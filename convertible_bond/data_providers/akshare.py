"""akshare 后端 (免费, 无 token, 数据来自东财/新浪/集思录).

数据组合:
  - bond_zh_cov            列表层: 转股价 / 正股代码 / 现价 / 信用评级 / 发行规模
  - bond_cb_profile_sina   详情层: 到期日 / 起息日 / 利率说明 (中文) / 计息方式
  - stock_zh_a_hist        正股日线历史 (主)
  - stock_zh_a_daily       正股日线历史 (兜底)
  - stock_zh_a_spot_em     正股实时快照 (现价兜底)
  - bond_zh_hs_cov_daily   转债日线历史
  - macro_china_shibor_all Shibor 期限结构

瞬态网络错误 (RemoteDisconnected / 超时) 自动重试 3 次。
强赎/回售触发比例、回售观察期月数 akshare 不直接给, 留 None
(落到 UniversalCBPricer 的默认 1.3 / 0.7 / put_active_years=2)。
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

import numpy as np

from .base import (
    BondTerms,
    DataProvider,
    infer_cb_trading_metadata,
    parse_coupon_chinese_text,
    to_date,
)
from ._helpers import (
    _float_or_none,
    _latest_finite,
    _retry,
    _row_value,
    _stock_history_from_df,
    _wind_to_ak_bond,
    _wind_to_ak_stock,
    _wind_to_ak_stock_prefixed,
)


logger = logging.getLogger(__name__)


class AkshareDataProvider(DataProvider):
    name = "akshare"

    def __init__(self):
        try:
            import akshare as ak  # type: ignore[import-not-found]
        except ImportError as e:
            raise ImportError(
                "未安装 akshare. 请运行: pip install akshare"
            ) from e
        self._ak = ak
        self._cb_list_cache = None
        self._profile_cache: dict = {}    # bond_code -> profile DataFrame
        self._value_analysis_cache: dict = {}  # bond_code -> value-analysis DataFrame
        self._historical_k_cache: dict[tuple[str, date], float | None] = {}

    def _cb_list(self):
        if self._cb_list_cache is None:
            self._cb_list_cache = _retry(self._ak.bond_zh_cov, label="bond_zh_cov")
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

        listing_dt = to_date(_gl("上市时间")) if _gl("上市时间") else None
        issue_dt = to_date(issue_str) if issue_str else to_date(_gl("申购日期"))
        terms = BondTerms(
            sec_name=_gl("债券简称"),
            underlying_code=underlying,
            issue_date=issue_dt or listing_dt,
            listing_date=listing_dt or issue_dt,
            maturity_date=to_date(maturity_str) if maturity_str else None,
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
        px = _latest_finite([v for _, v in history])
        if px is not None:
            return px

        plain = _wind_to_ak_stock(stock_code).zfill(6)
        try:
            spot = _retry(self._ak.stock_zh_a_spot_em, label="stock_zh_a_spot_em")
            if spot is not None and len(spot) > 0:
                mask = spot["代码"].astype(str).str.zfill(6) == plain
                if mask.any():
                    row = spot[mask].iloc[0]
                    value = _row_value(row, "最新价", "最新", "现价")
                    if value is not None:
                        return float(value)
        except Exception as e:
            logger.warning("akshare 正股实时快照取 %s 失败: %s", stock_code, e)
        raise RuntimeError(f"akshare 取正股 {stock_code} 现价为空")

    def get_stock_history(self, stock_code, start, end):
        plain = _wind_to_ak_stock(stock_code)
        prefixed = _wind_to_ak_stock_prefixed(stock_code)
        start_str = start.strftime("%Y%m%d")
        end_str = end.strftime("%Y%m%d")

        errors = []
        calls = [
            (
                f"stock_zh_a_hist({plain})",
                lambda: self._ak.stock_zh_a_hist(
                    symbol=plain, period="daily",
                    start_date=start_str, end_date=end_str, adjust=""),
            ),
            (
                f"stock_zh_a_daily({prefixed})",
                lambda: self._ak.stock_zh_a_daily(
                    symbol=prefixed, start_date=start_str, end_date=end_str, adjust=""),
            ),
        ]
        for label, call in calls:
            try:
                df = _retry(call, label=label)
                history = _stock_history_from_df(df)
                if history:
                    return [(d, v) for d, v in history if d is not None and start <= d <= end]
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
        """取正股股息率 (%), 优先使用乐咕估值指标, 失败时尝试实时快照字段."""
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
            spot = _retry(self._ak.stock_zh_a_spot_em, label="stock_zh_a_spot_em")
            if spot is not None and len(spot) > 0:
                mask = spot["代码"].astype(str).str.zfill(6) == plain
                if mask.any():
                    row = spot[mask].iloc[0]
                    for col in self._dividend_yield_columns(spot):
                        pct = self._dividend_yield_value(row.get(col))
                        if pct is not None:
                            return pct
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
        if on_date is not None and on_date != date.today():
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
