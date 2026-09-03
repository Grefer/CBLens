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
from .market_time import market_today


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

#: 暂停转股缺 ``effective_end`` 时的兜底时长, 从**暂停起始日**起算。
#:
#: 这个字段不能按"缺 end 就永久有效"处理: 实测全库 508 条 conversion_suspension 里
#: 150 条没有 end, 而它们让**主池 50/311 只 (16%)** 常年挂着「暂停转股」—— 距起始日
#: 中位 92 天、最长 812 天。上银转债一只就有五条 (2024-06/2024-11/2025-05/2025-09/
#: 2026-05), 每条 end 都是 None —— 有第二次就说明第一次结束了, 这个旗标可证伪。
#:
#: **为什么不能靠 end 自己**: 那个字段被回售期区间污染得很厉害 (AGENTS 记过宝莱转债
#: 「关于回售期间…暂停转股」解析出 start=2021-03-11 end=2026-09-03), 实测 358 条有
#: start+end 的时长中位 **1202 天**, 还有 74 条扎堆在 ~2000 天。
#:
#: **为什么锚 effective_start 而不是 event_date**: 那 150 条**全部**有 start, 而公告
#: 总是提前发 (公告日→起始日 中位 5 天)。锚公告日会把 TTL 提前烧掉一半。
#:
#: **10 天这个值不敏感**: 384/508 条标题是「权益分派」—— 那是**一日**停牌; 而现存
#: 最近的一条距今 49 天。阈值落在 10~49 的空档里, 取 8 或 20 结果一样。
_CONVERSION_SUSPENSION_TTL_DAYS = 10


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
        #: 上一次 add_many 原地升级了几条 (与新增分开报, 见 add_many 的说明)
        self.last_upgraded = 0
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
        """写入新事件, 并在同 key 的入参**解析得更全**时原地升级。

        返回值仍是"新增条数", 升级条数另经 :attr:`last_upgraded` 暴露 —— 改返回值的语义
        会让现有调用方把升级当成新增。

        **为什么必须能升级**: ``key()`` 只含 (代码, 日期, 类型, 标题), 不含任何解析字段。
        正文取不到时 (网络失败 / 扫描件 PDF / ``--no-pdf`` 快路径) 产生的降级事件因此会
        **永久占位** —— 修好解析器或带上 PDF 重跑一遍, 报告写 ``added: 0``, 库里一个字节
        没变。实测拿 ``data/announcement_text_cache`` 里 1368 份正文重放:
        **330 份**在带正文时能解析出严格更多的字段, 而 ``key()`` 逐位相同。
        存量库今天还算干净, 靠的是 ``cb-repair-*`` 那几条一次性回洗命令, 不是这条路。

        升级判据是**严格更全**: 字段数更多, 且原有的非空值一个都不丢。这样重跑一个更差
        的解析器 (或没带正文的那次) 不会把好数据擦掉。
        """
        existing = {e.key(): e for e in self._events}
        added = 0
        upgraded = 0
        for event in events:
            key = event.key()
            current = existing.get(key)
            if current is None:
                existing[key] = event
                added += 1
                continue
            if _is_strict_upgrade(current, event):
                existing[key] = event
                upgraded += 1
        self._events = list(existing.values())
        self.last_upgraded = upgraded
        if added or upgraded:
            self._save()
        return added

    def rewrite(self, transform, *, dry_run: bool = False) -> tuple[int, int]:
        """按 *transform* 逐条重写已有事件, 返回 ``(改写数, 删除数)``.

        *transform* 返回 None 表示删除该事件。用于修数据 (例如剔除标的串号误挂的公告);
        新增走 :meth:`add_many`。与 ``TermsPatchStore.rewrite`` 同形 —— 事件表和 patch 库
        往往要一起洗, 两边接口分叉会让回洗工具各写一套。
        """
        kept: list[CBEvent] = []
        changed = removed = 0
        for event in self._events:
            new_event = transform(event)
            if new_event is None:
                removed += 1
                continue
            if new_event != event:
                changed += 1
            kept.append(new_event)
        if not dry_run and (changed or removed):
            self._events = kept
            self._save()
        return changed, removed

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
        # **承诺期不可能在公告之前就结束**。正文常成段引用上一次的承诺
        # (「本公司曾承诺自 2024-01-20 起六个月内不下修…」), 而解析器抓的是第一段,
        # 于是一份 2026 年的公告解析出 2024 年的窗口。实测 21/540 条 down_reset_rejected
        # 与 7/338 条 call_no_redemption 是这个形状; 天能转债四份公告
        # (2025-08 / 2025-12 / 2026-06 / 2026-08) 全部解析成同一个 2024-01-20~2024-07-19。
        # 后果是**一份还在生效的不下修承诺被当成已过期**, 下修价值照常计入。
        # 丢掉这个窗口之后, resolve_down_reset 的 `_add_months(公告日, cooldown)` 兜底
        # 会给出一个方向正确的冻结期 —— 那比一个自相矛盾的窗口好。
        if commitment and _commitment_window_is_plausible(commitment, event_date):
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
    elif event_type == "putback":
        # 回售窗口**只认正文里解析到的**, 解析不到就是 None —— 不许回落成公告日。
        #
        # 与上面 call_redemption 的 last_trading_date 是同一条教训 (见
        # apply_events_to_terms 里那段注释): effective_start 的通用回落值是公告日本身,
        # 而"申报期从公告当天开始"几乎恒为解析失败的信号。实测这条回落把主池 28 只债的
        # putback_start_date 写成了公告日期 (美锦转债真实窗口 12-01~12-05, 却按第三次
        # 提示性公告的日期存成 12-11 且无截止日)。
        #
        # 更要命的是 177 条**法律意见书/核查意见**也被分类成 putback, 它们本来就没有
        # 申报窗口 —— 回落让每一条都变成一个"从公告日开始、永不结束"的假窗口。
        putback = parse_putback_terms(body) if body else {}
        effective_start = putback.get("start")
        effective_end = putback.get("end")
        if putback.get("price") is not None:
            event_price = float(putback["price"])
    elif body and event_type in {"conversion_suspension", "conversion_resume"}:
        suspension = _drop_implausible_suspension_start(
            parse_conversion_suspension_terms(body), event_date)
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


# 回售申报期的日期区间。三处容错都是实测公告逼出来的, 少一个就整条不匹配:
#   ``(?:起|始)?``  亿田转债写 "2025年8月7日**起至**2025年8月13日" —— 旧正则要求 "日"
#                   后紧跟分隔符, 中间多一个"起"就全不匹配 (该公告其余部分完全正常,
#                   价格 100.314 都解析出来了, 只有窗口丢了)。
#   分隔符字符类     除 至/到/- 外还有 –—~～ 等全半角变体。
#   第二个年份可省    "12月29日起至1月5日" 这类跨年窗口。
_PUTBACK_RANGE_RE = re.compile(
    r"(\d{4})年(\d{1,2})月(\d{1,2})日"
    r"(?:起|始)?"
    r"(?:至|到|[-–—~～])"
    r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日"
)


def parse_putback_terms(text: str) -> dict[str, date | float | None]:
    """从回售公告正文中解析申报期和回售价格.

    返回 ``start`` / ``end`` / ``price``。只接受明确锚定在回售申报期、
    回售期、回售价格附近的文本, 避免把触发观察期误当成行权窗口。

    **法律意见书/核查意见这类配套文件本来就没有申报窗口**, 解析不出是正常的
    (实测 177 条配套文件里 89% 无窗口), 不要为了提高"解析成功率"去放宽锚定词 ——
    那只会把募集说明书里引用的条款期误当成本次窗口。
    """
    if not text:
        return {"start": None, "end": None, "price": None}
    t = re.sub(r"\s+", "", text.replace("（", "(").replace("）", ")"))
    start = None
    end = None
    for m in _PUTBACK_RANGE_RE.finditer(t):
        head = t[max(0, m.start() - 40): m.start()]
        if not re.search(r"回售(?:申报)?期|申报期间|回售登记期", head):
            continue
        y1, m1, d1, y2, m2, d2 = m.groups()
        start = _safe_date(y1, m1, d1)
        end = _safe_date(y2 or y1, m2, d2)
        # 省略年份的跨年窗口 ("2025年12月29日起至1月5日"): 补年后若倒挂则跨到次年
        if start and end and end < start and y2 is None:
            end = _safe_date(int(y1) + 1, m2, d2)
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


def putback_window_is_complete(event: "CBEvent") -> bool:
    """这条 putback 记录是否带着**完整可用**的申报窗口 (起止俱全)。

    解析侧与回洗 CLI 共用同一个判据 —— 两边各写一份正是本仓库反复踩过的坑。
    """
    return (event.event_type == "putback"
            and event.effective_start is not None
            and event.effective_end is not None)


def putback_start_is_degraded(event: "CBEvent") -> bool:
    """``effective_start`` 是不是解析失败回落出来的公告日 (而非真实窗口起始日)。

    存量数据里的谎言长这样: start == 公告日 且没有 end。新解析已经不会再产生它
    (见 parse_event_from_announcement 的 putback 分支), 但落库的还得回洗掉。
    """
    return (event.event_type == "putback"
            and event.effective_end is None
            and event.effective_start is not None
            and event.effective_start == event.event_date)


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
    # 明确锚点先试, 不设闸
    end = _extract_labeled_date(
        t, (r"(?:恢复转股日|恢复转股起始日|恢复转股开始日)(?:为|是|:|：)?",))
    if end is None:
        # **裸的「至/截至」必须加语义闸**。停止转股公告绝大多数是权益分派的配套件, 正文里
        # 必然出现"本次利润分配以…截至2023年12月31日公司总股本…"这类**基准日**表述 ——
        # 与「赎回门槛条款被当成当期余额」「评级符号附录被当成状态」是同一类陷阱。
        # 实测存量 358 条起止俱全的记录里 30 条 end < start, 假 end 集中在期末日
        # (12-31 ×18 / 03-31 ×6 / 04-20 ×3), 正是股本基准日的形状。
        end = _extract_labeled_date(
            t, (r"(?:至|截至)",),
            negative_after=r"公司总股本|总股本|股本基数|股本总额|基准日|的?股份总数")
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
    # **不变量: 暂停窗口的结束不可能早于开始**。语义闸只挡得住"股本基准日"那一种误锚,
    # 实测 1399 份缓存正文重放后仍有 41 条 end < start —— 假 end 还能从 range_re 与
    # resume_after 等别的模式来。两者之中 ``end`` 是更不可靠的那半 (``start`` 有
    # 「暂停转股起始日」这类明确锚点), 所以丢 end 不丢 start。
    #
    # 代价是把"解析出了一个错值"降级成"没解析出来", 而下游对 None 有现成处置:
    # apply_events_to_terms 只在 ``effective_end >= val_date`` 或 end 为 None 时才写
    # 「暂停转股」状态 —— 一个过去的假 end 会让真正在停转的那几天**不写状态**,
    # 「暂停转股」旗标与定价页的告警同时消失。
    if start is not None and end is not None and end < start:
        end = None
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
    # 单位允许 "元/张" 与 "元人民币/张": 天23转债写 "回售价格：100.05元**人民币**/张",
    # 旧正则要求字面 "元/张", 于是价格整条丢失 (窗口反而解析正常)。
    for prefix in prefixes:
        match = re.search(
            prefix + r".{0,16}?([0-9]+(?:\.[0-9]+)?)元(?:人民币)?/张", text)
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


# 「摘牌」「提前赎回」在 A 股公告里是**多义词**, 同一个发行人当天可能在说完全不同的东西:
#   · 优先股全部赎回及摘牌            (上银/兴业转债 —— 说的是优先股)
#   · 公开摘牌取得某公司 100% 股权     (节能转债/精测转2 —— 产权交易所竞拍)
#   · 公司债券(第一期)本息兑付暨摘牌    (冀东/浙建/山路 —— 普通公司债)
#   · 可交换公司债券换股完成暨摘牌      (新乳转债 —— 股东发的 EB)
#   · 使用闲置募集资金现金管理提前赎回   (海顺转债 —— 赎回的是理财产品)
# 这些标题一律不含转债标识, 却全部被判成本债摘牌/强赎, 再经 apply_events_to_bundle
# 写进 last_trading_date / delisting_date —— 实测让 12 只在市转债 (含兴业、上银两只
# 千亿级银行转债) 被准入整体判死。所以会改写 BondTerms 状态的事件类型必须先过这道闸。
#
# 「公司债券」不算标识 —— 「可转换公司债券」含它, 但反过来不成立; 转2/转02 这类简称
# (恒逸转2、胜蓝转02、精测转2) 不含「债」字, 必须单列。
_CB_MENTION_RE = re.compile(r"转债|可转换公司债券|转[0-9]{1,2}(?![0-9])")


def _mentions_convertible_bond(text: str) -> bool:
    return bool(_CB_MENTION_RE.search(text))


#: 公司债/企业债的简称形态: 「22山路01」「23浙建02」「23太阳GK02」——
#: 两位年份 + 2~4 个汉字 + (可选 GK) + 两位序号。转债简称不长这样。
_OTHER_BOND_NAME_RE = re.compile(r"\d{2}[一-龥]{2,4}(?:GK)?\d{2}")


def _title_names_non_convertible_bond(text: str) -> bool:
    """标题点名的是**同发行人的普通公司债**, 不是本转债。

    cninfo 的 ``list_bond_announcements`` 按转债代码查不到时会 fallback 到 searchkey
    全库搜, 返回的是**发行人**的全部公告 —— 于是「22山路02」「23太阳GK02」这类公司债
    公告会挂到转债名下。``_title_names_other_bond`` 挡不住它们: 那个正则只认
    ``X转债`` / ``X转N`` 形状的兄弟转债名。

    实测全库: 命中 24 条 (putback 19 + balance_change 5), **误伤 0** ——
    带转债标识的标题一条都没被判成别的债。
    """
    if _mentions_convertible_bond(text):
        return False
    # 「公司债券」单独出现不是转债标识 (「可转换公司债券」含它但反之不成立),
    # 所以它 + 没有转债标识 = 这标题说的是普通公司债。
    return bool(_OTHER_BOND_NAME_RE.search(text)) or "公司债券" in text


def classify_announcement_title(title: str) -> str:
    # 空白在这里归一化, 而不是留给调用方 —— 巨潮标题里会出现「关于 晶瑞转债 到期兑付…」
    # 这种带空格的写法, 正则里的 ``.{0,30}`` 邻接约束会被撑开。
    # ``parse_event_from_announcement`` 原本自己先 strip 一遍, 于是"照标题重放分类器"的
    # 存量校验会凭空看到差异; 两处各归一化一份, 迟早分叉。
    text = re.sub(r"\s+", "", str(title or "")).upper()
    about_cb = _mentions_convertible_bond(text)
    if about_cb and re.search(
            r"不(提前)?赎回|不强赎|暂不(提前)?赎回|不行使.{0,30}(?:提前赎回|赎回权利?)", text):
        return "call_no_redemption"
    if about_cb and re.search(r"赎回实施|实施赎回|强制赎回|提前赎回|赎回暨摘牌|赎回登记日", text):
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
        # 这里用**否定闸**而不是要求 about_cb: 「未转股余额」「转股结果」是转债固有词,
        # 一条没点名债券简称的合法公告不该因为缺标识被丢掉 (实测 1213/1218 有标识,
        # 而无标识的 5 条全部点名了公司债)。
        if _title_names_non_convertible_bond(text):
            return "unknown"
        return "balance_change"
    if re.search(r"恢复.{0,8}转股|转股.{0,4}恢复", text):
        return "conversion_resume"
    if re.search(r"暂停.{0,8}转股|停止.{0,8}转股|转股.{0,4}暂停", text) and "停牌" not in text:
        return "conversion_suspension"
    # **putback 必须过 about_cb 闸** —— 与 call_redemption / call_no_redemption /
    # delisting 同处置。它此前是裸关键词, 于是同发行人的公司债回售公告被判成本转债的回售,
    # 带着**真实解析出来的**申报窗口与回售价 100.0 进 apply_events_to_terms, 再经
    # pricing_api 传给 pricer —— 而 pricer 的 putback_window_active 分支是**无条件**的
    # ``V = np.maximum(V, 100.0)``, 覆盖整条 S 网格 (不像常规回售只作用在触发线以下)。
    # 实测山路/太阳GK/浙建三只被这样写坏过。
    #
    # 影响面已量: 全库 1089 条 putback 里 1067 条标题含转债标识, 被这道闸挡掉的 22 条
    # 全部是法律意见书 / 评级机构关注函 / 公司债公告 —— 一条真实的转债回售都没误伤。
    if about_cb and "回售" in text:
        return "putback"
    if "评级" in text:
        return "rating_change"
    if about_cb and re.search(r"摘牌|最后交易日", text):
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


# 同一发行人可以在同一个名字下**先后发两只转债**: 110099.SH 福能转债 2025-10-30 上市, 而
# 库里挂着 2024-10/11 的「关于“福能转债”到期兑付暨摘牌」四条公告 —— 那是上一只同名债的。
# 标题守卫按名字比对, 对同名债天然无解; 日期能: 一只债上市之前不可能发生它自己的摘牌、
# 强赎、回售、转股价调整。这条不变量放在消费侧, 存量脏数据无需回洗即自愈。
#
# 例外: 评级报告 (初始评级本就早于上市) 与正股类事件 (ST/停牌讲的是股票, 与债无关)。
_PRE_LISTING_ALLOWED = frozenset({
    "rating_change", "underlying_st_risk", "underlying_st_clear", "underlying_suspension",
})


def _event_postdates_listing(event: CBEvent, terms: BondTerms) -> bool:
    if event.event_type in _PRE_LISTING_ALLOWED:
        return True
    listed = getattr(terms, "listing_date", None)
    return listed is None or event.event_date >= listed


def apply_events_to_terms(
    bond_code: str,
    terms: BondTerms,
    events: Sequence[CBEvent],
    *,
    valuation_date: date | None = None,
    down_reset_cooldown_months: int = 6,
) -> BondTerms:
    """把事件层合并到 ``BondTerms`` 中, 供筛选和定价使用."""
    val_date = valuation_date or market_today()
    active = [e for e in events if e.bond_code == bond_code and e.event_date <= val_date
              and _event_postdates_listing(e, terms)]
    if not active:
        return terms

    updates: dict[str, Any] = {}
    latest_call = _latest_event(active, "call_redemption")
    if latest_call:
        updates["call_status"] = _event_status(latest_call.event_type)
        updates["call_announce_date"] = latest_call.event_date
        # 只有**真解析到**停止交易日才写。``effective_start`` 在标题/正文都没有日期时会回落成
        # 公告日本身, 而强赎公告与停止交易之间隔着法定提示期 —— "最后交易日 = 公告当天"几乎
        # 恒为解析失败的信号。实测恒逸转2 被一份泛称"可转换公司债券"的法律意见书 (讲的是
        # 兄弟债恒逸转债) 按公告日写成 2026-03-03 停止交易, 于是这只当天成交 326 万手的
        # 活券被准入判成"已过最后交易日"。
        if latest_call.effective_start and latest_call.effective_start > latest_call.event_date:
            updates["last_trading_date"] = latest_call.effective_start
        if latest_call.effective_end:
            updates["call_redemption_date"] = latest_call.effective_end
            if "摘牌" in latest_call.raw_title:
                updates["delisting_date"] = latest_call.effective_end
        if latest_call.event_price is not None:
            updates["call_redemption_price"] = latest_call.event_price
    latest_no_call = _latest_event(active, "call_no_redemption")
    if latest_no_call and (latest_call is None or latest_no_call.event_date >= latest_call.event_date):
        updates["call_status"] = _event_status(latest_no_call.event_type)
        if latest_no_call.effective_end:
            updates["call_no_redemption_until"] = latest_no_call.effective_end

    latest_delist = _latest_event(active, "delisting")
    if latest_delist:
        # 与上面 ``last_trading_date`` 是**同一条闸**: ``effective_start`` 在标题/正文都没有
        # 日期时回落成公告日本身 (parse_event_from_announcement 的通用回落), 而"摘牌日 =
        # 提示性公告当天"恒为解析失败的信号 —— 摘牌提示与实际摘牌之间隔着法定期。
        #
        # 实测全库 20 条 delisting **20/20** 都是 ``effective_start == event_date`` 且
        # ``effective_end`` 为空, 也就是说这个字段 100% 被写成了"最后一次提示性公告的日期"。
        # 后果: 家悦转债真实最后交易日 2026-06-01, 却按 05-07 的提示公告写成已退市,
        # 于是 05-12 的批量重算把一只还能交易 20 个交易日的债剔出主池
        # (batch_pricing 拿 ``delisting_date <= check_date`` 当硬剔除)。
        #
        # 附带治好一个非幂等: ``cb-sync-admission-status`` 每天写回 Wind 的正确值,
        # 紧接着 ``cb-sync-events --apply`` 又改成公告日 —— 两条日常流程互相翻转同一个字段。
        delist_on = latest_delist.effective_end
        if delist_on is None and (latest_delist.effective_start
                                  and latest_delist.effective_start > latest_delist.event_date):
            delist_on = latest_delist.effective_start
        if delist_on is not None:
            updates["delisting_date"] = delist_on

    # 一只债常有几十条 putback 记录 (鸿路转债 33 条), 其中大量是法律意见书/核查意见,
    # 本来就没有申报窗口。取"最新一条"会让一份晚出的配套文件盖掉真正的窗口公告 ——
    # 所以先在**带完整窗口**的记录里取最新, 取不到再退回最新一条 (它可能只带回售价)。
    latest_putback = (
        _latest_event([e for e in active if putback_window_is_complete(e)], "putback")
        or _latest_event(active, "putback")
    )
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
        updates["conversion_suspension_status"] = _event_status(latest_conv_resume.event_type)
        # **恢复日是 effective_end 不是 effective_start**。解析层的返回约定是
        # start=暂停起始 / end=恢复日 (parse_conversion_suspension_terms), 而恢复公告正文里
        # 的"自 XXXX 年 X 月 X 日起暂停转股"往往引用的是几年前那次暂停 —— 拿它当"暂停结束"
        # 会写出早于暂停开始好几年的日期。实测 127 只由 resume 分支决定取值的债里 **120 只**
        # 写出来的值 ≠ 该事件解析到的恢复日, |差| 中位 160 天、最大 2185 天
        # (123064.SZ resume 2026-06-01 却写入 2021-03-08)。
        # 这个字段在事件页/定价页直接显示成「暂停转股结束」, 还参与 pricer 的转股期判定。
        if latest_conv_resume.effective_end:
            updates["conversion_suspension_end_date"] = latest_conv_resume.effective_end
    elif latest_conv_susp:
        if latest_conv_susp.effective_start:
            updates["conversion_suspension_start_date"] = latest_conv_susp.effective_start
        if latest_conv_susp.effective_end:
            updates["conversion_suspension_end_date"] = latest_conv_susp.effective_end
        # **缺 end 不等于永久暂停**。此前这里是 ``effective_end is None or end >= val_date``,
        # 于是一条几个月前的分红停牌把「暂停转股」永久挂在债上 —— 实测主池 50/311 只
        # (16%) 中招, 距起始日中位 92 天、最长 812 天。走 _conversion_suspension_end 兜底。
        # 只拦状态, **不伪造 end 日期**: 推出来的截止日是个估计值, 写进
        # ``conversion_suspension_end_date`` 会让它和真解析到的日期长得一样。
        if _conversion_suspension_end(latest_conv_susp) >= val_date:
            updates["conversion_suspension_status"] = _event_status(latest_conv_susp.event_type)
        elif terms.conversion_suspension_status is not None:
            # **过期了要显式清空**, 不能只是"不写"。``apply_events_to_terms`` 是增量的:
            # 不写这个键, cb_data 里上一次同步落下的旧值就原样活着 —— 实测 63 只债的
            # 「暂停转股」正是这么留在库里的 (改判据但不清空, 一只都不会掉)。
            # 与上面 suspension / underlying_suspension 那两族同一个写法。
            updates["conversion_suspension_status"] = None

    # 临停类事件: 仅在 effective_end 仍在窗口内时才标记停牌;
    # 过期超过 _TRANSIENT_CLEAR_GRACE_DAYS 才主动清空, 给 admission_status (Wind 直刷)
    # 留写入窗口, 避免刚过期的旧事件擦掉当天 admission 同步到的真实"停牌"。
    latest_suspension = _latest_event(active, "suspension")
    if latest_suspension:
        if _transient_still_active(latest_suspension, val_date):
            updates["suspension_status"] = _event_status(latest_suspension.event_type)
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
        updates["underlying_status"] = _event_status(latest_st.event_type)

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


def _conversion_suspension_end(event: CBEvent) -> date:
    """暂停转股的有效截止日: 有 ``effective_end`` 用它, 否则从起始日 + TTL 兜底。

    见 :data:`_CONVERSION_SUSPENSION_TTL_DAYS` —— 缺 end 不等于"永久暂停"。
    """
    if event.effective_end is not None:
        return event.effective_end
    anchor = event.effective_start or event.event_date
    return anchor + timedelta(days=_CONVERSION_SUSPENSION_TTL_DAYS)


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


# ── 事件类型的展示词表与可操作性次序 ────────────────────────────────────────
#
# 与下面的 ``_event_status`` **是两回事, 不要合并**: 那个的返回值会被写进
# BondTerms 的状态字段 (``call_status == "已公告强赎"`` 之类, batch_pricing 的事件
# 旗标就依赖这些字面量), 属于**数据**; 这里是**展示**用的短标签。
#
# 收在这里是因为它此前只存在于 gui/controllers/events.py 的一个私有 staticmethod 里,
# 且漏了 4 个类型 —— 缺的会 fallback 成 ``event_type[:4]``, 于是事件页 badge 渲染出
# "bala" / "conv" / "unkn"; 更糟的是 conversion_suspension 与 conversion_resume
# **两个相反的意思都渲染成 "conv"** (合计 817 条事件)。有守护测试比对 EVENT_TYPES 全覆盖。
EVENT_TYPE_SHORT_LABEL: dict[str, str] = {
    "down_reset_proposed": "提议下修",
    "down_reset_approved": "已下修",
    "down_reset_rejected": "不下修",
    "down_reset_trigger_notice": "触发提示",
    "conversion_price_adjusted": "调转股价",
    "balance_change": "余额变化",
    "call_redemption": "强赎",
    "call_no_redemption": "不强赎",
    "putback": "回售",
    "conversion_suspension": "暂停转股",
    "conversion_resume": "恢复转股",
    "rating_change": "评级",
    "delisting": "摘牌",
    "suspension": "停牌",
    "underlying_suspension": "正股停牌",
    "underlying_st_risk": "正股ST",
    "underlying_st_clear": "撤销ST",
    "unknown": "其他",
}

# 可操作性次序 (小 = 更该先看见)。横幅只放得下几条, 这个顺序直接决定用户看见什么,
# 所以按"错过它的代价"排, 不按字母序也不按发生频率:
#   0  有硬期限, 错过就被动接受结果 (强赎不转股 = 按赎回价被赎走; 摘牌后卖不掉)
#   1  在途下修 —— 本工具的核心 thesis, 结果未定, 是**可以据此下注**的窗口
#   2  回售 / 触发提示 —— 有可执行的价格下限或即将成立的条件
#   3  转股通道与承诺状态变化 —— 影响怎么估值, 但不逼你今天动手
#   4  已经落定的事实 —— 记录价值为主
EVENT_ACTIONABILITY: dict[str, int] = {
    "call_redemption": 0, "delisting": 0, "suspension": 0,
    "down_reset_proposed": 1, "down_reset_approved": 1,
    "putback": 2, "down_reset_trigger_notice": 2,
    "conversion_suspension": 3, "conversion_resume": 3,
    "call_no_redemption": 3, "underlying_suspension": 3, "underlying_st_risk": 3,
    "down_reset_rejected": 4, "conversion_price_adjusted": 4, "balance_change": 4,
    "rating_change": 4, "underlying_st_clear": 4, "unknown": 4,
}
_DEFAULT_ACTIONABILITY = 4


# 区间事件的**结束日**是否可信、以及结束时该怎么称呼。不在表里的类型, 其
# effective_end 一律不用于"未来窗口"提示。
#
# 逐类型实测 (全库 7794 条) 后才敢用:
#   call_no_redemption 94% 有 end / down_reset_rejected 66% / suspension 86% /
#   putback 60% / call_redemption 38%  —— 这几类的 end 就是承诺期满或申报截止, 可信。
#   其余类型 (调转股价 / 评级 / 余额变化 / 提议下修 / 触发提示 / 摘牌) end 覆盖率≈0, 无从谈起。
#
# **conversion_suspension / conversion_resume 刻意排除**, 尽管它们 70%/98% 有 end:
# 那个 end 被公告正文里的**回售期区间**污染了。实测宝莱转债 (123065.SZ) 的
# "关于回售期间宝莱转债暂停转股的提示性公告" 解析出 start=2021-03-11 end=2026-09-03
# —— 那是回售期的起止, 不是停牌窗口; 用它做提示会渲染出"恢复转股到期"这种胡话。
# 又一次的"条款文字 vs 当期状态"。
EVENT_END_LABEL: dict[str, str] = {
    "call_redemption": "截止",          # 赎回登记截止 —— 过了就按赎回价被赎走
    "putback": "截止",                  # 回售申报截止
    "call_no_redemption": "到期",       # 不强赎承诺期满 → 强赎上限恢复
    "down_reset_rejected": "到期",      # 不下修承诺期满 → 下修博弈解冻
    "suspension": "结束",
    "underlying_suspension": "结束",
}

# 承诺**期满**的可操作性与承诺公告本身不同 —— 公告当天是"这事没了", 期满那天是
# "这事又可能了"。对一个以下修博弈为核心的工具, 不下修承诺解冻恰恰是故事重新开始。
EVENT_END_ACTIONABILITY: dict[str, int] = {
    "down_reset_rejected": 1,           # 下修博弈解冻, 与在途下修同档
    "call_no_redemption": 2,            # 强赎上限恢复
}


def event_end_label(event_type: str) -> str | None:
    """区间事件结束时的后缀词; 返回 None 表示该类型的 effective_end 不可信/无意义。"""
    return EVENT_END_LABEL.get(event_type)


def event_short_label(event_type: str) -> str:
    """事件类型 → 展示用短标签; 未知类型退回原串而不是切前 4 个字符。"""
    return EVENT_TYPE_SHORT_LABEL.get(event_type, event_type or "其他")


#: 事件类型 → badge 颜色。与 EVENT_TYPE_SHORT_LABEL 并列为展示层的单一事实源 ——
#: GUI 曾自带一份只覆盖 14/18 的私有配色表, 剩下 4 类 (balance_change /
#: conversion_suspension / conversion_resume / unknown) 全渲染成同一个灰色。
#: 这与"GUI 自带一份短标签表并把暂停转股和恢复转股同显 conv"是同一类分叉:
#: **展示词表只许有一份**, 否则加事件类型时总会漏掉某一份。
#: 有守护测试比对 EVENT_TYPES 全覆盖。
EVENT_TYPE_COLOR: dict[str, str] = {
    # 下修一族: 提议=黄(待表决) / 通过=绿(利好落地) / 否决=红 / 触发提示=橙
    "down_reset_proposed":        "#e6a700",
    "down_reset_approved":        "#40a02b",
    "down_reset_rejected":        "#d20f39",
    "down_reset_trigger_notice":  "#df8e1d",
    "conversion_price_adjusted":  "#179299",
    # 强赎一族: 赎回=红(硬退出期限) / 不赎=绿(上限解除)
    "call_redemption":            "#d20f39",
    "call_no_redemption":         "#40a02b",
    "putback":                    "#7287fd",
    # 状态类
    "rating_change":              "#df8e1d",
    "delisting":                  "#8839ef",
    "suspension":                 "#fe640b",
    "conversion_suspension":      "#fe640b",   # 与停牌同族: 暂时不能转
    "conversion_resume":          "#40a02b",   # 相反的意思, 必须是相反的颜色
    "balance_change":             "#179299",
    # 正股类
    "underlying_suspension":      "#fe640b",
    "underlying_st_risk":         "#d20f39",
    "underlying_st_clear":        "#40a02b",
    "unknown":                    "#6c6f85",
}

#: 未登记类型的兜底色 (中性灰)。
EVENT_TYPE_COLOR_FALLBACK = "#6c6f85"


def event_type_color(event_type: str) -> str:
    return EVENT_TYPE_COLOR.get(event_type, EVENT_TYPE_COLOR_FALLBACK)


def event_actionability(event_type: str, *, is_end: bool = False) -> int:
    """事件类型 → 可操作性次序 (小 = 更该先看见)。

    *is_end* 表示这是区间事件的**结束**, 次序另有一张覆盖表 (见 EVENT_END_ACTIONABILITY)。
    """
    if is_end and event_type in EVENT_END_ACTIONABILITY:
        return EVENT_END_ACTIONABILITY[event_type]
    return EVENT_ACTIONABILITY.get(event_type, _DEFAULT_ACTIONABILITY)


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


def _is_strict_upgrade(current: CBEvent, incoming: CBEvent) -> bool:
    """*incoming* 是不是 *current* 的**严格**更优版本。

    严格 = 解析出的字段更多, **且**原有的非空值一个都不被抹成 None。后半条是防线:
    重跑一个更差的解析器 (或没带正文的那一次) 不许把已经解析好的数据擦掉。
    """
    fields = ("effective_start", "effective_end", "event_price")
    for name in fields:
        if getattr(current, name) is not None and getattr(incoming, name) is None:
            return False
    return _parsed_field_count(incoming) > _parsed_field_count(current)


#: 公告日与它所宣布的窗口起始日之间, 允许的最大**回溯**天数。
#:
#: 公告总是**提前**发 (实测暂停转股的提前量中位 5 天、最长 13 天), 所以一个正常的窗口
#: 起始日应当**不早于**公告日太多。而 ``parse_conversion_suspension_terms`` 里那个裸的
#: ``(?:自|从)`` 锚会抓到正文里对转股期/回售期的引述, 于是一份 2026 年的公告解析出
#: 2020 年的起始日 —— 实测 508 条里 **155 条 (31%)** 的 start 早于公告日 30 天以上,
#: 141 条早于 180 天, 最远 2072 天 (128136.SZ 立讯转债 2026-07-07 公告 → start 2020-11-03)。
#:
#: 留 30 天而不是 0: 偶有公告在窗口开始之后才补发, 而 30 天远小于 155 条里最小的那个
#: 回溯量 —— 阈值同样落在空档里 (次小的是 180 天那一档)。
_MAX_WINDOW_BACKDATE_DAYS = 30


def _commitment_window_is_plausible(commitment: dict, event_date: date) -> bool:
    """承诺期窗口是不是这份公告自己宣布的 (而不是正文里引用的上一期)。

    判据只有一条方向性的: **结束日不能早于公告日**。一份公告不可能在发布之前就失效。
    """
    end = commitment.get("end")
    return end is None or end >= event_date


def plausible_commitment_end(event: CBEvent) -> date | None:
    """事件上可信的承诺期结束日; 早于公告日的一律当没有.

    与 :func:`_commitment_window_is_plausible` 是**同一条**方向性不变量, 只是作用在
    消费侧。两侧都要有: 解析侧的闸只作用于**新解析**的事件, 而库里存量、手改、从旧
    快照重新导入的行照样会带着 ``end < event_date`` 进来 —— 实测 `cb_events.json`
    里 70 行是这个形状, 28 行属承诺期类型, 其中 6 行是本类型最新一条 (会真的被消费),
    3 只债今天因此把一个**还在生效**的冻结窗口读成已过期:

    ==============  ==========  ==========  ============
    债              公告日      脏 end      按承诺月数
    ==============  ==========  ==========  ============
    113650.SH       2026-05-27  2026-05-26  2026-11-27
    113700.SH       2026-07-18  2026-06-26  2026-10-18
    123071.SZ       2026-08-14  2024-07-19  2027-02-14
    ==============  ==========  ==========  ============

    返回 None 表示"这条 end 不可信", 由调用方回落到 ``公告日 + 承诺月数``。
    """
    end = getattr(event, "effective_end", None)
    if end is None:
        return None
    return end if end >= event.event_date else None


def _drop_implausible_suspension_start(
    suspension: dict | None, event_date: date,
) -> dict | None:
    """暂停转股窗口: 起始日早于公告日太多时丢掉它 (见 :data:`_MAX_WINDOW_BACKDATE_DAYS`)。

    只丢 ``start``, 不丢整条 —— ``end`` 走的是另一套锚点, 两者的可信度互不担保。
    这与 AGENTS 里"start 有明确锚点所以丢 end 不丢 start"那句话方向相反: 实测数据说
    **start 才是被引述污染的那一半** (155/508), 那句话已在 AGENTS 中更正。
    """
    if not suspension:
        return suspension
    start = suspension.get("start")
    if start is not None and (event_date - start).days > _MAX_WINDOW_BACKDATE_DAYS:
        suspension = dict(suspension)
        suspension["start"] = None
    return suspension


def _parsed_field_count(event: CBEvent) -> int:
    """这条事件真正解析出了几个字段。

    ``effective_start`` 只在**严格晚于公告日**时才算 —— 解析不到时的通用回落值就是
    公告日本身, 它不携带信息 (AGENTS 记过: "最后交易日/申报期从公告当天开始"几乎恒为
    解析失败的信号)。
    """
    fields = (
        event.effective_start if (
            event.effective_start is not None
            and event.effective_start > event.event_date) else None,
        event.effective_end,
        event.event_price,
    )
    return sum(1 for f in fields if f is not None)


def _event_selection_key(event: CBEvent) -> tuple:
    """``_latest_event`` 挑代表用的键 —— **与落盘排序键分开**。

    ``_event_sort_key`` 还负责 ``cb_events.json`` 的落盘顺序与 ``list_events`` 的输出
    顺序, 动它会把整个数据文件重排, 所以选择逻辑单独一把键。

    日期仍是主键 (最新的赢), 但**同日先比解析出的字段数**, 最后才拿标题兜底。
    此前同日直接落到 ``raw_title`` 的 Unicode 序, 而中文标题的排序把赢面系统性地给了
    承销商核查意见 / 律所法律意见书 (标题以「申万宏源」「国浩律师」这类机构名开头,
    排序靠后), 而带日期与价格的恰恰是发行人的「关于…的公告」。
    实测全库 697 组同 (债, 日, 类型) 里 **82 组**选中的那条比同组最富的少解析出字段,
    例: 127041.SZ 2024-08-23 选中国浩律师的核查意见 (0 个字段) 而不是
    「关于"弘亚转债"回售的公告」(3 个字段) —— 回售申报窗口与回售价就这么丢了。
    """
    return (event.event_date, _parsed_field_count(event), event.raw_title)


def _latest_event(events: Sequence[CBEvent], event_type: str) -> CBEvent | None:
    matched = [e for e in events if e.event_type == event_type]
    return max(matched, key=_event_selection_key) if matched else None


def _down_reset_block_until_from_event(event: CBEvent, *, cooldown_months: int) -> date:
    end = plausible_commitment_end(event)
    if end:
        return end
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
