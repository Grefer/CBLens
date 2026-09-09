import errno
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from threading import Barrier, Lock

import pytest

import convertible_bond.atomic_io as atomic_io
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


@pytest.mark.parametrize("inject_contention", [False, True], ids=["normal", "single-winerror5"])
def test_patch_store_concurrent_saves_publish_complete_json(tmp_path, monkeypatch, inject_contention):
    """每次写操作只同步一次; 重试仍能发布某一份完整快照。"""
    path = tmp_path / "patches.json"
    stores = [TermsPatchStore(path), TermsPatchStore(path)]
    patches = [TermsPatch("A", date(2026, 1, 1), {"conversion_price": 9}, note="甲"),
               TermsPatch("B", date(2026, 1, 2), {"conversion_price": 8}, note="乙" * 20000)]
    barrier = Barrier(2)
    lock = Lock()
    temporary_paths = []
    attempts = {}
    injected_paths = []
    real_publish = atomic_io._replace_with_retry
    real_replace = Path.replace

    def publish_together(src, dst):
        with lock:
            temporary_paths.append(src)
        # 屏障放在整次发布操作的入口。若挂在 Path.replace 上, Windows 重试时另一
        # writer 可能已完成, 第二次进入屏障就会永远等不到它。
        barrier.wait(timeout=5)
        return real_publish(src, dst)

    def replace_with_contention(src, dst):
        with lock:
            attempts[src] = attempts.get(src, 0) + 1
            if inject_contention and not injected_paths:
                injected_paths.append(src)
                failure = PermissionError(errno.EACCES, "模拟一次 Windows 目标句柄占用")
                failure.winerror = 5
                raise failure
        return real_replace(src, dst)

    monkeypatch.setattr(atomic_io, "_replace_with_retry", publish_together)
    monkeypatch.setattr(Path, "replace", replace_with_contention)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(store.add_many, [patch])
                   for store, patch in zip(stores, patches)]
        for future in futures:
            future.result(timeout=10)

    assert len(temporary_paths) == len(set(temporary_paths)) == 2
    assert all(p.parent == path.parent for p in temporary_paths)
    assert set(attempts) == set(temporary_paths)
    assert len(injected_paths) == int(inject_contention)
    if inject_contention:
        assert attempts[injected_paths[0]] >= 2, "注入的占用必须走真实重试链恢复"
    reloaded = TermsPatchStore(path)
    assert reloaded.list_patches() in ([patches[0]], [patches[1]])
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert path.read_text(encoding="utf-8") == json.dumps(
        payload, ensure_ascii=False, indent=2, sort_keys=True)
    assert list(tmp_path.iterdir()) == [path]


def test_patch_store_failed_publish_preserves_previous_json(tmp_path, monkeypatch):
    path = tmp_path / "patches.json"
    store = TermsPatchStore(path)
    store.add_many([TermsPatch("A", date(2026, 1, 1), {"conversion_price": 9})])
    before = path.read_bytes()

    def fail_replace(src, dst):
        raise OSError("模拟发布失败")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="模拟发布失败"):
        store.add_many([TermsPatch("B", date(2026, 1, 2), {"conversion_price": 8})])
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


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


def test_patch_shadowing_is_scoped_to_one_bond(tmp_path):
    """遮蔽必须**逐债逐字段**算, 不能拿 A 债的权威 patch 去遮 B 债的解析 patch。

    ``_drop_shadowed_patches`` 的权威字段集此前是在**整个传进来的列表**上算的。
    ``list_patches(bond_code=...)`` 传单债列表所以投影路径 (``apply()``) 没事, 但
    全库调用 (数据体检、存量回洗) 传的是所有债 —— 于是 A 债的一条 wind_asof patch
    会遮蔽 B 债该字段的解析 patch, 而 B 债在那个字段上可能根本没有权威源。
    实测差 2 条 (逐债累计 22724 vs 全库一次 22722): 今天很小, 但方向是"越修越看不见"。
    """
    from datetime import date

    from convertible_bond.historical_terms import TermsPatch, TermsPatchStore

    store = TermsPatchStore(tmp_path / "patches.json")
    store.add_many([
        # A 债: conversion_price 有权威源
        TermsPatch(bond_code="A.SZ", effective_date=date(2026, 1, 1),
                   fields={"conversion_price": 10.0}, source="wind_asof"),
        TermsPatch(bond_code="A.SZ", effective_date=date(2026, 2, 1),
                   fields={"conversion_price": 9.0}, source="cninfo"),
        # B 债: 同一个字段**只有**解析源 —— 不该被 A 债连累
        TermsPatch(bond_code="B.SZ", effective_date=date(2026, 2, 1),
                   fields={"conversion_price": 8.0}, source="cninfo"),
    ])

    whole = store.list_patches()
    b_rows = [p for p in whole if p.bond_code == "B.SZ"]
    assert b_rows and "conversion_price" in (b_rows[0].fields or {}), (
        "B 债的解析 patch 被 A 债的权威 patch 遮蔽了")

    # A 债自己的解析 patch 照旧被遮蔽掉
    a_parsed = [p for p in whole if p.bond_code == "A.SZ" and p.source == "cninfo"]
    assert not a_parsed, "同一只债的遮蔽失效了"

    # 全库一次调用与逐债累计必须给出同一个结果
    per_bond = sum(len(store.list_patches(bond_code=c)) for c in ("A.SZ", "B.SZ"))
    assert per_bond == len(whole)


def _real_stores():
    from convertible_bond.cache import TermsBundle, project_bundle_path
    from convertible_bond.historical_terms import (
        TermsPatchStore,
        project_terms_patches_path,
    )
    return TermsBundle(project_bundle_path()), TermsPatchStore(project_terms_patches_path())


def test_snapshot_anchor_is_clamped_to_the_valuation_date():
    """锚落在**估值日之后**时必须作废 —— 它什么也证明不了。

    ``terms_as_of`` 的全部含义是"基础条款是 X 日拍的快照, X 之前的 patch 它已经含着了"。
    这句话只在 ``X <= 估值日`` 时成立。而 ``CachedBondDataProvider.terms_as_of`` 返回的是
    cb_data 的条款抓取日, **与估值日无关** —— 实测对 2024-01-02 和 2022-06-01 都返回
    2026-08-30。没有 Wind 的机器 (``标准`` 口径回测) 走的正是这条链, 于是历史 patch 被
    整条裁光: 实测估值日 2024-01-02, 766 只在市债里 **508 只 K 是错的、270 只偏离超过
    10%** (山鹰转债真值 2.37 → 视图 1.54), 也就是 2024 年的回测"知道"2025/2026 才发生
    的下修。没有异常, ``get_terms_source_diagnostics`` 还报 uses_current_fallback=False。
    """
    from datetime import date

    from convertible_bond.historical_terms import clamp_snapshot_anchor

    val = date(2024, 1, 2)
    assert clamp_snapshot_anchor(date(2026, 8, 30), val) is None, "未来的锚没被作废"
    assert clamp_snapshot_anchor(date(2023, 6, 1), val) == date(2023, 6, 1)
    assert clamp_snapshot_anchor(val, val) == val, "锚正好等于估值日时仍然有效"
    assert clamp_snapshot_anchor(None, val) is None


def test_historical_view_reproduces_the_as_of_terms_on_the_real_library():
    """在真实条款库上, 历史视角的 K 与余额必须等于 wind_asof 链给出的真值。

    真值定义: 生效日 <= 估值日的最后一条 wind_asof patch; 一条都没有时取链头的
    ``before_fields`` (那正是变更前的值, 实测 17113/17113 条余额 patch 都带着它)。

    改动前: K 508/766 只不符 (>10% 的 270 只), 而 ``outstanding_balance`` 有 213 只
    显示 <0.5 亿 而真值 >=0.5 亿 —— 回测里它们会被 ``min_outstanding_balance`` 当成
    "余额过小"整批剔掉 (立昂转债 2024 年真实余额 33.9 亿, 视图里是 0)。
    """
    from datetime import date

    from convertible_bond.cache import CachedBondDataProvider
    from convertible_bond.cb_events import CBEventStore, project_events_path
    from convertible_bond.data_providers.base import DataProvider
    from convertible_bond.historical_terms import HistoricalBondDataProvider

    bundle, store = _real_stores()
    val = date(2024, 1, 2)

    class _Inner(DataProvider):
        name = "akshare"

        def get_bond_terms(self, code, d):
            return bundle.get(code)

        def get_stock_close(self, *a, **k):
            return None

        def get_stock_history(self, *a, **k):
            return []

        def get_bond_history(self, *a, **k):
            return []

        def hist_vol(self, *a, **k):
            return 0.2

        def get_risk_free_rate(self, *a, **k):
            return 0.022

        def get_admission_status(self, code, d, base_terms=None):
            return None

    # 与 gui/controllers/strategy_run.py 的「标准」口径同配置
    provider = HistoricalBondDataProvider(
        CachedBondDataProvider(_Inner(), bundle, static_source=_Inner()),
        patch_store=store, event_store=CBEventStore(project_events_path()),
        history_store=None, strip_fallback_status=False, merge_admission_status=True)

    def truth(code, field):
        chain = sorted(
            (p for p in store.list_patches(bond_code=code, include_shadowed=True)
             if p.source == "wind_asof" and field in (p.fields or {})),
            key=lambda p: p.effective_date)
        if not chain:
            return None
        earlier = [p for p in chain if p.effective_date <= val]
        if earlier:
            return float(earlier[-1].fields[field])
        before = chain[0].before_fields or {}
        return float(before[field]) if field in before else None

    for field in ("conversion_price", "outstanding_balance"):
        wrong, checked = [], 0
        for code in bundle.list_bonds():
            terms = bundle.get(code)
            listing = getattr(terms, "listing_date", None)
            maturity = getattr(terms, "maturity_date", None)
            if not (listing and maturity and listing <= val <= maturity):
                continue
            expected = truth(code, field)
            if expected is None:
                continue
            checked += 1
            got = getattr(provider.get_bond_terms(code, val), field, None)
            if got is not None and abs(float(got) - expected) > 1e-6:
                wrong.append((code, expected, float(got)))
        assert checked > 300, f"{field} 只检查了 {checked} 只, 样本太小说明前提坏了"
        assert not wrong, f"{field} 与真值不符 {len(wrong)} 只, 例: {wrong[:3]}"


def test_apply_uses_the_same_per_field_cut_as_project_terms():
    """``apply()`` 与 ``project_terms`` 必须共用逐字段裁剪判据。

    ``_SNAPSHOT_UNCOVERED_FIELDS`` 那个豁免集是为"快照里根本没有这个字段"准备的
    (cb_data 与每一份历史快照里 ``credit_rating_outlook`` 都是 0/1058)。``apply()``
    此前用 ``list_patches(after=...)`` 一刀切, 于是这两个字段被连坐裁掉 —— 同一天
    同一只债走 provider 是 None、走 project_terms 是「稳定」, 实测 345 / 14 处不一致。
    """
    from datetime import date

    from convertible_bond.historical_terms import (
        _SNAPSHOT_UNCOVERED_FIELDS,
        TermsPatch,
        TermsPatchStore,
        project_terms,
    )

    assert _SNAPSHOT_UNCOVERED_FIELDS  # 前提: 豁免集非空

    import tempfile
    from pathlib import Path

    from convertible_bond.data_providers.base import BondTerms

    tmp = Path(tempfile.mkdtemp()) / "p.json"
    store = TermsPatchStore(tmp)
    anchor = date(2026, 8, 26)
    store.add_many([
        TermsPatch(bond_code="A.SZ", effective_date=date(2026, 6, 1),
                   fields={"credit_rating_outlook": "稳定", "conversion_price": 9.0},
                   source="cninfo"),
    ])
    base = BondTerms(sec_name="A", conversion_price=10.0, maturity_date=date(2030, 1, 1))

    applied = store.apply("A.SZ", base, anchor, after=anchor)
    projected = project_terms("A.SZ", base, anchor, patch_store=store,
                              terms_as_of=anchor).terms

    # 快照覆盖不到的字段: 两条路都要留下它
    assert applied.credit_rating_outlook == "稳定", "apply 把豁免字段一起裁掉了"
    assert projected.credit_rating_outlook == "稳定"
    # 快照覆盖得到的字段: 两条路都该裁掉 (快照里已经含着了)
    assert applied.conversion_price == 10.0 and projected.conversion_price == 10.0


def test_future_lifecycle_fields_are_all_scrubbed_from_a_historical_view():
    """六个公告派生的生命周期字段也要按估值日剥掉未来值。

    ``_strip_unannounced_future_status`` 是「标准」口径回测路径上**唯一**的净化器
    (那条路 ``strip_fallback_status=False``, 剥得更干净的 ``strip_current_status_fields``
    根本不跑), 而它此前只处理强赎那一族。实测估值日 2024-01-02:
    ``down_reset_block_until`` 泄漏 201 只 —— 南航转债带着 2027-01-11 的不下修承诺,
    把整个 2024 年的下修博弈关掉了。
    """
    from datetime import date

    from convertible_bond.historical_terms import _strip_unannounced_future_status

    bundle, _ = _real_stores()
    val = date(2024, 1, 2)
    watched = ("putback_start_date", "putback_end_date",
               "conversion_suspension_start_date", "down_reset_block_until",
               "call_no_redemption_until")
    leaks: dict[str, int] = {}
    checked = 0
    for code in bundle.list_bonds():
        terms = bundle.get(code)
        listing = getattr(terms, "listing_date", None)
        maturity = getattr(terms, "maturity_date", None)
        if not (listing and maturity and listing <= val <= maturity):
            continue
        checked += 1
        out = _strip_unannounced_future_status(terms, val)
        for field in watched:
            value = getattr(out, field, None)
            if value is not None and value > val:
                leaks[field] = leaks.get(field, 0) + 1
        # 结束日只在"窗口当时确实开着"时才允许留在未来
        end = getattr(out, "conversion_suspension_end_date", None)
        start = getattr(out, "conversion_suspension_start_date", None)
        if end is not None and end > val and not (start is not None and start <= val):
            leaks["conversion_suspension_end_date(孤儿)"] = (
                leaks.get("conversion_suspension_end_date(孤儿)", 0) + 1)
    assert checked > 300, f"只检查了 {checked} 只, 样本太小说明前提坏了"
    assert not leaks, f"未来值仍然泄漏: {leaks}"


# ── bond_code 索引 (启动/批量重算的热点) ──────────────────────────
# ``split_batch_codes_from_cache`` 给库里**每只**债各调一次 ``list_patches(bond_code=)``,
# 而它原本每次复制整张 patch 表再逐条比对: 实测 1060 只 × 23174 条 = 0.43s, 每次开 GUI、
# 每次批量重算都白跑一遍。索引换掉的只是"从哪张表开始过滤", 排序与遮蔽都在后面。


def _patch(code: str, day: int, *, field="conversion_price", value=1.0, source="manual"):
    return TermsPatch(bond_code=code, effective_date=date(2026, 1, day),
                      fields={field: value}, source=source)


def _full_scan_list_patches(store, bond_code=None, **kwargs):
    """索引之前的实现: 复制整张表再按 bond_code 过滤。"""
    from convertible_bond.historical_terms import _drop_shadowed_patches, _patch_sort_key

    patches = list(store._patches)
    if bond_code:
        patches = [p for p in patches if p.bond_code == bond_code]
    if kwargs.get("through_date"):
        patches = [p for p in patches if p.effective_date <= kwargs["through_date"]]
    if kwargs.get("after") is not None:
        patches = [p for p in patches if p.effective_date > kwargs["after"]]
    patches = sorted(patches, key=_patch_sort_key)
    return patches if kwargs.get("include_shadowed") else _drop_shadowed_patches(patches)


def _store_with_mixed_patches(tmp_path):
    store = TermsPatchStore(tmp_path / "patches.json")
    store.add_many([
        _patch("128009.SZ", 3, value=12.3),
        _patch("110045.SH", 1, value=9.9),
        _patch("128009.SZ", 1, value=11.1),
        # 同日同债不同字段: 排序键有并列时, "从哪张表开始过滤"才可能改变输出顺序
        _patch("128009.SZ", 1, field="credit_rating", value="AA"),
        _patch("110045.SH", 5, value=8.8, source="wind_asof"),
        _patch("110045.SH", 2, value=7.7),
        # ⚠ 必须造出**排序键并列**的两条: ``_patch_sort_key`` 只到
        # (生效日, 事件日, 代码, 字段名集合) 为止, 值与来源都不在键里。没有并列时
        # 排序是全序, 桶内顺序怎么乱都看不出来 —— 那样的 fixture 会把"索引打乱了
        # 顺序"这个真 bug 测成绿的 (实测: 把索引改成 reversed 照样全过)。
        _patch("128009.SZ", 3, value=99.9, source="announcement"),
    ])
    return store


def test_bond_code_index_returns_exactly_what_the_full_scan_did(tmp_path):
    """逐位相同, 不只是"集合一样" —— 顺序会一路传到投影结果与 applied_patches。"""
    store = _store_with_mixed_patches(tmp_path)

    for code in ("128009.SZ", "110045.SH", "999999.SZ"):
        for kwargs in ({}, {"through_date": date(2026, 1, 2)}, {"after": date(2026, 1, 1)},
                       {"include_shadowed": True},
                       {"through_date": date(2026, 1, 4), "include_shadowed": True}):
            assert store.list_patches(bond_code=code, **kwargs) == \
                _full_scan_list_patches(store, code, **kwargs), (code, kwargs)
    # 不传 bond_code 的全库口径同样不能变 (数据体检与存量回洗走这一条)
    assert store.list_patches() == _full_scan_list_patches(store)
    assert store.list_patches(include_shadowed=True) == \
        _full_scan_list_patches(store, include_shadowed=True)


def test_index_follows_add_many_and_rewrite(tmp_path):
    """索引失效由 ``_patches`` 的 setter 兜底 —— 漏一处的表现是"改完还按旧的算", 不报错。"""
    store = _store_with_mixed_patches(tmp_path)
    assert len(store.list_patches(bond_code="128009.SZ")) == 4   # 先把索引建起来

    store.add_many([_patch("128009.SZ", 9, value=5.5)])
    assert [p.effective_date.day for p in store.list_patches(bond_code="128009.SZ")] == [1, 1, 3, 3, 9]

    store.rewrite(lambda p: None if p.bond_code == "128009.SZ" else p)
    assert store.list_patches(bond_code="128009.SZ") == []
    assert store.list_patches(bond_code="110045.SH"), "只该删掉那一只债的"

    # 重新读盘也要能拿到新状态 (``_load`` 攒到本地列表再一次性赋值, 不原地 append)
    assert TermsPatchStore(store.path).list_patches(bond_code="128009.SZ") == []


def _close_probe_provider(series_by_code):
    """只回答 get_bond_history 的桩, 顺带数发了几次。"""
    from convertible_bond.data_providers import DataProvider

    class _Stub(DataProvider):
        name = "stub"

        def __init__(self):
            self.calls = []

        def get_bond_terms(self, code, valuation_date):
            return None

        def get_stock_close(self, code, on_date):
            return 0.0

        def get_stock_history(self, code, start, end):
            return []

        def get_bond_history(self, code, start, end):
            self.calls.append((code, start, end))
            return [(d, v) for d, v in series_by_code.get(code, []) if start <= d <= end]

    return _Stub()


def test_close_window_hint_is_byte_identical_to_the_narrow_window():
    """宽窗口取一次再切片, 必须与"每个估值日打一发窄窗口"**逐日同值**。

    这是提速改动的验收: ``get_bond_terms`` 每次都无条件重取 ``close``, 而那一发打的是
    ``[估值日−15天, 估值日]`` 且直接问最内层 —— 同时绕开 ``_BacktestCacheProvider`` 与
    ``DiskCacheProvider`` 的复用 (后者按精确区间做键, 每个估值日都是新键)。改成一只债
    只取一次宽窗口之后, **结果不许有任何变化**。

    fixture 必须含一段 **>15 天的空档** (停牌/长假): 那正是两条路会分叉的地方 ——
    窄窗口里一根都没有 → ``close`` 是 None, 而宽窗口如果不把同一个下限再套一遍, 就会
    退回更早的一根陈旧价, 从"没有收盘价"变成"用半个月前的价"。那是口径变更不是提速
    (真实数据上实测: 去掉下限有 67 组从 None 变成陈旧价)。
    """
    from convertible_bond.historical_terms import (
        HistoricalBondDataProvider, _CLOSE_LOOKBACK_DAYS, _latest_bond_close)

    base = date(2025, 3, 3)
    normal = [(base + timedelta(days=i), 100.0 + i)
              for i in range(90) if (base + timedelta(days=i)).weekday() < 5]
    # 停牌 24 天: 空档跨过 15 天下限
    halted = [(d, v) for d, v in normal
              if not (date(2025, 4, 1) <= d <= date(2025, 4, 25))]
    series = {"110001.SH": normal, "110002.SH": halted, "110003.SH": []}

    lo, hi = base, base + timedelta(days=89)
    old_stub = _close_probe_provider(series)
    new_stub = _close_probe_provider(series)
    hp = HistoricalBondDataProvider(new_stub)
    hp.set_close_window_hint(lo - timedelta(days=_CLOSE_LOOKBACK_DAYS + 5), hi)

    saw_none = saw_value = 0
    for code in series:
        for offset in range((hi - lo).days + 1):
            vd = lo + timedelta(days=offset)
            expected = _latest_bond_close(old_stub, code, vd)
            assert hp._bond_close_at(code, vd) == expected, (code, vd)
            if expected is None:
                saw_none += 1
            else:
                saw_value += 1
    # fixture 自守卫: 两种结果都要出现过, 否则这条用例什么也没测到
    assert saw_none > 0 and saw_value > 0, (saw_none, saw_value)

    # 取数发数: 旧的每只债每个估值日一发, 新的**每只债一发**。
    # 空序列那只 (110003.SH) 刻意每次都重问 —— 见下一条用例的理由, 所以只数有数据的。
    n_dates = (hi - lo).days + 1
    for code in ("110001.SH", "110002.SH"):
        old_n = sum(1 for c, *_ in old_stub.calls if c == code)
        new_n = sum(1 for c, *_ in new_stub.calls if c == code)
        assert old_n == n_dates and new_n == 1, (code, old_n, new_n)


def test_close_window_hint_falls_back_outside_the_window_and_never_memoizes_empty():
    """两条边界: 窗口外原路走窄窗口; 空序列不记住。

    终点会被夹到「今天−1」(好让 DiskCacheProvider 肯落盘), 而 GUI 默认区间的最后一期
    可能就是今天 —— 那一期必须照常拿得到 close, 不能因为落在 hint 之外就变成 None。
    空序列不记住的理由与 ``_BacktestCacheProvider._remember`` 同源: 取数彻底失败与
    "这段本来就没行情"长得一样, 记下来这只债在整段回测里就再也拿不到 close 了。
    """
    from convertible_bond.historical_terms import HistoricalBondDataProvider

    base = date(2025, 3, 3)
    series = {"110001.SH": [(base + timedelta(days=i), 100.0 + i) for i in range(40)]}
    stub = _close_probe_provider(series)
    hp = HistoricalBondDataProvider(stub)
    hp.set_close_window_hint(base, base + timedelta(days=10))

    # 窗口内: 一发宽窗口
    assert hp._bond_close_at("110001.SH", base + timedelta(days=5)) == 105.0
    assert len(stub.calls) == 1
    # 窗口外: 回落窄窗口, 值照样对
    assert hp._bond_close_at("110001.SH", base + timedelta(days=30)) == 130.0
    assert len(stub.calls) == 2
    assert stub.calls[1][1] == base + timedelta(days=30 - 15)

    # 空序列: 不进缓存, 下一次还要真的再问一遍
    empty = _close_probe_provider({})
    hp2 = HistoricalBondDataProvider(empty)
    hp2.set_close_window_hint(base, base + timedelta(days=10))
    assert hp2._bond_close_at("110099.SH", base + timedelta(days=5)) is None
    assert hp2._bond_close_at("110099.SH", base + timedelta(days=6)) is None
    assert len(empty.calls) == 2, "空序列被当成权威结果记住了"


def test_backtest_hints_the_close_window_from_the_real_schedule():
    """hint 必须由**编排层**按真实日程给, 而且要够宽。

    窗口开小一格不会报错, 只会让那些估值日静默退回窄窗口 —— 提速没了而测试全绿。
    所以这里断言的是"起点留够回看余量、终点覆盖到最后一个可用调仓日"。
    """
    from convertible_bond.strategy_backtest import (
        _CLOSE_LOOKBACK_DAYS, _hint_close_window, build_rebalance_schedule)

    class _Recorder:
        inner = None
        def __init__(self):
            self.hint = None
        def set_close_window_hint(self, start, end):
            self.hint = (start, end)

    schedule = build_rebalance_schedule(date(2024, 1, 1), date(2024, 12, 31), "M")
    rec = _Recorder()
    _hint_close_window(rec, schedule)
    start, end = rec.hint
    assert start <= min(schedule) - timedelta(days=_CLOSE_LOOKBACK_DAYS)
    assert end >= max(schedule)

    # 沿装饰链往里找 (回测里 provider 外面还套着 _BacktestCacheProvider)
    class _Wrapper:
        def __init__(self, inner):
            self.inner = inner

    rec2 = _Recorder()
    _hint_close_window(_Wrapper(_Wrapper(rec2)), schedule)
    assert rec2.hint is not None

