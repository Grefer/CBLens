"""📦 批量定价 Tab — 基于 cb_data 转债池 → 并发定价 → 按基差排序导出."""
import threading
import math
import tkinter as tk
from datetime import date
import customtkinter as ctk
from tkinter import messagebox, filedialog, ttk

from ..theme import *
from ...batch_pricing import (
    batch_pricing_exclusion_reason,
    build_batch_provider,
    list_upcoming_tradable_from_cache,
    load_batch_results_cache,
    merge_upcoming_pricing_results,
    save_batch_results_cache,
    split_batch_codes_from_cache,
    summarize_batch_results,
    write_batch_results_csv,
)
from ...pricing_api import batch_price_from_provider_threaded
from ...watchlist import (
    add_to_watchlist,
    load_watchlist,
    remove_from_watchlist,
)


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
    ctk.CTkLabel(ch, text="基于 cb_data 全量转债池 → 并发定价 → 按理论偏差排序",
                 font=(FONT_FAMILY, 12), text_color=TEXT_DIM).pack(side="left", padx=(12, 0))

    cc = ctk.CTkFrame(ctrl, fg_color="transparent")
    cc.grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 15))

    app.v_batch_source = ctk.StringVar(value="Wind")
    ctk.CTkLabel(cc, text="行情源", text_color=TEXT_DIM, font=(FONT_FAMILY, 13)).pack(side="left", padx=(8, 4))
    ctk.CTkOptionMenu(cc, variable=app.v_batch_source, values=["Wind", "akshare"],
                      width=90, font=(FONT_FAMILY, 12), fg_color=BG_INPUT, button_color=BTN_HOVER,
                      text_color=TEXT, dropdown_fg_color=BG_INPUT, dropdown_text_color=TEXT).pack(side="left", padx=(0, 12))

    app.btn_batch_run = ctk.CTkButton(
        cc, text="🔄 刷新重算", command=lambda: _run_batch(app),
        fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color=("#ffffff", "#11111b"),
        font=(FONT_FAMILY, 13, "bold"), width=110, height=32, corner_radius=6)
    app.btn_batch_run.pack(side="left")

    app.btn_batch_load_cache = ctk.CTkButton(
        cc, text="📂 加载缓存", command=lambda: _load_result_cache(app),
        fg_color=BG_INPUT, hover_color=BTN_HOVER, text_color=TEXT,
        font=(FONT_FAMILY, 12), width=90, height=32, corner_radius=6)
    app.btn_batch_load_cache.pack(side="left", padx=(8, 0))

    app.btn_batch_upcoming = ctk.CTkButton(
        cc, text="刷新关注池", command=lambda: _show_upcoming_tradable(app),
        fg_color=BG_INPUT, hover_color=BTN_HOVER, text_color=TEXT,
        font=(FONT_FAMILY, 12), width=96, height=32, corner_radius=6)
    app.btn_batch_upcoming.pack(side="left", padx=(8, 0))

    app.btn_batch_add_watch = ctk.CTkButton(
        cc, text="⭐ 加入关注池", command=lambda: _add_selection_to_watchlist(app),
        fg_color=BG_INPUT, hover_color=BTN_HOVER, text_color=TEXT,
        font=(FONT_FAMILY, 12), width=110, height=32, corner_radius=6)
    app.btn_batch_add_watch.pack(side="left", padx=(8, 0))

    app.btn_batch_save_cache = ctk.CTkButton(
        cc, text="💾 保存缓存", command=lambda: _save_result_cache(app),
        fg_color=BG_INPUT, hover_color=BTN_HOVER, text_color=TEXT,
        font=(FONT_FAMILY, 12), width=90, height=32, corner_radius=6, state="disabled")
    app.btn_batch_save_cache.pack(side="left", padx=(8, 0))

    app.btn_batch_export = ctk.CTkButton(
        cc, text="📝 导出 CSV", command=lambda: _export_csv(app),
        fg_color=BG_INPUT, hover_color=BTN_HOVER, text_color=TEXT,
        font=(FONT_FAMILY, 12), width=90, height=32, corner_radius=6, state="disabled")
    app.btn_batch_export.pack(side="left", padx=(8, 0))

    codes, excluded = split_batch_codes_from_cache(getattr(app, "terms_cache", None))
    suffix = f", 已过滤 {len(excluded)} 只非主池标的" if excluded else ""
    app.v_batch_status = ctk.StringVar(value=f"将基于本地 cb_data 普通转债池定价 ({len(codes)} 只{suffix})")
    ctk.CTkLabel(tab, textvariable=app.v_batch_status,
                 font=(FONT_FAMILY, 12), text_color=TEXT_DIM).grid(
                     row=1, column=0, sticky="w", padx=16, pady=(0, 6))

    # 结果表格区: 主批量列表与近 7 日可交易关注池分开呈现
    app.batch_results_frame = ctk.CTkFrame(tab, fg_color="transparent")
    app.batch_results_frame.grid(row=2, column=0, sticky="nsew", padx=6, pady=(0, 6))
    app.batch_results_frame.grid_columnconfigure(0, weight=1)
    app.batch_results_frame.grid_rowconfigure(0, weight=3)
    app.batch_results_frame.grid_rowconfigure(1, weight=1)
    app.batch_results_frame.grid_rowconfigure(2, weight=1)

    app.batch_table_frame = _create_table_section(
        app.batch_results_frame, row=0, title="主批量定价结果")
    app.batch_upcoming_table_frame = _create_table_section(
        app.batch_results_frame, row=1, title="近 7 日可交易关注池")
    app.batch_watchlist_table_frame = _create_table_section(
        app.batch_results_frame, row=2, title="⭐ 我的关注池 (右键删除)")

    app._batch_results = []
    app._batch_upcoming_results = list_upcoming_tradable_from_cache(getattr(app, "terms_cache", None))
    app._batch_watchlist = load_watchlist()
    _render_upcoming_table(app, app._batch_upcoming_results, update_status=False)
    _render_watchlist_table(app)


def _create_table_section(parent, *, row, title):
    section = ctk.CTkFrame(parent, fg_color=BG_CARD, corner_radius=16)
    section.grid(row=row, column=0, sticky="nsew", pady=(0, 8) if row == 0 else (0, 0))
    section.grid_columnconfigure(0, weight=1)
    section.grid_rowconfigure(1, weight=1)

    ctk.CTkLabel(
        section,
        text=title,
        font=(FONT_FAMILY, 13, "bold"),
        text_color=TEXT,
    ).grid(row=0, column=0, sticky="w", padx=12, pady=(8, 0))

    body = ctk.CTkFrame(section, fg_color="transparent")
    body.grid(row=1, column=0, sticky="nsew")
    body.grid_columnconfigure(0, weight=1)
    body.grid_rowconfigure(0, weight=1)
    return body


def _run_batch(app):
    codes, excluded = split_batch_codes_from_cache(getattr(app, "terms_cache", None))
    upcoming_rows = list_upcoming_tradable_from_cache(getattr(app, "terms_cache", None))
    if not codes:
        messagebox.showwarning("提示", "本地 cb_data 普通转债池为空, 请先同步基础信息")
        return

    source = app.v_batch_source.get()
    csv_root = getattr(app, "_csv_root", None)
    if source == "CSV" and not csv_root:
        csv_root = filedialog.askdirectory(title="选择 CSV 数据根目录 (含 bonds/ stocks/ terms/ 子目录)")
        if not csv_root:
            return
        app._csv_root = csv_root

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
    skipped = f", 已过滤 {len(excluded)} 只非主池标的" if excluded else ""
    watch = f", 关注池 {len(upcoming_rows)} 只" if upcoming_rows else ""
    app.v_batch_status.set(f"正在定价 {len(codes)} 只普通转债 (自动并发{skipped}{watch}) ...")
    app._start_progress(f"全量定价 {len(codes)} 只")

    threading.Thread(
        target=_batch_worker,
        args=(app, codes, upcoming_rows, source, csv_root, params, len(excluded)),
        daemon=True,
    ).start()


def _batch_worker(app, codes, upcoming_rows, source, csv_root, params, excluded_count=0):
    try:
        provider = build_batch_provider(
            source,
            terms_cache=getattr(app, "terms_cache", None),
            csv_root=csv_root,
            max_age_days=30,
        )
        try:
            rf = provider.get_risk_free_rate(date.today())
            if rf is not None:
                params = dict(params, r=float(rf) / 100.0)
        except Exception:
            pass

        def on_progress(done, total):
            app.after(0, lambda: app.v_batch_status.set(
                f"{provider.name} 自动并发进度 {done}/{total} ..."))

        results = batch_price_from_provider_threaded(
            provider, codes,
            progress_cb=on_progress,
            **params,
        )
        upcoming_results = list(upcoming_rows)
        upcoming_codes = [row["bond_code"] for row in upcoming_rows]
        if upcoming_codes:
            app.after(0, lambda: app.v_batch_status.set(
                f"{provider.name} 正在计算近 7 日关注池 {len(upcoming_codes)} 只 ..."))
            priced_upcoming = batch_price_from_provider_threaded(
                provider, upcoming_codes,
                **params,
            )
            upcoming_results = merge_upcoming_pricing_results(upcoming_rows, priced_upcoming)
        cache_path = save_batch_results_cache(
            results,
            source=provider.name,
            params=params,
            upcoming_results=upcoming_results,
        )
        app._batch_results = results
        app._batch_upcoming_results = upcoming_results
        app.after(0, lambda: _render_batch_views(
            app, results, upcoming_results,
            cache_path=cache_path, excluded_count=excluded_count))
    except Exception as exc:
        app.after(0, lambda exc=exc: app.v_batch_status.set(f"❌ 批量定价失败: {exc}"))
        app.after(0, lambda exc=exc: messagebox.showerror("批量定价失败", str(exc)))
    finally:
        app.after(0, app._stop_progress)
        app.after(0, lambda: app.btn_batch_run.configure(state="normal"))


def _render_batch_views(
    app,
    results,
    upcoming_results,
    *,
    cache_path=None,
    cache_meta=None,
    excluded_count=0,
):
    _render_table(app, results, cache_path=cache_path,
                  cache_meta=cache_meta, excluded_count=excluded_count)
    _render_upcoming_table(app, upcoming_results, update_status=False)
    _render_watchlist_table(app)


def _render_table(app, results, *, cache_path=None, cache_meta=None, excluded_count=0):
    for child in app.batch_table_frame.winfo_children():
        child.destroy()

    if not results:
        app.v_batch_status.set("无结果")
        return

    headers = ["代码", "名称", "正股", "S₀", "K", "σ(%)", "理论价", "市价", "偏差(%)", "评级", "状态"]
    col_widths = [100, 80, 80, 60, 60, 55, 65, 65, 70, 50, 120]
    columns = [f"c{i}" for i in range(len(headers))]

    _configure_tree_style()
    tree = ttk.Treeview(
        app.batch_table_frame,
        columns=columns,
        show="headings",
        selectmode="extended",
    )
    y_scroll = ttk.Scrollbar(app.batch_table_frame, orient="vertical", command=tree.yview)
    x_scroll = ttk.Scrollbar(app.batch_table_frame, orient="horizontal", command=tree.xview)
    tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)

    tree.grid(row=0, column=0, sticky="nsew", padx=(8, 0), pady=(8, 0))
    y_scroll.grid(row=0, column=1, sticky="ns", pady=(8, 0), padx=(0, 8))
    x_scroll.grid(row=1, column=0, sticky="ew", padx=(8, 0), pady=(0, 8))

    for column, header, width in zip(columns, headers, col_widths):
        tree.heading(column, text=header)
        tree.column(column, width=width, minwidth=width, stretch=False, anchor="w")

    tree.tag_configure("underpriced", foreground=get_color(GREEN))
    tree.tag_configure("overpriced", foreground=get_color(RED))
    tree.tag_configure("failed", foreground=get_color(TEXT_DIM))
    app._batch_main_tree = tree
    _attach_main_context_menu(app, tree)

    for idx, r in enumerate(results):
        is_ok = r.get("status") == "ok"
        dev = r.get("deviation", float("nan"))
        dev_str = f"{dev*100:+.2f}" if not math.isnan(dev) else "—"

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
        tags = []
        if not is_ok:
            tags.append("failed")
        elif not math.isnan(dev) and dev < -0.03:
            tags.append("underpriced")
        elif not math.isnan(dev) and dev > 0.05:
            tags.append("overpriced")
        tree.insert("", "end", iid=str(idx), values=vals, tags=tags)

    summary = summarize_batch_results(results)
    app.v_batch_status.set(
        f"✅ 完成 {summary['total']} 只  |  成功 {summary['success']}  失败 {summary['failed']}  |  "
        f"按偏差升序排列 (负值 = 市价低于理论 = 潜在低估)")
    if excluded_count:
        app.v_batch_status.set(f"{app.v_batch_status.get()}  |  已过滤 {excluded_count} 只定向/非主池标的")
    app.btn_batch_export.configure(state="normal")
    app.btn_batch_save_cache.configure(state="normal")
    if cache_path is not None:
        app.v_batch_status.set(f"{app.v_batch_status.get()}  |  已刷新缓存 {cache_path}")
    elif cache_meta:
        saved_at = cache_meta.get("saved_at", "未知时间")
        source = cache_meta.get("source") or "未知数据源"
        app.v_batch_status.set(f"{app.v_batch_status.get()}  |  缓存 {saved_at} / {source}")


def _configure_tree_style():
    style = ttk.Style()
    style.theme_use("clam")
    style.configure(
        "Treeview",
        background=get_color(BG_CARD),
        fieldbackground=get_color(BG_CARD),
        foreground=get_color(TEXT),
        rowheight=26,
        borderwidth=0,
        font=(FONT_MONO, 11),
    )
    style.configure(
        "Treeview.Heading",
        background=get_color(BORDER),
        foreground=get_color(TEXT),
        borderwidth=0,
        font=(FONT_FAMILY, 11, "bold"),
    )
    style.map(
        "Treeview",
        background=[("selected", get_color(BG_INPUT))],
        foreground=[("selected", get_color(TEXT))],
    )


def _show_upcoming_tradable(app, window_days=7):
    rows = list_upcoming_tradable_from_cache(
        getattr(app, "terms_cache", None),
        window_days=window_days,
    )
    app._batch_upcoming_results = rows
    _render_upcoming_table(app, rows, window_days=window_days)


def _render_upcoming_table(app, rows, *, window_days=7, update_status=True):
    frame = getattr(app, "batch_upcoming_table_frame", app.batch_table_frame)
    for child in frame.winfo_children():
        child.destroy()

    if not rows:
        if update_status:
            app.v_batch_status.set(f"未来 {window_days} 天暂无即将可交易的定向/非主池转债")
        return

    headers = ["代码", "名称", "正股", "可交易日", "剩余天数", "K", "理论价", "参考价", "偏差(%)", "状态"]
    col_widths = [100, 90, 80, 95, 70, 65, 70, 70, 70, 130]
    columns = [f"u{i}" for i in range(len(headers))]

    _configure_tree_style()
    tree = ttk.Treeview(
        frame,
        columns=columns,
        show="headings",
        selectmode="browse",
    )
    y_scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    x_scroll = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
    tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)

    tree.grid(row=0, column=0, sticky="nsew", padx=(8, 0), pady=(8, 0))
    y_scroll.grid(row=0, column=1, sticky="ns", pady=(8, 0), padx=(0, 8))
    x_scroll.grid(row=1, column=0, sticky="ew", padx=(8, 0), pady=(0, 8))

    for column, header, width in zip(columns, headers, col_widths):
        tree.heading(column, text=header)
        tree.column(column, width=width, minwidth=width, stretch=False, anchor="w")

    tree.tag_configure("upcoming", foreground=get_color(ACCENT))
    tree.tag_configure("failed", foreground=get_color(TEXT_DIM))
    for idx, row in enumerate(rows):
        tradable_date = row.get("tradable_date")
        dev = row.get("deviation", float("nan"))
        dev_str = f"{dev*100:+.2f}" if _is_finite(dev) else "—"
        is_ok = row.get("status") == "ok"
        vals = [
            row.get("bond_code", ""),
            row.get("bond_name", ""),
            row.get("stock_code", ""),
            tradable_date.isoformat() if hasattr(tradable_date, "isoformat") else (tradable_date or ""),
            str(row.get("days_to_trade", "")),
            f"{float(row['K']):.2f}" if row.get("K") is not None else "—",
            f"{float(row['theoretical_price']):.2f}" if is_ok and row.get("theoretical_price") is not None else "—",
            f"{float(row['market_price']):.2f}" if row.get("market_price") is not None else "—",
            dev_str,
            row.get("status") or row.get("trading_status", ""),
        ]
        tree.insert("", "end", iid=str(idx), values=vals,
                    tags=["upcoming" if is_ok else "failed"])

    if update_status:
        app.v_batch_status.set(
            f"未来 {window_days} 天即将可交易/进入关注窗口 {len(rows)} 只  |  "
            f"刷新重算后显示理论价")


def _save_result_cache(app):
    if not app._batch_results:
        messagebox.showinfo("提示", "当前没有可保存的批量定价结果")
        return
    try:
        path = save_batch_results_cache(
            app._batch_results,
            source=getattr(app, "v_batch_source", None).get() if hasattr(app, "v_batch_source") else None,
            upcoming_results=getattr(app, "_batch_upcoming_results", []),
        )
        app.v_batch_status.set(f"已保存批量定价缓存: {path}")
    except Exception as exc:
        messagebox.showerror("保存缓存失败", str(exc))


def _load_result_cache(app):
    try:
        loaded = load_batch_results_cache()
    except FileNotFoundError as exc:
        messagebox.showinfo("提示", str(exc))
        return
    except Exception as exc:
        messagebox.showerror("加载缓存失败", str(exc))
        return

    results, excluded_count = _filter_nonstandard_results(loaded["results"])
    upcoming_results = loaded.get("upcoming_results") or list_upcoming_tradable_from_cache(
        getattr(app, "terms_cache", None))
    app._batch_results = results
    app._batch_upcoming_results = upcoming_results
    _render_batch_views(
        app, results, upcoming_results,
        cache_meta=loaded.get("meta"), excluded_count=excluded_count)


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
        write_batch_results_csv(path, app._batch_results)
        app.v_batch_status.set(f"已导出 {len(app._batch_results)} 条到 {path}")
    except Exception as exc:
        messagebox.showerror("导出失败", str(exc))


def _filter_nonstandard_results(results):
    kept = []
    excluded_count = 0
    for row in results:
        reason = batch_pricing_exclusion_reason(row.get("bond_code", ""), row)
        if reason is None:
            kept.append(row)
        else:
            excluded_count += 1
    return kept, excluded_count


def _is_finite(value) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _attach_main_context_menu(app, tree):
    menu = tk.Menu(tree, tearoff=0)
    menu.add_command(label="⭐ 加入关注池",
                     command=lambda: _add_selection_to_watchlist(app))

    def _popup(event):
        clicked = tree.identify_row(event.y)
        if clicked and clicked not in tree.selection():
            tree.selection_set(clicked)
        if not tree.selection():
            return
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    tree.bind("<Button-3>", _popup)
    tree.bind("<Button-2>", _popup)


def _add_selection_to_watchlist(app):
    tree = getattr(app, "_batch_main_tree", None)
    if tree is None or not app._batch_results:
        messagebox.showinfo("提示", "请先运行或加载批量定价结果, 再选择转债")
        return
    selection = tree.selection()
    if not selection:
        messagebox.showinfo("提示", "请先在主批量列表中选择一只或多只转债")
        return
    new_items = []
    for iid in selection:
        try:
            row = app._batch_results[int(iid)]
        except (ValueError, IndexError):
            continue
        code = row.get("bond_code")
        if not code:
            continue
        new_items.append({
            "bond_code": code,
            "bond_name": row.get("bond_name"),
            "stock_code": row.get("stock_code"),
        })
    if not new_items:
        return
    app._batch_watchlist, added = add_to_watchlist(new_items)
    _render_watchlist_table(app)
    skipped = len(new_items) - added
    msg = f"已加入关注池: {added} 只"
    if skipped:
        msg += f" (已存在 {skipped} 只跳过)"
    app.v_batch_status.set(msg)


def _remove_selected_from_watchlist(app):
    tree = getattr(app, "_batch_watchlist_tree", None)
    if tree is None:
        return
    selection = tree.selection()
    if not selection:
        return
    codes = [iid for iid in selection if iid]
    if not codes:
        return
    app._batch_watchlist = remove_from_watchlist(codes)
    _render_watchlist_table(app)
    app.v_batch_status.set(f"已从关注池移除 {len(codes)} 只")


def _watchlist_display_rows(app):
    by_code = {row.get("bond_code"): row for row in (app._batch_results or [])}
    rows = []
    for entry in app._batch_watchlist:
        code = entry.get("bond_code")
        merged = dict(entry)
        priced = by_code.get(code)
        if priced:
            for key in ("bond_name", "stock_code", "K", "theoretical_price",
                        "market_price", "deviation", "credit_rating", "status"):
                value = priced.get(key)
                if value is not None:
                    merged[key] = value
        rows.append(merged)
    return rows


def _render_watchlist_table(app):
    frame = getattr(app, "batch_watchlist_table_frame", None)
    if frame is None:
        return
    for child in frame.winfo_children():
        child.destroy()

    rows = _watchlist_display_rows(app)
    headers = ["代码", "名称", "正股", "K", "理论价", "市价", "偏差(%)", "状态", "加入时间"]
    col_widths = [100, 90, 80, 65, 70, 70, 70, 90, 150]
    columns = [f"w{i}" for i in range(len(headers))]

    _configure_tree_style()
    tree = ttk.Treeview(
        frame,
        columns=columns,
        show="headings",
        selectmode="extended",
    )
    y_scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    x_scroll = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
    tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)

    tree.grid(row=0, column=0, sticky="nsew", padx=(8, 0), pady=(8, 0))
    y_scroll.grid(row=0, column=1, sticky="ns", pady=(8, 0), padx=(0, 8))
    x_scroll.grid(row=1, column=0, sticky="ew", padx=(8, 0), pady=(0, 8))

    for column, header, width in zip(columns, headers, col_widths):
        tree.heading(column, text=header)
        tree.column(column, width=width, minwidth=width, stretch=False, anchor="w")

    tree.tag_configure("underpriced", foreground=get_color(GREEN))
    tree.tag_configure("overpriced", foreground=get_color(RED))
    tree.tag_configure("failed", foreground=get_color(TEXT_DIM))

    if not rows:
        placeholder = ctk.CTkLabel(
            frame,
            text="尚未关注任何转债 — 在主批量列表中选中一只或多只, 点击 \"⭐ 加入关注池\" 或右键添加",
            font=(FONT_FAMILY, 12),
            text_color=TEXT_DIM,
        )
        placeholder.grid(row=2, column=0, sticky="w", padx=12, pady=(2, 8))

    for entry in rows:
        code = entry.get("bond_code", "")
        dev = entry.get("deviation", float("nan"))
        dev_str = f"{float(dev) * 100:+.2f}" if _is_finite(dev) else "—"
        is_ok = entry.get("status") == "ok"
        vals = [
            code,
            entry.get("bond_name", "") or "",
            entry.get("stock_code", "") or "",
            f"{float(entry['K']):.2f}" if entry.get("K") is not None else "—",
            f"{float(entry['theoretical_price']):.2f}" if is_ok and entry.get("theoretical_price") is not None else "—",
            f"{float(entry['market_price']):.2f}" if entry.get("market_price") is not None else "—",
            dev_str,
            entry.get("status") or "—",
            entry.get("added_at", "") or "",
        ]
        tags = []
        if entry.get("status") and not is_ok:
            tags.append("failed")
        elif _is_finite(dev) and float(dev) < -0.03:
            tags.append("underpriced")
        elif _is_finite(dev) and float(dev) > 0.05:
            tags.append("overpriced")
        tree.insert("", "end", iid=code, values=vals, tags=tags)

    app._batch_watchlist_tree = tree
    _attach_watchlist_context_menu(app, tree)


def _attach_watchlist_context_menu(app, tree):
    menu = tk.Menu(tree, tearoff=0)
    menu.add_command(label="🗑 从关注池移除",
                     command=lambda: _remove_selected_from_watchlist(app))

    def _popup(event):
        clicked = tree.identify_row(event.y)
        if clicked and clicked not in tree.selection():
            tree.selection_set(clicked)
        if not tree.selection():
            return
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    tree.bind("<Button-3>", _popup)
    tree.bind("<Button-2>", _popup)
    tree.bind("<Delete>", lambda _e: _remove_selected_from_watchlist(app))
    tree.bind("<BackSpace>", lambda _e: _remove_selected_from_watchlist(app))
