"""Runtime paths for source checkouts and frozen desktop apps."""
from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


APP_NAME = "CBLens"
_SEEDED_DATA_FILES = {"cb_data.json", "cb_events.json", "down_reset_overrides.json"}


def is_frozen_app() -> bool:
    """True when running from a PyInstaller bundle."""
    return bool(getattr(sys, "frozen", False)) and hasattr(sys, "_MEIPASS")


def project_root() -> Path:
    """Repository root when running from source; PyInstaller temp root when frozen."""
    if is_frozen_app():
        return Path(getattr(sys, "_MEIPASS"))
    return Path(__file__).resolve().parent.parent


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


def _needs_seed(target: Path) -> bool:
    """True when the target file is missing or looks corrupt/empty."""
    if not target.exists():
        return True
    try:
        return target.stat().st_size < 10
    except OSError:
        return True


def data_path(filename: str, *, seed: bool = False) -> Path:
    """Return a writable data file path, optionally seeding it from bundled data."""
    root = app_data_dir()
    root.mkdir(parents=True, exist_ok=True)
    target = root / filename
    if seed and filename in _SEEDED_DATA_FILES and _needs_seed(target):
        bundled = project_root() / "data" / filename
        if bundled.exists() and bundled.resolve() != target.resolve():
            try:
                shutil.copy2(bundled, target)
                logger.info("seeded %s from bundle → %s", filename, target)
            except OSError as exc:
                logger.warning("seed %s 失败: %s", filename, exc)
        elif not bundled.exists():
            logger.warning(
                "seed %s 跳过: bundled 源文件不存在 (%s), 请确认构建时 data/ 已包含此文件",
                filename, bundled,
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
