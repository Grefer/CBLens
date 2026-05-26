"""GUI 共享常量.

集中放在这里, 避免 controller mixin 反向 import app 造成循环.
"""
import re

from ..model_defaults import DEFAULT_DOWN_RESET_TRIGGER_PCT


BOND_CODE_RE = re.compile(r"^\d{6}\.[A-Z]{2}$")
LOW_P_DOWN_PCT = 15.0
DEFAULT_P_DOWN_PCT = 25.0
TRIGGER_NOTICE_P_DOWN_PCT = 65.0
P_DOWN_AUTO_SOURCE_LABELS = frozenset({
    "模型",
    "默认",
    "未触发",
    "已触发",
    "触发提示",
    "公告态",
    "冻结后",
})
DEFAULT_DISTRESS_K_PCT = 5.0
DEFAULT_CREDIT_SPREAD_PCT = 3.0
EVENT_SYNC_STALE_HOURS = 24


# 策略页: 只保留对"系统化买入"有意义的视图 (剔除"需复核")
STRATEGY_SELECTION_VIEWS = ("综合机会", "低估候选", "转股折价")
# 策略页顶部模板下拉的展示顺序; "自定义" 表示完全手动
STRATEGY_TEMPLATE_NAMES = ("自定义", "低估轮动", "折价套利", "稳健打底")


def default_p_down_pct_for_state(
    *,
    triggered: bool | None,
    has_trigger_notice: bool = False,
    has_scheduled_reset: bool = False,
    in_no_reset_block: bool = False,
) -> tuple[float, str]:
    """按单债当前下修状态给 GUI 的背景下修强度默认值.

    返回的是年化强度 λ 的百分数, 不是 1 年内概率。已提议/已通过待生效
    的一次性下修节点仍由公告事件单独建模; 这里的值只作为背景 hazard。
    """
    if has_scheduled_reset:
        return DEFAULT_P_DOWN_PCT, "公告态"
    if has_trigger_notice:
        return TRIGGER_NOTICE_P_DOWN_PCT, "触发提示"
    if in_no_reset_block:
        return DEFAULT_P_DOWN_PCT, "冻结后"
    if triggered is False:
        return LOW_P_DOWN_PCT, "未触发"
    if triggered is True:
        return DEFAULT_P_DOWN_PCT, "已触发"
    return DEFAULT_P_DOWN_PCT, "模型"
