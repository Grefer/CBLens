"""``cb-repair-rating-patches``: 用当前解析器重放评级族 patch.

评级 / 展望 / 观察状态三个字段都只有公告正文一个来源 (Wind 的 creditrating 是发行时冻结值,
ratingoutlook 实测取不到), 所以解析器的历次 bug 全部原样沉淀在 patch 库里, 而存量 patch
不会被任何流程重新审视。

本工具**不猜数值** —— 它把正文重新取回来用当前解析器重新推导, 是换了一个独立证据源。
方向由证据强度决定, 三档不对称:

    解析出新值 → 改写      解析不出 → 删该字段      正文取不到 → 原样保留
"""
from datetime import date

from convertible_bond.cb_events import CBEvent, CBEventStore
from convertible_bond.cli import repair_rating_patches as mod
from convertible_bond.historical_terms import TermsPatch, TermsPatchStore

_URL = "https://example.invalid/a.PDF"


def _seed(tmp_path, fields, *, title="XX2026年跟踪评级报告"):
    patches = tmp_path / "patches.json"
    events = tmp_path / "events.json"
    TermsPatchStore(patches).add_many([
        TermsPatch(bond_code="113001.SH", effective_date=date(2026, 6, 13),
                   fields=dict(fields), source="cninfo", raw_title=title),
    ])
    CBEventStore(events).add_many([
        CBEvent(bond_code="113001.SH", event_date=date(2026, 6, 13),
                event_type="rating_change", raw_title=title, url=_URL, source="cninfo"),
    ])
    return patches, events


def _fake_body(monkeypatch, text):
    monkeypatch.setattr(mod, "fetch_body", lambda url, cache_dir, *, download: text)


def test_rewrites_rating_when_replay_disagrees(tmp_path, monkeypatch):
    """存量 A+ 是 rating_re 缺左界时从 AA+ 里抠出来的; 重放正文得到 AA+。"""
    patches, events = _seed(tmp_path, {"credit_rating": "A+"})
    _fake_body(monkeypatch, "维持“福能转债”信用等级为AA+，评级展望为稳定。")

    report = mod.repair(patches, events, dry_run=False, backup=False)
    assert report["changed"] == 1
    kept = TermsPatchStore(patches).list_patches(include_shadowed=True)
    assert kept[0].fields["credit_rating"] == "AA+"


def test_drops_field_when_replay_cannot_confirm_it(tmp_path, monkeypatch):
    """观察状态来自附录词表, 当前解析器已不再产生 → 该字段没有来源了, 删掉。"""
    patches, events = _seed(
        tmp_path, {"credit_rating": "AA", "credit_watch_status": "列入观察名单"})
    _fake_body(monkeypatch, "维持“鸿路转债”信用等级为AA。")

    mod.repair(patches, events, dry_run=False, backup=False)
    kept = TermsPatchStore(patches).list_patches(include_shadowed=True)
    assert kept[0].fields == {"credit_rating": "AA"}


def test_removes_patch_entirely_when_nothing_survives(tmp_path, monkeypatch):
    patches, events = _seed(tmp_path, {"credit_watch_status": "列入观察名单"})
    _fake_body(monkeypatch, "本次跟踪评级不涉及评级行动。")

    report = mod.repair(patches, events, dry_run=False, backup=False)
    assert report["removed"] == 1
    assert TermsPatchStore(patches).list_patches(include_shadowed=True) == []


def test_missing_body_leaves_patch_untouched(tmp_path, monkeypatch):
    """取不到证据 ≠ 证据为否。扫描件公告 (正文 0 字) 不能让存量数据被清空。"""
    patches, events = _seed(tmp_path, {"credit_rating": "AA-"})
    _fake_body(monkeypatch, None)

    report = mod.repair(patches, events, dry_run=False, backup=False)
    assert report["changed"] == 0 and report["removed"] == 0
    assert report["stats"].get("正文取不到(原样保留)") == 1
    kept = TermsPatchStore(patches).list_patches(include_shadowed=True)
    assert kept[0].fields == {"credit_rating": "AA-"}


def test_dry_run_does_not_touch_the_file(tmp_path, monkeypatch):
    patches, events = _seed(tmp_path, {"credit_rating": "A+"})
    _fake_body(monkeypatch, "维持“福能转债”信用等级为AA+。")
    before = patches.read_bytes()

    report = mod.repair(patches, events, dry_run=True, backup=False)
    assert report["planned"] == 1 and report["changed"] == 0
    assert patches.read_bytes() == before


def test_scan_sees_shadowed_patches(tmp_path):
    """回洗要看**文件里到底有什么**: 被权威源遮蔽的脏 patch 否则既扫不到也删不掉,
    而且遮蔽视图返回的是副本, 它的 key() 与磁盘上那条不同, 计划一条都对不上。"""
    patches = tmp_path / "patches.json"
    events = tmp_path / "events.json"
    TermsPatchStore(patches).add_many([
        TermsPatch(bond_code="113001.SH", effective_date=date(2026, 6, 13),
                   fields={"credit_rating": "A", "conversion_price": 99.9},
                   source="cninfo", raw_title="T"),
        TermsPatch(bond_code="113001.SH", effective_date=date(2026, 7, 1),
                   fields={"conversion_price": 12.0}, source="wind_asof"),
    ])
    CBEventStore(events).add_many([
        CBEvent(bond_code="113001.SH", event_date=date(2026, 6, 13),
                event_type="rating_change", raw_title="T", url=_URL, source="cninfo"),
    ])
    targets = mod.scan(patches, events)["targets"]
    assert len(targets) == 1
    # 拿到的必须是**未被遮蔽**的原件: conversion_price 还在, key() 才对得上磁盘
    assert "conversion_price" in targets[0][0].fields
