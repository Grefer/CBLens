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
    DEFAULT_UNDERVALUED_PERCENTILE,
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
    # 正股 ST **不再**硬剔除: 风险较大 ≠ 不能交易, ST 正股的转债照常挂牌撮合。
    # 改由「正股风险」标签承载, 见 test_underlying_st_is_a_tag_not_an_exclusion。
    assert batch_pricing_exclusion_reason(
        "113050.SH",
        BondTerms(sec_name="南银转债", underlying_name="*ST 测试"),
        on_date=check_date,
    ) is None
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
    # 评级默认**不再**硬剔除 (与余额同形): A 级债照常挂牌撮合, 准入层只管"买不买得到"。
    # 筛选口径下沉到 ScoreStrategyConfig.min_credit_rating (默认 AA-, 比原来的 A+ 还严),
    # 展示层由「低评级」标签承载。显式传阈值仍然生效 —— cb-screen-pool --min-rating 走这条。
    assert batch_pricing_exclusion_reason(
        "113050.SH",
        BondTerms(sec_name="南银转债", credit_rating="A"),
        on_date=check_date,
    ) is None
    assert batch_pricing_exclusion_reason(
        "113050.SH",
        BondTerms(sec_name="南银转债", credit_rating="A"),
        on_date=check_date,
        min_credit_rating="AA-",
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
    # 已发行未上市**进主池** (2026-08-31 起): 它们后续会挂牌, 正是最值得提前盯的一批。
    fresh = BondTerms(sec_name="强达转债", issue_date=date(2026, 8, 19),
                      listing_date=None, maturity_date=date(2032, 8, 19))
    assert batch_pricing_exclusion_reason(
        "123284.SZ", fresh, on_date=date(2026, 8, 20)) is None

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


def test_issued_but_unlisted_enters_the_pool_and_is_flagged_as_new():
    """已发行未上市**进主池** —— 它后续会挂牌, 值得提前算理论价、提前盯。

    此前归"扫新债"关注池而不进主池, 于是主表上根本看不见它们。放进来是安全的:
    行色的 ``new`` 档优先级最高 (价格类判据一律不适用), 策略层的有效性守卫要求有效
    市价所以进不了候选, 基准没有价格序列所以自然不进。唯一要额外挡的是估值基线的
    覆盖率分母 —— 见 ``market_valuation._usable_deviations``。

    ``is_tradable`` 对这一档天然是 False (``infer_cb_trading_metadata`` 的输出),
    所以准入层必须**同时**给「不可交易」那道闸让路, 否则只是换个原因串继续剔。
    """
    terms = BondTerms(
        sec_name="强达转债",
        issue_date=date(2026, 8, 19),
        listing_date=None,
        maturity_date=date(2032, 8, 19),
        credit_rating="AA-",
        outstanding_balance=5.5,
        is_tradable=False,               # ← 派生字段, 不该把它挡在池外
        trading_status="pending",
    )
    assert batch_pricing_exclusion_reason(
        "123284.SZ", terms, on_date=date(2026, 8, 20)) is None
    assert batch_pricing.is_unlisted_new_bond(terms, date(2026, 8, 20)) is True

    listed = replace(terms, listing_date=date(2026, 8, 25),
                     is_tradable=True, trading_status="tradable")
    assert batch_pricing_exclusion_reason(
        "123284.SZ", listed, on_date=date(2026, 9, 1)) is None
    assert batch_pricing.is_unlisted_new_bond(listed, date(2026, 9, 1)) is False

    # 而**真的**不可交易 (无上市痕迹、非 pending) 仍然被剔
    dead = BondTerms(sec_name="某定转", issue_date=date(2020, 1, 1),
                     listing_date=date(2020, 2, 1), maturity_date=date(2030, 1, 1),
                     is_tradable=False, trading_status="private_unknown")
    assert batch_pricing_exclusion_reason(
        "123284.SZ", dead, on_date=date(2026, 8, 20)) == "不可交易"


def test_announced_future_listing_date_also_enters_the_pool():
    """**上市日已公告但还没到**的新债同样进主池 —— 这一档曾是唯一漏网的。

    准入层放行判据原先用 ``is_issued_pending_listing``, 而它的第一行是
    ``if listing_date is not None: return False`` —— 上市日只要非空就为假, **哪怕在未来**。
    净效果是: 上市日未知的新债放进来了, 已经公告了挂牌日的那只 (最近、最该提前盯的那只)
    照旧被剔。实测 2026-08-31 库里三只在途新债主表上只出得来两只, 震裕转02 定于 09-02
    挂牌, 剔除原因「不可交易」。

    **两道闸必须同时让路**: 只修上面那道, 原因串会从「不可交易」变成「N 日后可交易」,
    主池数一个不变 —— 改动等于没做。
    """
    soon = BondTerms(
        sec_name="震裕转02",
        issue_date=date(2026, 8, 17),
        listing_date=date(2026, 9, 2),           # ← 已公告, 但还没到
        maturity_date=date(2032, 8, 17),
        credit_rating="AA-",
        outstanding_balance=18.8,
        is_tradable=False,                       # ← 派生字段
        trading_status="pending",
    )
    assert batch_pricing_exclusion_reason(
        "123282.SZ", soon, on_date=date(2026, 8, 31)) is None
    assert batch_pricing.is_unlisted_new_bond(soon, date(2026, 8, 31)) is True

    # 挂牌当天起就是普通在市债, 不再是新债
    assert batch_pricing_exclusion_reason(
        "123282.SZ", soon, on_date=date(2026, 9, 2)) is None
    assert batch_pricing.is_unlisted_new_bond(soon, date(2026, 9, 2)) is False


def test_future_tradable_date_does_not_let_private_bonds_into_the_pool():
    """放行的是**普通公募**新债 —— 定向债不因为"可交易日在未来"跟着进来。

    ``is_unlisted_new_bond`` 只看日期与派生状态, 不问代码段/命名, 所以准入层必须自己
    与 ``standard_public`` 相与。实测库里恰好有两只未来可交易日的定向债 (富乐定转
    2026-09-09 / 莱特定转), 它们与在途新债长得一样但不该进主池。
    """
    private = BondTerms(
        sec_name="富乐定转",
        issue_date=date(2026, 3, 9),
        listing_date=None,
        tradable_date=date(2026, 9, 9),
        maturity_date=date(2032, 3, 9),
        is_tradable=False,
        trading_status="private_pending",
    )
    assert batch_pricing_exclusion_reason(
        "124025.SZ", private, on_date=date(2026, 8, 31)) is not None


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
    # 「模型低估」(绝对阈值 dev<−8%) 已退役 —— 便宜度只留横截面那个。
    # ``undervaluation_rate`` 字段**保留**: 它是原始量, 不是标签。
    assert "模型低估" not in row["risk_tags"]
    assert "深度低估待核" in row["risk_tags"]
    assert row["undervaluation_rate"] == pytest.approx(0.294)
    assert "转股折价" in row["risk_tags"]
    assert "高HV" in row["risk_tags"]
    assert row["confidence"] == "中"
    assert row["model_signal_status"] == "不适合作为买入信号"
    assert row["sensitivity_status"] == "波动率敏感"
    # 「高HV」属模型适用性维度 —— 模型在这只债上不可靠, 但那是**永久属性**, 不是
    # "去做点什么就能解决"的事, 所以单列「模型存疑」而不是塞进需复核。
    # 需复核只留数据质量 + 可交易性 (这一行现在不能用, 得先去拉数据/确认能不能交易)。
    assert row["review_bucket"] == "模型存疑"
    assert row["review_notes"]


@pytest.mark.parametrize("sigma, confidence", [
    # 门是严格 `>`: σ 恰为 0.80 归「较高HV」(扣 6), 还够不着高 HV 那条线。
    (0.80, "高"),
    # 高 HV 惩罚 = min(28, 10 + (σ−0.80)·35), 而 高/中 的分界是扣满 22 分。
    # 解出来 σ* = 0.80 + 22/35 = 1.142857 —— 把它夹在中间, 斜率与截距各自可观测。
    (1.14, "高"),
    (1.15, "中"),
    # 上限**存在**的唯一观测点: 没有它 σ=2.0 要扣 52 分 → 落到「低」。
    # 但上限的**具体数值**在这个接口上观测不到 —— 输出只有 高/中/低 三档,
    # 而 100−28=72 与 100−40=60 同属「中」。要钉住 28 就得再叠一个别处的扣分项
    # 把分数推到 55 那条线附近, 那会让这条守护跟着那个无关的惩罚一起红。
    (2.00, "中"),
])
def test_high_hv_confidence_penalty_slope_offset_and_cap(sigma, confidence):
    """钉住高 HV 惩罚的斜率/截距/上限, 而不是"落在某个集合里".

    `confidence` 只导出 高/中/低 三档, 所以要让这条公式可观测, 行本身必须**干净**
    (无其他扣分项) —— 否则别处的惩罚会把分数推过界, 测的就不是这条公式了。
    ``σ=0.55`` 那一档 (下面的断言) 就是这个前提的看门人。
    """
    row = annotate_batch_result({
        "bond_code": "128000.SZ", "status": "ok",
        "S0": 10.0, "K": 10.0, "sigma": sigma,
        "theoretical_price": 110.0, "market_price": 112.0, "deviation": 0.0182,
        "credit_rating": "AAA", "outstanding_balance": 20.0, "T": 3.0,
    })
    assert row["confidence"] == confidence
    assert ("高HV" in row["risk_tags"]) is (sigma > 0.80)


def test_high_hv_penalty_fixture_is_otherwise_unpenalised():
    """看门人: 上面那个 fixture 除高 HV 外不许有任何扣分项."""
    row = annotate_batch_result({
        "bond_code": "128000.SZ", "status": "ok",
        "S0": 10.0, "K": 10.0, "sigma": 0.55,
        "theoretical_price": 110.0, "market_price": 112.0, "deviation": 0.0182,
        "credit_rating": "AAA", "outstanding_balance": 20.0, "T": 3.0,
    })
    assert row["risk_tags"] == []
    assert row["confidence"] == "高"


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


def test_sort_batch_results_for_review_is_pure_deviation_now():
    """研究排序不再惩罚高风险行 —— 这是删除机会分的**代价**, 记在这里.

    原行为: 第一排序键是机会分降序, 而机会分对 高HV / 低评级 / 小余额 / 短久期
    逐项扣分, 于是下面的 NOISY (σ=1.45、A 级、余额 0.2 亿、剩余 0.3 年) 即便
    deviation 更负 (−0.30 vs −0.16) 也会被压到 CLEAN 后面。

    现在纯按 deviation 升序, **NOISY 排第一**。机会分整体删除 (实测 95% 的行低估项
    恒为 0, 它度量的是信用质量而非错定价), 它承载的这条风险惩罚一并消失。

    没有顺手补一个新的沉底规则: 那等于用一个未经检验的新机制替换一个刚被证伪的旧
    机制, 而 ``sort_batch_results_for_review`` 的顺序会喂给 `filter_batch_results_by_view`
    —— 属于默认选债行为, 要改得单独立项。

    影响面有限: ``sort_batch_results_for_view`` 对 **5 个视图里的 4 个**走重排分支,
    只有「需复核」(2026-09-03 实测 11 行) 直接沿用本函数的顺序; 而那个视图里的行
    本来就全是高风险行。
    """
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

    assert [r["bond_code"] for r in rows] == ["NOISY", "CLEAN"]
    assert rows[0]["deviation"] < rows[1]["deviation"]
    # 高风险标签照常打出来 —— 消失的只是"靠它们把行压到后面"这条排序惩罚
    assert {"高HV", "低评级", "短久期"} & set(rows[0]["risk_tags"])


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
    # 0.3~0.5 亿原本是「临近摘牌线」, 该档 2026-08-31 退役 —— 那一刻既无法定依据 (法定线是
    # 0.3) 也不对应任何策略阈值 (min_outstanding_balance=1.0), 且主池在每个可测日期上都是空的。
    # 落进这条带的债改打「小余额」, 阈值严格更宽, 不会漏标。
    (0.35, "小余额"),
    (0.80, "小余额"),
])
def test_balance_tags_follow_statutory_delisting_line(balance, tag):
    assert tag in _annotated(outstanding_balance=balance)["risk_tags"]
    assert "临近摘牌线" not in _annotated(outstanding_balance=balance)["risk_tags"]


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


def test_strategy_selection_does_not_read_the_display_tag_set_at_all():
    """标签是给全池标的做**标注**的; 策略层用自己的数值阈值筛, 不吃标签。

    这条守护的意图没变, 机制换了、而且更强: 它此前钉的是"策略默认排除集必须与
    ``HARD_REVIEW_TAGS`` 解耦"(因为曾写成 ``tuple(sorted(HARD_REVIEW_TAGS))``, 于是
    任何为改展示而增删标签的动作都自动变成默认选债行为变更)。现在策略层**根本不读标签**,
    那条耦合从源头上不存在了 —— 展示层怎么改标签都不可能影响选债。

    ``exclude_risk_tags`` 保留为兼容字段 (旧快照能原样回放), 默认空。
    """
    from convertible_bond.strategy_backtest import ScoreStrategyConfig

    cfg = ScoreStrategyConfig()
    assert cfg.exclude_risk_tags == (), "默认配置不该再靠标签筛"

    # 默认阈值**逐条等于**它取代的那 6 个真在工作的标签的判据
    assert cfg.max_model_premium == 0.45          # 模型溢价高
    assert cfg.max_relative_deviation == 0.20     # 模型高估离群
    assert cfg.min_years_to_maturity == 0.5       # 短久期
    assert cfg.max_sigma == 0.80                  # 高HV
    assert cfg.min_credit_rating == "AA-"         # 低评级
    assert cfg.min_outstanding_balance == 1.0     # 小余额 (同时覆盖余额那一族的四个刻度)
    assert cfg.exclude_underlying_st is True      # 正股风险
    assert cfg.exclude_underlying_limit_down is True  # 正股跌停

    # 冻结集本身**保留**: 旧快照里存着它, 回放要认得
    assert "低评级" in LEGACY_STRATEGY_EXCLUDE_TAGS
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


def test_batch_result_columns_carry_the_new_signals():
    """新字段必须进 CSV/缓存列, 否则导出与跨运行缓存会静默丢掉它们。"""
    for key in ("relative_deviation", "cheapness_rank", "double_low",
                "quality_score", "double_low_rank"):
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
    「正股/下修线」数值列承载, 后者是模型入参, 不单独展示。
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


# ── 横截面锚回传 + 小样本秩字段清空 (S3) ─────────────────────────────

def _anchor_row(code, deviation, double_low=None, median=None):
    row = {"bond_code": code, "status": "ok", "deviation": deviation,
           "theoretical_price": 110.0, "market_price": 108.0}
    if double_low is not None:
        row["double_low"] = double_low
    if median is not None:
        row["market_median_deviation"] = median
    return row


def _anchor_pool(n=40, base=0.20):
    """一个 >= _DEVIATION_MEDIAN_MIN_SAMPLE 的合成主池."""
    return [_anchor_row(f"1230{i:02d}.SZ", base + (i - n / 2) / 1000.0) for i in range(n)]


def test_cross_section_anchor_reads_row_level_median():
    """锚在**每一行**里, 不在 _meta 里.

    实测 batch_pricing_cache.json 的 _meta 键只有
    {saved_at, source, params, n_results, n_upcoming_results, summary} ——
    任何"从 _meta 读锚"的实现都会静默取不到值。
    """
    rows = [_anchor_row("A", 0.1), _anchor_row("B", 0.3, median=0.2085946726167419)]
    assert batch_pricing.cross_section_anchor_from(rows) == pytest.approx(0.2085946726167419)


def test_cross_section_anchor_falls_back_to_self_computed():
    pool = batch_pricing.annotate_batch_results(_anchor_pool())
    anchor = batch_pricing.cross_section_anchor_from(pool)
    assert anchor == pytest.approx(batch_pricing.median_deviation_of(pool))


def test_cross_section_anchor_none_when_sample_too_small():
    """样本不足又没有行内锚时返回 None —— 让调用方看见"没有锚", 而不是拿到一个假的."""
    assert batch_pricing.cross_section_anchor_from([_anchor_row("A", 0.1)]) is None
    assert batch_pricing.cross_section_anchor_from([]) is None


def test_off_pool_subset_relative_deviation_matches_full_pool():
    """拿主池锚标注的子集, 相对偏差必须与它在全池里的值逐只相等.

    不传锚时 6 行子集自算中位就是它们自己, 每只恰好偏移一个中位的量 ——
    而那是个看上去完全正常的数字。
    """
    pool = batch_pricing.annotate_batch_results(_anchor_pool())
    anchor = batch_pricing.cross_section_anchor_from(pool)
    by_code = {r["bond_code"]: r for r in pool}

    subset_src = [dict(r) for r in pool[:6]]
    for row in subset_src:                     # 去掉行内锚, 强制走传入的那个
        row.pop("market_median_deviation", None)
        row.pop("relative_deviation", None)

    anchored = batch_pricing.annotate_batch_results(
        subset_src, market_median_deviation=anchor, rank_scope=False)
    for row in anchored:
        assert row["relative_deviation"] == pytest.approx(
            by_code[row["bond_code"]]["relative_deviation"])

    # 对照: 不传锚就会整体偏移
    naive = batch_pricing.annotate_batch_results([dict(r) for r in subset_src])
    assert any(
        n["relative_deviation"] != pytest.approx(by_code[n["bond_code"]]["relative_deviation"])
        for n in naive)


def test_rank_scope_false_blanks_every_rank_field():
    """传锚修不了秩 —— 名次是在这一批内部排的, 必须显式清空."""
    rows = [_anchor_row("A", 0.1, double_low=120),
            _anchor_row("B", 0.3, double_low=140)]
    out = batch_pricing.annotate_batch_results(rows, market_median_deviation=0.2,
                                               rank_scope=False)
    for row in out:
        for key in batch_pricing._CROSS_SECTIONAL_RANK_FIELDS:
            assert row[key] is None, f"{key} 应为 None, 实得 {row[key]!r}"


def test_rank_scope_false_would_otherwise_fabricate_top_percentile():
    """反证: 同一行在全池里排在后段, 单独拿子集算会变成"最便宜的 0%"."""
    pool = batch_pricing.annotate_batch_results(_anchor_pool())
    target = max(pool, key=lambda r: r.get("cheapness_percentile") or 0.0)
    assert target["cheapness_percentile"] > 0.5          # 全池里它并不便宜

    solo = batch_pricing.annotate_batch_results([dict(target)])
    assert solo[0]["cheapness_percentile"] == 0.0        # 子集里凭空变成最便宜

    safe = batch_pricing.annotate_batch_results([dict(target)], rank_scope=False)
    assert safe[0]["cheapness_percentile"] is None       # 加了闸就打「—」


def test_rank_scope_true_still_ranks():
    """默认路径不受影响 (主池标注仍要有名次, 否则视图长度上限失效)."""
    out = batch_pricing.annotate_batch_results(_anchor_pool())
    ranks = [r["cheapness_rank"] for r in out]
    assert sorted(ranks) == list(range(len(out)))
    assert all(r["cheapness_rank_total"] == len(out) for r in out)


def test_gui_never_calls_annotate_batch_results_without_an_anchor():
    """守护: GUI 侧任何小批量标注都要走 _annotate_off_pool.

    实测 2026-08 之前全仓库**没有一个调用点**传过 market_median_deviation,
    于是 _batch_upcoming_results 那一档 (新债 / 只在关注池里的债) 的横截面字段
    是在 <=6 行样本上算的 —— 而且它没有自愈路径: _batch_all_results 每轮都在
    全池上重标注能修回来, upcoming 标注一次之后再没人碰。
    """
    import re
    from pathlib import Path
    import convertible_bond

    call_re = re.compile(r"\bannotate_batch_results\(")
    gui_root = Path(convertible_bond.__file__).parent / "gui"
    offenders = []
    for path in sorted(gui_root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for match in call_re.finditer(text):
            depth, i = 0, match.end() - 1
            while i < len(text):                     # 取这次调用的完整实参段
                if text[i] == "(":
                    depth += 1
                elif text[i] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            if "market_median_deviation" in text[match.end():i]:
                continue
            lineno = text.count("\n", 0, match.start()) + 1
            offenders.append(f"{path.name}:{lineno}")
    assert not offenders, (
        "GUI 侧这些 annotate_batch_results 调用没带锚, 请改用 _annotate_off_pool:\n  "
        + "\n  ".join(offenders))


def test_full_pool_view_sorts_by_listing_date_newest_first():
    """「全池」按上市日倒序排**全部**标的; 只有没有上市日的沉底。

    为什么换掉便宜度排序: 全池是分母不是筛子, 便宜度那一路已由「低估候选」承担 ——
    实测两者都按 ``relative_deviation`` 排时**前 43 行重合 43/43**, 等于把同一屏看两次。

    两处不显然的地方:

    · **不按定价成没成功分组** —— 定价失败的也是标的, 有上市日就照常参与排序。其余
      视图的 ``by()`` 把失败行沉底是因为那些视图排的是模型输出 (相对偏差/双低),
      失败行根本没有值; 上市日与定价成不成功无关。
    · **取值走 ``safe_date`` 不是 ``to_date``** —— 缓存里是 ISO 串、内存里是 ``date``,
      而 ``pandas.NaT`` 是 ``datetime`` 子类且为真值, ``to_date`` 会原样放行它
      (``safe_date`` 的 docstring 点名的正是 ``listing_date``)。排序键里抛出来就是整表
      渲染失败, 所以 ``None`` / ``NaN`` 都要能落到底而不是炸掉。
    """
    rows = [
        {"bond_code": "OLD.SZ", "status": "ok", "listing_date": date(2020, 9, 23)},
        {"bond_code": "NEW.SZ", "status": "ok", "listing_date": "2026-08-26"},
        {"bond_code": "NODATE.SZ", "status": "ok", "listing_date": None},
        {"bond_code": "NAN.SZ", "status": "ok", "listing_date": float("nan")},
        # 定价失败但上市日最新 → 照样排第一
        {"bond_code": "FAIL.SZ", "status": "failed", "listing_date": "2026-08-27"},
    ]
    order = [r["bond_code"] for r in sort_batch_results_for_view(rows, "综合机会")]
    assert order[:3] == ["FAIL.SZ", "NEW.SZ", "OLD.SZ"], order
    assert set(order[3:]) == {"NODATE.SZ", "NAN.SZ"}, order

    # 其余视图的排序键没被顺带改掉
    cheap = [{"bond_code": "A", "status": "ok", "relative_deviation": 0.10},
             {"bond_code": "B", "status": "ok", "relative_deviation": -0.20}]
    assert [r["bond_code"]
            for r in sort_batch_results_for_view(cheap, "低估候选")] == ["B", "A"]


def test_underlying_st_is_a_tag_not_an_exclusion():
    """正股 ST 是「风险较大」而不是「不能交易」。

    ST 正股的转债照常挂牌撮合 (实测 2026-08-31 的 4 只: 闻泰/三房/宏图/章鼓)。硬剔除把
    这层信息表达成"这只债不存在", 而准入层的契约是只剔真的买不到的。

    **用完整行 + 非 ST 孪生行对照**, 不用退化行。第一版的 fixture 只填了 5 个字段, 于是
    带着「数据缺口」「无HV」等一堆标签 —— ``model_signal_status`` 那条断言靠别的标签
    就成立了, 把 ST 接线整个删掉也杀不掉它, 而改动真正的用户可见效果 (ST 债进得了
    「低估候选」、不染行色) 一条断言都没有。孪生行让每一处差异都只能归因于 ST。
    """
    common = dict(
        bond_code="127093.SZ", status="ok", bond_name="章鼓转债",
        credit_rating="AA-", outstanding_balance=5.0,
        S0=9.0, K=10.0, sigma=0.35, T=3.0,
        market_price=105.0, theoretical_price=118.0, deviation=-0.11,
        conversion_value=90.0,
    )
    st = annotate_batch_result(
        {**common, "underlying_name": "ST章鼓", "underlying_status": "是"},
        market_median_deviation=0.21)
    twin = annotate_batch_result(
        {**common, "underlying_name": "章鼓股份", "underlying_status": "否"},
        market_median_deviation=0.21)

    # ① 进得了主池 —— 准入层不再剔 ST
    terms = BondTerms(sec_name="章鼓转债", underlying_name="ST章鼓", underlying_status="是")
    assert batch_pricing_exclusion_reason("127093.SZ", terms, on_date=date(2026, 8, 31)) is None

    # ② 两行**只差 ST 这一个标签** —— 对照才成立
    assert set(st["risk_tags"]) - set(twin["risk_tags"]) == {"正股风险"}
    assert set(twin["risk_tags"]) - set(st["risk_tags"]) == set()

    # ③ 不拦路: 标的风险维, 不进拦截集 → **行不染色**, 且照样进得了「低估候选」
    assert batch_pricing.RISK_TAG_DIMENSION["正股风险"] == batch_pricing.DIM_ISSUER
    assert "正股风险" not in batch_pricing.BLOCKING_RISK_TAGS
    assert not (set(st["risk_tags"]) & batch_pricing.BLOCKING_RISK_TAGS)
    assert view_exclusion_reason(st, "低估候选") is None, "ST 债被「低估候选」筛掉了"
    assert st["review_bucket"] == twin["review_bucket"] == "低估候选"

    # ④ 但它**确实**削弱了这一行: 置信度与模型信号都因 ST 而变 (孪生行是对照)
    assert (st["confidence"], twin["confidence"]) == ("中", "高")
    assert st["model_signal_status"] == "不适合作为买入信号"
    assert twin["model_signal_status"] != "不适合作为买入信号"

    # ⑤ 自动选债不碰它 —— 但走的是**策略层的显式开关**, 不再是标签
    from convertible_bond.strategy_backtest import (
        ScoreStrategyConfig, _candidate_filter_reason)
    assert _candidate_filter_reason(st, ScoreStrategyConfig()) == "正股 ST/退市风险"
    assert _candidate_filter_reason(
        st, ScoreStrategyConfig(exclude_underlying_st=False)) is None
    # 孪生行在同一份配置下进得去 —— 证明拦它的确实是 ST 而不是别的闸
    assert _candidate_filter_reason(twin, ScoreStrategyConfig()) is None


def test_last_trading_date_stays_a_hard_exclusion():
    """对照组: 「已过最后交易日」是真的买不到, 必须留在硬剔除里。"""
    assert batch_pricing_exclusion_reason(
        "127033.SZ",
        BondTerms(sec_name="中装转2", last_trading_date=date(2026, 8, 1)),
        on_date=date(2026, 8, 31),
    ) == "已过最后交易日"


def test_limit_down_threshold_knows_main_board_st_is_five_percent():
    """主板 ST/*ST 的日涨跌幅限制是 **±5%**, 不是 10%.

    此前只有两档 (创业板/科创板 −19.5, 其余 −9.5), 于是主板 ST 股跌停当天
    ``underlying_pct_change`` 只有 −5.0, 判据 ``pct <= -9.5`` **恒为假** ——
    「正股跌停」对这一整类结构性不亮。

    它此前不出事是因为这条路是死的: ST 债在准入层就被剔了, 根本走不到标注。
    2026-08-31 把 ST 降级成标签之后它第一次真正生效, 而策略层的
    ``exclude_underlying_limit_down`` 也直接读它 —— 不修的话, ST 正股跌停当天,
    S0 钉在跌停板上算出的理论价照常产出、偏差照常偏负、行无色、直接进「低估候选」。

    **创业板/科创板不分叉**: 那两个板的 ±20% 是板块级规则, ST 不改变它。
    """
    st_row = {"underlying_name": "*ST闻泰", "underlying_status": "是",
              "stock_code": "600745.SH"}
    plain_row = {"underlying_name": "闻泰科技", "underlying_status": "否",
                 "stock_code": "600745.SH"}

    # 三档阈值
    assert batch_pricing._underlying_limit_down_threshold("600745.SH", is_st=True) == -4.5
    assert batch_pricing._underlying_limit_down_threshold("600745.SH", is_st=False) == -9.5
    assert batch_pricing._underlying_limit_down_threshold("300123.SZ", is_st=True) == -19.5
    assert batch_pricing._underlying_limit_down_threshold("688001.SH", is_st=True) == -19.5

    # 主板 ST 真实跌停 −5.0% 必须识别得出来 (这正是修复前漏掉的那一档)
    assert batch_pricing._underlying_at_limit_down({**st_row, "underlying_pct_change": -5.0},
                                                   "600745.SH") is True
    assert batch_pricing._underlying_at_limit_down({**st_row, "underlying_pct_change": -4.0},
                                                   "600745.SH") is False
    # 同一只股票不带 ST 时 −5% 只是普通下跌
    assert batch_pricing._underlying_at_limit_down({**plain_row, "underlying_pct_change": -5.0},
                                                   "600745.SH") is False
    assert batch_pricing._underlying_at_limit_down({**plain_row, "underlying_pct_change": -10.0},
                                                   "600745.SH") is True

    # 创业板 ST 仍走 20% 档 —— 板块规则压过 ST
    cyb_st = {"underlying_name": "ST某某", "underlying_status": "是",
              "stock_code": "300123.SZ"}
    assert batch_pricing._underlying_at_limit_down({**cyb_st, "underlying_pct_change": -5.0},
                                                   "300123.SZ") is False
    assert batch_pricing._underlying_at_limit_down({**cyb_st, "underlying_pct_change": -20.0},
                                                   "300123.SZ") is True

    # ST 判定与「正股风险」标签共用同一个判据 —— 同一行不许一边说 ST 一边按非 ST 判跌停
    assert batch_pricing._underlying_has_st_risk(st_row) is True
    assert batch_pricing._underlying_has_st_risk(plain_row) is False


def test_retired_tags_have_no_append_site_but_stay_registered():
    """已退役标签的契约: **没有 append 现场, 但必须仍在维度表里**.

    留在 ``RISK_TAG_DIMENSION`` 不是懒得删 —— 消费者是按维度派生的
    (``TRADABILITY_RISK_TAGS`` / ``DATA_QUALITY_RISK_TAGS`` / ``BLOCKING_RISK_TAGS``
    全走 ``tags_in()``)。字符串一旦不在册, 旧缓存里带着它的行就查不到维度, 行色与视图
    归属会**静默**改变 —— 而 ``data/batch_pricing_cache.json`` 与
    ``data/strategy_backtest_snapshots/`` 里存的正是这些字符串。

    这是既有做法 (偏差异常 / 极小余额 / 余额异常 一直如此), 这次只是给了它一个明确清单。
    """
    import inspect
    import re

    appended = set(re.findall(r'risk_tags\.append\("([^"]+)"\)',
                              inspect.getsource(batch_pricing)))

    for tag in batch_pricing.RETIRED_RISK_TAGS:
        assert tag in RISK_TAG_DIMENSION, f"{tag} 退役了但从维度表里删掉了 —— 旧缓存会静默改行为"
        assert tag not in appended, f"{tag} 登记为已退役, 但代码里还有 append 现场"

    # 反向: 没登记退役的标签必须都有 append 现场 (否则就是漏登记的死标签)
    orphans = set(RISK_TAG_DIMENSION) - appended - set(batch_pricing.RETIRED_RISK_TAGS)
    assert not orphans, f"这些标签没有 append 现场却没登记进 RETIRED_RISK_TAGS: {sorted(orphans)}"


def test_missing_balance_and_rating_no_longer_tagged():
    """余额/评级缺失不再打标签 —— 它们检测的是从不缺失的字段。

    实测: 主池 0/311 缺失, 全库 1059 只里余额缺 2 (含日升转债那只撤销发行的幽灵债,
    已被「无发行与上市日期」剔除)、评级缺 1, 7 个历史快照一致且全部在池外。
    与「无市价」的不对称是这次保留后者的理由: 市价来自每日 HTTP 端点 (本月真的挂过),
    而余额与评级来自本地条款库 —— cb_data.json 读不出来时是全字段一起失败。

    顺带修掉一个错误的连带效果: 两者都是 DIM_DATA 因此进 ``BLOCKING_RISK_TAGS``,
    于是"评级取不到"会把整行染灰并踢出「低估候选」, 而「评级」列只会渲染一个「—」。
    """
    no_balance = _annotated(outstanding_balance=None)
    no_rating = _annotated(credit_rating=None)

    assert "无余额" not in no_balance["risk_tags"]
    assert "无评级" not in no_rating["risk_tags"]
    # 也不该被拦截集吃掉
    assert not (set(no_balance["risk_tags"]) & batch_pricing.BLOCKING_RISK_TAGS)
    assert not (set(no_rating["risk_tags"]) & batch_pricing.BLOCKING_RISK_TAGS)
    # 而「无市价」保留 —— 它的数据源确实会挂
    assert "无市价" in _annotated(market_price=None)["risk_tags"]


def test_terms_as_of_anchors_the_terms_sync_bucket_not_the_global_stamp(tmp_path):
    """条款锚必须取**全量条款同步**那一桶, 不是全局 ``fetched_at``。

    实测盘上 5 个来源桶 (Wind:admission_status / Wind / cb_events / akshare:ratings /
    akshare:new_issues), 只有 ``Wind`` 真抓条款 —— 另外四个各刷几个状态字段, 却一样把
    **全局** 戳推到今天。用全局值当锚等于宣称"今天之前的条款变更都已含在快照里",
    于是那段条款 patch 被 ``after=`` 整段裁掉。

    这条同时钉住**三个出口共用一份实现**: ``cache.py`` 的两个 ``terms_as_of`` 与
    ``batch_pricing._terms_cache_as_of`` 曾各写一份逐字重复的代码, 而只有前两份被修好
    —— 第三份留在全局戳上, 静默给主池的条款投影用错锚 (实测 3 只债多裁 5 天; 每跑一次
    状态刷新 / 评级同步 / 事件同步, 全局戳就再往前推一次, 影响只会变大)。
    """
    import json
    from datetime import date

    from convertible_bond.batch_pricing import _terms_cache_as_of
    from convertible_bond.cache import (
        TERMS_SYNC_SOURCE,
        CachedBondDataProvider,
        CachingDataProvider,
        TermsBundle,
        terms_fetched_at,
    )

    code = "128009.SZ"
    path = tmp_path / "cb_data.json"
    path.write_text(json.dumps({code: {
        "sec_name": "歌尔转债", "conversion_price": 10.0,
        "_meta": {
            # 状态刷新把全局戳推到了今天, 但条款是 5 天前抓的
            "fetched_at": "2026-08-30T09:00:00",
            "source": "Wind:admission_status",
            "fetched_at_by_source": {
                TERMS_SYNC_SOURCE: "2026-08-25T09:00:00",
                "Wind:admission_status": "2026-08-30T09:00:00",
            },
        },
    }}, ensure_ascii=False), encoding="utf-8")
    bundle = TermsBundle(path)

    assert _terms_cache_as_of(bundle, code) == date(2026, 8, 25), (
        "batch_pricing 读了全局戳 —— 08-25~08-30 的条款 patch 会被多裁掉")

    inner = type("I", (), {"name": TERMS_SYNC_SOURCE})()
    assert CachingDataProvider.terms_as_of(
        type("P", (), {"cache": bundle, "inner": inner})(), code, date(2026, 8, 30),
    ) == date(2026, 8, 25)
    assert CachedBondDataProvider.terms_as_of(
        type("P", (), {"cache": bundle, "static_source": inner})(),
        code, date(2026, 8, 30),
    ) == date(2026, 8, 25)

    # **所有**取条款锚的地方都要走这一个函数。曾经有三份逐字重复的实现、只修好两份;
    # 后来在 ``cli/data_doctor.check_pool_terms_projection`` 里又发现第四份 (裸
    # ``bundle.fetched_at(code)``)。守护测试扫源码: 不许再出现不带 source 的裸调用。
    import ast
    import inspect

    from convertible_bond.cli import data_doctor

    src = inspect.getsource(data_doctor)
    bare = [
        node.lineno for node in ast.walk(ast.parse(src))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute) and node.func.attr == "fetched_at"
        and not any(kw.arg == "source" for kw in node.keywords)
    ]
    assert not bare, f"data_doctor 里还有不带 source 的 fetched_at 调用, 第 {bare} 行"

    # 缺桶时必须回落全局戳, 不能回 None —— None 在投影层表示"不裁剪", 会把整条 patch 链
    # 从发行日回放上来, 拿陈旧/解析错的值盖掉正确的 cb_data (实测海顺转债 K 11.63 会被
    # 盖成 17.74)。这不是假想的边界: 实测全库 739/1059 只还没有条款桶。
    assert terms_fetched_at(bundle, code, source="从来没有过的桶") == date(2026, 8, 30)
