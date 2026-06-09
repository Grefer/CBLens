"""cb-valuation CLI 单测: 关注池口径 + --json 合法性 (review P2/P3 修复)。"""
import json

from convertible_bond.cli.market_valuation import _load_results, main


def _write_cache(path, results, upcoming):
    path.write_text(json.dumps({
        "results": results, "upcoming_results": upcoming,
    }, ensure_ascii=False), encoding="utf-8")


def _row(dev):
    return {"deviation": dev, "status": "ok", "valuation_date": "2026-05-26"}


# ---------------- Fix 1: 默认只用主全市场池 results ----------------

def test_load_results_excludes_watchlist_by_default(tmp_path):
    cache = tmp_path / "c.json"
    _write_cache(cache, [_row(0.1), _row(0.2)], [_row(9.9)])
    rows = _load_results(cache)
    assert len(rows) == 2
    assert all(r["deviation"] < 1 for r in rows)        # 未混入关注池的 9.9


def test_load_results_includes_watchlist_with_flag(tmp_path):
    cache = tmp_path / "c.json"
    _write_cache(cache, [_row(0.1), _row(0.2)], [_row(9.9)])
    rows = _load_results(cache, include_watchlist=True)
    assert len(rows) == 3


# ---------------- Fix 4: 历史不足时 --json 仍是合法 JSON (percentile=null) ----------------

def test_json_valid_when_history_insufficient(tmp_path, capsys):
    cache = tmp_path / "c.json"
    _write_cache(cache, [_row(0.1), _row(0.15), _row(0.2)], [])
    hist = tmp_path / "hist.json"                       # 不存在 → 历史不足
    rc = main(["--cache", str(cache), "--history", str(hist), "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "NaN" not in out                              # 不得输出非标准 NaN
    parsed = json.loads(out)                             # 严格解析通过
    assert parsed["signal"]["percentile"] is None
    assert parsed["signal"]["label"] == "历史不足"


def test_json_valid_with_history(tmp_path, capsys):
    cache = tmp_path / "c.json"
    _write_cache(cache, [_row(0.1), _row(0.15), _row(0.2)], [])
    hist = tmp_path / "hist.json"
    hist.write_text(json.dumps({"records": [
        {"date": f"2024-{m:02d}-01", "n": 100, "median_deviation": 0.05 * i,
         "mean_deviation": 0.05 * i, "pct_overvalued": 0.5, "p25": 0.0, "p75": 0.1}
        for i, m in enumerate(range(1, 11))
    ]}), encoding="utf-8")
    rc = main(["--cache", str(cache), "--history", str(hist), "--json"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert isinstance(parsed["signal"]["percentile"], (int, float))
    assert parsed["snapshot"]["n"] == 3


def test_missing_cache_returns_error(tmp_path, capsys):
    rc = main(["--cache", str(tmp_path / "nope.json")])
    assert rc == 2
