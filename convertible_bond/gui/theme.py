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
_IS_WIN = sys.platform == "win32"
# Windows: 用 Microsoft YaHei UI 而不是 Segoe UI — 后者中英混排时会回退到
# SimSun (bitmap, 发虚); YaHei UI 是 Windows 自带的中英两用 GUI 字体, 高 DPI
# 下不模糊. mono 字体 Cascadia Mono 是 Windows 11 自带, 数字列对齐;
# Windows 10 老版本可能没有, Tk 会自动回退到 Consolas (universal, 也清晰).
if _IS_MAC:
    # macOS 系统字体：使用 PingFang SC 保证中英文显示无缝且不回退模糊
    # Menlo 是所有 macOS 内置的标准等宽字体
    FONT_FAMILY = "PingFang SC"
    FONT_MONO = "Menlo"
elif _IS_WIN:
    FONT_FAMILY = "Microsoft YaHei UI"
    FONT_MONO = "Cascadia Mono"
else:
    FONT_FAMILY = "Segoe UI"
    FONT_MONO = "DejaVu Sans Mono"

# 表格 (ttk.Treeview) 字号: Windows 上 Cascadia Mono 字重比 SF Mono 轻, 11pt
# 在 4K 屏上偏细, +1 让数据列易读; 行高同步上调保持留白.
if _IS_WIN:
    TABLE_FONT_SIZE = 12
    TABLE_ROW_HEIGHT = 30
else:
    TABLE_FONT_SIZE = 11
    TABLE_ROW_HEIGHT = 26

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

_WIN_EMOJI_FALLBACK = {
    "📦 ": "", "⚡ ": "", "📈 ": "", "🔥 ": "", 
    "🌐 ": "", "📥 ": "", "🆕 ": "", "⭐ ": "", "📝 ": "", 
    "✅ ": "", "❌ ": "", "⚠ ": "", "🗑 ": "",
    "📦": "", "⚡": "", "📈": "", "🔥": "", 
    "🔄": "刷新", "💾": "保存", "📂": "载入",
}

def E(text: str) -> str:
    """跨端 Emoji 处理器。
    Tkinter 8.6 在 Windows 无法渲染彩色 Emoji（降级为丑陋的黑白线框）。
    此函数在 Windows 下自动剥离常见 Emoji，或将独立 Emoji 按钮替换为纯文本；Mac 下则原样保留。
    """
    if not _IS_WIN:
        return text
    for emoji, fallback in _WIN_EMOJI_FALLBACK.items():
        if text == emoji.strip():  # 独立按钮（如 "🔄"）直接替换
            return fallback
        text = text.replace(emoji, fallback)
    return text.strip()
