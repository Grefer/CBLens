"""策略 Tab UI 构建.

布局分四层, 降低控件密度:
  - 决策行: 模板 / 区间 / 预检 / 运行 (4 件套, 常驻)
  - 摘要行: 频率·Top·视图·基准·成本 当前取值 + 编辑切换
  - 二级行 (按模板=自定义自动展开, 否则收起): 频率 / Top / 视图 / 基准 / 成本 bps
  - 高级设置 (默认折叠) 内分两卡:
      · 选债条件: 价格/溢价/偏差/HV (2×2 网格)
      · 标的池/历史口径: 代码池 + 三个历史口径覆盖

行情数据源沿用顶部工具栏统一的"行情源"全局设定; 选债哲学由"视图"统一驱动
(置信度与风险标签按视图推导), "模板"一键套用一整套自洽参数, 把默认可见旋钮压到最少。
"""
import os

import customtkinter as ctk

from ..constants import STRATEGY_SELECTION_VIEWS, STRATEGY_TEMPLATE_NAMES
from ..theme import (
    BG_CARD, BG_INPUT, BORDER, TEXT, TEXT_DIM,
    ACCENT, ACCENT_HOVER, BTN_HOVER, ORANGE,
    FONT_FAMILY, FONT_MONO, E,
)
from ..widgets import CollapsibleSection, Tooltip


def build(app, tab):
    """策略 Tab: 选债策略回测 Pro."""
    tab.grid_columnconfigure(0, weight=1)
    tab.grid_rowconfigure(0, weight=0)
    tab.grid_rowconfigure(1, weight=0)
    tab.grid_rowconfigure(2, weight=0)
    tab.grid_rowconfigure(3, weight=1)

    ctrl = ctk.CTkFrame(tab, fg_color=BG_CARD, corner_radius=16)
    ctrl.grid(row=0, column=0, sticky="ew", pady=(6, 10), padx=6)
    ctrl.grid_columnconfigure(0, weight=1)

    # ── 标题 ──────────────────────────────────────────────
    ch = ctk.CTkFrame(ctrl, fg_color="transparent")
    ch.grid(row=0, column=0, sticky="ew", padx=20, pady=(14, 8))
    ctk.CTkLabel(ch, text=E("🎯 策略"),
                 font=(FONT_FAMILY, 16, "bold"), text_color=TEXT).pack(side="left")
    ctk.CTkLabel(ch, text="PRO",
                 font=(FONT_FAMILY, 10, "bold"), text_color=("#ffffff", "#11111b"),
                 fg_color=ORANGE, corner_radius=5, padx=7, pady=2).pack(side="left", padx=(8, 12))
    ctk.CTkLabel(ch, text="选模板或选视图, 固定频率调仓回测; 细节在「高级设置」里调",
                 font=(FONT_FAMILY, 12), text_color=TEXT_DIM).pack(side="left")

    # ── 决策行 (常驻, 只保留高频项) ────────────────────────
    row1 = ctk.CTkFrame(ctrl, fg_color="transparent")
    row1.grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 6))
    _label(row1, "模板")
    tmpl_menu = ctk.CTkOptionMenu(
        row1, variable=app.v_st_template, values=list(STRATEGY_TEMPLATE_NAMES),
        command=app._apply_strategy_template, width=104,
        font=(FONT_FAMILY, 12), fg_color=BG_INPUT, button_color=BTN_HOVER,
        text_color=TEXT, dropdown_fg_color=BG_INPUT, dropdown_text_color=TEXT)
    tmpl_menu.pack(side="left", padx=(0, 12))
    Tooltip(tmpl_menu, "一键套用一整套自洽参数; 选「自定义」可在下方手动展开二级行调整")

    _label(row1, "开始")
    _entry(row1, app.v_st_start, 100).pack(side="left", padx=(0, 8))
    _label(row1, "结束")
    _entry(row1, app.v_st_end, 100).pack(side="left", padx=(0, 14))

    app.btn_strategy_backtest = ctk.CTkButton(
        row1, text="运行策略", command=app._run_strategy_backtest,
        fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color=("#ffffff", "#11111b"),
        font=(FONT_FAMILY, 13, "bold"), width=96, height=32, corner_radius=6)
    app.btn_strategy_backtest.pack(side="right", padx=(2, 0))
    app.btn_strategy_precheck = ctk.CTkButton(
        row1, text="预检", command=app._precheck_strategy_backtest,
        fg_color=BG_INPUT, hover_color=BTN_HOVER, text_color=TEXT,
        font=(FONT_FAMILY, 12), width=58, height=32, corner_radius=6)
    app.btn_strategy_precheck.pack(side="right", padx=(0, 8))
    Tooltip(app.btn_strategy_precheck, "不跑定价, 先检查代码池、历史口径和预计工作量")

    # ── 摘要行: 当前二级参数 + 编辑切换 ──────────────────
    summary_row = ctk.CTkFrame(ctrl, fg_color="transparent")
    summary_row.grid(row=2, column=0, sticky="ew", padx=20, pady=(0, 4))
    app.v_st_summary = ctk.StringVar(value="")
    ctk.CTkLabel(summary_row, textvariable=app.v_st_summary,
                 text_color=TEXT_DIM, font=(FONT_FAMILY, 12)).pack(side="left")

    # ── 二级行: 频率 / Top / 视图 / 基准 / 成本 ──────────
    sec_row = ctk.CTkFrame(ctrl, fg_color="transparent")
    _label(sec_row, "频率")
    _optmenu(sec_row, app.v_st_freq, ["周", "月", "季"], 64).pack(side="left", padx=(0, 10))
    _label(sec_row, "Top")
    _entry(sec_row, app.v_st_top_n, 50).pack(side="left", padx=(0, 10))
    _label(sec_row, "视图")
    view_menu = _optmenu(sec_row, app.v_st_view, list(STRATEGY_SELECTION_VIEWS), 110)
    view_menu.pack(side="left", padx=(0, 10))
    Tooltip(view_menu, "选债哲学; 置信度与风险标签按视图自动推导, 无需单独设置")
    bench_cb = ctk.CTkCheckBox(
        sec_row, text="基准", variable=app.v_st_benchmark,
        font=(FONT_FAMILY, 12), text_color=TEXT_DIM, fg_color=ACCENT,
        checkbox_width=16, checkbox_height=16, border_width=1, corner_radius=3)
    bench_cb.pack(side="left", padx=(0, 12))
    Tooltip(bench_cb, "等权买下每期全部通过公开交易过滤的转债作为基准, 用于衡量选债超额")
    _label(sec_row, "成本 bps")
    cost_entry = _entry(sec_row, app.v_st_cost, 50)
    cost_entry.pack(side="left")
    Tooltip(cost_entry, "单边换手对应的交易成本 (bps); 区间净收益扣 turnover × 成本")

    app._st_sec_expanded = False

    def _refresh_summary(*_):
        freq = app.v_st_freq.get()
        top = app.v_st_top_n.get()
        view = app.v_st_view.get()
        bench = "含基准" if app.v_st_benchmark.get() else "无基准"
        cost = app.v_st_cost.get().strip() or "0"
        app.v_st_summary.set(f"{freq}度 · Top {top} · {view} · {bench} · 成本 {cost}bps")

    def _toggle_sec(force=None):
        expand = (not app._st_sec_expanded) if force is None else force
        app._st_sec_expanded = expand
        if expand:
            sec_row.grid(row=3, column=0, sticky="ew", padx=20, pady=(0, 8))
            edit_btn.configure(text=E("收起 ▲"))
        else:
            sec_row.grid_remove()
            edit_btn.configure(text=E("编辑 ▾"))

    edit_btn = ctk.CTkButton(
        summary_row, text=E("编辑 ▾"), command=lambda: _toggle_sec(),
        fg_color="transparent", hover_color=BG_INPUT, text_color=TEXT_DIM,
        font=(FONT_FAMILY, 12), width=70, height=24, corner_radius=6)
    edit_btn.pack(side="right")
    Tooltip(edit_btn, "展开/收起 频率·Top·视图·基准·成本")

    for var in (app.v_st_freq, app.v_st_top_n, app.v_st_view,
                app.v_st_benchmark, app.v_st_cost):
        var.trace_add("write", _refresh_summary)

    def _on_template_change(*_):
        _toggle_sec(force=app.v_st_template.get() == "自定义")

    app.v_st_template.trace_add("write", _on_template_change)
    _refresh_summary()
    _on_template_change()

    # ── 高级设置 (默认折叠), 内部三卡 ─────────────────────
    adv = CollapsibleSection(ctrl, "高级设置", expanded=False)
    adv.grid(row=4, column=0, sticky="ew", padx=14, pady=(0, 10))
    body = adv.content

    # 卡 1: 选债条件 — 4 个范围 (2×2 网格)
    _, c1 = _adv_card(body, 0, "选债条件", "在视图基础上叠加; 留空 = 沿用视图默认")
    range_grid = ctk.CTkFrame(c1, fg_color="transparent")
    range_grid.pack(fill="x")
    range_grid.grid_columnconfigure(0, weight=1, uniform="rng")
    range_grid.grid_columnconfigure(1, weight=1, uniform="rng")
    _range_grid_cell(range_grid, 0, 0, "价格", app.v_st_min_price, app.v_st_max_price)
    _range_grid_cell(range_grid, 0, 1, "溢价%", app.v_st_min_premium, app.v_st_max_premium)
    _range_grid_cell(range_grid, 1, 0, "偏差%", app.v_st_min_deviation, app.v_st_max_deviation)
    _range_grid_cell(range_grid, 1, 1, "HV%", app.v_st_min_sigma, app.v_st_max_sigma)

    # 卡 2: 标的池 / 历史口径
    _, c3 = _adv_card(body, 1, "标的池 / 历史口径",
                      "代码池与防未来函数的可选覆盖; 留空即用下方默认")
    pool_row = ctk.CTkFrame(c3, fg_color="transparent")
    pool_row.pack(fill="x", pady=(0, 6))
    _label(pool_row, "代码池")
    code_entry = ctk.CTkEntry(
        pool_row, textvariable=app.v_st_codes, font=(FONT_MONO, 13),
        fg_color=BG_INPUT, border_width=0, corner_radius=6, text_color=TEXT, height=30,
        placeholder_text="留空 = 本地条款库全部转债; 逗号/空格分隔代码")
    code_entry.pack(side="left", fill="x", expand=True, padx=(0, 0))

    _path_override(c3, "历史快照", app.v_st_terms_history_dir,
                   app._choose_strategy_history_dir, "未用 · 按当前条款回测")
    _path_override(c3, "条款修正", app.v_st_terms_patches,
                   app._choose_strategy_patch_file, "默认 · data/cb_terms_patches.json")
    _path_override(c3, "事件表", app.v_st_events_path,
                   app._choose_strategy_events_file, "默认 · data/cb_events.json")

    # ── 状态 / 进度 / 导出 ────────────────────────────────
    status_row = ctk.CTkFrame(tab, fg_color="transparent")
    status_row.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 6))
    status_row.grid_columnconfigure(0, weight=1)
    app.lbl_strategy_bt_status = ctk.CTkLabel(
        status_row, textvariable=app.v_st_status,
        font=(FONT_FAMILY, 12), text_color=TEXT_DIM)
    app.lbl_strategy_bt_status.grid(row=0, column=0, sticky="w")
    app.strategy_bt_progress = ctk.CTkProgressBar(
        status_row, width=180, height=8, corner_radius=4,
        progress_color=ACCENT, fg_color=BG_INPUT)
    app.strategy_bt_progress.set(0)
    app.strategy_bt_progress.grid(row=0, column=1, sticky="e", padx=(12, 8))
    app.btn_strategy_bt_csv = ctk.CTkButton(
        status_row, text="导出CSV", command=app._export_strategy_backtest_csv,
        fg_color=BG_INPUT, hover_color=BTN_HOVER, text_color=TEXT,
        font=(FONT_FAMILY, 12), width=72, height=28, corner_radius=6, state="disabled")
    app.btn_strategy_bt_csv.grid(row=0, column=2, sticky="e")
    app.btn_strategy_compare_clear = ctk.CTkButton(
        status_row, text="清空对比", command=app._clear_strategy_comparison,
        fg_color=BG_INPUT, hover_color=BTN_HOVER, text_color=TEXT_DIM,
        font=(FONT_FAMILY, 12), width=78, height=28, corner_radius=6)
    app.btn_strategy_compare_clear.grid(row=0, column=3, sticky="e", padx=(8, 0))
    app.lbl_strategy_precheck = ctk.CTkLabel(
        status_row, textvariable=app.v_st_precheck,
        font=(FONT_FAMILY, 11), text_color=TEXT_DIM, justify="left")
    app.lbl_strategy_precheck.grid(row=1, column=0, columnspan=4, sticky="w", pady=(4, 0))

    # ── 指标卡 (5 核心 + 5 次级, 两行) ───────────────────
    app._strategy_stat_vars = {}
    app._strategy_stat_labels = {}
    stats_card = ctk.CTkFrame(tab, fg_color=BG_CARD, corner_radius=16)
    stats_card.grid(row=2, column=0, sticky="ew", padx=6, pady=(0, 8))
    for col in range(5):
        stats_card.grid_columnconfigure(col, weight=1, uniform="stbts")

    def _stat(row, col, key, title, *, primary=True):
        var = ctk.StringVar(value="—")
        cell = ctk.CTkFrame(stats_card, fg_color="transparent")
        pady = (10, 4) if row == 0 else (2, 10)
        cell.grid(row=row, column=col, sticky="nsew", padx=8, pady=pady)
        ctk.CTkLabel(cell, text=title, text_color=TEXT_DIM,
                     font=(FONT_FAMILY, 11)).pack(anchor="w")
        size = 16 if primary else 14
        value_lbl = ctk.CTkLabel(cell, textvariable=var, text_color=TEXT,
                                 font=(FONT_FAMILY, size, "bold"))
        value_lbl.pack(anchor="w")
        app._strategy_stat_vars[key] = var
        app._strategy_stat_labels[key] = value_lbl

    _stat(0, 0, "final_equity", "最终净值")
    _stat(0, 1, "total_return", "总收益")
    _stat(0, 2, "annualized", "年化")
    _stat(0, 3, "excess", "超额")
    _stat(0, 4, "max_drawdown", "最大回撤")
    _stat(1, 0, "sharpe", "Sharpe", primary=False)
    _stat(1, 1, "sortino", "Sortino", primary=False)
    _stat(1, 2, "calmar", "Calmar", primary=False)
    _stat(1, 3, "cash", "现金", primary=False)
    _stat(1, 4, "turnover", "换手", primary=False)

    app.strategy_result_tabs = ctk.CTkTabview(
        tab, fg_color=BG_CARD, segmented_button_fg_color=BG_INPUT,
        segmented_button_selected_color=ACCENT,
        segmented_button_selected_hover_color=ACCENT_HOVER,
        segmented_button_unselected_color=BG_INPUT,
        segmented_button_unselected_hover_color=BTN_HOVER,
        text_color=TEXT, corner_radius=16)
    app.strategy_result_tabs.grid(row=3, column=0, sticky="nsew", padx=6, pady=(0, 6))
    for name in ("总览", "筛选", "持仓", "归因", "风险", "稳健性", "数据", "对比"):
        app.strategy_result_tabs.add(name)

    overview_tab = app.strategy_result_tabs.tab("总览")
    overview_tab.grid_columnconfigure(0, weight=1)
    overview_tab.grid_rowconfigure(0, weight=0)
    overview_tab.grid_rowconfigure(1, weight=1)
    app.strategy_bt_insight_frame = ctk.CTkFrame(overview_tab, fg_color="transparent")
    app.strategy_bt_insight_frame.grid(row=0, column=0, sticky="ew", padx=4, pady=(4, 0))
    app.strategy_bt_insight_frame.grid_columnconfigure(0, weight=1)
    app.strategy_bt_chart_frame = ctk.CTkFrame(overview_tab, fg_color="transparent")
    app.strategy_bt_chart_frame.grid(row=1, column=0, sticky="nsew", padx=4, pady=4)
    app.strategy_bt_chart_frame.grid_columnconfigure(0, weight=1)
    app.strategy_bt_chart_frame.grid_rowconfigure(0, weight=1)

    holdings_tab = app.strategy_result_tabs.tab("持仓")
    holdings_tab.grid_columnconfigure(0, weight=1)
    holdings_tab.grid_rowconfigure(0, weight=1)
    app.strategy_bt_table_frame = ctk.CTkFrame(holdings_tab, fg_color="transparent")
    app.strategy_bt_table_frame.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)
    app.strategy_bt_table_frame.grid_columnconfigure(0, weight=1)
    app.strategy_bt_table_frame.grid_rowconfigure(0, weight=1)

    for tab_name, attr in (
        ("筛选", "strategy_bt_selection_frame"),
        ("归因", "strategy_bt_attribution_frame"),
        ("风险", "strategy_bt_risk_frame"),
        ("稳健性", "strategy_bt_robustness_frame"),
        ("数据", "strategy_bt_data_frame"),
        ("对比", "strategy_bt_compare_frame"),
    ):
        pane = app.strategy_result_tabs.tab(tab_name)
        pane.grid_columnconfigure(0, weight=1)
        pane.grid_rowconfigure(0, weight=1)
        frame = ctk.CTkFrame(pane, fg_color="transparent")
        frame.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(0, weight=1)
        setattr(app, attr, frame)


def _label(parent, text):
    ctk.CTkLabel(parent, text=text, text_color=TEXT_DIM,
                 font=(FONT_FAMILY, 13)).pack(side="left", padx=(0, 4))


def _entry(parent, var, width):
    return ctk.CTkEntry(parent, textvariable=var, width=width, font=(FONT_MONO, 13),
                        fg_color=BG_INPUT, border_width=0, corner_radius=6,
                        text_color=TEXT, height=30)


def _optmenu(parent, var, values, width):
    return ctk.CTkOptionMenu(
        parent, variable=var, values=values, width=width,
        font=(FONT_FAMILY, 12), fg_color=BG_INPUT, button_color=BTN_HOVER,
        text_color=TEXT, dropdown_fg_color=BG_INPUT, dropdown_text_color=TEXT)


def _adv_card(parent, row, title, subtitle=None):
    """高级设置子卡: 带浅色边框的分组容器; 返回 (card, body) 元组."""
    card = ctk.CTkFrame(parent, fg_color="transparent", corner_radius=10,
                        border_width=1, border_color=BORDER)
    card.grid(row=row, column=0, sticky="ew", padx=6, pady=(2, 6))
    head = ctk.CTkFrame(card, fg_color="transparent")
    head.pack(fill="x", padx=12, pady=(8, 4))
    ctk.CTkLabel(head, text=title, text_color=ACCENT,
                 font=(FONT_FAMILY, 12, "bold")).pack(side="left", padx=(0, 8))
    if subtitle:
        ctk.CTkLabel(head, text=subtitle, text_color=TEXT_DIM,
                     font=(FONT_FAMILY, 11)).pack(side="left")
    body = ctk.CTkFrame(card, fg_color="transparent")
    body.pack(fill="x", padx=12, pady=(0, 10))
    return card, body


def _range_grid_cell(parent, row, col, label, min_var, max_var, *, width=60):
    cell = ctk.CTkFrame(parent, fg_color="transparent")
    cell.grid(row=row, column=col, sticky="w", pady=2)
    _label(cell, label)
    _entry(cell, min_var, width).pack(side="left")
    ctk.CTkLabel(cell, text="~", text_color=TEXT_DIM,
                 font=(FONT_FAMILY, 12)).pack(side="left", padx=3)
    _entry(cell, max_var, width).pack(side="left")


def _path_override(parent, title, path_var, choose_cmd, default_hint):
    """一行"可选历史口径覆盖": 标题 + 当前取值 + 选择/清除.

    未设置时灰字显示默认行为 (``default_hint``); 设置后显示所选文件/目录名。
    """
    row = ctk.CTkFrame(parent, fg_color="transparent")
    row.pack(fill="x", pady=1)
    ctk.CTkLabel(row, text=title, text_color=TEXT_DIM, font=(FONT_FAMILY, 12),
                 width=64, anchor="w").pack(side="left", padx=(0, 6))
    status = ctk.CTkLabel(row, text="", font=(FONT_MONO, 12), anchor="w")
    status.pack(side="left", padx=(0, 8))

    def refresh(*_):
        raw = path_var.get().strip()
        if raw:
            status.configure(text=os.path.basename(raw.rstrip("/\\")) or raw, text_color=TEXT)
        else:
            status.configure(text=default_hint, text_color=TEXT_DIM)

    refresh()
    path_var.trace_add("write", refresh)
    ctk.CTkButton(row, text="选择", command=choose_cmd,
                  fg_color=BG_INPUT, hover_color=BTN_HOVER, text_color=TEXT,
                  font=(FONT_FAMILY, 12), width=48, height=28, corner_radius=6).pack(side="left", padx=(0, 4))
    ctk.CTkButton(row, text="清除", command=lambda: path_var.set(""),
                  fg_color=BG_INPUT, hover_color=BTN_HOVER, text_color=TEXT_DIM,
                  font=(FONT_FAMILY, 12), width=44, height=28, corner_radius=6).pack(side="left")


