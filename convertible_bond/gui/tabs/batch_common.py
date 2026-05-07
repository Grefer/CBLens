"""批量定价 / 关注池 Tab 共用 helper.

抽离的目的: 避免 ``batch.py`` 与 ``batch_watchlist.py`` 通过共同的 helper
互相导入造成循环依赖。所有 helper 仍是包内 (``_`` 前缀) 私有, 不对外公开。
"""
from __future__ import annotations

import math
from tkinter import ttk

from ..theme import (
    BG_CARD, BG_INPUT, BORDER,
    FONT_FAMILY, FONT_MONO,
    GREEN, ORANGE, RED, TEXT, TEXT_DIM,
    get_color,
)


# ── Treeview 行标签颜色 (主表 + 关注池表共用) ──────────────────
_TAG_COLORS: dict[str, tuple[str, str]] = {
    "underpriced": GREEN,
    "overpriced":  RED,
    "anomaly":     ORANGE,
    "failed":      TEXT_DIM,
}

# 已注册到 app 的 Treeview 实例属性名, 主题切换时统一刷新.
# 模块级集合: 假定单进程单 GUI 实例; 多实例场景下旧属性名会残留,
# 但 ``refresh_theme`` 通过 ``getattr(app, attr, None)`` 兜底, 无害.
_TREE_ATTRS: set[str] = set()


def _is_finite(value) -> bool:
    if value is None:
        return False
    try:
        f = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(f)


def _format_tags(tags) -> str:
    if not tags:
        return ""
    if isinstance(tags, str):
        return tags
    return " / ".join(str(tag) for tag in tags if tag)


def _median(values) -> float | None:
    finite = [float(v) for v in values if _is_finite(v)]
    if not finite:
        return None
    finite.sort()
    n = len(finite)
    if n % 2:
        return finite[n // 2]
    return (finite[n // 2 - 1] + finite[n // 2]) / 2.0


def _resolve_row_tag(row) -> str | None:
    """决定 Treeview 行染色: 偏差异常 > failed > underpriced/overpriced."""
    status = row.get("status")
    if status and status != "ok":
        return "failed"
    if status != "ok":
        return None  # 关注池未定价行 (无 status), 不染色
    risk_tags = set(row.get("risk_tags") or [])
    if "偏差异常" in risk_tags:
        return "anomaly"
    dev = row.get("deviation")
    if _is_finite(dev):
        d = float(dev)
        if d < -0.03:
            return "underpriced"
        if d > 0.05:
            return "overpriced"
    return None


def _apply_tag_colors(tree: ttk.Treeview) -> None:
    """将 ``_TAG_COLORS`` 中的标签颜色写入 *tree*."""
    for tag, color in _TAG_COLORS.items():
        tree.tag_configure(tag, foreground=get_color(color))


def _configure_tree_style() -> None:
    """配置 ttk Treeview 全局样式 (idempotent).

    设置 ``clam`` 主题并按当前 appearance mode 写入背景/边框/文字颜色.
    初始渲染与主题切换均调用; ``style.theme_use`` 在已设置时为 no-op.
    """
    style = ttk.Style()
    style.theme_use("clam")
    style.configure(
        "Treeview",
        background=get_color(BG_CARD),
        fieldbackground=get_color(BG_CARD),
        foreground=get_color(TEXT),
        rowheight=26,
        borderwidth=0,
        font=(FONT_MONO, 11),
    )
    style.configure(
        "Treeview.Heading",
        background=get_color(BORDER),
        foreground=get_color(TEXT),
        borderwidth=0,
        font=(FONT_FAMILY, 11, "bold"),
    )
    style.map(
        "Treeview",
        background=[("selected", get_color(BG_INPUT))],
        foreground=[("selected", get_color(TEXT))],
    )


def refresh_theme(app) -> None:
    """主题切换后刷新 Treeview 样式 + 给所有已注册树重新染色."""
    _configure_tree_style()
    for attr in _TREE_ATTRS:
        tree = getattr(app, attr, None)
        if tree is not None:
            _apply_tag_colors(tree)
