from dataclasses import dataclass
from datetime import date, timedelta

import inspect

import pytest

from convertible_bond.gui.tabs import batch as batch_tab
from convertible_bond.gui.tabs import batch_watchlist as watchlist_tab
from convertible_bond.gui.tabs.batch_common import _is_new_bond, _resolve_row_tag
from convertible_bond.gui.tabs.batch_watchlist import (
    _format_days_to_trade,
    _format_listing_cell,
)
from convertible_bond.market_time import market_today


def test_listed_tradable_bond_is_not_marked_new():
    row = {
        "bond_code": "118067.SH",
        "status": "ok",
        "is_tradable": True,
        "trading_status": "tradable",
        "listing_date": market_today() - timedelta(days=5),
        "tradable_date": market_today() - timedelta(days=5),
    }

    assert _is_new_bond(row) is False
    assert _resolve_row_tag(row) is None


def test_future_tradable_bond_is_marked_new():
    row = {
        "bond_code": "123999.SZ",
        "status": "ok",
        "is_tradable": False,
        "trading_status": "pending",
        "listing_date": market_today() + timedelta(days=2),
        "tradable_date": market_today() + timedelta(days=2),
    }

    assert _is_new_bond(row) is True
    assert _resolve_row_tag(row) == "new"


def test_issued_but_unlisted_bond_is_marked_new_after_pricing():
    """已发行未上市的新债定价成功后仍要保留新债高亮 (状态按估值日重算)."""
    row = {
        "bond_code": "123284.SZ",
        "status": "ok",
        "is_tradable": False,
        "trading_status": "pending",
        "listing_date": None,
        "tradable_date": None,
        "theoretical_price": 130.35,
    }

    assert _is_new_bond(row) is True
    assert _resolve_row_tag(row) == "new"


def test_watchlist_cells_show_pending_listing_as_undetermined():
    """上市日未公告 → 显示"待定"而不是"—", 后者读起来像缺数据."""
    entry = {
        "bond_code": "123284.SZ",
        "trading_status": "pending",
        "listing_date": None,
        "tradable_date": None,
        "days_to_trade": 3,  # 上一轮扫描留下的旧值, 不能显示
    }

    assert _format_listing_cell(entry, "listing_date") == "待定"
    assert _format_listing_cell(entry, "tradable_date") == "待定"
    assert _format_days_to_trade(entry) == "待定"


def test_watchlist_cells_keep_dash_for_plain_missing_dates():
    entry = {"bond_code": "128009.SZ", "trading_status": "tradable"}

    assert _format_listing_cell(entry, "listing_date") == "—"
    assert _format_days_to_trade(entry) == "—"


def test_watchlist_days_to_trade_uses_known_listing_date():
    entry = {
        "bond_code": "123281.SZ",
        "trading_status": "pending",
        "listing_date": market_today() + timedelta(days=4),
        "tradable_date": market_today() + timedelta(days=4),
    }

    assert _format_listing_cell(entry, "tradable_date") == (market_today() + timedelta(days=4)).isoformat()
    assert _format_days_to_trade(entry) == "+4"


def test_days_to_trade_shows_already_tradable_instead_of_negative():
    """"距交易 -3" 没有意义 — 可交易日已过就是能买了."""
    entry = {
        "bond_code": "123284.SZ",
        "trading_status": "tradable",
        "tradable_date": market_today() - timedelta(days=3),
    }
    assert _format_days_to_trade(entry) == "已可交易"

    today = {"bond_code": "123284.SZ", "tradable_date": market_today()}
    assert _format_days_to_trade(today) == "已可交易"

    stale_days = {"bond_code": "123284.SZ", "days_to_trade": -3}
    assert _format_days_to_trade(stale_days) == "已可交易"


# ── 批量页列 / 定价参数的静态守护 ──
#
# GUI 在测试环境跑不起真实渲染, 但这些是纯数据结构与源码常量, 可以机器兜底。

def test_every_batch_column_has_a_getter_and_a_stretch_weight():
    """列表加了列却忘了 getter, 运行期才 KeyError; F821 也扫不到 (是字典键不是名字)。"""
    for preset in (batch_tab._BATCH_COLS_FULL, batch_tab._BATCH_COLS_SIMPLE):
        for name, _width in preset:
            assert name in batch_tab._BATCH_COL_GETTERS, f"{name} 缺 getter"
            assert name in batch_tab._BATCH_COL_STRETCH_WEIGHTS, f"{name} 缺列宽权重"


def test_batch_column_getters_tolerate_missing_and_nan_values():
    """定价失败行 / 旧缓存行不带新字段, 取值函数必须退化成 '—' 而不是抛异常。"""
    import math
    for row in ({}, {"status": "error"},
                {"status": "ok", "relative_deviation": math.nan,
                 "double_low": None, "down_reset_robust_edge_value": math.nan}):
        for name, getter in batch_tab._BATCH_COL_GETTERS.items():
            assert isinstance(getter(row), str), f"{name} 在缺值行上没返回字符串"


def test_event_and_trigger_columns_are_present():
    """确定性的日程安排此前全算好了却一个都不显示 —— 强赎日、在途下修、回售窗口。"""
    simple = [name for name, _ in batch_tab._BATCH_COLS_SIMPLE]
    full = [name for name, _ in batch_tab._BATCH_COLS_FULL]
    assert "事件" in simple, "事件是最该被看见的一类, 不能只在完整视图里"
    assert "距下修线" in full


def test_event_column_never_truncates():
    """tooltip 取的是单元格 display value, 一旦截断被隐藏的那条就彻底看不见了。"""
    row = {"event_flags": ["强赎 08-27", "下修提议 09-05", "暂停转股", "不强赎至 27-01"]}
    rendered = batch_tab._BATCH_COL_GETTERS["事件"](row)
    for flag in row["event_flags"]:
        assert flag in rendered


def test_simple_view_leads_with_cross_sectional_signals_not_score():
    """简洁视图的决策位必须是相对偏差 —— 机会分在 92% 的行上与错定价无关。

    机会分没有删除, 切「完整」仍可查; 但它不该占着人第一眼看的那一列。
    """
    simple = [name for name, _ in batch_tab._BATCH_COLS_SIMPLE]
    assert "相对偏差" in simple
    assert "机会分" not in simple
    assert "机会分" in [name for name, _ in batch_tab._BATCH_COLS_FULL]
    assert simple.index("相对偏差") < simple.index("偏差(%)")


def test_both_pricing_entries_request_pde_down_reset_signals():
    """主池与关注池两条定价路径都要开 PDE 下修信号, 否则「下修优势」列一半是空的。

    这个 kwarg 走 ``**pricer_overrides``, 漏传不报错只静默不算 —— 正是它此前在批量页
    缺席、让稳健下修优势在缓存里 0/280 有值的原因。
    """
    for module in (batch_tab, watchlist_tab):
        src = inspect.getsource(module)
        assert "compute_pde_signals=True" in src, f"{module.__name__} 未开 PDE 下修信号"


# ── 事件横幅 ──
#
# 此前只扫关注池 —— 主池里昨天出的下修提议不会浮出来, 除非你已经在关注它。
# 而"已经在关注"恰恰意味着你已经知道了。

@dataclass
class _Ev:
    bond_code: str
    event_type: str
    event_date: date
    effective_start: date | None = None
    effective_end: date | None = None


class _Store:
    def __init__(self, events):
        self._by_code = {}
        for ev in events:
            self._by_code.setdefault(ev.bond_code, []).append(ev)

    def list_events(self, bond_code=None):
        return list(self._by_code.get(bond_code, []))


TODAY = date(2026, 8, 24)
HORIZON = TODAY + timedelta(days=30)


def _collect(events, codes=None):
    from convertible_bond.gui.tabs.batch_watchlist import collect_upcoming_events
    store = _Store(events)
    return collect_upcoming_events(
        store, codes or sorted({e.bond_code for e in events}), TODAY, HORIZON)


def test_banner_shows_the_date_that_actually_falls_in_the_window():
    """入窗判定看三个日期中任意一个, 显示的却固定是 effective_start ——
    于是 effective_end 在窗口内的区间事件, 会把几个月前的起始日当成"未来 30 天的事"。
    """
    rows = _collect([_Ev("A.SH", "call_no_redemption", date(2026, 3, 1),
                         effective_start=date(2026, 3, 1), effective_end=date(2026, 8, 26))])
    assert rows == [("A.SH", "不强赎到期", date(2026, 8, 26))]


def test_banner_ignores_events_entirely_outside_the_window():
    assert _collect([_Ev("A.SH", "rating_change", date(2025, 1, 1))]) == []
    assert _collect([_Ev("A.SH", "rating_change", date(2027, 1, 1))]) == []


def test_banner_ignores_untrustworthy_end_dates():
    """conversion_suspension 的 end 被公告里的回售期区间污染, 不能当"未来事件"。"""
    contaminated = _Ev("A.SH", "conversion_suspension", date(2024, 10, 25),
                       effective_start=date(2021, 3, 11), effective_end=date(2026, 9, 3))
    assert _collect([contaminated]) == []


def test_banner_dedupes_repeat_announcements():
    """同一件事常有"第N次提示性公告"多条 (实测鸿路转债 33 条 putback)。"""
    window = dict(effective_start=date(2026, 8, 25), effective_end=date(2026, 8, 31))
    rows = _collect([_Ev("A.SH", "putback", date(2026, 8, 20), **window),
                     _Ev("A.SH", "putback", date(2026, 8, 21), **window),
                     _Ev("A.SH", "putback", date(2026, 8, 22), **window)])
    assert rows == [("A.SH", "回售", date(2026, 8, 25))]


def test_banner_orders_by_actionability_not_by_date():
    """纯按日期排会让"评级调整"挤掉三天后的强赎。"""
    rows = _collect([
        _Ev("R.SH", "rating_change", date(2026, 8, 25)),
        _Ev("C.SH", "call_redemption", date(2026, 8, 28)),
        _Ev("D.SH", "down_reset_proposed", date(2026, 8, 27)),
    ])
    assert [code for code, _t, _d in rows] == ["C.SH", "D.SH", "R.SH"]


def test_banner_groups_repeats_so_the_urgent_one_survives():
    """扫全主池后同类成片 (实测 22 件里 11 件是「不下修到期」), 逐条铺开会占满展示位。"""
    from convertible_bond.gui.tabs.batch_watchlist import _group_banner_entries
    upcoming = [("C.SH", "强赎截止", date(2026, 8, 27))]
    upcoming += [(f"N{i}.SH", "不下修到期", date(2026, 8, 25) + timedelta(days=i))
                 for i in range(11)]
    parts = _group_banner_entries(upcoming, {"C.SH": "应流转债"})
    assert parts[0] == "应流转债 强赎截止 (08-27)"
    assert parts[1] == "不下修到期 x11 (最早 08-25)"
    assert len(parts) == 2                       # 12 条压成 2 段, 紧急那条不会被挤掉


def test_banner_scan_sets_are_split_but_neither_is_dropped():
    """横幅搬到关注池主页后, 扫描集拆成两个:

    - 关注池是**主**扫描集 ("我在盯的这几只今天有什么事")
    - 全池仍在, 但从"铺满横幅"降级成"末尾一句计数 + 单击展开" ——
      原来那条理由 ("横幅真正的用处是告诉你**还不知道的那些**") 依然成立,
      不能因为换了页面就把全池那 50 多件事整个丢掉。
    """
    from convertible_bond.gui.tabs.batch_watchlist import (
        _pool_scan_codes, _watchlist_scan_codes)

    class _App:
        _batch_watchlist = [{"bond_code": "W.SH"}]
        _batch_all_results = [{"bond_code": "M1.SH"}, {"bond_code": "M2.SH"}, {}]

    assert _watchlist_scan_codes(_App()) == {"W.SH"}
    assert _pool_scan_codes(_App()) == {"M1.SH", "M2.SH"}


# ── 扫新债: 窄同步 → 扫描 ──
#
# 原本这条路是"读 bundle_meta()['updated_at'] 判新鲜度 → 提示跑 cb-sync-tradable --incremental"。
# 三处同时失效: updated_at 被任何一次写盘推到今天 (提示永不弹出); 就算弹出, 增量同步按
# 7 天新鲜度**恰好跳过**刚被抓过的新债; 没装 WindPy 连提示都不给。详见
# convertible_bond/new_issue_sync.py。

class _FakeStatus:
    def __init__(self):
        self.value = ""
        self.history = []

    def set(self, value):
        self.value = value
        self.history.append(value)

    def get(self):
        return self.value


class _FakeApp:
    """够跑通同步→回调这条链的最小 app: after 同步执行, 线程 join 掉."""

    def __init__(self):
        self.v_batch_status = _FakeStatus()
        self.pool_syncs = []

    def after(self, _delay, fn):
        fn()

    def _run_pool_sync(self, module, label, extra_args=(), **kwargs):
        self.pool_syncs.append((module, extra_args))


def _run_sync_to_completion(monkeypatch, app, *, sync_result=None, exc=None, **kwargs):
    import convertible_bond.new_issue_sync as new_issue_sync

    def fake_sync(*_a, **_kw):
        if exc is not None:
            raise exc
        return sync_result or {"changes": []}

    monkeypatch.setattr(new_issue_sync, "sync_new_issues", fake_sync)
    seen = []
    real_thread = watchlist_tab.threading.Thread

    def blocking_thread(*args, **thread_kwargs):
        thread = real_thread(*args, **thread_kwargs)
        thread.start()
        thread.join(timeout=5)
        return _NoopThread()

    monkeypatch.setattr(watchlist_tab.threading, "Thread", blocking_thread)
    watchlist_tab.run_new_issue_sync_async(app, then=seen.append, **kwargs)
    return seen


class _NoopThread:
    def start(self):
        pass


def test_scan_new_issues_no_longer_asks_before_syncing(monkeypatch):
    """窄同步只碰那几只新债 (秒级, 不需要 Wind), 所以直接做, 不再问用户."""
    app = _FakeApp()
    seen = _run_sync_to_completion(
        monkeypatch, app,
        sync_result={"changes": [{"bond_code": "118076.SH", "kind": "listing_date"}]})

    assert seen == [True]                    # 后续流程照常触发
    assert app.pool_syncs == []              # 没有弹窗, 也没有退回全库增量同步
    assert any("新债上市日" in text for text in app.v_batch_status.history)


def test_scan_continues_when_the_narrow_sync_fails(monkeypatch):
    """取数失败不能阻断扫描 —— 退回按现有条款库继续, 状态栏说明原因."""
    app = _FakeApp()
    seen = _run_sync_to_completion(monkeypatch, app, exc=RuntimeError("网络不通"))

    assert seen == [False]
    assert "网络不通" in app.v_batch_status.value


def test_concurrent_scan_requests_are_dropped(monkeypatch):
    """「扫新债」与「批量重算」共用这条路径, 同步的这两秒里两个按钮都还能点."""
    app = _FakeApp()
    app._new_issue_sync_running = True
    seen = _run_sync_to_completion(monkeypatch, app)

    assert seen == []


def test_batch_rerun_refreshes_listing_dates_first():
    """批量重算前也要刷一次: 准入读的是 cb_data 的 listing_date, 不刷就把昨天挂牌的新债判死."""
    src = inspect.getsource(batch_tab._run_batch)
    assert "run_new_issue_sync_async" in src


# ── 关注池取价的口径 ──
#
# `_batch_results` 是**视图过滤后**的子表 (见 _render_batch_views), `_batch_all_results` 才是全池。
# 关注的债多半不在「低估候选」这类窄视图里 —— 读错变量, 关注池就整行显示「—」, 且理论价
# 随主表视图开关忽有忽无。实测视图 40/284 只, 中仑/派克/先锋三只在池内定价成功却都不在视图中。

def _watchlist_app(*, all_results, view_results, upcoming=(), watchlist=()):
    app = _FakeApp()
    app._batch_all_results = list(all_results)
    app._batch_results = list(view_results)
    app._batch_upcoming_results = list(upcoming)
    app._batch_watchlist = [dict(row) for row in watchlist]
    return app


def test_watchlist_price_survives_a_narrow_main_view():
    """主表切到窄视图时, 关注池的理论价不能跟着消失."""
    priced = {"bond_code": "123281.SZ", "bond_name": "中仑转债",
              "status": "ok", "theoretical_price": 110.78}
    app = _watchlist_app(
        all_results=[priced],
        view_results=[],                       # 「低估候选」视图里没有它
        watchlist=[{"bond_code": "123281.SZ", "bond_name": "中仑转债"}],
    )

    row = watchlist_tab._watchlist_display_rows(app)[0]

    assert row["theoretical_price"] == 110.78
    assert row["status"] == "ok"


def test_watchlist_repricing_of_main_pool_bonds_reaches_the_table():
    """⚡关注池重算 把主池标的写进 _batch_all_results —— 展示层必须读得到.

    读 `_batch_results` 时这条路是死的: 状态栏报"主表 N / 关注 M", 而表里只有走
    `_batch_upcoming_results` 的那 M 只出得来价, 主表那 N 只点多少次都是「—」。
    """
    app = _watchlist_app(
        all_results=[{"bond_code": "111026.SH", "status": "ok", "theoretical_price": 108.69}],
        view_results=[],
        upcoming=[{"bond_code": "123284.SZ", "status": "ok", "theoretical_price": 128.93}],
        watchlist=[{"bond_code": "111026.SH"}, {"bond_code": "123284.SZ"}],
    )

    priced = {row["bond_code"]: row.get("theoretical_price")
              for row in watchlist_tab._watchlist_display_rows(app)}

    assert priced == {"111026.SH": 108.69, "123284.SZ": 128.93}


# ── 新债没价时的自愈 ──
#
# 新债不在主池 (剔除原因「已发行未上市」), 理论价只能来自 upcoming_results。那一格一旦
# 没跑到就再没有自愈路径: 启动时 _load_result_cache 只把缓存里的空列表读回来, 行一直空着。

def test_unpriced_new_bonds_are_picked_up_for_repricing():
    app = _watchlist_app(
        all_results=[{"bond_code": "128044.SZ", "status": "ok", "theoretical_price": 105.0}],
        view_results=[],
        watchlist=[
            {"bond_code": "128044.SZ", "is_tradable": True, "trading_status": "tradable"},
            {"bond_code": "123284.SZ", "is_tradable": False, "trading_status": "pending"},
        ],
    )

    assert watchlist_tab.unpriced_new_bond_codes(app) == ["123284.SZ"]


def test_priced_new_bond_is_not_repriced_again():
    """已经有价的新债不再补枪 —— 否则每次加载缓存都白跑一轮."""
    app = _watchlist_app(
        all_results=[],
        view_results=[],
        upcoming=[{"bond_code": "123284.SZ", "status": "ok", "theoretical_price": 128.93}],
        watchlist=[{"bond_code": "123284.SZ", "is_tradable": False, "trading_status": "pending"}],
    )

    assert watchlist_tab.unpriced_new_bond_codes(app) == []


def test_cache_load_repairs_stale_and_missing_prices():
    """启动加载缓存后要补一轮.

    判据已从"是不是没价的新债"放宽成 `_price_state != "ok"` —— 隔夜的旧价、
    上一轮失败的行原本没有任何人管。
    """
    src = inspect.getsource(batch_tab._load_result_cache)
    assert "refresh_stale_watchlist" in src
    # 这一轮不是用户发起的 (启动 80ms 后自动跑): 失败只写状态栏, 不许糊一个模态错误框
    assert "quiet=True" in src


def test_watchlist_pricing_is_single_flight():
    """三个入口 (⚡重算 / 扫新债 / 缓存加载补价) 并发跑会互相覆盖 new_upcoming."""
    app = _watchlist_app(all_results=[], view_results=[],
                         watchlist=[{"bond_code": "123284.SZ", "trading_status": "pending"}])
    app._watchlist_pricing_running = True

    assert watchlist_tab._start_watchlist_pricing(app, ["123284.SZ"]) is False
    assert watchlist_tab.price_unpriced_new_bonds(app) == 0


# ── 关注池定价合并与降级守卫 (S4) ────────────────────────────────

def _wl_row(code, status="ok", price=110.0):
    return {"bond_code": code, "status": status, "theoretical_price": price,
            "market_price": 108.0, "valuation_date": "2026-08-26"}


def test_merge_only_ok_rows_overwrite_existing_good_rows():
    """一次取数失败不该把内存里昨天算好的行换成 nan 行."""
    main = [_wl_row("A", price=100.0)]
    upcoming = [_wl_row("B", price=200.0)]
    fresh = [_wl_row("A", status="failed", price=float("nan")),
             _wl_row("B", status="failed", price=float("nan"))]

    new_main, new_upcoming = watchlist_tab.merge_watchlist_pricing(main, upcoming, fresh)
    assert new_main[0]["theoretical_price"] == 100.0
    assert new_upcoming[0]["theoretical_price"] == 200.0


def test_merge_ok_rows_do_overwrite():
    main = [_wl_row("A", price=100.0)]
    new_main, _ = watchlist_tab.merge_watchlist_pricing(
        main, [], [_wl_row("A", price=111.0)])
    assert new_main[0]["theoretical_price"] == 111.0


def test_merge_appends_new_failures_to_upcoming():
    """失败的**在途新债**仍要出现在 upcoming 里.

    新债不进主池, 唯一来路就是 upcoming。顺手加一句
    `if status != "ok": continue` 会让它彻底消失 —— 于是"取价失败"和
    "我根本没关注它"变成同一种表现。
    """
    _, new_upcoming = watchlist_tab.merge_watchlist_pricing(
        [], [], [_wl_row("NEW", status="failed")])
    assert [r["bond_code"] for r in new_upcoming] == ["NEW"]
    assert new_upcoming[0]["status"] == "failed"


def test_merge_does_not_mutate_inputs():
    main, upcoming = [_wl_row("A")], [_wl_row("B")]
    watchlist_tab.merge_watchlist_pricing(main, upcoming, [_wl_row("C")])
    assert len(main) == 1 and len(upcoming) == 1


def test_worker_has_zero_success_guard_and_persists():
    """源码守护: 全失败不落盘不覆盖内存, 成功才写 watchlist_cache.

    这两件事都没法在无 Tk 环境跑真 worker 验证, 但它们各自对应一次会静默发生的
    数据损坏, 所以在这里钉住源码形态。
    """
    src = inspect.getsource(watchlist_tab._watchlist_pricing_worker)
    assert 'ok_rows = [r for r in results if r.get("status") == "ok"]' in src
    assert "if not ok_rows:" in src
    assert "save_watchlist_pricing(" in src
    assert "今日取价失败" in src
    # 落盘必须用主池锚, 不能让这几行自算
    assert "cross_section_anchor_from(" in src


def test_lock_is_set_immediately_before_thread_start():
    """置位必须紧挨 Thread.start(), 中间不许有会抛的裸控件访问.

    原先顺序是 `_watchlist_pricing_running = True` → `btn.configure(...)` →
    `Thread.start()`。中间那次访问一旦抛, finally 永不执行, 三个入口全被单飞
    检查静默挡死 —— 而检查只 return False, 不写状态、不排队。
    """
    src = inspect.getsource(watchlist_tab._start_watchlist_pricing)
    lock_at = src.index("app._watchlist_pricing_running = True")
    start_at = src.index("threading.Thread(")
    between = src[lock_at:start_at]
    assert "configure(" not in between, f"置位与起线程之间不该有控件访问:\n{between}"
    assert "_start_progress" not in between


def test_quiet_round_does_not_touch_shared_progress():
    """quiet 那一轮 (启动自愈) 不碰全局进度条与按钮.

    _start_progress 没有引用计数, 且 _tick_progress 写的是**全局** v_status ——
    一轮后台自愈会把别的页正在跑的任务的状态文字顶掉, 而 _stop_progress 不还原。
    """
    for fn in (watchlist_tab._start_watchlist_pricing,
               watchlist_tab._watchlist_pricing_worker):
        src = inspect.getsource(fn)
        for lineno, line in enumerate(src.splitlines()):
            if "_start_progress" in line or "_stop_progress" in line:
                # 该行前面必须有 quiet 分支把它挡住
                assert "if not quiet:" in src[:src.index(line)][-400:], (
                    f"{fn.__name__} 第 {lineno} 行的进度条调用没有被 quiet 挡住: {line.strip()}")


def test_watch_button_access_is_defensive():
    """按钮建在 batch.py、消费在 batch_watchlist.py —— 跨文件裸属性访问是定时炸弹."""
    src = inspect.getsource(watchlist_tab)
    assert "app.btn_batch_refresh_watch.configure" not in src
    assert "_set_watch_button_state" in src


# ── 三级取价合并层 (S5) ──────────────────────────────────────────

def _cache(rows):
    return {"meta": {}, "rows": {r["bond_code"]: r for r in rows}}


def test_merge_layer_never_touches_disk(monkeypatch):
    """展示层不许隐式读真实磁盘.

    一旦它会读 data/watchlist_pricing_cache.json, 用例过不过就取决于"你上次开
    GUI 点没点刷新" —— 这正是 sync_cb_events 那批用例踩过的坑 (实测: 一次纯数据
    提交就让套件转红)。读盘只许发生在 load_price_cache_into, 由启动路径显式调。
    """
    def boom(*a, **kw):
        raise AssertionError("展示层读盘了")

    monkeypatch.setattr(watchlist_tab, "load_watchlist_pricing", boom)
    app = _watchlist_app(all_results=[], view_results=[],
                         watchlist=[{"bond_code": "123284.SZ"}])
    rows = watchlist_tab._watchlist_display_rows(app)
    assert rows[0]["_price_state"] == "unpriced"


def test_disk_cache_supplies_price_when_memory_is_empty():
    """开页立刻有数: 没跑过全池时理论价来自磁盘热缓存."""
    app = _watchlist_app(all_results=[], view_results=[],
                         watchlist=[{"bond_code": "123284.SZ"}])
    app._watchlist_price_cache = _cache([
        {"bond_code": "123284.SZ", "status": "ok", "theoretical_price": 128.93,
         "market_price": 120.0, "valuation_date": date(2026, 8, 26)},
    ])
    row = watchlist_tab._watchlist_display_rows(app, today=date(2026, 8, 26))[0]
    assert row["theoretical_price"] == 128.93
    assert row["_price_state"] == "ok"


def test_memory_beats_disk():
    """内存是"这次算的", 磁盘是"上次算的" —— 内存永远压过磁盘."""
    app = _watchlist_app(
        all_results=[{"bond_code": "123284.SZ", "status": "ok",
                      "theoretical_price": 130.0, "market_price": 121.0,
                      "valuation_date": date(2026, 8, 26)}],
        view_results=[], watchlist=[{"bond_code": "123284.SZ"}])
    app._watchlist_price_cache = _cache([
        {"bond_code": "123284.SZ", "status": "ok", "theoretical_price": 128.93,
         "market_price": 120.0, "valuation_date": date(2026, 8, 25)},
    ])
    row = watchlist_tab._watchlist_display_rows(app, today=date(2026, 8, 26))[0]
    assert row["theoretical_price"] == 130.0


def test_stale_unstamped_market_price_cannot_win():
    """watchlist.json 里那个无 as-of 戳的 market_price 不许在"今天没市价"时顶上来.

    实测三只在途新债今天 market_price 全是 None (还没上市), 而 entry 里可能留着
    扫新债时写下的旧价 —— 让它胜出就等于把几天前的价当成今天的。
    """
    app = _watchlist_app(all_results=[], view_results=[],
                         watchlist=[{"bond_code": "123284.SZ", "market_price": 119.9}])
    app._watchlist_price_cache = _cache([
        {"bond_code": "123284.SZ", "status": "ok", "theoretical_price": 128.93,
         "market_price": None, "valuation_date": date(2026, 8, 26)},
    ])
    row = watchlist_tab._watchlist_display_rows(app, today=date(2026, 8, 26))[0]
    assert row["market_price"] is None
    assert row["_price_state"] == "no_market"


def test_entry_price_survives_when_nothing_priced_it():
    """完全没有定价行时 entry 的值仍然显示 —— 有总比空好, 但状态标成 unpriced."""
    app = _watchlist_app(all_results=[], view_results=[],
                         watchlist=[{"bond_code": "123284.SZ", "market_price": 119.9}])
    row = watchlist_tab._watchlist_display_rows(app, today=date(2026, 8, 26))[0]
    assert row["market_price"] == 119.9
    assert row["_price_state"] == "unpriced"


@pytest.mark.parametrize("priced,expected,why", [
    (None, "unpriced", "从没算过"),
    ({"status": "failed"}, "failed", "算了但失败"),
    ({"status": "ok", "market_price": None, "valuation_date": date(2026, 8, 26)},
     "no_market", "算了但数据源没给市价 (118076.SH 那个 case)"),
    ({"status": "ok", "market_price": 108.0, "valuation_date": date(2026, 8, 25)},
     "stale", "昨天的价"),
    ({"status": "ok", "market_price": 108.0, "valuation_date": None},
     "stale", "不知道是哪天的价"),
    ({"status": "ok", "market_price": 108.0, "valuation_date": date(2026, 8, 26)},
     "ok", "今天算的、有市价"),
])
def test_price_state_tells_the_three_dashes_apart(priced, expected, why):
    """今天三种「—」在表上长得一模一样, 成因却完全不同 —— 分不开就没法判断要不要刷新."""
    app = _watchlist_app(all_results=[], view_results=[],
                         watchlist=[{"bond_code": "X"}])
    if priced is not None:
        app._watchlist_price_cache = _cache([{"bond_code": "X", **priced}])
    row = watchlist_tab._watchlist_display_rows(app, today=date(2026, 8, 26))[0]
    assert row["_price_state"] == expected, why


def test_new_columns_have_a_source_in_the_merge_whitelist():
    """守护: 主页新增列要用的字段都得先登记进 _PRICED_MERGE_FIELDS.

    漏掉不会报错, 只是那一列恒空 —— 而这一处没有别的守护能替你发现。
    """
    needed = {"event_flags", "relative_deviation", "cheapness_percentile",
              "cheapness_rank_total", "double_low", "quality_score",
              "valuation_date", "origin", "market_price_as_of", "market_price_source"}
    missing = needed - set(watchlist_tab._PRICED_MERGE_FIELDS)
    assert not missing, f"这些字段没进合并白名单, 对应列会恒空: {sorted(missing)}"


def test_market_price_is_not_in_the_generic_merge_list():
    """market_price 必须单独处理, 不能走"非 None 才覆盖"那条通用规则."""
    assert "market_price" not in watchlist_tab._PRICED_MERGE_FIELDS


# ── 陈旧即刷 (S6) ────────────────────────────────────────────────

def _stale_app(watchlist, cache_rows=(), all_results=()):
    app = _watchlist_app(all_results=list(all_results), view_results=[],
                         watchlist=list(watchlist))
    app._watchlist_price_cache = _cache(list(cache_rows))
    return app


def test_stale_codes_cover_every_non_ok_state():
    """ok 之外全部要重来 —— 隔夜的旧价、失败的行原本没有任何人管."""
    today = date(2026, 8, 26)
    app = _stale_app(
        watchlist=[{"bond_code": "FRESH"}, {"bond_code": "OLD"},
                   {"bond_code": "FAILED"}, {"bond_code": "NEVER"}],
        cache_rows=[
            {"bond_code": "FRESH", "status": "ok", "market_price": 108.0,
             "valuation_date": today},
            {"bond_code": "OLD", "status": "ok", "market_price": 108.0,
             "valuation_date": date(2026, 8, 25)},
            {"bond_code": "FAILED", "status": "failed"},
        ])
    rows = watchlist_tab._watchlist_display_rows(app, today=today)
    assert watchlist_tab.stale_watchlist_codes(app, rows=rows) == ["OLD", "FAILED", "NEVER"]


def test_listed_bond_missing_market_price_is_retried():
    """118076.SH 那个 case: status ok + 今天的估值日, 唯独市价是 None.

    只看"是不是今天算的"会让它当天永远不再重试, 市价与偏差两列空到明天。
    """
    today = date(2026, 8, 26)
    app = _stale_app(
        watchlist=[{"bond_code": "118076.SH", "is_tradable": True,
                    "trading_status": "tradable"}],
        cache_rows=[{"bond_code": "118076.SH", "status": "ok", "market_price": None,
                     "valuation_date": today}])
    rows = watchlist_tab._watchlist_display_rows(app, today=today)
    assert rows[0]["_price_state"] == "no_market"
    assert watchlist_tab.stale_watchlist_codes(app, rows=rows) == ["118076.SH"]


def test_pre_listing_new_bond_is_not_retried_forever():
    """还没上市的新债没有市价是天然状态, 不该每一轮都陪跑."""
    today = date(2026, 8, 26)
    app = _stale_app(
        watchlist=[{"bond_code": "123284.SZ", "is_tradable": False,
                    "trading_status": "pending"}],
        cache_rows=[{"bond_code": "123284.SZ", "status": "ok", "market_price": None,
                     "theoretical_price": 128.93, "valuation_date": today}])
    rows = watchlist_tab._watchlist_display_rows(app, today=today)
    assert rows[0]["_price_state"] == "no_market"
    assert watchlist_tab.stale_watchlist_codes(app, rows=rows) == []


def test_refresh_stale_debounces_quiet_rounds(monkeypatch):
    """启动 / 切页都会触发这一轮, 没有窗口就会在页签之间来回点时不停起后台定价."""
    calls = []
    monkeypatch.setattr(watchlist_tab, "_start_watchlist_pricing",
                        lambda app, codes, **kw: calls.append(codes) or True)
    app = _stale_app(watchlist=[{"bond_code": "X"}])

    assert watchlist_tab.refresh_stale_watchlist(app, quiet=True) == 1
    assert watchlist_tab.refresh_stale_watchlist(app, quiet=True) == 0   # 窗口内
    assert len(calls) == 1

    # 用户自己点的不受防抖限制
    assert watchlist_tab.refresh_stale_watchlist(app, quiet=False) == 1
    assert len(calls) == 2


def test_refresh_stale_does_not_stamp_when_round_did_not_start(monkeypatch):
    """被单飞/源不可用挡掉时不能记时间戳, 否则真正能跑的时候还要再等 15 分钟."""
    monkeypatch.setattr(watchlist_tab, "_start_watchlist_pricing",
                        lambda app, codes, **kw: False)
    app = _stale_app(watchlist=[{"bond_code": "X"}])
    assert watchlist_tab.refresh_stale_watchlist(app, quiet=True) == 0
    assert getattr(app, "_last_stale_refresh_at", None) is None


def test_refresh_stale_is_a_noop_when_everything_is_fresh(monkeypatch):
    monkeypatch.setattr(watchlist_tab, "_start_watchlist_pricing",
                        lambda app, codes, **kw: pytest.fail("不该起这一轮"))
    today = date(2026, 8, 26)
    app = _stale_app(
        watchlist=[{"bond_code": "X"}],
        cache_rows=[{"bond_code": "X", "status": "ok", "market_price": 108.0,
                     "valuation_date": today}])
    monkeypatch.setattr(watchlist_tab, "market_today", lambda: today)
    assert watchlist_tab.refresh_stale_watchlist(app, quiet=True) == 0


# ── star import 盲区的守卫 ───────────────────────────────────────

def test_star_import_exemption_only_shields_real_theme_names():
    """`from ..theme import *` 会把本该报 F821 (未定义名) 的错降级成 F405,
    而 pyproject 对 tabs/batch.py 与 tabs/batch_watchlist.py 豁免了 F405 ——
    于是那两个文件里**任何拼错的名字 ruff 都看不见**, 只在真实渲染那一行抛
    NameError, 而 GUI 在测试环境跑不起来。

    实测这不是假想: 本次搬页时删掉 `_auto_add_upcoming_to_watchlist` 的 import
    却留着两处调用, ruff 与 pytest 双双全绿。

    这条守卫把豁免从"忽略一切"收窄成"只忽略 theme 里真实导出的名字":
    逐个取出 ruff 报的 F405 名字, 不在 theme 命名空间里的一律算错。
    """
    import json
    import subprocess
    from pathlib import Path
    import convertible_bond
    from convertible_bond.gui import theme

    targets = [
        Path(convertible_bond.__file__).parent / "gui" / "tabs" / "batch.py",
        Path(convertible_bond.__file__).parent / "gui" / "tabs" / "batch_watchlist.py",
    ]
    proc = subprocess.run(
        ["ruff", "check", "--isolated", "--select", "F", "--output-format", "json",
         *[str(p) for p in targets]],
        capture_output=True, text=True,
    )
    if proc.returncode not in (0, 1):
        pytest.skip(f"ruff 不可用: {proc.stderr[:200]}")

    theme_names = set(dir(theme))
    unknown = []
    for item in json.loads(proc.stdout or "[]"):
        if item.get("code") != "F405":
            continue
        # message 形如: `X` may be undefined, or defined from star imports: ...
        message = item.get("message", "")
        name = message.split("`")[1] if "`" in message else ""
        if name and name not in theme_names:
            unknown.append(f"{Path(item['filename']).name}:{item['location']['row']}: {name}")
    assert not unknown, (
        "以下名字既不是 theme 的导出、也没有在本模块定义 —— star import 豁免正在替它们打掩护, "
        "运行期会抛 NameError:\n  " + "\n  ".join(unknown))


# ── 列换血 / 数据列 / 涨跌基准 (S8) ─────────────────────────────

def test_column_definition_is_a_single_source_of_truth():
    """表头 / 列宽 / 拉伸权重三者的键集必须完全一致.

    权重表按**表头文本**索引: 删列留死条目、加列查不到会走 batch_common 的默认 1.0
    (与"名称"同级), 窗口一拉宽就把富余宽度均摊给窄数字列。不报错、不红测试。
    """
    headers, widths = watchlist_tab.watchlist_columns()
    assert len(headers) == len(widths)
    assert set(headers) == set(watchlist_tab._WATCHLIST_COL_STRETCH_WEIGHTS)
    assert len(set(headers)) == len(headers), "表头有重复"


def test_change_column_header_names_the_baseline_date():
    """表头不说清是跟哪天比, 这一列就是个没法核对的数字 ——
    周一/长假后它其实是 3 天涨跌。"""
    assert watchlist_tab.change_column_label(None) == watchlist_tab.CHANGE_COLUMN_KEY
    label = watchlist_tab.change_column_label({"valuation_date": date(2026, 8, 25)})
    assert label == "涨跌 vs 08-25"
    headers, _ = watchlist_tab.watchlist_columns(label)
    assert label in headers and watchlist_tab.CHANGE_COLUMN_KEY not in headers


def test_dropped_columns_are_really_gone():
    """按用户决策: 砍「可信」留「敏感性」; 上市日/可交易日/距交易/机会分折进别处;
    「加入时偏差」「市价变化」锚的是加入瞬间, 与选定的"vs 上一交易日"口径不是一回事。"""
    weights = set(watchlist_tab._WATCHLIST_COL_STRETCH_WEIGHTS)
    for gone in ("可信", "上市日", "可交易日", "距交易", "机会分",
                 "加入时偏差(%)", "市价变化(%)", "状态"):
        assert gone not in weights, f"{gone} 应该已经砍掉"
    assert "敏感性" in weights and "数据" in weights


@pytest.mark.parametrize("state,extra,expected", [
    ("ok", {}, "✓ 今日"),
    ("stale", {}, "昨日"),
    ("no_market", {}, "无市价"),
    ("failed", {"status": "provider error: timeout"}, "失败 · provider error: ti"),
    ("unpriced", {}, "未定价"),
])
def test_row_data_label(state, extra, expected):
    entry = {"bond_code": "X", "_price_state": state, **extra}
    assert watchlist_tab._row_data_label(entry) == expected


def test_data_label_flags_a_price_older_than_the_valuation_date():
    """停牌/节假日会让最近一笔收盘价落在几天前 —— 那和"今天的价"不是一回事."""
    entry = {"bond_code": "X", "_price_state": "ok",
             "valuation_date": date(2026, 8, 26),
             "market_price_as_of": date(2026, 8, 21),
             "market_price_source": "history"}
    assert watchlist_tab._row_data_label(entry) == "✓ 今日 · 价 08-21"


def test_data_label_flags_the_unstamped_fallback():
    """terms.close 兜底那一档没有 as-of, 可以任意旧 (日升转债库里的是 2021 年的值)."""
    entry = {"bond_code": "X", "_price_state": "ok",
             "market_price_source": "terms_close"}
    assert watchlist_tab._row_data_label(entry) == "✓ 今日 · 无戳"


def test_unpriced_label_uses_pool_exclusion_reason_not_view_reason():
    """「已发行未上市」这类文案来自 batch_pricing_exclusion_reason.

    接 view_exclusion_reason 是错的: 它返回视图口径文案 (「相对市场中位 +17.9pp,
    未便宜过 5pp」), 而且要收一个 view 参数 —— 主页根本没有视图选择器。
    """
    class _Cache:
        def get(self, code):
            return object()

    calls = []

    def fake_reason(code, terms, **kw):
        calls.append(code)
        return "已发行未上市"

    import convertible_bond.gui.tabs.batch_watchlist as mod
    original = mod.batch_pricing_exclusion_reason
    mod.batch_pricing_exclusion_reason = fake_reason
    try:
        label = mod._row_data_label({"bond_code": "123284.SZ", "_price_state": "unpriced"},
                                    terms_cache=_Cache())
    finally:
        mod.batch_pricing_exclusion_reason = original
    assert label == "未定价 · 已发行未上市"
    assert calls == ["123284.SZ"]


@pytest.mark.parametrize("cur,base,expected", [
    (110.0, 100.0, 10.0),
    (90.0, 100.0, -10.0),
    (100.0, None, None),
    (None, 100.0, None),
    (100.0, 0.0, None),
    (float("nan"), 100.0, None),
])
def test_pct_change_returns_none_not_zero_when_there_is_no_base(cur, base, expected):
    """没有基准就返回 None 显示「—」, **不是 0.0** —— "没有基准"和"确实没变"
    必须分得开, 否则用户会把"我昨天没开过 GUI"读成"今天没动"。"""
    got = watchlist_tab._pct_change(cur, base)
    if expected is None:
        assert got is None
    else:
        assert got == pytest.approx(expected)


# ── 事件区双向化 (S9) ────────────────────────────────────────────

class _FakeEventStore:
    def __init__(self, by_code):
        self._by_code = by_code

    def list_events(self, bond_code=None):
        return self._by_code.get(bond_code, [])


class _BannerEv:
    def __init__(self, event_type, day):
        self.event_type = event_type
        self.event_date = day
        self.effective_start = None
        self.effective_end = None


class _BannerApp:
    def __init__(self, watchlist, store, pool=()):
        self._batch_watchlist = [{"bond_code": c, "bond_name": c} for c in watchlist]
        self._batch_all_results = [{"bond_code": c, "bond_name": c} for c in pool]
        self.event_store = store
        self.v_batch_events_banner = _StrVar()
        self.lbl_batch_events_banner = _FakeLabel()
        self._batch_events_banner_full = []


class _StrVar:
    def __init__(self):
        self._v = ""

    def get(self):
        return self._v

    def set(self, v):
        self._v = v


class _FakeLabel:
    def __init__(self):
        self.shown = None

    def grid(self):
        self.shown = True

    def grid_remove(self):
        self.shown = False


def test_events_banner_shows_an_explicit_empty_state(monkeypatch):
    """空是**常态**不是异常 —— 实测今天关注池近 7 天与未来 30 天都是 0 件.

    藏起控件会重演「低估候选默认打开是空表、用户以为坏了」那次: 一个消失的控件
    和一个坏掉的控件长得一模一样。
    """
    monkeypatch.setattr(watchlist_tab, "market_today", lambda: date(2026, 8, 26))
    app = _BannerApp(["A.SH", "B.SH"], _FakeEventStore({}))
    watchlist_tab._refresh_events_banner(app)
    assert app.lbl_batch_events_banner.shown is True, "空态不许 grid_remove"
    assert "已扫 2 只" in app.v_batch_events_banner.get()
    assert "无日程事件" in app.v_batch_events_banner.get()


def test_events_banner_splits_past_and_future(monkeypatch):
    today = date(2026, 8, 26)
    monkeypatch.setattr(watchlist_tab, "market_today", lambda: today)
    store = _FakeEventStore({
        "A.SH": [_BannerEv("call_redemption", today - timedelta(days=3))],
        "B.SH": [_BannerEv("putback", today + timedelta(days=10))],
    })
    watchlist_tab._refresh_events_banner(_app := _BannerApp(["A.SH", "B.SH"], store))
    text = _app.v_batch_events_banner.get()
    assert "近 7 天 1 件" in text
    assert "未来 30 天 1 件" in text
    # 明细一条不少地留给弹窗, 过去的排在前面 (它们是"已经发生了而你可能没看见")
    assert len(_app._batch_events_banner_full) == 2
    assert _app._batch_events_banner_full[0][0] == "A.SH"


def test_events_banner_keeps_the_pool_wide_count(monkeypatch):
    """全池那条线索没被丢掉, 只是从"铺满横幅"降级成"末尾一句计数"。

    原来的理由 —— "横幅真正的用处是告诉你**还不知道的那些**" —— 依然成立。
    """
    today = date(2026, 8, 26)
    monkeypatch.setattr(watchlist_tab, "market_today", lambda: today)
    store = _FakeEventStore({
        "P1.SH": [_BannerEv("call_redemption", today + timedelta(days=5))],
        "P2.SH": [_BannerEv("putback", today + timedelta(days=6))],
    })
    app = _BannerApp(["A.SH"], store, pool=["P1.SH", "P2.SH"])
    watchlist_tab._refresh_events_banner(app)
    text = app.v_batch_events_banner.get()
    assert "全池另有 2 件" in text
    assert "无日程事件" in text          # 关注池自己那一半仍然显式说空


def test_events_banner_survives_a_missing_store():
    app = _BannerApp(["A.SH"], None)
    app.event_store = None
    watchlist_tab._refresh_events_banner(app)
    assert app.lbl_batch_events_banner.shown is True
    assert app.v_batch_events_banner.get() == "事件表未载入"
