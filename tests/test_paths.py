import sys
import json
from pathlib import Path

from convertible_bond import paths


def _fake_layout(monkeypatch, root: Path, *, with_pyproject: bool) -> Path:
    """把 ``paths.__file__`` 挪到 ``root/convertible_bond/paths.py``, 模拟一种安装形态。

    ``project_root()`` 读的就是模块的 ``__file__``, 所以这样能连它一起测到 ——
    直接 monkeypatch ``project_root`` 会把判据里最容易写错的那一半跳过去。
    """
    pkg = root / "convertible_bond"
    pkg.mkdir(parents=True, exist_ok=True)
    if with_pyproject:
        (root / "pyproject.toml").write_text('[project]\nname = "cblens"\n', encoding="utf-8")
    monkeypatch.delenv("CBLENS_DATA_DIR", raising=False)
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    monkeypatch.setattr(paths, "__file__", str(pkg / "paths.py"))
    return root


def test_wheel_install_never_writes_into_site_packages(monkeypatch, tmp_path, caplog):
    """非 editable 安装的数据目录必须离开 site-packages。

    实测 2026-09-03: ``pip wheel --no-deps .`` 产出的 92 个条目里 86 个是 ``.py`` (其余
    是 dist-info 元数据), ``data/`` 与 ``assets/`` **各 0 个文件**; 装进 venv 后 ``data_path('cb_data.json')`` 指向
    ``<venv>/lib/python3.13/site-packages/data/cb_data.json``, 而 ``data_path`` 会
    ``mkdir`` —— 实测跑一次 ``cb-screen-pool`` 就在 site-packages 里建出了 ``data/``,
    输出「总数: 0」而不说为什么。
    """
    site_packages = _fake_layout(monkeypatch, tmp_path / "site-packages", with_pyproject=False)
    monkeypatch.setattr(paths, "_warned_installed_layout", False)

    with caplog.at_level("WARNING", logger=paths.logger.name):
        target = paths.app_data_dir()

    assert site_packages not in target.parents, f"数据目录仍落在 site-packages 里: {target}"
    # 与桌面包共用的用户级目录: .../CBLens/data (三个平台分支同形)
    assert target.parts[-2:] == ("CBLens", "data"), target
    assert caplog.records, "回落到用户目录时必须出声 —— 空数据目录不能和程序坏了长得一样"


def test_source_checkout_still_uses_the_repo_data_dir(monkeypatch, tmp_path):
    """editable / 源码 checkout 的行为一个字节不能变 (判据: 根目录有 pyproject.toml)。

    实测 ``pip install -e .`` 之后 ``convertible_bond.__file__`` 仍指向源码树,
    所以这条同时守住 editable 安装。
    """
    root = _fake_layout(monkeypatch, tmp_path / "repo", with_pyproject=True)

    assert paths.app_data_dir() == root / "data"


def test_every_console_script_is_documented():
    """``[project.scripts]`` 注册的命令必须在用户文档里出现过。

    实测 2026-08-30 有三个命令零引用: ``cb-sync-terms`` / ``cb-backfill-delisted-cbs``
    / ``cb-backfill-down-reset-patches`` —— 注册了, 没文档, 没测试。前者更糟: 它把条款
    写进 ``TermsCache`` (``~/.cb_pricer_cache/terms/``), 而全仓**没有任何生产代码**构造
    过 TermsCache, 于是"同步成功 N 只"之后应用里什么都不会变; 而 ``cb-sync-tradable
    --codes`` 做的是同一件事且写进真正被读的 bundle。它已删除, 另两个补了文档。

    这条守的是"注册即承诺": 一个用户敲得出来、却没人写过怎么用的命令, 和一个坏掉的
    命令区分不开。
    """
    root = Path(__file__).resolve().parent.parent
    block = (root / "pyproject.toml").read_text(encoding="utf-8")
    block = block.split("[project.scripts]", 1)[1].split("\n[", 1)[0]
    names = [line.split("=", 1)[0].strip() for line in block.splitlines()
             if "=" in line and not line.strip().startswith("#")]

    # 防止 section 改名后这条静默通过 (那时 names 会是空的)
    assert "cb-gui" in names and "cb-sync-tradable" in names, names
    assert len(names) >= 15, names

    docs = "\n".join((root / p).read_text(encoding="utf-8")
                     for p in ("README.md", "docs/USAGE.md", "AGENTS.md"))
    missing = [n for n in names if n not in docs]
    assert not missing, f"注册了但任何文档里都没提过的命令: {missing}"


def test_source_data_path_defaults_to_repo_data(monkeypatch):
    monkeypatch.delenv("CBLENS_DATA_DIR", raising=False)
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)

    assert paths.data_path("cb_data.json").name == "cb_data.json"
    assert paths.data_path("cb_data.json").parent.name == "data"


def test_env_data_dir_override(monkeypatch, tmp_path):
    monkeypatch.setenv("CBLENS_DATA_DIR", str(tmp_path))

    assert paths.data_path("watchlist.json") == tmp_path / "watchlist.json"


def test_frozen_seeded_data_file(monkeypatch, tmp_path):
    bundled = tmp_path / "bundle"
    bundled_data = bundled / "data"
    user_data = tmp_path / "user"
    bundled_data.mkdir(parents=True)
    (bundled_data / "cb_events.json").write_text('{"events": []}', encoding="utf-8")

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundled), raising=False)
    monkeypatch.setenv("CBLENS_DATA_DIR", str(user_data))

    target = paths.data_path("cb_events.json", seed=True)

    assert target == user_data / "cb_events.json"
    assert target.read_text(encoding="utf-8") == '{"events": []}'


def test_frozen_seed_replaces_empty_cb_data(monkeypatch, tmp_path):
    bundled = tmp_path / "bundle"
    bundled_data = bundled / "data"
    user_data = tmp_path / "user"
    bundled_data.mkdir(parents=True)
    user_data.mkdir(parents=True)
    seed_payload = {
        "128009.SZ": {"sec_name": "测试转债"},
        "_bundle_meta": {"n_bonds": 1},
    }
    (bundled_data / "cb_data.json").write_text(
        json.dumps(seed_payload, ensure_ascii=False), encoding="utf-8")
    (user_data / "cb_data.json").write_text(
        json.dumps({"_bundle_meta": {"n_bonds": 0}}), encoding="utf-8")

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundled), raising=False)
    monkeypatch.setenv("CBLENS_DATA_DIR", str(user_data))

    target = paths.data_path("cb_data.json", seed=True)

    assert json.loads(target.read_text(encoding="utf-8")) == seed_payload


def test_frozen_seed_finds_onedir_internal_data(monkeypatch, tmp_path):
    exe_dir = tmp_path / "dist" / "CBLens"
    bundled_data = exe_dir / "_internal" / "data"
    user_data = tmp_path / "user"
    bundled_data.mkdir(parents=True)
    (bundled_data / "cb_events.json").write_text('{"events": []}', encoding="utf-8")

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "missing_meipass"), raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "CBLens"), raising=False)
    monkeypatch.setenv("CBLENS_DATA_DIR", str(user_data))

    target = paths.data_path("cb_events.json", seed=True)

    assert target.read_text(encoding="utf-8") == '{"events": []}'


def test_frozen_seed_replaces_empty_batch_cache(monkeypatch, tmp_path):
    bundled = tmp_path / "bundle"
    bundled_data = bundled / "data"
    user_data = tmp_path / "user"
    bundled_data.mkdir(parents=True)
    user_data.mkdir(parents=True)
    seed_payload = {
        "_meta": {"n_results": 1},
        "results": [{"bond_code": "128009.SZ", "status": "ok"}],
        "upcoming_results": [],
    }
    (bundled_data / "batch_pricing_cache.json").write_text(
        json.dumps(seed_payload, ensure_ascii=False), encoding="utf-8")
    (user_data / "batch_pricing_cache.json").write_text(
        json.dumps({"_meta": {"n_results": 0}, "results": []}), encoding="utf-8")

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundled), raising=False)
    monkeypatch.setenv("CBLENS_DATA_DIR", str(user_data))

    target = paths.data_path("batch_pricing_cache.json", seed=True)

    assert json.loads(target.read_text(encoding="utf-8")) == seed_payload


def test_frozen_seed_replaces_failed_only_batch_cache(monkeypatch, tmp_path):
    bundled = tmp_path / "bundle"
    bundled_data = bundled / "data"
    user_data = tmp_path / "user"
    bundled_data.mkdir(parents=True)
    user_data.mkdir(parents=True)
    seed_payload = {
        "_meta": {"n_results": 1},
        "results": [{"bond_code": "128009.SZ", "status": "ok"}],
        "upcoming_results": [],
    }
    failed_payload = {
        "_meta": {"n_results": 2, "summary": {"success": 0, "failed": 2}},
        "results": [
            {"bond_code": "110073.SH", "status": "未安装 WindPy"},
            {"bond_code": "110074.SH", "status": "未安装 WindPy"},
        ],
        "upcoming_results": [],
    }
    (bundled_data / "batch_pricing_cache.json").write_text(
        json.dumps(seed_payload, ensure_ascii=False), encoding="utf-8")
    (user_data / "batch_pricing_cache.json").write_text(
        json.dumps(failed_payload, ensure_ascii=False), encoding="utf-8")

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundled), raising=False)
    monkeypatch.setenv("CBLENS_DATA_DIR", str(user_data))

    target = paths.data_path("batch_pricing_cache.json", seed=True)

    assert json.loads(target.read_text(encoding="utf-8")) == seed_payload


def test_frozen_seed_falls_back_to_desktop_batch_cache_seed(monkeypatch, tmp_path):
    bundled = tmp_path / "bundle"
    bundled_data = bundled / "data"
    user_data = tmp_path / "user"
    bundled_data.mkdir(parents=True)
    seed_payload = {
        "_meta": {"n_results": 1},
        "results": [{"bond_code": "128009.SZ", "status": "ok"}],
        "upcoming_results": [],
    }
    (bundled_data / "desktop_batch_pricing_cache.json").write_text(
        json.dumps(seed_payload, ensure_ascii=False), encoding="utf-8")

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundled), raising=False)
    monkeypatch.setenv("CBLENS_DATA_DIR", str(user_data))

    target = paths.data_path("batch_pricing_cache.json", seed=True)

    assert target == user_data / "batch_pricing_cache.json"
    assert json.loads(target.read_text(encoding="utf-8")) == seed_payload


def test_asset_path_points_to_assets_dir():
    assert paths.asset_path("cblens-icon.png").parts[-2:] == ("assets", "cblens-icon.png")
