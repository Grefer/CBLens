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
import json
import pytest
import numpy as np
from datetime import date, timedelta

# 确保项目根目录在 sys.path 中
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from convertible_bond.pricer import (
    UniversalCBPricer,
    DEFAULT_COUPON_RATES,
    DEFAULT_FACE_VALUE,
    DEFAULT_REDEMPTION_PRICE,
)
from convertible_bond.data_providers import to_date, parse_coupon_string as parse_coupon


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

    def test_p_down_is_time_step_scaled(self):
        """p_down 应按时间步缩放, 不应随 PDE 网格 N 加密而被重复放大."""
        kwargs = dict(
            S0=18.66, K=24.55,
            current_date=date(2026, 4, 28),
            maturity_date=date(2028, 11, 28),
            issue_date=date(2022, 12, 22),
            conversion_start_date=date(2023, 6, 20),
            coupon_rates=(0.004, 0.006, 0.011, 0.015, 0.025, 0.03),
            redemption_price=115.0,
        )
        pricer = UniversalCBPricer(**kwargs)
        p0 = pricer.price(sigma=0.675, r=0.022, base_spread=0.03,
                          distress_k=0.05, p_down=0.0, M=300, N=1000)
        p1 = pricer.price(sigma=0.675, r=0.022, base_spread=0.03,
                          distress_k=0.05, p_down=0.15, M=300, N=1000)

        assert p1 >= p0
        assert p1 - p0 < 5.0

    def test_down_reset_block_until_suppresses_near_term_reset_value(self):
        """公告不下修期间应屏蔽对应窗口内的下修价值."""
        kwargs = dict(
            S0=18.66, K=24.55,
            current_date=date(2026, 4, 28),
            maturity_date=date(2028, 11, 28),
            issue_date=date(2022, 12, 22),
            conversion_start_date=date(2023, 6, 20),
            coupon_rates=(0.004, 0.006, 0.011, 0.015, 0.025, 0.03),
            redemption_price=115.0,
        )
        open_pricer = UniversalCBPricer(**kwargs)
        blocked_pricer = UniversalCBPricer(
            **kwargs, down_reset_block_until=date(2026, 6, 3))

        p_open = open_pricer.price(sigma=0.675, r=0.022, base_spread=0.03,
                                   distress_k=0.05, p_down=0.15, M=300, N=1000)
        p_blocked = blocked_pricer.price(sigma=0.675, r=0.022, base_spread=0.03,
                                         distress_k=0.05, p_down=0.15, M=300, N=1000)

        assert p_blocked <= p_open


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

    def test_bond_floor_increases_toward_maturity(self, base_pricer):
        """纯债价值随到期日临近应趋向赎回价."""
        bf_early = base_pricer.bond_floor_value(date(2025, 1, 1), 0.05)
        bf_late = base_pricer.bond_floor_value(date(2026, 7, 1), 0.05)
        assert bf_late > bf_early


# ── 9. 隐含波动率反解 ──────────────────────────────────────
class TestImpliedVol:

    def test_iv_round_trip(self):
        """已知 σ 计算理论价, 再反解 IV, 应回到原始 σ."""
        pricer = UniversalCBPricer(
            S0=52.0, K=52.77,
            current_date=date(2024, 1, 1),
            maturity_date=date(2026, 7, 30),
            issue_date=date(2020, 7, 30),
            conversion_start_date=date(2021, 2, 6),
            redemption_price=107.0,
        )
        sigma_true = 0.30
        target = pricer.price(sigma=sigma_true, r=0.022, base_spread=0.03,
                              p_down=0.0, distress_k=0.0, M=300, N=1000)
        iv = pricer.solve_implied_vol(target_price=target, r=0.022, base_spread=0.03,
                                      p_down=0.0, distress_k=0.0, M=300, N=1000)
        assert not np.isnan(iv), "IV 反解不应返回 NaN"
        assert abs(iv - sigma_true) < 0.03, \
            f"IV {iv:.4f} 与真实 σ {sigma_true:.4f} 偏差过大"

    def test_iv_out_of_range_returns_nan(self):
        """目标价超出合理区间时应返回 NaN."""
        pricer = UniversalCBPricer(
            S0=50.0, K=52.77,
            current_date=date(2025, 1, 1),
            maturity_date=date(2026, 7, 30),
            issue_date=date(2020, 7, 30),
            conversion_start_date=date(2021, 2, 6),
            redemption_price=107.0,
        )
        iv = pricer.solve_implied_vol(target_price=500.0, r=0.022, base_spread=0.03)
        assert np.isnan(iv), "超范围目标价应返回 NaN"


# ── 10. 希腊值基本约束 ─────────────────────────────────────
class TestGreeks:

    @pytest.fixture
    def greeks_pricer(self):
        return UniversalCBPricer(
            S0=50.0, K=52.77,
            current_date=date(2025, 1, 1),
            maturity_date=date(2026, 7, 30),
            issue_date=date(2020, 7, 30),
            conversion_start_date=date(2021, 2, 6),
            redemption_price=107.0,
        )

    def test_delta_non_negative(self, greeks_pricer):
        """Delta 应非负 (可转债价格随正股上涨)."""
        result = greeks_pricer.price(sigma=0.28, r=0.022, base_spread=0.03,
                                     M=200, N=500, return_greeks=True)
        assert result["delta"] >= 0, f"Delta={result['delta']:.4f} 不应为负"

    def test_vega_positive(self, greeks_pricer):
        """Vega 应为正 (波动率增大提升可转债价值)."""
        result = greeks_pricer.price(sigma=0.28, r=0.022, base_spread=0.03,
                                     M=200, N=500, return_greeks=True)
        assert result["vega"] > 0, f"Vega={result['vega']:.4f} 应为正"

    def test_price_decomposition_consistency(self, greeks_pricer):
        """理论价 ≈ max(纯债底, 转股价值) + 期权溢价."""
        result = greeks_pricer.price(sigma=0.28, r=0.022, base_spread=0.03,
                                     M=200, N=500, return_greeks=True)
        reconstructed = max(result["bond_floor"], result["parity"]) + result["option_premium"]
        assert abs(result["price"] - reconstructed) < 0.01, \
            f"价值分解不一致: price={result['price']:.3f}, reconstructed={reconstructed:.3f}"

    def test_return_greeks_false_returns_float(self, greeks_pricer):
        """return_greeks=False 应返回 float."""
        result = greeks_pricer.price(sigma=0.28, r=0.022, base_spread=0.03,
                                     M=200, N=500, return_greeks=False)
        assert isinstance(result, float)


# ── 11. 强赎宽限期 (call grace period) ───────────────────────
class TestCallNotice:
    """call_notice_days 把"立即行权" cap 抬升到 parity·(1+σ√t_grace),
    直接对应实务里"触发→公告→摘牌"窗口期的 stock optionality."""

    @pytest.fixture
    def itm_kwargs(self):
        return dict(
            S0=80.0, K=52.77,  # 深度 ITM, S/K ≈ 1.52 > 1.3 触发线
            current_date=date(2025, 1, 1),
            maturity_date=date(2026, 7, 30),
            issue_date=date(2020, 7, 30),
            conversion_start_date=date(2021, 2, 6),
            redemption_price=107.0,
        )

    def test_zero_grace_locks_option_premium_to_zero(self, itm_kwargs):
        """call_notice_days=0 + 深度 ITM → 期权溢价应锁定为 0 (旧版行为)."""
        pricer = UniversalCBPricer(call_notice_days=0, **itm_kwargs)
        result = pricer.price(sigma=0.28, r=0.022, base_spread=0.03,
                              M=200, N=500, return_greeks=True)
        assert abs(result["option_premium"]) < 0.5, \
            f"call_notice_days=0 期权溢价 {result['option_premium']:.3f} 应近 0"

    def test_positive_grace_yields_positive_premium(self, itm_kwargs):
        """call_notice_days=30 + 深度 ITM → 期权溢价 > 0."""
        pricer = UniversalCBPricer(call_notice_days=30, **itm_kwargs)
        result = pricer.price(sigma=0.30, r=0.022, base_spread=0.03,
                              M=200, N=500, return_greeks=True)
        assert result["option_premium"] > 0.5, \
            f"call_notice_days=30 期权溢价 {result['option_premium']:.3f} 应显著为正"

    def test_grace_monotone_in_days(self, itm_kwargs):
        """更长的宽限期 → 不低于的理论价 (单调性)."""
        p0 = UniversalCBPricer(call_notice_days=0, **itm_kwargs).price(
            sigma=0.30, r=0.022, base_spread=0.03, M=200, N=500)
        p30 = UniversalCBPricer(call_notice_days=30, **itm_kwargs).price(
            sigma=0.30, r=0.022, base_spread=0.03, M=200, N=500)
        p60 = UniversalCBPricer(call_notice_days=60, **itm_kwargs).price(
            sigma=0.30, r=0.022, base_spread=0.03, M=200, N=500)
        assert p0 <= p30 + 0.01, f"宽限期单调性破坏: p0={p0:.3f}, p30={p30:.3f}"
        assert p30 <= p60 + 0.01, f"宽限期单调性破坏: p30={p30:.3f}, p60={p60:.3f}"

    def test_theta_with_grace_no_error(self, itm_kwargs):
        """theta 重建 tomorrow_pricer 时应正确传入 call_notice_days, 不报错."""
        pricer = UniversalCBPricer(call_notice_days=30, **itm_kwargs)
        result = pricer.price(sigma=0.28, r=0.022, base_spread=0.03,
                              M=150, N=300, return_greeks=True)
        # theta 是数值差分, 不应是 NaN
        assert not np.isnan(result["theta"]), "theta 不应为 NaN"


# ── 12. 回测 (backtest with FakeProvider) ────────────────────
from convertible_bond.data_providers import DataProvider, BondTerms, CashflowSchedule


class FakeProvider(DataProvider):
    """直接实现 DataProvider 接口的最小桩, 给回测/批量定价测试用."""
    name = "fake"

    def __init__(self, bond_code, stock_code, terms: BondTerms,
                 bond_close, stock_close):
        self.bond_code = bond_code
        self.stock_code = stock_code
        self.terms = terms
        self.bond_close = bond_close   # [(date, float)]
        self.stock_close = stock_close

    def get_bond_terms(self, bond_code, valuation_date):
        return self.terms

    def get_stock_close(self, stock_code, on_date):
        for d, v in reversed(self.stock_close):
            if d <= on_date and v is not None:
                return float(v)
        raise RuntimeError(f"FakeProvider 无 {stock_code} 现价")

    def get_stock_history(self, stock_code, start, end):
        return [(d, v) for d, v in self.stock_close if start <= d <= end]

    def get_bond_history(self, bond_code, start, end):
        return [(d, v) for d, v in self.bond_close if start <= d <= end]


@pytest.fixture
def fake_provider():
    """构造跨 8 个月的伪数据 + FakeProvider."""
    start = date(2025, 1, 1)
    end = date(2025, 8, 31)

    bond_close, stock_close = [], []
    n = (end - start).days + 1
    for i in range(n):
        d = start + timedelta(days=i)
        if d.weekday() >= 5:
            continue
        bond_close.append((d, 110.0 + 0.01 * i))
        stock_close.append((d, 50.0 + 0.02 * i))

    terms = BondTerms(
        sec_name="测试债",
        underlying_code="000001.SZ",
        issue_date=date(2020, 7, 30),
        maturity_date=date(2026, 7, 30),
        face_value=100.0,
        conversion_price=52.77,
        redemption_price=107.0,
        call_trigger_pct=130.0,
        put_trigger_pct=70.0,
        put_obs_months=48.0,
        coupon_rates=(0.003, 0.004, 0.008, 0.015, 0.018, 0.02),
        close=110.0,
    )
    provider = FakeProvider("123001.SZ", "000001.SZ", terms, bond_close, stock_close)
    return provider, start, end


class TestBacktest:

    def test_backtest_returns_expected_keys(self, fake_provider):
        """回测应返回完整字段, 包括新增的 bond_floors / parities / ivs."""
        from convertible_bond.backtest import backtest_theoretical_price

        provider, start, end = fake_provider
        result = backtest_theoretical_price(
            "123001.SZ", start_date=start, end_date=end,
            freq="M", M=80, N=200, provider=provider,
        )
        for key in ["dates", "theo_prices", "market_prices", "stock_prices",
                    "sigmas", "bond_floors", "parities", "ivs"]:
            assert key in result, f"缺少字段: {key}"
        assert len(result["dates"]) >= 3, "应有 ≥3 个月度采样点"
        assert all(np.isnan(iv) for iv in result["ivs"]), \
            "默认 solve_iv=False 时 IV 应全 NaN"

    def test_backtest_theoretical_in_range(self, fake_provider):
        """理论价应落在合理范围 (面值附近)."""
        from convertible_bond.backtest import backtest_theoretical_price

        provider, start, end = fake_provider
        result = backtest_theoretical_price(
            "123001.SZ", start_date=start, end_date=end,
            freq="M", M=80, N=200, provider=provider,
        )
        for theo in result["theo_prices"]:
            assert 60 < theo < 200, f"理论价 {theo:.2f} 越界"

    def test_backtest_solve_iv_produces_finite_values(self, fake_provider):
        """solve_iv=True 时, 至少部分 IV 应能解出有限值."""
        from convertible_bond.backtest import backtest_theoretical_price

        provider, start, end = fake_provider
        result = backtest_theoretical_price(
            "123001.SZ", start_date=start, end_date=end,
            freq="M", M=80, N=200, solve_iv=True, provider=provider,
        )
        finite_ivs = [iv for iv in result["ivs"] if np.isfinite(iv)]
        assert len(finite_ivs) >= 1, "solve_iv=True 至少应解出一个有限 IV"

    def test_backtest_value_decomposition_relationship(self, fake_provider):
        """每个采样点应满足: parity = S0 * face/K, bond_floor > 0."""
        from convertible_bond.backtest import backtest_theoretical_price

        provider, start, end = fake_provider
        result = backtest_theoretical_price(
            "123001.SZ", start_date=start, end_date=end,
            freq="M", M=80, N=200, provider=provider,
        )
        K = 52.77
        face = 100.0
        for s0, par, bf in zip(result["stock_prices"], result["parities"],
                                result["bond_floors"]):
            assert abs(par - s0 * face / K) < 1e-6, \
                f"parity 一致性破坏: {par:.4f} vs {s0 * face / K:.4f}"
            assert bf > 0, f"bond_floor 应为正: {bf:.4f}"

    def test_backtest_applies_down_reset_p_scale(self, fake_provider, monkeypatch):
        """回测应和单点/批量一样应用下修强度缩放."""
        import convertible_bond.backtest as bt

        provider, start, end = fake_provider
        provider.terms.down_reset_p_scale = 0.0
        seen_p_down = []

        class SpyPricer:
            def __init__(self, *args, **kwargs):
                self.ratio = 100.0 / float(kwargs["K"])

            def price(self, **kwargs):
                seen_p_down.append(kwargs["p_down"])
                return 100.0

            def bond_floor_value(self, *_args, **_kwargs):
                return 95.0

        monkeypatch.setattr(bt, "UniversalCBPricer", SpyPricer)

        bt.backtest_theoretical_price(
            "123001.SZ", start_date=start, end_date=end,
            freq="M", p_down=0.15, M=80, N=200, provider=provider,
        )

        assert seen_p_down
        assert all(p == 0.0 for p in seen_p_down)


# ── 13. price_from_provider (provider 通用入口) ────────────
class TestPriceFromProvider:

    def test_price_from_provider_basic(self, fake_provider):
        """通过 FakeProvider 调 price_from_provider 应返回完整结果字典."""
        from convertible_bond.pricing_api import price_from_provider

        provider, _, end = fake_provider
        result = price_from_provider(
            provider, "123001.SZ",
            valuation_date=end, M=80, N=200,
        )
        assert result["bond_code"] == "123001.SZ"
        assert result["stock_code"] == "000001.SZ"
        assert result["data_source"] == "fake"
        assert 60 < result["theoretical_price"] < 200
        assert result["sigma"] > 0

    def test_price_from_provider_uses_latest_bond_history_close(self, fake_provider):
        """market_price 应来自估值日前最近转债收盘价, 而不是静态 terms.close."""
        from convertible_bond.pricing_api import price_from_provider

        provider, _, end = fake_provider
        result = price_from_provider(
            provider, "123001.SZ",
            valuation_date=end, M=80, N=200,
        )

        assert result["market_price"] == provider.bond_close[-1][1]
        assert result["market_price"] != provider.terms.close

    def test_price_from_provider_applies_down_reset_overrides(self, fake_provider):
        """单债下修事件覆盖应传入 pricer 并缩放 p_down."""
        from convertible_bond.pricing_api import price_from_provider

        provider, _, end = fake_provider
        provider.terms.down_reset_block_until = date(2025, 9, 30)
        provider.terms.down_reset_p_scale = 0.0
        provider.terms.down_reset_note = "公告不向下修正"

        result = price_from_provider(
            provider, "123001.SZ",
            valuation_date=end, p_down=0.15, M=80, N=200,
        )

        assert result["p_down"] == 0.0
        assert result["down_reset_block_until"] == date(2025, 9, 30)
        assert result["down_reset_note"] == "公告不向下修正"

    def test_price_from_provider_resolves_event_overrides(self, fake_provider, tmp_path, monkeypatch):
        """事件层 announce_date + cooldown_months → block_until 自动推算, p_scale 衰减 p_down."""
        from convertible_bond import down_reset_overrides as dro
        from convertible_bond.pricing_api import price_from_provider

        provider, _, end = fake_provider
        provider.terms.down_reset_cooldown_months = 6  # 募集说明书条款

        ov_path = tmp_path / "down_reset_overrides.json"
        ov_path.write_text(json.dumps({
            "123001.SZ": {
                "announce_date": "2025-04-13",
                "p_scale_after_cooldown": 0.3,
                "note": "测试: 公告不修正",
            }
        }), encoding="utf-8")
        monkeypatch.setattr(dro, "_default_overrides", dro.DownResetOverrides(ov_path))

        result = price_from_provider(
            provider, "123001.SZ",
            valuation_date=end, p_down=0.15, M=80, N=200,
        )

        assert result["down_reset_announce_date"] == date(2025, 4, 13)
        assert result["down_reset_block_until"] == date(2025, 10, 13)  # +6M
        assert result["down_reset_p_scale"] == 0.3
        assert result["p_down"] == pytest.approx(0.15 * 0.3)
        assert "announce=2025-04-13" in result["down_reset_note"]
        assert "测试: 公告不修正" in result["down_reset_note"]


# ── 14. 条款本地缓存 + CachingDataProvider ──────────────────
class TestTermsCache:

    def test_set_get_roundtrip(self, tmp_path):
        from convertible_bond.cache import TermsCache
        cache = TermsCache(tmp_path)
        terms = BondTerms(
            sec_name="测试债",
            underlying_code="000001.SZ",
            issue_date=date(2020, 7, 30),
            listing_date=date(2020, 8, 17),
            tradable_date=date(2020, 8, 17),
            is_tradable=True,
            trading_status="tradable",
            maturity_date=date(2026, 7, 30),
            face_value=100.0,
            conversion_price=52.77,
            coupon_rates=(0.003, 0.005, 0.01),
        )
        cache.set("123001.SZ", terms, source="wind")
        loaded = cache.get("123001.SZ")
        assert loaded is not None
        assert loaded.sec_name == "测试债"
        assert loaded.conversion_price == 52.77
        assert loaded.listing_date == date(2020, 8, 17)
        assert loaded.tradable_date == date(2020, 8, 17)
        assert loaded.is_tradable is True
        assert loaded.trading_status == "tradable"
        assert loaded.maturity_date == date(2026, 7, 30)
        assert loaded.coupon_rates == (0.003, 0.005, 0.01)

    def test_missing_returns_none(self, tmp_path):
        from convertible_bond.cache import TermsCache
        cache = TermsCache(tmp_path)
        assert cache.get("999999.SZ") is None
        assert not cache.has("999999.SZ")

    def test_list_bonds(self, tmp_path):
        from convertible_bond.cache import TermsCache
        cache = TermsCache(tmp_path)
        for code in ["123001.SZ", "113001.SH", "127001.SZ"]:
            cache.set(code, BondTerms(conversion_price=10.0), source="wind")
        assert sorted(cache.list_bonds()) == ["113001.SH", "123001.SZ", "127001.SZ"]

    def test_fetched_at_and_stale(self, tmp_path):
        from convertible_bond.cache import TermsCache
        cache = TermsCache(tmp_path)
        cache.set("X.SZ", BondTerms(conversion_price=1.0))
        ts = cache.fetched_at("X.SZ")
        assert ts is not None
        # 刚写的不应过期
        assert not cache.is_stale("X.SZ", max_age_days=30)
        # 不存在的视为过期
        assert cache.is_stale("Y.SZ", max_age_days=30)

    def test_delete(self, tmp_path):
        from convertible_bond.cache import TermsCache
        cache = TermsCache(tmp_path)
        cache.set("X.SZ", BondTerms(conversion_price=1.0))
        assert cache.has("X.SZ")
        cache.delete("X.SZ")
        assert not cache.has("X.SZ")


class TestCachingDataProvider:

    def test_first_call_fetches_and_persists(self, fake_provider, tmp_path):
        from convertible_bond.cache import TermsCache, CachingDataProvider
        provider, _, end = fake_provider
        cache = TermsCache(tmp_path)
        wrapped = CachingDataProvider(provider, cache, max_age_days=30)
        assert not cache.has("123001.SZ")
        terms = wrapped.get_bond_terms("123001.SZ", end)
        assert terms.conversion_price == 52.77
        assert cache.has("123001.SZ"), "首次调用应写回缓存"

    def test_second_call_uses_cache(self, fake_provider, tmp_path):
        """缓存命中后, 内层 provider 不应被调用."""
        from convertible_bond.cache import TermsCache, CachingDataProvider
        provider, _, end = fake_provider
        cache = TermsCache(tmp_path)
        wrapped = CachingDataProvider(provider, cache, max_age_days=30)

        wrapped.get_bond_terms("123001.SZ", end)  # 写入缓存

        # 把 inner.get_bond_terms 改成永远抛错, 验证下次仍能拿到 terms
        def boom(*a, **kw):
            raise RuntimeError("不应该走到这里")
        provider.get_bond_terms = boom  # type: ignore[method-assign]

        terms = wrapped.get_bond_terms("123001.SZ", end)
        assert terms.conversion_price == 52.77

    def test_force_refresh(self, fake_provider, tmp_path):
        from convertible_bond.cache import TermsCache, CachingDataProvider
        provider, _, end = fake_provider
        cache = TermsCache(tmp_path)
        wrapped = CachingDataProvider(provider, cache)

        # 先用 inner 写一个旧版本
        wrapped.get_bond_terms("123001.SZ", end)

        # 改 inner 的返回值, 然后强刷
        provider.terms = BondTerms(
            sec_name="新名字",
            underlying_code="000001.SZ",
            conversion_price=100.0,  # 新 K
        )
        fresh = wrapped.force_refresh("123001.SZ", end)
        assert fresh.conversion_price == 100.0
        assert cache.get("123001.SZ").conversion_price == 100.0

    def test_inner_failure_falls_back_to_cache(self, fake_provider, tmp_path):
        from convertible_bond.cache import TermsCache, CachingDataProvider
        provider, _, end = fake_provider
        cache = TermsCache(tmp_path)
        wrapped = CachingDataProvider(provider, cache, max_age_days=0)
        # max_age_days=0 → 永远视为过期, 强制走 inner
        wrapped.get_bond_terms("123001.SZ", end)  # 先写入缓存

        # 让 inner 抛错
        def boom(*a, **kw):
            raise RuntimeError("network down")
        provider.get_bond_terms = boom  # type: ignore[method-assign]

        # 即便缓存过期, inner 失败时也应回退到缓存
        terms = wrapped.get_bond_terms("123001.SZ", end)
        assert terms.conversion_price == 52.77

    def test_dynamic_methods_passthrough(self, fake_provider, tmp_path):
        from convertible_bond.cache import TermsCache, CachingDataProvider
        provider, start, end = fake_provider
        cache = TermsCache(tmp_path)
        wrapped = CachingDataProvider(provider, cache)

        # 价格/历史接口应直接透传
        s0 = wrapped.get_stock_close("000001.SZ", end)
        assert s0 > 0
        hist = wrapped.get_stock_history("000001.SZ", start, end)
        assert len(hist) > 50


class TestCachedBondDataProvider:

    def test_terms_read_from_cb_data_and_market_passthrough(self, fake_provider, tmp_path):
        from convertible_bond.cache import TermsCache, CachedBondDataProvider

        market, start, end = fake_provider
        cache = TermsCache(tmp_path)
        cache.set("123001.SZ", market.terms, source="Wind")

        class StaticBoom(FakeProvider):
            def get_bond_terms(self, bond_code, valuation_date):
                raise RuntimeError("不应该刷新 Wind")

        static = StaticBoom("123001.SZ", "000001.SZ", market.terms, [], [])
        wrapped = CachedBondDataProvider(
            market, cache, static_source=static, auto_refresh=False)

        terms = wrapped.get_bond_terms("123001.SZ", end)
        assert terms.conversion_price == 52.77
        assert wrapped.get_stock_close("000001.SZ", end) > 0
        assert len(wrapped.get_stock_history("000001.SZ", start, end)) > 50

    def test_force_refresh_uses_static_wind_and_merges_cashflow(self, fake_provider, tmp_path):
        from convertible_bond.cache import TermsCache, CachedBondDataProvider

        market, _, end = fake_provider
        cache = TermsCache(tmp_path)

        class StaticWind(FakeProvider):
            name = "Wind"

            def get_cashflow(self, bond_code):
                return CashflowSchedule(
                    coupon_rates=(0.001, 0.002, 0.003),
                    redemption_price=108.0,
                    maturity_date=date(2026, 7, 30),
                )

        static_terms = BondTerms(
            sec_name="Wind债",
            underlying_code="000001.SZ",
            issue_date=date(2020, 7, 30),
            maturity_date=date(2026, 7, 30),
            face_value=100.0,
            conversion_price=66.0,
            coupon_rates=(0.01,),
        )
        static = StaticWind("123001.SZ", "000001.SZ", static_terms, [], [])
        wrapped = CachedBondDataProvider(market, cache, static_source=static)

        fresh = wrapped.force_refresh("123001.SZ", end)
        assert fresh.conversion_price == 66.0
        assert fresh.coupon_rates == (0.001, 0.002, 0.003)
        assert fresh.redemption_price == 108.0
        assert cache.get("123001.SZ").redemption_price == 108.0

    def test_risk_free_rate_is_requested_once_per_date(self, fake_provider, tmp_path):
        from convertible_bond.cache import TermsCache, CachedBondDataProvider

        market, _, end = fake_provider
        market.risk_calls = 0

        def risk_free_once(on_date):
            market.risk_calls += 1
            return 2.25

        market.get_risk_free_rate = risk_free_once  # type: ignore[method-assign]
        wrapped = CachedBondDataProvider(market, TermsCache(tmp_path), static_source=market)

        assert wrapped.get_risk_free_rate(end) == 2.25
        assert wrapped.get_risk_free_rate(end) == 2.25
        assert market.risk_calls == 1


class TestAkshareStockFallbacks:

    def test_stock_history_falls_back_to_daily(self):
        import pandas as pd
        from convertible_bond.data_providers import AkshareDataProvider

        class FakeAk:
            def stock_zh_a_hist(self, **kwargs):
                raise RuntimeError("hist down")

            def stock_zh_a_daily(self, **kwargs):
                assert kwargs["symbol"] == "sz000001"
                return pd.DataFrame({
                    "date": ["2025-01-02", "2025-01-03"],
                    "close": [10.0, 10.5],
                })

        provider = object.__new__(AkshareDataProvider)
        provider._ak = FakeAk()

        history = provider.get_stock_history(
            "000001.SZ", date(2025, 1, 1), date(2025, 1, 10))
        assert history == [(date(2025, 1, 2), 10.0), (date(2025, 1, 3), 10.5)]

    def test_stock_close_falls_back_to_spot_snapshot(self):
        import pandas as pd
        from convertible_bond.data_providers import AkshareDataProvider

        class FakeAk:
            def stock_zh_a_hist(self, **kwargs):
                raise RuntimeError("hist down")

            def stock_zh_a_daily(self, **kwargs):
                raise RuntimeError("daily down")

            def stock_zh_a_spot_em(self):
                return pd.DataFrame({
                    "代码": ["000001", "600000"],
                    "最新价": [12.34, 7.89],
                })

        provider = object.__new__(AkshareDataProvider)
        provider._ak = FakeAk()

        assert provider.get_stock_close("000001.SZ", date(2025, 1, 10)) == 12.34

    def test_risk_free_rate_uses_on_date(self):
        """历史回测调用 get_risk_free_rate(过去某日) 应取该日期或之前最近一条 Shibor,
        而不是返回最新值 (回归 #akshare-shibor-historical)."""
        import pandas as pd
        from convertible_bond.data_providers import AkshareDataProvider

        class FakeAk:
            def macro_china_shibor_all(self):
                return pd.DataFrame({
                    "日期": ["2024-01-02", "2024-06-15", "2024-12-31", "2025-06-01"],
                    "1Y_定价": [2.10, 2.20, 2.30, 2.50],
                })

        provider = object.__new__(AkshareDataProvider)
        provider._ak = FakeAk()

        # 历史日期 → 应取 <= on_date 的最近一条
        assert provider.get_risk_free_rate(date(2024, 7, 1)) == 2.20
        assert provider.get_risk_free_rate(date(2025, 1, 1)) == 2.30
        # 当前及之后 → 取最近一条
        assert provider.get_risk_free_rate(date(2025, 12, 31)) == 2.50
        # 早于全部数据 → None (没有可参考的历史值)
        assert provider.get_risk_free_rate(date(2023, 1, 1)) is None


# ── 15. TermsBundle (单文件项目级 snapshot) ─────────────────
class TestTermsBundle:

    def test_set_get_roundtrip(self, tmp_path):
        from convertible_bond.cache import TermsBundle
        bundle = TermsBundle(tmp_path / "test_bundle.json")
        terms = BondTerms(
            sec_name="测试债",
            underlying_code="000001.SZ",
            issue_date=date(2020, 7, 30),
            listing_date=date(2020, 8, 17),
            tradable_date=date(2020, 8, 17),
            is_tradable=True,
            trading_status="tradable",
            maturity_date=date(2026, 7, 30),
            conversion_price=52.77,
            coupon_rates=(0.003, 0.005),
        )
        bundle.set("128009.SZ", terms, source="wind")
        # 重新打开同一文件, 验证持久化
        bundle2 = TermsBundle(tmp_path / "test_bundle.json")
        loaded = bundle2.get("128009.SZ")
        assert loaded is not None
        assert loaded.conversion_price == 52.77
        assert loaded.listing_date == date(2020, 8, 17)
        assert loaded.tradable_date == date(2020, 8, 17)
        assert loaded.is_tradable is True
        assert loaded.trading_status == "tradable"
        assert loaded.maturity_date == date(2026, 7, 30)

    def test_set_many_atomic(self, tmp_path):
        """set_many 应一次性提交, 期间只刷盘一次."""
        from convertible_bond.cache import TermsBundle
        bundle = TermsBundle(tmp_path / "b.json")
        items = [
            ("A.SZ", BondTerms(conversion_price=10.0)),
            ("B.SH", BondTerms(conversion_price=20.0)),
            ("C.SZ", BondTerms(conversion_price=30.0)),
        ]
        bundle.set_many(items, source="wind")
        assert sorted(bundle.list_bonds()) == ["A.SZ", "B.SH", "C.SZ"]

    def test_bundle_meta(self, tmp_path):
        from convertible_bond.cache import TermsBundle
        bundle = TermsBundle(tmp_path / "b.json")
        bundle.set("X.SZ", BondTerms(conversion_price=1.0), source="wind")
        meta = bundle.bundle_meta()
        assert meta.get("n_bonds") == 1
        assert "updated_at" in meta

    def test_bundle_compatible_with_caching_provider(self, fake_provider, tmp_path):
        """TermsBundle 应和 TermsCache 同样可用作 CachingDataProvider 的存储."""
        from convertible_bond.cache import TermsBundle, CachingDataProvider
        provider, _, end = fake_provider
        bundle = TermsBundle(tmp_path / "b.json")
        wrapped = CachingDataProvider(provider, bundle, max_age_days=30)
        terms = wrapped.get_bond_terms("123001.SZ", end)
        assert terms.conversion_price == 52.77
        assert bundle.has("123001.SZ"), "首次拉取应写回 bundle"

    def test_corrupt_bundle_treated_as_empty(self, tmp_path):
        """损坏的 JSON 不应让 bundle 初始化爆炸."""
        from convertible_bond.cache import TermsBundle
        p = tmp_path / "broken.json"
        p.write_text("{ this is not valid json")
        bundle = TermsBundle(p)
        assert bundle.list_bonds() == []
        # 之后写入应能正常工作 (覆盖损坏文件)
        bundle.set("X.SZ", BondTerms(conversion_price=1.0))
        assert bundle.has("X.SZ")

    def test_delete(self, tmp_path):
        from convertible_bond.cache import TermsBundle
        bundle = TermsBundle(tmp_path / "b.json")
        bundle.set("X.SZ", BondTerms(conversion_price=1.0))
        assert bundle.delete("X.SZ") is True
        assert not bundle.has("X.SZ")
        assert bundle.delete("X.SZ") is False  # 已删除, 再 delete 返回 False


class TestCSVDataProvider:

    def test_terms_loads_down_reset_fields(self, tmp_path):
        from convertible_bond.data_providers import CSVDataProvider

        terms_dir = tmp_path / "terms"
        terms_dir.mkdir()
        (terms_dir / "123001.SZ.json").write_text(json.dumps({
            "underlying_code": "000001.SZ",
            "conversion_price": 52.77,
            "down_reset_block_until": "2025-09-30",
            "down_reset_p_scale": 0.25,
            "down_reset_note": "csv override",
            "down_reset_cooldown_months": 6,
        }), encoding="utf-8")

        provider = CSVDataProvider(tmp_path)
        terms = provider.get_bond_terms("123001.SZ", date(2025, 8, 31))

        assert terms.down_reset_block_until == date(2025, 9, 30)
        assert terms.down_reset_p_scale == 0.25
        assert terms.down_reset_note == "csv override"
        assert terms.down_reset_cooldown_months == 6
