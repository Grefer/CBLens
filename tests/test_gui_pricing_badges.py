"""定价页风险徽章的优先级 (单槽首命中, 顺序即优先级)."""
from datetime import date
from types import SimpleNamespace

from convertible_bond.gui.controllers.pricing import PricingMixin
from convertible_bond.gui.theme import ORANGE, RED


class _Stub(PricingMixin):
    """只提供 ``_term_risk_summary`` 用到的那几个 helper —— GUI 在测试环境起不来。"""

    def __init__(self):
        self.v_mat_date = SimpleNamespace(get=lambda: "")

    @staticmethod
    def _contains_any(value, words):
        text = str(value or "")
        return any(w in text for w in words)

    @staticmethod
    def _conversion_suspension_active(_terms, _val_date, _impact=None):
        return False


def test_near_delisting_outranks_underlying_st():
    """既是 ST 正股、又在 30 天内摘牌时, 徽章必须报**红色的**「临近摘牌」。

    这是个单槽首命中的徽章, 而 ST 那一支曾排在摘牌前面 —— 于是一个**需要在 30 天内
    卖掉**的事实被一个"风险更大, 慢慢看"的 ORANGE 提示盖住, 而这两件事恰好最常
    同时出现 (ST 正股本来就是最可能走到摘牌那一步的)。

    优先级与行色 ``_resolve_row_tag`` 同向: 可交易性压过标的风险。
    """
    val_date = date(2026, 3, 1)
    both = SimpleNamespace(
        underlying_status="ST/退市风险",
        suspension_status=None, underlying_trade_status=None,
        delisting_date=date(2026, 3, 20), last_trading_date=date(2026, 3, 18),
        credit_rating="A", credit_rating_outlook=None, credit_watch_status=None,
    )
    text, colour = _Stub()._term_risk_summary(both, val_date)
    assert colour is RED and text.startswith("临近摘牌"), (
        f"ST 把「临近摘牌」盖住了: {text!r}")

    # ST 但**不**临近摘牌 → 仍是 ORANGE 的「正股风险」(这一档没有被顺序改动波及)
    st_only = SimpleNamespace(
        underlying_status="ST/退市风险",
        suspension_status=None, underlying_trade_status=None,
        delisting_date=None, last_trading_date=None,
        credit_rating="A", credit_rating_outlook=None, credit_watch_status=None,
    )
    text, colour = _Stub()._term_risk_summary(st_only, val_date)
    assert colour is ORANGE and text == "正股风险"

    # 转债停牌仍压过两者 —— 它是"现在就下不了单"
    halted = SimpleNamespace(
        underlying_status="ST/退市风险", suspension_status="停牌",
        underlying_trade_status=None,
        delisting_date=date(2026, 3, 20), last_trading_date=None,
        credit_rating="A", credit_rating_outlook=None, credit_watch_status=None,
    )
    text, colour = _Stub()._term_risk_summary(halted, val_date)
    assert colour is RED and text == "转债停牌"


def test_underlying_st_badge_survives_both_status_dialects():
    """ST 徽章不许只认 ``underlying_status`` 的字面 —— 那个字段有两套词表。

    每日 ``cb-sync-admission-status`` 按 Wind 写 ``是/否``, 而 ``cb-sync-events --apply``
    按 ``underlying_st_risk`` 事件写 ``ST/退市风险``。徽章原先只对后者做关键词匹配, 于是
    亮不亮取决于**哪个同步后跑**: 实测 2026-09-03 状态刷新之后主池 4 只 ST 债 (闻泰/
    三房/宏图/章鼓) 的徽章一起灭掉, 事件同步一跑又全亮回来 —— 而 ``是`` 这个值本身
    不含任何关键词, 光扩关键词表是补不上的。

    判据因此收到准入层那一个 ``_underlying_has_st_risk``, 它拼的是
    ``f"{underlying_name} {underlying_status}"``, 名字里的 ``*ST闻泰`` 兜得住。
    """
    val_date = date(2026, 3, 1)

    def _bond(**kw):
        base = dict(
            underlying_name=None, underlying_status=None,
            suspension_status=None, underlying_trade_status=None,
            delisting_date=None, last_trading_date=None,
            credit_rating="AA", credit_rating_outlook=None, credit_watch_status=None,
        )
        base.update(kw)
        return SimpleNamespace(**base)

    # Wind 方言: 状态是个光秃秃的「是」, 主语在名字里
    wind = _bond(underlying_name="*ST闻泰", underlying_status="是")
    assert _Stub()._term_risk_summary(wind, val_date) == ("正股风险", ORANGE), (
        "Wind 方言 (underlying_status='是') 下 ST 徽章不亮")

    # 事件层方言: 状态自带关键词
    evented = _bond(underlying_name="闻泰科技", underlying_status="ST/退市风险")
    assert _Stub()._term_risk_summary(evented, val_date) == ("正股风险", ORANGE)

    # 两个字段都被清空、只剩名字 (事件层的 ``underlying_st_clear`` 会把状态写成 None,
    # 实测 127033.SZ 中装转2 就是这个形状, 而它的正股仍叫 ST中装)
    name_only = _bond(underlying_name="ST中装")
    assert _Stub()._term_risk_summary(name_only, val_date) == ("正股风险", ORANGE)

    # 反面: 正常正股不许误报 —— ``否`` 里没有关键词, 名字里也没有
    plain = _bond(underlying_name="声迅股份", underlying_status="否")
    text, _ = _Stub()._term_risk_summary(plain, val_date)
    assert text != "正股风险", f"正常正股被报成 ST: {text!r}"


def test_no_site_still_keyword_matches_underlying_status():
    """整个定价页都不许再对 ``underlying_status`` 做关键词匹配。

    ST 判据在这个文件里有**两处**消费者: 单槽徽章 ``_term_risk_summary`` 与事件条
    ``_render_risk_event``。上面那条用例只盖得住第一处 —— 第二处要立起来得把
    ``_set_term_event`` / ``_date_progress`` / ``_risk_impact_detail`` 一起做桩,
    而它们跟这条判据毫无关系。所以第二处按源码扫: 判据本身就是"文件里不许再出现
    拿 ST 关键词去比 ``underlying_status`` 的写法"。

    只扫这一个字段, 不扫 ``_contains_any`` 本身 —— 停牌那几处照常用它, 它们的字段
    没有第二套词表。
    """
    import ast
    import inspect

    from convertible_bond.gui.controllers import pricing as mod

    source = inspect.getsource(mod)
    tree = ast.parse(source)
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        args = ast.dump(node)
        if "underlying_status" in args and "ST" in args:
            offenders.append(ast.get_source_segment(source, node))
    assert not offenders, (
        "又出现了拿 ST 关键词比 underlying_status 的写法, 走 _underlying_has_st_risk:\n"
        + "\n".join(str(o) for o in offenders))
