"""CninfoAnnouncementProvider 与 cb_event_sync PDF 注入测试."""
from datetime import date
from unittest.mock import MagicMock, patch

from convertible_bond.cb_event_sync import (
    _needs_body,
    _try_download_body,
    sync_cb_events,
)
from convertible_bond.cb_events import CBEventStore, classify_announcement_title
from convertible_bond.cninfo_provider import (
    CninfoAnnouncementProvider,
    _parse_announcement_item,
    _wind_code_to_plain,
    _infer_column,
    extract_text_from_pdf_bytes,
)


# ── 工具函数测试 ──

def test_wind_code_to_plain():
    assert _wind_code_to_plain("128009.SZ") == "128009"
    assert _wind_code_to_plain("113050.SH") == "113050"
    assert _wind_code_to_plain("128009") == "128009"


def test_infer_column():
    assert _infer_column("113050.SH") == "sse"
    assert _infer_column("128009.SZ") == "szse"
    assert _infer_column("127045.SZ") == "szse"


# ── 公告解析测试 ──

def test_parse_announcement_item_basic():
    ann = {
        "announcementTitle": "关于<em>不提前</em>赎回可转债的公告",
        "announcementTime": 1714185600000,  # 2024-04-27 in ms
        "adjunctUrl": "finalpage/2024-04-27/test.PDF",
    }
    row = _parse_announcement_item(ann)
    assert row is not None
    assert row["title"] == "关于不提前赎回可转债的公告"
    assert row["date"] == date(2024, 4, 27)
    assert "static.cninfo.com.cn" in row["url"]
    assert row["url"].endswith(".PDF")


def test_parse_announcement_item_missing_title():
    ann = {"announcementTitle": "", "announcementTime": 1714185600000}
    assert _parse_announcement_item(ann) is None


def test_parse_announcement_item_missing_date():
    ann = {"announcementTitle": "测试公告", "announcementTime": None}
    # adjunctUrl 里也没有日期 → None
    assert _parse_announcement_item(ann) is None


def test_parse_announcement_item_date_from_adjunct_url():
    ann = {
        "announcementTitle": "测试公告",
        "announcementTime": None,
        "adjunctUrl": "finalpage/2025-03-15/abc.PDF",
    }
    row = _parse_announcement_item(ann)
    assert row is not None
    assert row["date"] == date(2025, 3, 15)


# ── _needs_body 测试 ──

def test_needs_body_for_down_reset_rejected():
    assert _needs_body("\u5173\u4e8e\u4e0d\u5411\u4e0b\u4fee\u6b63\u201c\u6d4b\u8bd5\u8f6c\u503a\u201d\u8f6c\u80a1\u4ef7\u683c\u7684\u516c\u544a") is True


def test_needs_body_for_call_no_redemption():
    assert _needs_body("\u5173\u4e8e\u4e0d\u63d0\u524d\u8d4e\u56de\u201c\u6d4b\u8bd5\u8f6c\u503a\u201d\u7684\u516c\u544a") is True


def test_needs_body_for_call_redemption():
    assert _needs_body("关于提前赎回并摘牌的公告") is False


def test_needs_body_for_unknown_title():
    assert _needs_body("公司季度报告") is False


# ── sync_cb_events PDF 注入测试 ──

def test_sync_with_pdf_download(tmp_path):
    """模拟 PDF 下载注入 body."""
    fake_body = (
        "\u516c\u53f8\u8463\u4e8b\u4f1a\u51b3\u5b9a\u672c\u6b21\u4e0d\u5411\u4e0b\u4fee\u6b63\u201c\u6d4b\u8bd5\u8f6c\u503a\u201d\u8f6c\u80a1\u4ef7\u683c\uff0c"
        "\u4e14\u5728\u672a\u6765\u4e09\u4e2a\u6708\uff082026 \u5e74 4 \u6708 16 \u65e5\u81f3 2026 \u5e74 7 \u6708 15 \u65e5\uff09\u5185\uff0c"
        "\u5982\u518d\u6b21\u89e6\u53d1\u201c\u6d4b\u8bd5\u8f6c\u503a\u201d\u8f6c\u80a1\u4ef7\u683c\u5411\u4e0b\u4fee\u6b63\u6761\u6b3e\uff0c\u4ea6\u4e0d\u63d0\u51fa\u5411\u4e0b\u4fee\u6b63\u65b9\u6848\u3002"
    )

    class FakeProvider:
        name = "fake_cninfo"

        def list_bond_announcements(self, bond_code, start, end):
            return [
                {
                    "title": "\u5173\u4e8e\u4e0d\u5411\u4e0b\u4fee\u6b63\u201c\u6d4b\u8bd5\u8f6c\u503a\u201d\u8f6c\u80a1\u4ef7\u683c\u7684\u516c\u544a",
                    "date": date(2026, 4, 15),
                    "url": "http://example.com/test.PDF",
                    "pdf_url": "http://example.com/test.PDF",
                },
            ]

        def download_announcement_text(self, pdf_url):
            return fake_body

    store = CBEventStore(tmp_path / "events.json")
    result = sync_cb_events(
        FakeProvider(),
        ["128009.SZ"],
        store,
        start=date(2026, 1, 1),
        end=date(2026, 4, 28),
        download_pdf=True,
    )

    assert result["scanned_announcements"] == 1
    assert result["added"] == 1
    assert result["pdf_downloaded"] == 1

    events = store.list_events("128009.SZ")
    assert len(events) == 1
    event = events[0]
    assert event.event_type == "down_reset_rejected"
    assert event.commitment_months == 3
    assert event.effective_start == date(2026, 4, 16)
    assert event.effective_end == date(2026, 7, 15)


def test_sync_without_pdf_download(tmp_path):
    """download_pdf=False 时只用标题解析, 不下载 PDF."""

    class FakeProvider:
        name = "fake"

        def list_bond_announcements(self, bond_code, start, end):
            return [
                {
                    "title": "关于不向下修正转股价格的公告",
                    "date": date(2026, 4, 15),
                    "url": "http://example.com/test.PDF",
                    "pdf_url": "http://example.com/test.PDF",
                },
            ]

        def download_announcement_text(self, pdf_url):
            raise AssertionError("不应该被调用!")

    store = CBEventStore(tmp_path / "events.json")
    result = sync_cb_events(
        FakeProvider(),
        ["128009.SZ"],
        store,
        start=date(2026, 1, 1),
        end=date(2026, 4, 28),
        download_pdf=False,
    )

    assert result["added"] == 1
    assert result["pdf_downloaded"] == 0

    events = store.list_events("128009.SZ")
    assert len(events) == 1
    assert events[0].commitment_months is None  # 无 body, 不解析承诺期


def test_sync_with_pdf_download_failure(tmp_path):
    """PDF 下载失败时仍然可以正常用标题解析."""

    class FakeProvider:
        name = "fake"

        def list_bond_announcements(self, bond_code, start, end):
            return [
                {
                    "title": "关于不提前赎回可转债的公告",
                    "date": date(2026, 4, 1),
                    "pdf_url": "http://example.com/missing.PDF",
                },
            ]

        def download_announcement_text(self, pdf_url):
            return None  # 下载失败

    store = CBEventStore(tmp_path / "events.json")
    result = sync_cb_events(
        FakeProvider(),
        ["113050.SH"],
        store,
        start=date(2026, 1, 1),
        end=date(2026, 4, 28),
        download_pdf=True,
    )

    assert result["added"] == 1
    assert result["pdf_downloaded"] == 0
    assert result["pdf_failed"] == 1

    events = store.list_events("113050.SH")
    assert len(events) == 1
    assert events[0].event_type == "call_no_redemption"


# ── CninfoAnnouncementProvider 实例化测试 ──

def test_cninfo_provider_instantiates():
    provider = CninfoAnnouncementProvider()
    assert provider.name == "cninfo"


def test_cninfo_provider_raises_on_non_announcement_methods():
    provider = CninfoAnnouncementProvider()
    import pytest
    with pytest.raises(NotImplementedError):
        provider.get_bond_terms("128009.SZ", date(2026, 4, 28))
    with pytest.raises(NotImplementedError):
        provider.get_stock_close("000001.SZ", date(2026, 4, 28))
