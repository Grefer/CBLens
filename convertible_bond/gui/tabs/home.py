"""⭐ 关注池主页 — GUI 的默认落地页.

职责边界: 这个模块只**构建控件**。关注池的数据合并、取价、渲染、右键菜单全部留在
:mod:`batch_watchlist` (它同时被批量页的「⭐ 加入关注池」按钮复用), 本模块通过
``batch_watchlist.refresh_home(app)`` 驱动。方向是 ``home → batch_watchlist``,
不成环。

**这个文件刻意不用 ``from ..theme import *``**。``pyproject.toml`` 给
``tabs/batch.py`` 与 ``tabs/batch_watchlist.py`` 登记了 ``F403/F405`` 豁免, 而
star import 会把本该报 F821 (未定义名) 的错降级成 F405 被豁免吃掉 —— 实测那两个
文件 ``ruff check --isolated`` 有 84 处告警全被吸收。GUI 在测试环境跑不起来,
F821 是运行期 NameError 的唯一静态防线, 新页不要把它关掉。

页面结构 **与批量页同构** (那一页是: 控制卡片 → 状态行 → 结果区):
    row0  控制卡片 (BG_CARD): 标题 + 副标题 + 右侧摘要 / 按钮行
    row1  状态行 (``v_watchlist_status``; 批量页有自己的 ``v_batch_status``)
    row2  事件横幅 (默认隐藏, 单击展开)
    row3  结果区 (关注池表)

标题**必须在卡片里**, 与按钮同属一张 BG_CARD —— 此前标题裸在透明 frame 上、卡片只
包按钮, 于是两页并排看时关注页像少了一层。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import customtkinter as ctk

from ..theme import (
    ACCENT, ACCENT_HOVER,
    BG_CARD,
    BTN_CTRL, BTN_HOVER,
    FONT_FAMILY,
    ORANGE,
    TEXT,
)
from ..widgets import Tooltip
from .batch_common import _create_table_section
from .batch_watchlist import (
    WATCH_REFRESH_LABEL,
    _auto_add_upcoming_to_watchlist,
    _refresh_watchlist_pricing,
    _refresh_watchlist_with_upcoming,
    _show_events_banner_full,
    load_price_cache_into,
    refresh_home,
)

if TYPE_CHECKING:                       # pragma: no cover
    from ..app import CBPricerApp


def build(app, tab) -> None:
    """在 tab frame 上构建关注池主页.

    **必须排在 ``batch_tab.build`` 之前**: 批量页的「⭐ 加入关注池」与
    ``_render_batch_views`` 都要求 ``batch_watchlist_table_frame`` 已经存在 ——
    ``_render_watchlist_table`` 拿不到那个 frame 时是 ``return`` 而不是报错,
    顺序反了只会表现为"主页首屏是空的", 没有任何异常。
    """
    tab.grid_columnconfigure(0, weight=1)
    tab.grid_rowconfigure(0, weight=0)   # 控制卡片 (标题 + 按钮)
    tab.grid_rowconfigure(1, weight=0)   # 状态行
    tab.grid_rowconfigure(2, weight=0)   # 事件横幅 (默认隐藏)
    tab.grid_rowconfigure(3, weight=1)   # 结果区

    _build_control_card(app, tab)
    _build_status(app, tab)
    _build_events_banner(app, tab)
    _build_table(app, tab)

    # 首屏: 先把盘上的数据画出来, 再扫新债。顺序反过来会让首屏闪一次空表 ——
    # 而这是默认落地页, 那一闪就是用户对这个工具的第一印象。
    load_price_cache_into(app)
    _auto_add_upcoming_to_watchlist(app, silent=True)
    refresh_home(app)


# ── 分区 ────────────────────────────────────────────────────────

def _build_control_card(app, tab) -> None:
    """控制卡片 —— 与批量页的 ``ctrl`` 同构: 上排标题, 下排按钮, 同在一张 BG_CARD 里."""
    ctrl = ctk.CTkFrame(tab, fg_color=BG_CARD, corner_radius=12)
    ctrl.grid(row=0, column=0, sticky="ew", pady=(6, 8), padx=16)
    ctrl.grid_columnconfigure(0, weight=1)

    # ── 上排: 标题 + 副标题 + 右侧摘要 ──
    # 卡片里**只放标题和按钮**。副标题删掉了 (那句"开页即有上次落盘的价 · 要最新点
    # 「⚡ 关注池重算」"): 按钮自己已经说清要点什么, 而"开页即有价"是这一页的**行为**,
    # 用一次就知道, 不必每次都读一遍。完整说明留在标题的 Tooltip 里, 要看才看。
    # 摘要 (持仓/偏差中位/平均评级/估值日) 在状态行右侧 —— 那是逐日变化的**数据**,
    # 不是这一页"是干什么的"。
    ch = ctk.CTkFrame(ctrl, fg_color="transparent")
    ch.grid(row=0, column=0, sticky="ew", padx=16, pady=(10, 4))

    title = ctk.CTkLabel(ch, text="⭐ 我的关注池",
                         font=(FONT_FAMILY, 16, "bold"), text_color=TEXT)
    title.pack(side="left")
    # 与 COLUMN_HELP 同一条写法约定: 一句话说清怎么用, 不写实现细节 (三级兜底取价、
    # 缓存文件名那些属于代码注释)。逐列口径悬停表头看, 这里只留一条最容易读反的 ——
    # `+54.84` 有两种正好相反的读法, 而它同时出现在两列上。
    Tooltip(title,
            f"表里是上次算好的价, 要最新值点「{WATCH_REFRESH_LABEL}」。\n"
            "符号: 偏差(%) / 相对偏差(pp) 正 = 市价贵于模型价; 其余各列悬停表头看。\n"
            "⚠ 复核标记, 不是收益预测。")

    # ── 下排: 按钮 ──
    cc = ctk.CTkFrame(ctrl, fg_color="transparent")
    cc.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 10))

    # 文案走 WATCH_REFRESH_LABEL 单一事实源 —— 状态栏那句"点「…」再试"引的是同一个常量,
    # 免得按钮改名后消息还指着一个页面上不存在的名字。属性名保持 btn_batch_refresh_watch ——
    # batch_watchlist 的 worker 会跨模块改它的 enabled 状态 (走 _set_watch_button_state,
    # 找不到就静默跳过, 但没必要给自己制造那种缺口)。
    app.btn_batch_refresh_watch = ctk.CTkButton(
        cc, text=WATCH_REFRESH_LABEL, command=lambda: _refresh_watchlist_pricing(app),
        fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color=("#ffffff", "#11111b"),
        font=(FONT_FAMILY, 13, "bold"), width=110, height=32, corner_radius=6)
    app.btn_batch_refresh_watch.pack(side="left")

    app.btn_batch_upcoming = ctk.CTkButton(
        cc, text="🆕 扫新债", command=lambda: _refresh_watchlist_with_upcoming(app),
        fg_color=BTN_CTRL, hover_color=BTN_HOVER, text_color=TEXT,
        font=(FONT_FAMILY, 12), width=90, height=32, corner_radius=6)
    app.btn_batch_upcoming.pack(side="left", padx=(8, 0))

    # 行情源**只在顶栏那一个下拉里选**, 页内不再各摆一个 —— 三个下拉控三条链路时,
    # "我明明选了 akshare 怎么还在连 Wind"是找不出原因的那类问题。


def _build_status(app, tab) -> None:
    """状态行 —— **与批量页同格式**: 一行左对齐加粗, 由 ``v_watchlist_status`` 驱动.

    这一行时分复用: 空闲时是关注池摘要 (``_refresh_watchlist_summary`` 写),
    动作进行/结束时被消息覆盖 (⚡ 已刷新 N 只), 下一次 ``refresh_home`` 摘要再回来。
    与批量页的 ``v_batch_status`` 逐字同构。

    曾经拆成"左消息 + 右摘要"两半, 而左半在空闲时是空的 —— 看着像右边飘着一段孤字。
    也曾与批量页**共用**一个变量, 那时批量页的视图摘要会常驻在这里, 主页永久挂着一句
    「✅ 低估候选: 展示 41/283 只」说的是另一页的表 (见 ``app._build_vars``)。
    """
    ctk.CTkLabel(tab, textvariable=app.v_watchlist_status,
                 font=(FONT_FAMILY, 13, "bold"), text_color=TEXT, anchor="w").grid(
                     row=1, column=0, sticky="ew", padx=24, pady=(2, 8))


def _build_events_banner(app, tab) -> None:
    app.v_batch_events_banner = ctk.StringVar(value="")
    app._batch_events_banner_full = []
    app.lbl_batch_events_banner = ctk.CTkLabel(
        tab, textvariable=app.v_batch_events_banner,
        font=(FONT_FAMILY, 12, "bold"), text_color=ORANGE,
        fg_color=BG_CARD, corner_radius=12,
        padx=12, pady=8,
        anchor="w", justify="left", wraplength=1080, cursor="hand2")
    app.lbl_batch_events_banner.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 8))
    app.lbl_batch_events_banner.grid_remove()
    app.lbl_batch_events_banner.bind(
        "<Button-1>", lambda _e: _show_events_banner_full(app))


def _build_table(app, tab) -> None:
    holder = ctk.CTkFrame(tab, fg_color="transparent")
    holder.grid(row=3, column=0, sticky="nsew", padx=16, pady=(0, 6))
    holder.grid_columnconfigure(0, weight=1)
    holder.grid_rowconfigure(0, weight=1)
    app.batch_watchlist_table_frame = _create_table_section(
        holder, row=0, title="持仓/候选 (右键删除 · 双击载入定价页)")


def refresh_theme(app: "CBPricerApp") -> None:
    """主题切换后的刷新入口.

    实际染色由 ``batch_common.refresh_theme`` 遍历模块级 ``_TREE_ATTRS`` 完成,
    关注池表在 ``_render_watchlist_table`` 里自注册, 所以这里不需要额外动作 ——
    留这个函数是为了让 ``app.py`` 对每个页签的调用形态一致。
    """
    return None
