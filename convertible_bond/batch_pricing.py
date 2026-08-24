"""
批量定价的应用层辅助函数.

这里放 GUI / CLI 都能复用的薄业务逻辑:
  - 解析用户输入的转债代码列表
  - 从 cb_data 静态信息缓存获取默认批量转债池
  - 构造带条款缓存的 DataProvider
  - 汇总与导出 batch_price_from_provider 的结果
"""
from __future__ import annotations

import csv
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from datetime import date, datetime, timedelta
from collections.abc import Iterable, Sequence
from typing import Any

from .cache import CachedBondDataProvider
from .data_providers import (
    AkshareDataProvider,
    CSVDataProvider,
    DataProvider,
    WindDataProvider,
    finite_float,
    infer_cb_trading_metadata,
    is_issued_pending_listing,
    is_standard_public_cb_code,
    looks_private_cb_name,
    to_date,
)
from .cb_events import CBEventStore, project_events_path
from .historical_terms import TermsPatchStore, project_terms
from .paths import data_path
from .market_time import market_today


BATCH_RESULT_COLUMNS = [
    "bond_code",
    "bond_name",
    "stock_code",
    "S0",
    "K",
    "sigma",
    "theoretical_price",
    "market_price",
    "deviation",
    "undervaluation_rate",
    "credit_rating",
    "status",
    "parity",
    "conversion_premium",
    "model_premium_to_parity",
    "opportunity_score",
    "confidence",
    "risk_tags",
    "outstanding_balance",
    "days_to_last_trading",
    "model_signal_status",
    "no_down_price",
    "down_reset_uplift",
    "implied_p_down",
    "effective_p_down_1y_prob",
    "implied_p_down_1y_prob",
    "p_down_gap",
    "down_reset_probability_gap",
    "down_reset_sensitivity",
    "down_reset_edge_value",
    "down_reset_edge_pct",
    "down_reset_robust_edge_value",
    "down_reset_edge_scenario_range",
    "down_reset_edge_worst_sigma",
    "down_reset_edge_worst_spread",
    "down_reset_edge_scenario_count",
    "pde_down_reset_robust_status",
    "pde_down_reset_signal_status",
    "sensitivity_status",
    "review_bucket",
    "review_notes",
]

_CODE_SPLIT_RE = re.compile(r"[\s,;，；]+")
_HEADER_TOKENS = {"code", "bond_code", "证券代码", "转债代码", "代码"}
BATCH_RESULT_META_KEY = "_meta"
LOW_RATING_PREFIXES = ("A", "BBB", "BB", "B", "CCC", "CC", "C")
BATCH_REVIEW_VIEWS = ("综合机会", "低估候选", "转股折价", "需复核")
# ── 标签维度 ──────────────────────────────────────────────────────────────
# 标签混了四类**性质不同**的关切, 却挤在一个扁平集合里驱动四个消费者 (展示 / 置信度 /
# 批量页视图 / 策略 exclude_risk_tags), 于是调一个阈值会同时穿透四层。先给每个标签
# 归维, 后续每个消费者才能各取所需。
#
#   数据质量   这个数算不出来或输入缺失      → "别信这一行"
#   模型适用性 数算得出来, 但超出模型能力边界 → "别信这个价"
#   标的风险   数是对的, 债本身有风险        → "看清楚再买"
#   可交易性   买不到 / 拿不住              → 结构性排除
#   机会信号   不是风险, 是提示             → 本就不该混在 risk_tags 里
#
# 实测今日主池 280 只的维度分布: 数据质量 1 只(0%) / 模型适用性 202(72%) /
# 标的风险 99(35%) / 可交易性 3(1%) / 机会信号 5(2%)。数据质量维度几乎空了 —— 那是
# 条款与事件层清理干净之后的结果; 而"一个在 72% 的债上都亮的标签描述的是市场, 不是这只债"。
DIM_DATA = "数据质量"
DIM_MODEL = "模型适用性"
DIM_ISSUER = "标的风险"
DIM_TRADABILITY = "可交易性"
DIM_OPPORTUNITY = "机会信号"

RISK_TAG_DIMENSION: dict[str, str] = {
    "数据缺口": DIM_DATA, "无偏差": DIM_DATA, "无HV": DIM_DATA, "无余额": DIM_DATA,
    "无评级": DIM_DATA, "无市价": DIM_DATA, "理论价异常": DIM_DATA,

    "高HV": DIM_MODEL, "较高HV": DIM_MODEL, "模型溢价高": DIM_MODEL,
    "模型高估离群": DIM_MODEL, "下修贡献高": DIM_MODEL, "下修减值": DIM_MODEL,
    "偏差异常": DIM_MODEL,                      # legacy 名, 旧缓存里还有

    "低评级": DIM_ISSUER, "小余额": DIM_ISSUER, "临近摘牌线": DIM_ISSUER,
    "触及摘牌线": DIM_ISSUER, "短久期": DIM_ISSUER, "近到期": DIM_ISSUER,
    "极小余额": DIM_ISSUER,                     # legacy 名

    "余额清零": DIM_TRADABILITY, "正股风险": DIM_TRADABILITY, "正股停牌": DIM_TRADABILITY,
    "转债停牌": DIM_TRADABILITY, "正股跌停": DIM_TRADABILITY, "临近摘牌": DIM_TRADABILITY,
    "余额异常": DIM_TRADABILITY,                # legacy 名

    "转股折价": DIM_OPPORTUNITY, "贴近转股价值": DIM_OPPORTUNITY,
    "模型低估": DIM_OPPORTUNITY, "深度低估待核": DIM_OPPORTUNITY,
}


# 批量页视图/分桶用的"拦截集": **只有这两个维度**才是"这一行现在不能用, 得先去做点什么"。
# 模型适用性 (M) 与标的风险 (R) 不在内 —— 它们是永久属性, 查完还是那样, 属于该看见但
# 不该拦路的信息。实测: 用旧的扁平硬标签集时需复核 79%, 换成本集合后 6 只, M 单列 142 只。
#
# ⚠️ 与 LEGACY_STRATEGY_EXCLUDE_TAGS 是**两回事**: 那个是策略层默认排除集 (冻结),
# 这个是批量页展示口径。默认 selection_view="综合机会" 不过滤, 所以改这里不动策略默认行为;
# 但用户显式选「低估候选/需复核」作为 selection_view 时口径会变。
def tags_in(*dimensions: str) -> frozenset[str]:
    """取这些维度下的全部标签名。消费者按维度取子集, 而不是各自硬编码一份清单。"""
    wanted = set(dimensions)
    return frozenset(t for t, d in RISK_TAG_DIMENSION.items() if d in wanted)


_BLOCKING_TAGS = tags_in(DIM_DATA, DIM_TRADABILITY)


HARD_REVIEW_TAGS = {
    "高HV", "余额清零", "触及摘牌线", "临近摘牌线", "小余额", "短久期",
    "低评级", "模型溢价高", "数据缺口", "无市价", "理论价异常",
    "正股风险", "正股停牌", "转债停牌", "正股跌停", "偏差异常", "模型高估离群",
    # legacy: 旧批量缓存与旧策略快照里存的是这两个名字, 保留以免旧数据静默失去硬标签
    "极小余额", "余额异常",
}

# 策略层 ScoreStrategyConfig.exclude_risk_tags 的默认值, **逐字冻结**。
#
# 它曾经写成 tuple(sorted(HARD_REVIEW_TAGS)) —— 那是整个标签整合的死结: 只要这个引用
# 成立, 任何为了改批量页展示而增删 HARD_REVIEW_TAGS 的动作都自动变成**默认选债行为变更**,
# 要过 docs/research 的治理三条 (机制 / 跨 regime / 跨频率)。而这个集合极其敏感, 实测
# 今日截面: 默认候选池 59 只; 改成只排"数据质量+可交易性" → 262 只; 单去掉「偏差异常」
# → 125 只; 去掉「模型溢价高」→ 94 只。量级变更, 绝不能作为展示层重构的副作用发生。
#
# 因此把它冻结在这里, 与 HARD_REVIEW_TAGS 解耦。要改它请单独立项并走完治理三条。
LEGACY_STRATEGY_EXCLUDE_TAGS = frozenset({
    "高HV", "余额清零", "触及摘牌线", "临近摘牌线", "小余额", "短久期",
    "低评级", "模型溢价高", "数据缺口", "无市价", "理论价异常",
    "正股风险", "正股停牌", "转债停牌", "正股跌停", "偏差异常", "模型高估离群",
    "极小余额", "余额异常",
})
# 负 uplift 的告警阈值。定位是**安全网**而不是常规标签: 同网格后预期命中数≈0,
# 一旦亮起就说明模型或数值出了需要人看的事。取 0.5% 是为了压住残余数值噪声,
# 与正向 8% 不对称 —— 正向是"下修贡献大到影响估值", 负向是"出现了不该出现的东西"。
DOWN_RESET_DRAG_THRESHOLD = 0.005

# 只影响批量页复核视图, **不进** HARD_REVIEW_TAGS —— 后者是策略层
# ScoreStrategyConfig.exclude_risk_tags 的默认值, 动它就是默认选债行为变更。
# 「临近摘牌」是确定性的退出安排, 属于人该看一眼的事; 但要不要因此不买, 由策略参数
# 决定, 不在这里替用户决定。
REVIEW_ONLY_TAGS = {"临近摘牌"}
# 交易所停止交易的法定线: 未转股余额少于 3,000 万元 (0.3 亿) 触发停止交易安排。
# 余额档按这条线划, 而不是按与法规无关的 0.5。
BALANCE_DELISTING_LINE = 0.3
# 已公告最后交易日且落在该窗口内 → 打「临近摘牌」提示标签 (非硬标签, 只提高可见度)
DELISTING_WARNING_DAYS = 30
# 偏离**本期市场中位**超过该阈值时打 "偏差异常" 标签 —— 多数情况是市价/正股价不同日、
# 强赎/停牌未应用、转股价未刷新等数据问题, 而非真正的低估机会。
#
# 判据锚在中位数而不是 0, 是因为模型对全市场有一个**系统性水平偏移**: 干净数据下实测
# 中位偏差 +18.8%、92% 的券市价高于理论价 (这是模型的已知缺口, 不是数据问题)。用绝对
# 阈值等于把中位数附近的券也判成异常 —— 实测命中 45% (126/280), 早已丧失识别离群的能力;
# 锚到中位后命中 12%, 才回到这个标签自己声明的用途。
DEVIATION_ANOMALY_THRESHOLD = 0.20
# 样本太少时中位数本身不稳 (关注池/新债往往只有个位数行), 退回绝对阈值。
_DEVIATION_MEDIAN_MIN_SAMPLE = 30
DEFAULT_DELIST_WINDOW_DAYS = 0
# 默认不再按余额硬剔除。全库回填摘牌元数据后实测: 关掉该门槛主池 270 → 270,
# 独立贡献为 0 —— 它此前 99% 的作用是替缺失的 delisting_date 兜底 (被它剔除的 225 只
# 里 223 只余额恰为 0 的已退市券), 而那个职责现在由 delisting_date / last_trading_date
# 的日期判据接管, 剔除理由也从"余额过小"变成诚实的"已退市"。余额本身改由风险标签表达。
# 字段与语义保留, 想恢复硬过滤填个数值即可。
DEFAULT_MIN_OUTSTANDING_BALANCE: float | None = None
DEFAULT_MIN_CREDIT_RATING: str | None = "A+"
_UNDERLYING_ST_KEYWORDS = ("ST", "*ST", "退市风险", "风险警示", "暂停上市", "终止上市", "退市")
_UNDERLYING_SUSPENSION_KEYWORDS = ("停牌", "暂停交易", "停止交易")
_RATING_SCORES = {
    "C": 0,
    "CC": 1,
    "CCC": 2,
    "B-": 3,
    "B": 4,
    "B+": 5,
    "BB-": 6,
    "BB": 7,
    "BB+": 8,
    "BBB-": 9,
    "BBB": 10,
    "BBB+": 11,
    "A-": 12,
    "A": 13,
    "A+": 14,
    "AA-": 15,
    "AA": 16,
    "AA+": 17,
    "AAA": 18,
}


@dataclass(frozen=True)
class AdmissionFilterConfig:
    """批量定价主池公开交易过滤参数.

    当前硬剔除优先保证转债本身能公开交易 (退市/摘牌/停牌/到期/未上市/定向),
    并默认剔除正股 ST/停牌与低评级。**余额默认不再硬剔除** (见
    ``DEFAULT_MIN_OUTSTANDING_BALANCE``), 改由「余额清零 / 触及摘牌线 /
    临近摘牌线 / 小余额」风险标签表达; 高 HV 同理只有定价后才能识别。
    需要恢复余额硬过滤时给 ``min_outstanding_balance`` 填个数值即可。
    """

    delist_window_days: int = DEFAULT_DELIST_WINDOW_DAYS
    min_outstanding_balance: float | None = DEFAULT_MIN_OUTSTANDING_BALANCE
    min_credit_rating: str | None = DEFAULT_MIN_CREDIT_RATING
    min_turnover_amount: float | None = None


@dataclass(frozen=True)
class AdmissionFilterResult:
    """单只转债主池公开交易过滤结果."""

    bond_code: str
    accepted: bool
    reason: str | None = None


def project_batch_cache_path() -> Path:
    """项目级批量定价结果缓存路径."""
    return data_path("batch_pricing_cache.json")


def parse_bond_codes(raw: str | Iterable[str]) -> list[str]:
    """解析用户输入 / CSV 单元格中的转债代码, 去重并保持原始顺序."""
    if isinstance(raw, str):
        text = "\n".join(line for line in raw.splitlines() if not line.strip().startswith("#"))
        chunks = _CODE_SPLIT_RE.split(text)
    else:
        chunks = []
        for item in raw:
            text = "\n".join(
                line for line in str(item).splitlines()
                if not line.strip().startswith("#")
            )
            chunks.extend(_CODE_SPLIT_RE.split(text))

    codes: list[str] = []
    seen = set()
    for chunk in chunks:
        code = chunk.strip().strip('"').strip("'")
        if not code or code.startswith("#"):
            continue
        if code.lower() in _HEADER_TOKENS:
            continue
        code = code.upper()
        if code not in seen:
            seen.add(code)
            codes.append(code)
    return codes


def list_batch_codes_from_cache(
    terms_cache,
    *,
    include_nonstandard: bool = False,
    admission_config: AdmissionFilterConfig | None = None,
    on_date: date | None = None,
) -> list[str]:
    """返回 cb_data 静态信息缓存中的批量定价代码池.

    默认只返回当前 A 股普通公募可转债常见代码段:
    - SH: 110/111/113/118
    - SZ: 123/127/128

    Wind 的"沪深可转债"成分有时会混入 124xxx/1108xx 等定向转债、NQ/BJ
    债券或退市债。这些标的即使有条款和参考价格，也不适合参与主批量排序。
    """
    if terms_cache is None or not hasattr(terms_cache, "list_bonds"):
        return []
    codes = list(terms_cache.list_bonds())
    if include_nonstandard:
        return codes
    check_date = on_date or market_today()
    patch_store, event_store = _admission_projection_stores()
    return [
        code for code in codes
        if batch_pricing_exclusion_reason(
            code,
            _project_terms_for_admission(
                code,
                _cached_terms(terms_cache, code),
                check_date,
                patch_store=patch_store,
                event_store=event_store,
                terms_as_of=_terms_cache_as_of(terms_cache, code),
            ),
            on_date=check_date,
            admission_config=admission_config,
        ) is None
    ]


def split_batch_codes_from_cache(
    terms_cache,
    *,
    admission_config: AdmissionFilterConfig | None = None,
    on_date: date | None = None,
) -> tuple[list[str], list[tuple[str, str]]]:
    """把缓存代码池拆成 (可批量定价代码, 被过滤代码及原因)."""
    if terms_cache is None or not hasattr(terms_cache, "list_bonds"):
        return [], []
    check_date = on_date or market_today()
    patch_store, event_store = _admission_projection_stores()
    kept: list[str] = []
    excluded: list[tuple[str, str]] = []
    for code in terms_cache.list_bonds():
        terms = _project_terms_for_admission(
            code,
            _cached_terms(terms_cache, code),
            check_date,
            patch_store=patch_store,
            event_store=event_store,
            terms_as_of=_terms_cache_as_of(terms_cache, code),
        )
        reason = batch_pricing_exclusion_reason(
            code,
            terms,
            on_date=check_date,
            admission_config=admission_config,
        )
        if reason is None:
            kept.append(code)
        else:
            excluded.append((code, reason))
    return kept, excluded


def screen_batch_pool_from_cache(
    terms_cache,
    *,
    admission_config: AdmissionFilterConfig | None = None,
    on_date: date | None = None,
) -> dict:
    """返回主池公开交易筛选报告.

    报告用于 GUI/CLI 在定价前展示数据池质量，结构为:
    ``{accepted, excluded, total, n_accepted, n_excluded, excluded_by_reason}``。
    """
    accepted, excluded = split_batch_codes_from_cache(
        terms_cache,
        admission_config=admission_config,
        on_date=on_date,
    )
    return {
        "accepted": accepted,
        "excluded": excluded,
        "total": len(accepted) + len(excluded),
        "n_accepted": len(accepted),
        "n_excluded": len(excluded),
        "excluded_by_reason": summarize_exclusions(excluded),
    }


def summarize_exclusions(excluded: Sequence[tuple[str, str]]) -> dict[str, int]:
    """按剔除原因统计数量, 保持首次出现顺序."""
    summary: dict[str, int] = {}
    for _, reason in excluded:
        summary[reason] = summary.get(reason, 0) + 1
    return summary


def batch_pricing_exclusion_reason(
    code: str,
    terms: Any = None,
    *,
    on_date: date | None = None,
    delist_window_days: int = DEFAULT_DELIST_WINDOW_DAYS,
    min_outstanding_balance: float | None = DEFAULT_MIN_OUTSTANDING_BALANCE,
    min_credit_rating: str | None = DEFAULT_MIN_CREDIT_RATING,
    min_turnover_amount: float | None = None,
    admission_config: AdmissionFilterConfig | None = None,
) -> str | None:
    """返回批量主池过滤原因; None 表示可以进入主批量定价.

    这里只做进入主批量候选前的硬条件判断: 代码段/交易所、定向标识、
    是否已进入可交易窗口、转债自身停牌、最后交易/摘牌/到期日, 以及
    默认不适合直接作为买入信号的正股 ST/停牌与低评级标的 (余额默认不硬剔除)。
    """
    if admission_config is not None:
        min_outstanding_balance = admission_config.min_outstanding_balance
        min_credit_rating = admission_config.min_credit_rating
        min_turnover_amount = admission_config.min_turnover_amount
    check_date = on_date or market_today()
    terms = _with_inferred_trading_metadata(code, terms, check_date)
    tradable_date = _terms_date(terms, "tradable_date")
    is_tradable = _terms_value(terms, "is_tradable")

    raw_code = str(code or "").upper().strip()
    if "." not in raw_code:
        return "代码缺少交易所后缀"
    _plain, exch = raw_code.split(".", 1)
    if exch not in {"SH", "SZ"}:
        return "非沪深主板/深市可转债"
    delisting_date = _terms_date(terms, "delisting_date")
    if delisting_date and delisting_date <= check_date:
        return "已退市"
    last_trading_date = _terms_date(terms, "last_trading_date")
    if last_trading_date and last_trading_date < check_date:
        return "已过最后交易日"
    maturity_date = _terms_date(terms, "maturity_date")
    if maturity_date and maturity_date <= check_date:
        return "已到期"
    status_reason = _public_trading_status_reason(terms)
    if status_reason:
        return status_reason
    if is_issued_pending_listing(raw_code, terms, check_date):
        # 已发行但还没挂牌: 买不到, 也没有市价可比 —— 归"扫新债"关注池而不是主池
        return "已发行未上市"
    if is_tradable is False:
        return "不可交易"
    if _underlying_has_st_risk(terms):
        return "正股 ST/退市风险"
    if _underlying_suspended(terms):
        return "正股停牌"
    turnover = finite_float(_terms_value(terms, "bond_turnover_amount"))
    if min_turnover_amount is not None and turnover is not None and turnover < min_turnover_amount:
        return "成交额过低"
    balance = finite_float(_terms_value(terms, "outstanding_balance"))
    if (
        min_outstanding_balance is not None
        and balance is not None
        and balance < min_outstanding_balance
    ):
        return "余额过小"
    rating = _terms_value(terms, "credit_rating")
    if min_credit_rating and _rating_below(rating, min_credit_rating):
        return "评级过低"

    name = _terms_value(terms, "sec_name") or _terms_value(terms, "bond_name")
    standard_public = is_standard_public_cb_code(raw_code) and not looks_private_cb_name(name)
    if standard_public:
        if tradable_date and tradable_date > check_date:
            return f"{(tradable_date - check_date).days} 日后可交易"
        return None

    if tradable_date:
        if tradable_date > check_date:
            return f"{(tradable_date - check_date).days} 日后可交易"
        if not is_standard_public_cb_code(raw_code):
            return "非普通公募转债代码段"
        if looks_private_cb_name(name):
            return "定向转债/非公开交易标的"
        return "非公开交易标的"
    if is_tradable is True:
        if not is_standard_public_cb_code(raw_code):
            return "非普通公募转债代码段"
        if looks_private_cb_name(name):
            return "定向转债/非公开交易标的"
        return "非公开交易标的"
    if not is_standard_public_cb_code(raw_code):
        return "非普通公募转债代码段"
    if looks_private_cb_name(name):
        return "定向转债/暂不可自由交易"
    return None


def _public_trading_status_reason(terms: Any) -> str | None:
    status = " ".join(
        str(_terms_value(terms, key) or "")
        for key in ("trading_status", "suspension_status")
    ).upper()
    if not status:
        return None
    if any(keyword in status for keyword in ("退市", "摘牌", "终止上市")):
        return "已退市"
    if "暂停上市" in status:
        return "暂停上市"
    if any(keyword in status for keyword in ("停牌", "暂停交易", "停止交易")):
        return "停牌/暂停交易"
    if "违约" in status:
        return "违约/异常状态"
    return None


def _text_contains_any(text: str, keywords: Sequence[str]) -> bool:
    upper = str(text or "").upper()
    return any(keyword.upper() in upper for keyword in keywords)


def _underlying_has_st_risk(terms: Any) -> bool:
    name = str(_terms_value(terms, "underlying_name") or "")
    status = str(_terms_value(terms, "underlying_status") or "")
    return _text_contains_any(f"{name} {status}", _UNDERLYING_ST_KEYWORDS)


def _underlying_suspended(terms: Any) -> bool:
    trade_status = str(_terms_value(terms, "underlying_trade_status") or "")
    status = str(_terms_value(terms, "underlying_status") or "")
    return _text_contains_any(f"{trade_status} {status}", _UNDERLYING_SUSPENSION_KEYWORDS)


def _underlying_limit_down_threshold(stock_code: Any) -> float:
    """正股跌停阈值 (%, 负数). 创业板/科创板 20%, 其余主板 10%.

    ST 正股的 5% 限制不在此处理: ST 风险进入复核标签, 不作为主池硬剔除。
    阈值留 0.5% 余量, 避免数据源 pct_chg 取整偏差导致漏识别。
    """
    raw = str(stock_code or "").upper().strip()
    if "." in raw:
        plain, _, _ = raw.partition(".")
    else:
        plain = raw
    if plain.startswith(("30", "68")):
        return -19.5
    return -9.5


def _underlying_at_limit_down(terms_or_row: Any, stock_code: Any = None) -> bool:
    pct = finite_float(_terms_value(terms_or_row, "underlying_pct_change"))
    if pct is None:
        return False
    code = stock_code if stock_code is not None else _terms_value(terms_or_row, "underlying_code") or _terms_value(terms_or_row, "stock_code")
    return pct <= _underlying_limit_down_threshold(code)


def _rating_below(rating: Any, minimum: str) -> bool:
    score = _rating_score(rating)
    min_score = _rating_score(minimum)
    return score is not None and min_score is not None and score < min_score


def _rating_score(rating: Any) -> int | None:
    if rating is None:
        return None
    raw = str(rating).upper().replace(" ", "").strip()
    if not raw:
        return None
    for label in sorted(_RATING_SCORES, key=len, reverse=True):
        if raw == label or raw.startswith(label):
            return _RATING_SCORES[label]
    return None


def average_rating_label(ratings: Iterable[Any]) -> str | None:
    """对一组评级 (字符串或可转为字符串的对象) 求平均, 返回最接近的评级标签.

    无法识别的评级会被忽略; 全部识别失败时返回 None。供 GUI 汇总使用,
    避免外部模块直接依赖 ``_RATING_SCORES`` 私有字典。
    """
    scores = [s for s in (_rating_score(r) for r in ratings) if s is not None]
    if not scores:
        return None
    avg = sum(scores) / len(scores)
    return min(_RATING_SCORES.items(), key=lambda kv: abs(kv[1] - avg))[0]


def list_upcoming_tradable_from_cache(
    terms_cache,
    *,
    on_date: date | None = None,
    window_days: int = 7,
) -> list[dict]:
    """列出尚未开始交易的**普通公募**新债 (含已发行未上市).

    两类都算"新债":

    1. **已定上市日**: ``trading_status="pending"`` 且 tradable_date 落在
       ``[on_date, on_date + window_days]`` 窗口内。
    2. **已发行未上市**: 数据源还没给出上市日 (``listing_date`` 为空) 但已过
       起息日 —— 这类债连"哪天能买"都还不知道, 却正是最需要提前算理论价的一批。
       它们的 ``tradable_date`` / ``days_to_trade`` 返回 ``None`` (待定), 不受
       window_days 约束。

    仅包含公开交易标的。定向/私募转债 (非标准公募代码段、定转命名或
    private 交易状态) 不进"扫新债"关注池: 它们无集中竞价交易、常无
    上市正股关联 (PDE 模型不适用), 主池准入也会剔除 — 进池只产生噪音。
    """
    if terms_cache is None or not hasattr(terms_cache, "list_bonds"):
        return []
    check_date = on_date or market_today()
    end_date = check_date + timedelta(days=max(0, int(window_days)))
    rows: list[dict] = []
    for code in terms_cache.list_bonds():
        terms = _with_inferred_trading_metadata(code, _cached_terms(terms_cache, code), check_date)
        if terms is None:
            continue
        tradable_date = _terms_date(terms, "tradable_date")
        name = _terms_value(terms, "sec_name")
        trading_status = _terms_value(terms, "trading_status") or ""
        is_std_public = is_standard_public_cb_code(code) and not looks_private_cb_name(name)

        # 普通公募新债: 尚未开始交易 (pending)。定向/私募券在此剔除;
        # private_pending 等非公开状态由 != "pending" 一并排除。
        if not is_std_public:
            continue
        if trading_status != "pending":
            continue
        if tradable_date is None:
            # 上市日待定 — 只有"已发行未上市"才算新债, 纯缺字段的老债不算
            if not is_issued_pending_listing(code, terms, check_date):
                continue
            days_to_trade = None
        else:
            if tradable_date < check_date or tradable_date > end_date:
                continue
            days_to_trade = (tradable_date - check_date).days

        rows.append({
            "bond_code": code,
            "bond_name": name,
            "stock_code": _terms_value(terms, "underlying_code"),
            "underlying_name": _terms_value(terms, "underlying_name"),
            "issue_date": _terms_date(terms, "issue_date"),
            "listing_date": _terms_date(terms, "listing_date"),
            "tradable_date": tradable_date,
            "days_to_trade": days_to_trade,
            "K": _terms_value(terms, "conversion_price"),
            "market_price": _terms_value(terms, "close"),
            "credit_rating": _terms_value(terms, "credit_rating"),
            "outstanding_balance": _terms_value(terms, "outstanding_balance"),
            "maturity_date": _terms_date(terms, "maturity_date"),
            "is_tradable": _terms_value(terms, "is_tradable"),
            "trading_status": trading_status,
        })
    # 上市日待定的排在已定上市日之后
    rows.sort(key=lambda row: (row["tradable_date"] or date.max, row["bond_code"]))
    return rows


def merge_upcoming_pricing_results(
    upcoming_rows: Sequence[dict],
    pricing_results: Sequence[dict],
) -> list[dict]:
    """把关注池元数据与批量定价结果按代码合并."""
    priced_by_code = {row.get("bond_code"): row for row in pricing_results}
    merged: list[dict] = []
    for row in upcoming_rows:
        out = dict(row)
        priced = priced_by_code.get(row.get("bond_code"))
        if priced:
            for key in (
                "S0", "sigma", "theoretical_price", "market_price", "deviation",
                "credit_rating", "status", "data_source", "parity",
                "conversion_premium", "model_premium_to_parity",
                "opportunity_score", "confidence", "risk_tags",
            ):
                if key in priced:
                    out[key] = priced[key]
            out["bond_name"] = priced.get("bond_name") or out.get("bond_name")
            out["stock_code"] = priced.get("stock_code") or out.get("stock_code")
            out["K"] = priced.get("K", out.get("K"))
        else:
            out.setdefault("status", "待定价")
        merged.append(out)
    return merged


def build_batch_provider(
    source: str,
    *,
    terms_cache=None,
    csv_root: str | Path | None = None,
    max_age_days: int = 30,
) -> DataProvider:
    """按名称构造批量定价用 provider.

    转债基础信息固定从 cb_data 读取/由 Wind 刷新; source 只决定正股价格、
    历史波动率、转债历史和无风险利率等动态数据来源。
    """
    source_key = (source or "").strip().lower()
    if source_key == "wind":
        inner: DataProvider = WindDataProvider()
    elif source_key == "akshare":
        inner = AkshareDataProvider()
    elif source_key == "csv":
        if not csv_root:
            raise RuntimeError("请先选择 CSV 数据根目录")
        inner = CSVDataProvider(csv_root)
    else:
        raise RuntimeError(f"未知数据源: {source}")

    if terms_cache is None:
        return inner
    static_source = inner if isinstance(inner, WindDataProvider) else None
    return CachedBondDataProvider(
        inner,
        terms_cache,
        static_source=static_source,
        max_age_days=max_age_days,
    )


def _cached_terms(terms_cache, code: str):
    if terms_cache is None or not hasattr(terms_cache, "get"):
        return None
    try:
        return terms_cache.get(code)
    except Exception:
        return None


def _admission_projection_stores():
    try:
        return TermsPatchStore(), CBEventStore(project_events_path())
    except Exception:
        return None, None


def _terms_cache_as_of(terms_cache, code: str) -> date | None:
    """条款缓存里这只债的抓取日 —— 快照已含该日之前生效的全部条款变更。"""
    getter = getattr(terms_cache, "fetched_at", None)
    if getter is None:
        return None
    try:
        ts = getter(code)
    except Exception:
        return None
    return ts.date() if ts is not None else None


def _project_terms_for_admission(
    code: str,
    terms: Any,
    on_date: date,
    *,
    patch_store: TermsPatchStore | None = None,
    event_store: CBEventStore | None = None,
    terms_as_of: date | None = None,
):
    if terms is None or isinstance(terms, dict):
        return terms
    try:
        return project_terms(
            code,
            terms,
            on_date,
            patch_store=patch_store,
            event_store=event_store,
            terms_as_of=terms_as_of,
        ).terms
    except Exception:
        return terms


def _with_inferred_trading_metadata(code: str, terms: Any, on_date: date):
    if terms is None or isinstance(terms, dict):
        return terms
    try:
        return infer_cb_trading_metadata(code, terms, on_date)
    except Exception:
        return terms


def _terms_value(terms: Any, key: str):
    if terms is None:
        return None
    if isinstance(terms, dict):
        return terms.get(key)
    return getattr(terms, key, None)


def _terms_date(terms: Any, key: str) -> date | None:
    value = _terms_value(terms, key)
    try:
        return to_date(value)
    except Exception:
        return None


def summarize_batch_results(results: Sequence[dict]) -> dict:
    """返回批量结果的轻量汇总, 供 UI / CLI 展示."""
    ok_count = sum(1 for row in results if row.get("status") == "ok")
    return {
        "total": len(results),
        "success": ok_count,
        "failed": len(results) - ok_count,
    }


def median_deviation_of(results: Sequence[dict]) -> float | None:
    """一批定价结果的 deviation 中位数; 样本不足或无有效值时返回 None。

    "偏差异常" 的判据锚在它上面 —— 见 ``DEVIATION_ANOMALY_THRESHOLD``。
    """
    values = sorted(
        v for v in (finite_float(r.get("deviation")) for r in results
                    if r.get("status") == "ok")
        if v is not None
    )
    if len(values) < _DEVIATION_MEDIAN_MIN_SAMPLE:
        return None
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2.0


def annotate_batch_result(row: dict, *,
                          market_median_deviation: float | None = None) -> dict:
    """给单只批量结果补研究筛选字段.

    这些字段不改变模型定价, 只帮助排序和人工复核:
    - parity: 转股价值
    - conversion_premium: 市价相对转股价值溢价
    - opportunity_score: 低估程度经风险惩罚后的机会分
    - confidence / risk_tags: 结果可信度与复核提示
    """
    out = dict(row)
    if out.get("status") != "ok":
        out.setdefault("risk_tags", [])
        out.setdefault("confidence", "低")
        out.setdefault("opportunity_score", float("nan"))
        out.setdefault("model_signal_status", "不可用")
        # 定价失败的行也要有分桶, 否则分桶不是全覆盖的划分 (GUI 分桶列会空白),
        # 而视图侧的"需复核"是包含它们的 —— 两边口径必须一致。
        out.setdefault("review_bucket", "需复核")
        out.setdefault("review_notes", [str(out.get("status") or "定价失败")])
        return out

    s0 = finite_float(out.get("S0"))
    k = finite_float(out.get("K"))
    theo = finite_float(out.get("theoretical_price"))
    market = finite_float(out.get("market_price"))
    deviation = finite_float(out.get("deviation"))
    sigma = finite_float(out.get("sigma"))
    balance = finite_float(out.get("outstanding_balance"))
    t_years = finite_float(out.get("T"))
    rating = str(out.get("credit_rating") or "").upper().strip()

    risk_tags: list[str] = []
    score = 0.0
    confidence_points = 100.0

    parity = s0 / k * 100.0 if s0 is not None and k and k > 0 else None
    if parity is not None:
        out["parity"] = parity
    else:
        risk_tags.append("数据缺口")
        confidence_points -= 25

    conversion_premium = None
    if market is not None and parity and parity > 0:
        conversion_premium = market / parity - 1.0
        out["conversion_premium"] = conversion_premium
        if conversion_premium < -0.03:
            risk_tags.append("转股折价")
            score += min(30.0, abs(conversion_premium) * 140.0)
        elif conversion_premium < 0.03:
            risk_tags.append("贴近转股价值")
            score += 4.0

    if theo is not None and parity and parity > 0:
        model_premium = theo / parity - 1.0
        out["model_premium_to_parity"] = model_premium
        if model_premium > 0.45:
            risk_tags.append("模型溢价高")
            confidence_points -= 12

    if deviation is not None:
        out["undervaluation_rate"] = -deviation
        score += max(0.0, -deviation) * 100.0
        if deviation < -0.08:
            risk_tags.append("模型低估")
        if deviation > 0.08:
            score -= min(20.0, deviation * 60.0)
        # **按方向拆成两个标签, 而不是一个对称的"异常"**。贵侧与便宜侧的后验含义相反:
        #   贵侧  市价远高于模型对同期市场的一般水平 → 模型解释不了这个价, 属模型适用性;
        #   便宜侧 市价远低于模型 → 这正是本工具存在的理由, 是**待检验的假设**而不是噪声。
        # 曾用对称判据, 结果是唯一一只机会分 ≥8 的债 (美锦转债 dev −0.158) 被自己标成
        # "异常"踢出低估候选 —— 等于系统性删掉唯一的假设来源。
        anchor = market_median_deviation if market_median_deviation is not None else 0.0
        gap = deviation - anchor
        if gap >= DEVIATION_ANOMALY_THRESHOLD:
            risk_tags.append("模型高估离群")
            confidence_points -= 25
        elif gap <= -DEVIATION_ANOMALY_THRESHOLD:
            # 只提示核查, 不扣置信度、不进任何排除集。
            risk_tags.append("深度低估待核")
    else:
        risk_tags.append("无偏差")
        confidence_points -= 20

    if sigma is not None:
        if sigma > 0.80:
            risk_tags.append("高HV")
            penalty = min(28.0, 10.0 + (sigma - 0.80) * 35.0)
            score -= penalty
            confidence_points -= penalty
        elif sigma > 0.60:
            risk_tags.append("较高HV")
            score -= 4.0
            confidence_points -= 6.0
    else:
        risk_tags.append("无HV")
        confidence_points -= 20

    if balance is not None:
        if balance <= 0:
            # 余额清零 = 已转股完毕/已赎回, 是退市信号而不是"数据异常"
            risk_tags.append("余额清零")
            score -= 30.0
            confidence_points -= 35.0
        elif balance < BALANCE_DELISTING_LINE:
            # 低于 3,000 万法定线, 交易所将安排停止交易 —— 可执行的判断, 不是笼统的"小"
            risk_tags.append("触及摘牌线")
            score -= 22.0
            confidence_points -= 25.0
        elif balance < 0.5:
            risk_tags.append("临近摘牌线")
            score -= 16.0
            confidence_points -= 18.0
        elif balance < 1.0:
            risk_tags.append("小余额")
            score -= 12.0
            confidence_points -= 14.0
        elif balance >= 10.0:
            score += 2.0
    else:
        risk_tags.append("无余额")
        confidence_points -= 8.0

    # 已公告的最后交易日 = 确定性退出安排 (强赎或到期), 与余额推断出的摘牌风险互相独立。
    # 存续券的 delisting_date 多数等于到期日 (预定摘牌), 不是事件, 因此这里只认
    # last_trading_date。非硬标签: 只提高可见度, 不改变默认选债行为。
    last_trade = _terms_date(out, "last_trading_date")
    val_date = _terms_date(out, "valuation_date")
    if last_trade is not None and val_date is not None:
        days_left = (last_trade - val_date).days
        out["days_to_last_trading"] = days_left
        if 0 <= days_left <= DELISTING_WARNING_DAYS:
            risk_tags.append("临近摘牌")
            confidence_points -= 10.0

    if t_years is not None:
        if t_years < 0.5:
            risk_tags.append("短久期")
            score -= 12.0
            confidence_points -= 14.0
        elif t_years < 1.0:
            risk_tags.append("近到期")
            score -= 5.0
            confidence_points -= 7.0

    if rating:
        if rating.startswith("AA+"):
            score += 3.0
        elif rating == "AA" or rating.startswith("AAA"):
            score += 2.0
        elif rating.startswith("AA-"):
            score += 0.5
        elif rating.startswith(LOW_RATING_PREFIXES):
            risk_tags.append("低评级")
            score -= 8.0
            confidence_points -= 12.0
    else:
        risk_tags.append("无评级")
        confidence_points -= 8.0

    if _underlying_has_st_risk(out):
        risk_tags.append("正股风险")
        score -= 25.0
        confidence_points -= 30.0
    if _underlying_suspended(out):
        risk_tags.append("正股停牌")
        score -= 20.0
        confidence_points -= 25.0
    if _public_trading_status_reason(out) == "停牌/暂停交易":
        risk_tags.append("转债停牌")
        score -= 20.0
        confidence_points -= 25.0

    if _underlying_at_limit_down(out, out.get("stock_code")):
        risk_tags.append("正股跌停")
        score -= 15.0
        confidence_points -= 18.0

    down_uplift = finite_float(out.get("down_reset_uplift"))
    if down_uplift is None:
        no_down = finite_float(out.get("no_down_price"))
        if theo is not None and no_down is not None:
            down_uplift = theo - no_down
            out["down_reset_uplift"] = down_uplift
    if down_uplift is not None and theo and theo > 0:
        uplift_pct = down_uplift / theo
        if uplift_pct >= 0.08:
            risk_tags.append("下修贡献高")
            confidence_points -= 8.0
        elif uplift_pct <= -DOWN_RESET_DRAG_THRESHOLD:
            # 同网格求解后 uplift 理应 >= 0 (下修降 K 对持有人是额外期权)。仍然为负只有两种
            # 可能, 两种都必须可见而不是静默:
            #   ① 真实的减值 —— 下修降 K 后反弹更快撞上强赎上限, 理论上存在但尚未在本库
            #      构造出算例复现;
            #   ② 新的数值问题 —— 历史上正是这样: 混网格时代 55 只负 uplift 全部是伪信号,
            #      实测其值恰等于"粗网格价 − 细网格价"的相反数 (118064.SH +1.325 vs -1.325,
            #      118058.SH +1.188 vs -1.188)。根因是 S_max 在高 σ 下顶到 50·K 上限,
            #      粗网格 M=150 只剩 4 个格点落在 S0 以下 (细网格 15 个)。
            risk_tags.append("下修减值")
            confidence_points -= 8.0

    if market is None or market <= 0:
        risk_tags.append("无市价")
        confidence_points -= 25.0
        score = float("nan")
    if theo is None or theo <= 0:
        risk_tags.append("理论价异常")
        confidence_points -= 30.0
        score = float("nan")

    confidence_points = max(0.0, min(100.0, confidence_points))
    if confidence_points >= 78:
        confidence = "高"
    elif confidence_points >= 55:
        confidence = "中"
    else:
        confidence = "低"

    out["risk_tags"] = _dedupe_tags(risk_tags)
    out["confidence"] = confidence
    # opportunity_score 是"模型低估程度"的复核排序信号, 用于筛选值得人工复核的标的;
    # 经全市场池回测其横截面排序对未来收益预测力≈0 (Rank-IC≈-0.05), 不是收益预测/买入
    # 排名。可用 convertible_bond.signal_eval 复验。NaN 表示无市价/理论价异常 (上层已处理)。
    out["opportunity_score"] = score
    if set(out["risk_tags"]) & HARD_REVIEW_TAGS or confidence == "低":
        out["model_signal_status"] = "不适合作为买入信号"
    elif out["risk_tags"]:
        out["model_signal_status"] = "需复核"
    else:
        out["model_signal_status"] = "可作为模型信号复核"
    out["sensitivity_status"] = _sensitivity_status(out["risk_tags"], confidence)
    out["review_bucket"] = _review_bucket(out)
    out["review_notes"] = _review_notes(out)
    return out


def annotate_batch_results(results: Sequence[dict], *,
                           market_median_deviation: float | None = None) -> list[dict]:
    """补齐批量研究字段, 不改变输入列表.

    *market_median_deviation* 缺省时从这批结果自算 (样本 <30 则退回绝对阈值)。
    给关注池/新债这类小批量标注时, 应显式传主池的中位数, 否则自算出来的中位没有代表性。
    """
    if market_median_deviation is None:
        market_median_deviation = median_deviation_of(results)
    return [annotate_batch_result(row, market_median_deviation=market_median_deviation)
            for row in results]


def sort_batch_results_for_review(results: Sequence[dict]) -> list[dict]:
    """按实际复核价值排序: 成功行优先, 机会分降序, 偏差升序."""
    annotated = annotate_batch_results(results)

    def key(row: dict):
        score = finite_float(row.get("opportunity_score"))
        deviation = finite_float(row.get("deviation"))
        ok_rank = 0 if row.get("status") == "ok" else 1
        score_rank = -score if score is not None else float("inf")
        deviation_rank = deviation if deviation is not None else float("inf")
        return (ok_rank, score_rank, deviation_rank, row.get("bond_code") or "")

    return sorted(annotated, key=key)


DEFAULT_UNDERVALUED_SCORE_THRESHOLD = 8.0


def view_exclusion_reason(
    row: dict,
    view: str | None,
    *,
    undervalued_score_threshold: float | None = None,
) -> str | None:
    """这一行**不属于**该视图的原因; 属于则返回 None。

    视图归属的**单一事实源**: ``filter_batch_results_by_view`` 与策略页的落选解释
    (strategy_backtest._candidate_filter_reason) 都走这里。二者曾各自实现一份, 结果在
    标签体系重构后悄悄分叉 —— 一个已改读维度拦截集, 另一个还硬编码 HARD_REVIEW_TAGS。
    """
    view_name = view if view in BATCH_REVIEW_VIEWS else "综合机会"
    tags = set(row.get("risk_tags") or [])
    ok = row.get("status") == "ok"
    if view_name == "综合机会":
        return None
    if view_name == "低估候选":
        threshold = (DEFAULT_UNDERVALUED_SCORE_THRESHOLD
                     if undervalued_score_threshold is None
                     else float(undervalued_score_threshold))
        if not ok:
            return "定价未成功"
        score = finite_float(row.get("opportunity_score"))
        if score is None:
            return "缺少机会分"
        if score < threshold:
            return f"机会分 {score:.1f} < {threshold:.1f}"
        if row.get("confidence") not in {"高", "中"}:
            return "置信度不足"
        if "转股折价" in tags:
            return "转股折价单独归类"
        blocking = tags & _BLOCKING_TAGS
        if blocking:
            return "拦截标签 " + "/".join(sorted(blocking))
        return None
    if view_name == "转股折价":
        if not ok:
            return "定价未成功"
        return None if "转股折价" in tags else "未出现转股折价标签"
    if view_name == "需复核":
        if not ok or (tags & _BLOCKING_TAGS) or row.get("confidence") == "低":
            return None
        return "不属于复核池"
    return None


def filter_batch_results_by_view(
    results: Sequence[dict],
    view: str | None,
    *,
    undervalued_score_threshold: float | None = None,
) -> list[dict]:
    """按批量页视图过滤结果, 并保持研究排序.

    *undervalued_score_threshold* 仅作用于"低估候选"视图; None 时使用
    ``DEFAULT_UNDERVALUED_SCORE_THRESHOLD``。
    """
    rows = sort_batch_results_for_review(results)
    return [
        row for row in rows
        if view_exclusion_reason(
            row, view, undervalued_score_threshold=undervalued_score_threshold) is None
    ]


def save_batch_results_cache(
    results: Sequence[dict],
    *,
    path: str | Path | None = None,
    source: str | None = None,
    params: dict | None = None,
    upcoming_results: Sequence[dict] | None = None,
) -> Path:
    """保存批量定价结果快照, 供 GUI 下次直接加载."""
    upcoming = list(upcoming_results or [])
    cache_path = Path(path) if path else project_batch_cache_path()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        BATCH_RESULT_META_KEY: {
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "source": source,
            "params": _json_safe(params or {}),
            "n_results": len(results),
            "n_upcoming_results": len(upcoming),
            "summary": summarize_batch_results(results),
        },
        "results": [_json_safe(row) for row in results],
        "upcoming_results": [_json_safe(row) for row in upcoming],
    }
    tmp = cache_path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
    tmp.replace(cache_path)
    return cache_path


def load_batch_results_cache(path: str | Path | None = None) -> dict:
    """读取批量定价结果快照, 返回 {meta, results}."""
    cache_path = Path(path) if path else project_batch_cache_path()
    if not cache_path.exists():
        raise FileNotFoundError(f"批量定价缓存不存在: {cache_path}")
    with open(cache_path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    results = [_restore_result_row(row) for row in payload.get("results", [])]
    upcoming_results = [_restore_result_row(row) for row in payload.get("upcoming_results", [])]
    return {
        "meta": payload.get(BATCH_RESULT_META_KEY, {}),
        "results": results,
        "upcoming_results": upcoming_results,
        "path": cache_path,
    }


def write_batch_results_csv(path: str | Path, results: Sequence[dict]) -> None:
    """按统一列定义导出批量定价结果."""
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(BATCH_RESULT_COLUMNS)
        for row in results:
            writer.writerow([_csv_value(row, column) for column in BATCH_RESULT_COLUMNS])


def _csv_value(row: dict, column: str):
    if row.get("status") != "ok" and column in {
        "S0", "K", "sigma", "theoretical_price", "parity",
        "conversion_premium", "model_premium_to_parity", "opportunity_score",
    }:
        return ""
    value = row.get(column, "")
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    if column in {
        "deviation", "conversion_premium", "model_premium_to_parity",
        "effective_p_down_1y_prob", "implied_p_down_1y_prob",
        "down_reset_probability_gap", "down_reset_edge_pct",
    }:
        return f"{float(value):.6f}" if value != "" else ""
    if column == "undervaluation_rate":
        return f"{float(value):.6f}" if value != "" else ""
    if column in {
        "parity", "opportunity_score", "implied_p_down", "p_down_gap",
        "down_reset_sensitivity", "down_reset_edge_value",
        "down_reset_robust_edge_value", "down_reset_edge_scenario_range",
        "down_reset_edge_worst_sigma", "down_reset_edge_worst_spread",
    }:
        return f"{float(value):.4f}" if value != "" else ""
    if column == "risk_tags" and isinstance(value, list):
        return "|".join(str(tag) for tag in value)
    if column == "review_notes" and isinstance(value, list):
        return "|".join(str(note) for note in value)
    return value


def _json_safe(value: Any):
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def _restore_result_row(row: dict) -> dict:
    restored = dict(row)
    for key in (
        "deviation", "theoretical_price", "S0", "K", "sigma", "parity",
        "conversion_premium", "model_premium_to_parity", "opportunity_score",
        "undervaluation_rate", "no_down_price", "down_reset_uplift",
        "implied_p_down", "effective_p_down_1y_prob", "implied_p_down_1y_prob",
        "p_down_gap", "down_reset_probability_gap", "down_reset_sensitivity",
        "down_reset_edge_value", "down_reset_edge_pct", "down_reset_robust_edge_value",
        "down_reset_edge_scenario_range", "down_reset_edge_worst_sigma",
        "down_reset_edge_worst_spread",
    ):
        if key in restored and restored[key] is None:
            restored[key] = float("nan")
    return restored



def _dedupe_tags(tags: Sequence[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        if tag and tag not in seen:
            seen.add(tag)
            out.append(tag)
    return out


def _sensitivity_status(tags: Sequence[str], confidence: str) -> str:
    tag_set = set(tags or [])
    if {"高HV", "模型溢价高"} & tag_set:
        return "波动率敏感"
    if {"余额清零", "触及摘牌线", "临近摘牌线", "小余额", "临近摘牌", "短久期",
            "低评级", "正股风险", "正股停牌", "转债停牌",
            "极小余额", "余额异常"} & tag_set:
        return "条款/流动性敏感"
    if confidence == "高":
        return "较稳健"
    if confidence == "中":
        return "一般"
    return "需复核"


def _review_bucket(row: dict) -> str:
    tags = set(row.get("risk_tags") or [])
    if row.get("status") != "ok":
        return "需复核"
    if tags & _BLOCKING_TAGS or row.get("confidence") == "低":
        return "需复核"
    if tags & tags_in(DIM_MODEL):
        # 模型在这只债上不可靠, 但不是"去做点什么"就能解决的 —— 它是永久属性, 该单列
        # 一档而不是塞进需复核。实测这一档 142 只, 塞进需复核会让后者占到 61%。
        return "模型存疑"
    if "转股折价" in tags:
        return "转股折价"
    score = finite_float(row.get("opportunity_score"))
    if score is not None and score >= 8.0 and row.get("confidence") in {"高", "中"}:
        return "低估候选"
    return "综合机会"


def _review_notes(row: dict) -> list[str]:
    tags = set(row.get("risk_tags") or [])
    notes: list[str] = []
    if "转股折价" in tags:
        notes.append("核实是否已进入转股期、是否停牌/强赎、K 和 S0 是否同日最新")
    if "高HV" in tags or "较高HV" in tags:
        notes.append("用 60/120 日 HV 或手工 sigma 重算, 防止短期波动抬高理论价")
    if "模型溢价高" in tags:
        notes.append("理论价主要来自期权/下修价值, 需要降低基础下修强度或 sigma 做压力测试")
    if {"余额清零", "触及摘牌线", "临近摘牌线", "小余额", "极小余额", "余额异常"} & tags:
        notes.append("核实剩余规模、流动性、强赎/退市安排")
    if "下修减值" in tags:
        notes.append("下修权算出负价值 —— 同网格下不应出现; 优先怀疑数值问题 "
                     "(高 σ 时 S_max 顶到 50×K, 格点过疏), 其次才是强赎加速导致的真实减值")
    if "模型高估离群" in tags or "偏差异常" in tags:
        notes.append("市价显著高于模型对同期市场的一般水平; 先核实行情与正股价是否同日、"
                     "K 是否最新、强赎/停牌是否已应用, 再考虑是不是真的贵")
    if "深度低估待核" in tags:
        notes.append("市价显著低于模型 —— 这是待检验的假设不是结论; 核实行情同日性、"
                     "转股价是否已刷新、是否已进入强赎/回售或停牌")
    if "临近摘牌" in tags:
        days = finite_float(row.get("days_to_last_trading"))
        when = f"仅剩 {int(days)} 天" if days is not None else "已公告"
        notes.append(f"已公告最后交易日({when}), 到期后无法卖出; 确认退出安排与强赎条款")
    if "短久期" in tags or "近到期" in tags:
        notes.append("核实到期兑付、回售和强赎时间表")
    if "低评级" in tags:
        notes.append("核实信用风险和信用利差假设")
    if "正股跌停" in tags:
        notes.append("正股当日跌停, S0 不稳定; 需等待正股恢复正常交易后再判断")
    if "正股风险" in tags:
        notes.append("正股存在 ST/退市风险, 普通模型理论价不适合作为买入信号")
    if "正股停牌" in tags or "转债停牌" in tags:
        notes.append("交易暂停状态下行情锚点失真, 等复牌后重新定价")
    if "下修贡献高" in tags:
        notes.append("理论价对下修假设敏感, 对比无下修价和下修贡献后再判断")
    if "偏差异常" in tags:
        notes.append("|偏差|>20%, 多为正股/转债不同日或停牌/强赎未应用; 重新拉取行情和事件后再判断")
    if "模型低估" in tags and not notes:
        notes.append("优先核实条款、行情日期和模型参数后再进入单债分析")
    return _dedupe_tags(notes)
