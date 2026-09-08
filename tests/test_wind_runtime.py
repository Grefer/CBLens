"""外置 Wind 接口的真实文件导入与冻结包路径兼容；不连接 Wind。"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from convertible_bond.data_providers import wind
from convertible_bond.data_providers import wind_runtime as runtime


@pytest.fixture(autouse=True)
def isolate_runtime(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.delitem(sys.modules, "WindPy", raising=False)
    monkeypatch.setattr(runtime, "_RUNTIMES", {})
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(wind.site, "getsitepackages", lambda: [])
    monkeypatch.setattr(wind.site, "getusersitepackages", lambda: str(tmp_path / "missing"))
    yield
    sys.modules.pop("WindPy", None)
    for temp, _, handles in runtime._RUNTIMES.values():
        for handle in handles:
            handle.close()
        temp.cleanup()


def _choose(monkeypatch, path, source="settings"):
    monkeypatch.setattr(wind, "get_session_wind_selection", lambda: {
        "path": str(path), "source": source,
    })


def _module(directory, source="w = object()\n"):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "WindPy.py"
    path.write_text(source, encoding="utf-8")
    return path


def test_selected_file_bypasses_frozen_module_finder(monkeypatch, tmp_path):
    bundle = _module(tmp_path / "bundle", "raise AssertionError('不应加载包内接口')\n")
    selected = _module(tmp_path / "external", "origin = 'selected'\nw = object()\n")
    _choose(monkeypatch, selected)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle.parent), raising=False)

    class FrozenFinder:
        def find_spec(self, fullname, *args):
            if fullname == "WindPy":
                raise AssertionError("明确选中的文件不应经过冻结包 finder")

    monkeypatch.setattr(sys, "meta_path", [FrozenFinder(), *sys.meta_path])
    module = wind.load_windpy()
    from WindPy import w
    assert w is module.w
    assert Path(module.__file__) == selected
    assert module.origin == "selected"
    assert wind.load_windpy() is module


def test_explicit_missing_path_never_falls_back(monkeypatch, tmp_path):
    other = _module(tmp_path / "other")
    sys.path.insert(0, str(other.parent))
    _choose(monkeypatch, tmp_path / "missing" / "WindPy.py")
    with pytest.raises(ModuleNotFoundError) as caught:
        wind.load_windpy()
    assert caught.value.name == "WindPy"
    assert "指定" in str(caught.value)
    assert "WindPy" not in sys.modules


def test_terms_rebuild_cli_uses_selected_interface(monkeypatch, tmp_path):
    from convertible_bond.cli.rebuild_terms_patches import rebuild

    marker = tmp_path / "called-selected-interface"
    selected = _module(tmp_path / "selected", f"""
from pathlib import Path
class w:
    @staticmethod
    def start():
        Path({str(marker)!r}).touch()
""")
    _choose(monkeypatch, selected)
    # 无效字段在读写条款库之前终止；只检查实际 CLI 能加载所选接口。
    with pytest.raises(ValueError, match="没有可重建的字段"):
        rebuild(fields=["invalid_field"])
    assert marker.is_file()
    assert Path(sys.modules["WindPy"].__file__).resolve() == selected.resolve()


def test_loaded_different_interface_requires_restart(monkeypatch, tmp_path):
    first = _module(tmp_path / "first")
    second = _module(tmp_path / "second")
    _choose(monkeypatch, first)
    loaded = wind.load_windpy()
    _choose(monkeypatch, second)
    with pytest.raises(ImportError, match="重启"):
        wind.load_windpy()
    assert sys.modules["WindPy"] is loaded


@pytest.mark.parametrize("choice", ["package", "init", "parent"])
def test_selected_package_imports_its_relative_modules(monkeypatch, tmp_path, choice):
    package = tmp_path / "WindPy"
    package.mkdir()
    init = package / "__init__.py"
    init.write_text("from .api import w\n", encoding="utf-8")
    (package / "api.py").write_text("w = '本地接口'\n", encoding="utf-8")
    selected = {"package": package, "init": init, "parent": tmp_path}[choice]
    _choose(monkeypatch, selected)
    monkeypatch.delitem(sys.modules, "WindPy.api", raising=False)
    module = wind.load_windpy()
    assert module.w == "本地接口"
    assert Path(module.__file__) == init
    sys.modules.pop("WindPy.api", None)


def test_explicit_package_file_is_not_shadowed_by_neighboring_module(monkeypatch, tmp_path):
    package = tmp_path / "WindPy"
    package.mkdir()
    init = package / "__init__.py"
    init.write_text("w = 'package'\n", encoding="utf-8")
    _module(tmp_path, "raise AssertionError('选中包时不应导入邻近的单文件接口')\n")
    _choose(monkeypatch, init)
    assert wind.load_windpy().w == "package"


def test_auto_discovers_site_pth_without_executing_code(monkeypatch, tmp_path):
    _choose(monkeypatch, "", "auto")
    site_dir = tmp_path / "site-packages"
    site_dir.mkdir()
    actual = _module(tmp_path / "terminal" / "x64")
    marker = tmp_path / "unsafe"
    (site_dir / "WindPy.pth").write_text(
        f"# Wind API\n\nimport pathlib; pathlib.Path({str(marker)!r}).touch()\n"
        "../terminal/x64\n", encoding="utf-8",
    )
    monkeypatch.setattr(wind.site, "getsitepackages", lambda: [str(site_dir)])
    module = wind.load_windpy()
    assert Path(module.__file__).resolve() == actual.resolve()
    assert not marker.exists()


def test_windows_external_pth_precedes_bundle_and_retains_dll_handles(monkeypatch, tmp_path):
    selected = _module(tmp_path / "terminal" / "x64", """
import ctypes
import pathlib
import sys
site_dir = next(path for path in sys.path if pathlib.Path(path).name == 'site-packages')
runtime_dir = pathlib.Path(pathlib.Path(site_dir, 'WindPy.pth').read_text().strip())
native = ctypes.CDLL(str(runtime_dir / 'WindPy.dll'))
w = object()
"""
    )
    native_file = selected.parent / "WindPy.dll"
    native_file.write_bytes(b"test native handle")
    bundle_site = tmp_path / "bundle" / "site-packages"
    bundle_site.mkdir(parents=True)
    (bundle_site / "WindPy.pth").write_text(str(bundle_site.parent))
    sys.path.insert(0, str(bundle_site))
    _choose(monkeypatch, selected)
    monkeypatch.setattr(sys, "platform", "win32")
    calls, handles, closed = [], [], []

    def add_dll_directory(path):
        handle = SimpleNamespace(close=lambda: closed.append(path))
        handles.append(handle)
        calls.append(path)
        return handle

    import ctypes
    loaded_native = []
    monkeypatch.setattr(runtime.os, "add_dll_directory", add_dll_directory, raising=False)
    monkeypatch.setattr(ctypes, "CDLL", lambda path: loaded_native.append(path) or object())
    module = wind.load_windpy()
    assert module.runtime_dir == selected.parent
    assert loaded_native == [str(native_file)]
    assert calls == [str(selected.parent)]
    assert next(iter(runtime._RUNTIMES.values()))[2] == handles
    assert closed == []
    assert (bundle_site / "WindPy.pth").read_text() == str(bundle_site.parent)
    assert not (selected.parent / "WindPy.pth").exists()


def test_local_pth_links_separate_runtime_and_never_executes(monkeypatch, tmp_path):
    selected = _module(tmp_path / "python")
    native_dir = tmp_path / "native"
    native_dir.mkdir()
    (native_dir / "WindPy.dll").write_bytes(b"fake")
    (selected.parent / "WindPy.pth").write_text(
        "import no_such_module\n../native\n", encoding="utf-8",
    )
    monkeypatch.setattr(sys, "platform", "win32")
    _choose(monkeypatch, selected)
    monkeypatch.delattr(runtime.os, "add_dll_directory", raising=False)
    wind.load_windpy()
    assert next(iter(runtime._RUNTIMES.values()))[1] == native_dir


@pytest.mark.parametrize("message", [
    "[WinError 193] %1 不是有效的 Win32 应用程序。",
    "Could not find module 'WindPy.dll' (or one of its dependencies).",
])
def test_native_import_failure_preserves_original_error(monkeypatch, tmp_path, message):
    selected = _module(tmp_path / "terminal", f"raise OSError({message!r})\n")
    _choose(monkeypatch, selected)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delattr(runtime.os, "add_dll_directory", raising=False)
    with pytest.raises(OSError) as caught:
        wind.load_windpy()
    assert str(caught.value) == message
    assert "WindPy" not in sys.modules


def test_pth_decodes_windows_legacy_encoding(monkeypatch, tmp_path):
    native = tmp_path / "金融终端"
    native.mkdir()
    pth = tmp_path / "WindPy.pth"
    pth.write_bytes("金融终端\n".encode("cp936"))
    monkeypatch.setattr(runtime.locale, "getpreferredencoding", lambda *args: "cp936")
    assert runtime.read_windpy_pth(pth) == [native]
