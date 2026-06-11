"""
可转债定价系统 — CLI 入口与最小兼容 facade.

核心代码已拆分到 `convertible_bond/` 包内 (pricer / pricing_api / backtest /
data_providers / cache). 新代码请直接 import 子模块, 不要依赖本文件的 re-export。
"""
from datetime import date

from convertible_bond.pricer import UniversalCBPricer
from convertible_bond.pricing_api import price_from_provider
from convertible_bond.cache import CachedBondDataProvider, TermsBundle, project_bundle_path
from convertible_bond.data_providers import (
    WindDataProvider, AkshareDataProvider, auto_data_provider,
)


def _cli_price(argv):
    """命令行: python CB.py <bond_code> [valuation_date] [--source wind|akshare|auto].

    source 只选择正股价格/历史波动率/无风险利率等动态行情源;
    转债基础信息固定读取 cb_data, 缓存缺失时由 WindPy 刷新。
    """
    import argparse
    parser = argparse.ArgumentParser(prog="CB.py", description="可转债自动定价")
    parser.add_argument("bond_code", help="转债代码, 例 128009.SZ")
    parser.add_argument("valuation_date", nargs="?", default=None,
                        help="估值日 YYYY-MM-DD (默认今天)")
    parser.add_argument("--source", "-s", default="auto",
                        choices=["auto", "wind", "akshare"],
                        help="动态行情源 (默认 auto: Wind 优先, 缺则 akshare)")
    args = parser.parse_args(argv)

    val_date = date.fromisoformat(args.valuation_date) if args.valuation_date else None

    if args.source == "wind":
        market_provider = WindDataProvider()
    elif args.source == "akshare":
        market_provider = AkshareDataProvider()
    else:
        market_provider = auto_data_provider()
        print(f"[auto] 选用行情源: {market_provider.name}")

    provider = CachedBondDataProvider(
        market_provider,
        TermsBundle(project_bundle_path()),
        static_source=market_provider if isinstance(market_provider, WindDataProvider) else None,
        max_age_days=365,
    )

    result = price_from_provider(provider, args.bond_code, valuation_date=val_date)
    print(f"--- {provider.name} 自动定价: {result['bond_code']} ---")
    print(f"标的正股: {result['stock_code']}, S0={result['S0']:.3f}")
    print(f"转股价 K: {result['K']:.3f}")
    print(f"剩余期限 T: {result['T']:.4f} 年")
    print(f"历史波动率: {result['sigma']:.4%}")
    print(f"股息率 q: {result.get('q', 0.0):.4%}")
    print(f"理论价值: {result['theoretical_price']:.3f}")
    if result.get("market_price") is not None:
        diff = result["market_price"] - result["theoretical_price"]
        print(f"市场价格: {result['market_price']:.3f}  (溢价 {diff:+.3f})")


def _offline_demo():
    today = date(2026, 4, 20)
    pricer = UniversalCBPricer(
        S0=55.0, K=52.77,
        current_date=today, maturity_date=date(2026, 7, 30),
        issue_date=date(2020, 7, 30),
        conversion_start_date=date(2021, 2, 6),
        coupon_rates=(0.003, 0.004, 0.008, 0.015, 0.018, 0.02),
        redemption_price=107.0,
    )
    full = pricer.price(sigma=0.28, r=0.022, base_spread=0.03,
                        distress_k=0.05, p_down=0.0, return_greeks=True)

    print("--- 离线示例 ---")
    print(f"当前剩余期限: {pricer.T:.4f} 年")
    print(f"当前票面利率: {pricer.get_coupon_rate(today):.4%}")
    print(f"当前应计利息: {pricer.accrued_interest(today):.4f}")
    print(f"通用模型估算价: {full['price']:.3f}")
    print()
    print("--- 希腊值 & 价值分解 ---")
    print(f"  纯债价值: {full['bond_floor']:.3f}    "
          f"转股价值: {full['parity']:.3f}    "
          f"期权溢价: {full['option_premium']:.3f}")
    print(f"  Δ={full['delta']:.4f}  Γ={full['gamma']:.6f}  "
          f"ν={full['vega']:.4f}  Θ={full['theta']:.4f}")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        _cli_price(sys.argv[1:])
    else:
        _offline_demo()
