"""按原公告正文重放存量转股价 patch，保留权威值、人工值和无法确认的记录。

默认只预览且只读正文缓存；--download 允许补正文，--apply 备份后原位改写。
与重新同步的区别是替换原记录，避免 add_many 把新旧两份转股价都留下。
"""
from __future__ import annotations

import argparse
import collections
import json
import shutil
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from ..cb_event_sync import parse_conversion_price_adjustment
from ..cb_events import project_events_path
from ..historical_terms import TermsPatchStore, project_terms_patches_path
from .repair_putback_windows import (
    DEFAULT_DELAY_SECONDS,
    ConcurrentWriteError,
    default_cache_dir,
    fetch_body,
)
from .repair_rating_patches import _url_index


def repair(
    patches_path: Path | str | None = None,
    event_path: Path | str | None = None,
    *,
    dry_run: bool = True,
    download: bool = False,
    cache_dir: Path | None = None,
    codes: set[str] | None = None,
    delay: float = DEFAULT_DELAY_SECONDS,
    progress=None,
) -> dict:
    """只修改 cninfo 的 K 字段；无正文/解析失败/混合字段日期冲突均保留待核。"""
    path = Path(patches_path or project_terms_patches_path())
    fingerprint = path.read_bytes() if path.exists() else b""
    store = TermsPatchStore(path)
    exact, unique = _url_index(event_path or project_events_path())
    targets = [
        p for p in store.list_patches(include_shadowed=True)
        if p.source == "cninfo" and "conversion_price" in p.fields
        and (codes is None or p.bond_code in codes)
    ]
    stats: collections.Counter = collections.Counter()
    plan = {}
    details = []
    for index, patch in enumerate(targets, 1):
        # 已知公告日时必须精确接回同一天，不能用唯一标题猜另一年的正文。
        url = (exact.get((patch.bond_code, patch.raw_title or "", patch.event_date))
               if patch.event_date is not None
               else unique.get((patch.bond_code, patch.raw_title or "")))
        parsed = None
        if not url:
            stats["no_url"] += 1
        else:
            body = fetch_body(url, cache_dir, download=download)
            if not body:
                stats["no_body"] += 1
            else:
                parsed = parse_conversion_price_adjustment(body, bond_code=patch.bond_code)
                if not parsed:
                    stats["unparsed"] += 1
        if parsed:
            effective = parsed.get("effective_date") or patch.effective_date
            # 不能用 K 的生效日挪动同条 patch 中评级等其他字段。
            if effective != patch.effective_date and set(patch.fields) != {"conversion_price"}:
                stats["mixed_date_conflict"] += 1
            elif patch.event_date and effective < patch.event_date:
                # 沿革日期误命中的已知形态，不能自动写回。回溯公告留给人工复核。
                stats["predates_announcement"] += 1
            else:
                fields = {**patch.fields, "conversion_price": float(parsed["new_price"])}
                before = dict(patch.before_fields or {})
                old = parsed.get("old_price")
                if old is None:
                    before.pop("conversion_price", None)
                else:
                    before["conversion_price"] = float(old)
                if (fields == patch.fields and before == (patch.before_fields or {})
                        and effective == patch.effective_date):
                    stats["unchanged"] += 1
                else:
                    # 纯 K 记录的 note 同步刷新，不能继续描述已被替换的错误数值。
                    note = patch.note
                    if set(fields) == {"conversion_price"}:
                        old_text = f"{old:g}" if old is not None else "?"
                        note = f"转股价 {old_text}->{parsed['new_price']:g} | {url}"
                    fixed = replace(patch, fields=fields, before_fields=before or None,
                                    effective_date=effective, note=note,
                                    confidence=str(parsed.get("confidence") or patch.confidence))
                    # source 不属于 TermsPatch.key，计划额外带 source，免伤同键人工记录。
                    plan[(patch.source, patch.key())] = fixed
                    details.append({
                        "bond_code": patch.bond_code, "event_date": str(patch.event_date),
                        "before": patch.before_fields, "after": before,
                        "old_fields": patch.fields, "new_fields": fields,
                        "old_effective_date": str(patch.effective_date),
                        "new_effective_date": str(effective), "url": url,
                    })
        if progress:
            progress(index, len(targets), patch)
        if download and url and delay > 0:
            time.sleep(delay)

    result = {"scanned": len(targets), "planned": len(plan), "changed": 0,
              "stats": dict(stats), "details": details, "backup_path": None}
    if dry_run or not plan:
        return result
    if (path.read_bytes() if path.exists() else b"") != fingerprint:
        raise ConcurrentWriteError(f"{path} 在扫描后被改动，已放弃写入；请重跑")
    backup = path.with_suffix(f".bak-conversion-{datetime.now():%Y%m%d%H%M%S%f}.json")
    shutil.copy2(path, backup)
    changed, _ = store.rewrite(lambda p: plan.get((p.source, p.key()), p))
    result.update(changed=changed, backup_path=str(backup))
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按公告正文修复存量转股价 patch（默认预览）")
    parser.add_argument("--apply", action="store_true", help="先备份，再真正写盘")
    parser.add_argument("--download", action="store_true", help="允许联网补齐正文缓存")
    parser.add_argument("--codes", nargs="+", help="只处理指定转债代码")
    parser.add_argument("--patch-path")
    parser.add_argument("--event-path")
    parser.add_argument("--cache-dir")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = repair(
        args.patch_path, args.event_path, dry_run=not args.apply,
        download=args.download, codes=set(args.codes) if args.codes else None,
        cache_dir=Path(args.cache_dir) if args.cache_dir else default_cache_dir(),
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"扫描 {result['scanned']} 条，计划修复 {result['planned']} 条，"
              f"已写入 {result['changed']} 条" + ("" if args.apply else " [预览]"))
        print(f"保留/待核: {result['stats']}")
        for row in result["details"]:
            print(f"  {row['bond_code']} {row['old_effective_date']} → "
                  f"{row['new_effective_date']}: {row['old_fields']} → {row['new_fields']}")
        if result["backup_path"]:
            print(f"备份: {result['backup_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
