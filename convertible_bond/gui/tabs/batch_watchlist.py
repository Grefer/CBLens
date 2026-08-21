"""⭐ 关注池子表 / 摘要条 / 事件横幅 — 从 batch.py 抽离.

设计原则:
- 公共 helper (染色 / 格式化 / 主题刷新) 集中在 :mod:`batch_common`, 两侧共用同一份模块级 ``_TREE_ATTRS`` 注册集。
- 与主表的双向 callback (关注池刷新后需要重画主表) 通过 *延迟导入* 处理, 避免 ``batch.py`` ↔ ``batch_watchlist.py`` 形成循环依赖。
"""
from __future__ import annotations

import importlib.util
import threading
import tkinter as tk
from datetime import date, datetime, timedelta
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk

from ..theme import *  # noqa: F401,F403  保持与 batch.py 一致的颜色 / 字体常量入口
from ...batch_pricing import (
    annotate_batch_results,
    average_rating_label,
    build_batch_provider,
    list_upcoming_tradable_from_cache,
    sort_batch_results_for_review,
)
from ...pricing_api import batch_price_from_provider_threaded
from ...watchlist import (
    add_to_watchlist,
    load_watchlist,
    remove_from_watchlist,
)
from .batch_common import (
    _TREE_ATTRS,
    _apply_tag_colors,
    _attach_cell_tooltip,
    _attach_column_sort,
    _configure_responsive_columns,
    _configure_tree_style,
    _format_tags,
    _is_finite,
    _is_new_bond,
    _median,
    _resolve_row_tag,
)
from ...market_time import market_today

_WATCHLIST_COL_STRETCH_WEIGHTS = {
    "代码": 0.5,
    "名称": 1.0,
    "正股": 0.6,
    "上市日": 0.75,
    "可交易日": 0.75,
    "距交易": 0.25,
    "机会分": 0.35,
    "可信": 0.2,
    "理论价": 0.35,
    "市价": 0.35,
    "偏差(%)": 0.35,
    "加入时偏差(%)": 0.45,
    "市价变化(%)": 0.45,
    "敏感性": 0.8,
    "标签": 2.0,
    "状态": 0.25,
    "加入时间": 1.4,
}


# ── 关注池新债自动发现 / 刷新 ─────────────────────────────────
# 扫描窗口: 已定上市日的新债往前看 30 天 (交易所公告通常提前 1-2 周,
# 7 天窗口会让"周一发公告、下周三上市"这类债只在最后两天才出现)。
# "已发行未上市"那一类不受本窗口约束 —— 它们连上市日都还没有。
_UPCOMING_SCAN_WINDOW_DAYS = 30

# 必须与 wind_sync._POOL_SYNC_TARGETS 中的条目一致 (label 用于匹配确认文案)
_TERMS_SYNC_MODULE = "convertible_bond.cli.sync_tradable"
_TERMS_SYNC_LABEL = "🔄 增量更新基础信息 (推荐)"
# 条款库超过这个天数就在扫新债前提示同步 —— 新债每天都在发, 隔夜的快照就可能漏
_TERMS_STALE_DAYS = 1.0


def _terms_sync_available() -> bool:
    """条款库同步只走 Wind (cb-sync-tradable 固定 --source wind); 没装就别提示."""
    try:
        from ...data_providers.wind import prepare_windpy_import_path
        prepare_windpy_import_path()
        return importlib.util.find_spec("WindPy") is not None
    except Exception:
        return False


def _terms_bundle_age_days(app) -> float | None:
    """本地条款库距上次同步的天数; 无法判断时返回 None."""
    cache = getattr(app, "terms_cache", None)
    if cache is None or not hasattr(cache, "bundle_meta"):
        return None
    try:
        updated = (cache.bundle_meta() or {}).get("updated_at")
        if not updated:
            return None
        return (datetime.now() - datetime.fromisoformat(str(updated))).total_seconds() / 86400.0
    except (TypeError, ValueError):
        return None


def _auto_add_upcoming_to_watchlist(app, *, silent=False):
    """自动发现尚未开始交易的新债 (含已发行未上市) 并加入关注池."""
    upcoming = list_upcoming_tradable_from_cache(
        getattr(app, "terms_cache", None),
        window_days=_UPCOMING_SCAN_WINDOW_DAYS,
    )
    if upcoming:
        new_items = [dict(r) for r in upcoming]
        app._batch_watchlist, added = add_to_watchlist(new_items)
        if not silent:
            if added:
                app.v_batch_status.set(f"已自动添加 {added} 只新债到关注池")
            else:
                app.v_batch_status.set("关注池已包含所有已发行/即将上市的新债, 无新增")
    else:
        app._batch_watchlist = load_watchlist()
        if not silent:
            app.v_batch_status.set("暂无已发行未上市或即将上市的新债")


def _refresh_watchlist_with_upcoming(app):
    """'扫新债' 按钮: (可选) 先增量同步条款库 → 扫描新债 → 加入关注池 → 立刻定价.

    只扫本地 cb_data 的话, 新债得等用户想起来手动同步基础信息才看得见; 这里把
    "同步 → 扫描 → 定价"串成一步, 让按钮自己就能拿到当天新发的债和它的理论价。
    """
    age = _terms_bundle_age_days(app)
    if _terms_sync_available() and (age is None or age >= _TERMS_STALE_DAYS):
        age_text = "无法判断上次同步时间" if age is None else f"本地条款库最后同步于 {age:.1f} 天前"
        if messagebox.askyesno(
            "先同步基础信息?",
            f"新债要先同步进本地条款库才扫得到 ({age_text}).\n\n"
            "「是」: 先跑一次增量同步 (需 Wind 终端, 通常 1-3 分钟), 完成后自动继续扫描并定价\n"
            "「否」: 直接扫描现有条款库",
        ):
            app._run_pool_sync(
                _TERMS_SYNC_MODULE, _TERMS_SYNC_LABEL, ("--incremental",),
                confirm=False,
                on_success=lambda: _scan_upcoming_and_price(app, reload_terms=True),
            )
            return
    _scan_upcoming_and_price(app)


def _scan_upcoming_and_price(app, *, reload_terms: bool = False):
    """扫描新债 → 加入关注池 → 对还没有理论价的关注标的立刻定价."""
    if reload_terms:
        cache = getattr(app, "terms_cache", None)
        if hasattr(cache, "reload"):
            try:
                cache.reload()
            except Exception as exc:
                app.v_batch_status.set(f"⚠ 条款库重载失败: {exc}")

    _auto_add_upcoming_to_watchlist(app, silent=False)
    _render_watchlist_table(app)

    # 新债刚加进来时只有条款元数据, 表里一排空白; 顺手把还没有理论价的新债算出来,
    # 否则"扫新债"给出的只是一张代码清单, 没法判断贵贱。
    # 只管新债 — 关注池里其他没定价的标的交给 ⚡ 关注池重算, 免得一个按钮做两件事。
    pending = [
        row.get("bond_code")
        for row in _watchlist_display_rows(app)
        if row.get("bond_code") and row.get("status") != "ok" and _is_new_bond(row)
    ]
    if pending:
        scanned = app.v_batch_status.get()
        if _start_watchlist_pricing(app, pending, note=f"新债 {len(pending)} 只"):
            app.v_batch_status.set(f"{scanned} · 正在定价 {len(pending)} 只新债 ...")


# ── ⚡ 关注池快速重定价 ─────────────────────────────────────────
def _refresh_watchlist_pricing(app):
    """⚡ 仅对关注池的代码执行定价 — 跳过全市场, 秒级返回."""
    codes = [e.get("bond_code") for e in (app._batch_watchlist or []) if e.get("bond_code")]
    if not codes:
        messagebox.showinfo("提示", "关注池为空 — 在主表中右键加入或点 🆕 扫新债")
        return
    _start_watchlist_pricing(app, codes)


def _start_watchlist_pricing(app, codes, *, note: str | None = None) -> bool:
    """对给定代码起一轮关注池定价; 参数不合法或已在跑时返回 False."""
    codes = [c for c in dict.fromkeys(codes) if c]
    if not codes:
        return False

    source = app.v_batch_source.get()
    csv_root = getattr(app, "_csv_root", None)
    if source == "CSV" and not csv_root:
        csv_root = filedialog.askdirectory(title="选择 CSV 数据根目录 (含 bonds/ stocks/ terms/ 子目录)")
        if not csv_root:
            return False
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
    except ValueError as exc:
        messagebox.showerror("参数错误", str(exc))
        return False

    label = note or f"关注池 {len(codes)} 只"
    app.btn_batch_refresh_watch.configure(state="disabled")
    app.v_batch_status.set(f"⚡ 正在定价{label} ...")
    app._start_progress(f"定价{label}")

    threading.Thread(
        target=_watchlist_pricing_worker,
        args=(app, codes, source, csv_root, params),
        daemon=True,
    ).start()
    return True


def _watchlist_pricing_worker(app, codes, source, csv_root, params):
    # 延迟导入: 关注池刷新后回调主表渲染, 避免 batch ↔ batch_watchlist 循环导入
    from .batch import _render_batch_views

    try:
        provider = build_batch_provider(
            source,
            terms_cache=getattr(app, "terms_cache", None),
            csv_root=csv_root,
            max_age_days=30,
        )
        try:
            rf = provider.get_risk_free_rate(market_today())
            if rf is not None:
                params = dict(params, r=float(rf) / 100.0)
        except Exception:
            pass

        def on_progress(done, total):
            app.after(0, lambda: app.v_batch_status.set(
                f"{provider.name} 关注池进度 {done}/{total} ..."))

        results = batch_price_from_provider_threaded(
            provider, codes, progress_cb=on_progress, **params)
        results = annotate_batch_results(results)

        # 把结果合并: 主结果里有的就更新主结果, 否则写到 upcoming_results
        main_by_code = {r.get("bond_code"): i for i, r in enumerate(app._batch_all_results or [])}
        upcoming_by_code = {r.get("bond_code"): i for i, r in enumerate(app._batch_upcoming_results or [])}
        new_upcoming = list(app._batch_upcoming_results or [])
        new_main = list(app._batch_all_results or [])
        for row in results:
            code = row.get("bond_code")
            if not code:
                continue
            if code in main_by_code:
                new_main[main_by_code[code]] = row
            elif code in upcoming_by_code:
                new_upcoming[upcoming_by_code[code]] = row
            else:
                new_upcoming.append(row)

        app._batch_all_results = sort_batch_results_for_review(new_main)
        app._batch_upcoming_results = new_upcoming
        app.after(0, lambda: _render_batch_views(app))
        app.after(0, lambda: app.v_batch_status.set(
            f"⚡ 已刷新关注池 {len(codes)} 只 (主表 {sum(1 for c in codes if c in main_by_code)} / 关注 {len(codes) - sum(1 for c in codes if c in main_by_code)})"))
    except Exception as exc:
        app.after(0, lambda exc=exc: app.v_batch_status.set(f"❌ 关注池定价失败: {exc}"))
        app.after(0, lambda exc=exc: messagebox.showerror("关注池定价失败", str(exc)))
    finally:
        app.after(0, app._stop_progress)
        app.after(0, lambda: app.btn_batch_refresh_watch.configure(state="normal"))


# ── 加入 / 移除 / 渲染 ───────────────────────────────────────
def _add_selection_to_watchlist(app):
    """从主批量表选中行 → 加入关注池, 顺手存研究信号快照."""
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
        # 加入瞬间快照 — 让回头看时能复盘"我当时为什么觉得这债便宜"
        item = {
            "bond_code": code,
            "bond_name": row.get("bond_name"),
            "stock_code": row.get("stock_code"),
            "snapshot_deviation": row.get("deviation") if _is_finite(row.get("deviation")) else None,
            "snapshot_opportunity_score": row.get("opportunity_score") if _is_finite(row.get("opportunity_score")) else None,
            "snapshot_market_price": row.get("market_price") if _is_finite(row.get("market_price")) else None,
            "snapshot_theoretical_price": row.get("theoretical_price") if _is_finite(row.get("theoretical_price")) else None,
        }
        for key in (
            "listing_date", "tradable_date", "is_tradable", "trading_status",
            "credit_rating", "outstanding_balance", "underlying_name", "K",
            "market_price",
        ):
            value = row.get(key)
            if value is not None:
                item[key] = value
        new_items.append(item)
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
    """合并主批量定价结果 + 关注池额外定价结果, 生成关注池表展示行."""
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
                        "review_bucket", "review_notes", "listing_date",
                        "tradable_date", "is_tradable", "trading_status",
                        "underlying_name", "outstanding_balance",
                        "maturity_date"):
                value = priced.get(key)
                if value is not None:
                    merged[key] = value
        rows.append(merged)
    return rows


def _parse_watchlist_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _format_watchlist_date(value):
    parsed = _parse_watchlist_date(value)
    return parsed.isoformat() if parsed else "—"


def _is_pending_listing(entry) -> bool:
    """已发行未上市: 还没挂牌, 上市日/可交易日都是"待定"而不是"没有数据"."""
    return (
        str(entry.get("trading_status") or "").strip().lower() == "pending"
        and _parse_watchlist_date(entry.get("listing_date")) is None
    )


def _format_listing_cell(entry, key):
    parsed = _parse_watchlist_date(entry.get(key))
    if parsed is not None:
        return parsed.isoformat()
    return "待定" if _is_pending_listing(entry) else "—"


def _format_days_to_trade(entry):
    """还有几天能买 — 已经能买的说"已可交易", 不显示负数.

    这列是"距交易", 负数读起来像"欠了 3 天", 没有意义: 可交易日已过就是能买了。
    """
    tradable_date = _parse_watchlist_date(entry.get("tradable_date"))
    if tradable_date is not None:
        days = (tradable_date - market_today()).days
    elif _is_pending_listing(entry):
        # 上市日未公告时 days_to_trade 可能是上一轮扫描留下的旧值, 不能拿来显示
        return "待定"
    else:
        days = entry.get("days_to_trade")
        try:
            days = int(days)
        except (TypeError, ValueError):
            return "—"
    if days <= 0:
        return "已可交易"
    return f"+{days}"


def _render_watchlist_table(app):
    frame = getattr(app, "batch_watchlist_table_frame", None)
    if frame is None:
        return
    for child in frame.winfo_children():
        child.destroy()

    rows = _watchlist_display_rows(app)
    headers = ["代码", "名称", "正股", "上市日", "可交易日", "距交易",
               "机会分", "可信", "理论价", "市价", "偏差(%)",
               "加入时偏差(%)", "市价变化(%)", "敏感性", "标签", "状态", "加入时间"]
    col_widths = [100, 90, 80, 90, 90, 58, 70, 45, 70, 70, 70, 95, 95, 90, 160, 50, 150]
    columns = [f"w{i}" for i in range(len(headers))]

    _configure_tree_style()
    tree = ttk.Treeview(
        frame,
        columns=columns,
        show="headings",
        selectmode="extended",
    )
    y_scroll = ctk.CTkScrollbar(
        frame, orientation="vertical", command=tree.yview,
        width=10, fg_color="transparent", button_color=BORDER,
        button_hover_color=TEXT_DIM,
    )
    x_scroll = ctk.CTkScrollbar(
        frame, orientation="horizontal", command=tree.xview,
        height=8, fg_color="transparent", button_color=BORDER,
        button_hover_color=TEXT_DIM,
    )
    tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)

    tree.grid(row=0, column=0, sticky="nsew", padx=(10, 0), pady=(6, 0))
    y_scroll.grid(row=0, column=1, sticky="ns", pady=(6, 0), padx=(0, 10))
    x_scroll.grid(row=1, column=0, sticky="ew", padx=(10, 0), pady=(0, 8))

    _configure_responsive_columns(
        tree, columns, headers, col_widths,
        stretch_weights=_WATCHLIST_COL_STRETCH_WEIGHTS,
    )

    _apply_tag_colors(tree)
    _attach_column_sort(tree, columns, headers)
    _attach_cell_tooltip(tree, columns, headers, tooltip_headers={"标签"})

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
        snap_dev = entry.get("snapshot_deviation")
        snap_dev_str = f"{float(snap_dev) * 100:+.2f}" if _is_finite(snap_dev) else "—"
        # 市价变化 = (current − snapshot) / snapshot, 老条目无快照时显示 "—"
        cur_mkt = entry.get("market_price")
        snap_mkt = entry.get("snapshot_market_price")
        if _is_finite(cur_mkt) and _is_finite(snap_mkt) and float(snap_mkt) > 0:
            mkt_chg_str = f"{(float(cur_mkt) - float(snap_mkt)) / float(snap_mkt) * 100:+.2f}"
        else:
            mkt_chg_str = "—"
        is_ok = entry.get("status") == "ok"
        score = entry.get("opportunity_score")
        vals = [
            code,
            entry.get("bond_name", "") or "",
            entry.get("stock_code", "") or "",
            _format_listing_cell(entry, "listing_date"),
            _format_listing_cell(entry, "tradable_date"),
            _format_days_to_trade(entry),
            f"{float(score):.1f}" if _is_finite(score) else "—",
            entry.get("confidence", "") if is_ok else "—",
            f"{float(entry['theoretical_price']):.2f}" if is_ok and entry.get("theoretical_price") is not None else "—",
            f"{float(entry['market_price']):.2f}" if entry.get("market_price") is not None else "—",
            dev_str,
            snap_dev_str,
            mkt_chg_str,
            entry.get("sensitivity_status", "") if is_ok else "—",
            _format_tags(entry.get("risk_tags")),
            "✓" if is_ok else (entry.get("status") or "—"),
            entry.get("added_at", "") or "",
        ]
        row_tag = _resolve_row_tag(entry)
        tags = [row_tag] if row_tag else []
        tree.insert("", "end", iid=code, values=vals, tags=tags)

    app._batch_watchlist_tree = tree
    _TREE_ATTRS.add("_batch_watchlist_tree")
    _attach_watchlist_context_menu(app, tree)
    _refresh_watchlist_summary(app, rows)
    _refresh_events_banner(app)


def _attach_watchlist_context_menu(app, tree):
    menu = tk.Menu(tree, tearoff=0, font=(FONT_FAMILY, 12))
    menu.add_command(label="载入单债定价页 (双击)",
                     command=lambda: _load_watchlist_selection_in_pricing_tab(app))
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

    def _on_double_click(event):
        clicked = tree.identify_row(event.y)
        if not clicked:
            return
        tree.selection_set(clicked)
        _load_watchlist_selection_in_pricing_tab(app)

    tree.bind("<Button-3>", _popup)
    tree.bind("<Button-2>", _popup)
    tree.bind("<Double-1>", _on_double_click)
    tree.bind("<Delete>", lambda _e: _remove_selected_from_watchlist(app))
    tree.bind("<BackSpace>", lambda _e: _remove_selected_from_watchlist(app))


def _load_watchlist_selection_in_pricing_tab(app):
    tree = getattr(app, "_batch_watchlist_tree", None)
    if tree is None:
        return
    selection = tree.selection()
    if not selection:
        return
    code = selection[0]  # 关注池表的 iid 就是 bond_code
    if not code:
        return
    if hasattr(app, "v_bond_code"):
        app.v_bond_code.set(code)
    if hasattr(app, "tab_seg") and hasattr(app, "_switch_tab"):
        app.tab_seg.set(E("⚡ 定价"))
        app._switch_tab(E("⚡ 定价"))
    app.v_batch_status.set(f"已载入单债定价页: {code}")


# ── 摘要条 / 事件横幅 ─────────────────────────────────────────
def _refresh_watchlist_summary(app, rows):
    """汇总关注池: 持仓数 / 等权偏差中位 / 平均机会分 / 平均评级 / 异常计数."""
    summary_var = getattr(app, "v_batch_watchlist_summary", None)
    if summary_var is None:
        return
    if not rows:
        summary_var.set("")
        return

    n = len(rows)
    priced = [r for r in rows if r.get("status") == "ok"]
    devs = [r.get("deviation") for r in priced]
    scores = [r.get("opportunity_score") for r in priced]
    median_dev = _median(devs)
    finite_scores = [float(s) for s in scores if _is_finite(s)]
    mean_score = sum(finite_scores) / len(finite_scores) if finite_scores else None

    rating_label = average_rating_label(r.get("credit_rating") for r in priced) or "—"

    anomaly_count = sum(1 for r in priced
                        if "偏差异常" in (r.get("risk_tags") or []))

    parts = [f"持仓 {n}"]
    if priced:
        parts.append(f"已定价 {len(priced)}")
    if median_dev is not None:
        parts.append(f"偏差中位 {median_dev*100:+.1f}%")
    if mean_score is not None:
        parts.append(f"机会分均值 {mean_score:.1f}")
    if rating_label != "—":
        parts.append(f"平均评级 {rating_label}")
    if anomaly_count:
        parts.append(f"⚠ 异常 {anomaly_count}")
    summary_var.set("  ·  ".join(parts))


def _refresh_events_banner(app, *, window_days: int = 30):
    """扫描关注池在未来 window_days 天的事件, 拼成横幅; 无事件时隐藏."""
    label = getattr(app, "lbl_batch_events_banner", None)
    var = getattr(app, "v_batch_events_banner", None)
    if label is None or var is None:
        return

    store = getattr(app, "event_store", None)
    if store is None:
        label.grid_remove()
        return

    today = market_today()
    horizon = today + timedelta(days=window_days)
    watchlist_codes = {e.get("bond_code") for e in (app._batch_watchlist or []) if e.get("bond_code")}

    def _in_window(ev) -> bool:
        for d in (ev.event_date, ev.effective_start, ev.effective_end):
            if d is None:
                continue
            if today <= d <= horizon:
                return True
        return False

    upcoming: list[tuple[str, str, date]] = []
    for code in watchlist_codes:
        try:
            evs = store.list_events(bond_code=code)
        except Exception:
            continue
        for ev in evs:
            if not _in_window(ev):
                continue
            ref_date = ev.effective_start or ev.event_date
            label_text = (ev.event_type or "事件").replace("_", " ")
            upcoming.append((code, label_text, ref_date))

    if not upcoming:
        var.set("")
        app._batch_events_banner_full = []
        label.grid_remove()
        return

    upcoming.sort(key=lambda t: t[2])
    app._batch_events_banner_full = list(upcoming)
    head = upcoming[:5]
    parts = [f"{c} {t} ({d.isoformat()})" for c, t, d in head]
    suffix = f"  ·  ...展开 {len(upcoming) - 5} 件" if len(upcoming) > 5 else ""
    var.set(f"⚠ 关注池近 {window_days} 天事件 (单击查看全部): " + "  ·  ".join(parts) + suffix)
    label.grid()


def _show_events_banner_full(app):
    """单击事件横幅 → 弹窗按日期分组展示全部事件."""
    full = getattr(app, "_batch_events_banner_full", None)
    if not full:
        return
    win = ctk.CTkToplevel(app)
    win.title(f"关注池近 30 天事件 ({len(full)} 件)")
    win.geometry("520x420")
    win.transient(app)
    body = ctk.CTkScrollableFrame(win, fg_color=BG_CARD)
    body.pack(fill="both", expand=True, padx=12, pady=12)
    last_date: str | None = None
    for code, label_text, ref_date in full:
        date_iso = ref_date.isoformat()
        if date_iso != last_date:
            ctk.CTkLabel(
                body, text=date_iso, text_color=ORANGE,
                font=(FONT_FAMILY, 12, "bold"), anchor="w",
            ).pack(fill="x", pady=(8, 2))
            last_date = date_iso
        ctk.CTkLabel(
            body, text=f"  {code}  ·  {label_text}",
            text_color=TEXT, font=(FONT_FAMILY, 12), anchor="w",
        ).pack(fill="x")
