"""用户自定义关注池持久化."""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from collections.abc import Iterable, Sequence
from typing import Any

from .atomic_io import atomic_write_json
from .paths import data_path


def watchlist_path() -> Path:
    return data_path("watchlist.json")


def load_watchlist_file() -> dict:
    """整份关注池文件: ``{"items": [...], "dismissed": {...}}``.

    ``dismissed`` 是**用户手动移除过**的代码集。它必须与 items 存在同一份文件里:
    "我关注哪几只"和"我明确不想再看到哪几只"是同一个意图层的两面, 分开存迟早会
    出现只滚动了一半的状态。
    """
    path = watchlist_path()
    empty = {"items": [], "dismissed": set()}
    if not path.exists():
        return empty
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return empty
    items = data.get("items") if isinstance(data, dict) else data
    raw_dismissed = data.get("dismissed") if isinstance(data, dict) else None
    dismissed = {str(c) for c in raw_dismissed if c} if isinstance(raw_dismissed, list) else set()
    return {"items": items if isinstance(items, list) else [], "dismissed": dismissed}


def load_dismissed() -> set[str]:
    """用户手动移除过的代码集."""
    return load_watchlist_file()["dismissed"]


def load_watchlist() -> list[dict]:
    items = load_watchlist_file()["items"]
    cleaned: list[dict] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        code = item.get("bond_code")
        if not code or code in seen:
            continue
        seen.add(code)
        cleaned.append(dict(item))
    return cleaned


def save_watchlist(items: Sequence[dict], *, dismissed: Iterable[str] | None = None) -> Path:
    """写盘; *dismissed* 为 ``None`` 表示**保持盘上现有的那一份**.

    默认保持而不是清空: 这个函数有若干调用方只关心 items (例如"撤销移除"要原样
    写回一份旧列表), 让它们顺手把移除记录抹掉会让手删悄悄失效 —— 而失效的表现是
    "我删掉的债又回来了", 与这个集合当初要解决的问题一模一样。
    """
    path = watchlist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    keep = load_watchlist_file()["dismissed"] if dismissed is None else {
        str(c) for c in dismissed if c}
    payload = {
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "items": [_json_ready(dict(item)) for item in items],
        "dismissed": sorted(keep),
    }
    atomic_write_json(path, payload, sort_keys=False)
    return path


def _json_ready(value: Any) -> Any:
    """转成 watchlist.json 可安全保存的结构."""
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _json_ready(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(v) for v in value]
    return value


# 加入时持久化的可选快照字段 (由 GUI 提供): 让用户回头能看到加入瞬间的研究信号
_WATCHLIST_SNAPSHOT_FIELDS = (
    "snapshot_deviation",        # 加入瞬间的 (理论 − 市价) / 理论
    "snapshot_market_price",
    "snapshot_theoretical_price",
)


# 扫新债/批量结果带来的条款与状态字段. 这些字段用于关注池展示与复盘,
# 不覆盖加入瞬间的 snapshot_* 研究信号。
_WATCHLIST_METADATA_FIELDS = (
    "issue_date",
    "listing_date",
    "tradable_date",
    "days_to_trade",
    "K",
    "market_price",
    "credit_rating",
    "outstanding_balance",
    "maturity_date",
    "is_tradable",
    "trading_status",
    "underlying_name",
)


#: ``add_to_watchlist`` 的两种语义。写成显式实参而不是布尔开关, 是因为调用点
#: 读起来就是意图本身 —— ``source="auto"`` 一眼能看出"这是后台扫描, 要守用户的手删"。
_SOURCES = ("manual", "auto")


def add_to_watchlist(new_items: Iterable[dict], *,
                     source: str = "manual") -> tuple[list[dict], int]:
    """新增关注; 已存在的代码会被跳过. 返回 (最新关注池, 新增条数).

    *source* 决定怎么对待用户手删过的代码 (``dismissed``):

    - ``"auto"`` —— 后台扫描 (启动首屏 / 缓存加载 / 批量重算前的自动补新债)。
      **跳过** dismissed 里的代码。没有这道闸, 用户右键删掉一只在途新债,
      下次开 GUI 它就带着新的 ``added_at`` 回来了, 而状态栏当时明明报过
      「已从关注池移除 1 只」—— 用户的操作被系统无声撤销。
    - ``"manual"`` —— 用户显式动作 (「⭐ 加入关注池」/「🆕 扫新债」)。
      把这些代码从 dismissed 里**解除**: 显式手动加入压过历史手删, 否则
      "我删了又想加回来"这件事就没有出口了。

    只影响**新增**那一支: 已在池里的条目走 metadata 更新分支, 与 dismissed 无关。
    """
    if source not in _SOURCES:
        raise ValueError(f"未知 source: {source!r}; 允许 {list(_SOURCES)}")
    state = load_watchlist_file()
    current = state["items"]
    dismissed = set(state["dismissed"])
    by_code = {item["bond_code"]: item for item in current}
    added = 0
    changed = False
    for item in new_items:
        code = item.get("bond_code") if isinstance(item, dict) else None
        if not code:
            continue
        keep = (
            "bond_code", "bond_name", "stock_code",
            *_WATCHLIST_METADATA_FIELDS,
            *_WATCHLIST_SNAPSHOT_FIELDS,
        )
        if code not in by_code and code in dismissed:
            if source == "auto":
                continue                      # 用户删过, 后台不许替他加回来
            dismissed.discard(code)           # 显式加入 = 收回那次手删
            changed = True
        if code in by_code:
            entry = by_code[code]
            for key in ("bond_name", "stock_code"):
                value = item.get(key)
                if value is not None and _json_ready(entry.get(key)) != _json_ready(value):
                    entry[key] = value
                    changed = True
            for key in _WATCHLIST_METADATA_FIELDS:
                if key not in item:
                    continue          # 调用方没提这个字段 → 保持原值
                value = item.get(key)
                if value is None:
                    # 显式传 None = "这个字段现在确实没有值" (例: 新债上市日尚未公告)。
                    # 只 enrich 不 clear 的话, 一次写进来的错值就再也洗不掉了 —
                    # 扫新债重扫也修不好, 因为新结果里的 None 会被当成"没提供"跳过。
                    if key in entry:
                        del entry[key]
                        changed = True
                    continue
                if _json_ready(entry.get(key)) != _json_ready(value):
                    entry[key] = value
                    changed = True
            continue
        entry = {k: v for k, v in item.items() if k in keep and v is not None}
        entry["bond_code"] = code
        entry["added_at"] = datetime.now().isoformat(timespec="seconds")
        current.append(entry)
        by_code[code] = entry
        added += 1
        changed = True
    if changed:
        save_watchlist(current, dismissed=dismissed)
    return current, added


def remove_from_watchlist(codes: Iterable[str]) -> list[dict]:
    """移除并**记下这次手删**, 免得后台扫描把它加回来.

    记的是"用户明确不想再看到它", 所以按口径4 不设过期规则 —— 要回来只能靠显式
    动作 (「🆕 扫新债」或「⭐ 加入关注池」), 见 :func:`add_to_watchlist` 的 *source*。
    """
    code_set = {str(c) for c in codes if c}
    state = load_watchlist_file()
    current = state["items"]
    kept = [item for item in current if item.get("bond_code") not in code_set]
    if len(kept) != len(current):
        save_watchlist(kept, dismissed=state["dismissed"] | code_set)
    return kept


def undo_remove(items: Sequence[dict]) -> list[dict]:
    """把一份旧列表原样写回, 并解除其中代码的手删标记.

    走 ``save_watchlist`` 而不是 ``add_to_watchlist``: 后者会给新条目重写
    ``added_at``, 于是"撤销"会把"我什么时候开始关注它"这条信息抹掉。
    """
    restored = [dict(it) for it in items]
    codes = {str(it.get("bond_code")) for it in restored if it.get("bond_code")}
    save_watchlist(restored, dismissed=load_watchlist_file()["dismissed"] - codes)
    return restored
