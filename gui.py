#!/usr/bin/env python3
"""
可转债理论定价 GUI
支持手动输入参数 & 通过 Wind 代码自动拉取
最美观优雅的极简 Apple-inspired 设计，支持深/浅色模式无缝切换
"""

import customtkinter as ctk
from tkinter import messagebox
from datetime import date, datetime, timedelta
import threading
import numpy as np

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

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

FONT_FAMILY = "SF Pro Display"
FONT_MONO = "SF Mono"

# 历史波动率窗口选项 (交易日数)
VOL_WINDOW_MAP = {"1M": 21, "2M": 42, "3M": 63, "6M": 126, "1Y": 252}
VOL_WINDOW_DEFAULT = "6M"

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
def _form_row(parent, label_text, var, row, wind=False, extra_widget=None, width=150):
    text_color = ORANGE if wind else TEXT_DIM
    
    row_frame = ctk.CTkFrame(parent, fg_color="transparent")
    row_frame.grid(row=row, column=0, sticky="ew", padx=25, pady=8)
    row_frame.grid_columnconfigure(1, weight=1)
    
    dot_text = "●  " if wind else "    "
    lbl = ctk.CTkLabel(row_frame, text=f"{dot_text}{label_text}", text_color=text_color, font=(FONT_FAMILY, 14))
    lbl.grid(row=0, column=0, sticky="w")
    
    ent_container = ctk.CTkFrame(row_frame, fg_color="transparent")
    ent_container.grid(row=0, column=1, sticky="e")
    
    ent = ctk.CTkEntry(ent_container, textvariable=var, width=width, font=(FONT_MONO, 14), 
                       border_width=1, border_color=BORDER, corner_radius=6, 
                       fg_color=BG_INPUT, text_color=TEXT)
    ent.pack(side="left")
    
    if extra_widget:
        extra_widget(ent_container).pack(side="left", padx=(8, 0))
        
    return ent

def create_card(parent, title, row, col):
    card = ctk.CTkFrame(parent, fg_color=BG_CARD, corner_radius=16, border_width=1, border_color=BORDER)
    card.grid(row=row, column=col, sticky="nsew", padx=10, pady=10)
    card.grid_columnconfigure(0, weight=1)
    
    header = ctk.CTkFrame(card, fg_color="transparent")
    header.grid(row=0, column=0, sticky="ew", padx=25, pady=(20, 10))
    
    title_lbl = ctk.CTkLabel(header, text=title, font=(FONT_FAMILY, 16, "bold"), text_color=TEXT)
    title_lbl.pack(side="left")
    
    content = ctk.CTkFrame(card, fg_color="transparent")
    content.grid(row=1, column=0, sticky="nsew", pady=(0, 15))
    content.grid_columnconfigure(0, weight=1)
    return content


# ── 主窗口 ────────────────────────────────────────────────
class CBPricerApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("Dark") # 默认深色
        
        self.title("可转债理论定价系统")
        self.geometry("1060x900")
        self.minsize(980, 800)
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
        self.v_ref_info    = ctk.StringVar(value="输入代码获取转债数据...")
        self.v_vol_window  = ctk.StringVar(value=VOL_WINDOW_DEFAULT)
        self.v_theme       = ctk.StringVar(value="Dark")

        today = date.today()
        self.v_bt_start  = ctk.StringVar(value=(today - timedelta(days=180)).isoformat())
        self.v_bt_end    = ctk.StringVar(value=today.isoformat())
        self.v_bt_freq   = ctk.StringVar(value="周")
        self.v_bt_status = ctk.StringVar(value="输入转债代码 → 拉取参数 → 运行回测")

        self._last_stock_code = None
        self._last_credit = None
        self._bt_canvas = None
        self._bt_figure = None
        self._last_bt_result = None

    # ── UI ────────────────────────────────────────────────
    def _build_ui(self):
        self.main_frame = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.main_frame.grid(row=0, column=0, sticky="nsew", padx=20, pady=20)
        self.main_frame.grid_columnconfigure(0, weight=1, uniform="col")
        self.main_frame.grid_columnconfigure(1, weight=1, uniform="col")

        # ── 顶部栏：搜索与拉取 ──
        wind_card = ctk.CTkFrame(self.main_frame, fg_color=BG_CARD, corner_radius=16, border_width=1, border_color=BORDER)
        wind_card.grid(row=0, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 10))
        wind_card.grid_columnconfigure(1, weight=1)
        
        # 顶部标题 & 主题切换
        title_frame = ctk.CTkFrame(wind_card, fg_color="transparent")
        title_frame.grid(row=0, column=0, padx=25, pady=(25, 5), sticky="w")
        
        lbl_wind_title = ctk.CTkLabel(title_frame, text="⚡ 智能定价引擎", font=(FONT_FAMILY, 20, "bold"), text_color=ACCENT)
        lbl_wind_title.pack(side="left")
        
        # 深浅色切换 Switch
        self.theme_switch = ctk.CTkSwitch(title_frame, text="深色模式", command=self._toggle_theme, 
                                          progress_color=ACCENT, button_color=TEXT_DIM, button_hover_color=TEXT,
                                          font=(FONT_FAMILY, 13), text_color=TEXT)
        self.theme_switch.pack(side="left", padx=(20, 0))
        self.theme_switch.select() # 默认开启深色
        
        search_frame = ctk.CTkFrame(wind_card, fg_color="transparent")
        search_frame.grid(row=0, column=1, padx=25, pady=(25, 5), sticky="e")
        
        ent_code = ctk.CTkEntry(search_frame, textvariable=self.v_bond_code, width=180, font=(FONT_MONO, 15),
                                placeholder_text="例: 128009.SZ", border_width=1, corner_radius=8, fg_color=BG_INPUT)
        ent_code.pack(side="left", padx=(0, 12))
        
        self.btn_wind = ctk.CTkButton(search_frame, text="自动拉取 (Wind)", command=self._fetch_wind,
                                      fg_color=ACCENT, hover_color="#74c7ec", text_color="#11111b",
                                      font=(FONT_FAMILY, 14, "bold"), width=140, height=36, corner_radius=8)
        self.btn_wind.pack(side="left")

        ref_frame = ctk.CTkFrame(wind_card, fg_color="transparent")
        ref_frame.grid(row=1, column=0, columnspan=2, sticky="ew", padx=25, pady=(0, 20))
        
        lbl_legend = ctk.CTkLabel(ref_frame, text="● 橙色字段将自动填充", text_color=ORANGE, font=(FONT_FAMILY, 12))
        lbl_legend.pack(side="left")
        self.lbl_ref = ctk.CTkLabel(ref_frame, textvariable=self.v_ref_info, text_color=TEXT_DIM, font=(FONT_FAMILY, 13))
        self.lbl_ref.pack(side="right")

        # ── 左列 ──
        col0_frame = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        col0_frame.grid(row=1, column=0, sticky="nsew")
        col0_frame.grid_columnconfigure(0, weight=1)
        
        sec1 = create_card(col0_frame, "基本条款", 0, 0)
        _form_row(sec1, "正股价 S", self.v_S0, 0, wind=True)
        _form_row(sec1, "转股价 K", self.v_K, 1, wind=True)
        _form_row(sec1, "面值", self.v_face, 2, wind=True)
        _form_row(sec1, "到期赎回价", self.v_redemp, 3, wind=True)
        _form_row(sec1, "估值日期", self.v_cur_date, 4)
        _form_row(sec1, "到期日期", self.v_mat_date, 5, wind=True)
        _form_row(sec1, "发行日期", self.v_iss_date, 6, wind=True)
        _form_row(sec1, "转股起始日", self.v_conv_date, 7, wind=True)
        _form_row(sec1, "各年票息 (%)", self.v_coupons, 8, wind=True, width=200)

        # ── 右列 ──
        col1_frame = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        col1_frame.grid(row=1, column=1, sticky="nsew")
        col1_frame.grid_columnconfigure(0, weight=1)

        sec2 = create_card(col1_frame, "模型参数", 0, 0)
        
        def make_vol(p):
            self.vol_window_menu = ctk.CTkOptionMenu(p, variable=self.v_vol_window, values=list(VOL_WINDOW_MAP.keys()), 
                                                     width=80, font=(FONT_FAMILY, 12), fg_color=BORDER, button_color=BTN_HOVER, 
                                                     text_color=TEXT, dropdown_fg_color=BG_INPUT, dropdown_text_color=TEXT,
                                                     command=self._on_vol_window_change)
            return self.vol_window_menu
        _form_row(sec2, "波动率 σ (%)", self.v_sigma, 0, wind=True, width=100, extra_widget=make_vol)
        
        def make_shi(p):
            self.btn_shibor = ctk.CTkButton(p, text="Shibor", command=self._fetch_shibor, fg_color=BTN_CTRL, hover_color=BTN_HOVER, 
                                            text_color=ORANGE, font=(FONT_FAMILY, 12, "bold"), width=80, height=28, corner_radius=6)
            return self.btn_shibor
        _form_row(sec2, "无风险利率 r (%)", self.v_r, 1, width=100, extra_widget=make_shi)
        
        def make_spr(p):
            self.btn_spread = ctk.CTkButton(p, text="按评级填", command=self._fill_spread_from_rating, fg_color=BTN_CTRL, hover_color=BTN_HOVER, 
                                            text_color=ORANGE, font=(FONT_FAMILY, 12, "bold"), width=80, height=28, corner_radius=6)
            return self.btn_spread
        _form_row(sec2, "信用利差 (%)", self.v_spread, 2, width=100, extra_widget=make_spr)
        
        _form_row(sec2, "下修概率 p (%)", self.v_p_down, 3)
        _form_row(sec2, "信用扩张系数 (%)", self.v_dk, 4)

        sec3 = create_card(col1_frame, "条款触发条件", 1, 0)
        _form_row(sec3, "强赎触发 (%K)", self.v_call_ratio, 0, wind=True)
        _form_row(sec3, "回售触发 (%K)", self.v_put_ratio, 1, wind=True)
        _form_row(sec3, "回售生效年数", self.v_put_years, 2, wind=True)

        sec4 = create_card(col1_frame, "数值网格", 2, 0)
        _form_row(sec4, "空间节点 M", self.v_M, 0)
        _form_row(sec4, "时间步数 N", self.v_N, 1)

        # ── 底部结果区 ──
        res_card = ctk.CTkFrame(self.main_frame, fg_color=BG_CARD, corner_radius=16, border_width=1, border_color=BORDER)
        res_card.grid(row=2, column=0, columnspan=2, sticky="ew", padx=10, pady=(10, 20))
        res_card.grid_columnconfigure(0, weight=1)
        res_card.grid_columnconfigure(1, weight=1)
        
        btn_box = ctk.CTkFrame(res_card, fg_color="transparent")
        btn_box.grid(row=0, column=0, padx=30, pady=30, sticky="w")
        
        self.btn_calc = ctk.CTkButton(btn_box, text="▶ 计算理论价格", command=self._run_pricing,
                                      font=(FONT_FAMILY, 18, "bold"), width=240, height=54, corner_radius=12,
                                      fg_color=("#1e66f5", "#0052cc"), hover_color=("#7287fd", "#0066ff"), text_color=("#ffffff", "#ffffff"))
        self.btn_calc.pack()
        
        res_box = ctk.CTkFrame(res_card, fg_color="transparent")
        res_box.grid(row=0, column=1, padx=40, pady=25, sticky="e")
        
        lbl_res_title = ctk.CTkLabel(res_box, text="理论价格 (¥)", font=(FONT_FAMILY, 14), text_color=TEXT_DIM)
        lbl_res_title.pack(side="top", anchor="e")
        
        self.lbl_result = ctk.CTkLabel(res_box, textvariable=self.v_result, font=(FONT_FAMILY, 56, "bold"), text_color=TEXT)
        self.lbl_result.pack(side="top", anchor="e", pady=(0, 5))
        
        self.lbl_status = ctk.CTkLabel(res_card, textvariable=self.v_status, font=(FONT_FAMILY, 13), text_color=TEXT_DIM)
        self.lbl_status.grid(row=1, column=0, columnspan=2, sticky="w", padx=35, pady=(0, 20))

        # ── 历史回测卡片 ──
        bt_card = ctk.CTkFrame(self.main_frame, fg_color=BG_CARD, corner_radius=16, border_width=1, border_color=BORDER)
        bt_card.grid(row=3, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 20))
        bt_card.grid_columnconfigure(0, weight=1)

        bt_header = ctk.CTkFrame(bt_card, fg_color="transparent")
        bt_header.grid(row=0, column=0, sticky="ew", padx=25, pady=(20, 10))
        ctk.CTkLabel(bt_header, text="📈 历史回测对比", font=(FONT_FAMILY, 16, "bold"), text_color=TEXT).pack(side="left")
        ctk.CTkLabel(bt_header, text="理论价 vs 实际收盘 (条款/模型参数 = 当前界面值)",
                     font=(FONT_FAMILY, 12), text_color=TEXT_DIM).pack(side="left", padx=(12, 0))

        bt_ctrl = ctk.CTkFrame(bt_card, fg_color="transparent")
        bt_ctrl.grid(row=1, column=0, sticky="ew", padx=25, pady=(0, 10))

        ctk.CTkLabel(bt_ctrl, text="开始", text_color=TEXT_DIM, font=(FONT_FAMILY, 13)).pack(side="left", padx=(0, 4))
        ctk.CTkEntry(bt_ctrl, textvariable=self.v_bt_start, width=110, font=(FONT_MONO, 13),
                     fg_color=BG_INPUT, border_color=BORDER, border_width=1, corner_radius=6).pack(side="left", padx=(0, 12))

        ctk.CTkLabel(bt_ctrl, text="结束", text_color=TEXT_DIM, font=(FONT_FAMILY, 13)).pack(side="left", padx=(0, 4))
        ctk.CTkEntry(bt_ctrl, textvariable=self.v_bt_end, width=110, font=(FONT_MONO, 13),
                     fg_color=BG_INPUT, border_color=BORDER, border_width=1, corner_radius=6).pack(side="left", padx=(0, 12))

        ctk.CTkLabel(bt_ctrl, text="频率", text_color=TEXT_DIM, font=(FONT_FAMILY, 13)).pack(side="left", padx=(0, 4))
        ctk.CTkOptionMenu(bt_ctrl, variable=self.v_bt_freq, values=["日", "周", "月"],
                          width=70, font=(FONT_FAMILY, 12), fg_color=BORDER, button_color=BTN_HOVER,
                          text_color=TEXT, dropdown_fg_color=BG_INPUT, dropdown_text_color=TEXT).pack(side="left", padx=(0, 12))

        self.btn_backtest = ctk.CTkButton(bt_ctrl, text="运行回测", command=self._run_backtest,
                                          fg_color=ACCENT, hover_color="#74c7ec", text_color=("#ffffff", "#11111b"),
                                          font=(FONT_FAMILY, 13, "bold"), width=100, height=30, corner_radius=6)
        self.btn_backtest.pack(side="left")

        self.lbl_bt_status = ctk.CTkLabel(bt_card, textvariable=self.v_bt_status,
                                          font=(FONT_FAMILY, 12), text_color=TEXT_DIM)
        self.lbl_bt_status.grid(row=2, column=0, sticky="w", padx=30, pady=(0, 8))

        # matplotlib 图表容器
        self.bt_chart_frame = ctk.CTkFrame(bt_card, fg_color=BG_CARD, corner_radius=8)
        self.bt_chart_frame.grid(row=3, column=0, sticky="nsew", padx=20, pady=(0, 20))
        self.bt_chart_frame.grid_columnconfigure(0, weight=1)
        self.bt_chart_frame.grid_rowconfigure(0, weight=1)
        
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
            theo = pricer.price(**params["model"])
            self.after(0, self._show_result, theo, pricer)
        except Exception as exc:
            err_msg = f"计算失败: {exc}"
            self.after(0, self._on_error, err_msg)
        finally:
            self.after(0, self._stop_progress)
            self.after(0, lambda: self.btn_calc.configure(state="normal"))

    def _collect_params(self):
        def pf(v): return float(v.get())
        def pd(v): return date.fromisoformat(v.get().strip())

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

    def _show_result(self, theo, pricer):
        self.v_result.set(f"{theo:.3f}")
        info = (
            f"S₀={pricer.S0:.3f}  K={pricer.K:.2f}  "
            f"T={pricer.T:.4f}年  "
            f"σ={float(self.v_sigma.get()):.1f}%  "
            f"转股比例={pricer.ratio:.4f}"
        )
        self.v_status.set(info)

        if theo > 100:
            self.lbl_result.configure(text_color=GREEN)
        elif theo < 100:
            self.lbl_result.configure(text_color=RED)
        else:
            self.lbl_result.configure(text_color=TEXT)

    def _on_error(self, msg):
        self._stop_progress()
        self.v_status.set(f"❌ {msg}")
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
            import matplotlib.pyplot as plt
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

        fig = Figure(figsize=(9, 4), dpi=100, facecolor=bg_card_color)
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


# ── 启动 ──────────────────────────────────────────────────
if __name__ == "__main__":
    app = CBPricerApp()
    app.mainloop()
