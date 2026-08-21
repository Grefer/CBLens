"""交易所时间基准 (Asia/Shanghai).

A 股市场的"今天"是**北京时间**的今天。直接用 ``date.today()`` 会跟着运行机器的
时区走: 美西 (UTC-7) 从当地 09:00 起就已经是北京的次日, 于是估值日、公告同步
窗口、准入判断、缓存新鲜度会整体错开一天。

约定:

- 一切**市场口径的日期** (估值日、同步窗口、准入/防前视判断) 走 ``market_today()``;
- 只有**落盘元信息** (``saved_at`` / ``fetched_at`` 这类记录本机挂钟的时间戳)
  继续用本机时区的 ``datetime.now()``, 它们与本机的 staleness 比较自洽。

``tests/test_market_time.py`` 里有一条守护测试, 会扫描包内是否又冒出裸的
``date.today()``。
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

# 上交所/深交所所在时区; 数据源 (Wind / 巨潮 / akshare) 的日期口径也都是它。
EXCHANGE_TZ = ZoneInfo("Asia/Shanghai")


def market_now() -> datetime:
    """交易所时区的当前时间 (tz-aware)."""
    return datetime.now(EXCHANGE_TZ)


def market_today() -> date:
    """交易所时区的今天; 用来替代一切市场口径下的 ``date.today()``."""
    return market_now().date()
