"""策略 Tab UI 构建.

布局设计：
  - 标题栏与操作区：将标题、描述与“预检”、“运行策略”按钮合并至单行
  - 核心参数网格：采用 2×4 的网格布局常驻展示所有 8 个基本参数，美观对齐，标签在上，输入框在下
  - 联动逻辑：修改核心参数时，模板类型将自动联动切换至“自定义”
  - 指标看板：指标数据展示重构为 10 个独立的 Dashboard Tile（卡片磁贴）
  - 高级设置（默认折叠）内分两卡：
      · 选债条件：价格/溢价/偏差/HV (2×2 网格)，并带有“不限”灰色占位符
      · 标的池/历史口径：代码池及三个历史数据覆盖，采用清晰的按钮右对齐布局
"""
import os

import customtkinter as ctk

from ..constants import (
    STRATEGY_HISTORY_DESCRIPTIONS,
    STRATEGY_HISTORY_MODES,
    STRATEGY_POOL_DESCRIPTIONS,
    STRATEGY_POOL_MODES,
    STRATEGY_SELECTION_VIEWS,
    STRATEGY_STAT_TOOLTIPS,
    STRATEGY_TEMPLATE_DESCRIPTIONS,
    STRATEGY_TEMPLATE_NAMES,
    STRATEGY_VIEW_DESCRIPTIONS,
)
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

    # ── 标题与操作栏合并 (首屏高聚合) ──────────────────────────────────
    ch = ctk.CTkFrame(ctrl, fg_color="transparent")
    ch.grid(row=0, column=0, sticky="ew", padx=20, pady=(14, 8))
    ch.grid_columnconfigure(0, weight=1)
    ch.grid_columnconfigure(1, weight=0)

    # 左侧标题与描述
    title_box = ctk.CTkFrame(ch, fg_color="transparent")
    title_box.grid(row=0, column=0, sticky="w")
    ctk.CTkLabel(title_box, text=E("🎯 策略"),
                 font=(FONT_FAMILY, 16, "bold"), text_color=TEXT).pack(side="left")
    ctk.CTkLabel(title_box, text="PRO",
                 font=(FONT_FAMILY, 10, "bold"), text_color=("#ffffff", "#11111b"),
                 fg_color=ORANGE, corner_radius=5, padx=7, pady=2).pack(side="left", padx=(8, 12))
    ctk.CTkLabel(title_box, text="选模板或选视图, 固定频率调仓回测; 细节在「高级设置」里调",
                 font=(FONT_FAMILY, 12), text_color=TEXT_DIM).pack(side="left")

    # 右侧操作按钮
    btn_box = ctk.CTkFrame(ch, fg_color="transparent")
    btn_box.grid(row=0, column=1, sticky="e")

    app.btn_strategy_precheck = ctk.CTkButton(
        btn_box, text="预检", command=app._precheck_strategy_backtest,
        fg_color=BG_INPUT, hover_color=BTN_HOVER, text_color=TEXT,
        font=(FONT_FAMILY, 12), width=58, height=32, corner_radius=6)
    app.btn_strategy_precheck.pack(side="left", padx=(0, 8))
    Tooltip(app.btn_strategy_precheck, "不跑定价, 先检查代码池、历史口径和预计工作量")

    app.btn_strategy_backtest = ctk.CTkButton(
        btn_box, text="运行策略", command=app._run_strategy_backtest,
        fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color=("#ffffff", "#11111b"),
        font=(FONT_FAMILY, 13, "bold"), width=96, height=32, corner_radius=6)
    app.btn_strategy_backtest.pack(side="left")
    Tooltip(app.btn_strategy_backtest, "按当前模板、视图和过滤条件做固定频率调仓回测")

    # ── 核心参数设置网格 (2×4 干净规整) ────────────────────────────────────
    cc = ctk.CTkFrame(ctrl, fg_color="transparent")
    cc.grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 12))
    for col in range(4):
        cc.grid_columnconfigure(col, weight=1, uniform="st_cols")

    def _grid_cell(parent, label, var, row, col, widget_type="entry", values=None,
                   tooltip=None, command=None):
        cell = ctk.CTkFrame(parent, fg_color="transparent")
        cell.grid(row=row, column=col, sticky="ew", padx=8, pady=6)

        lbl = ctk.CTkLabel(cell, text=label, text_color=TEXT_DIM,
                           font=(FONT_FAMILY, 12), anchor="w")
        lbl.pack(anchor="w", pady=(0, 2))

        if widget_type == "entry":
            w = ctk.CTkEntry(cell, textvariable=var, font=(FONT_MONO, 13),
                             fg_color=BG_INPUT, border_width=0, corner_radius=6,
                             text_color=TEXT, height=28)
            w.pack(fill="x", expand=True)
        elif widget_type == "optmenu":
            menu_kwargs = {}
            if command is not None:
                menu_kwargs["command"] = command
            w = ctk.CTkOptionMenu(
                cell, variable=var, values=values or [], height=28,
                font=(FONT_FAMILY, 12), fg_color=BG_INPUT, button_color=BTN_HOVER,
                text_color=TEXT, dropdown_fg_color=BG_INPUT, dropdown_text_color=TEXT,
                **menu_kwargs)
            w.pack(fill="x", expand=True)
        elif widget_type == "checkbox":
            w = ctk.CTkCheckBox(
                cell, text="等权基准对标", variable=var, height=28,
                font=(FONT_FAMILY, 12), text_color=TEXT_DIM, fg_color=ACCENT,
                checkbox_width=16, checkbox_height=16, border_width=1, corner_radius=3)
            w.pack(anchor="w", pady=(2, 0))

        if tooltip:
            Tooltip(lbl, tooltip)
            Tooltip(w, tooltip)
        return w

    # 第一行参数 (模板, 开始日期, 结束日期, 调仓频率)
    _grid_cell(
        cc, "模板", app.v_st_template, 0, 0, "optmenu", list(STRATEGY_TEMPLATE_NAMES),
        lambda: STRATEGY_TEMPLATE_DESCRIPTIONS.get(app.v_st_template.get(), ""),
        command=app._apply_strategy_template)
    _grid_cell(cc, "开始日期", app.v_st_start, 0, 1, "entry", None, "回测的起始日期 (YYYY-MM-DD)")
    _grid_cell(cc, "结束日期", app.v_st_end, 0, 2, "entry", None, "回测的结束日期 (YYYY-MM-DD)")
    _grid_cell(cc, "调仓频率", app.v_st_freq, 0, 3, "optmenu", ["周", "月", "季"], "策略定期调仓重组的频率")

    # 第二行参数 (选债视图, Top选中数, 交易成本, 基准设置)
    _grid_cell(
        cc, "选债视图", app.v_st_view, 1, 0, "optmenu", list(STRATEGY_SELECTION_VIEWS),
        lambda: STRATEGY_VIEW_DESCRIPTIONS.get(app.v_st_view.get(), ""),
        command=app._describe_strategy_view)
    _grid_cell(cc, "Top N 选中数", app.v_st_top_n, 1, 1, "entry", None, "每期最大持仓转债数量")
    _grid_cell(cc, "交易成本 (bps)", app.v_st_cost, 1, 2, "entry", None, "单边调仓交易成本，单位为万分之一(bps)")
    _grid_cell(cc, "基准设置", app.v_st_benchmark, 1, 3, "checkbox", None, "等权买入全市场合格转债作为比较基准")

    app.v_st_hint = ctk.StringVar(value="")
    hint_lbl = ctk.CTkLabel(
        ctrl, textvariable=app.v_st_hint,
        font=(FONT_FAMILY, 12), text_color=TEXT_DIM,
        justify="left", wraplength=1120)
    hint_lbl.grid(row=2, column=0, sticky="ew", padx=28, pady=(0, 10))

    def _refresh_choice_hint(*_):
        template = app.v_st_template.get()
        view = app.v_st_view.get()
        app.v_st_hint.set(
            f"模板: {STRATEGY_TEMPLATE_DESCRIPTIONS.get(template, template)}  ·  "
            f"视图: {STRATEGY_VIEW_DESCRIPTIONS.get(view, view)}"
        )

    for var in (app.v_st_template, app.v_st_view, app.v_st_freq,
                app.v_st_top_n, app.v_st_cost, app.v_st_benchmark):
        var.trace_add("write", _refresh_choice_hint)
    _refresh_choice_hint()

    # ── 核心参数联动逻辑 (手动修改时模板自动切“自定义”) ──────────────────────────
    def _on_param_change(*_):
        if not getattr(app, "_programmatic_update", False):
            app.v_st_template.set("自定义")

    for var in (app.v_st_start, app.v_st_end, app.v_st_freq,
                app.v_st_view, app.v_st_top_n, app.v_st_cost,
                app.v_st_benchmark):
        var.trace_add("write", _on_param_change)

    # ── 回测范围与可信度: 把工程路径收进模式选择里 ─────────────────────
    scope = ctk.CTkFrame(ctrl, fg_color="transparent", corner_radius=10,
                         border_width=1, border_color=BORDER)
    scope.grid(row=3, column=0, sticky="ew", padx=20, pady=(0, 10))
    scope.grid_columnconfigure(0, weight=1)
    head = ctk.CTkFrame(scope, fg_color="transparent")
    head.grid(row=0, column=0, sticky="ew", padx=12, pady=(8, 4))
    ctk.CTkLabel(head, text="回测范围与可信度", text_color=ACCENT,
                 font=(FONT_FAMILY, 12, "bold")).pack(side="left", padx=(0, 8))
    ctk.CTkLabel(
        head, text="先选回测哪些债, 再选历史条款口径; 路径配置只在自定义模式展开",
        text_color=TEXT_DIM, font=(FONT_FAMILY, 11)).pack(side="left")

    scope_body = ctk.CTkFrame(scope, fg_color="transparent")
    scope_body.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 10))
    scope_body.grid_columnconfigure(0, weight=1, uniform="scope")
    scope_body.grid_columnconfigure(1, weight=1, uniform="scope")

    pool_box = ctk.CTkFrame(scope_body, fg_color="transparent")
    pool_box.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
    pool_title = ctk.CTkLabel(pool_box, text="回测范围", text_color=TEXT,
                              font=(FONT_FAMILY, 13, "bold"))
    pool_title.pack(anchor="w")
    Tooltip(pool_title, lambda: STRATEGY_POOL_DESCRIPTIONS.get(app.v_st_pool_mode.get(), ""))
    pool_seg = ctk.CTkSegmentedButton(
        pool_box, variable=app.v_st_pool_mode, values=list(STRATEGY_POOL_MODES),
        command=lambda _v: app._refresh_strategy_setup_summary(),
        font=(FONT_FAMILY, 12), height=28,
        selected_color=ACCENT, selected_hover_color=ACCENT_HOVER,
        unselected_color=BG_INPUT, unselected_hover_color=BTN_HOVER,
        text_color=TEXT, corner_radius=6)
    pool_seg.pack(fill="x", pady=(6, 4))

    app.v_st_pool_summary = ctk.StringVar(value="")
    pool_summary = ctk.CTkLabel(
        pool_box, textvariable=app.v_st_pool_summary,
        font=(FONT_FAMILY, 11), text_color=TEXT_DIM,
        justify="left", wraplength=520)
    pool_summary.pack(anchor="w", fill="x")
    Tooltip(pool_summary, lambda: STRATEGY_POOL_DESCRIPTIONS.get(app.v_st_pool_mode.get(), ""))

    manual_box = ctk.CTkFrame(pool_box, fg_color="transparent")
    codes_text = ctk.CTkTextbox(
        manual_box, height=62, font=(FONT_MONO, 12),
        fg_color=BG_INPUT, border_width=0, corner_radius=6,
        text_color=TEXT, wrap="word")
    codes_text.pack(fill="x", pady=(6, 4))

    def _sync_codes_from_box(_event=None):
        app.v_st_codes.set(codes_text.get("1.0", "end").strip())

    def _sync_codes_to_box(*_):
        raw = app.v_st_codes.get()
        current = codes_text.get("1.0", "end").strip()
        if current != raw:
            codes_text.delete("1.0", "end")
            if raw:
                codes_text.insert("1.0", raw)

    codes_text.bind("<KeyRelease>", _sync_codes_from_box)
    app.v_st_codes.trace_add("write", _sync_codes_to_box)
    _sync_codes_to_box()

    manual_actions = ctk.CTkFrame(manual_box, fg_color="transparent")
    manual_actions.pack(fill="x")
    ctk.CTkButton(
        manual_actions, text="导入文件", command=app._import_strategy_codes_file,
        fg_color=BG_INPUT, hover_color=BTN_HOVER, text_color=TEXT,
        font=(FONT_FAMILY, 12), width=76, height=28, corner_radius=6).pack(side="left")
    ctk.CTkButton(
        manual_actions, text="校验", command=app._refresh_strategy_setup_summary,
        fg_color=BG_INPUT, hover_color=BTN_HOVER, text_color=TEXT,
        font=(FONT_FAMILY, 12), width=52, height=28, corner_radius=6).pack(side="left", padx=(6, 0))
    ctk.CTkButton(
        manual_actions, text="清空", command=app._clear_strategy_codes,
        fg_color=BG_INPUT, hover_color=BTN_HOVER, text_color=TEXT_DIM,
        font=(FONT_FAMILY, 12), width=52, height=28, corner_radius=6).pack(side="left", padx=(6, 0))

    history_box = ctk.CTkFrame(scope_body, fg_color="transparent")
    history_box.grid(row=0, column=1, sticky="nsew", padx=(10, 0))
    history_title = ctk.CTkLabel(history_box, text="历史口径", text_color=TEXT,
                                 font=(FONT_FAMILY, 13, "bold"))
    history_title.pack(anchor="w")
    Tooltip(history_title, lambda: STRATEGY_HISTORY_DESCRIPTIONS.get(app.v_st_history_mode.get(), ""))
    history_seg = ctk.CTkSegmentedButton(
        history_box, variable=app.v_st_history_mode, values=list(STRATEGY_HISTORY_MODES),
        command=lambda _v: app._refresh_strategy_setup_summary(),
        font=(FONT_FAMILY, 12), height=28,
        selected_color=ACCENT, selected_hover_color=ACCENT_HOVER,
        unselected_color=BG_INPUT, unselected_hover_color=BTN_HOVER,
        text_color=TEXT, corner_radius=6)
    history_seg.pack(fill="x", pady=(6, 4))

    app.v_st_history_summary = ctk.StringVar(value="")
    history_summary = ctk.CTkLabel(
        history_box, textvariable=app.v_st_history_summary,
        font=(FONT_FAMILY, 11), text_color=TEXT_DIM,
        justify="left", wraplength=520)
    history_summary.pack(anchor="w", fill="x")
    Tooltip(history_summary, lambda: STRATEGY_HISTORY_DESCRIPTIONS.get(app.v_st_history_mode.get(), ""))

    custom_files = ctk.CTkFrame(history_box, fg_color="transparent")
    _path_override(
        custom_files, "历史条款快照", app.v_st_terms_history_dir,
        app._choose_strategy_history_dir, "未用 · 按当前条款回看过去",
        "保存各日期 cb_data 快照的目录; 启用后回测日只能看到当时或之前的条款")
    _path_override(
        custom_files, "历史转股价修正", app.v_st_terms_patches,
        app._choose_strategy_patch_file, "默认 · data/cb_terms_patches.json",
        "修正历史转股价、强赎等条款变化, 用来降低未来信息偏差")
    _path_override(
        custom_files, "公告事件表", app.v_st_events_path,
        app._choose_strategy_events_file, "默认 · data/cb_events.json",
        "公告事件表会在对应日期应用下修、强赎、回售等事件")

    def _refresh_scope_visibility(*_):
        if app.v_st_pool_mode.get() == "自选代码":
            manual_box.pack(fill="x", pady=(2, 0))
        else:
            manual_box.pack_forget()
        if app.v_st_history_mode.get() == "自定义文件":
            custom_files.pack(fill="x", pady=(6, 0))
        else:
            custom_files.pack_forget()
        app._refresh_strategy_setup_summary()

    for var in (
        app.v_st_pool_mode, app.v_st_history_mode, app.v_st_codes,
        app.v_st_terms_history_dir, app.v_st_terms_patches, app.v_st_events_path,
    ):
        var.trace_add("write", _refresh_scope_visibility)
    _refresh_scope_visibility()

    # ── 高级设置 (默认折叠) ─────────────────────
    adv = CollapsibleSection(ctrl, "高级设置", expanded=False)
    adv.grid(row=4, column=0, sticky="ew", padx=14, pady=(0, 10))
    body = adv.content

    # 卡 1: 选债条件 — 4 个范围 (2×2 网格)
    _, c1 = _adv_card(body, 0, "选债条件", "在视图基础上叠加; 留空 = 沿用视图默认")
    range_grid = ctk.CTkFrame(c1, fg_color="transparent")
    range_grid.pack(fill="x")
    range_grid.grid_columnconfigure(0, weight=1, uniform="rng")
    range_grid.grid_columnconfigure(1, weight=1, uniform="rng")
    _range_grid_cell(
        range_grid, 0, 0, "价格", app.v_st_min_price, app.v_st_max_price,
        tooltip="转债市价范围; 留空表示不限制")
    _range_grid_cell(
        range_grid, 0, 1, "溢价%", app.v_st_min_premium, app.v_st_max_premium,
        tooltip="转股溢价率 = 市价 / 转股价值 - 1; 负值代表转股折价")
    _range_grid_cell(
        range_grid, 1, 0, "偏差%", app.v_st_min_deviation, app.v_st_max_deviation,
        tooltip="模型偏差 = (市价 - 理论价) / 理论价; 负值越大代表越低估")
    _range_grid_cell(
        range_grid, 1, 1, "HV%", app.v_st_min_sigma, app.v_st_max_sigma,
        tooltip="正股历史波动率; 使用顶部选择的波动率窗口")

    # ── 状态 / 进度 / 导出 ────────────────────────────────
    status_row = ctk.CTkFrame(tab, fg_color="transparent")
    status_row.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 6))
    status_row.grid_columnconfigure(0, weight=1)
    app.lbl_strategy_bt_status = ctk.CTkLabel(
        status_row, textvariable=app.v_st_status,
        font=(FONT_FAMILY, 12), text_color=TEXT_DIM,
        justify="left", wraplength=760)
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
    Tooltip(app.btn_strategy_bt_csv, "导出逐期摘要、日频净值、持仓明细和汇总指标")
    app.btn_strategy_compare_clear = ctk.CTkButton(
        status_row, text="清空对比", command=app._clear_strategy_comparison,
        fg_color=BG_INPUT, hover_color=BTN_HOVER, text_color=TEXT_DIM,
        font=(FONT_FAMILY, 12), width=78, height=28, corner_radius=6)
    app.btn_strategy_compare_clear.grid(row=0, column=3, sticky="e", padx=(8, 0))
    Tooltip(app.btn_strategy_compare_clear, "清除最近 8 次策略结果对比记录")
    app.lbl_strategy_precheck = ctk.CTkLabel(
        status_row, textvariable=app.v_st_precheck,
        font=(FONT_FAMILY, 11), text_color=TEXT_DIM, justify="left",
        wraplength=1120)
    app.lbl_strategy_precheck.grid(row=1, column=0, columnspan=4, sticky="w", pady=(4, 0))

    # ── 指标卡 Dashboard Tiles (独立卡片化，轻盈美观) ───────────────────
    app._strategy_stat_vars = {}
    app._strategy_stat_labels = {}
    stats_card = ctk.CTkFrame(tab, fg_color="transparent")
    stats_card.grid(row=2, column=0, sticky="ew", padx=6, pady=(0, 8))
    for col in range(5):
        stats_card.grid_columnconfigure(col, weight=1, uniform="stbts")

    def _stat(row, col, key, title, *, primary=True):
        var = ctk.StringVar(value="—")
        cell = ctk.CTkFrame(stats_card, fg_color=BG_CARD, corner_radius=12, border_width=1, border_color=BORDER)
        pady = (8, 4) if row == 0 else (2, 8)
        cell.grid(row=row, column=col, sticky="nsew", padx=4, pady=pady)

        inner = ctk.CTkFrame(cell, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=12, pady=8)

        title_lbl = ctk.CTkLabel(inner, text=title, text_color=TEXT_DIM,
                                 font=(FONT_FAMILY, 11, "bold"))
        title_lbl.pack(anchor="w")
        size = 18 if primary else 15
        value_lbl = ctk.CTkLabel(inner, textvariable=var, text_color=TEXT,
                                 font=(FONT_FAMILY, size, "bold"))
        value_lbl.pack(anchor="w", pady=(2, 0))
        tooltip = STRATEGY_STAT_TOOLTIPS.get(key)
        if tooltip:
            Tooltip(cell, tooltip)
            Tooltip(title_lbl, tooltip)
            Tooltip(value_lbl, tooltip)

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


def _range_grid_cell(parent, row, col, label, min_var, max_var, *, width=70, tooltip=None):
    """微调后的范围过滤单元：增加灰色“不限”占位符"""
    cell = ctk.CTkFrame(parent, fg_color="transparent")
    cell.grid(row=row, column=col, sticky="w", pady=4, padx=6)

    lbl = ctk.CTkLabel(cell, text=label, text_color=TEXT_DIM, font=(FONT_FAMILY, 12, "bold"),
                       width=54, anchor="w")
    lbl.pack(side="left", padx=(0, 6))

    ent_min = ctk.CTkEntry(cell, textvariable=min_var, width=width, font=(FONT_MONO, 13),
                           fg_color=BG_INPUT, border_width=0, corner_radius=6,
                           text_color=TEXT, height=28, placeholder_text="不限")
    ent_min.pack(side="left")

    ctk.CTkLabel(cell, text="~", text_color=TEXT_DIM,
                 font=(FONT_FAMILY, 12)).pack(side="left", padx=4)

    ent_max = ctk.CTkEntry(cell, textvariable=max_var, width=width, font=(FONT_MONO, 13),
                           fg_color=BG_INPUT, border_width=0, corner_radius=6,
                           text_color=TEXT, height=28, placeholder_text="不限")
    ent_max.pack(side="left")
    if tooltip:
        Tooltip(lbl, tooltip)
        Tooltip(ent_min, tooltip)
        Tooltip(ent_max, tooltip)


def _path_override(parent, title, path_var, choose_cmd, default_hint, tooltip=None):
    """优化后的历史口径覆盖行：路径靠左自适应填充，按钮统一对齐靠右"""
    row = ctk.CTkFrame(parent, fg_color="transparent")
    row.pack(fill="x", pady=2)

    lbl = ctk.CTkLabel(row, text=title, text_color=TEXT_DIM, font=(FONT_FAMILY, 12),
                       width=112, anchor="w")
    lbl.pack(side="left", padx=(0, 8))

    status = ctk.CTkLabel(row, text="", font=(FONT_MONO, 11), anchor="w")
    status.pack(side="left", fill="x", expand=True)

    def refresh(*_):
        raw = path_var.get().strip()
        if raw:
            status.configure(text=os.path.basename(raw.rstrip("/\\")) or raw, text_color=TEXT)
        else:
            status.configure(text=default_hint, text_color=TEXT_DIM)

    refresh()
    path_var.trace_add("write", refresh)

    clear_btn = ctk.CTkButton(row, text="清除", command=lambda: path_var.set(""),
                              fg_color=BG_INPUT, hover_color=BTN_HOVER, text_color=TEXT_DIM,
                              font=(FONT_FAMILY, 12), width=44, height=28, corner_radius=6)
    clear_btn.pack(side="right", padx=(4, 0))

    choose_btn = ctk.CTkButton(row, text="选择", command=choose_cmd,
                               fg_color=BG_INPUT, hover_color=BTN_HOVER, text_color=TEXT,
                               font=(FONT_FAMILY, 12), width=48, height=28, corner_radius=6)
    choose_btn.pack(side="right")
    if tooltip:
        Tooltip(lbl, tooltip)
        Tooltip(status, tooltip)
        Tooltip(choose_btn, tooltip)
        Tooltip(clear_btn, "清除这个可选覆盖, 回到默认口径")
