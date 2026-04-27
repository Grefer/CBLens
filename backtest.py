"""
可转债历史回测模块.

从 CB.py 拆分而来, 包含 backtest_theoretical_price 及其 Wind 依赖辅助函数.
"""
import bisect
import logging
import numpy as np
from datetime import date, timedelta
from typing import Optional

from pricer import UniversalCBPricer, DEFAULT_REDEMPTION_PRICE

logger = logging.getLogger(__name__)


# ── Wind 辅助 (遗留, 仅供 backtest_theoretical_price 内部使用) ──
def _ensure_wind():
    try:
        from WindPy import w  # type: ignore[import-not-found]
    except ImportError as e:
        raise ImportError(
            "未检测到 WindPy. 请在 Wind 终端 '插件管理' 中安装 Python 接口, "
            "或使用 DataProvider 接口 (price_from_provider)."
        ) from e
    if not w.isconnected():
        ret = w.start()
        if ret.ErrorCode != 0:
            raise RuntimeError(f"Wind 启动失败 (ErrorCode={ret.ErrorCode})")
    return w


def _to_date(v):
    if v is None:
        return None
    from datetime import datetime
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def _parse_coupon(raw):
    if raw is None or raw == "":
        return None
    parts = [p.strip().rstrip("%") for p in str(raw).split(",") if p.strip()]
    try:
        return tuple(float(p) / 100.0 for p in parts)
    except ValueError:
        return None


def _fetch_cashflow(w, bond_code):
    res = w.wset("cashflow", f"windcode={bond_code}")
    if res.ErrorCode != 0 or not res.Data:
        return None
    fields = [f.lower() for f in res.Fields]
    try:
        i_date = fields.index("cash_flows_date")
        i_cf = fields.index("cash_flows_per_cny100_par")
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
    last = rows[-1]
    return {
        "coupon_rates": tuple(coupons) if coupons else None,
        "redemption_price": float(last[i_cf]) if last[i_cf] is not None else None,
        "maturity_date": _to_date(last[i_date]) if last[i_date] else None,
    }


def _hist_vol(w, stock_code, end_date, window_days):
    lookback = max(window_days * 2, window_days + 15)
    start = end_date - timedelta(days=lookback)
    res = w.wsd(stock_code, "close", start.isoformat(), end_date.isoformat(), "priceAdj=U")
    if res.ErrorCode != 0:
        raise RuntimeError(f"Wind 取正股历史价失败: {res.Data}")
    closes = np.array([float(v) if v is not None else np.nan for v in res.Data[0]])
    closes = closes[~np.isnan(closes)]
    if len(closes) > window_days + 1:
        closes = closes[-(window_days + 1):]
    if len(closes) < 5:
        raise ValueError(f"{stock_code} 历史样本仅 {len(closes)} 条, 无法估算波动率")
    log_ret = np.diff(np.log(closes))
    return float(np.std(log_ret, ddof=1) * np.sqrt(252))


# ── 回测主函数 ──────────────────────────────────────────────
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
    solve_iv=False,
    progress_cb=None,
    **pricer_overrides,
):
    """
    对历史时间区间内每个采样日逐点计算理论价, 返回与转债实际收盘价的对比序列.

    假设: K/条款/票息用当前值 (忽略历史下修); 正股 S0 与滚动 σ 取历史值.

    参数:
        freq: "D"(日)/"W"(周)/"M"(月). 采样频率
        solve_iv: 若为 True, 逐点反解隐含波动率 (耗时 ~5x). 失败/越界返回 NaN.
        progress_cb: callable(i, total) 用于 UI 进度反馈
    返回: dict{dates, theo_prices, market_prices, stock_prices, sigmas,
              bond_floors, parities, ivs}
        - sigmas 为采样日滚动 σ (年化, HV)
        - bond_floors / parities 为同期纯债价值 / 转股价值, 用于价值分解
        - ivs 为反解 IV 序列 (solve_iv=False 时全为 NaN)
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
    bf_out, par_out, iv_out = [], [], []
    total = len(sample_points)
    iv_M = max(150, M // 3)
    iv_N = max(500, N // 3)
    for i, (val_date, market_px) in enumerate(sample_points):
        if issue_dt and val_date < issue_dt:
            continue
        if maturity_dt and val_date >= maturity_dt:
            continue

        pos = bisect.bisect_right(stock_dates, val_date) - 1
        idx = None
        while pos >= 0:
            if not np.isnan(stock_close[pos]):
                idx = pos
                break
            pos -= 1
        if idx is None:
            continue
        S0 = stock_close[idx]

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

        bond_floor = float(pricer.bond_floor_value(val_date, r + base_spread))
        parity = float(S0 * pricer.ratio)

        iv_val = float("nan")
        if solve_iv and market_px is not None and market_px > 0:
            try:
                iv_val = float(pricer.solve_implied_vol(
                    target_price=float(market_px), r=r, base_spread=base_spread,
                    p_down=p_down, distress_k=distress_k, M=iv_M, N=iv_N))
            except Exception as exc:
                logger.debug("回测采样日 %s IV 反解失败: %s", val_date, exc)

        dates_out.append(val_date)
        theo_out.append(float(theo))
        mkt_out.append(market_px)
        s0_out.append(float(S0))
        sigma_out.append(sigma)
        bf_out.append(bond_floor)
        par_out.append(parity)
        iv_out.append(iv_val)
        if progress_cb:
            progress_cb(i + 1, total)

    return {
        "dates": dates_out,
        "theo_prices": theo_out,
        "market_prices": mkt_out,
        "stock_prices": s0_out,
        "sigmas": sigma_out,
        "bond_floors": bf_out,
        "parities": par_out,
        "ivs": iv_out,
        "bond_code": bond_code,
        "stock_code": stock_code,
    }
