"""cb-repair-putback-windows: 回洗回售事件里缺失/退化的申报窗口。

两类问题混在一起, 测试必须分开钉住:
  ① 正文当时没拿到 —— 重下重解析就能补齐 (实测抽样 20 条缺 end 的记录全部 HTTP 200)。
  ② ``effective_start`` 回落成公告日 —— 那不是窗口, 是谎; 补不回来也要洗掉。

判据与解析侧共用 ``putback_window_is_complete`` / ``putback_start_is_degraded`` /
``parse_putback_terms`` —— 两边各写一份正是本仓库反复踩过的坑。
"""
from datetime import date

import pytest

from convertible_bond.cb_events import (
    CBEvent,
    CBEventStore,
    parse_event_from_announcement,
    parse_putback_terms,
    putback_start_is_degraded,
    putback_window_is_complete,
)
from convertible_bond.cli import repair_putback_windows as mod
from convertible_bond.cli.repair_putback_windows import repair, scan


# ── 正文解析 ────────────────────────────────────────────────────────────────

def _body(window_text: str, price_text: str = "回售价格：100.27元/张（含税）") -> str:
    return (f"特别提示：1、转债代码：113049 2、{price_text} "
            f"3、回售申报期：{window_text} 4、发行人资金到账日：2026年8月10日")


@pytest.mark.parametrize("window_text, expected", [
    # 旧正则本来就认的写法
    ("2026年7月30日至2026年8月5日", (date(2026, 7, 30), date(2026, 8, 5))),
    ("2026年7月30日到2026年8月5日", (date(2026, 7, 30), date(2026, 8, 5))),
    # 亿田转债的写法: "日" 与分隔符之间多一个"起", 旧正则整条不匹配
    ("2026年7月30日起至2026年8月5日", (date(2026, 7, 30), date(2026, 8, 5))),
    # 全半角分隔符变体
    ("2026年7月30日—2026年8月5日", (date(2026, 7, 30), date(2026, 8, 5))),
    ("2026年7月30日～2026年8月5日", (date(2026, 7, 30), date(2026, 8, 5))),
    # 第二个日期省略年份
    ("2026年7月30日起至8月5日", (date(2026, 7, 30), date(2026, 8, 5))),
    # 省略年份且跨年
    ("2025年12月29日起至1月5日", (date(2025, 12, 29), date(2026, 1, 5))),
])
def test_putback_window_separator_variants(window_text, expected):
    parsed = parse_putback_terms(_body(window_text))
    assert (parsed["start"], parsed["end"]) == expected


@pytest.mark.parametrize("price_text, expected", [
    ("回售价格：101.284元/张（含当期应计利息、含税）", 101.284),
    # 天23转债的写法: 单位是"元人民币/张", 旧正则要求字面 "元/张", 价格整条丢失
    ("回售价格：100.05元人民币/张（含当期利息、含税）", 100.05),
    ("回售申报价格为 100.312 元/张", 100.312),
])
def test_putback_price_unit_variants(price_text, expected):
    assert parse_putback_terms(_body("2026年7月30日至2026年8月5日", price_text)
                               )["price"] == pytest.approx(expected)


def test_putback_window_still_requires_an_anchor_word():
    """锚定词是防线, 不能为了提高解析率放宽 —— 募集说明书引用的条款期会被误当本次窗口。"""
    unanchored = ("公司可转债的存续期为 2021年3月11日至2026年9月3日，"
                  "投资者可在满足条件时行使回售权。")
    assert parse_putback_terms(unanchored)["start"] is None


# ── 不再把公告日冒充窗口起始日 ───────────────────────────────────────────────

def test_putback_without_body_gets_no_window_at_all():
    """解析不到就是 None。与 call_redemption 的 last_trading_date 是同一条教训:
    ``effective_start`` 的通用回落值是公告日本身, 而"申报期从公告当天开始"几乎恒为
    解析失败的信号。实测这条回落把主池 28 只债的 putback_start_date 写成了公告日期。
    """
    event = parse_event_from_announcement(
        "127061.SZ", "关于美锦转债回售的第三次提示性公告", date(2025, 12, 4))
    assert event is not None and event.event_type == "putback"
    assert event.effective_start is None
    assert event.effective_end is None


def test_putback_with_body_gets_the_real_window():
    event = parse_event_from_announcement(
        "113049.SH", "关于长汽转债回售的第九次提示性公告", date(2026, 8, 5),
        body=_body("2026年7月30日至2026年8月5日"))
    assert event.effective_start == date(2026, 7, 30)
    assert event.effective_end == date(2026, 8, 5)
    assert event.event_price == pytest.approx(100.27)


def test_supporting_documents_do_not_manufacture_a_fake_window():
    """177 条法律意见书/核查意见也被分类成 putback, 它们本来就没有申报窗口 ——
    回落会让每一条都变成"从公告日开始、永不结束"的假窗口。"""
    event = parse_event_from_announcement(
        "127045.SZ",
        "北京市康达律师事务所关于牧原食品股份有限公司可转换公司债券回售的法律意见书",
        date(2024, 12, 19))
    assert event.event_type == "putback"
    assert event.effective_start is None


# ── 共用判据 ────────────────────────────────────────────────────────────────

def _ev(day, start=None, end=None, price=None, url=-1):
    # URL 必须逐条不同, 否则"正文取得到"与"取不到"两条分支在查表里撞成同一个,
    # 测试会看着通过却什么都没区分开。
    return CBEvent(bond_code="113000.SH", event_date=date(2026, 8, day),
                   event_type="putback", raw_title=f"回售提示性公告{day}",
                   effective_start=start, effective_end=end, event_price=price,
                   source="cninfo", url=f"http://x/{day}.PDF" if url == -1 else url)


def test_shared_predicates():
    complete = _ev(5, date(2026, 8, 1), date(2026, 8, 7))
    degraded = _ev(5, date(2026, 8, 5))            # start == 公告日, 无 end
    empty = _ev(5)
    half = _ev(5, date(2026, 8, 1))                # 有真起始日但没截止日

    assert putback_window_is_complete(complete)
    assert not any(map(putback_window_is_complete, (degraded, empty, half)))
    assert putback_start_is_degraded(degraded)
    assert not any(map(putback_start_is_degraded, (complete, empty, half)))


def test_repair_and_parser_share_one_judgement():
    """回洗不得自带一份"什么算完整窗口"的定义 —— 必须调解析侧那两个函数。"""
    import inspect
    src = inspect.getsource(mod)
    assert "putback_window_is_complete" in src
    assert "putback_start_is_degraded" in src
    assert "parse_putback_terms" in src


# ── 回洗行为 ────────────────────────────────────────────────────────────────

@pytest.fixture()
def store_path(tmp_path):
    path = tmp_path / "cb_events.json"
    CBEventStore(path).add_many([
        _ev(5, date(2026, 8, 1), date(2026, 8, 7)),        # 已完整, 不该动
        _ev(6, date(2026, 8, 6)),                          # 退化, 正文能补回
        _ev(7, date(2026, 8, 7)),                          # 退化, 正文补不回
        _ev(8, url=None),                                  # 无 URL, 无窗口
    ])
    return path


def test_scan_splits_complete_from_degraded(store_path):
    report = scan(store_path)
    assert report["n_putback"] == 4
    assert report["complete"] == 1
    assert len(report["incomplete"]) == 3
    assert len(report["degraded"]) == 2          # 8 号没有 start, 不算"退化"
    assert len(report["no_url"]) == 1


def _patch_fetch(monkeypatch, table):
    def fake_fetch(url, cache_dir, *, download):
        return table.get(url)
    monkeypatch.setattr(mod, "fetch_body", fake_fetch)


def test_repair_backfills_window_and_clears_the_lie(store_path, monkeypatch):
    events = {e.event_date: e for e in CBEventStore(store_path).list_events()}
    _patch_fetch(monkeypatch, {
        events[date(2026, 8, 6)].url: _body("2026年8月3日起至2026年8月9日"),
    })
    report = repair(store_path, dry_run=False, backup=False, download=True, delay=0)
    # 只有 6 号 (补回窗口) 与 7 号 (清掉假起始日) 该变; 5 号已完整、8 号无 URL 且无谎可洗
    assert report["changed"] == 2

    after = {e.event_date: e for e in CBEventStore(store_path).list_events()}
    # ① 正文补回真实窗口
    assert after[date(2026, 8, 6)].effective_start == date(2026, 8, 3)
    assert after[date(2026, 8, 6)].effective_end == date(2026, 8, 9)
    # ② 补不回来的退化记录: 清掉假起始日, 而不是留着骗人
    assert after[date(2026, 8, 7)].effective_start is None
    # ③ 本来就完整的不动
    assert after[date(2026, 8, 5)].effective_start == date(2026, 8, 1)


def test_dry_run_writes_nothing(store_path, monkeypatch):
    _patch_fetch(monkeypatch, {})
    before = store_path.read_bytes()
    report = repair(store_path, dry_run=True, download=True, delay=0)
    assert report["planned"] > 0 and report["changed"] == 0
    assert store_path.read_bytes() == before


def test_repair_is_idempotent(store_path, monkeypatch):
    _patch_fetch(monkeypatch, {})
    repair(store_path, dry_run=False, backup=False, download=True, delay=0)
    second = repair(store_path, dry_run=False, backup=False, download=True, delay=0)
    assert second["changed"] == 0


def test_apply_writes_a_backup(store_path, monkeypatch):
    _patch_fetch(monkeypatch, {})
    report = repair(store_path, dry_run=False, backup=True, download=True, delay=0)
    assert report["backup_path"] is not None and report["backup_path"].exists()


def test_refuses_to_write_when_the_file_changed_underneath(store_path, monkeypatch):
    _patch_fetch(monkeypatch, {})
    real_scan = mod.scan

    def scan_then_touch(path):
        report = real_scan(path)
        CBEventStore(path).add_many([_ev(9)])      # 模拟另一个进程写盘
        return report
    monkeypatch.setattr(mod, "scan", scan_then_touch)
    with pytest.raises(mod.ConcurrentWriteError):
        repair(store_path, dry_run=False, backup=False, download=True, delay=0)


def test_records_without_a_url_are_left_alone(store_path, monkeypatch):
    """没有 URL 也没有起始日 —— 无从重取, 也没有谎要洗, 不该被动。"""
    _patch_fetch(monkeypatch, {})
    repair(store_path, dry_run=False, backup=False, download=True, delay=0)
    after = {e.event_date: e for e in CBEventStore(store_path).list_events()}
    assert after[date(2026, 8, 8)].effective_start is None
    assert after[date(2026, 8, 8)].effective_end is None


def test_fetch_body_caches_empty_results(tmp_path, monkeypatch):
    """扫描件/图片版公告提不出文本, 空结果也要缓存, 否则每次重跑都重下一遍。"""
    hits = []
    monkeypatch.setattr(
        "convertible_bond.cb_event_sync._try_download_body",
        lambda provider, url: hits.append(url) or None)
    for _ in range(3):
        assert mod.fetch_body("http://x/a.PDF", tmp_path, download=True) is None
    assert len(hits) == 1


# ── cb_data 与事件对齐 ──────────────────────────────────────────────────────
#
# 回洗事件表还不够: apply_events_to_terms 只**加**更新从不清字段, 于是事件里那个假窗口
# 消失之后, cb_data.json 里已经写死的 putback_start_date 仍会留着。反过来, 补回来的
# 真窗口也要写回去 —— 同一个动作的两面: "让 cb_data 等于事件重放的结果"。

@pytest.fixture()
def bundle_path(tmp_path):
    from convertible_bond.cache import TermsBundle
    from convertible_bond.data_providers import BondTerms
    path = tmp_path / "cb_data.json"
    bundle = TermsBundle(path)
    common = dict(underlying_code="600000.SH", conversion_price=10.0,
                  maturity_date=date(2030, 1, 1), listing_date=date(2020, 1, 1))
    # A: 事件里有真窗口 (store_path 的 5 号), cb_data 却存着退化事件写进来的日期
    bundle.set("113000.SH", BondTerms(sec_name="有窗口转债",
                                      putback_start_date=date(2026, 8, 7), **common),
               source="unit")
    # B: 没有任何事件, 但 cb_data 存着**完整**窗口 —— 不许动 (见 C 的对照)
    bundle.set("113999.SH", BondTerms(sec_name="无事件完整", 
                                      putback_start_date=date(2026, 8, 5),
                                      putback_end_date=date(2026, 8, 11),
                                      putback_price=100.5, **common),
               source="unit")
    # C: 没有任何事件, cb_data 是退化签名 (有起始日没截止日) —— 该清
    bundle.set("113998.SH", BondTerms(sec_name="无事件退化",
                                      putback_start_date=date(2026, 8, 5), **common),
               source="unit")
    return path


def test_scan_bundle_writes_back_real_windows(store_path, bundle_path, monkeypatch):
    from convertible_bond.cli.repair_putback_windows import scan_bundle
    _patch_fetch(monkeypatch, {})
    repair(store_path, dry_run=False, backup=False, download=True, delay=0)

    diffs = {r["bond_code"]: r for r in scan_bundle(store_path, bundle_path)}
    assert diffs["113000.SH"]["new"]["putback_start_date"] == date(2026, 8, 1)
    assert diffs["113000.SH"]["new"]["putback_end_date"] == date(2026, 8, 7)


def test_missing_events_are_not_evidence_that_the_value_is_wrong(
        store_path, bundle_path, monkeypatch):
    """事件缺席 ≠ 值是错的 —— 源公告可能被兄弟债回洗清掉了。

    实测聚合转债 (111003.SH) / 恒逸转2 (127067.SZ) 的 cb_data 存着完整且合理的窗口,
    而事件表里一条 putback 都没有。按"没有事件就清空"处理会把正确数据一并销毁。
    只有**退化签名** (有起始日、没截止日) 才是错的证据。
    """
    from convertible_bond.cli.repair_putback_windows import scan_bundle
    _patch_fetch(monkeypatch, {})
    repair(store_path, dry_run=False, backup=False, download=True, delay=0)

    diffs = {r["bond_code"]: r for r in scan_bundle(store_path, bundle_path)}
    assert "113999.SH" not in diffs                       # 完整窗口, 不动
    assert diffs["113998.SH"]["new"] == {"putback_start_date": None}   # 退化, 清掉


def test_sync_bundle_applies_both_directions(store_path, bundle_path, monkeypatch):
    from convertible_bond.cache import TermsBundle
    from convertible_bond.cli.repair_putback_windows import sync_bundle
    _patch_fetch(monkeypatch, {})
    repair(store_path, dry_run=False, backup=False, download=True, delay=0)

    before = bundle_path.read_bytes()
    assert sync_bundle(store_path, bundle_path, dry_run=True)["updated"] == 0
    assert bundle_path.read_bytes() == before      # dry-run 不写盘

    report = sync_bundle(store_path, bundle_path, dry_run=False, backup=True)
    assert report["updated"] == 2
    assert report["backup_path"].exists()
    bundle = TermsBundle(bundle_path)
    assert bundle.get("113000.SH").putback_start_date == date(2026, 8, 1)
    assert bundle.get("113998.SH").putback_start_date is None
    assert bundle.get("113999.SH").putback_end_date == date(2026, 8, 11)   # 没被误清


def test_sync_bundle_is_idempotent(store_path, bundle_path, monkeypatch):
    from convertible_bond.cli.repair_putback_windows import sync_bundle
    _patch_fetch(monkeypatch, {})
    repair(store_path, dry_run=False, backup=False, download=True, delay=0)
    sync_bundle(store_path, bundle_path, dry_run=False, backup=False)
    assert sync_bundle(store_path, bundle_path, dry_run=False, backup=False)["updated"] == 0


def test_a_late_supporting_document_cannot_bury_the_real_window():
    """一只债常有几十条 putback 记录, 大量是没有窗口的配套文件。取"最新一条"会让
    晚出的法律意见书盖掉真正的窗口公告 (实测鸿路转债 33 条 putback 记录)。
    """
    from convertible_bond.cb_events import apply_events_to_terms
    from convertible_bond.data_providers import BondTerms
    terms = BondTerms(sec_name="测试转债", underlying_code="600000.SH",
                      conversion_price=10.0, maturity_date=date(2030, 1, 1),
                      listing_date=date(2020, 1, 1))
    events = [
        _ev(5, date(2026, 8, 1), date(2026, 8, 7)),   # 真窗口, 先出
        _ev(9),                                       # 法律意见书, 后出, 没有窗口
    ]
    patched = apply_events_to_terms("113000.SH", terms, events,
                                    valuation_date=date(2026, 8, 20))
    assert patched.putback_start_date == date(2026, 8, 1)
    assert patched.putback_end_date == date(2026, 8, 7)
