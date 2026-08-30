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
    baseline_medians,
    save_history,
    valuation_banner,
)


def _rows(devs, status="ok", vd="2026-05-26"):
    return [{"bond_code": f"c{i}", "deviation": d, "status": status,
             "valuation_date": vd} for i, d in enumerate(devs)]


def _quarterly(medians, start=(2022, 1)):
    """按季度末生成快照序列 —— 分位基线的正常形态 (每季一条)。"""
    year, q = start
    out = []
    for m in medians:
        month = q * 3
        day = 31 if month in (3, 12) else 30
        out.append(ValuationSnapshot(
            date=f"{year}-{month:02d}-{day:02d}", n=200, median_deviation=m,
            mean_deviation=m, pct_overvalued=0.9, p25=m - 0.05, p75=m + 0.05,
            caliber=CALIBER_V2))
        q += 1
        if q > 4:
            q, year = 1, year + 1
    return out


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
    """跨口径时详情要说清断点; 横幅本身保持单行不变。

    **悬浮里不出现 ``v1``/``v2``**: 那是内部口径版本号, 读者看不懂也用不上。悬浮只需
    回答"这个分位能不能当真" —— 跨了两种主池口径, 不能完全当真。列版本号与期数是
    ``cb-valuation`` 那份终端报告的事 (``caliber_note`` 默认 verbose)。
    """
    hist = [ValuationSnapshot(date=f"2025-{m:02d}-01", n=200, median_deviation=m / 100,
                              mean_deviation=m / 100, pct_overvalued=0.9, p25=0.0,
                              p75=0.2, caliber=CALIBER_V1) for m in range(1, 13)]
    banner, detail = valuation_banner(_rows([0.10, 0.11, 0.12]), hist)
    assert "市场估值" in banner and "\n" not in banner
    assert "口径" in detail and "不严格可比" in detail
    assert CALIBER_V1 not in detail and CALIBER_V2 not in detail, (
        "悬浮里不该出现内部口径版本号")
    # 但完整版 (CLI) 仍要列出版本与期数
    verbose = caliber_note(hist, CALIBER_V2)
    assert f"{CALIBER_V1} 12 期" in verbose and CALIBER_V2 in verbose


def test_valuation_tooltip_says_how_to_read_instead_of_repeating_the_banner():
    """悬浮补充横幅, 不复述它; 也不给操作建议.

    原文 331 字 / 6 行, 四个毛病 (实测):

    ① 横幅已写「中位偏差 +21.2% · 历史分位 95%」, 详情第一、二行又各重复一遍。
    ② 「样本」一词两种含义且**相邻两行**: 「样本 284 只」是个券数,
       「样本 21」是历史期数 —— 挨着放只会被当成其中一个写错了。
    ③ 「强烈利于减仓」是操作建议, 而且没有主语 (谁减、减什么、减多少都没说)。
       这是研究工作台, README 通篇写着"不是投资建议"。
    ④ 口径断点写成 changelog (修了什么 bug / 补回多少只 / 中位偏差移了多少),
       占 136/331 字 = 41%, 而读者拿它做不了决定。
    """
    hist = _quarterly([m / 100 for m in range(1, 13)])
    banner, detail = valuation_banner(_rows([0.10, 0.11, 0.12]), hist)

    # ① 横幅里的两个数不在详情里复述
    assert "中位偏差 +" not in detail, "详情在复述横幅的数值"
    assert "历史分位 " not in detail

    # ② 「样本」这个没有主语的词退场 —— 两个计数各自点明对象
    assert "样本" not in detail, "「样本」在同一屏里有两种含义, 必须点明对象"
    assert "只" in detail and "个季度" in detail

    # ③ 不给操作建议
    for advice in ("加仓", "减仓", "利于"):
        assert advice not in detail, f"悬浮里出现了操作建议: {advice}"
    assert "不是个券信号" in detail and "不是操作建议" in detail

    # ④ 口径段不写 bug 细节 (这一档是同口径, 所以整段不出现; 跨口径的压行由上一条用例钉)
    assert "bug" not in detail and "补回" not in detail
    # 每一行都要短 —— 悬浮里一行折成三行就比表格还占地方
    for line in detail.split("\n"):
        assert len(line) <= 50, f"这一行太长 ({len(line)} 字): {line}"


def test_valuation_banner_accepts_plain_float_history():
    """旧调用 (裸 median 序列) 仍可用, 只是详情里没有口径说明。"""
    hist = [i / 100 for i in range(0, 21)]
    banner, detail = valuation_banner(_rows([0.18, 0.20, 0.22]), hist)
    assert "市场估值" in banner
    assert "口径" not in detail


def test_current_quarter_is_dropped_instead_of_ranked_against_itself():
    """当期不许跟自己比 —— 而且要**整季度**剔掉, 不是只剔同一天.

    全量重算的顺序是 ``append_history(当期)`` → 渲染横幅, 所以基线**包含今天**;
    ``percentile_rank`` 用 ``arr <= value`` 计数, 自己必然命中, 分位恒定上偏 1/n。

    只剔"同一天"不够: 实测 2026Q3 里躺着 08-22 / 08-23 / 08-29 三条 (间隔 1 天和 6 天),
    剔掉 08-29 之后 08-23 会顶上来当该季度的代表 —— 那是拿今天跟六天前的自己比,
    而"当季"根本还不是历史。所以是**先分桶, 再整桶剔**。

    CLI 此前自己写了一份"剔同日期", GUI 连剔都没剔 —— 同一个中位偏差在两处给出不同
    分位, 两边都不报错。现在两边都走 ``baseline_medians``。
    """
    hist = _quarterly([m / 100 for m in range(1, 12)])          # 2022Q1..2024Q3
    # 2024Q4 里放两条: 一条早的 + 当期。当期必须是该季度**最晚**的那条, 否则它压根
    # 赢不了桶, 自比效应不会被触发 —— 上一版 fixture 就是这样, 测了个寂寞。
    early_q4 = ValuationSnapshot(date="2024-10-10", n=200, median_deviation=0.30,
                                 mean_deviation=0.30, pct_overvalued=1.0, p25=0.3,
                                 p75=0.3, caliber=CALIBER_V2)
    # 当期值取**中段** —— 取极值时含不含自己都是 100%, 那种 fixture 会把真 bug 测成绿的
    today = ValuationSnapshot(date="2024-12-20", n=3, median_deviation=0.065,
                              mean_deviation=0.065, pct_overvalued=1.0, p25=0.065,
                              p75=0.065, caliber=CALIBER_V2)
    with_self = hist + [early_q4, today]

    kept_all = baseline_medians(with_self)
    kept_excl = baseline_medians(with_self, exclude_date=today.date)
    # 11 个季度 + 2024Q4 (代表 = 当期) = 12; 剔掉当季只剩 11
    assert len(kept_all) == 12 and len(kept_excl) == 11
    assert today.median_deviation in kept_all
    # 当季那个桶**整个**不见了 —— 同季度更早的那条不许顶上来当代表
    assert today.median_deviation not in kept_excl
    assert early_q4.median_deviation not in kept_excl

    # 剔掉之后分位更低 —— 自己那一票必然落在 `arr <= value` 里
    assert percentile_rank(today.median_deviation, kept_all) > 58.0
    assert (percentile_rank(today.median_deviation, kept_excl)
            < percentile_rank(today.median_deviation, kept_all))

    # 裸 float 序列没有日期, 既分不了桶也剔不了 —— 原样返回, 不许抛
    assert len(baseline_medians([0.1, 0.2], exclude_date="2026-01-01")) == 2


def test_baseline_keeps_one_snapshot_per_quarter_without_dropping_the_record():
    """日频记录不许按天数给最近这段行情加权; 但也不许因此销毁观测.

    基线混了两种采样频率: 前 18 期是季度末 (相邻间隔中位 91 天), 而全量重算每成功
    一次就追加一条, 于是冒出日频点 (实测 2026-08-22 / 08-23 / 08-29)。分位的全部价值
    在于**跨完整牛熊周期**比较 (中位偏差摆幅 +0.4%~+21.6%) —— 按每天开一次 GUI 算,
    一年后基线是 18 个季度点 + 约 244 个日点, 93% 的比较对象来自最近 12 个月。

    **去重放在读侧不放在 ``append_history``**: 写侧去重销毁真实观测且不可逆, 而桶的
    粒度与代表怎么选还会调 —— 与 ``data/watchlist_daily/`` 只追加不删的既定态度一致。
    所以这条用例两头都钉: 读侧折叠 + 写侧原样保留。
    """
    hist = _quarterly([0.10, 0.12, 0.14])                       # 2022Q1/Q2/Q3
    noisy = hist + [
        ValuationSnapshot(date="2022-09-05", n=200, median_deviation=0.90,
                          mean_deviation=0.90, pct_overvalued=1.0, p25=0.9, p75=0.9,
                          caliber=CALIBER_V2),
        ValuationSnapshot(date="2022-09-20", n=200, median_deviation=0.99,
                          mean_deviation=0.99, pct_overvalued=1.0, p25=0.99, p75=0.99,
                          caliber=CALIBER_V2),
    ]
    kept = baseline_medians(noisy)
    assert len(kept) == 3, "同一季度的多条记录没有折叠成一条"
    # 桶内取**最晚**一条 —— 2022Q3 的代表是季末 09-30 那条 (0.14), 不是两个日频点
    assert 0.14 in kept and 0.90 not in kept and 0.99 not in kept

    # 写侧原样保留: append_history 仍只按"同日期"覆盖
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "h.json"
        for snap in noisy:
            append_history(path, snap)
        assert len(load_history(path)) == len(noisy), "写侧去重了 —— 观测被销毁"

