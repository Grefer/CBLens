"""Small diagnostics entry point for packaged desktop builds."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from .paths import (
    app_data_dir,
    bundled_data_path,
    is_frozen_app,
    project_root,
    seed_data_files,
)
from .data_providers.wind import prepare_windpy_import_path


_DATA_FILES = (
    "cb_data.json",
    "cb_events.json",
    "down_reset_overrides.json",
    "batch_pricing_cache.json",
)


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


def main(argv: list[str] | None = None) -> int:
    """Print frozen app resource/data/import state for quick support checks."""
    _ = argv or sys.argv[1:]
    windpy_paths = prepare_windpy_import_path()
    seeded = seed_data_files()

    print("CBLens desktop diagnostics")
    print(f"frozen: {is_frozen_app()}")
    print(f"executable: {sys.executable}")
    print(f"_MEIPASS: {getattr(sys, '_MEIPASS', '')}")
    print(f"resource root: {project_root()}")
    print(f"data dir: {app_data_dir()}")
    if windpy_paths:
        print(f"WindPy path added: {', '.join(str(p) for p in windpy_paths)}")
    print()
    print("seeded targets:")
    for target in seeded:
        print(f"  {target}: {_json_summary(target)}")
    print()
    print("bundled seeds:")
    for filename in _DATA_FILES:
        source = bundled_data_path(filename)
        if source is None:
            print(f"  {filename}: missing")
        else:
            print(f"  {source}: {_json_summary(source)}")
    print()
    print("modules:")
    for module_name in ("WindPy", "akshare", "certifi", "requests"):
        print(f"  {module_name}: {_module_status(module_name)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
