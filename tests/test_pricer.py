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


# ── 12. 回测 (backtest with fake Wind) ────────────────────────
class FakeWindResponse:
    """模拟 WindPy 的返回对象."""
    def __init__(self, error_code=0, fields=None, data=None, times=None):
        self.ErrorCode = error_code
        self.Fields = fields or []
        self.Data = data or [] if data is not None else []
        self.Times = times or []


class FakeWind:
    """最小化 WindPy 桩, 仅覆盖 backtest_theoretical_price 的调用面."""
    def __init__(self, bond_code, stock_code, terms, bond_close_series, stock_close_series):
        self.bond_code = bond_code
        self.stock_code = stock_code
        self.terms = terms  # dict[lowercase_field] -> value
        self.bond_close = bond_close_series  # list[(date, float)]
        self.stock_close = stock_close_series

    def wss(self, code, fields_str, opts=""):
        fields = [f.strip() for f in fields_str.split(",")]
        if code == self.bond_code:
            data = [[self.terms.get(f.lower())] for f in fields]
            return FakeWindResponse(fields=fields, data=data)
        if code == self.stock_code:
            # 只会被问 "close" 用于现价
            return FakeWindResponse(fields=fields, data=[[self.stock_close[-1][1]]])
        return FakeWindResponse(error_code=1)

    def wset(self, table, opts):
        # 让 cashflow 返回错, 强制 fallback 到 couponrate 字段
        return FakeWindResponse(error_code=1)

    def wsd(self, code, field, start, end, opts=""):
        if code == self.bond_code:
            dates = [d for d, _ in self.bond_close]
            data = [[v for _, v in self.bond_close]]
            return FakeWindResponse(fields=[field], data=data, times=dates)
        if code == self.stock_code:
            dates = [d for d, _ in self.stock_close]
            data = [[v for _, v in self.stock_close]]
            return FakeWindResponse(fields=[field], data=data, times=dates)
        return FakeWindResponse(error_code=1)


@pytest.fixture
def fake_wind():
    """构造一个跨 8 个月的伪 Wind 数据集."""
    issue_date = date(2020, 7, 30)
    maturity_date = date(2026, 7, 30)
    start = date(2025, 1, 1)
    end = date(2025, 8, 31)

    # 构造日级历史 (243 天) - 简单线性走势 + 噪声
    bond_close = []
    stock_close = []
    n = (end - start).days + 1
    for i in range(n):
        d = start + timedelta(days=i)
        # 跳过周末以模拟交易日
        if d.weekday() >= 5:
            continue
        bond_close.append((d, 110.0 + 0.01 * i))
        stock_close.append((d, 50.0 + 0.02 * i))

    terms = {
        "underlyingcode": "000001.SZ",
        "ipo_date": "2020-07-30",
        "maturitydate": "2026-07-30",
        "latestpar": 100.0,
        "clause_conversion2_swapshareprice": 52.77,
        "clause_calloption_redemptionprice": 107.0,
        "clause_calloption_triggerproportion": 130.0,
        "clause_putoption_redeem_triggerproportion": 70.0,
        "clause_putoption_putbackperiodobs": 48.0,
        "couponrate": "0.3,0.4,0.8,1.5,1.8,2.0",
    }
    return FakeWind("123001.SZ", "000001.SZ", terms, bond_close, stock_close), start, end


class TestBacktest:

    def test_backtest_returns_expected_keys(self, fake_wind, monkeypatch):
        """回测应返回完整字段, 包括新增的 bond_floors / parities / ivs."""
        from CB import backtest_theoretical_price
        import backtest as bt_module

        wind, start, end = fake_wind
        monkeypatch.setattr(bt_module, "_ensure_wind", lambda: wind)

        result = backtest_theoretical_price(
            "123001.SZ", start_date=start, end_date=end,
            freq="M",  # 月频, 减少计算量
            M=80, N=200,
        )
        for key in ["dates", "theo_prices", "market_prices", "stock_prices",
                    "sigmas", "bond_floors", "parities", "ivs"]:
            assert key in result, f"缺少字段: {key}"
        assert len(result["dates"]) >= 3, "应有 ≥3 个月度采样点"
        assert all(np.isnan(iv) for iv in result["ivs"]), "默认 solve_iv=False 时 IV 应全 NaN"

    def test_backtest_theoretical_in_range(self, fake_wind, monkeypatch):
        """理论价应落在合理范围 (面值附近)."""
        from CB import backtest_theoretical_price
        import backtest as bt_module

        wind, start, end = fake_wind
        monkeypatch.setattr(bt_module, "_ensure_wind", lambda: wind)

        result = backtest_theoretical_price(
            "123001.SZ", start_date=start, end_date=end, freq="M",
            M=80, N=200,
        )
        for theo in result["theo_prices"]:
            assert 60 < theo < 200, f"理论价 {theo:.2f} 越界"

    def test_backtest_solve_iv_produces_finite_values(self, fake_wind, monkeypatch):
        """solve_iv=True 时, 至少部分 IV 应能解出有限值."""
        from CB import backtest_theoretical_price
        import backtest as bt_module

        wind, start, end = fake_wind
        monkeypatch.setattr(bt_module, "_ensure_wind", lambda: wind)

        result = backtest_theoretical_price(
            "123001.SZ", start_date=start, end_date=end, freq="M",
            M=80, N=200, solve_iv=True,
        )
        finite_ivs = [iv for iv in result["ivs"] if np.isfinite(iv)]
        assert len(finite_ivs) >= 1, "solve_iv=True 至少应解出一个有限 IV"

    def test_backtest_value_decomposition_relationship(self, fake_wind, monkeypatch):
        """每个采样点应满足: parity = S0 * face/K, bond_floor > 0."""
        from CB import backtest_theoretical_price
        import backtest as bt_module

        wind, start, end = fake_wind
        monkeypatch.setattr(bt_module, "_ensure_wind", lambda: wind)

        result = backtest_theoretical_price(
            "123001.SZ", start_date=start, end_date=end, freq="M",
            M=80, N=200,
        )
        K = 52.77
        face = 100.0
        for s0, par, bf in zip(result["stock_prices"], result["parities"],
                                result["bond_floors"]):
            assert abs(par - s0 * face / K) < 1e-6, \
                f"parity 一致性破坏: {par:.4f} vs {s0 * face / K:.4f}"
            assert bf > 0, f"bond_floor 应为正: {bf:.4f}"
