"""cb-repair-putback-windows: 回洗回售事件里缺失/退化的申报窗口。

背景 (实测 data/cb_events.json 全库 7794 条事件, 其中 putback 1082 条):

  类别                          有 end   缺 end   合计   缺失率
  提示性公告 (应有窗口)            585      258    843     31%
  法律意见书 / 核查意见等配套文件     19      158    177     89%
  回售申报情况 / 结果公告             0        3      3    100%

两类问题混在一起, 必须分开处理:

① **正文当时没拿到**。抽样重下 20 条缺 end 的记录, 全部 HTTP 200 且正文可提取 ——
   也就是说 PDF 现在拿得到, 只是同步当时没拿到 (``cb-sync-events`` 的 ``--download-pdf``
   未开、或当时下载失败)。这类只要重下重解析就能补齐。

② **``effective_start`` 是谎**。解析不到窗口时它会回落成**公告日本身**, 于是每条配套
   文件都变成一个"从公告日开始、永不结束"的假窗口。实测主池 28 只债的
   ``putback_start_date`` 就是这么来的 (美锦转债真实窗口 2025-12-01~12-05, 却按第三次
   提示性公告的日期存成 2025-12-11 且无截止日)。解析侧已经不再产生它
   (见 ``cb_events.parse_event_from_announcement`` 的 putback 分支), 落库的这批要洗掉。

判据与解析侧**共用同一个函数** (``putback_window_is_complete`` /
``putback_start_is_degraded`` / ``parse_putback_terms``) —— 两边各写一份正是本仓库
反复踩过的坑 (见 AGENTS.md「公告解析里条款文字与当期状态必须区分」)。

用法::

    cb-repair-putback-windows                     # 只扫描, 不下载不写盘
    cb-repair-putback-windows --download          # 重下正文并预览能补回多少
    cb-repair-putback-windows --download --apply  # 真正写盘 (自动备份)

正文按 URL 落盘缓存 (``data/announcement_text_cache/``, 已 gitignore), 中断可续跑,
重跑不重复下载。
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import shutil
import sys
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from ..cb_events import (
    CBEvent,
    CBEventStore,
    parse_putback_terms,
    project_events_path,
    putback_start_is_degraded,
    putback_window_is_complete,
)
from ..paths import data_path


class ConcurrentWriteError(RuntimeError):
    """扫描后、写盘前事件文件被别的进程改过。"""


DEFAULT_DELAY_SECONDS = 0.8


def default_cache_dir() -> Path:
    return Path(data_path("announcement_text_cache", seed=False))


def _cache_file(cache_dir: Path, url: str) -> Path:
    return cache_dir / f"{hashlib.sha1(url.encode('utf-8')).hexdigest()}.txt"


def fetch_body(url: str, cache_dir: Path | None, *, download: bool) -> str | None:
    """取公告正文: 先读盘上缓存, 未命中且允许下载时再拉网络。

    缓存**同时记录空结果** (写入空文件), 否则扫描件/图片版公告每次重跑都要重下一遍。
    """
    cached: Path | None = None
    if cache_dir is not None:
        cached = _cache_file(cache_dir, url)
        if cached.exists():
            text = cached.read_text(encoding="utf-8")
            return text or None
    if not download:
        return None
    from ..cb_event_sync import _try_download_body

    body = _try_download_body(None, url)
    if cached is not None:
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_text(body or "", encoding="utf-8")
    return body


def scan(event_path: Path | str) -> dict:
    """扫出窗口不完整的 putback 事件, 并按"是否带着退化的起始日"分档。"""
    store = CBEventStore(event_path)
    events = store.list_events()
    putbacks = [e for e in events if e.event_type == "putback"]
    incomplete = [e for e in putbacks if not putback_window_is_complete(e)]
    degraded = [e for e in incomplete if putback_start_is_degraded(e)]
    return {
        "n_events": len(events),
        "n_putback": len(putbacks),
        "complete": len(putbacks) - len(incomplete),
        "incomplete": incomplete,
        "degraded": degraded,
        "no_url": [e for e in incomplete if not e.url],
    }


def _repaired(event: CBEvent, parsed: dict) -> CBEvent | None:
    """按解析结果算出这条事件该变成什么; 无需改动时返回 None。"""
    start, end = parsed.get("start"), parsed.get("end")
    price = parsed.get("price")
    updates: dict = {}
    if start is not None and end is not None:
        # 只有起止俱全才算真窗口 —— 半个窗口和没有窗口一样不可用, 而且更容易骗人
        if event.effective_start != start:
            updates["effective_start"] = start
        if event.effective_end != end:
            updates["effective_end"] = end
    elif putback_start_is_degraded(event):
        # 补不回来, 至少别再拿公告日冒充窗口起始日
        updates["effective_start"] = None
    if price is not None and event.event_price is None:
        updates["event_price"] = float(price)
    return replace(event, **updates) if updates else None


def repair(
    event_path: Path | str,
    *,
    dry_run: bool = True,
    backup: bool = True,
    download: bool = False,
    cache_dir: Path | None = None,
    limit: int | None = None,
    delay: float = DEFAULT_DELAY_SECONDS,
    progress=None,
) -> dict:
    event_path = Path(event_path)
    fingerprint = event_path.read_bytes() if event_path.exists() else b""
    report = scan(event_path)

    targets = [e for e in report["incomplete"] if e.url]
    if limit is not None:
        targets = targets[:limit]

    plan: dict[tuple, CBEvent] = {}
    stats = collections.Counter()
    for index, event in enumerate(targets, 1):
        body = fetch_body(event.url, cache_dir, download=download)
        if body is None:
            stats["正文取不到"] += 1
        else:
            parsed = parse_putback_terms(body)
            fixed = _repaired(event, parsed)
            if fixed is None:
                stats["正文有但解析不出窗口"] += 1
            else:
                plan[event.key()] = fixed
                if fixed.effective_end is not None:
                    stats["补回完整窗口"] += 1
                else:
                    stats["清掉退化起始日"] += 1
        if progress is not None:
            progress(index, len(targets), event)
        # 只有真发生了网络请求才需要限速; 命中缓存时不必等
        if download and delay > 0 and body is not None:
            time.sleep(delay)

    # 正文取不到的退化记录也要洗 —— 谎言不因为拿不到证据就变成真的
    for event in report["degraded"]:
        if event.key() in plan:
            continue
        plan[event.key()] = replace(event, effective_start=None)
        stats["清掉退化起始日(无正文)"] += 1

    result = {
        **report,
        "stats": dict(stats),
        "planned": len(plan),
        "changed": 0,
        "backup_path": None,
    }
    if not plan or dry_run:
        return result

    if (event_path.read_bytes() if event_path.exists() else b"") != fingerprint:
        raise ConcurrentWriteError(f"{event_path} 在扫描后被改动, 已放弃写入; 请重跑")
    backup_path = None
    if backup:
        backup_path = event_path.with_suffix(
            f".bak-putback-{datetime.now():%Y%m%d%H%M%S}.json")
        shutil.copy2(event_path, backup_path)

    changed, _removed = CBEventStore(event_path).rewrite(
        lambda event: plan.get(event.key(), event), dry_run=False)
    result["changed"] = changed
    result["backup_path"] = backup_path
    return result


# ── cb_data 侧的残留 ────────────────────────────────────────────────────────
#
# 回洗事件表还不够: ``apply_events_to_terms`` 只**加**更新, 从不清字段, 于是事件里
# 那个假窗口消失之后, cb_data.json 里已经写死的 putback_start_date 仍会留着。
# 而 putback_* 三个字段的注释写明是"**已公告**回售申报起始日" —— Wind 不提供, 只能
# 来自事件层, 所以"事件不支持了"就等于"这个值没有来源了", 该清。
#
# 只做这一次: 解析侧已经不会再产生假窗口, 之后 apply_events 也就不会再写进来。

PUTBACK_BUNDLE_FIELDS = ("putback_start_date", "putback_end_date", "putback_price")


def _bundle_changes(terms, rebuilt) -> dict:
    """cb_data 该跟着事件改哪几个 putback 字段。**只在有明确证据时才动**。

    两个方向不对称, 因为证据强度不对称:

      写回  事件里有**完整窗口** (起止俱全) 且与现值不同 → 直接以事件为准。
            这是正证据, 覆盖旧值没有风险。

      清空  只有当现值本身就是那个**退化签名** (有起始日、没有截止日) 且事件也给不出
            窗口时才清。事件**缺席不是证据**: 实测聚合转债 / 恒逸转2 的 cb_data 存着
            完整且合理的窗口, 而事件表里一条 putback 都没有 (多半是被兄弟债回洗清掉了
            源公告)。按"没有事件就清空"处理会把这类正确数据一并销毁 —— 与本仓库
            「字段明确才剔除」的保守过滤是同一条原则。
    """
    new_start = getattr(rebuilt, "putback_start_date", None)
    new_end = getattr(rebuilt, "putback_end_date", None)
    old_start = getattr(terms, "putback_start_date", None)
    old_end = getattr(terms, "putback_end_date", None)

    if new_start is not None and new_end is not None:
        changes = {}
        if old_start != new_start:
            changes["putback_start_date"] = new_start
        if old_end != new_end:
            changes["putback_end_date"] = new_end
        new_price = getattr(rebuilt, "putback_price", None)
        if new_price is not None and getattr(terms, "putback_price", None) != new_price:
            changes["putback_price"] = new_price
        return changes

    if old_start is not None and old_end is None:
        # 退化签名: 有起始日没截止日, 而事件给不出窗口 → 那个起始日是公告日冒充的
        return {"putback_start_date": None}
    return {}


def scan_bundle(event_path: Path | str, bundle_path: Path | str) -> list[dict]:
    """cb_data 的 putback 字段与**回洗后的事件**对不上的地方。

    做的是差异同步而不是单纯清空: 补回窗口的那 244 条要写回去, 只剩配套文件的那些
    要清掉。两件事是同一个动作的两面 —— "让 cb_data 等于事件重放的结果"。
    """
    from dataclasses import replace as _replace

    from ..cache import TermsBundle
    from ..cb_events import apply_events_to_terms

    store = CBEventStore(event_path)
    bundle = TermsBundle(bundle_path)
    diffs: list[dict] = []
    for code in bundle.list_bonds():
        terms = bundle.get(code)
        if terms is None:
            continue
        # 先把三个字段清空再让事件重放 —— 否则旧值会原样留在 patched 里, 看不出差异
        cleared = _replace(terms, **{f: None for f in PUTBACK_BUNDLE_FIELDS})
        rebuilt = apply_events_to_terms(code, cleared, store.list_events(bond_code=code))
        changes = _bundle_changes(terms, rebuilt)
        if changes:
            diffs.append({
                "bond_code": code,
                "bond_name": str(getattr(terms, "sec_name", "") or ""),
                "fields": sorted(changes),
                "old": {f: getattr(terms, f, None) for f in changes},
                "new": changes,
            })
    return diffs


def sync_bundle(event_path: Path | str, bundle_path: Path | str, *,
                dry_run: bool = True, backup: bool = True) -> dict:
    from dataclasses import replace as _replace

    from ..cache import TermsBundle

    diffs = scan_bundle(event_path, bundle_path)
    result = {"stale": diffs, "updated": 0, "backup_path": None}
    if not diffs or dry_run:
        return result

    bundle_path = Path(bundle_path)
    backup_path = None
    if backup and bundle_path.exists():
        backup_path = bundle_path.with_suffix(
            f".bak-putback-{datetime.now():%Y%m%d%H%M%S}.json")
        shutil.copy2(bundle_path, backup_path)

    bundle = TermsBundle(bundle_path)
    items = []
    for row in diffs:
        terms = bundle.get(row["bond_code"])
        if terms is None:
            continue
        items.append((row["bond_code"], _replace(terms, **row["new"])))
    if items:
        bundle.set_many(items, source="cb_events")
    result["updated"] = len(items)
    result["backup_path"] = backup_path
    return result


def _print_report(report: dict, *, downloaded: bool) -> None:
    print(f"事件总数 {report['n_events']}, 其中 putback {report['n_putback']} 条")
    print(f"  窗口完整      {report['complete']}")
    print(f"  窗口不完整    {len(report['incomplete'])}"
          f" (其中起始日是退化的公告日 {len(report['degraded'])} 条,"
          f" 无 URL 无法重取 {len(report['no_url'])} 条)")
    if report["stats"]:
        print("\n本次处理:")
        for reason, count in sorted(report["stats"].items(), key=lambda kv: -kv[1]):
            print(f"  {reason:24s} {count}")
    if not downloaded:
        print("\n未开 --download: 只用了盘上缓存的正文。加 --download 重新抓取公告 PDF。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="回洗回售事件里缺失/退化的申报窗口")
    parser.add_argument("--apply", action="store_true", help="真正写盘 (默认只预览)")
    parser.add_argument("--download", action="store_true",
                        help="重新抓取公告 PDF 正文 (默认只读盘上缓存)")
    parser.add_argument("--no-backup", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--event-path", default=None)
    parser.add_argument("--bundle-path", default=None)
    parser.add_argument("--sync-bundle", action="store_true",
                        help="回洗事件后, 一并清掉 cb_data 里失去事件支撑的 putback 字段")
    parser.add_argument("--cache-dir", default=None,
                        help="正文缓存目录 (默认 data/announcement_text_cache)")
    parser.add_argument("--no-cache", action="store_true", help="不读也不写正文缓存")
    parser.add_argument("--limit", type=int, default=None,
                        help="只处理前 N 条 (调试用)")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY_SECONDS,
                        help="两次下载之间的间隔秒数")
    args = parser.parse_args(argv)

    event_path = Path(args.event_path) if args.event_path else project_events_path()
    cache_dir = None
    if not args.no_cache:
        cache_dir = Path(args.cache_dir) if args.cache_dir else default_cache_dir()

    def progress(index: int, total: int, event) -> None:
        if args.json:
            return
        if index == 1 or index % 25 == 0 or index == total:
            print(f"  [{index}/{total}] {event.bond_code} {event.event_date}",
                  file=sys.stderr, flush=True)

    try:
        report = repair(
            event_path,
            dry_run=not args.apply,
            backup=not args.no_backup,
            download=args.download,
            cache_dir=cache_dir,
            limit=args.limit,
            delay=args.delay,
            progress=progress,
        )
    except ConcurrentWriteError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps({
            "n_events": report["n_events"],
            "n_putback": report["n_putback"],
            "complete": report["complete"],
            "incomplete": len(report["incomplete"]),
            "degraded": len(report["degraded"]),
            "stats": report["stats"],
            "planned": report["planned"],
            "changed": report["changed"],
            "backup_path": str(report["backup_path"]) if report["backup_path"] else None,
        }, ensure_ascii=False, indent=2))
        return 0

    _print_report(report, downloaded=args.download)

    if args.sync_bundle:
        # 顺序要紧: 先洗事件再同步 bundle。反过来的话那批假窗口还"支撑"着 cb_data,
        # 扫出来的残留就是空的 —— 看着干净, 实际什么都没修。
        from ..cache import project_bundle_path

        bundle_path = Path(args.bundle_path) if args.bundle_path else project_bundle_path()
        bundle_report = sync_bundle(event_path, bundle_path,
                                    dry_run=not args.apply, backup=not args.no_backup)
        stale = bundle_report["stale"]
        print(f"\ncb_data 的 putback 字段需要跟事件对齐: {len(stale)} 只")
        for row in stale[:10]:
            print(f"  {row['bond_code']:10s} {row['bond_name']:9s} "
                  + ", ".join(f"{f}: {row['old'][f]} → {row['new'][f]}"
                              for f in row["fields"]))
        if len(stale) > 10:
            print(f"  ... 另有 {len(stale) - 10} 只")
        if args.apply and bundle_report["updated"]:
            print(f"✅ 已对齐 {bundle_report['updated']} 只的 putback 字段"
                  + (f", 备份 {bundle_report['backup_path']}"
                     if bundle_report["backup_path"] else ""))

    if args.apply:
        print(f"\n✅ 已改写 {report['changed']} 条"
              + (f", 备份 {report['backup_path']}" if report["backup_path"] else ""))
    elif report["planned"]:
        print(f"\n可改写 {report['planned']} 条。确认无误后加 --apply 执行")
    else:
        print("\n没有可改写的条目")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
