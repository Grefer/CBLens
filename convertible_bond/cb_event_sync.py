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
) -> dict:
    """从 provider 同步公告并解析为事件表.

    Parameters
    ----------
    download_pdf : bool
        是否对 "不下修/不强赎" 公告下载 PDF 并提取正文以解析承诺期.
        默认 True; 设 False 可跳过 PDF 下载 (仅解析标题).
    """
    store = event_store or CBEventStore()
    end_date = end or date.today()
    start_date = start or (end_date - timedelta(days=max(1, int(lookback_days))))
    codes = list(bond_codes)
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


def parse_terms_patch_from_announcement(
    bond_code: str,
    title: str,
    event_date: date,
    *,
    event_type: str | None = None,
    source: str = "announcement",
    body: str | None = None,
    url: str | None = None,
) -> TermsPatch | None:
    """从公告正文解析会改变 ``BondTerms`` 的字段.

    当前只自动生成高置信度 patch: 评级变化、转股价格调整/下修实施公告中的新 K、
    剩余规模、已公告强赎价格等。
    解析不到有效字段时返回 None, 留给 Wind 刷新或人工 patch。
    """
    event_type = event_type or classify_announcement_title(title)
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

    pair_patterns = (
        r"转股价格.{0,30}?由([0-9]+(?:\.[0-9]+)?)元/股.{0,30}?(?:调整|修正)(?:为|至)([0-9]+(?:\.[0-9]+)?)元/股",
        r"(?:调整前|修正前).{0,30}?转股价格(?:为|:|：)?([0-9]+(?:\.[0-9]+)?)元/股.{0,80}?(?:调整后|修正后).{0,30}?转股价格(?:为|:|：)?([0-9]+(?:\.[0-9]+)?)元/股",
        r"(?:原|当前)转股价格(?:为|:|：)?([0-9]+(?:\.[0-9]+)?)元/股.{0,80}?(?:调整后|修正后|本次调整后).{0,30}?转股价格(?:为|:|：)?([0-9]+(?:\.[0-9]+)?)元/股",
    )
    for pattern in pair_patterns:
        match = re.search(pattern, t)
        if match:
            old_price = _safe_float(match.group(1))
            new_price = _safe_float(match.group(2))
            break

    if new_price is None:
        single_patterns = (
            r"(?:调整后|修正后|本次调整后|本次修正后).{0,35}?转股价格(?:为|:|：)?([0-9]+(?:\.[0-9]+)?)元/股",
            r"转股价格(?:调整|修正)(?:为|至)([0-9]+(?:\.[0-9]+)?)元/股",
        )
        for pattern in single_patterns:
            match = re.search(pattern, t)
            if match:
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
    t = re.sub(r"\s+", "", str(text).upper())
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


def parse_outstanding_balance_change(text: str | None) -> float | None:
    """解析公告中的剩余转债余额, 统一返回亿元口径."""
    if not text:
        return None
    t = re.sub(r"\s+", "", str(text).replace(",", ""))
    amount_re = r"([0-9]+(?:\.[0-9]+)?)"
    unit_re = r"(亿元|万元|元)"
    balance_label = (
        r"(?:未转股余额|未转股(?:可转债|债券|可转换公司债券)?余额|"
        r"剩余(?:可转债|债券|可转换公司债券)?余额|"
        r"未偿还(?:的)?(?:可转债|债券|可转换公司债券)?余额|"
        r"可转债余额|债券余额)"
    )
    patterns = (
        balance_label + r"(?:为|为人民币|是|:|：)?(?:人民币)?" + amount_re + unit_re,
        balance_label + r".{0,24}?(?:人民币)?" + amount_re + unit_re,
    )
    for pattern in patterns:
        for match in re.finditer(pattern, t):
            value = _safe_float(match.group(1))
            if value is None:
                continue
            unit = match.group(2)
            if unit == "亿元":
                return value
            if unit == "万元":
                return value / 10000.0
            if unit == "元":
                return value / 100000000.0
    return None


def _parse_bond_credit_rating(t: str) -> str | None:
    rating_re = r"(AAA|AA\+|AA-|AA|A\+|A-|A|BBB\+|BBB-|BBB|BB\+|BB-|BB|B\+|B-|B|CCC|CC|C)"
    bond_rating_label = (
        r"(?:债项信用等级|本期债券信用等级|可转债信用等级|转债信用等级|债券信用等级|"
        r"[“\"'《]?[^，。；：]{0,20}转债[”\"'》]?(?:债项)?信用等级)"
    )
    patterns = (
        bond_rating_label + r"(?:为|维持为|调整为)" + rating_re,
        bond_rating_label + r"由" + rating_re + r"(?:下调至|调降至|调整至|下调为|调降为|调整为)" + rating_re,
        r"(?:维持|确认).{0,20}" + bond_rating_label + r".{0,10}" + rating_re,
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


def _parse_credit_watch_status(t: str) -> str | None:
    if re.search(r"(?:撤出|移出|调出|取消).{0,12}(?:评级)?观察名单", t):
        return "撤出观察名单"
    if re.search(r"(?:继续)?(?:列入|纳入).{0,12}(?:评级)?观察名单", t):
        return "列入观察名单"
    if re.search(r"评级关注|关注公告", t):
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
    val_date = valuation_date or date.today()
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
