"""巨潮资讯网 (cninfo.com.cn) 公告抓取层.

通过 HTTP POST 请求巨潮的公开查询接口, 获取可转债公告列表 + PDF 下载.
不依赖 Wind / akshare, 是事件层"去 Wind 化"的关键拼图.

典型用法::

    from convertible_bond.cninfo_provider import CninfoAnnouncementProvider

    provider = CninfoAnnouncementProvider()
    rows = provider.list_bond_announcements("128009.SZ", date(2026, 1, 1), date(2026, 4, 28))
    for row in rows:
        print(row["title"], row["date"], row["url"])

    # 下载 PDF 并提取纯文本
    text = provider.download_announcement_text(rows[0]["url"])
"""
from __future__ import annotations

import io
import logging
import re
import time
from datetime import date, datetime
from typing import Any, Dict, List, Optional

import requests

from .data_providers import DataProvider, BondTerms, to_date, _retry

logger = logging.getLogger(__name__)

# ── 常量 ─────────────────────────────────────────────────

_QUERY_URL = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
_STATIC_BASE = "http://static.cninfo.com.cn/"
_SEARCH_URL = "http://www.cninfo.com.cn/new/information/topSearch/query"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Content-Type": "application/x-www-form-urlencoded",
    "Accept": "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": "http://www.cninfo.com.cn",
    "Referer": "http://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice",
}

# 可转债相关公告分类
_CB_CATEGORIES = (
    "",                        # 全部分类 (兜底)
    "category_cb_szsh",        # 可转债专项
)

# Wind code → plain 6-digit code
_CODE_RE = re.compile(r"^(\d{6})")


def _wind_code_to_plain(wind_code: str) -> str:
    """'128009.SZ' → '128009'."""
    return wind_code.split(".")[0] if "." in wind_code else wind_code


def _infer_column(wind_code: str) -> str:
    """推断交易所 column 参数: szse / sse."""
    plain = _wind_code_to_plain(wind_code)
    if plain.startswith("11"):
        return "sse"
    return "szse"


# ── CNINFO 公告查询 ──────────────────────────────────────

class CninfoAnnouncementProvider(DataProvider):
    """巨潮资讯网公告抓取 Provider.

    只实现 ``list_bond_announcements`` 和 PDF 下载, 不实现行情 / 条款接口.
    行情 / 条款继续走 akshare 或 Wind; 公告事件层完全由本 provider 承载.
    """

    name = "cninfo"

    def __init__(
        self,
        *,
        request_interval: float = 1.5,
        page_size: int = 30,
        max_pages: int = 10,
        timeout: int = 15,
    ):
        self._interval = request_interval
        self._page_size = page_size
        self._max_pages = max_pages
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers.update(_HEADERS)
        self._last_request_ts: float = 0.0
        # orgId 缓存: plain_code → orgId
        self._org_cache: Dict[str, str] = {}

    # ── DataProvider 必须实现的接口 (公告之外的都抛 NotImplementedError) ──

    def get_bond_terms(self, bond_code: str, valuation_date: date) -> BondTerms:
        raise NotImplementedError(
            "CninfoAnnouncementProvider 仅支持公告查询, "
            "条款请使用 AkshareDataProvider 或 WindDataProvider."
        )

    def get_stock_close(self, stock_code: str, on_date: date) -> float:
        raise NotImplementedError("请使用 AkshareDataProvider 或 WindDataProvider.")

    def get_stock_history(self, stock_code, start, end):
        raise NotImplementedError("请使用 AkshareDataProvider 或 WindDataProvider.")

    def get_bond_history(self, bond_code, start, end):
        raise NotImplementedError("请使用 AkshareDataProvider 或 WindDataProvider.")

    # ── 核心: 公告列表 ──

    def list_bond_announcements(
        self,
        bond_code: str,
        start: date,
        end: date,
    ) -> List[dict]:
        """从巨潮查询某可转债的公告列表.

        返回 ``[{"title": ..., "date": ..., "url": ..., "pdf_url": ...}, ...]``.
        ``url`` 是完整 PDF 下载地址, ``pdf_url`` 是同义别名.
        """
        plain_code = _wind_code_to_plain(bond_code)
        se_date = f"{start.strftime('%Y-%m-%d')}~{end.strftime('%Y-%m-%d')}"
        column = _infer_column(bond_code)

        # 准备 stock 参数: 先尝试带 orgId, 再退化为纯代码
        stock_param = self._resolve_stock_param(plain_code)

        all_rows: List[dict] = []
        seen_keys: set = set()

        for category in _CB_CATEGORIES:
            page_rows = self._query_pages(
                stock=stock_param,
                se_date=se_date,
                column=column,
                category=category,
            )
            for row in page_rows:
                key = (row.get("title", ""), row.get("date"))
                if key not in seen_keys:
                    seen_keys.add(key)
                    all_rows.append(row)

            # 第一个 category 有结果就不再尝试兜底
            if all_rows:
                break

        return all_rows

    # ── PDF 下载与文本提取 ──

    def download_pdf_bytes(self, pdf_url: str) -> Optional[bytes]:
        """下载公告 PDF, 返回原始字节."""
        self._throttle()
        try:
            resp = _retry(
                lambda: self._session.get(pdf_url, timeout=self._timeout),
                attempts=3,
                delay=2.0,
                label="cninfo_pdf_download",
            )
            if resp.status_code == 200 and len(resp.content) > 500:
                return resp.content
            logger.warning(
                "cninfo PDF 下载异常: status=%s, size=%d, url=%s",
                resp.status_code, len(resp.content), pdf_url,
            )
            return None
        except Exception as exc:
            logger.warning("cninfo PDF 下载失败: %s — %s", pdf_url, exc)
            return None

    def download_announcement_text(self, pdf_url: str) -> Optional[str]:
        """下载公告 PDF 并提取纯文本.

        依赖 ``pdfplumber`` (纯 Python, 不需要外部工具).
        若 pdfplumber 未安装, 会 log warning 并返回 None.
        """
        pdf_bytes = self.download_pdf_bytes(pdf_url)
        if not pdf_bytes:
            return None
        return extract_text_from_pdf_bytes(pdf_bytes)

    # ── 内部: 查询逻辑 ──

    def _throttle(self) -> None:
        """限速: 两次请求之间至少间隔 self._interval 秒."""
        now = time.time()
        elapsed = now - self._last_request_ts
        if elapsed < self._interval:
            time.sleep(self._interval - elapsed)
        self._last_request_ts = time.time()

    def _resolve_stock_param(self, plain_code: str) -> str:
        """构造 stock 查询参数.

        cninfo 查询参数格式: ``代码,orgId`` 或纯 ``代码``.
        带 orgId 精度更高, 但获取 orgId 需要一次额外请求.
        """
        if plain_code in self._org_cache:
            org_id = self._org_cache[plain_code]
            return f"{plain_code},{org_id}"

        # 尝试通过搜索接口获取 orgId
        org_id = self._fetch_org_id(plain_code)
        if org_id:
            self._org_cache[plain_code] = org_id
            return f"{plain_code},{org_id}"

        return plain_code

    def _fetch_org_id(self, plain_code: str) -> Optional[str]:
        """通过巨潮搜索接口获取 orgId."""
        self._throttle()
        try:
            resp = self._session.get(
                _SEARCH_URL,
                params={"keyWord": plain_code},
                timeout=self._timeout,
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            # 返回格式: [{"code": "128009", "orgId": "gfV100...", ...}, ...]
            if isinstance(data, list):
                for item in data:
                    if str(item.get("code", "")).strip() == plain_code:
                        return item.get("orgId")
            return None
        except Exception as exc:
            logger.debug("cninfo orgId 查询失败 (%s): %s", plain_code, exc)
            return None

    def _query_pages(
        self,
        stock: str,
        se_date: str,
        column: str,
        category: str,
    ) -> List[dict]:
        """分页查询公告列表."""
        all_rows: List[dict] = []

        for page_num in range(1, self._max_pages + 1):
            self._throttle()
            payload = {
                "stock": stock,
                "searchkey": "",
                "plate": "",
                "category": category,
                "trade": "",
                "column": column,
                "pageNum": str(page_num),
                "pageSize": str(self._page_size),
                "tabName": "fulltext",
                "seDate": se_date,
                "sortName": "",
                "sortType": "",
                "isHLtitle": "true",
            }
            try:
                resp = _retry(
                    lambda: self._session.post(
                        _QUERY_URL, data=payload, timeout=self._timeout,
                    ),
                    attempts=3,
                    delay=2.0,
                    label="cninfo_query",
                )
            except Exception as exc:
                logger.warning("cninfo 公告查询失败 (stock=%s, page=%d): %s",
                               stock, page_num, exc)
                break

            if resp.status_code != 200:
                logger.warning("cninfo 公告查询 HTTP %d (stock=%s)", resp.status_code, stock)
                break

            try:
                body = resp.json()
            except Exception:
                logger.warning("cninfo 公告查询返回非 JSON (stock=%s)", stock)
                break

            announcements = body.get("announcements") or []
            if not announcements:
                break

            for ann in announcements:
                row = _parse_announcement_item(ann)
                if row:
                    all_rows.append(row)

            # 判断是否有下一页
            total_ann = body.get("totalAnnouncement", 0)
            if page_num * self._page_size >= total_ann:
                break

        return all_rows


def _parse_announcement_item(ann: dict) -> Optional[dict]:
    """解析巨潮单条公告 JSON 为统一格式."""
    title = ann.get("announcementTitle") or ""
    # 去掉巨潮返回的 <em> 高亮标签
    title = re.sub(r"</?em>", "", title).strip()
    if not title:
        return None

    # 日期: announcementTime 是毫秒时间戳
    ts = ann.get("announcementTime")
    ann_date = None
    if ts is not None:
        try:
            ann_date = datetime.fromtimestamp(int(ts) / 1000).date()
        except (ValueError, OSError, OverflowError):
            pass
    # 退化: adjunctUrl 里可能带日期
    if ann_date is None:
        adj_url = ann.get("adjunctUrl") or ""
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})", adj_url)
        if m:
            try:
                ann_date = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                pass

    if ann_date is None:
        return None

    # PDF URL
    adj_url = ann.get("adjunctUrl") or ""
    if adj_url:
        pdf_url = _STATIC_BASE + adj_url
    else:
        pdf_url = None

    return {
        "title": title,
        "date": ann_date,
        "url": pdf_url,
        "pdf_url": pdf_url,
        "raw": ann,      # 保留原始数据便于调试
    }


# ── PDF 文本提取 ──────────────────────────────────────────

def extract_text_from_pdf_bytes(pdf_bytes: bytes) -> Optional[str]:
    """从 PDF 字节流提取纯文本.

    依赖 ``pdfplumber``; 未安装时回退到 ``PyPDF2``; 都没有则返回 None.
    """
    # 尝试 pdfplumber (推荐, 效果最好)
    try:
        import pdfplumber  # type: ignore[import-not-found]
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            pages_text = []
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    pages_text.append(text)
            if pages_text:
                return "\n".join(pages_text)
    except ImportError:
        logger.info("pdfplumber 未安装, 尝试 PyPDF2 兜底; 推荐: pip install pdfplumber")
    except Exception as exc:
        logger.warning("pdfplumber 提取失败: %s", exc)

    # 兜底: PyPDF2
    try:
        from PyPDF2 import PdfReader  # type: ignore[import-not-found]
        reader = PdfReader(io.BytesIO(pdf_bytes))
        pages_text = []
        for page in reader.pages:
            text = page.extract_text()
            if text:
                pages_text.append(text)
        if pages_text:
            return "\n".join(pages_text)
    except ImportError:
        logger.warning(
            "pdfplumber 和 PyPDF2 均未安装, 无法提取 PDF 文本. "
            "请运行: pip install pdfplumber"
        )
    except Exception as exc:
        logger.warning("PyPDF2 提取失败: %s", exc)

    return None
