#!/usr/bin/env python3
"""
可转债理论定价 GUI
支持手动输入参数 & 通过 Wind 代码自动拉取
最美观优雅的极简 Apple-inspired 设计，支持深/浅色模式无缝切换
"""

import customtkinter as ctk
from tkinter import messagebox, filedialog
from datetime import date, datetime, timedelta
import csv
import json
import threading
import sys
import numpy as np

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import matplotlib.pyplot as plt

# 解决 Matplotlib 中文显示和负号问题
matplotlib.rcParams['font.sans-serif'] = ['PingFang SC', 'Heiti TC', 'Arial Unicode MS', 'SimHei', 'Microsoft YaHei', 'sans-serif']
matplotlib.rcParams['axes.unicode_minus'] = False

from CB import (
    UniversalCBPricer, ensure_wind, to_date, parse_coupon, hist_vol,
    fetch_cashflow, backtest_theoretical_price,
    DEFAULT_COUPON_RATES,
)

# ── 颜色与主题 ──────────────────────────────────────────────
ctk.set_default_color_theme("blue")  

# Catppuccin 风格高级配色: (浅色 Latte, 深色 Mocha)
BG_APP    = ("#dce0e8", "#11111b")     # Crust
BG_CARD   = ("#eff1f5", "#1e1e2e")     # Base
BG_INPUT  = ("#e6e9ef", "#181825")     # Mantle
BORDER    = ("#ccd0da", "#313244")     # Surface0
TEXT      = ("#4c4f69", "#cdd6f4")     # Text
TEXT_DIM  = ("#6c6f85", "#a6adc8")     # Subtext0
ACCENT    = ("#1e66f5", "#89b4fa")     # Blue
GREEN     = ("#40a02b", "#a6e3a1")     # Green
RED       = ("#d20f39", "#f38ba8")     # Red
ORANGE    = ("#fe640b", "#fab387")     # Peach 

BTN_CTRL  = ("#ccd0da", "#313244")
BTN_HOVER = ("#bcc0cc", "#45475a")
ACCENT_HOVER = ("#7287fd", "#74c7ec")

_IS_MAC = sys.platform == "darwin"
FONT_FAMILY = "SF Pro Display" if _IS_MAC else "Segoe UI"
FONT_MONO = "SF Mono" if _IS_MAC else "Cascadia Mono"

# 历史波动率窗口选项 (交易日数)
VOL_WINDOW_MAP = {"1M": 21, "2M": 42, "3M": 63, "6M": 126, "1Y": 252}
VOL_WINDOW_DEFAULT = "1M"

# 同评级信用利差经验值 (%)
CREDIT_SPREAD_TABLE = {
    "AAA": 0.5, "AA+": 1.5, "AA": 2.5,
    "AA-": 4.0, "A+": 6.0, "A": 8.0,
}

def get_color(color_val):
    """解析当前模式下的颜色值，主要用于 Matplotlib"""
    if isinstance(color_val, tuple):
        return color_val[1] if ctk.get_appearance_mode() == "Dark" else color_val[0]
    return color_val

# ── UI 辅助函数 ──────────────────────────────────────────────
def _form_row(parent, label_text, var, row, wind=False, extra_widget=None, width=130):
    text_color = ORANGE if wind else TEXT_DIM
    
    row_frame = ctk.CTkFrame(parent, fg_color="transparent")
    row_frame.grid(row=row, column=0, sticky="ew", padx=16, pady=4)
    row_frame.grid_columnconfigure(1, weight=1)
    
    dot_text = "● " if wind else "  "
    lbl = ctk.CTkLabel(row_frame, text=f"{dot_text}{label_text}", text_color=text_color, font=(FONT_FAMILY, 13))
    lbl.grid(row=0, column=0, sticky="w")
    
    ent_container = ctk.CTkFrame(row_frame, fg_color="transparent")
    ent_container.grid(row=0, column=1, sticky="e")
    
    ent = ctk.CTkEntry(ent_container, textvariable=var, width=width, font=(FONT_MONO, 13), 
                       border_width=0, corner_radius=6, 
                       fg_color=BG_INPUT, text_color=TEXT, height=28)
    ent.pack(side="left")
    
    if extra_widget:
        extra_widget(ent_container).pack(side="left", padx=(6, 0))
        
    return ent

def create_card(parent, title, row, col, icon=""):
    card = ctk.CTkFrame(parent, fg_color=BG_CARD, corner_radius=12)
    card.grid(row=row, column=col, sticky="nsew", padx=6, pady=6)
    card.grid_columnconfigure(0, weight=1)
    
    header = ctk.CTkFrame(card, fg_color="transparent")
    header.grid(row=0, column=0, sticky="ew", padx=16, pady=(12, 4))
    
    title_lbl = ctk.CTkLabel(header, text=f"{icon} {title}" if icon else title, font=(FONT_FAMILY, 14, "bold"), text_color=TEXT)
    title_lbl.pack(side="left")
    
    content = ctk.CTkFrame(card, fg_color="transparent")
    content.grid(row=1, column=0, sticky="nsew", pady=(0, 10))
    content.grid_columnconfigure(0, weight=1)
    return content


class CollapsibleSection(ctk.CTkFrame):
    """可折叠面板: 点击标题行展开/收起内容"""
    def __init__(self, parent, title, expanded=False, **kw):
        kw.setdefault("fg_color", "transparent")
        super().__init__(parent, **kw)
        self.grid_columnconfigure(0, weight=1)
        self._expanded = expanded
        self._title = title
        arrow = "▼" if expanded else "▶"
        self.header_btn = ctk.CTkButton(
            self, text=f"{arrow}  {title}", command=self.toggle,
            anchor="w", fg_color="transparent", hover_color=BG_INPUT,
            text_color=TEXT_DIM, font=(FONT_FAMILY, 13, "bold"), height=28)
        self.header_btn.grid(row=0, column=0, sticky="ew", padx=10)
        self.content = ctk.CTkFrame(self, fg_color="transparent")
        self.content.grid_columnconfigure(0, weight=1)
        if expanded:
            self.content.grid(row=1, column=0, sticky="nsew", pady=(4, 0))

    def toggle(self):
        self._expanded = not self._expanded
        arrow = "▼" if self._expanded else "▶"
        self.header_btn.configure(text=f"{arrow}  {self._title}")
        if self._expanded:
            self.content.grid(row=1, column=0, sticky="nsew", pady=(4, 0))
        else:
            self.content.grid_remove()

# ── 主窗口 ────────────────────────────────────────────────
class CBPricerApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("Dark") # 默认深色
        
        self.title("CBPricer")
        self.geometry("1280x900")
        self.minsize(1100, 800)
        self.configure(fg_color=BG_APP)
        
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._build_vars()
        self._animating = False
        self._build_ui()

    # ── 变量 ──────────────────────────────────────────────
    def _build_vars(self):
        self.v_bond_code = ctk.StringVar()
        self.v_S0        = ctk.StringVar(value="55.0")
        self.v_K         = ctk.StringVar(value="52.77")
        self.v_face      = ctk.StringVar(value="100")
        self.v_redemp    = ctk.StringVar(value="107")
        self.v_cur_date  = ctk.StringVar(value=date.today().isoformat())
        self.v_mat_date  = ctk.StringVar(value="2026-07-30")
        self.v_iss_date  = ctk.StringVar(value="2020-07-30")
        self.v_conv_date = ctk.StringVar(value="2021-02-06")
        self.v_coupons   = ctk.StringVar(value="0.3,0.4,0.8,1.5,1.8,2.0")
        self.v_sigma     = ctk.StringVar(value="28")
        self.v_r         = ctk.StringVar(value="2.2")
        self.v_spread    = ctk.StringVar(value="3.0")
        self.v_p_down    = ctk.StringVar(value="0")
        self.v_dk        = ctk.StringVar(value="5")
        self.v_call_ratio  = ctk.StringVar(value="130")
        self.v_put_ratio   = ctk.StringVar(value="70")
        self.v_put_years   = ctk.StringVar(value="2")
        self.v_M           = ctk.StringVar(value="500")
        self.v_N           = ctk.StringVar(value="2000")
        self.v_result      = ctk.StringVar(value="—")
        self.v_status      = ctk.StringVar(value="就绪")
        self.v_ref_info    = ctk.StringVar(value="尚未拉取数据")
        self.v_vol_window  = ctk.StringVar(value=VOL_WINDOW_DEFAULT)
        self.v_theme       = ctk.StringVar(value="Dark")

        today = date.today()
        self.v_bt_start  = ctk.StringVar(value=(today - timedelta(days=180)).isoformat())
        self.v_bt_end    = ctk.StringVar(value=today.isoformat())
        self.v_bt_freq   = ctk.StringVar(value="周")
        self.v_bt_status = ctk.StringVar(value="输入转债代码 → 拉取参数 → 运行回测")

        # 价值分解 & 希腊值
        self.v_bond_floor   = ctk.StringVar(value="—")
        self.v_parity       = ctk.StringVar(value="—")
        self.v_option_prem  = ctk.StringVar(value="—")
        self.v_delta        = ctk.StringVar(value="—")
        self.v_gamma        = ctk.StringVar(value="—")
        self.v_vega         = ctk.StringVar(value="—")
        self.v_theta        = ctk.StringVar(value="—")

        # 隐含波动率反解 — 市价输入 & 反解结果
        self.v_market_price = ctk.StringVar(value="")
        self.v_iv           = ctk.StringVar(value="—")

        self.v_comparison   = ctk.StringVar(value="")

        self.v_sens_status  = ctk.StringVar(value="设置参数范围后点击运行")

        self._sens_figure   = None

        self._sens_canvas   = None



        self._last_stock_code = None
        self._last_credit = None
    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=0)
        self.grid_rowconfigure(1, weight=1)
        self.grid_rowconfigure(2, weight=0)
        self._build_header()
        self._build_tabview()
        self._build_statusbar()
        self._bind_shortcuts()

    def _build_header(self):
        self._tab_names = ["⚡ 定价", "📈 回测", "🔥 敏感性"]
        
        header = ctk.CTkFrame(self, fg_color=BG_CARD, corner_radius=0, height=60)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(0, weight=1)
        header.grid_columnconfigure(1, weight=0)
        header.grid_columnconfigure(2, weight=1)
        header.grid_propagate(False)

        left_frame = ctk.CTkFrame(header, fg_color="transparent")
        left_frame.grid(row=0, column=0, sticky="w", padx=20, pady=15)
        ctk.CTkLabel(left_frame, text="CBPricer", font=(FONT_FAMILY, 20, "bold"), text_color=TEXT).pack(side="left")
        ctk.CTkLabel(left_frame, text="PRO", font=(FONT_FAMILY, 10, "bold"), text_color=ACCENT, 
                     fg_color=BG_INPUT, corner_radius=4, padx=6, pady=2).pack(side="left", padx=(8, 16), pady=(0, 2))
                     
        self.theme_switch = ctk.CTkSwitch(left_frame, text="深色模式", command=self._toggle_theme, width=40, progress_color=ACCENT, font=(FONT_FAMILY, 12), text_color=TEXT_DIM)
        self.theme_switch.pack(side="left")
        self.theme_switch.select()

        self.tab_seg = ctk.CTkSegmentedButton(
            header, values=self._tab_names, command=self._switch_tab,
            font=(FONT_FAMILY, 13, "bold"), height=30,
            selected_color=ACCENT, selected_hover_color=ACCENT_HOVER,
            unselected_color=BG_INPUT, unselected_hover_color=BTN_HOVER,
            text_color=TEXT, text_color_disabled=TEXT_DIM,
            corner_radius=8)
        self.tab_seg.set("⚡ 定价")
        self.tab_seg.grid(row=0, column=1, pady=15)

        right_frame = ctk.CTkFrame(header, fg_color="transparent")
        right_frame.grid(row=0, column=2, sticky="e", padx=20, pady=15)
        
        ctk.CTkLabel(right_frame, text="💡 输入代码获取转债数据 👉", text_color=TEXT_DIM, font=(FONT_FAMILY, 12, "bold")).pack(side="left", padx=(0, 8))
        ctk.CTkEntry(right_frame, textvariable=self.v_bond_code, width=150,
                     font=(FONT_MONO, 13), placeholder_text="如 128009.SZ",
                     border_width=0, corner_radius=6, fg_color=BG_INPUT, height=30).pack(side="left")
        self.btn_wind = ctk.CTkButton(
            right_frame, text="Wind 同步", command=self._fetch_wind,
            fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color=("#ffffff", "#11111b"),
            font=(FONT_FAMILY, 12, "bold"), width=80, height=30, corner_radius=6)
        self.btn_wind.pack(side="left", padx=(8, 0))
        
        self.btn_save = ctk.CTkButton(right_frame, text="💾", command=self._save_preset, width=30, height=30, fg_color=BG_INPUT, hover_color=BTN_HOVER, text_color=TEXT, font=(FONT_FAMILY, 14), corner_radius=6)
        self.btn_save.pack(side="left", padx=(12, 0))
        self.btn_load = ctk.CTkButton(right_frame, text="📂", command=self._load_preset, width=30, height=30, fg_color=BG_INPUT, hover_color=BTN_HOVER, text_color=TEXT, font=(FONT_FAMILY, 14), corner_radius=6)
        self.btn_load.pack(side="left", padx=(6, 0))

    def _build_statusbar(self):
        sb = ctk.CTkFrame(self, fg_color=BG_CARD, corner_radius=0, height=28)
        sb.grid(row=2, column=0, sticky="ew")
        sb.grid_columnconfigure(1, weight=1)
        sb.grid_propagate(False)
        
        ctk.CTkLabel(sb, text="● 标橙参数由 Wind 自动填充", text_color=ORANGE, font=(FONT_FAMILY, 11)).grid(row=0, column=0, sticky="w", padx=15, pady=4)
        self.lbl_ref = ctk.CTkLabel(sb, textvariable=self.v_ref_info, text_color=TEXT_DIM, font=(FONT_FAMILY, 11))
        self.lbl_ref.grid(row=0, column=1, sticky="w", padx=15, pady=4)
        self.lbl_status = ctk.CTkLabel(sb, textvariable=self.v_status, text_color=TEXT, font=(FONT_FAMILY, 11, "bold"))
        self.lbl_status.grid(row=0, column=2, sticky="e", padx=15, pady=4)

    def _build_tabview(self):
        self._tab_frames = {}
        self._tab_container = ctk.CTkFrame(self, fg_color="transparent")
        self._tab_container.grid(row=1, column=0, sticky="nsew", padx=10, pady=10)
        self._tab_container.grid_columnconfigure(0, weight=1)
        self._tab_container.grid_rowconfigure(0, weight=1)

        for name in self._tab_names:
            f = ctk.CTkFrame(self._tab_container, fg_color="transparent")
            f.grid_columnconfigure(0, weight=1)
            f.grid_rowconfigure(0, weight=1)
            self._tab_frames[name] = f

        self._tab_frames["⚡ 定价"].grid(row=0, column=0, sticky="nsew")
        self._build_pricing_tab()
        self._build_backtest_tab()
        self._build_sensitivity_tab()

    def _switch_tab(self, selected):
        for name, f in self._tab_frames.items():
            if name == selected:
                f.grid(row=0, column=0, sticky="nsew")
            else:
                f.grid_remove()
    def _build_pricing_tab(self):
        tab = self._tab_frames["⚡ 定价"]
        tab.grid_columnconfigure(0, weight=0)
        tab.grid_columnconfigure(1, weight=1)
        tab.grid_rowconfigure(0, weight=1)

        # ── 左列: 参数面板 ──
        lp = ctk.CTkScrollableFrame(tab, fg_color="transparent", width=380,
                                    scrollbar_button_color=BORDER)
        lp.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        lp.grid_columnconfigure(0, weight=1)

        sec1 = create_card(lp, "基本条款", 0, 0, icon="📝")
        _form_row(sec1, "正股价 S", self.v_S0, 0, wind=True)
        _form_row(sec1, "转股价 K", self.v_K, 1, wind=True)
        _form_row(sec1, "面值", self.v_face, 2, wind=True)
        _form_row(sec1, "到期赎回价", self.v_redemp, 3, wind=True)
        _form_row(sec1, "估值日期", self.v_cur_date, 4)
        _form_row(sec1, "到期日期", self.v_mat_date, 5, wind=True)
        _form_row(sec1, "发行日期", self.v_iss_date, 6, wind=True)
        _form_row(sec1, "转股起始日", self.v_conv_date, 7, wind=True)
        _form_row(sec1, "各年票息 (%)", self.v_coupons, 8, wind=True, width=180)

        sec2 = create_card(lp, "模型参数", 1, 0, icon="⚙️")
        def make_vol(p):
            self.vol_window_menu = ctk.CTkOptionMenu(
                p, variable=self.v_vol_window, values=list(VOL_WINDOW_MAP.keys()),
                width=75, font=(FONT_FAMILY, 12), fg_color=BORDER, button_color=BTN_HOVER,
                text_color=TEXT, dropdown_fg_color=BG_INPUT, dropdown_text_color=TEXT,
                command=self._on_vol_window_change)
            return self.vol_window_menu
        _form_row(sec2, "波动率 σ (%)", self.v_sigma, 0, wind=True, width=80, extra_widget=make_vol)
        def make_shi(p):
            self.btn_shibor = ctk.CTkButton(
                p, text="Shibor", command=self._fetch_shibor, fg_color=BTN_CTRL,
                hover_color=BTN_HOVER, text_color=ORANGE,
                font=(FONT_FAMILY, 12, "bold"), width=75, height=28, corner_radius=6)
            return self.btn_shibor
        _form_row(sec2, "无风险利率 r (%)", self.v_r, 1, width=80, extra_widget=make_shi)
        def make_spr(p):
            self.btn_spread = ctk.CTkButton(
                p, text="按评级", command=self._fill_spread_from_rating, fg_color=BTN_CTRL,
                hover_color=BTN_HOVER, text_color=ORANGE,
                font=(FONT_FAMILY, 12, "bold"), width=75, height=28, corner_radius=6)
            return self.btn_spread
        _form_row(sec2, "信用利差 (%)", self.v_spread, 2, width=80, extra_widget=make_spr)
        _form_row(sec2, "下修概率 p (%)", self.v_p_down, 3)
        _form_row(sec2, "信用扩张系数 (%)", self.v_dk, 4)

        adv = CollapsibleSection(lp, "高级参数 (条款 & 网格)", expanded=False)
        adv.grid(row=2, column=0, sticky="ew", padx=6, pady=5)
        sec3 = create_card(adv.content, "条款触发条件", 0, 0, icon="⚡")
        _form_row(sec3, "强赎触发 (%K)", self.v_call_ratio, 0, wind=True)
        _form_row(sec3, "回售触发 (%K)", self.v_put_ratio, 1, wind=True)
        _form_row(sec3, "回售生效年数", self.v_put_years, 2, wind=True)
        sec4 = create_card(adv.content, "数值网格", 1, 0, icon="🧮")
        _form_row(sec4, "空间节点 M", self.v_M, 0)
        _form_row(sec4, "时间步数 N", self.v_N, 1)

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
        
        self.btn_calc = ctk.CTkButton(
            left_hero, text="✨ 开始计算 (Ctrl+Enter)", command=self._run_pricing,
            font=(FONT_FAMILY, 15, "bold"), width=200, height=50, corner_radius=10,
            fg_color=("#1e66f5", "#0052cc"), hover_color=("#7287fd", "#0066ff"),
            text_color=("#ffffff", "#ffffff"))
        self.btn_calc.pack(anchor="w", pady=(0, 15))
        
        self.progress_bar = ctk.CTkProgressBar(
            left_hero, orientation="horizontal", mode="indeterminate",
            width=200, height=4, corner_radius=2, progress_color=ACCENT, fg_color=BG_INPUT)
        self.progress_bar.pack(anchor="w", pady=(0, 10))
        self.progress_bar.set(0)

        right_hero = ctk.CTkFrame(rc, fg_color="transparent")
        right_hero.grid(row=0, column=1, sticky="ne", padx=30, pady=25)
        
        ctk.CTkLabel(right_hero, text="理论价格 (¥)", font=(FONT_FAMILY, 13),
                     text_color=TEXT_DIM).pack(anchor="e")
        self.lbl_result = ctk.CTkLabel(right_hero, textvariable=self.v_result,
                                       font=(FONT_FAMILY, 56, "bold"), text_color=TEXT)
        self.lbl_result.pack(anchor="e")

        # IV 工具栏
        tb = ctk.CTkFrame(rc, fg_color="transparent")
        tb.grid(row=1, column=0, columnspan=2, sticky="ew", padx=30, pady=(0, 25))
        
        ctk.CTkLabel(tb, text="🎯 隐含波动率反解", text_color=TEXT_DIM,
                     font=(FONT_FAMILY, 13, "bold")).pack(side="left", padx=(0, 15))
        ctk.CTkEntry(tb, textvariable=self.v_market_price, width=80,
                     font=(FONT_MONO, 13), fg_color=BG_INPUT, border_width=0, corner_radius=6,
                     placeholder_text="市价 ¥").pack(side="left", padx=(0, 8))
        self.btn_iv = ctk.CTkButton(
            tb, text="解 IV", command=self._solve_iv,
            fg_color=BTN_CTRL, hover_color=BTN_HOVER, text_color=ORANGE,
            font=(FONT_FAMILY, 12, "bold"), width=70, height=28, corner_radius=6)
        self.btn_iv.pack(side="left", padx=(0, 15))
        
        ctk.CTkLabel(tb, text="IV =", text_color=TEXT_DIM,
                     font=(FONT_FAMILY, 12)).pack(side="left", padx=(0, 4))
        ctk.CTkLabel(tb, textvariable=self.v_iv, text_color=ORANGE,
                     font=(FONT_MONO, 14, "bold"), width=70, anchor="w").pack(side="left", padx=(0, 20))
                     
        self.btn_conv = ctk.CTkButton(
            tb, text="🩺 收敛诊断", command=self._convergence_check,
            fg_color=BTN_CTRL, hover_color=BTN_HOVER, text_color=TEXT_DIM,
            font=(FONT_FAMILY, 12, "bold"), width=90, height=28, corner_radius=6)
        self.btn_conv.pack(side="right")

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

        _tile(dc, 0, 0, "🏷️ 纯债价值", self.v_bond_floor)
        _tile(dc, 0, 1, "🔄 转股价值", self.v_parity)
        _tile(dc, 0, 2, "✨ 期权溢价", self.v_option_prem, hl=True)
        _tile(dc, 0, 3, "Δ Delta", self.v_delta)
        _tile(dc, 1, 0, "Γ Gamma", self.v_gamma)
        _tile(dc, 1, 1, "ν Vega", self.v_vega)
        _tile(dc, 1, 2, "Θ Theta", self.v_theta)
        _tile(dc, 1, 3, "🎯 隐含波动率", self.v_iv)
    def _build_backtest_tab(self):
        tab = self._tab_frames["📈 回测"]
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(2, weight=1)

        # 控制栏
        ctrl = ctk.CTkFrame(tab, fg_color=BG_CARD, corner_radius=16)
        ctrl.grid(row=0, column=0, sticky="ew", pady=(6, 12), padx=6)

        ch = ctk.CTkFrame(ctrl, fg_color="transparent")
        ch.grid(row=0, column=0, sticky="ew", padx=20, pady=(15, 8))
        ctk.CTkLabel(ch, text="📈 历史回测对比",
                     font=(FONT_FAMILY, 16, "bold"), text_color=TEXT).pack(side="left")
        ctk.CTkLabel(ch, text="理论价 vs 实际收盘 (条款/模型参数 = 当前界面值)",
                     font=(FONT_FAMILY, 12), text_color=TEXT_DIM).pack(side="left", padx=(12, 0))

        cc = ctk.CTkFrame(ctrl, fg_color="transparent")
        cc.grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 15))
        ctk.CTkLabel(cc, text="开始", text_color=TEXT_DIM, font=(FONT_FAMILY, 13)).pack(side="left", padx=(0, 4))
        ctk.CTkEntry(cc, textvariable=self.v_bt_start, width=110, font=(FONT_MONO, 13),
                     fg_color=BG_INPUT, border_width=0, corner_radius=6).pack(side="left", padx=(0, 12))
        ctk.CTkLabel(cc, text="结束", text_color=TEXT_DIM, font=(FONT_FAMILY, 13)).pack(side="left", padx=(0, 4))
        ctk.CTkEntry(cc, textvariable=self.v_bt_end, width=110, font=(FONT_MONO, 13),
                     fg_color=BG_INPUT, border_width=0, corner_radius=6).pack(side="left", padx=(0, 12))
        ctk.CTkLabel(cc, text="频率", text_color=TEXT_DIM, font=(FONT_FAMILY, 13)).pack(side="left", padx=(0, 4))
        ctk.CTkOptionMenu(cc, variable=self.v_bt_freq, values=["日", "周", "月"],
                          width=70, font=(FONT_FAMILY, 12), fg_color=BG_INPUT, button_color=BTN_HOVER,
                          text_color=TEXT, dropdown_fg_color=BG_INPUT, dropdown_text_color=TEXT).pack(side="left", padx=(0, 15))
        self.btn_backtest = ctk.CTkButton(
            cc, text="📊 运行回测", command=self._run_backtest,
            fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color=("#ffffff", "#11111b"),
            font=(FONT_FAMILY, 13, "bold"), width=110, height=32, corner_radius=6)
        self.btn_backtest.pack(side="left")
        self.btn_bt_png = ctk.CTkButton(
            cc, text="📸 PNG", command=self._export_bt_png,
            fg_color=BG_INPUT, hover_color=BTN_HOVER, text_color=TEXT,
            font=(FONT_FAMILY, 12), width=75, height=32, corner_radius=6, state="disabled")
        self.btn_bt_png.pack(side="left", padx=(10, 0))
        self.btn_bt_csv = ctk.CTkButton(
            cc, text="📝 CSV", command=self._export_bt_csv,
            fg_color=BG_INPUT, hover_color=BTN_HOVER, text_color=TEXT,
            font=(FONT_FAMILY, 12), width=75, height=32, corner_radius=6, state="disabled")
        self.btn_bt_csv.pack(side="left", padx=(6, 0))

        self.lbl_bt_status = ctk.CTkLabel(
            tab, textvariable=self.v_bt_status,
            font=(FONT_FAMILY, 12), text_color=TEXT_DIM)
        self.lbl_bt_status.grid(row=1, column=0, sticky="w", padx=16, pady=(0, 6))

        self.bt_chart_frame = ctk.CTkFrame(tab, fg_color=BG_CARD, corner_radius=16)
        self.bt_chart_frame.grid(row=2, column=0, sticky="nsew", padx=6, pady=(0, 6))
        self.bt_chart_frame.grid_columnconfigure(0, weight=1)
        self.bt_chart_frame.grid_rowconfigure(0, weight=1)

    def _bind_shortcuts(self):
        self.bind_all("<Control-Return>", lambda e: self._run_pricing())
        self.bind_all("<Control-s>", lambda e: self._save_preset())
        self.bind_all("<Control-o>", lambda e: self._load_preset())
    def _build_sensitivity_tab(self):
        tab = self._tab_frames["🔥 敏感性"]
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(2, weight=1)

        ctrl = ctk.CTkFrame(tab, fg_color=BG_CARD, corner_radius=16)
        ctrl.grid(row=0, column=0, sticky="ew", pady=(6, 12), padx=6)

        ch = ctk.CTkFrame(ctrl, fg_color="transparent")
        ch.grid(row=0, column=0, sticky="ew", padx=20, pady=(15, 8))
        ctk.CTkLabel(ch, text="🔥 敏感性分析 (σ-S Heatmap)",
                     font=(FONT_FAMILY, 16, "bold"), text_color=TEXT).pack(side="left")
        ctk.CTkLabel(ch, text="固定其他参数，遍历 (波动率, 正股价) 网格",
                     font=(FONT_FAMILY, 12), text_color=TEXT_DIM).pack(side="left", padx=(12, 0))

        cc = ctk.CTkFrame(ctrl, fg_color="transparent")
        cc.grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 15))

        self.v_sens_s_min = ctk.StringVar(value="70")
        self.v_sens_s_max = ctk.StringVar(value="130")
        self.v_sens_sig_min = ctk.StringVar(value="10")
        self.v_sens_sig_max = ctk.StringVar(value="60")
        self.v_sens_steps = ctk.StringVar(value="12")

        ctk.CTkLabel(cc, text="S (%K)", text_color=TEXT_DIM, font=(FONT_FAMILY, 13)).pack(side="left", padx=(0, 4))
        ctk.CTkEntry(cc, textvariable=self.v_sens_s_min, width=50, font=(FONT_MONO, 13),
                     fg_color=BG_INPUT, border_width=0, corner_radius=6).pack(side="left", padx=(0, 2))
        ctk.CTkLabel(cc, text="~", text_color=TEXT_DIM, font=(FONT_FAMILY, 13)).pack(side="left", padx=2)
        ctk.CTkEntry(cc, textvariable=self.v_sens_s_max, width=50, font=(FONT_MONO, 13),
                     fg_color=BG_INPUT, border_width=0, corner_radius=6).pack(side="left", padx=(0, 15))

        ctk.CTkLabel(cc, text="σ (%)", text_color=TEXT_DIM, font=(FONT_FAMILY, 13)).pack(side="left", padx=(0, 4))
        ctk.CTkEntry(cc, textvariable=self.v_sens_sig_min, width=50, font=(FONT_MONO, 13),
                     fg_color=BG_INPUT, border_width=0, corner_radius=6).pack(side="left", padx=(0, 2))
        ctk.CTkLabel(cc, text="~", text_color=TEXT_DIM, font=(FONT_FAMILY, 13)).pack(side="left", padx=2)
        ctk.CTkEntry(cc, textvariable=self.v_sens_sig_max, width=50, font=(FONT_MONO, 13),
                     fg_color=BG_INPUT, border_width=0, corner_radius=6).pack(side="left", padx=(0, 15))

        ctk.CTkLabel(cc, text="网格", text_color=TEXT_DIM, font=(FONT_FAMILY, 13)).pack(side="left", padx=(0, 4))
        ctk.CTkEntry(cc, textvariable=self.v_sens_steps, width=40, font=(FONT_MONO, 13),
                     fg_color=BG_INPUT, border_width=0, corner_radius=6).pack(side="left", padx=(0, 15))

        self.btn_sensitivity = ctk.CTkButton(
            cc, text="🔥 运行分析", command=self._run_sensitivity,
            fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color=("#ffffff", "#11111b"),
            font=(FONT_FAMILY, 13, "bold"), width=110, height=32, corner_radius=6)
        self.btn_sensitivity.pack(side="left")

        self.lbl_sens_status = ctk.CTkLabel(
            tab, textvariable=self.v_sens_status, font=(FONT_FAMILY, 12), text_color=TEXT_DIM)
        self.lbl_sens_status.grid(row=1, column=0, sticky="sw", padx=16, pady=(0, 6))

        self.sens_chart_frame = ctk.CTkFrame(tab, fg_color=BG_CARD, corner_radius=16)
        self.sens_chart_frame.grid(row=2, column=0, sticky="nsew", padx=6, pady=(0, 6))
        self.sens_chart_frame.grid_columnconfigure(0, weight=1)
        self.sens_chart_frame.grid_rowconfigure(0, weight=1)

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
            grid = np.zeros((steps, steps))
            total = steps * steps
            done = [0]

            def compute_one(i, j):
                p = dict(params["pricer"], S0=float(S_vals[j]))
                pricer = UniversalCBPricer(**p)
                return i, j, float(pricer.price(
                    sigma=float(sig_vals[i]), r=m_fast["r"],
                    base_spread=m_fast["base_spread"], p_down=m_fast["p_down"],
                    distress_k=m_fast["distress_k"], M=m_fast["M"], N=m_fast["N"]))

            with ThreadPoolExecutor(max_workers=4) as pool:
                futs = [pool.submit(compute_one, i, j)
                        for i in range(steps) for j in range(steps)]
                for fut in as_completed(futs):
                    i, j, v = fut.result()
                    grid[i, j] = v
                    done[0] += 1
                    if done[0] % max(1, total // 20) == 0:
                        self.after(0, lambda d=done[0]: self.v_sens_status.set(
                            f"进度 {d}/{total} ..."))

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
        self.v_sens_status.set(
            f"✅ {len(S_vals)}×{len(sig_vals)} = {len(S_vals)*len(sig_vals)} 点  |  "
            f"价格范围 {float(np.min(grid)):.2f} ~ {float(np.max(grid)):.2f}")

    def _toggle_theme(self):
        """切换深浅色模式"""
        if self.theme_switch.get() == 1:
            ctk.set_appearance_mode("Dark")
            self.theme_switch.configure(text="深色模式")
        else:
            ctk.set_appearance_mode("Light")
            self.theme_switch.configure(text="浅色模式")
            
        # 刷新 matplotlib 图表色彩
        if self._last_bt_result is not None:
            self._render_backtest_chart(self._last_bt_result)

    # ── 进度动画 ────────────────────────────────────────
    def _start_progress(self, base_msg):
        self._animating = True
        self._anim_base = base_msg
        self._anim_step = 0
        if hasattr(self, 'progress_bar'):
            self.progress_bar.start()
        self._tick_progress()

    def _tick_progress(self):
        if not self._animating:
            return
        dots = "." * (self._anim_step % 4)
        self.v_status.set(f"{self._anim_base}{dots}")
        self._anim_step += 1
        self.after(400, self._tick_progress)

    def _stop_progress(self):
        self._animating = False
        if hasattr(self, 'progress_bar'):
            self.progress_bar.stop()
            self.progress_bar.set(0)

    # ── Wind 获取 ────────────────────────────────────────
    def _fetch_wind(self):
        code = self.v_bond_code.get().strip()
        if not code:
            messagebox.showwarning("提示", "请先输入转债代码, 例如 128009.SZ")
            return
        self.btn_wind.configure(state="disabled")
        self._start_progress(f"正在从 Wind 获取 {code} 数据")
        threading.Thread(target=self._fetch_wind_worker, args=(code,), daemon=True).start()

    def _fetch_wind_worker(self, code):
        try:
            w = ensure_wind()
            val_date = date.today()
            val_str = val_date.strftime("%Y%m%d")

            fields = [
                "sec_name", "underlyingcode", "ipo_date", "maturitydate",
                "latestpar",
                "clause_conversion2_swapshareprice",
                "clause_calloption_redemptionprice",
                "clause_calloption_triggerproportion",
                "clause_putoption_redeem_triggerproportion",
                "clause_putoption_putbackperiodobs",
                "couponrate",
                "close", "creditrating", "outstandingbalance",
            ]
            res = w.wss(code, ",".join(fields), f"tradeDate={val_str}")
            if res.ErrorCode != 0:
                raise RuntimeError(f"Wind 返回错误: {res.Data}")
            data = {f.lower(): d[0] for f, d in zip(res.Fields, res.Data)}

            stock_code = data.get("underlyingcode")
            if not stock_code:
                raise ValueError("未返回标的正股代码")

            res_s = w.wss(stock_code, "close", f"tradeDate={val_str};priceAdj=U")
            if res_s.ErrorCode != 0:
                raise RuntimeError(f"取正股现价失败: {res_s.Data}")
            S0 = float(res_s.Data[0][0])

            vol_win_days = VOL_WINDOW_MAP.get(self.v_vol_window.get(), 126)
            try:
                sigma = hist_vol(w, stock_code, val_date, vol_win_days)
            except Exception:
                sigma = None

            # 顺带拉 Shibor 1Y 作为无风险利率推荐值
            shibor_rate = None
            try:
                rr = w.edb("SHIBOR1Y.IR",
                           (val_date - timedelta(days=10)).isoformat(),
                           val_date.isoformat())
                if rr.ErrorCode == 0 and rr.Data and rr.Data[0]:
                    vals = [v for v in rr.Data[0] if v is not None]
                    if vals:
                        shibor_rate = float(vals[-1])
            except Exception:
                shibor_rate = None

            iss_dt = to_date(data["ipo_date"])
            conv_dt = iss_dt + timedelta(days=180) if iss_dt else None

            cf = fetch_cashflow(w, code)
            if cf and cf["coupon_rates"]:
                coupons_tuple = cf["coupon_rates"]
                coupon_src = "cashflow"
            else:
                coupons_tuple = parse_coupon(data.get("couponrate"))
                coupon_src = "couponrate"

            mat_dt = (cf["maturity_date"] if cf and cf["maturity_date"] else to_date(data["maturitydate"]))

            if cf and cf["redemption_price"] is not None:
                redemp = float(cf["redemption_price"])
            elif data.get("clause_calloption_redemptionprice") is not None:
                redemp = float(data["clause_calloption_redemptionprice"])
            else:
                redemp = 107.0

            put_obs_months = data.get("clause_putoption_putbackperiodobs")
            put_years = None
            if put_obs_months is not None and iss_dt and mat_dt:
                total_months = (mat_dt - iss_dt).days / 30.4375
                put_years = int(round(max(0, (total_months - float(put_obs_months)) / 12)))

            self.after(0, self._fill_wind_data, {
                "S0": S0,
                "K": float(data["clause_conversion2_swapshareprice"]),
                "face": float(data.get("latestpar") or 100.0),
                "mat_date": mat_dt,
                "iss_date": iss_dt,
                "conv_date": conv_dt,
                "redemp": float(redemp),
                "call_ratio": data.get("clause_calloption_triggerproportion"),
                "put_ratio": data.get("clause_putoption_redeem_triggerproportion"),
                "put_years": put_years,
                "coupons_tuple": coupons_tuple,
                "coupon_src": coupon_src,
                "sigma": sigma,
                "shibor": shibor_rate,
                "stock_code": stock_code,
                "sec_name": data.get("sec_name"),
                "close": data.get("close"),
                "credit": data.get("creditrating"),
                "outstanding": data.get("outstandingbalance"),
            })
        except Exception as exc:
            err_msg = f"Wind 获取失败: {exc}"
            self.after(0, self._on_error, err_msg)
        finally:
            self.after(0, self._stop_progress)
            self.after(0, lambda: self.btn_wind.configure(state="normal"))

    def _fill_wind_data(self, d):
        self.v_S0.set(f"{d['S0']:.4f}")
        self.v_K.set(f"{d['K']:.2f}")
        self.v_face.set(f"{d['face']:.0f}")
        self.v_cur_date.set(date.today().isoformat())
        self.v_mat_date.set(d["mat_date"].isoformat() if d["mat_date"] else "")
        self.v_iss_date.set(d["iss_date"].isoformat() if d["iss_date"] else "")
        self.v_conv_date.set(d["conv_date"].isoformat() if d["conv_date"] else "")
        self.v_redemp.set(f"{d['redemp']:.1f}")
        if d.get("call_ratio") is not None:
            self.v_call_ratio.set(f"{float(d['call_ratio']):.0f}")
        if d.get("put_ratio") is not None:
            self.v_put_ratio.set(f"{float(d['put_ratio']):.0f}")
        if d.get("put_years") is not None:
            self.v_put_years.set(f"{int(d['put_years'])}")
        if d["sigma"] is not None:
            self.v_sigma.set(f"{d['sigma'] * 100:.2f}")
        if d.get("shibor") is not None:
            self.v_r.set(f"{d['shibor']:.2f}")
        
        parsed = d.get("coupons_tuple")
        if parsed:
            self.v_coupons.set(",".join(f"{c*100:.2f}" for c in parsed))

        self._last_stock_code = d.get("stock_code")
        self._last_credit = d.get("credit")

        if d.get("credit") and d["credit"] in CREDIT_SPREAD_TABLE:
            self.v_spread.set(f"{CREDIT_SPREAD_TABLE[d['credit']]:.1f}")

        if d.get("close") is not None:
            self.v_market_price.set(f"{float(d['close']):.2f}")

        ref_parts = []
        if d.get("sec_name"):
            ref_parts.append(str(d["sec_name"]))
        if d.get("close") is not None:
            ref_parts.append(f"市价 {float(d['close']):.2f}")
        if d.get("credit"):
            ref_parts.append(f"评级 {d['credit']}")
        if d.get("outstanding") is not None:
            ref_parts.append(f"剩余规模 {float(d['outstanding']):.2f} 亿")
        self.v_ref_info.set("  ·  ".join(ref_parts) if ref_parts else "—")

        coupon_src = d.get("coupon_src", "couponrate")
        src_tag = "付息计划" if coupon_src == "cashflow" else "票面字段"
        self.v_status.set(
            f"已获取 {self.v_bond_code.get()} (正股 {d['stock_code']}, S₀={d['S0']:.3f}, 票息: {src_tag})"
        )

    # ── 波动率窗口切换 ────────────────────────────────────
    def _on_vol_window_change(self, choice):
        if not self._last_stock_code:
            return
        days = VOL_WINDOW_MAP.get(choice, 126)
        self._start_progress(f"重算波动率 ({choice})")
        self.vol_window_menu.configure(state="disabled")
        threading.Thread(
            target=self._recompute_vol_worker,
            args=(self._last_stock_code, days),
            daemon=True,
        ).start()

    def _recompute_vol_worker(self, stock_code, days):
        try:
            w = ensure_wind()
            sigma = hist_vol(w, stock_code, date.today(), days)
            self.after(0, lambda: self.v_sigma.set(f"{sigma * 100:.2f}"))
            self.after(0, lambda: self.v_status.set(
                f"已按 {self.v_vol_window.get()} 窗口重算 σ = {sigma*100:.2f}%"
            ))
        except Exception as exc:
            self.after(0, self._on_error, f"重算 σ 失败: {exc}")
        finally:
            self.after(0, self._stop_progress)
            self.after(0, lambda: self.vol_window_menu.configure(state="normal"))

    # ── Shibor 1Y ────────────────────────────────────────
    def _fetch_shibor(self):
        self.btn_shibor.configure(state="disabled")
        self._start_progress("拉取 Shibor 1Y")
        threading.Thread(target=self._fetch_shibor_worker, daemon=True).start()

    def _fetch_shibor_worker(self):
        try:
            w = ensure_wind()
            r = w.edb("SHIBOR1Y.IR",
                      (date.today() - timedelta(days=10)).isoformat(),
                      date.today().isoformat())
            if r.ErrorCode != 0 or not r.Data or not r.Data[0]:
                raise RuntimeError(f"Wind edb 返回空: {r.Data}")
            vals = [v for v in r.Data[0] if v is not None]
            if not vals:
                raise RuntimeError("近 10 天 Shibor 1Y 全为空")
            latest = float(vals[-1])
            self.after(0, lambda: self.v_r.set(f"{latest:.2f}"))
            self.after(0, lambda: self.v_status.set(f"Shibor 1Y = {latest:.4f}%"))
        except Exception as exc:
            self.after(0, self._on_error, f"Shibor 拉取失败: {exc}")
        finally:
            self.after(0, self._stop_progress)
            self.after(0, lambda: self.btn_shibor.configure(state="normal"))

    # ── 按评级填入信用利差 ────────────────────────────────
    def _fill_spread_from_rating(self):
        if not self._last_credit:
            messagebox.showinfo("提示", "请先从 Wind 获取参数, 取得评级后再按此按钮")
            return
        if self._last_credit not in CREDIT_SPREAD_TABLE:
            messagebox.showwarning(
                "提示",
                f"评级 '{self._last_credit}' 不在经验表中\n"
                f"已知评级: {', '.join(CREDIT_SPREAD_TABLE.keys())}"
            )
            return
        val = CREDIT_SPREAD_TABLE[self._last_credit]
        self.v_spread.set(f"{val:.1f}")
        self.v_status.set(f"按评级 {self._last_credit} 填入信用利差 {val:.1f}%")

    # ── 定价计算 ──────────────────────────────────────────
    def _run_pricing(self):
        self.v_result.set("…")
        self.lbl_result.configure(text_color=get_color(TEXT_DIM))
        self.btn_calc.configure(state="disabled")
        self._start_progress("正在计算理论价格")
        threading.Thread(target=self._pricing_worker, daemon=True).start()

    def _pricing_worker(self):
        try:
            params = self._collect_params()
            pricer = UniversalCBPricer(**params["pricer"])
            result = pricer.price(**params["model"], return_greeks=True)
            self.after(0, self._show_result, result, pricer)
        except Exception as exc:
            err_msg = f"计算失败: {exc}"
            self.after(0, self._on_error, err_msg)
        finally:
            self.after(0, self._stop_progress)
            self.after(0, lambda: self.btn_calc.configure(state="normal"))

    def _collect_params(self):
        def pf(v):
            val = v.get().strip()
            try:
                return float(val)
            except ValueError:
                raise ValueError(f"请输入有效数字，当前值: '{val}'")
        def pd(v):
            val = v.get().strip()
            try:
                return date.fromisoformat(val)
            except ValueError:
                raise ValueError(f"日期格式应为 YYYY-MM-DD，当前值: '{val}'")

        coupon_str = self.v_coupons.get().strip()
        coupon_rates = tuple(float(x.strip()) / 100.0
                             for x in coupon_str.split(",") if x.strip())

        pricer = dict(
            S0=pf(self.v_S0),
            K=pf(self.v_K),
            face_value=pf(self.v_face),
            redemption_price=pf(self.v_redemp),
            current_date=pd(self.v_cur_date),
            maturity_date=pd(self.v_mat_date),
            issue_date=pd(self.v_iss_date),
            conversion_start_date=pd(self.v_conv_date),
            coupon_rates=coupon_rates,
            call_trigger_ratio=pf(self.v_call_ratio) / 100.0,
            put_trigger_ratio=pf(self.v_put_ratio) / 100.0,
            put_active_years=int(pf(self.v_put_years)),
        )
        model = dict(
            sigma=pf(self.v_sigma) / 100.0,
            r=pf(self.v_r) / 100.0,
            base_spread=pf(self.v_spread) / 100.0,
            p_down=pf(self.v_p_down) / 100.0,
            distress_k=pf(self.v_dk) / 100.0,
            M=int(pf(self.v_M)),
            N=int(pf(self.v_N)),
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

    def _show_result(self, result, pricer):
        theo = result["price"] if isinstance(result, dict) else result
        self.v_result.set(f"{theo:.3f}")
        info = (
            f"S₀={pricer.S0:.3f}  K={pricer.K:.2f}  "
            f"T={pricer.T:.4f}年  "
            f"σ={float(self.v_sigma.get()):.1f}%  "
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


    def _animate_result(self, target, steps=15, interval=25):
        """结果数字跳动动画"""
        try:
            current = float(self.v_result.get())
        except (ValueError, TypeError):
            current = 0.0
        for i in range(1, steps + 1):
            val = current + (target - current) * (i / steps)
            self.after(i * interval, lambda v=val: self.v_result.set(f"{v:.3f}"))

    def _on_error(self, msg):
        self._stop_progress()
        self.v_status.set(f"❌ {msg}")
        self.v_ref_info.set(f"❌ 获取失败")
        self.v_result.set("—")
        self.lbl_result.configure(text_color=RED)
        messagebox.showerror("错误", str(msg))

    # ── 历史回测 ────────────────────────────────────────────
    def _run_backtest(self):
        code = self.v_bond_code.get().strip()
        if not code:
            messagebox.showwarning("提示", "请先输入转债代码")
            return
        try:
            start = date.fromisoformat(self.v_bt_start.get().strip())
            end = date.fromisoformat(self.v_bt_end.get().strip())
        except ValueError:
            messagebox.showerror("错误", "日期格式应为 YYYY-MM-DD")
            return
        if start >= end:
            messagebox.showerror("错误", "开始日期应早于结束日期")
            return

        freq_map = {"日": "D", "周": "W", "月": "M"}
        freq = freq_map.get(self.v_bt_freq.get(), "W")

        try:
            params = dict(
                r=float(self.v_r.get()) / 100.0,
                base_spread=float(self.v_spread.get()) / 100.0,
                p_down=float(self.v_p_down.get()) / 100.0,
                distress_k=float(self.v_dk.get()) / 100.0,
                M=int(float(self.v_M.get())),
                N=int(float(self.v_N.get())),
                vol_window_days=VOL_WINDOW_MAP.get(self.v_vol_window.get(), 21),
            )
        except ValueError as e:
            messagebox.showerror("错误", f"参数解析失败: {e}")
            return

        self.btn_backtest.configure(state="disabled")
        self.v_bt_status.set(f"正在回测 {code} {start} → {end} ({self.v_bt_freq.get()}频) ...")
        threading.Thread(
            target=self._backtest_worker,
            args=(code, start, end, freq, params),
            daemon=True,
        ).start()

    def _backtest_worker(self, code, start, end, freq, params):
        try:
            def progress(i, total):
                self.after(0, lambda: self.v_bt_status.set(
                    f"进度 {i}/{total} ..."
                ))

            result = backtest_theoretical_price(
                code, start_date=start, end_date=end, freq=freq,
                progress_cb=progress, **params,
            )
            self._last_bt_result = result
            self.after(0, self._render_backtest_chart, result)
        except Exception as exc:
            self.after(0, lambda: self.v_bt_status.set(f"❌ 回测失败: {exc}"))
            self.after(0, lambda: messagebox.showerror("回测失败", str(exc)))
        finally:
            self.after(0, lambda: self.btn_backtest.configure(state="normal"))

    def _render_backtest_chart(self, result):
        dates = result["dates"]
        theo = result["theo_prices"]
        mkt = result["market_prices"]

        if not dates:
            self.v_bt_status.set("❌ 无有效采样点")
            return

        # 释放旧图表资源，防止内存泄漏
        if self._bt_figure is not None:
            self._bt_figure.clf()
            plt.close(self._bt_figure)
            self._bt_figure = None
            self._bt_canvas = None

        for child in self.bt_chart_frame.winfo_children():
            child.destroy()

        # 根据当前深浅色模式获取真实 HEX
        bg_card_color = get_color(BG_CARD)
        bg_input_color = get_color(BG_INPUT)
        text_dim_color = get_color(TEXT_DIM)
        text_color = get_color(TEXT)
        border_color = get_color(BORDER)
        accent_color = get_color(ACCENT)
        orange_color = get_color(ORANGE)
        red_color = get_color(RED)
        green_color = get_color(GREEN)

        fig = Figure(figsize=(11, 5), dpi=100, facecolor=bg_card_color)
        ax = fig.add_subplot(111, facecolor=bg_input_color)

        ax.plot(dates, theo, color=accent_color, linewidth=2.0, marker="o", markersize=4,
                label="理论价", zorder=3)
        ax.plot(dates, mkt, color=orange_color, linewidth=2.0, marker="s", markersize=4,
                label="市价(收盘)", zorder=2)

        # numpy already imported at module level
        theo_arr = np.array(theo)
        mkt_arr = np.array(mkt)
        ax.fill_between(dates, theo_arr, mkt_arr,
                        where=(mkt_arr >= theo_arr).tolist(), color=red_color, alpha=0.12, label="市价溢价")
        ax.fill_between(dates, theo_arr, mkt_arr,
                        where=(mkt_arr < theo_arr).tolist(), color=green_color, alpha=0.12, label="市价折价")

        ax.set_xlabel("日期", color=text_dim_color, fontsize=10)
        ax.set_ylabel("价格", color=text_dim_color, fontsize=10)
        ax.tick_params(colors=text_dim_color, labelsize=9)
        for spine in ax.spines.values():
            spine.set_color(border_color)
        ax.grid(True, color=border_color, linestyle="--", alpha=0.4)

        legend = ax.legend(loc="best", framealpha=0.9, facecolor=bg_card_color,
                           edgecolor=border_color, fontsize=9, labelcolor=text_color)
        legend.get_frame().set_linewidth(0.5)

        fig.autofmt_xdate(rotation=25)
        fig.tight_layout()

        canvas = FigureCanvasTkAgg(fig, master=self.bt_chart_frame)
        canvas.draw()
        canvas.get_tk_widget().grid(row=0, column=0, sticky="nsew")

        self._bt_figure = fig
        self._bt_canvas = canvas

        mean_basis = float(np.mean(mkt_arr - theo_arr))
        corr = float(np.corrcoef(theo_arr, mkt_arr)[0, 1]) if len(theo) > 1 else float("nan")
        self.v_bt_status.set(
            f"✅ {len(dates)} 个采样点  ·  平均基差(市价-理论)={mean_basis:+.2f}  ·  相关系数={corr:.3f}"
        )
        self.btn_bt_png.configure(state="normal")
        self.btn_bt_csv.configure(state="normal")

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

    # ── 收敛诊断 ────────────────────────────────────────────
    def _convergence_check(self):
        self.btn_conv.configure(state="disabled")
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
            self.after(0, lambda: self.btn_conv.configure(state="normal"))

    # ── 参数预设保存/加载 ──────────────────────────────────
    _PRESET_VARS = (
        "v_bond_code", "v_S0", "v_K", "v_face", "v_redemp",
        "v_cur_date", "v_mat_date", "v_iss_date", "v_conv_date",
        "v_coupons", "v_sigma", "v_r", "v_spread", "v_p_down", "v_dk",
        "v_call_ratio", "v_put_ratio", "v_put_years", "v_M", "v_N",
        "v_vol_window", "v_market_price",
        "v_bt_start", "v_bt_end", "v_bt_freq",
    )

    def _save_preset(self):
        path = filedialog.asksaveasfilename(
            title="保存参数预设",
            defaultextension=".json",
            filetypes=[("JSON", "*.json"), ("所有文件", "*.*")],
            initialfile=(self.v_bond_code.get().strip() or "cb_preset") + ".json",
        )
        if not path:
            return
        try:
            data = {name: getattr(self, name).get() for name in self._PRESET_VARS}
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            self.v_status.set(f"已保存预设到 {path}")
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc))

    def _load_preset(self):
        path = filedialog.askopenfilename(
            title="加载参数预设",
            filetypes=[("JSON", "*.json"), ("所有文件", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for name in self._PRESET_VARS:
                if name in data:
                    getattr(self, name).set(data[name])
            self.v_status.set(f"已加载预设 {path}")
        except Exception as exc:
            messagebox.showerror("加载失败", str(exc))

    # ── 回测结果导出 ──────────────────────────────────────
    def _export_bt_png(self):
        if self._bt_figure is None:
            messagebox.showinfo("提示", "请先运行回测")
            return
        path = filedialog.asksaveasfilename(
            title="导出回测图",
            defaultextension=".png",
            filetypes=[("PNG", "*.png"), ("PDF", "*.pdf"), ("SVG", "*.svg")],
            initialfile=(self.v_bond_code.get().strip() or "backtest") + ".png",
        )
        if not path:
            return
        try:
            self._bt_figure.savefig(path, dpi=150, bbox_inches="tight",
                                    facecolor=self._bt_figure.get_facecolor())
            self.v_bt_status.set(f"已导出图表到 {path}")
        except Exception as exc:
            messagebox.showerror("导出失败", str(exc))

    def _export_bt_csv(self):
        if not self._last_bt_result or not self._last_bt_result.get("dates"):
            messagebox.showinfo("提示", "请先运行回测")
            return
        path = filedialog.asksaveasfilename(
            title="导出回测序列",
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv"), ("所有文件", "*.*")],
            initialfile=(self.v_bond_code.get().strip() or "backtest") + ".csv",
        )
        if not path:
            return
        try:
            r = self._last_bt_result
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f)
                w.writerow(["date", "theoretical_price", "market_price", "stock_price", "sigma"])
                for d, t, m, s, sg in zip(r["dates"], r["theo_prices"],
                                          r["market_prices"], r["stock_prices"], r["sigmas"]):
                    w.writerow([d.isoformat(), f"{t:.4f}", f"{m:.4f}", f"{s:.4f}", f"{sg:.6f}"])
            self.v_bt_status.set(f"已导出 {len(r['dates'])} 条记录到 {path}")
        except Exception as exc:
            messagebox.showerror("导出失败", str(exc))


# ── 启动 ──────────────────────────────────────────────────
def main():
    app = CBPricerApp()
    app.mainloop()

if __name__ == "__main__":
    main()
