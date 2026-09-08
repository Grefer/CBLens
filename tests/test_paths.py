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


def test_upgrade_seeds_missing_patches_and_keeps_existing_user_data(monkeypatch, tmp_path):
    """旧安装补回历史 K / 评级依据，同时保留用户已有的条款和关注池。"""
    bundled = tmp_path / "bundle"
    bundled_data = bundled / "data"
    user_data = tmp_path / "user"
    bundled_data.mkdir(parents=True)
    user_data.mkdir()
    patch_seed = {"patches": [{"bond_code": "123064.SZ", "fields": {"conversion_price": 26.6}}]}
    (bundled_data / "cb_terms_patches.json").write_text(json.dumps(patch_seed), encoding="utf-8")
    (bundled_data / "cb_data.json").write_text('{"new": {"conversion_price": 99}}', encoding="utf-8")
    existing = {
        "cb_data.json": '{"old": {"conversion_price": 20}}',
        "watchlist.json": '{"entries": [{"bond_code": "old"}]}',
    }
    for filename, content in existing.items():
        (user_data / filename).write_text(content, encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundled), raising=False)
    monkeypatch.setenv("CBLENS_DATA_DIR", str(user_data))
    paths.seed_data_files()
    assert json.loads((user_data / "cb_terms_patches.json").read_text()) == patch_seed
    for filename, content in existing.items():
        assert (user_data / filename).read_text() == content
    # 此后用户同步/修复过 patch，再开新版也不能用内置种子覆盖它。
    for local_patches in ('{"patches": [], "_meta": {"note": "user repaired"}}', '{}', '{broken'):
        (user_data / "cb_terms_patches.json").write_text(local_patches, encoding="utf-8")
        paths.seed_data_files()
        assert (user_data / "cb_terms_patches.json").read_text() == local_patches


# ── 桌面包的持久字体缓存 ────────────────────────────────────────
# 背景 (实测 2026-09-07, Python 3.13.1 / matplotlib 3.10.8): PyInstaller 的标准钩子
# ``pyi_rth_mplconfig`` 每次启动 ``secure_mkdtemp()`` 一个新的 ``MPLCONFIGDIR``, 退出即删,
# 于是 ``fontlist-vNNN.json`` 每次重建 —— 冷 **8.23s** / 热 **0.005s**。GUI 在建 Tk 窗口
# **之前**就经 ``controllers.backtest → pyplot`` 走到那一步, 8 秒整个落在首窗等待上。
# 钩子当年的理由 (缓存指向上一个已删的 ``_MEIxxxxx``) 在现代 matplotlib 上已不成立:
# 随包字体存的是相对 ``mpl.get_data_path()`` 的路径, 载入时再拼当前路径 —— 实测连开三次
# 各换一个 ``_MEIPASS``, 38 个随包字体全部命中当次路径, ``missing_files=0``。


def _fake_frozen(monkeypatch, tmp_path: Path) -> None:
    """冒充 PyInstaller onefile: frozen + _MEIPASS, 且 MPLCONFIGDIR 已被标准钩子占住。

    ``sys.modules`` 里的 matplotlib 也要摘掉: 入口脚本跑到这一步时它还没被导入, 而
    **同一次 pytest 里**别的用例可能早就导入过 —— 不摘就成了跟测试顺序有关的红/绿
    (实测单跑绿、全量跑红, 而全量那次红得对: 函数确实拒绝了一个无效的设置)。
    """
    monkeypatch.delitem(sys.modules, "matplotlib", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "_MEIxxxx"), raising=False)
    monkeypatch.setenv(paths.MPLCONFIGDIR_ENV, str(tmp_path / "rthook-temp"))
    monkeypatch.setenv(paths.MPL_CACHE_DIR_ENV, str(tmp_path / "cache" / "matplotlib-test"))


def test_source_checkout_keeps_its_own_matplotlib_cache(monkeypatch, tmp_path):
    """源码 checkout 不改 ``MPLCONFIGDIR`` —— ``~/.matplotlib`` 本来就是持久的。

    插一手只会多出第二份缓存, 并让开发机与用户机的字体解析路径不一致。
    """
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    monkeypatch.delenv(paths.MPLCONFIGDIR_ENV, raising=False)

    assert paths.use_persistent_matplotlib_cache() is None
    assert paths.MPLCONFIGDIR_ENV not in __import__("os").environ


def test_frozen_app_overrides_the_rthooks_throwaway_dir(monkeypatch, tmp_path):
    """冻结包里必须**盖掉**钩子设的临时目录 —— setdefault 语义会让这个修复整个失效。"""
    import os

    _fake_frozen(monkeypatch, tmp_path)
    throwaway = os.environ[paths.MPLCONFIGDIR_ENV]

    target = paths.use_persistent_matplotlib_cache()

    assert target is not None, "冻结包里没有生效"
    assert os.environ[paths.MPLCONFIGDIR_ENV] == str(target) != throwaway
    assert target.is_dir(), "目录要真的建出来, 否则 matplotlib 自己会再回落一次"


def test_cache_dir_is_keyed_by_app_version(monkeypatch):
    """按版本分目录: 升级后包里带哪些字体没有任何东西盯着, 版本号是唯一可靠的键。"""
    monkeypatch.delenv(paths.MPL_CACHE_DIR_ENV, raising=False)

    assert paths.matplotlib_cache_dir().name == f"matplotlib-{paths.__version__}"


def test_already_imported_matplotlib_is_reported_not_swallowed(monkeypatch, tmp_path, caplog):
    """导入后再设是**无效**的 (配置目录已 memo) —— 静默返回等于"改完还是慢 8 秒"。"""
    import os

    _fake_frozen(monkeypatch, tmp_path)
    monkeypatch.setitem(sys.modules, "matplotlib", object())
    before = os.environ[paths.MPLCONFIGDIR_ENV]

    with caplog.at_level("WARNING", logger=paths.logger.name):
        assert paths.use_persistent_matplotlib_cache() is None

    assert os.environ[paths.MPLCONFIGDIR_ENV] == before, "无效时不许假装设置成功"
    assert "matplotlib" in caplog.text


def test_unwritable_cache_dir_degrades_to_the_rthook_temp_dir(monkeypatch, tmp_path):
    """缓存目录建不出来时保持现状 (慢, 但能用), 不能让启动崩在一个性能优化上。"""
    import os

    _fake_frozen(monkeypatch, tmp_path)
    blocker = tmp_path / "blocked"
    blocker.write_text("我是文件不是目录", encoding="utf-8")
    monkeypatch.setenv(paths.MPL_CACHE_DIR_ENV, str(blocker / "matplotlib-x"))
    before = os.environ[paths.MPLCONFIGDIR_ENV]

    assert paths.use_persistent_matplotlib_cache() is None
    assert os.environ[paths.MPLCONFIGDIR_ENV] == before


def test_pruning_only_removes_our_own_stale_version_dirs(monkeypatch, tmp_path):
    """清理旧版本缓存只许碰 ``_user_cache_dir()`` 下自己建的 ``matplotlib-*`` 目录。"""
    cache_root = tmp_path / "cache"
    monkeypatch.setattr(paths, "_user_cache_dir", lambda: cache_root)
    _fake_frozen(monkeypatch, tmp_path)
    monkeypatch.delenv(paths.MPL_CACHE_DIR_ENV, raising=False)
    stale = cache_root / "matplotlib-1.0.0"
    stale.mkdir(parents=True)
    (stale / "fontlist-v390.json").write_text("{}", encoding="utf-8")
    innocent_dir = cache_root / "announcement_text"
    innocent_dir.mkdir()
    innocent_file = cache_root / "matplotlib-not-a-dir.txt"
    innocent_file.write_text("x", encoding="utf-8")

    target = paths.use_persistent_matplotlib_cache()

    assert target == cache_root / f"matplotlib-{paths.__version__}"
    assert not stale.exists(), "旧版本目录没清掉, 每升一次级留一份"
    assert innocent_dir.is_dir() and innocent_file.is_file(), "清理越界了"


def test_pruning_stays_away_from_an_explicit_override_location(monkeypatch, tmp_path):
    """``CBLENS_MPLCONFIGDIR`` 指到别处时不清理: 那不是我们建的目录, 邻居也不是我们的。"""
    _fake_frozen(monkeypatch, tmp_path)
    elsewhere = tmp_path / "elsewhere"
    neighbour = elsewhere / "matplotlib-someone-elses"
    neighbour.mkdir(parents=True)
    monkeypatch.setenv(paths.MPL_CACHE_DIR_ENV, str(elsewhere / "matplotlib-mine"))

    paths.use_persistent_matplotlib_cache()

    assert neighbour.is_dir(), "override 路径下的邻居目录被删了"
