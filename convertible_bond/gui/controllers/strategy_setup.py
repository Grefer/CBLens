"""策略回测 — 输入与预检 (模板/代码池/导入/precheck).

从 strategy_backtest.py 按职责拆出; 经 StrategyBacktestMixin 聚合混入
CBPricerApp, 方法间通过 self.* 跨 mixin 调用不受拆分影响。
"""
from __future__ import annotations

from datetime import date
from tkinter import filedialog, messagebox

from ...batch_pricing import batch_view_from_label, batch_view_label, parse_bond_codes
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
from ..theme import E

from .strategy_common import (
    STRATEGY_TEMPLATES,
    WIND_HIGH_FIDELITY_CODE_WARN_LIMIT,
    WIND_HIGH_FIDELITY_PRICING_WARN_LIMIT,
    WIND_HIGH_FIDELITY_REQUEST_MULTIPLIER,
    _STRATEGY_PDE_GRID_M,
    _STRATEGY_PDE_GRID_N,
    _STRATEGY_TEMPLATE_BASE,
    strategy_selection_description,
)


# 预检结果按**文件指纹**缓存: 两份 JSON 加起来 3 万多条, 从头解析一次 0.19s, 而
# `_refresh_strategy_setup_summary` 挂在 `v_st_pool_mode` / `v_st_history_mode` /
# `v_st_codes` 三个 var 的 trace 上 —— 实测在「自选代码」框里敲 10 个字符会触发 9 次,
# 卡 1.34s; 建页时它自己也跑两遍 (一次显式调用 + 一次 trace 回调)。
# 键含 mtime 与大小, 所以同步/回洗改过盘之后下一次读的还是新数, 不会拿旧数糊弄人。
_PRECHECK_CACHE: dict[str, tuple[tuple, dict]] = {}


def _file_stamp(path) -> tuple:
    """(路径, mtime_ns, 大小) —— 文件没动过就没必要重新解析。

    取不到 stat (文件不存在) 也要返回一个**确定**的键: 那一档的预检结果同样固定
    (count=0 / 「未找到」), 缓存住它是对的; 文件后来被建出来时 stamp 自然会变。
    """
    try:
        st = path.stat()
        return (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        return (str(path), None, None)


def _cached_precheck(name: str, path, compute) -> dict:
    """按文件指纹缓存 *compute* 的结果; 返回浅拷贝, 免得调用方改到缓存里那一份。"""
    stamp = _file_stamp(path)
    cached = _PRECHECK_CACHE.get(name)
    if cached is None or cached[0] != stamp:
        cached = (stamp, compute())
        _PRECHECK_CACHE[name] = cached
    return dict(cached[1])


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
            view = getattr(self, "_batch_results_view", None)
            if view is None:
                view_var = getattr(self, "v_batch_view", None)
                view = batch_view_from_label(view_var.get()) if view_var is not None else None
            return codes, f"批量页「{batch_view_label(view or '综合机会')}」"
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
                text = f"{label} · 0只；请到批量页刷新或调整筛选"
            else:
                text = f"{label} · {len(codes)}只 → 每期按下方限制选债{invalid_text}"
            if getattr(self, "_strategy_bt_running", False):
                text = f"下次回测：{text}"
            return text
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
        self._refresh_strategy_precheck_preview()

    def _refresh_strategy_precheck_preview(self, *_):
        """已有的待运行预检随表单更新；不改当前任务的进度分母和启动预检。"""
        var = getattr(self, "v_st_precheck", None)
        if var is None or not var.get().strip() or getattr(self, "_strategy_bt_running", False):
            return
        try:
            var.set(self._format_strategy_precheck(self._strategy_precheck_info()))
        except Exception as exc:
            var.set(f"待运行：{exc}")

    def _on_strategy_batch_scope_changed(self):
        """批量页完成名单更新后通知，避免只监听视图变量而读到上一份名单。"""
        mode = getattr(self, "v_st_pool_mode", None)
        if mode is not None and mode.get() == "当前筛选结果":
            self._refresh_strategy_setup_summary()

    def _open_strategy_batch_scope(self):
        """从代码池来源说明直接打开批量页调整筛选。"""
        label = E("📦 批量")
        self.tab_seg.set(label)
        self._switch_tab(label)

    def _strategy_logic_summary_text(self) -> str:
        """实时展示策略信号、Top N、现金和事件退出口径。"""
        def _get(name, default=""):
            var = getattr(self, name, None)
            try:
                return var.get() if var is not None else default
            except Exception:
                return default

        rank_signal = normalize_pde_rank_signal_label(
            _get("v_st_rank_signal", "估值偏差")
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
        signal_text = self._strategy_rule_description()
        event_exit = bool(_get("v_st_event_exit", False))
        parts = [signal_text, f"Top {n_text} 等权", f"缺口留现金（{yield_text}）"]
        if event_exit:
            parts.append("公告后退出")
        if use_valuation_exposure:
            parts.append("估值缩放")
        return " · ".join(parts)

    def _strategy_rule_description(self) -> str:
        """摘要只读实际输入；编辑到半个数时显示输入状态。"""
        from ...strategy_backtest import PDEStrategyConfig

        fields = {
            "min_relative_cheapness": "v_st_min_relative_cheapness",
            "min_deviation": "v_st_min_deviation",
            "max_deviation": "v_st_max_deviation",
        }
        values = {}
        try:
            for key, name in fields.items():
                var = getattr(self, name, None)
                raw = var.get().strip() if var is not None else None
                values[key] = (getattr(PDEStrategyConfig, key) if raw is None
                               else float(raw) / 100.0 if raw else None)
        except (ValueError, TypeError):
            return "选债参数输入中"
        return strategy_selection_description(values)

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
            "strategy_template": normalize_pde_strategy_template(self.v_st_template.get()),
            "rank_signal_label": normalize_pde_rank_signal_label(
                self.v_st_rank_signal.get()
                if hasattr(self, "v_st_rank_signal") else "估值偏差"
            ),
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
        return _cached_precheck("patches", path, lambda: self._read_patch_precheck(path))

    @staticmethod
    def _read_patch_precheck(path) -> dict:
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
        return _cached_precheck("events", path, lambda: self._read_events_precheck(path))

    @staticmethod
    def _read_events_precheck(path) -> dict:
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
