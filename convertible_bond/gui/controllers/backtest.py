"""📈 历史回测 — 单债"模型 vs 市场"偏差复盘.

从原 backtest.py 拆分: 本模块只保留单只转债的历史理论价 vs 市场价偏差回测;
基于机会分的多债选债策略回测见 strategy_backtest.py。两个 mixin 都混入 CBPricerApp。
"""
from __future__ import annotations

import csv
import threading
from datetime import date
from tkinter import filedialog, messagebox

import customtkinter as ctk
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

from ...backtest import backtest_theoretical_price
from ..theme import (
    ACCENT, BG_CARD, BG_INPUT, BORDER,
    GREEN, ORANGE, RED,
    TEXT, TEXT_DIM,
    FONT_FAMILY, FONT_MONO,
    VOL_WINDOW_MAP,
    get_color,
)


class BacktestMixin:
    """单债历史回测 tab 的业务逻辑."""

    def _run_backtest(self):
        code = self.v_bond_code.get().strip()
        if not code:
            messagebox.showwarning("提示", "请先输入转债代码")
            return
        try:
            start = date.fromisoformat(self.v_bt_start.get().strip())
            end = date.fromisoformat(self.v_bt_end.get().strip())
        except ValueError:
            messagebox.showerror("错误", "日期格式应为 YYYY-MM-DD")
            return
        if start >= end:
            messagebox.showerror("错误", "开始日期应早于结束日期")
            return

        freq_map = {"日": "D", "周": "W", "月": "M"}
        freq = freq_map.get(self.v_bt_freq.get(), "W")

        try:
            params = dict(
                r=float(self.v_r.get()) / 100.0,
                q=float(self.v_q.get()) / 100.0,
                base_spread=float(self.v_spread.get()) / 100.0,
                p_down=float(self.v_p_down.get()) / 100.0,
                distress_k=float(self.v_dk.get()) / 100.0,
                M=int(float(self.v_M.get())),
                N=int(float(self.v_N.get())),
                vol_window_days=VOL_WINDOW_MAP.get(self.v_vol_window.get(), 21),
                solve_iv=bool(self.v_bt_solve_iv.get()),
                call_notice_days=int(float(self.v_call_notice.get())),
            )
        except ValueError as e:
            messagebox.showerror("错误", f"参数解析失败: {e}")
            return

        self.btn_backtest.configure(state="disabled")
        self.v_bt_status.set(f"正在回测 {code} {start} → {end} ({self.v_bt_freq.get()}频) ...")
        threading.Thread(
            target=self._backtest_worker,
            args=(code, start, end, freq, params),
            daemon=True,
        ).start()

    def _backtest_worker(self, code, start, end, freq, params):
        try:
            provider = self._get_provider()

            def progress(i, total):
                self.after(0, lambda: self.v_bt_status.set(
                    f"进度 {i}/{total} ..."
                ))

            result = backtest_theoretical_price(
                code, start_date=start, end_date=end, freq=freq,
                provider=provider, progress_cb=progress, **params,
            )
            self._last_bt_result = result
            self.after(0, self._render_backtest_chart, result)
        except Exception as exc:
            self.after(0, lambda exc=exc: self.v_bt_status.set(f"❌ 回测失败: {exc}"))
            self.after(0, lambda exc=exc: messagebox.showerror("回测失败", str(exc)))
        finally:
            self.after(0, lambda: self.btn_backtest.configure(state="normal"))

    def _refresh_backtest_chart(self):
        """切换"价值分解"复选框时无需重新拉数据, 用缓存重绘."""
        if self._last_bt_result is not None:
            self._render_backtest_chart(self._last_bt_result)

    def _render_backtest_chart(self, result):
        dates = result["dates"]
        theo = result["theo_prices"]
        mkt = result["market_prices"]
        bond_floors = result.get("bond_floors", [])
        parities = result.get("parities", [])
        sigmas = result.get("sigmas", [])
        ivs = result.get("ivs", [])

        if not dates:
            self.v_bt_status.set("❌ 无有效采样点")
            return

        # 释放旧图表资源，防止内存泄漏
        if self._bt_figure is not None:
            self._bt_figure.clf()
            plt.close(self._bt_figure)
            self._bt_figure = None
            self._bt_canvas = None

        for child in self.bt_chart_frame.winfo_children():
            child.destroy()

        # 根据当前深浅色模式获取真实 HEX
        bg_card_color = get_color(BG_CARD)
        bg_input_color = get_color(BG_INPUT)
        text_dim_color = get_color(TEXT_DIM)
        text_color = get_color(TEXT)
        border_color = get_color(BORDER)
        accent_color = get_color(ACCENT)
        orange_color = get_color(ORANGE)
        red_color = get_color(RED)
        green_color = get_color(GREEN)

        iv_arr = np.array([v if v is not None else np.nan for v in ivs], dtype=float) \
                 if len(ivs) else np.array([])
        has_iv = iv_arr.size > 0 and bool(np.any(np.isfinite(iv_arr)))
        show_decomp = bool(self.v_bt_show_decomp.get()) and bond_floors and parities
        theo_arr = np.array(theo, dtype=float)
        mkt_arr = np.array(mkt, dtype=float)
        metrics = self._compute_backtest_metrics(
            dates, theo_arr, mkt_arr, sigmas, iv_arr,
            bond_floors=bond_floors, parities=parities,
        )
        rel_dev = metrics["rel_dev"]

        if has_iv:
            fig = Figure(figsize=(11, 7.2), dpi=100, facecolor=bg_card_color)
            gs = fig.add_gridspec(3, 1, height_ratios=[2.0, 0.9, 0.9])
            ax = fig.add_subplot(gs[0, 0], facecolor=bg_input_color)
            ax_dev = fig.add_subplot(gs[1, 0], facecolor=bg_input_color, sharex=ax)
            ax_iv = fig.add_subplot(gs[2, 0], facecolor=bg_input_color, sharex=ax)
        else:
            fig = Figure(figsize=(11, 6.2), dpi=100, facecolor=bg_card_color)
            gs = fig.add_gridspec(2, 1, height_ratios=[2.1, 0.9])
            ax = fig.add_subplot(gs[0, 0], facecolor=bg_input_color)
            ax_dev = fig.add_subplot(gs[1, 0], facecolor=bg_input_color, sharex=ax)
            ax_iv = None

        ax.plot(dates, theo, color=accent_color, linewidth=2.0, marker="o", markersize=4,
                label="理论价", zorder=3)
        ax.plot(dates, mkt, color=orange_color, linewidth=2.0, marker="s", markersize=4,
                label="市价(收盘)", zorder=2)

        if show_decomp:
            ax.plot(dates, bond_floors, color=text_dim_color, linewidth=1.2,
                    linestyle="--", alpha=0.7, label="纯债价值", zorder=1)
            ax.plot(dates, parities, color=green_color, linewidth=1.2,
                    linestyle=":", alpha=0.7, label="转股价值", zorder=1)

        ax.fill_between(dates, theo_arr, mkt_arr,
                        where=(mkt_arr >= theo_arr).tolist(), color=red_color, alpha=0.12, label="市价溢价")
        ax.fill_between(dates, theo_arr, mkt_arr,
                        where=(mkt_arr < theo_arr).tolist(), color=green_color, alpha=0.12, label="市价折价")

        ax.set_ylabel("价格", color=text_dim_color, fontsize=10)
        ax.tick_params(colors=text_dim_color, labelsize=9, labelbottom=False)
        for spine in ax.spines.values():
            spine.set_color(border_color)
        ax.grid(True, color=border_color, linestyle="--", alpha=0.4)

        legend = ax.legend(loc="best", framealpha=0.9, facecolor=bg_card_color,
                           edgecolor=border_color, fontsize=9, labelcolor=text_color)
        legend.get_frame().set_linewidth(0.5)

        dev_pct = rel_dev * 100
        ax_dev.axhspan(-5, 5, color=green_color, alpha=0.08, label="±5% 命中带")
        ax_dev.axhline(0.0, color=border_color, linewidth=1.0)
        ax_dev.axhline(5.0, color=border_color, linewidth=0.8, linestyle="--", alpha=0.7)
        ax_dev.axhline(-5.0, color=border_color, linewidth=0.8, linestyle="--", alpha=0.7)
        ax_dev.plot(dates, dev_pct, color=accent_color, linewidth=1.8,
                    marker="o", markersize=3, label="理论−市价")
        ax_dev.fill_between(
            dates, dev_pct, 0.0,
            where=np.nan_to_num(dev_pct, nan=0.0) >= 0,
            color=green_color, alpha=0.14)
        ax_dev.fill_between(
            dates, dev_pct, 0.0,
            where=np.nan_to_num(dev_pct, nan=0.0) < 0,
            color=red_color, alpha=0.14)
        max_idx = metrics.get("max_abs_idx")
        if max_idx is not None and np.isfinite(dev_pct[max_idx]):
            ax_dev.scatter([dates[max_idx]], [dev_pct[max_idx]], s=32,
                           color=red_color, zorder=4)
            ax_dev.annotate(
                f"最大偏差 {dev_pct[max_idx]:+.1f}%",
                xy=(dates[max_idx], dev_pct[max_idx]),
                xytext=(8, 10), textcoords="offset points",
                fontsize=8, color=red_color,
                arrowprops={"arrowstyle": "->", "color": red_color, "lw": 0.8},
            )
        ax_dev.set_ylabel("偏差 (%)", color=text_dim_color, fontsize=10)
        ax_dev.tick_params(colors=text_dim_color, labelsize=9, labelbottom=ax_iv is None)
        ax_dev.grid(True, color=border_color, linestyle="--", alpha=0.35)
        for spine in ax_dev.spines.values():
            spine.set_color(border_color)
        leg_dev = ax_dev.legend(loc="best", framealpha=0.9, facecolor=bg_card_color,
                                edgecolor=border_color, fontsize=8, labelcolor=text_color)
        leg_dev.get_frame().set_linewidth(0.5)

        if ax_iv is not None:
            hv_pct = np.array(sigmas) * 100
            iv_pct = iv_arr * 100
            ax_iv.plot(dates, hv_pct, color=text_dim_color, linewidth=1.5,
                       marker="o", markersize=3, label="历史波动率 HV", zorder=2)
            ax_iv.plot(dates, iv_pct, color=accent_color, linewidth=2.0,
                       marker="s", markersize=4, label="隐含波动率 IV", zorder=3)
            valid = np.isfinite(iv_pct) & np.isfinite(hv_pct)
            if np.any(valid):
                d_v = np.array(dates)[valid]
                hv_v = hv_pct[valid]
                iv_v = iv_pct[valid]
                where_high = [bool(x) for x in (iv_v >= hv_v)]
                where_low = [bool(x) for x in (iv_v < hv_v)]
                ax_iv.fill_between(d_v, hv_v, iv_v, where=where_high,
                                   color=red_color, alpha=0.12)
                ax_iv.fill_between(d_v, hv_v, iv_v, where=where_low,
                                   color=green_color, alpha=0.12)
            ax_iv.set_xlabel("日期", color=text_dim_color, fontsize=10)
            ax_iv.set_ylabel("σ (%)", color=text_dim_color, fontsize=10)
            ax_iv.tick_params(colors=text_dim_color, labelsize=9)
            for spine in ax_iv.spines.values():
                spine.set_color(border_color)
            ax_iv.grid(True, color=border_color, linestyle="--", alpha=0.4)
            leg_iv = ax_iv.legend(loc="best", framealpha=0.9, facecolor=bg_card_color,
                                  edgecolor=border_color, fontsize=9, labelcolor=text_color)
            leg_iv.get_frame().set_linewidth(0.5)
        else:
            ax_dev.set_xlabel("日期", color=text_dim_color, fontsize=10)

        fig.autofmt_xdate(rotation=25)
        fig.tight_layout()

        canvas = FigureCanvasTkAgg(fig, master=self.bt_chart_frame)
        canvas.draw()
        canvas.get_tk_widget().grid(row=0, column=0, sticky="nsew", padx=12, pady=12)

        self._bt_figure = fig
        self._bt_canvas = canvas

        self._update_backtest_stats(
            metrics["mean_dev"], metrics["rmse"], metrics["max_abs"],
            metrics["hit_rate"], metrics["corr"], metrics["iv_hv_pp"],
        )
        self._render_backtest_result_panel(result, metrics)
        status_parts = [
            f"✅ {len(dates)} 个采样点",
            f"平均基差(市价−理论)={metrics['mean_basis_abs']:+.2f}",
        ]
        self.v_bt_status.set("  ·  ".join(status_parts))
        self.btn_bt_png.configure(state="normal")
        self.btn_bt_csv.configure(state="normal")

    @staticmethod
    def _compute_backtest_metrics(
        dates, theo_arr, mkt_arr, sigmas, iv_arr, *, bond_floors=None, parities=None,
    ):
        """汇总单债回测展示所需指标; 偏差 = (理论 − 市价) / 市价."""
        valid = (mkt_arr > 0) & np.isfinite(mkt_arr) & np.isfinite(theo_arr)
        rel_dev = np.full(theo_arr.shape, np.nan)
        rel_dev[valid] = (theo_arr[valid] - mkt_arr[valid]) / mkt_arr[valid]
        rel_clean = rel_dev[np.isfinite(rel_dev)]
        basis = mkt_arr - theo_arr
        basis_clean = basis[np.isfinite(basis)]
        mean_basis_abs = float(np.mean(basis_clean)) if basis_clean.size else float("nan")
        corr = float("nan")
        if int(np.sum(valid)) > 1:
            theo_valid = theo_arr[valid]
            mkt_valid = mkt_arr[valid]
            if np.std(theo_valid) > 1e-12 and np.std(mkt_valid) > 1e-12:
                corr = float(np.corrcoef(theo_valid, mkt_valid)[0, 1])
        if rel_clean.size:
            mean_dev = float(np.mean(rel_clean))
            rmse = float(np.sqrt(np.mean(rel_clean ** 2)))
            max_abs = float(np.max(np.abs(rel_clean)))
            hit_rate = float(np.mean(np.abs(rel_clean) <= 0.05))
            finite_idx = np.where(np.isfinite(rel_dev))[0]
            max_abs_idx = int(finite_idx[np.argmax(np.abs(rel_dev[finite_idx]))])
            under_idx = int(finite_idx[np.argmax(rel_dev[finite_idx])])
            over_idx = int(finite_idx[np.argmin(rel_dev[finite_idx])])
            latest_idx = int(finite_idx[-1])
        else:
            mean_dev = rmse = max_abs = hit_rate = float("nan")
            max_abs_idx = under_idx = over_idx = latest_idx = None

        iv_hv_pp: float | None = None
        if iv_arr.size:
            hv_arr = np.array(sigmas, dtype=float)
            n = min(iv_arr.size, hv_arr.size)
            iv_valid_mask = np.isfinite(iv_arr[:n]) & np.isfinite(hv_arr[:n])
            if np.any(iv_valid_mask):
                iv_hv_pp = float(
                    np.mean(iv_arr[:n][iv_valid_mask] - hv_arr[:n][iv_valid_mask])
                ) * 100

        latest = {}
        if latest_idx is not None:
            latest = {
                "date": dates[latest_idx],
                "theo": float(theo_arr[latest_idx]),
                "market": float(mkt_arr[latest_idx]),
                "basis": float(mkt_arr[latest_idx] - theo_arr[latest_idx]),
                "dev": float(rel_dev[latest_idx]),
                "sigma": float(sigmas[latest_idx]) if latest_idx < len(sigmas) else float("nan"),
                "iv": float(iv_arr[latest_idx]) if latest_idx < iv_arr.size else float("nan"),
                "bond_floor": (
                    float(bond_floors[latest_idx])
                    if bond_floors and latest_idx < len(bond_floors) else float("nan")
                ),
                "parity": (
                    float(parities[latest_idx])
                    if parities and latest_idx < len(parities) else float("nan")
                ),
            }

        return {
            "rel_dev": rel_dev,
            "mean_dev": mean_dev,
            "rmse": rmse,
            "max_abs": max_abs,
            "hit_rate": hit_rate,
            "corr": corr,
            "iv_hv_pp": iv_hv_pp,
            "mean_basis_abs": mean_basis_abs,
            "max_abs_idx": max_abs_idx,
            "under_idx": under_idx,
            "over_idx": over_idx,
            "latest_idx": latest_idx,
            "latest": latest,
        }

    def _render_backtest_result_panel(self, result, metrics):
        frame = getattr(self, "bt_result_frame", None)
        if frame is None:
            return
        for child in frame.winfo_children():
            child.destroy()

        frame.grid_columnconfigure(0, weight=2, uniform="bt-result")
        frame.grid_columnconfigure(1, weight=3, uniform="bt-result")
        dates = result.get("dates") or []
        rel_dev = metrics["rel_dev"]
        latest = metrics.get("latest") or {}
        latest_dev = latest.get("dev")

        if latest_dev is None or not np.isfinite(latest_dev):
            verdict = "最新采样点暂无有效偏差"
        elif latest_dev >= 0.05:
            verdict = "最新理论价高于市价, 偏低估信号较明显"
        elif latest_dev > 0:
            verdict = "最新理论价略高于市价, 估值略有安全垫"
        elif latest_dev <= -0.05:
            verdict = "最新市价高于理论价, 估值偏贵需复核"
        else:
            verdict = "最新市价贴近模型中枢"

        rmse = metrics.get("rmse")
        hit_rate = metrics.get("hit_rate")
        if np.isfinite(rmse) and np.isfinite(hit_rate):
            if rmse <= 0.03 and hit_rate >= 0.7:
                quality = "模型跟踪稳定"
            elif rmse <= 0.07:
                quality = "模型跟踪一般"
            else:
                quality = "偏差波动较大, 建议复核条款、波动率或信用利差"
        else:
            quality = "样本不足, 暂不评价跟踪质量"

        max_idx = metrics.get("max_abs_idx")
        max_text = "最大偏差 —"
        if max_idx is not None and max_idx < len(dates):
            max_text = (
                f"最大偏差 {dates[max_idx]} "
                f"{self._fmt_bt_pct(rel_dev[max_idx], sign=True)}"
            )

        left = ctk.CTkFrame(frame, fg_color=BG_INPUT, corner_radius=8)
        left.grid(row=0, column=0, sticky="nsew", padx=(10, 5), pady=8)
        ctk.CTkLabel(left, text="结果解读", text_color=TEXT_DIM,
                     font=(FONT_FAMILY, 11, "bold")).pack(anchor="w", padx=12, pady=(8, 2))
        ctk.CTkLabel(left, text=verdict, text_color=TEXT,
                     font=(FONT_FAMILY, 14, "bold"),
                     wraplength=480, justify="left").pack(anchor="w", padx=12)
        ctk.CTkLabel(
            left,
            text=(
                f"{quality} · 平均偏差 "
                f"{self._fmt_bt_pct(metrics.get('mean_dev'), sign=True)} · {max_text}"
            ),
            text_color=TEXT_DIM, font=(FONT_FAMILY, 11),
            wraplength=520, justify="left",
        ).pack(anchor="w", padx=12, pady=(4, 8))

        right = ctk.CTkFrame(frame, fg_color=BG_INPUT, corner_radius=8)
        right.grid(row=0, column=1, sticky="nsew", padx=(5, 10), pady=8)
        for col in range(4):
            right.grid_columnconfigure(col, weight=1, uniform="bt-latest")
        ctk.CTkLabel(right, text="最新样本", text_color=TEXT_DIM,
                     font=(FONT_FAMILY, 11, "bold")).grid(
                         row=0, column=0, columnspan=4, sticky="w", padx=12, pady=(8, 2))

        items = [
            ("日期", str(latest.get("date", "—"))),
            ("理论 / 市价", (
                f"{self._fmt_bt_price(latest.get('theo'))} / "
                f"{self._fmt_bt_price(latest.get('market'))}"
            )),
            ("偏差", self._fmt_bt_pct(latest.get("dev"), sign=True)),
            ("基差", self._fmt_bt_price(latest.get("basis"), sign=True)),
            ("HV", self._fmt_bt_pct(latest.get("sigma"))),
            ("IV", self._fmt_bt_pct(latest.get("iv"))),
            ("纯债价值", self._fmt_bt_price(latest.get("bond_floor"))),
            ("转股价值", self._fmt_bt_price(latest.get("parity"))),
        ]
        for idx, (label, value) in enumerate(items):
            cell = ctk.CTkFrame(right, fg_color=BG_CARD, corner_radius=6)
            cell.grid(row=1 + idx // 4, column=idx % 4, sticky="nsew",
                      padx=(12 if idx % 4 == 0 else 4, 12 if idx % 4 == 3 else 4),
                      pady=(2, 8 if idx // 4 == 1 else 4))
            ctk.CTkLabel(cell, text=label, text_color=TEXT_DIM,
                         font=(FONT_FAMILY, 10)).pack(anchor="w", padx=8, pady=(5, 0))
            ctk.CTkLabel(cell, text=value, text_color=TEXT,
                         font=(FONT_MONO, 12, "bold")).pack(anchor="w", padx=8, pady=(0, 5))

    def _update_backtest_stats(self, mean_dev, rmse, max_abs, hit_rate, corr, iv_hv_pp):
        stats = getattr(self, "_bt_stat_vars", None)
        if not stats:
            return
        labels = getattr(self, "_bt_stat_labels", {})
        green, red, base = get_color(GREEN), get_color(RED), get_color(TEXT)

        def _fmt_pct(v, sign=False):
            if not np.isfinite(v):
                return "—"
            return f"{v*100:+.2f}%" if sign else f"{v*100:.2f}%"

        stats["mean_dev"].set(_fmt_pct(mean_dev, sign=True))
        stats["rmse"].set(_fmt_pct(rmse))
        stats["max_abs"].set(_fmt_pct(max_abs))
        stats["hit_rate"].set(f"{hit_rate*100:.1f}%" if np.isfinite(hit_rate) else "—")
        stats["corr"].set(f"{corr:.3f}" if np.isfinite(corr) else "—")
        stats["iv_hv"].set(f"{iv_hv_pp:+.2f}pp" if iv_hv_pp is not None and np.isfinite(iv_hv_pp) else "—")
        color_rules = {
            "mean_dev": green if np.isfinite(mean_dev) and mean_dev > 0 else red,
            "rmse": green if np.isfinite(rmse) and rmse <= 0.05 else red,
            "max_abs": green if np.isfinite(max_abs) and max_abs <= 0.10 else red,
            "hit_rate": green if np.isfinite(hit_rate) and hit_rate >= 0.70 else red,
            "corr": green if np.isfinite(corr) and corr >= 0.80 else red,
            "iv_hv": green if iv_hv_pp is not None and np.isfinite(iv_hv_pp) and iv_hv_pp <= 0 else red,
        }
        for key, label in labels.items():
            raw = stats.get(key).get() if stats.get(key) is not None else "—"
            label.configure(text_color=base if raw == "—" else color_rules.get(key, base))

    @staticmethod
    def _fmt_bt_pct(value, sign=False):
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
    def _fmt_bt_price(value, sign=False):
        if value is None:
            return "—"
        try:
            f = float(value)
        except (TypeError, ValueError):
            return "—"
        if not np.isfinite(f):
            return "—"
        return f"{f:+.2f}" if sign else f"{f:.2f}"

    # ── 回测结果导出 ──────────────────────────────────────
    def _export_bt_png(self):
        if self._bt_figure is None:
            messagebox.showinfo("提示", "请先运行回测")
            return
        path = filedialog.asksaveasfilename(
            title="导出回测图",
            defaultextension=".png",
            filetypes=[("PNG", "*.png"), ("PDF", "*.pdf"), ("SVG", "*.svg")],
            initialfile=(self.v_bond_code.get().strip() or "backtest") + ".png",
        )
        if not path:
            return
        try:
            # 把上方的 6 个统计指标压成一行附到图顶, 让导出图自带摘要;
            # 用 fig.text + bbox_extra_artists 而不是 suptitle, 避免改动现有 tight_layout
            extra_artists = []
            stats_line = self._compose_bt_stats_line()
            bond_code = self.v_bond_code.get().strip()
            header_lines = []
            if bond_code:
                header_lines.append(bond_code)
            if stats_line:
                header_lines.append(stats_line)
            if header_lines:
                txt = self._bt_figure.text(
                    0.5, 1.0, "\n".join(header_lines),
                    ha="center", va="bottom",
                    fontsize=10,
                    color=get_color(TEXT),
                )
                extra_artists.append(txt)
            try:
                self._bt_figure.savefig(
                    path, dpi=150, bbox_inches="tight",
                    bbox_extra_artists=extra_artists,
                    facecolor=self._bt_figure.get_facecolor())
            finally:
                for artist in extra_artists:
                    artist.remove()
            self.v_bt_status.set(f"已导出图表到 {path}")
        except Exception as exc:
            messagebox.showerror("导出失败", str(exc))

    def _compose_bt_stats_line(self) -> str:
        """把 6 个统计指标 StringVar 压成一行 '标签 值  ·  标签 值 ...'."""
        stats = getattr(self, "_bt_stat_vars", None)
        if not stats:
            return ""
        pairs = (
            ("均偏差",     stats.get("mean_dev")),
            ("RMSE",       stats.get("rmse")),
            ("最大|偏差|", stats.get("max_abs")),
            ("命中率±5%",  stats.get("hit_rate")),
            ("相关",       stats.get("corr")),
            ("IV−HV",      stats.get("iv_hv")),
        )
        parts = []
        for label, var in pairs:
            if var is None:
                continue
            val = var.get()
            if not val or val == "—":
                continue
            parts.append(f"{label} {val}")
        return "  ·  ".join(parts)

    def _export_bt_csv(self):
        if not self._last_bt_result or not self._last_bt_result.get("dates"):
            messagebox.showinfo("提示", "请先运行回测")
            return
        path = filedialog.asksaveasfilename(
            title="导出回测序列",
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv"), ("所有文件", "*.*")],
            initialfile=(self.v_bond_code.get().strip() or "backtest") + ".csv",
        )
        if not path:
            return
        try:
            r = self._last_bt_result
            n = len(r["dates"])
            bf = r.get("bond_floors") or [float("nan")] * n
            par = r.get("parities") or [float("nan")] * n
            iv = r.get("ivs") or [float("nan")] * n
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f)
                w.writerow(["date", "theoretical_price", "market_price", "stock_price",
                            "sigma", "bond_floor", "parity", "implied_vol"])
                for d, t, m, s, sg, b, p, ivv in zip(
                        r["dates"], r["theo_prices"], r["market_prices"],
                        r["stock_prices"], r["sigmas"], bf, par, iv):
                    w.writerow([d.isoformat(), f"{t:.4f}", f"{m:.4f}", f"{s:.4f}",
                                f"{sg:.6f}", f"{b:.4f}", f"{p:.4f}", f"{ivv:.6f}"])
            self.v_bt_status.set(f"已导出 {len(r['dates'])} 条记录到 {path}")
        except Exception as exc:
            messagebox.showerror("导出失败", str(exc))
