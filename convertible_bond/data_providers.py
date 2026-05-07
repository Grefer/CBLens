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
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any, Callable

import numpy as np


def _retry(call: Callable, attempts: int = 3, delay: float = 0.8, label: str = "akshare"):
    """对瞬态网络错误 (RemoteDisconnected / ConnectionError / timeout) 重试 attempts 次."""
    last_exc: BaseException | None = None
    for i in range(attempts):
        try:
            return call()
        except Exception as e:
            last_exc = e
            msg = str(e).lower()
            transient = (
                "remotedisconnected" in msg
                or "connection aborted" in msg
                or "connection reset" in msg
                or "timeout" in msg
                or "max retries" in msg
            )
            if not transient or i == attempts - 1:
                raise
            import logging as _l
            _l.getLogger(__name__).warning(
                "%s 调用失败 (第 %d/%d 次, %s), %.1fs 后重试",
                label, i + 1, attempts, type(e).__name__, delay)
            time.sleep(delay)
    if last_exc:
        raise last_exc
    raise RuntimeError(f"{label} 重试逻辑未触发任何调用")

logger = logging.getLogger(__name__)


# ── 数据载体 ──────────────────────────────────────────────
@dataclass
class BondTerms:
    """转债条款快照. 字段全部可选 (不同数据源能拉到的字段不一样)."""
    sec_name: str | None = None
    underlying_code: str | None = None
    issue_date: date | None = None
    listing_date: date | None = None             # 上市/挂牌日期
    tradable_date: date | None = None            # 可自由交易/关注日期
    is_tradable: bool | None = None              # valuation_date 视角是否可交易
    trading_status: str | None = None            # tradable/private_pending/pending/unknown
    maturity_date: date | None = None
    face_value: float | None = None              # 例: 100.0
    conversion_price: float | None = None        # 转股价 K
    redemption_price: float | None = None        # 到期赎回价 (例 107.0)
    call_trigger_pct: float | None = None        # 强赎触发 (例 130.0 = 130%K)
    put_trigger_pct: float | None = None         # 回售触发 (例 70.0)
    put_obs_months: float | None = None          # 回售观察期月数 (从发行起算)
    down_reset_block_until: date | None = None   # 公告不下修/不提议期间, 该日前不计下修
    down_reset_p_scale: float | None = None      # 单债下修强度缩放; 0 表示不计下修博弈
    down_reset_note: str | None = None           # 人工记录公告/判断来源
    down_reset_cooldown_months: float | None = None  # 募集说明书"再观察期", 决议不修正后的冻结月数
    coupon_rates: tuple[float, ...] | None = None  # 已解析的小数 (例 (0.003, 0.005, ...))
    close: float | None = None                   # 转债现价
    credit_rating: str | None = None
    outstanding_balance: float | None = None     # 剩余规模 (亿)
    suspension_status: str | None = None          # 停复牌/交易状态补充
    call_status: str | None = None                # 强赎公告/执行状态
    call_announce_date: date | None = None        # 强赎公告日
    call_redemption_date: date | None = None      # 强赎登记/赎回日
    call_no_redemption_until: date | None = None  # "不提前赎回"承诺到期日, 该日前不计强赎博弈
    last_trading_date: date | None = None         # 最后交易日/摘牌前最后可交易日
    delisting_date: date | None = None            # 摘牌日
    underlying_name: str | None = None            # 正股名称
    underlying_status: str | None = None          # 正股 ST/退市风险等结构性状态
    underlying_trade_status: str | None = None    # 正股临时停牌/暂停交易等日级状态
    underlying_pct_change: float | None = None    # 正股最近一日涨跌幅 (%); 用于跌停识别
    bond_turnover_amount: float | None = None     # 转债成交额, 口径由数据源决定


@dataclass
class CashflowSchedule:
    """完整付息计划. 通常比 BondTerms.coupon_rates 更准 (覆盖到期溢价)."""
    coupon_rates: tuple[float, ...] | None = None
    redemption_price: float | None = None
    maturity_date: date | None = None
    cashflows: list[Any] = field(default_factory=list)


# ── 公共工具 ──────────────────────────────────────────────
def to_date(v: Any) -> date | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def _add_months(d: date, months: int) -> date:
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    days = [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
            31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    return date(year, month, min(d.day, days[month - 1]))


_PUBLIC_CB_PREFIXES = {
    "SH": ("110", "111", "113", "118"),
    "SZ": ("123", "127", "128"),
}
_PRIVATE_CB_NAME_RE = re.compile(r"(定向|定转|定\d{2})")


def is_standard_public_cb_code(code: str) -> bool:
    raw = str(code or "").upper().strip()
    if "." not in raw:
        return False
    plain, exch = raw.split(".", 1)
    return exch in _PUBLIC_CB_PREFIXES and any(
        plain.startswith(prefix) for prefix in _PUBLIC_CB_PREFIXES[exch]
    )


def looks_private_cb_name(name: Any) -> bool:
    return bool(name and _PRIVATE_CB_NAME_RE.search(str(name)))


def infer_cb_trading_metadata(
    bond_code: str,
    terms: BondTerms,
    valuation_date: date | None = None,
) -> BondTerms:
    """补齐交易状态字段.

    数据源没有明确字段时采用保守规则:
    - 普通公募可转债: 上市/挂牌后视为可交易
    - 定向/非标准沪深代码段: 若无明确可交易日, 以发行/挂牌后 6 个月作为关注日期
    """
    val_date = valuation_date or date.today()
    listing_date = terms.listing_date or terms.issue_date
    tradable_date = terms.tradable_date
    explicit_is_tradable = terms.is_tradable
    explicit_status = terms.trading_status
    standard_public = is_standard_public_cb_code(bond_code) and not looks_private_cb_name(terms.sec_name)

    if standard_public:
        tradable_date = tradable_date or listing_date
        status = explicit_status or ("tradable" if tradable_date is None or tradable_date <= val_date else "pending")
    else:
        if tradable_date is None and listing_date is not None:
            tradable_date = _add_months(listing_date, 6)
        if tradable_date is None:
            status = explicit_status or "private_unknown"
            is_tradable = explicit_is_tradable if explicit_is_tradable is not None else False
            return replace(
                terms,
                listing_date=listing_date,
                tradable_date=tradable_date,
                is_tradable=is_tradable,
                trading_status=status,
            )
        status = explicit_status or ("private_tradable" if tradable_date <= val_date else "private_pending")

    inferred_is_tradable = tradable_date is None or tradable_date <= val_date
    is_tradable = explicit_is_tradable if explicit_is_tradable is not None else inferred_is_tradable
    return replace(
        terms,
        listing_date=listing_date,
        tradable_date=tradable_date,
        is_tradable=is_tradable,
        trading_status=status,
    )


def parse_coupon_string(raw: Any) -> tuple[float, ...] | None:
    """解析 '0.3,0.5,0.8' 格式的票息字符串 (单位 %)."""
    if raw is None or raw == "":
        return None
    parts = [p.strip().rstrip("%") for p in str(raw).split(",") if p.strip()]
    try:
        return tuple(float(p) / 100.0 for p in parts)
    except ValueError:
        return None


def parse_coupon_chinese_text(text: Any) -> tuple[float, ...] | None:
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


def _latest_finite(values) -> float | None:
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


def finite_float(value: Any) -> float | None:
    """转 float 并过滤 NaN/Inf; 失败或非有限数返回 None.

    与 ``_float_or_none`` 不同, 不解析含非数字字符的字符串 (例如 '--'),
    更适合上层定价/排序逻辑中对已经清洗过的数值做最后一道有限性校验。
    """
    import math
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        text = str(value).replace(",", "").strip()
        if text in {"", "--", "nan", "None"}:
            return None
        return float(text)
    except (TypeError, ValueError):
        return None


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text if text and text not in {"--", "nan", "None"} else None


def _date_or_none(value: Any) -> date | None:
    if value is None:
        return None
    try:
        if isinstance(value, (date, datetime)):
            return to_date(value)
        text = str(value).strip()
        if not text or text in {"--", "nan", "None"}:
            return None
        if re.fullmatch(r"\d{8}", text):
            return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
        return to_date(text)
    except Exception:
        return None


def _wind_table_rows(res) -> list[dict]:
    try:
        fields = [str(f).lower() for f in res.Fields]
        rows = list(zip(*res.Data))
    except Exception:
        return []
    out: list[dict] = []
    for row in rows:
        out.append({field: row[i] for i, field in enumerate(fields)})
    return out


def _announcement_row_from_wind(row: dict) -> dict:
    def pick(*keys):
        for key in keys:
            if key in row and row[key] not in (None, "", "--"):
                return row[key]
        return None

    return {
        "title": pick("title", "announcement_title", "ann_title", "content", "headline"),
        "date": _date_or_none(pick("date", "announcement_date", "ann_date", "publishdate", "publish_date")),
        "url": pick("url", "link", "announcement_url", "ann_url"),
    }


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
    def get_stock_history(self, stock_code: str, start: date, end: date) -> list[tuple[date, float | None]]:
        """正股 [start, end] 区间收盘价时序, 升序. 缺失值用 None."""

    @abstractmethod
    def get_bond_history(self, bond_code: str, start: date, end: date) -> list[tuple[date, float | None]]:
        """转债 [start, end] 区间收盘价时序, 升序. 缺失值用 None."""

    def get_cashflow(self, bond_code: str) -> CashflowSchedule | None:
        """完整付息计划. 默认 None, 让调用方回退到 BondTerms.coupon_rates."""
        return None

    def get_risk_free_rate(self, on_date: date) -> float | None:
        """无风险利率参考值 (%). 默认 None."""
        return None

    def get_admission_status(
        self,
        bond_code: str,
        valuation_date: date,
        base_terms: BondTerms | None = None,
    ) -> BondTerms:
        """拉取主池准入筛选所需的增量状态字段.

        默认退回 ``get_bond_terms``。Wind 等数据源可覆盖该方法, 只刷新停牌、
        强赎、摘牌、正股风险、成交额等字段, 供每日筛选前快速更新。
        """
        return self.get_bond_terms(bond_code, valuation_date)

    def list_bond_announcements(
        self,
        bond_code: str,
        start: date,
        end: date,
    ) -> list[dict]:
        """返回公告列表. 每项至少建议包含 ``title`` 与 ``date``.

        默认返回空列表, 让事件同步层可以在不支持公告接口的数据源上安全跳过。
        """
        return []

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

    def list_tradable_cbs(
        self, on_date: date | None = None,
    ) -> list[tuple[str, str | None]]:
        """返回某日仍在交易的所有可转债 ``(wind_code, sec_name)`` 列表.

        ``sec_name`` 用于上层按名字过滤定向转债; 数据源拿不到时填 ``None``。
        默认抛 NotImplementedError, 各后端按需实现.
        """
        raise NotImplementedError(f"{self.name} 不支持 list_tradable_cbs")


# ── Wind 后端 ──────────────────────────────────────────────
class WindDataProvider(DataProvider):
    """通过 WindPy 拉数据. 需要本机已安装 Wind 终端 + 插件."""
    name = "Wind"

    _BOND_FIELDS = (
        "sec_name", "underlyingcode", "ipo_date", "listdate", "maturitydate",
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

        terms = BondTerms(
            sec_name=d.get("sec_name"),
            underlying_code=d.get("underlyingcode"),
            issue_date=to_date(d.get("ipo_date")),
            listing_date=to_date(d.get("listdate")) or to_date(d.get("ipo_date")),
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
        return infer_cb_trading_metadata(bond_code, terms, valuation_date)

    def get_admission_status(self, bond_code, valuation_date, base_terms=None):
        """增量刷新主池准入状态字段.

        Wind 字段在不同终端/权限下可能存在差异, 因此这里逐个候选字段尝试;
        拿不到的字段保持 None, 不影响已有 cb_data 内容。
        """
        bond_data = self._wss_candidates(
            bond_code,
            {
                "suspension_status": ("trade_status", "suspensionstatus", "suspendtype"),
                "call_status": (
                    "clause_calloption_status",
                    "calloption_status",
                    "redemption_status",
                    "earlyredemption_status",
                ),
                "call_announce_date": (
                    "clause_calloption_announcementdate",
                    "calloption_announcementdate",
                    "redemption_announcementdate",
                ),
                "call_redemption_date": (
                    "clause_calloption_redemptiondate",
                    "calloption_redemptiondate",
                    "redemptiondate",
                ),
                "last_trading_date": ("lasttrade_date", "lasttradingdate", "last_trade_date"),
                "delisting_date": ("delist_date", "delistingdate"),
                "credit_rating": ("creditrating",),
                "outstanding_balance": ("outstandingbalance",),
            },
            valuation_date,
        )
        bond_turnover_amount = self._wsd_latest_number(bond_code, "amt", valuation_date)

        underlying_code = None
        if base_terms is not None:
            underlying_code = base_terms.underlying_code
        if not underlying_code:
            underlying_code = self._wss_value(bond_code, "underlyingcode", valuation_date)

        stock_data = {}
        underlying_pct_change = None
        if underlying_code:
            stock_data = self._wss_candidates(
                str(underlying_code),
                {
                    "underlying_name": ("sec_name",),
                    # 结构性状态: ST/退市风险等, 一般通过专用字段返回, 不会随每日交易切换
                    "underlying_status": ("riskwarning", "st_status", "specialtreatment"),
                    # 日级交易状态: 停牌/暂停交易; trade_status 在 Wind 上对停牌正股返回 "停牌"
                    "underlying_trade_status": ("trade_status", "tradestatus"),
                },
                valuation_date,
            )
            underlying_pct_change = self._wsd_latest_number(
                str(underlying_code), "pct_chg", valuation_date,
            )

        terms = BondTerms(
            suspension_status=_string_or_none(bond_data.get("suspension_status")),
            call_status=_string_or_none(bond_data.get("call_status")),
            call_announce_date=_date_or_none(bond_data.get("call_announce_date")),
            call_redemption_date=_date_or_none(bond_data.get("call_redemption_date")),
            last_trading_date=_date_or_none(bond_data.get("last_trading_date")),
            delisting_date=_date_or_none(bond_data.get("delisting_date")),
            underlying_name=_string_or_none(stock_data.get("underlying_name")),
            underlying_status=_string_or_none(stock_data.get("underlying_status")),
            underlying_trade_status=_string_or_none(stock_data.get("underlying_trade_status")),
            underlying_pct_change=underlying_pct_change,
            bond_turnover_amount=bond_turnover_amount,
            credit_rating=_string_or_none(bond_data.get("credit_rating")),
            outstanding_balance=_float_or_none(bond_data.get("outstanding_balance")),
        )
        return terms

    def _wss_candidates(self, code, candidates, valuation_date):
        return {
            key: self._wss_first_available(code, fields, valuation_date)
            for key, fields in candidates.items()
        }

    def _wss_first_available(self, code, fields, valuation_date):
        for field_name in fields:
            value = self._wss_value(code, field_name, valuation_date)
            if value is not None:
                return value
        return None

    def _wss_value(self, code, field_name, valuation_date):
        w = self._ensure()
        val_str = valuation_date.strftime("%Y%m%d")
        try:
            res = w.wss(code, field_name, f"tradeDate={val_str}")
        except Exception:
            return None
        if getattr(res, "ErrorCode", -1) != 0 or not getattr(res, "Data", None):
            return None
        try:
            value = res.Data[0][0]
        except Exception:
            return None
        return value if value not in ("", "--") else None

    def _wsd_latest_number(self, code, field_name, valuation_date):
        w = self._ensure()
        d = valuation_date.isoformat()
        try:
            res = w.wsd(code, field_name, d, d, "")
        except Exception:
            return None
        if getattr(res, "ErrorCode", -1) != 0 or not getattr(res, "Data", None):
            return None
        try:
            return _float_or_none(res.Data[0][-1])
        except Exception:
            return None

    def list_bond_announcements(self, bond_code, start, end):
        """尝试从 Wind 公告接口拉公告标题.

        Wind 的公告 wset 字段在不同环境中可能有差异; 本实现只做容错尝试,
        失败时返回空列表, 不影响状态字段同步和人工事件表。
        """
        w = self._ensure()
        options = (
            f"windcode={bond_code};startdate={start.isoformat()};enddate={end.isoformat()}",
            f"windcode={bond_code};startDate={start.isoformat()};endDate={end.isoformat()}",
            f"secid={bond_code};startdate={start.isoformat()};enddate={end.isoformat()}",
        )
        datasets = ("announcement", "announcemnent")
        for dataset in datasets:
            for option in options:
                try:
                    res = w.wset(dataset, option)
                except Exception:
                    continue
                if getattr(res, "ErrorCode", -1) != 0 or not getattr(res, "Data", None):
                    continue
                rows = _wind_table_rows(res)
                parsed = [_announcement_row_from_wind(row) for row in rows]
                parsed = [row for row in parsed if row.get("title") and row.get("date")]
                if parsed:
                    return parsed
        return []

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

    def list_tradable_cbs(self, on_date=None):
        """通过 wset('sectorconstituent') 拉某日的"沪深可转债"成分.

        sectorid 'a101020600000000' = 沪深可转债 (含已退市标记的债不入此列).
        返回 ``[(wind_code, sec_name), ...]``; ``sec_name`` 留给上层过滤定向转债。
        """
        w = self._ensure()
        d = (on_date or date.today()).isoformat()
        res = w.wset(
            "sectorconstituent",
            f"date={d};sectorid=a101020600000000;field=wind_code,sec_name",
        )
        if res.ErrorCode != 0:
            raise RuntimeError(f"Wind wset 拉转债成分失败: {res.Data}")
        try:
            fields = [f.lower() for f in res.Fields]
            i_code = fields.index("wind_code")
        except (AttributeError, ValueError):
            return []
        i_name = fields.index("sec_name") if "sec_name" in fields else None
        rows = list(zip(*res.Data))
        out: list[tuple[str, str | None]] = []
        for r in rows:
            code = r[i_code]
            if not code:
                continue
            name = str(r[i_name]) if i_name is not None and r[i_name] else None
            out.append((str(code), name))
        return out


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


def _wind_to_ak_stock_prefixed(wind_code: str) -> str:
    """正股 Wind 格式 (000001.SZ) → akshare 新浪/网易格式 (sz000001)."""
    raw = str(wind_code or "").strip().lower()
    if "." in raw:
        code, exch = raw.split(".", 1)
        return f"{exch}{code}"
    code = raw.zfill(6)
    if code.startswith(("6", "9")):
        return f"sh{code}"
    if code.startswith(("8", "4")):
        return f"bj{code}"
    return f"sz{code}"


def _row_value(row, *keys):
    for key in keys:
        if key in row.index:
            value = row.get(key)
            if value is None:
                continue
            if isinstance(value, float) and np.isnan(value):
                continue
            if str(value).strip() in {"", "--", "nan"}:
                continue
            return value
    return None


def _stock_history_from_df(df) -> list[tuple[date, float | None]]:
    """兼容 akshare 不同历史行情接口的列名差异."""
    if df is None or len(df) == 0:
        return []
    out: list[tuple[date, float | None]] = []
    for _, row in df.iterrows():
        d_raw = _row_value(row, "日期", "date")
        if d_raw is None:
            continue
        try:
            d = to_date(d_raw)
        except Exception:
            continue
        v = _row_value(row, "收盘", "收盘价", "close")
        try:
            close = float(v) if v is not None else None
        except (TypeError, ValueError):
            close = None
        out.append((d, close))
    out.sort(key=lambda item: item[0] or date.min)
    return out


class AkshareDataProvider(DataProvider):
    """通过 akshare 拉数据. 免费, 无 token; 数据来自东财/新浪/集思录, 时效偶有延迟.

    数据组合:
      - bond_zh_cov            列表层: 转股价 / 正股代码 / 现价 / 信用评级 / 发行规模
      - bond_cb_profile_sina   详情层: 到期日 / 起息日 / 利率说明 (中文) / 计息方式
      - stock_zh_a_hist        正股日线历史 (主)
      - stock_zh_a_daily       正股日线历史 (兜底)
      - stock_zh_a_spot_em     正股实时快照 (现价兜底)
      - bond_zh_hs_cov_daily   转债日线历史
      - macro_china_shibor_all Shibor 期限结构

    瞬态网络错误 (RemoteDisconnected / 超时) 自动重试 3 次.
    强赎/回售触发比例、回售观察期月数 akshare 不直接给, 留 None
    (落到 UniversalCBPricer 的默认 1.3 / 0.7 / put_active_years=2).
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
        self._cb_list_cache = None
        self._profile_cache: dict = {}    # bond_code -> profile DataFrame

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
        close_val = _gl("债现价", "现价", "价格")
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

    def _read_price_csv(self, path) -> list[tuple[date, float | None]]:
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
        terms = BondTerms(
            sec_name=d.get("sec_name"),
            underlying_code=d.get("underlying_code"),
            issue_date=to_date(d.get("issue_date")),
            listing_date=to_date(d.get("listing_date")),
            tradable_date=to_date(d.get("tradable_date")),
            is_tradable=d.get("is_tradable"),
            trading_status=d.get("trading_status"),
            maturity_date=to_date(d.get("maturity_date")),
            face_value=d.get("face_value"),
            conversion_price=d.get("conversion_price"),
            redemption_price=d.get("redemption_price"),
            call_trigger_pct=d.get("call_trigger_pct"),
            put_trigger_pct=d.get("put_trigger_pct"),
            put_obs_months=d.get("put_obs_months"),
            down_reset_block_until=to_date(d.get("down_reset_block_until")),
            down_reset_p_scale=d.get("down_reset_p_scale"),
            down_reset_note=d.get("down_reset_note"),
            down_reset_cooldown_months=d.get("down_reset_cooldown_months"),
            coupon_rates=coupons,
            close=d.get("close"),
            credit_rating=d.get("credit_rating"),
            outstanding_balance=d.get("outstanding_balance"),
            suspension_status=d.get("suspension_status"),
            call_status=d.get("call_status"),
            call_announce_date=to_date(d.get("call_announce_date")),
            call_redemption_date=to_date(d.get("call_redemption_date")),
            last_trading_date=to_date(d.get("last_trading_date")),
            delisting_date=to_date(d.get("delisting_date")),
            underlying_name=d.get("underlying_name"),
            underlying_status=d.get("underlying_status"),
            underlying_trade_status=d.get("underlying_trade_status"),
            underlying_pct_change=d.get("underlying_pct_change"),
            bond_turnover_amount=d.get("bond_turnover_amount"),
        )
        return infer_cb_trading_metadata(bond_code, terms, valuation_date)

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


# ── 自动探测 ──────────────────────────────────────────────
def detect_available_providers() -> list[str]:
    """返回当前环境可用的在线 provider 名字列表 (按优先级排序: Wind > akshare).

    仅做 import 检测, 不实例化, 不发起任何网络调用.
    """
    available: list[str] = []
    try:
        import WindPy  # type: ignore[import-not-found]  # noqa: F401
        available.append("Wind")
    except ImportError:
        pass
    try:
        import akshare  # type: ignore[import-not-found]  # noqa: F401
        available.append("akshare")
    except ImportError:
        pass
    return available


def auto_data_provider(prefer: str | None = None) -> DataProvider:
    """选择并实例化当前环境最合适的在线 provider.

    选择顺序: prefer (若指定且可用) → Wind → akshare.
    都不可用时抛 ImportError, 提示用户 `pip install akshare`.
    """
    available = detect_available_providers()
    if not available:
        raise ImportError(
            "未检测到任何可用的在线数据源.\n"
            "  → 推荐: pip install akshare  (免费, 无 token)\n"
            "  → 或在 Wind 终端 '插件管理' 安装 WindPy"
        )
    if prefer and prefer in available:
        choice = prefer
    else:
        choice = available[0]
    if choice == "Wind":
        return WindDataProvider()
    if choice == "akshare":
        return AkshareDataProvider()
    raise ValueError(f"未知 provider: {choice}")
