"""按业务域拆分的 controller mixin.

每个 mixin 只持有同一域的方法, 通过多继承组装到 ``CBPricerApp`` 上,
对外接口不变 (tab UI 仍然通过 ``app._method_name`` 调用).
"""
from .backtest import BacktestMixin
from .strategy_backtest import StrategyBacktestMixin
from .down_reset import DownResetMixin
from .events import EventsMixin
from .pricing import PricingMixin
from .sensitivity import SensitivityMixin
from .wind_sync import WindSyncMixin


__all__ = [
    "BacktestMixin",
    "StrategyBacktestMixin",
    "DownResetMixin",
    "EventsMixin",
    "PricingMixin",
    "SensitivityMixin",
    "WindSyncMixin",
]
