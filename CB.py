import logging
import numpy as np
from scipy.linalg import solve_banded
from datetime import date, datetime, timedelta
from typing import Optional, Tuple, Dict, Any, Callable

logger = logging.getLogger(__name__)

# ── 默认常量 ─────────────────────────────────────────────
DEFAULT_COUPON_RATES: Tuple[float, ...] = (0.003, 0.004, 0.008, 0.015, 0.018, 0.02)
DEFAULT_FACE_VALUE: float = 100.0
DEFAULT_REDEMPTION_PRICE: float = 107.0

__all__ = [
    "UniversalCBPricer",
    "price_from_wind",
    "backtest_theoretical_price",
    "ensure_wind",
    "to_date",
    "parse_coupon",
    "fetch_cashflow",
    "hist_vol",
    "DEFAULT_COUPON_RATES",
    "DEFAULT_FACE_VALUE",
    "DEFAULT_REDEMPTION_PRICE",
]


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
                 issue_date: Optional[date] = None, conversion_start_date: Optional[date] = None,
                 call_start_date: Optional[date] = None,
                 coupon_rates: Optional[Tuple[float, ...]] = None, call_trigger_ratio: float = 1.3,
                 put_trigger_ratio: float = 0.7,
                 put_active_years: int = 2,
                 down_reset_premium: float = 1.02):
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

    def price(self, sigma: float, r: float, base_spread: float, 
              p_down: float = 0.1,        # 下修博弈概率
              distress_k: float = 0.0,    # 信用扩张系数 (优化 3: 股价下跌导致利差增加)
              M: int = 500, N: int = 2000) -> float:
        if sigma < 0 or r < 0 or base_spread < 0:
            raise ValueError("sigma, r and base_spread must be non-negative")
        if M < 3 or N < 1:
            raise ValueError("M must be >= 3 and N must be >= 1")

        S_max_ref = max(4.0, float(np.exp(3.0 * sigma * np.sqrt(self.T)))) * self.K
        S_max = max(S_max_ref, 1.5 * self.S0)
        dt = self.T / N
        S_grid = np.linspace(0, S_max, M + 1)

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
            
            # 设计决策: alpha/gamma 的漂移项使用无风险利率 r (风险中性漂移),
            # 而 beta 的折现项使用 r_total = r + credit_spread.
            # 即: 信用利差仅影响折现 ("额外折现" 模型), 不影响标的的风险中性漂移率.
            alpha = 0.25 * dt * (sigma**2 * j**2 - r * j)
            beta  = -0.5 * dt * (sigma**2 * j**2 + r_mid)
            gamma = 0.25 * dt * (sigma**2 * j**2 + r * j)

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
                    mask_call = S_grid >= self.K * self.call_trigger_ratio
                    V[mask_call] = np.minimum(
                        V[mask_call],
                        np.maximum(call_price, S_grid[mask_call] * self.ratio),
                    )

                # 下修博弈: S < K 时才可能触发下修, 概率随 OTM 程度线性递增.
                # 下修后 K_new = S / down_reset_premium, 用齐次性近似 post-reset 延续价值:
                # 同一 CB 网格上 moneyness=premium 处的 V, 下限为 face*premium.
                # ITM 区域 (S>K) p_reset=0 不受影响; OTM 区域被适度拉升, 天然单调连续.
                if p_down > 0:
                    # S-dependent 下修概率: S>=K → 0, S=0 → p_down
                    p_reset = p_down * np.clip(1.0 - S_grid / self.K, 0.0, 1.0)
                    V_post_reset = float(np.interp(self.K * self.down_reset_premium, S_grid, V))
                    conv_floor = self.face_value * self.down_reset_premium
                    reset_value = max(V_post_reset, conv_floor)
                    V = (1 - p_reset) * V + p_reset * np.maximum(V, reset_value)

            if step_date >= self.put_start_date:
                mask_put = S_grid <= self.K * self.put_trigger_ratio
                V[mask_put] = np.maximum(V[mask_put], put_price)
                V[0] = max(V[0], put_price)

            V[-1] = max(V[-1], S_grid[-1] * self.ratio)

        return np.interp(self.S0, S_grid, V)


# ==========================================
# Wind 接口: 输入转债代码自动拉参数并定价
# ==========================================
def _ensure_wind():
    try:
        from WindPy import w  # type: ignore[import-not-found]
    except ImportError as e:
        raise ImportError(
            "未检测到 WindPy. 请在 Wind 终端 '插件管理' 中安装 Python 接口."
        ) from e
    if not w.isconnected():
        ret = w.start()
        if ret.ErrorCode != 0:
            raise RuntimeError(f"Wind 启动失败 (ErrorCode={ret.ErrorCode})")
    return w


def _to_date(v):
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def _parse_coupon(raw):
    """解析 Wind couponrate 字段. 可转债的 couponrate 常常只返回首年一个值,
    完整阶梯请用 _fetch_cashflow. 此函数只兜底."""
    if raw is None or raw == "":
        return None
    parts = [p.strip().rstrip("%") for p in str(raw).split(",") if p.strip()]
    try:
        return tuple(float(p) / 100.0 for p in parts)
    except ValueError:
        return None


def _fetch_cashflow(w, bond_code):
    """
    通过 wset('cashflow') 拉完整付息计划, 推导出完整阶梯票息、到期兑付价、到期日.

    返回 dict: {coupon_rates, redemption_price, maturity_date, cashflows} 或 None.
    - coupon_rates: 每期票息率 tuple (按年顺序)
    - redemption_price: 末期 '兑付' 行的 cash_flows_per_cny100_par (面值+末期利息+赎回溢价)
    - maturity_date: 末期日期
    - cashflows: 原始现金流列表, 用于展示/诊断

    对已强赎/退市的债, cashflow 仅含实际发生过的流, 末行可能仍是 '兑付' 但金额与原条款不同;
    此函数不做业务判断, 调用方结合 delist_date 自己决定是否采信.
    """
    res = w.wset("cashflow", f"windcode={bond_code}")
    if res.ErrorCode != 0 or not res.Data:
        return None
    fields = [f.lower() for f in res.Fields]
    try:
        i_date = fields.index("cash_flows_date")
        i_cf = fields.index("cash_flows_per_cny100_par")
        i_type = fields.index("cf_type")
        i_rate = fields.index("coupon_rate")
    except ValueError:
        return None

    rows = list(zip(*res.Data))
    if not rows:
        return None

    coupons = []
    for row in rows:
        rate = row[i_rate]
        if rate is None:
            continue
        coupons.append(float(rate) / 100.0)

    last_row = rows[-1]
    last_type = last_row[i_type]
    last_cf = last_row[i_cf]
    last_date = last_row[i_date]

    redemption_price = float(last_cf) if last_cf is not None else None
    maturity_dt = _to_date(last_date) if last_date else None

    return {
        "coupon_rates": tuple(coupons) if coupons else None,
        "redemption_price": redemption_price,
        "maturity_date": maturity_dt,
        "last_cf_type": last_type,
        "cashflows": rows,
    }


def _hist_vol(w, stock_code, end_date, window_days):
    """取最近 window_days 个交易日收盘价的年化历史波动率 (对数收益, ddof=1)."""
    lookback = max(window_days * 2, window_days + 15)
    res = w.wsd(stock_code, "close", end_date - timedelta(days=lookback), end_date, "priceAdj=U")
    if res.ErrorCode != 0:
        raise RuntimeError(f"Wind 取正股 {stock_code} 历史价失败: {res.Data}")
    closes = np.array([float(x) for x in res.Data[0] if x is not None], dtype=float)
    if len(closes) > window_days + 1:
        closes = closes[-(window_days + 1):]
    if len(closes) < 5:
        raise ValueError(f"{stock_code} 历史样本仅 {len(closes)} 条, 无法估算波动率")
    log_ret = np.diff(np.log(closes))
    return float(np.std(log_ret, ddof=1) * np.sqrt(252))


def price_from_wind(bond_code,
                    r=0.022, base_spread=0.03,
                    distress_k=0.05, p_down=0.15,
                    valuation_date=None, vol_window_days=21,
                    sigma=None,
                    M=500, N=2000,
                    **pricer_overrides):
    """
    输入转债代码 (例如 '128009.SZ'), 自动从 Wind 拉取条款+正股行情并返回理论价.

    波动率默认为正股最近 vol_window_days 个交易日的年化历史波动率;
    如需覆盖可直接传 sigma=0.30 或其他 pricer 参数 (K/maturity_date/...).
    """
    w = _ensure_wind()
    val_date = valuation_date or date.today()
    val_str = val_date.strftime("%Y%m%d")

    fields = [
        "sec_name",
        "underlyingcode",
        "ipo_date",
        "maturitydate",
        "latestpar",
        "clause_conversion2_swapshareprice",
        "clause_calloption_redemptionprice",
        "clause_calloption_triggerproportion",
        "clause_putoption_redeem_triggerproportion",
        "clause_putoption_putbackperiodobs",
        "couponrate",
        "close",
        "creditrating",
        "outstandingbalance",
    ]
    res = w.wss(bond_code, ",".join(fields), f"tradeDate={val_str}")
    if res.ErrorCode != 0:
        raise RuntimeError(f"Wind 取 {bond_code} 条款失败: {res.Data}")
    data = {f.lower(): d[0] for f, d in zip(res.Fields, res.Data)}

    stock_code = data.get("underlyingcode")
    if not stock_code:
        raise ValueError(f"{bond_code} 未返回标的正股代码")

    res_s = w.wss(stock_code, "close", f"tradeDate={val_str};priceAdj=U")
    if res_s.ErrorCode != 0:
        raise RuntimeError(f"Wind 取正股 {stock_code} 现价失败: {res_s.Data}")
    S0 = float(res_s.Data[0][0])

    if sigma is None:
        sigma = _hist_vol(w, stock_code, val_date, vol_window_days)

    issue_dt = _to_date(data["ipo_date"])
    # A 股可转债转股起始日 = 发行日 + 6 个月 (监管规定, Wind 无标量字段可直接取)
    conv_start_dt = issue_dt + timedelta(days=180) if issue_dt else None
    call_trigger_pct = data.get("clause_calloption_triggerproportion")

    # 优先用 cashflow 数据集拿完整阶梯票息 + 兑付价 + 到期日; 失败回落到 wss 字段
    cf = _fetch_cashflow(w, bond_code)
    if cf and cf["coupon_rates"]:
        coupon_rates = cf["coupon_rates"]
    else:
        coupon_rates = _parse_coupon(data["couponrate"])

    if cf and cf["maturity_date"]:
        maturity_dt = cf["maturity_date"]
    else:
        maturity_dt = _to_date(data["maturitydate"])

    if cf and cf["redemption_price"] is not None:
        redemption_price = float(cf["redemption_price"])
    elif data["clause_calloption_redemptionprice"] is not None:
        redemption_price = float(data["clause_calloption_redemptionprice"])
    else:
        redemption_price = 107.0

    pricer_kwargs = dict(
        S0=S0,
        K=float(data["clause_conversion2_swapshareprice"]),
        face_value=float(data.get("latestpar") or 100.0),
        current_date=val_date,
        maturity_date=maturity_dt,
        issue_date=issue_dt,
        conversion_start_date=conv_start_dt,
        redemption_price=float(redemption_price),
        coupon_rates=coupon_rates,
    )
    if call_trigger_pct is not None:
        pricer_kwargs["call_trigger_ratio"] = float(call_trigger_pct) / 100.0

    put_trigger_pct = data.get("clause_putoption_redeem_triggerproportion")
    if put_trigger_pct is not None:
        pricer_kwargs["put_trigger_ratio"] = float(put_trigger_pct) / 100.0

    # putbackperiodobs = 发行后 N 个月起可回售. 换算成 put_active_years (整数年)
    put_obs_months = data.get("clause_putoption_putbackperiodobs")
    if put_obs_months is not None and issue_dt and maturity_dt:
        total_months = (maturity_dt - issue_dt).days / 30.4375
        active_years = max(0, (total_months - float(put_obs_months)) / 12)
        pricer_kwargs["put_active_years"] = int(round(active_years))

    pricer_kwargs.update(pricer_overrides)
    pricer = UniversalCBPricer(**pricer_kwargs)  # type: ignore[arg-type]

    theo = pricer.price(sigma=sigma, r=r, base_spread=base_spread,
                        distress_k=distress_k, p_down=p_down, M=M, N=N)
    return {
        "bond_code": bond_code,
        "bond_name": data.get("sec_name"),
        "stock_code": stock_code,
        "valuation_date": val_date,
        "S0": S0,
        "K": pricer.K,
        "T": pricer.T,
        "sigma": sigma,
        "market_price": data.get("close"),
        "credit_rating": data.get("creditrating"),
        "outstanding_balance": data.get("outstandingbalance"),
        "coupon_source": "cashflow" if cf and cf["coupon_rates"] else "couponrate",
        "theoretical_price": theo,
    }


# ==========================================
# 历史理论价回测
# ==========================================
def backtest_theoretical_price(
    bond_code,
    start_date,
    end_date,
    freq="W",
    vol_window_days=21,
    r=0.022,
    base_spread=0.03,
    distress_k=0.05,
    p_down=0.0,
    M=300,
    N=1000,
    progress_cb=None,
    **pricer_overrides,
):
    """
    对历史时间区间内每个采样日逐点计算理论价, 返回与转债实际收盘价的对比序列.

    假设: K/条款/票息用当前值 (忽略历史下修); 正股 S0 与滚动 σ 取历史值.

    参数:
        freq: "D"(日)/"W"(周)/"M"(月). 采样频率
        progress_cb: callable(i, total) 用于 UI 进度反馈
    返回: dict{dates, theo_prices, market_prices, stock_prices, sigmas}
        其中 sigmas 对应每个采样日的滚动 σ (年化)
    """
    w = _ensure_wind()

    # 1) 拿条款快照 (与 price_from_wind 一致)
    fields = [
        "underlyingcode", "ipo_date", "maturitydate", "latestpar",
        "clause_conversion2_swapshareprice",
        "clause_calloption_redemptionprice",
        "clause_calloption_triggerproportion",
        "clause_putoption_redeem_triggerproportion",
        "clause_putoption_putbackperiodobs",
        "couponrate",
    ]
    res = w.wss(bond_code, ",".join(fields), f"tradeDate={end_date.strftime('%Y%m%d')}")
    if res.ErrorCode != 0:
        raise RuntimeError(f"Wind 取 {bond_code} 条款失败: {res.Data}")
    data = {f.lower(): d[0] for f, d in zip(res.Fields, res.Data)}

    stock_code = data.get("underlyingcode")
    if not stock_code:
        raise ValueError(f"{bond_code} 未返回标的正股代码")

    issue_dt = _to_date(data["ipo_date"])
    cf = _fetch_cashflow(w, bond_code)
    coupon_rates = (cf and cf["coupon_rates"]) or _parse_coupon(data["couponrate"])
    maturity_dt = (cf and cf["maturity_date"]) or _to_date(data["maturitydate"])
    if cf and cf["redemption_price"] is not None:
        redemption_price = float(cf["redemption_price"])
    elif data["clause_calloption_redemptionprice"] is not None:
        redemption_price = float(data["clause_calloption_redemptionprice"])
    else:
        redemption_price = 107.0

    K = float(data["clause_conversion2_swapshareprice"])
    face_value = float(data.get("latestpar") or 100.0)
    conv_start_dt = issue_dt + timedelta(days=180) if issue_dt else None

    common_kwargs = dict(
        K=K,
        face_value=face_value,
        maturity_date=maturity_dt,
        issue_date=issue_dt,
        conversion_start_date=conv_start_dt,
        redemption_price=redemption_price,
        coupon_rates=coupon_rates,
    )
    call_pct = data.get("clause_calloption_triggerproportion")
    if call_pct is not None:
        common_kwargs["call_trigger_ratio"] = float(call_pct) / 100.0
    put_pct = data.get("clause_putoption_redeem_triggerproportion")
    if put_pct is not None:
        common_kwargs["put_trigger_ratio"] = float(put_pct) / 100.0
    put_obs_months = data.get("clause_putoption_putbackperiodobs")
    if put_obs_months is not None and issue_dt and maturity_dt:
        total_months = (maturity_dt - issue_dt).days / 30.4375
        active_years = max(0, (total_months - float(put_obs_months)) / 12)
        common_kwargs["put_active_years"] = int(round(active_years))
    common_kwargs.update(pricer_overrides)

    # 2) 批量拉转债与正股历史收盘价
    lookback_start = start_date - timedelta(days=int(vol_window_days * 2.5) + 15)
    res_b = w.wsd(bond_code, "close",
                  start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d"))
    if res_b.ErrorCode != 0:
        raise RuntimeError(f"Wind 取 {bond_code} 历史价失败: {res_b.Data}")
    bond_dates_raw = res_b.Times
    bond_close = res_b.Data[0]
    bond_series = [
        (_to_date(d), float(v) if v is not None else None)
        for d, v in zip(bond_dates_raw, bond_close)
    ]

    res_s = w.wsd(stock_code, "close",
                  lookback_start.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d"),
                  "priceAdj=U")
    if res_s.ErrorCode != 0:
        raise RuntimeError(f"Wind 取正股 {stock_code} 历史价失败: {res_s.Data}")
    stock_dates = [_to_date(d) for d in res_s.Times]
    stock_close = np.array([float(v) if v is not None else np.nan for v in res_s.Data[0]])

    # 3) 采样日筛选
    valid_points = [(d, p) for d, p in bond_series if p is not None]
    if not valid_points:
        raise RuntimeError("历史区间内无有效转债收盘价")

    if freq == "D":
        sample_points = valid_points
    elif freq == "W":
        # 每周取最后一个有效交易日
        by_week = {}
        for d, p in valid_points:
            iso_year, iso_week, _ = d.isocalendar()
            by_week[(iso_year, iso_week)] = (d, p)
        sample_points = sorted(by_week.values(), key=lambda x: x[0])
    elif freq == "M":
        by_month = {}
        for d, p in valid_points:
            by_month[(d.year, d.month)] = (d, p)
        sample_points = sorted(by_month.values(), key=lambda x: x[0])
    else:
        raise ValueError(f"未知频率: {freq}")

    # 4) 逐点定价
    dates_out, theo_out, mkt_out, s0_out, sigma_out = [], [], [], [], []
    total = len(sample_points)
    for i, (val_date, market_px) in enumerate(sample_points):
        if issue_dt and val_date < issue_dt:
            continue
        if maturity_dt and val_date >= maturity_dt:
            continue

        # 拿当日正股 close (找 stock_dates 里 <= val_date 的最近一个)
        idx = None
        for j in range(len(stock_dates) - 1, -1, -1):
            if stock_dates[j] <= val_date and not np.isnan(stock_close[j]):
                idx = j
                break
        if idx is None:
            continue
        S0 = stock_close[idx]

        # 滚动 σ: 取 idx 往前 vol_window_days+1 个有效收盘
        window = stock_close[max(0, idx - vol_window_days * 2): idx + 1]
        window = window[~np.isnan(window)]
        if len(window) > vol_window_days + 1:
            window = window[-(vol_window_days + 1):]
        if len(window) < 5:
            continue
        log_ret = np.diff(np.log(window))
        sigma = float(np.std(log_ret, ddof=1) * np.sqrt(252))

        try:
            pricer = UniversalCBPricer(
                S0=S0, current_date=val_date, **common_kwargs)  # type: ignore[arg-type]
            theo = pricer.price(sigma=sigma, r=r, base_spread=base_spread,
                                distress_k=distress_k, p_down=p_down, M=M, N=N)
        except Exception as exc:
            logger.debug("回测采样日 %s 定价失败: %s", val_date, exc)
            continue

        dates_out.append(val_date)
        theo_out.append(float(theo))
        mkt_out.append(market_px)
        s0_out.append(float(S0))
        sigma_out.append(sigma)
        if progress_cb:
            progress_cb(i + 1, total)

    return {
        "dates": dates_out,
        "theo_prices": theo_out,
        "market_prices": mkt_out,
        "stock_prices": s0_out,
        "sigmas": sigma_out,
        "bond_code": bond_code,
        "stock_code": stock_code,
    }


# ==========================================
# 公有 API 别名 (供 GUI 及外部调用)
# ==========================================
ensure_wind = _ensure_wind
to_date = _to_date
parse_coupon = _parse_coupon
fetch_cashflow = _fetch_cashflow
hist_vol = _hist_vol


# ==========================================
# 示例
# ==========================================
if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        # 用法: python CB.py 128009.SZ [valuation_date]
        bond_code = sys.argv[1]
        val_date = date.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 else None
        result = price_from_wind(bond_code, valuation_date=val_date)
        print(f"--- Wind 自动定价: {result['bond_code']} ---")
        print(f"标的正股: {result['stock_code']}, S0={result['S0']:.3f}")
        print(f"转股价 K: {result['K']:.3f}")
        print(f"剩余期限 T: {result['T']:.4f} 年")
        print(f"历史波动率 (1M): {result['sigma']:.4%}")
        print(f"理论价值: {result['theoretical_price']:.3f}")
    else:
        today = date(2026, 4, 20)
        pricer = UniversalCBPricer(
            S0=55.0, K=52.77,
            current_date=today, maturity_date=date(2026, 7, 30),
            issue_date=date(2020, 7, 30),
            conversion_start_date=date(2021, 2, 6),
            coupon_rates=(0.003, 0.004, 0.008, 0.015, 0.018, 0.02),
            redemption_price=107.0,
        )
        result = pricer.price(sigma=0.28, r=0.022, base_spread=0.03,
                              distress_k=0.05, p_down=0.0)

        print(f"--- 离线示例 ---")
        print(f"当前剩余期限: {pricer.T:.4f} 年")
        print(f"当前票面利率: {pricer.get_coupon_rate(today):.4%}")
        print(f"当前应计利息: {pricer.accrued_interest(today):.4f}")
        print(f"通用模型估算价: {result:.3f}")