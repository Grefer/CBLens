"""转股价存量修复必须可预览、可回退，且不误伤权威值或其他字段。"""
from datetime import date
from dataclasses import replace

import pytest

from convertible_bond.cb_events import CBEvent, CBEventStore
from convertible_bond.cli import repair_conversion_prices as mod
from convertible_bond.historical_terms import TermsPatch, TermsPatchStore


def _seed(tmp_path, *, mixed=False):
    path, events = tmp_path / "patches.json", tmp_path / "events.json"
    patch = TermsPatch(
        "123091.SZ", date(2026, 9, 9), {"conversion_price": 15.64},
        source="cninfo", raw_title="关于转股价格调整的公告",
        event_date=date(2026, 9, 2), before_fields={"conversion_price": 15.79},
    )
    if mixed:
        patch = replace(patch, fields={**patch.fields, "credit_rating": "AA"},
                        before_fields={**patch.before_fields, "credit_rating": "AA-"})
    TermsPatchStore(path).add_many([patch])
    CBEventStore(events).add_many([
        CBEvent(patch.bond_code, patch.event_date, "conversion_price_adjusted",
                raw_title=patch.raw_title, url="https://example.invalid/current.pdf"),
    ])
    return path, events, patch


def _body(monkeypatch, text=None):
    text = text or ("调整前转股价格为：14.99元/股，调整后转股价格为：14.79元/股，"
                    "转股价格调整生效日期：2026年9月9日。")
    monkeypatch.setattr(mod, "fetch_body", lambda *_a, **_k: text)


def test_preview_backup_rewrite_and_idempotence(tmp_path, monkeypatch):
    path, events, _ = _seed(tmp_path)
    _body(monkeypatch)
    original = path.read_bytes()
    preview = mod.repair(path, events)
    assert preview["planned"] == 1 and preview["changed"] == 0
    assert path.read_bytes() == original
    result = mod.repair(path, events, dry_run=False)
    assert result["changed"] == 1
    from pathlib import Path
    assert Path(result["backup_path"]).read_bytes() == original
    rows = TermsPatchStore(path).list_patches(include_shadowed=True)
    assert len(rows) == 1
    assert rows[0].fields == {"conversion_price": 14.79}
    assert rows[0].before_fields == {"conversion_price": 14.99}
    assert "14.99->14.79" in rows[0].note
    assert mod.repair(path, events, dry_run=False)["planned"] == 0


def test_preserves_other_fields_and_sources_even_with_same_key(tmp_path, monkeypatch):
    path, events, patch = _seed(tmp_path, mixed=True)
    # 老数据可含同 key 的不同 source；key() 本身不含 source。
    import json
    payload = json.loads(path.read_text())
    for source in ("manual", "wind_asof"):
        payload["patches"].append({**payload["patches"][0], "source": source})
    path.write_text(json.dumps(payload))
    _body(monkeypatch)
    mod.repair(path, events, dry_run=False)
    rows = TermsPatchStore(path).list_patches(include_shadowed=True)
    for row in rows:
        if row.source == "cninfo":
            assert row.fields == {"conversion_price": 14.79, "credit_rating": "AA"}
            assert row.before_fields == {"conversion_price": 14.99, "credit_rating": "AA-"}
        else:
            assert row.fields == patch.fields and row.before_fields == patch.before_fields


@pytest.mark.parametrize("text,reason", [(None, "no_body"), ("扫描无有效转股价", "unparsed")])
def test_missing_evidence_preserves_original(tmp_path, monkeypatch, text, reason):
    path, events, _ = _seed(tmp_path)
    before = path.read_bytes()
    monkeypatch.setattr(mod, "fetch_body", lambda *_a, **_k: text)
    report = mod.repair(path, events, dry_run=False)
    assert report["stats"][reason] == 1
    assert path.read_bytes() == before


def test_same_title_resolves_the_actual_announcement_date(tmp_path, monkeypatch):
    path, events, patch = _seed(tmp_path)
    CBEventStore(events).add_many([
        CBEvent(patch.bond_code, date(2025, 9, 2), "conversion_price_adjusted",
                raw_title=patch.raw_title, url="https://example.invalid/previous.pdf"),
    ])
    seen = []
    monkeypatch.setattr(mod, "fetch_body", lambda url, *_a, **_k: seen.append(url))
    mod.repair(path, events)
    assert seen == ["https://example.invalid/current.pdf"]


def test_known_date_without_exact_match_does_not_use_another_year(tmp_path, monkeypatch):
    path, events, _ = _seed(tmp_path)
    # 标题虽唯一，但唯一那份的年份与 patch 公告日不同，不能猜。
    store = TermsPatchStore(path)
    store.rewrite(lambda p: replace(p, event_date=date(2025, 9, 2)))
    seen = []
    monkeypatch.setattr(mod, "fetch_body", lambda url, *_a, **_k: seen.append(url))
    report = mod.repair(path, events)
    assert report["stats"]["no_url"] == 1
    assert seen == []


def test_scanning_concurrent_change_is_not_overwritten(tmp_path, monkeypatch):
    path, events, _ = _seed(tmp_path)
    _body(monkeypatch)
    newer = path.read_bytes() + b"\n"
    with pytest.raises(mod.ConcurrentWriteError):
        mod.repair(path, events, dry_run=False,
                   progress=lambda *_a: path.write_bytes(newer))
    assert path.read_bytes() == newer
    assert not list(tmp_path.glob("*.bak-*.json"))


def test_codes_filter_and_mixed_field_dates_are_not_silently_rewritten(tmp_path, monkeypatch):
    path, events, _ = _seed(tmp_path, mixed=True)
    _body(monkeypatch, "调整前转股价格为14.99元/股，调整后转股价格为14.79元/股，"
                      "生效日期2026年9月10日。")
    before = path.read_bytes()
    assert mod.repair(path, events, codes={"113001.SH"})["scanned"] == 0
    report = mod.repair(path, events, dry_run=False)
    assert report["stats"]["mixed_date_conflict"] == 1
    assert path.read_bytes() == before
