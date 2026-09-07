"""CLI 子端统一 UTF-8: 用真实 cp936 文本流覆盖成功、失败和冻结分派。"""
from __future__ import annotations

import importlib
import io
import os
import subprocess
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from convertible_bond.cli import POOL_SYNC_MODULES
from convertible_bond.cli._console import configure_utf8_stdio


ROOT = Path(__file__).resolve().parent.parent
CLI_MODULES = sorted(POOL_SYNC_MODULES)
HELP_MODULES = [*CLI_MODULES, "convertible_bond.cli.strategy_backtest"]


def _run_cp936(args: list[str]) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONIOENCODING="cp936:strict")
    return subprocess.run([sys.executable, *args], cwd=ROOT, env=env,
                          capture_output=True, timeout=30)


def _prepare_sync_cli(monkeypatch, module, tmp_path, *, fail: bool):
    """隔离业务取数和写盘, 仍跑安装 console-script 实际调用的 main()。"""
    code = "123001.SZ"
    name = module.__name__.rsplit(".", 1)[-1]
    monkeypatch.setattr(sys, "argv", [module.__name__])

    def failing(*args, **kwargs):
        raise RuntimeError("模拟连接失败🧪\udcff")

    if name == "sync_new_issues":
        report = {
            "on_date": date(2026, 9, 7), "n_listings": 1, "n_tracked": 1,
            "changes": [{"kind": "new_bond", "bond_code": code, "bond_name": "测试转债",
                         "field": "listing_date", "before": None, "after": None}],
        }
        monkeypatch.setattr(module, "sync_new_issues", failing if fail else lambda *a, **k: report)
        return 1 if fail else 0

    bundle = SimpleNamespace(path=tmp_path / "unused.json", list_bonds=lambda: [code])
    monkeypatch.setattr(module, "TermsBundle", lambda *args: bundle)
    monkeypatch.setattr(module, "_make_provider", failing if fail else lambda _: SimpleNamespace(name="模拟源"))
    if name == "sync_tradable":
        monkeypatch.setattr(sys, "argv", [module.__name__, "--codes", code])
        monkeypatch.setattr(module, "sync_cb_terms", lambda *a, **k: {
            "success": [code], "failed": [], "dropped": [], "skipped": [],
        })
        monkeypatch.setattr(module, "_save_history_snapshot", lambda *args: None)
    elif name == "sync_admission_status":
        monkeypatch.setattr(module, "refresh_admission_status", lambda *a, **k: {
            "success": [code], "changed": [], "excluded": [], "failed": [], "excluded_by_reason": {},
        })
    else:
        monkeypatch.setattr(module, "CBEventStore", lambda *args: SimpleNamespace(path=tmp_path / "events.json"))
        monkeypatch.setattr(module, "TermsPatchStore", lambda *args: object())
        monkeypatch.setattr(module, "sync_cb_events", lambda *a, **k: {
            "scanned_announcements": 1, "parsed_events": [], "added": 0, "failed": [],
        })
    return 2 if fail else 0


@pytest.mark.parametrize("module_name", CLI_MODULES)
@pytest.mark.parametrize("fail", [False, True], ids=["success", "failure"])
@pytest.mark.parametrize("encoding", ["cp936", "cp1252"])
def test_console_script_main_outputs_utf8_from_legacy_streams(monkeypatch, tmp_path, module_name, fail, encoding):
    module = importlib.import_module(module_name)
    expected_rc = _prepare_sync_cli(monkeypatch, module, tmp_path, fail=fail)
    out_bytes, err_bytes = io.BytesIO(), io.BytesIO()
    stdout = io.TextIOWrapper(out_bytes, encoding=encoding, errors="strict")
    stderr = io.TextIOWrapper(err_bytes, encoding=encoding, errors="strict")
    with monkeypatch.context() as streams:
        streams.setattr(sys, "stdout", stdout)
        streams.setattr(sys, "stderr", stderr)
        assert module.main() == expected_rc
    assert stdout.encoding == stderr.encoding == "utf-8"
    assert stdout.errors == stderr.errors == "backslashreplace"
    stdout.flush()
    stderr.flush()
    out, err = out_bytes.getvalue().decode("utf-8"), err_bytes.getvalue().decode("utf-8")
    if fail:
        assert "❌" in err and "模拟连接失败🧪" in err and "\\udcff" in err
    else:
        assert "🆕" in out if module_name.endswith("sync_new_issues") else "✅" in out
        assert err == ""
    assert list(tmp_path.iterdir()) == [], "编码验证不应访问网络或写入业务数据"


@pytest.mark.parametrize("module_name", HELP_MODULES)
def test_python_m_help_uses_utf8_on_cp936_pipe(module_name):
    result = _run_cp936(["-m", module_name, "--help"])
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert any(word in result.stdout.decode("utf-8") for word in ("用法", "同步", "回测"))
    assert result.stderr == b""


@pytest.mark.parametrize("module_name", HELP_MODULES)
def test_python_m_argument_error_uses_utf8_with_emoji(module_name):
    required = (["--start", "2026-01-01", "--end", "2026-02-01"]
                if module_name.endswith("strategy_backtest") else [])
    result = _run_cp936(["-m", module_name, *required, "--未知参数🧪"])
    assert result.returncode == 2
    assert "未知参数🧪" in result.stderr.decode("utf-8")
    assert b"UnicodeEncodeError" not in result.stderr


def test_frozen_cli_dispatch_configures_streams_before_target_import():
    target = CLI_MODULES[0]
    script = f"""
import importlib, runpy, sys
from types import SimpleNamespace
real_import = importlib.import_module
def import_with_output(name):
    if name == {target!r}:
        assert 'convertible_bond.gui.app' not in sys.modules
        print('模块导入✅')
        print('导入提示🧪', file=sys.stderr)
        return SimpleNamespace(main=lambda: print('命令完成🆕'))
    return real_import(name)
importlib.import_module = import_with_output
sys.argv = ['gui.py', '--run-cli', {target!r}]
runpy.run_path('gui.py', run_name='__main__')
"""
    result = _run_cp936(["-c", script])
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert "模块导入✅" in result.stdout.decode("utf-8")
    assert "命令完成🆕" in result.stdout.decode("utf-8")
    assert "导入提示🧪" in result.stderr.decode("utf-8")


def test_frozen_dispatch_rejection_is_utf8_before_any_cli_import():
    result = _run_cp936(["gui.py", "--run-cli", "不允许的模块🧪"])
    assert result.returncode == 2
    assert "不允许的模块🧪" in result.stderr.decode("utf-8")


def test_importing_console_helper_and_cli_modules_does_not_reconfigure_streams():
    script = f"""
import importlib, sys
before = [(id(s), s.encoding, s.errors) for s in (sys.stdout, sys.stderr)]
import convertible_bond.cli
import convertible_bond.cli._console
assert 'akshare' not in sys.modules and 'WindPy' not in sys.modules
assert not any(m in sys.modules for m in {HELP_MODULES!r})
for name in {HELP_MODULES!r}:
    importlib.import_module(name)
assert [(id(s), s.encoding, s.errors) for s in (sys.stdout, sys.stderr)] == before
print('unchanged')
"""
    result = _run_cp936(["-c", script])
    assert result.returncode == 0, result.stderr.decode("cp936", errors="replace")
    assert result.stdout in (b"unchanged\n", b"unchanged\r\n")


def test_utf8_helper_leaves_absent_or_caller_owned_streams_alone(monkeypatch):
    custom = io.StringIO()
    with monkeypatch.context() as streams:
        streams.setattr(sys, "stdout", None)
        streams.setattr(sys, "stderr", custom)
        configure_utf8_stdio()
        assert sys.stdout is None and sys.stderr is custom
    assert custom.getvalue() == ""
