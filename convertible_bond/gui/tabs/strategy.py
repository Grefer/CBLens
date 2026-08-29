"""策略 Tab UI 构建.

默认只展示策略、区间、调仓频率、持仓数和数据模式。代码池、筛选条件、
模型假设与交易口径统一收进参数设置，结果按概览/持仓/诊断/对比组织。
"""

import customtkinter as ctk

from ..constants import (
    STRATEGY_HISTORY_DESCRIPTIONS,
    STRATEGY_POOL_DESCRIPTIONS,
    STRATEGY_POOL_MODES,
    STRATEGY_STAT_TOOLTIPS,
)
from ..theme import (
    BG_CARD, BG_INPUT, BORDER, TEXT, TEXT_DIM,
    ACCENT, ACCENT_HOVER, BTN_HOVER, ORANGE, RED,
    FONT_FAMILY, FONT_MONO, VOL_WINDOW_MAP, E,
)
from ..widgets import CollapsibleSection, Tooltip, make_date_picker


def build(app, tab):
    """策略 Tab: 定期调仓回测."""
    tab.grid_columnconfigure(0, weight=1)
    tab.grid_rowconfigure(0, weight=0)  # 控制卡
    tab.grid_rowconfigure(1, weight=0)  # 结果操作栏
    tab.grid_rowconfigure(2, weight=1)  # 结果 Tabview

    # ── 外边沿对齐 ──────────────────────────────────────────
    # 各主要卡片组件 (ctrl, stats_card, strategy_result_tabs) 外边沿统一对齐在 16px 处。
    ctrl = ctk.CTkFrame(tab, fg_color=BG_CARD, corner_radius=12)
    ctrl.grid(row=0, column=0, sticky="ew", pady=(6, 6), padx=16)
    ctrl.grid_columnconfigure(0, weight=1)

    # ── 标题 ──────────────────────────────────────────────
    ch = ctk.CTkFrame(ctrl, fg_color="transparent")
    ch.grid(row=0, column=0, sticky="ew", padx=14, pady=(8, 2))
    ch.grid_columnconfigure(0, weight=1)

    title_box = ctk.CTkFrame(ch, fg_color="transparent")
    title_box.grid(row=0, column=0, sticky="w")
    ctk.CTkLabel(title_box, text=E("🎯 策略回测"),
                 font=(FONT_FAMILY, 15, "bold"), text_color=TEXT).pack(side="left")

    # ── 策略选择 ───────────────────────────────────────────
    strategy_bar = ctk.CTkFrame(ctrl, fg_color="transparent")
    strategy_bar.grid(row=1, column=0, sticky="ew", padx=14, pady=(2, 3))
    strategy_bar.grid_columnconfigure(2, weight=1)

    ctk.CTkLabel(
        strategy_bar, text="策略", text_color=TEXT_DIM,
        font=(FONT_FAMILY, 13), width=52, anchor="w",
    ).grid(row=0, column=0, sticky="w")

    app.v_st_template_display = ctk.StringVar(value="下修机会")
    app.v_st_params_state = ctk.StringVar(value="")
    strategy_description = ctk.StringVar(value="")

    def _strategy_display_name():
        return "估值偏差" if app.v_st_template.get() == "估值偏差" else "下修机会"

    def _sync_strategy_display(*_):
        display = _strategy_display_name()
        if app.v_st_template_display.get() != display:
            app.v_st_template_display.set(display)
        description = (
            "优先选择市价低于模型理论价的转债"
            if display == "估值偏差"
            else "优先选择下修价值被低估且情景扰动后仍有优势的转债"
        )
        strategy_description.set(description)

    def _select_strategy(display):
        app._apply_strategy_template(
            "估值偏差" if display == "估值偏差" else "下修错定价"
        )
        app.v_st_params_state.set("")
        _sync_strategy_display()

    strategy_switch = ctk.CTkSegmentedButton(
        strategy_bar, variable=app.v_st_template_display,
        values=["下修机会", "估值偏差"], command=_select_strategy,
        width=210, height=28, corner_radius=7,
        font=(FONT_FAMILY, 12, "bold"),
        fg_color=BG_INPUT, selected_color=ACCENT,
        selected_hover_color=ACCENT_HOVER,
        unselected_color=BG_INPUT, unselected_hover_color=BTN_HOVER,
        text_color=TEXT,
    )
    strategy_switch.grid(row=0, column=1, sticky="w", padx=(8, 14))
    ctk.CTkLabel(
        strategy_bar, textvariable=strategy_description,
        text_color=TEXT_DIM, font=(FONT_FAMILY, 12), anchor="w",
    ).grid(row=0, column=2, sticky="ew")
    ctk.CTkLabel(
        strategy_bar, textvariable=app.v_st_params_state,
        text_color=ORANGE, font=(FONT_FAMILY, 11, "bold"),
    ).grid(row=0, column=3, sticky="e", padx=(10, 6))
    reset_btn = ctk.CTkButton(
        strategy_bar, text=E("↺"), command=lambda: _select_strategy(_strategy_display_name()),
        width=30, height=28, corner_radius=7, fg_color="transparent",
        hover_color=BTN_HOVER, text_color=TEXT_DIM, font=(FONT_FAMILY, 15),
    )
    reset_btn.grid(row=0, column=4, sticky="e")
    Tooltip(reset_btn, "恢复当前策略默认参数")
    app.v_st_template.trace_add("write", _sync_strategy_display)
    _sync_strategy_display()

    # ── 核心设置 ───────────────────────────────────────────
    cc = ctk.CTkFrame(ctrl, fg_color="transparent")
    cc.grid(row=2, column=0, sticky="ew", padx=6, pady=(0, 4))
    for col in range(5):
        cc.grid_columnconfigure(col, weight=1, uniform="st_cols")

    def _grid_cell(parent, label, var, row, col, widget_type="entry", values=None,
                   tooltip=None, command=None, control_width=140, label_width=60,
                   cell_pady=2, checkbox_text="等权基准对标"):
        """Inline label cell: label 左(定宽) + control 右, 同列控件左边缘对齐."""
        cell = ctk.CTkFrame(parent, fg_color="transparent")
        cell.grid(row=row, column=col, sticky="ew", padx=8, pady=cell_pady)
        cell.grid_columnconfigure(0, minsize=label_width)
        cell.grid_columnconfigure(1, weight=0)

        if widget_type == "checkbox":
            ctk.CTkLabel(cell, text=label, text_color=TEXT_DIM,
                         font=(FONT_FAMILY, 13), width=label_width,
                         anchor="w").grid(row=0, column=0, sticky="w", padx=(0, 8))
            w = ctk.CTkCheckBox(
                cell, text=checkbox_text, variable=var, height=26,
                font=(FONT_FAMILY, 12), text_color=TEXT_DIM, fg_color=ACCENT,
                hover_color=ACCENT_HOVER, border_color=BORDER,
                checkbox_width=16, checkbox_height=16, border_width=1, corner_radius=3)
            w.grid(row=0, column=1, sticky="w")
            if tooltip:
                Tooltip(w, tooltip)
            return w

        lbl = ctk.CTkLabel(cell, text=label, text_color=TEXT_DIM,
                           font=(FONT_FAMILY, 13),
                           width=label_width, anchor="w")
        lbl.grid(row=0, column=0, sticky="w", padx=(0, 8))

        if widget_type == "entry":
            w = ctk.CTkEntry(cell, textvariable=var, font=(FONT_MONO, 13),
                             fg_color=BG_INPUT, border_width=0,
                             corner_radius=6, text_color=TEXT, height=26,
                             width=control_width)
            w.grid(row=0, column=1, sticky="w")
        elif widget_type == "date":
            w = make_date_picker(cell, var, entry_width=control_width)
            w.grid(row=0, column=1, sticky="w")
        elif widget_type == "optmenu":
            menu_kwargs = {"width": control_width}
            if command is not None:
                menu_kwargs["command"] = command
            w = ctk.CTkOptionMenu(
                cell, variable=var, values=values or [], height=26,
                font=(FONT_FAMILY, 12), fg_color=BORDER, button_color=BTN_HOVER,
                text_color=TEXT, dropdown_fg_color=BG_INPUT, dropdown_text_color=TEXT,
                **menu_kwargs)
            w.grid(row=0, column=1, sticky="w")

        if tooltip:
            Tooltip(lbl, tooltip)
            Tooltip(w, tooltip)
        return w

    _grid_cell(cc, "开始", app.v_st_start, 0, 0, "date", None,
               "回测起始日",
               control_width=116, label_width=40)
    _grid_cell(cc, "结束", app.v_st_end, 0, 1, "date", None,
               "回测结束日",
               control_width=116, label_width=40)
    _grid_cell(
        cc, "调仓", app.v_st_freq, 0, 2, "optmenu", ["周", "月", "季"],
        "定期调仓的时间间隔", control_width=88, label_width=40)
    _grid_cell(
        cc, "持仓数", app.v_st_top_n, 0, 3, "entry", None,
        "每期按策略信号选取的最大持仓数\n候选不足或缺成交价时保留现金",
        control_width=88, label_width=52)

    data_cell = ctk.CTkFrame(cc, fg_color="transparent")
    data_cell.grid(row=0, column=4, sticky="ew", padx=8, pady=2)
    data_cell.grid_columnconfigure(0, minsize=52)
    data_mode_label = ctk.CTkLabel(
        data_cell, text="数据模式", text_color=TEXT_DIM,
        font=(FONT_FAMILY, 13), width=52, anchor="w",
    )
    data_mode_label.grid(row=0, column=0, sticky="w", padx=(0, 8))
    app.v_st_history_display = ctk.StringVar(value="Wind 历史")

    def _sync_history_display(*_):
        display = "快速验证" if app.v_st_history_mode.get() == "标准" else "Wind 历史"
        if app.v_st_history_display.get() != display:
            app.v_st_history_display.set(display)

    def _select_history(display):
        app.v_st_history_mode.set("标准" if display == "快速验证" else "Wind高保真")
        app._refresh_strategy_setup_summary()

    history_switch = ctk.CTkSegmentedButton(
        data_cell, variable=app.v_st_history_display,
        values=["快速验证", "Wind 历史"], command=_select_history,
        width=150, height=28, corner_radius=7,
        font=(FONT_FAMILY, 11), fg_color=BG_INPUT,
        selected_color=ACCENT, selected_hover_color=ACCENT_HOVER,
        unselected_color=BG_INPUT, unselected_hover_color=BTN_HOVER,
        text_color=TEXT,
    )
    history_switch.grid(row=0, column=1, sticky="w")
    Tooltip(
        data_mode_label,
        lambda: STRATEGY_HISTORY_DESCRIPTIONS.get(app.v_st_history_mode.get(), ""),
    )
    app.v_st_history_mode.trace_add("write", _sync_history_display)
    _sync_history_display()

    # 当前逻辑仍保留为内部状态，供预检、预设和快照使用。
    app.v_st_logic_summary = ctk.StringVar(value="")

    # 手动调参不再伪装成第三种策略，只标记当前模板已调整。
    def _on_param_change(*_):
        if not getattr(app, "_programmatic_update", False):
            app.v_st_params_state.set("参数已调整")

    editable_strategy_vars = (
        app.v_st_freq, app.v_st_top_n, app.v_st_cost,
        app.v_st_cash_yield, app.v_st_exposure, app.v_st_min_reset_edge,
        app.v_st_r, app.v_st_spread, app.v_st_distress_k, app.v_st_p_down,
        app.v_st_vol_window, app.v_st_sigma_band, app.v_st_spread_band_bps,
        app.v_st_event_exit, app.v_st_min_price, app.v_st_max_price,
        app.v_st_min_premium, app.v_st_max_premium,
        app.v_st_min_deviation, app.v_st_max_deviation,
        app.v_st_min_sigma, app.v_st_max_sigma,
    )
    for var in editable_strategy_vars:
        var.trace_add("write", _on_param_change)

    def _sync_selection_logic(*_):
        app.v_st_logic_summary.set(app._strategy_logic_summary_text())

    for var in editable_strategy_vars:
        var.trace_add("write", _sync_selection_logic)
    _sync_selection_logic()

    # 基准是回测解释的一部分，固定计算并从主界面移除。
    app.v_st_benchmark.set(True)

    # 标的范围/数据模式状态变量 (仅标的范围在参数设置中显示; 两个 StringVar 由
    # _refresh_strategy_setup_summary 维护, 供控制器逻辑读取, 不再常驻 UI)
    app.v_st_pool_summary = ctk.StringVar(value="")
    app.v_st_history_summary = ctk.StringVar(value="")

    # 自选代码输入: 仅当回测范围 = 自选代码 时, 在 ctrl 中显示
    manual_box = ctk.CTkFrame(ctrl, fg_color="transparent")
    manual_box.grid_columnconfigure(0, weight=1)
    manual_box.grid_columnconfigure(1, weight=0)

    codes_text = ctk.CTkTextbox(
        manual_box, height=62, font=(FONT_MONO, 12),
        fg_color=BG_INPUT, border_width=0, corner_radius=6,
        text_color=TEXT, wrap="word")
    codes_text.grid(row=0, column=0, sticky="ew", pady=0, padx=(0, 12))

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
    manual_actions.grid(row=0, column=1, sticky="ns")

    ctk.CTkButton(
        manual_actions, text=E("📥 导入"), command=app._import_strategy_codes_file,
        fg_color=BG_INPUT, hover_color=BTN_HOVER, text_color=TEXT,
        font=(FONT_FAMILY, 12), width=76, height=24, corner_radius=6).pack(side="top", pady=1)
    ctk.CTkButton(
        manual_actions, text=E("🔍 校验"), command=app._refresh_strategy_setup_summary,
        fg_color=BG_INPUT, hover_color=BTN_HOVER, text_color=TEXT,
        font=(FONT_FAMILY, 12), width=76, height=24, corner_radius=6).pack(side="top", pady=1)
    ctk.CTkButton(
        manual_actions, text=E("🗑 清空"), command=app._clear_strategy_codes,
        fg_color=BG_INPUT, hover_color=BTN_HOVER, text_color=TEXT_DIM,
        font=(FONT_FAMILY, 12), width=76, height=24, corner_radius=6).pack(side="top", pady=1)

    def _refresh_scope_visibility(*_):
        if app.v_st_pool_mode.get() == "自选代码":
            manual_box.grid(row=3, column=0, sticky="ew", padx=14, pady=(0, 8))
        else:
            manual_box.grid_forget()
        app._refresh_strategy_setup_summary()

    for var in (app.v_st_pool_mode, app.v_st_history_mode, app.v_st_codes):
        var.trace_add("write", _refresh_scope_visibility)
    _refresh_scope_visibility()

    # ── 参数设置 ───────────────────────────────────────────
    adv_shell = ctk.CTkFrame(ctrl, fg_color=BG_INPUT, corner_radius=10)
    adv_shell.grid(row=4, column=0, sticky="ew", padx=14, pady=(0, 6))
    adv_shell.grid_columnconfigure(0, weight=1)

    adv = CollapsibleSection(adv_shell, "参数设置", expanded=False)
    adv.grid(row=0, column=0, sticky="ew", padx=12, pady=(2, 5))
    adv.header_btn.grid_configure(sticky="w")
    adv.header_btn.configure(
        fg_color="transparent",
        hover_color=BG_INPUT,
        corner_radius=8,
        height=24,
        width=118,
    )
    body = adv.content
    body.grid_columnconfigure(0, weight=1)

    adv_panel = ctk.CTkFrame(body, fg_color="transparent")
    adv_panel.grid(row=0, column=0, sticky="ew")
    adv_panel.grid_columnconfigure(0, weight=1, uniform="adv-panel")
    adv_panel.grid_columnconfigure(1, weight=1, uniform="adv-panel")

    # 左: 标的范围
    _, c0 = _adv_card(adv_panel, 0, "标的范围", col=0)
    scope_grid = ctk.CTkFrame(c0, fg_color="transparent")
    scope_grid.pack(fill="x")
    scope_grid.grid_columnconfigure(0, weight=1)
    _grid_cell(
        scope_grid, "转债范围", app.v_st_pool_mode, 0, 0, "optmenu",
        list(STRATEGY_POOL_MODES),
        lambda: STRATEGY_POOL_DESCRIPTIONS.get(app.v_st_pool_mode.get(), ""),
        command=lambda _v: app._refresh_strategy_setup_summary(),
        control_width=160, label_width=64, cell_pady=2)
    # 右: 选债条件 — 4 个范围 (2×2 网格)
    _, c1 = _adv_card(adv_panel, 0, "选债限制", col=1)
    range_grid = ctk.CTkFrame(c1, fg_color="transparent")
    range_grid.pack(fill="x")
    range_grid.grid_columnconfigure(0, weight=1, uniform="rng")
    range_grid.grid_columnconfigure(1, weight=1, uniform="rng")
    _range_grid_cell(
        range_grid, 0, 0, "价格", app.v_st_min_price, app.v_st_max_price,
        tooltip="转债市价区间 (元)\n留空 = 不限制")
    _range_grid_cell(
        range_grid, 0, 1, "溢价%", app.v_st_min_premium, app.v_st_max_premium,
        tooltip="转股溢价率 = 市价 / 转股价值 − 1\n负值 = 转股折价")
    _range_grid_cell(
        range_grid, 1, 0, "偏差%", app.v_st_min_deviation, app.v_st_max_deviation,
        tooltip="模型偏差 = (市价 − 理论价) / 理论价\n负 = 市价低于理论价 (越负越便宜)")
    _range_grid_cell(
        range_grid, 1, 1, "HV%", app.v_st_min_sigma, app.v_st_max_sigma,
        tooltip="正股历史波动率\n窗口跟随顶部 σ 设置")

    tune_card, c2 = _adv_card(
        adv_panel, 1, "模型与交易",
        "仅下修策略使用的参数会随策略自动显示",
        col=0, columnspan=2)
    tune_card.grid_configure(pady=(8, 0))
    tune_grid = ctk.CTkFrame(c2, fg_color="transparent")
    tune_grid.pack(fill="x")
    for _c in range(4):
        tune_grid.grid_columnconfigure(_c, weight=1, uniform="tune")

    _grid_cell(
        tune_grid, "HV窗口", app.v_st_vol_window, 0, 0, "optmenu",
        list(VOL_WINDOW_MAP), "正股历史波动率的观测窗口",
        control_width=92, label_width=76)
    _grid_cell(
        tune_grid, "无风险%", app.v_st_r, 0, 1, "entry", None,
        "定价模型漂移与现金收益的基准年化利率",
        control_width=92, label_width=76)
    _grid_cell(
        tune_grid, "信用利差%", app.v_st_spread, 0, 2, "entry", None,
        "模型折现使用 r + credit_spread(S)",
        control_width=92, label_width=76)
    _grid_cell(
        tune_grid, "困境斜率%", app.v_st_distress_k, 0, 3, "entry", None,
        "正股低于转股价时信用利差的附加斜率",
        control_width=92, label_width=82)

    _grid_cell(
        tune_grid, "现金收益%", app.v_st_cash_yield, 1, 0, "entry", None,
        "未满持仓数、缺价、仓位缩放或事件退出后的现金年化收益",
        control_width=92, label_width=76)
    _grid_cell(
        tune_grid, "仓位口径", app.v_st_exposure, 1, 1, "optmenu",
        ["恒定满仓", "估值缩放"],
        "恒定满仓: 可投资部分按持仓数等权\n"
        "估值缩放: 按全市场估值偏差中位数缩放总仓位",
        control_width=92, label_width=76)
    _grid_cell(
        tune_grid, "成本bp", app.v_st_cost, 1, 2, "entry", None,
        "单边调仓交易成本; 基准同口径计成本",
        control_width=92, label_width=76)

    p_down_entry = _grid_cell(
        tune_grid, "年化下修%", app.v_st_p_down, 2, 0, "entry", None,
        "事件模型给定的年化下修强度 p_down\n下修策略会与市场隐含强度比较",
        control_width=92, label_width=76)
    sigma_band_entry = _grid_cell(
        tune_grid, "HV扰动%", app.v_st_sigma_band, 2, 1, "entry", None,
        "稳健下修优势在 HV ±该比例的情景中取最差值",
        control_width=92, label_width=76)
    spread_band_entry = _grid_cell(
        tune_grid, "利差扰动bp", app.v_st_spread_band_bps, 2, 2, "entry", None,
        "稳健下修优势在基准信用利差 ±该 bp 的情景中取最差值",
        control_width=92, label_width=82)
    min_reset_edge_entry = _grid_cell(
        tune_grid, "最低优势元", app.v_st_min_reset_edge, 2, 3, "entry", None,
        "下修优势最低金额\n0 = 只要求模型优势为正",
        control_width=92, label_width=82)
    event_exit_check = _grid_cell(
        tune_grid, "事件退出", app.v_st_event_exit, 3, 0, "checkbox", None,
        "下修提议/通过/拒绝公告后，在下一可得收盘价退出并持有现金",
        label_width=76, checkbox_text="公告后退出")

    def _sync_reset_edge_input(*_):
        enabled = "下修优势" in app.v_st_rank_signal.get()
        down_reset_cells = (
            p_down_entry.master,
            sigma_band_entry.master,
            spread_band_entry.master,
            min_reset_edge_entry.master,
            event_exit_check.master,
        )
        for cell in down_reset_cells:
            if enabled:
                cell.grid()
            else:
                cell.grid_remove()

    app.v_st_rank_signal.trace_add("write", _sync_reset_edge_input)
    _sync_reset_edge_input()

    # ── 执行状态栏 ────────────────────────────────────────
    console = ctk.CTkFrame(ctrl, fg_color=BG_INPUT, corner_radius=10)
    console.grid(row=5, column=0, sticky="ew", padx=14, pady=(0, 10))
    console.grid_columnconfigure(0, weight=1)
    console.grid_columnconfigure(1, weight=0)

    status_box = ctk.CTkFrame(console, fg_color="transparent")
    status_box.grid(row=0, column=0, sticky="ew", padx=12, pady=(7, 5))
    status_box.grid_columnconfigure(0, weight=1)

    app.lbl_strategy_bt_status = ctk.CTkLabel(
        status_box, textvariable=app.v_st_status,
        font=(FONT_FAMILY, 12, "bold"), text_color=TEXT,
        anchor="w", justify="left")
    app.lbl_strategy_bt_status.grid(row=0, column=0, sticky="ew")

    app.lbl_strategy_precheck = ctk.CTkLabel(
        status_box, textvariable=app.v_st_precheck,
        font=(FONT_FAMILY, 11), text_color=TEXT_DIM,
        anchor="w", justify="left", wraplength=720)
    app.lbl_strategy_precheck.grid(row=1, column=0, sticky="ew", pady=(2, 0))

    def _sync_precheck_visibility(*_):
        if app.v_st_precheck.get().strip():
            app.lbl_strategy_precheck.grid()
        else:
            app.lbl_strategy_precheck.grid_remove()

    app.v_st_precheck.trace_add("write", _sync_precheck_visibility)
    _sync_precheck_visibility()

    def _bind_meta_wrap(container):
        def _on_resize(event):
            width = max(320, event.width)
            app.lbl_strategy_precheck.configure(wraplength=max(300, width - 8))
        container.bind("<Configure>", _on_resize)

    _bind_meta_wrap(status_box)

    action_box = ctk.CTkFrame(console, fg_color="transparent")
    action_box.grid(row=0, column=1, sticky="e", padx=(0, 12), pady=(7, 5))
    BTN_H, BTN_R = 30, 7

    app.btn_strategy_backtest = ctk.CTkButton(
        action_box, text=E("▶ 运行回测"), command=app._run_strategy_backtest,
        fg_color=ACCENT, hover_color=ACCENT_HOVER,
        text_color=("#ffffff", "#11111b"),
        font=(FONT_FAMILY, 13, "bold"), width=126, height=BTN_H, corner_radius=BTN_R)
    app.btn_strategy_backtest.pack(side="left")
    Tooltip(app.btn_strategy_backtest, "开始回测；运行中可停止")

    # 进度条 (底部填充)
    app.strategy_bt_progress = ctk.CTkProgressBar(
        console, height=3, corner_radius=2,
        progress_color=ACCENT, fg_color=BG_CARD)
    app.strategy_bt_progress.set(0)
    app.strategy_bt_progress.grid(row=1, column=0, columnspan=2,
                                  sticky="ew", padx=12, pady=(0, 6))

    # ── 结果操作 ───────────────────────────────────────────
    result_bar = ctk.CTkFrame(tab, fg_color="transparent")
    result_bar.grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 2))
    result_bar.grid_columnconfigure(0, weight=1)
    ctk.CTkLabel(
        result_bar, text="回测结果", text_color=TEXT_DIM,
        font=(FONT_FAMILY, 12, "bold"),
    ).grid(row=0, column=0, sticky="w")

    result_actions = ctk.CTkFrame(result_bar, fg_color="transparent")
    result_actions.grid(row=0, column=1, sticky="e")
    app.btn_strategy_bt_csv = ctk.CTkButton(
        result_actions, text=E("↓"), command=app._export_strategy_backtest_csv,
        fg_color="transparent", border_width=1, border_color=BORDER,
        hover_color=BTN_HOVER, text_color=TEXT,
        font=(FONT_FAMILY, 15, "bold"), width=32, height=28, corner_radius=7,
        state="disabled")
    app.btn_strategy_bt_csv.pack(side="left", padx=(0, 6))
    Tooltip(app.btn_strategy_bt_csv, "导出当前结果为 CSV")

    app.btn_strategy_compare_clear = ctk.CTkButton(
        result_actions, text=E("🗑"), command=app._clear_strategy_comparison,
        fg_color="transparent", hover_color=BTN_HOVER, text_color=RED,
        font=(FONT_FAMILY, 13), width=32, height=28, corner_radius=7)
    app.btn_strategy_compare_clear.pack(side="left")
    Tooltip(app.btn_strategy_compare_clear, "清空当前结果、对比记录和本地快照")

    # ── 结果页 ─────────────────────────────────────────────
    app.strategy_result_tabs = ctk.CTkTabview(
        tab, fg_color=BG_CARD, segmented_button_fg_color=BG_INPUT,
        segmented_button_selected_color=ACCENT,
        segmented_button_selected_hover_color=ACCENT_HOVER,
        segmented_button_unselected_color=BG_INPUT,
        segmented_button_unselected_hover_color=BTN_HOVER,
        text_color=TEXT, corner_radius=12,
        command=lambda: app._on_strategy_result_tab_change())
    app.strategy_result_tabs.grid(row=2, column=0, sticky="nsew", padx=16, pady=(0, 6))
    for name in ("概览", "持仓", "诊断", "对比"):
        app.strategy_result_tabs.add(name)

    # ── 所有子页统一使用 ScrollableFrame ─────────────────────────
    def _scrollable_pane(tab_name):
        pane = app.strategy_result_tabs.tab(tab_name)
        pane.grid_columnconfigure(0, weight=1)
        pane.grid_rowconfigure(0, weight=1)
        scroll = ctk.CTkScrollableFrame(pane, fg_color="transparent")
        scroll.grid(row=0, column=0, sticky="nsew", padx=0, pady=0)
        scroll.grid_columnconfigure(0, weight=1)
        inner = ctk.CTkFrame(scroll, fg_color="transparent")
        inner.pack(fill="both", expand=True)
        inner.grid_columnconfigure(0, weight=1)
        return inner

    overview_inner = _scrollable_pane("概览")
    overview_inner.grid_rowconfigure(2, weight=1)

    # ── 核心指标 ───────────────────────────────────────────
    app._strategy_stat_vars = {}
    app._strategy_stat_labels = {}
    stats_card = ctk.CTkFrame(overview_inner, fg_color="transparent")
    stats_card.grid(row=0, column=0, sticky="ew", padx=8, pady=(6, 2))
    for col in range(5):
        stats_card.grid_columnconfigure(col, weight=1, uniform="stbts")
    stats_card.grid_rowconfigure(0, weight=1)

    def _stat(row, col, key, title, *, primary=True):
        var = ctk.StringVar(value="—")
        # 主指标赋予主题色边框高亮
        border_c = ACCENT if primary else BORDER
        cell = ctk.CTkFrame(stats_card, fg_color=BG_CARD, corner_radius=8, border_width=1, border_color=border_c)
        cell.grid(row=row, column=col, sticky="nsew", padx=4, pady=(2, 4))

        inner = ctk.CTkFrame(cell, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=9, pady=4)

        title_lbl = ctk.CTkLabel(inner, text=title, text_color=TEXT_DIM,
                                 font=(FONT_FAMILY, 10, "bold"))
        title_lbl.pack(anchor="w")

        size = 16 if primary else 14
        value_lbl = ctk.CTkLabel(inner, textvariable=var, text_color=TEXT,
                                 font=(FONT_MONO, size, "bold"))
        value_lbl.pack(anchor="w", pady=(2, 0))

        tooltip = STRATEGY_STAT_TOOLTIPS.get(key)
        if tooltip:
            Tooltip(cell, tooltip)
            Tooltip(title_lbl, tooltip)
            Tooltip(value_lbl, tooltip)

        app._strategy_stat_vars[key] = var
        app._strategy_stat_labels[key] = value_lbl

    _stat(0, 0, "total_return", "总收益")
    _stat(0, 1, "annualized", "年化收益")
    _stat(0, 2, "excess", "超额收益")
    _stat(0, 3, "max_drawdown", "最大回撤")
    _stat(0, 4, "sharpe", "Sharpe")

    app.strategy_bt_insight_frame = ctk.CTkFrame(overview_inner, fg_color="transparent")
    app.strategy_bt_insight_frame.grid(row=1, column=0, sticky="ew", padx=8, pady=(2, 0))
    app.strategy_bt_insight_frame.grid_columnconfigure(0, weight=1)
    app.strategy_bt_chart_frame = ctk.CTkFrame(overview_inner, fg_color="transparent")
    app.strategy_bt_chart_frame.grid(row=2, column=0, sticky="nsew", padx=8, pady=(4, 8))
    app.strategy_bt_chart_frame.grid_columnconfigure(0, weight=1)
    app.strategy_bt_chart_frame.grid_rowconfigure(0, weight=1)

    # 持仓: 调仓选择、买卖明细与收益归因合并。
    positions_inner = _scrollable_pane("持仓")
    app.strategy_bt_selection_frame = ctk.CTkFrame(positions_inner, fg_color="transparent")
    app.strategy_bt_selection_frame.grid(row=0, column=0, sticky="ew", padx=8, pady=(6, 2))
    app.strategy_bt_selection_frame.grid_columnconfigure(0, weight=1)
    app.strategy_bt_table_frame = ctk.CTkFrame(positions_inner, fg_color="transparent")
    app.strategy_bt_table_frame.grid(row=1, column=0, sticky="ew", padx=8, pady=(2, 8))
    app.strategy_bt_table_frame.grid_columnconfigure(0, weight=1)
    app.strategy_bt_attribution_frame = ctk.CTkFrame(positions_inner, fg_color="transparent")
    app.strategy_bt_attribution_frame.grid(row=2, column=0, sticky="ew", padx=8, pady=(2, 8))
    app.strategy_bt_attribution_frame.grid_columnconfigure(0, weight=1)

    # 诊断: 风险、稳健性与数据质量放在同一条阅读路径。
    diagnosis_inner = _scrollable_pane("诊断")
    app.strategy_bt_risk_frame = ctk.CTkFrame(diagnosis_inner, fg_color="transparent")
    app.strategy_bt_risk_frame.grid(row=0, column=0, sticky="ew", padx=8, pady=(6, 4))
    app.strategy_bt_risk_frame.grid_columnconfigure(0, weight=1)
    app.strategy_bt_data_frame = ctk.CTkFrame(diagnosis_inner, fg_color="transparent")
    app.strategy_bt_data_frame.grid(row=1, column=0, sticky="ew", padx=8, pady=(4, 8))
    app.strategy_bt_data_frame.grid_columnconfigure(0, weight=1)

    app.strategy_bt_compare_frame = _scrollable_pane("对比")

    # 初始化策略模板与摘要。
    app._apply_strategy_template(app.v_st_template.get())
    # 启动时仅载入快照数据并刷新指标卡, 不渲染结果面板 (render=False);
    # 真正的面板/图表渲染推迟到用户首次打开策略页, 避免开机卡顿。
    if hasattr(app, "_load_strategy_backtest_snapshot"):
        app.after_idle(lambda: app._load_strategy_backtest_snapshot(silent=True, render=False))


def _adv_card(parent, row, title, subtitle=None, *, col=0, columnspan=1):
    """参数设置子卡: 放在统一底色卡片内, 和执行控制台信息卡保持一致."""
    padx = (0, 0) if columnspan > 1 else ((0, 8) if col == 0 else (8, 0))
    card = ctk.CTkFrame(parent, fg_color=BG_CARD, corner_radius=8)
    card.grid(row=row, column=col, columnspan=columnspan,
              sticky="nsew", padx=padx, pady=0)
    head = ctk.CTkFrame(card, fg_color="transparent")
    head.pack(fill="x", padx=16, pady=(10, 5))
    ctk.CTkLabel(head, text=title, text_color=ACCENT,
                 font=(FONT_FAMILY, 12, "bold")).pack(side="left", padx=(0, 8))
    if subtitle:
        ctk.CTkLabel(head, text=subtitle, text_color=TEXT_DIM,
                     font=(FONT_FAMILY, 11)).pack(side="left")
    body = ctk.CTkFrame(card, fg_color="transparent")
    body.pack(fill="x", padx=16, pady=(0, 8))
    return card, body


def _range_grid_cell(parent, row, col, label, min_var, max_var, *, width=70, tooltip=None):
    """微调后的范围过滤单元：增加灰色“不限”占位符"""
    cell = ctk.CTkFrame(parent, fg_color="transparent")
    cell.grid(row=row, column=col, sticky="w", pady=2, padx=6)

    lbl = ctk.CTkLabel(cell, text=label, text_color=TEXT_DIM, font=(FONT_FAMILY, 12, "bold"),
                 width=54, anchor="w")
    lbl.pack(side="left", padx=(0, 6))

    ent_min = ctk.CTkEntry(cell, textvariable=min_var, width=width, font=(FONT_MONO, 13),
                           fg_color=BG_INPUT, border_width=1, border_color=BORDER, corner_radius=6,
                           text_color=TEXT, height=28, placeholder_text="不限")
    ent_min.pack(side="left")

    ctk.CTkLabel(cell, text="~", text_color=TEXT_DIM,
                 font=(FONT_FAMILY, 12)).pack(side="left", padx=4)

    ent_max = ctk.CTkEntry(cell, textvariable=max_var, width=width, font=(FONT_MONO, 13),
                           fg_color=BG_INPUT, border_width=1, border_color=BORDER, corner_radius=6,
                           text_color=TEXT, height=28, placeholder_text="不限")
    ent_max.pack(side="left")
    if tooltip:
        Tooltip(lbl, tooltip)
        Tooltip(ent_min, tooltip)
        Tooltip(ent_max, tooltip)
