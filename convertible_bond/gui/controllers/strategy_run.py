"""策略回测 — 运行执行 (启动/取消/worker/进度/provider 构建).

从 strategy_backtest.py 按职责拆出; 经 StrategyBacktestMixin 聚合混入
CBPricerApp, 方法间通过 self.* 跨 mixin 调用不受拆分影响。
"""
from __future__ import annotations

import threading
from datetime import date
from tkinter import messagebox

from ...batch_pricing import AdmissionFilterConfig, build_batch_provider
from ...backtest_disk_cache import DiskCacheProvider
from ...cb_events import CBEventStore, project_events_path
from ...data_providers import WindDataProvider
from ...historical_terms import (
    HistoricalBondDataProvider,
    TermsPatchStore,
    project_terms_patches_path,
)
from ...paths import data_dir
from ...strategy_backtest import (
    ScoreStrategyConfig,
    _funding_legacy_alias,
    _normalize_holding_mode,
    backtest_score_strategy,
)
from ..theme import VOL_WINDOW_MAP
from ..constants import normalize_strategy_history_mode

from .strategy_common import (
    STRATEGY_BACKTEST_PRO_FEATURE,
    STRATEGY_BACKTEST_PRO_PREVIEW,
    STRATEGY_VIEW_POLICY,
    StrategyBacktestCancelled,
    WIND_HIGH_FIDELITY_CODE_WARN_LIMIT,
    WIND_HIGH_FIDELITY_PRICING_WARN_LIMIT,
    _DEFAULT_VIEW_POLICY,
    _STRATEGY_PDE_GRID_M,
    _STRATEGY_PDE_GRID_N,
)


class StrategyRunMixin:
    """策略回测 — 运行执行 (启动/取消/worker/进度/provider 构建)."""

    def _run_strategy_backtest(self):
        if not self._strategy_backtest_pro_available():
            messagebox.showinfo("Pro 功能", "策略回测将作为 CBLens Pro 功能提供")
            return
        try:
            start = date.fromisoformat(self.v_st_start.get().strip())
            end = date.fromisoformat(self.v_st_end.get().strip())
        except ValueError:
            messagebox.showerror("错误", "策略回测日期格式应为 YYYY-MM-DD")
            return
        if start >= end:
            messagebox.showerror("错误", "策略回测开始日期应早于结束日期")
            return

        try:
            codes, _pool_label = self._strategy_codes_from_pool()
        except Exception as exc:
            messagebox.showerror("代码池错误", str(exc))
            return
        if not codes:
            mode = self.v_st_pool_mode.get() if hasattr(self, "v_st_pool_mode") else "本地全市场"
            if mode == "当前筛选结果":
                messagebox.showwarning("提示", "当前批量筛选结果为空, 请先到批量页刷新重算或切换视图")
            elif mode == "自选代码":
                messagebox.showwarning("提示", "自选代码池为空, 请粘贴或导入转债代码")
            else:
                messagebox.showwarning("提示", "本地条款库为空, 请先同步转债池")
            return

        freq_map = {"周": "W", "月": "M", "季": "Q"}
        view = self.v_st_view.get()
        policy = STRATEGY_VIEW_POLICY.get(view, _DEFAULT_VIEW_POLICY)
        # 本地全市场池含大量退市/已到期/定向债, 静态全量送 Wind 会大面积取数失败。
        # 改用动态时点池: 每期先按 list_tradable_cbs(当期) 取存活券再筛选定价,
        # 无幸存者偏差且从源头避开死债的 Wind 请求。自选/当前筛选池保持静态。
        gui_pool_mode = self.v_st_pool_mode.get() if hasattr(self, "v_st_pool_mode") else "本地全市场"
        engine_pool_mode = "dynamic" if gui_pool_mode == "本地全市场" else "static"
        try:
            holding_mode = _normalize_holding_mode(
                getattr(self, "v_st_weighting", None).get()
                if getattr(self, "v_st_weighting", None) is not None else "top_score")
            # pool→满仓等权(缺价摊回); top_score→缺口留现金 (沿用旧 score_rank 行为)
            funding_mode = "full_invest" if holding_mode == "pool" else "reserve_cash"
            cash_yield_pct = (
                self._optional_float(self.v_st_cash_yield)
                if hasattr(self, "v_st_cash_yield") else None)
            exposure_raw = (getattr(self, "v_st_exposure", None).get()
                            if getattr(self, "v_st_exposure", None) is not None else "恒定满仓")
            exposure_mode = "valuation" if "估值" in str(exposure_raw) else "full"
            config = ScoreStrategyConfig(
                top_n=max(1, int(float(self.v_st_top_n.get()))),
                holding_mode=holding_mode,
                funding_mode=funding_mode,
                cash_yield_rate=max(0.0, (cash_yield_pct or 0.0) / 100.0),
                exposure_mode=exposure_mode,
                rebalance_freq=freq_map.get(self.v_st_freq.get(), "M"),
                selection_view=view,
                min_confidence=policy["min_confidence"],
                exclude_risk_tags=(
                    ScoreStrategyConfig().exclude_risk_tags
                    if policy["exclude_review_risks"] else ()
                ),
                min_market_price=self._optional_float(self.v_st_min_price),
                max_market_price=self._optional_float(self.v_st_max_price),
                min_conversion_premium=self._optional_pct(self.v_st_min_premium),
                max_conversion_premium=self._optional_pct(self.v_st_max_premium),
                min_deviation=self._optional_pct(self.v_st_min_deviation),
                max_deviation=self._optional_pct(self.v_st_max_deviation),
                min_sigma=self._optional_pct(self.v_st_min_sigma),
                max_sigma=self._optional_pct(self.v_st_max_sigma),
                execution_timing="next_close",
                transaction_cost=max(0.0, self._optional_float(self.v_st_cost) or 0.0) / 10000.0,
                compute_benchmark=bool(self.v_st_benchmark.get()),
                # 开启基准时自动叠加中证转债指数第二基准 (Wind 可取; 其它源优雅缺省)
                benchmark_index_code=("000832.CSI" if bool(self.v_st_benchmark.get()) else None),
                pool_mode=engine_pool_mode,
            )
            admission_config = AdmissionFilterConfig(
                delist_window_days=max(0, int(float(self.v_st_delist_window.get() or 0))),
                min_outstanding_balance=self._optional_float(self.v_st_min_balance),
                min_credit_rating=self.v_st_min_rating.get().strip() or None,
                min_turnover_amount=self._optional_float(self.v_st_min_turnover),
            )
            params = dict(
                r=float(self.v_r.get()) / 100.0,
                base_spread=float(self.v_spread.get()) / 100.0,
                p_down=float(self.v_p_down.get()) / 100.0,
                distress_k=float(self.v_dk.get()) / 100.0,
                M=_STRATEGY_PDE_GRID_M,
                N=_STRATEGY_PDE_GRID_N,
                vol_window_days=VOL_WINDOW_MAP.get(self.v_vol_window.get(), 21),
            )
        except ValueError as exc:
            messagebox.showerror("参数错误", f"策略参数解析失败: {exc}")
            return

        # 运行前自动执行预检并展示, 预检失败不阻塞运行
        precheck = None
        try:
            precheck = self._strategy_precheck_info()
            self.v_st_precheck.set(self._format_strategy_precheck(precheck))
            self._strategy_bt_expected_pricing = precheck.get("estimated_pricing")
        except Exception as exc:
            self.v_st_precheck.set(f"⚠ 预检异常: {exc}")
            self._strategy_bt_expected_pricing = None

        if precheck is not None and precheck.get("history_mode") == "Wind高保真":
            params["max_workers"] = 1
            if self._strategy_wind_high_fidelity_is_expensive(precheck):
                if not self._confirm_expensive_wind_strategy_backtest(precheck):
                    self.v_st_status.set("已取消 Wind高保真大池回测")
                    return
        history_mode = (
            precheck.get("history_mode") if precheck is not None
            else normalize_strategy_history_mode(
                self.v_st_history_mode.get() if hasattr(self, "v_st_history_mode") else "标准"
            )
        )

        source = self.v_data_source.get()
        run_settings = self._strategy_run_settings(
            codes=codes,
            start=start,
            end=end,
            source=source,
            history_mode=history_mode,
            gui_pool_mode=gui_pool_mode,
            engine_pool_mode=engine_pool_mode,
            config=config,
            admission_config=admission_config,
            params=params,
            precheck=precheck,
        )
        self._strategy_bt_cancel = threading.Event()
        self._strategy_bt_running = True
        self.btn_strategy_backtest.configure(text="停止", command=self._cancel_strategy_backtest)
        self.btn_strategy_bt_csv.configure(state="disabled")
        if hasattr(self, "strategy_bt_progress"):
            self.strategy_bt_progress.set(0)
        self.v_st_status.set(
            f"正在回测 {len(codes)} 只 · "
            f"{start} → {end} · {self.v_st_freq.get()}调仓"
        )
        threading.Thread(
            target=self._strategy_backtest_worker,
            args=(
                codes, start, end, source, config, admission_config,
                params, history_mode, run_settings,
            ),
            daemon=True,
        ).start()

    def _cancel_strategy_backtest(self):
        if self._strategy_bt_cancel is not None:
            self._strategy_bt_cancel.set()
        self.v_st_status.set("⏹ 正在停止 (完成当前 Wind/定价请求后中断) ...")

    @staticmethod
    def _strategy_wind_high_fidelity_is_expensive(precheck: dict) -> bool:
        if precheck.get("history_mode") != "Wind高保真":
            return False
        return (
            int(precheck.get("code_count") or 0) > WIND_HIGH_FIDELITY_CODE_WARN_LIMIT
            or int(precheck.get("estimated_pricing") or 0) > WIND_HIGH_FIDELITY_PRICING_WARN_LIMIT
        )

    @staticmethod
    def _confirm_expensive_wind_strategy_backtest(precheck: dict) -> bool:
        return messagebox.askokcancel(
            "Wind高保真回测耗时很长",
            "当前配置会对 Wind 做大量同步请求:\n\n"
            f"代码池: {precheck.get('code_count')} 只\n"
            f"调仓期: {precheck.get('period_count')} 期\n"
            f"预计定价: ≈{precheck.get('estimated_pricing')} 次\n"
            f"Wind请求估算: ≈{precheck.get('estimated_wind_requests')} 次\n\n"
            "建议改用「标准」历史口径, 或切换到「当前筛选结果/自选代码」的小池再跑。"
            "仍要继续时将自动把 Wind 调用设为单线程, 但耗时仍可能很长。",
        )

    def _strategy_backtest_pro_available(self) -> bool:
        """未来接授权时只需替换这里的判断."""
        return bool(
            STRATEGY_BACKTEST_PRO_PREVIEW
            or getattr(self, "pro_license_active", False)
            or getattr(self, "_pro_features", {}).get(STRATEGY_BACKTEST_PRO_FEATURE)
        )

    def _strategy_backtest_worker(
        self,
        codes,
        start,
        end,
        source,
        config,
        admission_config,
        params,
        history_mode,
        run_settings,
    ):
        try:
            provider = self._build_strategy_provider(source)

            def cancel_check():
                if self._strategy_bt_cancel is not None and self._strategy_bt_cancel.is_set():
                    raise StrategyBacktestCancelled()

            def progress(done, total):
                cancel_check()

                def _update():
                    pct = done / total if total else 0
                    expected = getattr(self, "_strategy_bt_expected_pricing", None)
                    suffix = f" · 预计定价≈{expected} 次" if expected else ""
                    self.v_st_status.set(
                        f"定价/选债/估值 {done}/{total} ({pct:.0%}){suffix}"
                    )
                    if hasattr(self, "strategy_bt_progress"):
                        self.strategy_bt_progress.set(pct)
                self.after(0, _update)

            def stage_progress(stage, done, total, period_idx, total_periods):
                cancel_check()

                def _update():
                    pct = self._strategy_stage_progress_pct(
                        stage, done, total, period_idx, total_periods)
                    self.v_st_status.set(
                        f"{stage} {done}/{total} · "
                        f"第 {period_idx + 1}/{total_periods} 期"
                    )
                    if hasattr(self, "strategy_bt_progress"):
                        self.strategy_bt_progress.set(pct)
                self.after(0, _update)

            try:
                result = backtest_score_strategy(
                    provider,
                    codes,
                    start_date=start,
                    end_date=end,
                    config=config,
                    terms_cache=None,
                    admission_config=admission_config,
                    pricing_snapshot_cache=getattr(self, "_strategy_pricing_cache", None),
                    progress_cb=progress,
                    stage_cb=stage_progress,
                    cancel_cb=cancel_check,
                    **params,
                )
            finally:
                # 跨运行磁盘缓存: 中途取消/异常也落盘已拉取的昂贵数据 (与 CLI 同口径)
                flush = getattr(provider, "flush", None)
                if callable(flush):
                    flush()
            result_config = dict(result.get("config") or {})
            result_config["history_mode"] = history_mode
            result["config"] = result_config
            result["run_settings"] = run_settings
            self._last_strategy_bt_result = result
            self.after(0, self._handle_strategy_backtest_success, result)
        except StrategyBacktestCancelled:
            self.after(0, lambda: self.v_st_status.set("⏹ 策略回测已取消"))
        except Exception as exc:
            self.after(0, lambda exc=exc: self.v_st_status.set(f"❌ 策略回测失败: {exc}"))
            self.after(0, lambda exc=exc: messagebox.showerror("策略回测失败", str(exc)))
        finally:
            self.after(0, self._finish_strategy_backtest)

    @staticmethod
    def _strategy_stage_progress_pct(stage, done, total, period_idx, total_periods) -> float:
        if total_periods <= 0:
            return 0.0
        phase = {
            "准入筛选": (0.00, 0.28),
            "价格预筛": (0.28, 0.12),
            "定价": (0.40, 0.34),
            "持仓估值": (0.74, 0.12),
            "基准估值": (0.86, 0.14),
        }.get(stage, (0.0, 0.0))
        inner = (done / total) if total else 1.0
        pct = (period_idx + phase[0] + phase[1] * inner) / total_periods
        return max(0.0, min(1.0, pct))

    def _finish_strategy_backtest(self):
        self._strategy_bt_running = False
        self.btn_strategy_backtest.configure(text="运行策略", command=self._run_strategy_backtest)
        if getattr(self, "_last_strategy_bt_result", None):
            self.btn_strategy_bt_csv.configure(state="normal")

    def _handle_strategy_backtest_success(self, result):
        self._last_strategy_bt_result = result
        snapshot_info = None
        snapshot_error = None
        try:
            snapshot_info = self._save_strategy_backtest_snapshot()
            if snapshot_info:
                result["_snapshot_id"] = snapshot_info.get("snapshot_id")
                result["_snapshot_path"] = str(snapshot_info.get("path"))
        except Exception as exc:
            snapshot_error = exc
        self._record_strategy_comparison_result(result)
        self._render_strategy_backtest_result(result)
        if snapshot_error is not None:
            self.v_st_status.set(f"策略回测完成 · 快照保存失败: {snapshot_error}")
        elif snapshot_info:
            self.v_st_status.set(f"策略回测完成 · 快照已保存: {snapshot_info['path'].name}")

    def _build_strategy_provider(self, source):
        raw_mode = self.v_st_history_mode.get() if hasattr(self, "v_st_history_mode") else "标准"
        mode = normalize_strategy_history_mode(raw_mode)
        if mode == "Wind高保真":
            # 高保真 = 条款逐日从 Wind 历史 tradeDate 拉取; 但日级状态 (停牌/强赎/
            # 摘牌/正股ST/评级/成交额) 不再逐日 wss/wsd 拉 (每债 ~29 次 Wind 调用),
            # 而是 strip 掉 Wind 泄漏的当前状态、改由 cb_events 按 event_date 重建。
            # 这些状态本就是离散公告事件 (cb_events 已覆盖 18 类), 事件重建既防未来
            # 函数又把每债 Wind 调用压到 ~2 次 (条款批量 wss + close wsd)。
            provider = HistoricalBondDataProvider(
                WindDataProvider(),
                history_store=None,
                patch_store=TermsPatchStore(project_terms_patches_path()),
                event_store=CBEventStore(project_events_path()),
                strip_fallback_status=True,
                merge_admission_status=False,
                provider_history_terms=True,
            )
            # 跨运行磁盘缓存 (与 CLI --cache-dir 同机制): 高保真逐债拉取的 point-in-time
            # 条款/历史价落盘复用, 复跑从数小时降到定价时间; 补丁/事件文件一变自动失效。
            return DiskCacheProvider(provider, data_dir("strategy_backtest_cache"))

        base_provider = build_batch_provider(
            source,
            terms_cache=getattr(self, "terms_cache", None),
            csv_root=getattr(self, "_csv_root", None) or None,
            max_age_days=30,
        )
        return HistoricalBondDataProvider(
            base_provider,
            history_store=None,
            patch_store=TermsPatchStore(project_terms_patches_path()),
            event_store=CBEventStore(project_events_path()),
            strip_fallback_status=False,
            merge_admission_status=True,
        )

    @staticmethod
    def _optional_float(var):
        raw = var.get().strip()
        return float(raw) if raw else None

    @staticmethod
    def _optional_pct(var):
        raw = var.get().strip()
        return float(raw) / 100.0 if raw else None

    @staticmethod
    def _strategy_run_settings(
        *,
        codes,
        start,
        end,
        source,
        history_mode,
        gui_pool_mode,
        engine_pool_mode,
        config,
        admission_config,
        params,
        precheck,
    ):
        return {
            "data_source": source,
            "start_date": start,
            "end_date": end,
            "history_mode": history_mode,
            "pool": {
                "gui_mode": gui_pool_mode,
                "engine_mode": engine_pool_mode,
                "code_count": len(codes),
                "bond_codes": list(codes),
            },
            "strategy": {
                "selection_view": config.selection_view,
                "rebalance_freq": config.rebalance_freq,
                "top_n": config.top_n,
                "holding_mode": config.holding_mode,
                "max_holdings": config.max_holdings,
                "funding_mode": config.funding_mode,
                "cash_yield_rate": config.cash_yield_rate,
                "exposure_mode": config.exposure_mode,
                "top_n_shortfall_policy": _funding_legacy_alias(config.funding_mode),
                "min_score": config.min_score,
                "min_confidence": list(config.min_confidence) if config.min_confidence else None,
                "exclude_risk_tags": list(config.exclude_risk_tags),
                "min_market_price": config.min_market_price,
                "max_market_price": config.max_market_price,
                "min_conversion_premium": config.min_conversion_premium,
                "max_conversion_premium": config.max_conversion_premium,
                "min_deviation": config.min_deviation,
                "max_deviation": config.max_deviation,
                "min_sigma": config.min_sigma,
                "max_sigma": config.max_sigma,
                "price_lookback_days": config.price_lookback_days,
                "max_price_staleness_days": config.max_price_staleness_days,
                "execution_timing": config.execution_timing,
                "execution_lookahead_days": config.execution_lookahead_days,
                "mark_to_market": config.mark_to_market,
                "pre_filter_prices": config.pre_filter_prices,
                "transaction_cost": config.transaction_cost,
                "compute_benchmark": config.compute_benchmark,
            },
            "admission_filter": {
                "delist_window_days": admission_config.delist_window_days,
                "min_outstanding_balance": admission_config.min_outstanding_balance,
                "min_credit_rating": admission_config.min_credit_rating,
                "min_turnover_amount": admission_config.min_turnover_amount,
            },
            "pricing": dict(params),
            "precheck": {
                "period_count": precheck.get("period_count") if isinstance(precheck, dict) else None,
                "estimated_pricing": precheck.get("estimated_pricing") if isinstance(precheck, dict) else None,
                "estimated_wind_requests": (
                    precheck.get("estimated_wind_requests") if isinstance(precheck, dict) else None
                ),
            },
        }
