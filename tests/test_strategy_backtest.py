import json
from datetime import date, timedelta

import pytest

from convertible_bond.data_providers import BondTerms, DataProvider
from convertible_bond.strategy_backtest import (
    ScoreStrategyConfig,
    backtest_score_strategy,
    build_rebalance_schedule,
    write_strategy_backtest_csv,
)


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
    provider.bond_history["113003.SH"] = [(date(2025, 1, 2), 90.0)]
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
    provider.bond_history["113003.SH"] = [(date(2025, 1, 2), 90.0)]   # 无期末价 → 缺价
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
                min_score=None, min_confidence=None, exclude_risk_tags=(),
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
    provider.bond_history["113003.SH"] = [(date(2025, 1, 2), 90.0)]   # 缺期末价 → 现金 1/3
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
    provider.bond_history["113003.SH"] = [(date(2025, 1, 2), 90.0)]
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


def test_strategy_template_resets_new_knobs_to_full_config():
    """模板 = 完整可复现配置: 选券权重/现金收益不残留上次手动值。"""
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
            # 模拟残留态: 用户上次手动改过的新旋钮
            self.v_st_weighting = Var("等权全池")
            self.v_st_cash_yield = Var("0")

    app = DummyApp()
    app._apply_strategy_template("稳健打底")
    assert app.v_st_weighting.get() == "机会分排序"   # 随模板归位, 不残留
    assert app.v_st_cash_yield.get() == "2.2"
    assert app.v_st_view.get() == "综合机会"
    assert app.v_st_top_n.get() == "15"
    assert app.v_st_max_price.get() == "120"


def test_backtest_cache_history_end_never_extends_into_future():
    """批量历史区间 clamp 到昨天: 否则磁盘缓存的'只缓存过去'守卫拒绝落盘, 复跑退化为全量重拉。"""
    from convertible_bond.strategy_backtest import _BacktestCacheProvider
    provider = StrategyFakeProvider()
    near_today = _BacktestCacheProvider(
        provider, start_date=date.today() - timedelta(days=90), end_date=date.today(),
        price_lookback_days=31, execution_lookahead_days=10, vol_window_days=21)
    assert near_today._history_end <= date.today() - timedelta(days=1)
    # 区间完全在过去时不受 clamp 影响 (保持原 padding 语义)
    past = _BacktestCacheProvider(
        provider, start_date=date(2024, 1, 2), end_date=date(2024, 6, 28),
        price_lookback_days=31, execution_lookahead_days=10, vol_window_days=21)
    assert past._history_end == date(2024, 6, 28) + timedelta(days=25)


def test_turnover_cost_uses_actual_holdings_not_phantom_selected(monkeypatch):
    """换手/成本必须基于实际持仓码, 不能把'选中但缺成交价'的票当真实持仓。

    P1 持 {A,B,C}(各1/3); P2 中 C 缺期末价被剔除 → 实际持 {A,B}(各1/2)。
    正确单边换手 = 卖出 C(1/3) = 1/3; 旧实现用 selected_codes(含C) + 分母=held,
    权重和>1, 会算出错误换手 (0.25)。
    """
    provider = StrategyFakeProvider()
    # C 只到 01-31: P1 可建仓 (entry 01-02/exit 01-31), P2 期末(02-28)无价且 01-31 过旧 → 缺价
    provider.bond_history["113003.SH"] = [(date(2025, 1, 2), 90.0), (date(2025, 1, 31), 95.0)]
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


def test_score_strategy_can_hold_cash_when_score_filter_rejects_all(monkeypatch):
    provider = StrategyFakeProvider()

    def fake_batch_price(provider_arg, codes, *, valuation_date, **kwargs):
        return [
            {
                "bond_code": code,
                "status": "ok",
                "S0": 100.0,
                "K": 100.0,
                "sigma": 0.30,
                "theoretical_price": 101.0,
                "market_price": 100.0,
                "deviation": -1.0 / 101.0,
                "credit_rating": "AA+",
                "outstanding_balance": 10.0,
                "T": 3.0,
            }
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
        config=ScoreStrategyConfig(top_n=2, min_score=50.0),
    )

    assert result["periods"][0]["selected_codes"] == []
    assert result["periods"][0]["period_return"] == 0.0
    assert result["summary"]["final_equity"] == 1.0


def test_score_strategy_default_rejects_negative_opportunity_scores(monkeypatch):
    provider = StrategyFakeProvider()

    def fake_batch_price(provider_arg, codes, *, valuation_date, **kwargs):
        rows = []
        for code in codes:
            market = _latest(provider_arg.bond_history[code], valuation_date)
            sigma = 1.0 if code == "113002.SH" else 0.30
            theo = market if code == "113002.SH" else market * 1.10
            rows.append({
                "bond_code": code,
                "bond_name": provider_arg.terms[code].sec_name,
                "stock_code": provider_arg.terms[code].underlying_code,
                "status": "ok",
                "S0": market,
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
        ["113001.SH", "113002.SH"],
        start_date=date(2025, 1, 2),
        end_date=date(2025, 1, 31),
        config=ScoreStrategyConfig(top_n=2, min_confidence=None, exclude_risk_tags=()),
    )

    period = result["periods"][0]
    assert period["selected_codes"] == ["113001.SH"]
    assert period["cash_weight"] == pytest.approx(0.5)
    assert period["weight_denominator"] == 2
    assert period["turnover"] == pytest.approx(0.5)
    assert period["positions"][0]["weight"] == pytest.approx(0.5)
    assert period["period_return"] == pytest.approx(0.05)
    assert any(
        row["bond_code"] == "113002.SH" and "最低分 0.0" in row["reason"]
        for row in period["rejection_rows"]
    )

    full_invest = backtest_score_strategy(
        provider,
        ["113001.SH", "113002.SH"],
        start_date=date(2025, 1, 2),
        end_date=date(2025, 1, 31),
        config=ScoreStrategyConfig(
            top_n=2,
            funding_mode="full_invest",
            min_confidence=None,
            exclude_risk_tags=(),
        ),
    )

    full_period = full_invest["periods"][0]
    assert full_period["selected_codes"] == ["113001.SH"]
    assert full_period["cash_weight"] == pytest.approx(0.0)
    assert full_period["weight_denominator"] == 1
    assert full_period["turnover"] == pytest.approx(1.0)
    assert full_period["positions"][0]["weight"] == pytest.approx(1.0)
    assert full_period["period_return"] == pytest.approx(0.10)


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
    assert "机会分" in period["candidate_rows"][0]["selection_reason"]
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
    assert archive_payload["schema_version"] == 2
    assert archive_payload["snapshot_id"] == info["snapshot_id"]
    assert archive_payload["meta"]["config"]["selection_view"] == "综合机会"
    # 三层字段进入快照 meta (P3a)
    assert archive_payload["meta"]["config"]["holding_mode"] == "pool"
    assert archive_payload["meta"]["config"]["funding_mode"] == "full_invest"
    assert archive_payload["meta"]["config"]["top_n_shortfall_policy"] == "renormalize"  # 兼容镜像
    assert archive_payload["meta"]["config"]["history_mode"] == "Wind高保真"
    assert archive_payload["result"]["config"]["history_mode"] == "Wind高保真"
    assert archive_payload["meta"]["run_settings"]["data_source"] == "Wind"
    assert archive_payload["meta"]["run_settings"]["pool"]["bond_codes"] == [
        "113001.SH", "113002.SH",
    ]
    assert archive_payload["meta"]["run_settings"]["pricing"]["M"] == 120
    assert archive_payload["result"]["run_settings"]["admission_filter"]["min_credit_rating"] == "A+"
    assert archive_payload["meta"]["period_count"] == 1
    assert "_snapshot_path" not in archive_payload["result"]


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
        ("总览", ["insight", "chart", "idle"]),
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
    assert "总览" in app._strategy_dirty_tabs
    assert "数据" in app._strategy_dirty_tabs
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
