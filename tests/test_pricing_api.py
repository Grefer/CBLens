import threading
import time

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
    assert progress[-1] == (3, 3)


def test_batch_price_from_provider_keeps_legacy_worker_default(monkeypatch):
    seen = {}

    def fake_threaded(provider, bond_codes, **kwargs):
        seen["max_workers"] = kwargs["max_workers"]
        return []

    monkeypatch.setattr(pricing_api, "batch_price_from_provider_threaded", fake_threaded)

    pricing_api.batch_price_from_provider(DummyProvider(), ["A"])

    assert seen["max_workers"] == 4
