"""
UniversalCBPricer 单元测试

覆盖:
- 回归测试 (已知参数 → 已知价格)
- 边界条件 (T→0, S→0, S→∞)
- 应计利息与票息
- 输入校验
- 辅助函数
"""
import sys, os
import pytest
import numpy as np
from datetime import date, timedelta

# 确保项目根目录在 sys.path 中
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from CB import (
    UniversalCBPricer,
    to_date,
    parse_coupon,
    DEFAULT_COUPON_RATES,
    DEFAULT_FACE_VALUE,
    DEFAULT_REDEMPTION_PRICE,
)


# ── 公共 fixture ──────────────────────────────────────────
@pytest.fixture
def base_pricer():
    """标准测试用例: 模拟一只典型可转债."""
    return UniversalCBPricer(
        S0=55.0, K=52.77,
        current_date=date(2026, 4, 20),
        maturity_date=date(2026, 7, 30),
        issue_date=date(2020, 7, 30),
        conversion_start_date=date(2021, 2, 6),
        coupon_rates=(0.003, 0.004, 0.008, 0.015, 0.018, 0.02),
        redemption_price=107.0,
    )


# ── 1. 回归测试 ──────────────────────────────────────────
class TestRegression:
    """确保已知参数下理论价格不漂移."""

    def test_base_case_price_range(self, base_pricer):
        """基础用例: 近到期深度 ITM, 价格应在合理范围."""
        price = base_pricer.price(sigma=0.28, r=0.022, base_spread=0.03,
                                  distress_k=0.05, p_down=0.0, M=200, N=500)
        # 转换价值 ≈ 55 * (100/52.77) ≈ 104.2, 加上票息应略高
        assert 100 < price < 120, f"价格 {price:.3f} 超出预期范围"

    def test_deep_otm_near_bond_floor(self):
        """深度 OTM (S << K), 价格应接近纯债价值."""
        pricer = UniversalCBPricer(
            S0=20.0, K=52.77,
            current_date=date(2025, 1, 1),
            maturity_date=date(2026, 7, 30),
            issue_date=date(2020, 7, 30),
            conversion_start_date=date(2021, 2, 6),
            redemption_price=107.0,
        )
        price = pricer.price(sigma=0.28, r=0.022, base_spread=0.03,
                             distress_k=0.0, p_down=0.0, M=200, N=500)
        bond_floor = pricer.bond_floor_value(date(2025, 1, 1), 0.052)
        # OTM 价格应 >= 纯债底, 但不会太高
        assert price >= bond_floor * 0.95, f"OTM 价格 {price:.3f} 低于纯债底 {bond_floor:.3f}"

    def test_deep_itm_near_conversion(self):
        """深度 ITM (S >> K), 价格应接近转换价值."""
        pricer = UniversalCBPricer(
            S0=120.0, K=52.77,
            current_date=date(2026, 4, 20),
            maturity_date=date(2026, 7, 30),
            issue_date=date(2020, 7, 30),
            conversion_start_date=date(2021, 2, 6),
            redemption_price=107.0,
        )
        price = pricer.price(sigma=0.28, r=0.022, base_spread=0.03,
                             distress_k=0.0, p_down=0.0, M=200, N=500)
        conv_value = 120.0 * (100.0 / 52.77)
        assert price >= conv_value * 0.99, \
            f"深度 ITM 价格 {price:.3f} 应接近转换价值 {conv_value:.3f}"

    def test_p_down_increases_price(self, base_pricer):
        """下修博弈概率 > 0 应增加转债价值 (给定相同参数)."""
        price_no_down = base_pricer.price(sigma=0.28, r=0.022, base_spread=0.03,
                                           distress_k=0.05, p_down=0.0, M=200, N=500)
        # 重新构建, 因为 S0 在 ATM 附近
        pricer_otm = UniversalCBPricer(
            S0=40.0, K=52.77,
            current_date=date(2025, 1, 1),
            maturity_date=date(2026, 7, 30),
            issue_date=date(2020, 7, 30),
            conversion_start_date=date(2021, 2, 6),
            redemption_price=107.0,
        )
        p0 = pricer_otm.price(sigma=0.28, r=0.022, base_spread=0.03,
                               distress_k=0.05, p_down=0.0, M=200, N=500)
        p1 = pricer_otm.price(sigma=0.28, r=0.022, base_spread=0.03,
                               distress_k=0.05, p_down=0.15, M=200, N=500)
        assert p1 >= p0, f"p_down=0.15 价格 {p1:.3f} 应 >= p_down=0 价格 {p0:.3f}"


# ── 2. 边界条件 ──────────────────────────────────────────
class TestBoundary:

    def test_very_short_maturity(self):
        """T → 0: 价格应接近 max(redemption, conversion_value)."""
        pricer = UniversalCBPricer(
            S0=55.0, K=52.77,
            current_date=date(2026, 7, 28),
            maturity_date=date(2026, 7, 30),
            issue_date=date(2020, 7, 30),
            conversion_start_date=date(2021, 2, 6),
            redemption_price=107.0,
        )
        price = pricer.price(sigma=0.28, r=0.022, base_spread=0.03,
                             distress_k=0.0, p_down=0.0, M=100, N=50)
        conv = 55.0 * (100.0 / 52.77)
        expected = max(107.0, conv)
        assert abs(price - expected) < 2.0, \
            f"T→0 价格 {price:.3f} 应接近 {expected:.3f}"

    def test_higher_sigma_increases_price(self):
        """更高的波动率应增加可转债价格 (期权性质)."""
        pricer = UniversalCBPricer(
            S0=50.0, K=52.77,
            current_date=date(2025, 1, 1),
            maturity_date=date(2026, 7, 30),
            issue_date=date(2020, 7, 30),
            conversion_start_date=date(2021, 2, 6),
            redemption_price=107.0,
        )
        p_low = pricer.price(sigma=0.15, r=0.022, base_spread=0.03,
                              distress_k=0.0, p_down=0.0, M=200, N=500)
        p_high = pricer.price(sigma=0.45, r=0.022, base_spread=0.03,
                               distress_k=0.0, p_down=0.0, M=200, N=500)
        assert p_high > p_low, \
            f"高 σ 价格 {p_high:.3f} 应 > 低 σ 价格 {p_low:.3f}"


# ── 3. 应计利息与票息 ────────────────────────────────────
class TestCoupons:

    def test_accrued_interest_at_issue(self, base_pricer):
        """发行日应计利息为 0."""
        assert base_pricer.accrued_interest(date(2020, 7, 30)) == 0.0

    def test_accrued_interest_positive_during_period(self, base_pricer):
        """期间应计利息 > 0."""
        ai = base_pricer.accrued_interest(date(2021, 1, 15))
        assert ai > 0

    def test_discrete_coupon_captures_payment(self, base_pricer):
        """跨付息日区间应捕获到票息."""
        # 第一期付息日 = issue_date + 1年 = 2021-07-30
        cash = base_pricer.discrete_coupon_amount(date(2021, 7, 1), date(2021, 8, 1))
        expected = 100.0 * 0.003  # 首年 0.3%
        assert abs(cash - expected) < 1e-10

    def test_discrete_coupon_misses_boundary(self, base_pricer):
        """区间起点等于付息日时不应计入."""
        cash = base_pricer.discrete_coupon_amount(date(2021, 7, 30), date(2021, 8, 1))
        assert cash == 0.0

    def test_coupon_rate_lookup(self, base_pricer):
        """各期票息率查找正确."""
        assert base_pricer.get_coupon_rate(date(2020, 10, 1)) == 0.003
        assert base_pricer.get_coupon_rate(date(2022, 1, 1)) == 0.004


# ── 4. 输入校验 ──────────────────────────────────────────
class TestValidation:

    def test_negative_S0_raises(self):
        with pytest.raises(ValueError, match="S0 must be positive"):
            UniversalCBPricer(S0=-1, K=50, current_date=date(2025, 1, 1),
                              maturity_date=date(2026, 1, 1))

    def test_negative_K_raises(self):
        with pytest.raises(ValueError, match="K must be positive"):
            UniversalCBPricer(S0=50, K=-1, current_date=date(2025, 1, 1),
                              maturity_date=date(2026, 1, 1))

    def test_maturity_before_current_raises(self):
        with pytest.raises(ValueError, match="maturity_date must be after"):
            UniversalCBPricer(S0=50, K=50, current_date=date(2026, 1, 1),
                              maturity_date=date(2025, 1, 1))

    def test_negative_sigma_raises(self, base_pricer):
        with pytest.raises(ValueError, match="non-negative"):
            base_pricer.price(sigma=-0.1, r=0.02, base_spread=0.03)

    def test_small_M_raises(self, base_pricer):
        with pytest.raises(ValueError, match="M must"):
            base_pricer.price(sigma=0.28, r=0.02, base_spread=0.03, M=2)


# ── 5. 辅助函数 ──────────────────────────────────────────
class TestHelpers:

    def test_to_date_from_string(self):
        assert to_date("2025-06-15") == date(2025, 6, 15)

    def test_to_date_from_date(self):
        d = date(2025, 6, 15)
        assert to_date(d) is d

    def test_to_date_from_datetime(self):
        from datetime import datetime
        dt = datetime(2025, 6, 15, 10, 30)
        assert to_date(dt) == date(2025, 6, 15)

    def test_to_date_none(self):
        assert to_date(None) is None

    def test_parse_coupon_normal(self):
        result = parse_coupon("0.3,0.5,0.8")
        assert result == (0.003, 0.005, 0.008)

    def test_parse_coupon_none(self):
        assert parse_coupon(None) is None

    def test_parse_coupon_empty(self):
        assert parse_coupon("") is None

    def test_add_years_normal(self):
        d = date(2020, 7, 30)
        assert UniversalCBPricer._add_years(d, 1) == date(2021, 7, 30)

    def test_add_years_leap_day(self):
        d = date(2024, 2, 29)
        assert UniversalCBPricer._add_years(d, 1) == date(2025, 2, 28)

    def test_add_years_negative_overflow(self):
        d = date(2, 1, 1)
        with pytest.raises(ValueError, match="Cannot add"):
            UniversalCBPricer._add_years(d, -10)


# ── 6. 默认常量 ──────────────────────────────────────────
class TestDefaults:

    def test_default_coupon_rates(self):
        assert DEFAULT_COUPON_RATES == (0.003, 0.004, 0.008, 0.015, 0.018, 0.02)

    def test_default_face_value(self):
        assert DEFAULT_FACE_VALUE == 100.0

    def test_default_redemption_price(self):
        assert DEFAULT_REDEMPTION_PRICE == 107.0

    def test_pricer_uses_default_coupons(self):
        pricer = UniversalCBPricer(
            S0=50, K=50, current_date=date(2025, 1, 1),
            maturity_date=date(2026, 1, 1),
        )
        assert pricer.coupon_rates == DEFAULT_COUPON_RATES


# ── 7. 转股价调整 ────────────────────────────────────────
class TestConversionPriceAdjust:

    def test_cash_dividend_lowers_K(self, base_pricer):
        old_K = base_pricer.K
        base_pricer.adjust_conversion_price(cash_dividend=2.0)
        assert base_pricer.K < old_K

    def test_stock_dividend_lowers_K(self):
        pricer = UniversalCBPricer(
            S0=50, K=50, current_date=date(2025, 1, 1),
            maturity_date=date(2026, 1, 1),
        )
        pricer.adjust_conversion_price(stock_dividend_ratio=0.1)
        # K_new = 50 / (1 + 0.1) ≈ 45.45
        assert pricer.K == round(50.0 / 1.1, 2)

    def test_rights_issue_without_price_raises(self, base_pricer):
        with pytest.raises(ValueError, match="rights_issue_price"):
            base_pricer.adjust_conversion_price(rights_issue_ratio=0.1)

    def test_ratio_updated_after_adjust(self):
        pricer = UniversalCBPricer(
            S0=50, K=50, current_date=date(2025, 1, 1),
            maturity_date=date(2026, 1, 1),
        )
        pricer.adjust_conversion_price(cash_dividend=5.0)
        assert abs(pricer.ratio - pricer.face_value / pricer.K) < 1e-10


# ── 8. 纯债价值 ──────────────────────────────────────────
class TestBondFloor:

    def test_bond_floor_at_maturity(self):
        """到期日纯债价值应等于赎回价."""
        pricer = UniversalCBPricer(
            S0=50, K=50,
            current_date=date(2025, 12, 31),
            maturity_date=date(2026, 1, 1),
            issue_date=date(2020, 1, 1),
            redemption_price=107.0,
        )
        bf = pricer.bond_floor_value(date(2026, 1, 1), 0.05)
        # 到期日折现因子 = 1, 且无未来付息
        assert abs(bf - 107.0) < 0.1

    def test_bond_floor_positive(self, base_pricer):
        bf = base_pricer.bond_floor_value(date(2025, 1, 1), 0.05)
        assert bf > 0
