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
