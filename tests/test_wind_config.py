"""Wind 用户设置、原子更新及父子进程会话一致性。"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from convertible_bond import atomic_io
from convertible_bond import wind_config as config


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("CBLENS_CONFIG_DIR", str(tmp_path / "config"))
    for name in (*config._WINDPY_ENV_NAMES, config.WINDPY_SESSION_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config, "_session_selection", None)


def _module_at(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    module = directory / "WindPy.py"
    module.write_text("raise AssertionError('仅检查文件，不得导入')\n", encoding="utf-8")
    return module


@pytest.mark.parametrize("platform, expected", [
    ("win32", "AppData/Roaming/CBLens/settings.json"),
    ("darwin", "Library/Application Support/CBLens/settings.json"),
    ("linux", ".config/CBLens/settings.json"),
])
def test_platform_settings_path_without_creating_directory(monkeypatch, tmp_path, platform, expected):
    monkeypatch.delenv("CBLENS_CONFIG_DIR")
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(config.sys, "platform", platform)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    target = config.wind_settings_path()
    assert target == tmp_path / expected
    assert config.load_wind_settings() == {"windpy_path": ""}
    assert not target.parent.exists()


@pytest.mark.parametrize("platform, env_name", [("win32", "APPDATA"), ("linux", "XDG_CONFIG_HOME")])
def test_platform_config_root_overrides_data_path(monkeypatch, tmp_path, platform, env_name):
    monkeypatch.delenv("CBLENS_CONFIG_DIR")
    monkeypatch.setattr(config.sys, "platform", platform)
    monkeypatch.setenv(env_name, str(tmp_path / "preferences"))
    monkeypatch.setenv("CBLENS_DATA_DIR", str(tmp_path / "market-data"))
    assert config.wind_settings_path() == tmp_path / "preferences" / "CBLens" / "settings.json"


def test_config_override_is_complete_directory(tmp_path):
    assert config.wind_settings_path() == tmp_path / "config" / "settings.json"


def test_save_and_clear_preserve_unrelated_settings(tmp_path):
    target = config.wind_settings_path()
    target.parent.mkdir()
    target.write_text(json.dumps({"theme": "dark", "window": {"width": 1200}}), encoding="utf-8")
    module = _module_at(tmp_path / "接口目录")
    config.save_windpy_path(str(module.parent))
    assert config.load_wind_settings() == {
        "windpy_path": str(module), "theme": "dark", "window": {"width": 1200},
    }
    config.save_windpy_path(None)
    assert json.loads(target.read_text(encoding="utf-8")) == {"theme": "dark", "window": {"width": 1200}}
    assert config.load_wind_settings()["windpy_path"] == ""
    config.save_windpy_path("")
    assert "windpy_path" not in json.loads(target.read_text(encoding="utf-8"))


@pytest.mark.parametrize("contents", [b"{bad", b"[]", b'{"windpy_path": null}', b'{"windpy_path": 1}', b"\xff\xfe"])
def test_invalid_settings_reject_read_and_write_without_overwriting(tmp_path, contents):
    target = config.wind_settings_path()
    target.parent.mkdir()
    target.write_bytes(contents)
    module = _module_at(tmp_path / "valid")
    with pytest.raises(config.WindSettingsError, match="CBLens"):
        config.load_wind_settings()
    with pytest.raises(config.WindSettingsError):
        config.save_windpy_path(str(module))
    with pytest.raises(config.WindSettingsError):
        config.save_windpy_path(None)
    assert target.read_bytes() == contents
    assert list(target.parent.iterdir()) == [target]


def test_failed_atomic_replace_preserves_previous_settings(monkeypatch, tmp_path):
    old = _module_at(tmp_path / "old")
    new = _module_at(tmp_path / "new")
    config.save_windpy_path(str(old))
    target = config.wind_settings_path()
    original = target.read_bytes()

    def fail_replace(temporary, destination):
        assert temporary.parent == destination.parent
        assert json.loads(temporary.read_text(encoding="utf-8"))["windpy_path"] == str(new)
        assert destination.read_bytes() == original
        raise OSError("模拟磁盘替换失败")

    monkeypatch.setattr(atomic_io, "_replace_with_retry", fail_replace)
    with pytest.raises(OSError, match="模拟"):
        config.save_windpy_path(str(new))
    assert target.read_bytes() == original
    assert list(target.parent.iterdir()) == [target]


def test_normalize_file_directory_and_package_without_execution(tmp_path, monkeypatch):
    module = _module_at(tmp_path / "module")
    assert config.normalize_windpy_path(str(module)) == module
    assert config.normalize_windpy_path(str(module.parent)) == module
    monkeypatch.chdir(tmp_path)
    assert config.normalize_windpy_path("module/WindPy.py") == module
    package = tmp_path / "package" / "WindPy"
    package.mkdir(parents=True)
    init = package / "__init__.py"
    init.write_text("raise AssertionError('不得执行')", encoding="utf-8")
    for selected in (init, package, package.parent):
        assert config.normalize_windpy_path(str(selected)) == init


def test_invalid_path_does_not_create_or_modify_settings(tmp_path):
    other = tmp_path / "other.py"
    other.write_text("", encoding="utf-8")
    for value in (str(other), str(tmp_path), str(tmp_path / "WindPy.py")):
        with pytest.raises(ValueError, match="未找到"):
            config.save_windpy_path(value)
    assert not config.wind_settings_path().exists()


def test_live_selection_env_precedence_and_saved_fallback(monkeypatch, tmp_path):
    saved = _module_at(tmp_path / "saved")
    config.save_windpy_path(str(saved))
    assert config.get_wind_selection() == {"path": str(saved), "source": "settings"}
    for name in reversed(config._WINDPY_ENV_NAMES):
        monkeypatch.setenv(name, f"  {name}-value  ")
        assert config.get_wind_selection() == {"path": f"{name}-value", "source": name}
    for name in config._WINDPY_ENV_NAMES:
        monkeypatch.setenv(name, "   ")
    assert config.get_wind_selection() == {"path": str(saved), "source": "settings"}
    config.save_windpy_path(None)
    assert config.get_wind_selection() == {"path": "", "source": "auto"}


def test_session_stays_fixed_when_settings_or_environment_changes(monkeypatch, tmp_path):
    old = _module_at(tmp_path / "old")
    new = _module_at(tmp_path / "new")
    config.save_windpy_path(str(old))
    selected = config.get_session_wind_selection()
    selected["path"] = "caller mutation"
    config.save_windpy_path(str(new))
    monkeypatch.setenv("CBLENS_WINDPY_PATH", "later-env")
    assert config.get_wind_selection() == {"path": "later-env", "source": "CBLENS_WINDPY_PATH"}
    assert config.get_session_wind_selection() == {"path": str(old), "source": "settings"}


@pytest.mark.parametrize("initial", [{"path": "fixed-path", "source": "settings"}, {"path": "", "source": "auto"}])
def test_inherited_session_overrides_new_environment_and_settings(monkeypatch, tmp_path, initial):
    config.save_windpy_path(str(_module_at(tmp_path / "new")))
    monkeypatch.setenv("CBLENS_WINDPY_PATH", "new-env")
    monkeypatch.setenv(config.WINDPY_SESSION_ENV, json.dumps(initial))
    assert config.get_session_wind_selection() == initial
    assert config.get_wind_selection() == {"path": "new-env", "source": "CBLENS_WINDPY_PATH"}


@pytest.mark.parametrize("value", ["", "broken", "[]", "{}", '{"path": 1, "source": "probe"}', '{"path":"", "source":"settings"}', '{"path":"file", "source":"auto"}'])
def test_invalid_inherited_session_is_not_silently_replaced(monkeypatch, value):
    monkeypatch.setenv(config.WINDPY_SESSION_ENV, value)
    with pytest.raises(config.WindSettingsError, match="Wind 会话"):
        config.get_session_wind_selection()


def test_business_child_inherits_session_while_probe_can_override(monkeypatch, tmp_path):
    config.get_session_wind_selection()  # GUI 启动时固定为自动发现。
    config.save_windpy_path(str(_module_at(tmp_path / "new")))
    monkeypatch.setenv("CBLENS_TEST_UNRELATED_ENV", "preserved")
    env = config.wind_subprocess_env()
    assert env["CBLENS_TEST_UNRELATED_ENV"] == "preserved"
    assert config.WINDPY_SESSION_ENV not in os.environ
    script = (
        "import json, sys; from convertible_bond.wind_config import get_session_wind_selection; "
        "print(json.dumps(get_session_wind_selection())); "
        "assert not any(m.startswith(('WindPy', 'convertible_bond.gui', "
        "'convertible_bond.data_providers')) for m in sys.modules)"
    )
    result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == {"path": "", "source": "auto"}
    probe = {"path": str(tmp_path / "probe" / "WindPy.py"), "source": "probe"}
    env[config.WINDPY_SESSION_ENV] = json.dumps(probe)
    result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == probe
