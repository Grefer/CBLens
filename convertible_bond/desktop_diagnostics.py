"""Small diagnostics entry point for packaged desktop builds."""
from __future__ import annotations

import importlib
import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path

from ._version import __version__
from .paths import (
    _SEEDED_DATA_FILES,
    app_data_dir,
    bundled_data_path,
    is_frozen_app,
    project_root,
    seed_data_files,
)
from .data_providers.wind import prepare_windpy_import_path


#: 诊断要报告的种子文件 —— **由 ``paths`` 那份算出来, 不再另抄一份清单**。
#:
#: 三处清单曾各写各的: ``paths._SEEDED_DATA_FILES`` (运行时会去 seed 的)、
#: ``scripts/build_desktop.STATIC_DATA_FILES`` (构建真正打进包的)、和这里 (诊断会
#: 报告的)。实测这一份漏了 ``cb_valuation_history.json`` —— 那正好是**唯一**一个
#: 进版本库、只追加、丢了就永久丢的数据文件, 而诊断页恰恰是用户唯一能看出"桌面包
#: 里到底有没有它"的地方。漏报的表现不是报错, 是那一行压根不出现。
#:
#: 这里锚 ``paths`` 那份而不是构建脚本那份: 决定"装好之后能不能用"的是运行时
#: 去 seed 哪些文件, 而构建脚本是另一侧 (它还带着 ``desktop_`` 前缀的别名源文件)。
_DATA_FILES = tuple(sorted(_SEEDED_DATA_FILES))


def _saved_at_note(payload: dict) -> str:
    """``, saved 2026-05-09 (117 days ago)`` —— 没有戳就返回空串。

    用本机挂钟 (``datetime.now()``) 而不是 ``market_today()``: 这里量的是"这份文件在
    盘上放了多久", 是运维问题不是市场口径问题 —— 与落盘 ``saved_at`` 同一类。
    ``test_package_has_no_bare_date_today`` 点名的正是这个出口。
    """
    saved_at = (payload.get("_meta") or {}).get("saved_at")
    if not isinstance(saved_at, str) or not saved_at:
        return ", saved ?"
    try:
        stamped = datetime.fromisoformat(saved_at).date()
    except ValueError:
        return f", saved {saved_at} (unparseable)"
    return f", saved {stamped.isoformat()} ({(datetime.now().date() - stamped).days} days ago)"


def _json_summary(path: Path) -> str:
    if not path.exists():
        return "missing"
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as exc:
        return f"{path.stat().st_size} bytes, invalid JSON ({type(exc).__name__}: {exc})"
    if path.name == "cb_data.json" and isinstance(payload, dict):
        n_bonds = sum(1 for key in payload if not str(key).startswith("_"))
        return f"{path.stat().st_size} bytes, {n_bonds} bonds"
    # 两个名字都要认: 种子文件叫 ``desktop_batch_pricing_cache.json``
    # (``paths._BUNDLED_DATA_ALIASES`` 里的别名源), 而这个分支此前只认运行态那个名字
    # —— 于是**恰恰是打进包里的那一份**掉进最后的通用分支, 只报个 "dict keys=3"。
    if path.name.endswith("batch_pricing_cache.json") and isinstance(payload, dict):
        results = payload.get("results")
        if isinstance(results, list):
            ok_count = sum(
                1 for row in results
                if isinstance(row, dict) and row.get("status") == "ok"
            )
            # **年龄要报出来**。种子缓存与运行态缓存都走这一行, 而两道闸
            # (`build_desktop._is_usable_batch_cache` / `paths._needs_seed`) 判的都只是
            # "有没有一行 status==ok" —— 一份 117 天前的截面照样满足。首启看到的是几个月
            # 前的理论价与偏差, 而唯一的线索是摘要条那个「估值日」。诊断页是用户能看出
            # "这批数到底多旧"的地方, 不报年龄就等于这里也帮着藏。
            return (f"{path.stat().st_size} bytes, {len(results)} rows, {ok_count} ok"
                    f"{_saved_at_note(payload)}")
    if isinstance(payload, dict):
        return f"{path.stat().st_size} bytes, dict keys={len(payload)}"
    if isinstance(payload, list):
        return f"{path.stat().st_size} bytes, list items={len(payload)}"
    return f"{path.stat().st_size} bytes, {type(payload).__name__}"


def _module_status(module_name: str) -> str:
    spec = importlib.util.find_spec(module_name)
    if spec is None:
        return "missing"
    location = spec.origin or ""
    return f"found ({location})" if location else "found"


def _import_status(module_name: str) -> str:
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:
        return f"import failed ({type(exc).__name__}: {exc})"
    location = getattr(module, "__file__", None) or ""
    return f"imported ({location})" if location else "imported"


def _data_error(path: Path, filename: str) -> str | None:
    """发布检查只验证真正能消费的种子，打印了 missing 不能仍然退出成功。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"{filename}: {exc}"
    if not isinstance(payload, dict):
        return f"{filename}: 应为 JSON object"
    if filename == "cb_data.json" and not any(not str(key).startswith("_") for key in payload):
        return f"{filename}: 没有条款"
    if filename == "cb_terms_patches.json":
        patches = payload.get("patches")
        if not isinstance(patches, list) or not patches:
            return f"{filename}: 没有历史 patch"
    if filename == "batch_pricing_cache.json":
        rows = payload.get("results")
        if not isinstance(rows, list) or not any(isinstance(row, dict) and row.get("status") == "ok" for row in rows):
            return f"{filename}: 没有成功定价行"
    return None


def main(argv: list[str] | None = None) -> int:
    """Print frozen app resource/data/import state for quick support checks."""
    args = argv if argv is not None else sys.argv[1:]
    strict = "--check" in args
    errors: list[str] = []
    windpy_paths = prepare_windpy_import_path()
    seeded = seed_data_files()

    print("CBLens desktop diagnostics")
    print(f"version: {__version__}")
    identity_path = project_root() / "desktop_build.json"
    try:
        identity = json.loads(identity_path.read_text(encoding="utf-8"))
        print(f"build: {json.dumps(identity, ensure_ascii=False, sort_keys=True)}")
        if strict and (identity.get("version") != __version__ or not identity.get("commit")):
            errors.append("构建身份缺少 commit 或版本不一致")
    except (OSError, ValueError, AttributeError) as exc:
        print(f"build: unavailable ({exc})")
        if strict:
            errors.append("构建身份清单不可读")
    print(f"frozen: {is_frozen_app()}")
    print(f"executable: {sys.executable}")
    print(f"_MEIPASS: {getattr(sys, '_MEIPASS', '')}")
    print(f"resource root: {project_root()}")
    print(f"data dir: {app_data_dir()}")
    if windpy_paths:
        print(f"WindPy path prepared: {', '.join(str(p) for p in windpy_paths)}")
    print()
    print("seeded targets:")
    for target in seeded:
        print(f"  {target}: {_json_summary(target)}")
        if strict and (error := _data_error(target, target.name)):
            errors.append(error)
    print()
    print("bundled seeds:")
    for filename in _DATA_FILES:
        source = bundled_data_path(filename)
        if source is None:
            print(f"  {filename}: missing")
            if strict:
                errors.append(f"包内种子缺失: {filename}")
        else:
            print(f"  {source}: {_json_summary(source)}")
            if strict and (error := _data_error(source, filename)):
                errors.append(error)
    print()
    print("modules:")
    wind_status = _import_status("WindPy")
    print(f"  WindPy: {wind_status}")
    if "--require-windpy" in args and not wind_status.startswith("imported"):
        errors.append("发布包 WindPy 无法导入")
    modules = ("akshare", "certifi", "requests")
    if strict:
        modules += ("numpy", "scipy", "customtkinter", "matplotlib.backends.backend_tkagg")
    for module_name in modules:
        status = _import_status(module_name) if strict else _module_status(module_name)
        print(f"  {module_name}: {status}")
        if strict and not status.startswith("imported"):
            errors.append(f"{module_name}: {status}")
    for error in errors:
        print(f"ERROR: {error}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
