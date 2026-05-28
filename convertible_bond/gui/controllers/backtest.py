"""📈 历史回测."""
from __future__ import annotations

import csv
import threading
from collections import Counter
from datetime import date
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

from ...backtest import backtest_theoretical_price
from ...batch_pricing import (
    AdmissionFilterConfig,
    DEFAULT_MIN_CREDIT_RATING,
    DEFAULT_MIN_OUTSTANDING_BALANCE,
    build_batch_provider,
    parse_bond_codes,
)
from ...cb_events import CBEventStore, project_events_path
from ...data_providers import WindDataProvider
from ...historical_terms import (
    HistoricalBondDataProvider,
    TermsPatchStore,
    project_terms_patches_path,
)
from ...strategy_backtest import (
    ScoreStrategyConfig,
    backtest_score_strategy,
    build_rebalance_schedule,
    write_strategy_backtest_csv,
)
from ..theme import (
    ACCENT, BG_CARD, BG_INPUT, BORDER,
    GREEN, ORANGE, RED,
    TEXT, TEXT_DIM,
    FONT_FAMILY,
    VOL_WINDOW_MAP,
    get_color,
)
from ..constants import (
    BOND_CODE_RE,
    STRATEGY_TEMPLATE_DESCRIPTIONS,
    STRATEGY_VIEW_DESCRIPTIONS,
    normalize_strategy_history_mode,
)
from ..tabs.batch_common import (
    _TREE_ATTRS,
    _attach_column_sort,
    _configure_responsive_columns,
    _configure_tree_style,
)
from ..widgets import Tooltip


STRATEGY_BACKTEST_PRO_FEATURE = "strategy_backtest"
STRATEGY_BACKTEST_PRO_PREVIEW = True


class StrategyBacktestCancelled(Exception):
    """用户主动中断策略回测."""


# 选债哲学由"视图"统一驱动: 置信度与硬复核风险按视图推导, 不再单独暴露控件。
_DEFAULT_VIEW_POLICY = {"min_confidence": ("高", "中"), "exclude_review_risks": True}
STRATEGY_VIEW_POLICY = {
    "综合机会": {"min_confidence": ("高", "中"), "exclude_review_risks": True},
    "低估候选": {"min_confidence": ("高", "中"), "exclude_review_risks": True},
    "转股折价": {"min_confidence": ("高", "中"), "exclude_review_risks": True},
}

# 模板基线: 选模板时先重置这些"选债逻辑"字段 (未指定即清空), 避免上个模板残留;
# 数据源 / 区间 / 代码池 / 文件路径属环境配置, 不在模板范围内。
_STRATEGY_TEMPLATE_BASE = {
    "v_st_freq": "月", "v_st_top_n": "10", "v_st_view": "低估候选",
    "v_st_min_price": "", "v_st_max_price": "",
    "v_st_min_premium": "", "v_st_max_premium": "",
    "v_st_min_deviation": "", "v_st_max_deviation": "",
    "v_st_min_sigma": "", "v_st_max_sigma": "",
    "v_st_min_rating": DEFAULT_MIN_CREDIT_RATING or "",
    "v_st_min_balance": (
        "" if DEFAULT_MIN_OUTSTANDING_BALANCE is None else str(DEFAULT_MIN_OUTSTANDING_BALANCE)
    ),
    "v_st_min_turnover": "", "v_st_delist_window": "0", "v_st_cost": "20",
}
STRATEGY_TEMPLATES = {
    "低估轮动": {"v_st_view": "低估候选", "v_st_freq": "月", "v_st_top_n": "10",
                "v_st_max_premium": "30"},
    "折价套利": {"v_st_view": "转股折价", "v_st_freq": "周", "v_st_top_n": "10",
                "v_st_max_premium": "5"},
    "稳健打底": {"v_st_view": "综合机会", "v_st_freq": "月", "v_st_top_n": "15",
                "v_st_max_price": "120", "v_st_max_premium": "20"},
}

# 策略回测默认 PDE 网格 (原 "快速" 档): 调参体感够用; 出报告时未来可接入"精确重跑"按钮.
_STRATEGY_PDE_GRID_M = 120
_STRATEGY_PDE_GRID_N = 400


class BacktestMixin:
    """回测 tab 的业务逻辑."""

    def _run_backtest(self):
        code = self.v_bond_code.get().strip()
        if not code:
            messagebox.showwarning("提示", "请先输入转债代码")
            return
        try:
            start = date.fromisoformat(self.v_bt_start.get().strip())
            end = date.fromisoformat(self.v_bt_end.get().strip())
        except ValueError:
            messagebox.showerror("错误", "日期格式应为 YYYY-MM-DD")
            return
        if start >= end:
            messagebox.showerror("错误", "开始日期应早于结束日期")
            return

        freq_map = {"日": "D", "周": "W", "月": "M"}
        freq = freq_map.get(self.v_bt_freq.get(), "W")

        try:
            params = dict(
                r=float(self.v_r.get()) / 100.0,
                q=float(self.v_q.get()) / 100.0,
                base_spread=float(self.v_spread.get()) / 100.0,
                p_down=float(self.v_p_down.get()) / 100.0,
                distress_k=float(self.v_dk.get()) / 100.0,
                M=int(float(self.v_M.get())),
                N=int(float(self.v_N.get())),
                vol_window_days=VOL_WINDOW_MAP.get(self.v_vol_window.get(), 21),
                solve_iv=bool(self.v_bt_solve_iv.get()),
                call_notice_days=int(float(self.v_call_notice.get())),
            )
        except ValueError as e:
            messagebox.showerror("错误", f"参数解析失败: {e}")
            return

        self.btn_backtest.configure(state="disabled")
        self.v_bt_status.set(f"正在回测 {code} {start} → {end} ({self.v_bt_freq.get()}频) ...")
        threading.Thread(
            target=self._backtest_worker,
            args=(code, start, end, freq, params),
            daemon=True,
        ).start()

    def _backtest_worker(self, code, start, end, freq, params):
        try:
            provider = self._get_provider()

            def progress(i, total):
                self.after(0, lambda: self.v_bt_status.set(
                    f"进度 {i}/{total} ..."
                ))

            result = backtest_theoretical_price(
                code, start_date=start, end_date=end, freq=freq,
                provider=provider, progress_cb=progress, **params,
            )
            self._last_bt_result = result
            self.after(0, self._render_backtest_chart, result)
        except Exception as exc:
            self.after(0, lambda: self.v_bt_status.set(f"❌ 回测失败: {exc}"))
            self.after(0, lambda: messagebox.showerror("回测失败", str(exc)))
        finally:
            self.after(0, lambda: self.btn_backtest.configure(state="normal"))

    # ── 策略回测 (Pro 预览) ───────────────────────────────
    def _apply_strategy_template(self, name):
        """套用策略模板; 选「自定义」不改动现有参数, 仅保留手动调整。"""
        overrides = STRATEGY_TEMPLATES.get(name)
        if overrides is None:  # 自定义
            view = self.v_st_view.get()
            desc = STRATEGY_VIEW_DESCRIPTIONS.get(view, "可手动调整选债和过滤条件")
            self.v_st_status.set(f"自定义模式 · 当前视图「{view}」: {desc}")
            return
        merged = {**_STRATEGY_TEMPLATE_BASE, **overrides}
        self._programmatic_update = True
        try:
            for var_name, value in merged.items():
                var = getattr(self, var_name, None)
                if var is not None:
                    var.set(value)
        finally:
            self._programmatic_update = False
        view = merged.get("v_st_view", self.v_st_view.get())
        template_desc = STRATEGY_TEMPLATE_DESCRIPTIONS.get(name, "")
        view_desc = STRATEGY_VIEW_DESCRIPTIONS.get(view, "")
        self.v_st_status.set(f"已套用「{name}」: {template_desc} · {view_desc}")

    def _describe_strategy_view(self, name):
        """用户切换选债视图时, 直接解释这个视图代表的选债哲学。"""
        desc = STRATEGY_VIEW_DESCRIPTIONS.get(name)
        if desc:
            self.v_st_status.set(f"选债视图「{name}」: {desc}")

    def _strategy_codes_from_pool(self) -> tuple[list[str], str]:
        mode = self.v_st_pool_mode.get() if hasattr(self, "v_st_pool_mode") else "本地全市场"
        if mode == "当前筛选结果":
            rows = list(getattr(self, "_batch_results", []) or [])
            codes = self._dedupe_strategy_codes(row.get("bond_code") for row in rows)
            return codes, "批量页当前筛选结果"
        if mode == "自选代码":
            codes, invalid = self._parse_strategy_manual_codes()
            label = f"自选代码池"
            if invalid:
                label += f" (忽略无效 {len(invalid)} 个)"
            return codes, label

        cache = getattr(self, "terms_cache", None)
        codes = list(cache.list_bonds()) if cache is not None else []
        return self._dedupe_strategy_codes(codes), "本地条款库"

    @staticmethod
    def _dedupe_strategy_codes(codes) -> list[str]:
        out: list[str] = []
        seen = set()
        for code in codes or []:
            code = str(code or "").strip().upper()
            if not code or code in seen:
                continue
            seen.add(code)
            out.append(code)
        return out

    def _parse_strategy_manual_codes(self) -> tuple[list[str], list[str]]:
        raw_codes = parse_bond_codes(self.v_st_codes.get())
        valid = [code for code in raw_codes if BOND_CODE_RE.match(code)]
        invalid = [code for code in raw_codes if code not in valid]
        return self._dedupe_strategy_codes(valid), invalid

    def _strategy_pool_preview_text(self) -> str:
        mode = self.v_st_pool_mode.get() if hasattr(self, "v_st_pool_mode") else "本地全市场"
        try:
            codes, label = self._strategy_codes_from_pool()
            invalid_text = ""
            if mode == "自选代码":
                _, invalid = self._parse_strategy_manual_codes()
                invalid_text = f" · 无效 {len(invalid)} 个" if invalid else " · 无效 0 个"
            if mode == "当前筛选结果" and not codes:
                return "批量筛选结果为空, 请先到批量页刷新"
            return f"{label} · 已选择 {len(codes)} 只{invalid_text}"
        except Exception as exc:
            return f"读取失败: {exc}"

    def _strategy_history_preview_text(self) -> str:
        raw_mode = self.v_st_history_mode.get() if hasattr(self, "v_st_history_mode") else "标准"
        mode = normalize_strategy_history_mode(raw_mode)
        try:
            patch = self._strategy_patch_precheck()
            events = self._strategy_events_precheck()
            if mode == "Wind高保真":
                history = self._strategy_history_precheck([])
                return (
                    f"条款来源 {history['label']} · "
                    f"公告修补 {patch['count']} 条 / {events['count']} 条事件"
                )
            return f"默认修正 {patch['count']} 条 · 公告事件 {events['count']} 条"
        except Exception as exc:
            return f"读取失败: {exc}"

    def _refresh_strategy_setup_summary(self, *_):
        pool_var = getattr(self, "v_st_pool_summary", None)
        if pool_var is not None:
            pool_var.set(self._strategy_pool_preview_text())
        history_var = getattr(self, "v_st_history_summary", None)
        if history_var is not None:
            history_var.set(self._strategy_history_preview_text())

    def _clear_strategy_codes(self):
        self.v_st_codes.set("")
        if hasattr(self, "v_st_pool_mode"):
            self.v_st_pool_mode.set("自选代码")
        self.v_st_status.set("已清空自选代码池")

    def _import_strategy_codes_file(self):
        path = filedialog.askopenfilename(
            title="导入自选转债代码",
            filetypes=[("CSV / TXT", "*.csv *.txt"), ("所有文件", "*.*")],
        )
        if not path:
            return
        try:
            try:
                with open(path, "r", encoding="utf-8-sig", newline="") as f:
                    text = f.read()
            except UnicodeDecodeError:
                with open(path, "r", encoding="gb18030", newline="") as f:
                    text = f.read()
            codes = parse_bond_codes(text)
            self.v_st_codes.set("\n".join(codes))
            if hasattr(self, "v_st_pool_mode"):
                self.v_st_pool_mode.set("自选代码")
            valid, invalid = self._parse_strategy_manual_codes()
            self.v_st_status.set(f"已导入 {len(valid)} 个有效代码, 忽略 {len(invalid)} 个无效项")
        except Exception as exc:
            messagebox.showerror("导入失败", str(exc))

    def _precheck_strategy_backtest(self):
        """轻量检查策略回测输入、历史口径和预计工作量, 不触发批量定价。"""
        try:
            info = self._strategy_precheck_info()
        except Exception as exc:
            self.v_st_precheck.set(f"预检失败: {exc}")
            messagebox.showerror("策略预检失败", str(exc))
            return
        text = self._format_strategy_precheck(info)
        self.v_st_precheck.set(text)
        self.v_st_status.set(f"预检完成 · {info['code_count']} 只 · {info['period_count']} 个调仓区间")

    def _strategy_precheck_info(self) -> dict:
        start = date.fromisoformat(self.v_st_start.get().strip())
        end = date.fromisoformat(self.v_st_end.get().strip())
        if start >= end:
            raise ValueError("开始日期应早于结束日期")

        codes, pool_label = self._strategy_codes_from_pool()
        if not codes:
            if getattr(self, "v_st_pool_mode", None) is not None and self.v_st_pool_mode.get() == "当前筛选结果":
                raise ValueError("当前批量筛选结果为空, 请先到批量页刷新重算或切换视图")
            if getattr(self, "v_st_pool_mode", None) is not None and self.v_st_pool_mode.get() == "自选代码":
                raise ValueError("自选代码池为空, 请粘贴或导入转债代码")
            raise ValueError("代码池为空, 请先同步条款库或输入转债代码")

        freq_map = {"周": "W", "月": "M", "季": "Q"}
        freq = freq_map.get(self.v_st_freq.get(), "M")
        schedule = build_rebalance_schedule(start, end, freq)
        period_count = max(0, len(schedule) - 1)
        top_n = max(1, int(float(self.v_st_top_n.get())))
        estimated_pricing = len(codes) * period_count

        raw_mode = self.v_st_history_mode.get() if hasattr(self, "v_st_history_mode") else "标准"
        mode = normalize_strategy_history_mode(raw_mode)
        history = self._strategy_history_precheck(schedule[:-1])
        patch = self._strategy_patch_precheck()
        events = self._strategy_events_precheck()
        warnings = []
        if mode == "Wind高保真" and not history["enabled"]:
            warnings.append("Wind 历史条款未启用, 过去条款会回退到当前条款视角")
        if top_n > len(codes):
            warnings.append("TopN 大于代码池数量")
        # 历史条款修正覆盖度检查。
        if patch["count"] > 0 and patch.get("earliest"):
            if patch["earliest"] > start:
                warnings.append(f"历史转股价修正最早日期 {patch['earliest']} 晚于回测起始 {start}")
        else:
            warnings.append("无历史转股价修正, 部分时期可能使用当前转股价")

        return {
            "start": start,
            "end": end,
            "pool_label": pool_label,
            "pool_mode": self.v_st_pool_mode.get() if hasattr(self, "v_st_pool_mode") else "本地全市场",
            "history_mode": mode,
            "code_count": len(codes),
            "period_count": period_count,
            "top_n": top_n,
            "grid_M": _STRATEGY_PDE_GRID_M,
            "grid_N": _STRATEGY_PDE_GRID_N,
            "estimated_pricing": estimated_pricing,
            "history": history,
            "patch": patch,
            "events": events,
            "warnings": warnings,
        }

    def _strategy_history_precheck(self, rebalance_dates) -> dict:
        raw_mode = self.v_st_history_mode.get() if hasattr(self, "v_st_history_mode") else "标准"
        mode = normalize_strategy_history_mode(raw_mode)
        if mode == "Wind高保真":
            return {
                "enabled": True,
                "label": "实时 Wind tradeDate 历史截面",
                "snapshot_count": 0,
                "coverage_ratio": 1.0,
            }
        return {
            "enabled": False,
            "label": "标准模式不使用历史快照",
            "snapshot_count": 0,
            "coverage_ratio": 0.0,
        }

    def _strategy_patch_precheck(self) -> dict:
        path = project_terms_patches_path()
        try:
            store = TermsPatchStore(path)
            patches = store.list_patches()
            count = len(patches)
            if patches:
                dates = [p.effective_date for p in patches]
                earliest = min(dates)
                latest = max(dates)
                bond_codes_with_patches = len(set(p.bond_code for p in patches))
            else:
                earliest = latest = None
                bond_codes_with_patches = 0
        except Exception as exc:
            return {"path": path, "count": 0, "label": f"读取失败: {exc}",
                    "earliest": None, "latest": None, "bonds_with_patches": 0}
        exists = path.exists()
        return {
            "path": path, "count": count,
            "label": f"默认{'已启用' if exists else '未找到'} · {count} 条",
            "earliest": earliest, "latest": latest,
            "bonds_with_patches": bond_codes_with_patches,
        }

    def _strategy_events_precheck(self) -> dict:
        path = project_events_path()
        try:
            count = len(CBEventStore(path).list_events())
        except Exception as exc:
            return {"path": path, "count": 0, "label": f"读取失败: {exc}"}
        exists = path.exists()
        return {"path": path, "count": count, "label": f"默认{'已启用' if exists else '未找到'} · {count} 条"}

    @staticmethod
    def _format_strategy_precheck(info: dict) -> str:
        history = info["history"]
        warnings = info.get("warnings") or []
        patch_info = f"{info['patch']['count']} 条"
        if info['patch'].get('earliest') and info['patch'].get('latest'):
            patch_info += f" ({info['patch']['earliest']}~{info['patch']['latest']})"
        warning_text = "; ".join(warnings[:3]) if warnings else "未发现明显口径问题"
        return "\n".join((
            f"规模: {info['pool_label']} {info['code_count']} 只 · "
            f"{info['period_count']} 期 · Top{info['top_n']} · "
            f"预计定价≈{info['estimated_pricing']} 次 · PDE M{info['grid_M']}/N{info['grid_N']}",
            f"数据: {info.get('history_mode', '标准')}口径 · 条款来源 {history['label']} · 覆盖 {history['coverage_ratio']*100:.0f}% · "
            f"历史转股价修正 {patch_info} · 公告事件 {info['events']['count']} 条",
            f"提醒: {warning_text}",
        ))

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
        try:
            config = ScoreStrategyConfig(
                top_n=max(1, int(float(self.v_st_top_n.get()))),
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

        try:
            precheck = self._strategy_precheck_info()
            self.v_st_precheck.set(self._format_strategy_precheck(precheck))
            self._strategy_bt_expected_pricing = precheck.get("estimated_pricing")
        except Exception:
            self._strategy_bt_expected_pricing = None

        source = self.v_data_source.get()
        self._strategy_bt_cancel = threading.Event()
        self._strategy_bt_running = True
        self.btn_strategy_backtest.configure(text="停止", command=self._cancel_strategy_backtest)
        self.btn_strategy_bt_csv.configure(state="disabled")
        if hasattr(self, "strategy_bt_progress"):
            self.strategy_bt_progress.set(0)
        self.v_st_status.set(
            f"Pro 预览 · 正在回测 {len(codes)} 只, "
            f"{start} → {end}, {self.v_st_freq.get()}调仓 ..."
        )
        threading.Thread(
            target=self._strategy_backtest_worker,
            args=(codes, start, end, source, config, admission_config, params),
            daemon=True,
        ).start()

    def _cancel_strategy_backtest(self):
        if self._strategy_bt_cancel is not None:
            self._strategy_bt_cancel.set()
        self.v_st_status.set("⏹ 正在停止 (完成当前调仓后中断) ...")

    def _strategy_backtest_pro_available(self) -> bool:
        """未来接授权时只需替换这里的判断."""
        return bool(
            STRATEGY_BACKTEST_PRO_PREVIEW
            or getattr(self, "pro_license_active", False)
            or getattr(self, "_pro_features", {}).get(STRATEGY_BACKTEST_PRO_FEATURE)
        )

    def _strategy_backtest_worker(self, codes, start, end, source, config, admission_config, params):
        try:
            provider = self._build_strategy_provider(source)

            def progress(done, total):
                if self._strategy_bt_cancel is not None and self._strategy_bt_cancel.is_set():
                    raise StrategyBacktestCancelled()

                def _update():
                    pct = done / total if total else 0
                    expected = getattr(self, "_strategy_bt_expected_pricing", None)
                    suffix = f" · 预计定价≈{expected} 次" if expected else ""
                    self.v_st_status.set(
                        f"Pro 预览 · 定价/选债/估值 {done}/{total} ({pct:.0%}){suffix}"
                    )
                    if hasattr(self, "strategy_bt_progress"):
                        self.strategy_bt_progress.set(pct)
                self.after(0, _update)

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
                **params,
            )
            self._last_strategy_bt_result = result
            self.after(0, self._handle_strategy_backtest_success, result)
        except StrategyBacktestCancelled:
            self.after(0, lambda: self.v_st_status.set("⏹ 策略回测已取消"))
        except Exception as exc:
            self.after(0, lambda exc=exc: self.v_st_status.set(f"❌ 策略回测失败: {exc}"))
            self.after(0, lambda exc=exc: messagebox.showerror("策略回测失败", str(exc)))
        finally:
            self.after(0, self._finish_strategy_backtest)

    def _finish_strategy_backtest(self):
        self._strategy_bt_running = False
        self.btn_strategy_backtest.configure(text="运行策略", command=self._run_strategy_backtest)
        if getattr(self, "_last_strategy_bt_result", None):
            self.btn_strategy_bt_csv.configure(state="normal")

    def _handle_strategy_backtest_success(self, result):
        self._last_strategy_bt_result = result
        self._record_strategy_comparison_result(result)
        self._render_strategy_backtest_result(result)

    def _build_strategy_provider(self, source):
        raw_mode = self.v_st_history_mode.get() if hasattr(self, "v_st_history_mode") else "标准"
        mode = normalize_strategy_history_mode(raw_mode)
        if mode == "Wind高保真":
            return HistoricalBondDataProvider(
                WindDataProvider(),
                history_store=None,
                patch_store=TermsPatchStore(project_terms_patches_path()),
                event_store=CBEventStore(project_events_path()),
                strip_fallback_status=False,
                merge_admission_status=True,
            )

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
        )

    @staticmethod
    def _optional_float(var):
        raw = var.get().strip()
        return float(raw) if raw else None

    @staticmethod
    def _optional_pct(var):
        raw = var.get().strip()
        return float(raw) / 100.0 if raw else None

    def _render_strategy_backtest_result(self, result):
        summary = result.get("summary", {})
        self._update_strategy_stats(summary)
        self._render_strategy_insight(result)
        self._render_strategy_chart(result)
        self._render_strategy_selection_panel(result)
        self._render_strategy_table(result)
        self._render_strategy_attribution(result)
        self._render_strategy_risk_panel(result)
        self._render_strategy_robustness_panel(result)
        self._render_strategy_data_panel(result)
        self._render_strategy_comparison()

        periods = result.get("periods", [])
        excess = summary.get("excess_return")
        extra = (f" · 超额 {excess*100:+.2f}%"
                 if excess is not None and np.isfinite(excess) else "")
        warnings = ((result.get("diagnostics") or {}).get("warnings") or [])
        warning_text = f" · 提醒: {warnings[0]}" if warnings else ""
        perf = ((result.get("diagnostics") or {}).get("performance") or {})
        perf_text = ""
        if perf:
            hits = int(perf.get("pricing_snapshot_hits") or 0)
            prefiltered = int(perf.get("price_prefilter_excluded") or 0)
            if hits or prefiltered:
                perf_text = f" · 缓存命中 {hits} / 预筛 {prefiltered}"
        self.v_st_status.set(
            f"✅ Pro 预览 · {len(periods)} 个调仓区间 · "
            f"最终净值 {summary.get('final_equity', 1.0):.4f}{extra}{perf_text}{warning_text}"
        )
        if hasattr(self, "strategy_bt_progress"):
            self.strategy_bt_progress.set(1.0)
        self.btn_strategy_bt_csv.configure(state="normal")

    def _update_strategy_stats(self, summary):
        stats = getattr(self, "_strategy_stat_vars", None)
        if not stats:
            return
        labels = getattr(self, "_strategy_stat_labels", {})
        green, red, base = get_color(GREEN), get_color(RED), get_color(TEXT)

        def pct(value, sign=False):
            if value is None or not np.isfinite(value):
                return "—"
            return f"{value*100:+.2f}%" if sign else f"{value*100:.2f}%"

        def colorize(key, value):
            lbl = labels.get(key)
            if lbl is None:
                return
            if value is None or not np.isfinite(value) or value == 0:
                lbl.configure(text_color=base)
            else:
                lbl.configure(text_color=green if value > 0 else red)

        final_equity = summary.get("final_equity")
        stats["final_equity"].set(f"{float(final_equity):.4f}" if final_equity is not None else "—")
        total_return = summary.get("total_return")
        annualized = summary.get("annualized_return")
        excess = summary.get("excess_return")
        sharpe = summary.get("sharpe")
        sortino = summary.get("sortino")
        calmar = summary.get("calmar")
        stats["total_return"].set(pct(total_return, sign=True))
        stats["annualized"].set(pct(annualized, sign=True))
        stats["excess"].set(pct(excess, sign=True))
        stats["max_drawdown"].set(pct(summary.get("max_drawdown")))
        stats["sharpe"].set(
            f"{sharpe:.2f}" if sharpe is not None and np.isfinite(sharpe) else "—")
        if "sortino" in stats:
            stats["sortino"].set(
                f"{sortino:.2f}" if sortino is not None and np.isfinite(sortino) else "—")
        if "calmar" in stats:
            stats["calmar"].set(
                f"{calmar:.2f}" if calmar is not None and np.isfinite(calmar) else "—")
        if "hit_rate" in stats:
            stats["hit_rate"].set(pct(summary.get("hit_rate")))
        if "cash" in stats:
            stats["cash"].set(pct(summary.get("avg_cash_weight")))
        stats["turnover"].set(pct(summary.get("avg_turnover")))
        colorize("total_return", total_return)
        colorize("annualized", annualized)
        colorize("excess", excess)
        colorize("sharpe", sharpe)
        colorize("sortino", sortino)
        colorize("calmar", calmar)

    def _render_strategy_insight(self, result):
        frame = getattr(self, "strategy_bt_insight_frame", None)
        if frame is None:
            return
        for child in frame.winfo_children():
            child.destroy()

        summary = result.get("summary") or {}
        diagnostics = result.get("diagnostics") or {}
        attribution = diagnostics.get("attribution") or {}
        data_quality = diagnostics.get("data_quality") or {}

        total_return = summary.get("total_return")
        excess = summary.get("excess_return")
        max_drawdown = summary.get("max_drawdown")
        fallback_ratio = float(data_quality.get("current_fallback_ratio") or 0.0)
        top_contrib = (attribution.get("top_contributors") or [{}])[0]
        top_name = top_contrib.get("bond_name") or top_contrib.get("bond_code") or "—"

        if total_return is not None and np.isfinite(total_return):
            if total_return > 0 and (excess is None or excess >= 0):
                verdict = "收益与基准对比均偏正"
            elif total_return > 0:
                verdict = "绝对收益为正, 但弱于基准"
            else:
                verdict = "策略区间收益为负"
        else:
            verdict = "暂无足够收益数据"
        quality = "高" if fallback_ratio <= 0 else ("中" if fallback_ratio <= 0.2 else "低")

        row = ctk.CTkFrame(frame, fg_color="transparent")
        row.grid(row=0, column=0, sticky="nsew", padx=12, pady=(8, 2))
        row.grid_rowconfigure(0, weight=1)
        for col in range(4):
            row.grid_columnconfigure(col, weight=1)
        items = [
            ("结论", verdict),
            ("最大回撤", (
                f"{self._fmt_strategy_pct(max_drawdown)} · "
                f"{summary.get('max_drawdown_start') or '—'} → {summary.get('max_drawdown_end') or '—'}"
            )),
            ("主要贡献", (
                f"{top_name} {self._fmt_strategy_pct(top_contrib.get('contribution'), sign=True)}"
            )),
            ("可信度", f"{quality} · 当前回退 {self._fmt_strategy_pct(fallback_ratio)}"),
        ]
        for col, (title, value) in enumerate(items):
            cell = ctk.CTkFrame(row, fg_color=BG_INPUT, corner_radius=8)
            cell.grid(row=0, column=col, sticky="nsew", padx=6, pady=4)
            
            inner = ctk.CTkFrame(cell, fg_color="transparent")
            inner.pack(fill="both", expand=True, padx=12, pady=8)
            
            ctk.CTkLabel(inner, text=title, text_color=TEXT_DIM,
                         font=(FONT_FAMILY, 11)).pack(anchor="w")
            ctk.CTkLabel(inner, text=value, text_color=TEXT,
                         font=(FONT_FAMILY, 13, "bold"), wraplength=240,
                         justify="left").pack(anchor="w")

    def _render_strategy_chart(self, result):
        if self._strategy_bt_figure is not None:
            self._strategy_bt_figure.clf()
            plt.close(self._strategy_bt_figure)
            self._strategy_bt_figure = None
            self._strategy_bt_canvas = None

        for child in self.strategy_bt_chart_frame.winfo_children():
            child.destroy()

        curve = result.get("equity_curve") or []
        periods = result.get("periods") or []
        if not curve:
            return

        dates = [p["date"] for p in curve]
        equity = [float(p["equity"]) for p in curve]
        ret_dates = [p["end_date"] for p in periods]
        returns = [float(p.get("period_return") or 0.0) * 100.0 for p in periods]

        benchmark_curve = result.get("benchmark_curve") or []
        bench_dates = [p["date"] for p in benchmark_curve]
        bench_equity = [float(p["equity"]) for p in benchmark_curve]

        bg_card_color = get_color(BG_CARD)
        bg_input_color = get_color(BG_INPUT)
        text_dim_color = get_color(TEXT_DIM)
        text_color = get_color(TEXT)
        border_color = get_color(BORDER)
        accent_color = get_color(ACCENT)
        orange_color = get_color(ORANGE)
        green_color = get_color(GREEN)
        red_color = get_color(RED)

        fig = Figure(figsize=(11, 6.6), dpi=100, facecolor=bg_card_color)
        gs = fig.add_gridspec(3, 2, height_ratios=[1.25, 0.72, 0.95], width_ratios=[2.2, 1.0])
        ax_eq = fig.add_subplot(gs[0, :], facecolor=bg_input_color)
        ax_dd = fig.add_subplot(gs[1, :], facecolor=bg_input_color, sharex=ax_eq)
        ax_ret = fig.add_subplot(gs[2, 0], facecolor=bg_input_color)
        ax_hold = fig.add_subplot(gs[2, 1], facecolor=bg_input_color)

        # 净值: 策略 vs 等权基准
        ax_eq.plot(dates, equity, color=accent_color, linewidth=2.2, marker="o",
                   markersize=4, label="组合净值")
        if bench_equity:
            ax_eq.plot(bench_dates, bench_equity, color=orange_color, linewidth=1.6,
                       linestyle="--", marker="s", markersize=3, label="等权基准")
        ax_eq.axhline(1.0, color=border_color, linewidth=1.0, linestyle="--")
        ax_eq.set_ylabel("净值", color=text_dim_color, fontsize=10)
        ax_eq.tick_params(colors=text_dim_color, labelsize=9, labelbottom=False)
        ax_eq.grid(True, color=border_color, linestyle="--", alpha=0.4)
        for spine in ax_eq.spines.values():
            spine.set_color(border_color)
        leg = ax_eq.legend(loc="best", framealpha=0.9, facecolor=bg_card_color,
                           edgecolor=border_color, fontsize=9, labelcolor=text_color)
        leg.get_frame().set_linewidth(0.5)

        # 回撤: 投资者比净值更需要知道“熬了多久、跌了多深”
        dd_values = self._strategy_drawdown_values(equity)
        ax_dd.fill_between(dates, dd_values, 0.0, color=red_color, alpha=0.18)
        ax_dd.plot(dates, dd_values, color=red_color, linewidth=1.4)
        ax_dd.axhline(0.0, color=border_color, linewidth=1.0)
        ax_dd.set_ylabel("回撤 (%)", color=text_dim_color, fontsize=10)
        ax_dd.tick_params(colors=text_dim_color, labelsize=9, labelbottom=False)
        ax_dd.grid(True, color=border_color, linestyle="--", alpha=0.35)
        for spine in ax_dd.spines.values():
            spine.set_color(border_color)

        # 区间收益柱; 柱宽按调仓间隔自适应, 避免周/季频下重叠或过细
        bar_width = 8.0
        if len(ret_dates) >= 2:
            spacings = [(ret_dates[i + 1] - ret_dates[i]).days for i in range(len(ret_dates) - 1)]
            spacings = [s for s in spacings if s > 0]
            if spacings:
                bar_width = max(2.0, 0.7 * min(spacings))
        colors = [green_color if r >= 0 else red_color for r in returns]
        ax_ret.bar(ret_dates, returns, color=colors, alpha=0.72, width=bar_width)
        ax_ret.axhline(0.0, color=border_color, linewidth=1.0)
        ax_ret.set_ylabel("区间收益 (%)", color=text_dim_color, fontsize=10)
        ax_ret.set_xlabel("日期", color=text_dim_color, fontsize=10)
        ax_ret.tick_params(colors=text_dim_color, labelsize=9)
        ax_ret.grid(True, color=border_color, linestyle="--", alpha=0.35)
        for spine in ax_ret.spines.values():
            spine.set_color(border_color)
        for lbl in ax_ret.get_xticklabels():
            lbl.set_rotation(20)
            lbl.set_horizontalalignment("right")

        # 持仓频次: 策略最常入选的标的
        self._render_holdings_frequency(
            ax_hold, periods,
            accent_color=accent_color, text_dim_color=text_dim_color,
            text_color=text_color, border_color=border_color,
        )

        fig.tight_layout()

        canvas = FigureCanvasTkAgg(fig, master=self.strategy_bt_chart_frame)
        canvas.draw()
        canvas.get_tk_widget().grid(row=0, column=0, sticky="nsew", padx=12, pady=12)
        self._strategy_bt_figure = fig
        self._strategy_bt_canvas = canvas

    @staticmethod
    def _strategy_drawdown_values(equity_values):
        peak = -np.inf
        out = []
        for value in equity_values:
            peak = max(peak, value)
            out.append((value / peak - 1.0) * 100.0 if peak > 0 else 0.0)
        return out

    def _render_holdings_frequency(self, ax, periods, *, accent_color,
                                   text_dim_color, text_color, border_color, top_k=10):
        """画"最常入选标的"横向条形图, 让选债结果可视化."""
        counts: Counter = Counter()
        name_map: dict[str, str] = {}
        for period in periods:
            for code in (period.get("selected_codes") or []):
                counts[str(code)] += 1
            for pos in (period.get("positions") or []):
                code = pos.get("bond_code")
                if code:
                    name_map[str(code)] = pos.get("bond_name") or str(code)

        for spine in ax.spines.values():
            spine.set_color(border_color)
        ax.tick_params(colors=text_dim_color, labelsize=8)
        ax.set_title("最常入选", color=text_dim_color, fontsize=10)
        if not counts:
            ax.text(0.5, 0.5, "无持仓", ha="center", va="center",
                    color=text_dim_color, fontsize=10, transform=ax.transAxes)
            ax.set_xticks([])
            ax.set_yticks([])
            return

        top = counts.most_common(top_k)[::-1]  # 高频在上
        values = [n for _, n in top]
        labels = []
        for code, _ in top:
            name = name_map.get(code) or code
            labels.append(name[:6])
        positions = list(range(len(top)))
        ax.barh(positions, values, color=accent_color, alpha=0.82, height=0.7)
        ax.set_yticks(positions)
        ax.set_yticklabels(labels, fontsize=8, color=text_color)
        ax.set_xlabel("入选期数", color=text_dim_color, fontsize=9)
        ax.grid(True, axis="x", color=border_color, linestyle="--", alpha=0.3)
        max_v = max(values)
        ax.set_xticks(range(0, max_v + 1, max(1, max_v // 4)))

    def _render_strategy_selection_panel(self, result):
        frame = getattr(self, "strategy_bt_selection_frame", None)
        if frame is None:
            return
        self._clear_strategy_panel(frame)
        periods = result.get("periods") or []
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(1, weight=1)
        frame.grid_rowconfigure(3, weight=1)

        candidate_rows = []
        rejection_rows = []
        for period in periods:
            period_label = f"{period.get('start_date')} → {period.get('end_date')}"
            for row in period.get("candidate_rows") or []:
                candidate_rows.append([
                    period_label,
                    "买入" if row.get("selected") else "候选",
                    row.get("rank", ""),
                    row.get("bond_code", ""),
                    row.get("bond_name", ""),
                    f"{float(row.get('score')):.1f}" if row.get("score") is not None else "—",
                    self._fmt_strategy_price(row.get("market_price")),
                    self._fmt_strategy_pct(row.get("deviation"), sign=True),
                    self._fmt_strategy_pct(row.get("conversion_premium"), sign=True),
                    row.get("confidence", ""),
                    row.get("selection_reason", ""),
                ])
            for row in period.get("rejection_rows") or []:
                rejection_rows.append([
                    period_label,
                    row.get("source", ""),
                    row.get("bond_code", ""),
                    row.get("bond_name", ""),
                    row.get("reason", ""),
                    f"{float(row.get('score')):.1f}" if row.get("score") is not None else "—",
                    self._fmt_strategy_price(row.get("market_price")),
                    self._fmt_strategy_pct(row.get("deviation"), sign=True),
                    self._fmt_strategy_pct(row.get("conversion_premium"), sign=True),
                    row.get("confidence", ""),
                    " / ".join(str(tag) for tag in row.get("risk_tags") or []),
                ])

        self._strategy_section_title(frame, "候选排序 / 买入解释", 0, 0)
        self._render_strategy_small_tree(
            frame, 1, 0,
            ["period", "status", "rank", "code", "name", "score", "price",
             "dev", "premium", "confidence", "reason"],
            ["区间", "状态", "排名", "代码", "名称", "分数", "价格",
             "偏差", "溢价", "置信", "解释"],
            [170, 58, 52, 88, 96, 64, 68, 72, 72, 58, 420],
            candidate_rows,
            xscroll=True,
        )

        self._strategy_section_title(frame, "剔除 / 落选原因", 2, 0)
        self._render_strategy_small_tree(
            frame, 3, 0,
            ["period", "source", "code", "name", "reason", "score", "price",
             "dev", "premium", "confidence", "tags"],
            ["区间", "来源", "代码", "名称", "原因", "分数", "价格",
             "偏差", "溢价", "置信", "标签"],
            [170, 76, 88, 96, 360, 64, 68, 72, 72, 58, 240],
            rejection_rows,
            xscroll=True,
        )

    def _render_strategy_table(self, result):
        for child in self.strategy_bt_table_frame.winfo_children():
            child.destroy()

        periods = result.get("periods") or []
        if not periods:
            ctk.CTkLabel(
                self.strategy_bt_table_frame,
                text="无持仓明细",
                font=(FONT_FAMILY, 13),
                text_color=TEXT_DIM,
            ).grid(row=0, column=0, padx=12, pady=12, sticky="w")
            return

        self.strategy_bt_table_frame.grid_columnconfigure(0, weight=1)
        self.strategy_bt_table_frame.grid_rowconfigure(1, weight=1)
        self.strategy_bt_table_frame.grid_rowconfigure(3, weight=2)

        self._strategy_section_title(self.strategy_bt_table_frame, "调仓流水", 0, 0)
        summary_rows = []
        previous: set[str] = set()
        for period in periods:
            selected = {str(code) for code in period.get("selected_codes") or []}
            buys = selected - previous
            sells = previous - selected
            holds = selected & previous
            benchmark_return = period.get("benchmark_return")
            period_return = period.get("period_return")
            excess = (
                float(period_return) - float(benchmark_return)
                if period_return is not None and benchmark_return is not None else None
            )
            summary_rows.append([
                f"{period.get('start_date')} → {period.get('end_date')}",
                self._fmt_strategy_pct(period_return, sign=True),
                self._fmt_strategy_pct(excess, sign=True),
                period.get("selected_count", 0),
                len(buys),
                len(sells),
                len(holds),
                self._fmt_strategy_pct(period.get("turnover")),
                self._fmt_strategy_pct(period.get("cash_weight")),
                self._strategy_codes_preview(period.get("selected_codes") or []),
            ])
            previous = selected
        self._render_strategy_small_tree(
            self.strategy_bt_table_frame, 1, 0,
            ["period", "return", "excess", "selected", "buy", "sell", "hold", "turnover", "cash", "codes"],
            ["区间", "收益(%)", "超额(%)", "选中", "买入", "卖出", "续持", "换手", "现金", "持仓代码"],
            [170, 78, 78, 58, 58, 58, 58, 72, 72, 260],
            summary_rows,
            xscroll=True,
        )

        detail_rows = []
        for period in periods:
            period_label = f"{period.get('start_date')} → {period.get('end_date')}"
            for pos in period.get("positions") or []:
                detail_rows.append([
                    period_label,
                    "成交",
                    pos.get("rank", ""),
                    pos.get("bond_code", ""),
                    pos.get("bond_name", ""),
                    self._fmt_strategy_pct(pos.get("return_contribution"), sign=True),
                    self._fmt_strategy_pct(pos.get("period_return"), sign=True),
                    f"{float(pos.get('score')):.1f}" if pos.get("score") is not None else "—",
                    pos.get("confidence", ""),
                    f"{pos.get('entry_date', '—')} @ {self._fmt_strategy_price(pos.get('start_price'))}",
                    f"{pos.get('exit_date', '—')} @ {self._fmt_strategy_price(pos.get('end_price'))}",
                    " / ".join(str(tag) for tag in pos.get("risk_tags") or []),
                ])
            for pos in period.get("skipped_positions") or []:
                detail_rows.append([
                    period_label,
                    "跳过",
                    "",
                    pos.get("bond_code", ""),
                    pos.get("bond_name", ""),
                    "—",
                    "—",
                    "—",
                    "",
                    f"{pos.get('entry_date', '—')} @ {self._fmt_strategy_price(pos.get('start_price'))}",
                    f"{pos.get('exit_date', '—')} @ {self._fmt_strategy_price(pos.get('end_price'))}",
                    pos.get("reason", ""),
                ])

        self._strategy_section_title(self.strategy_bt_table_frame, "持仓 / 跳过明细", 2, 0)
        _configure_tree_style()
        columns = ["period", "status", "rank", "code", "name", "contrib", "ret",
                   "score", "confidence", "entry", "exit", "note"]
        headers = ["区间", "状态", "排名", "代码", "名称", "贡献(%)", "收益(%)",
                   "分数", "置信", "买入", "卖出", "标签/原因"]
        widths = [170, 56, 52, 88, 96, 76, 76, 62, 58, 122, 122, 260]
        tree = ttk.Treeview(
            self.strategy_bt_table_frame,
            columns=columns,
            show="headings",
            selectmode="browse",
        )
        y_scroll = ctk.CTkScrollbar(
            self.strategy_bt_table_frame, orientation="vertical", command=tree.yview)
        x_scroll = ctk.CTkScrollbar(
            self.strategy_bt_table_frame, orientation="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        tree.grid(row=3, column=0, sticky="nsew", padx=(8, 0), pady=(0, 0))
        y_scroll.grid(row=3, column=1, sticky="ns", padx=(0, 8), pady=(0, 0))
        x_scroll.grid(row=4, column=0, sticky="ew", padx=(8, 0), pady=(0, 8))

        _configure_responsive_columns(tree, columns, headers, widths)
        _attach_column_sort(tree, columns, headers)
        self._strategy_bt_tree = tree
        _TREE_ATTRS.add("_strategy_bt_tree")

        for idx, values in enumerate(detail_rows):
            tree.insert("", "end", iid=str(idx), values=values)

    def _render_strategy_attribution(self, result):
        frame = self.strategy_bt_attribution_frame
        self._clear_strategy_panel(frame)
        diagnostics = result.get("diagnostics") or {}
        attribution = diagnostics.get("attribution") or {}
        summary = result.get("summary") or {}

        frame.grid_columnconfigure(0, weight=1)
        frame.grid_columnconfigure(1, weight=1)
        frame.grid_rowconfigure(2, weight=1)
        frame.grid_rowconfigure(4, weight=1)

        metrics = ctk.CTkFrame(frame, fg_color="transparent")
        metrics.grid(row=0, column=0, columnspan=2, sticky="ew", padx=10, pady=(8, 4))
        for i in range(4):
            metrics.grid_columnconfigure(i, weight=1)
        self._strategy_metric_tile(metrics, 0, "成本拖累", self._fmt_strategy_pct(attribution.get("cost_drag"), sign=True))
        self._strategy_metric_tile(metrics, 1, "平均现金", self._fmt_strategy_pct(summary.get("avg_cash_weight")))
        self._strategy_metric_tile(metrics, 2, "跳过仓位", str(attribution.get("skipped_positions") or 0))
        self._strategy_metric_tile(metrics, 3, "累计成本", self._fmt_strategy_pct(summary.get("total_cost")))

        self._strategy_section_title(frame, "贡献最大", 1, 0)
        self._strategy_section_title(frame, "拖累最大", 1, 1)
        self._render_strategy_small_tree(
            frame, 2, 0,
            ["code", "name", "contrib", "holds"],
            ["代码", "名称", "贡献(%)", "期数"],
            [92, 110, 80, 54],
            [
                [
                    row.get("bond_code", ""),
                    row.get("bond_name", ""),
                    self._fmt_strategy_pct(row.get("contribution"), sign=True),
                    row.get("holding_periods", ""),
                ]
                for row in attribution.get("top_contributors") or []
            ],
        )
        self._render_strategy_small_tree(
            frame, 2, 1,
            ["code", "name", "contrib", "holds"],
            ["代码", "名称", "贡献(%)", "期数"],
            [92, 110, 80, 54],
            [
                [
                    row.get("bond_code", ""),
                    row.get("bond_name", ""),
                    self._fmt_strategy_pct(row.get("contribution"), sign=True),
                    row.get("holding_periods", ""),
                ]
                for row in attribution.get("top_detractors") or []
            ],
        )

        self._strategy_section_title(frame, "年度收益", 3, 0)
        self._strategy_section_title(frame, "月度收益", 3, 1)
        self._render_strategy_small_tree(
            frame, 4, 0,
            ["period", "return"],
            ["年份", "收益(%)"],
            [90, 90],
            [[row.get("period", ""), self._fmt_strategy_pct(row.get("return"), sign=True)]
             for row in diagnostics.get("yearly_returns") or []],
        )
        monthly_rows = diagnostics.get("monthly_returns") or []
        self._render_strategy_small_tree(
            frame, 4, 1,
            ["period", "return"],
            ["月份", "收益(%)"],
            [90, 90],
            [[row.get("period", ""), self._fmt_strategy_pct(row.get("return"), sign=True)]
             for row in monthly_rows[-24:]],
        )

    def _render_strategy_risk_panel(self, result):
        frame = self.strategy_bt_risk_frame
        self._clear_strategy_panel(frame)
        diagnostics = result.get("diagnostics") or {}
        summary = result.get("summary") or {}
        warnings = diagnostics.get("warnings") or []
        periods = result.get("periods") or []

        frame.grid_columnconfigure(0, weight=1)
        frame.grid_columnconfigure(1, weight=1)
        frame.grid_rowconfigure(2, weight=1)

        left = ctk.CTkFrame(frame, fg_color="transparent")
        left.grid(row=0, column=0, sticky="nsew", padx=12, pady=10)
        ctk.CTkLabel(left, text="风险提示", text_color=TEXT,
                     font=(FONT_FAMILY, 14, "bold")).pack(anchor="w")
        if warnings:
            for warning in warnings:
                ctk.CTkLabel(
                    left, text=f"• {warning}", text_color=ORANGE,
                    font=(FONT_FAMILY, 12), justify="left", wraplength=520,
                ).pack(anchor="w", pady=(6, 0))
        else:
            ctk.CTkLabel(left, text="暂无明显风险提示", text_color=TEXT_DIM,
                         font=(FONT_FAMILY, 12)).pack(anchor="w", pady=(6, 0))

        right = ctk.CTkFrame(frame, fg_color="transparent")
        right.grid(row=0, column=1, sticky="nsew", padx=12, pady=10)
        ctk.CTkLabel(right, text="回撤画像", text_color=TEXT,
                     font=(FONT_FAMILY, 14, "bold")).pack(anchor="w")
        drawdown_rows = [
            ("最大回撤", self._fmt_strategy_pct(summary.get("max_drawdown"))),
            ("回撤开始", summary.get("max_drawdown_start") or "—"),
            ("回撤结束", summary.get("max_drawdown_end") or "—"),
            ("最大回撤天数", f"{summary.get('max_drawdown_days') or 0} 天"),
            ("最长回撤期", f"{summary.get('longest_drawdown_days') or 0} 天"),
            ("年化波动", self._fmt_strategy_pct(summary.get("annualized_volatility"))),
        ]
        for label, value in drawdown_rows:
            row = ctk.CTkFrame(right, fg_color="transparent")
            row.pack(fill="x", pady=(5, 0))
            ctk.CTkLabel(row, text=label, text_color=TEXT_DIM,
                         font=(FONT_FAMILY, 12), width=92, anchor="w").pack(side="left")
            ctk.CTkLabel(row, text=str(value), text_color=TEXT,
                         font=(FONT_MONO, 12), anchor="w").pack(side="left")

        skipped_rows = []
        for period in periods:
            period_label = f"{period.get('start_date')} → {period.get('end_date')}"
            for pos in period.get("skipped_positions") or []:
                skipped_rows.append([
                    period_label,
                    pos.get("bond_code", ""),
                    pos.get("bond_name", ""),
                    pos.get("reason", ""),
                ])
        self._strategy_section_title(frame, "现金替代 / 跳过成交", 1, 0, columnspan=2)
        self._render_strategy_small_tree(
            frame, 2, 0,
            ["period", "code", "name", "reason"],
            ["区间", "代码", "名称", "原因"],
            [170, 90, 100, 420],
            skipped_rows,
            columnspan=2,
        )

    def _render_strategy_robustness_panel(self, result):
        frame = getattr(self, "strategy_bt_robustness_frame", None)
        if frame is None:
            return
        self._clear_strategy_panel(frame)
        periods = result.get("periods") or []
        diagnostics = result.get("diagnostics") or {}
        summary = result.get("summary") or {}
        attribution = diagnostics.get("attribution") or {}
        data_quality = diagnostics.get("data_quality") or {}

        frame.grid_columnconfigure(0, weight=1)
        frame.grid_columnconfigure(1, weight=1)
        frame.grid_rowconfigure(2, weight=1)

        returns = [
            float(p.get("period_return"))
            for p in periods
            if p.get("period_return") is not None and np.isfinite(p.get("period_return"))
        ]
        win_rate = (sum(1 for r in returns if r > 0) / len(returns)) if returns else None
        worst = min(returns) if returns else None
        best = max(returns) if returns else None
        ret_std = float(np.std(returns, ddof=1)) if len(returns) > 1 else None
        fallback_ratio = float(data_quality.get("current_fallback_ratio") or 0.0)
        positive_contrib = [
            float(row.get("contribution"))
            for row in attribution.get("top_contributors") or []
            if row.get("contribution") is not None and float(row.get("contribution")) > 0
        ]
        top3_contrib = sum(positive_contrib[:3])
        total_positive = sum(positive_contrib)
        concentration = top3_contrib / total_positive if total_positive > 0 else None

        metrics = ctk.CTkFrame(frame, fg_color="transparent")
        metrics.grid(row=0, column=0, columnspan=2, sticky="ew", padx=10, pady=(8, 4))
        for i in range(5):
            metrics.grid_columnconfigure(i, weight=1)
        self._strategy_metric_tile(metrics, 0, "区间胜率", self._fmt_strategy_pct(win_rate))
        self._strategy_metric_tile(metrics, 1, "最好单期", self._fmt_strategy_pct(best, sign=True))
        self._strategy_metric_tile(metrics, 2, "最差单期", self._fmt_strategy_pct(worst, sign=True))
        self._strategy_metric_tile(metrics, 3, "单期波动", self._fmt_strategy_pct(ret_std))
        self._strategy_metric_tile(metrics, 4, "前三贡献集中", self._fmt_strategy_pct(concentration))

        notes = self._strategy_robustness_notes(
            summary=summary,
            win_rate=win_rate,
            worst=worst,
            concentration=concentration,
            fallback_ratio=fallback_ratio,
        )
        left = ctk.CTkFrame(frame, fg_color="transparent")
        left.grid(row=1, column=0, sticky="nsew", padx=12, pady=8)
        ctk.CTkLabel(left, text="稳健性提示", text_color=TEXT,
                     font=(FONT_FAMILY, 14, "bold")).pack(anchor="w")
        for note in notes:
            ctk.CTkLabel(left, text=f"• {note}", text_color=TEXT_DIM,
                         font=(FONT_FAMILY, 12), justify="left",
                         wraplength=520).pack(anchor="w", pady=(6, 0))

        right = ctk.CTkFrame(frame, fg_color="transparent")
        right.grid(row=1, column=1, sticky="nsew", padx=12, pady=8)
        ctk.CTkLabel(right, text="参数复核建议", text_color=TEXT,
                     font=(FONT_FAMILY, 14, "bold")).pack(anchor="w")
        suggestions = [
            "用快速模式把 TopN 上下浮动一档后加入对比",
            "把交易成本调到 5/10 bps 检查换手敏感度",
            "周频和月频各跑一次, 看收益是否只来自调仓频率",
            "切到精确模式复核最终候选策略",
        ]
        for text in suggestions:
            ctk.CTkLabel(right, text=f"• {text}", text_color=TEXT_DIM,
                         font=(FONT_FAMILY, 12), justify="left",
                         wraplength=520).pack(anchor="w", pady=(6, 0))

        worst_rows = []
        for period in sorted(periods, key=lambda p: float(p.get("period_return") or 0.0))[:8]:
            period_return = period.get("period_return")
            benchmark = period.get("benchmark_return")
            excess = (
                float(period_return) - float(benchmark)
                if period_return is not None and benchmark is not None else None
            )
            worst_rows.append([
                f"{period.get('start_date')} → {period.get('end_date')}",
                self._fmt_strategy_pct(period_return, sign=True),
                self._fmt_strategy_pct(excess, sign=True),
                self._fmt_strategy_pct(period.get("turnover")),
                self._fmt_strategy_pct(period.get("cash_weight")),
                self._strategy_codes_preview(period.get("selected_codes") or [], limit=8),
            ])
        self._strategy_section_title(frame, "最差区间复盘", 2, 0, columnspan=2)
        self._render_strategy_small_tree(
            frame, 3, 0,
            ["period", "ret", "excess", "turnover", "cash", "codes"],
            ["区间", "收益", "超额", "换手", "现金", "持仓"],
            [170, 76, 76, 76, 76, 360],
            worst_rows,
            columnspan=2,
            xscroll=True,
        )

    @staticmethod
    def _strategy_robustness_notes(*, summary, win_rate, worst, concentration, fallback_ratio):
        notes = []
        if win_rate is not None:
            if win_rate >= 0.6:
                notes.append("多数调仓区间为正收益, 收益分布相对均衡")
            elif win_rate < 0.45:
                notes.append("区间胜率偏低, 需要确认是否靠少数大涨区间贡献")
        if concentration is not None and concentration >= 0.65:
            notes.append("前三贡献集中度较高, 需要检查是否依赖少数个券")
        if worst is not None and worst <= -0.08:
            notes.append("存在单期大幅亏损, 建议复核该期持仓和市场环境")
        if summary.get("avg_turnover") is not None and float(summary.get("avg_turnover")) >= 0.8:
            notes.append("平均换手较高, 成本和滑点敏感度需要重点复核")
        if fallback_ratio > 0.2:
            notes.append("当前条款回退比例较高, 历史口径可信度偏弱")
        if not notes:
            notes.append("未发现特别突出的单点脆弱性, 可继续用参数对比做复核")
        return notes

    def _render_strategy_data_panel(self, result):
        frame = self.strategy_bt_data_frame
        self._clear_strategy_panel(frame)
        diagnostics = result.get("diagnostics") or {}
        data_quality = diagnostics.get("data_quality") or {}
        performance = diagnostics.get("performance") or {}
        config = result.get("config") or {}
        periods = result.get("periods") or []

        frame.grid_columnconfigure(0, weight=1)
        frame.grid_columnconfigure(1, weight=1)
        frame.grid_rowconfigure(2, weight=1)

        fallback_ratio = float(data_quality.get("current_fallback_ratio") or 0.0)
        if fallback_ratio <= 0:
            quality, color = "高", get_color(GREEN)
        elif fallback_ratio <= 0.2:
            quality, color = "中", get_color(ORANGE)
        else:
            quality, color = "低", get_color(RED)
        overview = ctk.CTkFrame(frame, fg_color="transparent")
        overview.grid(row=0, column=0, sticky="nsew", padx=12, pady=10)
        ctk.CTkLabel(overview, text="回测可信度", text_color=TEXT,
                     font=(FONT_FAMILY, 14, "bold")).pack(anchor="w")
        ctk.CTkLabel(overview, text=quality, text_color=color,
                     font=(FONT_FAMILY, 28, "bold")).pack(anchor="w", pady=(4, 0))
        data_tooltips = {
            "当前回退": "使用当前条款替代历史条款快照的比例; 越高代表未来信息偏差风险越大",
            "修正应用": "历史转股价等条款修正被应用的次数",
            "事件应用": "公告事件表中下修、强赎、回售等事件被应用的次数",
            "来源分布": "历史条款来自快照、修正、事件或当前回退的数量分布",
        }
        for label, value in (
            ("条款样本", data_quality.get("sample_count") or 0),
            ("当前回退", self._fmt_strategy_pct(fallback_ratio)),
            ("修正应用", data_quality.get("patch_applied_count") or 0),
            ("事件应用", data_quality.get("event_applied_count") or 0),
            ("来源分布", data_quality.get("source_counts") or {}),
        ):
            lbl = ctk.CTkLabel(overview, text=f"{label}: {value}", text_color=TEXT_DIM,
                               font=(FONT_FAMILY, 12), wraplength=520)
            lbl.pack(anchor="w", pady=(4, 0))
            if label in data_tooltips:
                Tooltip(lbl, data_tooltips[label])

        params = ctk.CTkFrame(frame, fg_color="transparent")
        params.grid(row=0, column=1, sticky="nsew", padx=12, pady=10)
        ctk.CTkLabel(params, text="本次参数快照", text_color=TEXT,
                     font=(FONT_FAMILY, 14, "bold")).pack(anchor="w")
        param_labels = {
            "selection_view": "选债视图",
            "rebalance_freq": "调仓频率",
            "top_n": "Top N",
            "execution_timing": "成交时点",
            "mark_to_market": "逐日估值",
            "transaction_cost": "交易成本",
            "max_price_staleness_days": "成交价最长容忍天数",
            "min_market_price": "最低价格",
            "max_market_price": "最高价格",
            "max_conversion_premium": "最高转股溢价",
        }
        for key in (
            "selection_view", "rebalance_freq", "top_n", "execution_timing",
            "mark_to_market", "transaction_cost", "max_price_staleness_days",
            "min_market_price", "max_market_price", "max_conversion_premium",
        ):
            ctk.CTkLabel(params, text=f"{param_labels.get(key, key)}: {config.get(key)}", text_color=TEXT_DIM,
                         font=(FONT_MONO, 12), wraplength=520).pack(anchor="w", pady=(4, 0))
        if performance:
            ctk.CTkLabel(params, text="缓存 / 性能", text_color=TEXT,
                         font=(FONT_FAMILY, 13, "bold")).pack(anchor="w", pady=(10, 0))
            for key in (
                "pricing_snapshot_hits", "pricing_snapshot_misses",
                "price_prefilter_excluded", "runtime_cache.bond_history_hits",
                "runtime_cache.bond_history_misses", "runtime_cache.stock_history_hits",
                "runtime_cache.stock_history_misses", "runtime_cache.terms_hits",
                "runtime_cache.terms_misses",
            ):
                if key in performance:
                    ctk.CTkLabel(params, text=f"{key}: {performance.get(key)}",
                                 text_color=TEXT_DIM, font=(FONT_MONO, 12),
                                 wraplength=520).pack(anchor="w", pady=(4, 0))

        period_rows = []
        for period in periods:
            dq = period.get("data_quality") or {}
            period_rows.append([
                period.get("start_date", ""),
                period.get("eligible_count", 0),
                period.get("candidate_count", 0),
                period.get("selected_count", 0),
                self._fmt_strategy_pct(dq.get("current_fallback_ratio")),
                dq.get("patch_applied_count", 0),
                dq.get("event_applied_count", 0),
            ])
        self._strategy_section_title(frame, "逐期数据口径", 1, 0, columnspan=2)
        self._render_strategy_small_tree(
            frame, 2, 0,
            ["date", "eligible", "candidate", "selected", "fallback", "patch", "event"],
            ["调仓日", "可投", "候选", "选中", "当前回退", "修正", "事件"],
            [100, 70, 70, 70, 92, 70, 70],
            period_rows,
            columnspan=2,
        )

    def _record_strategy_comparison_result(self, result):
        summary = result.get("summary") or {}
        config = result.get("config") or {}
        label = (
            f"{self.v_st_template.get()} · {config.get('selection_view') or self.v_st_view.get()} · "
            f"{self.v_st_freq.get()}频 Top{config.get('top_n') or self.v_st_top_n.get()}"
        )
        records = list(getattr(self, "_strategy_compare_results", []) or [])
        key = (
            str(result.get("start_date")),
            str(result.get("end_date")),
            label,
            summary.get("final_equity"),
            summary.get("max_drawdown"),
        )
        records = [row for row in records if row.get("key") != key]
        records.append({"key": key, "label": label, "result": result})
        self._strategy_compare_results = records[-8:]

    def _clear_strategy_comparison(self):
        self._strategy_compare_results = []
        self._render_strategy_comparison()
        self.v_st_status.set("已清空策略对比")

    def _render_strategy_comparison(self):
        frame = getattr(self, "strategy_bt_compare_frame", None)
        if frame is None:
            return
        self._clear_strategy_panel(frame)
        records = list(getattr(self, "_strategy_compare_results", []) or [])
        if not records:
            ctk.CTkLabel(
                frame,
                text="运行策略后会自动保留最近 8 次结果, 用于横向比较",
                font=(FONT_FAMILY, 13),
                text_color=TEXT_DIM,
            ).grid(row=0, column=0, padx=12, pady=12, sticky="w")
            return

        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(1, weight=1)
        self._strategy_section_title(frame, "最近策略对比", 0, 0)
        rows = []
        best_idx = self._best_strategy_record_index(records)
        for idx, record in enumerate(records, start=1):
            result = record["result"]
            summary = result.get("summary") or {}
            diagnostics = result.get("diagnostics") or {}
            dq = diagnostics.get("data_quality") or {}
            label = record.get("label") or f"策略 {idx}"
            rows.append([
                "★" if idx - 1 == best_idx else "",
                label,
                f"{result.get('start_date')} → {result.get('end_date')}",
                self._fmt_strategy_pct(summary.get("annualized_return"), sign=True),
                self._fmt_strategy_pct(summary.get("total_return"), sign=True),
                self._fmt_strategy_pct(summary.get("excess_return"), sign=True),
                self._fmt_strategy_pct(summary.get("max_drawdown")),
                f"{summary.get('sharpe'):.2f}" if summary.get("sharpe") is not None else "—",
                f"{summary.get('calmar'):.2f}" if summary.get("calmar") is not None else "—",
                self._fmt_strategy_pct(summary.get("avg_turnover")),
                self._fmt_strategy_pct(summary.get("total_cost")),
                self._fmt_strategy_pct(dq.get("current_fallback_ratio")),
            ])
        self._render_strategy_small_tree(
            frame, 1, 0,
            ["best", "label", "period", "ann", "ret", "excess", "dd", "sharpe",
             "calmar", "turnover", "cost", "fallback"],
            ["", "策略", "区间", "年化", "总收益", "超额", "回撤", "Sharpe",
             "Calmar", "换手", "成本", "当前回退"],
            [34, 230, 190, 76, 76, 76, 76, 70, 70, 76, 76, 86],
            rows,
            xscroll=True,
        )

    @staticmethod
    def _best_strategy_record_index(records):
        best_idx = None
        best_score = -np.inf
        for idx, record in enumerate(records):
            summary = (record.get("result") or {}).get("summary") or {}
            annualized = summary.get("annualized_return")
            drawdown = summary.get("max_drawdown")
            sharpe = summary.get("sharpe")
            if annualized is None or not np.isfinite(annualized):
                continue
            score = float(annualized)
            if drawdown is not None and np.isfinite(drawdown):
                score -= 0.5 * abs(float(drawdown))
            if sharpe is not None and np.isfinite(sharpe):
                score += 0.03 * float(sharpe)
            if score > best_score:
                best_idx = idx
                best_score = score
        return best_idx

    def _clear_strategy_panel(self, frame):
        for child in frame.winfo_children():
            child.destroy()
        for i in range(8):
            frame.grid_rowconfigure(i, weight=0)
            frame.grid_columnconfigure(i, weight=0)

    def _strategy_metric_tile(self, parent, col, title, value):
        cell = ctk.CTkFrame(parent, fg_color="transparent")
        cell.grid(row=0, column=col, sticky="ew", padx=8, pady=6)
        ctk.CTkLabel(cell, text=title, text_color=TEXT_DIM,
                     font=(FONT_FAMILY, 11)).pack(anchor="w")
        ctk.CTkLabel(cell, text=str(value), text_color=TEXT,
                     font=(FONT_FAMILY, 16, "bold")).pack(anchor="w")

    def _strategy_section_title(self, parent, text, row, col, columnspan=1):
        ctk.CTkLabel(parent, text=text, text_color=TEXT,
                     font=(FONT_FAMILY, 14, "bold")).grid(
                         row=row, column=col, columnspan=columnspan,
                         sticky="w", padx=12, pady=(10, 4))

    def _render_strategy_small_tree(
        self, parent, row, col, columns, headers, widths, values, *,
        columnspan=1,
        xscroll=False,
    ):
        _configure_tree_style()
        container = ctk.CTkFrame(parent, fg_color="transparent")
        container.grid(row=row, column=col, columnspan=columnspan,
                       sticky="nsew", padx=8, pady=(0, 8))
        container.grid_columnconfigure(0, weight=1)
        container.grid_rowconfigure(0, weight=1)
        tree = ttk.Treeview(container, columns=columns, show="headings", selectmode="browse")
        y_scroll = ctk.CTkScrollbar(container, orientation="vertical", command=tree.yview)
        if xscroll:
            x_scroll = ctk.CTkScrollbar(container, orientation="horizontal", command=tree.xview)
            tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        else:
            x_scroll = None
            tree.configure(yscrollcommand=y_scroll.set)
        tree.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        if x_scroll is not None:
            x_scroll.grid(row=1, column=0, sticky="ew")
        _configure_responsive_columns(tree, columns, headers, widths)
        _attach_column_sort(tree, columns, headers)
        for idx, vals in enumerate(values):
            tree.insert("", "end", iid=str(idx), values=vals)
        if not values:
            tree.insert("", "end", values=["—"] + [""] * (len(columns) - 1))
        return tree

    @staticmethod
    def _fmt_strategy_pct(value, sign=False):
        if value is None:
            return "—"
        try:
            f = float(value)
        except (TypeError, ValueError):
            return "—"
        if not np.isfinite(f):
            return "—"
        return f"{f*100:+.2f}%" if sign else f"{f*100:.2f}%"

    @staticmethod
    def _fmt_strategy_price(value):
        if value is None:
            return "—"
        try:
            f = float(value)
        except (TypeError, ValueError):
            return "—"
        return f"{f:.2f}" if np.isfinite(f) else "—"

    @staticmethod
    def _strategy_codes_preview(codes, limit=6):
        codes = [str(code) for code in codes or []]
        if not codes:
            return "—"
        head = ", ".join(codes[:limit])
        if len(codes) > limit:
            head += f" +{len(codes) - limit}"
        return head

    def _export_strategy_backtest_csv(self):
        if not self._last_strategy_bt_result:
            messagebox.showinfo("提示", "请先运行策略回测")
            return
        path = filedialog.asksaveasfilename(
            title="导出策略回测逐期摘要",
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv"), ("所有文件", "*.*")],
            initialfile="strategy_backtest.csv",
        )
        if not path:
            return
        try:
            write_strategy_backtest_csv(path, self._last_strategy_bt_result)
            self.v_st_status.set(f"已导出策略回测到 {path}")
        except Exception as exc:
            messagebox.showerror("导出失败", str(exc))

    def _refresh_backtest_chart(self):
        """切换"价值分解"复选框时无需重新拉数据, 用缓存重绘."""
        if self._last_bt_result is not None:
            self._render_backtest_chart(self._last_bt_result)

    def _render_backtest_chart(self, result):
        dates = result["dates"]
        theo = result["theo_prices"]
        mkt = result["market_prices"]
        bond_floors = result.get("bond_floors", [])
        parities = result.get("parities", [])
        sigmas = result.get("sigmas", [])
        ivs = result.get("ivs", [])

        if not dates:
            self.v_bt_status.set("❌ 无有效采样点")
            return

        # 释放旧图表资源，防止内存泄漏
        if self._bt_figure is not None:
            self._bt_figure.clf()
            plt.close(self._bt_figure)
            self._bt_figure = None
            self._bt_canvas = None

        for child in self.bt_chart_frame.winfo_children():
            child.destroy()

        # 根据当前深浅色模式获取真实 HEX
        bg_card_color = get_color(BG_CARD)
        bg_input_color = get_color(BG_INPUT)
        text_dim_color = get_color(TEXT_DIM)
        text_color = get_color(TEXT)
        border_color = get_color(BORDER)
        accent_color = get_color(ACCENT)
        orange_color = get_color(ORANGE)
        red_color = get_color(RED)
        green_color = get_color(GREEN)

        iv_arr = np.array([v if v is not None else np.nan for v in ivs], dtype=float) \
                 if len(ivs) else np.array([])
        has_iv = iv_arr.size > 0 and bool(np.any(np.isfinite(iv_arr)))
        show_decomp = bool(self.v_bt_show_decomp.get()) and bond_floors and parities

        if has_iv:
            fig = Figure(figsize=(11, 6), dpi=100, facecolor=bg_card_color)
            ax = fig.add_subplot(2, 1, 1, facecolor=bg_input_color)
            ax_iv = fig.add_subplot(2, 1, 2, facecolor=bg_input_color, sharex=ax)
        else:
            fig = Figure(figsize=(11, 5), dpi=100, facecolor=bg_card_color)
            ax = fig.add_subplot(111, facecolor=bg_input_color)
            ax_iv = None

        ax.plot(dates, theo, color=accent_color, linewidth=2.0, marker="o", markersize=4,
                label="理论价", zorder=3)
        ax.plot(dates, mkt, color=orange_color, linewidth=2.0, marker="s", markersize=4,
                label="市价(收盘)", zorder=2)

        if show_decomp:
            ax.plot(dates, bond_floors, color=text_dim_color, linewidth=1.2,
                    linestyle="--", alpha=0.7, label="纯债价值", zorder=1)
            ax.plot(dates, parities, color=green_color, linewidth=1.2,
                    linestyle=":", alpha=0.7, label="转股价值", zorder=1)

        theo_arr = np.array(theo)
        mkt_arr = np.array(mkt)
        ax.fill_between(dates, theo_arr, mkt_arr,
                        where=(mkt_arr >= theo_arr).tolist(), color=red_color, alpha=0.12, label="市价溢价")
        ax.fill_between(dates, theo_arr, mkt_arr,
                        where=(mkt_arr < theo_arr).tolist(), color=green_color, alpha=0.12, label="市价折价")

        ax.set_ylabel("价格", color=text_dim_color, fontsize=10)
        ax.tick_params(colors=text_dim_color, labelsize=9)
        for spine in ax.spines.values():
            spine.set_color(border_color)
        ax.grid(True, color=border_color, linestyle="--", alpha=0.4)

        legend = ax.legend(loc="best", framealpha=0.9, facecolor=bg_card_color,
                           edgecolor=border_color, fontsize=9, labelcolor=text_color)
        legend.get_frame().set_linewidth(0.5)

        if ax_iv is not None:
            hv_pct = np.array(sigmas) * 100
            iv_pct = iv_arr * 100
            ax_iv.plot(dates, hv_pct, color=text_dim_color, linewidth=1.5,
                       marker="o", markersize=3, label="历史波动率 HV", zorder=2)
            ax_iv.plot(dates, iv_pct, color=accent_color, linewidth=2.0,
                       marker="s", markersize=4, label="隐含波动率 IV", zorder=3)
            valid = np.isfinite(iv_pct) & np.isfinite(hv_pct)
            if np.any(valid):
                d_v = np.array(dates)[valid]
                hv_v = hv_pct[valid]
                iv_v = iv_pct[valid]
                where_high = [bool(x) for x in (iv_v >= hv_v)]
                where_low = [bool(x) for x in (iv_v < hv_v)]
                ax_iv.fill_between(d_v, hv_v, iv_v, where=where_high,
                                   color=red_color, alpha=0.12)
                ax_iv.fill_between(d_v, hv_v, iv_v, where=where_low,
                                   color=green_color, alpha=0.12)
            ax_iv.set_xlabel("日期", color=text_dim_color, fontsize=10)
            ax_iv.set_ylabel("σ (%)", color=text_dim_color, fontsize=10)
            ax_iv.tick_params(colors=text_dim_color, labelsize=9)
            for spine in ax_iv.spines.values():
                spine.set_color(border_color)
            ax_iv.grid(True, color=border_color, linestyle="--", alpha=0.4)
            leg_iv = ax_iv.legend(loc="best", framealpha=0.9, facecolor=bg_card_color,
                                  edgecolor=border_color, fontsize=9, labelcolor=text_color)
            leg_iv.get_frame().set_linewidth(0.5)
        else:
            ax.set_xlabel("日期", color=text_dim_color, fontsize=10)

        fig.autofmt_xdate(rotation=25)
        fig.tight_layout()

        canvas = FigureCanvasTkAgg(fig, master=self.bt_chart_frame)
        canvas.draw()
        canvas.get_tk_widget().grid(row=0, column=0, sticky="nsew", padx=12, pady=12)

        self._bt_figure = fig
        self._bt_canvas = canvas

        # 统计指标: 偏差 = (理论 − 市价) / 市价  (相对值, 投资者角度更直观)
        valid = (mkt_arr > 0) & np.isfinite(mkt_arr) & np.isfinite(theo_arr)
        rel_dev = np.full(theo_arr.shape, np.nan)
        rel_dev[valid] = (theo_arr[valid] - mkt_arr[valid]) / mkt_arr[valid]
        rel_clean = rel_dev[np.isfinite(rel_dev)]
        mean_basis_abs = float(np.mean(mkt_arr - theo_arr))
        corr = float(np.corrcoef(theo_arr, mkt_arr)[0, 1]) if len(theo) > 1 else float("nan")
        if rel_clean.size:
            mean_dev = float(np.mean(rel_clean))
            rmse = float(np.sqrt(np.mean(rel_clean ** 2)))
            max_abs = float(np.max(np.abs(rel_clean)))
            hit_rate = float(np.mean(np.abs(rel_clean) <= 0.05))
        else:
            mean_dev = rmse = max_abs = hit_rate = float("nan")

        iv_hv_pp: float | None = None
        if has_iv:
            iv_valid = iv_arr[np.isfinite(iv_arr)]
            hv_arr = np.array(sigmas)
            hv_for_iv = hv_arr[np.isfinite(iv_arr)]
            if iv_valid.size:
                iv_hv_pp = float(np.mean(iv_valid - hv_for_iv)) * 100

        self._update_backtest_stats(mean_dev, rmse, max_abs, hit_rate, corr, iv_hv_pp)
        status_parts = [
            f"✅ {len(dates)} 个采样点",
            f"平均基差(市价−理论)={mean_basis_abs:+.2f}",
        ]
        self.v_bt_status.set("  ·  ".join(status_parts))
        self.btn_bt_png.configure(state="normal")
        self.btn_bt_csv.configure(state="normal")

    def _update_backtest_stats(self, mean_dev, rmse, max_abs, hit_rate, corr, iv_hv_pp):
        stats = getattr(self, "_bt_stat_vars", None)
        if not stats:
            return

        def _fmt_pct(v, sign=False):
            if not np.isfinite(v):
                return "—"
            return f"{v*100:+.2f}%" if sign else f"{v*100:.2f}%"

        stats["mean_dev"].set(_fmt_pct(mean_dev, sign=True))
        stats["rmse"].set(_fmt_pct(rmse))
        stats["max_abs"].set(_fmt_pct(max_abs))
        stats["hit_rate"].set(f"{hit_rate*100:.1f}%" if np.isfinite(hit_rate) else "—")
        stats["corr"].set(f"{corr:.3f}" if np.isfinite(corr) else "—")
        stats["iv_hv"].set(f"{iv_hv_pp:+.2f}pp" if iv_hv_pp is not None and np.isfinite(iv_hv_pp) else "—")

    # ── 回测结果导出 ──────────────────────────────────────
    def _export_bt_png(self):
        if self._bt_figure is None:
            messagebox.showinfo("提示", "请先运行回测")
            return
        path = filedialog.asksaveasfilename(
            title="导出回测图",
            defaultextension=".png",
            filetypes=[("PNG", "*.png"), ("PDF", "*.pdf"), ("SVG", "*.svg")],
            initialfile=(self.v_bond_code.get().strip() or "backtest") + ".png",
        )
        if not path:
            return
        try:
            # 把上方的 6 个统计指标压成一行附到图顶, 让导出图自带摘要;
            # 用 fig.text + bbox_extra_artists 而不是 suptitle, 避免改动现有 tight_layout
            extra_artists = []
            stats_line = self._compose_bt_stats_line()
            bond_code = self.v_bond_code.get().strip()
            header_lines = []
            if bond_code:
                header_lines.append(bond_code)
            if stats_line:
                header_lines.append(stats_line)
            if header_lines:
                txt = self._bt_figure.text(
                    0.5, 1.0, "\n".join(header_lines),
                    ha="center", va="bottom",
                    fontsize=10,
                    color=get_color(TEXT),
                )
                extra_artists.append(txt)
            try:
                self._bt_figure.savefig(
                    path, dpi=150, bbox_inches="tight",
                    bbox_extra_artists=extra_artists,
                    facecolor=self._bt_figure.get_facecolor())
            finally:
                for artist in extra_artists:
                    artist.remove()
            self.v_bt_status.set(f"已导出图表到 {path}")
        except Exception as exc:
            messagebox.showerror("导出失败", str(exc))

    def _compose_bt_stats_line(self) -> str:
        """把 6 个统计指标 StringVar 压成一行 '标签 值  ·  标签 值 ...'."""
        stats = getattr(self, "_bt_stat_vars", None)
        if not stats:
            return ""
        pairs = (
            ("均偏差",     stats.get("mean_dev")),
            ("RMSE",       stats.get("rmse")),
            ("最大|偏差|", stats.get("max_abs")),
            ("命中率±5%",  stats.get("hit_rate")),
            ("相关",       stats.get("corr")),
            ("IV−HV",      stats.get("iv_hv")),
        )
        parts = []
        for label, var in pairs:
            if var is None:
                continue
            val = var.get()
            if not val or val == "—":
                continue
            parts.append(f"{label} {val}")
        return "  ·  ".join(parts)

    def _export_bt_csv(self):
        if not self._last_bt_result or not self._last_bt_result.get("dates"):
            messagebox.showinfo("提示", "请先运行回测")
            return
        path = filedialog.asksaveasfilename(
            title="导出回测序列",
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv"), ("所有文件", "*.*")],
            initialfile=(self.v_bond_code.get().strip() or "backtest") + ".csv",
        )
        if not path:
            return
        try:
            r = self._last_bt_result
            n = len(r["dates"])
            bf = r.get("bond_floors") or [float("nan")] * n
            par = r.get("parities") or [float("nan")] * n
            iv = r.get("ivs") or [float("nan")] * n
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f)
                w.writerow(["date", "theoretical_price", "market_price", "stock_price",
                            "sigma", "bond_floor", "parity", "implied_vol"])
                for d, t, m, s, sg, b, p, ivv in zip(
                        r["dates"], r["theo_prices"], r["market_prices"],
                        r["stock_prices"], r["sigmas"], bf, par, iv):
                    w.writerow([d.isoformat(), f"{t:.4f}", f"{m:.4f}", f"{s:.4f}",
                                f"{sg:.6f}", f"{b:.4f}", f"{p:.4f}", f"{ivv:.6f}"])
            self.v_bt_status.set(f"已导出 {len(r['dates'])} 条记录到 {path}")
        except Exception as exc:
            messagebox.showerror("导出失败", str(exc))
