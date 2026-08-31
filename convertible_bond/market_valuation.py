"""转债大类估值 / 择时指标 (market_valuation).

把"模型理论价 vs 市价"的**全市场聚合偏差**做成一个可日常使用的估值/择时信号。

背景 (经本仓库 2022–2026 季度数据验证):
  - 单券 ``deviation = (市价 - 理论价)/理论价`` 的横截面**排序无预测力**, 但其
    **全市场中位数**是一个干净的转债大类估值周期: 中位偏差在 0%(2024-09 熊市谷底)
    到 +21%(2025-12 高位) 之间摆动, 长期中枢约 +13%。
  - 该中位数与中证转债指数**下一段收益显著负相关 (Spearman≈-0.52)**: 中位偏差高
    (市场贵) 后续跌, 压到低位 (便宜) 后续涨。便宜组下季均收益约 +2.8% vs 贵组约 0%。

因此本模块提供:
  - :func:`compute_snapshot` —— 从一批已定价结果 (含 ``deviation``) 算当期聚合快照;
  - :func:`classify` —— 把当期中位偏差放进历史分布给出分位 + 贵/便宜信号;
  - 历史基线的读写 (:func:`load_history` / :func:`append_history`)。

定位提醒: 这是**大类择时/估值**指标, 不是个券买入信号; 个券机会分仅作复核标记。
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Sequence

import numpy as np

logger = logging.getLogger(__name__)

# ---------------- 口径版本 (caliber) ----------------
# 中位偏差是**全市场池**的聚合量, 池口径一变, 序列前后就不严格可比。这里给每条快照
# 打口径标记, 让横幅与 CLI 能说清"哪一段是什么口径算出来的"。
#
#   v1: 2026-08 之前的历史基线。彼时赎回公告里的摘牌门槛条款
#       ("未转股余额少于3,000万元时…") 被解析成真实余额写进 cb_terms_patches.json,
#       导致约 74 只真实余额 ≥0.5 亿的大盘券被准入过滤当成"余额过小"剔除。
#   v2: 修复该解析 bug 之后的口径 (见 cb_event_sync.parse_outstanding_balance_change
#       与 cli/repair_balance_patches)。
#
# 为什么仍然合并计算分位而不是分段重算: 同一批定价结果下的对照实测显示, 补回这批券只
# 让中位偏差下移约 0.7pp (14.5%→13.8%), 相对该指标 [+0.4%, +21.6%] 的历史摆幅属于小量;
# 而分段会让 v2 序列在积满 8 个季度前完全失去分位信号。因此合并算分位、显式标注断点。
CALIBER_V1 = "v1"
CALIBER_V2 = "v2"
CURRENT_CALIBER = CALIBER_V2

CALIBER_CHANGES: dict[str, dict[str, str]] = {
    CALIBER_V2: {
        "since": "2026-08-21",
        "summary": "修复赎回门槛条款被误解析成未转股余额的 bug",
        "impact": "主池补回约 74 只被误判余额过小的大盘券; 同批对照实测中位偏差下移约 0.7pp",
    },
}

# 分位阈值: 当期中位偏差在历史中的百分位
_CHEAP_PCT = 25.0
_RICH_PCT = 75.0
_EXTREME_LO = 10.0
_EXTREME_HI = 90.0


@dataclass
class ValuationSnapshot:
    """某一估值日的全市场偏差聚合快照。"""

    date: str | None
    n: int
    median_deviation: float
    mean_deviation: float
    pct_overvalued: float
    p25: float
    p75: float
    caliber: str = CURRENT_CALIBER

    def to_record(self) -> dict[str, Any]:
        return asdict(self)


def _finite(value: Any) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if np.isfinite(f) else None


def _coerce_date(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()[:10]
    return str(value)[:10]


#: 记入历史基线所需的最低 deviation 覆盖率。
#:
#: **这不是架在市场量上的绝对阈值** (那种判据项目里已经栽过, 见 MIN_RELATIVE_CHEAPNESS
#: 的注释): 覆盖率度量的是"这一轮取数成功了多少", 取值恒在 [0,1] 且不随市场周期漂移。
#:
#: 取 0.9 的两条依据 —— 实测当前主池 284 只 (中位偏差 +21.25%):
#:
#: · **随机失败是良性的, 系统性失败才致命**。1000 次重采样: 随机剩 40 只时 95% 区间
#:   只偏离 ±4.8pp 且中位无偏; 而按上市日切的系统性失败, 只剩最新 40 只 → **+48.96%
#:   (偏离 +27.7pp)**, 只剩最老 40 只 → **+1.93% (−19.3pp)** —— 两个方向都超过整个
#:   历史摆幅 (21.2pp), 一条记录就能造出"史上最贵/最便宜"的假快照。而按上市日相关的
#:   系统性失败不是假想: 新债的 HV 样本、新老债的行情端点、临近到期停牌都与它相关。
#: · **要压到已经接受的噪声以下**。季度桶的"当季代表取哪天"本身有 2.84pp 抖动
#:   (实测 2026Q3 七天内)。按上市日掐掉一段的最坏偏离: 覆盖 80% → 3.11pp (已超),
#:   **90% → 2.05pp**, 95% → 1.31pp。
#:
#: 代价不对称也支持偏严: 漏记一次几乎无成本 (季度桶每季度只需要一次好记录), 而记错
#: 一次进的是**版本库**、还会当上该季度的代表 —— 要改就得手工动 JSON。
MIN_BASELINE_COVERAGE = 0.90


def _usable_deviations(
    rows: Sequence[dict[str, Any]],
    *,
    deviation_key: str = "deviation",
    status_key: str = "status",
    require_ok: bool = True,
) -> tuple[list[float], list[str]]:
    """能进快照的 (deviation 列表, 估值日列表)。``compute_snapshot`` 与覆盖率共用。

    **未上市新债整行跳过** —— 分子分母都不进。它们没有市价是天然状态而不是取数失败:
    进了分母就是拿"还没挂牌"当"今天没取到价", 实测在途新债超过 35 只就能把
    ``MIN_BASELINE_COVERAGE`` 的 90% 闸压住, 于是发行密集期反而记不进基线。
    关注池的 ``market_price_coverage`` 早就这么处理了 (那里 5 只里 3 只在途, 拿 5 做
    分母会让"一切正常"永远报成 2/5); 判据共用 ``batch_pricing.is_unlisted_new_bond``。
    """
    from .batch_pricing import is_unlisted_new_bond   # 延迟导入: 避免模块级循环依赖

    devs: list[float] = []
    dates: list[str] = []
    for row in rows:
        if require_ok and status_key in row and row.get(status_key) != "ok":
            continue
        if is_unlisted_new_bond(row):
            continue
        dv = _finite(row.get(deviation_key))
        if dv is None:
            continue
        devs.append(dv)
        vd = _coerce_date(row.get("valuation_date"))
        if vd:
            dates.append(vd)
    return devs, dates


def snapshot_coverage(rows: Sequence[dict[str, Any]], **kwargs: Any) -> tuple[int, int]:
    """``(能进快照的行数, 总行数)``。

    **必须与 ``compute_snapshot`` 逐条同口径**, 所以走同一个 ``_usable_deviations``。
    批量页此前的闸是 ``success_count == 0``, 数的是 ``status == "ok"`` —— 那是**另一批
    行**: ``pricing_api`` 在市价缺失时把 ``deviation`` 写 NaN 而 ``status`` 仍留 "ok",
    于是"转债行情整条挂掉、正股链路正常"会让 success_count 满员而快照样本为零。
    那一档碰巧被 ``compute_snapshot`` 抛 ValueError 兜住了, 但那是异常兜的, 不是判据。
    """
    from .batch_pricing import is_unlisted_new_bond   # 延迟导入: 避免模块级循环依赖

    devs, _ = _usable_deviations(rows, **kwargs)
    # 分子分母**必须一起**排除未上市新债 —— 只从分子里剔掉会让覆盖率比不剔还低,
    # 正好把这道闸推向它要防的那个方向。
    expected = sum(1 for row in rows if not is_unlisted_new_bond(row))
    return len(devs), expected


def compute_snapshot(
    rows: Sequence[dict[str, Any]],
    *,
    snapshot_date: Any = None,
    deviation_key: str = "deviation",
    status_key: str = "status",
    require_ok: bool = True,
) -> ValuationSnapshot:
    """从一批已定价结果聚合出当期估值快照。

    只统计状态为 ok (``require_ok``) 且 deviation 有限的行。``snapshot_date`` 缺省时
    取行内 ``valuation_date`` 的众数 (批量结果通常同一估值日)。
    """
    devs, dates = _usable_deviations(
        rows, deviation_key=deviation_key, status_key=status_key, require_ok=require_ok)
    if not devs:
        raise ValueError("没有可用的 deviation 数据 (检查结果是否已定价)")

    arr = np.array(devs, dtype=float)
    if snapshot_date is not None:
        snap_date = _coerce_date(snapshot_date)
    elif dates:
        snap_date = max(set(dates), key=dates.count)  # 众数估值日
    else:
        snap_date = None
    return ValuationSnapshot(
        date=snap_date,
        n=int(arr.size),
        median_deviation=float(np.median(arr)),
        mean_deviation=float(arr.mean()),
        pct_overvalued=float((arr > 0).mean()),
        p25=float(np.percentile(arr, 25)),
        p75=float(np.percentile(arr, 75)),
    )


@dataclass
class ValuationSignal:
    percentile: float          # 当期中位偏差在历史中的百分位 (0–100)
    label: str                 # 极便宜 / 便宜 / 中性 / 偏贵 / 极贵 / 历史不足
    median_deviation: float
    n_history: int
    note: str

    def __str__(self) -> str:
        return (f"估值信号: {self.label} (中位偏差 {self.median_deviation*100:+.1f}%, "
                f"历史分位 {self.percentile:.0f}%, 样本 {self.n_history})\n{self.note}")


def percentile_rank(value: float, history: Sequence[float]) -> float:
    """value 在 history 中的百分位 (0–100), 用 <= 计数。空历史返回 nan。"""
    arr = np.array([h for h in history if h is not None and np.isfinite(h)], dtype=float)
    if arr.size == 0:
        return float("nan")
    return float((arr <= value).mean() * 100.0)


def classify(median_deviation: float, history_medians: Sequence[float]) -> ValuationSignal:
    """把当期中位偏差放进历史中位偏差分布, 给出分位 + 贵/便宜标签。

    高分位 = 偏贵 (历史经验后续跑弱), 低分位 = 便宜 (后续跑强)。
    """
    hist = [h for h in history_medians if h is not None and np.isfinite(h)]
    n = len(hist)
    if n < 8:
        return ValuationSignal(
            percentile=float("nan"), label="历史不足",
            median_deviation=median_deviation, n_history=n,
            note=f"历史样本仅 {n} 个 (<8), 分位信号不可靠; 请先用 --record 积累基线。")
    pct = percentile_rank(median_deviation, hist)
    if pct <= _EXTREME_LO:
        label, tilt = "极便宜", "历史极低位, 转债大类罕见便宜, 强烈利于加仓。"
    elif pct <= _CHEAP_PCT:
        label, tilt = "便宜", "估值偏低区, 利于加仓 (历史上此区后续季度收益偏高)。"
    elif pct >= _EXTREME_HI:
        label, tilt = "极贵", "历史极高位, 转债大类罕见昂贵, 强烈利于减仓。"
    elif pct >= _RICH_PCT:
        label, tilt = "偏贵", "估值偏高区, 利于减仓 (历史上此区后续季度收益偏弱)。"
    else:
        label, tilt = "中性", "估值居中, 无明显择时倾向。"
    note = (f"{tilt}\n[参考] 中位偏差与中证转债指数下一季收益历史负相关≈-0.52; "
            f"便宜组下季约+2.8% vs 贵组约0%。仅供大类配置参考, 非个券信号。")
    return ValuationSignal(pct, label, median_deviation, n, note)


# ---------------- 历史基线读写 (原子写, 与项目其它 JSON 一致) ----------------

_LABEL_ICON = {
    "极便宜": "🟢", "便宜": "🟢", "中性": "⚪",
    "偏贵": "🔴", "极贵": "🔴", "历史不足": "⚪",
}


def _quarter_of(day: str | None) -> str | None:
    """``'2026-08-29'`` → ``'2026Q3'``; 无日期返回 None。"""
    if not day:
        return None
    try:
        d = date.fromisoformat(str(day)[:10])
    except ValueError:
        return None
    return f"{d.year}Q{(d.month - 1) // 3 + 1}"


def baseline_medians(
    history: Sequence[Any],
    *,
    exclude_date: str | None = None,
) -> list[float]:
    """算分位用的历史中位偏差序列 —— **每季度只取一条**。

    基线混了两种采样频率: 前 18 期是季度末 (相邻间隔中位 91 天, 2022-06~2026-06),
    而全量重算每成功一次就追加一条, 于是又冒出日频点 (实测 2026-08-22 / 08-23 / 08-29,
    间隔 1 天和 6 天) —— 即"哪天点了刷新重算"。

    **不去重会让分位变味**。分位的全部价值在于跨完整牛熊周期比较 (中位偏差摆幅
    +0.4%~+21.6%), 而日频点按**天数**给最近这段行情加权: 按每天开一次 GUI 算, 一年后
    基线是 18 个季度点 + 约 244 个日点, **93% 的比较对象来自最近 12 个月**。

    **去重放在读侧, 不放在 append_history**: 写侧去重会销毁真实观测且不可逆, 而这里
    的判据 (季度桶 / 代表怎么选) 还会调 —— 与 ``data/watchlist_daily/`` 只追加不删的
    既定态度一致。

    两条规则:

    · **桶内取最晚一条**。历史 18 期明显锚季末, 取最晚就是"能拿到的最接近季末的观测",
      用户不必记得在季末那天点刷新。代价要认: 季内任意日 ≠ 季末, 实测 2026Q3 七天内
      摆了 2.84pp。
    · **先分桶, 再整桶剔掉当期所在的那个季度**。反过来 (先剔当期再分桶) 会让当季剩下
      的点顶上来当代表 —— 那是拿今天跟六天前的自己比, 而"当季"根本还不是历史。
      剔当期本身是必需的: 全量重算先 ``append_history`` 再渲染横幅, 不剔就是在含自己
      的集合里排位, ``arr <= value`` 自己必然命中。

    裸 float 序列没有日期, 既分不了桶也剔不了, 原样返回 (兼容旧调用)。
    """
    latest: dict[str, ValuationSnapshot] = {}
    plain: list[float] = []
    loose: list[float] = []          # 有快照但没日期 —— 分不了桶, 各自成一条
    for item in history:
        if not isinstance(item, ValuationSnapshot):
            if item is not None:
                plain.append(float(item))
            continue
        quarter = _quarter_of(item.date)
        if quarter is None:
            loose.append(float(item.median_deviation))
            continue
        prev = latest.get(quarter)
        if prev is None or (item.date or "") > (prev.date or ""):
            latest[quarter] = item
    if plain:
        return plain
    drop = _quarter_of(exclude_date)
    return sorted(
        [s.median_deviation for q, s in latest.items() if q != drop] + loose)

def caliber_note(
    history: Sequence[Any],
    current: str = CURRENT_CALIBER,
    *,
    verbose: bool = True,
) -> str:
    """历史序列跨口径时返回断点说明; 全同口径返回空串。

    只接受 :class:`ValuationSnapshot` 序列 (裸 float 无口径信息, 返回空串)。

    ``verbose=False`` 压成**一行**给 GUI 悬浮用。完整版会把 bug 修了什么、补回多少只、
    中位偏差移了多少全写出来 —— 那是 ``cb-valuation`` 这种报告该有的样子, 但在悬浮里
    它占了 331 字里的 136 字 (41%), 而读者拿这些做不了任何决定。悬浮只需要回答
    "这个分位能不能当真": 不能完全当真, 因为基线跨了两种池口径。
    """
    counts: dict[str, int] = {}
    for snap in history:
        if isinstance(snap, ValuationSnapshot):
            counts[snap.caliber] = counts.get(snap.caliber, 0) + 1
    if not counts:
        return ""
    if set(counts) | {current} == {current}:
        return ""
    change = CALIBER_CHANGES.get(current)
    if not verbose:
        # 口径种数 = counts 里出现过的 ∪ 当期。**不是 len(counts)+1** ——
        # 当期口径的快照本来就在 history 里 (实测 counts = {v1: 18, v2: 3}), 加一会多报一种。
        n_cal = len(set(counts) | {current})
        since = f", {change['since']} 起换口径" if change else ""
        return f"⚠️ 分位基线跨 {n_cal} 种主池口径{since}, 断点前后不严格可比。"
    detail = ", ".join(f"{cal} {n} 期" for cal, n in sorted(counts.items()))
    head = f"[口径] 历史序列跨口径合并计算分位 (历史 {detail}; 当期 {current})。"
    if not change:
        return head
    return (f"{head}\n{current} 自 {change['since']} 起: {change['summary']}; "
            f"{change['impact']}。分位仅供参考, 断点前后不严格可比。")


def valuation_banner(
    rows: Sequence[dict[str, Any]],
    history: Sequence[Any],
    **snapshot_kwargs: Any,
) -> tuple[str, str]:
    """供 GUI 用: 由已定价结果 + 历史序列, 返回 (单行横幅, 悬浮详情)。

    *history* 可以是 :class:`ValuationSnapshot` 序列 (推荐, 详情里会附口径断点说明),
    也可以是裸中位偏差 float 序列 (兼容旧调用, 无口径信息)。
    无可用数据时返回 ("", "")。横幅形如
    ``🔴 市场估值 偏贵 · 中位偏差 +13.8% · 历史分位 78%``。
    """
    try:
        snap = compute_snapshot(rows, **snapshot_kwargs)
    except ValueError:
        return "", ""
    sig = classify(snap.median_deviation,
                   baseline_medians(history, exclude_date=snap.date))
    icon = _LABEL_ICON.get(sig.label, "⚪")
    pct = "" if not np.isfinite(sig.percentile) else f" · 历史分位 {sig.percentile:.0f}%"
    banner = (f"{icon} 市场估值 {sig.label} · 中位偏差 "
              f"{snap.median_deviation*100:+.1f}%{pct}")
    return banner, _tooltip_detail(snap, sig, history)


def _tooltip_detail(
    snap: ValuationSnapshot,
    sig: ValuationSignal,
    history: Sequence[Any],
) -> str:
    """GUI 悬浮的详情 —— **不复用** ``str(sig)``。

    ``str(sig)`` 与 ``sig.note`` 是给 ``cb-valuation`` 那份终端报告写的, 直接拿来当
    悬浮有四个毛病 (实测原文 331 字 / 6 行):

    ① **同一个数说三遍**: 横幅已经写了「中位偏差 +21.2% · 历史分位 95%」, 详情第一行
       和第二行又各重复一遍。悬浮是用来补充横幅的, 不是复述它。
    ② **「样本」一词两种含义且相邻两行**: 「样本 284 只」是**个券数**,
       「样本 21」是**历史期数** —— 挨着放, 读者只会以为其中一个写错了。
    ③ **给操作建议**: 「强烈利于减仓」。这是研究工作台, README 通篇写着"不是投资建议";
       而且那句话没有主语 —— 谁减仓、减什么、减多少, 一个都没说。
    ④ **口径断点写成 changelog**: 修了什么 bug、补回多少只、中位偏差移了多少,
       占 136/331 字 (41%)。读者拿它做不了决定, 他只需要知道"这个分位能不能当真"。

    所以这里按 AGENTS 对悬浮的要求重写: **说清怎么读**, 不复述数值, 不写实现细节。
    数值本身一个都没重算 —— 全部取自 ``snap`` / ``sig``, 单一事实源不变。
    """
    lines = [
        "中位偏差 = 全市场「市价 ÷ 理论价 − 1」的中位数; 越正 = 转债整体越贵。",
        f"当期主池 {snap.n} 只, 其中 {snap.pct_overvalued*100:.0f}% 市价高于模型价。",
    ]
    if np.isfinite(sig.percentile):
        lines.append(
            f"「历史分位」是拿它跟过去 {sig.n_history} 个季度比 —— "
            f"不是「多少只贵」, 而是「比历史上多少时候贵」。")
        lines.append(
            "经验上它与中证转债指数下一季收益负相关 ≈−0.52 "
            "(便宜组 +2.8% vs 贵组 0%)。")
    else:
        # 历史不足那一档: 原文写"请先用 --record 积累基线", 那是 CLI 的开关,
        # 用户在这个页面上找不到它 (与 WATCH_REFRESH_LABEL 那条同源)。
        lines.append(
            f"历史基线只有 {sig.n_history} 个季度 (需 ≥8), 分位信号不可靠 —— "
            "每次全量重算会自动累积, 同季度只留最新一条。")
    lines.append("只说转债大类贵不贵: 不是个券信号, 也不是操作建议。")
    note = caliber_note(history, snap.caliber, verbose=False)
    if note:
        lines.append(note)
    return "\n".join(lines)


def load_history(path: Path) -> list[ValuationSnapshot]:
    """读历史基线 (按日期升序)。文件不存在返回空。"""
    if not Path(path).exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    records = payload.get("records", payload) if isinstance(payload, dict) else payload
    out: list[ValuationSnapshot] = []
    for rec in records:
        out.append(ValuationSnapshot(
            date=rec.get("date"), n=int(rec.get("n", 0)),
            median_deviation=float(rec["median_deviation"]),
            mean_deviation=float(rec.get("mean_deviation", rec["median_deviation"])),
            pct_overvalued=float(rec.get("pct_overvalued", float("nan"))),
            p25=float(rec.get("p25", float("nan"))),
            p75=float(rec.get("p75", float("nan"))),
            # 旧基线没有这个字段, 一律归入 v1 口径
            caliber=str(rec.get("caliber") or CALIBER_V1),
        ))
    out.sort(key=lambda s: (s.date is None, s.date))
    return out


def save_history(path: Path, snapshots: Sequence[ValuationSnapshot]) -> Path:
    """原子写历史基线 (先 .tmp 再 rename)。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "records": [s.to_record() for s in
                    sorted(snapshots, key=lambda s: (s.date is None, s.date))],
    }
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    tmp.replace(path)
    return path


def append_history(path: Path, snapshot: ValuationSnapshot) -> Path:
    """把一条快照并入历史基线; 同日期则覆盖, 避免重复记录。

    **这是无闸的底层写入**。日常写入一律走 :func:`record_snapshot` —— 它带覆盖率闸。
    """
    history = [s for s in load_history(path)
               if not (snapshot.date is not None and s.date == snapshot.date)]
    history.append(snapshot)
    return save_history(path, history)


def baseline_refusal_reason(
    rows: Sequence[dict[str, Any]],
    *,
    min_coverage: float = MIN_BASELINE_COVERAGE,
) -> str | None:
    """这批结果能不能记进历史基线: 能则 ``None``, 不能则返回**不记的原因**。

    判据走 :func:`snapshot_coverage`, 与 :func:`compute_snapshot` 逐条同口径。
    """
    usable, total = snapshot_coverage(rows or [])
    if not usable:
        # 一条能用的都没有 —— 与"覆盖率低"分开说: 前者多半是行情整条挂了,
        # 后者是部分失败。``total == 0`` 也走这一档 (空批次不该被判成"覆盖率正常")。
        return f"没有可用的定价结果 (0/{total}), 未记入估值基线"
    if total and usable < total * min_coverage:
        return (f"定价覆盖 {usable}/{total} ({usable / total:.0%} < "
                f"{min_coverage:.0%}), 未记入估值基线")
    return None


def record_snapshot(
    path: Path,
    rows: Sequence[dict[str, Any]],
    *,
    snapshot: ValuationSnapshot | None = None,
    min_coverage: float = MIN_BASELINE_COVERAGE,
    force: bool = False,
) -> str | None:
    """带覆盖率闸地把当期快照并入历史基线。记了返回 ``None``, 没记返回**不记的原因**。

    **闸必须在这一层, 不能只长在某一个调用方身上。** 它此前只写在
    ``gui/tabs/batch._record_valuation_history`` 里, 而 ``cb-valuation --record``
    读同一份 ``batch_pricing_cache.json`` 却是无条件 ``append_history`` ——
    于是 GUI 拒记的那份产物就留在盘上等着 CLI 来记 (``_batch_worker`` 刻意在部分失败时
    照样写主缓存), 而 README 的日常流第 ⑦ 步正是那条命令。
    实测按上市日切的系统性失败能造出 +48.96% vs 真实 +21.25% 的假快照, 偏离 27.7pp,
    超过整个历史摆幅 21.2pp; 而 ``baseline_medians`` 取桶内最晚一条, 它还会当上该季度的代表。

    ``force=True`` 跳过闸 (CLI 的 ``--force``): 用户明知覆盖率不足仍要记时的显式出口。

    返回值分两类, 由 :func:`is_coverage_refusal` 区分 —— 调用方**必须**分开处置:
    覆盖率拒记是"数据不够好, 可以 ``--force``", 而写盘失败是"基础设施坏了, ``--force``
    一点用都没有" (它只跳过闸, ``append_history`` 照样在同一个地方失败)。
    两者共用一句"确要记入请加 --force"就是给用户一个注定无效的动作。
    """
    if not force:
        reason = baseline_refusal_reason(rows, min_coverage=min_coverage)
        if reason:
            return reason
    try:
        snap = snapshot if snapshot is not None else compute_snapshot(rows or [])
        append_history(path, snap)
        return None
    except Exception as exc:
        logger.debug("估值历史记录失败 (忽略)", exc_info=True)
        # 带上异常原文: CLI 不配置 logging, debug 那条实际上无处可看
        return f"{_WRITE_FAILURE_PREFIX}: {exc!r}"


_WRITE_FAILURE_PREFIX = "估值基线写入失败"


def is_coverage_refusal(reason: str | None) -> bool:
    """``record_snapshot`` 的返回值是不是"覆盖率闸拒记" (而不是写盘失败)。"""
    return bool(reason) and not reason.startswith(_WRITE_FAILURE_PREFIX)
