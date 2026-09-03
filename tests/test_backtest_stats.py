"""backtest_stats 统计稳健性工具单测 (合成序列, 已知性质)。"""
import math

import numpy as np
import pytest

from convertible_bond.backtest_stats import (
    annualized_sharpe,
    block_bootstrap_excess,
    block_bootstrap_sharpe,
    rolling_sharpe,
    summarize_stability,
)


def test_annualized_sharpe_basic():
    # 恒定收益 → 标准差 0 → nan (无定义)
    assert math.isnan(annualized_sharpe([0.01] * 10, 12))
    # 少于 2 个 → nan
    assert math.isnan(annualized_sharpe([0.01], 12))


def test_annualized_sharpe_sign_and_scale():
    rng = np.random.default_rng(1)
    pos = rng.normal(0.01, 0.02, 200)
    s = annualized_sharpe(pos, 252)
    assert s > 0
    assert math.isclose(s, (pos.mean()) / pos.std(ddof=1) * math.sqrt(252), rel_tol=1e-9)


def test_bootstrap_sharpe_ci_brackets_point_and_small_sample_none():
    rng = np.random.default_rng(2)
    rets = rng.normal(0.008, 0.02, 120)
    res = block_bootstrap_sharpe(rets, periods_per_year=252, n_boot=400, seed=7)
    assert res is not None
    assert res["ci_low"] <= res["point"] <= res["ci_high"]
    assert 0.0 <= res["prob_positive"] <= 1.0
    # 强正收益 → 为正概率高
    assert res["prob_positive"] > 0.8
    # 样本不足
    assert block_bootstrap_sharpe([0.01, 0.02, 0.03], periods_per_year=12) is None


def test_bootstrap_sharpe_reproducible_with_seed():
    rng = np.random.default_rng(3)
    rets = rng.normal(0.005, 0.03, 100)
    a = block_bootstrap_sharpe(rets, periods_per_year=252, n_boot=300, seed=42)
    b = block_bootstrap_sharpe(rets, periods_per_year=252, n_boot=300, seed=42)
    assert a == b


def test_bootstrap_excess_prob_beat():
    n = 60
    strat = [0.02] * n          # 策略恒优于基准
    bench = [0.01] * n
    res = block_bootstrap_excess(strat, bench, n_boot=300, seed=5)
    assert res is not None
    assert res["point_excess"] > 0
    assert res["prob_beat_benchmark"] == pytest.approx(1.0)
    # 反过来必败
    res2 = block_bootstrap_excess(bench, strat, n_boot=300, seed=5)
    assert res2["prob_beat_benchmark"] == pytest.approx(0.0)


def test_bootstrap_excess_noise_prob_non_degenerate():
    # 同分布无系统性差异 → 跑赢概率非退化 (不锁死在 0/1), 即"不显著"
    rng = np.random.default_rng(11)
    a = list(rng.normal(0.0, 0.02, 200))
    b = list(rng.normal(0.0, 0.02, 200))
    res = block_bootstrap_excess(a, b, n_boot=600, seed=9)
    assert 0.05 < res["prob_beat_benchmark"] < 0.95


def test_bootstrap_excess_short_returns_none():
    assert block_bootstrap_excess([0.01, 0.02], [0.0, 0.0]) is None


def test_rolling_sharpe_and_summary():
    rng = np.random.default_rng(4)
    rets = list(rng.normal(0.01, 0.02, 50))
    roll = rolling_sharpe(rets, window=12, periods_per_year=12)
    assert len(roll) == 50 - 12 + 1
    summ = summarize_stability(roll)
    assert summ["n_windows"] == len(roll)
    assert summ["rolling_sharpe_min"] <= summ["rolling_sharpe_mean"]
    assert 0.0 <= summ["rolling_sharpe_pct_positive"] <= 1.0
    # 样本不足窗口
    assert rolling_sharpe([0.01] * 5, window=12, periods_per_year=12) == []
    assert summarize_stability([]) is None


def test_block_bootstrap_pairs_by_period_not_by_compacted_index():
    """配对块自助必须**按期**配对 —— 各自剔 NaN 再按下标配对会静默错位。

    docstring 承诺的是"保留策略/基准同期对齐", 而实现曾是各自过一遍
    ``_finite_array`` (它把缺失项**压缩掉**, 不是留空), 再 ``[:n]`` 截齐: 策略在
    第 3 期缺一个值, 从第 4 期起两条序列就整体错位一格, 后面每一次配对比较照常算得出
    一个数, 没有任何东西会报错。

    缺失不是假想: 某一期基准取不到可成交成分时 ``benchmark_return`` 就是 None,
    而策略那一期有值。

    判据取**极端反相**的两条序列: 正确配对时每一期超额都是 +0.20, 而错位一格之后
    策略的正收益会去对上基准的正收益, 超额塌向 0。
    """
    from convertible_bond.backtest_stats import block_bootstrap_excess

    strat = [0.30, -0.10, 0.30, -0.10, 0.30, -0.10, 0.30, -0.10]
    bench = [0.10, -0.30, 0.10, -0.30, 0.10, -0.30, 0.10, -0.30]
    clean = block_bootstrap_excess(strat, bench, n_boot=200, seed=7)
    assert clean is not None

    # 策略第 3 期缺值 —— 正确处置是**把这一对整个丢掉**, 而不是把策略后面的值提前一格。
    holed = list(strat)
    holed[2] = None
    got = block_bootstrap_excess(holed, bench, n_boot=200, seed=7)
    assert got is not None
    assert got["n_obs"] == 7, f"配对后应剩 7 期, 实际 {got['n_obs']}"

    expected = block_bootstrap_excess(
        [x for i, x in enumerate(strat) if i != 2],
        [x for i, x in enumerate(bench) if i != 2],
        n_boot=200, seed=7)
    assert got["point_excess"] == pytest.approx(expected["point_excess"]), (
        "缺一期之后的配对结果与'把那一对整个删掉'不一致 —— 说明剔除是各自独立发生的")
