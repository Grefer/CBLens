"""cb-sync-ratings: 信用评级改由第三方 (akshare) 定当前值。

为什么不是 Wind: 它的 ``creditrating`` 是**发行时值**, 实测 cb_data.json 跨 17 个版本、
约 4000 次逐债重取零变化 (同批刷新里 conversion_price 变了 287 次), 已违约的搜特/鸿达/正邦
仍标 AA。评级经 ``pricing_api._rating_spread_floor`` 直接变成 pricer 的信用利差下限
(AA 2.50% ↔ C 80.00%), 陈旧的 AA 会让困境债的理论价被系统性高估。
"""
from datetime import date

import pytest

from convertible_bond.cache import TermsBundle
from convertible_bond.cli import sync_ratings as mod
from convertible_bond.data_providers import BondTerms


@pytest.fixture()
def bundle_path(tmp_path):
    path = tmp_path / "cb_data.json"
    bundle = TermsBundle(path)
    for code, name, rating in [
        ("128100.SZ", "搜特转债(退市)", "AA"),      # 已违约却标 AA
        ("123229.SZ", "艾录转债", "A+"),            # 常规年度下调
        ("110085.SH", "通22转债", "AA+"),           # 第三方更高
        ("110001.SH", "无第三方转债", "AA"),         # 第三方没覆盖 → 不动
    ]:
        bundle.set(code, BondTerms(sec_name=name, underlying_code="000001.SZ",
                                   conversion_price=10.0, maturity_date=date(2030, 1, 1),
                                   credit_rating=rating), source="unit")
    return path


@pytest.fixture()
def fake_third_party(monkeypatch):
    monkeypatch.setattr(mod, "fetch_third_party_ratings", lambda: {
        "128100": "CC", "123229": "A", "110085": "AAA",
    })


def test_dry_run_reports_changes_without_writing(bundle_path, fake_third_party):
    before = bundle_path.read_text(encoding="utf-8")
    report = mod.sync_ratings(bundle_path, dry_run=True)

    rows = {r["bond_code"]: r for r in report["changes"]}
    assert set(rows) == {"128100.SZ", "123229.SZ", "110085.SH"}
    assert rows["128100.SZ"]["notches"] == -15      # AA → CC
    assert rows["123229.SZ"]["notches"] == -1
    assert rows["110085.SH"]["notches"] == +1       # 上调也要跟
    assert bundle_path.read_text(encoding="utf-8") == before


def test_apply_writes_third_party_value(bundle_path, fake_third_party):
    mod.sync_ratings(bundle_path, dry_run=False)

    bundle = TermsBundle(bundle_path)
    assert bundle.get("128100.SZ").credit_rating == "CC"
    assert bundle.get("110085.SH").credit_rating == "AAA"
    # 第三方没覆盖的保持原值, 不能被清空 —— 宁可陈旧也不能变 None
    assert bundle.get("110001.SH").credit_rating == "AA"


def test_apply_is_idempotent(bundle_path, fake_third_party):
    mod.sync_ratings(bundle_path, dry_run=False)
    assert mod.sync_ratings(bundle_path, dry_run=False)["changes"] == []


def test_empty_third_party_refuses_to_touch_the_library(monkeypatch, bundle_path):
    """上游改字段名/返回空表时必须报错, 不能把全库评级判成"无变化"后静默放行。"""
    import pandas as pd
    monkeypatch.setattr("akshare.bond_zh_cov",
                        lambda *a, **k: pd.DataFrame({"债券代码": [], "信用评级": []}))
    with pytest.raises(RuntimeError):
        mod.fetch_third_party_ratings()


def test_missing_rating_column_is_an_error(monkeypatch):
    import pandas as pd
    monkeypatch.setattr("akshare.bond_zh_cov",
                        lambda *a, **k: pd.DataFrame({"债券代码": ["128100"]}))
    with pytest.raises(RuntimeError):
        mod.fetch_third_party_ratings()
