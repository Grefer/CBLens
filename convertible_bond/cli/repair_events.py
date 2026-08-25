"""存量迁移: 用**今天的解析器**重放事件表与 patch 库, 修掉两者的不自洽。

cninfo 按**发行人**返回公告, 同一发行人发过两只转债时, 兄弟债的公告会被逐条挂到本债上。
`cb_event_sync._title_names_other_bond` 已在解析入口拦下这类公告, 但它是后加的 ——
守卫上线前落库的 patch 仍在库里, 且不会再被任何流程重新审视。

实测存量 66 条 / 9 只债:

  - 62 条 ``call_redemption_price``: 兄弟债正在强赎, 赎回价被写到**没有被强赎**的本债上。
    历史投影会让 pricer 以为该债即将按 100.29 赎回, 期权价值被整段削平 —— 回测里静默失真。
  - 4 条 ``credit_rating_outlook``: 同发行人展望本就一致, 无实质影响, 但同样是错归属。

**事件表才是重灾区**: 守卫原本只挡 patch, 而事件本身早已落库, 再经
``apply_events_to_bundle`` 回写 ``last_trading_date`` / ``delisting_date`` /
``call_redemption_price``。实测一次 ``cb-sync-events --apply`` 把 15 只**在市**转债的摘牌日
从未来改成过去 (胜蓝转02 2031-08-28 → 2024-12-12, 而它当天成交 216 万手、报价 326 元),
准入随即把这批券整体判成"已退市" —— 主池 279 只里被静默抹掉 19 只, 全是高价活跃品种。

第二类不自洽是**误分类**: 「摘牌」「提前赎回」在 A 股公告里是多义词, 早期分类器只看关键词
不看主语, 把优先股赎回摘牌、产权交易所公开摘牌、普通公司债兑付摘牌、可交换债换股摘牌、
理财产品提前赎回统统判成本转债的摘牌/强赎 —— 又有 12 只在市转债 (含兴业、上银两只银行
转债) 被准入判死。``classify_announcement_title`` 现已加了"必须提到转债"的闸, 但存量事件
不会被任何流程重新审视。

所以本工具对每条存量事件做两件事:

1. **归属**: 标题点名了兄弟债 → 删除 (判据与解析侧共用 ``_title_names_other_bond``)。
2. **分类**: 用当前 ``classify_announcement_title`` 重判 ``raw_title``, 与存量类型不符则
   改判; 重判为 ``unknown`` 则删除。

改完事件表还要把 cb_data 的状态字段恢复成 Wind 口径再重放 —— ``apply_events_to_bundle``
只会写不会撤, 删掉错事件并不能自动把它写坏的摘牌日改回来。

判据与解析侧完全共用 ``_title_names_other_bond``, 不另立一套 —— 两边判据分叉正是上一轮
余额回洗踩过的坑。
"""
from __future__ import annotations

import argparse
import collections
import json
import shutil
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from ..cache import TermsBundle, project_bundle_path
from ..cb_event_sync import _title_names_other_bond
from ..cb_events import (
    CBEventStore,
    _event_postdates_listing,
    classify_announcement_title,
    project_events_path,
)
from ..historical_terms import TermsPatchStore, project_terms_patches_path


class ConcurrentWriteError(RuntimeError):
    """扫描后、写盘前 patch 文件被别的进程改过。"""


def _bond_name(bundle: TermsBundle, code: str) -> str:
    return str(getattr(bundle.get(code), "sec_name", "") or "").replace("(退市)", "").strip()


def scan_events(event_path: Path | str, bundle_path: Path | str) -> dict:
    """扫事件表里标题点名了兄弟债的条目。

    比 patch 更要紧: 事件会经 ``apply_events_to_bundle`` 回写 cb_data 的摘牌/强赎字段。
    """
    store = CBEventStore(event_path)
    bundle = TermsBundle(bundle_path)
    hits: list = []
    relabel: list = []
    for event in store.list_events():
        if _title_names_other_bond(event.raw_title, _bond_name(bundle, event.bond_code)):
            hits.append(event)
            continue
        # 上市之前不可能发生本债的摘牌/强赎/回售/转股价调整。消费侧
        # (``apply_events_to_terms``) 已经按同一判据过滤, 这里把存量也清掉 —— 库里留着
        # 错事实, 迟早有个不走那条路径的消费者踩上去。评级与正股类事件已在判据里豁免。
        terms = bundle.get(event.bond_code)
        if terms is not None and not _event_postdates_listing(event, terms):
            hits.append(event)
            continue
        fresh = classify_announcement_title(event.raw_title or "")
        if fresh != event.event_type:
            relabel.append((event, fresh))
    by_code: dict[str, dict] = {}
    for event in hits:
        row = by_code.setdefault(event.bond_code, {
            "bond_code": event.bond_code,
            "bond_name": _bond_name(bundle, event.bond_code),
            "n_events": 0,
            "types": collections.Counter(),
            "sample_title": event.raw_title,
        })
        row["n_events"] += 1
        row["types"][event.event_type] += 1
    moves = collections.Counter(f"{e.event_type} → {new}" for e, new in relabel)
    return {"n_events": len(store.list_events()), "hits": hits, "relabel": relabel,
            "moves": dict(moves.most_common()),
            "audit": [{**row, "types": dict(row["types"])} for row in by_code.values()]}


def repair_events(event_path: Path | str, bundle_path: Path | str, *,
                  dry_run: bool = True, backup: bool = True) -> dict:
    event_path = Path(event_path)
    fingerprint = event_path.read_bytes() if event_path.exists() else b""
    report = scan_events(event_path, bundle_path)
    drop = {e.key() for e in report["hits"]}
    # 重判为 unknown 的一并删除 —— 留着只会让"事件表条数"这个指标虚高。
    drop |= {e.key() for e, fresh in report["relabel"] if fresh == "unknown"}
    retype = {e.key(): fresh for e, fresh in report["relabel"] if fresh != "unknown"}
    if (not drop and not retype) or dry_run:
        return {**report, "removed": len(drop), "retyped": len(retype), "backup_path": None}

    if (event_path.read_bytes() if event_path.exists() else b"") != fingerprint:
        raise ConcurrentWriteError(f"{event_path} 在扫描后被改动, 已放弃写入; 请重跑")
    backup_path = None
    if backup:
        backup_path = event_path.with_suffix(
            f".bak-events-{datetime.now():%Y%m%d%H%M%S}.json")
        shutil.copy2(event_path, backup_path)

    def _fix(event):
        if event.key() in drop:
            return None
        fresh = retype.get(event.key())
        return replace(event, event_type=fresh) if fresh else event

    changed, removed = CBEventStore(event_path).rewrite(_fix, dry_run=False)
    return {**report, "removed": removed, "retyped": changed, "backup_path": backup_path}


def scan(patch_path: Path | str, bundle_path: Path | str) -> dict:
    store = TermsPatchStore(patch_path)
    bundle = TermsBundle(bundle_path)
    hits = []
    for patch in store.list_patches(include_shadowed=True):
        if patch.source == "wind_asof":          # 权威源不带标题, 也不会串号
            continue
        name = _bond_name(bundle, patch.bond_code)
        if not name or not _title_names_other_bond(patch.raw_title, name):
            continue
        hits.append(patch)
    by_code: dict[str, dict] = {}
    for patch in hits:
        row = by_code.setdefault(patch.bond_code, {
            "bond_code": patch.bond_code,
            "bond_name": _bond_name(bundle, patch.bond_code),
            "n_patches": 0,
            "fields": collections.Counter(),
            "sample_title": patch.raw_title,
        })
        row["n_patches"] += 1
        row["fields"].update((patch.fields or {}).keys())   # 计字段出现次数, 不是累加数值
    return {
        "n_patches": len(store.list_patches(include_shadowed=True)),
        "hits": hits,
        "audit": [{**row, "fields": dict(row["fields"])} for row in by_code.values()],
    }


def repair(patch_path: Path | str, bundle_path: Path | str, *,
           dry_run: bool = True, backup: bool = True) -> dict:
    patch_path = Path(patch_path)
    # 指纹必须在扫描**之前**取: 取在扫描之后, 并发窗口正好落在指纹与比对之间, 守卫恒为真。
    fingerprint = patch_path.read_bytes() if patch_path.exists() else b""
    report = scan(patch_path, bundle_path)
    drop = {p.key() for p in report["hits"]}
    # 评级**不动**: 解析 bug 的错误方向 (后缀残缺 → 评级偏低) 与真实下调的方向完全重合,
    # 任何架在数值上的启发式都分不开二者。而实测 patch 末条对第三方评级的精确命中率
    # 88.4% > cb_data 的 79.5% (平均档位误差 0.260 vs 0.534) —— 清洗它等于拿更准换更不准。
    strip: set = set()
    if not drop and not strip:
        return {**report, "removed": 0, "backup_path": None}
    if dry_run:
        return {**report, "removed": len(drop) + len(strip), "backup_path": None}

    # 扫描与写盘之间若有别的进程落盘 (GUI 后台同步), 整份重写会把它的成果吞掉。
    if (patch_path.read_bytes() if patch_path.exists() else b"") != fingerprint:
        raise ConcurrentWriteError(f"{patch_path} 在扫描后被改动, 已放弃写入; 请重跑")

    backup_path = None
    if backup:
        backup_path = patch_path.with_suffix(
            f".bak-crossbond-{datetime.now():%Y%m%d%H%M%S}.json")
        shutil.copy2(patch_path, backup_path)

    def _fix(patch):
        if patch.key() in drop:
            return None
        if patch.key() not in strip:
            return patch
        fields = {k: v for k, v in (patch.fields or {}).items() if k != "credit_rating"}
        if not fields:
            return None                      # 整条只有评级 → 删干净
        before = {k: v for k, v in (patch.before_fields or {}).items() if k != "credit_rating"}
        return replace(patch, fields=fields, before_fields=before or None)

    store = TermsPatchStore(patch_path)
    changed, removed = store.rewrite(_fix, dry_run=False)
    return {**report, "removed": removed + changed, "backup_path": backup_path}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="清洗标的串号的事件与条款 patch")
    parser.add_argument("--apply", action="store_true", help="真正写盘 (默认只预览)")
    parser.add_argument("--no-backup", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--patch-path", default=None)
    parser.add_argument("--bundle-path", default=None)
    parser.add_argument("--event-path", default=None)
    args = parser.parse_args(argv)

    bundle_path = args.bundle_path or project_bundle_path()
    ev = repair_events(
        args.event_path or project_events_path(), bundle_path,
        dry_run=not args.apply, backup=not args.no_backup,
    )
    report = repair(
        args.patch_path or project_terms_patches_path(),
        bundle_path,
        dry_run=not args.apply,
        backup=not args.no_backup,
    )
    if args.json:
        print(json.dumps({"events": {k: v for k, v in ev.items() if k != "hits"},
                          "patches": {k: v for k, v in report.items() if k != "hits"}},
                         ensure_ascii=False, indent=2, default=str))
        return 0

    print(f"扫描 {ev['n_events']} 条事件: 删除 {ev['removed']} 条 (串号 {len(ev['hits'])} + "
          f"重判为 unknown), 改判 {ev['retyped']} 条; 涉及串号 {len(ev['audit'])} 只债" + ("" if args.apply else "  [预览, 未写盘]"))
    for row in sorted(ev["audit"], key=lambda r: -r["n_events"]):
        print(f"  {row['bond_code']} {row['bond_name']}: {row['n_events']} 条 "
              f"{row['types']}\n       «{(row['sample_title'] or '')[:56]}»")
    if ev["moves"]:
        print("  分类改判分布:")
        for move, n in list(ev["moves"].items())[:12]:
            print(f"     {move}: {n}")
    if ev["backup_path"]:
        print(f"  备份: {ev['backup_path']}")

    print(f"\n扫描 {report['n_patches']} 条 patch: 串号 {len(report['hits'])} 条 / "
          f"{len(report['audit'])} 只债" + ("" if args.apply else "  [预览, 未写盘]"))
    for row in sorted(report["audit"], key=lambda r: -r["n_patches"]):
        print(f"  {row['bond_code']} {row['bond_name']}: {row['n_patches']} 条 "
              f"{row['fields']}\n       «{(row['sample_title'] or '')[:56]}»")
    if report["backup_path"]:
        print(f"\n备份: {report['backup_path']}")
    if not args.apply and (report["removed"] or ev["removed"]):
        print("\n确认无误后加 --apply 执行")
    return 0


if __name__ == "__main__":
    sys.exit(main())
