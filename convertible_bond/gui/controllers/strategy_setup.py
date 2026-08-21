"""策略回测 — 输入与预检 (模板/代码池/导入/precheck).

从 strategy_backtest.py 按职责拆出; 经 StrategyBacktestMixin 聚合混入
CBPricerApp, 方法间通过 self.* 跨 mixin 调用不受拆分影响。
"""
from __future__ import annotations

from datetime import date
from tkinter import filedialog, messagebox

from ...batch_pricing import parse_bond_codes
from ...cb_events import CBEventStore, project_events_path
from ...historical_terms import TermsPatchStore, project_terms_patches_path
from ...strategy_backtest import build_rebalance_schedule
from ..constants import (
    BOND_CODE_RE,
    STRATEGY_TEMPLATE_DESCRIPTIONS,
    normalize_pde_rank_signal_label,
    normalize_pde_strategy_template,
    normalize_strategy_history_mode,
)

from .strategy_common import (
    PDE_DOWN_RESET_SOLVE_MULTIPLIER,
    STRATEGY_TEMPLATES,
    WIND_HIGH_FIDELITY_CODE_WARN_LIMIT,
    WIND_HIGH_FIDELITY_PRICING_WARN_LIMIT,
    WIND_HIGH_FIDELITY_REQUEST_MULTIPLIER,
    _STRATEGY_PDE_GRID_M,
    _STRATEGY_PDE_GRID_N,
    _STRATEGY_TEMPLATE_BASE,
)


class StrategySetupMixin:
    """策略回测 — 输入与预检 (模板/代码池/导入/precheck)."""

    # ── 策略回测 (Pro 预览) ───────────────────────────────
    def _apply_strategy_template(self, name):
        """套用模型策略方案；自定义方案仅保留当前参数。"""
        name = normalize_pde_strategy_template(name)
        template_var = getattr(self, "v_st_template", None)
        if template_var is not None and template_var.get() != name:
            self._programmatic_update = True
            try:
                template_var.set(name)
            finally:
                self._programmatic_update = False
        overrides = STRATEGY_TEMPLATES.get(name)
        if overrides is None:
            self.v_st_summary.set("自定义参数 · 保留当前模型、情景与执行口径")
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
        template_desc = STRATEGY_TEMPLATE_DESCRIPTIONS.get(name, "")
        self.v_st_summary.set(f"策略方案「{name}」 · {template_desc}")

    def _describe_strategy_view(self, name):
        """旧预设回调的兼容入口; 新 GUI 不再暴露市场通用视图。"""
        template = normalize_pde_strategy_template(
            self.v_st_template.get() if hasattr(self, "v_st_template") else None
        )
        desc = STRATEGY_TEMPLATE_DESCRIPTIONS.get(template, "")
        self.v_st_summary.set(f"策略方案「{template}」 · {desc}")

    def _strategy_codes_from_pool(self) -> tuple[list[str], str]:
        mode = self.v_st_pool_mode.get() if hasattr(self, "v_st_pool_mode") else "本地全市场"
        if mode == "当前筛选结果":
            rows = list(getattr(self, "_batch_results", []) or [])
            codes = self._dedupe_strategy_codes(row.get("bond_code") for row in rows)
            return codes, "批量页当前筛选结果"
        if mode == "自选代码":
            codes, invalid = self._parse_strategy_manual_codes()
            label = "自选代码池"
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
        logic_var = getattr(self, "v_st_logic_summary", None)
        if logic_var is not None:
            logic_var.set(self._strategy_logic_summary_text())

    def _strategy_logic_summary_text(self) -> str:
        """实时展示策略信号、Top N、现金和事件退出口径。"""
        def _get(name, default=""):
            var = getattr(self, name, None)
            try:
                return var.get() if var is not None else default
            except Exception:
                return default

        rank_signal = normalize_pde_rank_signal_label(
            _get("v_st_rank_signal", "稳健下修优势")
        )
        use_valuation_exposure = "估值" in str(
            _get("v_st_exposure", "恒定满仓")
        )
        try:
            n_text = str(max(1, int(float(_get("v_st_top_n", "10")))))
        except (TypeError, ValueError):
            n_text = "N"
        try:
            yield_text = f"{float(_get('v_st_cash_yield', '0')):g}%/年"
        except (TypeError, ValueError):
            yield_text = "不计息"
        if rank_signal == "估值偏差":
            signal_text = "估值偏差 < 0"
            event_exit = False
        else:
            try:
                min_edge = float(_get("v_st_min_reset_edge", "0"))
                signal_text = f"{rank_signal} ≥ {min_edge:g}元"
            except (TypeError, ValueError):
                signal_text = rank_signal
            event_exit = bool(_get("v_st_event_exit", True))
        parts = [signal_text, f"Top {n_text} 等权", f"缺口留现金（{yield_text}）"]
        if event_exit:
            parts.append("公告后退出")
        if use_valuation_exposure:
            parts.append("估值缩放")
        return " · ".join(parts)

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
        rank_raw = normalize_pde_rank_signal_label(
            self.v_st_rank_signal.get()
            if hasattr(self, "v_st_rank_signal") else "稳健下修优势"
        )
        uses_pde_down_reset_signal = "下修" in str(rank_raw)
        estimated_pde_solves = (
            estimated_pricing * PDE_DOWN_RESET_SOLVE_MULTIPLIER
            if uses_pde_down_reset_signal else estimated_pricing
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
        if uses_pde_down_reset_signal:
            warnings.append(
                "下修优势需逐债反解隐含强度并做参数角点重算, "
                f"本地求解量约为普通回测的{PDE_DOWN_RESET_SOLVE_MULTIPLIER}倍"
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
            "strategy_template": normalize_pde_strategy_template(self.v_st_template.get()),
            "rank_signal_label": rank_raw,
            "code_count": len(codes),
            "period_count": period_count,
            "top_n": top_n,
            "grid_M": _STRATEGY_PDE_GRID_M,
            "grid_N": _STRATEGY_PDE_GRID_N,
            "estimated_pricing": estimated_pricing,
            "estimated_pde_solves": estimated_pde_solves,
            "uses_pde_down_reset_signal": uses_pde_down_reset_signal,
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
        warnings = info.get("warnings") or []
        parts = [
            f"{info['pool_label']} {info['code_count']}只",
            f"{info['period_count']}期",
            f"预计定价 {int(info['estimated_pricing']):,}次",
        ]
        if info.get("uses_pde_down_reset_signal"):
            parts.append(f"模型求解 {int(info['estimated_pde_solves']):,}次")
        if info.get("estimated_wind_requests"):
            parts.append(f"Wind请求 {int(info['estimated_wind_requests']):,}次")
        parts.append(str(info.get("history_mode") or "标准"))
        if warnings:
            suffix = f"（另有{len(warnings) - 1}项）" if len(warnings) > 1 else ""
            parts.append(f"⚠ {warnings[0]}{suffix}")
        else:
            parts.append("口径检查通过")
        return " · ".join(parts)

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
