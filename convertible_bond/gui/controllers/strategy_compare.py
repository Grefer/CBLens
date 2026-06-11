"""策略回测 — 多次回测对比 (记录/选择/删除/叠加图).

从 strategy_backtest.py 按职责拆出; 经 StrategyBacktestMixin 聚合混入
CBPricerApp, 方法间通过 self.* 跨 mixin 调用不受拆分影响。
"""
from __future__ import annotations

from tkinter import messagebox

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
from ..widgets import Tooltip

from .strategy_common import STRATEGY_MEDIUM_TABLE_HEIGHT, STRATEGY_SECONDARY_CHART_HEIGHT


class StrategyCompareMixin:
    """策略回测 — 多次回测对比 (记录/选择/删除/叠加图)."""

    def _record_strategy_comparison_result(self, result):
        summary = result.get("summary") or {}
        config = result.get("config") or {}
        try:
            template = self.v_st_template.get()
            view = self.v_st_view.get()
            freq = self.v_st_freq.get()
            top_n = self.v_st_top_n.get()
        except Exception:
            template = "—"
            view = config.get("selection_view", "—")
            freq = config.get("rebalance_freq", "—")
            top_n = config.get("top_n", "—")
        label = (
            f"{template} · {config.get('selection_view') or view} · "
            f"{freq}频 Top{config.get('top_n') or top_n}"
        )
        records = list(getattr(self, "_strategy_compare_results", []) or [])
        key = (
            str(result.get("start_date")),
            str(result.get("end_date")),
            label,
            summary.get("final_equity"),
            summary.get("max_drawdown"),
        )
        snapshot_id = result.get("_snapshot_id")
        if not snapshot_id:
            try:
                snapshot_id = self._strategy_snapshot_dedupe_key({}, result)
            except Exception:
                snapshot_id = None
        records = [
            row for row in records
            if row.get("key") != key and (not snapshot_id or row.get("snapshot_id") != snapshot_id)
        ]
        records.append({
            "key": key,
            "snapshot_id": snapshot_id,
            "label": label,
            "result": result,
            "snapshot_path": result.get("_snapshot_path"),
        })
        self._strategy_compare_results = records[-self._MAX_SNAPSHOTS:]

    def _clear_strategy_comparison(self):
        from tkinter import messagebox
        if not messagebox.askyesno("确认", "清空所有对比记录并删除磁盘快照?"):
            return
        records = list(getattr(self, "_strategy_compare_results", []) or [])
        for record in records:
            self._delete_strategy_snapshot_for_record(record)
        self._delete_strategy_snapshot_path(self._strategy_snapshot_path())
        self._strategy_compare_results = []
        self._clear_active_strategy_backtest_result(status="已清空策略对比和当前回测结果")

    def _render_strategy_comparison(self):
        frame = getattr(self, "strategy_bt_compare_frame", None)
        if frame is None:
            return
        self._clear_strategy_panel(frame)
        records = list(getattr(self, "_strategy_compare_results", []) or [])
        if not records:
            ctk.CTkLabel(
                frame,
                text="运行策略后会自动保留最近 8 次结果, 用于横向比较",
                font=(FONT_FAMILY, 13),
                text_color=TEXT_DIM,
            ).grid(row=0, column=0, padx=12, pady=12, sticky="w")
            return

        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(0, weight=0)
        frame.grid_rowconfigure(1, weight=0)
        frame.grid_rowconfigure(2, weight=1)

        header = ctk.CTkFrame(frame, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=12, pady=(8, 2))
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            header, text="最近策略对比", text_color=TEXT,
            font=(FONT_FAMILY, 14, "bold"),
        ).grid(row=0, column=0, sticky="w")
        self._strategy_compare_selection_text = ctk.StringVar(value="")
        ctk.CTkLabel(
            header, textvariable=self._strategy_compare_selection_text,
            text_color=TEXT_DIM, font=(FONT_FAMILY, 11),
        ).grid(row=0, column=1, sticky="e", padx=(8, 10))
        delete_btn = ctk.CTkButton(
            header, text="删除选中",
            command=lambda: self._delete_selected_comparison(),
            fg_color="transparent", hover_color=get_color(BG_INPUT),
            border_width=1, border_color=RED,
            text_color=RED, font=(FONT_FAMILY, 11, "bold"),
            width=88, height=26, corner_radius=6,
        )
        delete_btn.grid(row=0, column=2, sticky="e")
        self.btn_strategy_compare_delete = delete_btn
        Tooltip(delete_btn, "删除下方表格中选中的对比记录\n支持 Shift / Command 多选")

        rows = []
        best_idx = self._best_strategy_record_index(records)
        col_best = {"ann": [], "ret": [], "excess": [], "sharpe": [], "calmar": []}
        for record in records:
            summary = (record.get("result") or {}).get("summary") or {}
            for key, skey in (("ann", "annualized_return"), ("ret", "total_return"),
                              ("excess", "excess_return"), ("sharpe", "sharpe"),
                              ("calmar", "calmar")):
                v = summary.get(skey)
                col_best[key].append(float(v) if v is not None and np.isfinite(v) else -np.inf)

        for idx, record in enumerate(records, start=1):
            result = record["result"]
            summary = result.get("summary") or {}
            diagnostics = result.get("diagnostics") or {}
            dq = diagnostics.get("data_quality") or {}
            label = record.get("label") or f"策略 {idx}"
            rows.append([
                "★" if idx - 1 == best_idx else "",
                label,
                f"{result.get('start_date')} → {result.get('end_date')}",
                self._fmt_strategy_pct(summary.get("annualized_return"), sign=True),
                self._fmt_strategy_pct(summary.get("total_return"), sign=True),
                self._fmt_strategy_pct(summary.get("excess_return"), sign=True),
                self._fmt_strategy_pct(summary.get("max_drawdown")),
                f"{summary.get('sharpe'):.2f}" if summary.get("sharpe") is not None else "—",
                f"{summary.get('calmar'):.2f}" if summary.get("calmar") is not None else "—",
                self._fmt_strategy_pct(summary.get("avg_turnover")),
                self._fmt_strategy_pct(summary.get("total_cost")),
                self._fmt_strategy_pct(dq.get("current_fallback_ratio")),
            ])

        tree = self._render_strategy_small_tree(
            frame, 2, 0,
            ["best", "label", "period", "ann", "ret", "excess", "dd", "sharpe",
             "calmar", "turnover", "cost", "fallback"],
            ["", "策略", "区间", "年化", "总收益", "超额", "回撤", "Sharpe",
             "Calmar", "换手", "成本", "数据回退"],
            [34, 230, 190, 76, 76, 76, 76, 70, 70, 76, 76, 86],
            rows,
            xscroll=True,
            max_height=STRATEGY_MEDIUM_TABLE_HEIGHT,
            selectmode="extended",
        )
        self._strategy_compare_tree = tree

        # 高亮最优行
        if tree and len(records) >= 2:
            green_c = get_color(GREEN)
            tree.tag_configure("best_row", foreground=green_c)
            if best_idx is not None:
                try:
                    tree.item(str(best_idx), tags=("best_row",))
                except Exception:
                    pass

        if tree:
            selected_iids = self._initial_strategy_compare_selection(tree, records)
            selected_records = self._strategy_compare_records_by_iids(records, selected_iids)
            self._update_strategy_compare_selection_text(len(selected_records), len(records))
            self._render_comparison_overlay_chart(frame, 1, selected_records)

            # 单击选择控制上方图表; 双击加载结果; 右键显示上下文菜单。
            tree.bind("<<TreeviewSelect>>", lambda e: self._on_strategy_compare_selection_change())
            tree.bind("<Double-1>", lambda e: self._load_comparison_record())
            tree.bind("<Button-2>", lambda e: self._show_comparison_context_menu(e))
            # macOS 右键也可能是 <Button-3> 或 <Control-Button-1>
            tree.bind("<Button-3>", lambda e: self._show_comparison_context_menu(e))
            tree.bind("<Control-Button-1>", lambda e: self._show_comparison_context_menu(e))

    @staticmethod
    def _strategy_compare_record_key(record) -> str:
        snapshot_id = record.get("snapshot_id")
        if snapshot_id:
            return f"snapshot:{snapshot_id}"
        key = record.get("key")
        if key is not None:
            return f"key:{key!r}"
        result = record.get("result") or {}
        return (
            f"result:{result.get('start_date')}:{result.get('end_date')}:"
            f"{(result.get('summary') or {}).get('final_equity')}"
        )

    def _initial_strategy_compare_selection(self, tree, records):
        saved_keys = set(getattr(self, "_strategy_compare_selected_keys", set()) or set())
        selected_iids = [
            str(idx) for idx, record in enumerate(records)
            if self._strategy_compare_record_key(record) in saved_keys
        ]
        if not selected_iids:
            default_count = min(2, len(records))
            selected_iids = [str(idx) for idx in range(len(records) - default_count, len(records))]
        if selected_iids:
            tree.selection_set(selected_iids)
            self._strategy_compare_selected_keys = {
                self._strategy_compare_record_key(records[int(iid)])
                for iid in selected_iids
                if iid.isdigit() and int(iid) < len(records)
            }
        return selected_iids

    @staticmethod
    def _strategy_compare_records_by_iids(records, iids):
        selected = []
        seen = set()
        for iid in iids or []:
            try:
                idx = int(iid)
            except (TypeError, ValueError):
                continue
            if idx in seen or not (0 <= idx < len(records)):
                continue
            seen.add(idx)
            selected.append(records[idx])
        return selected

    def _strategy_compare_selected_iids(self):
        tree = getattr(self, "_strategy_compare_tree", None)
        if tree is None:
            return []
        try:
            return list(tree.selection())
        except Exception:
            return []

    def _update_strategy_compare_selection_text(self, selected_count: int, total_count: int) -> None:
        var = getattr(self, "_strategy_compare_selection_text", None)
        if var is not None:
            try:
                var.set(f"已选择 {selected_count} / {total_count}")
            except Exception:
                pass
        btn = getattr(self, "btn_strategy_compare_delete", None)
        if btn is not None:
            try:
                if selected_count:
                    btn.configure(state="normal", text_color=RED, border_color=RED)
                else:
                    btn.configure(state="disabled", text_color=TEXT_DIM, border_color=BORDER)
            except Exception:
                pass

    def _on_strategy_compare_selection_change(self):
        records = list(getattr(self, "_strategy_compare_results", []) or [])
        iids = self._strategy_compare_selected_iids()
        selected_records = self._strategy_compare_records_by_iids(records, iids)
        self._strategy_compare_selected_keys = {
            self._strategy_compare_record_key(record) for record in selected_records
        }
        self._update_strategy_compare_selection_text(len(selected_records), len(records))
        frame = getattr(self, "strategy_bt_compare_frame", None)
        if frame is not None:
            self._render_comparison_overlay_chart(frame, 1, selected_records)

    def _delete_selected_comparison(self):
        tree = getattr(self, "_strategy_compare_tree", None)
        if tree is None:
            return
        sel = tree.selection()
        if not sel:
            return
        records = list(getattr(self, "_strategy_compare_results", []) or [])
        indices = []
        for iid in sel:
            try:
                idx = int(iid)
            except (ValueError, TypeError):
                continue
            if 0 <= idx < len(records):
                indices.append(idx)
        indices = sorted(set(indices), reverse=True)
        if not indices:
            return
        if not self._confirm_delete_selected_comparison(len(indices)):
            return
        deleted_records = []
        for idx in indices:
            record = records.pop(idx)
            deleted_records.append(record)
            # 同时删除磁盘快照文件和可能指向同一结果的 latest 文件。
            self._delete_strategy_snapshot_for_record(record)
            self._delete_latest_strategy_snapshot_if_matches(record.get("snapshot_id"))
        deleted_keys = {self._strategy_compare_record_key(record) for record in deleted_records}
        self._strategy_compare_selected_keys = (
            set(getattr(self, "_strategy_compare_selected_keys", set()) or set()) - deleted_keys
        )
        self._strategy_compare_results = records
        deleted_current = any(
            self._strategy_record_matches_current_result(record) for record in deleted_records
        )
        if deleted_current or not records:
            self._clear_active_strategy_backtest_result(
                status=(
                    f"已删除当前回测结果 · 删除 {len(deleted_records)} 条对比记录 · "
                    f"剩余 {len(records)} 条"
                ))
        else:
            self._mark_strategy_tabs_dirty("对比")
            self._render_strategy_comparison()
            self.v_st_status.set(f"已删除 {len(deleted_records)} 条 · 剩余 {len(records)} 条对比记录")

    @staticmethod
    def _confirm_delete_selected_comparison(count: int) -> bool:
        return messagebox.askyesno(
            "确认删除",
            f"确定删除选中的 {count} 条策略对比记录吗?\n"
            "此操作会同时删除对应磁盘快照, 无法撤销。",
        )

    def _strategy_record_matches_current_result(self, record) -> bool:
        current = getattr(self, "_last_strategy_bt_result", None)
        if not isinstance(current, dict):
            return False
        current_id = current.get("_snapshot_id")
        record_id = record.get("snapshot_id")
        if current_id and record_id:
            return str(current_id) == str(record_id)
        try:
            return self._strategy_snapshot_dedupe_key({}, current) == self._strategy_snapshot_dedupe_key(
                {}, record.get("result") or {})
        except Exception:
            return (record.get("result") is current)

    def _load_comparison_record(self):
        """双击: 把选中的对比记录加载为当前活跃结果."""
        tree = getattr(self, "_strategy_compare_tree", None)
        if tree is None:
            return
        sel = tree.selection()
        if not sel:
            return
        try:
            idx = int(sel[0])
        except (ValueError, IndexError):
            return
        records = list(getattr(self, "_strategy_compare_results", []) or [])
        if 0 <= idx < len(records):
            result = records[idx].get("result")
            if result:
                self._last_strategy_bt_result = result
                self._mark_strategy_tabs_dirty()
                self._update_strategy_result_summary(result)
                label = records[idx].get("label", f"策略 {idx + 1}")
                self.v_st_status.set(f"已加载: {label}")

    def _show_comparison_context_menu(self, event):
        """右键: 显示加载/删除上下文菜单."""
        tree = getattr(self, "_strategy_compare_tree", None)
        if tree is None:
            return
        # 选中点击的行
        row_id = tree.identify_row(event.y)
        if row_id:
            tree.selection_set(row_id)
        else:
            return
        import tkinter as tk
        menu = tk.Menu(tree, tearoff=0)
        menu.add_command(label="加载为当前结果", command=self._load_comparison_record)
        menu.add_separator()
        menu.add_command(label="删除此记录", command=self._delete_selected_comparison)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _render_comparison_overlay_chart(self, frame, grid_row, records):
        """多策略净值叠加折线图."""
        old_fig = getattr(self, "_strategy_bt_compare_fig", None)
        if old_fig is not None:
            old_fig.clf()
            plt.close(old_fig)
            self._strategy_bt_compare_fig = None
        old_frame = getattr(self, "_strategy_compare_chart_frame", None)
        if old_frame is not None:
            try:
                old_frame.destroy()
            except Exception:
                pass

        bg_card_color = get_color(BG_CARD)
        bg_input_color = get_color(BG_INPUT)
        text_dim_color = get_color(TEXT_DIM)
        text_color = get_color(TEXT)
        border_color = get_color(BORDER)
        accent_color = get_color(ACCENT)
        palette = [accent_color, get_color(ORANGE), get_color(GREEN),
                   get_color(RED), "#9b59b6", "#3498db", "#e67e22", "#1abc9c"]

        chart_frame = ctk.CTkFrame(
            frame, fg_color="transparent", height=STRATEGY_SECONDARY_CHART_HEIGHT)
        chart_frame.grid(row=grid_row, column=0, sticky="ew", padx=8, pady=(4, 8))
        chart_frame.grid_columnconfigure(0, weight=1)
        chart_frame.grid_rowconfigure(0, weight=1)
        chart_frame.grid_propagate(False)
        self._strategy_compare_chart_frame = chart_frame

        if not records:
            ctk.CTkLabel(
                chart_frame,
                text="在下方表格选择一条或多条回测记录后显示净值对比",
                text_color=TEXT_DIM,
                font=(FONT_FAMILY, 13),
            ).grid(row=0, column=0, sticky="nsew", padx=12, pady=12)
            return

        fig = Figure(figsize=(10, 2.8), dpi=100, facecolor=bg_card_color)
        ax = fig.add_subplot(111, facecolor=bg_input_color)

        for idx, record in enumerate(records):
            result = record.get("result") or {}
            curve = result.get("equity_curve") or []
            if not curve:
                continue
            dates = [p["date"] for p in curve]
            equity = [float(p["equity"]) for p in curve]
            label = record.get("label") or f"策略 {idx + 1}"
            color = palette[idx % len(palette)]
            lw = 2.2 if idx == len(records) - 1 else 1.4
            ax.plot(dates, equity, color=color, linewidth=lw, label=label[:20],
                    alpha=0.9 if idx == len(records) - 1 else 0.65)

        ax.axhline(1.0, color=border_color, linewidth=0.8, linestyle="--")
        ax.set_ylabel("净值", color=text_dim_color, fontsize=9)
        ax.tick_params(colors=text_dim_color, labelsize=8)
        ax.grid(True, color=border_color, linestyle="--", alpha=0.3)
        for spine in ax.spines.values():
            spine.set_color(border_color)
        leg = ax.legend(loc="best", framealpha=0.9, facecolor=bg_card_color,
                        edgecolor=border_color, fontsize=8, labelcolor=text_color,
                        ncol=min(4, len(records)))
        leg.get_frame().set_linewidth(0.5)
        for lbl in ax.get_xticklabels():
            lbl.set_rotation(15)
            lbl.set_horizontalalignment("right")
        fig.tight_layout()

        canvas = FigureCanvasTkAgg(fig, master=chart_frame)
        canvas.draw()
        canvas.get_tk_widget().grid(row=0, column=0, sticky="nsew")
        self._strategy_bt_compare_fig = fig

    @staticmethod
    def _best_strategy_record_index(records):
        best_idx = None
        best_score = -np.inf
        for idx, record in enumerate(records):
            summary = (record.get("result") or {}).get("summary") or {}
            annualized = summary.get("annualized_return")
            drawdown = summary.get("max_drawdown")
            sharpe = summary.get("sharpe")
            if annualized is None or not np.isfinite(annualized):
                continue
            score = float(annualized)
            if drawdown is not None and np.isfinite(drawdown):
                score -= 0.5 * abs(float(drawdown))
            if sharpe is not None and np.isfinite(sharpe):
                score += 0.03 * float(sharpe)
            if score > best_score:
                best_idx = idx
                best_score = score
        return best_idx
