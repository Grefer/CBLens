"""用户自定义关注池持久化."""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from collections.abc import Iterable, Sequence
from typing import Any


def watchlist_path() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "watchlist.json"


def load_watchlist() -> list[dict]:
    path = watchlist_path()
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
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


def save_watchlist(items: Sequence[dict]) -> Path:
    path = watchlist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "items": [_json_ready(dict(item)) for item in items],
    }
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    tmp.replace(path)
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
    "snapshot_opportunity_score",
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


def add_to_watchlist(new_items: Iterable[dict]) -> tuple[list[dict], int]:
    """新增关注; 已存在的代码会被跳过. 返回 (最新关注池, 新增条数)."""
    current = load_watchlist()
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
        if code in by_code:
            entry = by_code[code]
            for key in ("bond_name", "stock_code", *_WATCHLIST_METADATA_FIELDS):
                value = item.get(key)
                if value is not None and _json_ready(entry.get(key)) != _json_ready(value):
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
        save_watchlist(current)
    return current, added


def remove_from_watchlist(codes: Iterable[str]) -> list[dict]:
    code_set = {str(c) for c in codes if c}
    current = load_watchlist()
    kept = [item for item in current if item.get("bond_code") not in code_set]
    if len(kept) != len(current):
        save_watchlist(kept)
    return kept
