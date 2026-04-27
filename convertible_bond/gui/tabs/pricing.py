"""⚡ 定价 Tab — 参数面板 + 结果仪表盘 + IV 反解 + 现金流 + 收敛诊断."""
import threading
import customtkinter as ctk
from datetime import timedelta
import numpy as np

from ..theme import *
from ..widgets import _form_row, create_card, CollapsibleSection, Tooltip

from ...pricer import UniversalCBPricer
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg


def build(app, tab):
    """在 tab frame 上构建定价面板, 把相关控件/方法绑定到 app."""
    tab.grid_columnconfigure(0, weight=0)
    tab.grid_columnconfigure(1, weight=1)
    tab.grid_rowconfigure(0, weight=1)

    # ── 左列: 参数面板 ──
    lp = ctk.CTkScrollableFrame(tab, fg_color="transparent", width=380,
                                scrollbar_button_color=BORDER)
    lp.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
    lp.grid_columnconfigure(0, weight=1)

    sec1 = create_card(lp, "基本条款", 0, 0, icon="📝")
    _form_row(sec1, "正股价 S", app.v_S0, 0, wind=True)
    _form_row(sec1, "转股价 K", app.v_K, 1, wind=True)
    _form_row(sec1, "面值", app.v_face, 2, wind=True)
    _form_row(sec1, "到期赎回价", app.v_redemp, 3, wind=True)
    _form_row(sec1, "估值日期", app.v_cur_date, 4)
    _form_row(sec1, "到期日期", app.v_mat_date, 5, wind=True)
    _form_row(sec1, "发行日期", app.v_iss_date, 6, wind=True)
    _form_row(sec1, "转股起始日", app.v_conv_date, 7, wind=True)
    _form_row(sec1, "各年票息 (%)", app.v_coupons, 8, wind=True, width=180)

    sec2 = create_card(lp, "模型参数", 1, 0, icon="⚙️")
    def make_vol(p):
        app.vol_window_menu = ctk.CTkOptionMenu(
            p, variable=app.v_vol_window, values=list(VOL_WINDOW_MAP.keys()),
            width=75, font=(FONT_FAMILY, 12), fg_color=BORDER, button_color=BTN_HOVER,
            text_color=TEXT, dropdown_fg_color=BG_INPUT, dropdown_text_color=TEXT,
            command=app._on_vol_window_change)
        return app.vol_window_menu
    _form_row(sec2, "波动率 σ (%)", app.v_sigma, 0, wind=True, width=80, extra_widget=make_vol)
    def make_shi(p):
        app.btn_shibor = ctk.CTkButton(
            p, text="Shibor", command=app._fetch_shibor, fg_color=BTN_CTRL,
            hover_color=BTN_HOVER, text_color=ORANGE,
            font=(FONT_FAMILY, 12, "bold"), width=75, height=28, corner_radius=6)
        return app.btn_shibor
    _form_row(sec2, "无风险利率 r (%)", app.v_r, 1, width=80, extra_widget=make_shi)
    def make_spr(p):
        app.btn_spread = ctk.CTkButton(
            p, text="按评级", command=app._fill_spread_from_rating, fg_color=BTN_CTRL,
            hover_color=BTN_HOVER, text_color=ORANGE,
            font=(FONT_FAMILY, 12, "bold"), width=75, height=28, corner_radius=6)
        return app.btn_spread
    _form_row(sec2, "信用利差 (%)", app.v_spread, 2, width=80, extra_widget=make_spr)
    _form_row(sec2, "下修概率 p (%)", app.v_p_down, 3)
    _form_row(sec2, "信用扩张系数 (%)", app.v_dk, 4)

    adv = CollapsibleSection(lp, "高级参数 (条款 & 网格)", expanded=False)
    adv.grid(row=2, column=0, sticky="ew", padx=6, pady=5)
    sec3 = create_card(adv.content, "条款触发条件", 0, 0, icon="⚡")
    _form_row(sec3, "强赎触发 (%K)", app.v_call_ratio, 0, wind=True)
    _form_row(sec3, "回售触发 (%K)", app.v_put_ratio, 1, wind=True)
    _form_row(sec3, "回售生效年数", app.v_put_years, 2, wind=True)
    _form_row(sec3, "强赎宽限天数", app.v_call_notice, 3)
    sec4 = create_card(adv.content, "数值网格", 1, 0, icon="🧮")
    _form_row(sec4, "空间节点 M", app.v_M, 0)
    _form_row(sec4, "时间步数 N", app.v_N, 1)

    # ── 右列: 结果面板 ──
    rp = ctk.CTkFrame(tab, fg_color="transparent")
    rp.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
    rp.grid_columnconfigure(0, weight=1)
    rp.grid_rowconfigure(1, weight=1)

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

    # IV 工具栏
    tb = ctk.CTkFrame(rc, fg_color="transparent")
    tb.grid(row=1, column=0, columnspan=2, sticky="ew", padx=30, pady=(0, 25))

    ctk.CTkLabel(tb, text="🎯 隐含波动率反解", text_color=TEXT_DIM,
                 font=(FONT_FAMILY, 13, "bold")).pack(side="left", padx=(0, 15))
    ctk.CTkEntry(tb, textvariable=app.v_market_price, width=80,
                 font=(FONT_MONO, 13), fg_color=BG_INPUT, border_width=0, corner_radius=6,
                 placeholder_text="市价 ¥").pack(side="left", padx=(0, 8))
    app.btn_iv = ctk.CTkButton(
        tb, text="解 IV", command=app._solve_iv,
        fg_color=BTN_CTRL, hover_color=BTN_HOVER, text_color=ORANGE,
        font=(FONT_FAMILY, 12, "bold"), width=70, height=28, corner_radius=6)
    app.btn_iv.pack(side="left", padx=(0, 15))

    ctk.CTkLabel(tb, text="IV =", text_color=TEXT_DIM,
                 font=(FONT_FAMILY, 12)).pack(side="left", padx=(0, 4))
    ctk.CTkLabel(tb, textvariable=app.v_iv, text_color=ORANGE,
                 font=(FONT_MONO, 14, "bold"), width=70, anchor="w").pack(side="left", padx=(0, 20))

    app.btn_conv = ctk.CTkButton(
        tb, text="🩺 收敛诊断", command=app._convergence_check,
        fg_color=BTN_CTRL, hover_color=BTN_HOVER, text_color=TEXT_DIM,
        font=(FONT_FAMILY, 12, "bold"), width=90, height=28, corner_radius=6)
    app.btn_conv.pack(side="right")
    app.btn_cashflow = ctk.CTkButton(
        tb, text="💰 现金流", command=app._show_cashflow,
        fg_color=BTN_CTRL, hover_color=BTN_HOVER, text_color=TEXT_DIM,
        font=(FONT_FAMILY, 12, "bold"), width=90, height=28, corner_radius=6)
    app.btn_cashflow.pack(side="right", padx=(0, 8))

    # 指标仪表盘
    dc = ctk.CTkFrame(rp, fg_color="transparent")
    dc.grid(row=1, column=0, sticky="nsew", pady=(0, 6))
    dc.grid_columnconfigure((0, 1, 2, 3), weight=1, uniform="dec")
    dc.grid_rowconfigure((0, 1), weight=1, uniform="r")

    def _tile(parent, row, col, label, var, hl=False):
        t = ctk.CTkFrame(parent, fg_color=BG_CARD, corner_radius=16)
        t.grid(row=row, column=col, sticky="nsew", padx=5, pady=5)
        t.grid_columnconfigure(0, weight=1)
        t.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(t, text=label, text_color=TEXT_DIM,
                     font=(FONT_FAMILY, 12, "bold")).grid(row=0, column=0, sticky="w", padx=16, pady=(16, 0))
        val_color = ACCENT if hl else TEXT
        ctk.CTkLabel(t, textvariable=var, text_color=val_color,
                     font=(FONT_MONO, 20, "bold")).grid(row=1, column=0, sticky="w", padx=16, pady=(0, 16))

    _tile(dc, 0, 0, "🏷️ 纯债价值", app.v_bond_floor)
    _tile(dc, 0, 1, "🔄 转股价值", app.v_parity)
    _tile(dc, 0, 2, "✨ 期权溢价", app.v_option_prem, hl=True)
    _tile(dc, 0, 3, "Δ Delta", app.v_delta)
    _tile(dc, 1, 0, "Γ Gamma", app.v_gamma)
    _tile(dc, 1, 1, "ν Vega", app.v_vega)
    _tile(dc, 1, 2, "Θ Theta", app.v_theta)
    _tile(dc, 1, 3, "🎯 隐含波动率", app.v_iv)
