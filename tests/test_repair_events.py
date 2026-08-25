"""cb-repair-events: 清洗兄弟债公告串号写入的 patch。

守卫 ``_title_names_other_bond`` 是后加的; 上线前落库的 patch 不会再被任何流程重新审视,
所以需要一次存量回洗。判据必须与解析侧共用同一个函数 —— 两边分叉正是余额回洗踩过的坑。
"""
from datetime import date

import pytest

from convertible_bond.cache import TermsBundle
from convertible_bond.cli import repair_events as mod
from convertible_bond.cli.repair_events import repair, scan
from convertible_bond.data_providers import BondTerms
from convertible_bond.historical_terms import TermsPatch, TermsPatchStore


def _patch(code, day, fields, title):
    return TermsPatch(
        bond_code=code,
        effective_date=date(2026, 1, day),
        event_date=date(2026, 1, day),
        fields=dict(fields),
        source="cninfo",
        raw_title=title,
        confidence="parsed",
        source_event_key=f"{code}|2026-01-{day:02d}|call_redemption|{title}",
    )


@pytest.fixture()
def paths(tmp_path):
    patches, bundle_path = tmp_path / "patches.json", tmp_path / "cb_data.json"
    TermsPatchStore(patches).add_many([
        # 兄弟债强赎公告挂到本债: 赎回价被写错, 回测里会让 pricer 以为本债即将被赎
        _patch("113652.SH", 5, {"call_redemption_price": 100.2904},
               "伟明环保关于实施“伟24转债”赎回暨摘牌的公告"),
        _patch("113652.SH", 6, {"call_redemption_price": 100.2904},
               "伟明环保关于实施“伟24转债”赎回暨摘牌的第一次提示性公告"),
        # 本债自己的公告: 必须原样保留
        _patch("113652.SH", 7, {"call_redemption_price": 101.5},
               "伟明环保关于“伟22转债”赎回的公告"),
        # 标题没点名任何转债: 无从判断, 保留
        _patch("110074.SH", 8, {"conversion_price": 12.34}, "关于调整转股价格的公告"),
        # 权威源 patch 没有标题, 不该被误伤
        TermsPatch(bond_code="110074.SH", effective_date=date(2026, 1, 9),
                   event_date=date(2026, 1, 9), fields={"conversion_price": 12.0},
                   source="wind_asof", confidence="wind"),
    ])
    bundle = TermsBundle(bundle_path)
    for code, name in [("113652.SH", "伟22转债"), ("110074.SH", "某某转债")]:
        bundle.set(code, BondTerms(sec_name=name, underlying_code="000001.SZ",
                                   conversion_price=10.0, maturity_date=date(2030, 1, 1)),
                   source="unit")
    return patches, bundle_path


def test_scan_flags_only_sibling_bond_patches(paths):
    report = scan(*paths)
    assert {p.bond_code for p in report["hits"]} == {"113652.SH"}
    assert len(report["hits"]) == 2
    audit = report["audit"][0]
    assert audit["bond_name"] == "伟22转债"
    # 字段栏是出现次数, 不是数值累加 (Counter.update(dict) 会把 100.29 加两遍)
    assert audit["fields"] == {"call_redemption_price": 2}


def test_dry_run_does_not_write(paths):
    before = paths[0].read_text(encoding="utf-8")
    report = repair(*paths, dry_run=True)
    assert report["removed"] == 2
    assert paths[0].read_text(encoding="utf-8") == before


def test_apply_keeps_own_and_authoritative_patches(paths):
    assert repair(*paths, dry_run=False, backup=False)["removed"] == 2

    store = TermsPatchStore(paths[0])
    kept = store.list_patches("113652.SH")
    assert [p.fields for p in kept] == [{"call_redemption_price": 101.5}]
    # 无标题的与权威源的都还在文件里 (默认视图里前者被 wind_asof 遮蔽, 所以要看原始视图)
    assert len(store.list_patches("110074.SH", include_shadowed=True)) == 2


def test_repair_is_idempotent(paths):
    repair(*paths, dry_run=False, backup=False)
    again = repair(*paths, dry_run=False, backup=False)
    assert (again["removed"], again["hits"]) == (0, [])


def test_apply_writes_backup(paths):
    backup = repair(*paths, dry_run=False, backup=True)["backup_path"]
    assert backup is not None and backup.exists()
    assert len(TermsPatchStore(backup).list_patches("113652.SH", include_shadowed=True)) == 3


def test_refuses_to_write_when_patch_file_changed_underneath(paths):
    original = mod.scan

    def scan_then_tamper(*a, **kw):
        report = original(*a, **kw)
        TermsPatchStore(paths[0]).add_many([
            _patch("110074.SH", 11, {"conversion_price": 5.0}, "关于调整转股价格的公告")])
        return report

    mod.scan = scan_then_tamper
    try:
        with pytest.raises(mod.ConcurrentWriteError):
            repair(*paths, dry_run=False, backup=False)
    finally:
        mod.scan = original
    assert len(TermsPatchStore(paths[0]).list_patches("113652.SH", include_shadowed=True)) == 3


def test_scan_sees_patches_shadowed_by_authoritative_source(tmp_path):
    """被 Wind 遮蔽的脏 patch 也必须能扫到 —— 否则它既删不掉, 又会在权威源覆盖收窄时复活。"""
    patches, bundle_path = tmp_path / "patches.json", tmp_path / "cb_data.json"
    TermsPatchStore(patches).add_many([
        _patch("113652.SH", 5, {"conversion_price": 3.26},
               "伟明环保关于“伟24转债”转股价格调整的公告"),
        TermsPatch(bond_code="113652.SH", effective_date=date(2026, 1, 5),
                   event_date=date(2026, 1, 5), fields={"conversion_price": 19.02},
                   source="wind_asof", confidence="wind"),
    ])
    TermsBundle(bundle_path).set(
        "113652.SH", BondTerms(sec_name="伟22转债", underlying_code="000001.SZ",
                               conversion_price=19.02, maturity_date=date(2030, 1, 1)),
        source="unit")

    store = TermsPatchStore(patches)
    assert len(store.list_patches("113652.SH")) == 1              # 生效视图: 脏的被遮蔽
    assert len(store.list_patches("113652.SH", include_shadowed=True)) == 2

    assert repair(patches, bundle_path, dry_run=False, backup=False)["removed"] == 1
    left = TermsPatchStore(patches).list_patches("113652.SH", include_shadowed=True)
    assert [p.source for p in left] == ["wind_asof"]


# ── 评级: 末条 patch 若是当前等级削掉前导字母的次级等级, 即为可证的解析残缺 ──

def test_stale_rating_tail_patch_is_dropped(tmp_path):
    """``.{0,10}`` 回溯会让评级"尽量晚开始", 从 AA- 抠出 A-; 低评级会让债在回测准入里被误杀。

    只对**末条**成立 —— 中间历史值本就该与当前值不同, 末条必须等于 cb_data 的权威当前值。
    评级没有 Wind as-of 源可重建, 所以只删可证错的, 不猜一个对的填回去。
    """
    patches, bundle_path = tmp_path / "patches.json", tmp_path / "cb_data.json"
    TermsPatchStore(patches).add_many([
        # 真实历史降级: AA+ → AA, 中间值与当前值不同是正常的, 不能动
        _patch("110099.SH", 5, {"credit_rating": "AA+"}, "关于“福能转债”2024年跟踪评级结果的公告"),
        # 末条被削掉首字母: cb_data 是 AA+, patch 是 A+
        _patch("110099.SH", 9, {"credit_rating": "A+"}, "关于“福能转债”2026年跟踪评级结果的公告"),
        # 真实降级到次级等级但方向相反 (当前 A+, patch AA) —— 不符合"削首字母"签名, 保留
        _patch("123138.SZ", 9, {"credit_rating": "AA"}, "关于“丝路转债”跟踪评级结果的公告"),
    ])
    bundle = TermsBundle(bundle_path)
    for code, name, rating in [("110099.SH", "福能转债", "AA+"), ("123138.SZ", "丝路转债", "A+")]:
        bundle.set(code, BondTerms(sec_name=name, underlying_code="000001.SZ",
                                   conversion_price=10.0, maturity_date=date(2030, 1, 1),
                                   credit_rating=rating), source="unit")

    assert repair(patches, bundle_path, dry_run=False, backup=False)["removed"] == 1
    left = TermsPatchStore(patches)
    assert [p.fields for p in left.list_patches("110099.SH", include_shadowed=True)] == \
        [{"credit_rating": "AA+"}]
    assert len(left.list_patches("123138.SZ", include_shadowed=True)) == 1
