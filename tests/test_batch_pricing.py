import csv
import math
from dataclasses import replace
from datetime import date

import pytest

from convertible_bond import batch_pricing
from convertible_bond.batch_pricing import (
    AdmissionFilterConfig,
    BATCH_RESULT_COLUMNS,
    HARD_REVIEW_TAGS,
    annotate_batch_result,
    filter_batch_results_by_view,
    sort_batch_results_for_review,
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
    assert row["review_bucket"] == "需复核"
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
