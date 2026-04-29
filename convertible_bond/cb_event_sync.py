"""公告事件同步与应用."""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Iterable, Optional

from .cache import TermsBundle
from .cb_events import (
    CBEvent,
    CBEventStore,
    apply_events_to_terms,
    classify_announcement_title,
    parse_event_from_announcement,
)
from .data_providers import DataProvider, to_date

logger = logging.getLogger(__name__)

# 只有这些事件类型需要从 PDF 正文解析承诺期
_BODY_REQUIRED_TYPES = {"down_reset_rejected", "call_no_redemption"}


def _needs_body(title: str) -> bool:
    """预判标题是否属于需要下载 PDF 正文的事件类型."""
    import re
    clean = re.sub(r"\s+", "", str(title or ""))
    event_type = classify_announcement_title(clean)
    return event_type in _BODY_REQUIRED_TYPES


def _try_download_body(provider, pdf_url: str) -> Optional[str]:
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
    event_store: Optional[CBEventStore] = None,
    *,
    start: Optional[date] = None,
    end: Optional[date] = None,
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

    added = store.add_many(parsed_events)
    return {
        "scanned_announcements": scanned,
        "parsed_events": parsed_events,
        "added": added,
        "failed": failed,
        "store_path": str(store.path),
        "pdf_downloaded": pdf_downloaded,
        "pdf_failed": pdf_failed,
    }


def apply_events_to_bundle(
    event_store: CBEventStore,
    bundle: TermsBundle,
    *,
    valuation_date: Optional[date] = None,
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
        "delisting_date",
        "suspension_status",
        "down_reset_block_until",
        "down_reset_note",
    )
    return [name for name in fields if getattr(before, name, None) != getattr(after, name, None)]
