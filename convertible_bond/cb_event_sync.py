"""公告事件同步与应用."""
from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from collections.abc import Iterable

from .cache import TermsBundle
from .cb_events import (
    CBEvent,
    CBEventStore,
    apply_events_to_terms,
    classify_announcement_title,
    parse_call_redemption_dates,
    parse_event_from_announcement,
)
from .data_providers import DataProvider, to_date
from .historical_terms import TermsPatch, TermsPatchStore
from .market_time import market_today

logger = logging.getLogger(__name__)

# 这些事件需要从 PDF 正文解析承诺期或条款影响.
_BODY_REQUIRED_TYPES = {
    "down_reset_rejected",
    "call_no_redemption",
    "down_reset_proposed",
    "down_reset_approved",
    "conversion_price_adjusted",
    "call_redemption",
    "putback",
    "rating_change",
    "balance_change",
    "conversion_suspension",
    "conversion_resume",
}


def _needs_body(title: str) -> bool:
    """预判标题是否属于需要下载 PDF 正文的事件类型."""
    clean = re.sub(r"\s+", "", str(title or ""))
    event_type = classify_announcement_title(clean)
    return event_type in _BODY_REQUIRED_TYPES


def _try_download_body(provider, pdf_url: str) -> str | None:
    """尝试从 provider 下载 PDF 并提取纯文本.

    优先使用 provider 自带的 ``download_announcement_text`` 方法
    (CninfoAnnouncementProvider 已实现); 若 provider 不支持, 则尝试
    通过通用 HTTP 下载 + pdfplumber 提取.
    """
    if pdf_url is None:
        return None

    # 方式 1: provider 自带方法
    downloader = getattr(provider, "download_announcement_text", None)
    if callable(downloader):
        try:
            return downloader(pdf_url)
        except Exception as exc:
            logger.debug("provider.download_announcement_text 失败: %s", exc)

    # 方式 2: 通用 HTTP 下载 + 本地提取
    try:
        from .cninfo_provider import extract_text_from_pdf_bytes
        import requests
        resp = requests.get(
            pdf_url,
            timeout=15,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            },
        )
        if resp.status_code == 200 and len(resp.content) > 500:
            return extract_text_from_pdf_bytes(resp.content)
    except ImportError:
        logger.debug("cninfo_provider 不可用, 跳过 PDF 正文提取")
    except Exception as exc:
        logger.debug("通用 PDF 下载失败 (%s): %s", pdf_url, exc)

    return None


def _bond_name_resolver(bond_names: dict[str, str] | None):
    """返回 code → 转债简称 的查询函数。

    显式传入优先; 否则best-effort 读项目 cb_data (读不到就返回 None, 退化成不做串号校验)。
    """
    if bond_names:
        return lambda code: bond_names.get(code)
    try:
        from .cache import project_bundle_path
        bundle = TermsBundle(project_bundle_path())
    except Exception:
        return lambda code: None

    def resolve(code: str) -> str | None:
        try:
            terms = bundle.get(code)
        except Exception:
            return None
        return getattr(terms, "sec_name", None) if terms is not None else None

    return resolve


def sync_cb_events(
    provider: DataProvider,
    bond_codes: Iterable[str],
    event_store: CBEventStore | None = None,
    *,
    term_patch_store: TermsPatchStore | None = None,
    start: date | None = None,
    end: date | None = None,
    lookback_days: int = 180,
    on_progress=None,
    download_pdf: bool = True,
    bond_names: dict[str, str] | None = None,
) -> dict:
    """从 provider 同步公告并解析为事件表.

    Parameters
    ----------
    download_pdf : bool
        是否对 "不下修/不强赎" 公告下载 PDF 并提取正文以解析承诺期.
        默认 True; 设 False 可跳过 PDF 下载 (仅解析标题).
    """
    store = event_store or CBEventStore()
    end_date = end or market_today()
    start_date = start or (end_date - timedelta(days=max(1, int(lookback_days))))
    codes = list(bond_codes)
    name_of = _bond_name_resolver(bond_names)
    parsed_events: list[CBEvent] = []
    parsed_patches: list[TermsPatch] = []
    failed: list[tuple[str, str]] = []
    scanned = 0
    pdf_downloaded = 0
    pdf_failed = 0

    for i, code in enumerate(codes):
        if on_progress:
            on_progress(i, len(codes), code)
        try:
            rows = provider.list_bond_announcements(code, start_date, end_date)
        except Exception as exc:
            failed.append((code, str(exc)))
            continue
        scanned += len(rows)
        for row in rows:
            title = row.get("title") or row.get("raw_title")
            raw_date = row.get("date") or row.get("event_date")
            event_date = to_date(raw_date) if raw_date else None
            if not title or event_date is None:
                continue

            # PDF body 注入: 对需要正文解析的事件类型尝试下载 PDF
            body = None
            pdf_url = row.get("pdf_url") or row.get("url")
            if download_pdf and pdf_url and _needs_body(title):
                body = _try_download_body(provider, pdf_url)
                if body:
                    pdf_downloaded += 1
                    logger.info(
                        "PDF 正文提取成功: %s %s (%d chars)",
                        code, title[:30], len(body),
                    )
                else:
                    pdf_failed += 1
                    logger.debug("PDF 正文提取失败: %s %s", code, pdf_url)

            # 串号守卫必须在**建事件之前**: 只挡 patch 是不够的。事件本身会经
            # apply_events_to_bundle 回写 last_trading_date/delisting_date/call_redemption_price,
            # 实测 cb-sync-events --apply 用兄弟债的摘牌公告把 15 只在市券的摘牌日从未来
            # 改成过去 (胜蓝转02 2031-08-28 → 2024-12-12), 准入随即把它们整批当成已退市 ——
            # 而这些券当天成交量都在十万手以上。
            if _title_names_other_bond(title, name_of(code)):
                logger.debug("跳过标的串号公告: %s ← %s", code, title)
                continue

            event = parse_event_from_announcement(
                code,
                str(title),
                event_date,
                source=provider.name,
                url=row.get("url"),
                body=body,
            )
            if event:
                parsed_events.append(event)
                patch = parse_terms_patch_from_announcement(
                    code,
                    str(title),
                    event_date,
                    event_type=event.event_type,
                    source=provider.name,
                    body=body,
                    url=row.get("url"),
                    bond_name=name_of(code),
                )
                if patch:
                    parsed_patches.append(patch)

    added = store.add_many(parsed_events)
    patches_added = 0
    if term_patch_store is not None and parsed_patches:
        patches_added = term_patch_store.add_many(parsed_patches)
    failed_codes = {code for code, _err in failed}
    synced_codes = [code for code in codes if code not in failed_codes]
    mark_synced = getattr(store, "mark_synced", None)
    if callable(mark_synced):
        mark_synced(synced_codes)
    return {
        "scanned_announcements": scanned,
        "parsed_events": parsed_events,
        "parsed_patches": parsed_patches,
        "added": added,
        "patches_added": patches_added,
        "failed": failed,
        "store_path": str(store.path),
        "pdf_downloaded": pdf_downloaded,
        "pdf_failed": pdf_failed,
    }


# 同一发行人可以有两只转债 (上声/上26、聚合/合顺、恒逸转债/恒逸转2、金诚/金25…),
# cninfo 按发行人返回公告, 于是另一只债的转股价调整公告会被归到当前查询的 code 上,
# 实测污染 ≥11 条 patch / 5 只债 (如 123250.SZ 嘉益转债 被写进"精达转债"的调整价 3.26,
# 把 K 从 79.66 改成 3.26, 转股价值算成 1035)。标题点名了具体转债就必须核对是不是本债。
# 前缀是贪婪的, "关于嘉益转债…" 会抽成 "关于嘉益转债", 所以用双向 endswith 兜住。
_BOND_NAME_IN_TITLE_RE = re.compile(r"[\u4e00-\u9fa5A-Za-z0-9]{1,6}转(?:债|[0-9]{1,2})")


def _title_names_other_bond(title: str | None, bond_name: str | None) -> bool:
    """标题点名了具体转债、且没有一个是本债 → 判为标的串号。"""
    if not title or not bond_name:
        return False
    names = {n for n in _BOND_NAME_IN_TITLE_RE.findall(str(title))
             if not n.endswith("可转债")}
    if not names:
        return False
    me = str(bond_name).replace("(退市)", "").strip()
    if not me:
        return False
    # 本债名字只要在标题里出现过就不算串号 —— 抽名字用的贪婪前缀会被标题里的重复词撑坏
    # (「关于宝莱转债转债回售的公告」抽出「关于宝莱转债转债」, 双向 endswith 全不成立),
    # 而"标题提到了本债"本身就是最直接的归属证据。标题同时点名两只债时也走这条放行。
    if me in str(title):
        return False
    return not any(n == me or me.endswith(n) or n.endswith(me) for n in names)


def parse_terms_patch_from_announcement(
    bond_code: str,
    title: str,
    event_date: date,
    *,
    event_type: str | None = None,
    source: str = "announcement",
    body: str | None = None,
    url: str | None = None,
    bond_name: str | None = None,
) -> TermsPatch | None:
    """从公告正文解析会改变 ``BondTerms`` 的字段.

    当前只自动生成高置信度 patch: 评级变化、转股价格调整/下修实施公告中的新 K、
    剩余规模、已公告强赎价格等。
    解析不到有效字段时返回 None, 留给 Wind 刷新或人工 patch。
    """
    event_type = event_type or classify_announcement_title(title)
    if _title_names_other_bond(title, bond_name):
        logger.debug("跳过标的串号公告: %s ← %s", bond_code, title)
        return None
    source_key = f"{bond_code}|{event_date.isoformat()}|{event_type}|{title.strip()}"
    if event_type == "rating_change":
        rating_terms = parse_credit_rating_terms(body or title, title=title)
        fields = {
            key: value for key, value in rating_terms.items()
            if value is not None
        }
        if not fields:
            return None
        return TermsPatch(
            bond_code=bond_code,
            effective_date=event_date,
            event_date=event_date,
            fields=fields,
            source=source,
            note=url,
            raw_title=title,
            confidence="parsed",
            source_event_key=source_key,
        )

    if event_type in {"down_reset_approved", "conversion_price_adjusted"}:
        parsed = parse_conversion_price_adjustment(body or title)
        if not parsed or parsed.get("new_price") is None:
            return None
        old_price = parsed.get("old_price")
        new_price = parsed["new_price"]
        effective_date = parsed.get("effective_date") or event_date
        note_parts = []
        if old_price is not None:
            note_parts.append(f"转股价 {old_price:g}->{new_price:g}")
        else:
            note_parts.append(f"转股价 ->{new_price:g}")
        if url:
            note_parts.append(url)
        return TermsPatch(
            bond_code=bond_code,
            effective_date=effective_date,
            event_date=event_date,
            fields={"conversion_price": float(new_price)},
            before_fields={"conversion_price": float(old_price)} if old_price is not None else None,
            source=source,
            note=" | ".join(note_parts),
            raw_title=title,
            confidence=str(parsed.get("confidence") or "parsed"),
            source_event_key=source_key,
        )

    text = body or title
    fields: dict[str, object] = {}
    note_parts: list[str] = []
    balance = parse_outstanding_balance_change(text)
    if balance is not None:
        fields["outstanding_balance"] = float(balance)
        note_parts.append(f"余额 {balance:g}亿")
    if event_type == "call_redemption":
        call_terms = parse_call_redemption_dates(text or "")
        call_price = call_terms.get("redemption_price")
        if call_price is not None:
            fields["call_redemption_price"] = float(call_price)
            note_parts.append(f"赎回价 {float(call_price):g}")
    if not fields:
        return None
    if url:
        note_parts.append(url)
    return TermsPatch(
        bond_code=bond_code,
        effective_date=event_date,
        event_date=event_date,
        fields=fields,
        source=source,
        note=" | ".join(note_parts),
        raw_title=title,
        confidence="parsed",
        source_event_key=source_key,
    )


def parse_conversion_price_adjustment(text: str | None) -> dict | None:
    """解析转股价格调整公告中的新旧转股价和生效日."""
    if not text:
        return None
    t = re.sub(r"\s+", "", str(text))
    old_price = None
    new_price = None

    # 转股价调整公告的惯例结构: 开头「特别提示」给一段**结构化摘要** (调整前/调整后各一个价),
    # 正文中段再按时间顺序铺开**历次调整沿革**, 结尾用"综上"给出本次结果。
    # 实测万孚转债 2026-05-25 那份: 叙述型 "由X调整为Y" 命中 11 次, 第一次是 2020 年的
    # 93.55→93.57, 最后一次才是本次的 21.10→20.88。旧实现按 pattern 顺序 + re.search 取
    # **首个**匹配, 于是 14 条 patch 跨两年恒为 93.57, 而真实 K 是 20.88。
    # 因此按可靠性排序, 并逐 pattern 指定取首个还是最后一个:
    #   结构化摘要只描述本次 → 取**首个** (它在文首, 后文的沿革不会命中这个句式);
    #   叙述型沿革按时间排   → 取**最后一个** (最新的那次)。
    pair_patterns = (
        (r"(?:调整前|修正前).{0,30}?转股价格(?:为|:|：)?([0-9]+(?:\.[0-9]+)?)元/股.{0,80}?(?:调整后|修正后).{0,30}?转股价格(?:为|:|：)?([0-9]+(?:\.[0-9]+)?)元/股", "first"),
        (r"(?:原|当前)转股价格(?:为|:|：)?([0-9]+(?:\.[0-9]+)?)元/股.{0,80}?(?:调整后|修正后|本次调整后).{0,30}?转股价格(?:为|:|：)?([0-9]+(?:\.[0-9]+)?)元/股", "first"),
        (r"转股价格.{0,30}?由([0-9]+(?:\.[0-9]+)?)元/股.{0,30}?(?:调整|修正)(?:为|至)([0-9]+(?:\.[0-9]+)?)元/股", "last"),
    )
    for pattern, pick in pair_patterns:
        matches = list(re.finditer(pattern, t))
        if matches:
            match = matches[0] if pick == "first" else matches[-1]
            old_price = _safe_float(match.group(1))
            new_price = _safe_float(match.group(2))
            break

    if new_price is None:
        # 单价兜底同理: "调整后转股价格为X" 是摘要句式取首个; 光杆
        # "转股价格调整为X" 会命中沿革里的每一次, 取最后一个。
        single_patterns = (
            (r"(?:调整后|修正后|本次调整后|本次修正后).{0,35}?转股价格(?:为|:|：)?([0-9]+(?:\.[0-9]+)?)元/股", "first"),
            (r"转股价格(?:调整|修正)(?:为|至)([0-9]+(?:\.[0-9]+)?)元/股", "last"),
        )
        for pattern, pick in single_patterns:
            matches = list(re.finditer(pattern, t))
            if matches:
                match = matches[0] if pick == "first" else matches[-1]
                new_price = _safe_float(match.group(1))
                break

    if new_price is None:
        return None

    effective_date = _parse_effective_date(t)
    return {
        "old_price": old_price,
        "new_price": new_price,
        "effective_date": effective_date,
        "confidence": "parsed" if effective_date else "parsed_no_effective_date",
    }


# 跟踪评级报告的末尾**附录**会成段解释评级符号的含义, 里面把"列入正面观察名单"、
# "评级展望通常分为正面、负面、稳定、发展中"这些词逐个列一遍。那是**术语词表**, 不是这家
# 发行人的状态 —— 与 ``parse_outstanding_balance_change`` 踩过的"赎回门槛条款被当成当期
# 余额"是同一类陷阱: 关键词命中了, 主语却不是本债。
#
# 实测: 主池 51 只带"评级观察"的债里 47 只的值来自这段附录 (皓元/国检/花园/鸿路四份
# 2026 跟踪评级报告的原句一字不差: "列入评级观察是对于已对受评主体给出了评级结果…评级
# 观察分为'列入正面观察名单'…"), 真正来自专项公告的只有 4 只。附录通常在正文最后 3%
# (鸿路那份 21342 字, 附录起于第 20662 字)。
_RATING_LEGEND_ANCHORS = (
    "设置及含义", "符号及含义", "符号及定义", "等级符号及定义",
    "评级结果释义", "信用等级释义",
    "列入评级观察是", "评级展望是对",
)
# 附录之前至少要留下这么多正文, 否则说明锚点命中的不是附录标题 (例如一份专讲评级符号的
# 短文档), 此时宁可不截。
_MIN_BODY_BEFORE_LEGEND = 200


def _strip_rating_legend(text: str) -> str:
    """截掉评级报告末尾的"评级符号设置及含义"附录, 只把正文交给解析器。"""
    cut = len(text)
    for anchor in _RATING_LEGEND_ANCHORS:
        idx = text.find(anchor)
        if 0 <= idx < cut:
            cut = idx
    if cut >= len(text) or cut < _MIN_BODY_BEFORE_LEGEND:
        return text
    return text[:cut]


def parse_credit_rating_terms(text: str | None, *, title: str = "") -> dict[str, str | None]:
    """解析债项评级、评级展望和评级观察状态.

    评级字段只接受明确锚定到债项/可转债的等级; 展望和观察状态可从同一份
    评级公告中单独解析, 即使没有识别到债项评级也可以返回。
    """
    empty = {
        "credit_rating": None,
        "credit_rating_outlook": None,
        "credit_watch_status": None,
    }
    if not text:
        return empty
    if re.search(r"变更.{0,12}评级机构|终止评级", title):
        return empty
    t = _strip_rating_legend(re.sub(r"\s+", "", str(text).upper()))
    return {
        "credit_rating": _parse_bond_credit_rating(t),
        "credit_rating_outlook": _parse_credit_rating_outlook(t),
        "credit_watch_status": _parse_credit_watch_status(t),
    }


def parse_credit_rating_change(text: str | None, *, title: str = "") -> str | None:
    """解析债项/可转债信用等级.

    只接受明确锚定到债项或可转债的等级, 不用主体评级兜底, 以免误改债项评级。
    """
    return parse_credit_rating_terms(text, title=title).get("credit_rating")


_BALANCE_AMOUNT_RE = r"(?P<amount>[0-9]+(?:\.[0-9]+)?)"
_BALANCE_UNIT_RE = r"(?P<unit>亿元|万元|元)"
_BALANCE_LABEL_RE = (
    r"(?:未转股余额|未转股(?:可转债|债券|可转换公司债券)?余额|"
    r"剩余(?:可转债|债券|可转换公司债券)?余额|"
    r"未偿还(?:的)?(?:可转债|债券|可转换公司债券)?余额|"
    r"可转债余额|债券余额)"
)

# 紧凑式 (余额为X) 优先于宽松式 (余额…X), 让同一段里的真实披露压过条款引用。
_BALANCE_PATTERNS = tuple(re.compile(
    _BALANCE_LABEL_RE + "(?P<gap>" + gap + ")" + _BALANCE_AMOUNT_RE + _BALANCE_UNIT_RE
) for gap in (
    r"(?:为|为人民币|是|:|：)?(?:人民币)?",
    r".{0,24}?(?:人民币)?",
))

# 赎回/回售/摘牌条款会成段引用"未转股余额少于3,000万元时公司有权赎回"这类**门槛条款**。
# 它是条款文字而非当期余额, 历史上被整段当成真实余额落库 (528/546 条余额 patch 值恰为
# 0.3 亿 = 3,000 万元, 覆盖 103 只债, 其中 96 只真实余额 ≥0.5 亿)。
# 判据是措辞而不是数值: 真实披露"未转股余额为3,000万元"仍应正常解析出 0.3。
_BALANCE_THRESHOLD_GAP_RE = re.compile(
    r"少于|低于|不足|小于|未达|达不到|不到|未满|超过|高于|大于|多于"
)
# 门槛也可以写在金额之后 ("未转股余额3,000万元以下时"), 此时 gap 为空, 只能靠尾缀识别。
_BALANCE_THRESHOLD_TAIL_RE = re.compile(r"(?:以下|以上)|时(?:,|，|;|；|公司|发行人|本公司)")

# 全角千分位: 只吃掉夹在数字之间的那种, 句读用的"，"必须保留 —— 它是 gap 窗口的天然边界。
_FULLWIDTH_THOUSANDS_RE = re.compile(r"(?<=[0-9])，(?=[0-9])")

# 一份公告可能同时覆盖同一发行人的两只转债 (如"关于晶瑞转债、晶瑞转2…的公告")。
# 函数签名里没有 bond_code, 无从判断该取哪个余额, 因此宁可不解析也不要串号。
# 注意前缀是贪婪的, 同一只债在不同上下文里可能被抽成不同字符串; 这只会让守卫更保守
# (多判成歧义 → 不解析), 不会产生错值。但通用词"可转债"本身会命中, 必须排除。
_BOND_NAME_RE = re.compile(r"[\u4e00-\u9fa5A-Za-z0-9]{1,6}转(?:债|[2-9])")
_GENERIC_BOND_WORDS = ("可转债",)


def _bond_names(text: str) -> set[str]:
    return {n for n in _BOND_NAME_RE.findall(text)
            if not n.endswith(_GENERIC_BOND_WORDS)}

# 必须用**除法**而不是乘 1e-4/1e-8: 两者在约 31% 的万元取值上位级不等, 而 patch 去重键
# (TermsPatch.key) 含字段值 —— 换算方式一变, 重新同步同一条公告就会生成"新" patch 而非命中去重。
_BALANCE_UNIT_DIVISOR = {"亿元": 1.0, "万元": 10000.0, "元": 100000000.0}


def parse_outstanding_balance_change(text: str | None) -> float | None:
    """解析公告中的剩余转债余额, 统一返回亿元口径.

    匹配到的金额要再过两道语义闸, 避免把赎回条款的门槛表述当成余额:
    label 与金额之间不得出现比较词 (少于/低于/不足…), 金额之后不得紧跟门槛尾缀
    (…以下 / …时,公司有权…)。被拒的匹配不终止扫描, 同段后文的真实披露仍可命中。

    同一档 pattern 命中多个值时取**第一个** (与历史实现一致)。不要改成取最后一个:
    提前赎回公告惯用"截至本公告日余额为 X 亿元。本次赎回完成后余额为 0 元", 取最后
    会把**未来态**当成当期余额, 而 0 余额在准入里是强杀值 —— 那正是本次要消灭的错值形态。
    若正文同时出现多个转债简称且余额值互不相同, 则判为标的歧义直接返回 None,
    避免把 A 债余额写进 B 债。
    """
    if not text:
        return None
    t = _FULLWIDTH_THOUSANDS_RE.sub("", re.sub(r"\s+", "", str(text).replace(",", "")))
    multi_bond = len(_bond_names(t)) > 1
    for pattern in _BALANCE_PATTERNS:
        values: list[float] = []
        for match in pattern.finditer(t):
            if _BALANCE_THRESHOLD_GAP_RE.search(match.group("gap")):
                continue
            if _BALANCE_THRESHOLD_TAIL_RE.match(t, match.end()):
                continue
            value = _safe_float(match.group("amount"))
            if value is None:
                continue
            values.append(value / _BALANCE_UNIT_DIVISOR[match.group("unit")])
        if not values:
            continue
        if multi_bond and len({round(v, 8) for v in values}) > 1:
            return None
        return values[0]
    return None


def _parse_bond_credit_rating(t: str) -> str | None:
    # 左界必须不是评级字母: 否则 ``.{0,10}`` 回溯时会让评级"尽量晚开始", 从 AA- 里抠出 A-。
    # 实测 46/165 只债的末条评级 patch 与 cb_data 当前值不符, 其中大多是被削掉首字母的次级等级
    # (AA+→A+, AA→A, AA-→A-), 而低评级会让这些债在回测准入里被整批误杀。
    rating_re = (r"(?<![A-C])"
                 r"(AAA|AA\+|AA-|AA|A\+|A-|A|BBB\+|BBB-|BBB|BB\+|BB-|BB|B\+|B-|B|CCC|CC|C)")
    bond_rating_label = (
        r"(?:债项信用等级|本期债券信用等级|可转债信用等级|转债信用等级|债券信用等级|"
        # ``的`` 是可选助词: 「"鸿路转债"**的**信用等级为AA」是真实措辞, 少了它整条解析不出来。
        # 漏解析在存量回洗里等于删数据 (重放不出来 → 该字段被判成无源), 所以宁可把这类
        # 无歧义的助词补全, 也不要靠"解析不出就丢掉"来兜。
        r"[“\"'《]?[^，。；：]{0,20}转债[”\"'》]?的?(?:债项)?信用等级)"
    )
    patterns = (
        bond_rating_label + r"(?:为|维持为|调整为)" + rating_re,
        bond_rating_label + r"由" + rating_re + r"(?:下调至|调降至|调整至|下调为|调降为|调整为)" + rating_re,
        r"(?:维持|确认).{0,20}?" + bond_rating_label + r".{0,10}?" + rating_re,
    )
    for pattern in patterns:
        match = re.search(pattern, t)
        if match:
            for group in reversed(match.groups()):
                if group:
                    return group
    return None


def _parse_credit_rating_outlook(t: str) -> str | None:
    outlook_re = r"(稳定|负面|正面|发展中)"
    pair = re.search(
        r"评级展望由" + outlook_re + r"(?:调整|调降|下调|上调|变更)(?:为|至)" + outlook_re,
        t,
    )
    if pair:
        return pair.group(2)
    patterns = (
        r"评级展望(?:为|维持为|调整为|调降为|下调为|上调为|维持|:|：)?" + outlook_re,
        r"展望(?:为|维持为|调整为|:|：)" + outlook_re,
    )
    for pattern in patterns:
        match = re.search(pattern, t)
        if match:
            return match.group(1)
    return None


_RE_WATCH_LISTED = re.compile(
    r"(?:继续)?(?:列入|纳入)[^。；;]{0,12}?(?:信用)?(?:评级)?观察名单")
_RE_WATCH_REMOVED = re.compile(
    r"(?:撤出|移出|调出|取消)[^。；;]{0,16}?(?:信用)?(?:评级)?观察名单")
# 词表句式: "列入评级观察**是**对于…" (系词) 与 "评级观察**分为**「列入正面观察名单」" (枚举)。
# 这是 _strip_rating_legend 之外的第二道网 —— 有些机构把释义混排进正文而不是放附录。
_RE_WATCH_DEFINITION = re.compile(
    r"评级观察分为|列入评级观察是|观察名单[是指]|分为[^。；;]{0,10}观察名单")
_WATCH_DEFINITION_WINDOW = 60


def _watch_match_is_definition(t: str, match: re.Match) -> bool:
    lo = max(0, match.start() - _WATCH_DEFINITION_WINDOW)
    return bool(_RE_WATCH_DEFINITION.search(t[lo: match.end() + _WATCH_DEFINITION_WINDOW]))


def _parse_credit_watch_status(t: str) -> str | None:
    """评级观察状态。命中关键词还不够 —— 必须确认这句话在**陈述一次评级行动**。

    跟踪评级报告末尾成段解释评级符号含义, 里面把"列入正面观察名单"逐个列一遍。实测主池
    51 只带观察状态的债里 47 只的值来自那段词表 (见 _strip_rating_legend 的注释)。
    """
    removed = _RE_WATCH_REMOVED.search(t)
    if removed and not _watch_match_is_definition(t, removed):
        return "撤出观察名单"
    listed = _RE_WATCH_LISTED.search(t)
    if listed and not _watch_match_is_definition(t, listed):
        return "列入观察名单"
    # "关注公告"是评级机构就某一具体事项 (业绩预亏/监管函/诉讼) 出的专项公告, 判据落在
    # 标题式措辞上; 光有"评级关注"四个字散落在正文里不算。
    if re.search(r"评级关注公告|的关注公告|关于[^。；;]{0,40}的关注", t):
        return "评级关注"
    return None


def _parse_effective_date(text: str) -> date | None:
    date_re = r"(\d{4})年(\d{1,2})月(\d{1,2})日"
    patterns = (
        r"(?:生效日期|调整生效日期|修正生效日期)(?:为|:|：)?.{0,20}?" + date_re,
        r"自.{0,12}?" + date_re + r"起生效",
        date_re + r"起生效",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            parsed = _safe_date(*match.groups()[-3:])
            if parsed:
                return parsed
    return None


def _safe_float(value: str | None) -> float | None:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _safe_date(y, m, d) -> date | None:
    try:
        return date(int(y), int(m), int(d))
    except (TypeError, ValueError):
        return None


def apply_events_to_bundle(
    event_store: CBEventStore,
    bundle: TermsBundle,
    *,
    valuation_date: date | None = None,
    on_progress=None,
) -> dict:
    """把事件表应用回 cb_data bundle 的状态字段."""
    val_date = valuation_date or market_today()
    changed: list[tuple[str, list[str]]] = []
    items = []
    codes = bundle.list_bonds()
    for i, code in enumerate(codes):
        if on_progress:
            on_progress(i, len(codes), code)
        terms = bundle.get(code)
        if terms is None:
            continue
        events = event_store.list_events(bond_code=code, through_date=val_date)
        patched = apply_events_to_terms(code, terms, events, valuation_date=val_date)
        fields = _changed_fields(terms, patched)
        if fields:
            changed.append((code, fields))
            items.append((code, patched))
    if items:
        bundle.set_many(items, source="cb_events")
    return {
        "changed": changed,
        "updated": len(items),
        "bundle_path": str(bundle.path),
    }


def _changed_fields(before, after) -> list[str]:
    fields = (
        "call_status",
        "call_announce_date",
        "call_redemption_date",
        "call_redemption_price",
        "call_no_redemption_until",
        "last_trading_date",
        "putback_start_date",
        "putback_end_date",
        "putback_price",
        "conversion_suspension_start_date",
        "conversion_suspension_end_date",
        "conversion_suspension_status",
        "delisting_date",
        "suspension_status",
        "underlying_status",
        "underlying_trade_status",
        "down_reset_block_until",
        "down_reset_note",
    )
    return [name for name in fields if getattr(before, name, None) != getattr(after, name, None)]
