"""转债大类估值 / 择时指标 CLI (cb-valuation).

读批量定价结果 (data/batch_pricing_cache.json), 算全市场理论价 vs 市价的中位偏差,
对照历史基线给出"贵 / 便宜"分位信号, 用于转债**大类配置择时** (非个券买入信号)。

用法::

    python -m convertible_bond.cli.market_valuation            # 当前估值信号
    python -m convertible_bond.cli.market_valuation --record   # 并把本次读数并入历史基线
    python -m convertible_bond.cli.market_valuation --json      # 机器可读输出
    python -m convertible_bond.cli.market_valuation --cache <path> --history <path>
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

from ..market_valuation import (
    append_history,
    classify,
    compute_snapshot,
    load_history,
)
from ..paths import data_path


def _load_results(cache_path: Path, *, include_watchlist: bool = False) -> list[dict]:
    """读批量定价结果。默认只取主全市场池 ``results``; ``upcoming_results`` 是关注池
    额外定价 (新债/在途), 不属于全市场口径, 仅在显式要求时并入。"""
    with open(cache_path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    if isinstance(payload, dict):
        results = list(payload.get("results") or [])
        if include_watchlist:
            results += list(payload.get("upcoming_results") or [])
        return results
    return list(payload)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="转债大类估值/择时指标: 全市场中位偏差 + 历史分位信号",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--cache", default="",
                        help="批量定价缓存路径 (默认 data/batch_pricing_cache.json)")
    parser.add_argument("--history", default="",
                        help="历史基线路径 (默认 data/cb_valuation_history.json)")
    parser.add_argument("--record", action="store_true",
                        help="把本次中位偏差并入历史基线 (同估值日覆盖)")
    parser.add_argument("--include-watchlist", action="store_true",
                        help="把关注池 upcoming_results 也并入估值 (默认只用主全市场池)")
    parser.add_argument("--json", action="store_true", help="机器可读 JSON 输出")
    args = parser.parse_args(argv)

    cache_path = Path(args.cache) if args.cache else data_path("batch_pricing_cache.json")
    history_path = (Path(args.history) if args.history
                    else data_path("cb_valuation_history.json", seed=True))

    if not cache_path.exists():
        print(f"找不到批量定价缓存: {cache_path}\n请先在 GUI 批量页或 cb-screen-pool 生成定价结果。",
              file=sys.stderr)
        return 2
    try:
        results = _load_results(cache_path, include_watchlist=args.include_watchlist)
        snapshot = compute_snapshot(results)
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"读取/聚合定价结果失败: {exc}", file=sys.stderr)
        return 2

    history = load_history(history_path)
    hist_medians = [s.median_deviation for s in history if s.date != snapshot.date]
    signal = classify(snapshot.median_deviation, hist_medians)

    if args.record:
        append_history(history_path, snapshot)

    if args.json:
        pct = None if math.isnan(signal.percentile) else signal.percentile
        print(json.dumps({
            "snapshot": snapshot.to_record(),
            "signal": {"label": signal.label, "percentile": pct,
                       "n_history": signal.n_history},
            "recorded": bool(args.record),
        }, ensure_ascii=False, indent=2, allow_nan=False))
        return 0

    print(f"批量定价缓存: {cache_path}")
    print(f"估值日: {snapshot.date or '未知'}   样本: {snapshot.n} 只")
    print("-" * 56)
    print(f"全市场中位偏差 (市价-理论)/理论 : {snapshot.median_deviation*100:+.1f}%")
    print(f"均值偏差                       : {snapshot.mean_deviation*100:+.1f}%")
    print(f"判高估占比 (deviation>0)       : {snapshot.pct_overvalued*100:.0f}%")
    print(f"分位区间 P25 / P75             : {snapshot.p25*100:+.1f}% / {snapshot.p75*100:+.1f}%")
    print("-" * 56)
    print(signal)
    if args.record:
        print(f"\n已记录到历史基线: {history_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
