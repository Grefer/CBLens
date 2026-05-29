"""
可转债历史回测模块.

通过 DataProvider 抽象拉取数据, 支持 Wind / akshare / CSV 等任意后端.
"""
import bisect
import logging
import numpy as np
from datetime import date, timedelta

from .pricer import UniversalCBPricer, DEFAULT_REDEMPTION_PRICE, DEFAULT_FACE_VALUE
from .data_providers import DataProvider, WindDataProvider, BondTerms
from .down_reset_overrides import resolve_down_reset, resolve_down_reset_intensity
from .model_defaults import DEFAULT_DOWN_RESET_TRIGGER_RATIO

logger = logging.getLogger(__name__)


# ── 回测主函数 ──────────────────────────────────────────────
def backtest_theoretical_price(
    bond_code,
    start_date,
    end_date,
    freq="W",
    vol_window_days=21,
    r=0.022,
    q=0.0,
    base_spread=0.03,
    distress_k=0.05,
    p_down=0.0,
    M=300,
    N=1000,
    solve_iv=False,
    progress_cb=None,
    provider: DataProvider | None = None,
    **pricer_overrides,
):
    """
    对历史时间区间内每个采样日逐点计算理论价, 返回与转债实际收盘价的对比序列.

    条款按采样日 ``valuation_date`` 逐点从 provider 读取; 若 provider 包装了
    HistoricalBondDataProvider, 可复用策略回测的历史快照/patch/公告事件口径。
    正股 S0 与滚动 σ 取历史值。

    参数:
        provider: DataProvider 实例 (Wind/akshare/CSV); 默认 WindDataProvider
        freq: "D"/"W"/"M" 采样频率
        solve_iv: True 时逐点反解 IV (耗时 ~5x)
        progress_cb: callable(i, total) 用于 UI 进度反馈
        返回 dict: {dates, theo_prices, market_prices, stock_prices, sigmas,
                  bond_floors, parities, ivs, conversion_prices,
                  terms_source_diagnostics, bond_code, stock_code}
    """
    if provider is None:
        provider = WindDataProvider()

    # 1) 拉基础条款以确定正股代码; 具体定价条款在每个采样日逐点读取, 避免
    # 把 end_date 或当前条款带回历史日期。
    try:
        initial_terms: BondTerms = provider.get_bond_terms(bond_code, start_date)
    except Exception:
        initial_terms = provider.get_bond_terms(bond_code, end_date)
    stock_code = initial_terms.underlying_code
    if not stock_code:
        raise ValueError(f"{bond_code} 数据源未返回标的正股代码")

    cf = provider.get_cashflow(bond_code)
    # 2) 拉历史价格 (转债 + 正股, 多取 2.5x vol_window 用于滚动 σ)
    lookback_start = start_date - timedelta(days=int(vol_window_days * 2.5) + 15)

    bond_series_raw = provider.get_bond_history(bond_code, start_date, end_date)
    bond_series = [(d, v) for d, v in bond_series_raw if d is not None]

    stock_series = provider.get_stock_history(stock_code, lookback_start, end_date)
    stock_dates = [d for d, _ in stock_series if d is not None]
    stock_close = np.array(
        [float(v) if v is not None else np.nan for d, v in stock_series if d is not None]
    )

    # 3) 采样筛选
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
    bf_out, par_out, iv_out, k_out, diag_out = [], [], [], [], []
    total = len(sample_points)
    last_progress = 0
    iv_M = max(150, M // 3)
    iv_N = max(500, N // 3)

    last_terms_value_error: ValueError | None = None
    for i, (val_date, market_px) in enumerate(sample_points):
        try:
            terms = provider.get_bond_terms(bond_code, val_date)
            point_kwargs, issue_dt, maturity_dt = _build_backtest_pricer_kwargs(
                bond_code,
                terms,
                cf,
            )
        except ValueError as exc:
            # 单个采样日条款不全 (例如发行前历史日缺转股价/到期日) 仅跳过该点,
            # 不再中止整段回测; 若全程无任何可定价点, 循环后统一抛出该错误。
            last_terms_value_error = exc
            logger.debug("回测采样日 %s 条款不完整: %s", val_date, exc)
            continue
        except Exception as exc:
            logger.debug("回测采样日 %s 条款获取失败: %s", val_date, exc)
            continue

        point_stock_code = terms.underlying_code or stock_code
        if point_stock_code != stock_code:
            logger.debug(
                "回测采样日 %s 正股代码变化: %s -> %s, 跳过",
                val_date, stock_code, point_stock_code,
            )
            continue
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
            resolved_down_reset = resolve_down_reset(
                bond_code, terms, valuation_date=val_date,
            )
            if resolved_down_reset.block_until is not None:
                point_kwargs["down_reset_block_until"] = resolved_down_reset.block_until
            point_kwargs.update(pricer_overrides)
            down_intensity = resolve_down_reset_intensity(
                p_down, resolved_down_reset,
            )
            effective_p_down = down_intensity.effective_p_down
            if (
                down_intensity.scheduled_reset_date is not None
                and down_intensity.scheduled_reset_prob > 0
            ):
                point_kwargs.setdefault(
                    "scheduled_reset_date", down_intensity.scheduled_reset_date)
                point_kwargs.setdefault(
                    "scheduled_reset_prob", down_intensity.scheduled_reset_prob)
                if down_intensity.scheduled_reset_target_k is not None:
                    point_kwargs.setdefault(
                        "scheduled_reset_target_k", down_intensity.scheduled_reset_target_k)

            pricer = UniversalCBPricer(
                S0=S0, current_date=val_date, **point_kwargs)  # type: ignore[arg-type]
            theo = pricer.price(sigma=sigma, r=r, q=q, base_spread=base_spread,
                                distress_k=distress_k, p_down=effective_p_down, M=M, N=N)
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
                    p_down=effective_p_down, distress_k=distress_k,
                    M=iv_M, N=iv_N, q=q))
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
        k_out.append(float(point_kwargs["K"]))
        diag_out.append(_terms_source_diagnostic(provider, bond_code, val_date))
        if progress_cb:
            last_progress = i + 1
            progress_cb(last_progress, total)

    if progress_cb and last_progress < total:
        progress_cb(total, total)

    # 全程无任何可定价点且曾因条款不全跳过 → 把硬错误透出, 避免静默空结果。
    if not dates_out and last_terms_value_error is not None:
        raise last_terms_value_error

    return {
        "dates": dates_out,
        "theo_prices": theo_out,
        "market_prices": mkt_out,
        "stock_prices": s0_out,
        "sigmas": sigma_out,
        "bond_floors": bf_out,
        "parities": par_out,
        "ivs": iv_out,
        "conversion_prices": k_out,
        "terms_source_diagnostics": diag_out,
        "bond_code": bond_code,
        "stock_code": stock_code,
    }


def _build_backtest_pricer_kwargs(
    bond_code: str,
    terms: BondTerms,
    cf,
) -> tuple[dict, date | None, date]:
    """按估值日条款构建 UniversalCBPricer 参数."""
    issue_dt = terms.issue_date
    coupon_rates = (cf.coupon_rates if cf and cf.coupon_rates else terms.coupon_rates)
    maturity_dt = (cf.maturity_date if cf and cf.maturity_date else terms.maturity_date)
    if cf and cf.redemption_price is not None:
        redemption_price = float(cf.redemption_price)
    elif terms.redemption_price is not None:
        redemption_price = float(terms.redemption_price)
    else:
        redemption_price = DEFAULT_REDEMPTION_PRICE

    if terms.conversion_price is None:
        raise ValueError(f"{bond_code} 数据源未返回转股价 K")
    if maturity_dt is None:
        raise ValueError(f"{bond_code} 数据源未返回到期日 maturity_date")

    conv_start_dt = issue_dt + timedelta(days=180) if issue_dt else None
    kwargs = dict(
        K=float(terms.conversion_price),
        face_value=float(terms.face_value or DEFAULT_FACE_VALUE),
        maturity_date=maturity_dt,
        issue_date=issue_dt,
        conversion_start_date=conv_start_dt,
        redemption_price=redemption_price,
        coupon_rates=coupon_rates,
    )
    kwargs["down_reset_trigger_ratio"] = (
        float(terms.down_reset_trigger_pct) / 100.0
        if terms.down_reset_trigger_pct is not None
        else DEFAULT_DOWN_RESET_TRIGGER_RATIO
    )
    if terms.call_trigger_pct is not None:
        kwargs["call_trigger_ratio"] = float(terms.call_trigger_pct) / 100.0
    if terms.call_no_redemption_until is not None:
        kwargs["call_no_redemption_until"] = terms.call_no_redemption_until
    if terms.put_trigger_pct is not None:
        kwargs["put_trigger_ratio"] = float(terms.put_trigger_pct) / 100.0
    if terms.putback_start_date is not None:
        kwargs["putback_start_date"] = terms.putback_start_date
    if terms.putback_end_date is not None:
        kwargs["putback_end_date"] = terms.putback_end_date
    if terms.putback_price is not None:
        kwargs["putback_price"] = float(terms.putback_price)
    if terms.put_obs_months is not None and issue_dt and maturity_dt:
        total_months = (maturity_dt - issue_dt).days / 30.4375
        active_years = max(0, (total_months - float(terms.put_obs_months)) / 12)
        kwargs["put_active_years"] = int(round(active_years))
    return kwargs, issue_dt, maturity_dt


def _terms_source_diagnostic(provider: DataProvider, bond_code: str, valuation_date: date) -> dict:
    describe = getattr(provider, "get_terms_source_diagnostics", None)
    if callable(describe):
        try:
            diag = describe(bond_code, valuation_date)
            if isinstance(diag, dict):
                return diag
        except Exception:
            pass
    return {
        "bond_code": bond_code,
        "valuation_date": valuation_date,
        "terms_source": "provider",
        "snapshot_date": None,
        "patch_count": 0,
        "event_count": 0,
        "uses_current_fallback": False,
    }
