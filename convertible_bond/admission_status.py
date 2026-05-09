"""主池准入状态字段的增量刷新.

这一层只更新批量定价筛选会用到的事件/状态字段, 不重建完整条款:
停牌、强赎公告、摘牌/最后交易日、正股 ST 风险、成交额、评级和余额等。
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import fields, replace
from datetime import date

from .batch_pricing import (
    batch_pricing_exclusion_reason,
    summarize_exclusions,
)
from .data_providers import BondTerms, DataProvider


ADMISSION_STATUS_FIELDS = (
    "is_tradable",
    "trading_status",
    "suspension_status",
    "call_status",
    "call_announce_date",
    "call_redemption_date",
    "last_trading_date",
    "delisting_date",
    "underlying_name",
    "underlying_status",
    "underlying_trade_status",
    "underlying_pct_change",
    "bond_turnover_amount",
    "credit_rating",
    "outstanding_balance",
)

_BOND_TERM_FIELD_NAMES = {f.name for f in fields(BondTerms)}


def merge_admission_status(base: BondTerms | None, patch: BondTerms | None) -> BondTerms:
    """把非空状态字段合并进已有条款.

    ``patch`` 中为 None 的字段不会覆盖 ``base``。这样 Wind 某些候选字段不可用时,
    不会把本地人工维护或上次同步到的状态清空。
    """
    base_terms = base or BondTerms()
    if patch is None:
        return base_terms
    updates = {
        name: getattr(patch, name)
        for name in ADMISSION_STATUS_FIELDS
        if name in _BOND_TERM_FIELD_NAMES and getattr(patch, name, None) is not None
    }
    return replace(base_terms, **updates) if updates else base_terms


def changed_admission_fields(before: BondTerms | None, after: BondTerms) -> list[str]:
    """返回准入状态字段中发生变化的字段名."""
    before_terms = before or BondTerms()
    return [
        name for name in ADMISSION_STATUS_FIELDS
        if getattr(before_terms, name, None) != getattr(after, name, None)
    ]


def refresh_admission_status(
    provider: DataProvider,
    bond_codes: Iterable[str],
    store=None,
    valuation_date: date | None = None,
    on_progress=None,
) -> dict:
    """批量刷新主池准入状态字段并写回 store.

    返回 ``{success, failed, changed, excluded, excluded_by_reason, store_path}``。
    ``store`` 建议传 ``TermsBundle``; 若 store 中没有某只债, 会先用 provider
    拉完整基础条款作为 base, 再合并状态字段。
    """
    val_date = valuation_date or date.today()
    codes = list(bond_codes)
    success: list[str] = []
    failed: list[tuple[str, str]] = []
    changed: list[tuple[str, list[str]]] = []
    excluded: list[tuple[str, str]] = []
    fresh_items: list[tuple[str, BondTerms]] = []

    for i, code in enumerate(codes):
        if on_progress:
            on_progress(i, len(codes), code)
        try:
            base = _store_get(store, code)
            if base is None:
                base = provider.get_bond_terms(code, val_date)
            patch = provider.get_admission_status(code, val_date, base_terms=base)
            merged = merge_admission_status(base, patch)
            changed_fields = changed_admission_fields(base, merged)
            reason = batch_pricing_exclusion_reason(code, merged, on_date=val_date)
        except Exception as exc:
            failed.append((code, str(exc)))
            continue

        success.append(code)
        if changed_fields:
            changed.append((code, changed_fields))
            fresh_items.append((code, merged))
        elif store is not None and _store_get(store, code) is None:
            fresh_items.append((code, merged))
        if reason:
            excluded.append((code, reason))

    if store is not None and fresh_items:
        if hasattr(store, "set_many"):
            store.set_many(fresh_items, source=f"{provider.name}:admission_status")
        else:
            for code, terms in fresh_items:
                store.set(code, terms, source=f"{provider.name}:admission_status")

    store_path = getattr(store, "path", None) or getattr(store, "root", None)
    return {
        "success": success,
        "failed": failed,
        "changed": changed,
        "excluded": excluded,
        "excluded_by_reason": summarize_exclusions(excluded),
        "store_path": str(store_path) if store_path else None,
    }


def refresh_admission_status_from_store(
    provider: DataProvider,
    store,
    *,
    valuation_date: date | None = None,
    limit: int = 0,
    on_progress=None,
) -> dict:
    """对 store 中已有转债刷新准入状态字段."""
    if store is None or not hasattr(store, "list_bonds"):
        raise ValueError("store 必须支持 list_bonds()")
    codes: Sequence[str] = store.list_bonds()
    if limit and limit > 0:
        codes = codes[:limit]
    return refresh_admission_status(
        provider,
        codes,
        store=store,
        valuation_date=valuation_date,
        on_progress=on_progress,
    )


def _store_get(store, code: str) -> BondTerms | None:
    if store is None or not hasattr(store, "get"):
        return None
    return store.get(code)
