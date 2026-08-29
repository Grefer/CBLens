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
                 "double_low": None, "down_reset_robust_edge_value": math.nan}):
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

    # 「距下修线」(284/284 有值) 顶掉「下修优势」—— 后者在开页读缓存这条路上整列空
    assert "正股/下修线" in simple and "下修优势(元)" not in simple
    assert "下修优势(元)" in full

    # 诊断项只进完整: 理论价可信度在默认视图里结构上只有高/中; 定价状态实测恒 ✓
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
    for unit_col in ("相对偏差(pp)", "偏差(%)", "剩余(年)", "余额(亿)", "下修优势(元)"):
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
    - `下修优势` → **`下修优势(元)`**: 左右两列都带单位, 不写就是个裸数。

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
    for named in ("定价状态", "正股/下修线", "正股σ(%)", "下修优势(元)"):
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
               "转股溢价(%)", "正股σ(%)", "下修优势(元)", "正股/下修线",
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
    是写死的 —— 实测 2000px 宽的窗口下「正股/下修线」「相对偏差(pp)」「下修优势(元)」
    「转股溢价(%)」四个表头全被截断 (截图里读到的是「正股/」)。
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
    - 它的非展示消费者当时也已全部不可达: GUI 的排序信号只有三个选项, CLI 默认
      ``down_reset_robust_edge``, ``_legacy_score_gate`` 生产代码零调用而全池
      **0/283** 够得着它的 8.0 阈值。

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



def test_both_pricing_entries_request_pde_down_reset_signals():
    """主池与关注池两条定价路径都要开 PDE 下修信号, 否则「下修优势」列一半是空的。

    这个 kwarg 走 ``**pricer_overrides``, 漏传不报错只静默不算 —— 正是它此前在批量页
    缺席、让稳健下修优势在缓存里 0/280 有值的原因。
    """
    for module in (batch_tab, watchlist_tab):
        src = inspect.getsource(module)
        assert "compute_pde_signals=True" in src, f"{module.__name__} 未开 PDE 下修信号"


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
    # 没登记的原样返回
    assert batch_pricing.risk_tag_label("低评级") == "低评级"
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
    assert batch_common._format_tags(tags, drop_covered=True) == "较高HV / 无评级"
    # 批量页不传 drop_covered —— 那边没有「数据状态」列
    assert "无市价" in batch_common._format_tags(tags)


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
               "余额(亿)", "转股价值", "转股溢价(%)", "正股σ(%)", "下修优势(元)",
               "正股/下修线"}
    rows = [
        {"status": "ok", "market_price": 128.37, "theoretical_price": 135.17,
         "deviation": -0.0503, "relative_deviation": -0.259, "double_low": 142.0,
         "T": 0.23, "outstanding_balance": 4.2, "parity": 112.55,
         "conversion_premium": 0.141, "sigma": 0.61,
         "down_reset_robust_edge_value": 3.5, "down_reset_trigger_gap": 0.32},
        {"status": "ok", "market_price": 116.35, "theoretical_price": 118.28,
         "deviation": -0.0163, "relative_deviation": -0.225, "double_low": 384.0,
         "T": 5.94, "outstanding_balance": 18.8, "parity": 30.3,
         "conversion_premium": 2.84, "sigma": 0.31,
         "down_reset_robust_edge_value": -12.0, "down_reset_trigger_gap": -0.63},
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
    722/1059。它 08-24 曾是 1033/1058, 被一次全量条款同步清掉 317 只 ——
    ``cb_data_sync._LOCALLY_AUTHORITATIVE_FIELDS`` 里只保护了 ``credit_rating``,
    而 ``get_bond_terms`` 对正股名取不到时返回 None 就会盖掉本地好值
    (与 AGENTS 里 ``delisting_date`` 那一档同形)。没有回落, 三成的行正股列直接变空。

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
       「综合机会/低估候选/转股折价」一律按 ``relative_deviation`` **升序**,
       而「低估候选」的准入判据本身就是 ``rel < −5pp`` —— 任何架在便宜度上的
       行色在那一页上都是整表同色 (实测 40/40)。便宜度已经被行位置编码完了。

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


def test_refresh_theme_survives_a_tree_that_was_already_destroyed():
    """空结果视图留下的悬垂 Treeview 不许打断整轮重染.

    触发链今天就成立: 默认落地「低估候选」(40 行, 注册树) → 切「下修优势」
    (**实测 0 行**) 或「转股折价」(**实测 0 行**) → 切主题 (app.py:996) 或跨响应式
    档位 (app.py:748)。真机 Tk **8.6.15** 实测 ``tag_configure`` 抛
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
    """图例与 ``_TAG_COLORS`` 键集必须同步, 且要显式解释"无色".

    与 ``WATCH_REFRESH_LABEL`` 那次同构: 改了 tag 但图例里留着一个**过期**的
    档位名, 用户对着表找一个不存在的颜色。所以比对的是**集合相等**而不是扫字面量
    —— 扫字面量抓不到"留着旧名字"这种真实形态。

    "无色"那一句是硬要求: 默认落地视图「低估候选」里 ``blocked`` 恒为 0
    (该视图本身就排除了拦截标签与低置信), 一页全无色是**设计意图**; 没有这句话,
    它和"配色坏了"长得一模一样 —— 与事件横幅空态那条同源。
    """
    assert set(_TAG_LEGEND) == set(_TAG_COLORS)
    legend = row_colour_legend()
    for label in _TAG_LEGEND.values():
        assert label in legend
    assert "无色" in legend
