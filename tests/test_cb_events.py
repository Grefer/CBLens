from datetime import date

from convertible_bond.cache import TermsBundle
from convertible_bond.cb_event_sync import apply_events_to_bundle, sync_cb_events
from convertible_bond.cb_events import (
    CBEvent,
    CBEventStore,
    apply_events_to_terms,
    classify_announcement_title,
    parse_commitment_period,
    parse_event_from_announcement,
)
from convertible_bond.data_providers import BondTerms


def test_classify_announcement_title_handles_core_event_types():
    assert classify_announcement_title("关于不提前赎回可转债的公告") == "call_no_redemption"
    assert classify_announcement_title("关于实施赎回暨摘牌的公告") == "call_redemption"
    assert classify_announcement_title("关于不向下修正转股价格的公告") == "down_reset_rejected"
    assert classify_announcement_title("董事会提议向下修正转股价格的公告") == "down_reset_proposed"
    assert classify_announcement_title("关于可转债回售的提示性公告") == "putback"


def test_parse_event_from_announcement_extracts_dates_and_status():
    event = parse_event_from_announcement(
        "118006.SH",
        "关于实施赎回暨摘牌的公告，最后交易日为2026年4月27日，赎回日为2026年5月6日",
        date(2026, 4, 15),
        source="unit",
    )

    assert event is not None
    assert event.event_type == "call_redemption"
    assert event.effective_start == date(2026, 4, 27)
    assert event.effective_end == date(2026, 5, 6)
    assert event.parsed_status == "已公告强赎"


def test_event_store_dedupes_by_event_key(tmp_path):
    store = CBEventStore(tmp_path / "events.json")
    event = CBEvent(
        bond_code="118006.SH",
        event_date=date(2026, 4, 15),
        event_type="call_redemption",
        raw_title="关于实施赎回暨摘牌的公告",
    )

    assert store.add_many([event, event]) == 1
    assert store.add_many([event]) == 0
    assert store.list_events("118006.SH")[0].event_type == "call_redemption"


def test_apply_events_to_terms_updates_admission_and_down_reset_fields():
    terms = BondTerms(sec_name="测试转债")
    events = [
        CBEvent(
            bond_code="118006.SH",
            event_date=date(2026, 4, 15),
            event_type="call_redemption",
            raw_title="关于实施赎回暨摘牌的公告",
            effective_end=date(2026, 5, 6),
            parsed_status="已公告强赎",
        ),
        CBEvent(
            bond_code="118006.SH",
            event_date=date(2026, 4, 20),
            event_type="down_reset_rejected",
            raw_title="关于不向下修正转股价格的公告",
            parsed_status="不下修",
        ),
    ]

    patched = apply_events_to_terms(
        "118006.SH",
        terms,
        events,
        valuation_date=date(2026, 4, 28),
    )

    assert patched.call_status == "已公告强赎"
    assert patched.call_announce_date == date(2026, 4, 15)
    assert patched.call_redemption_date == date(2026, 5, 6)
    assert patched.down_reset_block_until == date(2026, 10, 20)
    assert "不向下修正" in patched.down_reset_note


def test_parse_commitment_period_strategy_a_arabic_months():
    body = (
        "公司董事会决定本次不向下修正“嘉诚转债”的转股价格，"
        "并且在未来3个月内（即2026年4月21日至2026年7月20日），"
        "公司股价若再次触发此条款，亦不向下修正“嘉诚转债”的转股价格。"
    )
    result = parse_commitment_period(body, event_type="down_reset_rejected")
    assert result is not None
    assert result["months"] == 3
    assert result["start"] == date(2026, 4, 21)
    assert result["end"] == date(2026, 7, 20)
    assert result["strategy"] == "A"


def test_parse_commitment_period_strategy_a_chinese_numerals():
    body = (
        "公司董事会决定本次不行使“鼎龙转债”的提前赎回权利，"
        "同时在未来六个月内（即 2026 年 4 月 29 日至 2026 年 10 月 28 日），"
        "如再次触发“鼎龙转债”有条件赎回条款时，公司均不行使提前赎回权利。"
    )
    result = parse_commitment_period(body, event_type="call_no_redemption")
    assert result is not None
    assert result["months"] == 6
    assert result["start"] == date(2026, 4, 29)
    assert result["end"] == date(2026, 10, 28)


def test_parse_commitment_period_tolerates_inline_parenthetical():
    # 康泰医学风格: "未来六个月内即 2026 年 4 月 20 日（2026 年 4 月 17 日次一交易日）至 2026 年 10 月 19 日"
    body = (
        "公司董事会决定本次不向下修正“康医转债”转股价格，"
        "且在未来六个月内即 2026 年 4 月 20 日（2026 年 4 月 17 日次一交易日）至 "
        "2026 年 10 月 19 日，如再次触发“康医转债”转股价格的向下修正条款，"
        "亦不提出向下修正方案。"
    )
    result = parse_commitment_period(body, event_type="down_reset_rejected")
    assert result is not None
    assert result["months"] == 6
    assert result["start"] == date(2026, 4, 20)
    assert result["end"] == date(2026, 10, 19)


def test_parse_commitment_period_rejects_trigger_observation_window():
    # 触发段日期不应被误识别为承诺段; 仅有触发上下文, 没有"未来 X 个月"也没有决定+承诺锚定
    body = (
        "自 2026 年 3 月 24 日至 2026 年 4 月 14 日，公司股票已有十五个交易日的"
        "收盘价低于当期转股价格的 85%，已触发“共同转债”转股价格向下修正条件。"
    )
    assert parse_commitment_period(body, event_type="down_reset_rejected") is None


def test_parse_event_from_announcement_uses_body_for_commitment():
    body = (
        "公司董事会决定本次不向下修正“艾为转债”转股价格，"
        "且在未来三个月（2026 年 4 月 16 日至 2026 年 7 月 15 日）内，"
        "如再次触发“艾为转债”转股价格向下修正条款，亦不提出向下修正方案。"
    )
    event = parse_event_from_announcement(
        "118034.SH",
        "关于不向下修正“艾为转债”转股价格的公告",
        date(2026, 4, 15),
        body=body,
    )
    assert event is not None
    assert event.event_type == "down_reset_rejected"
    assert event.commitment_months == 3
    assert event.effective_start == date(2026, 4, 16)
    assert event.effective_end == date(2026, 7, 15)


def test_apply_events_uses_parsed_commitment_end_over_hardcode():
    terms = BondTerms(sec_name="测试转债")
    events = [
        CBEvent(
            bond_code="113001.SH",
            event_date=date(2026, 4, 15),
            event_type="down_reset_rejected",
            raw_title="关于不向下修正转股价格的公告",
            effective_start=date(2026, 4, 16),
            effective_end=date(2026, 7, 15),     # 解析得到的 3 个月承诺
            commitment_months=3,
        ),
        CBEvent(
            bond_code="113001.SH",
            event_date=date(2026, 4, 15),
            event_type="call_no_redemption",
            raw_title="关于不提前赎回的公告",
            effective_start=date(2026, 4, 16),
            effective_end=date(2026, 10, 15),    # 6 个月承诺
            commitment_months=6,
        ),
    ]

    patched = apply_events_to_terms(
        "113001.SH",
        terms,
        events,
        valuation_date=date(2026, 4, 28),
    )

    # down_reset 走 effective_end, 不再用 hardcode 6 个月
    assert patched.down_reset_block_until == date(2026, 7, 15)
    # call_no_redemption 写入新字段
    assert patched.call_no_redemption_until == date(2026, 10, 15)
    assert patched.call_status == "不强赎"


def test_event_store_round_trips_commitment_months(tmp_path):
    store = CBEventStore(tmp_path / "events.json")
    event = CBEvent(
        bond_code="113001.SH",
        event_date=date(2026, 4, 15),
        event_type="down_reset_rejected",
        raw_title="关于不向下修正转股价格的公告",
        effective_start=date(2026, 4, 16),
        effective_end=date(2026, 7, 15),
        commitment_months=3,
    )
    store.add_many([event])

    reloaded = CBEventStore(tmp_path / "events.json").list_events("113001.SH")
    assert len(reloaded) == 1
    assert reloaded[0].commitment_months == 3
    assert reloaded[0].effective_end == date(2026, 7, 15)


def test_event_store_marks_synced_even_without_new_events(tmp_path):
    store = CBEventStore(tmp_path / "events.json")
    store.mark_synced(["113001.SH"])

    reloaded = CBEventStore(tmp_path / "events.json")
    assert "113001.SH" in reloaded._meta["synced_at_by_code"]
    assert reloaded.list_events("113001.SH") == []


def test_sync_events_and_apply_to_bundle(tmp_path):
    class FakeProvider:
        name = "fake"

        def list_bond_announcements(self, bond_code, start, end):
            return [
                {
                    "title": "关于不提前赎回可转债的公告",
                    "date": date(2026, 4, 1),
                },
                {
                    "title": "关于不向下修正转股价格的公告",
                    "date": date(2026, 4, 2),
                },
            ]

    event_store = CBEventStore(tmp_path / "events.json")
    result = sync_cb_events(
        FakeProvider(),
        ["113050.SH"],
        event_store,
        start=date(2026, 1, 1),
        end=date(2026, 4, 28),
    )

    assert result["scanned_announcements"] == 2
    assert result["added"] == 2

    bundle = TermsBundle(tmp_path / "cb_data.json")
    bundle.set("113050.SH", BondTerms(sec_name="测试转债"), source="unit")
    applied = apply_events_to_bundle(
        event_store,
        bundle,
        valuation_date=date(2026, 4, 28),
    )

    patched = bundle.get("113050.SH")
    assert applied["updated"] == 1
    assert patched.call_status == "不强赎"
    assert patched.down_reset_block_until == date(2026, 10, 2)
