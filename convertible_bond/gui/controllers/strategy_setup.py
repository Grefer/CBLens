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
    STRATEGY_VIEW_DESCRIPTIONS,
    normalize_strategy_history_mode,
)

from .strategy_common import (
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
        """当前选债逻辑的一句话管线: 选券权重决定 Top N 是否参与、缺口留现金还是满仓摊回.

        随控件值实时刷新, 让筛选链路在配置阶段即可见 (而不是跑完回测才在漏斗里出现);
        口径与模型层三层结构 (选券 → 持仓 → 资金) 一致, 见 strategy_backtest.py docstring。
        """
        def _get(name, default=""):
            var = getattr(self, name, None)
            try:
                return var.get() if var is not None else default
            except Exception:
                return default

        view = _get("v_st_view", "综合机会") or "综合机会"
        weighting = _get("v_st_weighting", "机会分排序")
        if weighting == "等权全池":
            return (f"当前逻辑: 准入筛选 → 「{view}」规则过滤 → 等权持有全部候选"
                    f" (Top N 不参与) → 满仓, 缺口/缺价权重摊回已持仓")
        try:
            n_text = str(max(1, int(float(_get("v_st_top_n", "10")))))
        except (TypeError, ValueError):
            n_text = "N"
        try:
            yield_text = f"{float(_get('v_st_cash_yield', '0')):g}%/年计息"
        except (TypeError, ValueError):
            yield_text = "0 计息"
        return (f"当前逻辑: 准入筛选 → 「{view}」规则过滤 → 按机会分取前 {n_text} 只等权持有"
                f" → 候选不足/缺价留现金 ({yield_text})")

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
