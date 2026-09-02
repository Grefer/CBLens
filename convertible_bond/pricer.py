"""
可转债 PDE 定价引擎 (核心模块).

不依赖任何数据源, 纯数值计算. 从 CB.py 拆分而来.
"""
import logging
import numpy as np
from scipy.linalg import solve_banded
from scipy.optimize import brentq
from datetime import date, timedelta
from typing import Literal, overload

from convertible_bond.dateutil import add_years as _add_years_impl

logger = logging.getLogger(__name__)

# ── 默认常量 ─────────────────────────────────────────────
DEFAULT_COUPON_RATES: tuple[float, ...] = (0.003, 0.004, 0.008, 0.015, 0.018, 0.02)
DEFAULT_FACE_VALUE: float = 100.0
DEFAULT_REDEMPTION_PRICE: float = 107.0
_DAYS_PER_YEAR: float = 365.0
_S_MAX_CAP: float = 50.0  # 网格上界 S_max/K 的上限, 防止极端 σ/T 下内存爆炸


# ── 票息期与应计利息 (模块级共享实现) ─────────────────────
# UniversalCBPricer 与 pricing_api 共用同一套口径 (期末按到期日封顶),
# 避免两处独立实现在票息规则演化时漂移。

def build_coupon_periods(
    face_value: float,
    coupon_rates: tuple[float, ...],
    issue_date: date,
    maturity_date: date,
) -> list[dict]:
    """按年滚动构造票息期, 期末以到期日封顶; 最后一期标记 is_final."""
    periods = []
    period_start = issue_date
    for rate in coupon_rates:
        period_end = min(_add_years_impl(period_start, 1), maturity_date)
        periods.append({
            "start": period_start,
            "end": period_end,
            "rate": rate,
            "coupon_amount": face_value * rate,
            "is_final": period_end == maturity_date,
        })
        period_start = period_end
        if period_end >= maturity_date:
            break
    return periods


def accrued_interest_amount(
    coupon_periods: list[dict],
    valuation_date: date,
    *,
    face_value: float,
    issue_date: date,
    maturity_date: date,
) -> float:
    """按 build_coupon_periods 的票息期计算应计利息; 估值日超过到期日按到期日封顶."""
    if valuation_date <= issue_date:
        return 0.0
    capped_date = min(valuation_date, maturity_date)
    for period in coupon_periods:
        if period["start"] <= capped_date <= period["end"]:
            accrual_days = (capped_date - period["start"]).days
            return face_value * period["rate"] * accrual_days / _DAYS_PER_YEAR
    return 0.0


class UniversalCBPricer:
    """
    通用可转债定价引擎。

    当前版本按题述真实条款进行了参数化：
    - 六年阶梯票息
    - 到期 107 元兑付（含最后一期利息）
    - 2021-02-06 起进入转股期
    - 最后两个计息年度允许回售
    - 支持按公告公式调整转股价
    """
    def __init__(self, S0: float, K: float, current_date: date, maturity_date: date,
                 face_value: float = 100.0, redemption_price: float = 107.0,
                 issue_date: date | None = None, conversion_start_date: date | None = None,
                 call_start_date: date | None = None,
                 coupon_rates: tuple[float, ...] | None = None, call_trigger_ratio: float = 1.3,
                 call_no_redemption_until: date | None = None,
                 put_trigger_ratio: float | None = 0.7,
                 put_active_years: int = 2,
                 putback_start_date: date | None = None,
                 putback_end_date: date | None = None,
                 putback_price: float | None = None,
                 down_reset_premium: float = 1.02,
                 down_reset_trigger_ratio: float = 1.0,
                 down_reset_block_until: date | None = None,
                 down_reset_floor: float | None = None,
                 call_notice_days: int = 30,
                 scheduled_reset_date: date | None = None,
                 scheduled_reset_prob: float = 0.0,
                 scheduled_reset_target_k: float | None = None):
        self._validate_inputs(S0, K, current_date, maturity_date, face_value)
        self.S0 = S0
        self.K = K
        self.face_value = face_value
        self.redemption_price = redemption_price
        self.ratio = face_value / K
        self.issue_date = issue_date or current_date
        self.conversion_start_date = conversion_start_date or current_date
        self.call_start_date = call_start_date or self.conversion_start_date
        self.call_trigger_ratio = call_trigger_ratio
        self.call_no_redemption_until = call_no_redemption_until
        self.put_trigger_ratio = put_trigger_ratio
        self.put_active_years = put_active_years
        self.putback_start_date = putback_start_date
        self.putback_end_date = putback_end_date
        self.putback_price = putback_price
        self.down_reset_premium = down_reset_premium
        self.down_reset_trigger_ratio = float(down_reset_trigger_ratio)
        if self.down_reset_trigger_ratio <= 0:
            raise ValueError("down_reset_trigger_ratio must be positive")
        self.down_reset_block_until = down_reset_block_until
        self.down_reset_floor = down_reset_floor
        if self.down_reset_floor is not None and self.down_reset_floor <= 0:
            raise ValueError("down_reset_floor must be positive when provided")
        self.call_notice_days = max(0, int(call_notice_days))
        # 董事会已提议下修时的"一次性近确定下修"节点 (regime ②):
        # scheduled_reset_date ≈ 提议日 + 表决滞后, scheduled_reset_prob ≈ 通过率。
        # scheduled_reset_target_k: 公告解析到的新转股价; None 时回落 premium/floor 估算。
        self.scheduled_reset_date = scheduled_reset_date
        self.scheduled_reset_prob = max(0.0, float(scheduled_reset_prob))
        # 方向兜底: 下修不会**抬高**转股价, target_k 严格大于现 K 一定是上游解析错了 ——
        # 丢掉它回落 premium/floor 估算, 而不是留着让节点静默变 no-op
        # (max(V, reset_value) 的后果)。上游 resolve_down_reset_intensity 有同一道闸,
        # 这里防直接构造 pricer 的调用方。
        #
        # 注意闸是 **>** 不是 >=: ``target_k == 现 K`` 是有意义的状态 —— 下修已经落地、
        # 条款刷新已经把 K 改成新值, 此时节点该成 no-op 来防双计 (见 _down_reset_value)。
        # 把它一起拦掉会让 pricer 改用 premium/floor 估算, 反而把已落地的下修**再算一遍**。
        self.scheduled_reset_target_k = (
            float(scheduled_reset_target_k)
            if scheduled_reset_target_k is not None
            and scheduled_reset_target_k > 0
            and scheduled_reset_target_k <= self.K
            else None
        )
        self.coupon_rates = tuple(coupon_rates or DEFAULT_COUPON_RATES)

        self.T = (maturity_date - current_date).days / _DAYS_PER_YEAR
        self.current_date = current_date
        self.maturity_date = maturity_date
        self.put_start_date = self._add_years(self.maturity_date, -self.put_active_years)
        self.coupon_periods = self._build_coupon_periods()

        # 预计算连续时间上的关键事件点, 避免 PDE 步内 round(t*365) 日期量化导致事件漏判.
        # 当 N 较大时 dt*365 < 0.5, 连续多个 PDE 步的 round(t*365) 映射到同一日历日,
        # 票息/强赎/回售/下修等事件可能被跨越的步骤跳过.
        self._coupon_payment_events: list[tuple[float, float]] = []
        for p in self.coupon_periods:
            if not p["is_final"]:
                t_pay = (p["end"] - current_date).days / _DAYS_PER_YEAR
                if 0 < t_pay <= self.T:
                    self._coupon_payment_events.append((t_pay, p["coupon_amount"]))
        self._conv_start_t = (self.conversion_start_date - current_date).days / _DAYS_PER_YEAR
        self._call_start_t = (self.call_start_date - current_date).days / _DAYS_PER_YEAR
        self._call_no_redemption_until_t: float | None = None
        if self.call_no_redemption_until is not None:
            self._call_no_redemption_until_t = (
                self.call_no_redemption_until - current_date
            ).days / _DAYS_PER_YEAR
        # ``put_trigger_ratio is None`` = **这只债没有回售条款**, 与"有条款但比例未知"
        # 是两回事。把 ``_put_start_t`` 置为 +inf, 通用回售分支就永远进不去。
        # 起因: ``pricing_api`` 只在 ``terms.put_trigger_pct`` 非空时才传这个参数, 于是
        # 银行/券商转债 (实测全库 69 只、主池 5 只: 上银/财通/兴业/重银/常银 —— 它们
        # 本来就没有回售条款) 落到 pricer 的默认 0.7, 被凭空造出一个回售权。
        # 今天影响极小 (那 5 只 S/K 都在 0.87 以上, 底够不着, 实测最大 +0.001 元),
        # 但那只是当前位置的巧合: 正股跌到 70% 以下就会多出一整个回售底。
        self._put_start_t = (
            float("inf") if self.put_trigger_ratio is None
            else (self.put_start_date - current_date).days / _DAYS_PER_YEAR)
        self._putback_start_t: float | None = None
        self._putback_end_t: float | None = None
        if self.putback_start_date is not None:
            self._putback_start_t = (self.putback_start_date - current_date).days / _DAYS_PER_YEAR
        if self.putback_end_date is not None:
            self._putback_end_t = (self.putback_end_date - current_date).days / _DAYS_PER_YEAR
        self._down_reset_block_until_t: float | None = None
        if self.down_reset_block_until is not None:
            self._down_reset_block_until_t = (self.down_reset_block_until - current_date).days / _DAYS_PER_YEAR
        # 一次性下修节点的连续时间坐标; t_eff > T (生效日晚于到期) 视为无关, 置 None。
        # t_eff <= 0 (提议已逾期但无通过事件) 在 PDE 末步兜底应用。
        self._scheduled_reset_t: float | None = None
        if self.scheduled_reset_date is not None and self.scheduled_reset_prob > 0:
            t_eff = (self.scheduled_reset_date - current_date).days / _DAYS_PER_YEAR
            if t_eff <= self.T:
                self._scheduled_reset_t = t_eff

    def _constructor_kwargs(self) -> dict:
        """构造参数的单一事实源: Theta 重算等克隆场景复用, 避免漏同步新字段。

        新增 ``__init__`` 参数时只需在此登记一处; 否则 Theta 会静默用旧默认值
        重建明日 pricer, 导致唯独 Theta 偏差且极难定位。存储值均已规范化,
        重新喂回构造器是幂等的。
        """
        return dict(
            S0=self.S0,
            K=self.K,
            current_date=self.current_date,
            maturity_date=self.maturity_date,
            face_value=self.face_value,
            redemption_price=self.redemption_price,
            issue_date=self.issue_date,
            conversion_start_date=self.conversion_start_date,
            call_start_date=self.call_start_date,
            coupon_rates=self.coupon_rates,
            call_trigger_ratio=self.call_trigger_ratio,
            call_no_redemption_until=self.call_no_redemption_until,
            put_trigger_ratio=self.put_trigger_ratio,
            put_active_years=self.put_active_years,
            putback_start_date=self.putback_start_date,
            putback_end_date=self.putback_end_date,
            putback_price=self.putback_price,
            down_reset_premium=self.down_reset_premium,
            down_reset_trigger_ratio=self.down_reset_trigger_ratio,
            down_reset_block_until=self.down_reset_block_until,
            down_reset_floor=self.down_reset_floor,
            call_notice_days=self.call_notice_days,
            scheduled_reset_date=self.scheduled_reset_date,
            scheduled_reset_prob=self.scheduled_reset_prob,
            scheduled_reset_target_k=self.scheduled_reset_target_k,
        )

    @staticmethod
    def _validate_inputs(S0, K, current_date, maturity_date, face_value):
        if not isinstance(S0, (int, float)) or (S0 != S0) or (S0 != 0 and abs(S0) == float("inf")):
            raise ValueError(f"S0 must be a finite number, got {S0!r}")
        if S0 <= 0:
            raise ValueError("S0 must be positive")
        if not isinstance(K, (int, float)) or (K != K) or (K != 0 and abs(K) == float("inf")):
            raise ValueError(f"K must be a finite number, got {K!r}")
        if K <= 0:
            raise ValueError("K must be positive")
        if face_value <= 0:
            raise ValueError("face_value must be positive")
        if maturity_date <= current_date:
            raise ValueError("maturity_date must be after current_date")

    # 共用 convertible_bond.dateutil.add_years (保留静态方法 API 供既有调用/测试)
    _add_years = staticmethod(_add_years_impl)

    def _build_coupon_periods(self):
        return build_coupon_periods(
            self.face_value, self.coupon_rates, self.issue_date, self.maturity_date)

    def get_coupon_rate(self, valuation_date):
        for period in self.coupon_periods:
            if period["start"] <= valuation_date < period["end"]:
                return period["rate"]
        return self.coupon_periods[-1]["rate"]

    def accrued_interest(self, valuation_date):
        return accrued_interest_amount(
            self.coupon_periods, valuation_date,
            face_value=self.face_value,
            issue_date=self.issue_date,
            maturity_date=self.maturity_date,
        )

    def discrete_coupon_amount(self, interval_start: date, interval_end: date) -> float:
        """计算 (interval_start, interval_end] 区间内的离散票息支付额.
        
        注意: 使用半开区间 (start, end], 当 interval_start 恰好等于付息日时,
        该笔票息不计入当前区间, 避免与前一区间重复计数.
        """
        cash = 0.0
        for period in self.coupon_periods:
            payment_date = period["end"]
            if period["is_final"]:
                continue
            if interval_start < payment_date <= interval_end:
                cash += period["coupon_amount"]
        return cash

    def bond_floor_value(self, valuation_date, discount_rate):
        value = self.redemption_price / np.exp(discount_rate * max(0.0, (self.maturity_date - valuation_date).days / _DAYS_PER_YEAR))
        for period in self.coupon_periods:
            if period["is_final"] or period["end"] <= valuation_date:
                continue
            tau = (period["end"] - valuation_date).days / _DAYS_PER_YEAR
            value += period["coupon_amount"] / np.exp(discount_rate * max(0.0, tau))
        return value

    def adjust_conversion_price(self, stock_dividend_ratio=0.0,
                                rights_issue_ratio=0.0,
                                rights_issue_price=None,
                                cash_dividend=0.0):
        """按募集说明书中的公式调整转股价格。

        注意: 本方法就地修改实例状态 (self.K / self.ratio), 非线程安全;
        多线程批量定价中共享同一 pricer 实例时不要调用, 应在构造前调整 K
        或每线程独立构造实例。
        """
        if rights_issue_ratio and rights_issue_price is None:
            raise ValueError("rights_issue_price is required when rights_issue_ratio > 0")

        adjusted = self.K - cash_dividend
        if adjusted <= 0:
            raise ValueError(f"调整后转股价分子 {adjusted:.4f} <= 0, cash_dividend={cash_dividend!r} 不能超过当前转股价 {self.K}")
        denominator = 1.0 + stock_dividend_ratio + rights_issue_ratio
        numerator = adjusted + (rights_issue_price or 0.0) * rights_issue_ratio
        new_K = round(numerator / denominator, 2)
        if new_K <= 0:
            raise ValueError(f"调整后的转股价 {new_K} <= 0, 请检查参数 (K={self.K}, cash_dividend={cash_dividend!r})")
        self.K = new_K
        self.ratio = self.face_value / self.K
        return self.K

    def _down_reset_value(self, S_grid: np.ndarray, V: np.ndarray,
                          target_k: float | None = None):
        """下修后的延续价值, 用齐次性近似在同一网格上取 (背景 hazard 与已公告节点共用).

        下修后 K_new = S / down_reset_premium (受 down_reset_floor 约束)。由
        (S, K) 一阶齐次性, 当前股价 S 处的 post-reset 价值 ≈ 原网格上 moneyness
        相同点的 V; 再以转股价值 face·(K/K_new) 作下限。

        - ``target_k`` 给定 (公告解析到的确定新 K): K_new = target_k 固定常数,
          逐点映射。注意若 target_k == 现 K (下修已落地), 映射为恒等 → 节点变 no-op,
          天然防止与条款刷新双计。
        - 无 floor: K_new = S/premium → moneyness=premium 处取值, 对所有 S 同一标量。
        - 有 floor: K_new = max(S/premium, floor), 逐点映射到同 moneyness 的旧网格。

        返回标量 (无 floor 估算) 或数组; 调用方用 np.maximum 广播即可。
        """
        if target_k is not None:
            tk = float(target_k)
            equiv_s = self.K * S_grid / tk
            V_post_reset = np.interp(equiv_s, S_grid, V)
            conv_floor = self.face_value * S_grid / tk
            return np.maximum(V_post_reset, conv_floor)
        if self.down_reset_floor is not None:
            # 现实下修价有均价/净资产下限。floor 绑定时下修后 moneyness 不再固定,
            # 因此逐点映射到同 moneyness 的旧网格。
            target_k = np.maximum(
                S_grid / self.down_reset_premium,
                float(self.down_reset_floor),
            )
            equiv_s = self.K * S_grid / target_k
            V_post_reset = np.interp(equiv_s, S_grid, V)
            conv_floor = self.face_value * S_grid / target_k
            return np.maximum(V_post_reset, conv_floor)
        V_post_reset = float(np.interp(self.K * self.down_reset_premium, S_grid, V))
        conv_floor = self.face_value * self.down_reset_premium
        return max(V_post_reset, conv_floor)

    def _price_grid(self, sigma: float, r: float, q: float, base_spread: float,
                    p_down: float, distress_k: float,
                    M: int, N: int, *,
                    capture_out: dict | None = None,
                    capture_elapsed: float = 0.0) -> tuple[np.ndarray, np.ndarray]:
        """求解 PDE 并返回 (S_grid, V). price() 与希腊值扰动共用此核心.

        ``capture_out`` 给定时, 顺便截下回溯途中 ``t_prev`` **最接近**
        ``capture_elapsed`` (距今年数) 的那一片, 写成 ``{"t": …, "V": …}``。

        Θ 用它而不是"把明天当成新的一只债重解一遍": 那样两次求解的 dt 与 S_max 都变了
        (S_max = exp(3σ√T)·K 随 T 变), 而 Θ·1日 相对价格只有 ~2e-5 量级 —— 两个各带
        O(dt²) 离散误差的数相减, 误差不抵消反而被放大。实测重解法在生产网格上
        **N=1000 偏 +63.8% / N=2000 偏 +9.5%** (基准 N=16000), 要到 N=4000 才收敛。
        同一次回溯里的两片共享网格与格式, 误差同向抵消; 而且**省掉一整次求解** ——
        ``return_greeks=True`` 原本要解 3 次 (基准 + vega 扰动 + 明天), 现在 2 次。
        """
        # S_max 上限 _S_MAX_CAP 防止极端 σ/T (如 σ=2.0, T=10) 下 exp(3σ√T) 天文数字导致 OOM
        S_max_ref = min(max(4.0, float(np.exp(3.0 * sigma * np.sqrt(self.T)))), _S_MAX_CAP) * self.K
        S_max = max(S_max_ref, 1.5 * self.S0)
        dt = self.T / N
        S_grid = np.linspace(0, S_max, M + 1)
        risk_neutral_drift = r - q

        V = np.maximum(self.redemption_price, S_grid * self.ratio)

        # ── 循环不变量: 三对角系数与矩阵只依赖常量, 预计算一次 ──
        # current_spreads/r_total 只随 S_grid (固定网格) 与 base_spread/distress_k 变化,
        # 时间步之间恒定; 因此 alpha/beta/gamma 与系数矩阵 A 在整个回溯过程不变。
        # 提到循环外避免每步重建 (N 步省 N-1 次构造), 数值结果逐位一致。
        current_spreads = base_spread + distress_k * np.maximum(0, 1 - S_grid/self.K)
        r_total = r + current_spreads
        j = np.arange(1, M)
        r_mid = r_total[1:M]

        # 设计决策: alpha/gamma 的漂移项使用 r-q (含连续股息率的风险中性漂移),
        # 而 beta 的折现项使用 r_total = r + credit_spread.
        # 即: 信用利差仅影响折现 ("额外折现" 模型), 不影响标的的风险中性漂移率.
        alpha = 0.25 * dt * (sigma**2 * j**2 - risk_neutral_drift * j)
        beta  = -0.5 * dt * (sigma**2 * j**2 + r_mid)
        gamma = 0.25 * dt * (sigma**2 * j**2 + risk_neutral_drift * j)
        one_plus_beta = 1 + beta

        A = np.zeros((3, M - 1))
        A[0, 1:] = -gamma[:-1]
        A[1, :] = 1 - beta
        A[2, :-1] = -alpha[1:]

        low_discount = r + base_spread + distress_k

        for n in range(N, 0, -1):
            t_now = n * dt
            t_prev = (n - 1) * dt
            # step_date 仅用于 bond_floor_value / accrued_interest 等按日历日计息的计算;
            # int() 保证日期单调非递减, 不会像 round() 在 dt*365<0.5 时出现多步同日的情况.
            step_date = self.current_date + timedelta(days=int(t_prev * _DAYS_PER_YEAR))

            # 离散票息: 在连续时间轴上判断 (t_prev, t_now] 内是否有付息事件,
            # 避免日期量化导致跨步漏判
            coupon_cash = 0.0
            for t_pay, amount in self._coupon_payment_events:
                if t_prev < t_pay <= t_now:
                    coupon_cash += amount
            if coupon_cash:
                V += coupon_cash

            # RHS 用 V^n (当前步含票息), 再用 V^{n-1} 边界值做 += 修正
            V_now = V.copy()
            V[0] = self.bond_floor_value(step_date, low_discount)
            V[-1] = max(S_grid[-1] * self.ratio, self.face_value + self.accrued_interest(step_date))

            rhs = alpha * V_now[:-2] + one_plus_beta * V_now[1:-1] + gamma * V_now[2:]
            rhs[0] += alpha[0] * V[0]
            rhs[-1] += gamma[-1] * V[-1]

            # check_finite=False: A 与 rhs 均由内部确定性构造, 跳过 scipy 的全数组
            # asarray_chkfinite 扫描 (每步 2 次, 占用显著); 输入无 NaN/inf 风险.
            V[1:M] = solve_banded((1, 1), A, rhs, check_finite=False)

            accrued = self.accrued_interest(step_date)
            call_price = self.face_value + accrued
            put_price = self.face_value + accrued
            can_convert = t_prev >= self._conv_start_t
            call_redemption_allowed = (
                self._call_no_redemption_until_t is None
                or t_prev > self._call_no_redemption_until_t
            )
            can_call = t_prev >= self._call_start_t and call_redemption_allowed

            if can_convert:
                V = np.maximum(V, S_grid * self.ratio)

                if can_call:
                    # 强赎边界: 触发后持有人有 call_notice_days 的窗口, 期间 S 仍可波动
                    # → 留有 stock optionality. 用 BS 短期近似 σ·√t 把 cap 抬高到
                    # max(call_price, parity·(1+σ√t)). 默认 30 天 + σ=30% ≈ 抬升 8.6%,
                    # 与 A 股深度实值转债通常仍贴 5-10% 溢价的实务观察一致.
                    # call_notice_days=0 时退化为旧版"立即行权"刚性 cap.
                    mask_call = S_grid >= self.K * self.call_trigger_ratio
                    if self.call_notice_days > 0:
                        t_grace = self.call_notice_days / _DAYS_PER_YEAR
                        grace_premium = float(sigma) * np.sqrt(t_grace)
                        parity_capped = S_grid[mask_call] * self.ratio * (1.0 + grace_premium)
                    else:
                        parity_capped = S_grid[mask_call] * self.ratio
                    V[mask_call] = np.minimum(
                        V[mask_call],
                        np.maximum(call_price, parity_capped),
                    )

                # 下修博弈: S 低于下修触发线时才可能触发下修, 概率随
                # 低于触发线的程度线性递增。默认 trigger_ratio=1.0 保持旧行为
                # (S<K 即可计入); 条款库有 85%K 等明确阈值时会更保守。
                # p_down 按年化事件强度解释, 每个 PDE 时间步转换成 step probability;
                # 否则会在 N 个时间步里反复应用完整概率, 造成 OTM 转债被严重高估.
                # 下修后 K_new = S / down_reset_premium, 用齐次性近似 post-reset 延续价值:
                # 同一 CB 网格上 moneyness=premium 处的 V, 下限为 face*premium.
                # ITM 区域 (S>K) p_reset=0 不受影响; OTM 区域被适度拉升, 天然单调连续.
                down_reset_allowed = (
                    self._down_reset_block_until_t is None
                    or t_prev > self._down_reset_block_until_t
                )
                if p_down > 0 and down_reset_allowed:
                    # "纯触发后" 模型: 触发线下方 (S < K·trigger_ratio) 一律按 step 概率下修,
                    # 触发线之上为 0。p_down 解释为"触发后公司跟进下修"的年化概率,
                    # 每步换算 1-exp(-p·dt) 保证网格无关。不再用"越跌越可能"的 S 渐变。
                    step_down_prob = 1.0 - float(np.exp(-p_down * dt))
                    trigger_price = self.K * self.down_reset_trigger_ratio
                    p_reset = step_down_prob * (S_grid < trigger_price)
                    reset_value = self._down_reset_value(S_grid, V)
                    V = (1 - p_reset) * V + p_reset * np.maximum(V, reset_value)

                # 已公告下修 (regime ②: 已提议/已通过待生效): 一次性近确定下修节点。
                # 历史上提议后≈100% 通过 (cb_events 校准), 价值跳变集中在生效日附近,
                # 用单点 max(V, reset_value) 混合替代把背景 hazard 放大数倍摊到全周期。
                # target_k 给定时用公告真实新 K, 否则回落 premium/floor 估算。
                # 不受 block_until 约束 — 新公告覆盖此前的"不修正"承诺。
                # t_prev<t_eff<=t_now 跨步时触发; t_eff<=0 (逾期) 在末步 (n==1) 兜底。
                if self._scheduled_reset_t is not None and self.scheduled_reset_prob > 0 and (
                    t_prev < self._scheduled_reset_t <= t_now
                    or (self._scheduled_reset_t <= 0 and n == 1)
                ):
                    reset_value = self._down_reset_value(
                        S_grid, V, target_k=self.scheduled_reset_target_k)
                    p_sched = self.scheduled_reset_prob
                    V = (1 - p_sched) * V + p_sched * np.maximum(V, reset_value)

            putback_window_active = (
                self._putback_start_t is not None
                and self._putback_end_t is not None
                and self._putback_start_t <= t_prev <= self._putback_end_t
            )
            if putback_window_active:
                explicit_put_price = (
                    float(self.putback_price)
                    if self.putback_price is not None
                    else put_price
                )
                V = np.maximum(V, explicit_put_price)
                V[0] = max(V[0], explicit_put_price)
            elif self.put_trigger_ratio is not None and t_prev >= self._put_start_t:
                # **回售期内处处给底, 不按当前 S 掩码**。
                #
                # 曾经这里是 ``V[S <= K*ratio] = max(V, put_price)`` —— 回售价是**常数**,
                # 只加在低价侧, 于是曲面在触发线上有一个台阶。回溯把台阶抹成一段**非单调**
                # 的凹陷, ``dV/dS < 0``: 一只可转债不可能出现负 Δ。
                # 这不是离散伪影 —— M 从 300 加密到 4800 (h 缩小 16 倍), 凹陷稳定在
                # 2.08~2.14 元 (常银转债), 负区 S/K 带稳定在 0.629~0.783。
                # 逐条消融确认 100% 归因于回售: 关掉回售 7/7 负区消失, 关掉强赎 7/7 逐位
                # 不变, 关掉下修 5/7 更糟。
                #
                # **为什么不照搬强赎那一侧的 σ√t 软化**: 强赎的 cap 是
                # ``max(call_price, parity·(1+σ√t_grace))`` —— 目标**随 S 上升**, 曲面保持
                # 单调; 回售的底是常数, 软化只让 V 连续、仍然非单调 (实测 5/7 负区还在,
                # 常银 min Δ = −2.46), 还要 +28% 运行时和一个新自由参数。
                #
                # **代价要认**: 这比真实条款**宽**。真实条款是「**连续三十个交易日**收盘价
                # 低于转股价 70%」, 外加「每年…行使回售权一次」的年度用尽规则 —— 两者在
                # 数据模型里都没有字段 (只有 ``put_trigger_pct`` 幅度与 ``put_obs_months``),
                # 真做路径依赖要加一维网格 (实测 31 倍运行时) 且没有日线可以种计数器。
                # 所以这是在"按 S 掩码"与"路径依赖"之间取的那个**结构上单调**的上界:
                # 实测全池 291/311 只价格变动 < 0.005 元, 最大 +1.0037 元 (燃23转债),
                # 分桶 0 处变化, 「低估候选」成员 40 → 40 逐只相同, 前 30 名不变。
                V = np.maximum(V, put_price)
                V[0] = max(V[0], put_price)

            V[-1] = max(V[-1], S_grid[-1] * self.ratio)

            # 沿途截片: t_prev 单调递减, 取与目标最接近的那一步 (>0 才有意义 —— t_prev=0
            # 就是今天本身, 拿它算 Θ 恒得 0)。
            if capture_out is not None and t_prev > 0:
                gap = abs(t_prev - capture_elapsed)
                if gap <= capture_out.get("gap", float("inf")):
                    capture_out["gap"] = gap
                    capture_out["t"] = t_prev
                    capture_out["V"] = V.copy()

        return S_grid, V

    @overload
    def price(self, sigma: float, r: float, base_spread: float,
              p_down: float = ..., distress_k: float = ...,
              M: int = ..., N: int = ...,
              return_greeks: Literal[False] = ...,
              q: float = ...) -> float: ...
    @overload
    def price(self, sigma: float, r: float, base_spread: float,
              p_down: float = ..., distress_k: float = ...,
              M: int = ..., N: int = ...,
              *, return_greeks: Literal[True],
              q: float = ...) -> dict[str, float]: ...

    def price(self, sigma: float, r: float, base_spread: float,
              p_down: float = 0.1,        # 下修博弈年化强度
              distress_k: float = 0.0,    # 信用扩张系数 (优化 3: 股价下跌导致利差增加)
              M: int = 500, N: int = 2000,
              return_greeks: bool = False,
              q: float = 0.0) -> float | dict[str, float]:
        """求解理论价. return_greeks=True 时返回 dict (含 Δ/Γ/ν/Θ + 价值分解).

        说明:
        - ``q`` 为连续股息率 (小数, 例如 0.02 表示 2%/年), 进入股价漂移 ``r-q``。
        - ``vega`` 单位是 "理论价 / +1pp σ" (已乘以 0.01).
        - ``theta`` 单位是 "理论价 / +1 个日历日" (按实际/365 推进; 不剔除非交易日).
        - ``option_premium = price - max(bond_floor, parity)``: 在深度 ITM 且强赎宽限期内,
          模型 cap 把 V 截到 parity·(1+σ√t_grace), 数值上略低于 parity 时该字段可能为
          小负数 (~ 0.x 元), 不是错误而是 cap 与离散网格的数值噪声边界。
        """
        if sigma <= 0:
            raise ValueError("sigma must be positive (sigma=0 would cause PDE degeneracy)")
        if r < 0 or q < 0 or base_spread < 0 or p_down < 0:
            raise ValueError("r, q, base_spread and p_down must be non-negative")
        if M < 3 or N < 1:
            raise ValueError("M must be >= 3 and N must be >= 1")

        # 只有要 Θ 时才截片 (多一次 V.copy()/步)
        theta_slice: dict | None = {} if return_greeks else None
        S_grid, V = self._price_grid(
            sigma, r, q, base_spread, p_down, distress_k, M, N,
            capture_out=theta_slice, capture_elapsed=1.0 / _DAYS_PER_YEAR)
        theo = float(np.interp(self.S0, S_grid, V))

        if not return_greeks:
            return theo

        S0 = self.S0

        # Δ/Γ: 先在 PDE 网格上求导数场, 再插值到 S0; 不要对 np.interp 的结果做差分。
        # 原因: np.interp 是分段线性的, 而扰动步长 (~0.01*S0) 往往远小于网格步长
        # h = S_max/M —— 高 σ 或长久期时 S_max = exp(3σ√T)*K 会把 h 撑到 10 元以上,
        # 三个取值点落进同一线性段, 二阶差分恒为 0, Γ 直接变成 0.000000;
        # 即使跨段, 得到的也只是折点位置的伪影而非曲率 (实测相邻久期可差 4 倍)。
        h = float(S_grid[1] - S_grid[0])
        delta_grid = np.gradient(V, h)
        gamma_grid = np.gradient(delta_grid, h)
        delta = float(np.interp(S0, S_grid, delta_grid))
        gamma = float(np.interp(S0, S_grid, gamma_grid))

        # Vega: σ +1pp 整局重算; 单位为 "理论价 / 1pp σ"
        d_sigma = 0.01
        S_grid_v, V_v = self._price_grid(sigma + d_sigma, r, q, base_spread,
                                         p_down, distress_k, M, N)
        theo_vol = float(np.interp(S0, S_grid_v, V_v))
        vega = (theo_vol - theo)  # / d_sigma * 0.01 = / 1, 即每 1pp σ 的价格变化

        # Theta: 取**同一次回溯**里 t≈1 天那一片, 不再把明天当成新的一只债重解。
        # 重解法的两次求解 dt 与 S_max (= exp(3σ√T)·K, 随 T 变) 都不同, 而 Θ·1日 相对
        # 价格只有 ~2e-5 —— 两个各带 O(dt²) 误差的数相减, 误差不抵消。实测生产网格上
        # N=1000 偏 +63.8%、N=2000 偏 +9.5% (基准 N=16000)。同一次回溯的两片共享网格,
        # 误差同向抵消。网格步长 dt 未必正好等于 1 天 (N=1000, T=3.3 年时 dt≈1.2 天),
        # 所以按实际截到的 t 归一化到"每日历日"。
        #
        # **票息落在截片窗口内时 Θ 是除息下跌, 不是时间价值衰减** —— 那是脏价的真实
        # 行为, 不是 bug: 理论价含应计, 债券在除息日就会掉一个票息。旧的"重解明天"
        # 写法给同一个结果 (实测票息 1.0 元时 旧法 −0.996 / 新法 −0.907, 而无票息的
        # 常态是 +0.005), 所以这不是截片引入的。
        # 已知局限: **归一化把这个离散跳变按时间比例摊薄了** (截片落在 1.09 天时 −1.0
        # 的跳被报成 −0.92) —— 离散跳变不随时间缩放, 那一档的 Θ 只说得清方向, 说不清
        # 速率。不在这里特判剔票息: 那会让 Θ 变成净价口径, 与 ``price`` 的脏价分叉。
        # 行为由 test_theta_across_a_coupon_is_the_ex_coupon_drop 钉住。
        if theta_slice and theta_slice.get("t"):
            theo_ahead = float(np.interp(S0, S_grid, theta_slice["V"]))
            theta = (theo_ahead - theo) / (theta_slice["t"] * _DAYS_PER_YEAR)
        else:
            # 截不到片 = 只剩 1 步 (T 极短), Θ 没有意义
            theta = float("nan")

        # 债底要用**模型自己在 S0 处用的**那个利差, 不是裸 base_spread: 求解器里
        # current_spreads = base_spread + distress_k·max(0, 1 − S/K), 而这里只用
        # base_spread —— 两个数说的是同一只债的同一个量, 却各算各的。
        # 实测生产口径 (distress_k=0.05) 下 S0/K=0.38 时报出的债底比模型自用的高 8.9 元,
        # S0/K=0.09 时高 12.7 元; 而 option_premium = price − max(bond_floor, parity)
        # 直接吃这个差, 会渲染出**负的期权溢价** (−0.250) —— 一个债底比全价还高的组合,
        # 在模型自己的口径里根本不存在。
        spread_at_s0 = base_spread + distress_k * max(0.0, 1.0 - S0 / self.K)
        bond_floor = float(self.bond_floor_value(self.current_date, r + spread_at_s0))
        parity = float(self.S0 * self.ratio)
        # 深度实值 + 已过强赎窗口时, PDE cap 至 parity·(1+σ√t_grace),
        # 期权溢价 ≈ 强赎宽限期内的 stock optionality. call_notice_days=0 时退化为 0.
        option_premium = theo - max(bond_floor, parity)

        return {
            "price": theo,
            "delta": delta,
            "gamma": gamma,
            "vega": vega,
            "theta": theta,
            "bond_floor": bond_floor,
            "parity": parity,
            "option_premium": float(option_premium),
        }

    def solve_implied_vol(self, target_price: float, r: float, base_spread: float,
                          p_down: float = 0.0, distress_k: float = 0.0,
                          M: int = 300, N: int = 1000,
                          sigma_lo: float = 0.05, sigma_hi: float = 2.0,
                          tol: float = 1e-3,
                          q: float = 0.0) -> float:
        """反解使理论价 == target_price 的隐含波动率 (年化, 小数). 失败返回 NaN.

        网格默认 M=300/N=1000 与批量定价一致, 比单只定价 (M=500/N=2000) 粗一档,
        是为了在 brentq 多次求值时控制总耗时; 精度足以满足 IV 反解的 tol=1e-3。
        """
        def diff(s: float) -> float:
            return float(self.price(sigma=s, r=r, base_spread=base_spread,
                                    p_down=p_down, distress_k=distress_k,
                                    M=M, N=N, q=q)) - target_price

        try:
            f_lo = diff(sigma_lo)
            f_hi = diff(sigma_hi)
        except Exception:
            return float("nan")
        if f_lo * f_hi > 0:
            # 目标价超出可达区间 (低于 σ_lo 价或高于 σ_hi 价), 无解
            return float("nan")
        try:
            return float(brentq(diff, sigma_lo, sigma_hi, xtol=tol, maxiter=40))
        except Exception:
            return float("nan")
