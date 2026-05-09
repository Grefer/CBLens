"""⚡ 定价 Tab UI 构建."""
import tkinter as tk

import customtkinter as ctk

from ..theme import (
    BG_APP, BG_CARD, BG_INPUT, BORDER, TEXT, TEXT_DIM,
    ACCENT, ACCENT_HOVER, BTN_CTRL, BTN_HOVER, ORANGE,
    FONT_FAMILY, FONT_MONO, get_color,
    VOL_WINDOW_MAP,
)
from ..widgets import (
    _form_row, create_card, CollapsibleSection, Tooltip, ENTRY_HEIGHT,
)


def build(app, tab):
    """定价 Tab: 左列参数面板 + 右列结果仪表盘."""
    tab.grid_columnconfigure(0, weight=1)
    tab.grid_rowconfigure(0, weight=1)

    paned = tk.PanedWindow(
        tab,
        orient=tk.HORIZONTAL,
        bd=0,
        borderwidth=0,
        bg=get_color(BORDER),
        sashwidth=8,
        sashrelief="flat",
        showhandle=False,
    )
    paned.grid(row=0, column=0, sticky="nsew")
    app.pricing_paned = paned

    # paned 是 tk.PanedWindow, 子 CTkFrame 用 transparent 会把 master.cget("bg") 冻结成字符串, 主题切换不更新; 这里给 tuple 颜色避开
    # lp 宽度 460 — 留出空间给 [输入] [按钮槽] [来源槽] 三列对齐, 同时容纳 12 字以内的中文标签
    lp_host = ctk.CTkFrame(paned, fg_color=BG_APP, width=460)
    lp_host.grid_columnconfigure(0, weight=1)
    lp_host.grid_rowconfigure(0, weight=1)
    rp_host = ctk.CTkFrame(paned, fg_color=BG_APP)
    rp_host.grid_columnconfigure(0, weight=1)
    rp_host.grid_rowconfigure(0, weight=1)

    paned.add(lp_host, minsize=380)
    paned.add(rp_host, minsize=540)
    app.after(100, lambda: paned.sash_place(0, 470, 1))

    # ── 左列: 参数面板 (可滚动) ──
    lp = ctk.CTkScrollableFrame(lp_host, fg_color="transparent", width=460,
                                scrollbar_button_color=BORDER)
    lp.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
    lp.grid_columnconfigure(0, weight=1)

    sec1 = create_card(lp, "定价核心", 0, 0, icon="⚡")

    def make_vol(p):
        # 出现在 "波动率窗口" 行的 entry 槽内, 作为 custom_widget; 宽度略大于
        # entry 默认 130, 这样下拉菜单看起来跟其他行的输入框等宽。
        app.vol_window_menu = ctk.CTkOptionMenu(
            p, variable=app.v_vol_window, values=list(VOL_WINDOW_MAP.keys()),
            width=130, height=ENTRY_HEIGHT,
            font=(FONT_FAMILY, 12), fg_color=BORDER, button_color=BTN_HOVER,
            text_color=TEXT, dropdown_fg_color=BG_INPUT, dropdown_text_color=TEXT,
            command=app._on_vol_window_change)
        return app.vol_window_menu

    def make_shi(p):
        app.btn_shibor = ctk.CTkButton(
            p, text="Shibor", command=app._fetch_shibor, fg_color=BTN_CTRL,
            hover_color=BTN_HOVER, text_color=ORANGE,
            font=(FONT_FAMILY, 12, "bold"), width=75, height=ENTRY_HEIGHT,
            corner_radius=6)
        return app.btn_shibor

    def make_spr(p):
        app.btn_spread = ctk.CTkButton(
            p, text="按评级", command=app._fill_spread_from_rating, fg_color=BTN_CTRL,
            hover_color=BTN_HOVER, text_color=ORANGE,
            font=(FONT_FAMILY, 12, "bold"), width=75, height=ENTRY_HEIGHT,
            corner_radius=6)
        return app.btn_spread

    def make_event_status(p):
        # 事件状态只读显示 — 占用 entry 槽位, 与其他行左边沿对齐。
        return ctk.CTkLabel(
            p, textvariable=app.v_dr_status, text_color=TEXT,
            font=(FONT_FAMILY, 12), width=130, height=ENTRY_HEIGHT, anchor="w")

    _form_row(sec1, "正股价 S", app.v_S0, 0, wind=True,
              source_var=app.v_src_S0, show_source=True,
              tooltip="估值日附近正股收盘/最新价, 是转股价值和下修触发判断的核心输入。")
    _form_row(sec1, "转股价 K", app.v_K, 1, wind=True,
              source_var=app.v_src_K, show_source=True,
              tooltip="当前转股价。转股价值约等于正股价除以转股价再乘以 100。")
    _form_row(sec1, "波动率窗口", None, 2, custom_widget=make_vol,
              tooltip="用于重新估算波动率的历史窗口。修改后会重算当前正股的年化波动率。")
    _form_row(sec1, "波动率 σ (%)", app.v_sigma, 3, wind=True, width=130,
              source_var=app.v_src_sigma, show_source=True,
              tooltip="年化历史波动率。窗口长度由上方「波动率窗口」控制。")
    _form_row(sec1, "无风险利率 r (%)", app.v_r, 4, width=130,
              extra_widget=make_shi, source_var=app.v_src_r, show_source=True,
              tooltip="无风险利率, 默认可用 1 年期银行间同业拆借利率近似。")
    _form_row(sec1, "信用利差 (%)", app.v_spread, 5, width=130,
              extra_widget=make_spr,
              source_var=app.v_src_spread, show_source=True,
              tooltip="用于纯债折现和信用风险调整。可按评级经验表自动填入。")
    _form_row(sec1, "事件状态", None, 6, custom_widget=make_event_status)

    adv_terms = CollapsibleSection(lp, "条款明细", expanded=False)
    adv_terms.grid(row=1, column=0, sticky="ew", padx=6, pady=5)
    sec_terms = create_card(adv_terms.content, "条款与日期", 0, 0, icon="📄")
    _form_row(sec_terms, "面值", app.v_face, 0, wind=True, source_var=app.v_src_face,
              tooltip="通常为 100。除特殊测试外无需修改。")
    _form_row(sec_terms, "到期赎回价", app.v_redemp, 1, wind=True, source_var=app.v_src_redemp,
              tooltip="到期偿付价格, 含最后一期利息和赎回溢价。")
    _form_row(sec_terms, "估值日期", app.v_cur_date, 2, source_var=app.v_src_cur_date,
              tooltip="模型当前日期。历史定价或复盘时可手动调整。")
    _form_row(sec_terms, "到期日期", app.v_mat_date, 3, wind=True, source_var=app.v_src_mat_date)
    _form_row(sec_terms, "发行日期", app.v_iss_date, 4, wind=True, source_var=app.v_src_iss_date)
    _form_row(sec_terms, "转股起始日", app.v_conv_date, 5, wind=True, source_var=app.v_src_conv_date)
    _form_row(sec_terms, "各年票息 (%)", app.v_coupons, 6, wind=True, width=240,
              compact=True, source_var=app.v_src_coupons,
              tooltip="逐年票息百分比, 逗号分隔。")
    _form_row(sec_terms, "强赎触发 (%K)", app.v_call_ratio, 7, wind=True,
              source_var=app.v_src_call_ratio,
              tooltip="正股价格达到转股价的该比例附近时触发强赎条款。")
    _form_row(sec_terms, "回售触发 (%K)", app.v_put_ratio, 8, wind=True,
              source_var=app.v_src_put_ratio,
              tooltip="正股价格低于转股价的该比例附近时触发回售条款。")
    _form_row(sec_terms, "回售生效年数", app.v_put_years, 9, wind=True,
              source_var=app.v_src_put_years)
    _form_row(sec_terms, "强赎宽限天数", app.v_call_notice, 10,
              source_var=app.v_src_call_notice,
              tooltip="公告强赎后的缓冲窗口。用于近似宽限期内的股票选择权。")

    adv_model = CollapsibleSection(lp, "高级模型参数", expanded=False)
    adv_model.grid(row=2, column=0, sticky="ew", padx=6, pady=5)
    sec4 = create_card(adv_model.content, "数值网格", 0, 0, icon="🧮")
    _form_row(sec4, "空间节点 M", app.v_M, 0,
              tooltip="价格区间网格。越大越精细, 也越慢。")
    _form_row(sec4, "时间步数 N", app.v_N, 1,
              tooltip="定价时间步网格。越大越精细, 也越慢。")

    dr_sec = CollapsibleSection(lp, "下修事件", expanded=False)
    dr_sec.grid(row=3, column=0, sticky="ew", padx=6, pady=5)
    # p_down 直接对应"下修事件强度", distress_k 与下修触发的信用恶化耦合,
    # 一并放进下修事件区作为模型参数, 与下面的"事件覆盖"配合使用.
    sec_dr_model = create_card(dr_sec.content, "事件模型参数", 0, 0, icon="🎲")
    _form_row(sec_dr_model, "下修强度 p (%/年)", app.v_p_down, 0, source_var=app.v_src_p_down,
              tooltip="年化下修事件强度。公告不下修冻结期内会被事件表自动屏蔽。")
    _form_row(sec_dr_model, "信用扩张系数 (%)", app.v_dk, 1, source_var=app.v_src_dk,
              tooltip="正股越低时信用利差扩张的幅度参数。")
    app._build_down_reset_panel(dr_sec.content)

    ev_sec = CollapsibleSection(lp, "公告事件", expanded=False)
    ev_sec.grid(row=4, column=0, sticky="ew", padx=6, pady=5)
    app._build_events_panel(ev_sec.content)

    # ── 右列: 结果面板 ──
    rp = ctk.CTkFrame(rp_host, fg_color="transparent")
    rp.grid(row=0, column=0, sticky="nsew", padx=(5, 0))
    rp.grid_columnconfigure(0, weight=1)
    rp.grid_rowconfigure(2, weight=1)  # 仪表盘行才需要拉伸

    # 英雄结果卡
    rc = ctk.CTkFrame(rp, fg_color=BG_CARD, corner_radius=16)
    rc.grid(row=0, column=0, sticky="ew", pady=(6, 12))
    rc.grid_columnconfigure(0, weight=1)
    rc.grid_columnconfigure(1, weight=1)

    left_hero = ctk.CTkFrame(rc, fg_color="transparent")
    left_hero.grid(row=0, column=0, sticky="nw", padx=30, pady=25)

    app.btn_calc = ctk.CTkButton(
        left_hero, text="✨ 开始计算 (Ctrl+Enter)", command=app._run_pricing,
        font=(FONT_FAMILY, 15, "bold"), width=200, height=50, corner_radius=10,
        fg_color=("#1e66f5", "#0052cc"), hover_color=("#7287fd", "#0066ff"),
        text_color=("#ffffff", "#ffffff"))
    app.btn_calc.pack(anchor="w", pady=(0, 15))

    app.progress_bar = ctk.CTkProgressBar(
        left_hero, orientation="horizontal", mode="indeterminate",
        width=200, height=4, corner_radius=2, progress_color=ACCENT, fg_color=BG_INPUT)
    app.progress_bar.pack(anchor="w", pady=(0, 10))
    app.progress_bar.set(0)

    right_hero = ctk.CTkFrame(rc, fg_color="transparent")
    right_hero.grid(row=0, column=1, sticky="ne", padx=30, pady=25)

    ctk.CTkLabel(right_hero, text="理论价格 (¥)", font=(FONT_FAMILY, 13),
                 text_color=TEXT_DIM).pack(anchor="e")
    app.lbl_result = ctk.CTkLabel(right_hero, textvariable=app.v_result,
                                   font=(FONT_FAMILY, 56, "bold"), text_color=TEXT)
    app.lbl_result.pack(anchor="e")
    # 市价偏差 (vs market price)
    ctk.CTkLabel(right_hero, text="vs 市价", font=(FONT_FAMILY, 10),
                 text_color=TEXT_DIM).pack(anchor="e", pady=(4, 0))
    app.lbl_deviation = ctk.CTkLabel(right_hero, textvariable=app.v_deviation,
                                      font=(FONT_MONO, 14, "bold"), text_color=TEXT_DIM)
    app.lbl_deviation.pack(anchor="e")

    # IV 工具栏
    tb = ctk.CTkFrame(rc, fg_color="transparent")
    tb.grid(row=1, column=0, columnspan=2, sticky="ew", padx=30, pady=(0, 25))
    tb.grid_columnconfigure(0, weight=1)
    tb.grid_columnconfigure(1, weight=0)

    iv_tools = ctk.CTkFrame(tb, fg_color="transparent")
    iv_tools.grid(row=0, column=0, sticky="w")
    action_tools = ctk.CTkFrame(tb, fg_color="transparent")
    action_tools.grid(row=0, column=1, sticky="e", padx=(12, 0))

    ctk.CTkLabel(iv_tools, text="🎯 隐含波动率反解", text_color=TEXT_DIM,
                 font=(FONT_FAMILY, 13, "bold")).pack(side="left", padx=(0, 15))
    ctk.CTkEntry(iv_tools, textvariable=app.v_market_price, width=80,
                 font=(FONT_MONO, 13), fg_color=BG_INPUT, border_width=0, corner_radius=6,
                 placeholder_text="市价 ¥").pack(side="left", padx=(0, 8))
    app.btn_iv = ctk.CTkButton(
        iv_tools, text="解 IV", command=app._solve_iv,
        fg_color=BTN_CTRL, hover_color=BTN_HOVER, text_color=ORANGE,
        font=(FONT_FAMILY, 12, "bold"), width=70, height=28, corner_radius=6)
    app.btn_iv.pack(side="left", padx=(0, 15))

    ctk.CTkLabel(iv_tools, text="IV =", text_color=TEXT_DIM,
                 font=(FONT_FAMILY, 12)).pack(side="left", padx=(0, 4))
    ctk.CTkLabel(iv_tools, textvariable=app.v_iv, text_color=ORANGE,
                 font=(FONT_MONO, 14, "bold"), width=70, anchor="w").pack(side="left", padx=(0, 20))

    # 现金流按钮 (常用) 留在右侧, 收敛诊断属于开发者工具, 已迁移到状态栏右键菜单
    app.btn_cashflow = ctk.CTkButton(
        action_tools, text="💰 现金流", command=app._show_cashflow,
        fg_color=BTN_CTRL, hover_color=BTN_HOVER, text_color=ORANGE,
        font=(FONT_FAMILY, 12, "bold"), width=90, height=28, corner_radius=6)
    app.btn_cashflow.pack(side="right")

    # ── 🎯 What-if 快算 (波动率 ±2pp/±5pp · 正股 ±5%/±10%) ──
    _build_what_if_row(app, rp)

    # 指标仪表盘 (8 tiles)
    dc = ctk.CTkFrame(rp, fg_color="transparent")
    dc.grid(row=2, column=0, sticky="nsew", pady=(0, 6))
    dc.grid_columnconfigure((0, 1, 2, 3), weight=1, uniform="dec")
    dc.grid_rowconfigure((0, 1), weight=1, uniform="r")

    def _tile(parent, row, col, label, var, hl=False):
        t = ctk.CTkFrame(parent, fg_color=BG_CARD, corner_radius=16)
        t.grid(row=row, column=col, sticky="nsew", padx=5, pady=5)
        t.grid_columnconfigure(0, weight=1)
        t.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(t, text=label, text_color=TEXT_DIM,
                     font=(FONT_FAMILY, 12, "bold")).grid(
            row=0, column=0, sticky="w", padx=16, pady=(16, 0))
        val_color = ACCENT if hl else TEXT
        ctk.CTkLabel(t, textvariable=var, text_color=val_color,
                     font=(FONT_MONO, 20, "bold")).grid(
            row=1, column=0, sticky="w", padx=16, pady=(0, 16))

    _tile(dc, 0, 0, "🏷️ 纯债价值", app.v_bond_floor)
    _tile(dc, 0, 1, "🔄 转股价值", app.v_parity)
    _tile(dc, 0, 2, "✨ 期权溢价", app.v_option_prem, hl=True)
    _tile(dc, 0, 3, "Δ Delta", app.v_delta)
    _tile(dc, 1, 0, "Γ Gamma", app.v_gamma)
    _tile(dc, 1, 1, "ν Vega", app.v_vega)
    _tile(dc, 1, 2, "Θ Theta", app.v_theta)
    _tile(dc, 1, 3, "🎯 隐含波动率", app.v_iv)


# ── What-if 快算: σ ±pp 与 S ±% 微扰 ─────────────────────────
WHAT_IF_SIGMA_DELTAS_PP = (-5, -2, +2, +5)
WHAT_IF_S_DELTAS_PCT    = (-10, -5, +5, +10)


def _build_what_if_row(app, parent):
    """在右栏 hero 与 dashboard 之间插入一行 σ/S 快扫按钮."""
    card = ctk.CTkFrame(parent, fg_color=BG_CARD, corner_radius=12)
    card.grid(row=1, column=0, sticky="ew", pady=(0, 8))
    card.grid_columnconfigure(2, weight=1)

    ctk.CTkLabel(card, text="🎯 What-if", text_color=TEXT_DIM,
                 font=(FONT_FAMILY, 13, "bold")).grid(
        row=0, column=0, rowspan=2, padx=(16, 12), pady=12, sticky="w")

    # σ 行
    ctk.CTkLabel(card, text="σ", text_color=TEXT_DIM,
                 font=(FONT_FAMILY, 12, "bold")).grid(
        row=0, column=1, padx=(0, 8), pady=(10, 2), sticky="w")
    sig_box = ctk.CTkFrame(card, fg_color="transparent")
    sig_box.grid(row=0, column=2, sticky="w", padx=(0, 16), pady=(8, 2))

    app._wf_sigma_buttons = {}
    for delta in WHAT_IF_SIGMA_DELTAS_PP:
        var = ctk.StringVar(value=f"{delta:+d}pp")
        btn = ctk.CTkButton(
            sig_box, textvariable=var,
            command=lambda d=delta: app._run_what_if("sigma", d),
            fg_color=BTN_CTRL, hover_color=BTN_HOVER, text_color=TEXT_DIM,
            font=(FONT_MONO, 11, "bold"), width=80, height=26, corner_radius=6,
            state="disabled",  # 等待主结果出来后才解锁, 避免 base=NaN 时点了无响应
        )
        btn.pack(side="left", padx=(0, 4))
        app._wf_sigma_buttons[delta] = (btn, var)

    # S 行
    ctk.CTkLabel(card, text="S", text_color=TEXT_DIM,
                 font=(FONT_FAMILY, 12, "bold")).grid(
        row=1, column=1, padx=(0, 8), pady=(2, 10), sticky="w")
    s_box = ctk.CTkFrame(card, fg_color="transparent")
    s_box.grid(row=1, column=2, sticky="w", padx=(0, 16), pady=(2, 8))

    app._wf_s_buttons = {}
    for delta in WHAT_IF_S_DELTAS_PCT:
        var = ctk.StringVar(value=f"{delta:+d}%")
        btn = ctk.CTkButton(
            s_box, textvariable=var,
            command=lambda d=delta: app._run_what_if("S", d),
            fg_color=BTN_CTRL, hover_color=BTN_HOVER, text_color=TEXT_DIM,
            font=(FONT_MONO, 11, "bold"), width=80, height=26, corner_radius=6,
            state="disabled",
        )
        btn.pack(side="left", padx=(0, 4))
        app._wf_s_buttons[delta] = (btn, var)
