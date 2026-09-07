"""在真正没有控制台/标准句柄的 Windows 子进程中验收冻结桌面包。"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def _detached_options() -> dict:
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESTDHANDLES
    # NULL 是无效标准句柄。不能用 PIPE/DEVNULL：那会给被测 EXE 一套有效句柄。
    startup.hStdInput = startup.hStdOutput = startup.hStdError = 0
    return {
        "startupinfo": startup,
        "creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        "close_fds": True,
    }


def _run_detached(command: list[str], *, env: dict, timeout: float) -> tuple[int, int]:
    # 三个 stdio 参数均不传，保留上面显式指定的 NULL，且不继承任何父进程句柄。
    proc = subprocess.Popen(command, env=env, **_detached_options())
    try:
        return proc.pid, proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        # Windows onefile 有 bootloader 父进程及 Python 子进程；必须清理整棵树。
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, timeout=10, check=True)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)
        raise TimeoutError(f"无控制台桌面诊断超过 {timeout:g} 秒，进程树已终止") from None


def _validate_report(report: dict, manifest: dict, result_path: Path) -> None:
    if (report.get("schema_version") != 1 or report.get("ok") is not True
            or report.get("frozen") is not True or report.get("platform") != "win32"):
        raise ValueError("诊断未成功，或被测程序不是 Windows 冻结包")
    launcher = report.get("launcher") or {}
    if (type(report.get("pid")) is not int or report["pid"] <= 0
            or type(launcher.get("pid")) is not int or launcher["pid"] <= 0
            or report["pid"] == launcher["pid"] or launcher.get("exit_code") != 0):
        raise ValueError("诊断未来自已正常退出的 onefile 子进程")
    if report.get("output") != str(result_path.resolve()):
        raise ValueError("诊断报告不是本轮新建结果文件")
    if report.get("version") != manifest.get("version"):
        raise ValueError("诊断版本与构建清单不一致")
    identity = report.get("build") or {}
    for key in ("schema_version", "version", "commit", "source_ref", "release_tag", "platform", "architecture"):
        if key not in manifest or identity.get(key) != manifest[key]:
            raise ValueError(f"诊断构建身份不匹配: {key}")
    startup = report.get("startup_stdio") or {}
    for name in ("stdin", "stdout", "stderr"):
        if (startup.get("python_none") or {}).get(name) is not True:
            raise ValueError(f"未复现原始 Python {name}=None")
        handle = (startup.get("windows_handles") or {}).get(name) or {}
        if handle.get("kind") not in {"null", "invalid"} or handle.get("file_type") != 0:
            raise ValueError(f"启动时 {name} 仍有有效 Win32 句柄，不能证明无控制台场景")
    for name in ("stdout", "stderr"):
        stream = (report.get("streams") or {}).get(name) or {}
        if (stream.get("missing") is not False or stream.get("usable") is not True
                or str(stream.get("encoding")).lower().replace("-", "") != "utf8"):
            raise ValueError(f"stdio runtime hook 未提供可用的 UTF-8 {name}")
    probe = report.get("stdio_probe") or {}
    if probe.get("ok") is not True or probe.get("network_used") is not False:
        raise ValueError("离线进度条及主线程异常清理检查未通过")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, default=Path("dist/CBLens.exe"))
    parser.add_argument("--manifest", type=Path, default=Path("dist/CBLens-Windows-build.json"))
    parser.add_argument("--output", type=Path, default=Path("build/CBLens-Windows-diagnostics.json"))
    parser.add_argument("--timeout", type=float, default=90)
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        parser.error("这项烟测只能在 Windows 上运行")
    if not 0 < args.timeout <= 300:
        parser.error("--timeout 必须在 0 到 300 秒之间")
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="cblens-detached-check-") as temp:
        work = Path(temp)
        result_path = work / "diagnostics.json"  # 每次新目录，不能读到旧成功结果。
        env = {**os.environ, "CBLENS_DATA_DIR": str(work / "data"),
               "MPLCONFIGDIR": str(work / "mplconfig")}
        command = [str(args.exe.resolve()), "--diagnose", "--check", "--probe-stdio",
                   "--output", str(result_path)]
        pid, returncode = _run_detached(command, env=env, timeout=args.timeout)
        report = json.loads(result_path.read_text(encoding="utf-8"))
        report["launcher"] = {"pid": pid, "exit_code": returncode, "detached": True}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=True, indent=2))
        # 文件写完仍不算完成：先等 onefile bootloader 返回，并且检查真实退出码。
        if returncode != 0:
            raise RuntimeError(f"无控制台诊断进程失败: {returncode}")
        _validate_report(report, manifest, result_path)
    print(f"Windows detached desktop check passed: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
