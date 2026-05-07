"""仅在 provider 实现内部复用的小工具.

放在 _helpers 而不是 base, 因为这些函数语义偏 "粘合 Wind/akshare 返回的脏数据",
对外不属于公共 API。
"""
from __future__ import annotations

import logging
import re
import time
from datetime import date, datetime
from typing import Any, Callable

import numpy as np

from .base import to_date


logger = logging.getLogger(__name__)


def _retry(call: Callable, attempts: int = 3, delay: float = 0.8, label: str = "akshare"):
    """对瞬态网络错误 (RemoteDisconnected / ConnectionError / timeout) 重试 attempts 次."""
    last_exc: BaseException | None = None
    for i in range(attempts):
        try:
            return call()
        except Exception as e:
            last_exc = e
            msg = str(e).lower()
            transient = (
                "remotedisconnected" in msg
                or "connection aborted" in msg
                or "connection reset" in msg
                or "timeout" in msg
                or "max retries" in msg
            )
            if not transient or i == attempts - 1:
                raise
            logger.warning(
                "%s 调用失败 (第 %d/%d 次, %s), %.1fs 后重试",
                label, i + 1, attempts, type(e).__name__, delay)
            time.sleep(delay)
    if last_exc:
        raise last_exc
    raise RuntimeError(f"{label} 重试逻辑未触发任何调用")


def _latest_finite(values) -> float | None:
    """返回序列里最后一个有限数值."""
    if not values:
        return None
    for v in reversed(values):
        if v is None:
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if np.isfinite(fv):
            return fv
    return None


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        text = str(value).replace(",", "").strip()
        if text in {"", "--", "nan", "None"}:
            return None
        return float(text)
    except (TypeError, ValueError):
        return None


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text if text and text not in {"--", "nan", "None"} else None


def _date_or_none(value: Any) -> date | None:
    if value is None:
        return None
    try:
        if isinstance(value, (date, datetime)):
            return to_date(value)
        text = str(value).strip()
        if not text or text in {"--", "nan", "None"}:
            return None
        if re.fullmatch(r"\d{8}", text):
            return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
        return to_date(text)
    except Exception:
        return None


def _wind_table_rows(res) -> list[dict]:
    try:
        fields = [str(f).lower() for f in res.Fields]
        rows = list(zip(*res.Data))
    except Exception:
        return []
    out: list[dict] = []
    for row in rows:
        out.append({field: row[i] for i, field in enumerate(fields)})
    return out


def _announcement_row_from_wind(row: dict) -> dict:
    def pick(*keys):
        for key in keys:
            if key in row and row[key] not in (None, "", "--"):
                return row[key]
        return None

    return {
        "title": pick("title", "announcement_title", "ann_title", "content", "headline"),
        "date": _date_or_none(pick("date", "announcement_date", "ann_date", "publishdate", "publish_date")),
        "url": pick("url", "link", "announcement_url", "ann_url"),
    }


def _wind_to_ak_bond(wind_code: str) -> str:
    """Wind 格式 (128009.SZ) → akshare 格式 (sz128009)."""
    if "." in wind_code:
        code, exch = wind_code.split(".")
        return f"{exch.lower()}{code}"
    return wind_code


def _wind_to_ak_stock(wind_code: str) -> str:
    """正股 Wind 格式 (000001.SZ) → akshare 格式 (000001, 不带前缀)."""
    return wind_code.split(".")[0] if "." in wind_code else wind_code


def _wind_to_ak_stock_prefixed(wind_code: str) -> str:
    """正股 Wind 格式 (000001.SZ) → akshare 新浪/网易格式 (sz000001)."""
    raw = str(wind_code or "").strip().lower()
    if "." in raw:
        code, exch = raw.split(".", 1)
        return f"{exch}{code}"
    code = raw.zfill(6)
    if code.startswith(("6", "9")):
        return f"sh{code}"
    if code.startswith(("8", "4")):
        return f"bj{code}"
    return f"sz{code}"


def _row_value(row, *keys):
    for key in keys:
        if key in row.index:
            value = row.get(key)
            if value is None:
                continue
            if isinstance(value, float) and np.isnan(value):
                continue
            if str(value).strip() in {"", "--", "nan"}:
                continue
            return value
    return None


def _stock_history_from_df(df) -> list[tuple[date, float | None]]:
    """兼容 akshare 不同历史行情接口的列名差异."""
    if df is None or len(df) == 0:
        return []
    out: list[tuple[date, float | None]] = []
    for _, row in df.iterrows():
        d_raw = _row_value(row, "日期", "date")
        if d_raw is None:
            continue
        try:
            d = to_date(d_raw)
        except Exception:
            continue
        v = _row_value(row, "收盘", "收盘价", "close")
        try:
            close = float(v) if v is not None else None
        except (TypeError, ValueError):
            close = None
        out.append((d, close))
    out.sort(key=lambda item: item[0] or date.min)
    return out
