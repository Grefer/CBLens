"""🔥 敏感性热力图."""
from __future__ import annotations

import threading
from tkinter import filedialog, messagebox

import numpy as np
import matplotlib.pyplot as plt
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

from ...sensitivity import compute_sensitivity_grid
from ..theme import (
    ACCENT, BG_CARD, BG_INPUT, BORDER,
    TEXT, TEXT_DIM,
    get_color,
)


class SensitivityMixin:
    """敏感性 tab 的业务逻辑."""

    def _run_sensitivity(self):
        try:
            params = self._collect_params()
        except Exception as exc:
            messagebox.showerror("参数错误", str(exc))
            return
        try:
            s_min = float(self.v_sens_s_min.get()) / 100.0
            s_max = float(self.v_sens_s_max.get()) / 100.0
            sig_min = float(self.v_sens_sig_min.get()) / 100.0
            sig_max = float(self.v_sens_sig_max.get()) / 100.0
            steps = int(self.v_sens_steps.get())
        except ValueError:
            messagebox.showerror("错误", "请输入有效的范围参数")
            return
        if steps < 3 or steps > 30:
            messagebox.showwarning("提示", "网格步数建议 3~30")
            return
        self.btn_sensitivity.configure(state="disabled")
        self._start_progress("正在计算敏感性网格")
        threading.Thread(
            target=self._sensitivity_worker,
            args=(params, s_min, s_max, sig_min, sig_max, steps),
            daemon=True).start()

    def _sensitivity_worker(self, params, s_min, s_max, sig_min, sig_max, steps):
        try:
            K = params["pricer"]["K"]
            S_vals = np.linspace(K * s_min, K * s_max, steps)
            sig_vals = np.linspace(sig_min, sig_max, steps)
            m = params["model"]
            m_fast = dict(m, M=max(100, m["M"] // 4), N=max(500, m["N"] // 4))
            total = steps * steps

            def progress(done, _total):
                if done % max(1, total // 20) == 0:
                    self.after(0, lambda d=done: self.v_sens_status.set(
                        f"进度 {d}/{total} ..."))

            grid = compute_sensitivity_grid(
                pricer_kwargs=params["pricer"],
                model_kwargs=m_fast,
                s_grid=S_vals,
                sigma_grid=sig_vals,
                max_workers=4,
                progress_cb=progress,
            )
            self.after(0, self._render_sensitivity_chart, S_vals, sig_vals, grid, K)
        except Exception as exc:
            self.after(0, lambda: self.v_sens_status.set(f"❌ 敏感性分析失败: {exc}"))
            self.after(0, lambda: messagebox.showerror("敏感性分析失败", str(exc)))
        finally:
            self.after(0, self._stop_progress)
            self.after(0, lambda: self.btn_sensitivity.configure(state="normal"))

    def _render_sensitivity_chart(self, S_vals, sig_vals, grid, K):
        if self._sens_figure is not None:
            self._sens_figure.clf()
            plt.close(self._sens_figure)
            self._sens_figure = None
            self._sens_canvas = None
        for child in self.sens_chart_frame.winfo_children():
            child.destroy()

        bg = get_color(BG_CARD)
        bg_in = get_color(BG_INPUT)
        txt = get_color(TEXT)
        txt_dim = get_color(TEXT_DIM)
        brd = get_color(BORDER)

        fig = Figure(figsize=(11, 5), dpi=100, facecolor=bg)
        ax = fig.add_subplot(111, facecolor=bg_in)

        vmin, vmax = float(np.min(grid)), float(np.max(grid))
        center = 100.0
        if vmin < center < vmax:
            from matplotlib.colors import TwoSlopeNorm
            norm = TwoSlopeNorm(vmin=vmin, vcenter=center, vmax=vmax)
        else:
            norm = None

        im = ax.pcolormesh(S_vals, sig_vals * 100, grid, cmap="RdYlGn",
                           norm=norm, shading="auto")
        cbar = fig.colorbar(im, ax=ax, pad=0.02)
        cbar.set_label("理论价 (¥)", color=txt_dim, fontsize=10)
        cbar.ax.tick_params(colors=txt_dim, labelsize=9)

        # Mark current point
        try:
            cur_s = float(self.v_S0.get())
            cur_sig = float(self.v_sigma.get())
            ax.plot(cur_s, cur_sig, marker="*", markersize=18,
                    color=get_color(ACCENT), markeredgecolor="white",
                    markeredgewidth=1.5, zorder=5)
        except ValueError:
            pass

        ax.set_xlabel("正股价 S (¥)", color=txt_dim, fontsize=11)
        ax.set_ylabel("波动率 σ (%)", color=txt_dim, fontsize=11)
        ax.set_title("理论价 σ-S 敏感性热力图", color=txt, fontsize=13, fontweight="bold")
        ax.tick_params(colors=txt_dim, labelsize=9)
        for spine in ax.spines.values():
            spine.set_color(brd)

        fig.tight_layout()
        canvas = FigureCanvasTkAgg(fig, master=self.sens_chart_frame)
        canvas.draw()
        canvas.get_tk_widget().grid(row=0, column=0, sticky="nsew")
        self._sens_figure = fig
        self._sens_canvas = canvas
        self._last_sens_args = (S_vals, sig_vals, grid, K)
        self.v_sens_status.set(
            f"✅ {len(S_vals)}×{len(sig_vals)} = {len(S_vals)*len(sig_vals)} 点  |  "
            f"价格范围 {float(np.min(grid)):.2f} ~ {float(np.max(grid)):.2f}")
        if hasattr(self, "btn_sens_png"):
            self.btn_sens_png.configure(state="normal")

    def _export_sens_png(self):
        if self._sens_figure is None:
            messagebox.showinfo("提示", "请先运行敏感性分析")
            return
        path = filedialog.asksaveasfilename(
            title="导出热力图",
            defaultextension=".png",
            filetypes=[("PNG", "*.png"), ("PDF", "*.pdf"), ("SVG", "*.svg")],
            initialfile=(self.v_bond_code.get().strip() or "sensitivity") + "_heatmap.png",
        )
        if not path:
            return
        try:
            self._sens_figure.savefig(path, dpi=150, bbox_inches="tight",
                                      facecolor=self._sens_figure.get_facecolor())
            self.v_sens_status.set(f"已导出热力图到 {path}")
        except Exception as exc:
            messagebox.showerror("导出失败", str(exc))
