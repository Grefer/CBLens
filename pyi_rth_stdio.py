"""冻结窗口程序缺少标准输出流时，给第三方库提供可写的空输出。"""

from __future__ import annotations

import os
import sys


if getattr(sys, "frozen", False):
    # Windows console=False 将这两项置为 None。tqdm 初始化时直接 write，
    # 抛错后还会遗留显示锁；异常传回 Tk 后，进度条析构能把主线程锁死。
    # 必须早于第三方库导入，且保留已有控制台/CLI 管道，不在 worker 临时切换。
    for _name in ("stdout", "stderr"):
        if getattr(sys, _name) is None:
            setattr(sys, _name, open(os.devnull, "w", encoding="utf-8", errors="backslashreplace"))
