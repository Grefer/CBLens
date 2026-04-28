"""
DataProvider 驱动的可转债定价辅助接口.

该模块承接原先位于 CB.py 的 provider-backed helper:
  - price_from_provider
  - price_from_wind
  - batch_price_from_provider

新代码应直接 import 本模块; CB.py 仅保留向后兼容 re-export.
"""
from datetime import date, timedelta
import math
import os
from typing import List, Optional, Dict, Any
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np

from .pricer import UniversalCBPricer, DEFAULT_FACE_VALUE, DEFAULT_REDEMPTION_PRICE
from .data_providers import DataProvider, WindDataProvider, auto_data_provider
from .cache import CachedBondDataProvider, TermsBundle, project_bundle_path
from .down_reset_overrides import resolve_down_reset


def _finite_float(value) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _latest_price_on_or_before(history, on_date: date) -> Optional[float]:
    latest: Optional[float] = None
    latest_date: Optional[date] = None
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


def _latest_bond_close(provider: DataProvider, bond_code: str, val_date: date, fallback) -> Optional[float]:
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
                        sigma=None,
                        M=500, N=2000,
                        **pricer_overrides):
    """
    输入转债代码 (例如 '128009.SZ') + 一个 DataProvider 实例, 自动拉参数并定价.

    σ 默认为正股最近 vol_window_days 个交易日的年化历史波动率;
    如需覆盖直接传 sigma=0.30 或其他 pricer kwarg (K/maturity_date/...).
    """
    val_date = valuation_date or date.today()
    terms = provider.get_bond_terms(bond_code, val_date)

    stock_code = terms.underlying_code
    if not stock_code:
        raise ValueError(f"{bond_code} 数据源未返回标的正股代码")

    if sigma is None:
        sigma = provider.hist_vol(stock_code, val_date, vol_window_days)
    S0 = provider.get_stock_close(stock_code, val_date)
    market_price = _latest_bond_close(provider, bond_code, val_date, terms.close)

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

    if cf and cf.redemption_price is not None:
        redemption_price = float(cf.redemption_price)
    elif terms.redemption_price is not None:
        redemption_price = float(terms.redemption_price)
    else:
        redemption_price = DEFAULT_REDEMPTION_PRICE

    if terms.conversion_price is None:
        raise ValueError(f"{bond_code} 数据源未返回转股价 K, 无法定价")

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
    if terms.call_trigger_pct is not None:
        pricer_kwargs["call_trigger_ratio"] = float(terms.call_trigger_pct) / 100.0
    if terms.put_trigger_pct is not None:
        pricer_kwargs["put_trigger_ratio"] = float(terms.put_trigger_pct) / 100.0

    if terms.put_obs_months is not None and issue_dt and maturity_dt:
        total_months = (maturity_dt - issue_dt).days / 30.4375
        active_years = max(0, (total_months - float(terms.put_obs_months)) / 12)
        pricer_kwargs["put_active_years"] = int(round(active_years))
    resolved = resolve_down_reset(bond_code, terms)
    if resolved.block_until is not None:
        pricer_kwargs["down_reset_block_until"] = resolved.block_until

    pricer_kwargs.update(pricer_overrides)
    pricer = UniversalCBPricer(**pricer_kwargs)  # type: ignore[arg-type]

    effective_p_down = float(p_down)
    if resolved.p_scale is not None:
        effective_p_down *= max(0.0, float(resolved.p_scale))

    theo = pricer.price(sigma=sigma, r=r, base_spread=base_spread,
                        distress_k=distress_k, p_down=effective_p_down, M=M, N=N)
    return {
        "bond_code": bond_code,
        "bond_name": terms.sec_name,
        "stock_code": stock_code,
        "valuation_date": val_date,
        "S0": S0,
        "K": pricer.K,
        "T": pricer.T,
        "sigma": sigma,
        "p_down": effective_p_down,
        "down_reset_block_until": resolved.block_until,
        "down_reset_p_scale": resolved.p_scale,
        "down_reset_note": resolved.note,
        "down_reset_cooldown_months": resolved.cooldown_months,
        "down_reset_announce_date": resolved.announce_date,
        "market_price": market_price,
        "credit_rating": terms.credit_rating,
        "outstanding_balance": terms.outstanding_balance,
        "listing_date": terms.listing_date,
        "tradable_date": terms.tradable_date,
        "is_tradable": terms.is_tradable,
        "trading_status": terms.trading_status,
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


def _resolve_batch_workers(max_workers: Optional[int], total: int) -> int:
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
      - hist_vol(stock, end_date, window) → 按 (stock, end_date, window) 缓存

    线程安全 (多线程并发定价时不会重复请求, 先到的线程写入, 后到的直接读缓存).
    """

    def __init__(self, inner: DataProvider):
        self._inner = inner
        self.name = inner.name
        self._close_cache: Dict[tuple, float] = {}
        self._history_cache: Dict[tuple, list] = {}
        self._bond_history_cache: Dict[tuple, list] = {}
        self._vol_cache: Dict[tuple, float] = {}
        import threading
        self._lock = threading.Lock()

    # ── 缓存的接口 ────────────────────────────────────────
    def get_stock_close(self, stock_code, on_date):
        key = (stock_code, on_date)
        with self._lock:
            if key in self._close_cache:
                return self._close_cache[key]
            # 在线 provider 未明确线程安全, 缓存 miss 时也串行访问 inner.
            value = self._inner.get_stock_close(stock_code, on_date)
            self._close_cache[key] = value
            return value

    def get_stock_history(self, stock_code, start, end):
        key = (stock_code, start, end)
        with self._lock:
            if key in self._history_cache:
                return self._history_cache[key]
            value = self._inner.get_stock_history(stock_code, start, end)
            self._history_cache[key] = value
            return value

    def hist_vol(self, stock_code, end_date, window_days):
        key = (stock_code, end_date, window_days)
        with self._lock:
            if key in self._vol_cache:
                return self._vol_cache[key]
        lookback = max(window_days * 2, window_days + 15)
        history = self.get_stock_history(stock_code, end_date - timedelta(days=lookback), end_date)
        closes = np.array([v for _, v in history if v is not None], dtype=float)
        if len(closes) > window_days + 1:
            closes = closes[-(window_days + 1):]
        if len(closes) < 5:
            raise ValueError(f"{stock_code} 历史样本仅 {len(closes)} 条, 无法估算波动率")
        log_ret = np.diff(np.log(closes))
        value = float(np.std(log_ret, ddof=1) * np.sqrt(252))
        latest_close = _latest_price_on_or_before(history, end_date)
        with self._lock:
            self._vol_cache.setdefault(key, value)
            if latest_close is not None:
                self._close_cache.setdefault((stock_code, end_date), latest_close)
            return self._vol_cache[key]

    # ── 直接透传的接口 ────────────────────────────────────
    def get_bond_terms(self, bond_code, valuation_date):
        return self._inner.get_bond_terms(bond_code, valuation_date)

    def get_bond_history(self, bond_code, start, end):
        key = (bond_code, start, end)
        with self._lock:
            if key in self._bond_history_cache:
                return self._bond_history_cache[key]
            value = self._inner.get_bond_history(bond_code, start, end)
            self._bond_history_cache[key] = value
            return value

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
    sigma: Optional[float],
    M: int,
    N: int,
    pricer_overrides: Dict[str, Any],
) -> Dict[str, Any]:
    try:
        res = price_from_provider(
            provider, code,
            r=r, base_spread=base_spread,
            distress_k=distress_k, p_down=p_down,
            valuation_date=valuation_date,
            vol_window_days=vol_window_days,
            sigma=sigma, M=M, N=N,
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


def _sort_batch_results(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    results.sort(key=lambda x: x.get("deviation", float("inf"))
                 if not math.isnan(x.get("deviation", float("nan")))
                 else float("inf"))
    return results


def batch_price_from_provider_threaded(
    provider: DataProvider,
    bond_codes: List[str],
    *,
    r: float = 0.022,
    base_spread: float = 0.03,
    distress_k: float = 0.05,
    p_down: float = 0.15,
    valuation_date: Optional[date] = None,
    vol_window_days: int = 21,
    sigma: Optional[float] = None,
    M: int = 300,
    N: int = 1000,
    max_workers: Optional[int] = None,
    progress_cb=None,
    **pricer_overrides,
) -> List[Dict[str, Any]]:
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
    results: List[Dict[str, Any]] = []
    done_count = 0

    def _price_one(code: str) -> Dict[str, Any]:
        return _batch_result_from_provider(
            cached_provider, code,
            r=r, base_spread=base_spread,
            distress_k=distress_k, p_down=p_down,
            valuation_date=val_date,
            vol_window_days=vol_window_days,
            sigma=sigma, M=M, N=N,
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
    bond_codes: List[str],
    *,
    r: float = 0.022,
    base_spread: float = 0.03,
    distress_k: float = 0.05,
    p_down: float = 0.15,
    valuation_date: Optional[date] = None,
    vol_window_days: int = 21,
    sigma: Optional[float] = None,
    M: int = 300,
    N: int = 1000,
    max_workers: int = 4,
    progress_cb=None,
    **pricer_overrides,
) -> List[Dict[str, Any]]:
    """
    批量定价: 导入代码列表 → 并发定价 → 按理论价/市价基差排序返回.

    参数:
        bond_codes: 转债代码列表, 例如 ['128009.SZ', '113050.SH']
        max_workers: 并发线程数 (PDE 是 CPU-bound, 建议 ≤ CPU 核数)
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
        sigma=sigma, M=M, N=N,
        max_workers=max_workers,
        progress_cb=progress_cb,
        **pricer_overrides,
    )
