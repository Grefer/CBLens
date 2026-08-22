"""market_valuation 转债大类估值/择时指标单测。"""
import json
import math

import pytest

from convertible_bond.market_valuation import (
    CALIBER_CHANGES,
    CALIBER_V1,
    CALIBER_V2,
    CURRENT_CALIBER,
    ValuationSnapshot,
    caliber_note,
    append_history,
    classify,
    compute_snapshot,
    load_history,
    percentile_rank,
    save_history,
    valuation_banner,
)


def _rows(devs, status="ok", vd="2026-05-26"):
    return [{"bond_code": f"c{i}", "deviation": d, "status": status,
             "valuation_date": vd} for i, d in enumerate(devs)]


# ---------------- compute_snapshot ----------------

def test_compute_snapshot_basic_stats():
    snap = compute_snapshot(_rows([-0.1, 0.0, 0.1, 0.2, 0.3]))
    assert snap.n == 5
    assert snap.median_deviation == pytest.approx(0.1)
    assert snap.mean_deviation == pytest.approx(0.1)
    assert snap.pct_overvalued == pytest.approx(3 / 5)   # 0.1,0.2,0.3 > 0
    assert snap.date == "2026-05-26"


def test_compute_snapshot_skips_non_ok_and_nan():
    rows = _rows([0.1, 0.2])
    rows.append({"bond_code": "bad", "deviation": 9.9, "status": "error",
                 "valuation_date": "2026-05-26"})
    rows.append({"bond_code": "nan", "deviation": float("nan"), "status": "ok",
                 "valuation_date": "2026-05-26"})
    snap = compute_snapshot(rows)
    assert snap.n == 2


def test_compute_snapshot_require_ok_false():
    rows = [{"bond_code": "a", "deviation": 0.1, "valuation_date": "2024-09-30"}]
    snap = compute_snapshot(rows, require_ok=False)
    assert snap.n == 1
    assert snap.date == "2024-09-30"


def test_compute_snapshot_explicit_date_overrides():
    snap = compute_snapshot(_rows([0.1, 0.2]), snapshot_date="2025-01-01")
    assert snap.date == "2025-01-01"


def test_compute_snapshot_empty_raises():
    with pytest.raises(ValueError):
        compute_snapshot([])


# ---------------- percentile_rank / classify ----------------

def test_percentile_rank():
    hist = [0.0, 0.05, 0.10, 0.15, 0.20]
    assert percentile_rank(0.10, hist) == pytest.approx(60.0)   # 3 of 5 <= 0.10
    assert percentile_rank(-0.1, hist) == pytest.approx(0.0)
    assert percentile_rank(0.30, hist) == pytest.approx(100.0)


def test_classify_cheap_neutral_rich():
    hist = [i / 100 for i in range(0, 21)]  # 0%..20%, 21 points
    assert classify(0.005, hist).label in ("极便宜", "便宜")
    assert classify(0.10, hist).label == "中性"
    assert classify(0.195, hist).label in ("偏贵", "极贵")


def test_classify_extremes():
    hist = [i / 100 for i in range(0, 21)]
    assert classify(0.0, hist).label == "极便宜"
    assert classify(0.20, hist).label == "极贵"


def test_classify_insufficient_history():
    sig = classify(0.15, [0.1, 0.2, 0.3])  # <8
    assert sig.label == "历史不足"
    assert math.isnan(sig.percentile)


# ---------------- history IO ----------------

def test_history_roundtrip(tmp_path):
    path = tmp_path / "hist.json"
    snaps = [
        ValuationSnapshot("2024-09-30", 500, 0.004, 0.01, 0.51, -0.057, 0.052),
        ValuationSnapshot("2025-12-31", 285, 0.216, 0.22, 0.91, 0.128, 0.289),
    ]
    save_history(path, snaps)
    loaded = load_history(path)
    assert [s.date for s in loaded] == ["2024-09-30", "2025-12-31"]
    assert loaded[1].median_deviation == pytest.approx(0.216)


def test_append_history_overwrites_same_date(tmp_path):
    path = tmp_path / "hist.json"
    save_history(path, [ValuationSnapshot("2026-01-01", 10, 0.10, 0.10, 0.8, 0.0, 0.2)])
    append_history(path, ValuationSnapshot("2026-01-01", 12, 0.15, 0.15, 0.9, 0.0, 0.3))
    append_history(path, ValuationSnapshot("2026-04-01", 11, 0.05, 0.05, 0.6, 0.0, 0.1))
    loaded = load_history(path)
    assert len(loaded) == 2                                  # 同日覆盖
    by_date = {s.date: s for s in loaded}
    assert by_date["2026-01-01"].median_deviation == pytest.approx(0.15)


def test_load_history_missing_returns_empty(tmp_path):
    assert load_history(tmp_path / "nope.json") == []


def test_gui_auto_record_helper_idempotent(tmp_path):
    """批量页自动记录: 成功落盘、同估值日幂等覆盖、空结果静默失败。"""
    from convertible_bond.gui.tabs.batch import _record_valuation_history
    path = tmp_path / "hist.json"
    assert _record_valuation_history(_rows([0.10, 0.15, 0.20]), history_path=path) is True
    loaded = load_history(path)
    assert len(loaded) == 1
    assert loaded[0].median_deviation == pytest.approx(0.15)
    # 同日重算 → 覆盖而非追加
    assert _record_valuation_history(_rows([0.30, 0.30, 0.30]), history_path=path) is True
    loaded = load_history(path)
    assert len(loaded) == 1
    assert loaded[0].median_deviation == pytest.approx(0.30)
    # 空结果 → 静默失败不写盘
    assert _record_valuation_history([], history_path=path) is False


# ---------------- valuation_banner (GUI 横幅) ----------------

def test_valuation_banner_rich():
    hist = [i / 100 for i in range(0, 21)]          # 0%..20%
    rows = _rows([0.18, 0.20, 0.22, 0.25, 0.19])    # 中位 +20% -> 极贵
    banner, detail = valuation_banner(rows, hist)
    assert "市场估值" in banner and ("偏贵" in banner or "极贵" in banner)
    assert "中位偏差" in banner
    assert detail                                    # 详情非空


def test_valuation_banner_empty_rows():
    assert valuation_banner([], [0.1, 0.2]) == ("", "")


def test_valuation_banner_insufficient_history():
    banner, _ = valuation_banner(_rows([0.1, 0.2, 0.3]), [0.1, 0.2])  # 历史<8
    assert "历史不足" in banner


# ---------------- 口径分段 (caliber v1/v2) ----------------
#
# 修掉"赎回门槛条款被解析成未转股余额"的 bug 后, 主池补回约 74 只被误判余额过小的
# 大盘券, 中位偏差整体下移约 0.7pp。序列前后不严格可比, 因此给每条快照打口径标记。

def test_snapshot_defaults_to_current_caliber():
    snap = compute_snapshot(_rows([0.1, 0.2, 0.3]))
    assert snap.caliber == CURRENT_CALIBER == CALIBER_V2
    assert snap.to_record()["caliber"] == CALIBER_V2


def test_load_history_treats_unlabelled_records_as_v1(tmp_path):
    """存量 18 条基线没有 caliber 字段, 必须归入 v1 而不是被当成当期口径。"""
    path = tmp_path / "hist.json"
    path.write_text(json.dumps({"records": [
        {"date": "2024-09-30", "n": 400, "median_deviation": 0.004,
         "mean_deviation": 0.01, "pct_overvalued": 0.74, "p25": -0.05, "p75": 0.12},
    ]}, ensure_ascii=False), encoding="utf-8")
    loaded = load_history(path)
    assert [s.caliber for s in loaded] == [CALIBER_V1]


def test_caliber_survives_history_roundtrip(tmp_path):
    path = tmp_path / "hist.json"
    old = ValuationSnapshot(date="2024-09-30", n=400, median_deviation=0.004,
                            mean_deviation=0.01, pct_overvalued=0.74,
                            p25=-0.05, p75=0.12, caliber=CALIBER_V1)
    save_history(path, [old])
    append_history(path, compute_snapshot(_rows([0.1, 0.2, 0.3])))
    loaded = load_history(path)
    assert [s.caliber for s in loaded] == [CALIBER_V1, CALIBER_V2]


def test_caliber_note_silent_when_single_caliber():
    same = [ValuationSnapshot(date=f"2026-0{i}-01", n=200, median_deviation=0.1,
                              mean_deviation=0.1, pct_overvalued=0.9, p25=0.0,
                              p75=0.2, caliber=CALIBER_V2) for i in range(1, 4)]
    assert caliber_note(same, CALIBER_V2) == ""
    # 裸 float 序列没有口径信息, 不应凭空造断点提示
    assert caliber_note([0.1, 0.2, 0.3], CALIBER_V2) == ""


def test_caliber_note_flags_mixed_history():
    hist = [ValuationSnapshot(date=f"202{i}-06-30", n=200, median_deviation=0.13,
                              mean_deviation=0.13, pct_overvalued=0.9, p25=0.0,
                              p75=0.2, caliber=CALIBER_V1) for i in range(2, 6)]
    note = caliber_note(hist, CALIBER_V2)
    assert "口径" in note
    assert f"{CALIBER_V1} 4 期" in note
    assert CALIBER_CHANGES[CALIBER_V2]["since"] in note


def test_valuation_banner_appends_caliber_note_for_mixed_history():
    """跨口径时详情要说清断点; 横幅本身保持单行不变。"""
    hist = [ValuationSnapshot(date=f"2025-{m:02d}-01", n=200, median_deviation=m / 100,
                              mean_deviation=m / 100, pct_overvalued=0.9, p25=0.0,
                              p75=0.2, caliber=CALIBER_V1) for m in range(1, 13)]
    banner, detail = valuation_banner(_rows([0.10, 0.11, 0.12]), hist)
    assert "市场估值" in banner and "\n" not in banner
    assert "口径" in detail and CALIBER_V1 in detail


def test_valuation_banner_accepts_plain_float_history():
    """旧调用 (裸 median 序列) 仍可用, 只是详情里没有口径说明。"""
    hist = [i / 100 for i in range(0, 21)]
    banner, detail = valuation_banner(_rows([0.18, 0.20, 0.22]), hist)
    assert "市场估值" in banner
    assert "口径" not in detail
