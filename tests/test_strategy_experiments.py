"""冻结实验的离线验证：真实未来边界、版本/来源、完整前缀对账与原子只增记录。"""
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import date, datetime, timezone
from pathlib import Path
import json

import pytest

from convertible_bond import strategy_experiments as experiments
from convertible_bond.strategy_backtest import PDEStrategyConfig, build_rebalance_schedule


POOL = ["113001.SH", "113002.SH"]
PARAMS = {
    "pricing": {"r": 0.022, "q": 0.0, "M": 100, "N": 200},
    "admission_filter": {"min_outstanding_balance": 1.0, "min_credit_rating": "AA-",
                         "min_turnover_amount": None},
    "source": "offline", "history_mode": "标准",
    "pool": {"bond_codes": POOL, "code_count": 2, "engine_mode": "static"},
}


def result_for(config, start, end):
    schedule = build_rebalance_schedule(start, end, config.rebalance_freq)
    periods = []
    equity = 1.0
    curve = [{"date": start, "equity": equity}]
    for index, (first, last) in enumerate(zip(schedule, schedule[1:])):
        ret = (index + 1) * 0.01
        equity *= 1 + ret
        periods.append({
            "start_date": first, "end_date": last, "period_return": ret,
            "equity": equity, "cash_weight": 0.0, "turnover": 0.1,
            "positions": [{"bond_code": POOL[0], "entry_date": first,
                           "exit_date": last, "period_return": ret,
                           "q_fetched_at": "2025-01-01T00:00:00"}],
            "cache_stats": {"hits": 0},
        })
        curve.append({"date": last, "equity": equity})
    return {
        "start_date": start, "end_date": end,
        "config": {**asdict(config), "accounting_basis": "close_return"},
        "periods": periods, "equity_curve": curve,
        "summary": {"final_equity": equity, "total_return": equity - 1},
        "accounting_basis": {"return_basis": "close_return"},
    }


@pytest.fixture
def clock(monkeypatch):
    state = {"now": datetime(2025, 1, 15, 6, tzinfo=timezone.utc)}
    monkeypatch.setattr(experiments, "_utc_now", lambda: state["now"])
    return state


@pytest.fixture
def frozen(tmp_path, clock):
    config = PDEStrategyConfig(top_n=2)
    result = result_for(config, date(2024, 1, 31), date(2024, 12, 31))
    manifest = experiments.freeze_experiment(
        tmp_path / "strategy_experiments", name="月频原方案", config=asdict(config),
        engine_params=PARAMS, bond_codes=POOL, result=result,
    )
    return manifest, config


def advance(clock):
    clock["now"] = datetime(2025, 5, 5, 6, tzinfo=timezone.utc)


def append(manifest, config, end=date(2025, 2, 28), **kwargs):
    result = kwargs.pop("result", result_for(config, date(2025, 1, 31), end))
    return experiments.append_oos_result(
        manifest["path"], result=result, config=config,
        engine_params=PARAMS, bond_codes=POOL, **kwargs,
    )


def test_freeze_records_actual_configuration_pool_and_unknown_provenance(frozen):
    manifest, config = frozen
    assert manifest["config"] == experiments._jsonable(asdict(config))
    assert manifest["engine_params"] == PARAMS
    assert manifest["bond_codes"] == POOL
    assert manifest["frozen_date"] == "2025-01-15"
    assert manifest["oos_start_date"] == "2025-01-31"
    assert manifest["provenance"]["source"]["status"] == "unknown"
    assert manifest["record_count"] == 0
    assert Path(manifest["path"]).parent.parent.name == "strategy_experiments"
    assert (Path(manifest["path"]).parent / "training_result.json").exists()


def test_freeze_rejects_old_incomplete_config_and_unmatched_result(tmp_path, clock):
    config = PDEStrategyConfig()
    result = result_for(config, date(2024, 1, 31), date(2024, 12, 31))
    with pytest.raises(ValueError, match="完整 strategy_config"):
        experiments.freeze_experiment(tmp_path, name="old", config={"top_n": 10},
                                      engine_params=PARAMS, bond_codes=POOL, result=result)
    with pytest.raises(ValueError, match="不一致: top_n"):
        experiments.freeze_experiment(tmp_path, name="changed", config=replace(config, top_n=20),
                                      engine_params=PARAMS, bond_codes=POOL, result=result)


def test_freeze_rejects_future_training_and_mismatched_run_settings(tmp_path, clock):
    config = PDEStrategyConfig()
    result = result_for(config, date(2024, 1, 31), date(2025, 1, 31))
    with pytest.raises(ValueError, match="已经完成"):
        experiments.freeze_experiment(tmp_path, name="future", config=config,
                                      engine_params=PARAMS, bond_codes=POOL, result=result)
    result = result_for(config, date(2024, 1, 31), date(2024, 12, 31))
    result["run_settings"] = {"pricing": {"q": 0.04}}
    with pytest.raises(ValueError, match="运行条件不一致: pricing"):
        experiments.freeze_experiment(tmp_path, name="mismatch", config=config,
                                      engine_params=PARAMS, bond_codes=POOL, result=result)


def test_freeze_accepts_completed_calculation_ending_today_and_marks_it(tmp_path, clock):
    config = PDEStrategyConfig()
    result = result_for(config, date(2024, 12, 31), date(2025, 1, 15))
    manifest = experiments.freeze_experiment(
        tmp_path, name="今天运行", config=config, engine_params=PARAMS, bond_codes=POOL, result=result,
    )
    assert manifest["training"]["includes_freeze_day"] is True
    assert manifest["training"]["observed_through"] == "2025-01-15"
    assert manifest["oos_start_date"] == "2025-01-31"


def test_changed_configuration_creates_new_version_without_touching_parent(frozen, clock):
    manifest, config = frozen
    parent_path = Path(manifest["path"])
    before = parent_path.read_bytes()
    changed = replace(config, top_n=3)
    new = experiments.freeze_experiment(
        parent_path.parent.parent, name="邻域后新方案", config=changed,
        engine_params=PARAMS, bond_codes=POOL,
        result=result_for(changed, date(2024, 1, 31), date(2024, 12, 31)),
        parent_id=manifest["experiment_id"],
    )
    assert new["version"] == 2 and new["parent_id"] == manifest["experiment_id"]
    assert new["experiment_id"] != manifest["experiment_id"]
    assert parent_path.read_bytes() == before
    assert len(experiments.list_experiments(parent_path.parent.parent)) == 2


def test_provenance_hashes_actual_data_and_distinguishes_gui_from_economic_code(tmp_path, clock):
    package = tmp_path / "convertible_bond"
    (package / "gui").mkdir(parents=True)
    engine = package / "pricer.py"
    gui = package / "gui" / "app.py"
    engine.write_text("VALUE = 1\n")
    gui.write_text("LABEL = 'first'\n")
    data = tmp_path / "terms.json"
    data.write_text('{"price": 100}')
    paths = {"terms": data, "missing": tmp_path / "missing.json"}
    first = experiments.capture_provenance(paths, tmp_path)
    assert first["data"]["terms"]["status"] == "known"
    assert first["data"]["missing"]["status"] == "unknown"
    assert first["source"]["git_commit"] == "unknown"
    gui.write_text("LABEL = 'second'\n")
    second = experiments.capture_provenance(paths, tmp_path)
    assert second["source"]["sha256"] != first["source"]["sha256"]
    assert second["source"]["economic_sha256"] == first["source"]["economic_sha256"]
    engine.write_text("VALUE = 2\n")
    third = experiments.capture_provenance(paths, tmp_path)
    assert third["source"]["economic_sha256"] != first["source"]["economic_sha256"]
    missing = experiments.capture_provenance(source_root=tmp_path / "no-source")
    assert missing["source"]["status"] == "unknown"


def test_append_is_idempotent_and_only_adds_new_prefix_periods(frozen, clock):
    manifest, config = frozen
    advance(clock)
    first = append(manifest, config)
    assert first["appended_periods"] == 1
    records_path = Path(manifest["path"]).parent / "oos_records.json"
    original = records_path.read_bytes()
    duplicate = append(manifest, config)
    assert duplicate["appended_periods"] == 0
    assert records_path.read_bytes() == original
    next_run = append(manifest, config, date(2025, 3, 31))
    assert next_run["appended_periods"] == 1
    assert next_run["experiment"]["records"][:1] == first["experiment"]["records"]
    assert next_run["experiment"]["records"][-1]["equity"] == pytest.approx(1.01 * 1.02)


def test_fetch_timestamps_and_cache_stats_do_not_create_false_conflict(frozen, clock):
    manifest, config = frozen
    advance(clock)
    append(manifest, config)
    changed = result_for(config, date(2025, 1, 31), date(2025, 3, 31))
    changed["periods"][0]["positions"][0]["q_fetched_at"] = "2025-05-01T00:00:00"
    changed["periods"][0]["cache_stats"]["hits"] = 100
    changed["periods"][0]["run_time"] = 12.3
    changed["periods"][0]["runtime_cache.terms_hits"] = 100
    assert append(manifest, config, result=changed)["appended_periods"] == 1


def test_economic_revision_conflicts_before_any_new_record_is_written(frozen, clock):
    manifest, config = frozen
    advance(clock)
    append(manifest, config)
    records_path = Path(manifest["path"]).parent / "oos_records.json"
    before = records_path.read_bytes()
    changed = result_for(config, date(2025, 1, 31), date(2025, 3, 31))
    changed["periods"][0]["period_return"] = 0.5
    with pytest.raises(ValueError, match="已记录历史发生冲突"):
        append(manifest, config, result=changed)
    assert records_path.read_bytes() == before
    assert not (records_path.parent / ".append.lock").exists()


def test_prior_daily_equity_revision_is_a_conflict_even_when_period_return_matches(frozen, clock):
    manifest, config = frozen
    advance(clock)
    initial = result_for(config, date(2025, 1, 31), date(2025, 2, 28))
    initial["equity_curve"].insert(1, {"date": date(2025, 2, 14), "equity": 0.8})
    append(manifest, config, result=initial)
    revised = result_for(config, date(2025, 1, 31), date(2025, 3, 31))
    revised["equity_curve"].insert(1, {"date": date(2025, 2, 14), "equity": 0.95})
    with pytest.raises(ValueError, match="已记录历史发生冲突"):
        append(manifest, config, result=revised)


def test_editing_frozen_date_or_recorded_payload_is_detected(frozen, clock):
    manifest, config = frozen
    advance(clock)
    append(manifest, config)
    manifest_path = Path(manifest["path"])
    original_manifest = manifest_path.read_text()
    data = json.loads(original_manifest)
    data["frozen_date"] = "2020-01-01"
    manifest_path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="不能改写冻结日期"):
        experiments.load_experiment(manifest_path)
    manifest_path.write_text(original_manifest)
    records_path = manifest_path.parent / "oos_records.json"
    payload = json.loads(records_path.read_text())
    payload["records"][0]["period"]["period_return"] = 0.9
    records_path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="历史内容已变化"):
        experiments.load_experiment(manifest_path)


@pytest.mark.parametrize("change", ["config", "pool", "params"])
def test_oos_rejects_changed_parameters_or_pool(frozen, clock, change):
    manifest, config = frozen
    advance(clock)
    params, pool, actual_config = deepcopy(PARAMS), list(POOL), config
    if change == "config":
        actual_config = replace(config, transaction_cost=0.01)
    elif change == "pool":
        pool.append("113003.SH")
    else:
        params["pricing"]["q"] = 0.01
    with pytest.raises(ValueError, match="必须冻结新版本"):
        experiments.append_oos_result(
            manifest["path"], result=result_for(actual_config, date(2025, 1, 31), date(2025, 2, 28)),
            config=actual_config, engine_params=params, bond_codes=pool,
        )


@pytest.mark.parametrize("start,end,fragment", [
    (date(2025, 1, 15), date(2025, 2, 28), "固定调仓起点"),
    (date(2025, 2, 28), date(2025, 3, 31), "固定调仓起点"),
    (date(2025, 1, 31), date(2025, 3, 15), "正常边界"),
    (date(2025, 1, 31), date(2025, 5, 30), "早于今天"),
])
def test_oos_rejects_non_future_restarts_partial_and_unrealized_windows(frozen, clock, start, end, fragment):
    manifest, config = frozen
    advance(clock)
    with pytest.raises(ValueError, match=fragment):
        append(manifest, config, result=result_for(config, start, end))


def test_oos_rejects_gaps_and_future_execution(frozen, clock):
    manifest, config = frozen
    advance(clock)
    result = result_for(config, date(2025, 1, 31), date(2025, 3, 31))
    result["periods"].pop(0)
    with pytest.raises(ValueError, match="不连续"):
        append(manifest, config, result=result)
    result = result_for(config, date(2025, 1, 31), date(2025, 2, 28))
    result["periods"][0]["positions"][0]["exit_date"] = date(2025, 5, 5)
    with pytest.raises(ValueError, match="尚未结束交易日"):
        append(manifest, config, result=result)


def test_run_oos_preflights_without_accessing_provider_then_reuses_frozen_anchor(frozen, clock, monkeypatch):
    manifest, config = frozen
    calls = []

    def run(provider, codes, **kwargs):
        calls.append((codes, kwargs))
        return result_for(kwargs["config"], kwargs["start_date"], kwargs["end_date"])

    monkeypatch.setattr(experiments, "backtest_pde_strategy", run)
    with pytest.raises(ValueError, match="尚无完整样本外区间"):
        experiments.run_experiment_oos(manifest["path"], object(), end_date=date(2025, 1, 31))
    assert not calls
    advance(clock)
    provenance = {"source": {"status": "unknown"}}
    first = experiments.run_experiment_oos(manifest["path"], object(), end_date=date(2025, 2, 28),
                                          provenance=provenance)
    second = experiments.run_experiment_oos(manifest["path"], object(), end_date=date(2025, 3, 31),
                                           provenance=provenance)
    assert first["appended_periods"] == second["appended_periods"] == 1
    assert all(call[1]["start_date"] == date(2025, 1, 31) for call in calls)
    assert all(call[1]["q"] == 0.0 and call[1]["M"] == 100 for call in calls)
    assert all(call[0] == POOL for call in calls)
    assert calls[0][1]["admission_config"].min_credit_rating == "AA-"


def test_public_oos_window_works_before_constructing_provider(frozen, clock):
    manifest, _ = frozen
    advance(clock)
    expected = [date(2025, 1, 31), date(2025, 2, 28), date(2025, 3, 31)]
    assert experiments.validate_oos_window(manifest, date(2025, 3, 31)) == expected
    assert experiments.validate_oos_window(manifest["path"], date(2025, 3, 31)) == expected


def test_oos_core_source_changes_require_new_version_but_gui_changes_do_not(tmp_path, clock, monkeypatch):
    config = PDEStrategyConfig()
    source = {"status": "known", "sha256": "full-old", "economic_sha256": "core-old"}
    manifest = experiments.freeze_experiment(
        tmp_path, name="固定代码", config=config, engine_params=PARAMS, bond_codes=POOL,
        result=result_for(config, date(2024, 1, 31), date(2024, 12, 31)),
        provenance={"source": source},
    )
    advance(clock)
    calls = []

    def run(provider, codes, **kwargs):
        calls.append(1)
        return result_for(kwargs["config"], kwargs["start_date"], kwargs["end_date"])

    monkeypatch.setattr(experiments, "backtest_pde_strategy", run)
    with pytest.raises(ValueError, match="经济计算源码版本已改变"):
        experiments.run_experiment_oos(
            manifest["path"], object(), end_date=date(2025, 2, 28),
            provenance={"source": {"sha256": "full-new", "economic_sha256": "core-new"}},
        )
    assert not calls
    result = experiments.run_experiment_oos(
        manifest["path"], object(), end_date=date(2025, 2, 28),
        provenance={"source": {"sha256": "gui-only-new", "economic_sha256": "core-old"}},
    )
    assert result["appended_periods"] == 1


@pytest.mark.parametrize("original,current,reason", [
    ({"sha256": "known"}, {"sha256": "unknown"}, "无法核实原已知源码版本"),
    ({"economic_sha256": "known"}, {"sha256": "known"}, "无法核实原已知源码版本"),
    ({"app_version": "2.0", "sha256": "unknown"},
     {"app_version": "2.1", "sha256": "unknown"}, "桌面应用版本已改变"),
    ({"app_version": "2.0"}, {}, "无法核实原已知应用版本"),
])
def test_unknown_source_does_not_silently_bypass_known_version_guards(
        tmp_path, clock, monkeypatch, original, current, reason):
    config = PDEStrategyConfig()
    manifest = experiments.freeze_experiment(
        tmp_path, name="固定原版本", config=config, engine_params=PARAMS, bond_codes=POOL,
        result=result_for(config, date(2024, 1, 31), date(2024, 12, 31)),
        provenance={"source": original},
    )
    advance(clock)

    def no_run(*args, **kwargs):
        pytest.fail("版本预检不通过时不应进入回测")

    monkeypatch.setattr(experiments, "backtest_pde_strategy", no_run)
    with pytest.raises(ValueError, match=reason):
        experiments.run_experiment_oos(manifest["path"], object(), end_date=date(2025, 2, 28),
                                       provenance={"source": current})


def test_concurrent_append_lock_prevents_lost_history(frozen, clock):
    manifest, config = frozen
    advance(clock)
    lock = Path(manifest["path"]).parent / ".append.lock"
    lock.touch()
    with pytest.raises(ValueError, match="实验正在更新"):
        append(manifest, config)
    assert lock.exists()
    assert experiments.load_experiment(manifest["path"])["record_count"] == 0


def automatic_q_experiment(tmp_path, config):
    params = deepcopy(PARAMS)
    params["pricing"]["q"] = None
    manifest = experiments.freeze_experiment(
        tmp_path, name="自动历史q", config=config, engine_params=params, bond_codes=POOL,
        result=result_for(config, date(2024, 1, 31), date(2024, 12, 31)),
    )
    return manifest, params


@pytest.mark.parametrize("source,as_of,reason", [
    ("realtime_snapshot", "2025-05-01", "实时快照"),
    ("realtime_snapshot", None, "实时快照"),
    ("fallback_zero", None, "缺失回退"),
    ("unknown", None, "未知"),
    ("historical", None, "缺少历史观察日期"),
    ("historical", "2025-02-01", "晚于信号日"),
])
def test_oos_rejects_non_historical_or_forward_dividends_without_writing(
        tmp_path, clock, source, as_of, reason):
    config = PDEStrategyConfig()
    manifest, params = automatic_q_experiment(tmp_path, config)
    advance(clock)
    result = result_for(config, date(2025, 1, 31), date(2025, 2, 28))
    period = result["periods"][0]
    period["data_quality"] = {"dividend_source_counts": {source: 1}, "current_fallback_ratio": 0}
    period["positions"][0].update(q_source=source, q_as_of=as_of)
    with pytest.raises(ValueError, match=reason):
        experiments.append_oos_result(manifest["path"], result=result, config=config,
                                      engine_params=params, bond_codes=POOL)
    assert experiments.load_experiment(manifest["path"])["record_count"] == 0
    assert not (Path(manifest["path"]).parent / "oos_records.json").exists()


def test_oos_accepts_dated_historical_dividends(tmp_path, clock):
    config = PDEStrategyConfig()
    manifest, params = automatic_q_experiment(tmp_path, config)
    advance(clock)
    result = result_for(config, date(2025, 1, 31), date(2025, 2, 28))
    period = result["periods"][0]
    period["data_quality"] = {"dividend_source_counts": {"historical": 1}, "current_fallback_ratio": 0}
    period["positions"][0].update(q_source="historical", q_as_of="2025-01-31")
    outcome = experiments.append_oos_result(manifest["path"], result=result, config=config,
                                           engine_params=params, bond_codes=POOL)
    assert outcome["appended_periods"] == 1
    assert outcome["experiment"]["engine_params"]["pricing"]["q"] is None


@pytest.mark.parametrize("quality", [
    {"current_fallback_ratio": 0.25},
    {"current_fallback_count": 1},
    {"source_counts": {"current_fallback": 1}},
    {"bond_sources": [{"bond_code": "113001.SH", "uses_current_fallback": True}]},
])
def test_oos_rejects_known_current_terms_fallback_even_with_fixed_zero_q(frozen, clock, quality):
    manifest, config = frozen
    advance(clock)
    result = result_for(config, date(2025, 1, 31), date(2025, 2, 28))
    result["periods"][0]["data_quality"] = quality
    with pytest.raises(ValueError, match="当前条款回退"):
        append(manifest, config, result=result)
    assert not (Path(manifest["path"]).parent / "oos_records.json").exists()


def test_atomic_write_failure_preserves_prior_records_and_releases_lock(frozen, clock, monkeypatch):
    manifest, config = frozen
    advance(clock)
    append(manifest, config)
    records_path = Path(manifest["path"]).parent / "oos_records.json"
    before = records_path.read_bytes()

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(experiments, "atomic_write_json", fail)
    with pytest.raises(OSError, match="disk full"):
        append(manifest, config, date(2025, 3, 31))
    assert records_path.read_bytes() == before
    assert not (records_path.parent / ".append.lock").exists()
