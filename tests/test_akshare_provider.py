"""AkshareDataProvider 单元测试 (注入假 akshare 模块, 不发真实网络请求).

akshare 是免 Wind 用户的主力动态行情路径, 此前无专门测试。
当前覆盖: 转债列表缓存 TTL 行为 + 正股历史解析链路 (stock_zh_a_hist → 列名兼容)。
"""
import sys
import threading
import time
from datetime import date

import pandas as pd

import convertible_bond.data_providers.akshare as ak_mod
from convertible_bond.data_providers import is_issued_pending_listing
from convertible_bond.data_providers.akshare import AkshareDataProvider


class FakeAkshare:
    """最小假 akshare 模块: 计数 bond_zh_cov 调用, 返回固定 DataFrame."""

    def __init__(self):
        self.bond_zh_cov_calls = 0

    def bond_zh_cov(self):
        self.bond_zh_cov_calls += 1
        return pd.DataFrame({"债券代码": ["128009"], "债券简称": ["测试转债"]})

    def stock_zh_a_hist(self, **kwargs):
        return pd.DataFrame({
            "日期": ["2026-06-01", "2026-06-02", "2026-06-03"],
            "收盘": [10.0, 10.5, 11.0],
        })


def _make_provider(monkeypatch, fake):
    monkeypatch.setitem(sys.modules, "akshare", fake)
    return AkshareDataProvider()


def test_cb_list_cached_within_ttl_and_refetched_after_expiry(monkeypatch):
    """转债列表 TTL: 期内复用缓存, 过期自动重拉 (长开 GUI 不漏新上市/退市债)."""
    fake = FakeAkshare()
    provider = _make_provider(monkeypatch, fake)

    clock = {"now": 1000.0}
    monkeypatch.setattr(ak_mod.time, "monotonic", lambda: clock["now"])

    provider._cb_list()
    provider._cb_list()
    assert fake.bond_zh_cov_calls == 1, "TTL 内应复用缓存"

    clock["now"] += AkshareDataProvider._CB_LIST_TTL_SECONDS + 1
    provider._cb_list()
    assert fake.bond_zh_cov_calls == 2, "TTL 过期应重新拉取"

    provider._cb_list()
    assert fake.bond_zh_cov_calls == 2, "重拉后再次进入 TTL 期内"


def test_get_stock_history_parses_hist_dataframe(monkeypatch):
    """正股历史: stock_zh_a_hist 中文列名 DataFrame → [(date, close), ...] 升序."""
    provider = _make_provider(monkeypatch, FakeAkshare())

    history = provider.get_stock_history("000001.SZ", date(2026, 6, 1), date(2026, 6, 3))

    assert history == [
        (date(2026, 6, 1), 10.0),
        (date(2026, 6, 2), 10.5),
        (date(2026, 6, 3), 11.0),
    ]


class FakePendingAkshare(FakeAkshare):
    """还没挂牌的新债: 上游对 '上市时间' 返回 pandas.NaT (实测 2026-08 三只在途新债)."""

    def bond_zh_cov(self):
        self.bond_zh_cov_calls += 1
        return pd.DataFrame({
            "债券代码": ["123284"],
            "债券简称": ["强达转债"],
            "申购日期": [pd.Timestamp("2026-08-19")],
            "上市时间": [pd.NaT],
            "正股代码": ["301628"],
            "转股价": [84.04],
            "信用评级": ["AA-"],
        })

    def bond_cb_profile_sina(self, symbol=None):
        return None


def test_pending_new_bond_gets_no_fabricated_listing_date(monkeypatch):
    """上市时间为 NaT 时 listing_date 必须留空, 不能回落成起息日/申购日.

    ``pandas.NaT`` 是 datetime 子类且 ``bool(NaT) is True``, 所以 ``to_date`` 原样放行、
    ``listing_dt or issue_dt`` 也不回落 —— 两条路都会让"还没挂牌"的新债拿到一个日期。
    而 ``listing_date`` 非空正是 :func:`is_issued_pending_listing` 判"已经挂牌了"的依据:
    一旦伪造, 新债 tradable_date = 起息日 ≤ 今天 → 带着空市价混进主池, 同时从"扫新债"消失。
    """
    provider = _make_provider(monkeypatch, FakePendingAkshare())

    terms = provider.get_bond_terms("123284.SZ", date(2026, 8, 25))

    assert terms.listing_date is None
    assert terms.issue_date == date(2026, 8, 19)
    assert is_issued_pending_listing("123284.SZ", terms, date(2026, 8, 25))
    assert terms.trading_status == "pending"
    assert terms.is_tradable is False


# ── V8 (py_mini_racer) 预热: 防"切到 akshare 点重算, GUI 直接闪退" ──────────────
class FakeMiniRacerModule:
    """假 py_mini_racer: 记录上下文建了几个 / 分别在哪个线程 / 关没关."""

    def __init__(self, ctor_delay: float = 0.0):
        self.ctor_threads: list[str] = []
        self.closed = 0
        self._ctor_delay = ctor_delay
        outer = self

        class MiniRacer:
            def __init__(self):
                outer.ctor_threads.append(threading.current_thread().name)
                if outer._ctor_delay:
                    time.sleep(outer._ctor_delay)

            def eval(self, _src):
                return 1

            def close(self):
                outer.closed += 1

        self.MiniRacer = MiniRacer


def test_provider_init_warms_js_runtime(monkeypatch):
    """provider 构造时必须把 V8 预热掉 —— 那是 fan-out 之前唯一的单线程时机.

    akshare 的 ``bond_zh_hs_cov_daily`` / ``stock_zh_a_daily`` 每次调用都新建一个
    MiniRacer 上下文, 而 V8 的 partition_alloc 地址空间是进程级一次性初始化且非线程
    安全: 批量定价的 8 个 worker 头一回同时进到那一行, 落后的那个 PA_CHECK 失败 →
    SIGTRAP。那是 C 层 abort 不是 Python 异常, worker 的 try/except 接不住,
    整个进程当场消失 (实测 8 线程同时首建 3/3 崩, 预热后 5/5 干净)。
    """
    calls = []
    monkeypatch.setattr(ak_mod, "_warm_up_js_runtime", lambda: calls.append(1))

    _make_provider(monkeypatch, FakeAkshare())

    assert calls, "AkshareDataProvider.__init__ 必须调用 _warm_up_js_runtime"


def test_warm_up_js_runtime_builds_exactly_one_context_under_concurrency(monkeypatch):
    """并发调用只许建一个上下文 —— 竞态正是崩溃的成因, 预热自己不能再制造一次."""
    fake = FakeMiniRacerModule(ctor_delay=0.02)   # 拉宽窗口, 让后到的线程真的撞上
    monkeypatch.setitem(sys.modules, "py_mini_racer", fake)
    monkeypatch.setattr(ak_mod, "_js_runtime_warmed", False)

    barrier = threading.Barrier(8)

    def go():
        barrier.wait()
        ak_mod._warm_up_js_runtime()

    threads = [threading.Thread(target=go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)

    assert len(fake.ctor_threads) == 1, f"预热重复建了 {len(fake.ctor_threads)} 个上下文"
    assert ak_mod._js_runtime_warmed is True

    ak_mod._warm_up_js_runtime()
    assert len(fake.ctor_threads) == 1, "已预热后不该再建"


def test_warm_up_js_runtime_keeps_no_live_context(monkeypatch):
    """预热完必须把上下文关掉, 不留全局引用.

    ``MiniRacer.__del__`` → ``close()`` 会 join 它自己的事件循环线程, 而解释器退出时
    守护线程已被冻结 —— 留一份活引用只是把"启动闪退"换成"退出挂死"。
    """
    fake = FakeMiniRacerModule()
    monkeypatch.setitem(sys.modules, "py_mini_racer", fake)
    monkeypatch.setattr(ak_mod, "_js_runtime_warmed", False)

    ak_mod._warm_up_js_runtime()

    assert fake.closed == 1, "预热用的上下文必须显式 close"
    assert not [v for v in vars(ak_mod).values()
                if isinstance(v, fake.MiniRacer)], "模块里不许留活着的 MiniRacer"


def test_warm_up_js_runtime_never_raises(monkeypatch):
    """预热是防御性的: py_mini_racer 缺失/初始化失败都不该挡住取数本身."""
    monkeypatch.setitem(sys.modules, "py_mini_racer", None)   # import 时抛 ImportError
    monkeypatch.setattr(ak_mod, "_js_runtime_warmed", False)

    ak_mod._warm_up_js_runtime()          # 不抛就是通过

    assert ak_mod._js_runtime_warmed is True, "失败也要置位, 否则每次构造 provider 都重试"
