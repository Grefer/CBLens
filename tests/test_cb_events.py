from datetime import date, timedelta

import pytest

from convertible_bond.cache import TermsBundle
from convertible_bond.cb_event_sync import apply_events_to_bundle, sync_cb_events
from convertible_bond.cb_events import (
    _TRANSIENT_CLEAR_GRACE_DAYS,
    _TRANSIENT_EVENT_TTL_DAYS,
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


def test_apply_events_to_terms_supersedes_old_down_reset_event_state():
    """旧 cb_events 写回 cb_data 后, 后续不下修公告仍应能覆盖冻结期."""
    old_title = "关于不向下修正旧转债转股价格的公告"
    new_title = "关于不向下修正新转债转股价格的公告"
    terms = BondTerms(
        down_reset_block_until=date(2026, 7, 15),
        down_reset_note=old_title,
    )
    events = [
        CBEvent(
            bond_code="113001.SH",
            event_date=date(2026, 4, 15),
            event_type="down_reset_rejected",
            raw_title=old_title,
            effective_end=date(2026, 7, 15),
        ),
        CBEvent(
            bond_code="113001.SH",
            event_date=date(2026, 8, 1),
            event_type="down_reset_rejected",
            raw_title=new_title,
            effective_end=date(2026, 11, 1),
        ),
    ]

    patched = apply_events_to_terms(
        "113001.SH",
        terms,
        events,
        valuation_date=date(2026, 8, 2),
    )

    assert patched.down_reset_block_until == date(2026, 11, 1)
    assert patched.down_reset_note == new_title


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


@pytest.mark.parametrize(
    "title,expected",
    [
        ("关于公司股票被实施退市风险警示的公告", "underlying_st_risk"),
        ("关于公司股票被实行*ST的公告", "underlying_st_risk"),
        ("关于公司股票被实行其他风险警示的公告", "underlying_st_risk"),
        ("关于撤销退市风险警示的公告", "underlying_st_clear"),
        ("关于申请撤销*ST的公告", "underlying_st_clear"),
        ("关于撤销公司股票其他风险警示的公告", "underlying_st_clear"),
    ],
)
def test_classify_underlying_st_risk_and_clear(title, expected):
    """ST 风险 / 撤销 ST 的分类必须互斥."""
    assert classify_announcement_title(title) == expected


def test_classify_underlying_st_clear_evaluated_before_risk():
    # 撤销 ST 公告里也常出现"风险警示"字样, 必须先于 risk 判定
    assert classify_announcement_title(
        "关于撤销公司股票退市风险警示及*ST的公告"
    ) == "underlying_st_clear"


@pytest.mark.parametrize(
    "title,expected",
    [
        ("关于xx转债临时停牌的公告", "suspension"),
        ("关于停牌xx转债的公告", "suspension"),
        ("关于公司股票临时停牌的公告", "underlying_suspension"),
        ("关于公司A股股票停牌的公告", "underlying_suspension"),
        ("关于正股停牌的公告", "underlying_suspension"),
        # 没有"转债""股票"等线索 → 不强行分类, 留 unknown
        ("关于临时停牌的公告", "unknown"),
    ],
)
def test_classify_suspension_routing(title, expected):
    assert classify_announcement_title(title) == expected


def test_parse_event_assigns_transient_ttl_when_no_explicit_end():
    """临停事件在公告无明确结束日时, parse 应按 event_date+TTL 兜底."""
    event_date = date(2026, 4, 15)
    event = parse_event_from_announcement(
        "128009.SZ",
        "关于xx转债临时停牌的公告",
        event_date,
    )
    assert event is not None
    assert event.event_type == "suspension"
    assert event.effective_end == event_date + timedelta(days=_TRANSIENT_EVENT_TTL_DAYS)


def test_apply_events_underlying_suspension_within_window_sets_status():
    val_date = date(2026, 4, 16)
    event = CBEvent(
        bond_code="128009.SZ",
        event_date=date(2026, 4, 15),
        event_type="underlying_suspension",
        raw_title="关于公司股票临时停牌的公告",
        effective_end=date(2026, 4, 18),    # 窗口内
        parsed_status="正股停牌",
    )
    patched = apply_events_to_terms(
        "128009.SZ",
        BondTerms(sec_name="测试转债"),
        [event],
        valuation_date=val_date,
    )
    assert patched.underlying_trade_status == "停牌"


def test_apply_events_just_expired_suspension_does_not_clear_admission():
    """刚过期临停事件不应擦掉 admission_status 当天写入的 '停牌'.

    场景: 4/1 临停事件 (effective_end=4/6) 过期 10 天 (< grace 30 天), 当前 4/16
    Wind admission 仍标 '停牌'. 事件层不应该清空.
    """
    val_date = date(2026, 4, 16)
    expired_event = CBEvent(
        bond_code="128009.SZ",
        event_date=date(2026, 4, 1),
        event_type="underlying_suspension",
        raw_title="关于公司股票临时停牌的公告",
        effective_end=date(2026, 4, 6),
    )
    terms = BondTerms(sec_name="测试转债", underlying_trade_status="停牌")
    patched = apply_events_to_terms(
        "128009.SZ",
        terms,
        [expired_event],
        valuation_date=val_date,
    )
    # admission 写入的状态仍保留, 没被旧事件擦掉
    assert patched.underlying_trade_status == "停牌"


def test_apply_events_long_expired_suspension_clears_status():
    """过期超过 grace 的临停事件应清空 stale 状态字段."""
    val_date = date(2026, 6, 15)
    expired_event = CBEvent(
        bond_code="128009.SZ",
        event_date=date(2026, 4, 1),
        event_type="underlying_suspension",
        raw_title="关于公司股票临时停牌的公告",
        effective_end=date(2026, 4, 6),
    )
    grace_days_diff = (val_date - expired_event.effective_end).days
    assert grace_days_diff > _TRANSIENT_CLEAR_GRACE_DAYS    # sanity check

    terms = BondTerms(sec_name="测试转债", underlying_trade_status="停牌")
    patched = apply_events_to_terms(
        "128009.SZ",
        terms,
        [expired_event],
        valuation_date=val_date,
    )
    assert patched.underlying_trade_status is None


def test_apply_events_suspension_without_terms_no_op_when_clearing():
    """terms 上字段已经是 None 时, 过期事件不应触发 update (避免无谓写盘)."""
    val_date = date(2026, 6, 15)
    expired_event = CBEvent(
        bond_code="128009.SZ",
        event_date=date(2026, 4, 1),
        event_type="suspension",
        raw_title="关于xx转债临时停牌的公告",
        effective_end=date(2026, 4, 6),
    )
    base = BondTerms(sec_name="测试转债")    # suspension_status=None
    patched = apply_events_to_terms(
        "128009.SZ",
        base,
        [expired_event],
        valuation_date=val_date,
    )
    # 没变化, 仍是同一个对象 (apply 在没 update 时 return 原 terms)
    assert patched is base


def test_apply_events_underlying_st_risk_sets_status():
    val_date = date(2026, 4, 20)
    event = CBEvent(
        bond_code="128009.SZ",
        event_date=date(2026, 4, 15),
        event_type="underlying_st_risk",
        raw_title="关于公司股票被实施退市风险警示的公告",
        parsed_status="ST/退市风险",
    )
    patched = apply_events_to_terms(
        "128009.SZ",
        BondTerms(sec_name="测试转债"),
        [event],
        valuation_date=val_date,
    )
    assert patched.underlying_status == "ST/退市风险"


def test_apply_events_underlying_st_clear_supersedes_earlier_risk():
    val_date = date(2026, 5, 10)
    risk = CBEvent(
        bond_code="128009.SZ",
        event_date=date(2026, 4, 15),
        event_type="underlying_st_risk",
        raw_title="关于公司股票被实施退市风险警示的公告",
        parsed_status="ST/退市风险",
    )
    clear = CBEvent(
        bond_code="128009.SZ",
        event_date=date(2026, 5, 5),
        event_type="underlying_st_clear",
        raw_title="关于撤销退市风险警示的公告",
    )
    terms = BondTerms(sec_name="测试转债", underlying_status="ST/退市风险")
    patched = apply_events_to_terms(
        "128009.SZ",
        terms,
        [risk, clear],
        valuation_date=val_date,
    )
    assert patched.underlying_status is None


def test_apply_events_st_risk_after_clear_re_arms_status():
    """同一债先撤销再实施 ST: 最新 risk 公告应重新点亮状态."""
    val_date = date(2026, 6, 1)
    clear = CBEvent(
        bond_code="128009.SZ",
        event_date=date(2026, 5, 5),
        event_type="underlying_st_clear",
        raw_title="关于撤销退市风险警示的公告",
    )
    risk = CBEvent(
        bond_code="128009.SZ",
        event_date=date(2026, 5, 30),    # 比 clear 更新
        event_type="underlying_st_risk",
        raw_title="关于公司股票被实施*ST的公告",
        parsed_status="ST/退市风险",
    )
    patched = apply_events_to_terms(
        "128009.SZ",
        BondTerms(sec_name="测试转债"),
        [clear, risk],
        valuation_date=val_date,
    )
    assert patched.underlying_status == "ST/退市风险"


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
