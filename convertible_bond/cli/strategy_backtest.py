"""机会分选债策略回测.

用法:
    python -m convertible_bond.cli.strategy_backtest --start 2024-01-01 --end 2025-12-31
    cb-strategy-backtest --source akshare --top-n 10 --freq M --output strategy.csv
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from ..batch_pricing import (
    AdmissionFilterConfig,
    BATCH_REVIEW_VIEWS,
    DEFAULT_MIN_CREDIT_RATING,
    DEFAULT_MIN_OUTSTANDING_BALANCE,
    build_batch_provider,
    parse_bond_codes,
)
from ..backtest_disk_cache import DiskCacheProvider
from ..cache import TermsBundle, project_bundle_path
from ..cb_events import CBEventStore, project_events_path
from ..historical_terms import (
    HistoricalBondDataProvider,
    TermsHistoryStore,
    TermsPatchStore,
    project_terms_patches_path,
)
from ..strategy_backtest import (
    ScoreStrategyConfig,
    backtest_score_strategy,
    write_strategy_backtest_csv,
)


def _parse_date(raw: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"日期格式应为 YYYY-MM-DD: {raw}") from exc


def _fmt_pct(value) -> str:
    if value is None:
        return "-"
    return f"{float(value) * 100:.2f}%"


def _print_stability(stability) -> None:
    """打印统计稳健性: Sharpe 块自助 CI、跑赢基准概率、滚动 Sharpe 子区间。"""
    if not stability:
        return
    print("── 统计稳健性 (块自助, 判断差异是否为噪声) ──")
    sb = stability.get("sharpe_bootstrap")
    if sb:
        print(f"Sharpe: {sb['point']:.2f}  "
              f"{int(sb['ci_level']*100)}%CI[{sb['ci_low']:.2f}, {sb['ci_high']:.2f}]  "
              f"P(>0)={sb['prob_positive']*100:.0f}%  (block={sb['block']}, n={sb['n_boot']})")
    eb = stability.get("excess_bootstrap")
    if eb:
        print(f"超额: {_fmt_pct(eb['point_excess'])}  "
              f"{int(eb['ci_level']*100)}%CI[{_fmt_pct(eb['excess_ci_low'])}, "
              f"{_fmt_pct(eb['excess_ci_high'])}]  跑赢基准概率={eb['prob_beat_benchmark']*100:.0f}%")
    rs = stability.get("rolling_summary")
    if rs:
        print(f"滚动 Sharpe(1年窗): 均值 {rs['rolling_sharpe_mean']:.2f}  "
              f"最差 {rs['rolling_sharpe_min']:.2f}  "
              f"为正窗占比 {rs['rolling_sharpe_pct_positive']*100:.0f}%  ({rs['n_windows']} 窗)")


def main() -> int:
    default_min_balance = (
        DEFAULT_MIN_OUTSTANDING_BALANCE
        if DEFAULT_MIN_OUTSTANDING_BALANCE is not None
        else -1.0
    )
    parser = argparse.ArgumentParser(
        description="按 CBLens 机会分选债并做固定频率调仓回测",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--source", default="akshare", choices=["wind", "akshare", "csv"],
                        help="动态行情数据源 (默认 akshare)")
    parser.add_argument("--csv-root", default="",
                        help="source=csv 时的 CSV 数据根目录")
    parser.add_argument("--bundle", "-b", default="",
                        help="cb_data bundle 路径 (默认 <repo>/data/cb_data.json)")
    parser.add_argument("--terms-history-dir", default="",
                        help="cb_data 历史快照目录, 文件名形如 YYYY-MM-DD.json")
    parser.add_argument("--terms-patches", default="",
                        help="条款变更 patch JSON 路径 (默认 <data>/cb_terms_patches.json)")
    parser.add_argument("--events", default="",
                        help="事件表路径 (默认 <data>/cb_events.json)")
    parser.add_argument("--cache-dir", default="",
                        help="跨运行磁盘缓存目录 (缓存 point-in-time 条款/历史价, "
                             "多周期复跑大幅提速; 默认关闭)")
    parser.add_argument("--codes", default="",
                        help="只回测指定转债代码, 支持逗号/空格/换行分隔; 默认使用 bundle 主池")
    parser.add_argument("--start", required=True, type=_parse_date,
                        help="回测开始日期, YYYY-MM-DD")
    parser.add_argument("--end", required=True, type=_parse_date,
                        help="回测结束日期, YYYY-MM-DD")
    parser.add_argument("--freq", default="M", choices=["D", "W", "M", "Q"],
                        help="调仓频率: D/W/M/Q (默认 M)")
    parser.add_argument("--pool-mode", default="static", choices=["static", "dynamic"],
                        help="标的池模式: static=固定代码, dynamic=按日过滤 (默认 static)")
    parser.add_argument("--mode", default="standard", choices=["fast", "standard", "precise"],
                        help="定价速度/精度: fast=快速预览, standard=标准, precise=精确 (默认 standard)")
    parser.add_argument("--top-n", type=int, default=10,
                        help="每次调仓选债数量 (仅 top_score 模式; 默认 10)")
    parser.add_argument("--holding-mode", default="top_score",
                        choices=["top_score", "pool"],
                        help="B持仓层: top_score=按机会分取Top N; pool=等权持有整个候选池。"
                             "两者均无稳健选股 alpha, 各有取舍 (见 README 模型边界)")
    parser.add_argument("--max-holdings", type=int, default=None,
                        help="pool 模式持仓上限 (默认不限, 持全部候选)")
    parser.add_argument("--funding-mode", default="reserve_cash",
                        choices=["reserve_cash", "full_invest"],
                        help="C资金层: reserve_cash=未建仓/缺价槽位留现金; "
                             "full_invest=满仓等权(缺口/缺价摊回)")
    parser.add_argument("--exposure-mode", default="full",
                        choices=["full", "valuation"],
                        help="D仓位层: full=恒定满仓(默认); valuation=按当期已定价池中位偏差"
                             "缩放总仓位 (研究配置, 依据见 docs/research/2026-06-*)")
    parser.add_argument("--selection-view", default="综合机会", choices=BATCH_REVIEW_VIEWS,
                        help="复用批量页视图过滤候选 (默认 综合机会)")
    parser.add_argument("--min-score", type=float, default=None,
                        help="最低机会分; 未设置时不过滤")
    parser.add_argument("--min-price", type=float, default=None,
                        help="最低转债市价")
    parser.add_argument("--max-price", type=float, default=None,
                        help="最高转债市价")
    parser.add_argument("--min-premium", type=float, default=None,
                        help="最低转股溢价率, 百分数; 例 -5 表示 -5%%")
    parser.add_argument("--max-premium", type=float, default=None,
                        help="最高转股溢价率, 百分数; 例 30 表示 30%%")
    parser.add_argument("--min-deviation", type=float, default=None,
                        help="最低市价/理论价偏差, 百分数")
    parser.add_argument("--max-deviation", type=float, default=None,
                        help="最高市价/理论价偏差, 百分数")
    parser.add_argument("--min-sigma", type=float, default=None,
                        help="最低历史波动率, 百分数")
    parser.add_argument("--max-sigma", type=float, default=None,
                        help="最高历史波动率, 百分数")
    parser.add_argument("--allow-low-confidence", action="store_true",
                        help="允许低置信度结果进入候选")
    parser.add_argument("--include-review-risks", action="store_true",
                        help="允许带硬复核标签的结果进入候选")
    parser.add_argument("--price-lookback-days", type=int, default=31,
                        help="期初/期末转债收盘价向前查找天数 (默认 31)")
    parser.add_argument("--max-price-staleness-days", type=int, default=10,
                        help="信号日收盘成交时允许价格向前陈旧的最大自然日数 (默认 10)")
    parser.add_argument("--execution-timing", default="next_close",
                        choices=["next_close", "signal_close"],
                        help="成交时点: next_close=信号日后下一可得收盘 (默认, 与 GUI 一致); "
                             "signal_close=信号日当日收盘 (在'用于计算信号的那根收盘'上成交, 偏乐观)")
    parser.add_argument("--execution-lookahead-days", type=int, default=10,
                        help="next_close 模式下向后寻找成交价的最大自然日数 (默认 10)")
    parser.add_argument("--no-mark-to-market", action="store_true",
                        help="关闭持仓期日频净值估值, 仅保留调仓端点净值")
    parser.add_argument("--cost-bps", type=float, default=0.0,
                        help="单边换手对应的交易成本, 单位 bps; 区间净收益扣 turnover*成本 (默认 0); "
                             "基准同口径计成员变动换手成本")
    parser.add_argument("--cash-yield", type=float, default=0.0,
                        help="闲置现金年化收益率, 小数 (如 0.02≈货基; 默认 0)。"
                             "Sharpe 课征 rf 门槛, 现金 0 计息会低估持现金配置, 研究运行建议设为 --r 同值")
    parser.add_argument("--no-benchmark", action="store_true",
                        help="关闭等权全可投池基准对比 (默认开启)")
    parser.add_argument("--benchmark-index", default="",
                        help="真实指数第二基准代码 (如 000832.CSI 中证转债); "
                             "数据源取不到时优雅跳过")
    parser.add_argument("--delist-window", type=int, default=0,
                        help=argparse.SUPPRESS)
    parser.add_argument("--min-balance", type=float, default=default_min_balance,
                        help=argparse.SUPPRESS)
    parser.add_argument("--min-rating", default=DEFAULT_MIN_CREDIT_RATING or "",
                        help=argparse.SUPPRESS)
    parser.add_argument("--min-turnover", type=float, default=-1.0,
                        help=argparse.SUPPRESS)
    parser.add_argument("--r", type=float, default=0.022,
                        help="无风险利率小数 (默认 0.022)")
    parser.add_argument("--base-spread", type=float, default=0.03,
                        help="基础信用利差小数 (默认 0.03)")
    parser.add_argument("--distress-k", type=float, default=0.05,
                        help="困境信用利差斜率 (默认 0.05)")
    parser.add_argument("--p-down", type=float, default=0.15,
                        help="年化下修事件强度 (默认 0.15)")
    parser.add_argument("--vol-window", type=int, default=21,
                        help="历史波动率窗口交易日数 (默认 21)")
    parser.add_argument("--M", type=int, default=None,
                        help="覆盖 PDE 价格网格 M")
    parser.add_argument("--N", type=int, default=None,
                        help="覆盖 PDE 时间网格 N")
    parser.add_argument("--max-workers", type=int, default=4,
                        help="批量定价线程数 (默认 4)")
    parser.add_argument("--output", "-o", default="",
                        help="导出逐期摘要 CSV")
    parser.add_argument("--show-holdings", action="store_true",
                        help="打印每期选中持仓")
    args = parser.parse_args()

    bundle_path = Path(args.bundle) if args.bundle else project_bundle_path()
    bundle = TermsBundle(bundle_path)
    codes = parse_bond_codes(args.codes) if args.codes else bundle.list_bonds()
    if not codes:
        print("没有可回测的转债代码", file=sys.stderr)
        return 2

    base_provider = build_batch_provider(
        args.source,
        terms_cache=bundle,
        csv_root=args.csv_root or None,
    )
    history_store = TermsHistoryStore(args.terms_history_dir) if args.terms_history_dir else None
    patch_store = TermsPatchStore(
        Path(args.terms_patches) if args.terms_patches else project_terms_patches_path()
    )
    event_store = CBEventStore(Path(args.events) if args.events else project_events_path())
    provider = HistoricalBondDataProvider(
        base_provider,
        history_store=history_store,
        patch_store=patch_store,
        event_store=event_store,
    )
    disk_cache = None
    if args.cache_dir:
        disk_cache = DiskCacheProvider(provider, args.cache_dir)
        provider = disk_cache
    admission_config = AdmissionFilterConfig(
        delist_window_days=max(0, args.delist_window),
        min_outstanding_balance=None if args.min_balance < 0 else args.min_balance,
        min_credit_rating=args.min_rating.strip() or None,
        min_turnover_amount=None if args.min_turnover < 0 else args.min_turnover,
    )
    strategy_config = ScoreStrategyConfig(
        top_n=args.top_n,
        rebalance_freq=args.freq,
        selection_view=args.selection_view,
        min_score=args.min_score,
        min_confidence=None if args.allow_low_confidence else ("高", "中"),
        exclude_risk_tags=() if args.include_review_risks else ScoreStrategyConfig().exclude_risk_tags,
        min_market_price=args.min_price,
        max_market_price=args.max_price,
        min_conversion_premium=(args.min_premium / 100.0) if args.min_premium is not None else None,
        max_conversion_premium=(args.max_premium / 100.0) if args.max_premium is not None else None,
        min_deviation=(args.min_deviation / 100.0) if args.min_deviation is not None else None,
        max_deviation=(args.max_deviation / 100.0) if args.max_deviation is not None else None,
        min_sigma=(args.min_sigma / 100.0) if args.min_sigma is not None else None,
        max_sigma=(args.max_sigma / 100.0) if args.max_sigma is not None else None,
        price_lookback_days=max(1, args.price_lookback_days),
        max_price_staleness_days=max(0, args.max_price_staleness_days),
        execution_timing=args.execution_timing,
        execution_lookahead_days=max(1, args.execution_lookahead_days),
        mark_to_market=not args.no_mark_to_market,
        transaction_cost=max(0.0, args.cost_bps) / 10000.0,
        compute_benchmark=not args.no_benchmark,
        benchmark_index_code=args.benchmark_index.strip() or None,
        pool_mode=args.pool_mode,
        holding_mode=args.holding_mode,
        max_holdings=args.max_holdings,
        funding_mode=args.funding_mode,
        exposure_mode=args.exposure_mode,
        cash_yield_rate=max(0.0, args.cash_yield),
    )
    if args.M is not None or args.N is not None:
        grid_M = args.M or 300
        grid_N = args.N or 1000
    elif args.mode == "fast":
        grid_M, grid_N = 120, 400
    elif args.mode == "precise":
        grid_M, grid_N = 300, 1000
    else:
        grid_M, grid_N = 220, 700

    try:
        result = backtest_score_strategy(
            provider,
            codes,
            start_date=args.start,
            end_date=args.end,
            config=strategy_config,
            terms_cache=None,
            admission_config=admission_config,
            r=args.r,
            base_spread=args.base_spread,
            distress_k=args.distress_k,
            p_down=args.p_down,
            vol_window_days=args.vol_window,
            M=grid_M,
            N=grid_N,
            max_workers=args.max_workers,
        )
    finally:
        # 中途异常/中断也要落盘已拉取的昂贵缓存
        if disk_cache is not None:
            disk_cache.flush()
    summary = result["summary"]
    print(f"区间: {result['start_date']} ~ {result['end_date']}")
    print(f"样本池: {len(codes)} | top_n: {summary['top_n']} | 调仓: {summary['rebalance_freq']}")
    print(f"模式: {args.mode} | 网格: M={grid_M}, N={grid_N}")
    print(f"成交: {strategy_config.execution_timing} | 日频净值: {'是' if strategy_config.mark_to_market else '否'}")
    print(f"期数: {summary['periods']}")
    print(f"最终净值: {summary['final_equity']:.4f}")
    print(f"总收益: {_fmt_pct(summary['total_return'])}")
    print(f"年化收益: {_fmt_pct(summary['annualized_return'])}")
    print(f"年化波动: {_fmt_pct(summary['annualized_volatility'])}")
    print(f"最大回撤: {_fmt_pct(summary['max_drawdown'])}")
    sharpe = summary.get("sharpe")
    print(f"Sharpe: {sharpe:.2f}" if sharpe is not None else "Sharpe: -")
    sortino = summary.get("sortino")
    calmar = summary.get("calmar")
    print(f"Sortino: {sortino:.2f}" if sortino is not None else "Sortino: -")
    print(f"Calmar: {calmar:.2f}" if calmar is not None else "Calmar: -")
    print(f"胜率: {_fmt_pct(summary['hit_rate'])}")
    print(f"平均换手: {_fmt_pct(summary['avg_turnover'])}")
    print(f"平均现金: {_fmt_pct(summary.get('avg_cash_weight'))}")
    print(f"累计成本: {_fmt_pct(summary.get('total_cost'))}")
    if summary.get("benchmark_final_equity") is not None:
        print(f"基准净值: {summary['benchmark_final_equity']:.4f}")
        print(f"基准收益: {_fmt_pct(summary['benchmark_total_return'])}")
        print(f"超额收益: {_fmt_pct(summary['excess_return'])}")
    if summary.get("index_benchmark_total_return") is not None:
        print(f"指数基准({args.benchmark_index}): {_fmt_pct(summary['index_benchmark_total_return'])}"
              f" | 超额 {_fmt_pct(summary['excess_vs_index'])}")
    _print_stability(summary.get("stability"))
    diagnostics = result.get("diagnostics") or {}
    performance = diagnostics.get("performance") or {}
    if performance:
        print(
            "缓存: "
            f"定价命中 {performance.get('pricing_snapshot_hits', 0)} / "
            f"未命中 {performance.get('pricing_snapshot_misses', 0)}, "
            f"价格预筛剔除 {performance.get('price_prefilter_excluded', 0)}"
        )
    warnings = diagnostics.get("warnings") or []
    if warnings:
        print("\n风险提示:")
        for warning in warnings:
            print(f"  - {warning}")
    attribution = diagnostics.get("attribution") or {}
    top_contributors = attribution.get("top_contributors") or []
    top_detractors = attribution.get("top_detractors") or []
    if top_contributors:
        print("\n贡献最大:")
        for row in top_contributors[:5]:
            print(f"  {row.get('bond_code')} {row.get('bond_name')}: {_fmt_pct(row.get('contribution'))}")
    if top_detractors:
        print("\n拖累最大:")
        for row in top_detractors[:5]:
            print(f"  {row.get('bond_code')} {row.get('bond_name')}: {_fmt_pct(row.get('contribution'))}")

    if args.show_holdings:
        print("\n逐期持仓:")
        for row in result["periods"]:
            codes_text = ", ".join(row.get("selected_codes") or []) or "-"
            print(
                f"  {row['start_date']} -> {row['end_date']} "
                f"{_fmt_pct(row['period_return'])}: {codes_text}"
            )

    if args.output:
        out_path = Path(args.output)
        write_strategy_backtest_csv(out_path, result)
        print(f"\n已导出: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
