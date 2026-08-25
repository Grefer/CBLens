"""数据体检: 把"碰巧发现的 bug"变成"每次都查的检查".

这个工具的每一条检查, 都对应一个**真实发生过、且是靠运气才发现**的数据事故:

  - 赎回门槛条款"未转股余额少于3,000万元"被当成真实余额 → 528 条 patch 值恰为 0.3,
    103 只债受污染, 74 只真实余额 ≥0.5 亿的大盘券被准入当成"余额过小"无声剔除
  - 摘牌元数据只有 17/1059, 于是余额门槛在替它兜底, 而它拦不住余额非零的退市券
  - 转股价 patch 解析取到公告里的"历次调整沿革"最早一条 → 万孚转债 14 条 patch 跨两年
    恒为 93.57 (真实 K 20.88), 全库 73.5% 的末条 patch 与当前 K 不符
  - 同发行人两只债的公告串号 → 嘉益转债被写进"精达转债"的转股价
  - 公告事件停更 3 个月, 主池 282 只里只有 7 只是最新的
  - 回测磁盘缓存身份漏了 cb_data.json, 数据修完再跑原样复现修复前的数字

这些全部是**静默失败**: 不抛异常、不写日志、测试全绿, 只表现为"池子里怎么全是边角料"。
所以判据必须是可量化的比率与不变量, 而不是人工翻数据。

用法::

    cb-data-doctor                 # 全部检查
    cb-data-doctor --json          # 机器可读
    cb-data-doctor --only patch    # 只跑某一组
"""
from __future__ import annotations

import argparse
import collections
import json
import math
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

from ..batch_pricing import screen_batch_pool_from_cache
from ..cache import TermsBundle, project_bundle_path
from ..cb_events import project_events_path
from ..historical_terms import TermsPatchStore, project_terms_patches_path
from ..market_time import market_today
from ..paths import data_path

OK, WARN, FAIL = "ok", "warn", "fail"
_ICON = {OK: "✅", WARN: "⚠️ ", FAIL: "❌"}

# 权威源 (Wind as-of 日序列) 覆盖的字段。其余字段只能靠公告正文解析 —— 体检的重点在那里。
_AUTHORITATIVE_FIELDS = ("conversion_price", "outstanding_balance")
# 走了"重大变化过滤"的字段: 末条 patch 与当前值本就不该相等 (微动被有意丢弃),
# 用"末条==当前值"去验它会把设计当成 bug 报。
_MATERIALITY_FILTERED = ("outstanding_balance",)
# 法定停止交易线 3,000 万元 = 0.3 亿。历史上整批 patch 的值恰好落在这里, 因为
# 解析器把"未转股余额少于3,000万元时"这句**门槛条款**当成了当期余额。
_STATUTORY_BALANCE_LINE = 0.3


@dataclass
class Check:
    name: str
    status: str
    detail: str
    because: str
    group: str = ""
    extra: list[str] = field(default_factory=list)


def _pct(part: int, total: int) -> str:
    return f"{part}/{total} ({part / total:.0%})" if total else f"{part}/0"


def _grade(value: float, warn_below: float, fail_below: float) -> str:
    if value < fail_below:
        return FAIL
    if value < warn_below:
        return WARN
    return OK


# ─────────────────────────── 新鲜度 ───────────────────────────

def check_terms_freshness(ctx: dict) -> Check:
    bundle, today = ctx["bundle"], ctx["today"]
    ages = []
    for code in bundle.list_bonds():
        ts = bundle.fetched_at(code)
        if ts is not None:
            ages.append((today - ts.date()).days)
    if not ages:
        return Check("条款抓取新鲜度", FAIL, "没有任何 fetched_at 元信息",
                     "抓取日缺失时无法判断条款是否陈旧", "新鲜度")
    ages.sort()
    fresh = sum(1 for a in ages if a <= 7)
    return Check(
        "条款抓取新鲜度",
        _grade(fresh / len(ages), 0.9, 0.5),
        f"7 天内刷新 {_pct(fresh, len(ages))}; 中位 {ages[len(ages) // 2]} 天",
        "conversion_price 不在日常状态刷新的字段清单里, 摘牌债的 K 会冻结在最后一次全量同步",
        "新鲜度")


def check_event_sync_watermark(ctx: dict) -> Check:
    pool, today = ctx["pool"], ctx["today"]
    synced = (ctx["events_meta"].get("synced_at_by_code") or {})
    never = [c for c in pool if c not in synced]
    stale = [c for c in pool if c in synced
             and (today - date.fromisoformat(synced[c][:10])).days > 14]
    current = len(pool) - len(never) - len(stale)
    return Check(
        "主池公告同步水位",
        _grade(current / len(pool) if pool else 0, 0.9, 0.5),
        f"14 天内已同步 {_pct(current, len(pool))}; 从未同步 {len(never)}, 停更 {len(stale)}",
        "曾停更 3 个月 —— 主池 282 只里只有 7 只是最新的, 下修提议与不下修承诺全没进模型",
        "新鲜度",
        extra=[f"从未同步: {', '.join(never[:8])}" if never else ""])


# ─────────────────────────── 覆盖率 ───────────────────────────

def check_field_coverage(ctx: dict) -> list[Check]:
    bundle = ctx["bundle"]
    codes = list(bundle.list_bonds())
    wanted = {
        "conversion_price": (0.98, 0.90, "K 缺失即无法定价"),
        "delisting_date": (0.90, 0.50,
                           "曾只有 17/1059, 于是余额门槛在替摘牌判据兜底, 却拦不住余额非零的退市券"),
        "credit_rating": (0.95, 0.80, "评级缺失会让准入过滤失去依据"),
        "outstanding_balance": (0.95, 0.80, "余额档位标签与摘牌线判据都依赖它"),
    }
    out = []
    for fname, (warn, fail, because) in wanted.items():
        have = sum(1 for c in codes if getattr(bundle.get(c), fname, None) is not None)
        ratio = have / len(codes) if codes else 0.0
        out.append(Check(f"字段覆盖 · {fname}", _grade(ratio, warn, fail),
                         _pct(have, len(codes)), because, "覆盖率"))
    return out


def check_event_time_coverage(ctx: dict) -> Check:
    by_year = collections.Counter(
        (e.get("event_date") or "?")[:4] for e in ctx["events"])
    years = sorted(y for y in by_year if y.isdigit())
    gaps = [y for y in years if by_year[y] < 50]
    span = f"{years[0]}–{years[-1]}" if years else "空"
    return Check(
        "事件表时间覆盖",
        WARN if (gaps or len(years) < 3) else OK,
        f"覆盖 {span}; 各年 " + ", ".join(f"{y}:{by_year[y]}" for y in years),
        "2024 年之前全库 0 条事件 —— 那几年的强赎/摘牌层是空的, 历史回测据此得出的结论不可信",
        "覆盖率",
        extra=[f"事件稀少的年份: {', '.join(gaps)}" if gaps else ""])


# ─────────────────────────── patch 自洽性 ───────────────────────────

def _patches_by_field(store: TermsPatchStore, fname: str) -> dict[str, list]:
    out: dict[str, list] = collections.defaultdict(list)
    for p in store.list_patches():
        if fname in (p.fields or {}):
            out[p.bond_code].append(p)
    for v in out.values():
        v.sort(key=lambda p: p.effective_date)
    return out


def check_patch_chain(ctx: dict) -> list[Check]:
    store = ctx["patch_store"]
    out = []
    for fname in _AUTHORITATIVE_FIELDS:
        chains = _patches_by_field(store, fname)
        ok = broken = 0
        for seq in chains.values():
            for cur, nxt in zip(seq, seq[1:]):
                before = (nxt.before_fields or {}).get(fname)
                after = cur.fields[fname]
                if before is not None and abs(float(before) - float(after)) < 1e-9:
                    ok += 1
                else:
                    broken += 1
        total = ok + broken
        ratio = ok / total if total else 1.0
        out.append(Check(
            f"patch 链自洽 · {fname}", _grade(ratio, 0.99, 0.90),
            f"{_pct(ok, total)} 条相邻 patch 首尾相接; 覆盖 {len(chains)} 只",
            "解析取到公告里的历次调整沿革最早一条时, 链会断 —— 曾断裂 80%",
            "patch"))
    return out


def check_patch_tail_matches_current(ctx: dict) -> Check:
    """末条 patch 应等于 cb_data 当前值 —— 仅对未走重大变化过滤的字段有意义。"""
    bundle, store, today = ctx["bundle"], ctx["patch_store"], ctx["today"]
    fname = "conversion_price"
    chains = _patches_by_field(store, fname)
    same = 0
    live_bad = []
    for code, seq in chains.items():
        terms = bundle.get(code)
        cur = getattr(terms, fname, None)
        if cur is None:
            continue
        tail = float(seq[-1].fields[fname])
        if abs(float(cur) - tail) <= max(1e-6, abs(float(cur)) * 1e-6):
            same += 1
        elif not _looks_delisted(terms, today):
            live_bad.append(f"{code} 当前={cur} 末patch={tail}")
    in_pool = [row for row in live_bad if row.split()[0] in ctx["pool"]]
    return Check(
        f"末条 patch == 当前值 · {fname}",
        FAIL if in_pool else (WARN if live_bad else OK),
        f"在市债不符 {len(live_bad)} 只, 其中主池 {len(in_pool)} 只 (已摘牌的不计, 其值本就冻结)",
        "曾 73.5% 不符 —— 解析出的历史 K 与 Wind 权威值系统性冲突",
        "patch",
        extra=live_bad[:6])


def _looks_delisted(terms: Any, on_date: date | None = None) -> bool:
    """真的已经摘牌了吗 —— 判据是**日期已过**, 不是"有没有这个字段"。

    曾写成 ``delisting_date is not None``。当时全库只有 17 只有摘牌日, 这么写没问题;
    2026-08-22 全库回填 (17 → 1041 只) 之后, 几乎每只在市债都带着一个**未来的**到期摘牌日,
    于是「末条 patch == 当前值」这条检查跳过 952/958 (99%) 条链、只真检查 6 只, 藏着
    30 只不符。回填一个字段把另一条检查悄悄变成恒真 —— 这正是体检本身要防的那类事故。
    """
    day = on_date or market_today()
    if "退市" in str(getattr(terms, "sec_name", "") or ""):
        return True
    delisting = getattr(terms, "delisting_date", None)
    if delisting is not None and delisting <= day:
        return True
    last_trading = getattr(terms, "last_trading_date", None)
    return last_trading is not None and last_trading < day


def check_patch_authority(ctx: dict) -> Check:
    """权威源应覆盖它能覆盖的字段; 解析源残留只能是兜底。"""
    store = ctx["patch_store"]
    rows = []
    worst = OK
    for fname in _AUTHORITATIVE_FIELDS:
        by_source = collections.Counter(
            p.source for p in store.list_patches() if fname in (p.fields or {}))
        total = sum(by_source.values())
        auth = by_source.get("wind_asof", 0)
        ratio = auth / total if total else 0.0
        if ratio < 0.5:
            worst = FAIL
        elif ratio < 0.9 and worst == OK:
            worst = WARN
        rows.append(f"{fname}: 权威 {_pct(auth, total)}")
    return Check(
        "权威源覆盖", worst, "; ".join(rows),
        "cb-sync-events 每次都会写入自己解析的转股价 patch; 权威源必须逐字段压住它们",
        "patch")


def check_frozen_value_signature(ctx: dict) -> Check:
    """同一只债多条 patch 值完全相同 = 解析器每次都抓到同一句话。"""
    store = ctx["patch_store"]
    suspects = []
    for fname in _AUTHORITATIVE_FIELDS:
        for code, seq in _patches_by_field(store, fname).items():
            parsed = [p for p in seq if p.source != "wind_asof"]
            if len(parsed) < 4:
                continue
            values = {round(float(p.fields[fname]), 6) for p in parsed}
            if len(values) == 1:
                suspects.append(f"{code} {fname} {len(parsed)} 条恒为 {values.pop()}")
    return Check(
        "冻结值签名", FAIL if suspects else OK,
        f"{len(suspects)} 只债的解析 patch 值恒定不变",
        "万孚转债 14 条 patch 跨两年恒为 93.57 —— 解析器每次都抓到公告开头的初始转股价",
        "patch", extra=suspects[:6])


_BOND_NAME_RE = re.compile(r"[一-龥A-Za-z0-9]{1,6}转(?:债|[0-9]{1,2})")


def check_cross_bond_patches(ctx: dict) -> Check:
    """标题点名了具体转债、却都不是本债 = 同发行人两只债串号。"""
    bundle, store = ctx["bundle"], ctx["patch_store"]
    hits = []
    for p in store.list_patches(include_shadowed=True):
        title = p.raw_title or ""
        if not title or p.source == "wind_asof":
            continue
        names = {n for n in _BOND_NAME_RE.findall(title) if not n.endswith("可转债")}
        if not names:
            continue
        me = str(getattr(bundle.get(p.bond_code), "sec_name", "") or "").replace("(退市)", "")
        if not me:
            continue
        if not any(n == me or me.endswith(n) or n.endswith(me) for n in names):
            hits.append(f"{p.bond_code}({me}) ← {title[:34]}")
    return Check(
        "标的串号", FAIL if hits else OK,
        f"{len(hits)} 条 patch 的公告点名了别的转债",
        "cninfo 按发行人返回公告, 嘉益转债曾被写进'精达转债'的转股价 3.26 (真实 79.66)",
        "patch", extra=hits[:6])


def check_statutory_line_clustering(ctx: dict) -> Check:
    """余额值扎堆在法定门槛上 = 把'少于3,000万元时'这句条款当成了当期余额。"""
    store = ctx["patch_store"]
    values = [round(float(p.fields["outstanding_balance"]), 6)
              for p in store.list_patches() if "outstanding_balance" in (p.fields or {})]
    if not values:
        return Check("门槛值扎堆", OK, "无余额 patch", "—", "patch")
    at_line = sum(1 for v in values if abs(v - _STATUTORY_BALANCE_LINE) < 1e-9)
    ratio = at_line / len(values)
    return Check(
        "门槛值扎堆", _grade(1 - ratio, 0.98, 0.90),
        f"值恰为 {_STATUTORY_BALANCE_LINE} 亿 (法定线) 的占 {_pct(at_line, len(values))}",
        "曾 528/546 条余额 patch 恰为 0.3 —— 全部来自赎回条款正文而非真实披露",
        "patch")


# ─────────────────────────── 交叉校验 ───────────────────────────

_RATING_ORDER = ("C", "CC", "CCC", "B-", "B", "B+", "BB-", "BB", "BB+", "BBB-", "BBB", "BBB+",
                 "A-", "A", "A+", "AA-", "AA", "AA+", "AAA")
_RATING_RANK = {grade: i for i, grade in enumerate(_RATING_ORDER)}


def check_rating_divergence(ctx: dict) -> Check:
    """公告解析出的评级与 cb_data 的分歧率 —— **方向随 cb_data 的来源而变, 别硬读**。

    这条检查的判据方向翻过两次, 两次都是因为"cb_data 的评级从哪来"变了:

    ① 最早按"末条 patch 必须等于 cb_data"判 patch 脏, 据此删了 18 条、又剥掉 330 条。
       方向是反的 —— 当时 cb_data 装的是 Wind ``creditrating`` 即**发行时冻结值**
       (17 个版本、约 4000 次逐债重取零变化, 同批 ``conversion_price`` 变了 287 次),
       公告 patch 才是较新的那个。
    ② ``cb-sync-ratings`` 落地后, cb_data 改由 akshare 第三方**当前值**驱动, 于是①的
       前提整个失效。实测拿第三方给这 17 条分歧逐条当裁判: **15 条是公告 patch 错、
       cb_data 对**, 只有 1 条是 cb_data 真落后 (科蓝转债, 且成因是同步侧的 sti 后缀
       被丢弃, 不是"没跑同步")。公告侧错得如此一边倒是有具体成因的 —— 
       ``_parse_bond_credit_rating`` 的 ``rating_re`` 左界回溯会把 AA 抠成 A、AA+ 抠成 A+,
       方向恒为**偏低**, 与真实下调的方向完全重合。

    所以这里**只报分歧率、不断言谁错**: 它现在是一条公告解析质量的粗指标。判"谁对"要靠
    与两边都独立的第三方 —— 即 ``--online`` 的「评级同步水位」。要按这条去改库之前,
    先跑 ``cb-sync-ratings`` 看第三方站哪边。
    """
    bundle, store = ctx["bundle"], ctx["patch_store"]
    chains = _patches_by_field(store, "credit_rating")
    lower, higher = [], []
    for code, seq in chains.items():
        current = str(getattr(bundle.get(code), "credit_rating", "") or "")
        tail = str(seq[-1].fields["credit_rating"])
        if not current or tail == current:
            continue
        row = f"{code} cb_data={current} 最新公告={tail} @{seq[-1].effective_date}"
        if _RATING_RANK.get(tail, 99) < _RATING_RANK.get(current, -1):
            lower.append(row)
        else:
            higher.append(row)
    total = len(chains)
    n = len(lower) + len(higher)
    return Check(
        "公告评级 vs cb_data 分歧", _grade(1 - n / total if total else 1.0, 0.80, 0.60),
        f"分歧 {_pct(n, total)} (公告更低 {len(lower)}, 公告更高 {len(higher)})",
        "只报分歧率, 不断言谁错: cb_data 现在走 akshare 第三方当前值, 公告侧的 rating_re "
        "左界 bug 会系统性把评级抠低 —— 实测 17 条分歧里 15 条是公告错。裁判是 --online "
        "的「评级同步水位」, 别照这条去改库",
        "交叉校验", extra=(lower + higher)[:6])


def check_pool_terms_projection(ctx: dict) -> Check:
    """今日估值下, 主池的 K 必须等于 cb_data 当前值。"""
    bundle, store, today = ctx["bundle"], ctx["patch_store"], ctx["today"]
    bad = []
    for code in ctx["pool"]:
        terms = bundle.get(code)
        cur = getattr(terms, "conversion_price", None)
        if cur is None:
            continue
        ts = bundle.fetched_at(code)
        got = getattr(store.apply(code, terms, today,
                                  after=ts.date() if ts else None), "conversion_price", None)
        if got is not None and abs(float(cur) - float(got)) > 1e-9:
            bad.append(f"{code} cb_data={cur} 投影后={got}")
    return Check(
        "主池今日 K 投影一致", FAIL if bad else OK,
        f"不一致 {_pct(len(bad), len(ctx['pool']))}",
        "今日估值曾套用两年前的 patch, 把万孚转债的 K 从 20.88 盖成 93.57, 主池 60% 中招",
        "交叉校验", extra=bad[:6])


def check_events_predate_listing(ctx: dict) -> Check:
    """一只债上市之前不可能发生它自己的摘牌/强赎/回售/转股价调整。

    同一发行人可以在同一个名字下先后发两只债, 标题守卫对**同名**债天然无解, 日期能。
    """
    from ..cb_events import _PRE_LISTING_ALLOWED

    bundle = ctx["bundle"]
    bad = []
    for event in ctx["events"]:
        if event.get("event_type") in _PRE_LISTING_ALLOWED:
            continue
        terms = bundle.get(event.get("bond_code") or "")
        listed = getattr(terms, "listing_date", None) if terms else None
        day = (event.get("event_date") or "")[:10]
        if listed and day and day < listed.isoformat():
            bad.append(f"{event['bond_code']} {day} {event.get('event_type')} "
                       f"«{(event.get('raw_title') or '')[:30]}» 上市={listed}")
    return Check(
        "事件不早于上市日", WARN if bad else OK,
        f"早于本债上市日的 {_pct(len(bad), len(ctx['events']))}",
        "110099.SH 福能转债 2025-10-30 上市, 却挂着上一只同名债 2024-11 的到期摘牌公告, "
        "被准入判成已退市 —— 而它当天成交 29.8 万手",
        "交叉校验", extra=bad[:6])


def check_events_match_current_parser(ctx: dict) -> Check:
    """存量事件类型必须等于当前分类器对同一标题的判定。

    分类器改了但存量不会被任何流程重新审视, 于是"修好了"和"数据是对的"是两回事。
    """
    from ..cb_events import classify_announcement_title

    stale = collections.Counter()
    n = 0
    for event in ctx["events"]:
        title = event.get("raw_title") or ""
        if not title:
            continue
        n += 1
        fresh = classify_announcement_title(title)
        if fresh != event.get("event_type"):
            stale[f"{event.get('event_type')} → {fresh}"] += 1
    total = sum(stale.values())
    return Check(
        "事件表与当前解析器自洽", _grade(1 - total / n if n else 1.0, 0.999, 0.99),
        f"与当前分类器不符 {_pct(total, n)}"
        + (f"; 最多: {', '.join(f'{k}×{v}' for k, v in stale.most_common(3))}" if stale else ""),
        "42 条「权益分派引起的转股价调整」曾被存成「下修已通过」, 会在 pricer 里生成"
        "一次性下修节点; 跑 cb-repair-events 重放当前解析器",
        "交叉校验")


# 断言"这只债现在不能交易"的剔除原因 —— 只有这些才会与"今日有成交"直接矛盾。
#
# 判据必须按**语义**分类, 不能只列前两条: 早期版本的 dead 集只有 {已退市, 已过最后交易日},
# 于是派克转债 / 中仑转债两只上市首日成交 2.57 亿 / 12.95 亿的新债 (剔除原因分别是
# "停牌/暂停交易" 与 "不可交易") 从这条检查底下整只漏过去, 检查还报 0。
#
# 反过来, 评级过低 / 正股 ST / 成交额过低 / 余额过小 / 定向转债 这些是**策略口径**的剔除:
# 它们从不声称这只债不能交易, 放进来只会让检查天天误报几十只。
_NOT_TRADING_REASONS = frozenset({
    "已退市", "已过最后交易日", "已到期", "暂停上市",
    "停牌/暂停交易", "不可交易", "已发行未上市", "违约/异常状态",
})


def _asserts_not_trading(reason: str) -> bool:
    if reason in _NOT_TRADING_REASONS:
        return True
    # "N 日后可交易" 是动态串, 同样断言"今天买不到"
    return bool(reason) and reason.endswith("日后可交易")


# 交易时段边界的假阳性: akshare 现货表在收盘后仍留着**上一交易日**的行情 (``ticktime``
# 只有时分秒、没有日期), 而 ``market_today()`` 按 Asia/Shanghai 走 —— 在美西运行时本机
# 上午就已经是上海的次日凌晨。于是"最后交易日恰好是上一交易日"的债会被判成"判死却仍在
# 成交", 而那笔成交正是它自己最后一个交易日的。实测春23转债 (最后交易日 2026-08-25 当天
# 成交 453 万手, 08-31 摘牌) 就是这么被误报的。
#
# 判据: 停止交易的日期离今天不足这么多天时, 那笔成交无法归属到"今天", 不算证据。原始事故
# 里的 19 只已经死了几个月, 加这道闸不影响检出能力。取 3 天覆盖周五收盘 → 周一开盘。
_RECENT_STOP_GRACE_DAYS = 3


def _stopped_recently(terms: Any, today: date) -> bool:
    """这只债的停止交易/摘牌日近到无法与"上一交易日的残留行情"区分。"""
    for name in ("last_trading_date", "delisting_date"):
        day = getattr(terms, name, None)
        if day is not None and (today - day).days <= _RECENT_STOP_GRACE_DAYS:
            return True
    return False


def check_dead_but_trading(ctx: dict) -> Check:
    """**最强的一条**: 被准入判死、但今日实际有成交。

    上面所有检查都在库内自洽性上打转, 只有这条把结论顶到外部现实。实测一次抓出 19 只
    在市活券被判死 (精测转2 431 元、胜蓝转02 326 元), 占主池 6.8%, 而库内所有指标都正常。
    """
    if not ctx.get("online"):
        return Check("判死但今日有成交", WARN, "需要 --online (查 akshare 实时行情)",
                     "库内自洽性检查看不见这一类错误", "外部对照")
    try:
        import akshare as ak
        quote = {}
        for _, row in ak.bond_zh_hs_cov_spot().iterrows():
            sym = str(row.get("symbol") or "")
            if len(sym) <= 2:
                continue
            code = sym[2:].upper() + ("." + ("SZ" if sym.startswith("sz") else "SH"))
            try:
                quote[code] = (float(row.get("trade") or 0), float(row.get("volume") or 0))
            except (TypeError, ValueError):
                continue
    except Exception as exc:
        return Check("判死但今日有成交", WARN, f"取行情失败: {exc}",
                     "外部对照不可用时不阻断体检", "外部对照")

    ghosts, boundary = [], 0
    for code, reason in ctx["excluded"].items():
        px, vol = quote.get(code, (0.0, 0.0))
        # 零成交的陈旧行不算 —— 判据是成交, 不是有没有报价
        if not (_asserts_not_trading(reason) and vol > 0):
            continue
        if _stopped_recently(ctx["bundle"].get(code), ctx["today"]):
            boundary += 1
            continue
        ghosts.append(f"{code} {getattr(ctx['bundle'].get(code), 'sec_name', '')} "
                      f"{px}元 量={vol:.0f} ({reason})")
    return Check(
        "判死但今日有成交", FAIL if ghosts else OK,
        f"{len(ghosts)} 只被判死的债今日仍在成交"
        + (f"; 另有 {boundary} 只刚停止交易, 行情无法与上一交易日残留区分" if boundary else ""),
        "曾一次抓出 19 只 (精测转2 431 元、胜蓝转02 326 元), 占主池 6.8%, "
        "而当时库内每一项自洽性指标都正常; 覆盖所有断言'不能交易'的剔除原因, "
        "只查已退市/已过最后交易日会漏掉上市首日被判成'不可交易'的新债",
        "外部对照", extra=ghosts[:10])


def check_rating_sync_drift(ctx: dict) -> Check:
    """cb_data 的评级与第三方**当前**值是否还对得上 —— 即距上次 ``cb-sync-ratings`` 漂了多少。

    注意这条**不是**在验证"谁对": cb_data 的 credit_rating 就是从 akshare 同步来的, 拿它跟
    akshare 比在同步当天恒为 100%, 那种比法没有信息量 (评级检查上一次就是这么失效的)。
    它量的是**同步水位**: 第三方调了评级而本地还没跑同步, 这里就会显出来。
    独立的对错判据在离线组的「cb_data 评级新鲜度」—— 那条比的是公告, 与第三方彼此独立。
    """
    if not ctx.get("online"):
        return Check("评级同步水位", WARN, "需要 --online",
                     "量距上次 cb-sync-ratings 的漂移, 不是验证谁对", "外部对照")
    try:
        from .sync_ratings import fetch_third_party_ratings
        third = fetch_third_party_ratings()
    except Exception as exc:
        return Check("评级同步水位", WARN, f"取第三方评级失败: {exc}",
                     "外部对照不可用时不阻断体检", "外部对照")

    bundle = ctx["bundle"]
    drift, n = [], 0
    for code in bundle.list_bonds():
        external = third.get(code.split(".")[0])
        current = str(getattr(bundle.get(code), "credit_rating", "") or "")
        if not external or current not in _RATING_RANK:
            continue
        n += 1
        if external != current:
            gap = _RATING_RANK[external] - _RATING_RANK[current]
            drift.append(f"{code} {getattr(bundle.get(code), 'sec_name', '')} "
                         f"本地={current} 第三方={external} ({gap:+d} 档)")
    return Check(
        "评级同步水位", _grade(1 - len(drift) / n if n else 1.0, 0.99, 0.95),
        f"与第三方当前值不符 {_pct(len(drift), n)}" + ("  → 跑 cb-sync-ratings" if drift else ""),
        "评级经 _rating_spread_floor 直接变成 pricer 的信用利差下限 (AA 2.50% ↔ C 80.00%), "
        "陈旧的高评级会系统性高估困境债的理论价",
        "外部对照", extra=drift[:8])


# ─────────────────────────── 批量结果不变量 ───────────────────────────

def check_batch_invariants(ctx: dict) -> list[Check]:
    rows = ctx.get("batch_rows") or []
    if not rows:
        return [Check("批量结果不变量", WARN, "没有批量缓存, 跳过",
                      "先跑一次批量定价再体检", "不变量")]
    bundle = ctx["bundle"]

    def f(x):
        try:
            v = float(x)
            return v if math.isfinite(v) else None
        except (TypeError, ValueError):
            return None

    bad_k = [r["bond_code"] for r in rows
             if (lambda t: t and getattr(t, "conversion_price", None) is not None
                 and f(r.get("K")) is not None
                 and abs(t.conversion_price - f(r.get("K"))) > 1e-9)(bundle.get(r["bond_code"]))]
    neg_uplift = [r["bond_code"] for r in rows if (f(r.get("down_reset_uplift")) or 0) < -1e-6]
    bad_parity = [r["bond_code"] for r in rows
                  if all(f(r.get(k)) for k in ("S0", "K", "parity"))
                  and abs(f(r["parity"]) - f(r["S0"]) / f(r["K"]) * 100) > 0.05]
    no_bucket = [r["bond_code"] for r in rows if not r.get("review_bucket")]
    return [
        Check("批量 K 与 cb_data 一致", FAIL if bad_k else OK,
              f"不一致 {_pct(len(bad_k), len(rows))}",
              "条款 patch 写坏 K 时, 转股价值与理论价会整体失真而不报错", "不变量",
              extra=bad_k[:6]),
        Check("下修价值非负", FAIL if neg_uplift else OK,
              f"负值 {_pct(len(neg_uplift), len(rows))}",
              "下修权只会增加价值; 负值意味着两个价跨了不同网格 —— 曾 55/282 中招", "不变量",
              extra=neg_uplift[:6]),
        Check("转股价值自洽", FAIL if bad_parity else OK,
              f"parity ≠ S0/K×100 的 {_pct(len(bad_parity), len(rows))}", "基本恒等式", "不变量"),
        Check("分桶全覆盖", FAIL if no_bucket else OK,
              f"缺分桶 {_pct(len(no_bucket), len(rows))}",
              "定价失败的行曾没有 review_bucket, GUI 分桶列空白而视图侧却包含它们", "不变量"),
    ]


CHECKS: list[Callable[[dict], Any]] = [
    check_terms_freshness,
    check_event_sync_watermark,
    check_field_coverage,
    check_event_time_coverage,
    check_patch_chain,
    check_patch_tail_matches_current,
    check_patch_authority,
    check_frozen_value_signature,
    check_cross_bond_patches,
    check_statutory_line_clustering,
    check_rating_divergence,
    check_pool_terms_projection,
    check_events_predate_listing,
    check_events_match_current_parser,
    check_dead_but_trading,
    check_rating_sync_drift,
    check_batch_invariants,
]


def run(only: str | None = None, *, online: bool = False) -> list[Check]:
    bundle = TermsBundle(project_bundle_path())
    events_path = project_events_path()
    events_payload: dict = {}
    if Path(events_path).exists():
        with open(events_path, "r", encoding="utf-8") as handle:
            events_payload = json.load(handle)
    batch_rows: list[dict] = []
    batch_path = data_path("batch_pricing_cache.json")
    if Path(batch_path).exists():
        with open(batch_path, "r", encoding="utf-8") as handle:
            batch_rows = (json.load(handle) or {}).get("results") or []
    _pool = screen_batch_pool_from_cache(bundle)
    ctx = {
        "bundle": bundle,
        "patch_store": TermsPatchStore(project_terms_patches_path()),
        "events": events_payload.get("events") or [],
        "events_meta": events_payload.get("_meta") or {},
        "pool": sorted(_pool["accepted"]),
        "excluded": dict(_pool["excluded"]),
        "online": online,
        "batch_rows": batch_rows,
        "today": market_today(),
    }
    results: list[Check] = []
    for fn in CHECKS:
        try:
            got = fn(ctx)
        except Exception as exc:                      # 单条检查失败不该拖垮整份体检
            results.append(Check(fn.__name__, FAIL, f"检查自身出错: {exc}",
                                 "体检脚本的健壮性", "内部"))
            continue
        results.extend(got if isinstance(got, list) else [got])
    if only:
        results = [c for c in results if only in c.group or only in c.name]
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CBLens 数据体检")
    parser.add_argument("--json", action="store_true", help="机器可读输出")
    parser.add_argument("--only", default=None, help="只跑名称/分组含该串的检查")
    parser.add_argument("--quiet", action="store_true", help="只打印 warn/fail")
    parser.add_argument("--online", action="store_true",
                        help="额外做外部对照 (查 akshare 实时行情, 抓判死但仍在成交的活券)")
    args = parser.parse_args(argv)

    checks = run(args.only, online=args.online)
    if args.json:
        print(json.dumps([{
            "name": c.name, "status": c.status, "group": c.group,
            "detail": c.detail, "because": c.because, "extra": [e for e in c.extra if e],
        } for c in checks], ensure_ascii=False, indent=2))
    else:
        group = None
        for c in checks:
            if args.quiet and c.status == OK:
                continue
            if c.group != group:
                group = c.group
                print(f"\n── {group} " + "─" * max(0, 56 - len(group)))
            print(f"{_ICON[c.status]} {c.name}: {c.detail}")
            for line in (e for e in c.extra if e):
                print(f"      {line}")
            if c.status != OK:
                print(f"      ↳ 这条检查的由来: {c.because}")
        tally = collections.Counter(c.status for c in checks)
        print(f"\n体检完成于 {datetime.now():%Y-%m-%d %H:%M}: "
              f"通过 {tally[OK]} / 警告 {tally[WARN]} / 失败 {tally[FAIL]}")
    return 1 if any(c.status == FAIL for c in checks) else 0


if __name__ == "__main__":
    sys.exit(main())
