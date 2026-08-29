"""GUI 共享常量.

集中放在这里, 避免 controller mixin 反向 import app 造成循环.
"""
import re

# 显式 re-export: app.py / controllers.wind_sync 经本模块导入模型默认值
from ..model_defaults import DEFAULT_DOWN_RESET_TRIGGER_PCT as DEFAULT_DOWN_RESET_TRIGGER_PCT


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


# 新策略页只暴露模型错定价策略；旧批量视图常量保留给历史预设兼容。
STRATEGY_SELECTION_VIEWS = ("综合机会",)
STRATEGY_TEMPLATE_NAMES = ("下修错定价", "估值偏差")
STRATEGY_POOL_MODES = ("本地全市场", "当前筛选结果", "自选代码")
STRATEGY_HISTORY_MODES = ("标准", "Wind高保真")
STRATEGY_TEMPLATE_DESCRIPTIONS = {
    "下修错定价": "选择下修价值被低估且情景扰动后仍有优势的转债",
    "估值偏差": "选择市价低于模型理论价的转债",
}
STRATEGY_VIEW_DESCRIPTIONS = {
    "综合机会": "全部可交易主池, 不额外筛\n默认稳健视图",
    "低估候选": "优先市价低于模型理论价的转债\n偏向价值回归",
    "转股折价": "偏向低溢价或折价标的\n更关注股性和套利空间",
}
STRATEGY_POOL_DESCRIPTIONS = {
    "本地全市场": "本地条款库里的全部转债\n适合全市场策略回测",
    "当前筛选结果": "批量页当前视图里的转债\n适合先筛选再回测",
    "自选代码": "手动粘贴或导入一组转债代码\n适合小组合复盘",
}
STRATEGY_HISTORY_DESCRIPTIONS = {
    "标准": "快速诊断 · 本地条款修正 + 公告事件回放\n离线可跑, 最终结论需高保真复核",
    "Wind高保真": "推荐 · Wind 按估值日查询历史条款\n用于正式策略回测, 速度较慢",
}

STRATEGY_TEMPLATE_LEGACY_ALIASES = {
    "PDE下修错定价": "下修错定价",
    "PDE估值偏差": "估值偏差",
    "自定义": "下修错定价",
    "自定义PDE": "下修错定价",
    "低估轮动": "估值偏差",
    "折价套利": "估值偏差",
    "稳健打底": "估值偏差",
}

STRATEGY_PDE_RANK_SIGNAL_LABELS = (
    "稳健下修优势",
    "下修优势",
    "估值偏差",
)
STRATEGY_PDE_RANK_SIGNAL_LEGACY_ALIASES = {
    "": "稳健下修优势",
    "PDE稳健下修优势": "稳健下修优势",
    "PDE下修优势": "下修优势",
    "PDE估值偏差": "估值偏差",
    "机会分": "估值偏差",       # 机会分已删, 旧快照落到估值偏差
    "双低": "估值偏差",
    "score": "估值偏差",
    "double_low": "估值偏差",
    "down_reset_robust_edge": "稳健下修优势",
    "down_reset_edge": "下修优势",
    "deviation": "估值偏差",
}


def normalize_pde_strategy_template(value: str | None) -> str:
    name = str(value or "").strip()
    name = STRATEGY_TEMPLATE_LEGACY_ALIASES.get(name, name)
    return name if name in STRATEGY_TEMPLATE_NAMES else "下修错定价"


def normalize_pde_rank_signal_label(value: str | None) -> str:
    label = str(value or "").strip()
    label = STRATEGY_PDE_RANK_SIGNAL_LEGACY_ALIASES.get(label, label)
    return (
        label
        if label in STRATEGY_PDE_RANK_SIGNAL_LABELS
        else "稳健下修优势"
    )


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


# ── 行情源默认值 ────────────────────────────────────────────────

_DEFAULT_SOURCE_CACHE: list[str] = []


def default_market_source() -> str:
    """GUI 启动时该默认选哪个行情源.

    此前两处都硬编码 ``"Wind"``, 于是**没装 Wind 的机器上每一次取数都必然失败** ——
    而 GUI 侧从来没引用过 ``detect_available_providers``, 用户拿到的只是一句
    "未安装 WindPy"。

    判据只看**可导入性**, 故意不看"终端连没连": 连接状态是会变的 (用户可能先开
    CBLens 再登录 Wind 终端), 拿它定默认值会让下拉框的初值随开机顺序漂移。
    "装了但没连"那一档由 ``wind_is_ready()`` 在**非用户发起**的取数处单独挡
    (见 ``tabs/batch_watchlist._source_ready_without_connecting``)。

    只探测一次并缓存: 探测本身是 import (实测 WindPy 0.11s / akshare 0.56s),
    在启动路径上重复做没有意义。
    """
    if not _DEFAULT_SOURCE_CACHE:
        try:
            from ..data_providers import detect_available_providers
            available = detect_available_providers()
        except Exception:
            available = []
        # 两个都探测不到时仍回落 akshare: 它是 pip 依赖, 失败信息 ("pip install akshare")
        # 比 Wind 那条 ("请安装 Wind 金融终端") 对绝大多数用户更可操作。
        _DEFAULT_SOURCE_CACHE.append(available[0] if available else "akshare")
    return _DEFAULT_SOURCE_CACHE[0]
