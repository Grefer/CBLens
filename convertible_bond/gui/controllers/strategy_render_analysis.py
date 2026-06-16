"""策略回测 — 分析子页渲染 (归因/风险/稳健性/数据质量).

从 strategy_backtest.py 按职责拆出; 经 StrategyBacktestMixin 聚合混入
CBPricerApp, 方法间通过 self.* 跨 mixin 调用不受拆分影响。
"""
from __future__ import annotations

import customtkinter as ctk
import numpy as np
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.colors import LinearSegmentedColormap

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
    FONT_MONO,
    get_color,
)

from .strategy_common import (
    STRATEGY_COMPACT_TABLE_HEIGHT,
    STRATEGY_DATA_TABLE_HEIGHT,
    STRATEGY_DETAIL_TABLE_HEIGHT,
    STRATEGY_RISK_CHART_HEIGHT,
    STRATEGY_SECONDARY_CHART_HEIGHT,
)


class StrategyAnalysisRenderMixin:
    """策略回测 — 分析子页渲染 (归因/风险/稳健性/数据质量)."""

    def _render_strategy_attribution(self, result):
        frame = self.strategy_bt_attribution_frame
        self._clear_strategy_panel(frame)
        diagnostics = result.get("diagnostics") or {}
        attribution = diagnostics.get("attribution") or {}
        summary = result.get("summary") or {}

        frame.grid_columnconfigure(0, weight=1, uniform="strategy_attr_tables")
        frame.grid_columnconfigure(1, weight=1, uniform="strategy_attr_tables")
        frame.grid_rowconfigure(2, minsize=170)
        frame.grid_rowconfigure(4, weight=1, minsize=360)

        metrics = ctk.CTkFrame(frame, fg_color="transparent")
        metrics.grid(row=0, column=0, columnspan=2, sticky="ew", padx=10, pady=(8, 4))
        for i in range(4):
            metrics.grid_columnconfigure(i, weight=1)
        self._strategy_metric_tile(metrics, 0, "交易成本", self._fmt_strategy_pct(attribution.get("cost_drag"), sign=True))
        self._strategy_metric_tile(metrics, 1, "平均现金", self._fmt_strategy_pct(summary.get("avg_cash_weight")))
        self._strategy_metric_tile(metrics, 2, "未成交笔数", str(attribution.get("skipped_positions") or 0))
        self._strategy_metric_tile(metrics, 3, "总交易费", self._fmt_strategy_pct(summary.get("total_cost")))

        self._strategy_section_title(frame, "贡献最大", 1, 0)
        self._strategy_section_title(frame, "拖累最大", 1, 1)
        top_contribs = attribution.get("top_contributors") or []
        top_detractors = attribution.get("top_detractors") or []
        self._render_strategy_small_tree(
            frame, 2, 0,
            ["code", "name", "contrib", "holds"],
            ["代码", "名称", "贡献(%)", "期数"],
            [92, 110, 80, 54],
            [
                [
                    row.get("bond_code", ""),
                    row.get("bond_name", ""),
                    self._fmt_strategy_pct(row.get("contribution"), sign=True),
                    row.get("holding_periods", ""),
                ]
                for row in top_contribs
            ],
            max_height=STRATEGY_COMPACT_TABLE_HEIGHT,
            stretch_weights={"名称": 1.7, "贡献(%)": 0.7, "期数": 0.4},
        )
        self._render_strategy_small_tree(
            frame, 2, 1,
            ["code", "name", "contrib", "holds"],
            ["代码", "名称", "贡献(%)", "期数"],
            [92, 110, 80, 54],
            [
                [
                    row.get("bond_code", ""),
                    row.get("bond_name", ""),
                    self._fmt_strategy_pct(row.get("contribution"), sign=True),
                    row.get("holding_periods", ""),
                ]
                for row in top_detractors
            ],
            max_height=STRATEGY_COMPACT_TABLE_HEIGHT,
            stretch_weights={"名称": 1.7, "贡献(%)": 0.7, "期数": 0.4},
        )

        # 个券贡献瀑布图 + 月度/全年收益热力图 (年度收益已并入热力图右端「全年」列)
        self._strategy_section_title(frame, "个券贡献", 3, 0)
        self._strategy_section_title(frame, "月度 / 全年收益", 3, 1)
        self._render_attribution_charts(
            frame, 4,
            top_contribs, top_detractors,
            diagnostics.get("yearly_returns") or [],
            diagnostics.get("monthly_returns") or [],
        )

    def _render_attribution_charts(self, frame, row,
                                   top_contribs, top_detractors,
                                   yearly_returns, monthly_returns):
        """左: 个券贡献瀑布图(独占整列); 右: 月度收益热力图 + 全年列(紧凑置顶)."""
        bg_card_color = get_color(BG_CARD)
        bg_input_color = get_color(BG_INPUT)
        text_dim_color = get_color(TEXT_DIM)
        text_color = get_color(TEXT)
        border_color = get_color(BORDER)
        green_color = get_color(GREEN)
        red_color = get_color(RED)

        chart_shell = ctk.CTkFrame(frame, fg_color="transparent")
        chart_shell.grid(row=row, column=0, columnspan=2, sticky="nsew", padx=4, pady=(0, 8))
        chart_shell.grid_columnconfigure(0, weight=1, uniform="attr_chart")
        chart_shell.grid_columnconfigure(1, weight=1, uniform="attr_chart")
        chart_shell.grid_rowconfigure(0, weight=1)

        # ── 左列: 个券贡献瀑布图 (年度表已并入右侧「全年」列, 此处独占整列加宽) ──
        left = ctk.CTkFrame(chart_shell, fg_color="transparent")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8), pady=0)
        left.grid_columnconfigure(0, weight=1)
        left.grid_rowconfigure(0, weight=1)

        waterfall_items = (
            [(r.get("bond_name") or r.get("bond_code", "")[:6],
              float(r.get("contribution") or 0)) for r in top_contribs[:5]]
            + [(r.get("bond_name") or r.get("bond_code", "")[:6],
                float(r.get("contribution") or 0)) for r in top_detractors[:5]]
        )
        waterfall_items = [(n, v) for n, v in waterfall_items if abs(v) > 1e-8]
        waterfall_items.sort(key=lambda x: x[1], reverse=True)
        if waterfall_items:
            wf_frame = ctk.CTkFrame(left, fg_color="transparent")
            wf_frame.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)
            wf_frame.grid_columnconfigure(0, weight=1)
            wf_frame.grid_rowconfigure(0, weight=1)
            wf_h = max(2.8, 0.34 * len(waterfall_items) + 1.1)
            fig_wf = Figure(figsize=(7.2, wf_h), dpi=100, facecolor=bg_card_color)
            ax_wf = fig_wf.add_subplot(111, facecolor=bg_input_color)
            names = [n[:6] for n, _ in waterfall_items]
            vals = [v * 100 for _, v in waterfall_items]
            wf_colors = [green_color if v >= 0 else red_color for v in vals]
            ax_wf.barh(range(len(names)), vals, color=wf_colors, alpha=0.85, height=0.66)
            ax_wf.set_yticks(range(len(names)))
            ax_wf.set_yticklabels(names, fontsize=8, color=text_color)
            ax_wf.set_xlabel("贡献 (%)", color=text_dim_color, fontsize=9)
            ax_wf.axvline(0, color=border_color, linewidth=0.8)
            ax_wf.tick_params(colors=text_dim_color, labelsize=8)
            ax_wf.grid(True, axis="x", color=border_color, linestyle="--", alpha=0.3)
            for spine in ax_wf.spines.values():
                spine.set_color(border_color)
            ax_wf.invert_yaxis()
            # 数值标签直接标在条端, 省去对照刻度
            span = (max(vals) - min(vals)) or 1.0
            pad = 0.012 * span
            for yi, v in enumerate(vals):
                ax_wf.text(v + (pad if v >= 0 else -pad), yi, f"{v:+.1f}",
                           va="center", ha="left" if v >= 0 else "right",
                           fontsize=7.5, color=text_dim_color)
            ax_wf.margins(x=0.12)
            fig_wf.subplots_adjust(left=0.20, right=0.97, bottom=0.16, top=0.97)
            canvas_wf = FigureCanvasTkAgg(fig_wf, master=wf_frame)
            canvas_wf.draw()
            canvas_wf.get_tk_widget().grid(row=0, column=0, sticky="nsew")
            self._strategy_bt_waterfall_fig = fig_wf
        else:
            ctk.CTkLabel(
                left, text="暂无贡献数据", text_color=TEXT_DIM,
                font=(FONT_FAMILY, 12),
            ).grid(row=0, column=0, sticky="nsew", padx=12, pady=12)

        # ── 右列: 月度收益热力图 + 全年列 (紧凑置顶, 不随行高拉伸) ──
        right = ctk.CTkFrame(chart_shell, fg_color="transparent")
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 0), pady=0)
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(0, weight=0)   # 热力图按内容高度
        right.grid_rowconfigure(1, weight=1)   # 占位行吸收多余高度, 避免色块被纵向拉伸

        if not monthly_returns:
            ctk.CTkLabel(right, text="暂无月度数据", text_color=TEXT_DIM,
                         font=(FONT_FAMILY, 12)).grid(row=0, column=0, sticky="new",
                                                      padx=12, pady=12)
            return

        year_month_map: dict[int, dict[int, float]] = {}
        for mr in monthly_returns:
            period_str = mr.get("period", "")
            ret = mr.get("return")
            if ret is None or not period_str:
                continue
            try:
                parts = period_str.split("-")
                year, month = int(parts[0]), int(parts[1])
            except (ValueError, IndexError):
                continue
            year_month_map.setdefault(year, {})[month] = float(ret)

        if not year_month_map:
            ctk.CTkLabel(right, text="月度数据解析为空", text_color=TEXT_DIM,
                         font=(FONT_FAMILY, 12)).grid(row=0, column=0, sticky="new",
                                                      padx=12, pady=12)
            return

        years = sorted(year_month_map.keys())
        data = np.full((len(years), 12), np.nan)
        for yi, y in enumerate(years):
            for m, v in year_month_map[y].items():
                if 1 <= m <= 12:
                    data[yi, m - 1] = v * 100

        # 全年收益: 优先取 yearly_returns, 缺失年份用当年月度复利兜底
        annual_map: dict[int, float] = {}
        for row_d in yearly_returns or []:
            try:
                y = int(str(row_d.get("period", "")).split("-")[0])
            except (ValueError, IndexError):
                continue
            rv = row_d.get("return")
            if rv is None:
                continue
            try:
                annual_map[y] = float(rv) * 100
            except (TypeError, ValueError):
                continue
        annual = np.full((len(years), 1), np.nan)
        for yi, y in enumerate(years):
            if y in annual_map:
                annual[yi, 0] = annual_map[y]
            elif year_month_map[y]:
                comp = 1.0
                for v in year_month_map[y].values():
                    comp *= (1.0 + v)
                annual[yi, 0] = (comp - 1.0) * 100

        n_years = len(years)
        fig_h = 1.25 + 0.6 * n_years
        fig_hm = Figure(figsize=(6.6, fig_h), dpi=100, facecolor=bg_card_color)
        gs = fig_hm.add_gridspec(
            1, 2, width_ratios=[12, 1.5], wspace=0.08,
            left=0.085, right=0.9, bottom=0.42 / fig_h, top=1 - 0.26 / fig_h)
        ax_hm = fig_hm.add_subplot(gs[0, 0], facecolor=bg_input_color)
        ax_yr = fig_hm.add_subplot(gs[0, 1], facecolor=bg_input_color, sharey=ax_hm)
        cmap = LinearSegmentedColormap.from_list("rg", [red_color, bg_input_color, green_color])

        # 月度色块 (独立标尺, 不被全年大幅收益拉爆色阶)
        vmax = max(3.0, float(np.nanmax(np.abs(data)))) if np.any(np.isfinite(data)) else 5.0
        im = ax_hm.imshow(data, aspect="auto", cmap=cmap, vmin=-vmax, vmax=vmax,
                          interpolation="nearest")
        ax_hm.set_xticks(range(12))
        ax_hm.set_xticklabels([f"{m+1}月" for m in range(12)], fontsize=7,
                              color=text_dim_color)
        ax_hm.set_yticks(range(len(years)))
        ax_hm.set_yticklabels([str(y) for y in years], fontsize=8, color=text_color)
        ax_hm.tick_params(length=0)
        for spine in ax_hm.spines.values():
            spine.set_visible(False)
        for yi in range(len(years)):
            for mi in range(12):
                val = data[yi, mi]
                if np.isfinite(val):
                    ax_hm.text(mi, yi, f"{val:+.1f}", ha="center", va="center",
                               fontsize=6.5, color=text_color,
                               fontweight="bold" if abs(val) >= vmax * 0.5 else "normal")

        # 全年色块 (独立标尺 + 边框分隔, 视觉上与月度区分)
        a_vmax = (max(5.0, float(np.nanmax(np.abs(annual))))
                  if np.any(np.isfinite(annual)) else 10.0)
        ax_yr.imshow(annual, aspect="auto", cmap=cmap, vmin=-a_vmax, vmax=a_vmax,
                     interpolation="nearest")
        ax_yr.set_xticks([0])
        ax_yr.set_xticklabels(["全年"], fontsize=7.5, color=text_color)
        ax_yr.tick_params(length=0, labelleft=False)
        for spine in ax_yr.spines.values():
            spine.set_visible(True)
            spine.set_color(border_color)
            spine.set_linewidth(0.8)
        for yi in range(len(years)):
            val = annual[yi, 0]
            if np.isfinite(val):
                ax_yr.text(0, yi, f"{val:+.1f}", ha="center", va="center",
                           fontsize=8, color=text_color, fontweight="bold")

        cb = fig_hm.colorbar(im, ax=[ax_hm, ax_yr], fraction=0.045, pad=0.04)
        cb.ax.tick_params(colors=text_dim_color, labelsize=7)
        cb.set_label("月度 %", color=text_dim_color, fontsize=8)
        canvas_hm = FigureCanvasTkAgg(fig_hm, master=right)
        canvas_hm.draw()
        canvas_hm.get_tk_widget().grid(row=0, column=0, sticky="new")
        self._strategy_bt_heatmap_fig = fig_hm

    def _render_strategy_risk_panel(self, result):
        """风险 tab: 合并原风险 + 稳健性 + 数据可信度."""
        frame = self.strategy_bt_risk_frame
        self._clear_strategy_panel(frame)
        diagnostics = result.get("diagnostics") or {}
        summary = result.get("summary") or {}
        attribution = diagnostics.get("attribution") or {}
        data_quality = diagnostics.get("data_quality") or {}
        warnings = diagnostics.get("warnings") or []
        periods = result.get("periods") or []

        frame.grid_columnconfigure(0, weight=1)
        frame.grid_columnconfigure(1, weight=1)
        frame.grid_rowconfigure(2, minsize=240)
        frame.grid_rowconfigure(4, minsize=300)

        # ── Row 0: 稳健性指标条 ──────────────────────────────────
        returns = [
            float(p.get("period_return"))
            for p in periods
            if p.get("period_return") is not None and np.isfinite(p.get("period_return"))
        ]
        win_rate = (sum(1 for r in returns if r > 0) / len(returns)) if returns else None
        worst = min(returns) if returns else None
        best = max(returns) if returns else None
        ret_std = float(np.std(returns, ddof=1)) if len(returns) > 1 else None
        fallback_ratio = float(data_quality.get("current_fallback_ratio") or 0.0)
        positive_contrib = [
            float(row.get("contribution"))
            for row in attribution.get("top_contributors") or []
            if row.get("contribution") is not None and float(row.get("contribution")) > 0
        ]
        top3_contrib = sum(positive_contrib[:3])
        total_positive = sum(positive_contrib)
        concentration = top3_contrib / total_positive if total_positive > 0 else None

        # 可信度 badge
        if fallback_ratio <= 0:
            q_text, q_color = "高", get_color(GREEN)
        elif fallback_ratio <= 0.2:
            q_text, q_color = "中", get_color(ORANGE)
        else:
            q_text, q_color = "低", get_color(RED)

        metrics = ctk.CTkFrame(frame, fg_color="transparent")
        metrics.grid(row=0, column=0, columnspan=2, sticky="ew", padx=10, pady=(8, 4))
        for i in range(7):
            metrics.grid_columnconfigure(i, weight=1)
        self._strategy_metric_tile(metrics, 0, "区间胜率", self._fmt_strategy_pct(win_rate))
        self._strategy_metric_tile(metrics, 1, "最大回撤", self._fmt_strategy_pct(summary.get("max_drawdown")))
        self._strategy_metric_tile(metrics, 2, "年化波动", self._fmt_strategy_pct(summary.get("annualized_volatility")))
        self._strategy_metric_tile(metrics, 3, "最好单期", self._fmt_strategy_pct(best, sign=True))
        self._strategy_metric_tile(metrics, 4, "最差单期", self._fmt_strategy_pct(worst, sign=True))
        self._strategy_metric_tile(metrics, 5, "前三集中度", self._fmt_strategy_pct(concentration))
        self._strategy_metric_tile(metrics, 6, f"可信度: {q_text}", self._fmt_strategy_pct(fallback_ratio))

        # ── Row 1: 左=风险提示+回撤画像, 右=稳健性建议 ──────────────
        left = ctk.CTkFrame(frame, fg_color="transparent")
        left.grid(row=1, column=0, sticky="nsew", padx=12, pady=(6, 4))

        ctk.CTkLabel(left, text="风险提醒", text_color=TEXT,
                     font=(FONT_FAMILY, 13, "bold")).pack(anchor="w")
        if warnings:
            critical_keywords = ("大幅", "异常", "失败", "不足", "极端")
            for warning in warnings:
                is_critical = any(kw in warning for kw in critical_keywords)
                color = RED if is_critical else ORANGE
                prefix = "🔴" if is_critical else "🟡"
                ctk.CTkLabel(
                    left, text=f"{prefix} {warning}", text_color=color,
                    font=(FONT_FAMILY, 11), justify="left", wraplength=460,
                ).pack(anchor="w", pady=(3, 0))
        else:
            ctk.CTkLabel(left, text="🟢 暂无明显风险提示", text_color=TEXT_DIM,
                         font=(FONT_FAMILY, 11)).pack(anchor="w", pady=(3, 0))

        dd_items = [
            ("回撤区间", f"{summary.get('max_drawdown_start') or '—'} → {summary.get('max_drawdown_end') or '—'}"),
            ("持续天数", f"{summary.get('max_drawdown_days') or 0} 天 (最长 {summary.get('longest_drawdown_days') or 0} 天)"),
        ]
        for label, value in dd_items:
            row_w = ctk.CTkFrame(left, fg_color="transparent")
            row_w.pack(fill="x", pady=(3, 0))
            ctk.CTkLabel(row_w, text=label, text_color=TEXT_DIM,
                         font=(FONT_FAMILY, 11), width=70, anchor="w").pack(side="left")
            ctk.CTkLabel(row_w, text=str(value), text_color=TEXT,
                         font=(FONT_MONO, 11), anchor="w").pack(side="left")

        right = ctk.CTkFrame(frame, fg_color="transparent")
        right.grid(row=1, column=1, sticky="nsew", padx=12, pady=(6, 4))

        notes = self._strategy_robustness_notes(
            summary=summary, win_rate=win_rate, worst=worst,
            concentration=concentration, fallback_ratio=fallback_ratio,
        )
        suggestions = self._strategy_dynamic_suggestions(
            summary=summary, win_rate=win_rate, worst=worst,
            concentration=concentration, ret_std=ret_std,
        )
        ctk.CTkLabel(right, text="改进建议", text_color=TEXT,
                     font=(FONT_FAMILY, 13, "bold")).pack(anchor="w")
        for note in notes:
            ctk.CTkLabel(right, text=f"• {note}", text_color=TEXT_DIM,
                         font=(FONT_FAMILY, 11), justify="left",
                         wraplength=460).pack(anchor="w", pady=(3, 0))
        for text in suggestions[:3]:
            ctk.CTkLabel(right, text=f"→ {text}", text_color=ACCENT,
                         font=(FONT_FAMILY, 11), justify="left",
                         wraplength=460).pack(anchor="w", pady=(3, 0))

        # ── Row 2: 滚动风险图 ──────────────────────────────────
        self._render_rolling_risk_chart(frame, 2, periods, result.get("equity_curve") or [])

        # ── Row 3: 收益分布 + 最差区间复盘 ──────────────────────
        self._strategy_section_title(frame, "收益分布 / 最差区间", 3, 0, columnspan=2)
        dist_and_worst = ctk.CTkFrame(
            frame, fg_color="transparent", height=STRATEGY_SECONDARY_CHART_HEIGHT)
        dist_and_worst.grid(row=4, column=0, columnspan=2, sticky="ew", padx=4, pady=(0, 8))
        dist_and_worst.grid_columnconfigure(0, weight=4, minsize=520)
        dist_and_worst.grid_columnconfigure(1, weight=5, minsize=620)
        dist_and_worst.grid_rowconfigure(0, weight=1)
        dist_and_worst.grid_propagate(False)

        if returns:
            dist_frame = ctk.CTkFrame(dist_and_worst, fg_color="transparent")
            dist_frame.grid(row=0, column=0, sticky="nsew", padx=(4, 10), pady=4)
            dist_frame.grid_columnconfigure(0, weight=1)
            dist_frame.grid_rowconfigure(0, weight=1)
            bg_card_c = get_color(BG_CARD)
            bg_input_c = get_color(BG_INPUT)
            text_dim_c = get_color(TEXT_DIM)
            border_c = get_color(BORDER)
            green_c = get_color(GREEN)
            red_c = get_color(RED)

            fig_dist = Figure(figsize=(5.2, 2.8), dpi=100, facecolor=bg_card_c)
            ax_dist = fig_dist.add_subplot(111, facecolor=bg_input_c)
            ret_pct = [r * 100 for r in returns]
            n_bins = min(20, max(5, len(ret_pct) // 3))
            n, bins, patches = ax_dist.hist(ret_pct, bins=n_bins, alpha=0.75, edgecolor=border_c)
            for patch, left_edge in zip(patches, bins):
                patch.set_facecolor(green_c if left_edge >= 0 else red_c)
            ax_dist.axvline(0, color=border_c, linewidth=1.0, linestyle="--")
            median_r = float(np.median(ret_pct))
            ax_dist.axvline(median_r, color=get_color(ACCENT), linewidth=1.2,
                            linestyle=":", label=f"中位数 {median_r:.1f}%")
            ax_dist.set_xlabel("区间收益 (%)", color=text_dim_c, fontsize=9)
            ax_dist.set_ylabel("频次", color=text_dim_c, fontsize=9)
            ax_dist.tick_params(colors=text_dim_c, labelsize=8)
            ax_dist.grid(True, axis="y", color=border_c, linestyle="--", alpha=0.3)
            for spine in ax_dist.spines.values():
                spine.set_color(border_c)
            leg_dist = ax_dist.legend(loc="best", framealpha=0.9, facecolor=bg_card_c,
                                      edgecolor=border_c, fontsize=8,
                                      labelcolor=get_color(TEXT))
            leg_dist.get_frame().set_linewidth(0.5)
            fig_dist.tight_layout()
            canvas_dist = FigureCanvasTkAgg(fig_dist, master=dist_frame)
            canvas_dist.draw()
            canvas_dist.get_tk_widget().grid(row=0, column=0, sticky="nsew")
            self._strategy_bt_dist_fig = fig_dist

        worst_rows = []
        for period in sorted(periods, key=lambda p: float(p.get("period_return") or 0.0))[:8]:
            period_return = period.get("period_return")
            benchmark = period.get("benchmark_return")
            excess = (
                float(period_return) - float(benchmark)
                if period_return is not None and benchmark is not None else None
            )
            worst_rows.append([
                f"{period.get('start_date')} → {period.get('end_date')}",
                self._fmt_strategy_pct(period_return, sign=True),
                self._fmt_strategy_pct(excess, sign=True),
                self._fmt_strategy_pct(period.get("turnover")),
                self._strategy_codes_text(period.get("selected_codes") or []),
            ])
        self._render_strategy_small_tree(
            dist_and_worst, 0, 1,
            ["period", "ret", "excess", "turnover", "codes"],
            ["区间", "收益", "超额", "换手", "持仓"],
            [138, 66, 66, 60, 760],
            worst_rows,
            xscroll=True,
            max_height=STRATEGY_DETAIL_TABLE_HEIGHT,
            stretch_weights={"持仓": 6.0, "区间": 1.2, "收益": 0.3, "超额": 0.3, "换手": 0.3},
        )

    @staticmethod
    def _daily_curve_returns(equity_curve):
        """从日频净值曲线提取 (日期, 日收益) 序列.

        与总览净值曲线、核心 ``daily_mtm`` 波动率口径同源。逐日 MTM 步长均匀,
        天然不受回测末尾"残桩区间"(不足整周期的几天)影响, 故按时间口径的滚动
        风险走势基于此, 而非等权的调仓区间收益。
        """
        dates, eqs = [], []
        for row in equity_curve or []:
            d = row.get("date")
            e = row.get("equity")
            if d is None or e is None:
                continue
            try:
                e = float(e)
            except (TypeError, ValueError):
                continue
            if not np.isfinite(e) or e <= 0:
                continue
            dates.append(d)
            eqs.append(e)
        r_dates, rets = [], []
        for i in range(1, len(eqs)):
            if eqs[i - 1] > 0:
                r_dates.append(dates[i])
                rets.append(eqs[i] / eqs[i - 1] - 1.0)
        return r_dates, rets

    def _render_rolling_risk_chart(self, frame, grid_row, periods, equity_curve):
        """滚动年化波动率 + 滚动 Sharpe 走势.

        基于日频净值曲线 (与总览净值图、年化波动/Sharpe 指标同口径), 而非等权的
        调仓区间收益 —— 这样步长均匀, 不会因末尾不足整周期的残桩区间在末端跳变。
        """
        dates, dret = self._daily_curve_returns(equity_curve)
        # 滚动窗口按"月"定义: 默认近 3 个月 (≈63 个交易日); 样本不足时按月收窄
        days_per_month = 21
        months = 3
        window = months * days_per_month
        while months > 1 and len(dret) < window + 1:
            months -= 1
            window = months * days_per_month
        if len(dret) < max(window + 1, 25):
            ctk.CTkLabel(frame, text="日频样本不足, 无法计算滚动风险",
                         text_color=TEXT_DIM, font=(FONT_FAMILY, 12)).grid(
                             row=grid_row, column=0, columnspan=2, sticky="w", padx=12, pady=6)
            return

        bg_card_color = get_color(BG_CARD)
        bg_input_color = get_color(BG_INPUT)
        text_dim_color = get_color(TEXT_DIM)
        border_color = get_color(BORDER)
        accent_color = get_color(ACCENT)
        orange_color = get_color(ORANGE)

        arr = np.asarray(dret, dtype=float)
        ann = float(np.sqrt(252.0))
        roll_dates, roll_vol, roll_sharpe = [], [], []
        for i in range(window - 1, len(arr)):
            chunk = arr[i - window + 1: i + 1]
            sd = float(np.std(chunk, ddof=1))
            roll_vol.append(sd * ann * 100.0)
            roll_sharpe.append(float(np.mean(chunk)) / sd * ann if sd > 1e-12 else 0.0)
            roll_dates.append(dates[i])

        chart_frame = ctk.CTkFrame(
            frame, fg_color="transparent", height=STRATEGY_RISK_CHART_HEIGHT)
        chart_frame.grid(row=grid_row, column=0, columnspan=2, sticky="nsew", padx=8, pady=(4, 8))
        chart_frame.grid_columnconfigure(0, weight=1)
        chart_frame.grid_rowconfigure(0, weight=1)
        chart_frame.grid_propagate(False)

        fig = Figure(figsize=(10, 2.2), dpi=100, facecolor=bg_card_color)
        ax1 = fig.add_subplot(121, facecolor=bg_input_color)
        ax2 = fig.add_subplot(122, facecolor=bg_input_color)

        ax1.plot(roll_dates, roll_vol, color=orange_color, linewidth=1.5)
        ax1.set_ylabel("年化波动率 (%)", color=text_dim_color, fontsize=9)
        ax1.set_title(f"滚动年化波动率 ({months} 个月)", color=text_dim_color, fontsize=9)
        ax1.tick_params(colors=text_dim_color, labelsize=7)
        ax1.grid(True, color=border_color, linestyle="--", alpha=0.3)
        for spine in ax1.spines.values():
            spine.set_color(border_color)
        for lbl in ax1.get_xticklabels():
            lbl.set_rotation(20)
            lbl.set_horizontalalignment("right")

        ax2.plot(roll_dates, roll_sharpe, color=accent_color, linewidth=1.5)
        ax2.axhline(0, color=border_color, linewidth=0.8)
        ax2.set_ylabel("Sharpe (年化)", color=text_dim_color, fontsize=9)
        ax2.set_title(f"滚动 Sharpe ({months} 个月)", color=text_dim_color, fontsize=9)
        ax2.tick_params(colors=text_dim_color, labelsize=7)
        ax2.grid(True, color=border_color, linestyle="--", alpha=0.3)
        finite_sharpe = [v for v in roll_sharpe if np.isfinite(v)]
        if finite_sharpe:
            s_min, s_max = min(finite_sharpe), max(finite_sharpe)
            pad = max(0.3, (s_max - s_min) * 0.15)
            ax2.set_ylim(s_min - pad, s_max + pad)
        for spine in ax2.spines.values():
            spine.set_color(border_color)
        for lbl in ax2.get_xticklabels():
            lbl.set_rotation(20)
            lbl.set_horizontalalignment("right")

        fig.tight_layout()
        canvas = FigureCanvasTkAgg(fig, master=chart_frame)
        canvas.draw()
        canvas.get_tk_widget().grid(row=0, column=0, sticky="nsew")
        self._strategy_bt_rolling_fig = fig

    @staticmethod
    def _strategy_robustness_notes(*, summary, win_rate, worst, concentration, fallback_ratio):
        notes = []
        if win_rate is not None:
            if win_rate >= 0.6:
                notes.append("多数调仓区间为正收益, 收益分布相对均衡")
            elif win_rate < 0.45:
                notes.append("区间胜率偏低, 需要确认是否靠少数大涨区间贡献")
        if concentration is not None and concentration >= 0.65:
            notes.append("前三贡献集中度较高, 需要检查是否依赖少数个券")
        if worst is not None and worst <= -0.08:
            notes.append("存在单期大幅亏损, 建议复核该期持仓和市场环境")
        if summary.get("avg_turnover") is not None and float(summary.get("avg_turnover")) >= 0.8:
            notes.append("平均换手较高, 成本和滑点敏感度需要重点复核")
        if fallback_ratio > 0.2:
            notes.append("当前条款回退比例较高, 历史口径可信度偏弱")
        if not notes:
            notes.append("未发现特别突出的单点脆弱性, 可继续用参数对比做复核")
        return notes

    @staticmethod
    def _strategy_dynamic_suggestions(*, summary, win_rate, worst, concentration, ret_std):
        suggestions = []
        avg_turnover = summary.get("avg_turnover")
        if avg_turnover is not None and float(avg_turnover) >= 0.6:
            suggestions.append("换手偏高 → 把交易成本调到 30~50 bps 检查收益是否大幅缩水")
        elif avg_turnover is not None and float(avg_turnover) < 0.3:
            suggestions.append("换手很低 → 尝试缩短调仓频率 (周频) 看是否能捕获更多机会")
        if win_rate is not None and win_rate < 0.45:
            suggestions.append("胜率偏低 → 把 TopN 减少 2~3 档, 提高选债集中度")
        if concentration is not None and concentration >= 0.65:
            suggestions.append("收益集中 → 把 TopN 增加到 15~20, 分散个券依赖风险")
        if worst is not None and worst <= -0.1:
            suggestions.append("极端亏损 → 尝试加价格上限 (如 ≤130), 控制高位入场风险")
        if ret_std is not None and ret_std > 0.05:
            suggestions.append("波动偏大 → 加转股溢价率上限, 筛掉高弹性高波动标的")
        sharpe = summary.get("sharpe")
        if sharpe is not None and float(sharpe) < 0.5:
            suggestions.append("Sharpe 偏低 → 切换选债规则 (综合机会 vs 低估候选) 做对比")
        if not suggestions:
            suggestions.append("各项指标尚可, 用快速模式把 TopN 上下浮动一档加入对比验证")
        suggestions.append("切到精确模式 (M/N 调大) 复核最终候选策略")
        return suggestions

    def _render_strategy_data_panel(self, result):
        """数据质量 + 回测参数 + 各期质量表."""
        frame = getattr(self, "strategy_bt_data_frame", None)
        if frame is None:
            return
        self._clear_strategy_panel(frame)
        diagnostics = result.get("diagnostics") or {}
        data_quality = diagnostics.get("data_quality") or {}
        performance = diagnostics.get("performance") or {}
        config = result.get("config") or {}
        periods = result.get("periods") or []

        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(3, weight=1)

        # ── 顶部指标条 (一行 5 个 tile) ──────────────────────────
        fallback_ratio = float(data_quality.get("current_fallback_ratio") or 0.0)
        if fallback_ratio <= 0:
            quality, q_color = "高", get_color(GREEN)
        elif fallback_ratio <= 0.2:
            quality, q_color = "中", get_color(ORANGE)
        else:
            quality, q_color = "低", get_color(RED)

        sample_count = data_quality.get("sample_count") or 0
        patch_count = data_quality.get("patch_applied_count") or 0
        event_count = data_quality.get("event_applied_count") or 0
        source_counts = data_quality.get("source_counts") or {}
        if isinstance(source_counts, dict) and source_counts:
            source_labels = {
                "current_fallback": "当前回退",
                "history_snapshot": "历史快照",
                "provider_history": "实时历史",
            }
            source_text = " / ".join(
                f"{source_labels.get(str(k), str(k))} {v}"
                for k, v in source_counts.items()
            )
        else:
            source_text = "全部回退" if fallback_ratio >= 0.99 else "—"

        tiles_row = ctk.CTkFrame(frame, fg_color="transparent")
        tiles_row.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))
        for c in range(5):
            tiles_row.grid_columnconfigure(c, weight=1)

        def _data_tile(col, title, value, *, color=None):
            border_c = ACCENT if col == 0 else BORDER
            cell = ctk.CTkFrame(tiles_row, fg_color=BG_INPUT, corner_radius=8,
                                border_width=1, border_color=border_c)
            cell.grid(row=0, column=col, sticky="nsew", padx=4, pady=2)
            inner = ctk.CTkFrame(cell, fg_color="transparent")
            inner.pack(fill="both", expand=True, padx=10, pady=6)
            ctk.CTkLabel(inner, text=title, text_color=TEXT_DIM,
                         font=(FONT_FAMILY, 10, "bold")).pack(anchor="w")
            ctk.CTkLabel(inner, text=str(value),
                         text_color=color or TEXT,
                         font=(FONT_MONO, 15, "bold")).pack(anchor="w", pady=(2, 0))

        _data_tile(0, "数据质量", quality, color=q_color)
        _data_tile(1, "条款样本", f"{sample_count:,}")
        _data_tile(2, "条款回退", self._fmt_strategy_pct(fallback_ratio))
        _data_tile(3, "转股价修正", f"{patch_count:,}")
        _data_tile(4, "公告修正", f"{event_count:,}")

        # ── 中部: 条款来源 / 运算缓存 / 策略参数 / 成本基准 ─────────
        mid = ctk.CTkFrame(frame, fg_color="transparent")
        mid.grid(row=1, column=0, sticky="ew", padx=8, pady=(4, 4))
        for c in range(4):
            mid.grid_columnconfigure(c, weight=1, uniform="strategy_data_info")

        def _info_card(col, title, value, meta=None, *, color=None):
            card = ctk.CTkFrame(
                mid, fg_color=BG_INPUT, corner_radius=8, height=64,
                border_width=1, border_color=BORDER)
            card.grid(row=0, column=col, sticky="nsew", padx=4, pady=2)
            card.grid_propagate(False)
            inner = ctk.CTkFrame(card, fg_color="transparent")
            inner.pack(fill="both", expand=True, padx=12, pady=7)
            ctk.CTkLabel(
                inner, text=title, text_color=TEXT_DIM,
                font=(FONT_FAMILY, 10, "bold")).pack(anchor="w")
            value_label = ctk.CTkLabel(
                inner, text=str(value or "—"), text_color=color or TEXT,
                font=(FONT_FAMILY, 12, "bold"), justify="left", wraplength=360)
            value_label.pack(anchor="w", pady=(2, 0))
            meta_label = None
            if meta:
                meta_label = ctk.CTkLabel(
                    inner, text=str(meta), text_color=TEXT_DIM,
                    font=(FONT_FAMILY, 10), justify="left", wraplength=360)
                meta_label.pack(anchor="w", pady=(1, 0))

            def _update_wrap(event, labels=(value_label, meta_label)):
                for label in labels:
                    if label is not None:
                        label.configure(wraplength=max(120, event.width - 24))

            card.bind("<Configure>", _update_wrap)

        history_mode = config.get("history_mode") or "标准"
        perf_parts = []
        hits = performance.get("pricing_snapshot_hits")
        misses = performance.get("pricing_snapshot_misses")
        excluded = performance.get("price_prefilter_excluded")
        if hits is not None:
            perf_parts.append(f"命中 {hits}")
        if misses is not None:
            perf_parts.append(f"未命中 {misses}")
        if excluded is not None:
            perf_parts.append(f"预筛 {excluded}")
        perf_text = " · ".join(perf_parts) if perf_parts else "—"
        freq_map = {"M": "月频", "W": "周频", "Q": "季频", "D": "日频",
                    "月": "月频", "周": "周频", "季": "季频", "日": "日频"}
        freq_value = config.get("rebalance_freq")
        freq_text = freq_map.get(str(freq_value), str(freq_value or "—"))
        top_n = config.get("top_n")
        holding_mode = config.get("holding_mode")
        funding_mode = config.get("funding_mode")
        if not funding_mode:   # 兼容旧快照: 由 top_n_shortfall_policy 推断
            legacy = str(config.get("top_n_shortfall_policy") or "cash")
            funding_mode = "full_invest" if legacy in ("renormalize", "full_invest") else "reserve_cash"
        strategy_text = f"{config.get('selection_view') or '—'} · {freq_text}"
        if holding_mode == "pool":
            cap = config.get("max_holdings")
            strategy_text += " · 等权全池" + (f"(≤{int(cap)})" if cap else "")
        elif top_n is not None:   # top_score 或旧快照
            strategy_text += f" · 机会分Top{top_n}"
        shortfall_text = "满仓等权" if funding_mode == "full_invest" else "缺口留现金"
        cost = config.get("transaction_cost")
        try:
            cost_text = f"{float(cost) * 10000:.0f} bps"
        except (TypeError, ValueError):
            cost_text = "—"
        cash_yield = config.get("cash_yield_rate")
        try:
            if float(cash_yield) > 0:
                cost_text += f" · 现金计息 {float(cash_yield)*100:.1f}%/年"
        except (TypeError, ValueError):
            pass
        if config.get("exposure_mode") == "valuation":
            cost_text += " · 估值择时缩放"
        benchmark_text = "等权基准" if config.get("compute_benchmark") else "不对标"

        _info_card(0, "条款来源", source_text, f"历史口径 {history_mode}")
        _info_card(1, "运算缓存", perf_text)
        _info_card(2, "策略参数", strategy_text, shortfall_text)
        _info_card(3, "成本 / 基准", cost_text, benchmark_text)

        # 逐期数据口径
        period_rows = []
        for period in periods:
            dq = period.get("data_quality") or {}
            fb = dq.get("current_fallback_ratio")
            fb_pct = self._fmt_strategy_pct(fb)
            if fb is not None and float(fb) > 0.3:
                fb_pct = f"⚠ {fb_pct}"
            period_rows.append([
                period.get("start_date", ""),
                period.get("eligible_count", 0),
                period.get("candidate_count", 0),
                period.get("selected_count", 0),
                fb_pct,
                dq.get("patch_applied_count", 0),
                dq.get("event_applied_count", 0),
            ])
        self._strategy_section_title(frame, "各期数据质量", 2, 0)
        self._render_strategy_small_tree(
            frame, 3, 0,
            ["date", "eligible", "candidate", "selected", "fallback", "patch", "event"],
            ["换仓日", "可交易", "待选", "入选", "条款回退%", "转股价修正", "公告修正"],
            [100, 70, 70, 70, 92, 70, 70],
            period_rows,
            max_height=STRATEGY_DATA_TABLE_HEIGHT,
        )
