"""Wind 本机运行库准备：安全读取路径文件，不修改第三方安装目录。"""
from __future__ import annotations

import locale
import os
from pathlib import Path
import sys
import tempfile
import threading


def read_windpy_pth(path: Path) -> list[Path]:
    """只读取 .pth 的路径行；绝不执行其中的 Python import 语句。"""
    try:
        raw = path.read_bytes()
    except OSError:
        return []
    content = None
    for encoding in dict.fromkeys(("utf-8-sig", locale.getpreferredencoding(False))):
        try:
            content = raw.decode(encoding)
            break
        except (UnicodeError, LookupError):
            continue
    if content is None:
        return []
    paths: list[Path] = []
    for line in content.splitlines():
        value = line.strip()
        if not value or value.startswith("#") or value.startswith(("import ", "import\t")):
            continue
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = path.parent / candidate
        if candidate.is_dir() and candidate not in paths:
            paths.append(candidate)
    return paths


def windpy_module_file(candidate: Path) -> Path | None:
    """把接口文件或文件夹解析为真实模块文件，不导入接口。"""
    if candidate.is_file():
        if candidate.name.lower() == "windpy.py" or (
            candidate.name == "__init__.py" and candidate.parent.name.lower() == "windpy"
        ):
            return candidate
        return None
    if candidate.name.lower() == "windpy" and (candidate / "__init__.py").is_file():
        return candidate / "__init__.py"
    for path in (candidate / "WindPy.py", candidate / "WindPy" / "__init__.py"):
        if path.is_file():
            return path
    return None


def _contains_dll(path: Path) -> bool:
    try:
        return any(entry.is_file() and entry.suffix.lower() == ".dll" for entry in path.iterdir())
    except OSError:
        return False


def _runtime_directory(module_file: Path) -> Path:
    """只查接口邻近目录及其配套 .pth，避免借用另一套 Wind 的 DLL。"""
    module_dir = module_file.parent
    linked = read_windpy_pth(module_dir / "WindPy.pth")
    # Wind 安装程序常把接口放在 x64；Python 接口单列在 python/ 时其 DLL 在邻近 x64/。
    candidates = [module_dir, *linked, module_dir / "x64", module_dir.parent / "x64"]
    for candidate in candidates:
        if _contains_dll(candidate):
            return candidate.resolve()
    # 不猜 DLL 名称或替换 Wind 的加载器；缺库/架构错误由实际 import 原样报告。
    return (linked[0] if linked else module_dir).resolve()


_RUNTIME_LOCK = threading.RLock()
_RUNTIMES: dict[str, tuple[tempfile.TemporaryDirectory, Path, list[object]]] = {}


def prepare_windows_wind_runtime(module_file: Path) -> Path | None:
    """为 Windows WindPy 提供本进程的 .pth 与 DLL 搜索目录，返回运行库目录。

    WindPy 在 import 时从 sys.path 中的 site-packages/WindPy.pth 读取安装路径。
    窗口化冻结包没有该布局；临时目录只在本进程存在，外置接口不会被 _MEIPASS 覆盖。
    DLL 句柄与临时目录均保留到进程结束，防止 GC 提前撤销搜索路径。
    """
    if sys.platform != "win32":
        return None
    key = str(module_file.resolve())
    with _RUNTIME_LOCK:
        current = _RUNTIMES.get(key)
        if current is None:
            runtime_dir = _runtime_directory(module_file)
            temp = tempfile.TemporaryDirectory(prefix="cblens-wind-", ignore_cleanup_errors=True)
            handles: list[object] = []
            try:
                site_dir = Path(temp.name) / "site-packages"
                site_dir.mkdir()
                # 使用与 WindPy 内部 open() 相同的默认文本编码，并补目录分隔符兼容字符串拼接。
                (site_dir / "WindPy.pth").write_text(str(runtime_dir) + os.sep)
                add_directory = getattr(os, "add_dll_directory", None)
                if callable(add_directory):
                    for directory in dict.fromkeys((runtime_dir, module_file.parent.resolve())):
                        handles.append(add_directory(str(directory)))
                current = (temp, runtime_dir, handles)
                _RUNTIMES[key] = current
            except BaseException:
                for handle in handles:
                    handle.close()
                temp.cleanup()
                raise
        site_path = str(Path(current[0].name) / "site-packages")
        sys.path[:] = [entry for entry in sys.path if entry != site_path]
        sys.path.insert(0, site_path)
        return current[1]
