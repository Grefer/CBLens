import json
import importlib


def test_generate_spec_includes_tracked_desktop_cache_seed(tmp_path, monkeypatch):
    build_desktop = importlib.import_module("scripts.build_desktop")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    payload = {
        "_meta": {"n_results": 1},
        "results": [{"bond_code": "128009.SZ", "status": "ok"}],
        "upcoming_results": [],
    }
    (data_dir / "desktop_batch_pricing_cache.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    monkeypatch.setattr(build_desktop, "_detect_windpy", lambda: (False, None))

    spec = build_desktop._generate_spec(tmp_path)

    assert "desktop_batch_pricing_cache.json" in spec


def test_generate_spec_skips_unusable_runtime_batch_cache(tmp_path, monkeypatch):
    build_desktop = importlib.import_module("scripts.build_desktop")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "batch_pricing_cache.json").write_text(
        json.dumps({"results": [{"status": "未安装 WindPy"}]}, ensure_ascii=False),
        encoding="utf-8",
    )

    monkeypatch.setattr(build_desktop, "_detect_windpy", lambda: (False, None))

    spec = build_desktop._generate_spec(tmp_path)

    assert "batch_pricing_cache.json" not in spec


def test_frozen_build_can_actually_launch_the_pool_sync_clis():
    """「🌐 同步池」按**字符串模块名**起子进程, PyInstaller 静态分析看不见。

    实测从 ``gui.py`` 静态可达的 35 个模块里 ``convertible_bond.cli.*`` 一个都没有,
    所以冻结包里这四个模块根本不存在。而 ``sys.executable`` 在冻结包里是 app 本身,
    ``-m`` 会被 bootloader 连同模块名一起吃掉 —— 点一下菜单等于**又开一个 GUI**。

    两件事都要成立: 模块进了两份 spec 的 ``hiddenimports``, 且命令行按是否冻结分岔。
    """
    from pathlib import Path

    from convertible_bond.cli import POOL_SYNC_MODULES, RUN_CLI_FLAG

    root = Path(__file__).resolve().parent.parent
    for spec in ("CBLens.spec", "scripts/build_desktop.py"):
        text = (root / spec).read_text(encoding="utf-8")
        for module in POOL_SYNC_MODULES:
            assert module in text, f"{spec} 的 hiddenimports 里没有 {module}"

    # 菜单里用到的模块必须都在白名单里 —— 白名单是冻结入口唯一放行的那份
    from convertible_bond.gui.controllers.wind_sync import (
        _POOL_SYNC_TARGETS,
        pool_sync_command,
    )
    for _label, module, _args, _desc in _POOL_SYNC_TARGETS:
        assert module in POOL_SYNC_MODULES, f"{module} 不在白名单里, 冻结包会拒绝它"

    import convertible_bond.gui.controllers.wind_sync as ws

    src_cmd = pool_sync_command("convertible_bond.cli.sync_tradable", ["--incremental"])
    assert src_cmd[1:] == ["-u", "-m", "convertible_bond.cli.sync_tradable", "--incremental"]

    original = ws.is_frozen_app
    ws.is_frozen_app = lambda: True
    try:
        frozen_cmd = pool_sync_command("convertible_bond.cli.sync_tradable", ["--incremental"])
    finally:
        ws.is_frozen_app = original
    assert frozen_cmd[1:] == [RUN_CLI_FLAG, "convertible_bond.cli.sync_tradable",
                              "--incremental"], f"冻结包仍在用 -m: {frozen_cmd}"

    # gui.py 必须真的分派这个开关, 且只放行白名单
    gui_src = (root / "gui.py").read_text(encoding="utf-8")
    assert RUN_CLI_FLAG in gui_src
    assert "POOL_SYNC_MODULES" in gui_src, "冻结入口没有校验白名单"


def test_the_three_desktop_data_file_lists_agree():
    """"桌面包该带哪些数据文件"只许有一份判据。

    它曾散成三处各写各的: ``paths._SEEDED_DATA_FILES`` (运行时会去 seed 的)、
    ``scripts/build_desktop.STATIC_DATA_FILES`` (构建真正打进包的)、
    ``desktop_diagnostics._DATA_FILES`` (诊断会报告的)。实测最后一份漏了
    ``cb_valuation_history.json`` —— 那正好是**唯一**一个进版本库、只追加、丢了就
    永久丢的数据文件, 而诊断页恰恰是用户唯一能看出"包里到底有没有它"的地方。
    漏报的表现不是报错, 是那一行压根不出现。

    构建那份带着 ``desktop_`` 前缀的别名源文件 (种子与运行态缓存不同名), 所以判据是
    **别名归一之后**每个要 seed 的文件都真的被打进包。
    """
    build_desktop = importlib.import_module("scripts.build_desktop")
    from convertible_bond.desktop_diagnostics import _DATA_FILES
    from convertible_bond.paths import _BUNDLED_DATA_ALIASES, _SEEDED_DATA_FILES

    assert set(_DATA_FILES) == set(_SEEDED_DATA_FILES), "诊断报告的清单与运行时 seed 的清单分叉了"

    shipped = set(build_desktop.STATIC_DATA_FILES)
    for filename in _SEEDED_DATA_FILES:
        candidates = set(_BUNDLED_DATA_ALIASES.get(filename, (filename,)))
        assert candidates & shipped, (
            f"{filename} 会在运行时被 seed, 但构建脚本一个候选源都不打进包 —— "
            f"seed 时静默找不到文件")
