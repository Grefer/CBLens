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
    DEEP_UNDERVALUED_TAGS,
    risk_tag_label,
    LEGACY_DEVIATION_OUTLIER_TAGS,
    MODEL_OVERVALUED_TAGS,
    average_rating_label,
    batch_pricing_exclusion_reason,
    build_batch_provider,
    cross_section_anchor_as_of,
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
    undo_remove,
)
from ...watchlist_cache import (
    anchor_age_is_stale,
    anchor_is_stale,
    load_watchlist_pricing,
    save_watchlist_pricing,
)
from .batch_common import (
    _TREE_ATTRS,
    pad_cells,
    trigger_gap_text,
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

#: 列宽拉伸权重。**改 headers 必须同步改这里** —— 它按表头文本索引, 删掉的键会变成
#: 死条目、新增的列查不到会走 batch_common 的默认 1.0 (与"名称"同级), 于是窗口一拉宽
#: 就把富余宽度均摊给窄数字列。不报错、不红测试, 只是越拉越难看。
#: 有守护测试比对两边的键集。
_WATCHLIST_COL_STRETCH_WEIGHTS = {
    "代码": 0.5,
    "名称": 1.0,
    "正股": 0.7,
    # 定长日期 (或「待定」) 的两列权重一致。「加入日」此前是 1.2 —— 15 列里第 3 高,
    # 仅次于「标签」2.0 与「事件」1.6, 而它装的是 10 个字符的定长日期: 窗口一拉宽,
    # 富余宽度优先喂给了一个永远不会变长的格子。
    "上市日": 0.5,
    "事件": 1.6,
    "市价": 0.35,
    "理论价": 0.35,
    "偏差(%)": 0.35,
    "相对偏差(pp)": 0.4,
    "双低": 0.3,
    "正股/下修线": 0.4,
    "评级": 0.25,
    "剩余(年)": 0.3,
    "标签": 2.0,
    "数据状态": 0.6,
    "加入日": 0.5,
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


def _auto_add_upcoming_to_watchlist(app, *, silent=False, source: str = "auto"):
    """自动发现尚未开始交易的新债 (含已发行未上市) 并加入关注池.

    *source* 直接透给 :func:`~convertible_bond.watchlist.add_to_watchlist`:

    - 默认 ``"auto"`` —— 后台三处调用 (首屏 / 缓存加载 / 批量重算前) 用它,
      **不许**把用户手删过的债加回来。
    - ``"manual"`` —— 只有「🆕 扫新债」按钮传, 它是用户显式点的,
      语义就是"再帮我扫一遍", 所以顺手解除这些代码的手删标记。

    默认取 ``"auto"`` 而不是 ``"manual"``: 这个函数的名字里就写着 auto, 三个后台
    调用点也确实是自动的; 万一将来新增一个调用点忘了传, 落到"守手删"这一档是
    保守的那个方向 —— 反过来会让手删静默失效, 而那正是本参数要修的问题。
    """
    upcoming = list_upcoming_tradable_from_cache(
        getattr(app, "terms_cache", None),
        window_days=_UPCOMING_SCAN_WINDOW_DAYS,
    )
    if upcoming:
        new_items = [dict(r) for r in upcoming]
        app._batch_watchlist, added = add_to_watchlist(new_items, source=source)
        if not silent:
            if added:
                app.v_watchlist_status.set(f"已自动添加 {added} 只新债到关注池")
            else:
                app.v_watchlist_status.set("关注池已包含所有已发行/即将上市的新债, 无新增")
    else:
        app._batch_watchlist = load_watchlist()
        if not silent:
            app.v_watchlist_status.set("暂无已发行未上市或即将上市的新债")


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

    app.v_watchlist_status.set("正在刷新新债上市日 ...")
    threading.Thread(target=worker, daemon=True).start()


def _after_new_issue_sync(app, report, exc, then, prompt_on_error: bool):
    app._new_issue_sync_running = False
    if exc is not None:
        app.v_watchlist_status.set(f"⚠ 新债上市日刷新失败 ({exc}) — 按本地条款库继续")
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
        app.v_watchlist_status.set(f"已刷新 {changed} 项新债要素")
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
                app.v_watchlist_status.set(f"⚠ 条款库重载失败: {exc}")

    # source="manual": 「🆕 扫新债」是用户显式点的, 语义是"再帮我扫一遍" ——
    # 之前手删掉的在途新债应当能这样加回来, 否则手删就成了没有出口的单程票。
    _auto_add_upcoming_to_watchlist(app, silent=False, source="manual")
    refresh_home(app)

    # 新债刚加进来时只有条款元数据, 表里一排空白; 顺手把还没有理论价的新债算出来,
    # 否则"扫新债"给出的只是一张代码清单, 没法判断贵贱。
    scanned = app.v_watchlist_status.get()
    started = price_unpriced_new_bonds(app)
    if started:
        app.v_watchlist_status.set(f"{scanned} · 正在定价 {started} 只新债 ...")


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
        app.v_watchlist_status.set(
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
        )
    except ValueError as exc:
        messagebox.showerror("参数错误", str(exc))
        return False

    label = note or f"关注池 {len(codes)} 只"
    app.v_watchlist_status.set(f"⚡ 正在定价{label} ...")
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


def market_price_coverage(ok_rows):
    """把 ``ok_rows`` 拆成 (应当有市价的行, 其中真取到市价的行)。

    ``status == "ok"`` **只说明模型算完了**, 与"数据源今天给没给行情"是两回事:
    S0/σ 走**正股**链路 (akshare 里是东财 ``stock_zh_a_hist`` → 新浪
    ``stock_zh_a_daily``), 市价走**转债**链路 (新浪 ``bond_zh_hs_cov_daily``) ——
    后者整条挂掉时前者照样出理论价。``_batch_result_from_provider`` 对
    ``market_price`` 只是 ``if mkt is not None`` 分个岔, 缺了就把 deviation 写 nan,
    ``status`` 照样是 ``"ok"``。于是"一个市价都没取到"会一路走到
    「⚡ 已刷新关注池 N/N 只」, 而表里价格那一片全是「—」(实测 akshare 东财侧连不上时
    正是这个形状)。

    **未上市新债不进分母**: 它们没有市价是天然状态而不是取数失败 —— 实测这份关注池
    5 只里有 3 只在途新债, 拿 5 做分母会让"一切正常"永远报成 2/5。

    判空用 :func:`_is_finite` 而不是 ``is not None``: 市价在
    ``watchlist_cache._NAN_FIELDS`` 里, 落盘走一圈读回来是 **NaN**, 而
    ``NaN is not None`` 为真。
    """
    expect = [r for r in ok_rows if not _is_new_bond(r)]
    got = [r for r in expect if _is_finite(r.get("market_price"))]
    return expect, got


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
            app.after(0, lambda: app.v_watchlist_status.set(
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
            app.after(0, lambda: app.v_watchlist_status.set(
                f"⚠ 今日取价失败 ({provider.name}) — 表内仍是{stale_date}的价"))
            return

        expect_price, with_price = market_price_coverage(ok_rows)
        if expect_price and not with_price:
            # 在市的债一只都没拿到行情 = 取价失败, 与上面那道守卫是同一件事, 只是
            # 失败发生在链路更深处。同样不写盘: 热缓存是**整行 upsert**
            # (`save_watchlist_pricing` 里 `merged.update(fresh)`), 写进去就把昨天
            # 那个真实市价换成 NaN, 实测 163.19 → nan。
            stale_date = _cached_valuation_label(app)
            app.after(0, lambda: app.v_watchlist_status.set(
                f"⚠ {provider.name} 未取到市价 ({len(expect_price)} 只在市债全空) "
                f"— 表内仍是{stale_date}的价"))
            return

        val_date = market_today()
        pool_rows = app._batch_all_results or []
        anchor = cross_section_anchor_from(pool_rows)
        # 锚来自**主池缓存的行**, 那可能是几天前跑的 —— 实测热缓存记 2026-08-28
        # 而锚源行是 08-26。此前这里写的是 val_date (=今天), 于是锚的年龄在盘上
        # 恒为 0, anchor_is_stale 接上了也判不出陈旧, 恰好在"天天点 ⚡ 但久不跑
        # 全量重算"这个常态用法上失效。
        anchor_as_of = cross_section_anchor_as_of(pool_rows)
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
                                "from_valuation_date": (anchor_as_of or val_date).isoformat(),
                                "n": len(pool_rows)}
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
        n_failed = len(results) - len(ok_rows)
        # 报数要把"算完了"与"取到市价了"分开说 —— 只报前者时, 行情整条挂掉的那一轮
        # 与一切正常的那一轮在状态栏上一模一样。
        msg = f"⚡ 已刷新关注池 {len(ok_rows)}/{len(codes)} 只"
        if n_failed:
            msg += f" (定价失败 {n_failed})"
        if expect_price:
            msg += f" · 取到市价 {len(with_price)}/{len(expect_price)}"
        n_pending = len(ok_rows) - len(expect_price)
        if n_pending:
            msg += f" · {n_pending} 只未上市无市价"
        app.after(0, lambda: app.v_watchlist_status.set(msg))
    except Exception as exc:
        app.after(0, lambda exc=exc: app.v_watchlist_status.set(f"❌ 关注池定价失败: {exc}"))
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


#: 确认框里最多点名几只; 再多就只报数 —— 一个能滚动的 messagebox 反而看不清。
_REMOVE_CONFIRM_HEAD = 8


def _removal_prompt(app, codes) -> str:
    """确认框正文 —— **只问要不要删, 不讲怎么加回来**.

    对话框是拦一次误触的, 不是放帮助文档的地方: 恢复路径由状态栏那句
    「(右键可撤销)」承担, 塞进这里只会让人为了点「是」先读三行字。
    (顺带避开一个渲染问题: 文案里的 🆕 / ⭐ 在 Tk 的 messagebox 里会被画成
    一枚突兀的彩色位图, 而按钮上的同一个 emoji 是另一套渲染。)
    """
    names = {e.get("bond_code"): e.get("bond_name")
             for e in (app._batch_watchlist or [])}

    def label(code):
        name = names.get(code)
        return f"{name} ({code})" if name else str(code)

    if len(codes) == 1:
        return f"确定从关注池移除 {label(codes[0])}?"
    lines = [f"  · {label(c)}" for c in codes[:_REMOVE_CONFIRM_HEAD]]
    if len(codes) > _REMOVE_CONFIRM_HEAD:
        lines.append(f"  … 另 {len(codes) - _REMOVE_CONFIRM_HEAD} 只")
    return f"确定从关注池移除这 {len(codes)} 只?\n\n" + "\n".join(lines)


def _remove_selected_from_watchlist(app):
    """移除选中标的 —— **必须二次确认**.

    这个动作原本零拦截, 而 ``<Delete>`` 与 ``<BackSpace>`` 都直绑到它, macOS 的
    delete 键发的就是 BackSpace: 选中一行随手按一下, ``watchlist.json`` (gitignored)
    当场重写, ``added_at`` 与 ``snapshot_*`` 一并消失。
    """
    tree = getattr(app, "_batch_watchlist_tree", None)
    if tree is None:
        return
    selection = tree.selection()
    if not selection:
        return
    codes = [iid for iid in selection if iid]
    if not codes:
        return

    if not messagebox.askyesno(
            "从关注池移除", _removal_prompt(app, codes),
            default="no", icon="warning"):
        app.v_watchlist_status.set("已取消移除")
        return

    # 撤销用的是**移除前的整份列表**, 走 undo_remove → save_watchlist 原样写回;
    # 用 add_to_watchlist 回填会给条目重写 added_at, 把"我什么时候开始关注它"抹掉。
    app._watchlist_undo_snapshot = [dict(e) for e in (app._batch_watchlist or [])]
    app._batch_watchlist = remove_from_watchlist(codes)
    refresh_home(app)
    app.v_watchlist_status.set(f"已从关注池移除 {len(codes)} 只 (右键可撤销)")


def _undo_remove_from_watchlist(app):
    """撤销上一次移除; 只在本次会话内有效 (快照挂在 app 上, 不落盘)."""
    snapshot = getattr(app, "_watchlist_undo_snapshot", None)
    if not snapshot:
        app.v_watchlist_status.set("没有可撤销的移除")
        return
    app._batch_watchlist = undo_remove(snapshot)
    app._watchlist_undo_snapshot = None
    refresh_home(app)
    app.v_watchlist_status.set(f"已撤销移除, 关注池恢复到 {len(app._batch_watchlist)} 只")


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
    "quality_score", "double_low", "confidence",
    "sensitivity_status", "risk_tags", "review_bucket", "review_notes",
    "event_flags", "down_reset_trigger_gap",
    # 横截面 (小批量标注时为 None, 展示层打「—」而不是打一个假名次)
    "relative_deviation", "cheapness_rank", "cheapness_percentile",
    "cheapness_rank_total",
    # 锚值与**锚自己的估值日** —— 「相对偏差」的分母出处。少登记它们, 展示层就判
    # 不出该不该把那两列灰掉 (漏登记不报错, 只是那一列恒空)。
    "market_median_deviation", "market_median_deviation_as_of",
    "cross_section_origin",
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
    ``_auto_add_upcoming_to_watchlist`` 会用磁盘值整体重置那个列表 (四处调用),
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


#: 取价来源的优先级 (数值越大越优先), 用作**同一估值日**下的 tie-break。
#: 顺序即 ``_priced_rows_by_code`` 里三路来源的遍历顺序: 磁盘热缓存 < upcoming < 全池。
_SOURCE_RANK_CACHE, _SOURCE_RANK_UPCOMING, _SOURCE_RANK_POOL = 0, 1, 2


def _row_freshness(row: dict, source_rank: int) -> tuple[date, int]:
    """取价行的新鲜度排序键: 先市场口径的估值日, 同日再比**来源**.

    缺估值日的行排最旧 —— 那说明它从没被定价路径写过, 只是 watchlist.json 的
    条款元数据。

    **同日的 tie-break 必须是来源, 不能是 ``priced_at``**。那个戳只有磁盘热缓存行
    才有 (``watchlist_cache.to_cache_row`` 落盘时盖的); 定价结果行从头到尾没人写它
    (实测 ``batch_pricing_cache.json`` 284 行**一行都没有**, 而热缓存 6 行全有)。
    拿它当键, 缺失方恒为 ``""``、恒排最旧, 于是同日**磁盘永远赢内存** —— 与本函数
    要保证的语义正好相反, 而且比改动前 (内存无条件覆盖) 更差: 上午点过「⚡ 关注池
    重算」、下午跑一轮全量批量重算之后, 主页仍显示上午那一份价, 批量页却是下午的,
    两页对同一只债给出不同数字而「数据状态」列两边都写「✓ 今日」。

    内存在同日一定不比热缓存旧: ⚡ 的 worker 会把自己的结果 ``merge_watchlist_pricing``
    回 ``_batch_all_results`` 再落盘, 两边同源; 而全量重算只更新内存、不写热缓存。
    """
    return (_parse_watchlist_date(row.get("valuation_date")) or date.min, source_rank)


def _priced_rows_by_code(app) -> dict[str, dict]:
    """三路取价来源, **按新鲜度择优**, 同新时后来者胜.

    1. **磁盘热缓存** —— 开页立刻有数的那一层。没有它, 关注池的理论价就完全寄生在
       "你这次开机有没有跑过全市场"上 (实测缓存 ``n_upcoming_results=0`` 时, 三只
       在途新债连着几天没有理论价)。
    2. ``_batch_upcoming_results`` —— 主池外结果。
    3. ``_batch_all_results`` —— **全池**, 不是 ``_batch_results`` (那是视图子集,
       关注的债多半不在「低估候选」这类窄视图里, 读错会让整行随视图开关忽有忽无)。

    **这里曾按"后写的覆盖先写的"排优先级, 前提写的是"内存永远压过磁盘"** —— 那个
    前提是错的: 启动路径上的 ``_batch_all_results`` 本身就是 ``_load_result_cache``
    从 ``batch_pricing_cache.json`` 读回来的**另一份磁盘缓存**, 而且它排在
    ``home_tab.build`` 之后 (``app.after(80, ...)``)。实测两份盘的日期差两天:
    热缓存 08-28 / 全池 08-26。于是主页先用 08-28 画出「派克 160.35」, 80ms 后被
    08-26 的 155.72 顶掉, 涨跌符号当场反转; 先锋转债更狠 —— 177.259 被一行
    ``market_price=None`` 的全池行覆盖, 市价/涨跌/偏差三列一起退化成「—」。
    页面上没有任何线索说这两个数来自不同日期。

    同一天则仍让后来者 (内存) 胜出: 那是"这次算的" vs "上次算的", 原优先级正确。
    这条 tie-break 由**显式的来源序号**保证, 不要改回按行里的时间戳比 —— 理由见
    ``_row_freshness``。
    """
    by_code: dict[str, tuple[tuple[date, int], dict]] = {}

    def _offer(code, row: dict, source_rank: int) -> None:
        code = str(code)
        key = _row_freshness(row, source_rank)
        current = by_code.get(code)
        if current is None or key >= current[0]:
            by_code[code] = (key, row)

    for code, row in (_price_cache(app).get("rows") or {}).items():
        if code:
            _offer(code, row, _SOURCE_RANK_CACHE)
    for source, rank in ((getattr(app, "_batch_upcoming_results", None) or [],
                          _SOURCE_RANK_UPCOMING),
                         (getattr(app, "_batch_all_results", None) or [],
                          _SOURCE_RANK_POOL)):
        for row in source:
            code = row.get("bond_code")
            if code:
                _offer(code, row, rank)
    return {code: row for code, (_key, row) in by_code.items()}


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


#: 关注池表的列定义 —— **单一事实源**。表头文本、列宽、拉伸权重三者必须同步,
#: 而权重表是按表头**文本**索引的: 删列会留下死条目、加列查不到会走 batch_common 的
#: 默认 1.0 (与"名称"同级), 于是窗口一拉宽就把富余宽度均摊给窄数字列。不报错、
#: 不红测试, 只是越拉越难看。有守护测试比对三边的键集。
#:
#: 列序上有两处是刻意的:
#:
#: - **「上市日」排在左块 (跟在「正股」后)**, 不跟「加入日」凑成日期对。未上市的
#:   那几行右半边是整片「—」(市价 / 涨跌 / 偏差 / 相对偏差 / 双低 全空, 只有理论价
#:   有值), 信息只在左边; 把上市日放到右端等于让最需要它的那几行去横向滚动。
#: - **没有「敏感性」**。它的五个取值不在同一个轴上 (`较稳健` 115 / `一般` 20 /
#:   `波动率敏感` 97 / `条款/流动性敏感` 52 / `需复核` 1): 前两个哪个更好没有任何地方
#:   说明其序, 而「需复核」根本不是敏感性、是另一个轴的判断。批量页把它放在「完整」
#:   而不是「简洁」, 关注池是日常视图更不该让它常驻。要查去批量页完整预设。
#: - **没有「可交易日」「距交易」**。实测 ``tradable_date ≡ listing_date`` (主池
#:   284/284、关注池 10/10 完全相同), 两列是同一个数; 而「距交易」("+4") 是从同一个
#:   日期算的派生量, 还会随"你哪天开的 GUI"漂移、没法跟公告核对 —— 未来日期本身
#:   已经把"还有几天"说清了。
_WATCHLIST_COLUMNS: tuple[tuple[str, int], ...] = (
    # ① 这是哪只债。**「上市日」是这一页的例外, 留在左块** —— 未上市新债右半边整片
    #    是「—」(市价/偏差/双低全空), 而"还有几天挂牌"恰是它们仅有的可操作信息。
    ("代码", 100), ("名称", 90), ("正股", 90), ("上市日", 90),
    # ② 多少钱
    ("市价", 70), ("理论价", 70), ("偏差(%)", 70), ("相对偏差(pp)", 105),
    # ③ 便宜吗
    ("双低", 60),
    # ④ 现在有什么事
    ("事件", 150), ("正股/下修线", 100), ("标签", 170),
    # ⑤ 基础条款
    ("剩余(年)", 70), ("评级", 50),
    # ⑥ 溯源 —— 「数据」按用户决策放到这里, **不跟「市价」**。
    #    它不需要靠邻接承载对象 (批量页的「可信度」需要): 七档里有四档说的是**整行**
    #    (未定价 / 失败 / 未重算 MM-DD / 无市价), 只有三档专指市价的 as-of。
    #    与「加入日」同组也讲得通 —— 一个是"这行的数是哪天的", 一个是"我哪天开始盯的"。
    ("数据状态", 80), ("加入日", 90),
)

def watchlist_columns():
    """(表头, 列宽).

    没有动态表头了 —— 「涨跌%」列已删 (见 :func:`_render_watchlist_table`), 而它是
    唯一需要把基准日期写进表头的列。
    """
    headers = [name for name, _ in _WATCHLIST_COLUMNS]
    widths = [width for _, width in _WATCHLIST_COLUMNS]
    return headers, widths


#: 「数据状态」列的取值 —— 取代原来的「状态」列。今天几种「—」在表上长得一模一样,
#: 而成因完全不同 (实测同一份关注池里同时存在), 分不开就没法判断要不要点刷新。
#:
#: **主文案是日期, 不是"估值日是不是今天"**。上一版正常态写 `✓ 今日 · 价 08-28`
#: —— 14 个字符里主文案讲的是估值日 (那是 `market_today()` 给的, 用户不需要一列去
#: 确认), 真答案挤在后缀。现在主文案就是市价 as-of。
#:
#: 列名改过三版: 「状态」→「取价」→「数据状态」。「取价: 未定价」「取价: 未重算」
#: 都别扭 (那是**定价**没做, 不是取价失败); 而「数据」少了主语。这一列的语义本来就是
#: 双峰的 —— "这行是什么时候的" ∪ "为什么没有", 只有「数据状态」七档全读得通,
#: 也因此**不需要 tooltip**。与批量页的「定价状态」不冲突: 两个名字各自点了对象。
_PRICE_STATE_LABEL = {
    "no_market": "无市价",
    "unpriced": "未定价",
    "failed": "失败",
}

#: 条款库兜底价那一档: `market_price_source == "terms_close"` 没有 as-of, 可以任意旧
#: (日升转债库里的 `close=99.994` 是 2021 年撤销发行前的值, 那只债从未上市交易)。
#: 原文案是「无戳」—— 从代码里搬出来的黑话, 表上没人读得懂"戳"是时间戳。
_NO_STAMP_LABEL = "日期不明"


def _latest_market_as_of(rows) -> date | None:
    """本页所有行里最新的市价 as-of; 陈旧判据的基准.

    **不拿估值日当基准**。估值日走 ``market_today()`` 是自然日, 而 2026-08-29 是
    **星期六** —— 周五收盘价在周六就是最新价, 但 `as_of < 估值日` 对当天**每一行**
    都成立: 实测 9/9 有价的行全部被标成陈旧。周末两天加每个交易日收盘前的整段时间,
    这个提示恒亮, 把停牌/节假日那种**真**陈旧淹掉。

    改用"本页 as-of 的最大值"就不需要交易日历, 而且自己校准: 九行都拿到 08-28、
    只有你是 08-20, 那才叫旧。思路与 ``latest_daily_before``「由盘上有没有那天的文件
    定义, 不靠日历倒推」一致。

    代价要认: **整页一起旧时没有任何一行被标**。那一档归摘要条的「估值日 MM-DD」管,
    逐行与整页分工。
    """
    stamps = [d for d in (_parse_watchlist_date(r.get("market_price_as_of")) for r in rows)
              if d is not None]
    return max(stamps) if stamps else None


def _row_data_label(entry, *, terms_cache=None, admission_config=None,
                    latest_as_of=None) -> str:
    """这一行的数据状态 —— 七档.

    主文案是**市价 as-of 的日期**, 答不出来才用词说为什么答不出来::

        ✓ 08-28              价是本页最新的那天的
        市价旧 08-26         价比本页别的行旧 (停牌 / 节假日) —— 市场没给, 你无能为力
        日期不明             条款库兜底价, 没有 as-of, 可以任意旧
        未重算 08-26         整行是隔夜算的 —— 点「⚡ 关注池重算」就能修
        无市价               算出了理论价, 市价那条腿缺 (未上市新债的天然状态)
        未定价 · <原因>      从没算过, 附主池剔除原因
        失败 · <原因>        算了但报错

    「市价旧」与「未重算」刻意用两个词而不是一个「旧」: 前者是市场没给新价, 后者是
    你没重算。上一版写成「旧价」/「算于」, 差一个字, 太容易混。

    *latest_as_of* 由 :func:`_latest_market_as_of` 给, 缺省时不标陈旧 (宁可不报也
    不误报 —— 见那个函数关于"周六"的说明)。
    """
    state = entry.get("_price_state")
    if state == "unpriced":
        # 主池剔除原因 (「已发行未上市」这类) 比一句"未定价"有用得多。
        # **注意用 batch_pricing_exclusion_reason 而不是 view_exclusion_reason** ——
        # 后者返回的是视图口径文案 (「相对市场中位 +17.9pp, 未便宜过 5pp」),
        # 而且要收一个 view 参数, 主页根本没有视图选择器。
        code = entry.get("bond_code")
        if code and terms_cache is not None:
            try:
                reason = batch_pricing_exclusion_reason(
                    code, _cached_terms_for(terms_cache, code),
                    admission_config=admission_config)
            except Exception:
                reason = None
            if reason:
                return f"未定价 · {reason}"
        return "未定价"
    if state == "failed":
        status = str(entry.get("status") or "").strip()
        # 状态列此前直接把 status 原文塞进表格, 于是一条裸异常串能把整行撑爆
        return f"失败 · {status[:18]}" if status and status != "failed" else "失败"
    if state == "no_market":
        return _PRICE_STATE_LABEL["no_market"]
    if state == "stale":
        val_date = _parse_watchlist_date(entry.get("valuation_date"))
        # 永远带日期。写死的「旧」在出差一周回来时六行仍全写「旧」, 而真实估值日
        # 在整个主页只有一个出口 —— 取不到就只说「未重算」, 不编一个日期。
        return f"未重算 {val_date.strftime('%m-%d')}" if val_date else "未重算"
    if state != "ok":
        return "—"
    if entry.get("market_price_source") == "terms_close":
        return _NO_STAMP_LABEL
    as_of = _parse_watchlist_date(entry.get("market_price_as_of"))
    if as_of is None:
        return _NO_STAMP_LABEL
    if latest_as_of is not None and as_of < latest_as_of:
        return f"市价旧 {as_of.strftime('%m-%d')}"
    return f"✓ {as_of.strftime('%m-%d')}"


def _anchor_as_of(entry):
    """这一行的横截面锚是哪一天的.

    显式戳优先 (``_annotate_off_pool`` 给主池外的行盖的, 那是**主池那一批**的
    日子); 没有戳说明锚是与这行同一批自算的 —— ``annotate_batch_results`` 的默认
    路径 —— 此时锚的 as-of 就是这行自己的估值日。
    """
    return (_parse_watchlist_date(entry.get("market_median_deviation_as_of"))
            or _parse_watchlist_date(entry.get("valuation_date")))


def _cross_section_is_stale(entry, today, *, cache_meta=None) -> bool:
    """这一行的横截面口径还能不能用 —— 决定「相对偏差」「双低」显不显示.

    落地的是 2026-08-25 拍板的第 5 条: 锚超过 5 个交易日就不再拿它当今天的基准。
    判据在 ``watchlist_cache`` 里躺了一轮没人调, 而中位偏差的水平是时变的
    (``cb_valuation_history`` 20 期实测摆幅 21.2pp) —— 用几周前的市场水平算出来的
    「比中位便宜 5pp」是个看上去完全正常的数字。

    锚值只是绝对阈值兜底留下的 0.0 (``cross_section_origin == "absolute_fallback"``,
    主池为空时的小批量标注) 同样算不能用 —— 那时 ``relative_deviation`` 恒等于绝对
    偏差, 只是顶着横截面量的名字。

    行里**完全没有锚的痕迹**时退回 *cache_meta* 里那一份 (它描述的正是热缓存这批行),
    而不是一律判成过期: ``market_median_deviation`` 是随本次改动才进 ``CACHE_FIELDS``
    的, 存量热缓存行一个都没有 —— 一律判过期会让升级后第一次开页整列全空, 要点一次
    「⚡ 关注池重算」才回来, 而那与"锚真的旧了"在表上长得一模一样。
    """
    if str(entry.get("cross_section_origin") or "") == "absolute_fallback":
        return True
    if _is_finite(entry.get("market_median_deviation")):
        return anchor_age_is_stale(_anchor_as_of(entry), today)
    if cache_meta is not None:
        return anchor_is_stale(cache_meta, today)
    return True


def _years_to_maturity_text(entry, today) -> str:
    """剩余期限 (年), **锚这一行的估值日**而不是今天.

    与批量页同口径: 那边直接取 pricer 入参 ``T``, 实测 284/284 恒等于
    ``(到期日 − 估值日)/365.25``。锚今天会让**一行之内出现两个时点** —— 市价 /
    理论价 / 偏差全是估值日那天的, 单单剩余年限跳到今天。实测 271/284 行两页显示值
    不同 (估值日 08-26 vs 今天 08-29, 差 3 天在 2 位小数上就看得出来)。

    "这一行是哪天的"由「数据状态」列与摘要条的「估值日」承载, 不该由这一列偷偷表达。
    单位统一用年而不在极短端换成天 —— 混单位会毁掉列排序。
    """
    maturity = _parse_watchlist_date(entry.get("maturity_date"))
    if maturity is None:
        return "—"
    anchor = _parse_watchlist_date(entry.get("valuation_date")) or today
    return f"{(maturity - anchor).days / 365.25:.2f}"


def _cached_terms_for(terms_cache, code):
    try:
        return terms_cache.get(code)
    except Exception:
        return None


def _render_watchlist_table(app):
    frame = getattr(app, "batch_watchlist_table_frame", None)
    if frame is None:
        return
    for child in frame.winfo_children():
        child.destroy()

    rows = _watchlist_display_rows(app)
    headers, col_widths = watchlist_columns()
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
    _attach_cell_tooltip(tree, columns, headers,
                         tooltip_headers={"标签", "事件", "数据状态"})

    if not rows:
        placeholder = ctk.CTkLabel(
            frame,
            text="尚未关注任何转债 — 在主批量列表中选中一只或多只, 点击 \"⭐ 加入关注池\" 或右键添加",
            font=(FONT_FAMILY, 12),
            text_color=TEXT_DIM,
        )
        placeholder.grid(row=2, column=0, sticky="w", padx=12, pady=(2, 8))

    admission_config = None
    terms_cache = getattr(app, "terms_cache", None)
    if terms_cache is not None:
        try:
            from .batch import _batch_admission_config
            admission_config = _batch_admission_config(app)
        except Exception:
            admission_config = None
    today = market_today()
    latest_as_of = _latest_market_as_of(rows)
    cache_meta = _price_cache(app).get("meta") or {}
    n_anchor_stale = 0

    for entry in rows:
        code = entry.get("bond_code", "")
        anchor_stale = _cross_section_is_stale(entry, today, cache_meta=cache_meta)
        n_anchor_stale += 1 if anchor_stale else 0
        is_ok = entry.get("status") == "ok"

        dev = entry.get("deviation")
        # 1 位小数, 与紧邻的「相对偏差(pp)」一致 (两页同口径)
        dev_str = f"{float(dev) * 100:+.1f}" if _is_finite(dev) else "—"

        # 这张表上**没有跨日变化列**。曾有「涨跌%」与「偏差Δ(pp)」两列锚上一交易日的
        # 窄快照, 都已删:
        #
        # - 「偏差Δ」不是独立的数 —— 理论价不动时 ``偏差Δ ≡ 涨跌 × (1 + 偏差)`` 是
        #   恒等式, 残差项整个就是理论价的日间变动 (实测只占 1%~26%, 中位约 10%)。
        # - 「涨跌%」按用户决策删除: 这是研究工作台不是行情软件, 而它恰是全 app 最像
        #   行情软件的一格 —— 红绿轴在本项目已有 4 种含义, 那一列是冲突最集中的地方。
        #
        # ``data/watchlist_daily/`` 的窄快照**仍然照写**, 只是暂时没有读者: 那个目录
        # 只追加, 停写就等于把这些天永久丢掉, 而恢复任何一列都要靠它。
        # 「相对偏差」的分母就是锚, 锚过期就打「—」(口径5)。
        rel = entry.get("relative_deviation")
        rel_str = ("—" if anchor_stale
                   else (f"{float(rel) * 100:+.1f}" if _is_finite(rel) else "—"))
        # **「双低」不跟着锚变暗**。``double_low = 市价 + 转股溢价率×100`` 是纯局部量,
        # 只有它的 ``double_low_rank/percentile`` 需要横截面; 而表上显示的是**值**。
        # 此前它搭了相对偏差的车, 锚一过期整列黑 —— 而「双低 <130」是不需要任何锚
        # 就能读的行业经验阈值。
        dbl = entry.get("double_low")
        dbl_str = f"{float(dbl):.0f}" if _is_finite(dbl) else "—"

        vals = [
            code,
            entry.get("bond_name", "") or "",
            # 正股**名称**优先, 缺失才回落代码。实测 cb_data 的 underlying_name
            # 只有 722/1059 —— 08-24 曾是 1033/1058, 被一次全量条款同步清掉 317 只
            # (当时全量同步是整条记录替换, 只保 ``credit_rating``; 现已按 provider
            # 声明的字段所有权保护, 见 ``cb_data_sync.locally_authoritative_fields``,
            # 但存量缺口要等下一次状态刷新才补得回来)。
            # 所以回落不是保守, 是当前必需: 直接换成名字会让三成的行变空。
            entry.get("underlying_name") or entry.get("stock_code", "") or "—",
            # 三档: 过去日期 (这只债有多新) / 未来日期 (还有几天挂牌) / 「待定」
            # (已发行未上市、上市日还没公告)。**必须读 merged 后的 entry** ——
            # watchlist.json 意图层是加入那一刻的快照、不会更新, 实测先锋转债
            # 那里仍是 None 而热缓存已是 2026-08-26; 靠 _PRICED_MERGE_FIELDS 的
            # "非 None 才覆盖"让定价行胜出, 这一格才会自己翻过来。
            _format_listing_cell(entry, "listing_date"),
            # 一律用 _is_finite 而不是 `is not None`: 落盘的 None 读回来是 **NaN**
            # (与内存路径一致, 见 watchlist_cache._NAN_FIELDS), 而 NaN is not None 为真 ——
            # 于是"今天没有市价"会被渲染成字面的 "nan"。实测三只未上市新债全中。
            f"{float(entry['market_price']):.2f}" if _is_finite(entry.get("market_price")) else "—",
            f"{float(entry['theoretical_price']):.2f}" if is_ok and _is_finite(entry.get("theoretical_price")) else "—",
            dev_str,
            rel_str,
            dbl_str,
            # 事件旗标已按可操作性排好序 (batch_pricing.event_flags), 全列不截断
            " / ".join(entry.get("event_flags") or []) or "—",
            # 下修博弈活没活。负 = 正股价已在触发线下方。列名是比值形式
            # (「正股/下修线」) 而不是「距下修线」—— 否则负号读作"距离是负的"
            trigger_gap_text(entry.get("down_reset_trigger_gap")),
            # drop_covered: 「无市价」「无偏差」已由「数据状态」列以更具体的形式承载
            _format_tags(entry.get("risk_tags"), drop_covered=True),
            # 实测关注池双峰: 6 只 <0.6 年 (南航 0.13 ≈ 47 天), 6 只 ~5.95 年。此前
            # 只有「短久期」「近到期」两个标签暗示, 而南航 (47 天) 与上银 (5 个月)
            # 标的是**同一个**「短久期」。T 不在 CACHE_FIELDS, 从到期日算。
            _years_to_maturity_text(entry, today),
            # 摘要条一直在报「平均评级」, 逐行却看不到是哪只把它拖下来的
            entry.get("credit_rating", "") or "—",
            _row_data_label(entry, terms_cache=terms_cache,
                            admission_config=admission_config,
                            latest_as_of=latest_as_of),
            (entry.get("added_at", "") or "")[:10],
        ]
        row_tag = _resolve_row_tag(entry)
        tags = [row_tag] if row_tag else []
        # pad_cells: 右对齐列补尾随留白, 否则和右边左对齐列的文字贴在边界上
        tree.insert("", "end", iid=code, values=pad_cells(headers, vals), tags=tags)

    app._batch_watchlist_tree = tree
    _TREE_ATTRS.add("_batch_watchlist_tree")
    _attach_watchlist_context_menu(app, tree)
    _refresh_watchlist_summary(app, rows, anchor_stale_rows=n_anchor_stale)


def refresh_home(app) -> None:
    """主页数据变了就调这一下: 表 + 摘要 + 事件横幅.

    横幅的刷新**从 ``_render_watchlist_table`` 末尾提到这里**。原先它寄生在表渲染
    里且是全仓库唯一调用点 —— 于是任何"少画一次表"的优化都会顺手把横幅一起停掉,
    而横幅失败是静默的 (拿不到 label/var 直接 return)。它的扫描集本来也不只是关注池
    (关注池为主 + 全池计数), 挂在关注池表的渲染上本就不合理。
    """
    _render_watchlist_table(app)
    _refresh_events_banner(app)


def _attach_watchlist_context_menu(app, tree):
    menu = tk.Menu(tree, tearoff=0, font=(FONT_FAMILY, 12))
    menu.add_command(label="载入单债定价页 (双击)",
                     command=lambda: _load_watchlist_selection_in_pricing_tab(app))
    menu.add_command(label="🗑 从关注池移除…",
                     command=lambda: _remove_selected_from_watchlist(app))
    menu.add_command(label="↩ 撤销上一次移除",
                     command=lambda: _undo_remove_from_watchlist(app))

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
        # 同批量页: 双击是确定的选择, 不必再等打字防抖
        if hasattr(app, "_flush_pending_bond_autoload"):
            app._flush_pending_bond_autoload()
    if hasattr(app, "tab_seg") and hasattr(app, "_switch_tab"):
        app.tab_seg.set(E("⚡ 定价"))
        app._switch_tab(E("⚡ 定价"))
    app.v_watchlist_status.set(f"已载入单债定价页: {code}")


# ── 摘要条 / 事件横幅 ─────────────────────────────────────────
def _refresh_watchlist_summary(app, rows, *, anchor_stale_rows: int = 0):
    """汇总关注池: 关注数 / 等权偏差中位 / 平均评级 / 异常计数.

    末尾还带两条**口径出处**, 它们是表上唯一说清"这些数是哪天的"的地方:
    「估值日 MM-DD」(取展示行里出现最多的那个估值日) 与锚过期时的一句提示。
    没有它们, 「数据状态」列写「旧 · 08-26」而摘要条只字不提, 用户仍然要逐行看才知道
    整页是不是都陈旧了。
    """
    # **写状态行本身** (与批量页同格式: 一行左对齐加粗, ✅ 开头, `|` 分组)。
    # 此前是右对齐的一小条灰字, 而左半边 (v_watchlist_status) 在空闲时是空的 ——
    # 于是那一行看着像"右边飘着一段孤字"。合成一行之后与批量页的
    # 「✅ 低估候选: 展示 41/283 只 | 成功 41 失败 0」完全同构。
    #
    # 动作消息 (⚡ 已刷新 N 只) 写的是同一个变量, 靠**时序**分先后: worker 先
    # `refresh_home` (走到这里写摘要) 再 `set(msg)`, 后者覆盖 —— 与批量页 worker
    # 的顺序逐字一致。下一次 refresh_home 摘要再回来。
    summary_var = getattr(app, "v_watchlist_status", None)
    if summary_var is None:
        return
    if not rows:
        summary_var.set("✅ 关注池: 空 — 在批量页选中后点「⭐ 加入关注池」")
        return

    n = len(rows)
    priced = [r for r in rows if r.get("status") == "ok"]
    devs = [r.get("deviation") for r in priced]
    median_dev = _median(devs)

    rating_label = average_rating_label(r.get("credit_rating") for r in priced) or "—"

    # 偏差离群的**两个方向分开报**。这里曾是一份与 batch_common 行色判据一模一样的
    # 私有字面量集合, 把相对偏差中位 −21.95pp 与 +27.76pp 的两族合成一句「⚠ 异常 N」
    # —— 两边同时抄、同时错, 而它们分属机会信号与模型适用性两个不同维度。
    def _count(tags: frozenset[str]) -> int:
        return sum(1 for r in priced if tags & set(r.get("risk_tags") or []))

    n_overvalued = _count(MODEL_OVERVALUED_TAGS)
    n_undervalued = _count(DEEP_UNDERVALUED_TAGS)
    n_legacy_outlier = _count(LEGACY_DEVIATION_OUTLIER_TAGS)

    # 分组与批量页同构: 「✅ 名称: 规模」|「口径」|「异常」|「出处」
    # 「关注 N」不是「持仓 N」—— 口径1: 纯研究关注清单, 不记持仓。
    head = f"关注 {n}" + (f" · 已定价 {len(priced)}" if priced else "")
    parts = [f"✅ 关注池: {head}"]
    caliber = []
    if median_dev is not None:
        caliber.append(f"偏差中位 {median_dev*100:+.1f}%")
    # 这里曾报「机会分均值」—— 那个字段已整体删除, 见 AGENTS「机会分已整体删除」。
    if rating_label != "—":
        caliber.append(f"平均评级 {rating_label}")
    if caliber:
        parts.append(" · ".join(caliber))
    # **走 risk_tag_label 取展示名, 不在这里另写一份字面量**。此前直接写
    # 「⚠ 模型高估 2」—— 那是动宾读法, 而事实是**市价**远高于模型价 (模型给的价低),
    # 方向正好读反; 而且它绕过了 RISK_TAG_DISPLAY_LABEL, 于是标签列已经改成
    # 「市价远高于模型价」时摘要条还在说「模型高估」, 同一页两种说法。
    for count, tags in ((n_overvalued, MODEL_OVERVALUED_TAGS),
                        (n_undervalued, DEEP_UNDERVALUED_TAGS),
                        (n_legacy_outlier, LEGACY_DEVIATION_OUTLIER_TAGS)):
        if count:
            parts.append("⚠ " + " / ".join(
                risk_tag_label(t) for t in sorted(tags)) + f" {count}")

    val_dates = [d for d in (_parse_watchlist_date(r.get("valuation_date")) for r in rows)
                 if d is not None]
    if val_dates:
        # 众数而不是 max: 一行刚补过价不代表整页都是今天的。
        # 平局取**较晚**那个而不是让 set 的迭代序决定 —— date 的 hash 受
        # PYTHONHASHSEED 随机化, 否则同一份数据每次开页可能报不同的估值日。
        dominant = max(set(val_dates), key=lambda d: (val_dates.count(d), d))
        parts.append(f"估值日 {dominant.strftime('%m-%d')}")
    if anchor_stale_rows:
        parts.append(f"⚠ 横截面锚已过期 {anchor_stale_rows} 行 (相对偏差/双低暂不可比)")
    summary_var.set("  |  ".join(parts))


def _watchlist_scan_codes(app) -> set[str]:
    """横幅的**主**扫描集 = 关注池。这块挂在「⭐ 我的关注池」主页上, 首要问题是
    "我在盯的这几只今天有什么事"。"""
    return {e.get("bond_code") for e in (app._batch_watchlist or []) if e.get("bond_code")}


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


def _set_banner_tone(label, *, alert: bool) -> None:
    """事件横幅的色调 —— 有事件才是警报色.

    ``configure`` 传的是 theme 里的 (浅色, 深色) 二元组, CustomTkinter 会按当前
    外观模式自己挑, 所以主题切换后不需要再动它。
    """
    try:
        label.configure(text_color=ORANGE if alert else TEXT_DIM)
    except Exception:                                    # 测试里的假 label
        logger.debug("事件横幅色调设置失败 (忽略)", exc_info=True)


def _refresh_events_banner(app, *, past_days: int = 7, window_days: int = 30,
                           head: int = 5):
    """关注池的事件区: 「近 N 天已发生」+「未来 M 天」.

    **只讲关注池里的标的**。此前末尾还挂一句「全池另有 N 件」, 理由是"横幅真正的
    用处是告诉你**还不知道的那些**" —— 那条理由已按用户决策推翻: 这是**我的**关注池,
    池外的债在别的页面找。

    **空态显式写文案, 不 grid_remove**: "空"是常态而不是异常 (实测这个 5 只的关注池在
    7/14/30/60/90/180 天每个窗口都是 0 件), 而把控件藏起来会重演「低估候选默认打开是
    空表、用户以为坏了」那次 —— 一个消失的控件和一个坏掉的控件长得一模一样。

    **但空态不是警报**: 颜色跟着内容走 (见 :func:`_set_banner_tone`)。删掉池外那段
    之后横幅会更经常处于空态, 而一条橙色警告条常年说"什么都没发生", 训练出来的行为
    就是忽略它 —— 那正是真事件出现时会被漏掉的原因。
    """
    label = getattr(app, "lbl_batch_events_banner", None)
    var = getattr(app, "v_batch_events_banner", None)
    if label is None or var is None:
        return

    store = getattr(app, "event_store", None)
    watch_codes = _watchlist_scan_codes(app)
    if store is None:
        var.set("事件表未载入")
        app._batch_events_banner_full = []
        label.grid()
        return

    today = market_today()
    past = collect_upcoming_events(
        store, watch_codes, today - timedelta(days=past_days), today)
    future = collect_upcoming_events(
        store, watch_codes, today, today + timedelta(days=window_days))

    names = {row.get("bond_code"): row.get("bond_name")
             for row in (getattr(app, "_batch_all_results", None) or [])}
    for entry in (app._batch_watchlist or []):
        code, name = entry.get("bond_code"), entry.get("bond_name")
        if code and name and code not in names:
            names[code] = name

    # 明细留给弹窗, 一条不少; 过去的排在前面 (它们是"已经发生了而你可能没看见")
    app._batch_events_banner_full = list(past) + list(future)

    segments = []
    if past:
        segments.append(f"近 {past_days} 天 {len(past)} 件: "
                        + "  ·  ".join(_group_banner_entries(past, names)[:head]))
    if future:
        segments.append(f"未来 {window_days} 天 {len(future)} 件: "
                        + "  ·  ".join(_group_banner_entries(future, names)[:head]))
    # **颜色跟着内容走**: 有事件才用警报色 + ⚠。此前无条件 ORANGE + ⚠, 而实测这个
    # 关注池 (5 只) 在 7/14/30/60/90/180 天**每个窗口都是 0 件** —— 一条橙色警告条常年
    # 说"什么都没发生", 训练出来的行为是忽略它, 而那正是真事件出现时会被漏掉的原因。
    if segments:
        var.set("⚠ 关注池事件  |  " + "  |  ".join(segments))
        _set_banner_tone(label, alert=True)
    else:
        # 空态仍**显式写文案**, 不 grid_remove —— 一个消失的控件和一个坏掉的控件
        # 长得一模一样 (与「低估候选默认打开是空表」同源)。只是不再用警报色。
        var.set(f"📅 关注池事件  |  已扫 {len(watch_codes)} 只 · "
                f"近 {past_days} 天与未来 {window_days} 天均无日程事件")
        _set_banner_tone(label, alert=False)
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
