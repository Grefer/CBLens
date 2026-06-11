"""策略回测 — 结果渲染框架与概览/明细 (tab 调度/图表/表格/小部件).

从 strategy_backtest.py 按职责拆出; 经 StrategyBacktestMixin 聚合混入
CBPricerApp, 方法间通过 self.* 跨 mixin 调用不受拆分影响。
"""
from __future__ import annotations

from tkinter import ttk

import customtkinter as ctk
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

from ..theme import (
    ACCENT,
    BG_CARD,
    BG_INPUT,
    BORDER,
    GREEN,
    ORANGE,
    RED,
    TEXT,
    TEXT_DIM,
    FONT_FAMILY,
    get_color,
)
from ..tabs.batch_common import (
    _TREE_ATTRS,
    _attach_column_sort,
    _configure_responsive_columns,
    _configure_tree_style,
)
from ..widgets import Tooltip

from .strategy_common import STRATEGY_DETAIL_TABLE_HEIGHT, STRATEGY_OVERVIEW_CHART_HEIGHT


class StrategyRenderMixin:
    """策略回测 — 结果渲染框架与概览/明细 (tab 调度/图表/表格/小部件)."""

    # ── 懒渲染: 子页 tab 名 → 渲染函数映射 ──────────────────
    _STRATEGY_TAB_RENDERERS = {
        "总览": "_render_strategy_overview_tab",
        "明细": "_render_strategy_detail_tab",
        "归因": "_render_strategy_attribution_tab",
        "风险": "_render_strategy_risk_tab",
        "稳健性": "_render_strategy_robustness_tab",  # legacy alias, kept for tests/old callbacks
        "数据": "_render_strategy_data_tab",
        "对比": "_render_strategy_compare_tab",
    }

    def _mark_strategy_tabs_dirty(self, *tab_names):
        dirty = getattr(self, "_strategy_dirty_tabs", set())
        if tab_names:
            dirty |= set(tab_names)
        else:
            dirty = set(self._STRATEGY_TAB_RENDERERS.keys())
        self._strategy_dirty_tabs = dirty

    def _render_strategy_backtest_result(self, result):
        """入口: 更新摘要 + 标记全部子页为 dirty + 渲染当前子页."""
        self._mark_strategy_tabs_dirty()
        self._update_strategy_result_summary(result, reset_figures=True)
        self._render_current_strategy_tab(force=True)

    def _on_strategy_result_tab_change(self):
        """子页 Tabview command 回调: 切到哪页, 渲染哪页."""
        self._render_current_strategy_tab()

    def _render_current_strategy_tab(self, *, force=False):
        """只渲染当前选中的子页 (dirty 或 force 时才重绘)."""
        result = getattr(self, "_last_strategy_bt_result", None)
        if not isinstance(result, dict):
            return
        tabs = getattr(self, "strategy_result_tabs", None)
        if tabs is None:
            return
        selected = tabs.get()
        dirty = getattr(self, "_strategy_dirty_tabs", None)
        if dirty is None:
            dirty = set(self._STRATEGY_TAB_RENDERERS.keys())
            self._strategy_dirty_tabs = dirty
        if not force and selected not in dirty:
            return
        renderer_name = self._STRATEGY_TAB_RENDERERS.get(selected)
        if renderer_name is None:
            return
        renderer = getattr(self, renderer_name, None)
        if renderer is None:
            return
        try:
            renderer(result)
        except Exception as exc:
            import traceback
            traceback.print_exc()
            print(f"[策略回测] 渲染 '{selected}' 失败: {exc}")
            return
        dirty.discard(selected)
        if hasattr(self, "update_idletasks"):
            self.update_idletasks()

    def _clear_active_strategy_backtest_result(self, *, status: str | None = None):
        """清空当前活跃策略回测结果, 防止删除快照后其他页继续显示旧数据."""
        self._last_strategy_bt_result = None
        self._mark_strategy_tabs_dirty()
        self._reset_strategy_stats()
        if hasattr(self, "strategy_bt_progress"):
            self.strategy_bt_progress.set(0)
        btn_csv = getattr(self, "btn_strategy_bt_csv", None)
        if btn_csv is not None:
            try:
                btn_csv.configure(state="disabled")
            except Exception:
                pass
        for fig_attr in ("_strategy_bt_waterfall_fig", "_strategy_bt_heatmap_fig",
                         "_strategy_bt_rolling_fig", "_strategy_bt_dist_fig",
                         "_strategy_bt_compare_fig"):
            fig = getattr(self, fig_attr, None)
            if fig is not None:
                fig.clf()
                plt.close(fig)
                setattr(self, fig_attr, None)
        for frame_attr in (
            "strategy_bt_insight_frame",
            "strategy_bt_chart_frame",
            "strategy_bt_selection_frame",
            "strategy_bt_table_frame",
            "strategy_bt_attribution_frame",
            "strategy_bt_risk_frame",
            "strategy_bt_data_frame",
        ):
            frame = getattr(self, frame_attr, None)
            if frame is not None:
                self._clear_strategy_panel(frame)
        compare_frame = getattr(self, "strategy_bt_compare_frame", None)
        if compare_frame is not None:
            self._render_strategy_comparison()
        if status and hasattr(self, "v_st_status"):
            self.v_st_status.set(status)

    def _reset_strategy_stats(self):
        stats = getattr(self, "_strategy_stat_vars", None) or {}
        for var in stats.values():
            try:
                var.set("—")
            except Exception:
                pass
        labels = getattr(self, "_strategy_stat_labels", None) or {}
        for label in labels.values():
            try:
                label.configure(text_color=get_color(TEXT))
            except Exception:
                pass

    def _update_strategy_result_summary(self, result, *, reset_figures=False):
        """只更新指标卡、状态栏、CSV 按钮 (不渲染任何子页面板)."""
        if reset_figures:
            # 新结果/主题刷新时清理旧 figure; 普通切回策略页只更新摘要, 避免把已渲染子页清空。
            for fig_attr in ("_strategy_bt_waterfall_fig", "_strategy_bt_heatmap_fig",
                             "_strategy_bt_rolling_fig", "_strategy_bt_dist_fig",
                             "_strategy_bt_compare_fig"):
                fig = getattr(self, fig_attr, None)
                if fig is not None:
                    fig.clf()
                    plt.close(fig)
                    setattr(self, fig_attr, None)

        summary = result.get("summary", {})
        self._update_strategy_stats(summary)

        periods = result.get("periods", [])
        excess = summary.get("excess_return")
        extra = (f" · 超额 {excess*100:+.2f}%"
                 if excess is not None and np.isfinite(excess) else "")
        warnings = ((result.get("diagnostics") or {}).get("warnings") or [])
        warning_text = f" · 提醒: {warnings[0]}" if warnings else ""
        perf = ((result.get("diagnostics") or {}).get("performance") or {})
        perf_text = ""
        if perf:
            hits = int(perf.get("pricing_snapshot_hits") or 0)
            prefiltered = int(perf.get("price_prefilter_excluded") or 0)
            if hits or prefiltered:
                perf_text = f" · 缓存命中 {hits} / 预筛 {prefiltered}"
        self.v_st_status.set(
            f"✅ {len(periods)} 个调仓区间 · "
            f"最终净值 {summary.get('final_equity', 1.0):.4f}{extra}{perf_text}{warning_text}"
        )
        if hasattr(self, "strategy_bt_progress"):
            self.strategy_bt_progress.set(1.0)
        self.btn_strategy_bt_csv.configure(state="normal")

    # ── 各子页渲染入口 (被懒渲染调度器调用) ──────────────────
    def _render_strategy_overview_tab(self, result):
        self._render_strategy_insight(result)
        self._render_strategy_chart(result)

    def _render_strategy_detail_tab(self, result):
        self._render_strategy_selection_panel(result)
        self._render_strategy_table(result)

    def _on_strategy_detail_filter_change(self, *_):
        self._mark_strategy_tabs_dirty("明细")
        if hasattr(self, "after_idle"):
            self.after_idle(self._render_current_strategy_tab)
        else:
            self._render_current_strategy_tab()

    def _render_strategy_attribution_tab(self, result):
        self._render_strategy_attribution(result)

    def _render_strategy_risk_tab(self, result):
        self._render_strategy_risk_panel(result)

    def _render_strategy_robustness_tab(self, result):
        renderer = getattr(self, "_render_strategy_robustness_panel", None)
        if callable(renderer):
            renderer(result)
        else:
            self._render_strategy_risk_panel(result)

    def _render_strategy_data_tab(self, result):
        self._render_strategy_data_panel(result)

    def _render_strategy_compare_tab(self, result):
        self._render_strategy_comparison()

    def _update_strategy_stats(self, summary):
        stats = getattr(self, "_strategy_stat_vars", None)
        if not stats:
            return
        labels = getattr(self, "_strategy_stat_labels", {})
        green, red, base = get_color(GREEN), get_color(RED), get_color(TEXT)

        def pct(value, sign=False):
            if value is None or not np.isfinite(value):
                return "—"
            return f"{value*100:+.2f}%" if sign else f"{value*100:.2f}%"

        def colorize(key, value):
            lbl = labels.get(key)
            if lbl is None:
                return
            if value is None or not np.isfinite(value) or value == 0:
                lbl.configure(text_color=base)
            else:
                lbl.configure(text_color=green if value > 0 else red)

        final_equity = summary.get("final_equity")
        stats["final_equity"].set(f"{float(final_equity):.4f}" if final_equity is not None else "—")
        total_return = summary.get("total_return")
        annualized = summary.get("annualized_return")
        excess = summary.get("excess_return")
        sharpe = summary.get("sharpe")
        sortino = summary.get("sortino")
        calmar = summary.get("calmar")
        stats["total_return"].set(pct(total_return, sign=True))
        stats["annualized"].set(pct(annualized, sign=True))
        stats["excess"].set(pct(excess, sign=True))
        stats["max_drawdown"].set(pct(summary.get("max_drawdown")))
        stats["sharpe"].set(
            f"{sharpe:.2f}" if sharpe is not None and np.isfinite(sharpe) else "—")
        if "sortino" in stats:
            stats["sortino"].set(
                f"{sortino:.2f}" if sortino is not None and np.isfinite(sortino) else "—")
        if "calmar" in stats:
            stats["calmar"].set(
                f"{calmar:.2f}" if calmar is not None and np.isfinite(calmar) else "—")
        if "hit_rate" in stats:
            stats["hit_rate"].set(pct(summary.get("hit_rate")))
        if "cash" in stats:
            stats["cash"].set(pct(summary.get("avg_cash_weight")))
        stats["turnover"].set(pct(summary.get("avg_turnover")))
        colorize("total_return", total_return)
        colorize("annualized", annualized)
        colorize("excess", excess)
        colorize("sharpe", sharpe)
        colorize("sortino", sortino)
        colorize("calmar", calmar)

    def _render_strategy_insight(self, result):
        frame = getattr(self, "strategy_bt_insight_frame", None)
        if frame is None:
            return
        for child in frame.winfo_children():
            child.destroy()
        frame.grid_rowconfigure(0, weight=1)
        for col in range(4):
            frame.grid_columnconfigure(col, weight=1, uniform="strategy_insight")

        summary = result.get("summary") or {}
        diagnostics = result.get("diagnostics") or {}
        attribution = diagnostics.get("attribution") or {}
        data_quality = diagnostics.get("data_quality") or {}

        total_return = summary.get("total_return")
        excess = summary.get("excess_return")
        max_drawdown = summary.get("max_drawdown")
        fallback_ratio = float(data_quality.get("current_fallback_ratio") or 0.0)
        top_contrib = (attribution.get("top_contributors") or [{}])[0]
        top_name = top_contrib.get("bond_name") or top_contrib.get("bond_code") or "—"

        if total_return is not None and np.isfinite(total_return):
            if total_return > 0 and (excess is None or excess >= 0):
                verdict = "收益与基准对比均偏正"
            elif total_return > 0:
                verdict = "绝对收益为正, 但弱于基准"
            else:
                verdict = "策略区间收益为负"
        else:
            verdict = "暂无足够收益数据"
        quality = "高" if fallback_ratio <= 0 else ("中" if fallback_ratio <= 0.2 else "低")

        hints = {
            "结论": "解读铁律: 一切对照「等权基准」——跑不赢基准 = 这套规则没有超额,\n"
                    "哪怕绝对收益为正。机会分是复核标记、不是收益预测;\n"
                    "单一市场周期的回测不构成策略承诺 (详见 USAGE 4.2 与 README 模型边界)。",
            "最大回撤": "净值从峰值到谷底的最大跌幅 = 历史上最深要忍受的亏损。\n"
                       "对照基准回撤判断风险是否换来了收益; 与持仓集中度、现金缓冲相关。",
            "主要贡献": "收益贡献最大的单券。若总收益高度依赖个别券 (尤其强赎/退市收敛券),\n"
                       "说明结果偏尾部运气而非系统性能力, 复现性存疑——去「归因」页核对。",
            "数据质量": "条款回退占比 = 用当前条款顶替历史条款的样本比例, >0 有未来信息渗入风险。\n"
                       "下结论前先看「数据」子页的补丁覆盖率与缺价跳过数。",
        }
        items = [
            ("结论", verdict),
            ("最大回撤", (
                f"{self._fmt_strategy_pct(max_drawdown)} · "
                f"{summary.get('max_drawdown_start') or '—'} → {summary.get('max_drawdown_end') or '—'}"
            )),
            ("主要贡献", (
                f"{top_name} {self._fmt_strategy_pct(top_contrib.get('contribution'), sign=True)}"
            )),
            ("数据质量", f"{quality} · 条款回退占比 {self._fmt_strategy_pct(fallback_ratio)}"),
        ]
        for col, (title, value) in enumerate(items):
            cell = ctk.CTkFrame(frame, fg_color=BG_INPUT, corner_radius=8, height=76)
            cell.grid(row=0, column=col, sticky="nsew", padx=6, pady=(8, 4))
            cell.grid_propagate(False)
            
            inner = ctk.CTkFrame(cell, fg_color="transparent")
            inner.pack(fill="both", expand=True, padx=12, pady=8)
            
            ctk.CTkLabel(inner, text=title, text_color=TEXT_DIM,
                         font=(FONT_FAMILY, 11)).pack(anchor="w")
            value_label = ctk.CTkLabel(
                inner, text=value, text_color=TEXT,
                font=(FONT_FAMILY, 13, "bold"), wraplength=260,
                justify="left")
            value_label.pack(anchor="w")
            hint = hints.get(title)
            if hint:
                Tooltip(cell, hint)
                Tooltip(value_label, hint)

            def _update_wrap(event, label=value_label):
                label.configure(wraplength=max(180, event.width - 24))

            cell.bind("<Configure>", _update_wrap)

    def _render_strategy_chart(self, result):
        if self._strategy_bt_figure is not None:
            self._strategy_bt_figure.clf()
            plt.close(self._strategy_bt_figure)
            self._strategy_bt_figure = None
            self._strategy_bt_canvas = None

        for child in self.strategy_bt_chart_frame.winfo_children():
            child.destroy()
        self.strategy_bt_chart_frame.configure(height=STRATEGY_OVERVIEW_CHART_HEIGHT)
        self.strategy_bt_chart_frame.grid_columnconfigure(0, weight=1)
        self.strategy_bt_chart_frame.grid_rowconfigure(0, weight=1)
        self.strategy_bt_chart_frame.grid_propagate(False)

        curve = result.get("equity_curve") or []
        if not curve:
            return

        dates = [p["date"] for p in curve]
        equity = [float(p["equity"]) for p in curve]

        benchmark_curve = result.get("benchmark_curve") or []
        bench_dates = [p["date"] for p in benchmark_curve]
        bench_equity = [float(p["equity"]) for p in benchmark_curve]

        bg_card_color = get_color(BG_CARD)
        bg_input_color = get_color(BG_INPUT)
        text_dim_color = get_color(TEXT_DIM)
        text_color = get_color(TEXT)
        border_color = get_color(BORDER)
        accent_color = get_color(ACCENT)
        orange_color = get_color(ORANGE)
        red_color = get_color(RED)

        fig = Figure(figsize=(11, 5.2), dpi=100, facecolor=bg_card_color)
        gs = fig.add_gridspec(2, 1, height_ratios=[2.2, 0.9])
        ax_eq = fig.add_subplot(gs[0, 0], facecolor=bg_input_color)
        ax_dd = fig.add_subplot(gs[1, 0], facecolor=bg_input_color, sharex=ax_eq)

        # 净值: 策略 vs 等权基准
        ax_eq.plot(dates, equity, color=accent_color, linewidth=2.2, marker="o",
                   markersize=4, label="组合净值")
        if bench_equity:
            ax_eq.plot(bench_dates, bench_equity, color=orange_color, linewidth=1.6,
                       linestyle="--", marker="s", markersize=3, label="等权基准")
        ax_eq.axhline(1.0, color=border_color, linewidth=1.0, linestyle="--")

        # 标注最大回撤起止区间
        summary = result.get("summary") or {}
        dd_start = summary.get("max_drawdown_start")
        dd_end = summary.get("max_drawdown_end")
        max_dd = summary.get("max_drawdown")
        if dd_start and dd_end and max_dd:
            dd_values_all = self._strategy_drawdown_values(equity)
            dd_idx = int(np.argmin(dd_values_all)) if dd_values_all else None
            if dd_idx is not None and dd_idx < len(dates):
                ax_eq.axvspan(dd_start, dd_end, alpha=0.10, color=red_color, zorder=0)
                ax_eq.annotate(
                    f" 最大回撤 {max_dd*100:.1f}% ",
                    xy=(dates[dd_idx], equity[dd_idx]),
                    xytext=(30, -28), textcoords="offset points",
                    fontsize=10, fontweight="bold", color="#ffffff",
                    ha="left", va="top",
                    bbox={"boxstyle": "round,pad=0.3", "fc": red_color, "alpha": 0.85, "ec": "none"},
                    arrowprops={"arrowstyle": "->", "color": red_color, "lw": 1.2},
                )

        ax_eq.set_ylabel("净值", color=text_dim_color, fontsize=10)
        ax_eq.tick_params(colors=text_dim_color, labelsize=9, labelbottom=False)
        ax_eq.grid(True, color=border_color, linestyle="--", alpha=0.4)
        for spine in ax_eq.spines.values():
            spine.set_color(border_color)
        leg = ax_eq.legend(loc="best", framealpha=0.9, facecolor=bg_card_color,
                           edgecolor=border_color, fontsize=9, labelcolor=text_color)
        leg.get_frame().set_linewidth(0.5)

        # 回撤
        dd_values = self._strategy_drawdown_values(equity)
        ax_dd.fill_between(dates, dd_values, 0.0, color=red_color, alpha=0.18)
        ax_dd.plot(dates, dd_values, color=red_color, linewidth=1.4)
        ax_dd.axhline(0.0, color=border_color, linewidth=1.0)
        ax_dd.set_ylabel("回撤 (%)", color=text_dim_color, fontsize=10)
        ax_dd.tick_params(colors=text_dim_color, labelsize=9, labelbottom=True)
        ax_dd.grid(True, color=border_color, linestyle="--", alpha=0.35)
        for spine in ax_dd.spines.values():
            spine.set_color(border_color)
        ax_dd.set_xlabel("日期", color=text_dim_color, fontsize=10)
        for lbl in ax_dd.get_xticklabels():
            lbl.set_rotation(18)
            lbl.set_horizontalalignment("right")

        fig.tight_layout()

        canvas = FigureCanvasTkAgg(fig, master=self.strategy_bt_chart_frame)
        canvas.draw()
        canvas.get_tk_widget().grid(row=0, column=0, sticky="nsew", padx=12, pady=12)
        self._strategy_bt_figure = fig
        self._strategy_bt_canvas = canvas

    @staticmethod
    def _strategy_drawdown_values(equity_values):
        peak = -np.inf
        out = []
        for value in equity_values:
            peak = max(peak, value)
            out.append((value / peak - 1.0) * 100.0 if peak > 0 else 0.0)
        return out

    @staticmethod
    def _strategy_period_label(period):
        return f"{period.get('start_date')} → {period.get('end_date')}"

    def _strategy_detail_period_options(self, periods):
        labels = [self._strategy_period_label(period) for period in periods]
        return ["最近一期", "全部", *labels]

    def _strategy_detail_periods(self, periods):
        if not periods:
            return []
        period_var = getattr(self, "v_st_detail_period", None)
        selected = period_var.get() if period_var is not None else "最近一期"
        if selected == "全部":
            return list(periods)
        if selected == "最近一期":
            return [periods[-1]]
        return [
            period for period in periods
            if self._strategy_period_label(period) == selected
        ] or [periods[-1]]

    @staticmethod
    def _strategy_funnel_text(periods, label):
        if not periods:
            return "无调仓数据"
        totals = {
            "eligible_count": sum(int(p.get("eligible_count") or 0) for p in periods),
            "priced_count": sum(int(p.get("priced_count") or 0) for p in periods),
            "candidate_count": sum(int(p.get("candidate_count") or 0) for p in periods),
            "selected_count": sum(int(p.get("selected_count") or 0) for p in periods),
        }
        prefix = label
        if len(periods) == 1:
            prefix = f"{periods[0].get('start_date')}"
        elif label == "全部":
            prefix = f"全部 {len(periods)} 期"
        return (
            f"{prefix}: 合格 {totals['eligible_count']} → "
            f"定价 {totals['priced_count']} → "
            f"候选 {totals['candidate_count']} → "
            f"买入 {totals['selected_count']}"
        )

    def _render_strategy_selection_panel(self, result):
        frame = getattr(self, "strategy_bt_selection_frame", None)
        if frame is None:
            return
        self._clear_strategy_panel(frame)
        periods = result.get("periods") or []
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(0, weight=0)
        frame.grid_rowconfigure(1, weight=0)
        frame.grid_rowconfigure(2, weight=0)

        period_var = getattr(self, "v_st_detail_period", None)
        status_var = getattr(self, "v_st_detail_status", None)
        period_options = self._strategy_detail_period_options(periods)
        if period_var is not None and period_var.get() not in period_options:
            period_var.set("最近一期")
        status_options = ["全部", "买入", "候选", "剔除"]
        if status_var is not None and status_var.get() not in status_options:
            status_var.set("全部")

        filter_bar = ctk.CTkFrame(frame, fg_color="transparent")
        filter_bar.grid(row=0, column=0, sticky="ew", padx=12, pady=(6, 2))
        filter_bar.grid_columnconfigure(4, weight=1)
        ctk.CTkLabel(filter_bar, text="调仓期", text_color=TEXT_DIM,
                     font=(FONT_FAMILY, 11, "bold")).grid(row=0, column=0, sticky="w")
        ctk.CTkOptionMenu(
            filter_bar, variable=period_var, values=period_options,
            command=self._on_strategy_detail_filter_change,
            width=190, height=26, font=(FONT_FAMILY, 11),
            fg_color=BG_INPUT, button_color=BORDER,
            text_color=TEXT, dropdown_fg_color=BG_INPUT,
            dropdown_text_color=TEXT,
        ).grid(row=0, column=1, sticky="w", padx=(6, 16))
        ctk.CTkLabel(filter_bar, text="筛选状态", text_color=TEXT_DIM,
                     font=(FONT_FAMILY, 11, "bold")).grid(row=0, column=2, sticky="w")
        ctk.CTkOptionMenu(
            filter_bar, variable=status_var, values=status_options,
            command=self._on_strategy_detail_filter_change,
            width=92, height=26, font=(FONT_FAMILY, 11),
            fg_color=BG_INPUT, button_color=BORDER,
            text_color=TEXT, dropdown_fg_color=BG_INPUT,
            dropdown_text_color=TEXT,
        ).grid(row=0, column=3, sticky="w", padx=(6, 0))

        selected_periods = self._strategy_detail_periods(periods)
        period_label = period_var.get() if period_var is not None else "最近一期"
        status_filter = status_var.get() if status_var is not None else "全部"
        funnel_text = self._strategy_funnel_text(selected_periods, period_label)
        self._strategy_section_title(frame, f"筛选漏斗 · {funnel_text}", 1, 0)

        candidate_rows = []
        rejection_rows = []
        for period in selected_periods:
            period_label = self._strategy_period_label(period)
            for row in period.get("candidate_rows") or []:
                row_status = "买入" if row.get("selected") else "候选"
                if status_filter != "全部" and row_status != status_filter:
                    continue
                candidate_rows.append([
                    period_label,
                    row_status,
                    row.get("rank", ""),
                    row.get("bond_code", ""),
                    row.get("bond_name", ""),
                    f"{float(row.get('score')):.1f}" if row.get("score") is not None else "—",
                    self._fmt_strategy_price(row.get("market_price")),
                    self._fmt_strategy_pct(row.get("deviation"), sign=True),
                    self._fmt_strategy_pct(row.get("conversion_premium"), sign=True),
                    row.get("confidence", ""),
                    row.get("selection_reason", ""),
                ])
            for row in period.get("rejection_rows") or []:
                if status_filter not in ("全部", "剔除"):
                    continue
                source = row.get("source") or "剔除"
                reason = row.get("reason") or ""
                reason_text = f"{source}: {reason}" if reason else source
                rejection_rows.append([
                    period_label,
                    "剔除",
                    "",
                    row.get("bond_code", ""),
                    row.get("bond_name", ""),
                    f"{float(row.get('score')):.1f}" if row.get("score") is not None else "—",
                    self._fmt_strategy_price(row.get("market_price")),
                    self._fmt_strategy_pct(row.get("deviation"), sign=True),
                    self._fmt_strategy_pct(row.get("conversion_premium"), sign=True),
                    row.get("confidence", ""),
                    " / ".join(
                        text for text in (
                            reason_text,
                            " / ".join(str(tag) for tag in row.get("risk_tags") or []),
                        ) if text
                    ),
                ])

        # 候选 + 剔除合并为一张表
        all_rows = candidate_rows + rejection_rows
        self._render_strategy_small_tree(
            frame, 2, 0,
            ["period", "status", "rank", "code", "name", "score", "price",
             "dev", "premium", "confidence", "reason"],
            ["区间", "状态", "排名", "代码", "名称", "分数", "价格",
             "偏差", "溢价", "置信", "解释/原因"],
            [150, 58, 44, 88, 88, 56, 64, 68, 68, 52, 340],
            all_rows,
            xscroll=True,
            max_height=STRATEGY_DETAIL_TABLE_HEIGHT,
        )

    def _render_strategy_table(self, result):
        for child in self.strategy_bt_table_frame.winfo_children():
            child.destroy()

        all_periods = result.get("periods") or []
        periods = self._strategy_detail_periods(all_periods)
        if not periods:
            ctk.CTkLabel(
                self.strategy_bt_table_frame,
                text="无持仓明细",
                font=(FONT_FAMILY, 13),
                text_color=TEXT_DIM,
            ).grid(row=0, column=0, padx=12, pady=12, sticky="w")
            return

        self.strategy_bt_table_frame.grid_columnconfigure(0, weight=1)
        self.strategy_bt_table_frame.grid_rowconfigure(0, weight=0)
        self.strategy_bt_table_frame.grid_rowconfigure(1, weight=0)
        self.strategy_bt_table_frame.grid_rowconfigure(2, weight=0)
        self.strategy_bt_table_frame.grid_rowconfigure(3, weight=0)

        self._strategy_section_title(self.strategy_bt_table_frame, "换仓记录", 0, 0)
        summary_rows = []
        name_map: dict[str, str] = {}
        for period in all_periods:
            for pos in period.get("positions") or []:
                code = pos.get("bond_code")
                if code:
                    name_map[str(code)] = pos.get("bond_name") or str(code)
        previous_by_period: dict[str, set[str]] = {}
        previous: set[str] = set()
        for period in all_periods:
            period_label = self._strategy_period_label(period)
            previous_by_period[period_label] = set(previous)
            selected = {str(code) for code in period.get("selected_codes") or []}
            previous = selected
        for period in periods:
            period_label = self._strategy_period_label(period)
            previous = previous_by_period.get(period_label, set())
            selected = {str(code) for code in period.get("selected_codes") or []}
            buys = selected - previous
            sells = previous - selected
            holds = selected & previous
            benchmark_return = period.get("benchmark_return")
            period_return = period.get("period_return")
            excess = (
                float(period_return) - float(benchmark_return)
                if period_return is not None and benchmark_return is not None else None
            )
            buy_names = ", ".join(sorted(name_map.get(c, c)[:4] for c in buys)) or "—"
            sell_names = ", ".join(sorted(name_map.get(c, c)[:4] for c in sells)) or "—"
            summary_rows.append([
                period_label,
                self._fmt_strategy_pct(period_return, sign=True),
                self._fmt_strategy_pct(excess, sign=True),
                period.get("selected_count", 0),
                f"{len(buys)}",
                f"{len(sells)}",
                len(holds),
                self._fmt_strategy_pct(period.get("turnover")),
                self._fmt_strategy_pct(period.get("cash_weight")),
                buy_names,
                sell_names,
            ])
        self._render_strategy_small_tree(
            self.strategy_bt_table_frame, 1, 0,
            ["period", "return", "excess", "selected", "buy", "sell", "hold",
             "turnover", "cash", "buy_names", "sell_names"],
            ["区间", "收益(%)", "超额(%)", "持有", "新买", "卖出", "不动",
             "换手", "现金", "买入标的", "卖出标的"],
            [170, 78, 78, 52, 48, 48, 48, 68, 68, 180, 180],
            summary_rows,
            xscroll=True,
            max_height=STRATEGY_DETAIL_TABLE_HEIGHT,
        )

        detail_rows = []
        for period in periods:
            period_label = f"{period.get('start_date')} → {period.get('end_date')}"
            for pos in period.get("positions") or []:
                detail_rows.append([
                    period_label,
                    "成交",
                    pos.get("rank", ""),
                    pos.get("bond_code", ""),
                    pos.get("bond_name", ""),
                    self._fmt_strategy_pct(pos.get("return_contribution"), sign=True),
                    self._fmt_strategy_pct(pos.get("period_return"), sign=True),
                    f"{float(pos.get('score')):.1f}" if pos.get("score") is not None else "—",
                    pos.get("confidence", ""),
                    f"{pos.get('entry_date', '—')} @ {self._fmt_strategy_price(pos.get('start_price'))}",
                    f"{pos.get('exit_date', '—')} @ {self._fmt_strategy_price(pos.get('end_price'))}",
                    " / ".join(str(tag) for tag in pos.get("risk_tags") or []),
                ])
            for pos in period.get("skipped_positions") or []:
                detail_rows.append([
                    period_label,
                    "跳过",
                    "",
                    pos.get("bond_code", ""),
                    pos.get("bond_name", ""),
                    "—",
                    "—",
                    "—",
                    "",
                    f"{pos.get('entry_date', '—')} @ {self._fmt_strategy_price(pos.get('start_price'))}",
                    f"{pos.get('exit_date', '—')} @ {self._fmt_strategy_price(pos.get('end_price'))}",
                    pos.get("reason", ""),
                ])

        self._strategy_section_title(self.strategy_bt_table_frame, "买卖明细", 2, 0)
        tree = self._render_strategy_small_tree(
            self.strategy_bt_table_frame, 3, 0,
            ["period", "status", "rank", "code", "name", "contrib", "ret",
             "score", "confidence", "entry", "exit", "note"],
            ["区间", "状态", "排名", "代码", "名称", "贡献(%)", "收益(%)",
             "分数", "置信", "买入", "卖出", "标签/原因"],
            [170, 56, 52, 88, 96, 76, 76, 62, 58, 122, 122, 260],
            detail_rows,
            xscroll=True,
            max_height=STRATEGY_DETAIL_TABLE_HEIGHT,
        )
        self._strategy_bt_tree = tree
        _TREE_ATTRS.add("_strategy_bt_tree")

    def _clear_strategy_panel(self, frame):
        for child in frame.winfo_children():
            child.destroy()
        for i in range(8):
            frame.grid_rowconfigure(i, weight=0)
            frame.grid_columnconfigure(i, weight=0)

    def _strategy_metric_tile(self, parent, col, title, value):
        cell = ctk.CTkFrame(parent, fg_color="transparent")
        cell.grid(row=0, column=col, sticky="ew", padx=8, pady=6)
        ctk.CTkLabel(cell, text=title, text_color=TEXT_DIM,
                     font=(FONT_FAMILY, 11)).pack(anchor="w")
        ctk.CTkLabel(cell, text=str(value), text_color=TEXT,
                     font=(FONT_FAMILY, 16, "bold")).pack(anchor="w")

    def _strategy_section_title(self, parent, text, row, col, columnspan=1):
        ctk.CTkLabel(parent, text=text, text_color=TEXT,
                     font=(FONT_FAMILY, 14, "bold")).grid(
                         row=row, column=col, columnspan=columnspan,
                         sticky="w", padx=12, pady=(10, 4))

    def _render_strategy_small_tree(
        self, parent, row, col, columns, headers, widths, values, *,
        columnspan=1,
        xscroll=False,
        yscroll=True,
        max_height=None,
        stretch_weights=None,
        selectmode="browse",
    ):
        _configure_tree_style()
        container = ctk.CTkFrame(parent, fg_color="transparent")
        container.grid(row=row, column=col, columnspan=columnspan,
                       sticky="nsew", padx=8, pady=(0, 8))
        container.grid_columnconfigure(0, weight=1)
        container.grid_rowconfigure(0, weight=1)
        tree_kwargs = {}
        if max_height is not None:
            tree_kwargs["height"] = max_height
        tree = ttk.Treeview(container, columns=columns, show="headings",
                            selectmode=selectmode, **tree_kwargs)
        if yscroll:
            y_scroll = ctk.CTkScrollbar(
                container, orientation="vertical", command=tree.yview,
                width=14, fg_color=BG_INPUT, button_color=ACCENT,
                button_hover_color=TEXT,
            )
        else:
            y_scroll = None
        if xscroll:
            x_scroll = ctk.CTkScrollbar(
                container, orientation="horizontal", command=tree.xview,
                height=12, fg_color=BG_INPUT, button_color=ACCENT,
                button_hover_color=TEXT,
            )
            if y_scroll is not None:
                tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
            else:
                tree.configure(xscrollcommand=x_scroll.set)
        else:
            x_scroll = None
            if y_scroll is not None:
                tree.configure(yscrollcommand=y_scroll.set)
        tree.grid(row=0, column=0, sticky="nsew")
        if y_scroll is not None:
            y_scroll.grid(row=0, column=1, sticky="ns")
        if x_scroll is not None:
            x_scroll.grid(row=1, column=0, sticky="ew")
        _configure_responsive_columns(tree, columns, headers, widths, stretch_weights)
        _attach_column_sort(tree, columns, headers)
        self._style_strategy_tree_rows(tree)
        for idx, vals in enumerate(values):
            tree.insert("", "end", iid=str(idx), values=vals,
                        tags=(self._strategy_tree_row_tag(idx),))
        if not values:
            tree.insert("", "end", values=["—"] + [""] * (len(columns) - 1))
        return tree

    @staticmethod
    def _strategy_tree_row_tag(index: int) -> str:
        return "strategy_even" if index % 2 == 0 else "strategy_odd"

    @staticmethod
    def _style_strategy_tree_rows(tree) -> None:
        tree.tag_configure(
            "strategy_even", background=get_color(BG_CARD), foreground=get_color(TEXT))
        tree.tag_configure(
            "strategy_odd", background=get_color(BG_INPUT), foreground=get_color(TEXT))

    @staticmethod
    def _fmt_strategy_pct(value, sign=False):
        if value is None:
            return "—"
        try:
            f = float(value)
        except (TypeError, ValueError):
            return "—"
        if not np.isfinite(f):
            return "—"
        return f"{f*100:+.2f}%" if sign else f"{f*100:.2f}%"

    @staticmethod
    def _fmt_strategy_price(value):
        if value is None:
            return "—"
        try:
            f = float(value)
        except (TypeError, ValueError):
            return "—"
        return f"{f:.2f}" if np.isfinite(f) else "—"
