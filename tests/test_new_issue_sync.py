"""cb-sync-new-issues: 新债上市日的窄同步.

背景 (为什么不复用 cb-sync-tradable): ``listing_date`` 只有全量条款同步一条写入通道, 而
GUI 的「扫新债」原本靠 ``bundle_meta()['updated_at']`` 判新鲜度 —— 那个时间戳被**任何**写盘
刷新, 于是提示永不弹出; 即便弹出, ``--incremental`` 又按 7 天新鲜度**恰好跳过**刚被抓过的
新债 (实测 2026-08-25 的库: 跳过 4 只新债、真取 741 只无关的)。详见
``convertible_bond/new_issue_sync.py`` 的模块 docstring。
"""
from datetime import date

import pytest

from convertible_bond import new_issue_sync as mod
from convertible_bond.cache import TermsBundle
from convertible_bond.data_providers import BondTerms, is_issued_pending_listing

ON_DATE = date(2026, 8, 25)


@pytest.fixture()
def bundle_path(tmp_path):
    path = tmp_path / "cb_data.json"
    bundle = TermsBundle(path)
    rows = {
        # 已发行未上市 —— 目标集主力
        "123284.SZ": BondTerms(sec_name="强达转债", issue_date=date(2026, 8, 19)),
        # 上市日刚公告、本地还没有
        "118076.SH": BondTerms(sec_name="先锋转债", issue_date=date(2026, 8, 6)),
        # 刚挂牌 1 天, 继续盯
        "123281.SZ": BondTerms(sec_name="中仑转债", issue_date=date(2026, 8, 6),
                               listing_date=date(2026, 8, 24)),
        # 普通存续券 —— 不该进目标集
        "128044.SZ": BondTerms(sec_name="岭南转债", issue_date=date(2019, 1, 1),
                               listing_date=date(2019, 1, 20)),
        # 缓存里留着陈旧的派生状态 (pending/False), 但它 2019 年就上市了
        "128099.SZ": BondTerms(sec_name="陈旧标签转债", issue_date=date(2019, 1, 1),
                               listing_date=date(2019, 2, 1),
                               trading_status="pending", is_tradable=False),
        # 撤销发行、从未上市: 起息日过去 5 年, 早出了 UNLISTED_MAX_DAYS 窗口
        "123095.SZ": BondTerms(sec_name="日升转债", issue_date=date(2021, 1, 1)),
    }
    for code, terms in rows.items():
        bundle.set(code, terms, source="Wind")
    return path


@pytest.fixture()
def listings():
    return {
        "123284": {"sec_name": "强达转债", "issue_date": date(2026, 8, 19),
                   "listing_date": None, "underlying_code": "301628",
                   "underlying_name": "强达电路", "conversion_price": 84.04,
                   "credit_rating": "AA-"},
        "118076": {"sec_name": "先锋转债", "issue_date": date(2026, 8, 6),
                   "listing_date": date(2026, 8, 26), "underlying_code": "688159",
                   "underlying_name": "有方科技", "conversion_price": 86.4,
                   "credit_rating": "AA"},
        # 远端这一格是空的 —— 不能拿去清掉本地已有的上市日
        "123281": {"sec_name": "中仑转债", "issue_date": date(2026, 8, 6),
                   "listing_date": None, "underlying_code": "301565",
                   "underlying_name": "中仑新材", "conversion_price": 20.28,
                   "credit_rating": "AA-"},
        # 全新代码, 本地没有
        "123285": {"sec_name": "新发转债", "issue_date": date(2026, 8, 20),
                   "listing_date": None, "underlying_code": "300001",
                   "underlying_name": "新发科技", "conversion_price": 12.5,
                   "credit_rating": "A+"},
        # 早已退市的老债: 第三方表里仍在, 本地没有 —— 集合差会把它整批拉回来
        "110002": {"sec_name": "南山转债", "issue_date": date(2010, 4, 1),
                   "listing_date": date(2010, 4, 20), "underlying_code": "600219",
                   "underlying_name": "南山铝业", "conversion_price": 8.0,
                   "credit_rating": "AA"},
    }


# ── 目标集 ────────────────────────────────────────────────
def test_tracking_set_ignores_derived_status_fields(bundle_path):
    """目标集只看 issue_date/listing_date, 不看 trading_status/is_tradable.

    那三个字段是 ``infer_cb_trading_metadata`` 自己上一次的输出 (公募转债数据源根本不提供),
    拿它们选目标就是自我确认: 一只债一旦被判错一次, 就再也不会被重新检查。
    """
    tracked = set(mod.select_tracking_codes(TermsBundle(bundle_path), on_date=ON_DATE))

    assert tracked == {"123284.SZ", "118076.SH", "123281.SZ"}
    # 顶着 pending/False 标签, 但 2019 年就上市了 —— 不能因为标签把它拉进来
    assert "128099.SZ" not in tracked
    # 撤销发行的老债: 缺上市日但早出了 180 天窗口, 不是"新债"
    assert "123095.SZ" not in tracked


# ── 上市日更新 ────────────────────────────────────────────
def test_updates_listing_date_from_third_party(bundle_path, listings):
    mod.sync_new_issues(bundle_path, dry_run=False, on_date=ON_DATE, listings=listings)

    assert TermsBundle(bundle_path).get("118076.SH").listing_date == date(2026, 8, 26)


def test_missing_remote_listing_never_falls_back_to_issue_date(bundle_path, listings):
    """第三方那一格是空的 → 保持 None, 不拿申购日/起息日兜底.

    一个假的上市日会让 ``is_issued_pending_listing`` 直接判成"已上市", 于是还没挂牌的新债
    带着空市价混进主池。(pandas 的 ``NaT`` 是这条路上最容易漏的假值: 它是 datetime 子类且
    ``bool(NaT) is True``, ``x or fallback`` 根本不回落。)
    """
    mod.sync_new_issues(bundle_path, dry_run=False, on_date=ON_DATE, listings=listings)

    terms = TermsBundle(bundle_path).get("123284.SZ")
    assert terms.listing_date is None
    assert is_issued_pending_listing("123284.SZ", terms, ON_DATE)


def test_blank_remote_does_not_clear_local_listing_date(bundle_path, listings):
    """取不到证据 ≠ 证据为否: 远端为空时本地已有的上市日不动."""
    mod.sync_new_issues(bundle_path, dry_run=False, on_date=ON_DATE, listings=listings)

    assert TermsBundle(bundle_path).get("123281.SZ").listing_date == date(2026, 8, 24)


def test_dry_run_does_not_write(bundle_path, listings):
    before = bundle_path.read_text(encoding="utf-8")
    report = mod.sync_new_issues(bundle_path, dry_run=True, on_date=ON_DATE, listings=listings)

    assert report["changes"]
    assert report["applied"] is False
    assert bundle_path.read_text(encoding="utf-8") == before


# ── 写盘语义 ──────────────────────────────────────────────
def test_write_never_touches_the_wind_timestamp_bucket(bundle_path, listings):
    """窄同步的 source 桶必须独立.

    写进 ``Wind`` 那格会同时毒化两个消费者: ``cb-sync-tradable --incremental`` 以为条款刚
    抓过而跳过, ``terms_as_of`` 把条款 patch 按"快照已含"整段裁掉。
    """
    bundle = TermsBundle(bundle_path)
    wind_before = bundle.fetched_at("118076.SH", source="Wind")

    mod.sync_new_issues(bundle_path, dry_run=False, on_date=ON_DATE, listings=listings)

    after = TermsBundle(bundle_path)
    assert after.fetched_at("118076.SH", source="Wind") == wind_before
    assert after.fetched_at("118076.SH", source=mod.NEW_ISSUE_SOURCE) is not None


def test_fills_only_empty_metadata_fields(bundle_path, listings):
    """除上市日外一律"只填空": Wind 才是这些字段的权威源."""
    listings = {**listings, "123281": {**listings["123281"], "issue_date": date(1999, 1, 1)}}
    mod.sync_new_issues(bundle_path, dry_run=False, on_date=ON_DATE, listings=listings)

    terms = TermsBundle(bundle_path).get("123281.SZ")
    assert terms.issue_date == date(2026, 8, 6)          # 本地已有, 不被覆盖
    assert terms.underlying_code == "301565.SZ"          # 本地为空, 补齐


# ── 发现全新代码 ──────────────────────────────────────────
def test_discovery_uses_subscribe_window_not_set_difference(bundle_path, listings):
    """按申购日期窗口发现新债, 不能用"整表 - 本地"的集合差.

    实测第三方表含 110002 这类早已退市的老债, 有 76 个不在 cb_data 里 —— 集合差会把它们
    整批拉回库中。
    """
    discovered = mod.discover_new_codes(
        TermsBundle(bundle_path), listings, on_date=ON_DATE)

    assert discovered == ["123285.SZ"]


def test_discovered_stub_is_visible_but_still_pending(bundle_path, listings):
    """占位档要让新债"看得见"、同时仍被判成还没挂牌."""
    mod.sync_new_issues(bundle_path, dry_run=False, on_date=ON_DATE, listings=listings)

    terms = TermsBundle(bundle_path).get("123285.SZ")
    assert terms.sec_name == "新发转债"
    assert terms.underlying_code == "300001.SZ"
    assert terms.issue_date == date(2026, 8, 20)         # 申购日 == 起息日 (全库实测 974/974)
    assert terms.conversion_price == 12.5
    assert is_issued_pending_listing("123285.SZ", terms, ON_DATE)
    # 占位档没有 Wind 那格时间戳 → 下一次 --incremental 一定会去权威建档, 无需迁移脚本
    assert TermsBundle(bundle_path).is_stale("123285.SZ", 7, source="Wind")


# ── 解析防线 ──────────────────────────────────────────────
def test_pandas_nat_is_not_a_date():
    """``pandas.NaT`` 是 datetime 子类且为真值, 通用 ``to_date`` 会把它原样放行."""
    pd = pytest.importorskip("pandas")

    assert mod.safe_date(pd.NaT) is None
    assert mod.safe_date(float("nan")) is None
    assert mod.safe_date("--") is None
    assert mod.safe_date("2026-08-26") == date(2026, 8, 26)


def test_empty_third_party_table_refuses_to_change_anything(bundle_path):
    """一行都没解析出来时宁可不动库 —— 空表不是"所有新债都没上市日"的证据."""
    before = bundle_path.read_text(encoding="utf-8")
    report = mod.sync_new_issues(bundle_path, dry_run=False, on_date=ON_DATE, listings={})

    assert report["changes"] == []
    assert bundle_path.read_text(encoding="utf-8") == before
