import json
import subprocess
import sys
from datetime import date, timedelta

import pytest

from convertible_bond.data_providers import BondTerms, DataProvider
from convertible_bond.cb_events import CBEvent, CBEventStore
from convertible_bond.strategy_backtest import (
    PDEStrategyConfig,
    ScoreStrategyConfig,
    backtest_pde_strategy,
    backtest_score_strategy,
    build_rebalance_schedule,
    write_strategy_backtest_csv,
)
from convertible_bond.market_time import market_today


def test_pde_strategy_defaults_use_deviation_signal_and_reserve_cash():
    """下修优势信号已删 (两个 regime 都结构性无解), 默认落到「估值偏差」。"""
    config = PDEStrategyConfig()
    assert config.rank_signal == "deviation"
    assert config.down_reset_event_exit is False
    assert config.holding_mode == "top_score"
    assert config.funding_mode == "reserve_cash"
    assert config.execution_timing == "next_close"
    assert config.transaction_cost == pytest.approx(0.002)
    assert config.cash_yield_rate == pytest.approx(0.022)


def test_strategy_cli_help_exposes_only_pde_rank_signals():
    completed = subprocess.run(
        [sys.executable, "-m", "convertible_bond.cli.strategy_backtest", "--help"],
        check=True,
        capture_output=True,
        text=True,
    )
    help_text = completed.stdout
    # 下修两档仍留在 choices 里 (旧脚本传入会退化为 deviation), 但已标注为已删除
    assert "down_reset_robust_edge" in help_text
    assert "已删除" in help_text
    assert "deviation" in help_text
    assert "--holding-mode" not in help_text
    assert "--selection-view" not in help_text
    assert "机会分" not in help_text and "双低" not in help_text


def test_backtest_pde_strategy_uses_pde_config_by_default(monkeypatch):
    captured = {}

    def fake_backtest(provider, bond_codes, **kwargs):
        captured.update(kwargs)
        return {"config": kwargs["config"]}

    monkeypatch.setattr(
        "convertible_bond.strategy_backtest.backtest_score_strategy",
        fake_backtest,
    )
    result = backtest_pde_strategy(
        object(),
        ["113001.SH"],
        start_date=date(2025, 1, 2),
        end_date=date(2025, 2, 28),
    )
    assert isinstance(captured["config"], PDEStrategyConfig)
    assert result["config"].rank_signal == "deviation"


class StrategyFakeProvider(DataProvider):
    name = "strategy-fake"

    def __init__(self):
        self.terms = {
            "113001.SH": BondTerms(
                sec_name="甲转债",
                underlying_code="600001.SH",
                issue_date=date(2020, 1, 1),
                maturity_date=date(2030, 1, 1),
                face_value=100.0,
                conversion_price=100.0,
                credit_rating="AA+",
                outstanding_balance=10.0,
            ),
            "113002.SH": BondTerms(
                sec_name="乙转债",
                underlying_code="600002.SH",
                issue_date=date(2020, 1, 1),
                maturity_date=date(2030, 1, 1),
                face_value=100.0,
                conversion_price=100.0,
                credit_rating="AA+",
                outstanding_balance=10.0,
            ),
            "113003.SH": BondTerms(
                sec_name="丙转债",
                underlying_code="600003.SH",
                issue_date=date(2020, 1, 1),
                maturity_date=date(2030, 1, 1),
                face_value=100.0,
                conversion_price=100.0,
                credit_rating="AA+",
                outstanding_balance=10.0,
            ),
        }
        self.bond_history = {
            "113001.SH": [
                (date(2025, 1, 2), 100.0),
                (date(2025, 1, 31), 110.0),
                (date(2025, 2, 28), 120.0),
                (date(2025, 3, 31), 126.0),
            ],
            "113002.SH": [
                (date(2025, 1, 2), 200.0),
                (date(2025, 1, 31), 200.0),
                (date(2025, 2, 28), 190.0),
                (date(2025, 3, 31), 210.0),
            ],
            "113003.SH": [
                (date(2025, 1, 2), 90.0),
                (date(2025, 1, 31), 90.0),
                (date(2025, 2, 28), 91.0),
                (date(2025, 3, 31), 92.0),
            ],
        }
        self.stock_history = {
            "600001.SH": self._stock_series(100.0),
            "600002.SH": self._stock_series(200.0),
            "600003.SH": self._stock_series(90.0),
        }

    def _stock_series(self, base):
        start = date(2024, 12, 1)
        return [
            (start + timedelta(days=i), base + i * 0.01)
            for i in range(130)
            if (start + timedelta(days=i)).weekday() < 5
        ]

    def get_bond_terms(self, bond_code, valuation_date):
        return self.terms[bond_code]

    def get_stock_close(self, stock_code, on_date):
        for d, v in reversed(self.stock_history[stock_code]):
            if d <= on_date:
                return v
        raise RuntimeError("no stock close")

    def get_stock_history(self, stock_code, start, end):
        return [(d, v) for d, v in self.stock_history[stock_code] if start <= d <= end]

    def get_bond_history(self, bond_code, start, end):
        return [(d, v) for d, v in self.bond_history[bond_code] if start <= d <= end]


def test_build_rebalance_schedule_monthly_uses_last_weekday():
    schedule = build_rebalance_schedule(date(2025, 1, 2), date(2025, 3, 31), "M")

    assert schedule == [
        date(2025, 1, 2),
        date(2025, 1, 31),
        date(2025, 2, 28),
        date(2025, 3, 31),
    ]


def test_score_strategy_selects_top_score_and_compounds_returns(monkeypatch):
    provider = StrategyFakeProvider()
    calls = []

    def fake_batch_price(provider_arg, codes, *, valuation_date, **kwargs):
        calls.append(valuation_date)
        bonus_by_date = {
            date(2025, 1, 2): {"113001.SH": 0.18, "113002.SH": 0.04, "113003.SH": 0.01},
            date(2025, 1, 31): {"113001.SH": 0.02, "113002.SH": 0.18, "113003.SH": 0.01},
            date(2025, 2, 28): {"113001.SH": 0.12, "113002.SH": 0.01, "113003.SH": 0.02},
        }
        rows = []
        for code in codes:
            market = _latest(provider_arg.bond_history[code], valuation_date)
            bonus = bonus_by_date[valuation_date].get(code, 0.0)
            theo = market * (1.0 + bonus)
            rows.append({
                "bond_code": code,
                "bond_name": provider_arg.terms[code].sec_name,
                "stock_code": provider_arg.terms[code].underlying_code,
                "status": "ok",
                "S0": market,
                "K": 100.0,
                "sigma": 0.30,
                "theoretical_price": theo,
                "market_price": market,
                "deviation": (market - theo) / theo,
                "credit_rating": "AA+",
                "outstanding_balance": 10.0,
                "T": 3.0,
            })
        return rows

    monkeypatch.setattr(
        "convertible_bond.strategy_backtest.batch_price_from_provider_threaded",
        fake_batch_price,
    )

    result = backtest_score_strategy(
        provider,
        ["113001.SH", "113002.SH", "113003.SH"],
        start_date=date(2025, 1, 2),
        end_date=date(2025, 3, 31),
        config=ScoreStrategyConfig(top_n=1, rebalance_freq="M"),
    )

    assert calls == [date(2025, 1, 2), date(2025, 1, 31), date(2025, 2, 28)]
    assert [p["selected_codes"] for p in result["periods"]] == [
        ["113001.SH"],
        ["113002.SH"],
        ["113001.SH"],
    ]
    assert [p["period_return"] for p in result["periods"]] == pytest.approx([
        0.10,
        -0.05,
        0.05,
    ])
    assert result["summary"]["final_equity"] == pytest.approx(1.09725)
    assert result["summary"]["total_return"] == pytest.approx(0.09725)


def _positive_bonus_batch_price(provider_arg, codes, *, valuation_date, **kwargs):
    """所有传入券都判低估 (deviation<0 → 正机会分), 便于测选券/权重。"""
    rows = []
    for code in codes:
        hist = provider_arg.bond_history.get(code, [])
        market = next((v for d, v in reversed(hist) if d <= valuation_date), None)
        if market is None:
            rows.append({"bond_code": code, "status": "无市价",
                         "bond_name": provider_arg.terms[code].sec_name})
            continue
        theo = market * 1.08
        rows.append({
            "bond_code": code, "bond_name": provider_arg.terms[code].sec_name,
            "stock_code": provider_arg.terms[code].underlying_code, "status": "ok",
            "S0": market, "K": 100.0, "sigma": 0.28, "theoretical_price": theo,
            "market_price": market, "deviation": (market - theo) / theo,
            "credit_rating": "AA+", "outstanding_balance": 10.0, "T": 3.0,
        })
    return rows


def test_equal_pool_holds_whole_filtered_pool_equally(monkeypatch):
    """equal_pool: 等权持有整个候选池, 不按机会分取 Top N。"""
    provider = StrategyFakeProvider()
    monkeypatch.setattr(
        "convertible_bond.strategy_backtest.batch_price_from_provider_threaded",
        _positive_bonus_batch_price)
    result = backtest_score_strategy(
        provider, ["113001.SH", "113002.SH", "113003.SH"],
        start_date=date(2025, 1, 2), end_date=date(2025, 1, 31),
        config=ScoreStrategyConfig(
            top_n=1, rebalance_freq="M", holding_mode="pool", funding_mode="full_invest",
            min_confidence=None, exclude_risk_tags=(), compute_benchmark=False),
    )
    period = result["periods"][0]
    assert set(period["selected_codes"]) == {"113001.SH", "113002.SH", "113003.SH"}
    assert period["weight_denominator"] == 3        # 全池, 非 top_n=1
    assert period["cash_weight"] == pytest.approx(0.0)
    assert all(p["weight"] == pytest.approx(1 / 3) for p in period["positions"])
    # gross = 等权 (113001 +10%, 其余 0) = 3.33%
    assert period["gross_return"] == pytest.approx((0.10 + 0.0 + 0.0) / 3)


def test_equal_pool_redistributes_missing_price_positions(monkeypatch):
    """data-gap: 选中但缺成交价的标的权重摊回已持仓, 不留现金。"""
    provider = StrategyFakeProvider()
    # 113003 仅有期初价、无期末价 (staleness 超限) → 无法建仓 → 应被摊回
    # 只有**远早于期初**的一口价: 预筛看得见它 (所以照常进候选), 但期初执行价按陈旧
    # 上限判定不可用 → 真的建不了仓。此前这里写的是"只有期初价 90.0 (2025-01-02)",
    # 那是**买到了然后停牌**, 不是买不到 —— 现在那一档会按最后可得价平出并留在分母里
    # (见 test_position_bought_then_halted_is_marked_out_not_deleted)。
    provider.bond_history["113003.SH"] = [(date(2024, 6, 1), 90.0)]
    monkeypatch.setattr(
        "convertible_bond.strategy_backtest.batch_price_from_provider_threaded",
        _positive_bonus_batch_price)
    result = backtest_score_strategy(
        provider, ["113001.SH", "113002.SH", "113003.SH"],
        start_date=date(2025, 1, 2), end_date=date(2025, 1, 31),
        config=ScoreStrategyConfig(
            top_n=3, rebalance_freq="M", holding_mode="pool", funding_mode="full_invest",
            min_confidence=None, exclude_risk_tags=(), compute_benchmark=False),
    )
    period = result["periods"][0]
    assert len(period["positions"]) == 2            # 仅 113001/113002 建仓
    assert period["weight_denominator"] == 2        # 缺价者摊回, 非留现金 (否则=3)
    assert period["cash_weight"] == pytest.approx(0.0)
    assert period["gross_return"] == pytest.approx((0.10 + 0.0) / 2)  # 5%
    assert any(s["bond_code"] == "113003.SH" for s in period["skipped_positions"])


def test_pool_with_reserve_cash_leaves_gap_as_cash(monkeypatch):
    """三层矩阵: pool + reserve_cash → 持全池但缺价槽位留现金 (与 full_invest 摊回对照)。"""
    provider = StrategyFakeProvider()
    # 只有**远早于期初**的一口价: 预筛看得见它 (所以照常进候选), 但期初执行价按陈旧
    # 上限判定不可用 → 真的建不了仓。此前这里写的是"只有期初价 90.0 (2025-01-02)",
    # 那是**买到了然后停牌**, 不是买不到 —— 现在那一档会按最后可得价平出并留在分母里
    # (见 test_position_bought_then_halted_is_marked_out_not_deleted)。
    provider.bond_history["113003.SH"] = [(date(2024, 6, 1), 90.0)]
    monkeypatch.setattr(
        "convertible_bond.strategy_backtest.batch_price_from_provider_threaded",
        _positive_bonus_batch_price)
    result = backtest_score_strategy(
        provider, ["113001.SH", "113002.SH", "113003.SH"],
        start_date=date(2025, 1, 2), end_date=date(2025, 1, 31),
        config=ScoreStrategyConfig(
            top_n=3, rebalance_freq="M", holding_mode="pool", funding_mode="reserve_cash",
            min_confidence=None, exclude_risk_tags=(), compute_benchmark=False),
    )
    period = result["periods"][0]
    assert len(period["positions"]) == 2
    assert period["weight_denominator"] == 3        # 分母=候选数, 缺价槽位留现金
    assert period["cash_weight"] == pytest.approx(1 / 3)
    assert period["gross_return"] == pytest.approx((0.10 + 0.0) / 3)


def _overpriced_batch_price_factory(premium):
    """理论价 = 市价/(1+premium) → 每只券 deviation=+premium (模型判高估)。"""
    def _fake(provider_arg, codes, *, valuation_date, **kwargs):
        rows = []
        for code in codes:
            hist = provider_arg.bond_history.get(code, [])
            market = next((v for d, v in reversed(hist) if d <= valuation_date), None)
            if market is None:
                rows.append({"bond_code": code, "status": "无市价",
                             "bond_name": provider_arg.terms[code].sec_name})
                continue
            theo = market / (1.0 + premium)
            rows.append({
                "bond_code": code, "bond_name": provider_arg.terms[code].sec_name,
                "stock_code": provider_arg.terms[code].underlying_code, "status": "ok",
                "S0": market, "K": 100.0, "sigma": 0.28, "theoretical_price": theo,
                "market_price": market, "deviation": (market - theo) / theo,
                "credit_rating": "AA+", "outstanding_balance": 10.0, "T": 3.0,
            })
        return rows
    return _fake


def _exposure_test_config(**overrides):
    base = dict(rebalance_freq="M", holding_mode="pool", funding_mode="full_invest",
                min_confidence=None, exclude_risk_tags=(),
                compute_benchmark=False)
    base.update(overrides)
    return ScoreStrategyConfig(**base)


def test_exposure_valuation_scales_gross_cash_and_turnover(monkeypatch):
    """D 仓位层: medDev=+16% → gross=1-2.5*0.16=0.6; 收益/现金/换手/权重全口径缩放。"""
    provider = StrategyFakeProvider()
    monkeypatch.setattr(
        "convertible_bond.strategy_backtest.batch_price_from_provider_threaded",
        _overpriced_batch_price_factory(0.16))
    result = backtest_score_strategy(
        provider, ["113001.SH", "113002.SH", "113003.SH"],
        start_date=date(2025, 1, 2), end_date=date(2025, 1, 31),
        config=_exposure_test_config(exposure_mode="valuation"),
    )
    period = result["periods"][0]
    assert period["exposure"] == pytest.approx(0.6)
    assert period["median_deviation"] == pytest.approx(0.16)
    # raw = (10%+0+0)/3 = 3.33%; 缩放后 2%
    assert period["gross_return"] == pytest.approx(0.6 * 0.10 / 3)
    assert period["cash_weight"] == pytest.approx(0.4)
    assert period["turnover"] == pytest.approx(0.6)        # 首期建仓 = 买入 gross
    assert all(p["weight"] == pytest.approx(0.6 / 3) for p in period["positions"])


def test_exposure_full_records_median_but_does_not_scale(monkeypatch):
    provider = StrategyFakeProvider()
    monkeypatch.setattr(
        "convertible_bond.strategy_backtest.batch_price_from_provider_threaded",
        _overpriced_batch_price_factory(0.16))
    result = backtest_score_strategy(
        provider, ["113001.SH", "113002.SH", "113003.SH"],
        start_date=date(2025, 1, 2), end_date=date(2025, 1, 31),
        config=_exposure_test_config(),                     # exposure_mode 默认 full
    )
    period = result["periods"][0]
    assert period["exposure"] == pytest.approx(1.0)
    assert period["median_deviation"] == pytest.approx(0.16)  # 仍记录, 便于对照
    assert period["gross_return"] == pytest.approx(0.10 / 3)
    assert period["cash_weight"] == pytest.approx(0.0)


def test_exposure_valuation_caps_at_full_when_market_cheap(monkeypatch):
    """medDev<0 (市场低于模型公允) → 满仓上限 1.0。"""
    provider = StrategyFakeProvider()
    monkeypatch.setattr(
        "convertible_bond.strategy_backtest.batch_price_from_provider_threaded",
        _positive_bonus_batch_price)                        # dev ≈ -7.4%
    result = backtest_score_strategy(
        provider, ["113001.SH", "113002.SH", "113003.SH"],
        start_date=date(2025, 1, 2), end_date=date(2025, 1, 31),
        config=_exposure_test_config(exposure_mode="valuation"),
    )
    period = result["periods"][0]
    assert period["exposure"] == pytest.approx(1.0)
    assert period["median_deviation"] < 0


def test_exposure_valuation_floor(monkeypatch):
    """medDev=+30% → 1-0.75=0.25 → clip 到下限 0.5。"""
    provider = StrategyFakeProvider()
    monkeypatch.setattr(
        "convertible_bond.strategy_backtest.batch_price_from_provider_threaded",
        _overpriced_batch_price_factory(0.30))
    result = backtest_score_strategy(
        provider, ["113001.SH", "113002.SH", "113003.SH"],
        start_date=date(2025, 1, 2), end_date=date(2025, 1, 31),
        config=_exposure_test_config(exposure_mode="valuation"),
    )
    assert result["periods"][0]["exposure"] == pytest.approx(0.5)


def test_cash_yield_accrues_on_reserved_cash(monkeypatch):
    """P1: 闲置现金按年化 cash_yield_rate 计息 (缺价槽位留现金场景)。"""
    provider = StrategyFakeProvider()
    # 只有**远早于期初**的一口价: 预筛看得见它 (所以照常进候选), 但期初执行价按陈旧
    # 上限判定不可用 → 真的建不了仓。此前这里写的是"只有期初价 90.0 (2025-01-02)",
    # 那是**买到了然后停牌**, 不是买不到 —— 现在那一档会按最后可得价平出并留在分母里
    # (见 test_position_bought_then_halted_is_marked_out_not_deleted)。
    provider.bond_history["113003.SH"] = [(date(2024, 6, 1), 90.0)]
    monkeypatch.setattr(
        "convertible_bond.strategy_backtest.batch_price_from_provider_threaded",
        _positive_bonus_batch_price)
    result = backtest_score_strategy(
        provider, ["113001.SH", "113002.SH", "113003.SH"],
        start_date=date(2025, 1, 2), end_date=date(2025, 1, 31),
        config=ScoreStrategyConfig(
            top_n=3, rebalance_freq="M", holding_mode="pool", funding_mode="reserve_cash",
            cash_yield_rate=0.0365, mark_to_market=False,
            min_confidence=None, exclude_risk_tags=(), compute_benchmark=False),
    )
    period = result["periods"][0]
    accrual = (1 / 3) * 0.0365 * 29 / 365          # 29 天, 现金权重 1/3
    assert period["cash_yield_return"] == pytest.approx(accrual)
    assert period["period_return"] == pytest.approx(0.10 / 3 + accrual)


def test_cash_yield_accrues_on_exposure_scaled_cash(monkeypatch):
    """P1×D层: 择时缩放留出的现金 (1-g) 同样计息。"""
    provider = StrategyFakeProvider()
    monkeypatch.setattr(
        "convertible_bond.strategy_backtest.batch_price_from_provider_threaded",
        _overpriced_batch_price_factory(0.16))     # medDev=16% → g=0.6 → 现金 0.4
    result = backtest_score_strategy(
        provider, ["113001.SH", "113002.SH", "113003.SH"],
        start_date=date(2025, 1, 2), end_date=date(2025, 1, 31),
        config=_exposure_test_config(exposure_mode="valuation",
                                     cash_yield_rate=0.0365, mark_to_market=False),
    )
    period = result["periods"][0]
    accrual = 0.4 * 0.0365 * 29 / 365
    assert period["cash_yield_return"] == pytest.approx(accrual)
    assert period["period_return"] == pytest.approx(0.6 * 0.10 / 3 + accrual)


def test_cash_yield_reflected_in_mark_to_market_curve(monkeypatch):
    """P1: 日频净值曲线与区间记账同口径计息 (期末点一致)。"""
    provider = StrategyFakeProvider()
    # 只有**远早于期初**的一口价: 预筛看得见它 (所以照常进候选), 但期初执行价按陈旧
    # 上限判定不可用 → 真的建不了仓。此前这里写的是"只有期初价 90.0 (2025-01-02)",
    # 那是**买到了然后停牌**, 不是买不到 —— 现在那一档会按最后可得价平出并留在分母里
    # (见 test_position_bought_then_halted_is_marked_out_not_deleted)。
    provider.bond_history["113003.SH"] = [(date(2024, 6, 1), 90.0)]
    monkeypatch.setattr(
        "convertible_bond.strategy_backtest.batch_price_from_provider_threaded",
        _positive_bonus_batch_price)
    result = backtest_score_strategy(
        provider, ["113001.SH", "113002.SH", "113003.SH"],
        start_date=date(2025, 1, 2), end_date=date(2025, 1, 31),
        config=ScoreStrategyConfig(
            top_n=3, rebalance_freq="M", holding_mode="pool", funding_mode="reserve_cash",
            cash_yield_rate=0.0365, mark_to_market=True,
            min_confidence=None, exclude_risk_tags=(), compute_benchmark=False),
    )
    accrual = (1 / 3) * 0.0365 * 29 / 365
    assert result["summary"]["final_equity"] == pytest.approx(1 + 0.10 / 3 + accrual)


def test_pool_max_holdings_caps_by_balance_not_score(monkeypatch):
    """P3a: pool 截断按余额降序 (流动性), 分数不再从后门回流。"""
    provider = StrategyFakeProvider()
    balances = {"113001.SH": 1.0, "113002.SH": 50.0, "113003.SH": 30.0}
    bonus = {"113001.SH": 0.08, "113002.SH": 0.05, "113003.SH": 0.03}  # 分数 A>B>C

    def fake(provider_arg, codes, *, valuation_date, **kwargs):
        rows = []
        for code in codes:
            hist = provider_arg.bond_history.get(code, [])
            market = next((v for d, v in reversed(hist) if d <= valuation_date), None)
            theo = market * (1.0 + bonus[code])
            rows.append({
                "bond_code": code, "bond_name": provider_arg.terms[code].sec_name,
                "stock_code": provider_arg.terms[code].underlying_code, "status": "ok",
                "S0": market, "K": 100.0, "sigma": 0.28, "theoretical_price": theo,
                "market_price": market, "deviation": (market - theo) / theo,
                "credit_rating": "AA+", "outstanding_balance": balances[code], "T": 3.0,
            })
        return rows

    monkeypatch.setattr(
        "convertible_bond.strategy_backtest.batch_price_from_provider_threaded", fake)
    result = backtest_score_strategy(
        provider, ["113001.SH", "113002.SH", "113003.SH"],
        start_date=date(2025, 1, 2), end_date=date(2025, 1, 31),
        config=ScoreStrategyConfig(
            rebalance_freq="M", holding_mode="pool", funding_mode="full_invest",
            max_holdings=2, min_confidence=None, exclude_risk_tags=(),
            compute_benchmark=False),
    )
    # 取余额最大的 113002/113003, 而非分数最高的 113001
    assert set(result["periods"][0]["selected_codes"]) == {"113002.SH", "113003.SH"}


def test_benchmark_pays_membership_turnover_costs(monkeypatch):
    """P3b: 基准与策略同口径计成本 (首期建仓换手=1, 次期成员不变换手=0)。"""
    provider = StrategyFakeProvider()
    monkeypatch.setattr(
        "convertible_bond.strategy_backtest.batch_price_from_provider_threaded",
        _positive_bonus_batch_price)
    result = backtest_score_strategy(
        provider, ["113001.SH", "113002.SH", "113003.SH"],
        start_date=date(2025, 1, 2), end_date=date(2025, 2, 28),
        config=ScoreStrategyConfig(
            rebalance_freq="M", holding_mode="pool", funding_mode="full_invest",
            transaction_cost=0.01, min_confidence=None, exclude_risk_tags=()),
    )
    p1, p2 = result["periods"][0], result["periods"][1]
    # P1: 等权均值 (10%+0+0)/3, 减首期建仓换手 1×1% ；P2: A +9.09%, B -5%, C +1.11%, 换手 0
    assert p1["benchmark_return"] == pytest.approx(0.10 / 3 - 0.01)
    mean2 = (120 / 110 - 1 + 190 / 200 - 1 + 91 / 90 - 1) / 3
    assert p2["benchmark_return"] == pytest.approx(mean2)


def test_index_benchmark_curve_and_excess(monkeypatch):
    """真实指数第二基准: provider 提供指数收盘 → 输出指数净值曲线 + 超额。"""
    provider = StrategyFakeProvider()
    # 指数 000832.CSI 的收盘序列 (100→106 = +6%)
    provider.bond_history["000832.CSI"] = [
        (date(2025, 1, 2), 100.0), (date(2025, 1, 31), 103.0), (date(2025, 2, 28), 106.0)]
    monkeypatch.setattr(
        "convertible_bond.strategy_backtest.batch_price_from_provider_threaded",
        _positive_bonus_batch_price)
    result = backtest_score_strategy(
        provider, ["113001.SH", "113002.SH", "113003.SH"],
        start_date=date(2025, 1, 2), end_date=date(2025, 2, 28),
        config=ScoreStrategyConfig(
            rebalance_freq="M", holding_mode="pool", funding_mode="full_invest",
            benchmark_index_code="000832.CSI", mark_to_market=False,
            min_confidence=None, exclude_risk_tags=()),
    )
    idx_curve = result["index_benchmark_curve"]
    assert len(idx_curve) >= 2
    assert idx_curve[0]["equity"] == pytest.approx(1.0)
    assert result["summary"]["index_benchmark_total_return"] == pytest.approx(0.06)
    assert result["summary"]["excess_vs_index"] == pytest.approx(
        result["summary"]["total_return"] - 0.06)


def test_index_benchmark_absent_when_unavailable(monkeypatch):
    """数据源取不到指数 → 优雅缺省 (空曲线, 指数超额为 None)。"""
    provider = StrategyFakeProvider()   # 无 000832.CSI 历史
    monkeypatch.setattr(
        "convertible_bond.strategy_backtest.batch_price_from_provider_threaded",
        _positive_bonus_batch_price)
    result = backtest_score_strategy(
        provider, ["113001.SH", "113002.SH"],
        start_date=date(2025, 1, 2), end_date=date(2025, 1, 31),
        config=ScoreStrategyConfig(
            rebalance_freq="M", holding_mode="pool", funding_mode="full_invest",
            benchmark_index_code="000832.CSI", mark_to_market=False,
            min_confidence=None, exclude_risk_tags=()),
    )
    assert result["index_benchmark_curve"] == []
    assert result["summary"]["index_benchmark_total_return"] is None


def test_summary_includes_stability_block(monkeypatch):
    """引擎集成: summary['stability'] 含 Sharpe 块自助 + 跑赢基准概率 (多期+基准)。"""
    provider = StrategyFakeProvider()
    monkeypatch.setattr(
        "convertible_bond.strategy_backtest.batch_price_from_provider_threaded",
        _positive_bonus_batch_price)
    result = backtest_score_strategy(
        provider, ["113001.SH", "113002.SH", "113003.SH"],
        start_date=date(2025, 1, 2), end_date=date(2025, 2, 28),
        config=ScoreStrategyConfig(
            rebalance_freq="M", holding_mode="pool", funding_mode="full_invest",
            mark_to_market=False, min_confidence=None, exclude_risk_tags=()),
    )
    stab = result["summary"].get("stability")
    assert stab is not None
    # 2 期数据下 block 自助样本不足 (<4) → 各项优雅 None, 但键存在且 rolling 为列表
    assert "sharpe_bootstrap" in stab and "excess_bootstrap" in stab
    assert isinstance(stab["rolling_sharpe"], list)


def test_strategy_template_resets_new_knobs_to_full_config():
    """旧市场策略模板迁移为 PDE 估值策略，且参数完整归位。"""
    from convertible_bond.gui.controllers.strategy_backtest import (
        StrategyBacktestMixin, _STRATEGY_TEMPLATE_BASE)

    class Var:
        def __init__(self, value=""):
            self.value = value

        def set(self, v):
            self.value = v

        def get(self):
            return self.value

    class DummyApp(StrategyBacktestMixin):
        def __init__(self):
            for name in _STRATEGY_TEMPLATE_BASE:
                setattr(self, name, Var(""))
            self.v_st_view = Var("")
            self.v_st_summary = Var("")
            self.v_st_template = Var("稳健打底")
            # 模拟残留态: 用户上次手动改过的新旋钮
            self.v_st_weighting = Var("等权全池")
            self.v_st_cash_yield = Var("0")
            self.v_st_rank_signal = Var("下修优势")

    app = DummyApp()
    app._apply_strategy_template("稳健打底")
    assert app.v_st_weighting.get() == "Top N 排序"   # 随模板归位, 不残留
    assert app.v_st_cash_yield.get() == "2.2"
    assert app.v_st_rank_signal.get() == "估值偏差"
    assert app.v_st_view.get() == "综合机会"
    assert app.v_st_top_n.get() == "10"
    assert app.v_st_max_deviation.get() == "0"
    assert app.v_st_event_exit.get() is False
    assert app.v_st_template.get() == "估值偏差"


def test_strategy_gui_exposes_simplified_workflow_and_no_legacy_rank_controls():
    import inspect

    from convertible_bond.gui import constants
    from convertible_bond.gui.controllers import strategy_run
    from convertible_bond.gui.tabs import strategy as strategy_tab

    assert constants.STRATEGY_TEMPLATE_NAMES == ("估值偏差",)
    source = inspect.getsource(strategy_tab.build)

    # **扫字面量而不是扫源码文本**: 原来直接 `in source` 会连注释一起匹配, 于是
    # "解释为什么删掉了 X" 的那句注释本身就让守护测试变红 —— 抓不到真实故障形态,
    # 只抓到写注释的人。(同一个坑在 batch_watchlist 的几个守护测试里踩过。)
    import ast as _ast
    import textwrap as _textwrap
    literals = {
        node.value
        for node in _ast.walk(_ast.parse(_textwrap.dedent(source)))
        if isinstance(node, _ast.Constant) and isinstance(node.value, str)
    }
    for legacy_text in (
        "机会分", "双低", "等权全池", "选债规则", "持仓方式", "✓ 预检", "下修机会",
    ):
        assert not any(legacy_text in text for text in literals), (
            f"策略页仍有旧控件文案: {legacy_text}")
    # 策略只剩一个 —— 策略切换器从两选一的分段按钮换成静态标签, 免得让人一直找
    # 另一个选项 (「下修机会」已在上面的字面量扫描里挡掉)。
    assert "variable=app.v_st_template_display" in source
    assert 'for name in ("概览", "持仓", "诊断", "对比")' in source
    assert 'app.v_st_benchmark.set(True)' in source
    run_source = inspect.getsource(strategy_run.StrategyRunMixin._run_strategy_backtest)
    assert "compute_benchmark=True" in run_source
    assert 'benchmark_index_code="000832.CSI"' in run_source
    assert 'text=E("▶ 运行回测")' in source
    assert "运行策略" not in source


def test_strategy_pricing_params_are_independent_from_single_bond_page():
    from convertible_bond.gui.controllers.strategy_backtest import StrategyBacktestMixin

    class Var:
        def __init__(self, value):
            self.value = value

        def get(self):
            return self.value

    app = StrategyBacktestMixin()
    app.v_st_r = Var("2.2")
    app.v_st_spread = Var("3")
    app.v_st_p_down = Var("25")
    app.v_st_distress_k = Var("5")
    app.v_st_vol_window = Var("1M")
    # 单债页故意放入完全不同的值，策略参数不应读取它们。
    app.v_r = Var("99")
    app.v_spread = Var("88")
    app.v_p_down = Var("77")
    app.v_dk = Var("66")
    app.v_vol_window = Var("1Y")

    params = app._strategy_pricing_params()
    assert params["r"] == pytest.approx(0.022)
    assert params["base_spread"] == pytest.approx(0.03)
    assert params["p_down"] == pytest.approx(0.25)
    assert params["distress_k"] == pytest.approx(0.05)
    assert params["vol_window_days"] == 21
    # 「HV扰动%」「利差扰动bp」已删 —— 它们只配置稳健下修优势的四角点
    assert "pde_signal_sigma_rel_band" not in params
    assert "pde_signal_spread_band" not in params


def test_strategy_run_settings_record_effective_and_requested_data_sources():
    from convertible_bond.batch_pricing import AdmissionFilterConfig
    from convertible_bond.gui.controllers.strategy_backtest import StrategyBacktestMixin

    settings = StrategyBacktestMixin._strategy_run_settings(
        codes=["113001.SH"],
        start=date(2025, 1, 2),
        end=date(2025, 2, 28),
        source="Wind",
        requested_source="Akshare",
        template_name="估值偏差",
        history_mode="Wind高保真",
        gui_pool_mode="自选代码",
        engine_pool_mode="static",
        config=PDEStrategyConfig(),
        admission_config=AdmissionFilterConfig(),
        params={"r": 0.022, "p_down": 0.25},
        precheck={},
    )

    assert settings["data_source"] == "Wind"
    assert settings["requested_data_source"] == "Akshare"
    assert settings["strategy"]["template"] == "估值偏差"
    assert settings["strategy"]["strategy_type"] == "pde_valuation"
    assert settings["pricing"]["p_down"] == pytest.approx(0.25)


def test_backtest_cache_history_end_never_extends_into_future():
    """批量历史区间 clamp 到昨天: 否则磁盘缓存的'只缓存过去'守卫拒绝落盘, 复跑退化为全量重拉。"""
    from convertible_bond.strategy_backtest import _BacktestCacheProvider
    provider = StrategyFakeProvider()
    near_today = _BacktestCacheProvider(
        provider, start_date=market_today() - timedelta(days=90), end_date=market_today(),
        price_lookback_days=31, execution_lookahead_days=10, vol_window_days=21)
    assert near_today._history_end <= market_today() - timedelta(days=1)
    # 区间完全在过去时不受 clamp 影响 (保持原 padding 语义)
    past = _BacktestCacheProvider(
        provider, start_date=date(2024, 1, 2), end_date=date(2024, 6, 28),
        price_lookback_days=31, execution_lookahead_days=10, vol_window_days=21)
    assert past._history_end == date(2024, 6, 28) + timedelta(days=25)


def test_turnover_cost_uses_actual_holdings_not_phantom_selected(monkeypatch):
    """换手/成本必须基于实际持仓码, 不能把'选中但缺成交价'的票当真实持仓。

    P1 持 {A,B,C}(各1/3); P2 中 C **建不了仓** (期初无可用价) → 实际持 {A,B}(各1/2)。
    正确单边换手 = 卖出 C(1/3) = 1/3; 旧实现用 selected_codes(含C) + 分母=held,
    权重和>1, 会算出错误换手 (0.25)。

    fixture 改过一次: 原来给 C 的最后一口价在 01-31 (P2 期初), 于是 P2 **买得到** C,
    靠"期末无价 → 整条删掉"才得到 {A,B}。那正是 BT-1 修掉的语义 —— 现在买到了就会按
    最后可得价平出并留在持仓里。要测换手就得让 C 在 P2 真的建不了仓。
    """
    provider = StrategyFakeProvider()
    # C 的价只到 01-15: P1 可建仓 (entry 01-02, 期末按最后可得价 01-15 平出);
    # P2 期初 (01-31) 已无可用价 → 建不了仓
    provider.bond_history["113003.SH"] = [(date(2025, 1, 2), 90.0), (date(2025, 1, 15), 95.0)]
    monkeypatch.setattr(
        "convertible_bond.strategy_backtest.batch_price_from_provider_threaded",
        _positive_bonus_batch_price)
    result = backtest_score_strategy(
        provider, ["113001.SH", "113002.SH", "113003.SH"],
        start_date=date(2025, 1, 2), end_date=date(2025, 2, 28),
        config=ScoreStrategyConfig(
            rebalance_freq="M", holding_mode="pool", funding_mode="full_invest",
            transaction_cost=0.01, min_confidence=None, exclude_risk_tags=(),
            compute_benchmark=False),
    )
    p1, p2 = result["periods"][0], result["periods"][1]
    assert len(p1["positions"]) == 3 and p1["weight_denominator"] == 3   # P1 满仓持 3 只
    assert len(p2["positions"]) == 2 and p2["weight_denominator"] == 2   # P2 仅持 A,B
    # 正确单边换手 = 卖出 C(权重 1/3) = 1/3; 任何长仓等权换手都不应 >1
    assert p2["turnover"] == pytest.approx(1 / 3)
    assert p2["turnover"] <= 1.0 + 1e-9
    assert p2["cost"] == pytest.approx(p2["turnover"] * 0.01)




def test_score_strategy_applies_price_premium_and_sigma_filters(monkeypatch):
    provider = StrategyFakeProvider()

    def fake_batch_price(provider_arg, codes, *, valuation_date, **kwargs):
        rows = []
        for code in codes:
            terms = provider_arg.terms[code]
            if code == "113001.SH":
                market, sigma, s0 = 100.0, 0.30, 100.0
            elif code == "113002.SH":
                market, sigma, s0 = 130.0, 0.30, 100.0
            else:
                market, sigma, s0 = 100.0, 0.70, 100.0
            theo = market * 1.12
            rows.append({
                "bond_code": code,
                "bond_name": terms.sec_name,
                "stock_code": terms.underlying_code,
                "status": "ok",
                "S0": s0,
                "K": 100.0,
                "sigma": sigma,
                "theoretical_price": theo,
                "market_price": market,
                "deviation": (market - theo) / theo,
                "credit_rating": "AA+",
                "outstanding_balance": 10.0,
                "T": 3.0,
            })
        return rows

    monkeypatch.setattr(
        "convertible_bond.strategy_backtest.batch_price_from_provider_threaded",
        fake_batch_price,
    )

    result = backtest_score_strategy(
        provider,
        ["113001.SH", "113002.SH", "113003.SH"],
        start_date=date(2025, 1, 2),
        end_date=date(2025, 1, 31),
        config=ScoreStrategyConfig(
            top_n=3,
            min_market_price=80.0,
            max_market_price=120.0,
            max_conversion_premium=0.20,
            max_sigma=0.50,
        ),
    )

    assert result["periods"][0]["selected_codes"] == ["113001.SH"]
    period = result["periods"][0]
    assert period["candidate_rows"][0]["bond_code"] == "113001.SH"
    assert period["candidate_rows"][0]["selected"] is True
    # 机会分已删 —— 默认排序信号是「估值偏差」, 落选/入选解释里写的是 PDE 偏差
    assert "偏差" in period["candidate_rows"][0]["selection_reason"]
    assert any(
        row["bond_code"] == "113002.SH" and "价格预筛" in row["reason"]
        for row in period["rejection_rows"]
    )
    assert any(
        row["bond_code"] == "113003.SH" and "HV" in row["reason"]
        for row in period["rejection_rows"]
    )


def test_price_prefilter_skips_out_of_range_codes_before_pricing(monkeypatch):
    provider = StrategyFakeProvider()
    priced_code_sets = []

    def fake_batch_price(provider_arg, codes, *, valuation_date, **kwargs):
        priced_code_sets.append(list(codes))
        return [
            _row(code, provider_arg, _latest(provider_arg.bond_history[code], valuation_date), -0.10)
            for code in codes
        ]

    monkeypatch.setattr(
        "convertible_bond.strategy_backtest.batch_price_from_provider_threaded",
        fake_batch_price,
    )

    result = backtest_score_strategy(
        provider,
        ["113001.SH", "113002.SH", "113003.SH"],
        start_date=date(2025, 1, 2),
        end_date=date(2025, 1, 31),
        config=ScoreStrategyConfig(
            top_n=2,
            min_confidence=None,
            max_market_price=120.0,
            compute_benchmark=False,
        ),
    )

    assert priced_code_sets == [["113001.SH", "113003.SH"]]
    assert result["periods"][0]["pre_filtered_count"] == 1
    assert result["diagnostics"]["performance"]["price_prefilter_excluded"] == 1


def test_pricing_snapshot_cache_reuses_pricing_rows(monkeypatch):
    provider = StrategyFakeProvider()
    snapshot_cache = {}
    calls = []

    def fake_batch_price(provider_arg, codes, *, valuation_date, **kwargs):
        calls.append((valuation_date, tuple(codes)))
        return [
            _row(code, provider_arg, _latest(provider_arg.bond_history[code], valuation_date), -0.10)
            for code in codes
        ]

    monkeypatch.setattr(
        "convertible_bond.strategy_backtest.batch_price_from_provider_threaded",
        fake_batch_price,
    )

    kwargs = dict(
        provider=provider,
        bond_codes=["113001.SH", "113002.SH"],
        start_date=date(2025, 1, 2),
        end_date=date(2025, 1, 31),
        config=ScoreStrategyConfig(top_n=1, min_confidence=None, compute_benchmark=False),
        pricing_snapshot_cache=snapshot_cache,
    )
    first = backtest_score_strategy(**kwargs)
    second = backtest_score_strategy(**kwargs)

    assert len(calls) == 1
    assert first["diagnostics"]["performance"]["pricing_snapshot_misses"] == 1
    assert second["diagnostics"]["performance"]["pricing_snapshot_hits"] == 1


def test_score_strategy_reports_stage_progress_before_period_finish(monkeypatch):
    provider = StrategyFakeProvider()
    events = []

    def fake_batch_price(provider_arg, codes, *, valuation_date, progress_cb=None, **kwargs):
        if progress_cb is not None:
            progress_cb(1, len(codes))
            progress_cb(len(codes), len(codes))
        return [
            _row(code, provider_arg, _latest(provider_arg.bond_history[code], valuation_date), -0.10)
            for code in codes
        ]

    monkeypatch.setattr(
        "convertible_bond.strategy_backtest.batch_price_from_provider_threaded",
        fake_batch_price,
    )

    backtest_score_strategy(
        provider,
        ["113001.SH", "113002.SH"],
        start_date=date(2025, 1, 2),
        end_date=date(2025, 1, 31),
        config=ScoreStrategyConfig(top_n=1, min_confidence=None, compute_benchmark=False),
        stage_cb=lambda *args: events.append(args),
    )

    stages = [event[0] for event in events]
    assert "准入筛选" in stages
    assert "价格预筛" in stages
    assert "定价" in stages
    assert events[0] == ("准入筛选", 0, 2, 0, 1)


def test_score_strategy_aborts_on_wind_terms_transport_outage():
    class FailingTermsProvider(DataProvider):
        name = "wind-outage"

        def get_bond_terms(self, bond_code, valuation_date):
            raise RuntimeError(
                "Wind 取 113001.SH 条款失败: ErrorCode=-40521007, "
                "Data=[['WSS: SkyClient request failed']]"
            )

        def get_stock_close(self, stock_code, on_date):
            raise RuntimeError("unused")

        def get_stock_history(self, stock_code, start, end):
            raise RuntimeError("unused")

        def get_bond_history(self, bond_code, start, end):
            raise RuntimeError("unused")

    codes = [f"113{i:03d}.SH" for i in range(30)]

    # 30 只全部失败 (100%) = 系统性故障, 仍应中止。
    with pytest.raises(RuntimeError, match="系统性 Wind 条款获取失败"):
        backtest_score_strategy(
            FailingTermsProvider(),
            codes,
            start_date=date(2025, 1, 2),
            end_date=date(2025, 1, 31),
            config=ScoreStrategyConfig(top_n=1, min_confidence=None),
        )


def test_source_outage_guard_skips_partial_failure_but_aborts_systemic():
    """部分券瞬时失败 (限流) 应跳过继续; 仅系统性故障 (近全失败) 才中止。"""
    from convertible_bond.strategy_backtest import _raise_if_source_transport_outage

    def excluded(n_fail):
        return [
            (f"1232{i:02d}.SZ",
             "条款获取失败: Wind 取 x 条款失败: ErrorCode=-40521007, "
             "Data=[['WSS: SkyClient request failed']]")
            for i in range(n_fail)
        ]

    # 28% 失败 (用户实测场景 137/490): 多数成功 → 不中止
    _raise_if_source_transport_outage(
        excluded(137), total_count=490, period_start=date(2025, 6, 30), phase="准入筛选")

    # 少量失败 (<20 只): 不中止
    _raise_if_source_transport_outage(
        excluded(5), total_count=490, period_start=date(2025, 6, 30), phase="准入筛选")

    # 96% 失败 (Wind 未登录/宕机): 中止
    with pytest.raises(RuntimeError, match="系统性 Wind 条款获取失败"):
        _raise_if_source_transport_outage(
            excluded(470), total_count=490, period_start=date(2025, 6, 30), phase="准入筛选")


def _row(code, provider, market, deviation):
    return {
        "bond_code": code,
        "bond_name": provider.terms[code].sec_name,
        "stock_code": provider.terms[code].underlying_code,
        "status": "ok",
        "S0": market,
        "K": 100.0,
        "sigma": 0.30,
        "theoretical_price": market / (1.0 + deviation),
        "market_price": market,
        "deviation": deviation,
        "credit_rating": "AA+",
        "outstanding_balance": 10.0,
        "T": 3.0,
    }


def test_benchmark_equal_weights_universe_and_reports_excess(monkeypatch):
    provider = StrategyFakeProvider()
    deviation_by_code = {"113001.SH": -0.15, "113002.SH": 0.0, "113003.SH": 0.05}

    def fake_batch_price(provider_arg, codes, *, valuation_date, **kwargs):
        return [
            _row(code, provider_arg, _latest(provider_arg.bond_history[code], valuation_date),
                 deviation_by_code[code])
            for code in codes
        ]

    monkeypatch.setattr(
        "convertible_bond.strategy_backtest.batch_price_from_provider_threaded",
        fake_batch_price,
    )

    result = backtest_score_strategy(
        provider,
        ["113001.SH", "113002.SH", "113003.SH"],
        start_date=date(2025, 1, 2),
        end_date=date(2025, 1, 31),
        config=ScoreStrategyConfig(top_n=1, min_confidence=None),
    )

    period = result["periods"][0]
    assert period["selected_codes"] == ["113001.SH"]
    assert period["period_return"] == pytest.approx(0.10)
    # 基准 = 等权全可投池 (113001 +10%, 其余 0%) / 3
    assert period["benchmark_return"] == pytest.approx(0.10 / 3)
    assert result["benchmark_curve"][-1]["equity"] == pytest.approx(1.0 + 0.10 / 3)
    assert result["summary"]["excess_return"] == pytest.approx(0.10 - 0.10 / 3)


def test_transaction_cost_reduces_period_return(monkeypatch):
    provider = StrategyFakeProvider()
    deviation_by_code = {"113001.SH": -0.15, "113002.SH": 0.0, "113003.SH": 0.05}

    def fake_batch_price(provider_arg, codes, *, valuation_date, **kwargs):
        return [
            _row(code, provider_arg, _latest(provider_arg.bond_history[code], valuation_date),
                 deviation_by_code[code])
            for code in codes
        ]

    monkeypatch.setattr(
        "convertible_bond.strategy_backtest.batch_price_from_provider_threaded",
        fake_batch_price,
    )

    codes = ["113001.SH", "113002.SH", "113003.SH"]
    res = backtest_score_strategy(
        provider, codes,
        start_date=date(2025, 1, 2), end_date=date(2025, 1, 31),
        config=ScoreStrategyConfig(top_n=1, min_confidence=None, transaction_cost=0.01),
    )

    period = res["periods"][0]
    # 首期从空仓建满 113001, 单边换手 1.0; 成本 = 1.0 * 0.01
    assert period["gross_return"] == pytest.approx(0.10)
    assert period["turnover"] == pytest.approx(1.0)
    assert period["cost"] == pytest.approx(0.01)
    assert period["period_return"] == pytest.approx(0.09)


def test_mark_to_market_curve_uses_intraperiod_closes_for_drawdown(monkeypatch):
    provider = StrategyFakeProvider()
    provider.bond_history["113001.SH"] = [
        (date(2025, 1, 2), 100.0),
        (date(2025, 1, 15), 80.0),
        (date(2025, 1, 31), 110.0),
    ]

    def fake_batch_price(provider_arg, codes, *, valuation_date, **kwargs):
        return [_row("113001.SH", provider_arg, 100.0, -0.15)]

    monkeypatch.setattr(
        "convertible_bond.strategy_backtest.batch_price_from_provider_threaded",
        fake_batch_price,
    )

    result = backtest_score_strategy(
        provider,
        ["113001.SH"],
        start_date=date(2025, 1, 2),
        end_date=date(2025, 1, 31),
        config=ScoreStrategyConfig(top_n=1, min_confidence=None),
    )

    curve_by_date = {row["date"]: row["equity"] for row in result["equity_curve"]}
    assert curve_by_date[date(2025, 1, 15)] == pytest.approx(0.80)
    assert curve_by_date[date(2025, 1, 31)] == pytest.approx(1.10)
    assert result["summary"]["max_drawdown"] == pytest.approx(0.20)
    assert result["summary"]["volatility_basis"] == "daily_mtm"
    assert result["summary"]["calmar"] is not None
    assert result["diagnostics"]["monthly_returns"][0]["period"] == "2025-01"
    assert result["diagnostics"]["attribution"]["top_contributors"][0]["bond_code"] == "113001.SH"


def test_next_close_execution_uses_next_available_close(monkeypatch):
    provider = StrategyFakeProvider()
    provider.bond_history["113001.SH"] = [
        (date(2025, 1, 2), 100.0),
        (date(2025, 1, 3), 101.0),
        (date(2025, 1, 31), 108.0),
        (date(2025, 2, 3), 111.0),
    ]

    def fake_batch_price(provider_arg, codes, *, valuation_date, **kwargs):
        return [_row("113001.SH", provider_arg, 100.0, -0.15)]

    monkeypatch.setattr(
        "convertible_bond.strategy_backtest.batch_price_from_provider_threaded",
        fake_batch_price,
    )

    result = backtest_score_strategy(
        provider,
        ["113001.SH"],
        start_date=date(2025, 1, 2),
        end_date=date(2025, 1, 31),
        config=ScoreStrategyConfig(
            top_n=1,
            min_confidence=None,
            execution_timing="next_close",
        ),
    )

    period = result["periods"][0]
    assert period["positions"][0]["entry_date"] == date(2025, 1, 3)
    assert period["positions"][0]["exit_date"] == date(2025, 2, 3)
    assert period["period_return"] == pytest.approx(111.0 / 101.0 - 1.0)


def test_stale_signal_close_price_is_skipped_as_cash(monkeypatch):
    provider = StrategyFakeProvider()
    provider.bond_history["113001.SH"] = [
        (date(2024, 12, 20), 100.0),
        (date(2025, 1, 31), 110.0),
    ]

    def fake_batch_price(provider_arg, codes, *, valuation_date, **kwargs):
        return [_row("113001.SH", provider_arg, 100.0, -0.15)]

    monkeypatch.setattr(
        "convertible_bond.strategy_backtest.batch_price_from_provider_threaded",
        fake_batch_price,
    )

    result = backtest_score_strategy(
        provider,
        ["113001.SH"],
        start_date=date(2025, 1, 2),
        end_date=date(2025, 1, 31),
        config=ScoreStrategyConfig(
            top_n=1,
            min_confidence=None,
            max_price_staleness_days=3,
        ),
    )

    period = result["periods"][0]
    assert period["positions"] == []
    assert period["skipped_positions"][0]["reason"].startswith("缺少期初")
    assert period["cash_weight"] == pytest.approx(1.0)
    assert period["period_return"] == pytest.approx(0.0)
    assert result["summary"]["avg_cash_weight"] == pytest.approx(1.0)
    assert any("现金权重" in warning for warning in result["diagnostics"]["warnings"])


def test_skipped_position_counts_as_cash(monkeypatch):
    provider = StrategyFakeProvider()
    # 113002 在期末查找窗口内无收盘价 -> 无法建仓, 应按现金计入分母
    provider.bond_history["113002.SH"] = [(date(2024, 1, 1), 200.0)]
    market_by_code = {"113001.SH": 100.0, "113002.SH": 200.0}
    deviation_by_code = {"113001.SH": -0.15, "113002.SH": -0.12}

    def fake_batch_price(provider_arg, codes, *, valuation_date, **kwargs):
        return [
            _row(code, provider_arg, market_by_code[code], deviation_by_code[code])
            for code in codes
        ]

    monkeypatch.setattr(
        "convertible_bond.strategy_backtest.batch_price_from_provider_threaded",
        fake_batch_price,
    )

    result = backtest_score_strategy(
        provider,
        ["113001.SH", "113002.SH"],
        start_date=date(2025, 1, 2),
        end_date=date(2025, 1, 31),
        config=ScoreStrategyConfig(top_n=2, min_confidence=None, compute_benchmark=False),
    )

    period = result["periods"][0]
    assert [p["bond_code"] for p in period["positions"]] == ["113001.SH"]
    assert len(period["skipped_positions"]) == 1
    assert period["cash_weight"] == pytest.approx(0.5)
    # 仅 113001 +10% 成交, 另一半按现金 -> 0.10 / 2
    assert period["period_return"] == pytest.approx(0.05)


def test_strategy_snapshot_json_round_trips_dates_and_nonfinite_values():
    from convertible_bond.gui.controllers.strategy_backtest import (
        _strategy_snapshot_jsonable,
        _strategy_snapshot_object_hook,
    )

    payload = {
        "saved_at": date(2026, 5, 28),
        "result": {
            "start_date": date(2025, 5, 28),
            "equity_curve": [
                {"date": date(2025, 6, 1), "equity": 1.0},
                {"date": date(2025, 7, 1), "equity": float("nan")},
            ],
            "summary": {"sharpe": float("inf")},
        },
    }

    encoded = json.dumps(
        _strategy_snapshot_jsonable(payload),
        ensure_ascii=False,
        allow_nan=False,
    )
    restored = json.loads(encoded, object_hook=_strategy_snapshot_object_hook)

    assert restored["saved_at"] == date(2026, 5, 28)
    assert restored["result"]["start_date"] == date(2025, 5, 28)
    assert restored["result"]["equity_curve"][0]["date"] == date(2025, 6, 1)
    assert restored["result"]["equity_curve"][1]["equity"] is None
    assert restored["result"]["summary"]["sharpe"] is None


def test_strategy_snapshot_loader_includes_newer_legacy_file(tmp_path):
    from convertible_bond.gui.controllers.strategy_backtest import (
        StrategyBacktestMixin,
        _strategy_snapshot_jsonable,
    )

    snap_dir = tmp_path / "strategy_backtest_snapshots"
    snap_dir.mkdir()
    old_snapshot = snap_dir / "strategy_backtest_2025-05-28_2026-05-28_Q_top10_old.json"
    legacy_snapshot = tmp_path / "strategy_backtest_snapshot.json"

    def write_snapshot(path, final_equity):
        payload = {
            "schema_version": 1,
            "saved_at": date(2026, 5, 30),
            "result": {
                "start_date": date(2025, 5, 30),
                "end_date": date(2026, 5, 30),
                "summary": {"final_equity": final_equity},
            },
        }
        path.write_text(json.dumps(_strategy_snapshot_jsonable(payload)), encoding="utf-8")

    write_snapshot(old_snapshot, 0.90)
    write_snapshot(legacy_snapshot, 1.35)

    class DummyApp(StrategyBacktestMixin):
        def __init__(self):
            self.records = []
            self.dirty = False

        def _strategy_snapshots_dir(self):
            return snap_dir

        def _strategy_snapshot_path(self):
            return legacy_snapshot

        def _record_strategy_comparison_result(self, result):
            self.records.append(result)

        def _mark_strategy_tabs_dirty(self):
            self.dirty = True

    app = DummyApp()
    app._load_strategy_backtest_snapshot(silent=True, render=False)

    assert [record["summary"]["final_equity"] for record in app.records] == [0.90, 1.35]
    assert app._last_strategy_bt_result["summary"]["final_equity"] == 1.35
    assert app.dirty is True


def test_strategy_snapshot_loader_dedupes_latest_copy(tmp_path):
    from convertible_bond.gui.controllers.strategy_backtest import (
        StrategyBacktestMixin,
        _strategy_snapshot_jsonable,
    )

    snap_dir = tmp_path / "strategy_backtest_snapshots"
    snap_dir.mkdir()
    archived_snapshot = snap_dir / "strategy_backtest_2025-05-30_2026-05-30_M_top10_copy.json"
    legacy_snapshot = tmp_path / "strategy_backtest_snapshot.json"
    payload = {
        "schema_version": 1,
        "saved_at": date(2026, 6, 1),
        "result": {
            "start_date": date(2025, 5, 30),
            "end_date": date(2026, 5, 30),
            "summary": {"final_equity": 1.35},
        },
    }
    text = json.dumps(_strategy_snapshot_jsonable(payload), ensure_ascii=False)
    archived_snapshot.write_text(text, encoding="utf-8")
    legacy_snapshot.write_text(text, encoding="utf-8")

    class DummyApp(StrategyBacktestMixin):
        def __init__(self):
            self.records = []
            self.dirty = False

        def _strategy_snapshots_dir(self):
            return snap_dir

        def _strategy_snapshot_path(self):
            return legacy_snapshot

        def _record_strategy_comparison_result(self, result):
            self.records.append(result)

        def _mark_strategy_tabs_dirty(self):
            self.dirty = True

    app = DummyApp()
    app._load_strategy_backtest_snapshot(silent=True, render=False)

    assert len(app.records) == 1
    assert app.records[0]["_snapshot_path"] == str(archived_snapshot)


def test_strategy_snapshot_save_writes_metadata_and_strips_runtime_fields(tmp_path):
    from convertible_bond.gui.controllers.strategy_backtest import StrategyBacktestMixin

    class DummyApp(StrategyBacktestMixin):
        def __init__(self):
            self._last_strategy_bt_result = {
                "start_date": date(2025, 5, 30),
                "end_date": date(2026, 5, 30),
                "config": {
                    "selection_view": "综合机会",
                    "rebalance_freq": "M",
                    "top_n": 10,
                    "holding_mode": "pool",
                    # 手写的**旧配置字典** (不是新跑出来的) —— 快照保存这条路
                    # 必须原样透传未知键, 否则历史快照会在保存时被悄悄改写。
                    "rank_signal": "down_reset_robust_edge",
                    "min_down_reset_edge_value": 1.5,
                    "down_reset_event_exit": True,
                    "funding_mode": "full_invest",
                    "max_holdings": None,
                    "top_n_shortfall_policy": "renormalize",
                    "history_mode": "Wind高保真",
                },
                "summary": {
                    "final_equity": 1.35,
                    "total_return": 0.35,
                    "max_drawdown": 0.07,
                    "sharpe": 1.8,
                    "calmar": 5.1,
                },
                "run_settings": {
                    "data_source": "Wind",
                    "history_mode": "Wind高保真",
                    "pool": {
                        "gui_mode": "自选代码",
                        "engine_mode": "static",
                        "code_count": 2,
                        "bond_codes": ["113001.SH", "113002.SH"],
                    },
                    "strategy": {
                        "top_n": 10,
                        "rank_signal": "down_reset_robust_edge",
                        "min_down_reset_edge_value": 1.5,
                        "down_reset_event_exit": True,
                        "top_n_shortfall_policy": "cash",
                    },
                    "admission_filter": {
                        "min_outstanding_balance": 0.5,
                        "min_credit_rating": "A+",
                    },
                    "pricing": {
                        "r": 0.022,
                        "base_spread": 0.03,
                        "p_down": 0.25,
                        "distress_k": 0.05,
                        "M": 120,
                        "N": 400,
                        "vol_window_days": 21,
                    },
                },
                "periods": [{"start_date": date(2025, 5, 30)}],
                "equity_curve": [{"date": date(2025, 5, 30), "equity": 1.0}],
                "_snapshot_path": "/tmp/old.json",
            }

        def _strategy_snapshots_dir(self):
            return tmp_path / "strategy_backtest_snapshots"

        def _strategy_snapshot_path(self):
            return tmp_path / "strategy_backtest_snapshot.json"

    app = DummyApp()
    info = app._save_strategy_backtest_snapshot()

    assert info is not None
    archive_path = info["path"]
    latest_path = info["latest_path"]
    assert archive_path.exists()
    assert latest_path.exists()
    archive_payload = json.loads(archive_path.read_text(encoding="utf-8"))
    latest_payload = json.loads(latest_path.read_text(encoding="utf-8"))
    assert archive_payload == latest_payload
    assert archive_payload["schema_version"] == 3
    assert archive_payload["snapshot_id"] == info["snapshot_id"]
    assert archive_payload["meta"]["config"]["selection_view"] == "综合机会"
    # 三层字段进入快照 meta (P3a)
    assert archive_payload["meta"]["config"]["holding_mode"] == "pool"
    assert archive_payload["meta"]["config"]["rank_signal"] == "down_reset_robust_edge"
    assert archive_payload["meta"]["config"]["min_down_reset_edge_value"] == pytest.approx(1.5)
    assert archive_payload["meta"]["config"]["down_reset_event_exit"] is True
    assert archive_payload["meta"]["config"]["funding_mode"] == "full_invest"
    assert archive_payload["meta"]["config"]["top_n_shortfall_policy"] == "renormalize"  # 兼容镜像
    assert archive_payload["meta"]["config"]["history_mode"] == "Wind高保真"
    assert archive_payload["result"]["config"]["history_mode"] == "Wind高保真"
    assert archive_payload["meta"]["run_settings"]["data_source"] == "Wind"
    assert archive_payload["meta"]["run_settings"]["pool"]["bond_codes"] == [
        "113001.SH", "113002.SH",
    ]
    assert archive_payload["meta"]["run_settings"]["pricing"]["M"] == 120
    assert archive_payload["meta"]["config"]["strategy_type"] == "pde_valuation"
    assert archive_payload["meta"]["model_settings"]["p_down"] == pytest.approx(0.25)
    assert archive_payload["meta"]["admission_filter"]["min_credit_rating"] == "A+"
    assert archive_payload["result"]["run_settings"]["admission_filter"]["min_credit_rating"] == "A+"
    assert archive_payload["meta"]["period_count"] == 1
    assert "_snapshot_path" not in archive_payload["result"]


def test_comparison_label_uses_snapshot_config_not_current_gui_state():
    from convertible_bond.gui.controllers.strategy_backtest import StrategyBacktestMixin

    class Var:
        def __init__(self, value):
            self.value = value

        def get(self):
            return self.value

    app = StrategyBacktestMixin()
    app.v_st_template = Var("当前界面的其他策略")
    app.v_st_view = Var("当前视图")
    app.v_st_freq = Var("周")
    app.v_st_top_n = Var("99")
    app._strategy_compare_results = []
    result = {
        "_snapshot_id": "snapshot-a",
        "start_date": date(2025, 1, 2),
        "end_date": date(2025, 2, 28),
        "config": {
            "strategy_type": "pde_down_reset",
            "rank_signal": "down_reset_robust_edge",
            "rebalance_freq": "M",
            "top_n": 10,
            "history_mode": "Wind高保真",
        },
        "summary": {"final_equity": 1.1, "max_drawdown": 0.05},
    }

    app._record_strategy_comparison_result(result)

    # 这是**旧快照**的兼容渲染 (信号已删, 但快照里存着) —— 标签映射必须留着,
    # 否则历史对比条目会退化成「旧策略」。
    label = app._strategy_compare_results[0]["label"]
    assert "下修机会" in label and "PDE下修错定价" not in label
    assert "Wind 历史" in label
    assert "Top10" in label
    assert "当前界面" not in label and "Top99" not in label


def test_old_snapshot_pde_config_is_upgraded_in_memory():
    from convertible_bond.gui.controllers.strategy_backtest import StrategyBacktestMixin

    # 旧快照里的 down_reset_robust_edge 由 _normalize_rank_signal 落到 deviation ——
    # 该信号已删, 所以升级后的 strategy_type 是 pde_valuation 而不是硬崩。
    result = {
        "config": {"rank_signal": "down_reset_robust_edge"},
        "run_settings": {"history_mode": "Wind高保真"},
    }
    StrategyBacktestMixin._patch_snapshot_strategy_config(result)
    assert result["config"]["strategy_type"] == "pde_valuation"
    assert result["config"]["history_mode"] == "Wind高保真"


def test_delete_selected_comparison_clears_current_result_and_snapshot_files(tmp_path):
    from convertible_bond.gui.controllers.strategy_backtest import StrategyBacktestMixin

    class Var:
        def __init__(self, value=None):
            self.value = value

        def set(self, value):
            self.value = value

        def get(self):
            return self.value

    class Button:
        def __init__(self):
            self.state = None

        def configure(self, **kwargs):
            self.state = kwargs.get("state", self.state)

    class Label:
        def __init__(self):
            self.text_color = None

        def configure(self, **kwargs):
            self.text_color = kwargs.get("text_color", self.text_color)

    class Tree:
        def selection(self):
            return ("0",)

    class DummyApp(StrategyBacktestMixin):
        def __init__(self, archive_path, latest_path):
            result = {
                "_snapshot_id": "snap-current",
                "_snapshot_path": str(archive_path),
                "summary": {"final_equity": 1.2},
            }
            self._last_strategy_bt_result = result
            self._strategy_compare_results = [{
                "snapshot_id": "snap-current",
                "result": result,
                "snapshot_path": str(archive_path),
            }]
            self._strategy_compare_tree = Tree()
            self.v_st_status = Var()
            self.strategy_bt_progress = Var(1.0)
            self.btn_strategy_bt_csv = Button()
            self._strategy_stat_vars = {"final_equity": Var("1.2000")}
            self._strategy_stat_labels = {"final_equity": Label()}
            self.strategy_bt_compare_frame = object()
            self.rendered_compare = 0
            self.cleared_panels = 0
            self.confirm_count = None
            self._latest_path = latest_path

        def _strategy_snapshot_path(self):
            return self._latest_path

        def _confirm_delete_selected_comparison(self, count):
            self.confirm_count = count
            return True

        def _render_strategy_comparison(self):
            self.rendered_compare += 1

        def _clear_strategy_panel(self, frame):
            self.cleared_panels += 1

    archive_path = tmp_path / "strategy_backtest_snapshots" / "strategy_backtest_current.json"
    archive_path.parent.mkdir()
    archive_path.write_text('{"snapshot_id":"snap-current","result":{"summary":{}}}', encoding="utf-8")
    latest_path = tmp_path / "strategy_backtest_snapshot.json"
    latest_path.write_text('{"snapshot_id":"snap-current","result":{"summary":{}}}', encoding="utf-8")

    app = DummyApp(archive_path, latest_path)
    app._delete_selected_comparison()

    assert app.confirm_count == 1
    assert app._strategy_compare_results == []
    assert app._last_strategy_bt_result is None
    assert app.strategy_bt_progress.get() == 0
    assert app.btn_strategy_bt_csv.state == "disabled"
    assert app._strategy_stat_vars["final_equity"].get() == "—"
    assert "当前回测结果" in app.v_st_status.get()
    assert app.rendered_compare == 1
    assert not archive_path.exists()
    assert not latest_path.exists()


def test_delete_selected_comparison_cancel_keeps_records_and_snapshots(tmp_path):
    from convertible_bond.gui.controllers.strategy_backtest import StrategyBacktestMixin

    class Tree:
        def selection(self):
            return ("0",)

    class DummyApp(StrategyBacktestMixin):
        def __init__(self, archive_path, latest_path):
            result = {
                "_snapshot_id": "snap-current",
                "_snapshot_path": str(archive_path),
                "summary": {"final_equity": 1.2},
            }
            self._last_strategy_bt_result = result
            self._strategy_compare_results = [{
                "snapshot_id": "snap-current",
                "result": result,
                "snapshot_path": str(archive_path),
            }]
            self._strategy_compare_tree = Tree()
            self.confirm_count = None
            self._latest_path = latest_path

        def _strategy_snapshot_path(self):
            return self._latest_path

        def _confirm_delete_selected_comparison(self, count):
            self.confirm_count = count
            return False

    archive_path = tmp_path / "strategy_backtest_snapshots" / "strategy_backtest_current.json"
    archive_path.parent.mkdir()
    archive_path.write_text('{"snapshot_id":"snap-current","result":{"summary":{}}}', encoding="utf-8")
    latest_path = tmp_path / "strategy_backtest_snapshot.json"
    latest_path.write_text('{"snapshot_id":"snap-current","result":{"summary":{}}}', encoding="utf-8")

    app = DummyApp(archive_path, latest_path)
    app._delete_selected_comparison()

    assert app.confirm_count == 1
    assert len(app._strategy_compare_results) == 1
    assert app._last_strategy_bt_result is app._strategy_compare_results[0]["result"]
    assert archive_path.exists()
    assert latest_path.exists()


def test_strategy_result_tab_change_refreshes_selected_panel():
    from convertible_bond.gui.controllers.strategy_backtest import StrategyBacktestMixin

    class Tabs:
        def __init__(self, selected):
            self.selected = selected

        def get(self):
            return self.selected

    class DummyApp(StrategyBacktestMixin):
        def __init__(self):
            self._last_strategy_bt_result = {"summary": {}}
            self.strategy_result_tabs = Tabs("总览")
            self.calls = []

        def after_idle(self, callback):
            callback()

        def update_idletasks(self):
            self.calls.append("idle")

        def _render_strategy_insight(self, result):
            self.calls.append("insight")

        def _render_strategy_chart(self, result):
            self.calls.append("chart")

        def _render_strategy_selection_panel(self, result):
            self.calls.append("selection")

        def _render_strategy_table(self, result):
            self.calls.append("table")

        def _render_strategy_attribution(self, result):
            self.calls.append("attribution")

        def _render_strategy_risk_panel(self, result):
            self.calls.append("risk")

        def _render_strategy_robustness_panel(self, result):
            self.calls.append("robustness")

        def _render_strategy_data_panel(self, result):
            self.calls.append("data")

        def _render_strategy_comparison(self):
            self.calls.append("comparison")

    app = DummyApp()
    for selected, expected in (
        ("概览", ["insight", "chart", "idle"]),
        ("持仓", ["selection", "table", "attribution", "idle"]),
        ("诊断", ["risk", "data", "idle"]),
        ("总览", ["insight", "chart", "idle"]),
        ("明细", ["selection", "table", "idle"]),
        ("归因", ["attribution", "idle"]),
        ("风险", ["risk", "idle"]),
        ("稳健性", ["robustness", "idle"]),
        ("数据", ["data", "idle"]),
        ("对比", ["comparison", "idle"]),
    ):
        app.strategy_result_tabs.selected = selected
        app.calls.clear()
        app._on_strategy_result_tab_change()

        assert app.calls == expected


def test_strategy_snapshot_load_marks_result_tabs_dirty(tmp_path):
    from convertible_bond.gui.controllers.strategy_backtest import StrategyBacktestMixin

    class Var:
        def __init__(self, value):
            self.value = value

        def get(self):
            return self.value

    class DummyApp(StrategyBacktestMixin):
        def __init__(self, path, snap_dir):
            self._path = path
            self._snap_dir = snap_dir
            self.v_st_template = Var("自定义")
            self.v_st_view = Var("低估候选")
            self.v_st_freq = Var("月")
            self.v_st_top_n = Var("10")
            self._strategy_compare_results = []

        def _strategy_snapshot_path(self):
            return self._path

        def _strategy_snapshots_dir(self):
            return self._snap_dir

    path = tmp_path / "strategy_backtest_snapshot.json"
    path.write_text(
        json.dumps({
            "schema_version": 1,
            "saved_at": "2026-05-29T09:00:00",
            "result": {
                "start_date": "2025-01-01",
                "end_date": "2025-02-01",
                "summary": {"final_equity": 1.0},
                "config": {"selection_view": "低估候选", "top_n": 10},
                "periods": [],
            },
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    app = DummyApp(path, tmp_path / "nonexistent_snapshots")
    app._load_strategy_backtest_snapshot(silent=True, render=False)

    assert app._last_strategy_bt_result["summary"]["final_equity"] == 1.0
    assert "概览" in app._strategy_dirty_tabs
    assert "诊断" in app._strategy_dirty_tabs
    assert len(app._strategy_compare_results) == 1


def test_strategy_local_full_market_filters_non_standard_codes():
    from convertible_bond.gui.controllers.strategy_backtest import StrategyBacktestMixin

    class Var:
        def get(self):
            return "本地全市场"

    class Cache:
        def list_bonds(self):
            return [
                "113001.SH",
                "128009.SZ",
                "Q18082207.IME",
                "404001.NQ",
                "KZZ836523001.XEE",
            ]

    class DummyApp(StrategyBacktestMixin):
        def __init__(self):
            self.v_st_pool_mode = Var()
            self.terms_cache = Cache()

    codes, label = DummyApp()._strategy_codes_from_pool()

    assert codes == ["113001.SH", "128009.SZ"]
    assert "已排除非沪深代码 3 个" in label


def test_strategy_result_tab_failure_keeps_dirty_for_retry():
    from convertible_bond.gui.controllers.strategy_backtest import StrategyBacktestMixin

    class Tabs:
        def __init__(self, selected):
            self.selected = selected

        def get(self):
            return self.selected

    class DummyApp(StrategyBacktestMixin):
        def __init__(self):
            self._last_strategy_bt_result = {"summary": {}}
            self.strategy_result_tabs = Tabs("总览")
            self._strategy_dirty_tabs = {"总览"}
            self.fail = True
            self.calls = []

        def update_idletasks(self):
            self.calls.append("idle")

        def _render_strategy_insight(self, result):
            self.calls.append("insight")
            if self.fail:
                raise RuntimeError("boom")

        def _render_strategy_chart(self, result):
            self.calls.append("chart")

    app = DummyApp()
    app._on_strategy_result_tab_change()

    assert app.calls == ["insight"]
    assert "总览" in app._strategy_dirty_tabs

    app.fail = False
    app.calls.clear()
    app._on_strategy_result_tab_change()

    assert app.calls == ["insight", "chart", "idle"]
    assert "总览" not in app._strategy_dirty_tabs


def test_strategy_detail_period_filter_defaults_to_latest_and_aggregates_all():
    from convertible_bond.gui.controllers.strategy_backtest import StrategyBacktestMixin

    class Var:
        def __init__(self, value):
            self.value = value

        def get(self):
            return self.value

        def set(self, value):
            self.value = value

    class DummyApp(StrategyBacktestMixin):
        def __init__(self):
            self.v_st_detail_period = Var("最近一期")

    periods = [
        {
            "start_date": date(2025, 1, 1),
            "end_date": date(2025, 2, 1),
            "eligible_count": 10,
            "priced_count": 8,
            "candidate_count": 3,
            "selected_count": 2,
        },
        {
            "start_date": date(2025, 2, 1),
            "end_date": date(2025, 3, 1),
            "eligible_count": 12,
            "priced_count": 11,
            "candidate_count": 4,
            "selected_count": 3,
        },
    ]

    app = DummyApp()
    assert app._strategy_detail_periods(periods) == [periods[-1]]

    app.v_st_detail_period.set("全部")
    assert app._strategy_detail_periods(periods) == periods
    assert app._strategy_funnel_text(periods, "全部") == (
        "全部 2 期: 合格 22 → 定价 19 → 候选 7 → 买入 5"
    )


def _latest(history, on_date):
    for d, v in reversed(history):
        if d <= on_date:
            return v
    raise RuntimeError("no close")


def test_write_strategy_backtest_csv_emits_all_sections(tmp_path, monkeypatch):
    """端到端冒烟: 回测结果导出 CSV 应包含各区块标题且可被 csv 解析。

    该函数原先无测试覆盖; 拆成 _write_csv_* 区块辅助后用此守护输出结构 (区块齐全、
    逐期行数 = 区间数、可解析)。
    """
    provider = StrategyFakeProvider()

    def fake_batch_price(provider_arg, codes, *, valuation_date, **kwargs):
        rows = []
        for code in codes:
            market = _latest(provider_arg.bond_history[code], valuation_date)
            theo = market * 1.10  # 市价低于理论价 → 低估, 可进候选
            rows.append({
                "bond_code": code,
                "bond_name": provider_arg.terms[code].sec_name,
                "stock_code": provider_arg.terms[code].underlying_code,
                "status": "ok",
                "S0": market, "K": 100.0, "sigma": 0.30,
                "theoretical_price": theo, "market_price": market,
                "deviation": (market - theo) / theo,
                "credit_rating": "AA+", "outstanding_balance": 10.0, "T": 3.0,
            })
        return rows

    monkeypatch.setattr(
        "convertible_bond.strategy_backtest.batch_price_from_provider_threaded",
        fake_batch_price,
    )

    result = backtest_score_strategy(
        provider,
        ["113001.SH", "113002.SH", "113003.SH"],
        start_date=date(2025, 1, 2),
        end_date=date(2025, 3, 31),
        config=ScoreStrategyConfig(top_n=2, rebalance_freq="M", compute_benchmark=True),
    )

    out = tmp_path / "bt.csv"
    write_strategy_backtest_csv(out, result)
    text = out.read_text(encoding="utf-8-sig")
    for marker in (
        "# config", "start_date", "# equity_curve", "# positions",
        "# candidate_rows", "# summary", "# diagnostics",
    ):
        assert marker in text, f"缺少区块: {marker}"

    import csv as _csv
    parsed = list(_csv.reader(out.open(encoding="utf-8-sig")))
    # 逐期摘要表头行后紧跟每个区间一行
    header_idx = next(i for i, r in enumerate(parsed) if r[:1] == ["start_date"])
    period_rows = []
    for r in parsed[header_idx + 1:]:
        if not r or (r[0].startswith("#")):
            break
        period_rows.append(r)
    assert len(period_rows) == len(result["periods"]) == 3
    header = parsed[header_idx]
    assert "cash_yield_return" in header
    assert "average_cash_weight" in header
    assert "rank_signal" in header
    assert all(len(row) == len(header) for row in period_rows)
    for marker in ("# positions", "# candidate_rows", "# rejection_rows"):
        marker_rows = [i for i, row in enumerate(parsed) if row[:1] == [marker]]
        if not marker_rows:
            continue
        section_idx = marker_rows[0]
        section_header = parsed[section_idx + 1]
        section_data = []
        for row in parsed[section_idx + 2:]:
            if not row or row[0].startswith("#"):
                break
            section_data.append(row)
        assert section_data
        assert all(len(row) == len(section_header) for row in section_data), marker


def test_score_strategy_flushes_provider_disk_cache_each_period(monkeypatch):
    """逐期 flush: provider 链上有 flush 能力 (DiskCacheProvider) 时每个调仓期落盘一次。

    同时守护委托链: 回测内部把 provider 包成 _BacktestCacheProvider, flush 须经
    __getattr__ 透传到内层; 多小时高保真拉取中途进程被杀也只丢当期数据。
    """
    class FlushingProvider(StrategyFakeProvider):
        def __init__(self):
            super().__init__()
            self.flush_calls = 0

        def flush(self):
            self.flush_calls += 1

    provider = FlushingProvider()

    def fake_batch_price(provider_arg, codes, *, valuation_date, **kwargs):
        return [
            {
                "bond_code": code,
                "status": "ok",
                "S0": 100.0, "K": 100.0, "sigma": 0.30,
                "theoretical_price": 110.0, "market_price": 100.0,
                "deviation": -10.0 / 110.0,
                "credit_rating": "AA+", "outstanding_balance": 10.0, "T": 3.0,
            }
            for code in codes
        ]

    monkeypatch.setattr(
        "convertible_bond.strategy_backtest.batch_price_from_provider_threaded",
        fake_batch_price,
    )

    backtest_score_strategy(
        provider,
        ["113001.SH", "113002.SH", "113003.SH"],
        start_date=date(2025, 1, 2),
        end_date=date(2025, 3, 31),
        config=ScoreStrategyConfig(top_n=1, rebalance_freq="M"),
    )

    # 2025-01-02 → 2025-03-31 月频共 3 个调仓期, 每期 flush 一次
    assert provider.flush_calls == 3


def test_strategy_backtest_mixin_composition_integrity():
    """拆分守护: 子 mixin 间无方法名冲突, 聚合类暴露 UI 引用的全部入口。

    StrategyBacktestMixin 已按职责拆为 6 个子 mixin (setup/run/snapshots/
    render/render_analysis/compare)。GUI 无法在测试环境启动, 此组成性检查是
    "搬丢方法/命名冲突"这类拆分事故的静态防线; UI 入口清单来自 app.py 与
    tabs/ 对 app._xxx 的实际引用。
    """
    from convertible_bond.gui.controllers.strategy_backtest import StrategyBacktestMixin

    bases = StrategyBacktestMixin.__bases__
    assert len(bases) >= 6, "聚合类应由职责子 mixin 组成"
    owner: dict[str, str] = {}
    for base in bases:
        for name in vars(base):
            if name.startswith("__"):
                continue
            assert name not in owner, (
                f"mixin 方法名冲突: {name} 同时定义于 {owner[name]} 与 {base.__name__}")
            owner[name] = base.__name__

    ui_entry_points = (
        "_apply_strategy_template", "_describe_strategy_view",
        "_clear_strategy_codes", "_import_strategy_codes_file",
        "_refresh_strategy_setup_summary", "_precheck_strategy_backtest",
        "_run_strategy_backtest", "_cancel_strategy_backtest",
        "_save_strategy_backtest_snapshot", "_load_strategy_backtest_snapshot",
        "_export_strategy_backtest_csv", "_on_strategy_result_tab_change",
        "_render_strategy_backtest_result", "_render_current_strategy_tab",
        "_update_strategy_result_summary", "_clear_strategy_comparison",
        "_delete_selected_comparison",
    )
    for entry in ui_entry_points:
        assert callable(getattr(StrategyBacktestMixin, entry, None)), (
            f"聚合类缺少 UI 入口: {entry}")


def test_strategy_logic_summary_text_reflects_pde_signal_and_cash_policy():
    """新 GUI 固定 PDE Top N + 缺口留现金，摘要不再出现旧视图/全池逻辑。"""
    from convertible_bond.gui.controllers.strategy_backtest import StrategyBacktestMixin

    class Var:
        def __init__(self, value):
            self._v = value

        def get(self):
            return self._v

    app = StrategyBacktestMixin()
    app.v_st_top_n = Var("10")
    app.v_st_cash_yield = Var("2.2")
    app.v_st_rank_signal = Var("估值偏差")

    text = app._strategy_logic_summary_text()
    assert "估值偏差 < 0" in text
    assert "Top 10 等权" in text
    assert "缺口留现金（2.2%/年）" in text
    assert "机会分" not in text and "等权全池" not in text

    # 输入半截非法 TopN 时摘要不崩溃 (实时 trace 下常见)
    app.v_st_top_n = Var("")
    assert "Top N 等权" in app._strategy_logic_summary_text()

    # 旧标签一律落到「估值偏差」—— 下修优势信号已删, 摘要不该再说得出那几个词
    app.v_st_top_n = Var("10")
    for legacy in ("下修优势", "稳健下修优势", ""):
        app.v_st_rank_signal = Var(legacy)
        summary = app._strategy_logic_summary_text()
        assert "估值偏差 < 0" in summary
        assert "下修优势" not in summary


def test_strategy_signal_text_distinguishes_pde_and_legacy_snapshots():
    from convertible_bond.gui.controllers.strategy_backtest import StrategyBacktestMixin

    app = StrategyBacktestMixin()
    # 下修两档已删, 但**渲染分支保留** —— 旧快照的 rank_value 还在里面, 去掉会让
    # 它们掉进末尾的「旧分」分支, 把元读成分。
    assert app._strategy_signal_text({
        "rank_signal": "down_reset_robust_edge", "rank_value": 1.25,
    }) == "稳健 +1.25元"
    assert app._strategy_signal_text({
        "rank_signal": "deviation", "rank_value": -0.08,
    }) == "偏差 -8.00%"
    assert app._strategy_signal_text({"score": 12.3}) == "旧分 12.3"


# ── rank_signal (B 层排序信号) 与入口 fail-fast ──────────────────────────


def _rank_signal_batch_price(provider_arg, codes, *, valuation_date, **kwargs):
    """构造排序信号分歧场景: 113001 最低估(score/deviation 最优), 113003 价格最低(双低最优)。

    三只券溢价均为 0, 双低值 = 市价; 113001 偏差 -9.1%, 其余 -2%。
    """
    theo_bonus = {"113001.SH": 0.10, "113002.SH": 0.02, "113003.SH": 0.02}
    rows = []
    for code in codes:
        market = _latest(provider_arg.bond_history[code], valuation_date)
        theo = market * (1.0 + theo_bonus[code])
        rows.append({
            "bond_code": code,
            "bond_name": provider_arg.terms[code].sec_name,
            "stock_code": provider_arg.terms[code].underlying_code,
            "status": "ok",
            "S0": market, "K": 100.0, "sigma": 0.30,
            "theoretical_price": theo, "market_price": market,
            "deviation": (market - theo) / theo,
            "conversion_premium": 0.0,
            "effective_p_down_1y_prob": 0.20,
            "credit_rating": "AA+", "outstanding_balance": 10.0, "T": 3.0,
        })
    return rows


@pytest.mark.parametrize("rank_signal, expected_first", [
    ("deviation", "113001.SH"),    # 偏差升序: 同 113001 (-9.1% 最小)
    ("double_low", "113003.SH"),   # 双低升序: 价格最低的 113003 (90+0)
])
def test_rank_signal_reorders_candidates(monkeypatch, rank_signal, expected_first):
    provider = StrategyFakeProvider()
    monkeypatch.setattr(
        "convertible_bond.strategy_backtest.batch_price_from_provider_threaded",
        _rank_signal_batch_price,
    )

    result = backtest_score_strategy(
        provider,
        ["113001.SH", "113002.SH", "113003.SH"],
        start_date=date(2025, 1, 2),
        end_date=date(2025, 1, 31),
        config=ScoreStrategyConfig(top_n=1, rank_signal=rank_signal),
    )

    period = result["periods"][0]
    assert period["selected_codes"] == [expected_first]
    assert period["rank_signal"] == rank_signal
    assert result["config"]["rank_signal"] == rank_signal
    first_candidate = period["candidate_rows"][0]
    assert first_candidate["bond_code"] == expected_first
    assert first_candidate["rank_signal"] == rank_signal
    if rank_signal == "double_low":
        assert first_candidate["rank_value"] == pytest.approx(90.0)


def test_pde_backtest_snapshot_round_trip_preserves_strategy_settings(monkeypatch, tmp_path):
    """PDE 主入口跑出的结果可自动保存，并在新 GUI 实例中原样恢复。"""
    from convertible_bond.gui.controllers.strategy_backtest import StrategyBacktestMixin

    provider = StrategyFakeProvider()
    for history in provider.bond_history.values():
        history.insert(1, (date(2025, 1, 3), history[0][1]))
    monkeypatch.setattr(
        "convertible_bond.strategy_backtest.batch_price_from_provider_threaded",
        _rank_signal_batch_price,
    )
    result = backtest_pde_strategy(
        provider,
        ["113001.SH", "113002.SH", "113003.SH"],
        start_date=date(2025, 1, 2),
        end_date=date(2025, 1, 31),
        config=PDEStrategyConfig(
            top_n=2,
            min_confidence=None,
            exclude_risk_tags=(),
            compute_benchmark=False,
        ),
    )
    result["config"]["history_mode"] = "Wind高保真"
    result["run_settings"] = {
        "data_source": "Wind",
        "requested_data_source": "akshare",
        "history_mode": "Wind高保真",
        "strategy": {
            "template": "估值偏差",
            **result["config"],
        },
        "pricing": {
            "r": 0.022,
            "base_spread": 0.03,
            "distress_k": 0.05,
            "p_down": 0.25,
        },
        "admission_filter": {
            "min_outstanding_balance": 0.5,
            "min_credit_rating": "A+",
        },
    }

    class SnapshotApp(StrategyBacktestMixin):
        def __init__(self, active_result=None):
            self._last_strategy_bt_result = active_result
            self._strategy_compare_results = []

        def _strategy_snapshots_dir(self):
            return tmp_path / "strategy_backtest_snapshots"

        def _strategy_snapshot_path(self):
            return tmp_path / "strategy_backtest_snapshot.json"

    writer = SnapshotApp(result)
    saved = writer._save_strategy_backtest_snapshot()
    payload = json.loads(saved["path"].read_text(encoding="utf-8"))
    assert payload["schema_version"] == 3
    assert payload["meta"]["config"]["strategy_type"] == "pde_valuation"
    assert payload["meta"]["config"]["rank_signal"] == "deviation"
    assert payload["meta"]["run_settings"]["pricing"]["p_down"] == pytest.approx(0.25)

    reader = SnapshotApp()
    reader._load_strategy_backtest_snapshot(silent=True, render=False)
    loaded = reader._last_strategy_bt_result
    assert loaded["config"]["strategy_type"] == "pde_valuation"
    assert loaded["config"]["history_mode"] == "Wind高保真"
    assert loaded["run_settings"]["strategy"]["template"] == "估值偏差"
    # 按 deviation 升序: 113001 (−9.1%) 最低估, 113002/113003 并列 −2% 由代码序断开
    assert loaded["periods"][0]["selected_codes"] == ["113001.SH", "113002.SH"]
    assert loaded["periods"][0]["candidate_rows"][0]["rank_signal"] == "deviation"



def test_down_reset_strategy_exits_after_resolution_event(monkeypatch, tmp_path):
    provider = StrategyFakeProvider()
    provider.bond_history["113001.SH"] = [
        (date(2025, 1, 2), 100.0),
        (date(2025, 1, 15), 105.0),
        (date(2025, 1, 16), 106.0),
        (date(2025, 1, 20), 108.0),
        (date(2025, 1, 31), 110.0),
    ]
    provider.event_store = CBEventStore(tmp_path / "events.json")
    provider.event_store.add_many([
        CBEvent(
            bond_code="113001.SH",
            event_date=date(2025, 1, 15),
            event_type="down_reset_approved",
            raw_title="董事会通过向下修正转股价格议案",
        )
    ])
    monkeypatch.setattr(
        "convertible_bond.strategy_backtest.batch_price_from_provider_threaded",
        _rank_signal_batch_price,
    )

    result = backtest_score_strategy(
        provider,
        ["113001.SH"],
        start_date=date(2025, 1, 2),
        end_date=date(2025, 1, 31),
        config=ScoreStrategyConfig(
            top_n=1,
            # 事件退出此前由"排序信号是下修优势"隐式激活; 那个信号删掉之后,
            # 它变成**显式开关** (默认关闭), 所以这里必须自己打开。
            rank_signal="deviation",
            down_reset_event_exit=True,
            cash_yield_rate=0.365,
            min_confidence=None,
            exclude_risk_tags=(),
            compute_benchmark=False,
        ),
    )

    period = result["periods"][0]
    position = period["positions"][0]
    assert position["exit_date"] == date(2025, 1, 16)
    assert position["exit_reason"] == "down_reset_event"
    assert position["exit_event_type"] == "down_reset_approved"
    post_exit_cash = 1.06 * 0.365 * 15 / 365
    assert position["price_return"] == pytest.approx(0.06)
    assert position["post_exit_cash_return"] == pytest.approx(post_exit_cash)
    assert period["period_return"] == pytest.approx(0.06 + post_exit_cash)
    assert result["summary"]["final_equity"] == pytest.approx(1.06 + post_exit_cash)
    jan20_equity = next(
        row["equity"] for row in result["equity_curve"]
        if row["date"] == date(2025, 1, 20)
    )
    assert jan20_equity == pytest.approx(1.06 * (1.0 + 0.365 * 4 / 365))
    assert period["event_exit_count"] == 1
    assert period["event_exit_turnover"] == pytest.approx(1.0)
    assert period["average_cash_weight"] == pytest.approx(15 / 29)
    assert period["end_cash_weight"] == pytest.approx(1.0)
    assert result["summary"]["total_event_exits"] == 1
    assert result["summary"]["avg_cash_weight"] == pytest.approx(15 / 29)

    hold_to_rebalance = backtest_score_strategy(
        provider,
        ["113001.SH"],
        start_date=date(2025, 1, 2),
        end_date=date(2025, 1, 31),
        config=ScoreStrategyConfig(
            top_n=1,
            rank_signal="deviation",
            down_reset_event_exit=False,
            min_confidence=None,
            exclude_risk_tags=(),
            compute_benchmark=False,
            mark_to_market=False,
        ),
    )
    assert hold_to_rebalance["periods"][0]["period_return"] == pytest.approx(0.10)
    assert hold_to_rebalance["periods"][0]["event_exit_count"] == 0


def test_config_summary_records_benchmark_index_code(monkeypatch):
    """benchmark_index_code 应回显进 result['config'] (复现口径); 指数取不到时优雅缺省。"""
    provider = StrategyFakeProvider()
    monkeypatch.setattr(
        "convertible_bond.strategy_backtest.batch_price_from_provider_threaded",
        _rank_signal_batch_price,
    )

    result = backtest_score_strategy(
        provider,
        ["113001.SH", "113002.SH"],
        start_date=date(2025, 1, 2),
        end_date=date(2025, 1, 31),
        config=ScoreStrategyConfig(top_n=1, benchmark_index_code="000832.CSI"),
    )

    assert result["config"]["benchmark_index_code"] == "000832.CSI"
    # fake provider 无该指数历史 → 第二基准曲线优雅缺省
    assert result["index_benchmark_curve"] == []
    assert result["summary"]["index_benchmark_total_return"] is None


@pytest.mark.parametrize("bad_config", [
    {"rank_signal": "momentum"},
    {"holding_mode": "cap_weight"},
    {"funding_mode": "margin"},
    {"exposure_mode": "kelly"},
    {"execution_timing": "vwap"},
])
def test_backtest_rejects_invalid_enums_before_any_pricing(monkeypatch, bad_config):
    """非法枚举必须在任何取数/定价发生前抛 ValueError (防白烧 Wind 配额)。"""
    provider = StrategyFakeProvider()
    calls = []

    def counting_batch_price(provider_arg, codes, *, valuation_date, **kwargs):
        calls.append(valuation_date)
        return []

    monkeypatch.setattr(
        "convertible_bond.strategy_backtest.batch_price_from_provider_threaded",
        counting_batch_price,
    )

    with pytest.raises(ValueError):
        backtest_score_strategy(
            provider,
            ["113001.SH"],
            start_date=date(2025, 1, 2),
            end_date=date(2025, 1, 31),
            config=ScoreStrategyConfig(**bad_config),
        )
    assert calls == []


def test_eligible_codes_excludes_bond_issued_but_not_yet_listed():
    """起息日已过、上市日未到的新债不能进回测池.

    issue_date 是起息日 (比上市日早 2~4 周), 只看它会让还没挂牌、
    根本买不到的新债提前进池子。
    """
    from convertible_bond.strategy_backtest import _eligible_codes_for_date

    class _Provider(DataProvider):
        name = "stub"

        def __init__(self, terms):
            self._terms = terms

        def get_bond_terms(self, bond_code, valuation_date):
            return self._terms[bond_code]

        def get_stock_close(self, stock_code, valuation_date):
            return 10.0

        def hist_vol(self, stock_code, valuation_date, window_days=126):
            return 0.3

        def get_risk_free_rate(self, valuation_date):
            return 2.0

        def get_stock_history(self, stock_code, start_date, end_date):
            return []

        def get_bond_history(self, bond_code, start_date, end_date):
            return []

    base = dict(
        underlying_code="300953.SZ",
        conversion_price=100.0,
        maturity_date=date(2032, 8, 17),
        face_value=100.0,
        credit_rating="AA-",
        is_tradable=True,
        trading_status="tradable",
    )
    terms = {
        # 起息 8/17 已过, 上市 9/10 未到 → 应剔除
        "123282.SZ": BondTerms(issue_date=date(2026, 8, 17),
                               listing_date=date(2026, 9, 10), **base),
        # 已上市 → 保留
        "123283.SZ": BondTerms(issue_date=date(2026, 7, 24),
                               listing_date=date(2026, 8, 13), **base),
        # 缺上市日 + 起息不久 → 已发行未上市 (Wind ipo_date 挂牌后才有值), 剔除
        "123284.SZ": BondTerms(issue_date=date(2026, 7, 24), listing_date=None, **base),
        # 缺上市日 + 起息已久 (超过 UNLISTED_MAX_DAYS) → 视为数据缺口而非未挂牌, 保留
        "123285.SZ": BondTerms(issue_date=date(2024, 1, 10), listing_date=None, **base),
        # ↓ 两条**边界**行: 上面四只的日期全是离边界好几天的, 于是两个比较符
        #   (`listed_dt > on_date` / `maturity_dt <= on_date`) 各自都能被松紧一格
        #   而测试照常绿。上市当天买得到, 到期当天买不到 —— 各给一个等号用例。
        "123286.SZ": BondTerms(issue_date=date(2026, 7, 30),
                               listing_date=date(2026, 8, 20), **base),
        "123287.SZ": BondTerms(issue_date=date(2020, 8, 20),
                               listing_date=date(2020, 9, 10),
                               **{**base, "maturity_date": date(2026, 8, 20)}),
    }
    provider = _Provider(terms)

    eligible, excluded, _ = _eligible_codes_for_date(
        provider, list(terms), date(2026, 8, 20))

    assert "123282.SZ" not in eligible
    assert ("123282.SZ", "尚未上市 (上市日 2026-09-10)") in excluded
    assert "123283.SZ" in eligible
    assert "123284.SZ" not in eligible
    assert ("123284.SZ", "已发行未上市") in excluded
    assert "123285.SZ" in eligible
    # 上市日 == 估值日: 当天就能买, 必须进池
    assert "123286.SZ" in eligible
    # 到期日 == 估值日: 当天已经不是可交易标的
    assert "123287.SZ" not in eligible
    assert ("123287.SZ", "已到期 (到期日 2026-08-20)") in excluded


def test_threshold_filter_reproduces_the_legacy_tag_filter_row_for_row():
    """标签→阈值的替换必须是**逐只等价**的, 不是"大致等价".

    标签是给全池标的做标注的展示层产物, 判据粗 —— 余额那一族四个标签
    (余额清零/触及摘牌线/临近摘牌线/小余额) 说的是同一个连续量的四个刻度。策略层改用
    显式阈值之后每一条都能单独调, 但**默认值逐条等于被取代的标签判据**, 所以默认配置
    下候选池必须一只不差。实测全池 284 行 → 候选 116 只, 两条路完全相同。

    这条用例是这次重构的安全网: 阈值默认值被改动、或缺值语义写反 (标签路径缺 σ/评级/
    余额/偏差是**放行**的), 都会让它红。

    **必须断言在真正选债的那条路 ``_select_candidate_rows`` 上。** 第一版只比了
    ``_candidate_filter_reason`` —— 而那个函数当时只被落选解释 CSV 消费, 真正选债的
    ``_select_candidate_rows`` 是它的第二份副本、一个字节没改。于是"逐只等价 116"
    在一个没人走的路径上成立, 而生产路径的候选池悄悄从 116 涨到 263, 用例全绿。
    安全网架在被改的路径之外, 等于没有。
    """
    import json

    from convertible_bond import batch_pricing as bp
    from convertible_bond.paths import data_path
    from convertible_bond.strategy_backtest import (
        PDEStrategyConfig, ScoreStrategyConfig,
        _candidate_filter_reason, _select_candidate_rows,
    )

    cache = data_path("batch_pricing_cache.json")
    if not cache.exists():                      # 运行态缓存, gitignored
        pytest.skip("需要 data/batch_pricing_cache.json")
    rows = bp.annotate_batch_results(
        json.load(open(cache, encoding="utf-8"))["results"])
    if len(rows) < 50:
        pytest.skip("缓存样本太小, 等价性结论没有意义")

    legacy = set(bp.LEGACY_STRATEGY_EXCLUDE_TAGS)

    def passes_legacy(row):
        if row.get("status") != "ok":
            return False
        if bp.view_exclusion_reason(row, "综合机会") is not None:
            return False
        if bp.finite_float(row.get("deviation")) is None:
            return False
        if row.get("confidence") not in ("高", "中"):
            return False
        if set(row.get("risk_tags") or ()) & legacy:
            return False
        mkt = bp.finite_float(row.get("market_price"))
        return mkt is not None and mkt > 0

    by_tag = {r["bond_code"] for r in rows if passes_legacy(r)}

    # ① 库默认配置, 走**真正选债**那条路
    by_threshold = {r["bond_code"]
                    for r in _select_candidate_rows(rows, ScoreStrategyConfig())}
    assert by_threshold == by_tag, (
        f"只在标签口径里: {sorted(by_tag - by_threshold)[:5]}; "
        f"只在阈值口径里: {sorted(by_threshold - by_tag)[:5]}")

    # ② CLI/GUI 实际构造的 config 也要等价 —— 它们对 σ 留空时**沿用默认上限**,
    #    传 None 会把风险闸关掉 (实测那样候选池 116 → 126)。
    cli_like = PDEStrategyConfig(min_confidence=("高", "中"), min_sigma=None,
                                 max_sigma=PDEStrategyConfig.max_sigma)
    assert {r["bond_code"] for r in _select_candidate_rows(rows, cli_like)} == by_tag

    # ③ 选债路径与落选解释路径按定义一致: 解释里说的理由就是这里拦下它的理由
    explained = {r["bond_code"] for r in rows
                 if _candidate_filter_reason(r, ScoreStrategyConfig()) is None}
    assert by_threshold <= explained


def test_thresholds_let_missing_values_through():
    """缺值放行 —— 与被取代的标签同口径。

    缺 σ/评级/余额/偏差时标签路径打的是「无HV」「无评级」「无余额」「无偏差」, 四个都不在
    排除集里, 也就是说旧口径下这些行是**放行**的。阈值化若把缺值当不满足, 一次数据源
    抖动就会静默把几十只债踢出候选 —— 那是行为变更不是重构。
    """
    from convertible_bond.strategy_backtest import ScoreStrategyConfig, _candidate_filter_reason

    row = {
        "bond_code": "127093.SZ", "status": "ok", "confidence": "高",
        "market_price": 105.0, "theoretical_price": 120.0, "parity": 90.0,
        "deviation": -0.125,
        # σ / 评级 / 余额 / 剩余年限 / 相对偏差 / 模型溢价 全缺
    }

    assert _candidate_filter_reason(row, ScoreStrategyConfig()) is None

    # 而真正"这行不能用"的三档仍然无条件拦下
    for field, expected in (("market_price", "缺少有效市价"),
                            ("theoretical_price", "理论价异常"),
                            ("parity", "缺少转股价值")):
        broken = dict(row)
        broken[field] = None
        assert _candidate_filter_reason(broken, ScoreStrategyConfig()) == expected


def test_cli_risk_thresholds_do_not_silently_disable_the_caps():
    """CLI 构造 config 的那一步单独可测 —— 它此前既会静默关闸, 又会直接崩。

    两个真实缺陷:

    ① ``max_sigma`` 留空时传 ``None``, 把取代「高HV」的风险上限整个关掉 (实测候选池
       116 → 126)。重构前留空时那道闸由标签照常生效, 所以"留空 = 沿用默认"才是保行为的读法。
    ② ``--include-review-risks`` 的实现用 ``**dict(max_sigma=None, ...)`` 展开, 而调用点
       下面还有一个显式的 ``max_sigma=`` —— 同一个调用里**重复关键字**, 一开这个开关就
       ``TypeError: got multiple values for keyword argument``。抽成函数正是为了这个。
    """
    import argparse

    from convertible_bond.cli.strategy_backtest import _risk_threshold_kwargs
    from convertible_bond.strategy_backtest import PDEStrategyConfig

    plain = _risk_threshold_kwargs(
        argparse.Namespace(include_review_risks=False, max_sigma=None))
    cfg = PDEStrategyConfig(**plain)                 # 重复关键字会在这里炸
    assert cfg.max_sigma == PDEStrategyConfig.max_sigma == 0.80, "留空把 σ 上限关掉了"
    assert cfg.min_credit_rating == "AA-"
    assert cfg.exclude_underlying_st is True

    explicit = _risk_threshold_kwargs(
        argparse.Namespace(include_review_risks=False, max_sigma=65.0))
    assert PDEStrategyConfig(**explicit).max_sigma == pytest.approx(0.65)

    relaxed = _risk_threshold_kwargs(
        argparse.Namespace(include_review_risks=True, max_sigma=None))
    loose = PDEStrategyConfig(**relaxed)             # 同样不许重复关键字
    assert loose.max_sigma is None and loose.min_credit_rating is None
    assert loose.min_outstanding_balance is None and loose.min_years_to_maturity is None
    assert loose.exclude_underlying_st is False
    assert loose.exclude_underlying_limit_down is False


def test_relative_deviation_cap_is_closed_at_the_tag_boundary():
    """「模型高估离群」判据是 ``gap >= 0.20`` —— 六条阈值里唯一的闭区间。

    其余五条 (模型溢价 >0.45 / σ >0.80 / 余额 <1.0 / 剩余年限 <0.5 / 评级) 都是开区间,
    统一写成 ``>`` 会让恰好等于 0.20 的行两条路结论相反。边界不是零概率: 小批量标注时
    ``median_deviation_of`` 样本不足会让锚回落 0.0, 此时 gap 恒等于 deviation 本身。
    """
    from convertible_bond.strategy_backtest import ScoreStrategyConfig, _candidate_filter_reason

    def row(rel):
        return {"bond_code": "x", "status": "ok", "confidence": "高",
                "market_price": 110.0, "theoretical_price": 100.0, "parity": 95.0,
                "deviation": 0.1, "relative_deviation": rel}

    cfg = ScoreStrategyConfig()
    assert _candidate_filter_reason(row(0.20), cfg) is not None, "恰好 0.20 应当被拦"
    assert _candidate_filter_reason(row(0.1999), cfg) is None
    assert _candidate_filter_reason(row(0.21), cfg) is not None


def test_position_bought_then_halted_is_marked_out_not_deleted(monkeypatch):
    """建了仓再停牌的持仓, 必须按最后可得价平出, 不能整条删掉。

    此前 entry/exit 缺任何一个都走同一条 ``continue``, 而两者的经济含义正好相反:
    没有期初价 = 根本没成交 (那个槽位确实是现金); **有期初价、没有期末价 = 买到了,
    然后停牌 / 摘牌 / 强赎摘牌 / 到了最后交易日**。删掉它等于用建仓之后才知道的信息
    决定"这笔成交算不算发生过", 并把已实现盈亏整个抹平 —— 暴雷的亏损被向上删掉,
    强赎的收益被向下删掉。

    这条用例把两个跑法的**唯一**差别设成"期末还有没有报价", 暴跌本身完全相同。
    """
    from datetime import date

    def _mk(halted: bool):
        provider = StrategyFakeProvider()
        # C: 01-02 建仓 90, 次日暴跌到 45
        hist = [(date(2025, 1, 2), 90.0), (date(2025, 1, 6), 45.0)]
        if not halted:
            hist.append((date(2025, 1, 31), 45.0))     # 照常成交到期末
        provider.bond_history["113003.SH"] = hist
        return provider

    monkeypatch.setattr(
        "convertible_bond.strategy_backtest.batch_price_from_provider_threaded",
        _positive_bonus_batch_price)

    out = {}
    for halted in (False, True):
        result = backtest_score_strategy(
            _mk(halted), ["113001.SH", "113002.SH", "113003.SH"],
            start_date=date(2025, 1, 2), end_date=date(2025, 1, 31),
            config=ScoreStrategyConfig(
                top_n=3, rebalance_freq="M", holding_mode="pool",
                funding_mode="full_invest", min_confidence=None,
                exclude_risk_tags=(), compute_benchmark=True),
        )
        period = result["periods"][0]
        out[halted] = period

    normal, halted = out[False], out[True]
    # 停牌那一跑必须仍然持有 C, 并且认得出它是怎么平的
    codes = {p["bond_code"] for p in halted["positions"]}
    assert "113003.SH" in codes, "暴跌后停牌的持仓被整条删掉了"
    c = next(p for p in halted["positions"] if p["bond_code"] == "113003.SH")
    assert c["exit_reason"] == "no_exit_price"
    assert c["end_price"] == pytest.approx(45.0), "没有按最后可得价平出"

    # 两跑的经济事实相同 → 区间收益必须一致 (此前实测差 17.6pp)
    assert halted["gross_return"] == pytest.approx(normal["gross_return"], abs=1e-9)
    assert halted["cash_weight"] == pytest.approx(0.0)

    # 基准成分也不许静默少人 (此前 6 → 5, excess_return 跟着错且 CSV 无痕迹)
    assert len(halted.get("benchmark_codes") or []) == len(
        normal.get("benchmark_codes") or [])
    assert halted["benchmark_return"] == pytest.approx(
        normal["benchmark_return"], abs=1e-9)


def test_never_filled_position_is_still_treated_as_cash(monkeypatch):
    """反向守护: **真的没成交**的那一档语义不许被上面那条改掉。

    没有期初价 = 根本没买到, 它的槽位就该按 ScoreStrategyConfig docstring 说的
    "按现金(0 收益)计入分母"处理 (reserve_cash) 或摊回 (full_invest)。
    """
    from datetime import date

    provider = StrategyFakeProvider()
    provider.bond_history["113003.SH"] = [(date(2024, 6, 1), 90.0)]   # 只有远古价
    monkeypatch.setattr(
        "convertible_bond.strategy_backtest.batch_price_from_provider_threaded",
        _positive_bonus_batch_price)
    result = backtest_score_strategy(
        provider, ["113001.SH", "113002.SH", "113003.SH"],
        start_date=date(2025, 1, 2), end_date=date(2025, 1, 31),
        config=ScoreStrategyConfig(
            top_n=3, rebalance_freq="M", holding_mode="pool",
            funding_mode="reserve_cash", min_confidence=None,
            exclude_risk_tags=(), compute_benchmark=False),
    )
    period = result["periods"][0]
    assert len(period["positions"]) == 2
    assert period["cash_weight"] == pytest.approx(1 / 3)
    assert any(s["bond_code"] == "113003.SH" for s in period["skipped_positions"])


def _bare_provider():
    from convertible_bond.data_providers.base import DataProvider

    class _P(DataProvider):
        name = "bare"

        def get_bond_history(self, code, a, b):
            return []

        def get_bond_terms(self, code, d):
            return None

        def get_stock_close(self, *a, **k):
            return None

        def get_stock_history(self, *a, **k):
            return []

        def hist_vol(self, *a, **k):
            return 0.2

        def get_risk_free_rate(self, *a, **k):
            return 0.022

    return _P()


def test_cash_yield_does_not_accrue_past_the_period_end():
    """现金腿必须按 ``period_end`` 封顶。

    曲线的最后一点未必是期末: ``next_close`` 口径下平仓价落在期末之后的第一个可得
    交易日 (实测越过 1~3 个日历日), 而下一期又从它自己的 ``period_start`` 重新起算 ——
    重叠那几天的现金收益被付两次。后果是 ``summary.final_equity`` 不再等于逐期收益的
    链乘, 而这两个数在同一份报告里并排给出。实测单期多计 1.62bp。
    仓位那一腿早就封顶了 (下修事件退出的现金天数用 ``min(current_date, period_end)``),
    只有现金腿漏了。
    """
    from datetime import date

    from convertible_bond.strategy_backtest import _portfolio_mark_to_market_curve

    start, end = date(2025, 1, 31), date(2025, 2, 28)
    positions = [{"bond_code": "A.SH", "entry_date": start, "exit_date": date(2025, 3, 3),
                  "start_price": 100.0, "end_price": 100.0, "exit_reason": "rebalance"}]
    curve = _portfolio_mark_to_market_curve(
        _bare_provider(), positions, start_equity=1.0, period_end=end, cost=0.0,
        intended_count=10, period_start=start, cash_weight=0.9, cash_yield_rate=0.022)

    assert curve[-1]["date"] > end, "前提不成立: 曲线末点没有越过期末"
    expected = 1.0 + 0.9 * 0.022 * (end - start).days / 365.0
    assert curve[-1]["equity"] == pytest.approx(expected, rel=1e-12), (
        f"现金计息越过了期末: {curve[-1]['equity']} vs {expected}")


def test_the_main_curve_subtracts_the_rebalance_cost():
    """**主**净值曲线的成本方向必须钉住。

    上一版只盖了两条早返回分支 (空仓 / 无价格图), 而真正跑绝大多数期的是循环里那一行
    ``start_equity * (1 + gross_return + _cash_accrual(...) - cost)``。实测把那个减号翻成
    加号, **1081 条测试全绿** —— 我自己写的守护, 犯的正是这一路反复挑出来的那个毛病:
    测了 helper 没测接线。

    这条直接比对有成本与无成本两条曲线, 差额必须**恰好是** ``start_equity * cost``,
    方向为负。
    """
    from datetime import date

    from convertible_bond.strategy_backtest import _portfolio_mark_to_market_curve

    start, end = date(2025, 1, 31), date(2025, 2, 28)
    positions = [{"bond_code": "A.SH", "entry_date": start, "exit_date": end,
                  "start_price": 100.0, "end_price": 110.0, "exit_reason": "rebalance"}]
    kw = dict(start_equity=1.0, period_end=end, intended_count=1,
              period_start=start, cash_weight=0.0, cash_yield_rate=0.0)

    free = _portfolio_mark_to_market_curve(_bare_provider(), positions, cost=0.0, **kw)
    charged = _portfolio_mark_to_market_curve(
        _bare_provider(), positions, cost=0.002, **kw)

    assert len(free) == len(charged) >= 2, "前提不成立: 曲线太短, 走的是早返回分支"
    for a, b in zip(free, charged):
        assert b["equity"] == pytest.approx(a["equity"] - 1.0 * 0.002, abs=1e-12), (
            f"{a['date']}: 成本方向或大小不对 ({a['equity']} → {b['equity']})")
    assert charged[-1]["equity"] < free[-1]["equity"]


def test_empty_period_still_charges_the_rebalance_cost():
    """``intended_count <= 0`` 那条早返回不能漏 ``- cost``。

    它的两个兄弟早返回都带着成本, 而这一档 (候选池为空 / full_invest 下零成交) 恰恰是
    **清空整个组合**的那一期, 调仓成本最实在。漏掉之后 ``period_return`` 照常报了成本
    而净值曲线没扣 —— 同一份报告里 final 与逐期链乘对不上 (实测 1.00275722 vs 1.0007517)。
    """
    from datetime import date

    from convertible_bond.strategy_backtest import _portfolio_mark_to_market_curve

    start, end = date(2025, 1, 31), date(2025, 2, 28)
    for intended in (0, 10):
        curve = _portfolio_mark_to_market_curve(
            _bare_provider(), [], start_equity=1.0, period_end=end, cost=0.002,
            intended_count=intended, period_start=start,
            cash_weight=1.0, cash_yield_rate=0.0)
        assert curve[-1]["equity"] == pytest.approx(0.998), (
            f"intended_count={intended} 时净值 {curve[-1]['equity']}, 成本没扣")


def test_relative_deviation_gate_only_applies_to_a_market_wide_anchor():
    """锚不是全市场中位时, ``max_relative_deviation`` 这条闸不适用。

    ``relative_deviation`` 的定义是"比当期全市场中位贵多少"。而
    ``median_deviation_of`` 在样本 < 30 时返回 None, ``annotate_batch_result`` 于是回落
    ``anchor = 0.0``、把 ``relative_deviation`` 写成 ``deviation`` 本身并标
    ``cross_section_origin = "absolute_fallback"``。继续套用上限, 判据就从**横截面**
    悄悄变成**绝对**偏差阈值 —— 度量换了, 名字没换。

    实测在池子 29 → 30 上有一道悬崖: 同一批"人人 deviation=+25%"的债 (没有谁相对市场
    贵), 29 只时候选 0 且理由写「相对偏差 25.00% 不低于上限 20.00%」, 30 只时全部入选、
    相对偏差 0.00%。回测里每期可定价池的大小是变的, 于是同一只债的去留取决于那一期
    恰好有多少债定价成功。

    处置与 ``_threshold_reason`` 的既有契约一致: 缺值放行。
    """
    from convertible_bond.strategy_backtest import (
        PDEStrategyConfig,
        _candidate_filter_reason,
    )

    cfg = PDEStrategyConfig()
    assert cfg.max_relative_deviation == 0.20     # 前提

    base = dict(bond_code="123001.SZ", status="ok", market_price=120.0,
                theoretical_price=96.0, deviation=0.25, confidence="高", risk_tags=[],
                sigma=0.25, parity=100.0, conversion_value=100.0, conversion_premium=0.2,
                outstanding_balance=5.0, credit_rating="AA",
                model_premium_to_parity=0.0, T=3.0, quality_score=6.0)

    real = dict(base, relative_deviation=0.25, cross_section_origin="market_median")
    assert "相对偏差" in (_candidate_filter_reason(real, cfg) or ""), "真锚下这条闸失效了"

    fake = dict(base, relative_deviation=0.25, cross_section_origin="absolute_fallback")
    assert _candidate_filter_reason(fake, cfg) is None, "假锚下仍按绝对阈值卡人"

    # 缺 origin 键的存量行按**真锚**处理 (与 _anchor_is_market_wide 的既定口径一致)
    legacy = dict(base, relative_deviation=0.25)
    assert "相对偏差" in (_candidate_filter_reason(legacy, cfg) or "")

    # 真锚且在上限内照常放行
    assert _candidate_filter_reason(
        dict(base, relative_deviation=0.15, cross_section_origin="market_median"),
        cfg) is None


def test_disk_cache_does_not_freeze_an_empty_series(tmp_path):
    """取数失败返回的空序列不能被当权威缓存写盘。

    彻底失败与"这个窗口本来就没有行情"在 provider 层长得一模一样 —— akshare 两个端点
    都抛异常时返回的也是 ``[]``, 而东财集群按出口 IP 封禁是常态。把它写下去之后每次
    复跑都零网络地喂回空序列, 那一次抖动波及的债从此**永久**掉出候选池, 而 ``_meta``
    的身份只跟踪本地条款文件的 mtime, 什么都不会让它失效。
    """
    from datetime import date

    from convertible_bond.backtest_disk_cache import DiskCacheProvider
    from convertible_bond.data_providers.base import DataProvider

    class _Inner(DataProvider):
        name = "inner"

        def __init__(self, healthy):
            self.healthy = healthy
            self.calls = 0

        def get_stock_history(self, code, a, b):
            self.calls += 1
            return [(date(2024, 6, 3), 10.0)] if self.healthy else []

        def get_bond_history(self, code, a, b):
            self.calls += 1
            return [(date(2024, 6, 3), 120.0)] if self.healthy else []

        def get_bond_terms(self, code, d):
            return None

        def get_stock_close(self, *a, **k):
            return None

        def hist_vol(self, *a, **k):
            return 0.2

        def get_risk_free_rate(self, *a, **k):
            return 0.022

    start, end = date(2024, 1, 1), date(2024, 6, 28)

    down = _Inner(healthy=False)
    first = DiskCacheProvider(down, cache_dir=tmp_path)
    assert first.get_stock_history("000001.SZ", start, end) == []
    assert first.get_bond_history("113001.SH", start, end) == []
    first.flush()

    # 网络恢复后必须真的回源, 而不是从盘上读回那个空序列
    up = _Inner(healthy=True)
    second = DiskCacheProvider(up, cache_dir=tmp_path)
    assert second.get_stock_history("000001.SZ", start, end) == [(date(2024, 6, 3), 10.0)]
    assert second.get_bond_history("113001.SH", start, end) == [(date(2024, 6, 3), 120.0)]
    assert up.calls == 2, "空序列被冻在盘上了"

    # 非空序列照常缓存 (不能把缓存整个关掉)
    third = DiskCacheProvider(_Inner(healthy=True), cache_dir=tmp_path)
    second.flush()
    assert third.get_stock_history("000001.SZ", start, end) == [(date(2024, 6, 3), 10.0)]


def test_config_summary_echoes_every_field_the_selector_reads():
    """配置快照必须回显**选债真正读的**每一个字段。

    它的职责写在 docstring 里: 「供 GUI/CSV 展示与复现」。而 2026-08-31 标签→阈值
    重构引入的七条主口径 (max_model_premium / max_relative_deviation /
    min_years_to_maturity / min_credit_rating / min_outstanding_balance /
    exclude_underlying_st / exclude_underlying_limit_down) 一条都没进去 —— 快照因此
    复现不出那次运行, 而它上面回显的恰恰是重构**之前**那批已经不当主口径的字段。

    判据不是"这七个名字在不在", 而是**行为性**的: 逐字段扰动配置, 凡是能让
    ``_candidate_filter_reason`` 改口的都必须出现在快照里。将来再加阈值, 忘了同步就会红。

    **不再扫 `cfg.X` 源码文本**: 那种写法认的是字面量, 一个别名读取
    (``_floor = getattr(cfg, "min_credit_rating")``) 就能让字段从"被消费"的名单里
    消失, 于是把它从快照里删掉也全绿 —— 实测那样改过之后这条与整套 1118 条都是绿的。

    基线取**全放行**配置: `_candidate_filter_reason` 返回**第一条**理由, 用默认配置
    做基线时 min_confidence 会把后面每一条都遮住 (实测只认得出 1 个字段)。
    """
    import dataclasses

    from convertible_bond.strategy_backtest import (
        ScoreStrategyConfig,
        _candidate_filter_reason,
        _strategy_config_summary,
    )

    row = dict(
        bond_code="128000.SZ", stock_code="000001.SZ", status="ok",
        confidence="高", risk_tags=["模型高估离群"],
        market_price=120.0, theoretical_price=114.0, parity=100.0,
        conversion_premium=0.25, deviation=0.05, relative_deviation=-0.06,
        sigma=0.35, model_premium_to_parity=0.10, T=3.0,
        credit_rating="AA", outstanding_balance=8.0,
        underlying_status="ST/退市风险", underlying_pct_change=-19.9,
    )
    permissive = {}
    for f in dataclasses.fields(ScoreStrategyConfig):
        kind = str(f.type)
        if f.name == "exclude_risk_tags":
            permissive[f.name] = ()
        elif f.name.startswith("exclude_") and kind.startswith("bool"):
            permissive[f.name] = False
        elif f.name == "min_confidence" or "| None" in kind:
            permissive[f.name] = None
    base = dataclasses.replace(ScoreStrategyConfig(), **permissive)
    assert _candidate_filter_reason(row, base) is None, (
        f"全放行基线仍被剔除 ({_candidate_filter_reason(row, base)}), "
        "后面每个字段的扰动都会被这条理由遮住, 测不到东西")

    probes = [0.0, 1.0, -1.0, 1e9, -1e9, True, False, "AAA", "C",
              (), ("高",), ("模型高估离群",), 0, 10 ** 6, None]
    consumed = set()
    for f in dataclasses.fields(base):
        for value in probes:
            if value == getattr(base, f.name):
                continue
            try:
                cfg = dataclasses.replace(base, **{f.name: value})
                if _candidate_filter_reason(row, cfg) is not None:
                    consumed.add(f.name)
                    break
            except Exception:
                continue
    assert len(consumed) >= 17, (
        f"只认出 {len(consumed)} 个字段 ({sorted(consumed)}), 探测方式可能失效了")

    summary = _strategy_config_summary(ScoreStrategyConfig())
    missing = sorted(consumed - set(summary))
    assert not missing, f"选债读了但快照没回显: {missing}"


def test_period_sharpe_is_the_non_annualized_ratio():
    """参数扫描的逐期夏普必须能算出来。

    此前 ``_period_sharpe`` 调的是 ``backtest_stats.per_period_sharpe`` —— 那个函数
    **不存在**, 每次扫描都抛 AttributeError, 而且是在**第一个完整变体回测跑完之后**
    才崩: 代价先付了再失败。
    """
    import math
    import statistics

    from convertible_bond.strategy_sweep import _period_sharpe

    rs = [0.01, -0.02, 0.03, 0.005, -0.01]
    expected = (statistics.mean(rs) - 0.001) / statistics.stdev(rs)
    assert _period_sharpe(rs, 0.001) == pytest.approx(expected, rel=1e-12)
    # 与年化版共用实现, 所以边界处置也一并继承
    assert math.isnan(_period_sharpe([0.01], 0.0))
    assert math.isnan(_period_sharpe([0.01, 0.01, 0.01], 0.0))     # 恒定收益


def test_benchmark_is_not_filtered_by_the_strategy_price_band(monkeypatch):
    """基准不许过策略的价格带。

    ``_benchmark_period_return`` 的 docstring 明写「基准刻意不过策略的筛子 —— 唯一的闸
    是 status == "ok"」, 理由是**基准 = 市场代理**: 让它也过一遍阈值, 衡量的就只剩"在
    同一批候选里排序排得好不好", 而"避开了太贵/太便宜那一段"这个真实决策的贡献会被算进
    基准里抵消掉。而 ``priced_rows`` 是 ``_pre_filter_codes_by_price`` 的输出, 那正是
    ``ScoreStrategyConfig`` 的价格带。

    缺价那一档不同 —— 没有成交价的债基准也买不进去, 那是数据事实。所以两种剔除要分开。
    """
    from datetime import date

    provider = StrategyFakeProvider()
    monkeypatch.setattr(
        "convertible_bond.strategy_backtest.batch_price_from_provider_threaded",
        _positive_bonus_batch_price)

    def run(max_price):
        return backtest_score_strategy(
            provider, ["113001.SH", "113002.SH", "113003.SH"],
            start_date=date(2025, 1, 2), end_date=date(2025, 1, 31),
            config=ScoreStrategyConfig(
                top_n=3, rebalance_freq="M", holding_mode="pool",
                funding_mode="full_invest", min_confidence=None, exclude_risk_tags=(),
                compute_benchmark=True, pre_filter_prices=True,
                max_market_price=max_price),
        )["periods"][0]

    wide = run(None)
    narrow = run(101.0)     # 把 113002(105)/113003(115) 挡在候选之外
    assert len(narrow["positions"]) < len(wide["positions"]), "前提不成立: 价格带没起作用"
    assert sorted(narrow.get("benchmark_codes") or []) == sorted(
        wide.get("benchmark_codes") or []), "基准跟着策略的价格带一起缩了"
    assert narrow["benchmark_return"] == pytest.approx(wide["benchmark_return"])


def test_curve_annualization_follows_the_actual_observation_spacing():
    """年化因子按曲线**实际间距**推, 不写死 252。

    曲线只在"某只持仓当天有价"的日子上出点, 一个空仓期整月只贡献**一个**点, 于是同一条
    序列里混着日频与月频观测, 而波动率/夏普/索提诺全按"每个观测都是一个交易日"算。
    """
    from datetime import date, timedelta

    from convertible_bond.strategy_backtest import _curve_periods_per_year

    daily = [{"date": date(2025, 1, 1) + timedelta(days=i), "equity": 1.0}
             for i in range(60) if (date(2025, 1, 1) + timedelta(days=i)).weekday() < 5]
    monthly = [{"date": date(2025, m, 28), "equity": 1.0} for m in range(1, 13)]

    assert _curve_periods_per_year(daily) == 252.0, "日频曲线不该偏离 252"
    assert 8.0 < _curve_periods_per_year(monthly) < 16.0, "月频曲线仍按日频年化"
    assert _curve_periods_per_year(monthly[:2]) == 252.0      # 样本不足回落
    assert _curve_periods_per_year([]) == 252.0


def test_equity_curve_points_never_predate_their_own_period():
    """一期吐出的净值点不许落在本期起点之前。

    建仓价可能取到一个陈旧收盘 (``signal_close`` 口径允许 lookback), 于是这一期会吐出
    一个**上一期区间内**的点; ``_upsert_equity_points`` 对同日点是覆盖, 上一期真实的
    盘中估值就被这一期的开仓值顶掉, 曲线上凭空多一段假横盘。
    """
    from datetime import date

    from convertible_bond.strategy_backtest import _portfolio_mark_to_market_curve

    period_start, period_end = date(2025, 2, 1), date(2025, 2, 28)
    stale_entry = date(2025, 1, 20)      # 早于本期起点
    positions = [{"bond_code": "A.SH", "entry_date": stale_entry,
                  "exit_date": period_end, "start_price": 100.0, "end_price": 110.0,
                  "exit_reason": "rebalance"}]
    curve = _portfolio_mark_to_market_curve(
        _bare_provider(), positions, start_equity=1.0, period_end=period_end,
        cost=0.0, intended_count=1, period_start=period_start)
    assert curve, "曲线不该是空的"
    assert all(p["date"] >= period_start for p in curve), (
        f"有点落在本期起点之前: {[p['date'] for p in curve if p['date'] < period_start]}")

    # **边界两侧都要钉**。上一版断言是单侧的 (只查"不早于"), 而 fixture 又不产生
    # ``period_start`` **当天**的点 —— 于是把判据从 ``< period_start`` 收紧成
    # ``<= period_start`` 也看不见, 而那会把本期起点那一天的估值一起丢掉。
    on_start = [{"bond_code": "B.SH", "entry_date": period_start,
                 "exit_date": period_end, "start_price": 100.0, "end_price": 110.0,
                 "exit_reason": "rebalance"}]
    curve2 = _portfolio_mark_to_market_curve(
        _bare_provider(), on_start, start_equity=1.0, period_end=period_end,
        cost=0.0, intended_count=1, period_start=period_start)
    assert any(p["date"] == period_start for p in curve2), (
        "起点当天的点被一起裁掉了 —— 判据从 < 收紧成了 <=")


def test_excess_vs_index_requires_a_matching_window():
    """指数没覆盖到回测起点时不许报超额。

    ``_index_benchmark_curve`` 在缺价的调仓日直接跳过, 并把**第一个取得到价的日子**
    归一成 1.0 —— 数据源前 k 个调仓日没有指数价时, 指数收益只覆盖回测尾段, 而策略收益
    覆盖全程, 两个不同期限的数相减没有意义。
    """
    import inspect

    from convertible_bond.strategy_backtest import _summarize_strategy

    src = inspect.getsource(_summarize_strategy)
    assert "index_covers_full_window" in src
    # 覆盖不全时 excess 必须是 None, 而且要能与"根本没有指数基准"区分开
    assert '"index_covers_full_window": index_covers_full_window' in src


def test_summary_annualizes_with_the_curve_spacing_it_actually_has():
    """``_summarize_strategy`` 必须**真的用** ``_curve_periods_per_year``。

    上一版守护只测了那个 helper 本身, 于是把调用点改回写死 252 照样全绿 —— 测了函数
    没测接线。这条直接比对同一条月频曲线下报出来的年化波动。
    """
    from datetime import date

    from convertible_bond.strategy_backtest import _summarize_strategy

    # 月频曲线: 12 个点, 收益一正一负交替
    curve, equity = [], 1.0
    for m in range(1, 13):
        equity *= 1.02 if m % 2 else 0.99
        curve.append({"date": date(2025, m, 28), "equity": equity})
    periods = [{"period_return": 0.0}]        # 让 use_curve_returns 成立

    summary = _summarize_strategy(
        curve, periods, start_date=date(2025, 1, 1), end_date=date(2025, 12, 31),
        freq="M", top_n=10)
    vol = summary["annualized_volatility"]
    assert vol is not None

    import numpy as np
    rets = [curve[i]["equity"] / curve[i - 1]["equity"] - 1.0 for i in range(1, len(curve))]
    std = float(np.std(rets, ddof=1))
    assert vol == pytest.approx(std * (12 ** 0.5), rel=0.25), (
        f"月频曲线报出的年化波动 {vol:.4f} 不像 12 期/年; "
        f"按 252 会是 {std * (252 ** 0.5):.4f}")


def test_excess_vs_index_is_withheld_when_the_index_starts_late():
    """指数没覆盖到回测起点时, ``excess_vs_index`` 必须是 None。

    上一版守护只扫源码里有没有那个变量名, 把判据删掉照样全绿。
    """
    from datetime import date

    from convertible_bond.strategy_backtest import _summarize_strategy

    curve = [{"date": date(2025, 1, 2), "equity": 1.0},
             {"date": date(2025, 12, 31), "equity": 1.10}]
    periods = [{"period_return": 0.10}]

    late = [{"date": date(2025, 7, 1), "equity": 1.0},
            {"date": date(2025, 12, 31), "equity": 1.05}]
    s_late = _summarize_strategy(curve, periods, start_date=date(2025, 1, 2),
                                 end_date=date(2025, 12, 31), freq="M", top_n=10,
                                 index_benchmark_curve=late)
    assert s_late["index_covers_full_window"] is False
    assert s_late["excess_vs_index"] is None, "指数只覆盖尾段却报了超额"
    assert s_late["index_benchmark_total_return"] is not None   # 指数本身照常报

    full = [{"date": date(2025, 1, 2), "equity": 1.0},
            {"date": date(2025, 12, 31), "equity": 1.05}]
    s_full = _summarize_strategy(curve, periods, start_date=date(2025, 1, 2),
                                 end_date=date(2025, 12, 31), freq="M", top_n=10,
                                 index_benchmark_curve=full)
    assert s_full["index_covers_full_window"] is True
    assert s_full["excess_vs_index"] == pytest.approx(0.10 - 0.05)
