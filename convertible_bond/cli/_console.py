"""CLI 输出编码的轻量入口工具; 导入模块时不改变调用方的标准流。"""
from __future__ import annotations

import sys


def configure_utf8_stdio() -> None:
    """中文、emoji 和错误统一输出 UTF-8, 供终端与 GUI 的子进程管道解码。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="backslashreplace")
