"""发布边界：源码/tag/版本必须一致，产物不可冒用或静默覆盖。"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts import build_desktop as build
from scripts import release_macos_desktop as release


def _git(root, *args):
    return subprocess.check_output([
        "git", "-c", "user.name=CBLens Test", "-c", "user.email=test@localhost",
        "-c", "commit.gpgsign=false", *args,
    ], cwd=root, text=True).strip()


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "convertible_bond").mkdir()
    (root / "convertible_bond" / "_version.py").write_text('__version__ = "2.0.0rc1"\n', encoding="utf-8")
    (root / ".gitignore").write_text("build/\ndist/\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "test source")
    _git(root, "tag", "v2.0.0-rc.1")
    return root


def test_release_identity_binds_an_annotated_tag_to_the_actual_commit(checkout):
    _git(checkout, "tag", "-d", "v2.0.0-rc.1")
    _git(checkout, "tag", "-a", "v2.0.0-rc.1", "-m", "candidate")
    identity = build.build_identity(checkout, "refs/tags/v2.0.0-rc.1", release_tag="v2.0.0-rc.1")
    assert identity["commit"] == _git(checkout, "rev-parse", "HEAD")
    assert identity["commit"] != _git(checkout, "rev-parse", "refs/tags/v2.0.0-rc.1")
    assert identity["version"] == "2.0.0rc1"
    assert identity["release_tag"] == "v2.0.0-rc.1"


@pytest.mark.parametrize("untracked", [False, True])
def test_dirty_or_untracked_source_is_rejected_before_build(checkout, untracked):
    path = checkout / ("untracked.py" if untracked else "convertible_bond/_version.py")
    path.write_text("changed\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="干净工作区"):
        build.build_identity(checkout, "HEAD")


def test_ref_must_match_head_and_release_tag_must_match_version(checkout):
    _git(checkout, "tag", "v1.0.0")
    with pytest.raises(SystemExit, match="只能发布为 v2.0.0-rc.1"):
        build.build_identity(checkout, "refs/tags/v1.0.0", release_tag="v1.0.0")
    (checkout / "new.py").write_text("# next commit\n", encoding="utf-8")
    _git(checkout, "add", ".")
    _git(checkout, "commit", "-qm", "next source")
    with pytest.raises(SystemExit, match="当前 HEAD"):
        build.build_identity(checkout, "refs/tags/v2.0.0-rc.1")


def test_release_has_no_default_tag_or_skip_build_escape():
    with pytest.raises(SystemExit):
        release._parser().parse_args([])
    with pytest.raises(SystemExit):
        release._parser().parse_args(["--tag", "v2.0.0-rc.1", "--skip-build"])


def _artifact_paths(monkeypatch, root):
    dist = root / "dist"
    app = dist / "CBLens.app"
    app.mkdir(parents=True)
    monkeypatch.setattr(release, "ROOT", root)
    monkeypatch.setattr(release, "APP_PATH", app)
    monkeypatch.setattr(release, "ZIP_PATH", dist / "CBLens-macOS.zip")
    monkeypatch.setattr(release, "MANIFEST_PATH", dist / "CBLens-macOS-build.json")


def _write_artifact(identity):
    release.ZIP_PATH.write_bytes(b"candidate zip")
    release.MANIFEST_PATH.write_text(json.dumps(dict(
        identity, artifact=release.ZIP_PATH.name,
        artifact_sha256=build.artifact_sha256(release.ZIP_PATH),
    )), encoding="utf-8")


def test_artifact_manifest_rejects_wrong_commit_and_tampered_zip(checkout, monkeypatch):
    _artifact_paths(monkeypatch, checkout)
    identity = build.build_identity(checkout, "HEAD")
    _write_artifact(identity)
    release._verify_artifact(identity)
    with pytest.raises(SystemExit, match="产物身份"):
        release._verify_artifact(dict(identity, commit="0" * 40))
    release.ZIP_PATH.write_bytes(b"different zip")
    with pytest.raises(SystemExit, match="zip 摘要"):
        release._verify_artifact(identity)


def test_skip_upload_rebuilds_and_checks_in_a_disposable_user_directory(checkout, monkeypatch):
    monkeypatch.setattr(release.sys, "platform", "darwin")
    _artifact_paths(monkeypatch, checkout)
    calls = []

    def run(cmd, *, env=None):
        calls.append(cmd)
        if str(cmd[1]) == "scripts/build_desktop.py":
            assert cmd[-1] == "refs/tags/v2.0.0-rc.1"
            _write_artifact(build.build_identity(checkout, cmd[-1], release_tag="v2.0.0-rc.1"))
        else:
            assert cmd[1:] == ["--diagnose", "--check", "--require-windpy"]
            assert Path(env["CBLENS_DATA_DIR"]).parent.name.startswith("cblens-release-check-")

    monkeypatch.setattr(release, "_run", run)
    assert release.main(["--tag", "v2.0.0-rc.1", "--skip-upload"]) == 0
    assert len(calls) == 2
    assert all(cmd[0] != "gh" for cmd in calls)


def test_existing_release_asset_is_rejected_without_clobber(monkeypatch):
    def existing(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 0, json.dumps({"assets": [{"name": "CBLens-macOS.zip"}]}))
    monkeypatch.setattr(release.subprocess, "run", existing)
    with pytest.raises(SystemExit, match="拒绝覆盖"):
        release._check_assets_absent("v2.0.0-rc.1")


@pytest.mark.parametrize("http_status,allowed", [(404, True), (403, False), (503, False)])
def test_windows_upload_only_treats_confirmed_404_as_an_absent_release(monkeypatch, http_status, allowed):
    def missing(cmd, **kwargs):
        output = f"HTTP/2.0 {http_status} error\n\n{{}}" if cmd[1] == "api" else ""
        return subprocess.CompletedProcess(cmd, 1, output, "request failed")

    monkeypatch.setattr(release.subprocess, "run", missing)
    if allowed:
        release._check_assets_absent("v2.0.0-rc.1", allow_missing_release=True)
    else:
        with pytest.raises(subprocess.CalledProcessError):
            release._check_assets_absent("v2.0.0-rc.1", allow_missing_release=True)


def test_remote_annotated_tag_must_match_the_local_artifact_commit(monkeypatch):
    calls = []

    def remote(cmd, **kwargs):
        calls.append(cmd)
        obj = {"type": "tag", "sha": "annotated-tag"} if "/git/ref/tags/" in cmd[-1] else {"type": "commit", "sha": "actual-commit"}
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"object": obj}))

    monkeypatch.setattr(release.subprocess, "run", remote)
    release._check_remote_tag("v2.0.0-rc.1", "actual-commit")
    assert len(calls) == 2
    with pytest.raises(SystemExit, match="GitHub.*commit 不一致"):
        release._check_remote_tag("v2.0.0-rc.1", "moved-local-tag-commit")


@pytest.fixture
def diagnostic_environment(monkeypatch, tmp_path):
    from convertible_bond import desktop_diagnostics as diagnostics
    from convertible_bond._version import __version__

    (tmp_path / "desktop_build.json").write_text(
        json.dumps({"version": __version__, "commit": "a" * 40}), encoding="utf-8")
    monkeypatch.setattr(diagnostics, "prepare_windpy_import_path", lambda: [])
    monkeypatch.setattr(diagnostics, "project_root", lambda: tmp_path)
    monkeypatch.setattr(diagnostics, "app_data_dir", lambda: tmp_path)
    monkeypatch.setattr(diagnostics, "seed_data_files", lambda: [])
    monkeypatch.setattr(diagnostics, "_DATA_FILES", ())
    monkeypatch.setattr(diagnostics, "bundled_data_path", lambda filename: None)
    monkeypatch.setattr(diagnostics, "_import_status", lambda name: "imported (fake)")
    return diagnostics


def test_strict_diagnostics_fail_for_missing_patches_without_connecting(monkeypatch, diagnostic_environment, capsys):
    diagnostics = diagnostic_environment
    monkeypatch.setattr(diagnostics, "_DATA_FILES", ("cb_terms_patches.json",))
    assert diagnostics.main(["--diagnose", "--check"]) == 1
    assert "包内种子缺失: cb_terms_patches.json" in capsys.readouterr().err


@pytest.mark.parametrize("name", ["stdout", "stderr"])
@pytest.mark.parametrize("failure", ["missing", "write", "flush"])
def test_strict_diagnostics_reject_unusable_stream_without_replacing_it(monkeypatch, diagnostic_environment, name, failure):
    diagnostics = diagnostic_environment

    class BrokenStream:
        def write(self, text):
            if failure == "write":
                raise OSError("输出失败")

        def flush(self):
            if failure == "flush":
                raise OSError("刷新失败")

    stream = None if failure == "missing" else BrokenStream()
    with monkeypatch.context() as patch:
        patch.setattr(diagnostics.sys, name, stream)
        assert diagnostics.main(["--diagnose", "--check"]) == 1
        assert getattr(diagnostics.sys, name) is stream


def test_strict_diagnostics_accept_writable_streams_and_keep_them(monkeypatch, diagnostic_environment):
    import io

    diagnostics = diagnostic_environment
    stdout, stderr = io.StringIO(), io.StringIO()
    with monkeypatch.context() as patch:
        patch.setattr(diagnostics.sys, "stdout", stdout)
        patch.setattr(diagnostics.sys, "stderr", stderr)
        assert diagnostics.main(["--diagnose", "--check"]) == 0
        assert diagnostics.sys.stdout is stdout and diagnostics.sys.stderr is stderr
    assert "CBLens desktop diagnostics" in stdout.getvalue()
    assert stderr.getvalue() == ""
