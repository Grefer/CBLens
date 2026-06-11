"""定价计算 / IV 反解 / 收敛诊断 / 现金流可视化."""
from __future__ import annotations

import threading
from datetime import date, timedelta
from tkinter import messagebox

import customtkinter as ctk
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

from ...pricer import UniversalCBPricer
from ...dateutil import add_years as _add_years
from ...cb_events import is_down_reset_trigger_notice_title
from ...down_reset_overrides import resolve_down_reset, resolve_down_reset_intensity
from ...historical_terms import project_terms
from ...model_defaults import DEFAULT_DOWN_RESET_TRIGGER_RATIO
from ...pricing_api import (
    _accrued_interest,
    _estimate_down_reset_floor,
    _model_signal_status,
    _rating_spread_floor,
    _risk_warnings,
)
from ..constants import P_DOWN_AUTO_SOURCE_LABELS, default_p_down_pct_for_state
from ..theme import (
    ACCENT, BG_APP, BG_CARD, BG_INPUT, BORDER,
    FONT_FAMILY, FONT_MONO,
    GREEN, ORANGE, RED, TEXT, TEXT_DIM,
    get_color,
)


class PricingMixin:
    """⚡ 定价 tab 的业务逻辑."""

    def _p_down_source_is_auto(self) -> bool:
        src = self.v_src_p_down.get().strip()
        return not src or src in P_DOWN_AUTO_SOURCE_LABELS

    def _auto_fill_p_down_from_current_x(self, *, force: bool = False) -> None:
        """按单债状态填入背景下修强度默认值; 手工值不被覆盖。

        p_down 是触发后跟进的年化强度 λ。GUI 默认值只用当前状态分档:
        未触发/保守、已进入触发区、近期触发提示公告。已提议/已通过待生效
        继续由 scheduled reset 节点建模, 不塞进背景强度。
        """
        if not force and not self._p_down_source_is_auto():
            return
        default_pct, source = self._default_p_down_pct_for_current_state()
        value = self._fmt_pct(default_pct)
        if (
            self.v_p_down.get().strip() == value
            and self.v_src_p_down.get().strip() == source
        ):
            return
        self._set_field(
            self.v_p_down,
            value,
            self.v_src_p_down,
            source,
        )

    def _default_p_down_pct_for_current_state(self) -> tuple[float, str]:
        val_date = self._date_var_or_today(self.v_cur_date)
        code, terms, _projection = self._project_terms_for_pricing(val_date)

        resolved_down_reset = None
        if code and terms is not None:
            try:
                resolved_down_reset = resolve_down_reset(
                    code, terms, valuation_date=val_date)
            except Exception:
                resolved_down_reset = None

        has_scheduled_reset = False
        if resolved_down_reset is not None:
            has_scheduled_reset = bool(
                resolved_down_reset.proposal_date
                or resolved_down_reset.approved_effective_date
            )

        block_until = None
        if hasattr(self, "v_dr_block_until"):
            block_until = self._date_text_or_none(self.v_dr_block_until.get())
        if block_until is None and resolved_down_reset is not None:
            block_until = resolved_down_reset.block_until
        in_no_reset_block = block_until is not None and block_until >= val_date

        trigger_pct = self._float_var(self.v_down_reset_trigger_ratio)
        if (
            trigger_pct is None
            and terms is not None
            and getattr(terms, "down_reset_trigger_pct", None) is not None
        ):
            trigger_pct = float(terms.down_reset_trigger_pct)
        if trigger_pct is None:
            trigger_pct = DEFAULT_DOWN_RESET_TRIGGER_RATIO * 100.0

        triggered = None
        s0 = self._float_var(self.v_S0)
        k = self._float_var(self.v_K)
        if s0 is not None and k is not None and trigger_pct is not None:
            triggered = float(s0) < float(k) * float(trigger_pct) / 100.0

        return default_p_down_pct_for_state(
            triggered=triggered,
            has_trigger_notice=self._has_recent_down_reset_trigger_notice(
                code, val_date),
            has_scheduled_reset=has_scheduled_reset,
            in_no_reset_block=in_no_reset_block,
        )

    def _has_recent_down_reset_trigger_notice(
        self,
        code: str,
        val_date: date,
        *,
        lookback_days: int = 90,
    ) -> bool:
        store = getattr(self, "event_store", None)
        if not code or store is None:
            return False
        try:
            events = store.list_events(bond_code=code, through_date=val_date)
        except Exception:
            return False
        start = val_date - timedelta(days=lookback_days)
        notices = [
            e for e in events
            if (
                e.event_date >= start
                and (
                    e.event_type == "down_reset_trigger_notice"
                    or is_down_reset_trigger_notice_title(e.raw_title)
                )
            )
        ]
        if not notices:
            return False
        latest_notice = max(notices, key=lambda e: e.event_date)
        terminal_after_notice = [
            e for e in events
            if (
                e.event_date >= latest_notice.event_date
                and e.event_type in {
                    "down_reset_rejected",
                    "down_reset_proposed",
                    "down_reset_approved",
                }
                and not is_down_reset_trigger_notice_title(e.raw_title)
            )
        ]
        return not terminal_after_notice

    # ── 定价计算 ──────────────────────────────────────────
    def _run_pricing(self):
        code = self._normalize_bond_code(self.v_bond_code.get())
        if code:
            self._maybe_sync_events_background(code)
        self.v_result.set("…")
        self.lbl_result.configure(text_color=TEXT_DIM)
        self.btn_calc.configure(state="disabled")
        self._start_progress("正在计算理论价格")
        threading.Thread(target=self._pricing_worker, daemon=True).start()

    def _pricing_worker(self):
        try:
            params = self._collect_params()
            pricer = UniversalCBPricer(**params["pricer"])
            result = pricer.price(**params["model"], return_greeks=True)
            sigma_used = params["model"]["sigma"]
            self.after(0, lambda: self._show_result(result, pricer, sigma_used, params))
        except Exception as exc:
            err_msg = f"计算失败: {exc}"
            self.after(0, self._on_error, err_msg)
        finally:
            self.after(0, self._stop_progress)
            self.after(0, lambda: self.btn_calc.configure(state="normal"))

    def _collect_params(self):
        def pf(v, label):
            val = v.get().strip()
            try:
                return float(val)
            except ValueError:
                raise ValueError(f"{label} 需要有效数字, 当前值: '{val}'") from None
        def pfo(v, label):
            val = v.get().strip()
            if not val:
                return None
            try:
                return float(val)
            except ValueError:
                raise ValueError(f"{label} 需要有效数字, 当前值: '{val}'") from None
        def pd(v, label):
            val = v.get().strip()
            try:
                return date.fromisoformat(val)
            except ValueError:
                raise ValueError(f"{label} 日期格式应为 YYYY-MM-DD, 当前值: '{val}'") from None

        coupon_str = self.v_coupons.get().strip()
        coupon_rates = tuple(float(x.strip()) / 100.0
                             for x in coupon_str.split(",") if x.strip())

        current_date = pd(self.v_cur_date, "估值日期")
        pricer = dict(
            S0=pf(self.v_S0, "正股价 S"),
            K=pf(self.v_K, "转股价 K"),
            face_value=pf(self.v_face, "面值"),
            redemption_price=pf(self.v_redemp, "到期赎回价"),
            current_date=current_date,
            maturity_date=pd(self.v_mat_date, "到期日期"),
            issue_date=pd(self.v_iss_date, "发行日期"),
            conversion_start_date=pd(self.v_conv_date, "转股起始日"),
            coupon_rates=coupon_rates,
            call_trigger_ratio=pf(self.v_call_ratio, "强赎触发") / 100.0,
            put_trigger_ratio=pf(self.v_put_ratio, "回售触发") / 100.0,
            put_active_years=int(pf(self.v_put_years, "回售生效年数")),
            call_notice_days=int(pf(self.v_call_notice, "强赎宽限天数")),
        )
        down_reset_trigger_pct = pfo(self.v_down_reset_trigger_ratio, "下修触发")
        if down_reset_trigger_pct is not None:
            pricer["down_reset_trigger_ratio"] = down_reset_trigger_pct / 100.0

        code, terms, projection = self._project_terms_for_pricing(current_date)
        if (
            "down_reset_trigger_ratio" not in pricer
            and terms is not None
            and getattr(terms, "down_reset_trigger_pct", None) is not None
        ):
            pricer["down_reset_trigger_ratio"] = float(terms.down_reset_trigger_pct) / 100.0
        pricer.setdefault("down_reset_trigger_ratio", DEFAULT_DOWN_RESET_TRIGGER_RATIO)

        redemption_mode = False
        if terms is not None:
            redemption_date = getattr(terms, "call_redemption_date", None)
            if redemption_date is not None:
                if redemption_date <= current_date:
                    raise ValueError(
                        f"{code or '当前转债'} 已到/已过强赎赎回日 ({redemption_date}), 普通理论价不适用"
                    )
                redemption_mode = True
                pricer["maturity_date"] = redemption_date
                pricer["call_no_redemption_until"] = redemption_date
                if getattr(terms, "call_redemption_price", None) is not None:
                    pricer["redemption_price"] = float(terms.call_redemption_price)
                else:
                    face_value = float(getattr(terms, "face_value", None) or pricer["face_value"])
                    pricer["redemption_price"] = face_value + _accrued_interest(
                        face_value=face_value,
                        coupon_rates=coupon_rates,
                        issue_date=pricer.get("issue_date"),
                        on_date=redemption_date,
                    )
            elif getattr(terms, "call_no_redemption_until", None) is not None:
                no_until = getattr(terms, "call_no_redemption_until")
                if no_until >= current_date:
                    pricer["call_no_redemption_until"] = no_until

            if getattr(terms, "putback_start_date", None) is not None:
                pricer["putback_start_date"] = terms.putback_start_date
            if getattr(terms, "putback_end_date", None) is not None:
                pricer["putback_end_date"] = terms.putback_end_date
            if getattr(terms, "putback_price", None) is not None:
                pricer["putback_price"] = float(terms.putback_price)

        block_until, p_scale = self._resolve_down_reset_for_pricing(current_date)
        if block_until is not None:
            pricer["down_reset_block_until"] = block_until

        down_reset_floor = self._estimate_down_reset_floor_for_gui(terms, current_date)
        if down_reset_floor is not None:
            pricer["down_reset_floor"] = down_reset_floor

        base_p_down = pf(self.v_p_down, "年化下修强度") / 100.0

        resolved_down_reset = None
        if code and terms is not None:
            try:
                resolved_down_reset = resolve_down_reset(code, terms, valuation_date=current_date)
            except Exception:
                resolved_down_reset = None
        down_intensity = resolve_down_reset_intensity(
            base_p_down, resolved_down_reset,
            p_scale_override=p_scale,
            redemption_mode=redemption_mode,
        )
        p_down = down_intensity.effective_p_down
        p_scale = down_intensity.p_scale if down_intensity.p_scale is not None else p_scale
        # 已公告下修: 一次性下修节点传入 pricer (regime ②)
        if (
            down_intensity.scheduled_reset_date is not None
            and down_intensity.scheduled_reset_prob > 0
        ):
            pricer["scheduled_reset_date"] = down_intensity.scheduled_reset_date
            pricer["scheduled_reset_prob"] = down_intensity.scheduled_reset_prob
            if down_intensity.scheduled_reset_target_k is not None:
                pricer["scheduled_reset_target_k"] = down_intensity.scheduled_reset_target_k

        base_spread_input = pf(self.v_spread, "信用利差") / 100.0
        rating_base_spread = _rating_spread_floor(
            getattr(terms, "credit_rating", None) if terms is not None else getattr(self, "_last_credit", None)
        )
        effective_base_spread = base_spread_input
        if rating_base_spread is not None:
            effective_base_spread = max(effective_base_spread, float(rating_base_spread))

        model = dict(
            sigma=pf(self.v_sigma, "波动率 σ") / 100.0,
            r=pf(self.v_r, "无风险利率 r") / 100.0,
            q=pf(self.v_q, "股息率 q") / 100.0,
            base_spread=effective_base_spread,
            p_down=p_down,
            distress_k=pf(self.v_dk, "低股价利差扩张") / 100.0,
            M=int(pf(self.v_M, "空间节点 M")),
            N=int(pf(self.v_N, "时间步数 N")),
        )
        impact = self._make_pricing_impact(
            code=code,
            terms=terms,
            projection=projection,
            val_date=current_date,
            pricer=pricer,
            base_spread=base_spread_input,
            effective_base_spread=effective_base_spread,
            rating_base_spread=rating_base_spread,
            base_p_down=down_intensity.base_p_down,
            effective_p_down=p_down,
            sigma=model["sigma"],
            p_scale=p_scale,
            scheduled_reset_date=down_intensity.scheduled_reset_date,
            scheduled_reset_prob=down_intensity.scheduled_reset_prob,
            scheduled_reset_kind=down_intensity.scheduled_reset_kind,
            scheduled_reset_target_k=down_intensity.scheduled_reset_target_k,
            redemption_mode=redemption_mode,
            down_reset_floor=down_reset_floor,
            resolved_down_reset=resolved_down_reset,
        )
        return {"pricer": pricer, "model": model, "impact": impact}

    def _project_terms_for_pricing(self, val_date: date):
        """取 GUI 当前债在估值日的公告投影视图."""
        code = self._normalize_bond_code(self.v_bond_code.get())
        terms = None
        projection = None
        if code:
            try:
                base_terms = self.terms_cache.get(code)
                if base_terms is not None:
                    projection = project_terms(
                        code,
                        base_terms,
                        val_date,
                        event_store=getattr(self, "event_store", None),
                    )
                    terms = projection.terms
            except Exception:
                terms = getattr(self, "_current_projected_terms", None)
        if terms is None:
            terms = getattr(self, "_current_projected_terms", None)
        return code, terms, projection

    def _estimate_down_reset_floor_for_gui(self, terms, val_date: date) -> float | None:
        """尽量复用当前数据源估算下修底价; 失败则保持原手工链路."""
        stock_code = getattr(terms, "underlying_code", None) if terms is not None else None
        stock_code = stock_code or getattr(self, "_last_stock_code", None)
        if not stock_code or not hasattr(self, "_get_provider"):
            return None
        cache = getattr(self, "_down_reset_floor_cache", None)
        if cache is None:
            cache = {}
            self._down_reset_floor_cache = cache
        key = (stock_code, val_date)
        if key in cache:
            return cache[key]
        try:
            floor = _estimate_down_reset_floor(self._get_provider(), stock_code, val_date)
        except Exception:
            floor = None
        cache[key] = floor
        return floor

    def _make_pricing_impact(
        self, *, code: str, terms, projection, val_date: date,
        pricer: dict, base_spread: float | None, effective_base_spread: float | None,
        rating_base_spread: float | None, base_p_down: float | None,
        effective_p_down: float | None, sigma: float | None, p_scale,
        redemption_mode: bool, down_reset_floor, resolved_down_reset,
        scheduled_reset_date=None, scheduled_reset_prob: float = 0.0,
        scheduled_reset_kind: str | None = None,
        scheduled_reset_target_k: float | None = None,
    ) -> dict:
        risk_warnings = _risk_warnings(terms, val_date) if terms is not None else []
        model_signal_status = (
            _model_signal_status(terms, sigma, risk_warnings)
            if terms is not None
            else "可作为模型信号复核"
        )
        return {
            "bond_code": code,
            "valuation_date": val_date,
            "term_patch_count": len(getattr(projection, "applied_patches", ()) or ()),
            "term_patch_fields": sorted(getattr(projection, "patch_fields", ()) or ()),
            "redemption_mode": redemption_mode,
            "call_redemption_date": getattr(terms, "call_redemption_date", None) if terms is not None else None,
            "last_trading_date": getattr(terms, "last_trading_date", None) if terms is not None else None,
            "call_no_redemption_until": getattr(terms, "call_no_redemption_until", None) if terms is not None else None,
            "redemption_price": pricer.get("redemption_price"),
            "call_redemption_price": getattr(terms, "call_redemption_price", None) if terms is not None else None,
            "maturity_date": pricer.get("maturity_date"),
            "putback_start_date": getattr(terms, "putback_start_date", None) if terms is not None else None,
            "putback_end_date": getattr(terms, "putback_end_date", None) if terms is not None else None,
            "putback_price": getattr(terms, "putback_price", None) if terms is not None else None,
            "conversion_suspension_start_date": (
                getattr(terms, "conversion_suspension_start_date", None) if terms is not None else None
            ),
            "conversion_suspension_end_date": (
                getattr(terms, "conversion_suspension_end_date", None) if terms is not None else None
            ),
            "conversion_suspension_status": (
                getattr(terms, "conversion_suspension_status", None) if terms is not None else None
            ),
            "down_reset_block_until": pricer.get("down_reset_block_until"),
            "down_reset_p_scale": p_scale,
            "down_reset_scheduled_date": scheduled_reset_date,
            "down_reset_scheduled_prob": scheduled_reset_prob,
            "down_reset_scheduled_kind": scheduled_reset_kind,
            "down_reset_scheduled_target_k": scheduled_reset_target_k,
            "down_reset_proposed_date": getattr(resolved_down_reset, "proposal_date", None),
            "down_reset_approved_effective_date": getattr(resolved_down_reset, "approved_effective_date", None),
            "down_reset_announce_date": getattr(resolved_down_reset, "announce_date", None),
            "down_reset_note": getattr(resolved_down_reset, "note", None),
            "down_reset_trigger_ratio": pricer.get("down_reset_trigger_ratio", 1.0),
            "down_reset_trigger_pct": (
                float(pricer.get("down_reset_trigger_ratio", 1.0)) * 100.0
            ),
            "down_reset_floor": down_reset_floor,
            "base_p_down": base_p_down,
            "effective_p_down": effective_p_down,
            "base_spread": base_spread,
            "effective_base_spread": effective_base_spread,
            "rating_base_spread": rating_base_spread,
            "credit_rating": getattr(terms, "credit_rating", None) if terms is not None else getattr(self, "_last_credit", None),
            "credit_rating_outlook": (
                getattr(terms, "credit_rating_outlook", None) if terms is not None else None
            ),
            "credit_watch_status": (
                getattr(terms, "credit_watch_status", None) if terms is not None else None
            ),
            "outstanding_balance": (
                getattr(terms, "outstanding_balance", None) if terms is not None else None
            ),
            "risk_warnings": risk_warnings,
            "model_signal_status": model_signal_status,
        }

    def _preview_pricing_impact(self, *, code: str, terms, projection,
                                val_date: date, p_down) -> dict:
        resolved_down_reset = None
        if code and terms is not None:
            try:
                resolved_down_reset = resolve_down_reset(code, terms, valuation_date=val_date)
            except Exception:
                resolved_down_reset = None

        block_until = self._date_text_or_none(self.v_dr_block_until.get())
        if block_until is None:
            block_until = getattr(resolved_down_reset, "block_until", None)

        scale = getattr(resolved_down_reset, "p_scale", None)

        base_p = (float(p_down) / 100.0) if p_down is not None else None
        redemption_mode = False
        redemption_date = getattr(terms, "call_redemption_date", None) if terms is not None else None
        if redemption_date is not None and redemption_date > val_date:
            redemption_mode = True
        down_intensity = (
            resolve_down_reset_intensity(
                base_p,
                resolved_down_reset,
                p_scale_override=scale,
                redemption_mode=redemption_mode,
            )
            if base_p is not None
            else None
        )
        effective_p = down_intensity.effective_p_down if down_intensity else None
        scale = down_intensity.p_scale if down_intensity else scale

        base_spread = self._float_var(self.v_spread)
        base_spread = base_spread / 100.0 if base_spread is not None else None
        rating_base_spread = _rating_spread_floor(
            getattr(terms, "credit_rating", None) if terms is not None else getattr(self, "_last_credit", None)
        )
        effective_spread = base_spread
        if effective_spread is not None and rating_base_spread is not None:
            effective_spread = max(effective_spread, float(rating_base_spread))

        pricer = {
            "redemption_price": self._float_var(self.v_redemp),
            "maturity_date": redemption_date or self._date_text_or_none(self.v_mat_date.get()),
            "down_reset_block_until": block_until,
            "down_reset_trigger_ratio": DEFAULT_DOWN_RESET_TRIGGER_RATIO,
        }
        if redemption_mode and getattr(terms, "call_redemption_price", None) is not None:
            pricer["redemption_price"] = float(terms.call_redemption_price)
        trigger_pct = self._float_var(self.v_down_reset_trigger_ratio)
        if trigger_pct is not None:
            pricer["down_reset_trigger_ratio"] = trigger_pct / 100.0
        return self._make_pricing_impact(
            code=code,
            terms=terms,
            projection=projection,
            val_date=val_date,
            pricer=pricer,
            base_spread=base_spread,
            effective_base_spread=effective_spread,
            rating_base_spread=rating_base_spread,
            base_p_down=base_p,
            effective_p_down=effective_p,
            sigma=self._float_var(self.v_sigma) / 100.0 if self._float_var(self.v_sigma) is not None else None,
            p_scale=scale,
            scheduled_reset_date=(down_intensity.scheduled_reset_date if down_intensity else None),
            scheduled_reset_prob=(down_intensity.scheduled_reset_prob if down_intensity else 0.0),
            scheduled_reset_kind=(down_intensity.scheduled_reset_kind if down_intensity else None),
            scheduled_reset_target_k=(down_intensity.scheduled_reset_target_k if down_intensity else None),
            redemption_mode=redemption_mode,
            down_reset_floor=None,
            resolved_down_reset=resolved_down_reset,
        )

    def _impact_for_current_view(self, *, code: str, terms, projection,
                                 val_date: date, p_down) -> dict:
        last = getattr(self, "_last_pricing_impact", None)
        if (
            isinstance(last, dict)
            and last.get("bond_code") == code
            and last.get("valuation_date") == val_date
        ):
            return last
        return self._preview_pricing_impact(
            code=code,
            terms=terms,
            projection=projection,
            val_date=val_date,
            p_down=p_down,
        )

    # ── 条款事件状态 ──────────────────────────────────────
    def _refresh_terms_snapshot_card(self):
        """刷新左侧"条款事件"展示.

        这里展示的是当前定价页实际会用到的条款视角: UI 手工输入优先显示,
        状态类字段来自 cb_data + cb_terms_patches + cb_events 投影。
        """
        if not hasattr(self, "v_term_event_call_status"):
            return

        val_date = self._date_var_or_today(self.v_cur_date)
        code, terms, projection = self._project_terms_for_pricing(val_date)
        self._current_projected_terms = terms
        self._current_terms_projection = projection

        s0 = self._float_var(self.v_S0)
        k = self._float_var(self.v_K)
        face = self._float_var(self.v_face)
        redemp = self._float_var(self.v_redemp)
        call_ratio = self._float_var(self.v_call_ratio)
        put_ratio = self._float_var(self.v_put_ratio)
        put_years = self._float_var(self.v_put_years)
        p_down = self._float_var(self.v_p_down)
        impact = self._impact_for_current_view(
            code=code,
            terms=terms,
            projection=projection,
            val_date=val_date,
            p_down=p_down,
        )

        k_src = self.v_src_K.get().strip() or "来源?"
        call_text, call_color = self._term_call_summary(terms, val_date, k, call_ratio, impact=impact)
        down_text, down_color = self._term_down_summary(val_date, impact=impact)
        put_text, put_color = self._term_put_summary(
            val_date, k, put_ratio, put_years, terms=terms, impact=impact)
        risk_text, risk_color = self._term_risk_summary(terms, val_date, impact=impact)

        mat_date = self.v_mat_date.get().strip() or "—"
        conv_date = self.v_conv_date.get().strip() or "—"
        iss_date = self._date_text_or_none(self.v_iss_date.get())
        parity = (s0 / k * 100.0) if s0 and k else None
        call_trigger = (k * call_ratio / 100.0) if k and call_ratio else None
        put_trigger = (k * put_ratio / 100.0) if k and put_ratio else None
        rating = getattr(terms, "credit_rating", None) if terms is not None else None
        balance = getattr(terms, "outstanding_balance", None) if terms is not None else None

        self.v_term_event_alert.set(
            self._terms_snapshot_alert(code, val_date, projection, terms)
        )

        self._update_down_reset_event(
            val_date=val_date, s0=s0, k=k, p_down=p_down,
            summary=down_text, color=down_color, impact=impact)
        self._update_call_event(
            terms=terms, val_date=val_date, s0=s0, k=k, call_ratio=call_ratio,
            call_trigger=call_trigger, summary=call_text, color=call_color,
            impact=impact)
        self._update_put_event(
            val_date=val_date, s0=s0, k=k, put_ratio=put_ratio,
            put_years=put_years, put_trigger=put_trigger, summary=put_text,
            color=put_color, issue_date=iss_date, terms=terms, impact=impact)
        self._update_conversion_event(
            val_date=val_date, k=k, k_src=k_src, conv_date=conv_date,
            projection=projection, parity=parity, terms=terms, impact=impact)
        self._update_risk_event(
            terms=terms, val_date=val_date, rating=rating, balance=balance,
            summary=risk_text, color=risk_color, impact=impact)


    def _term_call_summary(self, terms, val_date: date, k, call_ratio,
                           impact: dict | None = None) -> tuple[str, object]:
        status = str(getattr(terms, "call_status", "") or "")
        no_until = (
            (impact or {}).get("call_no_redemption_until")
            or getattr(terms, "call_no_redemption_until", None)
        )
        redemption_date = (
            (impact or {}).get("call_redemption_date")
            or getattr(terms, "call_redemption_date", None)
        )
        if "不" not in status and ("强赎" in status or "赎回" in status or redemption_date):
            tail = redemption_date.isoformat() if redemption_date else "待日期"
            return f"已公告强赎\n{tail}", RED
        if no_until and no_until >= val_date:
            return f"不强赎至\n{no_until.isoformat()}", GREEN
        trigger = (k * call_ratio / 100.0) if k and call_ratio else None
        return f"触发价\n{self._fmt_num(trigger, '—', '.2f')}", TEXT

    def _term_down_summary(self, val_date: date, impact: dict | None = None) -> tuple[str, object]:
        # 状态优先级: 已公告 > 冻结 > 背景 (新公告覆盖更早的"不修正"承诺)。
        impact = impact or {}
        sched_prob = impact.get("down_reset_scheduled_prob") or 0.0
        sched_kind = impact.get("down_reset_scheduled_kind")
        if sched_prob > 0:
            if sched_kind == "approved":
                return "已通过待生效", ORANGE
            return f"已提议 {float(sched_prob) * 100:.0f}%", ORANGE
        block = impact.get("down_reset_block_until") or self._date_text_or_none(self.v_dr_block_until.get())
        if block and block >= val_date:
            return f"冻结至 {block.isoformat()}", ORANGE
        effective_p = impact.get("effective_p_down")
        if effective_p is not None and effective_p <= 0:
            return "不计下修", TEXT_DIM
        return "可计下修", GREEN

    def _term_put_summary(self, val_date: date, k, put_ratio, put_years,
                          terms=None, impact: dict | None = None) -> tuple[str, object]:
        putback_start = (impact or {}).get("putback_start_date") or getattr(terms, "putback_start_date", None)
        putback_end = (impact or {}).get("putback_end_date") or getattr(terms, "putback_end_date", None)
        if putback_start or putback_end:
            start_text = putback_start.isoformat() if putback_start else "待起始"
            end_text = putback_end.isoformat() if putback_end else "待截止"
            if putback_start and val_date < putback_start:
                return f"回售申报\n{start_text}", TEXT
            if putback_end and val_date > putback_end:
                return f"回售已截止\n{end_text}", TEXT_DIM
            return f"回售申报中\n{end_text}", ORANGE
        maturity = self._date_text_or_none(self.v_mat_date.get())
        trigger = (k * put_ratio / 100.0) if k and put_ratio else None
        if maturity and put_years is not None:
            start = self._add_years_safe(maturity, -int(round(put_years)))
            if val_date >= start:
                return f"回售期内\n触发 {self._fmt_num(trigger, '—', '.2f')}", ORANGE
            return f"未进入\n{start.isoformat()}", TEXT
        return f"触发价\n{self._fmt_num(trigger, '—', '.2f')}", TEXT

    def _term_risk_summary(self, terms, val_date: date, impact: dict | None = None) -> tuple[str, object]:
        if terms is None:
            return "待条款", TEXT_DIM
        if self._conversion_suspension_active(terms, val_date, impact):
            end = (impact or {}).get("conversion_suspension_end_date") or getattr(
                terms, "conversion_suspension_end_date", None)
            tail = f"\n至 {end.isoformat()}" if end else ""
            return f"暂停转股{tail}", ORANGE
        if self._contains_any(getattr(terms, "suspension_status", None), ("停牌", "暂停")):
            return "转债停牌", RED
        if self._contains_any(getattr(terms, "underlying_trade_status", None), ("停牌", "暂停")):
            return "正股停牌", RED
        if self._contains_any(getattr(terms, "underlying_status", None), ("ST", "退市", "风险警示")):
            return "正股风险", RED
        delisting = getattr(terms, "delisting_date", None)
        last_trading = getattr(terms, "last_trading_date", None)
        near_date = last_trading or delisting
        if near_date and near_date <= val_date + timedelta(days=30):
            return f"临近摘牌\n{near_date.isoformat()}", RED
        rating = str(getattr(terms, "credit_rating", None) or "").upper()
        outlook = str((impact or {}).get("credit_rating_outlook") or getattr(
            terms, "credit_rating_outlook", None) or "")
        watch_status = str((impact or {}).get("credit_watch_status") or getattr(
            terms, "credit_watch_status", None) or "")
        if watch_status and not any(word in watch_status for word in ("撤出", "移出", "取消")):
            return "评级观察", ORANGE
        if outlook and outlook not in {"稳定", "正面"}:
            return f"展望 {outlook}", ORANGE
        effective_spread = (impact or {}).get("effective_base_spread")
        base_spread = (impact or {}).get("base_spread")
        if (
            effective_spread is not None and base_spread is not None
            and effective_spread > base_spread + 1e-12
        ):
            return f"定价利差\n{effective_spread * 100:.1f}%", ORANGE
        if rating:
            color = ORANGE if rating.startswith(("A", "BBB", "BB", "B", "C")) and not rating.startswith(("AAA", "AA")) else GREEN
            return f"评级 {rating}", color
        return "无明显风险", GREEN

    def _terms_snapshot_alert(self, code: str, val_date: date, projection, terms) -> str:
        alerts: list[str] = []
        patches = list(getattr(projection, "applied_patches", ()) or ())
        conv_patches = [
            p for p in patches
            if "conversion_price" in p.fields and p.effective_date <= val_date
        ]
        if conv_patches:
            latest = conv_patches[-1]
            old = (latest.before_fields or {}).get("conversion_price")
            new = latest.fields.get("conversion_price")
            if old is not None and new is not None:
                alerts.append(f"公告 K={float(new):.2f} 覆盖条款库 K={float(old):.2f}")
            elif new is not None:
                alerts.append(f"公告 K={float(new):.2f} 已生效")

        if code and hasattr(self, "event_store"):
            try:
                events = self.event_store.list_events(bond_code=code, through_date=val_date)
            except Exception:
                events = []
            patch_dates = {
                p.event_date for p in conv_patches
                if p.event_date is not None
            }
            missing_k = [
                e for e in events
                if e.event_type in {"conversion_price_adjusted", "down_reset_approved"}
                and e.event_date not in patch_dates
            ]
            if missing_k and not conv_patches:
                alerts.append("有转股价调整公告未解析到新 K")
            latest_call = next(
                (e for e in reversed(events) if e.event_type == "call_redemption"),
                None,
            )
            if latest_call and getattr(terms, "call_redemption_date", None) is None:
                alerts.append("强赎公告缺少赎回登记日")

        return "；".join(alerts[:2])

    def _update_down_reset_event(self, *, val_date: date, s0, k, p_down,
                                 summary: str, color,
                                 impact: dict | None = None) -> None:
        impact = impact or {}
        block = impact.get("down_reset_block_until") or self._date_text_or_none(self.v_dr_block_until.get())
        announce = impact.get("down_reset_announce_date") or self._date_text_or_none(self.v_dr_announce_date.get())
        effective_p = impact.get("effective_p_down")
        effective_p_pct = effective_p * 100 if effective_p is not None else None
        eff_txt = self._fmt_num(effective_p_pct, "—", ".1f")
        trigger_ratio = float(impact.get("down_reset_trigger_ratio") or 1.0)

        sched_date = impact.get("down_reset_scheduled_date")
        sched_prob = impact.get("down_reset_scheduled_prob") or 0.0
        sched_kind = impact.get("down_reset_scheduled_kind")
        sched_k = impact.get("down_reset_scheduled_target_k")
        floor = impact.get("down_reset_floor")

        # 新 K 来源: 公告真值优先, 否则估算 (下限) 回落
        if sched_k is not None:
            new_k_txt = f"新K {self._fmt_num(sched_k, '—', '.2f')}(公告)"
        elif floor is not None:
            new_k_txt = f"新K≈{self._fmt_num(floor, '—', '.2f')}(估算)"
        else:
            new_k_txt = "新K 估算"

        # 状态优先级与徽章一致: 已公告 > 冻结 > 背景, 每态只显示与其相关的信息。
        if sched_date is not None and sched_prob > 0:
            days = (sched_date - val_date).days
            progress_text = f"距生效 {days}天" if days > 0 else "已到生效日"
            if sched_kind == "approved":
                detail = f"已通过 → {sched_date.isoformat()} 生效 · {new_k_txt}"
                progress = None
            else:
                detail = (
                    f"已提议 → 预计 {sched_date.isoformat()} 生效 · "
                    f"通过率{float(sched_prob) * 100:.0f}% · {new_k_txt}"
                )
                proposed = impact.get("down_reset_proposed_date")
                progress = self._date_progress(proposed, sched_date, val_date) if proposed else None
        elif block and block >= val_date:
            detail = f"冻结至 {block.isoformat()} · 下修价值=0"
            progress = self._date_progress(announce, block, val_date)
            progress_text = f"冻结 {self._pct_text(progress)}"
        else:
            # 纯触发后: 二元判断——跌破触发线才按跟进概率计入, 之上不计 (与跌幅深浅无关)
            trigger_price = None
            if k is not None:
                try:
                    trigger_price = float(k) * trigger_ratio
                except (TypeError, ValueError):
                    trigger_price = None
            tp_txt = self._fmt_num(trigger_price, "—", ".2f")
            below = (
                trigger_price is not None and s0 is not None
                and float(s0) < trigger_price
            )
            if below:
                detail = f"已跌破触发线 {tp_txt} · 跟进概率 {eff_txt}%/年"
                progress, progress_text = 1.0, "已触发"
            else:
                gap_txt = "—"
                try:
                    if trigger_price and s0 is not None:
                        gap_txt = f"{(float(s0) / trigger_price - 1) * 100:.0f}%"
                except (TypeError, ValueError):
                    pass
                detail = f"未触发 · 触发线 {tp_txt} (S 高 {gap_txt}) · 跌破后概率 {eff_txt}%/年"
                progress, progress_text = 0.0, "距触发线"
        self._set_term_event(
            "down", status=summary.replace("\n", " "), detail=detail,
            progress=progress, progress_text=progress_text, color=color)

    def _update_call_event(self, *, terms, val_date: date, s0, k, call_ratio,
                           call_trigger, summary: str, color,
                           impact: dict | None = None) -> None:
        impact = impact or {}
        status = str(getattr(terms, "call_status", "") or "")
        no_until = impact.get("call_no_redemption_until") or getattr(terms, "call_no_redemption_until", None)
        announce = getattr(terms, "call_announce_date", None)
        redemption_date = impact.get("call_redemption_date") or getattr(terms, "call_redemption_date", None)
        last_trading = impact.get("last_trading_date") or getattr(terms, "last_trading_date", None)
        if "不" not in status and ("强赎" in status or "赎回" in status or redemption_date):
            end = last_trading or redemption_date
            progress = self._date_progress(announce, end, val_date) if end else 1.0
            detail = (
                f"最后交易 {last_trading.isoformat() if last_trading else '—'}  "
                f"赎回登记 {redemption_date.isoformat() if redemption_date else '—'}"
            )
            if impact.get("redemption_mode"):
                detail += (
                    f"  模型T→{self._date_or_dash(impact.get('maturity_date'))}  "
                    f"赎回价 {self._fmt_num(impact.get('redemption_price'), '—', '.2f')}  "
                    "模型p=0"
                )
            self._set_term_event(
                "call", status="已公告强赎", detail=detail,
                progress=progress, progress_text=f"执行 {self._pct_text(progress)}",
                color=RED)
            return
        if no_until and no_until >= val_date:
            progress = self._date_progress(announce, no_until, val_date)
            detail = (
                f"承诺至 {no_until.isoformat()}  "
                f"触发价 {self._fmt_num(call_trigger, '—', '.2f')}"
            )
            detail += "  模型暂停触发式强赎"
            self._set_term_event(
                "call", status="不强赎承诺", detail=detail,
                progress=progress, progress_text=f"承诺 {self._pct_text(progress)}",
                color=GREEN)
            return
        progress = self._safe_ratio(s0, call_trigger)
        detail = (
            f"S {self._fmt_num(s0, '—', '.2f')} / "
            f"触发价 {self._fmt_num(call_trigger, '—', '.2f')}  "
            f"{self._fmt_num(call_ratio, '—', '.0f')}%K"
        )
        event_color = ORANGE if progress is not None and progress >= 0.9 else color
        self._set_term_event(
            "call", status=summary.replace("\n", " "), detail=detail,
            progress=progress, progress_text=f"S/触发 {self._pct_text(progress)}",
            color=event_color)

    def _update_put_event(self, *, val_date: date, s0, k, put_ratio, put_years,
                          put_trigger, summary: str, color, issue_date: date | None,
                          terms=None, impact: dict | None = None) -> None:
        impact = impact or {}
        putback_start = impact.get("putback_start_date") or getattr(terms, "putback_start_date", None)
        putback_end = impact.get("putback_end_date") or getattr(terms, "putback_end_date", None)
        putback_price = impact.get("putback_price") or getattr(terms, "putback_price", None)
        if putback_start or putback_end or putback_price is not None:
            progress = self._date_progress(putback_start, putback_end, val_date) if putback_end else None
            if putback_start and val_date < putback_start:
                progress = self._date_progress(issue_date, putback_start, val_date)
                status = "回售申报待开始"
                progress_text = f"时间 {self._pct_text(progress)}"
                event_color = color
            elif putback_end and val_date > putback_end:
                status = "回售申报已截止"
                progress_text = "已截止"
                event_color = TEXT_DIM
            else:
                status = "回售申报中"
                progress_text = f"窗口 {self._pct_text(progress)}"
                event_color = ORANGE
            detail = (
                f"申报 {self._date_or_dash(putback_start)}~{self._date_or_dash(putback_end)}  "
                f"回售价 {self._fmt_num(putback_price, '—', '.2f')}"
            )
            if putback_price is not None:
                detail += "  模型窗口内底价=回售价"
            self._set_term_event(
                "put", status=status, detail=detail,
                progress=progress, progress_text=progress_text,
                color=event_color)
            return
        maturity = self._date_text_or_none(self.v_mat_date.get())
        start = None
        if maturity and put_years is not None:
            start = self._add_years_safe(maturity, -int(round(put_years)))
        if start and val_date < start:
            progress = self._date_progress(issue_date, start, val_date)
            detail = (
                f"回售期 {start.isoformat()} 起  "
                f"触发价 {self._fmt_num(put_trigger, '—', '.2f')}"
            )
            self._set_term_event(
                "put", status="未进入回售期", detail=detail,
                progress=progress, progress_text=f"时间 {self._pct_text(progress)}",
                color=color)
            return
        progress = self._put_price_progress(s0, k, put_trigger)
        detail = (
            f"S {self._fmt_num(s0, '—', '.2f')} / "
            f"触发价 {self._fmt_num(put_trigger, '—', '.2f')}  "
            f"{self._fmt_num(put_ratio, '—', '.0f')}%K"
        )
        event_color = ORANGE if progress is not None and progress >= 0.8 else color
        self._set_term_event(
            "put", status=summary.replace("\n", " "), detail=detail,
            progress=progress, progress_text=f"触发 {self._pct_text(progress)}",
            color=event_color)

    def _update_conversion_event(self, *, val_date: date, k, k_src: str,
                                 conv_date: str, projection, parity,
                                 terms=None, impact: dict | None = None) -> None:
        impact = impact or {}
        if self._conversion_suspension_active(terms, val_date, impact):
            start = impact.get("conversion_suspension_start_date") or getattr(
                terms, "conversion_suspension_start_date", None)
            end = impact.get("conversion_suspension_end_date") or getattr(
                terms, "conversion_suspension_end_date", None)
            progress = self._date_progress(start, end, val_date) if end else 1.0
            detail = (
                f"K {self._fmt_num(k, '—', '.2f')}({k_src})  "
                f"暂停 {self._date_or_dash(start)}~{self._date_or_dash(end)}  "
                f"转股价值 {self._fmt_num(parity, '—', '.2f')}"
            )
            self._set_term_event(
                "conv", status="暂停转股", detail=detail,
                progress=progress, progress_text=f"窗口 {self._pct_text(progress)}",
                color=ORANGE)
            return
        patches = list(getattr(projection, "applied_patches", ()) or ())
        conv_patches = [
            p for p in patches
            if "conversion_price" in p.fields and p.effective_date <= val_date
        ]
        if conv_patches:
            latest = conv_patches[-1]
            old = (latest.before_fields or {}).get("conversion_price")
            new = latest.fields.get("conversion_price")
            event_key = str(getattr(latest, "source_event_key", "") or "")
            raw_title = str(getattr(latest, "raw_title", "") or "")
            status = (
                "下修已生效"
                if "down_reset_approved" in event_key or "下修" in raw_title
                else "新转股价已生效"
            )
            progress = (
                self._date_progress(latest.event_date, latest.effective_date, val_date)
                if latest.effective_date > val_date else 1.0
            )
            if old is not None and new is not None:
                detail = (
                    f"K {float(old):.2f} -> {float(new):.2f}  "
                    f"生效 {latest.effective_date.isoformat()}"
                )
            else:
                detail = (
                    f"K {self._fmt_num(new, '—', '.2f')}  "
                    f"生效 {latest.effective_date.isoformat()}"
                )
            self._set_term_event(
                "conv", status=status, detail=detail,
                progress=progress, progress_text=f"生效 {self._pct_text(progress)}",
                color=ACCENT)
            return
        conv_start = self._date_text_or_none(conv_date)
        issue_date = self._date_text_or_none(self.v_iss_date.get())
        if conv_start and conv_start > val_date:
            progress = self._date_progress(issue_date, conv_start, val_date)
            detail = (
                f"K {self._fmt_num(k, '—', '.2f')}({k_src})  "
                f"转股起始 {conv_start.isoformat()}"
            )
            self._set_term_event(
                "conv", status="未到转股期", detail=detail,
                progress=progress, progress_text=f"转股期 {self._pct_text(progress)}",
                color=TEXT)
            return
        progress = 1.0 if conv_start else 0.0
        detail = (
            f"K {self._fmt_num(k, '—', '.2f')}({k_src})  "
            f"转股价值 {self._fmt_num(parity, '—', '.2f')}  起始 {conv_date}"
        )
        self._set_term_event(
            "conv", status="可转股" if conv_start else "待转股日",
            detail=detail, progress=progress,
            progress_text="已转股期" if conv_start else "待日期",
            color=ACCENT if k_src == "公告" else TEXT)

    def _update_risk_event(self, *, terms, val_date: date, rating,
                           balance, summary: str, color,
                           impact: dict | None = None) -> None:
        impact = impact or {}
        if terms is None:
            self._set_term_event(
                "risk", status="待条款", detail="加载转债后显示停牌、摘牌、评级事件",
                progress=0.0, progress_text="—", color=TEXT_DIM)
            return
        delisting = getattr(terms, "delisting_date", None)
        last_trading = getattr(terms, "last_trading_date", None)
        near_date = last_trading or delisting
        if near_date:
            progress = self._date_progress(near_date - timedelta(days=30), near_date, val_date)
            days_left = (near_date - val_date).days
            if days_left <= 0:
                progress_text = "已到期"
                status = "已到最后交易/摘牌"
            else:
                progress_text = f"{days_left}天"
                status = summary.replace("\n", " ")
            detail = (
                f"最后交易 {last_trading.isoformat() if last_trading else '—'}  "
                f"摘牌 {delisting.isoformat() if delisting else '—'}"
            )
            extra = self._risk_impact_detail(impact)
            if extra:
                detail += f"  {extra}"
            self._set_term_event(
                "risk", status=status, detail=detail, progress=progress,
                progress_text=progress_text, color=RED if days_left <= 30 else color)
            return
        risky = (
            self._contains_any(getattr(terms, "suspension_status", None), ("停牌", "暂停"))
            or self._contains_any(getattr(terms, "underlying_trade_status", None), ("停牌", "暂停"))
            or self._contains_any(getattr(terms, "underlying_status", None), ("ST", "退市", "风险警示"))
            or self._conversion_suspension_active(terms, val_date, impact)
        )
        outlook = str(impact.get("credit_rating_outlook") or getattr(
            terms, "credit_rating_outlook", None) or "").strip()
        watch_status = str(impact.get("credit_watch_status") or getattr(
            terms, "credit_watch_status", None) or "").strip()
        rating_risk = bool(
            (watch_status and not any(word in watch_status for word in ("撤出", "移出", "取消")))
            or (outlook and outlook not in {"稳定", "正面"})
        )
        risky = risky or rating_risk
        progress = 1.0 if risky else 0.0
        bond_status = str(getattr(terms, "suspension_status", "") or "").strip()
        bond_status_text = f"转债 {bond_status}" if bond_status else "转债状态未披露"
        rating_bits = [f"评级 {rating or '—'}"]
        if outlook:
            rating_bits.append(f"展望 {outlook}")
        if watch_status:
            rating_bits.append(f"观察 {watch_status}")
        detail = (
            f"{' / '.join(rating_bits)}  "
            f"余额 {self._fmt_num(balance, '—', '.2f')}亿  "
            f"{bond_status_text}"
        )
        extra = self._risk_impact_detail(impact)
        if extra:
            detail += f"  {extra}"
        self._set_term_event(
            "risk", status=summary.replace("\n", " "), detail=detail,
            progress=progress, progress_text="风险命中" if risky else "无触发",
            color=color)

    def _set_term_event(self, key: str, *, status: str, detail: str,
                        progress, progress_text: str, color) -> None:
        status_var = getattr(self, f"v_term_event_{key}_status", None)
        detail_var = getattr(self, f"v_term_event_{key}_detail", None)
        progress_var = getattr(self, f"v_term_event_{key}_progress", None)
        if status_var is not None:
            status_var.set(status or "—")
        if detail_var is not None:
            detail_var.set(detail or "—")
        if progress_var is not None:
            progress_var.set(progress_text or "—")
        widgets = getattr(self, "_term_event_widgets", {}).get(key) or {}
        clamped = self._clamp01(progress)
        try:
            widgets.get("bar").set(0.0 if clamped is None else clamped)
        except Exception:
            pass
        for widget_key in ("status", "progress"):
            try:
                widgets.get(widget_key).configure(text_color=color)
            except Exception:
                pass
        try:
            widgets.get("bar").configure(progress_color=color)
        except Exception:
            pass

    @staticmethod
    def _clamp01(value):
        if value is None:
            return None
        try:
            f = float(value)
        except (TypeError, ValueError):
            return None
        if f != f:
            return None
        return max(0.0, min(1.0, f))

    @staticmethod
    def _pct_text(value) -> str:
        f = PricingMixin._clamp01(value)
        if f is None:
            return "—"
        return f"{f * 100:.0f}%"

    @staticmethod
    def _safe_ratio(numerator, denominator):
        try:
            num = float(numerator)
            den = float(denominator)
        except (TypeError, ValueError):
            return None
        if den <= 0:
            return None
        return PricingMixin._clamp01(num / den)

    @staticmethod
    def _ratio_text(numerator, denominator) -> str:
        try:
            num = float(numerator)
            den = float(denominator)
        except (TypeError, ValueError):
            return "—"
        if den <= 0:
            return "—"
        return f"{num / den * 100:.0f}%"

    @staticmethod
    def _date_progress(start: date | None, end: date | None, current: date):
        if end is None:
            return None
        if start is None:
            start = end - timedelta(days=30)
        if current >= end:
            return 1.0
        if current <= start:
            return 0.0
        total = (end - start).days
        if total <= 0:
            return 1.0 if current >= end else 0.0
        return PricingMixin._clamp01((current - start).days / total)

    @staticmethod
    def _down_reset_price_progress(s0, k, trigger_ratio=1.0):
        try:
            s = float(s0)
            conv = float(k)
            trigger = float(trigger_ratio)
        except (TypeError, ValueError):
            return None
        if conv <= 0 or trigger <= 0:
            return None
        # 仅作"距触发线"的可视化 (0=在触发线, →1=正股趋零); 纯触发后模型里
        # 跌破深度不再影响概率, 这里只用于进度条展示。
        return PricingMixin._clamp01(1.0 - s / (conv * trigger))

    @staticmethod
    def _put_price_progress(s0, k, trigger):
        try:
            s = float(s0)
            conv = float(k)
            trig = float(trigger)
        except (TypeError, ValueError):
            return None
        if conv <= trig:
            return None
        if s <= trig:
            return 1.0
        if s >= conv:
            return 0.0
        return PricingMixin._clamp01((conv - s) / (conv - trig))

    @staticmethod
    def _float_var(var):
        try:
            return float(str(var.get()).strip())
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _date_var_or_today(var) -> date:
        parsed = PricingMixin._date_text_or_none(var.get())
        return parsed or date.today()

    @staticmethod
    def _date_text_or_none(text) -> date | None:
        raw = str(text or "").strip()
        if not raw or raw in {"—", "-", "N/A"}:
            return None
        try:
            return date.fromisoformat(raw[:10])
        except ValueError:
            return None

    @staticmethod
    def _date_or_dash(value) -> str:
        return value.isoformat() if isinstance(value, date) else "—"

    @staticmethod
    def _fmt_num(value, fallback="—", fmt=".2f") -> str:
        try:
            f = float(value)
        except (TypeError, ValueError):
            return fallback
        if f != f:
            return fallback
        return format(f, fmt)

    # 共用 convertible_bond.dateutil.add_years (保留方法名供既有调用)
    _add_years_safe = staticmethod(_add_years)

    @staticmethod
    def _contains_any(value, needles: tuple[str, ...]) -> bool:
        text = str(value or "").upper()
        return any(needle.upper() in text for needle in needles)

    @staticmethod
    def _conversion_suspension_active(terms, val_date: date, impact: dict | None = None) -> bool:
        if terms is None:
            return False
        impact = impact or {}
        status = str(
            impact.get("conversion_suspension_status")
            or getattr(terms, "conversion_suspension_status", None)
            or ""
        )
        start = impact.get("conversion_suspension_start_date") or getattr(
            terms, "conversion_suspension_start_date", None)
        end = impact.get("conversion_suspension_end_date") or getattr(
            terms, "conversion_suspension_end_date", None)
        if "恢复" in status:
            return False
        if "暂停" in status:
            if end is None or val_date <= end:
                return start is None or val_date >= start
        return bool(start and start <= val_date and (end is None or val_date <= end))

    @staticmethod
    def _risk_impact_detail(impact: dict | None) -> str:
        if not impact:
            return ""
        parts: list[str] = []
        base_spread = impact.get("base_spread")
        effective_spread = impact.get("effective_base_spread")
        rating_floor = impact.get("rating_base_spread")
        if (
            base_spread is not None and effective_spread is not None
            and effective_spread > base_spread + 1e-12
        ):
            floor_text = f"评级底线 {rating_floor * 100:.1f}%" if rating_floor is not None else "评级底线"
            parts.append(
                f"定价利差 {effective_spread * 100:.1f}% "
                f"(输入 {base_spread * 100:.1f}%, {floor_text})"
            )
        elif effective_spread is not None:
            parts.append(f"定价利差 {effective_spread * 100:.1f}%")
        signal_status = str(impact.get("model_signal_status") or "").strip()
        if signal_status and signal_status != "可作为模型信号复核":
            parts.append(signal_status)
        warnings = [
            str(w) for w in (impact.get("risk_warnings") or [])
            if w and "已公告强赎" not in str(w)
        ]
        if warnings:
            parts.append(warnings[0][:32])
        return "  ".join(parts[:2])

    @staticmethod
    def _fmt_greek(val, fmt):
        if val is None:
            return "—"
        try:
            f = float(val)
        except (TypeError, ValueError):
            return "—"
        if f != f:  # NaN
            return "—"
        return format(f, fmt)

    def _show_result(self, result, pricer, sigma_used, params=None):
        if isinstance(params, dict):
            self._last_pricing_impact = params.get("impact")
        theo = result["price"] if isinstance(result, dict) else result
        self.v_result.set(f"{theo:.3f}")
        self._reset_what_if_labels()
        self._set_what_if_enabled(True)
        info = (
            f"S₀={pricer.S0:.3f}  K={pricer.K:.2f}  "
            f"T={pricer.T:.4f}年  "
            f"σ={sigma_used*100:.1f}%  "
            f"q={float(self.v_q.get() or 0):.2f}%  "
            f"转股比例={pricer.ratio:.4f}"
        )
        self.v_status.set(info)

        if isinstance(result, dict):
            self.v_bond_floor.set(self._fmt_greek(result.get("bond_floor"), ".3f"))
            self.v_parity.set(self._fmt_greek(result.get("parity"), ".3f"))
            self.v_option_prem.set(self._fmt_greek(result.get("option_premium"), ".3f"))
            self.v_delta.set(self._fmt_greek(result.get("delta"), ".4f"))
            self.v_gamma.set(self._fmt_greek(result.get("gamma"), ".6f"))
            self.v_vega.set(self._fmt_greek(result.get("vega"), ".4f"))
            self.v_theta.set(self._fmt_greek(result.get("theta"), ".4f"))

            # 深度实值 + 已过强赎线: 期权价值数学上为 0, 提示是模型预期而非 bug
            opt = result.get("option_premium") or 0.0
            if abs(opt) < 0.01 and pricer.S0 / pricer.K >= pricer.call_trigger_ratio:
                self.v_status.set(info + "  ·  深度实值 + 已过强赎线, 期权价值锁定为 0 (理论锚 = 转股价值)")

        if theo > 100:
            self.lbl_result.configure(text_color=GREEN)
        elif theo < 100:
            self.lbl_result.configure(text_color=RED)
        else:
            self.lbl_result.configure(text_color=TEXT)

        try:
            mkt = float(self.v_market_price.get())
            if mkt > 0:
                dev = (theo - mkt) / theo * 100
                self.v_deviation.set(f"{dev:+.2f}%")
                self.lbl_deviation.configure(text_color=GREEN if dev > 0 else RED)
            else:
                raise ValueError
        except (ValueError, AttributeError):
            self.v_deviation.set("—")
            if hasattr(self, "lbl_deviation"):
                self.lbl_deviation.configure(text_color=TEXT_DIM)
        self._refresh_terms_snapshot_card()

    # ── 隐含波动率反解 ──────────────────────────────────────
    def _solve_iv(self):
        try:
            target = float(self.v_market_price.get().strip())
        except ValueError:
            messagebox.showwarning("提示", "请在「市价 ¥」处填入有效数字 (如 110.5)")
            return
        if target <= 0:
            messagebox.showwarning("提示", "市价必须为正数")
            return
        self.btn_iv.configure(state="disabled")
        self._start_progress(f"反解 IV (target={target:.2f})")
        threading.Thread(target=self._solve_iv_worker, args=(target,), daemon=True).start()

    def _solve_iv_worker(self, target):
        try:
            params = self._collect_params()
            pricer = UniversalCBPricer(**params["pricer"])
            m = params["model"]
            iv = pricer.solve_implied_vol(
                target_price=target, r=m["r"], base_spread=m["base_spread"],
                p_down=m["p_down"], distress_k=m["distress_k"],
                M=max(150, m["M"] // 3), N=max(500, m["N"] // 3),
                q=m["q"],
            )
            if iv != iv:  # NaN
                self.after(0, lambda: self.v_iv.set("—"))
                self.after(0, lambda: self.v_status.set(
                    f"❌ 反解失败: 市价 {target:.2f} 在 σ ∈ [5%, 200%] 区间内无解"))
            else:
                self.after(0, lambda: self.v_iv.set(f"{iv*100:.2f}%"))
                hist = float(self.v_sigma.get())
                gap = iv * 100 - hist
                self.after(0, lambda: self.v_status.set(
                    f"反解 IV = {iv*100:.2f}% (匹配市价 {target:.2f}); "
                    f"历史 σ = {hist:.2f}%, 差 {gap:+.2f}pp"))
        except Exception as exc:
            self.after(0, self._on_error, f"反解 IV 失败: {exc}")
        finally:
            self.after(0, self._stop_progress)
            self.after(0, lambda: self.btn_iv.configure(state="normal"))

    # ── What-if 快算 (σ ±pp / S ±%) ──────────────────────────
    @staticmethod
    def _what_if_base_label(kind: str, delta) -> str:
        return f"{delta:+d}pp" if kind == "sigma" else f"{delta:+d}%"

    def _run_what_if(self, kind: str, delta):
        """微扰 σ 或 S 后重算理论价, 结果回写到对应按钮上.

        kind ∈ {"sigma", "S"}; delta 单位:
            sigma → 百分点 (例如 +2 表示 σ 由 28% 涨到 30%)
            S     → 相对百分比 (例如 -5 表示正股价下跌 5%)

        显示形如 "+5% → 130.40 (+2.1%)", 第二项为相对当前理论价的变化幅度。
        """
        button_map = (getattr(self, "_wf_sigma_buttons", None) if kind == "sigma"
                      else getattr(self, "_wf_s_buttons", None))
        if not button_map or delta not in button_map:
            return
        btn, var = button_map[delta]
        base_label = self._what_if_base_label(kind, delta)
        # 抓主结果作为对照基准 (而非 var 当前值, 避免连续点击累积成 "+5% → 130 → 132")
        try:
            base_price = float(self.v_result.get())
        except (TypeError, ValueError):
            base_price = float("nan")
        var.set(f"{base_label} …")
        btn.configure(state="disabled")

        def worker():
            try:
                params = self._collect_params()
                pricer_kwargs = dict(params["pricer"])
                model_kwargs = dict(params["model"])
                if kind == "sigma":
                    model_kwargs["sigma"] = max(0.001, model_kwargs["sigma"] + delta / 100.0)
                else:  # S
                    pricer_kwargs["S0"] = max(0.001, pricer_kwargs["S0"] * (1 + delta / 100.0))
                # what-if 不需要 greeks, 同时降低网格精度以加速
                model_kwargs["M"] = max(150, model_kwargs["M"] // 2)
                model_kwargs["N"] = max(500, model_kwargs["N"] // 2)
                pricer = UniversalCBPricer(**pricer_kwargs)
                price = float(pricer.price(**model_kwargs))
                if base_price == base_price and base_price > 0:
                    rel_pp = (price - base_price) / base_price * 100.0
                    label = f"{base_label} → {price:.2f} ({rel_pp:+.1f}%)"
                else:
                    label = f"{base_label} → {price:.2f}"
                self.after(0, lambda: var.set(label))
            except Exception as exc:
                self.after(0, lambda: var.set(base_label))
                self.after(0, lambda: self.v_status.set(f"What-if 失败: {exc}"))
            finally:
                self.after(0, lambda: btn.configure(state="normal"))

        threading.Thread(target=worker, daemon=True).start()

    def _reset_what_if_labels(self):
        """主定价完成后清掉 what-if 上一次的结果, 让下一轮微扰从干净状态开始."""
        for kind, button_map_attr in (
            ("sigma", "_wf_sigma_buttons"),
            ("S",     "_wf_s_buttons"),
        ):
            button_map = getattr(self, button_map_attr, None)
            if not button_map:
                continue
            for delta, (_btn, var) in button_map.items():
                var.set(self._what_if_base_label(kind, delta))

    def _set_what_if_enabled(self, enabled: bool) -> None:
        """主定价成功后启用 what-if 按钮; 出错或重置时禁用."""
        target = "normal" if enabled else "disabled"
        for attr in ("_wf_sigma_buttons", "_wf_s_buttons"):
            button_map = getattr(self, attr, None)
            if not button_map:
                continue
            for _delta, (btn, _var) in button_map.items():
                btn.configure(state=target)

    # ── 现金流可视化 ────────────────────────────────────────
    def _show_cashflow(self):
        try:
            params = self._collect_params()
        except Exception as exc:
            messagebox.showerror("参数错误", str(exc))
            return
        try:
            pricer = UniversalCBPricer(**params["pricer"])
        except Exception as exc:
            messagebox.showerror("构造失败", str(exc))
            return

        # 现金流序列: 非末期 → 每期票息; 末期 → redemption_price (含末期利息+面值+赎回溢价)
        labels, amounts, kinds = [], [], []
        for p in pricer.coupon_periods:
            if p["is_final"]:
                labels.append(p["end"].isoformat())
                amounts.append(float(pricer.redemption_price))
                kinds.append("到期兑付")
            else:
                labels.append(p["end"].isoformat())
                amounts.append(float(p["coupon_amount"]))
                kinds.append(f"票息 {p['rate']*100:.2f}%")

        if not labels:
            messagebox.showinfo("提示", "没有可显示的现金流")
            return

        win = ctk.CTkToplevel(self)
        win.title(f"现金流: {self.v_bond_code.get() or '未命名'}")
        win.geometry("900x500")
        win.configure(fg_color=BG_APP)
        win.transient(self)

        bg = get_color(BG_CARD)
        bg_in = get_color(BG_INPUT)
        txt = get_color(TEXT)
        txt_dim = get_color(TEXT_DIM)
        brd = get_color(BORDER)
        accent = get_color(ACCENT)
        orange = get_color(ORANGE)

        fig = Figure(figsize=(9, 4.5), dpi=100, facecolor=bg)
        ax = fig.add_subplot(111, facecolor=bg_in)

        x_pos = np.arange(len(labels))
        colors = [orange if k == "到期兑付" else accent for k in kinds]
        bars = ax.bar(x_pos, amounts, color=colors, edgecolor=brd, linewidth=0.5)

        for bar, amt in zip(bars, amounts):
            ax.annotate(f"{amt:.2f}",
                        xy=(bar.get_x() + bar.get_width() / 2, bar.get_height()),
                        xytext=(0, 3), textcoords="offset points",
                        ha="center", fontsize=9, color=txt)

        ax.set_xticks(x_pos)
        ax.set_xticklabels(labels, rotation=30, ha="right", fontsize=9)
        ax.set_ylabel("现金流 (¥/百元面值)", color=txt_dim, fontsize=10)
        ax.set_title(f"{self.v_bond_code.get() or '可转债'} 现金流计划",
                     color=txt, fontsize=13, fontweight="bold")
        ax.tick_params(colors=txt_dim, labelsize=9)
        for spine in ax.spines.values():
            spine.set_color(brd)
        ax.grid(True, axis="y", color=brd, linestyle="--", alpha=0.4)

        from matplotlib.patches import Patch
        legend = ax.legend(handles=[
            Patch(facecolor=accent, label="期间票息"),
            Patch(facecolor=orange, label="到期兑付 (面值+末期利息+溢价)"),
        ], loc="best", framealpha=0.9, facecolor=bg, edgecolor=brd,
            fontsize=9, labelcolor=txt)
        legend.get_frame().set_linewidth(0.5)

        fig.tight_layout()
        canvas = FigureCanvasTkAgg(fig, master=win)
        canvas.draw()
        canvas.get_tk_widget().pack(fill="both", expand=True, padx=12, pady=12)

        total = sum(amounts)
        ctk.CTkLabel(
            win, text=f"现金流合计 (未折现) = {total:.2f}  ·  共 {len(labels)} 笔",
            text_color=TEXT_DIM, font=(FONT_FAMILY, 11)).pack(pady=(0, 10))

    # ── 收敛诊断 (开发者工具, 不绑定到 UI; 可通过 Ctrl+D 快捷键触发) ─────
    def _convergence_check(self):
        btn = getattr(self, "btn_conv", None)
        if btn is not None:
            btn.configure(state="disabled")
        self._start_progress("收敛诊断 (M, N → 2M, 2N)")
        threading.Thread(target=self._convergence_worker, daemon=True).start()

    def _convergence_worker(self):
        try:
            params = self._collect_params()
            pricer = UniversalCBPricer(**params["pricer"])
            m = params["model"]
            theo_a = float(pricer.price(**m))
            m2 = dict(m)
            m2["M"] = m["M"] * 2
            m2["N"] = m["N"] * 2
            theo_b = float(pricer.price(**m2))
            diff = theo_b - theo_a
            rel = abs(diff) / max(abs(theo_b), 1e-9)
            verdict = "已收敛" if rel < 1e-3 else ("基本收敛" if rel < 5e-3 else "未收敛, 建议加密")
            self.after(0, lambda: self.v_status.set(
                f"收敛诊断: M={m['M']},N={m['N']} → {theo_a:.4f}; 翻倍 → {theo_b:.4f}; "
                f"Δ={diff:+.4f} ({rel*100:.3f}%)  [{verdict}]"))
        except Exception as exc:
            self.after(0, self._on_error, f"收敛诊断失败: {exc}")
        finally:
            self.after(0, self._stop_progress)
            btn = getattr(self, "btn_conv", None)
            if btn is not None:
                self.after(0, lambda b=btn: b.configure(state="normal"))


# 让模块级 import 仍能拿到 FONT_MONO/FONT_FAMILY 等 (legacy: 部分回调期望 module 上有)
_ = (FONT_FAMILY, FONT_MONO, BORDER, TEXT, TEXT_DIM, ACCENT, ORANGE, GREEN, RED,
     BG_APP, BG_CARD, BG_INPUT, plt)
