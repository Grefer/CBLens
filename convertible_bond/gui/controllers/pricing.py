"""定价计算 / IV 反解 / 收敛诊断 / 现金流可视化."""
from __future__ import annotations

import threading
from datetime import date
from tkinter import messagebox

import customtkinter as ctk
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

from ...pricer import UniversalCBPricer
from ..theme import (
    ACCENT, BG_APP, BG_CARD, BG_INPUT, BORDER,
    FONT_FAMILY, FONT_MONO,
    GREEN, ORANGE, RED, TEXT, TEXT_DIM,
    get_color,
)


class PricingMixin:
    """⚡ 定价 tab 的业务逻辑."""

    # ── 定价计算 ──────────────────────────────────────────
    def _run_pricing(self):
        code = self._normalize_bond_code(self.v_bond_code.get())
        if code:
            self._maybe_sync_events_background(code)
        self.v_result.set("…")
        self.lbl_result.configure(text_color=TEXT_DIM)
        self.btn_calc.configure(state="disabled")
        self._start_progress("正在计算理论价格")
        threading.Thread(target=self._pricing_worker, daemon=True).start()

    def _pricing_worker(self):
        try:
            params = self._collect_params()
            pricer = UniversalCBPricer(**params["pricer"])
            result = pricer.price(**params["model"], return_greeks=True)
            sigma_used = params["model"]["sigma"]
            self.after(0, lambda: self._show_result(result, pricer, sigma_used))
        except Exception as exc:
            err_msg = f"计算失败: {exc}"
            self.after(0, self._on_error, err_msg)
        finally:
            self.after(0, self._stop_progress)
            self.after(0, lambda: self.btn_calc.configure(state="normal"))

    def _collect_params(self):
        def pf(v, label):
            val = v.get().strip()
            try:
                return float(val)
            except ValueError:
                raise ValueError(f"{label} 需要有效数字, 当前值: '{val}'") from None
        def pd(v, label):
            val = v.get().strip()
            try:
                return date.fromisoformat(val)
            except ValueError:
                raise ValueError(f"{label} 日期格式应为 YYYY-MM-DD, 当前值: '{val}'") from None

        coupon_str = self.v_coupons.get().strip()
        coupon_rates = tuple(float(x.strip()) / 100.0
                             for x in coupon_str.split(",") if x.strip())

        current_date = pd(self.v_cur_date, "估值日期")
        pricer = dict(
            S0=pf(self.v_S0, "正股价 S"),
            K=pf(self.v_K, "转股价 K"),
            face_value=pf(self.v_face, "面值"),
            redemption_price=pf(self.v_redemp, "到期赎回价"),
            current_date=current_date,
            maturity_date=pd(self.v_mat_date, "到期日期"),
            issue_date=pd(self.v_iss_date, "发行日期"),
            conversion_start_date=pd(self.v_conv_date, "转股起始日"),
            coupon_rates=coupon_rates,
            call_trigger_ratio=pf(self.v_call_ratio, "强赎触发") / 100.0,
            put_trigger_ratio=pf(self.v_put_ratio, "回售触发") / 100.0,
            put_active_years=int(pf(self.v_put_years, "回售生效年数")),
            call_notice_days=int(pf(self.v_call_notice, "强赎宽限天数")),
        )

        block_until, p_scale = self._resolve_down_reset_for_pricing(current_date)
        if block_until is not None:
            pricer["down_reset_block_until"] = block_until

        p_down = pf(self.v_p_down, "下修强度 p") / 100.0
        if p_scale is not None:
            p_down *= max(0.0, p_scale)

        model = dict(
            sigma=pf(self.v_sigma, "波动率 σ") / 100.0,
            r=pf(self.v_r, "无风险利率 r") / 100.0,
            q=pf(self.v_q, "股息率 q") / 100.0,
            base_spread=pf(self.v_spread, "信用利差") / 100.0,
            p_down=p_down,
            distress_k=pf(self.v_dk, "信用扩张系数") / 100.0,
            M=int(pf(self.v_M, "空间节点 M")),
            N=int(pf(self.v_N, "时间步数 N")),
        )
        return {"pricer": pricer, "model": model}

    @staticmethod
    def _fmt_greek(val, fmt):
        if val is None:
            return "—"
        try:
            f = float(val)
        except (TypeError, ValueError):
            return "—"
        if f != f:  # NaN
            return "—"
        return format(f, fmt)

    def _show_result(self, result, pricer, sigma_used):
        theo = result["price"] if isinstance(result, dict) else result
        self.v_result.set(f"{theo:.3f}")
        self._reset_what_if_labels()
        self._set_what_if_enabled(True)
        info = (
            f"S₀={pricer.S0:.3f}  K={pricer.K:.2f}  "
            f"T={pricer.T:.4f}年  "
            f"σ={sigma_used*100:.1f}%  "
            f"q={float(self.v_q.get() or 0):.2f}%  "
            f"转股比例={pricer.ratio:.4f}"
        )
        self.v_status.set(info)

        if isinstance(result, dict):
            self.v_bond_floor.set(self._fmt_greek(result.get("bond_floor"), ".3f"))
            self.v_parity.set(self._fmt_greek(result.get("parity"), ".3f"))
            self.v_option_prem.set(self._fmt_greek(result.get("option_premium"), ".3f"))
            self.v_delta.set(self._fmt_greek(result.get("delta"), ".4f"))
            self.v_gamma.set(self._fmt_greek(result.get("gamma"), ".6f"))
            self.v_vega.set(self._fmt_greek(result.get("vega"), ".4f"))
            self.v_theta.set(self._fmt_greek(result.get("theta"), ".4f"))

            # 深度实值 + 已过强赎线: 期权价值数学上为 0, 提示是模型预期而非 bug
            opt = result.get("option_premium") or 0.0
            if abs(opt) < 0.01 and pricer.S0 / pricer.K >= pricer.call_trigger_ratio:
                self.v_status.set(info + "  ·  深度实值 + 已过强赎线, 期权价值锁定为 0 (理论锚 = 转股价值)")

        if theo > 100:
            self.lbl_result.configure(text_color=GREEN)
        elif theo < 100:
            self.lbl_result.configure(text_color=RED)
        else:
            self.lbl_result.configure(text_color=TEXT)

        try:
            mkt = float(self.v_market_price.get())
            if mkt > 0:
                dev = (theo - mkt) / theo * 100
                self.v_deviation.set(f"{dev:+.2f}%")
                self.lbl_deviation.configure(text_color=GREEN if dev > 0 else RED)
            else:
                raise ValueError
        except (ValueError, AttributeError):
            self.v_deviation.set("—")
            if hasattr(self, "lbl_deviation"):
                self.lbl_deviation.configure(text_color=TEXT_DIM)

    # ── 隐含波动率反解 ──────────────────────────────────────
    def _solve_iv(self):
        try:
            target = float(self.v_market_price.get().strip())
        except ValueError:
            messagebox.showwarning("提示", "请在「市价 ¥」处填入有效数字 (如 110.5)")
            return
        if target <= 0:
            messagebox.showwarning("提示", "市价必须为正数")
            return
        self.btn_iv.configure(state="disabled")
        self._start_progress(f"反解 IV (target={target:.2f})")
        threading.Thread(target=self._solve_iv_worker, args=(target,), daemon=True).start()

    def _solve_iv_worker(self, target):
        try:
            params = self._collect_params()
            pricer = UniversalCBPricer(**params["pricer"])
            m = params["model"]
            iv = pricer.solve_implied_vol(
                target_price=target, r=m["r"], base_spread=m["base_spread"],
                p_down=m["p_down"], distress_k=m["distress_k"],
                M=max(150, m["M"] // 3), N=max(500, m["N"] // 3),
                q=m["q"],
            )
            if iv != iv:  # NaN
                self.after(0, lambda: self.v_iv.set("—"))
                self.after(0, lambda: self.v_status.set(
                    f"❌ 反解失败: 市价 {target:.2f} 在 σ ∈ [5%, 200%] 区间内无解"))
            else:
                self.after(0, lambda: self.v_iv.set(f"{iv*100:.2f}%"))
                hist = float(self.v_sigma.get())
                gap = iv * 100 - hist
                self.after(0, lambda: self.v_status.set(
                    f"反解 IV = {iv*100:.2f}% (匹配市价 {target:.2f}); "
                    f"历史 σ = {hist:.2f}%, 差 {gap:+.2f}pp"))
        except Exception as exc:
            self.after(0, self._on_error, f"反解 IV 失败: {exc}")
        finally:
            self.after(0, self._stop_progress)
            self.after(0, lambda: self.btn_iv.configure(state="normal"))

    # ── What-if 快算 (σ ±pp / S ±%) ──────────────────────────
    @staticmethod
    def _what_if_base_label(kind: str, delta) -> str:
        return f"{delta:+d}pp" if kind == "sigma" else f"{delta:+d}%"

    def _run_what_if(self, kind: str, delta):
        """微扰 σ 或 S 后重算理论价, 结果回写到对应按钮上.

        kind ∈ {"sigma", "S"}; delta 单位:
            sigma → 百分点 (例如 +2 表示 σ 由 28% 涨到 30%)
            S     → 相对百分比 (例如 -5 表示正股价下跌 5%)

        显示形如 "+5% → 130.40 (+2.1%)", 第二项为相对当前理论价的变化幅度。
        """
        button_map = (getattr(self, "_wf_sigma_buttons", None) if kind == "sigma"
                      else getattr(self, "_wf_s_buttons", None))
        if not button_map or delta not in button_map:
            return
        btn, var = button_map[delta]
        base_label = self._what_if_base_label(kind, delta)
        # 抓主结果作为对照基准 (而非 var 当前值, 避免连续点击累积成 "+5% → 130 → 132")
        try:
            base_price = float(self.v_result.get())
        except (TypeError, ValueError):
            base_price = float("nan")
        var.set(f"{base_label} …")
        btn.configure(state="disabled")

        def worker():
            try:
                params = self._collect_params()
                pricer_kwargs = dict(params["pricer"])
                model_kwargs = dict(params["model"])
                if kind == "sigma":
                    model_kwargs["sigma"] = max(0.001, model_kwargs["sigma"] + delta / 100.0)
                else:  # S
                    pricer_kwargs["S0"] = max(0.001, pricer_kwargs["S0"] * (1 + delta / 100.0))
                # what-if 不需要 greeks, 同时降低网格精度以加速
                model_kwargs["M"] = max(150, model_kwargs["M"] // 2)
                model_kwargs["N"] = max(500, model_kwargs["N"] // 2)
                pricer = UniversalCBPricer(**pricer_kwargs)
                price = float(pricer.price(**model_kwargs))
                if base_price == base_price and base_price > 0:
                    rel_pp = (price - base_price) / base_price * 100.0
                    label = f"{base_label} → {price:.2f} ({rel_pp:+.1f}%)"
                else:
                    label = f"{base_label} → {price:.2f}"
                self.after(0, lambda: var.set(label))
            except Exception as exc:
                self.after(0, lambda: var.set(base_label))
                self.after(0, lambda: self.v_status.set(f"What-if 失败: {exc}"))
            finally:
                self.after(0, lambda: btn.configure(state="normal"))

        threading.Thread(target=worker, daemon=True).start()

    def _reset_what_if_labels(self):
        """主定价完成后清掉 what-if 上一次的结果, 让下一轮微扰从干净状态开始."""
        for kind, button_map_attr in (
            ("sigma", "_wf_sigma_buttons"),
            ("S",     "_wf_s_buttons"),
        ):
            button_map = getattr(self, button_map_attr, None)
            if not button_map:
                continue
            for delta, (_btn, var) in button_map.items():
                var.set(self._what_if_base_label(kind, delta))

    def _set_what_if_enabled(self, enabled: bool) -> None:
        """主定价成功后启用 what-if 按钮; 出错或重置时禁用."""
        target = "normal" if enabled else "disabled"
        for attr in ("_wf_sigma_buttons", "_wf_s_buttons"):
            button_map = getattr(self, attr, None)
            if not button_map:
                continue
            for _delta, (btn, _var) in button_map.items():
                btn.configure(state=target)

    # ── 现金流可视化 ────────────────────────────────────────
    def _show_cashflow(self):
        try:
            params = self._collect_params()
        except Exception as exc:
            messagebox.showerror("参数错误", str(exc))
            return
        try:
            pricer = UniversalCBPricer(**params["pricer"])
        except Exception as exc:
            messagebox.showerror("构造失败", str(exc))
            return

        # 现金流序列: 非末期 → 每期票息; 末期 → redemption_price (含末期利息+面值+赎回溢价)
        labels, amounts, kinds = [], [], []
        for p in pricer.coupon_periods:
            if p["is_final"]:
                labels.append(p["end"].isoformat())
                amounts.append(float(pricer.redemption_price))
                kinds.append("到期兑付")
            else:
                labels.append(p["end"].isoformat())
                amounts.append(float(p["coupon_amount"]))
                kinds.append(f"票息 {p['rate']*100:.2f}%")

        if not labels:
            messagebox.showinfo("提示", "没有可显示的现金流")
            return

        win = ctk.CTkToplevel(self)
        win.title(f"现金流: {self.v_bond_code.get() or '未命名'}")
        win.geometry("900x500")
        win.configure(fg_color=BG_APP)
        win.transient(self)

        bg = get_color(BG_CARD)
        bg_in = get_color(BG_INPUT)
        txt = get_color(TEXT)
        txt_dim = get_color(TEXT_DIM)
        brd = get_color(BORDER)
        accent = get_color(ACCENT)
        orange = get_color(ORANGE)

        fig = Figure(figsize=(9, 4.5), dpi=100, facecolor=bg)
        ax = fig.add_subplot(111, facecolor=bg_in)

        x_pos = np.arange(len(labels))
        colors = [orange if k == "到期兑付" else accent for k in kinds]
        bars = ax.bar(x_pos, amounts, color=colors, edgecolor=brd, linewidth=0.5)

        for bar, amt in zip(bars, amounts):
            ax.annotate(f"{amt:.2f}",
                        xy=(bar.get_x() + bar.get_width() / 2, bar.get_height()),
                        xytext=(0, 3), textcoords="offset points",
                        ha="center", fontsize=9, color=txt)

        ax.set_xticks(x_pos)
        ax.set_xticklabels(labels, rotation=30, ha="right", fontsize=9)
        ax.set_ylabel("现金流 (¥/百元面值)", color=txt_dim, fontsize=10)
        ax.set_title(f"{self.v_bond_code.get() or '可转债'} 现金流计划",
                     color=txt, fontsize=13, fontweight="bold")
        ax.tick_params(colors=txt_dim, labelsize=9)
        for spine in ax.spines.values():
            spine.set_color(brd)
        ax.grid(True, axis="y", color=brd, linestyle="--", alpha=0.4)

        from matplotlib.patches import Patch
        legend = ax.legend(handles=[
            Patch(facecolor=accent, label="期间票息"),
            Patch(facecolor=orange, label="到期兑付 (面值+末期利息+溢价)"),
        ], loc="best", framealpha=0.9, facecolor=bg, edgecolor=brd,
            fontsize=9, labelcolor=txt)
        legend.get_frame().set_linewidth(0.5)

        fig.tight_layout()
        canvas = FigureCanvasTkAgg(fig, master=win)
        canvas.draw()
        canvas.get_tk_widget().pack(fill="both", expand=True, padx=12, pady=12)

        total = sum(amounts)
        ctk.CTkLabel(
            win, text=f"现金流合计 (未折现) = {total:.2f}  ·  共 {len(labels)} 笔",
            text_color=TEXT_DIM, font=(FONT_FAMILY, 11)).pack(pady=(0, 10))

    # ── 收敛诊断 (开发者工具, 不绑定到 UI; 可通过 Ctrl+D 快捷键触发) ─────
    def _convergence_check(self):
        btn = getattr(self, "btn_conv", None)
        if btn is not None:
            btn.configure(state="disabled")
        self._start_progress("收敛诊断 (M, N → 2M, 2N)")
        threading.Thread(target=self._convergence_worker, daemon=True).start()

    def _convergence_worker(self):
        try:
            params = self._collect_params()
            pricer = UniversalCBPricer(**params["pricer"])
            m = params["model"]
            theo_a = float(pricer.price(**m))
            m2 = dict(m)
            m2["M"] = m["M"] * 2
            m2["N"] = m["N"] * 2
            theo_b = float(pricer.price(**m2))
            diff = theo_b - theo_a
            rel = abs(diff) / max(abs(theo_b), 1e-9)
            verdict = "已收敛" if rel < 1e-3 else ("基本收敛" if rel < 5e-3 else "未收敛, 建议加密")
            self.after(0, lambda: self.v_status.set(
                f"收敛诊断: M={m['M']},N={m['N']} → {theo_a:.4f}; 翻倍 → {theo_b:.4f}; "
                f"Δ={diff:+.4f} ({rel*100:.3f}%)  [{verdict}]"))
        except Exception as exc:
            self.after(0, self._on_error, f"收敛诊断失败: {exc}")
        finally:
            self.after(0, self._stop_progress)
            btn = getattr(self, "btn_conv", None)
            if btn is not None:
                self.after(0, lambda b=btn: b.configure(state="normal"))


# 让模块级 import 仍能拿到 FONT_MONO/FONT_FAMILY 等 (legacy: 部分回调期望 module 上有)
_ = (FONT_FAMILY, FONT_MONO, BORDER, TEXT, TEXT_DIM, ACCENT, ORANGE, GREEN, RED,
     BG_APP, BG_CARD, BG_INPUT, plt)
