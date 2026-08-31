"""仅在 provider 实现内部复用的小工具.

放在 _helpers 而不是 base, 因为这些函数语义偏 "粘合 Wind/akshare 返回的脏数据",
对外不属于公共 API。
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from datetime import date, datetime
from typing import Any, Callable

import numpy as np

from .base import safe_date, to_date


logger = logging.getLogger(__name__)


#: **拒绝**信号 —— 不是瞬态抖动, 重试只会加重。东财的实时行情集群
#: (``push2`` / ``push2his``) 按**出口 IP** 限流封禁: 被封时 TCP 连得上、
#: TLS 握手完整成功, HTTP 请求发完之后服务端才直接断开, requests 侧就报成
#: ``ConnectionError('Connection aborted.', RemoteDisconnected(...))``。
#:
#: 判成"瞬态"曾让每只债白撞 3 次: 实测 30s 一次的低频探测连续 8 次全失败
#: (不是短冷却), 而**同一个 URL 经海外代理照常返回数据** —— 拒绝是按 IP 生效的,
#: 接口本身活着。批量定价是 8 线程 × 280+ 只债, 每只再乘 3 就是以三倍力度
#: 继续敲同一个限流器, 等于自己给自己续封。
_REJECTION_MARKERS = ("remotedisconnected", "connection aborted")

#: 真正值得重试的瞬态抖动: 连接中途被重置 / 读超时 / urllib3 自己已经重试到头。
#: 判定顺序上 ``_REJECTION_MARKERS`` 必须**先**匹配 —— ``MaxRetryError`` 的消息里
#: 常把底层 cause 一起带上, 两边都能命中。
_TRANSIENT_MARKERS = ("connection reset", "timeout", "timed out", "max retries")

#: 某个端点被源站拒绝后的熔断冷却时长 (秒)。用
#: ``CBLENS_AKSHARE_ENDPOINT_COOLDOWN_SEC`` 覆盖; 设 0 关闭熔断 (每次都真的去打)。
#: 与 ``WIND_CONNECT_COOLDOWN_SEC`` 是同一个形状的负缓存。
AKSHARE_ENDPOINT_COOLDOWN_SEC = float(
    os.environ.get("CBLENS_AKSHARE_ENDPOINT_COOLDOWN_SEC", "300") or 0)

_ENDPOINT_LOCK = threading.Lock()
_endpoint_tripped_at: dict[str, float] = {}


class EndpointCooldownError(RuntimeError):
    """端点处于熔断冷却期, 本次**没有真的发起请求**就跳过了.

    与"请求发出去失败了"分开是有用的: 调用方据此知道这一档的代价是 0 秒,
    不必再为它保留 fallback 之外的额外提示。
    """


def endpoint_is_tripped(endpoint: str) -> bool:
    """该端点是否仍在拒绝冷却期内."""
    if AKSHARE_ENDPOINT_COOLDOWN_SEC <= 0:
        return False
    with _ENDPOINT_LOCK:
        at = _endpoint_tripped_at.get(endpoint)
    return at is not None and (time.monotonic() - at) < AKSHARE_ENDPOINT_COOLDOWN_SEC


def trip_endpoint(endpoint: str) -> None:
    """把端点标记为被源站拒绝, 起算冷却."""
    with _ENDPOINT_LOCK:
        _endpoint_tripped_at[endpoint] = time.monotonic()


def reset_endpoint_breaker(endpoint: str | None = None) -> None:
    """清掉熔断状态 (``None`` 表示全清). 给测试和"用户显式重试"用."""
    with _ENDPOINT_LOCK:
        if endpoint is None:
            _endpoint_tripped_at.clear()
        else:
            _endpoint_tripped_at.pop(endpoint, None)


def _retry(
    call: Callable,
    attempts: int = 3,
    delay: float = 0.8,
    label: str = "akshare",
    endpoint: str | None = None,
):
    """瞬态网络错误重试 attempts 次; **限流拒绝立即抛出, 不重试**.

    两类错误的区分见 ``_REJECTION_MARKERS`` / ``_TRANSIENT_MARKERS``。

    传了 ``endpoint`` 时额外启用熔断: 该端点一旦被判为拒绝就进负缓存,
    冷却期内后续调用直接抛 ``EndpointCooldownError`` 而**不发起请求** ——
    否则被封的那几个小时里, 每只债都要为一个注定失败的调用等满连接超时
    (实测 ``stock_zh_a_spot_em`` 单次失败就要 5.4s)。
    """
    if endpoint is not None and endpoint_is_tripped(endpoint):
        raise EndpointCooldownError(
            f"{endpoint} 处于源站限流冷却期 ({AKSHARE_ENDPOINT_COOLDOWN_SEC:.0f}s), 跳过本次调用")

    last_exc: BaseException | None = None
    for i in range(attempts):
        try:
            return call()
        except Exception as e:
            last_exc = e
            msg = str(e).lower()
            if any(m in msg for m in _REJECTION_MARKERS):
                if endpoint is not None:
                    trip_endpoint(endpoint)
                logger.warning(
                    "%s 被源站拒绝 (%s) — 判为按 IP 限流, 不重试%s",
                    label, type(e).__name__,
                    f", 该端点冷却 {AKSHARE_ENDPOINT_COOLDOWN_SEC:.0f}s" if endpoint else "")
                raise
            if not any(m in msg for m in _TRANSIENT_MARKERS) or i == attempts - 1:
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
        # safe_date 而不是 to_date: pandas.NaT 是 datetime 子类且为真值, to_date 原样
        # 放行它, 于是 NaT 混进序列, 末尾那次 sort 拿它和真 date 比就抛
        # TypeError (`item[0] or date.min` 也拦不住 —— NaT 是真值)。
        d = safe_date(d_raw)
        if d is None:
            continue
        v = _row_value(row, "收盘", "收盘价", "close")
        try:
            close = float(v) if v is not None else None
        except (TypeError, ValueError):
            close = None
        out.append((d, close))
    out.sort(key=lambda item: item[0] or date.min)
    return out
