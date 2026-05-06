"""可转债公告事件层.

事件层承接公告标题/原文解析后的结构化结果, 与 cb_data 的半静态条款解耦。
它主要服务两件事:
  1. 主池准入筛选: 强赎、摘牌、停牌、正股风险等
  2. 模型参数修正: 下修/不下修事件影响下修博弈
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from .data_providers import BondTerms, _add_months, to_date


EVENT_TYPES = (
    "down_reset_proposed",
    "down_reset_approved",
    "down_reset_rejected",
    "call_redemption",
    "call_no_redemption",
    "putback",
    "rating_change",
    "delisting",
    "suspension",
    "unknown",
)


def project_events_path() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "cb_events.json"


@dataclass(frozen=True)
class CBEvent:
    bond_code: str
    event_date: date
    event_type: str
    raw_title: str
    effective_start: date | None = None
    effective_end: date | None = None
    parsed_status: str | None = None
    source: str = "manual"
    url: str | None = None
    note: str | None = None
    commitment_months: int | None = None

    def key(self) -> tuple:
        return (
            self.bond_code,
            self.event_date.isoformat(),
            self.event_type,
            self.raw_title.strip(),
        )


class CBEventStore:
    """JSON 事件表, 文件结构为 ``{"_meta": {...}, "events": [...]}``."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else project_events_path()
        self._events: list[CBEvent] = []
        self._meta: dict = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            self._events = []
            self._meta = {}
            return
        with open(self.path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        self._meta = dict(payload.get("_meta", {}))
        self._events = [_event_from_json(row) for row in payload.get("events", [])]

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        meta = dict(self._meta)
        meta["updated_at"] = datetime.now().isoformat(timespec="seconds")
        payload = {
            "_meta": meta,
            "events": [_event_to_json(e) for e in sorted(self._events, key=_event_sort_key)],
        }
        tmp = self.path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
        tmp.replace(self.path)

    def list_events(
        self,
        bond_code: str | None = None,
        event_type: str | None = None,
        through_date: date | None = None,
    ) -> list[CBEvent]:
        events = list(self._events)
        if bond_code:
            events = [e for e in events if e.bond_code == bond_code]
        if event_type:
            events = [e for e in events if e.event_type == event_type]
        if through_date:
            events = [e for e in events if e.event_date <= through_date]
        return sorted(events, key=_event_sort_key)

    def add_many(self, events: Iterable[CBEvent]) -> int:
        existing = {e.key(): e for e in self._events}
        added = 0
        for event in events:
            if event.key() in existing:
                continue
            existing[event.key()] = event
            added += 1
        self._events = list(existing.values())
        if added:
            self._save()
        return added

    def mark_synced(self, bond_codes: Iterable[str], synced_at: datetime | None = None) -> None:
        """记录某些转债公告已完成同步, 即使本次没有新增事件也更新时间戳."""
        codes = sorted({str(code).strip().upper() for code in bond_codes if str(code).strip()})
        if not codes:
            return
        ts = (synced_at or datetime.now()).isoformat(timespec="seconds")
        by_code = dict(self._meta.get("synced_at_by_code", {}))
        for code in codes:
            by_code[code] = ts
        self._meta["last_sync_at"] = ts
        self._meta["synced_at_by_code"] = by_code
        self._save()


def parse_event_from_announcement(
    bond_code: str,
    title: str,
    event_date: date,
    *,
    source: str = "announcement",
    url: str | None = None,
    note: str | None = None,
    body: str | None = None,
) -> CBEvent | None:
    """根据公告标题解析事件. 不相关公告返回 None.

    可选传入 ``body`` (公告 PDF 抽取的纯文本); 若事件类型为不下修/不强赎,
    会进一步解析承诺期 (月数 + 起止日), 写入 ``effective_start/end`` 与
    ``commitment_months``。
    """
    clean_title = re.sub(r"\s+", "", str(title or ""))
    if not clean_title:
        return None
    event_type = classify_announcement_title(clean_title)
    if event_type == "unknown":
        return None
    dates = _extract_dates(clean_title)
    effective_start = dates[0] if dates else event_date
    effective_end = dates[-1] if len(dates) >= 2 else None
    commitment_months = None

    if body and event_type in {"down_reset_rejected", "call_no_redemption"}:
        commitment = parse_commitment_period(body, event_type=event_type)
        if commitment:
            effective_start = commitment["start"]
            effective_end = commitment["end"]
            commitment_months = commitment["months"]

    return CBEvent(
        bond_code=bond_code,
        event_date=event_date,
        event_type=event_type,
        raw_title=title,
        effective_start=effective_start,
        effective_end=effective_end,
        parsed_status=_event_status(event_type),
        source=source,
        url=url,
        note=note,
        commitment_months=commitment_months,
    )


_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
           "七": 7, "八": 8, "九": 9, "十": 10, "十二": 12}


def _cn_or_arabic_to_int(s: str) -> int | None:
    s = s.strip()
    if s.isdigit():
        return int(s)
    if s == "十":
        return 10
    if s.startswith("十"):
        return 10 + _CN_NUM.get(s[1:], 0)
    if s.endswith("十"):
        return _CN_NUM.get(s[:-1], 1) * 10
    if "十" in s:
        a, b = s.split("十", 1)
        return _CN_NUM.get(a, 1) * 10 + _CN_NUM.get(b, 0)
    return _CN_NUM.get(s)


_RE_COMMIT_DATE = r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"
_RE_COMMIT_RANGE = re.compile(
    _RE_COMMIT_DATE
    + r"(?:\s*[(（][^()（）]{0,40}[)）])?"   # 容忍中间括号注释 ("次一交易日"等)
    + r"\s*起?\s*至\s*"                      # 容忍 "起至"
    + _RE_COMMIT_DATE
)
_RE_COMMIT_MONTHS = re.compile(r"未来\s*([0-9一二三四五六七八九十]{1,3})\s*个月")
_RE_COMMIT_TAIL_DOWN = re.compile(r"(?:亦不(?:提出|再提出)|不再提出|公司均不行使).{0,30}向下修正")
_RE_COMMIT_TAIL_CALL = re.compile(r"(?:公司均不行使|亦不行使|不行使).{0,30}提前赎回")
_RE_COMMIT_DECISION_DOWN = re.compile(r"(?:决定本次|本次决定|董事会决定).{0,20}不向下修正")
_RE_COMMIT_DECISION_CALL = re.compile(r"(?:决定本次不行使|不行使|不提前赎回).{0,30}(?:提前赎回|赎回权利?)")


def parse_commitment_period(
    text: str,
    *,
    event_type: str = "down_reset_rejected",
) -> dict | None:
    """从公告正文中解析"未来 X 个月内 (Y 起至 Z)"承诺期.

    支持两类公告:
        - down_reset_rejected: 不向下修正承诺
        - call_no_redemption:  不提前赎回承诺

    返回 ``{"months": int, "start": date, "end": date, "strategy": str}`` 或 None.

    策略:
        A. 锚定 "未来 X 个月" 短语, 在其后窗口内找 "至" 日期范围 (覆盖 ~85%);
        B. 退化: 已被决定语句锚定的日期范围, 且其后出现承诺型措辞;
           严格要求决定句在前、承诺措辞在后, 避免命中触发观察窗。
    """
    if not text:
        return None
    t = text.replace("（", "(").replace("）", ")")
    t = re.sub(r"\s+", " ", t)

    # Strategy A: anchored on "未来 X 个月"
    for m in _RE_COMMIT_MONTHS.finditer(t):
        months = _cn_or_arabic_to_int(m.group(1))
        if months is None:
            continue
        window = t[m.start(): m.end() + 250]
        rng = _RE_COMMIT_RANGE.search(window)
        if not rng:
            continue
        start = _safe_date(*rng.groups()[:3])
        end = _safe_date(*rng.groups()[3:])
        if start and end and end > start:
            return {"months": months, "start": start, "end": end, "strategy": "A"}

    # Strategy B: decision-anchored; commitment language must follow
    tail_re = _RE_COMMIT_TAIL_DOWN if event_type == "down_reset_rejected" else _RE_COMMIT_TAIL_CALL
    decision_re = _RE_COMMIT_DECISION_DOWN if event_type == "down_reset_rejected" else _RE_COMMIT_DECISION_CALL

    for rng in _RE_COMMIT_RANGE.finditer(t):
        head = t[max(0, rng.start() - 120): rng.start()]
        tail = t[rng.end(): rng.end() + 200]
        if not decision_re.search(head):
            continue
        if not tail_re.search(tail):
            continue
        # 排除触发观察窗常见上下文 (出现这些词意味着是触发段, 不是承诺段)
        if re.search(r"已触发|低于.{0,15}(?:转股价|85%|70%)|三十个交易日中.{0,15}十五", head):
            continue
        start = _safe_date(*rng.groups()[:3])
        end = _safe_date(*rng.groups()[3:])
        if start and end and end > start:
            approx_months = round((end - start).days / 30)
            return {"months": approx_months, "start": start, "end": end, "strategy": "B"}

    return None


def _safe_date(y, m, d) -> date | None:
    try:
        return date(int(y), int(m), int(d))
    except (ValueError, TypeError):
        return None


def classify_announcement_title(title: str) -> str:
    text = str(title or "").upper()
    if re.search(r"不(提前)?赎回|不强赎|暂不(提前)?赎回", text):
        return "call_no_redemption"
    if re.search(r"赎回实施|实施赎回|强制赎回|提前赎回|赎回暨摘牌|赎回登记日", text):
        return "call_redemption"
    if re.search(r"不向下修正|不下修|暂不向下修正|不修正.*转股", text):
        return "down_reset_rejected"
    if re.search(r"董事会.*向下修正|提议.*向下修正|提议下修", text):
        return "down_reset_proposed"
    if re.search(r"向下修正.*转股价格|修正.*转股价格.*实施|转股价格调整", text):
        return "down_reset_approved"
    if "回售" in text:
        return "putback"
    if "评级" in text:
        return "rating_change"
    if re.search(r"摘牌|最后交易日", text):
        return "delisting"
    if "停牌" in text:
        return "suspension"
    return "unknown"


def apply_events_to_terms(
    bond_code: str,
    terms: BondTerms,
    events: Sequence[CBEvent],
    *,
    valuation_date: date | None = None,
    down_reset_cooldown_months: int = 6,
) -> BondTerms:
    """把事件层合并到 ``BondTerms`` 中, 供筛选和定价使用."""
    val_date = valuation_date or date.today()
    active = [e for e in events if e.bond_code == bond_code and e.event_date <= val_date]
    if not active:
        return terms

    updates: dict[str, Any] = {}
    latest_call = _latest_event(active, "call_redemption")
    if latest_call:
        updates["call_status"] = latest_call.parsed_status or "已公告强赎"
        updates["call_announce_date"] = latest_call.event_date
        if latest_call.effective_end:
            updates["call_redemption_date"] = latest_call.effective_end
    latest_no_call = _latest_event(active, "call_no_redemption")
    if latest_no_call and (latest_call is None or latest_no_call.event_date >= latest_call.event_date):
        updates["call_status"] = latest_no_call.parsed_status or "不强赎"
        if latest_no_call.effective_end:
            updates["call_no_redemption_until"] = latest_no_call.effective_end

    latest_delist = _latest_event(active, "delisting")
    if latest_delist:
        updates["delisting_date"] = latest_delist.effective_end or latest_delist.effective_start
    latest_suspension = _latest_event(active, "suspension")
    if latest_suspension:
        updates["suspension_status"] = latest_suspension.parsed_status or "停牌"

    latest_down_rejected = _latest_event(active, "down_reset_rejected")
    if latest_down_rejected and terms.down_reset_block_until is None:
        if latest_down_rejected.effective_end:
            updates["down_reset_block_until"] = latest_down_rejected.effective_end
        else:
            updates["down_reset_block_until"] = _add_months(
                latest_down_rejected.event_date,
                int(down_reset_cooldown_months),
            )
        updates["down_reset_note"] = latest_down_rejected.raw_title

    return replace(terms, **updates) if updates else terms


def events_for_down_reset(
    bond_code: str,
    *,
    store: CBEventStore | None = None,
    through_date: date | None = None,
) -> list[CBEvent]:
    event_store = store or default_event_store()
    return [
        e for e in event_store.list_events(bond_code=bond_code, through_date=through_date)
        if e.event_type in {"down_reset_rejected", "down_reset_proposed", "down_reset_approved"}
    ]


_default_event_store: CBEventStore | None = None


def default_event_store() -> CBEventStore:
    global _default_event_store
    if _default_event_store is None:
        _default_event_store = CBEventStore()
    return _default_event_store


def reload_default_event_store() -> CBEventStore:
    global _default_event_store
    _default_event_store = CBEventStore()
    return _default_event_store


def _event_status(event_type: str) -> str:
    return {
        "down_reset_proposed": "提议下修",
        "down_reset_approved": "已下修",
        "down_reset_rejected": "不下修",
        "call_redemption": "已公告强赎",
        "call_no_redemption": "不强赎",
        "putback": "回售",
        "rating_change": "评级调整",
        "delisting": "临近摘牌",
        "suspension": "停牌",
    }.get(event_type, event_type)


def _extract_dates(text: str) -> list[date]:
    out: list[date] = []
    patterns = [
        r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?",
    ]
    for pattern in patterns:
        for y, m, d in re.findall(pattern, text):
            try:
                out.append(date(int(y), int(m), int(d)))
            except ValueError:
                continue
    return sorted(set(out))


def _latest_event(events: Sequence[CBEvent], event_type: str) -> CBEvent | None:
    matched = [e for e in events if e.event_type == event_type]
    return max(matched, key=_event_sort_key) if matched else None


def _event_sort_key(event: CBEvent) -> tuple:
    return (event.event_date, event.bond_code, event.event_type, event.raw_title)


def _event_to_json(event: CBEvent) -> dict:
    row = asdict(event)
    for key in ("event_date", "effective_start", "effective_end"):
        if isinstance(row.get(key), date):
            row[key] = row[key].isoformat()
    return row


def _event_from_json(row: dict) -> CBEvent:
    months = row.get("commitment_months")
    return CBEvent(
        bond_code=str(row["bond_code"]),
        event_date=to_date(row["event_date"]),
        event_type=row.get("event_type") or "unknown",
        raw_title=row.get("raw_title") or "",
        effective_start=to_date(row.get("effective_start")),
        effective_end=to_date(row.get("effective_end")),
        parsed_status=row.get("parsed_status"),
        source=row.get("source") or "manual",
        url=row.get("url"),
        note=row.get("note"),
        commitment_months=int(months) if months is not None else None,
    )
