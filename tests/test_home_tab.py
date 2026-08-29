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
    for name in ("v_batch_source", "v_batch_status", "v_watchlist_status", "_batch_watchlist",
                 "_watchlist_price_cache"):
        assert f"self.{name}" in src, f"{name} 没提到 _build_vars"


# ── 控件归属 ────────────────────────────────────────────────────

def test_home_owns_the_watchlist_widgets():
    src = inspect.getsource(home_tab)
    for attr in ("batch_watchlist_table_frame",
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


# ── 行情源全局唯一 ───────────────────────────────────────────────

def test_market_source_has_exactly_one_selector():
    """行情源下拉全局只许有一个 (顶栏那个).

    此前有三个: 顶栏 v_data_source、批量页 v_batch_source、主页又一个。三个下拉
    控三条链路时, "我明明选了 akshare 怎么还在连 Wind"是找不出原因的那类问题。
    """
    menus = []
    for path in sorted(_GUI_ROOT.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            if "CTkOptionMenu" not in line:
                continue
            # 往后看几行找 variable=，判断是不是行情源下拉
            window = "\n".join(text.splitlines()[lineno - 1:lineno + 4])
            if "v_data_source" in window or "v_batch_source" in window:
                menus.append(f"{path.name}:{lineno}")
    assert menus == ["app.py:547"] or len(menus) == 1, (
        f"行情源下拉应当只有顶栏一个, 实际: {menus}")


def test_batch_source_is_the_same_var_as_the_header_one():
    """v_batch_source 必须就是 v_data_source 本身 (同一个 StringVar 对象).

    做成两个 var 再互相同步是行不通的 —— 同步总会漏掉某条路径, 而漏掉的表现是
    "两页显示的源不一样", 用户无从判断哪个说了算。
    """
    src = inspect.getsource(app_mod.CBPricerApp._build_vars)
    assert "self.v_batch_source = self.v_data_source" in src


def test_pages_do_not_build_their_own_source_selector():
    """两个业务页都不许自己建行情源下拉 (它们仍然**读** app.v_batch_source)."""
    for module in (batch_tab, home_tab):
        tree = ast.parse(inspect.getsource(module))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and getattr(node.func, "attr", "") == "CTkOptionMenu"):
                continue
            bound = {kw.value.attr for kw in node.keywords
                     if kw.arg == "variable" and isinstance(kw.value, ast.Attribute)}
            assert not (bound & {"v_batch_source", "v_data_source"}), (
                f"{module.__name__} 里还有页内行情源下拉")


# ── 按钮文案单一事实源 ───────────────────────────────────────────

def test_watch_refresh_label_is_not_hardcoded_anywhere_else():
    """状态栏那句"点「…」再试"引的必须是同一个常量.

    实测事故: 按钮从「⚡ 关注池重算」改成「⚡ 今日刷新」后, 消息里还写着旧名字,
    用户在页面上找不到那个按钮。

    只扫**运行期字符串字面量** —— docstring 与注释里提到按钮名是说明文字, 不参与
    渲染, 拿它们报错只会逼人把解释删掉。
    """
    label = watchlist_tab.WATCH_REFRESH_LABEL
    offenders = []
    for path in sorted(_GUI_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        exempt = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef,
                                 ast.FunctionDef, ast.AsyncFunctionDef)):
                doc = ast.get_docstring(node, clean=False)
                if doc is not None and node.body:
                    exempt.add(id(node.body[0].value))
            # 常量自己的定义处当然可以写字面量
            if isinstance(node, ast.Assign) and any(
                    getattr(t, "id", "") == "WATCH_REFRESH_LABEL" for t in node.targets):
                exempt.add(id(node.value))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in exempt and label in node.value):
                offenders.append(f"{path.name}:{node.lineno}: {node.value[:60]!r}")
    assert not offenders, (
        "这些运行期字符串硬编码了按钮文案, 请改引 WATCH_REFRESH_LABEL:\n  "
        + "\n  ".join(offenders))


def test_unavailable_message_interpolates_the_label():
    """真实故障形态是消息里留着一个**过期**的名字, 而不是重复写了当前名字 ——
    所以这里直接钉住"那句消息必须插值常量", 而不是扫字面量。"""
    src = inspect.getsource(watchlist_tab._start_watchlist_pricing)
    assert "再试" in src, "找不到那句提示, 用例需要更新"
    line = next(ln for ln in src.splitlines() if "再试" in ln)
    assert "WATCH_REFRESH_LABEL" in line, f"提示里的按钮名不是插值来的: {line.strip()}"


def test_button_uses_the_label_constant():
    assert "text=WATCH_REFRESH_LABEL" in inspect.getsource(home_tab)


# ── NaN 不是 None ────────────────────────────────────────────────

def test_price_cells_use_is_finite_not_is_not_none():
    """落盘的 None 读回来是 NaN, 而 `NaN is not None` 为真 —— 用 `is not None`
    判就会把"今天没有市价"渲染成字面的 "nan"。实测三只未上市新债全中。"""
    src = inspect.getsource(watchlist_tab._render_watchlist_table)
    for field in ("market_price", "theoretical_price"):
        bad = f'entry.get("{field}") is not None'
        assert bad not in src, f"{field} 还在用 `is not None` 判, NaN 会渲染成 'nan'"
        assert f'_is_finite(entry.get("{field}"))' in src


def test_watchlist_status_is_not_shared_with_the_batch_page():
    """两页各有自己的状态行 —— 共用一个 StringVar 时会串台.

    共用的初衷是"⚡ 已刷新关注池 N 只"这类**瞬时**消息在哪页都看得见, 但批量页的
    **视图摘要**也写在同一个变量里, 而它是**常驻**的 —— 于是关注池主页永久挂着一句
    「✅ 低估候选: 展示 41/283 只 | 成功 41 失败 0」, 说的是另一页的表。

    划分按**用户触发时在哪一页**: 关注池页的动作 (重算 / 扫新债 / 右键增删 / 自愈)
    写 ``v_watchlist_status``; 批量页的动作写 ``v_batch_status`` —— 包括
    「⭐ 加入关注池」, 那个按钮长在批量页上。
    """
    import inspect

    from convertible_bond.gui.tabs import batch as batch_tab
    from convertible_bond.gui.tabs import batch_watchlist as watchlist_tab
    from convertible_bond.gui.tabs import home as home_tab

    # 主页只挂自己的那个
    home_src = inspect.getsource(home_tab)
    assert "textvariable=app.v_watchlist_status" in home_src
    # 查**控件绑定**而不是裸名字 —— 文件头的结构说明里会提到另一个变量名
    assert "textvariable=app.v_batch_status" not in home_src
    assert "app.v_batch_status.set(" not in home_src

    # 批量页只挂自己的那个
    assert "textvariable=app.v_batch_status" in inspect.getsource(batch_tab)

    # batch_watchlist 里唯一还写 v_batch_status 的必须是「⭐ 加入关注池」——
    # 那是批量页上的按钮, 反馈该出现在批量页
    for name, fn in vars(watchlist_tab).items():
        if not callable(fn) or not getattr(fn, "__module__", "") == watchlist_tab.__name__:
            continue
        try:
            src = inspect.getsource(fn)
        except (OSError, TypeError):
            continue
        if "v_batch_status" in src:
            assert name == "_add_selection_to_watchlist", (
                f"{name} 写了 v_batch_status —— 关注池的动作该写 v_watchlist_status")


def test_status_line_matches_the_batch_page_format():
    """关注池状态行与批量页同构: **一行左对齐加粗**, ✅ 开头, ``|`` 分组.

    曾经拆成"左消息 + 右摘要"两半, 而左半在空闲时是空的 —— 看着像右边飘着一段孤字。
    现在一个变量时分复用: 空闲是摘要, 动作时被消息覆盖, 下一次 refresh_home 摘要回来
    (worker 里 ``refresh_home`` → ``set(msg)`` 的顺序与批量页 worker 逐字一致)。
    """
    import inspect

    from convertible_bond.gui.tabs import batch_watchlist as watchlist_tab

    src = inspect.getsource(home_tab._build_status)
    assert "textvariable=app.v_watchlist_status" in src
    assert 'anchor="w"' in src and '"bold"' in src
    # 摘要不再有独立变量 —— 有的话两者会在同一行抢位置
    assert not hasattr(app_mod.CBPricerApp, "v_batch_watchlist_summary")
    assert "v_batch_watchlist_summary" not in inspect.getsource(home_tab)
    assert "v_batch_watchlist_summary" not in inspect.getsource(watchlist_tab)

    class _Var:
        def __init__(self):
            self.value = ""

        def set(self, v):
            self.value = v

    class _App:
        v_watchlist_status = _Var()

    app = _App()
    watchlist_tab._refresh_watchlist_summary(app, [
        {"bond_code": "X", "status": "ok", "deviation": 0.1,
         "credit_rating": "AA", "risk_tags": []},
    ])
    text = app.v_watchlist_status.value
    assert text.startswith("✅ 关注池: "), text
    assert "  |  " in text, "分组分隔符要与批量页一致"

    # 空关注池也要有一句话 —— 空行会让这一行看着像坏了
    empty = _App()
    watchlist_tab._refresh_watchlist_summary(empty, [])
    assert empty.v_watchlist_status.value.startswith("✅ 关注池: 空")


def _home_tooltips() -> dict[str, str]:
    """从源码里取出 ``Tooltip(app.<btn>, "...")`` 的**字符串实参**.

    用 AST 而不是扫源码文本 —— 旁边的代码注释里会写"别再写不需要 Wind", 扫文本会
    把那句解释也当成提示内容命中。
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(home_tab).lstrip())
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and getattr(node.func, "id", "") == "Tooltip" and node.args):
            continue
        target = node.args[0]
        key = (target.attr if isinstance(target, ast.Attribute) else
               getattr(target, "id", "?"))
        text = ""
        for arg in node.args[1:]:
            for part in (arg.values if isinstance(arg, ast.JoinedStr) else [arg]):
                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                    text += part.value
                elif isinstance(part, ast.JoinedStr):
                    text += "".join(v.value for v in part.values
                                    if isinstance(v, ast.Constant))
        out[key] = text
    return out


def test_action_buttons_have_no_tooltip():
    """两个按钮**不要 tooltip** —— 按钮文案自己已经说清要做什么.

    此前的提示 ("找出新发/待上市的债, 加进关注池并定价" / "只给关注池这几只定价,
    跳过全市场") 是把按钮名字换个说法再说一遍, 属于**为了写 tooltip 而写 tooltip**。
    """
    tips = _home_tooltips()
    for key in ("btn_batch_upcoming", "btn_batch_refresh_watch"):
        assert key not in tips, f"{key} 不该有 tooltip"


def test_title_tooltip_stays_short():
    """标题那条留着 —— 它说的是**表怎么读**, 不是按钮做什么。但也要短.

    实现细节 (三级兜底取价的三层、缓存文件名、akshare 窄同步) 属于代码注释;
    逐列口径悬停表头看, 这里只留一条最容易读反的 —— `+54.84` 有两种正好相反的读法,
    而它同时出现在两列上。
    """
    title = _home_tooltips()["title"]
    assert title.count("\n") <= 2 and len(title) <= 110, f"{len(title)} 字"
    for detail in ("三级兜底", "watchlist_pricing_cache", "akshare", "不需要 Wind"):
        assert detail not in title, f"{detail} 是实现细节"
