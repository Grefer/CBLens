"""Wind 设置窗口的无头行为：检测/保存边界、线程归属和关闭后的迟到结果。"""
from __future__ import annotations

import json
import queue
import threading
from types import SimpleNamespace

import pytest

from convertible_bond import wind_config
from convertible_bond.gui import wind_settings


class _Widget:
    """所有界面访问必须来自创建线程；销毁后访问立即失败，不创建真实 Tk。"""

    def __init__(self, parent=None, **kwargs):
        self.parent = parent
        self.options = kwargs
        self.children = []
        self.bindings = {}
        self.protocols = {}
        self.callbacks = {}
        self.destroyed = False
        self.content = ""
        self.owner = threading.get_ident()
        self.lifts = 0
        self._callback_sequence = 0
        if isinstance(parent, _Widget):
            parent.children.append(self)

    def _check(self):
        assert threading.get_ident() == self.owner, "worker 不得访问 Tk"
        widget = self
        while isinstance(widget, _Widget):
            assert not widget.destroyed, "销毁后不得访问 Tk"
            widget = widget.parent

    def configure(self, **kwargs):
        self._check()
        self.options.update(kwargs)

    def grid(self, **kwargs): self._check()
    def pack(self, **kwargs): self._check()
    def title(self, text): self._check()
    def geometry(self, value): self._check()
    def minsize(self, width, height): self._check()
    def transient(self, parent): self._check()
    def grid_columnconfigure(self, *args, **kwargs): self._check()
    def grid_rowconfigure(self, *args, **kwargs): self._check()

    def insert(self, index, value):
        self._check()
        self.content += value

    def delete(self, *args):
        self._check()
        self.content = ""

    def protocol(self, name, callback):
        self._check()
        self.protocols[name] = callback

    def bind(self, name, callback, add=None):
        self._check()
        self.bindings.setdefault(name, []).append(callback)

    def lift(self):
        self._check()
        self.lifts += 1

    def after(self, delay, callback):
        self._check()
        self._callback_sequence += 1
        key = f"after-{self._callback_sequence}"
        self.callbacks[key] = callback
        return key

    def after_cancel(self, handle):
        self._check()
        self.callbacks.pop(handle, None)

    def tick(self):
        self._check()
        callbacks, self.callbacks = self.callbacks, {}
        for callback in callbacks.values():
            callback()

    def destroy(self):
        self._check()
        for child in self.children:
            if not child.destroyed:
                child.destroy()
        self.destroyed = True
        for callback in self.bindings.get("<Destroy>", []):
            callback(SimpleNamespace(widget=self))

    def clipboard_clear(self):
        self._check()
        self.clipboard = ""

    def clipboard_append(self, value):
        self._check()
        self.clipboard += value


class _StringVar:
    def __init__(self, master=None, value=""):
        self.master = master
        self.value = value
        self.traces = []

    def get(self):
        self.master._check()
        return self.value

    def set(self, value):
        self.master._check()
        self.value = value
        for callback in self.traces:
            callback("variable", "", "write")

    def trace_add(self, mode, callback):
        self.traces.append(callback)


class _CompletionQueue(queue.Queue):
    def __init__(self):
        super().__init__()
        self.completed = threading.Event()

    def put(self, value):
        super().put(value)
        self.completed.set()


@pytest.fixture
def dialog_ui(monkeypatch, tmp_path):
    for name in ("CTkToplevel", "CTkLabel", "CTkFrame", "CTkEntry", "CTkButton", "CTkTextbox"):
        monkeypatch.setattr(wind_settings.ctk, name, _Widget)
    monkeypatch.setattr(wind_settings.ctk, "StringVar", _StringVar)
    monkeypatch.setattr(wind_settings, "run_wind_probe", lambda *args, **kwargs: pytest.fail("必须显式注入离线检测"))
    module = tmp_path / "WindPy.py"
    module.write_text("raise AssertionError('不得导入接口')\n", encoding="utf-8")
    parent = _Widget()
    dialog = wind_settings.show_wind_settings(parent)
    dialog._results = _CompletionQueue()
    yield SimpleNamespace(parent=parent, dialog=dialog, module=module)
    if not dialog.closed:
        dialog.close()


def _report(path, *, loaded=True, connected=None, ok=True):
    return {
        "path": str(path), "loaded": loaded, "connected": connected, "ok": ok,
        "message": "接口可以加载" if ok else "Wind 终端尚未连接",
        "stage": "load" if connected is None else "connection",
        "diagnostic": "离线替身：接口加载成功" if loaded else "缺少运行库",
    }


def test_loaded_but_disconnected_interface_can_be_saved(dialog_ui, monkeypatch):
    dialog, module = dialog_ui.dialog, dialog_ui.module
    saves = []
    real_save = wind_settings.save_windpy_path

    def record_save(path):
        saves.append(path)
        real_save(path)

    monkeypatch.setattr(wind_settings, "save_windpy_path", record_save)
    dialog._apply_report(_report(module, connected=False, ok=False))
    assert dialog.save_button.options["state"] == "normal"
    assert "接口路径可以保存" in dialog.status_label.options["text"]
    assert "连接状态以测试结果为准" in dialog.status_label.options["text"]
    dialog._save()
    assert saves == [str(module)]
    assert wind_config.load_wind_settings()["windpy_path"] == str(module)
    assert "重启 CBLens" in dialog.status_label.options["text"]
    assert wind_config.get_session_wind_selection() == {"path": "", "source": "auto"}


def test_failed_loading_does_not_enable_or_perform_save(dialog_ui, monkeypatch):
    dialog = dialog_ui.dialog
    saves = []
    monkeypatch.setattr(wind_settings, "save_windpy_path", saves.append)
    dialog._apply_report(_report(dialog_ui.module, loaded=False, ok=False))
    assert dialog.save_button.options["state"] == "disabled"
    assert dialog._validated_path is None
    dialog._save()
    assert not saves


def test_bundled_probe_saves_auto_instead_of_ephemeral_extraction_path(dialog_ui, monkeypatch):
    dialog = dialog_ui.dialog
    wind_config.save_windpy_path(str(dialog_ui.module))
    transient_path = dialog_ui.module.parent / "deleted-onefile-extraction" / "WindPy.py"
    writes = []
    real_save = wind_settings.save_windpy_path

    def record_save(path):
        writes.append(path)
        real_save(path)

    monkeypatch.setattr(wind_settings, "save_windpy_path", record_save)
    report = {**_report(transient_path), "bundled": True, "selection_path": ""}
    dialog._apply_report(report)
    assert dialog.path.get() == ""
    assert dialog.save_button.options["state"] == "normal"
    assert "自动查找" in dialog.status_label.options["text"]
    assert str(transient_path) in dialog.details.content
    dialog._save()
    assert writes == [""]
    assert wind_config.get_wind_selection() == {"path": "", "source": "auto"}


def test_editing_or_browsing_path_invalidates_previous_result(dialog_ui, monkeypatch):
    dialog = dialog_ui.dialog
    saves = []
    monkeypatch.setattr(wind_settings, "save_windpy_path", saves.append)
    dialog._apply_report(_report(dialog_ui.module))
    dialog.path.set(str(dialog_ui.module.parent / "different" / "WindPy.py"))
    assert dialog._validated_path is None
    assert dialog.save_button.options["state"] == "disabled"
    dialog._save()
    assert not saves
    dialog._apply_report(_report(dialog_ui.module))
    selected = str(dialog_ui.module.parent / "selected" / "WindPy.py")
    requests = []

    def choose(**kwargs):
        requests.append(kwargs)
        return selected

    monkeypatch.setattr(wind_settings.filedialog, "askopenfilename", choose)
    dialog._browse()
    assert requests[0]["parent"] is dialog.window
    assert dialog.path.get() == selected
    assert dialog._validated_path is None
    assert "先检测" in dialog.status_label.options["text"]


def test_save_and_reset_preserve_other_settings_and_explain_environment_priority(dialog_ui, monkeypatch):
    dialog, module = dialog_ui.dialog, dialog_ui.module
    target = wind_config.wind_settings_path()
    target.parent.mkdir()
    target.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
    monkeypatch.setenv("WINDPY_PATH", "existing-environment-location")
    writes = []
    real_save = wind_settings.save_windpy_path

    def record_save(path):
        writes.append(path)
        real_save(path)

    monkeypatch.setattr(wind_settings, "save_windpy_path", record_save)
    dialog._apply_report(_report(module))
    dialog._save()
    assert "环境变量 WINDPY_PATH 优先" in dialog.status_label.options["text"]
    dialog._reset()
    assert writes == [str(module), None]
    assert json.loads(target.read_text(encoding="utf-8")) == {"theme": "dark"}
    assert dialog.path.get() == ""
    assert dialog._validated_path is None
    assert dialog.save_button.options["state"] == "disabled"
    assert "环境变量 WINDPY_PATH 优先" in dialog.status_label.options["text"]


def test_save_failure_keeps_validated_path_and_reports_error(dialog_ui, monkeypatch):
    dialog = dialog_ui.dialog
    dialog._apply_report(_report(dialog_ui.module))

    def fail(path):
        raise OSError("设置目录不可写")

    monkeypatch.setattr(wind_settings, "save_windpy_path", fail)
    dialog._save()
    assert dialog.path.get() == str(dialog_ui.module)
    assert dialog.save_button.options["state"] == "normal"
    assert "保存失败：设置目录不可写" == dialog.status_label.options["text"]
    dialog._reset()
    assert dialog.path.get() == str(dialog_ui.module)
    assert "保存失败" in dialog.status_label.options["text"]


@pytest.mark.parametrize("auto, connect", [(False, False), (True, False), (False, True)])
def test_probe_runs_in_worker_and_only_main_thread_poll_updates_gui(dialog_ui, monkeypatch, auto, connect):
    dialog = dialog_ui.dialog
    main_thread = threading.get_ident()
    calls = []
    dialog.path.set(str(dialog_ui.module))

    def probe(path, **kwargs):
        calls.append((path, kwargs, threading.get_ident()))
        return _report(dialog_ui.module)

    monkeypatch.setattr(wind_settings, "run_wind_probe", probe)
    dialog._start_probe(auto=auto, connect=connect)
    assert dialog._results.completed.wait(timeout=3)
    assert len(calls) == 1
    assert calls[0][0] == ("" if auto else str(dialog_ui.module))
    assert calls[0][1] == {"connect": connect, "cancel_event": dialog._cancel}
    assert calls[0][2] != main_thread
    assert dialog.busy
    assert dialog.save_button.options["state"] == "disabled"
    assert "正在" in dialog.status_label.options["text"]
    dialog.window.tick()
    assert not dialog.busy
    assert dialog.save_button.options["state"] == "normal"
    assert "接口可以加载" in dialog.status_label.options["text"]
    assert not dialog.window.callbacks


def test_busy_probe_is_single_flight_and_main_thread_poll_reschedules(dialog_ui, monkeypatch):
    dialog = dialog_ui.dialog
    entered, release = threading.Event(), threading.Event()
    calls, saves = [], []

    def probe(path, **kwargs):
        calls.append(path)
        entered.set()
        assert release.wait(timeout=3)
        return _report(dialog_ui.module)

    monkeypatch.setattr(wind_settings, "run_wind_probe", probe)
    monkeypatch.setattr(wind_settings, "save_windpy_path", saves.append)
    try:
        dialog._start_probe()
        assert entered.wait(timeout=3)
        dialog._start_probe(auto=True)
        dialog._start_probe(connect=True)
        dialog._save()
        dialog._reset()
        dialog.window.tick()  # 未完成，只在主线程续约一次轮询。
        assert len(dialog.window.callbacks) == 1
        assert calls == [""]
        assert not saves
        assert dialog.entry.options["state"] == "disabled"
    finally:
        release.set()
    assert dialog._results.completed.wait(timeout=3)
    dialog.window.tick()
    assert not dialog.busy
    assert dialog.entry.options["state"] == "normal"


def test_worker_exception_becomes_plain_diagnostic(dialog_ui, monkeypatch):
    dialog = dialog_ui.dialog

    def fail(*args, **kwargs):
        raise RuntimeError("模拟探测失败")

    monkeypatch.setattr(wind_settings, "run_wind_probe", fail)
    dialog._start_probe()
    assert dialog._results.completed.wait(timeout=3)
    dialog.window.tick()
    assert not dialog.busy
    assert dialog._validated_path is None
    assert "RuntimeError: 模拟探测失败" in dialog.details.content
    assert dialog.save_button.options["state"] == "disabled"


@pytest.mark.parametrize("destroy_parent", [False, True])
def test_close_or_parent_destroy_cancels_probe_and_ignores_late_result(dialog_ui, monkeypatch, destroy_parent):
    dialog = dialog_ui.dialog
    entered, release = threading.Event(), threading.Event()
    observed_cancel = []

    def probe(path, *, cancel_event, **kwargs):
        observed_cancel.append(cancel_event)
        entered.set()
        assert release.wait(timeout=3)
        return _report(dialog_ui.module)

    monkeypatch.setattr(wind_settings, "run_wind_probe", probe)
    try:
        dialog._start_probe()
        assert entered.wait(timeout=3)
        pending_poll = next(iter(dialog.window.callbacks.values()))
        if destroy_parent:
            dialog_ui.parent.destroy()
        else:
            dialog.close()
            assert not dialog.window.callbacks
        assert dialog.closed
        assert dialog.window.destroyed
        assert observed_cancel[0].is_set()
    finally:
        release.set()
    assert dialog._results.completed.wait(timeout=3)
    pending_poll()  # 已排队回调可能已取出；关闭判断必须先于任何 Tk 访问。
    dialog._apply_report(_report(dialog_ui.module))
    dialog._start_probe()
    dialog.close()
    assert len(observed_cancel) == 1


def test_settings_window_is_singleton_and_can_reopen_after_close(dialog_ui):
    dialog, parent = dialog_ui.dialog, dialog_ui.parent
    assert wind_settings.show_wind_settings(parent) is dialog
    assert dialog.window.lifts == 2
    dialog.close()
    replacement = wind_settings.show_wind_settings(parent)
    assert replacement is not dialog
    assert parent._wind_settings_window is replacement
    assert not replacement.closed
    replacement.close()


def test_copy_diagnostic_and_child_destroy_do_not_close_settings(dialog_ui):
    dialog = dialog_ui.dialog
    dialog._apply_report(_report(dialog_ui.module))
    dialog._copy()
    assert "CBLens Wind 接口检测" in dialog.window.clipboard
    assert "离线替身：接口加载成功" in dialog.window.clipboard
    dialog._on_destroy(SimpleNamespace(widget=dialog.entry))
    assert not dialog.closed
    assert not dialog._cancel.is_set()
