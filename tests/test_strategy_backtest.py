import json
from datetime import date, timedelta

import pytest

from convertible_bond.data_providers import BondTerms, DataProvider
from convertible_bond.strategy_backtest import (
    ScoreStrategyConfig,
    backtest_score_strategy,
    build_rebalance_schedule,
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
    from convertible_bond.gui.controllers.backtest import (
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


def test_strategy_result_tab_change_refreshes_selected_panel():
    from convertible_bond.gui.controllers.backtest import BacktestMixin

    class Tabs:
        def __init__(self, selected):
            self.selected = selected

        def get(self):
            return self.selected

    class DummyApp(BacktestMixin):
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
    from convertible_bond.gui.controllers.backtest import BacktestMixin

    class Var:
        def __init__(self, value):
            self.value = value

        def get(self):
            return self.value

    class DummyApp(BacktestMixin):
        def __init__(self, path):
            self._path = path
            self.v_st_template = Var("自定义")
            self.v_st_view = Var("低估候选")
            self.v_st_freq = Var("月")
            self.v_st_top_n = Var("10")
            self._strategy_compare_results = []

        def _strategy_snapshot_path(self):
            return self._path

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

    app = DummyApp(path)
    app._load_strategy_backtest_snapshot(silent=True, render=False)

    assert app._last_strategy_bt_result["summary"]["final_equity"] == 1.0
    assert "总览" in app._strategy_dirty_tabs
    assert "数据" in app._strategy_dirty_tabs
    assert len(app._strategy_compare_results) == 1


def test_strategy_result_tab_failure_keeps_dirty_for_retry():
    from convertible_bond.gui.controllers.backtest import BacktestMixin

    class Tabs:
        def __init__(self, selected):
            self.selected = selected

        def get(self):
            return self.selected

    class DummyApp(BacktestMixin):
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
    from convertible_bond.gui.controllers.backtest import BacktestMixin

    class Var:
        def __init__(self, value):
            self.value = value

        def get(self):
            return self.value

        def set(self, value):
            self.value = value

    class DummyApp(BacktestMixin):
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
