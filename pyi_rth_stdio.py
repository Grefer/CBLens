"""冻结窗口程序缺少标准输出流时，给第三方库提供可写的空输出。"""

from __future__ import annotations

import os
import sys


def _capture_startup_stdio():
    """在补流前留下只读证据，供脱离控制台的发布烟测验证启动条件。"""
    snapshot = {"python_none": {
        name: getattr(sys, name) is None for name in ("stdin", "stdout", "stderr")
    }}
    if sys.platform == "win32":
        import ctypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetStdHandle.argtypes = [ctypes.c_ulong]
        kernel.GetStdHandle.restype = ctypes.c_void_p
        kernel.GetFileType.argtypes = [ctypes.c_void_p]
        kernel.GetFileType.restype = ctypes.c_ulong
        invalid = ctypes.c_void_p(-1).value
        handles = {}
        for name, identifier in (("stdin", -10), ("stdout", -11), ("stderr", -12)):
            handle = kernel.GetStdHandle(identifier & 0xFFFFFFFF)
            handles[name] = {
                "handle": handle or 0,
                "kind": "null" if not handle else "invalid" if handle == invalid else "valid",
                "file_type": kernel.GetFileType(handle),
            }
        snapshot["windows_handles"] = handles
    return snapshot


if getattr(sys, "frozen", False):
    if not hasattr(sys, "_cblens_stdio_startup"):
        try:
            sys._cblens_stdio_startup = _capture_startup_stdio()
        except Exception as _exc:
            # 补流本身不能被诊断信息阻挡；发布烟测会拒绝没有完整证据的结果。
            sys._cblens_stdio_startup = {"capture_error": str(_exc)}
    # Windows console=False 将这两项置为 None。tqdm 初始化时直接 write，
    # 抛错后还会遗留显示锁；异常传回 Tk 后，进度条析构能把主线程锁死。
    # 必须早于第三方库导入，且保留已有控制台/CLI 管道，不在 worker 临时切换。
    for _name in ("stdout", "stderr"):
        if getattr(sys, _name) is None:
            setattr(sys, _name, open(os.devnull, "w", encoding="utf-8", errors="backslashreplace"))
