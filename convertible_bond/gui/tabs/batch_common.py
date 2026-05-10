"""批量定价 / 关注池 Tab 共用 helper.

抽离的目的: 避免 ``batch.py`` 与 ``batch_watchlist.py`` 通过共同的 helper
互相导入造成循环依赖。所有 helper 仍是包内 (``_`` 前缀) 私有, 不对外公开。
"""
from __future__ import annotations

import math
import tkinter as tk
from datetime import date, datetime
from tkinter import ttk

from ..theme import (
    BG_CARD, BG_INPUT, BORDER,
    FONT_FAMILY, FONT_MONO,
    GREEN, MAUVE, ORANGE, RED, TEXT, TEXT_DIM,
    TABLE_FONT_SIZE, TABLE_ROW_HEIGHT,
    get_color,
)


# ── Treeview 行标签颜色 (主表 + 关注池表共用) ──────────────────
_TAG_COLORS: dict[str, tuple[str, str]] = {
    "new":         MAUVE,    # 未上市 / 尚不可自由交易的新债
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


def _coerce_bool(value) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "y"}:
            return True
        if normalized in {"false", "0", "no", "n"}:
            return False
    return None


def _is_new_bond(row) -> bool:
    """新债判定: 未上市 / 尚不可自由交易才标记, 已上市标的不再按天数染色."""
    is_tradable = _coerce_bool(row.get("is_tradable"))
    status = str(row.get("trading_status") or "").strip().lower()
    if is_tradable is True or status in {"tradable", "private_tradable"}:
        return False
    if is_tradable is False or status in {"pending", "private_pending"}:
        return True

    today = date.today()
    for key in ("tradable_date", "listing_date"):
        d = _coerce_date(row.get(key))
        if d is None:
            continue
        if d > today:
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
        rowheight=TABLE_ROW_HEIGHT,
        borderwidth=0,
        font=(FONT_MONO, TABLE_FONT_SIZE),
    )
    style.configure(
        "Treeview.Heading",
        background=get_color(BORDER),
        foreground=get_color(TEXT),
        borderwidth=0,
        font=(FONT_FAMILY, TABLE_FONT_SIZE, "bold"),
    )
    style.map(
        "Treeview",
        background=[("selected", get_color(BG_INPUT))],
        foreground=[("selected", get_color(TEXT))],
    )


def _responsive_table_font_size(width: int) -> int:
    """根据表格可视宽度选择字号; 只做小幅分档, 保持数据表密度."""
    size = TABLE_FONT_SIZE
    if width < 1080:
        size -= 1
    elif width >= 2600:
        size += 3
    elif width >= 2200:
        size += 2
    elif width >= 1800:
        size += 1
    return max(10, min(TABLE_FONT_SIZE + 3, size))


def _apply_responsive_tree_font(tree: ttk.Treeview) -> None:
    width = tree.winfo_width()
    if width <= 1:
        return
    font_size = _responsive_table_font_size(width)
    if getattr(tree, "_responsive_font_size", None) == font_size:
        return
    tree._responsive_font_size = font_size  # type: ignore[attr-defined]
    row_height = max(22, TABLE_ROW_HEIGHT + (font_size - TABLE_FONT_SIZE) * 3)
    style = ttk.Style()
    style.configure(
        "Treeview",
        rowheight=row_height,
        font=(FONT_MONO, font_size),
    )
    style.configure(
        "Treeview.Heading",
        font=(FONT_FAMILY, font_size, "bold"),
    )


def _configure_responsive_columns(
    tree: ttk.Treeview,
    columns,
    headers,
    widths,
    stretch_weights: dict[str, float] | None = None,
) -> None:
    """按列权重分配窗口变宽后的剩余宽度, 避免只拉伸末列."""
    base_widths = [int(w) for w in widths]
    min_widths = [max(40, int(w) // 2) for w in base_widths]
    weights = [
        max(0.0, float((stretch_weights or {}).get(header, 1.0)))
        for header in headers
    ]

    for column, header, width, min_width in zip(columns, headers, base_widths, min_widths):
        tree.heading(column, text=header)
        tree.column(column, width=width, minwidth=min_width, stretch=False, anchor="w")

    def _apply_widths(_event=None) -> None:
        available = tree.winfo_width()
        if available <= 1:
            return
        extra = max(0, available - sum(base_widths) - 2)
        weighted = [(idx, weight) for idx, weight in enumerate(weights) if weight > 0]
        additions = [0] * len(base_widths)
        if extra and weighted:
            total_weight = sum(weight for _, weight in weighted)
            remaining = extra
            for pos, (idx, weight) in enumerate(weighted):
                if pos == len(weighted) - 1:
                    add = remaining
                else:
                    add = int(extra * weight / total_weight)
                    remaining -= add
                additions[idx] = add
        for column, width, add in zip(columns, base_widths, additions):
            tree.column(column, width=width + add)
        _apply_responsive_tree_font(tree)

    tree.bind("<Configure>", _apply_widths, add="+")
    tree.after_idle(_apply_widths)


def refresh_theme(app) -> None:
    """主题切换后刷新 Treeview 样式 + 给所有已注册树重新染色."""
    _configure_tree_style()
    for attr in _TREE_ATTRS:
        tree = getattr(app, attr, None)
        if tree is not None:
            _apply_tag_colors(tree)
            tree._responsive_font_size = None  # type: ignore[attr-defined]
            _apply_responsive_tree_font(tree)


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


def _attach_cell_tooltip(
    tree: ttk.Treeview,
    columns,
    headers,
    *,
    tooltip_headers: set[str] | None = None,
    delay_ms: int = 300,
) -> None:
    """给 Treeview 指定列加悬浮完整文本提示.

    主要用于"标签"、"复核建议"这类长文本列。tooltip 内容直接取当前单元格
    display value, 因此表头排序后也能自然跟随行移动。
    """
    targets = set(tooltip_headers or headers)
    state = {"tip": None, "after": None, "cell": None}

    def _cancel_after() -> None:
        after_id = state.get("after")
        if after_id is not None:
            try:
                tree.after_cancel(after_id)
            except Exception:
                pass
            state["after"] = None

    def _hide(_event=None) -> None:
        _cancel_after()
        tip = state.get("tip")
        if tip is not None:
            try:
                tip.destroy()
            except Exception:
                pass
            state["tip"] = None
        state["cell"] = None

    def _show(text: str, x_root: int, y_root: int) -> None:
        tip = tk.Toplevel(tree)
        tip.wm_overrideredirect(True)
        try:
            tip.attributes("-topmost", True)
        except tk.TclError:
            pass
        label = tk.Label(
            tip,
            text=text,
            justify="left",
            wraplength=460,
            background=get_color(BG_INPUT),
            foreground=get_color(TEXT),
            relief="solid",
            borderwidth=1,
            padx=8,
            pady=6,
            font=(FONT_FAMILY, 12),
        )
        label.pack()
        tip.wm_geometry(f"+{x_root + 12}+{y_root + 16}")
        state["tip"] = tip

    def _motion(event) -> None:
        row_id = tree.identify_row(event.y)
        col_id = tree.identify_column(event.x)
        if not row_id or not col_id:
            _hide()
            return
        try:
            col_idx = int(col_id.lstrip("#")) - 1
        except ValueError:
            _hide()
            return
        if col_idx < 0 or col_idx >= len(columns):
            _hide()
            return
        header = headers[col_idx]
        if header not in targets:
            _hide()
            return
        value = str(tree.set(row_id, columns[col_idx]) or "").strip()
        if not value or value in {"—", "-"}:
            _hide()
            return
        cell = (row_id, col_idx, value)
        if state.get("cell") == cell:
            tip = state.get("tip")
            if tip is not None:
                tip.wm_geometry(f"+{event.x_root + 12}+{event.y_root + 16}")
            return
        _hide()
        state["cell"] = cell
        state["after"] = tree.after(
            delay_ms,
            lambda text=value, x=event.x_root, y=event.y_root: _show(text, x, y),
        )

    tree.bind("<Motion>", _motion, add="+")
    tree.bind("<Leave>", _hide, add="+")
    tree.bind("<ButtonPress>", _hide, add="+")
    tree.bind("<MouseWheel>", _hide, add="+")
