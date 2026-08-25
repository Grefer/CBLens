from datetime import timedelta

import inspect

from convertible_bond.gui.tabs import batch as batch_tab
from convertible_bond.gui.tabs import batch_watchlist as watchlist_tab
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


# ── 批量页列 / 定价参数的静态守护 ──
#
# GUI 在测试环境跑不起真实渲染, 但这些是纯数据结构与源码常量, 可以机器兜底。

def test_every_batch_column_has_a_getter_and_a_stretch_weight():
    """列表加了列却忘了 getter, 运行期才 KeyError; F821 也扫不到 (是字典键不是名字)。"""
    for preset in (batch_tab._BATCH_COLS_FULL, batch_tab._BATCH_COLS_SIMPLE):
        for name, _width in preset:
            assert name in batch_tab._BATCH_COL_GETTERS, f"{name} 缺 getter"
            assert name in batch_tab._BATCH_COL_STRETCH_WEIGHTS, f"{name} 缺列宽权重"


def test_batch_column_getters_tolerate_missing_and_nan_values():
    """定价失败行 / 旧缓存行不带新字段, 取值函数必须退化成 '—' 而不是抛异常。"""
    import math
    for row in ({}, {"status": "error"},
                {"status": "ok", "relative_deviation": math.nan,
                 "double_low": None, "down_reset_robust_edge_value": math.nan}):
        for name, getter in batch_tab._BATCH_COL_GETTERS.items():
            assert isinstance(getter(row), str), f"{name} 在缺值行上没返回字符串"


def test_simple_view_leads_with_cross_sectional_signals_not_score():
    """简洁视图的决策位必须是相对偏差 —— 机会分在 92% 的行上与错定价无关。

    机会分没有删除, 切「完整」仍可查; 但它不该占着人第一眼看的那一列。
    """
    simple = [name for name, _ in batch_tab._BATCH_COLS_SIMPLE]
    assert "相对偏差" in simple
    assert "机会分" not in simple
    assert "机会分" in [name for name, _ in batch_tab._BATCH_COLS_FULL]
    assert simple.index("相对偏差") < simple.index("偏差(%)")


def test_both_pricing_entries_request_pde_down_reset_signals():
    """主池与关注池两条定价路径都要开 PDE 下修信号, 否则「下修优势」列一半是空的。

    这个 kwarg 走 ``**pricer_overrides``, 漏传不报错只静默不算 —— 正是它此前在批量页
    缺席、让稳健下修优势在缓存里 0/280 有值的原因。
    """
    for module in (batch_tab, watchlist_tab):
        src = inspect.getsource(module)
        assert "compute_pde_signals=True" in src, f"{module.__name__} 未开 PDE 下修信号"
