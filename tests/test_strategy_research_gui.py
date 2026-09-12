"""研究界面与实际运行配置的接线，使用无 Tk 的替身验证完整动作。"""
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import date
import json
from types import SimpleNamespace

import pytest

from convertible_bond.batch_pricing import AdmissionFilterConfig
from convertible_bond.strategy_backtest import PDEStrategyConfig
from convertible_bond.gui.controllers import strategy_research as gui
from convertible_bond.gui.controllers.strategy_common import strategy_selection_description
from convertible_bond.gui.controllers.strategy_render import StrategyRenderMixin
from convertible_bond.strategy_backtest_csv import write_strategy_backtest_csv


class Var:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


def completed_result():
    cfg = asdict(PDEStrategyConfig(top_n=15, min_relative_cheapness=0.07))
    return {
        "start_date": date(2025, 1, 1), "end_date": date(2025, 3, 31),
        "config": dict(cfg), "summary": {"total_return": 0.1},
        "periods": [{"start_date": date(2025, 1, 1), "end_date": date(2025, 3, 31)}],
        "run_settings": {
            "strategy_config": cfg, "strategy": dict(cfg),
            "pool": {"bond_codes": ["113001.SH"], "engine_mode": "static"},
            "pricing": {"q": 0.0, "r": 0.022, "base_spread": 0.03},
            "admission_filter": asdict(AdmissionFilterConfig()),
            "data_source": "akshare", "history_mode": "标准",
            "provenance": {"captured_at": "unknown", "source": {"status": "unknown"}},
        },
    }


def test_research_request_uses_saved_actual_settings_without_mutating_result():
    result = completed_result()
    request = gui.research_request_from_result(result)
    assert request["config"].top_n == 15
    assert request["config"].min_relative_cheapness == 0.07
    assert request["params"]["q"] == 0.0
    request["params"]["q"] = 0.02
    request["codes"].append("113002.SH")
    assert result["run_settings"]["pricing"]["q"] == 0.0
    assert result["run_settings"]["pool"]["bond_codes"] == ["113001.SH"]


def test_research_rejects_old_snapshot_instead_of_filling_with_current_form_defaults():
    result = completed_result()
    del result["run_settings"]["strategy_config"]
    with pytest.raises(ValueError, match="重跑"):
        gui.research_request_from_result(result)


def test_rule_description_distinguishes_relative_gate_and_explicit_absolute_gate():
    cfg = asdict(PDEStrategyConfig())
    text = strategy_selection_description(cfg)
    assert "比市场中位便宜至少" in text and "绝对偏差" not in text
    cfg.update(min_relative_cheapness=None, max_deviation=0.0)
    text = strategy_selection_description(cfg)
    assert "市场中位" not in text and "绝对偏差≤0%" in text


def test_research_worker_persists_and_exposes_each_variants_actual_config(monkeypatch, tmp_path):
    baseline = completed_result()
    request = gui.research_request_from_result(baseline)
    variant = deepcopy(baseline)
    variant["config"] = asdict(replace(request["config"], top_n=18))
    report = {"base_config": request["config"], "variants": [{"name": "持仓18"}],
              "results": {"持仓18": variant}}
    monkeypatch.setattr(gui, "run_strategy_neighborhood", lambda *a, **k: report)
    monkeypatch.setattr(gui, "strategy_run_provenance", lambda: {"source": {"sha256": "run-start"}})
    monkeypatch.setattr(gui, "data_dir", lambda *a: tmp_path)
    errors = []
    monkeypatch.setattr(gui, "show_error", lambda *a: errors.append(a))
    app = gui.StrategyResearchMixin()
    flushes = []
    app._build_strategy_provider = lambda *a, **k: SimpleNamespace(flush=lambda: flushes.append(1))
    app.after = lambda delay, fn, *a: fn(*a)
    shown = []
    app._show_strategy_research_report = shown.append
    app._research_finish = lambda: None
    app.v_st_research_status = Var()
    app.v_st_status = Var()
    app._strategy_research_worker("neighborhood", request)
    assert not errors
    assert flushes == [1]
    settings = shown[0]["results"]["持仓18"]["run_settings"]
    assert settings["strategy_config"]["top_n"] == 18
    assert settings["strategy"]["top_n"] == 18
    assert settings["provenance"]["source"]["sha256"] == "run-start"
    stored = json.loads((tmp_path / "neighborhood_latest.json").read_text())
    assert stored["base_config"]["top_n"] == 15
    assert stored["results"]["持仓18"]["run_settings"]["strategy_config"]["top_n"] == 18


def test_frozen_gui_uses_completed_variant_not_current_form(monkeypatch, tmp_path):
    app = gui.StrategyResearchMixin()
    app._last_strategy_bt_result = completed_result()
    app.v_st_top_n = Var("99")
    app.v_st_experiment = Var("暂无冻结方案")
    app.v_st_research_status = Var()
    app._show_strategy_experiment_status = lambda: None
    app._refresh_strategy_experiments = lambda *a: None
    calls = []
    monkeypatch.setattr(gui, "data_dir", lambda *a: tmp_path)
    def freeze(*args, **kwargs):
        calls.append(kwargs)
        return {"experiment_id": "saved"}
    monkeypatch.setattr(gui, "freeze_experiment", freeze)
    app._freeze_strategy_experiment()
    assert len(calls) == 1
    assert calls[0]["config"]["top_n"] == 15
    assert calls[0]["engine_params"]["pricing"]["q"] == 0.0


def test_future_oos_end_is_rejected_before_launching_a_worker(monkeypatch):
    app = gui.StrategyResearchMixin()
    app.v_st_experiment = Var("saved")
    app.v_st_end = Var("2025-10-01")
    app.v_st_research_status = Var()
    app._strategy_experiments = {"saved": {"path": "/unused/manifest.json"}}
    monkeypatch.setattr(gui, "market_today", lambda: date(2025, 9, 10))
    monkeypatch.setattr(gui, "load_experiment", lambda p: {"frozen_date": "2025-09-01"})
    app._research_start = lambda text: pytest.fail("未来区间不应启动任务")
    app._run_strategy_oos()
    assert "早于今天" in app.v_st_research_status.get()


def test_same_name_frozen_versions_remain_individually_selectable(monkeypatch, tmp_path):
    items = [{"name": "估值偏差·M频·Top10", "version": 1,
              "experiment_id": f"experiment_20250910T000000_{suffix}"}
             for suffix in ("new123", "old456")]
    monkeypatch.setattr(gui, "list_experiments", lambda root: items)
    monkeypatch.setattr(gui, "data_dir", lambda *a: tmp_path)
    app = gui.StrategyResearchMixin()
    app.v_st_experiment = Var()
    app.v_st_research_status = Var()
    app._strategy_experiment_menu = SimpleNamespace(configure=lambda **k: None)
    app._refresh_strategy_experiments(items[0]["experiment_id"])
    assert len(app._strategy_experiments) == 2
    selected = app._strategy_experiments[app.v_st_experiment.get()]
    assert selected["experiment_id"] == items[0]["experiment_id"]


def test_open_and_blocked_positions_show_valuation_instead_of_a_fictitious_exit():
    app = StrategyRenderMixin()
    for state in ("open", "blocked"):
        label, text = app._strategy_position_exit_text({
            "position_status": state, "exit_date": None,
            "mark_date": date(2025, 3, 28), "end_price": 85.0,
        })
        assert label in {"持有", "待成交"}
        assert "估值" in text and "退出" not in text
        assert "2025-03-28" in text and "85.00" in text


def test_csv_preserves_cash_ledger_and_unavailable_exit(tmp_path):
    result = completed_result()
    result["periods"][0].update({
        "accounting_basis": "cash_ledger_v1", "available_cash": 0.2,
        "receivable_value": 0.01, "blocked_count": 1,
        "positions": [{"bond_code": "113001.SH", "position_status": "blocked", "exit_date": None,
                       "mark_date": date(2025, 3, 28), "end_price": 85, "q_source": "fixed"}],
        "ledger_entries": [{"date": date(2025, 1, 2), "bond_code": "113001.SH", "kind": "buy",
                            "amount": 0.8, "cash_change": -0.8}],
        "cashflow_warnings": ["支付日期待核实"],
    })
    path = tmp_path / "result.csv"
    write_strategy_backtest_csv(path, result)
    text = path.read_text(encoding="utf-8-sig")
    for expected in ("# ledger_entries", "# cashflow_warnings", "position_status", "q_source",
                     "blocked", "fixed", "支付日期待核实", "-0.80000000"):
        assert expected in text
