"""AutocompleteEntry 下拉尺寸计算 — 纯逻辑部分, 不创建 Tk 控件.

CustomTkinter 在测试环境起不来, 这里用 ``__new__`` 绕过 ``__init__``, 只喂
``_popup_width`` 真正读到的几个属性; 真实渲染路径靠人工冒烟。
"""
from convertible_bond.gui.widgets import AutocompleteEntry


class _Font:
    """等宽字体替身: 1 字符 = 8px, 中文按 2 字符宽."""

    @staticmethod
    def measure(text):
        return sum(16 if ord(c) > 0x2E80 else 8 for c in text)


class _Scrollbar:
    @staticmethod
    def winfo_reqwidth():
        return 15


class _Popup:
    @staticmethod
    def winfo_screenwidth():
        return 1920


def _entry(items, *, max_rows=8):
    ac = AutocompleteEntry.__new__(AutocompleteEntry)
    ac._items = items
    ac._label_font = _Font()
    ac._scrollbar = _Scrollbar()
    ac._popup = _Popup()
    ac.max_rows = max_rows
    return ac


def test_popup_widens_to_fit_longest_label():
    """候选宽于输入框时按最长候选开窗 — 否则中文简称会被齐刷刷截掉."""
    items = [("123001.SZ", "123001.SZ  蓝标转债(退市)")]
    ac = _entry(items)

    width = ac._popup_width(entry_width=130, x=100)

    assert width == _Font.measure(items[0][1]) + 8
    assert width > 130


def test_popup_never_narrower_than_entry():
    ac = _entry([("110030.SH", "110030.SH")])
    assert ac._popup_width(entry_width=260, x=100) == 260


def test_popup_reserves_room_for_scrollbar():
    """候选超过可见行数时会出滚动条, 宽度要把它算进去, 否则又压回文字."""
    label = "123001.SZ  蓝标转债(退市)"
    few = _entry([(f"12300{i}.SZ", label) for i in range(1, 4)], max_rows=8)
    many = _entry([(f"1230{i:02d}.SZ", label) for i in range(1, 31)], max_rows=8)

    assert many._popup_width(130, 100) - few._popup_width(130, 100) == 15


def test_popup_width_is_capped():
    ac = _entry([("X", "长" * 200)])
    # 输入框很窄时不按倍数封顶, 至少给 _MIN_MAX_WIDTH
    assert ac._popup_width(entry_width=60, x=100) == AutocompleteEntry._MIN_MAX_WIDTH
    # 输入框够宽时按 3 倍封顶
    assert ac._popup_width(entry_width=400, x=100) == 1200


def test_popup_width_stays_on_screen():
    ac = _entry([("X", "长" * 200)])
    assert ac._popup_width(entry_width=400, x=1800) == 1920 - 1800 - 8


# ── Tooltip: 分段渲染不许破坏原有的 (widget, text) 用法 ──────────
def test_tooltip_keeps_the_positional_text_signature():
    """全仓库 30+ 个调用点都是 ``Tooltip(widget, "……")``.

    分段渲染是**加**出来的第二条路 (行色图例要演示颜色, 不是描述颜色), 不能把
    ``text`` 挤成关键字参数 —— 那会一次性打断所有既有调用点, 而 GUI 在测试环境
    起不来, F821 也拦不住"参数传错位置"这种运行期错误。
    """
    import inspect

    from convertible_bond.gui.widgets import Tooltip

    params = list(inspect.signature(Tooltip.__init__).parameters.values())
    assert [p.name for p in params[:4]] == ["self", "widget", "text", "delay_ms"]
    assert params[2].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert params[2].default is None
    segments = inspect.signature(Tooltip.__init__).parameters["segments"]
    assert segments.kind is inspect.Parameter.KEYWORD_ONLY
    assert segments.default is None


def test_tooltip_resolves_constants_callables_and_vars():
    """三种取值方式在本仓库都有现成用法; 分段与纯文本共用同一条解析路径."""
    from convertible_bond.gui.widgets import Tooltip

    class _Var:
        def get(self):
            return "from-var"

    assert Tooltip._resolve("abc") == "abc"
    assert Tooltip._resolve(lambda: "xyz") == "xyz"
    assert Tooltip._resolve(_Var()) == "from-var"
    assert Tooltip._resolve(None) is None
