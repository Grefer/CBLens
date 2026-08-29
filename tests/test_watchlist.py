import json
from datetime import date

import pytest

from convertible_bond import watchlist
from convertible_bond.market_time import market_today


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


class _Var:
    """最小 StringVar 替身: GUI 起不来, 但控制器逻辑可以脱离 Tk 单测."""

    def __init__(self, value=""):
        self._v = value

    def get(self):
        return self._v

    def set(self, value):
        self._v = value


class _Button:
    """CTkButton 替身: 只记 configure 进来的文案/配色."""

    def __init__(self):
        self.state = {}

    def configure(self, **kw):
        self.state.update(kw)

    @property
    def text(self):
        return self.state.get("text")


def _pricing_app(bond_terms=None, *, watchlist_items=None):
    from convertible_bond.gui.controllers.pricing import PricingMixin

    class _Cache:
        def __init__(self, terms):
            self._terms = terms

        def has(self, code):
            return self._terms is not None

        def get(self, code):
            return self._terms

    app = PricingMixin()
    app._normalize_bond_code = lambda text: str(text).strip().upper()
    app.terms_cache = _Cache(bond_terms)
    app.v_bond_code = _Var("123284.SZ")
    app.v_K = _Var("84.04")
    app.v_result = _Var("—")
    app.v_market_price = _Var("")
    app.v_status = _Var("")
    app.btn_add_watchlist = _Button()
    app._batch_watchlist = list(watchlist_items or [])
    # after/after_cancel 替身: 记下回调, 由测试决定何时"到点"
    app._pending = {}
    app.after = lambda ms, cb: app._pending.setdefault("cb", cb) or "flash-id"
    app.after_cancel = lambda _id: app._pending.pop("cb", None)
    return app


def test_pricing_tab_add_to_watchlist_stores_result_snapshot(tmp_path, monkeypatch):
    """定价页 ⭐: 单债钻取完能直接跟踪, 快照口径与批量页一致."""
    from convertible_bond.data_providers import BondTerms

    path = tmp_path / "watchlist.json"
    monkeypatch.setattr(watchlist, "watchlist_path", lambda: path)

    app = _pricing_app(BondTerms(
        sec_name="强达转债",
        underlying_code="301628.SZ",
        issue_date=date(2026, 8, 19),
        conversion_price=84.04,
        credit_rating="AA-",
        maturity_date=date(2032, 8, 19),
    ))
    app.v_result.set("112.500")
    app.v_market_price.set("108.000")

    app._add_current_to_watchlist()

    entry = watchlist.load_watchlist()[0]
    assert entry["bond_code"] == "123284.SZ"
    assert entry["bond_name"] == "强达转债"
    assert entry["stock_code"] == "301628.SZ"
    assert entry["snapshot_theoretical_price"] == 112.5
    assert entry["snapshot_market_price"] == 108.0
    # 与批量页同一符号约定: 负值 = 市价低于理论价
    assert entry["snapshot_deviation"] == pytest.approx((108.0 - 112.5) / 112.5)
    assert "⭐" in app.v_status.get()


def test_pricing_tab_add_to_watchlist_without_pricing_result(tmp_path, monkeypatch):
    """还没点计算就加入: 允许, 但不写快照, 并在状态栏说明."""
    path = tmp_path / "watchlist.json"
    monkeypatch.setattr(watchlist, "watchlist_path", lambda: path)

    app = _pricing_app()
    app._add_current_to_watchlist()

    entry = watchlist.load_watchlist()[0]
    assert entry["bond_code"] == "123284.SZ"
    assert entry["K"] == 84.04            # 缺条款时回落到页面上的 K
    assert "snapshot_theoretical_price" not in entry
    assert "尚无理论价快照" in app.v_status.get()


def test_pricing_tab_add_to_watchlist_rejects_bad_code(tmp_path, monkeypatch):
    path = tmp_path / "watchlist.json"
    monkeypatch.setattr(watchlist, "watchlist_path", lambda: path)
    warned = []
    monkeypatch.setattr(
        "convertible_bond.gui.controllers.pricing.messagebox.showwarning",
        lambda *a, **k: warned.append(a),
    )

    app = _pricing_app()
    app.v_bond_code.set("不是代码")
    app._add_current_to_watchlist()

    assert warned
    assert watchlist.load_watchlist() == []


def test_watchlist_button_shows_result_then_settles_on_tracked(tmp_path, monkeypatch):
    """按钮自己就是反馈: 状态栏在窗口右下角 11px, 只改那行等于没反馈."""
    path = tmp_path / "watchlist.json"
    monkeypatch.setattr(watchlist, "watchlist_path", lambda: path)

    app = _pricing_app()
    app._refresh_watchlist_button()
    assert app.btn_add_watchlist.text == app._WATCHLIST_BTN_IDLE

    app._add_current_to_watchlist()
    assert app.btn_add_watchlist.text == "✓ 已加入关注池"

    # 闪烁到点后回落成常驻的"已关注"
    app._pending.pop("cb")()
    assert app.btn_add_watchlist.text == app._WATCHLIST_BTN_TRACKED


def test_watchlist_button_reflects_existing_entry(tmp_path, monkeypatch):
    """切到一只已关注的债时, 按钮直接显示已关注, 不用点一下才知道."""
    path = tmp_path / "watchlist.json"
    monkeypatch.setattr(watchlist, "watchlist_path", lambda: path)

    app = _pricing_app(watchlist_items=[{"bond_code": "123284.SZ"}])
    app._refresh_watchlist_button()
    assert app.btn_add_watchlist.text == app._WATCHLIST_BTN_TRACKED

    app.v_bond_code.set("128009.SZ")
    app._refresh_watchlist_button()
    assert app.btn_add_watchlist.text == app._WATCHLIST_BTN_IDLE


def test_watchlist_button_flash_survives_refresh(tmp_path, monkeypatch):
    """闪烁期间的代码变更不能把结果提示冲掉."""
    path = tmp_path / "watchlist.json"
    monkeypatch.setattr(watchlist, "watchlist_path", lambda: path)

    app = _pricing_app()
    app._add_current_to_watchlist()
    app._refresh_watchlist_button()
    assert app.btn_add_watchlist.text == "✓ 已加入关注池"


def test_explicit_none_clears_stale_metadata(tmp_path, monkeypatch):
    """重扫时显式给 None 要能洗掉旧值 — 否则一次写错的日期永远留在关注池里."""
    path = tmp_path / "watchlist.json"
    monkeypatch.setattr(watchlist, "watchlist_path", lambda: path)
    watchlist.add_to_watchlist([{
        "bond_code": "123284.SZ",
        "bond_name": "强达转债",
        "tradable_date": date(2026, 8, 19),   # 旧的错值 (拿起息日当可交易日)
        "days_to_trade": 3,
    }])

    # 新一轮扫描: 上市日仍未公告 → 两个字段都该是"没有值"
    items, added = watchlist.add_to_watchlist([{
        "bond_code": "123284.SZ",
        "bond_name": "强达转债",
        "tradable_date": None,
        "days_to_trade": None,
        "trading_status": "pending",
    }])

    assert added == 0
    entry = items[0]
    assert "tradable_date" not in entry
    assert "days_to_trade" not in entry
    assert entry["trading_status"] == "pending"
    assert entry["bond_name"] == "强达转债"


def test_absent_key_leaves_existing_metadata_alone(tmp_path, monkeypatch):
    """"没提这个字段" ≠ "这个字段没有值" — 局部更新不能误删已有信息."""
    path = tmp_path / "watchlist.json"
    monkeypatch.setattr(watchlist, "watchlist_path", lambda: path)
    watchlist.add_to_watchlist([{
        "bond_code": "123281.SZ",
        "bond_name": "中仑转债",
        "tradable_date": date(2026, 8, 24),
        "credit_rating": "AA-",
    }])

    items, _ = watchlist.add_to_watchlist([{
        "bond_code": "123281.SZ",
        "market_price": 108.0,
    }])

    entry = items[0]
    # 二次调用会先从磁盘重载, 日期回来是 ISO 字符串
    assert entry["tradable_date"] == "2026-08-24"
    assert entry["credit_rating"] == "AA-"
    assert entry["market_price"] == 108.0


def test_pricing_tab_add_rederives_stale_trading_metadata(tmp_path, monkeypatch):
    """cb_data 里的交易状态是写入那天推断的, 抄进关注池前要按今天重算."""
    from convertible_bond.data_providers import BondTerms

    path = tmp_path / "watchlist.json"
    monkeypatch.setattr(watchlist, "watchlist_path", lambda: path)

    app = _pricing_app(BondTerms(
        sec_name="强达转债",
        underlying_code="301628.SZ",
        issue_date=market_today(),
        listing_date=None,
        tradable_date=market_today(),      # 旧推断残留: 起息日被当成可交易日
        is_tradable=True,
        trading_status="tradable",
        conversion_price=84.04,
        maturity_date=date(2032, 8, 19),
    ))
    app._add_current_to_watchlist()

    entry = watchlist.load_watchlist()[0]
    assert "tradable_date" not in entry     # 上市日未公告 → 不该有可交易日
    assert entry["trading_status"] == "pending"
    assert entry["is_tradable"] is False


# ── 手删记忆 (dismissed) ────────────────────────────────────────
def test_auto_scan_must_not_resurrect_a_bond_the_user_removed(tmp_path, monkeypatch):
    """右键删掉的在途新债, 后台扫描不许加回来.

    这是「新债不自动退出关注池, 只靠右键手删」那条口径的唯一出口。此前
    remove_from_watchlist 不留痕、add_to_watchlist 对不在池里的 code 无条件 append,
    于是用户删完、状态栏报「已从关注池移除 1 只」, 下次开 GUI 它就带着新的 added_at
    回来了 —— 用户的操作被系统无声撤销。
    """
    monkeypatch.setattr(watchlist, "watchlist_path", lambda: tmp_path / "w.json")
    item = {"bond_code": "123284.SZ", "bond_name": "强达转债"}

    watchlist.add_to_watchlist([item], source="auto")
    assert len(watchlist.load_watchlist()) == 1

    watchlist.remove_from_watchlist(["123284.SZ"])
    assert watchlist.load_dismissed() == {"123284.SZ"}

    # 后台再扫三轮 (首屏 / 缓存加载 / 批量重算前) 都不许把它加回来
    for _ in range(3):
        items, added = watchlist.add_to_watchlist([item], source="auto")
        assert added == 0 and items == []


def test_scanning_for_new_issues_is_an_explicit_action_and_undoes_the_removal(tmp_path, monkeypatch):
    """「🆕 扫新债」是用户显式点的 —— 它必须能把手删过的债重新加回来,
    否则手删就是一张没有出口的单程票。"""
    monkeypatch.setattr(watchlist, "watchlist_path", lambda: tmp_path / "w.json")
    item = {"bond_code": "123284.SZ", "bond_name": "强达转债"}

    watchlist.add_to_watchlist([item], source="auto")
    watchlist.remove_from_watchlist(["123284.SZ"])

    items, added = watchlist.add_to_watchlist([item], source="manual")
    assert added == 1
    assert [i["bond_code"] for i in items] == ["123284.SZ"]
    assert watchlist.load_dismissed() == set()      # 手删标记随之解除


def test_dismissed_survives_a_plain_save(tmp_path, monkeypatch):
    """save_watchlist 只关心 items 的调用方不该顺手把手删记录抹掉 ——
    抹掉的表现就是"我删的债又回来了", 与这个集合要解决的问题一模一样。"""
    monkeypatch.setattr(watchlist, "watchlist_path", lambda: tmp_path / "w.json")
    watchlist.add_to_watchlist([{"bond_code": "A"}, {"bond_code": "B"}], source="auto")
    watchlist.remove_from_watchlist(["A"])

    watchlist.save_watchlist([{"bond_code": "B"}])          # 不传 dismissed
    assert watchlist.load_dismissed() == {"A"}


def test_undo_remove_restores_the_original_added_at(tmp_path, monkeypatch):
    """撤销走 save_watchlist 原样写回, 不走 add_to_watchlist ——
    后者会给条目重写 added_at, 把"我什么时候开始关注它"抹掉。"""
    monkeypatch.setattr(watchlist, "watchlist_path", lambda: tmp_path / "w.json")
    watchlist.add_to_watchlist([{"bond_code": "A", "bond_name": "甲"}], source="manual")
    before = watchlist.load_watchlist()
    original_added_at = before[0]["added_at"]

    watchlist.remove_from_watchlist(["A"])
    assert watchlist.load_watchlist() == []

    restored = watchlist.undo_remove(before)
    assert restored[0]["added_at"] == original_added_at
    assert watchlist.load_dismissed() == set()
    assert watchlist.load_watchlist()[0]["added_at"] == original_added_at


def test_legacy_file_without_dismissed_still_loads(tmp_path, monkeypatch):
    """存量 watchlist.json 没有 dismissed 键, 读回来必须是空集而不是炸掉."""
    import json
    path = tmp_path / "w.json"
    path.write_text(json.dumps({"saved_at": "x", "items": [{"bond_code": "A"}]}),
                    encoding="utf-8")
    monkeypatch.setattr(watchlist, "watchlist_path", lambda: path)
    assert watchlist.load_dismissed() == set()
    assert [i["bond_code"] for i in watchlist.load_watchlist()] == ["A"]
