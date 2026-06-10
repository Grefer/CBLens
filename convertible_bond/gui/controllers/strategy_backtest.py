"""📈 历史回测 — 选债策略回测 (多债组合).

从原 backtest.py 拆分: 本模块负责基于机会分的选债策略回测、快照保存与多次对比;
单债"模型 vs 市场"偏差回测见 backtest.py。两个 mixin 都混入 CBPricerApp,
方法间通过 self.* 互相调用不受拆分影响。
"""
from __future__ import annotations

import csv
import threading
from datetime import date
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.colors import LinearSegmentedColormap

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
    _funding_legacy_alias,
    _normalize_holding_mode,
    backtest_score_strategy,
    build_rebalance_schedule,
    write_strategy_backtest_csv,
)
from ..theme import (
    ACCENT, BG_CARD, BG_INPUT, BORDER,
    GREEN, ORANGE, RED,
    TEXT, TEXT_DIM,
    FONT_FAMILY, FONT_MONO,
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
STRATEGY_DETAIL_TABLE_HEIGHT = 7
STRATEGY_COMPACT_TABLE_HEIGHT = 6
STRATEGY_MEDIUM_TABLE_HEIGHT = 8
STRATEGY_DATA_TABLE_HEIGHT = 10
STRATEGY_OVERVIEW_CHART_HEIGHT = 540
STRATEGY_RISK_CHART_HEIGHT = 240
STRATEGY_SECONDARY_CHART_HEIGHT = 300
WIND_HIGH_FIDELITY_CODE_WARN_LIMIT = 120
WIND_HIGH_FIDELITY_PRICING_WARN_LIMIT = 1000
WIND_HIGH_FIDELITY_REQUEST_MULTIPLIER = 10


def _strategy_snapshot_jsonable(obj):
    """递归将 date/datetime/nan/inf 转为 JSON 安全表示.

    既可作为 json.dump(default=...) 的 fallback,
    也可直接调用 _strategy_snapshot_jsonable(whole_dict) 做完整转换.
    """
    from datetime import datetime as _datetime
    if isinstance(obj, date):
        tag = "datetime" if isinstance(obj, _datetime) else "date"
        return {"__cblens_type__": tag, "value": obj.isoformat()}
    if isinstance(obj, (set, frozenset)):
        return [_strategy_snapshot_jsonable(v) for v in obj]
    if isinstance(obj, float):
        if np.isnan(obj) or np.isinf(obj):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: _strategy_snapshot_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_strategy_snapshot_jsonable(v) for v in obj]
    if isinstance(obj, (int, bool, str)) or obj is None:
        return obj
    # fallback for json.dump default
    raise TypeError(f"Not JSON serializable: {type(obj)} {obj!r}")


def _strategy_snapshot_object_hook(d):
    """json.load object_hook: tagged dict → date/datetime."""
    from datetime import datetime as _datetime
    if "__cblens_type__" in d:
        tag = d["__cblens_type__"]
        value = d.get("value", "")
        if tag == "date":
            return date.fromisoformat(value)
        if tag == "datetime":
            return _datetime.fromisoformat(value)
    return d


class StrategyBacktestCancelled(Exception):
    """用户主动中断策略回测."""


# 选债哲学由"选债规则"统一驱动: 置信度与硬复核风险按规则推导。
_DEFAULT_VIEW_POLICY = {"min_confidence": ("高", "中"), "exclude_review_risks": True}
STRATEGY_VIEW_POLICY = {
    "综合机会": {"min_confidence": ("高", "中"), "exclude_review_risks": True},
    "低估候选": {"min_confidence": ("高", "中"), "exclude_review_risks": True},
    "转股折价": {"min_confidence": ("高", "中"), "exclude_review_risks": True},
}

# 策略方案基线: 选择方案时先重置这些"选债逻辑"字段, 避免上个方案残留;
# 数据源 / 区间 / 代码池属环境配置, 不在策略方案范围内。
_STRATEGY_TEMPLATE_BASE = {
    "v_st_freq": "月", "v_st_top_n": "10", "v_st_view": "综合机会",
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


class StrategyBacktestMixin:
    """选债策略回测 tab 的业务逻辑 (含快照与对比)."""

    # ── 策略回测 (Pro 预览) ───────────────────────────────
    def _apply_strategy_template(self, name):
        """套用策略方案; 选「自定义」不改动现有参数, 仅保留手动调整。"""
        overrides = STRATEGY_TEMPLATES.get(name)
        if overrides is None:  # 自定义
            view = self.v_st_view.get()
            desc = STRATEGY_VIEW_DESCRIPTIONS.get(view, "可手动调整选债和过滤条件")
            self.v_st_summary.set(f"自定义参数 · 选债规则「{view}」: {desc}")
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
        self.v_st_summary.set(
            f"策略方案「{name}」 · {template_desc} · 选债规则: {view_desc}")

    def _describe_strategy_view(self, name):
        """用户切换选债规则时, 写入策略摘要区 (不覆盖运行状态)。"""
        desc = STRATEGY_VIEW_DESCRIPTIONS.get(name)
        if desc:
            template = self.v_st_template.get() if hasattr(self, "v_st_template") else "自定义"
            prefix = f"策略方案「{template}」" if template != "自定义" else "自定义参数"
            self.v_st_summary.set(f"{prefix} · 选债规则「{name}」: {desc}")

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
        standard_codes = [
            code for code in codes
            if BOND_CODE_RE.match(str(code or "").strip().upper())
            and str(code or "").strip().upper().endswith((".SH", ".SZ"))
        ]
        label = "本地条款库"
        skipped = len(codes) - len(standard_codes)
        if skipped > 0:
            label += f" (已排除非沪深代码 {skipped} 个)"
        return self._dedupe_strategy_codes(standard_codes), label

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
        """纯信息预览: 检查代码池、历史口径和预计工作量, 结果仅展示在面板内。"""
        try:
            info = self._strategy_precheck_info()
        except Exception as exc:
            self.v_st_precheck.set(f"⚠ 预检异常: {exc}")
            self.v_st_status.set(f"预检异常 · {exc}")
            return
        text = self._format_strategy_precheck(info)
        self.v_st_precheck.set(text)
        warnings = info.get("warnings") or []
        warn_suffix = f" · ⚠ {len(warnings)} 条提醒" if warnings else ""
        self.v_st_status.set(
            f"预检完成 · {info['code_count']} 只 · "
            f"{info['period_count']} 个调仓区间{warn_suffix}"
        )

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
        estimated_wind_requests = (
            estimated_pricing * WIND_HIGH_FIDELITY_REQUEST_MULTIPLIER
            if mode == "Wind高保真" else 0
        )
        history = self._strategy_history_precheck(schedule[:-1])
        patch = self._strategy_patch_precheck()
        events = self._strategy_events_precheck()
        warnings = []
        if mode == "Wind高保真" and not history["enabled"]:
            warnings.append("Wind 历史条款未启用, 过去条款会回退到当前条款视角")
        if mode == "Wind高保真" and (
            len(codes) > WIND_HIGH_FIDELITY_CODE_WARN_LIMIT
            or estimated_pricing > WIND_HIGH_FIDELITY_PRICING_WARN_LIMIT
        ):
            warnings.append(
                "Wind高保真会逐债拉取历史条款/状态/行情, 大池回测可能耗时数小时"
            )
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
            "estimated_wind_requests": estimated_wind_requests,
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
        wind_text = ""
        if info.get("estimated_wind_requests"):
            wind_text = f" · Wind请求估算≈{info['estimated_wind_requests']} 次"
        return (
            f"规模 {info['pool_label']} {info['code_count']} 只 · "
            f"{info['period_count']} 期 · Top{info['top_n']} · "
            f"预计定价≈{info['estimated_pricing']} 次{wind_text} · "
            f"{info.get('history_mode', '标准')}口径 · "
            f"条款 {history['label']} 覆盖 {history['coverage_ratio']*100:.0f}% · "
            f"修正 {patch_info} · 事件 {info['events']['count']} 条 · "
            f"提醒: {warning_text}"
        )

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
            config = ScoreStrategyConfig(
                top_n=max(1, int(float(self.v_st_top_n.get()))),
                holding_mode=holding_mode,
                funding_mode=funding_mode,
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
            return HistoricalBondDataProvider(
                WindDataProvider(),
                history_store=None,
                patch_store=TermsPatchStore(project_terms_patches_path()),
                event_store=CBEventStore(project_events_path()),
                strip_fallback_status=True,
                merge_admission_status=False,
                provider_history_terms=True,
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

    # ── 策略回测快照 保存 / 加载 ─────────────────────────────

    _MAX_SNAPSHOTS = 8

    def _save_strategy_backtest_snapshot(self):
        """保存到 data/strategy_backtest_snapshots/ 目录, 保留最近 N 份."""
        from datetime import datetime as _dt
        result = getattr(self, "_last_strategy_bt_result", None)
        if not result:
            return None
        snap_dir = self._strategy_snapshots_dir()
        snap_dir.mkdir(parents=True, exist_ok=True)
        saved_at = _dt.now()
        payload = self._build_strategy_snapshot_payload(result, saved_at=saved_at)
        encoded = _strategy_snapshot_jsonable(payload)
        config = payload.get("meta", {}).get("config") or {}
        freq = config.get("rebalance_freq", "M")
        top_n = config.get("top_n", "?")
        ts = saved_at.strftime("%Y%m%d-%H%M%S")
        start = payload.get("meta", {}).get("start_date", "")
        end = payload.get("meta", {}).get("end_date", "")
        snapshot_id = str(payload.get("snapshot_id") or "")[:12]
        fname = f"strategy_backtest_{start}_{end}_{freq}_top{top_n}_{ts}_{snapshot_id}.json"
        path = snap_dir / fname
        self._write_strategy_snapshot_json(path, encoded)
        latest = self._strategy_snapshot_path()
        self._write_strategy_snapshot_json(latest, encoded)
        self._prune_old_snapshots()
        return {"path": path, "latest_path": latest, "snapshot_id": payload.get("snapshot_id")}

    @staticmethod
    def _write_strategy_snapshot_json(path, encoded_payload):
        import json as _json
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump(encoded_payload, f, ensure_ascii=False, indent=2)
        tmp.replace(path)

    @classmethod
    def _build_strategy_snapshot_payload(cls, result, *, saved_at):
        clean_result = cls._strategy_snapshot_result_for_save(result)
        encoded_result = _strategy_snapshot_jsonable(clean_result)
        snapshot_id = cls._strategy_snapshot_id(encoded_result)
        config = clean_result.get("config") or {}
        run_settings = clean_result.get("run_settings") or {}
        summary = clean_result.get("summary") or {}
        meta = {
            "snapshot_id": snapshot_id,
            "start_date": clean_result.get("start_date"),
            "end_date": clean_result.get("end_date"),
            "config": {
                "rebalance_freq": config.get("rebalance_freq"),
                "top_n": config.get("top_n"),
                "holding_mode": config.get("holding_mode"),
                "funding_mode": config.get("funding_mode"),
                "max_holdings": config.get("max_holdings"),
                "top_n_shortfall_policy": config.get("top_n_shortfall_policy"),  # 兼容旧快照
                "selection_view": config.get("selection_view"),
                "history_mode": config.get("history_mode"),
            },
            "run_settings": run_settings,
            "summary": {
                "final_equity": summary.get("final_equity"),
                "total_return": summary.get("total_return"),
                "max_drawdown": summary.get("max_drawdown"),
                "sharpe": summary.get("sharpe"),
                "calmar": summary.get("calmar"),
            },
            "period_count": len(clean_result.get("periods") or []),
            "equity_curve_points": len(clean_result.get("equity_curve") or []),
        }
        return {
            "schema_version": 2,
            "snapshot_id": snapshot_id,
            "saved_at": saved_at,
            "meta": meta,
            "result": clean_result,
        }

    @staticmethod
    def _strategy_snapshot_result_for_save(result):
        if not isinstance(result, dict):
            return result
        cleaned = {}
        for key, value in result.items():
            if str(key).startswith("_"):
                continue
            cleaned[key] = value
        return cleaned

    @staticmethod
    def _strategy_snapshot_id(encoded_result) -> str:
        import hashlib
        import json as _json
        raw = _json.dumps(
            encoded_result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def _prune_old_snapshots(self):
        snap_dir = self._strategy_snapshots_dir()
        if not snap_dir.exists():
            return
        files = sorted(snap_dir.glob("strategy_backtest_*.json"), key=lambda p: p.stat().st_mtime)
        while len(files) > self._MAX_SNAPSHOTS:
            oldest = files.pop(0)
            try:
                oldest.unlink()
            except Exception:
                pass

    def _load_strategy_backtest_snapshot(self, *, silent=False, render=True):
        """从 snapshots 目录加载所有快照到对比列表, 最新一份设为当前结果."""
        import json as _json
        snap_dir = self._strategy_snapshots_dir()
        legacy_path = self._strategy_snapshot_path()
        # 收集所有快照文件 (按修改时间排序)
        files = []
        if snap_dir.exists():
            files = sorted(snap_dir.glob("strategy_backtest_*.json"),
                           key=lambda p: p.stat().st_mtime)
        if legacy_path.exists() and legacy_path not in files:
            files.append(legacy_path)
            files = sorted(files, key=lambda p: p.stat().st_mtime)
        if not files:
            if not silent:
                from tkinter import messagebox
                messagebox.showinfo("提示", "未找到策略回测快照")
            return
        loaded_count = 0
        latest_result = None
        latest_saved_at = None
        seen_snapshot_keys: set[str] = set()
        for path in files:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    payload = _json.load(f, object_hook=_strategy_snapshot_object_hook)
                result = payload.get("result")
                if not result:
                    continue
                dedupe_key = self._strategy_snapshot_dedupe_key(payload, result)
                if dedupe_key in seen_snapshot_keys:
                    continue
                seen_snapshot_keys.add(dedupe_key)
                self._patch_snapshot_drawdown(result)
                # 记录快照文件路径 (用于删除)
                result["_snapshot_id"] = dedupe_key
                result["_snapshot_path"] = str(path)
                self._record_strategy_comparison_result(result)
                latest_result = result
                latest_saved_at = payload.get("saved_at")
                loaded_count += 1
            except Exception as exc:
                print(f"[策略快照] 加载 {path.name} 失败: {exc}")
        if latest_result:
            self._last_strategy_bt_result = latest_result
            if render:
                self._render_strategy_backtest_result(latest_result)
            else:
                self._mark_strategy_tabs_dirty()
            if not silent:
                self.v_st_status.set(
                    f"已加载 {loaded_count} 份快照 (最新 {latest_saved_at or '?'})")
        elif not silent:
            self.v_st_status.set("快照文件存在但无有效数据")

    @classmethod
    def _strategy_snapshot_dedupe_key(cls, payload, result) -> str:
        snapshot_id = payload.get("snapshot_id")
        if not snapshot_id:
            meta = payload.get("meta") or {}
            snapshot_id = meta.get("snapshot_id")
        if snapshot_id:
            return str(snapshot_id)
        clean_result = cls._strategy_snapshot_result_for_save(result)
        return cls._strategy_snapshot_id(_strategy_snapshot_jsonable(clean_result))

    @staticmethod
    def _patch_snapshot_drawdown(result):
        """旧快照可能丢失 drawdown 日期, 从 equity_curve 重算补回."""
        summary = result.get("summary")
        curve = result.get("equity_curve")
        if not summary or not curve:
            return
        if summary.get("max_drawdown_start") is not None:
            return  # 已有数据, 无需修补
        try:
            from ...strategy_backtest import _drawdown_stats
            stats = _drawdown_stats(curve)
            for key in ("max_drawdown_start", "max_drawdown_end",
                        "max_drawdown_days", "longest_drawdown_days"):
                if stats.get(key) is not None:
                    summary[key] = stats[key]
        except Exception:
            pass

    @staticmethod
    def _strategy_snapshot_path():
        from pathlib import Path
        return Path(__file__).resolve().parents[3] / "data" / "strategy_backtest_snapshot.json"

    @staticmethod
    def _strategy_snapshots_dir():
        from pathlib import Path
        return Path(__file__).resolve().parents[3] / "data" / "strategy_backtest_snapshots"

    # ── 懒渲染: 子页 tab 名 → 渲染函数映射 ──────────────────
    _STRATEGY_TAB_RENDERERS = {
        "总览": "_render_strategy_overview_tab",
        "明细": "_render_strategy_detail_tab",
        "归因": "_render_strategy_attribution_tab",
        "风险": "_render_strategy_risk_tab",
        "稳健性": "_render_strategy_robustness_tab",  # legacy alias, kept for tests/old callbacks
        "数据": "_render_strategy_data_tab",
        "对比": "_render_strategy_compare_tab",
    }

    def _mark_strategy_tabs_dirty(self, *tab_names):
        dirty = getattr(self, "_strategy_dirty_tabs", set())
        if tab_names:
            dirty |= set(tab_names)
        else:
            dirty = set(self._STRATEGY_TAB_RENDERERS.keys())
        self._strategy_dirty_tabs = dirty

    def _render_strategy_backtest_result(self, result):
        """入口: 更新摘要 + 标记全部子页为 dirty + 渲染当前子页."""
        self._mark_strategy_tabs_dirty()
        self._update_strategy_result_summary(result, reset_figures=True)
        self._render_current_strategy_tab(force=True)

    def _on_strategy_result_tab_change(self):
        """子页 Tabview command 回调: 切到哪页, 渲染哪页."""
        self._render_current_strategy_tab()

    def _render_current_strategy_tab(self, *, force=False):
        """只渲染当前选中的子页 (dirty 或 force 时才重绘)."""
        result = getattr(self, "_last_strategy_bt_result", None)
        if not isinstance(result, dict):
            return
        tabs = getattr(self, "strategy_result_tabs", None)
        if tabs is None:
            return
        selected = tabs.get()
        dirty = getattr(self, "_strategy_dirty_tabs", None)
        if dirty is None:
            dirty = set(self._STRATEGY_TAB_RENDERERS.keys())
            self._strategy_dirty_tabs = dirty
        if not force and selected not in dirty:
            return
        renderer_name = self._STRATEGY_TAB_RENDERERS.get(selected)
        if renderer_name is None:
            return
        renderer = getattr(self, renderer_name, None)
        if renderer is None:
            return
        try:
            renderer(result)
        except Exception as exc:
            import traceback
            traceback.print_exc()
            print(f"[策略回测] 渲染 '{selected}' 失败: {exc}")
            return
        dirty.discard(selected)
        if hasattr(self, "update_idletasks"):
            self.update_idletasks()

    def _clear_active_strategy_backtest_result(self, *, status: str | None = None):
        """清空当前活跃策略回测结果, 防止删除快照后其他页继续显示旧数据."""
        self._last_strategy_bt_result = None
        self._mark_strategy_tabs_dirty()
        self._reset_strategy_stats()
        if hasattr(self, "strategy_bt_progress"):
            self.strategy_bt_progress.set(0)
        btn_csv = getattr(self, "btn_strategy_bt_csv", None)
        if btn_csv is not None:
            try:
                btn_csv.configure(state="disabled")
            except Exception:
                pass
        for fig_attr in ("_strategy_bt_waterfall_fig", "_strategy_bt_heatmap_fig",
                         "_strategy_bt_rolling_fig", "_strategy_bt_dist_fig",
                         "_strategy_bt_compare_fig"):
            fig = getattr(self, fig_attr, None)
            if fig is not None:
                fig.clf()
                plt.close(fig)
                setattr(self, fig_attr, None)
        for frame_attr in (
            "strategy_bt_insight_frame",
            "strategy_bt_chart_frame",
            "strategy_bt_selection_frame",
            "strategy_bt_table_frame",
            "strategy_bt_attribution_frame",
            "strategy_bt_risk_frame",
            "strategy_bt_data_frame",
        ):
            frame = getattr(self, frame_attr, None)
            if frame is not None:
                self._clear_strategy_panel(frame)
        compare_frame = getattr(self, "strategy_bt_compare_frame", None)
        if compare_frame is not None:
            self._render_strategy_comparison()
        if status and hasattr(self, "v_st_status"):
            self.v_st_status.set(status)

    def _reset_strategy_stats(self):
        stats = getattr(self, "_strategy_stat_vars", None) or {}
        for var in stats.values():
            try:
                var.set("—")
            except Exception:
                pass
        labels = getattr(self, "_strategy_stat_labels", None) or {}
        for label in labels.values():
            try:
                label.configure(text_color=get_color(TEXT))
            except Exception:
                pass

    def _update_strategy_result_summary(self, result, *, reset_figures=False):
        """只更新指标卡、状态栏、CSV 按钮 (不渲染任何子页面板)."""
        if reset_figures:
            # 新结果/主题刷新时清理旧 figure; 普通切回策略页只更新摘要, 避免把已渲染子页清空。
            for fig_attr in ("_strategy_bt_waterfall_fig", "_strategy_bt_heatmap_fig",
                             "_strategy_bt_rolling_fig", "_strategy_bt_dist_fig",
                             "_strategy_bt_compare_fig"):
                fig = getattr(self, fig_attr, None)
                if fig is not None:
                    fig.clf()
                    plt.close(fig)
                    setattr(self, fig_attr, None)

        summary = result.get("summary", {})
        self._update_strategy_stats(summary)

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
            f"✅ {len(periods)} 个调仓区间 · "
            f"最终净值 {summary.get('final_equity', 1.0):.4f}{extra}{perf_text}{warning_text}"
        )
        if hasattr(self, "strategy_bt_progress"):
            self.strategy_bt_progress.set(1.0)
        self.btn_strategy_bt_csv.configure(state="normal")

    # ── 各子页渲染入口 (被懒渲染调度器调用) ──────────────────
    def _render_strategy_overview_tab(self, result):
        self._render_strategy_insight(result)
        self._render_strategy_chart(result)

    def _render_strategy_detail_tab(self, result):
        self._render_strategy_selection_panel(result)
        self._render_strategy_table(result)

    def _on_strategy_detail_filter_change(self, *_):
        self._mark_strategy_tabs_dirty("明细")
        if hasattr(self, "after_idle"):
            self.after_idle(self._render_current_strategy_tab)
        else:
            self._render_current_strategy_tab()

    def _render_strategy_attribution_tab(self, result):
        self._render_strategy_attribution(result)

    def _render_strategy_risk_tab(self, result):
        self._render_strategy_risk_panel(result)

    def _render_strategy_robustness_tab(self, result):
        renderer = getattr(self, "_render_strategy_robustness_panel", None)
        if callable(renderer):
            renderer(result)
        else:
            self._render_strategy_risk_panel(result)

    def _render_strategy_data_tab(self, result):
        self._render_strategy_data_panel(result)

    def _render_strategy_compare_tab(self, result):
        self._render_strategy_comparison()

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
        frame.grid_rowconfigure(0, weight=1)
        for col in range(4):
            frame.grid_columnconfigure(col, weight=1, uniform="strategy_insight")

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

        items = [
            ("结论", verdict),
            ("最大回撤", (
                f"{self._fmt_strategy_pct(max_drawdown)} · "
                f"{summary.get('max_drawdown_start') or '—'} → {summary.get('max_drawdown_end') or '—'}"
            )),
            ("主要贡献", (
                f"{top_name} {self._fmt_strategy_pct(top_contrib.get('contribution'), sign=True)}"
            )),
            ("数据质量", f"{quality} · 条款回退占比 {self._fmt_strategy_pct(fallback_ratio)}"),
        ]
        for col, (title, value) in enumerate(items):
            cell = ctk.CTkFrame(frame, fg_color=BG_INPUT, corner_radius=8, height=76)
            cell.grid(row=0, column=col, sticky="nsew", padx=6, pady=(8, 4))
            cell.grid_propagate(False)
            
            inner = ctk.CTkFrame(cell, fg_color="transparent")
            inner.pack(fill="both", expand=True, padx=12, pady=8)
            
            ctk.CTkLabel(inner, text=title, text_color=TEXT_DIM,
                         font=(FONT_FAMILY, 11)).pack(anchor="w")
            value_label = ctk.CTkLabel(
                inner, text=value, text_color=TEXT,
                font=(FONT_FAMILY, 13, "bold"), wraplength=260,
                justify="left")
            value_label.pack(anchor="w")

            def _update_wrap(event, label=value_label):
                label.configure(wraplength=max(180, event.width - 24))

            cell.bind("<Configure>", _update_wrap)

    def _render_strategy_chart(self, result):
        if self._strategy_bt_figure is not None:
            self._strategy_bt_figure.clf()
            plt.close(self._strategy_bt_figure)
            self._strategy_bt_figure = None
            self._strategy_bt_canvas = None

        for child in self.strategy_bt_chart_frame.winfo_children():
            child.destroy()
        self.strategy_bt_chart_frame.configure(height=STRATEGY_OVERVIEW_CHART_HEIGHT)
        self.strategy_bt_chart_frame.grid_columnconfigure(0, weight=1)
        self.strategy_bt_chart_frame.grid_rowconfigure(0, weight=1)
        self.strategy_bt_chart_frame.grid_propagate(False)

        curve = result.get("equity_curve") or []
        if not curve:
            return

        dates = [p["date"] for p in curve]
        equity = [float(p["equity"]) for p in curve]

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
        red_color = get_color(RED)

        fig = Figure(figsize=(11, 5.2), dpi=100, facecolor=bg_card_color)
        gs = fig.add_gridspec(2, 1, height_ratios=[2.2, 0.9])
        ax_eq = fig.add_subplot(gs[0, 0], facecolor=bg_input_color)
        ax_dd = fig.add_subplot(gs[1, 0], facecolor=bg_input_color, sharex=ax_eq)

        # 净值: 策略 vs 等权基准
        ax_eq.plot(dates, equity, color=accent_color, linewidth=2.2, marker="o",
                   markersize=4, label="组合净值")
        if bench_equity:
            ax_eq.plot(bench_dates, bench_equity, color=orange_color, linewidth=1.6,
                       linestyle="--", marker="s", markersize=3, label="等权基准")
        ax_eq.axhline(1.0, color=border_color, linewidth=1.0, linestyle="--")

        # 标注最大回撤起止区间
        summary = result.get("summary") or {}
        dd_start = summary.get("max_drawdown_start")
        dd_end = summary.get("max_drawdown_end")
        max_dd = summary.get("max_drawdown")
        if dd_start and dd_end and max_dd:
            dd_values_all = self._strategy_drawdown_values(equity)
            dd_idx = int(np.argmin(dd_values_all)) if dd_values_all else None
            if dd_idx is not None and dd_idx < len(dates):
                ax_eq.axvspan(dd_start, dd_end, alpha=0.10, color=red_color, zorder=0)
                ax_eq.annotate(
                    f" 最大回撤 {max_dd*100:.1f}% ",
                    xy=(dates[dd_idx], equity[dd_idx]),
                    xytext=(30, -28), textcoords="offset points",
                    fontsize=10, fontweight="bold", color="#ffffff",
                    ha="left", va="top",
                    bbox={"boxstyle": "round,pad=0.3", "fc": red_color, "alpha": 0.85, "ec": "none"},
                    arrowprops={"arrowstyle": "->", "color": red_color, "lw": 1.2},
                )

        ax_eq.set_ylabel("净值", color=text_dim_color, fontsize=10)
        ax_eq.tick_params(colors=text_dim_color, labelsize=9, labelbottom=False)
        ax_eq.grid(True, color=border_color, linestyle="--", alpha=0.4)
        for spine in ax_eq.spines.values():
            spine.set_color(border_color)
        leg = ax_eq.legend(loc="best", framealpha=0.9, facecolor=bg_card_color,
                           edgecolor=border_color, fontsize=9, labelcolor=text_color)
        leg.get_frame().set_linewidth(0.5)

        # 回撤
        dd_values = self._strategy_drawdown_values(equity)
        ax_dd.fill_between(dates, dd_values, 0.0, color=red_color, alpha=0.18)
        ax_dd.plot(dates, dd_values, color=red_color, linewidth=1.4)
        ax_dd.axhline(0.0, color=border_color, linewidth=1.0)
        ax_dd.set_ylabel("回撤 (%)", color=text_dim_color, fontsize=10)
        ax_dd.tick_params(colors=text_dim_color, labelsize=9, labelbottom=True)
        ax_dd.grid(True, color=border_color, linestyle="--", alpha=0.35)
        for spine in ax_dd.spines.values():
            spine.set_color(border_color)
        ax_dd.set_xlabel("日期", color=text_dim_color, fontsize=10)
        for lbl in ax_dd.get_xticklabels():
            lbl.set_rotation(18)
            lbl.set_horizontalalignment("right")

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

    @staticmethod
    def _strategy_period_label(period):
        return f"{period.get('start_date')} → {period.get('end_date')}"

    def _strategy_detail_period_options(self, periods):
        labels = [self._strategy_period_label(period) for period in periods]
        return ["最近一期", "全部", *labels]

    def _strategy_detail_periods(self, periods):
        if not periods:
            return []
        period_var = getattr(self, "v_st_detail_period", None)
        selected = period_var.get() if period_var is not None else "最近一期"
        if selected == "全部":
            return list(periods)
        if selected == "最近一期":
            return [periods[-1]]
        return [
            period for period in periods
            if self._strategy_period_label(period) == selected
        ] or [periods[-1]]

    @staticmethod
    def _strategy_funnel_text(periods, label):
        if not periods:
            return "无调仓数据"
        totals = {
            "eligible_count": sum(int(p.get("eligible_count") or 0) for p in periods),
            "priced_count": sum(int(p.get("priced_count") or 0) for p in periods),
            "candidate_count": sum(int(p.get("candidate_count") or 0) for p in periods),
            "selected_count": sum(int(p.get("selected_count") or 0) for p in periods),
        }
        prefix = label
        if len(periods) == 1:
            prefix = f"{periods[0].get('start_date')}"
        elif label == "全部":
            prefix = f"全部 {len(periods)} 期"
        return (
            f"{prefix}: 合格 {totals['eligible_count']} → "
            f"定价 {totals['priced_count']} → "
            f"候选 {totals['candidate_count']} → "
            f"买入 {totals['selected_count']}"
        )

    def _render_strategy_selection_panel(self, result):
        frame = getattr(self, "strategy_bt_selection_frame", None)
        if frame is None:
            return
        self._clear_strategy_panel(frame)
        periods = result.get("periods") or []
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(0, weight=0)
        frame.grid_rowconfigure(1, weight=0)
        frame.grid_rowconfigure(2, weight=0)

        period_var = getattr(self, "v_st_detail_period", None)
        status_var = getattr(self, "v_st_detail_status", None)
        period_options = self._strategy_detail_period_options(periods)
        if period_var is not None and period_var.get() not in period_options:
            period_var.set("最近一期")
        status_options = ["全部", "买入", "候选", "剔除"]
        if status_var is not None and status_var.get() not in status_options:
            status_var.set("全部")

        filter_bar = ctk.CTkFrame(frame, fg_color="transparent")
        filter_bar.grid(row=0, column=0, sticky="ew", padx=12, pady=(6, 2))
        filter_bar.grid_columnconfigure(4, weight=1)
        ctk.CTkLabel(filter_bar, text="调仓期", text_color=TEXT_DIM,
                     font=(FONT_FAMILY, 11, "bold")).grid(row=0, column=0, sticky="w")
        ctk.CTkOptionMenu(
            filter_bar, variable=period_var, values=period_options,
            command=self._on_strategy_detail_filter_change,
            width=190, height=26, font=(FONT_FAMILY, 11),
            fg_color=BG_INPUT, button_color=BORDER,
            text_color=TEXT, dropdown_fg_color=BG_INPUT,
            dropdown_text_color=TEXT,
        ).grid(row=0, column=1, sticky="w", padx=(6, 16))
        ctk.CTkLabel(filter_bar, text="筛选状态", text_color=TEXT_DIM,
                     font=(FONT_FAMILY, 11, "bold")).grid(row=0, column=2, sticky="w")
        ctk.CTkOptionMenu(
            filter_bar, variable=status_var, values=status_options,
            command=self._on_strategy_detail_filter_change,
            width=92, height=26, font=(FONT_FAMILY, 11),
            fg_color=BG_INPUT, button_color=BORDER,
            text_color=TEXT, dropdown_fg_color=BG_INPUT,
            dropdown_text_color=TEXT,
        ).grid(row=0, column=3, sticky="w", padx=(6, 0))

        selected_periods = self._strategy_detail_periods(periods)
        period_label = period_var.get() if period_var is not None else "最近一期"
        status_filter = status_var.get() if status_var is not None else "全部"
        funnel_text = self._strategy_funnel_text(selected_periods, period_label)
        self._strategy_section_title(frame, f"筛选漏斗 · {funnel_text}", 1, 0)

        candidate_rows = []
        rejection_rows = []
        for period in selected_periods:
            period_label = self._strategy_period_label(period)
            for row in period.get("candidate_rows") or []:
                row_status = "买入" if row.get("selected") else "候选"
                if status_filter != "全部" and row_status != status_filter:
                    continue
                candidate_rows.append([
                    period_label,
                    row_status,
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
                if status_filter not in ("全部", "剔除"):
                    continue
                source = row.get("source") or "剔除"
                reason = row.get("reason") or ""
                reason_text = f"{source}: {reason}" if reason else source
                rejection_rows.append([
                    period_label,
                    "剔除",
                    "",
                    row.get("bond_code", ""),
                    row.get("bond_name", ""),
                    f"{float(row.get('score')):.1f}" if row.get("score") is not None else "—",
                    self._fmt_strategy_price(row.get("market_price")),
                    self._fmt_strategy_pct(row.get("deviation"), sign=True),
                    self._fmt_strategy_pct(row.get("conversion_premium"), sign=True),
                    row.get("confidence", ""),
                    " / ".join(
                        text for text in (
                            reason_text,
                            " / ".join(str(tag) for tag in row.get("risk_tags") or []),
                        ) if text
                    ),
                ])

        # 候选 + 剔除合并为一张表
        all_rows = candidate_rows + rejection_rows
        self._render_strategy_small_tree(
            frame, 2, 0,
            ["period", "status", "rank", "code", "name", "score", "price",
             "dev", "premium", "confidence", "reason"],
            ["区间", "状态", "排名", "代码", "名称", "分数", "价格",
             "偏差", "溢价", "置信", "解释/原因"],
            [150, 58, 44, 88, 88, 56, 64, 68, 68, 52, 340],
            all_rows,
            xscroll=True,
            max_height=STRATEGY_DETAIL_TABLE_HEIGHT,
        )

    def _render_strategy_table(self, result):
        for child in self.strategy_bt_table_frame.winfo_children():
            child.destroy()

        all_periods = result.get("periods") or []
        periods = self._strategy_detail_periods(all_periods)
        if not periods:
            ctk.CTkLabel(
                self.strategy_bt_table_frame,
                text="无持仓明细",
                font=(FONT_FAMILY, 13),
                text_color=TEXT_DIM,
            ).grid(row=0, column=0, padx=12, pady=12, sticky="w")
            return

        self.strategy_bt_table_frame.grid_columnconfigure(0, weight=1)
        self.strategy_bt_table_frame.grid_rowconfigure(0, weight=0)
        self.strategy_bt_table_frame.grid_rowconfigure(1, weight=0)
        self.strategy_bt_table_frame.grid_rowconfigure(2, weight=0)
        self.strategy_bt_table_frame.grid_rowconfigure(3, weight=0)

        self._strategy_section_title(self.strategy_bt_table_frame, "换仓记录", 0, 0)
        summary_rows = []
        name_map: dict[str, str] = {}
        for period in all_periods:
            for pos in period.get("positions") or []:
                code = pos.get("bond_code")
                if code:
                    name_map[str(code)] = pos.get("bond_name") or str(code)
        previous_by_period: dict[str, set[str]] = {}
        previous: set[str] = set()
        for period in all_periods:
            period_label = self._strategy_period_label(period)
            previous_by_period[period_label] = set(previous)
            selected = {str(code) for code in period.get("selected_codes") or []}
            previous = selected
        for period in periods:
            period_label = self._strategy_period_label(period)
            previous = previous_by_period.get(period_label, set())
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
            buy_names = ", ".join(sorted(name_map.get(c, c)[:4] for c in buys)) or "—"
            sell_names = ", ".join(sorted(name_map.get(c, c)[:4] for c in sells)) or "—"
            summary_rows.append([
                period_label,
                self._fmt_strategy_pct(period_return, sign=True),
                self._fmt_strategy_pct(excess, sign=True),
                period.get("selected_count", 0),
                f"{len(buys)}",
                f"{len(sells)}",
                len(holds),
                self._fmt_strategy_pct(period.get("turnover")),
                self._fmt_strategy_pct(period.get("cash_weight")),
                buy_names,
                sell_names,
            ])
        self._render_strategy_small_tree(
            self.strategy_bt_table_frame, 1, 0,
            ["period", "return", "excess", "selected", "buy", "sell", "hold",
             "turnover", "cash", "buy_names", "sell_names"],
            ["区间", "收益(%)", "超额(%)", "持有", "新买", "卖出", "不动",
             "换手", "现金", "买入标的", "卖出标的"],
            [170, 78, 78, 52, 48, 48, 48, 68, 68, 180, 180],
            summary_rows,
            xscroll=True,
            max_height=STRATEGY_DETAIL_TABLE_HEIGHT,
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

        self._strategy_section_title(self.strategy_bt_table_frame, "买卖明细", 2, 0)
        tree = self._render_strategy_small_tree(
            self.strategy_bt_table_frame, 3, 0,
            ["period", "status", "rank", "code", "name", "contrib", "ret",
             "score", "confidence", "entry", "exit", "note"],
            ["区间", "状态", "排名", "代码", "名称", "贡献(%)", "收益(%)",
             "分数", "置信", "买入", "卖出", "标签/原因"],
            [170, 56, 52, 88, 96, 76, 76, 62, 58, 122, 122, 260],
            detail_rows,
            xscroll=True,
            max_height=STRATEGY_DETAIL_TABLE_HEIGHT,
        )
        self._strategy_bt_tree = tree
        _TREE_ATTRS.add("_strategy_bt_tree")

    def _render_strategy_attribution(self, result):
        frame = self.strategy_bt_attribution_frame
        self._clear_strategy_panel(frame)
        diagnostics = result.get("diagnostics") or {}
        attribution = diagnostics.get("attribution") or {}
        summary = result.get("summary") or {}

        frame.grid_columnconfigure(0, weight=1, uniform="strategy_attr_tables")
        frame.grid_columnconfigure(1, weight=1, uniform="strategy_attr_tables")
        frame.grid_rowconfigure(2, minsize=170)
        frame.grid_rowconfigure(4, weight=1, minsize=360)

        metrics = ctk.CTkFrame(frame, fg_color="transparent")
        metrics.grid(row=0, column=0, columnspan=2, sticky="ew", padx=10, pady=(8, 4))
        for i in range(4):
            metrics.grid_columnconfigure(i, weight=1)
        self._strategy_metric_tile(metrics, 0, "交易成本", self._fmt_strategy_pct(attribution.get("cost_drag"), sign=True))
        self._strategy_metric_tile(metrics, 1, "平均现金", self._fmt_strategy_pct(summary.get("avg_cash_weight")))
        self._strategy_metric_tile(metrics, 2, "未成交笔数", str(attribution.get("skipped_positions") or 0))
        self._strategy_metric_tile(metrics, 3, "总交易费", self._fmt_strategy_pct(summary.get("total_cost")))

        self._strategy_section_title(frame, "贡献最大", 1, 0)
        self._strategy_section_title(frame, "拖累最大", 1, 1)
        top_contribs = attribution.get("top_contributors") or []
        top_detractors = attribution.get("top_detractors") or []
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
                for row in top_contribs
            ],
            max_height=STRATEGY_COMPACT_TABLE_HEIGHT,
            stretch_weights={"名称": 1.7, "贡献(%)": 0.7, "期数": 0.4},
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
                for row in top_detractors
            ],
            max_height=STRATEGY_COMPACT_TABLE_HEIGHT,
            stretch_weights={"名称": 1.7, "贡献(%)": 0.7, "期数": 0.4},
        )

        # 个券贡献瀑布图 + 月度/全年收益热力图 (年度收益已并入热力图右端「全年」列)
        self._strategy_section_title(frame, "个券贡献", 3, 0)
        self._strategy_section_title(frame, "月度 / 全年收益", 3, 1)
        self._render_attribution_charts(
            frame, 4,
            top_contribs, top_detractors,
            diagnostics.get("yearly_returns") or [],
            diagnostics.get("monthly_returns") or [],
        )

    def _render_attribution_charts(self, frame, row,
                                   top_contribs, top_detractors,
                                   yearly_returns, monthly_returns):
        """左: 个券贡献瀑布图(独占整列); 右: 月度收益热力图 + 全年列(紧凑置顶)."""
        bg_card_color = get_color(BG_CARD)
        bg_input_color = get_color(BG_INPUT)
        text_dim_color = get_color(TEXT_DIM)
        text_color = get_color(TEXT)
        border_color = get_color(BORDER)
        green_color = get_color(GREEN)
        red_color = get_color(RED)

        chart_shell = ctk.CTkFrame(frame, fg_color="transparent")
        chart_shell.grid(row=row, column=0, columnspan=2, sticky="nsew", padx=4, pady=(0, 8))
        chart_shell.grid_columnconfigure(0, weight=1, uniform="attr_chart")
        chart_shell.grid_columnconfigure(1, weight=1, uniform="attr_chart")
        chart_shell.grid_rowconfigure(0, weight=1)

        # ── 左列: 个券贡献瀑布图 (年度表已并入右侧「全年」列, 此处独占整列加宽) ──
        left = ctk.CTkFrame(chart_shell, fg_color="transparent")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8), pady=0)
        left.grid_columnconfigure(0, weight=1)
        left.grid_rowconfigure(0, weight=1)

        waterfall_items = (
            [(r.get("bond_name") or r.get("bond_code", "")[:6],
              float(r.get("contribution") or 0)) for r in top_contribs[:5]]
            + [(r.get("bond_name") or r.get("bond_code", "")[:6],
                float(r.get("contribution") or 0)) for r in top_detractors[:5]]
        )
        waterfall_items = [(n, v) for n, v in waterfall_items if abs(v) > 1e-8]
        waterfall_items.sort(key=lambda x: x[1], reverse=True)
        if waterfall_items:
            wf_frame = ctk.CTkFrame(left, fg_color="transparent")
            wf_frame.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)
            wf_frame.grid_columnconfigure(0, weight=1)
            wf_frame.grid_rowconfigure(0, weight=1)
            wf_h = max(2.8, 0.34 * len(waterfall_items) + 1.1)
            fig_wf = Figure(figsize=(7.2, wf_h), dpi=100, facecolor=bg_card_color)
            ax_wf = fig_wf.add_subplot(111, facecolor=bg_input_color)
            names = [n[:6] for n, _ in waterfall_items]
            vals = [v * 100 for _, v in waterfall_items]
            wf_colors = [green_color if v >= 0 else red_color for v in vals]
            ax_wf.barh(range(len(names)), vals, color=wf_colors, alpha=0.85, height=0.66)
            ax_wf.set_yticks(range(len(names)))
            ax_wf.set_yticklabels(names, fontsize=8, color=text_color)
            ax_wf.set_xlabel("贡献 (%)", color=text_dim_color, fontsize=9)
            ax_wf.axvline(0, color=border_color, linewidth=0.8)
            ax_wf.tick_params(colors=text_dim_color, labelsize=8)
            ax_wf.grid(True, axis="x", color=border_color, linestyle="--", alpha=0.3)
            for spine in ax_wf.spines.values():
                spine.set_color(border_color)
            ax_wf.invert_yaxis()
            # 数值标签直接标在条端, 省去对照刻度
            span = (max(vals) - min(vals)) or 1.0
            pad = 0.012 * span
            for yi, v in enumerate(vals):
                ax_wf.text(v + (pad if v >= 0 else -pad), yi, f"{v:+.1f}",
                           va="center", ha="left" if v >= 0 else "right",
                           fontsize=7.5, color=text_dim_color)
            ax_wf.margins(x=0.12)
            fig_wf.subplots_adjust(left=0.20, right=0.97, bottom=0.16, top=0.97)
            canvas_wf = FigureCanvasTkAgg(fig_wf, master=wf_frame)
            canvas_wf.draw()
            canvas_wf.get_tk_widget().grid(row=0, column=0, sticky="nsew")
            self._strategy_bt_waterfall_fig = fig_wf
        else:
            ctk.CTkLabel(
                left, text="暂无贡献数据", text_color=TEXT_DIM,
                font=(FONT_FAMILY, 12),
            ).grid(row=0, column=0, sticky="nsew", padx=12, pady=12)

        # ── 右列: 月度收益热力图 + 全年列 (紧凑置顶, 不随行高拉伸) ──
        right = ctk.CTkFrame(chart_shell, fg_color="transparent")
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 0), pady=0)
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(0, weight=0)   # 热力图按内容高度
        right.grid_rowconfigure(1, weight=1)   # 占位行吸收多余高度, 避免色块被纵向拉伸

        if not monthly_returns:
            ctk.CTkLabel(right, text="暂无月度数据", text_color=TEXT_DIM,
                         font=(FONT_FAMILY, 12)).grid(row=0, column=0, sticky="new",
                                                      padx=12, pady=12)
            return

        year_month_map: dict[int, dict[int, float]] = {}
        for mr in monthly_returns:
            period_str = mr.get("period", "")
            ret = mr.get("return")
            if ret is None or not period_str:
                continue
            try:
                parts = period_str.split("-")
                year, month = int(parts[0]), int(parts[1])
            except (ValueError, IndexError):
                continue
            year_month_map.setdefault(year, {})[month] = float(ret)

        if not year_month_map:
            ctk.CTkLabel(right, text="月度数据解析为空", text_color=TEXT_DIM,
                         font=(FONT_FAMILY, 12)).grid(row=0, column=0, sticky="new",
                                                      padx=12, pady=12)
            return

        years = sorted(year_month_map.keys())
        data = np.full((len(years), 12), np.nan)
        for yi, y in enumerate(years):
            for m, v in year_month_map[y].items():
                if 1 <= m <= 12:
                    data[yi, m - 1] = v * 100

        # 全年收益: 优先取 yearly_returns, 缺失年份用当年月度复利兜底
        annual_map: dict[int, float] = {}
        for row_d in yearly_returns or []:
            try:
                y = int(str(row_d.get("period", "")).split("-")[0])
            except (ValueError, IndexError):
                continue
            rv = row_d.get("return")
            if rv is None:
                continue
            try:
                annual_map[y] = float(rv) * 100
            except (TypeError, ValueError):
                continue
        annual = np.full((len(years), 1), np.nan)
        for yi, y in enumerate(years):
            if y in annual_map:
                annual[yi, 0] = annual_map[y]
            elif year_month_map[y]:
                comp = 1.0
                for v in year_month_map[y].values():
                    comp *= (1.0 + v)
                annual[yi, 0] = (comp - 1.0) * 100

        n_years = len(years)
        fig_h = 1.25 + 0.6 * n_years
        fig_hm = Figure(figsize=(6.6, fig_h), dpi=100, facecolor=bg_card_color)
        gs = fig_hm.add_gridspec(
            1, 2, width_ratios=[12, 1.5], wspace=0.08,
            left=0.085, right=0.9, bottom=0.42 / fig_h, top=1 - 0.26 / fig_h)
        ax_hm = fig_hm.add_subplot(gs[0, 0], facecolor=bg_input_color)
        ax_yr = fig_hm.add_subplot(gs[0, 1], facecolor=bg_input_color, sharey=ax_hm)
        cmap = LinearSegmentedColormap.from_list("rg", [red_color, bg_input_color, green_color])

        # 月度色块 (独立标尺, 不被全年大幅收益拉爆色阶)
        vmax = max(3.0, float(np.nanmax(np.abs(data)))) if np.any(np.isfinite(data)) else 5.0
        im = ax_hm.imshow(data, aspect="auto", cmap=cmap, vmin=-vmax, vmax=vmax,
                          interpolation="nearest")
        ax_hm.set_xticks(range(12))
        ax_hm.set_xticklabels([f"{m+1}月" for m in range(12)], fontsize=7,
                              color=text_dim_color)
        ax_hm.set_yticks(range(len(years)))
        ax_hm.set_yticklabels([str(y) for y in years], fontsize=8, color=text_color)
        ax_hm.tick_params(length=0)
        for spine in ax_hm.spines.values():
            spine.set_visible(False)
        for yi in range(len(years)):
            for mi in range(12):
                val = data[yi, mi]
                if np.isfinite(val):
                    ax_hm.text(mi, yi, f"{val:+.1f}", ha="center", va="center",
                               fontsize=6.5, color=text_color,
                               fontweight="bold" if abs(val) >= vmax * 0.5 else "normal")

        # 全年色块 (独立标尺 + 边框分隔, 视觉上与月度区分)
        a_vmax = (max(5.0, float(np.nanmax(np.abs(annual))))
                  if np.any(np.isfinite(annual)) else 10.0)
        ax_yr.imshow(annual, aspect="auto", cmap=cmap, vmin=-a_vmax, vmax=a_vmax,
                     interpolation="nearest")
        ax_yr.set_xticks([0])
        ax_yr.set_xticklabels(["全年"], fontsize=7.5, color=text_color)
        ax_yr.tick_params(length=0, labelleft=False)
        for spine in ax_yr.spines.values():
            spine.set_visible(True)
            spine.set_color(border_color)
            spine.set_linewidth(0.8)
        for yi in range(len(years)):
            val = annual[yi, 0]
            if np.isfinite(val):
                ax_yr.text(0, yi, f"{val:+.1f}", ha="center", va="center",
                           fontsize=8, color=text_color, fontweight="bold")

        cb = fig_hm.colorbar(im, ax=[ax_hm, ax_yr], fraction=0.045, pad=0.04)
        cb.ax.tick_params(colors=text_dim_color, labelsize=7)
        cb.set_label("月度 %", color=text_dim_color, fontsize=8)
        canvas_hm = FigureCanvasTkAgg(fig_hm, master=right)
        canvas_hm.draw()
        canvas_hm.get_tk_widget().grid(row=0, column=0, sticky="new")
        self._strategy_bt_heatmap_fig = fig_hm

    def _render_strategy_risk_panel(self, result):
        """风险 tab: 合并原风险 + 稳健性 + 数据可信度."""
        frame = self.strategy_bt_risk_frame
        self._clear_strategy_panel(frame)
        diagnostics = result.get("diagnostics") or {}
        summary = result.get("summary") or {}
        attribution = diagnostics.get("attribution") or {}
        data_quality = diagnostics.get("data_quality") or {}
        warnings = diagnostics.get("warnings") or []
        periods = result.get("periods") or []

        frame.grid_columnconfigure(0, weight=1)
        frame.grid_columnconfigure(1, weight=1)
        frame.grid_rowconfigure(2, minsize=240)
        frame.grid_rowconfigure(4, minsize=300)

        # ── Row 0: 稳健性指标条 ──────────────────────────────────
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

        # 可信度 badge
        if fallback_ratio <= 0:
            q_text, q_color = "高", get_color(GREEN)
        elif fallback_ratio <= 0.2:
            q_text, q_color = "中", get_color(ORANGE)
        else:
            q_text, q_color = "低", get_color(RED)

        metrics = ctk.CTkFrame(frame, fg_color="transparent")
        metrics.grid(row=0, column=0, columnspan=2, sticky="ew", padx=10, pady=(8, 4))
        for i in range(7):
            metrics.grid_columnconfigure(i, weight=1)
        self._strategy_metric_tile(metrics, 0, "区间胜率", self._fmt_strategy_pct(win_rate))
        self._strategy_metric_tile(metrics, 1, "最大回撤", self._fmt_strategy_pct(summary.get("max_drawdown")))
        self._strategy_metric_tile(metrics, 2, "年化波动", self._fmt_strategy_pct(summary.get("annualized_volatility")))
        self._strategy_metric_tile(metrics, 3, "最好单期", self._fmt_strategy_pct(best, sign=True))
        self._strategy_metric_tile(metrics, 4, "最差单期", self._fmt_strategy_pct(worst, sign=True))
        self._strategy_metric_tile(metrics, 5, "前三集中度", self._fmt_strategy_pct(concentration))
        self._strategy_metric_tile(metrics, 6, f"可信度: {q_text}", self._fmt_strategy_pct(fallback_ratio))

        # ── Row 1: 左=风险提示+回撤画像, 右=稳健性建议 ──────────────
        left = ctk.CTkFrame(frame, fg_color="transparent")
        left.grid(row=1, column=0, sticky="nsew", padx=12, pady=(6, 4))

        ctk.CTkLabel(left, text="风险提醒", text_color=TEXT,
                     font=(FONT_FAMILY, 13, "bold")).pack(anchor="w")
        if warnings:
            critical_keywords = ("大幅", "异常", "失败", "不足", "极端")
            for warning in warnings:
                is_critical = any(kw in warning for kw in critical_keywords)
                color = RED if is_critical else ORANGE
                prefix = "🔴" if is_critical else "🟡"
                ctk.CTkLabel(
                    left, text=f"{prefix} {warning}", text_color=color,
                    font=(FONT_FAMILY, 11), justify="left", wraplength=460,
                ).pack(anchor="w", pady=(3, 0))
        else:
            ctk.CTkLabel(left, text="🟢 暂无明显风险提示", text_color=TEXT_DIM,
                         font=(FONT_FAMILY, 11)).pack(anchor="w", pady=(3, 0))

        dd_items = [
            ("回撤区间", f"{summary.get('max_drawdown_start') or '—'} → {summary.get('max_drawdown_end') or '—'}"),
            ("持续天数", f"{summary.get('max_drawdown_days') or 0} 天 (最长 {summary.get('longest_drawdown_days') or 0} 天)"),
        ]
        for label, value in dd_items:
            row_w = ctk.CTkFrame(left, fg_color="transparent")
            row_w.pack(fill="x", pady=(3, 0))
            ctk.CTkLabel(row_w, text=label, text_color=TEXT_DIM,
                         font=(FONT_FAMILY, 11), width=70, anchor="w").pack(side="left")
            ctk.CTkLabel(row_w, text=str(value), text_color=TEXT,
                         font=(FONT_MONO, 11), anchor="w").pack(side="left")

        right = ctk.CTkFrame(frame, fg_color="transparent")
        right.grid(row=1, column=1, sticky="nsew", padx=12, pady=(6, 4))

        notes = self._strategy_robustness_notes(
            summary=summary, win_rate=win_rate, worst=worst,
            concentration=concentration, fallback_ratio=fallback_ratio,
        )
        suggestions = self._strategy_dynamic_suggestions(
            summary=summary, win_rate=win_rate, worst=worst,
            concentration=concentration, ret_std=ret_std,
        )
        ctk.CTkLabel(right, text="改进建议", text_color=TEXT,
                     font=(FONT_FAMILY, 13, "bold")).pack(anchor="w")
        for note in notes:
            ctk.CTkLabel(right, text=f"• {note}", text_color=TEXT_DIM,
                         font=(FONT_FAMILY, 11), justify="left",
                         wraplength=460).pack(anchor="w", pady=(3, 0))
        for text in suggestions[:3]:
            ctk.CTkLabel(right, text=f"→ {text}", text_color=ACCENT,
                         font=(FONT_FAMILY, 11), justify="left",
                         wraplength=460).pack(anchor="w", pady=(3, 0))

        # ── Row 2: 滚动风险图 ──────────────────────────────────
        self._render_rolling_risk_chart(frame, 2, periods, result.get("equity_curve") or [])

        # ── Row 3: 收益分布 + 最差区间复盘 ──────────────────────
        self._strategy_section_title(frame, "收益分布 / 最差区间", 3, 0, columnspan=2)
        dist_and_worst = ctk.CTkFrame(
            frame, fg_color="transparent", height=STRATEGY_SECONDARY_CHART_HEIGHT)
        dist_and_worst.grid(row=4, column=0, columnspan=2, sticky="ew", padx=4, pady=(0, 8))
        dist_and_worst.grid_columnconfigure(0, weight=4, minsize=520)
        dist_and_worst.grid_columnconfigure(1, weight=5, minsize=620)
        dist_and_worst.grid_rowconfigure(0, weight=1)
        dist_and_worst.grid_propagate(False)

        if returns:
            dist_frame = ctk.CTkFrame(dist_and_worst, fg_color="transparent")
            dist_frame.grid(row=0, column=0, sticky="nsew", padx=(4, 10), pady=4)
            dist_frame.grid_columnconfigure(0, weight=1)
            dist_frame.grid_rowconfigure(0, weight=1)
            bg_card_c = get_color(BG_CARD)
            bg_input_c = get_color(BG_INPUT)
            text_dim_c = get_color(TEXT_DIM)
            border_c = get_color(BORDER)
            green_c = get_color(GREEN)
            red_c = get_color(RED)

            fig_dist = Figure(figsize=(5.2, 2.8), dpi=100, facecolor=bg_card_c)
            ax_dist = fig_dist.add_subplot(111, facecolor=bg_input_c)
            ret_pct = [r * 100 for r in returns]
            n_bins = min(20, max(5, len(ret_pct) // 3))
            n, bins, patches = ax_dist.hist(ret_pct, bins=n_bins, alpha=0.75, edgecolor=border_c)
            for patch, left_edge in zip(patches, bins):
                patch.set_facecolor(green_c if left_edge >= 0 else red_c)
            ax_dist.axvline(0, color=border_c, linewidth=1.0, linestyle="--")
            median_r = float(np.median(ret_pct))
            ax_dist.axvline(median_r, color=get_color(ACCENT), linewidth=1.2,
                            linestyle=":", label=f"中位数 {median_r:.1f}%")
            ax_dist.set_xlabel("区间收益 (%)", color=text_dim_c, fontsize=9)
            ax_dist.set_ylabel("频次", color=text_dim_c, fontsize=9)
            ax_dist.tick_params(colors=text_dim_c, labelsize=8)
            ax_dist.grid(True, axis="y", color=border_c, linestyle="--", alpha=0.3)
            for spine in ax_dist.spines.values():
                spine.set_color(border_c)
            leg_dist = ax_dist.legend(loc="best", framealpha=0.9, facecolor=bg_card_c,
                                      edgecolor=border_c, fontsize=8,
                                      labelcolor=get_color(TEXT))
            leg_dist.get_frame().set_linewidth(0.5)
            fig_dist.tight_layout()
            canvas_dist = FigureCanvasTkAgg(fig_dist, master=dist_frame)
            canvas_dist.draw()
            canvas_dist.get_tk_widget().grid(row=0, column=0, sticky="nsew")
            self._strategy_bt_dist_fig = fig_dist

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
                self._strategy_codes_text(period.get("selected_codes") or []),
            ])
        self._render_strategy_small_tree(
            dist_and_worst, 0, 1,
            ["period", "ret", "excess", "turnover", "codes"],
            ["区间", "收益", "超额", "换手", "持仓"],
            [138, 66, 66, 60, 760],
            worst_rows,
            xscroll=True,
            max_height=STRATEGY_DETAIL_TABLE_HEIGHT,
            stretch_weights={"持仓": 6.0, "区间": 1.2, "收益": 0.3, "超额": 0.3, "换手": 0.3},
        )

    @staticmethod
    def _daily_curve_returns(equity_curve):
        """从日频净值曲线提取 (日期, 日收益) 序列.

        与总览净值曲线、核心 ``daily_mtm`` 波动率口径同源。逐日 MTM 步长均匀,
        天然不受回测末尾"残桩区间"(不足整周期的几天)影响, 故按时间口径的滚动
        风险走势基于此, 而非等权的调仓区间收益。
        """
        dates, eqs = [], []
        for row in equity_curve or []:
            d = row.get("date")
            e = row.get("equity")
            if d is None or e is None:
                continue
            try:
                e = float(e)
            except (TypeError, ValueError):
                continue
            if not np.isfinite(e) or e <= 0:
                continue
            dates.append(d)
            eqs.append(e)
        r_dates, rets = [], []
        for i in range(1, len(eqs)):
            if eqs[i - 1] > 0:
                r_dates.append(dates[i])
                rets.append(eqs[i] / eqs[i - 1] - 1.0)
        return r_dates, rets

    def _render_rolling_risk_chart(self, frame, grid_row, periods, equity_curve):
        """滚动年化波动率 + 滚动 Sharpe 走势.

        基于日频净值曲线 (与总览净值图、年化波动/Sharpe 指标同口径), 而非等权的
        调仓区间收益 —— 这样步长均匀, 不会因末尾不足整周期的残桩区间在末端跳变。
        """
        dates, dret = self._daily_curve_returns(equity_curve)
        # 滚动窗口按"月"定义: 默认近 3 个月 (≈63 个交易日); 样本不足时按月收窄
        days_per_month = 21
        months = 3
        window = months * days_per_month
        while months > 1 and len(dret) < window + 1:
            months -= 1
            window = months * days_per_month
        if len(dret) < max(window + 1, 25):
            ctk.CTkLabel(frame, text="日频样本不足, 无法计算滚动风险",
                         text_color=TEXT_DIM, font=(FONT_FAMILY, 12)).grid(
                             row=grid_row, column=0, columnspan=2, sticky="w", padx=12, pady=6)
            return

        bg_card_color = get_color(BG_CARD)
        bg_input_color = get_color(BG_INPUT)
        text_dim_color = get_color(TEXT_DIM)
        border_color = get_color(BORDER)
        accent_color = get_color(ACCENT)
        orange_color = get_color(ORANGE)

        arr = np.asarray(dret, dtype=float)
        ann = float(np.sqrt(252.0))
        roll_dates, roll_vol, roll_sharpe = [], [], []
        for i in range(window - 1, len(arr)):
            chunk = arr[i - window + 1: i + 1]
            sd = float(np.std(chunk, ddof=1))
            roll_vol.append(sd * ann * 100.0)
            roll_sharpe.append(float(np.mean(chunk)) / sd * ann if sd > 1e-12 else 0.0)
            roll_dates.append(dates[i])

        chart_frame = ctk.CTkFrame(
            frame, fg_color="transparent", height=STRATEGY_RISK_CHART_HEIGHT)
        chart_frame.grid(row=grid_row, column=0, columnspan=2, sticky="nsew", padx=8, pady=(4, 8))
        chart_frame.grid_columnconfigure(0, weight=1)
        chart_frame.grid_rowconfigure(0, weight=1)
        chart_frame.grid_propagate(False)

        fig = Figure(figsize=(10, 2.2), dpi=100, facecolor=bg_card_color)
        ax1 = fig.add_subplot(121, facecolor=bg_input_color)
        ax2 = fig.add_subplot(122, facecolor=bg_input_color)

        ax1.plot(roll_dates, roll_vol, color=orange_color, linewidth=1.5)
        ax1.set_ylabel("年化波动率 (%)", color=text_dim_color, fontsize=9)
        ax1.set_title(f"滚动年化波动率 ({months} 个月)", color=text_dim_color, fontsize=9)
        ax1.tick_params(colors=text_dim_color, labelsize=7)
        ax1.grid(True, color=border_color, linestyle="--", alpha=0.3)
        for spine in ax1.spines.values():
            spine.set_color(border_color)
        for lbl in ax1.get_xticklabels():
            lbl.set_rotation(20)
            lbl.set_horizontalalignment("right")

        ax2.plot(roll_dates, roll_sharpe, color=accent_color, linewidth=1.5)
        ax2.axhline(0, color=border_color, linewidth=0.8)
        ax2.set_ylabel("Sharpe (年化)", color=text_dim_color, fontsize=9)
        ax2.set_title(f"滚动 Sharpe ({months} 个月)", color=text_dim_color, fontsize=9)
        ax2.tick_params(colors=text_dim_color, labelsize=7)
        ax2.grid(True, color=border_color, linestyle="--", alpha=0.3)
        finite_sharpe = [v for v in roll_sharpe if np.isfinite(v)]
        if finite_sharpe:
            s_min, s_max = min(finite_sharpe), max(finite_sharpe)
            pad = max(0.3, (s_max - s_min) * 0.15)
            ax2.set_ylim(s_min - pad, s_max + pad)
        for spine in ax2.spines.values():
            spine.set_color(border_color)
        for lbl in ax2.get_xticklabels():
            lbl.set_rotation(20)
            lbl.set_horizontalalignment("right")

        fig.tight_layout()
        canvas = FigureCanvasTkAgg(fig, master=chart_frame)
        canvas.draw()
        canvas.get_tk_widget().grid(row=0, column=0, sticky="nsew")
        self._strategy_bt_rolling_fig = fig

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

    @staticmethod
    def _strategy_dynamic_suggestions(*, summary, win_rate, worst, concentration, ret_std):
        suggestions = []
        avg_turnover = summary.get("avg_turnover")
        if avg_turnover is not None and float(avg_turnover) >= 0.6:
            suggestions.append("换手偏高 → 把交易成本调到 30~50 bps 检查收益是否大幅缩水")
        elif avg_turnover is not None and float(avg_turnover) < 0.3:
            suggestions.append("换手很低 → 尝试缩短调仓频率 (周频) 看是否能捕获更多机会")
        if win_rate is not None and win_rate < 0.45:
            suggestions.append("胜率偏低 → 把 TopN 减少 2~3 档, 提高选债集中度")
        if concentration is not None and concentration >= 0.65:
            suggestions.append("收益集中 → 把 TopN 增加到 15~20, 分散个券依赖风险")
        if worst is not None and worst <= -0.1:
            suggestions.append("极端亏损 → 尝试加价格上限 (如 ≤130), 控制高位入场风险")
        if ret_std is not None and ret_std > 0.05:
            suggestions.append("波动偏大 → 加转股溢价率上限, 筛掉高弹性高波动标的")
        sharpe = summary.get("sharpe")
        if sharpe is not None and float(sharpe) < 0.5:
            suggestions.append("Sharpe 偏低 → 切换选债规则 (综合机会 vs 低估候选) 做对比")
        if not suggestions:
            suggestions.append("各项指标尚可, 用快速模式把 TopN 上下浮动一档加入对比验证")
        suggestions.append("切到精确模式 (M/N 调大) 复核最终候选策略")
        return suggestions

    def _render_strategy_data_panel(self, result):
        """数据质量 + 回测参数 + 各期质量表."""
        frame = getattr(self, "strategy_bt_data_frame", None)
        if frame is None:
            return
        self._clear_strategy_panel(frame)
        diagnostics = result.get("diagnostics") or {}
        data_quality = diagnostics.get("data_quality") or {}
        performance = diagnostics.get("performance") or {}
        config = result.get("config") or {}
        periods = result.get("periods") or []

        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(3, weight=1)

        # ── 顶部指标条 (一行 5 个 tile) ──────────────────────────
        fallback_ratio = float(data_quality.get("current_fallback_ratio") or 0.0)
        if fallback_ratio <= 0:
            quality, q_color = "高", get_color(GREEN)
        elif fallback_ratio <= 0.2:
            quality, q_color = "中", get_color(ORANGE)
        else:
            quality, q_color = "低", get_color(RED)

        sample_count = data_quality.get("sample_count") or 0
        patch_count = data_quality.get("patch_applied_count") or 0
        event_count = data_quality.get("event_applied_count") or 0
        source_counts = data_quality.get("source_counts") or {}
        if isinstance(source_counts, dict) and source_counts:
            source_labels = {
                "current_fallback": "当前回退",
                "history_snapshot": "历史快照",
                "provider_history": "实时历史",
            }
            source_text = " / ".join(
                f"{source_labels.get(str(k), str(k))} {v}"
                for k, v in source_counts.items()
            )
        else:
            source_text = "全部回退" if fallback_ratio >= 0.99 else "—"

        tiles_row = ctk.CTkFrame(frame, fg_color="transparent")
        tiles_row.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))
        for c in range(5):
            tiles_row.grid_columnconfigure(c, weight=1)

        def _data_tile(col, title, value, *, color=None):
            border_c = ACCENT if col == 0 else BORDER
            cell = ctk.CTkFrame(tiles_row, fg_color=BG_INPUT, corner_radius=8,
                                border_width=1, border_color=border_c)
            cell.grid(row=0, column=col, sticky="nsew", padx=4, pady=2)
            inner = ctk.CTkFrame(cell, fg_color="transparent")
            inner.pack(fill="both", expand=True, padx=10, pady=6)
            ctk.CTkLabel(inner, text=title, text_color=TEXT_DIM,
                         font=(FONT_FAMILY, 10, "bold")).pack(anchor="w")
            ctk.CTkLabel(inner, text=str(value),
                         text_color=color or TEXT,
                         font=(FONT_MONO, 15, "bold")).pack(anchor="w", pady=(2, 0))

        _data_tile(0, "数据质量", quality, color=q_color)
        _data_tile(1, "条款样本", f"{sample_count:,}")
        _data_tile(2, "条款回退", self._fmt_strategy_pct(fallback_ratio))
        _data_tile(3, "转股价修正", f"{patch_count:,}")
        _data_tile(4, "公告修正", f"{event_count:,}")

        # ── 中部: 条款来源 / 运算缓存 / 策略参数 / 成本基准 ─────────
        mid = ctk.CTkFrame(frame, fg_color="transparent")
        mid.grid(row=1, column=0, sticky="ew", padx=8, pady=(4, 4))
        for c in range(4):
            mid.grid_columnconfigure(c, weight=1, uniform="strategy_data_info")

        def _info_card(col, title, value, meta=None, *, color=None):
            card = ctk.CTkFrame(
                mid, fg_color=BG_INPUT, corner_radius=8, height=64,
                border_width=1, border_color=BORDER)
            card.grid(row=0, column=col, sticky="nsew", padx=4, pady=2)
            card.grid_propagate(False)
            inner = ctk.CTkFrame(card, fg_color="transparent")
            inner.pack(fill="both", expand=True, padx=12, pady=7)
            ctk.CTkLabel(
                inner, text=title, text_color=TEXT_DIM,
                font=(FONT_FAMILY, 10, "bold")).pack(anchor="w")
            value_label = ctk.CTkLabel(
                inner, text=str(value or "—"), text_color=color or TEXT,
                font=(FONT_FAMILY, 12, "bold"), justify="left", wraplength=360)
            value_label.pack(anchor="w", pady=(2, 0))
            meta_label = None
            if meta:
                meta_label = ctk.CTkLabel(
                    inner, text=str(meta), text_color=TEXT_DIM,
                    font=(FONT_FAMILY, 10), justify="left", wraplength=360)
                meta_label.pack(anchor="w", pady=(1, 0))

            def _update_wrap(event, labels=(value_label, meta_label)):
                for label in labels:
                    if label is not None:
                        label.configure(wraplength=max(120, event.width - 24))

            card.bind("<Configure>", _update_wrap)

        history_mode = config.get("history_mode") or "标准"
        perf_parts = []
        hits = performance.get("pricing_snapshot_hits")
        misses = performance.get("pricing_snapshot_misses")
        excluded = performance.get("price_prefilter_excluded")
        if hits is not None:
            perf_parts.append(f"命中 {hits}")
        if misses is not None:
            perf_parts.append(f"未命中 {misses}")
        if excluded is not None:
            perf_parts.append(f"预筛 {excluded}")
        perf_text = " · ".join(perf_parts) if perf_parts else "—"
        freq_map = {"M": "月频", "W": "周频", "Q": "季频", "D": "日频",
                    "月": "月频", "周": "周频", "季": "季频", "日": "日频"}
        freq_value = config.get("rebalance_freq")
        freq_text = freq_map.get(str(freq_value), str(freq_value or "—"))
        top_n = config.get("top_n")
        holding_mode = config.get("holding_mode")
        funding_mode = config.get("funding_mode")
        if not funding_mode:   # 兼容旧快照: 由 top_n_shortfall_policy 推断
            legacy = str(config.get("top_n_shortfall_policy") or "cash")
            funding_mode = "full_invest" if legacy in ("renormalize", "full_invest") else "reserve_cash"
        strategy_text = f"{config.get('selection_view') or '—'} · {freq_text}"
        if holding_mode == "pool":
            cap = config.get("max_holdings")
            strategy_text += " · 等权全池" + (f"(≤{int(cap)})" if cap else "")
        elif top_n is not None:   # top_score 或旧快照
            strategy_text += f" · 机会分Top{top_n}"
        shortfall_text = "满仓等权" if funding_mode == "full_invest" else "缺口留现金"
        cost = config.get("transaction_cost")
        try:
            cost_text = f"{float(cost) * 10000:.0f} bps"
        except (TypeError, ValueError):
            cost_text = "—"
        benchmark_text = "等权基准" if config.get("compute_benchmark") else "不对标"

        _info_card(0, "条款来源", source_text, f"历史口径 {history_mode}")
        _info_card(1, "运算缓存", perf_text)
        _info_card(2, "策略参数", strategy_text, shortfall_text)
        _info_card(3, "成本 / 基准", cost_text, benchmark_text)

        # 逐期数据口径
        period_rows = []
        for period in periods:
            dq = period.get("data_quality") or {}
            fb = dq.get("current_fallback_ratio")
            fb_pct = self._fmt_strategy_pct(fb)
            if fb is not None and float(fb) > 0.3:
                fb_pct = f"⚠ {fb_pct}"
            period_rows.append([
                period.get("start_date", ""),
                period.get("eligible_count", 0),
                period.get("candidate_count", 0),
                period.get("selected_count", 0),
                fb_pct,
                dq.get("patch_applied_count", 0),
                dq.get("event_applied_count", 0),
            ])
        self._strategy_section_title(frame, "各期数据质量", 2, 0)
        self._render_strategy_small_tree(
            frame, 3, 0,
            ["date", "eligible", "candidate", "selected", "fallback", "patch", "event"],
            ["换仓日", "可交易", "待选", "入选", "条款回退%", "转股价修正", "公告修正"],
            [100, 70, 70, 70, 92, 70, 70],
            period_rows,
            max_height=STRATEGY_DATA_TABLE_HEIGHT,
        )

    def _record_strategy_comparison_result(self, result):
        summary = result.get("summary") or {}
        config = result.get("config") or {}
        try:
            template = self.v_st_template.get()
            view = self.v_st_view.get()
            freq = self.v_st_freq.get()
            top_n = self.v_st_top_n.get()
        except Exception:
            template = "—"
            view = config.get("selection_view", "—")
            freq = config.get("rebalance_freq", "—")
            top_n = config.get("top_n", "—")
        label = (
            f"{template} · {config.get('selection_view') or view} · "
            f"{freq}频 Top{config.get('top_n') or top_n}"
        )
        records = list(getattr(self, "_strategy_compare_results", []) or [])
        key = (
            str(result.get("start_date")),
            str(result.get("end_date")),
            label,
            summary.get("final_equity"),
            summary.get("max_drawdown"),
        )
        snapshot_id = result.get("_snapshot_id")
        if not snapshot_id:
            try:
                snapshot_id = self._strategy_snapshot_dedupe_key({}, result)
            except Exception:
                snapshot_id = None
        records = [
            row for row in records
            if row.get("key") != key and (not snapshot_id or row.get("snapshot_id") != snapshot_id)
        ]
        records.append({
            "key": key,
            "snapshot_id": snapshot_id,
            "label": label,
            "result": result,
            "snapshot_path": result.get("_snapshot_path"),
        })
        self._strategy_compare_results = records[-self._MAX_SNAPSHOTS:]

    def _clear_strategy_comparison(self):
        from tkinter import messagebox
        if not messagebox.askyesno("确认", "清空所有对比记录并删除磁盘快照?"):
            return
        records = list(getattr(self, "_strategy_compare_results", []) or [])
        for record in records:
            self._delete_strategy_snapshot_for_record(record)
        self._delete_strategy_snapshot_path(self._strategy_snapshot_path())
        self._strategy_compare_results = []
        self._clear_active_strategy_backtest_result(status="已清空策略对比和当前回测结果")

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
        frame.grid_rowconfigure(0, weight=0)
        frame.grid_rowconfigure(1, weight=0)
        frame.grid_rowconfigure(2, weight=1)

        header = ctk.CTkFrame(frame, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=12, pady=(8, 2))
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            header, text="最近策略对比", text_color=TEXT,
            font=(FONT_FAMILY, 14, "bold"),
        ).grid(row=0, column=0, sticky="w")
        self._strategy_compare_selection_text = ctk.StringVar(value="")
        ctk.CTkLabel(
            header, textvariable=self._strategy_compare_selection_text,
            text_color=TEXT_DIM, font=(FONT_FAMILY, 11),
        ).grid(row=0, column=1, sticky="e", padx=(8, 10))
        delete_btn = ctk.CTkButton(
            header, text="删除选中",
            command=lambda: self._delete_selected_comparison(),
            fg_color="transparent", hover_color=get_color(BG_INPUT),
            border_width=1, border_color=RED,
            text_color=RED, font=(FONT_FAMILY, 11, "bold"),
            width=88, height=26, corner_radius=6,
        )
        delete_btn.grid(row=0, column=2, sticky="e")
        self.btn_strategy_compare_delete = delete_btn
        Tooltip(delete_btn, "删除下方表格中选中的对比记录\n支持 Shift / Command 多选")

        rows = []
        best_idx = self._best_strategy_record_index(records)
        col_best = {"ann": [], "ret": [], "excess": [], "sharpe": [], "calmar": []}
        for record in records:
            summary = (record.get("result") or {}).get("summary") or {}
            for key, skey in (("ann", "annualized_return"), ("ret", "total_return"),
                              ("excess", "excess_return"), ("sharpe", "sharpe"),
                              ("calmar", "calmar")):
                v = summary.get(skey)
                col_best[key].append(float(v) if v is not None and np.isfinite(v) else -np.inf)

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

        tree = self._render_strategy_small_tree(
            frame, 2, 0,
            ["best", "label", "period", "ann", "ret", "excess", "dd", "sharpe",
             "calmar", "turnover", "cost", "fallback"],
            ["", "策略", "区间", "年化", "总收益", "超额", "回撤", "Sharpe",
             "Calmar", "换手", "成本", "数据回退"],
            [34, 230, 190, 76, 76, 76, 76, 70, 70, 76, 76, 86],
            rows,
            xscroll=True,
            max_height=STRATEGY_MEDIUM_TABLE_HEIGHT,
            selectmode="extended",
        )
        self._strategy_compare_tree = tree

        # 高亮最优行
        if tree and len(records) >= 2:
            green_c = get_color(GREEN)
            tree.tag_configure("best_row", foreground=green_c)
            if best_idx is not None:
                try:
                    tree.item(str(best_idx), tags=("best_row",))
                except Exception:
                    pass

        if tree:
            selected_iids = self._initial_strategy_compare_selection(tree, records)
            selected_records = self._strategy_compare_records_by_iids(records, selected_iids)
            self._update_strategy_compare_selection_text(len(selected_records), len(records))
            self._render_comparison_overlay_chart(frame, 1, selected_records)

            # 单击选择控制上方图表; 双击加载结果; 右键显示上下文菜单。
            tree.bind("<<TreeviewSelect>>", lambda e: self._on_strategy_compare_selection_change())
            tree.bind("<Double-1>", lambda e: self._load_comparison_record())
            tree.bind("<Button-2>", lambda e: self._show_comparison_context_menu(e))
            # macOS 右键也可能是 <Button-3> 或 <Control-Button-1>
            tree.bind("<Button-3>", lambda e: self._show_comparison_context_menu(e))
            tree.bind("<Control-Button-1>", lambda e: self._show_comparison_context_menu(e))

    @staticmethod
    def _strategy_compare_record_key(record) -> str:
        snapshot_id = record.get("snapshot_id")
        if snapshot_id:
            return f"snapshot:{snapshot_id}"
        key = record.get("key")
        if key is not None:
            return f"key:{key!r}"
        result = record.get("result") or {}
        return (
            f"result:{result.get('start_date')}:{result.get('end_date')}:"
            f"{(result.get('summary') or {}).get('final_equity')}"
        )

    def _initial_strategy_compare_selection(self, tree, records):
        saved_keys = set(getattr(self, "_strategy_compare_selected_keys", set()) or set())
        selected_iids = [
            str(idx) for idx, record in enumerate(records)
            if self._strategy_compare_record_key(record) in saved_keys
        ]
        if not selected_iids:
            default_count = min(2, len(records))
            selected_iids = [str(idx) for idx in range(len(records) - default_count, len(records))]
        if selected_iids:
            tree.selection_set(selected_iids)
            self._strategy_compare_selected_keys = {
                self._strategy_compare_record_key(records[int(iid)])
                for iid in selected_iids
                if iid.isdigit() and int(iid) < len(records)
            }
        return selected_iids

    @staticmethod
    def _strategy_compare_records_by_iids(records, iids):
        selected = []
        seen = set()
        for iid in iids or []:
            try:
                idx = int(iid)
            except (TypeError, ValueError):
                continue
            if idx in seen or not (0 <= idx < len(records)):
                continue
            seen.add(idx)
            selected.append(records[idx])
        return selected

    def _strategy_compare_selected_iids(self):
        tree = getattr(self, "_strategy_compare_tree", None)
        if tree is None:
            return []
        try:
            return list(tree.selection())
        except Exception:
            return []

    def _update_strategy_compare_selection_text(self, selected_count: int, total_count: int) -> None:
        var = getattr(self, "_strategy_compare_selection_text", None)
        if var is not None:
            try:
                var.set(f"已选择 {selected_count} / {total_count}")
            except Exception:
                pass
        btn = getattr(self, "btn_strategy_compare_delete", None)
        if btn is not None:
            try:
                if selected_count:
                    btn.configure(state="normal", text_color=RED, border_color=RED)
                else:
                    btn.configure(state="disabled", text_color=TEXT_DIM, border_color=BORDER)
            except Exception:
                pass

    def _on_strategy_compare_selection_change(self):
        records = list(getattr(self, "_strategy_compare_results", []) or [])
        iids = self._strategy_compare_selected_iids()
        selected_records = self._strategy_compare_records_by_iids(records, iids)
        self._strategy_compare_selected_keys = {
            self._strategy_compare_record_key(record) for record in selected_records
        }
        self._update_strategy_compare_selection_text(len(selected_records), len(records))
        frame = getattr(self, "strategy_bt_compare_frame", None)
        if frame is not None:
            self._render_comparison_overlay_chart(frame, 1, selected_records)

    def _delete_selected_comparison(self):
        tree = getattr(self, "_strategy_compare_tree", None)
        if tree is None:
            return
        sel = tree.selection()
        if not sel:
            return
        records = list(getattr(self, "_strategy_compare_results", []) or [])
        indices = []
        for iid in sel:
            try:
                idx = int(iid)
            except (ValueError, TypeError):
                continue
            if 0 <= idx < len(records):
                indices.append(idx)
        indices = sorted(set(indices), reverse=True)
        if not indices:
            return
        if not self._confirm_delete_selected_comparison(len(indices)):
            return
        deleted_records = []
        for idx in indices:
            record = records.pop(idx)
            deleted_records.append(record)
            # 同时删除磁盘快照文件和可能指向同一结果的 latest 文件。
            self._delete_strategy_snapshot_for_record(record)
            self._delete_latest_strategy_snapshot_if_matches(record.get("snapshot_id"))
        deleted_keys = {self._strategy_compare_record_key(record) for record in deleted_records}
        self._strategy_compare_selected_keys = (
            set(getattr(self, "_strategy_compare_selected_keys", set()) or set()) - deleted_keys
        )
        self._strategy_compare_results = records
        deleted_current = any(
            self._strategy_record_matches_current_result(record) for record in deleted_records
        )
        if deleted_current or not records:
            self._clear_active_strategy_backtest_result(
                status=(
                    f"已删除当前回测结果 · 删除 {len(deleted_records)} 条对比记录 · "
                    f"剩余 {len(records)} 条"
                ))
        else:
            self._mark_strategy_tabs_dirty("对比")
            self._render_strategy_comparison()
            self.v_st_status.set(f"已删除 {len(deleted_records)} 条 · 剩余 {len(records)} 条对比记录")

    @staticmethod
    def _confirm_delete_selected_comparison(count: int) -> bool:
        return messagebox.askyesno(
            "确认删除",
            f"确定删除选中的 {count} 条策略对比记录吗?\n"
            "此操作会同时删除对应磁盘快照, 无法撤销。",
        )

    def _strategy_record_matches_current_result(self, record) -> bool:
        current = getattr(self, "_last_strategy_bt_result", None)
        if not isinstance(current, dict):
            return False
        current_id = current.get("_snapshot_id")
        record_id = record.get("snapshot_id")
        if current_id and record_id:
            return str(current_id) == str(record_id)
        try:
            return self._strategy_snapshot_dedupe_key({}, current) == self._strategy_snapshot_dedupe_key(
                {}, record.get("result") or {})
        except Exception:
            return (record.get("result") is current)

    def _delete_strategy_snapshot_for_record(self, record) -> None:
        self._delete_strategy_snapshot_path(record.get("snapshot_path"))

    @staticmethod
    def _delete_strategy_snapshot_path(path) -> None:
        if not path:
            return
        from pathlib import Path
        p = Path(path)
        if p.exists():
            try:
                p.unlink()
            except Exception:
                pass

    def _delete_latest_strategy_snapshot_if_matches(self, snapshot_id) -> None:
        if not snapshot_id:
            return
        latest = self._strategy_snapshot_path()
        if not latest.exists():
            return
        try:
            import json as _json
            with open(latest, "r", encoding="utf-8") as f:
                payload = _json.load(f, object_hook=_strategy_snapshot_object_hook)
            result = payload.get("result") or {}
            latest_id = self._strategy_snapshot_dedupe_key(payload, result)
        except Exception:
            return
        if str(latest_id) == str(snapshot_id):
            self._delete_strategy_snapshot_path(latest)

    def _load_comparison_record(self):
        """双击: 把选中的对比记录加载为当前活跃结果."""
        tree = getattr(self, "_strategy_compare_tree", None)
        if tree is None:
            return
        sel = tree.selection()
        if not sel:
            return
        try:
            idx = int(sel[0])
        except (ValueError, IndexError):
            return
        records = list(getattr(self, "_strategy_compare_results", []) or [])
        if 0 <= idx < len(records):
            result = records[idx].get("result")
            if result:
                self._last_strategy_bt_result = result
                self._mark_strategy_tabs_dirty()
                self._update_strategy_result_summary(result)
                label = records[idx].get("label", f"策略 {idx + 1}")
                self.v_st_status.set(f"已加载: {label}")

    def _show_comparison_context_menu(self, event):
        """右键: 显示加载/删除上下文菜单."""
        tree = getattr(self, "_strategy_compare_tree", None)
        if tree is None:
            return
        # 选中点击的行
        row_id = tree.identify_row(event.y)
        if row_id:
            tree.selection_set(row_id)
        else:
            return
        import tkinter as tk
        menu = tk.Menu(tree, tearoff=0)
        menu.add_command(label="加载为当前结果", command=self._load_comparison_record)
        menu.add_separator()
        menu.add_command(label="删除此记录", command=self._delete_selected_comparison)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _render_comparison_overlay_chart(self, frame, grid_row, records):
        """多策略净值叠加折线图."""
        old_fig = getattr(self, "_strategy_bt_compare_fig", None)
        if old_fig is not None:
            old_fig.clf()
            plt.close(old_fig)
            self._strategy_bt_compare_fig = None
        old_frame = getattr(self, "_strategy_compare_chart_frame", None)
        if old_frame is not None:
            try:
                old_frame.destroy()
            except Exception:
                pass

        bg_card_color = get_color(BG_CARD)
        bg_input_color = get_color(BG_INPUT)
        text_dim_color = get_color(TEXT_DIM)
        text_color = get_color(TEXT)
        border_color = get_color(BORDER)
        accent_color = get_color(ACCENT)
        palette = [accent_color, get_color(ORANGE), get_color(GREEN),
                   get_color(RED), "#9b59b6", "#3498db", "#e67e22", "#1abc9c"]

        chart_frame = ctk.CTkFrame(
            frame, fg_color="transparent", height=STRATEGY_SECONDARY_CHART_HEIGHT)
        chart_frame.grid(row=grid_row, column=0, sticky="ew", padx=8, pady=(4, 8))
        chart_frame.grid_columnconfigure(0, weight=1)
        chart_frame.grid_rowconfigure(0, weight=1)
        chart_frame.grid_propagate(False)
        self._strategy_compare_chart_frame = chart_frame

        if not records:
            ctk.CTkLabel(
                chart_frame,
                text="在下方表格选择一条或多条回测记录后显示净值对比",
                text_color=TEXT_DIM,
                font=(FONT_FAMILY, 13),
            ).grid(row=0, column=0, sticky="nsew", padx=12, pady=12)
            return

        fig = Figure(figsize=(10, 2.8), dpi=100, facecolor=bg_card_color)
        ax = fig.add_subplot(111, facecolor=bg_input_color)

        for idx, record in enumerate(records):
            result = record.get("result") or {}
            curve = result.get("equity_curve") or []
            if not curve:
                continue
            dates = [p["date"] for p in curve]
            equity = [float(p["equity"]) for p in curve]
            label = record.get("label") or f"策略 {idx + 1}"
            color = palette[idx % len(palette)]
            lw = 2.2 if idx == len(records) - 1 else 1.4
            ax.plot(dates, equity, color=color, linewidth=lw, label=label[:20],
                    alpha=0.9 if idx == len(records) - 1 else 0.65)

        ax.axhline(1.0, color=border_color, linewidth=0.8, linestyle="--")
        ax.set_ylabel("净值", color=text_dim_color, fontsize=9)
        ax.tick_params(colors=text_dim_color, labelsize=8)
        ax.grid(True, color=border_color, linestyle="--", alpha=0.3)
        for spine in ax.spines.values():
            spine.set_color(border_color)
        leg = ax.legend(loc="best", framealpha=0.9, facecolor=bg_card_color,
                        edgecolor=border_color, fontsize=8, labelcolor=text_color,
                        ncol=min(4, len(records)))
        leg.get_frame().set_linewidth(0.5)
        for lbl in ax.get_xticklabels():
            lbl.set_rotation(15)
            lbl.set_horizontalalignment("right")
        fig.tight_layout()

        canvas = FigureCanvasTkAgg(fig, master=chart_frame)
        canvas.draw()
        canvas.get_tk_widget().grid(row=0, column=0, sticky="nsew")
        self._strategy_bt_compare_fig = fig

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
        yscroll=True,
        max_height=None,
        stretch_weights=None,
        selectmode="browse",
    ):
        _configure_tree_style()
        container = ctk.CTkFrame(parent, fg_color="transparent")
        container.grid(row=row, column=col, columnspan=columnspan,
                       sticky="nsew", padx=8, pady=(0, 8))
        container.grid_columnconfigure(0, weight=1)
        container.grid_rowconfigure(0, weight=1)
        tree_kwargs = {}
        if max_height is not None:
            tree_kwargs["height"] = max_height
        tree = ttk.Treeview(container, columns=columns, show="headings",
                            selectmode=selectmode, **tree_kwargs)
        if yscroll:
            y_scroll = ctk.CTkScrollbar(
                container, orientation="vertical", command=tree.yview,
                width=14, fg_color=BG_INPUT, button_color=ACCENT,
                button_hover_color=TEXT,
            )
        else:
            y_scroll = None
        if xscroll:
            x_scroll = ctk.CTkScrollbar(
                container, orientation="horizontal", command=tree.xview,
                height=12, fg_color=BG_INPUT, button_color=ACCENT,
                button_hover_color=TEXT,
            )
            if y_scroll is not None:
                tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
            else:
                tree.configure(xscrollcommand=x_scroll.set)
        else:
            x_scroll = None
            if y_scroll is not None:
                tree.configure(yscrollcommand=y_scroll.set)
        tree.grid(row=0, column=0, sticky="nsew")
        if y_scroll is not None:
            y_scroll.grid(row=0, column=1, sticky="ns")
        if x_scroll is not None:
            x_scroll.grid(row=1, column=0, sticky="ew")
        _configure_responsive_columns(tree, columns, headers, widths, stretch_weights)
        _attach_column_sort(tree, columns, headers)
        self._style_strategy_tree_rows(tree)
        for idx, vals in enumerate(values):
            tree.insert("", "end", iid=str(idx), values=vals,
                        tags=(self._strategy_tree_row_tag(idx),))
        if not values:
            tree.insert("", "end", values=["—"] + [""] * (len(columns) - 1))
        return tree

    @staticmethod
    def _strategy_tree_row_tag(index: int) -> str:
        return "strategy_even" if index % 2 == 0 else "strategy_odd"

    @staticmethod
    def _style_strategy_tree_rows(tree) -> None:
        tree.tag_configure(
            "strategy_even", background=get_color(BG_CARD), foreground=get_color(TEXT))
        tree.tag_configure(
            "strategy_odd", background=get_color(BG_INPUT), foreground=get_color(TEXT))

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

    @staticmethod
    def _strategy_codes_text(codes):
        codes = [str(code) for code in codes or [] if code]
        return ", ".join(codes) if codes else "—"

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
