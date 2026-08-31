from datetime import date

from convertible_bond import down_reset_overrides as dro
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


def test_calibrated_down_reset_constants_are_pinned_to_their_values():
    """四个校准常数必须被**字面值**钉住, 不能只靠"拿常数算期望值"的断言.

    这四个数是 ``cb-calibrate-down-reset`` 从历史事件校准出来的模型参数, 直接决定
    regime ② 一次性下修节点的生效日与概率。但现存引用它们的用例全是
    ``assert result == pytest.approx(dro.PROPOSED_PASS_PROB)`` 这种形状 —— 期望值
    **从常数本身算出来**, 于是把 0.9 改成 0.1、把 17 天改成 170 天, 套件照样全绿。

    改这些值本身没问题, 但那是一次**模型行为变更**, 应该由这条用例红一次来确认,
    而不是悄悄生效。改校准请连同这里一起改, 并在提交信息里写明重跑了校准。
    """
    assert dro.PROPOSED_PASS_PROB == 0.9          # 有终态 100% / 含未决 83%
    assert dro.PROPOSED_EFFECTIVE_LAG_DAYS == 17  # 提议→通过 中位 17 / 均值 19 天
    assert dro.APPROVED_PASS_PROB == 1.0          # 已通过 → 必然落地
    assert dro.APPROVED_EFFECTIVE_LAG_DAYS == 7   # 通过→生效 (登记日次日) 的兜底
    assert dro.DEFAULT_COOLDOWN_MONTHS == 6       # 募集说明书常见冷静期

    # 概率是概率, 滞后是正整数天 —— 顺手钉住量纲, 免得单位被改成"月"还全绿
    assert 0.0 < dro.PROPOSED_PASS_PROB <= dro.APPROVED_PASS_PROB <= 1.0
    assert dro.PROPOSED_EFFECTIVE_LAG_DAYS > dro.APPROVED_EFFECTIVE_LAG_DAYS > 0
