import json
from dataclasses import replace
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


def test_historical_provider_reports_explicit_provider_history_terms(tmp_path):
    provider = HistoricalBondDataProvider(
        FakeHistoricalProvider(),
        patch_store=TermsPatchStore(tmp_path / "missing_patches.json"),
        event_store=CBEventStore(tmp_path / "events.json"),
        strip_fallback_status=True,
        merge_admission_status=False,
        provider_history_terms=True,
    )

    terms = provider.get_bond_terms("113001.SH", date(2025, 2, 20))
    diag = provider.get_terms_source_diagnostics("113001.SH", date(2025, 2, 20))

    assert terms.conversion_price == 8.0
    assert terms.call_status is None
    assert terms.delisting_date is None
    assert diag["terms_source"] == "provider_history"
    assert diag["uses_current_fallback"] is False
    assert diag["merge_admission_status"] is False


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


def test_resolve_down_reset_intensity_rejects_upward_target_k():
    """公告解析出的新 K 高于现 K → 方向不可能, 必须丢掉而不是原样透传。

    下修公告正文开头会成段引用"历次转股价格调整情况", 而 parse_down_reset_new_price 取的是
    **第一个**"由 A 元/股 修正为 B 元/股", 于是抓到几年前那次调整的 B。实测全库 147 条带
    event_price 的下修事件里 106 条 (72%) 新 K 严格高于当时的 K —— 例如强力转债 2026-08-07
    提议公告正文依次出现 18.98/18.98/18.94/18.94/18.90/12.70, 解析结果 18.94 (2021 年的值),
    而当时 K 已经是 12.70。

    这个错值不会算出错价 (pricer 的节点是 max(V, reset_value), 偏高的 target_k 只会让
    reset_value 低于 V), 但会让节点**静默变 no-op** —— 下修价值被整只抹平。
    """
    from datetime import date

    resolved = ResolvedDownReset(
        block_until=None, p_scale=None, note=None, cooldown_months=None,
        announce_date=None, proposal_date=date(2026, 8, 7), announced_new_k=18.94,
    )
    bogus = resolve_down_reset_intensity(0.15, resolved, current_k=12.70)
    assert bogus.scheduled_reset_target_k is None      # 丢掉 → pricer 回落 premium/floor 估算
    assert bogus.scheduled_reset_date is not None      # 但节点本身还在: 下修确实被提议了
    assert bogus.scheduled_reset_kind == "proposed"

    # 不给 current_k 时不做校验 (老调用方保持原行为)
    assert resolve_down_reset_intensity(0.15, resolved).scheduled_reset_target_k == pytest.approx(18.94)


def test_resolve_down_reset_intensity_keeps_target_k_equal_to_current():
    """等于现 K 的必须留着: 那是"下修已落地、条款已刷新"的正常状态。

    pricer 靠 target_k == K 让节点退化成恒等映射 (no-op) 来防双计。把它一起拦掉会改用
    premium/floor 估算, 反而把已经落地的下修**再算一遍**。
    """
    from datetime import date

    landed = ResolvedDownReset(
        block_until=None, p_scale=None, note=None, cooldown_months=None,
        announce_date=None, proposal_date=date(2026, 8, 7), announced_new_k=12.70,
    )
    assert resolve_down_reset_intensity(
        0.15, landed, current_k=12.70).scheduled_reset_target_k == pytest.approx(12.70)

    lower = ResolvedDownReset(
        block_until=None, p_scale=None, note=None, cooldown_months=None,
        announce_date=None, proposal_date=date(2026, 8, 7), announced_new_k=10.60,
    )
    assert resolve_down_reset_intensity(
        0.15, lower, current_k=12.70).scheduled_reset_target_k == pytest.approx(10.60)


def test_terms_patch_store_rewrite_edits_and_drops(tmp_path):
    """rewrite: 逐条改写已有 patch (修数据用), 返回 None 即删除该条。"""
    store = TermsPatchStore(tmp_path / "patches.json")
    store.add_many([
        TermsPatch(bond_code="113001.SH", effective_date=date(2025, 2, 10),
                   fields={"outstanding_balance": 0.3, "call_redemption_price": 100.5}),
        TermsPatch(bond_code="113002.SH", effective_date=date(2025, 2, 11),
                   fields={"outstanding_balance": 0.3}),
        TermsPatch(bond_code="113003.SH", effective_date=date(2025, 2, 12),
                   fields={"conversion_price": 8.0}),
    ])

    def drop_balance(patch):
        if "outstanding_balance" not in patch.fields:
            return patch
        fields = {k: v for k, v in patch.fields.items() if k != "outstanding_balance"}
        return replace(patch, fields=fields) if fields else None

    # dry_run 只统计不落盘
    assert store.rewrite(drop_balance, dry_run=True) == (1, 1)
    assert len(TermsPatchStore(tmp_path / "patches.json").list_patches()) == 3

    assert store.rewrite(drop_balance) == (1, 1)
    reloaded = TermsPatchStore(tmp_path / "patches.json")
    assert reloaded.list_patches("113001.SH")[0].fields == {"call_redemption_price": 100.5}
    assert reloaded.list_patches("113002.SH") == []
    assert reloaded.list_patches("113003.SH")[0].fields == {"conversion_price": 8.0}
    # 幂等: 再跑一次没有可改的
    assert reloaded.rewrite(drop_balance) == (0, 0)


def test_project_terms_skips_patches_already_baked_into_the_snapshot(tmp_path):
    """terms_as_of = 基础条款快照的截止日; 更早生效的 patch 不再重复套用。

    cb_data 的 conversion_price 是 Wind 的**当前 K**, 已内含全部已生效下修。不带这个锚,
    今日估值会把两年前的下修 patch 重新盖回去 —— 实测主池 60% 的 K 因此被写坏
    (万孚转债 20.88 → 93.57, 转股价值随之算错)。
    """
    store = TermsPatchStore(tmp_path / "patches.json")
    store.add_many([
        TermsPatch(bond_code="123064.SZ", effective_date=date(2024, 8, 22),
                   fields={"conversion_price": 93.57}),
        TermsPatch(bond_code="123064.SZ", effective_date=date(2026, 6, 2),
                   fields={"conversion_price": 93.57}),
    ])
    events = CBEventStore(tmp_path / "events.json")
    terms = BondTerms(sec_name="万孚转债", conversion_price=20.88)
    val = date(2026, 8, 22)

    # 无锚: 旧行为, 历史 patch 把当前 K 盖掉
    stale = project_terms("123064.SZ", terms, val,
                          patch_store=store, event_store=events).terms
    assert stale.conversion_price == pytest.approx(93.57)

    # 有锚 (快照抓于 2026-08-22): 两条 patch 都在快照之前生效 → 全部跳过
    fixed = project_terms("123064.SZ", terms, val, patch_store=store,
                          event_store=events, terms_as_of=date(2026, 8, 22)).terms
    assert fixed.conversion_price == pytest.approx(20.88)
    assert not fixed.conversion_price == stale.conversion_price


def test_project_terms_still_applies_patches_effective_after_the_snapshot(tmp_path):
    """回测口径: 快照之后生效的 patch 仍然要套用, 否则历史条款就停在快照那天。"""
    store = TermsPatchStore(tmp_path / "patches.json")
    store.add_many([
        TermsPatch(bond_code="123064.SZ", effective_date=date(2026, 3, 1),
                   fields={"conversion_price": 27.00}),
        TermsPatch(bond_code="123064.SZ", effective_date=date(2026, 6, 2),
                   fields={"conversion_price": 20.88}),
    ])
    events = CBEventStore(tmp_path / "events.json")
    terms = BondTerms(sec_name="万孚转债", conversion_price=27.00)
    got = project_terms("123064.SZ", terms, date(2026, 8, 22), patch_store=store,
                        event_store=events, terms_as_of=date(2026, 3, 1)).terms
    assert got.conversion_price == pytest.approx(20.88)


def test_terms_patch_store_list_patches_after_filter(tmp_path):
    store = TermsPatchStore(tmp_path / "patches.json")
    store.add_many([
        TermsPatch(bond_code="X.SZ", effective_date=date(2025, 1, 1), fields={"conversion_price": 1.0}),
        TermsPatch(bond_code="X.SZ", effective_date=date(2026, 1, 1), fields={"conversion_price": 2.0}),
    ])
    assert len(store.list_patches("X.SZ")) == 2
    assert [p.effective_date for p in store.list_patches("X.SZ", after=date(2025, 6, 1))] == [date(2026, 1, 1)]
    assert store.list_patches("X.SZ", after=date(2026, 1, 1)) == []


def test_historical_provider_trusts_inner_as_of_over_patches(tmp_path):
    """inner 若是真 as-of 数据源 (Wind), 就不该再用历史 patch 盖它的当日条款。

    实测 123064.SZ 万孚转债: Wind as-of 2025-06-30 → K=26.60 (与公告沿革吻合),
    而套用 effective_date <= 该日的 patch 后被盖成 93.57。条款 patch 是给**没有 as-of
    能力**的数据源 (akshare/CSV) 重建历史用的, 不是给 Wind 用的。
    """
    patches = TermsPatchStore(tmp_path / "patches.json")
    patches.add_many([
        TermsPatch(bond_code="123064.SZ", effective_date=date(2024, 8, 22),
                   fields={"conversion_price": 93.57}),
    ])
    events = CBEventStore(tmp_path / "events.json")
    val = date(2025, 6, 30)

    class AsOfInner(DataProvider):
        name = "asof"
        def get_bond_terms(self, bond_code, valuation_date):
            return BondTerms(sec_name="万孚转债", conversion_price=26.60)
        def terms_as_of(self, bond_code, valuation_date):
            return valuation_date          # 真 as-of
        def get_stock_close(self, c, d): return 1.0
        def get_stock_history(self, c, s, e): return []
        def get_bond_history(self, c, s, e): return []

    class TodayOnlyInner(AsOfInner):
        name = "today_only"
        def terms_as_of(self, bond_code, valuation_date):
            return None                    # 没有 as-of 能力

    def k_for(inner):
        provider = HistoricalBondDataProvider(
            inner, history_store=None, patch_store=patches, event_store=events)
        return provider.get_bond_terms("123064.SZ", val).conversion_price

    assert k_for(AsOfInner()) == pytest.approx(26.60)      # patch 被跳过
    assert k_for(TodayOnlyInner()) == pytest.approx(93.57)  # 老路: 套 patch 重建历史


def test_wind_provider_reports_valuation_date_as_terms_anchor():
    from convertible_bond.data_providers.wind import WindDataProvider
    assert WindDataProvider.terms_as_of(
        object(), "123064.SZ", date(2025, 6, 30)) == date(2025, 6, 30)


def test_authoritative_patches_shadow_parsed_ones_per_field(tmp_path):
    """同一字段有权威源 (Wind as-of) 时, 完全忽略公告解析源 —— 否则一条日期更晚的
    解析错值会盖掉正确值。

    实测: cb-sync-events 为 127112.SZ 尚太转债 写入 2026-07-03 K=84.72,
    而 Wind as-of 的真值是 60.02; 按生效日取最后一条就取到了错的那条。
    """
    store = TermsPatchStore(tmp_path / "patches.json")
    store.add_many([
        TermsPatch(bond_code="127112.SZ", effective_date=date(2026, 6, 30),
                   fields={"conversion_price": 60.02}, source="wind_asof"),
        TermsPatch(bond_code="127112.SZ", effective_date=date(2026, 7, 3),
                   fields={"conversion_price": 84.72}, source="cninfo"),
        # 权威源没覆盖的字段, 解析源照常生效
        TermsPatch(bond_code="127112.SZ", effective_date=date(2026, 7, 5),
                   fields={"credit_rating": "AA"}, source="cninfo"),
    ])

    terms = store.apply("127112.SZ", BondTerms(conversion_price=99.0),
                        date(2026, 8, 22))
    assert terms.conversion_price == pytest.approx(60.02)   # 权威值胜出
    assert terms.credit_rating == "AA"                      # 未被权威源覆盖的字段保留


def test_parsed_patches_still_apply_without_authoritative_source(tmp_path):
    """没有 Wind 权威源时 (akshare/CSV 口径), 解析源仍是唯一的历史条款来源。"""
    store = TermsPatchStore(tmp_path / "patches.json")
    store.add_many([
        TermsPatch(bond_code="127112.SZ", effective_date=date(2026, 7, 3),
                   fields={"conversion_price": 84.72}, source="cninfo"),
    ])
    terms = store.apply("127112.SZ", BondTerms(conversion_price=99.0), date(2026, 8, 22))
    assert terms.conversion_price == pytest.approx(84.72)


def test_project_terms_cuts_per_field_not_wholesale(tmp_path):
    """``terms_as_of`` 的裁剪要**逐字段**判 —— 快照覆盖不到的字段不能裁。

    cb_data 里 credit_rating_outlook / credit_watch_status 永远是空的 (Wind 的
    ratingoutlook 实测取不到, CBEvent 也不带这两个字段), 公告解析写进 patch 库是唯一来源。
    对它们"快照已含更早的变更"根本不成立, 照常裁剪的后果是字段永远进不了定价视图 ——
    实测 patch 库里躺着 986 条展望, 而主池 265/285 只债的展望在 live 路径上被整段丢掉。

    而 conversion_price 必须继续裁: cb_data 的 K 是 Wind 当前值, 是两者冲突时的权威。
    """
    from datetime import date

    from convertible_bond.data_providers import BondTerms
    from convertible_bond.historical_terms import TermsPatch, TermsPatchStore, project_terms

    store = TermsPatchStore(tmp_path / "patches.json")
    store.add_many([
        TermsPatch(bond_code="113001.SH", effective_date=date(2026, 6, 13),
                   fields={"credit_rating_outlook": "负面",
                           "conversion_price": 99.9},   # ← 陈旧的解析值, 必须被裁掉
                   source="cninfo"),
    ])
    terms = BondTerms(sec_name="测试转债", conversion_price=12.34)
    projected = project_terms(
        "113001.SH", terms, date(2026, 8, 25),
        patch_store=store, apply_events=False,
        terms_as_of=date(2026, 8, 20),      # patch 生效日更早 → 常规字段要被裁
    ).terms

    assert projected.conversion_price == 12.34        # 快照说了算
    assert projected.credit_rating_outlook == "负面"   # 快照里没有, 不能裁


def test_project_terms_still_lets_snapshot_win_on_rating(tmp_path):
    """credit_rating **不在**豁免集: cb_data 的评级由第三方同步驱动, 比公告解析准。

    实测拿 akshare 当裁判, 体检标记的 17 条分歧里 15 条是公告 patch 错、cb_data 对。
    """
    from datetime import date

    from convertible_bond.data_providers import BondTerms
    from convertible_bond.historical_terms import TermsPatch, TermsPatchStore, project_terms

    store = TermsPatchStore(tmp_path / "patches.json")
    store.add_many([
        TermsPatch(bond_code="113001.SH", effective_date=date(2026, 5, 19),
                   fields={"credit_rating": "A"}, source="cninfo"),
    ])
    projected = project_terms(
        "113001.SH", BondTerms(sec_name="精工转债", credit_rating="AA"),
        date(2026, 8, 25), patch_store=store, apply_events=False,
        terms_as_of=date(2026, 8, 20),
    ).terms
    assert projected.credit_rating == "AA"


def test_strip_removes_the_underlying_name_so_st_cannot_leak_backwards():
    """正股**名字**也必须剥 —— 否则 ST 判定会读到未来。

    ``_underlying_has_st_risk`` 判的是 ``f"{underlying_name} {underlying_status}"``,
    而 cb_data 里的 ``underlying_name`` 是**今天**的名字。只剥 status 不剥 name, 等于让
    2022 年的回测从「*ST闻泰」这四个字里读出"这家公司 2026 年会被 ST"——
    策略于是能提前躲开后来暴雷的债, 而等权基准照单全收, 超额被单边抬高。

    代价实测为零: 四只当前 ST 债 (110081/110092/118027/127093) **都有**
    ``underlying_st_risk`` 事件, 事件按估值日重放 —— 剥掉名字后它们照样被识别,
    只是从**公告日**起而不是从回测第一天起。
    """
    from datetime import date

    from convertible_bond import batch_pricing
    from convertible_bond.data_providers import BondTerms
    from convertible_bond.historical_terms import strip_current_status_fields

    terms = BondTerms(sec_name="闻泰转债", underlying_name="*ST闻泰",
                      underlying_status="是", conversion_price=10.0,
                      maturity_date=date(2030, 1, 1))

    stripped = strip_current_status_fields(terms)
    assert stripped.underlying_name is None
    assert stripped.underlying_status is None
    # 光剥 status 是不够的 —— 名字里就带着答案
    assert batch_pricing._underlying_has_st_risk(stripped) is False


def test_st_is_recognised_only_from_its_event_date_onward(tmp_path):
    """剥掉名字之后, ST 由**事件**重建 —— 公告日之前不认, 之后认。"""
    from datetime import date

    from convertible_bond import batch_pricing
    from convertible_bond.cb_events import CBEvent, CBEventStore, apply_events_to_terms
    from convertible_bond.data_providers import BondTerms
    from convertible_bond.historical_terms import strip_current_status_fields

    store = CBEventStore(tmp_path / "events.json")
    store.add_many([CBEvent(
        bond_code="110081.SH", event_date=date(2026, 4, 30),
        event_type="underlying_st_risk", parsed_status="ST/退市风险",
        raw_title="关于公司股票被实施退市风险警示的公告",
    )])

    base = strip_current_status_fields(BondTerms(
        sec_name="闻泰转债", underlying_name="*ST闻泰", underlying_status="是",
        conversion_price=10.0, maturity_date=date(2030, 1, 1)))

    def st_at(on_date):
        patched = apply_events_to_terms(
            "110081.SH", base,
            store.list_events(bond_code="110081.SH", through_date=on_date),
            valuation_date=on_date)
        return batch_pricing._underlying_has_st_risk(patched)

    assert st_at(date(2022, 6, 30)) is False      # 公告前四年 —— 不该知道
    assert st_at(date(2026, 4, 29)) is False      # 公告前一天
    assert st_at(date(2026, 8, 31)) is True       # 公告后
