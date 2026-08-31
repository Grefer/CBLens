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
from ...batch_pricing import (
    DATA_QUALITY_RISK_TAGS, TRADABILITY_RISK_TAGS,
    is_unlisted_new_bond, risk_tag_label,
)
from ...data_providers.base import CREDIT_RATING_RANK
from ..widgets import Tooltip


# ── Treeview 行标签颜色 (主表 + 关注池表共用) ──────────────────
#
# **整行颜色是全表最贵的通道, 只回答一件事: 这一行的话能不能听。**
# 它不表达贵/便宜, 两条独立的理由:
#
# ① 便宜度已经被**行位置**编码完了。``sort_batch_results_for_view`` 对
#    「低估候选/转股折价」按 ``relative_deviation`` 升序, 而「低估候选」的准入判据
#    本身就是 ``rel < −5pp`` —— 任何架在便宜度上的行色在那两页上都是整表同色
#    (实测 40/40)。换阈值救不了, 换的是同一个病。
#    ⚠️ **这条对「全池」不成立**: 它按上市日倒序, 行位置根本不编码便宜度, 上色在
#    那一页确实会**增加**信息。默认落地页改成全池之后 ① 不再覆盖落地页 —— 撑住这个
#    决定的是 ②, 而 ② 本来就是在全池上量的。
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
#:
#: ``blocked`` 刻意**不叫**「不可交易」: 它收的是 临近摘牌 / 余额清零 / 正股停牌 /
#: 正股跌停 / 转债停牌 —— 「临近摘牌」今天照样买得到, 「正股跌停」压根
#: 不拦转债本身。这一档的真实语义是 AGENTS 里那句"买不到 / 快买不到了", 「买卖受限」
#: 覆盖得住而「不可交易」是把最重的那一档当成了全部。
#:
#: 「正股风险」(ST) **已移出这一档** —— 它归标的风险维: ST 正股的转债照常挂牌撮合,
#: 是"风险较大"而不是"买不到"。它现在只出现在标签列里, 不染行色。
_TAG_LEGEND: dict[str, str] = {
    "new":     "未上市",
    "blocked": "买卖受限",
    "nodata":  "数据缺失或定价失败",
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


#: 「数据状态」列已经把这两档说清了 (而且更具体: 「未定价 · 已发行未上市」), 标签列
#: 再写一遍就是同一行两列逐字重复 —— 实测三只未上市新债的标签正是 ['无偏差','无市价']。
#: **只挡这两个**, 不是整个数据质量维: 「无HV」「无评级」「无余额」「数据缺口」在表上
#: 没有专属列承载, 挡掉就真丢了。底层 ``risk_tags`` 不动 —— 行色 / 置信度 / 策略
#: 排除集照读原集。
_TAGS_COVERED_BY_DATA_COLUMN = frozenset({"无市价", "无偏差"})

#: 标签 → **承载同一事实的数值列**。那一列在同一张表上时, 标签就是同一行两列逐字重复,
#: 而且列还更精确 ——「低评级」把 A+ / A / A- / BBB+ / BBB / CC 六档、信用利差下限差 14 倍
#: 的范围压成一个词, 而「评级」列原样显示、可排序、进得了 CSV。
#:
#: 这是 ``_TAGS_COVERED_BY_DATA_COLUMN`` 那条规则的一般化 (那里的理由写的是"「数据状态」列
#: 已经把这两档说清了, 标签列再写一遍就是同一行两列逐字重复")。两张表分开是因为判据不同:
#: 那一张的承载列是**状态文案**且标签是拦截档, 这一张的承载列是**数值**且标签一律非拦截。
#:
#: **按预设判, 不是一刀切**: 「较高HV」在简洁预设里必须留着 —— 「正股σ(%)」只在完整预设,
#: 简洁页上它是唯一的波动率线索 (实测 35 行)。一刀切删掉正是评审指出的删过头。
#:
#: 两条排除规则由守护测试钉住 (``test_suppressed_tags_are_never_blocking_or_opportunity``):
#:   · **拦截标签不进这张表** —— 行已经被染色, 标签是唯一的解释, 抑制它等于一行红色没有理由;
#:   · **机会信号不进这张表** —— "为什么值得看这只债"正是标签列存在的意义。
#: 底层 ``risk_tags`` 一个字节不动: 行色 / 置信度 / 复核分桶 / 复核建议 / CSV 照读原集,
#: 所以这是纯展示改动, 四个通道逐行不变。
_TAG_CARRIER_COLUMN: dict[str, str] = {
    "低评级": "评级",
    "短久期": "剩余(年)",
    "近到期": "剩余(年)",
    "高HV": "正股σ(%)",
    "较高HV": "正股σ(%)",
    "小余额": "余额(亿)",
    "触及摘牌线": "余额(亿)",
}


def _format_tags(tags, *, drop_covered: bool = False, columns=None) -> str:
    """标签串; 挡掉已经被同一张表上的列承载的那些。

    *drop_covered* 挡掉「数据状态」列承载的那两个 (关注池用)。
    *columns* 给出本表实际渲染的列名集合, 据此挡掉 ``_TAG_CARRIER_COLUMN`` 里承载列在场的
    标签 —— 传 None (默认) 表示不知道列集, 一个都不挡, 这是保守的那一侧。

    走 ``risk_tag_label`` 取展示名 —— 「模型高估离群」的动宾读法与事实相反, 见
    ``batch_pricing.RISK_TAG_DISPLAY_LABEL``。
    """
    if not tags:
        return ""
    if isinstance(tags, str):
        return tags
    items = [t for t in tags if t]
    if drop_covered:
        items = [t for t in items if t not in _TAGS_COVERED_BY_DATA_COLUMN]
    if columns:
        present = set(columns)
        items = [t for t in items if _TAG_CARRIER_COLUMN.get(t) not in present]
    return " / ".join(risk_tag_label(str(tag)) for tag in items)


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
    """新债判定 —— 委托给库层的 :func:`batch_pricing.is_unlisted_new_bond`。

    判据本身连同它的全部理由 (日期硬证据压过派生字段、自我确认陷阱) 都在那边。
    这里**不再另写一份**: 库层的估值基线覆盖率闸与这里的行色/关注池覆盖率问的是
    同一个问题, 两处各写一份正是这个仓库反复踩的形状 (事件展示表、评级利差表都栽过)。
    """
    return is_unlisted_new_bond(row)


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

    "无色"是**有含义的一档**: 没有否决理由, 而且是压倒性的常态 —— 2026-08-31 实测
    默认落地视图「全池」311 行里 **blocked 4 · new 2 · 无色 305 (98.1%)**。
    (早先记的是"284 行三档全为 0"; 那个更强的版本已不成立, 但结论不变 —— 一屏里
    染色行仍是个位数, 无色仍是默认预期。)
    图例怎么写见 ``row_colour_legend`` —— 那里按 2026-08-29 的决策**不再**为这一档
    单列一行。**那个决策当时的理由正是"三档全 0", 现在前提变了**: 每页确实看得见
    染色行了, 所以"一页全无色和配色坏了长得一样"这个风险已经小很多, 决策照旧成立,
    只是依据从"全 0"换成"98% 无色 + 有染色行做对照"。

    没有 ``status`` 键的行 (关注池从没算过的那一档) 不染色: "从没算过"不是"这行
    数字是坏的", 那两档的区分由「数据状态」列的五档文案承载。
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


_LEGEND_TITLE = "整行颜色的含义:"


def row_colour_legend_segments() -> list[tuple[str, tuple | None, tuple | None]]:
    """行色图例的分段 ``(文本, 颜色, 字体)`` —— 直接喂给 ``Tooltip(segments=)``.

    **这是单一事实源**, 纯文本版 (:func:`row_colour_legend`) 由它拼出来。与
    ``WATCH_REFRESH_LABEL`` 那条同构: 用户看得见的名字只许有一处, 否则改了 tag
    之后图例里会留着一个**过期**的档位名, 用户对着表找一个不存在的颜色。

    每档用**它自己的颜色与字重**渲染 —— 提示要*演示*颜色, 不是用文字描述颜色:
    在一行本来就是红色加粗的字前面写「红色加粗 =」, 说的是读者眼前就能看见的东西。
    (上一版没有颜色可用, 只能靠 ``_TAG_APPEARANCE`` 把色名写成文字; 那张表随之删掉,
    留着就是第二份会过期的展示词表。)

    字重/字形跟着 ``_BOLD_TAGS`` / ``_ITALIC_TAGS`` 走, 不另抄一份 —— 但**斜体那一档
    在 macOS 上对中文渲染不出来, 别再声称"三档不靠色相也分得开"**:

    - ``FONT_FAMILY`` = PingFang SC **没有斜体字面**, Tk/Cocoa 也不合成倾斜 ——
      实测 ``tkfont.Font(font=("PingFang SC", 12, "italic")).actual()["slant"]``
      返回 ``roman`` (同一组 ``bold`` 正常返回 ``bold``)。
    - 换 ``FONT_MONO`` = Menlo **也修不好**: ``actual()`` 确实报 ``italic``, 但 Menlo
      没有 CJK 字形, 中文走回落而回落字体不倾斜 —— 实测正体/斜体量宽**完全相等**
      (中文 106 = 106)。改字族只会让 ``actual()`` 变好看、让守护测试变绿, 而用户
      看到的还是直立的中文: 用一个绿测试担保一句假话, 比现状更糟。

    所以这个 tooltip 里第二通道**只有 bold 那一档真的生效**, ``nodata`` 与 ``new``
    之间实际只剩色相差 (灰阶下 MAUVE≈106 vs TEXT_DIM≈112, 基本分不开)。斜体仍然留着
    是因为它在 Windows 上会被合成、对拉丁/数字也真斜 —— 但**不要**据此在文档里写
    "三档分得开"。表里那一档同理: 数字列真斜、中文列不斜 (那是既有状态, 与本函数无关)。

    颜色传主题元组 ``(light, dark)`` 而不是 ``get_color`` 取死值: tooltip 每次悬停
    新建, CTk 构造时按当前 appearance mode 解析, 切主题天然跟随。
    """
    segments: list[tuple[str, tuple | None, tuple | None]] = [(_LEGEND_TITLE, None, None)]
    for tag, label in _TAG_LEGEND.items():
        segments.append((f"  {label}", _TAG_COLORS[tag], _legend_font(tag)))
    return segments


def _legend_font(tag: str) -> tuple:
    """图例某一档的字体 —— 第二通道跟着表里的定义走, 不另抄一份.

    ⚠️ ``italic`` 在 macOS 上对中文**渲染不出来** (见 ``row_colour_legend_segments``
    的说明); 留着它是为了 Windows 与拉丁字符, 不要把它当成"这一档分得开"的依据。
    """
    if tag in _BOLD_TAGS:
        return (FONT_FAMILY, 12, "bold")
    if tag in _ITALIC_TAGS:
        return (FONT_FAMILY, 12, "italic")
    return (FONT_FAMILY, 12)


def row_colour_legend() -> str:
    """图例的纯文本版 (给测试与任何非 GUI 消费者); 由分段拼出来, 不另写一份."""
    return "\n".join(text for text, _color, _font in row_colour_legend_segments())


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


#: 列对齐 —— **表头与内容必须同向**, 否则短表头居中而值靠左, 视觉上整列是错位的。
#: 三档判据各不相同, 不是审美偏好:
#:
#:   右 (`e`)  数值列。**要对小数点** —— 左对齐时 160.74 / 98.5 / 1234.5 参差不齐,
#:             比大小得逐行读数字, 而这几列的全部用途就是比大小。
#:   中 (`center`) 1~3 字符的分类值 (高/中/低、✓、AA-)。没有小数点要对, 而列宽由
#:             表头定, 左对齐会在右边留一大片空。
#:   左 (`w`)  文本 (代码/名称/正股/事件/标签/日期)。默认档。
#:
#: 键是表头文本, 两页共用。没登记的走左对齐。
_COLUMN_ALIGN_RIGHT = frozenset({
    "市价", "理论价", "转股价值", "双低",
    "偏差(%)", "相对偏差(pp)", "转股溢价(%)", "正股σ(%)",
    "正股/下修线", "剩余(年)", "余额(亿)",
})
_COLUMN_ALIGN_CENTER = frozenset({"可信度", "定价状态", "评级"})


#: 右对齐单元格的尾随留白。**ttk 没有 per-cell padding** (Treeview.Cell 元素在
#: aqua 主题下根本不暴露), 于是"右对齐列 + 紧跟一个左对齐列"时两段文字会**贴在
#: 列边界上** —— 实测关注池的「双低→事件」「正股/下修线→标签」两处正是这样。
#: 靠加宽解决不了: 右对齐把文字钉在右边缘, 加多少宽都贴着。
#:
#: 所以在文本上留白。统一给**右对齐**那一侧加, 而不是按"下一列是不是左对齐"分情况
#: —— 后者是位置相关的, 列序一变就得重算; 而所有右对齐列一起右移同样多, 它们之间
#: 的小数点对齐不受影响。
#:
#: 排序/缺失值判定都会 ``.strip()``, 所以这个留白不参与任何逻辑 (有守护测试)。
CELL_GUTTER = "  "


def pad_cells(headers, values) -> list[str]:
    """按对齐给单元格补留白 —— 两张表插行前都要过一道."""
    return [
        f"{value}{CELL_GUTTER}" if column_align(header) == "e" else str(value)
        for header, value in zip(headers, values)
    ]


def heading_text(header: str, arrow: str = "") -> str:
    """表头文本; 右对齐列同样补留白, 否则表头之间也会贴在一起 (实测「双低 事件」)."""
    text = f"{header}{arrow}"
    return f"{text}{CELL_GUTTER}" if column_align(header) == "e" else text


def column_align(header: str) -> str:
    """这一列的对齐方式 (``e`` / ``center`` / ``w``); 表头与内容共用同一个值."""
    if header in _COLUMN_ALIGN_RIGHT:
        return "e"
    if header in _COLUMN_ALIGN_CENTER:
        return "center"
    return "w"


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
        # 表头与内容同向 —— 此前表头走 ttk 默认 (居中)、内容一律 anchor="w",
        # 于是短表头居中而值靠左, 整列看着是错位的; 数值列还因为左对齐而对不上小数点。
        align = column_align(header)
        tree.heading(column, text=heading_text(header), anchor=align)
        tree.column(column, width=width, minwidth=min_width, stretch=False, anchor=align)

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
    默认落地「全池」(284 行) → 切「转股折价」(**实测 0 行**)
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


#: 有序的**分类**列: 值不是数, 但也不该按字符串排。键 = 表头文本 (与 COLUMN_HELP /
#: column_align 同一套键)。
#:
#: 少了这张表, ``_attach_column_sort`` 的"至少一半能 float"判据对它们全部失败, 整列
#: 静默退化成 ``str().lower()`` 序 —— 与 AGENTS 记的「线上 123% 排在线上 3% 前面」是
#: 同一个失效, 只是这次连中文前缀都没有, 更看不出来:
#:   · 「评级」ASCII 降序给出 CC > BBB+ > AA- > AA+ > AA > A+ —— 垃圾级排在最上面,
#:     而 AA+ 排在 AA- 下面 (``+`` 是 0x2B, ``-`` 是 0x2D, 所以 ``AA+`` < ``AA-``)
#:   · 「可信度」中文码点序是 中(0x4E2D) < 低(0x4F4E) < 高(0x9AD8), 于是「需复核」视图
#:     专门加出来解释"这行为什么在这儿"的那一列, 把「低」排在中间而不是任一端
#: 评级档位表复用 ``data_providers.base.CREDIT_RATING_RANK`` —— 不在这里抄第三份。
_ORDINAL_SORT_SCALES: dict[str, dict[str, int]] = {
    "评级": CREDIT_RATING_RANK,
    "可信度": {"低": 0, "中": 1, "高": 2},
}


def trigger_gap_text(gap) -> str:
    """正股价相对下修触发线, 带符号: ``S0/(K*ratio) - 1``。负 = 正股价在触发线**下方**。

    曾渲染成「线下 62%」/「线上 39%」—— 那是列名还叫「正股距下修线」时的补救:
    「距离 −62%」在中文里不通 (距离是非负概念)。列名去掉「距」字改成
    「正股/下修线」之后, `+39%` 读作"正股是下修线的 1.39 倍", 符号自洽了,
    而且**原生可排序** (`_parse_sortable_number` 会剥掉 +/%), 不再需要专门的排序键。

    刻意**不写「已触发」**: 下修触发是**路径条件** (连续 N 日中至少 M 日低于触发线),
    这里只是单点 S 的近似 (见 README「模型边界」)。价在线下是必要不充分条件。

    两页共用这一份 —— 曾经 batch 与 batch_watchlist 各存一份 (那两个模块会成环)。
    """
    if not _is_finite(gap):
        return "—"
    return f"{float(gap) * 100:+.0f}%"


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

        # 数值列识别: 至少一半 present 值能解析为 float。
        # **带中文前缀的值会全部返回 None, 这一列就静默退化成字符串序** —— 曾经
        # 「线上 123%」排在「线上 3%」前面、整个「线上」组还排在「线下」组前面, 不报错。
        # 现在所有数值列都渲染成裸的带符号数, 由 test_numeric_columns_sort_numerically 守着。
        # 有序分类列 (评级 / 可信度) 走各自的档位表; 其余列试解析成数。两条路之后的
        # 处理完全一样 (多数命中才算数, 解析不出的沉底), 所以只换取值函数不换分支。
        scale = _ORDINAL_SORT_SCALES.get(headers[col_idx])
        if scale is not None:
            parsed = [(scale.get(str(v).strip()), iid) for v, iid in present]
        else:
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
            tree.heading(col, text=heading_text(header, arrow))

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


#: 表头悬浮说明。**哪几列要写是编辑决定, 不是规则** —— 判据是"不看说明会不会读错",
#: 由人逐列过一遍定的 (当前 20 条; 剩余(年) / 定价状态 / 数据状态 三列刻意不写
#: —— 后者的列名已经把七档文案的共同点说出来了)。
#: 守护测试**不再钉"每列都要有"** —— 那正是为了写 tooltip 而写 tooltip: 给「评级」凑
#: 一句"债项信用评级"读者什么也没多知道, 真正需要解释的那几条反而被淹在里面。
#: 它现在只钉结构性的东西: 无孤儿键、够短、名字里不留方向注释。
#:
#: 写法: 一句话说清**怎么读**, 能一行就别两行 (当前全部单行, 平均 17 字)。带符号的量
#: 只说"越正/越负各是什么"; 公式只在它本身就是定义时写 (转股价值 / 转股溢价 / 双低),
#: 不写实现细节 (扣几分、哪个函数算的)。
#:
#: 分工: 列名说**是什么 / 度量的是谁 / 单位**, 悬浮说**怎么读 / 何时不可用**。
#: 所以 `(小=便宜)` 这类方向注释从名字挪进来, 而 `(pp)/(%)/(元)` 这类单位留在名字里。
#:
#: 两页共用一份 (键 = 表头文本)。
COLUMN_HELP: dict[str, str] = {
    "代码": "转债代码",
    "名称": "转债简称。上市首日会加 N 前缀。",
    "正股": "正股名称; 缺名字时显示代码。",
    "转股价值": "面值 / 转股价 × 当前正股价, 即转股后立刻卖出能拿到的钱。",
    "转股溢价(%)": "市价 / 转股价值 − 1。",
    "市价": "转债最新收盘价。",
    "理论价": "Crank-Nicolson PDE 定价结果。",
    "可信度": "模型理论价可信程度。",
    "偏差(%)": "越正，市价相比模型价越贵, 反之亦然。",
    "相对偏差(pp)": "越正，标的相比全市场中位数越贵，反之亦然。",
    "双低": "市价 + 转股溢价率×100, 越小越便宜。经验阈值约 130。",
    "事件": "在途日程: 强赎 / 下修 / 回售 / 暂停转股 / 不强赎承诺。",
    "正股/下修线": "当前正股价和下修触发价关系。",
    "标签": "风险与机会标签。悬停单元格看完整内容。",
    "上市日": "「待定」= 已发行但上市日未公告。",
    "余额(亿)": "未转股余额, 低于 0.3 亿触及法定摘牌线。",
    "评级": "债项信用评级。",
    "正股σ(%)": "正股的年化波动率 (21 日 HV)。",
    "加入日": "加入关注池的日期。",
}


def _attach_cell_tooltip(
    tree: ttk.Treeview,
    columns,
    headers,
    *,
    tooltip_headers: set[str] | None = None,
    header_help: dict[str, str] | None = None,
    delay_ms: int = 300,
) -> None:
    """给 Treeview 加悬浮提示: 单元格看完整文本, **表头看口径说明**.

    单元格 tooltip 直接取当前 display value, 因此表头排序后也能自然跟随行移动;
    表头 tooltip 查 *header_help* (默认 :data:`COLUMN_HELP`)。

    表头这一路是后加的 —— 在此之前 ``tree.heading(col, text=...)`` 是唯一出口,
    列名之外没有第二个解释预算, 于是方向/对象/单位全靠往名字里加字。
    """
    targets = set(tooltip_headers or headers)
    help_map = COLUMN_HELP if header_help is None else header_help
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
        col_id = tree.identify_column(event.x)
        # 表头区: identify_row 在这里返回 "", 老实现直接 _hide 了 —— 那正是
        # "表头没有 tooltip 机制"的由来。
        try:
            in_heading = tree.identify_region(event.x, event.y) == "heading"
        except tk.TclError:
            in_heading = False
        if in_heading and col_id:
            try:
                idx = int(col_id.lstrip("#")) - 1
            except ValueError:
                _hide()
                return
            text = help_map.get(headers[idx]) if 0 <= idx < len(headers) else None
            if not text:
                _hide()
                return
            key = ("__heading__", idx, text)
            if state.get("cell") == key:
                tip = state.get("tip")
                if tip is not None:
                    tip.wm_geometry(f"+{event.x_root + 12}+{event.y_root + 16}")
                return
            _hide()
            state["cell"] = key
            state["after"] = tree.after(
                delay_ms,
                lambda t=text, x=event.x_root, y=event.y_root: _show(t, x, y),
            )
            return
        row_id = tree.identify_row(event.y)
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
    title_label = ctk.CTkLabel(
        header, text=title,
        font=(FONT_FAMILY, 13, "bold"), text_color=TEXT,
    )
    title_label.grid(row=0, column=0, sticky="w")
    # 行色图例挂在**标题的 tooltip** 里, 不再常驻一行: 它解释的是一个多数行都用不上
    # 的维度 (默认落地视图里往往一行都不染), 常驻等于拿最显眼的位置放最少用的信息。
    # 两页共用同一份文案, 不各写一份。
    Tooltip(title_label, segments=row_colour_legend_segments())

    summary_var = None
    if with_summary:
        summary_var = ctk.StringVar(value="")
        ctk.CTkLabel(
            header, textvariable=summary_var,
            font=(FONT_FAMILY, 11), text_color=TEXT_DIM, anchor="e",
        ).grid(row=0, column=1, sticky="e", padx=(12, 0))

    body_row = 1
    section.grid_rowconfigure(body_row, weight=1)
    body = ctk.CTkFrame(section, fg_color="transparent")
    body.grid(row=body_row, column=0, sticky="nsew")
    body.grid_columnconfigure(0, weight=1)
    body.grid_rowconfigure(0, weight=1)
    if with_summary:
        return body, summary_var
    return body
