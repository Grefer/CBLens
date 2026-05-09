#!/usr/bin/env python3
"""Build CBLens desktop app with PyInstaller.

Run from the repository root:

    python scripts/build_desktop.py

Outputs:
  - macOS:   dist/CBLens.app
  - Windows: dist/CBLens.exe
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


APP_NAME = "CBLens"
STATIC_DATA_FILES = ("cb_data.json", "cb_events.json", "down_reset_overrides.json")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _add_data_arg(src: Path, dest: str) -> str:
    separator = ";" if os.name == "nt" else ":"
    return f"{src}{separator}{dest}"


def _ensure_pyinstaller() -> None:
    try:
        import PyInstaller  # noqa: F401
    except ImportError as exc:
        raise SystemExit(
            "PyInstaller is not installed. Run: python -m pip install -e '.[desktop]'"
        ) from exc


def _mac_icon(root: Path) -> Path | None:
    if sys.platform != "darwin":
        return None
    png = root / "assets" / "cblens-icon.png"
    if not png.exists():
        return None
    out = root / "build" / "cblens-icon.icns"
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        from PIL import Image

        image = Image.open(png).convert("RGBA")
        image.save(out, format="ICNS")
        return out
    except Exception:
        return None


def _icon_arg(root: Path) -> Path | None:
    if sys.platform == "win32":
        icon = root / "assets" / "cblens-icon.ico"
    elif sys.platform == "darwin":
        icon = _mac_icon(root)
    else:
        icon = root / "assets" / "cblens-icon.png"
    return icon if icon and icon.exists() else None


def _postprocess_macos_app(dist: Path) -> None:
    if sys.platform != "darwin":
        return
    app = dist / f"{APP_NAME}.app"
    if not app.exists():
        return
    subprocess.run(["xattr", "-cr", str(app)], check=False)
    subprocess.run(
        ["codesign", "--force", "--deep", "--sign", "-", str(app)],
        check=False,
    )


def build() -> None:
    _ensure_pyinstaller()
    root = _repo_root()
    dist = root / "dist"
    build_dir = root / "build"
    shutil.rmtree(dist, ignore_errors=True)
    shutil.rmtree(build_dir, ignore_errors=True)
    (build_dir / "pyinstaller-config").mkdir(parents=True, exist_ok=True)
    (build_dir / "mplconfig").mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--windowed",
        "--name",
        APP_NAME,
        "--collect-data",
        "customtkinter",
        "--collect-data",
        "matplotlib",
        "--hidden-import",
        "akshare",
        "--hidden-import",
        "matplotlib.backends.backend_tkagg",
        "--add-data",
        _add_data_arg(root / "assets", "assets"),
    ]
    if sys.platform == "win32":
        cmd.append("--onefile")
    for filename in STATIC_DATA_FILES:
        src = root / "data" / filename
        if src.exists():
            cmd.extend(["--add-data", _add_data_arg(src, "data")])

    icon = _icon_arg(root)
    if icon is not None:
        cmd.extend(["--icon", str(icon)])
    if sys.platform == "darwin":
        cmd.extend(["--osx-bundle-identifier", "com.grefer.cblens"])

    cmd.append(str(root / "gui.py"))
    print("Building", APP_NAME, "for", platform.platform())
    env = os.environ.copy()
    env["PYINSTALLER_CONFIG_DIR"] = str(build_dir / "pyinstaller-config")
    env["MPLCONFIGDIR"] = str(build_dir / "mplconfig")
    subprocess.run(cmd, cwd=root, check=True, env=env)
    _postprocess_macos_app(dist)
    print("Build complete:", dist)


if __name__ == "__main__":
    build()
