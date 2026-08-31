"""全量条款同步不许清空别的来源写入的字段.

``TermsBundle.set_many`` 是**整条记录替换**, 而 Wind 的 ``get_bond_terms`` 只覆盖条款 ——
所以每一次 ``cb-sync-tradable`` 都会把 ``get_admission_status`` / ``cb_events`` /
``cb-sync-ratings`` / ``cb-sync-new-issues`` 写入的字段整批清成 null。

实测代价 (2026-08-30 的 data/cb_data.json): 主池 284 只债的 ``underlying_status`` /
``underlying_trade_status`` / ``underlying_pct_change`` / ``suspension_status`` /
``bond_turnover_amount`` / ``delisting_date`` / ``last_trading_date`` 全部 **0/284**,
``underlying_name`` 只剩 3/284; 而 775 只死券保留着旧值 (``list_tradable_cbs`` 够不着
它们) —— 于是任何按全库统计的覆盖率都看不出问题。后果是「正股 ST/退市风险」「正股停牌」
「正股跌停」「转债停牌」「成交额过低」「临近摘牌」六条准入判据对主池全部无输入、恒为 False。
"""
from __future__ import annotations

import ast
import inspect
import textwrap
from dataclasses import fields
from dataclasses import replace
from datetime import date

from convertible_bond import cb_data_sync
from convertible_bond.cache import TermsBundle
from convertible_bond.cb_data_sync import refresh_one, sync_cb_terms
from convertible_bond.data_providers import BondTerms
from convertible_bond.data_providers.wind import WindDataProvider


class _Wind:
    """只返回条款的 provider —— 与真实 ``WindDataProvider.get_bond_terms`` 同形。"""

    name = "Wind"

    def __init__(self, **terms_overrides):
        self._overrides = terms_overrides

    def authoritative_terms_fields(self):
        return WindDataProvider._TERMS_FIELDS_WRITTEN

    def get_bond_terms(self, code, valuation_date):
        base = dict(
            sec_name="帝欧转债",
            conversion_price=10.0,
            maturity_date=date(2030, 1, 1),
            credit_rating="AA-",          # 发行时冻结值
            close=118.0,
            is_tradable=True,             # infer_cb_trading_metadata 的新推断
            trading_status="正常",
        )
        base.update(self._overrides)
        return BondTerms(**base)

    def get_cashflow(self, code):
        return None


def _stored_with_status(tmp_path) -> TermsBundle:
    """一份"日常状态刷新 + 事件回写之后"的库。"""
    bundle = TermsBundle(tmp_path / "cb_data.json")
    bundle.set("127047.SZ", BondTerms(
        sec_name="帝欧转债",
        credit_rating="BBB+",                       # akshare:ratings 刷来的当前值
        underlying_name="帝欧家居",                  # ← 以下全部只由状态刷新/事件写入
        underlying_status="ST",
        underlying_trade_status="停牌",
        underlying_pct_change=-9.98,
        suspension_status="停牌",
        bond_turnover_amount=1234.0,
        delisting_date=date(2026, 9, 30),
        last_trading_date=date(2026, 9, 20),
        call_status="已公告强赎",
        conversion_suspension_status="暂停转股",
    ), source="Wind:admission_status")
    return bundle


def test_full_sync_keeps_every_field_wind_does_not_return(tmp_path):
    """全量同步只许覆盖 Wind 真正返回的字段, 其余保本地值。"""
    bundle = _stored_with_status(tmp_path)

    sync_cb_terms(_Wind(), ["127047.SZ"], store=bundle,
                  valuation_date=date(2026, 8, 30), drop_terminal=False)

    got = bundle.get("127047.SZ")
    # 状态字段一个都不许丢 —— 这些是全量同步清空过的那一批
    assert got.underlying_name == "帝欧家居"
    assert got.underlying_status == "ST"
    assert got.underlying_trade_status == "停牌"
    assert got.underlying_pct_change == -9.98
    assert got.suspension_status == "停牌"
    assert got.bond_turnover_amount == 1234.0
    assert got.delisting_date == date(2026, 9, 30)
    assert got.last_trading_date == date(2026, 9, 20)
    assert got.call_status == "已公告强赎"
    assert got.conversion_suspension_status == "暂停转股"
    # 评级仍由第三方说了算 (Wind 的是发行时冻结值)
    assert got.credit_rating == "BBB+"
    # 而 Wind 真正拥有的字段照常被重建
    assert got.conversion_price == 10.0
    assert got.maturity_date == date(2030, 1, 1)
    assert got.close == 118.0


def test_full_sync_still_lets_wind_seed_empty_fields(tmp_path):
    """本地为空时 Wind 照常兜底建档 —— 保的是"已有值", 不是"这个字段"。"""
    bundle = TermsBundle(tmp_path / "cb_data.json")
    bundle.set("999999.SZ", BondTerms(sec_name="新债"), source="unit")

    sync_cb_terms(_Wind(), ["999999.SZ"], store=bundle,
                  valuation_date=date(2026, 8, 30), drop_terminal=False)

    assert bundle.get("999999.SZ").credit_rating == "AA-"


def test_derived_trading_metadata_must_not_be_preserved(tmp_path):
    """``is_tradable`` / ``trading_status`` 必须让**新推断**胜出。

    这两个字段数据源根本不提供, 缓存里读到的只可能是上一次 ``infer_cb_trading_metadata``
    的输出。保住旧值就是 AGENTS.md 记的自我确认陷阱 —— 债在"已发行未上市"时留下的
    ``pending`` / ``False`` 在真的挂牌后永远翻不回来 (实测派克/中仑两只上市首日分别成交
    2.57 亿 / 12.95 亿的新债被准入判成"不可交易")。
    """
    bundle = TermsBundle(tmp_path / "cb_data.json")
    bundle.set("123456.SZ", BondTerms(
        sec_name="新债", is_tradable=False, trading_status="已发行未上市",
    ), source="Wind")

    sync_cb_terms(_Wind(), ["123456.SZ"], store=bundle,
                  valuation_date=date(2026, 8, 30), drop_terminal=False)

    got = bundle.get("123456.SZ")
    assert got.is_tradable is True
    assert got.trading_status == "正常"


def test_refresh_one_protects_the_same_fields(tmp_path):
    """单只刷新落盘同样是整条替换 —— 少了这道闸就是"刷一只清一只"。"""
    bundle = _stored_with_status(tmp_path)

    refresh_one(_Wind(), "127047.SZ", store=bundle, valuation_date=date(2026, 8, 30))

    got = bundle.get("127047.SZ")
    assert got.underlying_status == "ST"
    assert got.credit_rating == "BBB+"
    assert got.conversion_price == 10.0      # Wind 拥有的照常刷新


def test_nan_local_value_does_not_count_as_a_value(tmp_path):
    """NaN 不算"本地已有值" —— ``NaN is not None`` 为真, 用 ``not in (None, "")`` 会保住它。"""
    bundle = TermsBundle(tmp_path / "cb_data.json")
    bundle.set("127047.SZ", BondTerms(sec_name="帝欧转债",
                                      underlying_pct_change=float("nan")),
               source="Wind:admission_status")

    fresh = BondTerms(sec_name="帝欧转债", underlying_pct_change=None)
    protected = cb_data_sync.locally_authoritative_fields(_Wind())
    merged = cb_data_sync._keep_locally_authoritative(
        bundle, "127047.SZ", fresh, protected)

    assert merged.underlying_pct_change is None


def test_has_local_value_does_not_broadcast_on_numpy_scalars():
    """判空不许用 ``value == ()`` —— numpy 标量会走广播, ``if`` 上去抛 ValueError.

    调用点在 ``sync_cb_terms`` 的逐只循环里, 而 ``fresh_items`` 要跑完整轮才落盘 ——
    一只债炸掉就把前面几百次 Wind 调用的结果一起丢掉。
    """
    import numpy as np

    assert cb_data_sync._has_local_value(np.float64(0.0)) is True
    assert cb_data_sync._has_local_value(np.float64("nan")) is False
    # 0 / 0.0 / False 都是**有效值**, 不能当空
    assert cb_data_sync._has_local_value(0) is True
    assert cb_data_sync._has_local_value(0.0) is True
    assert cb_data_sync._has_local_value(False) is True
    # 真正的空
    assert cb_data_sync._has_local_value(None) is False
    assert cb_data_sync._has_local_value("") is False
    assert cb_data_sync._has_local_value(()) is False


def test_locally_authoritative_set_is_derived_not_hand_registered():
    """保护集必须是**推导**出来的, 而且要真的覆盖到准入状态字段。

    手工登记正是漏掉一大批字段的原因: 上一版这张表只有 ``credit_rating`` 一个。
    """
    from convertible_bond.admission_status import ADMISSION_STATUS_FIELDS

    protected = set(cb_data_sync.locally_authoritative_fields(_Wind()))

    # 除了 Wind 自己就返回的那几个, 准入状态字段必须全在保护集里
    wind_owned = WindDataProvider._TERMS_FIELDS_WRITTEN
    for name in ADMISSION_STATUS_FIELDS:
        if name in wind_owned and name != "credit_rating":
            continue
        assert name in protected, f"准入状态字段 {name} 不在保护集里, 全量同步会清空它"

    # 且不许把 Wind 真正拥有的条款字段也保下来 (那会让条款永远刷不新)
    assert "conversion_price" not in protected
    assert "maturity_date" not in protected
    # infer_cb_trading_metadata 的**三个**输出都不许保 —— 少一个就是自我确认陷阱
    for derived in ("tradable_date", "is_tradable", "trading_status"):
        assert derived not in protected, f"{derived} 是推断产物, 保住它等于自我确认"


def test_the_real_wind_provider_declares_its_field_ownership():
    """必须断言**真的** WindDataProvider 声明了所有权, 不能只测替身。

    本文件的 ``_Wind`` 替身自己实现了 ``authoritative_terms_fields``, 所以真 provider
    把这个方法删掉/改名时上面那些用例照样全绿 —— 实测这个变异体确实活了下来。
    而真 provider 不声明就退回 ABC 默认 (None = 全字段权威), 于是每次
    ``cb-sync-tradable`` 又开始清空状态字段, 且完全静默。
    """
    provider = WindDataProvider()          # __init__ 不连接 Wind, 只置字段

    assert provider.authoritative_terms_fields() == WindDataProvider._TERMS_FIELDS_WRITTEN
    protected = set(cb_data_sync.locally_authoritative_fields(provider))
    assert "underlying_status" in protected and "last_trading_date" in protected
    assert "conversion_price" not in protected


def test_protection_is_scoped_to_the_provider_not_global():
    """保护集按 provider 问, 不是一张全局常量表。

    那张表是从 Wind 抄来的; 套在写满全部字段的 ``CSVDataProvider`` 上会让它的导入
    静默失效 —— 26 个字段改不动, 而 ``result['success']`` 照常 +1。
    """
    class _WritesEverything:
        name = "csv"

        def get_bond_terms(self, code, valuation_date):
            return BondTerms(sec_name="x", conversion_price=1.0)

        def get_cashflow(self, code):
            return None

    # 没声明 → 退回旧行为, 只保 credit_rating
    assert cb_data_sync.locally_authoritative_fields(_WritesEverything()) == ("credit_rating",)
    # Wind 声明了 → 保护集是它不写的那些
    assert len(cb_data_sync.locally_authoritative_fields(_Wind())) > 10


def test_csv_provider_import_is_not_silently_neutered(tmp_path):
    """走非 Wind provider 时, 它写的字段必须真的落盘。"""
    class _Csv:
        name = "csv"

        def get_bond_terms(self, code, valuation_date):
            return BondTerms(sec_name="帝欧转债", conversion_price=10.0,
                             maturity_date=date(2030, 1, 1),
                             putback_price=103.0, underlying_status="ST")

        def get_cashflow(self, code):
            return None

    bundle = TermsBundle(tmp_path / "cb_data.json")
    bundle.set("127047.SZ", BondTerms(sec_name="帝欧转债", putback_price=100.0,
                                      underlying_status="旧ST"), source="unit")

    sync_cb_terms(_Csv(), ["127047.SZ"], store=bundle,
                  valuation_date=date(2026, 8, 30), drop_terminal=False)

    got = bundle.get("127047.SZ")
    assert got.putback_price == 103.0
    assert got.underlying_status == "ST"


def test_wind_terms_field_whitelist_matches_the_provider():
    """白名单必须跟得上 ``WindDataProvider.get_bond_terms`` 实际写入的字段。

    这是这套推导的唯一活动部件: wind.py 多写一个字段而白名单没跟上, 表现是那个字段
    **永远刷不新** (被当成本地权威保下来), 而且不报错。用 ast 直接读构造现场, 不靠人记。
    """
    from convertible_bond.data_providers import wind as wind_mod

    src = textwrap.dedent(inspect.getsource(wind_mod.WindDataProvider.get_bond_terms))
    tree = ast.parse(src)
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name) and node.func.id == "BondTerms"
    ]
    # **不用 next()** —— 构造点被改名/别名导入/抽成 helper 时, next() 抛 StopIteration
    # 而不是给出可读的失败原因, 那种"测试炸了"和"测试红了"读起来完全不同。
    assert len(calls) == 1, (
        f"在 wind.py 的 get_bond_terms 里找到 {len(calls)} 处 BondTerms(...) 构造 (期望 1) —— "
        f"构造点若被改名或抽成 helper, 本守护测试就看不见它写了哪些字段, 请同步改这里")
    constructed = {kw.arg for kw in calls[0].keywords if kw.arg}

    owned = wind_mod.WindDataProvider._TERMS_FIELDS_WRITTEN
    missing = constructed - owned
    assert not missing, (
        f"wind.py 的 get_bond_terms 新写了字段 {sorted(missing)}, "
        f"但 _TERMS_FIELDS_WRITTEN 没跟上 —— 这些字段会被当成本地权威, 永远刷不新")

    # 表里多出来的只允许是 infer_cb_trading_metadata 的**三个**输出
    # (base.py 的 replace(terms, tradable_date=..., is_tradable=..., trading_status=...))
    assert owned - constructed == {"tradable_date", "is_tradable", "trading_status"}

    # 表里的名字必须都是 BondTerms 真实字段 (拼错会静默扩大保护集)
    assert owned <= {f.name for f in fields(BondTerms)}


def test_infer_cb_trading_metadata_outputs_are_all_accounted_for():
    """``infer_cb_trading_metadata`` 写几个字段, ``_TERMS_FIELDS_WRITTEN`` 就得排除几个。

    上一版只列了 ``is_tradable`` / ``trading_status``, 漏掉 ``tradable_date`` ——
    于是那个字段被冻结成"上一次推断的产物", 而守护测试还把这个遗漏钉死了。
    """
    from convertible_bond.data_providers import base as base_mod

    src = textwrap.dedent(inspect.getsource(base_mod.infer_cb_trading_metadata))
    tree = ast.parse(src)
    written = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "replace"):
            written |= {kw.arg for kw in node.keywords if kw.arg}

    assert written == {"tradable_date", "is_tradable", "trading_status"}
    assert written <= WindDataProvider._TERMS_FIELDS_WRITTEN, (
        f"{sorted(written - WindDataProvider._TERMS_FIELDS_WRITTEN)} 是推断产物却被保护了")


def test_preserve_local_false_restores_the_eraser(tmp_path):
    """``preserve_local=False`` 必须真的把 provider 不写的字段擦成 None。

    这条路不是可有可无的对称性: ``apply_events_to_terms`` 与 ``merge_admission_status``
    对 ``call_*`` / ``last_trading_date`` / ``putback_*`` **只写不撤**, 实测
    ``last_trading_date`` 在 342 只在市未到期债上 Wind 一只都不返回 —— 整条替换是它仅有的
    橡皮擦。``cli/repair_events`` 的流程说明 ("改完事件表还要把 cb_data 的状态字段恢复成
    Wind 口径再重放") 指的就是它; 没有这个开关那句话就没有实现。
    """
    bundle = _stored_with_status(tmp_path)
    bundle.set("127047.SZ", replace(bundle.get("127047.SZ"),
                                    last_trading_date=date(2024, 12, 12)),
               source="cb_events")

    sync_cb_terms(_Wind(), ["127047.SZ"], store=bundle,
                  valuation_date=date(2026, 8, 30), drop_terminal=False,
                  preserve_local=False)

    got = bundle.get("127047.SZ")
    assert got.last_trading_date is None
    assert got.call_status is None
    assert got.underlying_status is None
    # 连 credit_rating 也一起擦 —— 这就是"恢复成数据源口径"的字面意思
    assert got.credit_rating == "AA-"
