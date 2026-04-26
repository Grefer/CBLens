import sys

def patch_gui():
    with open("gui.py", "r", encoding="utf-8") as f:
        lines = f.readlines()

    # block 1: line 73 to 141
    # block 2: line 221 to 314
    # block 3: line 315 to 467
    # block 4: line 468 to 528
    # block 5: line 531 to 590

    new_ui_funcs = """def _form_row(parent, label_text, var, row, wind=False, extra_widget=None, width=130):
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
    \"\"\"可折叠面板: 点击标题行展开/收起内容\"\"\"
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
"""

    new_ui_build = """    def _build_ui(self):
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
        ctk.CTkLabel(left_frame, text="DeltaPricer", font=(FONT_FAMILY, 20, "bold"), text_color=TEXT).pack(side="left")
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
            text_color=("#ffffff", "#11111b"), text_color_disabled=TEXT_DIM,
            corner_radius=8)
        self.tab_seg.set("⚡ 定价")
        self.tab_seg.grid(row=0, column=1, pady=15)

        right_frame = ctk.CTkFrame(header, fg_color="transparent")
        right_frame.grid(row=0, column=2, sticky="e", padx=20, pady=15)
        
        ctk.CTkEntry(right_frame, textvariable=self.v_bond_code, width=150,
                     font=(FONT_MONO, 13), placeholder_text="输入代码 (如 128009.SZ)",
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
"""

    new_pricing_tab = """    def _build_pricing_tab(self):
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
"""

    new_backtest_tab = """    def _build_backtest_tab(self):
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
"""

    new_sens_tab = """    def _build_sensitivity_tab(self):
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
"""

    out_lines = lines[:72] + new_ui_funcs.splitlines(True) + lines[142:220] + new_ui_build.splitlines(True) + new_pricing_tab.splitlines(True) + new_backtest_tab.splitlines(True) + new_sens_tab.splitlines(True) + lines[590:]
    
    with open("gui.py", "w", encoding="utf-8") as f:
        f.writelines(out_lines)

if __name__ == "__main__":
    patch_gui()
