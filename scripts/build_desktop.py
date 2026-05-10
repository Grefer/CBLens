#!/usr/bin/env python3
"""Build CBLens desktop app with PyInstaller.

Run from the repository root:

    python scripts/build_desktop.py

Outputs:
  - macOS:   dist/CBLens.app
  - Windows: dist/CBLens.exe

WindPy 处理策略 (借鉴 DeltaLab):
  - 构建机上能 import WindPy 时自动打入发布包
  - 两种形态兼容: (a) Wind 终端单文件 WindPy.py  (b) pip 安装的 WindPy 包
  - CI 无 Wind 终端时自动跳过; 下载版运行时会再探测用户本机 Wind API 路径
"""
from __future__ import annotations

import os
import platform
import json
import shutil
import subprocess
import sys
from pathlib import Path


APP_NAME = "CBLens"


def _rp(path: Path) -> str:
    """Return a string literal for a path, safe for generated spec files.

    Escapes backslashes and single quotes so the generated spec never
    produces invalid Python syntax regardless of what characters appear
    in the path (Windows paths with ``\\a`` bell escapes, paths containing
    single quotes, etc.).
    """
    s = str(path).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{s}'"


STATIC_DATA_FILES = (
    "cb_data.json",
    "cb_events.json",
    "down_reset_overrides.json",
    # Tracked release seed. Runtime cache data/batch_pricing_cache.json is
    # intentionally ignored, but local builds may still bundle it when usable.
    "desktop_batch_pricing_cache.json",
)
BATCH_PRICING_CACHE_FILE = "batch_pricing_cache.json"


def _is_usable_batch_cache(path: Path) -> bool:
    """True when a batch cache contains at least one successful pricing row."""
    if not path.exists():
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False
    results = payload.get("results")
    return (
        isinstance(results, list)
        and any(isinstance(row, dict) and row.get("status") == "ok" for row in results)
    )


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


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


def _icon_path(root: Path) -> Path | None:
    if sys.platform == "win32":
        icon = root / "assets" / "cblens-icon.ico"
    elif sys.platform == "darwin":
        icon = _mac_icon(root)
    else:
        icon = root / "assets" / "cblens-icon.png"
    return icon if icon and icon.exists() else None


def _detect_windpy() -> tuple[bool, str | None]:
    """检测构建机上是否可导入 WindPy, 返回 (可用, __file__ 路径)."""
    try:
        import WindPy  # noqa: F401
        return True, getattr(WindPy, "__file__", None)
    except Exception:
        return False, None


def _generate_spec(root: Path) -> str:
    """生成 PyInstaller spec 文件内容, 包含 WindPy 条件打包逻辑."""
    icon = _icon_path(root)
    icon_str = _rp(icon) if icon else "None"

    # 收集 data 文件列表
    data_entries = [f"({_rp(root / 'assets')}, 'assets')"]
    for filename in STATIC_DATA_FILES:
        src = root / "data" / filename
        if src.exists():
            if filename == "desktop_batch_pricing_cache.json" and not _is_usable_batch_cache(src):
                print(f"[build] Skip unusable desktop cache seed: {src}")
                continue
            data_entries.append(f"({_rp(src)}, 'data')")

    runtime_cache = root / "data" / BATCH_PRICING_CACHE_FILE
    if _is_usable_batch_cache(runtime_cache):
        data_entries.append(f"({_rp(runtime_cache)}, 'data')")
        print(f"[build] Runtime batch cache detected, will be bundled: {runtime_cache}")
    elif runtime_cache.exists():
        print(f"[build] Skip unusable runtime batch cache: {runtime_cache}")

    # 检测 WindPy
    has_windpy, windpy_file = _detect_windpy()

    windpy_module_mode = "{}"
    windpy_rth = "None"
    network_rth_path = root / "pyi_rth_network.py"
    network_rth = _rp(network_rth_path) if network_rth_path.exists() else "None"

    if has_windpy:
        print(f"[build] WindPy detected ({windpy_file}), will be bundled")
        windpy_module_mode = "{'WindPy': 'pyz+py'}"

        rth_path = root / "pyi_rth_windpy.py"
        if rth_path.exists():
            windpy_rth = _rp(rth_path)

        # pip 包形态的 collect (安全失败)
        spec_collect = f"""
# --- WindPy pip 包形态收集 ---
try:
    _sub = collect_submodules("WindPy")
    hiddenimports += _sub
except Exception:
    pass
try:
    binaries += collect_dynamic_libs("WindPy")
except Exception:
    pass
try:
    datas += collect_data_files("WindPy")
except Exception:
    pass
"""
        # Wind 终端单文件形态: 手工扫 WindPy.__file__ 同目录 + Frameworks (macOS)
        if windpy_file:
            windpy_dir = os.path.dirname(os.path.abspath(windpy_file))
            # macOS: WindPy.py 的 dylib 在 ../Frameworks/ 而非同目录
            _wind_dirs = [windpy_dir]
            if sys.platform == "darwin":
                frameworks_dir = os.path.normpath(os.path.join(windpy_dir, "..", "Frameworks"))
                if os.path.isdir(frameworks_dir):
                    _wind_dirs.append(frameworks_dir)
            _wind_scan_blocks = []
            for _wd in _wind_dirs:
                _wind_scan_blocks.append(f"""
# --- Wind 终端扫描: {_wd} ---
_wind_dir = {repr(_wd)}
if os.path.isdir(_wind_dir):
    for _path in _glob.glob(os.path.join(_wind_dir, '*')):
        if not os.path.isfile(_path):
            continue
        _lower = os.path.basename(_path).lower()
        if _lower.endswith('.exe') or _lower == 'windpy.py':
            continue
        if _lower.endswith(('.dll', '.pyd', '.so', '.dylib')):
            binaries.append((_path, '.'))
        else:
            datas.append((_path, '.'))
""")
            spec_collect += f"""
# --- Wind 终端单文件形态 ---
import glob as _glob
{''.join(_wind_scan_blocks)}
"""
    else:
        print("[build] WindPy not available, skipping (Wind data source will be unavailable)")
        spec_collect = ""

    spec = f"""# -*- mode: python ; coding: utf-8 -*-
# Auto-generated by scripts/build_desktop.py — do not edit manually.
import os
import sys
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

datas = [{', '.join(data_entries)}]
datas += collect_data_files('customtkinter')
datas += collect_data_files('matplotlib')
datas += collect_data_files('numpy')
datas += collect_data_files('certifi')

binaries = []
for _pkg in ('numpy', 'scipy'):
    binaries += collect_dynamic_libs(_pkg)
for _pkg in ('curl_cffi', 'py_mini_racer'):
    try:
        binaries += collect_dynamic_libs(_pkg)
    except Exception:
        pass

_hidden = ['akshare', 'matplotlib.backends.backend_tkagg']
if {has_windpy}:
    _hidden.append('WindPy')
hiddenimports = _hidden
hiddenimports += collect_submodules('numpy')
hiddenimports += collect_submodules('scipy')
for _pkg in (
    'akshare', 'bs4', 'curl_cffi', 'html5lib', 'lxml',
    'openpyxl', 'py_mini_racer', 'requests', 'xlrd',
):
    try:
        hiddenimports += collect_submodules(_pkg)
    except Exception:
        pass
    try:
        datas += collect_data_files(_pkg)
    except Exception:
        pass

{spec_collect}

_runtime_hooks = []
_net_rth = {network_rth}
if _net_rth and os.path.isfile(_net_rth):
    _runtime_hooks.append(_net_rth)
_rth = {windpy_rth}
if _rth and os.path.isfile(_rth):
    _runtime_hooks.append(_rth)

a = Analysis(
    [{_rp(root / 'gui.py')}],
    pathex=[{_rp(root)}],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={{}},
    runtime_hooks=_runtime_hooks,
    excludes=[
        'PyQt5', 'PyQt6', 'PySide2', 'PySide6',
        'IPython', 'jupyter', 'notebook',
        'pytest', 'tests',
    ],
    noarchive=False,
    module_collection_mode={windpy_module_mode},
    optimize=0,
)
pyz = PYZ(a.pure)

if sys.platform == "win32":
    # Windows: onefile 模式 — 单 .exe, 无 COLLECT
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        exclude_binaries=False,
        name='{APP_NAME}',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=[{icon_str}],
    )
else:
    # macOS / Linux: onedir + COLLECT (+ BUNDLE on macOS)
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name='{APP_NAME}',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=[{icon_str}],
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=True,
        upx_exclude=[],
        name='{APP_NAME}',
    )
"""
    if sys.platform == "darwin":
        spec += f"""
app = BUNDLE(
    coll,
    name='{APP_NAME}.app',
    icon={icon_str},
    bundle_identifier='com.grefer.cblens',
)
"""
    return spec


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

    spec_content = _generate_spec(root)
    spec_file = build_dir / f"{APP_NAME}.spec"
    spec_file.write_text(spec_content, encoding="utf-8")
    print(f"[build] spec generated: {spec_file}")

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        str(spec_file),
    ]
    print(f"[build] Building {APP_NAME} ({platform.platform()})")
    env = os.environ.copy()
    env["PYINSTALLER_CONFIG_DIR"] = str(build_dir / "pyinstaller-config")
    env["MPLCONFIGDIR"] = str(build_dir / "mplconfig")
    subprocess.run(cmd, cwd=root, check=True, env=env)
    _postprocess_macos_app(dist)
    print(f"[build] Build complete: {dist}")


if __name__ == "__main__":
    build()
