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

logger = logging.getLogger(__name__)

# ── 默认常量 ─────────────────────────────────────────────
DEFAULT_COUPON_RATES: tuple[float, ...] = (0.003, 0.004, 0.008, 0.015, 0.018, 0.02)
DEFAULT_FACE_VALUE: float = 100.0
DEFAULT_REDEMPTION_PRICE: float = 107.0


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
                 put_trigger_ratio: float = 0.7,
                 put_active_years: int = 2,
                 down_reset_premium: float = 1.02,
                 down_reset_block_until: date | None = None,
                 call_notice_days: int = 30):
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
        self.put_trigger_ratio = put_trigger_ratio
        self.put_active_years = put_active_years
        self.down_reset_premium = down_reset_premium
        self.down_reset_block_until = down_reset_block_until
        self.call_notice_days = max(0, int(call_notice_days))
        self.coupon_rates = tuple(coupon_rates or DEFAULT_COUPON_RATES)

        self.T = (maturity_date - current_date).days / 365.0
        self.current_date = current_date
        self.maturity_date = maturity_date
        self.put_start_date = self._add_years(self.maturity_date, -self.put_active_years)
        self.coupon_periods = self._build_coupon_periods()

    @staticmethod
    def _validate_inputs(S0, K, current_date, maturity_date, face_value):
        if S0 <= 0:
            raise ValueError("S0 must be positive")
        if K <= 0:
            raise ValueError("K must be positive")
        if face_value <= 0:
            raise ValueError("face_value must be positive")
        if maturity_date <= current_date:
            raise ValueError("maturity_date must be after current_date")

    @staticmethod
    def _add_years(dt_value: date, years: int) -> date:
        new_year = dt_value.year + years
        if new_year < 1:
            raise ValueError(f"Cannot add {years} years to {dt_value}: resulting year {new_year} < 1")
        try:
            return dt_value.replace(year=new_year)
        except ValueError:
            return dt_value.replace(month=2, day=28, year=new_year)

    def _build_coupon_periods(self):
        periods = []
        period_start = self.issue_date
        for rate in self.coupon_rates:
            period_end = min(self._add_years(period_start, 1), self.maturity_date)
            periods.append({
                "start": period_start,
                "end": period_end,
                "rate": rate,
                "coupon_amount": self.face_value * rate,
                "is_final": period_end == self.maturity_date,
            })
            period_start = period_end
            if period_end >= self.maturity_date:
                break
        return periods

    def get_coupon_rate(self, valuation_date):
        for period in self.coupon_periods:
            if period["start"] <= valuation_date < period["end"]:
                return period["rate"]
        return self.coupon_periods[-1]["rate"]

    def accrued_interest(self, valuation_date):
        if valuation_date <= self.issue_date:
            return 0.0

        capped_date = min(valuation_date, self.maturity_date)
        for period in self.coupon_periods:
            if period["start"] <= capped_date <= period["end"]:
                accrual_days = (capped_date - period["start"]).days
                return self.face_value * period["rate"] * accrual_days / 365.0
        return 0.0

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
        value = self.redemption_price / np.exp(discount_rate * max(0.0, (self.maturity_date - valuation_date).days / 365.0))
        for period in self.coupon_periods:
            if period["is_final"] or period["end"] <= valuation_date:
                continue
            tau = (period["end"] - valuation_date).days / 365.0
            value += period["coupon_amount"] / np.exp(discount_rate * max(0.0, tau))
        return value

    def adjust_conversion_price(self, stock_dividend_ratio=0.0,
                                rights_issue_ratio=0.0,
                                rights_issue_price=None,
                                cash_dividend=0.0):
        """按募集说明书中的公式调整转股价格。"""
        if rights_issue_ratio and rights_issue_price is None:
            raise ValueError("rights_issue_price is required when rights_issue_ratio > 0")

        adjusted = self.K - cash_dividend
        denominator = 1.0 + stock_dividend_ratio + rights_issue_ratio
        numerator = adjusted + (rights_issue_price or 0.0) * rights_issue_ratio
        self.K = round(numerator / denominator, 2)
        self.ratio = self.face_value / self.K
        return self.K

    def _price_grid(self, sigma: float, r: float, q: float, base_spread: float,
                    p_down: float, distress_k: float,
                    M: int, N: int) -> tuple[np.ndarray, np.ndarray]:
        """求解 PDE 并返回 (S_grid, V). price() 与希腊值扰动共用此核心."""
        S_max_ref = max(4.0, float(np.exp(3.0 * sigma * np.sqrt(self.T)))) * self.K
        S_max = max(S_max_ref, 1.5 * self.S0)
        dt = self.T / N
        S_grid = np.linspace(0, S_max, M + 1)
        risk_neutral_drift = r - q

        V = np.maximum(self.redemption_price, S_grid * self.ratio)

        for n in range(N, 0, -1):
            t_now = n * dt
            t_prev = (n - 1) * dt
            step_date = self.current_date + timedelta(days=round(t_prev * 365.0))
            interval_end = self.current_date + timedelta(days=round(t_now * 365.0))

            current_spreads = base_spread + distress_k * np.maximum(0, 1 - S_grid/self.K)
            r_total = r + current_spreads
            coupon_cash = self.discrete_coupon_amount(step_date, interval_end)
            if coupon_cash:
                V += coupon_cash

            j = np.arange(1, M)
            r_mid = r_total[1:M]

            # 设计决策: alpha/gamma 的漂移项使用 r-q (含连续股息率的风险中性漂移),
            # 而 beta 的折现项使用 r_total = r + credit_spread.
            # 即: 信用利差仅影响折现 ("额外折现" 模型), 不影响标的的风险中性漂移率.
            alpha = 0.25 * dt * (sigma**2 * j**2 - risk_neutral_drift * j)
            beta  = -0.5 * dt * (sigma**2 * j**2 + r_mid)
            gamma = 0.25 * dt * (sigma**2 * j**2 + risk_neutral_drift * j)

            A = np.zeros((3, M - 1))
            A[0, 1:] = -gamma[:-1]
            A[1, :] = 1 - beta
            A[2, :-1] = -alpha[1:]

            # RHS 用 V^n (当前步含票息), 再用 V^{n-1} 边界值做 += 修正
            V_now = V.copy()
            low_discount = r + base_spread + distress_k
            V[0] = self.bond_floor_value(step_date, low_discount)
            V[-1] = max(S_grid[-1] * self.ratio, self.face_value + self.accrued_interest(step_date))

            rhs = alpha * V_now[:-2] + (1 + beta) * V_now[1:-1] + gamma * V_now[2:]
            rhs[0] += alpha[0] * V[0]
            rhs[-1] += gamma[-1] * V[-1]

            V[1:M] = solve_banded((1, 1), A, rhs)

            accrued = self.accrued_interest(step_date)
            call_price = self.face_value + accrued
            put_price = self.face_value + accrued
            can_convert = step_date >= self.conversion_start_date
            can_call = step_date >= self.call_start_date

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
                        t_grace = self.call_notice_days / 365.0
                        grace_premium = float(sigma) * np.sqrt(t_grace)
                        parity_capped = S_grid[mask_call] * self.ratio * (1.0 + grace_premium)
                    else:
                        parity_capped = S_grid[mask_call] * self.ratio
                    V[mask_call] = np.minimum(
                        V[mask_call],
                        np.maximum(call_price, parity_capped),
                    )

                # 下修博弈: S < K 时才可能触发下修, 概率随 OTM 程度线性递增.
                # p_down 按年化事件强度解释, 每个 PDE 时间步转换成 step probability;
                # 否则会在 N 个时间步里反复应用完整概率, 造成 OTM 转债被严重高估.
                # 下修后 K_new = S / down_reset_premium, 用齐次性近似 post-reset 延续价值:
                # 同一 CB 网格上 moneyness=premium 处的 V, 下限为 face*premium.
                # ITM 区域 (S>K) p_reset=0 不受影响; OTM 区域被适度拉升, 天然单调连续.
                down_reset_allowed = (
                    self.down_reset_block_until is None
                    or step_date > self.down_reset_block_until
                )
                if p_down > 0 and down_reset_allowed:
                    # S-dependent 下修概率: S>=K → 0, S=0 → step_down_prob
                    step_down_prob = 1.0 - float(np.exp(-p_down * dt))
                    p_reset = step_down_prob * np.clip(1.0 - S_grid / self.K, 0.0, 1.0)
                    V_post_reset = float(np.interp(self.K * self.down_reset_premium, S_grid, V))
                    conv_floor = self.face_value * self.down_reset_premium
                    reset_value = max(V_post_reset, conv_floor)
                    V = (1 - p_reset) * V + p_reset * np.maximum(V, reset_value)

            if step_date >= self.put_start_date:
                mask_put = S_grid <= self.K * self.put_trigger_ratio
                V[mask_put] = np.maximum(V[mask_put], put_price)
                V[0] = max(V[0], put_price)

            V[-1] = max(V[-1], S_grid[-1] * self.ratio)

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
        if sigma < 0 or r < 0 or q < 0 or base_spread < 0:
            raise ValueError("sigma, r, q and base_spread must be non-negative")
        if M < 3 or N < 1:
            raise ValueError("M must be >= 3 and N must be >= 1")

        S_grid, V = self._price_grid(sigma, r, q, base_spread, p_down, distress_k, M, N)
        theo = float(np.interp(self.S0, S_grid, V))

        if not return_greeks:
            return theo

        S0 = self.S0
        S_max = float(S_grid[-1])
        dS = max(0.01 * S0, 0.001 * self.K)

        if S0 - dS > 0 and S0 + dS < S_max:
            v_up = float(np.interp(S0 + dS, S_grid, V))
            v_dn = float(np.interp(S0 - dS, S_grid, V))
            v_mid = float(np.interp(S0, S_grid, V))
            delta = (v_up - v_dn) / (2 * dS)
            gamma = (v_up - 2 * v_mid + v_dn) / (dS * dS)
        else:
            delta = float("nan")
            gamma = float("nan")

        # Vega: σ +1pp 整局重算; 单位为 "理论价 / 1pp σ"
        d_sigma = 0.01
        S_grid_v, V_v = self._price_grid(sigma + d_sigma, r, q, base_spread,
                                         p_down, distress_k, M, N)
        theo_vol = float(np.interp(S0, S_grid_v, V_v))
        vega = (theo_vol - theo)  # / d_sigma * 0.01 = / 1, 即每 1pp σ 的价格变化

        # Theta: current_date + 1 日 重算; 单位 "理论价 / 天"
        if (self.maturity_date - self.current_date).days > 1:
            tomorrow_pricer = UniversalCBPricer(
                S0=self.S0, K=self.K,
                current_date=self.current_date + timedelta(days=1),
                maturity_date=self.maturity_date,
                face_value=self.face_value,
                redemption_price=self.redemption_price,
                issue_date=self.issue_date,
                conversion_start_date=self.conversion_start_date,
                call_start_date=self.call_start_date,
                coupon_rates=self.coupon_rates,
                call_trigger_ratio=self.call_trigger_ratio,
                put_trigger_ratio=self.put_trigger_ratio,
                put_active_years=self.put_active_years,
                down_reset_premium=self.down_reset_premium,
                down_reset_block_until=self.down_reset_block_until,
                call_notice_days=self.call_notice_days,
            )
            S_grid_t, V_t = tomorrow_pricer._price_grid(
                sigma, r, q, base_spread, p_down, distress_k, M, N)
            theo_tomorrow = float(np.interp(S0, S_grid_t, V_t))
            theta = theo_tomorrow - theo
        else:
            theta = float("nan")

        bond_floor = float(self.bond_floor_value(self.current_date, r + base_spread))
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
