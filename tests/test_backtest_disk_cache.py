"""backtest_disk_cache.DiskCacheProvider 单测 (离线 fake provider, 不连 Wind)。"""
from collections import Counter
from datetime import date


from convertible_bond.backtest_disk_cache import DiskCacheProvider
from convertible_bond.data_providers import BondTerms, DataProvider

PAST = date(2024, 6, 28)
FUTURE = date(2999, 1, 1)


def _sample_terms(name="测试转债") -> BondTerms:
    return BondTerms(
        sec_name=name,
        underlying_code="000001.SZ",
        issue_date=date(2022, 1, 5),
        maturity_date=date(2030, 1, 5),
        conversion_price=15.5,
        redemption_price=110.0,
        coupon_rates=(0.003, 0.005, 0.01, 0.015, 0.018, 0.02),
    )


class FakeProvider(DataProvider):
    """记录调用次数、返回确定数据的假数据源。"""

    name = "fake"

    def __init__(self):
        self.calls: Counter = Counter()

    def get_bond_terms(self, bond_code, valuation_date):
        self.calls["terms"] += 1
        return _sample_terms(f"债_{bond_code}")

    def get_stock_close(self, stock_code, on_date):
        self.calls["stock_close"] += 1
        return 10.0

    def get_stock_history(self, stock_code, start, end):
        self.calls["stock_history"] += 1
        return [(date(2024, 6, 3), 10.0), (date(2024, 6, 4), None), (date(2024, 6, 5), 11.5)]

    def get_bond_history(self, bond_code, start, end):
        self.calls["bond_history"] += 1
        return [(date(2024, 6, 3), 120.0), (date(2024, 6, 4), 121.5)]

    def cache_identity(self):
        return "fake-id"


def _provider(tmp_path, **kw):
    return DiskCacheProvider(FakeProvider(), tmp_path / "cache", today=date(2025, 1, 1), **kw)


# ---------------- terms ----------------

def test_terms_miss_then_hit(tmp_path):
    p = _provider(tmp_path)
    t1 = p.get_bond_terms("113062.SH", PAST)
    t2 = p.get_bond_terms("113062.SH", PAST)
    assert p.inner.calls["terms"] == 1            # 第二次命中, 未再调 inner
    assert p.stats["terms_hits"] == 1 and p.stats["terms_misses"] == 1
    # 往返一致: 关键字段保留 (含 date 与 tuple)
    assert t2.sec_name == t1.sec_name
    assert t2.issue_date == date(2022, 1, 5)
    assert t2.maturity_date == date(2030, 1, 5)
    assert t2.coupon_rates == (0.003, 0.005, 0.01, 0.015, 0.018, 0.02)
    assert isinstance(t2.coupon_rates, tuple)


def test_terms_future_date_not_cached(tmp_path):
    p = _provider(tmp_path)
    p.get_bond_terms("113062.SH", FUTURE)
    p.get_bond_terms("113062.SH", FUTURE)
    assert p.inner.calls["terms"] == 2            # 当日/未来每次都打 inner
    assert p.stats["terms_hits"] == 0


def test_terms_persist_across_instances(tmp_path):
    p1 = _provider(tmp_path)
    p1.get_bond_terms("128001.SZ", PAST)
    p1.flush()
    # 新实例 (新 inner) 从磁盘复用, 不再调 inner
    p2 = DiskCacheProvider(FakeProvider(), tmp_path / "cache", today=date(2025, 1, 1))
    t = p2.get_bond_terms("128001.SZ", PAST)
    assert p2.inner.calls["terms"] == 0
    assert p2.stats["terms_hits"] == 1
    assert t.sec_name == "债_128001.SZ"


def test_namespace_isolation(tmp_path):
    # 不同命名空间 (不同口径) 不串味
    p1 = DiskCacheProvider(FakeProvider(), tmp_path / "c", today=date(2025, 1, 1), namespace="A")
    p1.get_bond_terms("x", PAST); p1.flush()
    p2 = DiskCacheProvider(FakeProvider(), tmp_path / "c", today=date(2025, 1, 1), namespace="B")
    p2.get_bond_terms("x", PAST)
    assert p2.inner.calls["terms"] == 1           # 命名空间 B 未命中 A 的缓存


# ---------------- histories ----------------

def test_bond_history_miss_then_hit_roundtrip(tmp_path):
    p = _provider(tmp_path)
    h1 = p.get_bond_history("113062.SH", date(2024, 6, 1), PAST)
    h2 = p.get_bond_history("113062.SH", date(2024, 6, 1), PAST)
    assert p.inner.calls["bond_history"] == 1
    assert h2 == [(date(2024, 6, 3), 120.0), (date(2024, 6, 4), 121.5)]


def test_stock_history_preserves_none_values(tmp_path):
    p = _provider(tmp_path)
    p.get_stock_history("000001.SZ", date(2024, 6, 1), PAST)
    p.flush()
    p2 = DiskCacheProvider(FakeProvider(), tmp_path / "cache", today=date(2025, 1, 1))
    h = p2.get_stock_history("000001.SZ", date(2024, 6, 1), PAST)
    assert p2.inner.calls["stock_history"] == 0
    assert h == [(date(2024, 6, 3), 10.0), (date(2024, 6, 4), None), (date(2024, 6, 5), 11.5)]


def test_history_future_end_not_cached(tmp_path):
    p = _provider(tmp_path)
    p.get_bond_history("x", date(2024, 1, 1), FUTURE)
    p.get_bond_history("x", date(2024, 1, 1), FUTURE)
    assert p.inner.calls["bond_history"] == 2


# ---------------- 透传 & 鲁棒性 ----------------

def test_passthrough_methods(tmp_path):
    p = _provider(tmp_path)
    assert p.get_stock_close("000001.SZ", PAST) == 10.0
    assert p.cache_identity() == "fake-id"


def test_corrupt_cache_file_ignored(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "terms.json").write_text("{ not json", encoding="utf-8")
    p = DiskCacheProvider(FakeProvider(), cache_dir, today=date(2025, 1, 1))
    # 损坏文件被忽略, 退回向 inner 取数
    p.get_bond_terms("x", PAST)
    assert p.inner.calls["terms"] == 1


def test_context_manager_flushes(tmp_path):
    with DiskCacheProvider(FakeProvider(), tmp_path / "cache", today=date(2025, 1, 1)) as p:
        p.get_bond_terms("y", PAST)
    assert (tmp_path / "cache" / "terms.json").exists()


# ---------------- 身份守卫 (条款来源变更 → 缓存失效) ----------------

def test_meta_written_with_identity(tmp_path):
    p = DiskCacheProvider(FakeProvider(), tmp_path / "cache",
                          today=date(2025, 1, 1), namespace="idA")
    p.get_bond_terms("x", PAST)
    p.flush()
    import json
    meta = json.loads((tmp_path / "cache" / "_meta.json").read_text(encoding="utf-8"))
    assert meta["identity"] == "idA"


def test_identity_change_invalidates_cache(tmp_path):
    cache = tmp_path / "cache"
    # 身份 idA 写入
    p1 = DiskCacheProvider(FakeProvider(), cache, today=date(2025, 1, 1), namespace="idA")
    p1.get_bond_terms("x", PAST)
    p1.flush()
    # 同身份 idA → 命中
    p_same = DiskCacheProvider(FakeProvider(), cache, today=date(2025, 1, 1), namespace="idA")
    p_same.get_bond_terms("x", PAST)
    assert p_same.inner.calls["terms"] == 0 and p_same.stats["terms_hits"] == 1
    # 身份变 idB (模拟 patch/events/bundle 更新) → 旧缓存弃用 → 未命中
    p_new = DiskCacheProvider(FakeProvider(), cache, today=date(2025, 1, 1), namespace="idB")
    p_new.get_bond_terms("x", PAST)
    assert p_new.inner.calls["terms"] == 1 and p_new.stats["terms_hits"] == 0


def test_auto_identity_tracks_store_mtime(tmp_path):
    # 默认身份应纳入条款来源文件的 mtime: 文件变 → 身份变
    store = tmp_path / "events.json"
    store.write_text("{}", encoding="utf-8")

    class WithStore(FakeProvider):
        def __init__(self, path):
            super().__init__()
            self.event_store = type("S", (), {"path": path})()

    p1 = DiskCacheProvider(WithStore(store), tmp_path / "c", today=date(2025, 1, 1))
    id1 = p1._identity
    import os, time
    os.utime(store, (time.time() + 10, time.time() + 10))   # 改 mtime
    p2 = DiskCacheProvider(WithStore(store), tmp_path / "c", today=date(2025, 1, 1))
    assert p1._identity != p2._identity                      # 身份随文件变化
