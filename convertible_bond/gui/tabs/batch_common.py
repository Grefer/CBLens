"""批量定价 / 关注池 Tab 共用 helper.

抽离的目的: 避免 ``batch.py`` 与 ``batch_watchlist.py`` 通过共同的 helper
互相导入造成循环依赖。所有 helper 仍是包内 (``_`` 前缀) 私有, 不对外公开。
"""
from __future__ import annotations

import math
import tkinter as tk
from datetime import date, datetime
from tkinter import ttk

import customtkinter as ctk

from ..theme import (
    BG_CARD, BG_INPUT, BORDER,
    FONT_FAMILY, FONT_MONO,
    MAUVE, RED, TEXT, TEXT_DIM,
    TABLE_SELECTED_BG, TABLE_SELECTED_TEXT,
    TABLE_FONT_SIZE, TABLE_ROW_HEIGHT,
    get_color,
)
from ...batch_pricing import DATA_QUALITY_RISK_TAGS, TRADABILITY_RISK_TAGS
from ...market_time import market_today


# ── Treeview 行标签颜色 (主表 + 关注池表共用) ──────────────────
#
# **整行颜色是全表最贵的通道, 只回答一件事: 这一行的话能不能听。**
# 它不表达贵/便宜, 两条独立的理由:
#
# ① 便宜度已经被**行位置**编码完了。``sort_batch_results_for_view`` 对
#    「综合机会/低估候选/转股折价」一律按 ``relative_deviation`` 升序, 而
#    「低估候选」的准入判据本身就是 ``rel < −5pp`` —— 任何架在便宜度上的行色
#    在默认落地页上都是整表同色 (实测 40/40)。换阈值救不了, 换的是同一个病。
# ② 旧的绝对阈值绿线 ``dev < −3%`` 换算到相对轴是 ``rel < −(3% + 中位)``,
#    而橙线 ``|rel| ≥ 20pp`` 优先级更高 —— ``绿 ⊂ 橙 ⟺ 当期中位 ≥ 17%``。
#    实测中位 +20.86% 时 ``underpriced`` 渲染 **0/284** (独立判据其实命中
#    侨银/万讯/宝莱 3 只, 全被橙吃掉), ``overpriced`` 占 75.4% —— 颜色通道
#    近乎常量。更糟的是「低估候选」40 行里有 2 只被染红 (长汽 rel=−15.46pp、
#    长海 −15.73pp): 页面说"这是最便宜的 40 只", 颜色说其中 2 只贵。
#
# 红绿轴因此整体退出这两张表: ``theme.GREEN`` 在本项目已有 4 种含义 (策略页
# 收益为正 / 数据源可信 / 这里的"便宜" / 用户每天看的 A 股行情软件里的"跌"),
# 而关注池「涨跌」列带动态日期表头, 是全 app 最像行情软件的一格 —— 一旦上色,
# 同一行会同时出现"红=涨(好)"与"红=贵(差)"。选边站解决不了, 只能让它退出。
#
# 两个拦截维度**不共用一个警报色**: ``blocked`` (可交易性) 是关于**这只债**的事实、
# 需要动作 (临近摘牌 = 30 天内必须卖掉); ``nodata`` (数据质量 ∪ 定价失败) 是关于
# **数据管线**的事实, 在选债页上无事可做, 该去跑 ``cb-data-doctor``。所以是
# **警报 vs 静音**而不是两个红 —— 数据源抖一下「无市价」能一次命中几百行, 共用红色
# 会让一屏红被读成"市场出事了", 而真相是"取数挂了"。
_TAG_COLORS: dict[str, tuple[str, str]] = {
    "new":     MAUVE,      # 还没进入市场 —— 所有价格类判据一律不适用
    "blocked": RED,        # 可交易性: 买不到 / 快买不到了
    "nodata":  TEXT_DIM,   # 数据质量 / 定价失败: 这行数字是坏的
}

#: 行色图例文案 —— 键集必须与 ``_TAG_COLORS`` 相等 (有守护测试比对集合)。
_TAG_LEGEND: dict[str, str] = {
    "new":     "未上市",
    "blocked": "不可交易",
    "nodata":  "数据缺失/失败",
}

#: 非颜色的第二通道。颜色是最不可靠的那条: 实测灰阶下浅色 ``new`` 与旧的
#: ``overpriced`` 亮度比 **1.00** (灰值都是 106) —— 截图、单色打印、红绿色觉
#: 缺陷 (约 8% 男性) 拿到的信息量正好是 0。字重/字形不依赖色相。
#: ``nodata`` 尤其需要: ``TEXT_DIM`` 与 ``TEXT`` 在浅色下只差 7.06:1 → 4.37:1,
#: 光靠"淡一点"分不出来。
_BOLD_TAGS = frozenset({"blocked"})
_ITALIC_TAGS = frozenset({"nodata"})

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
    """新债判定: 未上市 / 尚不可自由交易才标记, 已上市标的不再按天数染色.

    **日期是硬证据, 压过 ``is_tradable`` / ``trading_status``**。后两个是派生字段
    (公募转债的数据源根本不提供, Wind ``get_admission_status`` 对它们显式返回 None),
    在关注池里更是**加入那一刻冻结的快照** —— 一只债在"已发行未上市"时被扫进关注池,
    此后即便真的挂牌上市, ``watchlist.json`` 里那个 ``pending`` 也不会自己翻回来
    (实测中仑转债 08-24 上市 / 派克转债 08-25 上市, 条目里至今写着
    ``is_tradable: false``)。而**已经过去的上市日**是"这只债确实挂牌了"的正面证据。

    这与 AGENTS 里那条库内判据同源: "成交额 > 0 却 ``is_tradable=False`` 是自相矛盾"
    —— 硬事实与派生标记冲突时, 信硬事实。

    此前判据顺序反着来, 但被一个巧合挡住了: 关注池取价原本让全池行无条件覆盖,
    而全池行带着刚推断出来的 ``is_tradable=True``。取价改成按新鲜度择优之后, 热缓存行
    (``CACHE_FIELDS`` **故意不收**这两个派生字段) 胜出, 冻结值立刻浮上来, 表现为
    整张关注池表全部染成新债色, 原本的高估/离群区分一起消失。
    """
    today = market_today()

    # ① 日期证据
    listed = False
    for key in ("tradable_date", "listing_date"):
        d = _coerce_date(row.get(key))
        if d is None:
            continue
        if d > today:
            return True          # 可交易日还没到 —— 确实还买不到
        listed = True            # 日子已经过了 —— 已经挂牌
    if listed:
        return False

    # ② 一个日期都没有时才回落到派生字段。"已发行未上市"正是这一档:
    #    连上市日都还没公告, 除了 pending 没有别的线索。
    is_tradable = _coerce_bool(row.get("is_tradable"))
    status = str(row.get("trading_status") or "").strip().lower()
    if is_tradable is True or status in {"tradable", "private_tradable"}:
        return False
    if is_tradable is False or status in {"pending", "private_pending"}:
        return True
    return False


def _resolve_row_tag(row) -> str | None:
    """决定 Treeview 行染色: 新债 > 不可交易 > 数据坏了 > 无色.

    三档**都与价格无关** —— 理由见 ``_TAG_COLORS`` 上方那段。

    优先级逐条都有理由:

    - **新债最高**: 语义是"这只债还没进入市场, 价格类判据一律不适用"。而且未上市
      新债天然带着「无市价」「无偏差」(数据质量维), 没有这道优先级它们会被误染成
      ``nodata`` —— 但"还没挂牌所以没有价"不是数据坏了, 是天然状态。
    - **可交易性压过数据质量**: 一只「临近摘牌」且当天恰好取不到价的债, 该看见的
      是"30 天内必须卖掉", 不是"数据缺了" (实测今天 0 行同时命中, 但优先级必须
      是显式的)。

    判据读 ``batch_pricing`` 的两个维度常量, **不在这里另抄一份清单**: 它们的并集
    还驱动 ``view_exclusion_reason`` 与 ``_review_bucket``, 抄第二份之后两边的分叉
    是静默的 (与"GUI 自带一份只覆盖 14/18 的事件配色表"同源)。模型适用性 / 标的
    风险两个维度**刻意都不在内** —— 它们是永久属性, 查完还是那样 (实测模型适用性
    在 72% 的债上都亮), 收进来行色就变回 79% 的垃圾桶。

    "无色"是**有含义的一档**: 没有否决理由。默认落地视图「低估候选」里三档全为 0
    (该视图本身就排除了拦截标签与低置信), 所以一页全无色是设计意图 —— 必须配图例
    把这句话说出来, 否则它和"配色坏了"长得一模一样。

    没有 ``status`` 键的行 (关注池从没算过的那一档) 不染色: "从没算过"不是"这行
    数字是坏的", 那两档的区分由「数据」列的五档文案承载。
    """
    if _is_new_bond(row):
        return "new"
    risk_tags = set(row.get("risk_tags") or [])
    if risk_tags & TRADABILITY_RISK_TAGS:
        return "blocked"
    status = row.get("status")
    if status is not None and str(status) != "ok":
        return "nodata"
    if risk_tags & DATA_QUALITY_RISK_TAGS:
        return "nodata"
    return None


def _apply_tag_colors(tree: ttk.Treeview) -> None:
    """将 ``_TAG_COLORS`` 中的标签颜色写入 *tree*.

    加粗档的字号要跟着响应式字号走: ``_apply_responsive_tree_font`` 改的是全局
    ``"Treeview"`` style 的 font, 而 tag 上的 font 会盖住它 —— 写死字号的话,
    窗口一拉宽 blocked 行就比周围小一号。所以两边都要能触发重写。
    """
    size = getattr(tree, "_responsive_font_size", None) or TABLE_FONT_SIZE
    for tag, color in _TAG_COLORS.items():
        style = ("bold" if tag in _BOLD_TAGS
                 else "italic" if tag in _ITALIC_TAGS else None)
        if style:
            tree.tag_configure(tag, foreground=get_color(color),
                               font=(FONT_MONO, size, style))
        else:
            tree.tag_configure(tag, foreground=get_color(color))


def row_colour_legend() -> str:
    """行色图例 —— 两页共用一份文案.

    与 ``WATCH_REFRESH_LABEL`` 那条同构: 用户看得见的名字必须有单一事实源, 否则
    改了 tag 之后图例里会留着一个**过期**的档位名, 用户对着表找一个不存在的颜色。
    """
    parts = " · ".join(f"{label}" for label in _TAG_LEGEND.values())
    return f"行色: {parts} · 无色 = 无否决理由"


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
        background=[("selected", get_color(TABLE_SELECTED_BG))],
        foreground=[("selected", get_color(TABLE_SELECTED_TEXT))],
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
    """主题切换后刷新 Treeview 样式 + 给所有已注册树重新染色.

    **每棵树各自兜住 ``TclError``, 并把死掉的那个从注册表里摘掉。**
    ``getattr(app, attr, None) is not None`` 拦不住已 ``destroy`` 的控件 —— 它还是
    个对象, 而 Tk 8.6.15 实测在 ``tag_configure`` 上抛
    ``TclError: invalid command name ".!frame.!treeview"``。触发链今天就成立:
    默认落地「低估候选」(40 行) → 切「下修优势」或「转股折价」(**实测都是 0 行**)
    → 切主题 (app.py) 或跨响应式档位。

    真正难查的是后果而不是异常本身: 它从 ``for attr in _TREE_ATTRS`` 抛出会**中断
    整轮循环**, 而 ``_TREE_ATTRS`` 是 ``set``、遍历顺序随 ``PYTHONHASHSEED`` 随机
    —— 用户看到的不是崩溃, 是"切了一下主题, 有些表变了色有些没变, 而且每次开机变的
    不是同一批"。
    """
    _configure_tree_style()
    dead: list[str] = []
    for attr in tuple(_TREE_ATTRS):
        tree = getattr(app, attr, None)
        if tree is None:
            continue
        try:
            _apply_tag_colors(tree)
            tree._responsive_font_size = None  # type: ignore[attr-defined]
            _apply_responsive_tree_font(tree)
        except tk.TclError:
            dead.append(attr)
    for attr in dead:
        _TREE_ATTRS.discard(attr)


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


def _create_table_section(parent, *, row, title, with_summary=False):
    section = ctk.CTkFrame(parent, fg_color=BG_CARD, corner_radius=12)
    section.grid(row=row, column=0, sticky="nsew", pady=(0, 8) if row == 0 else (0, 0))
    section.grid_columnconfigure(0, weight=1)

    header = ctk.CTkFrame(section, fg_color="transparent")
    header.grid(row=0, column=0, sticky="ew", padx=12, pady=(8, 2))
    header.grid_columnconfigure(1, weight=1)
    ctk.CTkLabel(
        header, text=title,
        font=(FONT_FAMILY, 13, "bold"), text_color=TEXT,
    ).grid(row=0, column=0, sticky="w")

    summary_var = None
    if with_summary:
        summary_var = ctk.StringVar(value="")
        ctk.CTkLabel(
            header, textvariable=summary_var,
            font=(FONT_FAMILY, 11), text_color=TEXT_DIM, anchor="e",
        ).grid(row=0, column=1, sticky="e", padx=(12, 0))

    # 行色图例 —— 两页共用一份文案, 挂在共用的表区里而不是各页各写一份。
    # 这一维不进 CSV、不能排序, 且默认落地视图里往往一行都不染 (「低估候选」的
    # 判据本身就排除了拦截标签) —— 没有这句话, "一页全无色"和"配色坏了"长得
    # 一模一样。与事件横幅空态那条同源: 要的是显式空态文案。
    ctk.CTkLabel(
        header, text=row_colour_legend(),
        font=(FONT_FAMILY, 10), text_color=TEXT_DIM, anchor="w",
    ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 0))

    body_row = 1
    section.grid_rowconfigure(body_row, weight=1)
    body = ctk.CTkFrame(section, fg_color="transparent")
    body.grid(row=body_row, column=0, sticky="nsew")
    body.grid_columnconfigure(0, weight=1)
    body.grid_rowconfigure(0, weight=1)
    if with_summary:
        return body, summary_var
    return body
