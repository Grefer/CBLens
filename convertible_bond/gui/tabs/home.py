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

页面结构 (单列纵向):
    row0  标题 + 关注池摘要
    row1  工具条 (⚡ 今日刷新 / 🆕 扫新债 / 行情源)
    row2  状态行 (与批量页共用 ``v_batch_status``, 两页各挂一个 Label)
    row3  事件横幅 (默认隐藏, 单击展开)
    row4  关注池表
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import customtkinter as ctk

from ..theme import (
    ACCENT, ACCENT_HOVER,
    BG_CARD, BG_INPUT,
    BTN_CTRL, BTN_HOVER,
    FONT_FAMILY,
    ORANGE,
    TEXT, TEXT_DIM,
)
from ..widgets import Tooltip
from .batch_common import _create_table_section
from .batch_watchlist import (
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
    tab.grid_rowconfigure(0, weight=0)   # 标题
    tab.grid_rowconfigure(1, weight=0)   # 工具条
    tab.grid_rowconfigure(2, weight=0)   # 状态行
    tab.grid_rowconfigure(3, weight=0)   # 事件横幅 (默认隐藏)
    tab.grid_rowconfigure(4, weight=1)   # 关注池表

    _build_header(app, tab)
    _build_toolbar(app, tab)
    _build_status(app, tab)
    _build_events_banner(app, tab)
    _build_table(app, tab)

    # 首屏: 先把盘上的数据画出来, 再扫新债。顺序反过来会让首屏闪一次空表 ——
    # 而这是默认落地页, 那一闪就是用户对这个工具的第一印象。
    load_price_cache_into(app)
    _auto_add_upcoming_to_watchlist(app, silent=True)
    refresh_home(app)


# ── 分区 ────────────────────────────────────────────────────────

def _build_header(app, tab) -> None:
    head = ctk.CTkFrame(tab, fg_color="transparent")
    head.grid(row=0, column=0, sticky="ew", padx=24, pady=(10, 4))
    head.grid_columnconfigure(1, weight=1)

    title = ctk.CTkLabel(head, text="⭐ 我的关注池",
                         font=(FONT_FAMILY, 16, "bold"), text_color=TEXT)
    title.grid(row=0, column=0, sticky="w")
    Tooltip(title,
            "每天开页先看这里: 表里是上次落盘的价 (带估值日), 要最新值点「⚡ 今日刷新」。\n"
            "理论价来自三级兜底 —— 磁盘热缓存 → 本轮新债定价 → 全市场批量结果,\n"
            "所以不跑全市场也有数。\n"
            "⚠ 这是复核标记而非收益预测, 请结合公告、流动性与组合风险人工判断。")

    app.v_batch_watchlist_summary = ctk.StringVar(value="")
    ctk.CTkLabel(head, textvariable=app.v_batch_watchlist_summary,
                 font=(FONT_FAMILY, 12), text_color=TEXT_DIM, anchor="e").grid(
                     row=0, column=1, sticky="e", padx=(16, 0))


def _build_toolbar(app, tab) -> None:
    bar = ctk.CTkFrame(tab, fg_color=BG_CARD, corner_radius=12)
    bar.grid(row=1, column=0, sticky="ew", padx=16, pady=(4, 8))

    row = ctk.CTkFrame(bar, fg_color="transparent")
    row.grid(row=0, column=0, sticky="ew", padx=16, pady=10)

    # ⚡ 今日刷新 = 原「⚡ 关注池重算」。属性名保持 btn_batch_refresh_watch ——
    # batch_watchlist 的 worker 会跨模块改它的 enabled 状态 (走 _set_watch_button_state,
    # 找不到就静默跳过, 但没必要给自己制造那种缺口)。
    app.btn_batch_refresh_watch = ctk.CTkButton(
        row, text="⚡ 今日刷新", command=lambda: _refresh_watchlist_pricing(app),
        fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color=("#ffffff", "#11111b"),
        font=(FONT_FAMILY, 13, "bold"), width=110, height=32, corner_radius=6)
    app.btn_batch_refresh_watch.pack(side="left")
    Tooltip(app.btn_batch_refresh_watch,
            "只给关注池这几只取数定价, 跳过全市场。\n"
            "结果会落盘 (data/watchlist_pricing_cache.json), 下次开页直接就有。")

    app.btn_batch_upcoming = ctk.CTkButton(
        row, text="🆕 扫新债", command=lambda: _refresh_watchlist_with_upcoming(app),
        fg_color=BTN_CTRL, hover_color=BTN_HOVER, text_color=TEXT,
        font=(FONT_FAMILY, 12), width=90, height=32, corner_radius=6)
    app.btn_batch_upcoming.pack(side="left", padx=(8, 0))
    Tooltip(app.btn_batch_upcoming,
            "同步新债上市日 → 扫描 → 加入关注池 → 立刻定价。\n"
            "秒级, 不需要 Wind (走 akshare 窄同步)。")

    ctk.CTkLabel(row, text="行情源", text_color=TEXT_DIM,
                 font=(FONT_FAMILY, 13)).pack(side="left", padx=(16, 4))
    # 与批量页共用同一个 StringVar: 两页各挂一个 OptionMenu, 改哪边都同步。
    ctk.CTkOptionMenu(
        row, variable=app.v_batch_source, values=["Wind", "akshare"],
        width=90, font=(FONT_FAMILY, 12), fg_color=BG_INPUT, button_color=BTN_HOVER,
        text_color=TEXT, dropdown_fg_color=BG_INPUT, dropdown_text_color=TEXT,
    ).pack(side="left")


def _build_status(app, tab) -> None:
    # 与批量页共用 v_batch_status: 同一个 StringVar 挂两个 Label, Tk 会一起更新,
    # 于是"⚡ 已刷新关注池 N 只"这类消息在哪一页都看得见。
    ctk.CTkLabel(tab, textvariable=app.v_batch_status,
                 font=(FONT_FAMILY, 13, "bold"), text_color=TEXT, anchor="w").grid(
                     row=2, column=0, sticky="ew", padx=24, pady=(0, 8))


def _build_events_banner(app, tab) -> None:
    app.v_batch_events_banner = ctk.StringVar(value="")
    app._batch_events_banner_full = []
    app.lbl_batch_events_banner = ctk.CTkLabel(
        tab, textvariable=app.v_batch_events_banner,
        font=(FONT_FAMILY, 12, "bold"), text_color=ORANGE,
        fg_color=BG_CARD, corner_radius=12,
        padx=12, pady=8,
        anchor="w", justify="left", wraplength=1080, cursor="hand2")
    app.lbl_batch_events_banner.grid(row=3, column=0, sticky="ew", padx=16, pady=(0, 8))
    app.lbl_batch_events_banner.grid_remove()
    app.lbl_batch_events_banner.bind(
        "<Button-1>", lambda _e: _show_events_banner_full(app))


def _build_table(app, tab) -> None:
    holder = ctk.CTkFrame(tab, fg_color="transparent")
    holder.grid(row=4, column=0, sticky="nsew", padx=16, pady=(0, 6))
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
