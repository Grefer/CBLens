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
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Sequence

import numpy as np

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
    devs: list[float] = []
    dates: list[str] = []
    for row in rows:
        if require_ok and status_key in row and row.get(status_key) != "ok":
            continue
        dv = _finite(row.get(deviation_key))
        if dv is None:
            continue
        devs.append(dv)
        vd = _coerce_date(row.get("valuation_date"))
        if vd:
            dates.append(vd)
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


def valuation_banner(
    rows: Sequence[dict[str, Any]],
    history_medians: Sequence[float],
    **snapshot_kwargs: Any,
) -> tuple[str, str]:
    """供 GUI 用: 由已定价结果 + 历史中位偏差序列, 返回 (单行横幅, 悬浮详情)。

    无可用数据时返回 ("", "")。横幅形如
    ``🔴 市场估值 偏贵 · 中位偏差 +13.8% · 历史分位 78%``。
    """
    try:
        snap = compute_snapshot(rows, **snapshot_kwargs)
    except ValueError:
        return "", ""
    sig = classify(snap.median_deviation, history_medians)
    icon = _LABEL_ICON.get(sig.label, "⚪")
    pct = "" if not np.isfinite(sig.percentile) else f" · 历史分位 {sig.percentile:.0f}%"
    banner = (f"{icon} 市场估值 {sig.label} · 中位偏差 "
              f"{snap.median_deviation*100:+.1f}%{pct}")
    detail = (f"全市场中位偏差 {snap.median_deviation*100:+.1f}% "
              f"(判高估 {snap.pct_overvalued*100:.0f}%, 样本 {snap.n} 只)\n{sig}")
    return banner, detail


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
    """把一条快照并入历史基线; 同日期则覆盖, 避免重复记录。"""
    history = [s for s in load_history(path)
               if not (snapshot.date is not None and s.date == snapshot.date)]
    history.append(snapshot)
    return save_history(path, history)
