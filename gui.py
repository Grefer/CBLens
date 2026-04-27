#!/usr/bin/env python3
"""
根目录 GUI 兼容入口.

旧版完整实现已拆分到 gui/ 包中.
保留本文件仅为兼容 `python gui.py` 这类历史启动方式.
"""

from convertible_bond.gui.app import CBPricerApp, main

__all__ = ["CBPricerApp", "main"]


if __name__ == "__main__":
    main()
