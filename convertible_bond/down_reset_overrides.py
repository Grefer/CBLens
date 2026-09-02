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

``resolve_down_reset`` 把 cb_events + 手工覆盖 + 条款字段合并成 pricer 需要的
``(block_until, p_scale, note)`` 三元组. 手工 ``down_reset_overrides.json``
优先; 其次使用 cb_events 中最新"不下修"公告, 避免旧的 cb_data 状态挡住后续公告;
``BondTerms.down_reset_block_until`` 仅作为无事件时的 fallback。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from .data_providers import BondTerms, _add_months, to_date
from .paths import data_path
from .market_time import market_today

logger = logging.getLogger(__name__)

DEFAULT_COOLDOWN_MONTHS = 6  # 多数 A 股转债募集说明书写 6 个月, 缺失时的兜底
# 已提议下修改用"一次性近确定下修节点"建模 (见 resolve_down_reset_intensity)。
# 经 cb_events 历史校准 (cli/calibrate_down_reset): 提议→通过通过率 96.4% (有终态口径) /
# 93.0% (含未决保守口径), 提议→通过滞后中位 17 天。旧的"已提议 × 倍数"语义已被
# scheduled reset 取代。**这几个数会随事件表增长而漂**: 上一版写的 100%/83% 是 2024-05 起
# 那批样本, 到 2026-09 已变 —— 改这里请重跑校准, 别照抄。
PROPOSED_PASS_PROB = 0.95  # 董事会提议后被股东会通过的概率 (2026-09-02 校准: 96.4% / 93.0%)
PROPOSED_EFFECTIVE_LAG_DAYS = 17  # 提议公告 → 通过公告的滞后 (校准: 中位 17 / 均值 19 天)
# 已通过待生效 (approved-pending): 股东会已通过、新转股价尚未到生效日的窗口。
# 下修已是定局, 通过率视为 1.0; 公告未给生效日时按这个滞后兜底估算。
APPROVED_PASS_PROB = 1.0  # 已通过 → 下修必然落地
APPROVED_EFFECTIVE_LAG_DAYS = 7  # 通过公告 → 新转股价生效的滞后 (登记日次日, 缺失时兜底)


def project_overrides_path() -> Path:
    """``repo_root/data/down_reset_overrides.json``."""
    return data_path("down_reset_overrides.json", seed=True)


@dataclass
class ResolvedDownReset:
    block_until: date | None
    p_scale: float | None
    note: str | None
    cooldown_months: float | None
    announce_date: date | None
    proposal_date: date | None = None
    # 已通过待生效: 股东会通过、新转股价尚未到生效日 (生效日 > 估值日才置位, 防双计)。
    approved_date: date | None = None
    approved_effective_date: date | None = None
    # 公告里解析到的下修后新转股价 (元/股); 缺失时定价回落到下限/premium 估算。
    announced_new_k: float | None = None


@dataclass(frozen=True)
class DownResetIntensity:
    """下修强度的基础值与事件调整后模型值.

    block_until 不在这里归零: 冻结窗口由 pricer 的
    ``down_reset_block_until`` 在时间维度上处理; effective_p_down 表示窗口外
    模型会采用的年化下修强度 (背景 hazard)。

    已公告下修 (已提议 / 已通过待生效) 不叠加到 effective_p_down, 而是输出一个
    ``scheduled_reset_*`` 一次性下修节点, 由 pricer 在预期生效日附近近确定地施加。
    ``scheduled_reset_kind`` 区分 "proposed"(待股东会) 与 "approved"(已通过待生效)。
    """
    base_p_down: float
    effective_p_down: float
    p_scale: float | None
    redemption_mode: bool = False
    scheduled_reset_date: date | None = None
    scheduled_reset_prob: float = 0.0
    scheduled_reset_kind: str | None = None  # "proposed" | "approved" | None
    scheduled_reset_target_k: float | None = None  # 公告新 K; None 时 pricer 用下限/premium 估算


def resolve_down_reset_intensity(
    base_p_down: float,
    resolved: ResolvedDownReset | None,
    *,
    p_scale_override: float | None = None,
    redemption_mode: bool = False,
    current_k: float | None = None,
) -> DownResetIntensity:
    """把基础 p_down 与公告事件合成为模型实际强度.

    背景态: effective_p_down = base · p_scale (年化 hazard)。
    已公告态: 不再抬升背景强度, 而是输出一次性下修节点
    (scheduled_reset_*), 由 pricer 在预期生效日附近近确定地施加。两种子态:
      - proposed (待股东会): 生效日 ≈ 提议日 + PROPOSED_EFFECTIVE_LAG_DAYS,
        概率 = PROPOSED_PASS_PROB。
      - approved (已通过待生效): 生效日 = resolved.approved_effective_date,
        概率 = APPROVED_PASS_PROB (≈1); 已通过优先于已提议。
    强赎模式: 背景强度与已公告节点都归零 (终点已是赎回/转股二择一)。

    ``current_k`` 给定时对公告解析到的新 K 做**方向校验**: 下修不可能抬高转股价,
    所以 ``announced_new_k > current_k`` 恒为解析失败的信号, 此时丢弃它、回落到
    pricer 的 premium/floor 估算 (见下方注释里的实测)。等于现 K 的照留 —— 那是
    "下修已落地"的正常状态, pricer 靠它把节点变 no-op 防双计。
    """
    base = max(0.0, float(base_p_down))
    p = base
    p_scale = p_scale_override
    if p_scale is None:
        p_scale = getattr(resolved, "p_scale", None)
    if p_scale is not None:
        p *= max(0.0, float(p_scale))
    scheduled_reset_date: date | None = None
    scheduled_reset_prob = 0.0
    scheduled_reset_kind: str | None = None
    proposal_date = getattr(resolved, "proposal_date", None)
    approved_effective = getattr(resolved, "approved_effective_date", None)
    announced_new_k = getattr(resolved, "announced_new_k", None)
    scheduled_reset_target_k = None
    if not redemption_mode:
        # 已通过待生效优先 (更确定); 否则用已提议节点。
        if approved_effective is not None:
            scheduled_reset_date = approved_effective
            scheduled_reset_prob = APPROVED_PASS_PROB
            scheduled_reset_kind = "approved"
        elif proposal_date is not None:
            scheduled_reset_date = proposal_date + timedelta(days=PROPOSED_EFFECTIVE_LAG_DAYS)
            scheduled_reset_prob = PROPOSED_PASS_PROB
            scheduled_reset_kind = "proposed"
        if scheduled_reset_kind is not None and announced_new_k is not None:
            scheduled_reset_target_k = float(announced_new_k)
            # 方向校验: 下修只会**降低**转股价, 解析出严格高于现 K 的新 K 一定是解析错了。
            #
            # 实测全库 147 条带 event_price 的下修提议/通过事件里 106 条 (72%) 方向不可能:
            # 下修公告正文开头会成段引用"历次转股价格调整情况", 而
            # ``parse_down_reset_new_price`` 取的是**第一个**"由 A 元/股 修正为 B 元/股",
            # 于是抓到的是几年前那次调整的 B。例: 强力转债 2026-08-07 提议公告正文里依次
            # 出现 18.98/18.98/18.94/18.94/18.90/12.70, 解析结果 18.94 (2021 年的值),
            # 而当时 K 已经是 12.70。
            #
            # 这个错值**不会算出错价** —— pricer 的节点是 max(V, reset_value), target_k
            # 偏高只会让 reset_value 低于 V, 节点静默变 no-op。代价是"下修价值"被抹平:
            # 实测主池当时仅有的 2 个在途下修节点 (晶能转债 target_k=13.7 vs K=6.35、
            # 强力转债 18.94 vs 12.70) uplift 都只剩 0.02%/0.04%。丢掉这个值改用
            # premium/floor 估算, 反而能把下修价值还原回来。
            #
            # 闸是 **>** 不是 >=: ``target_k == 现 K`` 是"下修已落地、条款已刷新"的正常状态,
            # pricer 靠它把节点变 no-op 来防双计, 拦掉反而会让已落地的下修再算一遍。
            if current_k is not None and scheduled_reset_target_k > float(current_k):
                scheduled_reset_target_k = None

    if redemption_mode:
        p = 0.0
    return DownResetIntensity(
        base_p_down=base,
        effective_p_down=p,
        p_scale=p_scale,
        redemption_mode=redemption_mode,
        scheduled_reset_date=scheduled_reset_date,
        scheduled_reset_prob=scheduled_reset_prob,
        scheduled_reset_kind=scheduled_reset_kind,
        scheduled_reset_target_k=scheduled_reset_target_k,
    )


class DownResetOverrides:
    """加载事件覆盖层 JSON 并按 bond_code 查询."""

    def __init__(self, path: Path | None = None):
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

    def get(self, bond_code: str) -> dict | None:
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
            announce_date: date | None,
            p_scale_after_cooldown: float | None,
            note: str | None = None) -> None:
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


_default_overrides: DownResetOverrides | None = None


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
    overrides: DownResetOverrides | None = None,
    *,
    valuation_date: date | None = None,
    event_store=None,
) -> ResolvedDownReset:
    """合并条款 + 事件层, 给 pricer 一组现成参数.

    优先级 (高 → 低):
      1. ``down_reset_overrides.json`` 显式公告日/强度覆盖
      2. cb_events 中最新不下修事件的 ``effective_end``
      3. 事件公告日 + cooldown_months 计算的 block_until
      4. ``terms.down_reset_block_until`` 历史字段 fallback
      5. 无 (block_until = None, 即不屏蔽)

    p_scale: 事件层 ``p_scale_after_cooldown`` 优先, 否则用 ``terms.down_reset_p_scale``.
    ``event_store`` 允许历史 provider/CLI 使用自定义事件文件; 缺省仍读取项目事件表。
    """
    raw_ov = (overrides or default_overrides()).get(bond_code) or {}
    announce_date = to_date(raw_ov.get("announce_date")) if raw_ov else None
    # 历史回测不能看到估值日之后才公告的手工覆盖。没有 announce_date 的旧式
    # 覆盖无法判定生效日, 仍按显式人工参数处理。
    if announce_date is not None and valuation_date is not None and announce_date > valuation_date:
        ov = {}
        announce_date = None
    else:
        ov = raw_ov
    event_block_until = None
    event_cooldown = None
    event_note = None
    proposal_date = None
    approved_date = None
    approved_effective_date = None
    announced_new_k = None
    down_events = []
    try:
        from .cb_events import events_for_down_reset
        down_events = events_for_down_reset(
            bond_code,
            store=event_store,
            through_date=valuation_date,
        )
    except Exception:
        down_events = []
    if announce_date is None:
        try:
            rejected = [
                e for e in down_events
                if e.event_type == "down_reset_rejected"
            ]
            if rejected:
                latest = max(rejected, key=lambda e: e.event_date)
                announce_date = latest.event_date
                event_block_until = latest.effective_end
                event_cooldown = latest.commitment_months
                event_note = latest.raw_title
        except Exception:
            event_note = None
    proposed = [e for e in down_events if e.event_type == "down_reset_proposed"]
    terminal = [
        e for e in down_events
        if e.event_type in {"down_reset_rejected", "down_reset_approved"}
    ]
    if proposed:
        latest_proposed = max(proposed, key=lambda e: e.event_date)
        latest_terminal = max(terminal, key=lambda e: e.event_date) if terminal else None
        if latest_terminal is None or latest_proposed.event_date > latest_terminal.event_date:
            proposal_date = latest_proposed.event_date
            announced_new_k = getattr(latest_proposed, "event_price", None)

    # 已通过待生效: 通过事件是最新下修事件, 且新转股价生效日仍在估值日之后。
    # 生效日已过的下修走条款刷新, 不在此叠加 (防双计)。公告未给生效日时按滞后兜底估算。
    approved = [e for e in down_events if e.event_type == "down_reset_approved"]
    if approved:
        latest_approved = max(approved, key=lambda e: e.event_date)
        is_latest = all(
            latest_approved.event_date >= e.event_date
            for e in down_events if e is not latest_approved
        )
        if is_latest:
            # ``effective_start`` 只有**真解析到**才算数 —— 与 delisting /
            # call_redemption 的 last_trading_date 是同一条闸: 它在标题/正文都没有日期时
            # 会回落成公告日本身 (parse_event_from_announcement 的通用回落), 而
            # ``parse_event_from_announcement`` 对 down_reset_approved **只解析 event_price,
            # 不解析生效日** —— 于是 effective_start 恒等于 event_date、effective_end 恒为
            # None (实测全库 113/113)。
            #
            # 而 ``events_for_down_reset(through_date=valuation_date)`` 已经保证
            # ``event_date <= valuation_date``, 所以下面的 ``eff > cmp_date`` **恒为假**,
            # ``APPROVED_EFFECTIVE_LAG_DAYS`` 那条兜底一行都执行不到 —— regime ②
            # (已通过待生效) 从来没有建过节点。
            eff = latest_approved.effective_end
            if eff is None and (latest_approved.effective_start
                                and latest_approved.effective_start > latest_approved.event_date):
                eff = latest_approved.effective_start
            if eff is None:
                eff = latest_approved.event_date + timedelta(days=APPROVED_EFFECTIVE_LAG_DAYS)
            cmp_date = valuation_date or market_today()
            if eff > cmp_date:
                approved_date = latest_approved.event_date
                approved_effective_date = eff
                # 已通过覆盖同券更早的"已提议"节点
                proposal_date = None
                announced_new_k = getattr(latest_approved, "event_price", None)

    cooldown = event_cooldown if event_cooldown is not None else terms.down_reset_cooldown_months
    if announce_date is not None and event_block_until is None and cooldown is None:
        cooldown = DEFAULT_COOLDOWN_MONTHS
        logger.info(
            "%s 有不修正公告 (%s) 但 cooldown_months 缺失, 兜底 %d 个月",
            bond_code, announce_date, DEFAULT_COOLDOWN_MONTHS,
        )

    if event_block_until is not None:
        block_until = event_block_until
    elif announce_date is not None and cooldown is not None:
        block_until = _add_months(announce_date, int(round(float(cooldown))))
    elif terms.down_reset_block_until is not None:
        block_until = terms.down_reset_block_until
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
    if event_block_until is not None:
        note_parts.append(f"event_end={event_block_until.isoformat()}")
    if ov.get("note"):
        note_parts.append(str(ov["note"]))
    if event_note:
        note_parts.append(str(event_note))
    if proposal_date is not None:
        note_parts.append(f"proposal={proposal_date.isoformat()}")
    if approved_effective_date is not None:
        note_parts.append(f"approved_effective={approved_effective_date.isoformat()}")
    if announced_new_k is not None:
        note_parts.append(f"new_k={announced_new_k:g}")
    if terms.down_reset_note:
        note_parts.append(terms.down_reset_note)
    note = " | ".join(note_parts) if note_parts else None

    return ResolvedDownReset(
        block_until=block_until,
        p_scale=p_scale,
        note=note,
        cooldown_months=cooldown,
        announce_date=announce_date,
        proposal_date=proposal_date,
        approved_date=approved_date,
        approved_effective_date=approved_effective_date,
        announced_new_k=announced_new_k,
    )
