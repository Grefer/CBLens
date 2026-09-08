"""GUI 错误展示：后台只交付文字，Wind 操作指引与技术诊断分开展示。"""
from __future__ import annotations

from dataclasses import dataclass
from tkinter import TclError, messagebox

import customtkinter as ctk

from ..data_providers.wind_errors import WindConnectionError, WindImportError
from .theme import BG_CARD, BG_INPUT, FONT_FAMILY, FONT_MONO, RED, TEXT, TEXT_DIM


@dataclass(frozen=True)
class ErrorPresentation:
    """可跨线程交付的纯文字快照，不保留异常或 traceback。"""

    summary: str
    message: str
    diagnostic: str = ""
    is_wind: bool = False


def prepare_error(exc: Exception, *, wind_guidance_suffix: str = "") -> ErrorPresentation:
    """在发生异常的线程中提取展示内容，识别被上层包装的 Wind 错误。"""
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (WindImportError, WindConnectionError)):
            details = current.details
            guidance = details.guidance
            if wind_guidance_suffix:
                guidance = f"{guidance}\n\n{wind_guidance_suffix}"
            return ErrorPresentation(
                summary=details.summary,
                message=guidance,
                diagnostic=details.diagnostic,
                is_wind=True,
            )
        current = current.__cause__ or current.__context__
    message = str(exc)
    return ErrorPresentation(summary=message, message=message)


def show_error(parent, title: str, error: ErrorPresentation, status_var=None):
    """在 GUI 线程显示错误；Wind 提示不占用主窗口，普通错误沿用系统弹窗。"""
    if error.is_wind:
        window = _show_wind_error(parent, title, error)
    else:
        messagebox.showerror(title, error.message, parent=parent)
        window = None
    # 系统模态框的事件循环仍会推进进度动画，状态文案要在弹框之后落下。
    if status_var is not None:
        status_var.set(f"❌ {title}: {error.summary}")
    return window


def _show_wind_error(parent, title: str, error: ErrorPresentation):
    """构造带折叠诊断的普通子窗口，不 grab、不进入嵌套事件循环。"""
    window = ctk.CTkToplevel(parent)
    window.title(title)
    window.geometry("640x260")
    window.minsize(640, 260)
    window.transient(parent)
    window.configure(fg_color=BG_CARD)
    window.grid_columnconfigure(0, weight=1)
    window.grid_rowconfigure(2, weight=1)
    window.protocol("WM_DELETE_WINDOW", window.destroy)
    window.bind("<Escape>", lambda _event: window.destroy())

    ctk.CTkLabel(
        window, text=error.summary, font=(FONT_FAMILY, 17, "bold"),
        text_color=RED, wraplength=570, justify="left", anchor="w",
    ).grid(row=0, column=0, sticky="ew", padx=24, pady=(24, 12))
    ctk.CTkLabel(
        window, text=error.message, font=(FONT_FAMILY, 13), text_color=TEXT,
        wraplength=570, justify="left", anchor="w",
    ).grid(row=1, column=0, sticky="ew", padx=24, pady=(0, 14))

    diagnostic = ctk.CTkTextbox(
        window, fg_color=BG_INPUT, text_color=TEXT_DIM,
        font=(FONT_MONO, 12), wrap="word", height=200,
    )
    diagnostic.insert("1.0", error.diagnostic)
    diagnostic.configure(state="disabled")
    diagnostic.grid(row=2, column=0, sticky="nsew", padx=24, pady=(0, 14))
    diagnostic.grid_remove()
    expanded = False

    buttons = ctk.CTkFrame(window, fg_color="transparent")
    buttons.grid(row=3, column=0, sticky="ew", padx=24, pady=(0, 20))

    def toggle_details():
        nonlocal expanded
        expanded = not expanded
        if expanded:
            diagnostic.grid()
            window.geometry("640x500")
        else:
            diagnostic.grid_remove()
            window.geometry("640x260")
        detail_button.configure(text="收起技术详情" if expanded else "查看技术详情")

    def copy_diagnostic():
        content = f"{title}\n{error.summary}\n\n{error.message}\n\n{error.diagnostic}"
        try:
            window.clipboard_clear()
            window.clipboard_append(content)
        except TclError:
            copy_button.configure(text="复制失败，请重试")
        else:
            copy_button.configure(text="已复制诊断")

    detail_button = ctk.CTkButton(buttons, text="查看技术详情", width=120, command=toggle_details)
    detail_button.pack(side="left", padx=(0, 10))
    copy_button = ctk.CTkButton(buttons, text="复制诊断", width=120, command=copy_diagnostic)
    copy_button.pack(side="left", padx=(0, 10))
    configure_wind = getattr(parent, "_open_wind_settings", None)
    if callable(configure_wind):
        ctk.CTkButton(buttons, text="设置接口", width=90, command=configure_wind).pack(side="left")
    ctk.CTkButton(buttons, text="关闭", width=88, command=window.destroy).pack(side="right")
    window.lift()
    return window
