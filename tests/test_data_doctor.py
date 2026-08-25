"""数据体检的两条**判据口径** —— 它们各自曾经把真事故报成"通过"。

体检本身是"把碰巧发现的 bug 变成每次都查的检查", 所以判据写窄一格、或方向读反,
后果不是报错而是**报绿**。这个文件只守这两处口径, 不重复各检查的业务逻辑。
"""
from datetime import date

import pytest

from convertible_bond.cache import TermsBundle
from convertible_bond.cli import data_doctor as mod
from convertible_bond.data_providers import BondTerms
from convertible_bond.historical_terms import TermsPatch, TermsPatchStore


# ── 「判死但今日有成交」的覆盖面 ────────────────────────────────────────────────

@pytest.mark.parametrize("reason", [
    "已退市", "已过最后交易日", "已到期", "暂停上市",
    "停牌/暂停交易", "不可交易", "已发行未上市", "违约/异常状态",
    "3 日后可交易",
])
def test_reasons_that_assert_not_trading(reason):
    assert mod._asserts_not_trading(reason)


@pytest.mark.parametrize("reason", [
    "评级过低", "正股 ST/退市风险", "正股停牌", "成交额过低", "余额过小",
    "定向转债/非公开交易标的", "非沪深主板/深市可转债", "",
])
def test_policy_reasons_do_not_assert_not_trading(reason):
    """策略口径的剔除从不声称"这只债不能交易" —— 混进来会让检查天天误报几十只。"""
    assert not mod._asserts_not_trading(reason)


def test_dead_but_trading_catches_newly_listed_bond_marked_untradable(monkeypatch):
    """早期 dead 集只有 {已退市, 已过最后交易日}, 于是上市首日被判成"不可交易"
    /"停牌"的新债从这条检查底下整只漏过去 —— 派克转债当天成交 2.57 亿、中仑转债
    12.95 亿, 检查还报 0。
    """
    import pandas as pd
    monkeypatch.setattr("akshare.bond_zh_hs_cov_spot", lambda *a, **k: pd.DataFrame({
        "symbol": ["sh111026", "sz123281", "sz123999"],
        "trade": [155.721, 153.710, 88.0],
        "volume": [1_649_410, 8_244_638, 0],      # 第三只零成交 = 陈旧行, 不算
    }))
    bundle = TermsBundle.__new__(TermsBundle)
    bundle.get = lambda code: BondTerms(sec_name={"111026.SH": "派克转债",
                                                  "123281.SZ": "中仑转债"}.get(code, ""))

    check = mod.check_dead_but_trading({
        "online": True,
        "bundle": bundle,
        "today": date(2026, 8, 25),
        "excluded": {
            "111026.SH": "停牌/暂停交易",
            "123281.SZ": "不可交易",
            "123999.SZ": "已退市",        # 零成交 → 不该报
            "128044.SZ": "评级过低",       # 策略口径 → 不该报
        },
    })
    assert check.status == mod.FAIL
    assert len(check.extra) == 2
    assert any("111026.SH" in row for row in check.extra)
    assert any("123281.SZ" in row for row in check.extra)


def test_dead_but_trading_tolerates_the_session_boundary(monkeypatch):
    """刚停止交易的债不算"判死却仍在成交" —— 那笔成交正是它自己最后一个交易日的。

    akshare 现货表在收盘后仍留着上一交易日的行情 (ticktime 只有时分秒、没有日期), 而
    market_today() 按 Asia/Shanghai 走: 在美西运行时本机上午已是上海次日凌晨。实测
    春23转债 (最后交易日 2026-08-25 当天成交 453 万手, 08-31 摘牌) 被这么误报过。
    """
    import pandas as pd
    monkeypatch.setattr("akshare.bond_zh_hs_cov_spot", lambda *a, **k: pd.DataFrame({
        "symbol": ["sh113667", "sh113610"],
        "trade": [181.029, 120.394],
        "volume": [4_537_630, 262_360],
    }))
    terms = {
        "113667.SH": BondTerms(sec_name="春23转债", last_trading_date=date(2026, 8, 25),
                               delisting_date=date(2026, 8, 31)),
        "113610.SH": BondTerms(sec_name="灵康转债", last_trading_date=date(2024, 12, 12),
                               delisting_date=date(2024, 12, 20)),
    }
    bundle = TermsBundle.__new__(TermsBundle)
    bundle.get = terms.get

    check = mod.check_dead_but_trading({
        "online": True,
        "bundle": bundle,
        "today": date(2026, 8, 26),
        "excluded": {"113667.SH": "已过最后交易日", "113610.SH": "已退市"},
    })
    assert check.status == mod.FAIL
    assert len(check.extra) == 1 and "113610.SH" in check.extra[0]   # 死了一年多的才算
    assert "另有 1 只刚停止交易" in check.detail


# ── 「公告评级 vs cb_data」的方向 ────────────────────────────────────────────

def _rating_ctx(tmp_path, current: str, announced: str):
    bundle = TermsBundle(tmp_path / "cb_data.json")
    bundle.set("123157.SZ", BondTerms(sec_name="科蓝转债", credit_rating=current),
               source="unit")
    store = TermsPatchStore(tmp_path / "patches.json")
    store.add_many([TermsPatch(bond_code="123157.SZ", effective_date=date(2026, 6, 24),
                               fields={"credit_rating": announced}, source="cninfo")])
    return {"bundle": bundle, "patch_store": store}


def test_rating_divergence_reports_both_directions_without_blaming(tmp_path):
    """这条检查的方向翻过两次, 都是因为"cb_data 的评级从哪来"变了。

    cb-sync-ratings 落地后 cb_data 走第三方**当前值**, 而公告侧的 rating_re 左界 bug 会
    系统性把 AA 抠成 A —— 实测拿第三方逐条当裁判, 17 条分歧里 **15 条是公告错**。
    所以它只报分歧率, 不再断言"cb_data 未跟进"。
    """
    lower = mod.check_rating_divergence(_rating_ctx(tmp_path, "AA-", "A-"))
    assert "分歧" in lower.detail and "公告更低 1" in lower.detail
    assert "未跟进" not in lower.detail and "未跟进" not in lower.because
    # 裁判是第三方, 不是这条检查本身
    assert "评级同步水位" in lower.because


def test_rating_divergence_stays_silent_when_they_agree(tmp_path):
    same = mod.check_rating_divergence(_rating_ctx(tmp_path, "AA-", "AA-"))
    assert same.status == mod.OK
    assert same.extra == []


# ── 「已摘牌」的判据 ────────────────────────────────────────────────────────

def test_looks_delisted_needs_the_date_to_have_passed():
    """判据是**日期已过**, 不是"有没有这个字段"。

    曾写成 ``delisting_date is not None``。当时全库只有 17 只有摘牌日, 没问题;
    2026-08-22 全库回填 (17 → 1041) 之后, 几乎每只在市债都带着一个**未来的**到期摘牌日,
    于是「末条 patch == 当前值」跳过 952/958 (99%) 条链、只真检查 6 只, 藏着 30 只不符。
    """
    today = date(2026, 8, 25)
    live = BondTerms(sec_name="鸿路转债", delisting_date=date(2032, 10, 9))
    assert not mod._looks_delisted(live, today)

    gone = BondTerms(sec_name="万孚转债", delisting_date=date(2026, 9, 1))
    assert not mod._looks_delisted(gone, today)          # 还没到
    assert mod._looks_delisted(gone, date(2026, 9, 2))   # 过了

    assert mod._looks_delisted(BondTerms(sec_name="格力转债(退市)"), today)
    assert mod._looks_delisted(
        BondTerms(sec_name="春23转债", last_trading_date=date(2026, 8, 24)), today)
    assert not mod._looks_delisted(
        BondTerms(sec_name="春23转债", last_trading_date=today), today)  # 今天还能交易
