from dataclasses import dataclass
from datetime import date, timedelta

import inspect

import pytest

from convertible_bond import batch_pricing
from convertible_bond.gui.tabs import batch as batch_tab
from convertible_bond.gui.tabs import batch_watchlist as watchlist_tab
from convertible_bond.gui.tabs.batch_common import _is_new_bond, _resolve_row_tag
from convertible_bond.gui.tabs.batch_watchlist import (
    _format_days_to_trade,
    _format_listing_cell,
)
from convertible_bond.market_time import market_today


def test_listed_tradable_bond_is_not_marked_new():
    row = {
        "bond_code": "118067.SH",
        "status": "ok",
        "is_tradable": True,
        "trading_status": "tradable",
        "listing_date": market_today() - timedelta(days=5),
        "tradable_date": market_today() - timedelta(days=5),
    }

    assert _is_new_bond(row) is False
    assert _resolve_row_tag(row) is None


def test_future_tradable_bond_is_marked_new():
    row = {
        "bond_code": "123999.SZ",
        "status": "ok",
        "is_tradable": False,
        "trading_status": "pending",
        "listing_date": market_today() + timedelta(days=2),
        "tradable_date": market_today() + timedelta(days=2),
    }

    assert _is_new_bond(row) is True
    assert _resolve_row_tag(row) == "new"


def test_issued_but_unlisted_bond_is_marked_new_after_pricing():
    """已发行未上市的新债定价成功后仍要保留新债高亮 (状态按估值日重算)."""
    row = {
        "bond_code": "123284.SZ",
        "status": "ok",
        "is_tradable": False,
        "trading_status": "pending",
        "listing_date": None,
        "tradable_date": None,
        "theoretical_price": 130.35,
    }

    assert _is_new_bond(row) is True
    assert _resolve_row_tag(row) == "new"


def test_watchlist_cells_show_pending_listing_as_undetermined():
    """上市日未公告 → 显示"待定"而不是"—", 后者读起来像缺数据."""
    entry = {
        "bond_code": "123284.SZ",
        "trading_status": "pending",
        "listing_date": None,
        "tradable_date": None,
        "days_to_trade": 3,  # 上一轮扫描留下的旧值, 不能显示
    }

    assert _format_listing_cell(entry, "listing_date") == "待定"
    assert _format_listing_cell(entry, "tradable_date") == "待定"
    assert _format_days_to_trade(entry) == "待定"


def test_watchlist_cells_keep_dash_for_plain_missing_dates():
    entry = {"bond_code": "128009.SZ", "trading_status": "tradable"}

    assert _format_listing_cell(entry, "listing_date") == "—"
    assert _format_days_to_trade(entry) == "—"


def test_watchlist_days_to_trade_uses_known_listing_date():
    entry = {
        "bond_code": "123281.SZ",
        "trading_status": "pending",
        "listing_date": market_today() + timedelta(days=4),
        "tradable_date": market_today() + timedelta(days=4),
    }

    assert _format_listing_cell(entry, "tradable_date") == (market_today() + timedelta(days=4)).isoformat()
    assert _format_days_to_trade(entry) == "+4"


def test_days_to_trade_shows_already_tradable_instead_of_negative():
    """"距交易 -3" 没有意义 — 可交易日已过就是能买了."""
    entry = {
        "bond_code": "123284.SZ",
        "trading_status": "tradable",
        "tradable_date": market_today() - timedelta(days=3),
    }
    assert _format_days_to_trade(entry) == "已可交易"

    today = {"bond_code": "123284.SZ", "tradable_date": market_today()}
    assert _format_days_to_trade(today) == "已可交易"

    stale_days = {"bond_code": "123284.SZ", "days_to_trade": -3}
    assert _format_days_to_trade(stale_days) == "已可交易"


# ── 批量页列 / 定价参数的静态守护 ──
#
# GUI 在测试环境跑不起真实渲染, 但这些是纯数据结构与源码常量, 可以机器兜底。

def test_every_batch_column_has_a_getter_and_a_stretch_weight():
    """列表加了列却忘了 getter, 运行期才 KeyError; F821 也扫不到 (是字典键不是名字)。"""
    for preset in (batch_tab._BATCH_COLS_FULL, batch_tab._BATCH_COLS_SIMPLE):
        for name, _width in preset:
            assert name in batch_tab._BATCH_COL_GETTERS, f"{name} 缺 getter"
            assert name in batch_tab._BATCH_COL_STRETCH_WEIGHTS, f"{name} 缺列宽权重"


def test_batch_column_getters_tolerate_missing_and_nan_values():
    """定价失败行 / 旧缓存行不带新字段, 取值函数必须退化成 '—' 而不是抛异常。"""
    import math
    for row in ({}, {"status": "error"},
                {"status": "ok", "relative_deviation": math.nan,
                 "double_low": None}):
        for name, getter in batch_tab._BATCH_COL_GETTERS.items():
            assert isinstance(getter(row), str), f"{name} 在缺值行上没返回字符串"


def test_event_and_trigger_columns_are_present():
    """确定性的日程安排此前全算好了却一个都不显示 —— 强赎日、在途下修、回售窗口。"""
    simple = [name for name, _ in batch_tab._BATCH_COLS_SIMPLE]
    full = [name for name, _ in batch_tab._BATCH_COLS_FULL]
    assert "事件" in simple, "事件是最该被看见的一类, 不能只在完整视图里"
    assert "正股/下修线" in full


def test_event_column_never_truncates():
    """tooltip 取的是单元格 display value, 一旦截断被隐藏的那条就彻底看不见了。"""
    row = {"event_flags": ["强赎 08-27", "下修提议 09-05", "暂停转股", "不强赎至 27-01"]}
    rendered = batch_tab._BATCH_COL_GETTERS["事件"](row)
    for flag in row["event_flags"]:
        assert flag in rendered


def test_simple_preset_is_the_decision_view_not_a_diagnostic_one():
    """简洁 = 决策位。四处刻意的取舍, 每一处都有实测依据 (见预设上方注释)。"""
    simple = [name for name, _ in batch_tab._BATCH_COLS_SIMPLE]
    full = [name for name, _ in batch_tab._BATCH_COLS_FULL]

    # 两个偏差都在简洁里 (按用户决策): 「偏差(%)」跟模型比, 「相对偏差(pp)」跟市场比
    assert "相对偏差(pp)" in simple and "偏差(%)" in simple

    # 「正股/下修线」是唯一的下修相关列 (284/284 有值)。「稳健优势(元)」曾在这个位置
    # 竞争, 已随隐含下修强度反解整体删除 —— 反解在两个 regime 都无解 (谷底
    # 市价 < price(λ=0)、高位 市价 > price(λ=3))。
    assert "正股/下修线" in simple
    assert "稳健优势(元)" not in simple and "稳健优势(元)" not in full

    # 诊断项只进完整: 理论价可信度近乎常量 (实测全池 高 219 / 中 64 / 低 1);
    # 定价状态实测恒 ✓
    for diagnostic in ("可信度", "定价状态", "转股价值", "正股σ(%)"):
        assert diagnostic not in simple, f"{diagnostic} 不该占决策位"
        assert diagnostic in full

    # 挑债时"这是哪家公司"是一等信息 —— 简洁此前完全没有正股
    assert "正股" in simple
    assert len(simple) <= 13, f"简洁预设膨胀到 {len(simple)} 列了"


def test_header_help_stays_short_and_has_no_orphans():
    """表头说明的**结构性**守护 —— 覆盖哪几列是编辑决定, 不在这里钉.

    这条曾经是"每列都要有说明", 那正是**为了写 tooltip 而写 tooltip**: 给「评级」凑
    一句"债项信用评级"、给「市价」凑一句"转债最新收盘价", 读者多读一次什么也没多知道,
    而真正需要解释的那几条被淹在里面。后来又反过来钉"这 11 列不许有" —— 同样是把
    一次编辑取舍固化成规则, 人改主意就红。

    所以现在只钉三件跑不掉的:
      · 无孤儿键 (删了列却留着说明, 不报错也永远不显示)
      · 够短 (一句话说清怎么读)
      · 名字里不留方向注释 / 单位留在名字里 (与 COLUMN_HELP 的分工)
    """
    headers = set()
    for preset in (batch_tab._BATCH_COLS_FULL, batch_tab._BATCH_COLS_SIMPLE):
        headers |= {name for name, _ in preset}
    headers |= set(watchlist_tab.watchlist_columns()[0])
    help_map = batch_common.COLUMN_HELP

    assert help_map, "整表空了 —— 大概是回写脚本出错"
    orphans = set(help_map) - headers
    assert not orphans, f"说明表里有孤儿键 (列已删): {sorted(orphans)}"

    texts = list(help_map.values())
    avg = sum(len(t) for t in texts) / len(texts)
    assert avg <= 36, f"表头说明平均 {avg:.0f} 字, 太啰嗦了"
    for name, text in help_map.items():
        assert text.strip(), f"「{name}」是空说明 —— 不写就别留键"
        assert len(text) <= 110, f"{name} 的说明 {len(text)} 字"
        assert text.count("\n") <= 2, f"{name} 的说明超过 3 行"

    # 名字里不该留方向注释 —— 那是悬浮的活
    for name in headers:
        assert "便宜" not in name and "=" not in name, f"{name} 的方向注释该进 tooltip"
    # 但单位必须留在名字里
    for unit_col in ("相对偏差(pp)", "偏差(%)", "剩余(年)", "余额(亿)"):
        assert unit_col in headers

    # 表头这一路真的接上了 —— 老实现在表头区 identify_row 返回 "" 就直接 _hide 了
    src = inspect.getsource(batch_common._attach_cell_tooltip)
    assert "identify_region" in src and '"heading"' in src


def test_column_names_name_their_object():
    """列名要说清**度量的是谁** —— 光换个词不算数.

    「可信」「敏感性」「状态」的毛病不是用词不好, 是**没有对象**: 可信的是什么?
    什么的稳健性? 谁的状态? 三处都补上了主语:

    - `可信` → **`理论价可信度`**: ``confidence_points`` 的扣分项全在削弱那一个数
      (数据缺口 −25 算不出 parity / 无偏差 −20 / 无 HV −20 / 高 HV 按 σ 递增 /
      模型溢价高 −12 / 余额清零 −35), 而 ``model_signal_status`` 也由它推出。
    - `状态` → **`定价状态`**: 对象是这一行的定价计算。刻意**不叫「数据」** ——
      关注池那一列已经叫「数据」而语义完全不同 (7 档取价新鲜度), 同名异概念更糟。
    - `距下修线` → **`正股距下修线`**: 触发线是拿**正股价**比的, 而值写「线上 32%」
      时"谁在线上"正是要点。两页同步改。
    - `σ(%)` → **`正股σ(%)`**: 是正股的波动率, 不是转债的。

    (`敏感性` 没有改名而是**删掉了** —— 见
    ``test_derivable_columns_are_off_the_batch_table``。)
    """
    full = [name for name, _ in batch_tab._BATCH_COLS_FULL]
    simple = [name for name, _ in batch_tab._BATCH_COLS_SIMPLE]
    for vague in ("可信", "置信度", "状态", "定价", "距下修线", "σ(%)", "下修优势",
                  "敏感性", "稳健性"):
        assert vague not in full and vague not in simple, f"{vague} 没有点明对象"
        assert vague not in batch_tab._BATCH_COL_GETTERS
        assert vague not in batch_tab._BATCH_COL_STRETCH_WEIGHTS
    for named in ("定价状态", "正股/下修线", "正股σ(%)"):
        assert named in full
    assert "正股/下修线" in simple

    # **对象也可以由列序承载**。「可信度」不写主语, 是因为它紧跟在「理论价」右边 ——
    # 邻接就是主语。挪走它就必须把名字改回「理论价可信度」, 否则紧挨「评级」时最容易
    # 被读成信用相关。同一条约定管着关注池的「数据」(紧跟「市价」)。
    assert full[full.index("可信度") - 1] == "理论价", "「可信度」必须紧跟「理论价」"

    # **关注池的「数据」不靠邻接** —— 它按用户决策放在末尾 (与「加入日」同组)。
    # 可以这么放是因为七档里有四档说的是**整行** (未定价 / 失败 / 未重算 MM-DD /
    # 无市价), 只有三档专指市价的 as-of; 而「可信度」是纯粹关于「理论价」那一个数的,
    # 离开它就必须把主语写回名字 (曾叫「理论价可信度」)。
    wl = watchlist_tab.watchlist_columns()[0]
    assert wl[wl.index("数据状态") + 1] == "加入日"
    # 「定价状态」不靠邻接 —— 对象在名字里, 所以它可以放到末尾与事件/标签同组
    assert "定价状态" == full[-1]


def test_base_terms_come_after_price_and_events():
    """基础条款排在**后面** —— 盯一只债的次序是: 多少钱 → 便宜吗 → 有什么事 → 条款.

    「剩余(年)」「评级」「余额(亿)」这些是回过头去核对的东西, 不是第一眼要扫的;
    放在前面会把价格块整体推到右边。

    **「上市日」在关注池是例外**: 未上市新债右半边整片是「—」(市价/偏差/双低全空),
    而"还有几天挂牌"恰是它们仅有的可操作信息, 所以那一页把它留在左块。
    """
    for preset in (batch_tab._BATCH_COLS_FULL, batch_tab._BATCH_COLS_SIMPLE):
        cols = [name for name, _ in preset]
        price_end = max(cols.index(c) for c in ("市价", "理论价") if c in cols)
        events = min(cols.index(c) for c in ("事件", "标签") if c in cols)
        terms = [cols.index(c) for c in ("剩余(年)", "评级", "余额(亿)", "上市日")
                 if c in cols]
        assert price_end < events, "价格块要排在事件之前"
        assert events < min(terms), (
            f"基础条款跑到事件前面了: {[cols[i] for i in sorted(terms)]}")

    wl = watchlist_tab.watchlist_columns()[0]
    assert wl.index("事件") < min(wl.index("剩余(年)"), wl.index("评级"))
    # 关注池的例外, 连理由一起钉住
    assert wl.index("上市日") < wl.index("市价"), "未上市新债只有左块有值"


def test_column_alignment_matches_between_header_and_content():
    """表头与内容**必须同向**, 且数值列右对齐.

    此前 ``_configure_responsive_columns`` 写死 ``anchor="w"``, 表头走 ttk 默认
    (居中) —— 于是短表头居中而值靠左, 整列看着是错位的; 数值列还因为左对齐而
    **对不上小数点**, 而这几列的全部用途就是比大小。
    """
    numeric = {"市价", "理论价", "转股价值", "双低", "偏差(%)", "相对偏差(pp)",
               "转股溢价(%)", "正股σ(%)", "正股/下修线",
               "剩余(年)", "余额(亿)"}
    text = {"代码", "名称", "正股", "事件", "标签", "上市日", "加入日", "数据状态"}
    for name in numeric:
        assert batch_common.column_align(name) == "e", f"{name} 该右对齐"
    for name in text:
        assert batch_common.column_align(name) == "w", f"{name} 该左对齐"
    for name in ("可信度", "定价状态", "评级"):
        assert batch_common.column_align(name) == "center"
    assert batch_common.column_align("没登记的列") == "w", "默认左对齐"

    # 表头与内容用同一个值 —— 分两处写就会分叉
    src = inspect.getsource(batch_common._configure_responsive_columns)
    assert "align = column_align(header)" in src
    assert "anchor=align" in src.split("tree.heading(")[1]
    assert "anchor=align" in src.split("tree.column(")[1]


def test_right_aligned_cells_get_a_gutter():
    """右对齐列补尾随留白 —— 否则和右边左对齐列的文字**贴在列边界上**.

    ttk 没有 per-cell padding (Treeview.Cell 元素在 aqua 主题下根本不暴露), 而右对齐
    把文字钉在右边缘, **加多少列宽都贴着**。实测关注池的「双低→事件」
    「正股/下修线→标签」两处正是这样。

    留白只加在右对齐那一侧, 不按"下一列是不是左对齐"分情况 —— 后者位置相关, 列序一变
    就得重算; 而所有右对齐列一起右移同样多, 小数点对齐不受影响。
    """
    headers = ["双低", "事件", "相对偏差(pp)", "标签"]
    padded = batch_common.pad_cells(headers, ["224", "—", "-25.9", "较高HV"])
    assert padded == ["224" + batch_common.CELL_GUTTER, "—",
                      "-25.9" + batch_common.CELL_GUTTER, "较高HV"]
    # 表头之间同样会贴 (实测截图里就是「双低 事件」连成一片)
    assert batch_common.heading_text("双低").endswith(batch_common.CELL_GUTTER)
    assert batch_common.heading_text("事件") == "事件"
    assert batch_common.heading_text("双低", " ↑").startswith("双低 ↑")

    # **留白不许参与任何逻辑** —— 排序与缺失值判定都要 strip 掉
    assert batch_common._parse_sortable_number("-25.9" + batch_common.CELL_GUTTER) == -25.9
    assert ("—" + batch_common.CELL_GUTTER).strip() in batch_common._MISSING_TOKENS

    # 两张表插行前都要过这一道
    for mod in (batch_tab, watchlist_tab):
        src = inspect.getsource(mod)
        assert "values=pad_cells(" in src, f"{mod.__name__} 插行没过 pad_cells"


def test_column_widths_fit_the_largest_responsive_font():
    """列宽要按**响应式字号上限**定, 不是基准字号.

    ``_apply_responsive_tree_font`` 会随窗口变宽把字号调到 TABLE_FONT_SIZE+3, 而列宽
    是写死的 —— 实测 2000px 宽的窗口下「正股/下修线」「相对偏差(pp)」「转股溢价(%)」
    三个表头全被截断 (截图里读到的是「正股/」)。
    """
    import tkinter.font as tkfont
    from convertible_bond.gui.theme import FONT_FAMILY, TABLE_FONT_SIZE
    try:
        font = tkfont.Font(family=FONT_FAMILY, size=TABLE_FONT_SIZE + 3, weight="bold")
    except Exception:                                    # 无显示环境
        pytest.skip("需要 Tk 显示环境才能量字体")
    presets = [list(batch_tab._BATCH_COLS_FULL), list(batch_tab._BATCH_COLS_SIMPLE),
               list(zip(*watchlist_tab.watchlist_columns()))]
    for preset in presets:
        for header, width in preset:
            need = font.measure(batch_common.heading_text(header)) + 6  # ttk Heading padding
            assert need <= width, f"「{header}」表头 {need}px 放不进 {width}px"


def test_deviation_columns_share_a_decimal_precision():
    """「偏差(%)」与「相对偏差(pp)」挨着放, 小数位必须一样.

    两列只差一个常数 (全市场当期中位), 一个 2 位一个 1 位时右对齐之后那一位是
    空的, 很扎眼。0.1pp 的分辨率对筛选足够。两页同口径。
    """
    row = {"status": "ok", "deviation": -0.0503, "relative_deviation": -0.259}
    dev = batch_tab._BATCH_COL_GETTERS["偏差(%)"](row)
    rel = batch_tab._BATCH_COL_GETTERS["相对偏差(pp)"](row)
    assert dev == "-5.0" and rel == "-25.9"
    assert len(dev.split(".")[1]) == len(rel.split(".")[1]) == 1
    # 关注池那一格是 vals 里的字面表达式, 用源码比对
    src = inspect.getsource(watchlist_tab._render_watchlist_table)
    assert "{float(dev) * 100:+.1f}" in src, "关注池的偏差也要 1 位小数"


def test_price_columns_stay_in_one_block():
    """价格块连成一片, 中间不许插别的列.

    读者比的就是 转股价值 / 转股溢价 / 市价 / 理论价 / 可信度 / 偏差 这几个数, 中间隔
    一列就得来回扫。三条恒等式也因此都落在视线内::

        转股溢价 = 市价 / 转股价值 − 1        偏差 = 市价 / 理论价 − 1

    「双低」「正股σ(%)」「定价状态」此前正好把这一片切成三段, 已挪出去 —— 它们分别是
    复合指标 / 模型入参 / 行状态, 都不是"这只债值多少钱"的直读。
    """
    full = [name for name, _ in batch_tab._BATCH_COLS_FULL]
    block = ["转股价值", "转股溢价(%)", "市价", "理论价", "可信度",
             "偏差(%)", "相对偏差(pp)"]
    idx = [full.index(name) for name in block]
    assert idx == list(range(idx[0], idx[0] + len(block))), (
        f"价格块被切断了: {[full[i] for i in range(idx[0], idx[-1] + 1)]}")

    # **两个偏差必须相邻**: 它们只差一个常数 (全市场当期中位, 实测 +20.86pp),
    # 并排才看得出"本券贵不贵"与"相对全市场贵不贵"是不是同向。
    for preset in (batch_tab._BATCH_COLS_FULL, batch_tab._BATCH_COLS_SIMPLE):
        cols = [name for name, _ in preset]
        assert cols[cols.index("偏差(%)") + 1] == "相对偏差(pp)"
    wl = watchlist_tab.watchlist_columns()[0]
    assert wl[wl.index("偏差(%)") + 1] == "相对偏差(pp)"

    # 简洁只留两端, 也要挨着
    simple = [name for name, _ in batch_tab._BATCH_COLS_SIMPLE]
    assert simple[simple.index("市价"):simple.index("市价") + 3] == ["市价", "理论价", "偏差(%)"]

    # 关注池同理: 市价 → 理论价 → 偏差 → 相对偏差, 中间不插别的
    wl = watchlist_tab.watchlist_columns()[0]
    i = wl.index("市价")
    assert wl[i:i + 4] == ["市价", "理论价", "偏差(%)", "相对偏差(pp)"]

    # 关注池同步 —— 两页共用的列名不许分叉
    wl_headers, _ = watchlist_tab.watchlist_columns()
    assert "正股/下修线" in wl_headers and "距下修线" not in wl_headers
    assert set(wl_headers) == set(watchlist_tab._WATCHLIST_COL_STRETCH_WEIGHTS)

    # 底层字段没动, 只是表头换了
    row = {"status": "ok", "confidence": "高", "sigma": 0.375}
    assert batch_tab._BATCH_COL_GETTERS["可信度"](row) == "高"
    assert batch_tab._BATCH_COL_GETTERS["正股σ(%)"](row) == "37.5"
    assert batch_tab._BATCH_COL_GETTERS["定价状态"](row) == "✓"
    assert batch_tab._BATCH_COL_GETTERS["定价状态"]({"status": "timeout"}) == "timeout"
    # 两页不许同名异概念
    assert "数据状态" not in full


def test_derivable_columns_are_off_the_batch_table():
    """三列被砍掉是因为**算术上完全可推导**, 实测各 0 例外.

    - 「质量分」= f(评级, 余额≥10亿): 8 个取值 = 5 个评级档 × 2 个余额档, 而评级与
      余额都是表上的列。
    - 「复核建议」= f(标签): 38 个标签组合 ↔ 38 个 (标签,建议) 组合, 同一标签组合
      从不对应多种建议; 而它占 260px, 是完整预设里最宽的一列。
    - 「正股码」: 与「正股」同一个东西 —— 正股列已改渲染名称 (缺名字才回落代码)。
    - 「敏感性」= f(标签, 置信度): ``_sensitivity_status(risk_tags, confidence)`` 逐字
      重算, **0/284 不一致**; 判据是"标签含 {高HV, 模型溢价高} → 波动率敏感; 标签含
      {余额清零/摘牌线/小余额/短久期/低评级/停牌...} → 条款·流动性敏感; 否则按置信度
      高/中/低 → 较稳健/一般/需复核"。标签与理论价可信度**都在表上**, 它是纯二次展开。

    「转股溢价(%)」「双低」「偏差(%)」同样可推导却**保留**: 它们是研究口径, 读者不该
    去心算 168.19/108.62−1。判据是"读者要不要这个数", 不是"能不能算出来"。
    """
    full = [name for name, _ in batch_tab._BATCH_COLS_FULL]
    for gone in ("质量分", "复核建议", "正股码", "敏感性", "稳健性"):
        assert gone not in full
        assert gone not in batch_tab._BATCH_COL_GETTERS
        assert gone not in batch_tab._BATCH_COL_STRETCH_WEIGHTS
    for kept in ("转股溢价(%)", "双低", "偏差(%)", "评级", "余额(亿)"):
        assert kept in full
    # 复核建议不再有列 → 也不该再挂 tooltip
    src = inspect.getsource(batch_tab)
    assert 'tooltip_headers={"标签", "事件"}' in src


def test_opportunity_score_is_gone_everywhere():
    """「机会分」已**整体删除** —— 列 / 字段 / 排序信号 / min_score 门槛全部拿掉.

    实测今日截面的依据:
    - 269/284 (95%) 的行低估项 ``max(0, −deviation)`` 恒为 0, 分数完全由评级/余额加分
      与风险惩罚决定; Spearman(机会分, 质量分) = +0.517, 而 Spearman(机会分, 偏差)
      只有 −0.640 (纯错定价排序应为 −1.0)。
    - 它的非展示消费者当时也已全部不可达: GUI 的排序信号里没有它,
      ``_legacy_score_gate`` 生产代码零调用而全池 **0/283** 够得着它的 8.0 阈值。

    **保留的是 ``quality_score``** —— 它本来就是从机会分里拆出来单独记账的那一支
    (评级档 + 大余额加分), 与错定价无关但对审计有用。
    """
    for preset in (batch_tab._BATCH_COLS_FULL, batch_tab._BATCH_COLS_SIMPLE):
        assert "机会分" not in [name for name, _ in preset]
    assert "机会分" not in batch_tab._BATCH_COL_GETTERS
    assert "机会分" not in batch_tab._BATCH_COL_STRETCH_WEIGHTS

    # 字段侧: 计算 / 存储 / 导出 / 反序列化 / 关注池白名单全都不许再有它
    assert not hasattr(batch_pricing, "_legacy_score_gate")
    assert not hasattr(batch_pricing, "DEFAULT_UNDERVALUED_SCORE_THRESHOLD")
    assert "opportunity_score" not in batch_pricing.BATCH_RESULT_COLUMNS
    assert "opportunity_score" not in inspect.getsource(batch_pricing._restore_result_row)
    from convertible_bond import watchlist as watchlist_mod
    from convertible_bond import watchlist_cache as wc
    assert not any("opportunity" in f for f in watchlist_mod._WATCHLIST_SNAPSHOT_FIELDS)
    assert "opportunity_score" not in wc.CACHE_FIELDS

    # 而 ``quality_score`` **字段**必须还在 —— 它是这次删除刻意保留的那一支。
    # (它的**列**后来单独砍掉了, 理由不同: 实测 = f(评级, 余额≥10亿), 0 例外,
    # 而那两列都在表上 —— 见 test_derivable_columns_are_off_the_batch_table。)
    assert "quality_score" in batch_pricing.BATCH_RESULT_COLUMNS

    # 摘要条查**渲染结果**: 不再有机会分, 「平均评级」照旧
    class _Var:
        def __init__(self):
            self.value = ""

        def set(self, v):
            self.value = v

    class _App:
        # 摘要现在写状态行本身 (与批量页同格式), 不再有独立的 summary 变量
        v_watchlist_status = _Var()

    app = _App()
    watchlist_tab._refresh_watchlist_summary(app, [
        {"bond_code": "X", "status": "ok", "deviation": 0.1,
         "credit_rating": "AA", "risk_tags": []},
    ])
    assert app.v_watchlist_status.value
    assert "机会分" not in app.v_watchlist_status.value
    assert "平均评级" in app.v_watchlist_status.value



# ── 事件横幅 ──
#
# 此前只扫关注池 —— 主池里昨天出的下修提议不会浮出来, 除非你已经在关注它。
# 而"已经在关注"恰恰意味着你已经知道了。

@dataclass
class _Ev:
    bond_code: str
    event_type: str
    event_date: date
    effective_start: date | None = None
    effective_end: date | None = None


class _Store:
    def __init__(self, events):
        self._by_code = {}
        for ev in events:
            self._by_code.setdefault(ev.bond_code, []).append(ev)

    def list_events(self, bond_code=None):
        return list(self._by_code.get(bond_code, []))


TODAY = date(2026, 8, 24)
HORIZON = TODAY + timedelta(days=30)


def _collect(events, codes=None):
    from convertible_bond.gui.tabs.batch_watchlist import collect_upcoming_events
    store = _Store(events)
    return collect_upcoming_events(
        store, codes or sorted({e.bond_code for e in events}), TODAY, HORIZON)


def test_banner_shows_the_date_that_actually_falls_in_the_window():
    """入窗判定看三个日期中任意一个, 显示的却固定是 effective_start ——
    于是 effective_end 在窗口内的区间事件, 会把几个月前的起始日当成"未来 30 天的事"。
    """
    rows = _collect([_Ev("A.SH", "call_no_redemption", date(2026, 3, 1),
                         effective_start=date(2026, 3, 1), effective_end=date(2026, 8, 26))])
    assert rows == [("A.SH", "不强赎到期", date(2026, 8, 26))]


def test_banner_ignores_events_entirely_outside_the_window():
    assert _collect([_Ev("A.SH", "rating_change", date(2025, 1, 1))]) == []
    assert _collect([_Ev("A.SH", "rating_change", date(2027, 1, 1))]) == []


def test_banner_ignores_untrustworthy_end_dates():
    """conversion_suspension 的 end 被公告里的回售期区间污染, 不能当"未来事件"。"""
    contaminated = _Ev("A.SH", "conversion_suspension", date(2024, 10, 25),
                       effective_start=date(2021, 3, 11), effective_end=date(2026, 9, 3))
    assert _collect([contaminated]) == []


def test_banner_dedupes_repeat_announcements():
    """同一件事常有"第N次提示性公告"多条 (实测鸿路转债 33 条 putback)。"""
    window = dict(effective_start=date(2026, 8, 25), effective_end=date(2026, 8, 31))
    rows = _collect([_Ev("A.SH", "putback", date(2026, 8, 20), **window),
                     _Ev("A.SH", "putback", date(2026, 8, 21), **window),
                     _Ev("A.SH", "putback", date(2026, 8, 22), **window)])
    assert rows == [("A.SH", "回售", date(2026, 8, 25))]


def test_banner_orders_by_actionability_not_by_date():
    """纯按日期排会让"评级调整"挤掉三天后的强赎。"""
    rows = _collect([
        _Ev("R.SH", "rating_change", date(2026, 8, 25)),
        _Ev("C.SH", "call_redemption", date(2026, 8, 28)),
        _Ev("D.SH", "down_reset_proposed", date(2026, 8, 27)),
    ])
    assert [code for code, _t, _d in rows] == ["C.SH", "D.SH", "R.SH"]


def test_banner_groups_repeats_so_the_urgent_one_survives():
    """扫全主池后同类成片 (实测 22 件里 11 件是「不下修到期」), 逐条铺开会占满展示位。"""
    from convertible_bond.gui.tabs.batch_watchlist import _group_banner_entries
    upcoming = [("C.SH", "强赎截止", date(2026, 8, 27))]
    upcoming += [(f"N{i}.SH", "不下修到期", date(2026, 8, 25) + timedelta(days=i))
                 for i in range(11)]
    parts = _group_banner_entries(upcoming, {"C.SH": "应流转债"})
    assert parts[0] == "应流转债 强赎截止 (08-27)"
    assert parts[1] == "不下修到期 x11 (最早 08-25)"
    assert len(parts) == 2                       # 12 条压成 2 段, 紧急那条不会被挤掉


def test_banner_only_covers_the_watchlist():
    """**横幅只讲关注池的标的** (按用户决策), 池外整段已删除.

    此前末尾挂着一句「全池另有 N 件 (单击查看全部)」, 理由是"横幅真正的用处是告诉你
    **还不知道的那些**"。那条理由被推翻了 —— 这是**我的**关注池, 池外的债在别的页面
    找。连带 ``_pool_scan_codes`` 一并删掉, 不留孤儿。
    """
    import inspect

    from convertible_bond.gui.tabs.batch_watchlist import _watchlist_scan_codes

    class _App:
        _batch_watchlist = [{"bond_code": "W.SH"}]
        _batch_all_results = [{"bond_code": "M1.SH"}, {"bond_code": "M2.SH"}, {}]

    assert _watchlist_scan_codes(_App()) == {"W.SH"}
    assert not hasattr(watchlist_tab, "_pool_scan_codes")
    # 查**代码标识符**而不是中文词 —— docstring 里会写"此前挂过全池另有 N 件"当背景。
    # 渲染结果由 test_banner_ignores_bonds_outside_the_watchlist 行为验证。
    src = inspect.getsource(watchlist_tab._refresh_events_banner)
    assert "pool_extra" not in src, "横幅里还在算池外的债"


def test_banner_tone_follows_content():
    """**有事件才用警报色**。空态仍显式写文案, 但不再是橙色 ⚠.

    实测这个关注池 (5 只) 在 7/14/30/60/90/180 天**每个窗口都是 0 件** —— 一条橙色
    警告条常年说"什么都没发生", 训练出来的行为是忽略它, 而那正是真事件出现时会被
    漏掉的原因。空态**不 grid_remove**: 消失的控件和坏掉的控件长得一模一样。
    """
    import inspect

    from convertible_bond.gui import theme

    src = inspect.getsource(watchlist_tab._refresh_events_banner)
    assert "_set_banner_tone(label, alert=True)" in src
    assert "_set_banner_tone(label, alert=False)" in src
    assert "label.grid()" in src
    assert "label.grid_remove()" not in src, "空态不许藏起控件"

    class _Label:
        def __init__(self):
            self.color = None

        def configure(self, **kw):
            self.color = kw.get("text_color")

    alert, quiet = _Label(), _Label()
    watchlist_tab._set_banner_tone(alert, alert=True)
    watchlist_tab._set_banner_tone(quiet, alert=False)
    assert alert.color == theme.ORANGE
    assert quiet.color == theme.TEXT_DIM
    # 假 label 不该把渲染打断
    watchlist_tab._set_banner_tone(object(), alert=True)


# ── 扫新债: 窄同步 → 扫描 ──
#
# 原本这条路是"读 bundle_meta()['updated_at'] 判新鲜度 → 提示跑 cb-sync-tradable --incremental"。
# 三处同时失效: updated_at 被任何一次写盘推到今天 (提示永不弹出); 就算弹出, 增量同步按
# 7 天新鲜度**恰好跳过**刚被抓过的新债; 没装 WindPy 连提示都不给。详见
# convertible_bond/new_issue_sync.py。

class _FakeStatus:
    def __init__(self):
        self.value = ""
        self.history = []

    def set(self, value):
        self.value = value
        self.history.append(value)

    def get(self):
        return self.value


class _FakeApp:
    """够跑通同步→回调这条链的最小 app: after 同步执行, 线程 join 掉."""

    def __init__(self):
        # 两页各有自己的状态行 —— 关注池的动作写 v_watchlist_status,
        # 批量页的 (含「⭐ 加入关注池」) 写 v_batch_status。见 app._build_vars。
        self.v_batch_status = _FakeStatus()
        self.v_watchlist_status = _FakeStatus()
        self.pool_syncs = []

    def after(self, _delay, fn):
        fn()

    def _run_pool_sync(self, module, label, extra_args=(), **kwargs):
        self.pool_syncs.append((module, extra_args))


def _run_sync_to_completion(monkeypatch, app, *, sync_result=None, exc=None, **kwargs):
    import convertible_bond.new_issue_sync as new_issue_sync

    def fake_sync(*_a, **_kw):
        if exc is not None:
            raise exc
        return sync_result or {"changes": []}

    monkeypatch.setattr(new_issue_sync, "sync_new_issues", fake_sync)
    seen = []
    real_thread = watchlist_tab.threading.Thread

    def blocking_thread(*args, **thread_kwargs):
        thread = real_thread(*args, **thread_kwargs)
        thread.start()
        thread.join(timeout=5)
        return _NoopThread()

    monkeypatch.setattr(watchlist_tab.threading, "Thread", blocking_thread)
    watchlist_tab.run_new_issue_sync_async(app, then=seen.append, **kwargs)
    return seen


class _NoopThread:
    def start(self):
        pass


def test_scan_new_issues_no_longer_asks_before_syncing(monkeypatch):
    """窄同步只碰那几只新债 (秒级, 不需要 Wind), 所以直接做, 不再问用户."""
    app = _FakeApp()
    seen = _run_sync_to_completion(
        monkeypatch, app,
        sync_result={"changes": [{"bond_code": "118076.SH", "kind": "listing_date"}]})

    assert seen == [True]                    # 后续流程照常触发
    assert app.pool_syncs == []              # 没有弹窗, 也没有退回全库增量同步
    assert any("新债上市日" in text for text in app.v_watchlist_status.history)


def test_scan_continues_when_the_narrow_sync_fails(monkeypatch):
    """取数失败不能阻断扫描 —— 退回按现有条款库继续, 状态栏说明原因."""
    app = _FakeApp()
    seen = _run_sync_to_completion(monkeypatch, app, exc=RuntimeError("网络不通"))

    assert seen == [False]
    assert "网络不通" in app.v_watchlist_status.value


def test_concurrent_scan_requests_are_dropped(monkeypatch):
    """「扫新债」与「批量重算」共用这条路径, 同步的这两秒里两个按钮都还能点."""
    app = _FakeApp()
    app._new_issue_sync_running = True
    seen = _run_sync_to_completion(monkeypatch, app)

    assert seen == []


def test_batch_rerun_refreshes_listing_dates_first():
    """批量重算前也要刷一次: 准入读的是 cb_data 的 listing_date, 不刷就把昨天挂牌的新债判死."""
    src = inspect.getsource(batch_tab._run_batch)
    assert "run_new_issue_sync_async" in src


# ── 关注池取价的口径 ──
#
# `_batch_results` 是**视图过滤后**的子表 (见 _render_batch_views), `_batch_all_results` 才是全池。
# 关注的债多半不在「低估候选」这类窄视图里 —— 读错变量, 关注池就整行显示「—」, 且理论价
# 随主表视图开关忽有忽无。实测视图 40/284 只, 中仑/派克/先锋三只在池内定价成功却都不在视图中。

def _watchlist_app(*, all_results, view_results, upcoming=(), watchlist=()):
    app = _FakeApp()
    app._batch_all_results = list(all_results)
    app._batch_results = list(view_results)
    app._batch_upcoming_results = list(upcoming)
    app._batch_watchlist = [dict(row) for row in watchlist]
    return app


def test_watchlist_price_survives_a_narrow_main_view():
    """主表切到窄视图时, 关注池的理论价不能跟着消失."""
    priced = {"bond_code": "123281.SZ", "bond_name": "中仑转债",
              "status": "ok", "theoretical_price": 110.78}
    app = _watchlist_app(
        all_results=[priced],
        view_results=[],                       # 「低估候选」视图里没有它
        watchlist=[{"bond_code": "123281.SZ", "bond_name": "中仑转债"}],
    )

    row = watchlist_tab._watchlist_display_rows(app)[0]

    assert row["theoretical_price"] == 110.78
    assert row["status"] == "ok"


def test_watchlist_repricing_of_main_pool_bonds_reaches_the_table():
    """⚡关注池重算 把主池标的写进 _batch_all_results —— 展示层必须读得到.

    读 `_batch_results` 时这条路是死的: 状态栏报"主表 N / 关注 M", 而表里只有走
    `_batch_upcoming_results` 的那 M 只出得来价, 主表那 N 只点多少次都是「—」。
    """
    app = _watchlist_app(
        all_results=[{"bond_code": "111026.SH", "status": "ok", "theoretical_price": 108.69}],
        view_results=[],
        upcoming=[{"bond_code": "123284.SZ", "status": "ok", "theoretical_price": 128.93}],
        watchlist=[{"bond_code": "111026.SH"}, {"bond_code": "123284.SZ"}],
    )

    priced = {row["bond_code"]: row.get("theoretical_price")
              for row in watchlist_tab._watchlist_display_rows(app)}

    assert priced == {"111026.SH": 108.69, "123284.SZ": 128.93}


# ── 新债没价时的自愈 ──
#
# 新债不在主池 (剔除原因「已发行未上市」), 理论价只能来自 upcoming_results。那一格一旦
# 没跑到就再没有自愈路径: 启动时 _load_result_cache 只把缓存里的空列表读回来, 行一直空着。

def test_unpriced_new_bonds_are_picked_up_for_repricing():
    app = _watchlist_app(
        all_results=[{"bond_code": "128044.SZ", "status": "ok", "theoretical_price": 105.0}],
        view_results=[],
        watchlist=[
            {"bond_code": "128044.SZ", "is_tradable": True, "trading_status": "tradable"},
            {"bond_code": "123284.SZ", "is_tradable": False, "trading_status": "pending"},
        ],
    )

    assert watchlist_tab.unpriced_new_bond_codes(app) == ["123284.SZ"]


def test_priced_new_bond_is_not_repriced_again():
    """已经有价的新债不再补枪 —— 否则每次加载缓存都白跑一轮."""
    app = _watchlist_app(
        all_results=[],
        view_results=[],
        upcoming=[{"bond_code": "123284.SZ", "status": "ok", "theoretical_price": 128.93}],
        watchlist=[{"bond_code": "123284.SZ", "is_tradable": False, "trading_status": "pending"}],
    )

    assert watchlist_tab.unpriced_new_bond_codes(app) == []


def test_cache_load_repairs_stale_and_missing_prices():
    """启动加载缓存后要补一轮.

    判据已从"是不是没价的新债"放宽成 `_price_state != "ok"` —— 隔夜的旧价、
    上一轮失败的行原本没有任何人管。
    """
    src = inspect.getsource(batch_tab._load_result_cache)
    assert "refresh_stale_watchlist" in src
    # 这一轮不是用户发起的 (启动 80ms 后自动跑): 失败只写状态栏, 不许糊一个模态错误框
    assert "quiet=True" in src


def test_watchlist_pricing_is_single_flight():
    """三个入口 (⚡重算 / 扫新债 / 缓存加载补价) 并发跑会互相覆盖 new_upcoming."""
    app = _watchlist_app(all_results=[], view_results=[],
                         watchlist=[{"bond_code": "123284.SZ", "trading_status": "pending"}])
    app._watchlist_pricing_running = True

    assert watchlist_tab._start_watchlist_pricing(app, ["123284.SZ"]) is False
    assert watchlist_tab.price_unpriced_new_bonds(app) == 0


# ── 关注池定价合并与降级守卫 (S4) ────────────────────────────────

def _wl_row(code, status="ok", price=110.0):
    return {"bond_code": code, "status": status, "theoretical_price": price,
            "market_price": 108.0, "valuation_date": "2026-08-26"}


def test_merge_only_ok_rows_overwrite_existing_good_rows():
    """一次取数失败不该把内存里昨天算好的行换成 nan 行."""
    main = [_wl_row("A", price=100.0)]
    upcoming = [_wl_row("B", price=200.0)]
    fresh = [_wl_row("A", status="failed", price=float("nan")),
             _wl_row("B", status="failed", price=float("nan"))]

    new_main, new_upcoming = watchlist_tab.merge_watchlist_pricing(main, upcoming, fresh)
    assert new_main[0]["theoretical_price"] == 100.0
    assert new_upcoming[0]["theoretical_price"] == 200.0


def test_merge_ok_rows_do_overwrite():
    main = [_wl_row("A", price=100.0)]
    new_main, _ = watchlist_tab.merge_watchlist_pricing(
        main, [], [_wl_row("A", price=111.0)])
    assert new_main[0]["theoretical_price"] == 111.0


def test_merge_appends_new_failures_to_upcoming():
    """失败的**在途新债**仍要出现在 upcoming 里.

    新债不进主池, 唯一来路就是 upcoming。顺手加一句
    `if status != "ok": continue` 会让它彻底消失 —— 于是"取价失败"和
    "我根本没关注它"变成同一种表现。
    """
    _, new_upcoming = watchlist_tab.merge_watchlist_pricing(
        [], [], [_wl_row("NEW", status="failed")])
    assert [r["bond_code"] for r in new_upcoming] == ["NEW"]
    assert new_upcoming[0]["status"] == "failed"


def test_merge_does_not_mutate_inputs():
    main, upcoming = [_wl_row("A")], [_wl_row("B")]
    watchlist_tab.merge_watchlist_pricing(main, upcoming, [_wl_row("C")])
    assert len(main) == 1 and len(upcoming) == 1


def test_worker_has_zero_success_guard_and_persists():
    """源码守护: 全失败不落盘不覆盖内存, 成功才写 watchlist_cache.

    这两件事都没法在无 Tk 环境跑真 worker 验证, 但它们各自对应一次会静默发生的
    数据损坏, 所以在这里钉住源码形态。
    """
    src = inspect.getsource(watchlist_tab._watchlist_pricing_worker)
    assert 'ok_rows = [r for r in results if r.get("status") == "ok"]' in src
    assert "if not ok_rows:" in src
    assert "save_watchlist_pricing(" in src
    assert "今日取价失败" in src
    # 落盘必须用主池锚, 不能让这几行自算
    assert "cross_section_anchor_from(" in src


# ── 「取到市价了吗」与「算完了吗」是两回事 ──────────────────────────

def _listed(code, market_price=163.19):
    return {"bond_code": code, "status": "ok", "theoretical_price": 110.0,
            "market_price": market_price, "listing_date": "2026-08-24",
            "is_tradable": True, "trading_status": "tradable"}


def _pending(code):
    """未上市新债: 没有市价是天然状态, 不是取数失败."""
    return {"bond_code": code, "status": "ok", "theoretical_price": 128.9,
            "market_price": None, "listing_date": None,
            "is_tradable": False, "trading_status": "pending"}


def test_market_price_coverage_excludes_pending_new_bonds():
    """在途新债不进分母 —— 否则一切正常的那一轮永远报成 2/5."""
    rows = [_listed("A"), _listed("B"), _pending("N1"), _pending("N2"), _pending("N3")]

    expect, got = watchlist_tab.market_price_coverage(rows)

    assert [r["bond_code"] for r in expect] == ["A", "B"]
    assert [r["bond_code"] for r in got] == ["A", "B"]


def test_market_price_coverage_flags_listed_bond_without_price():
    """已上市却没市价 = 转债行情链路挂了, 必须能与"新债本来就没有"分开."""
    rows = [_listed("A"), _listed("B", market_price=None), _pending("N1")]

    expect, got = watchlist_tab.market_price_coverage(rows)

    assert [r["bond_code"] for r in expect] == ["A", "B"]
    assert [r["bond_code"] for r in got] == ["A"]


def test_market_price_coverage_treats_nan_as_missing():
    """市价在 watchlist_cache._NAN_FIELDS 里, 落盘走一圈读回来是 NaN.

    ``NaN is not None`` 为真 —— 用 ``is not None`` 判空会把"没市价"数成"有市价",
    页面上渲染出来的是字面的 "nan"。
    """
    expect, got = watchlist_tab.market_price_coverage([_listed("A", market_price=float("nan"))])

    assert len(expect) == 1 and got == []


def test_worker_reports_market_price_coverage_not_just_status():
    """状态栏必须把"取到市价"单独报出来.

    ``status == "ok"`` 只说明模型算完了: S0/σ 走正股链路, 市价走转债链路, 后者整条
    挂掉时前者照样出理论价 (`_batch_result_from_provider` 缺市价只把 deviation 写 nan,
    status 照样 "ok")。实测 akshare 东财侧连不上时正是这个形状 —— 表里价格那一片
    全是「—」, 而状态栏报「⚡ 已刷新关注池 5/5 只」。
    """
    src = inspect.getsource(watchlist_tab._watchlist_pricing_worker)

    assert "market_price_coverage(ok_rows)" in src
    assert "取到市价" in src, "完成消息必须报市价覆盖率"


def test_worker_bails_out_before_persisting_when_no_market_price_at_all():
    """在市债一只都没取到行情时, 不许落盘.

    热缓存是**整行 upsert** (`save_watchlist_pricing` 里 `merged.update(fresh)`),
    写进去就把昨天那个真实市价换成 NaN (实测 163.19 → nan), 而状态栏说的是成功。
    这与 `if not ok_rows` 那道守卫是同一件事, 只是失败发生在链路更深处。
    """
    src = inspect.getsource(watchlist_tab._watchlist_pricing_worker)

    guard_at = src.index("if expect_price and not with_price:")
    persist_at = src.index("save_watchlist_pricing(")
    assert guard_at < persist_at, "空市价守卫必须排在落盘之前"
    # 守卫体内必须 return, 否则只是多打一行字
    assert "return" in src[guard_at:persist_at]


def test_lock_is_set_immediately_before_thread_start():
    """置位必须紧挨 Thread.start(), 中间不许有会抛的裸控件访问.

    原先顺序是 `_watchlist_pricing_running = True` → `btn.configure(...)` →
    `Thread.start()`。中间那次访问一旦抛, finally 永不执行, 三个入口全被单飞
    检查静默挡死 —— 而检查只 return False, 不写状态、不排队。
    """
    src = inspect.getsource(watchlist_tab._start_watchlist_pricing)
    lock_at = src.index("app._watchlist_pricing_running = True")
    start_at = src.index("threading.Thread(")
    between = src[lock_at:start_at]
    assert "configure(" not in between, f"置位与起线程之间不该有控件访问:\n{between}"
    assert "_start_progress" not in between


def test_quiet_round_does_not_touch_shared_progress():
    """quiet 那一轮 (启动自愈) 不碰全局进度条与按钮.

    _start_progress 没有引用计数, 且 _tick_progress 写的是**全局** v_status ——
    一轮后台自愈会把别的页正在跑的任务的状态文字顶掉, 而 _stop_progress 不还原。
    """
    for fn in (watchlist_tab._start_watchlist_pricing,
               watchlist_tab._watchlist_pricing_worker):
        src = inspect.getsource(fn)
        for lineno, line in enumerate(src.splitlines()):
            if "_start_progress" in line or "_stop_progress" in line:
                # 该行前面必须有 quiet 分支把它挡住
                assert "if not quiet:" in src[:src.index(line)][-400:], (
                    f"{fn.__name__} 第 {lineno} 行的进度条调用没有被 quiet 挡住: {line.strip()}")


def test_watch_button_access_is_defensive():
    """按钮建在 batch.py、消费在 batch_watchlist.py —— 跨文件裸属性访问是定时炸弹."""
    src = inspect.getsource(watchlist_tab)
    assert "app.btn_batch_refresh_watch.configure" not in src
    assert "_set_watch_button_state" in src


# ── 三级取价合并层 (S5) ──────────────────────────────────────────

def _cache(rows):
    return {"meta": {}, "rows": {r["bond_code"]: r for r in rows}}


def test_merge_layer_never_touches_disk(monkeypatch):
    """展示层不许隐式读真实磁盘.

    一旦它会读 data/watchlist_pricing_cache.json, 用例过不过就取决于"你上次开
    GUI 点没点刷新" —— 这正是 sync_cb_events 那批用例踩过的坑 (实测: 一次纯数据
    提交就让套件转红)。读盘只许发生在 load_price_cache_into, 由启动路径显式调。
    """
    def boom(*a, **kw):
        raise AssertionError("展示层读盘了")

    monkeypatch.setattr(watchlist_tab, "load_watchlist_pricing", boom)
    app = _watchlist_app(all_results=[], view_results=[],
                         watchlist=[{"bond_code": "123284.SZ"}])
    rows = watchlist_tab._watchlist_display_rows(app)
    assert rows[0]["_price_state"] == "unpriced"


def test_disk_cache_supplies_price_when_memory_is_empty():
    """开页立刻有数: 没跑过全池时理论价来自磁盘热缓存."""
    app = _watchlist_app(all_results=[], view_results=[],
                         watchlist=[{"bond_code": "123284.SZ"}])
    app._watchlist_price_cache = _cache([
        {"bond_code": "123284.SZ", "status": "ok", "theoretical_price": 128.93,
         "market_price": 120.0, "valuation_date": date(2026, 8, 26)},
    ])
    row = watchlist_tab._watchlist_display_rows(app, today=date(2026, 8, 26))[0]
    assert row["theoretical_price"] == 128.93
    assert row["_price_state"] == "ok"


def test_memory_beats_disk():
    """内存是"这次算的", 磁盘是"上次算的" —— 内存永远压过磁盘."""
    app = _watchlist_app(
        all_results=[{"bond_code": "123284.SZ", "status": "ok",
                      "theoretical_price": 130.0, "market_price": 121.0,
                      "valuation_date": date(2026, 8, 26)}],
        view_results=[], watchlist=[{"bond_code": "123284.SZ"}])
    app._watchlist_price_cache = _cache([
        {"bond_code": "123284.SZ", "status": "ok", "theoretical_price": 128.93,
         "market_price": 120.0, "valuation_date": date(2026, 8, 25)},
    ])
    row = watchlist_tab._watchlist_display_rows(app, today=date(2026, 8, 26))[0]
    assert row["theoretical_price"] == 130.0


def test_stale_unstamped_market_price_cannot_win():
    """watchlist.json 里那个无 as-of 戳的 market_price 不许在"今天没市价"时顶上来.

    实测三只在途新债今天 market_price 全是 None (还没上市), 而 entry 里可能留着
    扫新债时写下的旧价 —— 让它胜出就等于把几天前的价当成今天的。
    """
    app = _watchlist_app(all_results=[], view_results=[],
                         watchlist=[{"bond_code": "123284.SZ", "market_price": 119.9}])
    app._watchlist_price_cache = _cache([
        {"bond_code": "123284.SZ", "status": "ok", "theoretical_price": 128.93,
         "market_price": None, "valuation_date": date(2026, 8, 26)},
    ])
    row = watchlist_tab._watchlist_display_rows(app, today=date(2026, 8, 26))[0]
    assert row["market_price"] is None
    assert row["_price_state"] == "no_market"


def test_entry_price_survives_when_nothing_priced_it():
    """完全没有定价行时 entry 的值仍然显示 —— 有总比空好, 但状态标成 unpriced."""
    app = _watchlist_app(all_results=[], view_results=[],
                         watchlist=[{"bond_code": "123284.SZ", "market_price": 119.9}])
    row = watchlist_tab._watchlist_display_rows(app, today=date(2026, 8, 26))[0]
    assert row["market_price"] == 119.9
    assert row["_price_state"] == "unpriced"


@pytest.mark.parametrize("priced,expected,why", [
    (None, "unpriced", "从没算过"),
    ({"status": "failed"}, "failed", "算了但失败"),
    ({"status": "ok", "market_price": None, "valuation_date": date(2026, 8, 26)},
     "no_market", "算了但数据源没给市价 (118076.SH 那个 case)"),
    ({"status": "ok", "market_price": 108.0, "valuation_date": date(2026, 8, 25)},
     "stale", "昨天的价"),
    ({"status": "ok", "market_price": 108.0, "valuation_date": None},
     "stale", "不知道是哪天的价"),
    ({"status": "ok", "market_price": 108.0, "valuation_date": date(2026, 8, 26)},
     "ok", "今天算的、有市价"),
])
def test_price_state_tells_the_three_dashes_apart(priced, expected, why):
    """今天三种「—」在表上长得一模一样, 成因却完全不同 —— 分不开就没法判断要不要刷新."""
    app = _watchlist_app(all_results=[], view_results=[],
                         watchlist=[{"bond_code": "X"}])
    if priced is not None:
        app._watchlist_price_cache = _cache([{"bond_code": "X", **priced}])
    row = watchlist_tab._watchlist_display_rows(app, today=date(2026, 8, 26))[0]
    assert row["_price_state"] == expected, why


def test_new_columns_have_a_source_in_the_merge_whitelist():
    """守护: 主页新增列要用的字段都得先登记进 _PRICED_MERGE_FIELDS.

    漏掉不会报错, 只是那一列恒空 —— 而这一处没有别的守护能替你发现。
    """
    needed = {"event_flags", "relative_deviation", "cheapness_percentile",
              "cheapness_rank_total", "double_low", "quality_score",
              "valuation_date", "origin", "market_price_as_of", "market_price_source"}
    missing = needed - set(watchlist_tab._PRICED_MERGE_FIELDS)
    assert not missing, f"这些字段没进合并白名单, 对应列会恒空: {sorted(missing)}"


def test_market_price_is_not_in_the_generic_merge_list():
    """market_price 必须单独处理, 不能走"非 None 才覆盖"那条通用规则."""
    assert "market_price" not in watchlist_tab._PRICED_MERGE_FIELDS


# ── 陈旧即刷 (S6) ────────────────────────────────────────────────

def _stale_app(watchlist, cache_rows=(), all_results=()):
    app = _watchlist_app(all_results=list(all_results), view_results=[],
                         watchlist=list(watchlist))
    app._watchlist_price_cache = _cache(list(cache_rows))
    return app


def test_stale_codes_cover_every_non_ok_state():
    """ok 之外全部要重来 —— 隔夜的旧价、失败的行原本没有任何人管."""
    today = date(2026, 8, 26)
    app = _stale_app(
        watchlist=[{"bond_code": "FRESH"}, {"bond_code": "OLD"},
                   {"bond_code": "FAILED"}, {"bond_code": "NEVER"}],
        cache_rows=[
            {"bond_code": "FRESH", "status": "ok", "market_price": 108.0,
             "valuation_date": today},
            {"bond_code": "OLD", "status": "ok", "market_price": 108.0,
             "valuation_date": date(2026, 8, 25)},
            {"bond_code": "FAILED", "status": "failed"},
        ])
    rows = watchlist_tab._watchlist_display_rows(app, today=today)
    assert watchlist_tab.stale_watchlist_codes(app, rows=rows) == ["OLD", "FAILED", "NEVER"]


def test_listed_bond_missing_market_price_is_retried():
    """118076.SH 那个 case: status ok + 今天的估值日, 唯独市价是 None.

    只看"是不是今天算的"会让它当天永远不再重试, 市价与偏差两列空到明天。
    """
    today = date(2026, 8, 26)
    app = _stale_app(
        watchlist=[{"bond_code": "118076.SH", "is_tradable": True,
                    "trading_status": "tradable"}],
        cache_rows=[{"bond_code": "118076.SH", "status": "ok", "market_price": None,
                     "valuation_date": today}])
    rows = watchlist_tab._watchlist_display_rows(app, today=today)
    assert rows[0]["_price_state"] == "no_market"
    assert watchlist_tab.stale_watchlist_codes(app, rows=rows) == ["118076.SH"]


def test_pre_listing_new_bond_is_not_retried_forever():
    """还没上市的新债没有市价是天然状态, 不该每一轮都陪跑."""
    today = date(2026, 8, 26)
    app = _stale_app(
        watchlist=[{"bond_code": "123284.SZ", "is_tradable": False,
                    "trading_status": "pending"}],
        cache_rows=[{"bond_code": "123284.SZ", "status": "ok", "market_price": None,
                     "theoretical_price": 128.93, "valuation_date": today}])
    rows = watchlist_tab._watchlist_display_rows(app, today=today)
    assert rows[0]["_price_state"] == "no_market"
    assert watchlist_tab.stale_watchlist_codes(app, rows=rows) == []


def test_refresh_stale_debounces_quiet_rounds(monkeypatch):
    """启动 / 切页都会触发这一轮, 没有窗口就会在页签之间来回点时不停起后台定价."""
    calls = []
    monkeypatch.setattr(watchlist_tab, "_start_watchlist_pricing",
                        lambda app, codes, **kw: calls.append(codes) or True)
    app = _stale_app(watchlist=[{"bond_code": "X"}])

    assert watchlist_tab.refresh_stale_watchlist(app, quiet=True) == 1
    assert watchlist_tab.refresh_stale_watchlist(app, quiet=True) == 0   # 窗口内
    assert len(calls) == 1

    # 用户自己点的不受防抖限制
    assert watchlist_tab.refresh_stale_watchlist(app, quiet=False) == 1
    assert len(calls) == 2


def test_refresh_stale_does_not_stamp_when_round_did_not_start(monkeypatch):
    """被单飞/源不可用挡掉时不能记时间戳, 否则真正能跑的时候还要再等 15 分钟."""
    monkeypatch.setattr(watchlist_tab, "_start_watchlist_pricing",
                        lambda app, codes, **kw: False)
    app = _stale_app(watchlist=[{"bond_code": "X"}])
    assert watchlist_tab.refresh_stale_watchlist(app, quiet=True) == 0
    assert getattr(app, "_last_stale_refresh_at", None) is None


def test_refresh_stale_is_a_noop_when_everything_is_fresh(monkeypatch):
    monkeypatch.setattr(watchlist_tab, "_start_watchlist_pricing",
                        lambda app, codes, **kw: pytest.fail("不该起这一轮"))
    today = date(2026, 8, 26)
    app = _stale_app(
        watchlist=[{"bond_code": "X"}],
        cache_rows=[{"bond_code": "X", "status": "ok", "market_price": 108.0,
                     "valuation_date": today}])
    monkeypatch.setattr(watchlist_tab, "market_today", lambda: today)
    assert watchlist_tab.refresh_stale_watchlist(app, quiet=True) == 0


# ── star import 盲区的守卫 ───────────────────────────────────────

def test_star_import_exemption_only_shields_real_theme_names():
    """`from ..theme import *` 会把本该报 F821 (未定义名) 的错降级成 F405,
    而 pyproject 对 tabs/batch.py 与 tabs/batch_watchlist.py 豁免了 F405 ——
    于是那两个文件里**任何拼错的名字 ruff 都看不见**, 只在真实渲染那一行抛
    NameError, 而 GUI 在测试环境跑不起来。

    实测这不是假想: 本次搬页时删掉 `_auto_add_upcoming_to_watchlist` 的 import
    却留着两处调用, ruff 与 pytest 双双全绿。

    这条守卫把豁免从"忽略一切"收窄成"只忽略 theme 里真实导出的名字":
    逐个取出 ruff 报的 F405 名字, 不在 theme 命名空间里的一律算错。
    """
    import json
    import subprocess
    from pathlib import Path
    import convertible_bond
    from convertible_bond.gui import theme

    targets = [
        Path(convertible_bond.__file__).parent / "gui" / "tabs" / "batch.py",
        Path(convertible_bond.__file__).parent / "gui" / "tabs" / "batch_watchlist.py",
    ]
    proc = subprocess.run(
        ["ruff", "check", "--isolated", "--select", "F", "--output-format", "json",
         *[str(p) for p in targets]],
        capture_output=True, text=True,
    )
    if proc.returncode not in (0, 1):
        pytest.skip(f"ruff 不可用: {proc.stderr[:200]}")

    theme_names = set(dir(theme))
    unknown = []
    for item in json.loads(proc.stdout or "[]"):
        if item.get("code") != "F405":
            continue
        # message 形如: `X` may be undefined, or defined from star imports: ...
        message = item.get("message", "")
        name = message.split("`")[1] if "`" in message else ""
        if name and name not in theme_names:
            unknown.append(f"{Path(item['filename']).name}:{item['location']['row']}: {name}")
    assert not unknown, (
        "以下名字既不是 theme 的导出、也没有在本模块定义 —— star import 豁免正在替它们打掩护, "
        "运行期会抛 NameError:\n  " + "\n  ".join(unknown))


# ── 列换血 / 数据列 / 涨跌基准 (S8) ─────────────────────────────

def test_column_definition_is_a_single_source_of_truth():
    """表头 / 列宽 / 拉伸权重三者的键集必须完全一致.

    权重表按**表头文本**索引: 删列留死条目、加列查不到会走 batch_common 的默认 1.0
    (与"名称"同级), 窗口一拉宽就把富余宽度均摊给窄数字列。不报错、不红测试。
    """
    headers, widths = watchlist_tab.watchlist_columns()
    assert len(headers) == len(widths)
    assert set(headers) == set(watchlist_tab._WATCHLIST_COL_STRETCH_WEIGHTS)
    assert len(set(headers)) == len(headers), "表头有重复"


def test_event_flags_use_one_unambiguous_date_format():
    """「事件」列里不许出现两种 NN-NN.

    别的旗标是 ``%m-%d`` (「强赎 09-09」「下修提议 09-05」), 而「不强赎至」曾用
    ``%y-%m`` —— 「26-10」是 2026 年 10 月, 和「09-09」长得一模一样却是另一种格式,
    同一列里无从区分。实测 27/73 有事件的行走这一档 (37%), 而两类事件本就能共存。
    四位年份让格式自明。
    """
    from datetime import date as _d
    row = {"valuation_date": _d(2026, 8, 29), "call_status": "不强赎",
           "call_no_redemption_until": _d(2026, 10, 31)}
    flags = batch_pricing.event_flags(row)
    assert flags == ["不强赎至 2026-10"]
    # 反例守住: 别的旗标仍是月-日, 两者不会被读成同一种
    row2 = {"valuation_date": _d(2026, 8, 29), "call_status": "已公告强赎",
            "call_redemption_date": _d(2026, 9, 9)}
    assert batch_pricing.event_flags(row2) == ["强赎 09-09"]


def test_direction_tags_are_relabelled_without_touching_the_frozen_string():
    """「模型低估」的动宾读法与事实相反, 但标签字符串**不能**改.

    - 判据 ``deviation < −0.08`` = 市价**低**于理论价 → 模型给的价**高**;
      「模型高估离群」是市价**远高**于模型价 → 模型给的价**低**。两个标签用的是
      省略式"(按)模型(判为)X", 彼此一致, 但"模型把它低估了"正好读反。
    - 「模型高估离群」在 ``LEGACY_STRATEGY_EXCLUDE_TAGS`` (逐字冻结的默认选债排除集)
      里, 改字符串就是默认选债行为变更; 旧缓存与旧策略快照里存的也是原名。
    """
    assert "模型高估离群" in batch_pricing.LEGACY_STRATEGY_EXCLUDE_TAGS
    assert batch_pricing.risk_tag_label("模型高估离群") == "市价远高于模型价"
    assert batch_pricing.risk_tag_label("模型低估") == "市价低于模型价"
    # 展示名以**市价**为主语, 不能再出现会被反读的"模型高估/模型低估"动宾式
    for shown in batch_pricing.RISK_TAG_DISPLAY_LABEL.values():
        assert not shown.startswith("模型")
        assert "高估" not in shown and "低估" not in shown, (
            f"{shown} 里还留着没有主语的「高估/低估」")
        # **展示名不许暗示动作** —— 页面上有一个叫「需复核」的视图, 而标签里的
        # 「待核/复核」说的是完全不同的一件事: 那个视图是"数据/可交易性坏了, 去修",
        # 这些标签是"数是对的, 去研究"。**2026-09-03 复测: 原来那句「23/23 都在低估
        # 候选里」已不成立** —— 全池 311 行里带标签 22 只、「需复核」11 只, 重叠 7 只。
        # 但那 7 只恰好就是掉出「低估候选」的 7 只 (两个集合逐元素相同): 它们又真便宜、
        # 又不能信/不能买, 而这是两个不同的问题 —— 合并会恰好在最需要区分的那 7 只上
        # 把区分抹掉。方向相反这一点没变, 名字仍不该把人往那边引。
        # 该做什么由 review_notes 说, 不由标签名说。
        assert "核" not in shown, f"{shown} 在暗示动作, 而页面上有「需复核」视图"
    assert batch_pricing.risk_tag_label("深度低估待核") == "市价远低于市场中位"
    # 底层字符串一个字节没动 —— 它是 DEEP_UNDERVALUED_TAGS 的成员, 旧缓存里存的也是它
    assert "深度低估待核" in batch_pricing.DEEP_UNDERVALUED_TAGS
    assert batch_pricing.RISK_TAG_DIMENSION["深度低估待核"] == "机会信号"

    # 「正股风险」→「正股ST」: 底层串在两个冻结集里, 所以只改展示名。
    # 旧名太泛 —— 正股停牌 / 正股跌停 也都是"正股的风险", 而这个标签只判
    # ``_underlying_has_st_risk`` 一件事。新名字就是判据本身, 主语「正股」还在。
    assert batch_pricing.risk_tag_label("正股风险") == "正股ST"
    assert "正股风险" in batch_pricing.LEGACY_STRATEGY_EXCLUDE_TAGS
    assert "正股风险" in batch_pricing.HARD_REVIEW_TAGS

    # 便宜度只留横截面那一个: 绝对阈值那个已退役, 但展示名保留供旧缓存渲染
    assert "模型低估" in batch_pricing.RETIRED_RISK_TAGS
    assert batch_pricing.risk_tag_label("模型低估") == "市价低于模型价"

    # **摘要条也要走这张表** —— 它曾经另写一份字面量「⚠ 模型高估 2」, 于是标签列
    # 已经改口而摘要条还在说旧词, 同一页两种说法。
    class _Var:
        def __init__(self):
            self.value = ""

        def set(self, v):
            self.value = v

    class _App:
        v_watchlist_status = _Var()

    app = _App()
    watchlist_tab._refresh_watchlist_summary(app, [
        {"bond_code": "X", "status": "ok", "deviation": 0.3,
         "credit_rating": "AA", "risk_tags": ["模型高估离群"]},
    ])
    text = app.v_watchlist_status.value
    assert "市价远高于模型价 1" in text, text
    assert "模型高估" not in text
    # 展示名现已**全覆盖** (2026-08-31): 32 个登记标签逐个有名字。此前 3/32 是最坏的
    # 中间态 —— 3 个精心命名 + 29 个把内部变量名直接渲染给用户。
    assert set(batch_pricing.RISK_TAG_DISPLAY_LABEL) == set(batch_pricing.RISK_TAG_DIMENSION)
    assert batch_pricing.risk_tag_label("低评级") == "评级低于AA-"   # 名字带上阈值
    # 没登记的仍然原样返回 (旧缓存里可能有更早的字符串)
    assert batch_pricing.risk_tag_label("某个从未登记的标签") == "某个从未登记的标签"
    # 表放在 batch_pricing 而不是 GUI —— 事件短标签那次私有表分叉的教训
    assert not hasattr(batch_tab, "RISK_TAG_DISPLAY_LABEL")
    assert not hasattr(watchlist_tab, "RISK_TAG_DISPLAY_LABEL")


def test_tags_covered_by_the_data_column_are_dropped_on_the_watchlist():
    """「无市价」「无偏差」已由「数据状态」列以更具体的形式承载, 标签列不再复读.

    实测三只未上市新债的标签正是 ``['无偏差','无市价']``, 而同一行「数据状态」列写着
    「无市价」—— 同一行两列逐字重复。**只挡这两个**: 「无HV」「无评级」这些在表上
    没有专属列, 挡掉就真丢了。
    """
    tags = ["无偏差", "无市价", "较高HV", "无评级"]
    # 渲染走展示名 —— 「较高HV」→「正股波动偏高」, 「无评级」→「评级缺失」
    assert batch_common._format_tags(tags, drop_covered=True) == "正股波动偏高 / 评级缺失"
    # 批量页不传 drop_covered —— 那边没有「数据状态」列
    assert "市价缺失" in batch_common._format_tags(tags)


def test_trigger_gap_is_a_signed_ratio_and_sorts_natively():
    """「正股/下修线」带符号, 且**原生可排序**.

    它曾渲染成「线下 62%」/「线上 39%」—— 那是列名还叫「正股距下修线」时的补救,
    因为「距离 −62%」在中文里不通。但中文前缀让 ``_parse_sortable_number`` 一律返回
    None, 这一列于是静默退化成**字符串序**: 「线上 123%」排在「线上 3%」前面, 整个
    「线上」组还排在「线下」组前面 —— 方向和大小全反, **不报错**。

    列名去掉「距」字改成比值形式之后, `+39%` 读作"正股是触发线的 1.39 倍", 符号自洽,
    排序也回到默认路径, 那个专用排序键 (COLUMN_SORT_KEYS) 随之下线。
    """
    for gap, expected in [(-0.62, "-62%"), (0.39, "+39%"), (0.0, "+0%"),
                          (None, "—"), (float("nan"), "—")]:
        assert batch_common.trigger_gap_text(gap) == expected
    # 实现只许有一份 —— 两页曾各存一份 (那两个模块会成环), 靠比对输出防分叉
    assert not hasattr(batch_tab, "_trigger_gap_text")
    assert not hasattr(watchlist_tab, "_trigger_gap_text")
    assert not hasattr(batch_common, "COLUMN_SORT_KEYS"), "专用排序键已无消费者"


def test_numeric_columns_sort_numerically():
    """**看起来是数值的列, 点表头必须按数值排** —— 这是上面那个 bug 的通用守护.

    ``_attach_column_sort`` 靠 ``_parse_sortable_number`` 认数值列: 它只剥 ``+ % , ¥``
    再试 float。任何带文字前缀的渲染 (「线上 32%」「约 5 亿」) 都会让整列返回 None,
    于是**静默**退化成字符串序 —— 不报错, 只是点了表头之后顺序是错的。
    """
    numeric = {"市价", "理论价", "偏差(%)", "相对偏差(pp)", "双低", "剩余(年)",
               "余额(亿)", "转股价值", "转股溢价(%)", "正股σ(%)",
               "正股/下修线"}
    rows = [
        {"status": "ok", "market_price": 128.37, "theoretical_price": 135.17,
         "deviation": -0.0503, "relative_deviation": -0.259, "double_low": 142.0,
         "T": 0.23, "outstanding_balance": 4.2, "parity": 112.55,
         "conversion_premium": 0.141, "sigma": 0.61,
         "down_reset_trigger_gap": 0.32},
        {"status": "ok", "market_price": 116.35, "theoretical_price": 118.28,
         "deviation": -0.0163, "relative_deviation": -0.225, "double_low": 384.0,
         "T": 5.94, "outstanding_balance": 18.8, "parity": 30.3,
         "conversion_premium": 2.84, "sigma": 0.31,
         "down_reset_trigger_gap": -0.63},
    ]
    for name in numeric:
        getter = batch_tab._BATCH_COL_GETTERS[name]
        for row in rows:
            text = getter(row)
            assert batch_common._parse_sortable_number(text) is not None, (
                f"「{name}」渲染成 {text!r}, _parse_sortable_number 认不出来 —— "
                "这一列会静默按字符串排序")


def test_double_low_does_not_go_dark_with_a_stale_anchor():
    """``double_low = 市价 + 转股溢价率×100`` 是纯局部量, 只有它的**秩**需要横截面.

    此前它搭了「相对偏差」的车, 锚一过期整列黑 —— 而「双低 <130」是不需要任何锚
    就能读的行业经验阈值。
    """
    import ast
    import inspect
    src = inspect.getsource(watchlist_tab._render_watchlist_table)
    tree = ast.parse(src.lstrip())
    assigns = {t.id: ast.unparse(n.value)
               for n in ast.walk(tree) if isinstance(n, ast.Assign)
               for t in n.targets if isinstance(t, ast.Name)}
    assert "anchor_stale" in assigns["rel_str"], "「相对偏差」的分母就是锚, 必须跟着变暗"
    assert "anchor_stale" not in assigns["dbl_str"], "「双低」不该跟着锚变暗"


def test_shared_columns_use_the_same_caliber_on_both_pages():
    """**同名列两页必须同口径** —— 逐个边界用例跑两边的渲染表达式.

    实测跑出来过五处不一致 (不是看出来的):

    1. 「市价」批量页被 ``status == "ok"`` 门控, 关注池没有 —— 市价是**市场事实**,
       定价成不成功与它无关; 门控会让"模型算挂了"和"这只债真没行情"长得一样。
    2. 「理论价」正好**反过来**: 批量页不门控而关注池门控 —— 理论价是模型输出,
       定价失败时不该还显示一个数。
    3. 「市价」批量页判空用 ``is not None``, 而 ``watchlist_cache._NAN_FIELDS`` 含
       ``market_price`` —— 那条持久化路径读回来是 **NaN**, 而 ``NaN is not None``
       为真, 会渲染出字面的 "nan"。两页会互相喂行 (关注池 worker 写
       ``_batch_all_results``), 所以必须同口径。
    4. 「评级」空值批量页渲染成空串, 关注池渲染「—」。
    5. 「剩余(年)」批量页锚**估值日** (pricer 入参 T), 关注池锚**今天** ——
       实测 271/284 行显示值不同。锚今天会让一行之内出现两个时点。
    """
    nan = float("nan")
    cases = {
        "正常": {"status": "ok", "market_price": 128.37, "theoretical_price": 135.17,
                 "credit_rating": "A+"},
        "市价NaN": {"status": "ok", "market_price": nan, "theoretical_price": 135.17},
        "市价None": {"status": "ok", "market_price": None, "theoretical_price": 135.17},
        "定价失败": {"status": "timeout", "market_price": 128.37,
                     "theoretical_price": 135.17},
        "无评级": {"status": "ok", "credit_rating": None},
    }
    expected = {
        "正常": {"市价": "128.37", "理论价": "135.17", "评级": "A+"},
        "市价NaN": {"市价": "—", "理论价": "135.17"},
        "市价None": {"市价": "—", "理论价": "135.17"},
        # 市价照常显示 (市场事实), 理论价打「—」(模型没算出来)
        "定价失败": {"市价": "128.37", "理论价": "—"},
        "无评级": {"评级": "—"},
    }
    for case, row in cases.items():
        for col, want in expected[case].items():
            got = batch_tab._BATCH_COL_GETTERS[col](row)
            assert got == want, f"{case} 的「{col}」渲染成 {got!r}, 应当是 {want!r}"

    # 「剩余(年)」两页锚同一个时点 (估值日), 不是今天
    from datetime import date
    entry = {"maturity_date": "2026-10-15", "valuation_date": date(2026, 8, 26)}
    anchored = watchlist_tab._years_to_maturity_text(entry, date(2026, 8, 29))
    batch_t = batch_tab._BATCH_COL_GETTERS["剩余(年)"](
        {"T": (date(2026, 10, 15) - date(2026, 8, 26)).days / 365.25})
    assert anchored == batch_t, f"关注池 {anchored} vs 批量页 {batch_t}"
    # 缺估值日才回落到今天
    assert watchlist_tab._years_to_maturity_text(
        {"maturity_date": "2026-10-15"}, date(2026, 8, 29)) == "0.13"


def test_underlying_column_falls_back_to_the_stock_code():
    """正股列渲染**名称**, 但缺名字时必须回落代码 —— 不能留一格空的.

    这不是保守写法, 是当前必需: ``cb_data.json`` 的 ``underlying_name`` 实测只有
    722/1059。它 08-24 曾是 1033/1058, 被一次全量条款同步清掉 317 只 —— 当时全量同步是
    整条记录替换、只保 ``credit_rating``, 而 ``get_bond_terms`` 根本不返回正股名。
    该缺陷已修 (``cb_data_sync.locally_authoritative_fields`` 按 provider 声明的字段
    所有权保护), 但**存量缺口要等下一次状态刷新才补得回来**, 所以回落仍是当前必需:
    直接换成名字会让三成的行变空。

    两张表必须同口径 —— 它们共用「正股」这个列名。
    """
    named = {"underlying_name": "金隅冀东", "stock_code": "000401.SZ"}
    unnamed = {"underlying_name": None, "stock_code": "000401.SZ"}
    blank = {}

    assert batch_tab._BATCH_COL_GETTERS["正股"](named) == "金隅冀东"
    assert batch_tab._BATCH_COL_GETTERS["正股"](unnamed) == "000401.SZ"
    assert batch_tab._BATCH_COL_GETTERS["正股"](blank) == "—"

    # 关注池那一格是 vals 里的字面表达式, 用 AST 取出来比对同一套回落链
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(watchlist_tab))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_render_watchlist_table")
    vals = next(n for n in ast.walk(fn) if isinstance(n, ast.Assign)
                and any(getattr(t, "id", "") == "vals" for t in n.targets))
    headers, _ = watchlist_tab.watchlist_columns()
    cell = ast.unparse(vals.value.elts[headers.index("正股")])
    assert "underlying_name" in cell and "stock_code" in cell, (
        f"关注池「正股」格没走 名称→代码 回落: {cell}")


def test_row_values_line_up_with_the_column_definition():
    """``vals`` 的元素数必须等于列数 —— 差一个就整行右移, 而**不会报错**.

    表头 / 列宽 / 权重三者已有守护 (上一个用例), 但那三张表都是"列的定义",
    渲染出来的那一行是第四张表, 此前没人比对。少一个元素 ttk 会把尾列留空,
    多一个直接丢掉; 中间插错位置则是每一格都渲染在错的表头底下 —— 市价那一列
    读出来是理论价, 数字全都"看上去完全正常"。

    用 AST 数而不是真渲染: Treeview 在测试环境起不来 (CustomTkinter 无显示),
    而这里要防的恰恰是**静态**的列表长度不一致。
    """
    import ast
    import inspect

    src = inspect.getsource(watchlist_tab)
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_render_watchlist_table")
    vals = next(n for n in ast.walk(fn) if isinstance(n, ast.Assign)
                and any(getattr(t, "id", "") == "vals" for t in n.targets))
    headers, _ = watchlist_tab.watchlist_columns()
    assert len(vals.value.elts) == len(headers), (
        f"vals 有 {len(vals.value.elts)} 个元素, 表头有 {len(headers)} 列")


def test_dropped_columns_are_really_gone():
    """砍「可信」留「敏感性」; 机会分折进别处; 「加入时偏差」「市价变化」锚的是加入
    瞬间, 与选定的"vs 上一交易日"口径不是一回事。

    - **「上市日」已按用户决策加回**, 不再在本清单里 —— 关注池的 12 行里三档取值
      (过去日期 / 未来日期 / 「待定」) 全都真实存在, 这正是新债最想看的一格。
    - **「可交易日」「距交易」继续留在清单里, 理由比当初更强**: 实测
      ``tradable_date ≡ listing_date`` (主池 284/284、关注池 10/10 完全相同),
      前者与上市日同值; 后者是从同一个日期算的派生量, 还会随"你哪天开的 GUI"
      漂移、没法跟公告核对。
    - **「偏差Δ(pp)」新进清单**: 理论价不动时 ``偏差Δ ≡ 涨跌 × (1 + 偏差)`` 是
      恒等式, 它和左边的「涨跌」是同一列; 独立信息 (理论价漂移) 实测中位只占 10%,
      而安静的日子里那个量级就是 21 日 HV 窗口滚动的噪声。
    - **「涨跌%」按用户决策删除**: 这是研究工作台不是行情软件, 而它恰是全 app 最像
      行情软件的一格。它一走, 整条"vs 上一交易日"的展示机制就没有消费者了 ——
      ``change_column_label`` / ``CHANGE_COLUMN_KEY`` / ``_previous_daily_snapshot``
      / ``_baseline_is_usable`` / ``_pct_change`` 连同各自的用例一起删掉, 不留孤儿。
      **但 ``data/watchlist_daily/`` 的窄快照仍然照写** (见
      ``watchlist_cache.NARROW_FIELDS`` 上的说明): 那个目录只追加, 停写就等于把
      这些天永久丢掉, 而恢复任何一列都要靠它。
    """
    weights = set(watchlist_tab._WATCHLIST_COL_STRETCH_WEIGHTS)
    for gone in ("可信", "可交易日", "距交易", "机会分", "涨跌%", "涨跌(%)",
                 "加入时偏差(%)", "市价变化(%)", "状态", "偏差Δ(pp)", "敏感性"):
        assert gone not in weights, f"{gone} 应该已经砍掉"
    # 列名从「取价」→「数据」→「数据状态」: 最后这步把七档文案的共同点
    # (这一行的数是什么时候的) 写进了名字, 所以它不再需要 tooltip。
    assert "数据状态" in weights and "数据" not in weights and "取价" not in weights
    assert "取价" not in weights
    # 加回来的列: 上市日 (新债进度) / 评级 (摘要条一直在报平均评级) /
    # 距下修线 (下修博弈活没活) / 剩余(年) (实测半数关注池 <0.6 年到期)
    for back in ("上市日", "评级", "正股/下修线", "剩余(年)"):
        assert back in weights, f"{back} 应该已经加回"
    # 双低带方向: 它是个裸数, 且与左边的「市价」不同向, 表头不写方向读者无从判断
    # 方向注释已从名字挪进表头 tooltip (COLUMN_HELP), 名字收回「双低」
    assert "双低" in weights and "双低(小=便宜)" not in weights
    # 跨日变化列全没了 → 动态表头机制也该一起消失, 不留半截
    assert not hasattr(watchlist_tab, "change_column_label")
    assert not hasattr(watchlist_tab, "CHANGE_COLUMN_KEY")


@pytest.mark.parametrize("state,extra,latest,expected", [
    # 主文案是**市价 as-of 的日期**, 不是"估值日是不是今天"。上一版正常态写
    # 「✓ 今日 · 价 08-28」—— 14 个字符里主文案讲的是 market_today() 给的估值日,
    # 真答案挤在后缀。
    ("ok", {"market_price_as_of": date(2026, 8, 28)}, date(2026, 8, 28), "✓ 08-28"),
    ("ok", {"market_price_as_of": date(2026, 8, 26)}, date(2026, 8, 28), "市价旧 08-26"),
    ("stale", {}, None, "未重算"),
    ("stale", {"valuation_date": date(2026, 8, 26)}, None, "未重算 08-26"),
    ("no_market", {}, None, "无市价"),
    ("failed", {"status": "provider error: timeout"}, None, "失败 · provider error: ti"),
    ("unpriced", {}, None, "未定价"),
])
def test_row_data_label(state, extra, latest, expected):
    entry = {"bond_code": "X", "_price_state": state, **extra}
    assert watchlist_tab._row_data_label(entry, latest_as_of=latest) == expected


def test_stale_label_never_asserts_a_day_it_did_not_check():
    """「昨日」是写死的文案时, 出差一周回来六行仍全写「昨日」.

    _derive_price_state 只判"是不是今天"、不算天数, 所以标签**不能**替它断言差几天。
    有估值日就拼真实日期; 没有才退回一个不承诺任何天数的「未重算」。
    """
    label = watchlist_tab._row_data_label(
        {"bond_code": "X", "_price_state": "stale", "valuation_date": "2026-08-14"})
    assert label == "未重算 08-14"
    assert "昨日" not in label


def test_stale_price_baseline_is_the_page_not_the_valuation_date():
    """陈旧判据锚**本页 as-of 的最大值**, 不锚估值日.

    估值日走 ``market_today()`` 是自然日, 而 2026-08-29 是**星期六** —— 周五收盘价
    在周六就是最新价, 但 ``as_of < 估值日`` 对当天每一行都成立 (实测 9/9 有价的行
    全被标陈旧)。周末两天加每个交易日收盘前的整段时间, 这个提示恒亮, 把停牌/节假日
    那种**真**陈旧淹掉。
    """
    saturday = {"bond_code": "X", "_price_state": "ok",
                "valuation_date": date(2026, 8, 29),        # 周六
                "market_price_as_of": date(2026, 8, 28),    # 周五收盘 = 最新
                "market_price_source": "history"}
    rows = [saturday, {**saturday, "bond_code": "Y"}]
    latest = watchlist_tab._latest_market_as_of(rows)
    assert latest == date(2026, 8, 28)
    assert watchlist_tab._row_data_label(saturday, latest_as_of=latest) == "✓ 08-28"

    # 而真陈旧 (停牌: 别人都拿到 08-28, 只有它是 08-21) 照常标出来
    halted = {**saturday, "market_price_as_of": date(2026, 8, 21)}
    assert watchlist_tab._row_data_label(halted, latest_as_of=latest) == "市价旧 08-21"

    # 整页一起旧时没有基准可比 → 不标。那一档归摘要条的「估值日 MM-DD」管。
    assert watchlist_tab._latest_market_as_of([]) is None
    assert watchlist_tab._row_data_label(halted, latest_as_of=None) == "✓ 08-21"


def test_data_label_flags_the_unstamped_fallback():
    """terms.close 兜底那一档没有 as-of, 可以任意旧 (日升转债库里的是 2021 年的值).

    原文案是「无戳」—— 从代码里搬出来的黑话, 表上没人读得懂"戳"是时间戳。
    """
    entry = {"bond_code": "X", "_price_state": "ok",
             "market_price_source": "terms_close",
             "market_price_as_of": date(2026, 8, 28)}
    assert watchlist_tab._row_data_label(entry) == "日期不明"
    # as-of 干脆缺失的那一档也归这里, 不能渲染成一个假日期
    assert watchlist_tab._row_data_label(
        {"bond_code": "X", "_price_state": "ok"}) == "日期不明"


def test_unpriced_label_uses_pool_exclusion_reason_not_view_reason():
    """「已发行未上市」这类文案来自 batch_pricing_exclusion_reason.

    接 view_exclusion_reason 是错的: 它返回视图口径文案 (「相对市场中位 +17.9pp,
    未便宜过 5pp」), 而且要收一个 view 参数 —— 主页根本没有视图选择器。
    """
    class _Cache:
        def get(self, code):
            return object()

    calls = []

    def fake_reason(code, terms, **kw):
        calls.append(code)
        return "已发行未上市"

    import convertible_bond.gui.tabs.batch_watchlist as mod
    original = mod.batch_pricing_exclusion_reason
    mod.batch_pricing_exclusion_reason = fake_reason
    try:
        label = mod._row_data_label({"bond_code": "123284.SZ", "_price_state": "unpriced"},
                                    terms_cache=_Cache())
    finally:
        mod.batch_pricing_exclusion_reason = original
    assert label == "未定价 · 已发行未上市"
    assert calls == ["123284.SZ"]


# ── 事件区双向化 (S9) ────────────────────────────────────────────

class _FakeEventStore:
    def __init__(self, by_code):
        self._by_code = by_code

    def list_events(self, bond_code=None):
        return self._by_code.get(bond_code, [])


class _BannerEv:
    def __init__(self, event_type, day):
        self.event_type = event_type
        self.event_date = day
        self.effective_start = None
        self.effective_end = None


class _BannerApp:
    def __init__(self, watchlist, store, pool=()):
        self._batch_watchlist = [{"bond_code": c, "bond_name": c} for c in watchlist]
        self._batch_all_results = [{"bond_code": c, "bond_name": c} for c in pool]
        self.event_store = store
        self.v_batch_events_banner = _StrVar()
        self.lbl_batch_events_banner = _FakeLabel()
        self._batch_events_banner_full = []


class _StrVar:
    def __init__(self):
        self._v = ""

    def get(self):
        return self._v

    def set(self, v):
        self._v = v


class _FakeLabel:
    def __init__(self):
        self.shown = None

    def grid(self):
        self.shown = True

    def grid_remove(self):
        self.shown = False


def test_events_banner_shows_an_explicit_empty_state(monkeypatch):
    """空是**常态**不是异常 —— 实测今天关注池近 7 天与未来 30 天都是 0 件.

    藏起控件会重演「低估候选默认打开是空表、用户以为坏了」那次: 一个消失的控件
    和一个坏掉的控件长得一模一样。
    """
    monkeypatch.setattr(watchlist_tab, "market_today", lambda: date(2026, 8, 26))
    app = _BannerApp(["A.SH", "B.SH"], _FakeEventStore({}))
    watchlist_tab._refresh_events_banner(app)
    assert app.lbl_batch_events_banner.shown is True, "空态不许 grid_remove"
    assert "已扫 2 只" in app.v_batch_events_banner.get()
    assert "无日程事件" in app.v_batch_events_banner.get()


def test_events_banner_splits_past_and_future(monkeypatch):
    today = date(2026, 8, 26)
    monkeypatch.setattr(watchlist_tab, "market_today", lambda: today)
    store = _FakeEventStore({
        "A.SH": [_BannerEv("call_redemption", today - timedelta(days=3))],
        "B.SH": [_BannerEv("putback", today + timedelta(days=10))],
    })
    watchlist_tab._refresh_events_banner(_app := _BannerApp(["A.SH", "B.SH"], store))
    text = _app.v_batch_events_banner.get()
    assert "近 7 天 1 件" in text
    assert "未来 30 天 1 件" in text
    # 明细一条不少地留给弹窗, 过去的排在前面 (它们是"已经发生了而你可能没看见")
    assert len(_app._batch_events_banner_full) == 2
    assert _app._batch_events_banner_full[0][0] == "A.SH"


def test_events_banner_ignores_bonds_outside_the_watchlist(monkeypatch):
    """**池外的债不进横幅** (按用户决策). 这是"我的"关注池, 别人的事在别的页面找.

    此前末尾挂着「全池另有 N 件 (单击查看全部)」, 理由是"横幅真正的用处是告诉你
    **还不知道的那些**"。那条理由已被推翻 —— 而它恰好是横幅上唯一常年非空的一段,
    删掉之后这个控件会更经常地处于空态, 所以配色必须跟着内容走
    (见 test_banner_tone_follows_content)。
    """
    today = date(2026, 8, 26)
    monkeypatch.setattr(watchlist_tab, "market_today", lambda: today)
    store = _FakeEventStore({
        "P1.SH": [_BannerEv("call_redemption", today + timedelta(days=5))],
        "P2.SH": [_BannerEv("putback", today + timedelta(days=6))],
    })
    app = _BannerApp(["A.SH"], store, pool=["P1.SH", "P2.SH"])
    watchlist_tab._refresh_events_banner(app)
    text = app.v_batch_events_banner.get()
    assert "全池" not in text and "2 件" not in text
    assert "无日程事件" in text          # 关注池自己是空的, 显式说出来
    assert app.lbl_batch_events_banner.shown is True


def test_events_banner_survives_a_missing_store():
    app = _BannerApp(["A.SH"], None)
    app.event_store = None
    watchlist_tab._refresh_events_banner(app)
    assert app.lbl_batch_events_banner.shown is True
    assert app.v_batch_events_banner.get() == "事件表未载入"


# ── 日期口径的四道闸 (A1 / A2 / B1 / B2) ──────────────────────────
class _PricedSourcesApp:
    """只带取价三路来源的最小 app 替身 (名字别撞上文件里已有的 _FakeApp)."""

    def __init__(self, cache_rows=None, upcoming=None, pool=None):
        self._watchlist_price_cache = {"meta": {}, "rows": cache_rows or {}}
        self._batch_upcoming_results = upcoming or []
        self._batch_all_results = pool or []


def test_priced_rows_prefer_the_freshest_source_not_the_last_one():
    """更旧的全池缓存不得整行盖掉更新的关注池热缓存.

    实测事故形态: 热缓存 08-28 (派克 160.347 / 先锋 177.259), 而
    `_batch_all_results` 是 `_load_result_cache` 在 `app.after(80)` 里从
    batch_pricing_cache.json 读回来的**另一份磁盘**, 估值日 08-26 且先锋那行
    market_price=None。主页先画 08-28, 80ms 后被 08-26 顶掉 —— 涨跌符号当场反转,
    先锋的市价/涨跌/偏差三列一起退化成「—」。
    """
    app = _PricedSourcesApp(
        cache_rows={
            # 带 priced_at: 热缓存行落盘时一定有这个戳, 而定价结果行一个都没有
            # (实测 batch_pricing_cache.json 284/284 无)。fixture 漏掉它, 这两条
            # 用例就都在测一个现实中不存在的形态。
            "111026.SH": {"bond_code": "111026.SH", "market_price": 160.347,
                          "valuation_date": date(2026, 8, 28),
                          "priced_at": "2026-08-27T20:11:35"},
            "118076.SH": {"bond_code": "118076.SH", "market_price": 177.259,
                          "valuation_date": date(2026, 8, 28),
                          "priced_at": "2026-08-27T20:11:35"},
        },
        pool=[
            {"bond_code": "111026.SH", "market_price": 155.7155,
             "valuation_date": date(2026, 8, 26)},
            {"bond_code": "118076.SH", "market_price": None,
             "valuation_date": date(2026, 8, 26)},
        ],
    )
    got = watchlist_tab._priced_rows_by_code(app)
    assert got["111026.SH"]["market_price"] == 160.347
    assert got["118076.SH"]["market_price"] == 177.259


def test_priced_rows_still_let_memory_win_on_the_same_day():
    """同一天则"这次算的"压过"上次算的" —— 原优先级正确, 不要一起改掉.

    ⚠️ 热缓存行**必须带 priced_at**, 内存行**必须不带** —— 这就是生产形态
    (``to_cache_row`` 落盘时无条件盖戳; 定价结果行没有任何写入点)。曾拿
    ``priced_at`` 当同日 tie-break, 缺失方恒为 "" 恒排最旧, 于是磁盘永远赢内存:
    上午点过 ⚡、下午跑全量重算后主页仍显示上午的价, 而批量页是下午的。
    两边都不带戳的 fixture 会让这个回归全程绿灯 —— 它正是这么溜过去的。
    """
    day = date(2026, 8, 28)
    app = _PricedSourcesApp(
        cache_rows={"X": {"bond_code": "X", "market_price": 100.0, "valuation_date": day,
                          "priced_at": "2026-08-28T09:30:00"}},
        pool=[{"bond_code": "X", "market_price": 101.0, "valuation_date": day}],
    )
    assert watchlist_tab._priced_rows_by_code(app)["X"]["market_price"] == 101.0


def test_freshness_key_never_depends_on_a_field_only_one_source_has():
    """新鲜度的 tie-break 不得架在 priced_at 这类**单边字段**上.

    守的是判据本身而不是某一次的取值: 只要 _row_freshness 的返回值随 priced_at
    变化, 同日比较就会被"谁有这个戳"决定, 而不是被来源决定。
    """
    day = date(2026, 8, 28)
    base = {"bond_code": "X", "valuation_date": day}
    stamped = {**base, "priced_at": "2026-08-28T09:30:00"}
    rank = watchlist_tab._SOURCE_RANK_CACHE
    assert watchlist_tab._row_freshness(base, rank) == watchlist_tab._row_freshness(stamped, rank)
    # 来源序号必须真的参与排序
    assert (watchlist_tab._row_freshness(base, watchlist_tab._SOURCE_RANK_POOL)
            > watchlist_tab._row_freshness(stamped, watchlist_tab._SOURCE_RANK_CACHE))


def test_cross_section_columns_go_dark_when_the_anchor_is_old():
    """口径5: 锚超过 5 个交易日, 「相对偏差 / 双低」不再显示.

    中位偏差的水平时变 (cb_valuation_history 20 期实测摆幅 21.2pp), 用几周前的
    市场水平算出来的"比中位便宜 5pp"是个看上去完全正常的数字。
    """
    today = date(2026, 8, 28)
    fresh = {"market_median_deviation": 0.2089,
             "market_median_deviation_as_of": date(2026, 8, 26)}
    assert watchlist_tab._cross_section_is_stale(fresh, today) is False

    old = {"market_median_deviation": 0.2089,
           "market_median_deviation_as_of": date(2026, 7, 1)}
    assert watchlist_tab._cross_section_is_stale(old, today) is True

    # 锚值缺失 (小批量标注时主池为空) 同样不可比
    assert watchlist_tab._cross_section_is_stale({"valuation_date": today}, today) is True


def test_anchor_as_of_falls_back_to_the_rows_own_valuation_date():
    """没有显式戳 = 锚是与这行同一批自算的, as-of 就是这行自己的估值日.

    主池行走的正是这条路 (annotate_batch_results 默认自算), 不能把它们一律当成
    "锚来历不明"而灰掉。
    """
    assert watchlist_tab._anchor_as_of({"valuation_date": date(2026, 8, 26)}) == date(2026, 8, 26)
    assert watchlist_tab._anchor_as_of(
        {"valuation_date": date(2026, 8, 26),
         "market_median_deviation_as_of": date(2026, 8, 20)}) == date(2026, 8, 20)
    assert watchlist_tab._anchor_as_of({}) is None


def test_absolute_fallback_anchor_is_not_shown_as_a_cross_section_number():
    """主池为空时 anchor 回落 0.0, relative_deviation 恒等于绝对偏差.

    数值保留 (改它就是默认选债行为变更), 但展示层不能把它当横截面量渲染 ——
    实测派克转债 有锚 +26.7 / 无锚 +47.5, 后者是个看上去完全正常的数字。
    """
    today = date(2026, 8, 28)
    entry = {"market_median_deviation": 0.0,
             "cross_section_origin": "absolute_fallback",
             "valuation_date": today}
    assert watchlist_tab._cross_section_is_stale(entry, today) is True


def test_fallback_anchor_is_never_picked_up_as_a_real_anchor():
    """假的 0.0 锚不得被 cross_section_anchor_from 捡走传给下一批标注."""
    from convertible_bond import batch_pricing

    rows = [{"bond_code": "A", "status": "ok", "deviation": 0.4,
             "market_median_deviation": 0.0,
             "cross_section_origin": "absolute_fallback",
             "valuation_date": date(2026, 8, 26)}]
    assert batch_pricing.cross_section_anchor_from(rows) is None

    # 存量行没有 cross_section_origin —— 按真锚处理, 否则关注池整页立刻空掉
    legacy = [{"bond_code": "A", "status": "ok", "deviation": 0.4,
               "market_median_deviation": 0.2089,
               "valuation_date": date(2026, 8, 26)}]
    assert batch_pricing.cross_section_anchor_from(legacy) == pytest.approx(0.2089)
    assert batch_pricing.cross_section_anchor_as_of(legacy) == date(2026, 8, 26)


def test_a_listed_bond_is_not_new_even_if_the_watchlist_froze_it_as_pending():
    """关注池条目里的 is_tradable/trading_status 是**加入那一刻**冻结的.

    实测形态: 中仑转债 08-24 上市、派克转债 08-25 上市, 而 watchlist.json 里至今
    写着 is_tradable=False / trading_status=pending —— 它们是"已发行未上市"时被扫
    进来的, 上市之后没有任何路径回填。日期已过是"确实挂牌了"的正面证据, 必须压过
    这两个派生标记。

    这个坑原本被一个巧合挡着: 取价让全池行无条件覆盖, 而全池行带着刚推断出来的
    is_tradable=True。改成按新鲜度择优后热缓存行胜出 (CACHE_FIELDS 故意不收这两个
    派生字段), 冻结值浮上来, 整张关注池表全染成新债色, 高估/离群的区分一起消失。
    """
    row = {
        "bond_code": "123281.SZ", "status": "ok",
        "is_tradable": False, "trading_status": "pending",   # 冻结的旧值
        "listing_date": market_today() - timedelta(days=4),  # 但它早就上市了
        "deviation": 0.41, "risk_tags": ["模型高估离群"],
    }
    assert _is_new_bond(row) is False
    # 这条钉的是 _is_new_bond 的日期优先级, 不是某个具体颜色: 只要它没被误判成
    # 新债, 行色就该由后续判据说了算 (这一行没有拦截标签, 所以是无色)。
    assert _resolve_row_tag(row) is None


def test_a_future_listing_date_still_wins_over_a_tradable_flag():
    """反方向也要成立: 上市日在未来 → 还是新债, 别被一个乐观的 is_tradable 骗过去."""
    row = {
        "bond_code": "123999.SZ", "status": "ok",
        "is_tradable": True, "trading_status": "tradable",
        "listing_date": market_today() + timedelta(days=3),
    }
    assert _is_new_bond(row) is True


def test_issued_pending_listing_still_falls_back_to_the_derived_flags():
    """一个日期都没有时才回落到派生字段 —— 「已发行未上市」正是这一档,
    连上市日都还没公告, 除了 pending 没有别的线索。"""
    row = {
        "bond_code": "123284.SZ", "status": "ok",
        "is_tradable": False, "trading_status": "pending",
        "listing_date": None, "tradable_date": None,
    }
    assert _is_new_bond(row) is True


# ── 行染色的六道闸 ──────────────────────────────────────────────
#
# 这一族测试补在配色链**零覆盖**的事实之上: 改动前 `_TAG_COLORS` /
# `_apply_tag_colors` / `_configure_tree_style` / `_TREE_ATTRS` 在 tests/ 里
# 一处引用都没有 —— 实测把行色口径从绝对偏差换成相对偏差 (重着色 261→194 行、
# 绿色 0→81 只) 全套 915 个测试照样全绿。
import tkinter as tk

from convertible_bond.batch_pricing import (
    BLOCKING_RISK_TAGS,
    DATA_QUALITY_RISK_TAGS,
    RISK_TAG_DIMENSION,
    TRADABILITY_RISK_TAGS,
)
from convertible_bond.gui.tabs import batch_common
from convertible_bond.gui.tabs.batch_common import (
    _TAG_COLORS,
    _TAG_LEGEND,
    _TREE_ATTRS,
    row_colour_legend,
)


def test_row_colour_never_depends_on_how_cheap_the_bond_looks():
    """整行颜色不许表达贵/便宜 —— 喂遍偏差字段全域, tag 必须一动不动.

    钉住两个真实故障形态:

    ① **绿色结构性不可达**。绝对绿线 ``dev < −3%`` 换算到相对轴是
       ``rel < −(3% + 中位)``, 而橙线是 ``|rel| ≥ 20pp``
       (``DEVIATION_ANOMALY_THRESHOLD``) 且优先级更高 —— 于是
       ``绿 ⊂ 橙 ⟺ 当期中位 ≥ 17%``。实测中位 +20.86% 时 ``underpriced``
       渲染 **0/284**, 而独立判据其实命中 3 只 (侨银 −5.03% / 万讯 −3.93% /
       宝莱 −3.24%), 全被橙色吃掉; 20 期估值基线里 5 期都在这一档。

    ② **颜色是渲染排序键的单调函数**。``sort_batch_results_for_view`` 对
       「低估候选/转股折价」按 ``relative_deviation`` **升序**, 而「低估候选」的
       准入判据本身就是 ``rel < −5pp`` —— 任何架在便宜度上的行色在那一页上都是
       整表同色 (实测 40/40)。便宜度已经被行位置编码完了。
       (「全池」已改按**上市日倒序**, 那一页的排序键根本不是便宜度, 上色会与
       行位置各说各话。)

    所以这条不是"换个阈值", 是**便宜度整体退出行色通道**。
    """
    base = {"bond_code": "123456.SZ", "status": "ok", "risk_tags": []}
    baseline = _resolve_row_tag(base)

    for field in ("deviation", "relative_deviation", "cheapness_percentile"):
        for value in (-0.50, -0.2386, -0.05, -0.03, 0.0, 0.05, 0.15,
                      0.2086, 2.50, None, float("nan")):
            row = dict(base, **{field: value})
            assert _resolve_row_tag(row) == baseline, (
                f"{field}={value!r} 改变了行色 —— 便宜度不该进这个通道")


def test_two_tags_that_mean_opposite_things_never_share_one_visual_output():
    """「深度低估待核」与「模型高估离群」必须区分得开.

    它们由 ``batch_pricing`` 按 gap 符号显式拆成两个方向明确的标签
    (实测相对偏差中位 **−21.95pp** vs **+27.76pp**), 而且分属**两个不同维度**
    (机会信号 / 模型适用性) —— 老代码用一个字面量集合把它们合回同一个 ORANGE,
    「低估候选」40 行里被染橙的 16 只全部是深度低估待核。

    这是 AGENTS 里「暂停转股与恢复转股是相反的意思, 必须是相反的颜色」那次事故
    在行色上的复发。判据要落在**能区分**上, 而不是"必须各有一个颜色" —— 两个都
    不占用行色、由「标签」列的文字承载, 同样满足。
    """
    cheap = {"bond_code": "A", "status": "ok", "risk_tags": ["深度低估待核"]}
    rich = {"bond_code": "B", "status": "ok", "risk_tags": ["模型高估离群"]}

    assert RISK_TAG_DIMENSION["深度低估待核"] != RISK_TAG_DIMENSION["模型高估离群"]

    same_colour = _resolve_row_tag(cheap) == _resolve_row_tag(rich)
    same_text = (batch_common._format_tags(cheap["risk_tags"])
                 == batch_common._format_tags(rich["risk_tags"]))
    assert not (same_colour and same_text), "方向相反的两个标签在表上完全同形"


def test_every_row_colour_has_a_text_exit():
    """每个 tag 都要有一条非颜色出口 —— 颜色是最不可靠的那条通道.

    实测灰阶下浅色 ``new`` 与 ``overpriced`` 的亮度比 **1.00** (灰值都是 106)、
    深色 ``underpriced`` 与普通文字 **1.03**: 截图、单色打印、红绿色觉缺陷
    (约 8% 男性) 拿到的信息量正好是 0。这条比任何 ΔE 阈值稳, 因为它不依赖
    某一份色觉仿真实现。
    """
    plain = {"bond_code": "P", "status": "ok", "risk_tags": []}
    samples = {
        "new": {"bond_code": "N", "status": "ok", "risk_tags": ["无市价", "无偏差"],
                "trading_status": "pending", "is_tradable": False,
                "listing_date": None, "tradable_date": None},
        "blocked": {"bond_code": "B", "status": "ok", "risk_tags": ["临近摘牌"]},
        "nodata": {"bond_code": "D", "status": "ok", "risk_tags": ["无市价"]},
    }
    assert set(samples) == set(_TAG_COLORS), "新增 tag 时要同步补一条文字出口的样例"

    for tag, row in samples.items():
        assert _resolve_row_tag(row) == tag
        text_cells = {
            name: getter(row)
            for name, getter in batch_tab._BATCH_COL_GETTERS.items()
            if name in {"标签", "状态", "市价", "可信"}
        }
        plain_cells = {
            name: getter(plain)
            for name, getter in batch_tab._BATCH_COL_GETTERS.items()
            if name in {"标签", "状态", "市价", "可信"}
        }
        assert text_cells != plain_cells, f"{tag} 这一档只有颜色说得出来"


def test_refresh_theme_survives_a_tree_that_was_already_destroyed(monkeypatch):
    """空结果视图留下的悬垂 Treeview 不许打断整轮重染.

    触发链今天就成立: 默认落地「全池」(实测 284 行, 注册树) → 切「转股折价」
    (**实测 0 行**) → 切主题 (app.py:996) 或跨响应式档位 (app.py:748)。真机 Tk **8.6.15** 实测 ``tag_configure`` 抛
    ``TclError: invalid command name ".!frame.!treeview"`` —— 而
    ``getattr(app, attr, None) is not None`` 拦不住已 destroy 的控件 (它还是个对象)。

    更隐蔽的是后果: 异常从 ``for attr in _TREE_ATTRS`` 里抛出会**中断循环**, 而
    ``_TREE_ATTRS`` 是 ``set``、遍历顺序随 ``PYTHONHASHSEED`` 随机 —— 用户看到的
    不是崩溃对话框, 是"切了一下主题, 有些表变了色有些没变, 而且每次开机变的不是
    同一批"。所以这条要同时断言: **不抛** + **活树照常刷到** + 悬垂项被摘掉。
    """
    class _DeadTree:
        def tag_configure(self, *a, **kw):
            raise tk.TclError('invalid command name ".!frame.!treeview"')

        def winfo_width(self):
            raise tk.TclError('invalid command name ".!frame.!treeview"')

    class _LiveTree:
        def __init__(self):
            self.tags = {}

        def tag_configure(self, tag, **kw):
            self.tags[tag] = kw

        def winfo_width(self):
            return 1  # ≤1 → _apply_responsive_tree_font 直接返回, 不碰 ttk.Style

    class _App:
        pass

    # refresh_theme 头一件事是 _configure_tree_style() → ttk.Style() → 隐式建 Tk root,
    # 无头 runner 上直接 `TclError: no display name and no $DISPLAY` —— 本机有窗口系统
    # 所以只在 CI 红 (实测连红三次推送, 两个 Python 版本都挂在这一条)。全局样式与本条
    # 要断言的东西 (逐树 try/except + 摘掉死树) 无关, 打掉即可; 它没有别的副作用。
    monkeypatch.setattr(batch_common, "_configure_tree_style", lambda: None)

    app = _App()
    app._dead_tree = _DeadTree()
    app._live_tree = _LiveTree()
    saved = set(_TREE_ATTRS)
    _TREE_ATTRS.update({"_dead_tree", "_live_tree"})
    try:
        batch_common.refresh_theme(app)          # 不许抛
        assert app._live_tree.tags, "悬垂树把活树的重染一起带走了"
        assert set(app._live_tree.tags) == set(_TAG_COLORS)
        assert "_dead_tree" not in _TREE_ATTRS, "死树没被摘掉, 下一轮还会再抛一次"
    finally:
        _TREE_ATTRS.clear()
        _TREE_ATTRS.update(saved)


def test_each_blocking_dimension_reads_its_own_shared_tag_set():
    """两个拦截维度各有各的颜色, 判据都不许在 GUI 里另抄一份清单.

    ``BLOCKING_RISK_TAGS`` 是两者的并集, 同时驱动 ``view_exclusion_reason`` 与
    ``_review_bucket``; 行色再抄一份, 下一次调维度时两边就会静默分叉 —— 与
    「GUI 曾自带一份只覆盖 14/18 的事件配色表」同源。
    模型适用性/标的风险**都不在**内: 它们是永久属性, 查完还是那样 (实测模型适用性
    在 72% 的债上都亮), 收进来行色就变回 79% 的垃圾桶。
    """
    assert TRADABILITY_RISK_TAGS | DATA_QUALITY_RISK_TAGS == BLOCKING_RISK_TAGS
    assert not (TRADABILITY_RISK_TAGS & DATA_QUALITY_RISK_TAGS)

    for tag in sorted(TRADABILITY_RISK_TAGS):
        row = {"bond_code": "X", "status": "ok", "risk_tags": [tag]}
        assert _resolve_row_tag(row) == "blocked", f"{tag} 属可交易性维却没染上 blocked"

    for tag in sorted(DATA_QUALITY_RISK_TAGS):
        row = {"bond_code": "X", "status": "ok", "risk_tags": [tag]}
        assert _resolve_row_tag(row) == "nodata", f"{tag} 属数据质量维却没染上 nodata"

    for tag in sorted(RISK_TAG_DIMENSION):
        if tag in BLOCKING_RISK_TAGS:
            continue
        row = {"bond_code": "X", "status": "ok", "risk_tags": [tag]}
        assert _resolve_row_tag(row) is None, f"{tag} 不属拦截集却染了色"


def test_a_broken_pipeline_does_not_look_like_a_dying_market():
    """数据质量必须**静音**, 不许跟可交易性共用警报色.

    频次上今天只有 3 : 1 (临近摘牌 3 只 / 先锋转债的 无偏差+无市价 1 只), 分不分
    看着无所谓 —— 真正的理由是**降级场景**: 数据源抖一下, ``无市价`` 可以一次命中
    几百行。它们要是和 ``临近摘牌`` 共用红粗体, 一屏红色会被读成"市场出事了",
    而真相是"取数挂了, 去跑 cb-data-doctor"。

    方向也必须是静音而不是第二个警报色: 数据质量行在这一页上无事可做 (是噪声),
    可交易性行则是最需要动作的一档 (临近摘牌 = 30 天内必须卖掉)。
    """
    trading_risk = {"bond_code": "A", "status": "ok", "risk_tags": ["临近摘牌"]}
    broken_data = {"bond_code": "B", "status": "ok", "risk_tags": ["无市价"]}
    assert _resolve_row_tag(trading_risk) != _resolve_row_tag(broken_data)
    assert _resolve_row_tag(broken_data) == "nodata"

    # 定价失败是数据质量的极端档 —— 整行一个数字都没有, 同样归静音。
    # (老代码里 `failed` 本来就是 TEXT_DIM, 这是恢复而不是新发明。)
    assert _resolve_row_tag(
        {"bond_code": "C", "status": "provider error: timeout"}) == "nodata"


def test_tradability_outranks_a_data_gap_on_the_same_row():
    """同时命中两维时染可交易性 —— 可动作的那条压过维护提示.

    实测今天 0 行同时命中, 但优先级必须是显式的: 一只 ``临近摘牌`` 且当天恰好
    取不到价的债, 该看见的是"30 天内必须卖掉", 不是"数据缺了"。
    """
    row = {"bond_code": "X", "status": "ok",
           "risk_tags": ["无市价", "临近摘牌"]}
    assert _resolve_row_tag(row) == "blocked"


def test_a_never_priced_watchlist_row_is_not_painted_as_broken():
    """"从没算过"不是"这行数字是坏的" —— 关注池未定价行必须保持无色.

    ``no_market`` / ``unpriced`` 明确不进行色: 未上市新债没有市价是**天然状态**
    (实测关注池 6 行里有 3 只), 把它染成"别信它"就是把 ``_resolve_row_tag`` 里
    那条刻意的"没有 status 就 return None"拿掉。这两档的区分由「数据状态」列的
    五档文案承载 —— 那正是 ``_price_state`` 存在的理由。
    """
    never_priced = {"bond_code": "123999.SZ", "bond_name": "未算转债",
                    "listing_date": market_today() - timedelta(days=90)}
    assert never_priced.get("status") is None
    assert _resolve_row_tag(never_priced) is None

    failed = dict(never_priced, status="provider error: timeout")
    assert _resolve_row_tag(failed) == "nodata"


def test_row_colour_treats_nan_as_missing():
    """落盘 ``null`` 读回来是 **NaN**, 而 ``NaN is not None`` 为真.

    ``watchlist_cache._NAN_FIELDS`` 收了 ``cheapness_percentile``, 实测关注池
    热缓存 6 行该字段全是 ``nan``。与 ``safe_date`` / ``pandas.NaT`` 那条约定
    (NaT 是 datetime 子类且 ``bool(NaT)`` 为真) 是同一个坑的又一次出现。
    """
    row = {"bond_code": "X", "status": "ok", "risk_tags": [],
           "deviation": float("nan"), "relative_deviation": float("nan"),
           "cheapness_percentile": float("nan")}
    assert _resolve_row_tag(row) is None


def test_legend_names_exactly_the_colours_the_table_can_show():
    """图例与 ``_TAG_COLORS`` 键集必须同步.

    与 ``WATCH_REFRESH_LABEL`` 那次同构: 改了 tag 但图例里留着一个**过期**的
    档位名, 用户对着表找一个不存在的颜色。所以比对的是**集合相等**而不是扫字面量
    —— 扫字面量抓不到"留着旧名字"这种真实形态。

    「无色 = …」那一句**已按用户决策去掉** (2026-08-29)。此前它是硬要求, 理由是
    实测默认落地视图 284 行 ``blocked`` 为 0、一页全无色是设计意图, 没有那句话
    它和"配色坏了"长得一模一样。现在区分这两者靠的是"悬停表区标题看得到这三档
    确实存在"; 换来的是不再为绝大多数行的**常态**常驻一行说明。
    """
    assert set(_TAG_LEGEND) == set(_TAG_COLORS)
    legend = row_colour_legend()
    for label in _TAG_LEGEND.values():
        assert label in legend


def test_legend_segments_wear_the_colours_they_describe():
    """图例**演示**颜色, 不用文字描述颜色.

    上一版的 tooltip 是纯文本, 只能写「红色加粗 = 买卖受限」; 现在逐行按各自的
    颜色与字重渲染, 那张色名表 (``_TAG_APPEARANCE``) 随之删掉 —— 留着就是第二份
    会过期的展示词表, 而在一行本来就是红色加粗的字前面写「红色加粗 =」, 说的是
    读者眼前就能看见的东西。
    """
    from convertible_bond.gui.tabs.batch_common import (
        _BOLD_TAGS, _ITALIC_TAGS, _TAG_COLORS as COLORS, row_colour_legend_segments,
    )
    import convertible_bond.gui.tabs.batch_common as mod

    assert not hasattr(mod, "_TAG_APPEARANCE"), "色名表该随 tooltip 上色一起删掉"

    segments = row_colour_legend_segments()
    coloured = {seg[1]: seg for seg in segments if seg[1] is not None}
    # 每一档都得真的带上自己的颜色 —— 少一档就是"表里有这个色, 图例里没有"
    assert set(coloured) == set(COLORS.values())

    by_label = {seg[0].strip(): seg for seg in segments}
    for tag, label in _TAG_LEGEND.items():
        _text, color, font = by_label[label]
        assert color == COLORS[tag]
        # 第二通道跟着表走: 颜色在灰阶/单色打印/红绿色觉缺陷下信息量为 0
        if tag in _BOLD_TAGS:
            assert font[-1] == "bold"
        elif tag in _ITALIC_TAGS:
            assert font[-1] == "italic"
        else:
            assert len(font) == 2
    # 三档请求的字体互不相同 —— 但**请求不等于渲染**, 见下一条用例


def test_legend_italic_does_not_actually_render_on_macos_cjk():
    """守住一个**已知失效**, 免得文档里再写"三档不靠色相也分得开".

    实测 (Tk 8.6.15 / macOS): PingFang SC 没有斜体字面且 Tk 不合成 →
    ``actual()["slant"] == "roman"``; 换 Menlo 虽然 ``actual()`` 报 italic, 但它没有
    CJK 字形, 中文走回落而回落不倾斜 —— 正体/斜体量宽完全相等。所以改字族只会让这条
    用例变绿而用户看到的没变, 那是用绿测试担保一句假话。

    这条用例的作用是**把测量结果钉住**: 断言的是渲染态 (``actual()``) 而不是请求的
    元组 —— 上一版就是因为只比元组, 一个显式请求了却从不生效的属性一路全绿。
    """
    import tkinter
    import tkinter.font as tkfont

    from convertible_bond.gui.theme import FONT_FAMILY, FONT_MONO

    try:
        root = tkinter.Tk()
        root.withdraw()
    except Exception:  # pragma: no cover - CI 无显示环境
        pytest.skip("需要 Tk 显示环境才能量字体")
    try:
        if FONT_FAMILY != "PingFang SC":      # 非 macOS: 结论不适用, 不做断言
            pytest.skip(f"本条只描述 macOS 的 PingFang SC, 当前是 {FONT_FAMILY}")
        assert tkfont.Font(font=(FONT_FAMILY, 12, "bold")).actual()["weight"] == "bold"
        assert tkfont.Font(font=(FONT_FAMILY, 12, "italic")).actual()["slant"] == "roman"
        # 换 FONT_MONO 不是出路: actual() 好看了, 中文照样不斜 (量宽相等)
        upright = tkfont.Font(font=(FONT_MONO, 12)).measure("数据缺失或定价失败")
        slanted = tkfont.Font(font=(FONT_MONO, 12, "italic")).measure("数据缺失或定价失败")
        assert upright == slanted
    finally:
        root.destroy()


def test_legend_plain_text_is_derived_from_the_segments():
    """纯文本版必须由分段拼出来, 不许另写一份字面量 (改档位名后会留下过期的那份)."""
    from convertible_bond.gui.tabs.batch_common import row_colour_legend_segments

    text = row_colour_legend()
    for seg_text, _color, _font in row_colour_legend_segments():
        assert seg_text in text


def test_legend_does_not_call_a_still_tradable_bond_untradable():
    """``blocked`` 收的是 临近摘牌 / 正股跌停 这类"买不到 / 快买不到了",
    「临近摘牌」今天照样买得到 —— 叫它「不可交易」是把最重的那一档当成了全部。"""
    assert _TAG_LEGEND["blocked"] == "买卖受限"


def test_empty_view_note_explains_why_without_faking_a_pool_wide_number():
    """空视图的文案要说**为什么**空, 而且不许把某一行的数字冒充成全池口径.

    三件事分开:

    ① **通用文案是句废话, 而且会指向无效动作**。「换个视图或点刷新重算」既没说为什么
       空, 又在建议一个未必奏效的操作 —— 实测「转股折价」的判据是 转股溢价 < −3%,
       而全池最低 **−0.3%**、中位 **+58.4%**, 重算多少次都还是 0 行。所以全池理由
       一致时要逐字引用那个理由。

    ② **理由串带行内数字, 取众数展示就是造假**。「相对市场中位 +17.9pp, 未便宜过
       5pp」「双低 205 排第 44/283」里的数都是**那一行**的; 挑一条当"全池的原因"
       会渲染出一个看上去完全正常的假口径。所以只在**全集只有一个理由**时才引用,
       混合理由退回通用文案。

    ③ **``None`` 是"这行属于该视图", 与"视图是空的"自相矛盾**。「综合机会」从不排除
       任何行, 它的理由集恰是 ``{None}`` —— 少一道闸就会渲染出字面的 "None"。

    另有一条**结构性**约束: 理由必须问 ``view_exclusion_reason``, 不许在文案函数里按
    视图名分支。上一版给「下修优势」写过特判, 那个视图整体删除之后特判成了死代码 ——
    "每加一个视图加一个 if" 的写法必然烂掉, 而判据的单一事实源本来就在 batch_pricing。
    """
    import inspect

    from convertible_bond.batch_pricing import BATCH_REVIEW_VIEWS

    class _App:
        pass

    def note(rows, view):
        app = _App()
        app._batch_all_results = rows
        return batch_tab._empty_view_note(app, view)

    # ① 全池同一个理由 → 逐字引用
    discount = [{"status": "ok", "risk_tags": []} for _ in range(284)]
    text = note(discount, "转股折价")
    assert "未出现转股折价标签" in text and "284" in text
    assert "刷新重算" not in text, "理由已经说清楚了, 不该再指一个不奏效的动作"

    # ② 混合理由 → 通用文案, 且**一个行内数字都不许漏出来**
    mixed = [
        {"status": "ok", "risk_tags": [], "relative_deviation": 0.179,
         "cheapness_rank": 200.0, "cheapness_rank_total": 284.0, "confidence": "高"},
        {"status": "failed", "risk_tags": []},
    ]
    text = note(mixed, "低估候选")
    assert "刷新重算" in text
    for leaked in ("17.9", "+17.9pp", "200", "定价未成功"):
        assert leaked not in text, f"混合理由不该漏出单行口径 {leaked!r}"

    # ③ None 不许渲染出来 (「综合机会」的理由集恰是 {None})
    text = note(discount, "综合机会")
    assert "None" not in text and "刷新重算" in text

    # ④ 没有结果 / 连属性都还没建 —— 建表早于第一次批量, 不许抛
    assert note([], "双低")
    assert batch_tab._empty_view_note(_App(), "双低")

    # ⑤ 结构性: 不许按视图名分支。判据**不能**只扫 BATCH_REVIEW_VIEWS 的成员 ——
    #    烂掉的那个特判分支到的正是一个**已经删除**的视图名 ("下修优势"), 它早已不在
    #    这个元组里, 按成员扫等于永远抓不到真实故障形态。所以扫的是"拿 name 和任意
    #    字符串字面量比较"这个**形状**。
    import re

    src = inspect.getsource(batch_tab._empty_view_note)
    body = src.split('"""')[-1]          # 掐掉 docstring, 那里当然会点名视图
    branch = re.search(r'name\s*(?:==|!=|\bin\b)\s*[({\[]?["\u0027]', body)
    assert branch is None, (
        f"按视图名分支会随视图增删烂掉 (命中 {branch.group(0)!r} ), "
        "理由要问 view_exclusion_reason")
    assert "view_exclusion_reason" in body, "理由的单一事实源是 view_exclusion_reason"
    assert BATCH_REVIEW_VIEWS            # 视图表还在, 只是不该在这里被逐个点名


def test_view_display_label_is_split_from_the_frozen_view_name():
    """「综合机会」只改**展示名**, 底层字面量逐字冻结.

    为什么必须改: 这个视图的 ``view_exclusion_reason`` 直接 ``return None`` —— 它就是
    **不过滤的全池**, 既不"综合"也不排"机会"。而名字里的「机会」指的是
    ``opportunity_score``, 那个字段已于 2026-08-29 整体删除, 视图名成了唯一的残留引用。
    实测它也不是一个独立的机会排序: 按相对偏差升序的**前 43 行与「低估候选」重合
    43/43**, 独立信息全在第 44 行往后。

    为什么**不能**直接改串: ``"综合机会"`` 是 ``ScoreStrategyConfig.selection_view``
    的默认值, 随策略配置落进快照并被 ``_canonical_view_name`` 回读 —— 改它是兼容性
    破坏。与「模型高估离群」→「市价远高于模型价」同一条解法。

    三件事:

    ① **冻结锚**: 底层名还在, 且仍是策略层默认值。
    ② **往返闭合**, 且展示名不许撞上另一个视图的底层名 —— 撞了之后
       ``batch_view_from_label`` 会静默解析成**另一个视图**, 页面照常渲染, 只是筛子
       换了一把。
    ③ **每个面向用户的出口都要过 ``batch_view_label``**。少接一处的表现不是报错, 是
       "菜单叫「全池」而状态栏叫「综合机会」" —— 同一个东西两个名字, 用户无从判断
       是不是两回事。
    """
    import inspect

    from convertible_bond.batch_pricing import (
        BATCH_REVIEW_VIEWS,
        BATCH_VIEW_DISPLAY_LABEL,
        batch_view_from_label,
        batch_view_label,
    )
    from convertible_bond.strategy_backtest import ScoreStrategyConfig

    # ① 冻结锚: 底层名没动, 策略默认值仍指向它
    assert "综合机会" in BATCH_REVIEW_VIEWS
    assert ScoreStrategyConfig().selection_view == "综合机会"
    assert BATCH_VIEW_DISPLAY_LABEL["综合机会"] == "全池"

    # ② 往返闭合 + 展示名不许撞上另一个视图的底层名
    for view in BATCH_REVIEW_VIEWS:
        label = batch_view_label(view)
        assert batch_view_from_label(label) == view
        if label != view:
            assert label not in BATCH_REVIEW_VIEWS, (
                f"展示名 {label!r} 撞上了另一个视图的底层名, 回读会静默换一把筛子")
    assert batch_view_from_label("不存在的视图") is None
    # 旧快照/旧配置里存的是底层名, 也必须认
    assert batch_view_from_label("综合机会") == "综合机会"

    # 菜单标签带 " (N)" 计数后缀, 回读要能剥掉
    assert batch_tab._canonical_view_name("全池 (284)") == "综合机会"
    assert batch_tab._canonical_view_name("综合机会") == "综合机会"
    assert batch_tab._canonical_view_name("") == "综合机会"

    # ③ 面向用户的出口都接上了 (菜单 / 状态行 / 空态文案)
    for fn in (batch_tab._refresh_view_menu_labels,
               batch_tab._render_table,
               batch_tab._empty_view_note):
        assert "batch_view_label" in inspect.getsource(fn), (
            f"{fn.__name__} 没走展示名, 会和菜单显示两个名字")

    # 空态文案用展示名, 但**查判据仍用底层名** —— 两者混用会让 view_exclusion_reason
    # 收到一个它不认识的串, 静默退回「综合机会」的口径 (那个视图从不排除任何行)。
    class _App:
        pass

    app = _App()
    app._batch_all_results = [{"status": "ok", "risk_tags": []} for _ in range(7)]
    note = batch_tab._empty_view_note(app, "综合机会")
    assert "全池" in note and "综合机会" not in note
    # 判据没走岔: 「综合机会」全池理由集恰是 {None}, 该退回通用文案而不是引用 "None"
    assert "None" not in note


def test_each_view_shows_the_column_its_criterion_is_built_on():
    """切到一个视图, 它的**判据量**必须在表上 —— 否则是按不可见的数筛选.

    「简洁」是**全池视角**下的决策位, 它排掉「可信度」「定价状态」的理由是实测在默认
    视图里这两列近乎常量。但那个理由**随视图变**, 而列预设此前不随视图变:

    · 「需复核」的判据恰是 status / 拦截标签 / 置信度 三条, 其中两条没有列。实测今天
      那 1 只 (123270.SZ 盛德转债) **完全是因为 `confidence == "低"`** 进来的
      (全池: 定价失败 0 · 置信度低 1 · 带拦截标签 0) —— 表上一行, 没有任何一列说得出
      它为什么在那儿。
    · 「转股折价」的判据是 `转股溢价 < −3%`, 而那一列只在「完整」里。

    三件事:

    ① **补列只在该视图生效**, ``_BATCH_COLS_SIMPLE`` 本身一个字节不动 —— 全局加进去
       就是让不需要它的行陪着占宽 (那正是
       ``test_simple_preset_is_the_decision_view_not_a_diagnostic_one`` 钉住的东西)。
    ② **列序从 ``_BATCH_COLS_FULL`` 取, 不追加到末尾**。列序是"读者的提问次序"且价格块
       必须连成一片 —— 「转股溢价(%)」就落在价格块里, 追加会把它甩到「评级」后面, 与它
       要对照的「市价」隔开十列; 「可信度」的对象 (理论价) 也是**由列序承载**的。
    ③ **这套组装依赖两条不变量**: 简洁 ⊆ 完整, 且简洁的列序等于它在完整里的相对序。
       任一条破了, ``_batch_schema_for`` 会**静默**丢列或重排 —— 不报错, 只是表变了样。
    """
    from convertible_bond.batch_pricing import BATCH_REVIEW_VIEWS

    simple = [name for name, _ in batch_tab._BATCH_COLS_SIMPLE]
    full = [name for name, _ in batch_tab._BATCH_COLS_FULL]

    # ③ 先钉不变量 —— 下面两条都架在它上面
    assert set(simple) <= set(full), "简洁有完整没有的列, 组装会 KeyError 或丢列"
    assert [n for n in full if n in set(simple)] == simple, (
        "简洁的列序与完整的相对序不一致, 补列会静默重排整张表")

    # ① 补列只在该视图生效, 且不改简洁本身
    assert [n for n, _ in batch_tab._batch_schema_for("简洁", "低估候选")] == simple
    assert [n for n, _ in batch_tab._batch_schema_for("简洁", None)] == simple
    assert batch_tab._batch_schema_for("完整", "需复核") == batch_tab._BATCH_COLS_FULL

    # ② 判据列到位, 且落在完整预设给它的位置上 (不是末尾)
    review = [n for n, _ in batch_tab._batch_schema_for("简洁", "需复核")]
    assert "可信度" in review and "定价状态" in review
    assert review[review.index("理论价") + 1] == "可信度", "可信度的对象由列序承载"
    # 「全池」的那一列是**排序量**不是判据量: 它按上市日倒序排, 第一屏是刚上市的
    # 新债 —— 不显示上市日的话, 页面上没有任何一列说得出它们为什么排在最前。
    pool = [n for n, _ in batch_tab._batch_schema_for("简洁", "综合机会")]
    assert "上市日" in pool
    assert pool[pool.index("上市日") + 1] == "剩余(年)", "它属于基础条款块"

    discount = [n for n, _ in batch_tab._batch_schema_for("简洁", "转股折价")]
    assert "转股溢价(%)" in discount
    assert discount[discount.index("转股溢价(%)") + 1] == "市价", "它属于价格块"
    for view in (None, "需复核", "转股折价"):
        cols = [n for n, _ in batch_tab._batch_schema_for("简洁", view)]
        assert cols == [n for n in full if n in set(cols)], f"{view} 的列序被打乱了"

    # 补进来的列必须在三张表里都登记过 —— 缺权重会静默走默认 1.0 (与「名称」同级),
    # 缺 getter 直接 KeyError, 缺对齐则表头与内容不同向。
    for view, extra in batch_tab._VIEW_KEY_COLUMNS.items():
        assert view in BATCH_REVIEW_VIEWS, f"{view} 不是视图名, 这条补列永远不会生效"
        for name in extra:
            assert name in full, f"{name} 不在完整预设里, 取不到列宽"
            assert name not in simple, f"{name} 已经在简洁里, 这条登记是死条目"
            assert name in batch_tab._BATCH_COL_GETTERS
            assert name in batch_tab._BATCH_COL_STRETCH_WEIGHTS



def test_watchlist_pages_never_call_it_a_position():
    """关注池是**纯研究关注清单, 不记持仓** (2026-08-25 拍板的口径1).

    没有成本价 / 份额 / 浮盈, 也不打算有。而"持仓"这个词向读者承诺的正是那一套
    语义 —— 它出现在默认落地页最显眼的表标题和常驻摘要条上, 是全 app 最容易被
    读成"这里记着我买了多少"的两处。

    **只扫用户看得见的字符串**, 不扫注释与 docstring: 解释"为什么这里不叫持仓"的
    注释本身就得写出这个词, 扫源码文本会把它们一并判红 —— 那是"为了规则改文档",
    与 AGENTS 里 tooltip 那条来回摆过两次的教训同源。

    也只扫关注池那两个文件: 策略页与回测页确实建模持仓 (调仓/权重/归因),
    那里用这个词是对的。
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "convertible_bond" / "gui" / "tabs"
    for name in ("home.py", "batch_watchlist.py"):
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.ClassDef,
                                 ast.FunctionDef, ast.AsyncFunctionDef))
            and node.body and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        }
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in docstrings):
                assert "持仓" not in node.value, (
                    f"{name}:{node.lineno} 的用户可见文案里有「持仓」"
                    " —— 关注池不记持仓 (口径1)")


def test_batch_page_lands_on_the_full_pool():
    """批量页默认落「全池」, 而且 canonical 与 display var 不许各说各话.

    为什么不再落「低估候选」: 那是把一个**判断**放在落地页上。它只有 43/284 只, 而
    "今天有没有便宜货"本身随周期摆动 (中位偏差实测 +0.4% ~ +21.6%) —— 谷底时判据
    诚实归零, 默认打开就是一张空表。那正是绝对机会分阈值时代踩过的坑 (2026-08 主池
    280 只只剩 1 只候选), 换成横截面口径只是让它变罕见, 没有消除。分母做落地页永远不空。

    **两个 var 必须同源**。``v_batch_view`` 存冻结名、``_batch_view_display_var`` 存
    展示名, 各写一个字面量就会分叉 —— 表现不是报错, 是"菜单显示 A 而表里是 B 的行",
    用户无从判断哪个说了算。所以 display var 要由 ``batch_view_label`` 算出来。
    """
    import inspect

    from convertible_bond.batch_pricing import BATCH_REVIEW_VIEWS, batch_view_label

    src = inspect.getsource(batch_tab.build)
    assert 'app.v_batch_view = ctk.StringVar(value="综合机会")' in src, (
        "默认视图不是「全池」的冻结名「综合机会」")
    assert ('app._batch_view_display_var = ctk.StringVar('
            'value=batch_view_label("综合机会"))') in src, (
        "display var 写死了字面量, 会与 canonical 分叉")

    # 落地页的冻结名必须是真视图, 且它的展示名回得来
    assert "综合机会" in BATCH_REVIEW_VIEWS
    assert batch_tab._canonical_view_name(batch_view_label("综合机会")) == "综合机会"

    # 落地页不许是一个会空掉的视图: 全池的判据是"没有判据"
    from convertible_bond.batch_pricing import view_exclusion_reason
    for row in ({}, {"status": "failed"}, {"status": "ok", "risk_tags": ["无市价"]}):
        assert view_exclusion_reason(row, "综合机会") is None, (
            "全池开始排除行了 —— 落地页就可能空")



def test_suppressed_tags_are_never_blocking_or_opportunity():
    """被列承载而抑制渲染的标签, **不许**是拦截档或机会信号。

    两条规则各有各的理由:
      · **拦截标签**: 行已经被染成红色/灰色, 标签是它唯一的解释 —— 抑制它等于让用户
        看见一行红色却找不到原因 (与「一个消失的控件和一个坏掉的控件长得一模一样」同源)。
      · **机会信号**: "为什么值得看这只债"正是标签列存在的意义, 挡掉它标签列就只剩坏消息。

    没有这条守护, 以后往 ``_TAG_CARRIER_COLUMN`` 里加一个红行标签, 红行就没解释了 ——
    而那是静默的: 表照常渲染, 只是少了一个词。
    """
    from convertible_bond import batch_pricing

    for tag, column in batch_common._TAG_CARRIER_COLUMN.items():
        assert tag in batch_pricing.RISK_TAG_DIMENSION, f"{tag} 不是已登记的标签"
        assert tag not in batch_pricing.BLOCKING_RISK_TAGS, (
            f"{tag} 是拦截标签, 抑制它会让染色的行失去唯一的解释")
        assert batch_pricing.RISK_TAG_DIMENSION[tag] != batch_pricing.DIM_OPPORTUNITY, (
            f"{tag} 是机会信号, 那正是标签列存在的意义")
        assert isinstance(column, str) and column, f"{tag} 的承载列名为空"


def test_tag_suppression_is_per_preset_not_global():
    """按预设判, 不是一刀切 —— 「较高HV」在简洁里必须留着。

    「正股σ(%)」只在完整预设; 简洁页上「较高HV」是唯一的波动率线索 (实测 35 行)。
    一刀切删掉正是评审指出的"删过头": 那 35 行会一个字都不剩。
    """
    from convertible_bond.gui.tabs import batch as batch_tab

    simple = {name for name, _ in batch_tab._BATCH_COLS_SIMPLE}
    full = {name for name, _ in batch_tab._BATCH_COLS_FULL}
    watchlist, _ = watchlist_tab.watchlist_columns()

    row_tags = ["低评级", "较高HV", "短久期"]

    # 「评级」「剩余(年)」三个预设都有 → 那两个标签处处抑制
    for cols in (simple, full, set(watchlist)):
        rendered = batch_common._format_tags(row_tags, columns=cols)
        assert "低评级" not in rendered
        assert "短久期" not in rendered

    # 而「较高HV」只在完整里抑制 —— 简洁/关注池没有 σ 列
    hv = batch_pricing.risk_tag_label("较高HV")          # 「正股波动偏高」
    assert hv in batch_common._format_tags(row_tags, columns=simple)
    assert hv in batch_common._format_tags(row_tags, columns=set(watchlist))
    assert hv not in batch_common._format_tags(row_tags, columns=full)

    # 不传列集 = 不知道渲染了什么 → 一个都不挡 (保守的那一侧)
    assert batch_common._format_tags(row_tags).count("/") == 2


def test_suppression_does_not_touch_the_underlying_tag_set():
    """抑制是**纯展示**: 行色 / 置信度 / 分桶 / 策略排除集照读原集。

    这是这次改动与"删标签"的全部区别 —— 删标签会连带改四个通道 (实测
    review_notes 123/284 行、model_signal_status 74/284 行), 抑制一个都不碰。
    """
    from convertible_bond import batch_pricing
    from convertible_bond.gui.tabs import batch as batch_tab

    row = {"bond_code": "x", "status": "ok", "risk_tags": ["低评级", "短久期", "余额清零"]}
    full = {name for name, _ in batch_tab._BATCH_COLS_FULL}

    # 表上只剩「余额清零」(拦截档, 不抑制) —— 渲染成它的展示名
    assert (batch_common._format_tags(row["risk_tags"], columns=full)
            == batch_pricing.risk_tag_label("余额清零") == "余额已清零")
    # 但行色仍由完整的 risk_tags 决定
    assert batch_common._resolve_row_tag(row) == "blocked"
    # 底层集合一个字节没动
    assert row["risk_tags"] == ["低评级", "短久期", "余额清零"]
    assert set(row["risk_tags"]) & batch_pricing.BLOCKING_RISK_TAGS == {"余额清零"}


def test_both_tables_actually_pass_their_column_set_to_the_tag_cell():
    """两张表都必须把**本次渲染的列集**传给标签格 —— 不传就退回"一个都不挡"。

    这条只能在源码上钉: GUI 在无头环境起不来, 而漏传的表现是"抑制没生效", 表照常渲染、
    测试照常绿 (实测这个变异体第一轮活了下来)。用 AST 查关键字实参, 不扫源码文本 ——
    文本扫描会把解释性注释判红, 那是为了让规则变绿去改文档 (库内踩过一次)。
    """
    import ast
    import inspect
    import textwrap

    from convertible_bond.gui.tabs import batch as batch_tab
    from convertible_bond.gui.tabs import batch_watchlist as wl_tab

    for module, func_name in ((batch_tab, "_render_table"),
                              (wl_tab, "_render_watchlist_table")):
        src = textwrap.dedent(inspect.getsource(getattr(module, func_name)))
        calls = [
            node for node in ast.walk(ast.parse(src))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name) and node.func.id == "_format_tags"
        ]
        assert calls, f"{module.__name__}.{func_name} 里找不到 _format_tags 调用"
        for call in calls:
            kwargs = {kw.arg for kw in call.keywords if kw.arg}
            assert "columns" in kwargs, (
                f"{module.__name__}.{func_name} 的 _format_tags 没传 columns= —— "
                f"标签抑制会静默失效, 表照常渲染")


def test_every_registered_tag_has_a_display_name():
    """展示名必须**全覆盖** —— 这是 2026-08-31 的决定, 不是巧合。

    此前 3/32 有名字, 那是最坏的中间态: 3 个精心命名 + 29 个把内部变量名
    (「数据缺口」「模型溢价高」「无HV」) 直接渲染给用户。要么全补齐, 要么承认内部名
    就是展示名 —— 选了前者, 所以这条钉住"新增标签必须同时给名字"。

    命名规则 (违反会被下面几条断言接住):
      · 必须点明**度量的是谁** —— 「无HV」→「正股σ缺失」(HV 是内部缩写);
      · 带阈值的档位把**阈值写进名字** —— 「低评级」→「评级低于AA-」("低"低到哪?);
      · 不许以「模型」开头、不许出现无主语的「高估/低估」、不许暗示动作 (「核」)。
    """
    missing = set(RISK_TAG_DIMENSION) - set(batch_pricing.RISK_TAG_DISPLAY_LABEL)
    assert not missing, f"这些标签没有展示名, 会把内部变量名渲染给用户: {sorted(missing)}"

    orphans = set(batch_pricing.RISK_TAG_DISPLAY_LABEL) - set(RISK_TAG_DIMENSION)
    assert not orphans, f"展示名表里有未登记维度的标签: {sorted(orphans)}"

    for tag, shown in batch_pricing.RISK_TAG_DISPLAY_LABEL.items():
        assert shown and shown.strip() == shown, f"{tag} 的展示名有空白问题"
        assert not shown.startswith("模型"), f"{shown}: 「模型」不是主语"
        assert "高估" not in shown and "低估" not in shown, f"{shown}: 无主语的高估/低估"
        assert "核" not in shown, f"{shown}: 在暗示动作, 而页面上有「需复核」视图"
        # 内部缩写不许出现在展示名里 —— HV 是 hist_vol 的简写, 表上那列叫「正股σ(%)」
        assert "HV" not in shown, f"{shown}: HV 是内部缩写"


def test_tag_column_is_wide_enough_for_the_longest_rendered_name():
    """标签列宽要放得下**实际渲染**的名字, 不是放得下最短的那个。

    展示名全补齐 (2026-08-31) 之后名字变长了 —— 简洁预设的 p90 从 12 字符涨到 15,
    所以列宽同步从 170/180 调到 220px。这条钉住"名字变长时列宽要跟上":
    截断会让最有信息量的那几行反而读不全, 而它不报错、只是看着少了几个字。
    """
    from convertible_bond.gui.tabs import batch as batch_tab
    from convertible_bond.gui.tabs import batch_watchlist as wl_tab

    wl_headers, wl_widths = wl_tab.watchlist_columns()
    presets = {
        "简洁": dict(batch_tab._BATCH_COLS_SIMPLE),
        "完整": dict(batch_tab._BATCH_COLS_FULL),
        "关注池": dict(zip(wl_headers, wl_widths)),
    }
    # **逐个预设查** —— 合成一个 dict 再查会让"只有一个预设退回窄列"被后面的覆盖掉
    # (实测这个变异体正是这样活下来的)。
    for name, cols in presets.items():
        assert cols["标签"] >= 220, (
            f"{name}预设的标签列只有 {cols['标签']}px —— 展示名全补齐之后放不下, "
            f"最长的组合是「贴近转股价值 / 市价远低于市场中位 / 正股波动极高」")

    # 单个展示名不许长到一个都放不下 (220px 约 16 个中文字符)
    for tag, shown in batch_pricing.RISK_TAG_DISPLAY_LABEL.items():
        assert len(shown) <= 12, f"{tag} 的展示名 {shown!r} 有 {len(shown)} 字, 单个就要截断"


# ── GUI 批量页: 排序 / 覆盖率闸 / 事件横幅 / 展示名 的守护 ──────────────
def test_ordinal_columns_sort_by_their_own_ladder_not_by_codepoint():
    """「评级」「可信度」是**有序分类**列, 既不是数也不该按字符串排。

    ``_attach_column_sort`` 的判据是"至少一半 present 值能 float", 而这两列全部返回
    None, 于是整列静默落进 ``str().lower()`` 分支 —— 与 AGENTS 记的「线上 123% 排在
    线上 3% 前面」是同一个失效, 只是这次连中文前缀都没有, 更看不出来:

      · 评级 ASCII 降序 = CC > BBB+ > AA- > AA+ > AA > A+ —— 垃圾级排最上面, 而
        ``+`` (0x2B) < ``-`` (0x2D) 让 AA+ 排在 AA- 下面
      · 置信度中文码点序是 中(0x4E2D) < 低(0x4F4E) < 高(0x9AD8), 于是「需复核」视图
        专门加出来解释"这行为什么在这儿"的那一列, 把「低」排在中间而不是任一端

    档位表必须复用 ``data_providers.base.CREDIT_RATING_RANK`` —— 仓库里曾有两份逐字
    重复的 19 档表 (``cli/sync_ratings`` 与 ``cli/data_doctor``), GUI 再抄第三份就是
    在等它们分叉。
    """
    from convertible_bond.data_providers.base import CREDIT_RATING_RANK
    from convertible_bond.gui.tabs.batch_common import (
        _ORDINAL_SORT_SCALES,
        _parse_sortable_number,
    )

    # 前提: 这些值确实解析不成数 —— 否则这条用例测的是别的东西
    for v in ("AA+", "AA-", "BBB+", "CC", "高", "中", "低"):
        assert _parse_sortable_number(v) is None

    ratings = ["AA+", "AA", "AA-", "A+", "BBB+", "CC"]
    scale = _ORDINAL_SORT_SCALES["评级"]
    assert scale is CREDIT_RATING_RANK, "GUI 抄了第三份评级表"
    assert sorted(ratings, key=lambda x: scale[x], reverse=True) == [
        "AA+", "AA", "AA-", "A+", "BBB+", "CC"]
    # 字符串序会给出这个 —— 钉住它, 免得有人"顺手"把 scale 去掉
    assert sorted(ratings, reverse=True) == ["CC", "BBB+", "AA-", "AA+", "AA", "A+"]

    conf = _ORDINAL_SORT_SCALES["可信度"]
    assert sorted(["高", "中", "低"], key=lambda x: conf[x], reverse=True) == [
        "高", "中", "低"]
    assert sorted(["高", "中", "低"]) == ["中", "低", "高"]

    # 全部 19 档都要在表里 —— 少一档那一档就沉底, 不报错
    assert set(scale) == set(CREDIT_RATING_RANK)


def test_credit_rating_ladder_has_exactly_one_definition():
    """评级档位表只许有一份。

    ``cli/sync_ratings.RATING_ORDER`` 与 ``cli/data_doctor._RATING_ORDER`` 曾是两份
    **逐字重复**的 19 档元组 (当时恰好一致)。这类重复不会一起被改 —— 本仓库已经在
    ``terms_as_of`` (三份, 只修了两份) 和事件展示词表 (GUI 私有一份, 漏 4 个类型)
    上各栽过一次。
    """
    from convertible_bond.cli import data_doctor, sync_ratings
    from convertible_bond.data_providers.base import (
        CREDIT_RATING_ORDER,
        CREDIT_RATING_RANK,
    )

    assert sync_ratings.RATING_ORDER is CREDIT_RATING_ORDER
    assert sync_ratings._RANK is CREDIT_RATING_RANK
    assert data_doctor._RATING_ORDER is CREDIT_RATING_ORDER
    assert data_doctor._RATING_RANK is CREDIT_RATING_RANK


def test_terms_close_fallback_row_is_re_requested_not_frozen_as_ok():
    """条款库兜底价不能判 "ok" —— "ok" 的含义是"今天真取到了".

    `_latest_bond_close_with_provenance` 在转债行情挂掉时回落到 `terms.close`,
    那个值**没有 as-of**、可以任意旧 (日升转债库里的 99.994 是 2021 年撤销发行前的)。
    判 "ok" 就把这一行钉死到明天: `stale_watchlist_codes` 只重取
    `_STALE_PRICE_STATES` 里的档, 而 "ok" 不在里面。

    展示文案刻意不变 (仍是「日期不明」) —— 改的只是自愈行为。
    """
    from convertible_bond.gui.tabs.batch_watchlist import (
        _STALE_PRICE_STATES, _derive_price_state, _row_data_label)

    today = date(2026, 9, 3)
    priced = {"status": "ok", "valuation_date": today}

    fallback = _derive_price_state(
        {"market_price": 99.994, "market_price_source": "terms_close"}, priced, today)
    assert fallback == "undated_market"
    assert fallback in _STALE_PRICE_STATES, "兜底行不会被重新取价"
    assert _row_data_label(
        {"_price_state": fallback, "market_price_source": "terms_close"}) == "日期不明"

    # 真实行情照旧 ok, 不跟着一起被拖去重取
    real = _derive_price_state(
        {"market_price": 158.40, "market_price_source": "history"}, priced, today)
    assert real == "ok"
    assert real not in _STALE_PRICE_STATES


def test_a_fallback_quote_does_not_overwrite_yesterdays_real_one():
    """整行 upsert 不许用**更差**的市价盖掉更好的.

    「全失败守卫」是全或无的 (`expect_price and not with_price`), 而"取到市价"是
    **逐只**成败的 —— 一只回落到 terms_close、另一只正常, 就从守卫底下整只穿过去,
    而热缓存 `merged.update(fresh)` 会把昨天那个真实的 158.40 / as_of 2026-09-01 /
    deviation +0.42 换成 99.994 / None / NaN。

    只护市价那条腿: 理论价是本轮真算出来的, 照旧覆盖。
    """
    from convertible_bond.watchlist_cache import _keep_better_market_fields

    yesterday = {
        "market_price": 158.40, "market_price_as_of": "2026-09-01",
        "market_price_source": "history", "deviation": 158.40 / 111.5 - 1,
        "theoretical_price": 111.5,
    }
    today_fallback = {
        "market_price": 99.994, "market_price_as_of": None,
        "market_price_source": "terms_close", "deviation": float("nan"),
        "theoretical_price": 112.7,
    }
    kept = _keep_better_market_fields(yesterday, today_fallback)
    assert kept["market_price"] == 158.40
    assert kept["market_price_as_of"] == "2026-09-01"
    assert kept["deviation"] == pytest.approx(158.40 / 111.5 - 1)
    # 价格块要**整块**留 —— 只留市价那三个会让表上的恒等式当场断: 昨天的市价配今天的
    # 理论价, `偏差 = 市价/理论价 − 1` 算出来对不上存着的那个 deviation。
    assert kept["theoretical_price"] == 111.5
    assert kept["market_price"] / kept["theoretical_price"] - 1 == pytest.approx(
        kept["deviation"]), "价格块自相矛盾"

    # 今天拿到真价 → 照常覆盖
    today_real = dict(today_fallback, market_price=160.0,
                      market_price_as_of="2026-09-03",
                      market_price_source="history", deviation=0.44)
    assert _keep_better_market_fields(yesterday, today_real)["market_price"] == 160.0

    # 昨天本来就没有真价 → 今天的兜底照常写进去 (总比空着强)
    assert _keep_better_market_fields(
        {"market_price": None}, today_fallback)["market_price"] == 99.994
    assert _keep_better_market_fields(None, today_fallback)["market_price"] == 99.994

    # terms_close 判据要单独可观测: 上面那些 fixture 的 as_of 都是 None, 于是
    # "没有 as_of" 那一条先命中, terms_close 那一条删掉也测不出来。给它一个带戳的。
    stamped_fallback = dict(today_fallback,
                            market_price_as_of="2026-09-03",
                            market_price_source="terms_close")
    assert _keep_better_market_fields(yesterday, stamped_fallback)["market_price"] == 158.40


def test_save_watchlist_pricing_wires_the_market_leg_guard(tmp_path):
    """接线也要守 —— 只测 helper 等于没守住 `save_watchlist_pricing` 里那一行.

    实测: 把那两行换回 `merged.update(fresh)`, helper 那条用例照常绿。
    """
    from convertible_bond.watchlist_cache import (
        load_watchlist_pricing, save_watchlist_pricing)

    cache = tmp_path / "hot.json"
    daily = tmp_path / "daily"
    save_watchlist_pricing(
        [{"bond_code": "128000.SZ", "market_price": 158.40,
          "market_price_as_of": "2026-09-01", "market_price_source": "history",
          "deviation": 158.40 / 120.0 - 1, "theoretical_price": 120.0,
          "status": "ok"}],
        valuation_date=date(2026, 9, 1), cache_path=cache, daily_dir=daily)

    save_watchlist_pricing(
        [{"bond_code": "128000.SZ", "market_price": 99.994,
          "market_price_as_of": None, "market_price_source": "terms_close",
          "deviation": float("nan"), "theoretical_price": 130.0, "status": "ok"}],
        valuation_date=date(2026, 9, 2), cache_path=cache, daily_dir=daily)

    row = load_watchlist_pricing(cache)["rows"]["128000.SZ"]
    assert row["market_price"] == 158.40, "兜底价盖掉了昨天的真市价"
    assert row["market_price_as_of"] == date(2026, 9, 1)   # 读回来是 date 不是串
    assert row["theoretical_price"] == 120.0, "价格块要整块留, 不能半块"
    assert row["market_price"] / row["theoretical_price"] - 1 == pytest.approx(
        row["deviation"]), "落盘之后价格块自相矛盾"


def test_market_price_coverage_does_not_count_the_terms_close_fallback():
    """``terms_close`` 兜底不是"取到市价"。

    转债行情整条挂掉时 ``_latest_bond_close_with_provenance`` 回落到条款库的
    ``terms.close`` —— 那个值**没有 as-of**、可以任意旧 (日升转债库里的 99.994 是
    2021 年撤销发行前的)。它是个有限数, 所以按 ``_is_finite`` 判就全算"取到了",
    闸在"一个真实报价都没拿到"这一档上恰好不响。

    代价不只是消息不准: 热缓存是整行 upsert, 这一轮就把昨天的真市价与真 deviation
    换成兜底价与 NaN, 而 ``_price_state`` 仍是 ``ok``, 于是 ``stale_watchlist_codes``
    当天再不重试。口径要与 ``market_valuation._usable_deviations`` 一致 —— 实测同一批
    行两个闸曾给出相反结论 (关注池 5/5 vs 批量页 0/5)。
    """
    from convertible_bond import market_valuation as mv
    from convertible_bond.gui.tabs.batch_watchlist import market_price_coverage

    def row(source, price=99.994):
        return {"bond_code": "123095.SZ", "status": "ok", "market_price": price,
                "market_price_as_of": None, "market_price_source": source,
                "deviation": float("nan"), "is_tradable": True,
                "listing_date": "2022-01-01"}

    fallback = [row("terms_close") for _ in range(5)]
    expect, got = market_price_coverage(fallback)
    assert len(expect) == 5 and len(got) == 0, "兜底价被当成取到了市价"
    # 与批量页那道闸同口径
    assert mv.snapshot_coverage(fallback)[0] == 0

    real = [dict(row("history"), deviation=0.4653) for _ in range(5)]
    expect2, got2 = market_price_coverage(real)
    assert len(expect2) == len(got2) == 5, "真实行情反而被挡掉了"


def test_events_banner_does_not_count_today_twice():
    """过去窗口与未来窗口**不许共用 today 端点**。

    ``_window_hit`` 两端都是闭区间, 而两次 ``collect_upcoming_events`` 曾都带上 today
    —— 正好落在今天的事件被各数一次, 横幅写成「近 7 天 1 件 … 强赎 (08-31) | 未来
    30 天 1 件 … 强赎 (08-31)」, 读起来像同一只债有两次强赎, 弹窗里同一行出现两遍,
    还占掉 head=5 的展示位。而"今天到期"恰恰是横幅最该说清楚的那一档。

    ``collect_upcoming_events`` 内部的 ``seen`` 去重是**每次调用各一份**, 拦不住跨调用
    的重复 —— 所以这条必须测两次调用的合集。
    """
    import inspect

    from convertible_bond.gui.tabs import batch_watchlist as bw

    src = inspect.getsource(bw._refresh_events_banner)
    # 过去窗口的右端点必须早于 today
    assert "today - timedelta(days=1)" in src, "过去窗口仍以 today 收尾, 今天会被双计"
    # 未来窗口仍从 today 开始 —— 今天归"还来得及处理"的那一侧
    assert "store, watch_codes, today, today + timedelta(days=window_days)" in src


def test_events_popup_title_is_derived_from_the_actual_window():
    """弹窗标题不许写死天数, 也不许宣称已删除的池外范围。

    它曾是 ``近 30 天事件 (N 件, 关注池+主池)``, 两处都在说假话: 天数写死 30 而横幅走
    ``past_days``/``window_days``; 那个"关注池加主池"的范围是 2026-08-29 已经删掉的
    (``_pool_scan_codes`` 一并删了)。弹窗标题于是在给它自己的内容作伪证。

    判据只看**真正传给 ``win.title()`` 的那个串**, 不扫源码文本 —— 第一版扫文本, 当场
    把上面这段解释历史的 docstring 判红。同一个教训 AGENTS 里已经记过一次
    (关注池"不说持仓"那条守护, 第一版把解释性注释判成违规)。
    """
    import ast
    import inspect

    from convertible_bond.gui.tabs import batch_watchlist as bw

    src = inspect.getsource(bw._show_events_banner_full)
    tree = ast.parse(src.lstrip())
    titles = [
        ast.get_source_segment(src.lstrip(), node.args[0])
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "title"
        and node.args
    ]
    assert titles, "找不到 win.title(...) 调用"
    title_expr = titles[0]
    assert "关注池+主池" not in title_expr, f"标题仍宣称已删除的池外范围: {title_expr}"
    assert "30" not in title_expr, f"标题仍写死天数: {title_expr}"
    assert "past_days" in title_expr and "window_days" in title_expr, (
        f"标题没用这一轮实际的窗口: {title_expr}")

    # 窗口天数本身要有单一事实源, 且横幅默认值就取它
    assert bw._BANNER_PAST_DAYS == 7 and bw._BANNER_FUTURE_DAYS == 30
    banner_sig = str(inspect.signature(bw._refresh_events_banner))
    assert "past_days" in banner_sig and "window_days" in banner_sig
    assert bw._refresh_events_banner.__kwdefaults__["past_days"] is bw._BANNER_PAST_DAYS
    assert bw._refresh_events_banner.__kwdefaults__["window_days"] is bw._BANNER_FUTURE_DAYS


def test_strategy_page_renders_risk_tags_through_the_display_table():
    """策略页的**两处** risk_tags 出口都要走 ``risk_tag_label``。

    AGENTS 明令"所有消费者都要走 ``risk_tag_label()``" —— 裸 ``str(tag)`` 会让同一个
    标签在批量页读作「市价远低于市场中位」而在策略页读作「深度低估待核」, 而那两个
    说法指向相反的动作 (研究 vs 去修数据)。

    这条钉**两处**: 候选/剔除表 (~L655) 与持仓明细 (~L758)。只修一处正是"同一页两种
    说法"的来源 —— 关注池摘要条曾私抄一份字面量, 就是这么分叉的。
    """
    import ast
    import inspect

    from convertible_bond.gui.controllers import strategy_render

    src = inspect.getsource(strategy_render)
    tree = ast.parse(src)

    bare = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Name) and node.func.id == "str"):
            continue
        # str(x) where the comprehension iterates risk_tags
        seg = ast.get_source_segment(src, node) or ""
        if seg.strip() == "str(tag)":
            bare.append(node.lineno)
    assert not bare, f"strategy_render 仍有裸 str(tag) 于第 {bare} 行"
    assert src.count("risk_tag_label(tag)") >= 2, "只改了一处出口"


class _FakeTree:
    """够 ``_attach_column_sort`` 用的最小 Treeview 替身。

    它只碰四个方法 (``set`` / ``get_children`` / ``move`` / ``heading``), 所以鸭子类型
    就够 —— 这样能测**真实的排序代码路径**而不是只测那张档位表。
    不建 Tk root: macOS 的 Tk 不要 X11, 本机会悄悄成功而 CI 抛
    ``TclError: no display name``, 那是"本机全绿 CI 全红"的唯一常见来源。
    """

    def __init__(self, rows, columns):
        self._columns = list(columns)
        self._rows = {f"i{n}": dict(zip(columns, r)) for n, r in enumerate(rows)}
        self._order = list(self._rows)
        self.commands: dict[str, object] = {}

    def set(self, iid, col):
        return self._rows[iid][col]

    def get_children(self, _parent=""):
        return list(self._order)

    def move(self, iid, _parent, index):
        self._order.remove(iid)
        self._order.insert(index, iid)

    def heading(self, col, **kw):
        if "command" in kw:
            self.commands[col] = kw["command"]

    def column_values(self, col):
        return [self._rows[iid][col] for iid in self._order]


def test_clicking_an_ordinal_header_actually_reorders_by_the_ladder():
    """驱动**真实**的 ``_attach_column_sort``, 不只是断言档位表的内容。

    上一版只比对 ``_ORDINAL_SORT_SCALES`` 里的字典 —— 把 ``sort_by`` 里那行
    ``scale = _ORDINAL_SORT_SCALES.get(...)`` 改成 ``scale = None``, 排序当场退回
    字符串序而整套测试照样全绿。测数据结构不等于测代码路径。
    """
    from convertible_bond.gui.tabs.batch_common import _attach_column_sort

    columns, headers = ("c0", "c1"), ("评级", "可信度")
    rows = [("AA-", "中"), ("CC", "低"), ("AA+", "高"), ("BBB+", "中")]
    tree = _FakeTree(rows, columns)
    _attach_column_sort(tree, columns, headers)

    tree.commands["c0"]()                      # 第一次点 = 升序
    assert tree.column_values("c0") == ["CC", "BBB+", "AA-", "AA+"], "评级不是信用序"
    tree.commands["c0"]()                      # 再点 = 降序
    assert tree.column_values("c0") == ["AA+", "AA-", "BBB+", "CC"]
    # 字符串降序会给出这个 —— 钉住反例, 否则"碰巧对"也能过
    assert sorted(["AA-", "CC", "AA+", "BBB+"], reverse=True) == [
        "CC", "BBB+", "AA-", "AA+"]

    tree2 = _FakeTree(rows, columns)
    _attach_column_sort(tree2, columns, headers)
    tree2.commands["c1"]()
    assert tree2.column_values("c1") == ["低", "中", "中", "高"], "可信度不是高中低序"
    assert sorted(["中", "低", "高", "中"]) == ["中", "中", "低", "高"]

    # 缺失值无论升降都沉底 (既有约定, 顺手钉住不被序数分支带坏)
    tree3 = _FakeTree([("AA", "高"), ("—", "低"), ("A+", "中")], columns)
    _attach_column_sort(tree3, columns, headers)
    tree3.commands["c0"]()
    assert tree3.column_values("c0")[-1] == "—"


def test_home_tooltip_gives_the_two_deviation_columns_their_own_reference():
    """两列的**参照物不同**, 不许并成一句。

    ``偏差(%)`` 比的是模型价, ``相对偏差(pp)`` 比的是全市场中位 —— 主页 tooltip 曾写成
    「偏差(%) / 相对偏差(pp) 正 = 市价贵于模型价」, 对后者说错了。实测缓存里 135/309 行
    两者符号相反: 一只债完全可以"比模型价贵"同时"比全市场便宜", 而主页是默认落地页,
    这是整页唯一一条符号说明。口径以 ``COLUMN_HELP`` 为准 (那里两列本来就是分开写的)。

    既有的 ``test_title_tooltip_stays_short`` 只管长度 —— 它挡不住这个。
    """
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parent))
    from test_home_tab import _home_tooltips

    from convertible_bond.gui.tabs.batch_common import COLUMN_HELP

    title = _home_tooltips()["title"]
    assert "偏差(%)" in title and "相对偏差(pp)" in title
    # 两个参照物都要出现, 且不能只留模型价那一个
    assert "模型价" in title, "没说 偏差(%) 比的是模型价"
    assert "全市场" in title, "相对偏差(pp) 被并进了模型价那一句"
    # 与 COLUMN_HELP 的口径同向 (那里是逐列写的单一事实源)
    assert "全市场" in COLUMN_HELP["相对偏差(pp)"]
    assert "模型价" in COLUMN_HELP["偏差(%)"]


def test_daily_watchlist_snapshot_is_append_only_within_a_day(tmp_path):
    """当天的窄快照必须**按 bond_code 合并**, 不能被一轮部分刷新整份重写。

    ``save_watchlist_pricing`` 曾只写本轮的 ``fresh``。而部分刷新是常态 —— 关注池里
    只重算几只、自愈只挑 ``_price_state != "ok"`` 的那几只 —— 于是当天文件被重写成
    那几行, 同一天早些时候写进去的其他债**永久消失**。实测: 第一轮写 A/B/C, 第二轮
    只刷 A, 当天文件只剩 A, 而热缓存 (merge-upsert) 三只都在 —— 同一批数据两个文件
    两个答案, 且这个目录是只追加的历史, 丢了不可恢复。

    合并只发生在**同一个估值日内** (文件名就按日期分片), 所以不存在"隔夜旧行混进
    今天"的风险 —— 原实现担心的正是那件事, 但那件事由分片本身挡住了。
    """
    import json
    from datetime import date

    from convertible_bond import watchlist_cache as wc

    val = date(2026, 8, 31)

    def row(code, price):
        return {"bond_code": code, "bond_name": code, "market_price": price,
                "theoretical_price": 100.0, "deviation": 0.1,
                "valuation_date": val.isoformat(), "status": "ok"}

    kw = dict(valuation_date=val, cache_path=tmp_path / "hot.json",
              daily_dir=tmp_path / "daily")
    wc.save_watchlist_pricing(
        [row(c, 100 + i) for i, c in enumerate(["A.SZ", "B.SZ", "C.SZ"])], **kw)
    wc.save_watchlist_pricing([row("A.SZ", 999.0)], **kw)

    payload = json.loads((tmp_path / "daily" / f"{val}.json").read_text(encoding="utf-8"))
    codes = [r["bond_code"] for r in payload["records"]]
    assert codes == ["A.SZ", "B.SZ", "C.SZ"], f"部分刷新截断了当天快照: {codes}"
    a_price = next(r["market_price"] for r in payload["records"] if r["bond_code"] == "A.SZ")
    assert a_price == 999.0, "重刷的那只没有被更新"
    # 计数要说文件里实际有多少条, 本轮刷了几只另记 —— 合并之后这两个数不再相等
    assert payload["_meta"]["n_records"] == 3
    assert payload["_meta"]["n_fresh"] == 1


def test_terms_bundle_does_not_drop_another_writers_bonds(tmp_path):
    """整份重写不能吃掉别的写入方新增的债。

    ``_save`` 是 ``json.dump(self._data)`` —— 一个长命实例只要快照比盘上旧, 下一次写
    就静默删掉别人新增的条目, 而 ``_bundle_meta.n_bonds`` 跟着改小, 连"少了"都看不出来。
    实测: a 与 b 都读到 {A}, a 写 B, b 写 C → 盘上只剩 {A, C}。

    这不是假想的并发: GUI 的「🌐 同步池」菜单就是在 GUI 持有 bundle 的同时起子进程
    去写同一个文件。``reload()`` 是给这个场景准备的, 但它要人显式调, 两次写之间的
    任何一次 ``set()`` 都来不及。
    """
    import json

    from convertible_bond.cache import TermsBundle
    from convertible_bond.data_providers.base import BondTerms

    path = tmp_path / "cb.json"
    a = TermsBundle(path)
    a.set("A.SZ", BondTerms(sec_name="A", conversion_price=10.0), source="Wind")

    b = TermsBundle(path)                    # b 的快照停在 {A}
    a.set("B.SZ", BondTerms(sec_name="B", conversion_price=11.0), source="Wind")
    b.set("C.SZ", BondTerms(sec_name="C", conversion_price=12.0), source="Wind")

    raw = json.loads(path.read_text(encoding="utf-8"))
    codes = sorted(k for k in raw if not k.startswith("_"))
    assert codes == ["A.SZ", "B.SZ", "C.SZ"], f"并发写丢了条目: {codes}"
    assert raw["_bundle_meta"]["n_bonds"] == 3

    # 两边都有的键**以写入方为准** —— 它才是这一次带着新值来的
    a.set("C.SZ", BondTerms(sec_name="C-新", conversion_price=99.0), source="Wind")
    raw2 = json.loads(path.read_text(encoding="utf-8"))
    assert raw2["C.SZ"]["sec_name"] == "C-新"


def test_sensitivity_page_does_not_write_a_fabricated_s0_into_the_shared_var(tmp_path):
    """敏感性页不许把 K 写进共享的 ``v_S0``。

    ``v_S0`` 是**两页共享**的 (`tabs/pricing.py` 的「正股价 S」输入框, 以及
    `_collect_params` 里真正拿去定价的那个值)。敏感性页曾在行情未到时直接
    ``self.v_S0.set(self.v_K.get())`` 来让 `_collect_params` 通过 —— 于是"行情还没到
    就点了一次敏感性"之后, 定价页会一直用 **正股价 = 转股价** 这个捏造的数算理论价,
    页面上没有任何提示说它是编的。

    改成本地 ``s0_fallback``: 只在 ``v_S0`` 为空时顶上, 不写回。热力图不受影响
    (S0 在 ``compute_sensitivity_grid`` 内被逐点覆盖); 图上那颗"当前点"星标本来就
    包在 try/except 里, S0 未知时不画 —— 那比画一个 S/K 恒等于 1 的假点诚实。
    """
    import ast
    import inspect

    from convertible_bond.gui.controllers import pricing as pricing_ctl
    from convertible_bond.gui.controllers import sensitivity as sens

    src = inspect.getsource(sens)
    tree = ast.parse(src)
    writes = [
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute) and node.func.attr == "set"
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "v_S0"
    ]
    assert not writes, f"敏感性页仍在写共享 v_S0, 第 {writes} 行"

    # 顶替值必须有地方进得去
    sig = inspect.signature(pricing_ctl.PricingMixin._collect_params
                            if hasattr(pricing_ctl, "PricingMixin")
                            else pricing_ctl._collect_params)
    assert "s0_fallback" in sig.parameters, "_collect_params 没有接收本地顶替值的入口"


def test_autocomplete_selection_foreground_follows_the_resolved_background():
    """联想下拉选中行的前景色必须**跟着解析后的底色**挑, 不能写死白色。

    ``ACCENT`` 的深色档是 #89b4fa (浅蓝) —— 白字上去只有 **2.11:1**, 而项目自己的
    ``theme.badge_text_color`` 会挑 #11111b 得到 **8.91:1**。这与 AGENTS 记的
    「底色由数据决定的控件不许写死前景色」(EVENT_TYPE_COLOR 那次 13/18 低于 AA)
    是同一条规则, 只是这次底色由**主题**决定而不是由数据决定。
    """
    import ast
    import inspect

    from convertible_bond.gui import theme, widgets

    src = inspect.getsource(widgets.AutocompleteEntry._ensure_popup)
    tree = ast.parse(src.lstrip())
    hardcoded = [
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.keyword) and node.arg == "selectforeground"
        and isinstance(node.value, ast.Constant)
    ]
    assert not hardcoded, f"selectforeground 写死了常量, 第 {hardcoded} 行"

    # 两个主题档都要达到 WCAG AA (4.5:1)
    for col in theme.ACCENT:
        fg = theme.badge_text_color(col)
        ratio = theme.contrast_ratio(fg, col)
        assert ratio >= 4.5, f"底 {col} 字 {fg} 只有 {ratio:.2f}:1"
    # 钉住反例: 深色档白字确实不合格 —— 否则这条用例可能在任何实现下都绿
    assert theme.contrast_ratio("#ffffff", theme.ACCENT[1]) < 3.0


def test_strategy_detail_rows_match_the_column_count():
    """「买卖明细」两个行构造器必须都吐出与列数相同的元素数。

    Treeview 对多出来的值是**静默丢弃** —— 「跳过」行曾经塞 14 个值进 13 列, 于是从
    「买入」列起整行左移一格: 买入渲染成空, 卖出/原因显示的是**买入**的日期与价,
    标签/事件显示的是卖出的, 而 ``reason`` (跳过行唯一说明"为什么没成交"的东西)
    一个字都没露。不报错、不红测试, 只是每一格都落在错的表头底下。

    静态比对两个 ``detail_rows.append([...])`` 的长度与那张列表的列数; 另外钉住渲染
    入口自己也会校验 (那道闸管的是将来新加的行构造器)。
    """
    import ast
    import inspect

    from convertible_bond.gui.controllers import strategy_render

    src = inspect.getsource(strategy_render)
    tree = ast.parse(src)

    lengths = [
        (node.lineno, len(node.args[0].elts))
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute) and node.func.attr == "append"
        and isinstance(node.func.value, ast.Name) and node.func.value.id == "detail_rows"
        and node.args and isinstance(node.args[0], ast.List)
    ]
    assert len(lengths) >= 2, "找不到两个行构造器"
    assert len({n for _, n in lengths}) == 1, f"行长不一致: {lengths}"

    # 列名要从**真正传 detail_rows 的那次调用**里取 —— 文件里另有一张同样以
    # period/status/rank 开头的 11 列表, 按前缀去 walk 会抓错。
    call = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_render_strategy_small_tree"
        and any(isinstance(a, ast.Name) and a.id == "detail_rows" for a in node.args)
    )
    cols = next(a for a in call.args if isinstance(a, ast.List) and a.elts
                and isinstance(a.elts[0], ast.Constant) and a.elts[0].value == "period")
    assert lengths[0][1] == len(cols.elts), (
        f"行长 {lengths[0][1]} != 列数 {len(cols.elts)}")

    # 渲染入口本身要拦住长度不符 —— 将来新加的行构造器靠它
    assert "表格行长与列数不符" in inspect.getsource(
        strategy_render.StrategyRenderMixin._render_strategy_small_tree)


def test_strategy_dates_render_dash_not_the_literal_none():
    """``pos.get("exit_date", "—")`` 挡不住**键存在但值是 None** —— 而跳过行全是这一档。"""
    from convertible_bond.gui.controllers.strategy_render import StrategyRenderMixin

    fmt = StrategyRenderMixin._fmt_strategy_date
    assert fmt(None) == "—"
    assert "None" not in fmt(None)
    from datetime import date
    assert fmt(date(2025, 1, 2)) == "2025-01-02"


def test_concentration_uses_all_positive_contributors():
    """「前三集中度」的分母是**全体**正贡献, 不是 ``top_contributors``。

    那张表是 ``ranked[:10]`` —— 排在第 11 名之后的正贡献者不在分母里, 显示出来的集中度
    系统性偏高。而这个数还要去撞 ``_strategy_robustness_notes`` /
    ``_strategy_dynamic_suggestions`` 里 >=0.65 那道闸, 于是"收益过于集中"会被虚报。
    """
    from convertible_bond.strategy_backtest import _strategy_attribution

    periods = [{
        "cost": 0.0,
        "average_cash_weight": 0.0,
        "skipped_positions": [],
        "positions": [
            {"bond_code": f"{i:06d}.SZ", "bond_name": f"B{i}",
             "return_contribution": 0.01 * (20 - i), "period_return": 0.01}
            for i in range(20)
        ],
    }]
    attribution = _strategy_attribution(periods)
    assert len(attribution["top_contributors"]) == 10      # 前提: 表被截断了
    assert attribution["positive_contributor_count"] == 20

    from_top10 = sum(float(r["contribution"]) for r in attribution["top_contributors"]
                     if float(r["contribution"]) > 0)
    assert attribution["total_positive_contribution"] > from_top10, (
        "全体正贡献不该等于前十之和 —— 这条用例的前提坏了")

    honest = (attribution["top3_positive_contribution"]
              / attribution["total_positive_contribution"])
    inflated = sum(sorted(
        (float(r["contribution"]) for r in attribution["top_contributors"]),
        reverse=True)[:3]) / from_top10
    assert honest < inflated, f"集中度没有被修正: {honest} vs {inflated}"


def test_strategy_labels_come_from_one_shared_source():
    """持仓/资金方式的措辞只许有一份。

    比较表的标签曾自己拼一个只含 ``top_n`` 的简版, 于是 ``holding_mode="pool"`` 的运行
    被标成「Top10」—— pool 模式压根不用 top_n, 而数据面板同时把它写作「等权全池」,
    同一次运行两个页面两种说法。更糟的是**两次只差候选池 (selection_view) 的运行标签
    逐字节相同**, 比较表里认不出谁是谁。
    """
    import inspect

    from convertible_bond.gui.controllers import strategy_compare, strategy_render_analysis
    from convertible_bond.gui.controllers.strategy_common import (
        strategy_funding_label,
        strategy_holding_label,
    )

    assert strategy_holding_label(
        {"holding_mode": "pool", "max_holdings": 30, "top_n": 10}) == "等权全池(≤30)"
    assert strategy_holding_label(
        {"holding_mode": "top_score", "top_n": 10, "rank_signal": "deviation"}
    ) == "估值偏差 Top10"
    assert strategy_funding_label({"funding_mode": "full_invest"}) == "满仓等权"
    assert strategy_funding_label({"funding_mode": "reserve_cash"}) == "缺口留现金"

    # 两个页面都要用这一份, 不许各拼各的。
    # 判据只看**真实的字符串字面量** (走 ast), 不扫源码文本 —— 第一版扫文本, 当场把
    # 两处解释这段历史的**注释**判红。同一个教训 AGENTS 里记过 (关注池"不说持仓"那条
    # 守护, 第一版也是把解释性注释当成了违规)。
    import ast

    for mod, expected in ((strategy_compare, "strategy_compare_label("),
                          (strategy_render_analysis, "strategy_holding_label(")):
        src = inspect.getsource(mod)
        assert expected in src, f"{mod.__name__} 没用共用措辞 (期望调 {expected})"
        literals = [
            node.value for node in ast.walk(ast.parse(src))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        ]
        leftover = [x for x in literals if "等权全池" in x or "缺口留现金" in x]
        assert not leftover, f"{mod.__name__} 里还留着自己拼的措辞: {leftover}"

    # 候选池必须进比较标签 —— **行为断言**, 不是"源码里出现过这个词"。
    # 只差候选池的两次运行标签必须不同, 否则比较表里认不出谁是谁。
    from convertible_bond.gui.controllers.strategy_common import strategy_compare_label

    base = {"history_mode": "标准", "rebalance_freq": "M", "rank_signal": "deviation",
            "holding_mode": "top_score", "top_n": 10, "funding_mode": "reserve_cash"}
    a = strategy_compare_label("估值策略", dict(base, selection_view="低估候选"))
    b = strategy_compare_label("估值策略", dict(base, selection_view="双低"))
    assert a != b, f"只差候选池的两次运行标签相同: {a!r}"

    # pool 模式不许被标成 Top{n}
    pool = strategy_compare_label(
        "估值策略", dict(base, holding_mode="pool", max_holdings=30))
    assert "Top10" not in pool and "等权全池" in pool, pool
