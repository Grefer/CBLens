"""GUI 控件生命周期 / 线程边界的守护。

三件事各有一次真实事故形态, 共同点是**不报错也不变红**:

1. 池同步弹窗的后台线程往用户已经关掉的窗口里写 —— `self.after` 排在**主窗口**上,
   窗口销毁撤不掉已排队的回调, 于是每一行输出都撞上一个不存在的 Tcl 命令。
2. 定价 worker 在线程里现读 Tk 变量 —— 实测本机 Tcl 8.6.15 是 threaded 版,
   跨线程 `.get()` 被 marshal 回主线程**正常返回**, 拿到的是用户改之后的新值。
3. 定价页的 ⭐ 只重画关注池表, 不刷主页事件横幅 —— 横幅拿不到控件时直接 return。

这些用例都不建真的 Tk root (macOS 上会悄悄成功、CI 上抛 TclError), 走 stub。
"""
from __future__ import annotations

import ast
import inspect
import subprocess
import types
from pathlib import Path

import pytest

from convertible_bond.gui.controllers import wind_sync


# ───────────────────────── R1-33: 关窗之后的排队回调 ─────────────────────────

class _StubTclError(Exception):
    """站 `tkinter.TclError` 的位: 对已销毁控件的任何操作都抛它。"""


class _StubWidget:
    """只实现 `_run_pool_sync` 用到的那几个方法, 销毁语义与真 Tk 一致。

    实测 Tk 8.6.15: `destroy()` 之后 `winfo_exists()` 安全返回 0, 而 `insert` /
    `configure` 抛 `TclError: invalid command name ".!toplevel.!text"` —— 这里逐字
    复刻这个不对称, 否则守护测的就不是真实失效形状。
    """

    def __init__(self, master=None, **kwargs):
        self._alive = True
        self._kids: list[_StubWidget] = []
        self.inserted: list[str] = []
        self.configured: list[dict] = []
        self.protocols: dict = {}
        self.kwargs = kwargs
        if isinstance(master, _StubWidget):
            master._kids.append(self)

    # -- 生命周期 --
    def destroy(self):
        for kid in self._kids:
            kid.destroy()
        self._alive = False

    def winfo_exists(self):
        return 1 if self._alive else 0

    def _require_alive(self):
        if not self._alive:
            raise _StubTclError('invalid command name ".!ctktoplevel.!ctktextbox"')

    # -- 会被后台线程碰到的那几个 --
    def insert(self, _index, text):
        self._require_alive()
        self.inserted.append(text)

    def see(self, _index):
        self._require_alive()

    def configure(self, **kwargs):
        self._require_alive()
        self.configured.append(kwargs)

    # -- 建窗时用到的无副作用方法 --
    def pack(self, **_kwargs):
        pass

    def title(self, *_a):
        pass

    def geometry(self, *_a):
        pass

    def transient(self, *_a):
        pass

    def protocol(self, name, func):
        self.protocols[name] = func


class _StubVar:
    def __init__(self, value=""):
        self._value = value

    def get(self):
        return self._value

    def set(self, value):
        self._value = value


class _StubProc:
    def __init__(self, lines):
        self.stdout = iter(lines)
        self.pid = 4242
        self.terminated = False

    def poll(self):
        return None if not self.terminated else 0

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        self.terminated = True


class _StubApp:
    """`self.after` 只入队不执行 —— 真实时序就是"回调排好队, 窗口后关, 队列再跑"。"""

    def __init__(self):
        self.queue: list[tuple] = []

    def after(self, _delay, func, *args):
        self.queue.append((func, args))

    def drain(self):
        pending, self.queue = self.queue, []
        for func, args in pending:
            func(*args)


@pytest.fixture
def pool_sync_env(monkeypatch):
    """把 `_run_pool_sync` 的外部依赖全换成 stub, 线程改为同步执行。"""
    stub_ctk = types.SimpleNamespace(
        CTkToplevel=_StubWidget, CTkLabel=_StubWidget, CTkTextbox=_StubWidget,
        CTkFrame=_StubWidget, CTkButton=_StubWidget, StringVar=_StubVar,
    )
    monkeypatch.setattr(wind_sync, "ctk", stub_ctk)

    lines = ["第 1 行\n", "第 2 行\n", "第 3 行\n"]
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: _StubProc(lines))

    class _SyncThread:
        def __init__(self, target=None, **_kwargs):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr(wind_sync.threading, "Thread", _SyncThread)
    return lines


def _run_pool_sync_on_stub(app):
    """跑一次池同步, 返回 (弹窗, 输出框, 终止按钮, 关闭按钮)。"""
    made: list[_StubWidget] = []
    original_init = _StubWidget.__init__

    def _tracking_init(self, master=None, **kwargs):
        original_init(self, master, **kwargs)
        made.append(self)

    _StubWidget.__init__ = _tracking_init
    try:
        wind_sync.WindSyncMixin._run_pool_sync(
            app, "convertible_bond.cli.sync_events", "📰 同步公告事件", confirm=False)
    finally:
        _StubWidget.__init__ = original_init
    win = made[0]
    text_box = next(w for w in made if w.kwargs.get("wrap") == "word")
    cancel_btn = next(w for w in made if w.kwargs.get("text") == "终止")
    close_btn = next(w for w in made if w.kwargs.get("text") == "关闭")
    return win, text_box, cancel_btn, close_btn


def test_pool_sync_writes_output_when_the_window_is_still_open(pool_sync_env):
    """反面基线: 窗口没关时三行输出必须真的落进文本框。

    没有这一条, 一个"永远 return"的假闸也能让下面那个用例变绿。
    """
    app = _StubApp()
    _win, text_box, cancel_btn, close_btn = _run_pool_sync_on_stub(app)
    app.drain()

    assert text_box.inserted == ["第 1 行\n", "第 2 行\n", "第 3 行\n"]
    assert {"state": "disabled"} in cancel_btn.configured
    assert {"state": "normal"} in close_btn.configured


def test_pool_sync_survives_a_window_the_user_closed_midway(pool_sync_env):
    """用户中途点 X 关窗: 已排队的回调不许把 TclError 抛进 Tk 的回调栈。

    `win.protocol("WM_DELETE_WINDOW", ...)` 明确允许关窗, 而队列里此刻躺着每行输出
    一条 `append` 加两条按钮 `configure`。修复前这里会抛
    `_StubTclError: invalid command name ...`。
    """
    app = _StubApp()
    win, text_box, cancel_btn, close_btn = _run_pool_sync_on_stub(app)

    # 走真实路径: 点窗口的 X (protocol 处理器里 _kill 后 destroy)
    win.protocols["WM_DELETE_WINDOW"]()
    assert win.winfo_exists() == 0

    app.drain()  # 修复前: 这里抛 TclError

    assert text_box.inserted == []
    assert cancel_btn.configured == []
    assert close_btn.configured == []


def test_widget_alive_treats_a_raising_widget_as_dead():
    """`winfo_exists` 本身抛异常时按"已销毁"处理 —— 闸不许自己变成新的崩溃点。"""
    class _Exploding:
        def winfo_exists(self):
            raise _StubTclError("boom")

    assert wind_sync._widget_alive(_Exploding()) is False
    assert wind_sync._configure_if_alive(_Exploding(), state="normal") is False

    alive = _StubWidget()
    assert wind_sync._widget_alive(alive) is True
    assert wind_sync._configure_if_alive(alive, state="disabled") is True
    assert alive.configured == [{"state": "disabled"}]


# ─────────────── R1-54: worker 线程不许现读 Tk 变量 ───────────────

#: 一组能让 `_collect_params` 跑通的输入。**写字面量**, 不从被测常量推导。
_PRICING_INPUT_TEXT = {
    "v_bond_code": "",          # 空代码 → 不走条款投影, 用例不碰真实数据
    "v_coupons": "0.3,0.5,1.0,1.5,1.8,2.0",
    "v_cur_date": "2026-09-03",
    "v_S0": "10.0",
    "v_K": "12.0",
    "v_face": "100",
    "v_redemp": "108",
    "v_mat_date": "2030-09-03",
    "v_iss_date": "2024-09-03",
    "v_conv_date": "2025-03-03",
    "v_call_ratio": "130",
    "v_put_ratio": "70",
    "v_put_years": "2",
    "v_call_notice": "30",
    "v_down_reset_trigger_ratio": "85",
    "v_p_down": "25",
    "v_spread": "3.0",
    "v_sigma": "28",
    "v_r": "2.2",
    "v_q": "1.0",
    "v_dk": "5",
    "v_M": "300",
    "v_N": "1000",
}


class _TripwireVar:
    """被读到就炸 —— 用来证明"传了快照就一格活变量都不碰"。"""

    def __init__(self, name):
        self._name = name

    def get(self):
        raise AssertionError(f"传了输入快照, 却仍然去读活的 Tk 变量 {self._name}")


def _make_pricing_app():
    from convertible_bond.gui.controllers import pricing as pricing_ctl

    class _StubPricingApp(pricing_ctl.PricingMixin):
        def _normalize_bond_code(self, raw):      # app.py 上的方法, 这里给个直通
            return (raw or "").strip()

        def _resolve_down_reset_for_pricing(self, _valuation_date):
            return None, None                     # 手工下修覆盖不参与本用例

    app = _StubPricingApp()
    for name, text in _PRICING_INPUT_TEXT.items():
        setattr(app, name, _StubVar(text))
    return app


def test_snapshot_beats_a_variable_the_user_edited_after_the_click():
    """点了「开始计算」之后再改 σ / M, 算出来的必须还是点击那一刻的值。

    `_collect_params` 中间隔着一次真正的取数 (`_estimate_down_reset_floor_for_gui`
    → provider 拉正股日线, 实测 0.22s ~ 20s), 而 σ/r/q/利差/p_down/M/N 这 8 格是在
    那之后才读的 —— 窗口是秒级不是微秒级。
    """
    app = _make_pricing_app()
    snapshot = app._read_pricing_inputs()

    # 用户在计算过程中把输入框改了
    app.v_sigma.set("99")
    app.v_M.set("1234")
    app.v_S0.set("77.7")

    params = app._collect_params(inputs=snapshot)

    assert params["model"]["sigma"] == pytest.approx(0.28)
    assert params["model"]["M"] == 300
    assert params["pricer"]["S0"] == pytest.approx(10.0)


def test_collect_params_with_a_snapshot_touches_no_live_variable():
    """传了快照就一格活变量都不许读 —— 漏登记一个名字要在这里当场变红。"""
    app = _make_pricing_app()
    snapshot = app._read_pricing_inputs()
    for name in _PRICING_INPUT_TEXT:
        setattr(app, name, _TripwireVar(name))

    params = app._collect_params(inputs=snapshot)   # 读到活变量 → AssertionError
    assert params["model"]["N"] == 1000


def test_collect_params_without_a_snapshot_still_reads_live_variables():
    """主线程调用方 (敏感性页 / 现金流窗口) 不传快照, 行为与从前逐字一致。"""
    app = _make_pricing_app()
    app.v_sigma.set("41")
    params = app._collect_params()
    assert params["model"]["sigma"] == pytest.approx(0.41)


def _thread_target_functions(tree: ast.AST) -> set[str]:
    """`threading.Thread(target=...)` 指向的函数名 (含 `self.x` 与裸 `worker`)。"""
    targets: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "Thread"):
            continue
        for kw in node.keywords:
            if kw.arg != "target":
                continue
            if isinstance(kw.value, ast.Attribute):
                targets.add(kw.value.attr)
            elif isinstance(kw.value, ast.Name):
                targets.add(kw.value.id)
    return targets


def test_every_threaded_collect_params_call_passes_a_snapshot():
    """线程体里的 `_collect_params` 必须带 `inputs=`。

    上面两条钉的是"传了快照会怎样", 这条钉的是"启动器真的传了" —— 少了它, 一个
    功能完好的快照机制可以一个调用点都没接上, 而三条用例照样全绿。
    """
    from convertible_bond.gui.controllers import pricing as pricing_ctl

    source = Path(inspect.getsourcefile(pricing_ctl)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    targets = _thread_target_functions(tree)
    assert {"_pricing_worker", "_solve_iv_worker", "_convergence_worker", "worker"} <= targets, (
        f"定价页的线程目标集变了: {sorted(targets)}"
    )

    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name not in targets:
            continue
        for call in ast.walk(node):
            if not (isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "_collect_params"):
                continue
            if not any(kw.arg == "inputs" for kw in call.keywords):
                offenders.append(f"{node.name} (第 {call.lineno} 行)")
    assert not offenders, (
        "这些线程体里的 _collect_params 没带 inputs=, 等于在 worker 里现读 Tk 变量: "
        + ", ".join(offenders)
    )


def test_the_snapshot_is_taken_before_the_thread_starts():
    """`_read_pricing_inputs` 只许在**主线程**调 —— 线程体里不许出现它。

    上一条钉的是"worker 拿到了快照", 但把 `inputs = self._read_pricing_inputs()`
    整句搬进 worker 一样能让它变绿: 签名照旧、`inputs=` 照传, 而读那 23 格的时刻
    又回到了线程里 —— 也就是这次改动要修的那个形状原封不动地回来了。
    """
    from convertible_bond.gui.controllers import pricing as pricing_ctl

    source = Path(inspect.getsourcefile(pricing_ctl)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    targets = _thread_target_functions(tree)

    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name not in targets:
            continue
        for call in ast.walk(node):
            if (isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "_read_pricing_inputs"):
                offenders.append(f"{node.name} (第 {call.lineno} 行)")
    assert not offenders, (
        "这些线程体里调了 _read_pricing_inputs —— 快照必须在起线程之前取: "
        + ", ".join(offenders)
    )


# ─────────────── R1-34: 定价页的 ⭐ 要刷主页横幅, 不写别页的状态行 ───────────────

def _watchlist_star_env(monkeypatch):
    from convertible_bond.gui.controllers import pricing as pricing_ctl
    from convertible_bond.gui.tabs import batch_watchlist

    calls: list[str] = []
    monkeypatch.setattr(batch_watchlist, "refresh_home",
                        lambda _app: calls.append("refresh_home"))
    monkeypatch.setattr(batch_watchlist, "_render_watchlist_table",
                        lambda _app: calls.append("_render_watchlist_table"))
    monkeypatch.setattr(pricing_ctl, "add_to_watchlist",
                        lambda items: ([dict(items[0])], 1))

    class _StarApp(pricing_ctl.PricingMixin):
        def _normalize_bond_code(self, raw):
            return (raw or "").strip()

        def _flash_watchlist_button(self, _text, **_kwargs):
            pass

    app = _StarApp()
    app.v_bond_code = _StubVar("128009.SZ")
    app.v_K = _StubVar("12.0")
    app.v_result = _StubVar("118.30")
    app.v_market_price = _StubVar("125.00")
    app.v_status = _StubVar("")
    app.v_batch_status = _StubVar("✅ 全池: 展示 311/311 只")
    app.terms_cache = None
    return app, calls


def test_star_on_the_pricing_page_refreshes_the_home_event_banner(monkeypatch):
    """走 `refresh_home` 而不是 `_render_watchlist_table`.

    横幅的扫描集就是关注池, 刚加进来的那只债有没有在途事件正是它该说的话; 而横幅
    拿不到控件时是静默 return, 少画一次没有任何提示。
    """
    app, calls = _watchlist_star_env(monkeypatch)
    app._add_current_to_watchlist()

    assert calls == ["refresh_home"], (
        f"定价页 ⭐ 的刷新调用链是 {calls} —— 只重画表会把事件横幅停在旧内容上"
    )


def test_star_on_the_pricing_page_leaves_the_batch_status_line_alone(monkeypatch):
    """状态行按**用户触发时在哪一页**分: 定价页触发的只写 `v_status`。

    `v_batch_status` 是批量页的**常驻**视图摘要, 塞一句定价页的瞬时消息进去,
    用户切过去看到的就是一句说着别的页的话, 而且要等下一次 `_render_table` 才被冲掉。
    """
    app, _calls = _watchlist_star_env(monkeypatch)
    app._add_current_to_watchlist()

    assert app.v_batch_status.get() == "✅ 全池: 展示 311/311 只"
    assert "关注池" in app.v_status.get()
