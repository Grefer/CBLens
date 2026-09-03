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
    # **按季度末**造 10 期。分位基线是季度桶去重的 (baseline_medians), 10 个**月度**点
    # 只折叠成 4 个季度 → 不足 8 → 分位是 NaN。"够不够"按季度数算, 不按记录条数算。
    hist.write_text(json.dumps({"records": [
        {"date": f"{2022 + i // 4}-{(i % 4 + 1) * 3:02d}-28", "n": 100,
         "median_deviation": 0.05 * i, "mean_deviation": 0.05 * i,
         "pct_overvalued": 0.5, "p25": 0.0, "p75": 0.1}
        for i in range(10)
    ]}), encoding="utf-8")
    rc = main(["--cache", str(cache), "--history", str(hist), "--json"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert isinstance(parsed["signal"]["percentile"], (int, float))
    assert parsed["snapshot"]["n"] == 3


def test_missing_cache_returns_error(tmp_path, capsys):
    rc = main(["--cache", str(tmp_path / "nope.json")])
    assert rc == 2


# ---------------- 覆盖率闸: --record 不能绕过 GUI 那道闸 ----------------

def _no_market_row():
    """``status == "ok"`` 但市价缺失 → deviation 是 NaN, 进不了快照。"""
    return {"deviation": float("nan"), "status": "ok", "valuation_date": "2026-05-26"}


def test_record_is_refused_when_pricing_coverage_is_too_low(tmp_path, capsys):
    """覆盖率不足时 ``--record`` 必须拒记并返回非 0.

    此前这里是无条件 ``append_history``: GUI 批量页在覆盖率 < 90% 时拒记并在状态栏
    说明原因, 而 ``_batch_worker`` **刻意仍然写主缓存** (它是运行态, 部分结果照样有用),
    于是被拒的那份产物就留在盘上等着这条 CLI 来记 —— 而 ``cb_valuation_history.json``
    进版本库、只追加, 且 ``baseline_medians`` 取桶内最晚一条, 坏记录会当上该季度的代表。
    实测按上市日切的系统性失败能造出 +48.96% vs 真实 +21.25% 的假快照 (偏离 27.7pp,
    超过整个历史摆幅 21.2pp)。
    """
    cache = tmp_path / "c.json"
    hist = tmp_path / "hist.json"
    _write_cache(cache, [_row(0.1), _row(0.2)] + [_no_market_row()] * 8, [])

    rc = main(["--cache", str(cache), "--history", str(hist), "--record"])

    assert rc == 1
    assert not hist.exists(), "覆盖率不足却仍然写了基线"
    err = capsys.readouterr().err
    assert "2/10" in err and "未记入估值基线" in err


def test_record_is_allowed_when_coverage_is_fine(tmp_path):
    cache = tmp_path / "c.json"
    hist = tmp_path / "hist.json"
    # 池子取 120 只: `--record` 那条路上还有 ``MIN_BASELINE_POOL`` 绝对下限,
    # 而三只债的"全市场中位偏差"本来就不该记进版本库的基线。
    _write_cache(cache, [_row(0.1), _row(0.15), _row(0.2)] * 40, [])

    rc = main(["--cache", str(cache), "--history", str(hist), "--record"])

    assert rc == 0
    assert json.loads(hist.read_text(encoding="utf-8"))["records"]


def test_force_is_the_explicit_escape_hatch(tmp_path):
    cache = tmp_path / "c.json"
    hist = tmp_path / "hist.json"
    _write_cache(cache, [_row(0.1)] + [_no_market_row()] * 9, [])

    rc = main(["--cache", str(cache), "--history", str(hist), "--record", "--force"])

    assert rc == 0
    assert json.loads(hist.read_text(encoding="utf-8"))["records"]


def test_report_prints_the_denominator_not_just_the_numerator(tmp_path, capsys):
    """只印"样本 N 只"时用户连眼估覆盖率都做不到 —— 而那正是这个中位数能不能信的判据。"""
    cache = tmp_path / "c.json"
    _write_cache(cache, [_row(0.1), _row(0.2)] + [_no_market_row()] * 3, [])

    main(["--cache", str(cache), "--history", str(tmp_path / "h.json")])

    assert "定价覆盖: 2/5 (40%)" in capsys.readouterr().out


def test_json_carries_coverage_and_the_refusal(tmp_path, capsys):
    cache = tmp_path / "c.json"
    hist = tmp_path / "hist.json"
    _write_cache(cache, [_row(0.1)] + [_no_market_row()] * 9, [])

    rc = main(["--cache", str(cache), "--history", str(hist),
               "--record", "--json"])

    assert rc == 1
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["coverage"] == {"usable": 1, "total": 10, "min_required": 0.9}
    assert parsed["recorded"] is False
    assert "未记入估值基线" in parsed["record_refused"]
