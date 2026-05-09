"""
GUI 主题常量与工具函数.

Catppuccin 风格配色 (Latte/Mocha), 字体定义, 共享常量.
"""
import sys
import customtkinter as ctk

# Catppuccin 风格高级配色: (浅色 Latte, 深色 Mocha)
BG_APP    = ("#dce0e8", "#11111b")     # Crust
BG_CARD   = ("#eff1f5", "#1e1e2e")     # Base
BG_INPUT  = ("#e6e9ef", "#181825")     # Mantle
BORDER    = ("#ccd0da", "#313244")     # Surface0
TEXT      = ("#4c4f69", "#cdd6f4")     # Text
TEXT_DIM  = ("#6c6f85", "#a6adc8")     # Subtext0
ACCENT    = ("#1e66f5", "#89b4fa")     # Blue
GREEN     = ("#40a02b", "#a6e3a1")     # Green
RED       = ("#d20f39", "#f38ba8")     # Red
ORANGE    = ("#fe640b", "#fab387")     # Peach
MAUVE     = ("#8839ef", "#cba6f7")     # Mauve (用于新债等需要醒目但不告警的标识)

BTN_CTRL  = ("#ccd0da", "#313244")
BTN_HOVER = ("#bcc0cc", "#45475a")
ACCENT_HOVER = ("#7287fd", "#74c7ec")

_IS_MAC = sys.platform == "darwin"
FONT_FAMILY = "SF Pro Display" if _IS_MAC else "Segoe UI"
FONT_MONO = "SF Mono" if _IS_MAC else "Cascadia Mono"

# 历史波动率窗口选项 (交易日数)
VOL_WINDOW_MAP = {"1M": 21, "2M": 42, "3M": 63, "6M": 126, "1Y": 252}
VOL_WINDOW_DEFAULT = "1M"

# 同评级信用利差经验值 (%)
CREDIT_SPREAD_TABLE = {
    "AAA": 0.5, "AA+": 1.5, "AA": 2.5,
    "AA-": 4.0, "A+": 6.0, "A": 8.0,
}


def get_color(color_val):
    """解析当前模式下的颜色值，主要用于 Matplotlib"""
    if isinstance(color_val, tuple):
        return color_val[1] if ctk.get_appearance_mode() == "Dark" else color_val[0]
    return color_val
