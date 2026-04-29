"""同步并解析可转债公告事件.

用法:
    python -m convertible_bond.cli.sync_events --source cninfo --limit 50
    python -m convertible_bond.cli.sync_events --source cninfo --codes 128009.SZ
    python -m convertible_bond.cli.sync_events --source cninfo --apply
    python -m convertible_bond.cli.sync_events --source cninfo --no-pdf
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

from ..cache import TermsBundle, project_bundle_path
from ..cb_event_sync import apply_events_to_bundle, sync_cb_events
from ..cb_events import CBEventStore, project_events_path
from ..data_providers import DataProvider, WindDataProvider


def _make_provider(name: str) -> DataProvider:
    if name == "wind":
        return WindDataProvider()
    if name == "cninfo":
        from ..cninfo_provider import CninfoAnnouncementProvider
        return CninfoAnnouncementProvider()
    raise ValueError(f"未知数据源: {name}  (支持: wind, cninfo)")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="同步公告标题并解析为 cb_events 事件表",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--source", "-s", default="cninfo", choices=["wind", "cninfo"],
                        help="公告数据源 (默认 cninfo, 无需 Wind 终端)")
    parser.add_argument("--bundle", "-b", default="",
                        help="cb_data bundle 路径 (默认 <repo>/data/cb_data.json)")
    parser.add_argument("--events", default="",
                        help="事件表路径 (默认 <repo>/data/cb_events.json)")
    parser.add_argument("--lookback-days", type=int, default=180,
                        help="公告回看天数 (默认 180)")
    parser.add_argument("--limit", type=int, default=0,
                        help="限制最多同步 N 只 (调试用)")
    parser.add_argument("--codes", nargs="*", default=[],
                        help="只同步指定代码")
    parser.add_argument("--apply", action="store_true",
                        help="同步后把事件应用回 cb_data 状态字段")
    parser.add_argument("--no-pdf", action="store_true",
                        help="跳过 PDF 下载, 仅用标题解析事件 (更快)")
    args = parser.parse_args()

    bundle = TermsBundle(Path(args.bundle) if args.bundle else project_bundle_path())
    store = CBEventStore(Path(args.events) if args.events else project_events_path())
    codes = list(args.codes) if args.codes else bundle.list_bonds()
    if args.limit > 0:
        codes = codes[:args.limit]
    if not codes:
        print("❌ 无可同步代码", file=sys.stderr)
        return 1
    try:
        provider = _make_provider(args.source)
    except Exception as exc:
        print(f"❌ 数据源初始化失败: {exc}", file=sys.stderr)
        return 2

    download_pdf = not args.no_pdf
    pdf_label = "✅ PDF 正文提取" if download_pdf else "⏭️  仅标题解析"
    print(f"开始同步公告事件: {len(codes)} 只, 回看 {args.lookback_days} 天")
    print(f"数据源: {args.source}  |  {pdf_label}")
    print(f"事件表: {store.path}")
    start_ts = time.time()

    def progress(i, total, code):
        if (i + 1) % 20 == 0 or (i + 1) == total:
            elapsed = time.time() - start_ts
            rate = (i + 1) / max(elapsed, 1e-6)
            eta = (total - i - 1) / max(rate, 1e-6)
            print(f"  [{i+1:>4}/{total}]  {code:<14}  {rate:.1f}/s  ETA {eta:.0f}s",
                  flush=True)

    result = sync_cb_events(
        provider,
        codes,
        store,
        end=date.today(),
        lookback_days=max(1, args.lookback_days),
        on_progress=progress,
        download_pdf=download_pdf,
    )
    elapsed = time.time() - start_ts
    print(
        f"\n✅ 扫描公告 {result['scanned_announcements']} 条, "
        f"解析事件 {len(result['parsed_events'])} 条, 新增 {result['added']} 条, "
        f"失败 {len(result['failed'])} 只  ({elapsed:.1f}s)"
    )
    if download_pdf:
        print(
            f"   PDF 下载: 成功 {result.get('pdf_downloaded', 0)}, "
            f"失败 {result.get('pdf_failed', 0)}"
        )

    if result["parsed_events"]:
        print("\n解析事件 (前 20):")
        for event in result["parsed_events"][:20]:
            extra = ""
            if event.commitment_months:
                extra = f" [承诺{event.commitment_months}个月→{event.effective_end}]"
            print(f"  {event.event_date} {event.bond_code} {event.event_type}: "
                  f"{event.raw_title[:40]}{extra}")
        if len(result["parsed_events"]) > 20:
            print(f"  ... 还有 {len(result['parsed_events']) - 20} 条")

    if args.apply:
        applied = apply_events_to_bundle(store, bundle)
        print(f"\n已应用事件到 cb_data: 更新 {applied['updated']} 只")
        for code, fields in applied["changed"][:20]:
            print(f"  {code}: {', '.join(fields)}")
        if len(applied["changed"]) > 20:
            print(f"  ... 还有 {len(applied['changed']) - 20} 只")

    if result["failed"]:
        print("\n失败列表 (前 20):")
        for code, err in result["failed"][:20]:
            print(f"  {code}: {err}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
