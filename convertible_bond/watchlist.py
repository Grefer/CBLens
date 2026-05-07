"""用户自定义关注池持久化."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence


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
        "items": [dict(item) for item in items],
    }
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    tmp.replace(path)
    return path


# 加入时持久化的可选快照字段 (由 GUI 提供): 让用户回头能看到加入瞬间的研究信号
_WATCHLIST_SNAPSHOT_FIELDS = (
    "snapshot_deviation",        # 加入瞬间的 (理论 − 市价) / 理论
    "snapshot_opportunity_score",
    "snapshot_market_price",
    "snapshot_theoretical_price",
)


def add_to_watchlist(new_items: Iterable[dict]) -> tuple[list[dict], int]:
    """新增关注; 已存在的代码会被跳过. 返回 (最新关注池, 新增条数)."""
    current = load_watchlist()
    by_code = {item["bond_code"]: item for item in current}
    added = 0
    for item in new_items:
        code = item.get("bond_code") if isinstance(item, dict) else None
        if not code or code in by_code:
            continue
        keep = ("bond_code", "bond_name", "stock_code", *_WATCHLIST_SNAPSHOT_FIELDS)
        entry = {k: v for k, v in item.items() if k in keep and v is not None}
        entry["bond_code"] = code
        entry["added_at"] = datetime.now().isoformat(timespec="seconds")
        current.append(entry)
        by_code[code] = entry
        added += 1
    if added:
        save_watchlist(current)
    return current, added


def remove_from_watchlist(codes: Iterable[str]) -> list[dict]:
    code_set = {str(c) for c in codes if c}
    current = load_watchlist()
    kept = [item for item in current if item.get("bond_code") not in code_set]
    if len(kept) != len(current):
        save_watchlist(kept)
    return kept
