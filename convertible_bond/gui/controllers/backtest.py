"""📈 历史回测."""
from __future__ import annotations

import csv
import threading
from datetime import date
from tkinter import filedialog, messagebox

import numpy as np
import matplotlib.pyplot as plt
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

from ...backtest import backtest_theoretical_price
from ..theme import (
    ACCENT, BG_CARD, BG_INPUT, BORDER,
    GREEN, ORANGE, RED,
    TEXT, TEXT_DIM,
    VOL_WINDOW_MAP,
    get_color,
)


class BacktestMixin:
    """回测 tab 的业务逻辑."""

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
            self.after(0, lambda: self.v_bt_status.set(f"❌ 回测失败: {exc}"))
            self.after(0, lambda: messagebox.showerror("回测失败", str(exc)))
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

        if has_iv:
            fig = Figure(figsize=(11, 6), dpi=100, facecolor=bg_card_color)
            ax = fig.add_subplot(2, 1, 1, facecolor=bg_input_color)
            ax_iv = fig.add_subplot(2, 1, 2, facecolor=bg_input_color, sharex=ax)
        else:
            fig = Figure(figsize=(11, 5), dpi=100, facecolor=bg_card_color)
            ax = fig.add_subplot(111, facecolor=bg_input_color)
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

        theo_arr = np.array(theo)
        mkt_arr = np.array(mkt)
        ax.fill_between(dates, theo_arr, mkt_arr,
                        where=(mkt_arr >= theo_arr).tolist(), color=red_color, alpha=0.12, label="市价溢价")
        ax.fill_between(dates, theo_arr, mkt_arr,
                        where=(mkt_arr < theo_arr).tolist(), color=green_color, alpha=0.12, label="市价折价")

        ax.set_ylabel("价格", color=text_dim_color, fontsize=10)
        ax.tick_params(colors=text_dim_color, labelsize=9)
        for spine in ax.spines.values():
            spine.set_color(border_color)
        ax.grid(True, color=border_color, linestyle="--", alpha=0.4)

        legend = ax.legend(loc="best", framealpha=0.9, facecolor=bg_card_color,
                           edgecolor=border_color, fontsize=9, labelcolor=text_color)
        legend.get_frame().set_linewidth(0.5)

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
            ax.set_xlabel("日期", color=text_dim_color, fontsize=10)

        fig.autofmt_xdate(rotation=25)
        fig.tight_layout()

        canvas = FigureCanvasTkAgg(fig, master=self.bt_chart_frame)
        canvas.draw()
        canvas.get_tk_widget().grid(row=0, column=0, sticky="nsew", padx=12, pady=12)

        self._bt_figure = fig
        self._bt_canvas = canvas

        # 统计指标: 偏差 = (理论 − 市价) / 市价  (相对值, 投资者角度更直观)
        valid = (mkt_arr > 0) & np.isfinite(mkt_arr) & np.isfinite(theo_arr)
        rel_dev = np.full(theo_arr.shape, np.nan)
        rel_dev[valid] = (theo_arr[valid] - mkt_arr[valid]) / mkt_arr[valid]
        rel_clean = rel_dev[np.isfinite(rel_dev)]
        mean_basis_abs = float(np.mean(mkt_arr - theo_arr))
        corr = float(np.corrcoef(theo_arr, mkt_arr)[0, 1]) if len(theo) > 1 else float("nan")
        if rel_clean.size:
            mean_dev = float(np.mean(rel_clean))
            rmse = float(np.sqrt(np.mean(rel_clean ** 2)))
            max_abs = float(np.max(np.abs(rel_clean)))
            hit_rate = float(np.mean(np.abs(rel_clean) <= 0.05))
        else:
            mean_dev = rmse = max_abs = hit_rate = float("nan")

        iv_hv_pp: float | None = None
        if has_iv:
            iv_valid = iv_arr[np.isfinite(iv_arr)]
            hv_arr = np.array(sigmas)
            hv_for_iv = hv_arr[np.isfinite(iv_arr)]
            if iv_valid.size:
                iv_hv_pp = float(np.mean(iv_valid - hv_for_iv)) * 100

        self._update_backtest_stats(mean_dev, rmse, max_abs, hit_rate, corr, iv_hv_pp)
        status_parts = [
            f"✅ {len(dates)} 个采样点",
            f"平均基差(市价−理论)={mean_basis_abs:+.2f}",
        ]
        self.v_bt_status.set("  ·  ".join(status_parts))
        self.btn_bt_png.configure(state="normal")
        self.btn_bt_csv.configure(state="normal")

    def _update_backtest_stats(self, mean_dev, rmse, max_abs, hit_rate, corr, iv_hv_pp):
        stats = getattr(self, "_bt_stat_vars", None)
        if not stats:
            return

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
