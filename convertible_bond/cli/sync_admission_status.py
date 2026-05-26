"""刷新 cb_data 中的交易状态与风险字段.

用法:
    python -m convertible_bond.cli.sync_admission_status
    python -m convertible_bond.cli.sync_admission_status --limit 50
    python -m convertible_bond.cli.sync_admission_status --codes 113050.SH 128009.SZ

该命令只做增量状态刷新, 不重拉完整条款。会尝试更新:
停牌/交易状态、强赎公告状态、最后交易日/摘牌日、正股 ST 风险、
转债成交额、评级、剩余余额等字段。
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

from ..admission_status import refresh_admission_status
from ..cache import TermsBundle, project_bundle_path
from ..data_providers import DataProvider, WindDataProvider


def _make_provider(name: str) -> DataProvider:
    if name == "wind":
        return WindDataProvider()
    raise ValueError(f"未知数据源: {name}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="刷新 cb_data 中的交易状态与风险字段",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--source", "-s", default="wind", choices=["wind"],
                        help="状态数据源 (默认 Wind)")
    parser.add_argument("--bundle", "-b", default="",
                        help="cb_data bundle 路径 (默认 <repo>/data/cb_data.json)")
    parser.add_argument("--limit", type=int, default=0,
                        help="限制最多刷新 N 只 (调试用, 0=不限制)")
    parser.add_argument("--codes", nargs="*", default=[],
                        help="只刷新指定代码")
    args = parser.parse_args()

    bundle_path = Path(args.bundle) if args.bundle else project_bundle_path()
    bundle = TermsBundle(bundle_path)
    codes = list(args.codes) if args.codes else bundle.list_bonds()
    if args.limit > 0:
        codes = codes[:args.limit]
    if not codes:
        print("❌ 无可刷新的代码", file=sys.stderr)
        return 1

    try:
        provider = _make_provider(args.source)
    except Exception as exc:
        print(f"❌ 数据源初始化失败: {exc}", file=sys.stderr)
        return 2

    print(f"开始刷新状态字段: {len(codes)} 只")
    print(f"Bundle: {bundle.path}")
    start = time.time()

    def progress(i, total, code):
        if (i + 1) % 20 == 0 or (i + 1) == total:
            elapsed = time.time() - start
            rate = (i + 1) / max(elapsed, 1e-6)
            eta = (total - i - 1) / max(rate, 1e-6)
            print(f"  [{i+1:>4}/{total}]  {code:<14}  {rate:.1f}/s  ETA {eta:.0f}s",
                  flush=True)

    result = refresh_admission_status(
        provider,
        codes,
        store=bundle,
        valuation_date=date.today(),
        on_progress=progress,
    )
    elapsed = time.time() - start

    print(
        f"\n✅ 成功 {len(result['success'])} 只, "
        f"变更 {len(result['changed'])} 只, "
            f"当前公开交易过滤 {len(result['excluded'])} 只, "
        f"失败 {len(result['failed'])} 只, 耗时 {elapsed:.1f}s"
    )
    if result["excluded_by_reason"]:
        print("\n当前剔除原因:")
        for reason, count in result["excluded_by_reason"].items():
            print(f"  {reason}: {count}")
    if result["changed"]:
        print("\n变更明细 (前 20):")
        for code, fields in result["changed"][:20]:
            print(f"  {code}: {', '.join(fields)}")
        if len(result["changed"]) > 20:
            print(f"  ... 还有 {len(result['changed']) - 20} 只")
    if result["failed"]:
        print("\n失败列表 (前 20):")
        for code, err in result["failed"][:20]:
            print(f"  {code}: {err}")
        if len(result["failed"]) > 20:
            print(f"  ... 还有 {len(result['failed']) - 20} 只")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
