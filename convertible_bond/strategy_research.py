"""策略研究编排：小规模单因素邻域、同候选池三臂对照与持仓重合度。"""
from __future__ import annotations

from dataclasses import replace
from datetime import date
from typing import Any

from .data_providers.base import DataProvider
from .strategy_backtest import PDEStrategyConfig, validate_strategy_config
from .strategy_sweep import sweep_pde_strategy, sweep_score_strategy


BASELINE_NAME = "PDE TopN"
CANDIDATE_POOL_NAME = "同候选池等权"
VALUATION_NAME = "PDE＋估值缩放"


def _validate_pde_base(config: PDEStrategyConfig) -> None:
    validate_strategy_config(config)
    if (config.rank_signal != "deviation" or config.holding_mode != "top_score"
            or config.funding_mode != "reserve_cash"):
        raise ValueError("研究基线必须为 PDE 偏差排序、Top N 持仓、缺口留现金")


def build_neighborhood_variants(config: PDEStrategyConfig) -> list[dict[str, Any]]:
    """基线加持仓数、便宜度、成本各两个邻点；单因素最多七组，不做笛卡尔积。"""
    _validate_pde_base(config)
    variants: list[dict[str, Any]] = [{"name": BASELINE_NAME}]
    top_step = max(1, round(config.top_n * 0.2))
    values = {
        "top_n": [max(1, config.top_n - top_step), config.top_n + top_step],
        "min_relative_cheapness": (
            [0.025, 0.05] if config.min_relative_cheapness is None
            else [max(0.0, config.min_relative_cheapness - 0.01),
                  config.min_relative_cheapness + 0.01]
        ),
        "transaction_cost": [max(0.0, config.transaction_cost - 0.001),
                             config.transaction_cost + 0.001],
    }
    for field, neighbors in values.items():
        seen = {getattr(config, field)}
        for value in neighbors:
            value = value if isinstance(value, int) else round(value, 10)
            if value in seen:
                continue
            seen.add(value)
            if field == "top_n":
                label = f"持仓 {value} 只"
            elif field == "min_relative_cheapness":
                label = f"便宜度下限 {value * 100:g}pp"
            else:
                label = f"单边成本 {value * 100:g}%"
            variants.append({"name": label, field: value})
    return variants


def _period_holdings(result: dict[str, Any]) -> dict[tuple[str, str], set[str]]:
    return {
        (str(period.get("start_date")), str(period.get("end_date"))): {
            str(position["bond_code"])
            for position in period.get("positions") or []
            if position.get("bond_code")
        }
        for period in result.get("periods") or []
    }


def holding_overlap(reference: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """同一持有区间实际持仓的 Jaccard 均值；双方全现金不作为持仓相同的证据。"""
    base, other = _period_holdings(reference), _period_holdings(result)
    overlaps = []
    empty = 0
    for key in sorted(base.keys() & other.keys()):
        union = base[key] | other[key]
        if not union:
            empty += 1
            continue
        overlaps.append(len(base[key] & other[key]) / len(union))
    return {
        "mean": sum(overlaps) / len(overlaps) if overlaps else None,
        "compared_periods": len(overlaps),
        "both_cash_periods": empty,
        "unmatched_periods": len(base.keys() ^ other.keys()),
    }


def _enrich(result: dict[str, Any], kind: str) -> dict[str, Any]:
    baseline = result["results"][BASELINE_NAME]
    for row in result["variants"]:
        detail = holding_overlap(baseline, result["results"][row["name"]])
        row["holding_overlap"] = detail["mean"]
        row["holding_overlap_detail"] = detail
    result["kind"] = kind
    result["base_name"] = BASELINE_NAME
    return result


def run_strategy_neighborhood(
    provider: DataProvider,
    bond_codes: list[str],
    *,
    start_date: date,
    end_date: date,
    base_config: PDEStrategyConfig | None = None,
    **sweep_kwargs,
) -> dict[str, Any]:
    """共用一次 PDE 面板的邻域比较；保留所有变体结果及现有 DSR 口径。"""
    config = base_config or PDEStrategyConfig()
    variants = build_neighborhood_variants(config)
    sweep_kwargs.pop("keep_results", None)
    result = sweep_pde_strategy(
        provider, bond_codes, start_date=start_date, end_date=end_date,
        base_config=config, variants=variants, keep_results=True, **sweep_kwargs,
    )
    return _enrich(result, "neighborhood")


def run_strategy_comparison(
    provider: DataProvider,
    bond_codes: list[str],
    *,
    start_date: date,
    end_date: date,
    base_config: PDEStrategyConfig | None = None,
    **sweep_kwargs,
) -> dict[str, Any]:
    """三臂分离候选过滤、Top N 排序与估值缩放贡献；全池仅作研究对照。"""
    config = base_config or PDEStrategyConfig()
    _validate_pde_base(config)
    # 两个基线都恒定仓位；第三臂只增加估值缩放。成交、费用、现金与候选闸一致。
    config = replace(config, exposure_mode="full", max_holdings=None)
    variants = [
        {"name": BASELINE_NAME},
        {"name": CANDIDATE_POOL_NAME, "holding_mode": "pool"},
        {"name": VALUATION_NAME, "exposure_mode": "valuation"},
    ]
    sweep_kwargs.pop("keep_results", None)
    # 仅此显式研究入口允许构造全候选池对照，正常 PDE 扫描白名单保持不变。
    result = sweep_score_strategy(
        provider, bond_codes, start_date=start_date, end_date=end_date,
        base_config=config, variants=variants, keep_results=True, **sweep_kwargs,
    )
    return _enrich(result, "comparison")
