"""批量定价 / 关注池 Tab 共用 helper.

抽离的目的: 避免 ``batch.py`` 与 ``batch_watchlist.py`` 通过共同的 helper
互相导入造成循环依赖。所有 helper 仍是包内 (``_`` 前缀) 私有, 不对外公开。
"""
from __future__ import annotations

import math
from datetime import date, datetime
from tkinter import ttk

from ..theme import (
    BG_CARD, BG_INPUT, BORDER,
    FONT_FAMILY, FONT_MONO,
    GREEN, MAUVE, ORANGE, RED, TEXT, TEXT_DIM,
    get_color,
)


# ── Treeview 行标签颜色 (主表 + 关注池表共用) ──────────────────
_TAG_COLORS: dict[str, tuple[str, str]] = {
    "new":         MAUVE,    # 新债 (尚未交易 / 上市 ≤ 30 天)
    "underpriced": GREEN,
    "overpriced":  RED,
    "anomaly":     ORANGE,
    "failed":      TEXT_DIM,
}

# 上市后多少天内仍标记为"新债"
_NEW_BOND_DAYS = 30

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


def _coerce_date(value) -> date | None:
    """宽松解析: 接受 date / datetime / ISO 字符串 / None."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _is_new_bond(row) -> bool:
    """新债判定: 显式标记不可交易, 或可交易日/上市日在未来或近 30 天内."""
    if row.get("is_tradable") is False:
        return True
    today = date.today()
    for key in ("tradable_date", "listing_date"):
        d = _coerce_date(row.get(key))
        if d is None:
            continue
        if d > today:           # 尚未交易
            return True
        if (today - d).days <= _NEW_BOND_DAYS:  # 刚上市不久
            return True
    return False


def _resolve_row_tag(row) -> str | None:
    """决定 Treeview 行染色: 新债 > failed > 偏差异常 > underpriced/overpriced.

    新债优先级最高, 让"扫新债"加入的标的即使尚未定价 (status=None) 也能醒目标识.
    """
    if _is_new_bond(row):
        return "new"
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


# ── 表头点击排序 ─────────────────────────────────────────────
_MISSING_TOKENS = {"", "—", "-", "N/A"}


def _parse_sortable_number(value) -> float | None:
    """从单元格文本里提取数字; 失败返回 None.

    去掉常见装饰符 (+, %, ',', ¥) 后试 float; 缺失值 (—/-/N/A) 视为 None.
    """
    if value is None:
        return None
    text = str(value).strip()
    if text in _MISSING_TOKENS:
        return None
    cleaned = text.replace(",", "").replace("%", "").replace("+", "").replace("¥", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def _attach_column_sort(tree: ttk.Treeview, columns, headers) -> None:
    """给 Treeview 加表头点击排序: 数字列按数值, 其他列按文本; 缺失值始终排末尾.

    重复点同一列翻转升降序; 切换列时默认升序. 表头会附 ↑/↓ 箭头指示当前排序状态.
    """
    state = {"col": None, "asc": True}

    def sort_by(col_idx: int) -> None:
        col = columns[col_idx]
        asc = state["asc"]
        items = [(tree.set(iid, col), iid) for iid in tree.get_children("")]

        # 拆分缺失/有值; 缺失值无论升降序都落在末尾, 避免它们干扰用户判断
        present: list[tuple[str, str]] = []
        missing_iids: list[str] = []
        for raw, iid in items:
            if str(raw).strip() in _MISSING_TOKENS:
                missing_iids.append(iid)
            else:
                present.append((raw, iid))

        # 数值列识别: 至少一半 present 值能解析为 float
        parsed = [(_parse_sortable_number(v), iid) for v, iid in present]
        ok_numeric = sum(1 for n, _ in parsed if n is not None)
        is_numeric = present and ok_numeric >= max(1, len(present) // 2)

        if is_numeric:
            ok = [(n, iid) for n, iid in parsed if n is not None]
            unparsable = [iid for n, iid in parsed if n is None]
            ok.sort(key=lambda x: x[0], reverse=not asc)
            order = [iid for _, iid in ok] + unparsable + missing_iids
        else:
            present.sort(key=lambda x: str(x[0]).lower(), reverse=not asc)
            order = [iid for _, iid in present] + missing_iids

        for index, iid in enumerate(order):
            tree.move(iid, "", index)

    def update_headers(active_idx: int) -> None:
        for i, (col, header) in enumerate(zip(columns, headers)):
            arrow = ""
            if i == active_idx:
                arrow = " ↑" if state["asc"] else " ↓"
            tree.heading(col, text=f"{header}{arrow}")

    def on_click(idx: int) -> None:
        if state["col"] == idx:
            state["asc"] = not state["asc"]
        else:
            state["col"] = idx
            state["asc"] = True
        sort_by(idx)
        update_headers(idx)

    for i, col in enumerate(columns):
        tree.heading(col, command=lambda i=i: on_click(i))
