"""Wind 初始化错误分类及平台指引，不连接真实终端。"""
import builtins
import sys
from types import SimpleNamespace

import pytest

from convertible_bond.data_providers import wind as wind_mod
from convertible_bond.data_providers.wind_errors import (
    WIND_CONNECT_FAILED, WIND_LOAD_FAILED, WIND_NOT_FOUND,
    WindConnectionError, WindImportError,
)
from convertible_bond.strategy_backtest import (
    _looks_like_transport_failure, _raise_if_source_transport_outage,
)


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
@pytest.mark.parametrize("failure", ["missing", "dependency", "dll", "symbol"])
def test_import_failure_classification_and_platform_guidance(monkeypatch, platform, failure):
    causes = {
        "missing": ModuleNotFoundError("No module named 'WindPy'", name="WindPy"),
        "dependency": ModuleNotFoundError("No module named 'native_helper'", name="native_helper"),
        "dll": OSError("DLL load failed: missing WindPy.dll"),
        "symbol": ImportError("cannot import name 'w' from 'WindPy'"),
    }
    original_import = builtins.__import__

    def import_with_failure(name, *args, **kwargs):
        if name == "WindPy":
            raise causes[failure]
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_with_failure)
    monkeypatch.setattr(wind_mod, "prepare_windpy_import_path", lambda: [])
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.delenv("CBLENS_WINDPY_PATH", raising=False)
    provider = wind_mod.WindDataProvider()
    with pytest.raises(WindImportError) as caught:
        provider._ensure()
    error = caught.value
    assert isinstance(error, ImportError)
    assert error.__cause__ is causes[failure]
    assert provider._connect_error is error
    assert error.details.summary == (WIND_NOT_FOUND if failure == "missing" else WIND_LOAD_FAILED)
    assert str(causes[failure]) in error.details.diagnostic
    assert "pip install" not in str(error)
    assert "[frozen build]" not in str(error)
    assert "下载版不会自带" not in str(error)
    assert ("/Applications/" in str(error)) == (platform == "darwin")
    assert (r"C:\Software" in str(error)) == (platform == "win32")
    assert _looks_like_transport_failure(error)
    # core 已把异常转为行文本时，也不能漏掉系统性接口错误。
    assert _looks_like_transport_failure(f"条款获取失败: {error}")


@pytest.mark.parametrize("failure", ["return_code", "start_exception", "status_exception"])
def test_connection_failure_keeps_details_and_cooldown(monkeypatch, failure):
    calls = []

    def isconnected():
        calls.append("isconnected")
        if failure == "status_exception":
            raise OSError("status unavailable")
        return False

    def start(**kwargs):
        calls.append("start")
        if failure == "start_exception":
            raise OSError("connection unavailable")
        return SimpleNamespace(ErrorCode=-40521004, Data=[["not connected"]])

    monkeypatch.setitem(sys.modules, "WindPy", SimpleNamespace(w=SimpleNamespace(
        isconnected=isconnected, start=start,
    )))
    monkeypatch.setattr(wind_mod, "prepare_windpy_import_path", lambda: [])
    monkeypatch.setattr(wind_mod, "WIND_CONNECT_COOLDOWN_SEC", 3600)
    provider = wind_mod.WindDataProvider()
    for _ in range(2):
        with pytest.raises(WindConnectionError) as caught:
            provider._ensure()
        assert isinstance(caught.value, ConnectionError)
        assert caught.value.details.summary == WIND_CONNECT_FAILED
        assert "登录" in caught.value.details.guidance
    assert calls.count("isconnected") == 1
    if failure == "return_code":
        assert "ErrorCode=-40521004" in caught.value.details.diagnostic
        assert "not connected" in caught.value.details.diagnostic
    else:
        assert "OSError" in caught.value.details.diagnostic


def test_new_import_messages_still_stop_systemic_strategy_outage():
    from datetime import date

    for reason in (WIND_NOT_FOUND, WIND_LOAD_FAILED):
        excluded = [(str(i), f"条款获取失败: {reason}") for i in range(30)]
        with pytest.raises(RuntimeError, match="系统性 Wind 条款获取失败"):
            _raise_if_source_transport_outage(
                excluded, total_count=30, period_start=date(2026, 9, 7), phase="准入筛选",
            )
