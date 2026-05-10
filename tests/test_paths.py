import sys
import json

from convertible_bond import paths


def test_source_data_path_defaults_to_repo_data(monkeypatch):
    monkeypatch.delenv("CBLENS_DATA_DIR", raising=False)
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)

    assert paths.data_path("cb_data.json").name == "cb_data.json"
    assert paths.data_path("cb_data.json").parent.name == "data"


def test_env_data_dir_override(monkeypatch, tmp_path):
    monkeypatch.setenv("CBLENS_DATA_DIR", str(tmp_path))

    assert paths.data_path("watchlist.json") == tmp_path / "watchlist.json"


def test_frozen_seeded_data_file(monkeypatch, tmp_path):
    bundled = tmp_path / "bundle"
    bundled_data = bundled / "data"
    user_data = tmp_path / "user"
    bundled_data.mkdir(parents=True)
    (bundled_data / "cb_events.json").write_text('{"events": []}', encoding="utf-8")

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundled), raising=False)
    monkeypatch.setenv("CBLENS_DATA_DIR", str(user_data))

    target = paths.data_path("cb_events.json", seed=True)

    assert target == user_data / "cb_events.json"
    assert target.read_text(encoding="utf-8") == '{"events": []}'


def test_frozen_seed_replaces_empty_cb_data(monkeypatch, tmp_path):
    bundled = tmp_path / "bundle"
    bundled_data = bundled / "data"
    user_data = tmp_path / "user"
    bundled_data.mkdir(parents=True)
    user_data.mkdir(parents=True)
    seed_payload = {
        "128009.SZ": {"sec_name": "测试转债"},
        "_bundle_meta": {"n_bonds": 1},
    }
    (bundled_data / "cb_data.json").write_text(
        json.dumps(seed_payload, ensure_ascii=False), encoding="utf-8")
    (user_data / "cb_data.json").write_text(
        json.dumps({"_bundle_meta": {"n_bonds": 0}}), encoding="utf-8")

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundled), raising=False)
    monkeypatch.setenv("CBLENS_DATA_DIR", str(user_data))

    target = paths.data_path("cb_data.json", seed=True)

    assert json.loads(target.read_text(encoding="utf-8")) == seed_payload


def test_frozen_seed_finds_onedir_internal_data(monkeypatch, tmp_path):
    exe_dir = tmp_path / "dist" / "CBLens"
    bundled_data = exe_dir / "_internal" / "data"
    user_data = tmp_path / "user"
    bundled_data.mkdir(parents=True)
    (bundled_data / "cb_events.json").write_text('{"events": []}', encoding="utf-8")

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "missing_meipass"), raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "CBLens"), raising=False)
    monkeypatch.setenv("CBLENS_DATA_DIR", str(user_data))

    target = paths.data_path("cb_events.json", seed=True)

    assert target.read_text(encoding="utf-8") == '{"events": []}'


def test_frozen_seed_replaces_empty_batch_cache(monkeypatch, tmp_path):
    bundled = tmp_path / "bundle"
    bundled_data = bundled / "data"
    user_data = tmp_path / "user"
    bundled_data.mkdir(parents=True)
    user_data.mkdir(parents=True)
    seed_payload = {
        "_meta": {"n_results": 1},
        "results": [{"bond_code": "128009.SZ", "status": "ok"}],
        "upcoming_results": [],
    }
    (bundled_data / "batch_pricing_cache.json").write_text(
        json.dumps(seed_payload, ensure_ascii=False), encoding="utf-8")
    (user_data / "batch_pricing_cache.json").write_text(
        json.dumps({"_meta": {"n_results": 0}, "results": []}), encoding="utf-8")

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundled), raising=False)
    monkeypatch.setenv("CBLENS_DATA_DIR", str(user_data))

    target = paths.data_path("batch_pricing_cache.json", seed=True)

    assert json.loads(target.read_text(encoding="utf-8")) == seed_payload


def test_asset_path_points_to_assets_dir():
    assert paths.asset_path("cblens-icon.png").parts[-2:] == ("assets", "cblens-icon.png")
