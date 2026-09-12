"""小规模研究编排的离线端到端验证：缓存复用、同池比较与正常入口隔离。"""
from dataclasses import replace
from datetime import date, timedelta

import pytest

from convertible_bond.data_providers.base import BondTerms, DataProvider
from convertible_bond.strategy_backtest import PDEStrategyConfig
from convertible_bond.strategy_research import (
    BASELINE_NAME,
    CANDIDATE_POOL_NAME,
    VALUATION_NAME,
    build_neighborhood_variants,
    holding_overlap,
    run_strategy_comparison,
    run_strategy_neighborhood,
)
from convertible_bond.strategy_sweep import sweep_pde_strategy


class ResearchProvider(DataProvider):
    name = "offline-research"

    def get_bond_terms(self, bond_code, valuation_date):
        return BondTerms(
            issue_date=date(2020, 1, 1), listing_date=date(2020, 2, 1),
            maturity_date=date(2030, 1, 1), underlying_code="600001.SH",
            conversion_price=100, credit_rating="AA+", outstanding_balance=10,
        )

    def get_bond_history(self, bond_code, start, end):
        index = int(bond_code[:6]) - 113000
        origin = date(2025, 1, 1)
        return [(day, 100 + index + (day - origin).days * 0.02 * index
                 + 2 * ((day - origin).days // 30 % 2))
                for n in range((end - start).days + 1)
                if (day := start + timedelta(days=n)).weekday() < 5]

    def get_stock_history(self, stock_code, start, end):
        return self.get_bond_history("113001.SH", start, end)

    def get_stock_close(self, stock_code, on_date):
        return 100.0


@pytest.fixture
def pricing_calls(monkeypatch):
    calls = []

    def price(provider, codes, **kwargs):
        calls.append((kwargs["valuation_date"], tuple(codes)))
        return [
            {"bond_code": code, "status": "ok", "confidence": "高", "risk_tags": [],
             "market_price": 100.0, "theoretical_price": 110.0, "parity": 100.0,
             "deviation": 0.08 + i * 0.02, "relative_deviation": -0.1 + i * 0.01,
             "cross_section_origin": "market", "credit_rating": "AA+",
             "outstanding_balance": 10.0, "T": 4.0}
            for i, code in enumerate(codes)
        ]

    monkeypatch.setattr("convertible_bond.strategy_backtest.batch_price_from_provider_threaded", price)
    return calls


def test_neighborhood_is_single_factor_and_deduplicates_boundaries():
    config = PDEStrategyConfig()
    variants = build_neighborhood_variants(config)
    assert variants[0] == {"name": BASELINE_NAME}
    assert len(variants) == 7
    assert all(len(set(item) - {"name"}) == 1 for item in variants[1:])
    assert {item.get("top_n") for item in variants if "top_n" in item} == {8, 12}
    edge = build_neighborhood_variants(replace(config, top_n=1, transaction_cost=0))
    assert len(edge) == 5
    assert all(item.get("top_n", 1) >= 1 for item in edge)


def test_neighborhood_reuses_one_pricing_panel_and_exposes_metrics(pricing_calls):
    config = PDEStrategyConfig(top_n=3, min_relative_cheapness=None)
    result = run_strategy_neighborhood(
        ResearchProvider(), [f"11300{i}.SH" for i in range(1, 7)],
        start_date=date(2025, 1, 2), end_date=date(2025, 6, 30), base_config=config,
    )
    # 六个持有区间只批价六次，七组没有重复求 PDE。
    assert len(pricing_calls) == 6
    assert len(set(pricing_calls)) == 6
    assert len(result["variants"]) == len(result["results"]) == 7
    base = next(row for row in result["variants"] if row["name"] == BASELINE_NAME)
    assert base["holding_overlap"] == 1.0
    assert {"sharpe", "max_drawdown", "avg_turnover", "holding_overlap"} <= base.keys()
    assert "deflated" in result
    assert result["base_name"] == BASELINE_NAME
    assert config.pre_filter_prices is True
    assert all(not item["config"]["pre_filter_prices"] for item in result["results"].values())


def test_three_arms_share_candidates_and_execution_assumptions(pricing_calls):
    result = run_strategy_comparison(
        ResearchProvider(), [f"11300{i}.SH" for i in range(1, 7)],
        start_date=date(2025, 1, 2), end_date=date(2025, 6, 30),
        base_config=PDEStrategyConfig(top_n=2, min_relative_cheapness=None),
    )
    assert len(pricing_calls) == 6
    baseline = result["results"][BASELINE_NAME]
    pool = result["results"][CANDIDATE_POOL_NAME]
    exposure = result["results"][VALUATION_NAME]
    for number in range(6):
        rows = [arm["periods"][number] for arm in (baseline, pool, exposure)]
        assert len({row["candidate_count"] for row in rows}) == 1
        assert {r["bond_code"] for r in rows[0]["candidate_rows"]} == {
            r["bond_code"] for r in rows[1]["candidate_rows"]}
        assert rows[0]["selected_codes"] == rows[2]["selected_codes"]
        assert len(rows[0]["positions"]) == 2
        assert len(rows[1]["positions"]) == 6
        assert rows[0]["exposure"] == 1.0 and rows[2]["exposure"] < 1.0
    for key in ("execution_timing", "transaction_cost", "cash_yield_rate", "funding_mode"):
        assert len({arm["config"][key] for arm in (baseline, pool, exposure)}) == 1
    # 全候选池对照只在研究入口放行，原 PDE 扫描继续拒绝。
    with pytest.raises(ValueError, match="PDE扫描不支持"):
        sweep_pde_strategy(ResearchProvider(), ["113001.SH"], start_date=date(2025, 1, 1),
                           end_date=date(2025, 6, 30), variants=[{"holding_mode": "pool"}])


def test_overlap_uses_actual_matched_holdings_and_does_not_reward_all_cash():
    def period(day, codes):
        return {"start_date": day, "end_date": day + "-end",
                "positions": [{"bond_code": code} for code in codes]}

    left = {"periods": [period("a", ["A", "B"]), period("b", []), period("c", ["D"])]}
    right = {"periods": [period("a", ["B", "C"]), period("b", [])]}
    assert holding_overlap(left, right) == {
        "mean": 1 / 3, "compared_periods": 1, "both_cash_periods": 1, "unmatched_periods": 1,
    }


def test_legacy_base_is_rejected_before_data_access():
    with pytest.raises(ValueError, match="研究基线必须"):
        run_strategy_neighborhood(object(), ["A"], start_date=date(2025, 1, 1),
                                  end_date=date(2025, 6, 30),
                                  base_config=PDEStrategyConfig(rank_signal="double_low"))


def test_sweep_best_does_not_select_nan_sharpe(monkeypatch):
    from convertible_bond import strategy_sweep

    def run(provider, codes, **kwargs):
        n = kwargs["config"].top_n
        returns = [0.0] * 4 if n == 1 else [0.01, 0.03, -0.01, 0.02]
        return {"periods": [{"period_return": value} for value in returns], "summary": {}}

    monkeypatch.setattr(strategy_sweep, "backtest_score_strategy", run)
    result = strategy_sweep.sweep_pde_strategy(
        ResearchProvider(), ["A"], start_date=date(2025, 1, 1), end_date=date(2025, 6, 30),
        variants=[{"name": "无样本方差", "top_n": 1}, {"name": "有效", "top_n": 2}],
    )
    assert result["best"]["name"] == "有效"
