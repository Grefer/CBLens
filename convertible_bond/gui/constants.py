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


# 策略页选债规则: 只保留对"系统化买入"有意义的规则 (剔除"需复核")
STRATEGY_SELECTION_VIEWS = ("综合机会", "低估候选", "转股折价")
# 策略页顶部策略方案的展示顺序; "自定义" 表示完全手动
STRATEGY_TEMPLATE_NAMES = ("自定义", "低估轮动", "折价套利", "稳健打底")
STRATEGY_POOL_MODES = ("本地全市场", "当前筛选结果", "自选代码")
STRATEGY_HISTORY_MODES = ("标准", "Wind高保真")
STRATEGY_TEMPLATE_DESCRIPTIONS = {
    "自定义": "保留当前手动参数\n适合在已有结果上继续微调",
    "低估轮动": "月频 · Top 10 · 低估候选\n转股溢价 ≤ 30%",
    "折价套利": "周频 · Top 10 · 转股折价\n转股溢价 ≤ 5%",
    "稳健打底": "月频 · Top 15 · 综合机会\n价格 ≤ 120 · 转股溢价 ≤ 20%",
}
STRATEGY_VIEW_DESCRIPTIONS = {
    "综合机会": "平衡低估程度、机会分和风险标签\n默认稳健视图",
    "低估候选": "优先市价低于模型理论价的转债\n偏向价值回归",
    "转股折价": "偏向低溢价或折价标的\n更关注股性和套利空间",
}
STRATEGY_POOL_DESCRIPTIONS = {
    "本地全市场": "本地条款库里的全部转债\n适合全市场策略回测",
    "当前筛选结果": "批量页当前视图里的转债\n适合先筛选再回测",
    "自选代码": "手动粘贴或导入一组转债代码\n适合小组合复盘",
}
STRATEGY_HISTORY_DESCRIPTIONS = {
    "标准": "推荐 · 本地条款修正 + 公告事件回放\n离线可跑, 适合日常复盘",
    "Wind高保真": "Wind 按估值日实时查询历史条款\n可信度最高, 需 Wind 接口且较慢",
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
    "final_equity": "扣除交易成本后的期末组合净值\n初始净值 = 1.0000",
    "total_return": "期末净值相对初始净值的累计收益率",
    "annualized": "按回测天数折算的年化收益率\n便于与基准和其他策略横向比较",
    "excess": "策略总收益 − 等权基准总收益\n正值表示跑赢基准, 需勾选基准后参考",
    "max_drawdown": "净值从历史高点到后续低点的最大回落幅度\n衡量策略最极端的亏损压力",
    "sharpe": "超额收益 / 波动率 (年化)\n> 1 较好, > 2 优秀, < 0 表示期望亏损",
    "sortino": "仅对下行波动惩罚的风险调整收益\n比 Sharpe 更关注亏损方向的波动",
    "calmar": "年化收益 / 最大回撤\n衡量单位回撤带来的年化回报",
    "cash": "平均未投入的现金权重\n偏高通常表示选债条件过严或标的流动性不足",
    "turnover": "每期平均调仓比例\n越高越容易受交易成本和滑点侵蚀收益",
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
