"""从第三方 (akshare) 刷新 ``cb_data.credit_rating``.

为什么不用 Wind: 它的 ``creditrating`` 是**发行时值**, 不跟踪年度跟踪评级。实测
``cb_data.json`` 跨 17 个版本 (2026-04 → 08)、约 4000 次逐债 Wind 重取, 该字段变化 **0 次**,
而同批刷新中 ``conversion_price`` 变了 287 次, 区间还完整覆盖 6 月法定跟踪评级季 —— 库里因此
出现过搜特退债 / 鸿达退债 / 正邦转债这类**已违约券仍标 AA**。

判据用外部第三方而不是库内自洽: 评级没有任何库内裁判可用 (Wind 冻结, 公告解析无自校验)。
实测对 akshare ``bond_zh_cov`` 的信用评级, cb_data 精确命中 79% / 平均差 0.55 档,
公告解析的末条 patch 命中 84% / 平均差 0.42 档。

评级直接进 pricer: ``pricing_api._rating_spread_floor`` 把它变成信用利差下限
(AA 2.50% ↔ C 80.00%), 所以一个陈旧的 AA 会让困境债的理论价被系统性高估。

一次 ``ak.bond_zh_cov()`` 拿全市场, 秒级完成; 评级按年变动, 每月跑一次即可::

    cb-sync-ratings              # 预览
    cb-sync-ratings --apply
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import sys
from dataclasses import replace
from typing import Any

from ..cache import TermsBundle, project_bundle_path
from ..data_providers.base import CREDIT_RATING_ORDER, CREDIT_RATING_RANK

# 档位表的单一事实源在 data_providers.base —— 这里只做别名, 供既有 import 继续用
RATING_ORDER = CREDIT_RATING_ORDER
_RANK = CREDIT_RATING_RANK

# 上游对部分券 (科创板 118xxx 段居多) 会在评级后缀一个 "sti", 例如 "AA+sti" / "A-sti"。
# 档位本身是标准的, 后缀只是上游的口径标记。早期实现按"值必须精确落在 _RANK 里"过滤,
# 于是这一整类券**每次同步都被静默跳过**, 继续挂着 Wind 的发行时冻结值 —— 实测 26 只
# (主池 21 只) 从未被刷新过, 其中科蓝转债本地 AA- / 第三方 A-, 差 3 档, 经
# ``pricing_api._rating_spread_floor`` 直接变成信用利差下限 3.50% vs 8.00%。
_RATING_SUFFIX_RE = re.compile(r"(?i)sti$")


def normalize_rating(value: Any) -> str | None:
    """把第三方/库内的评级字串归一到标准档位; 归一不到返回 None.

    只剥离已知的口径后缀, **不做任何档位映射** —— 剥完必须精确命中 ``_RANK``,
    否则宁可返回 None 让这一条跳过, 也不猜。
    """
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw in _RANK:
        return raw
    stripped = _RATING_SUFFIX_RE.sub("", raw).strip()
    return stripped if stripped in _RANK else None


def fetch_third_party_ratings() -> dict[str, str]:
    """``ak.bond_zh_cov()`` 的信用评级, 按 6 位债券代码索引 (已归一化档位)。"""
    import akshare as ak

    frame = ak.bond_zh_cov()
    column = next((c for c in frame.columns if "评级" in c), None)
    if column is None:
        raise RuntimeError("akshare bond_zh_cov 没有评级列, 上游字段可能改名了")
    out: dict[str, str] = {}
    for _, row in frame.iterrows():
        code = str(row.get("债券代码") or "").strip()
        value = normalize_rating(row.get(column))
        if code and value:
            out[code] = value
    if not out:
        raise RuntimeError("akshare 评级一条都没解析出来, 拒绝据此改库")
    return out


def sync_ratings(bundle_path=None, *, dry_run: bool = True) -> dict:
    bundle = TermsBundle(bundle_path or project_bundle_path())
    third = fetch_third_party_ratings()
    changes = []
    for code in bundle.list_bonds():
        terms = bundle.get(code)
        if terms is None:
            continue
        new = third.get(code.split(".")[0])
        current = str(getattr(terms, "credit_rating", "") or "")
        if not new or new == current:
            continue
        # 库里也可能存着带后缀的旧值 (首次建档时原样落库)。档位相同、只是写法不规范的,
        # 照样写回标准档位: 留着 "AA+sti" 会让所有按 _RANK 精确查表的消费者 (体检的评级
        # 检查、sync 自身的档位差) 静默跳过这几只。
        current_norm = normalize_rating(current)
        changes.append({
            "bond_code": code,
            "bond_name": getattr(terms, "sec_name", None),
            "before": current or None,
            "after": new,
            # 负数 = 下调; 无法比较 (cb_data 为空或非法值) 时留 None
            "notches": (_RANK[new] - _RANK[current_norm]) if current_norm else None,
            # True = 档位没变, 只是把 "AA+sti" 这类写法洗成标准档位
            "normalize_only": current_norm == new,
        })
    if not dry_run and changes:
        bundle.set_many(
            [(row["bond_code"], replace(bundle.get(row["bond_code"]), credit_rating=row["after"]))
             for row in changes],
            source="akshare:ratings",
        )
    return {"n_third_party": len(third), "n_bonds": len(bundle.list_bonds()),
            "changes": changes, "applied": not dry_run}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从 akshare 第三方刷新 cb_data 的信用评级")
    parser.add_argument("--apply", action="store_true", help="真正写盘 (默认只预览)")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--bundle-path", default=None)
    args = parser.parse_args(argv)

    report = sync_ratings(args.bundle_path, dry_run=not args.apply)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    rows = report["changes"]
    graded = [r for r in rows if not r.get("normalize_only")]
    norm_only = [r for r in rows if r.get("normalize_only")]
    down = [r for r in graded if (r["notches"] or 0) < 0]
    print(f"第三方评级 {report['n_third_party']} 只 / 本地 {report['n_bonds']} 只; "
          f"需要更新 {len(rows)} 只 (下调 {len(down)}, 上调 {len(graded) - len(down)}, "
          f"仅规范写法 {len(norm_only)})"
          + ("" if args.apply else "  [预览, 未写盘]"))
    for row in sorted(rows, key=lambda r: (r["notches"] if r["notches"] is not None else 0))[:20]:
        print(f"  {row['bond_code']} {str(row['bond_name'] or ''):<12} "
              f"{row['before']} → {row['after']}"
              + (f"  ({row['notches']:+d} 档)" if row["notches"] is not None else ""))
    if len(rows) > 20:
        print(f"  … 另有 {len(rows) - 20} 只未列出")
    if rows:
        print("\n下调档数分布:", dict(collections.Counter(
            r["notches"] for r in graded if r["notches"] is not None).most_common(8)))
    if not args.apply and rows:
        print("\n确认无误后加 --apply 执行")
    return 0


if __name__ == "__main__":
    sys.exit(main())
