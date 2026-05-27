import json
from datetime import date

import pytest

from convertible_bond.cache import TermsBundle
from convertible_bond.cb_events import CBEvent, CBEventStore
from convertible_bond.data_providers import BondTerms, DataProvider
from convertible_bond.down_reset_overrides import (
    DownResetOverrides,
    ResolvedDownReset,
    resolve_down_reset,
    resolve_down_reset_intensity,
)
from convertible_bond.historical_terms import (
    HistoricalBondDataProvider,
    TermsHistoryStore,
    TermsPatch,
    TermsPatchStore,
    project_terms,
)


class FakeHistoricalProvider(DataProvider):
    name = "fake-history"

    def __init__(self):
        self.terms = BondTerms(
            sec_name="测试转债",
            underlying_code="600001.SH",
            issue_date=date(2020, 1, 1),
            listing_date=date(2020, 2, 1),
            maturity_date=date(2030, 1, 1),
            face_value=100.0,
            conversion_price=8.0,
            close=999.0,
            credit_rating="AA",
            outstanding_balance=6.0,
            is_tradable=True,
            trading_status="tradable",
            call_status="已公告强赎",
            call_announce_date=date(2025, 3, 1),
            call_redemption_date=date(2025, 4, 1),
            delisting_date=date(2025, 4, 2),
            underlying_status="ST/退市风险",
            bond_turnover_amount=1.0,
            down_reset_block_until=date(2025, 9, 1),
            down_reset_p_scale=0.0,
            down_reset_note="未来不下修公告",
        )
        self.bond_history = [
            (date(2025, 1, 31), 101.0),
            (date(2025, 2, 20), 102.0),
        ]

    def get_bond_terms(self, bond_code, valuation_date):
        return self.terms

    def get_stock_close(self, stock_code, on_date):
        return 10.0

    def get_stock_history(self, stock_code, start, end):
        return [(date(2025, 1, 31), 10.0)]

    def get_bond_history(self, bond_code, start, end):
        return [(d, v) for d, v in self.bond_history if start <= d <= end]


class FakeWindHistoricalProvider(FakeHistoricalProvider):
    name = "fake-wind-history"

    def get_admission_status(self, bond_code, valuation_date, base_terms=None):
        return BondTerms(
            suspension_status="交易",
            underlying_status="正常",
            underlying_trade_status="交易",
            bond_turnover_amount=2.5,
            call_status="历史强赎状态",
        )


class FakeFutureEventWindProvider(FakeHistoricalProvider):
    name = "fake-future-event-wind"

    def get_admission_status(self, bond_code, valuation_date, base_terms=None):
        return BondTerms(
            call_status="已公告强赎",
            call_announce_date=None,
            call_redemption_date=date(2025, 6, 30),
            call_redemption_price=100.2,
            last_trading_date=date(2025, 6, 29),
            delisting_date=date(2025, 7, 10),
            bond_turnover_amount=3.0,
        )


def test_terms_patch_store_applies_field_changes_as_of_date(tmp_path):
    path = tmp_path / "cb_terms_patches.json"
    path.write_text(json.dumps({
        "patches": [
            {
                "bond_code": "113001.SH",
                "effective_date": "2025-01-01",
                "field": "conversion_price",
                "value": 10.0,
            },
            {
                "bond_code": "113001.SH",
                "effective_date": "2025-02-10",
                "fields": {"conversion_price": 8.0, "credit_rating": "AA+"},
            },
        ]
    }), encoding="utf-8")

    store = TermsPatchStore(path)
    base = BondTerms(conversion_price=12.0, credit_rating="AA")

    jan = store.apply("113001.SH", base, date(2025, 1, 31))
    feb = store.apply("113001.SH", base, date(2025, 2, 20))

    assert jan.conversion_price == 10.0
    assert jan.credit_rating == "AA"
    assert feb.conversion_price == 8.0
    assert feb.credit_rating == "AA+"


def test_terms_patch_store_add_many_round_trips_metadata(tmp_path):
    store = TermsPatchStore(tmp_path / "patches.json")
    patch = TermsPatch(
        bond_code="113001.SH",
        effective_date=date(2025, 2, 10),
        event_date=date(2025, 2, 8),
        fields={"conversion_price": 8.0},
        before_fields={"conversion_price": 10.0},
        raw_title="关于转股价格调整的公告",
        confidence="parsed",
        source="unit",
    )

    assert store.add_many([patch, patch]) == 1
    reloaded = TermsPatchStore(tmp_path / "patches.json")
    patches = reloaded.list_patches("113001.SH")
    assert len(patches) == 1
    assert patches[0].before_fields == {"conversion_price": 10.0}
    assert patches[0].raw_title == "关于转股价格调整的公告"


def test_project_terms_applies_patches_before_events(tmp_path):
    patch_store = TermsPatchStore(tmp_path / "patches.json")
    patch_store.add_many([
        TermsPatch(
            bond_code="113001.SH",
            effective_date=date(2025, 2, 10),
            fields={"conversion_price": 8.0},
        )
    ])
    event_store = CBEventStore(tmp_path / "events.json")
    event_store.add_many([
        CBEvent(
            bond_code="113001.SH",
            event_date=date(2025, 2, 15),
            event_type="call_no_redemption",
            raw_title="关于不提前赎回的公告",
            effective_end=date(2025, 5, 15),
            parsed_status="不强赎",
        )
    ])

    projection = project_terms(
        "113001.SH",
        BondTerms(conversion_price=10.0),
        date(2025, 2, 20),
        patch_store=patch_store,
        event_store=event_store,
    )

    assert projection.terms.conversion_price == 8.0
    assert projection.terms.call_status == "不强赎"
    assert projection.terms.call_no_redemption_until == date(2025, 5, 15)
    assert projection.patch_fields == frozenset({"conversion_price"})


def test_historical_provider_strips_current_status_and_applies_events_and_patches(tmp_path):
    patch_path = tmp_path / "patches.json"
    patch_path.write_text(json.dumps({
        "patches": [
            {
                "bond_code": "113001.SH",
                "effective_date": "2025-01-01",
                "field": "conversion_price",
                "value": 10.0,
            },
            {
                "bond_code": "113001.SH",
                "effective_date": "2025-02-10",
                "field": "conversion_price",
                "value": 8.0,
            },
        ]
    }), encoding="utf-8")
    event_store = CBEventStore(tmp_path / "events.json")
    event_store.add_many([
        CBEvent(
            bond_code="113001.SH",
            event_date=date(2025, 2, 15),
            event_type="call_redemption",
            raw_title="关于实施赎回暨摘牌的公告",
            effective_end=date(2025, 3, 10),
            parsed_status="已公告强赎",
        ),
    ])

    provider = HistoricalBondDataProvider(
        FakeHistoricalProvider(),
        patch_store=TermsPatchStore(patch_path),
        event_store=event_store,
    )

    before_call = provider.get_bond_terms("113001.SH", date(2025, 1, 31))
    after_call = provider.get_bond_terms("113001.SH", date(2025, 2, 20))

    assert before_call.conversion_price == 10.0
    assert before_call.call_status is None
    assert before_call.delisting_date is None
    assert before_call.underlying_status is None
    assert before_call.down_reset_block_until is None
    assert before_call.close == 101.0
    assert after_call.conversion_price == 8.0
    assert after_call.call_status == "已公告强赎"
    assert after_call.call_announce_date == date(2025, 2, 15)
    assert after_call.call_redemption_date == date(2025, 3, 10)
    assert after_call.close == 102.0


def test_historical_provider_prefers_snapshot_before_current_bundle(tmp_path):
    history_dir = tmp_path / "cb_data_history"
    history_dir.mkdir()
    snapshot = TermsBundle(history_dir / "2025-01-31.json")
    snapshot.set(
        "113001.SH",
        BondTerms(
            sec_name="历史转债",
            underlying_code="600001.SH",
            conversion_price=11.0,
            call_status=None,
        ),
        source="unit",
    )

    provider = HistoricalBondDataProvider(
        FakeHistoricalProvider(),
        history_store=TermsHistoryStore(history_dir),
        patch_store=TermsPatchStore(tmp_path / "missing_patches.json"),
        event_store=CBEventStore(tmp_path / "events.json"),
    )

    terms = provider.get_bond_terms("113001.SH", date(2025, 2, 1))

    assert terms.sec_name == "历史转债"
    assert terms.conversion_price == 11.0


def test_historical_provider_can_merge_wind_admission_status(tmp_path):
    provider = HistoricalBondDataProvider(
        FakeWindHistoricalProvider(),
        patch_store=TermsPatchStore(tmp_path / "missing_patches.json"),
        event_store=CBEventStore(tmp_path / "events.json"),
        strip_fallback_status=False,
        merge_admission_status=True,
    )

    terms = provider.get_bond_terms("113001.SH", date(2025, 2, 20))
    diag = provider.get_terms_source_diagnostics("113001.SH", date(2025, 2, 20))

    assert terms.conversion_price == 8.0
    assert terms.suspension_status == "交易"
    assert terms.underlying_status == "正常"
    assert terms.underlying_trade_status == "交易"
    assert terms.bond_turnover_amount == 2.5
    assert diag["terms_source"] == "provider_history"
    assert diag["uses_current_fallback"] is False
    assert diag["merge_admission_status"] is True


def test_historical_provider_strips_unannounced_future_wind_status(tmp_path):
    provider = HistoricalBondDataProvider(
        FakeFutureEventWindProvider(),
        patch_store=TermsPatchStore(tmp_path / "missing_patches.json"),
        event_store=CBEventStore(tmp_path / "events.json"),
        strip_fallback_status=False,
        merge_admission_status=True,
    )

    before = provider.get_bond_terms("113001.SH", date(2025, 1, 31))
    after = provider.get_bond_terms("113001.SH", date(2025, 7, 11))

    assert before.call_status is None
    assert before.call_redemption_date is None
    assert before.call_redemption_price is None
    assert before.last_trading_date is None
    assert before.delisting_date is None
    assert before.bond_turnover_amount == 3.0
    assert after.call_status == "已公告强赎"
    assert after.call_redemption_date == date(2025, 6, 30)
    assert after.last_trading_date == date(2025, 6, 29)
    assert after.delisting_date == date(2025, 7, 10)


def test_historical_provider_reports_terms_source_diagnostics(tmp_path):
    history_dir = tmp_path / "cb_data_history"
    history_dir.mkdir()
    snapshot = TermsBundle(history_dir / "2025-01-31.json")
    snapshot.set(
        "113001.SH",
        BondTerms(sec_name="历史转债", underlying_code="600001.SH", conversion_price=11.0),
        source="unit",
    )
    patch_store = TermsPatchStore(tmp_path / "patches.json")
    patch_store.add_many([
        TermsPatch(
            bond_code="113001.SH",
            effective_date=date(2025, 2, 1),
            fields={"conversion_price": 10.5},
        )
    ])
    event_store = CBEventStore(tmp_path / "events.json")
    event_store.add_many([
        CBEvent(
            bond_code="113001.SH",
            event_date=date(2025, 2, 2),
            event_type="call_no_redemption",
            raw_title="关于不提前赎回的公告",
            effective_end=date(2025, 5, 2),
            parsed_status="不强赎",
        )
    ])
    provider = HistoricalBondDataProvider(
        FakeHistoricalProvider(),
        history_store=TermsHistoryStore(history_dir),
        patch_store=patch_store,
        event_store=event_store,
    )

    with_snapshot = provider.get_terms_source_diagnostics("113001.SH", date(2025, 2, 3))
    no_snapshot = provider.get_terms_source_diagnostics("113001.SH", date(2025, 1, 1))

    assert with_snapshot["terms_source"] == "history_snapshot"
    assert with_snapshot["snapshot_date"] == date(2025, 1, 31)
    assert with_snapshot["patch_count"] == 1
    assert with_snapshot["event_count"] == 1
    assert with_snapshot["uses_current_fallback"] is False
    assert no_snapshot["terms_source"] == "current_fallback"
    assert no_snapshot["uses_current_fallback"] is True


def test_future_down_reset_override_is_ignored_for_historical_date(tmp_path):
    path = tmp_path / "down_reset_overrides.json"
    path.write_text(json.dumps({
        "113001.SH": {
            "announce_date": "2025-04-13",
            "p_scale_after_cooldown": 0.3,
            "note": "未来公告",
        }
    }), encoding="utf-8")
    overrides = DownResetOverrides(path)
    terms = BondTerms(down_reset_cooldown_months=6)

    early = resolve_down_reset(
        "113001.SH",
        terms,
        overrides,
        valuation_date=date(2025, 4, 1),
    )
    later = resolve_down_reset(
        "113001.SH",
        terms,
        overrides,
        valuation_date=date(2025, 4, 20),
    )

    assert early.announce_date is None
    assert early.block_until is None
    assert early.p_scale is None
    assert later.announce_date == date(2025, 4, 13)
    assert later.block_until == date(2025, 10, 13)
    assert later.p_scale == 0.3


def test_resolve_down_reset_intensity_applies_background_scale():
    """背景态: effective_p_down = base · p_scale."""
    resolved = ResolvedDownReset(
        block_until=None,
        p_scale=0.5,
        note=None,
        cooldown_months=None,
        announce_date=None,
    )

    intensity = resolve_down_reset_intensity(0.15, resolved)
    assert intensity.base_p_down == 0.15
    assert intensity.effective_p_down == pytest.approx(0.075)
    assert intensity.p_scale == 0.5
    assert intensity.scheduled_reset_date is None
    assert intensity.scheduled_reset_prob == 0.0

    redemption = resolve_down_reset_intensity(
        0.15, resolved, redemption_mode=True)
    assert redemption.effective_p_down == 0.0

    override = resolve_down_reset_intensity(
        0.15, resolved, p_scale_override=0.2)
    assert override.effective_p_down == pytest.approx(0.03)


def test_resolve_down_reset_intensity_schedules_node_for_proposal():
    """已提议态: 不抬升背景强度, 改输出一次性下修节点 (提议日 + 滞后, 通过率)。"""
    from datetime import date, timedelta
    from convertible_bond.down_reset_overrides import (
        PROPOSED_EFFECTIVE_LAG_DAYS,
        PROPOSED_PASS_PROB,
    )

    resolved = ResolvedDownReset(
        block_until=None,
        p_scale=None,
        note=None,
        cooldown_months=None,
        announce_date=None,
        proposal_date=date(2025, 8, 1),
    )

    intensity = resolve_down_reset_intensity(0.15, resolved)
    # 背景强度保持 base, 未被提议放大
    assert intensity.effective_p_down == pytest.approx(0.15)
    assert intensity.scheduled_reset_date == date(2025, 8, 1) + timedelta(
        days=PROPOSED_EFFECTIVE_LAG_DAYS)
    assert intensity.scheduled_reset_prob == pytest.approx(PROPOSED_PASS_PROB)

    # 强赎模式下提议节点也归零
    redemption = resolve_down_reset_intensity(0.15, resolved, redemption_mode=True)
    assert redemption.scheduled_reset_date is None
    assert redemption.scheduled_reset_prob == 0.0


def test_resolve_down_reset_intensity_schedules_node_for_approved_pending():
    """已通过待生效: 节点用生效日 + 通过率≈1, kind=approved, 优先于已提议。"""
    from datetime import date
    from convertible_bond.down_reset_overrides import APPROVED_PASS_PROB

    resolved = ResolvedDownReset(
        block_until=None,
        p_scale=None,
        note=None,
        cooldown_months=None,
        announce_date=None,
        proposal_date=date(2025, 8, 1),               # 同券更早的提议
        approved_date=date(2025, 8, 20),
        approved_effective_date=date(2025, 8, 27),     # 生效日 (未来)
    )

    intensity = resolve_down_reset_intensity(0.15, resolved)
    assert intensity.scheduled_reset_kind == "approved"
    assert intensity.scheduled_reset_date == date(2025, 8, 27)
    assert intensity.scheduled_reset_prob == pytest.approx(APPROVED_PASS_PROB)


def test_resolve_down_reset_intensity_passes_announced_new_k():
    """公告解析到的新 K 应透传成 scheduled_reset_target_k; 缺失时为 None。"""
    from datetime import date

    with_k = ResolvedDownReset(
        block_until=None, p_scale=None, note=None, cooldown_months=None,
        announce_date=None, proposal_date=date(2025, 8, 1), announced_new_k=6.2,
    )
    assert resolve_down_reset_intensity(0.15, with_k).scheduled_reset_target_k == pytest.approx(6.2)

    without_k = ResolvedDownReset(
        block_until=None, p_scale=None, note=None, cooldown_months=None,
        announce_date=None, proposal_date=date(2025, 8, 1),
    )
    assert resolve_down_reset_intensity(0.15, without_k).scheduled_reset_target_k is None
