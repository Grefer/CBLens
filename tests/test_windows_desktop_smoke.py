"""Windows 无控制台发布烟测必须证明原始无句柄，并等待/清理 onefile 进程树。"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from scripts import check_windows_desktop as smoke


def _valid_report(result_path):
    identity = {"schema_version": 1, "version": "2.0.0rc4", "commit": "a" * 40,
                "source_ref": "refs/tags/v2.0.0-rc.4", "release_tag": "v2.0.0-rc.4",
                "platform": "win32", "architecture": "AMD64"}
    report = {
        "schema_version": 1, "ok": True, "frozen": True, "platform": "win32",
        "version": identity["version"], "build": identity, "pid": 102,
        "output": str(result_path.resolve()), "launcher": {"pid": 101, "exit_code": 0},
        "startup_stdio": {
            "python_none": dict.fromkeys(("stdin", "stdout", "stderr"), True),
            "windows_handles": {name: {"kind": "null", "handle": 0, "file_type": 0}
                                for name in ("stdin", "stdout", "stderr")},
        },
        "streams": {name: {"missing": False, "usable": True, "encoding": "utf-8"}
                    for name in ("stdout", "stderr")},
        "stdio_probe": {"ok": True, "network_used": False},
    }
    return report, copy.deepcopy(identity)


def _valid_wind_report(interface, result_path, token):
    return {
        "schema_version": 1, "ok": True, "loaded": True, "stage": "load",
        "connected": None, "cancelled": False, "bundled": False,
        "source": "probe", "path": str(interface), "selection_path": str(interface),
        "output": str(result_path.resolve()), "token": token, "pid": 202,
        "launcher": {"pid": 201, "exit_code": 0, "detached": True},
        "smoke": {"interface_kind": "external_python_stub", "loaded": True,
                  "gui_loaded": False, "api_calls": [], "network_used": False},
    }


def test_detached_launch_supplies_null_handles_without_redirects(monkeypatch):
    class Startup:
        dwFlags = 0

    monkeypatch.setattr(smoke.subprocess, "STARTUPINFO", Startup, raising=False)
    monkeypatch.setattr(smoke.subprocess, "STARTF_USESTDHANDLES", 0x100, raising=False)
    monkeypatch.setattr(smoke.subprocess, "DETACHED_PROCESS", 0x8, raising=False)
    monkeypatch.setattr(smoke.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)
    calls = []

    class Process:
        pid = 123

        def wait(self, *, timeout):
            calls.append(("wait", timeout))
            return 0

    def launch(command, **kwargs):
        startup = kwargs["startupinfo"]
        assert startup.dwFlags & 0x100
        assert (startup.hStdInput, startup.hStdOutput, startup.hStdError) == (0, 0, 0)
        assert kwargs["creationflags"] == 0x208 and kwargs["close_fds"] is True
        assert not {"stdin", "stdout", "stderr"} & kwargs.keys()
        calls.append(("launch", command))
        return Process()

    monkeypatch.setattr(smoke.subprocess, "Popen", launch)
    assert smoke._run_detached(["CBLens.exe"], env={}, timeout=12) == (123, 0)
    assert calls == [("launch", ["CBLens.exe"]), ("wait", 12)]


def test_detached_timeout_kills_the_entire_tree_and_waits_again(monkeypatch):
    calls = []

    class Process:
        pid = 123

        def wait(self, *, timeout):
            calls.append(("wait", timeout))
            if len(calls) == 1:
                raise subprocess.TimeoutExpired("CBLens.exe", timeout)
            return 1

        def poll(self):
            return 1

    monkeypatch.setattr(smoke, "_detached_options", lambda: {})
    monkeypatch.setattr(smoke.subprocess, "Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr(smoke.subprocess, "run", lambda command, **kwargs: calls.append(("kill", command)))
    with pytest.raises(TimeoutError, match="进程树已终止"):
        smoke._run_detached(["CBLens.exe"], env={}, timeout=1)
    assert calls == [("wait", 1), ("kill", ["taskkill", "/PID", "123", "/T", "/F"]), ("wait", 10)]


@pytest.mark.parametrize("problem", [
    "python_pipe", "windows_pipe", "missing_snapshot", "broken_stream", "probe_failed",
    "wrong_commit", "wrong_pid", "old_output", "failed_exit",
])
def test_report_cannot_pass_without_actual_detached_frozen_evidence(tmp_path, problem):
    path = tmp_path / "new-result.json"
    report, manifest = _valid_report(path)
    smoke._validate_report(report, manifest, path)
    if problem == "python_pipe":
        report["startup_stdio"]["python_none"]["stdout"] = False
    elif problem == "windows_pipe":
        report["startup_stdio"]["windows_handles"]["stdout"] = {"kind": "valid", "handle": 99, "file_type": 3}
    elif problem == "missing_snapshot":
        report["startup_stdio"] = None
    elif problem == "broken_stream":
        report["streams"]["stderr"]["usable"] = False
    elif problem == "probe_failed":
        report["stdio_probe"]["ok"] = False
    elif problem == "wrong_commit":
        report["build"]["commit"] = "b" * 40
    elif problem == "wrong_pid":
        report["pid"] = None
    elif problem == "old_output":
        report["output"] = str(tmp_path / "old-result.json")
    elif problem == "failed_exit":
        report["launcher"]["exit_code"] = 1
    with pytest.raises(ValueError):
        smoke._validate_report(report, manifest, path)


def test_old_success_report_is_never_used_when_new_process_writes_nothing(monkeypatch, tmp_path):
    output = tmp_path / "last-success.json"
    report, manifest = _valid_report(output)
    output.write_text(json.dumps(report), encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(smoke.sys, "platform", "win32")
    monkeypatch.setattr(smoke, "_run_detached", lambda *args, **kwargs: (123, 0))
    with pytest.raises(FileNotFoundError):
        smoke.main(["--exe", str(tmp_path / "CBLens.exe"), "--manifest", str(manifest_path), "--output", str(output)])


@pytest.mark.parametrize("field,value", [
    ("ok", False), ("loaded", False), ("stage", "connection"), ("connected", True),
    ("bundled", True), ("source", "settings"), ("path", "old/WindPy.py"),
    ("selection_path", "old/WindPy.py"), ("token", "old-token"), ("output", "old-result.json"),
    ("pid", 201), ("launcher", {"pid": 201, "exit_code": 1}),
    ("smoke", {"interface_kind": "external_python_stub", "loaded": True,
               "gui_loaded": False, "api_calls": ["w.start"], "network_used": False}),
])
def test_wind_probe_rejects_wrong_identity_or_side_effects(tmp_path, field, value):
    interface, output, token = tmp_path / "WindPy.py", tmp_path / "result.json", "fresh-token"
    report = _valid_wind_report(interface, output, token)
    smoke._validate_wind_probe_report(report, interface=interface, result_path=output, token=token)
    report[field] = value
    with pytest.raises(ValueError):
        smoke._validate_wind_probe_report(report, interface=interface, result_path=output, token=token)


def test_generated_wind_stub_records_import_and_forbids_connections(tmp_path):
    """实际执行替身源码；用独立解释器避免 socket 拦截改变 pytest 进程。"""
    interface, marker = smoke._write_wind_stub(tmp_path / "stub")
    script = """
import runpy, socket, sys
module = runpy.run_path(sys.argv[1])
assert callable(module['w'].start) and callable(module['w'].isconnected)
for action in (module['w'].start, module['w'].isconnected,
               lambda: socket.create_connection(('127.0.0.1', 1))):
    try:
        action()
    except AssertionError:
        pass
    else:
        raise AssertionError('替身没有拦住连接')
"""
    result = subprocess.run([sys.executable, "-c", script, str(interface)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    evidence = json.loads(marker.read_text(encoding="utf-8"))
    assert evidence == {"loaded": True, "gui_loaded": False,
                        "api_calls": ["w.start", "w.isconnected", "socket.create_connection"],
                        "network_used": True}


def test_release_stub_passes_actual_probe_dispatch_without_gui_or_connection(tmp_path):
    interface, marker = smoke._write_wind_stub(tmp_path / "stub")
    output = tmp_path / "probe.json"
    entry = Path(__file__).resolve().parents[1] / "gui.py"
    env = {**os.environ, "CBLENS_CONFIG_DIR": str(tmp_path / "config"),
           "CBLENS_DATA_DIR": str(tmp_path / "data"),
           "CBLENS_WINDPY_SESSION_SELECTION": json.dumps({"path": str(interface), "source": "probe"})}
    result = subprocess.run([sys.executable, str(entry), "--wind-probe", "--output", str(output),
                             "--token", "this-probe"], env=env, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["ok"] is True and report["loaded"] is True and report["connected"] is None
    assert report["path"] == str(interface) and report["token"] == "this-probe"
    assert json.loads(marker.read_text(encoding="utf-8")) == {
        "loaded": True, "gui_loaded": False, "api_calls": [], "network_used": False,
    }


def test_release_check_runs_and_saves_both_detached_reports(monkeypatch, tmp_path):
    output = tmp_path / "combined.json"
    _, manifest = _valid_report(output)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(smoke.sys, "platform", "win32")
    launches = []

    def launch(command, *, env, timeout):
        launches.append(command)
        result_path = Path(command[command.index("--output") + 1])
        assert env["CBLENS_CONFIG_DIR"] == str(result_path.parent / "config")
        if "--diagnose" in command:
            report, _ = _valid_report(result_path)
            launcher_pid = 101
        else:
            assert command[1] == "--wind-probe" and "--connect" not in command
            selection = json.loads(env["CBLENS_WINDPY_SESSION_SELECTION"])
            interface = Path(selection["path"])
            assert selection["source"] == "probe" and interface.is_file()
            token = command[command.index("--token") + 1]
            report = _valid_wind_report(interface, result_path, token)
            marker = interface.parent / "import.json"
            marker.write_text(json.dumps({key: value for key, value in report["smoke"].items()
                                          if key != "interface_kind"}), encoding="utf-8")
            launcher_pid = 201
        result_path.write_text(json.dumps(report), encoding="utf-8")
        return launcher_pid, 0

    monkeypatch.setattr(smoke, "_run_detached", launch)
    assert smoke.main(["--exe", str(tmp_path / "CBLens.exe"), "--manifest", str(manifest_path),
                       "--output", str(output)]) == 0
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert len(launches) == 2
    assert saved["ok"] is True and saved["wind_probe"]["loaded"] is True
    assert saved["wind_probe"]["smoke"]["interface_kind"] == "external_python_stub"


def test_wind_probe_failure_blocks_release_and_is_saved(monkeypatch, tmp_path):
    output = tmp_path / "combined.json"
    _, manifest = _valid_report(output)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(smoke.sys, "platform", "win32")

    def launch(command, **kwargs):
        result_path = Path(command[command.index("--output") + 1])
        report, _ = _valid_report(result_path)
        result_path.write_text(json.dumps(report), encoding="utf-8")
        return 101, 0

    def fail_probe(*args, **kwargs):
        raise ValueError("冻结包未包含 Wind 检测入口")

    monkeypatch.setattr(smoke, "_run_detached", launch)
    monkeypatch.setattr(smoke, "_run_wind_probe", fail_probe)
    with pytest.raises(ValueError, match="冻结包未包含"):
        smoke.main(["--exe", str(tmp_path / "CBLens.exe"), "--manifest", str(manifest_path),
                    "--output", str(output)])
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved["ok"] is False
    assert saved["wind_probe"]["ok"] is False
    assert "冻结包未包含" in saved["wind_probe"]["error"]


@pytest.mark.skipif(sys.platform != "win32", reason="需要 Windows 原生 CreateProcess 与标准句柄")
def test_native_windows_launcher_really_removes_standard_handles(tmp_path):
    hook = Path(__file__).resolve().parents[1] / "pyi_rth_stdio.py"
    script = tmp_path / "child.py"
    output = tmp_path / "native-handles.json"
    script.write_text("""
import json, pathlib, runpy, sys
sys.frozen = True
runpy.run_path(sys.argv[1])
pathlib.Path(sys.argv[2]).write_text(json.dumps(sys._cblens_stdio_startup), encoding='utf-8')
""", encoding="utf-8")
    _, returncode = smoke._run_detached(
        [sys.executable, str(script), str(hook), str(output)], env=os.environ.copy(), timeout=15)
    assert returncode == 0
    startup = json.loads(output.read_text(encoding="utf-8"))
    assert all(startup["python_none"].values())
    for handle in startup["windows_handles"].values():
        assert handle["kind"] in {"null", "invalid"} and handle["file_type"] == 0
