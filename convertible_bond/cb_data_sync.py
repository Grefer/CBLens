"""转债基础信息的获取与更新.

把"全市场列表 → 过滤 → 拉条款 → 再过滤 → 落盘"的串联逻辑集中到这里,
避免散落在 cache.py / cli / gui 各处。

公开 API:
  sync_cb_data(provider, bundle, ...)        全市场同步: 拉清单, 过滤定向 / 到期 /
                                              违约后, 写入 bundle (sync_tradable CLI 用)
  sync_cb_terms(provider, codes, store, ...) 指定代码同步: 不拉清单, 直接按代码批量
                                              拉条款 (sync_terms CLI / 调试用)
  refresh_one(provider, code, store, ...)    单只刷新, GUI 🔄 按钮用 (默认不做过滤)

过滤分两阶段:
  Stage 1 (代码层 — filter_listed_codes): 按代码段 + 名字模式剔定向转债
  Stage 2 (条款层 — is_terminal_terms): 拉到条款后再剔已到期 / 退市 / 违约
"""
from __future__ import annotations

import logging
from dataclasses import replace
from datetime import date
from collections.abc import Iterable, Sequence

from .cache import TermsBundle
from .data_providers import (
    BondTerms, DataProvider,
    is_standard_public_cb_code, looks_private_cb_name,
)




logger = logging.getLogger(__name__)


# Stage 2 黑名单: trading_status 字段中出现这些关键字, 视为终止态
# (当前 _BOND_FIELDS 未拉 trade_status, 只能由数据源主动写入; 留作未来扩展挂钩)
_TERMINAL_STATUS_KEYWORDS = ("退市", "暂停上市", "违约")


def filter_listed_codes(
    codes_with_names: Sequence[tuple[str, str | None]],
    *,
    include_private: bool = False,
) -> tuple[list[str], list[tuple[str, str]]]:
    """Stage 1: 过滤非沪深公募代码段 (124xxx/1108xx 等定向) 与名字含定向标识的债.

    返回 ``(kept_codes, dropped_with_reason)``. ``include_private=True`` 时不过滤,
    全部入选。
    """
    if include_private:
        return [c for c, _ in codes_with_names if c], []
    kept: list[str] = []
    dropped: list[tuple[str, str]] = []
    for code, name in codes_with_names:
        if not code:
            continue
        if not is_standard_public_cb_code(code):
            dropped.append((code, "非沪深公募代码段"))
            continue
        if looks_private_cb_name(name):
            dropped.append((code, "名字含定向标识"))
            continue
        kept.append(code)
    return kept, dropped


def is_terminal_terms(terms: BondTerms, on_date: date) -> str | None:
    """Stage 2: 已到期 / 退市 / 违约 / 定向 (名字兜底) → 返回原因, 否则 None.

    退市/违约目前依赖数据源把状态写入 ``trading_status``; 未集成此字段时只能靠
    ``maturity_date`` 兜住已到期场景。
    Stage 1 在 wset 不返回 sec_name 时识别不到 "九丰定01" 这类名字, 这里用
    ``terms.sec_name`` 再过一次。
    """
    if terms.maturity_date and terms.maturity_date < on_date:
        return f"已到期 ({terms.maturity_date.isoformat()})"
    status = (terms.trading_status or "").strip()
    for kw in _TERMINAL_STATUS_KEYWORDS:
        if kw in status:
            return f"异常状态: {status}"
    if looks_private_cb_name(terms.sec_name):
        return f"名字含定向标识 ({terms.sec_name})"
    return None


def _fetch_one(
    provider: DataProvider,
    code: str,
    val_date: date,
    *,
    with_cashflow: bool = True,
) -> BondTerms:
    """单只: ``get_bond_terms`` + 可选 cashflow 合并. 条款不全时抛 ``ValueError``."""
    terms = provider.get_bond_terms(code, val_date)
    if terms.conversion_price is None:
        raise ValueError("条款不完整: 无转股价")
    if not with_cashflow:
        return terms
    try:
        cf = provider.get_cashflow(code)
    except Exception as e:
        logger.debug("get_cashflow(%s) 失败, 退回 terms.coupon_rates: %s", code, e)
        cf = None
    if cf:
        patch = {}
        if cf.coupon_rates:
            patch["coupon_rates"] = cf.coupon_rates
        if cf.maturity_date and not terms.maturity_date:
            patch["maturity_date"] = cf.maturity_date
        if cf.redemption_price is not None:
            patch["redemption_price"] = float(cf.redemption_price)
        if patch:
            terms = replace(terms, **patch)
    return terms


def _store_set(store, code: str, terms: BondTerms, source: str) -> None:
    if hasattr(store, "set_many"):
        store.set_many([(code, terms)], source=source)
    else:
        store.set(code, terms, source=source)


def sync_cb_terms(
    provider: DataProvider,
    bond_codes: Iterable[str],
    store=None,
    valuation_date: date | None = None,
    with_cashflow: bool = True,
    drop_terminal: bool = True,
    on_progress=None,
    incremental: bool = False,
    max_age_days: int = 7,
) -> dict:
    """指定代码批量同步, 返回 ``{success, failed, dropped, skipped, store_path}``.

    ``drop_terminal=True`` 时在条款层做 Stage 2 过滤 (剔已到期/违约).
    Bundle 模式下一次性 ``set_many()`` 提交; 中途失败不会留下半截 bundle。

    ``incremental=True`` 时跳过本地 store 中已在 ``max_age_days`` 天内刷新的债;
    跳过的代码进入 ``skipped`` 列表, 不消耗 Wind 调用. 全量同步用 False.
    """
    store = store or TermsBundle()
    val_date = valuation_date or date.today()
    success: list[str] = []
    failed: list[tuple[str, str]] = []
    dropped: list[tuple[str, str]] = []
    skipped: list[tuple[str, str]] = []
    fresh_items: list[tuple[str, BondTerms]] = []
    codes = list(bond_codes)

    # 增量过滤: store 提供 is_stale 时按时效跳过, 否则忽略 incremental 标志
    if incremental and hasattr(store, "is_stale"):
        to_fetch: list[str] = []
        for code in codes:
            if store.is_stale(code, max_age_days):
                to_fetch.append(code)
            else:
                skipped.append((code, f"已在 {max_age_days} 天内更新"))
        codes = to_fetch

    for i, code in enumerate(codes):
        if on_progress:
            on_progress(i, len(codes), code)
        try:
            terms = _fetch_one(provider, code, val_date, with_cashflow=with_cashflow)
        except Exception as e:
            failed.append((code, str(e)))
            continue
        if drop_terminal:
            reason = is_terminal_terms(terms, val_date)
            if reason:
                dropped.append((code, reason))
                continue
        fresh_items.append((code, terms))
        success.append(code)

    if fresh_items:
        if hasattr(store, "set_many"):
            store.set_many(fresh_items, source=provider.name)
        else:
            for code, terms in fresh_items:
                store.set(code, terms, source=provider.name)

    store_path = getattr(store, "path", None) or getattr(store, "root", None)
    return {
        "success": success,
        "failed": failed,
        "dropped": dropped,
        "skipped": skipped,
        "store_path": str(store_path) if store_path else None,
    }


def sync_cb_data(
    provider: DataProvider,
    bundle=None,
    valuation_date: date | None = None,
    with_cashflow: bool = True,
    include_private: bool = False,
    on_progress=None,
    incremental: bool = False,
    max_age_days: int = 7,
) -> dict:
    """全市场同步: 拉清单 → 过滤定向 → 拉条款 → 过滤到期/违约 → 落盘.

    返回 ``{success, failed, dropped, skipped, codes_total, codes_kept, store_path}``.
    ``dropped`` 合并了 Stage 1 (代码层) 与 Stage 2 (条款层) 两阶段被剔除的债。
    ``incremental=True`` 时只刷新 ``max_age_days`` 天前/缺失的债。
    """
    val_date = valuation_date or date.today()
    raw = provider.list_tradable_cbs(val_date)
    # 兼容旧实现仍返回 list[str] 的情况 (无 sec_name, 名字过滤不会触发)
    codes_with_names: list[tuple[str, str | None]] = []
    for item in raw or []:
        if isinstance(item, str):
            codes_with_names.append((item, None))
        else:
            code, name = item[0], item[1] if len(item) > 1 else None
            codes_with_names.append((str(code), name))

    kept, dropped_at_list = filter_listed_codes(
        codes_with_names, include_private=include_private,
    )
    result = sync_cb_terms(
        provider, kept,
        store=bundle,
        valuation_date=val_date,
        with_cashflow=with_cashflow,
        on_progress=on_progress,
        incremental=incremental,
        max_age_days=max_age_days,
    )
    result["dropped"] = dropped_at_list + result.get("dropped", [])
    result["codes_total"] = len(codes_with_names)
    result["codes_kept"] = len(kept)
    return result


def refresh_one(
    provider: DataProvider,
    bond_code: str,
    store=None,
    valuation_date: date | None = None,
    with_cashflow: bool = True,
) -> BondTerms:
    """单只刷新 (GUI 🔄 按钮). 用户主动刷新即视为想要, 不做过滤."""
    val_date = valuation_date or date.today()
    terms = _fetch_one(provider, bond_code, val_date, with_cashflow=with_cashflow)
    if store is not None:
        _store_set(store, bond_code, terms, source=provider.name)
    return terms
