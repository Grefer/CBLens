"""Wind 接口设置窗口：本机路径、独立检测与保存后的重启指引。"""
from __future__ import annotations

import queue
import sys
import threading
from tkinter import TclError, filedialog

import customtkinter as ctk

from ..wind_config import (
    get_session_wind_selection, get_wind_selection, load_wind_settings,
    save_windpy_path, wind_settings_path,
)
from ..wind_probe import run_wind_probe
from .theme import BG_CARD, BG_INPUT, FONT_FAMILY, FONT_MONO, GREEN, RED, TEXT, TEXT_DIM


def _source_text(selection: dict) -> str:
    source = selection.get("source", "auto")
    if source == "auto":
        return "自动查找"
    if source == "settings":
        return "应用设置"
    return f"环境变量 {source}"


def show_wind_settings(parent):
    """复用已打开的设置窗口，不在主线程导入或连接 Wind。"""
    existing = getattr(parent, "_wind_settings_window", None)
    if existing is not None and not existing.closed:
        existing.window.lift()
        return existing
    dialog = _WindSettingsDialog(parent)
    parent._wind_settings_window = dialog
    return dialog


class _WindSettingsDialog:
    """GUI 只通过队列收取检测文字，关闭时取消子进程和界面轮询。"""

    def __init__(self, parent):
        self.closed = False
        self.busy = False
        self._poll_handle = None
        self._cancel = threading.Event()
        self._results: queue.Queue = queue.Queue()
        self._validated_path: str | None = None
        self._diagnostic = ""
        self.window = window = ctk.CTkToplevel(parent)
        window.title("Wind 接口设置")
        window.geometry("740x530")
        window.minsize(740, 530)
        window.transient(parent)
        window.configure(fg_color=BG_CARD)
        window.grid_columnconfigure(0, weight=1)
        window.grid_rowconfigure(5, weight=1)
        window.protocol("WM_DELETE_WINDOW", self.close)
        window.bind("<Escape>", lambda _event: self.close())
        window.bind("<Destroy>", self._on_destroy, add="+")

        ctk.CTkLabel(window, text="Wind 接口设置", font=(FONT_FAMILY, 19, "bold"),
                     text_color=TEXT, anchor="w").grid(
                         row=0, column=0, sticky="ew", padx=24, pady=(20, 8))
        ctk.CTkLabel(
            window, text="使用本机 Wind 终端安装的 Python 接口。检测接口不连接终端；测试连接由你发起。",
            font=(FONT_FAMILY, 13), text_color=TEXT_DIM, wraplength=680,
            justify="left", anchor="w",
        ).grid(row=1, column=0, sticky="ew", padx=24, pady=(0, 12))
        form = ctk.CTkFrame(window, fg_color="transparent")
        form.grid(row=2, column=0, sticky="ew", padx=24)
        form.grid_columnconfigure(0, weight=1)
        self.path = ctk.StringVar(master=window, value="")
        self.source_label = ctk.CTkLabel(form, text="", font=(FONT_FAMILY, 12),
                                        text_color=TEXT_DIM, wraplength=680,
                                        justify="left", anchor="w")
        self.source_label.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        self.entry = ctk.CTkEntry(form, textvariable=self.path, font=(FONT_MONO, 12),
                                 placeholder_text="选择 WindPy.py，或留空使用自动查找")
        self.entry.grid(row=1, column=0, sticky="ew", padx=(0, 10))
        self.browse_button = ctk.CTkButton(form, text="选择文件…", width=100, command=self._browse)
        self.browse_button.grid(row=1, column=1)

        actions = ctk.CTkFrame(window, fg_color="transparent")
        actions.grid(row=3, column=0, sticky="ew", padx=24, pady=14)
        self.auto_button = ctk.CTkButton(actions, text="自动检测", width=110,
                                       command=lambda: self._start_probe(auto=True))
        self.load_button = ctk.CTkButton(actions, text="检测接口", width=110,
                                       command=self._start_probe)
        self.connect_button = ctk.CTkButton(actions, text="测试连接", width=110,
                                          command=lambda: self._start_probe(connect=True))
        for button in (self.auto_button, self.load_button, self.connect_button):
            button.pack(side="left", padx=(0, 10))
        self.status_label = ctk.CTkLabel(window, text="选择接口后先检测，再保存。", wraplength=680,
                                        font=(FONT_FAMILY, 13), text_color=TEXT,
                                        justify="left", anchor="w")
        self.status_label.grid(row=4, column=0, sticky="ew", padx=24, pady=(0, 10))
        self.details = ctk.CTkTextbox(window, font=(FONT_MONO, 12), fg_color=BG_INPUT,
                                      text_color=TEXT_DIM, wrap="word", height=170)
        self.details.grid(row=5, column=0, sticky="nsew", padx=24, pady=(0, 16))
        self.details.configure(state="disabled")
        footer = ctk.CTkFrame(window, fg_color="transparent")
        footer.grid(row=6, column=0, sticky="ew", padx=24, pady=(0, 20))
        self.reset_button = ctk.CTkButton(footer, text="恢复自动", width=100, command=self._reset)
        self.reset_button.pack(side="left", padx=(0, 10))
        self.copy_button = ctk.CTkButton(footer, text="复制诊断", width=100, command=self._copy)
        self.copy_button.pack(side="left")
        ctk.CTkButton(footer, text="关闭", width=80, command=self.close).pack(side="right")
        self.save_button = ctk.CTkButton(footer, text="保存配置", width=100, state="disabled",
                                       command=self._save)
        self.save_button.pack(side="right", padx=(0, 10))
        self.path.trace_add("write", self._path_changed)
        try:
            selection = get_session_wind_selection()
            saved = load_wind_settings().get("windpy_path", "")
            self.path.set(saved or selection["path"])
            active = getattr(sys.modules.get("WindPy"), "__file__", "") or selection["path"]
            self.source_label.configure(text=f"当前会话：{_source_text(selection)}"
                                              + (f" · {active}" if active else ""))
            self._set_details(f"设置文件：{wind_settings_path()}\n"
                              "保存的更改在重启 CBLens 后生效，当前任务继续使用原接口。")
        except (OSError, ValueError) as exc:
            self._status(str(exc), error=True)
            self._set_details(str(exc))
        window.lift()

    def _status(self, text: str, *, error: bool = False, success: bool = False):
        self.status_label.configure(text=text, text_color=RED if error else GREEN if success else TEXT)

    def _set_details(self, text: str):
        self._diagnostic = text
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.insert("1.0", text)
        self.details.configure(state="disabled")

    def _path_changed(self, *_args):
        self._validated_path = None
        self.save_button.configure(state="disabled")

    def _browse(self):
        selected = filedialog.askopenfilename(parent=self.window, title="选择 WindPy.py",
                                              filetypes=[("Wind Python 接口", "WindPy.py"),
                                                         ("Python 文件", "*.py")])
        if selected:
            self.path.set(selected)
            self._status("已选择文件，请先检测接口。")

    def _set_busy(self, busy: bool):
        self.busy = busy
        for widget in (self.entry, self.browse_button, self.auto_button, self.load_button,
                       self.connect_button, self.reset_button):
            widget.configure(state="disabled" if busy else "normal")
        self.save_button.configure(state="disabled" if busy or self._validated_path is None else "normal")

    def _start_probe(self, *, auto: bool = False, connect: bool = False):
        if self.closed or self.busy:
            return
        path = "" if auto else self.path.get().strip()
        self._validated_path = None
        self._set_busy(True)
        self._cancel = threading.Event()
        cancel = self._cancel
        results = self._results
        self._status("正在测试连接…" if connect else "正在检测接口…")

        def worker():
            try:
                report = run_wind_probe(path, connect=connect, cancel_event=cancel)
            except Exception as exc:
                report = {"ok": False, "loaded": False, "message": "检测未完成",
                          "diagnostic": f"{type(exc).__name__}: {exc}"}
            results.put(report)

        threading.Thread(target=worker, daemon=True).start()
        self._poll_handle = self.window.after(100, self._poll)

    def _poll(self):
        self._poll_handle = None
        if self.closed:
            return
        try:
            report = self._results.get_nowait()
        except queue.Empty:
            self._poll_handle = self.window.after(100, self._poll)
            return
        self._apply_report(report)

    def _apply_report(self, report: dict):
        if self.closed:
            return
        if report.get("loaded") and report.get("path"):
            # onefile 子进程退出后 _MEIPASS 就会消失；包内接口只保存“自动”选择。
            self.path.set("" if report.get("bundled") else report["path"])
            self._validated_path = self.path.get()
        self._set_busy(False)
        message = report.get("message", "检测未完成")
        if self._validated_path is not None:
            message += ("\n可保存为自动查找，随应用启动定位包内接口。" if report.get("bundled") else
                        "\n接口路径可以保存；连接状态以测试结果为准。")
        self._status(message, error=not report.get("ok"), success=bool(report.get("ok")))
        self._set_details(f"接口：{report.get('path') or '未找到'}\n"
                          f"阶段：{report.get('stage', 'process')}\n\n"
                          + report.get("diagnostic", ""))

    def _saved_message(self, text: str):
        selection = get_wind_selection()
        if selection["source"] not in {"auto", "settings"}:
            text += f"\n当前环境变量 {selection['source']} 优先，重启后仍会使用它指定的接口。"
        self._status(text, success=True)

    def _save(self):
        if self.busy or self._validated_path is None or self.path.get() != self._validated_path:
            return
        try:
            save_windpy_path(self._validated_path)
            self._saved_message("配置已保存，请重启 CBLens 后使用。")
        except (OSError, ValueError) as exc:
            self._status(f"保存失败：{exc}", error=True)

    def _reset(self):
        if self.busy:
            return
        try:
            save_windpy_path(None)
            self.path.set("")
            self._saved_message("已恢复自动查找，请重启 CBLens 后使用。")
        except (OSError, ValueError) as exc:
            self._status(f"保存失败：{exc}", error=True)

    def _copy(self):
        try:
            self.window.clipboard_clear()
            self.window.clipboard_append("CBLens Wind 接口检测\n" + self._diagnostic)
            self.copy_button.configure(text="已复制")
        except TclError:
            self.copy_button.configure(text="复制失败")

    def _on_destroy(self, event):
        if event.widget is self.window:
            self.closed = True
            self._cancel.set()

    def close(self):
        if self.closed:
            return
        self.closed = True
        self._cancel.set()
        if self._poll_handle is not None:
            try:
                self.window.after_cancel(self._poll_handle)
            except TclError:
                pass
            self._poll_handle = None
        self.window.destroy()
