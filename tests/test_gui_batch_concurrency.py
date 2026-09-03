"""两个后台定价任务共享一个 app 时的守护.

批量页的「🔄 刷新重算」(tabs/batch) 与关注池主页的「⚡ 关注池重算」
(tabs/batch_watchlist) 各起一条后台线程,
而它们共用三样东西: ``_batch_all_results`` (两边都读-改-写)、进度条与 ``v_status``、
以及那条"新债窄同步"的前置。三样都曾经没有任何协调, 而失效形态一律是**静默**的 ——
两边的状态栏都报成功, 表里却只剩其中一份结果。

这里的用例都不建 Tk root: 进度与互斥那四个方法只碰普通属性, 所以 ``_App`` 直接**借用
真 ``CBPricerApp`` 的实现**。抄一份等价实现就等于测一个自己写的假货。
"""
from __future__ import annotations

import inspect
import threading

import pytest

from convertible_bond.gui import app as app_mod
from convertible_bond.gui.tabs import batch as batch_tab
from convertible_bond.gui.tabs import batch_watchlist as watchlist_tab


# ── 替身 ────────────────────────────────────────────────────────

class _Var:
    """StringVar 替身; 留 history 是为了断言"被挡住时到底说了什么"."""

    def __init__(self, value: str = ""):
        self.value = value
        self.history: list[str] = []

    def get(self) -> str:
        return self.value

    def set(self, value) -> None:
        self.value = value
        self.history.append(value)


class _Bar:
    def __init__(self):
        self.starts = 0
        self.stops = 0

    def start(self):
        self.starts += 1

    def stop(self):
        self.stops += 1

    def set(self, _value):
        pass


class _Button:
    def __init__(self):
        self.states: list[str] = []

    def configure(self, **kwargs):
        if "state" in kwargs:
            self.states.append(kwargs["state"])


class _App:
    """够跑通这几条路径的最小 app。

    进度条与互斥道的五个方法直接绑真实现 —— 它们不碰 Tk。
    """

    _start_progress = app_mod.CBPricerApp._start_progress
    _tick_progress = app_mod.CBPricerApp._tick_progress
    _stop_progress = app_mod.CBPricerApp._stop_progress
    _on_error = app_mod.CBPricerApp._on_error
    _acquire_pricing_slot = app_mod.CBPricerApp._acquire_pricing_slot
    _release_pricing_slot = app_mod.CBPricerApp._release_pricing_slot

    def __init__(self, source: str = "akshare"):
        self._animating = False
        self._progress_tasks: list[str] = []
        self._pricing_slot = None
        self._pricing_slot_lock = threading.Lock()
        self.progress_bar = _Bar()
        self.v_status = _Var()
        self.v_batch_status = _Var()
        self.v_watchlist_status = _Var()
        self.after_calls: list[tuple[int, object]] = []

        self.v_batch_source = _Var(source)
        self.v_r = _Var("2.2")
        self.v_spread = _Var("3.0")
        self.v_p_down = _Var("25")
        self.v_dk = _Var("5")
        self.v_M = _Var("300")
        self.v_N = _Var("1000")
        self.v_vol_window = _Var("21日")
        self.v_batch_min_balance = _Var("")
        self.v_batch_min_rating = _Var("AA-")
        self.v_batch_min_turnover = _Var("")

        # _on_error 会碰的那几样 (它与进度计数共用一条路)
        self.v_ref_info = _Var()
        self.v_bond_title = _Var()
        self.v_result = _Var()
        self.lbl_result = _Button()

        self.btn_batch_run = _Button()
        self._batch_watchlist: list[dict] = []
        self._batch_results: list[dict] = []
        self._batch_all_results: list[dict] = []
        self._batch_upcoming_results: list[dict] = []
        self._watchlist_pricing_running = False
        self._new_issue_sync_running = False

    def after(self, delay, fn):
        self.after_calls.append((delay, fn))


class _NoopThread:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    def start(self):
        pass


def _record_threads(monkeypatch, module) -> list:
    started: list = []

    def factory(*args, **kwargs):
        started.append(kwargs.get("target") or (args[0] if args else None))
        return _NoopThread()

    monkeypatch.setattr(module.threading, "Thread", factory)
    return started


def _stub_batch_entry(monkeypatch, *, codes=("128044.SZ",)):
    """把 ``_run_batch_now`` 的取数与关注池副作用摘掉, 只留下起线程那一段决策."""
    monkeypatch.setattr(batch_tab, "split_batch_codes_from_cache",
                        lambda *a, **kw: (list(codes), []))
    monkeypatch.setattr(batch_tab, "_auto_add_upcoming_to_watchlist",
                        lambda *a, **kw: None)


# ── ① 互斥道本身 (真 CBPricerApp 的方法) ─────────────────────────

def test_the_app_really_provides_the_pricing_slot():
    """两个 tab 是用 getattr 拿这两个方法的 —— 真类上没有的话互斥会静默消失."""
    for name in ("_acquire_pricing_slot", "_release_pricing_slot"):
        assert callable(getattr(app_mod.CBPricerApp, name, None)), f"CBPricerApp 缺 {name}"


def test_pricing_slot_admits_one_and_names_the_occupant():
    app = _App()

    assert app._acquire_pricing_slot("批量页的「🔄 刷新重算」") is None
    # 被挡住时要说得出"在等谁" —— 状态栏靠它写出那句「⏳ 「…」正在进行」
    assert app._acquire_pricing_slot("关注池主页的「⚡ 关注池重算」") == "批量页的「🔄 刷新重算」"

    app._release_pricing_slot("批量页的「🔄 刷新重算」")
    assert app._acquire_pricing_slot("关注池主页的「⚡ 关注池重算」") is None


def test_only_the_occupant_can_release_the_slot():
    """迟到的 finally 不许放掉别人的锁 —— 那等于互斥在最忙的时候失效."""
    app = _App()
    app._acquire_pricing_slot("批量页的「🔄 刷新重算」")

    app._release_pricing_slot("关注池主页的「⚡ 关注池重算」")

    assert app._acquire_pricing_slot("关注池主页的「⚡ 关注池重算」") == "批量页的「🔄 刷新重算」"


def test_slot_names_say_which_page_and_which_button():
    """被挡住的那一次要靠占用者名字指路 —— 两个按钮长在不同页上.

    只报按钮名 (「⚡ 关注池重算」) 的话, 站在批量页的用户在**这一页**上找不到它;
    与 ``WATCH_REFRESH_LABEL`` 那次"消息指着一个找不到的按钮"是同一个形状。
    """
    assert watchlist_tab.PRICING_SLOT_WATCHLIST == "关注池主页的「⚡ 关注池重算」"
    assert watchlist_tab.PRICING_SLOT_BATCH == "批量页的「🔄 刷新重算」"


def test_batch_rerun_button_text_comes_from_the_constant():
    """状态栏引的就是这个常量 —— 按钮改名而常量没改, 消息就指向一个不存在的按钮."""
    assert "text=BATCH_RERUN_LABEL" in inspect.getsource(batch_tab)
    # 常量的值必须真是按钮上那几个字 (字面量, 不从被测常量推导)
    assert watchlist_tab.BATCH_RERUN_LABEL == "🔄 刷新重算"


# ── ② 两个入口互斥 (R1-31) ──────────────────────────────────────

def test_watchlist_repricing_is_refused_while_the_full_pool_run_holds_the_slot(monkeypatch):
    """⚡ 那一轮的 merge 基于它读到的旧 _batch_all_results, 会把全池结果整份盖掉."""
    app = _App()
    app._acquire_pricing_slot(watchlist_tab.PRICING_SLOT_BATCH)
    started = _record_threads(monkeypatch, watchlist_tab)

    assert watchlist_tab._start_watchlist_pricing(app, ["123284.SZ"]) is False

    assert started == [], "全池正在跑, 不许再起一条关注池定价线程"
    assert watchlist_tab.BATCH_RERUN_LABEL in app.v_watchlist_status.value, (
        f"被挡住时要说清在等谁: {app.v_watchlist_status.value!r}")


def test_full_pool_rerun_is_refused_while_the_watchlist_worker_holds_the_slot(monkeypatch):
    """反方向同样要挡 —— btn_batch_run 的 disable 挡不住它, 那个按钮在另一页上."""
    app = _App()
    app._acquire_pricing_slot(watchlist_tab.PRICING_SLOT_WATCHLIST)
    _stub_batch_entry(monkeypatch)
    started = _record_threads(monkeypatch, batch_tab)

    batch_tab._run_batch_now(app)

    assert started == [], "关注池正在跑, 不许再起一条全池定价线程"
    assert watchlist_tab.WATCH_REFRESH_LABEL in app.v_batch_status.value, (
        f"被挡住时要说清在等谁: {app.v_batch_status.value!r}")
    assert app.btn_batch_run.states == [], "被挡住的那一次不该把按钮灰掉"


def test_the_two_entry_points_cannot_both_be_running(monkeypatch):
    """端到端: 真的点了批量页的「🔄 刷新重算」之后, 「⚡ 关注池重算」必须点不动."""
    app = _App()
    _stub_batch_entry(monkeypatch)
    batch_threads = _record_threads(monkeypatch, batch_tab)

    batch_tab._run_batch_now(app)
    assert len(batch_threads) == 1, "全池那一轮本身要起得来"

    watch_threads = _record_threads(monkeypatch, watchlist_tab)
    assert watchlist_tab._start_watchlist_pricing(app, ["123284.SZ"]) is False
    assert watch_threads == []


def test_full_pool_run_greys_out_the_watchlist_button(monkeypatch):
    """只靠状态栏拒绝的话, 用户点了才知道点不动 —— 而这一组缺陷的共同形状就是它."""
    app = _App()
    app.btn_batch_refresh_watch = _Button()
    _stub_batch_entry(monkeypatch)
    _record_threads(monkeypatch, batch_tab)

    batch_tab._run_batch_now(app)

    assert app.btn_batch_refresh_watch.states == ["disabled"]


def test_a_refused_watchlist_start_hands_the_slot_back():
    """占了却没起成 = 互斥道被一次失败的点击永久占住, 此后两页的定价入口全静默失效."""
    app = _App(source="CSV")          # quiet 那一轮不许为 CSV 弹模态目录框 → 直接不起

    assert watchlist_tab._start_watchlist_pricing(app, ["123284.SZ"], quiet=True) is False

    assert app._acquire_pricing_slot("探针") is None, "互斥道没还回来"


def test_a_failed_batch_thread_start_hands_the_slot_back(monkeypatch):
    app = _App()
    _stub_batch_entry(monkeypatch)

    def boom(*_a, **_kw):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(batch_tab.threading, "Thread", boom)
    with pytest.raises(RuntimeError):
        batch_tab._run_batch_now(app)

    assert app._acquire_pricing_slot("探针") is None, "互斥道没还回来"
    assert app.btn_batch_run.states == ["disabled", "normal"], "按钮也要恢复"


def test_a_crash_between_acquiring_and_starting_hands_the_slot_back(monkeypatch):
    """占了却没起成 = 互斥道被永久占住, 此后**两页**的定价入口全部静默失效。

    起线程那一句不是唯一会抛的地方: 占锁之后紧跟着的是 ``app.btn_batch_run.configure``
    这样一次裸属性访问, 而 AGENTS 记过同一个形状 (按钮被搬走/改名 → finally 永远不执行)。
    """
    app = _App()
    _stub_batch_entry(monkeypatch)
    started = _record_threads(monkeypatch, batch_tab)

    class _BrokenButton:
        def configure(self, **_kwargs):
            raise AttributeError("btn_batch_run 被搬走了")

    app.btn_batch_run = _BrokenButton()

    with pytest.raises(AttributeError):
        batch_tab._run_batch_now(app)

    assert started == [], "线程压根没起来"
    assert app._acquire_pricing_slot("探针") is None, "互斥道没还回来"
    assert app._animating is False, "进度条根本没开过, 不许留下计数"


def test_watchlist_single_flight_refusal_is_not_silent():
    """三个入口的单飞检查原先只 return False —— "点了没反应"与"点了但坏了"长得一样."""
    app = _App()
    app._watchlist_pricing_running = True

    assert watchlist_tab._start_watchlist_pricing(app, ["123284.SZ"]) is False

    assert app.v_watchlist_status.value, "被单飞挡掉时状态栏一个字都没写"


def test_quiet_self_heal_stays_silent_when_refused():
    """启动自愈那一轮没人要过, 报一句只会把别人的状态顶掉."""
    app = _App()
    app._watchlist_pricing_running = True
    app.v_watchlist_status.set("别人的消息")

    assert watchlist_tab._start_watchlist_pricing(app, ["123284.SZ"], quiet=True) is False

    assert app.v_watchlist_status.value == "别人的消息"


# ── ③ worker 不许写视图子集变量 (R1-32) ─────────────────────────

def _pool_rows():
    return [
        {"bond_code": "128044.SZ", "bond_name": "岭南转债", "status": "ok",
         "theoretical_price": 105.0, "market_price": 100.0, "deviation": -0.05},
        {"bond_code": "123281.SZ", "bond_name": "中仑转债", "status": "ok",
         "theoretical_price": 110.0, "market_price": 130.0, "deviation": 0.18},
    ]


def _stub_batch_worker_deps(monkeypatch, rows, *, priced=None):
    class _Provider:
        name = "akshare"

        def get_risk_free_rate(self, _day):
            return None

    monkeypatch.setattr(batch_tab, "build_batch_provider", lambda *a, **kw: _Provider())
    monkeypatch.setattr(batch_tab, "batch_price_from_provider_threaded",
                        lambda *a, **kw: list(priced if priced is not None else rows))
    monkeypatch.setattr(batch_tab, "save_batch_results_cache", lambda *a, **kw: "cache.json")
    monkeypatch.setattr(batch_tab, "_record_valuation_history", lambda *a, **kw: None)


def test_worker_never_writes_the_view_subset_variable(monkeypatch):
    """全池要写 ``_batch_all_results``。

    ``_batch_results`` 是**视图过滤后**的子表, 唯一的预期写入方是
    ``_render_batch_views``; 而 ⭐「加入关注池」用的 iid 就是它的整数下标。worker
    在渲染之前塞一份全池进去, 那段窗口里表上画的还是上一次那几十行的视图, 按下标
    取到的却是另一只债 —— 不报错, 加进关注池的就是错的那只。
    """
    rows = _pool_rows()
    _stub_batch_worker_deps(monkeypatch, rows)
    app = _App()
    view_subset = [dict(rows[0])]
    app._batch_results = view_subset

    batch_tab._batch_worker(app, ["128044.SZ", "123281.SZ"], [], "akshare", None, {})

    assert app._batch_results is view_subset, "worker 动了视图子集变量"
    assert {r["bond_code"] for r in app._batch_all_results} == {"128044.SZ", "123281.SZ"}


def test_worker_all_failed_path_also_writes_the_full_pool_variable(monkeypatch):
    """全失败那一条分支是同一个坑的第二份拷贝."""
    rows = [dict(r, status="failed") for r in _pool_rows()]
    _stub_batch_worker_deps(monkeypatch, rows)
    monkeypatch.setattr(batch_tab, "_load_successful_result_cache", lambda _app: None)
    app = _App()
    view_subset: list[dict] = []
    app._batch_results = view_subset

    batch_tab._batch_worker(app, ["128044.SZ", "123281.SZ"], [], "akshare", None, {})

    assert app._batch_results is view_subset, "全失败分支动了视图子集变量"
    assert len(app._batch_all_results) == 2


def test_worker_hands_the_slot_back_on_the_main_thread_and_last(monkeypatch):
    """放锁要排在 after 队列**最后**, 而不是在 worker 线程上直接放。

    直接放的话, 下一个任务可以在这条线程还没跑完 finally 的瞬间起来, 而排在队里的
    ``_stop_progress`` 随后落到它头上, 把刚开始的那一轮的进度条关掉。
    """
    _stub_batch_worker_deps(monkeypatch, _pool_rows())
    app = _App()
    app._acquire_pricing_slot(watchlist_tab.PRICING_SLOT_BATCH)

    batch_tab._batch_worker(app, ["128044.SZ"], [], "akshare", None, {})

    assert app._pricing_slot == watchlist_tab.PRICING_SLOT_BATCH, "在 worker 线程上就把锁放了"
    names = [getattr(fn, "__name__", "<lambda>") for _delay, fn in app.after_calls]
    assert "_stop_progress" in names, "finally 没登记停进度条"
    app.after_calls[-1][1]()                       # 队尾那一条必须就是放锁
    assert app._pricing_slot is None, "放锁没排在队尾 (或者压根没登记)"


# ── ④ 共享进度条的引用计数 (R1-36) ──────────────────────────────

def test_progress_bar_survives_the_first_of_two_tasks_finishing():
    """两个后台任务并发时, 先结束的那个不许把还在跑的那个的进度条关掉."""
    app = _App()
    app._start_progress("全量定价 311 只")
    app._start_progress("定价关注池 5 只")

    app._stop_progress()

    assert app._animating is True, "还有任务在跑, 进度条被提前关了"
    assert app.progress_bar.stops == 0

    app._stop_progress()

    assert app._animating is False
    assert app.progress_bar.stops == 1


def test_remaining_task_gets_the_label_back():
    app = _App()
    app._start_progress("全量定价 311 只")
    app._start_progress("定价关注池 5 只")

    app._stop_progress()

    assert app._anim_base == "全量定价 311 只"


def test_a_second_start_does_not_double_the_animation_chain():
    """重新 tick 一次会让 after(400) 链翻倍, 点几次就越转越快."""
    app = _App()
    app._start_progress("A")
    ticks = sum(1 for delay, _fn in app.after_calls if delay == 400)

    app._start_progress("B")

    assert sum(1 for delay, _fn in app.after_calls if delay == 400) == ticks == 1


def test_an_unbalanced_stop_still_stops_the_bar():
    """``_on_error`` 之后 finally 往往还会再停一次 —— 多出来的那次不许把条挂住."""
    app = _App()
    app._start_progress("A")
    app._stop_progress()
    app._stop_progress()

    assert app._animating is False
    assert app.progress_bar.stops == 2


def test_stop_without_start_is_safe():
    app = _App()
    app._stop_progress()

    assert app._animating is False


def test_on_error_does_not_eat_another_tasks_progress_slot():
    """六个 worker 的 except 里都是 ``after(0, self._on_error, ...)`` 而 finally 里
    还有一次 ``_stop_progress`` —— 一次 start 对两次 stop。只有一个任务时两次都落在
    空栈上看不出来; 两个任务时那多出来的一次会把**另一个**任务的计数吃掉。
    """
    app = _App()
    app._start_progress("全量定价 311 只")
    app._start_progress("正在计算理论价格")

    app._on_error("计算失败: boom", show_dialog=False)
    app._stop_progress()                      # 出错那个任务自己的 finally

    assert app._animating is True, "另一个任务还在跑, 它的进度条被出错的那个关掉了"


def test_every_on_error_call_site_still_stops_progress_itself():
    """``_on_error`` 不再停进度, 所以每个调用点自己必须停.

    走 AST 而不是扫文本: 断言的是"这个函数里既提到 ``_on_error`` 也提到
    ``_stop_progress``"这个**结构**, 注释怎么写都不影响。少一个的表现是进度条永远转下去,
    而没有任何异常。
    """
    import ast
    from pathlib import Path

    import convertible_bond

    gui_root = Path(convertible_bond.__file__).parent / "gui"
    offenders = []
    for path in sorted(gui_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name == "_on_error":
                continue
            attrs = {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}
            if "_on_error" in attrs and "_stop_progress" not in attrs:
                offenders.append(f"{path.name}:{node.lineno}: {node.name}")
    assert not offenders, (
        "这些函数把错误交给 _on_error 却没有自己停进度条 —— _on_error 刻意不停 "
        "(它会吃掉另一个还在跑的任务的计数), 请在 finally 里补 "
        "`self.after(0, self._stop_progress)`:\n  " + "\n  ".join(offenders))


def test_error_text_is_written_after_the_modal(monkeypatch):
    """``showerror`` 是模态, 它自己的事件循环里那条 400ms 的动画 tick 照样在跑.

    ``_on_error`` 现在不停进度了, 所以状态行必须写在弹框之后 —— 先写就被 tick 盖掉,
    用户关掉弹框看到的是一句"正在计算…"而不是错误原因。
    """
    app = _App()
    app._start_progress("正在计算理论价格")
    seen = []
    monkeypatch.setattr(app_mod.messagebox, "showerror",
                        lambda *a, **kw: seen.append(app.v_status.value))

    app._on_error("计算失败: boom")

    assert seen == ["正在计算理论价格"], f"错误文案写在了弹框之前: {seen!r}"
    assert "计算失败: boom" in app.v_status.value


# ── ⑤ 被丢掉的第二次窄同步要说话 (R1-35) ────────────────────────

def test_a_dropped_new_issue_sync_reports_itself(monkeypatch):
    app = _App()
    app._new_issue_sync_running = True
    seen: list = []
    _record_threads(monkeypatch, watchlist_tab)

    assert watchlist_tab.run_new_issue_sync_async(app, then=seen.append) is False
    assert seen == [], "并发那一次仍然不许真的跑"


def test_scan_button_says_why_nothing_happened(monkeypatch):
    """「🆕 扫新债」连点两下, 第二下原先是**什么都不发生**: 不写状态、不排队、按钮没灰."""
    app = _App()
    app._new_issue_sync_running = True
    _record_threads(monkeypatch, watchlist_tab)

    watchlist_tab._refresh_watchlist_with_upcoming(app)

    assert app.v_watchlist_status.value, "第二下点击一个字都没说"


def test_batch_rerun_says_why_nothing_happened(monkeypatch):
    """被丢掉的是整轮全池定价, 而用户会一直等一个永远不会来的结果.

    状态写在**批量页** —— 按钮长在那一页上。
    """
    app = _App()
    app._new_issue_sync_running = True
    _record_threads(monkeypatch, batch_tab)

    batch_tab._run_batch(app)

    assert app.v_batch_status.value, "批量页一个字都没说"
    assert app.v_watchlist_status.history == [], "不许写到另一页的状态行上"
