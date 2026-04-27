"""📦 批量定价 Tab — 导入代码列表 → 并发定价 → 按基差排序导出."""
import threading
import math
import csv
import customtkinter as ctk
from tkinter import messagebox, filedialog
from datetime import date

from gui.theme import *
from gui.widgets import create_card

from CB import batch_price_from_provider
from data_providers import WindDataProvider, AkshareDataProvider, CSVDataProvider


def build(app, tab):
    """在 tab frame 上构建批量定价面板."""
    tab.grid_columnconfigure(0, weight=1)
    tab.grid_rowconfigure(2, weight=1)

    # 控制栏
    ctrl = ctk.CTkFrame(tab, fg_color=BG_CARD, corner_radius=16)
    ctrl.grid(row=0, column=0, sticky="ew", pady=(6, 12), padx=6)

    ch = ctk.CTkFrame(ctrl, fg_color="transparent")
    ch.grid(row=0, column=0, sticky="ew", padx=20, pady=(15, 8))
    ctk.CTkLabel(ch, text="📦 批量定价 / 转债池筛选",
                 font=(FONT_FAMILY, 16, "bold"), text_color=TEXT).pack(side="left")
    ctk.CTkLabel(ch, text="导入代码列表 → 并发定价 → 按理论偏差排序",
                 font=(FONT_FAMILY, 12), text_color=TEXT_DIM).pack(side="left", padx=(12, 0))

    cc = ctk.CTkFrame(ctrl, fg_color="transparent")
    cc.grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 15))

    app.v_batch_codes = ctk.StringVar(value="")
    ctk.CTkLabel(cc, text="代码列表", text_color=TEXT_DIM, font=(FONT_FAMILY, 13)).pack(side="left", padx=(0, 4))
    ctk.CTkEntry(cc, textvariable=app.v_batch_codes, width=400, font=(FONT_MONO, 12),
                 fg_color=BG_INPUT, border_width=0, corner_radius=6,
                 placeholder_text="128009.SZ, 113050.SH, ...  或点击导入").pack(side="left", padx=(0, 8))

    app.btn_batch_import = ctk.CTkButton(
        cc, text="📂 导入 CSV", command=lambda: _import_codes(app),
        fg_color=BG_INPUT, hover_color=BTN_HOVER, text_color=TEXT,
        font=(FONT_FAMILY, 12), width=90, height=30, corner_radius=6)
    app.btn_batch_import.pack(side="left", padx=(0, 8))

    app.v_batch_source = ctk.StringVar(value="Wind")
    ctk.CTkLabel(cc, text="数据源", text_color=TEXT_DIM, font=(FONT_FAMILY, 13)).pack(side="left", padx=(8, 4))
    ctk.CTkOptionMenu(cc, variable=app.v_batch_source, values=["Wind", "akshare"],
                      width=90, font=(FONT_FAMILY, 12), fg_color=BG_INPUT, button_color=BTN_HOVER,
                      text_color=TEXT, dropdown_fg_color=BG_INPUT, dropdown_text_color=TEXT).pack(side="left", padx=(0, 12))

    app.btn_batch_run = ctk.CTkButton(
        cc, text="🚀 批量定价", command=lambda: _run_batch(app),
        fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color=("#ffffff", "#11111b"),
        font=(FONT_FAMILY, 13, "bold"), width=110, height=32, corner_radius=6)
    app.btn_batch_run.pack(side="left")

    app.btn_batch_export = ctk.CTkButton(
        cc, text="📝 导出 CSV", command=lambda: _export_csv(app),
        fg_color=BG_INPUT, hover_color=BTN_HOVER, text_color=TEXT,
        font=(FONT_FAMILY, 12), width=90, height=32, corner_radius=6, state="disabled")
    app.btn_batch_export.pack(side="left", padx=(8, 0))

    app.v_batch_status = ctk.StringVar(value="输入代码列表 (逗号分隔) 或导入 CSV 文件")
    ctk.CTkLabel(tab, textvariable=app.v_batch_status,
                 font=(FONT_FAMILY, 12), text_color=TEXT_DIM).grid(
                     row=1, column=0, sticky="w", padx=16, pady=(0, 6))

    # 结果表格区
    app.batch_table_frame = ctk.CTkScrollableFrame(tab, fg_color=BG_CARD, corner_radius=16)
    app.batch_table_frame.grid(row=2, column=0, sticky="nsew", padx=6, pady=(0, 6))
    app.batch_table_frame.grid_columnconfigure(0, weight=1)

    app._batch_results = []


def _import_codes(app):
    path = filedialog.askopenfilename(
        title="导入转债代码列表",
        filetypes=[("CSV", "*.csv"), ("文本文件", "*.txt"), ("所有文件", "*.*")],
    )
    if not path:
        return
    try:
        codes = []
        with open(path, "r", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            for row in reader:
                for cell in row:
                    cell = cell.strip()
                    if cell and not cell.startswith("#"):
                        codes.append(cell)
        app.v_batch_codes.set(", ".join(codes))
        app.v_batch_status.set(f"已导入 {len(codes)} 只转债代码")
    except Exception as exc:
        messagebox.showerror("导入失败", str(exc))


def _run_batch(app):
    raw = app.v_batch_codes.get().strip()
    if not raw:
        messagebox.showwarning("提示", "请输入或导入转债代码")
        return

    codes = [c.strip() for c in raw.replace("\n", ",").split(",") if c.strip()]
    if not codes:
        messagebox.showwarning("提示", "未解析到有效代码")
        return

    source = app.v_batch_source.get()
    try:
        params = dict(
            r=float(app.v_r.get()) / 100.0,
            base_spread=float(app.v_spread.get()) / 100.0,
            p_down=float(app.v_p_down.get()) / 100.0,
            distress_k=float(app.v_dk.get()) / 100.0,
            M=max(100, int(float(app.v_M.get())) // 3),
            N=max(500, int(float(app.v_N.get())) // 3),
            vol_window_days=VOL_WINDOW_MAP.get(app.v_vol_window.get(), 21),
        )
    except ValueError as e:
        messagebox.showerror("参数错误", str(e))
        return

    app.btn_batch_run.configure(state="disabled")
    app.v_batch_status.set(f"正在批量定价 {len(codes)} 只转债 ...")
    app._start_progress(f"批量定价 {len(codes)} 只")

    threading.Thread(
        target=_batch_worker,
        args=(app, codes, source, params),
        daemon=True,
    ).start()


def _batch_worker(app, codes, source, params):
    try:
        if source == "Wind":
            provider = WindDataProvider()
        elif source == "akshare":
            provider = AkshareDataProvider()
        else:
            provider = WindDataProvider()

        def on_progress(done, total):
            app.after(0, lambda: app.v_batch_status.set(
                f"进度 {done}/{total} ..."))

        results = batch_price_from_provider(
            provider, codes,
            progress_cb=on_progress,
            **params,
        )
        app._batch_results = results
        app.after(0, lambda: _render_table(app, results))
    except Exception as exc:
        app.after(0, lambda: app.v_batch_status.set(f"❌ 批量定价失败: {exc}"))
        app.after(0, lambda: messagebox.showerror("批量定价失败", str(exc)))
    finally:
        app.after(0, app._stop_progress)
        app.after(0, lambda: app.btn_batch_run.configure(state="normal"))


def _render_table(app, results):
    for child in app.batch_table_frame.winfo_children():
        child.destroy()

    if not results:
        app.v_batch_status.set("无结果")
        return

    # 表头
    headers = ["代码", "名称", "正股", "S₀", "K", "σ(%)", "理论价", "市价", "偏差(%)", "评级", "状态"]
    col_widths = [100, 80, 80, 60, 60, 55, 65, 65, 70, 50, 120]

    hdr_frame = ctk.CTkFrame(app.batch_table_frame, fg_color=BORDER, corner_radius=0, height=30)
    hdr_frame.pack(fill="x", padx=4, pady=(4, 0))
    for i, (h, w) in enumerate(zip(headers, col_widths)):
        ctk.CTkLabel(hdr_frame, text=h, width=w, font=(FONT_FAMILY, 11, "bold"),
                     text_color=TEXT, anchor="w").pack(side="left", padx=4, pady=4)

    # 数据行
    ok_count = 0
    for idx, r in enumerate(results):
        is_ok = r.get("status") == "ok"
        if is_ok:
            ok_count += 1
        bg = BG_CARD if idx % 2 == 0 else BG_INPUT
        row_frame = ctk.CTkFrame(app.batch_table_frame, fg_color=bg, corner_radius=0, height=28)
        row_frame.pack(fill="x", padx=4)

        dev = r.get("deviation", float("nan"))
        dev_str = f"{dev*100:+.2f}" if not math.isnan(dev) else "—"
        # 高亮: 市价 < 理论 (被低估) 用绿色, 反之用红色
        dev_color = GREEN if not math.isnan(dev) and dev < -0.03 else (
            RED if not math.isnan(dev) and dev > 0.05 else TEXT_DIM)

        vals = [
            r.get("bond_code", ""),
            r.get("bond_name", ""),
            r.get("stock_code", ""),
            f"{r['S0']:.2f}" if is_ok and "S0" in r else "—",
            f"{r['K']:.2f}" if is_ok and "K" in r else "—",
            f"{r['sigma']*100:.1f}" if is_ok and "sigma" in r else "—",
            f"{r['theoretical_price']:.2f}" if is_ok else "—",
            f"{float(r['market_price']):.2f}" if is_ok and r.get("market_price") is not None else "—",
            dev_str,
            r.get("credit_rating", ""),
            r.get("status", ""),
        ]
        colors = [TEXT] * len(vals)
        colors[8] = dev_color  # deviation column

        for v, w, c in zip(vals, col_widths, colors):
            ctk.CTkLabel(row_frame, text=str(v), width=w, font=(FONT_MONO, 11),
                         text_color=c, anchor="w").pack(side="left", padx=4, pady=2)

    fail_count = len(results) - ok_count
    app.v_batch_status.set(
        f"✅ 完成 {len(results)} 只  |  成功 {ok_count}  失败 {fail_count}  |  "
        f"按偏差升序排列 (负值 = 市价低于理论 = 潜在低估)")
    app.btn_batch_export.configure(state="normal")


def _export_csv(app):
    if not app._batch_results:
        messagebox.showinfo("提示", "请先运行批量定价")
        return
    path = filedialog.asksaveasfilename(
        title="导出批量定价结果",
        defaultextension=".csv",
        filetypes=[("CSV", "*.csv")],
        initialfile="batch_pricing.csv",
    )
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["bond_code", "bond_name", "stock_code", "S0", "K",
                         "sigma", "theoretical_price", "market_price",
                         "deviation", "credit_rating", "status"])
            for r in app._batch_results:
                dev = r.get("deviation", float("nan"))
                w.writerow([
                    r.get("bond_code", ""),
                    r.get("bond_name", ""),
                    r.get("stock_code", ""),
                    f"{r.get('S0', '')}" if r.get("status") == "ok" else "",
                    f"{r.get('K', '')}" if r.get("status") == "ok" else "",
                    f"{r.get('sigma', '')}" if r.get("status") == "ok" else "",
                    f"{r.get('theoretical_price', '')}" if r.get("status") == "ok" else "",
                    f"{r.get('market_price', '')}" if r.get("market_price") is not None else "",
                    f"{dev:.6f}" if not math.isnan(dev) else "",
                    r.get("credit_rating", ""),
                    r.get("status", ""),
                ])
        app.v_batch_status.set(f"已导出 {len(app._batch_results)} 条到 {path}")
    except Exception as exc:
        messagebox.showerror("导出失败", str(exc))
