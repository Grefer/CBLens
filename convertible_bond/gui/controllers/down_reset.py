"""下修事件覆盖面板与解析."""
from __future__ import annotations

from datetime import date
from tkinter import messagebox

import customtkinter as ctk

from ...data_providers import BondTerms, _add_months
from ...down_reset_overrides import (
    DEFAULT_COOLDOWN_MONTHS,
    default_overrides,
    reload_default_overrides,
    resolve_down_reset,
)
from ..theme import (
    BTN_CTRL, BTN_HOVER,
    FONT_FAMILY,
    ORANGE, TEXT, TEXT_DIM,
)
from ..widgets import _form_row, create_card


class DownResetMixin:
    """下修事件覆盖面板的 UI 构建 + 事件解析."""

    def _build_down_reset_panel(self, parent, *, embedded: bool = False, start_row: int = 0):
        """下修事件覆盖面板.

        条款字段 (cooldown_months) 写回 cb_data.json;
        事件字段 (announce_date / note) 写到 down_reset_overrides.json.
        触发 announce_date + cooldown → block_until 自动推算并显示.
        """
        card = parent if embedded else create_card(parent, "下修事件", 0, 0, icon="🛡")
        _form_row(card, "不下修公告日", self.v_dr_announce_date, start_row, width=130,
                  tooltip="公告明确近期不下修时填写。日常定价优先读取本地公告事件表, 通常无需手工填。")
        _form_row(card, "承诺月数", self.v_dr_cooldown, start_row + 1, width=130,
                  tooltip="单位: 月。没有公告正文承诺截止日时, 用公告日 + 承诺月数推算冻结至。")
        # 备注是长文本输入, compact 跳过右侧两槽以容纳更宽的输入框
        _form_row(card, "依据/备注", self.v_dr_note, start_row + 2, width=260, compact=True,
                  tooltip="公告标题或人工记录依据。")
        _form_row(card, "冻结至", self.v_dr_block_until, start_row + 3, width=130,
                  tooltip="下修价值在该日期前被屏蔽。事件表有公告约定截止日时会自动填入。")

        status_row = ctk.CTkFrame(card, fg_color="transparent")
        status_row.grid(row=start_row + 4, column=0, sticky="ew", padx=16, pady=(2, 4))
        ctk.CTkLabel(status_row, textvariable=self.v_dr_status,
                     text_color=TEXT_DIM, font=(FONT_FAMILY, 11)).pack(side="left")

        btns = ctk.CTkFrame(card, fg_color="transparent")
        btns.grid(row=start_row + 5, column=0, sticky="ew", padx=16, pady=(2, 8))
        ctk.CTkButton(btns, text="保存事件", command=self._save_down_reset_override,
                      fg_color=BTN_CTRL, hover_color=BTN_HOVER, text_color=ORANGE,
                      font=(FONT_FAMILY, 12, "bold"), width=85, height=28,
                      corner_radius=6).pack(side="left", padx=(0, 6))
        ctk.CTkButton(btns, text="清除事件", command=self._clear_down_reset_override,
                      fg_color=BTN_CTRL, hover_color=BTN_HOVER, text_color=TEXT_DIM,
                      font=(FONT_FAMILY, 12), width=85, height=28,
                      corner_radius=6).pack(side="left", padx=(0, 6))
        # "cooldown → 本地条款库" 按钮已下线 — 这是写文件的维护动作, 平日不需要;
        # 仍保留 ``_save_down_reset_cooldown_to_cb_data`` 方法供脚本/CLI 调用。

    # ── 下修事件覆盖 ───────────────────────────────────────
    def _resolve_down_reset_for_pricing(self, valuation_date: date):
        """定价前直接从事件表/覆盖层解析下修冻结, UI 字段只作兜底维护入口."""
        code = self._normalize_bond_code(self.v_bond_code.get())
        terms = self.terms_cache.get(code) if code else None
        ui_block = self._compute_down_reset_from_ui(update_display=False)
        if terms is None:
            return ui_block, None

        resolved = resolve_down_reset(code, terms, valuation_date=valuation_date)
        block_until = resolved.block_until or ui_block
        return block_until, resolved.p_scale

    def _compute_down_reset_from_ui(self, *, update_display: bool = True):
        """读取下修事件 GUI 字段 → block_until.

        仅作为手工维护兜底. 常规定价优先走 cb_events / overrides 解析.
        有公告日时用 announce_date + cooldown 推算 block_until; 没有公告日时,
        允许直接使用 "推算屏蔽至" 中的硬 override 日期.
        """
        ann_str = self.v_dr_announce_date.get().strip()
        cd_str = self.v_dr_cooldown.get().strip()
        block_str = self.v_dr_block_until.get().strip()

        block_until = None
        if ann_str:
            try:
                ann = date.fromisoformat(ann_str)
            except ValueError:
                raise ValueError(f"公告不修正日期格式应为 YYYY-MM-DD: '{ann_str}'")
            try:
                cd = float(cd_str) if cd_str else float(DEFAULT_COOLDOWN_MONTHS)
            except ValueError:
                raise ValueError(f"再观察期(月)应为数字或留空: '{cd_str}'")
            block_until = _add_months(ann, int(round(cd)))
        elif block_str and block_str not in {"—", "-", "N/A"}:
            try:
                block_until = date.fromisoformat(block_str)
            except ValueError:
                raise ValueError(f"推算屏蔽至日期格式应为 YYYY-MM-DD: '{block_str}'")

        if update_display:
            self.v_dr_block_until.set(block_until.isoformat() if block_until else "—")
        return block_until

    def _populate_down_reset_from_resolver(self, code: str, terms: BondTerms) -> None:
        """根据 cb_events + cb_data.cooldown + overrides.json 填充 GUI 字段."""
        ov = default_overrides().get(code) or {}
        ann = ov.get("announce_date") or ""
        resolved = resolve_down_reset(code, terms, valuation_date=date.today())
        note_parts = []
        if ov.get("note"):
            note_parts.append(str(ov["note"]))
        if terms.down_reset_note:
            note_parts.append(terms.down_reset_note)
        note_text = " | ".join(note_parts) if note_parts else (resolved.note or "")

        cooldown = terms.down_reset_cooldown_months
        if cooldown is None:
            cooldown = resolved.cooldown_months
        cd_str = "" if cooldown is None else f"{float(cooldown):g}"

        self.v_dr_announce_date.set(str(ann or resolved.announce_date or ""))
        self.v_dr_cooldown.set(cd_str)
        self.v_dr_note.set(note_text)

        # 同步 block_until 显示
        self.v_dr_block_until.set(
            resolved.block_until.isoformat() if resolved.block_until else "—"
        )

        if ann:
            tag = f"事件: {ann}"
            if cooldown is None:
                tag += " (cooldown 用默认值)"
            self.v_dr_status.set(tag)
        elif resolved.announce_date is not None:
            self.v_dr_status.set(f"本地事件表: {resolved.announce_date}")
        elif terms.down_reset_block_until is not None:
            self.v_dr_status.set(f"人工屏蔽至: {terms.down_reset_block_until}")
        else:
            self.v_dr_status.set("无事件")
        if hasattr(self, "_auto_fill_p_down_from_current_x"):
            self.after_idle(lambda: self._auto_fill_p_down_from_current_x())

    def _save_down_reset_override(self):
        code = self._normalize_bond_code(self.v_bond_code.get())
        if not code:
            messagebox.showwarning("提示", "请先输入转债代码")
            return
        ann_str = self.v_dr_announce_date.get().strip()
        ann = None
        if ann_str:
            try:
                ann = date.fromisoformat(ann_str)
            except ValueError:
                messagebox.showwarning("提示", f"公告日格式应为 YYYY-MM-DD: {ann_str}")
                return
        try:
            default_overrides().set(
                code, announce_date=ann, p_scale_after_cooldown=None,
                note=self.v_dr_note.get().strip() or None,
            )
            reload_default_overrides()
            self.v_dr_status.set(f"已保存到下修人工覆盖记录 ({code})")
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc))

    def _clear_down_reset_override(self):
        code = self._normalize_bond_code(self.v_bond_code.get())
        if not code:
            return
        if default_overrides().delete(code):
            reload_default_overrides()
        self.v_dr_announce_date.set("")
        self.v_dr_note.set("")
        self.v_dr_block_until.set("—")
        self.v_dr_status.set("已清除")

    def _save_down_reset_cooldown_to_cb_data(self):
        """把 cooldown 写回 cb_data.json 的 down_reset_cooldown_months 字段."""
        code = self._normalize_bond_code(self.v_bond_code.get())
        if not code:
            messagebox.showwarning("提示", "请先输入转债代码")
            return
        terms = self.terms_cache.get(code)
        if terms is None:
            messagebox.showwarning("提示", f"{code} 不在条款库中, 先 '同步' 拉取")
            return
        cd_str = self.v_dr_cooldown.get().strip()
        try:
            cd_val = float(cd_str) if cd_str else None
        except ValueError:
            messagebox.showwarning("提示", f"再观察期应为数字或留空: {cd_str}")
            return
        terms.down_reset_cooldown_months = cd_val
        self.terms_cache.set(code, terms, source="manual_gui")
        self.v_dr_status.set(f"已写回条款库 (再观察期={cd_val})")
