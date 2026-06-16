"""基于批量机会分的选债策略回测.

第一版策略保持可解释:
  - 每个调仓日对候选池做批量定价并复用 ``opportunity_score`` 排序
  - 选出前 N 只转债, 按等权持有到下一调仓边界
  - 收益用调仓日/期末附近最近有效转债收盘价计算

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
    DEFAULT_UNDERVALUED_SCORE_THRESHOLD,
    HARD_REVIEW_TAGS,
    batch_pricing_exclusion_reason,
    filter_batch_results_by_view,
)
from .data_providers import DataProvider, finite_float
from .pricing_api import batch_price_from_provider_threaded


@dataclass(frozen=True)
class ScoreStrategyConfig:
    """机会分选债策略参数 (三层解耦: A 过滤 / B 持仓 / C 资金)。

    A 过滤层(选什么): selection_view + min_score/min_confidence/exclude_risk_tags +
        价格/溢价/偏差/波动率区间 → 候选池; **机会分在此仅作过滤, 不再做权重排序**。
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
    min_score: float | None = 0.0
    min_confidence: tuple[str, ...] | None = ("高", "中")
    exclude_risk_tags: tuple[str, ...] = tuple(sorted(HARD_REVIEW_TAGS))
    min_market_price: float | None = None
    max_market_price: float | None = None
    min_conversion_premium: float | None = None
    max_conversion_premium: float | None = None
    min_deviation: float | None = None
    max_deviation: float | None = None
    min_sigma: float | None = None
    max_sigma: float | None = None
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
    pool_mode: str = "static"  # "static" | "dynamic"
    # ── B 持仓层: 怎么从候选池构成持仓 (一律等权) ──
    #   "top_score": 按机会分取前 top_n 只。
    #   "pool"     : 等权持有整个候选池, 不按机会分精排。
    # 证据现状 (两种均为研究配置, 无推荐): 跨周期(2022-2026)横截面 Rank-IC≈0,
    # 机会分排序无稳健选股 alpha; top_score 在 4 年季频对比中风险调整更优
    # (Sharpe 0.60 vs 0.40), 但源于"候选不足→留现金"的隐性缓冲与极端偏差尾部集中,
    # 月频 2025-26 反向 (现金拖累跑输基准), 不跨频率稳健。
    holding_mode: str = "top_score"
    max_holdings: int | None = None    # pool 模式持仓上限 (None=全池; 设值时取分数最高的若干只)
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
        self._history_end = min(padded_end, date.today() - timedelta(days=1))
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
    held_codes: list[str]        # 实际建仓的标的码 (供下期换手计算, 不含缺价票)
    weight_denominator: int      # 本期等权份数分母 (intended), 供下期换手计算
    exposure: float              # 本期总仓位 gross (D 仓位层), 供下期换手计算
    benchmark_codes: list[str]   # 本期基准成分 (供下期基准换手/成本计算)


def _strategy_config_summary(cfg: ScoreStrategyConfig) -> dict[str, Any]:
    """回测结果里回显的配置快照 (供 GUI/CSV 展示与复现)。"""
    holding_mode = _normalize_holding_mode(cfg.holding_mode)
    funding_mode = _normalize_funding_mode(cfg.funding_mode)
    return {
        "top_n": cfg.top_n,
        "holding_mode": holding_mode,
        "max_holdings": cfg.max_holdings,
        "funding_mode": funding_mode,
        # 兼容旧快照/GUI 的派生镜像 (新接口请读 holding_mode/funding_mode)
        "top_n_shortfall_policy": _funding_legacy_alias(funding_mode),
        "rebalance_freq": cfg.rebalance_freq,
        "selection_view": cfg.selection_view,
        "min_score": cfg.min_score,
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
        "price_lookback_days": cfg.price_lookback_days,
        "max_price_staleness_days": cfg.max_price_staleness_days,
        "execution_timing": _normalize_execution_timing(cfg.execution_timing),
        "execution_lookahead_days": cfg.execution_lookahead_days,
        "mark_to_market": cfg.mark_to_market,
        "pre_filter_prices": cfg.pre_filter_prices,
        "transaction_cost": cfg.transaction_cost,
        "compute_benchmark": cfg.compute_benchmark,
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
    pricing_codes, prefilter_excluded = _pre_filter_codes_by_price(
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
        **ctx.pricer_overrides,
    )
    _raise_if_pricing_transport_outage(
        priced_rows,
        total_count=len(pricing_codes),
        period_start=period_start,
    )

    # 转债成交价缓存: 策略持仓与基准共享, 避免同一调仓期重复拉历史。
    price_cache: dict[tuple, PricePoint | None] = {}
    candidates = _select_candidate_rows(priced_rows, cfg)
    # B 持仓层: 从候选池构成持仓
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
    else:  # top_score: 按机会分取前 top_n
        selected = candidates[:cfg.top_n]
    selected_codes = [str(row.get("bond_code")) for row in selected]
    candidate_rows = _candidate_explanation_rows(candidates, selected_codes, cfg)
    rejection_rows = _rejection_explanation_rows(
        priced_rows,
        excluded,
        cfg,
        candidate_codes={str(row.get("bond_code")) for row in candidates},
    )
    _emit_stage_progress(stage_cb, "持仓估值", 0, len(selected), idx, total_periods)
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
    )
    _emit_stage_progress(stage_cb, "持仓估值", len(selected), len(selected), idx, total_periods)

    # C 资金层: 等权份数分母 (intended)
    funding_mode = _normalize_funding_mode(cfg.funding_mode)
    held = len(positions)            # 实际有成交价、能建仓的标的数
    held_codes = [str(pos.get("bond_code")) for pos in positions]
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
    turnover = _equal_weight_turnover(
        previous_held_codes,
        held_codes,
        previous_denominator=previous_intended,
        current_denominator=intended,
        previous_gross=previous_exposure,
        current_gross=exposure,
    )

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
    period_start_equity = start_equity
    cost = turnover * cfg.transaction_cost
    # 闲置现金按年化 cash_yield_rate 计息 (默认 0 = 旧行为)。不计息时, Sharpe 的
    # rf 门槛会系统性惩罚一切持现金配置 (缺口留现金 / 择时缩放)——内部不一致。
    period_days = max(0, (period_end - period_start).days)
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
        benchmark_return, benchmark_codes = _benchmark_period_return(
            provider,
            priced_rows,
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

    scored = [finite_float(row.get("opportunity_score")) for row in selected]
    finite_scores = [s for s in scored if s is not None]
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
        "selected_codes": selected_codes,
        "candidate_rows": candidate_rows,
        "rejection_rows": rejection_rows,
        "avg_score": (sum(finite_scores) / len(finite_scores)) if finite_scores else None,
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
        "holding_mode": holding_mode,
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
    p_down: float = 0.15,
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
    """回测机会分选债策略.

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
        的参照线; 用于衡量机会分排序带来的超额。
    """
    cfg = config or ScoreStrategyConfig()
    if cfg.top_n <= 0:
        raise ValueError("top_n 必须为正整数")
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

    summary = _summarize_strategy(
        equity_curve,
        periods,
        start_date=schedule[0],
        end_date=schedule[-1],
        freq=cfg.rebalance_freq,
        top_n=cfg.top_n,
        risk_free_rate=r,
        benchmark_curve=benchmark_curve if cfg.compute_benchmark else None,
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
        "periods": periods,
        "rebalance_snapshots": snapshots,
        "summary": summary,
        "diagnostics": diagnostics,
    }


_SUMMARY_CSV_KEYS = (
    "periods", "final_equity", "total_return", "annualized_return",
    "annualized_volatility", "volatility_basis", "sharpe", "sortino",
    "calmar", "max_drawdown", "max_drawdown_days", "hit_rate",
    "avg_selected_count", "avg_turnover", "avg_cash_weight", "total_cost",
    "benchmark_final_equity", "benchmark_total_return", "excess_return",
)


_PERIOD_CSV_COLUMNS = [
    "start_date", "end_date", "entry_date", "exit_date",
    "period_return", "gross_return", "cost",
    "benchmark_return", "equity", "benchmark_equity", "turnover", "cash_weight",
    "eligible_count", "priced_count", "candidate_count", "selected_count",
    "avg_score", "execution_timing", "selected_codes",
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
        writer.writerow([
            _csv_value(row.get("start_date")),
            _csv_value(row.get("end_date")),
            _csv_value(row.get("entry_date")),
            _csv_value(row.get("exit_date")),
            _csv_value(row.get("period_return")),
            _csv_value(row.get("gross_return")),
            _csv_value(row.get("cost")),
            _csv_value(row.get("benchmark_return")),
            _csv_value(row.get("equity")),
            _csv_value(row.get("benchmark_equity")),
            _csv_value(row.get("turnover")),
            _csv_value(row.get("cash_weight")),
            row.get("eligible_count", ""),
            row.get("priced_count", ""),
            row.get("candidate_count", ""),
            row.get("selected_count", ""),
            _csv_value(row.get("avg_score")),
            row.get("execution_timing", ""),
            "|".join(str(code) for code in row.get("selected_codes") or []),
        ])


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
        "entry_date", "exit_date", "start_price", "end_price",
        "period_return", "score", "confidence", "risk_tags",
    ])
    for period, pos in positions:
        writer.writerow([
            _csv_value(period.get("start_date")),
            _csv_value(period.get("end_date")),
            pos.get("rank", ""),
            pos.get("bond_code", ""),
            pos.get("bond_name", ""),
            _csv_value(pos.get("entry_date")),
            _csv_value(pos.get("exit_date")),
            _csv_value(pos.get("start_price")),
            _csv_value(pos.get("end_price")),
            _csv_value(pos.get("period_return")),
            _csv_value(pos.get("score")),
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
        "period_start", "period_end", "bond_code", "bond_name",
        "reason", "entry_date", "exit_date", "start_price", "end_price",
    ])
    for period, pos in skipped:
        writer.writerow([
            _csv_value(period.get("start_date")),
            _csv_value(period.get("end_date")),
            pos.get("bond_code", ""),
            pos.get("bond_name", ""),
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
        "selection_reason", "score", "market_price", "deviation",
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
            _csv_value(row.get("score")),
            _csv_value(row.get("market_price")),
            _csv_value(row.get("deviation")),
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
        "reason", "score", "market_price", "deviation",
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
            _csv_value(row.get("score")),
            _csv_value(row.get("market_price")),
            _csv_value(row.get("deviation")),
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
    parts = [getattr(provider, "name", provider.__class__.__name__)]
    inner = getattr(provider, "inner", None)
    if inner is not None:
        parts.append(_provider_cache_identity(inner))
    for attr in ("history_store", "patch_store", "event_store"):
        obj = getattr(provider, attr, None)
        path = getattr(obj, "root", None) or getattr(obj, "path", None)
        if path is not None:
            p = Path(path)
            try:
                stat = p.stat()
                parts.append(f"{attr}:{p}:{stat.st_mtime_ns}")
            except OSError:
                parts.append(f"{attr}:{p}:missing")
    return "|".join(str(part) for part in parts)


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
) -> tuple[list[str], list[tuple[str, str]]]:
    if not cfg.pre_filter_prices or (cfg.min_market_price is None and cfg.max_market_price is None):
        if progress_cb is not None:
            progress_cb(len(codes), len(codes))
        return codes, []
    kept: list[str] = []
    excluded: list[tuple[str, str]] = []
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
                continue
            kept.append(code)
        finally:
            if progress_cb is not None and _should_emit_code_progress(done, total):
                progress_cb(done, total)
    return kept, excluded


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
            # 防前视: 回测日期早于发行日 → 该转债尚未存在
            issue_dt = getattr(terms, 'issue_date', None) if terms is not None else None
            if issue_dt is not None and issue_dt > on_date:
                excluded.append((code, f"尚未发行 (发行日 {issue_dt})"))
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
    """动态标的池: 只保留估值日已发行且未到期的转债.

    优先使用 provider.list_tradable_cbs(on_date), 取与 base_codes 的交集;
    若 provider 不支持, 则根据 issue_date/maturity_date 过滤 base_codes.
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
        issue_dt = getattr(terms, 'issue_date', None)
        maturity_dt = getattr(terms, 'maturity_date', None)
        if issue_dt is not None and issue_dt > on_date:
            continue  # 尚未发行
        if maturity_dt is not None and maturity_dt <= on_date:
            continue  # 已到期
        filtered.append(code)
    return filtered


def _select_candidate_rows(rows: list[dict[str, Any]], cfg: ScoreStrategyConfig) -> list[dict[str, Any]]:
    view = cfg.selection_view if cfg.selection_view in BATCH_REVIEW_VIEWS else "综合机会"
    ranked = filter_batch_results_by_view(rows, view)
    excluded_tags = set(cfg.exclude_risk_tags or ())
    selected: list[dict[str, Any]] = []
    for row in ranked:
        if row.get("status") != "ok":
            continue
        score = finite_float(row.get("opportunity_score"))
        if score is None:
            continue
        if cfg.min_score is not None and score < cfg.min_score:
            continue
        if cfg.min_confidence and row.get("confidence") not in cfg.min_confidence:
            continue
        if excluded_tags and excluded_tags & set(row.get("risk_tags") or []):
            continue
        market_price = finite_float(row.get("market_price"))
        if market_price is None or market_price <= 0:
            continue
        if not _passes_range(market_price, cfg.min_market_price, cfg.max_market_price):
            continue
        premium = finite_float(row.get("conversion_premium"))
        if not _passes_range(premium, cfg.min_conversion_premium, cfg.max_conversion_premium):
            continue
        deviation = finite_float(row.get("deviation"))
        if not _passes_range(deviation, cfg.min_deviation, cfg.max_deviation):
            continue
        sigma = finite_float(row.get("sigma"))
        if not _passes_range(sigma, cfg.min_sigma, cfg.max_sigma):
            continue
        selected.append(row)
    return selected


def _candidate_explanation_rows(
    candidates: list[dict[str, Any]],
    selected_codes: list[str],
    cfg: ScoreStrategyConfig,
    *,
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
            "score": finite_float(row.get("opportunity_score")),
            "market_price": finite_float(row.get("market_price")),
            "theoretical_price": finite_float(row.get("theoretical_price")),
            "deviation": finite_float(row.get("deviation")),
            "conversion_premium": finite_float(row.get("conversion_premium")),
            "sigma": finite_float(row.get("sigma")),
            "confidence": row.get("confidence"),
            "risk_tags": list(row.get("risk_tags") or []),
            "model_signal_status": row.get("model_signal_status"),
        })
    return rows


def _candidate_selection_reason(
    row: dict[str, Any],
    rank: int,
    cfg: ScoreStrategyConfig,
    selected: bool,
) -> str:
    score = finite_float(row.get("opportunity_score"))
    deviation = finite_float(row.get("deviation"))
    premium = finite_float(row.get("conversion_premium"))
    tags = [str(tag) for tag in row.get("risk_tags") or []]
    parts = []
    if score is not None:
        parts.append(f"机会分 {score:.1f}")
    if deviation is not None:
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
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for code, reason in excluded:
        code = str(code)
        rows.append({
            "bond_code": code,
            "bond_name": "",
            "source": "准入/预筛",
            "reason": str(reason),
            "score": None,
            "market_price": None,
            "deviation": None,
            "conversion_premium": None,
            "confidence": "",
            "risk_tags": [],
        })
        seen.add(code)
        if len(rows) >= limit:
            return rows

    for row in filter_batch_results_by_view(priced_rows, "综合机会"):
        code = str(row.get("bond_code") or "")
        if not code or code in seen or code in candidate_codes:
            continue
        reason = _candidate_filter_reason(row, cfg)
        if reason is None:
            continue
        rows.append({
            "bond_code": code,
            "bond_name": row.get("bond_name"),
            "source": "筛选",
            "reason": reason,
            "score": finite_float(row.get("opportunity_score")),
            "market_price": finite_float(row.get("market_price")),
            "deviation": finite_float(row.get("deviation")),
            "conversion_premium": finite_float(row.get("conversion_premium")),
            "confidence": row.get("confidence"),
            "risk_tags": list(row.get("risk_tags") or []),
        })
        seen.add(code)
        if len(rows) >= limit:
            break
    return rows


def _candidate_filter_reason(row: dict[str, Any], cfg: ScoreStrategyConfig) -> str | None:
    if row.get("status") != "ok":
        return str(row.get("error") or row.get("message") or "定价失败")

    tags = set(str(tag) for tag in row.get("risk_tags") or [])
    score = finite_float(row.get("opportunity_score"))
    view = cfg.selection_view if cfg.selection_view in BATCH_REVIEW_VIEWS else "综合机会"
    if view == "低估候选":
        if score is None:
            return "低估候选视图: 缺少机会分"
        if score < DEFAULT_UNDERVALUED_SCORE_THRESHOLD:
            return f"低估候选视图: 机会分 {score:.1f} < {DEFAULT_UNDERVALUED_SCORE_THRESHOLD:.1f}"
        if row.get("confidence") not in {"高", "中"}:
            return "低估候选视图: 置信度不足"
        if "转股折价" in tags:
            return "低估候选视图: 转股折价单独归类"
        hard = tags & HARD_REVIEW_TAGS
        if hard:
            return "低估候选视图: 硬复核标签 " + "/".join(sorted(hard))
    elif view == "转股折价" and "转股折价" not in tags:
        return "转股折价视图: 未出现转股折价标签"
    elif view == "需复核" and not (
        tags & HARD_REVIEW_TAGS or row.get("confidence") == "低"
    ):
        return "需复核视图: 不属于复核池"

    if score is None:
        return "缺少机会分"
    if cfg.min_score is not None and score < cfg.min_score:
        return f"机会分 {score:.1f} < 最低分 {cfg.min_score:.1f}"
    if cfg.min_confidence and row.get("confidence") not in cfg.min_confidence:
        return f"置信度 {row.get('confidence') or '—'} 不在允许范围"
    excluded_tags = set(cfg.exclude_risk_tags or ())
    hard = excluded_tags & tags
    if hard:
        return "命中排除标签 " + "/".join(sorted(hard))

    market_price = finite_float(row.get("market_price"))
    if market_price is None or market_price <= 0:
        return "缺少有效市价"
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
    sigma = finite_float(row.get("sigma"))
    reason = _range_filter_reason("HV", sigma, cfg.min_sigma, cfg.max_sigma, pct=True)
    if reason:
        return reason
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
        exit_point = _execution_price_point(
            provider, code, end_date,
            timing=execution_timing,
            side="exit",
            lookback_days=lookback_days,
            max_staleness_days=max_staleness_days,
            lookahead_days=execution_lookahead_days,
            cache=price_cache,
        )
        if entry_point is None or exit_point is None:
            skipped.append({
                "bond_code": code,
                "bond_name": row.get("bond_name"),
                "reason": _missing_execution_reason(
                    entry_point, exit_point, execution_timing),
                "entry_date": entry_point.date if entry_point else None,
                "exit_date": exit_point.date if exit_point else None,
                "start_price": entry_point.price if entry_point else None,
                "end_price": exit_point.price if exit_point else None,
            })
            continue
        ret = exit_point.price / entry_point.price - 1.0
        positions.append({
            "rank": rank,
            "bond_code": code,
            "bond_name": row.get("bond_name"),
            "score": finite_float(row.get("opportunity_score")),
            "confidence": row.get("confidence"),
            "risk_tags": list(row.get("risk_tags") or []),
            "entry_date": entry_point.date,
            "exit_date": exit_point.date,
            "start_price": entry_point.price,
            "end_price": exit_point.price,
            "period_return": ret,
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
        if entry_point is None or exit_point is None:
            continue
        returns.append(exit_point.price / entry_point.price - 1.0)
        codes.append(code)
    if not returns:
        return None, []
    return float(sum(returns) / len(returns)), codes


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
        return cash_weight * cash_yield_rate * max(0, (on_date - period_start).days) / 365.0

    if intended_count <= 0:
        return [{"date": period_end,
                 "equity": start_equity * (1.0 + _cash_accrual(period_end))}]
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
        if entry_price is None or exit_price is None:
            continue
        start, end = min(entry_date, exit_date), max(entry_date, exit_date)
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
                pos_return = exit_price / entry_price - 1.0
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
        if px is None:
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
    periods_per_year = 252 if use_curve_returns else _periods_per_year(freq)
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
    selected_counts = [int(row.get("selected_count") or 0) for row in periods]
    turnovers = [finite_float(row.get("turnover")) for row in periods]
    finite_turnovers = [t for t in turnovers if t is not None]
    cash_weights = [finite_float(row.get("cash_weight")) for row in periods]
    finite_cash_weights = [w for w in cash_weights if w is not None]
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
        "total_cost": sum(finite_costs) if finite_costs else 0.0,
        "benchmark_final_equity": benchmark_final_equity,
        "benchmark_total_return": benchmark_total_return,
        "excess_return": excess_return,
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
        cash_weights.append(finite_float(period.get("cash_weight")) or 0.0)
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
    return {
        "total_cost": sum(costs),
        "cost_drag": -sum(costs),
        "avg_cash_weight": sum(cash_weights) / len(cash_weights) if cash_weights else None,
        "skipped_positions": skipped,
        "top_contributors": ranked[:10],
        "top_detractors": list(reversed(ranked[-10:])) if ranked else [],
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
