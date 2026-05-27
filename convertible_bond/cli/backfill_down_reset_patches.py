"""从历史 down_reset_approved 事件回填 conversion_price patch.

实时事件同步 (``cb-sync-events --apply``) 会在解析下修公告时生成
``conversion_price`` patch, 但早期事件入库时解析逻辑不全, 导致
``cb_terms_patches.json`` 里下修 K 覆盖偏稀。本脚本扫描 ``cb_events.json``
里所有 ``down_reset_approved`` (以及 ``conversion_price_adjusted``) 事件,
按以下优先级回填 patch:

  1. 直接使用 ``CBEvent.event_price`` (后期事件已解析)
  2. 用 ``parse_down_reset_new_price`` / ``parse_conversion_price_adjustment``
     从 ``raw_title`` 重新解析 (老事件 fallback)
  3. ``--fetch-pdf`` 开启时, 下载/读取本地 PDF 正文重新解析 (最慢但覆盖率最高)

生效日规则: ``effective_start`` > ``event_date``。生成的 patch 通过
``TermsPatchStore.add_many`` 写回, 已有同 key 的 patch 不会重复。

用法::

    python -m convertible_bond.cli.backfill_down_reset_patches --dry-run
    python -m convertible_bond.cli.backfill_down_reset_patches --fetch-pdf
    python -m convertible_bond.cli.backfill_down_reset_patches --fetch-pdf --dry-run
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from ..announcement_pdf import announcement_pdf_path, fetch_announcement_pdf
from ..cb_event_sync import parse_conversion_price_adjustment
from ..cb_events import CBEvent, CBEventStore, parse_down_reset_new_price, project_events_path
from ..historical_terms import TermsPatch, TermsPatchStore, project_terms_patches_path

_TARGET_TYPES = ("down_reset_approved", "conversion_price_adjusted")


def _resolve_from_text(text: str | None) -> float | None:
    if not text:
        return None
    price = parse_down_reset_new_price(text)
    if price is not None:
        return float(price)
    adj = parse_conversion_price_adjustment(text)
    if adj and adj.get("new_price"):
        try:
            value = float(adj["new_price"])
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None
    return None


def _resolve_from_pdf(event: CBEvent) -> float | None:
    """下载 / 读本地缓存的 PDF, 提取正文重新解析新 K. 失败返回 None。"""
    if not event.url:
        return None
    try:
        from ..cninfo_provider import extract_text_from_pdf_bytes
    except ImportError:
        return None
    pdf_path = announcement_pdf_path(event.bond_code, event.event_date, event.url)
    try:
        fetch_announcement_pdf(event.url, pdf_path)
    except Exception:
        return None
    try:
        text = extract_text_from_pdf_bytes(pdf_path.read_bytes())
    except Exception:
        return None
    return _resolve_from_text(text)


def _resolve_new_price(
    event: CBEvent, *, fetch_pdf: bool = False
) -> tuple[float | None, str]:
    """返回 ``(new_price, source_label)``.

    source_label: ``event_price`` / ``parsed_title`` / ``parsed_pdf`` / ``unresolved``。
    """
    if event.event_price is not None and event.event_price > 0:
        return float(event.event_price), "event_price"
    price = _resolve_from_text(event.raw_title)
    if price is not None:
        return price, "parsed_title"
    if fetch_pdf:
        price = _resolve_from_pdf(event)
        if price is not None:
            return price, "parsed_pdf"
    return None, "unresolved"


def _build_patch(event: CBEvent, new_price: float) -> TermsPatch:
    effective_date = event.effective_start or event.event_date
    source_key = (
        f"{event.bond_code}|{event.event_date.isoformat()}|"
        f"{event.event_type}|{event.raw_title.strip()}"
    )
    return TermsPatch(
        bond_code=event.bond_code,
        effective_date=effective_date,
        event_date=event.event_date,
        fields={"conversion_price": float(new_price)},
        source="backfill_events",
        note=f"回填: 转股价 ->{new_price:g} | {event.url or ''}".rstrip(" |"),
        raw_title=event.raw_title,
        confidence="parsed",
        source_event_key=source_key,
    )


def backfill(
    events_path: Path | None = None,
    patches_path: Path | None = None,
    *,
    dry_run: bool = False,
    fetch_pdf: bool = False,
    progress_cb=None,
) -> dict:
    event_store = CBEventStore(events_path or project_events_path())
    patch_store = TermsPatchStore(patches_path or project_terms_patches_path())
    existing_keys = {p.key() for p in patch_store.list_patches()}

    stats: Counter = Counter()
    candidate_patches: list[TermsPatch] = []
    unresolved: list[CBEvent] = []

    target_events = [e for e in event_store.list_events() if e.event_type in _TARGET_TYPES]
    for idx, event in enumerate(target_events):
        stats["scanned"] += 1
        if progress_cb:
            progress_cb(idx + 1, len(target_events), event)
        new_price, source_label = _resolve_new_price(event, fetch_pdf=fetch_pdf)
        stats[f"source_{source_label}"] += 1
        if new_price is None:
            unresolved.append(event)
            continue
        patch = _build_patch(event, new_price)
        if patch.key() in existing_keys:
            stats["already_patched"] += 1
            continue
        existing_keys.add(patch.key())
        candidate_patches.append(patch)

    if dry_run:
        added = 0
    else:
        added = patch_store.add_many(candidate_patches) if candidate_patches else 0
    stats["new_patches"] = added if not dry_run else len(candidate_patches)
    return {
        "stats": dict(stats),
        "candidate_patches": candidate_patches,
        "unresolved_events": unresolved,
        "patches_path": patch_store.path,
        "events_path": event_store.path,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="从历史 down_reset_approved 事件回填 conversion_price patch",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--events-path", default="", help="事件表路径 (默认 data/cb_events.json)")
    parser.add_argument("--patches-path", default="",
                        help="patch 文件路径 (默认 data/cb_terms_patches.json)")
    parser.add_argument("--dry-run", action="store_true",
                        help="只报告将要新增的 patch, 不写入文件")
    parser.add_argument("--fetch-pdf", action="store_true",
                        help="raw_title 解析失败时, 下载/读本地 PDF 正文重新解析 (慢, 需网络)")
    parser.add_argument("--show-unresolved", action="store_true",
                        help="打印解析失败的事件, 便于人工补 patch")
    parser.add_argument("--quiet", action="store_true",
                        help="不打印进度条")
    parser.add_argument("--limit-show", type=int, default=20,
                        help="show-unresolved/dry-run 列表打印上限 (默认 20)")
    args = parser.parse_args()

    events_path = Path(args.events_path) if args.events_path else None
    patches_path = Path(args.patches_path) if args.patches_path else None

    def _progress(idx: int, total: int, event):
        if args.quiet or not args.fetch_pdf:
            return
        sys.stderr.write(f"\r[{idx}/{total}] {event.bond_code} {event.event_date}  ")
        sys.stderr.flush()

    result = backfill(
        events_path, patches_path,
        dry_run=args.dry_run,
        fetch_pdf=args.fetch_pdf,
        progress_cb=_progress,
    )
    if args.fetch_pdf and not args.quiet:
        sys.stderr.write("\n")
    stats = result["stats"]
    print(f"事件表: {result['events_path']}")
    print(f"Patch:  {result['patches_path']}")
    print(f"扫描事件: {stats.get('scanned', 0)} 条")
    print(f"  - 已有 event_price: {stats.get('source_event_price', 0)}")
    print(f"  - 从 raw_title 解析: {stats.get('source_parsed_title', 0)}")
    if args.fetch_pdf:
        print(f"  - 从 PDF 正文解析: {stats.get('source_parsed_pdf', 0)}")
    print(f"  - 解析失败: {stats.get('source_unresolved', 0)}")
    print(f"已存在 patch (跳过): {stats.get('already_patched', 0)}")
    if args.dry_run:
        print(f"将新增 patch: {stats.get('new_patches', 0)} 条 (dry-run, 未写入)")
        for patch in result["candidate_patches"][:args.limit_show]:
            print(f"  + {patch.bond_code} {patch.effective_date} K={patch.fields['conversion_price']:g}"
                  f"  ← {patch.raw_title[:48]}")
        more = len(result["candidate_patches"]) - args.limit_show
        if more > 0:
            print(f"  ... 还有 {more} 条")
    else:
        print(f"新增 patch: {stats.get('new_patches', 0)} 条 (已写入)")

    if args.show_unresolved and result["unresolved_events"]:
        print("\n未解析事件 (raw_title 中找不到新转股价):")
        for event in result["unresolved_events"][:args.limit_show]:
            print(f"  {event.bond_code} {event.event_date} | {event.raw_title[:70]}")
        more = len(result["unresolved_events"]) - args.limit_show
        if more > 0:
            print(f"  ... 还有 {more} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
