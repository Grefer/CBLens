"""📈 回测 Tab — 控制栏 + 图表区."""
import customtkinter as ctk

from gui.theme import *


def build(app, tab):
    """在 tab frame 上构建回测面板."""
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
    ctk.CTkEntry(cc, textvariable=app.v_bt_start, width=110, font=(FONT_MONO, 13),
                 fg_color=BG_INPUT, border_width=0, corner_radius=6).pack(side="left", padx=(0, 12))
    ctk.CTkLabel(cc, text="结束", text_color=TEXT_DIM, font=(FONT_FAMILY, 13)).pack(side="left", padx=(0, 4))
    ctk.CTkEntry(cc, textvariable=app.v_bt_end, width=110, font=(FONT_MONO, 13),
                 fg_color=BG_INPUT, border_width=0, corner_radius=6).pack(side="left", padx=(0, 12))
    ctk.CTkLabel(cc, text="频率", text_color=TEXT_DIM, font=(FONT_FAMILY, 13)).pack(side="left", padx=(0, 4))
    ctk.CTkOptionMenu(cc, variable=app.v_bt_freq, values=["日", "周", "月"],
                      width=70, font=(FONT_FAMILY, 12), fg_color=BG_INPUT, button_color=BTN_HOVER,
                      text_color=TEXT, dropdown_fg_color=BG_INPUT, dropdown_text_color=TEXT).pack(side="left", padx=(0, 15))
    app.btn_backtest = ctk.CTkButton(
        cc, text="📊 运行回测", command=app._run_backtest,
        fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color=("#ffffff", "#11111b"),
        font=(FONT_FAMILY, 13, "bold"), width=110, height=32, corner_radius=6)
    app.btn_backtest.pack(side="left")
    ctk.CTkCheckBox(
        cc, text="价值分解", variable=app.v_bt_show_decomp,
        command=app._refresh_backtest_chart,
        font=(FONT_FAMILY, 12), text_color=TEXT_DIM, fg_color=ACCENT,
        checkbox_width=16, checkbox_height=16,
        border_width=1, corner_radius=3).pack(side="left", padx=(15, 0))
    ctk.CTkCheckBox(
        cc, text="反解 IV", variable=app.v_bt_solve_iv,
        font=(FONT_FAMILY, 12), text_color=TEXT_DIM, fg_color=ACCENT,
        checkbox_width=16, checkbox_height=16,
        border_width=1, corner_radius=3).pack(side="left", padx=(10, 0))
    app.btn_bt_png = ctk.CTkButton(
        cc, text="📸 PNG", command=app._export_bt_png,
        fg_color=BG_INPUT, hover_color=BTN_HOVER, text_color=TEXT,
        font=(FONT_FAMILY, 12), width=75, height=32, corner_radius=6, state="disabled")
    app.btn_bt_png.pack(side="left", padx=(10, 0))
    app.btn_bt_csv = ctk.CTkButton(
        cc, text="📝 CSV", command=app._export_bt_csv,
        fg_color=BG_INPUT, hover_color=BTN_HOVER, text_color=TEXT,
        font=(FONT_FAMILY, 12), width=75, height=32, corner_radius=6, state="disabled")
    app.btn_bt_csv.pack(side="left", padx=(6, 0))

    app.lbl_bt_status = ctk.CTkLabel(
        tab, textvariable=app.v_bt_status,
        font=(FONT_FAMILY, 12), text_color=TEXT_DIM)
    app.lbl_bt_status.grid(row=1, column=0, sticky="w", padx=16, pady=(0, 6))

    app.bt_chart_frame = ctk.CTkFrame(tab, fg_color=BG_CARD, corner_radius=16)
    app.bt_chart_frame.grid(row=2, column=0, sticky="nsew", padx=6, pady=(0, 6))
    app.bt_chart_frame.grid_columnconfigure(0, weight=1)
    app.bt_chart_frame.grid_rowconfigure(0, weight=1)
