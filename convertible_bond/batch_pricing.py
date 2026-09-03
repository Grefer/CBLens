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

from .cache import TERMS_SYNC_SOURCE, CachedBondDataProvider, terms_fetched_at
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
    safe_date,
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
    "double_low",
    "double_low_rank",
    "model_premium_to_parity",
    "relative_deviation",
    "market_median_deviation",
    "cheapness_rank",
    "cheapness_percentile",
    "quality_score",
    "confidence",
    "risk_tags",
    "event_flags",
    "down_reset_trigger_gap",
    "outstanding_balance",
    "days_to_last_trading",
    "model_signal_status",
    "no_down_price",
    "down_reset_uplift",
    "effective_p_down_1y_prob",
    "sensitivity_status",
    "review_bucket",
    "review_notes",
]

_CODE_SPLIT_RE = re.compile(r"[\s,;，；]+")
_HEADER_TOKENS = {"code", "bond_code", "证券代码", "转债代码", "代码"}
BATCH_RESULT_META_KEY = "_meta"
LOW_RATING_PREFIXES = ("A", "BBB", "BB", "B", "CCC", "CC", "C")

BATCH_REVIEW_VIEWS = ("综合机会", "低估候选", "双低", "转股折价", "需复核")

#: 视图的**展示名**; 底层字面量逐字冻结。
#:
#: ``"综合机会"`` 是 ``ScoreStrategyConfig.selection_view`` 的默认值, 会随策略配置
#: 落进快照 (``strategy_run`` 的 ``config``) 并被 ``_canonical_view_name`` 回读 ——
#: 改这个串是兼容性破坏, 不是改个标签。解法与「模型高估离群」→「市价远高于模型价」
#: 同一条: 只改展示名, 底层一个字节不动 (见 ``RISK_TAG_DISPLAY_LABEL``)。
#:
#: 为什么这一条非改不可: 这个视图的 ``view_exclusion_reason`` 直接 ``return None``,
#: 它**就是不过滤的全池**, 既不"综合"也不排"机会" —— 而它名字里的那个「机会」指的是
#: ``opportunity_score``, 那个字段已于 2026-08-29 整体删除, 名字成了唯一的残留引用。
#: 实测它也确实不是一个独立的机会排序: 按相对偏差升序的**前 43 行与「低估候选」
#: 重合 43/43**, 独立信息全在第 44 行往后。``STRATEGY_VIEW_DESCRIPTIONS`` 早就把它
#: 描述成"全部可交易主池, 不额外筛" —— 只有名字一直没跟上。
BATCH_VIEW_DISPLAY_LABEL: dict[str, str] = {
    "综合机会": "全池",
}


def batch_view_label(view: str) -> str:
    """视图的展示名; 没登记的原样返回。**所有**面向用户的出口都要走这里。"""
    return BATCH_VIEW_DISPLAY_LABEL.get(view, view)


def batch_view_from_label(label: str) -> str | None:
    """展示名 → 冻结的视图名; 认不出来返回 None。

    与 ``batch_view_label`` 共用**同一张表**, 不许在消费者里各自反转一份 —— 那正是
    展示名与底层名分叉的老路子。canonical 名本身也认 (菜单未刷新时显示的就是它)。
    """
    if label in BATCH_REVIEW_VIEWS:
        return label
    for canonical, shown in BATCH_VIEW_DISPLAY_LABEL.items():
        if shown == label:
            return canonical
    return None

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

#: 已退役的标签: 代码里**没有 append 点**, 只可能从旧缓存/旧快照里读回来。
#:
#: 它们仍然登记在 ``RISK_TAG_DIMENSION`` 里 (而不是删掉), 因为消费者是按维度派生的 ——
#: ``TRADABILITY_RISK_TAGS`` / ``DATA_QUALITY_RISK_TAGS`` / ``BLOCKING_RISK_TAGS`` 都走
#: ``tags_in(...)``, 一旦某个字符串不在册, 旧缓存里带着它的行就查不到维度, 行色与视图归属
#: 会静默改变。留在册 = 旧数据读回来行为不变, 这是既有做法 (偏差异常 / 极小余额 / 余额异常
#: 一直就是这么处理的), 只是此前没有一个明确的清单。
#:
#: 有守护测试比对: 这里的每一个都必须**没有** ``risk_tags.append`` 现场, 且必须仍在
#: ``RISK_TAG_DIMENSION`` 里。
RETIRED_RISK_TAGS: frozenset[str] = frozenset({
    "偏差异常",      # 拆成「模型高估离群」/「深度低估待核」之前的对称旧名
    "极小余额",      # 余额那一族早期的扁平名
    "余额异常",      # 同上
    "无余额",        # 2026-08-31 退役: 字段从不缺失 (主池 0/311, 全库 2/1059 且全在池外)
    "无评级",        # 2026-08-31 退役: 同上 (全库 1/1059)
    "临近摘牌线",    # 2026-08-31 退役: 0.3~0.5 亿这条带主池恒空, 落进来的债改打「小余额」
    "模型低估",      # 2026-08-31 退役: 绝对阈值 dev<−8%, 实测 1/284; 便宜度只留横截面那个
})


RISK_TAG_DIMENSION: dict[str, str] = {
    "数据缺口": DIM_DATA, "无偏差": DIM_DATA, "无HV": DIM_DATA,
    "无市价": DIM_DATA, "理论价异常": DIM_DATA,
    "无余额": DIM_DATA, "无评级": DIM_DATA,     # ← 已退役, 见 RETIRED_RISK_TAGS

    "高HV": DIM_MODEL, "较高HV": DIM_MODEL, "模型溢价高": DIM_MODEL,
    "模型高估离群": DIM_MODEL, "下修贡献高": DIM_MODEL, "下修减值": DIM_MODEL,
    "偏差异常": DIM_MODEL,                      # ← 已退役

    "低评级": DIM_ISSUER, "小余额": DIM_ISSUER,
    "触及摘牌线": DIM_ISSUER, "短久期": DIM_ISSUER, "近到期": DIM_ISSUER,
    "临近摘牌线": DIM_ISSUER,                   # ← 已退役
    "极小余额": DIM_ISSUER,                     # ← 已退役
    # 「正股风险」归**标的风险**而不是可交易性: ST 正股的转债照常挂牌撮合, 它描述的是
    # 这个发行人有多危险 (与「低评级」同族), 不是"今天买不到"。归可交易性会让它进
    # BLOCKING_RISK_TAGS —— 那会把它从「低估候选」里筛掉、并把整行染成红色加粗的
    # 「买卖受限」, 而那一档收的是 临近摘牌 / 正股停牌 / 转债停牌 这种真的下不了单的。
    "正股风险": DIM_ISSUER,

    "余额清零": DIM_TRADABILITY, "正股停牌": DIM_TRADABILITY,
    "转债停牌": DIM_TRADABILITY, "正股跌停": DIM_TRADABILITY, "临近摘牌": DIM_TRADABILITY,
    "余额异常": DIM_TRADABILITY,                # ← 已退役

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


#: 两个拦截维度**分开公开**: 行色给它们不同的视觉语言, 判据不许在 GUI 里另抄一份。
#:
#: 分开的理由不是频次 (实测主池 可交易性 3 只 / 数据质量 1 只), 是**降级场景**:
#: 数据源抖一下, 「无市价」可以一次命中几百行 —— 它们要是和「临近摘牌」共用同一个
#: 警报色, 一屏红色会被读成"市场出事了", 而真相是"取数挂了, 去跑 cb-data-doctor"。
#: 方向也不对称: 数据质量行在选债页上无事可做 (是噪声, 该静音), 可交易性行则是最
#: 需要动作的一档 (临近摘牌 = 30 天内必须卖掉)。
TRADABILITY_RISK_TAGS = tags_in(DIM_TRADABILITY)
DATA_QUALITY_RISK_TAGS = tags_in(DIM_DATA)

#: 两者的并集 —— ``view_exclusion_reason`` / ``_review_bucket`` 用的是这个口径
#: (进不进得了主池不看是哪一维), 行色才按维度分。
BLOCKING_RISK_TAGS = TRADABILITY_RISK_TAGS | DATA_QUALITY_RISK_TAGS


# ── 模型偏差离群的两个方向 ──────────────────────────────────────
# 这两族**方向相反**, 分属两个不同维度 (机会信号 / 模型适用性), 实测相对偏差中位
# −21.95pp vs +27.76pp。GUI 曾用一个字面量集合把它们合成同一个橙色行, 而
# ``batch_watchlist`` 的摘要条又抄了第二份一模一样的字面量 —— 与「暂停转股与恢复
# 转股是相反的意思却同色」是同一次分叉的两半。展示层要报计数就分开报。
MODEL_OVERVALUED_TAGS = frozenset({"模型高估离群"})
DEEP_UNDERVALUED_TAGS = frozenset({"深度低估待核"})

#: 标签的**展示名** —— 只改表上怎么写, 底层字符串一个字节不动。
#:
#: 「模型低估」判据是 ``deviation < −0.08`` 即市价**低**于理论价 → 模型给的价**高**;
#: 「模型高估离群」是市价**远高**于模型价 → 模型给的价**低**。两个标签用的是省略式
#: "(按)模型(判为)低估/高估", 彼此一致; 但中文最自然的动宾读法 ("模型把它低估了")
#: 正好把方向读反。展示名改成以**市价**为主语, 消掉这个歧义。
#:
#: **为什么是展示名而不是改标签**: 「模型高估离群」在 ``LEGACY_STRATEGY_EXCLUDE_TAGS``
#: (逐字冻结的默认选债排除集) 里, 改字符串就是默认选债行为变更; 旧批量缓存与旧策略
#: 快照里存的也是原名。
#:
#: **表放在这里而不是 GUI**: 事件短标签曾在 GUI 自带一份私有表, 漏掉 4 个类型、把
#: 暂停转股与恢复转股渲染成同一个词 —— 展示词表只许有一份。
RISK_TAG_DISPLAY_LABEL: dict[str, str] = {
    # ── 数据质量: 这一行的数坏了 —— 统一句式「X缺失 / X无效」 ──────────────
    "数据缺口": "转股价值缺失",      # 判据是 parity 算不出 (缺 S0 或 K); 「数据」缺什么说不出来
    "无市价": "市价缺失",
    "无偏差": "偏差缺失",
    "无HV": "正股σ缺失",             # HV 是内部缩写; 表上那列叫「正股σ(%)」
    "理论价异常": "理论价无效",       # 判据是 None 或 ≤0, 「异常」比它泛
    "无余额": "余额缺失",             # ← 已退役
    "无评级": "评级缺失",             # ← 已退役

    # ── 模型适用性: 模型在这只债上不那么可靠 ────────────────────────────
    # 「模型高估离群」/「模型低估」用的是省略式 "(按)模型(判为)高估/低估", 两者彼此一致;
    # 但中文最自然的动宾读法 ("模型把它低估了") 正好把方向读反 —— 展示名改成以**市价**
    # 为主语消掉这个歧义。守护测试因此禁止展示名以「模型」开头、或出现无主语的「高估/低估」。
    "模型高估离群": "市价远高于模型价",
    "模型低估": "市价低于模型价",      # ← 已退役, 展示名保留供旧缓存渲染
    "模型溢价高": "理论价溢价高",      # 主语是**理论价** (相对转股价值的溢价), 不是"模型"
    "高HV": "正股波动极高",
    "较高HV": "正股波动偏高",         # 与上一档拉开: 极高 / 偏高
    "下修贡献高": "下修贡献高",
    "下修减值": "下修贡献为负",       # 「减值」读不出减的是什么; 它其实是一条断言失败的告警
    "偏差异常": "偏差离群",           # ← 已退役 (拆成高估/低估两侧之前的对称旧名)

    # ── 标的风险: 这个发行人有多危险 —— 名字带上**阈值**, "低/小/短"说不出低到哪 ──
    "低评级": "评级低于AA-",
    "小余额": "余额不足1亿",
    "触及摘牌线": "余额触及摘牌线",
    "短久期": "剩余不足半年",
    "近到期": "剩余不足1年",
    "正股风险": "正股ST",             # 「正股风险」太泛 —— 停牌/跌停也是"正股的风险",
                                     # 而这个标签只判 ST/退市风险警示 (_underlying_has_st_risk)
    "临近摘牌线": "余额不足5千万",     # ← 已退役
    "极小余额": "余额极小",           # ← 已退役

    # ── 可交易性: 买不到 / 快买不到了 ──────────────────────────────────
    "余额清零": "余额已清零",
    "正股停牌": "正股停牌",
    "转债停牌": "转债停牌",
    "正股跌停": "正股跌停",
    "临近摘牌": "30天内摘牌",         # 「临近」多近? 这是可交易性维里唯一可操作的一档
    "余额异常": "余额异常",           # ← 已退役

    # ── 机会信号: 为什么值得看这只债 ───────────────────────────────────
    # 尾巴上的「·待核」已去掉 (2026-08-30): 页面上恰好有一个叫「需复核」的视图, 而这两个
    # 「核」不是一回事 —— 那个视图是"数据/可交易性坏了, 去**修**", 这个标签是"数是对的、
    # 便宜是真的, 去**研究**"。**2026-09-03 复测: 原来那句「23/23 都在低估候选里」已不成立**
    # —— 全池 311 行里带标签 22 只、「需复核」11 只, 两者重叠 7 只。但重叠不是反例而是
    # 机制本身: 那 7 只恰好就是掉出「低估候选」的 7 只 (两个集合逐元素相同), 它们又真便宜
    # 又不能信/不能买 —— 合并会恰好在最需要区分的那 7 只上把区分抹掉。详见 AGENTS。
    # 展示名只陈述事实, 该做什么由 ``review_notes`` 说。
    "深度低估待核": "市价远低于市场中位",
    "转股折价": "转股折价",
    "贴近转股价值": "贴近转股价值",
}


def risk_tag_label(tag: str) -> str:
    """标签的展示名; 没登记的原样返回."""
    return RISK_TAG_DISPLAY_LABEL.get(tag, tag)

#: 拆分前的**对称**旧名, 不带方向, 所以归不进上面任何一族 (实测当前全库 0 条,
#: 只可能出现在旧缓存里)。要报就单独报, 不要猜一个方向塞进去。
LEGACY_DEVIATION_OUTLIER_TAGS = frozenset({"偏差异常"})


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
#
# 「正股风险」**刻意留在集合里**: 准入层已不再硬剔除 ST 正股 (改由标签承载), 但策略层
# 是自动选债, 那里排除它才让这次改动对**策略结果零影响** —— ST 的债此前根本进不了池,
# 现在进池了但被这个标签挡在候选之外, 两条路的结果逐只相同。要让策略也买 ST, 那是另一个
# 决定 (走治理三条), 不该作为"准入层降级成标签"的副作用发生。
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
# 默认不再按余额硬剔除。全库回填摘牌元数据后实测: 关掉该门槛主池 270 → 270,
# 独立贡献为 0 —— 它此前 99% 的作用是替缺失的 delisting_date 兜底 (被它剔除的 225 只
# 里 223 只余额恰为 0 的已退市券), 而那个职责现在由 delisting_date / last_trading_date
# 的日期判据接管, 剔除理由也从"余额过小"变成诚实的"已退市"。余额本身改由风险标签表达。
# 字段与语义保留, 想恢复硬过滤填个数值即可。
DEFAULT_MIN_OUTSTANDING_BALANCE: float | None = None
# 评级**不再**是准入硬过滤 (2026-08-31), 与余额那次降级同形。
#
# 准入层的职责是"买不买得到", 而 A 级债照常挂牌撮合 —— 硬剔除把"信用风险大"表达成了
# "这只债不存在"。实测它是准入层最后一条策略口径的剔除: 全库 12 条剔除原因里其余全是
# "已退市/已到期/停牌/已过最后交易日"这类真买不到的, 或"定向债/非沪深段"这类根本不是标的。
#
# 降级是安全的, 因为筛选口径已经下沉到策略层: ``ScoreStrategyConfig.min_credit_rating``
# 默认 "AA-"(比这里的 A+ 还严), 低评级债进得了全池但进不了策略候选。展示层由「低评级」
# 标签承载 (< AA-, 实测能接住原本被剔的 27 只中的 27 只)。
#
# 仍保留为**可选参数**: ``cb-screen-pool --min-rating AA-`` 照常生效。
DEFAULT_MIN_CREDIT_RATING: str | None = None
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
    并默认剔除正股停牌与低评级。**正股 ST 与余额默认不再硬剔除**: 前者见
    ``batch_pricing_exclusion_reason`` 里那段注释 (2026-08-31 改由「正股风险」标签
    承载), 后者见
    ``DEFAULT_MIN_OUTSTANDING_BALANCE``), 改由「余额清零 / 触及摘牌线 /
    临近摘牌线 / 小余额」风险标签表达; 高 HV 同理只有定价后才能识别。
    需要恢复余额硬过滤时给 ``min_outstanding_balance`` 填个数值即可。
    """

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
    min_outstanding_balance: float | None = DEFAULT_MIN_OUTSTANDING_BALANCE,
    min_credit_rating: str | None = DEFAULT_MIN_CREDIT_RATING,
    min_turnover_amount: float | None = None,
    admission_config: AdmissionFilterConfig | None = None,
) -> str | None:
    """返回批量主池过滤原因; None 表示可以进入主批量定价.

    这里只做进入主批量候选前的硬条件判断: 代码段/交易所、定向标识、
    是否已进入可交易窗口、转债自身停牌、最后交易/摘牌/到期日, 以及
    默认不适合直接作为买入信号的正股停牌与低评级标的
    (正股 ST 与余额默认不硬剔除, 各由风险标签承载)。
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
    # 已发行未上市**不再剔除** (2026-08-31): 它们后续会挂牌, 正是最值得提前算理论价、
    # 提前盯的一批。此前归"扫新债"关注池而不进主池, 于是主表上看不见它们。
    #
    # 三条连锁都已就位, 所以放进来是安全的:
    #   · 行色: `_resolve_row_tag` 的 `new` 档优先级最高, 语义就是"还没进入市场,
    #     价格类判据一律不适用" —— 它们天然带的「无市价」「无偏差」不会被误染成 nodata;
    #   · 策略层: `_candidate_filter_reason` 的有效性守卫要求有效市价, 新债进不了候选;
    #   · 基准: 没有价格序列 → `_execution_price_point` 返回 None → 自然不进等权基准。
    #
    # 唯一需要额外挡一道的是**估值基线的覆盖率闸**: 新债没有市价是天然状态而不是取数
    # 失败, 放进分母会稀释覆盖率 (实测在途新债超过 35 只就把 90% 的闸压住)。
    # 见 `market_valuation._usable_deviations` 与 `is_unlisted_new_bond`。
    #
    # 「🆕 扫新债」不受影响: `list_upcoming_tradable_from_cache` 读的是
    # `trading_status == "pending"` 与 `is_issued_pending_listing`, 不是这个原因串。
    if _never_entered_market(terms):
        return "无发行与上市日期"
    name = _terms_value(terms, "sec_name") or _terms_value(terms, "bond_name")
    standard_public = is_standard_public_cb_code(raw_code) and not looks_private_cb_name(name)
    # **放行判据必须与下游共用 `is_unlisted_new_bond`, 不能用 `is_issued_pending_listing`**
    # (2026-08-31 修): 后者的第一行是 `if listing_date is not None: return False` —— 上市日
    # 只要非空就为假, **哪怕那个日期在未来**。它表达的是"数据源还没给出上市日"这个更窄的
    # 意思, 而这里要问的是"这只债进入市场了没有"。用它做闸的净效果是: 上市日未知的新债
    # 放进来了, 而**已经公告了挂牌日**的那只 —— 最近、最该提前盯的那只 —— 照旧被剔。
    # 实测 2026-08-31 库里三只在途新债, 主表上只出得来两只 (震裕转02 定于 09-02 挂牌,
    # 剔除原因「不可交易」)。改动注释里列的三条下游安全垫 (行色 `_resolve_row_tag`、
    # 覆盖率分母 `market_valuation._usable_deviations`) 走的本来就是 `is_unlisted_new_bond`,
    # 只有这道闸分叉了。
    pending_listing = standard_public and is_unlisted_new_bond(terms, check_date)
    if is_tradable is False and not pending_listing:
        # 已发行未上市的债 ``is_tradable`` 天然是 False (``infer_cb_trading_metadata``
        # 的输出), 所以放行「已发行未上市」之后必须在这里也给它让路 —— 否则它只是
        # 换个原因串 (「不可交易」) 继续被剔, 改动等于没做。
        # 这一档剩下的是**真的**不可交易: 定向债、非公开标的等。
        return "不可交易"
    # 正股 ST/退市风险**不在这里剔除** —— 它是"风险较大"而不是"不能交易": ST 正股的转债
    # 照常挂牌撮合, 只是波动更大、退市尾部风险更高。硬剔除把这层信息表达成"这只债不存在",
    # 而准入层的契约是「字段明确才剔除」+「只剔真的买不到的」。
    #
    # 改由「正股风险」标签承载 (标的风险维): 表上看得见、可排序、可导出, 且
    # ``HARD_REVIEW_TAGS`` 让它的 ``model_signal_status`` 落到"不适合作为买入信号",
    # 而策略层照旧排除它 —— 但那道闸**不是标签**: 选债口径 2026-08-31 已从标签集换成
    # ``ScoreStrategyConfig.exclude_underlying_st`` (默认 True), ``exclude_risk_tags``
    # 的默认值现在是空元组。净效果不变 ("人看得到, 自动选债仍然不碰"), 但要改这条行为
    # 得动那个开关, 不是动标签集。``_underlying_limit_down_threshold`` 的注释早就写着"ST 风险进入复核标签,
    # 不作为主池硬剔除", 这里此前与那句话是分叉的。
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

    if standard_public:
        # **未来的可交易日不再剔除** —— 这一档就是上面刚放行的那批新债, 拦在这里等于
        # 只换个原因串继续剔 (实测: 把上面那道闸修好之后, 震裕转02 的剔除原因从
        # 「不可交易」变成「2 日后可交易」, 主池仍是 311 —— 两道闸必须同时改)。
        # 这里不再留一个形似守卫的分支: `is_unlisted_new_bond` 只要 `tradable_date` 或
        # `listing_date` 落在未来就为真, 所以对标准公募债 `tradable_date > check_date`
        # ⟹ `pending_listing`, 那个分支恒不成立。留着不可达的剔除分支正是本仓库反复
        # 踩过的形状 (「下修优势」删除后留下的死特判)。
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


# 日内临停 ≠ 停牌。新债上市首日触发涨跌幅熔断时 Wind 的 ``trade_status`` 就返回
# "盘中停牌", 但那是当日内几分钟到半小时的机制性熔断, 收盘照样有巨额成交 —— 派克转债
# 上市首日标着"盘中停牌"、当天成交 2.57 亿, 却被这条判据整只踢出主池。这类词必须先于
# 通用的"停牌"关键词识别, 否则子串匹配会先命中。
_INTRADAY_HALT_KEYWORDS = ("盘中停牌", "临时停牌", "盘中临停")


def is_unlisted_new_bond(row: Any, on_date: date | None = None) -> bool:
    """这一行是不是"还没挂牌的新债" —— **日期是硬证据, 压过派生字段**。

    单一事实源: 库层 (估值基线的覆盖率分母) 与 GUI 层 (行色的 ``new`` 档、关注池的
    ``market_price_coverage``) 共用它。两处各写一份正是这个仓库反复踩的形状。

    判据顺序不能反 (见 ``infer_cb_trading_metadata`` 里那段自我确认陷阱):
    ``is_tradable`` / ``trading_status`` 是**派生**字段 —— 公募转债的数据源根本不提供,
    关注池里更是加入那一刻冻结的快照, 一只债在"已发行未上市"时被扫进来、此后真的挂牌了,
    ``watchlist.json`` 里那个 ``pending`` 也不会自己翻回来。而**已经过去的上市日**是
    "确实挂牌了"的正面证据。所以: 未来日期 → 新债; 日期已过 → 不是新债;
    一个日期都没有才回落到派生字段 ("已发行未上市"正是这一档)。
    """
    check_date = on_date or market_today()
    listed = False
    for key in ("tradable_date", "listing_date"):
        d = _terms_date(row, key)
        if d is None:
            continue
        if d > check_date:
            return True
        listed = True
    if listed:
        return False
    is_tradable = _terms_value(row, "is_tradable")
    status = str(_terms_value(row, "trading_status") or "").strip().lower()
    if is_tradable is True or status in {"tradable", "private_tradable"}:
        return False
    if is_tradable is False or status in {"pending", "private_pending"}:
        return True
    return False


def _never_entered_market(terms: Any) -> bool:
    """起息日与上市日**同时**缺失 = 没有任何证据表明这只债进过市场。

    与「字段明确才剔除」不冲突: 这里缺的不是一个字段, 而是**两个互为兜底**的字段, 而它们
    的缺失模式本身是确定的 —— 实测全库 1058 只里"有上市日却没有起息日"的 **0 只**,
    "有起息日却没有上市日"的 35 只 (那是已发行未上市 / 老债数据缺口, 由
    :func:`is_issued_pending_listing` 与后面的保守规则分别处理)。两个都没有只命中 10 只,
    其中 9 只是私募/非标代码段 (余额全为 0, 本就被别的判据剔除)。

    唯一漏进主池的那只是 **123095.SZ 日升转债**: 2021-01 发行申购, 2021-02 东方日升业绩
    预告大幅亏损后**撤销发行**、申购资金退回, 从未上市交易。Wind 里仍留着代码与到期日
    (2027-01-22) 和一个 99.994 的陈旧价, 于是它带着 AA 评级被定出 −14% 低估躺在主池里。
    三个独立来源都说它不在市: akshare 现货表查无、akshare 发行表查无、Wind
    ``list_tradable_cbs`` 也不返回它 (它是主池里唯一没有 Wind 条款戳的债)。

    ``infer_cb_trading_metadata`` 兜不住这一档: 两个日期都没有时 ``tradable_date`` 为 None,
    而那里的 ``inferred_is_tradable = tradable_date is None or ...`` 把"没有日期"读成了
    "随时可交易" —— 那个默认对定向债是对的 (它们本就没有明确可交易日), 对撤销发行的
    公募债则恰好反了。

    **必须同时要求有到期日**, 判据才是"条款齐备却从未进入市场"这个语义, 而不是泛泛的
    "字段缺失": 到期日是发行时就定下的条款, 有它说明这份记录是一只被**设计出来**的债;
    再配上两个入市日期都没有, 才构成"设计了但没发出去"。少了这一条, 判据会退化成对任何
    信息不全的记录都开火 —— 与「字段明确才剔除」直接冲突。
    """
    return (_terms_date(terms, "maturity_date") is not None
            and _terms_date(terms, "issue_date") is None
            and _terms_date(terms, "listing_date") is None)


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
    for keyword in _INTRADAY_HALT_KEYWORDS:
        status = status.replace(keyword, "")
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


def _underlying_limit_down_threshold(stock_code: Any, *, is_st: bool = False) -> float:
    """正股跌停阈值 (%, 负数).

    三档, 按**板块**而不是按公司: 创业板 (30x) / 科创板 (68x) 一律 20% —— 那是板块级
    规则, ST 不改变它; 沪深主板普通股 10%, 而**主板的 ST/*ST 是 5%**。

    ``is_st`` 这一档此前不存在, 于是主板 ST 股跌停当天 ``underlying_pct_change``
    只有 −5.0, 判据 ``pct <= -9.5`` **恒为假** —— 「正股跌停」对这一类结构性不亮。
    此前这条路是死的 (ST 债在准入层就被剔了, 根本走不到标注), 2026-08-31 把 ST 从硬剔除
    降级成标签之后判据本身才有机会跑到; 策略层的 ``exclude_underlying_limit_down``
    也直接读它。旧 docstring 里那句"ST 正股的 5% 限制不在此处理"当时是空转的。

    **但"判据能跑到"不等于"检测器在工作"**: 2026-08-31 复测, 主池 311 只里
    ``underlying_pct_change`` **一只都没有值** (全库 702/1059 有值, 但那 702 只
    没有一只在主池里、429 只已终止 —— 是旧同步留下的存量, 靠 ``merge_admission_status``
    的 None 保护才没被清掉)。也就是说 Wind 现在这个字段返回 None, 而
    ``_underlying_at_limit_down`` 对 None 直接返回 False, 于是「正股跌停」实测
    **0/311** 命中。接在恒空输入上的检测器是静默的, 不是响的 —— 所以
    ``cb-data-doctor`` 加了一条**按主池**量的覆盖率检查 (全库口径量不出来:
    档案库里的存量值会把停摆盖住, 全库看是 66%)。

    阈值留 0.5% 余量, 避免数据源 pct_chg 取整偏差导致漏识别。
    """
    raw = str(stock_code or "").upper().strip()
    if "." in raw:
        plain, _, _ = raw.partition(".")
    else:
        plain = raw
    if plain.startswith(("30", "68")):
        return -19.5
    return -4.5 if is_st else -9.5


def _underlying_at_limit_down(terms_or_row: Any, stock_code: Any = None) -> bool:
    pct = finite_float(_terms_value(terms_or_row, "underlying_pct_change"))
    if pct is None:
        return False
    code = stock_code if stock_code is not None else _terms_value(terms_or_row, "underlying_code") or _terms_value(terms_or_row, "stock_code")
    # ST 判定走同一个 ``_underlying_has_st_risk`` —— 「正股风险」标签用的也是它,
    # 两处不许各写一份 (同一行不能一边说"正股 ST"、一边按非 ST 的阈值判跌停)。
    return pct <= _underlying_limit_down_threshold(
        code, is_st=_underlying_has_st_risk(terms_or_row))


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
                "confidence", "risk_tags",
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
    """条款缓存里这只债的抓取日 —— 快照已含该日之前生效的全部条款变更。

    走 ``cache.terms_fetched_at`` 这个单一事实源。曾经这里自己写了一份**只读全局戳**的
    实现, 而 ``cache.py`` 那两份早就改成按来源桶取了 —— 逐字重复的三份代码只有两份被修,
    留下的这份静默给主池条款投影用错锚 (实测 3 只债的 patch 被多裁 5 天; 每跑一次
    状态刷新/评级同步/事件同步, 全局戳就往前推一次, 影响只会变大)。
    """
    return terms_fetched_at(terms_cache, code, source=TERMS_SYNC_SOURCE)


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
    """取条款上的日期字段。

    用 ``safe_date`` 不用 ``to_date``: ``pandas.NaT`` 是 ``datetime`` 的**子类**且
    ``bool(NaT)`` 为真, ``to_date`` 既不抛异常也不回落, 会把 NaT 原样放行 ——
    下游拿它和真 date 比较时抛 ``TypeError: Cannot compare NaT with datetime.date``,
    而这里的 ``except Exception`` 在上游, 接不到。
    """
    return safe_date(_terms_value(terms, key))


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
    - confidence / risk_tags: 结果可信度与复核提示
    """
    out = dict(row)
    if out.get("status") != "ok":
        out.setdefault("risk_tags", [])
        out.setdefault("confidence", "低")
        out.setdefault("quality_score", float("nan"))
        out.setdefault("double_low", float("nan"))
        out.setdefault("relative_deviation", float("nan"))
        out.setdefault("event_flags", [])
        out.setdefault("down_reset_trigger_gap", float("nan"))
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
    # 「质量分」= 评级档 + 大余额加分, 即"与错定价无关"的那部分。它原是从
    # ``opportunity_score`` 里拆出来单独记账的一支; 那个分已整体删除 (实测 95% 的行
    # 低估项恒为 0, 它度量的其实就是信用质量), 质量分留下来单做展示与审计。
    quality_score = 0.0
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
        # 双低值 = 转债价格 + 转股溢价率x100 (越低越好)。口径与
        # strategy_backtest._rank_signal_value("double_low") 逐字一致, 两边不得分叉。
        out["double_low"] = market + conversion_premium * 100.0
        if conversion_premium < -0.03:
            risk_tags.append("转股折价")
        elif conversion_premium < 0.03:
            risk_tags.append("贴近转股价值")

    if theo is not None and parity and parity > 0:
        model_premium = theo / parity - 1.0
        out["model_premium_to_parity"] = model_premium
        if model_premium > 0.45:
            risk_tags.append("模型溢价高")
            confidence_points -= 12

    if deviation is not None:
        out["undervaluation_rate"] = -deviation
        # 「模型低估」**已退役** (2026-08-31): 判据是绝对阈值 ``deviation < −8%``, 而全市场
        # 中位偏差本身在 +0.4%~+21.6% 之间整体漂移 —— 这正是本仓库反复否定的"绝对阈值锚在
        # 时变量上"。实测全池只命中 **1/284**; 拿 cb_valuation_history 里 21 期真实锚做扫描,
        # 它的命中数在 1 → 76 (0.4% → 27%) 之间摆, 而横截面口径的两个标签稳在 23 / 31。
        # 便宜度只留一个标签, 用工作的那个 (「深度低估待核」, 相对全市场中位 ≤ −20pp);
        # 两者今天几乎不重叠 (交集 1), 且在 21 期里的 16 期它数学上是后者的子集。
        # 字符串仍登记在册, 旧缓存照样读得出来 (见 RETIRED_RISK_TAGS)。
        # **按方向拆成两个标签, 而不是一个对称的"异常"**。贵侧与便宜侧的后验含义相反:
        #   贵侧  市价远高于模型对同期市场的一般水平 → 模型解释不了这个价, 属模型适用性;
        #   便宜侧 市价远低于模型 → 这正是本工具存在的理由, 是**待检验的假设**而不是噪声。
        # 曾用对称判据, 结果是唯一一只机会分 ≥8 的债 (美锦转债 dev −0.158) 被自己标成
        # "异常"踢出低估候选 —— 等于系统性删掉唯一的假设来源。
        anchor = market_median_deviation if market_median_deviation is not None else 0.0
        gap = deviation - anchor
        # 相对偏差 = 这只债比全市场中位贵/便宜多少。这是**横截面**口径, 与随周期
        # 在 +0.4%~+21.6% 之间摆动的绝对 deviation 不同, 它的分布形状跨 regime 稳定,
        # 因此「低估候选」视图与排序都锚在它上面 (见 MIN_RELATIVE_CHEAPNESS)。
        out["relative_deviation"] = gap
        out["market_median_deviation"] = anchor
        # **出处要留痕**: anchor 回落 0.0 时 gap 恒等于 deviation —— 一个绝对量顶着
        # 横截面量的名字, 而且看上去完全正常 (实测派克转债 有锚 +26.7 / 无锚 +47.5)。
        #
        # 数值本身**一个字节不动**: 把它改写成 NaN 会连带关掉小批量的「低估候选」
        # 视图与便宜度秩 (_cross_sectional_cheapness_gate 直接报「缺少相对偏差」),
        # 那是默认选债行为变更 —— 实测三条现存用例当场变红。所以只加一个出处标记,
        # 由两类消费者各自决定要不要信:
        #   · cross_section_anchor_from —— 跳过这种行, 否则那个假的 0.0 会被当成真锚
        #     捡走, 把污染传给下一批标注;
        #   · 关注池展示层 —— 打「—」而不是打一个没有分母出处的横截面数字。
        # cross_section_origin 早就登记在 watchlist_cache.CACHE_FIELDS 里, 只是从来
        # 没有生产代码写过它。
        out["cross_section_origin"] = ("market_median" if market_median_deviation is not None
                                       else "absolute_fallback")
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
            confidence_points -= penalty
        elif sigma > 0.60:
            risk_tags.append("较高HV")
            confidence_points -= 6.0
    else:
        risk_tags.append("无HV")
        confidence_points -= 20

    if balance is not None:
        if balance <= 0:
            # 余额清零 = 已转股完毕/已赎回, 是退市信号而不是"数据异常"
            risk_tags.append("余额清零")
            confidence_points -= 35.0
        elif balance < BALANCE_DELISTING_LINE:
            # 低于 3,000 万法定线, 交易所将安排停止交易 —— 可执行的判断, 不是笼统的"小"
            risk_tags.append("触及摘牌线")
            confidence_points -= 25.0
        elif balance < 1.0:
            # 「临近摘牌线」(0.3~0.5 亿) 这一档已退役 (2026-08-31): 余额那一族本来就是同一个
            # 连续量的四个刻度, 而 0.5 这一刻没有法定依据也不对应任何策略阈值 (法定线是 0.3,
            # 策略阈值是 min_outstanding_balance=1.0)。实测主池在每个可测日期上这条带都是空的
            # —— 全库落在 [0.3, 0.5) 的只有甬矽转债 (已退市) 与智转债K1 (不可交易, 且代码段
            # 就不是公募转债), 两只都进不了池。落进这条带的债现在打「小余额」, 阈值严格更宽,
            # 不会漏标。字符串仍登记在册, 旧缓存照样读得出来。
            risk_tags.append("小余额")
            confidence_points -= 14.0
        elif balance >= 10.0:
            quality_score += 2.0
    # 余额缺失**不再打「无余额」** (2026-08-31): 实测主池 0/311 缺失, 全库 1059 只里只有 2 只
    # (日升转债 —— 那只撤销发行的幽灵债, 已被「无发行与上市日期」剔除), 7 个历史快照一致 2~3 只
    # 且全部在池外。它检测的是一个从不缺失的字段, 而余额来自本地条款库而不是每日 HTTP 端点
    # —— cb_data.json 读不出来的话是全字段一起失败, 逐行标签帮不上忙 (这是它与「无市价」的
    # 不对称之处: 后者的数据源本月真的挂过)。

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
            confidence_points -= 14.0
        elif t_years < 1.0:
            risk_tags.append("近到期")
            confidence_points -= 7.0

    if rating:
        # 一律走 _rating_score 归一, **不要再用裸前缀匹配**。历史上这里是自成一套的
        # 前缀判断, 与准入层的 _rating_score 两套口径, 于是:
        #   ① 阶梯倒挂: "AAA".startswith("AA+") 为假 → AAA 落到 +2.0 分支, 比 AA+ 的
        #      +3.0 还低、与 AA 同分 (实测影响 15 只);
        #   ② 带展望后缀的评级被误判: "AAsti" (AA/稳定) 三个 AA 分支全不匹配, 掉进
        #      LOW_RATING_PREFIXES 的 "A" → 被打「低评级」并扣 8 分 12 置信度
        #      (华峰转债 118071.SH / 联瑞转债 118064.SH), 而同一字符串在准入层
        #      _rating_score("AAsti") 正确返回 16 (=AA)。
        # 分档边界与修复前逐档等价 (低评级 <=> A+ 及以下), 只有上面两类错判被纠正。
        rating_score = _rating_score(rating)
        if rating_score is None:
            pass                                # 无法识别的评级: 不加分也不打标签
        elif rating_score >= _RATING_SCORES["AAA"]:
            quality_score += 3.5
        elif rating_score >= _RATING_SCORES["AA+"]:
            quality_score += 3.0
        elif rating_score >= _RATING_SCORES["AA"]:
            quality_score += 2.0
        elif rating_score >= _RATING_SCORES["AA-"]:
            quality_score += 0.5
        else:
            risk_tags.append("低评级")
            quality_score -= 8.0
            confidence_points -= 12.0
    # 评级缺失**不再打「无评级」** (2026-08-31): 与「无余额」同一条论证 —— 实测主池 0/311,
    # 全库 1059 只里只有 1 只 (且在池外), 7 个历史快照一致。评级同样来自本地条款库。
    # 顺带修掉一个错误的连带效果: 它是 DIM_DATA 因此进 BLOCKING_RISK_TAGS, 于是"评级取不到"
    # 会把整行染灰并踢出「低估候选」—— 而「评级」列只会渲染一个「—」。那是标的属性缺失,
    # 不是这一行的数坏了。

    if _underlying_has_st_risk(out):
        risk_tags.append("正股风险")
        confidence_points -= 30.0
    if _underlying_suspended(out):
        risk_tags.append("正股停牌")
        confidence_points -= 25.0
    if _public_trading_status_reason(out) == "停牌/暂停交易":
        risk_tags.append("转债停牌")
        confidence_points -= 25.0

    if _underlying_at_limit_down(out, out.get("stock_code")):
        risk_tags.append("正股跌停")
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
    if theo is None or theo <= 0:
        risk_tags.append("理论价异常")
        confidence_points -= 30.0

    confidence_points = max(0.0, min(100.0, confidence_points))
    if confidence_points >= 78:
        confidence = "高"
    elif confidence_points >= 55:
        confidence = "中"
    else:
        confidence = "低"

    out["risk_tags"] = _dedupe_tags(risk_tags)
    out["confidence"] = confidence
    out["quality_score"] = quality_score
    out["event_flags"] = event_flags(out)
    out["down_reset_trigger_gap"] = down_reset_trigger_gap(out)
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


def _selection_cutoff(n: int, percentile: float) -> int:
    """取"最便宜的 percentile"时实际保留几行 —— 秩口径, 小批量有下限保护。"""
    if n <= 0:
        return 0
    return min(n, max(MIN_VIEW_ROWS, math.ceil(percentile * n)))


def _assign_cross_sectional_ranks(rows: list[dict]) -> list[dict]:
    """就地写入横截面秩与分位 (0 / 0.0 = 本批最便宜), 返回同一个列表。

    秩是**这一批**内部的精确名次, 不是对全市场的估计, 因此不设最小样本门槛 ——
    与 median_deviation_of 的 <30 退化规则不同, 那里退化是因为中位数被当作市场水平
    的估计量在用, 估计不准会静默污染标签; 而名次在任何批量大小下都良定义。
    调用方要对"这批是不是有代表性"负责 (关注池/新债那种子集本就不该被当成市场)。
    """
    # (名次键, 分位键, 总数键, 取值字段, 是否越大越靠前)
    for rank_key, pct_key, total_key, source, descending in (
        ("cheapness_rank", "cheapness_percentile", "cheapness_rank_total",
         "relative_deviation", False),
        ("double_low_rank", "double_low_percentile", "double_low_rank_total",
         "double_low", False),
    ):
        ranked: list[tuple[float, str, int]] = []
        for idx, row in enumerate(rows):
            if row.get("status") != "ok":
                continue
            value = finite_float(row.get(source))
            if value is None:
                continue
            if descending:
                value = -value          # 统一成"升序 = 越靠前越好"
            # 并列按代码稳定排序, 免得同值行的名次随输入顺序漂移
            ranked.append((value, str(row.get("bond_code") or ""), idx))
        ranked.sort()
        total = len(ranked)
        for rank, (_value, _code, idx) in enumerate(ranked):
            rows[idx][rank_key] = rank
            rows[idx][pct_key] = rank / (total - 1) if total > 1 else 0.0
            # 名次总数随行走, 这样 view_exclusion_reason 仍是**单行**判据 ——
            # 它被 strategy_backtest._candidate_filter_reason 共用, 签名不能变。
            rows[idx][total_key] = total
        for row in rows:
            row.setdefault(rank_key, None)
            row.setdefault(pct_key, None)
            row.setdefault(total_key, None)
    # 名次到位后重算分桶 —— 单行标注时只有相对便宜度下限生效, 长度上限要等到这里。
    for row in rows:
        row["review_bucket"] = _review_bucket(row)
    return rows


#: ``_assign_cross_sectional_ranks`` 写出的全部字段。小批量标注时要**显式清空**
#: 它们 —— 见 ``annotate_batch_results`` 的 ``rank_scope``。
_CROSS_SECTIONAL_RANK_FIELDS = (
    "cheapness_rank", "cheapness_percentile", "cheapness_rank_total",
    "double_low_rank", "double_low_percentile", "double_low_rank_total",
)


def cross_section_anchor_from(results: Sequence[dict]) -> float | None:
    """从一批**已标注**的结果里取回横截面锚 (全市场中位偏差)。

    优先直接读行内的 ``market_median_deviation`` —— ``annotate_batch_result`` 会把
    当时用的锚原样写进每一行 (实测主池缓存 283/284 行有值), 所以一份读回来的
    ``batch_pricing_cache.json`` 自带它当初的锚, 不需要重算。取不到才退回自算。

    **不要改成从 ``_meta`` 读**: 实测 ``batch_pricing_cache.json`` 的 ``_meta`` 键
    只有 ``{saved_at, source, params, n_results, n_upcoming_results, summary}``,
    那条路永远取不到值, 而且会静默落到"返回 None"那一档 —— 于是关注池又变回
    自算中位, 与本函数要解决的问题完全一样。
    """
    for row in results or ():
        if not _anchor_is_market_wide(row):
            continue
        value = finite_float(row.get("market_median_deviation"))
        if value is not None:
            return value
    return median_deviation_of(results or ())


def _anchor_is_market_wide(row: dict) -> bool:
    """这一行的 ``market_median_deviation`` 是真锚, 还是绝对阈值兜底留下的 0.0.

    缺 ``cross_section_origin`` 的行按**真锚**处理: 那是本字段落地之前写的存量
    结果 (``batch_pricing_cache.json`` 里全是这种), 而它们绝大多数确实是全池标注
    出来的。反过来把它们一律判成假锚会让关注池整页的「相对偏差」立刻空掉。
    """
    return str(row.get("cross_section_origin") or "market_median") != "absolute_fallback"


def cross_section_anchor_as_of(results: Sequence[dict]) -> date | None:
    """``cross_section_anchor_from`` 取回的那个锚, **它自己**是哪一天的.

    判据与那个函数逐行对齐: 锚来自第一个带 ``market_median_deviation`` 的行, 所以
    as-of 就是**那一行**的估值日; 走自算兜底时锚是这批行的中位, as-of 取这批里
    第一个有估值日的行。

    单独成一个函数而不是让 ``cross_section_anchor_from`` 多返回一个值, 是因为后者
    已有两个调用方 —— 而这个日期只有落盘与展示需要。

    **不要用 ``market_today()`` 代替它**: 锚来自 ``batch_pricing_cache.json`` 里
    上一次全市场重算的行, 那可能是几天前 (实测热缓存记着 2026-08-28, 锚源行是
    08-26)。把今天的日期盖上去, 锚的年龄在盘上恒为 0, 于是 ``anchor_is_stale``
    接上了也判不出陈旧 —— 恰好在"天天点 ⚡ 但久不跑全量重算"这个常态用法上失效。
    """
    fallback: date | None = None
    for row in results or ():
        day = _coerce_valuation_date(row.get("valuation_date"))
        if (_anchor_is_market_wide(row)
                and finite_float(row.get("market_median_deviation")) is not None):
            return day
        if fallback is None:
            fallback = day
    return fallback


def _coerce_valuation_date(value: Any) -> date | None:
    """宽松解析估值日: 落盘走 ISO 字符串, 内存里是 date."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def annotate_batch_results(results: Sequence[dict], *,
                           market_median_deviation: float | None = None,
                           rank_scope: bool = True) -> list[dict]:
    """补齐批量研究字段, 不改变输入列表.

    *market_median_deviation* 缺省时从这批结果自算 (样本 <30 则退回绝对阈值)。
    给关注池/新债这类小批量标注时, 应显式传主池的中位数 (见
    :func:`cross_section_anchor_from`), 否则自算出来的中位没有代表性。

    *rank_scope* 说的是"这一批本身是不是一个有代表性的横截面"。**传锚修不了秩** ——
    ``_assign_cross_sectional_ranks`` 是在**传进来的这一批内部**排名次, 与锚无关,
    而且末尾还用这些名次重算 ``review_bucket``。实测 123281.SZ 在全池 284 行里
    ``cheapness_percentile=0.8794``, 单独拿 6 行子集算就变成 0.0 —— 一个"全市场最
    便宜的 0%"标签, 数字看上去完全正常。所以小批量必须 ``rank_scope=False``,
    把这一族字段显式写成 None, 让展示层打「—」而不是打一个假名次。

    横截面秩 (cheapness_rank / double_low_rank) 在这一层算 —— 单行的
    ``annotate_batch_result`` 拿不到population, 「低估候选」「双低」两个视图的
    长度上限就依赖它。因此**视图过滤前必须先过这个函数**
    (``filter_batch_results_by_view`` 经 ``sort_batch_results_for_review`` 保证了这点)。
    """
    # 自算 = 锚就是**这一批**自己的中位, 于是锚与行同日。行里可能还留着上一轮
    # 小批量标注盖上的 market_median_deviation_as_of (关注池 worker 的结果会经
    # merge_watchlist_pricing 回流进主池列表, 再被这里整体重标注), 那个戳此刻已经
    # 过期 —— 不清掉会让刚用今天的池子重标注过的行显示成"锚是几天前的"。
    self_anchored = market_median_deviation is None
    if market_median_deviation is None:
        market_median_deviation = median_deviation_of(results)
    annotated = [annotate_batch_result(row, market_median_deviation=market_median_deviation)
                 for row in results]
    if self_anchored:
        for row in annotated:
            row.pop("market_median_deviation_as_of", None)
    if not rank_scope:
        # review_bucket 保留单行标注的结果: 没有 population 就没有"长度上限"这回事,
        # 只有相对便宜度下限生效 —— 那正是单行版本已经算好的语义。
        for row in annotated:
            for key in _CROSS_SECTIONAL_RANK_FIELDS:
                row[key] = None
        return annotated
    return _assign_cross_sectional_ranks(annotated)


def sort_batch_results_for_review(results: Sequence[dict]) -> list[dict]:
    """按实际复核价值排序: 成功行优先, 偏差升序 (最便宜的在前).

    **原来的第一排序键是机会分降序, 已随 opportunity_score 一并删除**。那个键在
    展示上早就看不见了 —— ``sort_batch_results_for_view`` 对 **5 个视图里的 4 个**
    走重排分支, 只有「需复核」(2026-09-03 实测 11 行) 沿用本函数的顺序。现在它按
    偏差升序, 与其余视图同向, 而且是表上看得见的量。
    """
    annotated = annotate_batch_results(results)

    def key(row: dict):
        deviation = finite_float(row.get("deviation"))
        ok_rank = 0 if row.get("status") == "ok" else 1
        deviation_rank = deviation if deviation is not None else float("inf")
        return (ok_rank, deviation_rank, row.get("bond_code") or "")

    return sorted(annotated, key=key)


# ── 「低估候选」的横截面口径 ────────────────────────────────────────────────
#
# 旧口径是 opportunity_score >= 8.0 —— 一个**绝对**阈值架在一个水平时变的量上。
# 实测 2026-08-22 主池 280 只: 低估候选 1 只、转股折价 0 只, 页面默认打开等于空表。
#
# 根因: score 的低估项是 max(0, -deviation)*100, 而全市场中位 deviation = +18.7%,
# 于是 258/280 (92%) 的行这一项恒为 0, 分数完全由评级/余额加分与风险惩罚决定 ——
# 「机会分」在九成的债上度量的是**信用质量**而不是错定价 (秩相关 score vs deviation
# 只有 -0.63, 纯错定价排序应为 -1.0)。
#
# 为什么换成分位而不是换个数: cb_valuation_history 的 20 期季度基线实测,
#   中位偏差 (水平)     +0.4% ~ +21.6%, 摆幅 21.2pp  → 绝对阈值随周期整体塌缩/泛滥
#   IQR (横截面离散度)   0.103 ~ 0.181, 摆幅  7.7pp
#   p25 - 中位 (便宜尾)  -9.6pp ~ -5.4pp, 摆幅 4.2pp → 跨完整牛熊周期几乎不变
# 便宜尾的**形状**稳定而水平不稳定, 所以判据必须锚在当期横截面上。
#
# 两道闸串联, 各自管一件事 —— 缺了任一道都会退化:
#   ① 相对便宜度下限 MIN_RELATIVE_CHEAPNESS —— 「比市场中位便宜至少这么多」。
#      取 5pp 是因为历史上 p25-中位 从未浅于 -5.4pp, 即该线在任何 regime 下都不松于
#      "最便宜的四分之一"。**只有它**能表达"今天真的没有便宜货": 离散度塌掉时候选数
#      诚实归零, 而单靠分位永远凑得满 15%。
#   ② 名单长度上限 DEFAULT_UNDERVALUED_PERCENTILE —— 人工复核一次能看完的量。
#      **只有它**能挡住反过来的那一天: 熊市谷底中位偏差压到 0 时闸① 会放行几百只。
# 长度上限按**秩**而不是按分位阈值实现 (见 _selection_cutoff): 小批量 (关注池/新债/
# 单元测试常见的个位数行) 下分位数没有意义, 秩仍然良定义, 且 MIN_VIEW_ROWS 保证
# 小批量不会被 15% 削到只剩一行。
DEFAULT_UNDERVALUED_PERCENTILE = 0.15
MIN_RELATIVE_CHEAPNESS = 0.05
# 长度上限的下限: 批量再小也至少保留这么多行, 免得 15% 在小样本上把名单削没。
MIN_VIEW_ROWS = 10
# 双低 = 转债价格 + 转股溢价率x100, 国内转债最通用的实操筛子; strategy_backtest 早已
# 有 double_low 排序信号, 批量页此前既无列也无视图。同样用分位而非绝对值: 价格中枢
# 与溢价中枢都随周期漂移 (今日主池市价中位 132.9, p95 297.7)。
DEFAULT_DOUBLE_LOW_PERCENTILE = 0.15


def _cross_sectional_cheapness_gate(row: dict) -> str | None:
    """「低估候选」的当期横截面判据: 相对便宜度下限 + 名单长度上限。

    两道闸的分工与实测依据见 MIN_RELATIVE_CHEAPNESS / DEFAULT_UNDERVALUED_PERCENTILE
    的注释。这里只做判定, 不做估计 —— 相对偏差与名次都已由 annotate_batch_results
    在有 population 的那一层算好。
    """
    relative = finite_float(row.get("relative_deviation"))
    if relative is None:
        return "缺少相对偏差"
    if relative > -MIN_RELATIVE_CHEAPNESS:
        return (f"相对市场中位 {relative * 100:+.1f}pp, "
                f"未便宜过 {MIN_RELATIVE_CHEAPNESS * 100:.0f}pp")
    rank = finite_float(row.get("cheapness_rank"))
    total = finite_float(row.get("cheapness_rank_total"))
    if rank is not None and total is not None:
        cutoff = _selection_cutoff(int(total), DEFAULT_UNDERVALUED_PERCENTILE)
        if rank >= cutoff:
            return f"便宜度排第 {int(rank) + 1}/{int(total)}, 不在最便宜的 {cutoff} 名内"
    return None


def view_exclusion_reason(row: dict, view: str | None) -> str | None:
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
        if not ok:
            return "定价未成功"
        reason = _cross_sectional_cheapness_gate(row)
        if reason is not None:
            return reason
        if row.get("confidence") not in {"高", "中"}:
            return "置信度不足"
        if "转股折价" in tags:
            return "转股折价单独归类"
        blocking = tags & BLOCKING_RISK_TAGS
        if blocking:
            return "拦截标签 " + "/".join(sorted(blocking))
        return None
    if view_name == "双低":
        if not ok:
            return "定价未成功"
        value = finite_float(row.get("double_low"))
        if value is None:
            return "缺少双低值"
        rank = finite_float(row.get("double_low_rank"))
        total = finite_float(row.get("double_low_rank_total"))
        if rank is not None and total is not None:
            cutoff = _selection_cutoff(int(total), DEFAULT_DOUBLE_LOW_PERCENTILE)
            if rank >= cutoff:
                return f"双低 {value:.0f} 排第 {int(rank) + 1}/{int(total)}, 不在最低 {cutoff} 名内"
        # 双低是纯市场量 (价格 + 溢价), 不含模型输出, 因此**不**加置信度闸 ——
        # 那是模型可信度, 与这个筛子无关。可交易性/数据质量的拦截标签仍然生效。
        blocking = tags & BLOCKING_RISK_TAGS
        if blocking:
            return "拦截标签 " + "/".join(sorted(blocking))
        return None
    if view_name == "转股折价":
        if not ok:
            return "定价未成功"
        return None if "转股折价" in tags else "未出现转股折价标签"
    if view_name == "需复核":
        if not ok or (tags & BLOCKING_RISK_TAGS) or row.get("confidence") == "低":
            return None
        return "不属于复核池"
    return None


def filter_batch_results_by_view(results: Sequence[dict], view: str | None) -> list[dict]:
    """按批量页视图过滤结果, 并保持研究排序.

    「低估候选」一律走当期横截面口径 (相对便宜度下限 + 名单长度上限)。旧的绝对
    机会分阈值 (``opportunity_score >= 8.0``) 已随该字段一并删除 —— 它是个**绝对**
    阈值架在一个水平时变的量上, 实测全池 0/283 够得着。
    """
    rows = sort_batch_results_for_review(results)
    return [row for row in rows if view_exclusion_reason(row, view) is None]


def _by_listing_date_desc(row: dict):
    """上市日倒序; 没有上市日的沉底。

    日期取负而不是 ``reverse=True`` —— 后者会把"缺值沉底"那一项一起翻上来。
    取值走 ``safe_date``: 缓存里是 ISO 串、内存里是 ``date``, 而 ``pandas.NaT`` 是
    ``datetime`` 子类且为真值, ``to_date`` 会原样放行它 (见 ``safe_date`` 的 docstring,
    它点名的正是 ``listing_date``)。
    """
    listed = safe_date(row.get("listing_date"))
    return (listed is None, -listed.toordinal() if listed is not None else 0)


def sort_batch_results_for_view(results: Sequence[dict], view: str | None) -> list[dict]:
    """按视图**该看的顺序**排列 —— 展示层用, 不改变 sort_batch_results_for_review。

    为什么还要分成两个函数: ``sort_batch_results_for_review`` 是「需复核」视图与
    ``filter_batch_results_by_view`` 的输入顺序 (偏差升序), 而各视图要看的顺序不同
    —— 「双低」按双低升序。两者的消费者不同, 合并会让改一个
    视图的展示顺序变成改另一个的默认行为。
    (原来这里的第一排序键是机会分降序, 已随 ``opportunity_score`` 一并删除。)

    ⚠️ 本函数**只排序, 不重新标注**。输入必须是已经过 ``annotate_batch_results``
    的行 —— 典型用法是接在 ``filter_batch_results_by_view`` 之后。在过滤后的子集上
    重新标注会把中位锚与横截面名次算到**子集**上 (38 只的中位不是市场的中位),
    相对偏差和视图归属会随之整体漂移。
    """
    rows = list(results)
    view_name = view if view in BATCH_REVIEW_VIEWS else "综合机会"

    def by(key: str):
        def sort_key(row: dict):
            value = finite_float(row.get(key))
            return (
                0 if row.get("status") == "ok" else 1,
                value if value is not None else float("inf"),
                str(row.get("bond_code") or ""),
            )
        return sort_key

    if view_name == "双低":
        return sorted(rows, key=by("double_low"))
    if view_name == "综合机会":
        # 全池按**上市日倒序** (最新在前) —— 全部标的一起排, 不按定价成没成功分组。
        # 它是分母不是筛子, 便宜度那一路已由「低估候选」承担 (实测两者按相对偏差排时
        # 前 43 行重合 43/43, 全池再排一遍等于把同一屏看两次)。
        return sorted(rows, key=_by_listing_date_desc)
    if view_name in {"低估候选", "转股折价"}:
        # 相对偏差升序 = 相对全市场最便宜的在前
        return sorted(rows, key=by("relative_deviation"))
    return rows                                   # 需复核: 保持研究排序


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
        "conversion_premium", "model_premium_to_parity",
    }:
        return ""
    value = row.get(column, "")
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    if column in {
        "deviation", "conversion_premium", "model_premium_to_parity",
        "effective_p_down_1y_prob",
    }:
        return f"{float(value):.6f}" if value != "" else ""
    if column == "undervaluation_rate":
        return f"{float(value):.6f}" if value != "" else ""
    if column == "parity":
        return f"{float(value):.4f}" if value != "" else ""
    if column in {"risk_tags", "event_flags", "review_notes"} and isinstance(value, list):
        return "|".join(str(item) for item in value)
    if column == "down_reset_trigger_gap":
        return f"{float(value):.6f}" if value != "" else ""
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
        "conversion_premium", "model_premium_to_parity",
        "quality_score", "double_low", "relative_deviation",
        "market_median_deviation", "cheapness_percentile", "down_reset_trigger_gap",
        "undervaluation_rate", "no_down_price", "down_reset_uplift",
        "effective_p_down_1y_prob",
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
    # 「正股跌停」此前漏在外面 —— 它是 DIM_TRADABILITY 里**唯一**没被收进来的一个,
    # 而这一档收的就是可交易性/条款类的标签。
    if {"余额清零", "触及摘牌线", "临近摘牌线", "小余额", "临近摘牌", "短久期",
            "低评级", "正股风险", "正股停牌", "转债停牌", "正股跌停",
            "极小余额", "余额异常"} & tag_set:
        return "条款/流动性敏感"
    if confidence == "高":
        return "较稳健"
    if confidence == "中":
        return "一般"
    return "需复核"


# ── 事件旗标 ────────────────────────────────────────────────────────────────
#
# 与 risk_tags **分成两族, 不要合并**:
#   risk_tags    驱动策略排除集 (LEGACY_STRATEGY_EXCLUDE_TAGS)、置信度扣分与视图拦截
#                —— 往里加一个标签就是默认选债行为变更, 要单独立项。
#   event_flags  只进展示与 CSV 导出, 回答"这只债现在有没有正在发生的事"。
#
# 这些字段此前**全部算好了却一个都不显示**: 实测 2026-08-22 主池 280 只里, 2 只有在途
# 下修提议 (生效日 08-24 / 09-05)、1 只已公告强赎、67 只有不强赎承诺 (强赎上限被暂时
# 解除, 是实打实的正面信息)、42 只暂停转股、82 只有回售窗口 —— 而"找交易机会"的页面
# 一个都看不到。
#
# 顺序 = 可操作性, 不是字母序: 有硬退出期限的排最前, 纯提示排最后。表格列窄, 只显示
# 前两个, 所以顺序直接决定用户看见什么。
#
# 刻意**不收**两类高频状态: 「已触发下修线」(实测 127/280 = 45%) 与「下修冻结中」
# (186/280 = 66%)。在近半数债上都亮的旗标描述的是市场不是这只债 —— 与标签维度那条
# 教训同源。前者改由「正股/下修线」数值列承载, 后者是模型入参, 不单独展示。
_DOWN_RESET_KIND_LABEL = {"proposed": "下修提议", "approved": "下修已通过"}
# 回售窗口提前多少天开始提示
PUTBACK_NOTICE_DAYS = 60


def event_flags(row: dict) -> list[str]:
    """这一行当前有哪些**确定性的日程/状态安排**, 按可操作性降序。

    纯读 row 上已有的字段, 不做任何取数; 无事件返回空列表。
    """
    val_date = _terms_date(row, "valuation_date")
    flags: list[str] = []

    def md(day: date | None) -> str:
        return day.strftime("%m-%d") if day is not None else "待定"

    # ① 已公告强赎: 唯一带硬退出期限的事件 —— 不转股就按赎回价被赎走
    call_date = _terms_date(row, "call_redemption_date")
    if row.get("redemption_mode") or row.get("call_status") == "已公告强赎":
        flags.append(f"强赎 {md(call_date)}")

    # ② 在途下修: 本工具的核心 thesis, 也是 pricer 建一次性下修节点的依据
    kind = _DOWN_RESET_KIND_LABEL.get(str(row.get("down_reset_scheduled_kind") or ""))
    if kind:
        flags.append(f"{kind} {md(_terms_date(row, 'down_reset_scheduled_date'))}")

    # ③ 回售申报窗口: 开启中是可执行的价格下限, 临近的值得提前排期。
    #
    # **必须同时有起止日才认**。缺 end 的记录不是"窗口还没结束", 而是公告正文没解析出
    # 截止日 —— 此时 effective_start 退化成了**公告日**而不是窗口起始日。实测主池 82 条
    # 有 start 的记录里 29 条缺 end, 全部来自"关于XX转债回售的第N次提示性公告"这类正文
    # (与解析成功的帝欧/长汽同一类公告), 窗口其实早已关闭。按 end is None 当成"仍开启"
    # 会把 30 只债长期错报成「回售中」—— 又一次把**解析残缺**当成**当期状态**。
    put_start = _terms_date(row, "putback_start_date")
    put_end = _terms_date(row, "putback_end_date")
    if val_date is not None and put_start is not None and put_end is not None:
        if put_start <= val_date <= put_end:
            flags.append(f"回售中 至{md(put_end)}")
        elif 0 < (put_start - val_date).days <= PUTBACK_NOTICE_DAYS:
            flags.append(f"回售 {md(put_start)}起")

    # ④ 暂停转股: 转股价值这条腿暂时断了, parity 口径要打折扣看
    if row.get("conversion_suspension_status") == "暂停转股":
        flags.append("暂停转股")

    # ⑤ 不强赎承诺: 强赎上限在承诺期内被解除, 对持有人是正面信息
    no_call_until = _terms_date(row, "call_no_redemption_until")
    if (row.get("call_status") == "不强赎" and no_call_until is not None
            and val_date is not None and no_call_until > val_date):
        # **四位年份**。这一格里别的旗标都是 `%m-%d` (「强赎 09-09」「下修提议 09-05」),
        # 写成 `%y-%m` 的「26-10」和它们长得一模一样却是"2026 年 10 月"——同一列里两种
        # 不可区分的 NN-NN。实测 27/73 有事件的行走这一档 (37%), 而两类事件本就能共存。
        flags.append(f"不强赎至 {no_call_until.strftime('%Y-%m')}")

    return flags


def down_reset_trigger_gap(row: dict) -> float | None:
    """正股价距下修触发线还有多远: ``S0 / (K * trigger_ratio) - 1``。

    负 = 已在触发线下方 (下修博弈已经活了), 0 = 恰在线上。实测主池 127/280 已在线下、
    41 只在线上 10% 以内 —— 这是"哪些债的下修故事正在发生"最直接的一个数, 而它此前
    只作为 pricer 入参存在, 表上没有。
    """
    s0 = finite_float(row.get("S0"))
    k = finite_float(row.get("K"))
    ratio = finite_float(row.get("down_reset_trigger_ratio"))
    if ratio is None:
        pct = finite_float(row.get("down_reset_trigger_pct"))
        ratio = None if pct is None else pct / 100.0
    if s0 is None or k is None or not ratio or k <= 0 or ratio <= 0:
        return None
    return s0 / (k * ratio) - 1.0


def _review_bucket(row: dict) -> str:
    tags = set(row.get("risk_tags") or [])
    if row.get("status") != "ok":
        return "需复核"
    if tags & BLOCKING_RISK_TAGS or row.get("confidence") == "低":
        return "需复核"
    if tags & tags_in(DIM_MODEL):
        # 模型在这只债上不可靠, 但不是"去做点什么"就能解决的 —— 它是永久属性, 该单列
        # 一档而不是塞进需复核。实测这一档 142 只, 塞进需复核会让后者占到 61%。
        return "模型存疑"
    if "转股折价" in tags:
        return "转股折价"
    # 「低估候选」的判据**只此一处**: 直接问 view_exclusion_reason, 不再复制一份阈值。
    # 曾经这里硬编码 score >= 8.0 而视图另写一份, 于是改口径要记得改两处 —— 视图归属
    # 分叉的老毛病 (见 view_exclusion_reason 的 docstring)。
    #
    # 注意单行标注时 cheapness_rank 还不存在 (population 在上一层), 此刻只有相对便宜度
    # 下限生效; _assign_cross_sectional_ranks 排完名会再刷一次分桶补上长度上限。
    if view_exclusion_reason(row, "低估候选") is None:
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
