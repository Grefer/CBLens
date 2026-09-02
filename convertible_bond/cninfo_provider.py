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
import json
import logging
import re
import time
from datetime import date, datetime

import requests

from .data_providers import DataProvider, BondTerms, _retry
from .market_time import EXCHANGE_TZ

logger = logging.getLogger(__name__)

# ── 常量 ─────────────────────────────────────────────────

_QUERY_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
_STATIC_BASE = "https://static.cninfo.com.cn/"
_SEARCH_URL = "https://www.cninfo.com.cn/new/information/topSearch/query"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Content-Type": "application/x-www-form-urlencoded",
    "Accept": "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": "https://www.cninfo.com.cn",
    "Referer": "https://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice",
}

# 可转债相关公告分类 (先精确后兜底; 配合 break-if-results 逻辑)
_CB_CATEGORIES = (
    "category_cb_szsh",        # 可转债专项 (优先)
    "",                        # 全部分类 (兜底)
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
        self._org_cache: dict[str, str] = {}

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
    ) -> list[dict]:
        """从巨潮查询某可转债的公告列表.

        返回 ``[{"title": ..., "date": ..., "url": ..., "pdf_url": ...}, ...]``.
        ``url`` 是完整 PDF 下载地址, ``pdf_url`` 是同义别名.
        """
        plain_code = _wind_code_to_plain(bond_code)
        se_date = f"{start.strftime('%Y-%m-%d')}~{end.strftime('%Y-%m-%d')}"
        column = _infer_column(bond_code)

        # 准备 stock 参数: 先尝试带 orgId, 再退化为纯代码
        stock_param = self._resolve_stock_param(plain_code)

        all_rows: list[dict] = []
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

        # 巨潮目前对可转债代码作为 stock 参数经常返回空; 用 searchkey 全库搜索兜底.
        # 该路径会返回发行人公告列表, 后续事件层再按标题筛出可转债事件.
        if not all_rows:
            logger.info("cninfo stock 查询无公告, 改用 searchkey 兜底: %s", plain_code)
            for category in _CB_CATEGORIES:
                page_rows = self._query_pages(
                    stock="",
                    se_date=se_date,
                    column=column,
                    category=category,
                    searchkey=plain_code,
                )
                for row in page_rows:
                    key = (row.get("title", ""), row.get("date"))
                    if key not in seen_keys:
                        seen_keys.add(key)
                        all_rows.append(row)

                if all_rows:
                    break

        return all_rows

    # ── PDF 下载与文本提取 ──

    def download_pdf_bytes(self, pdf_url: str) -> bytes | None:
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

    def download_announcement_text(self, pdf_url: str) -> str | None:
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

    def _fetch_org_id(self, plain_code: str) -> str | None:
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
        searchkey: str = "",
    ) -> list[dict]:
        """分页查询公告列表.

        **翻页没翻完时抛 IncompleteAnnouncementList**, 不再静默截断。三种停止方式此前
        都被当成"取完了": ① 第一页之后任何一页失败 (只在 ``all_rows`` 非空时 break);
        ② 响应缺 ``totalAnnouncement`` —— 默认 0 让 ``page*size >= 0`` 立刻为真, 一页就停;
        ③ ``max_pages`` × ``page_size`` 用尽而总数更多 (实测 600 条公告只取回 300, 零日志)。
        而上层 ``sync_cb_events`` 对这三种一视同仁: 不记 ``failed``、照常
        ``mark_synced``, **把水位推过它从没看见的公告** —— 那些公告之后永远不会再被拉取。
        """
        all_rows: list[dict] = []

        for page_num in range(1, self._max_pages + 1):
            self._throttle()
            payload = {
                "stock": stock,
                "searchkey": searchkey,
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
                msg = f"cninfo 公告查询失败 (stock={stock}, page={page_num}): {exc}"
                logger.warning(msg)
                if not all_rows:
                    raise RuntimeError(msg) from exc
                raise IncompleteAnnouncementList(msg, rows=all_rows) from exc

            if resp.status_code != 200:
                msg = f"cninfo 公告查询 HTTP {resp.status_code} (stock={stock})"
                logger.warning(msg)
                if not all_rows:
                    raise RuntimeError(msg)
                raise IncompleteAnnouncementList(msg, rows=all_rows)

            try:
                body = resp.json()
            except (ValueError, json.JSONDecodeError):
                msg = f"cninfo 公告查询返回非 JSON (stock={stock})"
                logger.warning(msg)
                if not all_rows:
                    raise RuntimeError(msg)
                raise IncompleteAnnouncementList(msg, rows=all_rows)

            announcements = body.get("announcements") or []
            if not announcements:
                break

            for ann in announcements:
                row = _parse_announcement_item(ann)
                if row:
                    all_rows.append(row)

            # 判断是否有下一页。``totalAnnouncement`` 缺失时**不能当成 0** ——
            # 那会让 ``page*size >= 0`` 立刻为真, 一页就停而且看上去像取完了。
            if "totalAnnouncement" not in body:
                raise IncompleteAnnouncementList(
                    f"cninfo 响应缺 totalAnnouncement, 无法判断是否翻完 (stock={stock})",
                    rows=all_rows)
            total_ann = int(body.get("totalAnnouncement") or 0)
            if page_num * self._page_size >= total_ann:
                return all_rows
        else:
            # for 正常跑完 = max_pages 用尽。总数还没取够就是截断了。
            if len(all_rows) < total_ann:
                raise IncompleteAnnouncementList(
                    f"cninfo 公告超过 max_pages={self._max_pages} 上限 "
                    f"(stock={stock}, 已取 {len(all_rows)}/{total_ann})",
                    rows=all_rows)

        return all_rows


class IncompleteAnnouncementList(RuntimeError):
    """公告列表**没取完**。

    与"一条都没取到"分开: 后者直接 ``RuntimeError``, 上层照常记进 ``failed``。这一档
    手里有部分数据, 但把它当完整结果会让同步水位推过没看见的公告 —— 那些公告之后
    永远不会再被拉取, 而这是**静默**的。``rows`` 带着已取到的部分, 供调用方按需降级。
    """

    def __init__(self, message: str, *, rows: list[dict] | None = None):
        super().__init__(message)
        self.rows = rows or []


def _parse_announcement_item(ann: dict) -> dict | None:
    """解析巨潮单条公告 JSON 为统一格式."""
    title = ann.get("announcementTitle") or ""
    # 去掉巨潮返回的 <em> 高亮标签
    title = re.sub(r"</?em>", "", title).strip()
    if not title:
        return None

    # 日期: announcementTime 是北京时间口径的毫秒时间戳 —— 必须按交易所时区换算,
    # 用本机时区解析的话非东八区机器 (例如美西 UTC-7) 会把公告整体记早一天。
    ts = ann.get("announcementTime")
    ann_date = None
    if ts is not None:
        try:
            ann_date = datetime.fromtimestamp(int(ts) / 1000, tz=EXCHANGE_TZ).date()
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

def extract_text_from_pdf_bytes(pdf_bytes: bytes) -> str | None:
    """从 PDF 字节流提取纯文本.

    依次尝试 ``pdfplumber`` → ``pypdf`` → ``PyPDF2``; 都取不到文本则返回 None.

    区分"没装库"和"装了但这份 PDF 提不出文本"很重要: 后者多半是扫描件/图片版公告,
    装再多库也没用, 而旧实现在这种情况下会打出"pdfplumber 和 PyPDF2 均未安装"的
    误导性提示 (实测 pdfplumber 装着且对多数公告工作正常, 只是对扫描件返回空)。
    """
    available: list[str] = []
    pages_text: list[str] = []

    def _extract(reader_pages) -> list[str]:
        out = []
        for page in reader_pages:
            text = page.extract_text()
            if text:
                out.append(text)
        return out

    try:
        import pdfplumber  # type: ignore[import-not-found]
        available.append("pdfplumber")
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            pages_text = _extract(pdf.pages)
            if pages_text:
                return "\n".join(pages_text)
    except ImportError:
        pass
    except Exception as exc:
        logger.warning("pdfplumber 提取失败: %s", exc)

    for module_name in ("pypdf", "PyPDF2"):
        try:
            reader_cls = __import__(module_name, fromlist=["PdfReader"]).PdfReader
        except ImportError:
            continue
        available.append(module_name)
        try:
            pages_text = _extract(reader_cls(io.BytesIO(pdf_bytes)).pages)
            if pages_text:
                return "\n".join(pages_text)
        except Exception as exc:
            logger.warning("%s 提取失败: %s", module_name, exc)

    if not available:
        logger.warning("未安装任何 PDF 解析库, 无法提取正文. 请运行: pip install pdfplumber")
    else:
        logger.info("PDF 无可提取文本 (可能是扫描件/图片版), 已试: %s", ", ".join(available))
    return None
