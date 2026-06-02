import sys
from datetime import date

import convertible_bond.data_providers.wind as wind_mod
from convertible_bond.data_providers.wind import WindDataProvider, prepare_windpy_import_path


def test_prepare_windpy_import_path_uses_env_dir(monkeypatch, tmp_path):
    wind_dir = tmp_path / "wind-python"
    wind_dir.mkdir()
    (wind_dir / "WindPy.py").write_text("# fake WindPy\n", encoding="utf-8")
    monkeypatch.setenv("CBLENS_WINDPY_PATH", str(wind_dir))
    monkeypatch.setattr(sys, "platform", "linux", raising=False)
    monkeypatch.setattr(wind_mod.site, "getusersitepackages", lambda: str(tmp_path / "missing-user"))
    monkeypatch.setattr(wind_mod.site, "getsitepackages", lambda: [str(tmp_path / "missing-site")])
    monkeypatch.setattr(sys, "path", [p for p in sys.path if p != str(wind_dir)])

    added = prepare_windpy_import_path()

    assert added == [wind_dir]
    assert sys.path[0] == str(wind_dir)


def test_prepare_windpy_import_path_uses_env_file(monkeypatch, tmp_path):
    wind_file = tmp_path / "WindPy.py"
    wind_file.write_text("# fake WindPy\n", encoding="utf-8")
    monkeypatch.setenv("CBLENS_WINDPY_PATH", str(wind_file))
    monkeypatch.setattr(sys, "platform", "linux", raising=False)
    monkeypatch.setattr(wind_mod.site, "getusersitepackages", lambda: str(tmp_path / "missing-user"))
    monkeypatch.setattr(wind_mod.site, "getsitepackages", lambda: [str(tmp_path / "missing-site")])
    monkeypatch.setattr(sys, "path", [p for p in sys.path if p != str(tmp_path)])

    added = prepare_windpy_import_path()

    assert added == [tmp_path]
    assert sys.path[0] == str(tmp_path)


def test_prepare_windpy_import_path_prefers_frozen_bundle(monkeypatch, tmp_path):
    bundle_dir = tmp_path / "bundle"
    external_dir = tmp_path / "external"
    bundle_dir.mkdir()
    external_dir.mkdir()
    (bundle_dir / "WindPy.py").write_text("# bundled WindPy\n", encoding="utf-8")
    (external_dir / "WindPy.py").write_text("# external WindPy\n", encoding="utf-8")

    monkeypatch.setenv("CBLENS_WINDPY_PATH", str(external_dir))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle_dir), raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "App" / "Contents" / "MacOS" / "CBLens"))
    monkeypatch.setattr(sys, "platform", "linux", raising=False)
    monkeypatch.setattr(wind_mod.site, "getusersitepackages", lambda: str(tmp_path / "missing-user"))
    monkeypatch.setattr(wind_mod.site, "getsitepackages", lambda: [str(tmp_path / "missing-site")])
    monkeypatch.setattr(sys, "path", [str(external_dir), str(bundle_dir), "original"])

    added = prepare_windpy_import_path()

    assert added[:2] == [bundle_dir, external_dir]
    assert sys.path[:3] == [str(bundle_dir), str(external_dir), "original"]


def test_get_bond_terms_reads_wind_reset_trigger_ratio(monkeypatch):
    class Result:
        ErrorCode = 0

        def __init__(self, fields, values):
            self.Fields = fields
            self.Data = [[values.get(field)] for field in fields]

    class FakeWind:
        def __init__(self):
            self.requested_fields = None

        def wss(self, code, fields, options):
            requested = fields.split(",")
            self.requested_fields = requested
            return Result(
                requested,
                {
                    "sec_name": "测试转债",
                    "clause_reset_resettriggerratio": 85.0,
                },
            )

    fake_wind = FakeWind()
    provider = WindDataProvider()
    monkeypatch.setattr(provider, "_ensure", lambda: fake_wind)

    terms = provider.get_bond_terms("113001.SH", date(2026, 5, 25))

    assert "clause_reset_resettriggerratio" in fake_wind.requested_fields
    assert terms.down_reset_trigger_pct == 85.0


def test_wss_candidate_invalid_indicator_is_cached(monkeypatch):
    class Result:
        def __init__(self, error_code, data):
            self.ErrorCode = error_code
            self.Data = data

    class FakeWind:
        def __init__(self):
            self.calls = []

        def wss(self, code, field, options):
            self.calls.append(field)
            if field == "bad_field":
                return Result(-40522006, [["CWSSService: invalid indicators."]])
            return Result(0, [[42]])

    fake_wind = FakeWind()
    provider = WindDataProvider()
    monkeypatch.setattr(provider, "_ensure", lambda: fake_wind)

    assert provider._wss_first_available("113001.SH", ("bad_field", "good_field"), date(2026, 5, 25)) == 42
    assert provider._wss_first_available("113002.SH", ("bad_field", "good_field"), date(2026, 5, 25)) == 42

    assert fake_wind.calls == ["bad_field", "good_field", "good_field"]


def test_get_bond_terms_error_includes_wind_error_code(monkeypatch):
    class Result:
        ErrorCode = -40521007
        Data = [["WSS: SkyClient request failed"]]

    class FakeWind:
        def wss(self, code, fields, options):
            return Result()

    provider = WindDataProvider()
    monkeypatch.setattr(provider, "_ensure", lambda: FakeWind())

    try:
        provider.get_bond_terms("113001.SH", date(2026, 5, 25))
    except RuntimeError as exc:
        text = str(exc)
    else:
        raise AssertionError("expected RuntimeError")

    assert "ErrorCode=-40521007" in text
    assert "SkyClient request failed" in text
