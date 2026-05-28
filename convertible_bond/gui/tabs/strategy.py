"""策略 Tab UI 构建.

布局设计：
  - 标题栏：策略 + PRO 徽标 + 一行简短描述
  - 核心参数: 策略方案 + 选债规则 + 日期/频率/仓位/成本
  - 联动: 手动修改核心参数或选债规则时, 策略方案自动切回 "自定义"
  - 高级设置 (默认折叠): 整体浅底卡片, 内部两张设置子卡
  - 指标看板: 10 个独立 Dashboard Tile 卡, 主指标边框高亮
"""

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
from ..widgets import CollapsibleSection, Tooltip, make_date_picker


def build(app, tab):
    """策略 Tab: 选债策略回测 Pro."""
    tab.grid_columnconfigure(0, weight=1)
    tab.grid_rowconfigure(0, weight=0)
    tab.grid_rowconfigure(1, weight=0)
    tab.grid_rowconfigure(2, weight=0)
    tab.grid_rowconfigure(3, weight=1)

    # ── 外边沿对齐 ──────────────────────────────────────────
    # 各主要卡片组件 (ctrl, stats_card, strategy_result_tabs) 外边沿统一对齐在 16px 处。
    ctrl = ctk.CTkFrame(tab, fg_color=BG_CARD, corner_radius=16)
    ctrl.grid(row=0, column=0, sticky="ew", pady=(6, 10), padx=16)
    ctrl.grid_columnconfigure(0, weight=1)

    # ── 标题与操作栏合并 (首屏高聚合) ──────────────────────────────────
    ch = ctk.CTkFrame(ctrl, fg_color="transparent")
    ch.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 8))
    ch.grid_columnconfigure(0, weight=1)
    ch.grid_columnconfigure(1, weight=0)

    # 左侧标题与描述
    title_box = ctk.CTkFrame(ch, fg_color="transparent")
    title_box.grid(row=0, column=0, sticky="w")
    ctk.CTkLabel(title_box, text=E("🎯 策略"),
                 font=(FONT_FAMILY, 16, "bold"), text_color=TEXT).pack(side="left")
    ctk.CTkLabel(title_box, text="选择方案和规则, 定频调仓回测",
                 font=(FONT_FAMILY, 12), text_color=TEXT_DIM).pack(side="left", padx=(12, 0))

    # 右上角: PRO 徽标 (轻量描边款, 不抢主标题视觉)
    pro_box = ctk.CTkFrame(
        ch, fg_color="transparent", corner_radius=4,
        border_width=1, border_color=ORANGE)
    pro_box.grid(row=0, column=1, sticky="e", padx=(8, 0))
    ctk.CTkLabel(
        pro_box, text="PRO", font=(FONT_FAMILY, 9, "bold"),
        text_color=ORANGE, padx=6, pady=0).pack(pady=(1, 1))

    # ── 核心参数设置网格 (2×4 干净规整) ────────────────────────────────────
    # 结合 ctrl(padx=16) + cc(padx=8) + cell(padx=8) = 32px，第一列输入框与标题文字完美左对齐。
    # 4 列等宽栅格, 每格内 inline label + control (label 在左, 控件按内容定宽)
    cc = ctk.CTkFrame(ctrl, fg_color="transparent")
    cc.grid(row=1, column=0, sticky="ew", padx=8, pady=(0, 4))
    for col in range(4):
        cc.grid_columnconfigure(col, weight=1, uniform="st_cols")

    def _grid_cell(parent, label, var, row, col, widget_type="entry", values=None,
                   tooltip=None, command=None, control_width=140, label_width=60,
                   cell_pady=4):
        """Inline label cell: label 左(定宽) + control 右, 同列控件左边缘对齐."""
        cell = ctk.CTkFrame(parent, fg_color="transparent")
        cell.grid(row=row, column=col, sticky="ew", padx=8, pady=cell_pady)

        if widget_type == "checkbox":
            # 占位 frame, 让 checkbox 左边缘对齐其他行的控件左边缘 (label_width + 8 gap)
            spacer = ctk.CTkFrame(cell, width=label_width + 8, height=1,
                                  fg_color="transparent")
            spacer.pack(side="left")
            spacer.pack_propagate(False)
            w = ctk.CTkCheckBox(
                cell, text="等权基准对标", variable=var, height=28,
                font=(FONT_FAMILY, 12), text_color=TEXT_DIM, fg_color=ACCENT,
                hover_color=ACCENT_HOVER, border_color=BORDER,
                checkbox_width=16, checkbox_height=16, border_width=1, corner_radius=3)
            w.pack(side="left", anchor="w")
            if tooltip:
                Tooltip(w, tooltip)
            return w

        lbl = ctk.CTkLabel(cell, text=label, text_color=TEXT_DIM,
                           font=(FONT_FAMILY, 13),
                           width=label_width, anchor="w")
        lbl.pack(side="left", padx=(0, 8))

        if widget_type == "entry":
            w = ctk.CTkEntry(cell, textvariable=var, font=(FONT_MONO, 13),
                             fg_color=BG_INPUT, border_width=0,
                             corner_radius=6, text_color=TEXT, height=28,
                             width=control_width)
            w.pack(side="left")
        elif widget_type == "date":
            w = make_date_picker(cell, var, entry_width=control_width)
            w.pack(side="left")
        elif widget_type == "optmenu":
            menu_kwargs = {"width": control_width}
            if command is not None:
                menu_kwargs["command"] = command
            w = ctk.CTkOptionMenu(
                cell, variable=var, values=values or [], height=28,
                font=(FONT_FAMILY, 12), fg_color=BORDER, button_color=BTN_HOVER,
                text_color=TEXT, dropdown_fg_color=BG_INPUT, dropdown_text_color=TEXT,
                **menu_kwargs)
            w.pack(side="left")

        if tooltip:
            Tooltip(lbl, tooltip)
            Tooltip(w, tooltip)
        return w

    # 每列 label_width 取该列两行 label 的最大宽度, 同列控件左边缘对齐
    # col 0: 策略方案/选债规则 → 72
    # col 1: 开始日期/Top N → 80
    # col 2: 结束日期/成本 (bps) → 80
    # col 3: 频率/基准设置 → 32

    # 第一行: 策略方案, 开始日期, 结束日期, 频率
    _grid_cell(
        cc, "策略方案", app.v_st_template, 0, 0, "optmenu", list(STRATEGY_TEMPLATE_NAMES),
        lambda: STRATEGY_TEMPLATE_DESCRIPTIONS.get(app.v_st_template.get(), ""),
        command=app._apply_strategy_template, control_width=130, label_width=72)
    _grid_cell(cc, "开始日期", app.v_st_start, 0, 1, "date", None,
               "回测起始日期\n格式 YYYY-MM-DD, 可点击日历选择",
               control_width=120, label_width=80)
    _grid_cell(cc, "结束日期", app.v_st_end, 0, 2, "date", None,
               "回测结束日期\n格式 YYYY-MM-DD, 可点击日历选择",
               control_width=120, label_width=80)
    _grid_cell(cc, "频率", app.v_st_freq, 0, 3, "optmenu", ["周", "月", "季"],
               "定期调仓的时间间隔", control_width=80, label_width=32)

    # 第二行: 选债规则, Top N, 成本, 基准设置
    _grid_cell(
        cc, "选债规则", app.v_st_view, 1, 0, "optmenu", list(STRATEGY_SELECTION_VIEWS),
        lambda: STRATEGY_VIEW_DESCRIPTIONS.get(app.v_st_view.get(), ""),
        command=app._describe_strategy_view, control_width=130, label_width=72)
    _grid_cell(cc, "Top N", app.v_st_top_n, 1, 1, "entry", None,
               "每期最大持仓转债数量", control_width=120, label_width=80)
    _grid_cell(cc, "成本 (bps)", app.v_st_cost, 1, 2, "entry", None,
               "单边调仓交易成本\n单位 bps (万分之一)",
               control_width=120, label_width=80)
    _grid_cell(cc, "基准设置", app.v_st_benchmark, 1, 3, "checkbox", None,
               "等权买入全市场合格转债\n作为对比基准", label_width=32)

    # ── 核心参数联动逻辑 (手动修改时策略方案自动切“自定义”) ───────────────────────
    def _on_param_change(*_):
        if not getattr(app, "_programmatic_update", False):
            app.v_st_template.set("自定义")

    for var in (app.v_st_start, app.v_st_end, app.v_st_freq,
                app.v_st_view, app.v_st_top_n, app.v_st_cost,
                app.v_st_benchmark):
        var.trace_add("write", _on_param_change)

    # 回测范围/历史口径 状态变量 (UI 在高级设置卡内; 这两个 StringVar 由
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
            manual_box.grid(row=3, column=0, sticky="ew", padx=16, pady=(0, 10))
        else:
            manual_box.grid_forget()
        app._refresh_strategy_setup_summary()

    for var in (app.v_st_pool_mode, app.v_st_history_mode, app.v_st_codes):
        var.trace_add("write", _refresh_scope_visibility)
    _refresh_scope_visibility()

    # ── 高级设置 (整体浅底卡片, 内部两张设置子卡) ─────────────────────
    adv_shell = ctk.CTkFrame(ctrl, fg_color=BG_INPUT, corner_radius=12)
    adv_shell.grid(row=4, column=0, sticky="ew", padx=16, pady=(0, 10))
    adv_shell.grid_columnconfigure(0, weight=1)

    adv = CollapsibleSection(adv_shell, "高级设置", expanded=False)
    adv.grid(row=0, column=0, sticky="ew", padx=16, pady=(6, 12))
    adv.header_btn.grid_configure(sticky="w")
    adv.header_btn.configure(
        fg_color="transparent",
        hover_color=BG_INPUT,
        corner_radius=8,
        height=28,
        width=118,
    )
    body = adv.content
    body.grid_columnconfigure(0, weight=1)

    adv_panel = ctk.CTkFrame(body, fg_color="transparent")
    adv_panel.grid(row=0, column=0, sticky="ew")
    adv_panel.grid_columnconfigure(0, weight=1, uniform="adv-panel")
    adv_panel.grid_columnconfigure(1, weight=1, uniform="adv-panel")

    # 左: 回测数据 — 回测范围 + 历史口径
    _, c0 = _adv_card(adv_panel, 0, "回测数据", "代码范围与历史口径", col=0)
    scope_grid = ctk.CTkFrame(c0, fg_color="transparent")
    scope_grid.pack(fill="x")
    scope_grid.grid_columnconfigure(0, weight=1)
    _grid_cell(
        scope_grid, "回测范围", app.v_st_pool_mode, 0, 0, "optmenu",
        list(STRATEGY_POOL_MODES),
        lambda: STRATEGY_POOL_DESCRIPTIONS.get(app.v_st_pool_mode.get(), ""),
        command=lambda _v: app._refresh_strategy_setup_summary(),
        control_width=160, label_width=64, cell_pady=2)
    _grid_cell(
        scope_grid, "历史口径", app.v_st_history_mode, 1, 0, "optmenu",
        list(STRATEGY_HISTORY_MODES),
        lambda: STRATEGY_HISTORY_DESCRIPTIONS.get(app.v_st_history_mode.get(), ""),
        command=lambda _v: app._refresh_strategy_setup_summary(),
        control_width=160, label_width=64, cell_pady=2)

    # 右: 选债条件 — 4 个范围 (2×2 网格)
    _, c1 = _adv_card(adv_panel, 0, "选债条件", "价格、溢价、偏差与波动率", col=1)
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
        tooltip="模型偏差 = (市价 − 理论价) / 理论价\n负值越大越低估")
    _range_grid_cell(
        range_grid, 1, 1, "HV%", app.v_st_min_sigma, app.v_st_max_sigma,
        tooltip="正股历史波动率\n窗口跟随顶部 σ 设置")

    # ── 执行控制台: 分为上方信息区(左策略, 右预检) 与 下方操作区(左状态, 右按钮) ────────────────
    console = ctk.CTkFrame(ctrl, fg_color=BG_INPUT, corner_radius=12)
    console.grid(row=5, column=0, sticky="ew", padx=16, pady=(0, 16))

    # 上半部：信息展示区 (左右两列)
    info_row = ctk.CTkFrame(console, fg_color="transparent")
    info_row.pack(fill="x", padx=16, pady=(12, 4))
    info_row.grid_columnconfigure(0, weight=1, uniform="info")
    info_row.grid_columnconfigure(1, weight=1, uniform="info")

    # 左列：已选策略摘要 (内部带有独立底色卡片，增加视觉边界)
    summary_box = ctk.CTkFrame(info_row, fg_color=BG_CARD, corner_radius=8)
    summary_box.grid(row=0, column=0, sticky="nsew", padx=(0, 6))

    summary_inner = ctk.CTkFrame(summary_box, fg_color="transparent")
    summary_inner.pack(fill="both", expand=True, padx=16, pady=12)

    ctk.CTkLabel(
        summary_inner, text="📌 已选策略", text_color=TEXT_DIM,
        font=(FONT_FAMILY, 11, "bold"), anchor="w").pack(anchor="w")
    app.lbl_strategy_summary = ctk.CTkLabel(
        summary_inner, textvariable=app.v_st_summary,
        font=(FONT_FAMILY, 12), text_color=TEXT,
        anchor="w", justify="left", wraplength=460)
    app.lbl_strategy_summary.pack(anchor="w", pady=(6, 0))

    # 右列：预检与数据信息 (内部带有独立底色卡片，增加视觉边界)
    precheck_box = ctk.CTkFrame(info_row, fg_color=BG_CARD, corner_radius=8)
    precheck_box.grid(row=0, column=1, sticky="nsew", padx=(6, 0))

    precheck_inner = ctk.CTkFrame(precheck_box, fg_color="transparent")
    precheck_inner.pack(fill="both", expand=True, padx=16, pady=12)

    ctk.CTkLabel(
        precheck_inner, text="🔍 预检与口径信息", text_color=TEXT_DIM,
        font=(FONT_FAMILY, 11, "bold"), anchor="w").pack(anchor="w")
    app.lbl_strategy_precheck = ctk.CTkLabel(
        precheck_inner, textvariable=app.v_st_precheck,
        font=(FONT_FAMILY, 11), text_color=TEXT_DIM,
        anchor="w", justify="left", wraplength=480)
    app.lbl_strategy_precheck.pack(anchor="w", pady=(6, 0))

    # 下半部：操作与状态区
    action_row = ctk.CTkFrame(console, fg_color="transparent")
    action_row.pack(fill="x", padx=16, pady=(4, 12))

    # 运行状态 (左对齐)
    app.lbl_strategy_bt_status = ctk.CTkLabel(
        action_row, textvariable=app.v_st_status,
        font=(FONT_FAMILY, 12, "bold"), text_color=TEXT, anchor="w", justify="left")
    app.lbl_strategy_bt_status.pack(side="left")

    # 操作按钮组 (右对齐)
    action_box = ctk.CTkFrame(action_row, fg_color="transparent")
    action_box.pack(side="right")
    BTN_H, BTN_R = 32, 8

    app.btn_strategy_compare_clear = ctk.CTkButton(
        action_box, text="清空对比", command=app._clear_strategy_comparison,
        fg_color="transparent", hover_color=BTN_HOVER, text_color=TEXT_DIM,
        font=(FONT_FAMILY, 12), width=72, height=BTN_H, corner_radius=BTN_R)
    app.btn_strategy_compare_clear.pack(side="left", padx=(0, 6))
    Tooltip(app.btn_strategy_compare_clear, "清除最近 8 次的策略对比记录")

    app.btn_strategy_bt_csv = ctk.CTkButton(
        action_box, text="导出 CSV", command=app._export_strategy_backtest_csv,
        fg_color="transparent", border_width=1, border_color=BORDER,
        hover_color=BTN_HOVER, text_color=TEXT,
        font=(FONT_FAMILY, 12), width=80, height=BTN_H, corner_radius=BTN_R,
        state="disabled")
    app.btn_strategy_bt_csv.pack(side="left", padx=(0, 6))
    Tooltip(app.btn_strategy_bt_csv, "导出回测结果\n逐期摘要 · 日频净值 · 持仓明细 · 汇总指标")

    app.btn_strategy_precheck = ctk.CTkButton(
        action_box, text=E("📋 预检"), command=app._precheck_strategy_backtest,
        fg_color=BG_CARD, border_width=1, border_color=BORDER,
        hover_color=BTN_HOVER, text_color=TEXT,
        font=(FONT_FAMILY, 12, "bold"), width=80, height=BTN_H, corner_radius=BTN_R)
    app.btn_strategy_precheck.pack(side="left", padx=(0, 10))
    Tooltip(app.btn_strategy_precheck, "预览代码池规模、历史口径和预计工作量\n仅查看信息, 不触发定价")

    app.btn_strategy_backtest = ctk.CTkButton(
        action_box, text=E("⚡ 运行策略"), command=app._run_strategy_backtest,
        fg_color=ACCENT, hover_color=ACCENT_HOVER,
        text_color=("#ffffff", "#11111b"),
        font=(FONT_FAMILY, 13, "bold"), width=120, height=BTN_H, corner_radius=BTN_R)
    app.btn_strategy_backtest.pack(side="left")
    Tooltip(app.btn_strategy_backtest, "自动预检后直接启动策略回测\n运行中可点击停止")

    # 进度条 (底部填充)
    app.strategy_bt_progress = ctk.CTkProgressBar(
        console, height=3, corner_radius=2,
        progress_color=ACCENT, fg_color=BG_CARD)
    app.strategy_bt_progress.set(0)
    app.strategy_bt_progress.pack(fill="x", padx=16, pady=(0, 12))

    # ── 指标卡 Dashboard Tiles (对齐卡片 16px) ───────────────────
    app._strategy_stat_vars = {}
    app._strategy_stat_labels = {}
    stats_card = ctk.CTkFrame(tab, fg_color="transparent")
    stats_card.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 8))
    for col in range(5):
        stats_card.grid_columnconfigure(col, weight=1, uniform="stbts")

    def _stat(row, col, key, title, *, primary=True):
        var = ctk.StringVar(value="—")
        # 主指标赋予主题色边框高亮
        border_c = ACCENT if primary else BORDER
        cell = ctk.CTkFrame(stats_card, fg_color=BG_CARD, corner_radius=12, border_width=1, border_color=border_c)
        pady = (8, 4) if row == 0 else (2, 8)
        cell.grid(row=row, column=col, sticky="nsew", padx=4, pady=pady)

        inner = ctk.CTkFrame(cell, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=12, pady=8)

        title_lbl = ctk.CTkLabel(inner, text=title, text_color=TEXT_DIM,
                                 font=(FONT_FAMILY, 11, "bold"))
        title_lbl.pack(anchor="w")

        size = 20 if primary else 16
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

    # 使用 Windows 兼容表情前缀，增强较强视觉指示
    _stat(0, 0, "final_equity", E("📈 最终净值"))
    _stat(0, 1, "total_return", E("💰 总收益"))
    _stat(0, 2, "annualized", E("📊 年化收益"))
    _stat(0, 3, "excess", E("✨ 超额收益"))
    _stat(0, 4, "max_drawdown", E("📉 最大回撤"))
    _stat(1, 0, "sharpe", E("⚡ Sharpe"), primary=False)
    _stat(1, 1, "sortino", E("🛡️ Sortino"), primary=False)
    _stat(1, 2, "calmar", E("🎯 Calmar"), primary=False)
    _stat(1, 3, "cash", E("💵 平均现金"), primary=False)
    _stat(1, 4, "turnover", E("🔄 平均换手"), primary=False)

    app.strategy_result_tabs = ctk.CTkTabview(
        tab, fg_color=BG_CARD, segmented_button_fg_color=BG_INPUT,
        segmented_button_selected_color=ACCENT,
        segmented_button_selected_hover_color=ACCENT_HOVER,
        segmented_button_unselected_color=BG_INPUT,
        segmented_button_unselected_hover_color=BTN_HOVER,
        text_color=TEXT, corner_radius=16)
    app.strategy_result_tabs.grid(row=2, column=0, sticky="nsew", padx=16, pady=(0, 6))
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

    # 初始化已选策略摘要 (默认选债规则描述)
    app._describe_strategy_view(app.v_st_view.get())


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


def _adv_card(parent, row, title, subtitle=None, *, col=0):
    """高级设置子卡: 放在统一底色卡片内, 和执行控制台信息卡保持一致."""
    padx = (0, 8) if col == 0 else (8, 0)
    card = ctk.CTkFrame(parent, fg_color=BG_CARD, corner_radius=8)
    card.grid(row=row, column=col, sticky="nsew", padx=padx, pady=0)
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
