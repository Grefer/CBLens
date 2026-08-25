import csv
import math
from dataclasses import replace
from datetime import date

import pytest

from convertible_bond import batch_pricing
from convertible_bond.batch_pricing import (
    AdmissionFilterConfig,
    BATCH_RESULT_COLUMNS,
    BATCH_REVIEW_VIEWS,
    HARD_REVIEW_TAGS,
    LEGACY_STRATEGY_EXCLUDE_TAGS,
    REVIEW_ONLY_TAGS,
    RISK_TAG_DIMENSION,
    tags_in,
    view_exclusion_reason,
    annotate_batch_result,
    annotate_batch_results,
    down_reset_trigger_gap,
    filter_batch_results_by_view,
    sort_batch_results_for_review,
    sort_batch_results_for_view,
    DEFAULT_DOWN_RESET_EDGE_PERCENTILE,
    DEFAULT_UNDERVALUED_PERCENTILE,
    DEFAULT_UNDERVALUED_SCORE_THRESHOLD,
    MIN_RELATIVE_CHEAPNESS,
    MIN_VIEW_ROWS,
    _selection_cutoff,
    batch_pricing_exclusion_reason,
    list_upcoming_tradable_from_cache,
    list_batch_codes_from_cache,
    load_batch_results_cache,
    merge_upcoming_pricing_results,
    parse_bond_codes,
    save_batch_results_cache,
    screen_batch_pool_from_cache,
    split_batch_codes_from_cache,
    summarize_exclusions,
    summarize_batch_results,
    write_batch_results_csv,
)
from convertible_bond.data_providers import BondTerms
import convertible_bond.batch_pricing as batch_pricing_module


def test_parse_bond_codes_dedupes_and_skips_headers():
    raw = "bond_code, 128009.sz\n# comment\n113050.SH；128009.SZ  转债代码"

    assert parse_bond_codes(raw) == ["128009.SZ", "113050.SH"]
    assert parse_bond_codes(["代码", "# comment", "128009.sz"]) == ["128009.SZ"]


def test_summarize_batch_results_counts_ok_status():
    rows = [{"status": "ok"}, {"status": "missing K"}, {"status": "ok"}]

    assert summarize_batch_results(rows) == {"total": 3, "success": 2, "failed": 1}


def test_list_batch_codes_from_cache_uses_terms_pool():
    class FakeTermsCache:
        def list_bonds(self):
            return ["113050.SH", "128009.SZ"]

    assert list_batch_codes_from_cache(FakeTermsCache()) == ["113050.SH", "128009.SZ"]
    assert list_batch_codes_from_cache(None) == []


def test_list_batch_codes_from_cache_filters_nonstandard_private_bonds():
    class FakeTermsCache:
        data = {
            "124025.SZ": BondTerms(sec_name="富乐定转"),
            "110815.SH": BondTerms(sec_name="九丰定01"),
            "404004.NQ": BondTerms(sec_name="汇车退债"),
            "123456.SZ": BondTerms(sec_name="普通转债"),
            "113050.SH": BondTerms(sec_name="南银转债"),
            "113575.SH": BondTerms(sec_name="东时转债", maturity_date=date(2026, 4, 9)),
            "128044.SZ": BondTerms(sec_name="岭南转债", maturity_date=date(2024, 8, 14)),
        }

        def list_bonds(self):
            return list(self.data)

        def get(self, code):
            return self.data[code]

    kept, excluded = split_batch_codes_from_cache(FakeTermsCache())

    assert kept == ["123456.SZ", "113050.SH"]
    assert {code for code, _ in excluded} == {
        "124025.SZ", "110815.SH", "404004.NQ", "113575.SH", "128044.SZ",
    }
    assert list_batch_codes_from_cache(FakeTermsCache(), include_nonstandard=True) == [
        "124025.SZ", "110815.SH", "404004.NQ", "123456.SZ", "113050.SH",
        "113575.SH", "128044.SZ",
    ]
    assert batch_pricing_exclusion_reason("124025.SZ", {"bond_name": "富乐定转"}) is not None
    assert batch_pricing_exclusion_reason(
        "124025.SZ",
        BondTerms(
            sec_name="富乐定转",
            listing_date=date(2025, 1, 1),
            tradable_date=date(2025, 7, 1),
            is_tradable=True,
        ),
        on_date=date(2026, 4, 28),
    ) == "非普通公募转债代码段"
    assert batch_pricing_exclusion_reason(
        "113575.SH",
        BondTerms(sec_name="东时转债", maturity_date=date(2026, 4, 9)),
        on_date=date(2026, 4, 28),
    ) == "已到期"


def test_split_batch_codes_from_cache_applies_projected_terms(monkeypatch):
    class FakeTermsCache:
        data = {
            "113001.SH": BondTerms(sec_name="事件终止债"),
            "113002.SH": BondTerms(sec_name="正常债"),
        }

        def list_bonds(self):
            return list(self.data)

        def get(self, code):
            return self.data[code]

    def fake_project(code, terms, on_date, **_kwargs):
        if code == "113001.SH":
            return replace(terms, last_trading_date=date(2026, 5, 1))
        return terms

    monkeypatch.setattr(batch_pricing, "_project_terms_for_admission", fake_project)

    kept, excluded = split_batch_codes_from_cache(
        FakeTermsCache(),
        on_date=date(2026, 5, 20),
    )

    assert kept == ["113002.SH"]
    assert excluded == [("113001.SH", "已过最后交易日")]


def test_batch_pricing_exclusion_reason_blocks_hard_risks():
    check_date = date(2026, 4, 28)

    assert batch_pricing_exclusion_reason(
        "113050.SH",
        BondTerms(sec_name="南银转债", is_tradable=False),
        on_date=check_date,
    ) == "不可交易"
    assert batch_pricing_exclusion_reason(
        "113050.SH",
        BondTerms(sec_name="南银转债", trading_status="停牌"),
        on_date=check_date,
    ) == "停牌/暂停交易"
    assert batch_pricing_exclusion_reason(
        "113050.SH",
        BondTerms(sec_name="南银转债", last_trading_date=date(2026, 4, 27)),
        on_date=check_date,
    ) == "已过最后交易日"
    assert batch_pricing_exclusion_reason(
        "113050.SH",
        BondTerms(sec_name="南银转债", trading_status="退市"),
        on_date=check_date,
    ) == "已退市"
    assert batch_pricing_exclusion_reason(
        "113050.SH",
        {"sec_name": "南银转债", "call_status": "已公告强赎"},
        on_date=check_date,
    ) is None
    assert batch_pricing_exclusion_reason(
        "113050.SH",
        BondTerms(sec_name="南银转债", call_redemption_date=date(2026, 5, 6)),
        on_date=check_date,
    ) is None
    assert batch_pricing_exclusion_reason(
        "113050.SH",
        BondTerms(sec_name="南银转债", last_trading_date=date(2026, 5, 10)),
        on_date=check_date,
    ) is None
    assert batch_pricing_exclusion_reason(
        "113050.SH",
        BondTerms(sec_name="南银转债", underlying_name="*ST 测试"),
        on_date=check_date,
    ) == "正股 ST/退市风险"
    assert batch_pricing_exclusion_reason(
        "113050.SH",
        BondTerms(sec_name="南银转债", bond_turnover_amount=400.0),
        on_date=check_date,
        min_turnover_amount=1000.0,
    ) == "成交额过低"
    # 余额默认不再硬剔除 (DEFAULT_MIN_OUTSTANDING_BALANCE=None), 显式给阈值才生效。
    # 这里显式传值, 让用例测的是过滤逻辑本身而不是当期默认值。
    assert batch_pricing_exclusion_reason(
        "113050.SH",
        BondTerms(sec_name="南银转债", outstanding_balance=0.3),
        on_date=check_date,
    ) is None
    assert batch_pricing_exclusion_reason(
        "113050.SH",
        BondTerms(sec_name="南银转债", outstanding_balance=0.3),
        on_date=check_date,
        min_outstanding_balance=0.5,
    ) == "余额过小"
    assert batch_pricing_exclusion_reason(
        "113050.SH",
        BondTerms(sec_name="南银转债", credit_rating="A"),
        on_date=check_date,
    ) == "评级过低"


def test_batch_pool_screening_report_uses_configurable_thresholds():
    class FakeTermsCache:
        data = {
            "113001.SH": BondTerms(sec_name="大余额", outstanding_balance=2.0, credit_rating="AA"),
            "113002.SH": BondTerms(sec_name="小余额", outstanding_balance=0.8, credit_rating="AA"),
            "113003.SH": BondTerms(sec_name="低评级", outstanding_balance=2.0, credit_rating="A+"),
        }

        def list_bonds(self):
            return list(self.data)

        def get(self, code):
            return self.data[code]

    report = screen_batch_pool_from_cache(
        FakeTermsCache(),
        admission_config=AdmissionFilterConfig(
            min_outstanding_balance=1.0,
            min_credit_rating="AA-",
        ),
    )

    assert report["accepted"] == ["113001.SH"]
    assert summarize_exclusions(report["excluded"]) == {"余额过小": 1, "评级过低": 1}


def test_upcoming_tradable_cache_excludes_private_bonds():
    """扫新债关注池只收普通公募新债: 定向/私募券即使进入可交易窗口也不出现.

    非公开转债无集中竞价交易、常无上市正股关联 (如 145905.SH 智转债K1,
    发行人未上市), 进池只会在单债定价处撞"无正股代码"。
    """
    class FakeTermsCache:
        data = {
            # 定转命名 + 定向代码段, 可交易窗口内 — 不应出现
            "124025.SZ": BondTerms(
                sec_name="富乐定转",
                underlying_code="301297.SZ",
                issue_date=date(2026, 3, 9),
                listing_date=date(2026, 3, 9),
                conversion_price=16.14,
                close=99.99,
            ),
            # 真实案例: 非上市公司私募科创转债, 名字不带"定转"但代码段非公募,
            # private_pending 且 tradable_date 在窗口内 — 不应出现
            "145905.SH": BondTerms(
                sec_name="智转债K1",
                issue_date=date(2025, 12, 16),
                listing_date=date(2025, 12, 16),
                tradable_date=date(2026, 9, 8),
                trading_status="private_pending",
                conversion_price=9.51,
            ),
            # 普通公募存量老债 (非 pending) — 不应出现
            "113050.SH": BondTerms(
                sec_name="南银转债",
                issue_date=date(2021, 6, 15),
                listing_date=date(2021, 7, 1),
            ),
        }

        def list_bonds(self):
            return list(self.data)

        def get(self, code):
            return self.data[code]

    rows = list_upcoming_tradable_from_cache(
        FakeTermsCache(),
        on_date=date(2026, 9, 4),
        window_days=7,
    )

    assert rows == []


def test_upcoming_tradable_cache_includes_public_listing_metadata():
    class FakeTermsCache:
        data = {
            "123269.SZ": BondTerms(
                sec_name="金杨转债",
                underlying_code="301210.SZ",
                underlying_name="金杨精密",
                issue_date=date(2026, 5, 11),
                listing_date=date(2026, 5, 11),
                tradable_date=date(2026, 5, 11),
                trading_status="pending",
                conversion_price=39.8,
                credit_rating="AA-",
                outstanding_balance=9.8,
                maturity_date=date(2032, 4, 20),
            ),
        }

        def list_bonds(self):
            return list(self.data)

        def get(self, code):
            return self.data[code]

    rows = list_upcoming_tradable_from_cache(
        FakeTermsCache(),
        on_date=date(2026, 5, 9),
        window_days=7,
    )

    assert rows == [
        {
            "bond_code": "123269.SZ",
            "bond_name": "金杨转债",
            "stock_code": "301210.SZ",
            "underlying_name": "金杨精密",
            "issue_date": date(2026, 5, 11),
            "listing_date": date(2026, 5, 11),
            "tradable_date": date(2026, 5, 11),
            "days_to_trade": 2,
            "K": 39.8,
            "market_price": None,
            "credit_rating": "AA-",
            "outstanding_balance": 9.8,
            "maturity_date": date(2032, 4, 20),
            "is_tradable": False,
            "trading_status": "pending",
        }
    ]


def test_infer_trading_metadata_marks_issued_but_unlisted_as_pending():
    """已发行未上市: 起息日不能当可交易起点, 否则新债一发行就被判成 tradable."""
    from convertible_bond.data_providers import infer_cb_trading_metadata

    terms = BondTerms(
        sec_name="强达转债",
        issue_date=date(2026, 8, 19),
        listing_date=None,
        maturity_date=date(2032, 8, 19),
    )
    out = infer_cb_trading_metadata("123284.SZ", terms, date(2026, 8, 20))

    assert out.trading_status == "pending"
    assert out.is_tradable is False
    assert out.tradable_date is None
    assert out.listing_date is None  # 不回填, 上市日就该是空的


def test_infer_trading_metadata_ignores_stale_derived_status():
    """cb_data 里的 tradable/tradable_date 是上一轮推断写回的, 不能拿来自我确认."""
    from convertible_bond.data_providers import infer_cb_trading_metadata

    terms = BondTerms(
        sec_name="强达转债",
        issue_date=date(2026, 8, 19),
        listing_date=None,
        tradable_date=date(2026, 8, 19),   # 上一轮错误推断的残留
        is_tradable=True,
        trading_status="tradable",
    )
    out = infer_cb_trading_metadata("123284.SZ", terms, date(2026, 8, 20))

    assert out.trading_status == "pending"
    assert out.is_tradable is False


def test_infer_trading_metadata_flips_pending_back_once_listed():
    """上市日到了, 上一轮留下的 pending/False 必须翻回来 —— 否则自我确认永远翻不了身。

    ``is_issued_pending_listing`` 的文档早就点名了这个陷阱, 但当时只堵了**判定侧**:
    判定不看缓存值, 回填侧却仍让缓存里的 ``trading_status="pending"`` /
    ``is_tradable=False`` 覆盖新推断。这两个字段公募转债的数据源根本不提供
    (Wind ``get_admission_status`` 对它们显式返回 None), 所以缓存里读到的只可能是本函数
    自己上一次的输出。实测让派克转债 / 中仑转债两只上市首日分别成交 2.57 亿 / 12.95 亿
    的新债被准入判成"不可交易"。
    """
    from convertible_bond.data_providers import infer_cb_trading_metadata

    terms = BondTerms(
        sec_name="中仑转债",
        issue_date=date(2026, 8, 6),
        listing_date=date(2026, 8, 24),    # 挂牌了
        tradable_date=date(2026, 8, 24),
        is_tradable=False,                 # 上一轮"已发行未上市"那一档的残留
        trading_status="pending",
        bond_turnover_amount=1_295_018_009.9,
    )
    out = infer_cb_trading_metadata("123281.SZ", terms, date(2026, 8, 25))

    assert out.trading_status == "tradable"
    assert out.is_tradable is True
    assert batch_pricing_exclusion_reason(
        "123281.SZ", out, on_date=date(2026, 8, 25)) is None


def test_intraday_halt_is_not_a_suspension():
    """"盘中停牌"是上市首日的涨跌幅熔断, 几分钟到半小时, 收盘照样有巨额成交。

    它与"停牌/暂停交易"是两件事, 但子串匹配会先命中"停牌" —— 派克转债上市首日标着
    "盘中停牌"、当天成交 2.57 亿, 却被整只踢出主池。
    """
    terms = BondTerms(
        sec_name="派克转债",
        issue_date=date(2026, 8, 7),
        listing_date=date(2026, 8, 25),
        maturity_date=date(2032, 8, 6),
        conversion_price=10.0,
        credit_rating="AA",
        suspension_status="盘中停牌",
        underlying_status="否",
        underlying_trade_status="交易",
        bond_turnover_amount=257_450_679.0,
    )
    assert batch_pricing_exclusion_reason(
        "111026.SH", terms, on_date=date(2026, 8, 25)) is None

    # 真停牌仍然要拦住
    halted = replace(terms, suspension_status="停牌")
    assert batch_pricing_exclusion_reason(
        "111026.SH", halted, on_date=date(2026, 8, 25)) == "停牌/暂停交易"


def test_never_entered_market_is_excluded():
    """起息日与上市日同时缺失 = 没有任何证据表明这只债进过市场。

    实测 123095.SZ 日升转债: 2021-01 发行申购, 2021-02 东方日升业绩预告大幅亏损后**撤销
    发行**、申购资金退回, 从未上市交易。Wind 里仍留着代码、到期日 2027-01-22 和一个
    99.994 的陈旧价, 于是它带着 AA 评级被定出 −14% 低估躺在主池里。

    ``infer_cb_trading_metadata`` 兜不住: 两个日期都没有时 tradable_date 为 None, 而那里的
    ``inferred_is_tradable = tradable_date is None or ...`` 把"没有日期"读成"随时可交易" ——
    那个默认对定向债是对的, 对撤销发行的公募债恰好反了。
    """
    terms = BondTerms(
        sec_name="日升转债",
        issue_date=None,
        listing_date=None,
        maturity_date=date(2027, 1, 22),
        conversion_price=28.01,
        credit_rating="AA",
        outstanding_balance=None,
    )
    assert batch_pricing_exclusion_reason(
        "123095.SZ", terms, on_date=date(2026, 8, 26)) == "无发行与上市日期"


def test_missing_listing_date_alone_is_not_enough_to_exclude():
    """只缺上市日不算 —— 那是"已发行未上市"或老债数据缺口, 各有各的判据。

    实测全库"有起息日却没上市日"的 35 只, 而"有上市日却没起息日"的 **0 只**: 两个都缺
    才是确定的信号, 缺一个不是。
    """
    fresh = BondTerms(sec_name="强达转债", issue_date=date(2026, 8, 19),
                      listing_date=None, maturity_date=date(2032, 8, 19))
    assert batch_pricing_exclusion_reason(
        "123284.SZ", fresh, on_date=date(2026, 8, 20)) == "已发行未上市"

    old = BondTerms(sec_name="岭南转债", issue_date=date(2018, 8, 14), listing_date=None,
                    maturity_date=date(2030, 1, 1), conversion_price=10.0,
                    credit_rating="AA", underlying_status="否", underlying_trade_status="交易")
    assert batch_pricing_exclusion_reason(
        "128044.SZ", old, on_date=date(2026, 8, 20)) != "无发行与上市日期"


def test_infer_trading_metadata_keeps_old_missing_listing_date_tradable():
    """起息日已久却仍缺上市日 → 数据缺口, 保持旧的保守判定, 不误杀老债."""
    from convertible_bond.data_providers import infer_cb_trading_metadata

    terms = BondTerms(
        sec_name="岭南转债",
        issue_date=date(2018, 8, 14),
        listing_date=None,
    )
    out = infer_cb_trading_metadata("128044.SZ", terms, date(2026, 8, 20))

    assert out.trading_status == "tradable"
    assert out.is_tradable is True


def test_upcoming_tradable_cache_includes_issued_but_unlisted():
    """新债扫描要覆盖"已发行未上市"这一档 — 上市日待定, 不受 window_days 约束."""
    class FakeTermsCache:
        data = {
            # 已发行未上市 (Wind 还没给 ipo_date) → 应出现, 上市日/可交易日待定
            "123284.SZ": BondTerms(
                sec_name="强达转债",
                underlying_code="301628.SZ",
                issue_date=date(2026, 8, 19),
                listing_date=None,
                conversion_price=84.04,
                maturity_date=date(2032, 8, 19),
            ),
            # 已定上市日且落在窗口内 → 应出现
            "123281.SZ": BondTerms(
                sec_name="中仑转债",
                underlying_code="301565.SZ",
                issue_date=date(2026, 8, 6),
                listing_date=date(2026, 8, 24),
                conversion_price=20.28,
                maturity_date=date(2032, 8, 6),
            ),
            # 缺上市日但起息已久 → 数据缺口而非新债, 不应出现
            "128044.SZ": BondTerms(
                sec_name="岭南转债",
                issue_date=date(2018, 8, 14),
                listing_date=None,
            ),
        }

        def list_bonds(self):
            return list(self.data)

        def get(self, code):
            return self.data[code]

    rows = list_upcoming_tradable_from_cache(
        FakeTermsCache(), on_date=date(2026, 8, 20), window_days=30)

    by_code = {row["bond_code"]: row for row in rows}
    assert set(by_code) == {"123281.SZ", "123284.SZ"}
    # 已定上市日的排在待定之前
    assert [row["bond_code"] for row in rows] == ["123281.SZ", "123284.SZ"]
    assert by_code["123281.SZ"]["days_to_trade"] == 4
    assert by_code["123284.SZ"]["tradable_date"] is None
    assert by_code["123284.SZ"]["days_to_trade"] is None
    assert by_code["123284.SZ"]["K"] == 84.04


def test_exclusion_reason_reports_issued_but_unlisted():
    """已发行未上市既买不到也没有市价可比 — 归"扫新债"关注池而不是主池."""
    terms = BondTerms(
        sec_name="强达转债",
        issue_date=date(2026, 8, 19),
        listing_date=None,
        maturity_date=date(2032, 8, 19),
        credit_rating="AA-",
        outstanding_balance=5.5,
    )
    reason = batch_pricing_exclusion_reason(
        "123284.SZ", terms, on_date=date(2026, 8, 20))
    assert reason == "已发行未上市"

    listed = replace(terms, listing_date=date(2026, 8, 25))
    assert batch_pricing_exclusion_reason(
        "123284.SZ", listed, on_date=date(2026, 9, 1)) is None


def test_merge_upcoming_pricing_results_adds_theoretical_price():
    merged = merge_upcoming_pricing_results(
        [
            {
                "bond_code": "124025.SZ",
                "bond_name": "富乐定转",
                "K": 16.14,
                "tradable_date": date(2026, 9, 9),
            }
        ],
        [
            {
                "bond_code": "124025.SZ",
                "bond_name": "富乐定转",
                "stock_code": "301297.SZ",
                "K": 16.14,
                "S0": 40.07,
                "sigma": 0.46,
                "theoretical_price": 245.6,
                "market_price": 99.99,
                "status": "ok",
            }
        ],
    )

    assert merged[0]["theoretical_price"] == 245.6
    assert merged[0]["S0"] == 40.07
    assert merged[0]["status"] == "ok"


def test_annotate_batch_result_adds_review_metrics_and_tags():
    row = annotate_batch_result({
        "bond_code": "118033.SH",
        "status": "ok",
        "S0": 208.27,
        "K": 82.75,
        "sigma": 1.32,
        "theoretical_price": 310.0,
        "market_price": 218.9,
        "deviation": -0.294,
        "credit_rating": "AA-",
        "outstanding_balance": 6.1,
        "T": 2.9,
    })

    assert row["parity"] == pytest.approx(251.69, rel=1e-3)
    assert row["conversion_premium"] == pytest.approx(-0.130, rel=1e-2)
    assert "模型低估" in row["risk_tags"]
    assert row["undervaluation_rate"] == pytest.approx(0.294)
    assert "转股折价" in row["risk_tags"]
    assert "高HV" in row["risk_tags"]
    assert row["confidence"] in {"中", "低"}
    assert row["model_signal_status"] == "不适合作为买入信号"
    assert row["sensitivity_status"] == "波动率敏感"
    # 「高HV」属模型适用性维度 —— 模型在这只债上不可靠, 但那是**永久属性**, 不是
    # "去做点什么就能解决"的事, 所以单列「模型存疑」而不是塞进需复核。
    # 需复核只留数据质量 + 可交易性 (这一行现在不能用, 得先去拉数据/确认能不能交易)。
    assert row["review_bucket"] == "模型存疑"
    assert row["review_notes"]
    assert math.isfinite(row["opportunity_score"])


def test_annotate_batch_result_flags_underlying_risk_and_down_uplift():
    row = annotate_batch_result({
        "bond_code": "110081.SH",
        "status": "ok",
        "S0": 18.0,
        "K": 30.0,
        "sigma": 0.35,
        "theoretical_price": 110.0,
        "no_down_price": 98.0,
        "market_price": 90.0,
        "deviation": -0.1818,
        "credit_rating": "AA",
        "outstanding_balance": 20.0,
        "T": 1.5,
        "underlying_status": "ST/退市风险",
    })

    assert row["down_reset_uplift"] == pytest.approx(12.0)
    assert "正股风险" in row["risk_tags"]
    assert "下修贡献高" in row["risk_tags"]
    assert row["model_signal_status"] == "不适合作为买入信号"


def test_sort_batch_results_for_review_penalizes_noisy_deviation():
    rows = sort_batch_results_for_review([
        {
            "bond_code": "NOISY",
            "status": "ok",
            "S0": 12.0,
            "K": 10.0,
            "sigma": 1.45,
            "theoretical_price": 200.0,
            "market_price": 140.0,
            "deviation": -0.30,
            "credit_rating": "A",
            "outstanding_balance": 0.2,
            "T": 0.3,
        },
        {
            "bond_code": "CLEAN",
            "status": "ok",
            "S0": 16.0,
            "K": 10.0,
            "sigma": 0.42,
            "theoretical_price": 176.0,
            "market_price": 148.0,
            "deviation": -0.16,
            "credit_rating": "AA+",
            "outstanding_balance": 12.0,
            "T": 2.0,
        },
    ])

    assert rows[0]["bond_code"] == "CLEAN"
    assert "转股折价" in rows[0]["risk_tags"]
    assert rows[0]["opportunity_score"] > rows[1]["opportunity_score"]


def test_filter_batch_results_by_view_splits_review_lists():
    rows = [
        {
            "bond_code": "VALUE",
            "status": "ok",
            "S0": 16.0,
            "K": 10.0,
            "sigma": 0.42,
            "theoretical_price": 195.0,
            "market_price": 166.0,
            "deviation": -0.15,
            "credit_rating": "AA+",
            "outstanding_balance": 12.0,
            "T": 2.0,
        },
        {
            "bond_code": "DISCOUNT",
            "status": "ok",
            "S0": 20.0,
            "K": 10.0,
            "sigma": 0.45,
            "theoretical_price": 214.0,
            "market_price": 188.0,
            "deviation": -0.12,
            "credit_rating": "AA",
            "outstanding_balance": 8.0,
            "T": 2.0,
        },
        {
            "bond_code": "NOISY",
            "status": "ok",
            "S0": 12.0,
            "K": 10.0,
            "sigma": 1.2,
            "theoretical_price": 190.0,
            "market_price": 140.0,
            "deviation": -0.26,
            "credit_rating": "A",
            "outstanding_balance": 0.2,
            "T": 0.3,
        },
    ]

    assert [r["bond_code"] for r in filter_batch_results_by_view(rows, "低估候选")] == ["VALUE"]
    assert [r["bond_code"] for r in filter_batch_results_by_view(rows, "转股折价")] == ["DISCOUNT"]
    assert [r["bond_code"] for r in filter_batch_results_by_view(rows, "需复核")] == ["NOISY"]


def test_write_batch_results_csv_uses_stable_columns(tmp_path):
    path = tmp_path / "batch.csv"
    write_batch_results_csv(
        path,
        [
            {
                "bond_code": "128009.SZ",
                "status": "ok",
                "S0": 55.0,
                "deviation": -0.0123456,
                "market_price": None,
            },
            {
                "bond_code": "113050.SH",
                "status": "数据源未返回转股价 K",
                "S0": 50.0,
                "theoretical_price": math.nan,
                "deviation": math.nan,
            },
        ],
    )

    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))

    assert rows[0] == BATCH_RESULT_COLUMNS
    assert rows[1][0] == "128009.SZ"
    assert rows[1][8] == "-0.012346"
    assert rows[2][3] == ""
    assert rows[2][8] == ""


def test_batch_results_cache_round_trips_dates_and_nan(tmp_path):
    path = tmp_path / "batch_cache.json"
    save_batch_results_cache(
        [
            {
                "bond_code": "128009.SZ",
                "valuation_date": date(2026, 4, 27),
                "status": "ok",
                "deviation": math.nan,
            }
        ],
        path=path,
        source="unit-test",
        params={"r": 0.02},
        upcoming_results=[
            {
                "bond_code": "124025.SZ",
                "tradable_date": date(2026, 9, 9),
                "theoretical_price": 245.6,
                "status": "ok",
            }
        ],
    )

    loaded = load_batch_results_cache(path)

    assert loaded["meta"]["source"] == "unit-test"
    assert loaded["meta"]["n_results"] == 1
    assert loaded["meta"]["n_upcoming_results"] == 1
    assert loaded["results"][0]["valuation_date"] == "2026-04-27"
    assert math.isnan(loaded["results"][0]["deviation"])
    assert loaded["upcoming_results"][0]["tradable_date"] == "2026-09-09"
    assert loaded["upcoming_results"][0]["theoretical_price"] == 245.6


# ── Step 1: 余额从硬过滤降级为标签 ──
#
# 全库回填摘牌元数据后实测: 关掉余额门槛主池 270 → 270, 独立贡献为 0。它此前 99% 的
# 作用是替缺失的 delisting_date 兜底 (被它剔的 225 只里 223 只余额恰为 0 的已退市券),
# 而那个职责已由日期判据接管。余额本身改由按 3,000 万法定线分档的风险标签表达。

def test_balance_is_not_a_hard_filter_by_default():
    """默认放行小余额; 传了阈值才剔除 —— 字段与语义保留, 只是默认不启用。"""
    check_date = date(2026, 4, 28)
    tiny = BondTerms(sec_name="南银转债", outstanding_balance=0.01)
    assert batch_pricing_exclusion_reason("113050.SH", tiny, on_date=check_date) is None
    assert batch_pricing_exclusion_reason(
        "113050.SH", tiny, on_date=check_date, min_outstanding_balance=0.5) == "余额过小"
    assert AdmissionFilterConfig().min_outstanding_balance is None


def _annotated(**overrides):
    row = dict(status="ok", S0=12.0, K=13.5, theoretical_price=110.0, market_price=99.0,
               deviation=-0.10, sigma=0.30, T=3.0, credit_rating="AA",
               outstanding_balance=4.25, valuation_date="2026-08-22")
    row.update(overrides)
    return annotate_batch_result(row)


@pytest.mark.parametrize("balance, tag", [
    (0.0, "余额清零"),        # 已转股完毕/已赎回, 是退市信号不是"数据异常"
    (0.15, "触及摘牌线"),      # 低于 3,000 万法定线, 交易所将安排停止交易
    (0.29, "触及摘牌线"),
    (0.35, "临近摘牌线"),
    (0.80, "小余额"),
])
def test_balance_tags_follow_statutory_delisting_line(balance, tag):
    assert tag in _annotated(outstanding_balance=balance)["risk_tags"]


def test_normal_balance_gets_no_balance_tag():
    assert not ({"余额清零", "触及摘牌线", "临近摘牌线", "小余额"}
                & set(_annotated(outstanding_balance=5.0)["risk_tags"]))


def test_legacy_balance_tags_stay_hard_for_old_cached_rows():
    """旧批量缓存与旧策略快照里存的是「极小余额/余额异常」, 保留以免旧数据静默失去硬标签。"""
    assert {"极小余额", "余额异常"} <= HARD_REVIEW_TAGS


@pytest.mark.parametrize("last_trading_date, expect_tag, days", [
    ("2026-08-24", True, 2),      # 后天停止交易
    ("2026-09-21", True, 30),     # 窗口边界内
    ("2026-09-22", False, 31),    # 窗口外
    ("2026-08-21", False, -1),    # 已过 (由准入层剔除, 不在这里打标签)
])
def test_near_delisting_tag_uses_announced_last_trading_date(last_trading_date, expect_tag, days):
    """存续券的 delisting_date 多数等于到期日 (预定摘牌, 非事件), 所以只认 last_trading_date。"""
    row = _annotated(last_trading_date=last_trading_date)
    assert row["days_to_last_trading"] == days
    assert ("临近摘牌" in row["risk_tags"]) is expect_tag


def test_near_delisting_leaves_undervalued_view_but_not_strategy_defaults():
    """两只其余完全相同的债: 后天停止交易的那只掉出「低估候选」进「需复核」。

    但「临近摘牌」不进 HARD_REVIEW_TAGS —— 那是策略层 exclude_risk_tags 的默认值,
    动它就是默认选债行为变更; 要不要因此不买由策略参数决定。
    """
    near = _annotated(bond_code="113697.SH", last_trading_date="2026-08-24")
    norm = _annotated(bond_code="113000.SH")
    assert near["review_bucket"] == "需复核"
    assert norm["review_bucket"] == "低估候选"

    rows = [near, norm]
    assert [r["bond_code"] for r in filter_batch_results_by_view(rows, "低估候选")] == ["113000.SH"]
    assert [r["bond_code"] for r in filter_batch_results_by_view(rows, "需复核")] == ["113697.SH"]
    assert "临近摘牌" not in HARD_REVIEW_TAGS
    assert any("最后交易日" in n for n in near["review_notes"])


# ── 下修减值 ──
#
# 同网格求解后 uplift 理应 >= 0 (下修降 K 对持有人是额外期权)。这个标签是**安全网**:
# 预期命中数≈0, 一旦亮起就是模型或数值出了需要人看的事。
# 历史教训: 混网格时代 282 只里 55 只 uplift 为负, 曾被误判为"真实减值", 实测其值恰等于
# "粗网格价 − 细网格价"的相反数 (118064.SH +1.325 vs -1.325) —— 全部是伪信号。

def _uplift_row(theo, no_down, **kw):
    row = dict(status="ok", S0=85.0, K=55.13, theoretical_price=theo, market_price=200.0,
               deviation=0.07, sigma=0.35, T=3.0, credit_rating="AA",
               outstanding_balance=5.0, valuation_date="2026-08-22", no_down_price=no_down)
    row.update(kw)
    return annotate_batch_result(row)


@pytest.mark.parametrize("theo, no_down, expected", [
    (186.47, 191.55, "下修减值"),      # -5.08 = -2.7%, 远超阈值
    (100.0, 100.6, "下修减值"),        # 恰好 -0.6%, 超阈值
    (100.0, 100.4, None),              # -0.4%, 阈值内, 不标
    (100.0, 100.0, None),
    (108.0, 100.0, "下修贡献高"),      # +7.4%... 未达 8%
])
def test_down_reset_drag_and_boost_tags(theo, no_down, expected):
    tags = [t for t in _uplift_row(theo, no_down)["risk_tags"] if t.startswith("下修")]
    if expected == "下修贡献高" and (theo - no_down) / theo < 0.08:
        assert tags == []                      # 正向阈值仍是 8%, 没到就不标
    elif expected is None:
        assert tags == []
    else:
        assert tags == [expected]


def test_down_reset_boost_tag_still_fires_above_8pct():
    assert "下修贡献高" in _uplift_row(110.0, 100.0)["risk_tags"]


def test_down_reset_drag_is_visibility_only_not_a_selection_change():
    """标签只提高可见度: 不进 HARD_REVIEW_TAGS (= 策略 exclude_risk_tags 默认值),
    也不进 REVIEW_ONLY_TAGS —— 它是"这里有反常, 去看一眼"的提示, 不是买入禁令。"""
    assert "下修减值" not in HARD_REVIEW_TAGS
    assert "下修减值" not in REVIEW_ONLY_TAGS
    assert "下修贡献高" not in HARD_REVIEW_TAGS      # 与正向标签对称处理
    row = _uplift_row(186.47, 191.55)
    assert row["review_bucket"] != "需复核"          # 仅凭这个标签不改分桶
    assert any("下修" in n for n in row["review_notes"])


# ── 标签维度体系 ──────────────────────────────────────────────────────────
#
# 28+ 个标签曾挤在一个扁平集合里驱动四个消费者 (展示 / 置信度 / 批量页视图 /
# 策略 exclude_risk_tags), 调一个阈值会同时穿透四层。现在按维度归类, 各消费者按需取子集。

def test_every_emitted_tag_is_registered_in_a_dimension():
    """凡是 annotate_batch_result 会打出的标签, 都必须登记维度。

    漏登记不会报错, 只会让该标签在所有按维度取子集的消费者那里**静默消失** ——
    既不拦路也不显示分组, 而 ruff 和现有测试都查不出来。
    """
    import re
    from pathlib import Path
    source = Path(batch_pricing_module.__file__).read_text(encoding="utf-8")
    emitted = set(re.findall(r'risk_tags\.append\("([^"]+)"\)', source))
    assert emitted, "没扫到任何 risk_tags.append —— 扫描逻辑坏了, 后面的断言会假通过"
    missing = emitted - set(RISK_TAG_DIMENSION)
    assert not missing, f"这些标签没登记维度: {sorted(missing)}"


def test_dimensions_partition_the_tag_space():
    """每个标签恰好属于一个维度, 且五个维度都非空。"""
    dims = set(RISK_TAG_DIMENSION.values())
    assert dims == {"数据质量", "模型适用性", "标的风险", "可交易性", "机会信号"}
    for dim in dims:
        assert tags_in(dim), f"{dim} 维度为空"
    # tags_in 取并集时不重不漏
    assert sum(len(tags_in(d)) for d in dims) == len(RISK_TAG_DIMENSION)


def test_strategy_exclude_set_is_frozen_independently_of_display_tags():
    """策略默认排除集与批量页展示用的 HARD_REVIEW_TAGS **必须解耦**。

    它曾写成 tuple(sorted(HARD_REVIEW_TAGS)) —— 那样任何为了改展示而增删标签的动作都会
    自动变成默认选债行为变更 (实测该集合极敏感: 改成只排数据+可交易, 候选池 59 → 262)。
    """
    from convertible_bond.strategy_backtest import ScoreStrategyConfig
    assert set(ScoreStrategyConfig().exclude_risk_tags) == set(LEGACY_STRATEGY_EXCLUDE_TAGS)
    # 拦截集只含数据质量 + 可交易性; 冻结集是历史快照, 两者本就不同
    assert set(LEGACY_STRATEGY_EXCLUDE_TAGS) != tags_in("数据质量", "可交易性")


def test_view_membership_has_a_single_source_of_truth():
    """filter_batch_results_by_view 与策略页落选解释必须用同一个判据。

    二者曾各自实现一份, 在标签体系重构后悄悄分叉: 视图改读维度拦截集, 而
    strategy_backtest._candidate_filter_reason 还硬编码 HARD_REVIEW_TAGS。
    """
    rows = [
        _annotated(bond_code="A.SZ", outstanding_balance=5.0),
        _annotated(bond_code="B.SZ", sigma=1.5),
        _annotated(bond_code="C.SZ", status="error"),
    ]
    for view in BATCH_REVIEW_VIEWS:
        kept = {r["bond_code"] for r in filter_batch_results_by_view(rows, view)}
        by_reason = {r["bond_code"] for r in rows
                     if view_exclusion_reason(r, view) is None}
        assert kept == by_reason, f"{view} 两条路径不一致"


def test_deviation_outlier_tags_split_by_direction():
    """贵侧与便宜侧的后验含义相反, 不能用一个对称的「异常」标签。

    对称处理会把唯一的假设来源删掉 —— 实测唯一一只机会分 ≥8 的债 (dev −0.158)
    被自己标成异常踢出低估候选。
    """
    med = 0.19
    rich = annotate_batch_result(
        dict(status="ok", S0=12.0, K=13.5, theoretical_price=100.0, market_price=145.0,
             deviation=med + 0.25, sigma=0.3, T=3.0, credit_rating="AA",
             outstanding_balance=5.0, valuation_date="2026-08-23"),
        market_median_deviation=med)
    cheap = annotate_batch_result(
        dict(status="ok", S0=12.0, K=13.5, theoretical_price=100.0, market_price=90.0,
             deviation=med - 0.25, sigma=0.3, T=3.0, credit_rating="AA",
             outstanding_balance=5.0, valuation_date="2026-08-23"),
        market_median_deviation=med)

    assert "模型高估离群" in rich["risk_tags"]
    assert "深度低估待核" in cheap["risk_tags"]
    # 便宜侧不扣置信度、不进任何拦截集 —— 它是待检验的假设, 不是要剔除的噪声
    assert "深度低估待核" not in LEGACY_STRATEGY_EXCLUDE_TAGS
    assert RISK_TAG_DIMENSION["深度低估待核"] == "机会信号"
    assert RISK_TAG_DIMENSION["模型高估离群"] == "模型适用性"
    assert any("待检验" in n or "低于模型" in n for n in cheap["review_notes"])


def test_review_bucket_is_a_real_partition():
    """四个分桶互斥且覆盖全部 —— 而不是 79% 挤在需复核里。"""
    rows = [
        _annotated(bond_code="ok.SZ", outstanding_balance=5.0),
        _annotated(bond_code="hv.SZ", sigma=1.5),
        _annotated(bond_code="bad.SZ", status="error"),
    ]
    buckets = [r["review_bucket"] for r in rows]
    assert len(buckets) == len(rows)                    # 每行恰好一个桶
    assert set(buckets) <= {"综合机会", "低估候选", "转股折价", "需复核", "模型存疑"}
    # 高 σ = 模型适用性 → 模型存疑, 不该占用需复核
    assert dict(zip([r["bond_code"] for r in rows], buckets))["hv.SZ"] == "模型存疑"
    assert dict(zip([r["bond_code"] for r in rows], buckets))["bad.SZ"] == "需复核"


# ── 「低估候选」的横截面口径 ────────────────────────────────────────────────
#
# 旧口径 opportunity_score >= 8.0 是绝对阈值架在水平时变的量上: 实测 2026-08-22
# 主池 280 只只剩 1 只候选, 页面默认打开等于空表。而 cb_valuation_history 20 期
# 基线显示中位偏差摆幅 21.2pp、便宜尾形状 (p25-中位) 摆幅只有 4.2pp —— 判据必须
# 锚在当期横截面上。

def _pool(n, *, level, spread=0.30, **overrides):
    """造一个 n 只的定价池: deviation 以 *level* 为中位、在 ±spread/2 内均匀铺开。

    S0/K/theo 的取值有窄窗口, 动之前先算: 转股价值 88.9 要同时满足
      · 低于这批最低市价 (否则便宜的一头带上「转股折价」, 被单独归类进另一个视图)
      · 高于 theo/1.45 (否则整批带上「模型溢价高」, 全被吸进「模型存疑」分桶)
    spread 超过约 0.3 后这两条就不可兼得 —— 那不是 fixture 的毛病, 是"理论价远高于
    市价"本来就该同时被标成深度低估**和**模型溢价高。要测长度上限就靠加 n, 不靠加 spread。
    """
    rows = []
    for i in range(n):
        dev = level + spread * (i / (n - 1) - 0.5)
        row = dict(status="ok", bond_code=f"{100000 + i}.SZ", S0=12.0, K=13.5,
                   theoretical_price=110.0, market_price=110.0 * (1 + dev),
                   deviation=dev, sigma=0.30, T=3.0, credit_rating="AA",
                   outstanding_balance=4.25, valuation_date="2026-08-22")
        row.update(overrides)
        rows.append(row)
    return rows


@pytest.mark.parametrize("level", [0.004, 0.13, 0.216])
def test_undervalued_view_survives_the_market_level_cycle(level):
    """同一批相对结构的债, 整体估值水平从熊市谷底搬到牛市高位, 候选数不变。

    这是换口径的**全部理由**: 绝对阈值下候选数随周期塌缩/泛滥 (实测 280 只主池
    在 +18.7% 中位下只剩 1 只), 相对口径下不随水平漂移。
    三个 level 取自 cb_valuation_history 的实际极值与中枢。
    """
    rows = filter_batch_results_by_view(_pool(120, level=level), "低估候选")
    assert len(rows) == _selection_cutoff(120, DEFAULT_UNDERVALUED_PERCENTILE)
    # 入选的必须是这批里最便宜的那一头, 而不是碰巧评级高的
    assert rows[0]["relative_deviation"] < rows[-1]["relative_deviation"] <= -MIN_RELATIVE_CHEAPNESS


def test_undervalued_view_goes_empty_when_dispersion_collapses():
    """离散度塌掉时候选诚实归零 —— 分位闸单独用会永远凑满 15%。

    这是相对便宜度下限 (闸①) 存在的唯一理由: 它能表达"今天真的没有便宜货",
    而纯分位口径表达不了。
    """
    flat = _pool(120, level=0.15, spread=0.01)     # 全市场挤在中位附近
    assert filter_batch_results_by_view(flat, "低估候选") == []


def test_undervalued_view_caps_list_length_when_many_are_cheap():
    """反过来: 便宜的一大片时闸② 挡住"几百只候选", 保持名单可人工复核。"""
    wide = _pool(200, level=0.10)
    kept = filter_batch_results_by_view(wide, "低估候选")
    assert len(kept) == _selection_cutoff(200, DEFAULT_UNDERVALUED_PERCENTILE) == 30
    # 闸① 单独会放行两倍以上 —— 证明上面那个数确实是闸② 定的, 不是闸① 恰好卡在 30
    passing_floor = [r for r in annotate_batch_results(wide)
                     if r["relative_deviation"] <= -MIN_RELATIVE_CHEAPNESS]
    assert len(passing_floor) > 2 * len(kept)


def test_selection_cutoff_keeps_small_batches_usable():
    """小批量 (关注池/新债) 下 15% 会把名单削没, MIN_VIEW_ROWS 兜底。"""
    assert _selection_cutoff(6, DEFAULT_UNDERVALUED_PERCENTILE) == 6
    assert _selection_cutoff(40, DEFAULT_UNDERVALUED_PERCENTILE) == MIN_VIEW_ROWS
    assert _selection_cutoff(200, DEFAULT_UNDERVALUED_PERCENTILE) == 30
    assert _selection_cutoff(0, DEFAULT_UNDERVALUED_PERCENTILE) == 0


def test_legacy_absolute_score_gate_still_reachable():
    """显式传阈值 = 旧的绝对机会分口径, 供旧快照复现与对照实验。"""
    pool = _pool(120, level=0.13)
    legacy = filter_batch_results_by_view(
        pool, "低估候选",
        undervalued_score_threshold=DEFAULT_UNDERVALUED_SCORE_THRESHOLD)
    assert len(legacy) < len(filter_batch_results_by_view(pool, "低估候选"))


def test_review_bucket_and_undervalued_view_stay_in_sync():
    """分桶与视图必须同判据 —— 曾经一个硬编码 8.0, 另一个另写一份。"""
    rows = annotate_batch_results(_pool(120, level=0.13))
    in_view = {r["bond_code"] for r in filter_batch_results_by_view(rows, "低估候选")}
    in_bucket = {r["bond_code"] for r in rows if r["review_bucket"] == "低估候选"}
    assert in_view and in_bucket
    # 精确契约: 没被更高优先级的桶吸走的行, 分桶与视图必须逐行一致
    higher_priority = {"需复核", "模型存疑", "转股折价"}
    for row in rows:
        if row["review_bucket"] in higher_priority:
            continue
        assert (row["review_bucket"] == "低估候选") is (row["bond_code"] in in_view)
    assert in_bucket <= in_view


# ── 双低 ──

def test_double_low_matches_strategy_rank_signal():
    """批量页的双低值必须与 strategy_backtest 的 double_low 信号逐字同口径。"""
    from convertible_bond.strategy_backtest import _rank_signal_value
    row = annotate_batch_result(dict(
        status="ok", S0=12.0, K=13.5, theoretical_price=110.0, market_price=118.0,
        deviation=0.07, sigma=0.30, T=3.0, credit_rating="AA",
        outstanding_balance=4.25, valuation_date="2026-08-22"))
    assert row["double_low"] == pytest.approx(_rank_signal_value(row, "double_low"))


def test_double_low_view_takes_the_lowest_and_ignores_model_confidence():
    """双低是纯市场量 (价格+溢价), 不该被模型置信度筛掉。"""
    rows = _pool(120, level=0.13, sigma=1.5)       # 全部高 HV → 模型置信度低
    kept = filter_batch_results_by_view(rows, "双低")
    assert len(kept) == _selection_cutoff(120, DEFAULT_UNDERVALUED_PERCENTILE)
    ordered = sort_batch_results_for_view(kept, "双低")
    values = [r["double_low"] for r in ordered]
    assert values == sorted(values)
    # 入选的最贵一只仍不贵于落选的最便宜一只 —— 即确实是"最低的一截"
    kept_codes = {r["bond_code"] for r in kept}
    dropped = [r["double_low"] for r in annotate_batch_results(rows)
               if r["bond_code"] not in kept_codes]
    assert max(values) <= min(dropped)


def test_view_sort_must_not_reannotate_a_filtered_subset():
    """在过滤后的子集上重排不得改动相对偏差 —— 重新标注会把中位算到子集上。"""
    base = sort_batch_results_for_review(_pool(120, level=0.13))
    subset = filter_batch_results_by_view(base, "低估候选")
    before = {r["bond_code"]: r["relative_deviation"] for r in subset}
    after = {r["bond_code"]: r["relative_deviation"]
             for r in sort_batch_results_for_view(subset, "低估候选")}
    assert before == after


# ── 评级阶梯的两个修复 ──

def test_rating_bonus_ladder_is_monotone():
    """AAA 曾因 "AAA".startswith("AA+") 为假而落到 +2.0 分支, 比 AA+ 还低、与 AA 同分。"""
    def quality(rating):
        return annotate_batch_result(dict(
            status="ok", S0=12.0, K=13.5, theoretical_price=110.0, market_price=99.0,
            deviation=-0.10, sigma=0.30, T=3.0, credit_rating=rating,
            outstanding_balance=4.25, valuation_date="2026-08-22"))["quality_score"]

    ladder = [quality(r) for r in ("AAA", "AA+", "AA", "AA-", "A+")]
    assert ladder == sorted(ladder, reverse=True)
    assert len(set(ladder)) == len(ladder)          # 不许有并列, 否则档就没意义


@pytest.mark.parametrize("rating, expect_low", [
    ("AAsti", False),      # AA + 稳定展望: 曾掉进 LOW_RATING_PREFIXES 的 "A" 被判低评级
    ("AA+sti", False),
    ("AAAsti", False),
    ("AA-sti", False),
    ("A+", True),
    ("BBB", True),
])
def test_rating_outlook_suffix_does_not_flip_low_rating(rating, expect_low):
    """打分层与准入层必须同一套评级口径 —— 前者曾用裸前缀匹配, 两边对 AAsti 判反。"""
    row = annotate_batch_result(dict(
        status="ok", S0=12.0, K=13.5, theoretical_price=110.0, market_price=99.0,
        deviation=-0.10, sigma=0.30, T=3.0, credit_rating=rating,
        outstanding_balance=4.25, valuation_date="2026-08-22"))
    assert ("低评级" in row["risk_tags"]) is expect_low
    # 与准入层的判据一致: 低评级 <=> 低于 A+ 之上的档
    assert (batch_pricing._rating_score(rating) < batch_pricing._RATING_SCORES["AA-"]) is expect_low


# ── 下修优势 ──
#
# 稳健下修优势是策略页的默认排序信号, 而批量页此前跑完整 PDE 网格却整批丢掉这族
# 信号 (实测缓存 down_reset_robust_edge_value 有值 0/280) —— 全项目最贴近模型能力
# 边界的信号, 在"找机会"的页面上是空列。

def _edge_row(edge, **kw):
    row = dict(status="ok", bond_code=kw.pop("bond_code", "113000.SH"),
               S0=12.0, K=13.5, theoretical_price=110.0, market_price=118.0,
               deviation=0.07, sigma=0.30, T=3.0, credit_rating="AA",
               outstanding_balance=4.25, valuation_date="2026-08-22",
               down_reset_robust_edge_value=edge)
    row.update(kw)
    return row


def test_down_reset_edge_view_keeps_only_positive_edge():
    """零点在这里是**有意义**的: 最差角点下模型价仍高于市价才算有优势。"""
    rows = [_edge_row(3.0, bond_code="POS.SH"), _edge_row(-3.0, bond_code="NEG.SH"),
            _edge_row(0.0, bond_code="ZERO.SH")]
    kept = [r["bond_code"] for r in filter_batch_results_by_view(rows, "下修优势")]
    assert kept == ["POS.SH"]
    assert "不为正" in view_exclusion_reason(
        annotate_batch_results(rows)[1], "下修优势")


def test_down_reset_edge_view_ranks_strongest_first():
    rows = [_edge_row(e, bond_code=f"{100000 + i}.SZ")
            for i, e in enumerate([1.0, 9.0, 5.0, -2.0])]
    ordered = sort_batch_results_for_view(
        filter_batch_results_by_view(rows, "下修优势"), "下修优势")
    assert [r["down_reset_robust_edge_value"] for r in ordered] == [9.0, 5.0, 1.0]


def test_down_reset_edge_view_explains_missing_signal():
    """没开 compute_pde_signals 与"这只债反解不出隐含强度"是两回事, 落选解释要分得开。"""
    off = annotate_batch_results([_edge_row(math.nan)])[0]
    assert view_exclusion_reason(off, "下修优势") == "未计算 PDE 下修信号"
    unsolvable = annotate_batch_results(
        [_edge_row(math.nan, pde_down_reset_signal_status="no_implied_solution")])[0]
    assert "反解不出" in view_exclusion_reason(unsolvable, "下修优势")


def test_down_reset_edge_view_caps_length_in_a_cheap_market():
    """贵市场里这个视图合法地变空; 但谷底时 5% 会变成几百只, 长度上限得在。"""
    rows = [_edge_row(float(i) + 1.0, bond_code=f"{100000 + i}.SZ") for i in range(200)]
    kept = filter_batch_results_by_view(rows, "下修优势")
    assert len(kept) == _selection_cutoff(200, DEFAULT_DOWN_RESET_EDGE_PERCENTILE) == 30


def test_batch_result_columns_carry_the_new_signals():
    """新字段必须进 CSV/缓存列, 否则导出与跨运行缓存会静默丢掉它们。"""
    for key in ("relative_deviation", "cheapness_rank", "double_low",
                "quality_score", "down_reset_edge_rank"):
        assert key in BATCH_RESULT_COLUMNS


# ── 事件旗标 / 距下修线 ──
#
# 这些字段此前**全部算好了却一个都不显示**: 实测主池 280 只里 2 只有在途下修提议、
# 1 只已公告强赎、67 只有不强赎承诺、42 只暂停转股 —— 而"找交易机会"的页面看不到。

def _flag_row(**kw):
    row = dict(status="ok", bond_code="113000.SH", S0=12.0, K=13.5,
               theoretical_price=110.0, market_price=118.0, deviation=0.07,
               sigma=0.30, T=3.0, credit_rating="AA", outstanding_balance=4.25,
               valuation_date="2026-08-24")
    row.update(kw)
    return annotate_batch_result(row)


def test_event_flags_put_hard_deadlines_first():
    """列窄, 顺序直接决定用户看见什么: 错过就没得选的排最前。"""
    row = _flag_row(redemption_mode=True, call_redemption_date="2026-08-27",
                    conversion_suspension_status="暂停转股",
                    down_reset_scheduled_kind="proposed",
                    down_reset_scheduled_date="2026-09-05")
    assert row["event_flags"][0].startswith("强赎")
    assert row["event_flags"][1].startswith("下修提议")
    assert "暂停转股" in row["event_flags"]


def test_event_flags_stay_empty_for_a_quiet_bond():
    assert _flag_row()["event_flags"] == []


def test_putback_flag_requires_both_window_dates():
    """缺 end 不是"窗口还没结束", 是公告没解析出截止日 —— 此时 start 已退化成公告日。

    实测主池 82 条有 start 的回售记录里 29 条缺 end, 全部来自"回售的第N次提示性公告"
    (与解析成功的帝欧/长汽同一类公告), 窗口早已关闭。按 end is None 当"仍开启"会把
    30 只债长期错报成「回售中」—— 把**解析残缺**当成**当期状态**。
    """
    partial = _flag_row(putback_start_date="2025-12-11")          # 只有起始日
    assert not any(f.startswith("回售") for f in partial["event_flags"])

    live = _flag_row(putback_start_date="2026-08-20", putback_end_date="2026-08-26")
    assert any(f.startswith("回售中") for f in live["event_flags"])

    closed = _flag_row(putback_start_date="2025-08-14", putback_end_date="2025-08-20")
    assert not any(f.startswith("回售") for f in closed["event_flags"])

    soon = _flag_row(putback_start_date="2026-09-10", putback_end_date="2026-09-16")
    assert any(f.startswith("回售 09-10起") for f in soon["event_flags"])


def test_no_call_commitment_flag_needs_a_live_deadline():
    """不强赎承诺过期就不该再显示 —— 实测 call_status 有 67 只, 承诺未过期的只有 33 只。"""
    live = _flag_row(call_status="不强赎", call_no_redemption_until="2027-01-09")
    assert any(f.startswith("不强赎至") for f in live["event_flags"])
    expired = _flag_row(call_status="不强赎", call_no_redemption_until="2026-01-09")
    assert not any(f.startswith("不强赎") for f in expired["event_flags"])


def test_high_frequency_states_are_deliberately_not_flags():
    """在近半数债上都亮的旗标描述的是市场不是这只债 (与标签维度同源的教训)。

    「已触发下修线」实测 127/280 = 45%, 「下修冻结中」186/280 = 66% —— 前者改由
    「距下修线」数值列承载, 后者是模型入参, 经「下修优势」体现。
    """
    row = _flag_row(S0=8.0, K=13.5, down_reset_trigger_ratio=0.85,
                    down_reset_block_until="2027-01-09")
    assert row["event_flags"] == []
    assert row["down_reset_trigger_gap"] < 0          # 确实已在触发线下方


@pytest.mark.parametrize("s0, k, ratio, expected", [
    (11.475, 13.5, 0.85, 0.0),        # 恰在触发线上
    (8.0, 13.5, 0.85, -0.303),        # 已在线下
    (13.5, 13.5, 0.85, 0.176),        # 线上方
])
def test_down_reset_trigger_gap_measures_distance_to_the_line(s0, k, ratio, expected):
    row = {"S0": s0, "K": k, "down_reset_trigger_ratio": ratio}
    assert down_reset_trigger_gap(row) == pytest.approx(expected, abs=1e-3)


def test_down_reset_trigger_gap_falls_back_to_pct_and_degrades_safely():
    assert down_reset_trigger_gap(
        {"S0": 8.0, "K": 13.5, "down_reset_trigger_pct": 85.0}) == pytest.approx(-0.303, abs=1e-3)
    for bad in ({}, {"S0": 8.0}, {"S0": 8.0, "K": 0.0, "down_reset_trigger_ratio": 0.85},
                {"S0": 8.0, "K": 13.5, "down_reset_trigger_ratio": 0.0}):
        assert down_reset_trigger_gap(bad) is None


def test_event_flags_are_not_risk_tags():
    """两族必须分开: risk_tags 驱动策略排除集, 往里加东西就是默认选债行为变更。"""
    row = _flag_row(redemption_mode=True, call_redemption_date="2026-08-27")
    assert row["event_flags"]
    assert not (set(row["event_flags"]) & set(row["risk_tags"]))
    assert not (set(row["event_flags"]) & LEGACY_STRATEGY_EXCLUDE_TAGS)
