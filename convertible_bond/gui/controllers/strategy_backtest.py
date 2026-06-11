"""📈 历史回测 — 选债策略回测 (聚合入口).

原 3200+ 行单文件按职责拆为 6 个 mixin 子模块, 本模块聚合为
``StrategyBacktestMixin``, 对 app.py 与既有导入方完全透明:

- strategy_setup            输入与预检 (模板/代码池/导入/precheck)
- strategy_run              运行执行 (启动/取消/worker/provider)
- strategy_snapshots        快照与导出 (保存/加载/prune/CSV)
- strategy_render           结果渲染框架与概览/明细
- strategy_render_analysis  分析子页渲染 (归因/风险/稳健性/数据)
- strategy_compare          多次回测对比

模块级常量与快照 JSON 辅助在 strategy_common, 此处 re-export
保持旧导入路径 (tests 等 ``from ...strategy_backtest import X``) 有效。
单债"模型 vs 市场"偏差回测见 backtest.py。
"""
from .strategy_common import (  # re-export: 既有测试/调用方从本模块导入
    STRATEGY_BACKTEST_PRO_FEATURE,
    STRATEGY_BACKTEST_PRO_PREVIEW,
    STRATEGY_COMPACT_TABLE_HEIGHT,
    STRATEGY_DATA_TABLE_HEIGHT,
    STRATEGY_DETAIL_TABLE_HEIGHT,
    STRATEGY_MEDIUM_TABLE_HEIGHT,
    STRATEGY_OVERVIEW_CHART_HEIGHT,
    STRATEGY_RISK_CHART_HEIGHT,
    STRATEGY_SECONDARY_CHART_HEIGHT,
    STRATEGY_TEMPLATES,
    STRATEGY_VIEW_POLICY,
    StrategyBacktestCancelled,
    WIND_HIGH_FIDELITY_CODE_WARN_LIMIT,
    WIND_HIGH_FIDELITY_PRICING_WARN_LIMIT,
    WIND_HIGH_FIDELITY_REQUEST_MULTIPLIER,
    _DEFAULT_VIEW_POLICY,
    _STRATEGY_PDE_GRID_M,
    _STRATEGY_PDE_GRID_N,
    _STRATEGY_TEMPLATE_BASE,
    _strategy_snapshot_jsonable,
    _strategy_snapshot_object_hook,
)
from .strategy_setup import StrategySetupMixin
from .strategy_run import StrategyRunMixin
from .strategy_snapshots import StrategySnapshotMixin
from .strategy_render import StrategyRenderMixin
from .strategy_render_analysis import StrategyAnalysisRenderMixin
from .strategy_compare import StrategyCompareMixin

__all__ = [
    "StrategyBacktestMixin",
    "StrategySetupMixin",
    "StrategyRunMixin",
    "StrategySnapshotMixin",
    "StrategyRenderMixin",
    "StrategyAnalysisRenderMixin",
    "StrategyCompareMixin",
    "StrategyBacktestCancelled",
    "STRATEGY_BACKTEST_PRO_FEATURE",
    "STRATEGY_BACKTEST_PRO_PREVIEW",
    "STRATEGY_COMPACT_TABLE_HEIGHT",
    "STRATEGY_DATA_TABLE_HEIGHT",
    "STRATEGY_DETAIL_TABLE_HEIGHT",
    "STRATEGY_MEDIUM_TABLE_HEIGHT",
    "STRATEGY_OVERVIEW_CHART_HEIGHT",
    "STRATEGY_RISK_CHART_HEIGHT",
    "STRATEGY_SECONDARY_CHART_HEIGHT",
    "STRATEGY_TEMPLATES",
    "STRATEGY_VIEW_POLICY",
    "WIND_HIGH_FIDELITY_CODE_WARN_LIMIT",
    "WIND_HIGH_FIDELITY_PRICING_WARN_LIMIT",
    "WIND_HIGH_FIDELITY_REQUEST_MULTIPLIER",
    "_DEFAULT_VIEW_POLICY",
    "_STRATEGY_PDE_GRID_M",
    "_STRATEGY_PDE_GRID_N",
    "_STRATEGY_TEMPLATE_BASE",
    "_strategy_snapshot_jsonable",
    "_strategy_snapshot_object_hook",
]


class StrategyBacktestMixin(
    StrategySetupMixin,
    StrategyRunMixin,
    StrategySnapshotMixin,
    StrategyRenderMixin,
    StrategyAnalysisRenderMixin,
    StrategyCompareMixin,
):
    """选债策略回测 tab 的业务逻辑 (含快照与对比)."""
