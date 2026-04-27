"""
数据源后端抽象层.

把 Wind / akshare / CSV 等不同数据源统一到一个 DataProvider 接口,
让 CB.py 的定价 / 回测函数与具体数据源解耦.

新增后端只需继承 DataProvider 并实现下列方法:
  - get_bond_terms(code, valuation_date) -> BondTerms
  - get_stock_close(stock_code, on_date) -> float
  - get_stock_history(stock_code, start, end) -> [(date, float|None), ...]
  - get_bond_history(bond_code, start, end) -> [(date, float|None), ...]
  - get_cashflow(bond_code) -> CashflowSchedule | None
  - get_risk_free_rate(on_date) -> float | None  (单位: %, 例如 2.20)
"""
from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# ── 数据载体 ──────────────────────────────────────────────
@dataclass
class BondTerms:
    """转债条款快照. 字段全部可选 (不同数据源能拉到的字段不一样)."""
    sec_name: Optional[str] = None
    underlying_code: Optional[str] = None
    issue_date: Optional[date] = None
    maturity_date: Optional[date] = None
    face_value: Optional[float] = None              # 例: 100.0
    conversion_price: Optional[float] = None        # 转股价 K
    redemption_price: Optional[float] = None        # 到期赎回价 (例 107.0)
    call_trigger_pct: Optional[float] = None        # 强赎触发 (例 130.0 = 130%K)
    put_trigger_pct: Optional[float] = None         # 回售触发 (例 70.0)
    put_obs_months: Optional[float] = None          # 回售观察期月数 (从发行起算)
    coupon_rates: Optional[Tuple[float, ...]] = None  # 已解析的小数 (例 (0.003, 0.005, ...))
    close: Optional[float] = None                   # 转债现价
    credit_rating: Optional[str] = None
    outstanding_balance: Optional[float] = None     # 剩余规模 (亿)


@dataclass
class CashflowSchedule:
    """完整付息计划. 通常比 BondTerms.coupon_rates 更准 (覆盖到期溢价)."""
    coupon_rates: Optional[Tuple[float, ...]] = None
    redemption_price: Optional[float] = None
    maturity_date: Optional[date] = None
    cashflows: List[Any] = field(default_factory=list)


# ── 公共工具 ──────────────────────────────────────────────
def to_date(v: Any) -> Optional[date]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def parse_coupon_string(raw: Any) -> Optional[Tuple[float, ...]]:
    """解析 '0.3,0.5,0.8' 格式的票息字符串 (单位 %)."""
    if raw is None or raw == "":
        return None
    parts = [p.strip().rstrip("%") for p in str(raw).split(",") if p.strip()]
    try:
        return tuple(float(p) / 100.0 for p in parts)
    except ValueError:
        return None


def parse_coupon_chinese_text(text: Any) -> Optional[Tuple[float, ...]]:
    """从 '第一年0.40%、第二年0.60%、第三年1.00%...' 这类中文描述里提取票息序列.

    返回按顺序的票息小数; 解析失败返回 None.
    主要给 akshare 用 (它的 '票面利率说明' 字段是中文段落).
    """
    if text is None:
        return None
    s = str(text)
    # 抓所有 '数字%' (按出现顺序)
    rates = re.findall(r"(\d+\.?\d*)\s*%", s)
    if not rates:
        return None
    try:
        return tuple(float(r) / 100.0 for r in rates)
    except ValueError:
        return None


def _latest_finite(values) -> Optional[float]:
    """返回序列里最后一个有限数值."""
    if not values:
        return None
    for v in reversed(values):
        if v is None:
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if np.isfinite(fv):
            return fv
    return None


# ── 接口 ──────────────────────────────────────────────────
class DataProvider(ABC):
    """所有数据源后端的统一接口."""

    name: str = "abstract"

    @abstractmethod
    def get_bond_terms(self, bond_code: str, valuation_date: date) -> BondTerms:
        """拉取条款快照. 不可获取的字段保留为 None."""

    @abstractmethod
    def get_stock_close(self, stock_code: str, on_date: date) -> float:
        """正股某日收盘价 (未复权)."""

    @abstractmethod
    def get_stock_history(self, stock_code: str, start: date, end: date) -> List[Tuple[date, Optional[float]]]:
        """正股 [start, end] 区间收盘价时序, 升序. 缺失值用 None."""

    @abstractmethod
    def get_bond_history(self, bond_code: str, start: date, end: date) -> List[Tuple[date, Optional[float]]]:
        """转债 [start, end] 区间收盘价时序, 升序. 缺失值用 None."""

    def get_cashflow(self, bond_code: str) -> Optional[CashflowSchedule]:
        """完整付息计划. 默认 None, 让调用方回退到 BondTerms.coupon_rates."""
        return None

    def get_risk_free_rate(self, on_date: date) -> Optional[float]:
        """无风险利率参考值 (%). 默认 None."""
        return None

    def hist_vol(self, stock_code: str, end_date: date, window_days: int) -> float:
        """从历史收盘计算年化滚动波动率 (默认实现, 子类可覆盖)."""
        lookback = max(window_days * 2, window_days + 15)
        history = self.get_stock_history(stock_code, end_date - timedelta(days=lookback), end_date)
        closes = np.array([v for _, v in history if v is not None], dtype=float)
        if len(closes) > window_days + 1:
            closes = closes[-(window_days + 1):]
        if len(closes) < 5:
            raise ValueError(f"{stock_code} 历史样本仅 {len(closes)} 条, 无法估算波动率")
        log_ret = np.diff(np.log(closes))
        return float(np.std(log_ret, ddof=1) * np.sqrt(252))


# ── Wind 后端 ──────────────────────────────────────────────
class WindDataProvider(DataProvider):
    """通过 WindPy 拉数据. 需要本机已安装 Wind 终端 + 插件."""
    name = "Wind"

    _BOND_FIELDS = (
        "sec_name", "underlyingcode", "ipo_date", "maturitydate",
        "latestpar",
        "clause_conversion2_swapshareprice",
        "clause_calloption_redemptionprice",
        "clause_calloption_triggerproportion",
        "clause_putoption_redeem_triggerproportion",
        "clause_putoption_putbackperiodobs",
        "couponrate",
        "close", "creditrating", "outstandingbalance",
    )

    def __init__(self):
        self._w = None

    def _ensure(self):
        if self._w is not None:
            return self._w
        try:
            from WindPy import w  # type: ignore[import-not-found]
        except ImportError as e:
            raise ImportError(
                "未检测到 WindPy. 请在 Wind 终端 '插件管理' 中安装 Python 接口."
            ) from e
        if not w.isconnected():
            ret = w.start()
            if ret.ErrorCode != 0:
                raise RuntimeError(f"Wind 启动失败 (ErrorCode={ret.ErrorCode})")
        self._w = w
        return w

    def get_bond_terms(self, bond_code, valuation_date):
        w = self._ensure()
        val_str = valuation_date.strftime("%Y%m%d")
        res = w.wss(bond_code, ",".join(self._BOND_FIELDS), f"tradeDate={val_str}")
        if res.ErrorCode != 0:
            raise RuntimeError(f"Wind 取 {bond_code} 条款失败: {res.Data}")
        d = {f.lower(): v[0] for f, v in zip(res.Fields, res.Data)}

        def _f(key):
            v = d.get(key)
            return float(v) if v is not None else None

        return BondTerms(
            sec_name=d.get("sec_name"),
            underlying_code=d.get("underlyingcode"),
            issue_date=to_date(d.get("ipo_date")),
            maturity_date=to_date(d.get("maturitydate")),
            face_value=_f("latestpar"),
            conversion_price=_f("clause_conversion2_swapshareprice"),
            redemption_price=_f("clause_calloption_redemptionprice"),
            call_trigger_pct=_f("clause_calloption_triggerproportion"),
            put_trigger_pct=_f("clause_putoption_redeem_triggerproportion"),
            put_obs_months=_f("clause_putoption_putbackperiodobs"),
            coupon_rates=parse_coupon_string(d.get("couponrate")),
            close=_f("close"),
            credit_rating=d.get("creditrating"),
            outstanding_balance=_f("outstandingbalance"),
        )

    def get_stock_close(self, stock_code, on_date):
        w = self._ensure()
        val_str = on_date.strftime("%Y%m%d")
        res = w.wss(stock_code, "close", f"tradeDate={val_str};priceAdj=U")
        if res.ErrorCode != 0:
            raise RuntimeError(f"Wind 取正股 {stock_code} 现价失败: {res.Data}")
        return float(res.Data[0][0])

    def get_stock_history(self, stock_code, start, end):
        w = self._ensure()
        res = w.wsd(stock_code, "close", start.isoformat(), end.isoformat(), "priceAdj=U")
        if res.ErrorCode != 0:
            raise RuntimeError(f"Wind 取正股 {stock_code} 历史价失败: {res.Data}")
        return [
            (to_date(t), (float(v) if v is not None else None))
            for t, v in zip(res.Times, res.Data[0])
        ]

    def get_bond_history(self, bond_code, start, end):
        w = self._ensure()
        res = w.wsd(bond_code, "close", start.isoformat(), end.isoformat())
        if res.ErrorCode != 0:
            raise RuntimeError(f"Wind 取 {bond_code} 历史价失败: {res.Data}")
        return [
            (to_date(t), (float(v) if v is not None else None))
            for t, v in zip(res.Times, res.Data[0])
        ]

    def get_cashflow(self, bond_code):
        w = self._ensure()
        res = w.wset("cashflow", f"windcode={bond_code}")
        if res.ErrorCode != 0 or not res.Data:
            return None
        fields = [f.lower() for f in res.Fields]
        try:
            i_date = fields.index("cash_flows_date")
            i_cf = fields.index("cash_flows_per_cny100_par")
            i_rate = fields.index("coupon_rate")
        except ValueError:
            return None
        rows = list(zip(*res.Data))
        if not rows:
            return None
        coupons = []
        for row in rows:
            rate = row[i_rate]
            if rate is None:
                continue
            coupons.append(float(rate) / 100.0)
        last = rows[-1]
        return CashflowSchedule(
            coupon_rates=tuple(coupons) if coupons else None,
            redemption_price=float(last[i_cf]) if last[i_cf] is not None else None,
            maturity_date=to_date(last[i_date]) if last[i_date] else None,
            cashflows=rows,
        )

    def get_risk_free_rate(self, on_date):
        w = self._ensure()
        rr = w.edb("SHIBOR1Y.IR",
                   (on_date - timedelta(days=10)).isoformat(),
                   on_date.isoformat())
        if rr.ErrorCode != 0 or not rr.Data or not rr.Data[0]:
            return None
        return _latest_finite(rr.Data[0])


# ── akshare 后端 ──────────────────────────────────────────
def _wind_to_ak_bond(wind_code: str) -> str:
    """Wind 格式 (128009.SZ) → akshare 格式 (sz128009)."""
    if "." in wind_code:
        code, exch = wind_code.split(".")
        return f"{exch.lower()}{code}"
    return wind_code


def _wind_to_ak_stock(wind_code: str) -> str:
    """正股 Wind 格式 (000001.SZ) → akshare 格式 (000001, 不带前缀)."""
    return wind_code.split(".")[0] if "." in wind_code else wind_code


class AkshareDataProvider(DataProvider):
    """通过 akshare 拉数据. 免费, 无 token; 数据来自东财/集思录, 时效偶有延迟.

    覆盖能力:
      ✓ 转债历史 / 正股历史 / 正股现价 / 票息文本解析
      ~ 条款 (转股价/到期日/正股代码/票息说明 from bond_zh_cov, 部分字段可能缺失)
      ~ Shibor (1Y from macro_china_shibor_all, 字段名以源为准)
      ✗ 完整 cashflow (无对应接口) → 由 coupon_rates 文本推算
    """
    name = "akshare"

    def __init__(self):
        try:
            import akshare as ak  # type: ignore[import-not-found]
        except ImportError as e:
            raise ImportError(
                "未安装 akshare. 请运行: pip install akshare"
            ) from e
        self._ak = ak
        self._cb_list_cache = None  # bond_zh_cov 全表缓存

    def _cb_list(self):
        if self._cb_list_cache is None:
            self._cb_list_cache = self._ak.bond_zh_cov()
        return self._cb_list_cache

    def get_bond_terms(self, bond_code, valuation_date):
        plain_code = bond_code.split(".")[0]
        df = self._cb_list()
        # 列名按 akshare 当前版本: 债券代码 / 转股价 / 到期日期 / 正股代码 / 票面利率说明 / ...
        try:
            row = df[df["债券代码"].astype(str) == plain_code]
        except Exception:
            row = None
        if row is None or len(row) == 0:
            logger.warning("akshare 未找到转债 %s 的条款行, 字段将为空", bond_code)
            return BondTerms()

        r = row.iloc[0]

        def _g(*keys):
            for k in keys:
                if k in r.index and r[k] is not None:
                    val = r[k]
                    if isinstance(val, float) and np.isnan(val):
                        continue
                    return val
            return None

        # 把 SH/SZ 推回去
        underlying_plain = _g("正股代码")
        underlying = None
        if underlying_plain is not None:
            up = str(underlying_plain).strip()
            if up.startswith(("6", "9")):  # 沪市
                underlying = f"{up}.SH"
            elif up.startswith(("0", "3", "2")):  # 深市
                underlying = f"{up}.SZ"
            else:
                underlying = up

        return BondTerms(
            sec_name=_g("债券简称"),
            underlying_code=underlying,
            issue_date=to_date(_g("申购日期", "上市日期")),
            maturity_date=to_date(_g("到期日期")),
            face_value=float(_g("债券面值") or 100.0),
            conversion_price=float(_g("转股价") or 0) or None,
            redemption_price=None,  # akshare CB 列表无字段, 由用户/默认填
            call_trigger_pct=None,
            put_trigger_pct=None,
            put_obs_months=None,
            coupon_rates=parse_coupon_chinese_text(_g("票面利率说明", "票面利率")),
            close=(float(_g("价格")) if _g("价格") is not None else None),
            credit_rating=_g("信用评级"),
            outstanding_balance=None,
        )

    def get_stock_close(self, stock_code, on_date):
        plain = _wind_to_ak_stock(stock_code)
        d = on_date.strftime("%Y%m%d")
        # 取当天前后 7 天兜底
        start = (on_date - timedelta(days=7)).strftime("%Y%m%d")
        df = self._ak.stock_zh_a_hist(symbol=plain, period="daily",
                                       start_date=start, end_date=d, adjust="")
        if df is None or len(df) == 0:
            raise RuntimeError(f"akshare 取正股 {stock_code} 现价为空")
        return float(df["收盘"].iloc[-1])

    def _stock_history_df(self, stock_code, start, end):
        plain = _wind_to_ak_stock(stock_code)
        df = self._ak.stock_zh_a_hist(
            symbol=plain, period="daily",
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
            adjust="",
        )
        return df

    def get_stock_history(self, stock_code, start, end):
        df = self._stock_history_df(stock_code, start, end)
        if df is None or len(df) == 0:
            return []
        out = []
        for _, row in df.iterrows():
            d = to_date(row["日期"])
            v = row.get("收盘")
            out.append((d, float(v) if v is not None and not (isinstance(v, float) and np.isnan(v)) else None))
        return out

    def get_bond_history(self, bond_code, start, end):
        ak_code = _wind_to_ak_bond(bond_code)
        try:
            df = self._ak.bond_zh_hs_cov_daily(symbol=ak_code)
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
            df = self._ak.macro_china_shibor_all()
        except Exception:
            return None
        if df is None or len(df) == 0:
            return None
        # 取最近 10 天的 1Y 列 (列名按 akshare 现版: '1Y_定价' or '1Y')
        col = None
        for c in df.columns:
            if "1Y" in str(c) or "1y" in str(c):
                col = c
                break
        if col is None:
            return None
        try:
            last = df[col].dropna().iloc[-1]
            return float(last)
        except Exception:
            return None


# ── CSV 后端 ──────────────────────────────────────────────
class CSVDataProvider(DataProvider):
    """从本地 CSV 文件读取数据, 适用于无网络/无 Wind/无 akshare 的环境.

    目录布局 (root/):
      bonds/<bond_code>.csv      列: date,close
      stocks/<stock_code>.csv    列: date,close
      terms/<bond_code>.json     条款 JSON (字段名同 BondTerms)

    任何文件缺失会抛出 FileNotFoundError; 上层应捕获并提示用户手填.
    """
    name = "CSV"

    def __init__(self, root: str):
        from pathlib import Path
        self.root = Path(root)
        if not self.root.exists():
            raise FileNotFoundError(f"CSV 数据根目录不存在: {root}")

    def _read_price_csv(self, path) -> List[Tuple[date, Optional[float]]]:
        import csv
        out = []
        with open(path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                d_raw = row.get("date") or row.get("日期")
                c_raw = row.get("close") or row.get("收盘")
                if d_raw is None:
                    continue
                d = to_date(d_raw)
                try:
                    v = float(c_raw) if c_raw not in (None, "") else None
                except ValueError:
                    v = None
                out.append((d, v))
        out.sort(key=lambda x: x[0] or date.min)
        return out

    def _bond_csv(self, code):
        p = self.root / "bonds" / f"{code}.csv"
        if not p.exists():
            raise FileNotFoundError(f"未找到转债历史: {p}")
        return self._read_price_csv(p)

    def _stock_csv(self, code):
        p = self.root / "stocks" / f"{code}.csv"
        if not p.exists():
            raise FileNotFoundError(f"未找到正股历史: {p}")
        return self._read_price_csv(p)

    def get_bond_terms(self, bond_code, valuation_date):
        import json
        p = self.root / "terms" / f"{bond_code}.json"
        if not p.exists():
            return BondTerms()
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
        coupons = d.get("coupon_rates")
        if isinstance(coupons, list):
            coupons = tuple(float(x) for x in coupons)
        return BondTerms(
            sec_name=d.get("sec_name"),
            underlying_code=d.get("underlying_code"),
            issue_date=to_date(d.get("issue_date")),
            maturity_date=to_date(d.get("maturity_date")),
            face_value=d.get("face_value"),
            conversion_price=d.get("conversion_price"),
            redemption_price=d.get("redemption_price"),
            call_trigger_pct=d.get("call_trigger_pct"),
            put_trigger_pct=d.get("put_trigger_pct"),
            put_obs_months=d.get("put_obs_months"),
            coupon_rates=coupons,
            close=d.get("close"),
            credit_rating=d.get("credit_rating"),
            outstanding_balance=d.get("outstanding_balance"),
        )

    def get_stock_close(self, stock_code, on_date):
        history = self._stock_csv(stock_code)
        for d, v in reversed(history):
            if d is not None and v is not None and d <= on_date:
                return float(v)
        raise RuntimeError(f"CSV 中无 {stock_code} 在 {on_date} 之前的有效收盘价")

    def get_stock_history(self, stock_code, start, end):
        return [(d, v) for d, v in self._stock_csv(stock_code) if d is not None and start <= d <= end]

    def get_bond_history(self, bond_code, start, end):
        return [(d, v) for d, v in self._bond_csv(bond_code) if d is not None and start <= d <= end]
