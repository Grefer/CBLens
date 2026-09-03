"""market_valuation 转债大类估值/择时指标单测。"""
import json
import math

import pytest

from convertible_bond.market_valuation import (
    CALIBER_CHANGES,
    CALIBER_V1,
    CALIBER_V2,
    CALIBER_V3,
    CURRENT_CALIBER,
    ValuationSnapshot,
    caliber_note,
    append_history,
    baseline_refusal_reason,
    classify,
    record_snapshot,
    compute_snapshot,
    load_history,
    MIN_BASELINE_COVERAGE,
    percentile_rank,
    snapshot_coverage,
    baseline_medians,
    save_history,
    valuation_banner,
)


def _rows(devs, status="ok", vd="2026-05-26", repeat=1):
    """构造一批已定价行。

    ``repeat`` 把整个 devs 模式原样重复若干遍 —— 中位与分位完全不变, 只是池子够大。
    走 ``record_snapshot`` / ``baseline_refusal_reason`` 的用例需要它: 那条路上有
    ``MIN_BASELINE_POOL`` 绝对下限, 而三只债的"全市场中位偏差"本来就不该记进基线。
    """
    devs = list(devs) * repeat
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


def test_classify_bands_sit_at_10_25_75_90():
    """四条分位线的**位置**要逐条钉住, 不能只断言"三个区都存在"。

    这条用例原先写的是 ``label in ("极便宜", "便宜")`` 这类析取断言, 实测把
    ``_CHEAP_PCT`` 25→40、``_RICH_PCT`` 75→60、``_EXTREME_HI`` 90→97 **三种改法
    整套都全绿** —— 而 90% 那条正是横幅上「极贵」的判据 (AGENTS 里那句"分位
    95.0% → 94.1%, 标签仍是「极贵」, 阈值 90%"依赖的就是它)。

    fixture 用 21 个等距点, 于是每个取值的分位是算得出来的整数比 (n/21), 边界两侧
    各取一个: 括号里的百分数是 ``percentile_rank`` 的实际输出, 不是从常量反推的。
    """
    hist = [i / 100 for i in range(0, 21)]  # 0%..20%, 21 points

    assert classify(0.01, hist).label == "极便宜"    # 9.52% ≤ 10
    assert classify(0.02, hist).label == "便宜"      # 14.29% > 10
    assert classify(0.04, hist).label == "便宜"      # 23.81% ≤ 25
    assert classify(0.05, hist).label == "中性"      # 28.57% > 25
    assert classify(0.14, hist).label == "中性"      # 71.43% < 75
    assert classify(0.15, hist).label == "偏贵"      # 76.19% ≥ 75
    assert classify(0.17, hist).label == "偏贵"      # 85.71% < 90
    assert classify(0.18, hist).label == "极贵"      # 90.48% ≥ 90


def test_classify_extremes():
    hist = [i / 100 for i in range(0, 21)]
    assert classify(0.0, hist).label == "极便宜"
    assert classify(0.20, hist).label == "极贵"


def test_classify_insufficient_history():
    """"够不够"的门槛是 **8**, 两侧各钉一条 —— 只测 3 个点时门槛降到 4 也全绿。

    这个数按**季度**算 (``baseline_medians`` 每季度只留一条): 8 个季度 = 两年,
    少于两年的基线给不出可信的周期分位。
    """
    sig = classify(0.15, [i / 100 for i in range(7)])       # 7 期 → 不够
    assert sig.label == "历史不足"
    assert math.isnan(sig.percentile)
    # 钉**状态**而不是消息文本: 期数是 ValuationSignal 的字段, 而 note 里那句
    # "仅 7 个 (<8)" 的 8 是 f-string 里另写的一份字面量, 改门槛时它未必跟着动。
    assert sig.n_history == 7
    assert "7" in sig.note

    enough = classify(0.15, [i / 100 for i in range(8)])    # 8 期 → 够了
    assert enough.label != "历史不足" and math.isfinite(enough.percentile)


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
    """批量页自动记录: 成功落盘、同估值日幂等覆盖、空结果静默失败。

    返回值是**不记的原因**(记了返回 None), 不是 bool —— 拒记时那句话要显示给用户,
    静默跳过和"记了"长得一模一样。
    """
    from convertible_bond.gui.tabs.batch import _record_valuation_history
    path = tmp_path / "hist.json"
    assert _record_valuation_history(_rows([0.10, 0.15, 0.20], repeat=40), history_path=path) is None
    loaded = load_history(path)
    assert len(loaded) == 1
    assert loaded[0].median_deviation == pytest.approx(0.15)
    # 同日重算 → 覆盖而非追加
    assert _record_valuation_history(_rows([0.30, 0.30, 0.30], repeat=40), history_path=path) is None
    loaded = load_history(path)
    assert len(loaded) == 1
    assert loaded[0].median_deviation == pytest.approx(0.30)
    # 空结果 → 静默失败不写盘, 但要说得出原因
    assert _record_valuation_history([], history_path=path)
    assert len(load_history(path)) == 1


def test_partial_batch_does_not_pollute_the_versioned_baseline(tmp_path):
    """"部分取到"不许把几十只的中位当全市场快照写进基线.

    唯一的闸此前是 ``_batch_worker`` 的 ``success_count == 0``, 两个毛病:

    ① **数的是另一批行**。它数 ``status == "ok"``, 而快照数有限 ``deviation`` ——
       市价缺失时 ``deviation`` 是 NaN 而 ``status`` 仍是 "ok"。所以这个 fixture
       (全部 ok, 只有 10% 有市价) 在旧闸下是**满员通过**的。
    ② **全或无**。它只挡"一只都没成功", 而部分取到才是危险的那一档 ——
       ``cb_valuation_history.json`` **进版本库**、只追加, 而且 ``baseline_medians``
       取桶内最晚一条, 一条坏记录会当上该季度的代表。

    实测系统性失败 (按上市日切) 能错 **+27.7pp / −19.3pp**, 都超过整个历史摆幅 21.2pp。
    """
    from convertible_bond.gui.tabs.batch import _record_valuation_history

    path = tmp_path / "hist.json"
    # 先放一条好记录, 用来验证坏记录既不覆盖也不追加
    assert _record_valuation_history(_rows([0.10, 0.15, 0.20], repeat=40), history_path=path) is None
    before = load_history(path)

    # 284 行全部 status=ok, 但只有 28 行拿到市价 → 覆盖 9.9%
    partial = (_rows([0.50] * 28)
               + _rows([float("nan")] * 256))
    assert sum(1 for r in partial if r["status"] == "ok") == 284, "旧闸在这个 fixture 上满员"
    note = _record_valuation_history(partial, history_path=path)
    assert note and "28/284" in note and "未记入估值基线" in note, note
    assert load_history(path) == before, "坏快照写进了版本库里的基线"

    # 覆盖率够了 → 照常记 (同估值日 → 幂等覆盖那一条, 不是追加)
    good = _rows([0.10] * 260) + _rows([float("nan")] * 24)     # 91.5%
    assert _record_valuation_history(good, history_path=path) is None
    after = load_history(path)
    assert len(after) == 1 and after[0].n == 260
    assert after[0].median_deviation == pytest.approx(0.10)


def test_baseline_note_is_shown_after_the_render_overwrites_the_status_line(tmp_path):
    """拒记的那句话必须排在渲染**之后** —— 否则它被视图摘要盖掉, 等于没说.

    ``_render_table`` 把 ``v_batch_status`` 整个重写成「✅ 全池: 展示 N/M 只…」。
    ``app.after`` 的回调按登记顺序跑, 所以追加状态那一句要登记在渲染之后。
    这是**顺序**约束, 运行期看不出来 (只表现为"警告没出现"), 所以在源码上钉。
    """
    import inspect

    from convertible_bond.gui.tabs import batch as batch_tab

    src = inspect.getsource(batch_tab._batch_worker)
    render_at = src.index("_render_batch_views(\n            app, results,\n            cache_path=cache_path")
    note_at = src.index("baseline_note:")
    assert note_at > render_at, "拒记提示登记在渲染之前, 会被视图摘要盖掉"
    assert "_record_valuation_history(results)" in src
    # 主缓存**不受这道闸管** —— 它是运行态 gitignored, 部分结果照样有用
    assert src.index("save_batch_results_cache") < src.index("baseline_note = ")


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
    # 引用 CURRENT_CALIBER 而不是写死 v2 —— 用例的意图是「新快照打当期口径」,
    # 写死会让每次口径变更都误红一次 (口径变更本身由
    # test_current_caliber_is_registered_with_its_breakpoint 守着)
    assert snap.caliber == CURRENT_CALIBER
    assert snap.to_record()["caliber"] == CURRENT_CALIBER


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
    assert [s.caliber for s in loaded] == [CALIBER_V1, CURRENT_CALIBER]


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
    # 但完整版 (CLI) 仍要列出版本与期数。
    # **期数要数实际参与分位计算的那份基线**, 不是原始序列: 这里 12 条月度快照会去重成
    # 4 个季度桶 (``baseline_medians`` 每季度只留一条), 所以是 "v1 4 期"。早先这里写死
    # 12 —— 那个数描述的是原始 history, 而这句话警告的是**基线**的口径构成, 两者可以差
    # 很远 (实测盘上 22 条 v1/v2/v3 混合 → 基线 17 条全 v1, 于是横幅警告了一个根本不在
    # 基线里的断点)。
    from convertible_bond.market_valuation import baseline_snapshots
    verbose = caliber_note(hist, CALIBER_V2)
    n_baseline = len(baseline_snapshots(hist))
    assert n_baseline == 4, "12 条月度快照应去重成 4 个季度桶"
    assert f"{CALIBER_V1} {n_baseline} 期" in verbose and CALIBER_V2 in verbose


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


def test_snapshot_coverage_counts_the_same_rows_compute_snapshot_does():
    """覆盖率必须与快照**逐条同口径** —— 不是数 ``status == "ok"``.

    这两者是**两批不同的行**: ``pricing_api`` 在市价缺失时把 ``deviation`` 写 NaN 而
    ``status`` 仍留 "ok" (pricing_api.py:853 的 else 分支)。所以"转债行情整条挂掉、
    正股链路正常"这一档下 ``status == "ok"`` 满员而快照样本为零 —— 批量页此前的闸
    (``success_count == 0``) 完全看不见它, 那一档是靠 ``compute_snapshot`` 抛
    ValueError 兜住的, 不是靠判据。
    """
    ok_with_dev = {"deviation": 0.1, "status": "ok"}
    ok_no_market = {"deviation": float("nan"), "status": "ok"}   # 市价缺失 → 仍是 ok
    failed = {"deviation": 0.1, "status": "failed"}

    rows = [ok_with_dev] * 3 + [ok_no_market] * 6 + [failed] * 1
    usable, total = snapshot_coverage(rows)
    assert (usable, total) == (3, 10)
    # 与 compute_snapshot 真的同口径: 它聚合的样本数就是 usable
    assert compute_snapshot(rows).n == usable
    # 而"数 status == ok"会给出 9 —— 那是另一批行
    assert sum(1 for r in rows if r.get("status") == "ok") == 9

    # 一条能用的都没有时 compute_snapshot 抛错 (调用方靠覆盖率闸提前挡, 不靠这个异常)
    assert snapshot_coverage([ok_no_market] * 5) == (0, 5)
    with pytest.raises(ValueError):
        compute_snapshot([ok_no_market] * 5)


def test_coverage_threshold_is_pinned_to_the_measurement_that_produced_it():
    """0.90 是**量出来的**, 所以要用字面量钉住, 并配一条卡在边界上的行为用例。

    这条用例存在的理由是一次真实的空守护: 此处原本只有 ``0 < MIN_BASELINE_COVERAGE
    <= 1`` 一条范围断言, 而实测把常量改成 **0.80 整套仍然全绿** —— 那个值恰恰是
    AGENTS 里被测量判出局的那一档 (最坏偏离 3.11pp, 已超过季度桶自身 2.84pp 的抖动)。

    改这个数要重做的测量: 按上市日掐掉一段的最坏中位偏差偏离,
    80% → 3.11pp / **90% → 2.05pp** / 95% → 1.31pp, 而"当季代表取哪天"本身抖 2.84pp。
    闸要把部分失败的误差压到这个已接受的噪声**以下**, 所以取 90%。
    """
    assert MIN_BASELINE_COVERAGE == 0.90

    # 行为侧: 边界值全部写死字面量, 不从常量算 —— 从常量算出来的边界恒真。
    # 分子刻意都在 MIN_BASELINE_POOL 之上, 触发的确定是覆盖率那道闸。
    refused = _rows([0.1] * 178) + _rows([float("nan")] * 22)     # 178/200 = 89%
    reason = baseline_refusal_reason(refused)
    assert reason is not None and "178/200" in reason, "89% 覆盖率被放行了"

    accepted = _rows([0.1] * 182) + _rows([float("nan")] * 18)    # 182/200 = 91%
    assert baseline_refusal_reason(accepted) is None, "91% 覆盖率被拒了"


def test_record_snapshot_is_the_single_gate_both_writers_share(tmp_path):
    """覆盖率闸必须长在 ``market_valuation`` 这一层, 不能只长在某个调用方身上.

    它此前只写在 ``gui/tabs/batch._record_valuation_history`` 里, 而
    ``cb-valuation --record`` 读同一份 ``batch_pricing_cache.json`` 却是无条件
    ``append_history`` —— 两个写入方对同一个版本库文件用了两套判据。
    """
    path = tmp_path / "hist.json"
    ok_rows = _rows([0.1, 0.15, 0.2], repeat=40)
    bad_rows = ok_rows + [{"deviation": float("nan"), "status": "ok",
                           "valuation_date": "2026-05-26"}] * 1080

    # 覆盖率够 → 记, 返回 None
    assert record_snapshot(path, ok_rows) is None
    assert len(load_history(path)) == 1

    # 覆盖率不够 → 不记, 返回**原因** (不是静默跳过 —— 那和"记了"长得一模一样)。
    # 分子刻意仍在 MIN_BASELINE_POOL 之上, 这样触发的确定是覆盖率那道闸。
    reason = record_snapshot(path, bad_rows)
    assert reason is not None and "120/1200" in reason and "10%" in reason
    assert len(load_history(path)) == 1, "被拒的快照仍然写进去了"

    # force 是显式出口
    assert record_snapshot(path, bad_rows, force=True) is None

    # baseline_refusal_reason 单独可用, 且与 record_snapshot 判据一致
    assert baseline_refusal_reason(ok_rows) is None
    assert baseline_refusal_reason(bad_rows) == reason


def test_gui_batch_page_routes_through_the_shared_gate():
    """批量页不许再自己写一份覆盖率判据 —— 两份判据分叉正是这条链的老毛病。"""
    import ast
    import inspect

    from convertible_bond.gui.tabs import batch as batch_tab

    import textwrap

    tree = ast.parse(textwrap.dedent(
        inspect.getsource(batch_tab._record_valuation_history)))
    # 扫 **AST 的名字**, 不扫源码文本 —— 文本扫描会把 docstring 里"阈值依据见
    # MIN_BASELINE_COVERAGE"这句解释判红, 那是为了让规则变绿去改文档 (库内踩过一次)。
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}

    assert "record_snapshot" in names, "批量页没走共用闸"
    assert "append_history" not in names, (
        "批量页直接调了无闸的 append_history —— 那正是 CLI 曾经的写法")
    assert "MIN_BASELINE_COVERAGE" not in names, "批量页又自己写了一份阈值比较"
    assert "snapshot_coverage" not in names, "批量页又自己数了一遍覆盖率"


def test_unlisted_new_bonds_leave_the_coverage_denominator():
    """未上市新债**分子分母都不进** —— 它们没有市价是天然状态, 不是取数失败。

    准入层 2026-08-31 起放行「已发行未上市」(它们后续会挂牌, 值得提前盯), 于是主池里
    开始出现没有市价的行。若把它们算进覆盖率分母, 就是拿"还没挂牌"当"今天没取到价":
    实测在途新债超过 35 只就能把 ``MIN_BASELINE_COVERAGE`` 的 90% 闸压住, 结果是
    **发行密集期反而记不进基线** —— 正好把这道闸推向它要防的反方向。

    只从分子里剔掉更糟 (覆盖率比不剔还低), 所以这条用例两头都钉。
    关注池的 ``market_price_coverage`` 早就这么处理了, 判据共用
    ``batch_pricing.is_unlisted_new_bond``。
    """
    # 池子取 120 只 (> ``MIN_BASELINE_POOL``): 这条用例钉的是**分母**怎么数,
    # 不是池子大不大, 所以不能让绝对下限那道闸抢先拒记。
    priced = [{"bond_code": f"c{i}", "status": "ok", "deviation": 0.1,
               "listing_date": "2020-01-01", "valuation_date": "2026-08-31"}
              for i in range(120)]
    new_bond = {"bond_code": "new", "status": "ok", "deviation": float("nan"),
                "listing_date": None, "trading_status": "pending",
                "valuation_date": "2026-08-31"}

    # 120 只在市 + 1 只在途 → 覆盖率是 120/120 而不是 120/121
    assert snapshot_coverage(priced + [new_bond]) == (120, 120)
    assert baseline_refusal_reason(priced + [new_bond]) is None

    # 加到 5 只在途也一样 —— 分母不随发行节奏漂移
    assert snapshot_coverage(priced + [new_bond] * 5) == (120, 120)

    # 而**在市**债缺市价仍然照常拉低覆盖率 (那才是取数失败)
    stale = {"bond_code": "stale", "status": "ok", "deviation": float("nan"),
             "listing_date": "2020-01-01", "valuation_date": "2026-08-31"}
    assert snapshot_coverage(priced + [stale] * 40) == (120, 160)
    assert baseline_refusal_reason(priced + [stale] * 40) is not None


def test_current_caliber_is_registered_with_its_breakpoint():
    """``CURRENT_CALIBER`` 必须在 ``CALIBER_CHANGES`` 里登记 —— 否则断点说明是空的。

    口径标记的全部作用是让横幅与 CLI 说得出"这个分位跨了几种池口径、从哪天开始变的"。
    换了 ``CURRENT_CALIBER`` 却忘了登记, ``caliber_note`` 里的 ``change`` 会是 None,
    那句提示就退化成"跨 N 种口径"而说不出**哪天**换的 —— 而它不报错。
    """
    assert CURRENT_CALIBER in CALIBER_CHANGES, (
        f"{CURRENT_CALIBER} 没登记断点, caliber_note 说不出换口径的日期")
    entry = CALIBER_CHANGES[CURRENT_CALIBER]
    assert {"since", "summary", "impact"} <= set(entry)
    from datetime import date as _date
    _date.fromisoformat(entry["since"])          # 必须是可解析的日期
    assert entry["impact"], "断点必须写清对中位偏差的影响量, 否则读者判断不了可比性"


def test_pool_widening_registered_as_caliber_v3():
    """2026-08-31 准入层收窄成"买不买得到"是一次**池口径变更**, 必须换 caliber。

    中位偏差是**全市场池**的聚合量, 池成员一变前后就不严格可比。实测同一批定价结果:
    旧口径 (评级≥A+、非 ST、已上市) n=282 中位 +21.30%,
    新口径 n=309 中位 +20.55% —— 下移 **0.75pp**。

    这个量级与当年 v1→v2 那次 (0.7pp) 相同, 而那次登记了新口径。参照系: 该指标的历史
    摆幅是 21.2pp (+0.4% ~ +21.6%), 所以 0.75pp 属小量 —— 仍**合并算分位**、只标断点,
    与 v2 同处置 (分段会让新序列在积满 8 个季度前完全失去分位信号)。
    """
    # 两边都解同一个符号时它的**值**完全不受约束 —— 实测把 CALIBER_V3 改成
    # "v7-typo" 这条与整套全绿, 而这个串是**落进 cb_valuation_history.json 的**,
    # 改了会让新旧记录分成两组、`caliber_note` 认不出。所以值要写死。
    assert CALIBER_V3 == "v3"
    assert CURRENT_CALIBER == CALIBER_V3
    assert CALIBER_CHANGES[CALIBER_V3]["since"] == "2026-08-31"

    # 新快照默认打当期口径
    snap = compute_snapshot(_rows([0.10, 0.15, 0.20]))
    assert snap.caliber == CALIBER_V3

    # 跨口径时断点说明要说得出日期
    from convertible_bond.market_valuation import caliber_note
    hist = _quarterly([0.10, 0.12, 0.14]) + [snap]
    note = caliber_note(hist, verbose=False)
    assert "2026-08-31" in note and "不严格可比" in note


def test_a_collapsed_pool_cannot_slip_past_the_coverage_ratio():
    """覆盖率是比值, 池子塌掉时分子分母一起塌 —— 必须另有一道绝对下限。

    ``baseline_refusal_reason`` 此前只有 ``usable < total * MIN_BASELINE_COVERAGE``
    一道闸。极端情形: 主池只剩 1 只债、那一只定价成功 → 1/1 = 100%, 干净通过, 而
    写进版本库的"全市场中位偏差"是**一只债**的偏差, 还会当上该季度的代表
    (``baseline_medians`` 取桶内最晚一条)。

    池子塌掉不是假想 —— 一次全量同步把 ``underlying_name`` 清掉 311 只、一次解析
    bug 让 103 只大盘券被判"余额过小", 库内每一项自洽性检查当时都是绿的。
    """
    from convertible_bond.market_valuation import (
        MIN_BASELINE_POOL, baseline_refusal_reason, is_coverage_refusal)

    # 100 这个数是按盘上 22 期历史基线的池规模 (193 ~ 522, 中位 424) 定的: 对任何一期
    # 真实历史都不触发 (离最小的那期还有 1.93 倍余量), 而全库 1000+ 只债只剩不到 100 只
    # 可投是结构性故障不是行情。改它要重新量那个区间。
    #
    # 写字面量而不是范围: 这里原本是 ``30 < MIN_BASELINE_POOL < 193``, 实测把常量
    # 改成 **50 整套仍然全绿** —— 范围断言放行了一半的取值, 而下面的边界用例又是从
    # 常量自己算出来的 (``MIN_BASELINE_POOL - 1``), 恒真。
    assert MIN_BASELINE_POOL == 100

    full = _rows([0.10, 0.15, 0.20], repeat=40)          # 120 只, 覆盖率 100%
    assert baseline_refusal_reason(full) is None

    collapsed = _rows([0.10])                             # 1 只, 覆盖率同样是 100%
    reason = baseline_refusal_reason(collapsed)
    assert reason is not None, "只剩一只债的池子以 100% 覆盖率通过了闸"
    assert "1" in reason
    # 与覆盖率拒记同一类: 用户明知故犯时 ``--force`` 仍是出口, 而不是"基础设施坏了"。
    assert is_coverage_refusal(reason)

    # 边界写字面量 —— 从 MIN_BASELINE_POOL 算出来的 99/100 对**任何**取值都成立。
    # 两条覆盖率都是 100%, 所以触发的确定是绝对下限那道闸。
    assert baseline_refusal_reason(_rows([0.10] * 99)) is not None, "99 只的池子通过了"
    assert baseline_refusal_reason(_rows([0.10] * 100)) is None, "100 只的池子被拒了"
