"""下修事件覆盖层.

把 *条款* (募集说明书的再观察期) 与 *事件* (董事会决议不修正) 解耦:

- 静态条款层: ``BondTerms.down_reset_cooldown_months`` (写在 ``data/cb_data.json``,
  发行后稳定不变, 可由 Wind/募集说明书一次性补齐).
- 事件层: ``data/down_reset_overrides.json``, 一债一条, 记录某次"不修正"公告日 +
  期满后的强度衰减系数, 文件结构::

      {
        "_meta": {"updated_at": "..."},
        "118027.SH": {
          "announce_date": "2026-04-13",
          "p_scale_after_cooldown": 0.3,
          "note": "宏图转债 2026-04-13 公告不修正"
        }
      }

``resolve_down_reset`` 把两层合并成 pricer 需要的 ``(block_until, p_scale, note)``
三元组. 显式手填的 ``BondTerms.down_reset_block_until`` 仍然优先 (硬 override).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from .data_providers import BondTerms, _add_months, to_date

logger = logging.getLogger(__name__)

DEFAULT_COOLDOWN_MONTHS = 6  # 多数 A 股转债募集说明书写 6 个月, 缺失时的兜底


def project_overrides_path() -> Path:
    """``repo_root/data/down_reset_overrides.json``."""
    return Path(__file__).resolve().parent.parent / "data" / "down_reset_overrides.json"


@dataclass
class ResolvedDownReset:
    block_until: Optional[date]
    p_scale: Optional[float]
    note: Optional[str]
    cooldown_months: Optional[float]
    announce_date: Optional[date]


class DownResetOverrides:
    """加载事件覆盖层 JSON 并按 bond_code 查询."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else project_overrides_path()
        self._data: dict = {}
        self._load()

    def _load(self):
        if not self.path.exists():
            self._data = {}
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self._data = json.load(f)
        except Exception as e:
            logger.warning("down_reset_overrides %s 解析失败: %s; 视为空", self.path, e)
            self._data = {}

    def get(self, bond_code: str) -> Optional[dict]:
        v = self._data.get(bond_code)
        return v if isinstance(v, dict) else None

    def _save(self):
        meta = self._data.get("_meta", {})
        meta["updated_at"] = datetime.now().isoformat(timespec="seconds")
        self._data["_meta"] = meta
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2, sort_keys=True)
        tmp.replace(self.path)

    def set(self, bond_code: str, *,
            announce_date: Optional[date],
            p_scale_after_cooldown: Optional[float],
            note: Optional[str] = None) -> None:
        """写入或更新一条事件覆盖. announce_date=None 等价于 delete."""
        if announce_date is None and p_scale_after_cooldown is None and not note:
            self.delete(bond_code)
            return
        entry: dict = {}
        if announce_date is not None:
            entry["announce_date"] = announce_date.isoformat()
        if p_scale_after_cooldown is not None:
            entry["p_scale_after_cooldown"] = float(p_scale_after_cooldown)
        if note:
            entry["note"] = str(note)
        self._data[bond_code] = entry
        self._save()

    def delete(self, bond_code: str) -> bool:
        if bond_code in self._data:
            del self._data[bond_code]
            self._save()
            return True
        return False


_default_overrides: Optional[DownResetOverrides] = None


def default_overrides() -> DownResetOverrides:
    global _default_overrides
    if _default_overrides is None:
        _default_overrides = DownResetOverrides()
    return _default_overrides


def reload_default_overrides() -> DownResetOverrides:
    """强制重新加载磁盘上的 overrides (GUI 保存后调用)."""
    global _default_overrides
    _default_overrides = DownResetOverrides()
    return _default_overrides


def resolve_down_reset(
    bond_code: str,
    terms: BondTerms,
    overrides: Optional[DownResetOverrides] = None,
) -> ResolvedDownReset:
    """合并条款 + 事件层, 给 pricer 一组现成参数.

    优先级 (高 → 低):
      1. ``terms.down_reset_block_until`` 显式硬 override
      2. 事件层 ``announce_date + cooldown_months`` 计算的 block_until
      3. 无 (block_until = None, 即不屏蔽)

    p_scale: 事件层 ``p_scale_after_cooldown`` 优先, 否则用 ``terms.down_reset_p_scale``.
    """
    ov = (overrides or default_overrides()).get(bond_code) or {}
    announce_date = to_date(ov.get("announce_date")) if ov else None
    event_note = None
    if announce_date is None:
        try:
            from .cb_events import events_for_down_reset
            rejected = [
                e for e in events_for_down_reset(bond_code)
                if e.event_type == "down_reset_rejected"
            ]
            if rejected:
                latest = max(rejected, key=lambda e: e.event_date)
                announce_date = latest.event_date
                event_note = latest.raw_title
        except Exception:
            event_note = None

    cooldown = terms.down_reset_cooldown_months
    if announce_date is not None and cooldown is None:
        cooldown = DEFAULT_COOLDOWN_MONTHS
        logger.info(
            "%s 有不修正公告 (%s) 但 cooldown_months 缺失, 兜底 %d 个月",
            bond_code, announce_date, DEFAULT_COOLDOWN_MONTHS,
        )

    if terms.down_reset_block_until is not None:
        block_until = terms.down_reset_block_until
    elif announce_date is not None and cooldown is not None:
        block_until = _add_months(announce_date, int(round(float(cooldown))))
    else:
        block_until = None

    if "p_scale_after_cooldown" in ov:
        p_scale = ov.get("p_scale_after_cooldown")
        p_scale = float(p_scale) if p_scale is not None else None
    else:
        p_scale = terms.down_reset_p_scale

    note_parts = []
    if announce_date is not None:
        note_parts.append(f"announce={announce_date.isoformat()}")
    if ov.get("note"):
        note_parts.append(str(ov["note"]))
    if event_note:
        note_parts.append(str(event_note))
    if terms.down_reset_note:
        note_parts.append(terms.down_reset_note)
    note = " | ".join(note_parts) if note_parts else None

    return ResolvedDownReset(
        block_until=block_until,
        p_scale=p_scale,
        note=note,
        cooldown_months=cooldown,
        announce_date=announce_date,
    )
