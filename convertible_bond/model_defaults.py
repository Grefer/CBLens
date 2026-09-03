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

#: 评级 → 信用利差**下限** (小数)。`pricing_api._rating_spread_floor` 把它当
#: 定价的 base_spread 下界, GUI 的 `theme.CREDIT_SPREAD_TABLE` 是它的百分号视图。
#: 两处曾各写一份 19 行的字面量。
RATING_SPREAD_FLOORS = {
    "AAA": 0.012,
    "AA+": 0.018,
    "AA": 0.025,
    "AA-": 0.035,
    "A+": 0.045,
    "A": 0.060,
    "A-": 0.080,
    "BBB+": 0.100,
    "BBB": 0.120,
    "BBB-": 0.150,
    "BB+": 0.180,
    "BB": 0.220,
    "BB-": 0.260,
    "B+": 0.300,
    "B": 0.360,
    "B-": 0.420,
    "CCC": 0.500,
    "CC": 0.650,
    "C": 0.800,
}
