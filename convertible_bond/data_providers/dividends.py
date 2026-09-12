"""股息率观察值与有期限缓存：保留来源，区分历史值、实时快照及缺失。"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from typing import Callable

from ..market_time import market_today


def _fetched_at() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class DividendYieldObservation:
    """value_pct 单位为百分数；0 是有效观察值，None 才表示取不到。"""

    value_pct: float | None
    kind: str
    source: str
    as_of: date | None = None
    fetched_at: str = field(default_factory=_fetched_at)
    fetched_on: date = field(default_factory=market_today)
    reason: str = ""

    def __post_init__(self):
        if self.kind not in {"historical", "realtime_snapshot", "unavailable"}:
            raise ValueError(f"未知股息率来源类型: {self.kind}")
        if self.value_pct is not None:
            value = float(self.value_pct)
            if not math.isfinite(value) or value < 0:
                raise ValueError("股息率必须为有限非负百分数")
            object.__setattr__(self, "value_pct", value)
        if (self.kind == "unavailable") != (self.value_pct is None):
            raise ValueError("股息率缺失必须明确标记 unavailable")
        if self.kind == "historical" and self.as_of is None:
            raise ValueError("历史股息率必须包含观察日期")

    def to_dict(self) -> dict:
        raw = asdict(self)
        raw["as_of"] = self.as_of.isoformat() if self.as_of else None
        raw["fetched_on"] = self.fetched_on.isoformat()
        return raw

    @classmethod
    def from_dict(cls, raw: dict) -> DividendYieldObservation:
        values = dict(raw)
        values["as_of"] = date.fromisoformat(values["as_of"]) if values.get("as_of") else None
        values["fetched_on"] = date.fromisoformat(values["fetched_on"])
        return cls(**values)


def scalar_dividend_observation(value, source: str) -> DividendYieldObservation:
    """兼容旧 provider：未声明观察日期的标量不能声称是历史值。"""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        number = float("nan")
    if not math.isfinite(number) or number < 0:
        return DividendYieldObservation(None, "unavailable", source, reason="数据源未返回有效股息率")
    return DividendYieldObservation(number, "realtime_snapshot", source,
                                    reason="旧接口未声明股息率的历史日期")


def fetch_dividend_observation(provider, stock_code: str, on_date: date) -> DividendYieldObservation:
    """取带来源的观察值；保留异常供调用方选择重试或明确回退。"""
    getter = getattr(provider, "get_stock_dividend_yield_observation", None)
    if callable(getter):
        result = getter(stock_code, on_date)
        if not isinstance(result, DividendYieldObservation):
            raise TypeError("股息率观察值接口必须返回 DividendYieldObservation")
    else:
        getter = getattr(provider, "get_stock_dividend_yield", None)
        value = getter(stock_code, on_date) if callable(getter) else None
        result = scalar_dividend_observation(value, getattr(provider, "name", type(provider).__name__))
    if result.kind == "historical" and result.as_of > on_date:
        raise ValueError("股息率观察日期晚于估值日")
    return result


class DividendYieldCache:
    """同股同估值日去重；实时值按采集日缓存，失败最多记五分钟。

    历史数据只有请求日严格早于今天才永久缓存。实时快照保留请求日，避免某日的
    兜底值掩盖另一估值日实际可取的历史值；行情源可以另行复用全市场快照。
    records 可直接 JSON 落盘。锁按请求分开，慢请求不会挡住其他正股。
    """

    SNAPSHOT_TTL_SECONDS = 3600.0
    MISSING_TTL_SECONDS = 300.0

    def __init__(self, records: dict | None = None, *,
                 today: Callable[[], date] = market_today,
                 now: Callable[[], float] = time.time,
                 on_change: Callable[[], None] | None = None):
        self.records = records if records is not None else {}
        self._today, self._now, self._on_change = today, now, on_change
        self._lock = threading.Lock()
        self._key_locks: dict[str, threading.Lock] = {}

    def get(self, provider, stock_code: str, on_date: date) -> DividendYieldObservation:
        source = str(getattr(provider, "name", type(provider).__name__))
        request = f"{source}|{stock_code}|{on_date.isoformat()}"
        with self._lock:
            request_lock = self._key_locks.setdefault(request, threading.Lock())
        with request_lock:
            today, now = self._today(), self._now()
            keys = (f"historical|{request}", f"snapshot|{today.isoformat()}|{request}",
                    f"missing|{today.isoformat()}|{request}")
            for key in keys:
                raw = self.records.get(key)
                if not isinstance(raw, dict):
                    continue
                try:
                    expires = raw["expires_at"]
                    observation = DividendYieldObservation.from_dict(raw["observation"])
                    # 回拨挂钟/错误时间戳也不能延长实时/负缓存的寿命。
                    valid_time = raw["cached_at"] <= now and (expires is None or now < expires)
                    valid_history = (observation.kind == "historical"
                                     and observation.as_of <= on_date < today)
                    if valid_time and (expires is not None or valid_history):
                        return observation
                except (KeyError, TypeError, ValueError, OverflowError):
                    pass
            try:
                observation = fetch_dividend_observation(provider, stock_code, on_date)
            except Exception as exc:
                observation = DividendYieldObservation(None, "unavailable", source,
                                                       reason=f"{type(exc).__name__}: {exc}")
            if observation.kind == "historical" and on_date < today:
                key, expires = keys[0], None
            elif observation.kind == "unavailable":
                key, expires = keys[2], now + self.MISSING_TTL_SECONDS
            else:
                key, expires = keys[1], now + self.SNAPSHOT_TTL_SECONDS
            if expires is not None:
                # 外层缓存不能在读到内层旧观察值时重新计满一轮 TTL。
                ttl = (self.MISSING_TTL_SECONDS if observation.kind == "unavailable"
                       else self.SNAPSHOT_TTL_SECONDS)
                try:
                    fetched = datetime.fromisoformat(observation.fetched_at).timestamp()
                    expires = min(expires, fetched + ttl)
                except (TypeError, ValueError, OverflowError):
                    pass
            # 历史来源当日值也用短期桶；不把暂未收盘的观察固化成历史事实。
            with self._lock:
                for old_key in keys:
                    self.records.pop(old_key, None)
                self.records[key] = {"observation": observation.to_dict(),
                                     "cached_at": now, "expires_at": expires}
                if self._on_change:
                    self._on_change()
            return observation
