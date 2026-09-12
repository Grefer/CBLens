"""跨期数量、现金约束、票息与实际兑付的账本回归。"""
from datetime import date, timedelta

import pytest

from convertible_bond.data_providers.base import BondTerms, CashflowSchedule
from convertible_bond.strategy_ledger import StrategyLedger


def d(value):
    return date.fromisoformat(value)


class LedgerProvider:
    def __init__(self, histories, *, terms=None, cashflows=None):
        self.histories = {code: [(d(day), px) for day, px in values]
                          for code, values in histories.items()}
        self.terms = terms or {}
        self.cashflows = cashflows or {}

    def get_bond_history(self, code, start, end):
        return [(day, px) for day, px in self.histories.get(code, []) if start <= day <= end]

    def get_bond_terms(self, code, on_date):
        return self.terms.get(code, BondTerms(face_value=100.0))

    def get_cashflow(self, code):
        return self.cashflows.get(code)


def run(ledger, provider, selected, start, end, **kwargs):
    return ledger.run_period(provider, [{"bond_code": code} for code in selected], d(start), d(end),
                             intended_count=kwargs.pop("intended_count", len(selected)), **kwargs)


def assert_reconciles(result):
    assert result["period_return"] == pytest.approx(
        result["gross_return"] + result["cash_yield_return"] - result["cost"], abs=1e-12)
    assert result["equity"] == pytest.approx(
        result["available_cash"] + result["receivable_value"]
        + sum(row["market_value"] for row in result["ending_holdings"]), abs=1e-12)
    assert result["equity"] == pytest.approx(result["curve"][-1]["equity"], abs=1e-12)
    assert sum(row["return_contribution"] for row in result["positions"]) == pytest.approx(
        result["gross_return"], abs=1e-12)
    assert result["available_cash"] == pytest.approx(
        result["opening_cash"] + sum(row["cash_change"] for row in result["ledger_entries"]), abs=1e-12)


def test_halt_carries_quantity_and_blocks_reinvestment_until_actual_resumption():
    provider = LedgerProvider({
        "A": [("2025-01-02", 100), ("2025-01-03", 50), ("2025-03-03", 40)],
        "B": [("2025-01-31", 100), ("2025-02-28", 200), ("2025-03-03", 210)],
    })
    ledger = StrategyLedger()
    first = run(ledger, provider, ["A"], "2025-01-02", "2025-01-31")
    second = run(ledger, provider, ["B"], "2025-01-31", "2025-02-28")
    third = run(ledger, provider, [], "2025-02-28", "2025-03-05")
    assert first["equity"] == pytest.approx(0.5)
    assert first["positions"][0]["exit_date"] is None
    assert second["equity"] == pytest.approx(0.5)
    assert second["available_cash"] == 0
    assert second["blocked_count"] == 1
    assert second["turnover"] == 0
    assert second["ending_holdings"][0]["quantity"] == pytest.approx(0.01)
    assert second["skipped_positions"][0]["bond_code"] == "B"
    assert third["available_cash"] == pytest.approx(0.4)
    assert third["positions"][0]["exit_date"] == d("2025-03-03")
    for result in (first, second, third):
        assert_reconciles(result)


def test_next_close_keeps_old_holding_through_signal_and_never_reads_future_period():
    provider = LedgerProvider({
        "A": [("2025-01-02", 100), ("2025-01-31", 110), ("2025-02-03", 120)],
        "B": [("2025-01-31", 100), ("2025-02-03", 200), ("2025-02-28", 210)],
    })
    ledger = StrategyLedger()
    first = run(ledger, provider, ["A"], "2025-01-02", "2025-01-31")
    second = run(ledger, provider, ["B"], "2025-01-31", "2025-02-28", execution_timing="next_close")
    assert first["equity"] == pytest.approx(1.1)
    assert max(point["date"] for point in first["curve"]) == d("2025-01-31")
    assert second["equity"] == pytest.approx(1.26)
    assert all(entry["date"] == d("2025-02-03") for entry in second["ledger_entries"])
    assert next(row for row in second["positions"] if row["bond_code"] == "A")["price_return"] == pytest.approx(120/110 - 1)
    assert_reconciles(first)
    assert_reconciles(second)


def test_unchanged_membership_rebalances_drifted_weights_and_pays_both_trade_sides():
    provider = LedgerProvider({
        "A": [("2025-01-02", 100), ("2025-01-31", 200), ("2025-02-28", 200)],
        "B": [("2025-01-02", 100), ("2025-01-31", 100), ("2025-02-28", 100)],
    })
    ledger = StrategyLedger()
    run(ledger, provider, ["A", "B"], "2025-01-02", "2025-01-31")
    result = run(ledger, provider, ["A", "B"], "2025-01-31", "2025-02-28", transaction_cost=0.01)
    trades = [r for r in result["ledger_entries"] if r["kind"] in {"buy", "sell"}]
    assert {r["kind"] for r in trades} == {"buy", "sell"}
    assert result["cost"] == pytest.approx(sum(r["amount"] for r in trades) * 0.01 / 1.5)
    assert result["turnover"] == pytest.approx(0.25 / 1.5)
    assert result["available_cash"] >= 0
    assert_reconciles(result)


def test_full_rotation_costs_sell_and_buy_and_does_not_borrow_for_fees():
    provider = LedgerProvider({"A": [("2025-01-02", 100), ("2025-01-31", 100)],
                               "B": [("2025-01-31", 100), ("2025-02-28", 100)]})
    ledger = StrategyLedger()
    first = run(ledger, provider, ["A"], "2025-01-02", "2025-01-31", transaction_cost=0.01)
    second = run(ledger, provider, ["B"], "2025-01-31", "2025-02-28", transaction_cost=0.01)
    assert first["equity"] == pytest.approx(1 / 1.01)
    assert second["equity"] == pytest.approx(first["equity"] * 0.99 / 1.01)
    assert second["cost"] == pytest.approx(0.01 * (1 + 0.99 / 1.01))
    assert second["available_cash"] == 0
    assert_reconciles(second)


def test_contract_coupon_is_receivable_until_explicit_payment_and_boundary_never_double_counts():
    provider = LedgerProvider({"A": [("2025-01-02", 102), ("2025-01-15", 100), ("2025-01-31", 100),
                                     ("2025-02-28", 100)]},
                              terms={"A": BondTerms(issue_date=d("2024-01-15"),
                                                     maturity_date=d("2030-01-15"),
                                                     coupon_rates=(0.02,) * 6, face_value=100)})
    ledger = StrategyLedger()
    first = run(ledger, provider, ["A"], "2025-01-02", "2025-01-15")
    assert first["equity"] == pytest.approx(1.0)
    assert first["available_cash"] == 0
    assert first["receivable_value"] == pytest.approx(2/102)
    provider.cashflows["A"] = CashflowSchedule(cashflows=[{
        "kind": "coupon", "payment_date": "2025-01-20", "coupon_date": "2025-01-15",
        "amount_per_bond": 2.0, "confirmed": True}])
    second = run(ledger, provider, ["A"], "2025-01-15", "2025-01-31")
    assert second["coupon_income"] == 0
    assert second["coupon_cash"] == pytest.approx(2/102)
    assert second["receivable_value"] == 0
    assert second["available_cash"] == pytest.approx(2/102)
    third = run(ledger, provider, ["A"], "2025-01-31", "2025-02-28")
    assert third["coupon_cash"] == 0
    assert third["equity"] == pytest.approx(1.0)
    for result in (first, second, third):
        assert_reconciles(result)


def test_stale_dirty_quote_and_coupon_receivable_do_not_create_double_value():
    provider = LedgerProvider({"A": [("2025-01-02", 102)]},
                              terms={"A": BondTerms(issue_date=d("2024-01-15"),
                                                     maturity_date=d("2030-01-15"),
                                                     coupon_rates=(0.02,), face_value=100)})
    result = run(StrategyLedger(), provider, ["A"], "2025-01-02", "2025-01-31")
    assert result["equity"] == pytest.approx(1)
    assert result["ending_holdings"][0]["mark_price"] == 100
    assert result["available_cash"] == 0
    assert_reconciles(result)


def test_redemption_including_last_coupon_is_booked_once_only_after_confirmed_payment():
    provider = LedgerProvider({"A": [("2025-01-02", 108), ("2025-01-10", 109)]},
                              terms={"A": BondTerms(issue_date=d("2024-01-15"),
                                                     maturity_date=d("2025-01-15"),
                                                     coupon_rates=(0.02,), redemption_price=110,
                                                     call_redemption_date=d("2025-01-15"),
                                                     call_redemption_price=110)},
                              cashflows={"A": CashflowSchedule(cashflows=[{
                                  "kind": "redemption", "payment_date": "2025-01-20", "amount": 110,
                                  "confirmed": True}])})
    ledger = StrategyLedger()
    first = run(ledger, provider, ["A"], "2025-01-02", "2025-01-15")
    assert first["available_cash"] == 0
    assert first["coupon_income"] == 0
    assert first["ending_holdings"]
    second = run(ledger, provider, [], "2025-01-15", "2025-01-31")
    assert second["equity"] == pytest.approx(110/108)
    assert second["available_cash"] == pytest.approx(110/108)
    assert second["coupon_income"] == 0
    assert second["ending_holdings"] == []
    assert second["positions"][0]["exit_date"] == d("2025-01-20")
    assert second["cost"] == 0
    assert len([r for r in second["ledger_entries"] if r["kind"] == "redemption"]) == 1
    assert_reconciles(second)


@pytest.mark.parametrize("cashflow", [
    ("2025-01-15", 110, 2.0),
    {"kind": "redemption", "redemption_date": "2025-01-15", "amount": 110, "confirmed": True},
    {"kind": "redemption", "payment_date": "2025-01-15", "amount": 110, "confirmed": "false"},
])
def test_missing_or_ambiguous_payment_evidence_never_releases_principal(cashflow):
    provider = LedgerProvider({"A": [("2025-01-02", 108)]},
                              terms={"A": BondTerms(maturity_date=d("2025-01-15"), redemption_price=110)},
                              cashflows={"A": CashflowSchedule(cashflows=[cashflow])})
    ledger = StrategyLedger()
    result = run(ledger, provider, ["A"], "2025-01-02", "2025-01-31")
    assert result["available_cash"] == 0
    assert result["redemption_cash"] == 0
    assert result["ending_holdings"][0]["quantity"] == pytest.approx(1/108)
    assert result["cashflow_warnings"]
    assert_reconciles(result)


def test_cash_curve_is_daily_and_chains_exactly_through_empty_periods():
    provider = LedgerProvider({})
    ledger = StrategyLedger()
    first = run(ledger, provider, [], "2025-01-02", "2025-01-31", cash_yield_rate=0.0365)
    second = run(ledger, provider, [], "2025-01-31", "2025-02-28", cash_yield_rate=0.0365)
    assert second["equity"] == pytest.approx(1.0001 ** 57)
    assert second["equity"] == pytest.approx((1 + first["period_return"]) * (1 + second["period_return"]))
    assert [r["date"] for r in first["curve"]] == [day for i in range(29) if (day := d("2025-01-03") + timedelta(days=i)).weekday() < 5]
    assert_reconciles(first)
    assert_reconciles(second)


def test_new_coupon_buyer_on_payment_date_is_not_paid_previous_owners_coupon():
    provider = LedgerProvider({"A": [("2025-01-15", 100), ("2025-01-31", 100)]},
                              cashflows={"A": CashflowSchedule(cashflows=[{
                                  "kind": "coupon", "payment_date": "2025-01-15", "amount": 2,
                                  "confirmed": True}])})
    result = run(StrategyLedger(), provider, ["A"], "2025-01-15", "2025-01-31")
    assert result["coupon_income"] == 0
    assert result["equity"] == pytest.approx(1)


def test_allocation_does_not_use_a_later_quote_to_fund_an_earlier_purchase():
    provider = LedgerProvider({"A": [("2025-01-02", 100), ("2025-01-31", 100), ("2025-02-07", 50)],
                               "B": [("2025-02-03", 100), ("2025-02-07", 200)]})
    ledger = StrategyLedger()
    run(ledger, provider, ["A"], "2025-01-02", "2025-01-31")
    result = run(ledger, provider, ["B"], "2025-01-31", "2025-02-28", execution_timing="next_close")
    buys = [r for r in result["ledger_entries"] if r["kind"] == "buy"]
    assert len(buys) == 1
    assert buys[0]["date"] == d("2025-02-07")
    assert buys[0]["price"] == 200
    assert result["equity"] == pytest.approx(0.5)
    assert_reconciles(result)


def test_coupon_entitlement_survives_sale_between_record_and_payment():
    provider = LedgerProvider({"A": [("2025-01-02", 100), ("2025-01-15", 100),
                                     ("2025-01-16", 98), ("2025-01-31", 98)]},
                              cashflows={"A": CashflowSchedule(cashflows=[{
                                  "kind": "coupon", "record_date": "2025-01-15",
                                  "payment_date": "2025-01-20", "amount": 2, "confirmed": True}])})
    ledger = StrategyLedger()
    run(ledger, provider, ["A"], "2025-01-02", "2025-01-16")
    result = run(ledger, provider, [], "2025-01-16", "2025-01-31")
    assert result["coupon_cash"] == pytest.approx(2/98)
    assert result["equity"] == pytest.approx(1)
    assert not result["ending_holdings"]
    assert_reconciles(result)


def test_confirmed_coupon_without_period_date_clears_the_only_existing_receivable():
    provider = LedgerProvider({"A": [("2025-01-02", 102), ("2025-01-15", 100), ("2025-01-31", 100)]},
                              terms={"A": BondTerms(issue_date=d("2024-01-15"),
                                                     maturity_date=d("2030-01-15"),
                                                     coupon_rates=(0.02,), face_value=100)},
                              cashflows={"A": CashflowSchedule(cashflows=[{
                                  "kind": "coupon", "payment_date": "2025-01-20", "amount": 2,
                                  "confirmed": True}])})
    result = run(StrategyLedger(), provider, ["A"], "2025-01-02", "2025-01-31")
    assert result["equity"] == pytest.approx(1)
    assert result["receivable_value"] == 0
    assert result["coupon_cash"] == pytest.approx(2/102)
    assert result["coupon_income"] == pytest.approx(2/102)
    assert_reconciles(result)


def test_full_invest_rebalances_existing_members_when_later_member_can_first_trade():
    provider = LedgerProvider({"A": [("2025-01-03", 100), ("2025-01-06", 100), ("2025-01-31", 100)],
                               "B": [("2025-01-06", 100), ("2025-01-31", 200)]})
    result = run(StrategyLedger(), provider, ["A", "B"], "2025-01-02", "2025-01-31",
                 execution_timing="next_close", funding_mode="full_invest")
    assert result["equity"] == pytest.approx(1.5)
    assert result["weight_denominator"] == 2
    sales = [r for r in result["ledger_entries"] if r["kind"] == "sell"]
    assert len(sales) == 1
    assert sales[0]["date"] == d("2025-01-06")
    assert sales[0]["amount"] == pytest.approx(0.5)
    assert_reconciles(result)


def test_frozen_holding_consumes_exposure_budget_before_any_new_purchase():
    provider = LedgerProvider({"A": [("2025-01-02", 100)],
                               "B": [("2025-01-02", 100), ("2025-01-31", 100)],
                               "C": [("2025-01-31", 100), ("2025-02-28", 200)]})
    ledger = StrategyLedger()
    run(ledger, provider, ["A", "B"], "2025-01-02", "2025-01-31")
    result = run(ledger, provider, ["C"], "2025-01-31", "2025-02-28", exposure=0.2)
    assert result["available_cash"] == pytest.approx(0.5)
    assert result["blocked_weight"] == pytest.approx(0.5)
    assert result["equity"] == pytest.approx(1)
    assert not [r for r in result["ledger_entries"] if r["kind"] == "buy"]
    assert_reconciles(result)


def test_unconfirmed_coupon_cannot_manufacture_equity_above_stale_dirty_value():
    provider = LedgerProvider({"A": [("2025-01-02", 1)]},
                              terms={"A": BondTerms(issue_date=d("2024-01-15"),
                                                     maturity_date=d("2030-01-15"),
                                                     coupon_rates=(0.02,), face_value=100)})
    result = run(StrategyLedger(), provider, ["A"], "2025-01-02", "2025-01-31")
    assert result["equity"] == pytest.approx(1)
    assert result["receivable_value"] == pytest.approx(1)
    assert result["receivable_nominal_value"] == pytest.approx(2)
    assert result["available_cash"] == 0
    assert_reconciles(result)


def test_cancellation_checked_before_fetching_every_security():
    provider = LedgerProvider({"A": [("2025-01-02", 100)], "B": [("2025-01-02", 100)]})
    reads = []
    original = provider.get_bond_history

    def history(code, start, end):
        reads.append(code)
        return original(code, start, end)

    provider.get_bond_history = history

    def cancel():
        if reads:
            raise InterruptedError("cancelled")

    with pytest.raises(InterruptedError, match="cancelled"):
        run(StrategyLedger(), provider, ["A", "B"], "2025-01-02", "2025-01-31", cancel_cb=cancel)
    assert reads == ["A"]


def test_patch_coverage_never_attributes_one_bonds_patch_to_the_whole_period():
    from convertible_bond.strategy_backtest import _compute_patch_coverage

    result = _compute_patch_coverage([
        {"start_date": d("2025-01-02"), "selected_codes": ["A", "B"],
         "data_quality": {"patch_applied_count": 1, "bond_sources": [
             {"bond_code": "A", "patch_count": 1}, {"bond_code": "B", "patch_count": 0}]}},
        # 旧快照只有总数，C 的逐债来源未知，不能推断成已覆盖。
        {"start_date": d("2025-02-03"), "selected_codes": ["C"],
         "data_quality": {"patch_applied_count": 2}},
    ])
    assert result["bonds_with_patches"] == 1
    assert result["bonds_without_patches"] == ["B"]
    assert result["bonds_unknown"] == ["C"]


def test_dividend_sources_survive_candidate_rows_and_period_aggregation():
    from convertible_bond.strategy_backtest import (
        ScoreStrategyConfig, _candidate_explanation_rows, _summarize_data_quality,
    )

    row = {"bond_code": "A", "q_source": "historical", "q_provider": "fake",
           "q_as_of": d("2025-01-02"), "q_observed_pct": 2.0}
    candidate = _candidate_explanation_rows([row], ["A"], ScoreStrategyConfig())[0]
    assert candidate["q_source"] == "historical"
    assert candidate["q_as_of"] == row["q_as_of"]
    result = _summarize_data_quality([
        {"data_quality": {"dividend_source_counts": {"historical": 3, "fixed": 1}}},
        {"data_quality": {"dividend_source_counts": {"historical": 2, "realtime_snapshot": 1}}},
    ])
    assert result["dividend_source_counts"] == {"historical": 5, "fixed": 1, "realtime_snapshot": 1}
