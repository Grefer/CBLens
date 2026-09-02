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
