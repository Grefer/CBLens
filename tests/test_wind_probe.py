"""Wind 独立检测只使用临时假模块；覆盖加载、连接、取消和进程隔离。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import types

import pytest

from convertible_bond import wind_probe as probe
from convertible_bond import wind_config


def _fake_module(tmp_path, code):
    path = tmp_path / "WindPy.py"
    path.write_text("""
class DefaultW:
    def start(self, **kwargs):
        raise AssertionError('default test should only load')
    def isconnected(self):
        raise AssertionError('default test should not query connection')
w = DefaultW()
""" + code, encoding="utf-8")
    return path


def test_loading_never_queries_or_connects_wind_and_tolerates_noisy_stdout(tmp_path):
    path = _fake_module(tmp_path, """
import sys
assert 'convertible_bond.gui.app' not in sys.modules
print('接口输出 🆕' * 20000)
class W:
    def start(self, **kwargs):
        raise AssertionError('load-only must not connect')
    def isconnected(self):
        raise AssertionError('load-only must not query connection')
w = W()
""")
    result = probe.run_wind_probe(str(path), timeout=8)
    assert result["ok"] is True and result["loaded"] is True
    assert result["stage"] == "load" and result["connected"] is None
    assert result["path"] == str(path.resolve()) and result["source"] == "probe"
    assert "接口输出" in result["diagnostic"]
    assert len(result["diagnostic"].encode("utf-8")) < 12_100


@pytest.mark.parametrize("error_code, connected", [(0, True), (0, False), (-40520009, False)])
def test_connection_is_explicit_and_loading_success_survives_login_failure(tmp_path, error_code, connected):
    path = _fake_module(tmp_path, f"""
class Result:
    ErrorCode = {error_code}
    Data = ['模拟终端结果']
class W:
    checked = False
    def start(self, *, waitTime):
        assert 0 < waitTime <= 15
        return Result()
    def isconnected(self):
        if not self.checked:
            self.checked = True
            return False
        assert {error_code} == 0
        return {connected}
w = W()
""")
    result = probe.run_wind_probe(str(path), connect=True, timeout=8)
    assert result["loaded"] is True and result["path"] == str(path.resolve())
    assert result["stage"] == "connection"
    assert result["ok"] is connected and result["connected"] is connected


def test_invalid_path_and_native_load_error_are_distinguished(tmp_path):
    result = probe.run_wind_probe(str(tmp_path / "missing"), timeout=8)
    assert result["stage"] == "discovery" and result["loaded"] is False
    path = _fake_module(tmp_path, "raise OSError('模拟 DLL 加载错误')\n")
    result = probe.run_wind_probe(str(path), timeout=8)
    assert result["stage"] == "load" and result["loaded"] is False
    assert "模拟 DLL 加载错误" in result["diagnostic"]


def test_empty_module_is_not_a_usable_wind_interface(tmp_path):
    path = _fake_module(tmp_path, "del w\n")
    result = probe.run_wind_probe(str(path), timeout=8)
    assert result["ok"] is False and result["loaded"] is False and result["stage"] == "load"
    assert "w.start / w.isconnected" in result["diagnostic"]


def test_already_connected_probe_does_not_start_again(tmp_path):
    path = _fake_module(tmp_path, """
class W:
    def start(self, **kwargs):
        raise AssertionError('must not restart existing connection')
    def isconnected(self):
        return True
w = W()
""")
    result = probe.run_wind_probe(str(path), connect=True, timeout=8)
    assert result["ok"] is True and result["connected"] is True and result["loaded"] is True


def test_probe_uses_frozen_session_selection_without_changing_parent_environment(tmp_path, monkeypatch):
    path = _fake_module(tmp_path, "# chosen module\n")
    monkeypatch.setattr(wind_config, "_session_selection", {"path": str(path), "source": "settings"})
    monkeypatch.setenv("WINDPY_PATH", str(tmp_path / "wrong-module"))
    before = dict(os.environ)
    result = probe.run_wind_probe(timeout=8)
    assert result["ok"] is True and result["source"] == "settings"
    assert result["path"] == str(path.resolve())
    assert dict(os.environ) == before


@pytest.mark.parametrize("during", ["load", "connection"])
def test_probe_timeout_covers_import_and_connection_and_reaps_process(tmp_path, during):
    marker = tmp_path / "pid.txt"
    preamble = f"import os, time\nfrom pathlib import Path\nPath({str(marker)!r}).write_text(str(os.getpid()))\n"
    body = ("time.sleep(30)\n" if during == "load" else """
class W:
    def start(self, **kwargs):
        time.sleep(30)
    def isconnected(self):
        return False
w = W()
""")
    path = _fake_module(tmp_path, preamble + body)
    start = time.monotonic()
    result = probe.run_wind_probe(str(path), connect=during == "connection", timeout=2)
    assert result["stage"] == "timeout" and result["ok"] is False
    assert time.monotonic() - start < 8
    if sys.platform != "win32" and marker.exists():
        with pytest.raises(ProcessLookupError):
            os.kill(int(marker.read_text()), 0)


def test_cancel_event_stops_an_active_child(tmp_path):
    marker = tmp_path / "entered.txt"
    path = _fake_module(tmp_path, f"""
from pathlib import Path
import time
Path({str(marker)!r}).write_text('entered')
time.sleep(30)
""")
    cancel = threading.Event()

    def close_dialog():
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        cancel.set()

    thread = threading.Thread(target=close_dialog, daemon=True)
    thread.start()
    result = probe.run_wind_probe(str(path), timeout=8, cancel_event=cancel)
    thread.join(timeout=1)
    assert result["cancelled"] is True and result["stage"] == "process"
    assert result["message"] == "检测已取消"


def _pid_is_running(pid):
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE，只查询、不终止
        if not handle:
            return False
        try:
            return kernel.WaitForSingleObject(handle, 0) == 0x00000102  # WAIT_TIMEOUT
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.parametrize("cancel_before_exit", [False, True])
def test_normal_parent_exit_reaps_probe_even_when_daemon_cannot_poll(tmp_path, cancel_before_exit):
    marker = tmp_path / "probe-pid.txt"
    path = _fake_module(tmp_path, f"""
import os, time
from pathlib import Path
Path({str(marker)!r}).write_text(str(os.getpid()), encoding='utf-8')
time.sleep(30)
""")
    driver = f"""
import threading, time
from pathlib import Path
from convertible_bond.wind_probe import run_wind_probe
cancel = threading.Event()
threading.Thread(target=run_wind_probe, args=({str(path)!r},),
                 kwargs={{'timeout': 20, 'cancel_event': cancel}}, daemon=True).start()
deadline = time.monotonic() + 8
while not Path({str(marker)!r}).exists():
    if time.monotonic() > deadline:
        raise RuntimeError('fake WindPy never entered import')
    time.sleep(.005)
if {cancel_before_exit!r}:
    cancel.set()
# 模拟关闭整个应用：不 join daemon、不等它下一次取消轮询，正常退出解释器。
"""
    try:
        parent = subprocess.run([sys.executable, "-c", driver], capture_output=True, timeout=15)
        assert parent.returncode == 0, parent.stderr.decode("utf-8", errors="replace")
        assert marker.exists()
        pid = int(marker.read_text(encoding="utf-8"))
        assert not _pid_is_running(pid), "正常退出后仍留下没有 timeout 管理者的 Wind 检测进程"
    finally:
        # 回归失败时也不把测试制造的 30 秒进程留给开发机/CI。
        if marker.exists():
            pid = int(marker.read_text(encoding="utf-8"))
            if _pid_is_running(pid):
                if sys.platform == "win32":
                    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
                else:
                    os.killpg(pid, probe.signal.SIGKILL)


def test_exit_waits_for_launch_registration_and_prevents_later_launches(monkeypatch):
    monkeypatch.setattr(probe, "_ACTIVE_PROBES", {})
    monkeypatch.setattr(probe, "_SHUTTING_DOWN", False)
    entered, allow_launch, exiting = threading.Event(), threading.Event(), threading.Event()
    stopped = []
    proc = types.SimpleNamespace(pid=123, poll=lambda: None)

    def launch(*args, **kwargs):
        entered.set()
        assert allow_launch.wait(timeout=3)
        return proc

    def stop(actual, log):
        assert log == subprocess.DEVNULL  # 退出清理不能借用已关闭的 worker 日志。
        stopped.append(actual.pid)

    def finish():
        exiting.set()
        probe._cleanup_probes_at_exit()

    monkeypatch.setattr(probe.subprocess, "Popen", launch)
    monkeypatch.setattr(probe, "_stop_process_tree", stop)
    worker = threading.Thread(target=probe._launch_probe, args=(["fake"],))
    cleanup = threading.Thread(target=finish)
    worker.start()
    try:
        assert entered.wait(timeout=3)
        cleanup.start()
        assert exiting.wait(timeout=3)
    finally:
        allow_launch.set()
        worker.join(timeout=3)
        if cleanup.ident is not None:
            cleanup.join(timeout=3)
    assert not worker.is_alive() and not cleanup.is_alive()
    assert stopped == [123] and probe._ACTIVE_PROBES == {}
    with pytest.raises(RuntimeError, match="正在退出"):
        probe._launch_probe(["fake"])


def test_exit_cleanup_attempts_every_registered_process_after_a_failure(monkeypatch):
    first = types.SimpleNamespace(pid=101, poll=lambda: None)
    second = types.SimpleNamespace(pid=102, poll=lambda: None)
    monkeypatch.setattr(probe, "_ACTIVE_PROBES", {id(first): first, id(second): second})
    monkeypatch.setattr(probe, "_SHUTTING_DOWN", False)
    stopped = []

    def stop(proc, log):
        stopped.append(proc.pid)
        if proc is first:
            raise OSError("模拟第一个进程清理失败")

    monkeypatch.setattr(probe, "_stop_process_tree", stop)
    probe._cleanup_probes_at_exit()
    assert stopped == [101, 102] and probe._ACTIVE_PROBES == {}


def _successful_child(command, *, source="probe", **kwargs):
    output = Path(command[command.index("--output") + 1])
    token = command[command.index("--token") + 1]
    selection = json.loads(kwargs["env"][wind_config.WINDPY_SESSION_ENV])
    report = probe._result(ok=True, loaded=True, stage="load", path="/fake/WindPy.py", source=source,
                           selection_path=selection["path"])
    report.update(schema_version=1, token=token, output=str(output.resolve()), pid=123)
    output.write_text(json.dumps(report), encoding="utf-8")

    class Process:
        pid = 122

        def wait(self, **kwargs):
            return 0

        def poll(self):
            return 0

    return Process()


@pytest.mark.parametrize("utf8_mode", [None, "0", "1"])
def test_probe_keeps_inherited_file_encoding_mode_but_uses_utf8_stdio(monkeypatch, utf8_mode):
    if utf8_mode is None:
        monkeypatch.delenv("PYTHONUTF8", raising=False)
    else:
        monkeypatch.setenv("PYTHONUTF8", utf8_mode)

    def launch(command, **kwargs):
        assert kwargs["env"].get("PYTHONUTF8") == utf8_mode
        assert kwargs["env"]["PYTHONIOENCODING"] == "utf-8"
        return _successful_child(command, **kwargs)

    monkeypatch.setattr(probe.subprocess, "Popen", launch)
    assert probe.run_wind_probe("/fake/path")["ok"] is True


def test_frozen_command_and_auto_override_do_not_read_broken_settings(monkeypatch):
    monkeypatch.setattr(probe.sys, "frozen", True, raising=False)
    monkeypatch.setattr(wind_config, "wind_subprocess_env", lambda: pytest.fail("explicit auto must not read settings"))
    monkeypatch.setenv("WINDPY_PATH", "/hostile/path")
    seen = []

    def launch(command, **kwargs):
        seen.append(command)
        assert command[:2] == [sys.executable, "--wind-probe"]
        assert json.loads(kwargs["env"][wind_config.WINDPY_SESSION_ENV]) == {"path": "", "source": "auto"}
        assert kwargs["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
        assert kwargs["env"]["PYTHONIOENCODING"] == "utf-8"
        assert kwargs["stdout"] != subprocess.PIPE and hasattr(kwargs["stdout"], "fileno")
        return _successful_child(command, source="auto", **kwargs)

    monkeypatch.setattr(probe.subprocess, "Popen", launch)
    result = probe.run_wind_probe("")
    assert result["ok"] is True and result["source"] == "auto" and len(seen) == 1


@pytest.mark.parametrize("invalid", ["token", "schema", "output", "bool", "bundled", "selection", "missing"])
def test_probe_rejects_missing_or_stale_or_malformed_child_report(monkeypatch, invalid):
    def launch(command, **kwargs):
        proc = _successful_child(command, **kwargs)
        output = Path(command[command.index("--output") + 1])
        if invalid == "missing":
            output.unlink()
        else:
            report = json.loads(output.read_text())
            key, value = {"token": ("token", "old"), "schema": ("schema_version", 99),
                          "output": ("output", "/old/result.json"), "bool": ("ok", "true"),
                          "bundled": ("bundled", "false"), "selection": ("selection_path", "/other/path")}[invalid]
            report[key] = value
            output.write_text(json.dumps(report), encoding="utf-8")
        return proc

    monkeypatch.setattr(probe.subprocess, "Popen", launch)
    result = probe.run_wind_probe("/fake/path")
    assert result["stage"] == "process" and result["ok"] is False


def test_abnormal_process_exit_cannot_leave_a_saveable_loaded_result(monkeypatch):
    def launch(command, **kwargs):
        proc = _successful_child(command, **kwargs)
        proc.wait = lambda **kwargs: 23
        return proc

    monkeypatch.setattr(probe.subprocess, "Popen", launch)
    result = probe.run_wind_probe("/fake/path")
    assert result["stage"] == "process" and result["loaded"] is False and result["ok"] is False
    assert "23" in result["diagnostic"]


@pytest.mark.parametrize("bundled", [True, False])
def test_child_report_distinguishes_extracted_bundle_from_external_selection(tmp_path, monkeypatch, bundled):
    from convertible_bond.data_providers import wind

    extracted = tmp_path / "_MEI1234"
    actual = (extracted if bundled else tmp_path / "external") / "WindPy.py"
    selected = "" if bundled else str(actual)
    source = "auto" if bundled else "probe"
    monkeypatch.setattr(probe.sys, "frozen", True, raising=False)
    monkeypatch.setattr(probe.sys, "_MEIPASS", str(extracted), raising=False)
    monkeypatch.setattr(wind_config, "get_session_wind_selection", lambda: {"path": selected, "source": source})
    api = types.SimpleNamespace(start=lambda **kwargs: pytest.fail("load-only"),
                                isconnected=lambda: pytest.fail("load-only"))
    monkeypatch.setattr(wind, "load_windpy", lambda: types.SimpleNamespace(__file__=str(actual), w=api))
    result = probe._probe_in_child(connect=False, connect_wait=15)
    assert result["loaded"] is True and result["bundled"] is bundled
    assert result["selection_path"] == selected and result["path"] == str(actual.resolve())


def test_macos_bundle_resources_are_marked_bundled(monkeypatch, tmp_path):
    app = tmp_path / "CBLens.app"
    monkeypatch.setattr(probe.sys, "platform", "darwin")
    monkeypatch.setattr(probe.sys, "frozen", True, raising=False)
    monkeypatch.setattr(probe.sys, "executable", str(app / "Contents/MacOS/CBLens"))
    monkeypatch.delattr(probe.sys, "_MEIPASS", raising=False)
    assert probe._is_bundled_module(str(app / "Contents/Resources/WindPy.py"))
    assert not probe._is_bundled_module(str(tmp_path / "WindPy.py"))


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_timeout_cleanup_targets_process_tree_or_new_session_and_waits(monkeypatch, tmp_path, platform):
    calls = []
    monkeypatch.setattr(probe.sys, "platform", platform)

    class Process:
        pid = 123

        def poll(self):
            return 1

        def wait(self, **kwargs):
            calls.append(("wait", kwargs["timeout"]))

    if platform == "win32":
        monkeypatch.setattr(probe.subprocess, "CREATE_NO_WINDOW", 0x8000000, raising=False)
        monkeypatch.setattr(probe.subprocess, "run", lambda command, **kwargs: calls.append(("tree", command)))
    else:
        # 这是 POSIX 分支的模拟；Windows 宿主没有 killpg / SIGKILL。
        monkeypatch.setattr(probe.os, "killpg", lambda pid, signum: calls.append(("group", pid, signum)),
                            raising=False)
        monkeypatch.setattr(probe.signal, "SIGKILL", 9, raising=False)
    with (tmp_path / "cleanup.log").open("w+b") as log:
        probe._stop_process_tree(Process(), log)
    assert calls[-1] == ("wait", 5)
    if platform == "win32":
        assert calls[0] == ("tree", ["taskkill", "/PID", "123", "/T", "/F"])
    else:
        assert calls[0] == ("group", 123, probe.signal.SIGKILL)


def test_failed_windows_tree_cleanup_still_kills_and_waits_for_direct_child(monkeypatch):
    calls = []
    monkeypatch.setattr(probe.sys, "platform", "win32")
    monkeypatch.setattr(probe.subprocess, "CREATE_NO_WINDOW", 0x8000000, raising=False)
    proc = types.SimpleNamespace(pid=123, poll=lambda: None,
                                 kill=lambda: calls.append("kill"),
                                 wait=lambda **kwargs: calls.append("wait"))

    def fail(*args, **kwargs):
        raise subprocess.TimeoutExpired("taskkill", 5)

    monkeypatch.setattr(probe.subprocess, "run", fail)
    with pytest.raises(subprocess.TimeoutExpired):
        probe._stop_process_tree(proc, subprocess.DEVNULL)
    assert calls == ["kill", "wait"]


def test_gui_dispatch_runs_probe_before_importing_gui(tmp_path):
    path = _fake_module(tmp_path, "import sys\nassert 'convertible_bond.gui.app' not in sys.modules\n")
    output = tmp_path / "result.json"
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, wind_config.WINDPY_SESSION_ENV: json.dumps({"path": str(path), "source": "probe"})}
    result = subprocess.run(
        [sys.executable, str(root / "gui.py"), "--wind-probe", "--output", str(output), "--token", "test-dispatch"],
        env=env, capture_output=True, timeout=8,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["ok"] is True and report["token"] == "test-dispatch"


def test_importing_probe_does_not_import_wind_or_gui():
    code = """
import sys
import convertible_bond.wind_probe
assert 'WindPy' not in sys.modules
assert 'convertible_bond.data_providers.wind' not in sys.modules
assert 'convertible_bond.gui.app' not in sys.modules
"""
    subprocess.run([sys.executable, "-c", code], check=True, timeout=8)
