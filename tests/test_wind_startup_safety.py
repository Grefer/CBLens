"""没有 Wind 环境时, GUI 启动路径不许卡住, 也不许因此打不开.

背景 (实测): GUI 启动 80ms 后走 ``_load_result_cache`` → ``price_unpriced_new_bonds``
→ ``_start_watchlist_pricing(quiet=True)`` → 后台线程 → ``build_batch_provider("Wind")``
→ ``get_risk_free_rate`` → ``WindDataProvider._ensure()`` → ``w.start()``。

而 ``w.start()`` 的 WindPy 默认签名是 ``start(options=None, waitTime=120)`` ——
**终端没开时它会等满两分钟**。本机就是这个状态: WindPy 可导入、``isconnected()``
为 False。于是"打开 GUI"变成"打开后转两分钟圈", 而这一轮取数用户根本没要求。

三道防线, 每条一组用例:
1. ``w.start()`` 有界 + 失败进负缓存 (否则全池 284 只每只重等一遍)
2. 非用户发起的那一轮按 ``wind_is_ready()`` 直接不起 —— 它只检查, 不连接
3. 默认行情源按**实际可用性**挑, 不再硬编码 "Wind"
"""
from __future__ import annotations

import sys
import types

import pytest

import convertible_bond.data_providers.wind as wind_mod
from convertible_bond.data_providers.wind import WindDataProvider, wind_is_ready
from convertible_bond.gui.tabs import batch_watchlist as watchlist_tab


# ── 假 WindPy ────────────────────────────────────────────────────

class _FakeRet:
    def __init__(self, code):
        self.ErrorCode = code
        self.Data = "fake"


class _FakeW:
    """记录 start() 的调用次数与实参, 永远连不上."""

    def __init__(self, error_code=-40521004):
        self.calls: list[dict] = []
        self._error_code = error_code

    def isconnected(self):
        return False

    def start(self, options=None, waitTime=120, *a, **kw):
        self.calls.append({"options": options, "waitTime": waitTime})
        return _FakeRet(self._error_code)


@pytest.fixture()
def fake_windpy(monkeypatch):
    fake_w = _FakeW()
    module = types.ModuleType("WindPy")
    module.w = fake_w
    monkeypatch.setitem(sys.modules, "WindPy", module)
    monkeypatch.setattr(wind_mod, "prepare_windpy_import_path", lambda: [])
    return fake_w


# ── ① w.start() 必须有界 ─────────────────────────────────────────

def test_connect_passes_bounded_wait_time(fake_windpy, monkeypatch):
    """不传 waitTime 就会用 WindPy 的默认 120s —— 那正是"打开就卡住"的成因."""
    monkeypatch.setattr(wind_mod, "WIND_START_WAIT_SEC", 7)
    provider = WindDataProvider()
    with pytest.raises(ConnectionError):
        provider._ensure()
    assert fake_windpy.calls == [{"options": None, "waitTime": 7}]


def test_zero_wait_time_falls_back_to_windpy_default(fake_windpy, monkeypatch):
    """留一个逃生口: CBLENS_WIND_START_WAIT_SEC=0 时沿用 WindPy 自己的默认."""
    monkeypatch.setattr(wind_mod, "WIND_START_WAIT_SEC", 0)
    provider = WindDataProvider()
    with pytest.raises(ConnectionError):
        provider._ensure()
    assert fake_windpy.calls == [{"options": None, "waitTime": 120}]


def test_failed_connect_is_negatively_cached(fake_windpy, monkeypatch):
    """冷却期内不再重连.

    没有这道闸时 ``self._w`` 失败后仍是 None, 于是**每一次**取数都重等一遍:
    全池 284 只按 10 线程折算 = 284 × waitTime / 10, 20s 就是约 570s 的假死。
    """
    monkeypatch.setattr(wind_mod, "WIND_START_WAIT_SEC", 1)
    monkeypatch.setattr(wind_mod, "WIND_CONNECT_COOLDOWN_SEC", 3600)
    provider = WindDataProvider()
    errors = []
    for _ in range(50):
        with pytest.raises(ConnectionError) as exc:
            provider._ensure()
        errors.append(exc.value)
    assert len(fake_windpy.calls) == 1, "冷却期内只该真连一次"
    assert all(e is errors[0] for e in errors), "复用同一个异常对象"


def test_cooldown_expiry_allows_retry(fake_windpy, monkeypatch):
    monkeypatch.setattr(wind_mod, "WIND_START_WAIT_SEC", 1)
    monkeypatch.setattr(wind_mod, "WIND_CONNECT_COOLDOWN_SEC", 0)   # 立刻过期
    provider = WindDataProvider()
    for _ in range(3):
        with pytest.raises(ConnectionError):
            provider._ensure()
    assert len(fake_windpy.calls) == 3


def test_missing_windpy_is_negatively_cached_too(monkeypatch):
    """没装 Wind 的机器上, prepare_windpy_import_path 会扫盘找 WindPy.py ——
    一轮批量重复几百遍毫无意义。"""
    monkeypatch.setitem(sys.modules, "WindPy", None)   # import 时抛 ImportError
    probes = []
    monkeypatch.setattr(wind_mod, "prepare_windpy_import_path",
                        lambda: probes.append(1) or [])
    monkeypatch.setattr(wind_mod, "WIND_CONNECT_COOLDOWN_SEC", 3600)
    provider = WindDataProvider()
    for _ in range(20):
        with pytest.raises(ImportError):
            provider._ensure()
    assert len(probes) == 1


# ── ② wind_is_ready 只检查, 不连接 ───────────────────────────────

def test_wind_is_ready_false_when_terminal_not_connected(fake_windpy):
    assert wind_is_ready() is False
    assert fake_windpy.calls == [], "只问 isconnected(), 绝不能真去 start()"


def test_wind_is_ready_false_when_windpy_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "WindPy", None)
    monkeypatch.setattr(wind_mod, "prepare_windpy_import_path", lambda: [])
    assert wind_is_ready() is False        # 抛异常也要当成 False, 不能往外冒


def test_wind_is_ready_true_when_connected(monkeypatch):
    module = types.ModuleType("WindPy")
    module.w = types.SimpleNamespace(isconnected=lambda: True)
    monkeypatch.setitem(sys.modules, "WindPy", module)
    monkeypatch.setattr(wind_mod, "prepare_windpy_import_path", lambda: [])
    assert wind_is_ready() is True


# ── ③ 非用户发起的那一轮必须被挡住 ────────────────────────────────

class _Var:
    def __init__(self, value=""):
        self._value = value

    def get(self):
        return self._value

    def set(self, value):
        self._value = value


class _App:
    def __init__(self, source="Wind"):
        self.v_batch_source = _Var(source)
        self.v_batch_status = _Var("")
        self._watchlist_pricing_running = False


def test_quiet_round_does_not_start_when_wind_not_ready(monkeypatch):
    """启动自愈那一轮遇到"Wind 装了但没连"必须直接不起, 并说清怎么办."""
    monkeypatch.setattr(watchlist_tab, "wind_is_ready", lambda: False)
    started = []
    monkeypatch.setattr(watchlist_tab.threading, "Thread",
                        lambda *a, **kw: started.append(kw) or types.SimpleNamespace(start=lambda: None))

    app = _App("Wind")
    assert watchlist_tab._start_watchlist_pricing(app, ["123284.SZ"], quiet=True) is False
    assert started == [], "不许起线程"
    assert "⚡ 关注池重算" in app.v_batch_status.get(), "要告诉用户手动入口在哪"


def test_manual_round_is_not_gated(monkeypatch):
    """用户自己点的那一下不挡 —— 他知道自己在等什么, 且 waitTime 已经有界.

    这里只验证"没被 wind_is_ready 挡掉": 用一个缺定价参数的替身 app, 于是流程
    一定会往后走到读参数那一步并抛 AttributeError —— 抛到了就证明闸没拦它。
    """
    monkeypatch.setattr(watchlist_tab, "wind_is_ready", lambda: False)
    app = _App("Wind")
    with pytest.raises(AttributeError):
        watchlist_tab._start_watchlist_pricing(app, ["123284.SZ"], quiet=False)
    assert "当前不可用" not in app.v_batch_status.get()


def test_quiet_round_allows_akshare(monkeypatch):
    """akshare 是纯 HTTP, 没有"连接"这回事, 失败也是秒级 —— 不该被挡."""
    assert watchlist_tab._source_ready_without_connecting("akshare") is True


def test_quiet_round_blocks_csv():
    """CSV 要在主线程弹目录选择框; 启动时弹一个模态比不定价糟得多."""
    assert watchlist_tab._source_ready_without_connecting("CSV") is False
    assert watchlist_tab._source_ready_without_connecting("") is False


def test_single_flight_is_checked_before_source_readiness():
    """单飞最便宜且与源无关, 必须先判 —— 否则"已经在跑"会被误报成"源不可用"."""
    import inspect
    src = inspect.getsource(watchlist_tab._start_watchlist_pricing)
    assert src.index("_watchlist_pricing_running") < src.index("_source_ready_without_connecting")


# ── ④ 默认行情源不再硬编码 ──────────────────────────────────────

def test_default_source_falls_back_to_akshare_when_nothing_detected(monkeypatch):
    monkeypatch.setattr(watchlist_tab, "wind_is_ready", lambda: False)
    from convertible_bond.gui import constants
    monkeypatch.setattr(constants, "_DEFAULT_SOURCE_CACHE", [])
    monkeypatch.setattr("convertible_bond.data_providers.detect_available_providers",
                        lambda: [])
    assert constants.default_market_source() == "akshare"


def test_default_source_prefers_first_available(monkeypatch):
    from convertible_bond.gui import constants
    monkeypatch.setattr(constants, "_DEFAULT_SOURCE_CACHE", [])
    monkeypatch.setattr("convertible_bond.data_providers.detect_available_providers",
                        lambda: ["akshare"])
    assert constants.default_market_source() == "akshare"


def test_default_source_survives_detection_blowing_up(monkeypatch):
    """探测本身炸了也不能让 GUI 起不来 —— 这是构造 StringVar 时调的."""
    from convertible_bond.gui import constants
    monkeypatch.setattr(constants, "_DEFAULT_SOURCE_CACHE", [])

    def boom():
        raise RuntimeError("boom")

    monkeypatch.setattr("convertible_bond.data_providers.detect_available_providers", boom)
    assert constants.default_market_source() == "akshare"


def test_no_hardcoded_wind_default_left_in_gui():
    """守护: 行情源默认值一律走 default_market_source().

    硬编码 "Wind" 会让没装 Wind 的机器上每一次取数都必然失败, 而 GUI 侧
    从来没引用过 detect_available_providers, 用户拿到的只是一句"未安装 WindPy"。
    """
    import re
    from pathlib import Path
    import convertible_bond

    gui_root = Path(convertible_bond.__file__).parent / "gui"
    pattern = re.compile(r'StringVar\(\s*value\s*=\s*["\']Wind["\']')
    offenders = []
    for path in sorted(gui_root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for match in pattern.finditer(text):
            offenders.append(f"{path.name}:{text.count(chr(10), 0, match.start()) + 1}")
    assert not offenders, (
        "行情源默认值请改用 gui.constants.default_market_source():\n  " + "\n  ".join(offenders))
