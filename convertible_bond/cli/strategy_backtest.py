"""CBLens 错定价策略回测.

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
    PDEStrategyConfig,
    backtest_pde_strategy,
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


def _risk_threshold_kwargs(args) -> dict:
    """取代旧标签排除集的那组风险阈值。

    ``--include-review-risks`` 把它们整组放开 —— 有效性守卫 (缺市价/理论价/转股价值)
    与用户显式传的区间不动。该开关此前写的是 ``exclude_risk_tags=()``, 而标签排除
    已经不是主口径, 保持原样会让它变成一个静默的 no-op。

    ``max_sigma`` 留空 = **沿用默认上限** (0.80, 即旧「高HV」判据) 而不是关掉它:
    重构前留空时那道闸由「高HV」标签照常生效, 所以这才是保行为的读法。
    要真的不设上限, 传一个大数或用 ``--include-review-risks``。

    单独抽出来是为了**可测**, 也为了避免它和调用点里另一个 ``max_sigma=`` 撞成
    重复关键字 —— 那会让 ``--include-review-risks`` 直接 TypeError 崩掉。
    """
    if args.include_review_risks:
        return dict(
            max_sigma=None,
            max_model_premium=None,
            max_relative_deviation=None,
            min_years_to_maturity=None,
            min_credit_rating=None,
            min_outstanding_balance=None,
            exclude_underlying_st=False,
            exclude_underlying_limit_down=False,
        )
    return dict(
        max_sigma=((args.max_sigma / 100.0) if args.max_sigma is not None
                   else PDEStrategyConfig.max_sigma),
    )


def main() -> int:
    default_min_balance = (
        DEFAULT_MIN_OUTSTANDING_BALANCE
        if DEFAULT_MIN_OUTSTANDING_BALANCE is not None
        else -1.0
    )
    parser = argparse.ArgumentParser(
        description="按 CBLens 估值偏差信号做定期调仓回测",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--source", default="akshare", choices=["wind", "akshare", "csv"],
                        help="动态行情数据源 (默认 akshare)")
    parser.add_argument(
        "--history-mode",
        default="standard",
        choices=["standard", "wind-high-fidelity"],
        help="历史条款口径: standard=本地修正; "
             "wind-high-fidelity=Wind tradeDate历史截面 (正式结论推荐)",
    )
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
                        help="每次调仓按策略信号选债数量; 不足时留现金 (默认 10)")
    # 下修优势两档已删 —— 隐含下修强度反解在两个 regime 都结构性无解 (谷底
    # 市价 < price(λ=0)、高位 市价 > price(λ=3))。旧脚本传的值由
    # _normalize_rank_signal 落到 deviation, 所以 choices 里仍然接受它们。
    parser.add_argument("--rank-signal", default="deviation",
                        choices=[
                            "deviation", "down_reset_edge", "down_reset_robust_edge",
                        ],
                        help="排序信号: deviation=市价/理论价偏差升序 (默认)。"
                             "down_reset_edge / down_reset_robust_edge 已删除, "
                             "传入会退化为 deviation")
    # 曾是 --no-down-reset-event-exit (store_false, 默认开启), 因为那时它还被
    # `and is_down_reset` 二次把关 —— 按 deviation 排序时恒为 False。下修优势信号删掉
    # 之后那道把关没了, 留着 store_false 会让事件退出**突然对所有回测生效**, 而那是
    # 默认选债行为变更。改成显式开启, 与 ScoreStrategyConfig 的默认值对齐。
    parser.add_argument(
        "--down-reset-event-exit",
        action="store_true",
        dest="down_reset_event_exit",
        help="下修提议/通过/拒绝公告后在下一可得收盘退出并持有现金 (默认关闭)",
    )
    parser.add_argument("--exposure-mode", default="full",
                        choices=["full", "valuation"],
                        help="D仓位层: full=恒定满仓(默认); valuation=按当期已定价池中位偏差"
                             "缩放总仓位 (研究配置, 依据见 docs/research/2026-06-*)")
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
                        help="放开取代标签的那组风险阈值 (评级/余额/剩余年限/σ/模型溢价/"
                             "相对偏差/正股ST/正股跌停), 只保留有效性守卫与显式区间")
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
    parser.add_argument("--cost-bps", type=float, default=20.0,
                        help="单边换手对应的交易成本, 单位 bps; 区间净收益扣 turnover*成本 (默认 20); "
                             "基准同口径计成员变动换手成本")
    parser.add_argument("--cash-yield", type=float, default=0.022,
                        help="闲置现金年化收益率小数 (默认 0.022)")
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
    parser.add_argument("--p-down", type=float, default=0.25,
                        help="年化下修事件强度 (默认 0.25)")
    # 正股股息率 q。**回测里这是最贵的一次取数, 而且口径可疑**: akshare 走
    # stock_a_indicator_lg 逐只股票拉, 失败再回落 stock_zh_a_spot_em (整张全市场
    # 现货快照), 每只 3 次重试 —— 实测 2024-09 那个窗口 623 只债全部失败, 光这一步
    # 就耗掉 55 分钟, 而失败后本来就回落 q=0, 结果与显式传 0 完全一样。
    # 更根本的是它取的是**实时**快照, 却拿去给历史估值日用。
    #
    # 显式给一个值就整段跳过取数 (price_from_provider: q is None 才去 provider 取)。
    # **信号对照场景下这是正确做法** —— 两次回测用同一个 q, 比较才公平; 而 q=0 本来
    # 就是 README 记录的模型边界 ("数据源缺失时默认 0")。
    # ``backtest_disk_cache`` 不缓存 q (bond_history/stock_history/terms 三样都缓存了,
    # 只有它是直接透传), 补那个缓存是另一件事。
    parser.add_argument("--q", type=float, default=None,
                        help="正股股息率 (%%, 例 1.5)。给了就跳过逐只联网取数; "
                             "不给则按数据源取, 取不到回落 0")
    parser.add_argument("--vol-window", type=int, default=21,
                        help="历史波动率窗口交易日数 (默认 21)")
    parser.add_argument("--M", type=int, default=None,
                        help="覆盖定价价格网格 M")
    parser.add_argument("--N", type=int, default=None,
                        help="覆盖定价时间网格 N")
    parser.add_argument("--max-workers", type=int, default=4,
                        help="批量定价线程数 (默认 4)")
    parser.add_argument("--output", "-o", default="",
                        help="导出逐期摘要 CSV")
    parser.add_argument("--show-holdings", action="store_true",
                        help="打印每期选中持仓")
    args = parser.parse_args()
    if args.pde_sigma_band < 0 or args.pde_spread_band < 0:
        parser.error("稳健情景扰动带不能为负")
    if args.p_down < 0:
        parser.error("--p-down 不能为负")
    if args.top_n <= 0:
        parser.error("--top-n 必须为正整数")
    if args.history_mode == "wind-high-fidelity" and args.source != "wind":
        parser.error("--history-mode wind-high-fidelity 需要 --source wind")

    bundle_path = Path(args.bundle) if args.bundle else project_bundle_path()
    bundle = TermsBundle(bundle_path)
    codes = parse_bond_codes(args.codes) if args.codes else bundle.list_bonds()
    if not codes:
        print("没有可回测的转债代码", file=sys.stderr)
        return 2

    base_provider = build_batch_provider(
        args.source,
        # Wind高保真必须绕过 cb_data 当前条款缓存，才会按 tradeDate 查询。
        terms_cache=None if args.history_mode == "wind-high-fidelity" else bundle,
        csv_root=args.csv_root or None,
    )
    history_store = TermsHistoryStore(args.terms_history_dir) if args.terms_history_dir else None
    patch_store = TermsPatchStore(
        Path(args.terms_patches) if args.terms_patches else project_terms_patches_path()
    )
    event_store = CBEventStore(Path(args.events) if args.events else project_events_path())
    if args.history_mode == "wind-high-fidelity":
        provider = HistoricalBondDataProvider(
            base_provider,
            history_store=None,
            patch_store=patch_store,
            event_store=event_store,
            strip_fallback_status=True,
            merge_admission_status=False,
            provider_history_terms=True,
        )
    else:
        provider = HistoricalBondDataProvider(
            base_provider,
            history_store=history_store,
            patch_store=patch_store,
            event_store=event_store,
            strip_fallback_status=False,
            merge_admission_status=True,
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
    max_deviation = args.max_deviation
    if max_deviation is None:
        max_deviation = 0.0
    strategy_config = PDEStrategyConfig(
        top_n=args.top_n,
        rebalance_freq=args.freq,
        selection_view="综合机会",
        min_confidence=None if args.allow_low_confidence else ("高", "中"),
        **_risk_threshold_kwargs(args),
        min_market_price=args.min_price,
        max_market_price=args.max_price,
        min_conversion_premium=(args.min_premium / 100.0) if args.min_premium is not None else None,
        max_conversion_premium=(args.max_premium / 100.0) if args.max_premium is not None else None,
        min_deviation=(args.min_deviation / 100.0) if args.min_deviation is not None else None,
        max_deviation=(max_deviation / 100.0) if max_deviation is not None else None,
        min_sigma=(args.min_sigma / 100.0) if args.min_sigma is not None else None,
        price_lookback_days=max(1, args.price_lookback_days),
        max_price_staleness_days=max(0, args.max_price_staleness_days),
        execution_timing=args.execution_timing,
        execution_lookahead_days=max(1, args.execution_lookahead_days),
        mark_to_market=not args.no_mark_to_market,
        transaction_cost=max(0.0, args.cost_bps) / 10000.0,
        compute_benchmark=not args.no_benchmark,
        benchmark_index_code=args.benchmark_index.strip() or None,
        pool_mode=args.pool_mode,
        holding_mode="top_score",
        rank_signal=args.rank_signal,
        down_reset_event_exit=bool(args.down_reset_event_exit),
        max_holdings=None,
        funding_mode="reserve_cash",
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
    effective_max_workers = (
        1 if args.history_mode == "wind-high-fidelity" else max(1, args.max_workers)
    )

    try:
        result = backtest_pde_strategy(
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
            q=(args.q / 100.0) if args.q is not None else None,
            M=grid_M,
            N=grid_N,
            max_workers=effective_max_workers,
        )
    finally:
        # 中途异常/中断也要落盘已拉取的昂贵缓存
        if disk_cache is not None:
            disk_cache.flush()
    result_config = dict(result.get("config") or {})
    result_config["history_mode"] = (
        "Wind高保真" if args.history_mode == "wind-high-fidelity" else "标准"
    )
    result["config"] = result_config
    result["run_settings"] = {
        "data_source": args.source,
        "history_mode": result_config["history_mode"],
        "strategy": {
            "template": "估值偏差",
            **result_config,
        },
        "pricing": {
            "r": args.r,
            "base_spread": args.base_spread,
            "distress_k": args.distress_k,
            "p_down": args.p_down,
            "vol_window_days": args.vol_window,
            "q": args.q,
            "M": grid_M,
            "N": grid_N,
            "max_workers": effective_max_workers,
        },
    }
    summary = result["summary"]
    print(f"区间: {result['start_date']} ~ {result['end_date']}")
    print(f"样本池: {len(codes)} | top_n: {summary['top_n']} | 调仓: {summary['rebalance_freq']}")
    print(f"模式: {args.mode} | 网格: M={grid_M}, N={grid_N}")
    print(
        f"策略信号: {strategy_config.rank_signal} | p_down={args.p_down:.2%} | "
        f"HV扰动=±{args.pde_sigma_band:.0%} | "
        f"利差扰动=±{args.pde_spread_band*10000:.0f}bp"
    )
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
