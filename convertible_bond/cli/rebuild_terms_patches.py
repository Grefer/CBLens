"""用 Wind 的 as-of 日序列重建条款 patch, 替换公告解析出来的脏值.

``cb_terms_patches.json`` 里的 ``conversion_price`` / ``credit_rating`` 是从公告正文解析
出来的, 实测错误率很高:

  - 1307 条 conversion_price patch 覆盖 283 只债, **208 只 (73.5%) 的最新 patch 值与
    cb_data 当前 K 不符**; 33 条取值低于当前 K (K 只降不升, 数学上不可能);
    patch 链自洽率只有 544/1024 (断裂 80%)
  - 典型错法: 调整公告正文里带着**历次调整沿革**, 解析器取到最早那次 (万孚转债 14 条
    patch 跨两年恒为 93.57, 真实 K 是 20.88); 同发行人两只债的公告串号 (嘉益转债被写进
    "精达转债"的 3.26); 评级公告解析出错 (广核转债 AAA 被写成 A)

而 Wind 的 ``clause_conversion2_swapshareprice`` / ``creditrating`` 是**真 as-of**:
逐日拉取后取变化点, 就是权威的条款变更时点与新值。实测万孚转债 2024-2026 的 10 个变化点
与公告沿革逐条吻合 (2024-12-26 下修至 27.00、2026-01-15 下修至 21.10、2026-06-02 至 20.88)。

因此本工具**不重新解析公告**, 而是直接从 Wind 重建。被重建的字段, 其原有 patch 全部替换;
其它字段 (call_redemption_price / outstanding_balance / 评级展望…) 原样保留。

用法::

    python -m convertible_bond.cli.rebuild_terms_patches --dry-run
    python -m convertible_bond.cli.rebuild_terms_patches --apply
    python -m convertible_bond.cli.rebuild_terms_patches --apply --codes 123064.SZ
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from ..cache import TermsBundle, project_bundle_path
from ..historical_terms import TermsPatch, TermsPatchStore, project_terms_patches_path
from ..market_time import market_today

# 可从 Wind 日序列重建的字段 → Wind 字段名。
# ⚠️ 只放**经实测确认是真 as-of** 的字段。`creditrating` 看着能用其实不行:
# wsd 拉 2023-2026 共 881 个交易日, 返回的是恒定的**当前**评级 (123142.SZ 申昊转债
# 全程 A+, 而 patch 声称 2024→A、2025→A-)。把它放进来的后果是 --apply 删光 246 条
# 评级 patch 却一条不补 —— 静默数据丢失。评级历史目前没有可靠重建源。
REBUILDABLE_FIELDS: dict[str, str] = {
    "conversion_price": "clause_conversion2_swapshareprice",
    "outstanding_balance": "outstandingbalance",
}

# 数值字段的"重大变化"规则: 只有跨过决策边界、或相对上次落库值变动超过 rel_tol 才落一条 patch。
#
# 转股价是**离散的调整事件**, 每次变化都是一次公告、都直接改变转股价值, 因此全留 (rel_tol=0)。
# 未转股余额则随转股进度逐日微动 —— 实测 127110.SZ 广核转债 274 个交易日变化 105 次, 全程
# 49.0000 → 48.9923 (0.016%)。照单全收会生成十万条 patch 去追踪毫无决策含义的漂移。
# 余额真正影响的只有档位边界 (余额清零 / 触及摘牌线 0.3 / 临近摘牌线 0.5 / 小余额 1.0 /
# 大余额加分 10) 与排序量级, 所以按边界 + 1% 相对变动过滤: 实测压缩 92%, 而
# 123118.SZ 惠城转债 3.200 → 0.654 的 55 步真实缩量一步不落。
_NUMERIC_FIELDS = frozenset(REBUILDABLE_FIELDS)
_MATERIALITY: dict[str, tuple[float, tuple[float, ...]]] = {
    # field: (相对变动阈值, 决策边界)
    "conversion_price": (0.0, ()),
    "outstanding_balance": (0.01, (0.0, 0.3, 0.5, 1.0, 10.0)),
}
# 扫描规模达到该只数时, 若某字段一条 patch 都没重建出来, 判为"数据源不可用"而拒绝删存量。
# 这是上面那个教训的通用守卫: 一个字段能被删, 前提是确实有东西替换它。
_EMPTY_FIELD_GUARD_MIN_CODES = 20
DEFAULT_START = date(2018, 1, 1)


class ConcurrentWriteError(RuntimeError):
    """扫描之后、写盘之前 patch 库被别的进程改过 —— 拒绝写, 免得互相覆盖。"""


def _fingerprint(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def _window(terms: Any, on_date: date, start_floor: date) -> tuple[date, date] | None:
    """这只债有条款变更可能的时间窗 = [上市日, min(今天, 摘牌/到期)]。"""
    begin = _as_date(getattr(terms, "listing_date", None)) \
        or _as_date(getattr(terms, "issue_date", None))
    if begin is None:
        return None
    begin = max(begin, start_floor)
    ends = [d for d in (_as_date(getattr(terms, "delisting_date", None)),
                        _as_date(getattr(terms, "last_trading_date", None)),
                        _as_date(getattr(terms, "maturity_date", None))) if d is not None]
    end = min([on_date] + ends)
    # 摘牌日当天仍可能有最后一次调整, 留一天余量
    end = min(on_date, end + timedelta(days=1))
    return (begin, end) if begin < end else None


def _band(value: float, bounds: tuple[float, ...]) -> int:
    return sum(1 for b in bounds if value > b)


def _change_points(series: list[tuple[date, Any]],
                   field: str = "") -> list[tuple[date, Any, Any]]:
    """从日序列提取 (生效日, 新值, 旧值)。首个观测是初始状态, 不算变更。

    数值字段按 ``_MATERIALITY`` 过滤掉无决策含义的微动 —— 见该常量的说明。
    比较基准是**上次落库的值**而不是前一日, 否则连续微动会被逐段吞掉而累积成大偏移。
    """
    out: list[tuple[date, Any, Any]] = []
    if not series:
        return out
    rel_tol, bounds = _MATERIALITY.get(field, (0.0, ()))
    prev = series[0][1]
    for day, value in series[1:]:
        if value is None or value == prev:
            continue
        if rel_tol > 0 or bounds:
            try:
                new_v, old_v = float(value), float(prev)
            except (TypeError, ValueError):
                new_v = old_v = None
            if new_v is not None:
                crossed = _band(new_v, bounds) != _band(old_v, bounds) if bounds else False
                moved = bool(old_v) and abs(new_v - old_v) / abs(old_v) >= rel_tol
                if not (crossed or moved):
                    continue
        out.append((day, value, prev))
        prev = value
    return out


def _fetch_series(wind, code: str, wind_field: str,
                  begin: date, end: date) -> list[tuple[date, Any]] | None:
    res = wind.wsd(code, wind_field, begin.isoformat(), end.isoformat(), "")
    if getattr(res, "ErrorCode", -1) != 0 or not res.Data or not res.Data[0]:
        return None
    out: list[tuple[date, Any]] = []
    for raw_day, value in zip(res.Times, res.Data[0]):
        day = _as_date(raw_day)
        if day is None or value is None:
            continue
        out.append((day, value))
    return out or None


def _normalize(field: str, value: Any) -> Any:
    if field in _NUMERIC_FIELDS:
        try:
            v = float(value)
        except (TypeError, ValueError):
            return None
        return round(v, 6) if v > 0 else None
    text = str(value).strip()
    return text or None


def rebuild(
    fields: list[str] | None = None,
    *,
    codes: list[str] | None = None,
    bundle_path: Path | None = None,
    patches_path: Path | None = None,
    start: date = DEFAULT_START,
    on_date: date | None = None,
    limit: int = 0,
    dry_run: bool = True,
    backup: bool = True,
    progress_cb=None,
) -> dict[str, Any]:
    """从 Wind 重建指定字段的 patch, 返回统计与明细。"""
    from WindPy import w as wind

    wind.start()
    target_fields = [f for f in (fields or list(REBUILDABLE_FIELDS)) if f in REBUILDABLE_FIELDS]
    if not target_fields:
        raise ValueError(f"没有可重建的字段, 可选: {sorted(REBUILDABLE_FIELDS)}")

    today = on_date or market_today()
    bundle = TermsBundle(bundle_path or project_bundle_path())
    store = TermsPatchStore(patches_path or project_terms_patches_path())
    fingerprint = _fingerprint(store.path)

    all_codes = list(codes) if codes else list(bundle.list_bonds())
    if limit and limit > 0:
        all_codes = all_codes[:limit]

    built: list[TermsPatch] = []
    stats: Counter = Counter()
    skipped: list[tuple[str, str]] = []
    for idx, code in enumerate(all_codes, start=1):
        if progress_cb is not None:
            progress_cb(idx, len(all_codes), code)
        terms = bundle.get(code)
        if terms is None:
            skipped.append((code, "cb_data 无条款")); stats["no_terms"] += 1; continue
        win = _window(terms, today, start)
        if win is None:
            skipped.append((code, "无有效时间窗")); stats["no_window"] += 1; continue
        begin, end = win
        got_any = False
        for field in target_fields:
            try:
                series = _fetch_series(wind, code, REBUILDABLE_FIELDS[field], begin, end)
            except Exception as exc:
                skipped.append((code, f"{field} 取数失败: {exc}")); stats["fetch_error"] += 1
                continue
            if not series:
                stats[f"{field}_empty"] += 1
                continue
            got_any = True
            norm = [(d, _normalize(field, v)) for d, v in series]
            norm = [(d, v) for d, v in norm if v is not None]
            for eff, new, old in _change_points(norm, field):
                built.append(TermsPatch(
                    bond_code=code,
                    effective_date=eff,
                    event_date=eff,
                    fields={field: new},
                    before_fields={field: old} if old is not None else None,
                    source="wind_asof",
                    note=f"{field} {old}->{new} (Wind as-of 日序列变化点)",
                    confidence="wind_asof",
                ))
                stats[f"{field}_patches"] += 1
        stats["scanned"] += 1
        if got_any:
            stats["with_data"] += 1

    # 删除范围**必须**是实际扫描到的代码集, 不能是"没指定 --codes 就是全库" ——
    # 否则 --limit N --apply 会删光全库该字段的 patch, 却只补回 N 只的量。
    scanned_codes = set(all_codes)
    rebuilt_codes = {p.bond_code for p in built}
    # ``include_shadowed=True``: 预览必须和**真删**看同一个总体。``rewrite`` 遍历的是
    # ``self._patches`` (原始文件), 而默认视图会把被权威源逐字段遮蔽的解析 patch 藏起来
    # —— 于是 --dry-run 少报, 被遮蔽的那些**没出现在操作者审过的报告里就被删掉了**。
    # 实测 conversion_price: 预览 4422 条 / 实删 4426 条, 差 4。
    dropped = [p for p in store.list_patches(include_shadowed=True)
               if set(p.fields or {}) & set(target_fields)
               and p.bond_code in scanned_codes]

    result: dict[str, Any] = {
        "patches_path": store.path,
        "fields": target_fields,
        "scanned": len(all_codes),
        "stats": dict(stats),
        "built": built,
        "dropped": dropped,
        "rebuilt_codes": rebuilt_codes,
        "skipped": skipped,
        "backup_path": None,
        "written": 0,
    }
    if dry_run:
        return result

    if _fingerprint(store.path) != fingerprint:
        raise ConcurrentWriteError(
            f"{store.path} 在扫描期间被改动过 (可能 GUI 正在后台同步公告)。未做任何写入。")
    if backup and store.path.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        result["backup_path"] = store.path.with_suffix(f".json.bak-{stamp}")
        shutil.copy2(store.path, result["backup_path"])

    # 守卫: 规模够大却零产出的字段, 不参与删除 —— 宁可留着旧的脏值, 也不能删完补不上。
    usable = [f for f in target_fields
              if stats.get(f"{f}_patches", 0) > 0
              or len(all_codes) < _EMPTY_FIELD_GUARD_MIN_CODES]
    refused = [f for f in target_fields if f not in usable]
    result["refused_fields"] = refused
    if refused:
        print(f"⚠️  {', '.join(refused)} 扫描 {len(all_codes)} 只零产出, "
              f"判为数据源不可用, 其存量 patch 保持不动", file=sys.stderr)
    if not usable:
        return result
    target = set(usable)

    def transform(patch: TermsPatch) -> TermsPatch | None:
        """剔除被重建字段的旧 patch; 其它字段与未扫描到的债原样保留。"""
        if patch.bond_code not in scanned_codes:
            return patch
        remaining = {k: v for k, v in (patch.fields or {}).items() if k not in target}
        if remaining == (patch.fields or {}):
            return patch
        if not remaining:
            return None
        before = {k: v for k, v in (patch.before_fields or {}).items() if k not in target}
        from dataclasses import replace as _replace
        return _replace(patch, fields=remaining, before_fields=before or None)

    store.rewrite(transform)
    result["written"] = store.add_many(built)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="用 Wind as-of 日序列重建条款 patch (转股价 / 债项评级)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True, help="只出报告 (默认)")
    mode.add_argument("--apply", action="store_true", help="真正写回 (先自动备份)")
    parser.add_argument("--fields", nargs="*", default=None,
                        help=f"要重建的字段, 默认全部: {' '.join(REBUILDABLE_FIELDS)}")
    parser.add_argument("--codes", nargs="*", default=None, help="只处理指定代码")
    parser.add_argument("--limit", type=int, default=0, help="限制处理只数 (调试用)")
    parser.add_argument("--start", default=DEFAULT_START.isoformat(), help="起始扫描日")
    parser.add_argument("--no-backup", action="store_true", help="--apply 时跳过备份")
    parser.add_argument("--patches-path", help="覆盖 cb_terms_patches.json 路径")
    parser.add_argument("--bundle-path", help="覆盖 cb_data.json 路径")
    parser.add_argument("--limit-show", type=int, default=15, help="明细展示条数")
    args = parser.parse_args(argv)

    started = time.time()

    def progress(i: int, total: int, code: str) -> None:
        if i % 25 == 0 or i == total:
            rate = i / max(time.time() - started, 1e-6)
            eta = (total - i) / max(rate, 1e-6)
            print(f"  [{i:>4}/{total}] {code:<12} {rate:.1f}/s  ETA {eta:.0f}s", flush=True)

    try:
        result = rebuild(
            args.fields,
            codes=args.codes,
            bundle_path=Path(args.bundle_path) if args.bundle_path else None,
            patches_path=Path(args.patches_path) if args.patches_path else None,
            start=date.fromisoformat(args.start),
            limit=args.limit,
            dry_run=not args.apply,
            backup=not args.no_backup,
            progress_cb=progress,
        )
    except ConcurrentWriteError as exc:
        print(str(exc), file=sys.stderr)
        return 3

    stats = result["stats"]
    print(f"\nPatch 库: {result['patches_path']}")
    print(f"重建字段: {', '.join(result['fields'])}")
    print(f"扫描 {result['scanned']} 只, 取到数据 {stats.get('with_data', 0)} 只, "
          f"耗时 {time.time() - started:.0f}s")
    for field in result["fields"]:
        print(f"  {field}: 新建 {stats.get(field + '_patches', 0)} 条 patch, "
              f"无数据 {stats.get(field + '_empty', 0)} 只")
    print(f"将替换掉的旧 patch: {len(result['dropped'])} 条")

    if result["built"]:
        print(f"\n新 patch 明细 (前 {args.limit_show} 条):")
        for p in result["built"][:args.limit_show]:
            field, value = next(iter(p.fields.items()))
            before = (p.before_fields or {}).get(field)
            print(f"  {p.bond_code} {p.effective_date}  {field}: {before} -> {value}")

    if result["skipped"]:
        print(f"\n跳过 {len(result['skipped'])} 只 (前 8):")
        for code, why in result["skipped"][:8]:
            print(f"  {code}: {why}")

    if args.apply:
        if result["backup_path"]:
            print(f"\n已备份: {result['backup_path']}")
        print(f"已写入 {result['written']} 条新 patch")
    else:
        print("\n[dry-run] 未写盘。确认无误后加 --apply。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
