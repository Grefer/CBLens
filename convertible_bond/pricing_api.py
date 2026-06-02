"""
DataProvider 驱动的可转债定价辅助接口.

该模块承接原先位于 CB.py 的 provider-backed helper:
  - price_from_provider
  - price_from_wind
  - batch_price_from_provider

新代码应直接 import 本模块; CB.py 仅保留向后兼容 re-export.
"""
from datetime import date, timedelta
import os
from typing import Any
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np

from .pricer import (
    UniversalCBPricer,
    DEFAULT_COUPON_RATES,
    DEFAULT_FACE_VALUE,
    DEFAULT_REDEMPTION_PRICE,
)
from .data_providers import DataProvider, WindDataProvider, auto_data_provider, finite_float
from .cache import CachedBondDataProvider, TermsBundle, project_bundle_path
from .down_reset_overrides import resolve_down_reset, resolve_down_reset_intensity
from .historical_terms import TermsPatchStore, project_terms
from .model_defaults import DEFAULT_DOWN_RESET_TRIGGER_PCT, DEFAULT_DOWN_RESET_TRIGGER_RATIO
from .dateutil import add_years as _add_years


_finite_float = finite_float

_RATING_SPREAD_FLOORS = {
    "AAA": 0.012,
    "AA+": 0.018,
    "AA": 0.025,
    "AA-": 0.035,
    "A+": 0.045,
    "A": 0.060,
    "A-": 0.080,
    "BBB+": 0.100,
    "BBB": 0.120,
    "BBB-": 0.150,
    "BB+": 0.180,
    "BB": 0.220,
    "BB-": 0.260,
    "B+": 0.300,
    "B": 0.360,
    "B-": 0.420,
    "CCC": 0.500,
    "CC": 0.650,
    "C": 0.800,
}

def _latest_price_on_or_before(history, on_date: date) -> float | None:
    latest: float | None = None
    latest_date: date | None = None
    for d, value in history or []:
        if d is None or d > on_date:
            continue
        price = _finite_float(value)
        if price is None:
            continue
        if latest_date is None or d >= latest_date:
            latest_date = d
            latest = price
    return latest


def _latest_bond_close(provider: DataProvider, bond_code: str, val_date: date, fallback) -> float | None:
    fallback_price = _finite_float(fallback)
    try:
        history = provider.get_bond_history(
            bond_code,
            val_date - timedelta(days=15),
            val_date,
        )
    except Exception:
        return fallback_price
    return _latest_price_on_or_before(history, val_date) or fallback_price


def _rating_spread_floor(rating: Any) -> float | None:
    if rating is None:
        return None
    raw = str(rating).upper().replace(" ", "").strip()
    for label in sorted(_RATING_SPREAD_FLOORS, key=len, reverse=True):
        if raw == label or raw.startswith(label):
            return _RATING_SPREAD_FLOORS[label]
    return None


def _accrued_interest(
    *,
    face_value: float,
    coupon_rates: tuple[float, ...] | None,
    issue_date: date | None,
    on_date: date,
) -> float:
    if issue_date is None or on_date <= issue_date:
        return 0.0
    period_start = issue_date
    for rate in tuple(coupon_rates or DEFAULT_COUPON_RATES):
        period_end = _add_years(period_start, 1)
        if period_start <= on_date <= period_end:
            return face_value * float(rate) * (on_date - period_start).days / 365.0
        period_start = period_end
    return 0.0


def _estimate_down_reset_floor(provider: DataProvider, stock_code: str, val_date: date) -> float | None:
    """估算下修价监管/条款下限: max(近20交易日均价, 前一交易日均价)."""
    try:
        history = provider.get_stock_history(stock_code, val_date - timedelta(days=60), val_date)
    except Exception:
        return None
    closes = [
        float(v)
        for d, v in history or []
        if d is not None and d <= val_date and _finite_float(v) is not None
    ]
    if len(closes) < 20:
        return None
    last20_avg = sum(closes[-20:]) / 20.0
    prev_close = closes[-1]
    return max(last20_avg, prev_close)


def _risk_warnings(terms, val_date: date) -> list[str]:
    warnings: list[str] = []
    if terms.suspension_status:
        warnings.append(f"转债交易状态异常: {terms.suspension_status}")
    conv_status = str(getattr(terms, "conversion_suspension_status", "") or "")
    conv_start = getattr(terms, "conversion_suspension_start_date", None)
    conv_end = getattr(terms, "conversion_suspension_end_date", None)
    conversion_paused = "暂停" in conv_status or (
        conv_start is not None
        and conv_start <= val_date
        and (conv_end is None or val_date <= conv_end)
    )
    if conversion_paused:
        start_text = conv_start.isoformat() if conv_start else "待起始"
        end_text = conv_end.isoformat() if conv_end else "待恢复"
        warnings.append(f"转股暂停窗口: {start_text}~{end_text}")
    if terms.underlying_trade_status:
        warnings.append(f"正股交易状态异常: {terms.underlying_trade_status}")
    if terms.underlying_status:
        warnings.append(f"正股风险状态: {terms.underlying_status}")
    outlook = str(getattr(terms, "credit_rating_outlook", "") or "").strip()
    if outlook and outlook not in {"稳定", "正面"}:
        warnings.append(f"评级展望: {outlook}")
    watch_status = str(getattr(terms, "credit_watch_status", "") or "").strip()
    if watch_status and not any(word in watch_status for word in ("撤出", "移出", "取消")):
        warnings.append(f"评级观察: {watch_status}")
    if terms.call_redemption_date is not None:
        if terms.call_redemption_date <= val_date:
            warnings.append("强赎赎回日已过, 普通理论价不适用")
        else:
            warnings.append("已公告强赎, 估值切换为短期限赎回视角")
    if terms.last_trading_date is not None and terms.last_trading_date < val_date:
        warnings.append("已过最后交易日, 市价/偏差参考意义有限")
    if terms.delisting_date is not None:
        if terms.delisting_date <= val_date:
            warnings.append("已摘牌或临近摘牌状态已过期")
        elif (terms.delisting_date - val_date).days <= 30:
            warnings.append("30日内临近摘牌")
    return warnings


def _model_signal_status(terms, sigma: float | None, risk_warnings: list[str]) -> str:
    hard_risk = False
    if risk_warnings:
        hard_risk = any(
            any(token in text for token in ("ST", "退市", "停牌", "暂停", "摘牌", "最后交易"))
            for text in risk_warnings
        )
    rating = str(getattr(terms, "credit_rating", "") or "").upper().replace(" ", "").strip()
    if rating:
        if not (rating.startswith("AAA") or rating.startswith("AA")):
            hard_risk = True
    balance = _finite_float(getattr(terms, "outstanding_balance", None))
    if balance is not None and balance < 0.5:
        hard_risk = True
    vol = _finite_float(sigma)
    if vol is not None and vol > 0.80:
        hard_risk = True
    return "不适合作为买入信号" if hard_risk else "可作为模型信号复核"


def _assert_pricing_status_active(
    bond_code: str,
    terms,
    val_date: date,
    *,
    maturity_date: date | None = None,
) -> None:
    if terms.call_redemption_date is not None and terms.call_redemption_date <= val_date:
        raise ValueError(
            f"{bond_code} 已到/已过强赎赎回日 ({terms.call_redemption_date}), 普通理论价不适用")
    if terms.last_trading_date is not None and terms.last_trading_date < val_date:
        raise ValueError(
            f"{bond_code} 已过最后交易日 ({terms.last_trading_date}), 普通理论价不适用")
    if terms.delisting_date is not None and terms.delisting_date <= val_date:
        raise ValueError(
            f"{bond_code} 已摘牌/退市 ({terms.delisting_date}), 普通理论价不适用")
    if maturity_date is not None and maturity_date <= val_date:
        raise ValueError(
            f"{bond_code} 已到期 ({maturity_date}), 普通理论价不适用")


def cb_data_provider_for_market(market_provider: DataProvider) -> DataProvider:
    """用 cb_data 提供转债静态信息, 用 market_provider 提供动态行情/利率."""
    static_source = market_provider if isinstance(market_provider, WindDataProvider) else None
    return CachedBondDataProvider(
        market_provider,
        TermsBundle(project_bundle_path()),
        static_source=static_source,
        max_age_days=365,
    )


def price_from_provider(provider: DataProvider, bond_code,
                        r=0.022, base_spread=0.03,
                        distress_k=0.05, p_down=0.15,
                        valuation_date=None, vol_window_days=21,
                        sigma=None, q=None,
                        M=500, N=2000,
                        term_patch_store: TermsPatchStore | None = None,
                        apply_term_events: bool = True,
                        use_rating_spread: bool = True,
                        estimate_down_reset_floor: bool = True,
                        **pricer_overrides):
    """
    输入转债代码 (例如 '128009.SZ') + 一个 DataProvider 实例, 自动拉参数并定价.

    σ 默认为正股最近 vol_window_days 个交易日的年化历史波动率;
    q 默认从 provider.get_stock_dividend_yield() 读取 (返回百分数), 缺失时回退 0;
    如需覆盖直接传 sigma=0.30 或其他 pricer kwarg (K/maturity_date/...).
    """
    val_date = valuation_date or date.today()
    terms = provider.get_bond_terms(bond_code, val_date)
    projection = project_terms(
        bond_code,
        terms,
        val_date,
        patch_store=term_patch_store,
        apply_events=apply_term_events,
    )
    terms = projection.terms

    _assert_pricing_status_active(
        bond_code,
        terms,
        val_date,
        maturity_date=terms.maturity_date,
    )

    stock_code = terms.underlying_code
    if not stock_code:
        raise ValueError(f"{bond_code} 数据源未返回标的正股代码")

    if sigma is None:
        sigma = provider.hist_vol(stock_code, val_date, vol_window_days)
    S0 = provider.get_stock_close(stock_code, val_date)

    # 校验 S0 / sigma: provider 可能返回 None/NaN/0, 提前拦截避免 pricer 报错不清晰
    S0 = _finite_float(S0)
    if S0 is None or S0 <= 0:
        raise ValueError(f"{bond_code} 正股价无效 (S0={S0!r}), 无法定价")
    sigma = _finite_float(sigma)
    if sigma is None or sigma <= 0:
        raise ValueError(f"{bond_code} 波动率无效 (sigma={sigma!r}), 无法定价")

    if q is None:
        try:
            q_pct = _finite_float(provider.get_stock_dividend_yield(stock_code, val_date))
        except Exception:
            q_pct = None
        effective_q = (q_pct / 100.0) if q_pct is not None else 0.0
    else:
        effective_q = float(q)
    market_price = _latest_bond_close(provider, bond_code, val_date, terms.close)
    risk_warnings = _risk_warnings(terms, val_date)

    issue_dt = terms.issue_date
    conv_start_dt = issue_dt + timedelta(days=180) if issue_dt else None

    cf = provider.get_cashflow(bond_code)
    if cf and cf.coupon_rates:
        coupon_rates = cf.coupon_rates
    else:
        coupon_rates = terms.coupon_rates

    if cf and cf.maturity_date:
        maturity_dt = cf.maturity_date
    else:
        maturity_dt = terms.maturity_date
    contractual_maturity_dt = maturity_dt
    if maturity_dt is None:
        raise ValueError(f"{bond_code} 数据源未返回到期日, 无法定价")
    _assert_pricing_status_active(
        bond_code,
        terms,
        val_date,
        maturity_date=maturity_dt,
    )

    if cf and cf.redemption_price is not None:
        redemption_price = float(cf.redemption_price)
    elif terms.redemption_price is not None:
        redemption_price = float(terms.redemption_price)
    else:
        redemption_price = DEFAULT_REDEMPTION_PRICE

    if terms.conversion_price is None:
        raise ValueError(f"{bond_code} 数据源未返回转股价 K, 无法定价")

    redemption_mode = False
    if terms.call_redemption_date is not None:
        redemption_mode = True
        maturity_dt = terms.call_redemption_date
        if terms.call_redemption_price is not None:
            redemption_price = float(terms.call_redemption_price)
        else:
            face_value_for_call = float(terms.face_value or DEFAULT_FACE_VALUE)
            redemption_price = face_value_for_call + _accrued_interest(
                face_value=face_value_for_call,
                coupon_rates=coupon_rates,
                issue_date=issue_dt,
                on_date=terms.call_redemption_date,
            )

    pricer_kwargs = dict(
        S0=S0,
        K=float(terms.conversion_price),
        face_value=float(terms.face_value or DEFAULT_FACE_VALUE),
        current_date=val_date,
        maturity_date=maturity_dt,
        issue_date=issue_dt,
        conversion_start_date=conv_start_dt,
        redemption_price=float(redemption_price),
        coupon_rates=coupon_rates,
    )
    if redemption_mode:
        # 已公告强赎时, 终点已经是赎回/转股二择一, 不再套用普通触发式强赎 cap。
        pricer_kwargs["call_no_redemption_until"] = maturity_dt
    down_reset_trigger_source = "terms"
    if terms.down_reset_trigger_pct is None:
        down_reset_trigger_pct = DEFAULT_DOWN_RESET_TRIGGER_PCT
        down_reset_trigger_source = "default"
    else:
        down_reset_trigger_pct = float(terms.down_reset_trigger_pct)
    pricer_kwargs["down_reset_trigger_ratio"] = down_reset_trigger_pct / 100.0
    if terms.call_trigger_pct is not None:
        pricer_kwargs["call_trigger_ratio"] = float(terms.call_trigger_pct) / 100.0
    if terms.call_no_redemption_until is not None and not redemption_mode:
        pricer_kwargs["call_no_redemption_until"] = terms.call_no_redemption_until
    if terms.put_trigger_pct is not None:
        pricer_kwargs["put_trigger_ratio"] = float(terms.put_trigger_pct) / 100.0
    if terms.putback_start_date is not None:
        pricer_kwargs["putback_start_date"] = terms.putback_start_date
    if terms.putback_end_date is not None:
        pricer_kwargs["putback_end_date"] = terms.putback_end_date
    if terms.putback_price is not None:
        pricer_kwargs["putback_price"] = float(terms.putback_price)

    if terms.put_obs_months is not None and issue_dt and (contractual_maturity_dt or maturity_dt):
        put_maturity = contractual_maturity_dt or maturity_dt
        total_months = (put_maturity - issue_dt).days / 30.4375
        active_years = max(0, (total_months - float(terms.put_obs_months)) / 12)
        pricer_kwargs["put_active_years"] = int(round(active_years))
    resolved = resolve_down_reset(bond_code, terms, valuation_date=val_date)
    if resolved.block_until is not None:
        pricer_kwargs["down_reset_block_until"] = resolved.block_until
    down_reset_floor = None
    if estimate_down_reset_floor and "down_reset_floor" not in pricer_overrides:
        down_reset_floor = _estimate_down_reset_floor(provider, stock_code, val_date)
        if down_reset_floor is not None:
            pricer_kwargs["down_reset_floor"] = down_reset_floor

    down_intensity = resolve_down_reset_intensity(
        p_down, resolved, redemption_mode=redemption_mode,
    )
    effective_p_down = down_intensity.effective_p_down
    # 已公告下修: 把一次性下修节点传入 pricer (regime ②); 显式 override 优先。
    if (
        down_intensity.scheduled_reset_date is not None
        and down_intensity.scheduled_reset_prob > 0
    ):
        pricer_kwargs.setdefault("scheduled_reset_date", down_intensity.scheduled_reset_date)
        pricer_kwargs.setdefault("scheduled_reset_prob", down_intensity.scheduled_reset_prob)
        if down_intensity.scheduled_reset_target_k is not None:
            pricer_kwargs.setdefault(
                "scheduled_reset_target_k", down_intensity.scheduled_reset_target_k)

    pricer_kwargs.update(pricer_overrides)
    if "down_reset_trigger_ratio" in pricer_overrides:
        down_reset_trigger_source = "override"
    down_reset_trigger_ratio = float(
        pricer_kwargs.get("down_reset_trigger_ratio", DEFAULT_DOWN_RESET_TRIGGER_RATIO)
    )
    down_reset_trigger_pct = down_reset_trigger_ratio * 100.0
    pricer = UniversalCBPricer(**pricer_kwargs)  # type: ignore[arg-type]

    rating_base_spread = _rating_spread_floor(terms.credit_rating)
    effective_base_spread = float(base_spread)
    if use_rating_spread and rating_base_spread is not None:
        effective_base_spread = max(effective_base_spread, float(rating_base_spread))

    theo = pricer.price(sigma=sigma, r=r, base_spread=effective_base_spread,
                        distress_k=distress_k, p_down=effective_p_down,
                        M=M, N=N, q=effective_q)
    has_down_value = (
        effective_p_down > 0
        or float(pricer_kwargs.get("scheduled_reset_prob", 0.0) or 0.0) > 0
    )
    if has_down_value:
        no_down_kwargs = dict(pricer_kwargs)
        no_down_kwargs.pop("scheduled_reset_date", None)
        no_down_kwargs.pop("scheduled_reset_target_k", None)
        no_down_kwargs["scheduled_reset_prob"] = 0.0
        no_down_pricer = UniversalCBPricer(**no_down_kwargs)  # type: ignore[arg-type]
        no_down_price = no_down_pricer.price(
            sigma=sigma, r=r, base_spread=effective_base_spread,
            distress_k=distress_k, p_down=0.0,
            M=M, N=N, q=effective_q,
        )
    else:
        no_down_price = theo
    down_reset_uplift = float(theo) - float(no_down_price)
    model_signal_status = _model_signal_status(terms, sigma, risk_warnings)
    return {
        "bond_code": bond_code,
        "bond_name": terms.sec_name,
        "stock_code": stock_code,
        "valuation_date": val_date,
        "S0": S0,
        "K": pricer.K,
        "T": pricer.T,
        "sigma": sigma,
        "q": effective_q,
        "base_spread": float(base_spread),
        "effective_base_spread": effective_base_spread,
        "rating_base_spread": rating_base_spread,
        "base_p_down": down_intensity.base_p_down,
        "effective_p_down": effective_p_down,
        # Backward-compatible alias: historically this field held the model value.
        "p_down": effective_p_down,
        "down_reset_block_until": resolved.block_until,
        "down_reset_p_scale": resolved.p_scale,
        "down_reset_note": resolved.note,
        "down_reset_cooldown_months": resolved.cooldown_months,
        "down_reset_announce_date": resolved.announce_date,
        "down_reset_proposed_date": getattr(resolved, "proposal_date", None),
        "down_reset_approved_effective_date": getattr(resolved, "approved_effective_date", None),
        "down_reset_scheduled_date": down_intensity.scheduled_reset_date,
        "down_reset_scheduled_prob": down_intensity.scheduled_reset_prob,
        "down_reset_scheduled_kind": down_intensity.scheduled_reset_kind,
        "down_reset_scheduled_target_k": down_intensity.scheduled_reset_target_k,
        "down_reset_trigger_pct": down_reset_trigger_pct,
        "down_reset_trigger_ratio": down_reset_trigger_ratio,
        "down_reset_trigger_source": down_reset_trigger_source,
        "down_reset_floor": pricer_kwargs.get("down_reset_floor"),
        "no_down_price": no_down_price,
        "down_reset_uplift": down_reset_uplift,
        "down_reset_uplift_pct": (down_reset_uplift / theo) if theo else float("nan"),
        "redemption_mode": redemption_mode,
        "call_status": terms.call_status,
        "call_redemption_date": terms.call_redemption_date,
        "last_trading_date": terms.last_trading_date,
        "delisting_date": terms.delisting_date,
        "maturity_date": maturity_dt,
        "contractual_maturity_date": contractual_maturity_dt,
        "redemption_price": pricer_kwargs.get("redemption_price", redemption_price),
        "call_redemption_price": terms.call_redemption_price,
        "putback_start_date": terms.putback_start_date,
        "putback_end_date": terms.putback_end_date,
        "putback_price": terms.putback_price,
        "market_price": market_price,
        "credit_rating": terms.credit_rating,
        "credit_rating_outlook": terms.credit_rating_outlook,
        "credit_watch_status": terms.credit_watch_status,
        "outstanding_balance": terms.outstanding_balance,
        "conversion_suspension_start_date": terms.conversion_suspension_start_date,
        "conversion_suspension_end_date": terms.conversion_suspension_end_date,
        "conversion_suspension_status": terms.conversion_suspension_status,
        "listing_date": terms.listing_date,
        "tradable_date": terms.tradable_date,
        "is_tradable": terms.is_tradable,
        "trading_status": terms.trading_status,
        "suspension_status": terms.suspension_status,
        "underlying_name": terms.underlying_name,
        "underlying_status": terms.underlying_status,
        "underlying_trade_status": terms.underlying_trade_status,
        "underlying_pct_change": terms.underlying_pct_change,
        "call_no_redemption_until": terms.call_no_redemption_until,
        "term_patch_fields": sorted(projection.patch_fields),
        "term_patch_count": len(projection.applied_patches),
        "risk_warnings": risk_warnings,
        "model_signal_status": model_signal_status,
        "coupon_source": "cashflow" if cf and cf.coupon_rates else "terms",
        "theoretical_price": theo,
        "data_source": provider.name,
    }


def price_from_wind(bond_code, **kwargs):
    """便捷封装: cb_data 静态信息 + Wind 动态行情."""
    return price_from_provider(cb_data_provider_for_market(WindDataProvider()), bond_code, **kwargs)


def price_from_auto(bond_code, *, prefer=None, **kwargs):
    """便捷封装: 自动挑选动态行情源 (Wind > akshare), 静态信息仍走 cb_data/Wind."""
    return price_from_provider(cb_data_provider_for_market(auto_data_provider(prefer=prefer)), bond_code, **kwargs)


def _resolve_batch_workers(max_workers: int | None, total: int) -> int:
    if total <= 0:
        return 1
    if max_workers is None:
        # GUI 批量定价同时包含数据读取与 NumPy PDE 求解; 默认给到一个温和的自动并发。
        max_workers = min(8, max(2, os.cpu_count() or 4))
    return max(1, min(int(max_workers), total))


class _BatchStockCache(DataProvider):
    """装饰器: 批量定价期间缓存正股级数据, 避免同一正股重复发网络请求.

    在批量定价场景中, 同一只正股可能被多只转债引用 (如 A、B 两只转债对应
    同一只正股). 此外, ``price_from_provider`` 对每只债先调 ``get_stock_close``
    再调 ``hist_vol`` (内部又调 ``get_stock_history``), 导致同一正股的历史数据
    被拉取两次.

    本装饰器在一次 batch run 的生命周期内:
      - get_stock_close(stock, date) → 按 (stock, date) 缓存
      - get_stock_history(stock, start, end) → 按 (stock, start, end) 缓存
      - get_stock_dividend_yield(stock, date) → 按 (stock, date) 缓存
      - hist_vol(stock, end_date, window) → 按 (stock, end_date, window) 缓存

    线程安全 (多线程并发定价时不会重复请求, 先到的线程写入, 后到的直接读缓存).
    """
    _INFLIGHT_TIMEOUT = 30.0
    _MAX_INFLIGHT_RETRIES = 2

    def __init__(self, inner: DataProvider):
        self._inner = inner
        self.name = inner.name
        self._close_cache: dict[tuple, float] = {}
        self._history_cache: dict[tuple, list] = {}
        self._bond_history_cache: dict[tuple, list] = {}
        self._vol_cache: dict[tuple, float] = {}
        self._dividend_yield_cache: dict[tuple, float | None] = {}
        import threading
        self._lock = threading.Lock()
        self._inflight: dict[tuple, "threading.Event"] = {}

    # ── inflight-event 辅助 ────────────────────────────────
    def _inflight_try_acquire(self, inflight_key: tuple, cache_dict: dict, cache_key: tuple):
        """尝试获取 inflight 所有权. 返回 (is_owner, cache_hit, value_or_event).

        已命中缓存 → (True, True, value); 非 owner → (False, False, event);
        owner → (True, False, new_event).
        """
        import threading
        with self._lock:
            if cache_key in cache_dict:
                return True, True, cache_dict[cache_key]
            event = self._inflight.get(inflight_key)
            if event is not None:
                return False, False, event
            event = self._inflight[inflight_key] = threading.Event()
            return True, False, event

    def _inflight_release(self, inflight_key: tuple):
        with self._lock:
            ev = self._inflight.pop(inflight_key, None)
        if ev is not None:
            ev.set()

    def _inflight_wait_for(self, event, inflight_key: tuple) -> None:
        """等待 inflight event, 超时直接暴露数据源卡顿而不是返回 None."""
        if not event.wait(timeout=self._INFLIGHT_TIMEOUT):
            raise TimeoutError(f"{self.name} 批量缓存等待超时: {inflight_key!r}")

    # ── 缓存的接口 ────────────────────────────────────────
    def get_stock_close(self, stock_code, on_date):
        cache_key = (stock_code, on_date)
        inflight_key = ("close", stock_code, on_date)
        for _ in range(self._MAX_INFLIGHT_RETRIES + 1):
            is_owner, hit, value_or_ev = self._inflight_try_acquire(
                inflight_key, self._close_cache, cache_key)
            if hit:
                return value_or_ev
            if not is_owner:
                self._inflight_wait_for(value_or_ev, inflight_key)
                with self._lock:
                    if cache_key in self._close_cache:
                        return self._close_cache[cache_key]
                continue
            try:
                value = self._inner.get_stock_close(stock_code, on_date)
                with self._lock:
                    self._close_cache[cache_key] = value
                return value
            finally:
                self._inflight_release(inflight_key)
        raise RuntimeError(f"{stock_code} {on_date} 正股价缓存填充失败")

    def get_stock_history(self, stock_code, start, end):
        cache_key = (stock_code, start, end)
        inflight_key = ("history", stock_code, start, end)
        for _ in range(self._MAX_INFLIGHT_RETRIES + 1):
            is_owner, hit, value_or_ev = self._inflight_try_acquire(
                inflight_key, self._history_cache, cache_key)
            if hit:
                return value_or_ev
            if not is_owner:
                self._inflight_wait_for(value_or_ev, inflight_key)
                with self._lock:
                    if cache_key in self._history_cache:
                        return self._history_cache[cache_key]
                continue
            try:
                value = self._inner.get_stock_history(stock_code, start, end)
                with self._lock:
                    self._history_cache[cache_key] = value
                return value
            finally:
                self._inflight_release(inflight_key)
        raise RuntimeError(f"{stock_code} {start}~{end} 正股历史缓存填充失败")

    def get_stock_dividend_yield(self, stock_code, on_date):
        cache_key = (stock_code, on_date)
        inflight_key = ("div_yield", stock_code, on_date)
        for _ in range(self._MAX_INFLIGHT_RETRIES + 1):
            is_owner, hit, value_or_ev = self._inflight_try_acquire(
                inflight_key, self._dividend_yield_cache, cache_key)
            if hit:
                return value_or_ev
            if not is_owner:
                self._inflight_wait_for(value_or_ev, inflight_key)
                with self._lock:
                    if cache_key in self._dividend_yield_cache:
                        return self._dividend_yield_cache[cache_key]
                continue
            try:
                getter = getattr(self._inner, "get_stock_dividend_yield", None)
                value = getter(stock_code, on_date) if getter is not None else None
                with self._lock:
                    self._dividend_yield_cache[cache_key] = value
                return value
            finally:
                self._inflight_release(inflight_key)
        raise RuntimeError(f"{stock_code} {on_date} 股息率缓存填充失败")

    def hist_vol(self, stock_code, end_date, window_days):
        cache_key = (stock_code, end_date, window_days)
        inflight_key = ("vol", *cache_key)
        _MAX_RETRIES = 2
        for attempt in range(_MAX_RETRIES + 1):
            is_owner, hit, value_or_ev = self._inflight_try_acquire(
                inflight_key, self._vol_cache, cache_key)
            if hit:
                return value_or_ev
            if not is_owner:
                self._inflight_wait_for(value_or_ev, inflight_key)
                # owner 可能已完成 (cache hit) 或失败 (inflight 已清除, 需重试)
                with self._lock:
                    if cache_key in self._vol_cache:
                        return self._vol_cache[cache_key]
                continue  # owner 失败, 下次循环重新尝试获取 ownership
            # 本线程是 owner, 执行计算
            try:
                lookback = max(window_days * 2, window_days + 15)
                history = self.get_stock_history(
                    stock_code, end_date - timedelta(days=lookback), end_date)
                closes = np.array([v for _, v in history if v is not None], dtype=float)
                if len(closes) > window_days + 1:
                    closes = closes[-(window_days + 1):]
                if len(closes) < 5:
                    raise ValueError(f"{stock_code} 历史样本仅 {len(closes)} 条, 无法估算波动率")
                log_ret = np.diff(np.log(closes))
                value = float(np.std(log_ret, ddof=1) * np.sqrt(252))
                latest_close = _latest_price_on_or_before(history, end_date)
                with self._lock:
                    self._vol_cache[cache_key] = value
                    if latest_close is not None:
                        self._close_cache.setdefault((stock_code, end_date), latest_close)
                return value
            except Exception:
                if attempt == _MAX_RETRIES:
                    raise
            finally:
                self._inflight_release(inflight_key)
        raise RuntimeError("hist_vol: unreachable")

    # ── 直接透传的接口 ────────────────────────────────────
    def get_bond_terms(self, bond_code, valuation_date):
        return self._inner.get_bond_terms(bond_code, valuation_date)

    def get_bond_history(self, bond_code, start, end):
        cache_key = (bond_code, start, end)
        inflight_key = ("bond_history", *cache_key)
        for _ in range(self._MAX_INFLIGHT_RETRIES + 1):
            is_owner, hit, value_or_ev = self._inflight_try_acquire(
                inflight_key, self._bond_history_cache, cache_key)
            if hit:
                return value_or_ev
            if not is_owner:
                self._inflight_wait_for(value_or_ev, inflight_key)
                with self._lock:
                    if cache_key in self._bond_history_cache:
                        return self._bond_history_cache[cache_key]
                continue
            try:
                value = self._inner.get_bond_history(bond_code, start, end)
                with self._lock:
                    self._bond_history_cache[cache_key] = value
                return value
            finally:
                self._inflight_release(inflight_key)
        raise RuntimeError(f"{bond_code} {start}~{end} 转债历史缓存填充失败")

    def get_cashflow(self, bond_code):
        return self._inner.get_cashflow(bond_code)

    def get_risk_free_rate(self, on_date):
        return self._inner.get_risk_free_rate(on_date)


def _batch_result_from_provider(
    provider: DataProvider,
    code: str,
    *,
    r: float,
    base_spread: float,
    distress_k: float,
    p_down: float,
    valuation_date: date,
    vol_window_days: int,
    sigma: float | None,
    q: float | None,
    M: int,
    N: int,
    pricer_overrides: dict[str, Any],
) -> dict[str, Any]:
    try:
        res = price_from_provider(
            provider, code,
            r=r, base_spread=base_spread,
            distress_k=distress_k, p_down=p_down,
            valuation_date=valuation_date,
            vol_window_days=vol_window_days,
            sigma=sigma, q=q, M=M, N=N,
            **pricer_overrides,
        )
        mkt = res.get("market_price")
        theo = res["theoretical_price"]
        if mkt is not None and theo > 0:
            res["deviation"] = (float(mkt) - theo) / theo
            res["undervaluation_rate"] = -res["deviation"]
        else:
            res["deviation"] = float("nan")
            res["undervaluation_rate"] = float("nan")
        res["status"] = "ok"
        return res
    except Exception as exc:
        return {
            "bond_code": code,
            "status": str(exc),
            "theoretical_price": float("nan"),
            "market_price": None,
            "deviation": float("nan"),
            "undervaluation_rate": float("nan"),
        }


def _sort_batch_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def _key(row: dict[str, Any]) -> float:
        deviation = _finite_float(row.get("deviation"))
        return deviation if deviation is not None else float("inf")

    results.sort(key=_key)
    return results


def batch_price_from_provider_threaded(
    provider: DataProvider,
    bond_codes: list[str],
    *,
    r: float = 0.022,
    base_spread: float = 0.03,
    distress_k: float = 0.05,
    p_down: float = 0.15,
    valuation_date: date | None = None,
    vol_window_days: int = 21,
    sigma: float | None = None,
    q: float | None = None,
    M: int = 300,
    N: int = 1000,
    max_workers: int | None = None,
    progress_cb=None,
    **pricer_overrides,
) -> list[dict[str, Any]]:
    """
    多线程批量定价入口: 自动或显式指定线程数, 供 GUI 批量计算调用.

    与 batch_price_from_provider 参数一致; max_workers=None 时按 CPU 核数自动选择
    一个温和上限, 避免 GUI 大批量计算时固定 4 线程成为瓶颈.

    内部自动启用 _BatchStockCache: 同一正股的现价/历史/波动率只从数据源拉取一次,
    后续引用相同正股的转债直接走内存缓存, 大幅减少网络请求量.
    """
    val_date = valuation_date or date.today()
    codes = list(bond_codes)
    total = len(codes)
    if total == 0:
        return []

    # 批量级正股数据缓存: 同一正股只拉一次, 显著减少网络请求
    cached_provider = _BatchStockCache(provider)
    workers = _resolve_batch_workers(max_workers, total)
    results: list[dict[str, Any]] = []
    done_count = 0

    def _price_one(code: str) -> dict[str, Any]:
        return _batch_result_from_provider(
            cached_provider, code,
            r=r, base_spread=base_spread,
            distress_k=distress_k, p_down=p_down,
            valuation_date=val_date,
            vol_window_days=vol_window_days,
            sigma=sigma, q=q, M=M, N=N,
            pricer_overrides=pricer_overrides,
        )

    if workers == 1:
        for code in codes:
            results.append(_price_one(code))
            done_count += 1
            if progress_cb:
                progress_cb(done_count, total)
        return _sort_batch_results(results)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_price_one, code): code for code in codes}
        for fut in as_completed(futures):
            results.append(fut.result())
            done_count += 1
            if progress_cb:
                progress_cb(done_count, total)

    return _sort_batch_results(results)


def batch_price_from_provider(
    provider: DataProvider,
    bond_codes: list[str],
    *,
    r: float = 0.022,
    base_spread: float = 0.03,
    distress_k: float = 0.05,
    p_down: float = 0.15,
    valuation_date: date | None = None,
    vol_window_days: int = 21,
    sigma: float | None = None,
    q: float | None = None,
    M: int = 300,
    N: int = 1000,
    max_workers: int = 4,
    progress_cb=None,
    **pricer_overrides,
) -> list[dict[str, Any]]:
    """
    批量定价: 导入代码列表 → 并发定价 → 按理论价/市价基差排序返回.

    本入口为 ``batch_price_from_provider_threaded`` 的兼容别名, 仍保留 legacy
    默认 ``max_workers=4``。新代码建议直接调用
    ``batch_price_from_provider_threaded`` (默认按 CPU 核数自动选择并发数)。

    参数:
        bond_codes: 转债代码列表, 例如 ['128009.SZ', '113050.SH']
        max_workers: 并发线程数 (legacy 默认 4)
        progress_cb: callable(done, total) 进度回调
        其余参数同 price_from_provider

    返回: list[dict], 每个 dict 额外包含:
        - "deviation": (市价 - 理论价) / 理论价  (无市价时为 NaN)
        - "status": "ok" | 错误信息
      按 deviation 升序排列 (低估排前面).
    """
    return batch_price_from_provider_threaded(
        provider, bond_codes,
        r=r, base_spread=base_spread,
        distress_k=distress_k, p_down=p_down,
        valuation_date=valuation_date,
        vol_window_days=vol_window_days,
        sigma=sigma, q=q, M=M, N=N,
        max_workers=max_workers,
        progress_cb=progress_cb,
        **pricer_overrides,
    )
