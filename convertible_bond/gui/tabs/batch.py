"""📦 批量定价 Tab — 基于 cb_data 转债池 → 并发定价 → 按基差排序导出."""
from __future__ import annotations

import threading
import math
import tkinter as tk
from datetime import date
from typing import TYPE_CHECKING
import customtkinter as ctk
from tkinter import messagebox, filedialog, ttk

from ..theme import *
from ...batch_pricing import (
    BATCH_REVIEW_VIEWS,
    annotate_batch_results,
    batch_pricing_exclusion_reason,
    build_batch_provider,
    filter_batch_results_by_view,
    list_upcoming_tradable_from_cache,
    load_batch_results_cache,
    save_batch_results_cache,
    sort_batch_results_for_review,
    split_batch_codes_from_cache,
    summarize_exclusions,
    summarize_batch_results,
    write_batch_results_csv,
)
from ...pricing_api import batch_price_from_provider_threaded
from ...watchlist import (
    add_to_watchlist,
    load_watchlist,
    remove_from_watchlist,
)

if TYPE_CHECKING:
    from ..app import CBPricerApp

# ── Treeview 行标签颜色 (集中定义, 初始渲染与主题切换共用) ──
_TAG_COLORS: dict[str, tuple[str, str]] = {
    "underpriced": GREEN,
    "overpriced":  RED,
    "failed":      TEXT_DIM,
}
# 已注册到 app 的树实例属性名, 主题切换时统一刷新.
# 模块级集合: 假定单进程单 GUI 实例; 多实例场景下旧属性名会残留,
# 但 refresh_theme 通过 getattr(app, attr, None) 兜底, 无害.
_TREE_ATTRS: set[str] = set()


def _apply_tag_colors(tree: ttk.Treeview) -> None:
    """将 _TAG_COLORS 中的标签颜色写入 *tree*."""
    for tag, color in _TAG_COLORS.items():
        tree.tag_configure(tag, foreground=get_color(color))


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
    ctk.CTkLabel(ch, text="基于 cb_data 全量转债池 → 并发定价 → 按机会分筛选复核",
                 font=(FONT_FAMILY, 12), text_color=TEXT_DIM).pack(side="left", padx=(12, 0))

    cc = ctk.CTkFrame(ctrl, fg_color="transparent")
    cc.grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 15))

    app.v_batch_source = ctk.StringVar(value="Wind")
    ctk.CTkLabel(cc, text="行情源", text_color=TEXT_DIM, font=(FONT_FAMILY, 13)).pack(side="left", padx=(8, 4))
    ctk.CTkOptionMenu(cc, variable=app.v_batch_source, values=["Wind", "akshare"],
                      width=90, font=(FONT_FAMILY, 12), fg_color=BG_INPUT, button_color=BTN_HOVER,
                      text_color=TEXT, dropdown_fg_color=BG_INPUT, dropdown_text_color=TEXT).pack(side="left", padx=(0, 12))

    app.v_batch_view = ctk.StringVar(value="综合机会")
    ctk.CTkLabel(cc, text="视图", text_color=TEXT_DIM, font=(FONT_FAMILY, 13)).pack(side="left", padx=(0, 4))
    ctk.CTkOptionMenu(
        cc, variable=app.v_batch_view, values=list(BATCH_REVIEW_VIEWS),
        command=lambda _v: _change_batch_view(app),
        width=96, font=(FONT_FAMILY, 12), fg_color=BG_INPUT, button_color=BTN_HOVER,
        text_color=TEXT, dropdown_fg_color=BG_INPUT, dropdown_text_color=TEXT,
    ).pack(side="left", padx=(0, 12))

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
        cc, text="刷新关注池", command=lambda: _refresh_watchlist_with_upcoming(app),
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
    suffix = _excluded_status_suffix(excluded)
    app.v_batch_status = ctk.StringVar(value=f"将基于本地 cb_data 普通转债池定价 ({len(codes)} 只{suffix})")
    ctk.CTkLabel(tab, textvariable=app.v_batch_status,
                 font=(FONT_FAMILY, 12), text_color=TEXT_DIM).grid(
                     row=1, column=0, sticky="w", padx=16, pady=(0, 6))

    # 结果表格区: 主批量列表 + 我的关注池 (含自动发现的即将上市新债)
    app.batch_results_frame = ctk.CTkFrame(tab, fg_color="transparent")
    app.batch_results_frame.grid(row=2, column=0, sticky="nsew", padx=6, pady=(0, 6))
    app.batch_results_frame.grid_columnconfigure(0, weight=1)
    app.batch_results_frame.grid_rowconfigure(0, weight=3)
    app.batch_results_frame.grid_rowconfigure(1, weight=2)

    app.batch_table_frame = _create_table_section(
        app.batch_results_frame, row=0, title="主批量定价结果")
    app.batch_watchlist_table_frame = _create_table_section(
        app.batch_results_frame, row=1, title="⭐ 我的关注池 (右键删除)")

    app._batch_results = []
    app._batch_all_results = []
    app._batch_upcoming_results = []
    app._batch_watchlist = load_watchlist()
    # 自动发现即将上市/可交易的新债并加入关注池
    _auto_add_upcoming_to_watchlist(app, silent=True)
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
            M=max(300, int(float(app.v_M.get()))),
            N=max(1000, int(float(app.v_N.get()))),
            vol_window_days=VOL_WINDOW_MAP.get(app.v_vol_window.get(), 21),
        )
    except ValueError as e:
        messagebox.showerror("参数错误", str(e))
        return

    # 自动发现即将上市新债并加入关注池
    _auto_add_upcoming_to_watchlist(app, silent=True)
    watchlist_codes = [e.get("bond_code") for e in app._batch_watchlist if e.get("bond_code")]

    app.btn_batch_run.configure(state="disabled")
    skipped = _excluded_status_suffix(excluded)
    watch = f", 关注池 {len(watchlist_codes)} 只" if watchlist_codes else ""
    app.v_batch_status.set(f"正在定价 {len(codes)} 只普通转债 (自动并发{skipped}{watch}) ...")
    app._start_progress(f"全量定价 {len(codes)} 只")

    threading.Thread(
        target=_batch_worker,
        args=(app, codes, watchlist_codes, source, csv_root, params, len(excluded)),
        daemon=True,
    ).start()


def _batch_worker(app, codes, watchlist_codes, source, csv_root, params, excluded_count=0):
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
        results = sort_batch_results_for_review(results)
        # 对关注池中不在主批量结果里的代码单独定价
        main_codes_set = set(codes)
        extra_codes = [c for c in watchlist_codes if c not in main_codes_set]
        watchlist_pricing = []
        if extra_codes:
            app.after(0, lambda: app.v_batch_status.set(
                f"{provider.name} 正在计算关注池 {len(extra_codes)} 只 ..."))
            watchlist_pricing = batch_price_from_provider_threaded(
                provider, extra_codes,
                **params,
            )
            watchlist_pricing = annotate_batch_results(watchlist_pricing)
        cache_path = save_batch_results_cache(
            results,
            source=provider.name,
            params=params,
            upcoming_results=watchlist_pricing,
        )
        app._batch_results = results
        app._batch_upcoming_results = watchlist_pricing
        app._last_batch_source = provider.name
        app._last_batch_params = dict(params)
        app.after(0, lambda: _render_batch_views(
            app, results,
            cache_path=cache_path, excluded_count=excluded_count))
    except Exception as exc:
        app.after(0, lambda exc=exc: app.v_batch_status.set(f"❌ 批量定价失败: {exc}"))
        app.after(0, lambda exc=exc: messagebox.showerror("批量定价失败", str(exc)))
    finally:
        app.after(0, app._stop_progress)
        app.after(0, lambda: app.btn_batch_run.configure(state="normal"))


def _excluded_status_suffix(excluded):
    if not excluded:
        return ""
    by_reason = summarize_exclusions(excluded)
    top = "、".join(f"{reason}{count}" for reason, count in list(by_reason.items())[:2])
    return f", 准入过滤 {len(excluded)} 只 ({top})"


def _render_batch_views(
    app,
    results=None,
    *,
    cache_path=None,
    cache_meta=None,
    excluded_count=0,
):
    if results is not None:
        app._batch_all_results = sort_batch_results_for_review(results)
    base_results = getattr(app, "_batch_all_results", None) or []
    view = getattr(app, "v_batch_view", None).get() if hasattr(app, "v_batch_view") else "综合机会"
    display_results = filter_batch_results_by_view(base_results, view)
    app._batch_results = display_results
    _render_table(app, display_results, total_results=len(base_results), view=view, cache_path=cache_path,
                  cache_meta=cache_meta, excluded_count=excluded_count)
    _render_watchlist_table(app)


def _change_batch_view(app):
    if not getattr(app, "_batch_all_results", None):
        return
    _render_batch_views(app)


def _render_table(app, results, *, total_results=None, view=None, cache_path=None, cache_meta=None, excluded_count=0):
    for child in app.batch_table_frame.winfo_children():
        child.destroy()

    if not results:
        app.v_batch_status.set("无结果")
        return

    headers = [
        "代码", "名称", "正股", "机会分", "可信", "转股价值", "转股溢价(%)",
        "σ(%)", "理论价", "市价", "偏差(%)", "评级", "敏感性", "标签", "复核建议", "状态",
    ]
    col_widths = [100, 80, 80, 70, 45, 70, 80, 55, 65, 65, 70, 50, 90, 180, 260, 120]
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

    _apply_tag_colors(tree)
    app._batch_main_tree = tree
    _TREE_ATTRS.add("_batch_main_tree")
    _attach_main_context_menu(app, tree)

    for idx, r in enumerate(results):
        is_ok = r.get("status") == "ok"
        dev = r.get("deviation", float("nan"))
        dev_str = f"{float(dev)*100:+.2f}" if _is_finite(dev) else "—"
        conv_premium = r.get("conversion_premium")
        conv_str = f"{float(conv_premium)*100:+.1f}" if _is_finite(conv_premium) else "—"
        score = r.get("opportunity_score")
        score_str = f"{float(score):.1f}" if _is_finite(score) else "—"
        tags_str = _format_tags(r.get("risk_tags"))
        notes_str = _format_tags(r.get("review_notes"))

        vals = [
            r.get("bond_code", ""),
            r.get("bond_name", ""),
            r.get("stock_code", ""),
            score_str,
            r.get("confidence", "") if is_ok else "—",
            f"{float(r['parity']):.2f}" if is_ok and _is_finite(r.get("parity")) else "—",
            conv_str,
            f"{r['sigma']*100:.1f}" if is_ok and "sigma" in r else "—",
            f"{r['theoretical_price']:.2f}" if is_ok else "—",
            f"{float(r['market_price']):.2f}" if is_ok and r.get("market_price") is not None else "—",
            dev_str,
            r.get("credit_rating", ""),
            r.get("sensitivity_status", ""),
            tags_str,
            notes_str,
            r.get("status", ""),
        ]
        tags = []
        if not is_ok:
            tags.append("failed")
        elif _is_finite(dev) and float(dev) < -0.03:
            tags.append("underpriced")
        elif _is_finite(dev) and float(dev) > 0.05:
            tags.append("overpriced")
        tree.insert("", "end", iid=str(idx), values=vals, tags=tags)

    summary = summarize_batch_results(results)
    total = total_results if total_results is not None else summary["total"]
    view_name = view or "综合机会"
    app.v_batch_status.set(
        f"✅ {view_name}: 展示 {summary['total']}/{total} 只  |  成功 {summary['success']}  失败 {summary['failed']}  |  "
        f"按机会分排序 (兼顾低估、转股折价、余额/评级/HV风险)")
    if excluded_count:
        app.v_batch_status.set(f"{app.v_batch_status.get()}  |  准入过滤 {excluded_count} 只")
    app.btn_batch_export.configure(state="normal")
    app.btn_batch_save_cache.configure(state="normal")
    if cache_path is not None:
        app.v_batch_status.set(f"{app.v_batch_status.get()}  |  已刷新缓存 {cache_path}")
    elif cache_meta:
        saved_at = cache_meta.get("saved_at", "未知时间")
        source = cache_meta.get("source") or "未知数据源"
        app.v_batch_status.set(f"{app.v_batch_status.get()}  |  缓存 {saved_at} / {source}")


def _configure_tree_style() -> None:
    """配置 ttk Treeview 全局样式 (idempotent).

    设置 ``clam`` 主题并按当前 appearance mode 写入背景/边框/文字颜色.
    初始渲染与主题切换均调用; ``style.theme_use`` 在已设置时为 no-op.
    """
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


def refresh_theme(app: "CBPricerApp") -> None:
    """主题切换后刷新 Treeview 样式 + 给所有已注册树重新染色.

    ``app.py`` 的 ``_toggle_theme`` 在 ``ctk.set_appearance_mode`` 之后调用本函数.
    """
    _configure_tree_style()
    for attr in _TREE_ATTRS:
        tree = getattr(app, attr, None)
        if tree is not None:
            _apply_tag_colors(tree)


def _auto_add_upcoming_to_watchlist(app, *, silent=False):
    """自动发现即将上市/可交易的新债并加入关注池."""
    upcoming = list_upcoming_tradable_from_cache(
        getattr(app, "terms_cache", None))
    if upcoming:
        new_items = [
            {"bond_code": r["bond_code"],
             "bond_name": r.get("bond_name"),
             "stock_code": r.get("stock_code")}
            for r in upcoming
        ]
        app._batch_watchlist, added = add_to_watchlist(new_items)
        if not silent:
            if added:
                app.v_batch_status.set(
                    f"已自动添加 {added} 只即将上市/可交易转债到关注池")
            else:
                app.v_batch_status.set(
                    "关注池已包含所有即将上市/可交易转债, 无新增")
    else:
        app._batch_watchlist = load_watchlist()
        if not silent:
            app.v_batch_status.set("暂无即将上市/可交易的新债")


def _refresh_watchlist_with_upcoming(app):
    """'刷新关注池' 按钮: 检测即将上市新债 → 自动加入关注池 → 刷新显示."""
    _auto_add_upcoming_to_watchlist(app, silent=False)
    _render_watchlist_table(app)


def _save_result_cache(app):
    results = getattr(app, "_batch_all_results", None) or getattr(app, "_batch_results", [])
    if not results:
        messagebox.showinfo("提示", "当前没有可保存的批量定价结果")
        return
    try:
        path = save_batch_results_cache(
            results,
            source=getattr(app, "_last_batch_source", None)
            or (getattr(app, "v_batch_source", None).get() if hasattr(app, "v_batch_source") else None),
            params=getattr(app, "_last_batch_params", None),
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

    results, excluded_count = _filter_nonstandard_results(
        loaded["results"], getattr(app, "terms_cache", None))
    results = sort_batch_results_for_review(results)
    app._batch_all_results = results
    app._batch_upcoming_results = annotate_batch_results(loaded.get("upcoming_results") or [])
    # 自动将即将上市新债加入关注池
    _auto_add_upcoming_to_watchlist(app, silent=True)
    _render_batch_views(
        app,
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


def _filter_nonstandard_results(results, terms_cache=None):
    kept = []
    excluded_count = 0
    for row in results:
        code = row.get("bond_code", "")
        reason = batch_pricing_exclusion_reason(code, row)
        if reason is None and terms_cache is not None and hasattr(terms_cache, "get"):
            try:
                reason = batch_pricing_exclusion_reason(code, terms_cache.get(code))
            except Exception:
                reason = None
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
    menu.add_command(label="载入单债定价页",
                     command=lambda: _load_selection_in_pricing_tab(app))

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


def _load_selection_in_pricing_tab(app):
    tree = getattr(app, "_batch_main_tree", None)
    if tree is None or not app._batch_results:
        return
    selection = tree.selection()
    if not selection:
        messagebox.showinfo("提示", "请先在主批量列表中选择一只转债")
        return
    try:
        row = app._batch_results[int(selection[0])]
    except (ValueError, IndexError):
        return
    code = row.get("bond_code")
    if not code:
        return
    if hasattr(app, "v_bond_code"):
        app.v_bond_code.set(code)
    if hasattr(app, "tab_seg") and hasattr(app, "_switch_tab"):
        app.tab_seg.set("⚡ 定价")
        app._switch_tab("⚡ 定价")
    app.v_batch_status.set(f"已载入单债定价页: {code}")


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
    # 合并主批量定价结果 + 关注池额外定价结果
    by_code = {row.get("bond_code"): row for row in (app._batch_results or [])}
    for row in (getattr(app, "_batch_upcoming_results", None) or []):
        code = row.get("bond_code")
        if code and code not in by_code:
            by_code[code] = row
    rows = []
    for entry in app._batch_watchlist:
        code = entry.get("bond_code")
        merged = dict(entry)
        priced = by_code.get(code)
        if priced:
            for key in ("bond_name", "stock_code", "K", "theoretical_price",
                        "market_price", "deviation", "credit_rating", "status",
                        "parity", "conversion_premium", "opportunity_score",
                        "confidence", "risk_tags", "sensitivity_status",
                        "review_bucket", "review_notes"):
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
    headers = ["代码", "名称", "正股", "机会分", "可信", "理论价", "市价", "偏差(%)", "敏感性", "标签", "状态", "加入时间"]
    col_widths = [100, 90, 80, 70, 45, 70, 70, 70, 90, 160, 90, 150]
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

    _apply_tag_colors(tree)

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
        score = entry.get("opportunity_score")
        vals = [
            code,
            entry.get("bond_name", "") or "",
            entry.get("stock_code", "") or "",
            f"{float(score):.1f}" if _is_finite(score) else "—",
            entry.get("confidence", "") if is_ok else "—",
            f"{float(entry['theoretical_price']):.2f}" if is_ok and entry.get("theoretical_price") is not None else "—",
            f"{float(entry['market_price']):.2f}" if entry.get("market_price") is not None else "—",
            dev_str,
            entry.get("sensitivity_status", "") if is_ok else "—",
            _format_tags(entry.get("risk_tags")),
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
    _TREE_ATTRS.add("_batch_watchlist_tree")
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


def _format_tags(tags) -> str:
    if not tags:
        return ""
    if isinstance(tags, str):
        return tags
    return " / ".join(str(tag) for tag in tags if tag)
