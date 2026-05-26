"""可转债公告事件层.

事件层承接公告标题/原文解析后的结构化结果, 与 cb_data 的半静态条款解耦。
它主要服务两件事:
  1. 主池公开交易筛选与复核提示: 强赎、摘牌、停牌、正股风险等
  2. 模型参数修正: 下修/不下修事件影响下修博弈
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from collections.abc import Iterable, Sequence
from typing import Any

from .data_providers import BondTerms, _add_months, to_date
from .paths import data_path


EVENT_TYPES = (
    "down_reset_proposed",
    "down_reset_approved",
    "down_reset_rejected",
    "down_reset_trigger_notice",
    "conversion_price_adjusted",
    "balance_change",
    "call_redemption",
    "call_no_redemption",
    "putback",
    "conversion_suspension",
    "conversion_resume",
    "rating_change",
    "delisting",
    "suspension",
    "underlying_suspension",
    "underlying_st_risk",
    "underlying_st_clear",
    "unknown",
)

# 临时停牌类事件的默认 TTL: 公告未明示截止日期时, 按 event_date + N 天作为过期日,
# 避免单日临停永久污染 cb_data 状态字段。窗口选 5 个自然日 ≈ 3-4 个交易日,
# 真正的长停 (重组/退市) 一般会有明确日期或后续公告续期。
_TRANSIENT_EVENT_TTL_DAYS = 5
_TRANSIENT_EVENT_TYPES = frozenset({"suspension", "underlying_suspension"})
# 临停事件过期后, 还要再观察一段时间才主动清空 cb_data 上的状态字段。
# 这个 grace 是为了避免误伤 admission_status 层 (Wind 直刷) 同步到的实时停牌:
# 流程上 admission_status 先跑、apply_events 后跑, 若上一轮临停事件刚过期,
# 当天又被 Wind 标停, 没 grace 就会被旧事件误擦。30 天足够覆盖一次完整刷新周期。
_TRANSIENT_CLEAR_GRACE_DAYS = 30


def project_events_path() -> Path:
    return data_path("cb_events.json", seed=True)


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
    event_price: float | None = None

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
    event_price = None

    if body and event_type in {"down_reset_rejected", "call_no_redemption"}:
        commitment = parse_commitment_period(body, event_type=event_type)
        if commitment:
            effective_start = commitment["start"]
            effective_end = commitment["end"]
            commitment_months = commitment["months"]
    elif body and event_type == "call_redemption":
        redemption_dates = parse_call_redemption_dates(body)
        if redemption_dates.get("last_trading_date"):
            effective_start = redemption_dates["last_trading_date"]
        if redemption_dates.get("redemption_date"):
            effective_end = redemption_dates["redemption_date"]
        elif redemption_dates.get("delisting_date"):
            effective_end = redemption_dates["delisting_date"]
        if redemption_dates.get("redemption_price") is not None:
            event_price = float(redemption_dates["redemption_price"])
    elif body and event_type == "putback":
        putback = parse_putback_terms(body)
        if putback.get("start"):
            effective_start = putback["start"]
        if putback.get("end"):
            effective_end = putback["end"]
        if putback.get("price") is not None:
            event_price = float(putback["price"])
    elif body and event_type in {"conversion_suspension", "conversion_resume"}:
        suspension = parse_conversion_suspension_terms(body)
        if suspension.get("start"):
            effective_start = suspension["start"]
        if suspension.get("end"):
            effective_end = suspension["end"]
    elif body and event_type in {"down_reset_proposed", "down_reset_approved"}:
        # 下修提议/通过公告: 抽取下修后新转股价 (元/股) 填入 event_price,
        # 供定价层把"已公告"节点的目标 K 用真实公告值而非估算下限。
        new_price = parse_down_reset_new_price(body)
        if new_price is not None:
            event_price = float(new_price)

    # 临停类事件没有明确截止日期时, 给一个保守 TTL, 防止永久污染状态字段
    if effective_end is None and event_type in _TRANSIENT_EVENT_TYPES:
        effective_end = event_date + timedelta(days=_TRANSIENT_EVENT_TTL_DAYS)

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
        event_price=event_price,
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


def parse_call_redemption_dates(text: str) -> dict[str, date | float | None]:
    """从强赎公告正文中解析关键日期.

    返回 ``last_trading_date`` / ``redemption_date`` / ``delisting_date``。
    命中不到时字段为 None。只使用有明确标签的日期, 避免把观察期误当执行日。
    """
    if not text:
        return {
            "last_trading_date": None,
            "redemption_date": None,
            "delisting_date": None,
            "redemption_price": None,
        }
    t = re.sub(r"\s+", "", text.replace("（", "(").replace("）", ")"))
    return {
        "last_trading_date": _extract_labeled_date(
            t,
            (
                r"(?:最后交易日|停止交易日|最后一个交易日)(?:为|是|:|：)?",
            ),
            negative_after=r"提示|安排|详见",
        ),
        "redemption_date": _extract_labeled_date(
            t,
            (
                r"(?:赎回登记日|赎回日|提前赎回日)(?:为|是|:|：)?",
            ),
        ),
        "delisting_date": _extract_labeled_date(
            t,
            (
                r"(?:摘牌日|摘牌日期)(?:为|是|:|：)?",
            ),
        ),
        "redemption_price": _extract_labeled_price(
            t,
            (
                r"(?:赎回价格|提前赎回价格|本次赎回价格)(?:为|是|:|：)?",
            ),
        ),
    }


def parse_putback_terms(text: str) -> dict[str, date | float | None]:
    """从回售公告正文中解析申报期和回售价格.

    返回 ``start`` / ``end`` / ``price``。只接受明确锚定在回售申报期、
    回售期、回售价格附近的文本, 避免把触发观察期误当成行权窗口。
    """
    if not text:
        return {"start": None, "end": None, "price": None}
    t = re.sub(r"\s+", "", text.replace("（", "(").replace("）", ")"))
    range_re = (
        r"(\d{4})年(\d{1,2})月(\d{1,2})日"
        r"(?:至|到|-)"
        r"(\d{4})年(\d{1,2})月(\d{1,2})日"
    )
    start = None
    end = None
    for m in re.finditer(range_re, t):
        head = t[max(0, m.start() - 40): m.start()]
        if not re.search(r"回售(?:申报)?期|申报期间|回售登记期", head):
            continue
        start = _safe_date(*m.groups()[:3])
        end = _safe_date(*m.groups()[3:])
        if start and end:
            break
    price = _extract_labeled_price(
        t,
        (
            r"回售价格(?:为|是|:|：)?",
            r"回售申报价格(?:为|是|:|：)?",
        ),
    )
    return {"start": start, "end": end, "price": price}


def parse_conversion_suspension_terms(text: str) -> dict[str, date | None]:
    """从暂停/恢复转股公告正文中解析暂停转股窗口."""
    if not text:
        return {"start": None, "end": None}
    t = re.sub(r"\s+", "", text.replace("（", "(").replace("）", ")"))
    date_re = r"(\d{4})年(\d{1,2})月(\d{1,2})日"
    start = _extract_labeled_date(
        t,
        (
            r"(?:暂停转股起始日|停止转股起始日|暂停转股开始日)(?:为|是|:|：)?",
            r"(?:自|从)",
        ),
        negative_after=r"恢复|开始恢复",
    )
    end = _extract_labeled_date(
        t,
        (
            r"(?:恢复转股日|恢复转股起始日|恢复转股开始日)(?:为|是|:|：)?",
            r"(?:至|截至)",
        ),
    )
    resume_after = re.search(date_re + r"(?:起|开始)?(?:恢复|开始恢复)转股", t)
    if resume_after:
        end = _safe_date(*resume_after.groups()[-3:]) or end
    range_re = (
        date_re +
        r"(?:至|到|-)"
        r"(\d{4})年(\d{1,2})月(\d{1,2})日"
    )
    if start is None or end is None:
        for m in re.finditer(range_re, t):
            head = t[max(0, m.start() - 40): m.start()]
            tail = t[m.end(): m.end() + 40]
            if not re.search(r"暂停转股|停止转股|转股暂停", head + tail):
                continue
            start = start or _safe_date(*m.groups()[:3])
            end = end or _safe_date(*m.groups()[3:])
            break
    return {"start": start, "end": end}


# 下修公告里的新转股价 (元/股)。提议公告给"拟修正至"价, 通过公告给"修正后"价。
# 与 cb_event_sync.parse_conversion_price_adjustment (用于生成 K patch) 区别:
# 这里只取新价标量, 用于填 CBEvent.event_price, 覆盖提议/通过两类措辞。
# 容忍金额前的货币词 ("为人民币6.20元/股")
_CUR = r"(?:人民币|RMB|¥)?"
_RE_DR_NEW_PRICE_PAIR = re.compile(
    r"转股价格.{0,20}?由(?:原来的|原)?" + _CUR + r"[0-9]+(?:\.[0-9]+)?元/股.{0,20}?"
    r"(?:向下修正|修正|调整)(?:为|至)" + _CUR + r"([0-9]+(?:\.[0-9]+)?)元/股"
)
_RE_DR_NEW_PRICE_SINGLE = (
    re.compile(r"(?:向下修正后|本次向下修正后|修正后|下修后).{0,16}?转股价格(?:为|是|:|：)?" + _CUR + r"([0-9]+(?:\.[0-9]+)?)元/股"),
    re.compile(r"(?:提议|拟).{0,30}?(?:向下修正|修正|调整).{0,16}?(?:为|至)" + _CUR + r"([0-9]+(?:\.[0-9]+)?)元/股"),
    re.compile(r"(?:向下修正|下修)(?:转股价格)?(?:为|至)" + _CUR + r"([0-9]+(?:\.[0-9]+)?)元/股"),
)


def parse_down_reset_new_price(text: str | None) -> float | None:
    """从下修提议/通过公告正文中解析下修后的新转股价 (元/股). 解析不到返回 None.

    优先匹配"由 A 元/股 修正为 B 元/股"取 B; 否则匹配"修正后转股价格为 X 元/股"
    或"提议/拟向下修正为 X 元/股"等单值措辞。
    """
    if not text:
        return None
    t = re.sub(r"\s+", "", str(text).replace("（", "(").replace("）", ")"))
    m = _RE_DR_NEW_PRICE_PAIR.search(t)
    if not m:
        for pattern in _RE_DR_NEW_PRICE_SINGLE:
            m = pattern.search(t)
            if m:
                break
    if m:
        try:
            price = float(m.group(1))
        except (TypeError, ValueError):
            price = None
        if price is not None and price > 0:
            return price
    # 提议措辞 ("向下修正至X") 之外, 已通过公告多用"调整前/调整后转股价格"或
    # "由...调整为..."的表格式措辞 — 复用通用调整解析器覆盖 (避免正则重复漂移)。
    # 懒导入打破 cb_events ←→ cb_event_sync 的模块级循环。
    try:
        from .cb_event_sync import parse_conversion_price_adjustment
        adj = parse_conversion_price_adjustment(text)
    except Exception:
        adj = None
    if adj and adj.get("new_price"):
        try:
            price = float(adj["new_price"])
        except (TypeError, ValueError):
            return None
        return price if price > 0 else None
    return None


def _extract_labeled_date(
    text: str,
    prefixes: tuple[str, ...],
    *,
    negative_after: str | None = None,
) -> date | None:
    date_re = r"(\d{4})年(\d{1,2})月(\d{1,2})日"
    for prefix in prefixes:
        for m in re.finditer(prefix + r".{0,20}?" + date_re, text):
            if negative_after and re.search(negative_after, text[m.end(): m.end() + 12]):
                continue
            parsed = _safe_date(*m.groups()[-3:])
            if parsed:
                return parsed
    return None


def _extract_labeled_price(text: str, prefixes: tuple[str, ...]) -> float | None:
    for prefix in prefixes:
        match = re.search(prefix + r".{0,16}?([0-9]+(?:\.[0-9]+)?)元/张", text)
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                return None
    return None


def _safe_date(y, m, d) -> date | None:
    try:
        return date(int(y), int(m), int(d))
    except (ValueError, TypeError):
        return None


def is_down_reset_trigger_notice_title(title: str) -> bool:
    """是否为"可能/预计触发下修条件"提示公告.

    这类公告只是进入观察/提示状态, 不是董事会提议, 更不是已经下修。
    """
    text = re.sub(r"\s+", "", str(title or "").upper())
    if not text or "触发" not in text:
        return False
    if "向下修正" not in text and "下修" not in text:
        return False
    if re.search(r"不.{0,6}触发", text):
        return False
    if re.search(r"董事会.{0,16}(?:提议|建议)|提议.{0,20}(?:向下修正|下修)", text):
        return False
    if re.search(r"审议通过|修正.{0,12}实施|实施.{0,12}修正", text):
        return False
    if "提示" not in text and not re.search(r"可能|预计|即将|将要", text):
        return False
    return bool(re.search(r"(可能|预计|即将|将要)?.{0,12}触发.{0,20}(向下修正|下修)", text))


def classify_announcement_title(title: str) -> str:
    text = str(title or "").upper()
    if re.search(r"不(提前)?赎回|不强赎|暂不(提前)?赎回|不行使.{0,30}(?:提前赎回|赎回权利?)", text):
        return "call_no_redemption"
    if re.search(r"赎回实施|实施赎回|强制赎回|提前赎回|赎回暨摘牌|赎回登记日", text):
        return "call_redemption"
    if re.search(r"不向下修正|不下修|暂不向下修正|不修正.*转股", text):
        return "down_reset_rejected"
    if is_down_reset_trigger_notice_title(text):
        return "down_reset_trigger_notice"
    if re.search(r"董事会.*向下修正|提议.*向下修正|提议下修", text):
        return "down_reset_proposed"
    if re.search(r"向下修正.*转股价格|修正.*转股价格.*实施", text):
        return "down_reset_approved"
    if re.search(r"转股价格调整|调整.*转股价格", text):
        return "conversion_price_adjusted"
    if re.search(r"转股结果|回售结果|赎回结果|未转股余额|债券余额|剩余可转债余额", text):
        return "balance_change"
    if re.search(r"恢复.{0,8}转股|转股.{0,4}恢复", text):
        return "conversion_resume"
    if re.search(r"暂停.{0,8}转股|停止.{0,8}转股|转股.{0,4}暂停", text) and "停牌" not in text:
        return "conversion_suspension"
    if "回售" in text:
        return "putback"
    if "评级" in text:
        return "rating_change"
    if re.search(r"摘牌|最后交易日", text):
        return "delisting"
    # ── 正股风险 (反向事件优先, 避免被 ST risk 误判) ──
    if _is_underlying_st_clear(text):
        return "underlying_st_clear"
    # ── 正股风险 ── 必须在转债停牌判断之前, 防止 ST 标题中"停牌"被误判
    if _is_underlying_st_risk(text):
        return "underlying_st_risk"
    if "停牌" in text:
        # "可转债停牌" 也命中 "转债.*停牌"  (.*  匹配空串), 不必单列
        if re.search(r"转债.*停牌|停牌.*转债", text):
            return "suspension"      # 转债自身停牌
        # 仅在明确出现正股/股票/A股等线索时归为正股停牌, 否则保守留 unknown,
        # 避免把券商笼统的"关于临时停牌的公告"误挂到正股侧。
        if re.search(r"股票|正股|A股|公司股", text):
            return "underlying_suspension"
        return "unknown"
    return "unknown"


def _is_underlying_st_risk(text: str) -> bool:
    """正股 ST / 退市风险警示公告.

    排除"撤销风险警示""申请撤销 *ST"等利好公告, 只保留风险确认型。
    """
    if re.search(r"撤销.*(?:风险警示|\*ST)|申请撤销.*ST", text):
        return False
    if re.search(r"实施.*退市风险|被实行退市风险|退市风险警示", text):
        return True
    if re.search(r"实施\*ST|被实施\*ST|实施其他风险警示|被实行其他风险警示", text):
        return True
    if re.search(r"股票.*被(?:实行|实施).{0,6}(?:风险警示|ST)", text):
        return True
    return False


def _is_underlying_st_clear(text: str) -> bool:
    """正股撤销风险警示 / *ST 利好公告.

    用于反向清除 ``underlying_status``, 与 ``_is_underlying_st_risk`` 互斥。
    """
    return bool(re.search(r"撤销.*(?:退市)?风险警示|撤销.*\*?ST|申请撤销.*ST", text))


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
        if latest_call.effective_start:
            updates["last_trading_date"] = latest_call.effective_start
        if latest_call.effective_end:
            updates["call_redemption_date"] = latest_call.effective_end
            if "摘牌" in latest_call.raw_title:
                updates["delisting_date"] = latest_call.effective_end
        if latest_call.event_price is not None:
            updates["call_redemption_price"] = latest_call.event_price
    latest_no_call = _latest_event(active, "call_no_redemption")
    if latest_no_call and (latest_call is None or latest_no_call.event_date >= latest_call.event_date):
        updates["call_status"] = latest_no_call.parsed_status or "不强赎"
        if latest_no_call.effective_end:
            updates["call_no_redemption_until"] = latest_no_call.effective_end

    latest_delist = _latest_event(active, "delisting")
    if latest_delist:
        updates["delisting_date"] = latest_delist.effective_end or latest_delist.effective_start

    latest_putback = _latest_event(active, "putback")
    if latest_putback:
        if latest_putback.effective_start:
            updates["putback_start_date"] = latest_putback.effective_start
        if latest_putback.effective_end:
            updates["putback_end_date"] = latest_putback.effective_end
        if latest_putback.event_price is not None:
            updates["putback_price"] = latest_putback.event_price

    latest_conv_resume = _latest_event(active, "conversion_resume")
    latest_conv_susp = _latest_event(active, "conversion_suspension")
    if latest_conv_resume and (
        latest_conv_susp is None or latest_conv_resume.event_date >= latest_conv_susp.event_date
    ):
        updates["conversion_suspension_status"] = latest_conv_resume.parsed_status or "恢复转股"
        if latest_conv_resume.effective_start:
            updates["conversion_suspension_end_date"] = latest_conv_resume.effective_start
    elif latest_conv_susp:
        if latest_conv_susp.effective_start:
            updates["conversion_suspension_start_date"] = latest_conv_susp.effective_start
        if latest_conv_susp.effective_end:
            updates["conversion_suspension_end_date"] = latest_conv_susp.effective_end
        if (
            latest_conv_susp.effective_end is None
            or latest_conv_susp.effective_end >= val_date
        ):
            updates["conversion_suspension_status"] = latest_conv_susp.parsed_status or "暂停转股"

    # 临停类事件: 仅在 effective_end 仍在窗口内时才标记停牌;
    # 过期超过 _TRANSIENT_CLEAR_GRACE_DAYS 才主动清空, 给 admission_status (Wind 直刷)
    # 留写入窗口, 避免刚过期的旧事件擦掉当天 admission 同步到的真实"停牌"。
    latest_suspension = _latest_event(active, "suspension")
    if latest_suspension:
        if _transient_still_active(latest_suspension, val_date):
            updates["suspension_status"] = latest_suspension.parsed_status or "停牌"
        elif (
            terms.suspension_status is not None
            and _transient_long_expired(latest_suspension, val_date)
        ):
            updates["suspension_status"] = None
    latest_underlying_susp = _latest_event(active, "underlying_suspension")
    if latest_underlying_susp:
        if _transient_still_active(latest_underlying_susp, val_date):
            updates["underlying_trade_status"] = "停牌"
        elif (
            terms.underlying_trade_status is not None
            and _transient_long_expired(latest_underlying_susp, val_date)
        ):
            updates["underlying_trade_status"] = None
    # ST 状态: 撤销公告日期晚于风险公告时, 显式清空 underlying_status
    latest_st = _latest_event(active, "underlying_st_risk")
    latest_st_clear = _latest_event(active, "underlying_st_clear")
    if latest_st_clear and (latest_st is None or latest_st_clear.event_date >= latest_st.event_date):
        if terms.underlying_status is not None:
            updates["underlying_status"] = None
    elif latest_st:
        updates["underlying_status"] = latest_st.parsed_status or "ST/退市风险"

    latest_down_rejected = _latest_event(active, "down_reset_rejected")
    if latest_down_rejected:
        updates["down_reset_block_until"] = _down_reset_block_until_from_event(
            latest_down_rejected,
            cooldown_months=int(down_reset_cooldown_months),
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
        if (
            e.event_type in {"down_reset_rejected", "down_reset_proposed", "down_reset_approved"}
            and not is_down_reset_trigger_notice_title(e.raw_title)
        )
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


def _transient_event_end(event: CBEvent) -> date:
    """临停事件的有效截止日 (缺失时按 event_date + TTL 兜底)."""
    return event.effective_end or (
        event.event_date + timedelta(days=_TRANSIENT_EVENT_TTL_DAYS)
    )


def _transient_still_active(event: CBEvent, val_date: date) -> bool:
    """判断临停类事件在 ``val_date`` 是否仍处于生效窗口."""
    return _transient_event_end(event) >= val_date


def _transient_long_expired(event: CBEvent, val_date: date) -> bool:
    """临停事件已过期超过 GRACE 天: 视作真的失效, 可清空状态字段.

    刚过期不清, 是为了给 admission_status 层留窗口写入实时 Wind 状态,
    避免上一轮事件刚过期就把当天 admission 同步到的真实"停牌"擦掉。
    """
    return (val_date - _transient_event_end(event)).days > _TRANSIENT_CLEAR_GRACE_DAYS


def _event_status(event_type: str) -> str:
    return {
        "down_reset_proposed": "提议下修",
        "down_reset_approved": "已下修",
        "down_reset_rejected": "不下修",
        "down_reset_trigger_notice": "触发提示",
        "conversion_price_adjusted": "转股价调整",
        "balance_change": "余额变化",
        "call_redemption": "已公告强赎",
        "call_no_redemption": "不强赎",
        "putback": "回售",
        "conversion_suspension": "暂停转股",
        "conversion_resume": "恢复转股",
        "rating_change": "评级调整",
        "delisting": "临近摘牌",
        "suspension": "停牌",
        "underlying_suspension": "正股停牌",
        "underlying_st_risk": "ST/退市风险",
        "underlying_st_clear": "撤销ST",
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


def _down_reset_block_until_from_event(event: CBEvent, *, cooldown_months: int) -> date:
    if event.effective_end:
        return event.effective_end
    return _add_months(event.event_date, int(cooldown_months))


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
    event_price = row.get("event_price")
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
        event_price=float(event_price) if event_price is not None else None,
    )
