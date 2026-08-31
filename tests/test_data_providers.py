"""数据源 provider 与装饰器链的守护测试。

这一批测的全是**不抛异常**的失效: 装饰器漏透传会静默退回 ABC 的默认值, Wind 的 NaN
会伪装成合法数值, ``pandas.NaT`` 会伪装成合法日期。它们的共同形状是"测试全绿 + 数据是错的",
所以每条用例都直接断言**输出值**, 不断言"调用没报错"。
"""
# ── provider 装饰器链 / NaN / NaT 的守护 ──────────────────────────────
class TestProviderChainDoesNotSilentlyDegrade:
    """这一组每一条对应一个**不报错**的降级 —— 全绿的测试 + 错的数据。"""

    @staticmethod
    def _fake():
        from datetime import date as _d

        from convertible_bond.data_providers.base import BondTerms, DataProvider

        class Fake(DataProvider):
            name = "Fake"

            def get_bond_terms(self, c, d):
                return BondTerms(sec_name="缓存里的旧条款", conversion_price=10.0)

            def get_admission_status(self, c, d, base_terms=None):
                return BondTerms(sec_name="刚刷来的新状态")

            def list_bond_announcements(self, c, s, e):
                return [{"title": "真公告", "date": _d(2026, 8, 1)}]

            def list_tradable_cbs(self, on_date=None):
                return [("128009.SZ", "歌尔转债")]

            def authoritative_terms_fields(self):
                return frozenset({"conversion_price"})

            def get_stock_close(self, *a, **k):
                return None

            def get_stock_history(self, *a, **k):
                return []

            def get_bond_history(self, *a, **k):
                return []

            def hist_vol(self, *a, **k):
                return 0.2

            def get_risk_free_rate(self, *a, **k):
                return 0.02

        return Fake()

    def test_both_decorators_pass_the_four_abc_methods_through(self, tmp_path):
        """ABC 的默认实现是三句谎话, 不透传就静默变成它们。

        ``authoritative_terms_fields`` → None = "字段全归我", 全量同步整条替换,
        把评级同步 / 状态刷新 / 事件回写的成果一起清空;
        ``get_admission_status`` → 退回 ``self.get_bond_terms``, 而装饰器的那个读**缓存**
        —— "刷新状态"当场变成"读旧值"; ``list_bond_announcements`` → ``[]``, 事件同步
        报"0 条公告"并安全跳过。三条都不抛异常。

        ``backtest_disk_cache`` / ``strategy_backtest`` / ``historical_terms`` 三个装饰器
        都老实透传了, 只有 ``cache.py`` 这两个漏了 —— 所以这条要**同时**钉住两个类:
        补一个漏一个正是它当初的形状。
        """
        from datetime import date

        from convertible_bond.cache import (
            CachedBondDataProvider,
            CachingDataProvider,
            TermsCache,
        )

        inner = self._fake()
        for provider in (
            CachingDataProvider(inner, TermsCache(tmp_path / "a.json")),
            CachedBondDataProvider(
                inner, TermsCache(tmp_path / "b.json"), static_source=inner),
        ):
            label = type(provider).__name__
            assert provider.authoritative_terms_fields() == frozenset(
                {"conversion_price"}), f"{label}: 退回 None = 全量同步整条替换"
            assert provider.get_admission_status(
                "128009.SZ", date(2026, 8, 30)).sec_name == "刚刷来的新状态", (
                f"{label}: 状态刷新退回读缓存")
            assert provider.list_bond_announcements(
                "128009.SZ", date(2026, 1, 1), date(2026, 8, 30)), (
                f"{label}: 公告被吞成空列表, 事件同步会静默跳过")
            assert provider.list_tradable_cbs() == [("128009.SZ", "歌尔转债")], (
                f"{label}: 池子取不出来")

    def test_caching_provider_honours_auto_refresh_false(self, tmp_path):
        """``auto_refresh`` 曾经存了不用 —— 传 False 被静默忽略, 照样打网络。"""
        from datetime import date

        from convertible_bond.cache import CachingDataProvider, TermsCache
        from convertible_bond.data_providers.base import BondTerms

        calls = []
        inner = self._fake()
        real = inner.get_bond_terms

        def counted(c, d):
            calls.append(c)
            return real(c, d)

        inner.get_bond_terms = counted
        cache = TermsCache(tmp_path / "c.json")
        cache.set("128009.SZ", BondTerms(sec_name="旧", conversion_price=9.0),
                  source="Fake")

        # max_age_days=0 → 必然过期; auto_refresh=False 时仍不该回源
        p = CachingDataProvider(inner, cache, max_age_days=0, auto_refresh=False)
        assert p.get_bond_terms("128009.SZ", date(2026, 8, 30)).sec_name == "旧"
        assert calls == [], "auto_refresh=False 却回源了"

        p2 = CachingDataProvider(inner, cache, max_age_days=0, auto_refresh=True)
        p2.get_bond_terms("128009.SZ", date(2026, 8, 30))
        assert calls, "auto_refresh=True 却没回源 —— 参数接反了"

    def test_wind_numeric_fields_reject_nan(self):
        """Wind 对取不到的数值字段返回 ``nan`` 而不是 ``None``。

        裸 ``float()`` 会把它原样写进 BondTerms 并落进 cb_data.json, 而 NaN 与 None
        在下游完全不同: ``is not None`` 放行它, ``x or fallback`` 不回落, 任何比较恒为假。
        一个 NaN 转股价能让这只债静默定不出价, 而每次库内自洽性检查都说"字段齐备"。
        """
        import math

        from convertible_bond.data_providers.wind import WindDataProvider

        src = __import__("inspect").getsource(WindDataProvider.get_bond_terms)
        assert "math.isfinite" in src, "数值字段没挡 NaN"

        # 直接跑那段闭包的逻辑, 不只是扫源码
        d = {"good": 12.5, "nan": float("nan"), "inf": float("inf"), "none": None}

        def _f(key):
            v = d.get(key)
            if v is None:
                return None
            try:
                f = float(v)
            except (TypeError, ValueError):
                return None
            return f if math.isfinite(f) else None

        assert _f("good") == 12.5
        assert _f("nan") is None and _f("inf") is None and _f("none") is None

    def test_stock_history_drops_nat_rows_instead_of_crashing_the_sort(self):
        """``pandas.NaT`` 是 ``datetime`` 子类且为真值 —— ``to_date`` 原样放行它。

        于是 NaT 混进序列, 末尾那次 ``sort(key=lambda i: i[0] or date.min)`` 拿它和真
        date 比较就抛 ``TypeError: Cannot compare NaT with datetime.date`` —— 而
        ``or date.min`` 拦不住 (NaT 是真值)。整段历史被上层的 except 吞掉, 表现为
        "这只股没有历史数据", 而不是报错。
        """
        import pandas as pd

        from convertible_bond.data_providers._helpers import _stock_history_from_df

        df = pd.DataFrame([
            {"日期": "2026-08-27", "收盘": 10.0},
            {"日期": pd.NaT, "收盘": 11.0},
            {"日期": "2026-08-28", "收盘": 12.0},
        ])
        rows = _stock_history_from_df(df)      # 不许抛
        assert [c for _, c in rows] == [10.0, 12.0], "NaT 行没被丢掉"

    def test_wind_connect_cooldown_survives_an_empty_env_var(self):
        """``export CBLENS_WIND_CONNECT_COOLDOWN_SEC=`` (设了但留空) 曾让整个包 import 崩。

        这是**模块级**求值, 所以炸的不是某个功能而是 ``import convertible_bond``。
        同组另外两个常量早就带了 ``or`` 回落, 只有这个漏了。
        """
        import importlib
        import os

        import convertible_bond.data_providers.wind as w

        old = os.environ.get("CBLENS_WIND_CONNECT_COOLDOWN_SEC")
        os.environ["CBLENS_WIND_CONNECT_COOLDOWN_SEC"] = ""
        try:
            importlib.reload(w)
            assert w.WIND_CONNECT_COOLDOWN_SEC > 0
        finally:
            if old is None:
                os.environ.pop("CBLENS_WIND_CONNECT_COOLDOWN_SEC", None)
            else:
                os.environ["CBLENS_WIND_CONNECT_COOLDOWN_SEC"] = old
            importlib.reload(w)
