"""
gui 包入口 — 向后兼容 `from gui import main` 及 `cb-gui` entry point.
"""
from .app import CBPricerApp, main  # noqa: F401

__all__ = ["CBPricerApp", "main"]
