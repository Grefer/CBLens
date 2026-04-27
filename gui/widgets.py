"""
可复用 GUI 组件: 表单行, 卡片, 折叠面板, 悬浮提示.
"""
import numpy as np
import customtkinter as ctk

from gui.theme import (
    BG_CARD, BG_INPUT, BORDER, TEXT, TEXT_DIM, ORANGE,
    FONT_FAMILY, FONT_MONO,
)


def _latest_finite_number(values):
    """返回序列中最后一个可用有限数值，若无则返回 None。"""
    if not values:
        return None
    for v in reversed(values):
        if v is None:
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if np.isfinite(fv):
            return fv
    return None


def _form_row(parent, label_text, var, row, wind=False, extra_widget=None, width=130):
    text_color = ORANGE if wind else TEXT_DIM

    row_frame = ctk.CTkFrame(parent, fg_color="transparent")
    row_frame.grid(row=row, column=0, sticky="ew", padx=16, pady=4)
    row_frame.grid_columnconfigure(1, weight=1)

    dot_text = "● " if wind else "  "
    lbl = ctk.CTkLabel(row_frame, text=f"{dot_text}{label_text}", text_color=text_color, font=(FONT_FAMILY, 13))
    lbl.grid(row=0, column=0, sticky="w")

    ent_container = ctk.CTkFrame(row_frame, fg_color="transparent")
    ent_container.grid(row=0, column=1, sticky="e")

    ent = ctk.CTkEntry(ent_container, textvariable=var, width=width, font=(FONT_MONO, 13),
                       border_width=0, corner_radius=6,
                       fg_color=BG_INPUT, text_color=TEXT, height=28)
    ent.pack(side="left")

    if extra_widget:
        extra_widget(ent_container).pack(side="left", padx=(6, 0))

    return ent


def create_card(parent, title, row, col, icon=""):
    card = ctk.CTkFrame(parent, fg_color=BG_CARD, corner_radius=12)
    card.grid(row=row, column=col, sticky="nsew", padx=6, pady=6)
    card.grid_columnconfigure(0, weight=1)

    header = ctk.CTkFrame(card, fg_color="transparent")
    header.grid(row=0, column=0, sticky="ew", padx=16, pady=(12, 4))

    title_lbl = ctk.CTkLabel(header, text=f"{icon} {title}" if icon else title, font=(FONT_FAMILY, 14, "bold"), text_color=TEXT)
    title_lbl.pack(side="left")

    content = ctk.CTkFrame(card, fg_color="transparent")
    content.grid(row=1, column=0, sticky="nsew", pady=(0, 10))
    content.grid_columnconfigure(0, weight=1)
    return content


class CollapsibleSection(ctk.CTkFrame):
    """可折叠面板: 点击标题行展开/收起内容"""
    def __init__(self, parent, title, expanded=False, **kw):
        kw.setdefault("fg_color", "transparent")
        super().__init__(parent, **kw)
        self.grid_columnconfigure(0, weight=1)
        self._expanded = expanded
        self._title = title
        arrow = "▼" if expanded else "▶"
        self.header_btn = ctk.CTkButton(
            self, text=f"{arrow}  {title}", command=self.toggle,
            anchor="w", fg_color="transparent", hover_color=BG_INPUT,
            text_color=TEXT_DIM, font=(FONT_FAMILY, 13, "bold"), height=28)
        self.header_btn.grid(row=0, column=0, sticky="ew", padx=10)
        self.content = ctk.CTkFrame(self, fg_color="transparent")
        self.content.grid_columnconfigure(0, weight=1)
        if expanded:
            self.content.grid(row=1, column=0, sticky="nsew", pady=(4, 0))

    def toggle(self):
        self._expanded = not self._expanded
        arrow = "▼" if self._expanded else "▶"
        self.header_btn.configure(text=f"{arrow}  {self._title}")
        if self._expanded:
            self.content.grid(row=1, column=0, sticky="nsew", pady=(4, 0))
        else:
            self.content.grid_remove()


class Tooltip:
    """轻量悬浮提示: 鼠标悬停 delay_ms 后弹出, 离开/点击立即收起."""
    def __init__(self, widget, text, delay_ms=450):
        self.widget = widget
        self.text = text
        self.delay = delay_ms
        self._after_id = None
        self._tip = None
        widget.bind("<Enter>", self._on_enter)
        widget.bind("<Leave>", self._on_leave)
        widget.bind("<ButtonPress>", self._on_leave)

    def _on_enter(self, _event=None):
        self._cancel()
        self._after_id = self.widget.after(self.delay, self._show)

    def _on_leave(self, _event=None):
        self._cancel()
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None

    def _cancel(self):
        if self._after_id is not None:
            self.widget.after_cancel(self._after_id)
            self._after_id = None

    def _show(self):
        if self._tip is not None:
            return
        x = self.widget.winfo_rootx() + self.widget.winfo_width() // 2
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        tip = ctk.CTkToplevel(self.widget)
        tip.wm_overrideredirect(True)
        tip.attributes("-topmost", True)
        tip.configure(fg_color=BG_INPUT)
        lbl = ctk.CTkLabel(
            tip, text=self.text, font=(FONT_FAMILY, 11),
            text_color=TEXT, fg_color=BG_INPUT, corner_radius=6,
            padx=10, pady=4)
        lbl.pack()
        tip.update_idletasks()
        # 居中对齐到目标控件下方
        tip.geometry(f"+{x - tip.winfo_width() // 2}+{y}")
        self._tip = tip
