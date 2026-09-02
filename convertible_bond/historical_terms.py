"""历史条款/事件视角的数据源装饰器.

策略回测最怕"未来信息": 当前 ``cb_data`` 里的转股价、强赎状态、摘牌状态等
直接用于过去日期, 会让历史选债结果失真。本模块把三层历史信息合成一个
DataProvider:

  1. cb_data 历史快照: ``cb_data_history/YYYY-MM-DD.json``
  2. 条款变更 patch: ``cb_terms_patches.json`` (如下修后的转股价)
  3. 公告事件表: ``cb_events.json`` 中 ``event_date <= valuation_date`` 的事件

动态行情仍完全透传给内层 provider。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields, replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any, get_args, get_origin, get_type_hints

from .cache import TermsBundle
from .cb_events import CBEventStore, apply_events_to_terms, project_events_path
from .data_providers import BondTerms, DataProvider, finite_float, to_date
from .paths import data_dir, data_path


_BOND_FIELDS = tuple(fields(BondTerms))
_BOND_FIELD_NAMES = frozenset(f.name for f in _BOND_FIELDS)
_BOND_HINTS = get_type_hints(BondTerms)


def project_terms_patches_path() -> Path:
    """项目级条款 patch 默认路径."""
    return data_path("cb_terms_patches.json")


def project_terms_history_dir() -> Path:
    """项目级 cb_data 历史快照默认目录."""
    return data_dir("cb_data_history")


# 权威源: 直接来自数据终端的 as-of 序列, 不经公告正文解析。
# 同一 (债, 字段) 上只要存在权威源 patch, 就完全忽略解析源的 —— 二者按生效日混排会让一条
# 日期更晚的解析错值盖掉正确值。实测: cb-sync-events 会为 127112.SZ 尚太转债 写入
# 2026-07-03 K=84.72, 而 Wind as-of 的真值是 60.02; 按日期取最后一条就取到了错的。
_AUTHORITATIVE_PATCH_SOURCES = frozenset({"wind_asof"})

# **快照覆盖不到的字段**: cb_data 里这两个字段永远是空的 —— Wind 的 ``ratingoutlook`` /
# ``creditratingoutlook`` 实测取不到 (0/1058), CBEvent 里也不带它们, 公告正文解析写进
# patch 库是唯一来源。
#
# 对它们, ``terms_as_of`` 那条"快照已含更早的变更、所以更早的 patch 不带来新信息"的推理
# **不成立** —— 快照里根本是空的。照常裁剪的后果是这两个字段永远进不了定价视图: patch 库
# 里躺着 986 条展望 + 84 条观察, 而主池 265/285 只债的展望在 live 路径上被整段丢掉。
#
# 反过来 ``credit_rating`` **不在**这个集合里, 而且不能加进来: cb_data 的评级由
# ``cb-sync-ratings`` 从 akshare 第三方刷新 (实测与第三方 100% 一致), 而公告解析的评级
# 拿第三方当裁判时 17 条分歧里 15 条是公告错。快照覆盖得到的字段, 就该由快照说了算。
_SNAPSHOT_UNCOVERED_FIELDS = frozenset({"credit_rating_outlook", "credit_watch_status"})


def _drop_shadowed_patches(patches: list["TermsPatch"]) -> list["TermsPatch"]:
    """**逐债逐字段**: 某只债的某个字段有权威源, 就丢掉**这只债**该字段的解析源 patch。

    权威字段集必须按 ``bond_code`` 分组算。此前它是在**整个传进来的列表**上算的 ——
    ``list_patches(bond_code=...)`` 传的是单债列表所以没事 (投影路径 ``apply()`` 走的
    正是这一条), 但全库调用 (数据体检、存量回洗) 传的是所有债, 于是**A 债的一条
    wind_asof patch 会遮蔽 B 债该字段的解析 patch**, 而 B 债在那个字段上可能根本没有
    权威源。实测差 2 条 (逐债累计 22724 vs 全库一次 22722) —— 今天很小, 但它随解析
    patch 累积增长, 且方向是"越修越看不见"。

    docstring 与 AGENTS 记的规则一直都是"同一只债的同一个字段", 是实现漏了分组。
    """
    by_bond: dict[str, set[str]] = {}
    for patch in patches:
        if patch.source in _AUTHORITATIVE_PATCH_SOURCES:
            by_bond.setdefault(patch.bond_code, set()).update(patch.fields or {})
    if not by_bond:
        return patches
    kept: list[TermsPatch] = []
    for patch in patches:
        if patch.source in _AUTHORITATIVE_PATCH_SOURCES:
            kept.append(patch)
            continue
        shadowed = by_bond.get(patch.bond_code) or set()
        fields = {k: v for k, v in (patch.fields or {}).items() if k not in shadowed}
        if fields:
            kept.append(replace(patch, fields=fields))
    return kept


@dataclass(frozen=True)
class TermsPatch:
    """一条或一组从某日起生效的条款字段更新."""

    bond_code: str
    effective_date: date
    fields: dict[str, Any]
    source: str = "manual"
    note: str | None = None
    event_date: date | None = None
    before_fields: dict[str, Any] | None = None
    raw_title: str | None = None
    confidence: str = "manual"
    source_event_key: str | None = None

    def key(self) -> tuple:
        return (
            self.bond_code,
            self.effective_date.isoformat(),
            self.event_date.isoformat() if self.event_date else "",
            tuple(sorted((str(k), _jsonable_patch_value(v)) for k, v in self.fields.items())),
            self.raw_title or "",
        )


@dataclass(frozen=True)
class TermsProjection:
    """估值日视角下已应用 patch 和事件后的条款."""

    terms: BondTerms
    applied_patches: tuple[TermsPatch, ...]
    patch_fields: frozenset[str]


class TermsPatchStore:
    """读取 ``cb_terms_patches.json`` 并按估值日应用条款变更.

    支持两种 JSON 形态:

    ``{"patches": [{"bond_code": "...", "effective_date": "...",
    "field": "conversion_price", "value": 12.34}]}``

    或者一次更新多个字段:
    ``{"patches": [{"bond_code": "...", "effective_date": "...",
    "fields": {"conversion_price": 12.34, "credit_rating": "AA"}}]}``
    """

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else project_terms_patches_path()
        self._patches: list[TermsPatch] = []
        self._meta: dict = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            self._patches = []
            self._meta = {}
            return
        with open(self.path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        self._meta = dict(payload.get("_meta", {}))
        rows = payload.get("patches")
        if rows is None:
            # 兼容早期讨论里的 "events" 命名。
            rows = payload.get("events", [])
        self._patches = []
        for row in rows:
            patch = _patch_from_json(row)
            if patch is not None:
                self._patches.append(patch)

    def _save(self) -> None:
        from datetime import datetime

        self.path.parent.mkdir(parents=True, exist_ok=True)
        meta = dict(getattr(self, "_meta", {}))
        meta["updated_at"] = datetime.now().isoformat(timespec="seconds")
        payload = {
            "_meta": meta,
            "patches": [_patch_to_json(p) for p in sorted(self._patches, key=_patch_sort_key)],
        }
        tmp = self.path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
        tmp.replace(self.path)

    def list_patches(
        self,
        bond_code: str | None = None,
        through_date: date | None = None,
        after: date | None = None,
        include_shadowed: bool = False,
    ) -> list[TermsPatch]:
        """列出 patch。*after* 排除生效日 <= 该日的条目 —— 基础条款快照已经包含它们。

        默认返回**生效视图**: 被权威源逐字段遮蔽的解析 patch 不出现。数据体检与存量回洗
        要的是**文件里到底有什么**, 传 ``include_shadowed=True`` —— 否则一条被 Wind 遮蔽的
        脏 patch 会既扫不到、也删不掉, 等哪天权威源覆盖收窄就原地复活。
        """
        patches = list(self._patches)
        if bond_code:
            patches = [p for p in patches if p.bond_code == bond_code]
        if through_date:
            patches = [p for p in patches if p.effective_date <= through_date]
        if after is not None:
            patches = [p for p in patches if p.effective_date > after]
        patches = sorted(patches, key=_patch_sort_key)
        return patches if include_shadowed else _drop_shadowed_patches(patches)

    def apply(self, bond_code: str, terms: BondTerms, valuation_date: date,
              *, after: date | None = None) -> BondTerms:
        updates: dict[str, Any] = {}
        for patch in self.list_patches(
                bond_code=bond_code, through_date=valuation_date, after=after):
            for key, value in patch.fields.items():
                if key not in _BOND_FIELD_NAMES:
                    continue
                updates[key] = _coerce_bond_field_value(key, value)
        return replace(terms, **updates) if updates else terms

    def add_many(self, patches: list[TermsPatch] | tuple[TermsPatch, ...]) -> int:
        existing = {p.key(): p for p in self._patches}
        added = 0
        for patch in patches:
            if patch.key() in existing:
                continue
            existing[patch.key()] = patch
            added += 1
        self._patches = list(existing.values())
        if added:
            self._save()
        return added

    def rewrite(self, transform, *, dry_run: bool = False) -> tuple[int, int]:
        """按 *transform* 逐条重写已有 patch, 返回 ``(改写数, 删除数)``.

        *transform* 接受一条 :class:`TermsPatch`, 返回改写后的 patch, 或返回 None
        表示删除该条。用于修数据 (例如剔除误解析出来的字段); 新增走 :meth:`add_many`。
        ``dry_run`` 时只统计不落盘。
        """
        kept: list[TermsPatch] = []
        changed = 0
        removed = 0
        for patch in self._patches:
            new_patch = transform(patch)
            if new_patch is None:
                removed += 1
                continue
            if new_patch != patch:
                changed += 1
            kept.append(new_patch)
        if not dry_run and (changed or removed):
            self._patches = kept
            self._save()
        return changed, removed


_default_terms_patch_store: TermsPatchStore | None = None


def default_terms_patch_store() -> TermsPatchStore:
    global _default_terms_patch_store
    if _default_terms_patch_store is None:
        _default_terms_patch_store = TermsPatchStore()
    return _default_terms_patch_store


def reload_default_terms_patch_store() -> TermsPatchStore:
    global _default_terms_patch_store
    _default_terms_patch_store = TermsPatchStore()
    return _default_terms_patch_store


def project_terms(
    bond_code: str,
    terms: BondTerms,
    valuation_date: date,
    *,
    patch_store: TermsPatchStore | None = None,
    event_store: CBEventStore | None = None,
    apply_events: bool = True,
    terms_as_of: date | None = None,
) -> TermsProjection:
    """生成估值日视角的条款.

    顺序固定为: 基础条款 -> 条款 patch -> 公告事件状态。这样下修后的 K、
    不强赎承诺、不下修冻结等都会在定价和 GUI 使用同一个视图。

    *terms_as_of* = 基础条款这份快照的截止日。**一条 patch 只有在快照拍摄之后才生效,
    才带来新信息**; 生效日更早的那些, 快照里已经含着了。不传这个锚会让今日估值把两年前
    的下修 patch 重新盖回 Wind 的当前转股价 —— 实测 282 只有 patch 的债里 209 只的
    最新 patch 值与 cb_data 当前 K 不一致, 主池 60% 的 K 因此被写坏 (万孚转债
    20.88 → 93.57)。cb_data 的 conversion_price 是 Wind ``clause_conversion2_swapshareprice``
    即**当前 K**, 已内含全部已生效下修; 两个源冲突时它才是权威。
    """
    store = patch_store or default_terms_patch_store()
    # 全量取出后**逐字段**决定要不要按 terms_as_of 裁剪 —— 快照覆盖不到的字段不能裁
    # (见 _SNAPSHOT_UNCOVERED_FIELDS)。用 after= 一刀切会把它们连同别的字段一起丢掉。
    patches = store.list_patches(bond_code=bond_code, through_date=valuation_date)
    projected = terms
    patch_fields: set[str] = set()
    applied: list[TermsPatch] = []
    for patch in patches:
        cut = terms_as_of is not None and patch.effective_date <= terms_as_of
        updates = {
            key: _coerce_bond_field_value(key, value)
            for key, value in patch.fields.items()
            if key in _BOND_FIELD_NAMES
            and not (cut and key not in _SNAPSHOT_UNCOVERED_FIELDS)
        }
        if updates:
            projected = replace(projected, **updates)
            patch_fields.update(updates)
            applied.append(patch)
    patches = applied
    if apply_events:
        events = (event_store or CBEventStore(project_events_path())).list_events(
            bond_code=bond_code,
            through_date=valuation_date,
        )
        projected = apply_events_to_terms(
            bond_code,
            projected,
            events,
            valuation_date=valuation_date,
        )
    return TermsProjection(
        terms=projected,
        applied_patches=tuple(patches),
        patch_fields=frozenset(patch_fields),
    )


class TermsHistoryStore:
    """按日期选择 ``cb_data`` 历史快照."""

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root) if root else project_terms_history_dir()
        self._bundle_cache: dict[Path, TermsBundle] = {}

    def list_snapshot_dates(self) -> list[date]:
        if not self.root.exists():
            return []
        dates: list[date] = []
        for path in self.root.glob("*.json"):
            try:
                dates.append(date.fromisoformat(path.stem))
            except ValueError:
                continue
        return sorted(dates)

    def snapshot_date_on_or_before(self, valuation_date: date) -> date | None:
        candidates = [d for d in self.list_snapshot_dates() if d <= valuation_date]
        return candidates[-1] if candidates else None

    def get_terms(self, bond_code: str, valuation_date: date) -> tuple[BondTerms | None, date | None]:
        snapshot_date = self.snapshot_date_on_or_before(valuation_date)
        if snapshot_date is None:
            return None, None
        path = self.root / f"{snapshot_date.isoformat()}.json"
        bundle = self._bundle_cache.get(path)
        if bundle is None:
            bundle = TermsBundle(path)
            self._bundle_cache[path] = bundle
        return bundle.get(bond_code), snapshot_date


class HistoricalBondDataProvider(DataProvider):
    """给任意行情 provider 增加历史条款/事件视角."""

    def __init__(
        self,
        inner: DataProvider,
        *,
        history_store: TermsHistoryStore | None = None,
        patch_store: TermsPatchStore | None = None,
        event_store: CBEventStore | None = None,
        strip_fallback_status: bool = True,
        merge_admission_status: bool = False,
        provider_history_terms: bool = False,
    ):
        self.inner = inner
        self.history_store = history_store
        self.patch_store = patch_store or TermsPatchStore()
        self.event_store = event_store or CBEventStore(project_events_path())
        self.strip_fallback_status = strip_fallback_status
        self.merge_admission_status = merge_admission_status
        self.provider_history_terms = provider_history_terms
        self.name = f"{inner.name}+history"

    def terms_as_of(self, bond_code: str, valuation_date: date) -> date | None:
        """用到快照时返回快照日期; 回落到 inner 的当前条款时返回 None。

        None 意味着"基础条款是今天的口径", 此时**要**套用全部历史 patch 才能把 K 拉回
        当时的值 —— 这正是回测 current_fallback 分支需要的。有快照时相反: 快照已经含着
        它拍摄之前的全部变更, 只有之后生效的 patch 才是新信息。
        """
        if self.history_store is None:
            try:
                return self.inner.terms_as_of(bond_code, valuation_date)
            except Exception:
                return None
        try:
            terms, snapshot_date = self.history_store.get_terms(bond_code, valuation_date)
        except Exception:
            return None
        if terms is not None:
            return snapshot_date
        try:
            return self.inner.terms_as_of(bond_code, valuation_date)
        except Exception:
            return None

    def get_bond_terms(self, bond_code: str, valuation_date: date) -> BondTerms:
        terms = None
        snapshot_date = None
        if self.history_store is not None:
            terms, snapshot_date = self.history_store.get_terms(bond_code, valuation_date)
        if terms is None:
            # 回落到 inner。锚改问 inner 自己: Wind 的 get_bond_terms 是**真 as-of**
            # (实测 123064.SZ as-of 2025-06-30 → K=26.60, 与公告沿革吻合), 此时锚就是估值日,
            # 再套历史 patch 只会把正确值盖回旧值 (实测被盖成 93.57)。
            # akshare/CSV 没有 as-of 能力, 返回 None, 仍走"套用全部 patch 重建历史"的老路。
            terms = self.inner.get_bond_terms(bond_code, valuation_date)
            try:
                snapshot_date = self.inner.terms_as_of(bond_code, valuation_date)
            except Exception:
                snapshot_date = None
            if self.strip_fallback_status:
                terms = strip_current_status_fields(terms)

        if self.merge_admission_status:
            terms = _merge_admission_status(
                self.inner,
                bond_code,
                valuation_date,
                terms,
            )

        terms = self.patch_store.apply(
            bond_code, terms, valuation_date, after=snapshot_date)
        terms = apply_events_to_terms(
            bond_code,
            terms,
            self.event_store.list_events(bond_code=bond_code, through_date=valuation_date),
            valuation_date=valuation_date,
        )
        # ``close`` **无条件按估值日重取**, 取不到就置 None —— 不许沿用 terms 里带来的那个。
        #
        # 它与 ``strip_current_status_fields`` 管的那批状态字段不是一回事: 那些字段由
        # ``strip_fallback_status`` 开关控制 (standard 口径下刻意为 False, 因为条款来自
        # 按日期归档的历史快照), 而 ``close`` 是**市场价格**, 任何情况下都不该把别的日期
        # 的价带进这个估值日。
        #
        # 漏掉的那条路是 fallback: history_store 没有该日快照时会退回
        # ``self.inner.get_bond_terms()`` = **今天**的条款, 而 standard 口径下
        # strip_fallback_status=False 不剥它 —— 于是今天的收盘价被当成历史价。
        # 下游 ``_latest_bond_close_with_provenance`` 又会把它当 ``terms_close`` 兜底价用,
        # 而那一档此前对 deviation / 覆盖率闸完全隐形。
        close = _latest_bond_close(self.inner, bond_code, valuation_date)
        return replace(terms, close=close)

    def get_terms_source_diagnostics(self, bond_code: str, valuation_date: date) -> dict[str, Any]:
        """返回历史回测在某日使用的条款口径, 供策略回测做防未来函数提示."""
        snapshot_date = None
        has_snapshot_terms = False
        if self.history_store is not None:
            terms, snapshot_date = self.history_store.get_terms(bond_code, valuation_date)
            has_snapshot_terms = terms is not None
        patches = self.patch_store.list_patches(bond_code=bond_code, through_date=valuation_date)
        events = self.event_store.list_events(bond_code=bond_code, through_date=valuation_date)
        if has_snapshot_terms:
            terms_source = "history_snapshot"
            uses_fallback = False
        elif self.provider_history_terms or (
            self.merge_admission_status and not self.strip_fallback_status
        ):
            terms_source = "provider_history"
            uses_fallback = False
        else:
            terms_source = "current_fallback"
            uses_fallback = True
        return {
            "bond_code": bond_code,
            "valuation_date": valuation_date,
            "terms_source": terms_source,
            "snapshot_date": snapshot_date,
            "patch_count": len(patches),
            "event_count": len(events),
            "uses_current_fallback": uses_fallback,
            "strip_fallback_status": bool(self.strip_fallback_status and uses_fallback),
            "merge_admission_status": bool(self.merge_admission_status),
        }

    # 动态行情完全透传给内层 provider。
    def get_stock_close(self, stock_code, on_date):
        return self.inner.get_stock_close(stock_code, on_date)

    def get_stock_history(self, stock_code, start, end):
        return self.inner.get_stock_history(stock_code, start, end)

    def get_stock_dividend_yield(self, stock_code, on_date):
        return self.inner.get_stock_dividend_yield(stock_code, on_date)

    def get_bond_history(self, bond_code, start, end):
        return self.inner.get_bond_history(bond_code, start, end)

    def get_cashflow(self, bond_code):
        # CashflowSchedule 没有 valuation_date 参数; 透传内层会把当前 cb_data 的
        # 现金流重新覆盖到历史条款上。这里返回 None, 让调用方使用
        # get_bond_terms(valuation_date) 已经重建好的 coupon/maturity/redemption 字段。
        return None

    def get_risk_free_rate(self, on_date):
        return self.inner.get_risk_free_rate(on_date)

    def hist_vol(self, stock_code, end_date, window_days):
        return self.inner.hist_vol(stock_code, end_date, window_days)

    def list_tradable_cbs(self, on_date: date | None = None):
        return self.inner.list_tradable_cbs(on_date)


def _merge_admission_status(
    provider: DataProvider,
    bond_code: str,
    valuation_date: date,
    base_terms: BondTerms,
) -> BondTerms:
    """把数据源按估值日提供的准入/状态字段合并到基础条款上."""
    try:
        status_terms = provider.get_admission_status(
            bond_code,
            valuation_date,
            base_terms=base_terms,
        )
    except Exception:
        return base_terms
    if status_terms is None or status_terms is base_terms:
        return _strip_unannounced_future_status(base_terms, valuation_date)
    updates = {
        field.name: value
        for field in _BOND_FIELDS
        if (value := getattr(status_terms, field.name, None)) is not None
    }
    merged = replace(base_terms, **updates) if updates else base_terms
    return _strip_unannounced_future_status(merged, valuation_date)


def _strip_unannounced_future_status(terms: BondTerms, valuation_date: date) -> BondTerms:
    """Wind 部分生命周期字段会在历史 ``tradeDate`` 下暴露未来事件日期.

    这类字段只有在已经公告或已经发生时才合并进历史视角; 未来已知但当时
    不可见的强赎、最后交易、摘牌信息交给公告事件层按 ``event_date`` 应用。
    """
    call_announce = terms.call_announce_date
    call_announced = call_announce is not None and call_announce <= valuation_date
    call_redeemed = (
        terms.call_redemption_date is not None
        and terms.call_redemption_date <= valuation_date
    )
    delisted = terms.delisting_date is not None and terms.delisting_date <= valuation_date
    call_visible = call_announced or call_redeemed or delisted
    has_future_lifecycle = any(
        value is not None and value > valuation_date
        for value in (
            terms.call_announce_date,
            terms.call_redemption_date,
            terms.last_trading_date,
            terms.delisting_date,
        )
    )
    if not has_future_lifecycle:
        return terms
    updates: dict[str, Any] = {}

    if call_announce is not None and call_announce > valuation_date:
        updates["call_announce_date"] = None
    if not call_visible:
        updates.update({
            "call_status": None,
            "call_redemption_date": None,
            "call_redemption_price": None,
        })

    last_trading = terms.last_trading_date
    if last_trading is not None and last_trading > valuation_date and not call_visible:
        updates["last_trading_date"] = None

    delisting = terms.delisting_date
    if delisting is not None and delisting > valuation_date and not call_visible:
        updates["delisting_date"] = None

    return replace(terms, **updates) if updates else terms


def strip_current_status_fields(terms: BondTerms) -> BondTerms:
    """清掉最容易把当前状态泄漏到历史日期的字段.

    静态字段和半静态字段保留; 事件/日级状态交给 ``cb_events``、历史快照或
    patch store 重建。
    """
    return replace(
        terms,
        close=None,
        is_tradable=None,
        trading_status=None,
        suspension_status=None,
        call_status=None,
        call_announce_date=None,
        call_redemption_date=None,
        call_redemption_price=None,
        call_no_redemption_until=None,
        putback_start_date=None,
        putback_end_date=None,
        putback_price=None,
        conversion_suspension_start_date=None,
        conversion_suspension_end_date=None,
        conversion_suspension_status=None,
        last_trading_date=None,
        delisting_date=None,
        # **正股名字也必须剥**: ``_underlying_has_st_risk`` 判的是
        # ``f"{underlying_name} {underlying_status}"`` 的拼接串, 而 cb_data 里的
        # ``underlying_name`` 是**今天**的名字 —— 剥了 status 却留着名字, 等于让 2022 年的
        # 回测从「*ST闻泰」这四个字里读出"这家公司 2026 年会被 ST"。实测四只当前 ST 债
        # (110081/110092/118027/127093) 剥离后 ST 判据仍为 True, 全靠名字。
        #
        # 代价已量过, 是零: 这四只**都有 ``underlying_st_risk`` 事件**, 而事件在
        # ``apply_events_to_terms`` 里按估值日重放 —— 剥掉名字之后它们照样被识别,
        # 只是从**事件公告日**起而不是从回测第一天起, 那才是 point-in-time。
        # 回测路径不渲染正股名 (只有 GUI 的「正股」列用它, 那条路不走 strip)。
        underlying_name=None,
        underlying_status=None,
        underlying_trade_status=None,
        underlying_pct_change=None,
        bond_turnover_amount=None,
        down_reset_block_until=None,
        down_reset_p_scale=None,
        down_reset_note=None,
    )


def _patch_from_json(row: dict) -> TermsPatch | None:
    if not isinstance(row, dict):
        return None
    bond_code = str(row.get("bond_code") or "").strip().upper()
    raw_date = row.get("effective_date") or row.get("effective_start") or row.get("event_date")
    if not bond_code or raw_date is None:
        return None
    effective_date = to_date(raw_date)
    if effective_date is None:
        return None

    if isinstance(row.get("fields"), dict):
        patch_fields = dict(row["fields"])
    elif row.get("field"):
        patch_fields = {str(row["field"]): row.get("value")}
    else:
        patch_fields = {
            key: value
            for key, value in row.items()
            if key in _BOND_FIELD_NAMES
        }
    if not patch_fields:
        return None
    event_date = to_date(row.get("event_date")) if row.get("event_date") else None
    return TermsPatch(
        bond_code=bond_code,
        effective_date=effective_date,
        fields=patch_fields,
        source=str(row.get("source") or "manual"),
        note=row.get("note"),
        event_date=event_date,
        before_fields=dict(row["before_fields"]) if isinstance(row.get("before_fields"), dict) else None,
        raw_title=row.get("raw_title"),
        confidence=str(row.get("confidence") or "manual"),
        source_event_key=row.get("source_event_key"),
    )


def _patch_to_json(patch: TermsPatch) -> dict:
    row = asdict(patch)
    for key in ("effective_date", "event_date"):
        if isinstance(row.get(key), date):
            row[key] = row[key].isoformat()
    row["fields"] = {
        key: _jsonable_patch_value(value)
        for key, value in patch.fields.items()
    }
    if patch.before_fields is not None:
        row["before_fields"] = {
            key: _jsonable_patch_value(value)
            for key, value in patch.before_fields.items()
        }
    return {key: value for key, value in row.items() if value is not None}


def _jsonable_patch_value(value: Any):
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, tuple):
        return list(value)
    return value


def _coerce_bond_field_value(key: str, value: Any):
    if value is None:
        return None
    hint = _BOND_HINTS.get(key)
    if hint and any(t is date for t in _unwrap_type_args(hint)):
        return to_date(value)
    if hint and any(get_origin(t) is tuple for t in _unwrap_type_args(hint)):
        if isinstance(value, (list, tuple)):
            return tuple(float(v) for v in value)
    return value


def _unwrap_type_args(tp) -> tuple:
    origin = get_origin(tp)
    if origin is None:
        return (tp,)
    return get_args(tp) or (tp,)


def _patch_sort_key(patch: TermsPatch) -> tuple:
    return (
        patch.effective_date,
        patch.event_date or patch.effective_date,
        patch.bond_code,
        tuple(sorted(patch.fields)),
    )


def _latest_bond_close(provider: DataProvider, bond_code: str, valuation_date: date) -> float | None:
    try:
        history = provider.get_bond_history(
            bond_code,
            valuation_date - timedelta(days=15),
            valuation_date,
        )
    except Exception:
        return None
    latest = None
    latest_date = None
    for d, value in history:
        if d is None or d > valuation_date:
            continue
        close = finite_float(value)
        if close is None:
            continue
        if latest_date is None or d >= latest_date:
            latest_date = d
            latest = close
    return latest
