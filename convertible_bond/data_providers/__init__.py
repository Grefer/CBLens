"""数据源后端抽象层 (拆分包入口).

把 Wind / akshare / CSV 等不同数据源统一到一个 ``DataProvider`` 接口,
让 CB.py 的定价 / 回测函数与具体数据源解耦.

新增后端只需继承 ``DataProvider`` (在 ``base.py``) 并实现下列方法:
  - get_bond_terms(code, valuation_date) -> BondTerms
  - get_stock_close(stock_code, on_date) -> float
  - get_stock_history(stock_code, start, end) -> [(date, float|None), ...]
  - get_stock_dividend_yield(stock_code, on_date) -> float | None  (单位: %, 例如 2.50)
  - get_bond_history(bond_code, start, end) -> [(date, float|None), ...]
  - get_cashflow(bond_code) -> CashflowSchedule | None
  - get_risk_free_rate(on_date) -> float | None  (单位: %, 例如 2.20)

模块布局 (历史上是单文件 ``data_providers.py``):
  - base.py            BondTerms / CashflowSchedule / DataProvider ABC + 公共工具
  - _helpers.py        粘合脏数据用的私有 helper (_retry, _row_value, ...)
  - wind.py            WindDataProvider
  - akshare.py         AkshareDataProvider
  - csv_provider.py    CSVDataProvider
  - auto.py            detect/auto provider

本入口 re-export 全部历史公共 + 私有名字, 旧 ``from .data_providers import X``
保持工作。
"""
from __future__ import annotations

# 公共 API
from .base import (
    BondTerms,
    CashflowSchedule,
    DataProvider,
    finite_float,
    infer_cb_trading_metadata,
    is_standard_public_cb_code,
    looks_private_cb_name,
    parse_coupon_chinese_text,
    parse_coupon_string,
    to_date,
    _add_months,
)
from ._helpers import (
    _announcement_row_from_wind,
    _date_or_none,
    _float_or_none,
    _latest_finite,
    _retry,
    _row_value,
    _stock_history_from_df,
    _string_or_none,
    _wind_table_rows,
    _wind_to_ak_bond,
    _wind_to_ak_stock,
    _wind_to_ak_stock_prefixed,
)
from .wind import WindDataProvider
from .akshare import AkshareDataProvider
from .csv_provider import CSVDataProvider
from .auto import auto_data_provider, detect_available_providers


__all__ = [
    # 公共 API
    "BondTerms",
    "CashflowSchedule",
    "DataProvider",
    "WindDataProvider",
    "AkshareDataProvider",
    "CSVDataProvider",
    "auto_data_provider",
    "detect_available_providers",
    "finite_float",
    "infer_cb_trading_metadata",
    "is_standard_public_cb_code",
    "looks_private_cb_name",
    "parse_coupon_chinese_text",
    "parse_coupon_string",
    "to_date",
]
