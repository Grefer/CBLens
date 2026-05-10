#!/usr/bin/env python3
"""Build and upload the macOS CBLens release asset from a local WindPy machine."""
from __future__ import annotations

import argparse
import hashlib
import shlex
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_PATH = ROOT / "dist" / "CBLens.app"
ZIP_PATH = ROOT / "dist" / "CBLens-macOS.zip"


def _run(cmd: Sequence[str | Path]) -> None:
    printable = " ".join(shlex.quote(str(part)) for part in cmd)
    print(f"+ {printable}")
    subprocess.run([str(part) for part in cmd], cwd=ROOT, check=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build CBLens.app locally, package CBLens-macOS.zip, and upload it "
            "to a GitHub Release. Run this on a machine whose Wind API can be "
            "imported so the release asset keeps bundled WindPy support."
        )
    )
    parser.add_argument(
        "--tag",
        default="v1.0.0",
        help="GitHub Release tag to upload to (default: v1.0.0).",
    )
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="Reuse the existing dist/CBLens.app instead of rebuilding it.",
    )
    parser.add_argument(
        "--skip-diagnose",
        action="store_true",
        help="Do not run CBLens.app --diagnose before packaging.",
    )
    parser.add_argument(
        "--skip-upload",
        action="store_true",
        help="Package and print the digest without uploading to GitHub.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    tag = args.tag.strip()
    if not tag:
        raise SystemExit("--tag cannot be empty")

    if sys.platform != "darwin":
        raise SystemExit("This helper must be run on macOS.")

    if not args.skip_build:
        _run([sys.executable, "scripts/build_desktop.py"])

    if not APP_PATH.exists():
        raise SystemExit(f"Missing app bundle: {APP_PATH}")

    if not args.skip_diagnose:
        _run([APP_PATH / "Contents" / "MacOS" / "CBLens", "--diagnose"])

    _run(["ditto", "-c", "-k", "--norsrc", "--keepParent", APP_PATH, ZIP_PATH])
    digest = _sha256(ZIP_PATH)
    print(f"[release] {ZIP_PATH} sha256:{digest}")

    if not args.skip_upload:
        _run(["gh", "release", "upload", tag, ZIP_PATH, "--clobber"])
        print(f"[release] uploaded CBLens-macOS.zip to {tag}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
