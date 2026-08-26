"""⭐ 关注池主页 (tabs/home.py) 的组成性守护.

GUI 在测试环境跑不起来 (CustomTkinter 需要真实显示), 所以这里只能钉住那些
**不需要渲染就能验证**的接线不变量。每一条都对应一次会静默发生的事故:
控件建了没人刷新、页签顺序反了首屏空表、渲染入口被别人的优化顺手停掉。
"""
from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

import convertible_bond
from convertible_bond.gui import app as app_mod
from convertible_bond.gui.tabs import batch as batch_tab
from convertible_bond.gui.tabs import batch_watchlist as watchlist_tab
from convertible_bond.gui.tabs import home as home_tab

_GUI_ROOT = Path(convertible_bond.__file__).parent / "gui"


# ── 页签接线 ────────────────────────────────────────────────────

def test_home_builds_before_batch():
    """主页必须先 build.

    ``_render_watchlist_table`` 拿不到 ``batch_watchlist_table_frame`` 时是
    ``return`` 而不是报错 —— 顺序反了只表现为"主页首屏是空的", 没有任何异常,
    而这是默认落地页。
    """
    src = inspect.getsource(app_mod.CBPricerApp._build_tabview)
    assert src.index("home_tab.build") < src.index("batch_tab.build")


def test_watchlist_button_refresh_comes_after_watchlist_is_loaded():
    """定价页 ⭐ 按钮的初始状态要等关注池载入之后才能定."""
    src = inspect.getsource(app_mod.CBPricerApp._build_tabview)
    assert src.index("home_tab.build") < src.index("_refresh_watchlist_button")


def test_home_is_the_default_landing_tab():
    src = inspect.getsource(app_mod)
    for pattern in (r'tab_seg\.set\(E\("⭐ 关注"\)\)',
                    r'_active_tab_name = E\("⭐ 关注"\)'):
        assert re.search(pattern, src), f"没找到: {pattern}"
    # 两处 fallback 也要指向主页, 否则页签名对不上时会掉回一个不是默认页的页
    assert src.count('or E("⭐ 关注")') >= 1
    assert src.count('selected = E("⭐ 关注")') >= 1


def test_shared_state_lives_on_the_app_not_a_page():
    """两页共用的状态必须在 _build_vars 里建.

    主页比批量页先 build, 定价页的 ⭐ 按钮也读 _batch_watchlist —— 谁都不该
    假设自己是创建方。
    """
    src = inspect.getsource(app_mod.CBPricerApp._build_vars)
    for name in ("v_batch_source", "v_batch_status", "_batch_watchlist",
                 "_watchlist_price_cache"):
        assert f"self.{name}" in src, f"{name} 没提到 _build_vars"


# ── 控件归属 ────────────────────────────────────────────────────

def test_home_owns_the_watchlist_widgets():
    src = inspect.getsource(home_tab)
    for attr in ("batch_watchlist_table_frame", "v_batch_watchlist_summary",
                 "btn_batch_refresh_watch", "btn_batch_upcoming",
                 "lbl_batch_events_banner", "v_batch_events_banner"):
        assert f"app.{attr}" in src, f"主页没有建 {attr}"


def test_batch_page_no_longer_builds_watchlist_widgets():
    src = inspect.getsource(batch_tab)
    for attr in ("app.batch_watchlist_table_frame =", "app.btn_batch_refresh_watch =",
                 "app.btn_batch_upcoming =", "app.lbl_batch_events_banner ="):
        assert attr not in src, f"批量页还在建 {attr}, 会和主页抢同一个属性名"


def test_add_to_watchlist_button_stays_on_the_batch_page():
    """「⭐ 加入关注池」搬不走: 它读主表控件的 selection, 且 iid 是
    _batch_results 的整数下标。搬到主页后永远只会弹"请先运行批量定价"。"""
    assert "app.btn_batch_add_watch" in inspect.getsource(batch_tab)
    assert "btn_batch_add_watch" not in inspect.getsource(home_tab)


# ── 渲染入口 ────────────────────────────────────────────────────

def test_home_ui_entry_points_are_callable():
    for name in ("build", "refresh_theme"):
        assert callable(getattr(home_tab, name, None)), f"缺 {name}"


def test_events_banner_refresh_is_not_parasitic_on_the_table_render():
    """横幅刷新从 _render_watchlist_table 末尾提到 refresh_home.

    它原先寄生在表渲染里且是全仓库唯一调用点 —— 任何"少画一次表"的优化都会
    顺手把横幅一起停掉, 而横幅失败是静默的 (拿不到 label/var 直接 return)。
    """
    table_src = inspect.getsource(watchlist_tab._render_watchlist_table)
    assert "_refresh_events_banner" not in table_src
    assert "_refresh_events_banner" in inspect.getsource(watchlist_tab.refresh_home)


def test_view_switch_does_not_rebuild_the_home_table():
    """切视图 / 切列预设时数据一个字节没变, 不该 destroy 重建主页那棵 17 列的树."""
    assert "refresh_home_table=False" in inspect.getsource(batch_tab._change_batch_view)


def test_data_changing_paths_still_refresh_home():
    """反过来: 凡是数据变了的路径都必须让 refresh_home_table 保持默认 True.

    少刷一次的表现是"算完了但表还是旧值" —— 没有任何异常。
    """
    src = inspect.getsource(batch_tab)
    for match in re.finditer(r"_render_batch_views\(", src):
        depth, i = 0, match.end() - 1
        while i < len(src):
            if src[i] == "(":
                depth += 1
            elif src[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        args = src[match.end():i]
        if "refresh_home_table=False" not in args:
            continue
        lineno = src.count("\n", 0, match.start()) + 1
        fn_src = inspect.getsource(batch_tab._change_batch_view)
        assert src[match.start():i] in fn_src, (
            f"batch.py 第 {lineno} 行附近传了 refresh_home_table=False, "
            "但只有 _change_batch_view (纯展示) 才允许这么做")


def test_watchlist_worker_refreshes_both_pages():
    """主表读 _batch_all_results, 主页读三级取价表 —— 各有各的入口, 都要刷."""
    src = inspect.getsource(watchlist_tab._watchlist_pricing_worker)
    assert "_render_batch_views(" in src
    assert "refresh_home(app)" in src


# ── F821 防线在新页是活的 ────────────────────────────────────────

def test_home_does_not_use_star_import():
    """star import 会把未定义名从 F821 降级成 F405, 而 pyproject 对老的两个
    tabs 文件豁免了 F405 —— 新页不要把这道防线一起关掉。"""
    tree = ast.parse((_GUI_ROOT / "tabs" / "home.py").read_text(encoding="utf-8"))
    stars = [n for n in ast.walk(tree)
             if isinstance(n, ast.ImportFrom)
             and any(a.name == "*" for a in n.names)]
    assert not stars, f"home.py 第 {[n.lineno for n in stars]} 行有 star import"


def test_home_is_not_in_per_file_ignores():
    pyproject = (Path(convertible_bond.__file__).parent.parent / "pyproject.toml").read_text(
        encoding="utf-8")
    assert "tabs/home.py" not in pyproject
