"""CBLens convertible bond pricing package.

本模块只作**对外公开 API 的聚合入口**, 名字按 PEP 562 惰性解析 (``__getattr__``),
不在导入时把子模块拉起来。

**为什么惰性 (2026-09-03 实测)**: 此前这里在模块顶层饥饿导入 14 个子模块, 于是
``import convertible_bond.<任何东西>`` 都要先付 pricer→scipy、cninfo_provider→requests
这整条链的钱 —— 哪怕调用方只想要一个纯标准库模块。实测:

- ``import convertible_bond``          0.39s → **0.02s** (裸解释器就是 0.02s)
- ``import convertible_bond.market_time`` (只 import datetime/zoneinfo) 0.35s → **0.02s**
- ``cb-sync-new-issues`` 冷启动 (AGENTS 说它"秒级, 不需要 Wind") 0.35s → **0.06s**,
  且 ``scipy`` / ``requests`` 从它的模块图里彻底消失 —— 那条命令一个都用不上,
  ``pandas``/``akshare`` 反而是它真正需要却当时还没加载的。

**对外承诺一个字节不变**: ``__all__`` 仍是那 95 个名字, ``from convertible_bond import X``
与 ``convertible_bond.X`` 照常工作 (``from ... import`` 会走 getattr 回落到这里)。
``from convertible_bond import cb_data_sync`` 这种子模块形式由导入机制自己解决, 不经过
``_EXPORTS`` —— 全仓 37 处包级导入用的都是这个形式。
唯一没还原的是 ``dir()`` 里的子模块名: 饥饿版顺带列出被它 import 的那 14 个 (全包约 40 个),
惰性版只列还原过的。那 14 个本来就是个任意子集, 照抄没有意义, 所以不追。

**子模块的属性访问要显式补** (2026-09-03): ``import convertible_bond`` 之后
``convertible_bond.pricer`` 在饥饿时代是取得到的 (那 14 条 ``from .X import`` 顺带把
``X`` 挂在了包上), 惰性化会让它变成"取决于这个进程碰过什么" —— 裸 import 之后
``cb.pricer`` 抛 AttributeError, 而先碰一下 ``cb.UniversalCBPricer`` 它又出现了。
这正是下面 ``__dir__`` 那条注释说的同一种病, 只是发生在子模块名上。所以 ``__getattr__``
在 ``_EXPORTS`` 未命中时再按子模块解析一次。用 ``find_spec`` 先探一下而不是直接
``import_module`` 兜 ``ImportError``: 后者会把**存在但自己 import 炸了**的子模块
(比如 ``pricer`` 缺 scipy) 一并吞成 "no attribute 'pricer'", 把真正的原因藏掉。

**代价要认**: 名字写错/搬家不再在 ``import`` 那一刻暴露, 而是推迟到第一次访问。
饥饿导入此前是**免费**在做这件校验, 所以
``tests/test_public_api.py::test_every_public_name_actually_resolves`` 必须把它补回来 ——
那条用例是这次改动的承重墙, 不是锦上添花。

**线程安全**: ``importlib.import_module`` 走导入锁, 写 ``globals()`` 是原子字典写,
所以并发首次访问是安全的。但要留神本仓另一条约定 (见 AGENTS「预热 V8」那段): 惰性
导入会把"某个模块的首次 import"挪到更晚、可能是 worker 线程里。今天没有这种路径 ——
全仓没有一处按符号从包里取东西, 更没有在线程里取的。新增时请自查。
"""

from __future__ import annotations

import importlib
import importlib.util  # ``import importlib`` 不保证 ``importlib.util`` 已绑定

__version__ = "1.0.0"

# 公开名 → 它所在的子模块。这是公开 API 的**单一事实源**: ``__all__`` 由它派生,
# ``__getattr__`` 也查它, 两处各写一份就是又一张会分叉的表。
_EXPORTS: dict[str, str] = {
    "UniversalCBPricer": "pricer",
    "DEFAULT_COUPON_RATES": "pricer",
    "DEFAULT_FACE_VALUE": "pricer",
    "DEFAULT_REDEMPTION_PRICE": "pricer",
    "price_from_provider": "pricing_api",
    "price_from_wind": "pricing_api",
    "price_from_auto": "pricing_api",
    "batch_price_from_provider": "pricing_api",
    "batch_price_from_provider_threaded": "pricing_api",
    "AdmissionFilterConfig": "batch_pricing",
    "AdmissionFilterResult": "batch_pricing",
    "BATCH_RESULT_COLUMNS": "batch_pricing",
    "build_batch_provider": "batch_pricing",
    "list_upcoming_tradable_from_cache": "batch_pricing",
    "list_batch_codes_from_cache": "batch_pricing",
    "load_batch_results_cache": "batch_pricing",
    "merge_upcoming_pricing_results": "batch_pricing",
    "parse_bond_codes": "batch_pricing",
    "project_batch_cache_path": "batch_pricing",
    "save_batch_results_cache": "batch_pricing",
    "screen_batch_pool_from_cache": "batch_pricing",
    "summarize_batch_results": "batch_pricing",
    "summarize_exclusions": "batch_pricing",
    "write_batch_results_csv": "batch_pricing",
    "backtest_theoretical_price": "backtest",
    "PDEStrategyConfig": "strategy_backtest",
    "ScoreStrategyConfig": "strategy_backtest",
    "backtest_pde_strategy": "strategy_backtest",
    "backtest_score_strategy": "strategy_backtest",
    "build_rebalance_schedule": "strategy_backtest",
    "write_strategy_backtest_csv": "strategy_backtest",
    "HistoricalBondDataProvider": "historical_terms",
    "TermsHistoryStore": "historical_terms",
    "TermsPatch": "historical_terms",
    "TermsPatchStore": "historical_terms",
    "default_terms_patch_store": "historical_terms",
    "project_terms": "historical_terms",
    "project_terms_history_dir": "historical_terms",
    "project_terms_patches_path": "historical_terms",
    "reload_default_terms_patch_store": "historical_terms",
    "strip_current_status_fields": "historical_terms",
    "DataProvider": "data_providers",
    "BondTerms": "data_providers",
    "CashflowSchedule": "data_providers",
    "WindDataProvider": "data_providers",
    "AkshareDataProvider": "data_providers",
    "CSVDataProvider": "data_providers",
    "CninfoAnnouncementProvider": "cninfo_provider",
    "extract_text_from_pdf_bytes": "cninfo_provider",
    "auto_data_provider": "data_providers",
    "detect_available_providers": "data_providers",
    "TermsCache": "cache",
    "TermsBundle": "cache",
    "CachedBondDataProvider": "cache",
    "CachingDataProvider": "cache",
    "filter_listed_codes": "cb_data_sync",
    "is_terminal_terms": "cb_data_sync",
    "refresh_one": "cb_data_sync",
    "sync_cb_data": "cb_data_sync",
    "sync_cb_terms": "cb_data_sync",
    "ADMISSION_STATUS_FIELDS": "admission_status",
    "changed_admission_fields": "admission_status",
    "merge_admission_status": "admission_status",
    "refresh_admission_status": "admission_status",
    "refresh_admission_status_from_store": "admission_status",
    "CBEvent": "cb_events",
    "CBEventStore": "cb_events",
    "apply_events_to_terms": "cb_events",
    "classify_announcement_title": "cb_events",
    "default_event_store": "cb_events",
    "parse_event_from_announcement": "cb_events",
    "parse_call_redemption_dates": "cb_events",
    "parse_conversion_suspension_terms": "cb_events",
    "parse_down_reset_new_price": "cb_events",
    "parse_putback_terms": "cb_events",
    "project_events_path": "cb_events",
    "apply_events_to_bundle": "cb_event_sync",
    "parse_credit_rating_change": "cb_event_sync",
    "parse_credit_rating_terms": "cb_event_sync",
    "parse_conversion_price_adjustment": "cb_event_sync",
    "parse_outstanding_balance_change": "cb_event_sync",
    "parse_terms_patch_from_announcement": "cb_event_sync",
    "sync_cb_events": "cb_event_sync",
    "project_bundle_path": "cache",
    "seed_data_files": "paths",
    "data_path": "paths",
    "data_dir": "paths",
    "app_data_dir": "paths",
    "asset_path": "paths",
    "project_root": "paths",
    "is_frozen_app": "paths",
    "APP_NAME": "paths",
    "to_date": "data_providers",
    "parse_coupon_string": "data_providers",
}

__all__ = ["__version__", *_EXPORTS]


def __getattr__(name: str):
    """PEP 562: 只有真被访问的名字才导入它所在的子模块。"""
    module = _EXPORTS.get(name)
    if module is not None:
        value = getattr(importlib.import_module(f".{module}", __name__), name)
        # 缓存进模块命名空间: 之后走正常属性查找, 不再进这里。
        globals()[name] = value
        return value
    # 不是导出名, 那就试试它是不是子模块 (见模块 docstring「子模块的属性访问要显式补」)。
    # 下划线开头的一律不探: 它们是 ``__wrapped__`` / ``__bases__`` 这类探测性 getattr,
    # 不可能是子模块, 每次都去 stat 一遍文件系统是白花钱。
    if not name.startswith("_"):
        try:
            found = importlib.util.find_spec(f".{name}", __name__) is not None
        except (ImportError, AttributeError, ValueError):
            found = False
        if found:
            return importlib.import_module(f".{name}", __name__)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    # 没有它, ``dir(convertible_bond)`` 只看得见已被访问过的名字 —— 补全和
    # ``help()`` 会随"这个进程碰过什么"变化。
    return sorted(set(globals()) | set(_EXPORTS))
