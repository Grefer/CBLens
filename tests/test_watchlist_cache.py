"""关注池行情层落盘 (watchlist_cache) 测试.

每条用例都对应一个具体的静默失效, 不是为覆盖率而写:
- round-trip 不还原 date → ``as_of < valuation_date`` 在两条路径上类型不同, TypeError
- 同日重跑把窄快照写成并集 → 明天的"涨跌"拿今早的旧行当基准
- 写今天顺手改了昨天 → 变化列的基准被自己冲掉
- stale 判据漏掉"市价非有限" → 已上市但缺市价的行当天永远不再重试
"""
from __future__ import annotations

import json
import math
from datetime import date

import pytest

from convertible_bond import watchlist_cache as wc


@pytest.fixture()
def paths(tmp_path):
    return {"cache_path": tmp_path / "hot.json", "daily_dir": tmp_path / "daily"}


def _row(code="123281.SZ", **over):
    row = {
        "bond_code": code,
        "bond_name": "中仑转债",
        "stock_code": "301565.SZ",
        "status": "ok",
        "theoretical_price": 110.77,
        "market_price": 108.5,
        "deviation": -0.0205,
        "K": 12.7,
        "risk_tags": ["评级偏低"],
        "event_flags": ["下修提议中"],
        "relative_deviation": -0.2589,
        "cheapness_rank": 0,
        "cheapness_percentile": 0.0,
        "credit_rating": "AA-",
        "maturity_date": date(2031, 3, 1),
        "market_price_as_of": date(2026, 8, 26),
        "market_price_source": "history",
        # 白名单外的键, 必须被裁掉
        "delta": 0.61,
        "gamma": 0.004,
        "grid_M": 500,
    }
    row.update(over)
    return row


# ── 白名单与裁剪 ────────────────────────────────────────────────

def test_cache_row_drops_fields_outside_whitelist(paths):
    out = wc.to_cache_row(_row(), origin="watchlist_worker",
                          valuation_date=date(2026, 8, 26), priced_at="x")
    assert "delta" not in out and "gamma" not in out and "grid_M" not in out
    assert out["origin"] == "watchlist_worker"
    assert out["valuation_date"] == date(2026, 8, 26)


def test_narrow_row_is_a_subset_of_cache_row():
    """窄快照字段必须全在热缓存白名单里, 否则日志会写出热缓存拿不到的键."""
    assert set(wc.NARROW_FIELDS) <= set(wc.CACHE_FIELDS)


def test_derived_tradability_fields_are_not_cached():
    """is_tradable / trading_status 是派生字段, 缓存它们就是自我确认."""
    assert "is_tradable" not in wc.CACHE_FIELDS
    assert "trading_status" not in wc.CACHE_FIELDS


def test_unknown_origin_is_rejected(paths):
    with pytest.raises(ValueError):
        wc.save_watchlist_pricing([_row()], valuation_date=date(2026, 8, 26),
                                  origin="whatever", **paths)


# ── round-trip ─────────────────────────────────────────────────

def test_roundtrip_restores_dates_and_nan(paths):
    row = _row(deviation=float("nan"), theoretical_price=float("nan"))
    wc.save_watchlist_pricing([row], valuation_date=date(2026, 8, 26),
                              source="Wind", **paths)
    got = wc.load_watchlist_pricing(paths["cache_path"])["rows"]["123281.SZ"]

    # NaN 而不是 None —— 与内存路径一致, 否则 `x is not None` 在两条路径上不同答案
    assert math.isnan(got["deviation"])
    assert math.isnan(got["theoretical_price"])
    # date 而不是 str —— 否则 as_of < valuation_date 会 TypeError
    assert got["valuation_date"] == date(2026, 8, 26)
    assert got["market_price_as_of"] == date(2026, 8, 26)
    assert got["maturity_date"] == date(2031, 3, 1)
    assert got["market_price_as_of"] < date(2026, 8, 27)   # 比较不炸


def test_roundtrip_keeps_list_fields(paths):
    wc.save_watchlist_pricing([_row()], valuation_date=date(2026, 8, 26), **paths)
    got = wc.load_watchlist_pricing(paths["cache_path"])["rows"]["123281.SZ"]
    assert got["risk_tags"] == ["评级偏低"]
    assert got["event_flags"] == ["下修提议中"]


def test_load_missing_or_corrupt_cache_returns_empty(paths, tmp_path):
    assert wc.load_watchlist_pricing(tmp_path / "nope.json")["rows"] == {}
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert wc.load_watchlist_pricing(broken)["rows"] == {}


# ── 幂等与不越界 ───────────────────────────────────────────────

def test_same_day_rerun_replaces_daily_snapshot_not_appends(paths):
    day = date(2026, 8, 26)
    wc.save_watchlist_pricing([_row(market_price=100.0)], valuation_date=day, **paths)
    wc.save_watchlist_pricing([_row(market_price=101.0)], valuation_date=day, **paths)

    files = sorted(paths["daily_dir"].glob("*.json"))
    assert len(files) == 1                      # 一天一份, 不是两份
    snap = wc.load_daily_snapshot(day, daily_dir=paths["daily_dir"])
    assert len(snap["rows"]) == 1               # 不是并集
    assert snap["rows"]["123281.SZ"]["market_price"] == 101.0   # 是第二次那份


def test_writing_today_leaves_yesterday_untouched(paths):
    y, t = date(2026, 8, 25), date(2026, 8, 26)
    wc.save_watchlist_pricing([_row(market_price=100.0)], valuation_date=y, **paths)
    y_file = wc.daily_snapshot_path(y, daily_dir=paths["daily_dir"])
    before_bytes = y_file.read_bytes()
    before_mtime = y_file.stat().st_mtime_ns

    wc.save_watchlist_pricing([_row(market_price=120.0)], valuation_date=t, **paths)

    assert y_file.read_bytes() == before_bytes
    assert y_file.stat().st_mtime_ns == before_mtime


def test_hot_cache_upserts_per_code(paths):
    day = date(2026, 8, 26)
    wc.save_watchlist_pricing([_row("111026.SH", market_price=90.0)],
                              valuation_date=day, **paths)
    wc.save_watchlist_pricing([_row("123281.SZ", market_price=95.0)],
                              valuation_date=day, **paths)
    rows = wc.load_watchlist_pricing(paths["cache_path"])["rows"]
    assert set(rows) == {"111026.SH", "123281.SZ"}      # 第二次没冲掉第一次
    assert rows["111026.SH"]["market_price"] == 90.0


def test_daily_snapshot_holds_only_this_rounds_rows(paths):
    """窄快照只写本轮算出来的行, 不把热缓存里隔夜的旧行也塞进今天."""
    wc.save_watchlist_pricing([_row("111026.SH")], valuation_date=date(2026, 8, 25), **paths)
    wc.save_watchlist_pricing([_row("123281.SZ")], valuation_date=date(2026, 8, 26), **paths)
    snap = wc.load_daily_snapshot(date(2026, 8, 26), daily_dir=paths["daily_dir"])
    assert set(snap["rows"]) == {"123281.SZ"}


def test_merge_false_replaces_hot_cache(paths):
    day = date(2026, 8, 26)
    wc.save_watchlist_pricing([_row("111026.SH")], valuation_date=day, **paths)
    wc.save_watchlist_pricing([_row("123281.SZ")], valuation_date=day, merge=False, **paths)
    assert set(wc.load_watchlist_pricing(paths["cache_path"])["rows"]) == {"123281.SZ"}


def test_atomic_write_leaves_no_tmp(paths):
    wc.save_watchlist_pricing([_row()], valuation_date=date(2026, 8, 26), **paths)
    assert not list(paths["cache_path"].parent.glob("*.tmp"))
    assert not list(paths["daily_dir"].glob("*.tmp"))


# ── 上一交易日 ──────────────────────────────────────────────────

def test_latest_daily_before_skips_weekend_gap(paths):
    for d in (date(2026, 8, 21), date(2026, 8, 24), date(2026, 8, 26)):
        wc.save_watchlist_pricing([_row()], valuation_date=d, **paths)
    prev = wc.latest_daily_before(date(2026, 8, 26), daily_dir=paths["daily_dir"])
    assert prev["valuation_date"] == date(2026, 8, 24)

    # 周一往前找, 落在上周五那份 —— 「上一交易日」由盘上有没有文件定义
    prev = wc.latest_daily_before(date(2026, 8, 24), daily_dir=paths["daily_dir"])
    assert prev["valuation_date"] == date(2026, 8, 21)


def test_latest_daily_before_is_strict_and_none_when_empty(paths):
    wc.save_watchlist_pricing([_row()], valuation_date=date(2026, 8, 26), **paths)
    # 严格早于: 今天那份不算自己的前一天
    assert wc.latest_daily_before(date(2026, 8, 26), daily_dir=paths["daily_dir"]) is None
    assert wc.latest_daily_before(date(2020, 1, 1), daily_dir=paths["daily_dir"]) is None


def test_list_daily_dates_ignores_junk_files(paths):
    wc.save_watchlist_pricing([_row()], valuation_date=date(2026, 8, 26), **paths)
    (paths["daily_dir"] / "notes.json").write_text("{}", encoding="utf-8")
    assert wc.list_daily_dates(daily_dir=paths["daily_dir"]) == [date(2026, 8, 26)]


# ── 陈旧判据 ────────────────────────────────────────────────────

TODAY = date(2026, 8, 26)


@pytest.mark.parametrize("over,stale,why", [
    ({}, False, "今天算的、有市价 → 不重算"),
    ({"valuation_date": date(2026, 8, 25)}, True, "昨天的价"),
    ({"status": "failed"}, True, "上一轮失败"),
    ({"market_price": None}, True, "已上市但缺市价 (118076.SH 那个 case)"),
    ({"market_price": float("nan")}, True, "市价 NaN 同理"),
    ({"valuation_date": None}, True, "没有估值日 = 不知道是哪天的价"),
])
def test_row_is_stale(over, stale, why):
    row = {"status": "ok", "valuation_date": TODAY, "market_price": 108.5}
    row.update(over)
    assert wc.row_is_stale(row, TODAY) is stale, why


def test_row_is_stale_for_missing_row():
    assert wc.row_is_stale(None, TODAY) is True


def test_stale_codes_preserves_order_and_dedupes(paths):
    cache = {"rows": {
        "A": {"status": "ok", "valuation_date": TODAY, "market_price": 1.0},
        "B": {"status": "ok", "valuation_date": date(2026, 8, 25), "market_price": 1.0},
    }}
    assert wc.stale_codes(cache, ["A", "B", "C", "B"], TODAY) == ["B", "C"]


def test_stale_codes_skips_seeded_unless_asked():
    cache = {"rows": {"A": {"status": "ok", "valuation_date": date(2020, 1, 1),
                            "market_price": 1.0, "origin": "seeded"}}}
    assert wc.stale_codes(cache, ["A"], TODAY) == []
    assert wc.stale_codes(cache, ["A"], TODAY, include_seeded=True) == ["A"]


# ── 横截面锚有效期 ──────────────────────────────────────────────

def _meta(anchor_day, median=0.2086):
    return {"cross_section": {"market_median_deviation": median,
                              "from_valuation_date": anchor_day}}


def test_anchor_fresh_within_five_trading_days():
    # 2026-08-26 是周三; 往前 5 个工作日是 08-19 (周三)
    assert wc.anchor_is_stale(_meta(date(2026, 8, 19)), TODAY) is False
    assert wc.anchor_is_stale(_meta(TODAY), TODAY) is False


def test_anchor_stale_beyond_five_trading_days():
    assert wc.anchor_is_stale(_meta(date(2026, 8, 18)), TODAY) is True
    assert wc.anchor_is_stale(_meta(date(2026, 7, 1)), TODAY) is True


def test_anchor_missing_counts_as_stale():
    assert wc.anchor_is_stale(None, TODAY) is True
    assert wc.anchor_is_stale({}, TODAY) is True
    assert wc.anchor_is_stale(_meta(date(2026, 8, 25), median=None), TODAY) is True
    assert wc.anchor_is_stale({"cross_section": {"market_median_deviation": 0.2}}, TODAY) is True


def test_anchor_weekend_does_not_burn_the_budget():
    """周末不算交易日 —— 否则跨一个周末就白扔掉 2 天额度."""
    friday, next_friday = date(2026, 8, 21), date(2026, 8, 28)
    assert wc._trading_days_between(friday, next_friday) == 5
    assert wc.anchor_is_stale(_meta(friday), next_friday) is False


# ── 落盘内容自检 ────────────────────────────────────────────────

def test_meta_carries_two_distinct_stamps(paths):
    wc.save_watchlist_pricing([_row()], valuation_date=date(2026, 8, 26),
                              source="Wind", **paths)
    meta = json.loads(paths["cache_path"].read_text(encoding="utf-8"))["_meta"]
    assert meta["valuation_date"] == "2026-08-26"          # 市场口径, 只有日期
    assert "T" in meta["saved_at"]                          # 本机挂钟, 带时分秒
    assert meta["schema"] == wc.CACHE_SCHEMA
    assert meta["source"] == "Wind"


def test_cross_section_is_none_when_not_supplied(paths):
    wc.save_watchlist_pricing([_row()], valuation_date=date(2026, 8, 26), **paths)
    meta = wc.load_watchlist_pricing(paths["cache_path"])["meta"]
    assert meta["cross_section"] is None


def test_cross_section_is_recorded_when_supplied(paths):
    anchor = {"market_median_deviation": 0.2086, "from": "batch_pricing_cache.rows",
              "from_valuation_date": "2026-08-26", "n": 284}
    wc.save_watchlist_pricing([_row()], valuation_date=date(2026, 8, 26),
                              cross_section=anchor, **paths)
    meta = wc.load_watchlist_pricing(paths["cache_path"])["meta"]
    assert meta["cross_section"]["market_median_deviation"] == pytest.approx(0.2086)
    assert meta["cross_section"]["n"] == 284
