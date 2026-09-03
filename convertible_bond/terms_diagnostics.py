"""条款来源诊断的**唯一实现** (单债回测页与策略回测页共用).

单债回测 (``backtest.py``) 与策略回测 (``strategy_backtest.py``) 都要回答同一个问题:
"这一天用的条款是从哪儿来的、投影层生效没有"。此前两边**各写了一份逐字相同**的
``_terms_source_diagnostic`` —— 同样的七键兜底字典、同样的日志文案。

这是本仓库反复出事的那个形状 (见 AGENTS「同一段口径不许有第二份实现」): 副本的典型
失败形态不是"抄错", 是**修的时候只修了一份**。本次会话刚因为
``backtest._build_backtest_pricer_kwargs`` 手抄 ``pricing_api`` 的条款管线然后各自
演化修过一轮 —— 那一份抄漏了四处, 其中"逐点读取条款"整整一条承诺在代码里一天都没成立
过, 实测全库 322/1059 只 (30.4%) 至少有一个采样日的转股价用错、最大偏离 115%。

兜底字典的**键集**是承重的: ``strategy_backtest._period_data_quality`` 逐键读
``terms_source`` / ``uses_current_fallback`` / ``patch_count`` / ``event_count`` /
``snapshot_date`` 去算数据质量摘要。两份实现里只给一份加键, 表现是"策略页数据质量
这一栏在单债回测口径下恒为默认值", 不报错。
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

from .data_providers import DataProvider

logger = logging.getLogger(__name__)


def default_terms_source_diagnostic(bond_code: str, valuation_date: date) -> dict[str, Any]:
    """provider 说不出条款来源时的兜底诊断 —— 这七个键就是**唯一**的键集.

    ``terms_source="provider"`` 表示"就是 provider 直接给的条款, 没有投影层"; 与
    ``HistoricalBondDataProvider`` 报的 ``snapshot`` / ``patched`` 等档位并列。
    ``uses_current_fallback=False`` 在这一档是**诚实的**: 没包投影层时无所谓"回落当前条款",
    那个字段问的是"投影层找不到快照所以退回当前值了吗"。

    **不要在旁边再放一份 ``TERMS_DIAGNOSTIC_KEYS`` 之类的键名元组** —— 这个模块存在的
    全部理由就是消灭"同一段口径的第二份实现", 而一个没人读的键名副本正是它的退化形态:
    往这个字典里加键时它不会红, 只会静默过期。键集由
    ``test_eligible_codes_emits_the_seven_key_terms_diagnostic`` 用字面量钉住。
    """
    return {
        "bond_code": bond_code,
        "valuation_date": valuation_date,
        "terms_source": "provider",
        "snapshot_date": None,
        "patch_count": 0,
        "event_count": 0,
        "uses_current_fallback": False,
    }


def terms_source_diagnostic(
    provider: DataProvider,
    bond_code: str,
    valuation_date: date,
) -> dict[str, Any]:
    """问 provider 要这一天的条款来源诊断, 要不到就回落默认.

    鸭子类型而不是 ABC 方法: provider 装饰器链上只有 ``HistoricalBondDataProvider``
    与 ``DiskCacheProvider`` 真有 ``get_terms_source_diagnostics``, 给 base ABC 加抽象
    方法会打断整条链。异常只 debug 记录 —— 诊断是**旁路信息**, 它失败不该让整轮回测倒下。
    """
    describe = getattr(provider, "get_terms_source_diagnostics", None)
    if callable(describe):
        try:
            diag = describe(bond_code, valuation_date)
            if isinstance(diag, dict):
                return diag
        except Exception:
            logger.debug("get_terms_source_diagnostics(%s) 失败, 回落默认诊断",
                         bond_code, exc_info=True)
    return default_terms_source_diagnostic(bond_code, valuation_date)
