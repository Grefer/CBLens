#!/usr/bin/env python3
"""从版本匹配的 tag 构建、隔离诊断并上传 macOS 桌面包；不覆盖已有资产。"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

if __package__:
    from .build_desktop import artifact_sha256, build_identity
else:
    from build_desktop import artifact_sha256, build_identity


ROOT = Path(__file__).resolve().parents[1]
APP_PATH = ROOT / "dist" / "CBLens.app"
ZIP_PATH = ROOT / "dist" / "CBLens-macOS.zip"
MANIFEST_PATH = ROOT / "dist" / "CBLens-macOS-build.json"


def _run(cmd: Sequence[str | Path], *, env: dict | None = None) -> None:
    print("+ " + " ".join(shlex.quote(str(part)) for part in cmd))
    subprocess.run([str(part) for part in cmd], cwd=ROOT, check=True, env=env)


def _verify_artifact(identity: dict) -> None:
    """zip 及旁置清单必须来自刚才验证的源码身份。"""
    try:
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"构建身份清单不可读: {exc}") from exc
    if any(manifest.get(key) != value for key, value in identity.items()):
        raise SystemExit("产物身份与当前 tag/commit/版本不一致，拒绝上传")
    if manifest.get("artifact") != ZIP_PATH.name or not ZIP_PATH.is_file():
        raise SystemExit("构建清单未指向预期的 macOS zip")
    if manifest.get("artifact_sha256") != artifact_sha256(ZIP_PATH):
        raise SystemExit("zip 摘要与构建清单不一致，拒绝上传")


def _check_assets_absent(tag: str, *, names: set[str] | None = None,
                        allow_missing_release: bool = False) -> None:
    result = subprocess.run(
        ["gh", "release", "view", tag, "--json", "assets"],
        cwd=ROOT, capture_output=True, text=True,
    )
    if result.returncode and allow_missing_release:
        # 新 tag 尚无 Release 是 Windows 工作流的正常起点。只有明确 HTTP 404
        # 才允许 action 创建；网络/权限错误不能冒充“没有旧资产”。
        status = subprocess.run(
            ["gh", "api", "--include", f"repos/{{owner}}/{{repo}}/releases/tags/{tag}"],
            cwd=ROOT, capture_output=True, text=True,
        )
        if status.returncode and re.search(r"^HTTP/[\d.]+ 404\b", status.stdout, re.MULTILINE):
            return
    result.check_returncode()
    existing = {asset["name"] for asset in json.loads(result.stdout)["assets"]}
    conflicts = existing & (names if names is not None else {ZIP_PATH.name, MANIFEST_PATH.name})
    if conflicts:
        raise SystemExit(f"Release 已有同名资产，拒绝覆盖: {', '.join(sorted(conflicts))}")


def _check_remote_tag(tag: str, commit: str) -> None:
    """本地 tag 可能被移动过；上传目标仓库的 tag 也必须指向同一个 commit。"""
    endpoint = f"repos/{{owner}}/{{repo}}/git/ref/tags/{tag}"
    for _ in range(10):
        result = subprocess.run(
            ["gh", "api", endpoint], cwd=ROOT,
            capture_output=True, text=True, check=True,
        )
        obj = json.loads(result.stdout)["object"]
        if obj["type"] == "commit":
            if obj["sha"] != commit:
                raise SystemExit(f"GitHub 上的 {tag} 与本地产物 commit 不一致，拒绝上传")
            return
        if obj["type"] != "tag":
            break
        endpoint = f"repos/{{owner}}/{{repo}}/git/tags/{obj['sha']}"
    raise SystemExit(f"无法将 GitHub tag {tag} 解析到 commit")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True, help="已存在且与包版本匹配的 tag，例如 v2.0.0-rc.1")
    parser.add_argument("--skip-upload", action="store_true", help="只构建、诊断和打包，不联系 GitHub")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if sys.platform != "darwin":
        raise SystemExit("This helper must be run on macOS.")
    tag = args.tag.strip()
    identity = build_identity(ROOT, f"refs/tags/{tag}", release_tag=tag)
    # 每次重新构建：不再接受身份含糊的 --skip-build 或跳过诊断。
    _run([sys.executable, "scripts/build_desktop.py", "--ref", f"refs/tags/{tag}"])
    if not APP_PATH.is_dir():
        raise SystemExit(f"Missing app bundle: {APP_PATH}")
    with tempfile.TemporaryDirectory(prefix="cblens-release-check-") as tmp:
        env = os.environ.copy()
        env["CBLENS_DATA_DIR"] = str(Path(tmp) / "data")
        env["CBLENS_CONFIG_DIR"] = str(Path(tmp) / "config")
        env["MPLCONFIGDIR"] = str(Path(tmp) / "mplconfig")
        _run([APP_PATH / "Contents" / "MacOS" / "CBLens", "--diagnose", "--check", "--require-windpy"], env=env)
    if build_identity(ROOT, f"refs/tags/{tag}", release_tag=tag) != identity:
        raise SystemExit("构建期间源码身份发生变化，拒绝发布")
    _verify_artifact(identity)
    if not args.skip_upload:
        _check_remote_tag(tag, identity["commit"])
        _check_assets_absent(tag)
        _run(["gh", "release", "upload", tag, ZIP_PATH, MANIFEST_PATH])
        print(f"[release] uploaded macOS zip and build manifest to {tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
