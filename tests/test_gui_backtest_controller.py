from datetime import date

import numpy as np
import pytest

from convertible_bond.gui.controllers.backtest import BacktestMixin


def test_backtest_metrics_capture_latest_and_extreme_deviation():
    metrics = BacktestMixin._compute_backtest_metrics(
        [
            date(2026, 1, 31),
            date(2026, 2, 28),
            date(2026, 3, 31),
        ],
        np.array([105.0, 95.0, 120.0]),
        np.array([100.0, 100.0, 100.0]),
        [0.20, 0.21, 0.22],
        np.array([0.25, np.nan, 0.30]),
        bond_floors=[92.0, 93.0, 94.0],
        parities=[88.0, 90.0, 95.0],
    )

    assert metrics["latest"]["date"] == date(2026, 3, 31)
    assert metrics["latest"]["dev"] == pytest.approx(0.20)
    assert metrics["latest"]["bond_floor"] == pytest.approx(94.0)
    assert metrics["max_abs_idx"] == 2
    assert metrics["hit_rate"] == pytest.approx(2 / 3)
    assert metrics["iv_hv_pp"] == pytest.approx(6.5)


def test_out_of_band_iv_days_are_counted_and_surfaced():
    """IV 反解失败的天数要**记账并显示** —— 丢样本是单向的.

    模型可解带是 `[price(σ=5%), price(σ=200%)]`; 市价高于上界的那些天正是"贵"的
    那些天, 全被丢掉之后 IV-HV 的均值只代表便宜的那一半。实测 8 个采样点里 4 点
    出带, 界面照常报「IV-HV +25.16pp」而不说少了一半样本。

    这条守护端到端跑: 回测要产出 `iv_failures` 分档计数, metrics 要产出
    `iv_used`/`iv_total`, 统计块要把它写进那一格。
    """
    import sys
    from datetime import date, timedelta

    import numpy as np

    sys.path.insert(0, "tests")
    from test_pricer import FakeProvider

    from convertible_bond.backtest import backtest_theoretical_price
    from convertible_bond.data_providers import BondTerms
    from convertible_bond.gui.controllers.backtest import BacktestMixin

    start, end = date(2025, 1, 1), date(2025, 8, 31)
    bond, stock = [], []
    d = start
    while d <= end:
        if d.weekday() < 5:
            # 前四个月在带内, 后四个月市价 400 远高于 σ=200% 的模型价
            bond.append((d, 110.0 if d.month <= 4 else 400.0))
            stock.append((d, 50.0 + 0.02 * (d - start).days))
        d += timedelta(days=1)
    terms = BondTerms(
        sec_name="测试债", underlying_code="000001.SZ",
        issue_date=date(2020, 7, 30), maturity_date=date(2026, 7, 30),
        face_value=100.0, conversion_price=52.77, redemption_price=107.0,
        call_trigger_pct=130.0, put_trigger_pct=70.0, put_obs_months=48.0,
        coupon_rates=(0.003, 0.004, 0.008, 0.015, 0.018, 0.02), close=110.0,
    )
    result = backtest_theoretical_price(
        "123001.SZ", start_date=start, end_date=end, freq="M", M=80, N=200,
        solve_iv=True, point_in_time=False,
        provider=FakeProvider("123001.SZ", "000001.SZ", terms, bond, stock),
    )

    assert result["iv_failures"] == {"above_ceiling": 4}, result["iv_failures"]

    metrics = BacktestMixin._compute_backtest_metrics(
        result["dates"],
        np.array(result["theo_prices"], dtype=float),
        np.array(result["market_prices"], dtype=float),
        result["sigmas"],
        np.array(result["ivs"], dtype=float),
    )
    assert (metrics["iv_used"], metrics["iv_total"]) == (4, 8)

    # 统计块必须把它写出来, 而全用上时不该加这条尾巴 (那句话就成了噪声)
    class _Var:
        def __init__(self): self.value = None
        def set(self, v): self.value = v

    class _App:
        _bt_stat_vars = {k: _Var() for k in
                         ("mean_dev", "rmse", "max_abs", "hit_rate", "corr", "iv_hv")}
        _bt_stat_labels = {}

    app = _App()
    BacktestMixin._update_backtest_stats(
        app, 0.01, 0.02, 0.03, 0.9, 0.95, metrics["iv_hv_pp"],
        metrics["iv_used"], metrics["iv_total"])
    assert app._bt_stat_vars["iv_hv"].value.endswith("(4/8 天)"), \
        app._bt_stat_vars["iv_hv"].value

    BacktestMixin._update_backtest_stats(app, 0.01, 0.02, 0.03, 0.9, 0.95, 1.5, 8, 8)
    assert app._bt_stat_vars["iv_hv"].value == "+1.50pp"
