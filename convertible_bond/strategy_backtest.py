"""基于批量 PDE 信号的可转债策略回测.

策略保持可解释且分层:
  - 每个调仓日对候选池做批量定价, 按 PDE 估值偏差排序
  - 选出前 N 只转债, 按等权持有到下一调仓边界
  - 下修策略遇到提议/通过/拒绝公告时提前退出, 其余持有到调仓边界
  - 收益用信号日或下一可得收盘价计算

注意: 若使用当前 ``cb_data`` 作为历史条款快照, 下修、强赎和退市状态可能带有
当前信息偏差。该模块负责把口径固定下来; 更严格的历史点位数据可通过 provider
或历史 bundle 接入。
"""
from __future__ import annotations

import csv
import logging
import math
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
from .market_time import market_today
from .model_defaults import DEFAULT_BACKGROUND_P_DOWN

logger = logging.getLogger(__name__)

# 大面积取数失败的中止阈值: 仅当失败率高到代表系统性故障 (Wind 未登录/宕机,
# 接近全部失败) 才中止回测; 部分券瞬时失败 (限流, 已在 provider 层退避重试)
# 视为可跳过, 用成功券继续, 避免把"72% 成功 + 28% 失败"误判为整体不可用。
_SOURCE_OUTAGE_FAIL_RATIO = 0.6
_MIN_OUTAGE_FAILURES = 20

from . import backtest_stats
from .batch_pricing import (
    AdmissionFilterConfig,
    BATCH_REVIEW_VIEWS,
    view_exclusion_reason,
    batch_pricing_exclusion_reason,
    filter_batch_results_by_view,
    _anchor_is_market_wide,
    _rating_below,
    _underlying_at_limit_down,
    _underlying_has_st_risk,
)
from .data_providers import DataProvider, finite_float, is_issued_pending_listing
from .pricing_api import batch_price_from_provider_threaded


_DOWN_RESET_EXIT_EVENT_TYPES = frozenset({
    "down_reset_proposed",
    "down_reset_approved",
    "down_reset_rejected",
})


@dataclass(frozen=True)
class ScoreStrategyConfig:
    """策略回测配置基类。

    机会分 (``opportunity_score``) 及其 ``min_score`` 门槛已整体删除；旧快照里
    ``rank_signal="score"`` 会由 ``_normalize_rank_signal`` 落到「估值偏差」。
    新 GUI/CLI 使用 :class:`PDEStrategyConfig`，正常流程只暴露 PDE 信号。

    A 过滤层(选什么): selection_view + min_confidence/exclude_risk_tags +
        价格/溢价/偏差/波动率区间 → 候选池。
    B 持仓层(持哪些/多少): holding_mode + top_n / max_holdings; 一律等权持有。
    C 资金层(缺口/缺价怎么办): funding_mode。
    三层互不耦合: 任意 holding_mode 都可搭配任意 funding_mode。

    持仓数 (held) = 候选中实际有成交价可建仓者。等权份数分母 intended 与现金:

        holding_mode \\ funding_mode │ reserve_cash(留现金)      │ full_invest(满仓摊回)
        ─────────────────────────────┼───────────────────────────┼──────────────────────
        top_score (取前 top_n)        │ 分母=top_n, 缺口/缺价→现金 │ 分母=held, 缺价摊回
        pool (整个候选池)             │ 分母=候选数, 缺价→现金     │ 分母=held, 缺价摊回

    (引擎与 GUI 默认均为 top_score + reserve_cash = 旧 score_rank + cash 行为。)

    ⚠️ 破坏性变更 (v?.?): 旧字段已移除, 请迁移——
        top_n_shortfall_policy="renormalize" → funding_mode="full_invest"
        top_n_shortfall_policy="cash"         → funding_mode="reserve_cash" (默认)
        selection_weighting="equal_pool"      → holding_mode="pool"
        selection_weighting="score_rank"      → holding_mode="top_score" (默认)
        max_pool_size=N                        → max_holdings=N
    旧值字符串仍被 holding/funding 的 _normalize_* 接受 (传入对应字段即可); 但旧
    **关键字参数名**不再兼容, 会触发 TypeError。输出/快照保留 top_n_shortfall_policy 镜像。
    """

    top_n: int = 10
    rebalance_freq: str = "M"
    selection_view: str = "综合机会"
    min_confidence: tuple[str, ...] | None = ("高", "中")
    #: **兼容字段, 默认空**。标签是给全池标的做**标注**的展示层产物, 判据粗 (四个余额标签
    #: 只表达一个连续量的四个刻度), 策略层不该拿它当筛子 —— 那等于把展示层的分档常量
    #: 冻结进选债口径, 也是 ``LEGACY_STRATEGY_EXCLUDE_TAGS`` 必须"逐字冻结"的根本原因。
    #: 现在策略层用下面那组**显式数值阈值**, 每一条都能单独调。
    #:
    #: 实测这次替换是等价的: 旧的 19 个冻结标签里只有 6 个真在筛东西 (模型溢价高 76 /
    #: 低评级 56 / 模型高估离群 31 / 短久期 26 / 高HV 20 / 小余额 1), 其余 13 个要么
    #: 准入层已经剔掉 (正股停牌 / 转债停牌), 要么在池内结构上不可能亮 (余额清零 /
    #: 触及摘牌线 / 临近摘牌线 —— 池内余额最小 0.65 亿), 要么是已无 append 点的死标签
    #: (偏差异常 / 极小余额 / 余额异常)。下面阈值的默认值**逐条等于**那 6 个标签的判据,
    #: 所以默认配置下候选池逐只相同 (实测 116/284)。
    #:
    #: 传入非空值仍然生效 (旧快照能原样回放), 但它是**追加**的一道闸, 不是主口径。
    exclude_risk_tags: tuple[str, ...] = ()
    min_market_price: float | None = None
    max_market_price: float | None = None
    min_conversion_premium: float | None = None
    max_conversion_premium: float | None = None
    min_deviation: float | None = None
    max_deviation: float | None = None
    min_sigma: float | None = None
    #: 默认 0.80 = 旧「高HV」判据 (σ > 0.80)。
    max_sigma: float | None = 0.80
    # ── A 过滤层: 取代标签的显式阈值 ──────────────────────────────
    # 缺值一律**放行**, 与被取代的标签口径一致 (缺 σ/评级/余额/偏差时打的是
    # 「无HV」「无评级」「无余额」「无偏差」, 那四个都不在旧的排除集里)。
    # 真正"这行不能用"的三档 (缺转股价值 / 缺市价 / 理论价非正) 是**有效性守卫**,
    # 不可配置, 见 _candidate_filter_reason。
    #: 理论价/转股价值 − 1 的上限。默认 0.45 = 旧「模型溢价高」判据。
    max_model_premium: float | None = 0.45
    #: 相对全市场中位的偏差上限。默认 0.20 = 旧「模型高估离群」判据 (贵得离谱的不买)。
    max_relative_deviation: float | None = 0.20
    #: 剩余年限下限。默认 0.5 = 旧「短久期」判据。
    min_years_to_maturity: float | None = 0.5
    #: 债项评级下限。默认 "AA-" = 旧「低评级」判据 (低于 AA- 即打标签)。
    min_credit_rating: str | None = "AA-"
    #: 未转股余额下限 (亿元)。默认 1.0 = 旧「小余额」判据; 它同时覆盖了旧的
    #: 「临近摘牌线」(0.5) /「触及摘牌线」(0.3) /「余额清零」(0) 三档 —— 四个标签
    #: 本来就是同一个连续量的四个刻度, 合成一个阈值才是"更细"的口径。
    min_outstanding_balance: float | None = 1.0
    #: 正股被 ST/退市风险警示时是否排除。默认 True = 旧「正股风险」标签的效果。
    exclude_underlying_st: bool = True
    #: 正股当日跌停时是否排除 (S0 钉在跌停板上, 理论价不可信)。默认 True。
    exclude_underlying_limit_down: bool = True
    price_lookback_days: int = 31
    max_price_staleness_days: int = 10
    # ⚠️ "signal_close" = 在"用于计算信号的那根收盘"上成交, 对低流动性偏乐观;
    # 严肃研究请用 "next_close" (CLI/GUI 默认)。dataclass 默认保留 signal_close
    # 仅为 Python API 向后兼容, 不代表推荐口径。
    execution_timing: str = "signal_close"
    execution_lookahead_days: int = 10
    mark_to_market: bool = True
    pre_filter_prices: bool = True
    transaction_cost: float = 0.0
    compute_benchmark: bool = True
    # 真实指数第二基准 (如 "000832.CSI" 中证转债)。设置后回测额外输出该指数净值曲线,
    # 回答"有没有跑赢被动持有指数"; 数据源取不到 (如 akshare) 时优雅缺省。默认关闭。
    benchmark_index_code: str | None = None
    pool_mode: str = "static"  # "static" | "dynamic"
    # ── B 持仓层: 怎么从候选池构成持仓 (一律等权) ──
    #   "top_score": 按机会分取前 top_n 只。
    #   "pool"     : 等权持有整个候选池, 不按机会分精排。
    # 证据现状 (两种均为研究配置, 无推荐): 跨周期(2022-2026)横截面 Rank-IC≈0,
    # 旧机会分排序无稳健选股 alpha; top_score 在 4 年季频对比中风险调整更优
    # (Sharpe 0.60 vs 0.40), 但源于"候选不足→留现金"的隐性缓冲与极端偏差尾部集中,
    # 月频 2025-26 反向 (现金拖累跑输基准), 不跨频率稳健。
    holding_mode: str = "top_score"
    max_holdings: int | None = None    # pool 模式持仓上限 (None=全池; 设值时取分数最高的若干只)
    # ── B 持仓层排序信号: top_score 取前 N 的排序依据 (pool 的余额截断与此解耦) ──
    #   "double_low": 双低值 = 转债价格 + 转股溢价率×100, 升序 (经典双低轮动)。
    #   "deviation" : 模型偏差 (市价-理论价)/理论价 升序 (最低估优先, 纯 PDE 信号)。
    # **默认从 "score" 改成 "deviation"**: 机会分及其字段已整体删除 (实测 95% 的行
    # 低估项恒为 0, 它度量的是信用质量而非错定价)。让这里跟上 GUI 的
    # ``STRATEGY_PDE_RANK_SIGNAL_LEGACY_ALIASES`` 是消除分叉, 不是新立一个决定。
    # 下修优势 (``down_reset_edge`` / ``down_reset_robust_edge``) 已随隐含下修强度反解
    # 一并删除, 旧值由 ``_normalize_rank_signal`` 落到这里。
    rank_signal: str = "deviation"
    # 下修提议/通过/拒绝公告落地后, 在下一可成交收盘退出, 余下时间持有现金, 而不是
    # 机械等到下一调仓边界。**默认从 True 改成 False**: 它此前只在排序信号是下修优势时
    # 才被激活, 而那个信号已整体删除; 留着 True 会让事件退出突然对所有回测生效。
    down_reset_event_exit: bool = False
    # ── C 资金层: 未建仓/缺成交价的槽位怎么办 ──
    #   "reserve_cash": 留现金 (分母=目标槽位数; top_score 下=top_n, pool 下=候选数)。
    #   "full_invest" : 满仓等权, 缺口/缺价权重摊回已持仓 (分母=实际持仓数)。
    funding_mode: str = "reserve_cash"
    # ── D 仓位层 (可选): 按当期全市场估值水平缩放总仓位 ──
    #   "full"     : 恒定满仓 (默认, 行为与历史版本一致)。
    #   "valuation": gross = clip(1 - k·max(0, medDev), floor, 1.0), medDev = 当期
    #                **已定价池** (非候选子集) deviation 中位数, 逐期点时计算, 自包含无未来函数。
    # 依据见 docs/research/2026-06-score-ic-and-valuation-timing.md: 聚合中位偏差与
    # 下季指数收益 corr≈-0.52; 同组合离线对照 Sharpe 0.59→0.70 / MDD 12.4%→7.7%
    # (以收益换风险的风险预算工具)。研究配置, 默认关闭。
    exposure_mode: str = "full"
    exposure_valuation_k: float = 2.5   # medDev 每 +1, gross 减 k (映射斜率, 锚点 +20%→半仓)
    exposure_floor: float = 0.5         # gross 下限
    # 闲置现金年化收益率 (如 0.02≈货基)。默认 0 = 旧行为; 但注意 Sharpe 课征 rf 门槛,
    # 现金 0 计息会系统性低估一切持现金配置 (留现金/择时缩放), 研究运行建议设为 r。
    cash_yield_rate: float = 0.0


@dataclass(frozen=True)
class PDEStrategyConfig(ScoreStrategyConfig):
    """PDE 策略的推荐默认口径。

    主策略为估值错定价：按模型偏差升序取前 N，公告后下一可得收盘退出。
    未满 Top N 的仓位保留现金。
    """

    execution_timing: str = "next_close"
    transaction_cost: float = 0.002
    rank_signal: str = "deviation"
    cash_yield_rate: float = 0.022


@dataclass(frozen=True)
class PricePoint:
    """某只转债在一个交易日上的可用成交价格."""

    date: date
    price: float


class _BacktestCacheProvider(DataProvider):
    """单次策略回测内的数据源缓存层.

    批量定价、成交价查询和日频估值都会反复访问同一批历史行情/条款。把缓存放在
    provider 装饰器里, 可以让下游 helper 无感复用, 同时保持现有 DataProvider
    契约不变。
    """

    def __init__(
        self,
        inner: DataProvider,
        *,
        start_date: date,
        end_date: date,
        price_lookback_days: int,
        execution_lookahead_days: int,
        vol_window_days: int,
    ):
        self.inner = inner
        self.name = f"{inner.name}+btcache"
        lookback = max(price_lookback_days, vol_window_days * 3 + 30)
        self._history_start = start_date - timedelta(days=lookback + 15)
        # 批量历史区间不越过昨天: 未来日期本就无数据, 且越过今天会让 DiskCacheProvider 的
        # "只缓存严格过去"守卫拒绝落盘, 导致跨运行复跑重复拉取全部历史 (实测 6 小时级)。
        padded_end = end_date + timedelta(days=max(1, execution_lookahead_days) + 15)
        self._history_end = min(padded_end, market_today() - timedelta(days=1))
        self._bond_history: dict[str, list[tuple[date, float | None]]] = {}
        self._stock_history: dict[str, list[tuple[date, float | None]]] = {}
        self._bond_history_exact: dict[tuple, list[tuple[date, float | None]]] = {}
        self._stock_history_exact: dict[tuple, list[tuple[date, float | None]]] = {}
        self._terms: dict[tuple[str, date], Any] = {}
        self._diagnostics: dict[tuple[str, date], dict[str, Any]] = {}
        self._stock_close: dict[tuple[str, date], float] = {}
        self.stats: Counter = Counter()

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def cache_identity(self) -> str:
        return _provider_cache_identity(self.inner)

    def terms_as_of(self, bond_code: str, valuation_date: date) -> date | None:
        """透传内层的条款截止日 —— 装饰器不改变条款来源, 就不能改变它的口径锚。"""
        return self.inner.terms_as_of(bond_code, valuation_date)

    def get_bond_terms(self, bond_code: str, valuation_date: date):
        key = (bond_code, valuation_date)
        if key in self._terms:
            self.stats["terms_hits"] += 1
            return self._terms[key]
        self.stats["terms_misses"] += 1
        terms = self.inner.get_bond_terms(bond_code, valuation_date)
        self._terms[key] = terms
        return terms

    def get_stock_close(self, stock_code: str, on_date: date) -> float:
        key = (stock_code, on_date)
        if key in self._stock_close:
            self.stats["stock_close_hits"] += 1
            return self._stock_close[key]
        self.stats["stock_close_misses"] += 1
        value = self.inner.get_stock_close(stock_code, on_date)
        self._stock_close[key] = value
        return value

    def get_stock_history(self, stock_code: str, start: date, end: date):
        if start >= self._history_start and end <= self._history_end:
            if stock_code not in self._stock_history:
                self.stats["stock_history_misses"] += 1
                self._stock_history[stock_code] = self.inner.get_stock_history(
                    stock_code, self._history_start, self._history_end)
            else:
                self.stats["stock_history_hits"] += 1
            return _slice_history(self._stock_history[stock_code], start, end)
        key = (stock_code, start, end)
        if key in self._stock_history_exact:
            self.stats["stock_history_hits"] += 1
            return self._stock_history_exact[key]
        self.stats["stock_history_misses"] += 1
        history = self.inner.get_stock_history(stock_code, start, end)
        self._stock_history_exact[key] = history
        return history

    def get_stock_dividend_yield(self, stock_code, on_date):
        return self.inner.get_stock_dividend_yield(stock_code, on_date)

    def get_bond_history(self, bond_code: str, start: date, end: date):
        if start >= self._history_start and end <= self._history_end:
            if bond_code not in self._bond_history:
                self.stats["bond_history_misses"] += 1
                self._bond_history[bond_code] = self.inner.get_bond_history(
                    bond_code, self._history_start, self._history_end)
            else:
                self.stats["bond_history_hits"] += 1
            return _slice_history(self._bond_history[bond_code], start, end)
        key = (bond_code, start, end)
        if key in self._bond_history_exact:
            self.stats["bond_history_hits"] += 1
            return self._bond_history_exact[key]
        self.stats["bond_history_misses"] += 1
        history = self.inner.get_bond_history(bond_code, start, end)
        self._bond_history_exact[key] = history
        return history

    def get_cashflow(self, bond_code):
        return self.inner.get_cashflow(bond_code)

    def get_risk_free_rate(self, on_date):
        return self.inner.get_risk_free_rate(on_date)

    def get_admission_status(self, bond_code, valuation_date, base_terms=None):
        return self.inner.get_admission_status(bond_code, valuation_date, base_terms)

    def list_bond_announcements(self, bond_code, start, end):
        return self.inner.list_bond_announcements(bond_code, start, end)

    def list_tradable_cbs(self, on_date: date | None = None):
        return self.inner.list_tradable_cbs(on_date)

    def get_terms_source_diagnostics(self, bond_code: str, valuation_date: date) -> dict[str, Any]:
        key = (bond_code, valuation_date)
        if key in self._diagnostics:
            self.stats["diagnostics_hits"] += 1
            return self._diagnostics[key]
        self.stats["diagnostics_misses"] += 1
        describe = getattr(self.inner, "get_terms_source_diagnostics", None)
        if callable(describe):
            diag = describe(bond_code, valuation_date)
        else:
            diag = {
                "bond_code": bond_code,
                "valuation_date": valuation_date,
                "terms_source": "provider",
                "snapshot_date": None,
                "patch_count": 0,
                "event_count": 0,
                "uses_current_fallback": False,
            }
        self._diagnostics[key] = diag
        return diag

    def cache_stats(self) -> dict[str, int]:
        return dict(self.stats)


def build_rebalance_schedule(start_date: date, end_date: date, freq: str = "M") -> list[date]:
    """生成回测边界日期, 首尾始终包含 ``start_date`` / ``end_date``.

    ``D`` 近似按工作日, ``W`` 取周五, ``M`` 取自然月最后一个工作日,
    ``Q`` 取季末月份最后一个工作日。遇到 A 股节假日时, 定价和收益计算会
    自动回退到该日之前最近的有效收盘价。
    """
    if end_date <= start_date:
        raise ValueError("end_date 必须晚于 start_date")

    freq_key = (freq or "M").upper()
    if freq_key not in {"D", "W", "M", "Q"}:
        raise ValueError(f"未知调仓频率: {freq}")

    points: set[date] = {start_date, end_date}
    d = start_date + timedelta(days=1)
    while d < end_date:
        if freq_key == "D" and d.weekday() < 5:
            points.add(d)
        elif freq_key == "W" and d.weekday() == 4:
            points.add(d)
        elif freq_key in {"M", "Q"}:
            month_end = _last_weekday_of_month(d.year, d.month)
            is_freq_month = freq_key == "M" or d.month in {3, 6, 9, 12}
            if is_freq_month and d == month_end:
                points.add(d)
        d += timedelta(days=1)
    return sorted(points)


@dataclass
class _RebalanceContext:
    """单个调仓区间所需的不变上下文 (整个回测期共用一份)。

    把 ``backtest_score_strategy`` 的众多入参收拢到这里, 让 ``_run_rebalance_period``
    的签名保持简洁。``performance_stats`` 是 Counter, 区间内原地累加并在外层复用。
    """
    provider: DataProvider
    bond_codes: list[str]
    cfg: ScoreStrategyConfig
    terms_cache: Any
    admission_config: AdmissionFilterConfig | None
    total_periods: int
    performance_stats: Counter
    pricing_snapshot_cache: dict[Any, list[dict[str, Any]]] | None
    stage_cb: Any
    cancel_cb: Any
    r: float
    base_spread: float
    distress_k: float
    p_down: float
    vol_window_days: int
    sigma: float | None
    q: float | None
    M: int
    N: int
    max_workers: int | None
    pricer_overrides: dict[str, Any]


@dataclass
class _PeriodResult:
    """``_run_rebalance_period`` 的产出, 由外层累积成净值曲线与逐期记录。"""
    period: dict[str, Any]
    snapshot: dict[str, Any]
    equity: float
    benchmark_equity: float
    benchmark_point: dict[str, Any] | None
    selected_codes: list[str]
    held_codes: list[str]        # 期末仍持有的标的码 (不含缺价票和期内事件退出)
    weight_denominator: int      # 本期等权份数分母 (intended), 供下期换手计算
    exposure: float              # 本期总仓位 gross (D 仓位层), 供下期换手计算
    benchmark_codes: list[str]   # 本期基准成分 (供下期基准换手/成本计算)


def validate_strategy_config(cfg: ScoreStrategyConfig) -> None:
    """枚举/取值 fail-fast: 非法配置在任何取数/定价之前抛 ValueError。

    避免 Wind 高保真跑完第一期准入+定价才在 ``_normalize_*`` 处炸掉、白烧配额;
    ``sweep_score_strategy`` 也用它在跑任何变体前校验全部变体。
    """
    if cfg.top_n <= 0:
        raise ValueError("top_n 必须为正整数")
    _normalize_holding_mode(cfg.holding_mode)
    _normalize_rank_signal(cfg.rank_signal)
    _normalize_funding_mode(cfg.funding_mode)
    _normalize_exposure_mode(cfg.exposure_mode)
    _normalize_execution_timing(cfg.execution_timing)


def strategy_type_for_rank_signal(value: str | None) -> str:
    """排序信号对应的产品策略类型；legacy 仅用于旧快照兼容。"""
    signal = _normalize_rank_signal(value)
    if signal == "deviation":
        return "pde_valuation"
    return "legacy"


def _strategy_config_summary(cfg: ScoreStrategyConfig) -> dict[str, Any]:
    """回测结果里回显的配置快照 (供 GUI/CSV 展示与复现)。"""
    holding_mode = _normalize_holding_mode(cfg.holding_mode)
    funding_mode = _normalize_funding_mode(cfg.funding_mode)
    rank_signal = _normalize_rank_signal(cfg.rank_signal)
    return {
        "strategy_type": strategy_type_for_rank_signal(rank_signal),
        "top_n": cfg.top_n,
        "holding_mode": holding_mode,
        "rank_signal": rank_signal,
        "down_reset_event_exit": bool(cfg.down_reset_event_exit),
        "max_holdings": cfg.max_holdings,
        "funding_mode": funding_mode,
        # 兼容旧快照/GUI 的派生镜像 (新接口请读 holding_mode/funding_mode)
        "top_n_shortfall_policy": _funding_legacy_alias(funding_mode),
        "rebalance_freq": cfg.rebalance_freq,
        "selection_view": cfg.selection_view,
        "min_confidence": list(cfg.min_confidence) if cfg.min_confidence else None,
        "exclude_risk_tags": list(cfg.exclude_risk_tags),
        "min_market_price": cfg.min_market_price,
        "max_market_price": cfg.max_market_price,
        "min_conversion_premium": cfg.min_conversion_premium,
        "max_conversion_premium": cfg.max_conversion_premium,
        "min_deviation": cfg.min_deviation,
        "max_deviation": cfg.max_deviation,
        "min_sigma": cfg.min_sigma,
        "max_sigma": cfg.max_sigma,
        # 2026-08-31 标签→阈值重构引入的**主口径**七条。它们是 _candidate_filter_reason
        # (唯一的选债路径) 真正在读的东西, 而这份快照的职责就是"供 GUI/CSV 展示与复现"
        # —— 漏掉它们等于快照复现不出那次运行。上面那批 (exclude_risk_tags / 价格带 /
        # 溢价 / 偏差 / σ) 是重构**之前**的字段, 留着是为了旧快照能读。
        "max_model_premium": cfg.max_model_premium,
        "max_relative_deviation": cfg.max_relative_deviation,
        "min_years_to_maturity": cfg.min_years_to_maturity,
        "min_credit_rating": cfg.min_credit_rating,
        "min_outstanding_balance": cfg.min_outstanding_balance,
        "exclude_underlying_st": bool(cfg.exclude_underlying_st),
        "exclude_underlying_limit_down": bool(cfg.exclude_underlying_limit_down),
        "price_lookback_days": cfg.price_lookback_days,
        "max_price_staleness_days": cfg.max_price_staleness_days,
        "execution_timing": _normalize_execution_timing(cfg.execution_timing),
        "execution_lookahead_days": cfg.execution_lookahead_days,
        "mark_to_market": cfg.mark_to_market,
        "pre_filter_prices": cfg.pre_filter_prices,
        "transaction_cost": cfg.transaction_cost,
        "compute_benchmark": cfg.compute_benchmark,
        "benchmark_index_code": cfg.benchmark_index_code,
        "pool_mode": cfg.pool_mode,
        "exposure_mode": _normalize_exposure_mode(cfg.exposure_mode),
        "exposure_valuation_k": cfg.exposure_valuation_k,
        "exposure_floor": cfg.exposure_floor,
        "cash_yield_rate": cfg.cash_yield_rate,
    }


def _run_rebalance_period(
    ctx: _RebalanceContext,
    idx: int,
    period_start: date,
    period_end: date,
    *,
    previous_held_codes: list[str],
    previous_intended: int,
    previous_exposure: float,
    previous_benchmark_codes: list[str],
    start_equity: float,
    benchmark_equity: float,
    equity_curve: list[dict[str, Any]],
) -> _PeriodResult:
    """跑单个调仓区间: 准入→价格预筛→定价→选债→持仓估值→净值/基准更新→区间摘要。

    ``equity_curve`` 原地 upsert; equity / benchmark_equity / previous_codes 通过返回值
    回传给外层累积。``ctx.performance_stats`` 原地累加。
    """
    cfg = ctx.cfg
    provider = ctx.provider
    rank_signal = _normalize_rank_signal(cfg.rank_signal)
    total_periods = ctx.total_periods
    stage_cb = ctx.stage_cb
    cancel_cb = ctx.cancel_cb

    _check_cancel(cancel_cb)
    if cfg.pool_mode == "dynamic":
        period_codes = _dynamic_pool_for_date(
            provider, ctx.bond_codes, period_start, terms_cache=ctx.terms_cache)
    else:
        period_codes = ctx.bond_codes
    _emit_stage_progress(stage_cb, "准入筛选", 0, len(period_codes), idx, total_periods)
    eligible, excluded, source_diagnostics = _eligible_codes_for_date(
        provider,
        period_codes,
        period_start,
        terms_cache=ctx.terms_cache,
        admission_config=ctx.admission_config,
        progress_cb=lambda done, total: _emit_stage_progress(
            stage_cb, "准入筛选", done, total, idx, total_periods),
        cancel_cb=cancel_cb,
    )
    _raise_if_source_transport_outage(
        excluded,
        total_count=len(period_codes),
        period_start=period_start,
        phase="准入筛选",
    )
    _emit_stage_progress(stage_cb, "价格预筛", 0, len(eligible), idx, total_periods)
    pricing_codes, prefilter_excluded, price_band_excluded = _pre_filter_codes_by_price(
        provider,
        eligible,
        period_start,
        cfg,
        progress_cb=lambda done, total: _emit_stage_progress(
            stage_cb, "价格预筛", done, total, idx, total_periods),
        cancel_cb=cancel_cb,
    )
    if prefilter_excluded:
        excluded.extend(prefilter_excluded)
        ctx.performance_stats["price_prefilter_excluded"] += len(prefilter_excluded)
    _emit_stage_progress(stage_cb, "定价", 0, len(pricing_codes), idx, total_periods)
    pricing_overrides = dict(ctx.pricer_overrides)
    priced_rows = _batch_price_with_snapshot_cache(
        provider,
        pricing_codes,
        snapshot_cache=ctx.pricing_snapshot_cache,
        stats=ctx.performance_stats,
        r=ctx.r,
        base_spread=ctx.base_spread,
        distress_k=ctx.distress_k,
        p_down=ctx.p_down,
        valuation_date=period_start,
        vol_window_days=ctx.vol_window_days,
        sigma=ctx.sigma,
        q=ctx.q,
        M=ctx.M,
        N=ctx.N,
        max_workers=ctx.max_workers,
        progress_cb=lambda done, total: _emit_stage_progress(
            stage_cb, "定价", done, total, idx, total_periods),
        **pricing_overrides,
    )
    _raise_if_pricing_transport_outage(
        priced_rows,
        total_count=len(pricing_codes),
        period_start=period_start,
    )

    # 转债成交价缓存: 策略持仓与基准共享, 避免同一调仓期重复拉历史。
    price_cache: dict[tuple, PricePoint | None] = {}
    candidates = _select_candidate_rows(priced_rows, cfg)
    # B 持仓层: 先按排序信号重排候选, 再构成持仓
    candidates = _sort_candidates_by_rank_signal(candidates, rank_signal)
    holding_mode = _normalize_holding_mode(cfg.holding_mode)
    if holding_mode == "pool":
        # 等权持有整个候选池 (不按机会分精排)。max_holdings 截断按**余额降序**
        # (流动性代理), 避免分数从截断的后门回流; 同余额按代码排序保证确定性。
        cap = cfg.max_holdings if cfg.max_holdings else len(candidates)
        cap = max(0, int(cap))
        if cap < len(candidates):
            selected = sorted(
                candidates,
                key=lambda row: (
                    -(finite_float(row.get("outstanding_balance")) or 0.0),
                    str(row.get("bond_code") or ""),
                ),
            )[:cap]
        else:
            selected = list(candidates)
    else:  # top_score: 按当前排序信号取前 top_n
        selected = candidates[:cfg.top_n]
    selected_codes = [str(row.get("bond_code")) for row in selected]
    candidate_rows = _candidate_explanation_rows(
        candidates, selected_codes, cfg, rank_signal=rank_signal)
    rejection_rows = _rejection_explanation_rows(
        priced_rows,
        excluded,
        cfg,
        candidate_codes={str(row.get("bond_code")) for row in candidates},
    )
    _emit_stage_progress(stage_cb, "持仓估值", 0, len(selected), idx, total_periods)
    # 此前这道门是 "排序信号是下修优势 **且** 开了 down_reset_event_exit"。下修优势信号
    # 已整体删除, 于是唯一的自动触发路径没了。默认值同时从 True 改成 False, 是为了**保住
    # 既有行为**: 按 deviation 排序的回测在删除前就走不到这条路 (第一个条件恒假), 若只删
    # 前半个条件、留着 True, 事件退出会突然对所有回测生效 —— 那是默认选债行为变更。
    event_exit_store = (
        _event_store_from_provider(provider) if cfg.down_reset_event_exit else None
    )
    positions, skipped_positions = _position_returns(
        provider,
        selected,
        period_start,
        period_end,
        lookback_days=cfg.price_lookback_days,
        max_staleness_days=cfg.max_price_staleness_days,
        execution_timing=cfg.execution_timing,
        execution_lookahead_days=cfg.execution_lookahead_days,
        price_cache=price_cache,
        event_exit_store=event_exit_store,
        cash_yield_rate=cfg.cash_yield_rate,
        rank_signal=rank_signal,
    )
    _emit_stage_progress(stage_cb, "持仓估值", len(selected), len(selected), idx, total_periods)

    # C 资金层: 等权份数分母 (intended)
    funding_mode = _normalize_funding_mode(cfg.funding_mode)
    held = len(positions)            # 实际有成交价、能建仓的标的数
    initial_held_codes = [str(pos.get("bond_code")) for pos in positions]
    event_exit_positions = [
        pos for pos in positions if pos.get("exit_reason") == "down_reset_event"
    ]
    held_codes = [
        str(pos.get("bond_code"))
        for pos in positions
        if pos.get("exit_reason") != "down_reset_event"
    ]
    if funding_mode == "full_invest":
        # 满仓等权: 分母=实际持仓; 未建仓/缺价权重摊回已持仓 (不留现金)。
        intended = held
    else:
        # reserve_cash: 分母=目标槽位 (top_score→top_n, pool→候选数); 未建仓/缺价槽位留现金。
        target = cfg.top_n if holding_mode == "top_score" else len(selected)
        intended = max(0, int(target))
    # D 仓位层: 按当期已定价池中位 deviation 缩放总仓位 (点时, 自包含)
    exposure, median_deviation = _resolve_exposure(cfg, priced_rows)
    # 换手/成本基于**实际持仓码**与各期 gross (非含缺价的 selected); 上期持仓码/分母/
    # gross 由编排层顺延。reserve_cash 下分母>持仓数, 缺口/缺价自然计入现金、不算换手。
    rebalance_turnover = _equal_weight_turnover(
        previous_held_codes,
        initial_held_codes,
        previous_denominator=previous_intended,
        current_denominator=intended,
        previous_gross=previous_exposure,
        current_gross=exposure,
    )
    event_exit_turnover = (
        exposure * len(event_exit_positions) / intended
        if intended > 0 else 0.0
    )
    turnover = rebalance_turnover + event_exit_turnover

    # 等权持有 top_n; 缺收盘价无法建仓的标的按现金(0 收益)计入分母; gross 缩放整体仓位。
    if intended > 0:
        for pos in positions:
            pos["weight"] = exposure / intended
            pos["return_contribution"] = exposure * float(pos["period_return"]) / intended
        gross_return = exposure * float(sum(p["period_return"] for p in positions) / intended)
        cash_weight = 1.0 - exposure * (held / intended)
    else:
        gross_return = 0.0
        cash_weight = 1.0
    event_exit_cash_yield_return = (
        exposure * sum(
            float(pos.get("post_exit_cash_return") or 0.0)
            for pos in event_exit_positions
        ) / intended
        if intended > 0 else 0.0
    )
    end_cash_weight = min(1.0, cash_weight + event_exit_turnover)
    period_start_equity = start_equity
    cost = turnover * cfg.transaction_cost
    # 闲置现金按年化 cash_yield_rate 计息 (默认 0 = 旧行为)。不计息时, Sharpe 的
    # rf 门槛会系统性惩罚一切持现金配置 (缺口留现金 / 择时缩放)——内部不一致。
    period_days = max(0, (period_end - period_start).days)
    event_exit_time_weighted_cash = (
        sum(
            exposure / intended
            * max(0, (period_end - pos["exit_date"]).days)
            / period_days
            for pos in event_exit_positions
            if isinstance(pos.get("exit_date"), date)
        )
        if intended > 0 and period_days > 0 else 0.0
    )
    average_cash_weight = min(1.0, cash_weight + event_exit_time_weighted_cash)
    cash_yield_return = cash_weight * cfg.cash_yield_rate * period_days / 365.0
    period_return = gross_return + cash_yield_return - cost
    equity = period_start_equity * (1.0 + period_return)
    if cfg.mark_to_market:
        curve_points = _portfolio_mark_to_market_curve(
            provider,
            positions,
            start_equity=period_start_equity,
            period_start=period_start,
            period_end=period_end,
            cost=cost,
            intended_count=intended,
            exposure=exposure,
            cash_weight=cash_weight,
            cash_yield_rate=cfg.cash_yield_rate,
        )
        _upsert_equity_points(equity_curve, curve_points)
        if curve_points:
            equity = float(curve_points[-1]["equity"])
    else:
        _upsert_equity_points(equity_curve, [{"date": period_end, "equity": equity}])

    benchmark_return = None
    benchmark_point = None
    benchmark_codes: list[str] = list(previous_benchmark_codes)
    new_benchmark_equity = benchmark_equity
    if cfg.compute_benchmark:
        _emit_stage_progress(stage_cb, "基准估值", 0, len(priced_rows), idx, total_periods)
        # **价格带剔除的债要加回基准**。价格带是 ScoreStrategyConfig 的策略阈值, 而
        # ``_benchmark_period_return`` 的 docstring 明说"基准刻意不过策略的筛子 —— 唯一
        # 的闸是 status == ok"。让基准也过一遍价格带, 衡量的就只剩"在同一批候选里排序
        # 排得好不好", 而"避开了太贵/太便宜的那一段"这个真实决策的贡献被算进基准里抵消掉。
        # 这些债没有定价结果 (预筛在定价之前), 但基准只用成交价, 不用 PDE 输出 ——
        # 给一个最小行即可; 取不到成交价的那一档基准自己会跳过。
        benchmark_rows = list(priced_rows) + [
            {"bond_code": code, "status": "ok"} for code in price_band_excluded]
        benchmark_return, benchmark_codes = _benchmark_period_return(
            provider,
            benchmark_rows,
            period_start,
            period_end,
            lookback_days=cfg.price_lookback_days,
            max_staleness_days=cfg.max_price_staleness_days,
            execution_timing=cfg.execution_timing,
            execution_lookahead_days=cfg.execution_lookahead_days,
            price_cache=price_cache,
        )
        # 基准与策略同口径计成本 (等权满仓的成员变动换手), 消除"策略计费/基准免费"的不对称
        if benchmark_return is not None and cfg.transaction_cost:
            bench_turnover = _equal_weight_turnover(
                previous_benchmark_codes, benchmark_codes)
            benchmark_return -= bench_turnover * cfg.transaction_cost
        new_benchmark_equity = benchmark_equity * (1.0 + (benchmark_return or 0.0))
        benchmark_point = {"date": period_end, "equity": new_benchmark_equity}
        _emit_stage_progress(stage_cb, "基准估值", len(priced_rows), len(priced_rows), idx, total_periods)

    rank_values = [_rank_signal_value(row, rank_signal) for row in selected]
    finite_rank_values = [value for value in rank_values if value is not None]
    snapshot = {
        "date": period_start,
        "eligible_count": len(eligible),
        "excluded_count": len(excluded),
        "pricing_count": len(pricing_codes),
        "pre_filtered_count": len(prefilter_excluded),
        "priced_count": sum(1 for row in priced_rows if row.get("status") == "ok"),
        "failed_count": sum(1 for row in priced_rows if row.get("status") != "ok"),
        "candidate_count": len(candidates),
        "selected_count": len(selected),
        "event_exit_count": len(event_exit_positions),
        "selected_codes": selected_codes,
        "rank_signal": rank_signal,
        "avg_rank_value": (
            sum(finite_rank_values) / len(finite_rank_values)
            if finite_rank_values else None
        ),
        "candidate_rows": candidate_rows,
        "rejection_rows": rejection_rows,
        "data_quality": _period_data_quality(source_diagnostics),
    }
    period = {
        "start_date": period_start,
        "end_date": period_end,
        "period_return": period_return,
        "gross_return": gross_return,
        "cash_yield_return": cash_yield_return,
        "cost": cost,
        "cash_weight": cash_weight,
        "average_cash_weight": average_cash_weight,
        "end_cash_weight": end_cash_weight,
        "rebalance_turnover": rebalance_turnover,
        "event_exit_turnover": event_exit_turnover,
        "event_exit_count": len(event_exit_positions),
        "event_exit_cash_yield_return": event_exit_cash_yield_return,
        "holding_mode": holding_mode,
        "rank_signal": rank_signal,
        "funding_mode": funding_mode,
        "top_n_shortfall_policy": _funding_legacy_alias(funding_mode),  # 兼容旧快照/GUI
        "target_count": cfg.top_n if holding_mode == "top_score" else len(selected),
        "weight_denominator": intended,
        "benchmark_return": benchmark_return,
        "equity": equity,
        "benchmark_equity": new_benchmark_equity if cfg.compute_benchmark else None,
        "turnover": turnover,
        "exposure": exposure,
        "median_deviation": median_deviation,
        "execution_timing": _normalize_execution_timing(cfg.execution_timing),
        "entry_date": _min_position_date(positions, "entry_date"),
        "exit_date": _max_position_date(positions, "exit_date"),
        "positions": positions,
        "skipped_positions": skipped_positions,
        "excluded_reasons": excluded,
        **snapshot,
    }
    return _PeriodResult(
        period=period,
        snapshot=snapshot,
        equity=equity,
        benchmark_equity=new_benchmark_equity,
        benchmark_point=benchmark_point,
        selected_codes=selected_codes,
        held_codes=held_codes,
        weight_denominator=intended,
        exposure=exposure,
        benchmark_codes=benchmark_codes,
    )


def backtest_score_strategy(
    provider: DataProvider,
    bond_codes: list[str],
    *,
    start_date: date,
    end_date: date,
    config: ScoreStrategyConfig | None = None,
    terms_cache=None,
    admission_config: AdmissionFilterConfig | None = None,
    r: float = 0.022,
    base_spread: float = 0.03,
    distress_k: float = 0.05,
    p_down: float = DEFAULT_BACKGROUND_P_DOWN,
    vol_window_days: int = 21,
    sigma: float | None = None,
    q: float | None = None,
    M: int = 300,
    N: int = 1000,
    max_workers: int | None = None,
    use_runtime_cache: bool = True,
    pricing_snapshot_cache: dict[Any, list[dict[str, Any]]] | None = None,
    progress_cb=None,
    stage_cb=None,
    cancel_cb=None,
    **pricer_overrides,
) -> dict[str, Any]:
    """回测 PDE 错定价选债策略.

    返回结构包含:
      - ``equity_curve``: 组合净值点位
      - ``benchmark_curve``: 等权全可投池基准净值 (``compute_benchmark`` 开启时)
      - ``periods``: 每个持有区间的收益、持仓和候选池统计
      - ``rebalance_snapshots``: 每次调仓的候选/选中摘要
      - ``summary``: 总收益、年化、回撤、波动率、胜率、Sharpe、超额等指标

    净值口径:
      - 默认按 ``top_n`` 固定仓位分母等权; 未满 Top N 和缺期初/期末成交价的
        仓位按现金(0 收益)计入, 避免少数可成交标的把组合静默放大成高集中度。
      - 区间净收益 = 毛收益 - ``turnover * transaction_cost`` (单边换手 × 成本率)。
      - 基准为每个调仓日"全部通过准入且已定价"标的的等权收益, 表示买下整个筛选池
        的参照线; 用于衡量当前排序信号带来的超额。
    """
    cfg = config or ScoreStrategyConfig()
    validate_strategy_config(cfg)
    if not bond_codes:
        raise ValueError("bond_codes 不能为空")
    runtime_cache_provider = None
    if use_runtime_cache:
        runtime_cache_provider = _BacktestCacheProvider(
            provider,
            start_date=start_date,
            end_date=end_date,
            price_lookback_days=cfg.price_lookback_days,
            execution_lookahead_days=cfg.execution_lookahead_days,
            vol_window_days=vol_window_days,
        )
        provider = runtime_cache_provider

    schedule = build_rebalance_schedule(start_date, end_date, cfg.rebalance_freq)
    periods: list[dict[str, Any]] = []
    snapshots: list[dict[str, Any]] = []
    equity_curve = [{"date": schedule[0], "equity": 1.0}]
    benchmark_curve = [{"date": schedule[0], "equity": 1.0}] if cfg.compute_benchmark else []
    equity = 1.0
    benchmark_equity = 1.0
    previous_held_codes: list[str] = []
    previous_intended = 0
    previous_exposure = 1.0
    previous_benchmark_codes: list[str] = []
    total_periods = len(schedule) - 1
    performance_stats: Counter = Counter()

    ctx = _RebalanceContext(
        provider=provider,
        bond_codes=bond_codes,
        cfg=cfg,
        terms_cache=terms_cache,
        admission_config=admission_config,
        total_periods=total_periods,
        performance_stats=performance_stats,
        pricing_snapshot_cache=pricing_snapshot_cache,
        stage_cb=stage_cb,
        cancel_cb=cancel_cb,
        r=r,
        base_spread=base_spread,
        distress_k=distress_k,
        p_down=p_down,
        vol_window_days=vol_window_days,
        sigma=sigma,
        q=q,
        M=M,
        N=N,
        max_workers=max_workers,
        pricer_overrides=pricer_overrides,
    )

    # 跨运行磁盘缓存 (DiskCacheProvider, 经 _BacktestCacheProvider.__getattr__ 链可达)
    # 的阶段性落盘句柄: 多小时高保真拉取中途进程被杀也只丢当期数据。
    # flush 为原子写且无脏数据时零成本; 链上无 flush 能力时为 None, 安全跳过。
    provider_flush = getattr(provider, "flush", None)

    for idx, period_start in enumerate(schedule[:-1]):
        period_end = schedule[idx + 1]
        res = _run_rebalance_period(
            ctx,
            idx,
            period_start,
            period_end,
            previous_held_codes=previous_held_codes,
            previous_intended=previous_intended,
            previous_exposure=previous_exposure,
            previous_benchmark_codes=previous_benchmark_codes,
            start_equity=equity,
            benchmark_equity=benchmark_equity,
            equity_curve=equity_curve,
        )
        equity = res.equity
        benchmark_equity = res.benchmark_equity
        previous_held_codes = res.held_codes
        previous_intended = res.weight_denominator
        previous_exposure = res.exposure
        previous_benchmark_codes = res.benchmark_codes
        if res.benchmark_point is not None:
            benchmark_curve.append(res.benchmark_point)
        snapshots.append(res.snapshot)
        periods.append(res.period)
        if progress_cb:
            progress_cb(idx + 1, total_periods)
        if callable(provider_flush):
            provider_flush()

    index_benchmark_curve = (
        _index_benchmark_curve(provider, cfg.benchmark_index_code, schedule, cfg)
        if cfg.compute_benchmark and cfg.benchmark_index_code else []
    )
    summary = _summarize_strategy(
        equity_curve,
        periods,
        start_date=schedule[0],
        end_date=schedule[-1],
        freq=cfg.rebalance_freq,
        top_n=cfg.top_n,
        risk_free_rate=r,
        benchmark_curve=benchmark_curve if cfg.compute_benchmark else None,
        index_benchmark_curve=index_benchmark_curve,
    )
    diagnostics = _build_strategy_diagnostics(
        equity_curve,
        periods,
        summary,
    )
    if runtime_cache_provider is not None:
        performance_stats.update({
            f"runtime_cache.{key}": value
            for key, value in runtime_cache_provider.cache_stats().items()
        })
    diagnostics["performance"] = dict(performance_stats)
    return {
        "start_date": schedule[0],
        "end_date": schedule[-1],
        "config": _strategy_config_summary(cfg),
        "equity_curve": equity_curve,
        "benchmark_curve": benchmark_curve,
        "index_benchmark_curve": index_benchmark_curve,
        "periods": periods,
        "rebalance_snapshots": snapshots,
        "summary": summary,
        "diagnostics": diagnostics,
    }


def backtest_pde_strategy(
    provider: DataProvider,
    bond_codes: list[str],
    *,
    start_date: date,
    end_date: date,
    config: PDEStrategyConfig | None = None,
    **kwargs,
) -> dict[str, Any]:
    """PDE 策略主入口；旧 ``backtest_score_strategy`` 继续作为兼容接口。"""
    return backtest_score_strategy(
        provider,
        bond_codes,
        start_date=start_date,
        end_date=end_date,
        config=config or PDEStrategyConfig(),
        **kwargs,
    )


_SUMMARY_CSV_KEYS = (
    "periods", "final_equity", "total_return", "annualized_return",
    "annualized_volatility", "volatility_basis", "sharpe", "sortino",
    "calmar", "max_drawdown", "max_drawdown_days", "hit_rate",
    "avg_selected_count", "avg_turnover", "avg_cash_weight", "avg_end_cash_weight",
    "total_event_exits", "total_cost",
    "benchmark_final_equity", "benchmark_total_return", "excess_return",
    "index_benchmark_total_return", "excess_vs_index", "index_covers_full_window",
)


_PERIOD_CSV_COLUMNS = [
    "start_date", "end_date", "entry_date", "exit_date",
    "period_return", "gross_return", "cash_yield_return", "event_exit_cash_yield_return",
    "cost",
    "benchmark_return", "equity", "benchmark_equity", "turnover", "cash_weight",
    "average_cash_weight",
    "end_cash_weight", "rebalance_turnover", "event_exit_turnover", "event_exit_count",
    "exposure", "median_deviation",
    "eligible_count", "priced_count", "candidate_count", "selected_count",
    "rank_signal", "avg_rank_value", "execution_timing", "selected_codes",
]


def _flatten_period_rows(periods: list[dict[str, Any]], key: str) -> list[tuple[dict, dict]]:
    """把每个区间下 ``period[key]`` 的明细行摊平成 (period, row) 对, 供持仓/候选/拒绝区块复用."""
    return [(period, row) for period in periods for row in period.get(key, [])]


def _write_csv_config(writer, config: dict[str, Any]) -> None:
    if not config:
        return
    writer.writerow(["# config"])
    for key, value in config.items():
        writer.writerow([key, _csv_value(value)])
    writer.writerow([])


def _write_csv_periods(writer, periods: list[dict[str, Any]]) -> None:
    writer.writerow(_PERIOD_CSV_COLUMNS)
    for row in periods:
        values = []
        for column in _PERIOD_CSV_COLUMNS:
            if column == "selected_codes":
                value = "|".join(str(code) for code in row.get(column) or [])
            else:
                value = _csv_value(row.get(column))
            values.append(value)
        writer.writerow(values)


def _write_csv_equity_curve(writer, curve: list[dict[str, Any]]) -> None:
    if not curve:
        return
    writer.writerow([])
    writer.writerow(["# equity_curve"])
    writer.writerow(["date", "equity"])
    for row in curve:
        writer.writerow([_csv_value(row.get("date")), _csv_value(row.get("equity"))])


def _write_csv_positions(writer, periods: list[dict[str, Any]]) -> None:
    positions = _flatten_period_rows(periods, "positions")
    if not positions:
        return
    writer.writerow([])
    writer.writerow(["# positions"])
    writer.writerow([
        "period_start", "period_end", "rank", "bond_code", "bond_name",
        "rank_signal", "rank_value", "signal_market_price", "theoretical_price",
        "deviation", "effective_p_down_1y_prob",
        "entry_date", "exit_date", "start_price", "end_price",
        "price_return", "post_exit_cash_return", "period_return",
        "exit_reason", "exit_signal_date", "exit_event_type", "exit_event_title",
        "confidence", "risk_tags",
    ])
    for period, pos in positions:
        writer.writerow([
            _csv_value(period.get("start_date")),
            _csv_value(period.get("end_date")),
            pos.get("rank", ""),
            pos.get("bond_code", ""),
            pos.get("bond_name", ""),
            pos.get("rank_signal", ""),
            _csv_value(pos.get("rank_value")),
            _csv_value(pos.get("signal_market_price")),
            _csv_value(pos.get("theoretical_price")),
            _csv_value(pos.get("deviation")),
            _csv_value(pos.get("effective_p_down_1y_prob")),
            _csv_value(pos.get("entry_date")),
            _csv_value(pos.get("exit_date")),
            _csv_value(pos.get("start_price")),
            _csv_value(pos.get("end_price")),
            _csv_value(pos.get("price_return")),
            _csv_value(pos.get("post_exit_cash_return")),
            _csv_value(pos.get("period_return")),
            pos.get("exit_reason", ""),
            _csv_value(pos.get("exit_signal_date")),
            pos.get("exit_event_type", ""),
            pos.get("exit_event_title", ""),
            pos.get("confidence", ""),
            "|".join(str(tag) for tag in pos.get("risk_tags") or []),
        ])


def _write_csv_skipped_positions(writer, periods: list[dict[str, Any]]) -> None:
    skipped = _flatten_period_rows(periods, "skipped_positions")
    if not skipped:
        return
    writer.writerow([])
    writer.writerow(["# skipped_positions"])
    writer.writerow([
        "period_start", "period_end", "rank", "bond_code", "bond_name",
        "rank_signal", "rank_value", "reason", "entry_date", "exit_date",
        "start_price", "end_price",
    ])
    for period, pos in skipped:
        writer.writerow([
            _csv_value(period.get("start_date")),
            _csv_value(period.get("end_date")),
            pos.get("rank", ""),
            pos.get("bond_code", ""),
            pos.get("bond_name", ""),
            pos.get("rank_signal", ""),
            _csv_value(pos.get("rank_value")),
            pos.get("reason", ""),
            _csv_value(pos.get("entry_date")),
            _csv_value(pos.get("exit_date")),
            _csv_value(pos.get("start_price")),
            _csv_value(pos.get("end_price")),
        ])


def _write_csv_candidate_rows(writer, periods: list[dict[str, Any]]) -> None:
    candidate_rows = _flatten_period_rows(periods, "candidate_rows")
    if not candidate_rows:
        return
    writer.writerow([])
    writer.writerow(["# candidate_rows"])
    writer.writerow([
        "period_start", "period_end", "rank", "selected", "bond_code", "bond_name",
        "selection_reason", "rank_signal", "rank_value", "market_price",
        "theoretical_price", "deviation", "effective_p_down_1y_prob",
        "conversion_premium", "sigma", "confidence", "risk_tags",
    ])
    for period, row in candidate_rows:
        writer.writerow([
            _csv_value(period.get("start_date")),
            _csv_value(period.get("end_date")),
            row.get("rank", ""),
            row.get("selected", ""),
            row.get("bond_code", ""),
            row.get("bond_name", ""),
            row.get("selection_reason", ""),
            row.get("rank_signal", ""),
            _csv_value(row.get("rank_value")),
            _csv_value(row.get("market_price")),
            _csv_value(row.get("theoretical_price")),
            _csv_value(row.get("deviation")),
            _csv_value(row.get("effective_p_down_1y_prob")),
            _csv_value(row.get("conversion_premium")),
            _csv_value(row.get("sigma")),
            row.get("confidence", ""),
            "|".join(str(tag) for tag in row.get("risk_tags") or []),
        ])


def _write_csv_rejection_rows(writer, periods: list[dict[str, Any]]) -> None:
    rejection_rows = _flatten_period_rows(periods, "rejection_rows")
    if not rejection_rows:
        return
    writer.writerow([])
    writer.writerow(["# rejection_rows"])
    writer.writerow([
        "period_start", "period_end", "source", "bond_code", "bond_name",
        "reason", "rank_signal", "rank_value", "market_price", "deviation",
        "effective_p_down_1y_prob",
        "conversion_premium", "confidence", "risk_tags",
    ])
    for period, row in rejection_rows:
        writer.writerow([
            _csv_value(period.get("start_date")),
            _csv_value(period.get("end_date")),
            row.get("source", ""),
            row.get("bond_code", ""),
            row.get("bond_name", ""),
            row.get("reason", ""),
            row.get("rank_signal", ""),
            _csv_value(row.get("rank_value")),
            _csv_value(row.get("market_price")),
            _csv_value(row.get("deviation")),
            _csv_value(row.get("effective_p_down_1y_prob")),
            _csv_value(row.get("conversion_premium")),
            row.get("confidence", ""),
            "|".join(str(tag) for tag in row.get("risk_tags") or []),
        ])


def _write_csv_summary(writer, summary: dict[str, Any]) -> None:
    if not summary:
        return
    writer.writerow([])
    writer.writerow(["# summary"])
    for key in _SUMMARY_CSV_KEYS:
        writer.writerow([key, _csv_value(summary.get(key))])


def _write_csv_diagnostics(writer, diagnostics: dict[str, Any]) -> None:
    if not diagnostics:
        return
    writer.writerow([])
    writer.writerow(["# diagnostics"])
    data_quality = diagnostics.get("data_quality") or {}
    for key, value in data_quality.items():
        writer.writerow([f"data_quality.{key}", _csv_value(value)])
    attribution = diagnostics.get("attribution") or {}
    for key in ("total_cost", "avg_cash_weight", "skipped_positions", "cost_drag"):
        writer.writerow([f"attribution.{key}", _csv_value(attribution.get(key))])
    warnings = diagnostics.get("warnings") or []
    for idx, warning in enumerate(warnings, start=1):
        writer.writerow([f"warning.{idx}", warning])
    performance = diagnostics.get("performance") or {}
    for key, value in performance.items():
        writer.writerow([f"performance.{key}", _csv_value(value)])
    for section, rows in (
        ("top_contributors", attribution.get("top_contributors") or []),
        ("top_detractors", attribution.get("top_detractors") or []),
        ("yearly_returns", diagnostics.get("yearly_returns") or []),
        ("monthly_returns", diagnostics.get("monthly_returns") or []),
    ):
        if not rows:
            continue
        writer.writerow([])
        writer.writerow([f"# {section}"])
        keys = list(rows[0].keys())
        writer.writerow(keys)
        for row in rows:
            writer.writerow([_csv_value(row.get(key)) for key in keys])


def write_strategy_backtest_csv(path: str | Path, result: dict[str, Any]) -> None:
    """导出策略回测的逐期摘要、日频净值、持仓明细和汇总指标 CSV.

    各区块由独立的 ``_write_csv_*`` 辅助函数写出 (有数据才写空行+标题), 顺序:
    config / 逐期摘要 / equity_curve / positions / skipped_positions /
    candidate_rows / rejection_rows / summary / diagnostics。
    """
    periods = result.get("periods", [])
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        _write_csv_config(writer, result.get("config") or {})
        _write_csv_periods(writer, periods)
        _write_csv_equity_curve(writer, result.get("equity_curve") or [])
        _write_csv_positions(writer, periods)
        _write_csv_skipped_positions(writer, periods)
        _write_csv_candidate_rows(writer, periods)
        _write_csv_rejection_rows(writer, periods)
        _write_csv_summary(writer, result.get("summary") or {})
        _write_csv_diagnostics(writer, result.get("diagnostics") or {})


def _slice_history(history, start: date, end: date):
    return [
        (d, value)
        for d, value in history or []
        if d is not None and start <= d <= end
    ]


def _provider_cache_identity(provider: DataProvider) -> str:
    """provider 身份 = 缓存键的一部分。单一实现在 backtest_disk_cache。

    这里曾复制一份, 并在演化中分叉 —— 那份漏了 path/bundle/cache 三个属性,
    于是同一条 provider 链在两处算出不同身份。缓存身份漏字段不会报错, 只会让缓存
    **该失效时不失效**, 数据修完再跑回测原样复现修复前的数字。
    """
    from .backtest_disk_cache import _provider_identity
    return _provider_identity(provider)


def _batch_price_with_snapshot_cache(
    provider: DataProvider,
    codes: list[str],
    *,
    snapshot_cache: dict[Any, list[dict[str, Any]]] | None,
    stats: Counter,
    **kwargs,
) -> list[dict[str, Any]]:
    if not codes:
        return []
    key = None
    if snapshot_cache is not None:
        key = _pricing_snapshot_key(provider, codes, kwargs)
        if key in snapshot_cache:
            stats["pricing_snapshot_hits"] += 1
            return [_copy_pricing_row(row) for row in snapshot_cache[key]]
    stats["pricing_snapshot_misses"] += 1
    rows = batch_price_from_provider_threaded(provider, codes, **kwargs)
    if snapshot_cache is not None and key is not None:
        if len(snapshot_cache) > 256:
            snapshot_cache.pop(next(iter(snapshot_cache)))
        snapshot_cache[key] = [_copy_pricing_row(row) for row in rows]
    return rows


def _pricing_snapshot_key(
    provider: DataProvider,
    codes: list[str],
    kwargs: dict[str, Any],
) -> tuple:
    relevant = {
        key: value
        for key, value in kwargs.items()
        if key not in {"progress_cb"}
    }
    return (
        _provider_cache_identity(provider),
        tuple(codes),
        tuple(sorted((key, _hashable_value(value)) for key, value in relevant.items())),
    )


def _hashable_value(value: Any):
    if isinstance(value, (str, int, float, bool, type(None), date)):
        return value
    if isinstance(value, (list, tuple)):
        return tuple(_hashable_value(v) for v in value)
    if isinstance(value, dict):
        return tuple(sorted((str(k), _hashable_value(v)) for k, v in value.items()))
    return repr(value)


def _copy_pricing_row(row: dict[str, Any]) -> dict[str, Any]:
    copied = dict(row)
    if isinstance(copied.get("risk_tags"), list):
        copied["risk_tags"] = list(copied["risk_tags"])
    return copied


def _check_cancel(cancel_cb) -> None:
    if cancel_cb is not None:
        cancel_cb()


def _emit_stage_progress(
    stage_cb,
    stage: str,
    done: int,
    total: int,
    period_index: int,
    total_periods: int,
) -> None:
    if stage_cb is not None:
        stage_cb(stage, done, total, period_index, total_periods)


def _should_emit_code_progress(done: int, total: int) -> bool:
    return done <= 1 or done == total or done % 10 == 0


def _looks_like_transport_failure(reason: Any) -> bool:
    text = str(reason)
    markers = (
        "SkyClient request failed",
        "ErrorCode=-40521007",
        "SendMessage returned null response",
        "GetConnectStatus: 0",
        "Wind 连接失败",
        "未安装 WindPy",
    )
    return any(marker in text for marker in markers)


def _raise_if_source_transport_outage(
    excluded: list[tuple[str, str]],
    *,
    total_count: int,
    period_start: date,
    phase: str,
) -> None:
    if total_count <= 0:
        return
    failures = [
        (code, reason)
        for code, reason in excluded
        if str(reason).startswith("条款获取失败") and _looks_like_transport_failure(reason)
    ]
    if len(failures) < _MIN_OUTAGE_FAILURES:
        return
    fail_ratio = len(failures) / total_count
    if fail_ratio < _SOURCE_OUTAGE_FAIL_RATIO:
        # 部分券取数失败 (限流 / 个别券数据缺失), 但多数成功 → Wind 连接正常。
        # 跳过失败券, 用成功券继续回测, 不中止。
        logger.warning(
            "%s在 %s 有 %d/%d 只债 Wind 取数失败 (已退避重试), 本期跳过这些债; "
            "成功率 %.0f%%, 判定为部分失败而非系统性故障, 继续回测。",
            phase, period_start, len(failures), total_count, (1 - fail_ratio) * 100,
        )
        return
    sample = ", ".join(str(code) for code, _reason in failures[:5])
    first_reason = failures[0][1]
    raise RuntimeError(
        f"{phase}在 {period_start} 出现系统性 Wind 条款获取失败 "
        f"({len(failures)}/{total_count}, 失败率 {fail_ratio*100:.0f}%); 样例 {sample}; "
        f"首个错误: {first_reason}. 已中止回测, 避免生成全现金无效结果。"
        "请确认 Wind API 已登录且连接稳定后重试, 或改用标准历史口径/小代码池。"
    )


def _raise_if_pricing_transport_outage(
    rows: list[dict[str, Any]],
    *,
    total_count: int,
    period_start: date,
) -> None:
    if total_count <= 0:
        return
    failures = [
        row for row in rows
        if row.get("status") != "ok" and _looks_like_transport_failure(
            row.get("error") or row.get("message") or row)
    ]
    if len(failures) < _MIN_OUTAGE_FAILURES:
        return
    fail_ratio = len(failures) / total_count
    if fail_ratio < _SOURCE_OUTAGE_FAIL_RATIO:
        logger.warning(
            "定价阶段在 %s 有 %d/%d 只债 Wind 数据失败 (已退避重试), 本期跳过这些债; "
            "成功率 %.0f%%, 判定为部分失败而非系统性故障, 继续回测。",
            period_start, len(failures), total_count, (1 - fail_ratio) * 100,
        )
        return
    sample = ", ".join(str(row.get("bond_code")) for row in failures[:5])
    first_error = failures[0].get("error") or failures[0].get("message") or failures[0]
    raise RuntimeError(
        f"定价阶段在 {period_start} 出现系统性 Wind 数据失败 "
        f"({len(failures)}/{total_count}, 失败率 {fail_ratio*100:.0f}%); 样例 {sample}; "
        f"首个错误: {first_error}. 已中止回测, 避免生成全现金无效结果。"
        "请确认 Wind API 已登录且连接稳定后重试, 或改用标准历史口径/小代码池。"
    )


def _pre_filter_codes_by_price(
    provider: DataProvider,
    codes: list[str],
    valuation_date: date,
    cfg: ScoreStrategyConfig,
    *,
    progress_cb=None,
    cancel_cb=None,
) -> tuple[list[str], list[tuple[str, str]], list[str]]:
    """返回 ``(进入定价的, 剔除及原因, **仅因价格带被剔除的**)``。

    第三个返回值是给**基准**用的: 价格带是 ``ScoreStrategyConfig`` 的策略阈值, 而基准
    的口径是"市场代理", 它自己的 docstring 写着"刻意不过策略的筛子"。缺价那一档不同 ——
    没有成交价的债基准也买不进去, 那是数据事实不是策略选择, 所以两种剔除必须分开,
    而不是让调用方去解析理由串。
    """
    if not cfg.pre_filter_prices or (cfg.min_market_price is None and cfg.max_market_price is None):
        if progress_cb is not None:
            progress_cb(len(codes), len(codes))
        return codes, [], []
    kept: list[str] = []
    excluded: list[tuple[str, str]] = []
    band_excluded: list[str] = []
    total = len(codes)
    for done, code in enumerate(codes, start=1):
        _check_cancel(cancel_cb)
        try:
            point = _latest_bond_price_point(
                provider,
                code,
                valuation_date,
                lookback_days=cfg.price_lookback_days,
                max_staleness_days=cfg.max_price_staleness_days,
            )
            if point is None:
                excluded.append((code, "价格预筛: 缺少有效转债收盘价"))
                continue
            if not _passes_range(point.price, cfg.min_market_price, cfg.max_market_price):
                excluded.append((code, f"价格预筛: {point.price:.2f} 不在区间内"))
                band_excluded.append(code)
                continue
            kept.append(code)
        finally:
            if progress_cb is not None and _should_emit_code_progress(done, total):
                progress_cb(done, total)
    return kept, excluded, band_excluded


def _eligible_codes_for_date(
    provider: DataProvider,
    bond_codes: list[str],
    on_date: date,
    *,
    terms_cache=None,
    admission_config: AdmissionFilterConfig | None = None,
    progress_cb=None,
    cancel_cb=None,
) -> tuple[list[str], list[tuple[str, str]], list[dict[str, Any]]]:
    eligible: list[str] = []
    excluded: list[tuple[str, str]] = []
    source_diagnostics: list[dict[str, Any]] = []
    total = len(bond_codes)
    for done, code in enumerate(bond_codes, start=1):
        _check_cancel(cancel_cb)
        try:
            terms = _terms_from_cache(terms_cache, code)
            if terms is None:
                try:
                    terms = provider.get_bond_terms(code, on_date)
                except Exception as exc:
                    excluded.append((code, f"条款获取失败: {exc}"))
                    continue
            # 防前视: 回测日期早于上市日 → 该转债还不能买
            # 注意用 listing_date 而不是 issue_date: issue_date 是起息日,
            # 比上市日早 2~4 周 (中位 25 天), 单用它会把还没挂牌的新债放进池子。
            listed_dt = (
                (getattr(terms, 'listing_date', None) or getattr(terms, 'issue_date', None))
                if terms is not None else None
            )
            if listed_dt is not None and listed_dt > on_date:
                excluded.append((code, f"尚未上市 (上市日 {listed_dt})"))
                continue
            # 上市日**根本还没有**的那一档 (已发行未上市): 准入层 2026-08-31 起放行它们
            # —— 实盘页要提前看见新债、提前算理论价。但**回测路径的问题不同**: 那天它还
            # 没挂牌, 买不到就是买不到, 放进来只会去拉一段不存在的历史行情。
            # 所以这道闸留在回测这一侧, 而不是靠准入层顺手挡着。
            if is_issued_pending_listing(code, terms, on_date):
                excluded.append((code, "已发行未上市"))
                continue
            # 到期检查: strip_current_status_fields 不清 maturity_date, 此处冗余但安全
            maturity_dt = getattr(terms, 'maturity_date', None) if terms is not None else None
            if maturity_dt is not None and maturity_dt <= on_date:
                excluded.append((code, f"已到期 (到期日 {maturity_dt})"))
                continue
            source_diagnostics.append(_terms_source_diagnostic(provider, code, on_date))
            reason = batch_pricing_exclusion_reason(
                code,
                terms,
                on_date=on_date,
                admission_config=admission_config,
            )
            if reason is None:
                eligible.append(code)
            else:
                excluded.append((code, reason))
        finally:
            if progress_cb is not None and _should_emit_code_progress(done, total):
                progress_cb(done, total)
    return eligible, excluded, source_diagnostics


def _terms_source_diagnostic(
    provider: DataProvider,
    bond_code: str,
    valuation_date: date,
) -> dict[str, Any]:
    describe = getattr(provider, "get_terms_source_diagnostics", None)
    if callable(describe):
        try:
            diag = describe(bond_code, valuation_date)
            if isinstance(diag, dict):
                return diag
        except Exception:
            logger.debug("get_terms_source_diagnostics(%s) 失败, 回落默认诊断",
                         bond_code, exc_info=True)
    return {
        "bond_code": bond_code,
        "valuation_date": valuation_date,
        "terms_source": "provider",
        "snapshot_date": None,
        "patch_count": 0,
        "event_count": 0,
        "uses_current_fallback": False,
    }


def _period_data_quality(rows: list[dict[str, Any]]) -> dict[str, Any]:
    source_counts = Counter(str(row.get("terms_source") or "unknown") for row in rows)
    fallback_count = sum(1 for row in rows if row.get("uses_current_fallback"))
    patch_count = sum(1 for row in rows if int(row.get("patch_count") or 0) > 0)
    event_count = sum(1 for row in rows if int(row.get("event_count") or 0) > 0)
    total = len(rows)
    # 快照陈旧度: 各转债 (valuation_date - snapshot_date) 的最大天数
    staleness_days: list[int] = []
    without_snapshot = 0
    for row in rows:
        if row.get("uses_current_fallback"):
            without_snapshot += 1
        snap = row.get("snapshot_date")
        val = row.get("valuation_date")
        if isinstance(snap, date) and isinstance(val, date):
            staleness_days.append((val - snap).days)
    return {
        "sample_count": total,
        "source_counts": dict(source_counts),
        "current_fallback_count": fallback_count,
        "current_fallback_ratio": fallback_count / total if total else 0.0,
        "patch_applied_count": patch_count,
        "event_applied_count": event_count,
        "max_snapshot_staleness_days": max(staleness_days) if staleness_days else None,
        "bonds_without_snapshot_count": without_snapshot,
    }


def _terms_from_cache(terms_cache, code: str):
    if terms_cache is None or not hasattr(terms_cache, "get"):
        return None
    try:
        return terms_cache.get(code)
    except Exception:
        return None


def _dynamic_pool_for_date(
    provider: DataProvider,
    base_codes: list[str],
    on_date: date,
    *,
    terms_cache=None,
) -> list[str]:
    """动态标的池: 只保留估值日已上市且未到期的转债.

    优先使用 provider.list_tradable_cbs(on_date), 取与 base_codes 的交集;
    若 provider 不支持, 则根据 listing_date/maturity_date 过滤 base_codes.
    """
    try:
        tradable = provider.list_tradable_cbs(on_date)
        if tradable:
            # list_tradable_cbs 返回 [(wind_code, sec_name), ...]; 仅取代码做交集。
            # 兼容个别 provider 直接返回代码字符串的情况。
            tradable_set = {
                str(entry[0] if isinstance(entry, (tuple, list)) else entry)
                for entry in tradable
            }
            return [code for code in base_codes if code in tradable_set]
    except Exception:  # provider 不支持 list_tradable_cbs 或调用失败 → 走下方 issue/maturity 兜底
        pass
    # Fallback: filter by issue_date/maturity_date
    filtered: list[str] = []
    for code in base_codes:
        terms = _terms_from_cache(terms_cache, code)
        if terms is None:
            try:
                terms = provider.get_bond_terms(code, on_date)
            except Exception:
                filtered.append(code)  # 无法获取条款, 保守保留
                continue
        # 同上: 以上市日为准, 缺失才退回起息日
        listed_dt = getattr(terms, 'listing_date', None) or getattr(terms, 'issue_date', None)
        maturity_dt = getattr(terms, 'maturity_date', None)
        if listed_dt is not None and listed_dt > on_date:
            continue  # 尚未上市
        if maturity_dt is not None and maturity_dt <= on_date:
            continue  # 已到期
        filtered.append(code)
    return filtered


def _select_candidate_rows(rows: list[dict[str, Any]], cfg: ScoreStrategyConfig) -> list[dict[str, Any]]:
    """真正选债的那条路。判据**只此一处** —— 直接问 :func:`_candidate_filter_reason`。

    它此前是 ``_candidate_filter_reason`` 的一份完整副本 (逐条重写了 status / 视图 /
    置信度 / 标签 / 价格·溢价·偏差·σ 区间), 而那个函数只被 ``_rejection_explanation_rows``
    消费、产出落选解释 CSV。两份实现在 2026-08-31 的标签→阈值重构里**当场分叉**: 新加的
    8 条阈值只接进了解释那一份, 而这一份的标签排除集刚被改成空 —— 净效果是默认口径下
    策略层的风险筛选整体消失 (实测候选 116 → 263, CLI/GUI 实际配置下 283/284),
    低评级与 ST 正股的债一路进到持仓, 而等价性用例测的正是没接线的那一半, 全绿。
    这与 AGENTS 记的「视图归属的单一事实源是 ``view_exclusion_reason``, 两边曾各自
    实现一份并在重构后悄悄分叉」是同一个形状。

    合并之后"选债"与"为什么落选"按定义一致: 落选解释里写的理由, 就是这里拦下它的理由。
    """
    view = cfg.selection_view if cfg.selection_view in BATCH_REVIEW_VIEWS else "综合机会"
    ranked = filter_batch_results_by_view(rows, view)
    return [row for row in ranked if _candidate_filter_reason(row, cfg) is None]


def _candidate_explanation_rows(
    candidates: list[dict[str, Any]],
    selected_codes: list[str],
    cfg: ScoreStrategyConfig,
    *,
    rank_signal: str = "score",
    limit: int = 60,
) -> list[dict[str, Any]]:
    selected_set = set(selected_codes)
    rows: list[dict[str, Any]] = []
    for rank, row in enumerate(candidates[:limit], start=1):
        code = str(row.get("bond_code") or "")
        selected = code in selected_set
        rows.append({
            "rank": rank,
            "bond_code": code,
            "bond_name": row.get("bond_name"),
            "selected": selected,
            "selection_reason": _candidate_selection_reason(row, rank, cfg, selected),
            "rank_signal": rank_signal,
            "rank_value": _rank_signal_value(row, rank_signal),
            "market_price": finite_float(row.get("market_price")),
            "theoretical_price": finite_float(row.get("theoretical_price")),
            "deviation": finite_float(row.get("deviation")),
            "conversion_premium": finite_float(row.get("conversion_premium")),
            "sigma": finite_float(row.get("sigma")),
            "confidence": row.get("confidence"),
            "risk_tags": list(row.get("risk_tags") or []),
            "model_signal_status": row.get("model_signal_status"),
            "effective_p_down_1y_prob": finite_float(
                row.get("effective_p_down_1y_prob")
            ),
        })
    return rows


def _candidate_selection_reason(
    row: dict[str, Any],
    rank: int,
    cfg: ScoreStrategyConfig,
    selected: bool,
) -> str:
    rank_signal = _normalize_rank_signal(cfg.rank_signal)
    deviation = finite_float(row.get("deviation"))
    premium = finite_float(row.get("conversion_premium"))
    tags = [str(tag) for tag in row.get("risk_tags") or []]
    parts = []
    if rank_signal == "deviation":
        if deviation is not None:
            parts.append(f"PDE偏差 {deviation * 100:+.1f}%")
    if deviation is not None and rank_signal != "deviation":
        parts.append(f"偏差 {deviation * 100:+.1f}%")
    if premium is not None:
        parts.append(f"溢价 {premium * 100:+.1f}%")
    if tags:
        parts.append("标签 " + "/".join(tags[:3]))
    prefix = "选中" if selected else f"落选: 排名 {rank} 超过 Top{cfg.top_n}"
    return f"{prefix}; " + " · ".join(parts) if parts else prefix


def _rejection_explanation_rows(
    priced_rows: list[dict[str, Any]],
    excluded: list[tuple[str, str]],
    cfg: ScoreStrategyConfig,
    *,
    candidate_codes: set[str],
    limit: int = 120,
) -> list[dict[str, Any]]:
    """落选解释, 按信息量倒序填预算。

    两段共用一个 ``limit``: 「筛选」段 (过了准入、定过价、却没进候选) 是**唯一**能回答
    "信号为什么是空的"的一段; 「准入/预筛」段几乎全是已退市/已到期这类结构性死券, 每期
    几乎不变、看一次就够。曾经先填准入段并在填满时直接 return, 于是死券把预算整个吃光 ——
    实测全库 615 只里死券就有约 100 只, 每期稳稳撞满 120, 导出的 CSV 里**一条「筛选」都没有**。
    而"没有落选解释"和"根本没有落选"长得一模一样: 排查"策略为什么 100% 现金"时,
    唯一能用的那段证据恰好是被截掉的那段。所以先填「筛选」, 准入段拿剩下的。
    """
    rank_signal = _normalize_rank_signal(cfg.rank_signal)
    excluded_codes = {str(code) for code, _ in excluded}
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    for row in filter_batch_results_by_view(priced_rows, "综合机会"):
        code = str(row.get("bond_code") or "")
        if not code or code in seen or code in candidate_codes:
            continue
        if code in excluded_codes:
            continue
        reason = _candidate_filter_reason(row, cfg)
        if reason is None:
            continue
        rows.append({
            "bond_code": code,
            "bond_name": row.get("bond_name"),
            "source": "筛选",
            "reason": reason,
            "rank_signal": rank_signal,
            "rank_value": _rank_signal_value(row, rank_signal),
            "market_price": finite_float(row.get("market_price")),
            "deviation": finite_float(row.get("deviation")),
            "conversion_premium": finite_float(row.get("conversion_premium")),
            "confidence": row.get("confidence"),
            "risk_tags": list(row.get("risk_tags") or []),
            "effective_p_down_1y_prob": finite_float(
                row.get("effective_p_down_1y_prob")
            ),
        })
        seen.add(code)
        if len(rows) >= limit:
            break

    for code, reason in excluded:
        if len(rows) >= limit:
            break
        code = str(code)
        if code in seen:
            continue
        rows.append({
            "bond_code": code,
            "bond_name": "",
            "source": "准入/预筛",
            "reason": str(reason),
            "rank_signal": rank_signal,
            "rank_value": None,
            "market_price": None,
            "deviation": None,
            "conversion_premium": None,
            "confidence": "",
            "risk_tags": [],
        })
        seen.add(code)
    return rows


def _candidate_filter_reason(row: dict[str, Any], cfg: ScoreStrategyConfig) -> str | None:
    if row.get("status") != "ok":
        return str(row.get("error") or row.get("message") or "定价失败")

    tags = set(str(tag) for tag in row.get("risk_tags") or [])
    rank_signal = _normalize_rank_signal(cfg.rank_signal)
    view = cfg.selection_view if cfg.selection_view in BATCH_REVIEW_VIEWS else "综合机会"
    # 视图归属只有一个事实源 (batch_pricing.view_exclusion_reason)。这里曾复制一份实现,
    # 在标签体系重构后与视图口径悄悄分叉 —— 视图已改读维度拦截集, 这里还在读 HARD_REVIEW_TAGS。
    view_reason = view_exclusion_reason(row, view)
    if view_reason is not None:
        return f"{view}视图: {view_reason}"

    if rank_signal == "deviation":
        if finite_float(row.get("deviation")) is None:
            return "缺少PDE估值偏差"
    if cfg.min_confidence and row.get("confidence") not in cfg.min_confidence:
        return f"置信度 {row.get('confidence') or '—'} 不在允许范围"
    excluded_tags = set(cfg.exclude_risk_tags or ())
    hard = excluded_tags & tags
    if hard:
        return "命中排除标签 " + "/".join(sorted(hard))

    # ── 有效性守卫: 不可配置 ────────────────────────────────────
    # 这三档不是"风险大小"而是"这一行没法用来下单/排序", 所以不给阈值:
    # 没有转股价值就算不出模型溢价、没有市价就买不了、理论价非正说明定价坏了。
    # 它们对应旧排除集里的「数据缺口」「无市价」「理论价异常」—— 恰好是那三个
    # **缺值也拦**的标签, 与下面那组"缺值放行"的阈值是两回事。
    market_price = finite_float(row.get("market_price"))
    if market_price is None or market_price <= 0:
        return "缺少有效市价"
    if finite_float(row.get("theoretical_price")) is None or (
            finite_float(row.get("theoretical_price")) or 0.0) <= 0:
        return "理论价异常"
    if finite_float(row.get("parity")) is None:
        return "缺少转股价值"

    # ── 取代标签的显式阈值: 缺值一律放行 ─────────────────────────
    reason = _threshold_reason(
        "模型溢价", row.get("model_premium_to_parity"),
        max_value=cfg.max_model_premium, pct=True)
    if reason:
        return reason
    # ``max_inclusive``: 被取代的「模型高估离群」判据是 ``gap >= DEVIATION_ANOMALY_THRESHOLD``,
    # 六条阈值里只有这一条是闭区间。
    #
    # **锚不是全市场中位时这条闸不适用**。``relative_deviation`` 的定义是"比当期全市场
    # 中位贵多少", 而 ``median_deviation_of`` 在样本 < 30 时返回 None,
    # ``annotate_batch_result`` 于是回落 ``anchor = 0.0`` 并把 ``relative_deviation``
    # 写成 ``deviation`` 本身、``cross_section_origin`` 标成 ``absolute_fallback``。
    # 此时继续套用这个上限, 判据就从**横截面**悄悄变成了**绝对**偏差阈值 —— 度量换了,
    # 名字没换。实测在池子大小 29 → 30 上有一道悬崖: 同一批"人人 deviation=+25%"的债
    # (即没有谁相对市场贵), 池子 29 只时候选 0、理由写「相对偏差 25.00% 不低于上限
    # 20.00%」, 池子 30 只时全部入选、相对偏差 0.00%。回测里每期的可定价池大小是变的,
    # 于是同一只债的去留取决于那一期恰好有多少债定价成功。
    #
    # 处置与 ``_threshold_reason`` 的既有契约一致: **缺值放行**。假锚下这个量根本不存在,
    # 按"没有这个值"处理, 而不是拿一个换了含义的数去卡。
    if _anchor_is_market_wide(row):
        reason = _threshold_reason(
            "相对偏差", row.get("relative_deviation"),
            max_value=cfg.max_relative_deviation, pct=True, max_inclusive=True)
        if reason:
            return reason
    reason = _threshold_reason(
        "剩余年限", row.get("T"), min_value=cfg.min_years_to_maturity)
    if reason:
        return reason
    reason = _threshold_reason(
        "余额(亿)", row.get("outstanding_balance"),
        min_value=cfg.min_outstanding_balance)
    if reason:
        return reason
    rating = row.get("credit_rating")
    if cfg.min_credit_rating and rating and _rating_below(rating, cfg.min_credit_rating):
        return f"评级 {rating} 低于 {cfg.min_credit_rating}"
    if cfg.exclude_underlying_st and _underlying_has_st_risk(row):
        return "正股 ST/退市风险"
    if cfg.exclude_underlying_limit_down and _underlying_at_limit_down(
            row, row.get("stock_code")):
        return "正股当日跌停"
    reason = _range_filter_reason("价格", market_price, cfg.min_market_price, cfg.max_market_price)
    if reason:
        return reason
    premium = finite_float(row.get("conversion_premium"))
    reason = _range_filter_reason("溢价", premium, cfg.min_conversion_premium,
                                  cfg.max_conversion_premium, pct=True)
    if reason:
        return reason
    deviation = finite_float(row.get("deviation"))
    reason = _range_filter_reason("偏差", deviation, cfg.min_deviation, cfg.max_deviation, pct=True)
    if reason:
        return reason
    # σ 与上面那组阈值同口径: **缺值放行**。它的 max 默认 0.80 正是旧「高HV」判据,
    # 而缺 σ 时旧口径打的是「无HV」—— 不在排除集里, 所以是放行的。
    reason = _threshold_reason("HV", row.get("sigma"),
                               min_value=cfg.min_sigma, max_value=cfg.max_sigma, pct=True)
    if reason:
        return reason
    return None


def _threshold_reason(
    label: str,
    raw: Any,
    *,
    min_value: float | None = None,
    max_value: float | None = None,
    pct: bool = False,
    max_inclusive: bool = False,
) -> str | None:
    """取代标签的单边阈值。**缺值放行** —— 这是与 ``_range_filter_reason`` 的唯一区别。

    被取代的那批标签在缺值时打的是「无HV」「无评级」「无余额」「无偏差」, 而那四个
    都不在旧的排除集里 —— 也就是说旧口径下"这只债没有 σ"是放行的。阈值化必须继承这个
    语义, 否则一次数据源抖动就会把几十只债静默踢出候选, 而那是**行为变更**不是重构。
    真正"缺了就不能用"的三档由 ``_candidate_filter_reason`` 的有效性守卫处理。
    """
    if min_value is None and max_value is None:
        return None
    value = finite_float(raw)
    if value is None:
        return None
    display = value * 100.0 if pct else value
    suffix = "%" if pct else ""
    if min_value is not None and value < min_value:
        bound = min_value * 100.0 if pct else min_value
        return f"{label} {display:.2f}{suffix} 低于下限 {bound:.2f}{suffix}"
    if max_value is not None:
        # 比较必须在 None 判定**之内** —— 提到外面就是 `value > None`, TypeError。
        over = value >= max_value if max_inclusive else value > max_value
        if over:
            bound = max_value * 100.0 if pct else max_value
            edge = "不低于" if max_inclusive else "高于"
            return f"{label} {display:.2f}{suffix} {edge}上限 {bound:.2f}{suffix}"
    return None


def _range_filter_reason(
    label: str,
    value: float | None,
    min_value: float | None,
    max_value: float | None,
    *,
    pct: bool = False,
) -> str | None:
    if min_value is None and max_value is None:
        return None
    if value is None:
        return f"缺少{label}"
    display = value * 100.0 if pct else value
    suffix = "%" if pct else ""
    if min_value is not None and value < min_value:
        threshold = min_value * 100.0 if pct else min_value
        return f"{label} {display:.2f}{suffix} < 下限 {threshold:.2f}{suffix}"
    if max_value is not None and value > max_value:
        threshold = max_value * 100.0 if pct else max_value
        return f"{label} {display:.2f}{suffix} > 上限 {threshold:.2f}{suffix}"
    return None


def _passes_range(value: float | None, min_value: float | None, max_value: float | None) -> bool:
    if min_value is None and max_value is None:
        return True
    if value is None:
        return False
    if min_value is not None and value < min_value:
        return False
    if max_value is not None and value > max_value:
        return False
    return True


def _event_store_from_provider(provider: DataProvider):
    """沿 provider 装饰链寻找点时公告事件表; 无事件能力时返回 None."""
    current = provider
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        store = getattr(current, "event_store", None)
        if callable(getattr(store, "list_events", None)):
            return store
        current = getattr(current, "inner", None)
    return None


def _first_down_reset_exit_event(
    event_store,
    bond_code: str,
    *,
    after_date: date,
    before_date: date,
):
    """返回持仓期间首个会使下修 thesis 落地或失效的公告事件."""
    if event_store is None or after_date >= before_date:
        return None
    try:
        events = event_store.list_events(
            bond_code=bond_code,
            through_date=before_date,
        )
    except Exception:
        return None
    matched = []
    for event in events or []:
        event_date = getattr(event, "event_date", None)
        if not isinstance(event_date, date):
            continue
        if (
            getattr(event, "event_type", None) in _DOWN_RESET_EXIT_EVENT_TYPES
            and after_date < event_date < before_date
        ):
            matched.append(event)
    if not matched:
        return None
    return min(
        matched,
        key=lambda event: (
            event.event_date,
            str(getattr(event, "event_type", "")),
            str(getattr(event, "raw_title", "")),
        ),
    )


def _position_returns(
    provider: DataProvider,
    selected: list[dict[str, Any]],
    start_date: date,
    end_date: date,
    *,
    lookback_days: int,
    max_staleness_days: int | None = None,
    execution_timing: str = "signal_close",
    execution_lookahead_days: int = 10,
    price_cache: dict[tuple, PricePoint | None] | None = None,
    event_exit_store=None,
    cash_yield_rate: float = 0.0,
    rank_signal: str = "score",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    positions: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for rank, row in enumerate(selected, start=1):
        code = str(row.get("bond_code"))
        entry_point = _execution_price_point(
            provider, code, start_date,
            timing=execution_timing,
            side="entry",
            lookback_days=lookback_days,
            max_staleness_days=max_staleness_days,
            lookahead_days=execution_lookahead_days,
            cache=price_cache,
        )
        regular_exit_point = _execution_price_point(
            provider, code, end_date,
            timing=execution_timing,
            side="exit",
            lookback_days=lookback_days,
            max_staleness_days=max_staleness_days,
            lookahead_days=execution_lookahead_days,
            cache=price_cache,
        )
        exit_point = regular_exit_point
        exit_event = None
        exit_signal_date = end_date
        if entry_point is not None and event_exit_store is not None:
            candidate_event = _first_down_reset_exit_event(
                event_exit_store,
                code,
                after_date=entry_point.date,
                before_date=end_date,
            )
            if candidate_event is not None:
                event_exit_point = _execution_price_point(
                    provider,
                    code,
                    candidate_event.event_date,
                    # 公告通常在收盘后披露; 即便普通回测选 signal_close, 事件退出也必须
                    # 用公告后的下一可得收盘, 不能倒用公告日收盘制造未来函数。
                    timing="next_close",
                    side="exit",
                    lookback_days=lookback_days,
                    max_staleness_days=max_staleness_days,
                    lookahead_days=execution_lookahead_days,
                    cache=price_cache,
                )
                if (
                    event_exit_point is not None
                    and (
                        regular_exit_point is None
                        or event_exit_point.date < regular_exit_point.date
                    )
                ):
                    exit_point = event_exit_point
                    exit_event = candidate_event
                    exit_signal_date = candidate_event.event_date
        if entry_point is not None and exit_point is None:
            # **建了仓就不能当没买过**。此前 entry/exit 缺任何一个都把仓位整条删掉,
            # 而这两种情况的经济含义完全相反:
            #   · 没有期初价 = 根本没成交 → 那个槽位确实是现金 (ScoreStrategyConfig
            #     docstring 写的"缺收盘价无法建仓的标的按现金(0 收益)计入分母"说的是这一档);
            #   · 有期初价、没有期末价 = **买到了, 然后停牌/摘牌/强赎摘牌/到了最后交易日**。
            #     把它删掉等于用建仓之后才知道的信息决定"这笔成交算不算发生过", 而且
            #     它的已实现盈亏被整个抹平。
            # 实测同一只债 95→45 暴跌: 照常成交到期末时区间收益 −17.54%, 期末停牌时
            # 变成 +0.06% —— 同一段经济事实, 一个月差 17.6pp; 基准同时从 −8.77% 变成
            # 0.00% 且成分静默 6→5。而状态栏那句"N 个入选仓位因现金替代"是在替这个
            # 错误经济学作证。
            # 处置: 用**期末之前最后一个可得收盘价**平出 (不设陈旧上限 —— 停牌债的最后
            # 一口价就是它当时唯一能被记账的价), 并单列 exit_reason 让它在明细里认得出来。
            fallback_exit = _latest_bond_price_point(
                provider, code, end_date,
                lookback_days=max(lookback_days, 365), max_staleness_days=None)
            if fallback_exit is not None and fallback_exit.date >= entry_point.date:
                exit_point = fallback_exit
                exit_reason_override = "no_exit_price"
            else:
                exit_reason_override = None
        else:
            exit_reason_override = None

        if entry_point is None or exit_point is None:
            skipped.append({
                "rank": rank,
                "bond_code": code,
                "bond_name": row.get("bond_name"),
                "reason": _missing_execution_reason(
                    entry_point, exit_point, execution_timing),
                "entry_date": entry_point.date if entry_point else None,
                "exit_date": exit_point.date if exit_point else None,
                "start_price": entry_point.price if entry_point else None,
                "end_price": exit_point.price if exit_point else None,
                "rank_signal": rank_signal,
                "rank_value": _rank_signal_value(row, rank_signal),
            })
            continue
        price_ratio = exit_point.price / entry_point.price
        price_return = price_ratio - 1.0
        event_cash_days = (
            max(0, (end_date - exit_point.date).days)
            if exit_event is not None else 0
        )
        post_exit_cash_return = (
            price_ratio * max(0.0, float(cash_yield_rate)) * event_cash_days / 365.0
        )
        ret = price_return + post_exit_cash_return
        positions.append({
            "rank": rank,
            "bond_code": code,
            "bond_name": row.get("bond_name"),
            "rank_signal": rank_signal,
            "rank_value": _rank_signal_value(row, rank_signal),
            "signal_market_price": finite_float(row.get("market_price")),
            "theoretical_price": finite_float(row.get("theoretical_price")),
            "deviation": finite_float(row.get("deviation")),
            "effective_p_down_1y_prob": finite_float(
                row.get("effective_p_down_1y_prob")
            ),
            "confidence": row.get("confidence"),
            "risk_tags": list(row.get("risk_tags") or []),
            "entry_date": entry_point.date,
            "exit_date": exit_point.date,
            "start_price": entry_point.price,
            "end_price": exit_point.price,
            "price_return": price_return,
            "post_exit_cash_return": post_exit_cash_return,
            "period_return": ret,
            "exit_reason": (
                "down_reset_event" if exit_event is not None
                else (exit_reason_override or "rebalance")),
            "exit_signal_date": exit_signal_date,
            "exit_event_type": getattr(exit_event, "event_type", None),
            "exit_event_title": getattr(exit_event, "raw_title", None),
            "event_exit_cash_days": event_cash_days,
        })
    return positions, skipped


def _benchmark_period_return(
    provider: DataProvider,
    priced_rows: list[dict[str, Any]],
    start_date: date,
    end_date: date,
    *,
    lookback_days: int,
    max_staleness_days: int | None = None,
    execution_timing: str = "signal_close",
    execution_lookahead_days: int = 10,
    price_cache: dict[tuple, PricePoint | None] | None = None,
) -> tuple[float | None, list[str]]:
    """等权买下全部"通过准入且已定价"标的的区间收益, 作为筛选池基准.

    成交价采用与策略相同的执行时点设置, 避免基准和策略出现不同的未来函数口径。
    返回 (等权区间收益, 实际可成交的成分码列表); 成分码供调用方计算基准自身的
    成员变动换手并计成本 (与策略同口径)。

    ⚠️ **基准刻意不过策略的筛子** —— 唯一的闸是 ``status == "ok"``, 不看标签也不看
    ``ScoreStrategyConfig`` 的任何阈值。这是**基准 = 市场代理**的口径: 准入层已经收窄成
    "买不买得到", 所以"全部通过准入且已定价"就是当期**可投资全域**, 而超额收益要回答的
    正是"挑出来的这些, 比无脑等权买下全市场好多少"。反过来让基准也过一遍策略阈值, 那
    衡量的就只剩"在同一批候选里排序排得好不好", 而且会把"避开了低评级/ST"这类真实贡献
    从超额里抹掉。

    **代价必须认**: 基准因此持有 ST 与低评级债 (2026-08-31 起它们不再被准入层剔除),
    而策略默认用 ``exclude_underlying_st`` / ``min_credit_rating`` 避开它们 —— 这两只债
    真的暴雷时, 超额会**单边**变好看。这在口径上是诚实的 (市场里确实有这些债), 前提是
    策略的规避不能用未来信息 —— 那正是 ``strip_current_status_fields`` 必须连
    ``underlying_name`` 一起剥的原因 (cb_data 里存的是**今天**的正股名, 留着它等于让
    2022 年的回测知道 2026 年谁会被 ST)。两件事是一体的, 改任何一边都要重看另一边。
    """
    returns: list[float] = []
    codes: list[str] = []
    for row in priced_rows:
        if row.get("status") != "ok":
            continue
        code = str(row.get("bond_code"))
        entry_point = _execution_price_point(
            provider, code, start_date,
            timing=execution_timing,
            side="entry",
            lookback_days=lookback_days,
            max_staleness_days=max_staleness_days,
            lookahead_days=execution_lookahead_days,
            cache=price_cache,
        )
        exit_point = _execution_price_point(
            provider, code, end_date,
            timing=execution_timing,
            side="exit",
            lookback_days=lookback_days,
            max_staleness_days=max_staleness_days,
            lookahead_days=execution_lookahead_days,
            cache=price_cache,
        )
        if entry_point is not None and exit_point is None:
            # 与持仓侧同一条规则: 建了仓 (有期初价) 就不能因为期末停牌而把成分删掉。
            # 此前这里连诊断都没有 —— 基准成分静默从 6 变成 5, excess_return 跟着错,
            # 而 CSV 里一行痕迹都不留。
            fallback = _latest_bond_price_point(
                provider, code, end_date,
                lookback_days=max(lookback_days, 365), max_staleness_days=None)
            if fallback is not None and fallback.date >= entry_point.date:
                exit_point = fallback
        if entry_point is None or exit_point is None:
            continue
        returns.append(exit_point.price / entry_point.price - 1.0)
        codes.append(code)
    if not returns:
        return None, []
    return float(sum(returns) / len(returns)), codes


def _index_benchmark_curve(
    provider: DataProvider,
    index_code: str,
    schedule: list[date],
    cfg: ScoreStrategyConfig,
) -> list[dict[str, Any]]:
    """真实指数 (如中证转债 000832.CSI) 的归一化净值曲线, 用作第二基准.

    用与策略相同的成交时点在每个调仓边界取指数收盘, 归一到首日=1.0。数据源取不到
    (akshare/CSV 无该指数, 或全程缺价) 时返回 [], 调用方据此跳过指数基准。
    """
    price_cache: dict[tuple, PricePoint | None] = {}
    points: list[dict[str, Any]] = []
    base: float | None = None
    for d in schedule:
        try:
            p = _execution_price_point(
                provider, index_code, d,
                timing=cfg.execution_timing, side="entry",
                lookback_days=cfg.price_lookback_days,
                max_staleness_days=cfg.max_price_staleness_days,
                lookahead_days=cfg.execution_lookahead_days,
                cache=price_cache,
            )
        except Exception:
            p = None
        if p is None or not p.price or p.price <= 0:
            continue
        if base is None:
            base = float(p.price)
        points.append({"date": d, "equity": float(p.price) / base})
    return points if len(points) >= 2 else []


def _normalize_execution_timing(value: str | None) -> str:
    raw = (value or "signal_close").strip().lower()
    aliases = {
        "signal": "signal_close",
        "same_close": "signal_close",
        "signal_close": "signal_close",
        "close": "signal_close",
        "当日收盘": "signal_close",
        "next": "next_close",
        "next_close": "next_close",
        "next_day_close": "next_close",
        "下一收盘": "next_close",
        "次日收盘": "next_close",
    }
    if raw not in aliases:
        raise ValueError(f"未知成交时点: {value}")
    return aliases[raw]


def _execution_price_point(
    provider: DataProvider,
    bond_code: str,
    signal_date: date,
    *,
    timing: str,
    side: str,
    lookback_days: int,
    max_staleness_days: int | None,
    lookahead_days: int,
    cache: dict[tuple, PricePoint | None] | None = None,
) -> PricePoint | None:
    timing_key = _normalize_execution_timing(timing)
    max_stale = None if max_staleness_days is None else max(0, int(max_staleness_days))
    if timing_key == "signal_close":
        cache_key = ("latest", bond_code, signal_date, lookback_days, max_stale)
        if cache is not None and cache_key in cache:
            return cache[cache_key]
        point = _latest_bond_price_point(
            provider, bond_code, signal_date,
            lookback_days=lookback_days,
            max_staleness_days=max_stale,
        )
    else:
        cache_key = ("next", bond_code, signal_date, max(1, int(lookahead_days)))
        if cache is not None and cache_key in cache:
            return cache[cache_key]
        point = _next_bond_price_point(
            provider, bond_code, signal_date,
            lookahead_days=max(1, int(lookahead_days)),
        )
    if cache is not None:
        cache[cache_key] = point
    return point


def _missing_execution_reason(
    entry_point: PricePoint | None,
    exit_point: PricePoint | None,
    execution_timing: str,
) -> str:
    timing = _normalize_execution_timing(execution_timing)
    label = "下一可得收盘" if timing == "next_close" else "信号日收盘"
    if entry_point is None and exit_point is None:
        return f"缺少期初/期末{label}成交价或价格过旧"
    if entry_point is None:
        return f"缺少期初{label}成交价或价格过旧"
    return f"缺少期末{label}成交价或价格过旧"


def _portfolio_mark_to_market_curve(
    provider: DataProvider,
    positions: list[dict[str, Any]],
    *,
    start_equity: float,
    period_end: date,
    cost: float,
    intended_count: int,
    exposure: float = 1.0,
    period_start: date | None = None,
    cash_weight: float = 0.0,
    cash_yield_rate: float = 0.0,
) -> list[dict[str, Any]]:
    """根据持仓期内可得收盘价生成组合净值点位.

    等权口径与区间收益保持一致: 未能建仓的标的占用现金权重, 已建仓标的在两个
    可得成交价之间逐日按最新收盘价估值; ``exposure`` 为 D 仓位层的总仓位缩放;
    现金权重按 ``cash_yield_rate`` 自 ``period_start`` 起按日线性计息 (与区间记账一致)。
    """
    def _cash_accrual(on_date: date) -> float:
        if not cash_yield_rate or period_start is None:
            return 0.0
        # **按期末封顶**。曲线的最后一点未必是 period_end: ``next_close`` 口径下平仓价
        # 落在期末之后的第一个可得交易日 (实测越过 1~3 个日历日), 而下一期又从它自己的
        # period_start 重新起算 —— 重叠那几天的现金收益被付了两次。
        # 结果是 ``summary.final_equity`` 不再等于逐期收益的链乘 (实测单期多计 1.62bp,
        # 12 期约 20bp/年), 而这两个数在报告里是并排给出的。
        # 仓位那一腿早就封顶了 (下修事件退出的现金天数用的是 ``min(current_date,
        # period_end)``), 只有现金腿漏了。
        capped = min(on_date, period_end)
        return cash_weight * cash_yield_rate * max(0, (capped - period_start).days) / 365.0

    if intended_count <= 0:
        # ``- cost`` 不能漏: 下面两个兄弟早返回都带着它, 而这一档 (候选池为空 /
        # full_invest 下零成交) 恰恰是**清空整个组合**的那一期, 调仓成本最实在。
        # 漏掉之后 ``period_return`` 照常报了成本而净值曲线没扣 —— 实测 final 1.00275722
        # vs 链乘 1.0007517, 两个数在同一份报告里对不上。
        return [{"date": period_end,
                 "equity": start_equity * (1.0 + _cash_accrual(period_end) - cost)}]
    if not positions:
        return [{"date": period_end,
                 "equity": start_equity * (1.0 + _cash_accrual(period_end) - cost)}]

    price_maps: dict[str, dict[date, float]] = {}
    all_dates: set[date] = set()
    for pos in positions:
        code = str(pos.get("bond_code"))
        entry_date = pos.get("entry_date")
        exit_date = pos.get("exit_date")
        entry_price = finite_float(pos.get("start_price"))
        exit_price = finite_float(pos.get("end_price"))
        if not isinstance(entry_date, date) or not isinstance(exit_date, date):
            continue
        # 建仓价还要 `> 0`: 它是下面 `exit_price / entry_price` 的除数, 而持仓行可以
        # 是从快照读回来的 —— 取价那一侧的守卫管不到这条路。
        if entry_price is not None and entry_price <= 0:
            continue
        if entry_price is None or exit_price is None:
            continue
        start = min(entry_date, exit_date)
        end = max(entry_date, exit_date)
        if pos.get("exit_reason") == "down_reset_event":
            # 退出后虽不再使用债价, 仍借该券交易日序列补齐现金持有期的日频净值点。
            end = max(end, period_end)
        series = _bond_price_map(provider, code, start, end)
        series[entry_date] = entry_price
        series[exit_date] = exit_price
        price_maps[code] = series
        all_dates.update(series)

    if not price_maps:
        return [{"date": period_end,
                 "equity": start_equity * (1.0 + _cash_accrual(period_end) - cost)}]

    all_dates.add(period_end)
    curve: list[dict[str, Any]] = []
    for current_date in sorted(all_dates):
        # **本期的点不许落在本期起点之前**。``all_dates`` 收的是各持仓的价格日期, 而
        # 建仓价可能取到一个陈旧收盘 (``signal_close`` 口径下允许 lookback), 于是这一期
        # 会吐出一个**上一期区间内**的点。``_upsert_equity_points`` 对同日点是覆盖,
        # 于是上一期真实的盘中估值被这一期的开仓值顶掉, 净值曲线上凭空多一段假的横盘。
        if period_start is not None and current_date < period_start:
            continue
        gross_return = 0.0
        for pos in positions:
            code = str(pos.get("bond_code"))
            series = price_maps.get(code)
            if not series:
                continue
            entry_date = pos["entry_date"]
            exit_date = pos["exit_date"]
            entry_price = float(pos["start_price"])
            exit_price = float(pos["end_price"])
            if current_date < entry_date:
                pos_return = 0.0
            elif current_date >= exit_date:
                price_ratio = exit_price / entry_price
                if pos.get("exit_reason") == "down_reset_event":
                    cash_days = max(0, (min(current_date, period_end) - exit_date).days)
                    pos_return = (
                        price_ratio * (1.0 + cash_yield_rate * cash_days / 365.0) - 1.0
                    )
                else:
                    pos_return = price_ratio - 1.0
            else:
                mark = _latest_price_from_map(series, current_date)
                pos_return = (mark / entry_price - 1.0) if mark is not None else 0.0
            gross_return += exposure * pos_return / intended_count
        curve.append({
            "date": current_date,
            "equity": start_equity * (
                1.0 + gross_return + _cash_accrual(current_date) - cost),
        })
    return curve


def _bond_price_map(
    provider: DataProvider,
    bond_code: str,
    start: date,
    end: date,
) -> dict[date, float]:
    try:
        history = provider.get_bond_history(bond_code, start, end)
    except Exception:
        return {}
    prices: dict[date, float] = {}
    for d, value in history or []:
        if d is None or d < start or d > end:
            continue
        px = finite_float(value)
        if px is not None and px > 0:
            prices[d] = px
    return prices


def _latest_price_from_map(series: dict[date, float], on_date: date) -> float | None:
    latest_date = None
    latest_price = None
    for d, px in series.items():
        if d <= on_date and (latest_date is None or d > latest_date):
            latest_date = d
            latest_price = px
    return latest_price


def _curve_periods_per_year(equity_curve: list[dict[str, Any]]) -> float:
    """按净值曲线**实际的观测间距**推年化因子, 不写死 252。

    曲线只在"某只持仓当天有价"的日子上出点 —— 一个空仓期整月只贡献**一个**点
    (见 ``_portfolio_mark_to_market_curve`` 的三条早返回)。于是同一条序列里混着日频与
    月频观测, 而波动率 / 夏普 / 索提诺全按"每个观测都是一个交易日"算, 空仓越多年化
    越离谱。

    用中位间距而不是均值: 长假与停牌会拉出几个很大的间隔, 均值被它们拽偏。
    夹在 [1, 365] 之间并在样本不足时回落 252 —— 纯日频曲线的中位间距是 1 个日历日
    (周末被跳过, 中位仍是 1), 于是 365.25 → 略高于 252 的年化因子; 这比"把月频观测
    当成日频"的偏差小一个量级, 而且随数据自己校准。
    """
    dates = [row.get("date") for row in equity_curve or []
             if isinstance(row.get("date"), date)]
    if len(dates) < 3:
        return 252.0
    gaps = [(b - a).days for a, b in zip(dates, dates[1:]) if (b - a).days > 0]
    if not gaps:
        return 252.0
    gaps.sort()
    median_gap = gaps[len(gaps) // 2]
    if median_gap <= 0:
        return 252.0
    # 日频那一档保持 252 —— 中位间距 1~3 天的曲线本来就是"每个交易日一个点"
    if median_gap <= 3:
        return 252.0
    return max(1.0, min(365.0, 365.25 / median_gap))


def _upsert_equity_points(
    curve: list[dict[str, Any]],
    points: list[dict[str, Any]],
) -> None:
    for point in points:
        point_date = point.get("date")
        if not isinstance(point_date, date):
            continue
        if not curve or point_date > curve[-1]["date"]:
            curve.append(point)
        elif point_date == curve[-1]["date"]:
            curve[-1] = point
        else:
            for i, existing in enumerate(curve):
                if existing["date"] == point_date:
                    curve[i] = point
                    break
                if existing["date"] > point_date:
                    curve.insert(i, point)
                    break


def _min_position_date(positions: list[dict[str, Any]], key: str) -> date | None:
    vals = [p.get(key) for p in positions if isinstance(p.get(key), date)]
    return min(vals) if vals else None


def _max_position_date(positions: list[dict[str, Any]], key: str) -> date | None:
    vals = [p.get(key) for p in positions if isinstance(p.get(key), date)]
    return max(vals) if vals else None


def _latest_bond_price(
    provider: DataProvider,
    bond_code: str,
    on_date: date,
    lookback_days: int,
) -> float | None:
    point = _latest_bond_price_point(
        provider, bond_code, on_date, lookback_days=lookback_days,
        max_staleness_days=None,
    )
    return point.price if point else None


def _latest_bond_price_point(
    provider: DataProvider,
    bond_code: str,
    on_date: date,
    *,
    lookback_days: int,
    max_staleness_days: int | None,
) -> PricePoint | None:
    start = on_date - timedelta(days=max(1, int(lookback_days)))
    try:
        history = provider.get_bond_history(bond_code, start, on_date)
    except Exception:
        return None
    latest_price: float | None = None
    latest_date: date | None = None
    for d, value in history or []:
        if d is None or d > on_date:
            continue
        px = finite_float(value)
        # **``<= 0`` 与 ``None`` 同处置**。这个循环的两个兄弟 (`_bond_price_series`、
        # `_first_bond_price_after`) 都写着 `px is None or px <= 0`, 只有这里漏了 ——
        # 而它的返回值会当**除数**用 (`exit_point.price / entry_point.price`), 于是
        # 一个 0 收盘价不是少一个成分, 是 ZeroDivisionError 掐断整轮回测。行情源给出
        # 0 或负收盘价不是假想: 停牌/退市行附近的脏数据就是这个形状, 而它不该被读成
        # "这只债今天值 0 元"。
        if px is None or px <= 0:
            continue
        if latest_date is None or d >= latest_date:
            latest_date = d
            latest_price = px
    if latest_price is None or latest_date is None:
        return None
    if max_staleness_days is not None and (on_date - latest_date).days > max_staleness_days:
        return None
    return PricePoint(date=latest_date, price=latest_price)


def _next_bond_price_point(
    provider: DataProvider,
    bond_code: str,
    signal_date: date,
    *,
    lookahead_days: int,
) -> PricePoint | None:
    end = signal_date + timedelta(days=max(1, int(lookahead_days)))
    try:
        history = provider.get_bond_history(bond_code, signal_date, end)
    except Exception:
        return None
    best_date: date | None = None
    best_price: float | None = None
    for d, value in history or []:
        if d is None or d <= signal_date:
            continue
        px = finite_float(value)
        if px is None or px <= 0:
            continue
        if best_date is None or d < best_date:
            best_date = d
            best_price = px
    if best_date is None or best_price is None:
        return None
    return PricePoint(date=best_date, price=best_price)


def _summarize_strategy(
    equity_curve: list[dict[str, Any]],
    periods: list[dict[str, Any]],
    *,
    start_date: date,
    end_date: date,
    freq: str,
    top_n: int,
    risk_free_rate: float = 0.0,
    benchmark_curve: list[dict[str, Any]] | None = None,
    index_benchmark_curve: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    final_equity = float(equity_curve[-1]["equity"]) if equity_curve else 1.0
    total_return = final_equity - 1.0
    years = max((end_date - start_date).days / 365.25, 0.0)
    annualized_return = (
        final_equity ** (1.0 / years) - 1.0
        if years > 0 and final_equity > 0
        else None
    )
    returns = [finite_float(row.get("period_return")) for row in periods]
    period_returns = [r for r in returns if r is not None]
    curve_returns = _equity_curve_returns(equity_curve)
    use_curve_returns = len(curve_returns) > max(len(period_returns), 1)
    metric_returns = curve_returns if use_curve_returns else period_returns
    periods_per_year = (
        _curve_periods_per_year(equity_curve) if use_curve_returns
        else _periods_per_year(freq))
    rf_per_period = (risk_free_rate or 0.0) / periods_per_year
    if len(metric_returns) >= 2:
        std = float(np.std(metric_returns, ddof=1))
        annualized_vol = std * math.sqrt(periods_per_year)
        excess_returns = [r - rf_per_period for r in metric_returns]
        sharpe = (
            float(np.mean(excess_returns)) / std * math.sqrt(periods_per_year)
            if std > 0
            else None
        )
        downside = [min(0.0, r - rf_per_period) for r in metric_returns]
        downside_dev = math.sqrt(sum(x * x for x in downside) / len(downside))
        sortino = (
            float(np.mean(excess_returns)) / downside_dev * math.sqrt(periods_per_year)
            if downside_dev > 0
            else None
        )
    else:
        annualized_vol = None
        sharpe = None
        sortino = None
    benchmark_final_equity = None
    benchmark_total_return = None
    excess_return = None
    if benchmark_curve:
        benchmark_final_equity = float(benchmark_curve[-1]["equity"])
        benchmark_total_return = benchmark_final_equity - 1.0
        excess_return = total_return - benchmark_total_return
    index_total_return = None
    excess_vs_index = None
    index_covers_full_window = None
    if index_benchmark_curve:
        index_total_return = float(index_benchmark_curve[-1]["equity"]) - 1.0
        # **两条曲线要覆盖同一段窗口才谈得上超额**。``_index_benchmark_curve`` 在缺价的
        # 调仓日直接 ``continue``, 并把**第一个取得到价的日子**归一成 1.0 —— 数据源前
        # k 个调仓日没有指数价时, 指数收益只覆盖回测的尾段, 而策略收益覆盖全程,
        # 两个不同期限的数相减没有意义。
        index_start = index_benchmark_curve[0].get("date")
        index_covers_full_window = (
            isinstance(index_start, date) and index_start <= start_date)
        if index_covers_full_window:
            excess_vs_index = total_return - index_total_return
    selected_counts = [int(row.get("selected_count") or 0) for row in periods]
    turnovers = [finite_float(row.get("turnover")) for row in periods]
    finite_turnovers = [t for t in turnovers if t is not None]
    cash_weights = [
        finite_float(
            row.get("average_cash_weight")
            if row.get("average_cash_weight") is not None
            else row.get("cash_weight")
        )
        for row in periods
    ]
    finite_cash_weights = [w for w in cash_weights if w is not None]
    end_cash_weights = [finite_float(row.get("end_cash_weight")) for row in periods]
    finite_end_cash_weights = [w for w in end_cash_weights if w is not None]
    total_event_exits = sum(int(row.get("event_exit_count") or 0) for row in periods)
    costs = [finite_float(row.get("cost")) for row in periods]
    finite_costs = [c for c in costs if c is not None]
    dd_stats = _drawdown_stats(equity_curve)
    max_drawdown = dd_stats["max_drawdown"]
    calmar = (
        annualized_return / max_drawdown
        if annualized_return is not None and max_drawdown and max_drawdown > 0
        else None
    )
    stability = _stability_stats(
        metric_returns, period_returns, benchmark_curve,
        periods_per_year=periods_per_year, rf_per_period=rf_per_period)
    return {
        "top_n": top_n,
        "rebalance_freq": (freq or "M").upper(),
        "periods": len(periods),
        "final_equity": final_equity,
        "total_return": total_return,
        "annualized_return": annualized_return,
        "annualized_volatility": annualized_vol,
        "sharpe": sharpe,
        "sortino": sortino,
        "calmar": calmar,
        "volatility_basis": "daily_mtm" if use_curve_returns else "rebalance_period",
        **dd_stats,
        "hit_rate": (
            sum(1 for r in period_returns if r > 0) / len(period_returns)
            if period_returns
            else None
        ),
        "avg_period_return": (
            float(np.mean(period_returns)) if period_returns else None
        ),
        "avg_selected_count": (
            sum(selected_counts) / len(selected_counts) if selected_counts else 0.0
        ),
        "avg_turnover": (
            sum(finite_turnovers) / len(finite_turnovers) if finite_turnovers else None
        ),
        "avg_cash_weight": (
            sum(finite_cash_weights) / len(finite_cash_weights) if finite_cash_weights else None
        ),
        "avg_end_cash_weight": (
            sum(finite_end_cash_weights) / len(finite_end_cash_weights)
            if finite_end_cash_weights else None
        ),
        "total_event_exits": total_event_exits,
        "total_cost": sum(finite_costs) if finite_costs else 0.0,
        "benchmark_final_equity": benchmark_final_equity,
        "benchmark_total_return": benchmark_total_return,
        "excess_return": excess_return,
        "index_benchmark_total_return": index_total_return,
        "excess_vs_index": excess_vs_index,
        # None 表示"指数没覆盖到回测起点, 超额无法计算" —— 与"没有指数基准"分开,
        # 否则页面上两种情况长得一样。
        "index_covers_full_window": index_covers_full_window,
        "stability": stability,
    }


def _stability_stats(
    metric_returns: list[float],
    period_returns: list[float],
    benchmark_curve: list[dict[str, Any]] | None,
    *,
    periods_per_year: float,
    rf_per_period: float,
) -> dict[str, Any]:
    """统计稳健性: Sharpe 块自助 CI、超额块自助/跑赢概率、滚动 Sharpe (1 年窗)。

    Sharpe CI 用与表头同口径的 metric_returns; 超额检验用按期配对的 period_returns
    vs 基准期收益 (二者等长可比)。样本不足时各项优雅返回 None。
    """
    roll = backtest_stats.rolling_sharpe(
        metric_returns, window=int(round(periods_per_year)),
        periods_per_year=periods_per_year, rf_per_period=rf_per_period)
    bench_period_returns = _equity_curve_returns(benchmark_curve) if benchmark_curve else []
    return {
        "sharpe_bootstrap": backtest_stats.block_bootstrap_sharpe(
            metric_returns, periods_per_year=periods_per_year,
            rf_per_period=rf_per_period),
        "excess_bootstrap": (
            backtest_stats.block_bootstrap_excess(period_returns, bench_period_returns)
            if bench_period_returns else None
        ),
        "rolling_sharpe": roll,
        "rolling_summary": backtest_stats.summarize_stability(roll),
    }


def _equity_curve_returns(equity_curve: list[dict[str, Any]]) -> list[float]:
    returns: list[float] = []
    prev_date = None
    prev_equity = None
    for row in sorted(equity_curve, key=lambda x: x.get("date") or date.min):
        current_date = row.get("date")
        equity = finite_float(row.get("equity"))
        if not isinstance(current_date, date) or equity is None or equity <= 0:
            continue
        if prev_date is not None and current_date > prev_date and prev_equity and prev_equity > 0:
            returns.append(equity / prev_equity - 1.0)
        prev_date = current_date
        prev_equity = equity
    return returns


def _compute_patch_coverage(periods: list[dict[str, Any]]) -> dict[str, Any]:
    """聚合各期 patch 覆盖信息, 用于诊断 patch 缺口."""
    all_codes: set[str] = set()
    codes_with_patches: set[str] = set()
    earliest_patch_date: date | None = None
    latest_patch_date: date | None = None
    for period in periods:
        period_start = period.get("start_date")
        dq = period.get("data_quality") or {}
        patch_applied = int(dq.get("patch_applied_count") or 0)
        # 从 excluded_reasons 和 positions 中收集出现过的转债代码
        for code_reason in period.get("excluded_reasons") or []:
            if isinstance(code_reason, (list, tuple)) and len(code_reason) >= 1:
                all_codes.add(str(code_reason[0]))
        for pos in period.get("positions") or []:
            code = str(pos.get("bond_code") or "")
            if code:
                all_codes.add(code)
        for pos in period.get("skipped_positions") or []:
            code = str(pos.get("bond_code") or "")
            if code:
                all_codes.add(code)
        selected = period.get("selected_codes") or []
        for code in selected:
            all_codes.add(str(code))
        if patch_applied > 0 and isinstance(period_start, date):
            if earliest_patch_date is None or period_start < earliest_patch_date:
                earliest_patch_date = period_start
            if latest_patch_date is None or period_start > latest_patch_date:
                latest_patch_date = period_start
            # 记录有 patch 的期中出现过的转债
            for code in selected:
                codes_with_patches.add(str(code))
    bonds_without_patches = sorted(all_codes - codes_with_patches)
    return {
        "earliest_patch_date": earliest_patch_date,
        "latest_patch_date": latest_patch_date,
        "bonds_with_patches": len(codes_with_patches),
        "bonds_without_patches": bonds_without_patches,
    }


def _build_strategy_diagnostics(
    equity_curve: list[dict[str, Any]],
    periods: list[dict[str, Any]],
    summary: dict[str, Any],
) -> dict[str, Any]:
    data_quality = _summarize_data_quality(periods)
    attribution = _strategy_attribution(periods)
    # patch_coverage: 聚合各期 patch 覆盖信息
    patch_coverage = _compute_patch_coverage(periods)
    data_quality["patch_coverage"] = patch_coverage
    diagnostics = {
        "data_quality": data_quality,
        "attribution": attribution,
        "yearly_returns": _calendar_return_table(equity_curve, "Y"),
        "monthly_returns": _calendar_return_table(equity_curve, "M"),
    }
    diagnostics["warnings"] = _strategy_warnings(summary, data_quality, attribution)
    return diagnostics


def _summarize_data_quality(periods: list[dict[str, Any]]) -> dict[str, Any]:
    source_counts: Counter = Counter()
    total = 0
    fallback = 0
    patch_applied = 0
    event_applied = 0
    max_staleness: int | None = None
    total_without_snapshot = 0
    for period in periods:
        dq = period.get("data_quality") or {}
        count = int(dq.get("sample_count") or 0)
        total += count
        fallback += int(dq.get("current_fallback_count") or 0)
        patch_applied += int(dq.get("patch_applied_count") or 0)
        event_applied += int(dq.get("event_applied_count") or 0)
        total_without_snapshot += int(dq.get("bonds_without_snapshot_count") or 0)
        period_staleness = dq.get("max_snapshot_staleness_days")
        if period_staleness is not None:
            if max_staleness is None or int(period_staleness) > max_staleness:
                max_staleness = int(period_staleness)
        for key, value in (dq.get("source_counts") or {}).items():
            source_counts[str(key)] += int(value or 0)
    return {
        "sample_count": total,
        "source_counts": dict(source_counts),
        "current_fallback_count": fallback,
        "current_fallback_ratio": fallback / total if total else 0.0,
        "patch_applied_count": patch_applied,
        "event_applied_count": event_applied,
        "max_snapshot_staleness_days": max_staleness,
        "bonds_without_snapshot_count": total_without_snapshot,
    }


def _strategy_attribution(periods: list[dict[str, Any]]) -> dict[str, Any]:
    by_code: dict[str, dict[str, Any]] = {}
    skipped = 0
    costs = []
    cash_weights = []
    for period in periods:
        selected_count = int(period.get("selected_count") or 0)
        costs.append(finite_float(period.get("cost")) or 0.0)
        period_cash = (
            period.get("average_cash_weight")
            if period.get("average_cash_weight") is not None
            else period.get("cash_weight")
        )
        cash_weights.append(finite_float(period_cash) or 0.0)
        skipped += len(period.get("skipped_positions") or [])
        for pos in period.get("positions") or []:
            code = str(pos.get("bond_code") or "")
            if not code:
                continue
            contribution = finite_float(pos.get("return_contribution"))
            if contribution is None:
                weight = 1.0 / selected_count if selected_count > 0 else 0.0
                contribution = (finite_float(pos.get("period_return")) or 0.0) * weight
            bucket = by_code.setdefault(code, {
                "bond_code": code,
                "bond_name": pos.get("bond_name") or "",
                "contribution": 0.0,
                "holding_periods": 0,
                "wins": 0,
                "losses": 0,
            })
            bucket["contribution"] += contribution
            bucket["holding_periods"] += 1
            ret = finite_float(pos.get("period_return")) or 0.0
            if ret > 0:
                bucket["wins"] += 1
            elif ret < 0:
                bucket["losses"] += 1
    ranked = sorted(by_code.values(), key=lambda x: float(x["contribution"]), reverse=True)
    # **集中度要在全体正贡献上算, 不能只看 top_contributors**。那张表是 ``ranked[:10]``,
    # 排在第 11 名之后的正贡献者不在分母里, 于是显示出来的「前三集中度」系统性偏高 ——
    # 而这个数还要去撞 ``_strategy_robustness_notes`` / ``_strategy_dynamic_suggestions``
    # 里 >=0.65 的那道闸, 于是"收益过于集中"这句警告会被虚报出来。
    # 在引擎侧一次算好, 展示层直接读: 让展示层自己去聚合全表, 就是把同一个口径写第二遍。
    positives = [float(x["contribution"]) for x in ranked if float(x["contribution"]) > 0]
    total_positive = sum(positives)
    return {
        "total_cost": sum(costs),
        "cost_drag": -sum(costs),
        "avg_cash_weight": sum(cash_weights) / len(cash_weights) if cash_weights else None,
        "skipped_positions": skipped,
        "top_contributors": ranked[:10],
        "top_detractors": list(reversed(ranked[-10:])) if ranked else [],
        "total_positive_contribution": total_positive,
        "top3_positive_contribution": sum(positives[:3]),
        "positive_contributor_count": len(positives),
    }


def _calendar_return_table(equity_curve: list[dict[str, Any]], granularity: str) -> list[dict[str, Any]]:
    rows = sorted(
        (
            (row.get("date"), finite_float(row.get("equity")))
            for row in equity_curve
        ),
        key=lambda x: x[0] or date.min,
    )
    grouped: dict[str, float] = {}
    prev_date = None
    prev_equity = None
    for current_date, equity in rows:
        if not isinstance(current_date, date) or equity is None or equity <= 0:
            continue
        if prev_date is not None and current_date > prev_date and prev_equity and prev_equity > 0:
            key = (
                f"{current_date.year}"
                if granularity.upper() == "Y"
                else f"{current_date.year}-{current_date.month:02d}"
            )
            grouped[key] = (1.0 + grouped.get(key, 0.0)) * (equity / prev_equity) - 1.0
        prev_date = current_date
        prev_equity = equity
    return [
        {"period": key, "return": value}
        for key, value in sorted(grouped.items())
    ]


def _strategy_warnings(
    summary: dict[str, Any],
    data_quality: dict[str, Any],
    attribution: dict[str, Any],
) -> list[str]:
    warnings: list[str] = []
    fallback_ratio = finite_float(data_quality.get("current_fallback_ratio")) or 0.0
    if fallback_ratio > 0.2:
        warnings.append(f"{fallback_ratio*100:.0f}% 条款样本使用当前数据回退, 需警惕未来函数")
    max_staleness = data_quality.get("max_snapshot_staleness_days")
    if max_staleness is not None and int(max_staleness) > 90:
        warnings.append(f"最近快照距估值日最大滞后 {int(max_staleness)} 天, 部分条款可能过时")
    patch_coverage = data_quality.get("patch_coverage") or {}
    without_patches = patch_coverage.get("bonds_without_patches") or []
    with_patches = int(patch_coverage.get("bonds_with_patches") or 0)
    total_patch_bonds = with_patches + len(without_patches)
    if total_patch_bonds > 0 and len(without_patches) > total_patch_bonds * 0.5:
        warnings.append(
            f"{len(without_patches)}/{total_patch_bonds} 只转债无条款补丁, patch 覆盖率偏低"
        )
    max_drawdown = finite_float(summary.get("max_drawdown")) or 0.0
    if max_drawdown > 0.2:
        warnings.append(f"最大回撤 {max_drawdown*100:.1f}%, 需要检查回撤区间持仓")
    avg_turnover = finite_float(summary.get("avg_turnover")) or 0.0
    if avg_turnover > 0.8:
        warnings.append(f"平均换手 {avg_turnover*100:.0f}%, 对成本和流动性敏感")
    avg_cash = finite_float(summary.get("avg_cash_weight")) or 0.0
    if avg_cash > 0.2:
        warnings.append(f"平均现金权重 {avg_cash*100:.0f}%, 策略条件可能过严或成交数据不足")
    skipped = int(attribution.get("skipped_positions") or 0)
    if skipped > 0:
        warnings.append(f"{skipped} 个入选仓位因缺成交价被现金替代")
    total_cost = finite_float(summary.get("total_cost")) or 0.0
    if total_cost > 0.03:
        warnings.append(f"累计交易成本约 {total_cost*100:.1f}%, 需评估滑点和费率假设")
    return warnings


def _drawdown_stats(equity_curve: list[dict[str, Any]]) -> dict[str, Any]:
    peak = -math.inf
    peak_date = None
    max_dd = 0.0
    max_start = None
    max_end = None
    longest_days = 0
    active_start = None
    last_valid_date = None
    for row in sorted(equity_curve, key=lambda x: x.get("date") or date.min):
        current_date = row.get("date")
        equity = finite_float(row.get("equity"))
        if not isinstance(current_date, date) or equity is None or equity <= 0:
            continue
        last_valid_date = current_date
        if equity >= peak:
            if active_start is not None:
                longest_days = max(longest_days, (current_date - active_start).days)
                active_start = None
            peak = equity
            peak_date = current_date
            continue
        if peak <= 0:
            continue
        if active_start is None:
            active_start = peak_date
        dd = 1.0 - equity / peak
        if dd > max_dd:
            max_dd = dd
            max_start = peak_date
            max_end = current_date
    if active_start is not None and last_valid_date is not None:
        longest_days = max(longest_days, (last_valid_date - active_start).days)
    return {
        "max_drawdown": max_dd,
        "max_drawdown_start": max_start,
        "max_drawdown_end": max_end,
        "max_drawdown_days": (
            (max_end - max_start).days
            if isinstance(max_start, date) and isinstance(max_end, date)
            else 0
        ),
        "longest_drawdown_days": longest_days,
    }


def _normalize_holding_mode(value: str | None) -> str:
    """B 持仓层: top_score(按机会分取前 N) | pool(等权全池)。兼容旧 selection_weighting 别名。"""
    raw = str(value or "").strip().lower()
    aliases = {
        "": "top_score",
        "top_score": "top_score", "score_rank": "top_score", "score": "top_score",
        "rank": "top_score", "top_n": "top_score", "机会分排序": "top_score", "按分topn": "top_score",
        "top n 排序": "top_score", "topn排序": "top_score",
        "pool": "pool", "equal_pool": "pool", "equal": "pool",
        "等权": "pool", "等权全池": "pool", "等权候选池": "pool",
    }
    if raw not in aliases:
        raise ValueError(f"未知持仓模式 holding_mode: {value}")
    return aliases[raw]


def _normalize_funding_mode(value: str | None) -> str:
    """C 资金层: reserve_cash(缺口留现金) | full_invest(满仓摊回)。兼容旧 shortfall_policy 别名。"""
    raw = str(value or "").strip().lower()
    aliases = {
        "": "reserve_cash",
        "reserve_cash": "reserve_cash", "cash": "reserve_cash", "hold_cash": "reserve_cash",
        "leave_cash": "reserve_cash", "留现金": "reserve_cash", "缺口留现金": "reserve_cash",
        "未满留现金": "reserve_cash",
        "full_invest": "full_invest", "full_investment": "full_invest",
        "renormalize": "full_invest", "rebalance": "full_invest",
        "剩余等权": "full_invest", "剩余标的等权": "full_invest", "满仓等权": "full_invest",
    }
    if raw not in aliases:
        raise ValueError(f"未知资金模式 funding_mode: {value}")
    return aliases[raw]


def _funding_legacy_alias(funding_mode: str) -> str:
    """新 funding_mode → 旧 top_n_shortfall_policy 取值 (快照/GUI 兼容镜像)。"""
    return "renormalize" if _normalize_funding_mode(funding_mode) == "full_invest" else "cash"


def _normalize_rank_signal(value: str | None) -> str:
    """B 持仓层排序信号, 含普通排序与按需 PDE 下修优势."""
    raw = str(value or "").strip().lower()
    aliases = {
        # 机会分已整体删除; 旧值 (含空字符串默认) 一律落到「估值偏差」——
        # GUI 的 STRATEGY_PDE_RANK_SIGNAL_LEGACY_ALIASES 早就是这么映射的,
        # 这里跟上是消除分叉。旧快照里 rank_signal 缺失也走这条。
        "": "deviation",
        "score": "deviation", "opportunity_score": "deviation", "机会分": "deviation",
        "double_low": "double_low", "doublelow": "double_low", "双低": "double_low",
        "deviation": "deviation", "偏差": "deviation", "模型偏差": "deviation",
        "pde估值偏差": "deviation",
        # 下修优势系列已随隐含下修强度反解整体删除 (信号在两个 regime 都结构性
        # 无解: 谷底 市价 < price(λ=0)、高位 市价 > price(λ=3))。旧配置/旧快照
        # 仍可能带这些值, 一律落到「估值偏差」—— 与机会分那次的处置一致。
        "down_reset_edge": "deviation", "reset_edge": "deviation",
        "下修优势": "deviation", "pde下修优势": "deviation",
        "下修错定价": "deviation",
        "down_reset_robust_edge": "deviation",
        "robust_reset_edge": "deviation",
        "稳健下修优势": "deviation",
        "pde稳健下修优势": "deviation",
    }
    if raw not in aliases:
        raise ValueError(f"未知排序信号 rank_signal: {value}")
    return aliases[raw]


def _rank_signal_value(row: dict[str, Any], signal: str) -> float | None:
    """行的排序信号值; 排序方向由 ``_sort_candidates_by_rank_signal`` 决定."""
    if signal == "double_low":
        price = finite_float(row.get("market_price"))
        premium = finite_float(row.get("conversion_premium"))
        if price is None or premium is None:
            return None
        return price + premium * 100.0
    if signal == "deviation":
        return finite_float(row.get("deviation"))
    return finite_float(row.get("deviation"))


def _sort_candidates_by_rank_signal(
    candidates: list[dict[str, Any]],
    signal: str,
) -> list[dict[str, Any]]:
    """按排序信号重排候选池。

    两种下修优势降序, 其余信号升序。缺值行沉底, 同值按代码稳定排序。
    (原来还有一档 ``score`` 直接沿用 ``sort_batch_results_for_review`` 的顺序,
    随机会分一并删除。)
    """
    def key(row: dict[str, Any]):
        value = _rank_signal_value(row, signal)
        sort_value = value
        return (
            0 if value is not None else 1,
            sort_value if sort_value is not None else float("inf"),
            str(row.get("bond_code") or ""),
        )

    return sorted(candidates, key=key)


def _normalize_exposure_mode(value: str | None) -> str:
    """D 仓位层: full(恒定满仓) | valuation(估值水平缩放)。"""
    raw = str(value or "").strip().lower()
    aliases = {
        "": "full", "full": "full", "满仓": "full", "恒定满仓": "full",
        "valuation": "valuation", "估值": "valuation", "估值择时": "valuation",
        "估值缩放": "valuation", "timing": "valuation",
    }
    if raw not in aliases:
        raise ValueError(f"未知仓位模式 exposure_mode: {value}")
    return aliases[raw]


def _resolve_exposure(
    cfg: ScoreStrategyConfig,
    priced_rows: list[dict[str, Any]],
) -> tuple[float, float | None]:
    """按当期已定价池中位 deviation 解析总仓位 gross。

    返回 (gross, median_deviation)。full 模式恒为 (1.0, medDev) — medDev 仍记录,
    便于结果里对照。valuation 模式: gross = clip(1 - k·max(0, medDev), floor, 1.0);
    medDev 不可得 (无有效 deviation) 时回落满仓, 不猜。
    """
    devs = [
        d for d in (finite_float(row.get("deviation")) for row in priced_rows
                    if row.get("status") == "ok")
        if d is not None
    ]
    median_dev = float(np.median(devs)) if devs else None
    if _normalize_exposure_mode(cfg.exposure_mode) != "valuation" or median_dev is None:
        return 1.0, median_dev
    floor = min(max(float(cfg.exposure_floor), 0.0), 1.0)
    gross = 1.0 - float(cfg.exposure_valuation_k) * max(0.0, median_dev)
    return float(min(1.0, max(floor, gross))), median_dev


def _equal_weight_portfolio_weights(
    codes: list[str],
    denominator: int | None = None,
    gross: float = 1.0,
) -> dict[str, float]:
    """等权权重映射 (含现金桶)。``gross`` 为总仓位缩放 (D 仓位层), 余量计入现金。"""
    if denominator is None:
        denominator = len(codes)
    denominator = max(0, int(denominator))
    if denominator <= 0:
        return {"__cash__": 1.0}
    gross = max(0.0, float(gross))
    weights = {code: gross / denominator for code in codes}
    cash_weight = max(0.0, 1.0 - gross * len(codes) / denominator)
    if cash_weight > 0:
        weights["__cash__"] = cash_weight
    return weights


def _equal_weight_turnover(
    previous_codes: list[str],
    current_codes: list[str],
    *,
    previous_denominator: int | None = None,
    current_denominator: int | None = None,
    previous_gross: float = 1.0,
    current_gross: float = 1.0,
) -> float:
    prev_weight = _equal_weight_portfolio_weights(
        previous_codes, previous_denominator, previous_gross)
    curr_weight = _equal_weight_portfolio_weights(
        current_codes, current_denominator, current_gross)
    codes = set(prev_weight) | set(curr_weight)
    # 0.5·Σ|Δw| (含现金桶) = 单边换手: 证券净卖出与现金净增完全对偶, 不会双计。
    return 0.5 * sum(
        abs(curr_weight.get(code, 0.0) - prev_weight.get(code, 0.0)) for code in codes)


def _periods_per_year(freq: str) -> int:
    return {
        "D": 252,
        "W": 52,
        "M": 12,
        "Q": 4,
    }.get((freq or "M").upper(), 12)


def _last_weekday_of_month(year: int, month: int) -> date:
    if month == 12:
        d = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        d = date(year, month + 1, 1) - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _csv_value(value: Any):
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float):
        return "" if math.isnan(value) else f"{value:.8f}"
    if value is None:
        return ""
    return value
