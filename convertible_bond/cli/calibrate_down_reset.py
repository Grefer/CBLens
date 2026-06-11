"""下修博弈参数校准: 从 cb_events 历史事件统计模型常量.

把 ``data/cb_events.json`` 里的 down_reset_proposed / approved / rejected 事件
聚合成几个可直接喂给定价模型的经验量:

- **提议→通过通过率** (``PROPOSED_PASS_PROB``): 董事会提议后最终被股东会通过的比例。
- **提议→通过滞后天数** (``PROPOSED_EFFECTIVE_LAG_DAYS``): 提议公告到通过公告的间隔。
- **决策点下修占比**: approved / (approved + rejected), 反映触发后真正修正的频率。
- **不修正承诺期分布** (``cooldown_months``): 否决事件的 commitment_months。

注意: cb_events 当前未解析下修后的新转股价 (``event_price`` 多为空), 因此**下修幅度**
(down_reset_premium) 无法从事件层校准, 需另行结合行情/条款历史, 这里只报告缺口。

用法::

    python -m convertible_bond.cli.calibrate_down_reset
    python -m convertible_bond.cli.calibrate_down_reset --events-path data/cb_events.json --json
"""
from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter, defaultdict

from ..cb_events import CBEvent, CBEventStore, project_events_path

_DOWN_TYPES = ("down_reset_proposed", "down_reset_approved", "down_reset_rejected")


def _percentile(values: list[float], q: float) -> float:
    """线性插值分位数 (q ∈ [0,1]); 空列表返回 nan。"""
    if not values:
        return float("nan")
    s = sorted(values)
    if len(s) == 1:
        return float(s[0])
    pos = q * (len(s) - 1)
    lo = int(pos)
    frac = pos - lo
    if lo + 1 >= len(s):
        return float(s[-1])
    return float(s[lo] * (1 - frac) + s[lo + 1] * frac)


def calibrate(events: list[CBEvent]) -> dict:
    """把下修事件聚合成校准报告 dict。"""
    down = [e for e in events if e.event_type in _DOWN_TYPES]
    by_type = Counter(e.event_type for e in down)
    n_proposed = by_type.get("down_reset_proposed", 0)
    n_approved = by_type.get("down_reset_approved", 0)
    n_rejected = by_type.get("down_reset_rejected", 0)

    by_bond: dict[str, list[CBEvent]] = defaultdict(list)
    for e in down:
        by_bond[e.bond_code].append(e)
    for lst in by_bond.values():
        lst.sort(key=lambda e: e.event_date)

    # 提议 → 终态 (approved/rejected) 链接
    gaps: list[int] = []
    pass_n = rej_after = pending = 0
    for lst in by_bond.values():
        for i, e in enumerate(lst):
            if e.event_type != "down_reset_proposed":
                continue
            nxt = [
                x for x in lst[i + 1:]
                if x.event_type in ("down_reset_approved", "down_reset_rejected")
            ]
            if not nxt:
                pending += 1
                continue
            terminal = nxt[0]
            if terminal.event_type == "down_reset_approved":
                pass_n += 1
                gaps.append((terminal.event_date - e.event_date).days)
            else:
                rej_after += 1

    linked = pass_n + rej_after
    # 通过率: 有终态的样本按通过/(通过+否决); 另给一个把未决计入分母的保守口径
    pass_rate = (pass_n / linked) if linked else float("nan")
    pass_rate_with_pending = (
        pass_n / (linked + pending) if (linked + pending) else float("nan")
    )

    decided = n_approved + n_rejected
    reset_share = (n_approved / decided) if decided else float("nan")

    cooldowns = [
        e.commitment_months
        for e in down
        if e.event_type == "down_reset_rejected" and e.commitment_months is not None
    ]

    ever_approved = {
        b for b, lst in by_bond.items()
        if any(e.event_type == "down_reset_approved" for e in lst)
    }
    ever_rejected = {
        b for b, lst in by_bond.items()
        if any(e.event_type == "down_reset_rejected" for e in lst)
    }

    # event_price 覆盖率 (下修幅度能否从事件层校准)
    price_cov = sum(
        1 for e in down
        if e.event_type == "down_reset_approved" and e.event_price is not None
    )

    dates = sorted(e.event_date for e in down)
    return {
        "date_range": [dates[0].isoformat(), dates[-1].isoformat()] if dates else None,
        "counts": {
            "proposed": n_proposed,
            "approved": n_approved,
            "rejected": n_rejected,
            "total": len(down),
        },
        "proposal_to_terminal": {
            "linked": linked,
            "approved_after": pass_n,
            "rejected_after": rej_after,
            "pending": pending,
            "pass_rate": pass_rate,
            "pass_rate_with_pending": pass_rate_with_pending,
            "lag_days": {
                "n": len(gaps),
                "median": statistics.median(gaps) if gaps else None,
                "mean": statistics.mean(gaps) if gaps else None,
                "p25": _percentile([float(g) for g in gaps], 0.25) if gaps else None,
                "p75": _percentile([float(g) for g in gaps], 0.75) if gaps else None,
            },
        },
        "decision_reset_share": reset_share,
        "cooldown_months": {
            "n": len(cooldowns),
            "dist": dict(sorted(Counter(cooldowns).items())),
            "median": statistics.median(cooldowns) if cooldowns else None,
            "mean": statistics.mean(cooldowns) if cooldowns else None,
        },
        "bond_participation": {
            "bonds_with_events": len(by_bond),
            "ever_approved": len(ever_approved),
            "ever_rejected": len(ever_rejected),
            "both": len(ever_approved & ever_rejected),
        },
        "magnitude_gap": {
            "approved_with_event_price": price_cov,
            "note": "event_price 未解析时下修幅度无法从事件层校准",
        },
        "suggested_constants": {
            # 通过率取有终态口径与含未决口径之间, 向下取整到 0.05
            "PROPOSED_PASS_PROB": (
                round(((pass_rate + pass_rate_with_pending) / 2) * 20) / 20
                if linked else None
            ),
            "PROPOSED_EFFECTIVE_LAG_DAYS": (
                int(round(statistics.median(gaps))) if gaps else None
            ),
            "DEFAULT_COOLDOWN_MONTHS": (
                int(statistics.median(cooldowns)) if cooldowns else None
            ),
        },
    }


def _fmt(report: dict) -> str:
    c = report["counts"]
    pt = report["proposal_to_terminal"]
    lag = pt["lag_days"]
    cd = report["cooldown_months"]
    bp = report["bond_participation"]
    sc = report["suggested_constants"]
    rng = report["date_range"]

    def pct(x):
        return "—" if x is None or x != x else f"{x:.1%}"

    def num(x, fmt="{}"):
        return "—" if x is None else fmt.format(x)

    lines = [
        "下修博弈参数校准报告",
        "=" * 48,
        f"样本区间: {rng[0]} → {rng[1]}" if rng else "样本区间: (无事件)",
        "",
        f"事件计数: 提议={c['proposed']}  通过={c['approved']}  否决={c['rejected']}  (合计 {c['total']})",
        "",
        "── 提议 → 终态 ──",
        f"  有终态样本: {pt['linked']}  (通过后 {pt['approved_after']} / 否决后 {pt['rejected_after']}), 窗内未决 {pt['pending']}",
        f"  通过率: {pct(pt['pass_rate'])} (有终态口径) / {pct(pt['pass_rate_with_pending'])} (含未决保守口径)",
        f"  提议→通过滞后(天): 中位 {num(lag['median'])}  均值 {num(lag['mean'], '{:.0f}')}  "
        f"p25 {num(lag['p25'], '{:.0f}')}  p75 {num(lag['p75'], '{:.0f}')}  (n={lag['n']})",
        "",
        f"── 决策点下修占比: {pct(report['decision_reset_share'])}  (通过/(通过+否决)) ──",
        "",
        "── 不修正承诺期 (月) ──",
        f"  分布: {cd['dist']}",
        f"  中位 {num(cd['median'])}  均值 {num(cd['mean'], '{:.1f}')}  (n={cd['n']})",
        "",
        "── 个券参与度 ──",
        f"  有下修事件的债: {bp['bonds_with_events']}  曾通过 {bp['ever_approved']}  曾否决 {bp['ever_rejected']}  两者皆有 {bp['both']}",
        "",
        "── 下修幅度缺口 ──",
        f"  通过事件中带 event_price 的: {report['magnitude_gap']['approved_with_event_price']}  "
        f"({report['magnitude_gap']['note']})",
        "",
        "── 建议常量 ──",
        f"  PROPOSED_PASS_PROB            = {num(sc['PROPOSED_PASS_PROB'])}",
        f"  PROPOSED_EFFECTIVE_LAG_DAYS   = {num(sc['PROPOSED_EFFECTIVE_LAG_DAYS'])}",
        f"  DEFAULT_COOLDOWN_MONTHS       = {num(sc['DEFAULT_COOLDOWN_MONTHS'])}",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="从 cb_events 历史事件校准下修博弈模型常量",
    )
    parser.add_argument(
        "--events-path", default=None,
        help="cb_events.json 路径 (默认用项目数据目录)",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="输出机器可读 JSON 而非文本报告",
    )
    args = parser.parse_args(argv)

    path = args.events_path or project_events_path()
    store = CBEventStore(path)
    report = calibrate(store.list_events())

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        print(_fmt(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
