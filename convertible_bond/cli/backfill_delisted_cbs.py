"""通过 Wind 历史成分回填已退市可转债静态条款.

``cb-sync-tradable`` 调 ``list_tradable_cbs`` 只返回**今日存续**的可转债, 老 cb_data
长期承受幸存者偏差: 已强赎/已到期的债退出样本, 回测 2024 之前的窗口会偷偷剔除掉
那些"涨到强赎"的好券。本 CLI 用 Wind ``sectorconstituent`` 按多个历史时点扫描转债
成分并集, 与本地 ``cb_data.json`` 取差集, 然后逐只补静态条款。

与 ``cb-sync-tradable`` 的区别:

  - 显式 ``drop_terminal=False``: 已到期 / 异常状态的债**不**被剔除, 否则补不进
  - 默认按季度末扫描 2018 年至今的成分, 抓取尽可能完整的"曾上市"集合
  - 写入仍走 ``TermsBundle`` 同一个 ``cb_data.json``, 与当前 345 只共存

用法::

    python -m convertible_bond.cli.backfill_delisted_cbs --dry-run
    python -m convertible_bond.cli.backfill_delisted_cbs
    python -m convertible_bond.cli.backfill_delisted_cbs --start-year 2020
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

from ..cache import TermsBundle, project_bundle_path
from ..cb_data_sync import sync_cb_terms
from ..data_providers import WindDataProvider


def _quarter_end_dates(start_year: int, end_year: int) -> list[date]:
    dates: list[date] = []
    for y in range(start_year, end_year + 1):
        for m in (3, 6, 9, 12):
            try:
                dates.append(date(y, m, 28))
            except ValueError:
                continue
    return dates


def _scan_historical_universe(
    provider: WindDataProvider,
    *,
    start_year: int,
    end_year: int,
) -> dict[str, str | None]:
    """按季度末扫描 Wind 转债成分, 返回 ``{wind_code: sec_name}`` 全宇宙."""
    w = provider._ensure()  # 确保 Wind 已启动 (provider 内部会调 w.start)

    universe: dict[str, str | None] = {}
    dates = _quarter_end_dates(start_year, end_year)
    print(f"扫描历史成分: {len(dates)} 个时点 (Q-end, {start_year}-{end_year})")
    for d in dates:
        res = w.wset(
            "sectorconstituent",
            f"date={d.isoformat()};sectorid=a101020600000000;field=wind_code,sec_name",
        )
        if res.ErrorCode != 0 or not res.Data:
            print(f"  {d}: ErrorCode={res.ErrorCode}, 跳过", file=sys.stderr)
            continue
        codes = res.Data[0]
        names = res.Data[1] if len(res.Data) > 1 else []
        for i, code in enumerate(codes):
            if not code:
                continue
            name = names[i] if i < len(names) else None
            universe.setdefault(str(code), str(name) if name else None)
        print(f"  {d}: 累计 {len(universe)} 只", flush=True)
    return universe


def main() -> int:
    parser = argparse.ArgumentParser(
        description="通过 Wind 历史成分回填已退市可转债静态条款",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--bundle", "-b", default="",
                        help="cb_data bundle 路径 (默认 <repo>/data/cb_data.json)")
    parser.add_argument("--start-year", type=int, default=2018,
                        help="历史成分扫描起始年 (默认 2018)")
    parser.add_argument("--end-year", type=int, default=date.today().year,
                        help="历史成分扫描结束年 (默认今年)")
    parser.add_argument("--codes", nargs="*", default=[],
                        help="跳过历史扫描, 直接指定要补的代码 (调试用)")
    parser.add_argument("--limit", type=int, default=0,
                        help="只补前 N 只 (调试用, 0=全补)")
    parser.add_argument("--dry-run", action="store_true",
                        help="只列出差集, 不调 Wind 拉条款")
    args = parser.parse_args()

    bundle_path = Path(args.bundle) if args.bundle else project_bundle_path()
    bundle = TermsBundle(bundle_path)
    local_codes = set(bundle.list_bonds())
    print(f"本地 cb_data: {bundle_path}")
    print(f"本地条款数: {len(local_codes)}")

    provider = WindDataProvider()

    if args.codes:
        missing = sorted(set(args.codes) - local_codes)
        print(f"使用用户指定的 {len(args.codes)} 个代码, 去重后差集 {len(missing)} 只")
    else:
        universe = _scan_historical_universe(
            provider, start_year=args.start_year, end_year=args.end_year,
        )
        missing = sorted(set(universe.keys()) - local_codes)
        print(f"\nWind 历史并集: {len(universe)} 只")
        print(f"缺失 (将回填): {len(missing)} 只")
        if missing:
            print("样本前 10:")
            for code in missing[:10]:
                print(f"  {code}  {universe.get(code) or ''}")

    if args.limit > 0:
        missing = missing[:args.limit]
        print(f"\n(--limit 截断至前 {len(missing)} 只)")
    if not missing:
        print("\n无需要补的代码")
        return 0
    if args.dry_run:
        print(f"\n--dry-run 启用, 不调 Wind 拉条款")
        return 0

    print(f"\n开始 Wind 回拉 {len(missing)} 只静态条款 (预计 ~{len(missing)*0.6:.0f}s)")
    print("注: drop_terminal=False, 不剔除已到期/异常债")
    start = time.time()

    def progress(i: int, total: int, code: str):
        if (i + 1) % 20 == 0 or (i + 1) == total:
            elapsed = time.time() - start
            rate = (i + 1) / max(elapsed, 1e-6)
            eta = (total - i - 1) / max(rate, 1e-6)
            print(f"  [{i+1:>4}/{total}]  {code:<14}  {rate:.1f}/s  ETA {eta:.0f}s",
                  flush=True)

    result = sync_cb_terms(
        provider, missing, store=bundle,
        drop_terminal=False,
        on_progress=progress,
    )
    elapsed = time.time() - start
    print(
        f"\n✅ 成功 {len(result['success'])} 只, "
        f"⚠️ 剔除 {len(result.get('dropped', []))} 只, "
        f"❌ 失败 {len(result.get('failed', []))} 只, "
        f"耗时 {elapsed:.1f}s"
    )
    if result.get("failed"):
        print("\n失败列表 (前 20):")
        for code, err in result["failed"][:20]:
            print(f"  {code}: {err}")
    if result.get("dropped"):
        print("\n剔除列表 (前 20):")
        for code, reason in result["dropped"][:20]:
            print(f"  {code}: {reason}")
    print(f"\nbundle: {bundle.path} 现存 {len(bundle.list_bonds())} 只")
    return 0


if __name__ == "__main__":
    sys.exit(main())
