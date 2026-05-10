"""Runtime paths for source checkouts and frozen desktop apps."""
from __future__ import annotations

import json
import logging
import os
import shutil
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


APP_NAME = "CBLens"
_SEEDED_DATA_FILES = {"cb_data.json", "cb_events.json", "down_reset_overrides.json", "batch_pricing_cache.json"}
_BUNDLED_DATA_ALIASES = {
    # 运行态批量缓存仍写入/读取 batch_pricing_cache.json；Release 构建则可携带
    # 一个只读种子文件，避免 CI 没有本机运行态缓存时桌面包首启空表。
    "batch_pricing_cache.json": ("batch_pricing_cache.json", "desktop_batch_pricing_cache.json"),
}


def is_frozen_app() -> bool:
    """True when running from a PyInstaller bundle."""
    return bool(getattr(sys, "frozen", False)) and hasattr(sys, "_MEIPASS")


def project_root() -> Path:
    """Repository root when running from source; PyInstaller temp root when frozen."""
    if is_frozen_app():
        return Path(getattr(sys, "_MEIPASS"))
    return Path(__file__).resolve().parent.parent


def _frozen_resource_roots() -> list[Path]:
    """Candidate roots that may contain bundled resources in PyInstaller builds."""
    roots: list[Path] = []
    if is_frozen_app():
        mei = Path(getattr(sys, "_MEIPASS"))
        roots.append(mei)
        roots.append(mei.parent / "Resources")
        roots.append(mei.parent / "_internal")

        exe_parent = Path(sys.executable).resolve().parent
        roots.append(exe_parent / "_internal")
        for parent in exe_parent.parents:
            if parent.name == "Contents":
                roots.extend([
                    parent / "Resources",
                    parent / "Frameworks",
                    parent / "MacOS" / "_internal",
                ])
                break
    else:
        roots.append(project_root())

    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root)
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return unique


def bundled_data_path(filename: str) -> Path | None:
    """Return the bundled seed data path when present."""
    candidate_names = _BUNDLED_DATA_ALIASES.get(filename, (filename,))
    for root in _frozen_resource_roots():
        for candidate_name in candidate_names:
            candidate = root / "data" / candidate_name
            if candidate.exists():
                return candidate
    return None


def app_data_dir() -> Path:
    """Writable data directory used by packaged desktop apps.

    Source checkouts keep the historical ``<repo>/data`` behavior unless
    ``CBLENS_DATA_DIR`` is set. Frozen apps use a per-user writable location.
    """
    override = os.environ.get("CBLENS_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if not is_frozen_app():
        return project_root() / "data"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME / "data"
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Roaming"
        return root / APP_NAME / "data"
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / APP_NAME / "data"


def _needs_seed(target: Path, filename: str | None = None) -> bool:
    """True when the target file is missing or looks corrupt/empty."""
    if not target.exists():
        return True
    try:
        if target.stat().st_size < 10:
            return True
    except OSError:
        return True
    if filename and filename.endswith(".json"):
        try:
            with open(target, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except (json.JSONDecodeError, OSError):
            return True
        if filename == "cb_data.json":
            return not (
                isinstance(payload, dict)
                and any(not str(k).startswith("_") for k in payload)
            )
        if filename == "batch_pricing_cache.json":
            results = payload.get("results")
            return not (
                isinstance(payload, dict)
                and isinstance(results, list)
                and any(isinstance(row, dict) and row.get("status") == "ok" for row in results)
            )
    return False


def data_path(filename: str, *, seed: bool = False) -> Path:
    """Return a writable data file path, optionally seeding it from bundled data."""
    root = app_data_dir()
    root.mkdir(parents=True, exist_ok=True)
    target = root / filename
    if seed and filename in _SEEDED_DATA_FILES and _needs_seed(target, filename):
        bundled = bundled_data_path(filename)
        if bundled is not None and bundled.resolve() != target.resolve():
            try:
                shutil.copy2(bundled, target)
                logger.info("seeded %s from bundle → %s", filename, target)
            except OSError as exc:
                logger.warning("seed %s 失败: %s", filename, exc)
        elif bundled is None:
            logger.warning(
                "seed %s 跳过: bundled 源文件不存在, 请确认构建时 data/ 已包含此文件; candidates=%s",
                filename, [str(p / "data" / filename) for p in _frozen_resource_roots()],
            )
    return target


def seed_data_files() -> list[Path]:
    """Ensure all bundled seed data files are copied to the writable data dir.

    Safe to call multiple times; only missing/corrupt files are re-seeded.
    Returns the list of target paths.
    """
    targets: list[Path] = []
    for filename in sorted(_SEEDED_DATA_FILES):
        targets.append(data_path(filename, seed=True))
    return targets


def data_dir(*parts: str) -> Path:
    """Return a writable data directory path."""
    path = app_data_dir().joinpath(*parts)
    path.mkdir(parents=True, exist_ok=True)
    return path


def asset_path(filename: str) -> Path:
    """Return an asset path from source or a PyInstaller bundle."""
    return project_root() / "assets" / filename
