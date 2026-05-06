"""🔥 敏感性 Tab UI 构建."""
import customtkinter as ctk

from ..theme import (
    BG_CARD, BG_INPUT, TEXT, TEXT_DIM,
    ACCENT, ACCENT_HOVER, BTN_HOVER,
    FONT_FAMILY, FONT_MONO,
)


def build(app, tab):
    """敏感性 Tab: 控制栏 (参数范围) + 热力图区 + PNG 导出."""
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

    app.v_sens_s_min   = ctk.StringVar(value="70")
    app.v_sens_s_max   = ctk.StringVar(value="130")
    app.v_sens_sig_min = ctk.StringVar(value="10")
    app.v_sens_sig_max = ctk.StringVar(value="60")
    app.v_sens_steps   = ctk.StringVar(value="12")

    def _range_entry(parent, var, width=50):
        return ctk.CTkEntry(parent, textvariable=var, width=width,
                            font=(FONT_MONO, 13), fg_color=BG_INPUT,
                            border_width=0, corner_radius=6)

    def _sep(parent):
        ctk.CTkLabel(parent, text="~", text_color=TEXT_DIM,
                     font=(FONT_FAMILY, 13)).pack(side="left", padx=2)

    ctk.CTkLabel(cc, text="S (%K)", text_color=TEXT_DIM, font=(FONT_FAMILY, 13)).pack(
        side="left", padx=(0, 4))
    _range_entry(cc, app.v_sens_s_min).pack(side="left", padx=(0, 2))
    _sep(cc)
    _range_entry(cc, app.v_sens_s_max).pack(side="left", padx=(0, 15))

    ctk.CTkLabel(cc, text="σ (%)", text_color=TEXT_DIM, font=(FONT_FAMILY, 13)).pack(
        side="left", padx=(0, 4))
    _range_entry(cc, app.v_sens_sig_min).pack(side="left", padx=(0, 2))
    _sep(cc)
    _range_entry(cc, app.v_sens_sig_max).pack(side="left", padx=(0, 15))

    ctk.CTkLabel(cc, text="网格", text_color=TEXT_DIM, font=(FONT_FAMILY, 13)).pack(
        side="left", padx=(0, 4))
    _range_entry(cc, app.v_sens_steps, width=40).pack(side="left", padx=(0, 15))

    app.btn_sensitivity = ctk.CTkButton(
        cc, text="🔥 运行分析", command=app._run_sensitivity,
        fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color=("#ffffff", "#11111b"),
        font=(FONT_FAMILY, 13, "bold"), width=110, height=32, corner_radius=6)
    app.btn_sensitivity.pack(side="left")

    app.btn_sens_png = ctk.CTkButton(
        cc, text="📸 PNG", command=app._export_sens_png,
        fg_color=BG_INPUT, hover_color=BTN_HOVER, text_color=TEXT,
        font=(FONT_FAMILY, 12), width=75, height=32, corner_radius=6, state="disabled")
    app.btn_sens_png.pack(side="left", padx=(8, 0))

    app.lbl_sens_status = ctk.CTkLabel(
        tab, textvariable=app.v_sens_status,
        font=(FONT_FAMILY, 12), text_color=TEXT_DIM)
    app.lbl_sens_status.grid(row=1, column=0, sticky="sw", padx=16, pady=(0, 6))

    app.sens_chart_frame = ctk.CTkFrame(tab, fg_color=BG_CARD, corner_radius=16)
    app.sens_chart_frame.grid(row=2, column=0, sticky="nsew", padx=6, pady=(0, 6))
    app.sens_chart_frame.grid_columnconfigure(0, weight=1)
    app.sens_chart_frame.grid_rowconfigure(0, weight=1)
