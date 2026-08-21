"""代码输入框的候选来源 — cb_data 本地索引匹配与排序.

cb_data.json 只增不删 (同步只是不再*写入*终止态的债), 1000+ 只里近一半已退市/
到期; 按代码排序会让 2017 年那批最早的退市债霸占可见的前几行。这里守住
"存续优先 + 终止态标注" 的排序契约。
"""
from datetime import date

from convertible_bond.gui.controllers.wind_sync import WindSyncMixin


def _searcher(data):
    class _Cache:
        _data = data

    app = WindSyncMixin()
    app.terms_cache = _Cache()
    return app


# 到期日远在过去/未来, 让用例不依赖运行当天的日期
_PAST = "2000-01-01"
_FUTURE = "2099-01-01"


def test_live_bonds_rank_before_terminal_ones():
    app = _searcher({
        "123001.SZ": {"sec_name": "蓝标转债(退市)", "maturity_date": _PAST},
        "123002.SZ": {"sec_name": "国祯转债(退市)", "maturity_date": _PAST},
        "123284.SZ": {"sec_name": "强达转债", "maturity_date": _FUTURE},
    })

    codes = [code for code, _label in app._search_bond_index("123")]

    assert codes == ["123284.SZ", "123001.SZ", "123002.SZ"]


def test_prefix_match_still_beats_substring_match_among_live():
    app = _searcher({
        "123284.SZ": {"sec_name": "强达转债", "maturity_date": _FUTURE},
        "128123.SZ": {"sec_name": "某某转债", "maturity_date": _FUTURE},
    })

    codes = [code for code, _label in app._search_bond_index("123")]

    assert codes == ["123284.SZ", "128123.SZ"]


def test_terminal_bond_without_name_suffix_gets_labelled():
    """31 只已到期的债简称里没有"(退市)", 不标出来就跟存续债长得一样."""
    app = _searcher({
        "110073.SH": {"sec_name": "国投转债", "maturity_date": _PAST},
    })

    (_code, label), = app._search_bond_index("110073")

    assert label == "110073.SH  国投转债 · 已到期"


def test_terminal_bond_with_name_suffix_is_not_double_tagged():
    app = _searcher({
        "123001.SZ": {"sec_name": "蓝标转债(退市)", "maturity_date": _PAST},
    })

    (_code, label), = app._search_bond_index("蓝标")

    assert label == "123001.SZ  蓝标转债(退市)"


def test_terminal_reason_prefers_name_then_delisting_then_maturity():
    on_date = date(2026, 8, 20)
    reason = WindSyncMixin._bond_index_terminal_reason

    # 强赎转股提前摘牌: delisting_date 常为空, 只有简称带后缀
    assert reason({"sec_name": "蓝标转债(退市)"}, on_date) == "已退市"
    assert reason({"sec_name": "蓝帆转债", "delisting_date": "2026-01-05"}, on_date) == "已退市"
    assert reason({"sec_name": "某转债", "last_trading_date": "2026-08-19"}, on_date) == "已过最后交易日"
    assert reason({"sec_name": "国投转债", "maturity_date": "2026-07-24"}, on_date) == "已到期"
    # 最后交易日就是当天 → 还能交易
    assert reason({"sec_name": "某转债", "last_trading_date": "2026-08-20"}, on_date) is None
    assert reason({"sec_name": "强达转债", "maturity_date": "2032-08-19"}, on_date) is None
    assert reason({"sec_name": "无日期转债"}, on_date) is None


def test_empty_query_returns_nothing():
    app = _searcher({"123284.SZ": {"sec_name": "强达转债"}})
    assert app._search_bond_index("") == []
    assert app._search_bond_index("   ") == []


def test_bundle_meta_and_non_dict_entries_are_skipped():
    app = _searcher({
        "_bundle_meta": {"updated_at": "2026-08-21T11:13:57"},
        "123284.SZ": "坏数据",
        "123285.SZ": {"sec_name": "正常转债", "maturity_date": _FUTURE},
    })

    assert [c for c, _ in app._search_bond_index("123")] == ["123285.SZ"]
