"""股息率缓存与来源的离线契约：真实零、失败零和实时历史必须可区分。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
import threading
from types import SimpleNamespace

import pandas as pd
import pytest

from convertible_bond.backtest_disk_cache import DiskCacheProvider
from convertible_bond.cache import CachedBondDataProvider, CachingDataProvider
from convertible_bond.data_providers.akshare import AkshareDataProvider
from convertible_bond.data_providers.base import BondTerms, DataProvider
from convertible_bond.data_providers.dividends import (
    DividendYieldCache, DividendYieldObservation, fetch_dividend_observation,
)
from convertible_bond.data_providers.wind import WindDataProvider
from convertible_bond.historical_terms import HistoricalBondDataProvider
from convertible_bond.pricing_api import _BatchStockCache, price_from_provider


PAST = date(2024, 6, 28)
TODAY = date(2026, 9, 10)


class YieldProvider(DataProvider):
    name = "fixture"

    def __init__(self, value=2.5, kind="historical"):
        self.value, self.kind, self.calls = value, kind, 0

    def get_stock_dividend_yield_observation(self, stock_code, on_date):
        self.calls += 1
        if isinstance(self.value, Exception):
            raise self.value
        return DividendYieldObservation(
            self.value, self.kind if self.value is not None else "unavailable", "fixture.endpoint",
            as_of=on_date if self.kind == "historical" else TODAY,
            fetched_on=TODAY, reason="没有可用记录" if self.value is None else "")

    def get_bond_terms(self, bond_code, valuation_date):
        return BondTerms(sec_name="测试债", underlying_code="000001.SZ",
                         issue_date=date(2020, 1, 1), maturity_date=date(2030, 1, 1),
                         conversion_price=10.0, redemption_price=107.0,
                         face_value=100.0, coupon_rates=(0.01,) * 10)

    def get_stock_close(self, stock_code, on_date):
        return 10.0

    def get_stock_history(self, stock_code, start, end):
        return [(end, 10.0)]

    def get_bond_history(self, bond_code, start, end):
        return [(end, 105.0)]


def _disk(tmp_path, provider, clock, namespace=None, today=TODAY):
    return DiskCacheProvider(provider, tmp_path, today=today, now=lambda: clock[0], namespace=namespace)


def test_historical_dividend_survives_restart_with_source_and_real_zero(tmp_path):
    clock, original = [1000.0], YieldProvider(0.0)
    first = _disk(tmp_path, original, clock)
    observed = first.get_stock_dividend_yield_observation("000001.SZ", PAST)
    first.flush()
    clock[0] += 86400 * 30
    changed = YieldProvider(4.0)
    second = _disk(tmp_path, changed, clock)
    assert second.get_stock_dividend_yield_observation("000001.SZ", PAST) == observed
    assert observed.value_pct == 0.0
    assert observed.kind == "historical"
    assert observed.source == "fixture.endpoint"
    assert observed.as_of == PAST
    assert changed.calls == 0
    assert second.get_stock_dividend_yield("000001.SZ", PAST + timedelta(days=1)) == 4.0
    assert changed.calls == 1


def test_snapshot_expires_and_is_never_loaded_as_historical(tmp_path):
    clock, source = [1000.0], YieldProvider(2.0, "realtime_snapshot")
    first = _disk(tmp_path, source, clock)
    original = first.get_stock_dividend_yield_observation("000001.SZ", PAST)
    first.flush()
    assert next(iter(first._dividends.records)).startswith(f"snapshot|{TODAY.isoformat()}|")
    second_source = YieldProvider(3.0, "realtime_snapshot")
    second = _disk(tmp_path, second_source, clock)
    assert second.get_stock_dividend_yield_observation("000001.SZ", PAST) == original
    assert second_source.calls == 0
    clock[0] += DividendYieldCache.SNAPSHOT_TTL_SECONDS
    refreshed = second.get_stock_dividend_yield_observation("000001.SZ", PAST)
    assert refreshed.kind == "realtime_snapshot" and refreshed.value_pct == 3.0
    assert refreshed.as_of == TODAY
    assert second_source.calls == 1


def test_snapshot_collection_day_changes_even_before_ttl(tmp_path):
    clock, source = [1000.0], YieldProvider(2.0, "realtime_snapshot")
    first = _disk(tmp_path, source, clock)
    first.get_stock_dividend_yield("000001.SZ", PAST)
    first.flush()
    next_source = YieldProvider(3.0, "realtime_snapshot")
    second = _disk(tmp_path, next_source, clock, today=TODAY + timedelta(days=1))
    assert second.get_stock_dividend_yield("000001.SZ", PAST) == 3.0
    assert next_source.calls == 1


def test_snapshot_does_not_mask_another_dates_available_history(tmp_path):
    clock, source = [1000.0], YieldProvider(2.0, "realtime_snapshot")
    cached = _disk(tmp_path, source, clock)
    cached.get_stock_dividend_yield("000001.SZ", PAST)
    source.kind, source.value = "historical", 1.5
    observed = cached.get_stock_dividend_yield_observation("000001.SZ", PAST + timedelta(days=1))
    assert observed.kind == "historical" and observed.value_pct == 1.5
    assert source.calls == 2


@pytest.mark.parametrize("missing", [None, RuntimeError("temporary failure")])
def test_negative_cache_has_ttl_and_recovers_across_restart(tmp_path, missing):
    clock, source = [1000.0], YieldProvider(missing)
    first = _disk(tmp_path, source, clock)
    observed = first.get_stock_dividend_yield_observation("000001.SZ", PAST)
    assert observed.kind == "unavailable" and observed.value_pct is None
    assert observed.reason
    first.flush()
    recovered = YieldProvider(1.5)
    second = _disk(tmp_path, recovered, clock)
    assert second.get_stock_dividend_yield("000001.SZ", PAST) is None
    assert recovered.calls == 0
    clock[0] += DividendYieldCache.MISSING_TTL_SECONDS
    assert second.get_stock_dividend_yield("000001.SZ", PAST) == 1.5
    assert recovered.calls == 1


def test_today_historical_value_is_not_permanently_frozen(tmp_path):
    clock, source = [1000.0], YieldProvider(1.0)
    cached = _disk(tmp_path, source, clock)
    cached.get_stock_dividend_yield("000001.SZ", TODAY)
    source.value = 3.0
    clock[0] += DividendYieldCache.SNAPSHOT_TTL_SECONDS
    assert cached.get_stock_dividend_yield("000001.SZ", TODAY) == 3.0


def test_cache_source_namespace_and_identity_invalidation_include_dividends(tmp_path):
    clock = [1000.0]
    first = _disk(tmp_path, YieldProvider(1.0), clock, namespace="A")
    first.get_stock_dividend_yield("000001.SZ", PAST)
    first.flush()
    second = _disk(tmp_path, YieldProvider(3.0), clock, namespace="B")
    # 只重取条款也必须让旧股息率文件失效，不能套上新身份继续使用。
    second.get_bond_terms("110001.SH", PAST)
    second.flush()
    assert not (tmp_path / "dividend_yields.json").exists()
    third = _disk(tmp_path, YieldProvider(3.0), clock, namespace="B")
    assert third.get_stock_dividend_yield("000001.SZ", PAST) == 3.0
    assert third.inner.calls == 1


def test_source_names_do_not_collide_in_runtime_cache():
    cache = DividendYieldCache(today=lambda: TODAY)
    first, second = YieldProvider(1.0), YieldProvider(3.0)
    second.name = "another-source"
    assert cache.get(first, "000001.SZ", PAST).value_pct == 1.0
    assert cache.get(second, "000001.SZ", PAST).value_pct == 3.0


def test_nested_caches_do_not_extend_the_original_snapshot_ttl():
    clock = [datetime(2026, 9, 10, tzinfo=timezone.utc).timestamp()]
    calls = []
    def fetch(code, day):
        calls.append(1)
        return DividendYieldObservation(2.0, "realtime_snapshot", "source", as_of=TODAY,
            fetched_at=datetime.fromtimestamp(clock[0], timezone.utc).isoformat(), fetched_on=TODAY)
    source = SimpleNamespace(name="source", get_stock_dividend_yield_observation=fetch)
    inner = DividendYieldCache(now=lambda: clock[0], today=lambda: TODAY)
    outer = DividendYieldCache(now=lambda: clock[0], today=lambda: TODAY)
    wrapped = SimpleNamespace(name="source+cache", get_stock_dividend_yield_observation=
                              lambda code, day: inner.get(source, code, day))
    inner.get(source, "000001.SZ", PAST)
    clock[0] += DividendYieldCache.SNAPSHOT_TTL_SECONDS - 10
    outer.get(wrapped, "000001.SZ", PAST)
    assert len(calls) == 1
    clock[0] += 11
    outer.get(wrapped, "000001.SZ", PAST)
    assert len(calls) == 2


def test_concurrent_bonds_share_one_stock_dividend_observation():
    provider = YieldProvider()
    cache = _BatchStockCache(provider)
    with ThreadPoolExecutor(max_workers=8) as pool:
        observations = list(pool.map(lambda _: cache.get_stock_dividend_yield_observation(
            "000001.SZ", PAST), range(24)))
    assert provider.calls == 1
    assert all(item is observations[0] for item in observations)
    assert observations[0].kind == "historical"


@pytest.mark.parametrize("wrapper,attribute", [(CachingDataProvider, "inner"),
    (CachedBondDataProvider, "market"), (HistoricalBondDataProvider, "inner")])
def test_decorators_preserve_observation_without_loading_real_local_stores(wrapper, attribute):
    provider = wrapper.__new__(wrapper)
    setattr(provider, attribute, YieldProvider(0.0))
    observation = provider.get_stock_dividend_yield_observation("000001.SZ", PAST)
    assert observation.kind == "historical" and observation.value_pct == 0.0
    assert observation.source == "fixture.endpoint" and observation.as_of == PAST


def test_legacy_scalar_provider_is_never_assumed_to_supply_history():
    provider = SimpleNamespace(name="legacy", get_stock_dividend_yield=lambda code, day: 0)
    observation = fetch_dividend_observation(provider, "000001.SZ", PAST)
    assert observation.kind == "realtime_snapshot" and observation.value_pct == 0.0
    assert observation.as_of is None


def test_future_dated_observation_is_rejected():
    provider = SimpleNamespace(name="bad", get_stock_dividend_yield_observation=lambda code, day:
                               DividendYieldObservation(2.0, "historical", "bad", as_of=TODAY))
    with pytest.raises(ValueError, match="晚于估值日"):
        fetch_dividend_observation(provider, "000001.SZ", PAST)


@pytest.mark.parametrize("kind,value,expected", [("historical", 0.0, "historical"),
    ("historical", None, "fallback_zero"), ("realtime_snapshot", 2.0, "realtime_snapshot")])
def test_pricing_rows_retain_zero_fallback_and_snapshot_sources(kind, value, expected):
    provider = YieldProvider(value, kind)
    row = price_from_provider(provider, "110001.SH", valuation_date=PAST, sigma=0.2,
                              r=0.02, base_spread=0.02, p_down=0, M=30, N=30,
                              estimate_down_reset_floor=False)
    assert row["q_source"] == expected
    assert row["q"] == (value or 0.0) / 100
    assert row["q_observed_pct"] == value
    assert row["q_provider"] == "fixture.endpoint"
    assert row["q_fetched_at"]


def test_fixed_q_does_not_fetch_any_dividend_data():
    provider = YieldProvider(RuntimeError("must not fetch"))
    row = price_from_provider(provider, "110001.SH", valuation_date=PAST, sigma=0.2,
                              r=0.02, base_spread=0.02, p_down=0, q=0.03, M=30, N=30,
                              estimate_down_reset_floor=False)
    assert row["q"] == 0.03 and row["q_source"] == "fixed"
    assert row["q_observed_pct"] is None and row["q_fetched_at"] is None
    assert provider.calls == 0


def _ak(fake):
    provider = AkshareDataProvider.__new__(AkshareDataProvider)
    provider._ak = fake
    return provider


def test_akshare_historical_dates_are_cut_off_and_undated_indicators_are_snapshots():
    fake = SimpleNamespace(stock_a_indicator_lg=lambda symbol: pd.DataFrame({
        "trade_date": [PAST - timedelta(days=1), PAST + timedelta(days=1)], "dv_ttm": [1.5, 3.0]}))
    provider = _ak(fake)
    observation = provider.get_stock_dividend_yield_observation("000001.SZ", PAST)
    assert observation.kind == "historical" and observation.value_pct == 1.5
    assert observation.as_of == PAST - timedelta(days=1)
    fake.stock_a_indicator_lg = lambda symbol: pd.DataFrame({"dv_ttm": [3.0]})
    observation = provider.get_stock_dividend_yield_observation("000001.SZ", PAST)
    assert observation.kind == "realtime_snapshot" and observation.as_of is None


@pytest.mark.parametrize("has_yield", [False, True])
def test_akshare_reuses_one_full_market_snapshot_for_all_stocks_and_dates(has_yield):
    calls = []
    def spot():
        calls.append(1)
        raw = {"代码": ["000001", "000002"]}
        if has_yield:
            raw["股息率"] = [0.0, 2.5]
        return pd.DataFrame(raw)
    provider = _ak(SimpleNamespace(stock_zh_a_spot_em=spot))
    for day in (PAST, PAST + timedelta(days=30)):
        for code in ("000001.SZ", "000002.SZ"):
            observed = provider.get_stock_dividend_yield_observation(code, day)
            assert observed.kind == ("realtime_snapshot" if has_yield else "unavailable")
    assert len(calls) == 1


def test_akshare_concurrent_stock_requests_share_snapshot_and_missing_fields_retry(monkeypatch):
    from convertible_bond.data_providers import akshare as module
    clock, calls, ready = [100.0], [], threading.Barrier(8)
    def spot():
        calls.append(1)
        raw = {"代码": ["000001"]}
        if len(calls) > 1:
            raw["股息率"] = [2.0]
        return pd.DataFrame(raw)
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    provider = _ak(SimpleNamespace(stock_zh_a_spot_em=spot))
    def observe(_):
        ready.wait(timeout=5)
        return provider.get_stock_dividend_yield_observation("000001.SZ", PAST)
    with ThreadPoolExecutor(max_workers=8) as pool:
        observations = list(pool.map(observe, range(8)))
    assert all(observed.value_pct is None for observed in observations)
    assert len(calls) == 1
    clock[0] += DividendYieldCache.MISSING_TTL_SECONDS
    recovered = provider.get_stock_dividend_yield_observation("000001.SZ", PAST)
    assert recovered.value_pct == 2.0 and recovered.kind == "realtime_snapshot"
    assert len(calls) == 2


def test_wind_observation_marks_trade_date_without_changing_scalar_candidate_path():
    provider = WindDataProvider.__new__(WindDataProvider)
    provider.get_stock_dividend_yield = lambda stock_code, on_date: 0.0
    observation = provider.get_stock_dividend_yield_observation("000001.SZ", PAST)
    assert observation.kind == "historical" and observation.as_of == PAST
    assert observation.value_pct == 0.0 and observation.source.startswith("Wind.wss.")
