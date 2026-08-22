"""清洗被门槛条款污染的 ``outstanding_balance`` patch.

``parse_outstanding_balance_change`` 早期版本的宽松模式
(``余额标签 + .{0,24}? + 金额 + 单位``) 会把赎回/回售/摘牌条款里的**门槛表述**
当成当期真实余额, 典型句式::

    当本次发行的可转换公司债券未转股余额少于3,000万元时, 公司有权……赎回

于是 ``outstanding_balance = 0.3`` (亿) 被成批写进 ``cb_terms_patches.json``。
这些 patch 经 ``project_terms`` 覆盖回条款后, 会让准入过滤的
``min_outstanding_balance`` (默认 0.5 亿) 把大盘券整批误杀。

解析侧已在 :func:`convertible_bond.cb_event_sync.parse_outstanding_balance_change`
修复 (按措辞而非数值判别, 真实披露的"未转股余额为3,000万元"仍会正常解析)。
本脚本负责回洗**存量**: patch 只存了 note 与 url、没有正文, 无法离线重新解析,
因此按"数值恰为法定门槛 3,000 万元"定位并剔除该字段。

安全性: 剔除的是 ``outstanding_balance`` 这一个 key, patch 里的其它字段
(如 ``call_redemption_price``) 原样保留; 字段被清空的 patch 才整条删除。
写入前先备份原文件, 并校验 patch 库在扫描之后没被别的进程改过。``--dry-run`` 只出报告。

.. warning::
   这是**一次性存量迁移**, 不是日常运维命令。回洗判据是数值 (值恰为 0.3), 而解析侧
   的判据是措辞 —— 真实披露的"未转股余额为 3,000 万元"会被解析侧刻意保留、却会被本
   工具无差别删掉。报告里 ``verdict`` 为"疑似真实披露"的条目 (raw 余额恰等于门槛)
   就是这种情形, ``--apply`` 前必须人工过一眼。存量清完之后不要再定期跑。

用法::

    python -m convertible_bond.cli.repair_balance_patches --dry-run
    python -m convertible_bond.cli.repair_balance_patches --apply
"""
from __future__ import annotations

import argparse
import shutil
import sys
from collections import defaultdict
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from ..cache import TermsBundle, project_bundle_path
from ..historical_terms import TermsPatch, TermsPatchStore, project_terms_patches_path

# 法定摘牌/赎回门槛 3,000 万元 = 0.3 亿。存量脏数据全部落在这个值上
# (实测 546 条余额 patch 中 528 条恰为 0.3, 覆盖 103 只债)。
DEFAULT_THRESHOLD_VALUE = 0.3
_EPS = 1e-9

# 同一数值在 ≥ 该条数的不同公告里重复出现, 多半也是条款引用而非真实余额
# (真实余额随转股进度单调变化)。这一类只报告、不自动删除。
_REPEAT_SUSPICION_MIN = 3


class ConcurrentWriteError(RuntimeError):
    """扫描之后、写盘之前 patch 库被别的进程改过 —— 拒绝写, 免得互相覆盖。"""


def _fingerprint(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _assert_unchanged(path: Path, expected: tuple[int, int] | None) -> None:
    if _fingerprint(path) != expected:
        raise ConcurrentWriteError(
            f"{path} 在扫描之后被改动过 (可能 GUI 正在后台同步公告)。\n"
            f"未做任何写入。请关掉 GUI / 等同步结束后重跑。"
        )


def _balance_of(patch: TermsPatch) -> float | None:
    value = (patch.fields or {}).get("outstanding_balance")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _raw_balance(bundle: TermsBundle, code: str) -> float | None:
    try:
        terms = bundle.get(code)
    except Exception:
        return None
    value = getattr(terms, "outstanding_balance", None) if terms is not None else None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _event_type(patch: TermsPatch) -> str:
    parts = (patch.source_event_key or "").split("|")
    return parts[2] if len(parts) > 2 else "?"


def scan(
    patches_path: Path | None = None,
    bundle_path: Path | None = None,
    *,
    threshold_value: float = DEFAULT_THRESHOLD_VALUE,
) -> dict[str, Any]:
    """扫描 patch 库, 返回命中门槛值的条目与可疑重复值报告 (不写盘)."""
    store = TermsPatchStore(patches_path or project_terms_patches_path())
    bundle = TermsBundle(bundle_path or project_bundle_path())

    balance_patches = [p for p in store.list_patches() if _balance_of(p) is not None]
    hits = [p for p in balance_patches if abs(_balance_of(p) - threshold_value) < _EPS]

    by_bond: dict[str, list[TermsPatch]] = defaultdict(list)
    for patch in hits:
        by_bond[patch.bond_code].append(patch)

    audit: list[dict[str, Any]] = []
    for code, items in sorted(by_bond.items()):
        raw = _raw_balance(bundle, code)
        dates = sorted(p.effective_date for p in items)
        audit.append({
            "bond_code": code,
            "n_patches": len(items),
            "raw_balance": raw,
            "first_date": dates[0],
            "last_date": dates[-1],
            "event_types": sorted({_event_type(p) for p in items}),
            # raw 恰好等于门槛值 = 唯一"可能是真实披露"的情形, 必须单列出来供人工判断,
            # 不能和"没有 raw 可比"混在一起 (回洗判据是数值, 人工复核靠的就是这一栏)。
            "verdict": (
                "疑似真实披露" if raw is not None and abs(raw - threshold_value) < _EPS
                else "真实余额更大" if raw is not None and raw > threshold_value
                else "真实余额更小" if raw is not None and raw < threshold_value
                else "无 raw 余额可比"
            ),
        })

    # 只报告不删除: 同一非门槛数值在多条公告里重复出现
    repeats: list[dict[str, Any]] = []
    grouped: dict[tuple[str, float], list[TermsPatch]] = defaultdict(list)
    for patch in balance_patches:
        value = _balance_of(patch)
        if abs(value - threshold_value) < _EPS:
            continue
        grouped[(patch.bond_code, round(value, 6))].append(patch)
    for (code, value), items in sorted(grouped.items()):
        if len(items) < _REPEAT_SUSPICION_MIN:
            continue
        dates = sorted(p.effective_date for p in items)
        repeats.append({
            "bond_code": code, "value": value, "n_patches": len(items),
            "first_date": dates[0], "last_date": dates[-1],
        })

    return {
        "patches_path": store.path,
        "fingerprint": _fingerprint(store.path),
        "bundle_path": Path(bundle_path or project_bundle_path()),
        "n_balance_patches": len(balance_patches),
        "hits": hits,
        "audit": audit,
        "repeats": repeats,
        "threshold_value": threshold_value,
    }


def repair(
    patches_path: Path | None = None,
    bundle_path: Path | None = None,
    *,
    threshold_value: float = DEFAULT_THRESHOLD_VALUE,
    dry_run: bool = True,
    backup: bool = True,
) -> dict[str, Any]:
    """剔除命中门槛值的 ``outstanding_balance`` 字段, 返回扫描报告 + 改动统计."""
    report = scan(patches_path, bundle_path, threshold_value=threshold_value)
    hit_keys = {p.key() for p in report["hits"]}
    if not hit_keys:
        report.update({"changed": 0, "removed": 0, "backup_path": None})
        return report

    def transform(patch: TermsPatch) -> TermsPatch | None:
        if patch.key() not in hit_keys:
            return patch
        fields = {k: v for k, v in (patch.fields or {}).items()
                  if k != "outstanding_balance"}
        if not fields:
            return None
        return replace(patch, fields=fields)

    store = TermsPatchStore(patches_path or project_terms_patches_path())
    # 并发守卫: rewrite/_save 都是全量重写整份 JSON, 没有锁。若 GUI 后台公告同步在
    # scan 与写盘之间落了一次盘, 那份快照会把回洗成果整份覆盖回去且两边都不报错。
    if not dry_run:
        _assert_unchanged(store.path, report["fingerprint"])
    backup_path: Path | None = None
    if not dry_run and backup and store.path.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = store.path.with_suffix(f".json.bak-{stamp}")
        shutil.copy2(store.path, backup_path)
    changed, removed = store.rewrite(transform, dry_run=dry_run)
    report.update({"changed": changed, "removed": removed, "backup_path": backup_path})
    return report


def _fmt(value: float | None) -> str:
    return "—" if value is None else f"{value:g}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="剔除被赎回门槛条款污染的 outstanding_balance patch")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True,
                      help="只出报告, 不写盘 (默认)")
    mode.add_argument("--apply", action="store_true",
                      help="真正写回 cb_terms_patches.json (先自动备份)")
    parser.add_argument("--threshold-value", type=float, default=DEFAULT_THRESHOLD_VALUE,
                        help=f"判定为门槛条款的余额值, 亿元 (默认 {DEFAULT_THRESHOLD_VALUE})")
    parser.add_argument("--no-backup", action="store_true", help="--apply 时跳过备份")
    parser.add_argument("--patches-path", help="覆盖 cb_terms_patches.json 路径")
    parser.add_argument("--bundle-path", help="覆盖 cb_data.json 路径")
    parser.add_argument("--limit-show", type=int, default=20, help="明细展示条数")
    args = parser.parse_args(argv)

    dry_run = not args.apply
    try:
        result = repair(
            Path(args.patches_path) if args.patches_path else None,
            Path(args.bundle_path) if args.bundle_path else None,
            threshold_value=args.threshold_value,
            dry_run=dry_run,
            backup=not args.no_backup,
        )
    except ConcurrentWriteError as exc:
        print(str(exc), file=sys.stderr)
        return 3

    print(f"Patch 库: {result['patches_path']}")
    print(f"含余额字段的 patch: {result['n_balance_patches']} 条")
    print(f"命中门槛值 {result['threshold_value']:g} 亿: "
          f"{len(result['hits'])} 条 / {len(result['audit'])} 只债")
    if result["audit"]:
        print("\n受影响标的 (raw = cb_data 当前余额, 亿元):")
        print(f"  {'代码':<12}{'条数':>4}  {'raw余额':>10}  {'判定':<14}生效日区间 / 公告类型")
        for row in result["audit"][:args.limit_show]:
            types = ",".join(row["event_types"])
            print(f"  {row['bond_code']:<12}{row['n_patches']:>4}  "
                  f"{_fmt(row['raw_balance']):>10}  {row['verdict']:<14}"
                  f"{row['first_date']}~{row['last_date']}  {types}")
        more = len(result["audit"]) - args.limit_show
        if more > 0:
            print(f"  ... 还有 {more} 只")

    if result["repeats"]:
        print(f"\n[仅提示, 未改动] 同值重复 ≥{_REPEAT_SUSPICION_MIN} 次的余额 patch "
              f"(可能也是条款引用):")
        for row in result["repeats"][:args.limit_show]:
            print(f"  {row['bond_code']:<12}{row['n_patches']:>4} 条  "
                  f"值 {row['value']:g} 亿  {row['first_date']}~{row['last_date']}")

    changed, removed = result["changed"], result["removed"]
    if dry_run:
        print(f"\n[dry-run] 将改写 {changed} 条 (保留其它字段), 删除 {removed} 条整 patch")
        print("确认无误后加 --apply 写回。")
    else:
        if result["backup_path"]:
            print(f"\n已备份: {result['backup_path']}")
        print(f"已改写 {changed} 条, 删除 {removed} 条整 patch")
    return 0


if __name__ == "__main__":
    sys.exit(main())
