"""📦 批量定价 Tab — 基于 cb_data 转债池 → 并发定价 → 按基差排序导出.

关注池子表 / 事件横幅 / 摘要条已抽到 :mod:`batch_watchlist`,
公共 helper (染色 / 主题刷新 / 数值格式化) 集中在 :mod:`batch_common`。
"""
from __future__ import annotations

import threading
import tkinter as tk
from datetime import date
from typing import TYPE_CHECKING
import customtkinter as ctk
from tkinter import messagebox, filedialog, ttk

from ..theme import *
from ...batch_pricing import (
    AdmissionFilterConfig,
    BATCH_REVIEW_VIEWS,
    DEFAULT_DELIST_WINDOW_DAYS,
    DEFAULT_MIN_CREDIT_RATING,
    DEFAULT_MIN_OUTSTANDING_BALANCE,
    annotate_batch_results,
    batch_pricing_exclusion_reason,
    build_batch_provider,
    filter_batch_results_by_view,
    load_batch_results_cache,
    save_batch_results_cache,
    sort_batch_results_for_review,
    split_batch_codes_from_cache,
    summarize_exclusions,
    summarize_batch_results,
    write_batch_results_csv,
)
from ...pricing_api import batch_price_from_provider_threaded
from ...watchlist import load_watchlist
from ..widgets import Tooltip
from .batch_common import (
    _TREE_ATTRS,
    _apply_tag_colors,
    _attach_cell_tooltip,
    _attach_column_sort,
    _configure_responsive_columns,
    _configure_tree_style,
    _format_tags,
    _is_finite,
    _resolve_row_tag,
    refresh_theme as _refresh_theme_impl,
)
from .batch_watchlist import (
    _add_selection_to_watchlist,
    _auto_add_upcoming_to_watchlist,
    _refresh_watchlist_pricing,
    _refresh_watchlist_with_upcoming,
    _render_watchlist_table,
    _show_events_banner_full,
)

if TYPE_CHECKING:
    from ..app import CBPricerApp

# 列预设: 简洁视图只保留投资决策最常看的字段, 完整视图沿用所有字段
# 状态列: 成功 → ✓ (单字符即可), 失败行保留错误文本, 故宽度大幅收窄
_BATCH_COLS_FULL = (
    ("代码", 100), ("名称", 80), ("正股", 80), ("机会分", 70), ("可信", 45),
    ("转股价值", 70), ("转股溢价(%)", 80), ("σ(%)", 55), ("理论价", 65),
    ("市价", 65), ("偏差(%)", 70), ("评级", 50), ("敏感性", 90),
    ("标签", 180), ("复核建议", 260), ("状态", 60),
)
_BATCH_COLS_SIMPLE = (
    ("代码", 100), ("名称", 90), ("机会分", 70), ("可信", 45),
    ("理论价", 70), ("市价", 70), ("偏差(%)", 75), ("评级", 50),
    ("标签", 220), ("状态", 50),
)
# 列名 → 取值函数, 简洁/完整共用
_BATCH_COL_GETTERS = {
    "代码":         lambda r: r.get("bond_code", ""),
    "名称":         lambda r: r.get("bond_name", ""),
    "正股":         lambda r: r.get("stock_code", ""),
    "机会分":       lambda r: f"{float(r['opportunity_score']):.1f}" if _is_finite(r.get("opportunity_score")) else "—",
    "可信":         lambda r: r.get("confidence", "") if r.get("status") == "ok" else "—",
    "转股价值":     lambda r: f"{float(r['parity']):.2f}" if r.get("status") == "ok" and _is_finite(r.get("parity")) else "—",
    "转股溢价(%)":  lambda r: f"{float(r['conversion_premium'])*100:+.1f}" if _is_finite(r.get("conversion_premium")) else "—",
    "σ(%)":         lambda r: f"{r['sigma']*100:.1f}" if r.get("status") == "ok" and "sigma" in r else "—",
    "理论价":       lambda r: f"{r['theoretical_price']:.2f}" if r.get("status") == "ok" else "—",
    "市价":         lambda r: f"{float(r['market_price']):.2f}" if r.get("status") == "ok" and r.get("market_price") is not None else "—",
    "偏差(%)":      lambda r: f"{float(r['deviation'])*100:+.2f}" if _is_finite(r.get("deviation")) else "—",
    "评级":         lambda r: r.get("credit_rating", ""),
    "敏感性":       lambda r: r.get("sensitivity_status", ""),
    "标签":         lambda r: _format_tags(r.get("risk_tags")),
    "复核建议":     lambda r: _format_tags(r.get("review_notes")),
    "状态":         lambda r: "✓" if r.get("status") == "ok" else r.get("status", ""),
}

_BATCH_COL_STRETCH_WEIGHTS = {
    "代码": 0.5,
    "名称": 1.0,
    "正股": 0.6,
    "机会分": 0.35,
    "可信": 0.2,
    "转股价值": 0.35,
    "转股溢价(%)": 0.4,
    "σ(%)": 0.25,
    "理论价": 0.35,
    "市价": 0.35,
    "偏差(%)": 0.35,
    "评级": 0.25,
    "敏感性": 0.8,
    "标签": 2.0,
    "复核建议": 3.0,
    "状态": 0.25,
}


def build(app, tab):
    """在 tab frame 上构建批量定价面板."""
    tab.grid_columnconfigure(0, weight=1)
    # _build_tabview 默认给 row 0 weight=1 (定价 tab 需要); 这里显式归零, 否则
    # 表格行的 Treeview 自然高度过大时, tkinter 会按权重同步压缩 row 0,
    # 把工具栏 ctrl 从 98px 压到 52px, cc 按钮行被裁出可视区域.
    tab.grid_rowconfigure(0, weight=0)  # ctrl
    tab.grid_rowconfigure(1, weight=0)  # status
    tab.grid_rowconfigure(2, weight=0)  # events banner (默认隐藏)
    tab.grid_rowconfigure(3, weight=1)  # results frame

    # 控制栏
    ctrl = ctk.CTkFrame(tab, fg_color=BG_CARD, corner_radius=12)
    ctrl.grid(row=0, column=0, sticky="ew", pady=(6, 8), padx=16)

    ch = ctk.CTkFrame(ctrl, fg_color="transparent")
    ch.grid(row=0, column=0, sticky="ew", padx=16, pady=(10, 4))
    ctk.CTkLabel(ch, text="📦 批量定价 / 转债池筛选",
                 font=(FONT_FAMILY, 16, "bold"), text_color=TEXT).pack(side="left")
    ctk.CTkLabel(ch, text="基于本地条款库全量转债池 → 并发定价 → 按机会分筛选复核",
                 font=(FONT_FAMILY, 12), text_color=TEXT_DIM).pack(side="left", padx=(12, 0))

    cc = ctk.CTkFrame(ctrl, fg_color="transparent")
    cc.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 10))

    app.v_batch_source = ctk.StringVar(value="Wind")
    ctk.CTkLabel(cc, text="行情源", text_color=TEXT_DIM, font=(FONT_FAMILY, 13)).pack(side="left", padx=(8, 4))
    ctk.CTkOptionMenu(cc, variable=app.v_batch_source, values=["Wind", "akshare"],
                      width=90, font=(FONT_FAMILY, 12), fg_color=BG_INPUT, button_color=BTN_HOVER,
                      text_color=TEXT, dropdown_fg_color=BG_INPUT, dropdown_text_color=TEXT).pack(side="left", padx=(0, 12))

    # 默认进入"低估候选"视图: 评分高、可信度高、无硬复核风险的精选 (偏差异常自动排除)
    # canonical 名 (v_batch_view) 永远是 BATCH_REVIEW_VIEWS 之一; 菜单显示带 "(N)" 计数
    # 后缀的 display var 与之分离, 避免回写 canonical 引发字符串不一致.
    app.v_batch_view = ctk.StringVar(value="低估候选")
    app._batch_view_display_var = ctk.StringVar(value="低估候选")
    ctk.CTkLabel(cc, text="视图", text_color=TEXT_DIM, font=(FONT_FAMILY, 13)).pack(side="left", padx=(0, 4))
    app._batch_view_menu = ctk.CTkOptionMenu(
        cc, variable=app._batch_view_display_var, values=list(BATCH_REVIEW_VIEWS),
        command=lambda label: _on_view_menu_select(app, label),
        width=130, font=(FONT_FAMILY, 12), fg_color=BG_INPUT, button_color=BTN_HOVER,
        text_color=TEXT, dropdown_fg_color=BG_INPUT, dropdown_text_color=TEXT,
    )
    app._batch_view_menu.pack(side="left", padx=(0, 6))

    app.v_batch_cols = ctk.StringVar(value="简洁")
    ctk.CTkLabel(cc, text="列", text_color=TEXT_DIM, font=(FONT_FAMILY, 13)).pack(side="left", padx=(0, 4))
    ctk.CTkSegmentedButton(
        cc, variable=app.v_batch_cols, values=["简洁", "完整"],
        command=lambda _v: _change_batch_view(app),
        font=(FONT_FAMILY, 12), height=28,
        selected_color=ACCENT, selected_hover_color=ACCENT_HOVER,
        unselected_color=BG_INPUT, unselected_hover_color=BTN_HOVER,
        text_color=TEXT, corner_radius=6,
    ).pack(side="left", padx=(0, 12))

    app.btn_batch_run = ctk.CTkButton(
        cc, text="🔄 刷新重算", command=lambda: _run_batch(app),
        fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color=("#ffffff", "#11111b"),
        font=(FONT_FAMILY, 13, "bold"), width=110, height=32, corner_radius=6)
    app.btn_batch_run.pack(side="left")

    # 次要按钮用 BTN_CTRL 而非 BG_INPUT: 浅色模式下 BG_INPUT(#e6e9ef) 与 BG_CARD(#eff1f5) 几乎同色, 按钮看不见
    app.btn_batch_upcoming = ctk.CTkButton(
        cc, text="🆕 扫新债", command=lambda: _refresh_watchlist_with_upcoming(app),
        fg_color=BTN_CTRL, hover_color=BTN_HOVER, text_color=TEXT,
        font=(FONT_FAMILY, 12), width=90, height=32, corner_radius=6)
    app.btn_batch_upcoming.pack(side="left", padx=(8, 0))

    app.btn_batch_add_watch = ctk.CTkButton(
        cc, text="⭐ 加入关注池", command=lambda: _add_selection_to_watchlist(app),
        fg_color=BTN_CTRL, hover_color=BTN_HOVER, text_color=TEXT,
        font=(FONT_FAMILY, 12), width=110, height=32, corner_radius=6)
    app.btn_batch_add_watch.pack(side="left", padx=(8, 0))

    # ⚡ 仅定价关注池: 跳过全市场 322 只, 几秒级反馈; 紧邻 ⭐ 加入关注池
    app.btn_batch_refresh_watch = ctk.CTkButton(
        cc, text="⚡ 关注池重算", command=lambda: _refresh_watchlist_pricing(app),
        fg_color=BTN_CTRL, hover_color=BTN_HOVER, text_color=TEXT,
        font=(FONT_FAMILY, 12), width=110, height=32, corner_radius=6)
    app.btn_batch_refresh_watch.pack(side="left", padx=(8, 0))

    app.btn_batch_export = ctk.CTkButton(
        cc, text="📝 导出 CSV", command=lambda: _export_csv(app),
        fg_color=BTN_CTRL, hover_color=BTN_HOVER, text_color=TEXT,
        font=(FONT_FAMILY, 12), width=90, height=32, corner_radius=6, state="disabled")
    app.btn_batch_export.pack(side="left", padx=(8, 0))

    # ── 公开交易硬过滤; ST/停牌/低评级/小余额等风险默认不进入主池 ──
    ctrl.grid_columnconfigure(0, weight=1)
    app.v_batch_min_rating = ctk.StringVar(value=DEFAULT_MIN_CREDIT_RATING or "")
    app.v_batch_min_balance = ctk.StringVar(
        value="" if DEFAULT_MIN_OUTSTANDING_BALANCE is None else str(DEFAULT_MIN_OUTSTANDING_BALANCE)
    )
    app.v_batch_min_turnover = ctk.StringVar(value="")
    app.v_batch_delist_window = ctk.StringVar(value="0")

    codes, excluded = split_batch_codes_from_cache(
        getattr(app, "terms_cache", None),
        admission_config=_batch_admission_config(app),
    )
    suffix = _excluded_status_suffix(excluded)
    app.v_batch_status = ctk.StringVar(value=f"将基于本地条款库的公开交易转债池定价 ({len(codes)} 只{suffix})")
    ctk.CTkLabel(tab, textvariable=app.v_batch_status,
                 font=(FONT_FAMILY, 13, "bold"), text_color=TEXT).grid(
                     row=1, column=0, sticky="w", padx=24, pady=(2, 8))

    # 事件 banner (近 30 天关注池内事件), 仅在有内容时显示; 单击弹窗展开全部
    app.v_batch_events_banner = ctk.StringVar(value="")
    app._batch_events_banner_full: list[tuple[str, str, "date"]] = []
    app.lbl_batch_events_banner = ctk.CTkLabel(
        tab, textvariable=app.v_batch_events_banner,
        font=(FONT_FAMILY, 12, "bold"), text_color=ORANGE,
        fg_color=BG_CARD, corner_radius=12,
        padx=12, pady=8,
        anchor="w", justify="left", wraplength=1080, cursor="hand2")
    app.lbl_batch_events_banner.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 8))
    app.lbl_batch_events_banner.grid_remove()
    app.lbl_batch_events_banner.bind(
        "<Button-1>", lambda _e: _show_events_banner_full(app))

    # 结果表格区: 主批量列表 + 我的关注池 (含自动发现的即将上市新债)
    app.batch_results_frame = ctk.CTkFrame(tab, fg_color="transparent")
    app.batch_results_frame.grid(row=3, column=0, sticky="nsew", padx=16, pady=(0, 6))
    app.batch_results_frame.grid_columnconfigure(0, weight=1)
    app.batch_results_frame.grid_rowconfigure(0, weight=3)
    app.batch_results_frame.grid_rowconfigure(1, weight=2)

    app.batch_table_frame = _create_table_section(
        app.batch_results_frame, row=0, title="主批量定价结果")
    app.batch_watchlist_table_frame, app.v_batch_watchlist_summary = _create_table_section(
        app.batch_results_frame, row=1, title="⭐ 我的关注池 (右键删除)",
        with_summary=True)

    app._batch_results = []
    app._batch_all_results = []
    app._batch_upcoming_results = []
    app._batch_watchlist = load_watchlist()
    # 自动发现即将上市/可交易的新债并加入关注池
    _auto_add_upcoming_to_watchlist(app, silent=True)
    _render_watchlist_table(app)  # 内部已调用 _refresh_watchlist_summary + _refresh_events_banner
    # 启动时异步加载上次的批量定价缓存; 缓存文件 ~440KB, 同步读会让窗口出现前停顿
    # 80ms 延迟让 mainloop 先完成首屏绘制, 主表加载后再调一次 _render_batch_views 不影响关注池
    app.after(80, lambda: _load_result_cache(app, silent=True))


def _create_table_section(parent, *, row, title, with_summary=False):
    section = ctk.CTkFrame(parent, fg_color=BG_CARD, corner_radius=12)
    section.grid(row=row, column=0, sticky="nsew", pady=(0, 8) if row == 0 else (0, 0))
    section.grid_columnconfigure(0, weight=1)

    header = ctk.CTkFrame(section, fg_color="transparent")
    header.grid(row=0, column=0, sticky="ew", padx=12, pady=(8, 2))
    header.grid_columnconfigure(1, weight=1)
    ctk.CTkLabel(
        header, text=title,
        font=(FONT_FAMILY, 13, "bold"), text_color=TEXT,
    ).grid(row=0, column=0, sticky="w")

    summary_var = None
    if with_summary:
        summary_var = ctk.StringVar(value="")
        ctk.CTkLabel(
            header, textvariable=summary_var,
            font=(FONT_FAMILY, 11), text_color=TEXT_DIM, anchor="e",
        ).grid(row=0, column=1, sticky="e", padx=(12, 0))

    body_row = 1
    section.grid_rowconfigure(body_row, weight=1)
    body = ctk.CTkFrame(section, fg_color="transparent")
    body.grid(row=body_row, column=0, sticky="nsew")
    body.grid_columnconfigure(0, weight=1)
    body.grid_rowconfigure(0, weight=1)
    if with_summary:
        return body, summary_var
    return body


def _run_batch(app):
    codes, excluded = split_batch_codes_from_cache(
        getattr(app, "terms_cache", None),
        admission_config=_batch_admission_config(app),
    )
    if not codes:
        messagebox.showwarning("提示", "本地条款库的公开交易转债池为空, 请先同步基础信息")
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
        success_count = sum(1 for row in results if row.get("status") == "ok")
        if success_count == 0:
            cached = _load_successful_result_cache(app)
            if cached is not None:
                app.after(0, lambda: _render_cached_after_failed_batch(
                    app, provider.name, cached))
                return
            app._batch_results = results
            app._batch_upcoming_results = watchlist_pricing
            app.after(0, lambda: _render_batch_views(
                app, results, excluded_count=excluded_count))
            app.after(0, lambda: app.v_batch_status.set(
                f"{provider.name} 本次批量定价全部失败，未更新缓存"))
            return

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


def _load_successful_result_cache(app):
    try:
        loaded = load_batch_results_cache()
    except Exception:
        return None
    results, excluded_count = _filter_nonstandard_results(
        loaded["results"], getattr(app, "terms_cache", None),
        admission_config=_batch_admission_config(app))
    if not any(row.get("status") == "ok" for row in results):
        return None
    return {
        "results": sort_batch_results_for_review(results),
        "upcoming_results": annotate_batch_results(loaded.get("upcoming_results") or []),
        "meta": loaded.get("meta"),
        "excluded_count": excluded_count,
    }


def _render_cached_after_failed_batch(app, provider_name, cached):
    app._batch_all_results = cached["results"]
    app._batch_upcoming_results = cached["upcoming_results"]
    _render_batch_views(
        app,
        cache_meta=cached.get("meta"),
        excluded_count=cached.get("excluded_count", 0),
    )
    app.v_batch_status.set(
        f"{provider_name} 本次批量定价全部失败，已保留并显示上次可用缓存")


def _excluded_status_suffix(excluded):
    if not excluded:
        return ""
    by_reason = summarize_exclusions(excluded)
    top = "、".join(f"{reason}{count}" for reason, count in list(by_reason.items())[:2])
    return f", 公开交易过滤 {len(excluded)} 只 ({top})"


def _batch_optional_pos_float(var):
    """解析非负浮点; 留空或负数表示关闭该过滤项 (返回 None)."""
    raw = var.get().strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def _batch_int(var, default):
    raw = var.get().strip()
    if not raw:
        return default
    try:
        return max(0, int(float(raw)))
    except ValueError:
        return default


def _batch_admission_config(app):
    """构造公开交易硬过滤配置."""
    return AdmissionFilterConfig(
        delist_window_days=_batch_int(app.v_batch_delist_window, DEFAULT_DELIST_WINDOW_DAYS),
        min_outstanding_balance=_batch_optional_pos_float(app.v_batch_min_balance),
        min_credit_rating=(app.v_batch_min_rating.get().strip() or None),
        min_turnover_amount=_batch_optional_pos_float(app.v_batch_min_turnover),
    )


def _canonical_view_name(label: str) -> str:
    """剥离视图标签里的 ' (24)' 计数后缀, 还原为 BATCH_REVIEW_VIEWS 里的标准名."""
    if not label:
        return "综合机会"
    name = label.split(" (")[0]
    return name if name in BATCH_REVIEW_VIEWS else "综合机会"


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
    view = _canonical_view_name(
        app.v_batch_view.get() if hasattr(app, "v_batch_view") else "综合机会")
    display_results = filter_batch_results_by_view(base_results, view)
    app._batch_results = display_results
    _refresh_view_menu_labels(app, base_results)
    _render_table(app, display_results, total_results=len(base_results), view=view, cache_path=cache_path,
                  cache_meta=cache_meta, excluded_count=excluded_count)
    _render_watchlist_table(app)


def _refresh_view_menu_labels(app, base_results):
    """根据当前结果实时计算各视图条数, 仅写入 *display var* (e.g. '低估候选 (24)').

    canonical 名 ``v_batch_view`` 始终保持纯净的 ``BATCH_REVIEW_VIEWS`` 之一,
    避免被 ``(N)`` 计数后缀污染。
    """
    menu = getattr(app, "_batch_view_menu", None)
    display_var = getattr(app, "_batch_view_display_var", None)
    if menu is None or display_var is None:
        return
    counts = {
        view: len(filter_batch_results_by_view(base_results, view))
        for view in BATCH_REVIEW_VIEWS
    }
    canonical = list(BATCH_REVIEW_VIEWS)
    decorated = [f"{name} ({counts.get(name, 0)})" for name in canonical]

    current_name = _canonical_view_name(app.v_batch_view.get())
    target_label = decorated[canonical.index(current_name)]
    menu.configure(values=decorated)
    # 程式化 set 不会触发 CTkOptionMenu 的 command, 因此不会递归回到这里
    if display_var.get() != target_label:
        display_var.set(target_label)


def _on_view_menu_select(app, label: str) -> None:
    """用户从下拉菜单选择 → 把 canonical 名写回 ``v_batch_view`` 并刷新."""
    canonical = _canonical_view_name(label)
    if app.v_batch_view.get() != canonical:
        app.v_batch_view.set(canonical)
    _change_batch_view(app)


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

    cols_preset = (app.v_batch_cols.get()
                   if hasattr(app, "v_batch_cols") else "简洁")
    schema = _BATCH_COLS_SIMPLE if cols_preset == "简洁" else _BATCH_COLS_FULL
    headers = [name for name, _ in schema]
    col_widths = [w for _, w in schema]
    columns = [f"c{i}" for i in range(len(headers))]

    _configure_tree_style()
    tree = ttk.Treeview(
        app.batch_table_frame,
        columns=columns,
        show="headings",
        selectmode="extended",
    )
    y_scroll = ctk.CTkScrollbar(
        app.batch_table_frame, orientation="vertical", command=tree.yview,
        width=10, fg_color="transparent", button_color=BORDER,
        button_hover_color=TEXT_DIM,
    )
    x_scroll = ctk.CTkScrollbar(
        app.batch_table_frame, orientation="horizontal", command=tree.xview,
        height=8, fg_color="transparent", button_color=BORDER,
        button_hover_color=TEXT_DIM,
    )
    tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)

    tree.grid(row=0, column=0, sticky="nsew", padx=(10, 0), pady=(6, 0))
    y_scroll.grid(row=0, column=1, sticky="ns", pady=(6, 0), padx=(0, 10))
    x_scroll.grid(row=1, column=0, sticky="ew", padx=(10, 0), pady=(0, 8))

    _configure_responsive_columns(
        tree, columns, headers, col_widths,
        stretch_weights=_BATCH_COL_STRETCH_WEIGHTS,
    )

    _apply_tag_colors(tree)
    _attach_column_sort(tree, columns, headers)
    _attach_cell_tooltip(tree, columns, headers, tooltip_headers={"标签", "复核建议"})
    app._batch_main_tree = tree
    _TREE_ATTRS.add("_batch_main_tree")
    _attach_main_context_menu(app, tree)

    for idx, r in enumerate(results):
        vals = [_BATCH_COL_GETTERS[name](r) for name, _ in schema]
        row_tag = _resolve_row_tag(r)
        tags = [row_tag] if row_tag else []
        tree.insert("", "end", iid=str(idx), values=vals, tags=tags)

    summary = summarize_batch_results(results)
    total = total_results if total_results is not None else summary["total"]
    view_name = view or "综合机会"
    parts = [
        f"✅ {view_name}: 展示 {summary['total']}/{total} 只",
        f"成功 {summary['success']}  失败 {summary['failed']}",
    ]
    if excluded_count:
        parts.append(f"公开交易过滤 {excluded_count} 只")
    app.v_batch_status.set("  |  ".join(parts))
    app.btn_batch_export.configure(state="normal")

    # 缓存时效信息搬到状态栏 (左侧 _data_freshness 区), 不再挤占复核状态行
    saved_at_iso: str | None = None
    if cache_path is not None:
        from datetime import datetime as _dt
        saved_at_iso = _dt.now().isoformat(timespec="seconds")
    elif cache_meta:
        saved_at_iso = cache_meta.get("saved_at")
    if hasattr(app, "_set_batch_freshness"):
        app._set_batch_freshness(saved_at_iso)


def refresh_theme(app: "CBPricerApp") -> None:
    """主题切换后刷新 Treeview 样式 + 给所有已注册树重新染色.

    ``app.py`` 的 ``_toggle_theme`` 在 ``ctk.set_appearance_mode`` 之后调用本函数.
    """
    _refresh_theme_impl(app)


def _load_result_cache(app, *, silent: bool = False):
    try:
        loaded = load_batch_results_cache()
    except FileNotFoundError as exc:
        if not silent:
            messagebox.showinfo("提示", str(exc))
        return
    except Exception as exc:
        if not silent:
            messagebox.showerror("加载缓存失败", str(exc))
        return

    results, excluded_count = _filter_nonstandard_results(
        loaded["results"], getattr(app, "terms_cache", None),
        admission_config=_batch_admission_config(app))
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


def _filter_nonstandard_results(results, terms_cache=None, admission_config=None):
    kept = []
    excluded_count = 0
    for row in results:
        code = row.get("bond_code", "")
        reason = batch_pricing_exclusion_reason(code, row, admission_config=admission_config)
        if reason is None and terms_cache is not None and hasattr(terms_cache, "get"):
            try:
                reason = batch_pricing_exclusion_reason(
                    code, terms_cache.get(code), admission_config=admission_config)
            except Exception:
                reason = None
        if reason is None:
            kept.append(row)
        else:
            excluded_count += 1
    return kept, excluded_count


def _attach_main_context_menu(app, tree):
    menu = tk.Menu(tree, tearoff=0, font=(FONT_FAMILY, 12))
    menu.add_command(label="⭐ 加入关注池",
                     command=lambda: _add_selection_to_watchlist(app))
    menu.add_command(label="载入单债定价页 (双击)",
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

    def _on_double_click(event):
        clicked = tree.identify_row(event.y)
        if not clicked:
            return
        tree.selection_set(clicked)
        _load_selection_in_pricing_tab(app)

    tree.bind("<Button-3>", _popup)
    tree.bind("<Button-2>", _popup)
    tree.bind("<Double-1>", _on_double_click)


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
        app.tab_seg.set(E("⚡ 定价"))
        app._switch_tab(E("⚡ 定价"))
    app.v_batch_status.set(f"已载入单债定价页: {code}")
