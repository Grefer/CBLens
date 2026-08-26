"""刷新新债的上市日 (窄同步, 不需要 Wind).

``listing_date`` 全库只有全量条款同步一条写入通道, 而新债挂牌是**每天**发生的事 ——
两次全量同步之间, 新债就一直挂着空上市日, 既进不了主池也在"扫新债"里显示"待定"。
这条命令只碰那几只新债 (实测每天 4 只上下), 一次 ``ak.bond_zh_cov()`` 秒级完成。

设计约定与实测依据见 :mod:`convertible_bond.new_issue_sync` 的模块 docstring。

每天跑, 放在状态刷新旁边::

    cb-sync-new-issues              # 预览
    cb-sync-new-issues --apply
"""
from __future__ import annotations

import argparse
import json
import sys

from ..new_issue_sync import sync_new_issues


def _fmt(value) -> str:
    return "—" if value is None else str(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="从 akshare 刷新新债上市日 (窄同步, 不重建条款, 不需要 Wind)")
    parser.add_argument("--apply", action="store_true", help="真正写盘 (默认只预览)")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--bundle-path", default=None)
    args = parser.parse_args(argv)

    try:
        report = sync_new_issues(args.bundle_path, dry_run=not args.apply)
    except Exception as exc:      # 网络/上游字段改名 — 报错要能一眼看出是取数失败而不是"没新债"
        print(f"❌ 取新债清单失败: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return 0

    changes = report["changes"]
    listed = [c for c in changes if c["kind"] == "listing_date"]
    stubs = [c for c in changes if c["kind"] == "new_bond"]
    fills = [c for c in changes if c["kind"] == "fill"]

    print(f"估值日 {report['on_date']} · 第三方清单 {report['n_listings']} 只 · "
          f"在盯新债 {report['n_tracked']} 只"
          + ("" if args.apply else "  [预览, 未写盘]"))
    if not changes:
        print("  无变化 — 新债的上市日都已是最新")
        return 0

    for row in listed:
        print(f"  📅 {row['bond_code']} {str(row['bond_name'] or ''):<12} "
              f"上市日 {_fmt(row['before'])} → {row['after']}")
    for row in stubs:
        print(f"  🆕 {row['bond_code']} {str(row['bond_name'] or ''):<12} "
              f"新建占位档 (上市日 {_fmt(row['after'])}) — 完整条款待下次 cb-sync-tradable")
    if fills:
        print(f"  🩹 补齐 {len(fills)} 个本地为空的元信息字段: "
              + ", ".join(sorted({row["field"] for row in fills})))
    if not args.apply:
        print("\n确认无误后加 --apply 执行")
    return 0


if __name__ == "__main__":
    sys.exit(main())
