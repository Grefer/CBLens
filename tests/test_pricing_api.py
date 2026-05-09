import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import pytest

from convertible_bond import pricing_api


class DummyProvider:
    name = "dummy"


def test_batch_price_from_provider_threaded_runs_concurrently(monkeypatch):
    active = 0
    max_active = 0
    lock = threading.Lock()

    def fake_price_from_provider(provider, code, **kwargs):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        try:
            time.sleep(0.03)
            market_price = {"A": 90.0, "B": 110.0, "C": None}[code]
            return {
                "bond_code": code,
                "theoretical_price": 100.0,
                "market_price": market_price,
            }
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(pricing_api, "price_from_provider", fake_price_from_provider)
    progress = []

    results = pricing_api.batch_price_from_provider_threaded(
        DummyProvider(),
        ["A", "B", "C"],
        max_workers=3,
        progress_cb=lambda done, total: progress.append((done, total)),
    )

    assert max_active > 1
    assert [row["bond_code"] for row in results] == ["A", "B", "C"]
    assert [row["status"] for row in results] == ["ok", "ok", "ok"]
    assert results[0]["undervaluation_rate"] == 0.1
    assert results[1]["undervaluation_rate"] == -0.1
    assert progress[-1] == (3, 3)


def test_batch_price_from_provider_keeps_legacy_worker_default(monkeypatch):
    seen = {}

    def fake_threaded(provider, bond_codes, **kwargs):
        seen["max_workers"] = kwargs["max_workers"]
        return []

    monkeypatch.setattr(pricing_api, "batch_price_from_provider_threaded", fake_threaded)

    pricing_api.batch_price_from_provider(DummyProvider(), ["A"])

    assert seen["max_workers"] == 4


def test_batch_stock_cache_hist_vol_uses_shared_history_and_fills_close_cache():
    class HistoryProvider:
        name = "history"

        def __init__(self):
            self.history_calls = 0
            self.close_calls = 0
            self.hist_vol_calls = 0

        def get_stock_history(self, stock_code, start, end):
            self.history_calls += 1
            return [
                (start + timedelta(days=i), 10.0 + i)
                for i in range((end - start).days + 1)
            ]

        def get_stock_close(self, stock_code, on_date):
            self.close_calls += 1
            raise AssertionError("close should be served from batch history cache")

        def hist_vol(self, stock_code, end_date, window_days):
            self.hist_vol_calls += 1
            raise AssertionError("hist_vol should be computed by the batch cache")

    inner = HistoryProvider()
    cached = pricing_api._BatchStockCache(inner)
    end = date(2026, 4, 28)

    vol1 = cached.hist_vol("000001.SZ", end, 21)
    vol2 = cached.hist_vol("000001.SZ", end, 21)
    close = cached.get_stock_close("000001.SZ", end)

    assert vol1 == vol2
    assert vol1 > 0
    assert close == 52.0
    assert inner.history_calls == 1
    assert inner.close_calls == 0
    assert inner.hist_vol_calls == 0


def test_batch_stock_cache_caches_dividend_yield():
    class DividendProvider:
        name = "dividend"

        def __init__(self):
            self.calls = 0

        def get_stock_dividend_yield(self, stock_code, on_date):
            self.calls += 1
            return 2.5

    inner = DividendProvider()
    cached = pricing_api._BatchStockCache(inner)
    end = date(2026, 4, 28)

    assert cached.get_stock_dividend_yield("000001.SZ", end) == 2.5
    assert cached.get_stock_dividend_yield("000001.SZ", end) == 2.5
    assert inner.calls == 1


def test_batch_stock_cache_waiter_retries_after_owner_failure():
    class FlakyCloseProvider:
        name = "flaky"

        def __init__(self):
            self.calls = 0
            self.lock = threading.Lock()
            self.started = threading.Event()
            self.release = threading.Event()

        def get_stock_close(self, stock_code, on_date):
            with self.lock:
                self.calls += 1
                call_no = self.calls
            if call_no == 1:
                self.started.set()
                self.release.wait(timeout=1.0)
                raise RuntimeError("first fetch failed")
            return 10.0

    inner = FlakyCloseProvider()
    cached = pricing_api._BatchStockCache(inner)
    end = date(2026, 4, 28)

    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(cached.get_stock_close, "000001.SZ", end)
        assert inner.started.wait(timeout=1.0)
        waiter = pool.submit(cached.get_stock_close, "000001.SZ", end)
        inner.release.set()

        with pytest.raises(RuntimeError, match="first fetch failed"):
            owner.result(timeout=1.0)
        assert waiter.result(timeout=1.0) == 10.0

    assert inner.calls == 2


def test_batch_stock_cache_waiter_timeout_is_explicit():
    class SlowCloseProvider:
        name = "slow"

        def __init__(self):
            self.started = threading.Event()
            self.release = threading.Event()

        def get_stock_close(self, stock_code, on_date):
            self.started.set()
            self.release.wait(timeout=1.0)
            return 10.0

    inner = SlowCloseProvider()
    cached = pricing_api._BatchStockCache(inner)
    cached._INFLIGHT_TIMEOUT = 0.01
    end = date(2026, 4, 28)

    with ThreadPoolExecutor(max_workers=1) as pool:
        owner = pool.submit(cached.get_stock_close, "000001.SZ", end)
        assert inner.started.wait(timeout=1.0)
        with pytest.raises(TimeoutError, match="批量缓存等待超时"):
            cached.get_stock_close("000001.SZ", end)
        inner.release.set()
        assert owner.result(timeout=1.0) == 10.0
