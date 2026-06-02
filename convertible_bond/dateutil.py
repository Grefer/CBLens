"""日期工具: 跨模块共用的日历运算 helper (零三方依赖)。"""
from datetime import date


def add_years(d: date, years: int) -> date:
    """返回 ``d`` 加上 ``years`` 年的日期; 2/29 等非法日期回落到 2/28。

    与 ``dateutil.relativedelta`` 同义但零依赖。``years`` 可为负 (回溯计算,
    如回售起始日 = 到期日 - N 年)。结果年份 < 1 时抛 ``ValueError``
    (公历无 0 年/负年, 多为参数错误而非合法回溯)。
    """
    new_year = d.year + years
    if new_year < 1:
        raise ValueError(
            f"Cannot add {years} years to {d}: resulting year {new_year} < 1"
        )
    try:
        return d.replace(year=new_year)
    except ValueError:
        return d.replace(month=2, day=28, year=new_year)
