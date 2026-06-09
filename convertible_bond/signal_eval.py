"""信号有效性评估工具 (signal_eval).

为任意横截面信号 (机会分 / deviation / 动量 / 低价 ...) 提供与 ``strategy_backtest``
解耦的**纯统计检验**, 用来回答"这个信号到底有没有预测力": 秩相关 (Rank-IC)、
分位数价差、横截面去均值 (相对化) 以及聚合时序 (可作转债大类择时指标)。

设计要点:
  - 只依赖 numpy / scipy, **不触发任何取数**; 观测面板由调用方 (快照 / 历史价) 预先拼好,
    因此本模块完全可离线单测、可复用。
  - 观测面板 = 一组 dict, 每条至少含日期键、收益键和一个或多个因子键::

        obs = [
            {"date": d, "bond_code": c, "score": s, "deviation": dv,
             "forward_return": r},
            ...,
        ]

典型用法::

    from convertible_bond.signal_eval import rank_ic, quantile_returns, aggregate_series
    print(rank_ic(obs, "score"))                       # 机会分预测力
    print(quantile_returns(obs, "score", n_quantiles=5))
    series = aggregate_series(obs, "deviation")        # 聚合中位偏差时序 (择时)

约定:
  - Rank-IC 逐日 (横截面) 计算 Spearman 相关, 再对时间序列求均值/中位/胜率/IR;
    IR(t) = mean_ic / (std_ic / sqrt(n_dates)), 是 IC 时间序列的 t 统计量。
  - 分位数 Q1 = 因子最低, QN = 因子最高; spread = QN 均值 - Q1 均值。
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Hashable, Sequence

import numpy as np
from scipy.stats import spearmanr

Observation = dict[str, Any]

DEFAULT_RETURN_KEY = "forward_return"
DEFAULT_DATE_KEY = "date"


def _finite(value: Any) -> float | None:
    """转 float, 非有限 (None/NaN/inf) 返回 None。"""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _group_by(
    observations: Sequence[Observation],
    key: str,
) -> dict[Hashable, list[Observation]]:
    groups: dict[Hashable, list[Observation]] = defaultdict(list)
    for row in observations:
        groups[row.get(key)].append(row)
    return groups


def _clean_pairs(
    rows: Sequence[Observation],
    factor_key: str,
    return_key: str,
) -> tuple[list[float], list[float]]:
    """抽出同时具备有限因子值与有限收益的配对。"""
    xs: list[float] = []
    ys: list[float] = []
    for row in rows:
        fv = _finite(row.get(factor_key))
        rv = _finite(row.get(return_key))
        if fv is None or rv is None:
            continue
        xs.append(fv)
        ys.append(rv)
    return xs, ys


@dataclass
class ICResult:
    """Rank-IC 汇总结果。"""

    factor: str
    mean_ic: float
    median_ic: float
    pct_positive: float
    ir_tstat: float
    n_dates: int
    per_date: list[tuple[Hashable, int, float]] = field(default_factory=list)

    def __str__(self) -> str:
        return (
            f"Rank-IC[{self.factor}] mean={self.mean_ic:+.3f} "
            f"median={self.median_ic:+.3f} %>0={self.pct_positive*100:.0f}% "
            f"IR(t)={self.ir_tstat:+.2f} n_dates={self.n_dates}"
        )


def rank_ic(
    observations: Sequence[Observation],
    factor_key: str,
    *,
    return_key: str = DEFAULT_RETURN_KEY,
    date_key: str = DEFAULT_DATE_KEY,
    min_obs: int = 10,
) -> ICResult:
    """逐日横截面 Spearman Rank-IC, 再对时间序列汇总。

    只统计当日有效配对数 >= ``min_obs`` 的截面。返回 :class:`ICResult`,
    其中 ``ir_tstat`` 是 IC 序列的 t 统计量 (均值/标准误)。
    """
    per_date: list[tuple[Hashable, int, float]] = []
    for d, rows in _group_by(observations, date_key).items():
        xs, ys = _clean_pairs(rows, factor_key, return_key)
        if len(xs) < min_obs:
            continue
        # 因子或收益无变化时 Spearman 无定义, 记 0 (无区分力)
        if len(set(xs)) < 2 or len(set(ys)) < 2:
            ic = 0.0
        else:
            ic = float(spearmanr(xs, ys).correlation)
            if not math.isfinite(ic):
                ic = 0.0
        per_date.append((d, len(xs), ic))

    per_date.sort(key=lambda t: (t[0] is None, t[0]))
    ics = np.array([t[2] for t in per_date], dtype=float)
    if ics.size == 0:
        return ICResult(factor_key, float("nan"), float("nan"),
                        float("nan"), float("nan"), 0, per_date)
    mean_ic = float(ics.mean())
    median_ic = float(np.median(ics))
    pct_pos = float((ics > 0).mean())
    if ics.size > 1 and ics.std(ddof=1) > 0:
        ir = mean_ic / (ics.std(ddof=1) / math.sqrt(ics.size))
    else:
        ir = float("nan")
    return ICResult(factor_key, mean_ic, median_ic, pct_pos, ir, ics.size, per_date)


@dataclass
class QuantileResult:
    """分位数收益结果。Q1 = 因子最低分位, QN = 最高。"""

    factor: str
    n_quantiles: int
    quantile_means: list[float]
    quantile_counts: list[int]
    spread: float          # QN - Q1
    n_dates: int

    def __str__(self) -> str:
        body = "  ".join(
            f"Q{i+1}={m*100:+.2f}%(n={c})"
            for i, (m, c) in enumerate(zip(self.quantile_means, self.quantile_counts))
        )
        return (f"Quantiles[{self.factor}] {body}  "
                f"spread(QN-Q1)={self.spread*100:+.2f}%  n_dates={self.n_dates}")


def quantile_returns(
    observations: Sequence[Observation],
    factor_key: str,
    *,
    n_quantiles: int = 5,
    return_key: str = DEFAULT_RETURN_KEY,
    date_key: str = DEFAULT_DATE_KEY,
    min_obs: int | None = None,
) -> QuantileResult:
    """按因子在**每个截面内**分 ``n_quantiles`` 档, 跨日汇总各档平均收益。

    用基于排序索引的等分 (对并列稳健), 因此即使因子取值离散也能均匀分档。
    ``min_obs`` 缺省为 ``n_quantiles`` (每档至少摊到 1 只)。
    """
    if n_quantiles < 2:
        raise ValueError("n_quantiles 必须 >= 2")
    floor = n_quantiles if min_obs is None else max(min_obs, n_quantiles)
    buckets: list[list[float]] = [[] for _ in range(n_quantiles)]
    n_dates = 0
    for _d, rows in _group_by(observations, date_key).items():
        pairs = [
            (fv, rv)
            for fv, rv in (
                (_finite(r.get(factor_key)), _finite(r.get(return_key))) for r in rows
            )
            if fv is not None and rv is not None
        ]
        if len(pairs) < floor:
            continue
        pairs.sort(key=lambda p: p[0])
        n = len(pairs)
        n_dates += 1
        for qi in range(n_quantiles):
            seg = pairs[int(qi * n / n_quantiles): int((qi + 1) * n / n_quantiles)]
            buckets[qi].extend(rv for _fv, rv in seg)
    means = [float(np.mean(b)) if b else float("nan") for b in buckets]
    counts = [len(b) for b in buckets]
    spread = (means[-1] - means[0]
              if math.isfinite(means[-1]) and math.isfinite(means[0]) else float("nan"))
    return QuantileResult(factor_key, n_quantiles, means, counts, spread, n_dates)


def cross_sectional_zscore(
    observations: Sequence[Observation],
    factor_key: str,
    *,
    out_key: str | None = None,
    date_key: str = DEFAULT_DATE_KEY,
    bucket_key: str | None = None,
) -> list[Observation]:
    """对因子做**当期截面去均值标准化** (相对化), 返回带新键的副本列表。

    用于把"绝对偏差"转成"相对同侪的偏差"——当 level 由时变的全市场因子主导时
    (如转债估值溢价周期), 相对量才可能含横截面 alpha。``bucket_key`` 非空时在
    (日期, 桶) 内分组标准化 (如同价位桶)。无法标准化的观测 ``out_key`` 置 None。
    """
    target = out_key or f"{factor_key}_z"
    group_key = (lambda r: (r.get(date_key), r.get(bucket_key))) if bucket_key \
        else (lambda r: r.get(date_key))
    groups: dict[Hashable, list[Observation]] = defaultdict(list)
    for row in observations:
        groups[group_key(row)].append(row)

    out: list[Observation] = []
    for rows in groups.values():
        vals = [_finite(r.get(factor_key)) for r in rows]
        finite = [v for v in vals if v is not None]
        mu = float(np.mean(finite)) if finite else float("nan")
        sd = float(np.std(finite, ddof=0)) if len(finite) > 1 else 0.0
        for row, v in zip(rows, vals):
            copy = dict(row)
            copy[target] = ((v - mu) / sd) if (v is not None and sd > 0) else None
            out.append(copy)
    return out


@dataclass
class SeriesPoint:
    date: Hashable
    n: int
    value: float
    pct_positive: float
    p25: float
    p75: float


def aggregate_series(
    observations: Sequence[Observation],
    value_key: str,
    *,
    agg: str = "median",
    date_key: str = DEFAULT_DATE_KEY,
) -> list[SeriesPoint]:
    """逐日聚合某个量 (默认 deviation 中位数) 成时间序列。

    本项目里 ``aggregate_series(obs, "deviation")`` 的中位序列即**转债大类估值
    /择时指标**: 中位偏差高=市场贵, 压到 0 或转负=便宜。
    """
    if agg not in ("median", "mean"):
        raise ValueError("agg 仅支持 'median' / 'mean'")
    points: list[SeriesPoint] = []
    for d, rows in _group_by(observations, date_key).items():
        vals = np.array([v for v in (_finite(r.get(value_key)) for r in rows)
                         if v is not None], dtype=float)
        if vals.size == 0:
            continue
        center = float(np.median(vals)) if agg == "median" else float(vals.mean())
        points.append(SeriesPoint(
            date=d, n=int(vals.size), value=center,
            pct_positive=float((vals > 0).mean()),
            p25=float(np.percentile(vals, 25)),
            p75=float(np.percentile(vals, 75)),
        ))
    points.sort(key=lambda p: (p.date is None, p.date))
    return points


def compare_factors(
    observations: Sequence[Observation],
    factor_keys: Sequence[str],
    *,
    return_key: str = DEFAULT_RETURN_KEY,
    date_key: str = DEFAULT_DATE_KEY,
    min_obs: int = 10,
) -> dict[str, ICResult]:
    """对多个因子批量算 Rank-IC, 返回 {因子名: ICResult}。"""
    return {
        f: rank_ic(observations, f, return_key=return_key,
                   date_key=date_key, min_obs=min_obs)
        for f in factor_keys
    }


def format_ic_table(results: dict[str, ICResult]) -> str:
    """把 compare_factors 的结果格式化成对齐表格字符串。"""
    lines = [f"{'factor':<22}{'IC_mean':>9}{'IC_med':>9}{'%>0':>6}{'IR(t)':>8}{'n':>5}"]
    for name, r in results.items():
        lines.append(
            f"{name:<22}{r.mean_ic:>+9.3f}{r.median_ic:>+9.3f}"
            f"{r.pct_positive*100:>5.0f}%{r.ir_tstat:>+8.2f}{r.n_dates:>5}"
        )
    return "\n".join(lines)
