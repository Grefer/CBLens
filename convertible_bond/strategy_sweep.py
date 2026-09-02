"""策略参数扫描: 一份定价面板 × N 组组合层变体 + 多重检验校正.

信号计算 (逐期 PDE 定价) 与组合仿真解耦的第一步:

  - 变体只允许覆盖**组合层**字段 (``PORTFOLIO_SWEEP_FIELDS``), 这些字段不影响
    逐期定价输入, 因而全部变体共享一份 ``pricing_snapshot_cache`` —— N 组变体
    只为定价付一次费, 参数扫描从"跑一次看一次"变成秒级对比。
  - 行情/条款取数经共享的 ``_BacktestCacheProvider`` 跨变体复用。
  - 扫描便宜之后过拟合风险同步上升, 结果附最优变体的 Deflated Sharpe
    (Bailey & López de Prado): 为"从 N 组里挑最好"的选择偏差付费后,
    Sharpe 仍为正的概率。DSR < 0.95 时最优变体应视为可能是数据挖掘产物。

约束 (设计取舍, 非实现偷懒):

  - ``rebalance_freq`` / ``pool_mode`` / 定价参数 (r, spread, ...) 不可扫——它们
    改变逐期估值日或定价输入, 缓存必然击穿, 应作为独立的完整回测分别跑。
  - 扫描统一强制 ``pre_filter_prices=False``: 价格预筛会改变送定价的代码集合
    (击穿缓存), 关掉后价格区间过滤仍在 A 过滤层 (定价后) 生效, 语义不变,
    只是多定价了一些券——这正是"面板"的本意。
"""
from __future__ import annotations

import logging
from dataclasses import replace
from datetime import date
from itertools import product
from typing import Any

from . import backtest_stats
from .data_providers import DataProvider, finite_float
from .strategy_backtest import (
    PDEStrategyConfig,
    ScoreStrategyConfig,
    _BacktestCacheProvider,
    _periods_per_year,
    backtest_score_strategy,
    validate_strategy_config,
)

logger = logging.getLogger(__name__)

# 允许在变体间变化的组合层字段: 均不影响 (估值日, 定价参数, 送定价代码集) 三元组。
PORTFOLIO_SWEEP_FIELDS = frozenset({
    "top_n", "holding_mode", "rank_signal", "max_holdings",
    "down_reset_event_exit",
    "funding_mode", "exposure_mode", "exposure_valuation_k", "exposure_floor",
    "cash_yield_rate", "transaction_cost",
    "selection_view", "min_confidence", "exclude_risk_tags",
    "min_market_price", "max_market_price",
    "min_conversion_premium", "max_conversion_premium",
    "min_deviation", "max_deviation", "min_sigma", "max_sigma",
})

PDE_PORTFOLIO_SWEEP_FIELDS = frozenset({
    "top_n", "rank_signal", "down_reset_event_exit",
    "exposure_mode", "exposure_valuation_k", "exposure_floor",
    "cash_yield_rate", "transaction_cost",
    "min_confidence", "exclude_risk_tags",
    "min_market_price", "max_market_price",
    "min_conversion_premium", "max_conversion_premium",
    "min_deviation", "max_deviation", "min_sigma", "max_sigma",
})


def build_sweep_variants(grid: dict[str, list[Any]]) -> list[dict[str, Any]]:
    """把 {字段: [取值, ...]} 网格展开成变体列表 (笛卡尔积, 保持字段声明顺序)。"""
    if not grid:
        return []
    _validate_sweep_fields(grid.keys())
    keys = list(grid)
    combos = product(*(grid[key] for key in keys))
    return [dict(zip(keys, values)) for values in combos]


def sweep_score_strategy(
    provider: DataProvider,
    bond_codes: list[str],
    *,
    start_date: date,
    end_date: date,
    variants: list[dict[str, Any]],
    base_config: ScoreStrategyConfig | None = None,
    terms_cache=None,
    admission_config=None,
    keep_results: bool = False,
    progress_cb=None,
    stage_cb=None,
    cancel_cb=None,
    **engine_kwargs,
) -> dict[str, Any]:
    """对同一定价面板跑多组组合层变体, 输出对比表 + 最优变体 DSR.

    ``variants`` 每项为覆盖 ``base_config`` 的字段 dict (可含 ``name`` 键作展示名,
    仅允许 ``PORTFOLIO_SWEEP_FIELDS``); ``engine_kwargs`` 与
    ``backtest_score_strategy`` 相同 (r, base_spread, M, N, ...), 全变体共享。

    返回:
      - ``variants``: 每变体一行 {name, overrides, summary 摘要指标}
      - ``best``: 按每期 Sharpe 最优的变体名及指标
      - ``deflated``: 最优变体的 Deflated Sharpe (变体数 < 2 时为 None)
      - ``results``: {name: 完整回测结果}; ``keep_results=False`` 时只留最优变体
    """
    if not variants:
        raise ValueError("variants 不能为空")
    base = base_config or ScoreStrategyConfig()

    # 跑任何变体之前先校验全部变体: 字段白名单 + 引擎枚举 fail-fast。
    parsed: list[tuple[str, dict[str, Any], ScoreStrategyConfig]] = []
    seen_names: set[str] = set()
    for raw in variants:
        overrides = dict(raw)
        name = str(overrides.pop("name", "") or "").strip()
        _validate_sweep_fields(overrides.keys())
        overrides = _normalize_override_values(overrides)
        cfg = replace(base, pre_filter_prices=False, **overrides)
        validate_strategy_config(cfg)
        if not name:
            name = _variant_name(overrides)
        if name in seen_names:
            raise ValueError(f"变体名重复: {name}")
        seen_names.add(name)
        parsed.append((name, overrides, cfg))

    # 共享缓存: 定价快照跨变体复用 + 行情/条款运行时缓存跨变体复用。
    snapshot_cache = engine_kwargs.pop("pricing_snapshot_cache", None)
    if snapshot_cache is None:
        snapshot_cache = {}
    shared_provider = _BacktestCacheProvider(
        provider,
        start_date=start_date,
        end_date=end_date,
        price_lookback_days=base.price_lookback_days,
        execution_lookahead_days=base.execution_lookahead_days,
        vol_window_days=int(engine_kwargs.get("vol_window_days", 21)),
    )
    risk_free = float(engine_kwargs.get("r", 0.022))
    rf_per_period = risk_free / _periods_per_year(base.rebalance_freq)

    rows: list[dict[str, Any]] = []
    results: dict[str, dict[str, Any]] = {}
    period_returns_by_name: dict[str, list[float]] = {}
    total = len(parsed)
    for done, (name, overrides, cfg) in enumerate(parsed, start=1):
        result = backtest_score_strategy(
            shared_provider,
            bond_codes,
            start_date=start_date,
            end_date=end_date,
            config=cfg,
            terms_cache=terms_cache,
            admission_config=admission_config,
            use_runtime_cache=False,       # 共享外层 _BacktestCacheProvider
            pricing_snapshot_cache=snapshot_cache,
            stage_cb=stage_cb,
            cancel_cb=cancel_cb,
            **engine_kwargs,
        )
        period_returns = [
            r for r in (
                finite_float(p.get("period_return")) for p in result["periods"])
            if r is not None
        ]
        period_returns_by_name[name] = period_returns
        summary = result.get("summary") or {}
        rows.append({
            "name": name,
            "overrides": overrides,
            # 变体间可比的统一口径: 每期收益的非年化 Sharpe (DSR 同口径)
            "sharpe_period": _period_sharpe(period_returns, rf_per_period),
            "final_equity": summary.get("final_equity"),
            "total_return": summary.get("total_return"),
            "annualized_return": summary.get("annualized_return"),
            "sharpe": summary.get("sharpe"),
            "max_drawdown": summary.get("max_drawdown"),
            "excess_return": summary.get("excess_return"),
            "avg_turnover": summary.get("avg_turnover"),
            "avg_cash_weight": summary.get("avg_cash_weight"),
            "total_cost": summary.get("total_cost"),
            "periods": summary.get("periods"),
        })
        results[name] = result
        if progress_cb:
            progress_cb(done, total)

    best_row = max(
        rows,
        key=lambda row: (
            row["sharpe_period"] if row["sharpe_period"] is not None else -float("inf")),
    )
    trial_sharpes = [
        row["sharpe_period"] for row in rows if row["sharpe_period"] is not None
    ]
    deflated = backtest_stats.deflated_sharpe(
        period_returns_by_name.get(best_row["name"]) or [],
        trial_sharpes,
        rf_per_period=rf_per_period,
    )
    if not keep_results:
        results = {best_row["name"]: results[best_row["name"]]}
    return {
        "start_date": start_date,
        "end_date": end_date,
        "n_variants": total,
        "base_config": base,
        "variants": rows,
        "best": dict(best_row),
        "deflated": deflated,
        "results": results,
    }


def sweep_pde_strategy(
    provider: DataProvider,
    bond_codes: list[str],
    *,
    start_date: date,
    end_date: date,
    variants: list[dict[str, Any]],
    base_config: PDEStrategyConfig | None = None,
    **kwargs,
) -> dict[str, Any]:
    """PDE 策略参数扫描主入口，禁止旧机会分/全池组合变体。"""
    for raw in variants:
        unknown = set(raw) - {"name"} - PDE_PORTFOLIO_SWEEP_FIELDS
        if unknown:
            raise ValueError(f"PDE扫描不支持字段: {sorted(unknown)}")
        signal = raw.get("rank_signal")
        if signal is not None and signal != "deviation":
            raise ValueError(f"PDE扫描不支持排序信号: {signal}")
    return sweep_score_strategy(
        provider,
        bond_codes,
        start_date=start_date,
        end_date=end_date,
        variants=variants,
        base_config=base_config or PDEStrategyConfig(),
        **kwargs,
    )


def _validate_sweep_fields(fields) -> None:
    unknown = set(fields) - PORTFOLIO_SWEEP_FIELDS
    if unknown:
        raise ValueError(
            f"不可扫描的字段: {sorted(unknown)}; 仅允许组合层字段 "
            f"{sorted(PORTFOLIO_SWEEP_FIELDS)} (调仓频率/池模式/定价参数会击穿定价缓存, "
            "请作为独立回测分别跑)")


def _normalize_override_values(overrides: dict[str, Any]) -> dict[str, Any]:
    """dataclass 的 tuple 字段接受 list 输入 (CLI/JSON 友好)。"""
    fixed = dict(overrides)
    for key in ("min_confidence", "exclude_risk_tags"):
        if isinstance(fixed.get(key), list):
            fixed[key] = tuple(fixed[key])
    return fixed


def _variant_name(overrides: dict[str, Any]) -> str:
    if not overrides:
        return "base"
    return "·".join(f"{key}={overrides[key]}" for key in sorted(overrides))


def _period_sharpe(period_returns: list[float], rf_per_period: float) -> float | None:
    """逐期 (未年化) 夏普。

    此前这里调的是 ``backtest_stats.per_period_sharpe`` —— 那个函数**不存在**, 于是每次
    参数扫描都抛 AttributeError, 而且是在**第一个完整变体回测跑完之后**才崩, 代价先付了
    再失败。``annualized_sharpe(..., periods_per_year=1)`` 就是它: 年化因子取 1 时
    ``sqrt(1) = 1``, 公式退化成 ``(mean - rf) / std(ddof=1)``, 且样本 < 2 或恒定收益
    返回 NaN 的处置也一并复用, 不必再写第二份。
    """
    return backtest_stats.annualized_sharpe(
        period_returns, periods_per_year=1, rf_per_period=rf_per_period)
