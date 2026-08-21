"""策略回测 controller 的共享常量与快照 JSON 辅助.

单一定义点: 各 strategy_* mixin 子模块与聚合入口 strategy_backtest 均从此导入,
避免常量在多个子模块间复制漂移。
"""
from __future__ import annotations

from datetime import date

import numpy as np

from ...batch_pricing import (
    DEFAULT_MIN_CREDIT_RATING,
    DEFAULT_MIN_OUTSTANDING_BALANCE,
)
from ...pricing_api import (
    DEFAULT_PDE_SIGNAL_SIGMA_REL_BAND,
    DEFAULT_PDE_SIGNAL_SPREAD_BAND,
)
from ..constants import DEFAULT_P_DOWN_PCT


STRATEGY_BACKTEST_PRO_FEATURE = "strategy_backtest"
STRATEGY_BACKTEST_PRO_PREVIEW = True
STRATEGY_DETAIL_TABLE_HEIGHT = 7
STRATEGY_COMPACT_TABLE_HEIGHT = 6
STRATEGY_MEDIUM_TABLE_HEIGHT = 8
STRATEGY_DATA_TABLE_HEIGHT = 10
STRATEGY_OVERVIEW_CHART_HEIGHT = 540
STRATEGY_RISK_CHART_HEIGHT = 240
STRATEGY_SECONDARY_CHART_HEIGHT = 300
WIND_HIGH_FIDELITY_CODE_WARN_LIMIT = 120
WIND_HIGH_FIDELITY_PRICING_WARN_LIMIT = 1000
WIND_HIGH_FIDELITY_REQUEST_MULTIPLIER = 10
# 每个样本除常规定价外, 还需端点定价、Brent 迭代和一次 p_down bump。
# 仅用于 GUI 工作量预估, 不参与模型计算。
PDE_DOWN_RESET_SOLVE_MULTIPLIER = 14
PDE_DOWN_RESET_SOLVE_WARN_LIMIT = 5000


def _strategy_snapshot_jsonable(obj):
    """递归将 date/datetime/nan/inf 转为 JSON 安全表示.

    既可作为 json.dump(default=...) 的 fallback,
    也可直接调用 _strategy_snapshot_jsonable(whole_dict) 做完整转换.
    """
    from datetime import datetime as _datetime
    if isinstance(obj, date):
        tag = "datetime" if isinstance(obj, _datetime) else "date"
        return {"__cblens_type__": tag, "value": obj.isoformat()}
    if isinstance(obj, (set, frozenset)):
        return [_strategy_snapshot_jsonable(v) for v in obj]
    if isinstance(obj, float):
        if np.isnan(obj) or np.isinf(obj):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: _strategy_snapshot_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_strategy_snapshot_jsonable(v) for v in obj]
    if isinstance(obj, (int, bool, str)) or obj is None:
        return obj
    # fallback for json.dump default
    raise TypeError(f"Not JSON serializable: {type(obj)} {obj!r}")


def _strategy_snapshot_object_hook(d):
    """json.load object_hook: tagged dict → date/datetime."""
    from datetime import datetime as _datetime
    if "__cblens_type__" in d:
        tag = d["__cblens_type__"]
        value = d.get("value", "")
        if tag == "date":
            return date.fromisoformat(value)
        if tag == "datetime":
            return _datetime.fromisoformat(value)
    return d


class StrategyBacktestCancelled(Exception):
    """用户主动中断策略回测."""


# 选债哲学由"选债规则"统一驱动: 置信度与硬复核风险按规则推导。
_DEFAULT_VIEW_POLICY = {"min_confidence": ("高", "中"), "exclude_review_risks": True}
STRATEGY_VIEW_POLICY = {
    "综合机会": {"min_confidence": ("高", "中"), "exclude_review_risks": True},
    "低估候选": {"min_confidence": ("高", "中"), "exclude_review_risks": True},
    "转股折价": {"min_confidence": ("高", "中"), "exclude_review_risks": True},
}

# 策略方案基线: 选择方案时先重置这些"选债逻辑"字段, 避免上个方案残留;
# 数据源 / 区间 / 代码池属环境配置, 不在策略方案范围内。
_STRATEGY_TEMPLATE_BASE = {
    "v_st_freq": "月", "v_st_top_n": "10", "v_st_view": "综合机会",
    "v_st_min_price": "", "v_st_max_price": "",
    "v_st_min_premium": "", "v_st_max_premium": "",
    "v_st_min_deviation": "", "v_st_max_deviation": "",
    "v_st_min_sigma": "", "v_st_max_sigma": "",
    "v_st_min_rating": DEFAULT_MIN_CREDIT_RATING or "",
    "v_st_min_balance": (
        "" if DEFAULT_MIN_OUTSTANDING_BALANCE is None else str(DEFAULT_MIN_OUTSTANDING_BALANCE)
    ),
    "v_st_min_turnover": "", "v_st_delist_window": "0", "v_st_cost": "20",
    # 模板 = 完整可复现配置: 选券权重/现金收益/仓位择时也随模板归位, 不残留上次手动值
    "v_st_weighting": "Top N 排序", "v_st_cash_yield": "2.2",
    "v_st_rank_signal": "稳健下修优势", "v_st_min_reset_edge": "0",
    "v_st_r": "2.2", "v_st_spread": "3.0", "v_st_distress_k": "5.0",
    "v_st_p_down": f"{DEFAULT_P_DOWN_PCT:g}", "v_st_vol_window": "1M",
    "v_st_sigma_band": f"{DEFAULT_PDE_SIGNAL_SIGMA_REL_BAND * 100:g}",
    "v_st_spread_band_bps": f"{DEFAULT_PDE_SIGNAL_SPREAD_BAND * 10000:g}",
    "v_st_event_exit": True,
    "v_st_exposure": "恒定满仓",
}
STRATEGY_TEMPLATES = {
    "下修错定价": {
        "v_st_view": "综合机会", "v_st_freq": "月", "v_st_top_n": "10",
        "v_st_weighting": "Top N 排序", "v_st_rank_signal": "稳健下修优势",
        "v_st_min_reset_edge": "0", "v_st_event_exit": True,
        "v_st_history_mode": "Wind高保真",
    },
    "估值偏差": {
        "v_st_view": "综合机会", "v_st_freq": "月", "v_st_top_n": "10",
        "v_st_weighting": "Top N 排序", "v_st_rank_signal": "估值偏差",
        "v_st_max_deviation": "0", "v_st_event_exit": False,
        "v_st_history_mode": "Wind高保真",
    },
}

# 策略回测默认 PDE 网格 (原 "快速" 档): 调参体感够用; 出报告时未来可接入"精确重跑"按钮.
_STRATEGY_PDE_GRID_M = 120
_STRATEGY_PDE_GRID_N = 400
