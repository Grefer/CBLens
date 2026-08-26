"""⭐ 关注池子表 / 摘要条 / 事件横幅 — 从 batch.py 抽离.

设计原则:
- 公共 helper (染色 / 格式化 / 主题刷新) 集中在 :mod:`batch_common`, 两侧共用同一份模块级 ``_TREE_ATTRS`` 注册集。
- 与主表的双向 callback (关注池刷新后需要重画主表) 通过 *延迟导入* 处理, 避免 ``batch.py`` ↔ ``batch_watchlist.py`` 形成循环依赖。
"""
from __future__ import annotations

import importlib.util
import logging
import threading
import tkinter as tk
from datetime import date, datetime, timedelta
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk

from ..theme import *  # noqa: F401,F403  保持与 batch.py 一致的颜色 / 字体常量入口
from ...batch_pricing import (
    average_rating_label,
    build_batch_provider,
    cross_section_anchor_from,
    list_upcoming_tradable_from_cache,
    sort_batch_results_for_review,
)
from ...pricing_api import batch_price_from_provider_threaded
from ...cb_events import (
    event_actionability,
    event_end_label,
    event_short_label,
)
from ...watchlist import (
    add_to_watchlist,
    load_watchlist,
    remove_from_watchlist,
)
from ...watchlist_cache import (
    load_watchlist_pricing,
    save_watchlist_pricing,
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
from ...data_providers import wind_is_ready

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

# 窄同步失败时的逃生出口: 必须与 wind_sync._POOL_SYNC_TARGETS 中的条目一致
# (label 用于匹配确认文案)
_TERMS_SYNC_MODULE = "convertible_bond.cli.sync_tradable"
_TERMS_SYNC_LABEL = "🔄 增量更新基础信息 (推荐)"


logger = logging.getLogger(__name__)


def _source_ready_without_connecting(source: str) -> bool:
    """这个行情源现在能不能**不新建连接**就取到数.

    只给非用户发起的那一轮当闸。判据按源分:

    - **Wind**: 要求终端已连接 (``wind_is_ready``)。"装了 WindPy"不算 ——
      装了但终端没开时 ``w.start()`` 会等满 waitTime, 那正是"打开 GUI 就卡住"
      的成因; 实测本机就是这个状态 (可导入 / ``isconnected()`` 为 False)。
    - **akshare**: 纯 HTTP, 没有"连接"这回事, 失败也是秒级 —— 放行。
    - **CSV**: 要在主线程弹目录选择框, 启动时弹一个模态比不定价糟得多 —— 挡住。
    """
    key = (source or "").strip().lower()
    if key == "wind":
        return wind_is_ready()
    if key == "akshare":
        return True
    return False


def _set_watch_button_state(app, state: str) -> None:
    """改「⚡ 关注池重算」按钮状态; 按钮不在就静默跳过.

    原先是直接在 app 上取那个按钮属性再 configure, 而按钮建在
    ``tabs/batch.py`` 里、被这个模块跨文件消费。按钮一旦搬走或改名, 起线程前那次
    访问就抛 AttributeError, 而 finally 里那次在 Tk 回调中抛 (Tk 只打到 stderr),
    症状是"按钮再也不恢复"。用 getattr 兜住, 让控件归属的变化不再是个定时炸弹。
    """
    button = getattr(app, "btn_batch_refresh_watch", None)
    if button is None:
        return
    try:
        button.configure(state=state)
    except Exception:
        logger.debug("关注池重算按钮状态更新失败 (忽略)", exc_info=True)


def _cached_valuation_label(app) -> str:
    """内存里这批行是哪个估值日的 —— 给"今日取价失败"那条状态用."""
    for row in (app._batch_upcoming_results or []) + (app._batch_all_results or []):
        value = row.get("valuation_date")
        if value:
            return str(value)[:10]
    return "上一次"


def merge_watchlist_pricing(main_rows, upcoming_rows, fresh_rows):
    """把一轮关注池定价结果并回 (主池行, 主池外行), 返回两个新列表.

    规则不对称, 两半都是必须的:

    - **code 已存在时, 只有 ``status == "ok"`` 才覆盖。** 否则一次取数失败就把
      内存里昨天算好的那一行换成 nan 行 —— 而表上看不出区别, 只是"今天数字没了"。
    - **code 不存在时, 无条件 append 到 upcoming (失败行也进)。** 这一半容易被
      当成 bug 顺手"修掉": 加一句 ``if status != "ok": continue`` 会让一只**失败的
      在途新债**从表里彻底消失, 而新债不进主池、唯一的来路就是 upcoming —— 于是
      "这只债取价失败"和"我根本没关注它"变成同一种表现。
    """
    main_by_code = {r.get("bond_code"): i for i, r in enumerate(main_rows)}
    upcoming_by_code = {r.get("bond_code"): i for i, r in enumerate(upcoming_rows)}
    new_main = list(main_rows)
    new_upcoming = list(upcoming_rows)
    for row in fresh_rows:
        code = row.get("bond_code")
        if not code:
            continue
        is_ok = row.get("status") == "ok"
        if code in main_by_code:
            if is_ok:
                new_main[main_by_code[code]] = row
        elif code in upcoming_by_code:
            if is_ok:
                new_upcoming[upcoming_by_code[code]] = row
        else:
            new_upcoming.append(row)
    return new_main, new_upcoming


def _terms_sync_available() -> bool:
    """条款库全量同步只走 Wind (cb-sync-tradable 固定 --source wind); 没装就别提示."""
    try:
        from ...data_providers.wind import prepare_windpy_import_path
        prepare_windpy_import_path()
        return importlib.util.find_spec("WindPy") is not None
    except Exception:
        return False


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


def run_new_issue_sync_async(app, *, then, prompt_on_error: bool = False):
    """后台跑一次新债窄同步, 完成 (无论成败) 后在主线程回调 ``then(synced: bool)``.

    只碰"在盯新债"那几只 (实测每天 4 只上下), 一次 akshare 调用秒级完成, 不需要 Wind ——
    所以这里**不再问"要不要先同步"**: 原本那道闸读的是 ``bundle_meta()['updated_at']``,
    而任何一次写盘都会把它推到今天, 于是提示永不弹出; 即便弹出并点"是", 跑的
    ``cb-sync-tradable --incremental`` 又恰好按 7 天新鲜度跳过刚抓过的新债。
    详见 :mod:`convertible_bond.new_issue_sync`。
    """
    # 「扫新债」和「批量重算」共用这条路径, 两个按钮在同步的这两秒里都还是可点的 —
    # 不挡住就会并发写同一个 bundle, 并且把后续流程跑两遍
    if getattr(app, "_new_issue_sync_running", False):
        return
    app._new_issue_sync_running = True

    def worker():
        try:
            from ...new_issue_sync import sync_new_issues
            report = sync_new_issues(dry_run=False)
        except Exception as exc:
            app.after(0, lambda exc=exc: _after_new_issue_sync(app, None, exc, then, prompt_on_error))
            return
        app.after(0, lambda: _after_new_issue_sync(app, report, None, then, prompt_on_error))

    app.v_batch_status.set("正在刷新新债上市日 ...")
    threading.Thread(target=worker, daemon=True).start()


def _after_new_issue_sync(app, report, exc, then, prompt_on_error: bool):
    app._new_issue_sync_running = False
    if exc is not None:
        app.v_batch_status.set(f"⚠ 新债上市日刷新失败 ({exc}) — 按本地条款库继续")
        if prompt_on_error and _terms_sync_available() and messagebox.askyesno(
            "改用全量条款同步?",
            f"新债上市日刷新失败:\n{exc}\n\n"
            "「是」: 改跑 Wind 增量条款同步 (通常 1-3 分钟), 完成后继续扫描\n"
            "「否」: 直接扫描现有条款库",
        ):
            app._run_pool_sync(
                _TERMS_SYNC_MODULE, _TERMS_SYNC_LABEL, ("--incremental",),
                confirm=False,
                on_success=lambda: then(True),
            )
            return
        then(False)
        return

    changed = len(report.get("changes") or [])
    if changed:
        app.v_batch_status.set(f"已刷新 {changed} 项新债要素")
    then(bool(changed))


#: 「⚡ 关注池重算」按钮的文案。**只在这里写一次** —— 状态栏那句"点「…」再试"曾经
#: 把它硬编码了一遍, 于是按钮改名后消息还指着旧名字, 用户在页面上找不到那个按钮。
WATCH_REFRESH_LABEL = "⚡ 关注池重算"

#: 「陈旧即刷」的防抖窗口。启动、切页、扫新债都可能触发这一轮, 没有窗口的话
#: 用户在页签之间来回点就会不停起后台定价。
STALE_REFRESH_DEBOUNCE_SEC = 15 * 60

#: 需要重算的取价状态。``ok`` 之外**全部**要重来, 其中 ``no_market`` 那一档最容易
#: 被漏: 实测 118076.SH 先锋转债 ``status=="ok"``、``valuation_date`` 就是今天、
#: 唯独市价是 None —— 只看"是不是今天算的"会让它当天永远不再重试。
_STALE_PRICE_STATES = frozenset({"unpriced", "failed", "no_market", "stale"})


def stale_watchlist_codes(app, *, rows=None) -> list[str]:
    """关注池里今天需要重算的代码.

    判据直接架在 :func:`_watchlist_display_rows` 派生的 ``_price_state`` 上, 不另写
    一份 —— 两份判据迟早分叉, 而分叉的表现是"表上显示要刷新、刷新却不刷它"。

    唯一的例外是**还没上市的新债**: 它们的 ``no_market`` 是天然状态 (市场还不存在),
    不该每一轮都陪跑。已上市却缺市价的 (118076.SH) 仍然要重算。
    """
    rows = rows if rows is not None else _watchlist_display_rows(app)
    out: list[str] = []
    for row in rows:
        code = row.get("bond_code")
        if not code:
            continue
        state = row.get("_price_state")
        if state not in _STALE_PRICE_STATES:
            continue
        if state == "no_market" and _is_new_bond(row):
            continue          # 还没上市, 没有市价是正常的
        out.append(code)
    return out


def refresh_stale_watchlist(app, *, quiet: bool = True,
                            note: str | None = None) -> int:
    """给关注池里陈旧/缺价的标的补一轮定价; 返回本轮送出的只数.

    ``quiet=True`` 是**非用户发起**的那一轮 (启动 / 切进主页) 用的: 不碰全局进度条,
    失败只写状态栏, 且遇到"Wind 装了但终端没连"会被
    :func:`_source_ready_without_connecting` 直接挡掉 —— 见 AGENTS.md「GUI 启动路径上
    不许建立新的数据源连接」。
    """
    now = datetime.now()
    last = getattr(app, "_last_stale_refresh_at", None)
    if quiet and last is not None and (now - last).total_seconds() < STALE_REFRESH_DEBOUNCE_SEC:
        return 0
    pending = stale_watchlist_codes(app)
    if not pending:
        return 0
    if not _start_watchlist_pricing(
            app, pending, note=note or f"待刷新 {len(pending)} 只", quiet=quiet):
        return 0
    app._last_stale_refresh_at = now
    return len(pending)


def unpriced_new_bond_codes(app) -> list[str]:
    """关注池里"是新债、且还没有理论价"的代码.

    只管新债 —— 关注池里其他没定价的标的交给「⚡ 关注池重算」, 免得一个入口做两件事。
    """
    return [
        row.get("bond_code")
        for row in _watchlist_display_rows(app)
        if row.get("bond_code") and row.get("status") != "ok" and _is_new_bond(row)
    ]


def price_unpriced_new_bonds(app, *, note: str | None = None, quiet: bool = False) -> int:
    """给关注池里还没有理论价的新债补一轮定价; 返回本轮送出的只数 (没有则 0).

    新债不进主池 (剔除原因「已发行未上市」), 理论价**只能**来自关注池额外定价
    (``_batch_upcoming_results``)。而这一格一旦没跑到就再没有自愈路径: 启动时
    ``_load_result_cache`` 只把缓存里的空列表读回来, 行就一直空着。所以缓存加载与扫新债
    两条路都要补这一枪 —— 否则"关注池里的新债没有理论价"会同时对应两种成因
    (**没算过** 与 **算了但没落到展示层**), 症状却完全一样。
    """
    pending = unpriced_new_bond_codes(app)
    if not pending:
        return 0
    if not _start_watchlist_pricing(
            app, pending, note=note or f"新债 {len(pending)} 只", quiet=quiet):
        return 0
    return len(pending)


def _refresh_watchlist_with_upcoming(app):
    """'扫新债' 按钮: 窄同步新债上市日 → 扫描新债 → 加入关注池 → 立刻定价.

    只扫本地 cb_data 的话, 新债得等用户想起来手动同步基础信息才看得见; 这里把
    "同步 → 扫描 → 定价"串成一步, 让按钮自己就能拿到当天新发的债和它的理论价。
    """
    run_new_issue_sync_async(
        app,
        then=lambda synced: _scan_upcoming_and_price(app, reload_terms=True),
        prompt_on_error=True,
    )


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
    refresh_home(app)

    # 新债刚加进来时只有条款元数据, 表里一排空白; 顺手把还没有理论价的新债算出来,
    # 否则"扫新债"给出的只是一张代码清单, 没法判断贵贱。
    scanned = app.v_batch_status.get()
    started = price_unpriced_new_bonds(app)
    if started:
        app.v_batch_status.set(f"{scanned} · 正在定价 {started} 只新债 ...")


# ── ⚡ 关注池快速重定价 ─────────────────────────────────────────
def _refresh_watchlist_pricing(app):
    """⚡ 仅对关注池的代码执行定价 — 跳过全市场, 秒级返回."""
    codes = [e.get("bond_code") for e in (app._batch_watchlist or []) if e.get("bond_code")]
    if not codes:
        messagebox.showinfo("提示", "关注池为空 — 在主表中右键加入或点 🆕 扫新债")
        return
    _start_watchlist_pricing(app, codes)


def _start_watchlist_pricing(app, codes, *, note: str | None = None,
                             quiet: bool = False) -> bool:
    """对给定代码起一轮关注池定价; 参数不合法或已在跑时返回 False.

    ``quiet=True`` 时失败只写状态栏不弹窗 —— 给非用户发起的那一轮 (缓存加载后的自动补价)
    用: 启动就糊一个模态错误框, 比"新债那几行暂时没价"糟得多。
    """
    codes = [c for c in dict.fromkeys(codes) if c]
    if not codes:
        return False

    # 单飞检查必须最先 —— 它最便宜, 而且"已经在跑"这件事与源可用与否无关。
    # 现在有三个入口能起这一轮 (⚡ 关注池重算 / 扫新债 / 缓存加载后的自动补价), 并发跑会
    # 让两个 worker 各自基于同一份旧列表算出 new_upcoming 再互相覆盖。
    if getattr(app, "_watchlist_pricing_running", False):
        return False

    source = app.v_batch_source.get()
    if quiet and not _source_ready_without_connecting(source):
        # 非用户发起的那一轮 (启动自愈) **绝不允许**去建立一条新连接。
        # WindPy 装了但终端没开时 w.start() 会等满 waitTime, 于是"打开 GUI"
        # 变成"打开后转圈几十秒" —— 而这一轮本来就只是锦上添花: 用户没要求它,
        # 表里该有的盘上数据 (watchlist_cache) 已经画出来了。
        app.v_batch_status.set(
            f"ℹ {len(codes)} 只待定价 — {source} 当前不可用, 点「{WATCH_REFRESH_LABEL}」再试")
        return False

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
            # 反解隐含下修强度 + 四角点扰动, 产出稳健下修优势 (策略页的默认排序信号)。
            # 此前批量页跑完整 PDE 网格却把这族信号整批丢掉 (实测缓存里 0/280 有值)。
            # 实测边际成本: 诊断走粗网格 (150,400) = 细网格的 0.16x, 每债 +166ms,
            # 全池 280 只按 10 线程折算 3.1s → 7.7s —— 相对分钟级的取数可忽略。
            compute_pde_signals=True,
        )
    except ValueError as exc:
        messagebox.showerror("参数错误", str(exc))
        return False

    label = note or f"关注池 {len(codes)} 只"
    app.v_batch_status.set(f"⚡ 正在定价{label} ...")
    if not quiet:
        # quiet 那一轮 (启动自愈) 刻意不碰这两样共用件: _start_progress 没有引用计数,
        # 且 _tick_progress 写的是**全局** v_status —— 一轮后台自愈会把定价页/回测页
        # 正在跑的任务的状态文字顶掉, 而 finally 里的 _stop_progress 又不还原文案。
        _set_watch_button_state(app, "disabled")
        app._start_progress(f"定价{label}")

    # 置位必须紧挨 start(): 它原先在 btn.configure 之前, 而那次裸属性访问一旦抛
    # (按钮被搬走/改名), finally 就永远不执行 —— 三个入口全被单飞检查静默挡死,
    # 且检查只 return False、不写状态、不排队, 症状是"点了没反应"。
    app._watchlist_pricing_running = True
    try:
        threading.Thread(
            target=_watchlist_pricing_worker,
            args=(app, codes, source, csv_root, params),
            kwargs={"quiet": quiet},
            daemon=True,
        ).start()
    except Exception:
        app._watchlist_pricing_running = False
        if not quiet:
            _set_watch_button_state(app, "normal")
            app._stop_progress()
        raise
    return True


def _watchlist_pricing_worker(app, codes, source, csv_root, params, *, quiet: bool = False):
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
        # 主池外的那几只必须锚主池中位并跳过秩 —— 延迟导入与 _render_batch_views
        # 同因 (batch ↔ batch_watchlist 会成环)
        from .batch import _annotate_off_pool
        results = _annotate_off_pool(results, app._batch_all_results or [])

        ok_rows = [r for r in results if r.get("status") == "ok"]
        if not ok_rows:
            # 全失败守卫 (照主表 worker 的既有做法): 不覆盖内存、不写盘。
            # 原先无条件报「⚡ 已刷新关注池 N 只」并用一批 nan 行盖掉内存里已有的好行 ——
            # 一次网络抖动就把昨天算好的价抹平, 而状态栏说的是成功。
            stale_date = _cached_valuation_label(app)
            app.after(0, lambda: app.v_batch_status.set(
                f"⚠ 今日取价失败 ({provider.name}) — 表内仍是{stale_date}的价"))
            return

        val_date = market_today()
        anchor = cross_section_anchor_from(app._batch_all_results or [])
        new_main, new_upcoming = merge_watchlist_pricing(
            app._batch_all_results or [], app._batch_upcoming_results or [], results)

        try:
            save_watchlist_pricing(
                ok_rows,
                valuation_date=val_date,
                source=provider.name,
                params=params,
                cross_section=({"market_median_deviation": anchor,
                                "from": "batch_pricing_cache.rows",
                                "from_valuation_date": val_date.isoformat(),
                                "n": len(app._batch_all_results or [])}
                               if anchor is not None else None),
                origin="watchlist_worker",
            )
        except Exception:
            # 落盘只是让下次开页有数, 失败不该让这一轮的结果丢掉
            logger.warning("关注池行情落盘失败 (本轮结果仍在内存里)", exc_info=True)

        app._batch_all_results = sort_batch_results_for_review(new_main)
        app._batch_upcoming_results = new_upcoming
        app._watchlist_price_cache = load_watchlist_pricing()
        # 两页都要刷: 主表读 _batch_all_results, 主页读三级取价表, 各有各的入口。
        # 只刷一边的表现是"算完了但表还是旧值", 正是 AGENTS 记的那个陷阱。
        app.after(0, lambda: _render_batch_views(app, refresh_home_table=False))
        app.after(0, lambda: refresh_home(app))
        n_main = sum(1 for c in codes if c in {r.get("bond_code") for r in (app._batch_all_results or [])})
        n_failed = len(results) - len(ok_rows)
        msg = f"⚡ 已刷新关注池 {len(ok_rows)}/{len(codes)} 只"
        if n_failed:
            msg += f" (失败 {n_failed})"
        app.after(0, lambda: app.v_batch_status.set(msg))
    except Exception as exc:
        app.after(0, lambda exc=exc: app.v_batch_status.set(f"❌ 关注池定价失败: {exc}"))
        if not quiet:
            app.after(0, lambda exc=exc: messagebox.showerror("关注池定价失败", str(exc)))
    finally:
        app._watchlist_pricing_running = False
        if not quiet:
            app.after(0, app._stop_progress)
            app.after(0, lambda: _set_watch_button_state(app, "normal"))


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
    refresh_home(app)
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
    refresh_home(app)
    app.v_batch_status.set(f"已从关注池移除 {len(codes)} 只")


#: 从定价结果行合并进关注池展示行的字段。**新增列的数据来源都要先登记在这里** ——
#: 漏掉不会报错, 只是那一列恒空 (这一处没有守护测试能替你发现)。
_PRICED_MERGE_FIELDS = (
    # 身份与条款
    "bond_name", "stock_code", "underlying_name", "K", "credit_rating",
    "maturity_date", "listing_date", "tradable_date",
    "is_tradable", "trading_status", "outstanding_balance",
    # 定价主结果
    "status", "theoretical_price", "deviation", "parity", "conversion_premium",
    # 研究信号
    "opportunity_score", "quality_score", "double_low", "confidence",
    "sensitivity_status", "risk_tags", "review_bucket", "review_notes",
    "event_flags", "down_reset_trigger_gap", "down_reset_robust_edge_value",
    # 横截面 (小批量标注时为 None, 展示层打「—」而不是打一个假名次)
    "relative_deviation", "cheapness_rank", "cheapness_percentile",
    "cheapness_rank_total",
    # 溯源
    "valuation_date", "priced_at", "origin",
    "market_price_as_of", "market_price_source",
)

#: ``market_price`` 单拎出来: 它**同时**存在于 watchlist.json 的 metadata 白名单
#: (无 as-of 戳, 可能是几天前扫新债时写的) 与定价结果行里。放进上面那个元组会走
#: "value is not None 才覆盖"的规则 —— 于是定价行明明算出"今天没有市价"(None) 时,
#: entry 里那个陈旧值会静默胜出, 表上看不出任何区别。
_PRICE_FIELD = "market_price"


_EMPTY_PRICE_CACHE: dict = {"meta": {}, "rows": {}}


def load_price_cache_into(app) -> dict:
    """读关注池行情热缓存并挂到 app 上; 由**启动路径**显式调用.

    读盘刻意留在这里而不是让 ``_price_cache`` 惰性去读 —— 展示层一旦会隐式碰
    真实磁盘, 用例就变成"过不过取决于你上次开 GUI 点没点刷新"。这正是
    ``sync_cb_events`` 那批用例踩过的坑 (真实 code + 真实 cb_data → 一次纯数据
    提交就让套件转红), 不要在新代码里重演。

    挂在**独立属性**上而不是塞进 ``app._batch_watchlist`` 的元素里:
    ``_auto_add_upcoming_to_watchlist`` 会用磁盘值整体重置那个列表 (三处调用),
    任何挂在其元素上的内存态增强都会在下一次扫新债时无声蒸发。
    """
    try:
        cache = load_watchlist_pricing()
    except Exception:
        logger.debug("关注池热缓存读取失败, 按空处理", exc_info=True)
        cache = dict(_EMPTY_PRICE_CACHE)
    app._watchlist_price_cache = cache
    return cache


def _price_cache(app) -> dict:
    """已挂在 app 上的热缓存; 没有就当空 —— **不读盘**, 理由见 load_price_cache_into."""
    return getattr(app, "_watchlist_price_cache", None) or _EMPTY_PRICE_CACHE


def _priced_rows_by_code(app) -> dict[str, dict]:
    """三级兜底的取价表, 优先级由低到高.

    1. **磁盘热缓存** —— 开页立刻有数的那一层。没有它, 关注池的理论价就完全寄生在
       "你这次开机有没有跑过全市场"上 (实测缓存 ``n_upcoming_results=0`` 时, 三只
       在途新债连着几天没有理论价)。
    2. ``_batch_upcoming_results`` —— 本轮算出来的主池外结果。
    3. ``_batch_all_results`` —— **全池**, 不是 ``_batch_results`` (那是视图子集,
       关注的债多半不在「低估候选」这类窄视图里, 读错会让整行随视图开关忽有忽无)。

    内存永远压过磁盘: 磁盘是"上次算的", 内存是"这次算的"。
    """
    by_code: dict[str, dict] = {}
    for code, row in (_price_cache(app).get("rows") or {}).items():
        if code:
            by_code[str(code)] = row
    for source in (getattr(app, "_batch_upcoming_results", None) or [],
                   getattr(app, "_batch_all_results", None) or []):
        for row in source:
            code = row.get("bond_code")
            if code:
                by_code[str(code)] = row
    return by_code


def _derive_price_state(merged: dict, priced: dict | None, today) -> str:
    """这一行的取价状态, 六选一.

    今天三种「—」在表上长得一模一样, 而成因完全不同 (实测同一份关注池里同时存在):
    ``unpriced`` 是没算过, ``no_market`` 是算了但数据源没给市价 (118076.SH 先锋转债),
    ``failed`` 是算了但失败。分不开就没法判断"要不要点刷新"。
    """
    if priced is None:
        return "unpriced"
    if str(priced.get("status") or "") != "ok":
        return "failed"
    if not _is_finite(merged.get(_PRICE_FIELD)):
        return "no_market"
    value = priced.get("valuation_date")
    try:
        if value is None or _parse_watchlist_date(value) != today:
            return "stale"
    except (TypeError, ValueError):
        return "stale"
    return "ok"


def _watchlist_display_rows(app, *, today=None):
    """合并关注池意图层 + 三级取价, 生成关注池表展示行.

    ``entry`` (来自 watchlist.json) 是**基座**, 定价行覆盖在上面。基座这个位置很关键:
    所有上层都缺值时它一定胜出 —— 所以 ``market_price`` 这种"两边都有、但一边没有
    as-of 戳"的字段必须显式处理, 见 ``_PRICE_FIELD``。
    """
    today = today or market_today()
    by_code = _priced_rows_by_code(app)
    rows = []
    for entry in app._batch_watchlist:
        code = entry.get("bond_code")
        merged = dict(entry)
        priced = by_code.get(code)
        if priced:
            for key in _PRICED_MERGE_FIELDS:
                value = priced.get(key)
                if value is not None:
                    merged[key] = value
            # 有定价行时市价**一律**以它为准 (包括算出来是 None 的情况) ——
            # 否则 entry 里那个无戳的旧价会在"今天没市价"时静默顶上来。
            merged[_PRICE_FIELD] = priced.get(_PRICE_FIELD)
            if merged.get("market_price_source") is None and _is_finite(merged[_PRICE_FIELD]):
                merged["market_price_source"] = "unstamped"
        merged["_price_state"] = _derive_price_state(merged, priced, today)
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
            # 一律用 _is_finite 而不是 `is not None`: 落盘的 None 读回来是 **NaN**
            # (与内存路径一致, 见 watchlist_cache._NAN_FIELDS), 而 NaN is not None 为真 ——
            # 于是"今天没有市价"会被渲染成字面的 "nan"。实测三只未上市新债全中。
            f"{float(entry['theoretical_price']):.2f}" if is_ok and _is_finite(entry.get("theoretical_price")) else "—",
            f"{float(entry['market_price']):.2f}" if _is_finite(entry.get("market_price")) else "—",
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


def refresh_home(app) -> None:
    """主页数据变了就调这一下: 表 + 摘要 + 事件横幅.

    横幅的刷新**从 ``_render_watchlist_table`` 末尾提到这里**。原先它寄生在表渲染
    里且是全仓库唯一调用点 —— 于是任何"少画一次表"的优化都会顺手把横幅一起停掉,
    而横幅失败是静默的 (拿不到 label/var 直接 return)。它的扫描集本来也不只是关注池
    (``_banner_scan_codes`` = 关注池 ∪ 主池全量), 挂在关注池表的渲染上本就不合理。
    """
    _render_watchlist_table(app)
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

    _ANOMALY_TAGS = {"模型高估离群", "深度低估待核", "偏差异常"}   # 含 legacy 名
    anomaly_count = sum(1 for r in priced
                        if _ANOMALY_TAGS & set(r.get("risk_tags") or []))

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


def _banner_scan_codes(app) -> set[str]:
    """横幅要扫的代码集 = **主池全量** + 关注池。

    此前只扫关注池, 于是主池里昨天出的下修提议不会浮出来 —— 除非你已经在关注它。
    而"已经在关注"恰恰意味着你已经知道了; 横幅真正的用处是告诉你**还不知道的那些**。
    """
    codes = {e.get("bond_code") for e in (app._batch_watchlist or []) if e.get("bond_code")}
    for row in (getattr(app, "_batch_all_results", None) or []):
        code = row.get("bond_code")
        if code:
            codes.add(code)
    return codes


def _window_hit(ev, today: date, horizon: date) -> tuple[date, bool] | None:
    """事件是哪个日期落进了窗口, 以及那是不是**结束**日; 都没落进返回 None。

    此前入窗判定看 (event_date, effective_start, effective_end) 里**任意一个**,
    显示的却固定是 ``effective_start or event_date`` —— 于是一条 effective_end 在窗口内
    的区间事件, 会把几个月前的起始日当成"未来 30 天的事"显示出来 (实测扫全主池后
    冒出「暂停转股 03-08」这种明显自相矛盾的行)。这里改成: 谁把它带进窗口就显示谁。

    结束日只对 ``event_end_label`` 认可的类型生效 —— 其余类型的 effective_end 要么
    覆盖率≈0, 要么被公告正文里的回售期区间污染 (详见 cb_events.EVENT_END_LABEL)。
    """
    for day in (ev.event_date, ev.effective_start):
        if day is not None and today <= day <= horizon:
            return day, False
    end = ev.effective_end
    if (end is not None and today <= end <= horizon
            and event_end_label(ev.event_type or "") is not None):
        return end, True
    return None


def collect_upcoming_events(store, codes, today, horizon):
    """扫这些代码在 [today, horizon] 内的事件, 去重并按可操作性排序。

    返回 ``(bond_code, 标签, 日期)`` 三元组列表 —— 与横幅/弹窗的既有契约一致。
    纯函数, 不碰 GUI, 便于单测。
    """
    collected: list[tuple[int, date, str, str]] = []
    seen: set[tuple[str, str, date]] = set()
    for code in codes:
        try:
            events = store.list_events(bond_code=code)
        except Exception:
            continue
        for ev in events:
            hit = _window_hit(ev, today, horizon)
            if hit is None:
                continue
            ref_date, is_end = hit
            event_type = ev.event_type or ""
            # 区间事件落进窗口的往往是**结束日**, 那与"事件发生"是两回事:
            # 「不强赎」是上限被解除, 「不强赎到期」是上限恢复 —— 含义相反, 标签必须分开。
            text = event_short_label(event_type)
            if is_end:
                text += event_end_label(event_type) or "结束"
            key = (code, text, ref_date)
            # 同一件事常有"第一次/第二次/第N次提示性公告"多条 (实测鸿路转债 33 条
            # putback), 逐条铺出来会把横幅刷满。按 (代码, 标签, 日期) 去重。
            if key in seen:
                continue
            seen.add(key)
            # 排序键在这里定, **不要**事后从标签字符串反查类型 —— 那是把展示词当主键。
            collected.append(
                (event_actionability(event_type, is_end=is_end), ref_date, code, text))
    # 先可操作性再日期: 纯按日期排会让"评级调整"挤掉三天后的强赎。
    collected.sort()
    return [(code, text, day) for _rank, day, code, text in collected]


def _refresh_events_banner(app, *, window_days: int = 30, head: int = 5):
    """扫描主池+关注池在未来 window_days 天的事件, 按可操作性拼成横幅; 无事件时隐藏."""
    label = getattr(app, "lbl_batch_events_banner", None)
    var = getattr(app, "v_batch_events_banner", None)
    if label is None or var is None:
        return

    store = getattr(app, "event_store", None)
    if store is None:
        label.grid_remove()
        return

    today = market_today()
    upcoming = collect_upcoming_events(
        store, _banner_scan_codes(app), today, today + timedelta(days=window_days))

    if not upcoming:
        var.set("")
        app._batch_events_banner_full = []
        label.grid_remove()
        return

    names = {row.get("bond_code"): row.get("bond_name")
             for row in (getattr(app, "_batch_all_results", None) or [])}
    app._batch_events_banner_full = list(upcoming)      # 明细留给弹窗, 一条不少
    groups = _group_banner_entries(upcoming, names)
    parts = groups[:head]
    suffix = f"  ·  ...展开 {len(upcoming)} 件" if len(groups) > head else ""
    # 文案要跟 _banner_scan_codes 的实际扫描集对上 (关注池 ∪ 主池全量), 尤其是这块
    # 现在挂在「⭐ 我的关注池」主页上 —— 写"主池"会让人以为它漏了自己关注的债。
    var.set(f"⚠ 近 {window_days} 天事件 {len(upcoming)} 件 (关注池+主池, 单击查看全部): "
            + "  ·  ".join(parts) + suffix)
    label.grid()


def _group_banner_entries(upcoming, names) -> list[str]:
    """横幅是**摘要行**, 同类事件折叠成 "标签 xN (最早 MM-DD)"。

    扫全主池后同类事件很容易成片 (实测 22 件里 11 件是「不下修到期」), 逐条铺开会把
    5 个展示位全占满, 把当天唯一一条「强赎截止」挤下去 —— 而那才是错过就没得选的。
    折叠只影响横幅这一行; ``_batch_events_banner_full`` 仍是逐条明细, 弹窗照旧全展开。
    """
    order: list[str] = []
    grouped: dict[str, list[tuple[str, str, date]]] = {}
    for entry in upcoming:                              # upcoming 已按可操作性排好序
        text = entry[1]
        if text not in grouped:
            grouped[text] = []
            order.append(text)
        grouped[text].append(entry)
    parts: list[str] = []
    for text in order:
        items = grouped[text]
        first_code, _text, first_date = items[0]
        if len(items) == 1:
            parts.append(f"{names.get(first_code) or first_code} {text} ({first_date.strftime('%m-%d')})")
        else:
            parts.append(f"{text} x{len(items)} (最早 {first_date.strftime('%m-%d')})")
    return parts


def _show_events_banner_full(app):
    """单击事件横幅 → 弹窗按日期分组展示全部事件."""
    full = getattr(app, "_batch_events_banner_full", None)
    if not full:
        return
    win = ctk.CTkToplevel(app)
    win.title(f"近 30 天事件 ({len(full)} 件, 关注池+主池)")
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
