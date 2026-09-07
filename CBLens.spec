# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for CBLens (参考模板).

实际构建由 ``scripts/build_desktop.py`` 动态生成 spec 文件, 该文件仅作参考.
如需手动构建: ``pyinstaller --noconfirm CBLens.spec``
"""
import os
import sys
from pathlib import Path
from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
)

ROOT = Path(SPECPATH).resolve()

# ── 数据文件 ────────────────────────────────────────────
datas = [
    (str(ROOT / "assets"), "assets"),
    (str(ROOT / "data" / "cb_data.json"), "data"),
    (str(ROOT / "data" / "cb_events.json"), "data"),
    (str(ROOT / "data" / "down_reset_overrides.json"), "data"),
    (str(ROOT / "data" / "batch_pricing_cache.json"), "data"),
]
datas += collect_data_files("customtkinter")
datas += collect_data_files("matplotlib")
datas += collect_data_files("numpy")

# ── 动态库 ──────────────────────────────────────────────
binaries = []
for _pkg in ("numpy", "scipy"):
    binaries += collect_dynamic_libs(_pkg)

# ── 隐藏导入 ────────────────────────────────────────────
hiddenimports = [
    "akshare",
    "matplotlib.backends.backend_tkagg",
]
# GUI 的「🌐 同步池」菜单按**字符串模块名**起子进程, PyInstaller 的静态分析看不见 ——
# 实测从 gui.py 静态可达的 35 个模块里 convertible_bond.cli.* 一个都没有, 所以冻结包
# 里这几个模块根本不存在。名单与 convertible_bond.cli.POOL_SYNC_MODULES 一致
# (有守护测试比对)。
hiddenimports += [
    "convertible_bond.cli.sync_tradable",
    "convertible_bond.cli.sync_admission_status",
    "convertible_bond.cli.sync_events",
    "convertible_bond.cli.sync_new_issues",
]
hiddenimports += collect_submodules("numpy")
hiddenimports += collect_submodules("scipy")

# ── WindPy: 可选打包 ───────────────────────────────────
_has_windpy = False
_windpy_file = None
try:
    import WindPy  # noqa: F401
    _has_windpy = True
    _windpy_file = getattr(WindPy, "__file__", None)
except Exception as _err:
    print(f"[CBLens.spec] WindPy 不可用, 跳过打包 ({_err!r})")

if _has_windpy:
    print(f"[CBLens.spec] WindPy 已安装 ({_windpy_file}), 打入发布包")
    hiddenimports += ["WindPy"]
    try:
        hiddenimports += collect_submodules("WindPy")
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

    # Wind 终端单文件形态: 手工扫描同目录依赖
    _windpy_dir = os.path.dirname(os.path.abspath(_windpy_file)) if _windpy_file else None
    if _windpy_dir and os.path.isdir(_windpy_dir):
        import glob as _glob
        for _path in _glob.glob(os.path.join(_windpy_dir, "*")):
            if not os.path.isfile(_path):
                continue
            _lower = os.path.basename(_path).lower()
            if _lower.endswith(".exe") or _lower == "windpy.py":
                continue
            if _lower.endswith((".dll", ".pyd", ".so", ".dylib")):
                binaries.append((_path, "."))
            else:
                datas.append((_path, "."))

    _module_collection_mode = {"WindPy": "pyz+py"}
else:
    _module_collection_mode = {}

# ── Runtime hook ────────────────────────────────────────
_runtime_hooks = []
_rthook = str(ROOT / "pyi_rth_windpy.py")
if _has_windpy and os.path.isfile(_rthook):
    _runtime_hooks.append(_rthook)

# ── Analysis ────────────────────────────────────────────
a = Analysis(
    [str(ROOT / "gui.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=_runtime_hooks,
    excludes=[
        "PyQt5", "PyQt6", "PySide2", "PySide6",
        "IPython", "jupyter", "notebook",
        "pytest", "tests",
    ],
    noarchive=False,
    module_collection_mode=_module_collection_mode,
    optimize=0,
)
pyz = PYZ(a.pure)

_icon = ROOT / "assets" / ("cblens-icon.ico" if sys.platform == "win32" else "cblens-icon.icns")
if not _icon.exists():
    _icon = ROOT / "build" / "cblens-icon.icns"

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="CBLens",
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
    icon=[str(_icon)] if _icon.exists() else [],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="CBLens",
)
app = BUNDLE(
    coll,
    name="CBLens.app",
    icon=str(_icon) if _icon.exists() else None,
    bundle_identifier="com.grefer.cblens",
)
