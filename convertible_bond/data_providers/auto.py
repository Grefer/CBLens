"""自动探测可用的在线 provider."""
from __future__ import annotations

import importlib.util

from .base import DataProvider
from .akshare import AkshareDataProvider
from .wind import WindDataProvider, discover_windpy_paths, load_windpy


def detect_available_providers(*, import_check: bool = True) -> list[str]:
    """返回当前环境可用的在线 provider 名字列表 (按优先级排序: Wind > akshare).

    默认做 import 检测，不连接 Wind、不发起网络调用。
    GUI 启动用 import_check=False：只查接口文件/模块位置，避免坏 DLL 在主线程加载。
    """
    available: list[str] = []
    try:
        if import_check:
            load_windpy()
            available.append("Wind")
        elif discover_windpy_paths():
            available.append("Wind")
    except Exception:
        pass
    try:
        if import_check:
            import akshare  # type: ignore[import-not-found]  # noqa: F401
            available.append("akshare")
        elif importlib.util.find_spec("akshare") is not None:
            available.append("akshare")
    except (ImportError, ValueError):
        pass
    return available


def auto_data_provider(prefer: str | None = None) -> DataProvider:
    """选择并实例化当前环境最合适的在线 provider.

    选择顺序: prefer (若指定且可用) → Wind → akshare.
    都不可用时抛 ImportError, 提示用户 `pip install akshare`.
    """
    available = detect_available_providers()
    if not available:
        raise ImportError(
            "未检测到任何可用的在线数据源.\n"
            "  → 推荐: pip install akshare  (免费, 无 token)\n"
            "  → 或在 Wind 终端 '插件管理' 安装 WindPy"
        )
    if prefer and prefer in available:
        choice = prefer
    else:
        choice = available[0]
    if choice == "Wind":
        return WindDataProvider()
    if choice == "akshare":
        return AkshareDataProvider()
    raise ValueError(f"未知 provider: {choice}")
