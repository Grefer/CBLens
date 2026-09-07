"""策略快照跨源码、桌面包和自定义数据目录的持久化验证。"""
from datetime import date
import sys

import pytest

from convertible_bond import paths
from convertible_bond.gui.controllers import strategy_snapshots
from convertible_bond.gui.controllers.strategy_snapshots import StrategySnapshotMixin


@pytest.mark.parametrize("frozen,override", [(False, False), (True, False),
                                             (False, True), (True, True)])
def test_strategy_snapshots_save_and_load_in_runtime_data_dir(
    tmp_path, monkeypatch, frozen, override,
):
    """桌面升级替换安装目录后，结果仍应从用户目录读回；环境变量同样覆盖快照。"""
    install = tmp_path / "install"
    install.mkdir()
    (install / "pyproject.toml").write_text('[project]\nname = "cblens"\n')
    monkeypatch.setattr(paths, "__file__", str(install / "convertible_bond" / "paths.py"))
    monkeypatch.setattr(
        strategy_snapshots, "__file__",
        str(install / "convertible_bond" / "gui" / "controllers" / "strategy_snapshots.py"),
    )
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(install), raising=False)
    user_data = tmp_path / "user_data"
    monkeypatch.setattr(paths, "_user_data_dir", lambda: user_data)
    if override:
        expected = tmp_path / "custom_data"
        monkeypatch.setenv("CBLENS_DATA_DIR", str(expected))
    else:
        monkeypatch.delenv("CBLENS_DATA_DIR", raising=False)
        expected = user_data if frozen else install / "data"

    class SnapshotApp(StrategySnapshotMixin):
        def _record_strategy_comparison_result(self, result):
            self.loaded = result

        def _mark_strategy_tabs_dirty(self):
            pass

    writer = SnapshotApp()
    writer._last_strategy_bt_result = {
        "config": {"rank_signal": "deviation", "top_n": 10},
        "start_date": date(2025, 1, 1),
        "summary": {"final_equity": 1.12},
    }
    saved = writer._save_strategy_backtest_snapshot()
    assert saved["path"].parent == expected / "strategy_backtest_snapshots"
    assert saved["latest_path"] == expected / "strategy_backtest_snapshot.json"
    assert saved["path"].read_bytes() == saved["latest_path"].read_bytes()

    reader = SnapshotApp()
    reader._load_strategy_backtest_snapshot(silent=True, render=False)
    assert reader.loaded["start_date"] == date(2025, 1, 1)
    assert reader.loaded["summary"]["final_equity"] == 1.12
    if frozen or override:
        assert not (install / "data").exists(), "快照不应写进安装目录"
