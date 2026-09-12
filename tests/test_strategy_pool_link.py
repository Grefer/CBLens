"""批量视图到策略代码池的联动，使用真实筛选与预检且不创建 Tk 窗口。"""
from copy import deepcopy
from datetime import date
from types import SimpleNamespace

import pytest

from convertible_bond.gui.constants import (
    STRATEGY_POOL_MODES,
    strategy_pool_from_label,
    strategy_pool_label,
)
from convertible_bond.gui.controllers.strategy_setup import StrategySetupMixin
from convertible_bond.gui.tabs import batch


class Var:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class App(StrategySetupMixin):
    def __init__(self, mode="当前筛选结果"):
        self.v_st_pool_mode = Var(mode)
        self.v_st_pool_summary = Var("旧代码池摘要")
        self.v_st_precheck = Var("旧预检 1026只")
        self.v_st_status = Var("上次回测已完成")
        self.v_st_codes = Var("113008.SH")
        self.v_st_start = Var("2025-01-01")
        self.v_st_end = Var("2025-03-31")
        self.v_st_freq = Var("月")
        self.v_st_top_n = Var("1")
        self.v_st_history_mode = Var("标准")
        self.v_st_template = Var("估值偏差")
        self.v_batch_view = Var("综合机会")
        self._batch_results = []
        self._batch_all_results = []
        self._strategy_bt_running = False
        self.terms_cache = SimpleNamespace(list_bonds=lambda: ["113009.SH"])
        self.precheck_code_lists = []

    def _strategy_precheck_info(self):
        self.precheck_code_lists.append(self._strategy_codes_from_pool()[0])
        return super()._strategy_precheck_info()

    def _strategy_patch_precheck(self):
        return {"count": 1, "earliest": date(2020, 1, 1)}

    def _strategy_events_precheck(self):
        return {"count": 1}


@pytest.fixture
def rendered_rows(monkeypatch):
    """只替换绘表与外部数据读取，保留名单生成和跨页通知。"""
    calls = []
    monkeypatch.setattr(batch, "_render_table", lambda app, rows, **kw: calls.append(rows))
    monkeypatch.setattr(batch, "_refresh_view_menu_labels", lambda *a: None)
    monkeypatch.setattr(batch, "_update_valuation_banner", lambda *a: None)
    monkeypatch.setattr(batch, "refresh_home", lambda *a: None)
    return calls


def healthy_row(code):
    return {
        "bond_code": code, "status": "ok", "S0": 10.0, "K": 10.0,
        "market_price": 110.0, "theoretical_price": 110.0,
        "deviation": 0.0, "sigma": 0.3, "T": 3.0,
        "outstanding_balance": 10.0, "credit_rating": "AAA",
    }


def test_pool_display_labels_round_trip_and_accept_old_saved_keys():
    assert "当前筛选结果" in STRATEGY_POOL_MODES
    assert strategy_pool_label("当前筛选结果") == "跟随批量页筛选"
    for mode in STRATEGY_POOL_MODES:
        assert strategy_pool_from_label(strategy_pool_label(mode)) == mode
        assert strategy_pool_from_label(mode) == mode
    with pytest.raises(ValueError):
        strategy_pool_from_label("并不存在的代码池")


def test_pool_reads_published_list_and_its_actual_view_without_reinterpreting_form():
    app = App()
    app._batch_results = [{"bond_code": "113001.SH"}, {"bond_code": "113001.sh"}]
    app._batch_all_results = [healthy_row("113001.SH"), healthy_row("113002.SH")]
    app._batch_results_view = "双低"
    # 下拉变量已变、主表尚未发布新名单时，不能给旧名单贴上新视图名。
    app.v_batch_view.set("低估候选")

    codes, label = app._strategy_codes_from_pool()
    preview = app._strategy_pool_preview_text()

    assert codes == ["113001.SH"]
    assert "批量页" in label and "双低" in label
    assert "低估候选" not in label
    assert "1只" in preview and "每期" in preview and "下方限制" in preview


def test_batch_refresh_replaces_equal_sized_pool_and_updates_view_and_precheck(rendered_rows):
    app = App()
    batch._render_batch_views(app, [healthy_row("113001.SH"), healthy_row("113002.SH")])
    assert app.precheck_code_lists[-1] == ["113001.SH", "113002.SH"]

    app.v_batch_view.set("需复核")
    batch._render_batch_views(app, [
        {"bond_code": "113003.SH", "status": "error"},
        {"bond_code": "113004.SH", "status": "error"},
        healthy_row("113005.SH"),
    ])

    assert [row["bond_code"] for row in rendered_rows[-1]] == ["113003.SH", "113004.SH"]
    assert app.precheck_code_lists == [["113001.SH", "113002.SH"], ["113003.SH", "113004.SH"]]
    assert "需复核" in app.v_st_pool_summary.get()
    assert "2只" in app.v_st_pool_summary.get()
    assert "需复核" in app.v_st_precheck.get()
    assert "2只" in app.v_st_precheck.get() and "1026" not in app.v_st_precheck.get()
    assert app.v_st_status.get() == "上次回测已完成"


def test_empty_batch_view_clears_previous_pool_and_workload(rendered_rows):
    app = App()
    app._batch_results = [healthy_row("113001.SH")]
    app._batch_results_view = "综合机会"
    app.v_batch_view.set("转股折价")

    batch._change_batch_view(app)

    assert app._strategy_codes_from_pool()[0] == []
    assert app._batch_results_view == "转股折价"
    assert "转股折价" in app.v_st_pool_summary.get()
    assert "0只" in app.v_st_pool_summary.get()
    assert "空" in app.v_st_precheck.get() and "1026" not in app.v_st_precheck.get()


@pytest.mark.parametrize("mode", ["本地全市场", "自选代码"])
def test_non_following_pool_ignores_batch_changes(mode, rendered_rows):
    app = App(mode)
    own_codes = app._strategy_codes_from_pool()[0]

    batch._render_batch_views(app, [healthy_row("113001.SH")])

    assert app._strategy_codes_from_pool()[0] == own_codes
    assert app.v_st_pool_summary.get() == "旧代码池摘要"
    assert app.v_st_precheck.get() == "旧预检 1026只"
    assert app.precheck_code_lists == []


def test_running_task_keeps_its_recorded_pool_and_startup_precheck(rendered_rows):
    app = App()
    app._strategy_bt_running = True
    app.v_st_precheck.set("本次启动预检：113001.SH，1只")
    app._last_strategy_bt_result = {
        "run_settings": {"pool": {"mode": "当前筛选结果", "bond_codes": ["113001.SH"]}},
    }
    saved_result = deepcopy(app._last_strategy_bt_result)

    batch._render_batch_views(app, [healthy_row("113002.SH"), healthy_row("113003.SH")])

    assert app._strategy_codes_from_pool()[0] == ["113002.SH", "113003.SH"]
    assert app.v_st_pool_summary.get().startswith("下次回测")
    assert "2只" in app.v_st_pool_summary.get()
    assert app.v_st_precheck.get() == "本次启动预检：113001.SH，1只"
    assert app.precheck_code_lists == []
    assert app._last_strategy_bt_result == saved_result


def test_pool_notification_does_not_create_precheck_until_requested(rendered_rows):
    app = App()
    app.v_st_precheck.set("")

    batch._render_batch_views(app, [healthy_row("113001.SH")])

    assert "1只" in app.v_st_pool_summary.get()
    assert app.v_st_precheck.get() == ""
    assert app.precheck_code_lists == []


def test_preview_replaces_stale_workload_during_partial_date_input_then_recovers():
    app = App()
    app._batch_results = [healthy_row("113001.SH")]
    app.v_st_end.set("2025-03-")

    app._refresh_strategy_precheck_preview()

    assert "待运行" in app.v_st_precheck.get()
    assert "1026" not in app.v_st_precheck.get()
    assert app.v_st_status.get() == "上次回测已完成"

    app.v_st_end.set("2025-03-31")
    app._refresh_strategy_precheck_preview()

    assert "1只" in app.v_st_precheck.get() and "预计定价" in app.v_st_precheck.get()
    assert "待运行" not in app.v_st_precheck.get()
