import sys

from convertible_bond.data_providers.wind import prepare_windpy_import_path


def test_prepare_windpy_import_path_uses_env_dir(monkeypatch, tmp_path):
    wind_dir = tmp_path / "wind-python"
    wind_dir.mkdir()
    (wind_dir / "WindPy.py").write_text("# fake WindPy\n", encoding="utf-8")
    monkeypatch.setenv("CBLENS_WINDPY_PATH", str(wind_dir))
    monkeypatch.setattr(sys, "platform", "linux", raising=False)
    monkeypatch.setattr(sys, "path", [p for p in sys.path if p != str(wind_dir)])

    added = prepare_windpy_import_path()

    assert added == [wind_dir]
    assert sys.path[0] == str(wind_dir)


def test_prepare_windpy_import_path_uses_env_file(monkeypatch, tmp_path):
    wind_file = tmp_path / "WindPy.py"
    wind_file.write_text("# fake WindPy\n", encoding="utf-8")
    monkeypatch.setenv("CBLENS_WINDPY_PATH", str(wind_file))
    monkeypatch.setattr(sys, "platform", "linux", raising=False)
    monkeypatch.setattr(sys, "path", [p for p in sys.path if p != str(tmp_path)])

    added = prepare_windpy_import_path()

    assert added == [tmp_path]
    assert sys.path[0] == str(tmp_path)
