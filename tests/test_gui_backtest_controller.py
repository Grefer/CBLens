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
