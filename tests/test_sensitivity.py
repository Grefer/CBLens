"""compute_sensitivity_grid 烟雾测试."""
from datetime import date

import numpy as np
import pytest

from convertible_bond.pricer import UniversalCBPricer
from convertible_bond.sensitivity import compute_sensitivity_grid


@pytest.fixture
def pricer_kwargs():
    return dict(
        S0=55.0,
        K=52.77,
        current_date=date(2026, 4, 20),
        maturity_date=date(2026, 7, 30),
        issue_date=date(2020, 7, 30),
        conversion_start_date=date(2021, 2, 6),
        coupon_rates=(0.003, 0.004, 0.008, 0.015, 0.018, 0.02),
        redemption_price=107.0,
    )


@pytest.fixture
def model_kwargs():
    return dict(r=0.022, base_spread=0.03, p_down=0.0, distress_k=0.0,
                M=100, N=200)


class TestSensitivityGrid:
    def test_grid_shape_matches_inputs(self, pricer_kwargs, model_kwargs):
        s_grid = np.linspace(40, 70, 4)
        sig_grid = np.linspace(0.15, 0.45, 3)
        grid = compute_sensitivity_grid(
            pricer_kwargs, model_kwargs,
            s_grid=s_grid, sigma_grid=sig_grid, max_workers=2,
        )
        assert grid.shape == (3, 4)
        assert np.all(np.isfinite(grid))

    def test_progress_callback_called_for_each_cell(self, pricer_kwargs, model_kwargs):
        s_grid = np.array([50.0, 60.0])
        sig_grid = np.array([0.2, 0.3])
        seen = []
        compute_sensitivity_grid(
            pricer_kwargs, model_kwargs,
            s_grid=s_grid, sigma_grid=sig_grid, max_workers=1,
            progress_cb=lambda done, total: seen.append((done, total)),
        )
        assert len(seen) == 4
        assert seen[-1] == (4, 4)

    def test_grid_matches_direct_pricer(self, pricer_kwargs, model_kwargs):
        s_grid = np.array([55.0])
        sig_grid = np.array([0.28])
        grid = compute_sensitivity_grid(
            pricer_kwargs, model_kwargs,
            s_grid=s_grid, sigma_grid=sig_grid, max_workers=1,
        )
        direct = UniversalCBPricer(**pricer_kwargs).price(
            sigma=0.28, **{k: v for k, v in model_kwargs.items() if k != "sigma"})
        assert abs(grid[0, 0] - direct) < 1e-6
