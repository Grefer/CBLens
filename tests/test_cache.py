"""``convertible_bond.cache`` 测试.

重点覆盖反射式 ``_json_dict_to_terms`` (类型注解驱动的反序列化):
任何 ``BondTerms`` 字段类型注解出错或字段名 typo 都是 silent corruption,
这些测试是最划算的"投资性测试"。
"""
from datetime import date, datetime
from dataclasses import fields

import pytest

from convertible_bond.cache import (
    TermsBundle,
    _DATE_FIELD_NAMES,
    _TUPLE_FIELD_NAMES,
    _json_dict_to_terms,
    _terms_to_json_dict,
)
from convertible_bond.data_providers import BondTerms


def _sample_terms() -> BondTerms:
    """覆盖每种字段类型的样本 BondTerms."""
    return BondTerms(
        sec_name="测试转债",
        underlying_code="000001.SZ",
        issue_date=date(2024, 1, 5),
        listing_date=date(2024, 1, 20),
        tradable_date=date(2024, 7, 20),
        is_tradable=True,
        trading_status="tradable",
        maturity_date=date(2030, 1, 5),
        face_value=100.0,
        conversion_price=15.5,
        redemption_price=110.0,
        call_trigger_pct=130.0,
        put_trigger_pct=70.0,
        put_obs_months=24.0,
        down_reset_block_until=date(2026, 10, 15),
        down_reset_p_scale=0.8,
        down_reset_note="不下修承诺",
        down_reset_cooldown_months=6.0,
        coupon_rates=(0.003, 0.005, 0.01, 0.015, 0.018, 0.02),
        close=112.5,
        credit_rating="AA",
        outstanding_balance=4.2,
        suspension_status=None,
        call_status="不强赎",
        call_announce_date=date(2026, 4, 15),
        call_redemption_date=None,
        call_no_redemption_until=date(2026, 10, 15),
        last_trading_date=None,
        delisting_date=None,
        underlying_name="测试股份",
        underlying_status=None,
        underlying_trade_status=None,
        underlying_pct_change=-2.5,
        bond_turnover_amount=1.2e8,
    )


def test_date_and_tuple_field_sets_are_populated_from_annotations():
    """反射出来的 date / tuple 字段集应非空 — 防止类型注解写错被 silent skip."""
    assert "issue_date" in _DATE_FIELD_NAMES
    assert "maturity_date" in _DATE_FIELD_NAMES
    assert "down_reset_block_until" in _DATE_FIELD_NAMES
    assert "call_announce_date" in _DATE_FIELD_NAMES
    assert "coupon_rates" in _TUPLE_FIELD_NAMES


def test_terms_round_trip_preserves_dates_and_tuples():
    terms = _sample_terms()
    d = _terms_to_json_dict(terms)
    # 序列化后 date 应该是 ISO string, tuple 应该是 list (JSON 不支持 tuple)
    assert d["issue_date"] == "2024-01-05"
    assert d["coupon_rates"] == [0.003, 0.005, 0.01, 0.015, 0.018, 0.02]
    assert isinstance(d["coupon_rates"], list)

    restored = _json_dict_to_terms(d)
    # 反序列化后所有字段应与原对象逐一相等
    assert restored == terms
    assert isinstance(restored.issue_date, date)
    assert isinstance(restored.coupon_rates, tuple)
    assert all(isinstance(x, float) for x in restored.coupon_rates)


def test_json_dict_to_terms_handles_missing_fields_as_defaults():
    """JSON 缺字段时应回到 dataclass 默认值, 不抛."""
    sparse = {"sec_name": "稀疏转债", "conversion_price": 12.0}
    restored = _json_dict_to_terms(sparse)
    assert restored.sec_name == "稀疏转债"
    assert restored.conversion_price == 12.0
    # 其它字段保持默认 None
    assert restored.issue_date is None
    assert restored.coupon_rates is None
    assert restored.is_tradable is None


def test_json_dict_to_terms_ignores_unknown_keys():
    """额外 _meta / 未知字段不应导致反序列化失败."""
    d = _terms_to_json_dict(_sample_terms())
    d["_meta"] = {"fetched_at": "2026-04-15T10:00:00", "source": "wind"}
    d["__legacy_field__"] = "ignore me"
    restored = _json_dict_to_terms(d)
    assert restored.sec_name == "测试转债"


def test_json_dict_to_terms_accepts_string_dates_and_list_tuples():
    """直接构造 dict (绕过 _terms_to_json_dict) 模拟跨设备读取."""
    payload = {
        "sec_name": "兼容测试",
        "issue_date": "2024-01-05",
        "maturity_date": "2030-01-05",
        "coupon_rates": [0.003, 0.005],
    }
    restored = _json_dict_to_terms(payload)
    assert restored.issue_date == date(2024, 1, 5)
    assert restored.maturity_date == date(2030, 1, 5)
    assert restored.coupon_rates == (0.003, 0.005)


def test_json_dict_to_terms_preserves_none_for_optional_dates():
    """显式 None 不应被 to_date 误转成今天/异常."""
    payload = {
        "sec_name": "可空字段",
        "issue_date": None,
        "coupon_rates": None,
    }
    restored = _json_dict_to_terms(payload)
    assert restored.issue_date is None
    assert restored.coupon_rates is None


def test_all_dataclass_fields_round_trip():
    """每个字段都应该能通过 round-trip 保持值 — 防止漏注解.

    实现逻辑: 给每个字段塞一个非默认的 sentinel 值, dump+load 后必须相等。
    """
    sentinels: dict[str, object] = {}
    for f in fields(BondTerms):
        # 取出原始注解字符串里的"主类型" (跳过 None)
        ann = str(f.type)
        if "date" in ann:
            sentinels[f.name] = date(2025, 6, 1)
        elif "tuple" in ann or "Tuple" in ann:
            sentinels[f.name] = (0.01, 0.02)
        elif "bool" in ann:
            sentinels[f.name] = True
        elif "float" in ann:
            sentinels[f.name] = 3.14
        elif "int" in ann:
            sentinels[f.name] = 42
        else:
            sentinels[f.name] = "sentinel"
    terms = BondTerms(**sentinels)    # type: ignore[arg-type]
    restored = _json_dict_to_terms(_terms_to_json_dict(terms))
    assert restored == terms


def test_terms_bundle_round_trip_through_disk(tmp_path):
    bundle = TermsBundle(tmp_path / "cb_data.json")
    terms = _sample_terms()
    bundle.set("128009.SZ", terms, source="unit-test")

    reloaded = TermsBundle(tmp_path / "cb_data.json")
    assert reloaded.has("128009.SZ")
    assert reloaded.get("128009.SZ") == terms
    # bundle_meta 应有 updated_at + n_bonds
    meta = reloaded.bundle_meta()
    assert meta["n_bonds"] == 1
    assert "updated_at" in meta


def test_terms_bundle_set_many_writes_once(tmp_path, monkeypatch):
    """set_many 应只刷盘一次, 不论传多少条."""
    bundle = TermsBundle(tmp_path / "cb_data.json")
    save_calls = []
    real_save = bundle._save

    def counting_save():
        save_calls.append(None)
        real_save()

    monkeypatch.setattr(bundle, "_save", counting_save)
    items = [
        ("128001.SZ", BondTerms(sec_name="债 A")),
        ("128002.SZ", BondTerms(sec_name="债 B")),
        ("128003.SZ", BondTerms(sec_name="债 C")),
    ]
    bundle.set_many(items, source="batch")
    assert len(save_calls) == 1
    assert sorted(bundle.list_bonds()) == ["128001.SZ", "128002.SZ", "128003.SZ"]


def test_terms_bundle_is_stale_uses_fetched_at(tmp_path):
    bundle = TermsBundle(tmp_path / "cb_data.json")
    bundle.set("128009.SZ", _sample_terms(), source="fresh")
    assert not bundle.is_stale("128009.SZ", max_age_days=30)
    # 不存在的债视为 stale
    assert bundle.is_stale("999999.SZ", max_age_days=30)
    fetched = bundle.fetched_at("128009.SZ")
    assert isinstance(fetched, datetime)


def test_terms_bundle_delete_removes_entry_and_meta(tmp_path):
    bundle = TermsBundle(tmp_path / "cb_data.json")
    bundle.set("128009.SZ", _sample_terms(), source="unit")
    assert bundle.delete("128009.SZ") is True
    assert bundle.delete("128009.SZ") is False
    assert "128009.SZ" not in bundle.list_bonds()
