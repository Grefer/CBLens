"""GUI 条款投影和无回售输入回归: 不建 Tk、不联网、不读真实条款库。"""
from dataclasses import replace
from datetime import date, datetime
from functools import partial
from types import SimpleNamespace

import pytest

from convertible_bond.cb_events import CBEventStore
from convertible_bond.data_providers.base import BondTerms
from convertible_bond.down_reset_overrides import DownResetOverrides, resolve_down_reset
from convertible_bond.gui.controllers import events, pricing, wind_sync
from convertible_bond.historical_terms import TermsPatch, TermsPatchStore, project_terms
from convertible_bond.pricer import UniversalCBPricer
from convertible_bond.pricing_api import build_pricer_kwargs
from tests.test_gui_widget_lifetime import _make_pricing_app, _StubVar


CODE = "113001.SH"
TODAY = date(2026, 2, 1)
ANCHOR = date(2026, 1, 10)


@pytest.fixture
def gui_terms(tmp_path, monkeypatch):
    terms = BondTerms(
        sec_name="合成转债", underlying_code="600001.SH",
        issue_date=date(2022, 2, 1), maturity_date=date(2028, 2, 1),
        face_value=100, conversion_price=10, redemption_price=108,
        coupon_rates=(0.003, 0.005, 0.01, 0.015, 0.018, 0.02),
        credit_rating="AA", outstanding_balance=5,
        call_trigger_pct=130, put_trigger_pct=70, put_obs_months=48,
    )
    store = TermsPatchStore(tmp_path / "patches.json")
    store.add_many([
        TermsPatch(CODE, date(2026, 1, 5),
                   {"conversion_price": 9, "credit_rating": "C"}),
        TermsPatch(CODE, date(2026, 1, 20), {"outstanding_balance": 7}),
    ])
    for module in (events, pricing, wind_sync):
        monkeypatch.setattr(module, "project_terms", partial(project_terms, patch_store=store))
        monkeypatch.setattr(module, "market_today", lambda: TODAY)

    app = _make_pricing_app()
    app.v_bond_code.set(CODE)
    for name in list(app._read_pricing_inputs()):
        setattr(app, "v_src_" + name[2:], _StubVar("手工"))
    for name in ("v_data_source", "v_ref_info", "v_ref_detail", "v_status",
                 "v_market_price", "v_vol_window"):
        setattr(app, name, _StubVar("Fake"))
    app.terms_cache = SimpleNamespace(
        get=lambda code: terms if code == CODE else None,
        has=lambda code: code == CODE,
        # 状态刷新晚于条款刷新, 不得拿全局戳裁掉 1/20 的新补丁。
        fetched_at=lambda code, source=None: datetime(
            2026, 1, 10 if source == "Wind" else 30),
    )
    app.event_store = CBEventStore(tmp_path / "events.json")
    monkeypatch.setattr(pricing, "resolve_down_reset", partial(
        resolve_down_reset, overrides=DownResetOverrides(tmp_path / "overrides.json"),
        event_store=app.event_store))
    app._cache_meta_source = lambda code: "Wind"
    app._terms_source_label = lambda origin: origin
    app._populate_down_reset_from_resolver = lambda *args: None
    app._auto_fill_p_down_from_current_x = lambda **kwargs: None
    app._fmt_pct = lambda value: f"{value:g}"
    app._maybe_sync_events_background = lambda *args: None
    app._refresh_terms_snapshot_card = lambda: None
    app._estimate_down_reset_floor_for_gui = lambda *args: None

    def set_field(var, text, source_var=None, source=None):
        var.set(text)
        if source_var is not None and source is not None:
            source_var.set(source)

    app._set_field = set_field
    app._fill_wind_data = wind_sync.WindSyncMixin._fill_wind_data.__get__(app)
    return app, terms, store


@pytest.mark.parametrize("entry", ["cache", "pricing", "events", "provider"])
def test_all_gui_projection_entries_use_the_snapshot_anchor(gui_terms, entry):
    app, terms, store = gui_terms
    anchor = ANCHOR
    if entry == "cache":
        wind_sync.WindSyncMixin._fill_from_cache(app, CODE)
        projected = app._current_projected_terms
        assert app.v_K.get() == "10.00"
    elif entry == "pricing":
        _, projected, projection = app._project_terms_for_pricing(TODAY, code=CODE)
        assert projection is not None   # 不许异常被 fallback 静默吞掉
    elif entry == "events":
        events.EventsMixin._apply_projected_terms_to_current_fields(app, CODE, terms)
        projected = app._current_projected_terms
        assert app.v_K.get() == "10.00"
    else:
        # 数据源可以刚刷新过, worker 必须用 provider 的锚而非旧缓存的锚。
        anchor = date(2026, 1, 25)
        provider = SimpleNamespace(
            name="Fake", get_bond_terms=lambda *args: terms,
            terms_as_of=lambda *args: anchor,
            get_stock_close=lambda *args: 10,
            get_stock_dividend_yield=lambda *args: 0,
            hist_vol=lambda *args: 0.28,
            get_risk_free_rate=lambda *args: 0.022,
            get_cashflow=lambda *args: None,
        )
        app._get_provider = lambda *args: provider
        app._provider_market_name = lambda provider: provider.name
        app._force_refresh_terms = False
        app._fetch_in_flight_code = CODE
        app._fetch_in_flight_source = "Fake"
        app.after = lambda delay, callback, *args: callback(*args)
        app._stop_progress = lambda: None
        app.btn_wind = SimpleNamespace(configure=lambda **kwargs: None)
        errors = []
        app._on_error = lambda *args: errors.append(args)
        wind_sync.WindSyncMixin._fetch_wind_worker(app, CODE, source_name="Fake")
        assert errors == []
        projected = app._current_projected_terms

    expected = project_terms(CODE, terms, TODAY, patch_store=store,
                             event_store=app.event_store, terms_as_of=anchor).terms
    assert projected == expected
    assert projected.conversion_price == 10
    assert projected.credit_rating == "AA"
    assert projected.outstanding_balance == (5 if entry == "provider" else 7)


def test_loading_no_put_bond_clears_previous_trigger_and_can_switch_back(gui_terms):
    app, terms, _ = gui_terms
    wind_sync.WindSyncMixin._fill_from_cache(app, CODE)
    assert app.v_put_ratio.get() == "70"
    no_put = replace(terms, put_trigger_pct=None, put_obs_months=None)
    app.terms_cache.get = lambda code: no_put
    wind_sync.WindSyncMixin._fill_from_cache(app, CODE)
    assert app.v_put_ratio.get() == "无"
    app.v_S0.set("6")
    app.v_sigma.set("28")
    app.v_p_down.set("0")
    params = app._collect_params(inputs=app._read_pricing_inputs())
    api, _ = build_pricer_kwargs(CODE, no_put, None, S0=6, valuation_date=TODAY)
    assert params["pricer"]["put_trigger_ratio"] is api["put_trigger_ratio"] is None
    price = UniversalCBPricer(**params["pricer"]).price(**params["model"])
    phantom_price = UniversalCBPricer(**dict(
        params["pricer"], put_trigger_ratio=0.7, put_active_years=2,
    )).price(**params["model"])
    assert phantom_price > price + 1

    app.terms_cache.get = lambda code: terms
    wind_sync.WindSyncMixin._fill_from_cache(app, CODE)
    app.v_S0.set("6")
    app.v_sigma.set("28")
    params = app._collect_params()
    assert params["pricer"]["put_trigger_ratio"] == 0.7
    assert params["pricer"]["put_active_years"] == 2


@pytest.mark.parametrize("text", ["", "无", " 无 "])
def test_no_put_manual_input_and_frozen_snapshot(text):
    app = _make_pricing_app()
    app.v_put_ratio.set(text)
    app.v_put_years.set("")   # 未启用回售时该字段不影响定价
    snapshot = app._read_pricing_inputs()
    app.v_put_ratio.set("70")
    assert app._collect_params(inputs=snapshot)["pricer"]["put_trigger_ratio"] is None


def test_numeric_manual_put_remains_editable_and_invalid_text_is_rejected():
    app = _make_pricing_app()
    app.v_put_ratio.set("65")
    assert app._collect_params()["pricer"]["put_trigger_ratio"] == 0.65
    app.v_put_ratio.set("不是数字")
    with pytest.raises(ValueError, match="回售触发"):
        app._collect_params()


@pytest.mark.parametrize("with_window", [False, True])
def test_no_put_display_does_not_invent_trigger_but_preserves_announced_window(with_window):
    app = _make_pricing_app()
    terms = BondTerms()
    if with_window:
        terms = replace(terms, putback_start_date=date(2026, 1, 30),
                        putback_end_date=date(2026, 2, 5), putback_price=100.5)
    summary, color = app._term_put_summary(TODAY, 10, None, 2, terms=terms)
    expected = "回售申报中" if with_window else "无股价触发回售"
    assert summary.startswith(expected)
    rendered = []
    app._set_term_event = lambda kind, **kwargs: rendered.append(kwargs)
    app._update_put_event(val_date=TODAY, s0=6, k=10, put_ratio=None, put_years=2,
                          put_trigger=None, summary=summary, color=color,
                          issue_date=date(2022, 2, 1), terms=terms)
    assert rendered[0]["status"] == expected
    if not with_window:
        assert rendered[0]["progress"] is None
