"""关注池**行情层**落盘: 热缓存 + 按日窄快照.

与 :mod:`convertible_bond.watchlist` 的分工:

- ``watchlist.py`` 是**意图层** —— "我关注哪几只, 以及加入那一瞬间的研究信号
  (``snapshot_*``)"。永久保留, 只随手动增删变化。
- 本模块是**行情层** —— "这些债今天/那天多少钱"。每次刷新重写。

两层分开而不是往 ``watchlist.json`` 里塞定价字段, 是因为它们的保留期与语义完全
不同: 把"加入时理论价 108.7"和"今天理论价 110.8"放进同一个 dict, 迟早有人分不清
自己读到的是哪一个。

行情层又分两个文件:

- ``watchlist_pricing_cache.json`` —— **热缓存**, 最新一期完整行, 逐只 upsert。
  ``rows`` 是 ``code → row`` 的 dict 而不是 list: 关注池是逐只 upsert, 主表才是
  整池重写, 用 list 会逼每个调用方自己再做一次 code→index 映射。
- ``watchlist_daily/YYYY-MM-DD.json`` —— **按日窄快照**, 每交易日一份, 只追加,
  支撑"涨跌 vs 上一交易日"。

为什么日志用按日文件, 而不是在热缓存里放一个 ``prev`` 块: ``prev`` 需要一条
"新 valuation_date != 旧 valuation_date 才滚动"的规则, 而这条规则写错的后果是
**同日重跑两次就把 prev 冲成今早的值**, 于是所有变化列恒为 0 且完全静默 ——
表面上只是"今天没什么变化"。按日文件让这件事在结构上不可能: 写今天的文件永远
不碰昨天的文件。代价约 5KB/天。

三个时间戳不要混用 (混用会静默错一天: 本机时区的"今天"与市场口径的
``market_today()`` 在非东八区会差一天, 实测本机在美西时正是如此):

===============================  =========================  ==========================
字段                             取值                        回答
===============================  =========================  ==========================
``_meta.saved_at`` / ``priced_at``  ``datetime.now()``       几分钟前算的
``_meta.valuation_date`` / 文件名   ``market_today()``       这是哪个交易日的价
``market_price_as_of``              行情数据自身的日期        这个市价本身是哪天的
===============================  =========================  ==========================
"""
from __future__ import annotations

import json
import logging
import math
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from collections.abc import Iterable, Sequence

from .atomic_io import atomic_write_json
from .data_providers import finite_float
from .paths import data_path

logger = logging.getLogger(__name__)

CACHE_SCHEMA = 1
META_KEY = "_meta"

#: 行来源。``seeded`` 这一档是必须的 —— ``paths._SEEDED_DATA_FILES`` 含
#: ``batch_pricing_cache.json``, 桌面包首启时 ``_batch_all_results`` 里装的是
#: **别人机器上**的种子缓存, ``valuation_date`` 可能是几周前。不区分它就会在
#: 多半没装 Wind 的机器上, 开机即起一轮必然失败的全量取数。
ORIGINS = frozenset({"batch_main", "watchlist_worker", "upcoming", "seeded"})

#: 热缓存行的字段白名单。定价结果行实测 112 键, 绝大部分 (希腊值、诊断中间量、
#: 网格参数) 对关注池展示没有意义, 存下来只会让每次读盘变慢并给未来的字段改名
#: 制造无谓的兼容负担。
CACHE_FIELDS: tuple[str, ...] = (
    # 身份
    "bond_code", "bond_name", "stock_code", "underlying_name",
    # 定价主结果
    "status", "theoretical_price", "market_price", "deviation",
    "K", "parity", "conversion_premium",
    # 研究信号
    "double_low", "quality_score",
    "confidence", "sensitivity_status", "risk_tags", "event_flags",
    "down_reset_trigger_gap",
    # 条款/状态 (只收真实日期与评级, 不收 is_tradable / trading_status ——
    # 那两个是派生字段, 缓存值只可能是 infer_cb_trading_metadata 自己上一次的
    # 输出, 当独立证据用就是自我确认)
    "credit_rating", "maturity_date", "listing_date", "tradable_date",
    # 横截面 (只有拿到主池锚时才有值, 见 save_watchlist_pricing 的 cross_section)
    "relative_deviation", "cheapness_rank", "cheapness_rank_total",
    "cheapness_percentile", "cross_section_origin",
    # 锚值与**锚自己的估值日**: 相对偏差是 deviation 减去这个中位, 而中位的水平
    # 时变 (cb_valuation_history 20 期实测摆幅 21.2pp)。不落盘就等于读回来一个
    # 没有分母出处的横截面量, 展示层也判不出该不该灰掉那两列。
    "market_median_deviation", "market_median_deviation_as_of",
    # 溯源三件套
    "valuation_date", "priced_at", "origin",
    "market_price_as_of", "market_price_source",
)

#: 按日窄快照的字段。只留"回头算变化"真正要用的那几个 —— 日志是要长期堆积的,
#: 每多一个字段就是每天多一份存储和一次未来的兼容负担。
#:
#: **``deviation`` / ``relative_deviation`` / ``theoretical_price`` 目前没有展示层
#: 消费者** (关注池的「偏差Δ(pp)」列已删), 但它们要留着: 这个目录是**只追加**的,
#: 删掉字段就等于把过去那些天的值永久丢掉 —— 将来想画关注池的偏差历史、或者想把
#: 那一列加回来, 都补不回来。而代价只是每天十几行各多几个 float。
NARROW_FIELDS: tuple[str, ...] = (
    "bond_code", "status",
    "market_price", "market_price_as_of", "market_price_source",
    "theoretical_price", "deviation", "relative_deviation",
    "risk_tags", "event_flags", "origin",
)

#: 落盘时 NaN 写成 null, 读回时还原成 NaN —— 与 ``batch_pricing`` 的内存路径
#: 保持一致。不还原的话 ``row.get(k) is not None`` 这类检查会在两条路径上给出
#: 不同答案 (内存里是 nan 走 True, 读盘回来是 None 走 False)。
_NAN_FIELDS = frozenset({
    "theoretical_price", "market_price", "deviation", "K", "parity",
    "conversion_premium", "double_low", "quality_score",
    "down_reset_trigger_gap",
    "relative_deviation", "cheapness_percentile", "market_median_deviation",
})

#: 读回时还原成 ``date`` 对象的字段。**必须还原** —— ``market_price_as_of <
#: valuation_date`` 这个比较在内存路径拿到 date、在读盘路径拿到 str, 混用直接
#: TypeError, 且症状随"刚算完 vs 读回来"漂移。
_DATE_FIELDS = frozenset({
    "valuation_date", "market_price_as_of",
    "maturity_date", "listing_date", "tradable_date",
    "market_median_deviation_as_of",
})

#: 横截面锚的有效期 (交易日)。中位偏差水平是时变的 —— ``cb_valuation_history``
#: 20 期实测摆幅 21.2pp —— 拿几周前的市场水平当今天的基准会让"相对便宜度"整体
#: 漂移。这里按自然周内的工作日数近似 (项目没有交易日历, ``strategy_backtest``
#: 也是同样的 ``weekday() < 5`` 口径)。节假日会让近似**偏保守**(5 个工作日里可能
#: 只有 3 个交易日, 于是提前失效), 这是安全的那个方向。
DEFAULT_ANCHOR_MAX_TRADING_DAYS = 5


# ── 路径 ────────────────────────────────────────────────────────────

def watchlist_pricing_cache_path() -> Path:
    return data_path("watchlist_pricing_cache.json")


def watchlist_daily_dir() -> Path:
    return data_path("watchlist_daily")


def daily_snapshot_path(day: date, *, daily_dir: str | Path | None = None) -> Path:
    root = Path(daily_dir) if daily_dir else watchlist_daily_dir()
    return root / f"{_as_date(day).isoformat()}.json"


# ── 序列化 ──────────────────────────────────────────────────────────

def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _json_ready(value: Any) -> Any:
    """转成可安全落盘的结构; NaN → None, date/datetime → isoformat 字符串.

    date 一律转字符串而不是留原对象, 是为了让"落盘再读回"成为一个**确定**的
    round-trip: 只有一种在盘上的表示, 反序列化时才有唯一的还原规则。
    """
    if isinstance(value, dict):
        return {str(k): _json_ready(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def _restore_row(row: dict) -> dict:
    """读盘还原: null → NaN (数值字段), 字符串 → date (日期字段).

    刻意**不复用** ``batch_pricing._restore_result_row`` —— 那是一份 27 键硬编码
    白名单, 只还原 NaN 不还原 date, 且它的键集与本模块的白名单不是一回事。共用
    一份会让两边任何一次字段增删都悄悄影响对方。
    """
    restored = dict(row)
    for key in _NAN_FIELDS:
        if key in restored and restored[key] is None:
            restored[key] = float("nan")
    for key in _DATE_FIELDS:
        value = restored.get(key)
        if isinstance(value, str) and value:
            try:
                restored[key] = _as_date(value)
            except ValueError:
                restored[key] = None
    return restored


def to_cache_row(row: dict, *, origin: str | None = None,
                 valuation_date: date | str | None = None,
                 priced_at: str | None = None) -> dict:
    """按白名单裁出一行热缓存行, 并补齐溯源三件套."""
    out = {key: row[key] for key in CACHE_FIELDS if key in row}
    if origin is not None:
        out["origin"] = origin
    if valuation_date is not None:
        out["valuation_date"] = _as_date(valuation_date)
    if priced_at is not None:
        out["priced_at"] = priced_at
    return out


def to_narrow(row: dict) -> dict:
    """按日窄快照的一行."""
    return {key: row[key] for key in NARROW_FIELDS if key in row}


def _atomic_write(path: Path, payload: dict) -> Path:
    """先写 .tmp 再 rename —— 半截 JSON 比没有文件更难查."""
    return atomic_write_json(path, payload)


# ── 写 ──────────────────────────────────────────────────────────────

#: 价格块 —— 整行 upsert 时这几个字段要作为**一个单位**一起判, 见
#: :func:`_keep_better_market_fields`。
#:
#: **不能只护市价那三个**: 关注池表就是围着两条恒等式排的
#: (`偏差 = 市价/理论价 − 1`、`双低 = 市价 + 转股溢价×100`), 保住昨天的市价却让
#: 理论价换成今天的, 表上那一行当场自相矛盾 —— 实测 市价 158.40 / 理论价 130.00 /
#: 偏差 0.3200, 而 158.40/130.00−1 = 0.2185。读者按恒等式一算就发现对不上,
#: 而页面上没有任何线索说这两个数来自不同的天。
_PRICE_BLOCK_FIELDS = (
    "market_price", "market_price_as_of", "market_price_source",
    "theoretical_price", "parity",
    "deviation", "undervaluation_rate", "relative_deviation",
    "conversion_premium", "double_low",
)


def _has_dated_market_price(row: dict | None) -> bool:
    """这一行有没有一个**带日期的真实**市价 (不是条款库兜底)。"""
    if not row:
        return False
    value = row.get("market_price")
    if value is None or value != value:          # None / NaN
        return False
    if row.get("market_price_source") == "terms_close":
        return False
    return row.get("market_price_as_of") is not None


def _keep_better_market_fields(old: dict | None, new: dict) -> dict:
    """整行 upsert 时不要用**更差**的市价盖掉更好的.

    热缓存是整行 upsert (`merged.update(fresh)`), 而"取到市价"是**逐只**成败的:
    转债行情抖一下, 这一只回落到 `terms_close` 兜底 (没有 as-of, 可以任意旧 ——
    日升转债库里那个 99.994 是 2021 年撤销发行前的值), 而正股链路正常, 于是
    `status` 照样是 "ok"、理论价照样算得出来。整行写进去就把昨天那个真实的
    158.40 / as_of 2026-09-01 / deviation +0.42 换成 99.994 / None / NaN。

    「全失败守卫」拦不住这一档 —— 它是**全或无**的 (`expect_price and not with_price`),
    而部分失败 (1 只真价 + 1 只兜底) 从它底下整只穿过去。

    保留的是**整个价格块** (见 :data:`_PRICE_BLOCK_FIELDS`), 不是只有市价那三个 ——
    半块保留会让表上的恒等式当场断掉。风险标签/置信度/σ/正股价这些不在块里,
    照旧取本轮的值。
    """
    if not old or _has_dated_market_price(new) or not _has_dated_market_price(old):
        return new
    kept = dict(new)
    for field in _PRICE_BLOCK_FIELDS:
        if field in old:
            kept[field] = old[field]
    return kept


def save_watchlist_pricing(
    rows: Sequence[dict],
    *,
    valuation_date: date | str,
    source: str | None = None,
    params: dict | None = None,
    cross_section: dict | None = None,
    origin: str = "watchlist_worker",
    cache_path: str | Path | None = None,
    daily_dir: str | Path | None = None,
    merge: bool = True,
) -> dict[str, Path]:
    """写热缓存 (逐只 upsert) + 当日窄快照 (整体重写), 返回两个路径.

    Parameters
    ----------
    cross_section :
        主池横截面锚, 形如 ``{"market_median_deviation": .., "from": ..,
        "from_valuation_date": .., "n": ..}``。**为 None 时不要伪造** ——
        ``relative_deviation`` / ``cheapness_*`` 会随之写空, 展示层打「—」。
        锚缺失时回落 0.0 是比空值更坏的失败模式: 6 行子集自算中位, 每只恰好
        偏移一个中位的量 (实测 +20.9pp), 而这个错误看上去完全像个正常数字。
    merge :
        True 时把 *rows* upsert 进已有热缓存 (关注池是逐只刷新的常态);
        False 时整体替换。**当日窄快照永远是整体重写** —— 同一 valuation_date
        重跑两次, 结果应当是第二次那一份, 而不是两次的并集。
    """
    val_date = _as_date(valuation_date)
    now_iso = datetime.now().isoformat(timespec="seconds")
    if origin not in ORIGINS:
        raise ValueError(f"未知 origin: {origin!r}; 允许 {sorted(ORIGINS)}")

    fresh: dict[str, dict] = {}
    for row in rows:
        code = row.get("bond_code")
        if not code:
            continue
        fresh[str(code)] = to_cache_row(
            row, origin=origin, valuation_date=val_date, priced_at=now_iso)

    cache_file = Path(cache_path) if cache_path else watchlist_pricing_cache_path()
    merged: dict[str, dict] = {}
    if merge:
        try:
            merged = dict(load_watchlist_pricing(cache_file).get("rows") or {})
        except Exception:
            logger.debug("读旧热缓存失败, 按整体重写处理", exc_info=True)
            merged = {}
    for code, row in fresh.items():
        merged[code] = _keep_better_market_fields(merged.get(code), row)

    meta = {
        "schema": CACHE_SCHEMA,
        "saved_at": now_iso,                       # 本机挂钟
        "valuation_date": val_date.isoformat(),    # 市场口径
        "source": source,
        "params": _json_ready(params or {}),
        "cross_section": _json_ready(cross_section) if cross_section else None,
        "n_rows": len(merged),
    }
    _atomic_write(cache_file, {
        META_KEY: meta,
        "rows": {code: _json_ready(row) for code, row in sorted(merged.items())},
    })

    daily_file = daily_snapshot_path(val_date, daily_dir=daily_dir)
    daily_records = _merge_daily_records(daily_file, fresh)
    _atomic_write(daily_file, {
        META_KEY: {
            "schema": CACHE_SCHEMA,
            "saved_at": now_iso,
            "valuation_date": val_date.isoformat(),
            "source": source,
            "market_median_deviation": (cross_section or {}).get("market_median_deviation"),
            # 文件里**实际有多少条**, 不是本轮刷了多少条 —— 合并之后这两个数不再相等,
            # 而 n_records 是给读盘方核对用的。本轮刷了几只由 n_fresh 单独记。
            "n_records": len(daily_records),
            "n_fresh": len(fresh),
        },
        # 与**当天已有的**记录合并, 按 bond_code 覆盖。
        #
        # 只写 ``fresh`` 是错的: 一轮**部分**刷新 (关注池里只重算了几只、自愈只挑
        # ``_price_state != "ok"`` 的那几只) 会把当天文件整份重写成那几行, 把同一天
        # 早些时候已经写进去的其他债**永久删掉** —— 实测第一轮写 A/B/C 三只, 第二轮
        # 只刷 A, 当天文件就只剩 A, 而热缓存 (它是 merge-upsert) 三只都还在。
        # 同一批数据两个文件给出不同答案, 而这个目录是**只追加的历史**, 丢了不可恢复
        # (AGENTS 记过这条: "只追加的日志停写就是永久丢历史")。
        #
        # 合并只在**同一个估值日**内发生, 所以不存在"隔夜旧行混进今天"的风险 ——
        # 原注释担心的那件事由文件名按日期分片本身挡住了; 真正要防的是同一天内
        # 早写的行被后一轮抹掉。
        "records": daily_records,
    })
    return {"cache": cache_file, "daily": daily_file}


# ── 读 ──────────────────────────────────────────────────────────────

def _merge_daily_records(daily_file: Path, fresh: dict[str, dict]) -> list[dict]:
    """当天窄快照 = 盘上已有的那份 按 bond_code 覆盖上本轮的 ``fresh``。

    读盘失败按空处理: 顶多退化成"只写本轮", 与修复前同行为, 不会因为一个坏文件
    让整轮刷新失败 (这条路在关注池刷新的主线程上)。
    """
    existing: dict[str, dict] = {}
    try:
        if daily_file.exists():
            payload = json.loads(daily_file.read_text(encoding="utf-8"))
            for rec in payload.get("records") or []:
                code = rec.get("bond_code")
                if code:
                    existing[str(code)] = rec
    except Exception:
        logger.debug("读当天窄快照失败, 按只写本轮处理: %s", daily_file, exc_info=True)
        existing = {}
    existing.update({code: _json_ready(to_narrow(row)) for code, row in fresh.items()})
    return [existing[code] for code in sorted(existing)]


def load_watchlist_pricing(path: str | Path | None = None) -> dict:
    """读热缓存; 文件不存在或损坏时返回空壳而不是抛异常.

    这是启动首屏路径上的调用 —— 一个坏掉的运行态缓存不该让主页打不开。
    """
    cache_file = Path(path) if path else watchlist_pricing_cache_path()
    empty = {"meta": {}, "rows": {}, "path": cache_file}
    if not cache_file.exists():
        return empty
    try:
        with open(cache_file, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError):
        logger.warning("关注池热缓存读取失败, 按空处理: %s", cache_file)
        return empty
    raw_rows = payload.get("rows")
    if not isinstance(raw_rows, dict):
        return empty
    return {
        "meta": payload.get(META_KEY) or {},
        "rows": {str(code): _restore_row(row)
                 for code, row in raw_rows.items() if isinstance(row, dict)},
        "path": cache_file,
    }


def load_daily_snapshot(day: date | str, *,
                        daily_dir: str | Path | None = None) -> dict | None:
    """读某一天的窄快照; 没有那天的文件返回 None."""
    snap_file = daily_snapshot_path(_as_date(day), daily_dir=daily_dir)
    if not snap_file.exists():
        return None
    try:
        with open(snap_file, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError):
        logger.warning("关注池日快照读取失败, 按缺失处理: %s", snap_file)
        return None
    records = payload.get("records")
    if not isinstance(records, list):
        return None
    return {
        "meta": payload.get(META_KEY) or {},
        "valuation_date": _as_date(payload.get(META_KEY, {}).get("valuation_date")
                                   or snap_file.stem),
        "rows": {str(r.get("bond_code")): _restore_row(r)
                 for r in records if isinstance(r, dict) and r.get("bond_code")},
        "path": snap_file,
    }


def list_daily_dates(*, daily_dir: str | Path | None = None) -> list[date]:
    """已落盘的窄快照日期, 升序."""
    root = Path(daily_dir) if daily_dir else watchlist_daily_dir()
    if not root.exists():
        return []
    days: list[date] = []
    for path in root.glob("*.json"):
        try:
            days.append(date.fromisoformat(path.stem))
        except ValueError:
            continue          # 手工放进来的杂文件, 忽略而不是炸掉
    return sorted(days)


def latest_daily_before(day: date | str, *,
                        daily_dir: str | Path | None = None) -> dict | None:
    """严格早于 *day* 的最近一份窄快照.

    「上一交易日」由**盘上有没有那天的文件**定义, 而不是靠日历倒推 —— 前者天然
    处理周末与节假日, 后者要维护一份交易日历。代价是: 你没开过 GUI 的那些天不会
    有文件, 于是"涨跌"的基准会跳到更早的一天。所以表头必须写**动态日期**
    (「涨跌% vs 08-25」) 而不是写死「日涨跌」。
    """
    target = _as_date(day)
    earlier = [d for d in list_daily_dates(daily_dir=daily_dir) if d < target]
    if not earlier:
        return None
    return load_daily_snapshot(earlier[-1], daily_dir=daily_dir)


# ── 陈旧判据 ────────────────────────────────────────────────────────

#: 判空一律走这一个 —— **NaN 不是 None**。落盘时 NaN 写成 ``null``, 读回来还原成
#: NaN (``_NAN_FIELDS``), 而 ``NaN is not None`` 为**真**, 于是 ``x is not None``
#: 这种判据会放行 NaN 并把"今天没有市价"渲染成字面的 ``"nan"``。
#:
#: 实现委托给 ``data_providers.finite_float``: 这个仓库同一段判据曾有五份手写副本
#: (base / signal_eval / market_valuation / watchlist_cache / batch_common), 而
#: 关注池与批量页**会互相喂行** —— 两侧对同一字段判空口径不同, 表现是同一只债在
#: 一页有值、另一页是「—」, 不报错。
def _is_finite(value: Any) -> bool:
    return finite_float(value) is not None


def row_is_stale(row: dict | None, today: date) -> bool:
    """这一行今天还要不要重算.

    四个条件任一命中即为陈旧。第四条 (**市价非有限**) 容易被漏而且非漏不可:
    实测 118076.SH 先锋转债 ``status == "ok"``、``valuation_date`` 就是今天、
    唯独 ``market_price`` 是 None —— 前三条一条都不命中, 于是刷一轮之后当天
    **永远不再重试**, 市价与偏差两列空到明天。
    """
    if not row:
        return True
    if str(row.get("status") or "") != "ok":
        return True
    val_date = row.get("valuation_date")
    try:
        if val_date is None or _as_date(val_date) != today:
            return True
    except (TypeError, ValueError):
        return True
    return not _is_finite(row.get("market_price"))


def stale_codes(cache: dict | None, watch_codes: Iterable[str], today: date,
                *, include_seeded: bool = False) -> list[str]:
    """关注池里今天需要重算的代码, 保持传入顺序、去重.

    ``origin == "seeded"`` 的行**按陈旧算但默认不进返回值**: 它们来自桌面包里
    别人机器上的种子缓存, 确实旧, 但首启就对着一台多半没装 Wind 的机器起一轮
    全量取数, 换来的只是一个必然失败的错误框。用户手动点刷新时传
    ``include_seeded=True``。
    """
    rows = (cache or {}).get("rows") or {}
    out: list[str] = []
    seen: set[str] = set()
    for code in watch_codes:
        if not code or code in seen:
            continue
        seen.add(code)
        row = rows.get(str(code))
        if not row_is_stale(row, today):
            continue
        if not include_seeded and row and row.get("origin") == "seeded":
            continue
        out.append(str(code))
    return out


def _trading_days_between(start: date, end: date) -> int:
    """近似交易日数: 只数工作日, 不查节假日 (项目没有交易日历).

    误差方向是**偏保守** —— 含节假日的一周里工作日多于交易日, 于是锚会比真实
    的 5 个交易日更早失效。宁可早失效: 晚失效意味着拿旧市场水平当今天的基准,
    而那是个看上去完全正常的数字。
    """
    if end <= start:
        return 0
    days = 0
    cursor = start
    while cursor < end:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            days += 1
    return days


def anchor_age_is_stale(anchored: date | None, today: date, *,
                        max_trading_days: int = DEFAULT_ANCHOR_MAX_TRADING_DAYS) -> bool:
    """锚的**日期**是不是已经旧到不该再用; ``None`` 一律算陈旧.

    与 :func:`anchor_is_stale` 的分工: 那个查热缓存 ``_meta`` 里的锚 (描述的是
    热缓存**这一批**), 这个只判年龄, 供展示层**逐行**判断 —— 关注池表上的行是
    主池行 / upcoming 行 / 热缓存行的混合, 各自的锚来自不同批次, 只查单个 ``_meta``
    会把它们一概而论。
    """
    if anchored is None:
        return True
    return _trading_days_between(anchored, today) > max_trading_days


def anchor_is_stale(meta: dict | None, today: date, *,
                    max_trading_days: int = DEFAULT_ANCHOR_MAX_TRADING_DAYS) -> bool:
    """横截面锚是不是已经旧到不该再用.

    锚缺失也算陈旧 —— 展示层据此把「相对偏差 / 双低 / 便宜度」整列灰掉, 而不是
    拿一个没有基准的数字冒充横截面量。
    """
    cross = (meta or {}).get("cross_section") or {}
    if not _is_finite(cross.get("market_median_deviation")):
        return True
    raw = cross.get("from_valuation_date")
    if not raw:
        return True
    try:
        anchored = _as_date(raw)
    except (TypeError, ValueError):
        return True
    return anchor_age_is_stale(anchored, today, max_trading_days=max_trading_days)
