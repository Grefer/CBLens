"""模型层共享默认值."""

DEFAULT_DOWN_RESET_TRIGGER_PCT = 85.0
DEFAULT_DOWN_RESET_TRIGGER_RATIO = DEFAULT_DOWN_RESET_TRIGGER_PCT / 100.0
#: 背景态下修强度的**唯一**默认值 (年化 hazard, 触发线下方生效)。
#: 此前这个数分叉成两份: 库层 API 默认 0.15, 而 GUI (``DEFAULT_P_DOWN_PCT``) 与
#: ``cb-strategy-backtest --p-down`` 都用 0.25 —— 于是 README 里那段
#: ``price_from_auto("128009.SZ")`` 示例算出来的理论价, 和用户在 GUI 里对同一只债
#: 看到的不是一个数, 而两边都不报错。UI/CLI 一律显式传参, 所以收口只改**直接调库**
#: 那条路 (0.15 → 0.25), 方向是向生产口径靠拢。
DEFAULT_BACKGROUND_P_DOWN = 0.25
DEFAULT_BACKGROUND_P_DOWN_PCT = DEFAULT_BACKGROUND_P_DOWN * 100.0
