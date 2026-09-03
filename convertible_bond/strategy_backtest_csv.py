"""策略回测结果 → CSV 导出.

从 ``strategy_backtest`` 里**原样搬出来**的序列化层 (逻辑一个字节没改)。

搬出来的理由不是"文件太长"这句空话, 而是这一簇**真的自成一体**: 它引用的三个模块级
私有名 (``_csv_value`` / ``_PERIOD_CSV_COLUMNS`` / ``_SUMMARY_CSV_KEYS``) 在原文件里
只被这几个函数用到, 且反向零依赖 —— 它不碰回测引擎的任何状态。对照之下, 统计那一簇
(``_summarize_strategy`` / ``_stability_stats``) 看着像同类却**不是**: 它要
``_equity_curve_returns`` / ``_curve_periods_per_year`` / ``_periods_per_year`` /
``_drawdown_stats`` 四个引擎侧的私有名, 其中两个还被 ``strategy_sweep`` 与
``gui.controllers.strategy_snapshots`` 从 ``strategy_backtest`` 直接导入, 所以它留在原地。

``write_strategy_backtest_csv`` 由 ``strategy_backtest`` 再导出, 四个既有导入点
(``convertible_bond/__init__``、CLI、GUI 快照控制器、测试) 的路径一律不变。
"""
from __future__ import annotations

import csv
import math
from datetime import date
from pathlib import Path
from typing import Any


def _csv_value(value: Any):
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float):
        return "" if math.isnan(value) else f"{value:.8f}"
    if value is None:
        return ""
    return value


_SUMMARY_CSV_KEYS = (
    "periods", "final_equity", "total_return", "annualized_return",
    "annualized_volatility", "volatility_basis", "sharpe", "sortino",
    "calmar", "max_drawdown", "max_drawdown_days", "hit_rate",
    "avg_selected_count", "avg_turnover", "avg_cash_weight", "avg_end_cash_weight",
    "total_event_exits", "total_cost",
    "benchmark_final_equity", "benchmark_total_return", "excess_return",
    "index_benchmark_total_return", "excess_vs_index", "index_covers_full_window",
)


_PERIOD_CSV_COLUMNS = [
    "start_date", "end_date", "entry_date", "exit_date",
    "period_return", "gross_return", "cash_yield_return", "event_exit_cash_yield_return",
    "cost",
    "benchmark_return", "equity", "benchmark_equity", "turnover", "cash_weight",
    "average_cash_weight",
    "end_cash_weight", "rebalance_turnover", "event_exit_turnover", "event_exit_count",
    "exposure", "median_deviation",
    "eligible_count", "priced_count", "candidate_count", "selected_count",
    "rank_signal", "avg_rank_value", "execution_timing", "selected_codes",
]


def _flatten_period_rows(periods: list[dict[str, Any]], key: str) -> list[tuple[dict, dict]]:
    """把每个区间下 ``period[key]`` 的明细行摊平成 (period, row) 对, 供持仓/候选/拒绝区块复用."""
    return [(period, row) for period in periods for row in period.get(key, [])]


def _write_csv_config(writer, config: dict[str, Any]) -> None:
    if not config:
        return
    writer.writerow(["# config"])
    for key, value in config.items():
        writer.writerow([key, _csv_value(value)])
    writer.writerow([])


def _write_csv_periods(writer, periods: list[dict[str, Any]]) -> None:
    writer.writerow(_PERIOD_CSV_COLUMNS)
    for row in periods:
        values = []
        for column in _PERIOD_CSV_COLUMNS:
            if column == "selected_codes":
                value = "|".join(str(code) for code in row.get(column) or [])
            else:
                value = _csv_value(row.get(column))
            values.append(value)
        writer.writerow(values)


def _write_csv_equity_curve(writer, curve: list[dict[str, Any]]) -> None:
    if not curve:
        return
    writer.writerow([])
    writer.writerow(["# equity_curve"])
    writer.writerow(["date", "equity"])
    for row in curve:
        writer.writerow([_csv_value(row.get("date")), _csv_value(row.get("equity"))])


def _write_csv_positions(writer, periods: list[dict[str, Any]]) -> None:
    positions = _flatten_period_rows(periods, "positions")
    if not positions:
        return
    writer.writerow([])
    writer.writerow(["# positions"])
    writer.writerow([
        "period_start", "period_end", "rank", "bond_code", "bond_name",
        "rank_signal", "rank_value", "signal_market_price", "theoretical_price",
        "deviation", "effective_p_down_1y_prob",
        "entry_date", "exit_date", "start_price", "end_price",
        "price_return", "post_exit_cash_return", "period_return",
        "exit_reason", "exit_signal_date", "exit_event_type", "exit_event_title",
        "confidence", "risk_tags",
    ])
    for period, pos in positions:
        writer.writerow([
            _csv_value(period.get("start_date")),
            _csv_value(period.get("end_date")),
            pos.get("rank", ""),
            pos.get("bond_code", ""),
            pos.get("bond_name", ""),
            pos.get("rank_signal", ""),
            _csv_value(pos.get("rank_value")),
            _csv_value(pos.get("signal_market_price")),
            _csv_value(pos.get("theoretical_price")),
            _csv_value(pos.get("deviation")),
            _csv_value(pos.get("effective_p_down_1y_prob")),
            _csv_value(pos.get("entry_date")),
            _csv_value(pos.get("exit_date")),
            _csv_value(pos.get("start_price")),
            _csv_value(pos.get("end_price")),
            _csv_value(pos.get("price_return")),
            _csv_value(pos.get("post_exit_cash_return")),
            _csv_value(pos.get("period_return")),
            pos.get("exit_reason", ""),
            _csv_value(pos.get("exit_signal_date")),
            pos.get("exit_event_type", ""),
            pos.get("exit_event_title", ""),
            pos.get("confidence", ""),
            "|".join(str(tag) for tag in pos.get("risk_tags") or []),
        ])


def _write_csv_skipped_positions(writer, periods: list[dict[str, Any]]) -> None:
    skipped = _flatten_period_rows(periods, "skipped_positions")
    if not skipped:
        return
    writer.writerow([])
    writer.writerow(["# skipped_positions"])
    writer.writerow([
        "period_start", "period_end", "rank", "bond_code", "bond_name",
        "rank_signal", "rank_value", "reason", "entry_date", "exit_date",
        "start_price", "end_price",
    ])
    for period, pos in skipped:
        writer.writerow([
            _csv_value(period.get("start_date")),
            _csv_value(period.get("end_date")),
            pos.get("rank", ""),
            pos.get("bond_code", ""),
            pos.get("bond_name", ""),
            pos.get("rank_signal", ""),
            _csv_value(pos.get("rank_value")),
            pos.get("reason", ""),
            _csv_value(pos.get("entry_date")),
            _csv_value(pos.get("exit_date")),
            _csv_value(pos.get("start_price")),
            _csv_value(pos.get("end_price")),
        ])


def _write_csv_candidate_rows(writer, periods: list[dict[str, Any]]) -> None:
    candidate_rows = _flatten_period_rows(periods, "candidate_rows")
    if not candidate_rows:
        return
    writer.writerow([])
    writer.writerow(["# candidate_rows"])
    writer.writerow([
        "period_start", "period_end", "rank", "selected", "bond_code", "bond_name",
        "selection_reason", "rank_signal", "rank_value", "market_price",
        "theoretical_price", "deviation", "effective_p_down_1y_prob",
        "conversion_premium", "sigma", "confidence", "risk_tags",
    ])
    for period, row in candidate_rows:
        writer.writerow([
            _csv_value(period.get("start_date")),
            _csv_value(period.get("end_date")),
            row.get("rank", ""),
            row.get("selected", ""),
            row.get("bond_code", ""),
            row.get("bond_name", ""),
            row.get("selection_reason", ""),
            row.get("rank_signal", ""),
            _csv_value(row.get("rank_value")),
            _csv_value(row.get("market_price")),
            _csv_value(row.get("theoretical_price")),
            _csv_value(row.get("deviation")),
            _csv_value(row.get("effective_p_down_1y_prob")),
            _csv_value(row.get("conversion_premium")),
            _csv_value(row.get("sigma")),
            row.get("confidence", ""),
            "|".join(str(tag) for tag in row.get("risk_tags") or []),
        ])


def _write_csv_rejection_rows(writer, periods: list[dict[str, Any]]) -> None:
    rejection_rows = _flatten_period_rows(periods, "rejection_rows")
    if not rejection_rows:
        return
    writer.writerow([])
    writer.writerow(["# rejection_rows"])
    writer.writerow([
        "period_start", "period_end", "source", "bond_code", "bond_name",
        "reason", "rank_signal", "rank_value", "market_price", "deviation",
        "effective_p_down_1y_prob",
        "conversion_premium", "confidence", "risk_tags",
    ])
    for period, row in rejection_rows:
        writer.writerow([
            _csv_value(period.get("start_date")),
            _csv_value(period.get("end_date")),
            row.get("source", ""),
            row.get("bond_code", ""),
            row.get("bond_name", ""),
            row.get("reason", ""),
            row.get("rank_signal", ""),
            _csv_value(row.get("rank_value")),
            _csv_value(row.get("market_price")),
            _csv_value(row.get("deviation")),
            _csv_value(row.get("effective_p_down_1y_prob")),
            _csv_value(row.get("conversion_premium")),
            row.get("confidence", ""),
            "|".join(str(tag) for tag in row.get("risk_tags") or []),
        ])


def _write_csv_summary(writer, summary: dict[str, Any]) -> None:
    if not summary:
        return
    writer.writerow([])
    writer.writerow(["# summary"])
    for key in _SUMMARY_CSV_KEYS:
        writer.writerow([key, _csv_value(summary.get(key))])


def _write_csv_diagnostics(writer, diagnostics: dict[str, Any]) -> None:
    if not diagnostics:
        return
    writer.writerow([])
    writer.writerow(["# diagnostics"])
    data_quality = diagnostics.get("data_quality") or {}
    for key, value in data_quality.items():
        writer.writerow([f"data_quality.{key}", _csv_value(value)])
    attribution = diagnostics.get("attribution") or {}
    for key in ("total_cost", "avg_cash_weight", "skipped_positions", "cost_drag"):
        writer.writerow([f"attribution.{key}", _csv_value(attribution.get(key))])
    warnings = diagnostics.get("warnings") or []
    for idx, warning in enumerate(warnings, start=1):
        writer.writerow([f"warning.{idx}", warning])
    performance = diagnostics.get("performance") or {}
    for key, value in performance.items():
        writer.writerow([f"performance.{key}", _csv_value(value)])
    for section, rows in (
        ("top_contributors", attribution.get("top_contributors") or []),
        ("top_detractors", attribution.get("top_detractors") or []),
        ("yearly_returns", diagnostics.get("yearly_returns") or []),
        ("monthly_returns", diagnostics.get("monthly_returns") or []),
    ):
        if not rows:
            continue
        writer.writerow([])
        writer.writerow([f"# {section}"])
        keys = list(rows[0].keys())
        writer.writerow(keys)
        for row in rows:
            writer.writerow([_csv_value(row.get(key)) for key in keys])


def write_strategy_backtest_csv(path: str | Path, result: dict[str, Any]) -> None:
    """导出策略回测的逐期摘要、日频净值、持仓明细和汇总指标 CSV.

    各区块由独立的 ``_write_csv_*`` 辅助函数写出 (有数据才写空行+标题), 顺序:
    config / 逐期摘要 / equity_curve / positions / skipped_positions /
    candidate_rows / rejection_rows / summary / diagnostics。
    """
    periods = result.get("periods", [])
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        _write_csv_config(writer, result.get("config") or {})
        _write_csv_periods(writer, periods)
        _write_csv_equity_curve(writer, result.get("equity_curve") or [])
        _write_csv_positions(writer, periods)
        _write_csv_skipped_positions(writer, periods)
        _write_csv_candidate_rows(writer, periods)
        _write_csv_rejection_rows(writer, periods)
        _write_csv_summary(writer, result.get("summary") or {})
        _write_csv_diagnostics(writer, result.get("diagnostics") or {})
