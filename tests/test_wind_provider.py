import sys
from datetime import date, datetime

import convertible_bond.data_providers.wind as wind_mod
from convertible_bond.data_providers.wind import (
    WindDataProvider,
    _is_transient_wind_result,
    prepare_windpy_import_path,
)


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
    monkeypatch.setattr(wind_mod.time, "sleep", lambda _seconds: None)

    try:
        provider.get_bond_terms("113001.SH", date(2026, 5, 25))
    except RuntimeError as exc:
        text = str(exc)
    else:
        raise AssertionError("expected RuntimeError")

    assert "ErrorCode=-40521007" in text
    assert "SkyClient request failed" in text


def test_transient_wind_result_detects_error_code_without_data():
    class Result:
        ErrorCode = -40521007
        Data = []

    assert _is_transient_wind_result(Result())


def test_call_wss_retries_transient_error_with_connection_reset(monkeypatch):
    class Result:
        def __init__(self, error_code, data):
            self.ErrorCode = error_code
            self.Data = data

    class FakeWind:
        def __init__(self, result):
            self.result = result
            self.stopped = False
            self.calls = 0

        def wss(self, code, fields, options):
            self.calls += 1
            return self.result

        def stop(self):
            self.stopped = True

    first = FakeWind(Result(-40521007, []))
    second = FakeWind(Result(0, [["ok"]]))
    winds = [first, second]
    sleeps = []

    provider = WindDataProvider()

    def ensure():
        provider._w = winds.pop(0)
        return provider._w

    monkeypatch.setattr(provider, "_ensure", ensure)
    monkeypatch.setattr(wind_mod.time, "sleep", lambda seconds: sleeps.append(seconds))

    res = provider._call_wss("113001.SH", "sec_name", "tradeDate=20260525")

    assert res.ErrorCode == 0
    assert first.calls == 1
    assert second.calls == 1
    assert first.stopped
    assert sleeps == [provider._TRANSIENT_BACKOFF_SEC]


class _StubResult:
    ErrorCode = 0

    def __init__(self, fields, values):
        self.Fields = fields
        self.Data = [[values.get(field)] for field in fields]


class _StubWind:
    """按字段字典应答 wss 的假 Wind; 记录实际请求过的字段."""

    def __init__(self, values):
        self.values = values
        self.requested_fields = None

    def wss(self, code, fields, options):
        requested = fields.split(",")
        self.requested_fields = requested
        return _StubResult(requested, self.values)


def _terms_with(monkeypatch, values):
    provider = WindDataProvider()
    fake = _StubWind(values)
    monkeypatch.setattr(provider, "_ensure", lambda: fake)
    return provider.get_bond_terms("113001.SH", date(2026, 8, 20)), fake


def test_issue_date_uses_carrydate_not_ipo_date(monkeypatch):
    """发行日/起息日取 carrydate; ipo_date 是上市首日, 只喂 listing_date.

    实测 400 只可交易公开转债: 到期日 100% 对齐 carrydate 的整周年,
    0% 对齐 ipo_date, 两者中位差 25 天 — 用 ipo_date 会整体错开票息期。
    """
    terms, fake = _terms_with(monkeypatch, {
        "sec_name": "南航转债",
        "carrydate": datetime(2020, 10, 15),
        "issue_firstissue": datetime(2020, 10, 15),
        "ipo_date": datetime(2020, 11, 3),
        "maturitydate": datetime(2026, 10, 15),
    })

    assert "carrydate" in fake.requested_fields
    assert terms.issue_date == date(2020, 10, 15)
    assert terms.listing_date == date(2020, 11, 3)


def test_issue_date_falls_back_to_first_issue_then_ipo_date(monkeypatch):
    terms, _ = _terms_with(monkeypatch, {
        "issue_firstissue": datetime(2019, 2, 27),
        "ipo_date": datetime(2019, 3, 20),
    })
    assert terms.issue_date == date(2019, 2, 27)

    terms, _ = _terms_with(monkeypatch, {"ipo_date": datetime(2019, 3, 20)})
    assert terms.issue_date == date(2019, 3, 20)


def test_unlisted_new_bond_keeps_issue_date_without_ipo_date(monkeypatch):
    """已发行未上市的新债: Wind 无 ipo_date, 但 carrydate 有值 → 仍可定价."""
    terms, _ = _terms_with(monkeypatch, {
        "sec_name": "震裕转02",
        "carrydate": datetime(2026, 8, 17),
        "issue_firstissue": datetime(2026, 8, 17),
        "ipo_date": None,
        "maturitydate": datetime(2032, 8, 17),
    })

    assert terms.issue_date == date(2026, 8, 17)
    assert terms.listing_date is None


def test_drop_sentinel_date_filters_wind_far_future_placeholder():
    """Wind 对"最后交易日尚未确定"的存续券返回 2079-06-02 哨兵而非空值。

    原样写回会给 1000+ 只券种上 2079 年的假日期, 让 last_trading_date 的覆盖率
    看起来是满的却毫无意义; 而真实的摘牌安排 (含存续券的预定摘牌日=到期日) 必须保留。
    """
    from convertible_bond.data_providers.wind import _drop_sentinel_date

    val = date(2026, 8, 21)
    assert _drop_sentinel_date(date(2079, 6, 2), val) is None      # 哨兵
    assert _drop_sentinel_date(date(2026, 7, 21), val) == date(2026, 7, 21)  # 已摘牌
    assert _drop_sentinel_date(date(2032, 1, 16), val) == date(2032, 1, 16)  # 预定摘牌=到期
    assert _drop_sentinel_date(None, val) is None
    assert _drop_sentinel_date(date(2079, 6, 2), None) == date(2079, 6, 2)   # 无估值日不判
