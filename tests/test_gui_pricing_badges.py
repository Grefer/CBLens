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
