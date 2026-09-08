"""Wind 接口初始化错误：用户操作指引与技术诊断分开保存。"""
from __future__ import annotations

from dataclasses import dataclass


WIND_NOT_FOUND = "未找到 Wind Python 接口（WindPy）"
WIND_LOAD_FAILED = "Wind Python 接口加载失败"
WIND_CONNECT_FAILED = "Wind 连接失败"


@dataclass(frozen=True)
class WindErrorDetails:
    """纯文本错误快照，可安全交给 GUI 线程。"""

    summary: str
    guidance: str
    diagnostic: str

    def __str__(self) -> str:
        return f"{self.summary}\n{self.guidance}\n\n技术信息：\n{self.diagnostic}"


class WindImportError(ImportError):
    """未找到接口或加载接口失败，保留 ImportError 兼容性。"""

    def __init__(self, details: WindErrorDetails):
        self.details = details
        super().__init__(str(details))


class WindConnectionError(ConnectionError):
    """接口已导入但终端连接失败，保留 ConnectionError 兼容性。"""

    def __init__(self, details: WindErrorDetails):
        self.details = details
        super().__init__(str(details))


def wind_import_error(
    cause: Exception, *, platform: str, frozen: bool, bits: int,
    prepared_paths: list[str], configured_path: str = "", configured_source: str = "CBLENS_WINDPY_PATH",
) -> WindImportError:
    """只把 WindPy 本身缺失判为未找到，依赖/DLL 错误归为加载失败。"""
    missing = isinstance(cause, ModuleNotFoundError) and cause.name == "WindPy"
    summary = WIND_NOT_FOUND if missing else WIND_LOAD_FAILED
    if missing:
        guidance = "请在本机 Wind 终端中安装或修复 Python 接口，然后重启 CBLens。"
    else:
        guidance = "请修复本机 Wind 的 Python 接口，然后重启 CBLens。"
        if platform == "win32":
            guidance += f"\n请确认接口及其 DLL 与 CBLens 的 {bits} 位运行环境匹配。"
        elif platform == "darwin":
            guidance += "\n请确认 Wind API 与本机芯片架构匹配。"
    diagnostic = [
        f"原始错误：{type(cause).__name__}: {cause}",
        f"运行环境：{platform} / {bits} 位 / {'桌面包' if frozen else 'Python 源码'}",
        "已加入接口目录：" + ("；".join(prepared_paths) or "无"),
        "已安装但仍无法找到时，请在 CBLens 的「同步池 → Wind 接口设置」中检测或选择 WindPy.py，"
        "保存后重启 CBLens。高级配置也可使用环境变量 CBLENS_WINDPY_PATH。",
    ]
    if configured_path:
        diagnostic.append(f"当前接口设置（{configured_source}）：{configured_path}")
    if platform == "win32":
        diagnostic.append(r"路径示例（请按实际安装位置填写）：C:\Software\Wind\x64\WindPy.py")
    elif platform == "darwin":
        diagnostic.append("路径示例：/Applications/Wind API.app/Contents/python/WindPy.py")
    return WindImportError(WindErrorDetails(summary, guidance, "\n".join(diagnostic)))


def wind_connection_error(diagnostic: str) -> WindConnectionError:
    """连接失败仅说明连接状态，不推断一定是未登录或无权限。"""
    return WindConnectionError(WindErrorDetails(
        WIND_CONNECT_FAILED,
        "请确认本机 Wind 终端已打开并登录，然后重试。\n"
        "若仍失败，请检查网络及账号的 API 使用权限。",
        diagnostic,
    ))
