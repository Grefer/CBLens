"""signal_eval 信号检验工具单测。

用已知性质的合成面板验证 Rank-IC / 分位数 / 横截面去均值 / 聚合时序 的正确性。
"""
import math

import pytest

from convertible_bond.signal_eval import (
    aggregate_series,
    compare_factors,
    cross_sectional_zscore,
    format_ic_table,
    quantile_returns,
    rank_ic,
)


def _panel(date, factor_to_ret):
    """factor_to_ret: list of (factor_value, forward_return) -> 一个截面的观测。"""
    return [
        {"date": date, "bond_code": f"{date}_{i}", "f": fv, "forward_return": rv}
        for i, (fv, rv) in enumerate(factor_to_ret)
    ]


# ---------------- rank_ic ----------------

def test_rank_ic_perfect_positive():
    obs = []
    for d in ("d1", "d2"):
        obs += _panel(d, [(i, i) for i in range(12)])  # 因子与收益完全同序
    res = rank_ic(obs, "f")
    assert res.n_dates == 2
    assert res.mean_ic == pytest.approx(1.0)
    assert res.median_ic == pytest.approx(1.0)
    assert res.pct_positive == pytest.approx(1.0)


def test_rank_ic_perfect_negative():
    obs = _panel("d1", [(i, -i) for i in range(15)])
    res = rank_ic(obs, "f")
    assert res.mean_ic == pytest.approx(-1.0)
    assert res.pct_positive == pytest.approx(0.0)


def test_rank_ic_constant_factor_is_zero():
    # 因子恒定 -> 无区分力 -> IC 记 0 (而非 NaN/报错)
    obs = _panel("d1", [(5.0, i) for i in range(12)])
    res = rank_ic(obs, "f")
    assert res.mean_ic == pytest.approx(0.0)


def test_rank_ic_min_obs_filters_thin_dates():
    obs = _panel("thin", [(i, i) for i in range(5)])      # < min_obs 被剔除
    obs += _panel("fat", [(i, i) for i in range(20)])
    res = rank_ic(obs, "f", min_obs=10)
    assert res.n_dates == 1
    assert [d for d, _n, _ic in res.per_date] == ["fat"]


def test_rank_ic_ignores_nan_and_missing():
    rows = _panel("d1", [(i, i) for i in range(12)])
    rows.append({"date": "d1", "bond_code": "bad1", "f": float("nan"),
                 "forward_return": 1.0})
    rows.append({"date": "d1", "bond_code": "bad2", "forward_return": 1.0})  # 缺因子
    res = rank_ic(rows, "f")
    assert res.per_date[0][1] == 12      # 只用 12 个有效配对
    assert res.mean_ic == pytest.approx(1.0)


def test_rank_ic_empty_panel():
    res = rank_ic([], "f")
    assert res.n_dates == 0
    assert math.isnan(res.mean_ic)


# ---------------- quantile_returns ----------------

def test_quantile_monotone_spread_positive():
    obs = _panel("d1", [(i, float(i)) for i in range(10)])
    res = quantile_returns(obs, "f", n_quantiles=5)
    assert res.quantile_counts == [2, 2, 2, 2, 2]
    # 单调递增, Q5 均值 > Q1 均值
    assert res.quantile_means == sorted(res.quantile_means)
    assert res.spread > 0
    assert res.quantile_means[0] == pytest.approx(0.5)   # {0,1}
    assert res.quantile_means[-1] == pytest.approx(8.5)  # {8,9}
    assert res.spread == pytest.approx(8.0)


def test_quantile_anti_spread_negative():
    obs = _panel("d1", [(i, float(-i)) for i in range(10)])
    res = quantile_returns(obs, "f", n_quantiles=5)
    assert res.spread < 0


def test_quantile_pools_across_dates():
    obs = _panel("d1", [(i, float(i)) for i in range(10)])
    obs += _panel("d2", [(i, float(i)) for i in range(10)])
    res = quantile_returns(obs, "f", n_quantiles=5)
    assert res.n_dates == 2
    assert sum(res.quantile_counts) == 20


def test_quantile_requires_min_obs():
    obs = _panel("d1", [(i, float(i)) for i in range(3)])  # < n_quantiles
    res = quantile_returns(obs, "f", n_quantiles=5)
    assert res.n_dates == 0


# ---------------- cross_sectional_zscore ----------------

def test_zscore_demeans_per_date():
    obs = _panel("d1", [(0.0, 0.0), (10.0, 0.0)])  # mean=5, sd(ddof0)=5
    out = cross_sectional_zscore(obs, "f", out_key="fz")
    zs = sorted(r["fz"] for r in out)
    assert zs == pytest.approx([-1.0, 1.0])


def test_zscore_default_key_and_constant_group_none():
    obs = _panel("d1", [(3.0, 0.0), (3.0, 0.0)])   # sd=0 -> None
    out = cross_sectional_zscore(obs, "f")
    assert all(r["f_z"] is None for r in out)


def test_zscore_buckets_within_date():
    obs = [
        {"date": "d1", "bucket": "lo", "f": 0.0, "forward_return": 0.0},
        {"date": "d1", "bucket": "lo", "f": 2.0, "forward_return": 0.0},
        {"date": "d1", "bucket": "hi", "f": 100.0, "forward_return": 0.0},
        {"date": "d1", "bucket": "hi", "f": 102.0, "forward_return": 0.0},
    ]
    out = cross_sectional_zscore(obs, "f", out_key="fz", bucket_key="bucket")
    by_code = {(r["bucket"], r["f"]): r["fz"] for r in out}
    # 每个桶内独立标准化 -> 两桶的 z 同形
    assert by_code[("lo", 0.0)] == pytest.approx(by_code[("hi", 100.0)])
    assert by_code[("lo", 2.0)] == pytest.approx(by_code[("hi", 102.0)])


# ---------------- aggregate_series ----------------

def test_aggregate_series_median_and_positive_share():
    obs = [
        {"date": "d2", "deviation": 0.2},
        {"date": "d2", "deviation": 0.0},
        {"date": "d2", "deviation": -0.1},
        {"date": "d1", "deviation": 0.5},
    ]
    series = aggregate_series(obs, "deviation", agg="median")
    assert [p.date for p in series] == ["d1", "d2"]  # 按日期排序
    d2 = series[1]
    assert d2.n == 3
    assert d2.value == pytest.approx(0.0)            # median of {-0.1,0,0.2}
    assert d2.pct_positive == pytest.approx(1 / 3)


def test_aggregate_series_mean():
    obs = [{"date": "d1", "deviation": v} for v in (0.0, 0.1, 0.2)]
    series = aggregate_series(obs, "deviation", agg="mean")
    assert series[0].value == pytest.approx(0.1)


def test_aggregate_series_bad_agg():
    with pytest.raises(ValueError):
        aggregate_series([], "deviation", agg="p99")


# ---------------- compare_factors / formatting ----------------

def test_compare_factors_and_table():
    obs = _panel("d1", [(i, float(i)) for i in range(12)])
    for r, i in zip(obs, range(12)):
        r["neg"] = -i                                 # 反向因子
    res = compare_factors(obs, ["f", "neg"])
    assert res["f"].mean_ic == pytest.approx(1.0)
    assert res["neg"].mean_ic == pytest.approx(-1.0)
    table = format_ic_table(res)
    assert "factor" in table and "neg" in table


def test_quantile_invalid_n():
    with pytest.raises(ValueError):
        quantile_returns([], "f", n_quantiles=1)
