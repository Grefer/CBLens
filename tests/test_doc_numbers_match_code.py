"""文档里那些**从代码算得出来**的数字, 必须真的等于代码算出来的.

这个模块只钉一类东西: **纯函数于代码**的派生量 —— 列数、列宽之和、页签个数、
某个函数七个分支的真实输出。它们与 AGENTS「关于本文里的「实测」数字」那条豁免
不是一回事: 那条说的是随数据漂的池子分母 (「40/284」这种), 分母过期不等于结论过期;
而这里的数改了之后**没有任何东西会红**, 只会让读者照着一个假数去做决定。

也不钉**编辑决定** (哪几列该写 tooltip、要不要保留某一行图例) —— 那正是
AGENTS 里"来回摆过两次"的教训: 把一次取舍固化成规则, 人改主意就红。

实测这些数确实会静默漂: AGENTS 的四个列宽在「标签」列从 180 加宽到 220 之后
全部低了 50px; COLUMN_HELP 的分母在「涨跌%」删掉之后从 23 变成 22, 而
「转股价值」明明已经写了说明却还挂在"刻意不写"的名单上。
"""
import ast
import re
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def _assign(path, name):
    """从源码里取一个模块级赋值的字面量 (不 import —— GUI 在测试环境起不来)."""
    tree = ast.parse(_read(path))
    for node in ast.walk(tree):
        targets = (node.targets if isinstance(node, ast.Assign)
                   else [node.target] if isinstance(node, ast.AnnAssign) else [])
        if any(getattr(t, "id", None) == name for t in targets):
            return node.value
    raise AssertionError(f"{path} 里找不到 {name}")


def _headers(path, name):
    return [e.elts[0].value for e in _assign(path, name).elts]


def _widths(path, name):
    return {e.elts[0].value: e.elts[1].value for e in _assign(path, name).elts}


def test_agents_column_help_coverage_numbers():
    """AGENTS 的「19/22 列」与那三个"刻意不写"的列名."""
    node = _assign("convertible_bond/gui/tabs/batch_common.py", "COLUMN_HELP")
    help_keys = [k.value for k in node.keys]
    union = list(dict.fromkeys(
        _headers("convertible_bond/gui/tabs/batch.py", "_BATCH_COLS_FULL")
        + _headers("convertible_bond/gui/tabs/batch_watchlist.py", "_WATCHLIST_COLUMNS")))
    uncovered = [h for h in union if h not in help_keys]

    agents = _read("AGENTS.md")
    assert f"{len(help_keys)}/{len(union)} 列" in agents, (
        f"AGENTS 的覆盖面分数与实测不符 (实测 {len(help_keys)}/{len(union)})")
    for name in uncovered:
        assert name in agents, f"AGENTS 没提到未写说明的列「{name}」"
    for name in help_keys:
        # 写了说明的列不许还挂在"刻意不写"的名单上
        assert f"{name} 四列刻意不写" not in agents

    common = _read("convertible_bond/gui/tabs/batch_common.py")
    assert f"实测 {len(help_keys)} 条 / 表头并集 {len(union)} 列" in common


def test_agents_preset_width_numbers():
    """AGENTS 的列预设宽度 —— 列宽的纯函数, 不随数据漂."""
    full = _widths("convertible_bond/gui/tabs/batch.py", "_BATCH_COLS_FULL")
    simple = _widths("convertible_bond/gui/tabs/batch.py", "_BATCH_COLS_SIMPLE")
    agents = _read("AGENTS.md")
    assert f"简洁 {len(simple)} 列 {sum(simple.values())}px" in agents
    assert f"(完整 {len(full)} 列 {sum(full.values())}px)" in agents

    # 视图追加列之后的总宽也要对得上
    from convertible_bond.gui.tabs.batch import _batch_schema_for
    for view, label in (("综合机会", "全池"), ("转股折价", "转股折价"), ("需复核", "需复核")):
        cols = _batch_schema_for("简洁", view)
        assert f"{label} {len(cols)} 列 {sum(w for _, w in cols)}px" in agents, (
            f"AGENTS 的「{label}」宽度与实测不符 "
            f"({len(cols)} 列 {sum(w for _, w in cols)}px)")


def test_view_count_in_the_three_sort_docstrings():
    """「N 个视图里的 M 个」三处副本必须等于 BATCH_REVIEW_VIEWS 的真实规模."""
    from convertible_bond.batch_pricing import BATCH_REVIEW_VIEWS

    n = len(BATCH_REVIEW_VIEWS)
    phrase = f"{n} 个视图里的 {n - 1} 个"
    for rel in ("AGENTS.md", "convertible_bond/batch_pricing.py",
                "tests/test_batch_pricing.py"):
        text = _read(rel)
        assert phrase in text, f"{rel} 里的视图个数与 BATCH_REVIEW_VIEWS 不符 (应为 {phrase})"
        # 上一版留下的旧数不许还在
        assert "6 个视图里的 5 个" not in text or n == 6


def test_readme_gui_tab_count():
    """README 的「GUI N 大页面」必须等于 app._tab_names 的长度."""
    tree = ast.parse(_read("convertible_bond/gui/app.py"))
    tabs = None
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and any(getattr(t, "attr", None) == "_tab_names" for t in node.targets)):
            tabs = node.value.elts
    assert tabs, "app.py 里找不到 _tab_names"
    cn = "零一二三四五六七八九十"[len(tabs)]
    assert f"GUI {cn}大页面" in _read("README.md"), (
        f"README 的页面数与 _tab_names 不符 (实测 {len(tabs)} 个)")


def test_readme_module_tree_matches_the_real_layout():
    """README 的模块树里写成包的必须真是包, 写成文件的必须真是文件."""
    readme = _read("README.md")
    for line in readme.splitlines():
        m = re.match(r"^[│├└─\s]*(\w[\w_]*)(/|\.py)\s+#", line)
        if not m:
            continue
        name, kind = m.group(1), m.group(2)
        bases = (ROOT / "convertible_bond", ROOT, ROOT / "convertible_bond" / "gui")
        # 同名的包与兼容入口可以并存 (`convertible_bond/gui/` 与根目录的 `gui.py`),
        # 所以判据是"存在某个位置与声明的形态一致", 不是"第一个找到的位置"。
        exists_as_pkg = any((b / name).is_dir() for b in bases)
        exists_as_mod = any((b / f"{name}.py").exists() for b in bases)
        if not (exists_as_pkg or exists_as_mod):
            continue
        want_pkg = kind == "/"
        assert (exists_as_pkg if want_pkg else exists_as_mod), (
            f"README 把 {name} 写成 {'包' if want_pkg else '文件'}, 但盘上没有这个形态")


@pytest.mark.parametrize("entry, expected", [
    ({"_price_state": "ok", "market_price_as_of": date(2026, 8, 28)}, "✓ 08-28"),
    ({"_price_state": "ok", "market_price_as_of": date(2026, 8, 26)}, "市价旧 08-26"),
    ({"_price_state": "undated_market", "market_price_source": "terms_close"}, "日期不明"),
    ({"_price_state": "stale", "valuation_date": date(2026, 8, 26)}, "未重算 08-26"),
    ({"_price_state": "no_market"}, "无市价"),
    ({"_price_state": "unpriced"}, "未定价"),
    ({"_price_state": "failed", "status": "boom"}, "失败 · boom"),
])
def test_usage_data_state_table_shows_strings_the_code_can_emit(entry, expected):
    """USAGE 的「数据状态」表里每一行都得是 `_row_data_label` 真能吐出来的串.

    上一版有 3/6 行是代码吐不出来的 (`✓ 今日` / `✓ 今日 · 价 08-21` / `✓ 今日 · 无戳`),
    还漏了「未重算」整整一档 —— 那张表是从列还叫「数据」的时候原样抄过来的。
    """
    from convertible_bond.gui.tabs.batch_watchlist import _row_data_label

    got = _row_data_label(entry, latest_as_of=date(2026, 8, 28))
    assert got == expected, f"函数吐的是 {got!r}"
    doc = _read("docs/USAGE.md")
    token = expected.split(" · ")[0] if " · " in expected else expected
    assert f"`{token}" in doc, f"USAGE 的数据状态表里没有「{token}」这一档"


def test_every_deferral_marker_is_listed_in_the_backlog_table():
    """散在各处的「单独立项 / 尚未处理 / 已知边界」必须在汇总表里数得清.

    这些账**没有**任何 issue、任务或测试在跟 —— 唯一的记录就是 AGENTS.md 本身。
    上一轮复核就是靠外部 sweep 才发现它们从没被汇总过。加一处标记却不进表,
    等于又多了一条只有写的人知道的账。
    """
    text = _read("AGENTS.md")
    head = "### 刻意留着的账 (deferred)"
    assert head in text, "汇总表不见了"
    table = text[text.index(head):text.index("### 五层架构")]
    rows = [ln for ln in table.splitlines() if ln.startswith("| ") and "---" not in ln]
    entries = len(rows) - 1                      # 减掉表头
    markers = sum(text.count(m) for m in
                  ("单独立项", "⚠ 尚未处理", "已知代价, 未处理", "⚠ 已知边界"))
    assert entries >= 7, f"汇总表只有 {entries} 条"
    assert markers <= entries + 3, (
        f"正文里有 {markers} 处延期标记, 而汇总表只有 {entries} 条 —— 有账没进表")
