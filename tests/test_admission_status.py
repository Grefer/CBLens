from datetime import date

from convertible_bond.admission_status import (
    changed_admission_fields,
    merge_admission_status,
    refresh_admission_status,
)
from convertible_bond.data_providers import BondTerms


def test_merge_admission_status_only_overwrites_non_empty_patch_fields():
    base = BondTerms(
        sec_name="测试转债",
        credit_rating="AA",
        outstanding_balance=3.0,
        suspension_status="正常交易",
    )
    patch = BondTerms(
        credit_rating=None,
        outstanding_balance=2.8,
        suspension_status=None,
        call_status="已公告强赎",
    )

    merged = merge_admission_status(base, patch)

    assert merged.credit_rating == "AA"
    assert merged.outstanding_balance == 2.8
    assert merged.suspension_status == "正常交易"
    assert merged.call_status == "已公告强赎"
    assert changed_admission_fields(base, merged) == ["call_status", "outstanding_balance"]


def test_refresh_admission_status_updates_store_and_reports_exclusions():
    class FakeProvider:
        name = "fake"

        def get_bond_terms(self, code, valuation_date):
            return BondTerms(sec_name="新增转债", credit_rating="AA", outstanding_balance=2.0)

        def get_admission_status(self, code, valuation_date, base_terms=None):
            if code == "113001.SH":
                return BondTerms(suspension_status="停牌", bond_turnover_amount=100.0)
            return BondTerms(credit_rating="AA+", outstanding_balance=5.0)

    class FakeStore:
        def __init__(self):
            self.data = {
                "113001.SH": BondTerms(sec_name="老转债", credit_rating="AA", outstanding_balance=2.0),
                "113002.SH": BondTerms(sec_name="稳健转债", credit_rating="AA", outstanding_balance=2.0),
            }
            self.saved = []

        def get(self, code):
            return self.data.get(code)

        def set_many(self, items, source="?"):
            self.saved.extend((code, source) for code, _ in items)
            for code, terms in items:
                self.data[code] = terms

    store = FakeStore()
    result = refresh_admission_status(
        FakeProvider(),
        ["113001.SH", "113002.SH"],
        store=store,
        valuation_date=date(2026, 4, 28),
    )

    assert result["success"] == ["113001.SH", "113002.SH"]
    assert result["failed"] == []
    assert ("113001.SH", ["suspension_status", "bond_turnover_amount"]) in result["changed"]
    assert ("113002.SH", ["credit_rating", "outstanding_balance"]) in result["changed"]
    assert result["excluded"] == [("113001.SH", "停牌/暂停交易")]
    assert store.data["113001.SH"].suspension_status == "停牌"
    assert store.saved[0][1] == "fake:admission_status"
