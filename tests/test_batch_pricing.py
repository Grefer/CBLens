import csv
import math
from datetime import date

from convertible_bond.batch_pricing import (
    BATCH_RESULT_COLUMNS,
    batch_pricing_exclusion_reason,
    list_upcoming_tradable_from_cache,
    list_batch_codes_from_cache,
    load_batch_results_cache,
    merge_upcoming_pricing_results,
    parse_bond_codes,
    save_batch_results_cache,
    split_batch_codes_from_cache,
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
        }

        def list_bonds(self):
            return list(self.data)

        def get(self, code):
            return self.data[code]

    kept, excluded = split_batch_codes_from_cache(FakeTermsCache())

    assert kept == ["123456.SZ", "113050.SH"]
    assert {code for code, _ in excluded} == {"124025.SZ", "110815.SH", "404004.NQ"}
    assert list_batch_codes_from_cache(FakeTermsCache(), include_nonstandard=True) == [
        "124025.SZ", "110815.SH", "404004.NQ", "123456.SZ", "113050.SH",
    ]
    assert batch_pricing_exclusion_reason("124025.SZ", {"bond_name": "富乐定转"}) is not None


def test_upcoming_tradable_cache_finds_private_bonds_in_window():
    class FakeTermsCache:
        data = {
            "124025.SZ": BondTerms(
                sec_name="富乐定转",
                underlying_code="301297.SZ",
                issue_date=date(2026, 3, 9),
                listing_date=date(2026, 3, 9),
                conversion_price=16.14,
                close=99.99,
            ),
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

    assert [row["bond_code"] for row in rows] == ["124025.SZ"]
    assert rows[0]["tradable_date"] == date(2026, 9, 9)
    assert rows[0]["days_to_trade"] == 5


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
