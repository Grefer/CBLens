"""
可转债历史回测模块.

通过 DataProvider 抽象拉取数据, 支持 Wind / akshare / CSV 等任意后端.
"""
import bisect
import logging
from collections import Counter
import numpy as np
from datetime import date, timedelta

from .pricer import UniversalCBPricer
from .data_providers import DataProvider, WindDataProvider, BondTerms
from .down_reset_overrides import resolve_down_reset, resolve_down_reset_intensity
from .historical_terms import (
    HistoricalBondDataProvider, TermsPatchStore, project_terms_patches_path)
from .cb_events import CBEventStore, project_events_path
from .pricing_api import (
    build_pricer_kwargs, _estimate_down_reset_floor, _rating_spread_floor)

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
    point_in_time: bool = True,
    **pricer_overrides,
):
    """
    对历史时间区间内每个采样日逐点计算理论价, 返回与转债实际收盘价的对比序列.

    条款按采样日 ``valuation_date`` 逐点从 provider 读取, 并**默认**在外面包一层
    HistoricalBondDataProvider (patch + 公告事件投影), 与策略回测同口径。传进来的
    provider 若本身就是那一层则原样使用。定价口径 (强赎截断 / 回售关闭 / 评级利差
    下限 / 下修价下限) 与批量页共用 `pricing_api.build_pricer_kwargs`。
    正股 S0 与滚动 σ 取历史值。

    参数:
        provider: DataProvider 实例 (Wind/akshare/CSV); 默认 WindDataProvider
        point_in_time: 是否自动叠加历史条款投影层。**只在你确知传进来的 provider
            已经是逐日口径、或刻意要看"用今天条款回看历史"时才关掉** —— 关掉等于
            让每个历史采样日都拿到今天的转股价。
        freq: "D"/"W"/"M" 采样频率
        solve_iv: True 时逐点反解 IV (耗时 ~5x)
        progress_cb: callable(i, total) 用于 UI 进度反馈
        返回 dict: {dates, theo_prices, market_prices, stock_prices, sigmas,
                  bond_floors, parities, ivs, conversion_prices,
                  terms_source_diagnostics, bond_code, stock_code}
    """
    if provider is None:
        provider = WindDataProvider()
    if point_in_time and not isinstance(provider, HistoricalBondDataProvider):
        # 「逐点读取条款」此前只是**注释里的承诺**: GUI 回测页传进来的是
        # `CachedBondDataProvider`, 它的 `get_bond_terms(code, val_date)` 根本不看
        # val_date, 一律回今天那一行 —— 于是每个历史采样日都用**下修之后**的 K 定价,
        # 而这一页的全部用途就是看模型偏差随时间怎么走。实测 12 个月度采样点上,
        # 全库 322/1059 只 (30.4%) 至少有一个采样日 K 用错, 采样点口径 21.4% 用的是
        # 未来的 K, 最大偏离 115% (128119.SZ: 4.20 vs 当日 1.95)。
        # 策略页早就在外面包了这一层 (strategy_run.py), 单债页漏了。
        provider = HistoricalBondDataProvider(
            provider,
            history_store=None,
            patch_store=TermsPatchStore(project_terms_patches_path()),
            event_store=CBEventStore(project_events_path()),
        )

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
    iv_failures: Counter = Counter()
    for i, (val_date, market_px) in enumerate(sample_points):
        try:
            terms = provider.get_bond_terms(bond_code, val_date)
            point_kwargs, issue_dt, maturity_dt = _build_backtest_pricer_kwargs(
                bond_code,
                terms,
                cf,
                val_date,
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
            # 下修价下限: 与 `price_from_provider` 同口径 (近 20 交易日均价 vs 前收)。
            # 估不出来时 pricer 走无下限分支, 下修价值偏高 —— 与批量页一致地静默回落,
            # 但这里连"估不出来"都不该悄悄发生在**只有回测页缺这一步**的情况下。
            if "down_reset_floor" not in pricer_overrides:
                dr_floor = _estimate_down_reset_floor(provider, stock_code, val_date)
                if dr_floor is not None:
                    point_kwargs["down_reset_floor"] = dr_floor
            point_kwargs.update(pricer_overrides)
            down_intensity = resolve_down_reset_intensity(
                p_down, resolved_down_reset,
                current_k=getattr(terms, "conversion_price", None),
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

            # 评级信用利差下限: 陈旧/偏低的 base_spread 会系统性高估困境债的理论价,
            # 批量页一直有这道闸而回测页没有 —— 实测全库 532/1059 只的评级下限高于
            # 回测页默认的 0.03 (A- 是 0.08, 差 2.7 倍)。两页必须同口径, 否则
            # "回测说模型准" 与 "批量页说模型贵" 可以同时成立而没人看得出为什么。
            rating_floor = _rating_spread_floor(terms.credit_rating)
            point_base_spread = float(base_spread)
            if rating_floor is not None:
                point_base_spread = max(point_base_spread, float(rating_floor))
            pricer = UniversalCBPricer(
                S0=S0, current_date=val_date, **point_kwargs)  # type: ignore[arg-type]
            theo = pricer.price(sigma=sigma, r=r, q=q, base_spread=point_base_spread,
                                distress_k=distress_k, p_down=effective_p_down, M=M, N=N)
        except Exception as exc:
            logger.debug("回测采样日 %s 定价失败: %s", val_date, exc)
            continue

        bond_floor = float(pricer.bond_floor_value(val_date, r + point_base_spread))
        parity = float(S0 * pricer.ratio)

        iv_val = float("nan")
        if solve_iv and market_px is not None and market_px > 0:
            # 反解失败要**记账**: 落在模型可解带之外的天数被丢掉是**单向**的 ——
            # 市价高于 σ=200% 那一端 (`above_ceiling`) 恰恰是"贵"的那些天, 全丢掉之后
            # 剩下的均值只代表便宜的那一半。实测 8 天里 4 天出带, 界面照常报
            # 「IV-HV +25.16pp」而不说少了一半样本。
            bracket: dict = {}
            try:
                iv_val = float(pricer.solve_implied_vol(
                    target_price=float(market_px), r=r, base_spread=point_base_spread,
                    p_down=effective_p_down, distress_k=distress_k,
                    M=iv_M, N=iv_N, q=q, bracket_out=bracket))
            except Exception as exc:
                logger.debug("回测采样日 %s IV 反解失败: %s", val_date, exc)
                bracket.setdefault("reason", "pricing_failed")
            if not np.isfinite(iv_val):
                iv_failures[bracket.get("reason") or "unknown"] += 1

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
        # IV 反解失败的天数, 按原因分档 (above_ceiling / below_floor / pricing_failed /
        # solver_failed)。空 dict = 一天都没丢。消费方要把它显示出来 —— 丢样本是单向的。
        "iv_failures": dict(iv_failures),
        "bond_code": bond_code,
        "stock_code": stock_code,
    }


def _build_backtest_pricer_kwargs(
    bond_code: str,
    terms: BondTerms,
    cf,
    val_date: date,
) -> tuple[dict, date | None, date]:
    """按估值日条款构建 UniversalCBPricer 参数.

    **口径由 `pricing_api.build_pricer_kwargs` 说了算, 这里不再自己写一份。**
    此前这个函数是那段的手抄副本, 抄漏了两处, 于是同一只债同一天回测页与批量页
    给出不同的理论价: 已公告强赎完全没处理 (全库 541 只带 `call_redemption_date`),
    以及 `put_trigger_ratio` 缺值不显式关掉 → 给 69 只本来没有回售条款的债凭空造
    一个回售权。
    """
    kwargs, meta = build_pricer_kwargs(
        bond_code, terms, cf, S0=0.0, valuation_date=val_date)
    # S0 / current_date 由调用方逐采样日填 (每个点的股价和日期都不同)
    kwargs.pop("S0", None)
    kwargs.pop("current_date", None)
    return kwargs, meta["issue_date"], meta["maturity_date"]


def _terms_source_diagnostic(provider: DataProvider, bond_code: str, valuation_date: date) -> dict:
    describe = getattr(provider, "get_terms_source_diagnostics", None)
    if callable(describe):
        try:
            diag = describe(bond_code, valuation_date)
            if isinstance(diag, dict):
                return diag
        except Exception:
            logger.debug("get_terms_source_diagnostics(%s) 失败, 回落默认诊断",
                         bond_code, exc_info=True)
    return {
        "bond_code": bond_code,
        "valuation_date": valuation_date,
        "terms_source": "provider",
        "snapshot_date": None,
        "patch_count": 0,
        "event_count": 0,
        "uses_current_fallback": False,
    }
