import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import pytest

from convertible_bond import pricing_api
from convertible_bond.cb_events import CBEvent, CBEventStore
from convertible_bond.data_providers import BondTerms
from convertible_bond.down_reset_overrides import PROPOSED_EFFECTIVE_LAG_DAYS
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


def test_price_from_provider_uses_wrapped_provider_history_stores(monkeypatch, tmp_path):
    provider = SimplePricingProvider(_base_terms())
    provider.event_store = CBEventStore(tmp_path / "custom-events.json")
    provider.patch_store = TermsPatchStore(tmp_path / "custom-patches.json")
    provider.patch_store.add_many([
        TermsPatch(
            bond_code="199999.SZ",
            effective_date=date(2026, 5, 5),
            fields={"conversion_price": 9.0},
        )
    ])
    proposal_date = date(2026, 5, 10)
    provider.event_store.add_many([
        CBEvent(
            bond_code="199999.SZ",
            event_date=proposal_date,
            event_type="down_reset_proposed",
            raw_title="董事会提议向下修正转股价格",
        )
    ])
    constructor_calls = []

    class FakePricer:
        def __init__(self, **kwargs):
            constructor_calls.append(kwargs)
            self.K = kwargs["K"]
            self.S0 = kwargs["S0"]
            self.T = 1.0
            self.ratio = kwargs["face_value"] / self.K

        def price(self, **kwargs):
            return 104.0 if kwargs["p_down"] > 0 else 100.0

    monkeypatch.setattr(pricing_api, "UniversalCBPricer", FakePricer)

    result = pricing_api.price_from_provider(
        pricing_api._BatchStockCache(provider),
        "199999.SZ",
        valuation_date=date(2026, 5, 20),
        sigma=0.20,
    )

    expected_date = proposal_date + timedelta(days=PROPOSED_EFFECTIVE_LAG_DAYS)
    assert result["down_reset_proposed_date"] == proposal_date
    assert result["down_reset_scheduled_date"] == expected_date
    assert result["K"] == pytest.approx(9.0)
    assert constructor_calls[0]["scheduled_reset_date"] == expected_date


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


def test_sort_batch_results_tolerates_non_numeric_deviation():
    rows = [
        {"bond_code": "BAD", "deviation": {"not": "numeric"}},
        {"bond_code": "NAN", "deviation": float("nan")},
        {"bond_code": "LOW", "deviation": -0.10},
        {"bond_code": "STR", "deviation": "0.20"},
    ]

    sorted_rows = pricing_api._sort_batch_results(rows)

    assert [row["bond_code"] for row in sorted_rows] == ["LOW", "STR", "BAD", "NAN"]


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


def test_accrued_interest_caps_at_maturity_and_matches_pricer():
    """应计利息共享口径守护: pricing_api 兜底实现与 pricer 类方法逐值一致, 且按到期日封顶。

    历史上 pricing_api._accrued_interest 是独立实现且无到期日封顶 (残段期会漂移);
    现两者共用 pricer.build_coupon_periods/accrued_interest_amount, 本测试防止再分叉。
    """
    from datetime import date as _date

    from convertible_bond.pricer import UniversalCBPricer
    from convertible_bond.pricing_api import _accrued_interest

    issue = _date(2020, 1, 1)
    maturity = _date(2022, 7, 1)   # 2.5 年: 第三期为半年残段, 封顶语义在此显形
    rates = (0.01, 0.02, 0.03)

    # 残段内: 两套口径逐值一致, 且等于手算 (第三期 2022-01-01 起息, 费率 3%)
    on = _date(2022, 6, 1)
    api_accrued = _accrued_interest(
        face_value=100.0, coupon_rates=rates,
        issue_date=issue, maturity_date=maturity, on_date=on)
    pricer = UniversalCBPricer(
        S0=10.0, K=10.0, current_date=_date(2021, 1, 2), maturity_date=maturity,
        issue_date=issue, coupon_rates=rates)
    assert api_accrued == pytest.approx(pricer.accrued_interest(on))
    assert api_accrued == pytest.approx(100 * 0.03 * (on - _date(2022, 1, 1)).days / 365)

    # 超过到期日: 按到期日封顶 (旧实现无封顶, 会按整年累到 3.0 元)
    beyond = _accrued_interest(
        face_value=100.0, coupon_rates=rates,
        issue_date=issue, maturity_date=maturity, on_date=_date(2023, 1, 1))
    capped = 100 * 0.03 * (maturity - _date(2022, 1, 1)).days / 365
    assert beyond == pytest.approx(capped)
    assert beyond == pytest.approx(pricer.accrued_interest(_date(2023, 1, 1)))


def test_no_down_price_uses_the_same_grid_as_theoretical(monkeypatch):
    """down_reset_uplift 是两个价的**差**, 跨网格相减会把离散化误差混进信号。

    实测同债同参数、仅 (150,400) vs (500,2000) 之差中位 0.005 / P90 0.13 / 最大 0.47 元,
    而 |uplift| 中位只有 0.69 —— 曾让 282 只里 33 只出现伪负号, 而下修权只会增加价值。
    """
    calls: list[tuple[int, int, float]] = []

    class FakePricer:
        def __init__(self, **kwargs):
            self.K = kwargs["K"]
            self.S0 = kwargs["S0"]
            self.T = 1.0
            self.ratio = kwargs["face_value"] / self.K

        def price(self, **kwargs):
            calls.append((kwargs["M"], kwargs["N"], kwargs["p_down"]))
            return 108.0 if kwargs["p_down"] > 0 else 100.0

    monkeypatch.setattr(pricing_api, "UniversalCBPricer", FakePricer)
    pricing_api.price_from_provider(
        SimplePricingProvider(_base_terms()),
        "113001.SH",
        valuation_date=date(2026, 5, 20),
        p_down=0.15,
        M=500,
        N=2000,
    )

    theo_calls = [c for c in calls if c[2] > 0]
    no_down_calls = [c for c in calls if c[2] == 0]
    assert theo_calls and no_down_calls
    assert theo_calls[0][:2] == (500, 2000)
    assert no_down_calls[0][:2] == theo_calls[0][:2], (
        f"no_down 价用了 {no_down_calls[0][:2]}, headline 用了 {theo_calls[0][:2]} —— "
        f"两价之差必须同网格, 否则 uplift 的符号不可信"
    )


def test_every_accrued_interest_call_site_satisfies_the_signature():
    """``_accrued_interest`` 的每个调用点都必须传齐 keyword-only 必填参数。

    这条守护的是一类**静态检查与测试双双看不见**的缺陷: 给一个函数加 keyword-only
    且无默认值的参数时漏改某个调用点。ruff 的 E9+F 不检查关键字实参 (F821 只看名字),
    而 GUI 在无头环境起不来 —— 于是 ``gui/controllers/pricing.py`` 那处漏传
    ``maturity_date`` 在 957 条用例全绿的情况下活了下来, 表现是用户打开某只**已公告强赎
    但公告未给赎回价**的债时状态栏弹
    「计算失败: _accrued_interest() missing 1 required keyword-only argument」。

    它当时是潜伏的 (那类债当天恰好都已退市), 后来变成实打实的: 复核时库里有 3 只
    (113667.SH / 127067.SZ / 123112.SZ) 赎回日在未来且 call_redemption_price 为空。
    """
    import ast
    import inspect
    import pathlib

    from convertible_bond import pricing_api

    sig = inspect.signature(pricing_api._accrued_interest)
    required_kwonly = {
        name for name, p in sig.parameters.items()
        if p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is inspect.Parameter.empty
    }
    assert required_kwonly, "签名里没有 keyword-only 必填参数, 这条守护就失去意义了"

    root = pathlib.Path(pricing_api.__file__).parent
    call_sites = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (func.id if isinstance(func, ast.Name)
                    else func.attr if isinstance(func, ast.Attribute) else None)
            if name != "_accrued_interest":
                continue
            passed = {kw.arg for kw in node.keywords if kw.arg}
            splat = any(kw.arg is None for kw in node.keywords)   # **kwargs 无法静态判定
            call_sites.append((path.relative_to(root.parent), node.lineno, passed, splat))

    assert len(call_sites) >= 2, f"只找到 {len(call_sites)} 个调用点, 扫描可能失效了"
    for rel, lineno, passed, splat in call_sites:
        if splat:
            continue
        missing = required_kwonly - passed
        assert not missing, f"{rel}:{lineno} 漏传 {sorted(missing)} —— 运行到这一支必然 TypeError"


def test_terms_close_fallback_does_not_produce_a_deviation():
    """条款库兜底价不参与 deviation —— 它没有 as-of, 可以任意旧。

    ``_latest_bond_close_with_provenance`` 取不到行情序列时回落 ``terms.close``,
    而那个字段没有 as-of: 日升转债库里的 99.994 是 2021 年撤销发行前的值。拿它比今天的
    理论价, 算出来的偏差没有含义。

    此前 ``market_price_source`` 只有一个消费者 (关注池「数据状态」列的文案),
    deviation / ``snapshot_coverage`` / 策略层一个都不看它 —— 于是行情源挂一下午,
    全池用三周前的价, 覆盖率照报 100%, 一条假快照进版本库的估值基线。

    **市价本身保留** (真实成交过的价格, 展示有用), 作废的只是由它派生的偏差。

    走**真实调用路径**: 第一版只扫源码文本 + 本地复述判据, 而变异体保留了那行赋值、
    只删掉 ``and not stale_fallback``, 于是照样全绿 —— 复述不是测试。
    """
    import math

    from convertible_bond.pricing_api import _batch_result_from_provider

    class _NoHistoryProvider(SimplePricingProvider):
        """行情序列取不到 → 触发 terms.close 兜底那一档。"""

        def get_bond_history(self, bond_code, start, end):
            return []

    terms = _base_terms(close=188.8)          # 条款库里躺着一个没有 as-of 的旧价

    kw = dict(valuation_date=date(2026, 8, 31), r=0.022, base_spread=0.03,
              distress_k=0.05, p_down=0.25, vol_window_days=21,
              sigma=None, q=None, M=120, N=300, pricer_overrides={})

    row = _batch_result_from_provider(_NoHistoryProvider(terms), "123456.SZ", **kw)

    assert row["status"] == "ok"
    assert row["market_price_source"] == "terms_close", row.get("market_price_source")
    assert row["market_price"] == pytest.approx(188.8), "市价不该被丢掉, 它是真实成交过的价"
    assert math.isnan(row["deviation"]), "兜底价算出了 deviation"
    assert math.isnan(row["undervaluation_rate"])

    # 对照: 有真实行情时照常算
    ok = _batch_result_from_provider(SimplePricingProvider(terms), "123456.SZ", **kw)
    assert ok["market_price_source"] == "history"
    assert not math.isnan(ok["deviation"])

    # 而覆盖率闸现在看得见这一档了 —— 那才是它要保护的东西
    from convertible_bond.market_valuation import snapshot_coverage
    assert snapshot_coverage([row]) == (0, 1)
    assert snapshot_coverage([ok]) == (1, 1)


def test_historical_provider_never_carries_a_close_from_another_date():
    """历史路径上 ``close`` **无条件**按估值日重取, 取不到就置 None。

    它与 ``strip_current_status_fields`` 管的状态字段不是一回事: 那批由
    ``strip_fallback_status`` 控制 (standard 口径刻意为 False), 而 ``close`` 是市场价格,
    任何情况下都不该把别的日期的价带进这个估值日。

    漏掉的是 fallback 路: history_store 没有该日快照时退回**今天**的条款, 而 standard
    口径不剥它 —— 今天的收盘价就成了历史价, 再被当成 ``terms_close`` 兜底价用掉。
    """
    from datetime import date as _date

    from convertible_bond.data_providers import BondTerms
    from convertible_bond.historical_terms import HistoricalBondDataProvider

    class _Inner:
        name = "fake"

        def get_bond_terms(self, code, valuation_date):
            # 今天的条款, 带着**今天**的收盘价
            return BondTerms(sec_name="测试转债", conversion_price=10.0,
                             maturity_date=_date(2030, 1, 1), close=188.8)

        def get_bond_history(self, code, start, end):
            return []                      # 该估值日没有行情

        def terms_as_of(self, code, valuation_date):
            return None

    for strip in (True, False):            # 两种 history_mode 都不许带过来
        provider = HistoricalBondDataProvider(_Inner(), strip_fallback_status=strip)
        terms = provider.get_bond_terms("123456.SZ", _date(2022, 6, 30))
        assert terms.close is None, f"strip_fallback_status={strip} 时把今天的收盘价带进了历史"
