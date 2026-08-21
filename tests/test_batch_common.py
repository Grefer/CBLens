from datetime import timedelta

from convertible_bond.gui.tabs.batch_common import _is_new_bond, _resolve_row_tag
from convertible_bond.gui.tabs.batch_watchlist import (
    _format_days_to_trade,
    _format_listing_cell,
)
from convertible_bond.market_time import market_today


def test_listed_tradable_bond_is_not_marked_new():
    row = {
        "bond_code": "118067.SH",
        "status": "ok",
        "is_tradable": True,
        "trading_status": "tradable",
        "listing_date": market_today() - timedelta(days=5),
        "tradable_date": market_today() - timedelta(days=5),
    }

    assert _is_new_bond(row) is False
    assert _resolve_row_tag(row) is None


def test_future_tradable_bond_is_marked_new():
    row = {
        "bond_code": "123999.SZ",
        "status": "ok",
        "is_tradable": False,
        "trading_status": "pending",
        "listing_date": market_today() + timedelta(days=2),
        "tradable_date": market_today() + timedelta(days=2),
    }

    assert _is_new_bond(row) is True
    assert _resolve_row_tag(row) == "new"


def test_issued_but_unlisted_bond_is_marked_new_after_pricing():
    """已发行未上市的新债定价成功后仍要保留新债高亮 (状态按估值日重算)."""
    row = {
        "bond_code": "123284.SZ",
        "status": "ok",
        "is_tradable": False,
        "trading_status": "pending",
        "listing_date": None,
        "tradable_date": None,
        "theoretical_price": 130.35,
    }

    assert _is_new_bond(row) is True
    assert _resolve_row_tag(row) == "new"


def test_watchlist_cells_show_pending_listing_as_undetermined():
    """上市日未公告 → 显示"待定"而不是"—", 后者读起来像缺数据."""
    entry = {
        "bond_code": "123284.SZ",
        "trading_status": "pending",
        "listing_date": None,
        "tradable_date": None,
        "days_to_trade": 3,  # 上一轮扫描留下的旧值, 不能显示
    }

    assert _format_listing_cell(entry, "listing_date") == "待定"
    assert _format_listing_cell(entry, "tradable_date") == "待定"
    assert _format_days_to_trade(entry) == "待定"


def test_watchlist_cells_keep_dash_for_plain_missing_dates():
    entry = {"bond_code": "128009.SZ", "trading_status": "tradable"}

    assert _format_listing_cell(entry, "listing_date") == "—"
    assert _format_days_to_trade(entry) == "—"


def test_watchlist_days_to_trade_uses_known_listing_date():
    entry = {
        "bond_code": "123281.SZ",
        "trading_status": "pending",
        "listing_date": market_today() + timedelta(days=4),
        "tradable_date": market_today() + timedelta(days=4),
    }

    assert _format_listing_cell(entry, "tradable_date") == (market_today() + timedelta(days=4)).isoformat()
    assert _format_days_to_trade(entry) == "+4"


def test_days_to_trade_shows_already_tradable_instead_of_negative():
    """"距交易 -3" 没有意义 — 可交易日已过就是能买了."""
    entry = {
        "bond_code": "123284.SZ",
        "trading_status": "tradable",
        "tradable_date": market_today() - timedelta(days=3),
    }
    assert _format_days_to_trade(entry) == "已可交易"

    today = {"bond_code": "123284.SZ", "tradable_date": market_today()}
    assert _format_days_to_trade(today) == "已可交易"

    stale_days = {"bond_code": "123284.SZ", "days_to_trade": -3}
    assert _format_days_to_trade(stale_days) == "已可交易"
