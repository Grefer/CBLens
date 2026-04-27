"""
可转债定价系统 — 统一入口 (facade).

本文件为向后兼容的 re-export 层. 核心代码已拆分至:
  - pricer.py      — UniversalCBPricer (PDE 求解器, 无数据源依赖)
  - backtest.py    — backtest_theoretical_price (Wind 回测)
  - data_providers.py — DataProvider / Wind / akshare / CSV 后端

新代码建议直接 import 对应子模块; 本文件保证
  from CB import UniversalCBPricer, price_from_wind, ...
仍然可用.
"""
import logging
from datetime import date, timedelta
from typing import List, Optional, Dict, Any
from concurrent.futures import ThreadPoolExecutor, as_completed

from pricer import (
    UniversalCBPricer,
    DEFAULT_COUPON_RATES,
    DEFAULT_FACE_VALUE,
    DEFAULT_REDEMPTION_PRICE,
)

from data_providers import (
    DataProvider,
    BondTerms,
    CashflowSchedule,
    WindDataProvider,
    AkshareDataProvider,
    CSVDataProvider,
    to_date,
    parse_coupon_string as parse_coupon,
)

from backtest import (
    backtest_theoretical_price,
    _ensure_wind,
    _to_date,
    _parse_coupon,
    _fetch_cashflow,
    _hist_vol,
)

logger = logging.getLogger(__name__)

__all__ = [
    "UniversalCBPricer",
    "price_from_wind",
    "price_from_provider",
    "batch_price_from_provider",
    "backtest_theoretical_price",
    "DataProvider",
    "BondTerms",
    "CashflowSchedule",
    "WindDataProvider",
    "AkshareDataProvider",
    "CSVDataProvider",
    "to_date",
    "parse_coupon",
    "DEFAULT_COUPON_RATES",
    "DEFAULT_FACE_VALUE",
    "DEFAULT_REDEMPTION_PRICE",
]


# ==========================================
# 通用接口: 输入转债代码 + DataProvider, 自动拉参数并定价
# ==========================================
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

    S0 = provider.get_stock_close(stock_code, val_date)

    if sigma is None:
        sigma = provider.hist_vol(stock_code, val_date, vol_window_days)

    issue_dt = terms.issue_date
    # A 股可转债转股起始日 = 发行日 + 6 个月 (监管规定)
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

    # 回售观察期月数 → 整数年生效窗口
    if terms.put_obs_months is not None and issue_dt and maturity_dt:
        total_months = (maturity_dt - issue_dt).days / 30.4375
        active_years = max(0, (total_months - float(terms.put_obs_months)) / 12)
        pricer_kwargs["put_active_years"] = int(round(active_years))

    pricer_kwargs.update(pricer_overrides)
    pricer = UniversalCBPricer(**pricer_kwargs)  # type: ignore[arg-type]

    theo = pricer.price(sigma=sigma, r=r, base_spread=base_spread,
                        distress_k=distress_k, p_down=p_down, M=M, N=N)
    return {
        "bond_code": bond_code,
        "bond_name": terms.sec_name,
        "stock_code": stock_code,
        "valuation_date": val_date,
        "S0": S0,
        "K": pricer.K,
        "T": pricer.T,
        "sigma": sigma,
        "market_price": terms.close,
        "credit_rating": terms.credit_rating,
        "outstanding_balance": terms.outstanding_balance,
        "coupon_source": "cashflow" if cf and cf.coupon_rates else "terms",
        "theoretical_price": theo,
        "data_source": provider.name,
    }


def price_from_wind(bond_code, **kwargs):
    """便捷封装: 默认用 WindDataProvider. 完整 API 见 price_from_provider."""
    return price_from_provider(WindDataProvider(), bond_code, **kwargs)


# ==========================================
# 批量定价 (#10)
# ==========================================
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
    val_date = valuation_date or date.today()
    total = len(bond_codes)
    results: List[Dict[str, Any]] = []
    done_count = [0]

    def _price_one(code: str) -> Dict[str, Any]:
        try:
            res = price_from_provider(
                provider, code,
                r=r, base_spread=base_spread,
                distress_k=distress_k, p_down=p_down,
                valuation_date=val_date,
                vol_window_days=vol_window_days,
                sigma=sigma, M=M, N=N,
                **pricer_overrides,
            )
            mkt = res.get("market_price")
            theo = res["theoretical_price"]
            if mkt is not None and theo > 0:
                res["deviation"] = (float(mkt) - theo) / theo
            else:
                res["deviation"] = float("nan")
            res["status"] = "ok"
            return res
        except Exception as exc:
            return {
                "bond_code": code,
                "status": str(exc),
                "theoretical_price": float("nan"),
                "market_price": None,
                "deviation": float("nan"),
            }

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_price_one, code): code for code in bond_codes}
        for fut in as_completed(futures):
            result = fut.result()
            results.append(result)
            done_count[0] += 1
            if progress_cb:
                progress_cb(done_count[0], total)

    # 按 deviation 升序: 最被低估的排最前
    import math
    results.sort(key=lambda x: x.get("deviation", float("inf"))
                 if not math.isnan(x.get("deviation", float("nan")))
                 else float("inf"))
    return results


# ==========================================
# 公有 API 别名 (供 GUI 及外部调用)
# ==========================================
ensure_wind = _ensure_wind
hist_vol = _hist_vol
fetch_cashflow = _fetch_cashflow


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

        full = pricer.price(sigma=0.28, r=0.022, base_spread=0.03,
                            distress_k=0.05, p_down=0.0, return_greeks=True)
        print()
        print(f"--- 希腊值 & 价值分解 ---")
        print(f"理论价: {full['price']:.3f}")
        print(f"  纯债价值: {full['bond_floor']:.3f}    "
              f"转股价值: {full['parity']:.3f}    "
              f"期权溢价: {full['option_premium']:.3f}")
        print(f"  Δ={full['delta']:.4f}  Γ={full['gamma']:.6f}  "
              f"ν={full['vega']:.4f}  Θ={full['theta']:.4f}")