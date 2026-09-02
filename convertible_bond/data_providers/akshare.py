"""akshare 后端 (免费, 无 token, 数据来自东财/新浪/集思录).

数据组合:
  - bond_zh_cov            列表层: 转股价 / 正股代码 / 现价 / 信用评级 / 发行规模
  - bond_cb_profile_sina   详情层: 到期日 / 起息日 / 利率说明 (中文) / 计息方式
  - stock_zh_a_daily       正股日线历史 (主, 新浪)
  - stock_zh_a_hist        正股日线历史 (兜底, 东财)
  - stock_zh_a_spot_em     正股实时快照 (现价兜底, 东财)
  - bond_zh_hs_cov_daily   转债日线历史
  - macro_china_shibor_all Shibor 期限结构

**正股日线以新浪为主、东财为兜底**, 与直觉相反是实测决定的: 东财的实时行情集群
(``push2`` / ``push2his``) 按出口 IP 限流封禁, 被封期间 ``stock_zh_a_hist`` 与
``stock_zh_a_spot_em`` 整段不可用 (单次失败还要等满 5.4s), 而同期新浪
``stock_zh_a_daily`` 稳定 0.3s 出数。东财排在前面时, 每只债都要先为一个注定失败的
调用付一次代价, 而它能给的东西新浪已经给了。详见 ``_helpers._REJECTION_MARKERS``。

瞬态网络错误 (连接重置 / 超时) 自动重试 3 次; **源站限流拒绝不重试**, 并让该端点
进熔断冷却 (见 ``_helpers.AKSHARE_ENDPOINT_COOLDOWN_SEC``)。
强赎/回售触发比例、回售观察期月数 akshare 不直接给, 留 None
(落到 UniversalCBPricer 的默认 1.3 / 0.7 / put_active_years=2)。
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import date, timedelta

import numpy as np

from .base import (
    BondTerms,
    DataProvider,
    infer_cb_trading_metadata,
    parse_coupon_chinese_text,
    safe_date,
    to_date,
)
from ._helpers import (
    EndpointCooldownError,
    _float_or_none,
    _retry,
    _row_value,
    _stock_history_from_df,
    _wind_to_ak_bond,
    _wind_to_ak_stock,
    _wind_to_ak_stock_prefixed,
)
from ..market_time import market_today


logger = logging.getLogger(__name__)
_STALE_STOCK_CLOSE_DAYS = 7

_JS_RUNTIME_LOCK = threading.Lock()
_js_runtime_warmed = False


def _warm_up_js_runtime() -> None:
    """把 V8 (py_mini_racer) 的进程级一次性初始化压在单线程里跑完。

    akshare 的 ``bond_zh_hs_cov_daily`` (转债日线) 与 ``stock_zh_a_daily`` (正股日线
    兜底) 每次调用都**新建**一个 MiniRacer 上下文去跑新浪的 JS 解密。而 V8 的
    partition_alloc 地址空间是**进程级一次性**初始化、且这一步不是线程安全的:
    批量定价的多个线程头一回同时进到那一行, 落后的那个会在
    ``PartitionAddressSpace::Init`` 里 PA_CHECK 失败 → **SIGTRAP**。

    那是 C 层 abort **不是 Python 异常** —— worker 的 try/except 一个字都接不住,
    整个进程当场消失, 用户看到的就是"行情源切成 akshare, 点关注池重算, GUI 直接闪退"。
    实测本机 py_mini_racer 0.14.1: 8 线程同时首建 3/3 崩 (rc=133=128+SIGTRAP),
    先在单线程建一个再放开则 5/5 干净; 崩溃报告里两个线程一个停在
    ``PartitionAddressSpace::Init`` 一个停在 ``Isolate::Init``, 正是这个竞态的形状。

    **只热身, 不留全局引用**: ``MiniRacer.__del__`` → ``close()`` 会 join 它自己的
    事件循环线程, 而解释器退出时守护线程已被冻结 —— 留一份活引用会把"启动闪退"换成
    "退出挂死" (实测 12s 超时栈就停在那个 join 上)。
    """
    global _js_runtime_warmed
    if _js_runtime_warmed:
        return
    with _JS_RUNTIME_LOCK:
        # 双检: 落后的线程在这里排队, 等先到的那个把进程级初始化做完再放行
        if _js_runtime_warmed:
            return
        try:
            import py_mini_racer  # type: ignore[import-not-found]

            ctx = py_mini_racer.MiniRacer()
            try:
                ctx.eval("1")
            finally:
                close = getattr(ctx, "close", None)
                if close is not None:
                    close()
        except Exception:
            # 预热是防御性的: 装不上/初始化不了都不该挡住定价本身 (JS 解密只影响
            # 日线那两个端点, 其余取数照常)
            logger.debug("py_mini_racer 预热跳过", exc_info=True)
        _js_runtime_warmed = True


class AkshareDataProvider(DataProvider):
    name = "akshare"

    # 转债列表缓存 TTL: 长开 GUI/桌面包场景下定期重拉, 否则新上市/退市债永不可见。
    # 12 小时覆盖盘前到收盘后; 单次批量定价 (分钟级) 内仍只拉一次。
    _CB_LIST_TTL_SECONDS = 12 * 3600
    # 类级默认: 兼容绕过 __init__ 手工组装的实例 (测试常用模式)
    _cb_list_fetched_at: float | None = None

    def __init__(self):
        try:
            import akshare as ak  # type: ignore[import-not-found]
        except ImportError as e:
            raise ImportError(
                "未安装 akshare. 请运行: pip install akshare"
            ) from e
        self._ak = ak
        # 必须在批量定价 fan-out **之前**做掉 (provider 总是先在单线程里构造好再分发),
        # 否则多个 worker 同时首建 V8 上下文会 SIGTRAP 掉整个进程, 见 _warm_up_js_runtime
        _warm_up_js_runtime()
        self._cb_list_cache = None
        self._cb_list_fetched_at: float | None = None
        self._profile_cache: dict = {}    # bond_code -> profile DataFrame
        self._value_analysis_cache: dict = {}  # bond_code -> value-analysis DataFrame
        self._historical_k_cache: dict[tuple[str, date], float | None] = {}

    def _cb_list(self):
        # 时间戳未知 (手工预置缓存) 视为新鲜, 只有明确超过 TTL 才重拉
        now = time.monotonic()
        fetched_at = self._cb_list_fetched_at
        expired = fetched_at is not None and now - fetched_at > self._CB_LIST_TTL_SECONDS
        if self._cb_list_cache is None or expired:
            self._cb_list_cache = _retry(self._ak.bond_zh_cov, label="bond_zh_cov")
            self._cb_list_fetched_at = now
        return self._cb_list_cache

    def _profile(self, bond_code):
        ak_code = _wind_to_ak_bond(bond_code)
        if ak_code in self._profile_cache:
            return self._profile_cache[ak_code]
        try:
            df = _retry(lambda: self._ak.bond_cb_profile_sina(symbol=ak_code),
                        label="bond_cb_profile_sina")
        except Exception as e:
            logger.warning("bond_cb_profile_sina 取 %s 失败: %s", bond_code, e)
            df = None
        self._profile_cache[ak_code] = df
        return df

    @staticmethod
    def _profile_value(df, item_name):
        """从 'item / value' 二列长表里抽某一项."""
        if df is None or len(df) == 0:
            return None
        try:
            mask = df["item"].astype(str).str.strip() == item_name
            if not mask.any():
                return None
            v = df.loc[mask, "value"].iloc[0]
            if v is None or v == "" or v == "--":
                return None
            return v
        except Exception:
            return None

    def _value_analysis(self, bond_code):
        """东方财富价值分析: 包含每日转股价值, 可用于反推历史转股价."""
        ak_code = str(bond_code or "").split(".")[0]
        if ak_code in self._value_analysis_cache:
            return self._value_analysis_cache[ak_code]
        try:
            df = _retry(
                lambda: self._ak.bond_zh_cov_value_analysis(symbol=ak_code),
                label=f"bond_zh_cov_value_analysis({ak_code})",
            )
            if df is not None and len(df) > 0 and "日期" in df.columns:
                df = df.copy()
                df["_d"] = df["日期"].apply(self._safe_date_value)
                df = df[df["_d"].notna()].sort_values("_d")
        except Exception as e:
            logger.debug("bond_zh_cov_value_analysis 取 %s 失败: %s", bond_code, e)
            df = None
        self._value_analysis_cache[ak_code] = df
        return df

    def _value_analysis_row(self, bond_code, valuation_date):
        df = self._value_analysis(bond_code)
        if df is None or len(df) == 0 or "_d" not in df.columns:
            return None
        try:
            sub = df[df["_d"] <= valuation_date]
            if len(sub) == 0:
                return None
            return sub.iloc[-1]
        except Exception:
            return None

    def _historical_conversion_price(self, bond_code, stock_code, valuation_date) -> float | None:
        """用 AkShare 历史转股价值 + 正股历史价反推估值日转股价.

        ``bond_zh_cov`` 只给当前转股价; ``bond_zh_cov_value_analysis`` 有每日
        转股价值。根据 ``转股价值 = 正股收盘价 / 转股价 * 100`` 可反推历史 K。
        """
        key = (bond_code, valuation_date)
        if key in self._historical_k_cache:
            return self._historical_k_cache[key]
        value: float | None = None
        row = self._value_analysis_row(bond_code, valuation_date)
        if row is not None and stock_code:
            conv_value = _float_or_none(row.get("转股价值"))
            row_date = row.get("_d")
            if conv_value is not None and conv_value > 0 and row_date is not None:
                try:
                    stock_close = self.get_stock_close(stock_code, row_date)
                    if stock_close and stock_close > 0:
                        value = float(stock_close) * 100.0 / float(conv_value)
                except Exception as e:
                    logger.debug("akshare 历史转股价反推失败 %s %s: %s", bond_code, valuation_date, e)
        self._historical_k_cache[key] = value
        return value

    def _historical_bond_close_from_value_analysis(self, bond_code, valuation_date) -> float | None:
        row = self._value_analysis_row(bond_code, valuation_date)
        if row is None:
            return None
        value = _float_or_none(row.get("收盘价"))
        return value if value is not None and value > 0 else None

    def get_bond_terms(self, bond_code, valuation_date):
        plain_code = bond_code.split(".")[0]

        # 1) 列表层: 转股价 / 正股代码 / 现价 / 评级
        list_df = self._cb_list()
        list_row = None
        try:
            mask = list_df["债券代码"].astype(str) == plain_code
            if mask.any():
                list_row = list_df[mask].iloc[0]
        except Exception:
            list_row = None
        if list_row is None:
            logger.warning("akshare bond_zh_cov 未找到 %s, 列表字段全空", bond_code)

        def _gl(*keys):
            if list_row is None:
                return None
            for k in keys:
                if k in list_row.index:
                    v = list_row[k]
                    if v is None:
                        continue
                    if isinstance(v, float) and np.isnan(v):
                        continue
                    return v
            return None

        underlying_plain = _gl("正股代码")
        underlying = None
        if underlying_plain is not None:
            up = str(underlying_plain).strip().zfill(6)
            if up.startswith(("6", "9")):
                underlying = f"{up}.SH"
            elif up.startswith(("0", "3", "2")):
                underlying = f"{up}.SZ"
            else:
                underlying = up

        # 2) 详情层 (新浪): 到期日 / 起息日 / 利率说明
        profile = self._profile(bond_code)
        maturity_str = self._profile_value(profile, "到期日") or self._profile_value(profile, "兑付日")
        issue_str = self._profile_value(profile, "起息日期") or self._profile_value(profile, "发行日期")
        coupon_text = self._profile_value(profile, "利率说明")
        rating_profile = self._profile_value(profile, "信用等级")
        size_str = self._profile_value(profile, "发行规模（亿元）")

        # 3) 类型转换
        K = _gl("转股价")
        K_val = float(K) if K is not None and float(K) > 0 else None
        historical_k = self._historical_conversion_price(bond_code, underlying, valuation_date)
        if historical_k is not None and historical_k > 0:
            K_val = historical_k
        close_val = (
            self._historical_bond_close_from_value_analysis(bond_code, valuation_date)
            or _gl("债现价", "现价", "价格")
        )
        rating = _gl("信用评级") or rating_profile
        turnover = _float_or_none(_gl("成交额", "成交额(元)", "成交额(万元)"))

        size_val = None
        if size_str is not None:
            try:
                size_val = float(str(size_str).replace(",", ""))
            except ValueError:
                size_val = None

        # 这两格必须走 safe_date: 上游对还没挂牌的新债返回 ``pandas.NaT``, 而 NaT 是
        # datetime 子类且 ``bool(NaT) is True`` —— ``to_date`` 会原样放行、``or`` 也不回落,
        # 于是 NaT 一路混进 listing_date。
        listing_dt = safe_date(_gl("上市时间"))
        issue_dt = safe_date(issue_str) or safe_date(_gl("申购日期"))
        terms = BondTerms(
            sec_name=_gl("债券简称"),
            underlying_code=underlying,
            issue_date=issue_dt or listing_dt,
            # 上市日**没有兜底**: 缺它才是"已发行未上市"的判据 (见 is_issued_pending_listing)。
            # 原本回落成起息日, 于是刚申购完还没挂牌的新债被判成已上市 → tradable_date =
            # 起息日 ≤ 今天 → 带着空市价混进主池, 同时从"扫新债"里消失。
            listing_date=listing_dt,
            maturity_date=safe_date(maturity_str),
            face_value=100.0,
            conversion_price=K_val,
            redemption_price=None,         # 不在 akshare 字段, 由默认 107 兜底
            down_reset_trigger_pct=None,   # 同上, 由 Wind/本地基础条款补充; 定价层默认 85%K
            call_trigger_pct=None,         # 同上, 由默认 130 兜底
            put_trigger_pct=None,          # 同上, 由默认 70 兜底
            put_obs_months=None,
            coupon_rates=parse_coupon_chinese_text(coupon_text),
            close=(float(close_val) if close_val is not None else None),
            credit_rating=str(rating) if rating else None,
            outstanding_balance=size_val,
            underlying_name=str(_gl("正股简称")) if _gl("正股简称") else None,
            bond_turnover_amount=turnover,
        )
        return infer_cb_trading_metadata(bond_code, terms, valuation_date)

    def get_stock_close(self, stock_code, on_date):
        history = self.get_stock_history(stock_code, on_date - timedelta(days=15), on_date)
        px = None
        px_date = None
        for d, value in history:
            if d is None or d > on_date:
                continue
            try:
                finite_value = float(value)
            except (TypeError, ValueError):
                continue
            if np.isfinite(finite_value):
                px = finite_value
                px_date = d
        if px is not None:
            stale_days = (on_date - px_date).days if px_date is not None else 0
            if stale_days > _STALE_STOCK_CLOSE_DAYS:
                logger.warning(
                    "akshare 正股 %s 在估值日 %s 未取到近期收盘价, 使用 %s 的收盘价 %.4f",
                    stock_code, on_date, px_date, px,
                )
            return px

        # **实时快照只能顶今天, 不能顶历史**。这里回落的是 ``stock_zh_a_spot_em`` 的
        # 「最新价」—— 对历史估值日而言那是**未来**的价, 而 S0 驱动整个 PDE。
        # 回测确实走得到这条路: _BacktestCacheProvider → DiskCacheProvider →
        # HistoricalBondDataProvider → CachedBondDataProvider → 这里, 而上面那个
        # (D-15, D) 的窄请求两层回测缓存都不接, 所以正股停牌超过 15 天、或者那一次
        # 请求恰好碰上东财按 IP 封禁 (AGENTS 记的常态), 就会把今天的价当成 D 的 S0。
        # 实测: 停牌起 2022-06-06, 估值日 2022-06-30, spot=999 → status ok、S0 999、
        # 理论价 9990、deviation −98.8%, 而 max_model_premium 那道闸拦不住 (parity 同样
        # 按 S0 缩放, 比值不变), 于是它以 confidence 高 排在候选第一。
        # 这是 AGENTS 已经记过的 get_stock_dividend_yield 同一类问题, 只是 S0 更要命。
        if on_date < market_today():
            raise RuntimeError(
                f"akshare 取正股 {stock_code} 在 {on_date} 的收盘价为空; "
                f"实时快照只适用于当日, 不用于历史估值日")

        plain = _wind_to_ak_stock(stock_code).zfill(6)
        try:
            spot = _retry(self._ak.stock_zh_a_spot_em, label="stock_zh_a_spot_em",
                          endpoint="stock_zh_a_spot_em")
            if spot is not None and len(spot) > 0:
                mask = spot["代码"].astype(str).str.zfill(6) == plain
                if mask.any():
                    row = spot[mask].iloc[0]
                    value = _row_value(row, "最新价", "最新", "现价")
                    if value is not None:
                        return float(value)
        except EndpointCooldownError as e:
            logger.debug("akshare 正股实时快照跳过 %s: %s", stock_code, e)
        except Exception as e:
            logger.warning("akshare 正股实时快照取 %s 失败: %s", stock_code, e)
        raise RuntimeError(f"akshare 取正股 {stock_code} 现价为空")

    def get_stock_history(self, stock_code, start, end):
        plain = _wind_to_ak_stock(stock_code)
        prefixed = _wind_to_ak_stock_prefixed(stock_code)
        start_str = start.strftime("%Y%m%d")
        end_str = end.strftime("%Y%m%d")

        errors = []
        # 顺序是实测决定的: 新浪在前, 东财兜底 —— 见模块 docstring。
        # 东财这一路挂上 endpoint 熔断: 被 IP 封禁期间只有第一只债付一次代价,
        # 之后直接跳过, 不再每只都等满连接超时, 也不再继续喂那个限流器。
        calls = [
            (
                f"stock_zh_a_daily({prefixed})",
                lambda: self._ak.stock_zh_a_daily(
                    symbol=prefixed, start_date=start_str, end_date=end_str, adjust=""),
                None,
            ),
            (
                f"stock_zh_a_hist({plain})",
                lambda: self._ak.stock_zh_a_hist(
                    symbol=plain, period="daily",
                    start_date=start_str, end_date=end_str, adjust=""),
                "stock_zh_a_hist",
            ),
        ]
        for label, call, endpoint in calls:
            try:
                df = _retry(call, label=label, endpoint=endpoint)
                history = _stock_history_from_df(df)
                if history:
                    return [(d, v) for d, v in history if d is not None and start <= d <= end]
            except EndpointCooldownError as e:
                errors.append(f"{label}: {e}")
            except Exception as e:
                errors.append(f"{label}: {e}")
                logger.warning("akshare %s 失败: %s", label, e)
        logger.warning("akshare 正股历史 %s 全部失败: %s", stock_code, " | ".join(errors))
        return []

    @staticmethod
    def _dividend_yield_value(value) -> float | None:
        if value is None:
            return None
        text = str(value).replace("%", "").strip()
        pct = _float_or_none(text)
        if pct is None or pct < 0:
            return None
        return pct

    @staticmethod
    def _dividend_yield_columns(df) -> list:
        cols = []
        if df is None:
            return cols
        for col in df.columns:
            raw = str(col)
            key = raw.lower().replace(" ", "").replace("-", "_")
            if (
                "股息" in raw
                or key in {"dv_ratio", "dv_ttm", "dv_ratio_ttm", "dividend_yield"}
            ):
                cols.append(col)
        return cols

    @staticmethod
    def _safe_date_value(value):
        try:
            return to_date(value)
        except Exception:
            return None

    def get_stock_dividend_yield(self, stock_code, on_date):
        """取正股股息率 (%), 优先使用乐咕估值指标, 失败时尝试实时快照字段.

        ⚠️ 两条路当前都可能不通, 于是 ``q`` 静默落到 0 (见 ``pricing_api`` 的回退):
        ``stock_a_indicator_lg`` 已被 **akshare 上游删除** (实测 1.18.58 起
        ``AttributeError``, 所以下面那道 ``hasattr`` 现在恒为 False), 而兜底的
        ``stock_zh_a_spot_em`` 属于东财被限流封禁的那个集群。这不是本项目的 bug,
        但**别把"q=0"读成"这只股不分红"** —— 要区分, 看有没有
        "正股实时股息率取 … 失败" 的告警。
        """
        plain = _wind_to_ak_stock(stock_code).zfill(6)

        if hasattr(self._ak, "stock_a_indicator_lg"):
            try:
                df = _retry(
                    lambda: self._ak.stock_a_indicator_lg(symbol=plain),
                    label=f"stock_a_indicator_lg({plain})",
                )
                cols = self._dividend_yield_columns(df)
                if df is not None and len(df) > 0 and cols:
                    date_col = next(
                        (c for c in df.columns if str(c).lower() in {"trade_date", "date", "日期"}),
                        None,
                    )
                    rows_df = df
                    if date_col is not None:
                        rows_df = df.copy()
                        rows_df["_d"] = rows_df[date_col].apply(self._safe_date_value)
                        rows_df = rows_df[rows_df["_d"].notna() & (rows_df["_d"] <= on_date)]
                        rows_df = rows_df.sort_values("_d")
                    if len(rows_df) > 0:
                        for _, row in rows_df.iloc[::-1].iterrows():
                            for col in cols:
                                pct = self._dividend_yield_value(row.get(col))
                                if pct is not None:
                                    return pct
            except Exception as e:
                logger.warning("akshare 股息率取 %s 失败: %s", stock_code, e)

        try:
            spot = _retry(self._ak.stock_zh_a_spot_em, label="stock_zh_a_spot_em",
                          endpoint="stock_zh_a_spot_em")
            if spot is not None and len(spot) > 0:
                mask = spot["代码"].astype(str).str.zfill(6) == plain
                if mask.any():
                    row = spot[mask].iloc[0]
                    for col in self._dividend_yield_columns(spot):
                        pct = self._dividend_yield_value(row.get(col))
                        if pct is not None:
                            return pct
        except EndpointCooldownError as e:
            logger.debug("akshare 正股实时股息率跳过 %s: %s", stock_code, e)
        except Exception as e:
            logger.warning("akshare 正股实时股息率取 %s 失败: %s", stock_code, e)
        return None

    def get_bond_history(self, bond_code, start, end):
        ak_code = _wind_to_ak_bond(bond_code)
        try:
            df = _retry(lambda: self._ak.bond_zh_hs_cov_daily(symbol=ak_code),
                        label=f"bond_zh_hs_cov_daily({ak_code})")
        except Exception as e:
            raise RuntimeError(f"akshare 取转债 {bond_code} 历史价失败: {e}") from e
        if df is None or len(df) == 0:
            return []
        out = []
        for _, row in df.iterrows():
            try:
                d = to_date(row["date"])
            except Exception:
                continue
            if d is None or d < start or d > end:
                continue
            v = row.get("close")
            out.append((d, float(v) if v is not None and not (isinstance(v, float) and np.isnan(v)) else None))
        return out

    def get_risk_free_rate(self, on_date):
        try:
            df = _retry(self._ak.macro_china_shibor_all, label="macro_china_shibor_all")
        except Exception as e:
            logger.warning("akshare Shibor 拉取失败: %s", e)
            return None
        if df is None or len(df) == 0:
            return None
        # 列名按 akshare 现版: '1Y_定价' or '1Y'
        rate_col = None
        for c in df.columns:
            cs = str(c)
            if "1Y" in cs or "1y" in cs or "1年" in cs:
                rate_col = c
                break
        if rate_col is None:
            return None

        # 历史回测时需要 on_date 当天 (或之前最近一日) 的 Shibor, 不能用最新值
        date_col = None
        for c in df.columns:
            cs = str(c).lower()
            if cs in {"日期", "date"} or "日期" in str(c):
                date_col = c
                break

        try:
            if date_col is None:
                # 无日期列时只能退回 "最新值" — 历史回测会有偏差, 但好过抛错
                return float(df[rate_col].dropna().iloc[-1])
            sub = df[[date_col, rate_col]].dropna()
            sub = sub.assign(_d=sub[date_col].apply(to_date))
            sub = sub[sub["_d"].notna() & (sub["_d"] <= on_date)]
            if len(sub) == 0:
                return None
            return float(sub.sort_values("_d")[rate_col].iloc[-1])
        except Exception:
            return None

    def list_tradable_cbs(self, on_date=None):
        """从 bond_zh_cov 抽出所有 CB 代码, 转换为 Wind 格式.

        akshare 返回的 '债券代码' 是 6 位数字; 按首位推断交易所:
            11xxxx → SH (沪市), 其它 (12xxxx/13xxxx) → SZ (深市)
        返回 ``[(wind_code, sec_name), ...]``; akshare 的 '债券简称' 列充当 sec_name。
        """
        if on_date is not None and on_date != market_today():
            raise NotImplementedError("akshare 不支持历史可转债全市场成分")
        df = self._cb_list()
        if df is None or len(df) == 0:
            return []
        name_col = next(
            (c for c in ("债券简称", "债券名称", "证券简称") if c in df.columns),
            None,
        )
        out: list[tuple[str, str | None]] = []
        for idx, code in enumerate(df["债券代码"].astype(str)):
            c = code.strip().zfill(6)
            wind_code = f"{c}.SH" if c.startswith("11") else f"{c}.SZ"
            name = None
            if name_col is not None:
                raw = df[name_col].iloc[idx]
                if raw is not None and str(raw).strip():
                    name = str(raw).strip()
            out.append((wind_code, name))
        return out
