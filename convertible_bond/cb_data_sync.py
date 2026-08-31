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
import math
from dataclasses import fields, replace
from datetime import date
from collections.abc import Iterable, Sequence

from .cache import TermsBundle
from .data_providers import (
    BondTerms, DataProvider,
    is_standard_public_cb_code, looks_private_cb_name,
)
from .market_time import market_today




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


def locally_authoritative_fields(provider) -> tuple[str, ...]:
    """本次同步**不许覆盖**的字段: 这个 provider 的 ``get_bond_terms`` 根本不写的那些.

    ``TermsBundle.set_many`` 是**整条记录替换**, 而 Wind 的 ``get_bond_terms`` 只覆盖条款 ——
    于是每一次 ``cb-sync-tradable`` 都把 ``get_admission_status`` / ``cb_events`` 写入的
    状态字段整批清成 null。实测代价: 主池 284 只债的 ``underlying_status`` /
    ``underlying_trade_status`` / ``suspension_status`` / ``bond_turnover_amount`` /
    ``delisting_date`` / ``last_trading_date`` **全部 0/284** (775 只死券反而保留着旧值,
    因为 ``list_tradable_cbs`` 够不着它们 —— 所以按全库统计的覆盖率看不出问题)。后果是
    「正股 ST/退市风险」「正股停牌」「转债停牌」「成交额过低」「临近摘牌」等六条准入判据
    对主池全部无输入、恒为 False。

    保护集**按 provider 问** (:meth:`DataProvider.authoritative_terms_fields`), 不是一张
    全局常量 —— 那张表是从 Wind 抄来的, 套在写满全部字段的 ``CSVDataProvider`` 上会让
    它的导入静默失效 (26 个字段改不动, 而 ``success`` 照常 +1)。provider 说"不知道"
    (返回 None) 时退回旧行为: 只保 ``credit_rating``。

    ``credit_rating`` 永远在保护集里: Wind 确实返回它, 但那是**发行时冻结值** (cb_data
    跨 17 个版本、约 4000 次逐债重取零变化), 当前值由 ``cb-sync-ratings`` 从 akshare
    刷新。Wind 仍然**兜底首次建档** —— 保的是"已有值", 不是"这个字段"。
    """
    try:
        owned = provider.authoritative_terms_fields()
    except Exception:
        owned = None
    if owned is None:
        return ("credit_rating",)
    return tuple(sorted(
        ({f.name for f in fields(BondTerms)} - set(owned)) | {"credit_rating"}
    ))


def _has_local_value(value) -> bool:
    """本地这一格算不算"已有值"。

    NaN 不算 —— ``NaN is not None`` 为真, 用 ``not in (None, "")`` 判会把一个 NaN
    当成好值保下来, 而这一层保的字段里有 ``underlying_pct_change`` / ``putback_price``
    这类数值列 (见 AGENTS.md「NaN 不是 None」)。

    判据逐类型写死, **不用 ``value == "" or value == ()``** —— 那种写法对 numpy 标量会
    走广播 (``np.float64(0.0) == ()`` 返回空数组), ``if`` 上去直接抛
    ``ValueError: truth value of an empty array is ambiguous``。
    """
    if value is None:
        return False
    if isinstance(value, float) and math.isnan(value):
        return False
    if isinstance(value, (str, tuple, list, dict)) and len(value) == 0:
        return False
    return True


def _keep_locally_authoritative(
    store, code: str, terms: BondTerms, protected: Sequence[str],
) -> BondTerms:
    """``protected`` 里本地已有值的字段不被本次同步覆盖。"""
    try:
        existing = store.get(code) if hasattr(store, "get") else None
    except Exception:
        existing = None
    if existing is None:
        return terms
    keep = {
        name: getattr(existing, name)
        for name in protected
        if _has_local_value(getattr(existing, name, None))
    }
    return replace(terms, **keep) if keep else terms


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
    preserve_local: bool = True,
) -> dict:
    """指定代码批量同步, 返回 ``{success, failed, dropped, skipped, store_path}``.

    ``drop_terminal=True`` 时在条款层做 Stage 2 过滤 (剔已到期/违约).
    Bundle 模式下一次性 ``set_many()`` 提交; 中途失败不会留下半截 bundle。

    ``incremental=True`` 时跳过本地 store 中已在 ``max_age_days`` 天内刷新的债;
    跳过的代码进入 ``skipped`` 列表, 不消耗 Wind 调用. 全量同步用 False.

    ``preserve_local=False`` 恢复"整条记录替换"的老行为: provider 不写的字段一律清成
    None。**这是一个破坏性动作**, 只给一种场景用 —— 公告误分类把 ``last_trading_date``
    / ``call_*`` / ``putback_*`` 写坏之后, 把 cb_data 的状态字段擦回数据源口径再让
    ``cb-sync-events --apply`` 重放 (见 ``cli/repair_events`` 的流程说明)。
    这条路必须留着: ``apply_events_to_terms`` 与 ``merge_admission_status`` 对这批字段
    **只写不撤**, 实测 ``last_trading_date`` 在 342 只在市未到期债上 Wind 一只都不返回,
    所以整条替换是它仅有的橡皮擦。默认 True。
    """
    store = store or TermsBundle()
    val_date = valuation_date or market_today()
    success: list[str] = []
    failed: list[tuple[str, str]] = []
    dropped: list[tuple[str, str]] = []
    skipped: list[tuple[str, str]] = []
    fresh_items: list[tuple[str, BondTerms]] = []
    codes = list(bond_codes)

    # 增量过滤: store 提供 is_stale 时按时效跳过, 否则忽略 incremental 标志。
    #
    # 时效必须按**本 provider 这个来源**算, 不能用全局 fetched_at —— 每日的
    # admission_status 刷新、每月的 ratings 同步、每日的 cb_events 回写都会把全局值推到
    # 今天, 却一个条款字段都不抓。用全局值判定时实测 1052/1058 只被判成"7 天内已更新"
    # 而跳过, 且照常打印"已在 N 天内更新" —— 增量同步永久空转且完全静默。
    if incremental and hasattr(store, "is_stale"):
        to_fetch: list[str] = []
        for code in codes:
            try:
                stale = store.is_stale(code, max_age_days, source=provider.name)
            except TypeError:      # 旧式 store 没有 source 形参
                stale = store.is_stale(code, max_age_days)
            if stale:
                to_fetch.append(code)
            else:
                skipped.append((code, f"已在 {max_age_days} 天内更新"))
        codes = to_fetch

    protected = locally_authoritative_fields(provider) if preserve_local else ()

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
        if protected:
            # 放在 try 内: 这一步会读 store, 单只出问题该落进 failed 而不是把整轮
            # 已取到的几百次调用一起拖掉 (fresh_items 要循环跑完才 set_many 落盘)。
            try:
                terms = _keep_locally_authoritative(store, code, terms, protected)
            except Exception as e:
                failed.append((code, f"合并本地权威字段失败: {e}"))
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
    preserve_local: bool = True,
) -> dict:
    """全市场同步: 拉清单 → 过滤定向 → 拉条款 → 过滤到期/违约 → 落盘.

    返回 ``{success, failed, dropped, skipped, codes_total, codes_kept, store_path}``.
    ``dropped`` 合并了 Stage 1 (代码层) 与 Stage 2 (条款层) 两阶段被剔除的债。
    ``incremental=True`` 时只刷新 ``max_age_days`` 天前/缺失的债。
    """
    val_date = valuation_date or market_today()
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
        preserve_local=preserve_local,
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
    preserve_local: bool = True,
) -> BondTerms:
    """单只刷新 (GUI 🔄 按钮). 用户主动刷新即视为想要, 不做过滤.

    ``_keep_locally_authoritative`` 这一道不能省: 落盘同样是**整条替换**, 少了它
    刷新一只债就把它的准入状态字段清空一次 —— 与全量同步是同一个缺陷, 只是范围是 1 只。
    "不做过滤"说的是不剔终止态, 不是可以覆盖别的来源写的字段。
    """
    val_date = valuation_date or market_today()
    terms = _fetch_one(provider, bond_code, val_date, with_cashflow=with_cashflow)
    if store is not None:
        protected = locally_authoritative_fields(provider) if preserve_local else ()
        if protected:
            terms = _keep_locally_authoritative(store, bond_code, terms, protected)
        _store_set(store, bond_code, terms, source=provider.name)
    return terms
