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


def test_stale_desktop_seed_is_reported_not_silently_shipped(tmp_path, capsys):
    """陈旧的种子缓存照打, 但**不许悄无声息**。

    两道闸 (`build_desktop._is_usable_batch_cache` 与 `paths._needs_seed`) 判的都只是
    "有没有一行 status=='ok'" —— 一份 117 天前的截面照样满足, 而发版时没有任何东西
    提醒该刷一次种子。桌面用户首启看到的就是几个月前的理论价与偏差, 唯一的线索是
    摘要条那个「估值日」。

    判据**不是**"陈旧就不打" —— "首启空表 vs 首启是旧数"这个取舍已经做过, 结论是宁可
    旧也别空 (见 `paths._BUNDLED_DATA_ALIASES` 上的注释)。这里钉的是那个决定的前提:
    有人知道它旧。
    """
    import json as _json
    from datetime import date, timedelta

    build_desktop = importlib.import_module("scripts.build_desktop")

    def _seed(days_old):
        path = tmp_path / f"seed_{days_old}.json"
        path.write_text(_json.dumps({
            "_meta": {"saved_at": (date.today() - timedelta(days=days_old)).isoformat() + "T00:00:00"},
            "results": [{"bond_code": "128009.SZ", "status": "ok"}],
        }), encoding="utf-8")
        return path

    fresh, stale = _seed(3), _seed(build_desktop.SEED_STALE_WARN_DAYS + 30)

    # 两份都"可用" —— 陈旧不改变这个判据
    assert build_desktop._is_usable_batch_cache(fresh)
    assert build_desktop._is_usable_batch_cache(stale)

    assert build_desktop._seed_age_days(fresh) == 3
    assert build_desktop._seed_age_days(stale) == build_desktop.SEED_STALE_WARN_DAYS + 30

    # 没有 saved_at 戳时返回 None (而不是 0 —— 0 会被读成"今天刚存的")
    no_stamp = tmp_path / "no_stamp.json"
    no_stamp.write_text(_json.dumps({"results": [{"status": "ok"}]}), encoding="utf-8")
    assert build_desktop._seed_age_days(no_stamp) is None


def test_diagnostics_recognises_the_seed_file_by_its_own_name():
    """诊断对种子缓存要给出和运行态缓存一样详细的摘要, 并报出年龄。

    种子文件叫 ``desktop_batch_pricing_cache.json`` (``_BUNDLED_DATA_ALIASES`` 里的
    别名源), 而那个详细分支此前写死 ``path.name == "batch_pricing_cache.json"``
    —— 于是**恰恰是打进包里的那一份**掉进最后的通用分支, 只报个 ``dict keys=3``。
    """
    import json as _json
    import tempfile
    from datetime import date, timedelta
    from pathlib import Path as _Path

    from convertible_bond.desktop_diagnostics import _json_summary

    payload = {
        "_meta": {"saved_at": (date.today() - timedelta(days=42)).isoformat() + "T09:00:00"},
        "results": [{"bond_code": "128009.SZ", "status": "ok"},
                    {"bond_code": "113029.SH", "status": "boom"}],
    }
    with tempfile.TemporaryDirectory() as d:
        for name in ("batch_pricing_cache.json", "desktop_batch_pricing_cache.json"):
            path = _Path(d) / name
            path.write_text(_json.dumps(payload), encoding="utf-8")
            summary = _json_summary(path)
            assert "2 rows, 1 ok" in summary, f"{name}: {summary}"
            assert "42 days ago" in summary, f"{name} 没报年龄: {summary}"
