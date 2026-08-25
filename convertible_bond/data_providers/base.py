"""数据源后端的基础类型与公共工具.

把不依赖具体后端的部分集中到一个模块: ``BondTerms`` / ``CashflowSchedule``
数据载体、``DataProvider`` ABC、日期/票息/代码段判定的公共 helper。
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np
from ..market_time import market_today


# ── 数据载体 ──────────────────────────────────────────────
@dataclass
class BondTerms:
    """转债条款快照. 字段全部可选 (不同数据源能拉到的字段不一样)."""
    sec_name: str | None = None
    underlying_code: str | None = None
    issue_date: date | None = None               # 发行日/起息日 (票息期与应计利息的锚点)
    listing_date: date | None = None             # 上市/挂牌日期 (通常晚于 issue_date 约 2~4 周)
    tradable_date: date | None = None            # 可自由交易/关注日期
    is_tradable: bool | None = None              # valuation_date 视角是否可交易
    trading_status: str | None = None            # tradable/private_pending/pending/unknown
    maturity_date: date | None = None
    face_value: float | None = None              # 例: 100.0
    conversion_price: float | None = None        # 转股价 K
    redemption_price: float | None = None        # 到期赎回价 (例 107.0)
    down_reset_trigger_pct: float | None = None  # 下修触发 (例 85.0 = 85%K)
    call_trigger_pct: float | None = None        # 强赎触发 (例 130.0 = 130%K)
    put_trigger_pct: float | None = None         # 回售触发 (例 70.0)
    put_obs_months: float | None = None          # 回售观察期月数 (从发行起算)
    putback_start_date: date | None = None       # 已公告回售申报起始日
    putback_end_date: date | None = None         # 已公告回售申报截止日
    putback_price: float | None = None           # 已公告回售价格 (元/张, 含息口径)
    conversion_suspension_start_date: date | None = None  # 已公告暂停转股起始日
    conversion_suspension_end_date: date | None = None    # 已公告暂停转股截止/恢复日
    conversion_suspension_status: str | None = None       # 暂停转股/恢复转股等状态
    down_reset_block_until: date | None = None   # 公告不下修/不提议期间, 该日前不计下修
    down_reset_p_scale: float | None = None      # 单债下修强度缩放; 0 表示不计下修博弈
    down_reset_note: str | None = None           # 人工记录公告/判断来源
    down_reset_cooldown_months: float | None = None  # 募集说明书"再观察期", 决议不修正后的冻结月数
    coupon_rates: tuple[float, ...] | None = None  # 已解析的小数 (例 (0.003, 0.005, ...))
    close: float | None = None                   # 转债现价
    credit_rating: str | None = None
    credit_rating_outlook: str | None = None     # 评级展望: 稳定/负面/正面等
    credit_watch_status: str | None = None       # 评级观察/关注状态
    outstanding_balance: float | None = None     # 剩余规模 (亿)
    suspension_status: str | None = None          # 停复牌/交易状态补充
    call_status: str | None = None                # 强赎公告/执行状态
    call_announce_date: date | None = None        # 强赎公告日
    call_redemption_date: date | None = None      # 强赎登记/赎回日
    call_redemption_price: float | None = None    # 已公告强赎价格 (元/张, 含息口径)
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
    rates = re.findall(r"(\d+\.?\d*)\s*%", s)
    if not rates:
        return None
    try:
        return tuple(float(r) / 100.0 for r in rates)
    except ValueError:
        return None


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


# 标准公募转债"已发行未上市"的判定窗口 (自起息日算起).
# 起息日 (carrydate) → 上市日 (ipo_date) 实测中位 25 天, 但有长尾; 超过这个天数
# 仍拿不到上市日, 更可能是数据缺口而不是真的没挂牌 —— 此时保持旧的保守判定
# (按可交易处理), 避免把有数据缺口的老债误杀出主池。
UNLISTED_MAX_DAYS = 180


def _terms_field(terms: Any, name: str):
    """兼容 BondTerms / dict / None 的字段读取."""
    if terms is None:
        return None
    if isinstance(terms, dict):
        return terms.get(name)
    return getattr(terms, name, None)


def is_issued_pending_listing(
    bond_code: str,
    terms: Any,
    valuation_date: date | None = None,
) -> bool:
    """标准公募转债已过起息日, 但数据源仍未给出上市日 → 已发行未上市.

    Wind 的 ``ipo_date`` (= ``listing_date``) 只有挂牌后才有值, 所以"缺上市日 +
    起息日就在不久前"基本等价于"债已经发出去了但还没挂牌交易"。

    判定**只看 issue_date / listing_date**, 不看 ``tradable_date`` /
    ``trading_status`` / ``is_tradable``: 公募转债这三个字段数据源根本不提供,
    它们是 :func:`infer_cb_trading_metadata` 的派生产物并被写回 cb_data, 缓存里
    读到的值只是上一次推断的结果, 不能当独立证据用 (否则一旦推断错过一次,
    错误状态会被自己"确认"下来, 永远翻不回来)。
    """
    if _terms_field(terms, "listing_date") is not None:
        return False
    issue_date = _terms_field(terms, "issue_date")
    if not isinstance(issue_date, date):
        return False
    name = _terms_field(terms, "sec_name")
    if not (is_standard_public_cb_code(bond_code) and not looks_private_cb_name(name)):
        return False
    val_date = valuation_date or market_today()
    if issue_date > val_date:
        return True
    return (val_date - issue_date).days <= UNLISTED_MAX_DAYS


def infer_cb_trading_metadata(
    bond_code: str,
    terms: BondTerms,
    valuation_date: date | None = None,
) -> BondTerms:
    """补齐交易状态字段.

    数据源没有明确字段时采用保守规则:
    - 普通公募可转债: 上市/挂牌后视为可交易
    - 定向/非标准沪深代码段: 若无明确可交易日, 以发行/挂牌后 6 个月作为关注日期

    注: 缺上市日时用发行日当"可交易起点"的锚, 但**不回填** ``listing_date`` —
    已发行未上市的新债上市日就该是空的, 回填会让该字段谎报成起息日。

    公募新债 (已发行未上市) 单独一档: 起息日**不是**可交易起点, 拿它当锚会把
    刚发行还没挂牌的债判成 tradable, 于是新债既进不了"扫新债", 又会带着空市价
    混进主池。这一档强制 ``tradable_date=None / is_tradable=False /
    trading_status="pending"``, 并且不受缓存里旧派生值影响 (见
    :func:`is_issued_pending_listing`)。
    """
    val_date = valuation_date or market_today()
    listing_anchor = terms.listing_date or terms.issue_date
    tradable_date = terms.tradable_date
    explicit_is_tradable = terms.is_tradable
    explicit_status = terms.trading_status
    standard_public = is_standard_public_cb_code(bond_code) and not looks_private_cb_name(terms.sec_name)

    if standard_public:
        if is_issued_pending_listing(bond_code, terms, val_date):
            return replace(
                terms,
                tradable_date=None,
                is_tradable=False,
                trading_status="pending",
            )
        tradable_date = tradable_date or listing_anchor
        if tradable_date is not None and tradable_date <= val_date:
            # 已挂牌: 缓存里的 is_tradable / trading_status 是**本函数自己上一次的派生产物**
            # (Wind ``get_admission_status`` 对这两个字段显式返回 None, 公募转债数据源根本
            # 不提供), 不能当独立证据 —— 否则新债在"已发行未上市"那一档留下的
            # pending/False 会被自己确认下来, 永远翻不回。这正是
            # :func:`is_issued_pending_listing` 文档里点名的自我确认陷阱, 只是那时只堵了
            # 判定侧、没堵回填侧。实测让派克转债 / 中仑转债两只上市首日分别成交 2.57 亿 /
            # 12.95 亿的新债被准入判成"不可交易"。
            if explicit_status == "pending":
                explicit_status = None
            if explicit_is_tradable is False:
                explicit_is_tradable = None
        status = explicit_status or ("tradable" if tradable_date is None or tradable_date <= val_date else "pending")
    else:
        if tradable_date is None and listing_anchor is not None:
            tradable_date = _add_months(listing_anchor, 6)
        if tradable_date is None:
            status = explicit_status or "private_unknown"
            is_tradable = explicit_is_tradable if explicit_is_tradable is not None else False
            return replace(
                terms,
                tradable_date=tradable_date,
                is_tradable=is_tradable,
                trading_status=status,
            )
        status = explicit_status or ("private_tradable" if tradable_date <= val_date else "private_pending")

    inferred_is_tradable = tradable_date is None or tradable_date <= val_date
    is_tradable = explicit_is_tradable if explicit_is_tradable is not None else inferred_is_tradable
    return replace(
        terms,
        tradable_date=tradable_date,
        is_tradable=is_tradable,
        trading_status=status,
    )


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

    def get_stock_dividend_yield(self, stock_code: str, on_date: date) -> float | None:
        """正股股息率参考值 (%). 默认 None, 上层回退到 q=0."""
        return None

    @abstractmethod
    def get_bond_history(self, bond_code: str, start: date, end: date) -> list[tuple[date, float | None]]:
        """转债 [start, end] 区间收盘价时序, 升序. 缺失值用 None."""

    def get_cashflow(self, bond_code: str) -> CashflowSchedule | None:
        """完整付息计划. 默认 None, 让调用方回退到 BondTerms.coupon_rates."""
        return None

    def terms_as_of(self, bond_code: str, valuation_date: date) -> date | None:
        """``get_bond_terms`` 返回的那份条款快照的截止日。

        条款 patch 只有在这一天**之后**生效才带来新信息 —— 更早的已经含在快照里了。
        返回 None 表示"未知", 此时上层不做裁剪 (保持旧行为)。
        实时 provider 应返回条款的抓取日, 历史 provider 应返回所用快照的日期。
        """
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
        """拉取主池筛选和复核所需的增量状态字段.

        默认退回 ``get_bond_terms``。Wind 等数据源可覆盖该方法, 只刷新停牌、
        强赎、摘牌、正股风险、成交额等字段, 供每日批量前快速更新。
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
