"""📦 批量定价 Tab — 基于 cb_data 转债池 → 并发定价 → 按基差排序导出.

关注池子表 / 事件横幅 / 摘要条已抽到 :mod:`batch_watchlist`,
公共 helper (染色 / 主题刷新 / 数值格式化) 集中在 :mod:`batch_common`。
"""
from __future__ import annotations

import logging
import threading
import tkinter as tk
from typing import TYPE_CHECKING
import customtkinter as ctk
from tkinter import messagebox, filedialog, ttk

from ..theme import *
from ...batch_pricing import (
    AdmissionFilterConfig,
    BATCH_REVIEW_VIEWS,
    DEFAULT_MIN_CREDIT_RATING,
    DEFAULT_MIN_OUTSTANDING_BALANCE,
    annotate_batch_results,
    batch_pricing_exclusion_reason,
    batch_view_from_label,
    batch_view_label,
    build_batch_provider,
    cross_section_anchor_as_of,
    cross_section_anchor_from,
    filter_batch_results_by_view,
    load_batch_results_cache,
    save_batch_results_cache,
    sort_batch_results_for_review,
    sort_batch_results_for_view,
    split_batch_codes_from_cache,
    summarize_exclusions,
    summarize_batch_results,
    view_exclusion_reason,
    write_batch_results_csv,
)
from ...pricing_api import batch_price_from_provider_threaded
from ...market_valuation import (
    load_history,
    record_snapshot,
    valuation_banner,
)
from ...paths import data_path
from ..widgets import Tooltip
from .batch_common import (
    _coerce_date,
    pad_cells,
    trigger_gap_text,
    _create_table_section,
    _TREE_ATTRS,
    _apply_tag_colors,
    _attach_cell_tooltip,
    _attach_column_sort,
    _configure_responsive_columns,
    _configure_tree_style,
    _format_tags,
    _is_finite,
    _resolve_row_tag,
    refresh_theme as _refresh_theme_impl,
)
from .batch_watchlist import (
    _add_selection_to_watchlist,
    _auto_add_upcoming_to_watchlist,
    refresh_stale_watchlist,
    refresh_home,
    run_new_issue_sync_async,
)
from ...market_time import market_today

if TYPE_CHECKING:
    from ..app import CBPricerApp

logger = logging.getLogger(__name__)


def _empty_view_note(app, view: str | None) -> str:
    """空视图的说明。

    空视图是**信号在说话**, 不是判据坏了 —— 但一个空表和一个坏掉的表长得一模一样,
    所以永远要显式写一句, 不能靠留白。而"写一句"不等于"写句废话": 通用文案
    (「换个视图或点刷新重算」) 既没说为什么空, 又在建议一个未必奏效的动作 ——
    实测「转股折价」的判据是 转股溢价 < −3%, 而全池最低 **−0.3%**、中位 +58.4%,
    重算一百次也还是 0 行。

    **理由要问 ``view_exclusion_reason``, 不许在这里按视图名分支**。上一版给
    「下修优势」写过一段特判, 那个视图整体删除之后特判跟着成了死代码 —— "每加一个
    视图就加一个 if" 必然烂掉, 而视图归属的单一事实源本来就在 ``batch_pricing``。

    只在**全池同一个理由**时逐字引用它。理由串里带着**行内**数字 (「相对市场中位
    +17.9pp, 未便宜过 5pp」「双低 205 排第 44/283」), 取众数展示等于把某一只债的
    数字冒充成全池的口径 —— 实测「低估候选」若真空掉, 284 行会给出 200+ 种不同写法。
    混合理由退回通用文案: 一句诚实的下限, 好过一个精确的假数。
    """
    view_name = view or "综合机会"       # 查判据用**冻结名**
    name = batch_view_label(view_name)   # 写文案用**展示名**
    rows = getattr(app, "_batch_all_results", None) or []
    reasons = {view_exclusion_reason(row, view_name) for row in rows}
    # ``None`` = 这一行**属于**该视图, 与"视图是空的"直接矛盾 (「综合机会」从不排除
    # 任何行, 全池的理由集恰是 {None})。留着它会渲染出字面的 "None"; 而这种自相矛盾
    # 的状态本来就不该由这句文案去解释, 退回通用文案。
    if rows and len(reasons) == 1 and None not in reasons:
        # 全池同一个理由 → 这句话说的是**池子**的性质, 可以逐字引用
        return (f"「{name}」当前没有标的 — 全池 {len(rows)} 只的落选理由"
                f"都是「{reasons.pop()}」。")
    return f"「{name}」当前没有标的 — 换个视图或点「🔄 刷新重算」。"


def _iso_or_dash(value) -> str:
    """日期单元格: 缓存读回来可能是 ``date`` 也可能是 ISO 串, 两种都要认."""
    parsed = _coerce_date(value)
    return parsed.isoformat() if parsed is not None else "—"


# ── 列序 = 读者的提问次序 ─────────────────────────────────────────────────
#   这是哪只债 → **多少钱** → 便宜吗 → 现在有什么事 → (最后才是) 基础条款
#
# 关键在最后一段: 「剩余(年)」「评级」「余额」「上市日」这些**基础条款排在后面** ——
# 盯一只债时先看价、看偏差、看有没有在途事件, 条款是回过头去核对的东西, 不是第一眼
# 要扫的。把它们放在前面会把价格块整体推到右边。
#
# **价格块连成一片, 中间不插别的**: 转股价值 转股溢价 市价 理论价 可信度 偏差 相对偏差
# —— 读者比的就是这几个数, 中间隔一列就得来回扫。两条恒等式也因此都在视线内:
#   转股溢价 = 市价/转股价值 − 1     偏差 = 市价/理论价 − 1
# **两个偏差必须相邻**: 它们只差一个常数 (全市场当期中位, 实测 +20.86pp), 并排才看得出
# "本券贵不贵" 与 "相对全市场贵不贵" 是不是同向 —— 隔开之后就只剩两个孤立的百分数。
#
# **质量标注紧贴它标注的那个数**: 「可信度」说的是「理论价」, 挨着放之后邻接就承载了
# 对象, 名字才收得回 `可信度` (曾叫「理论价可信度」—— 那是它隔着 4 列、还紧挨「评级」
# 时的补救)。有守护测试钉住这处邻接: 挪走就得把主语写回名字里。
# 「定价状态」不需要邻接 —— 对象在名字里, 所以放到最末。
_BATCH_COLS_FULL = (
    # ① 这是哪只债
    ("代码", 100), ("名称", 80), ("正股", 90),
    # ② 多少钱 —— 价格块, 不许插入其它列
    ("转股价值", 75), ("转股溢价(%)", 100),
    ("市价", 65), ("理论价", 65), ("可信度", 60),
    # 两个偏差**挨着放** —— 它们只差一个常数 (全市场中位), 并排才看得出
    # "本券贵/便宜" 与 "相对全市场贵/便宜" 是不是同向
    ("偏差(%)", 70), ("相对偏差(pp)", 105),
    # ③ 便宜吗
    ("双低", 60),
    # ④ 现在有什么事
    ("事件", 150), ("正股/下修线", 100), ("标签", 220),
    # ⑤ 最后才是基础条款
    ("上市日", 90), ("剩余(年)", 70), ("余额(亿)", 75), ("评级", 50), ("正股σ(%)", 80),
    # ⑥ 这行算没算出来
    ("定价状态", 70),
)
# 「机会分」已**整体删除** (列 + 字段 + 排序信号 + min_score 门槛), 见 AGENTS。
# 实测 269/284 (95%) 的行低估项 max(0,−deviation) 恒为 0, 分数完全由评级/余额加分与
# 风险惩罚决定: Spearman(机会分, 质量分) = +0.517, 而 Spearman(机会分, 偏差) 只有
# −0.640 (纯错定价排序应为 −1.0) —— 它和「质量分」在度量同一件事的重叠部分。
# 「质量分」保留, 它本来就是从机会分里拆出来单独记账的那一支。
# 简洁 = **决策位**, 只放"看一眼就决定要不要深入"的量。三处刻意的取舍:
#   · 「正股/下修线」是唯一的下修相关列 (284/284 有值)。「稳健优势(元)」曾在这个
#     位置竞争, 已随隐含下修强度反解整体删除 —— 它在两个 regime 都结构性无解:
#     谷底 市价 < price(λ=0)、高位 市价 > price(λ=3)。
#   · 没有「可信度」—— 它近乎常量: 实测全池 高 219 / 中 64 / **低 1** (77% 是「高」)。
#     (次要理由: 「低估候选」「双低」的判据本身就含 `confidence in {高,中}`, 在那两页
#     结构上不可能出现「低」。默认视图改成「全池」之后这条不再覆盖落地页, 但主论证
#     "近乎常量"是在全池上量的, 不受影响。)
#   · 没有「定价状态」—— 实测 284/284 全 ok; 失败行由**行色**标出 (`nodata` 档
#     TEXT_DIM + 斜体), 要错误原文切「完整」。
_BATCH_COLS_SIMPLE = (
    ("代码", 100), ("名称", 90), ("正股", 90),
    # 价格块。按用户决策把「偏差(%)」也放进简洁 —— 它是"模型说贵了还是便宜了"的直读量,
    # 而「相对偏差(pp)」是同一个数减去全市场中位。两个都留: 前者跟模型比, 后者跟市场比。
    ("市价", 70), ("理论价", 70), ("偏差(%)", 70), ("相对偏差(pp)", 105),
    ("双低", 60),
    ("事件", 150), ("正股/下修线", 100), ("标签", 220),
    ("剩余(年)", 70), ("评级", 50),
)
# 列名 → 取值函数, 简洁/完整共用
_BATCH_COL_GETTERS = {
    "代码":         lambda r: r.get("bond_code", ""),
    "名称":         lambda r: r.get("bond_name", ""),
    # 正股**名称**优先, 缺失才回落代码 —— 「金隅冀东」比「000401.SZ」认得出来。
    # 实测 cb_data 里 underlying_name 只有 722/1059 (见下方数据回归说明), 所以
    # 必须有回落: 直接换成名字会让三成的行变空。
    "正股":         lambda r: r.get("underlying_name") or r.get("stock_code", "") or "—",
    # 剩余期限: 用 pricer 入参 T (实测 284/284 恒等于 (到期日−估值日)/365.25),
    # 不自己再算一遍 —— 那样"表上显示的"和"模型算的"会在条款投影后悄悄分叉。
    # 实测主池 <1 年 51 只 (18%)、<0.5 年 26 只, 而此前只有「近到期」「短久期」
    # 两个标签兜住其中 25/26 只。
    "剩余(年)":     lambda r: f"{float(r['T']):.2f}" if _is_finite(r.get("T")) else "—",
    # 余额: 硬阈值已降级为风险标签 (DEFAULT_MIN_OUTSTANDING_BALANCE=None), 而
    # 「小余额」实测只命中 1 只 —— 连续量既没进表也没被标签覆盖。中位 8.0 亿,
    # <3 亿 21 只 (7%)。
    "余额(亿)":     lambda r: f"{float(r['outstanding_balance']):.1f}" if _is_finite(r.get("outstanding_balance")) else "—",
    # 主池里这一列答的是"这只债有多新"(21 日 HV 样本够不够 / 流动性薄不薄), 不是
    # "什么时候上市" —— 实测主池 284/284 有值、未来上市 **0 只**、近 90 天上市 35 只。
    # 「什么时候上市」那个问题只在关注池里有意义 (新债不进主池, 剔除原因「已发行未
    # 上市」), 所以那边才有「待定」一档, 这边只会渲染出日期。故只进完整预设。
    "上市日":       lambda r: _iso_or_dash(r.get("listing_date")),
    # 相对偏差 = 这只债比全市场中位便宜/贵多少 (pp)。负=相对便宜。绝对偏差的水平
    # 随市场周期在 +0.4%~+21.6% 之间整体漂移, 只有相对量在横截面上可比。
    "相对偏差(pp)": lambda r: f"{float(r['relative_deviation'])*100:+.1f}" if _is_finite(r.get("relative_deviation")) else "—",
    # 双低 = 市价 + 转股溢价率×100, 越小越便宜。**方向注释已从名字挪进表头 tooltip**
    # (batch_common.COLUMN_HELP): 单位留在名字里, 方向这类"要想一下才用得上"的口径
    # 交给悬浮 —— 否则名字会一直被口径撑长。
    "双低":         lambda r: f"{float(r['double_low']):.0f}" if _is_finite(r.get("double_low")) else "—",
    # 对象 = **理论价**, 由**列序**承载: 它紧跟在「理论价」右边 (见 _BATCH_COLS_FULL
    # 的列序说明)。曾叫「理论价可信度」把对象写进名字, 那是它还隔着 4 列、且紧挨
    # 「评级」时的补救; 挪到位之后名字就该收回来。
    "可信度":       lambda r: r.get("confidence", "") if r.get("status") == "ok" else "—",
    "转股价值":     lambda r: f"{float(r['parity']):.2f}" if r.get("status") == "ok" and _is_finite(r.get("parity")) else "—",
    "转股溢价(%)":  lambda r: f"{float(r['conversion_premium'])*100:+.1f}" if _is_finite(r.get("conversion_premium")) else "—",
    # 这两个原先一个是裸下标、一个只查键在不在, 与相邻 getter 的 _is_finite 口径不一致:
    # status=="ok" 但字段缺失/为 NaN 时前者 KeyError、后者渲染出 "nan"。统一收口。
    # 对象 = **正股**的波动率 (批量路径是 vol_window_days=21 的 HV), 不是转债的
    "正股σ(%)":     lambda r: f"{float(r['sigma'])*100:.1f}" if _is_finite(r.get("sigma")) else "—",
    # **要被 status 门控**: 理论价是模型输出, 定价失败时它不该还显示一个数
    # (与「市价」正好相反 —— 那个是市场事实)。两页此前的门控方向是反的。
    "理论价":       lambda r: f"{float(r['theoretical_price']):.2f}" if r.get("status") == "ok" and _is_finite(r.get("theoretical_price")) else "—",
    # **不被 status 门控**: 市价是市场事实, 定价成不成功与它无关 —— 定价失败时整行
    # 打「—」会让"模型算挂了"和"这只债真没行情"长得一样。关注池一直是这个口径。
    # **判空用 _is_finite 而不是 `is not None`**: 落盘的 None 在关注池那条持久化路径上
    # 读回来是 **NaN** (watchlist_cache._NAN_FIELDS 含 market_price), 而
    # `NaN is not None` 为真 —— 会把"没有市价"渲染成字面的 "nan"。两页会互相喂行
    # (关注池 worker 写 _batch_all_results), 所以两边必须同口径。
    "市价":         lambda r: f"{float(r['market_price']):.2f}" if _is_finite(r.get("market_price")) else "—",
    # **1 位小数, 与紧邻的「相对偏差(pp)」一致** —— 两列只差一个常数, 挨着放却一个
    # 2 位一个 1 位, 小数点对齐之后那一位空位很扎眼; 而 0.1pp 的分辨率对筛选足够。
    "偏差(%)":      lambda r: f"{float(r['deviation'])*100:+.1f}" if _is_finite(r.get("deviation")) else "—",
    # 空值渲染「—」而不是空串 —— 空单元格读起来像"这里没有这一列", 与关注池对齐
    "评级":         lambda r: r.get("credit_rating", "") or "—",
    # 事件 = 确定性的日程/状态安排 (强赎日 / 在途下修 / 回售窗口 / 暂停转股 / 不强赎承诺)。
    # **全列出不截断**: 实测每行最多 2 条、最长 17 字符, 放得下; 而 tooltip 取的是单元格
    # display value, 一旦截断被隐藏的那条就彻底看不见了 —— 事件正是最不该被静默吞掉的一类。
    # batch_pricing.event_flags 已按可操作性排好序, 硬退出期限在最前。
    "事件":         lambda r: " / ".join(r.get("event_flags") or []) or "—",
    # 正股价距下修触发线还有多远; 负 = 已在线下, 下修博弈已经活了
    # **方向用词不用符号**。原来是 `+39%` / `−62%`, 而"距某条线 −62%"本身就不通;
    # 负 = 正股价已在触发线**下方** (下修博弈已经活了), 正 = 还在线上。
    # 对象 = **正股价**。名字去掉「距」字改成比值形式, 符号才读得通:
    # 「距离 −62%」在中文里不通, 而「正股/下修线 −62%」= 正股是触发线的 0.38 倍。
    "正股/下修线": lambda r: trigger_gap_text(r.get("down_reset_trigger_gap")),
    "标签":         lambda r: _format_tags(r.get("risk_tags")),
    # 对象 = **这一行的定价计算**。「状态」不说是谁的状态 (债的? 数据的? 行的?),
    # 「定价」是个名词不是状态量 —— 「定价状态」两样都说到。
    # 与关注池的「数据状态」是**两件事**, 名字各自点了对象: 那边说"这一行的数是什么
    # 时候的 / 为什么没有"(七档), 这边说"这一行定价成功了吗"。
    "定价状态":     lambda r: "✓" if r.get("status") == "ok" else r.get("status", ""),
}

#: 各视图**赖以成立的那几列** (判据量或排序量) 在「简洁」里缺的部分 —— 切到该视图
#: 时补进去。名字不叫 CRITERION 是因为「全池」没有判据, 它需要的是**排序量**
#: 「上市日」; 按判据命名会让那一条看上去像登记错了。
#:
#: 「简洁」是**全池视角**下的决策位, 它排掉「可信度」「定价状态」的理由是实测在默认
#: 视图里这两列近乎常量 (低估候选/双低的判据本身就含 `confidence in {高,中}`)。但那个
#: 理由**是随视图变的**, 而列预设此前不随视图变: 切到「需复核」时, 判据恰恰就是
#: status / 拦截标签 / 置信度 这三条, 其中两条没有列。实测今天那 1 只
#: (123270.SZ 盛德转债) **完全是因为 `confidence == "低"`** 进来的 (全池: 定价失败 0 ·
#: 置信度低 1 · 带拦截标签 0) —— 表上看到一行, 没有任何一列说得出它为什么在那儿。
#: 「转股折价」同理: 判据是 `转股溢价 < −3%`, 而那一列只在「完整」里。
#:
#: **不动 `_BATCH_COLS_SIMPLE` 本身** —— 全局加进去就是让 283 只不需要它的行陪着一起
#: 占宽; 这里补的是"这一屏正好需要"的那几列。
_VIEW_KEY_COLUMNS: dict[str, tuple[str, ...]] = {
    # 全池按**上市日倒序** (最新在前) —— 排序量不可见等于让用户看一张按不可见的数排好
    # 的表: 第一屏是三只刚上市的新债, 而页面上没有任何一列说得出它们为什么排在最前。
    "综合机会": ("上市日",),
    "转股折价": ("转股溢价(%)",),
    "需复核": ("可信度", "定价状态"),
}


def _batch_schema_for(cols_preset: str, view: str | None):
    """列预设 + 视图 → 实际用的 (表头, 列宽) 序列。

    **列序一律从 `_BATCH_COLS_FULL` 取**, 不是把补进来的列追加到末尾: 列序是"读者的
    提问次序"且价格块必须连成一片 (「转股溢价(%)」就落在价格块里), 追加会把它甩到
    「评级」后面, 与它要对照的「市价」隔开十列。列宽以「简洁」自己的为准 (两个预设
    对同名列的宽度不完全一样), 补进来的列取「完整」的宽度。
    """
    if cols_preset != "简洁":
        return _BATCH_COLS_FULL
    extra = _VIEW_KEY_COLUMNS.get(view or "", ())
    if not extra:
        return _BATCH_COLS_SIMPLE
    widths = dict(_BATCH_COLS_FULL) | dict(_BATCH_COLS_SIMPLE)
    keep = {name for name, _ in _BATCH_COLS_SIMPLE} | set(extra)
    return tuple((name, widths[name]) for name, _ in _BATCH_COLS_FULL if name in keep)


_BATCH_COL_STRETCH_WEIGHTS = {
    "代码": 0.5,
    "名称": 1.0,
    "正股": 0.7,
    "上市日": 0.5,          # 定长日期, 拉宽窗口不需要多余位置
    "剩余(年)": 0.3,
    "余额(亿)": 0.3,
    "相对偏差(pp)": 0.4,
    "双低": 0.3,
    "可信度": 0.25,
    "转股价值": 0.35,
    "转股溢价(%)": 0.4,
    "正股σ(%)": 0.3,
    "理论价": 0.35,
    "市价": 0.35,
    "偏差(%)": 0.35,
    "评级": 0.25,
    "正股/下修线": 0.4,
    "事件": 1.6,
    "标签": 2.0,
    "定价状态": 0.25,
}


def build(app, tab):
    """在 tab frame 上构建批量定价面板."""
    tab.grid_columnconfigure(0, weight=1)
    # _build_tabview 默认给 row 0 weight=1 (定价 tab 需要); 这里显式归零, 否则
    # 表格行的 Treeview 自然高度过大时, tkinter 会按权重同步压缩 row 0,
    # 把工具栏 ctrl 从 98px 压到 52px, cc 按钮行被裁出可视区域.
    tab.grid_rowconfigure(0, weight=0)  # ctrl
    tab.grid_rowconfigure(1, weight=0)  # status
    tab.grid_rowconfigure(2, weight=1)  # results frame

    # 控制栏
    ctrl = ctk.CTkFrame(tab, fg_color=BG_CARD, corner_radius=12)
    ctrl.grid(row=0, column=0, sticky="ew", pady=(6, 8), padx=16)

    ch = ctk.CTkFrame(ctrl, fg_color="transparent")
    ch.grid(row=0, column=0, sticky="ew", padx=16, pady=(10, 4))
    lbl_batch_title = ctk.CTkLabel(ch, text="📦 批量定价 / 转债池筛选",
                                   font=(FONT_FAMILY, 16, "bold"), text_color=TEXT)
    lbl_batch_title.pack(side="left")
    ctk.CTkLabel(ch, text="基于本地条款库全量转债池 → 并发定价 → 按当期横截面相对便宜度排序 (复核标记, 非收益预测)",
                 font=(FONT_FAMILY, 12), text_color=TEXT_DIM).pack(side="left", padx=(12, 0))
    Tooltip(lbl_batch_title,
            "相对偏差 = 本券偏差 − 全市场当期中位偏差 (负 = 相对便宜)。\n"
            "模型对全市场有系统性水平偏移且随周期在 +0.4%~+21.6% 摆动, 所以绝对低估度\n"
            "不可跨期比较, 只有横截面相对量可比。「低估候选」= 相对中位便宜 ≥5pp\n"
            "且排进当期最便宜的 15%。\n"
            "⚠️ 这仍是复核标记而非收益预测: 全市场池回测 Rank-IC≈0, 请结合公告、\n"
            "流动性与组合风险人工判断。")

    # 右侧: 转债大类估值/择时信号 (全市场中位偏差 → 贵/便宜), 随结果刷新
    app.v_batch_valuation = ctk.StringVar(value="")
    app._batch_valuation_detail = ""
    lbl_val = ctk.CTkLabel(ch, textvariable=app.v_batch_valuation,
                           font=(FONT_FAMILY, 13, "bold"), text_color=TEXT)
    lbl_val.pack(side="right")
    Tooltip(lbl_val, lambda: app._batch_valuation_detail
            or "转债大类估值/择时指标: 全市场理论价 vs 市价的中位偏差。\n"
               "中位偏差高=市场贵, 低=便宜 (历史与中证转债指数下一季收益负相关≈-0.52)。\n"
               "属大类配置参考, 非个券买入信号。")

    cc = ctk.CTkFrame(ctrl, fg_color="transparent")
    cc.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 10))

    # 行情源在顶栏 (app.v_data_source, v_batch_source 就是它本身), 页内不再摆第二个。
    # v_batch_status 同理已提到 app._build_vars —— 主页比批量页先 build, 谁也不能
    # 再假设自己是创建方。
    # 默认进入「全池」(底层名「综合机会」): 不过滤, 按上市日倒序 —— 打开先看到的是
    # 全市场的近况和最新挂牌的债, 要找便宜货再切「低估候选」。
    # 此前默认落「低估候选」, 那是把一个**判断**放在了落地页上: 它只有 43/284 只,
    # 而"今天有没有便宜货"本身随周期摆动 (中位偏差 +0.4%~+21.6%), 谷底时它诚实归零,
    # 于是默认打开就是一张空表 —— 那正是绝对机会分阈值时代踩过的坑, 换成横截面口径只是
    # 把它变得罕见, 没有消除。分母做落地页则永远不空。
    # canonical 名 (v_batch_view) 永远是 BATCH_REVIEW_VIEWS 之一; 菜单显示的是**展示名**
    # 且带 "(N)" 计数后缀, display var 与之分离, 避免回写 canonical 引发字符串不一致.
    app.v_batch_view = ctk.StringVar(value="综合机会")
    app._batch_view_display_var = ctk.StringVar(value=batch_view_label("综合机会"))
    ctk.CTkLabel(cc, text="视图", text_color=TEXT_DIM, font=(FONT_FAMILY, 13)).pack(side="left", padx=(0, 4))
    app._batch_view_menu = ctk.CTkOptionMenu(
        cc, variable=app._batch_view_display_var,
        values=[batch_view_label(v) for v in BATCH_REVIEW_VIEWS],
        command=lambda label: _on_view_menu_select(app, label),
        width=130, font=(FONT_FAMILY, 12), fg_color=BG_INPUT, button_color=BTN_HOVER,
        text_color=TEXT, dropdown_fg_color=BG_INPUT, dropdown_text_color=TEXT,
    )
    app._batch_view_menu.pack(side="left", padx=(0, 6))

    app.v_batch_cols = ctk.StringVar(value="简洁")
    ctk.CTkLabel(cc, text="列", text_color=TEXT_DIM, font=(FONT_FAMILY, 13)).pack(side="left", padx=(0, 4))
    ctk.CTkSegmentedButton(
        cc, variable=app.v_batch_cols, values=["简洁", "完整"],
        command=lambda _v: _change_batch_view(app),
        font=(FONT_FAMILY, 12), height=28,
        selected_color=ACCENT, selected_hover_color=ACCENT_HOVER,
        unselected_color=BG_INPUT, unselected_hover_color=BTN_HOVER,
        text_color=TEXT, corner_radius=6,
    ).pack(side="left", padx=(0, 12))

    app.btn_batch_run = ctk.CTkButton(
        cc, text="🔄 刷新重算", command=lambda: _run_batch(app),
        fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color=("#ffffff", "#11111b"),
        font=(FONT_FAMILY, 13, "bold"), width=110, height=32, corner_radius=6)
    app.btn_batch_run.pack(side="left")

    # 「🆕 扫新债」「⚡ 关注池重算」已搬到 ⭐ 关注池主页 —— 它们是关注池的操作,
    # 顶在「📦 批量定价 / 转债池筛选」标题下名不副实。
    # 「⭐ 加入关注池」**必须留在这里**: 它读主表控件 app._batch_main_tree 的 selection,
    # 且 iid 是 _batch_results 的整数下标, 搬到主页后永远只会弹"请先运行批量定价"。
    #
    # 次要按钮用 BTN_CTRL 而非 BG_INPUT: 浅色模式下 BG_INPUT(#e6e9ef) 与 BG_CARD(#eff1f5) 几乎同色, 按钮看不见
    app.btn_batch_add_watch = ctk.CTkButton(
        cc, text="⭐ 加入关注池", command=lambda: _add_selection_to_watchlist(app),
        fg_color=BTN_CTRL, hover_color=BTN_HOVER, text_color=TEXT,
        font=(FONT_FAMILY, 12), width=110, height=32, corner_radius=6)
    app.btn_batch_add_watch.pack(side="left", padx=(8, 0))

    app.btn_batch_export = ctk.CTkButton(
        cc, text="📝 导出 CSV", command=lambda: _export_csv(app),
        fg_color=BTN_CTRL, hover_color=BTN_HOVER, text_color=TEXT,
        font=(FONT_FAMILY, 12), width=90, height=32, corner_radius=6, state="disabled")
    app.btn_batch_export.pack(side="left", padx=(8, 0))

    # ── 公开交易硬过滤; ST/停牌/低评级/小余额等风险默认不进入主池 ──
    ctrl.grid_columnconfigure(0, weight=1)
    app.v_batch_min_rating = ctk.StringVar(value=DEFAULT_MIN_CREDIT_RATING or "")
    app.v_batch_min_balance = ctk.StringVar(
        value="" if DEFAULT_MIN_OUTSTANDING_BALANCE is None else str(DEFAULT_MIN_OUTSTANDING_BALANCE)
    )
    app.v_batch_min_turnover = ctk.StringVar(value="")

    codes, excluded = split_batch_codes_from_cache(
        getattr(app, "terms_cache", None),
        admission_config=_batch_admission_config(app),
    )
    suffix = _excluded_status_suffix(excluded)
    app.v_batch_status.set(f"将基于本地条款库的公开交易转债池定价 ({len(codes)} 只{suffix})")
    ctk.CTkLabel(tab, textvariable=app.v_batch_status,
                 font=(FONT_FAMILY, 13, "bold"), text_color=TEXT).grid(
                     row=1, column=0, sticky="w", padx=24, pady=(2, 8))

    # 事件横幅与关注池表已搬到 ⭐ 关注池主页。主表现在独占整个结果区 ——
    # 此前两表纵向 3:2 分屏, 而 Tk 在空间不足时按权重**收缩**(权重越大缩得越多),
    # 于是"主表是主角"这个意图从未兑现: 实测主表实际只占 44%~50%。
    app.batch_results_frame = ctk.CTkFrame(tab, fg_color="transparent")
    app.batch_results_frame.grid(row=2, column=0, sticky="nsew", padx=16, pady=(0, 6))
    app.batch_results_frame.grid_columnconfigure(0, weight=1)
    app.batch_results_frame.grid_rowconfigure(0, weight=1)

    app.batch_table_frame = _create_table_section(
        app.batch_results_frame, row=0, title="主批量定价结果")

    app._batch_results = []
    app._batch_all_results = []
    app._batch_upcoming_results = []
    # 启动时异步加载上次的批量定价缓存; 缓存文件 ~440KB, 同步读会让窗口出现前停顿
    # 80ms 延迟让 mainloop 先完成首屏绘制, 主表加载后再调一次 _render_batch_views 不影响关注池
    app.after(80, lambda: _load_result_cache(app, silent=True))


def _run_batch(app):
    """'批量重算' 按钮: 先窄同步新债上市日 (秒级), 再跑全池定价.

    准入判定读的是 cb_data 里的 ``listing_date``, 而它只有全量条款同步才会写 —— 不先刷
    一下, 昨天挂牌的新债今天照样被判成"已发行未上市"而进不了主池。窄同步只碰那几只新债,
    相对分钟级的全池取数可忽略。失败不阻断: 退回按现有条款库跑。
    """
    run_new_issue_sync_async(app, then=lambda synced: _run_batch_now(app, reload_terms=synced))


def _run_batch_now(app, *, reload_terms: bool = False):
    if reload_terms:
        cache = getattr(app, "terms_cache", None)
        if hasattr(cache, "reload"):
            try:
                cache.reload()
            except Exception as exc:
                logger.warning("批量重算前重载条款库失败: %s", exc)
    codes, excluded = split_batch_codes_from_cache(
        getattr(app, "terms_cache", None),
        admission_config=_batch_admission_config(app),
    )
    if not codes:
        messagebox.showwarning("提示", "本地条款库的公开交易转债池为空, 请先同步基础信息")
        return

    source = app.v_batch_source.get()
    csv_root = getattr(app, "_csv_root", None)
    if source == "CSV" and not csv_root:
        csv_root = filedialog.askdirectory(title="选择 CSV 数据根目录 (含 bonds/ stocks/ terms/ 子目录)")
        if not csv_root:
            return
        app._csv_root = csv_root

    try:
        params = dict(
            r=float(app.v_r.get()) / 100.0,
            base_spread=float(app.v_spread.get()) / 100.0,
            p_down=float(app.v_p_down.get()) / 100.0,
            distress_k=float(app.v_dk.get()) / 100.0,
            M=max(300, int(float(app.v_M.get()))),
            N=max(1000, int(float(app.v_N.get()))),
            vol_window_days=VOL_WINDOW_MAP.get(app.v_vol_window.get(), 21),
        )
    except ValueError as e:
        messagebox.showerror("参数错误", str(e))
        return

    # 自动发现即将上市新债并加入关注池
    _auto_add_upcoming_to_watchlist(app, silent=True)
    watchlist_codes = [e.get("bond_code") for e in app._batch_watchlist if e.get("bond_code")]

    app.btn_batch_run.configure(state="disabled")
    skipped = _excluded_status_suffix(excluded)
    watch = f", 关注池 {len(watchlist_codes)} 只" if watchlist_codes else ""
    app.v_batch_status.set(f"正在定价 {len(codes)} 只普通转债 (自动并发{skipped}{watch}) ...")
    app._start_progress(f"全量定价 {len(codes)} 只")

    threading.Thread(
        target=_batch_worker,
        args=(app, codes, watchlist_codes, source, csv_root, params, len(excluded)),
        daemon=True,
    ).start()


def _batch_worker(app, codes, watchlist_codes, source, csv_root, params, excluded_count=0):
    try:
        provider = build_batch_provider(
            source,
            terms_cache=getattr(app, "terms_cache", None),
            csv_root=csv_root,
            max_age_days=30,
        )
        try:
            rf = provider.get_risk_free_rate(market_today())
            if rf is not None:
                params = dict(params, r=float(rf) / 100.0)
        except Exception:
            pass

        def on_progress(done, total):
            app.after(0, lambda: app.v_batch_status.set(
                f"{provider.name} 自动并发进度 {done}/{total} ..."))

        results = batch_price_from_provider_threaded(
            provider, codes,
            progress_cb=on_progress,
            **params,
        )
        results = sort_batch_results_for_review(results)
        # 对关注池中不在主批量结果里的代码单独定价
        main_codes_set = set(codes)
        extra_codes = [c for c in watchlist_codes if c not in main_codes_set]
        watchlist_pricing = []
        if extra_codes:
            app.after(0, lambda: app.v_batch_status.set(
                f"{provider.name} 正在计算关注池 {len(extra_codes)} 只 ..."))
            watchlist_pricing = batch_price_from_provider_threaded(
                provider, extra_codes,
                **params,
            )
            watchlist_pricing = _annotate_off_pool(watchlist_pricing, results)
        success_count = sum(1 for row in results if row.get("status") == "ok")
        if success_count == 0:
            cached = _load_successful_result_cache(app)
            if cached is not None:
                app.after(0, lambda: _render_cached_after_failed_batch(
                    app, provider.name, cached))
                return
            app._batch_results = results
            app._batch_upcoming_results = watchlist_pricing
            app.after(0, lambda: _render_batch_views(
                app, results, excluded_count=excluded_count))
            app.after(0, lambda: app.v_batch_status.set(
                f"{provider.name} 本次批量定价全部失败，未更新缓存"))
            return

        cache_path = save_batch_results_cache(
            results,
            source=provider.name,
            params=params,
            upcoming_results=watchlist_pricing,
        )
        # 样本外纪律: 全市场重算即自动记录当期估值快照 (同估值日幂等覆盖)。
        # 仅主池重算触发; 缓存加载/关注池局部重算不记, 避免污染基线。
        # 覆盖率不够时它拒记并返回原因 —— 那句话要让用户看见, 见下面的 after 顺序。
        baseline_note = _record_valuation_history(results)
        app._batch_results = results
        app._batch_upcoming_results = watchlist_pricing
        app._last_batch_source = provider.name
        app._last_batch_params = dict(params)
        app.after(0, lambda: _render_batch_views(
            app, results,
            cache_path=cache_path, excluded_count=excluded_count))
        if baseline_note:
            # **排在渲染之后** —— `_render_table` 会把 v_batch_status 整个重写成视图摘要,
            # 先设就被盖掉了。app.after 的回调按登记顺序跑, 所以这里追加在摘要末尾。
            app.after(0, lambda note=baseline_note: app.v_batch_status.set(
                f"{app.v_batch_status.get()}  |  ⚠️ {note}"))
    except Exception as exc:
        app.after(0, lambda exc=exc: app.v_batch_status.set(f"❌ 批量定价失败: {exc}"))
        app.after(0, lambda exc=exc: messagebox.showerror("批量定价失败", str(exc)))
    finally:
        app.after(0, app._stop_progress)
        app.after(0, lambda: app.btn_batch_run.configure(state="normal"))


def _load_successful_result_cache(app):
    try:
        loaded = load_batch_results_cache()
    except Exception:
        return None
    results, excluded_count = _filter_nonstandard_results(
        loaded["results"], getattr(app, "terms_cache", None),
        admission_config=_batch_admission_config(app))
    if not any(row.get("status") == "ok" for row in results):
        return None
    main_results = sort_batch_results_for_review(results)
    return {
        "results": main_results,
        "upcoming_results": _annotate_off_pool(loaded.get("upcoming_results") or [],
                                               main_results),
        "meta": loaded.get("meta"),
        "excluded_count": excluded_count,
    }


def _render_cached_after_failed_batch(app, provider_name, cached):
    app._batch_all_results = cached["results"]
    app._batch_upcoming_results = cached["upcoming_results"]
    _render_batch_views(
        app,
        cache_meta=cached.get("meta"),
        excluded_count=cached.get("excluded_count", 0),
    )
    app.v_batch_status.set(
        f"{provider_name} 本次批量定价全部失败，已保留并显示上次可用缓存")


def _excluded_status_suffix(excluded):
    if not excluded:
        return ""
    by_reason = summarize_exclusions(excluded)
    top = "、".join(f"{reason}{count}" for reason, count in list(by_reason.items())[:2])
    return f", 公开交易过滤 {len(excluded)} 只 ({top})"


def _batch_optional_pos_float(var):
    """解析非负浮点; 留空或负数表示关闭该过滤项 (返回 None)."""
    raw = var.get().strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def _batch_int(var, default):
    raw = var.get().strip()
    if not raw:
        return default
    try:
        return max(0, int(float(raw)))
    except ValueError:
        return default


def _batch_admission_config(app):
    """构造公开交易硬过滤配置."""
    return AdmissionFilterConfig(
        min_outstanding_balance=_batch_optional_pos_float(app.v_batch_min_balance),
        min_credit_rating=(app.v_batch_min_rating.get().strip() or None),
        min_turnover_amount=_batch_optional_pos_float(app.v_batch_min_turnover),
    )


def _canonical_view_name(label: str) -> str:
    """剥离视图标签里的 ' (24)' 计数后缀, 还原为 BATCH_REVIEW_VIEWS 里的标准名.

    菜单里显示的是**展示名** (「综合机会」显示成「全池」), 所以回读要过
    ``batch_view_from_label`` —— 它与 ``batch_view_label`` 共用同一张表。
    这里不许再写一份反向映射: 展示名与底层名分叉正是这条路上的老毛病。
    """
    if not label:
        return "综合机会"
    name = label.split(" (")[0]
    return batch_view_from_label(name) or "综合机会"


def _render_batch_views(
    app,
    results=None,
    *,
    cache_path=None,
    cache_meta=None,
    excluded_count=0,
    refresh_home_table=True,
):
    """重画主表 (以及默认情况下的关注池主页).

    ``refresh_home_table=False`` 只给**纯展示**操作用 (切视图 / 切列预设): 那时
    ``_batch_all_results`` 一个字节都没变, 而重画会把主页那棵 17 列的树整个
    destroy 重建, 用户在上面的排序/选中/滚动位置全丢。

    反过来, 凡是**数据变了**的路径都必须让它保持 True —— 少刷一次的表现是
    "算完了但表还是旧值", 没有任何异常, 正是 AGENTS 记的那个陷阱。
    """
    if results is not None:
        app._batch_all_results = sort_batch_results_for_review(results)
    base_results = getattr(app, "_batch_all_results", None) or []
    view = _canonical_view_name(
        app.v_batch_view.get() if hasattr(app, "v_batch_view") else "综合机会")
    # 过滤在**全池**上做 (中位锚与横截面名次都需要完整 population), 排序在过滤后做。
    # 顺序反过来会让相对偏差算到子集上, 视图归属整体漂移。
    display_results = sort_batch_results_for_view(
        filter_batch_results_by_view(base_results, view), view)
    app._batch_results = display_results
    _refresh_view_menu_labels(app, base_results)
    _update_valuation_banner(app, base_results)
    _render_table(app, display_results, total_results=len(base_results), view=view, cache_path=cache_path,
                  cache_meta=cache_meta, excluded_count=excluded_count)
    if refresh_home_table:
        refresh_home(app)


def _record_valuation_history(results, history_path=None) -> str | None:
    """全市场重算后把当期估值快照并入历史基线 (样本外纪律的自动化形态)。

    返回 ``None`` = 已记录; 否则返回**不记的原因**, 由调用方显示。

    **覆盖率不够就不记**。此前唯一的闸是 ``_batch_worker`` 里的 ``success_count == 0``,
    那道闸有两个毛病: 它数的是 ``status == "ok"`` 而快照数的是有限 ``deviation``
    (两批不同的行, 见 ``snapshot_coverage``); 而且它是**全或无** —— "部分取到"那一档
    会拿几十只的中位当全市场快照追加进 ``cb_valuation_history.json``, 而那个文件**进
    版本库**、只追加, 还会当上该季度的代表 (``baseline_medians`` 取桶内最晚一条)。
    实测系统性失败能错 ±20~28pp, 超过整个历史摆幅。阈值依据见 MIN_BASELINE_COVERAGE。

    **主缓存不受这道闸管**: 它是运行态 gitignored, 部分结果照样有用 (表上「定价状态」
    列诚实标出失败行), 下次重算就修好了。两者的代价完全不同, 所以分开处置。

    同估值日幂等覆盖; 记录失败静默降级 (它是副产品, 不能影响主流程)。

    闸本身在 ``market_valuation.record_snapshot`` —— 与 ``cb-valuation --record``
    **共用同一道**。它此前只长在这个函数里, 于是 CLI 那条路可以绕过去。
    """
    try:
        # 路径解析必须留在保护区内: ``data_path`` 会 ``mkdir(parents=True)``, 在只读 HOME /
        # 打包桌面版上会抛 OSError —— 而 ``_batch_worker`` 的外层 try 会把它当成"批量定价
        # 失败", 于是 `_render_batch_views` 整个不跑, 用户看到的是一片空表加一句
        # 「❌ 批量定价失败: Read-only file system」。本函数的契约是"记录失败静默降级"。
        path = history_path or data_path("cb_valuation_history.json", seed=True)
    except Exception:
        logger.debug("估值基线路径解析失败 (忽略)", exc_info=True)
        return "估值基线写入失败: 无法定位基线文件"
    # **不静默跳过** —— 静默跳过和"记了"长得一模一样, 那是这个项目反复踩的形状
    return record_snapshot(path, results or [])


def _update_valuation_banner(app, base_results) -> None:
    """用全量定价池更新标题栏的转债大类估值/择时信号 (静默失败不影响主流程)。"""
    if not hasattr(app, "v_batch_valuation"):
        return
    try:
        history = load_history(data_path("cb_valuation_history.json", seed=True))
        # 传快照而不是裸 median: 详情里才能带上 v1/v2 口径断点说明
        banner, detail = valuation_banner(base_results or [], history)
    except Exception:
        banner, detail = "", ""
    app._batch_valuation_detail = detail
    app.v_batch_valuation.set(banner)


def _refresh_view_menu_labels(app, base_results):
    """根据当前结果实时计算各视图条数, 仅写入 *display var* (e.g. '低估候选 (24)').

    canonical 名 ``v_batch_view`` 始终保持纯净的 ``BATCH_REVIEW_VIEWS`` 之一,
    避免被 ``(N)`` 计数后缀污染。
    """
    menu = getattr(app, "_batch_view_menu", None)
    display_var = getattr(app, "_batch_view_display_var", None)
    if menu is None or display_var is None:
        return
    counts = {
        view: len(filter_batch_results_by_view(base_results, view))
        for view in BATCH_REVIEW_VIEWS
    }
    canonical = list(BATCH_REVIEW_VIEWS)
    decorated = [f"{batch_view_label(name)} ({counts.get(name, 0)})"
                 for name in canonical]

    current_name = _canonical_view_name(app.v_batch_view.get())
    target_label = decorated[canonical.index(current_name)]
    menu.configure(values=decorated)
    # 程式化 set 不会触发 CTkOptionMenu 的 command, 因此不会递归回到这里
    if display_var.get() != target_label:
        display_var.set(target_label)


def _on_view_menu_select(app, label: str) -> None:
    """用户从下拉菜单选择 → 把 canonical 名写回 ``v_batch_view`` 并刷新."""
    canonical = _canonical_view_name(label)
    if app.v_batch_view.get() != canonical:
        app.v_batch_view.set(canonical)
    _change_batch_view(app)


def _change_batch_view(app):
    """切视图 / 切列预设 —— 纯展示操作, 数据没变, 别去动主页那棵树."""
    if not getattr(app, "_batch_all_results", None):
        return
    _render_batch_views(app, refresh_home_table=False)


def _render_table(app, results, *, total_results=None, view=None, cache_path=None, cache_meta=None, excluded_count=0):
    for child in app.batch_table_frame.winfo_children():
        child.destroy()

    # 空结果**照样建表** —— 不再早返回。早返回留下的是一个已 destroy 却还挂在
    # ``app._batch_main_tree`` / ``_TREE_ATTRS`` 上的悬垂 Treeview, 下一次
    # ``refresh_theme`` 在它上面抛 TclError (真机 Tk 8.6.15 实测)。触发链是现成的:
    # 默认落地「全池」(284 行) → 切「转股折价」(实测 0 行) →
    # 切主题。``refresh_theme`` 那边也补了兜底, 但两道都要有: 空视图整块消失还会
    # 让页面高度跳变, 而关注池的空态一直是"留着表 + 一句占位文案"。
    cols_preset = (app.v_batch_cols.get()
                   if hasattr(app, "v_batch_cols") else "简洁")
    # 判据量/排序量看不见的表, 等于让用户按一个不可见的数筛选 (见 _VIEW_KEY_COLUMNS)
    schema = _batch_schema_for(cols_preset, view)
    headers = [name for name, _ in schema]
    col_widths = [w for _, w in schema]
    columns = [f"c{i}" for i in range(len(headers))]

    _configure_tree_style()
    tree = ttk.Treeview(
        app.batch_table_frame,
        columns=columns,
        show="headings",
        selectmode="extended",
    )
    y_scroll = ctk.CTkScrollbar(
        app.batch_table_frame, orientation="vertical", command=tree.yview,
        width=10, fg_color="transparent", button_color=BORDER,
        button_hover_color=TEXT_DIM,
    )
    x_scroll = ctk.CTkScrollbar(
        app.batch_table_frame, orientation="horizontal", command=tree.xview,
        height=8, fg_color="transparent", button_color=BORDER,
        button_hover_color=TEXT_DIM,
    )
    tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)

    tree.grid(row=0, column=0, sticky="nsew", padx=(10, 0), pady=(6, 0))
    y_scroll.grid(row=0, column=1, sticky="ns", pady=(6, 0), padx=(0, 10))
    x_scroll.grid(row=1, column=0, sticky="ew", padx=(10, 0), pady=(0, 8))

    _configure_responsive_columns(
        tree, columns, headers, col_widths,
        stretch_weights=_BATCH_COL_STRETCH_WEIGHTS,
    )

    _apply_tag_colors(tree)
    _attach_column_sort(tree, columns, headers)
    _attach_cell_tooltip(tree, columns, headers, tooltip_headers={"标签", "事件"})  # 表头说明走 COLUMN_HELP
    app._batch_main_tree = tree
    _TREE_ATTRS.add("_batch_main_tree")
    _attach_main_context_menu(app, tree)

    # 「标签」那一格要知道**本次渲染了哪些列** —— 承载列在场时标签就是逐字重复
    # (见 batch_common._TAG_CARRIER_COLUMN)。其余列的 getter 与行无关, 照旧走查表。
    present_columns = {name for name, _ in schema}
    for idx, r in enumerate(results):
        vals = [
            _format_tags(r.get("risk_tags"), columns=present_columns) if name == "标签"
            else _BATCH_COL_GETTERS[name](r)
            for name, _ in schema
        ]
        row_tag = _resolve_row_tag(r)
        tags = [row_tag] if row_tag else []
        # pad_cells: 右对齐列补尾随留白, 否则和右边左对齐列的文字贴在边界上
        tree.insert("", "end", iid=str(idx), values=pad_cells(headers, vals), tags=tags)

    if not results:
        # 「转股折价」实测今天是 0 行 —— 空视图是信号在说话, 不是判据坏了
        # (见 README 的模型边界一节)。所以要显式写出来: 一个消失的控件和一个坏掉的
        # 控件长得一模一样。文案怎么组织见 _empty_view_note。
        ctk.CTkLabel(
            app.batch_table_frame,
            text=_empty_view_note(app, view),
            font=(FONT_FAMILY, 12), text_color=TEXT_DIM,
            anchor="w", justify="left", wraplength=1000,
        ).grid(row=2, column=0, sticky="w", padx=12, pady=(2, 8))

    summary = summarize_batch_results(results)
    total = total_results if total_results is not None else summary["total"]
    view_name = batch_view_label(view or "综合机会")
    parts = [
        f"✅ {view_name}: 展示 {summary['total']}/{total} 只",
        f"成功 {summary['success']}  失败 {summary['failed']}",
    ]
    if excluded_count:
        parts.append(f"公开交易过滤 {excluded_count} 只")
    app.v_batch_status.set("  |  ".join(parts))
    app.btn_batch_export.configure(state="normal" if results else "disabled")

    # 缓存时效信息搬到状态栏 (左侧 _data_freshness 区), 不再挤占复核状态行
    saved_at_iso: str | None = None
    if cache_path is not None:
        from datetime import datetime as _dt
        saved_at_iso = _dt.now().isoformat(timespec="seconds")
    elif cache_meta:
        saved_at_iso = cache_meta.get("saved_at")
    if hasattr(app, "_set_batch_freshness"):
        app._set_batch_freshness(saved_at_iso)


def refresh_theme(app: "CBPricerApp") -> None:
    """主题切换后刷新 Treeview 样式 + 给所有已注册树重新染色.

    ``app.py`` 的 ``_toggle_theme`` 在 ``ctk.set_appearance_mode`` 之后调用本函数.
    """
    _refresh_theme_impl(app)


def _load_result_cache(app, *, silent: bool = False):
    try:
        loaded = load_batch_results_cache()
    except FileNotFoundError as exc:
        if not silent:
            messagebox.showinfo("提示", str(exc))
        return
    except Exception as exc:
        if not silent:
            messagebox.showerror("加载缓存失败", str(exc))
        return

    results, excluded_count = _filter_nonstandard_results(
        loaded["results"], getattr(app, "terms_cache", None),
        admission_config=_batch_admission_config(app))
    results = sort_batch_results_for_review(results)
    app._batch_all_results = results
    app._batch_upcoming_results = _annotate_off_pool(
        loaded.get("upcoming_results") or [], results)
    # 自动将即将上市新债加入关注池
    _auto_add_upcoming_to_watchlist(app, silent=True)
    _render_batch_views(
        app,
        cache_meta=loaded.get("meta"), excluded_count=excluded_count)
    # 给关注池里陈旧/缺价的标的补一轮 —— 判据从"是不是没价的新债"放宽成
    # `_price_state != "ok"` (见 stale_watchlist_codes): 新债不在主池 (剔除原因
    # 「已发行未上市」), 理论价只能来自 upcoming_results, 而那一格一旦是空的就再没有
    # 自愈路径 (实测缓存 n_upcoming_results=0 时三只在途新债连着几天都没有理论价);
    # 隔夜的旧价、上一轮失败的行同理没人管。带 15 分钟防抖, 且遇到"源当前连不上"
    # 会被直接挡掉而不是卡住启动。
    try:
        refresh_stale_watchlist(app, quiet=True)
    except Exception:
        logger.debug("缓存加载后自动补价失败 (忽略)", exc_info=True)


def _export_csv(app):
    if not app._batch_results:
        messagebox.showinfo("提示", "请先运行批量定价")
        return
    path = filedialog.asksaveasfilename(
        title="导出批量定价结果",
        defaultextension=".csv",
        filetypes=[("CSV", "*.csv")],
        initialfile="batch_pricing.csv",
    )
    if not path:
        return
    try:
        write_batch_results_csv(path, app._batch_results)
        app.v_batch_status.set(f"已导出 {len(app._batch_results)} 条到 {path}")
    except Exception as exc:
        messagebox.showerror("导出失败", str(exc))


def _annotate_off_pool(rows, pool_results):
    """给**主池外**的一小撮结果 (新债 / 只在关注池里的债) 补研究字段.

    三件事必须同时做, 缺一个都会静默算错:

    1. 锚用主池的中位偏差, 不要让这几行自算 —— ``median_deviation_of`` 样本 <30 时
       返回 None, ``annotate_batch_result`` 随即退回绝对阈值; 而真自算出来更糟:
       6 行子集的中位就是它们自己, 于是每只的 ``relative_deviation`` 恰好偏移一个
       中位的量 (实测 +20.9pp), 数字看上去完全正常。
    2. ``rank_scope=False`` —— 传锚**修不了秩**, 名次是在传进来的这一批内部排的。
       实测 123281.SZ 全池 ``cheapness_percentile=0.8794``, 6 行子集单独算变 0.0。
    3. 锚的 **as-of** 也盖上 —— 这几行拿的是**别人的**锚, 与主池行"锚天然与自己
       同日"不是一回事。展示层靠它判断该不该把「相对偏差 / 双低」灰掉。

    这一档尤其容易被漏, 因为它没有自愈路径: ``_batch_all_results`` 每次都过
    ``sort_batch_results_for_review`` 在全池上重标注, 错了下一轮就修回来; 而
    ``_batch_upcoming_results`` 标注一次之后再没人碰。
    """
    if not rows:
        return []
    pool = pool_results or []
    annotated = annotate_batch_results(
        rows,
        market_median_deviation=cross_section_anchor_from(pool),
        rank_scope=False,
    )
    # 3. 锚的 **as-of** 也要跟着盖上。这几行是拿**别人的**锚标注的, 与主池行
    #    "锚天然与自己同日"不是一回事 —— 主池缓存可能是几天前跑的 (实测热缓存
    #    2026-08-28, 锚源行 08-26)。展示层靠这个戳决定该不该把「相对偏差 / 双低」
    #    灰掉; 没有它, 锚的年龄在盘上恒为 0, 口径5 接上了也判不出陈旧。
    anchor_as_of = cross_section_anchor_as_of(pool)
    if anchor_as_of is not None:
        for row in annotated:
            row["market_median_deviation_as_of"] = anchor_as_of
    return annotated


def _filter_nonstandard_results(results, terms_cache=None, admission_config=None):
    kept = []
    excluded_count = 0
    for row in results:
        code = row.get("bond_code", "")
        reason = batch_pricing_exclusion_reason(code, row, admission_config=admission_config)
        if reason is None and terms_cache is not None and hasattr(terms_cache, "get"):
            try:
                reason = batch_pricing_exclusion_reason(
                    code, terms_cache.get(code), admission_config=admission_config)
            except Exception:
                reason = None
        if reason is None:
            kept.append(row)
        else:
            excluded_count += 1
    return kept, excluded_count


def _attach_main_context_menu(app, tree):
    menu = tk.Menu(tree, tearoff=0, font=(FONT_FAMILY, 12))
    menu.add_command(label="⭐ 加入关注池",
                     command=lambda: _add_selection_to_watchlist(app))
    menu.add_command(label="载入单债定价页 (双击)",
                     command=lambda: _load_selection_in_pricing_tab(app))

    def _popup(event):
        clicked = tree.identify_row(event.y)
        if clicked and clicked not in tree.selection():
            tree.selection_set(clicked)
        if not tree.selection():
            return
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _on_double_click(event):
        clicked = tree.identify_row(event.y)
        if not clicked:
            return
        tree.selection_set(clicked)
        _load_selection_in_pricing_tab(app)

    tree.bind("<Button-3>", _popup)
    tree.bind("<Button-2>", _popup)
    tree.bind("<Double-1>", _on_double_click)


def _load_selection_in_pricing_tab(app):
    tree = getattr(app, "_batch_main_tree", None)
    if tree is None or not app._batch_results:
        return
    selection = tree.selection()
    if not selection:
        messagebox.showinfo("提示", "请先在主批量列表中选择一只转债")
        return
    try:
        row = app._batch_results[int(selection[0])]
    except (ValueError, IndexError):
        return
    code = row.get("bond_code")
    if not code:
        return
    if hasattr(app, "v_bond_code"):
        app.v_bond_code.set(code)
        # 650ms 防抖是给"用户逐字敲代码"用的; 从表里双击是一次确定的选择, 没有后续
        # 按键要合并, 等它只会让新债的条款晚半秒才落位。
        if hasattr(app, "_flush_pending_bond_autoload"):
            app._flush_pending_bond_autoload()
    if hasattr(app, "tab_seg") and hasattr(app, "_switch_tab"):
        app.tab_seg.set(E("⚡ 定价"))
        app._switch_tab(E("⚡ 定价"))
    app.v_batch_status.set(f"已载入单债定价页: {code}")
