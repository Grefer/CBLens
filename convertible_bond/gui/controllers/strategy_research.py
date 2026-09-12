"""策略研究界面：邻域对比、组合对照与冻结后的样本外复评。"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from datetime import date
import threading

import customtkinter as ctk

from ...atomic_io import atomic_write_json
from ...batch_pricing import AdmissionFilterConfig
from ...market_time import market_today
from ...paths import data_dir, data_path, project_root
from ...strategy_backtest import PDEStrategyConfig
from ...strategy_experiments import (
    capture_provenance, freeze_experiment, list_experiments,
    load_experiment, run_experiment_oos, validate_oos_window,
)
from ...strategy_research import run_strategy_comparison, run_strategy_neighborhood
from ..error_dialogs import prepare_error, show_error
from ..theme import ACCENT, BG_INPUT, BORDER, FONT_FAMILY, TEXT, TEXT_DIM, E
from .strategy_common import StrategyBacktestCancelled, _strategy_snapshot_jsonable


def research_request_from_result(result: dict) -> dict:
    """只复用实际完成的运行设置，不能把当前表单冒充旧结果的配置。"""
    settings = deepcopy(result.get("run_settings") or {})
    config = settings.get("strategy_config")
    codes = (settings.get("pool") or {}).get("bond_codes")
    if not config or not codes or not settings.get("pricing"):
        raise ValueError("这份快照缺少完整运行设置，请先按当前版本重跑一次回测")
    cfg = PDEStrategyConfig(**config)
    if cfg.holding_mode != "top_score" or cfg.rank_signal != "deviation":
        raise ValueError("请先选择一份 PDE 排序结果；候选池等权用于研究对照")
    return {
        "codes": list(codes),
        "config": cfg,
        "admission": AdmissionFilterConfig(**settings["admission_filter"]),
        "params": settings["pricing"],
        "source": settings["data_source"],
        "history_mode": settings["history_mode"],
        "start": _research_date(result["start_date"]),
        "end": _research_date(result["end_date"]),
        "settings": settings,
    }


def _research_date(value) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def strategy_run_provenance() -> dict:
    """运行开始时记录代码与静态数据的指纹，不触发任何行情连接。"""
    return capture_provenance(
        data_paths={name: data_path(name) for name in (
            "cb_data.json", "cb_events.json", "cb_terms_patches.json")},
        source_root=project_root(),
    )


class StrategyResearchMixin:
    """研究操作独立于最近八份结果快照的生命周期。"""

    def _build_strategy_research_panel(self, parent):
        card = ctk.CTkFrame(parent, fg_color=BG_INPUT, corner_radius=10, height=1)
        card.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))
        card.grid_columnconfigure(0, weight=1)
        toolbar = ctk.CTkFrame(card, fg_color="transparent", height=1)
        toolbar.grid(row=0, column=0, sticky="ew", padx=12, pady=(8, 4))
        toolbar.grid_columnconfigure(0, weight=1)
        actions = ctk.CTkFrame(toolbar, fg_color="transparent")
        actions.grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(actions, text="验证当前方案", font=(FONT_FAMILY, 13, "bold"),
                     text_color=TEXT, anchor="w").pack(side="left", padx=(0, 12))
        self._strategy_research_buttons = []
        for text, command in (
            ("稳健性对比", lambda: self._run_strategy_research("neighborhood")),
            ("选债 / 仓位对照", lambda: self._run_strategy_research("comparison")),
            ("冻结当前方案", self._freeze_strategy_experiment),
        ):
            button = ctk.CTkButton(actions, text=text, command=command, height=28,
                                   width=132, fg_color=BORDER, text_color=TEXT,
                                   font=(FONT_FAMILY, 12))
            button.pack(side="left", padx=(0, 8))
            self._strategy_research_buttons.append(button)

        tracking = ctk.CTkFrame(toolbar, fg_color="transparent")
        tracking.grid(row=1, column=0, sticky="w", pady=(6, 0))
        ctk.CTkLabel(tracking, text="样本外方案", text_color=TEXT_DIM,
                     font=(FONT_FAMILY, 12)).pack(side="left", padx=(0, 8))
        self.v_st_experiment = ctk.StringVar(value="暂无冻结方案")
        self._strategy_experiment_menu = ctk.CTkOptionMenu(
            tracking, variable=self.v_st_experiment, values=["暂无冻结方案"],
            command=lambda _: self._show_strategy_experiment_status(),
            width=260, height=28, dynamic_resizing=False,
            fg_color=BORDER, text_color=TEXT,
            font=(FONT_FAMILY, 11))
        self._strategy_experiment_menu.pack(side="left", padx=(0, 8))
        update = ctk.CTkButton(
            tracking, text="更新样本外", command=self._run_strategy_oos,
            width=116, height=28, fg_color=ACCENT, font=(FONT_FAMILY, 12))
        update.pack(side="left")
        self._strategy_research_buttons.append(update)
        self.v_st_research_status = ctk.StringVar(value="以当前回测结果验证；双击对比行可查看并冻结方案。")
        status = ctk.CTkLabel(card, textvariable=self.v_st_research_status,
                              font=(FONT_FAMILY, 11), text_color=TEXT_DIM,
                              anchor="w", justify="left", height=18, width=1,
                              wraplength=600)
        status.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 6))

        def layout_toolbar(_event=None):
            # 按容器宽度换行；Label 的 Configure 还会来自内部文字，不能用它定宽。
            width = card._reverse_widget_scaling(card.winfo_width())
            needed = card._reverse_widget_scaling(
                actions.winfo_reqwidth() + tracking.winfo_reqwidth()) + 40
            inline = width >= needed
            placement = (0, 1) if inline else (1, 0)
            current = tracking.grid_info()
            if (current.get("row"), current.get("column")) != placement:
                tracking.grid_configure(
                    row=placement[0], column=placement[1],
                    sticky="e" if inline else "w",
                    padx=(16, 0) if inline else 0,
                    pady=0 if inline else (6, 0))
            wraplength = max(260, int(width - 24))
            if status.cget("wraplength") != wraplength:
                status.configure(wraplength=wraplength)

        card.bind("<Configure>", layout_toolbar, add="+")
        self.after_idle(layout_toolbar)
        # 空 Frame 默认请求 200px；无结果时不映射，清表后也不能保留旧表高度。
        self._strategy_research_table_frame = ctk.CTkFrame(
            card, fg_color="transparent", height=1)
        self._strategy_research_table_frame.grid_columnconfigure(0, weight=1)
        self.after_idle(self._refresh_strategy_experiments)

    def _refresh_strategy_experiments(self, selected_id=None):
        try:
            experiments = list_experiments(data_dir("strategy_experiments"))
            self._strategy_experiments = {
                f"{item['name']} · v{item['version']} · {item['experiment_id'].rsplit('_', 1)[-1]}": item
                for item in experiments
            }
            labels = list(self._strategy_experiments) or ["暂无冻结方案"]
            self._strategy_experiment_menu.configure(values=labels)
            label = next((key for key, item in self._strategy_experiments.items()
                          if item["experiment_id"] == selected_id), None)
            current = self.v_st_experiment.get()
            self.v_st_experiment.set(label or (current if current in labels else labels[0]))
        except Exception as exc:
            self.v_st_research_status.set(f"读取冻结方案失败：{exc}")

    def _show_strategy_experiment_status(self):
        item = getattr(self, "_strategy_experiments", {}).get(self.v_st_experiment.get())
        if item:
            self.v_st_research_status.set(
                f"冻结于 {item['frozen_date']} · 已记录 {item['record_count']} 期；"
                f"样本外起点 {item['oos_start_date']}。"
                "更新截止日使用上方“结束”日期。")
            frame = self._strategy_research_table_frame
            self._clear_strategy_panel(frame)
            frame.grid_forget()  # CTk 缩放会重放 grid；forget 同时清除这份布局缓存。
            frame.grid_columnconfigure(0, weight=1)
            records = item.get("records") or []
            if records:
                frame.grid(row=2, column=0, sticky="ew", padx=8)
                self._render_strategy_small_tree(
                    frame, 0, 0, ["start", "end", "ret", "equity", "codes"],
                    ["开始", "结束", "区间收益", "累计净值", "实际持仓"],
                    [110, 110, 90, 100, 300],
                    [[r["start_date"], r["end_date"], self._fmt_strategy_pct(r.get("period_return")),
                      f"{r['equity']:.4f}" if r.get("equity") is not None else "—",
                      " / ".join(str(p.get("bond_code")) for p in r["period"].get("positions", []))]
                     for r in records], max_height=min(9, len(records)),
                    xscroll=True, yscroll=len(records) > 9)

    def _research_start(self, text):
        self._strategy_bt_cancel = threading.Event()
        self._strategy_bt_running = True
        self.btn_strategy_backtest.configure(text=E("■ 停止"), command=self._cancel_strategy_backtest)
        for button in getattr(self, "_strategy_research_buttons", []):
            button.configure(state="disabled")
        self.v_st_research_status.set(text)
        self.v_st_status.set(text)

    def _research_finish(self):
        self._finish_strategy_backtest()
        for button in getattr(self, "_strategy_research_buttons", []):
            button.configure(state="normal")

    def _research_cancel_check(self):
        if self._strategy_bt_cancel.is_set():
            raise StrategyBacktestCancelled()

    def _run_strategy_research(self, kind):
        if getattr(self, "_strategy_bt_running", False):
            self.v_st_research_status.set("请等待当前任务完成，或先停止当前任务。")
            return
        try:
            request = research_request_from_result(getattr(self, "_last_strategy_bt_result", None) or {})
        except (KeyError, TypeError, ValueError) as exc:
            self.v_st_research_status.set(str(exc))
            return
        self._research_start("正在准备研究对比；首次定价完成后复用同一面板。")
        threading.Thread(target=self._strategy_research_worker,
                         args=(kind, request), daemon=True).start()

    def _strategy_research_worker(self, kind, request):
        provider = None
        try:
            provenance = strategy_run_provenance()
            provider = self._build_strategy_provider(request["source"], history_mode=request["history_mode"])
            def progress(done, total):
                self._research_cancel_check()
                self.after(0, self.v_st_research_status.set, f"对比方案 {done}/{total}")
            def stage(stage, done, total, period_idx, total_periods):
                self._research_cancel_check()
                self.after(0, self.v_st_status.set,
                           f"{stage} {done}/{total} · 第 {period_idx + 1}/{total_periods} 期")
            runner = run_strategy_neighborhood if kind == "neighborhood" else run_strategy_comparison
            report = runner(
                provider, request["codes"], start_date=request["start"], end_date=request["end"],
                base_config=request["config"], admission_config=request["admission"],
                pricing_snapshot_cache=getattr(self, "_strategy_pricing_cache", None),
                progress_cb=progress, stage_cb=stage, cancel_cb=self._research_cancel_check,
                **request["params"],
            )
            for result in report["results"].values():
                settings = deepcopy(request["settings"])
                # 每个变体保存真正执行的配置，不沿用被比较基线的阈值。
                settings["strategy_config"] = {
                    key: result["config"].get(key, value)
                    for key, value in asdict(request["config"]).items()
                }
                settings["strategy"].update(settings["strategy_config"])
                settings["provenance"] = provenance
                result["run_settings"] = settings
                result["config"]["history_mode"] = request["history_mode"]
            path = data_dir("strategy_research_results") / f"{kind}_latest.json"
            atomic_write_json(path, _strategy_snapshot_jsonable(report))
            self.after(0, self._show_strategy_research_report, report)
        except StrategyBacktestCancelled:
            self.after(0, self.v_st_research_status.set, "研究对比已停止，已取得的数据保留在缓存中。")
        except Exception as exc:
            self.after(0, show_error, self, "研究对比失败", prepare_error(exc), self.v_st_research_status)
        finally:
            if provider is not None:
                try:
                    provider.flush()
                except Exception:
                    pass
            self.after(0, self._research_finish)

    def _show_strategy_research_report(self, report):
        self._last_strategy_research_report = report
        frame = self._strategy_research_table_frame
        self._clear_strategy_panel(frame)
        frame.grid_forget()
        frame.grid_columnconfigure(0, weight=1)
        rows = [[row["name"], self._fmt_strategy_pct(row.get("total_return")),
                 self._fmt_strategy_pct(row.get("max_drawdown")),
                 f"{row['sharpe']:.2f}" if row.get("sharpe") is not None else "—",
                 self._fmt_strategy_pct(row.get("avg_turnover")),
                 self._fmt_strategy_pct(row.get("holding_overlap"))]
                for row in report["variants"]]
        tree = None
        if rows:
            frame.grid(row=2, column=0, sticky="ew", padx=8)
            tree = self._render_strategy_small_tree(
                frame, 0, 0, ["name", "ret", "dd", "sharpe", "turnover", "overlap"],
                ["方案", "总收益", "回撤", "Sharpe", "换手", "持仓重合"],
                [260, 90, 90, 80, 90, 100], rows, max_height=min(9, len(rows)),
                xscroll=True, yscroll=len(rows) > 9)
        if tree:
            tree.bind("<Double-1>", lambda _: self._activate_strategy_research_variant(tree))
        deflated = report.get("deflated") or {}
        probability = deflated.get("probability", deflated.get("dsr"))
        note = (f"多重检验校正概率 {probability:.1%}。" if isinstance(probability, (float, int))
                else "多重检验校正结果随报告保存。")
        self.v_st_research_status.set(
            f"已完成 {len(rows)} 组对比。{note}持仓重合以 PDE 基线为参照；双击查看方案。")
        self.v_st_status.set("研究对比完成 · 结果已保存")

    def _activate_strategy_research_variant(self, tree):
        selection = tree.selection()
        if not selection:
            return
        report = self._last_strategy_research_report
        row = report["variants"][int(selection[0])]
        result = report["results"].get(row["name"])
        if result:
            self._handle_strategy_backtest_success(result)
            self.v_st_research_status.set(f"当前结果：{row['name']}。冻结操作将使用这份结果的实际配置。")

    def _freeze_strategy_experiment(self):
        if getattr(self, "_strategy_bt_running", False):
            return
        try:
            result = getattr(self, "_last_strategy_bt_result", None) or {}
            request = research_request_from_result(result)
            cfg = request["config"]
            manifest = freeze_experiment(
                data_dir("strategy_experiments"),
                name=f"估值偏差·{cfg.rebalance_freq}频·Top{cfg.top_n}·{market_today()}",
                config=asdict(cfg), engine_params={
                    "pricing": request["params"], "admission_filter": asdict(request["admission"]),
                    "source": request["source"], "history_mode": request["history_mode"],
                    "pool": request["settings"]["pool"],
                }, bond_codes=request["codes"], result=result,
                provenance=request["settings"].get("provenance"),
                parent_id=getattr(self, "_strategy_experiments", {}).get(
                    self.v_st_experiment.get(), {}).get("experiment_id"),
            )
            self._refresh_strategy_experiments(manifest["experiment_id"])
            self._show_strategy_experiment_status()
        except Exception as exc:
            self.v_st_research_status.set(f"冻结方案失败：{exc}")

    def _run_strategy_oos(self):
        if getattr(self, "_strategy_bt_running", False):
            return
        item = getattr(self, "_strategy_experiments", {}).get(self.v_st_experiment.get())
        if not item:
            self.v_st_research_status.set("请先冻结一份已完成回测的方案。")
            return
        try:
            manifest = load_experiment(item["path"])
            end = date.fromisoformat(self.v_st_end.get().strip())
            frozen = date.fromisoformat(manifest["frozen_date"])
            if end <= frozen or end >= market_today():
                raise ValueError("结束日期须晚于冻结日且早于今天，并落在完整调仓边界。")
            validate_oos_window(manifest, end)
        except Exception as exc:
            self.v_st_research_status.set(str(exc))
            return
        self._research_start("正在复评冻结方案；已记录的历史区间不会被覆盖。")
        threading.Thread(target=self._strategy_oos_worker,
                         args=(item["path"], manifest, end), daemon=True).start()

    def _strategy_oos_worker(self, path, manifest, end):
        provider = None
        try:
            engine = manifest["engine_params"]
            provider = self._build_strategy_provider(engine["source"], history_mode=engine["history_mode"])
            report = run_experiment_oos(
                path, provider, end_date=end, cancel_cb=self._research_cancel_check,
                provenance=strategy_run_provenance(),
            )
            self.after(0, self._handle_strategy_oos_success, report)
        except StrategyBacktestCancelled:
            self.after(0, self.v_st_research_status.set, "样本外复评已停止。")
        except Exception as exc:
            self.after(0, show_error, self, "样本外复评失败", prepare_error(exc), self.v_st_research_status)
        finally:
            if provider is not None:
                try:
                    provider.flush()
                except Exception:
                    pass
            self.after(0, self._research_finish)

    def _handle_strategy_oos_success(self, report):
        self._refresh_strategy_experiments()
        self._show_strategy_experiment_status()
        self.v_st_research_status.set(f"样本外已追加 {report['appended_periods']} 个区间；历史记录保持原样。")
        result = report.get("result") or {}
        summary = result.get("summary") or {}
        self.v_st_status.set(
            f"样本外复评完成 · 累计收益 {self._fmt_strategy_pct(summary.get('total_return'))} · "
            f"最大回撤 {self._fmt_strategy_pct(summary.get('max_drawdown'))}")
