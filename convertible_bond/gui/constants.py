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
STRATEGY_POOL_MODES = ("本地全市场", "当前筛选结果", "自选代码")
STRATEGY_HISTORY_MODES = ("标准", "Wind高保真")
STRATEGY_TEMPLATE_DESCRIPTIONS = {
    "自定义": "保留当前手动参数, 适合从已有结果继续微调",
    "低估轮动": "月频 · Top10 · 低估候选 · 转股溢价≤30%",
    "折价套利": "周频 · Top10 · 转股折价 · 转股溢价≤5%",
    "稳健打底": "月频 · Top15 · 综合机会 · 价格≤120 · 转股溢价≤20%",
}
STRATEGY_VIEW_DESCRIPTIONS = {
    "综合机会": "平衡低估程度、机会分和风险标签, 适合作为默认稳健视图",
    "低估候选": "优先选择市价低于模型理论价的转债, 偏向价值回归",
    "转股折价": "偏向低转股溢价或折价标的, 更关注股性和套利空间",
}
STRATEGY_POOL_DESCRIPTIONS = {
    "本地全市场": "使用本地条款库里的全部转债, 适合做全市场策略回测",
    "当前筛选结果": "复用批量页当前视图中的转债代码, 适合先筛选再回测",
    "自选代码": "手动粘贴或导入一组转债代码, 适合小组合复盘",
}
STRATEGY_HISTORY_DESCRIPTIONS = {
    "标准": "推荐 · 使用 cb_terms_patches 历史条款修正 + cb_events 公告事件回放, 离线可跑, 适合日常复盘",
    "Wind高保真": "运行时直接用 Wind 按估值日查询历史条款和状态, 可信度最高, 需 Wind 接口且较慢",
}
STRATEGY_HISTORY_LEGACY_ALIASES = {
    "快速": "标准",
    "Wind防未来": "Wind高保真",
    "本地快照": "标准",
    "自定义文件": "标准",
}


def normalize_strategy_history_mode(value: str | None) -> str:
    """兼容旧预设/旧 UI 文案里的历史口径值."""
    mode = str(value or "").strip()
    mode = STRATEGY_HISTORY_LEGACY_ALIASES.get(mode, mode)
    return mode if mode in STRATEGY_HISTORY_DESCRIPTIONS else "标准"


STRATEGY_STAT_TOOLTIPS = {
    "final_equity": "初始净值为 1.0000, 这里是扣除交易成本后的期末组合净值",
    "total_return": "期末净值相对初始净值的累计收益",
    "annualized": "按回测天数折算后的年化收益率",
    "excess": "策略总收益减等权基准总收益; 勾选基准后最有参考价值",
    "max_drawdown": "从历史净值高点到后续低点的最大跌幅",
    "sharpe": "收益相对波动的比值, 越高代表单位波动带来的收益越高",
    "sortino": "只惩罚下行波动的风险收益比, 更关注亏损波动",
    "calmar": "年化收益与最大回撤的比值, 用来观察收益是否覆盖回撤压力",
    "cash": "平均未投入现金权重; 偏高通常表示条件过严或成交数据不足",
    "turnover": "每期平均调仓比例; 越高越容易受交易成本和滑点影响",
}


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
