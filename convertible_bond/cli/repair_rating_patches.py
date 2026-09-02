"""存量迁移: 用**今天的解析器**重放评级族 patch (评级 / 展望 / 观察状态).

三个字段都只有公告正文一个来源 —— Wind 的 ``creditrating`` 是发行时冻结值,
``ratingoutlook`` 实测取不到 —— 所以解析器的历次 bug 全部原样沉淀在 patch 库里, 而存量
patch 不会被任何流程重新审视。

实测三类脏数据:

  - **评级偏低**: ``rating_re`` 早期缺 ``(?<![A-C])`` 左界, ``.{0,10}`` 回溯会让评级
    "尽量晚开始", 从 AA- 抠出 A-、从 AA+ 抠出 A+。拿 akshare 第三方当裁判, 体检标记的
    17 条分歧里 **15 条是公告 patch 错、cb_data 对**。
  - **观察名单假阳性**: 跟踪评级报告末尾的"评级符号设置及含义"附录把"列入正面观察名单"
    逐个列一遍, 早期正则全文搜关键词就命中了词表。实测主池 51 只带观察状态的债里 47 只
    的值来自那段附录, 真正来自专项公告的只有 4 只。
  - **展望**: 986 条里 983 条的值取自正文而非标题, 其中 892 条来自跟踪评级报告 —— 与
    观察状态同一批文档、同一段附录, 必须一并重放才能确认。

**为什么可以重放, 而 AGENTS.md 说"cb-repair-events 不碰评级"**: 那条说的是不能用**架在
数值上的启发式**去猜哪条存量 patch 是 bug 造成的 (解析 bug 的错误方向"偏低"与真实下调的
方向完全重合, 分不开)。本工具不猜数值 —— 它把公告正文重新取回来, 用当前解析器**重新推导**,
是换了一个独立证据源, 不是在旧数值上做判断。实测当前解析器在样本上从不产生错值: 要么解析对
(3/6 直接纠正了存量错值), 要么返回 None (3/6 安全失败)。

三种处置, 方向由证据强度决定::

    解析出新值且与存量不同  → 改写 (正证据, 直接以重放结果为准)
    解析不出 (None)         → **删掉该字段** (当前解析器无法确认, 留着就是无源之水)
    正文取不到              → 原样保留 (取不到证据 ≠ 证据为否)

用法::

    cb-repair-rating-patches                      # 只用已缓存正文, 出报告
    cb-repair-rating-patches --download           # 允许联网补正文 (可中断续跑)
    cb-repair-rating-patches --download --apply   # 真正写盘 (先自动备份)
"""
from __future__ import annotations

import argparse
import collections
import json
import shutil
import sys
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from ..cb_event_sync import parse_credit_rating_terms
from ..cb_events import CBEventStore, project_events_path
from ..historical_terms import TermsPatchStore, project_terms_patches_path
from .repair_putback_windows import (
    DEFAULT_DELAY_SECONDS,
    ConcurrentWriteError,
    default_cache_dir,
    fetch_body,
)

RATING_FIELDS = ("credit_rating", "credit_rating_outlook", "credit_watch_status")


def _url_index(event_path: Path | str) -> tuple[dict[tuple, str], dict[tuple, str]]:
    """返回两级索引: ``(代码, 标题, 公告日) → url`` 和 ``(代码, 标题) → url``。

    **日期必须进主键**。发行人会用**一模一样的标题**发多份公告 —— 定期跟踪评级报告
    每年一份, 标题里连年份都不带 (「关于"皖天转债"定期跟踪评级结果的公告」)。实测
    评级类事件 1298 条里 ``(代码, 标题)`` 只有 1197 个不同键、**77 组撞车**
    (123192.SZ 有 3 份同名), 加上公告日之后 1298 个键、**0 组撞车**。
    键撞车 + ``setdefault`` = 整条评级链上每条 patch 都被接到**第一份** PDF 上, 重放
    出来是同一个 (错年份的) 评级 —— 实测 1037 条评级 patch 里 **62 条 (6.0%)** 会接错,
    而这个命令把重放结果**直接写回库**。这与 AGENTS 记的"标题点名了兄弟债"是同一类
    归属错误, 只是这次撞的是同一只债的不同年份。

    第二级只收**该债下标题唯一**的那些: 老 patch 可能没有 ``event_date`` (那个字段是
    后加的), 对它们退回按标题查是安全的; 而标题本身就撞车、又没有日期可消歧时,
    这里**查不到**才是正确结果 —— 让它落进 ``no_url``, 命令原样保留那条 patch。
    "取不到证据 ≠ 证据为否"。
    """
    exact: dict[tuple, str] = {}
    urls_by_title: dict[tuple, set[str]] = {}
    for event in CBEventStore(event_path).list_events():
        if not (event.url and event.raw_title):
            continue
        exact.setdefault((event.bond_code, event.raw_title, event.event_date), event.url)
        urls_by_title.setdefault((event.bond_code, event.raw_title), set()).add(event.url)
    unique = {k: next(iter(v)) for k, v in urls_by_title.items() if len(v) == 1}
    return exact, unique


def _lookup_url(exact: dict, unique: dict, patch) -> str | None:
    title = patch.raw_title or ""
    if patch.event_date is not None:
        hit = exact.get((patch.bond_code, title, patch.event_date))
        if hit:
            return hit
    return unique.get((patch.bond_code, title))


def scan(patches_path: Path | str, event_path: Path | str) -> dict:
    store = TermsPatchStore(patches_path)
    exact_urls, unique_urls = _url_index(event_path)
    targets, no_url = [], []
    # ``include_shadowed=True`` 是必须的, 两个原因: ① 回洗要的是**文件里到底有什么**,
    # 被权威源遮蔽的脏 patch 否则既扫不到也删不掉; ② 遮蔽视图返回的是 ``replace(patch,
    # fields=...)`` 的副本, 它的 ``key()`` 与磁盘上那条不同 —— 拿它当计划的键, rewrite
    # 时一条都对不上。
    for patch in store.list_patches(include_shadowed=True):
        if not any(f in (patch.fields or {}) for f in RATING_FIELDS):
            continue
        url = _lookup_url(exact_urls, unique_urls, patch)
        (targets if url else no_url).append((patch, url))
    return {
        "n_patches": len(store.list_patches(include_shadowed=True)),
        "targets": [(p, u) for p, u in targets],
        "no_url": [p for p, _ in no_url],
    }


def _repaired(patch, parsed: dict) -> tuple[object | None, collections.Counter]:
    """按重放结果算出这条 patch 该变成什么; 无需改动时返回 ``(None, stats)``。"""
    stats: collections.Counter = collections.Counter()
    fields = dict(patch.fields or {})
    for name in RATING_FIELDS:
        if name not in fields:
            continue
        new = parsed.get(name)
        if new is None:
            del fields[name]
            stats[f"删字段·{name}"] += 1
        elif str(new) != str(fields[name]):
            stats[f"改写·{name}: {fields[name]}→{new}"] += 1
            fields[name] = new
    if fields == (patch.fields or {}):
        return None, stats
    return replace(patch, fields=fields), stats


def repair(
    patches_path: Path | str | None = None,
    event_path: Path | str | None = None,
    *,
    dry_run: bool = True,
    backup: bool = True,
    download: bool = False,
    cache_dir: Path | None = None,
    limit: int | None = None,
    delay: float = DEFAULT_DELAY_SECONDS,
    progress=None,
) -> dict:
    patches_path = Path(patches_path or project_terms_patches_path())
    event_path = Path(event_path or project_events_path())
    fingerprint = patches_path.read_bytes() if patches_path.exists() else b""
    report = scan(patches_path, event_path)

    targets = report["targets"]
    if limit is not None:
        targets = targets[:limit]

    plan: dict[tuple, object | None] = {}
    stats: collections.Counter = collections.Counter()
    details: list[str] = []
    for index, (patch, url) in enumerate(targets, 1):
        body = fetch_body(url, cache_dir, download=download)
        if body is None:
            stats["正文取不到(原样保留)"] += 1
        else:
            # 与解析侧完全同构: sync 也是 ``parse_credit_rating_terms(body or title, ...)``。
            # 两边判据分叉正是上一轮余额回洗踩过的坑。
            parsed = parse_credit_rating_terms(
                body or (patch.raw_title or ""), title=patch.raw_title or "")
            fixed, row_stats = _repaired(patch, parsed)
            stats.update(row_stats)
            if fixed is None:
                stats["重放后无变化"] += 1
            elif not fixed.fields:
                plan[patch.key()] = None          # 字段全没了 → 整条删除
                stats["整条删除(无字段剩余)"] += 1
                details.append(f"删 {patch.bond_code} {patch.effective_date} "
                               f"{patch.fields} «{(patch.raw_title or '')[:26]}»")
            else:
                plan[patch.key()] = fixed
                stats["改写"] += 1
                details.append(f"改 {patch.bond_code} {patch.effective_date} "
                               f"{patch.fields} → {fixed.fields}")
        if progress is not None:
            progress(index, len(targets), patch)
        if download and delay > 0 and body is not None:
            time.sleep(delay)

    result = {
        "n_patches": report["n_patches"],
        "n_targets": len(report["targets"]),
        "n_scanned": len(targets),
        "no_url": len(report["no_url"]),
        "stats": dict(stats),
        "details": details,
        "planned": len(plan),
        "changed": 0,
        "removed": 0,
        "backup_path": None,
    }
    if not plan or dry_run:
        return result

    if (patches_path.read_bytes() if patches_path.exists() else b"") != fingerprint:
        raise ConcurrentWriteError(f"{patches_path} 在扫描后被改动, 已放弃写入; 请重跑")
    backup_path = None
    if backup:
        backup_path = patches_path.with_suffix(
            f".bak-ratings-{datetime.now():%Y%m%d%H%M%S}.json")
        shutil.copy2(patches_path, backup_path)

    changed, removed = TermsPatchStore(patches_path).rewrite(
        lambda p: plan[p.key()] if p.key() in plan else p, dry_run=False)
    result["changed"] = changed
    result["removed"] = removed
    result["backup_path"] = str(backup_path) if backup_path else None
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="用当前解析器重放评级/展望/观察状态 patch",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    parser.add_argument("--apply", action="store_true", help="真正写盘 (默认只预览)")
    parser.add_argument("--download", action="store_true", help="允许联网补正文 (可中断续跑)")
    parser.add_argument("--no-backup", action="store_true")
    parser.add_argument("--limit", type=int, default=None, help="只处理前 N 条 (调试用)")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY_SECONDS)
    parser.add_argument("--cache-dir", default=None, help="正文缓存目录")
    parser.add_argument("--patches-path", default=None)
    parser.add_argument("--events-path", default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--limit-show", type=int, default=20)
    args = parser.parse_args(argv)

    start = time.time()

    def progress(i, total, patch):
        if i % 50 == 0 or i == total:
            rate = i / max(time.time() - start, 1e-6)
            print(f"  [{i:>4}/{total}] {patch.bond_code:<12} {rate:.1f}/s "
                  f"ETA {(total - i) / max(rate, 1e-6):.0f}s", flush=True)

    report = repair(
        args.patches_path, args.events_path,
        dry_run=not args.apply, backup=not args.no_backup,
        download=args.download,
        cache_dir=Path(args.cache_dir) if args.cache_dir else default_cache_dir(),
        limit=args.limit, delay=args.delay, progress=progress,
    )
    if args.json:
        print(json.dumps({k: v for k, v in report.items() if k != "details"},
                         ensure_ascii=False, indent=2))
        return 0

    print(f"\npatch 库 {report['n_patches']} 条; 评级族且能接回源公告的 "
          f"{report['n_targets']} 条 (无 url {report['no_url']} 条)")
    print(f"本次重放 {report['n_scanned']} 条:")
    for key, n in sorted(report["stats"].items(), key=lambda kv: -kv[1])[:args.limit_show]:
        print(f"   {n:5d}  {key}")
    if report["details"]:
        print(f"\n明细 (前 {args.limit_show} 条):")
        for row in report["details"][:args.limit_show]:
            print("  ", row)
        if len(report["details"]) > args.limit_show:
            print(f"   … 另有 {len(report['details']) - args.limit_show} 条")
    if report["backup_path"]:
        print(f"\n已备份: {report['backup_path']}")
        print(f"已改写 {report['changed']} 条, 删除 {report['removed']} 条")
    elif report["planned"]:
        print(f"\n[预览] 待改 {report['planned']} 条。确认无误后加 --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
