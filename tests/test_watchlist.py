import json
from datetime import date

from convertible_bond import watchlist


def test_add_to_watchlist_preserves_upcoming_metadata(tmp_path, monkeypatch):
    path = tmp_path / "watchlist.json"
    monkeypatch.setattr(watchlist, "watchlist_path", lambda: path)

    items, added = watchlist.add_to_watchlist([
        {
            "bond_code": "123269.SZ",
            "bond_name": "金杨转债",
            "stock_code": "301210.SZ",
            "underlying_name": "金杨精密",
            "issue_date": date(2026, 5, 11),
            "listing_date": date(2026, 5, 11),
            "tradable_date": date(2026, 5, 11),
            "days_to_trade": 2,
            "K": 39.8,
            "credit_rating": "AA-",
            "outstanding_balance": 9.8,
            "trading_status": "pending",
        }
    ])

    assert added == 1
    assert items[0]["listing_date"] == date(2026, 5, 11)
    assert items[0]["tradable_date"] == date(2026, 5, 11)
    assert items[0]["K"] == 39.8

    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)

    saved = payload["items"][0]
    assert saved["listing_date"] == "2026-05-11"
    assert saved["tradable_date"] == "2026-05-11"
    assert saved["credit_rating"] == "AA-"


def test_add_to_watchlist_enriches_existing_entries(tmp_path, monkeypatch):
    path = tmp_path / "watchlist.json"
    monkeypatch.setattr(watchlist, "watchlist_path", lambda: path)
    watchlist.add_to_watchlist([
        {
            "bond_code": "113702.SH",
            "bond_name": "斯达转债",
            "stock_code": "603290.SH",
        }
    ])

    items, added = watchlist.add_to_watchlist([
        {
            "bond_code": "113702.SH",
            "listing_date": date(2026, 5, 11),
            "tradable_date": date(2026, 5, 11),
            "credit_rating": "AA+",
        }
    ])

    assert added == 0
    assert items[0]["listing_date"] == date(2026, 5, 11)
    assert items[0]["tradable_date"] == date(2026, 5, 11)
    assert items[0]["credit_rating"] == "AA+"

    loaded = watchlist.load_watchlist()
    assert loaded[0]["listing_date"] == "2026-05-11"
    assert loaded[0]["tradable_date"] == "2026-05-11"
