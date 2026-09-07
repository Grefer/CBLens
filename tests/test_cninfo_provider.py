"""CninfoAnnouncementProvider 与 cb_event_sync PDF 注入测试."""
import re
import time
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from convertible_bond.cb_event_sync import (
    _needs_body,
    parse_credit_rating_change,
    parse_credit_rating_terms,
    parse_conversion_price_adjustment,
    parse_outstanding_balance_change,
    parse_terms_patch_from_announcement,
    sync_cb_events,
)
from convertible_bond.cb_events import CBEventStore
from convertible_bond.historical_terms import TermsPatchStore
from convertible_bond.cninfo_provider import (
    CninfoAnnouncementProvider,
    _parse_announcement_item,
    _wind_code_to_plain,
    _infer_column,
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


def test_list_bond_announcements_falls_back_to_searchkey():
    provider = CninfoAnnouncementProvider()
    provider._resolve_stock_param = lambda plain_code: plain_code

    rows = [
        {
            "title": "关于不提前赎回可转债的公告",
            "date": date(2026, 4, 1),
            "url": "http://example.com/test.PDF",
        },
    ]
    calls = []

    def fake_query_pages(*, stock, se_date, column, category, searchkey=""):
        calls.append((stock, column, category, searchkey))
        if stock == "" and searchkey == "110073":
            return rows
        return []

    provider._query_pages = fake_query_pages

    result = provider.list_bond_announcements(
        "110073.SH",
        date(2026, 1, 1),
        date(2026, 4, 29),
    )

    assert result == rows
    assert calls[0] == ("110073", "sse", "category_cb_szsh", "")
    assert ("", "sse", "category_cb_szsh", "110073") in calls


def test_query_pages_raises_on_first_page_http_error():
    import pytest

    provider = CninfoAnnouncementProvider(request_interval=0)
    resp = MagicMock(status_code=500)
    provider._session.post = MagicMock(return_value=resp)

    with pytest.raises(RuntimeError, match="HTTP 500"):
        provider._query_pages(
            stock="110073",
            se_date="2026-01-01~2026-04-29",
            column="sse",
            category="",
        )


# ── 公告解析测试 ──

def test_parse_announcement_item_basic():
    ann = {
        "announcementTitle": "关于<em>不提前</em>赎回可转债的公告",
        "announcementTime": 1714185600000,  # 2024-04-27 08:00 北京时间
        "adjunctUrl": "finalpage/2024-04-27/test.PDF",
    }
    row = _parse_announcement_item(ann)
    assert row is not None
    assert row["title"] == "关于不提前赎回可转债的公告"
    assert row["date"] == date(2024, 4, 27)
    assert "static.cninfo.com.cn" in row["url"]
    assert row["url"].endswith(".PDF")


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="time.tzset 仅 POSIX 可用")
@pytest.mark.parametrize("tz", [
    "America/Los_Angeles",   # UTC-7: 按本机时区解析会早一天
    "UTC",
    "Asia/Shanghai",
    "Pacific/Kiritimati",    # UTC+14: 反方向也不能晚一天
])
def test_parse_announcement_item_date_is_timezone_independent(monkeypatch, tz):
    """公告日期按北京时间口径, 不随运行机器时区漂移.

    巨潮的 announcementTime 是北京时间; 早年用本机时区解析, 美西用户拿到的
    每条公告都早一天, 会连带把事件日期和回测防前视判断整体前移。
    """
    monkeypatch.setenv("TZ", tz)
    time.tzset()
    try:
        row = _parse_announcement_item({
            "announcementTitle": "关于不提前赎回可转债的公告",
            "announcementTime": 1714185600000,   # 2024-04-27 08:00 北京时间
            "adjunctUrl": "",                    # 清空 URL, 排除日期从 URL 兜底的可能
        })
        assert row is not None
        assert row["date"] == date(2024, 4, 27)
    finally:
        monkeypatch.undo()
        time.tzset()


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
    assert _needs_body("关于提前赎回“阿拉转债”并摘牌的公告") is True


def test_needs_body_for_conversion_price_adjustment():
    assert _needs_body("关于可转换公司债券转股价格调整的公告") is True


def test_needs_body_for_rating_change():
    assert _needs_body("关于可转换公司债券2026年跟踪评级结果的公告") is True


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
        bond_names={"128009.SZ": "测试转债"},
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
        bond_names={"128009.SZ": "测试转债"},
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
        bond_names={"113050.SH": "南银转债"},
    )

    assert result["added"] == 1
    assert result["pdf_downloaded"] == 0
    assert result["pdf_failed"] == 1

    events = store.list_events("113050.SH")
    assert len(events) == 1
    assert events[0].event_type == "call_no_redemption"


def test_parse_conversion_price_adjustment_extracts_k_and_effective_date():
    body = (
        "本次调整前转股价格为10.20元/股，调整后转股价格为9.80元/股。"
        "调整后的转股价格自2026年5月12日起生效。"
    )
    parsed = parse_conversion_price_adjustment(body)
    assert parsed is not None
    assert parsed["old_price"] == 10.20
    assert parsed["new_price"] == 9.80
    assert parsed["effective_date"] == date(2026, 5, 12)


def test_parse_credit_rating_change_is_bond_rating_only():
    body = "经评定，公司主体信用等级为AA，天润转债债项信用等级为AA+，评级展望为稳定。"
    assert parse_credit_rating_change(body, title="关于跟踪评级结果的公告") == "AA+"
    assert parse_credit_rating_change(
        body,
        title="关于变更惠云转债信用评级机构的公告",
    ) is None


def test_parse_credit_rating_change_handles_bond_downgrade_phrase():
    body = (
        "综合以上分析，联合资信决定将公司个体信用等级由bb-下调至b，"
        "主体长期信用等级由BB-下调至B，将“龙大转债”信用等级由BB-下调至B，"
        "评级展望为负面。"
    )
    assert parse_credit_rating_change(body, title="关于下调主体及相关债项信用评级的公告") == "B"


def test_parse_credit_rating_terms_extracts_outlook_and_watch():
    body = (
        "经评定，天润转债债项信用等级为AA+，评级展望由稳定调整为负面，"
        "并列入信用评级观察名单。"
    )
    parsed = parse_credit_rating_terms(body, title="关于跟踪评级结果的公告")
    assert parsed["credit_rating"] == "AA+"
    assert parsed["credit_rating_outlook"] == "负面"
    assert parsed["credit_watch_status"] == "列入观察名单"


def test_parse_outstanding_balance_change_handles_common_units():
    assert parse_outstanding_balance_change(
        "截至本公告披露日，“龙大转债”未转股余额为94591.26万元。"
    ) == 9.459126
    assert parse_outstanding_balance_change(
        "本次赎回完成后，剩余可转债余额为0元。"
    ) == 0.0
    assert parse_outstanding_balance_change(
        "截至2026年5月31日，可转债余额为9.46亿元。"
    ) == 9.46


# ── 余额解析: 门槛条款污染回归 ──
#
# 赎回/回售/停止交易条款会成段引用"未转股余额少于3,000万元时…", 早期宽松正则把它
# 当成真实余额, 让 546 条余额 patch 里 528 条值恰为 0.3 亿、覆盖 103 只债, 其中 96 只
# 真实余额 ≥0.5 亿 —— 这些大盘券随后被准入过滤当成"余额过小"整批踢出主池。

@pytest.mark.parametrize("text", [
    # 不提前赎回公告的标准段落 (存量脏数据最大来源)
    "根据《募集说明书》的约定，当本次发行的可转换公司债券未转股余额少于3,000万元时，"
    "公司有权按面值加当期应计利息的价格赎回全部未转股的可转债。",
    "当本次发行的可转换公司债券未转股余额低于3,000万元时，公司有权决定赎回。",
    "若“XX转债”未转股余额不足3,000万元，公司有权行使提前赎回权。",
    "本次发行的可转换公司债券未转股余额小于3000万元时，公司有权提前赎回。",
    "在转股期内，可转债未转股余额未达到3,000万元的，不触发本条款。",
    "如果未转股余额达不到人民币3,000万元，公司董事会有权提议赎回。",
    # 门槛写成亿元口径: 证明判据是措辞而不是"值恰为 0.3"
    "未转股余额低于0.3亿元的，公司有权按面值加当期应计利息的价格赎回。",
    # 门槛写在金额之后, gap 为空, 只能靠尾缀识别
    "未转股余额3,000万元以下时，公司有权赎回。",
    # 交易所停止交易规则引述
    "根据《深圳证券交易所可转换公司债券业务实施细则》，当可转债未转股余额少于3,000万元时，"
    "本所自公司发布相关公告三个交易日后停止其交易。",
    # 门槛句不以"时/的"收尾
    "本公司可转换公司债券未转股余额少于3,000万元后的三个交易日起，将对“XX转债”停止交易。",
])
def test_parse_outstanding_balance_rejects_threshold_clause(text):
    assert parse_outstanding_balance_change(text) is None


def test_parse_outstanding_balance_keeps_genuine_threshold_sized_value():
    """真实披露的 3,000 万元余额必须照常解析 —— 判据是措辞, 不是数值。"""
    assert parse_outstanding_balance_change(
        "截至本公告日，“XX转债”未转股余额为3,000万元。"
    ) == pytest.approx(0.3)
    assert parse_outstanding_balance_change(
        "截至2026年7月31日，“XX转债”未转股余额为2,850万元，已低于3,000万元，将停止交易。"
    ) == pytest.approx(0.285)


@pytest.mark.parametrize("text, expected", [
    # 门槛在前、真实披露在后 (宽松档才能命中真实值)
    ("根据《募集说明书》，当未转股余额少于3,000万元时，公司有权提前赎回。"
     "截至本公告披露日，“和邦转债”未转股余额合计为46.02亿元。", 46.02),
    # 真实披露在前、门槛在后
    ("截至本公告日未转股余额为12,345.00万元；当未转股余额少于3,000万元时公司有权赎回。", 1.2345),
    # 真实值本身贴近门槛
    ("未转股余额少于3,000万元时公司有权赎回；截至本公告披露日，未转股余额为人民币0.31亿元。", 0.31),
])
def test_parse_outstanding_balance_prefers_real_disclosure_over_threshold(text, expected):
    assert parse_outstanding_balance_change(text) == pytest.approx(expected)


def test_parse_outstanding_balance_handles_fullwidth_thousands_separator():
    """全角千分位曾被截断成 0.0 —— 比漏解析更危险 (余额 0 会让该债被当成已赎回踢出主池)。"""
    assert parse_outstanding_balance_change(
        "截至本公告日，“XX转债”未转股余额为3，000万元。"
    ) == pytest.approx(0.3)


def test_parse_outstanding_balance_takes_first_not_future_state():
    """多值时取第一个。提前赎回公告惯用"当期 X 亿 → 赎回完成后 0 元", 取最后会把未来态
    当成当期余额, 而 0 余额在准入里是强杀值 —— 那正是本次要消灭的错值形态。"""
    assert parse_outstanding_balance_change(
        "关于提前赎回“旺能转债”的公告。截至本公告日，“旺能转债”未转股余额为1.86亿元。"
        "本次赎回完成后，“旺能转债”未转股余额为0元，并将在深交所摘牌。"
    ) == pytest.approx(1.86)
    # 赎回结果/摘牌公告里的零余额是当期真值, 仍要解析出来
    assert parse_outstanding_balance_change(
        "本次赎回完成后，剩余可转债余额为0元。"
    ) == 0.0


def test_parse_outstanding_balance_ignores_generic_word_as_bond_name():
    """通用词"可转债"不是简称, 不能让单债公告被歧义闸误拦。"""
    assert parse_outstanding_balance_change(
        "公司可转债未转股余额为2.00亿元；截至本公告日，本次可转债余额为1.50亿元。"
    ) == pytest.approx(2.00)


def test_parse_outstanding_balance_unit_conversion_is_bit_stable():
    """换算必须是除法: 乘 1e-4 在约 31% 的万元取值上位级不等, 会让同一条公告重新同步时
    生成"新" patch 而不是命中 TermsPatch.key 去重 (key 含字段值)。"""
    assert parse_outstanding_balance_change(
        "截至本公告日，“XX转债”未转股余额为2,850万元。") == 2850 / 10000.0
    assert parse_outstanding_balance_change(
        "截至本公告披露日，“龙大转债”未转股余额为94591.26万元。") == 94591.26 / 10000.0
    assert parse_outstanding_balance_change(
        "未转股余额:50,000,000元") == 50000000 / 100000000.0


def test_parse_outstanding_balance_rejects_two_bond_ambiguity():
    """一份公告覆盖两只转债且余额不同时宁可不解析, 避免把 A 债余额写进 B 债 patch。"""
    assert parse_outstanding_balance_change(
        "“A转债”未转股余额为2.00亿元；“B转债”未转股余额为3.00亿元。"
    ) is None


def test_sync_does_not_write_balance_patch_from_threshold_clause(tmp_path):
    """端到端防回流: 只含门槛条款的"不提前赎回"公告不得再生成余额 patch。

    这条守的是回洗的持久性 —— sync 会重新解析窗口内的**每一条**公告并按
    TermsPatch.key 去重, 若解析仍产出 0.3, cb-repair-balance-patches 的成果
    会在下一次 cb-sync-events --apply 时被悄悄写回来。
    """
    body = (
        "公司董事会决定本次不行使“和邦转债”的提前赎回权。根据《募集说明书》约定，"
        "在本次发行的可转换公司债券转股期内，当本次发行的可转换公司债券未转股余额"
        "少于人民币3,000万元时，公司有权按照债券面值加当期应计利息的价格赎回全部"
        "未转股的可转债。"
    )

    class FakeProvider:
        name = "fake_cninfo"

        def list_bond_announcements(self, bond_code, start, end):
            return [{
                "title": "关于不提前赎回“和邦转债”的公告",
                "date": date(2026, 5, 9),
                "url": "http://example.com/n.PDF",
                "pdf_url": "http://example.com/n.PDF",
            }]

        def download_announcement_text(self, pdf_url):
            return body

    store = CBEventStore(tmp_path / "events.json")
    patch_store = TermsPatchStore(tmp_path / "patches.json")
    result = sync_cb_events(
        FakeProvider(), ["113691.SH"], store,
        term_patch_store=patch_store,
        start=date(2026, 5, 1), end=date(2026, 5, 20),
        bond_names={"113691.SH": "和邦转债"},
    )

    # 事件本身照常入库 (不提前赎回承诺仍要影响定价), 但不得带余额 patch
    assert result["added"] == 1
    assert store.list_events("113691.SH")[0].event_type == "call_no_redemption"
    assert result["patches_added"] == 0
    assert patch_store.list_patches("113691.SH") == []


def test_sync_writes_terms_patch_for_conversion_price_adjustment(tmp_path):
    body = (
        "本次调整前转股价格为10.20元/股，调整后转股价格为9.80元/股。"
        "调整后的转股价格自2026年5月12日起生效。"
    )

    class FakeProvider:
        name = "fake_cninfo"

        def list_bond_announcements(self, bond_code, start, end):
            return [
                {
                    "title": "关于可转换公司债券转股价格调整的公告",
                    "date": date(2026, 5, 9),
                    "url": "http://example.com/k.PDF",
                    "pdf_url": "http://example.com/k.PDF",
                },
            ]

        def download_announcement_text(self, pdf_url):
            return body

    store = CBEventStore(tmp_path / "events.json")
    patch_store = TermsPatchStore(tmp_path / "patches.json")
    result = sync_cb_events(
        FakeProvider(),
        ["123211.SZ"],
        store,
        term_patch_store=patch_store,
        start=date(2026, 5, 1),
        end=date(2026, 5, 20),
        bond_names={"123211.SZ": "阳谷转债"},
    )

    assert result["added"] == 1
    assert result["patches_added"] == 1
    events = store.list_events("123211.SZ")
    assert events[0].event_type == "conversion_price_adjusted"
    patches = patch_store.list_patches("123211.SZ")
    assert patches[0].effective_date == date(2026, 5, 12)
    assert patches[0].fields == {"conversion_price": 9.8}
    assert patches[0].before_fields == {"conversion_price": 10.2}


def test_sync_writes_terms_patch_for_balance_change(tmp_path):
    body = "截至本公告披露日，“龙大转债”未转股余额为94591.26万元。"

    class FakeProvider:
        name = "fake_cninfo"

        def list_bond_announcements(self, bond_code, start, end):
            return [
                {
                    "title": "关于可转换公司债券转股结果暨股份变动公告",
                    "date": date(2026, 5, 20),
                    "url": "http://example.com/balance.PDF",
                    "pdf_url": "http://example.com/balance.PDF",
                },
            ]

        def download_announcement_text(self, pdf_url):
            return body

    store = CBEventStore(tmp_path / "events.json")
    patch_store = TermsPatchStore(tmp_path / "patches.json")
    result = sync_cb_events(
        FakeProvider(),
        ["128119.SZ"],
        store,
        term_patch_store=patch_store,
        start=date(2026, 5, 1),
        end=date(2026, 5, 25),
        bond_names={"128119.SZ": "龙大转债"},
    )

    assert result["added"] == 1
    assert result["patches_added"] == 1
    events = store.list_events("128119.SZ")
    assert events[0].event_type == "balance_change"
    patches = patch_store.list_patches("128119.SZ")
    assert patches[0].fields == {"outstanding_balance": 9.459126}


def test_sync_writes_terms_patch_for_call_redemption_price(tmp_path):
    body = (
        "本次赎回的最后交易日为2026年4月27日。"
        "赎回登记日为2026年5月6日。"
        "本次赎回价格为100.62元/张。"
    )

    class FakeProvider:
        name = "fake_cninfo"

        def list_bond_announcements(self, bond_code, start, end):
            return [
                {
                    "title": "关于实施“阿拉转债”赎回暨摘牌的公告",
                    "date": date(2026, 4, 15),
                    "url": "http://example.com/call.PDF",
                    "pdf_url": "http://example.com/call.PDF",
                },
            ]

        def download_announcement_text(self, pdf_url):
            return body

    store = CBEventStore(tmp_path / "events.json")
    patch_store = TermsPatchStore(tmp_path / "patches.json")
    result = sync_cb_events(
        FakeProvider(),
        ["118006.SH"],
        store,
        term_patch_store=patch_store,
        start=date(2026, 4, 1),
        end=date(2026, 4, 30),
        bond_names={"118006.SH": "阿拉转债"},
    )

    assert result["added"] == 1
    assert result["patches_added"] == 1
    events = store.list_events("118006.SH")
    assert events[0].event_price == 100.62
    patches = patch_store.list_patches("118006.SH")
    assert patches[0].fields == {"call_redemption_price": 100.62}


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


# ── 转股价解析: 历次调整沿革导致取到最老的一次 ──
#
# 调整公告的惯例结构是「开头结构化摘要 → 中段历次调整沿革 → 结尾综上」。旧实现按
# pattern 顺序 + re.search 取首个匹配, 于是抓到沿革里最早那次: 万孚转债 14 条 patch
# 跨两年恒为 93.57 (2020 年的初始价附近), 而真实 K 是 20.88 —— 主池 60% 的 K 被写坏。

def test_conversion_price_prefers_header_summary_over_history_recap():
    """结构化摘要只描述本次调整, 优先级最高。"""
    text = (
        "特别提示：1、债券代码：123064，债券简称：万孚转债"
        "3、调整前转股价格：21.10元/股4、调整后转股价格：20.88元/股"
        "5、调整后转股价格生效日期：2026年6月2日。"
        "本次发行的可转债初始转股价为93.55元/股。"
        "“万孚转债”转股价格由93.55元/股调整为93.57元/股。"
        "“万孚转债”转股价格由93.57元/股调整为71.64元/股。"
    )
    parsed = parse_conversion_price_adjustment(text)
    assert parsed["new_price"] == pytest.approx(20.88)
    assert parsed["old_price"] == pytest.approx(21.10)


def test_conversion_price_takes_latest_of_history_recap():
    """没有摘要时, 叙述型沿革按时间排 —— 取最后一次而不是第一次。"""
    text = (
        "本次发行的可转债初始转股价为93.55元/股。"
        "“万孚转债”转股价格由93.55元/股调整为93.57元/股。"
        "“万孚转债”转股价格由93.57元/股调整为71.64元/股。"
        "综上，“万孚转债”转股价格由21.10元/股调整为20.88元/股。"
    )
    parsed = parse_conversion_price_adjustment(text)
    assert parsed["new_price"] == pytest.approx(20.88)
    assert parsed["old_price"] == pytest.approx(21.10)


def test_conversion_price_single_price_fallback_takes_latest():
    text = ("转股价格调整为93.57元/股。转股价格调整为71.64元/股。"
            "转股价格调整为20.88元/股。")
    assert parse_conversion_price_adjustment(text)["new_price"] == pytest.approx(20.88)


@pytest.mark.parametrize("separator", ["为", ":", "：", "为:", "为："])
def test_conversion_price_summary_accepts_punctuation_without_taking_old_recap(separator):
    """长海 2026-09-02: 「为：」摘要不能让位给 2024 年沿革。"""
    text = (
        f"本次调整前“长海转债”的转股价格{separator}14.99元/股。"
        f"本次调整后“长海转债”的转股价格{separator}14.79元/股。"
        "本次转股价格调整生效日期：2026年9月9日。"
        "历次调整情况：转股价格由15.79元/股调整为15.64元/股，"
        "调整后的转股价格自2024年5月29日起生效。"
    )
    assert parse_conversion_price_adjustment(text) == {
        "old_price": 14.99, "new_price": 14.79,
        "effective_date": date(2026, 9, 9), "confidence": "parsed",
    }


def test_conversion_price_compact_summary_and_start_date_precede_history():
    """希望转 2 的共享表头与「调整起始日期」都在摘要, 不能拿沿革日期拼接。"""
    text = (
        "特别提示：“希望转2”转股价格：调整前为10.59元/股，调整后为10.13元/股。"
        "转股价格调整起始日期：2026年9月1日。"
        "前次转股价格调整情况：转股价格由14.39元/股向下修正为10.6元/股，"
        "修正后的转股价格自2023年12月28日起生效。"
        "转股价格由10.61元/股调整为10.59元/股。"
    )
    assert parse_conversion_price_adjustment(text) == {
        "old_price": 10.59, "new_price": 10.13,
        "effective_date": date(2026, 9, 1), "confidence": "parsed",
    }


@pytest.mark.parametrize("date_label", [
    "本次转股价格调整实施日期", "本次转股价格修正实施日期",
    "转股价格调整生效时间", "新转股价实行日期",
])
def test_conversion_price_effective_date_label_does_not_fall_back_to_history(date_label):
    text = (
        "调整前转股价格：19.00元/股。调整后转股价格：19.02元/股。"
        f"{date_label}：2026年7月10日。"
        "此前调整后的转股价格自2024年6月24日起生效。"
    )
    assert parse_conversion_price_adjustment(text)["effective_date"] == date(2026, 7, 10)


@pytest.mark.parametrize("original", ["原", "原来的"])
def test_conversion_price_narrative_accepts_original_price_and_its_latest_date(original):
    text = (
        "前次转股价格由15.79元/股调整为15.64元/股，"
        "调整后的转股价格自2024年5月29日起生效。"
        f"本次转股价格由{original}14.99元/股调整为14.79元/股，"
        "调整后的转股价格自2026年9月9日起生效。"
    )
    parsed = parse_conversion_price_adjustment(text)
    assert parsed["old_price"] == 14.99
    assert parsed["new_price"] == 14.79
    assert parsed["effective_date"] == date(2026, 9, 9)


def test_conversion_price_summary_accepts_currency_label_for_upward_adjustment():
    """国微 2026-07-24 是回购后的上调, 不应误取前一次派息下调。"""
    text = (
        "调整前转股价格：人民币96.99元/股。调整后转股价格：人民币97.01元/股。"
        "转股价格调整生效日期：2026年7月24日。"
        "前次转股价格由97.30元/股调整为96.99元/股，"
        "调整后的转股价格自2026年6月30日起生效。"
    )
    parsed = parse_conversion_price_adjustment(text)
    assert parsed["old_price"] == 96.99
    assert parsed["new_price"] == 97.01
    assert parsed["effective_date"] == date(2026, 7, 24)


def test_conversion_price_current_single_summary_precedes_historical_pair():
    text = (
        "本次调整后“测试转债”的转股价格为：14.79元/股。"
        "调整生效日期：2026年9月9日。"
        "历次调整情况：转股价格由15.79元/股调整为15.64元/股。"
    )
    parsed = parse_conversion_price_adjustment(text)
    assert parsed["new_price"] == 14.79
    assert parsed["old_price"] is None, "本次摘要没给旧价, 不许拿沿革补齐"


@pytest.mark.parametrize("body", [
    "调整前转股价格：42.38元/股。调整后转股价格：42.38元/股。转股价格不变。",
    "转股价格：调整前为8.67元/股，调整后为8.67元/股。本次不作调整。",
    "本次调整后转股价格为：不低于10.00元/股。",
])
def test_conversion_price_does_not_create_a_change_from_confirmation_or_condition(body):
    assert parse_conversion_price_adjustment(body) is None
    assert parse_terms_patch_from_announcement(
        "111025.SH", "关于可转债转股价格调整的公告", date(2026, 8, 20),
        event_type="conversion_price_adjusted", body=body,
    ) is None


# ── 标的串号: 同一发行人两只转债 ──

_MULTI_BOND_SHARED_BLOCKS = (
    "证券代码：688533 证券简称：上声电子\n"
    "转债代码：118037 转债简称：上声转债\n"
    "转债代码：118067 转债简称：上 26 转债\n"
    "重要内容提示："
    "调整前“上声转债”转股价格：28.99元/股；“上26转债”转股价格：32.60元/股。"
    "调整后“上声转债”转股价格：28.64元/股；“上26转债”转股价格：32.25元/股。"
    "转股价格调整实施日期：2026年6月8日。"
    "一、转股价格调整依据。"
    "历次调整情况：“上声转债”转股价格由32.00元/股调整为28.99元/股。"
)
_MULTI_BOND_NAMED_ROWS = (
    "证券代码：605166 证券简称：聚合顺\n"
    "转债代码：111003 转债简称：聚合转债\n"
    "转债代码：111020 转债简称：合顺转债\n"
    "重要内容提示："
    "“聚合转债”修正前转股价格：11.37元/股。"
    "“聚合转债”修正后转股价格：11.31元/股。"
    "“合顺转债”修正前转股价格：10.59元/股。"
    "“合顺转债”修正后转股价格：10.53元/股。"
    "“聚合转债”、“合顺转债”本次转股价格调整实施日期：2026年6月24日。"
    "一、可转债基本情况。"
    "历次调整情况：转股价格由14.63元/股调整为14.42元/股。"
)


@pytest.mark.parametrize("body,code,old,new,effective", [
    (_MULTI_BOND_SHARED_BLOCKS, "118037.SH", 28.99, 28.64, date(2026, 6, 8)),
    (_MULTI_BOND_SHARED_BLOCKS, "118067.SH", 32.60, 32.25, date(2026, 6, 8)),
    (_MULTI_BOND_NAMED_ROWS, "111003.SH", 11.37, 11.31, date(2026, 6, 24)),
    (_MULTI_BOND_NAMED_ROWS, "111020.SH", 10.59, 10.53, date(2026, 6, 24)),
])
def test_conversion_price_multi_bond_announcement_selects_target(body, code, old, new, effective):
    """两份官方 PDF 的摘要结构; 同一公告按查询债券返回各自的 K。"""
    expected = {
        "old_price": old, "new_price": new,
        "effective_date": effective, "confidence": "parsed",
    }
    assert parse_conversion_price_adjustment(body, bond_code=code) == expected
    assert parse_conversion_price_adjustment(body, bond_code=code[:6]) == expected
    patch = parse_terms_patch_from_announcement(
        code, "关于实施年度权益分派调整可转债转股价格的公告", date(2026, 5, 29),
        event_type="conversion_price_adjusted", body=body,
    )
    assert patch.fields == {"conversion_price": new}
    assert patch.before_fields == {"conversion_price": old}
    assert patch.effective_date == effective


@pytest.mark.parametrize("body", [_MULTI_BOND_SHARED_BLOCKS, _MULTI_BOND_NAMED_ROWS])
@pytest.mark.parametrize("code", [None, "123456.SZ"])
def test_conversion_price_multi_bond_without_known_target_is_ambiguous(body, code):
    assert parse_conversion_price_adjustment(body, bond_code=code) is None


@pytest.mark.parametrize("body", [
    _MULTI_BOND_SHARED_BLOCKS.replace("“上26转债”转股价格：32.25元/股", "本次不调整"),
    _MULTI_BOND_SHARED_BLOCKS.replace("转债简称：上 26 转债", "转债简称：未知简称"),
    _MULTI_BOND_SHARED_BLOCKS.replace(
        "一、转股价格调整依据", "上26转债转股价格调整实施日期：2026年6月9日。一、转股价格调整依据"),
    _MULTI_BOND_SHARED_BLOCKS.replace(
        "一、转股价格调整依据", "“上26转债”转股价格：31.00元/股。一、转股价格调整依据"),
])
def test_conversion_price_multi_bond_incomplete_or_conflicting_summary_does_not_fall_back(body):
    assert parse_conversion_price_adjustment(body, bond_code="118067.SH") is None


def test_conversion_price_single_bond_code_rejects_other_target():
    body = "债券代码：123002 债券简称：测试转债。调整前转股价格：10.0元/股。调整后转股价格：9.8元/股。"
    assert parse_conversion_price_adjustment(body, bond_code="123091.SZ") is None
    assert parse_conversion_price_adjustment(body, bond_code="123002.SZ")["new_price"] == 9.8
    assert parse_conversion_price_adjustment(body)["new_price"] == 9.8


def test_conversion_price_regular_bond_codes_and_pdf_page_number_do_not_imply_multiple_cbs():
    body = (
        "债券代码：242471 债券简称：25渝水01。"
        "债券代码：149812 债券简称：22太阳G1。"
        "转债代码：123269 转债简称：金杨转债。"
        "债券代码：\n1\n123269。"
        "本次调整前的转股价格：39.80元/股。本次调整后的转股价格：28.32元/股。"
        "调整后的转股价格生效日期：2026年6月3日。"
    )
    assert parse_conversion_price_adjustment(body, bond_code="123269.SZ")["new_price"] == 28.32


def test_conversion_price_multi_bond_header_without_named_summary_stays_ambiguous():
    body = (
        "转债代码：111003 转债简称：聚合转债。转债代码：111020 转债简称：合顺转债。"
        "重要内容提示：修正前转股价格：11.31元/股。修正后转股价格：8.85元/股。"
        "“聚合转债”本次转股价格修正实施日期：2026年8月14日。"
    )
    assert parse_conversion_price_adjustment(body, bond_code="111003.SH") is None
    assert parse_conversion_price_adjustment(body, bond_code="111020.SH") is None


def test_terms_patch_rejects_announcement_naming_another_bond():
    """cninfo 按发行人返回公告, 另一只债的调整公告会被归到当前查询的 code 上。

    实测污染 ≥11 条 patch / 5 只债 (嘉益转债被写进"精达转债"的 3.26, K 从 79.66 变 3.26)。
    """
    body = "本次调整前转股价格为10.20元/股，调整后转股价格为9.80元/股。"
    common = dict(event_date=date(2026, 5, 9), body=body)

    assert parse_terms_patch_from_announcement(
        "123250.SZ", "精达股份关于因实施2025年年度权益分派调整“精达转债”转股价格的公告",
        bond_name="嘉益转债", **common) is None

    # 点名本债 → 保留
    kept = parse_terms_patch_from_announcement(
        "123250.SZ", "关于嘉益转债转股价格调整的公告", bond_name="嘉益转债", **common)
    assert kept is not None and kept.fields["conversion_price"] == pytest.approx(9.8)

    # 通用标题 (只说"可转债", 没点名) → 保留
    assert parse_terms_patch_from_announcement(
        "123250.SZ", "关于回购股份注销完成调整可转债转股价格的公告",
        bond_name="嘉益转债", **common) is not None

    # 一份公告覆盖同发行人两只债且含本债 → 保留
    assert parse_terms_patch_from_announcement(
        "123124.SZ", "关于晶瑞转债、晶瑞转2转股价格调整的公告",
        bond_name="晶瑞转债", **common) is not None

    # 不知道本债简称时不做校验 (退化成旧行为)
    assert parse_terms_patch_from_announcement(
        "123250.SZ", "关于“精达转债”转股价格调整的公告", **common) is not None


def test_extract_text_from_pdf_bytes_falls_through_without_crashing(caplog):
    """坏字节流要安静地返回 None, 并按 pdfplumber → pypdf → PyPDF2 依次尝试。

    旧实现只在 PyPDF2 缺失时打"pdfplumber 和 PyPDF2 均未安装" —— 而 pdfplumber 装着、
    只是对扫描件返回空文本时也会走到这句, 给出完全错误的诊断 (实测同步日志里出现 5 次,
    误导成"要装依赖")。现在区分"没装库"与"装了但提不出文本"。
    """
    from convertible_bond.cninfo_provider import extract_text_from_pdf_bytes

    assert extract_text_from_pdf_bytes(b"not a pdf at all") is None
    # 不应该再声称库没装 —— 本机 pdfplumber/pypdf 都在
    assert "均未安装" not in caplog.text


# ── 评级报告附录词表 ────────────────────────────────────────────────────────
#
# 跟踪评级报告末尾成段解释评级符号含义, 把"列入正面观察名单"逐个列一遍。早期正则全文搜
# 关键词就命中了词表 —— 实测主池 51 只带观察状态的债里 47 只的值来自那段附录 (皓元/国检/
# 花园/鸿路四份 2026 跟踪评级报告原句一字不差), 真正来自专项公告的只有 4 只。
# 与 ``parse_outstanding_balance_change`` 踩过的"赎回门槛条款被当成当期余额"是同一类陷阱。

_LEGEND = (
    "附件4-1主体长期信用等级设置及含义联合资信主体长期信用等级划分为三等九级，"
    "符号表示为：AAA、AA、A、BBB、BB、B、CCC、CC、C。"
    "附件4-2评级观察设置及含义列入评级观察是对于已对受评主体给出了评级结果，"
    "由于突发事件，且对突发事件暂时没有结论时，采取的一种评级行动。"
    "评级观察分为“列入正面观察名单”、“列入负面观察名单”和“列入发展中观察名单”。"
    "附件4-3评级展望设置及含义评级展望是对信用等级未来一年左右变化方向的评价，"
    "评级展望通常分为正面、负面、稳定、发展中等四种。"
)


def test_rating_legend_appendix_does_not_fabricate_watch_status():
    body = (
        "联合资信评估股份有限公司出具跟踪评级报告。经过跟踪评级，联合资信确定维持安徽鸿路"
        "钢结构（集团）股份有限公司主体长期信用等级为AA，维持“鸿路转债”的信用等级为AA，"
        "评级展望为稳定。" + "补充分析。" * 40 + _LEGEND
    )
    parsed = parse_credit_rating_terms(body, title="安徽鸿路钢结构公开发行可转换公司债券2026年跟踪评级报告")
    assert parsed["credit_watch_status"] is None      # ← 词表不是状态
    assert parsed["credit_rating"] == "AA"
    assert parsed["credit_rating_outlook"] == "稳定"


def test_real_watch_listing_still_parses():
    """真实专项公告: 有施动者、点名发行人与债项。"""
    body = (
        "中证鹏元决定维持公司主体信用等级为A+，“声迅转债”信用等级维持为A+，"
        "并将公司主体信用等级及“声迅转债”信用等级列入信用评级观察名单。"
    )
    parsed = parse_credit_rating_terms(
        body, title="中证鹏元关于将北京声迅电子股份有限公司主体信用等级和声迅转债信用等级列入信用评级观察名单的公告")
    assert parsed["credit_watch_status"] == "列入观察名单"


def test_watch_removal_still_parses():
    body = "经跟踪评级，中证鹏元决定将公司主体信用等级及“欧晶转债”信用等级撤出信用评级观察名单。"
    assert parse_credit_rating_terms(body, title="关于撤出信用评级观察名单的公告")[
        "credit_watch_status"] == "撤出观察名单"


def test_strip_rating_legend_keeps_short_documents_intact():
    """锚点出现在很靠前的位置 → 不是附录标题, 不能整篇截掉。"""
    from convertible_bond.cb_event_sync import _strip_rating_legend

    short = "评级符号及含义说明。" + "尾部。" * 5
    assert _strip_rating_legend(short) == short


# ── 守护: sync_cb_events 的测试不得依赖真实 cb_data ────────────────

_SYNC_CALL = re.compile(r"\bsync_cb_events\(")


def test_sync_cb_events_tests_pass_bond_names():
    """所有 ``sync_cb_events`` 用例必须显式传 ``bond_names``.

    不传时 ``_bond_name_resolver`` 会 best-effort 去读**真实的**
    ``data/cb_data.json`` 取 ``sec_name``, 再喂给串号守卫
    ``_title_names_other_bond``。于是"用例过不过"取决于那只债当天在库里叫什么:

    实测 2026-08-25 一次纯数据提交把 ``128009.SZ`` 的 ``sec_name`` 从 ``None``
    填成 ``歌尔转债(退市)``, 而该用例的假公告标题写的是「测试转债」——
    守卫正确判为串号并跳过, ``added`` 从 1 变 0, 套件一夜之间转红。
    也就是说这条用例此前一直是**靠那只债在库里没名字**才通过的。

    显式传名字让用例自带债名事实源, 与生产路径的解析结果一致但不再随数据漂移。
    """
    here = Path(__file__).parent
    offenders = []
    for path in sorted(here.glob("test_*.py")):
        text = path.read_text(encoding="utf-8")
        if not _SYNC_CALL.search(text):
            continue
        for match in _SYNC_CALL.finditer(text):
            # 取这次调用的实参段 (到配对右括号为止), 看里面有没有 bond_names=
            depth, i = 0, match.end() - 1
            while i < len(text):
                if text[i] == "(":
                    depth += 1
                elif text[i] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            args = text[match.end():i]
            if "bond_names" in args:
                continue
            lineno = text.count("\n", 0, match.start()) + 1
            offenders.append(f"{path.name}:{lineno}")
    assert not offenders, (
        "以下 sync_cb_events 调用没传 bond_names, 会去读真实 data/cb_data.json, "
        "用例过不过将取决于那只债当天在库里叫什么:\n  " + "\n  ".join(offenders)
    )
