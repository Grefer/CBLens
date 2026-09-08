"""有界发现 Windows 的 Wind 安装位置及 Python 接口记录，不执行第三方文件。"""
from __future__ import annotations

from itertools import islice
import os
from pathlib import Path
import re
import sys


# 只枚举固定父目录及注册表的 Company/Tag 两层；异常大量条目不拖长 GUI 启动。
_MAX_ENTRIES = 128
_MAX_UNINSTALL_ENTRIES = 2048
_UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"


def _unique_paths(paths: list[Path]) -> list[Path]:
    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        try:
            path = path.resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        key = str(path).casefold()  # Windows 路径不区分大小写。
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def _registry_names(registry, key, *, limit: int | None = None) -> list[str]:
    names = []
    for index in range(_MAX_ENTRIES if limit is None else limit):
        try:
            names.append(registry.EnumKey(key, index))
        except OSError:
            break
    return sorted(names, key=str.casefold)


def _registry_string(registry, key, name: str) -> str:
    try:
        value, kind = registry.QueryValueEx(key, name)
        if not isinstance(value, str):
            return ""
        if kind == registry.REG_EXPAND_SZ:
            value = registry.ExpandEnvironmentStrings(value)
        elif kind != registry.REG_SZ:
            return ""
        return value.strip()
    except (OSError, ValueError):
        return ""


def _is_wind_product(name: str) -> bool:
    """识别终端/API 产品名，不把 Windows、Windscribe 等相似名称当作 Wind。"""
    return ("万得" in name or name.casefold().strip() == "wind" or bool(re.search(
        r"(?<![a-z])wind[ ._-]*(?:金融|资讯|api\b|net(?:[ ._-]*client)?\b|(?:financial\s+)?terminal\b)",
        name, flags=re.IGNORECASE,
    )))


def _local_absolute_path(value: str) -> Path | None:
    value = value.strip()
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    # 不因自动发现访问 UNC 共享或设备路径；手工选择仍由统一加载器处理。
    if not value or value.startswith(("\\\\", "//")) or "\x00" in value:
        return None
    path = Path(value)
    return path if path.is_absolute() else None


def _icon_executable(value: str) -> Path | None:
    """只提取 DisplayIcon 中明确的 .exe 路径，不解释或执行后面的参数。"""
    if value.startswith('"'):
        end = value.find('"', 1)
        filename = value[1:end] if end > 0 else ""
    else:
        match = re.match(r"^(.+?\.exe)(?=\s|,|$)", value, flags=re.IGNORECASE)
        filename = match[1] if match else ""
    path = _local_absolute_path(filename)
    try:
        return path if path is not None and path.suffix.casefold() == ".exe" and path.is_file() else None
    except OSError:
        return None


def windows_wind_install_paths() -> list[Path]:
    """从标准卸载记录定位本机 Wind，无须安装任何外部 Python。

    微软规定 InstallLocation 为产品主目录，见 Windows Installer 的
    uninstall-registry-key / arpinstalllocation 文档。仅读 DisplayName、
    InstallLocation 与 DisplayIcon，绝不运行 UninstallString 或图标文件。
    仅返回有安装证据的现存目录；调用方再在目录内按有限布局查找 WindPy。
    """
    if sys.platform != "win32":
        return []
    try:
        import winreg
    except ImportError:
        return []
    paths: list[Path] = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            access = winreg.KEY_READ | view
            try:
                with winreg.OpenKey(hive, _UNINSTALL_KEY, 0, access) as root:
                    for name in _registry_names(winreg, root, limit=_MAX_UNINSTALL_ENTRIES):
                        try:
                            with winreg.OpenKey(root, name, 0, access) as app:
                                if not _is_wind_product(_registry_string(winreg, app, "DisplayName")):
                                    continue
                                location = _local_absolute_path(_registry_string(winreg, app, "InstallLocation"))
                                if location is not None and location.is_dir():
                                    paths.append(location)
                                    continue
                                executable = _icon_executable(_registry_string(winreg, app, "DisplayIcon"))
                                if executable is not None:
                                    paths.append(executable.parent)
                        except (OSError, ValueError):
                            continue
            except OSError:
                continue
    return _unique_paths(paths)


def registered_python_roots() -> list[Path]:
    """只读 PEP 514 InstallPath 默认值，覆盖用户/机器的 32/64 位注册视图。

    https://peps.python.org/pep-0514/：Software/Python/Company/Tag/InstallPath
    的默认值为 sys.prefix。这里收集所有注册位置，保留 HKCU 优先；不解释 Tag，
    不读取或运行 ExecutablePath，也不要求外部 Python 与 CBLens 版本相同。
    """
    if sys.platform != "win32":
        return []
    try:
        import winreg
    except ImportError:
        return []
    paths: list[Path] = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            access = winreg.KEY_READ | view
            try:
                with winreg.OpenKey(hive, r"Software\Python", 0, access) as root:
                    for company in _registry_names(winreg, root):
                        if company.casefold() == "pylauncher":
                            continue
                        try:
                            with winreg.OpenKey(root, company, 0, access) as company_key:
                                for tag in _registry_names(winreg, company_key):
                                    try:
                                        with winreg.OpenKey(company_key, tag + r"\InstallPath", 0, access) as install:
                                            value, kind = winreg.QueryValueEx(install, "")
                                        if not isinstance(value, str) or not value.strip():
                                            continue
                                        if kind == winreg.REG_EXPAND_SZ:
                                            value = winreg.ExpandEnvironmentStrings(value)
                                        elif kind != winreg.REG_SZ:
                                            continue
                                        path = Path(value.strip())
                                        if path.is_absolute():
                                            paths.append(path)
                                    except (OSError, ValueError):
                                        continue
                        except OSError:
                            continue
            except OSError:
                continue
    return _unique_paths(paths)


def _python_children(parent: Path) -> list[Path]:
    """只枚举给定父目录的一层 Python* 子目录，不递归、不读取接口文件。"""
    paths: list[Path] = []
    try:
        with os.scandir(parent) as entries:
            for entry in islice(entries, _MAX_ENTRIES):
                try:
                    if entry.name.casefold().startswith("python") and entry.is_dir():
                        paths.append(Path(entry.path))
                except OSError:
                    continue
    except OSError:
        pass
    return sorted(paths, key=lambda path: path.name.casefold())


def windows_python_site_paths() -> list[Path]:
    """返回其他 Python 已存在的 site-packages 目录，不修改 sys.path。

    Wind 的安装程序可能仅为外部 Python 写入 WindPy.pth；CBLens 冻结包不会自动
    处理那个解释器的 site 目录。调用方只读这些目录下的 WindPy 路径信息，不可
    仅凭发现 Python 就添加整个目录；确实定位接口后才准备其导入目录，也不可通过
    site.addsitedir 执行 .pth。
    """
    if sys.platform != "win32":
        return []
    candidates: list[Path] = []
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if local:
        for root in _python_children(Path(local) / "Programs" / "Python"):
            candidates.append(root / "Lib" / "site-packages")
    roaming = os.environ.get("APPDATA", "").strip()
    if roaming:
        for root in _python_children(Path(roaming) / "Python"):
            candidates.append(root / "site-packages")
    candidates.extend(root / "Lib" / "site-packages" for root in registered_python_roots())
    existing: list[Path] = []
    for path in candidates:
        try:
            if path.is_dir():
                existing.append(path)
        except OSError:
            continue
    return _unique_paths(existing)
