"""错误窗口的无头行为验证：首屏可操作、诊断可复制、异常在 worker 释放。"""
from dataclasses import FrozenInstanceError
from datetime import date
from types import SimpleNamespace
import threading

import pytest

from convertible_bond.data_providers.wind_errors import (
    WindConnectionError, WindErrorDetails, WindImportError,
)
from convertible_bond.gui import error_dialogs
from convertible_bond.gui.controllers import backtest, strategy_run


DETAILS = WindErrorDetails(
    "未找到 Wind Python 接口（WindPy）",
    "请在本机 Wind 终端中安装或修复 Python 接口，然后重启 CBLens。",
    "原始错误：ModuleNotFoundError: No module named 'WindPy'\n运行环境：win32 / 64 位 / 桌面包",
)


@pytest.mark.parametrize("error_type", [WindImportError, WindConnectionError])
def test_prepare_error_extracts_wrapped_wind_text_and_freezes_it(error_type):
    try:
        try:
            raise error_type(DETAILS)
        except error_type as exc:
            raise RuntimeError("数据源构造失败") from exc
    except RuntimeError as exc:
        error = error_dialogs.prepare_error(exc)

    assert error.is_wind
    assert error.summary == DETAILS.summary
    assert error.message == DETAILS.guidance
    assert error.diagnostic == DETAILS.diagnostic
    assert all(isinstance(value, (str, bool)) for value in vars(error).values())
    with pytest.raises(FrozenInstanceError):
        error.summary = "changed"


def test_prepare_error_preserves_unknown_errors_and_handles_cause_cycles():
    first = RuntimeError("原始数据错误\n请检查日期")
    second = ValueError("日期错误")
    first.__cause__ = second
    second.__cause__ = first
    error = error_dialogs.prepare_error(first, wind_guidance_suffix="不可混入普通错误")
    assert not error.is_wind
    assert error.message == str(first)


class _Var:
    def __init__(self):
        self.value = "正在回测"

    def set(self, value):
        self.value = value


def test_unknown_errors_keep_native_dialog_and_write_status_after_modal(monkeypatch):
    parent = object()
    status = _Var()
    shown = []
    monkeypatch.setattr(
        error_dialogs.messagebox, "showerror",
        lambda *args, **kwargs: shown.append((args, kwargs, status.value)),
    )
    error = error_dialogs.prepare_error(ValueError("日期无效"))
    assert error_dialogs.show_error(parent, "回测失败", error, status) is None
    assert shown == [(("回测失败", "日期无效"), {"parent": parent}, "正在回测")]
    assert status.value == "❌ 回测失败: 日期无效"


class _Widget:
    """没有 grab/wait 方法：若窗口退回模态，这个替身会直接失败。"""

    def __init__(self, parent=None, **kwargs):
        self.parent = parent
        self.options = kwargs
        self.children = []
        self.visible = False
        self.destroyed = False
        self.content = ""
        self.bindings = {}
        self.protocols = {}
        if isinstance(parent, _Widget):
            parent.children.append(self)

    def configure(self, **kwargs): self.options.update(kwargs)
    def grid(self, **kwargs): self.visible = True
    def grid_remove(self): self.visible = False
    def pack(self, **kwargs): self.visible = True
    def insert(self, index, text): self.content += text
    def title(self, text): self.window_title = text
    def geometry(self, geometry): self.window_geometry = geometry
    def minsize(self, width, height): self.minimum_size = (width, height)
    def transient(self, parent): self.transient_parent = parent
    def grid_rowconfigure(self, *args, **kwargs): pass
    def grid_columnconfigure(self, *args, **kwargs): pass
    def protocol(self, name, callback): self.protocols[name] = callback
    def bind(self, name, callback): self.bindings[name] = callback
    def lift(self): self.lifted = True
    def destroy(self): self.destroyed = True
    def clipboard_clear(self): self.clipboard = ""
    def clipboard_append(self, text): self.clipboard += text


def test_wind_dialog_is_non_modal_and_details_can_be_expanded_copied_and_closed(monkeypatch):
    for name in ("CTkToplevel", "CTkLabel", "CTkTextbox", "CTkFrame", "CTkButton"):
        monkeypatch.setattr(error_dialogs.ctk, name, _Widget)
    monkeypatch.setattr(
        error_dialogs.messagebox, "showerror",
        lambda *args, **kwargs: pytest.fail("Wind 提示不应打开系统模态框"),
    )
    parent = _Widget()
    status = _Var()
    error = error_dialogs.prepare_error(WindImportError(DETAILS))
    window = error_dialogs.show_error(parent, "回测失败", error, status)

    assert window.parent is parent
    assert window.transient_parent is parent
    assert window.lifted
    labels = window.children[:2]
    assert [label.options["text"] for label in labels] == [DETAILS.summary, DETAILS.guidance]
    diagnostic, buttons = window.children[2:]
    assert diagnostic.content == DETAILS.diagnostic
    assert diagnostic.options["state"] == "disabled"
    assert not diagnostic.visible
    assert status.value == f"❌ 回测失败: {DETAILS.summary}"
    assert "ModuleNotFoundError" not in status.value

    expand, copy, close = buttons.children
    expand.options["command"]()
    assert diagnostic.visible
    assert expand.options["text"] == "收起技术详情"
    expand.options["command"]()
    assert not diagnostic.visible
    assert expand.options["text"] == "查看技术详情"
    copy.options["command"]()
    assert DETAILS.diagnostic in window.clipboard
    assert DETAILS.summary in window.clipboard
    assert copy.options["text"] == "已复制诊断"
    close.options["command"]()
    assert window.destroyed
    assert not parent.destroyed


@pytest.mark.parametrize("operation", ["single", "strategy"])
@pytest.mark.parametrize("wind_error", [True, False])
def test_backtest_workers_release_exception_resources_before_gui_callbacks(
    monkeypatch, operation, wind_error,
):
    finalized_on = []
    shown = []
    callbacks = []
    button_states = []
    finish_calls = []

    class WorkerResource:
        def __del__(self):
            finalized_on.append(threading.get_ident())

    def fail_in_worker(*args, **kwargs):
        resource = WorkerResource()  # noqa: F841 — 由异常 traceback 保活的线程资源
        if wind_error:
            try:
                raise WindImportError(DETAILS)
            except WindImportError as exc:
                raise RuntimeError("数据源创建失败") from exc
        raise RuntimeError("模拟数据错误")

    app = SimpleNamespace(
        _get_provider=fail_in_worker,
        _build_strategy_provider=fail_in_worker,
        v_bt_status=_Var(),
        v_st_status=_Var(),
        btn_backtest=SimpleNamespace(configure=lambda **kwargs: button_states.append(kwargs)),
        _finish_strategy_backtest=lambda: finish_calls.append(True),
        after=lambda delay, callback, *args: callbacks.append((callback, args)),
    )
    monkeypatch.setattr(
        error_dialogs, "_show_wind_error",
        lambda parent, title, error: shown.append((parent, title, error)),
    )
    native_dialogs = []
    monkeypatch.setattr(
        error_dialogs.messagebox, "showerror",
        lambda *args, **kwargs: native_dialogs.append((args, kwargs)),
    )
    if operation == "single":
        target = backtest.BacktestMixin._backtest_worker
        args = (app, "123999.SZ", date(2025, 1, 1), date(2026, 1, 1), "M", {})
    else:
        target = strategy_run.StrategyRunMixin._strategy_backtest_worker
        args = (app, ["123999.SZ"], date(2025, 1, 1), date(2026, 1, 1),
                "Wind", None, None, {}, "Wind高保真", {})
    worker = threading.Thread(target=target, args=args, daemon=True)
    worker.start()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert finalized_on == [worker.ident], "异常资源必须在 worker 释放，不能等 GUI 回调"
    assert callbacks
    for callback, args in callbacks:
        assert not any(isinstance(arg, BaseException) for arg in args)
        callback(*args)

    status = app.v_bt_status if operation == "single" else app.v_st_status
    if wind_error:
        assert len(shown) == 1
        error = shown[0][2]
        assert "ModuleNotFoundError" not in status.value
        assert ("akshare" in error.message) is (operation == "single")
        assert not native_dialogs
    else:
        assert not shown
        assert len(native_dialogs) == 1
        assert "模拟数据错误" in status.value
    assert button_states == ([{"state": "normal"}] if operation == "single" else [])
    assert finish_calls == ([True] if operation == "strategy" else [])
