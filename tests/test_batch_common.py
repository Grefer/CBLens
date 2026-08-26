from dataclasses import dataclass
from datetime import date, timedelta

import inspect

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


def test_banner_scan_codes_cover_the_whole_main_pool():
    from convertible_bond.gui.tabs.batch_watchlist import _banner_scan_codes

    class _App:
        _batch_watchlist = [{"bond_code": "W.SH"}]
        _batch_all_results = [{"bond_code": "M1.SH"}, {"bond_code": "M2.SH"}, {}]

    assert _banner_scan_codes(_App()) == {"W.SH", "M1.SH", "M2.SH"}


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


def test_cache_load_repairs_missing_new_bond_prices():
    src = inspect.getsource(batch_tab._load_result_cache)
    assert "price_unpriced_new_bonds" in src
    # 这一轮不是用户发起的 (启动 80ms 后自动跑): 失败只写状态栏, 不许糊一个模态错误框
    assert "quiet=True" in src


def test_watchlist_pricing_is_single_flight():
    """三个入口 (⚡重算 / 扫新债 / 缓存加载补价) 并发跑会互相覆盖 new_upcoming."""
    app = _watchlist_app(all_results=[], view_results=[],
                         watchlist=[{"bond_code": "123284.SZ", "trading_status": "pending"}])
    app._watchlist_pricing_running = True

    assert watchlist_tab._start_watchlist_pricing(app, ["123284.SZ"]) is False
    assert watchlist_tab.price_unpriced_new_bonds(app) == 0
