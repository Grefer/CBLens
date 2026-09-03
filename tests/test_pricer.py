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

    def test_down_reset_trigger_ratio_gates_p_down_value(self):
        """下修触发线低于 K 时, 同样 p_down 的下修价值应更保守."""
        kwargs = dict(
            S0=40.0, K=52.77,
            current_date=date(2025, 1, 1),
            maturity_date=date(2026, 7, 30),
            issue_date=date(2020, 7, 30),
            conversion_start_date=date(2021, 2, 6),
            redemption_price=107.0,
        )
        p0 = UniversalCBPricer(**kwargs).price(
            sigma=0.28, r=0.022, base_spread=0.03,
            distress_k=0.05, p_down=0.0, M=200, N=500)
        old_gate = UniversalCBPricer(**kwargs, down_reset_trigger_ratio=1.0).price(
            sigma=0.28, r=0.022, base_spread=0.03,
            distress_k=0.05, p_down=0.30, M=200, N=500)
        strict_gate = UniversalCBPricer(**kwargs, down_reset_trigger_ratio=0.85).price(
            sigma=0.28, r=0.022, base_spread=0.03,
            distress_k=0.05, p_down=0.30, M=200, N=500)

        assert old_gate >= strict_gate >= p0

    def test_flat_below_trigger_resets_near_trigger(self):
        """纯触发后(flat): 股价刚跌破触发线就应获得明确下修价值,

        不像旧的 S 渐变那样在触发线附近趋近于 0。
        """
        kwargs = dict(
            S0=51.0, K=52.77,  # S/K≈0.97, 刚跌破触发线 (trigger_ratio=1.0)
            current_date=date(2025, 1, 1),
            maturity_date=date(2026, 7, 30),
            issue_date=date(2020, 7, 30),
            conversion_start_date=date(2021, 2, 6),
            redemption_price=107.0,
            down_reset_trigger_ratio=1.0,
        )
        price_kw = dict(sigma=0.28, r=0.022, base_spread=0.03,
                        distress_k=0.05, M=300, N=800)
        p0 = UniversalCBPricer(**kwargs).price(p_down=0.0, **price_kw)
        p1 = UniversalCBPricer(**kwargs).price(p_down=0.30, **price_kw)
        # flat 下方 uplift≈1.2; 旧渐变在 S/K=0.97 处只有 ~0.04
        assert p1 - p0 > 0.5, f"近触发线下修价值 {p1 - p0:.3f} 过小, 疑似仍在用 S 渐变"

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

    def test_scheduled_reset_raises_otm_price(self):
        """已提议下修 (一次性近确定下修节点) 应抬升 OTM 转债价值."""
        kwargs = dict(
            S0=40.0, K=52.77,
            current_date=date(2025, 1, 1),
            maturity_date=date(2026, 7, 30),
            issue_date=date(2020, 7, 30),
            conversion_start_date=date(2021, 2, 6),
            redemption_price=107.0,
        )
        price_kw = dict(sigma=0.28, r=0.022, base_spread=0.03,
                        distress_k=0.05, p_down=0.0, M=200, N=500)
        no_sched = UniversalCBPricer(**kwargs).price(**price_kw)
        with_sched = UniversalCBPricer(
            **kwargs,
            scheduled_reset_date=date(2025, 3, 1),
            scheduled_reset_prob=0.9,
        ).price(**price_kw)
        assert with_sched > no_sched, (
            f"已提议下修价 {with_sched:.3f} 应高于无提议 {no_sched:.3f}")

    def test_scheduled_reset_prob_monotonic(self):
        """一次性下修节点的价值应随通过率单调上升."""
        kwargs = dict(
            S0=40.0, K=52.77,
            current_date=date(2025, 1, 1),
            maturity_date=date(2026, 7, 30),
            issue_date=date(2020, 7, 30),
            conversion_start_date=date(2021, 2, 6),
            redemption_price=107.0,
        )
        price_kw = dict(sigma=0.28, r=0.022, base_spread=0.03,
                        distress_k=0.05, p_down=0.0, M=200, N=500)
        prices = [
            UniversalCBPricer(
                **kwargs, scheduled_reset_date=date(2025, 3, 1),
                scheduled_reset_prob=p).price(**price_kw)
            for p in (0.0, 0.5, 1.0)
        ]
        assert prices[0] <= prices[1] <= prices[2]

    def test_scheduled_reset_beyond_maturity_ignored(self):
        """生效日晚于到期日的一次性下修节点应被忽略, 不影响定价."""
        kwargs = dict(
            S0=40.0, K=52.77,
            current_date=date(2025, 1, 1),
            maturity_date=date(2026, 7, 30),
            issue_date=date(2020, 7, 30),
            conversion_start_date=date(2021, 2, 6),
            redemption_price=107.0,
        )
        price_kw = dict(sigma=0.28, r=0.022, base_spread=0.03,
                        distress_k=0.05, p_down=0.0, M=200, N=500)
        base = UniversalCBPricer(**kwargs).price(**price_kw)
        beyond = UniversalCBPricer(
            **kwargs, scheduled_reset_date=date(2027, 1, 1),
            scheduled_reset_prob=0.9).price(**price_kw)
        assert beyond == pytest.approx(base)

    def test_scheduled_reset_target_k_noop_when_equals_current_k(self):
        """目标 K == 现 K (下修已落地) 时, 一次性节点应近似 no-op (防与条款刷新双计)."""
        kwargs = dict(
            S0=40.0, K=52.77,
            current_date=date(2025, 1, 1),
            maturity_date=date(2026, 7, 30),
            issue_date=date(2020, 7, 30),
            conversion_start_date=date(2021, 2, 6),
            redemption_price=107.0,
        )
        price_kw = dict(sigma=0.28, r=0.022, base_spread=0.03,
                        distress_k=0.05, p_down=0.0, M=300, N=800)
        no_node = UniversalCBPricer(**kwargs).price(**price_kw)
        same_k = UniversalCBPricer(
            **kwargs, scheduled_reset_date=date(2025, 6, 1),
            scheduled_reset_prob=1.0, scheduled_reset_target_k=52.77,
        ).price(**price_kw)
        assert same_k == pytest.approx(no_node, abs=0.05)

    def test_scheduled_reset_target_k_above_current_k_falls_back_to_estimate(self):
        """公告新 K 高于现 K = 上游解析错了: 丢掉它回落 premium/floor 估算, 而不是留着
        让节点静默退化成 no-op。

        pricer 的节点是 max(V, reset_value); 偏高的 target_k 只会让 reset_value 低于 V,
        于是"已提议下修"这件事在价格上完全消失。实测主池当时仅有的两个在途下修节点
        (晶能转债 target_k=13.7 vs K=6.35、强力转债 18.94 vs 12.70) uplift 只剩 0.02%/0.04%。
        """
        kwargs = dict(
            S0=40.0, K=52.77,
            current_date=date(2025, 1, 1),
            maturity_date=date(2026, 7, 30),
            issue_date=date(2020, 7, 30),
            conversion_start_date=date(2021, 2, 6),
            redemption_price=107.0,
        )
        node = dict(scheduled_reset_date=date(2025, 6, 1), scheduled_reset_prob=1.0)
        price_kw = dict(sigma=0.28, r=0.022, base_spread=0.03,
                        distress_k=0.05, p_down=0.0, M=300, N=800)

        bogus = UniversalCBPricer(**kwargs, **node, scheduled_reset_target_k=80.0)
        assert bogus.scheduled_reset_target_k is None
        estimated = UniversalCBPricer(**kwargs, **node)
        assert bogus.price(**price_kw) == pytest.approx(estimated.price(**price_kw), abs=1e-9)

        # 而 no-op 那一档 (target_k == 现 K) 的价格明显更低 —— 两者不能被混为一谈
        noop = UniversalCBPricer(**kwargs, **node, scheduled_reset_target_k=52.77)
        assert bogus.price(**price_kw) > noop.price(**price_kw) + 0.5

    def test_scheduled_reset_target_k_lower_raises_value(self):
        """公告新 K 更低时, 已公告节点应抬升 OTM 价值 (优于无节点)."""
        kwargs = dict(
            S0=40.0, K=52.77,
            current_date=date(2025, 1, 1),
            maturity_date=date(2026, 7, 30),
            issue_date=date(2020, 7, 30),
            conversion_start_date=date(2021, 2, 6),
            redemption_price=107.0,
        )
        price_kw = dict(sigma=0.28, r=0.022, base_spread=0.03,
                        distress_k=0.05, p_down=0.0, M=300, N=800)
        no_node = UniversalCBPricer(**kwargs).price(**price_kw)
        low_k = UniversalCBPricer(
            **kwargs, scheduled_reset_date=date(2025, 6, 1),
            scheduled_reset_prob=1.0, scheduled_reset_target_k=42.0,
        ).price(**price_kw)
        assert low_k > no_node

    def test_down_reset_floor_caps_reset_value(self):
        """下修价下限绑定时, 下修博弈价值不应高于无下限近似."""
        kwargs = dict(
            S0=18.0, K=30.0,
            current_date=date(2026, 4, 28),
            maturity_date=date(2028, 11, 28),
            issue_date=date(2022, 12, 22),
            conversion_start_date=date(2023, 6, 20),
            redemption_price=115.0,
        )
        no_floor = UniversalCBPricer(**kwargs).price(
            sigma=0.55, r=0.022, base_spread=0.03,
            distress_k=0.05, p_down=0.50, M=160, N=400)
        with_floor = UniversalCBPricer(**kwargs, down_reset_floor=25.0).price(
            sigma=0.55, r=0.022, base_spread=0.03,
            distress_k=0.05, p_down=0.50, M=160, N=400)

        assert with_floor <= no_floor

    def test_down_reset_value_maps_moneyness_and_takes_conversion_floor(self):
        """逐点钉死 `_down_reset_value` 三个分支的算术, 而不只是一个单调不等式.

        既有的 `test_down_reset_floor_caps_reset_value` 只断言 `有下限 <= 无下限`,
        任何**保序**的改写 (premium 挪个位置、K/target_k 写反、face 换成 K) 都能
        让它照常通过 —— 而这段映射是三 regime 下修建模的全部算术。

        ``V`` 刻意取非单调 (先涨后跌): 真实网格上 V 恒 ≥ 转股价值, `np.maximum`
        的转股价值那一侧永远赢不了, 于是那一半算术不可观测。这里两侧都要看得见。
        """
        base = dict(
            S0=20.0, K=20.0,
            current_date=date(2026, 1, 1),
            maturity_date=date(2028, 1, 1),
            issue_date=date(2023, 1, 1),
            conversion_start_date=date(2023, 7, 1),
            down_reset_premium=1.05,
        )
        S = np.array([0.0, 10.0, 20.0, 30.0, 40.0])
        V = np.array([0.0, 100.0, 150.0, 60.0, 20.0])

        # ① 公告给定新 K: 逐点映射到 moneyness 相同的旧网格 (equiv = K·S/target_k
        #    = 0.8·S), 以 face·S/target_k = 4·S 作转股价值下限。两侧交替胜出。
        got = UniversalCBPricer(**base)._down_reset_value(S, V, target_k=25.0)
        assert got == pytest.approx([0.0, 80.0, 130.0, 120.0, 160.0])

        # ② 有下限: target_k = max(S/1.05, 15) → 低股价段被 15 钉住 (equiv 随 S 变),
        #    高股价段 equiv 恒为 K·premium = 21 —— 那个常数就是 premium 的观测点。
        got = UniversalCBPricer(**base, down_reset_floor=15.0)._down_reset_value(S, V)
        assert got == pytest.approx([0.0, 116.666667, 141.0, 141.0, 141.0])

        # ③ 无下限: 退化成标量 max(interp(K·premium), face·premium)。两侧各取一次 ——
        #    只测延续价值胜出那一档时, 「face·premium」整个可以删掉而测试照常绿。
        assert UniversalCBPricer(**base)._down_reset_value(S, V) == pytest.approx(141.0)
        V_low = np.array([0.0, 40.0, 60.0, 70.0, 80.0])   # interp@21 = 61 < 105
        assert UniversalCBPricer(**base)._down_reset_value(S, V_low) == pytest.approx(105.0)

    def test_explicit_putback_window_sets_price_floor(self):
        """已公告回售申报期内, 回售价应成为全状态价格底."""
        pricer = UniversalCBPricer(
            S0=80.0, K=120.0,
            current_date=date(2026, 6, 2),
            maturity_date=date(2027, 6, 2),
            issue_date=date(2022, 6, 2),
            conversion_start_date=date(2022, 12, 2),
            putback_start_date=date(2026, 6, 1),
            putback_end_date=date(2026, 6, 5),
            putback_price=101.2,
            redemption_price=107.0,
        )

        price = pricer.price(
            sigma=0.20, r=0.022, base_spread=0.08,
            distress_k=0.0, p_down=0.0, M=120, N=300)

        assert price >= 101.2


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

    def test_higher_dividend_yield_lowers_price(self):
        """股息率 q 提高会降低未转股状态下的正股风险中性漂移, 理论价不应升高."""
        pricer = UniversalCBPricer(
            S0=50.0, K=52.77,
            current_date=date(2025, 1, 1),
            maturity_date=date(2026, 7, 30),
            issue_date=date(2020, 7, 30),
            conversion_start_date=date(2021, 2, 6),
            redemption_price=107.0,
        )
        p_no_q = pricer.price(sigma=0.28, r=0.022, q=0.0, base_spread=0.03,
                              distress_k=0.0, p_down=0.0, M=200, N=500)
        p_high_q = pricer.price(sigma=0.28, r=0.022, q=0.05, base_spread=0.03,
                                distress_k=0.0, p_down=0.0, M=200, N=500)
        assert p_high_q <= p_no_q + 0.01


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
        with pytest.raises(ValueError, match="positive"):
            base_pricer.price(sigma=-0.1, r=0.02, base_spread=0.03)

    def test_zero_sigma_raises(self, base_pricer):
        with pytest.raises(ValueError, match="positive"):
            base_pricer.price(sigma=0.0, r=0.02, base_spread=0.03)

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

    def test_reported_bond_floor_uses_the_spread_the_model_itself_uses_at_s0(self):
        """``price()`` 报出来的 ``bond_floor`` 必须用**模型自己在 S0 处用的**那个利差.

        上面三条都是符号/单调性检查, ``test_price_decomposition_consistency`` 是拿分解
        跟自己比 —— 没有一条约束**折现率本身**。实测把折现率从 ``r + base_spread`` 改成
        ``r``, 957 条测试全绿, 而长久期债的「纯债底」差 17.35 元 (81.39 → 98.74, +21.2%),
        且这个数直接显示在定价页上。

        它曾只用 ``base_spread``, 而求解器里是
        ``base_spread + distress_k·max(0, 1 − S/K)`` —— 同一只债的同一个量, 两处各算各的。
        实测生产口径 (distress_k=0.05) 下 S0/K=0.38 时报出的债底比模型自用的高 **8.9 元**,
        S0/K=0.09 时高 **12.7 元**; 而 ``option_premium = price − max(bond_floor, parity)``
        直接吃这个差, 会渲染出**负的期权溢价** (实测 −0.250) —— 一个"债底比全价还高"的
        组合, 在模型自己的口径里根本不存在。
        """
        pricer = _long_dated_otm_pricer()
        r, spread, distress_k = 0.022, 0.03, 0.05
        result = pricer.price(sigma=0.28, r=r, base_spread=spread, distress_k=distress_k,
                              p_down=0.0, M=200, N=500, return_greeks=True)

        # 与求解器里 current_spreads 的公式逐字同形, 在 S0 处取值
        spread_at_s0 = spread + distress_k * max(0.0, 1.0 - pricer.S0 / pricer.K)
        assert result["bond_floor"] == pytest.approx(
            pricer.bond_floor_value(pricer.current_date, r + spread_at_s0))

        # 这个 fixture 深度 OTM (S0/K≈0.38), distress 项必须真的起作用 —— 否则这条用例
        # 退化成"跟 base_spread 比", 又变回它原本要防的那个形状
        assert spread_at_s0 > spread + 0.02
        assert result["bond_floor"] < pricer.bond_floor_value(
            pricer.current_date, r + spread) - 3.0

        # 原本那条保护留着: 必须**严格低于**只用 r 折现的值, 差额就是利差的作用
        assert result["bond_floor"] < pricer.bond_floor_value(pricer.current_date, r) - 5.0

        # 期权溢价不再为负
        assert result["option_premium"] > 0


# ── 8.5 信用利差的 distress 扩张 ────────────────────────────
def _long_dated_otm_pricer(**overrides):
    """长久期 + 深度 OTM: distress 折现真正起作用的那一档.

    套件里 68 处 ``.price()`` 调用全都传了 ``distress_k=0.05``, 但用的都是短久期 /
    地板绑定的案例 —— 在那些案例上 ``distress_k`` 从 0 扫到 2.0 价格**一个字节都不变**
    (转股/赎回地板先绑住了)。所以"传了参数"不等于"测到了", 需要这个 fixture。
    """
    kwargs = dict(
        S0=20.0, K=52.77,
        current_date=date(2020, 1, 1),
        maturity_date=date(2026, 7, 30),
        issue_date=date(2020, 1, 1),
        conversion_start_date=date(2020, 7, 1),
        coupon_rates=(0.003, 0.004, 0.008, 0.015, 0.018, 0.02),
        redemption_price=107.0,
    )
    kwargs.update(overrides)
    return UniversalCBPricer(**kwargs)


class TestDistressSpread:
    """``s(S) = base_spread + distress_k · max(0, 1 − S/K)`` 是 README/AGENTS 点名的核心特性.

    实测它此前**没有任何有效覆盖**: 把 ``distress_k`` 那一项乘 0 之后套件仍然
    957 passed, 而长久期 OTM 债的理论价变化 +11.3%, 足以让券在 5pp 的
    ``MIN_RELATIVE_CHEAPNESS`` 闸上跨过去 —— 原因不是没写测试, 是 fixture 全落在死区。
    """

    def test_price_is_strictly_decreasing_in_distress_k(self):
        prices = [
            _long_dated_otm_pricer().price(
                sigma=0.28, r=0.022, base_spread=0.03, distress_k=dk,
                p_down=0.0, M=200, N=500)
            for dk in (0.0, 0.05, 0.5, 2.0)
        ]

        assert prices == sorted(prices, reverse=True), (
            f"价格必须随 distress_k 单调下降, 实得 {prices}")
        # 光"单调"还不够 —— 乘 0 的变异体也满足单调 (全部相等)。要钉住**幅度**:
        # 实测 0.0 → 0.05 这一步就有 ~10.96 元。
        assert prices[0] - prices[1] > 3.0, (
            f"distress_k 0→0.05 只改变了 {prices[0] - prices[1]:.4f} 元, "
            f"扩张项可能已失效")

    def test_short_dated_fixture_is_why_the_long_dated_one_exists(self):
        """把"为什么必须用长久期 fixture"钉成断言, 免得后人把上面那条挪回短久期案例。

        同样是 S0=20 的深度 OTM, 只把估值日从 2020 挪到 2025 (剩 1.6 年), **回售地板**
        就先绑住了价格 —— ``distress_k`` 从 0 扫到 2.0 价格**逐位不变**。

        绑住它的是 ``pricer.py`` 的 ``elif t_prev >= self._put_start_t`` 那一支
        (S ≤ 0.7·K 时 ``V`` 被钉在 ``face + accrued``), 不是赎回价: 估值日 2025-01-01 时
        回售期已从 2024-07-30 起生效, 而 S0=20 ≤ 0.7×52.77 = 36.94, 于是整片低 S 区被
        钉成闭式常数 —— 实测价格 bit-exact 等于 100 + 1.804931506849315 = 101.80493150684931,
        赎回价 107 全程没参与。所以下面那个精确 0 是**常数相等**而不是数值噪声, 不会
        因为 numpy/scipy 版本或平台不同而飘。
        """
        def step(current_date):
            p0, p1 = (
                _long_dated_otm_pricer(current_date=current_date).price(
                    sigma=0.28, r=0.022, base_spread=0.03, distress_k=dk,
                    p_down=0.0, M=200, N=500)
                for dk in (0.0, 0.05)
            )
            return p0 - p1

        short_dated = step(date(2025, 1, 1))
        long_dated = step(date(2020, 1, 1))

        assert short_dated == pytest.approx(0.0, abs=1e-9), (
            f"短久期 OTM 上 distress_k 竟然有区分度了 ({short_dated:.6f} 元) —— "
            f"若属真实改进, 请同步更新本用例与 TestDistressSpread 的说明")
        assert long_dated > 3.0


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


# ── 10. 隐含下修强度与敏感度 ───────────────────────────────
class TestImpliedPDown:

    @pytest.fixture
    def reset_pricer(self):
        return UniversalCBPricer(
            S0=35.0,
            K=52.77,
            current_date=date(2024, 1, 1),
            maturity_date=date(2027, 7, 30),
            issue_date=date(2021, 7, 30),
            conversion_start_date=date(2022, 2, 6),
            redemption_price=107.0,
        )



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

    def test_gamma_positive_under_coarse_grid(self):
        """回归: 高 σ + 长久期把 S_max = exp(3σ√T)·K 撑大, 网格步长远超希腊值扰动步长。

        旧实现对 np.interp (分段线性) 的结果做二阶差分, 三个取值点落进同一线性段时
        Γ 恒等于 0。可转债对正股是凸的, Γ 必须严格为正。
        """
        pricer = UniversalCBPricer(
            S0=97.66, K=104.85,
            current_date=date(2026, 8, 24),
            maturity_date=date(2031, 3, 1),
            # **必须带 down_reset_floor**: 生产上 311/311 只都有它, 而 184 只的 floor
            # 恰好等于 S0 (取数是 max(20日均价, 前一交易日收盘))。不给它就走无 floor
            # 分支 —— 那是生产中不存在的形状, Γ 的守护会看不见真正的那个折点
            # (见 test_frozen_down_reset_floor_puts_a_kink_right_at_s0)。
            down_reset_floor=97.66,
        )
        result = pricer.price(sigma=0.65, r=0.015, q=0.002, base_spread=0.035,
                              p_down=0.25, return_greeks=True)
        assert result["gamma"] > 0, f"Γ={result['gamma']:.8f} 不应退化为 0"

    def test_gamma_stable_across_grid_refinement(self):
        """Γ 应随网格加密收敛, 而不是随网格步长跳变 (旧实现相邻久期可差 4 倍).

        **这条只覆盖 `p_down` 影响不大的那一段。** 全池实测 (Γ ≥ 1e-3 的 242 只,
        M ∈ {500,1000,2000}): 中位漂移 0.21%, 但 11 只 >10%, 最大 111020.SH **77.3%**
        (Γ = 0.484 / 1.021 / 2.129)。四只最差的逐条消融, `p_down=0` 让漂移**全部归零** ——
        那是下修那张面的伪影, 与 `_down_reset_value` 的折点同源 (见 AGENTS
        「已知边界」)。这条守护不去断言那一段, 因为把它变红等于要求一个尚未做的口径变更。
        """
        pricer = UniversalCBPricer(
            S0=97.66, K=104.85,
            current_date=date(2026, 8, 24),
            maturity_date=date(2031, 3, 1),
            # **必须带 down_reset_floor**: 生产上 311/311 只都有它, 而 184 只的 floor
            # 恰好等于 S0 (取数是 max(20日均价, 前一交易日收盘))。不给它就走无 floor
            # 分支 —— 那是生产中不存在的形状, Γ 的守护会看不见真正的那个折点
            # (见 test_frozen_down_reset_floor_puts_a_kink_right_at_s0)。
            down_reset_floor=97.66,
        )
        import convertible_bond.pricer as pricer_mod

        # ① 自适应加密**确实起作用** —— 这才是这条守护真正护住的东西。
        #    这个 fixture 上 S_max 由 σ√T 撑到 5242 (被 _S_MAX_CAP 夹住), 于是
        #    M=500 只在 S0 以下留 9.3 个格点。加密之后必须够 _MIN_NODES_BELOW_S0。
        S_grid, _ = pricer._price_grid(0.65, 0.015, 0.002, 0.035, 0.25, 0.05, 500, 1000)
        nodes_below = pricer.S0 / float(S_grid[1] - S_grid[0])
        # 期望值写**字面量**: 拿 `_MIN_NODES_BELOW_S0 - 1` 当期望等于让被测常数
        # 自己批改自己 —— 实测那样写时把它改成 0 或 20 这条用例照样绿。
        assert nodes_below >= 59, (
            f"S0 以下只有 {nodes_below:.1f} 个格点, 自适应加密没生效")
        assert pricer_mod._MIN_NODES_BELOW_S0 == 60, "改这个下限是模型行为变更"

        # ② 收敛性要在**加密夹不到的那一段**上测。M=500/1000/2000 在这个 fixture 上
        #    全被夹成有效 M=3221 —— 三个 Γ 逐位相同, 相对极差恒等于 0.0, 那条断言
        #    什么也观测不到 (实测: gammas 三个值完全相等)。取夹点以上的 M。
        gammas = [
            pricer.price(sigma=0.65, r=0.015, q=0.002, base_spread=0.035,
                         p_down=0.25, M=M, N=1000, return_greeks=True)["gamma"]
            for M in (3221, 4000, 8000)
        ]
        assert all(g > 0 for g in gammas), f"Γ 出现非正值: {gammas}"
        assert len(set(gammas)) == len(gammas), (
            f"三个 M 给出重复值, 说明它们又落进同一个有效网格, 测不到收敛: {gammas}")
        drift = (max(gammas) - min(gammas)) / max(gammas)
        assert drift < 0.10, f"Γ 随网格步长漂移过大: {gammas} (相对极差 {drift:.1%})"

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

    def test_call_no_redemption_until_suppresses_call_cap(self, itm_kwargs):
        """不强赎承诺期内不应套用强赎 cap; 过期承诺不影响强赎边界."""
        capped = UniversalCBPricer(call_notice_days=0, **itm_kwargs).price(
            sigma=0.30, r=0.022, base_spread=0.03, M=200, N=500)
        blocked = UniversalCBPricer(
            call_notice_days=0,
            call_no_redemption_until=date(2025, 12, 31),
            **itm_kwargs,
        ).price(sigma=0.30, r=0.022, base_spread=0.03, M=200, N=500)
        expired = UniversalCBPricer(
            call_notice_days=0,
            call_no_redemption_until=date(2024, 12, 31),
            **itm_kwargs,
        ).price(sigma=0.30, r=0.022, base_spread=0.03, M=200, N=500)

        assert blocked > capped + 0.5
        assert expired == pytest.approx(capped)


# ── 12. 回测 (backtest with FakeProvider) ────────────────────
from convertible_bond.data_providers import DataProvider, BondTerms, CashflowSchedule


class FakeProvider(DataProvider):
    """直接实现 DataProvider 接口的最小桩, 给回测/批量定价测试用."""
    name = "fake"

    def terms_as_of(self, bond_code, valuation_date):
        """桩返回的条款按定义就是估值日当天的, 因此锚是估值日本身。

        不声明这个锚, price_from_provider 会回落到**真实项目 patch 库**
        (default_terms_patch_store), 于是用例的合成条款被真实数据覆盖 —— 测试结果
        随 data/cb_terms_patches.json 的内容漂移。曾实测: 转股价 patch 重建后
        123001.SZ 多了 5 条 patch, 把 K 改成 4.28, 理论价从合理区间跳到 1280。
        """
        return valuation_date

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

    def test_backtest_uses_terms_as_of_each_sample_date(self, fake_provider, monkeypatch):
        """单债回测不应把区间末/当前转股价带回每个历史采样日."""
        from dataclasses import replace
        import convertible_bond.backtest as bt

        provider, start, end = fake_provider
        switch_date = date(2025, 5, 1)
        seen: list[tuple[date, float]] = []
        requested_terms_dates = []

        def get_terms(_bond_code, valuation_date):
            requested_terms_dates.append(valuation_date)
            k = 45.0 if valuation_date >= switch_date else 52.77
            return replace(provider.terms, conversion_price=k)

        class SpyPricer:
            def __init__(self, *args, **kwargs):
                seen.append((kwargs["current_date"], float(kwargs["K"])))
                self.ratio = 100.0 / float(kwargs["K"])

            def price(self, **_kwargs):
                return 100.0

            def bond_floor_value(self, *_args, **_kwargs):
                return 95.0

            def spread_at_s0(self, base_spread, distress_k):
                return float(base_spread)

        monkeypatch.setattr(provider, "get_bond_terms", get_terms)
        monkeypatch.setattr(bt, "UniversalCBPricer", SpyPricer)

        result = bt.backtest_theoretical_price(
            "123001.SZ", start_date=start, end_date=end,
            freq="M", M=80, N=200, provider=provider,
        )

        assert seen
        assert len(set(requested_terms_dates)) > 2
        assert all(
            k == (45.0 if val_date >= switch_date else 52.77)
            for val_date, k in seen
        )
        assert result["conversion_prices"] == [k for _, k in seen]

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

            def spread_at_s0(self, base_spread, distress_k):
                return float(base_spread)

        monkeypatch.setattr(bt, "UniversalCBPricer", SpyPricer)

        bt.backtest_theoretical_price(
            "123001.SZ", start_date=start, end_date=end,
            freq="M", p_down=0.15, M=80, N=200, provider=provider,
            # 关掉历史条款投影: 这条测的是"字段有没有传到 pricer"这段管道, 而投影层
            # 会把无公告支撑的状态字段当未来信息剥掉 —— fixture 正是直接往 terms 上
            # 塞的。默认开着这一层由 test_backtest_wraps_the_provider_… 单独守。
            point_in_time=False,
        )

        assert seen_p_down
        assert all(p == 0.0 for p in seen_p_down)

    def test_backtest_passes_call_no_redemption_until(self, fake_provider, monkeypatch):
        """回测也要把不强赎承诺窗口传给 UniversalCBPricer."""
        import convertible_bond.backtest as bt

        provider, start, end = fake_provider
        provider.terms.call_no_redemption_until = date(2025, 12, 31)
        seen_until = []

        class SpyPricer:
            def __init__(self, *args, **kwargs):
                seen_until.append(kwargs.get("call_no_redemption_until"))
                self.ratio = 100.0 / float(kwargs["K"])

            def price(self, **_kwargs):
                return 100.0

            def bond_floor_value(self, *_args, **_kwargs):
                return 95.0

            def spread_at_s0(self, base_spread, distress_k):
                return float(base_spread)

        monkeypatch.setattr(bt, "UniversalCBPricer", SpyPricer)

        bt.backtest_theoretical_price(
            "123001.SZ", start_date=start, end_date=end,
            freq="M", p_down=0.15, M=80, N=200, provider=provider,
            # 关掉历史条款投影: 这条测的是"字段有没有传到 pricer"这段管道, 而投影层
            # 会把无公告支撑的状态字段当未来信息剥掉 —— fixture 正是直接往 terms 上
            # 塞的。默认开着这一层由 test_backtest_wraps_the_provider_… 单独守。
            point_in_time=False,
        )

        assert seen_until
        assert all(d == date(2025, 12, 31) for d in seen_until)

    def test_backtest_rejects_missing_maturity_date(self, fake_provider):
        from convertible_bond.backtest import backtest_theoretical_price

        provider, start, end = fake_provider
        provider.terms.maturity_date = None

        with pytest.raises(ValueError, match="数据源未返回到期日"):
            backtest_theoretical_price(
                "123001.SZ", start_date=start, end_date=end,
                freq="M", M=80, N=200, provider=provider,
            )

    def test_backtest_progress_finishes_when_trailing_points_are_skipped(
        self, fake_provider, monkeypatch,
    ):
        import convertible_bond.backtest as bt

        provider, start, end = fake_provider
        provider.terms.maturity_date = date(2025, 7, 15)
        progress = []

        class SpyPricer:
            def __init__(self, *args, **kwargs):
                self.ratio = 100.0 / float(kwargs["K"])

            def price(self, **_kwargs):
                return 100.0

            def bond_floor_value(self, *_args, **_kwargs):
                return 95.0

            def spread_at_s0(self, base_spread, distress_k):
                return float(base_spread)

        monkeypatch.setattr(bt, "UniversalCBPricer", SpyPricer)

        bt.backtest_theoretical_price(
            "123001.SZ", start_date=start, end_date=end,
            freq="M", M=80, N=200, provider=provider,
            progress_cb=lambda done, total: progress.append((done, total)),
        )

        assert progress
        assert progress[-1][0] == progress[-1][1]


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
        assert result["q"] == 0.0

    def test_price_from_provider_reads_dividend_yield(self, fake_provider):
        """provider 返回的股息率是百分数, price_from_provider 应转成模型小数 q."""
        from convertible_bond.pricing_api import price_from_provider

        provider, _, end = fake_provider
        provider.get_stock_dividend_yield = lambda stock_code, on_date: 2.5  # type: ignore[method-assign]

        result = price_from_provider(
            provider, "123001.SZ",
            valuation_date=end, M=80, N=200,
        )

        assert result["q"] == pytest.approx(0.025)

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

    def test_price_from_provider_passes_call_no_redemption_until(self, fake_provider):
        """单只定价应把不强赎承诺窗口传入模型, 并在结果里暴露."""
        from convertible_bond.pricing_api import price_from_provider

        provider, _, end = fake_provider
        provider.terms.call_no_redemption_until = date(2025, 12, 31)

        result = price_from_provider(
            provider, "123001.SZ",
            valuation_date=end, M=80, N=200,
        )

        assert result["call_no_redemption_until"] == date(2025, 12, 31)

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

    def test_price_from_provider_reads_cb_events_effective_end(self, fake_provider, tmp_path, monkeypatch):
        """单只定价应直接用 cb_events 的 effective_end, 不要求先 apply 到 cb_data."""
        from convertible_bond import cb_events as cbe
        from convertible_bond import down_reset_overrides as dro
        from convertible_bond.pricing_api import price_from_provider

        provider, _, end = fake_provider
        provider.terms.down_reset_cooldown_months = 6

        store = cbe.CBEventStore(tmp_path / "cb_events.json")
        store.add_many([
            cbe.CBEvent(
                bond_code="123001.SZ",
                event_date=date(2025, 4, 13),
                event_type="down_reset_rejected",
                raw_title="关于不向下修正测试转债转股价格的公告",
                effective_start=date(2025, 4, 14),
                effective_end=date(2025, 7, 12),
                commitment_months=3,
            ),
        ])
        monkeypatch.setattr(cbe, "_default_event_store", store)
        monkeypatch.setattr(
            dro,
            "_default_overrides",
            dro.DownResetOverrides(tmp_path / "down_reset_overrides.json"),
        )

        early = dro.resolve_down_reset(
            "123001.SZ",
            provider.terms,
            valuation_date=date(2025, 4, 1),
        )
        assert early.block_until is None

        result = price_from_provider(
            provider, "123001.SZ",
            valuation_date=end, p_down=0.15, M=80, N=200,
        )

        assert result["down_reset_announce_date"] == date(2025, 4, 13)
        assert result["down_reset_block_until"] == date(2025, 7, 12)
        assert result["down_reset_cooldown_months"] == 3
        assert "event_end=2025-07-12" in result["down_reset_note"]
        assert "不向下修正测试转债" in result["down_reset_note"]

    def test_price_from_provider_prefers_latest_event_over_stale_cb_data(self, fake_provider, tmp_path, monkeypatch):
        """cb_data 里旧不下修字段存在时, cb_events 最新公告仍应覆盖它."""
        from convertible_bond import cb_events as cbe
        from convertible_bond import down_reset_overrides as dro
        from convertible_bond.pricing_api import price_from_provider

        provider, _, end = fake_provider
        provider.terms.down_reset_block_until = date(2025, 6, 30)
        provider.terms.down_reset_note = "旧不下修公告"

        store = cbe.CBEventStore(tmp_path / "cb_events.json")
        store.add_many([
            cbe.CBEvent(
                bond_code="123001.SZ",
                event_date=date(2025, 7, 1),
                event_type="down_reset_rejected",
                raw_title="关于不向下修正测试转债转股价格的新公告",
                effective_end=date(2025, 10, 1),
                commitment_months=3,
            ),
        ])
        monkeypatch.setattr(cbe, "_default_event_store", store)
        monkeypatch.setattr(
            dro,
            "_default_overrides",
            dro.DownResetOverrides(tmp_path / "down_reset_overrides.json"),
        )

        result = price_from_provider(
            provider, "123001.SZ",
            valuation_date=end, p_down=0.15, M=80, N=200,
        )

        assert result["down_reset_block_until"] == date(2025, 10, 1)
        assert "新公告" in result["down_reset_note"]

    def test_price_from_provider_schedules_reset_for_active_proposal(self, fake_provider, tmp_path, monkeypatch):
        """董事会已提议下修但未落地/否决时, 单只定价应输出一次性下修节点 (regime ②),

        而不再把背景 hazard 抬升数倍: effective_p_down 保持背景值, 另给出
        scheduled_reset_date (提议日 + 表决滞后) 与 scheduled_prob (通过率)。
        """
        from convertible_bond import cb_events as cbe
        from convertible_bond import down_reset_overrides as dro
        from convertible_bond.pricing_api import price_from_provider

        provider, _, end = fake_provider
        store = cbe.CBEventStore(tmp_path / "cb_events.json")
        store.add_many([
            cbe.CBEvent(
                bond_code="123001.SZ",
                event_date=date(2025, 8, 1),
                event_type="down_reset_proposed",
                raw_title="关于董事会提议向下修正测试转债转股价格的公告",
            ),
        ])
        monkeypatch.setattr(cbe, "_default_event_store", store)
        monkeypatch.setattr(
            dro,
            "_default_overrides",
            dro.DownResetOverrides(tmp_path / "down_reset_overrides.json"),
        )

        result = price_from_provider(
            provider, "123001.SZ",
            valuation_date=end, p_down=0.15, M=80, N=200,
        )

        assert result["down_reset_proposed_date"] == date(2025, 8, 1)
        # 背景强度不再被 ×3 抬升
        assert result["base_p_down"] == pytest.approx(0.15)
        assert result["effective_p_down"] == pytest.approx(0.15)
        assert result["p_down"] == pytest.approx(0.15)
        # 一次性下修节点: 提议日 + PROPOSED_EFFECTIVE_LAG_DAYS, 概率 = PROPOSED_PASS_PROB
        assert result["down_reset_scheduled_date"] == (
            date(2025, 8, 1) + timedelta(days=dro.PROPOSED_EFFECTIVE_LAG_DAYS))
        assert result["down_reset_scheduled_prob"] == pytest.approx(dro.PROPOSED_PASS_PROB)
        assert result["down_reset_scheduled_kind"] == "proposed"

    def test_price_from_provider_schedules_reset_for_approved_pending(self, fake_provider, tmp_path, monkeypatch):
        """已通过但新转股价尚未生效时, 应输出 kind=approved 的近确定下修节点 (生效日 > 估值日)。"""
        from convertible_bond import cb_events as cbe
        from convertible_bond import down_reset_overrides as dro
        from convertible_bond.pricing_api import price_from_provider

        provider, _, end = fake_provider  # end = 2025-08-31
        store = cbe.CBEventStore(tmp_path / "cb_events.json")
        store.add_many([
            cbe.CBEvent(
                bond_code="123001.SZ",
                event_date=date(2025, 8, 25),
                event_type="down_reset_approved",
                raw_title="关于向下修正测试转债转股价格的公告",
                effective_end=date(2025, 9, 10),  # 生效日仍在未来
            ),
        ])
        monkeypatch.setattr(cbe, "_default_event_store", store)
        monkeypatch.setattr(
            dro, "_default_overrides",
            dro.DownResetOverrides(tmp_path / "down_reset_overrides.json"),
        )

        result = price_from_provider(
            provider, "123001.SZ",
            valuation_date=end, p_down=0.15, M=80, N=200,
        )

        assert result["down_reset_approved_effective_date"] == date(2025, 9, 10)
        assert result["down_reset_scheduled_kind"] == "approved"
        assert result["down_reset_scheduled_date"] == date(2025, 9, 10)
        assert result["down_reset_scheduled_prob"] == pytest.approx(dro.APPROVED_PASS_PROB)

    def test_price_from_provider_ignores_already_effective_approval(self, fake_provider, tmp_path, monkeypatch):
        """生效日已过的下修不再叠加节点 (防双计), 回落背景强度。"""
        from convertible_bond import cb_events as cbe
        from convertible_bond import down_reset_overrides as dro
        from convertible_bond.pricing_api import price_from_provider

        provider, _, end = fake_provider  # end = 2025-08-31
        store = cbe.CBEventStore(tmp_path / "cb_events.json")
        store.add_many([
            cbe.CBEvent(
                bond_code="123001.SZ",
                event_date=date(2025, 6, 1),
                event_type="down_reset_approved",
                raw_title="关于向下修正测试转债转股价格的公告",
                effective_end=date(2025, 6, 10),  # 生效日已过
            ),
        ])
        monkeypatch.setattr(cbe, "_default_event_store", store)
        monkeypatch.setattr(
            dro, "_default_overrides",
            dro.DownResetOverrides(tmp_path / "down_reset_overrides.json"),
        )

        result = price_from_provider(
            provider, "123001.SZ",
            valuation_date=end, p_down=0.15, M=80, N=200,
        )

        assert result["down_reset_approved_effective_date"] is None
        assert result["down_reset_scheduled_kind"] is None
        assert result["down_reset_scheduled_prob"] == 0.0
        assert result["effective_p_down"] == pytest.approx(0.15)

    def test_price_from_provider_uses_announced_new_k_as_target(self, fake_provider, tmp_path, monkeypatch):
        """提议公告带 event_price 时, 节点目标 K 应透传成公告新 K (而非估算)。"""
        from convertible_bond import cb_events as cbe
        from convertible_bond import down_reset_overrides as dro
        from convertible_bond.pricing_api import price_from_provider

        provider, _, end = fake_provider
        store = cbe.CBEventStore(tmp_path / "cb_events.json")
        store.add_many([
            cbe.CBEvent(
                bond_code="123001.SZ",
                event_date=date(2025, 8, 1),
                event_type="down_reset_proposed",
                raw_title="关于董事会提议向下修正测试转债转股价格的公告",
                event_price=6.20,  # 公告解析出的下修后新转股价
            ),
        ])
        monkeypatch.setattr(cbe, "_default_event_store", store)
        monkeypatch.setattr(
            dro, "_default_overrides",
            dro.DownResetOverrides(tmp_path / "down_reset_overrides.json"),
        )

        result = price_from_provider(
            provider, "123001.SZ",
            valuation_date=end, p_down=0.15, M=80, N=200,
        )

        assert result["down_reset_scheduled_kind"] == "proposed"
        assert result["down_reset_scheduled_target_k"] == pytest.approx(6.20)


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

        from convertible_bond.market_time import market_today

        provider = object.__new__(AkshareDataProvider)
        provider._ak = FakeAk()

        # 当日: 实时快照是合法兜底
        assert provider.get_stock_close("000001.SZ", market_today()) == 12.34

        # **历史估值日: 不许拿实时快照顶**。那是未来的价, 而 S0 驱动整个 PDE。
        # 回测确实走得到这条路 (_BacktestCacheProvider → DiskCacheProvider →
        # HistoricalBondDataProvider → CachedBondDataProvider → 这里), 而那个
        # (D-15, D) 的窄请求两层回测缓存都不接 —— 正股停牌超过 15 天、或那次请求恰好
        # 撞上东财按 IP 封禁, 就会把今天的价当成 D 的 S0。实测停牌起 2022-06-06、
        # 估值日 2022-06-30、spot=999 时: status ok、S0 999、理论价 9990、
        # deviation −98.8%, 且 max_model_premium 拦不住 (parity 同样按 S0 缩放),
        # 于是它以 confidence 高 排在候选第一。
        with pytest.raises(RuntimeError, match="实时快照只适用于当日"):
            provider.get_stock_close("000001.SZ", date(2025, 1, 10))

    def test_stock_close_warns_when_history_price_is_stale(self, caplog):
        from convertible_bond.data_providers import AkshareDataProvider

        provider = object.__new__(AkshareDataProvider)
        provider.get_stock_history = lambda *_args: [(date(2025, 1, 2), 10.5)]

        with caplog.at_level("WARNING", logger="convertible_bond.data_providers.akshare"):
            close = provider.get_stock_close("000001.SZ", date(2025, 1, 20))

        assert close == 10.5
        assert "使用 2025-01-02 的收盘价" in caplog.text

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

    def test_dividend_yield_uses_lg_indicator_on_date(self):
        """股息率应取估值日之前最近一条指标, 单位保持为百分数."""
        import pandas as pd
        from convertible_bond.data_providers import AkshareDataProvider

        class FakeAk:
            def stock_a_indicator_lg(self, symbol):
                assert symbol == "000001"
                return pd.DataFrame({
                    "trade_date": ["2025-01-02", "2025-01-10", "2025-02-01"],
                    "dv_ratio": [1.2, "2.5%", 3.0],
                })

        provider = object.__new__(AkshareDataProvider)
        provider._ak = FakeAk()

        assert provider.get_stock_dividend_yield("000001.SZ", date(2025, 1, 15)) == 2.5

    def test_bond_terms_derives_historical_conversion_price_from_value_analysis(self):
        """bond_zh_cov 只有当前 K; 历史估值日应从转股价值反推历史 K."""
        import pandas as pd
        from convertible_bond.data_providers import AkshareDataProvider

        class FakeAk:
            def bond_zh_cov(self):
                return pd.DataFrame({
                    "债券代码": ["110073"],
                    "债券简称": ["国投转债"],
                    "正股代码": ["600061"],
                    "正股简称": ["国投资本"],
                    "转股价": [9.42],
                    "债现价": [106.75],
                    "信用评级": ["AAA"],
                    "上市时间": ["2020-08-20"],
                    "申购日期": ["2020-07-24"],
            })

            def bond_cb_profile_sina(self, symbol):
                assert symbol == "sh110073"
                return pd.DataFrame({
                    "item": ["到期日", "起息日期", "利率说明", "发行规模（亿元）"],
                    "value": [
                        "2026-07-24",
                        "2020-07-24",
                        "第一年0.2%、第二年0.4%",
                        "80",
                    ],
                })

            def bond_zh_cov_value_analysis(self, symbol):
                assert symbol == "110073"
                return pd.DataFrame({
                    "日期": ["2024-01-31"],
                    "收盘价": [107.119],
                    "转股价值": [69.1511387164],
                })

            def stock_zh_a_hist(self, **kwargs):
                assert kwargs["symbol"] == "600061"
                return pd.DataFrame({
                    "日期": ["2024-01-31"],
                    "收盘": [6.68],
                })

            def stock_zh_a_daily(self, **kwargs):
                raise RuntimeError("daily fallback should not be used")

        provider = object.__new__(AkshareDataProvider)
        provider._ak = FakeAk()
        provider._cb_list_cache = None
        provider._profile_cache = {}
        provider._value_analysis_cache = {}
        provider._historical_k_cache = {}

        terms = provider.get_bond_terms("110073.SH", date(2024, 1, 31))

        assert terms.conversion_price == pytest.approx(9.66)
        assert terms.close == pytest.approx(107.119)

    def test_historical_list_tradable_cbs_is_not_supported(self):
        from convertible_bond.data_providers import AkshareDataProvider

        provider = object.__new__(AkshareDataProvider)
        with pytest.raises(NotImplementedError):
            provider.list_tradable_cbs(date(2024, 1, 31))


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

    def test_missing_terms_file_raises_clear_error(self, tmp_path):
        from convertible_bond.data_providers import CSVDataProvider

        provider = CSVDataProvider(tmp_path)

        with pytest.raises(FileNotFoundError, match="未找到转债条款"):
            provider.get_bond_terms("123001.SZ", date(2025, 8, 31))

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
            "call_no_redemption_until": "2025-12-31",
        }), encoding="utf-8")

        provider = CSVDataProvider(tmp_path)
        terms = provider.get_bond_terms("123001.SZ", date(2025, 8, 31))

        assert terms.down_reset_block_until == date(2025, 9, 30)
        assert terms.down_reset_p_scale == 0.25
        assert terms.down_reset_note == "csv override"
        assert terms.down_reset_cooldown_months == 6
        assert terms.call_no_redemption_until == date(2025, 12, 31)


# ── 16. PDE 收敛性与应力测试 ────────────────────────────────
class TestPDEConvergence:

    def test_mesh_refinement_converges(self):
        """M×N 翻倍后理论价变化应足够小 (< 0.5 元), 即网格已收敛."""
        pricer = UniversalCBPricer(
            S0=50.0, K=52.77,
            current_date=date(2025, 1, 1),
            maturity_date=date(2026, 7, 30),
            issue_date=date(2020, 7, 30),
            conversion_start_date=date(2021, 2, 6),
            coupon_rates=(0.003, 0.004, 0.008, 0.015, 0.018, 0.02),
            redemption_price=107.0,
        )
        p0 = pricer.price(sigma=0.28, r=0.022, q=0.01, base_spread=0.03,
                          p_down=0.05, distress_k=0.03, M=200, N=500)
        p1 = pricer.price(sigma=0.28, r=0.022, q=0.01, base_spread=0.03,
                          p_down=0.05, distress_k=0.03, M=400, N=1000)
        p2 = pricer.price(sigma=0.28, r=0.022, q=0.01, base_spread=0.03,
                          p_down=0.05, distress_k=0.03, M=800, N=2000)

        # M=400→800 的变化应 < M=200→400 的变化 (收敛)
        d1 = abs(p1 - p0)
        d2 = abs(p2 - p1)
        assert d1 < 3.0, f"粗网格 → 中网格 变动 {d1:.2f} 元, 超出预期"
        assert d2 < 0.5, f"中网格 → 细网格 变动 {d2:.2f} 元, 未收敛"

    def test_default_grids_produce_similar_price(self):
        """批量 (M=300/N=1000) 与单只 (M=500/N=2000) 默认网格定价接近."""
        pricer = UniversalCBPricer(
            S0=50.0, K=52.77,
            current_date=date(2025, 1, 1),
            maturity_date=date(2026, 7, 30),
            issue_date=date(2020, 7, 30),
            conversion_start_date=date(2021, 2, 6),
            redemption_price=107.0,
        )
        p_batch = pricer.price(sigma=0.28, r=0.022, q=0.0, base_spread=0.03,
                               M=300, N=1000)
        p_single = pricer.price(sigma=0.28, r=0.022, q=0.0, base_spread=0.03,
                                M=500, N=2000)
        assert abs(p_single - p_batch) < 0.3, \
            f"批量 {p_batch:.3f} vs 单只 {p_single:.3f}, 偏差 {abs(p_single - p_batch):.4f} > 0.3"


class TestPDEStress:

    def test_low_sigma_behaves_sensibly(self):
        """极低波动率 (1%) 下, 定价仍合理且不崩溃."""
        pricer = UniversalCBPricer(
            S0=100.0, K=100.0,
            current_date=date(2025, 1, 1),
            maturity_date=date(2025, 12, 31),
            issue_date=date(2023, 1, 1),
            conversion_start_date=date(2023, 7, 1),
            coupon_rates=(0.01,),
            redemption_price=107.0,
        )
        p = pricer.price(sigma=0.01, r=0.02, q=0.0, base_spread=0.02,
                         M=300, N=1000)
        assert p > 0
        bf = pricer.bond_floor_value(date(2025, 1, 1), 0.02 + 0.02)
        assert p > bf * 0.95

    def test_high_sigma_behaves_sensibly(self):
        """极高波动率 (200%) 下, 定价不崩溃且 ≥ 转股价值."""
        pricer = UniversalCBPricer(
            S0=100.0, K=100.0,
            current_date=date(2025, 1, 1),
            maturity_date=date(2025, 12, 31),
            issue_date=date(2023, 1, 1),
            conversion_start_date=date(2023, 7, 1),
            coupon_rates=(0.01,),
            redemption_price=107.0,
        )
        p = pricer.price(sigma=2.0, r=0.02, q=0.0, base_spread=0.02,
                         M=300, N=1000)
        assert p > 0
        parity = 100.0 / 100.0 * 100
        assert p >= parity * 0.95, f"高 σ 定价 {p:.2f} 不应远低于转股价值 {parity:.2f}"

    def test_very_short_maturity(self):
        """极短剩余期限 (1 天) 定价不 crash, 应接近 max(parity, redeem)."""
        pricer = UniversalCBPricer(
            S0=100.0, K=100.0,
            current_date=date(2025, 12, 30),
            maturity_date=date(2025, 12, 31),
            issue_date=date(2023, 1, 1),
            conversion_start_date=date(2023, 7, 1),
            coupon_rates=(0.02,),
            redemption_price=107.0,
        )
        p = pricer.price(sigma=0.30, r=0.02, q=0.0, base_spread=0.02,
                         M=200, N=500)
        parity = 100.0
        assert abs(p - max(107.0, parity)) < 5.0, \
            f"T=1天 定价 {p:.1f} 远离 max(redeem, parity)={max(107.0, parity):.1f}"

    def test_deep_otm_approaches_bond_floor(self):
        """深度虚值 (S≪K) 时, 理论价接近纯债价值."""
        pricer = UniversalCBPricer(
            S0=10.0, K=100.0,
            current_date=date(2025, 1, 1),
            maturity_date=date(2025, 12, 31),
            issue_date=date(2023, 1, 1),
            conversion_start_date=date(2023, 7, 1),
            coupon_rates=(0.02,),
            redemption_price=107.0,
        )
        p = pricer.price(sigma=0.30, r=0.02, q=0.0, base_spread=0.03,
                         M=300, N=1000)
        bf = pricer.bond_floor_value(date(2025, 1, 1), 0.02 + 0.03)
        assert p >= bf * 0.9
        assert p < 120, f"深度虚值定价 {p:.1f} 不应显著高于纯债值 {bf:.1f}"

    def test_deep_itm_tracks_parity(self):
        """深度实值 (S≫K) 时, 定价应接近转股价值."""
        pricer = UniversalCBPricer(
            S0=300.0, K=100.0,
            current_date=date(2025, 1, 1),
            maturity_date=date(2025, 12, 31),
            issue_date=date(2023, 1, 1),
            conversion_start_date=date(2023, 7, 1),
            coupon_rates=(0.01,),
            redemption_price=107.0,
        )
        p = pricer.price(sigma=0.30, r=0.02, q=0.0, base_spread=0.03,
                         M=300, N=1000)
        parity = 300.0 / 100.0 * 100  # 300
        # 深度 ITM 会触发强赎 cap, price 不应远超 parity
        assert abs(p - parity) / parity < 0.20, \
            f"深度 ITM 定价 {p:.1f} 距转股价值 {parity:.1f} 偏差 {(abs(p-parity)/parity)*100:.1f}%"

    def test_high_dividend_yield_reduces_drift(self):
        """q 接近 r 时股价漂移趋零, OTM 定价应明显低于 q=0 情形."""
        pricer = UniversalCBPricer(
            S0=80.0, K=100.0,
            current_date=date(2025, 1, 1),
            maturity_date=date(2025, 12, 31),
            issue_date=date(2023, 1, 1),
            conversion_start_date=date(2023, 7, 1),
            coupon_rates=(0.01,),
            redemption_price=107.0,
        )
        p_no_q = pricer.price(sigma=0.30, r=0.03, q=0.0, base_spread=0.02,
                              M=300, N=1000)
        p_high_q = pricer.price(sigma=0.30, r=0.03, q=0.025, base_spread=0.02,
                                M=300, N=1000)
        assert p_high_q < p_no_q, \
            f"高股息率应降低 OTM 定价: q=0 → {p_no_q:.2f}, q=0.025 → {p_high_q:.2f}"




class TestThetaAccuracy:
    """Θ 此前**一条守护都没有** —— 124 条用例全绿地放过了 +64% 的误差。"""

    @staticmethod
    def _pricer():
        return UniversalCBPricer(
            S0=55.0, K=52.77,
            current_date=date(2026, 4, 20), maturity_date=date(2029, 7, 30),
            issue_date=date(2023, 7, 30), conversion_start_date=date(2024, 2, 6),
            coupon_rates=(0.003, 0.004, 0.008, 0.015, 0.018, 0.02),
            redemption_price=107.0, call_notice_days=30,
        )

    def test_theta_matches_the_pde_identity(self):
        """Θ 必须与 PDE 恒等式一致 —— 这是与网格完全独立的第二条推导。

        PDE 是 ``∂V/∂t + ½σ²S²V_SS + (r−q)S·V_S − (r+s)V = 0``, 所以
        ``∂V/∂t = (r+s)V − (r−q)S·Δ − ½σ²S²·Γ``。在没有离散票息落在窗口内、也没有约束
        绑定的点上它是精确的, 且只用已经算好的 Δ/Γ, 不碰时间网格。

        旧实现"把明天当成新的一只债重解一遍": 两次求解的 dt 与 S_max (= exp(3σ√T)·K,
        随 T 变) 都不同, 而 Θ·1日 相对价格只有 ~2e-5 —— 两个各带 O(dt²) 误差的数相减,
        误差不抵消反而被放大。实测偏差 **N=1000 +63.8% / N=2000 +9.5%**, 而且在 M 上
        也不收敛到恒等式 (M=19200 仍偏 +33%)。
        """
        p = self._pricer()
        r, q, spread, sigma = 0.022, 0.0, 0.03, 0.28
        g = p.price(sigma, r, base_spread=spread, distress_k=0.0, p_down=0.0,
                    q=q, M=300, N=1000, return_greeks=True)

        identity = ((r + spread) * g["price"]
                    - (r - q) * p.S0 * g["delta"]
                    - 0.5 * sigma ** 2 * p.S0 ** 2 * g["gamma"]) / 365.0
        assert g["theta"] == pytest.approx(identity, rel=0.05), (
            f"Θ={g['theta']:.6f} 与 PDE 恒等式 {identity:.6f} 不符")

    def test_theta_is_stable_across_the_time_grid(self):
        """Θ 不该随 N 漂 —— 网格是数值手段, 不是模型参数。

        旧实现在生产网格上 N=1000→N=2000 就从 +0.003561 跳到 +0.002380 (差 33%),
        而用户看到的只是「Θ」那一格换了个数字。批量默认 N=1000、单只 N=2000, 两页
        对同一只债会给出差三分之一的 Θ。
        """
        p = self._pricer()
        thetas = [
            p.price(0.28, 0.022, base_spread=0.03, distress_k=0.0, p_down=0.0,
                    M=300, N=N, return_greeks=True)["theta"]
            for N in (1000, 2000, 4000)
        ]
        assert max(thetas) - min(thetas) < 0.02 * abs(thetas[0]), (
            f"Θ 随 N 漂: {thetas}")

    def test_theta_costs_no_extra_pde_solve(self):
        """截片法必须真的省掉那次重解 —— 否则改动只换了准确度没换成本。

        ``return_greeks=True`` 原本解 3 次 (基准 + vega 扰动 + 明天), 现在 2 次。
        直接数 ``_price_grid`` 的调用次数, 不看墙钟 (那会在 CI 上抖)。
        """
        p = self._pricer()
        calls = []
        real = p._price_grid

        def counted(*a, **k):
            calls.append(1)
            return real(*a, **k)

        p._price_grid = counted
        p.price(0.28, 0.022, base_spread=0.03, distress_k=0.0, p_down=0.0,
                M=300, N=500, return_greeks=True)
        assert len(calls) == 2, f"解了 {len(calls)} 次, 期望 2 次 (基准 + vega)"

    def test_theta_is_nan_when_there_is_no_room_for_a_slice(self):
        """只剩一步时截不到片, Θ 没有意义 —— 必须是 NaN 而不是 0。

        0 会被读成"这只债没有时间价值衰减", 而真相是"算不出来"。
        """
        import math

        p = UniversalCBPricer(
            S0=55.0, K=52.77,
            current_date=date(2026, 4, 20), maturity_date=date(2026, 4, 21),
            issue_date=date(2023, 7, 30), conversion_start_date=date(2024, 2, 6),
            coupon_rates=(0.02,), redemption_price=107.0,
        )
        g = p.price(0.28, 0.022, base_spread=0.03, M=300, N=1,
                    return_greeks=True)
        assert math.isnan(g["theta"])


class TestPutbackClauseAbsence:
    """没有回售条款的债不许被套用默认触发比。"""

    @staticmethod
    def _kwargs(**over):
        base = dict(
            S0=10.0, K=10.0, current_date=date(2026, 9, 2),
            maturity_date=date(2029, 9, 2), issue_date=date(2023, 9, 2),
            coupon_rates=(0.003, 0.005, 0.01, 0.015, 0.018, 0.02),
            redemption_price=108.0, call_notice_days=30,
        )
        base.update(over)
        return base

    def test_none_trigger_ratio_disables_the_putback_entirely(self):
        """``put_trigger_ratio=None`` = 这只债**没有回售条款**。

        ``pricing_api`` 只在 ``terms.put_trigger_pct`` 非空时才传这个参数, 于是银行/券商
        转债 (实测全库 69 只、主池 5 只: 上银/财通/兴业/重银/常银 —— 它们本来就没有回售)
        落到 pricer 的默认 0.7, 被凭空造出一个回售权。

        今天影响极小 —— 那 5 只 S/K 都在 0.87 以上, 底够不着, 实测最大 0.0014 元 ——
        但那只是当前位置的巧合: 把常银转债的正股放到 0.60K, 虚构的回售底让它**虚增
        5.614 元**。
        """
        deep_otm = self._kwargs(S0=6.0)      # S/K = 0.6, 深在 0.7 触发线之下
        with_put = UniversalCBPricer(**deep_otm).price(
            0.20, 0.022, base_spread=0.03, distress_k=0.05, p_down=0.0, M=300, N=600)
        without = UniversalCBPricer(**dict(deep_otm, put_trigger_ratio=None)).price(
            0.20, 0.022, base_spread=0.03, distress_k=0.05, p_down=0.0, M=300, N=600)
        assert with_put > without + 1.0, (
            f"关掉回售没有降低价格 ({with_put:.4f} vs {without:.4f}), "
            "说明这个 fixture 的回售底本来就不起作用, 测不到东西")

        # 有条款的照常生效
        explicit = UniversalCBPricer(**dict(deep_otm, put_trigger_ratio=0.7)).price(
            0.20, 0.022, base_spread=0.03, distress_k=0.05, p_down=0.0, M=300, N=600)
        assert explicit == pytest.approx(with_put)

    def test_pricing_api_passes_none_when_the_bond_has_no_put_clause(self):
        """空值要**显式关掉**, 不能什么都不传 —— 不传就落到默认 0.7。

        原来这条扫的是 `price_from_provider` 的**源码文本**, 于是这段口径抽成
        `build_pricer_kwargs` 共用之后它就红了 —— 而口径本身一个字没变。
        改成直接问构建器: 键必须在, 值必须是 None。
        """
        from convertible_bond.data_providers import BondTerms
        from convertible_bond.pricing_api import build_pricer_kwargs

        base = dict(
            underlying_code="300953.SZ", conversion_price=10.0,
            issue_date=date(2023, 1, 1), maturity_date=date(2029, 1, 1),
            face_value=100.0,
        )
        no_put, _ = build_pricer_kwargs(
            "128000.SZ", BondTerms(put_trigger_pct=None, **base), None,
            S0=10.0, valuation_date=date(2026, 1, 1))
        assert "put_trigger_ratio" in no_put, "键必须在 —— 不传就落到 pricer 默认 0.7"
        assert no_put["put_trigger_ratio"] is None

        with_put, _ = build_pricer_kwargs(
            "128001.SZ", BondTerms(put_trigger_pct=70.0, **base), None,
            S0=10.0, valuation_date=date(2026, 1, 1))
        assert with_put["put_trigger_ratio"] == pytest.approx(0.70)

    def test_constructor_kwargs_round_trip_keeps_the_absence(self):
        """往返克隆不能把"没有回售"变回"默认 0.7"。"""
        p = UniversalCBPricer(**self._kwargs(put_trigger_ratio=None))
        clone = UniversalCBPricer(**p._constructor_kwargs())
        assert clone.put_trigger_ratio is None
        args = dict(sigma=0.20, r=0.022, base_spread=0.03, distress_k=0.05,
                    p_down=0.0, M=300, N=600)
        assert clone.price(**args) == pytest.approx(p.price(**args))


class TestPutbackBoundaryIsMonotone:
    """回售期内处处给底 —— 价值曲面必须单调, Δ 不许为负。"""

    @staticmethod
    def _pricer(**over):
        base = dict(
            S0=10.0, K=10.0, current_date=date(2026, 9, 2),
            maturity_date=date(2029, 9, 2), issue_date=date(2023, 9, 2),
            coupon_rates=(0.003, 0.005, 0.01, 0.015, 0.018, 0.02),
            redemption_price=108.0, call_notice_days=30,
        )
        base.update(over)
        return UniversalCBPricer(**base)

    @staticmethod
    def _min_delta(pricer, **args):
        grid_args = dict(sigma=0.17, r=0.0148, q=0.0, base_spread=0.03,
                         p_down=0.0, distress_k=0.05, M=500, N=2000)
        grid_args.update(args)
        S_grid, V = pricer._price_grid(
            grid_args["sigma"], grid_args["r"], grid_args["q"],
            grid_args["base_spread"], grid_args["p_down"], grid_args["distress_k"],
            grid_args["M"], grid_args["N"])
        return float(np.gradient(V, float(S_grid[1] - S_grid[0])).min())

    def test_no_negative_delta_anywhere_on_the_surface(self):
        """回售底是**常数**, 只加在低价侧会让曲面在触发线上出现台阶。

        回溯把台阶抹成一段非单调凹陷, ``dV/dS < 0`` —— 一只可转债不可能出现负 Δ。
        这不是离散伪影: M 从 300 加密到 4800 (h 缩小 16 倍), 凹陷稳定在 2.08~2.14 元
        (常银转债), 负区 S/K 带稳定在 0.629~0.783。全池 7 只有负区。

        逐条消融确认 100% 归因于回售: 关掉回售 7/7 负区消失, 关掉强赎 7/7 逐位不变,
        关掉下修 5/7 更糟。
        """
        # 低 σ + 已进入回售期 —— 旧实现下这一档负 Δ 最深
        assert self._min_delta(self._pricer()) >= -1e-9

        # 扫一片参数, 不靠单个 fixture 碰运气
        worst = 0.0
        for sigma in (0.12, 0.17, 0.25, 0.40):
            for ratio in (0.6, 0.7, 0.8):
                for notice in (0, 30):
                    d = self._min_delta(
                        self._pricer(put_trigger_ratio=ratio, call_notice_days=notice),
                        sigma=sigma)
                    worst = min(worst, d)
        assert worst >= -1e-9, f"仍有负 Δ, 最深 {worst:.6f}"

    def test_the_floor_still_does_its_job(self):
        """修单调性不能把回售底本身改没了 —— 它是个大件。

        实测完全关掉回售底, 全池中位少 0.07 元、最大少 26.75 元。
        """
        args = dict(sigma=0.17, r=0.0148, base_spread=0.03, distress_k=0.05,
                    p_down=0.0, M=300, N=600)
        deep = self._pricer(S0=6.0)                       # S/K = 0.6, 深在触发线下
        with_floor = deep.price(**args)
        without = self._pricer(S0=6.0, put_trigger_ratio=None).price(**args)
        assert with_floor > without + 1.0, (
            f"回售底没起作用: {with_floor:.4f} vs {without:.4f}")

    def test_the_floor_is_bounded_by_the_announced_window_branch(self):
        """通用分支与"已公告回售窗口"分支现在是同一个形状 (整格给底)。

        差别只在用哪个价: 公告窗口用 ``putback_price``, 通用分支用 面值+应计。
        这条钉住两者不再分叉 —— 分叉正是不连续的来源。
        """
        import inspect

        src = inspect.getsource(UniversalCBPricer._price_grid)
        # 通用分支不许再按 S 掩码
        assert "mask_put" not in src, "通用回售分支仍在按当前 S 掩码"
        assert src.count("V = np.maximum(V, put_price)") == 1


def test_theta_across_a_coupon_is_the_ex_coupon_drop():
    """票息落在截片窗口内时 Θ 是**除息下跌**, 不是时间价值衰减 —— 这是脏价的真实行为。

    理论价含应计, 债券在除息日就是会掉一个票息。这不是 2026-08-31 那次截片改写引入的:
    旧的"重解明天"写法给同一个结果 (票息 1.0 元时 旧法 −0.996 / 新法 −0.907, 而无票息
    的常态是 +0.005)。

    钉住它是为了**防止有人把它"修"掉** —— 剔掉票息会让 Θ 变成净价 Θ, 与 ``price`` 的
    脏价口径分叉, 那是比现状更糟的不一致。

    **2026-09-03 更正**: 上一版这里写着"归一化按时间比例摊薄了跳变, 这一档的 Θ 只说得清
    方向、说不清速率"。**方向恰恰是说不清的那一半** —— 票息落不落在截片窗口内由
    ``t_slice`` 决定, 而它随 N 变 (N=1000 时 1.46 天, N=2000 时 0.73 天), 于是"明天付不
    付息"这件事由网格步长而不是日历决定。实测同一只债在两套**生产**口径下六个久期里
    **3 个符号相反**, 量级差 ~250 倍。现在票息按它真正支付的那一天计入 (窗口内的加回来、
    次日的减掉), 脏价口径不变而 Θ 不再随 N 漂: 4y 那档 N ∈ {500…16000} 稳定在 −0.9949。
    """
    issue = date(2023, 9, 3)

    def _pricer(current):
        return UniversalCBPricer(
            S0=100.0, K=100.0, current_date=current,
            maturity_date=date(2029, 9, 3), issue_date=issue,
            coupon_rates=(0.003, 0.005, 0.01, 0.015, 0.018, 0.02),
            redemption_price=108.0, call_notice_days=30)

    args = dict(sigma=0.25, r=0.022, base_spread=0.03, distress_k=0.05,
                p_down=0.0, M=300, N=2000, return_greeks=True)

    on_coupon = _pricer(date(2026, 9, 2))          # 次日 09-03 付息 1.0 元
    coupons = dict(on_coupon._coupon_payment_events)
    nearest = min(coupons, key=abs)
    assert nearest * 365 < 2.0, "前提不成立: 票息不在截片窗口内"
    amount = coupons[nearest]
    assert amount == pytest.approx(1.0)

    theta_ex = on_coupon.price(**args)["theta"]
    assert -amount * 1.2 < theta_ex < -amount * 0.7, (
        f"Θ={theta_ex:.4f} 不像一个 {amount} 元的除息下跌")

    # 摊薄没了: 现在报的就是那一整笔, 而不是按 t_slice 比例缩过的
    assert theta_ex == pytest.approx(-amount, abs=0.02), (
        f"Θ={theta_ex:.4f} 不是那一整笔除息 (−{amount})")

    quiet = _pricer(date(2026, 6, 2)).price(**args)["theta"]
    assert 0.0 < quiet < 0.05, f"无票息的常态 Θ 应是小的正数, 实测 {quiet:.6f}"


def test_theta_does_not_flip_sign_between_the_two_production_calibers():
    """Θ 在批量 (M=300/N=1000) 与单只 (M=500/N=2000) 之间不许改号.

    票息落不落在截片窗口内曾由 ``t_slice`` 决定, 而它随 N 变 —— 于是"明天付不付息"
    由网格步长而不是日历决定。实测改前六个久期里 **3 个符号相反**, 量级差 ~250 倍
    (批量报 −0.68 / 单只报 +0.005), 而当时的注释把这写成"方向仍然可读"。

    两套口径都是**生产**在跑的: 批量定价用 M=300/N=1000, 单只页默认 M=500/N=2000。
    """
    def theta(years, M, N):
        p = UniversalCBPricer(
            S0=100.0, K=100.0, current_date=date(2026, 9, 2),
            maturity_date=date(2026 + years, 9, 3), issue_date=date(2023, 9, 3),
            conversion_start_date=date(2024, 3, 3),
            coupon_rates=tuple([0.01] * max(1, years + 1)), redemption_price=107.0)
        return p.price(sigma=0.25, r=0.022, base_spread=0.03, distress_k=0.05,
                       p_down=0.0, M=M, N=N, return_greeks=True)["theta"]

    for years in range(1, 7):
        batch, single = theta(years, 300, 1000), theta(years, 500, 2000)
        assert (batch > 0) == (single > 0), (
            f"剩余 {years}y: 批量 Θ={batch:.6f} 单只 Θ={single:.6f} 符号相反")
        assert batch == pytest.approx(single, abs=0.01), (
            f"剩余 {years}y: 两套口径 Θ 差得太远 ({batch:.6f} vs {single:.6f})")

    # N 扫描: 除息那一档也要稳 —— 改前它在 −0.45 / −0.91 / +0.005 之间跳
    swept = [theta(4, 500, N) for N in (500, 1000, 2000, 4000, 8000)]
    assert max(swept) - min(swept) < 0.002, f"Θ 随 N 漂: {swept}"


class TestGridResolutionAtS0:
    """S0 附近的分辨率与希腊值网格一致性。"""

    #: σ 取 0.40 而不是更高: 再高 ``_S_MAX_CAP`` 就绑定了, 那时 σ 变化**不会**改变
    #: S_max, 于是"两次解跑在不同网格上"这件事根本测不到 (实测 σ=0.95 时 σ 与 σ+0.01
    #: 的 S_max 都是 1000.0)。0.40 下 exp(3σ√T) ≈ 8.9 < 50, 上限不绑定。
    SIGMA = 0.40

    @staticmethod
    def _high_sigma_otm():
        """S0 远低于 K —— S_max 由 K 与 σ√T 定, 与 S0 无关, 于是分辨率全花在高价区。"""
        return dict(
            S0=4.0, K=20.0, current_date=date(2026, 9, 2),
            maturity_date=date(2029, 12, 25), issue_date=date(2023, 12, 25),
            coupon_rates=(0.003, 0.005, 0.01, 0.015, 0.018, 0.02),
            redemption_price=108.0, call_notice_days=30,
        )

    def test_grid_is_refined_when_s0_would_be_starved(self):
        """S0 以下的格点数有下限, 不够就自动加密。

        网格在 [0, S_max] 上均匀, 而 ``S_max = min(exp(3σ√T), 50)·K`` **只由 K 与 σ√T 定,
        与 S0 无关**。实测生产 M=500 下主池 311 只里 **50 只**在 S0 以下不足 20 个格点,
        最少的 118066.SH 只有 **5.6 个** —— 价格误差最大 **+2.78 元 (+1.90%)**、vega
        **+33.6%**, 而 N 早已收敛 (N 2000→8000 中位只动 0.0013 元), 所以是纯空间误差。

        **修法是加密不是缩小 S_max**: 后者会截断定义域 —— 实测把 ``_S_MAX_CAP`` 从 50
        降到 6 时最大误差反而从 2.72 涨到 **5.11 元**。加密是单调改进, 没有新失效模式。
        """
        import convertible_bond.pricer as pricer_mod

        p = UniversalCBPricer(**self._high_sigma_otm())
        args = dict(sigma=self.SIGMA, r=0.0148, base_spread=0.03,
                    distress_k=0.05, p_down=0.0)

        S_grid, _ = p._price_grid(args["sigma"], args["r"], 0.0, args["base_spread"],
                                  args["p_down"], args["distress_k"], 500, 2000)
        nodes_below = p.S0 / float(S_grid[1] - S_grid[0])
        assert nodes_below >= 59, (          # 字面量: 期望值不许从被测常数算出来
            f"S0 以下只有 {nodes_below:.1f} 个格点")

        # 前提: 不加密的话确实是饿着的 —— 否则这条用例什么也没测
        old = pricer_mod._MIN_NODES_BELOW_S0
        pricer_mod._MIN_NODES_BELOW_S0 = 0
        try:
            starved, _ = p._price_grid(args["sigma"], args["r"], 0.0, args["base_spread"],
                                       args["p_down"], args["distress_k"], 500, 2000)
            starved_nodes = p.S0 / float(starved[1] - starved[0])
            coarse = p.price(M=500, N=2000, **args)
        finally:
            pricer_mod._MIN_NODES_BELOW_S0 = old
        assert starved_nodes < 20, f"fixture 没有饿着 ({starved_nodes:.1f} 个格点)"

        # 加密之后要更接近收敛值
        refined = p.price(M=500, N=2000, **args)
        converged = p.price(M=8000, N=2000, **args)
        assert abs(refined - converged) < abs(coarse - converged), (
            f"加密没有更准: 粗 {coarse:.4f} 细 {refined:.4f} 收敛 {converged:.4f}")

        # 上限要生效, 免得 S0/S_max 极小时把 M 撑到天文数字
        tiny = UniversalCBPricer(**dict(self._high_sigma_otm(), S0=0.02))
        g, _ = tiny._price_grid(self.SIGMA, 0.0148, 0.0, 0.03, 0.0, 0.05, 500, 200)
        assert len(g) - 1 <= 4000            # 同上, 不写 pricer_mod._MAX_ADAPTIVE_M
        assert pricer_mod._MAX_ADAPTIVE_M == 4000, "改这个上限是模型行为变更"

    def test_vega_uses_the_same_s_grid_as_the_base_solve(self):
        """vega 的两次解必须跑在同一张 S 网格上。

        ``S_max = exp(3σ√T)·K`` 随 σ 变, 所以扰动解的 h 会变、强赎触发掩码
        ``S_grid >= K·call_trigger_ratio`` 会吸附到不同格点。于是 vega 里混进 O(h) 的
        网格抖动 —— 而 vega·1pp 相对价格只有 1e-3 量级, 两个各带网格误差的数相减不抵消
        反而放大。实测生产网格上 vega 偏 **+33.6%** (万凯转债 0.6190 vs 收敛值 0.4633)。
        """
        p = UniversalCBPricer(**self._high_sigma_otm())
        args = dict(sigma=self.SIGMA, r=0.0148, base_spread=0.03,
                    distress_k=0.05, p_down=0.0)

        base_grid, _ = p._price_grid(args["sigma"], args["r"], 0.0, args["base_spread"],
                                     args["p_down"], args["distress_k"], 500, 2000)
        bumped_free, _ = p._price_grid(args["sigma"] + 0.01, args["r"], 0.0,
                                       args["base_spread"], args["p_down"],
                                       args["distress_k"], 500, 2000)
        assert float(bumped_free[-1]) != pytest.approx(float(base_grid[-1])), (
            "前提不成立: σ 变了 S_max 却没变, 这条用例测不到东西")

        bumped_locked, _ = p._price_grid(args["sigma"] + 0.01, args["r"], 0.0,
                                         args["base_spread"], args["p_down"],
                                         args["distress_k"], 500, 2000,
                                         s_max_override=float(base_grid[-1]))
        assert float(bumped_locked[-1]) == pytest.approx(float(base_grid[-1]))
        assert len(bumped_locked) == len(base_grid)

        # vega 要收敛: 粗网格与细网格的差距必须小
        coarse = p.price(M=500, N=2000, return_greeks=True, **args)["vega"]
        fine = p.price(M=4000, N=2000, return_greeks=True, **args)["vega"]
        assert abs(coarse / fine - 1.0) < 0.10, (
            f"vega 随网格漂: 粗 {coarse:.4f} vs 细 {fine:.4f}")

    def test_price_actually_locks_the_vega_grid(self):
        """``price()`` 必须**真的**把基准网格传下去 —— 光有 ``s_max_override`` 形参不算。

        自适应加密之后网格抖动小了很多, 但没有消失: 用 127053.SZ 的真实参数实测,
        锁网格 vega = 0.489, 不锁 = 0.338 —— **差 30.8%**。所以这条比对
        ``price()`` 报出来的 vega 与手工锁网格算出来的, 两者必须一致。
        """
        real = UniversalCBPricer(
            S0=24.52, K=17.23, current_date=date(2026, 9, 3),
            maturity_date=date(2028, 1, 24), issue_date=date(2022, 1, 24),
            coupon_rates=(0.003, 0.006, 0.01, 0.016, 0.025, 0.03),
            redemption_price=118.0, call_notice_days=30,
            put_trigger_ratio=0.70, down_reset_floor=24.52,
        )
        args = dict(sigma=0.6328, r=0.0148, base_spread=0.035,
                    distress_k=0.05, p_down=0.25)
        reported = real.price(M=500, N=2000, return_greeks=True, **args)["vega"]

        def _solve(sigma, override=None):
            grid, values = real._price_grid(
                sigma, args["r"], 0.0, args["base_spread"], args["p_down"],
                args["distress_k"], 500, 2000,
                **({"s_max_override": override} if override is not None else {}))
            return grid, float(np.interp(real.S0, grid, values))

        base_grid, base = _solve(args["sigma"])
        _, locked = _solve(args["sigma"] + 0.01, override=float(base_grid[-1]))
        _, unlocked = _solve(args["sigma"] + 0.01)

        # 前提: 这只债上锁不锁确实差很多, 否则这条用例测不到东西
        assert abs((locked - base) - (unlocked - base)) > 0.05, (
            "这只 fixture 上锁网格没有影响, 换一只")
        assert reported == pytest.approx(locked - base, abs=1e-12), (
            f"price() 报的 vega {reported:.5f} 不等于锁网格的 {locked - base:.5f}")


def test_frozen_down_reset_floor_puts_a_kink_right_at_s0():
    """**已知模型边界**: 冻结的下修价下限在 ≈1.02·S0 处留下一个折点, Γ(S0) 因此可能为负。

    ``_estimate_down_reset_floor`` 返回 ``max(20日均价, 前一交易日收盘)`` —— 后者**就是
    S0**, 所以 floor ≥ S0 恒成立, 实测主池 **184/311 只 floor 恰好等于 S0**。
    而 ``_down_reset_value`` 的有 floor 分支是 ``target_k = max(S/premium, floor)``:
    它在 ``S = premium·floor ≈ 1.02·S0`` 处从常数切成线性 —— 折点正好落在读取
    价格/Δ/Γ 的那一点上。实测 **39/311 只**的 Γ(S0) < 0 (最深 127062.SZ −0.75)。

    **根因是 floor 被冻结在今天**: 真实监管下限是"下修**当时**的 20 日均价", 它应当
    随 S 走; 随 S 走时 ``target_k`` 对 S 线性, 折点消失。

    **为什么不改**: 实测把 floor 改成按 S 比例走, 39 只负 Γ 全清零, 但价格中位
    **+0.89 元**、均值 +1.91、最大 **+11.13 元**, 192/311 只动超过 0.5 元 —— 那是与
    下修价值本身同量级的**模型口径变更**, 不是补丁 (作为对照: 2026-09-02 回售那次
    口径变更全池最大只动 0.45 元)。而且"随 S 走"用哪个比例本身是个建模问题: 下跌行情里
    20 日均价高于现价, ratio 应当 >1, 而今天 184 只的 ratio 恰好是 1.0 只因为它们的
    最新收盘高于 20 日均价。

    这条用例钉住**现状**与它的量级, 免得有人顺手"修"掉而没意识到那是口径变更。
    """
    import json
    from pathlib import Path

    from convertible_bond.cache import TermsBundle, project_bundle_path
    from convertible_bond.market_time import market_today

    cache = Path("data/batch_pricing_cache.json")
    if not cache.exists():
        pytest.skip("需要 batch_pricing_cache.json")
    rows = {r["bond_code"]: r for r in json.loads(cache.read_text())["results"]}
    bundle = TermsBundle(project_bundle_path())
    today = market_today()

    equal_floor = sum(
        1 for r in rows.values()
        if r.get("down_reset_floor") is not None and r.get("S0")
        and abs(float(r["down_reset_floor"]) - float(r["S0"])) < 1e-9)
    assert equal_floor > 100, (
        f"只有 {equal_floor} 只 floor == S0 —— 取数口径变了, 这条记录要重新量")

    # 折点位置: target_k 在 S = premium·floor 处从常数切成线性
    code = "110100.SH"
    row, terms = rows.get(code), bundle.get(code)
    if not (row and terms and terms.maturity_date and terms.maturity_date > today):
        pytest.skip(f"{code} 不在当前池里")
    p = UniversalCBPricer(
        S0=float(row["S0"]), K=float(terms.conversion_price), current_date=today,
        maturity_date=terms.maturity_date,
        issue_date=terms.issue_date or date(2023, 1, 1),
        coupon_rates=terms.coupon_rates or (0.003, 0.005, 0.01, 0.015, 0.018, 0.02),
        redemption_price=terms.redemption_price or 108.0, call_notice_days=30,
        down_reset_floor=float(row["down_reset_floor"]),
    )
    # premium 写**字面量**: 184/311 只 floor == S0, 于是 kink/S0 恒等于
    # `down_reset_premium` —— 拿它当期望再检查"落在含它的区间里", 等于把常数递给
    # 自己。实测那样写时把 premium 从 1.02 改成 1.0449 这条与整套 1118 条全绿。
    assert p.down_reset_premium == pytest.approx(1.02), "改这个溢价是模型口径变更"
    kink = 1.02 * float(row["down_reset_floor"])
    assert kink / p.S0 == pytest.approx(1.02, abs=0.03), (
        f"折点在 {kink / p.S0:.3f}·S0 —— 不再落在读数点上, 这条记录要重新量")


def test_gamma_guards_use_a_realistic_down_reset_floor():
    """Γ 的两条守护必须带 ``down_reset_floor`` —— 否则看不见生产上真正的那个形状。

    它们构造 pricer 时既不给 floor 也不给 trigger_ratio, 又用 ``price()`` 的默认
    ``p_down=0.1``, 于是走的是**无 floor** 分支 —— 而生产上 311/311 只都有 floor,
    184 只的 floor 恰好等于 S0。守护跑在一个生产中不存在的形状上。
    """
    import ast
    import inspect

    from tests import test_pricer as self_mod

    for name in ("test_gamma_positive_under_coarse_grid",
                 "test_gamma_stable_across_grid_refinement"):
        fn = None
        for attr in vars(self_mod).values():
            if inspect.isclass(attr) and hasattr(attr, name):
                fn = getattr(attr, name)
                break
        assert fn is not None, f"找不到 {name}"
        # **看真实的关键字参数, 不扫源码文本**。第一版扫文本, 而那两条用例的注释里
        # 恰好也写着 "down_reset_floor" —— 把 kwarg 删掉、注释留着, 守护照样绿。
        # 同一个陷阱本项目已经踩过三次 (关注池"不说持仓"、策略页共用措辞、这里)。
        tree = ast.parse(inspect.getsource(fn).lstrip())
        passed = {
            kw.arg
            for node in ast.walk(tree) if isinstance(node, ast.Call)
            for kw in node.keywords if kw.arg
        }
        assert "down_reset_floor" in passed, (
            f"{name} 没**传** down_reset_floor —— 它测的是生产中不存在的形状")
        assert "p_down" in passed, (
            f"{name} 用的是 price() 的默认 p_down, 生产上 308/311 只是 0.25")


class TestImpliedVolBracket:
    """反解无解时必须说清是**哪一侧**。"""

    @staticmethod
    def _pricer():
        return UniversalCBPricer(
            S0=10.0, K=10.0, current_date=date(2026, 9, 3),
            maturity_date=date(2029, 9, 3), issue_date=date(2023, 9, 3),
            coupon_rates=(0.003, 0.005, 0.01, 0.015, 0.018, 0.02),
            redemption_price=108.0, call_notice_days=30)

    ARGS = dict(r=0.0148, base_spread=0.03, distress_k=0.05, p_down=0.0, M=300, N=1000)

    def test_out_of_band_failures_report_their_direction(self):
        """两种无解方向**相反**, 不能都报"区间内无解"。

        · 市价**高于** σ=200% 的模型价 → 再高的波动率也够不着, 模型上限被强赎 cap
          ``max(call_price, parity·(1+σ√t_grace))`` 封住了。实测主池 309 只里 **76 只**。
        · 市价**低于** σ=5% 的模型价 → 市场比模型的债底还悲观 (信用/退市风险)。**12 只**。

        合计 88/309 (28%), 而此前界面上是同一句话 —— 读者据此要做的事完全不同。
        这与已删除的 ``solve_implied_p_down`` 是同一形状 (带太窄 + 静默弃权), 区别是
        这次**把可达带交出来**而不是只回一个 NaN。
        """
        import math

        p = self._pricer()
        lo = p.price(sigma=0.05, **self.ARGS)
        hi = p.price(sigma=2.0, **self.ARGS)
        assert hi > lo + 10, "fixture 的可达带太窄, 测不出方向"

        for target, expected in ((hi + 20, "above_ceiling"), (lo - 20, "below_floor")):
            bracket: dict = {}
            iv = p.solve_implied_vol(target_price=target, bracket_out=bracket,
                                     **self.ARGS)
            assert math.isnan(iv)
            assert bracket["reason"] == expected, (
                f"市价 {target:.2f} 的无解方向报成了 {bracket['reason']}")
            assert bracket["price_lo"] == pytest.approx(lo, rel=1e-9)
            assert bracket["price_hi"] == pytest.approx(hi, rel=1e-9)

        # 带内照常解出来, 且 reason 清空
        bracket = {}
        iv = p.solve_implied_vol(target_price=(lo + hi) / 2, bracket_out=bracket,
                                 **self.ARGS)
        assert not math.isnan(iv) and 0.05 < iv < 2.0
        assert bracket["reason"] is None

        # bracket_out 是可选的 —— 不传也不能崩
        assert math.isnan(p.solve_implied_vol(target_price=hi + 20, **self.ARGS))

    def test_gui_message_names_the_direction(self):
        """定价页要把方向说出来, 不能停在"区间内无解"。"""
        import ast
        import inspect

        from convertible_bond.gui.controllers import pricing as mod

        src = inspect.getsource(mod)
        literals = [
            n.value for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
        ]
        joined = " ".join(literals)
        assert "强赎 cap 封住了模型上限" in joined
        assert "市场比模型的债底更悲观" in joined
        assert "bracket_out" in src


def test_vega_is_stable_across_the_time_grid():
    """Θ 有三条守护 (PDE 恒等式 / N 稳定性 / 求解次数), vega 只有一条"为正"。

    而 vega 恰恰是这轮被查出偏 **+33.6%** 的那个量 —— 一个只断言符号的守护看不见
    三分之一的误差。这条补上 N 与 M 两个方向的稳定性。
    """
    p = UniversalCBPricer(
        S0=10.0, K=10.0, current_date=date(2026, 9, 3),
        maturity_date=date(2029, 9, 3), issue_date=date(2023, 9, 3),
        coupon_rates=(0.003, 0.005, 0.01, 0.015, 0.018, 0.02),
        redemption_price=108.0, call_notice_days=30)
    args = dict(sigma=0.35, r=0.0148, base_spread=0.03, distress_k=0.05, p_down=0.0)

    by_n = [p.price(M=500, N=N, return_greeks=True, **args)["vega"]
            for N in (1000, 2000, 4000)]
    assert all(v > 0 for v in by_n), f"vega 出现非正值: {by_n}"
    assert (max(by_n) - min(by_n)) / max(by_n) < 0.05, f"vega 随 N 漂: {by_n}"

    by_m = [p.price(M=M, N=2000, return_greeks=True, **args)["vega"]
            for M in (500, 1000, 2000)]
    assert (max(by_m) - min(by_m)) / max(by_m) < 0.05, f"vega 随 M 漂: {by_m}"


class TestBacktestSharesTheProductionCaliber:
    """单债回测页与批量页必须是同一套口径 —— 曾经是两份手抄的实现.

    `backtest._build_backtest_pricer_kwargs` 此前逐行抄自 `price_from_provider`,
    抄漏了四处, 于是同一只债同一天两个页面给出不同的理论价, 而页面上没有任何
    线索说得出为什么。这四条各守一处。
    """

    @staticmethod
    def _provider(terms, start, end):
        """用现成的 FakeProvider 铺一段工作日行情."""
        bond_close, stock_close = [], []
        d = start
        while d <= end:
            if d.weekday() < 5:
                bond_close.append((d, 110.0))
                stock_close.append((d, 9.0 + (d.toordinal() % 5) * 0.1))
            d += timedelta(days=1)
        return FakeProvider("123001.SZ", terms.underlying_code, terms,
                            bond_close, stock_close)

    @staticmethod
    def _terms(**over):
        from convertible_bond.data_providers import BondTerms
        base = dict(
            underlying_code="300953.SZ", conversion_price=10.0,
            issue_date=date(2023, 1, 1), maturity_date=date(2029, 1, 1),
            face_value=100.0, put_trigger_pct=70.0,
        )
        base.update(over)
        return BondTerms(**base)

    def test_backtest_and_pricing_api_build_identical_pricer_kwargs(self):
        """两页共用同一个构建器 —— 这是"不再分叉"的唯一保证。"""
        from convertible_bond.backtest import _build_backtest_pricer_kwargs
        from convertible_bond.pricing_api import build_pricer_kwargs

        val = date(2026, 1, 1)
        terms = self._terms(call_redemption_date=date(2026, 3, 1),
                            call_redemption_price=103.5)
        bt_kwargs, _, _ = _build_backtest_pricer_kwargs("128000.SZ", terms, None, val)
        api_kwargs, _ = build_pricer_kwargs(
            "128000.SZ", terms, None, S0=9.0, valuation_date=val)
        # 回测页逐点自己填 S0 / current_date, 其余必须一字不差
        api_kwargs.pop("S0"); api_kwargs.pop("current_date")
        assert bt_kwargs == api_kwargs

    def test_announced_forced_redemption_truncates_maturity_and_closes_the_call(self):
        """已公告强赎: 到期日截断到赎回日, 赎回价换成公告价, 触发式 cap 关掉。

        回测页此前完全没有这个分支 —— 一只已公告强赎的债照常按剩余全部期权寿命
        和永远收不到的票息定价。实测全库 541 只带 `call_redemption_date`。
        """
        from convertible_bond.backtest import _build_backtest_pricer_kwargs

        kwargs, _, maturity = _build_backtest_pricer_kwargs(
            "128000.SZ",
            self._terms(call_redemption_date=date(2026, 3, 1),
                        call_redemption_price=103.5),
            None, date(2026, 1, 1))
        assert kwargs["maturity_date"] == date(2026, 3, 1)
        assert maturity == date(2026, 3, 1)
        assert kwargs["redemption_price"] == pytest.approx(103.5)
        assert kwargs["call_no_redemption_until"] == date(2026, 3, 1)

    def test_no_put_clause_is_switched_off_not_left_to_the_default(self):
        """没有回售条款时必须显式 None —— 缺这一步等于凭空造一个回售权 (全库 69 只)。"""
        from convertible_bond.backtest import _build_backtest_pricer_kwargs

        kwargs, _, _ = _build_backtest_pricer_kwargs(
            "128000.SZ", self._terms(put_trigger_pct=None), None, date(2026, 1, 1))
        assert "put_trigger_ratio" in kwargs
        assert kwargs["put_trigger_ratio"] is None

    def test_rating_spread_floor_applies_to_the_backtest_too(self, monkeypatch):
        """评级利差下限: 回测页此前不套, 实测全库 532/1059 只的下限高于默认 0.03。"""
        import convertible_bond.backtest as bt

        seen = []

        class SpyPricer:
            def __init__(self, **kwargs):
                self.ratio = 100.0 / float(kwargs["K"])

            def price(self, **kwargs):
                seen.append(kwargs["base_spread"])
                return 100.0

            def bond_floor_value(self, *_a, **_k):
                return 95.0

            def spread_at_s0(self, base_spread, distress_k):
                return float(base_spread)

        monkeypatch.setattr(bt, "UniversalCBPricer", SpyPricer)
        provider = self._provider(self._terms(credit_rating="A-"),
                                  date(2025, 6, 2), date(2025, 9, 30))
        bt.backtest_theoretical_price(
            "123001.SZ", start_date=date(2025, 6, 2), end_date=date(2025, 9, 30),
            freq="M", provider=provider, base_spread=0.03,
            point_in_time=False, M=60, N=120)
        assert seen, "一个采样点都没定价, 这条守护测不到东西"
        # A- 的下限 0.08 必须压过传入的 0.03
        assert all(s == pytest.approx(0.08) for s in seen), seen

    def test_down_reset_floor_reaches_the_pricer_in_the_backtest_too(self, monkeypatch):
        """下修价下限也要逐点估, 与批量页同口径.

        这是 `test_rating_spread_floor_applies_to_the_backtest_too` 的姊妹条 ——
        两个口径步骤原本只守住了一个: 实测删掉 `backtest.py` 里估下限的那四行,
        整套 1111 条照常全绿, 而同一只债同一天回测页与批量页的理论价从 0.0
        重新分叉到 +0.181 元。
        """
        import convertible_bond.backtest as bt

        seen = []

        class SpyPricer:
            def __init__(self, **kwargs):
                seen.append(kwargs.get("down_reset_floor"))
                self.ratio = 100.0 / float(kwargs["K"])

            def price(self, **_kw):
                return 100.0

            def bond_floor_value(self, *_a, **_k):
                return 95.0

            def spread_at_s0(self, base_spread, distress_k):
                return float(base_spread)

        monkeypatch.setattr(bt, "UniversalCBPricer", SpyPricer)
        provider = self._provider(self._terms(),
                                  date(2025, 6, 2), date(2025, 9, 30))
        bt.backtest_theoretical_price(
            "123001.SZ", start_date=date(2025, 6, 2), end_date=date(2025, 9, 30),
            freq="M", provider=provider, point_in_time=False, M=60, N=120)
        assert seen, "一个采样点都没定价, 这条守护测不到东西"
        assert all(f is not None for f in seen), (
            f"有采样点没拿到下修价下限: {seen}")
        # 下限来自正股近 20 个交易日均价 vs 前收, 必须是个真实数而不是常数占位
        assert len(set(seen)) > 1 or seen[0] > 0

    def test_backtest_counts_the_days_it_could_not_estimate_a_floor(self, monkeypatch):
        """估不出下限的天数要记账 —— 批量页对这一档是出声的, 回测页不能哑着.

        那些天 pricer 走无下限分支, 下修价值偏高; 批量页把同一件事写进
        `risk_warnings`, 回测页此前完全不说。
        """
        import convertible_bond.backtest as bt

        monkeypatch.setattr(bt, "_estimate_down_reset_floor",
                            lambda *_a, **_k: None)
        provider = self._provider(self._terms(),
                                  date(2025, 6, 2), date(2025, 9, 30))
        result = bt.backtest_theoretical_price(
            "123001.SZ", start_date=date(2025, 6, 2), end_date=date(2025, 9, 30),
            freq="M", provider=provider, point_in_time=False, M=60, N=120)
        assert result["no_down_reset_floor_days"] == len(result["dates"]) > 0

        # 正常估得出来时计数必须是 0 —— 否则这个提示会常年挂着
        monkeypatch.undo()
        ok = bt.backtest_theoretical_price(
            "123001.SZ", start_date=date(2025, 6, 2), end_date=date(2025, 9, 30),
            freq="M", provider=provider, point_in_time=False, M=60, N=120)
        assert ok["no_down_reset_floor_days"] == 0

    def test_bond_floor_uses_the_models_own_spread_on_both_pages(self, monkeypatch):
        """债底折现的利差两页必须一致 —— 定价页修过一次, 回测页留在旧公式上.

        利差是 S 的函数 (`base_spread + distress_k·max(0, 1−S/K)`), 而债底是"不转股时
        这张债值多少" —— 当然要按这只债此刻的信用状况折现。实测全池 311 只里
        **151 只**两页差 >1 元, 最大 10.48 元 (123250.SZ, S0/K=0.434)。
        """
        import convertible_bond.backtest as bt

        seen = []

        class SpyPricer:
            def __init__(self, **kwargs):
                self.S0 = float(kwargs["S0"])
                self.K = float(kwargs["K"])
                self.ratio = 100.0 / self.K

            def price(self, **_kw):
                return 100.0

            def spread_at_s0(self, base_spread, distress_k):
                # 真类的公式, 桩照抄一遍是为了让"传进来的是不是它"可观测
                return float(base_spread) + float(distress_k) * max(
                    0.0, 1.0 - self.S0 / self.K)

            def bond_floor_value(self, _date, discount_rate):
                seen.append(float(discount_rate))
                return 95.0

        # S0/K ≈ 0.5 → distress 项 ≈ 0.05·0.5 = 0.025, 与裸利差差得开
        terms = self._terms(conversion_price=20.0)
        provider = self._provider(terms, date(2025, 6, 2), date(2025, 9, 30))
        monkeypatch.setattr(bt, "UniversalCBPricer", SpyPricer)
        bt.backtest_theoretical_price(
            "123001.SZ", start_date=date(2025, 6, 2), end_date=date(2025, 9, 30),
            freq="M", provider=provider, point_in_time=False,
            r=0.022, base_spread=0.03, distress_k=0.05, M=60, N=120)
        assert seen, "一个采样点都没算债底"
        assert all(d > 0.022 + 0.03 + 1e-9 for d in seen), (
            f"债底用的是裸利差 (r+base_spread), 没有 distress 扩张: {seen[:3]}")

    def test_backtest_wraps_the_provider_in_the_point_in_time_layer(self, monkeypatch):
        """默认必须叠历史条款投影层 —— 否则每个历史采样日都用今天的转股价。

        实测 12 个月度采样点上, 全库 322/1059 只 (30.4%) 至少有一个采样日 K 用错,
        采样点口径 21.4%, 最大偏离 115%。而 GUI 回测页传进来的
        `CachedBondDataProvider` 的 `get_bond_terms(code, val_date)` 根本不看 val_date。
        """
        import convertible_bond.backtest as bt

        built = []

        class SpyLayer:
            def __init__(self, inner, **kwargs):
                built.append(inner)
                self._inner = inner

            def __getattr__(self, name):
                return getattr(self._inner, name)

        monkeypatch.setattr(bt, "HistoricalBondDataProvider", SpyLayer)
        monkeypatch.setattr(bt, "TermsPatchStore", lambda *a, **k: None)
        monkeypatch.setattr(bt, "CBEventStore", lambda *a, **k: None)

        provider = self._provider(self._terms(),
                                  date(2025, 6, 2), date(2025, 9, 30))
        kw = dict(bond_code="123001.SZ", start_date=date(2025, 6, 2),
                  end_date=date(2025, 9, 30), freq="M", provider=provider,
                  M=60, N=120)
        bt.backtest_theoretical_price(**kw)
        assert built == [provider], "默认没有包历史条款投影层"

        built.clear()
        bt.backtest_theoretical_price(**kw, point_in_time=False)
        assert built == [], "point_in_time=False 时不该包"
