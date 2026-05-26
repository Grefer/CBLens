from datetime import date

from convertible_bond.cb_events import (
    CBEvent,
    CBEventStore,
    classify_announcement_title,
    events_for_down_reset,
    parse_event_from_announcement,
)
from convertible_bond.gui.constants import default_p_down_pct_for_state


def test_default_p_down_pct_for_state_uses_calibrated_buckets():
    assert default_p_down_pct_for_state(triggered=False) == (15.0, "未触发")
    assert default_p_down_pct_for_state(triggered=True) == (25.0, "已触发")
    assert default_p_down_pct_for_state(
        triggered=True, has_trigger_notice=True
    ) == (65.0, "触发提示")
    assert default_p_down_pct_for_state(
        triggered=True, has_scheduled_reset=True
    ) == (25.0, "公告态")
    assert default_p_down_pct_for_state(
        triggered=True, in_no_reset_block=True
    ) == (25.0, "冻结后")


def test_trigger_notice_is_not_classified_as_approved_down_reset():
    title = "关于惠云转债可能触发向下修正转股价格条件的提示性公告"

    assert classify_announcement_title(title) == "down_reset_trigger_notice"
    assert (
        classify_announcement_title(
            "关于触发转股价格向下修正条件暨董事会提议向下修正转股价格的公告"
        )
        == "down_reset_proposed"
    )
    event = parse_event_from_announcement("123456.SZ", title, date(2026, 1, 5))

    assert event is not None
    assert event.event_type == "down_reset_trigger_notice"
    assert event.parsed_status == "触发提示"


def test_events_for_down_reset_ignores_legacy_misclassified_trigger_notice(tmp_path):
    title = "关于惠云转债可能触发向下修正转股价格条件的提示性公告"
    store = CBEventStore(tmp_path / "events.json")
    store.add_many([
        CBEvent(
            bond_code="123456.SZ",
            event_date=date(2026, 1, 5),
            event_type="down_reset_approved",
            raw_title=title,
        )
    ])

    assert events_for_down_reset(
        "123456.SZ", store=store, through_date=date(2026, 1, 31)
    ) == []
