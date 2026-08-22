"""cb-repair-balance-patches: 清洗被赎回门槛条款污染的余额 patch。"""
from datetime import date

import pytest

from convertible_bond.cache import TermsBundle
from convertible_bond.cli import repair_balance_patches as repair_module
from convertible_bond.cli.repair_balance_patches import repair, scan
from convertible_bond.data_providers import BondTerms
from convertible_bond.historical_terms import TermsPatch, TermsPatchStore


def _patch(code, day, fields, event_type="call_no_redemption"):
    return TermsPatch(
        bond_code=code,
        effective_date=date(2026, 1, day),
        event_date=date(2026, 1, day),
        fields=dict(fields),
        source="cninfo",
        note="余额 0.3亿",
        raw_title=f"关于不提前赎回{code}的公告",
        confidence="parsed",
        source_event_key=f"{code}|2026-01-{day:02d}|{event_type}|标题",
    )


def _terms(code, balance):
    return BondTerms(
        sec_name=f"{code}债",
        underlying_code="000001.SZ",
        conversion_price=10.0,
        maturity_date=date(2030, 1, 1),
        outstanding_balance=balance,
    )


@pytest.fixture()
def store_paths(tmp_path):
    patches = tmp_path / "patches.json"
    bundle_path = tmp_path / "cb_data.json"

    store = TermsPatchStore(patches)
    store.add_many([
        # 大盘券: 真实余额 46 亿, 却被门槛条款写成 0.3
        _patch("113691.SH", 5, {"outstanding_balance": 0.3}),
        _patch("113691.SH", 6, {"outstanding_balance": 0.3}),
        # 同时带赎回价: 只应剔除余额字段, 保留赎回价
        _patch("113043.SH", 7, {"outstanding_balance": 0.3,
                                "call_redemption_price": 100.68}, "call_redemption"),
        # 真实余额本就小于门槛的债: 同样是错值, 一并清掉
        _patch("113039.SH", 8, {"outstanding_balance": 0.3}),
        # 正常余额 patch: 不能动
        _patch("110074.SH", 9, {"outstanding_balance": 2.35768}, "balance_change"),
        # 与余额无关的 patch: 不能动
        _patch("110077.SH", 10, {"conversion_price": 12.34}),
    ])

    bundle = TermsBundle(bundle_path)
    bundle.set("113691.SH", _terms("113691.SH", 45.99), source="unit")
    bundle.set("113043.SH", _terms("113043.SH", 37.99), source="unit")
    bundle.set("113039.SH", _terms("113039.SH", 0.024), source="unit")
    bundle.set("110074.SH", _terms("110074.SH", 2.35768), source="unit")
    return patches, bundle_path


def test_scan_reports_threshold_hits_with_evidence(store_paths):
    patches, bundle_path = store_paths
    report = scan(patches, bundle_path)

    assert report["n_balance_patches"] == 5      # 4 条门槛值 + 1 条正常
    assert len(report["hits"]) == 4
    audit = {row["bond_code"]: row for row in report["audit"]}
    assert set(audit) == {"113691.SH", "113043.SH", "113039.SH"}
    assert audit["113691.SH"]["n_patches"] == 2
    assert audit["113691.SH"]["verdict"] == "真实余额更大"
    # 真实余额比门槛值还小的债: 判定相反, 但同样是错值
    assert audit["113039.SH"]["verdict"] == "真实余额更小"


def test_repair_dry_run_does_not_write(store_paths):
    patches, bundle_path = store_paths
    before = patches.read_text(encoding="utf-8")

    report = repair(patches, bundle_path, dry_run=True)

    assert report["changed"] == 1        # 113043 保留赎回价
    assert report["removed"] == 3        # 其余三条整删
    assert report["backup_path"] is None
    assert patches.read_text(encoding="utf-8") == before


def test_repair_apply_drops_only_balance_field(store_paths):
    patches, bundle_path = store_paths

    report = repair(patches, bundle_path, dry_run=False, backup=False)
    assert (report["changed"], report["removed"]) == (1, 3)

    reloaded = TermsPatchStore(patches)
    assert reloaded.list_patches("113691.SH") == []

    # 多字段 patch 只掉余额, 赎回价原样保留
    kept = reloaded.list_patches("113043.SH")
    assert len(kept) == 1
    assert kept[0].fields == {"call_redemption_price": 100.68}

    # 正常余额与无关 patch 不受影响
    assert reloaded.list_patches("110074.SH")[0].fields == {"outstanding_balance": 2.35768}
    assert reloaded.list_patches("110077.SH")[0].fields == {"conversion_price": 12.34}


def test_repair_apply_writes_backup(store_paths):
    patches, bundle_path = store_paths
    report = repair(patches, bundle_path, dry_run=False, backup=True)
    backup = report["backup_path"]
    assert backup is not None and backup.exists()
    # 备份里仍是清洗前的内容
    assert TermsPatchStore(backup).list_patches("113691.SH")


def test_repair_is_idempotent(store_paths):
    patches, bundle_path = store_paths
    repair(patches, bundle_path, dry_run=False, backup=False)
    again = repair(patches, bundle_path, dry_run=False, backup=False)
    assert (again["changed"], again["removed"]) == (0, 0)
    assert again["hits"] == []


def test_repair_leaves_pool_relevant_balances_intact(store_paths):
    """清洗后, 受害大盘券不再被 patch 覆盖成 0.3, 准入看到的就是 cb_data 原值。"""
    patches, bundle_path = store_paths
    repair(patches, bundle_path, dry_run=False, backup=False)

    store = TermsPatchStore(patches)
    bundle = TermsBundle(bundle_path)
    terms = store.apply("113691.SH", bundle.get("113691.SH"), date(2026, 6, 1))
    assert terms.outstanding_balance == pytest.approx(45.99)


def test_scan_flags_raw_equal_to_threshold_as_possible_real_disclosure(tmp_path):
    """raw 余额恰等于门槛 = 唯一"可能是真实披露"的情形, 必须单列供人工复核。

    回洗判据是数值而解析侧判据是措辞, 二者在这一点上冲突; 报告里的这一栏是人工
    识别误删的唯一抓手, 不能和"没有 raw 可比"混在一起。
    """
    patches = tmp_path / "patches.json"
    bundle_path = tmp_path / "cb_data.json"
    TermsPatchStore(patches).add_many([
        _patch("113050.SH", 5, {"outstanding_balance": 0.3}, "conversion_suspension"),
    ])
    bundle = TermsBundle(bundle_path)
    bundle.set("113050.SH", _terms("113050.SH", 0.3), source="unit")

    audit = scan(patches, bundle_path)["audit"]
    assert audit[0]["verdict"] == "疑似真实披露"


def test_repair_refuses_to_write_when_patch_file_changed_underneath(store_paths):
    """扫描后、写盘前被别的进程改过就拒绝写 —— 否则回洗成果会被并发快照整份覆盖。"""
    patches, bundle_path = store_paths

    original = repair_module.scan

    def scan_then_tamper(*args, **kwargs):
        report = original(*args, **kwargs)
        # 模拟 GUI 后台公告同步在这个窗口里落了一次盘
        TermsPatchStore(patches).add_many([
            _patch("110099.SH", 11, {"conversion_price": 5.0}),
        ])
        return report

    repair_module.scan = scan_then_tamper
    try:
        with pytest.raises(repair_module.ConcurrentWriteError):
            repair(patches, bundle_path, dry_run=False, backup=False)
    finally:
        repair_module.scan = original

    # 拒写之后原始脏 patch 仍在, 没有被半途改坏
    assert TermsPatchStore(patches).list_patches("113691.SH")
