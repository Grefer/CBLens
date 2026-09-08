"""在可终止的独立进程中检测 Wind 接口，避免导入/连接拖住 GUI。"""
from __future__ import annotations

import argparse
import atexit
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid


_SCHEMA_VERSION = 1
_STAGES = {"discovery", "load", "connection", "timeout", "process"}
_LOG_LIMIT = 12_000
_ACTIVE_LOCK = threading.Lock()
_ACTIVE_PROBES: dict[int, subprocess.Popen] = {}
_SHUTTING_DOWN = False


def _result(*, ok=False, loaded=False, stage="process", message="", diagnostic="",
            path=None, source="session", connected=None, cancelled=False,
            bundled=False, selection_path=None) -> dict:
    return {"ok": ok, "loaded": loaded, "stage": stage, "message": message,
            "diagnostic": diagnostic, "path": path, "source": source,
            "connected": connected, "cancelled": cancelled,
            "bundled": bundled, "selection_path": selection_path}


def _log_tail(log) -> str:
    log.flush()
    log.seek(0, os.SEEK_END)
    log.seek(max(0, log.tell() - _LOG_LIMIT))
    return log.read().decode("utf-8", errors="replace").strip()


def _stop_process_tree(proc, log) -> None:
    """终止 onefile 父子进程或 POSIX 新会话进程组，并回收直接子进程。"""
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           stdout=log, stderr=subprocess.STDOUT, timeout=5, check=False,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    finally:
        # taskkill 自身超时/失败也不能跳过直接子进程的 kill 和 wait。
        try:
            if proc.poll() is None:
                proc.kill()
        finally:
            proc.wait(timeout=5)


def _launch_probe(command, **kwargs):
    """启动与登记共用退出锁，避免 daemon 在两步之间退出留下孤儿进程。"""
    with _ACTIVE_LOCK:
        if _SHUTTING_DOWN:
            raise RuntimeError("应用正在退出，无法开始 Wind 检测")
        proc = subprocess.Popen(command, **kwargs)
        _ACTIVE_PROBES[id(proc)] = proc
        return proc


def _stop_registered_probe(proc, log) -> None:
    with _ACTIVE_LOCK:
        if id(proc) not in _ACTIVE_PROBES:
            return
        try:
            if proc.poll() is None:
                _stop_process_tree(proc, log)
        finally:
            if proc.poll() is not None:
                _ACTIVE_PROBES.pop(id(proc), None)


def _cleanup_probes_at_exit() -> None:
    """正常退出时仍由主线程回收检测树，不依赖 daemon 的下一次取消轮询。"""
    global _SHUTTING_DOWN
    with _ACTIVE_LOCK:
        _SHUTTING_DOWN = True
        for proc in list(_ACTIVE_PROBES.values()):
            try:
                if proc.poll() is None:
                    # 原 worker 的日志可能已关闭；清理只使用独立的空输出句柄。
                    _stop_process_tree(proc, subprocess.DEVNULL)
            except BaseException:
                # 一棵树清理失败仍须继续其他检测；_stop_process_tree 已尝试 kill/wait。
                pass
            finally:
                _ACTIVE_PROBES.pop(id(proc), None)


atexit.register(_cleanup_probes_at_exit)


def _valid_report(report, token: str, output: Path) -> bool:
    if not isinstance(report, dict):
        return False
    if (report.get("schema_version") != _SCHEMA_VERSION or report.get("token") != token
            or report.get("output") != str(output.resolve())
            or type(report.get("pid")) is not int or report["pid"] <= 0):
        return False
    if any(type(report.get(key)) is not bool for key in ("ok", "loaded", "cancelled", "bundled")):
        return False
    if report.get("stage") not in _STAGES:
        return False
    if any(not isinstance(report.get(key), str) for key in ("message", "diagnostic", "source")):
        return False
    if report.get("path") is not None and not isinstance(report["path"], str):
        return False
    if report.get("selection_path") is not None and not isinstance(report["selection_path"], str):
        return False
    return report.get("connected") is None or type(report["connected"]) is bool


def run_wind_probe(path: str | None = None, *, connect: bool = False, timeout: float = 45,
                   cancel_event: threading.Event | None = None) -> dict:
    """检测候选路径；None 沿用会话，空串明确使用自动发现，永不修改父进程选择。

    默认只加载模块；仅 connect=True 才连接终端。调用方应在自己的 worker 调用，
    关闭窗口可设置 cancel_event。返回值只包含可序列化状态和文字，不携带异常。
    """
    try:
        timeout = float(timeout)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("检测超时必须是大于零的有限秒数")
        if path is not None and not isinstance(path, str):
            raise ValueError("Wind 接口路径必须是字符串")
    except (TypeError, ValueError) as exc:
        return _result(message="无法开始 Wind 检测", diagnostic=str(exc))
    deadline = time.monotonic() + timeout
    try:
        from .wind_config import WINDPY_SESSION_ENV, wind_subprocess_env

        if path is None:
            env = wind_subprocess_env()
            selection = json.loads(env[WINDPY_SESSION_ENV])
        else:
            selection = {"path": path.strip(), "source": "probe" if path.strip() else "auto"}
            env = dict(os.environ)
            env[WINDPY_SESSION_ENV] = json.dumps(selection, ensure_ascii=False)
        source = selection["source"]
    except Exception as exc:
        return _result(stage="discovery", message="无法读取 Wind 接口选择", diagnostic=str(exc))
    if cancel_event is not None and cancel_event.is_set():
        return _result(message="检测已取消", source=source, selection_path=selection["path"], cancelled=True)
    # 只调整标准流，保留父进程的默认文件编码，忠实检测第三方 open()/WindPy.pth。
    env["PYTHONIOENCODING"] = "utf-8"
    frozen = bool(getattr(sys, "frozen", False))
    if frozen:
        # 独立检测重新走 onefile 启动；不能冒充当前 app 的 multiprocessing worker。
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        command = [sys.executable, "--wind-probe"]
    else:
        package_root = str(Path(__file__).resolve().parents[1])
        env["PYTHONPATH"] = os.pathsep.join(filter(None, (package_root, env.get("PYTHONPATH"))))
        command = [sys.executable, "-m", "convertible_bond.wind_probe"]
    launch_options = ({"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
                      if sys.platform == "win32" else {"start_new_session": True})
    proc = None
    try:
        with tempfile.TemporaryDirectory(prefix="cblens-wind-probe-") as temp:
            output = Path(temp) / "result.json"
            token = uuid.uuid4().hex
            command += ["--output", str(output), "--token", token]
            if connect:
                command += ["--connect", "--connect-wait", str(max(1, min(15, int(timeout))))]
            with (Path(temp) / "process.log").open("w+b") as log:
                # 普通文件不会像未读取的 PIPE 一样被大量 Wind 输出塞满。
                proc = _launch_probe(command, stdin=subprocess.DEVNULL, stdout=log,
                                     stderr=subprocess.STDOUT, env=env, **launch_options)
                while True:
                    cancelled = cancel_event is not None and cancel_event.is_set()
                    remaining = deadline - time.monotonic()
                    if cancelled or remaining <= 0:
                        _stop_registered_probe(proc, log)
                        return _result(stage="process" if cancelled else "timeout", source=source,
                                       selection_path=selection["path"],
                                       message="检测已取消" if cancelled else "Wind 检测超时，检测进程已终止",
                                       diagnostic=_log_tail(log), cancelled=cancelled)
                    try:
                        returncode = proc.wait(timeout=min(0.1, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        continue
                diagnostic = _log_tail(log)
                try:
                    if output.stat().st_size > 100_000:
                        raise ValueError("检测结果文件过大")
                    report = json.loads(output.read_text(encoding="utf-8"))
                    if not _valid_report(report, token, output):
                        raise ValueError("检测结果格式或本轮身份无效")
                    if report["source"] != source or report["selection_path"] != selection["path"]:
                        raise ValueError("检测结果与本轮接口选择不一致")
                except (OSError, ValueError) as exc:
                    return _result(source=source, message="Wind 检测进程未返回有效结果",
                                   selection_path=selection["path"],
                                   diagnostic=f"退出码 {returncode}: {exc}\n{diagnostic}".strip())
                public = {key: report[key] for key in _result()}
                public["diagnostic"] = "\n".join(filter(None, (public["diagnostic"], diagnostic)))
                if returncode != (0 if report["ok"] else 1):
                    public.update(ok=False, loaded=False, connected=None,
                                  stage="process", message="Wind 检测进程异常退出")
                    public["diagnostic"] = f"退出码 {returncode}\n{public['diagnostic']}".strip()
                return public
    except Exception as exc:
        diagnostic = str(exc)
        if proc is not None and proc.poll() is None:
            try:
                with open(os.devnull, "wb") as cleanup_log:
                    _stop_registered_probe(proc, cleanup_log)
            except Exception as cleanup_exc:
                diagnostic += f"\n检测进程清理失败: {cleanup_exc}"
        return _result(source=source, selection_path=selection["path"],
                       message="无法完成 Wind 检测", diagnostic=diagnostic)
    finally:
        if proc is not None:
            with _ACTIVE_LOCK:
                if proc.poll() is not None:
                    _ACTIVE_PROBES.pop(id(proc), None)


def _is_bundled_module(path: str | None) -> bool:
    if not path or not getattr(sys, "frozen", False):
        return False
    module_path = Path(path).resolve()
    mei = getattr(sys, "_MEIPASS", None)
    if mei and module_path.is_relative_to(Path(mei).resolve()):
        return True
    if sys.platform == "darwin":
        for parent in Path(sys.executable).resolve().parents:
            if parent.suffix == ".app" and module_path.is_relative_to(parent / "Contents"):
                return True
    return False


def _probe_in_child(*, connect: bool, connect_wait: int) -> dict:
    source = "session"
    selection_path = None
    try:
        from .wind_config import get_session_wind_selection

        selection = get_session_wind_selection()
        source, selection_path = selection["source"], selection["path"]
    except Exception as exc:
        return _result(stage="discovery", message="Wind 接口选择无效", diagnostic=str(exc),
                       source=source, selection_path=selection_path)
    try:
        from .data_providers.wind import load_windpy

        module = load_windpy()
        api = getattr(module, "w", None)
        if not all(callable(getattr(api, name, None)) for name in ("start", "isconnected")):
            raise ImportError("WindPy 未提供可调用的 w.start / w.isconnected 接口")
    except Exception as exc:
        missing = isinstance(exc, ModuleNotFoundError) and exc.name == "WindPy"
        return _result(stage="discovery" if missing else "load", source=source,
                       selection_path=selection_path,
                       message="未找到可用的 WindPy 接口" if missing else "WindPy 接口加载失败",
                       diagnostic=f"{type(exc).__name__}: {exc}")
    actual_path = str(Path(module.__file__).resolve()) if getattr(module, "__file__", None) else None
    metadata = {"path": actual_path, "source": source, "selection_path": selection_path,
                "bundled": _is_bundled_module(actual_path)}
    if not connect:
        return _result(ok=True, loaded=True, stage="load", message="WindPy 接口已加载，尚未连接终端",
                       **metadata)
    try:
        if module.w.isconnected():
            return _result(ok=True, loaded=True, stage="connection", connected=True,
                           message="Wind 终端已连接", **metadata)
        result = module.w.start(waitTime=max(1, min(15, connect_wait)))
        error_code = getattr(result, "ErrorCode", None)
        if error_code != 0:
            return _result(loaded=True, stage="connection", message="Wind 终端连接失败", **metadata,
                           connected=False,
                           diagnostic=f"ErrorCode={error_code}, Data={getattr(result, 'Data', '')}")
        connected = bool(module.w.isconnected())
        return _result(ok=connected, loaded=True, stage="connection", **metadata,
                       connected=connected, message="Wind 终端连接成功" if connected else "Wind 终端尚未连接")
    except Exception as exc:
        return _result(loaded=True, stage="connection", **metadata, connected=False,
                       message="Wind 终端连接失败", diagnostic=f"{type(exc).__name__}: {exc}")


def main(argv: list[str] | None = None) -> int:
    """源码 -m 与冻结 --wind-probe 共用的无 GUI 子进程入口。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--connect", action="store_true")
    parser.add_argument("--connect-wait", type=int, default=15)
    args = parser.parse_args(argv)
    from .cli._console import configure_utf8_stdio

    configure_utf8_stdio()
    report = _probe_in_child(connect=args.connect, connect_wait=args.connect_wait)
    report.update(schema_version=_SCHEMA_VERSION, token=args.token, pid=os.getpid(),
                  output=str(args.output.resolve()))
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
