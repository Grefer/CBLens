import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import pytest

from convertible_bond import pricing_api
from convertible_bond.data_providers import BondTerms
from convertible_bond.historical_terms import TermsPatch, TermsPatchStore


class DummyProvider:
    name = "dummy"


class SimplePricingProvider:
    name = "simple"

    def __init__(self, terms: BondTerms):
        self.terms = terms

    def get_bond_terms(self, bond_code, valuation_date):
        return self.terms

    def hist_vol(self, stock_code, end_date, window_days):
        return 0.2

    def get_stock_close(self, stock_code, on_date):
        return 12.0

    def get_stock_dividend_yield(self, stock_code, on_date):
        return None

    def get_bond_history(self, bond_code, start, end):
        return [(end, 101.0)]

    def get_stock_history(self, stock_code, start, end):
        return []

    def get_cashflow(self, bond_code):
        return None


def _base_terms(**updates):
    values = dict(
        sec_name="测试转债",
        underlying_code="600001.SH",
        issue_date=date(2024, 1, 1),
        maturity_date=date(2030, 1, 1),
        conversion_price=10.0,
        face_value=100.0,
        redemption_price=107.0,
        coupon_rates=(0.003,),
    )
    values.update(updates)
    return BondTerms(**values)


def test_price_from_provider_applies_terms_patch_before_pricing(monkeypatch, tmp_path):
    class Provider:
        name = "patched"

        def get_bond_terms(self, bond_code, valuation_date):
            return BondTerms(
                sec_name="测试转债",
                underlying_code="600001.SH",
                issue_date=date(2024, 1, 1),
                maturity_date=date(2030, 1, 1),
                conversion_price=10.0,
                face_value=100.0,
                redemption_price=107.0,
                coupon_rates=(0.003,),
            )

        def hist_vol(self, stock_code, end_date, window_days):
            return 0.2

        def get_stock_close(self, stock_code, on_date):
            return 12.0

        def get_stock_dividend_yield(self, stock_code, on_date):
            return None

        def get_bond_history(self, bond_code, start, end):
            return [(end, 101.0)]

        def get_cashflow(self, bond_code):
            return None

    seen = {}

    class FakePricer:
        def __init__(self, **kwargs):
            seen.update(kwargs)
            self.K = kwargs["K"]
            self.S0 = kwargs["S0"]
            self.T = 1.0
            self.ratio = 100.0 / self.K

        def price(self, **kwargs):
            return 123.0

    monkeypatch.setattr(pricing_api, "UniversalCBPricer", FakePricer)

    patch_store = TermsPatchStore(tmp_path / "patches.json")
    patch_store.add_many([
        TermsPatch(
            bond_code="113001.SH",
            effective_date=date(2026, 5, 12),
            fields={"conversion_price": 8.0},
        )
    ])

    result = pricing_api.price_from_provider(
        Provider(),
        "113001.SH",
        valuation_date=date(2026, 5, 20),
        term_patch_store=patch_store,
    )

    assert seen["K"] == 8.0
    assert result["K"] == 8.0
    assert result["term_patch_fields"] == ["conversion_price"]
    assert result["term_patch_count"] == 1


def test_price_from_provider_announced_call_uses_redemption_horizon(monkeypatch):
    seen_init = {}
    seen_price = {}

    class FakePricer:
        def __init__(self, **kwargs):
            seen_init.update(kwargs)
            self.K = kwargs["K"]
            self.S0 = kwargs["S0"]
            self.T = 0.05
            self.ratio = kwargs["face_value"] / self.K

        def price(self, **kwargs):
            seen_price.update(kwargs)
            return 109.0

    monkeypatch.setattr(pricing_api, "UniversalCBPricer", FakePricer)
    terms = _base_terms(
        call_status="已公告强赎",
        call_announce_date=date(2026, 5, 1),
        last_trading_date=date(2026, 5, 20),
        call_redemption_date=date(2026, 5, 25),
        call_redemption_price=100.62,
    )

    result = pricing_api.price_from_provider(
        SimplePricingProvider(terms),
        "113001.SH",
        valuation_date=date(2026, 5, 10),
        p_down=0.15,
    )

    assert seen_init["maturity_date"] == date(2026, 5, 25)
    assert seen_init["call_no_redemption_until"] == date(2026, 5, 25)
    assert seen_init["redemption_price"] == 100.62
    assert seen_price["p_down"] == 0.0
    assert result["base_p_down"] == pytest.approx(0.15)
    assert result["effective_p_down"] == 0.0
    assert result["redemption_mode"] is True
    assert result["call_redemption_date"] == date(2026, 5, 25)
    assert result["call_redemption_price"] == 100.62
    assert any("已公告强赎" in text for text in result["risk_warnings"])


def test_price_from_provider_rejects_terminal_terms_before_market_fetch():
    class TerminalProvider(SimplePricingProvider):
        def hist_vol(self, stock_code, end_date, window_days):
            raise AssertionError("terminal bond should not fetch volatility")

        def get_stock_close(self, stock_code, on_date):
            raise AssertionError("terminal bond should not fetch stock close")

        def get_bond_history(self, bond_code, start, end):
            raise AssertionError("terminal bond should not fetch bond close")

    terms = _base_terms(maturity_date=date(2026, 5, 1))

    with pytest.raises(ValueError, match="已到期"):
        pricing_api.price_from_provider(
            TerminalProvider(terms),
            "113001.SH",
            valuation_date=date(2026, 5, 20),
        )


def test_price_from_provider_returns_status_dates(monkeypatch):
    class FakePricer:
        def __init__(self, **kwargs):
            self.K = kwargs["K"]
            self.S0 = kwargs["S0"]
            self.T = 1.0
            self.ratio = kwargs["face_value"] / self.K

        def price(self, **kwargs):
            return 102.0

    monkeypatch.setattr(pricing_api, "UniversalCBPricer", FakePricer)
    terms = _base_terms(
        call_status="不强赎",
        suspension_status="正常交易",
        last_trading_date=date(2026, 6, 20),
        delisting_date=date(2026, 6, 30),
    )

    result = pricing_api.price_from_provider(
        SimplePricingProvider(terms),
        "113001.SH",
        valuation_date=date(2026, 5, 20),
    )

    assert result["call_status"] == "不强赎"
    assert result["suspension_status"] == "正常交易"
    assert result["last_trading_date"] == date(2026, 6, 20)
    assert result["delisting_date"] == date(2026, 6, 30)
    assert result["maturity_date"] == date(2030, 1, 1)
    assert result["contractual_maturity_date"] == date(2030, 1, 1)


def test_price_from_provider_reports_down_reset_uplift(monkeypatch):
    class FakePricer:
        def __init__(self, **kwargs):
            self.K = kwargs["K"]
            self.S0 = kwargs["S0"]
            self.T = 1.0
            self.ratio = kwargs["face_value"] / self.K

        def price(self, **kwargs):
            return 108.0 if kwargs["p_down"] > 0 else 100.0

    monkeypatch.setattr(pricing_api, "UniversalCBPricer", FakePricer)

    result = pricing_api.price_from_provider(
        SimplePricingProvider(_base_terms()),
        "113001.SH",
        valuation_date=date(2026, 5, 20),
        p_down=0.15,
    )

    assert result["theoretical_price"] == 108.0
    assert result["no_down_price"] == 100.0
    assert result["down_reset_uplift"] == pytest.approx(8.0)
    assert result["down_reset_uplift_pct"] == pytest.approx(8.0 / 108.0)


def test_price_from_provider_marks_risky_single_bond_signal(monkeypatch):
    class FakePricer:
        def __init__(self, **kwargs):
            self.K = kwargs["K"]
            self.S0 = kwargs["S0"]
            self.T = 1.0
            self.ratio = kwargs["face_value"] / self.K

        def price(self, **kwargs):
            return 100.0

    monkeypatch.setattr(pricing_api, "UniversalCBPricer", FakePricer)
    terms = _base_terms(underlying_status="ST/退市风险")

    result = pricing_api.price_from_provider(
        SimplePricingProvider(terms),
        "113001.SH",
        valuation_date=date(2026, 5, 20),
    )

    assert result["model_signal_status"] == "不适合作为买入信号"
    assert any("正股风险状态" in text for text in result["risk_warnings"])


def test_price_from_provider_passes_putback_window(monkeypatch):
    seen = {}

    class FakePricer:
        def __init__(self, **kwargs):
            seen.update(kwargs)
            self.K = kwargs["K"]
            self.S0 = kwargs["S0"]
            self.T = 1.0
            self.ratio = kwargs["face_value"] / self.K

        def price(self, **kwargs):
            return 101.5

    monkeypatch.setattr(pricing_api, "UniversalCBPricer", FakePricer)
    terms = _base_terms(
        putback_start_date=date(2026, 6, 1),
        putback_end_date=date(2026, 6, 5),
        putback_price=100.8,
    )

    result = pricing_api.price_from_provider(
        SimplePricingProvider(terms),
        "113001.SH",
        valuation_date=date(2026, 5, 20),
    )

    assert seen["putback_start_date"] == date(2026, 6, 1)
    assert seen["putback_end_date"] == date(2026, 6, 5)
    assert seen["putback_price"] == 100.8
    assert result["putback_price"] == 100.8


def test_price_from_provider_passes_down_reset_trigger_ratio(monkeypatch):
    seen = {}

    class FakePricer:
        def __init__(self, **kwargs):
            seen.update(kwargs)
            self.K = kwargs["K"]
            self.S0 = kwargs["S0"]
            self.T = 1.0
            self.ratio = kwargs["face_value"] / self.K

        def price(self, **kwargs):
            return 102.0

    monkeypatch.setattr(pricing_api, "UniversalCBPricer", FakePricer)
    terms = _base_terms(down_reset_trigger_pct=85.0)

    result = pricing_api.price_from_provider(
        SimplePricingProvider(terms),
        "113001.SH",
        valuation_date=date(2026, 5, 20),
    )

    assert seen["down_reset_trigger_ratio"] == pytest.approx(0.85)
    assert result["down_reset_trigger_pct"] == 85.0
    assert result["down_reset_trigger_ratio"] == pytest.approx(0.85)
    assert result["down_reset_trigger_source"] == "terms"


def test_price_from_provider_defaults_down_reset_trigger_ratio(monkeypatch):
    seen = {}

    class FakePricer:
        def __init__(self, **kwargs):
            seen.update(kwargs)
            self.K = kwargs["K"]
            self.S0 = kwargs["S0"]
            self.T = 1.0
            self.ratio = kwargs["face_value"] / self.K

        def price(self, **kwargs):
            return 102.0

    monkeypatch.setattr(pricing_api, "UniversalCBPricer", FakePricer)

    result = pricing_api.price_from_provider(
        SimplePricingProvider(_base_terms()),
        "113001.SH",
        valuation_date=date(2026, 5, 20),
    )

    assert seen["down_reset_trigger_ratio"] == pytest.approx(0.85)
    assert result["down_reset_trigger_pct"] == 85.0
    assert result["down_reset_trigger_ratio"] == pytest.approx(0.85)
    assert result["down_reset_trigger_source"] == "default"


def test_price_from_provider_uses_rating_spread_floor(monkeypatch):
    seen_price = {}

    class FakePricer:
        def __init__(self, **kwargs):
            self.K = kwargs["K"]
            self.S0 = kwargs["S0"]
            self.T = 1.0
            self.ratio = kwargs["face_value"] / self.K

        def price(self, **kwargs):
            seen_price.update(kwargs)
            return 99.0

    monkeypatch.setattr(pricing_api, "UniversalCBPricer", FakePricer)
    terms = _base_terms(credit_rating="A")

    result = pricing_api.price_from_provider(
        SimplePricingProvider(terms),
        "113001.SH",
        valuation_date=date(2026, 5, 20),
        base_spread=0.03,
    )

    assert seen_price["base_spread"] == pytest.approx(0.06)
    assert result["rating_base_spread"] == pytest.approx(0.06)
    assert result["effective_base_spread"] == pytest.approx(0.06)


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


def test_batch_stock_cache_bond_history_fetch_does_not_hold_global_lock():
    class SlowBondHistoryProvider:
        name = "slow_bond_history"

        def __init__(self):
            self.started = threading.Event()
            self.release = threading.Event()
            self.dividend_calls = 0

        def get_bond_history(self, bond_code, start, end):
            self.started.set()
            self.release.wait(timeout=1.0)
            return [(end, 101.0)]

        def get_stock_dividend_yield(self, stock_code, on_date):
            self.dividend_calls += 1
            return 2.5

    inner = SlowBondHistoryProvider()
    cached = pricing_api._BatchStockCache(inner)
    start = date(2026, 4, 1)
    end = date(2026, 4, 28)

    with ThreadPoolExecutor(max_workers=2) as pool:
        bond_future = pool.submit(cached.get_bond_history, "113001.SH", start, end)
        assert inner.started.wait(timeout=1.0)
        dividend_future = pool.submit(cached.get_stock_dividend_yield, "000001.SZ", end)
        try:
            assert dividend_future.result(timeout=0.2) == 2.5
        finally:
            inner.release.set()
        assert bond_future.result(timeout=1.0) == [(end, 101.0)]

    assert inner.dividend_calls == 1
