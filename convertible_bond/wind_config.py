"""Wind 接口用户设置与会话选择：只读文件和环境变量，不导入或连接 Wind。"""
from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from typing import Any

from .atomic_io import atomic_write_json


WINDPY_SESSION_ENV = "CBLENS_WINDPY_SESSION_SELECTION"
_WINDPY_ENV_NAMES = ("CBLENS_WINDPY_PATH", "WINDPY_PATH", "WINDPY_DIR")
_session_selection: dict[str, str] | None = None
_session_selection_lock = threading.Lock()


class WindSettingsError(ValueError):
    """设置文件损坏或内部会话选择无效，不能静默换成其他接口。"""


def wind_settings_path() -> Path:
    """返回独立于行情数据目录的用户设置位置；读取时不创建目录。"""
    override = os.environ.get("CBLENS_CONFIG_DIR", "").strip()
    if override:
        return Path(override).expanduser() / "settings.json"
    if sys.platform == "win32":
        base = os.environ.get("APPDATA", "").strip()
        root = Path(base) if base else Path.home() / "AppData" / "Roaming"
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        base = os.environ.get("XDG_CONFIG_HOME", "").strip()
        root = Path(base) if base else Path.home() / ".config"
    return root / "CBLens" / "settings.json"


def load_wind_settings() -> dict[str, Any]:
    """读取设置并补齐空路径；文件缺失正常，损坏内容必须明确报错。"""
    target = wind_settings_path()
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"windpy_path": ""}
    except UnicodeError as exc:
        raise WindSettingsError(f"CBLens 设置文件不是有效的 UTF-8 文本：{target}。") from exc
    try:
        settings = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise WindSettingsError(f"CBLens 设置文件无法解析：{target}。请修复后重试。") from exc
    if not isinstance(settings, dict):
        raise WindSettingsError(f"CBLens 设置文件应为 JSON 对象：{target}。")
    if not isinstance(settings.get("windpy_path", ""), str):
        raise WindSettingsError(f"CBLens 设置 windpy_path 必须是路径字符串：{target}。")
    return {**settings, "windpy_path": settings.get("windpy_path", "")}


def normalize_windpy_path(path: str) -> Path:
    """验证文件/目录并返回模块绝对路径，不执行其中的 Python 代码。"""
    value = path.strip()
    if not value:
        raise ValueError("请选择 WindPy.py 文件或包含 WindPy 接口的目录。")
    candidate = Path(os.path.expandvars(value)).expanduser()
    if candidate.is_dir():
        files = [candidate / "WindPy.py", candidate / "WindPy" / "__init__.py"]
        if candidate.name == "WindPy":
            files.append(candidate / "__init__.py")
    elif candidate.name == "WindPy.py" or (
        candidate.name == "__init__.py" and candidate.parent.name == "WindPy"
    ):
        files = [candidate]
    else:
        files = []
    for module_file in files:
        if module_file.is_file():
            return module_file.resolve()
    raise ValueError(f"所选位置未找到 WindPy.py 或 WindPy 包：{candidate}。")


def save_windpy_path(path: str | None) -> None:
    """原子更新接口路径并保留其他设置；清除后下次启动恢复自动发现。"""
    settings = load_wind_settings()
    if path is None or not path.strip():
        settings.pop("windpy_path", None)
    else:
        settings["windpy_path"] = str(normalize_windpy_path(path))
    atomic_write_json(wind_settings_path(), settings)


def get_wind_selection() -> dict[str, str]:
    """实时选择：用户环境变量优先，其次应用设置，最后自动发现。"""
    for name in _WINDPY_ENV_NAMES:
        value = os.environ.get(name, "").strip()
        if value:
            return {"path": value, "source": name}
    saved = load_wind_settings()["windpy_path"].strip()
    if saved:
        return {"path": saved, "source": "settings"}
    return {"path": "", "source": "auto"}


def _inherited_wind_selection(value: str) -> dict[str, str]:
    """读取父进程冻结的选择；空路径明确表示自动发现，不再读取新设置。"""
    try:
        selected = json.loads(value)
    except (json.JSONDecodeError, ValueError) as exc:
        raise WindSettingsError("Wind 会话接口配置无效，请重新启动 CBLens。") from exc
    if not isinstance(selected, dict):
        raise WindSettingsError("Wind 会话接口配置必须包含 path 和 source。")
    path, source = selected.get("path"), selected.get("source")
    if not isinstance(path, str) or not isinstance(source, str) or not source.strip():
        raise WindSettingsError("Wind 会话接口配置必须包含路径字符串和来源。")
    if (not path.strip()) != (source == "auto"):
        raise WindSettingsError("Wind 会话接口路径与来源不一致，请重新启动 CBLens。")
    return {"path": path.strip(), "source": source}


def get_session_wind_selection() -> dict[str, str]:
    """首次读取后固定当前进程选择；保存新路径只影响重启后的会话。"""
    global _session_selection
    with _session_selection_lock:
        if _session_selection is None:
            inherited = os.environ.get(WINDPY_SESSION_ENV)
            _session_selection = (
                _inherited_wind_selection(inherited)
                if inherited is not None else get_wind_selection()
            )
        return dict(_session_selection)


def wind_subprocess_env() -> dict[str, str]:
    """业务子进程沿用本次启动的选择，避免保存设置后无意切换接口。

    独立探测进程可以显式覆盖 ``WINDPY_SESSION_ENV``：JSON 对象 ``path`` 指向
    待测模块、``source`` 为 ``probe``；探测自动发现时设置空路径及 ``auto``。
    """
    env = dict(os.environ)
    env[WINDPY_SESSION_ENV] = json.dumps(get_session_wind_selection(), ensure_ascii=False)
    return env
