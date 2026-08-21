"""交易所时间基准 (market_time) 与"包内不得再出现裸 date.today()"守护."""
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

import convertible_bond
from convertible_bond.market_time import EXCHANGE_TZ, market_now, market_today


_TZS = [
    "America/Los_Angeles",   # UTC-7/8: 本机口径会比北京早一天
    "UTC",
    "Asia/Shanghai",
    "Pacific/Kiritimati",    # UTC+14: 反方向会晚一天
]


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="time.tzset 仅 POSIX 可用")
@pytest.mark.parametrize("tz", _TZS)
def test_market_today_follows_beijing_not_machine(monkeypatch, tz):
    """market_today() 恒等于北京时间的今天, 与本机时区无关."""
    monkeypatch.setenv("TZ", tz)
    time.tzset()
    try:
        expected = datetime.now(EXCHANGE_TZ).date()
        assert market_today() == expected
        # 本机口径与北京口径最多差一天; 差出来时必须以北京口径为准
        assert abs((market_today() - date.today()).days) <= 1
    finally:
        monkeypatch.undo()
        time.tzset()


def test_market_now_is_timezone_aware_and_offset_is_east_eight():
    now = market_now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(hours=8)
    assert now.date() == market_today()


_BARE_TODAY = re.compile(r"\b(?:date|datetime)\.today\(\)")


def test_package_has_no_bare_date_today():
    """包内市场口径日期一律走 market_today().

    裸 ``date.today()`` 跟着运行机器时区走, 非东八区用户的估值日、公告同步
    窗口、准入判断会整体错开一天 —— 这种偏差不会报错, 只会静默算错, 所以在
    这里拦一道。真需要本机挂钟 (落盘 saved_at 之类) 请用 ``datetime.now()``。
    """
    pkg_root = Path(convertible_bond.__file__).parent
    offenders = []
    for path in sorted(pkg_root.rglob("*.py")):
        if path.name == "market_time.py":
            continue                      # 规则本身写在它的 docstring 里
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _BARE_TODAY.search(line):
                rel = path.relative_to(pkg_root.parent)
                offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert not offenders, (
        "以下位置仍在用本机时区的 today(), 请改用 convertible_bond.market_time.market_today():\n"
        + "\n".join(offenders)
    )
