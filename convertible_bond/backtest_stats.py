"""回测统计稳健性工具 (backtest_stats).

把"这个差异是不是噪声"从人工判断变成工具能回答的问题。提供:
  - 循环块自助 (circular block bootstrap) 的 Sharpe 置信区间与"为正概率";
  - 配对块自助的"跑赢基准概率"与超额收益 CI;
  - 滚动窗口 Sharpe (子区间稳定性)。

设计: 只依赖 numpy, 不触发取数, 可离线单测 (与 signal_eval 同风格)。块自助保留
时间序列的自相关结构 (普通 iid 自助会高估显著性)。块长默认 √n (经验法则)。

动机见 docs/research/2026-06-score-ic-and-valuation-timing.md: 17 个季度的 Sharpe
0.60 vs 0.40 在抽样噪声内 —— 点估计会骗人, 必须给离散度。
"""
from __future__ import annotations

import numpy as np


def _finite_array(returns) -> np.ndarray:
    arr = np.asarray([float(x) for x in returns if x is not None], dtype=float)
    return arr[np.isfinite(arr)]


_STD_EPS = 1e-12   # 收益序列标准差视为零的阈值: 防恒定/近恒定序列浮点残差炸出天文 Sharpe


def annualized_sharpe(returns, periods_per_year: float, rf_per_period: float = 0.0) -> float:
    r = _finite_array(returns)
    if r.size < 2:
        return float("nan")
    sd = float(r.std(ddof=1))
    if sd <= _STD_EPS:           # 恒定收益 → 无定义 (而非浮点残差放大成巨值)
        return float("nan")
    return float((r.mean() - rf_per_period) / sd * np.sqrt(periods_per_year))


def _default_block(n: int) -> int:
    return max(1, int(round(n ** 0.5)))


def _circular_block_indices(n: int, block: int, rng: np.random.Generator) -> np.ndarray:
    """循环块自助索引: 取 ceil(n/block) 个长度为 block 的环形连续块, 截到 n。"""
    n_blocks = -(-n // block)  # ceil
    starts = rng.integers(0, n, size=n_blocks)
    offsets = np.arange(block)
    idx = ((starts[:, None] + offsets[None, :]) % n).reshape(-1)[:n]
    return idx


def block_bootstrap_sharpe(
    returns,
    *,
    periods_per_year: float,
    rf_per_period: float = 0.0,
    n_boot: int = 1000,
    block: int | None = None,
    seed: int = 0,
    ci_level: float = 0.90,
) -> dict | None:
    """Sharpe 的循环块自助 CI 与为正概率。样本 < 4 返回 None。"""
    r = _finite_array(returns)
    if r.size < 4:
        return None
    block = block or _default_block(r.size)
    rng = np.random.default_rng(seed)
    stats = []
    for _ in range(n_boot):
        s = annualized_sharpe(r[_circular_block_indices(r.size, block, rng)],
                              periods_per_year, rf_per_period)
        if np.isfinite(s):
            stats.append(s)
    if not stats:
        return None
    arr = np.array(stats)
    lo, hi = (1 - ci_level) / 2, 1 - (1 - ci_level) / 2
    return {
        "point": annualized_sharpe(r, periods_per_year, rf_per_period),
        "ci_low": float(np.quantile(arr, lo)),
        "ci_high": float(np.quantile(arr, hi)),
        "prob_positive": float((arr > 0).mean()),
        "ci_level": ci_level,
        "block": block,
        "n_boot": len(stats),
    }


def block_bootstrap_excess(
    strategy_returns,
    benchmark_returns,
    *,
    n_boot: int = 1000,
    block: int | None = None,
    seed: int = 0,
    ci_level: float = 0.90,
) -> dict | None:
    """配对块自助: 策略 vs 基准的总超额 CI 与"跑赢概率"。

    按期配对重采样 (保留策略/基准同期对齐), 每次比较两条复利总收益。回答
    "这点超额是不是噪声"。两序列取等长前缀; 有效期 < 4 返回 None。
    """
    a = _finite_array(strategy_returns)
    b = _finite_array(benchmark_returns)
    n = min(a.size, b.size)
    if n < 4:
        return None
    a, b = a[:n], b[:n]
    block = block or _default_block(n)
    rng = np.random.default_rng(seed)
    diffs = np.empty(n_boot)
    beats = 0
    for i in range(n_boot):
        idx = _circular_block_indices(n, block, rng)
        strat_tot = float(np.prod(1.0 + a[idx]) - 1.0)
        bench_tot = float(np.prod(1.0 + b[idx]) - 1.0)
        diffs[i] = strat_tot - bench_tot
        beats += strat_tot > bench_tot
    lo, hi = (1 - ci_level) / 2, 1 - (1 - ci_level) / 2
    return {
        "point_excess": float(np.prod(1.0 + a) - 1.0) - float(np.prod(1.0 + b) - 1.0),
        "prob_beat_benchmark": beats / n_boot,
        "excess_ci_low": float(np.quantile(diffs, lo)),
        "excess_ci_high": float(np.quantile(diffs, hi)),
        "ci_level": ci_level,
        "block": block,
        "n_boot": n_boot,
    }


def rolling_sharpe(
    returns,
    *,
    window: int,
    periods_per_year: float,
    rf_per_period: float = 0.0,
) -> list[float]:
    """逐点滚动窗口 (1 年=window) 年化 Sharpe 序列; 样本不足窗口返回空。"""
    r = _finite_array(returns)
    window = max(2, int(window))
    if r.size < window:
        return []
    return [
        annualized_sharpe(r[end - window:end], periods_per_year, rf_per_period)
        for end in range(window, r.size + 1)
    ]


def summarize_stability(roll: list[float]) -> dict | None:
    """滚动 Sharpe 序列的稳健性摘要: 均值/最差/为正窗口占比。"""
    arr = _finite_array(roll)
    if arr.size == 0:
        return None
    return {
        "rolling_sharpe_mean": float(arr.mean()),
        "rolling_sharpe_min": float(arr.min()),
        "rolling_sharpe_pct_positive": float((arr > 0).mean()),
        "n_windows": int(arr.size),
    }
