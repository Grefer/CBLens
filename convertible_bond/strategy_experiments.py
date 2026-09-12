"""冻结策略实验与不可覆盖的样本外记录，独立于 GUI 最近快照清理。"""
from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import uuid
from contextlib import contextmanager
from dataclasses import asdict, fields, is_dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ._version import __version__
from .atomic_io import atomic_write_json
from .batch_pricing import AdmissionFilterConfig
from .data_providers.base import finite_float
from .market_time import EXCHANGE_TZ
from .paths import project_root
from .strategy_backtest import (
    PDEStrategyConfig,
    backtest_pde_strategy,
    build_rebalance_schedule,
    validate_strategy_config,
)


_NON_ECONOMIC_KEYS = frozenset({
    "fetched_at", "run_time", "elapsed", "elapsed_seconds", "cache_stats",
    "provenance", "performance", "saved_at", "updated_at",
})


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _market_today() -> date:
    return _utc_now().astimezone(EXCHANGE_TZ).date()


def _jsonable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"研究记录不能序列化 {type(value).__name__}")


def _digest(value: Any) -> str:
    raw = json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _day(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"无效研究日期: {value}") from exc


def _fingerprint(path: Path) -> dict[str, Any]:
    try:
        stat_before = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        stat_after = path.stat()
        if (stat_before.st_mtime_ns, stat_before.st_size) != (
                stat_after.st_mtime_ns, stat_after.st_size):
            return {"path": str(path), "status": "unknown", "reason": "读取期间文件变化"}
        return {"path": str(path), "status": "known", "sha256": digest.hexdigest(),
                "size": stat_after.st_size, "mtime_ns": stat_after.st_mtime_ns}
    except OSError as exc:
        return {"path": str(path), "status": "unknown", "reason": type(exc).__name__}


def capture_provenance(
    data_paths: dict[str, Path | str] | None = None,
    source_root: Path | str | None = None,
) -> dict[str, Any]:
    """在运行开始捕获源码版本和数据指纹；不可得项明确 unknown，不伪造版本。"""
    root = Path(source_root) if source_root is not None else project_root()
    source: dict[str, Any] = {
        "path": str(root), "app_version": __version__, "status": "unknown",
        "git_commit": "unknown", "sha256": "unknown",
    }
    try:
        paths = sorted((root / "convertible_bond").rglob("*.py"))
        if paths:
            fingerprints = [_fingerprint(path) for path in paths]
            if all(item["status"] == "known" for item in fingerprints):
                source["sha256"] = _digest([
                    (str(path.relative_to(root)), item["sha256"])
                    for path, item in zip(paths, fingerprints)
                ])
                # GUI/CLI 文案与布局不改变经济计算，单独记录核心指纹供续写核对。
                source["economic_sha256"] = _digest([
                    (str(path.relative_to(root)), item["sha256"])
                    for path, item in zip(paths, fingerprints)
                    if not {"gui", "cli"} & set(path.relative_to(root).parts[:-1])
                    and path.name != "_version.py"
                ])
                source["status"] = "known"
        if (root / ".git").exists():
            completed = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=5, check=False,
            )
            if completed.returncode == 0:
                source["git_commit"] = completed.stdout.strip() or "unknown"
    except (OSError, subprocess.TimeoutExpired):
        pass
    return {
        "captured_at": _utc_now().isoformat(),
        "source": source,
        "data": {name: _fingerprint(Path(path)) for name, path in (data_paths or {}).items()},
        "data_status": "captured" if data_paths else "unknown",
    }


def _config_dict(config: PDEStrategyConfig | dict[str, Any]) -> dict[str, Any]:
    raw = asdict(config) if is_dataclass(config) else dict(config)
    required = {field.name for field in fields(PDEStrategyConfig)}
    if set(raw) != required:
        raise ValueError("冻结需要实际运行的完整 strategy_config，不能用缺字段的旧快照补猜")
    cfg = PDEStrategyConfig(**raw)
    validate_strategy_config(cfg)
    if (cfg.rank_signal != "deviation" or cfg.holding_mode != "top_score"
            or cfg.funding_mode != "reserve_cash"):
        raise ValueError("只能冻结 PDE Top N、缺口留现金的正式策略配置")
    return _jsonable(raw)


def _check_result_config(result: dict[str, Any], config: dict[str, Any]) -> None:
    actual = result.get("config") or {}
    for key, value in config.items():
        if key not in actual or _jsonable(actual[key]) != value:
            raise ValueError(f"结果与冻结配置不一致: {key}")


def _next_boundary(day: date, freq: str) -> date:
    # 含首尾的公共日程生成器是唯一频率规则；中间第一个点就是未来正常调仓锚。
    return build_rebalance_schedule(day, day + timedelta(days=400), freq)[1]


def _manifest_path(path: Path | str) -> Path:
    path = Path(path)
    return path / "manifest.json" if path.is_dir() else path


def freeze_experiment(
    root: Path | str,
    *,
    name: str,
    config: PDEStrategyConfig | dict[str, Any],
    engine_params: dict[str, Any],
    bond_codes: list[str],
    result: dict[str, Any],
    provenance: dict[str, Any] | None = None,
    data_paths: dict[str, Path | str] | None = None,
    source_root: Path | str | None = None,
    parent_id: str | None = None,
) -> dict[str, Any]:
    """新建不可变方案版本；依据已完成结果冻结，旧运行缺来源时保留 unknown。"""
    cfg = _config_dict(config)
    _check_result_config(result, cfg)
    start, end = _day(result.get("start_date")), _day(result.get("end_date"))
    now = _utc_now()
    frozen_date = now.astimezone(EXCHANGE_TZ).date()
    if not result.get("periods") or end <= start or end > frozen_date:
        raise ValueError("只能冻结已经完成计算、结束日不晚于今天的实际回测结果")
    for period in result["periods"]:
        for position in period.get("positions") or []:
            for key in ("entry_date", "exit_date"):
                if position.get(key) and _day(position[key]) > frozen_date:
                    raise ValueError("训练结果含未来成交，不能据此冻结方案")
    pool = list(dict.fromkeys(str(code) for code in bond_codes))
    if not pool:
        raise ValueError("冻结代码池不能为空")
    params = _jsonable(engine_params)
    settings = result.get("run_settings") or {}
    for key in ("pricing", "admission_filter", "history_mode", "pool"):
        if key in settings and key in params and _jsonable(settings[key]) != params[key]:
            raise ValueError(f"结果与冻结运行条件不一致: {key}")
    if params.get("source") and settings.get("data_source") not in (None, params["source"]):
        raise ValueError("结果与冻结行情源不一致")
    parameter_pool = (params.get("pool") or {}).get("bond_codes")
    if parameter_pool is not None and list(parameter_pool) != pool:
        raise ValueError("运行条件代码池与冻结代码池不一致")
    if provenance is None:
        provenance = settings.get("provenance")
    if provenance is None:
        provenance = {"captured_at": "unknown", "source": {"status": "unknown"},
                      "data": {}, "data_status": "unknown"}
    elif provenance.get("captured_at") not in (None, "unknown"):
        captured = datetime.fromisoformat(str(provenance["captured_at"]))
        if captured.tzinfo is None or captured > now:
            raise ValueError("运行来源的捕获时间无效")
    root = Path(root)
    version = 1
    if parent_id is not None:
        if Path(parent_id).name != parent_id:
            raise ValueError("无效父实验编号")
        parent = load_experiment(root / parent_id)
        version = int(parent["version"]) + 1
    experiment_id = f"experiment_{now:%Y%m%dT%H%M%S}_{uuid.uuid4().hex[:12]}"
    spec = {"config": cfg, "engine_params": params, "bond_codes": pool}
    manifest = {
        "schema_version": 1, "experiment_id": experiment_id,
        "name": str(name).strip() or experiment_id,
        "version": version, "parent_id": parent_id,
        "frozen_at": now.isoformat(), "frozen_date": frozen_date.isoformat(),
        "oos_start_date": _next_boundary(frozen_date, cfg["rebalance_freq"]).isoformat(),
        **spec, "spec_hash": _digest(spec), "provenance": _jsonable(provenance),
        "training": {"start_date": start.isoformat(), "end_date": end.isoformat(),
                     "observed_through": min(end, frozen_date).isoformat(),
                     "includes_freeze_day": end == frozen_date,
                     "summary": _jsonable(result.get("summary") or {}),
                     "result_hash": _digest(result)},
        # 这里只是未来运行再取指纹的路径，不冒充历史运行时的数据内容。
        "provenance_paths": {"data": _jsonable(data_paths or {}),
                             "source_root": str(source_root) if source_root else None},
    }
    manifest["manifest_hash"] = _digest(manifest)
    directory = root / experiment_id
    directory.mkdir(parents=True, exist_ok=False)
    atomic_write_json(directory / "manifest.json", manifest, allow_nan=False)
    atomic_write_json(directory / "training_result.json", _jsonable(result), allow_nan=False)
    return load_experiment(directory)


def load_experiment(path: Path | str) -> dict[str, Any]:
    """读取冻结方案与只增记录；检验方案哈希，避免手改参数后沿用旧实验。"""
    manifest_path = _manifest_path(path)
    with manifest_path.open(encoding="utf-8") as stream:
        manifest = json.load(stream)
    original = {key: value for key, value in manifest.items() if key != "manifest_hash"}
    if _digest(original) != manifest.get("manifest_hash"):
        raise ValueError("冻结档案内容已变化；不能改写冻结日期、来源或原方案")
    spec = {key: manifest[key] for key in ("config", "engine_params", "bond_codes")}
    if manifest.get("schema_version") != 1 or _digest(spec) != manifest.get("spec_hash"):
        raise ValueError("冻结方案已变化或格式不支持，请创建新版本")
    records_path = manifest_path.parent / "oos_records.json"
    records = []
    if records_path.exists():
        with records_path.open(encoding="utf-8") as stream:
            records = json.load(stream)["records"]
        for record in records:
            payload = _record_economics(record["period"], record.get("curves", {}),
                                        record.get("accounting_basis"))
            if _digest(payload) != record.get("economic_hash"):
                raise ValueError("已记录样本外历史内容已变化，不能继续追加")
    manifest["path"] = str(manifest_path)
    manifest["records"] = records
    manifest["record_count"] = len(records)
    return manifest


def list_experiments(root: Path | str) -> list[dict[str, Any]]:
    """列出独立实验档案，按冻结时间倒序；损坏档案报错而非静默消失。"""
    return sorted(
        (load_experiment(path) for path in Path(root).glob("experiment_*/manifest.json")),
        key=lambda item: item["frozen_at"], reverse=True,
    )


def _economic_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _economic_payload(item) for key, item in value.items()
                if key not in _NON_ECONOMIC_KEYS and not str(key).startswith("_")
                and not str(key).endswith("_fetched_at")
                and not str(key).startswith("runtime_cache.")}
    if isinstance(value, list):
        return [_economic_payload(item) for item in value]
    return value


def _record_economics(period: dict, curves: dict, accounting_basis: Any) -> dict:
    """一份持仓期的完整经济记录；日内/逐日回撤路径同样不得事后替换。"""
    return _economic_payload({"period": period, "curves": curves,
                              "accounting_basis": accounting_basis})


def _period_curves(result: dict[str, Any], period: dict[str, Any]) -> dict:
    first, last = _day(period["start_date"]), _day(period["end_date"])
    return {
        key: _jsonable([point for point in result.get(key) or []
                       if first <= _day(point.get("date")) <= last])
        for key in ("equity_curve", "benchmark_curve", "index_benchmark_curve")
    }


@contextmanager
def _append_lock(directory: Path):
    lock = directory / ".append.lock"
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ValueError("实验正在更新；如上次进程异常退出，请先检查遗留锁文件") from exc
    try:
        os.close(descriptor)
        yield
    finally:
        lock.unlink(missing_ok=True)


def _validate_oos_window(manifest: dict[str, Any], end_date: date) -> list[date]:
    start = _day(manifest["oos_start_date"])
    if end_date <= start:
        raise ValueError(f"尚无完整样本外区间；固定调仓起点为 {start}")
    if end_date >= _market_today():
        raise ValueError("样本外结束日必须早于今天，不能记录尚未实现的收益")
    freq = manifest["config"]["rebalance_freq"]
    schedule = build_rebalance_schedule(start, end_date + timedelta(days=1), freq)
    if end_date not in schedule[1:-1]:
        raise ValueError("样本外结束日必须为原调仓频率的正常边界，不能追加临时残期")
    return schedule[:-1]


def validate_oos_window(
    path_or_manifest: Path | str | dict[str, Any], end_date: date,
) -> list[date]:
    """GUI 取数前检查未来完整区间；返回固定锚点起的完整调仓日程。"""
    manifest = (path_or_manifest if isinstance(path_or_manifest, dict)
                else load_experiment(path_or_manifest))
    return _validate_oos_window(manifest, _day(end_date))


def _check_source_compatible(original: dict[str, Any], current: dict[str, Any]) -> None:
    """源码包优先核对经济核心；PYZ 无源码时至少锁住已知应用版本。"""
    for key in ("economic_sha256", "sha256"):
        previous = original.get(key)
        if previous in (None, "unknown"):
            continue
        actual = current.get(key)
        if actual in (None, "unknown"):
            raise ValueError("无法核实原已知源码版本；请创建新实验版本，不能续写原口径")
        if actual != previous:
            raise ValueError("经济计算源码版本已改变；请创建新实验版本，不能续写原口径")
        return
    previous_version = original.get("app_version")
    if previous_version not in (None, "unknown"):
        actual_version = current.get("app_version")
        if actual_version in (None, "unknown"):
            raise ValueError("无法核实原已知应用版本；请创建新实验版本")
        if actual_version != previous_version:
            raise ValueError("桌面应用版本已改变且源码不可核实；请创建新实验版本")


def _validate_oos_inputs(result: dict[str, Any], params: dict[str, Any]) -> None:
    """固定参数仍需真实历史输入；当前条款回退/实时股息率不能冒充样本外证据。"""
    fixed_q = finite_float((params.get("pricing") or {}).get("q"))
    periods = result.get("periods") or []
    quality = [(result.get("diagnostics") or {}).get("data_quality") or {}]
    quality.extend(period.get("data_quality") or {} for period in periods)
    for item in quality:
        sources = item.get("source_counts") or {}
        if ((finite_float(item.get("current_fallback_ratio")) or 0) > 0
                or (finite_float(item.get("current_fallback_count")) or 0) > 0
                or (finite_float(sources.get("current_fallback")) or 0) > 0
                or any(row.get("uses_current_fallback") for row in item.get("bond_sources") or [])):
            raise ValueError("样本外含当前条款回退；请补齐历史条款后重跑，不会写入正式记录")
    if fixed_q is not None:
        return  # 包括显式 q=0：这是冻结的情景假设，不声称真实历史股息率为零。
    for period in periods:
        signal_day = _day(period.get("start_date"))
        counts = (period.get("data_quality") or {}).get("dividend_source_counts") or {}
        observed_counts = {source: count for source, count in counts.items()
                           if (finite_float(count) or 0) > 0}
        if not observed_counts and period.get("priced_count") == 0:
            continue  # 当期没有定价信号，未消费股息率。
        if not observed_counts or set(observed_counts) != {"historical"}:
            raise ValueError("自动股息率含实时快照、未知或缺失回退，不能记录为有效样本外；"
                             "请使用历史股息率，或另建固定 q 的新方案")
        rows = [row for key in ("candidate_rows", "positions")
                for row in period.get(key) or []]
        for row in rows:
            if row.get("q_source") != "historical" or not row.get("q_as_of"):
                raise ValueError("自动股息率缺少历史观察日期；请使用历史股息率或冻结固定 q 新方案")
            if _day(row["q_as_of"]) > signal_day:
                raise ValueError("股息率观察日期晚于信号日，存在未来信息；不会写入样本外记录")


def append_oos_result(
    path: Path | str,
    *,
    result: dict[str, Any],
    config: PDEStrategyConfig | dict[str, Any],
    engine_params: dict[str, Any],
    bond_codes: list[str],
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """只接受从固定未来锚点重跑的完整前缀；旧期对账通过后原子追加，绝不覆盖。"""
    manifest = load_experiment(path)
    _check_source_compatible(
        (manifest.get("provenance") or {}).get("source") or {},
        (provenance or {}).get("source") or {},
    )
    spec = {"config": _config_dict(config), "engine_params": _jsonable(engine_params),
            "bond_codes": list(bond_codes)}
    if _digest(spec) != manifest["spec_hash"]:
        raise ValueError("策略参数或代码池已变更，必须冻结新版本")
    _check_result_config(result, spec["config"])
    _validate_oos_inputs(result, spec["engine_params"])
    if _day(result.get("start_date")) != _day(manifest["oos_start_date"]):
        raise ValueError("样本外必须从固定调仓起点完整重跑，不能拼接重新建仓的区间")
    expected = _validate_oos_window(manifest, _day(result.get("end_date")))
    periods = _jsonable(result.get("periods") or [])
    actual = [(_day(item.get("start_date")), _day(item.get("end_date"))) for item in periods]
    if actual != list(zip(expected, expected[1:])):
        raise ValueError("样本外持有区间不连续或与冻结调仓日程不一致")
    today = _market_today()
    for key in ("equity_curve", "benchmark_curve", "index_benchmark_curve"):
        if any(_day(point.get("date")) >= today for point in result.get(key) or []):
            raise ValueError("样本外净值曲线含尚未结束交易日")
    for period in periods:
        for position in period.get("positions") or []:
            for key in ("entry_date", "exit_date"):
                if position.get(key) and _day(position[key]) >= today:
                    raise ValueError("样本外含尚未结束交易日的成交记录")
    directory = Path(manifest["path"]).parent
    curves = [_period_curves(result, period) for period in periods]
    basis = _jsonable(result.get("accounting_basis"))
    payloads = [_record_economics(period, curve, basis) for period, curve in zip(periods, curves)]
    with _append_lock(directory):
        manifest = load_experiment(path)
        records = manifest["records"]
        if len(periods) < len(records):
            raise ValueError("结果未覆盖已经记录的完整样本外历史")
        for stored, payload in zip(records, payloads):
            if stored["economic_hash"] != _digest(payload):
                raise ValueError(f"已记录历史发生冲突: {stored['start_date']} → {stored['end_date']}；不会覆盖")
        added = len(periods) - len(records)
        if added:
            for period, curve, payload in zip(
                    periods[len(records):], curves[len(records):], payloads[len(records):]):
                records.append({
                    "start_date": period["start_date"], "end_date": period["end_date"],
                    "period_return": period.get("period_return"),
                    "equity": period.get("equity"),
                    "economic_hash": _digest(payload), "curves": curve,
                    "accounting_basis": basis,
                    "period": period, "recorded_at": _utc_now().isoformat(),
                    "provenance": _jsonable(provenance or {"status": "unknown"}),
                })
            atomic_write_json(directory / "oos_records.json", {
                "schema_version": 1, "experiment_id": manifest["experiment_id"],
                "records": records,
            }, allow_nan=False)
    return {"experiment": load_experiment(path), "appended_periods": added, "result": result}


def run_experiment_oos(
    path: Path | str,
    provider,
    *,
    end_date: date,
    terms_cache=None,
    progress_cb=None,
    stage_cb=None,
    cancel_cb=None,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """从冻结方案重建原配置并完整重跑未来窗，验证旧期后仅追加新期。"""
    manifest = load_experiment(path)
    _validate_oos_window(manifest, _day(end_date))  # 在任何联网前拒绝无新完整区间。
    cfg = PDEStrategyConfig(**manifest["config"])
    params = manifest["engine_params"]
    if "pricing" not in params:
        raise ValueError("冻结方案缺少 pricing 运行参数，不能自动重建样本外运行")
    if provenance is None:
        paths = manifest.get("provenance_paths") or {}
        provenance = capture_provenance(paths.get("data"), paths.get("source_root"))
    original_source = manifest.get("provenance", {}).get("source") or {}
    current_source = provenance.get("source") or {}
    _check_source_compatible(original_source, current_source)
    result = backtest_pde_strategy(
        provider, manifest["bond_codes"],
        start_date=_day(manifest["oos_start_date"]), end_date=_day(end_date),
        config=cfg, terms_cache=terms_cache,
        admission_config=AdmissionFilterConfig(**params.get("admission_filter", {})),
        progress_cb=progress_cb, stage_cb=stage_cb, cancel_cb=cancel_cb,
        **params["pricing"],
    )
    return append_oos_result(
        path, result=result, config=cfg, engine_params=params,
        bond_codes=manifest["bond_codes"], provenance=provenance,
    )
