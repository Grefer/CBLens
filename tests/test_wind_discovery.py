"""模拟所有 Windows 安装来源；测试进程不读取真实注册表或运行外部 Python。"""
from __future__ import annotations

import os
from pathlib import Path
import re
import sys

import pytest

from convertible_bond.data_providers import wind_discovery as discovery


class _RegistryKey:
    def __init__(self, identity):
        self.identity = identity

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class _Registry:
    HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE = 1, 2
    KEY_READ, KEY_WOW64_64KEY, KEY_WOW64_32KEY = 0x10, 0x100, 0x200
    REG_SZ, REG_EXPAND_SZ = 1, 2

    def __init__(self):
        self.nodes = set()
        self.values = {}
        self.app_values = {}
        self.denied = set()
        self.opened = []
        self.queried = []

    def add(self, hive, view, company, tag, value, kind=REG_SZ):
        base = r"Software\Python"
        install = base + f"\\{company}\\{tag}\\InstallPath"
        self.nodes.update({(hive, view, base), (hive, view, base + "\\" + company),
                           (hive, view, install)})
        self.values[hive, view, install] = (value, kind)

    def add_app(self, hive, view, key_name, **values):
        base = discovery._UNINSTALL_KEY
        key = base + "\\" + key_name
        self.nodes.update({(hive, view, base), (hive, view, key)})
        self.app_values[hive, view, key] = values

    def OpenKey(self, root, key, reserved=0, access=KEY_READ):
        assert reserved == 0 and access & self.KEY_READ
        if isinstance(root, _RegistryKey):
            hive, _, parent = root.identity
            key = parent + "\\" + key
        else:
            hive = root
        view = access & (self.KEY_WOW64_64KEY | self.KEY_WOW64_32KEY)
        identity = (hive, view, key)
        self.opened.append(identity)
        if identity in self.denied:
            raise PermissionError(key)
        if identity not in self.nodes:
            raise FileNotFoundError(key)
        return _RegistryKey(identity)

    def EnumKey(self, root, index):
        hive, view, path = root.identity
        prefix = path + "\\"
        names = sorted({key[len(prefix):].split("\\")[0]
                        for item_hive, item_view, key in self.nodes
                        if (item_hive, item_view) == (hive, view) and key.startswith(prefix)})
        try:
            return names[index]
        except IndexError:
            raise OSError("no more entries") from None

    def QueryValueEx(self, key, name):
        self.queried.append(name)
        assert name in {"", "DisplayName", "InstallLocation", "DisplayIcon"}, "不读取可执行命令"
        if name == "":
            return self.values[key.identity]
        try:
            value = self.app_values[key.identity][name]
        except KeyError:
            raise FileNotFoundError(name) from None
        return value if isinstance(value, tuple) else (value, self.REG_SZ)

    def ExpandEnvironmentStrings(self, value):
        return re.sub(r"%([^%]+)%", lambda match: os.environ.get(match[1], match[0]), value)


@pytest.fixture(autouse=True)
def registry(monkeypatch):
    fake = _Registry()
    monkeypatch.setitem(sys.modules, "winreg", fake)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    return fake


def _site(root):
    path = root / "Lib" / "site-packages"
    path.mkdir(parents=True)
    return path


def test_local_python_installations_are_found_without_reading_or_executing_pth(monkeypatch, tmp_path):
    local = tmp_path / "LocalAppData"
    first = _site(local / "Programs" / "Python" / "Python310")
    current = _site(local / "Programs" / "Python" / "Python313")
    _site(local / "Programs" / "Python" / "UnrelatedApp")
    _site(local / "Programs" / "Python" / "nested" / "Python312")
    marker = tmp_path / "must-not-run"
    (current / "WindPy.pth").write_text(
        f"import pathlib; pathlib.Path({str(marker)!r}).touch()\n", encoding="utf-8")
    (current / "WindPy.py").write_text("raise AssertionError('不得导入 Wind')\n", encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    before = list(sys.path)
    assert discovery.windows_python_site_paths() == [first, current]
    assert sys.path == before and not marker.exists()


def test_roaming_python_user_site_is_found(monkeypatch, tmp_path):
    roaming = tmp_path / "Roaming"
    site = roaming / "Python" / "Python313" / "site-packages"
    site.mkdir(parents=True)
    monkeypatch.setenv("APPDATA", str(roaming))
    assert discovery.windows_python_site_paths() == [site]


def test_registry_finds_nondefault_locations_in_both_hives_and_architecture_views(registry, tmp_path):
    expected = []
    for hive in (registry.HKEY_CURRENT_USER, registry.HKEY_LOCAL_MACHINE):
        for view in (registry.KEY_WOW64_64KEY, registry.KEY_WOW64_32KEY):
            root = tmp_path / "custom_volume" / f"installation-{hive}-{view}"
            expected.append(_site(root))
            registry.add(hive, view, "PythonCore", "3.13", str(root))
    assert discovery.windows_python_site_paths() == expected
    assert registry.queried == [""] * 4


def test_duplicate_registry_and_default_paths_are_returned_once(monkeypatch, registry, tmp_path):
    local = tmp_path / "Local"
    root = local / "Programs" / "Python" / "Python313"
    site = _site(root)
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    for view in (registry.KEY_WOW64_64KEY, registry.KEY_WOW64_32KEY):
        registry.add(registry.HKEY_CURRENT_USER, view, "PythonCore", "3.13", str(root))
    assert discovery.windows_python_site_paths() == [site]


def test_inaccessible_registry_branches_do_not_hide_other_installations(registry, tmp_path):
    hidden = tmp_path / "hidden"
    _site(hidden)
    readable = tmp_path / "readable"
    expected = _site(readable)
    registry.add(1, 0x100, "PythonCore", "3.13", str(hidden))
    registry.denied.add((1, 0x100, r"Software\Python"))
    registry.add(1, 0x200, "BlockedVendor", "custom", str(hidden))
    registry.denied.add((1, 0x200, r"Software\Python\BlockedVendor"))
    registry.add(2, 0x100, "AllowedVendor", "opaque-tag", str(readable))
    assert discovery.windows_python_site_paths() == [expected]


def test_missing_site_directories_and_invalid_registry_values_are_ignored(registry, tmp_path):
    registry.add(1, 0x100, "PythonCore", "removed", str(tmp_path / "removed"))
    registry.add(1, 0x100, "PythonCore", "relative", "relative-installation")
    registry.add(1, 0x100, "PythonCore", "not-a-string", 123, kind=4)
    registry.add(1, 0x100, "PythonCore", "empty", "  ")
    registry.add(1, 0x100, "PythonCore", "wrong-type", str(tmp_path), kind=4)
    assert discovery.windows_python_site_paths() == []


def test_registry_expand_string_and_nonstandard_company_tag(registry, monkeypatch, tmp_path):
    root = tmp_path / "External Distribution"
    site = _site(root)
    monkeypatch.setenv("CUSTOM_PY_ROOT", str(root))
    registry.add(1, 0x100, "ExampleVendor", "opaque-identifier", "%CUSTOM_PY_ROOT%", kind=2)
    assert discovery.windows_python_site_paths() == [site]


def test_reserved_launcher_registration_is_ignored(registry, tmp_path):
    root = tmp_path / "launcher"
    _site(root)
    registry.add(1, 0x100, "pYlAuNcHeR", "launcher-tag", str(root))
    assert discovery.registered_python_roots() == []
    assert registry.queried == []


def test_inaccessible_default_directory_still_allows_registry_fallback(monkeypatch, registry, tmp_path):
    local = tmp_path / "Local"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    root = tmp_path / "registered"
    site = _site(root)
    registry.add(1, 0x100, "PythonCore", "3.13", str(root))
    original_scandir = discovery.os.scandir

    def scan(path):
        if Path(path) == local / "Programs" / "Python":
            raise PermissionError("blocked")
        return original_scandir(path)

    monkeypatch.setattr(discovery.os, "scandir", scan)
    assert discovery.windows_python_site_paths() == [site]


def test_registry_enumeration_is_bounded(monkeypatch, registry, tmp_path):
    monkeypatch.setattr(discovery, "_MAX_ENTRIES", 3)
    for index in range(10):
        root = tmp_path / f"python-{index}"
        _site(root)
        registry.add(1, 0x100, "PythonCore", str(index), str(root))
    assert len(discovery.windows_python_site_paths()) == 3


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_other_platforms_do_not_enumerate_windows_directories(monkeypatch, registry, platform):
    monkeypatch.setattr(sys, "platform", platform)
    assert discovery.windows_python_site_paths() == []
    assert discovery.registered_python_roots() == []
    assert discovery.windows_wind_install_paths() == []
    assert registry.opened == []


def test_wind_install_record_finds_custom_volume_without_python(registry, tmp_path):
    root = tmp_path / "D_volume" / "Software" / "Wind"
    interface = root / "x64" / "WindPy.py"
    interface.parent.mkdir(parents=True)
    interface.write_text("raise AssertionError('不得执行接口')\n", encoding="utf-8")
    registry.add_app(2, 0x100, "wind-install", DisplayName="Wind金融终端", InstallLocation=str(root))
    before = list(sys.path)
    assert discovery.windows_wind_install_paths() == [root]
    assert sys.path == before
    assert not any(path.startswith(r"Software\Python") for _, _, path in registry.opened)


@pytest.mark.parametrize("name", [
    "Wind金融终端", "Wind 资讯金融终端", "万得金融终端", "Wind.NET.Client", "WindNETClient",
    "Wind API", "Wind Financial Terminal", "Wind",
])
def test_wind_product_names_are_recognized(registry, tmp_path, name):
    root = tmp_path / "terminal"
    root.mkdir()
    registry.add_app(1, 0x100, "product", DisplayName=name, InstallLocation=str(root))
    assert discovery.windows_wind_install_paths() == [root]


@pytest.mark.parametrize("name", [
    "Microsoft Windows Desktop Runtime", "Windows Financial Terminal", "Windscribe",
    "OpenWind API", "Windmill", "Windows SDK",
])
def test_similar_names_do_not_match_wind(registry, tmp_path, name):
    root = tmp_path / "other-software"
    root.mkdir()
    registry.add_app(1, 0x100, "product", DisplayName=name, InstallLocation=str(root))
    assert discovery.windows_wind_install_paths() == []
    assert registry.queried == ["DisplayName"]


@pytest.mark.parametrize("format", ['"{exe}",0', '"{exe}" --show-window', '{exe},-1', '{exe} --show-window'])
def test_icon_executable_fills_missing_install_location_without_running_parameters(registry, tmp_path, format):
    root = tmp_path / "E_volume" / "Wind Terminal"
    root.mkdir(parents=True)
    executable = root / "Wind.exe"
    executable.write_text("raise AssertionError('不得运行')\n", encoding="utf-8")
    registry.add_app(2, 0x200, "wind", DisplayName="Wind.NET", DisplayIcon=format.format(exe=executable))
    assert discovery.windows_wind_install_paths() == [root]


def test_install_location_takes_priority_over_icon(registry, tmp_path):
    root, icon_root = tmp_path / "Wind", tmp_path / "cached-icon"
    root.mkdir()
    icon_root.mkdir()
    executable = icon_root / "Wind.exe"
    executable.touch()
    registry.add_app(1, 0x100, "wind", DisplayName="Wind金融终端", InstallLocation=str(root),
                     DisplayIcon=str(executable))
    assert discovery.windows_wind_install_paths() == [root]
    assert "DisplayIcon" not in registry.queried


def test_wind_records_cover_all_views_and_deduplicate(registry, tmp_path):
    root = tmp_path / "Wind"
    root.mkdir()
    for hive in (registry.HKEY_CURRENT_USER, registry.HKEY_LOCAL_MACHINE):
        for view in (registry.KEY_WOW64_64KEY, registry.KEY_WOW64_32KEY):
            registry.add_app(hive, view, "wind", DisplayName="Wind金融终端", InstallLocation=str(root))
    assert discovery.windows_wind_install_paths() == [root]
    assert len([key for key in registry.opened if key[2] == discovery._UNINSTALL_KEY]) == 4


def test_inaccessible_wind_record_does_not_hide_another_installation(registry, tmp_path):
    root = tmp_path / "Wind"
    root.mkdir()
    registry.add_app(1, 0x100, "blocked", DisplayName="Wind", InstallLocation=str(root))
    registry.denied.add((1, 0x100, discovery._UNINSTALL_KEY + r"\blocked"))
    registry.add_app(2, 0x200, "working", DisplayName="Wind", InstallLocation=str(root))
    assert discovery.windows_wind_install_paths() == [root]


@pytest.mark.parametrize("icon", ['cmd.exe /c "D:\\Wind\\Wind.exe"', '"D:\\Wind\\Wind.exe',
                                  r"\\server\share\Wind.exe,0", "relative/Wind.exe,0"])
def test_ambiguous_nonexistent_or_remote_icon_is_ignored(registry, icon):
    registry.add_app(1, 0x100, "wind", DisplayName="Wind金融终端", DisplayIcon=icon)
    assert discovery.windows_wind_install_paths() == []


def test_stale_install_location_can_fall_back_to_actual_icon_executable(registry, tmp_path):
    executable = tmp_path / "Wind.exe"
    executable.touch()
    registry.add_app(1, 0x100, "wind", DisplayName="Wind金融终端", InstallLocation=str(tmp_path / "removed"),
                     DisplayIcon=str(executable))
    assert discovery.windows_wind_install_paths() == [tmp_path]


def test_wind_scan_checks_beyond_first_128_other_products(registry, tmp_path):
    for index in range(140):
        registry.add_app(1, 0x100, f"product-{index:03}", DisplayName="Other Software")
    root = tmp_path / "Wind"
    root.mkdir()
    registry.add_app(1, 0x100, "z-Wind", DisplayName="Wind金融终端", InstallLocation=str(root))
    assert discovery.windows_wind_install_paths() == [root]
