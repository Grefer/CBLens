"""数据体检的两条**判据口径** —— 它们各自曾经把真事故报成"通过"。

体检本身是"把碰巧发现的 bug 变成每次都查的检查", 所以判据写窄一格、或方向读反,
后果不是报错而是**报绿**。这个文件只守这两处口径, 不重复各检查的业务逻辑。
"""
from datetime import date

import pytest

from convertible_bond.cache import TermsBundle
from convertible_bond.cli import data_doctor as mod
from convertible_bond.data_providers import BondTerms
from convertible_bond.historical_terms import TermsPatch, TermsPatchStore


# ── 「判死但今日有成交」的覆盖面 ────────────────────────────────────────────────

@pytest.mark.parametrize("reason", [
    "已退市", "已过最后交易日", "已到期", "暂停上市",
    "停牌/暂停交易", "不可交易", "已发行未上市", "违约/异常状态",
    "3 日后可交易",
])
def test_reasons_that_assert_not_trading(reason):
    assert mod._asserts_not_trading(reason)


@pytest.mark.parametrize("reason", [
    "评级过低", "正股 ST/退市风险", "正股停牌", "成交额过低", "余额过小",
    "定向转债/非公开交易标的", "非沪深主板/深市可转债", "",
])
def test_policy_reasons_do_not_assert_not_trading(reason):
    """策略口径的剔除从不声称"这只债不能交易" —— 混进来会让检查天天误报几十只。"""
    assert not mod._asserts_not_trading(reason)


def test_dead_but_trading_catches_newly_listed_bond_marked_untradable(monkeypatch):
    """早期 dead 集只有 {已退市, 已过最后交易日}, 于是上市首日被判成"不可交易"
    /"停牌"的新债从这条检查底下整只漏过去 —— 派克转债当天成交 2.57 亿、中仑转债
    12.95 亿, 检查还报 0。
    """
    import pandas as pd
    monkeypatch.setattr("akshare.bond_zh_hs_cov_spot", lambda *a, **k: pd.DataFrame({
        "symbol": ["sh111026", "sz123281", "sz123999"],
        "trade": [155.721, 153.710, 88.0],
        "volume": [1_649_410, 8_244_638, 0],      # 第三只零成交 = 陈旧行, 不算
    }))
    bundle = TermsBundle.__new__(TermsBundle)
    bundle.get = lambda code: BondTerms(sec_name={"111026.SH": "派克转债",
                                                  "123281.SZ": "中仑转债"}.get(code, ""))

    check = mod.check_dead_but_trading({
        "online": True,
        "bundle": bundle,
        "today": date(2026, 8, 25),
        "excluded": {
            "111026.SH": "停牌/暂停交易",
            "123281.SZ": "不可交易",
            "123999.SZ": "已退市",        # 零成交 → 不该报
            "128044.SZ": "评级过低",       # 策略口径 → 不该报
        },
    })
    assert check.status == mod.FAIL
    assert len(check.extra) == 2
    assert any("111026.SH" in row for row in check.extra)
    assert any("123281.SZ" in row for row in check.extra)


def test_dead_but_trading_tolerates_the_session_boundary(monkeypatch):
    """刚停止交易的债不算"判死却仍在成交" —— 那笔成交正是它自己最后一个交易日的。

    akshare 现货表在收盘后仍留着上一交易日的行情 (ticktime 只有时分秒、没有日期), 而
    market_today() 按 Asia/Shanghai 走: 在美西运行时本机上午已是上海次日凌晨。实测
    春23转债 (最后交易日 2026-08-25 当天成交 453 万手, 08-31 摘牌) 被这么误报过。
    """
    import pandas as pd
    monkeypatch.setattr("akshare.bond_zh_hs_cov_spot", lambda *a, **k: pd.DataFrame({
        "symbol": ["sh113667", "sh113610"],
        "trade": [181.029, 120.394],
        "volume": [4_537_630, 262_360],
    }))
    terms = {
        "113667.SH": BondTerms(sec_name="春23转债", last_trading_date=date(2026, 8, 25),
                               delisting_date=date(2026, 8, 31)),
        "113610.SH": BondTerms(sec_name="灵康转债", last_trading_date=date(2024, 12, 12),
                               delisting_date=date(2024, 12, 20)),
    }
    bundle = TermsBundle.__new__(TermsBundle)
    bundle.get = terms.get

    check = mod.check_dead_but_trading({
        "online": True,
        "bundle": bundle,
        "today": date(2026, 8, 26),
        "excluded": {"113667.SH": "已过最后交易日", "113610.SH": "已退市"},
    })
    assert check.status == mod.FAIL
    assert len(check.extra) == 1 and "113610.SH" in check.extra[0]   # 死了一年多的才算
    assert "另有 1 只刚停止交易" in check.detail


# ── 「公告评级 vs cb_data」的方向 ────────────────────────────────────────────

def _rating_ctx(tmp_path, current: str, announced: str):
    bundle = TermsBundle(tmp_path / "cb_data.json")
    bundle.set("123157.SZ", BondTerms(sec_name="科蓝转债", credit_rating=current),
               source="unit")
    store = TermsPatchStore(tmp_path / "patches.json")
    store.add_many([TermsPatch(bond_code="123157.SZ", effective_date=date(2026, 6, 24),
                               fields={"credit_rating": announced}, source="cninfo")])
    return {"bundle": bundle, "patch_store": store}


def test_rating_divergence_reports_both_directions_without_blaming(tmp_path):
    """这条检查的方向翻过两次, 都是因为"cb_data 的评级从哪来"变了。

    cb-sync-ratings 落地后 cb_data 走第三方**当前值**, 而公告侧的 rating_re 左界 bug 会
    系统性把 AA 抠成 A —— 实测拿第三方逐条当裁判, 17 条分歧里 **15 条是公告错**。
    所以它只报分歧率, 不再断言"cb_data 未跟进"。
    """
    lower = mod.check_rating_divergence(_rating_ctx(tmp_path, "AA-", "A-"))
    assert "分歧" in lower.detail and "公告更低 1" in lower.detail
    assert "未跟进" not in lower.detail and "未跟进" not in lower.because
    # 裁判是第三方, 不是这条检查本身
    assert "评级同步水位" in lower.because


def test_rating_divergence_stays_silent_when_they_agree(tmp_path):
    same = mod.check_rating_divergence(_rating_ctx(tmp_path, "AA-", "AA-"))
    assert same.status == mod.OK
    assert same.extra == []


# ── 「已摘牌」的判据 ────────────────────────────────────────────────────────

def test_looks_delisted_needs_the_date_to_have_passed():
    """判据是**日期已过**, 不是"有没有这个字段"。

    曾写成 ``delisting_date is not None``。当时全库只有 17 只有摘牌日, 没问题;
    2026-08-22 全库回填 (17 → 1041) 之后, 几乎每只在市债都带着一个**未来的**到期摘牌日,
    于是「末条 patch == 当前值」跳过 952/958 (99%) 条链、只真检查 6 只, 藏着 30 只不符。
    """
    today = date(2026, 8, 25)
    live = BondTerms(sec_name="鸿路转债", delisting_date=date(2032, 10, 9))
    assert not mod._looks_delisted(live, today)

    gone = BondTerms(sec_name="万孚转债", delisting_date=date(2026, 9, 1))
    assert not mod._looks_delisted(gone, today)          # 还没到
    assert mod._looks_delisted(gone, date(2026, 9, 2))   # 过了

    assert mod._looks_delisted(BondTerms(sec_name="格力转债(退市)"), today)
    assert mod._looks_delisted(
        BondTerms(sec_name="春23转债", last_trading_date=date(2026, 8, 24)), today)
    assert not mod._looks_delisted(
        BondTerms(sec_name="春23转债", last_trading_date=today), today)  # 今天还能交易


# ── 反向的外部对照: 在池却查无行情 ──────────────────────────────────────────

def test_pool_without_quotes_catches_a_bond_that_never_listed(monkeypatch):
    """「判死但今日有成交」查误杀; 这条查**误留**。

    实测抓出 123095.SZ 日升转债: 2021 年撤销发行、从未上市, 却带着完整条款和 AA 评级
    在主池里被定出 −14% 低估 —— 库内每一项自洽性指标都正常, 因为它的条款确实一应俱全,
    只是那个市场从未存在过。
    """
    import pandas as pd
    monkeypatch.setattr("akshare.bond_zh_hs_cov_spot", lambda *a, **k: pd.DataFrame({
        "symbol": ["sh113052", "sz123096"],
        "volume": [1_000_000, 0],
    }))
    terms = {
        "113052.SH": BondTerms(sec_name="兴业转债", listing_date=date(2022, 1, 7)),
        "123095.SZ": BondTerms(sec_name="日升转债", listing_date=None),
        "118076.SH": BondTerms(sec_name="先锋转债", listing_date=date(2026, 8, 26)),
    }
    bundle = TermsBundle.__new__(TermsBundle)
    bundle.get = terms.get

    check = mod.check_pool_without_quotes({
        "online": True, "bundle": bundle, "today": date(2026, 8, 26),
        "pool": ["113052.SH", "123095.SZ", "118076.SH"],
    })
    assert check.status == mod.FAIL
    assert len(check.extra) == 1 and "123095.SZ" in check.extra[0]
    assert "另有 1 只刚挂牌" in check.detail      # 今天上市的不算


def test_pool_without_quotes_does_not_flag_bonds_that_have_not_listed_yet(monkeypatch):
    """**还没挂牌的新债不是幽灵** —— 现货表里本来就不该有它们。

    准入层 2026-08-31 起放在途新债进主池, 而这条检查的宽限原先只认"上市日已过 ≤3 天"
    (``listing is not None and (today - listing).days <= 3``)。于是 ``listing_date`` 还是
    None 的在途新债 (实测丰茂/强达两只) 每天被报成幽灵, 把日升转债那种**真**幽灵淹掉。

    两档在途新债都要放过: 上市日未定, 以及上市日已公告但还没到。
    """
    import pandas as pd
    monkeypatch.setattr("akshare.bond_zh_hs_cov_spot", lambda *a, **k: pd.DataFrame({
        "symbol": ["sh113052"], "volume": [1_000_000],
    }))
    terms = {
        "113052.SH": BondTerms(sec_name="兴业转债", listing_date=date(2022, 1, 7)),
        "123283.SZ": BondTerms(sec_name="丰茂转债", issue_date=date(2026, 8, 18),
                               listing_date=None, is_tradable=False,
                               trading_status="pending"),
        "123282.SZ": BondTerms(sec_name="震裕转02", issue_date=date(2026, 8, 17),
                               listing_date=date(2026, 9, 2), is_tradable=False,
                               trading_status="pending"),
        "123095.SZ": BondTerms(sec_name="日升转债", listing_date=None),
    }
    bundle = TermsBundle.__new__(TermsBundle)
    bundle.get = terms.get

    check = mod.check_pool_without_quotes({
        "online": True, "bundle": bundle, "today": date(2026, 8, 31),
        "pool": ["113052.SH", "123283.SZ", "123282.SZ", "123095.SZ"],
    })
    # 只剩真幽灵 —— 两只在途新债不进 extra
    assert len(check.extra) == 1 and "123095.SZ" in check.extra[0]
    assert "2 只尚未挂牌" in check.detail


def test_patch_checks_read_the_raw_file_not_the_effective_view(tmp_path):
    """体检一律看**文件里到底有什么**, 不看被权威源遮蔽后的生效视图。

    ``list_patches()`` 自己的 docstring 就写了这条: "数据体检与存量回洗要的是文件里
    到底有什么, 传 include_shadowed=True —— 否则一条被 Wind 遮蔽的脏 patch 会既扫不到、
    也删不掉, 等哪天权威源覆盖收窄就原地复活"。三处 patch 组检查此前都用了默认视图,
    于是对被遮蔽的那部分是瞎的 (实测盘上 4 条)。
    """
    from datetime import date

    from convertible_bond.cli import data_doctor as dd
    from convertible_bond.historical_terms import TermsPatch, TermsPatchStore

    store = TermsPatchStore(tmp_path / "patches.json")
    store.add_many([
        TermsPatch(bond_code="A.SZ", effective_date=date(2026, 1, 1),
                   fields={"conversion_price": 10.0}, source="wind_asof"),
        # 被上面那条逐字段遮蔽 —— 但它就在文件里, 体检必须看得见
        TermsPatch(bond_code="A.SZ", effective_date=date(2026, 2, 1),
                   fields={"conversion_price": 999.0}, source="cninfo"),
    ])
    assert len(store.list_patches()) == 1, "前提不成立: 那条并没有被遮蔽"

    seen = dd._patches_by_field(store, "conversion_price")["A.SZ"]
    assert len(seen) == 2, f"体检只看到 {len(seen)} 条, 遮蔽掉的那条没进来"
    assert any(p.source == "cninfo" for p in seen)


def test_event_coverage_flags_a_year_with_zero_events():
    """**整年 0 条**必须算缺口 —— 那正是这条检查存在的理由。

    ``by_year`` 是从事件表自己的键 build 出来的, 所以一个零事件的年份**压根不在里面**,
    ``by_year[y] < 50`` 永远看不到它。检查的 extra 文案写的就是 "2024 年之前全库 0 条
    事件", 而那种情况恰好是它唯一漏掉的。
    """
    from convertible_bond.cli import data_doctor as dd

    events = ([{"event_date": "2023-05-01"}] * 60) + ([{"event_date": "2025-05-01"}] * 60)
    check = dd.check_event_time_coverage({"events": events})
    assert "2024" in " ".join(check.extra), f"零事件的 2024 没被标成缺口: {check.extra}"
    assert check.status != dd.OK


def test_daily_field_coverage_is_measured_on_the_pool_not_the_whole_library():
    """每日刷新字段的覆盖率必须**按主池**量。

    档案库里留着退市券上一次同步时的存量值 (``merge_admission_status`` 有 None 保护,
    不会被清), 按全库量它们会把停摆整个盖住 —— 实测 ``underlying_pct_change``
    全库 702/1059 (66%, 看着正常) 而**主池 0/311**, 于是「正股跌停」这个标签从来没
    亮过, 没有异常、没有红测试, 只是一个接在恒空输入上的检测器。
    """
    from convertible_bond.cli import data_doctor as dd

    class _Bundle:
        """主池全空、库里其余的债都有值 —— 正是真实盘上的形状。"""

        def __init__(self):
            self._pool = {f"P{i}.SZ" for i in range(10)}
            self._dead = {f"D{i}.SZ" for i in range(90)}

        def list_bonds(self):
            return sorted(self._pool | self._dead)

        def get(self, code):
            value = None if code in self._pool else 3.5
            return type("T", (), {"underlying_pct_change": value,
                                  "underlying_trade_status": "交易",
                                  "underlying_status": "否"})()

    bundle = _Bundle()
    ctx = {"bundle": bundle, "pool": sorted(bundle._pool), "today": None}
    by_name = {c.name: c for c in dd.check_live_pool_daily_coverage(ctx)}
    pct = by_name["主池每日字段 · underlying_pct_change"]
    assert pct.status == dd.FAIL, f"主池全空却没报警: {pct.detail}"
    assert "0/10" in pct.detail

    # 对照: 同一份数据按**全库**量是 90/100, 完全看不出问题 —— 这就是为什么要分开量
    whole = sum(1 for c in bundle.list_bonds()
                if bundle.get(c).underlying_pct_change is not None)
    assert whole / len(bundle.list_bonds()) == 0.9

    # 其余两个字段有值时不该跟着报警
    assert by_name["主池每日字段 · underlying_status"].status == dd.OK


def test_limit_down_tag_is_covered_by_the_sensitivity_grouping():
    """每个可交易性标签都要真的映射到「条款/流动性敏感」—— 逐个**跑**, 不扫源码.

    上一版扫的是 ``inspect.getsource(_sensitivity_status)`` 里有没有那个字面量,
    而 ``getsource`` **连注释一起返回**: 把「转债停牌」从活的集合里删掉、在后面留一行
    ``# "转债停牌"``, 这条用例和整套 1118 条全绿 —— 而一只停牌的转债 (最强的
    可交易性信号) 会静默掉进按置信度分档的兜底路径。
    """
    from convertible_bond.batch_pricing import TRADABILITY_RISK_TAGS, _sensitivity_status

    wrong = {t: _sensitivity_status([t], "高") for t in TRADABILITY_RISK_TAGS
             if _sensitivity_status([t], "高") != "条款/流动性敏感"}
    assert not wrong, f"可交易性标签没进条款/流动性敏感这一档: {wrong}"
    # 置信度不该把它翻掉 —— 这一档是标的事实, 不是模型信心
    assert _sensitivity_status(["正股跌停"], "低") == "条款/流动性敏感"


def test_rebuild_dry_run_previews_the_same_population_it_will_delete(tmp_path):
    """``--dry-run`` 报告的删除范围必须和 ``--apply`` 真删的是同一批。

    ``TermsPatchStore.rewrite`` 遍历的是 ``self._patches`` (原始文件), 而
    ``list_patches()`` 默认返回被权威源逐字段遮蔽后的视图 —— 于是被遮蔽的解析 patch
    **没出现在操作者审过的报告里就被删掉了**。实测 conversion_price: 预览 4424 条 /
    实删 4426 条。
    """
    import inspect

    from convertible_bond.cli import rebuild_terms_patches as mod

    src = inspect.getsource(mod)
    idx = src.index("dropped = [p for p in store.list_patches")
    line = src[idx:src.index("\n", idx)]
    assert "include_shadowed=True" in line, f"预览仍用生效视图: {line.strip()}"

    # 行为侧: 造一条被遮蔽的 patch, 它必须出现在预览里
    from datetime import date

    from convertible_bond.historical_terms import TermsPatch, TermsPatchStore

    store = TermsPatchStore(tmp_path / "p.json")
    store.add_many([
        TermsPatch(bond_code="A.SZ", effective_date=date(2026, 1, 1),
                   fields={"conversion_price": 10.0}, source="wind_asof"),
        TermsPatch(bond_code="A.SZ", effective_date=date(2026, 2, 1),
                   fields={"conversion_price": 99.0}, source="cninfo"),
    ])
    assert len(store.list_patches()) == 1, "前提不成立: 并没有被遮蔽"
    assert len(store.list_patches(include_shadowed=True)) == 2


def test_every_cli_reads_only_arguments_its_parser_registers():
    """``args.X`` 必须有对应的 ``add_argument`` —— 否则 ``main()`` 一跑就 AttributeError。

    实测事故: ``--pde-sigma-band`` / ``--pde-spread-band`` 随「下修优势」一起从 parser
    删掉了, 但两个消费者留在原地, 于是 ``cb-strategy-backtest`` **每次调用都在取数之前
    崩掉**。``--help`` 恰好走 ``parse_args`` 的提前退出所以看不出来, 而套件里没有任何
    用例调 ``main()`` —— 一个 README 里写在每日流程上的命令就这么死着。

    这条守护扫的是**整类**问题, 不是那两个名字: 静态比对每个 CLI 模块里
    ``add_argument`` 注册的 dest 与 ``args.<attr>`` 的读取。位置参数 (``add_argument
    ("codes", nargs="*")``) 也算注册 —— 第一版漏了它, 把 ``sync_terms`` 误报成 bug。
    """
    import ast
    from pathlib import Path

    cli_dir = Path(__file__).resolve().parent.parent / "convertible_bond" / "cli"
    problems: dict[str, dict[str, int]] = {}
    for path in sorted(cli_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        dests: set[str] = set()
        reads: dict[str, int] = {}
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "add_argument"):
                explicit = next(
                    (kw.value.value for kw in node.keywords
                     if kw.arg == "dest" and isinstance(kw.value, ast.Constant)), None)
                if explicit:
                    dests.add(explicit)
                    continue
                for arg in node.args:
                    if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
                        continue
                    if arg.value.startswith("--"):
                        dests.add(arg.value[2:].replace("-", "_"))
                    elif not arg.value.startswith("-"):
                        dests.add(arg.value.replace("-", "_"))   # 位置参数
            if (isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name) and node.value.id == "args"):
                reads.setdefault(node.attr, node.lineno)
        if not dests:
            continue
        missing = {k: v for k, v in reads.items() if k not in dests}
        if missing:
            problems[path.name] = missing
    assert not problems, f"CLI 读了未注册的参数: {problems}"


def test_strategy_cli_main_survives_argument_handling():
    """``cb-strategy-backtest`` 必须能走完参数处理。

    上面那条是静态的; 这条真的调一次 ``main()``, 停在"没给 CSV 根目录"这个**正确**的
    错误上 —— 只要参数处理段有孤儿读取, 它就会先抛 AttributeError。不联网: CSV 源在
    build provider 时就失败了。
    """
    import sys

    import convertible_bond.cli.strategy_backtest as mod

    argv = sys.argv
    sys.argv = ["cb-strategy-backtest", "--start", "2024-01-01", "--end", "2024-03-01",
                "--freq", "M", "--source", "csv"]
    try:
        with pytest.raises((RuntimeError, SystemExit, ValueError)) as excinfo:
            mod.main()
    finally:
        sys.argv = argv
    assert not isinstance(excinfo.value, AttributeError)
    assert "pde_" not in str(excinfo.value)


def test_strategy_cli_summary_prints_the_normalized_rank_signal():
    """摘要要打**归一化后**的排序信号。

    ``--rank-signal down_reset_edge`` 这类已删除的值仍被 choices 接受 (向后兼容), 但
    ``_normalize_rank_signal`` 会把它们落到 ``deviation``。照原样打印会让屏幕上写着
    「策略信号: down_reset_edge」而引擎实际按估值偏差排序 —— 结果对不上解释。
    """
    import ast
    import inspect

    import convertible_bond.cli.strategy_backtest as mod

    src = inspect.getsource(mod)
    tree = ast.parse(src)
    raw_reads = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        for value in node.values:
            if not isinstance(value, ast.FormattedValue):
                continue
            seg = ast.get_source_segment(src, value.value) or ""
            if seg.strip() == "strategy_config.rank_signal":
                raw_reads.append(node.lineno)
    assert not raw_reads, f"摘要打印了未归一化的 rank_signal, 第 {raw_reads} 行"
    assert "_normalize_rank_signal(strategy_config.rank_signal)" in src


def test_disk_cache_identity_change_wipes_the_store_files(tmp_path):
    """身份变更必须**立刻落到盘上**, 不能只清内存。

    ``flush()`` 只重写 dirty 的那几个 store, 却无条件把 ``_meta.json`` 盖成新身份 ——
    一次只弄脏了一部分的运行 (例如在准入阶段就中断, 那时只取过条款、还没碰行情) 会留下
    旧身份的 bond_history.json / stock_history.json 顶着新身份的 meta。下一次启动看到
    ``stored == identity``, 就把那些文件当成新身份的缓存读回来了。
    """
    from datetime import date

    from convertible_bond.backtest_disk_cache import DiskCacheProvider
    from convertible_bond.data_providers.base import DataProvider

    class _Inner(DataProvider):
        name = "i"

        def __init__(self, value):
            self.value = value

        def get_bond_history(self, code, a, b):
            return [(date(2024, 6, 3), self.value)]

        def get_stock_history(self, code, a, b):
            return [(date(2024, 6, 3), self.value)]

        def get_bond_terms(self, code, d):
            return None

        def get_stock_close(self, *a, **k):
            return None

        def hist_vol(self, *a, **k):
            return 0.2

        def get_risk_free_rate(self, *a, **k):
            return 0.022

    start, end = date(2024, 1, 1), date(2024, 6, 28)

    first = DiskCacheProvider(_Inner(100.0), cache_dir=tmp_path, namespace="ID-A")
    first.get_bond_history("X.SH", start, end)
    first.get_stock_history("Y.SZ", start, end)
    first.flush()
    assert (tmp_path / "bond_history.json").exists()

    # 换身份 → 旧 store 文件当场消失, 不等 flush
    DiskCacheProvider(_Inner(200.0), cache_dir=tmp_path, namespace="ID-B")
    assert not (tmp_path / "bond_history.json").exists(), "旧身份的行情缓存还留在盘上"
    assert not (tmp_path / "stock_history.json").exists()

    # 以新身份重新启动必须真的回源
    third = DiskCacheProvider(_Inner(999.0), cache_dir=tmp_path, namespace="ID-B")
    assert third.get_bond_history("X.SH", start, end) == [(date(2024, 6, 3), 999.0)]


def test_csv_root_is_part_of_the_disk_cache_identity(tmp_path):
    """``--csv-root`` 必须进缓存身份。

    ``CSVDataProvider`` 把数据根目录存在 ``self.root``, 而身份扫描的属性名单里没有它,
    嵌套递归也够不到 —— 两次指向**不同数据集**、共用同一个 ``--cache-dir`` 的 csv 回测
    会算出一模一样的身份串, 第二次直接把第一次的缓存当有效。

    修法里有个坑要一并钉住: 路径解析必须**先判"它本身就是路径"**。原来的顺序把
    isinstance 放最后, 于是一个 ``Path`` 对象先命中 ``Path.root`` 这个属性 (返回 "/"),
    所有数据集又都得到同一个身份 ``root:/``。
    """
    from convertible_bond.backtest_disk_cache import _provider_identity
    from convertible_bond.data_providers import CSVDataProvider

    a, b = tmp_path / "ds_a", tmp_path / "ds_b"
    for root in (a, b):
        for sub in ("bonds", "stocks", "terms"):
            (root / sub).mkdir(parents=True, exist_ok=True)

    ident_a = _provider_identity(CSVDataProvider(a))
    ident_b = _provider_identity(CSVDataProvider(b))
    assert ident_a != ident_b, f"两个数据集身份相同: {ident_a}"
    assert ident_a.endswith(str(a)) or str(a) in ident_a, f"根目录没进身份: {ident_a}"
    assert "root:/:" not in ident_a, "命中了 Path.root, 解析顺序又反了"
    assert _provider_identity(CSVDataProvider(a)) == ident_a       # 同数据集稳定


def test_cninfo_pagination_truncation_is_loud():
    """翻页没翻完必须抛, 不能静默返回半截列表。

    三种停止方式此前都被当成"取完了": ① 第一页之后任何一页失败; ② 响应缺
    ``totalAnnouncement`` (默认 0 让 ``page*size >= 0`` 立刻为真, 一页就停);
    ③ ``max_pages × page_size`` 用尽而总数更多 (600 条公告只取回 300, 零日志)。
    而 ``sync_cb_events`` 对三种一视同仁: 不记 failed、照常 ``mark_synced`` ——
    **把水位推过它从没看见的公告**, 那些公告之后永远不会再被拉取。
    """
    from convertible_bond.cninfo_provider import (
        CninfoAnnouncementProvider,
        IncompleteAnnouncementList,
    )

    class _Resp:
        def __init__(self, body):
            self._body = body
            self.status_code = 200

        def json(self):
            return self._body

    def _poster(total, *, fail_page=None, omit_total=False, page_size=30):
        def post(url, data=None, timeout=None):
            page = int(data["pageNum"])
            if fail_page and page == fail_page:
                raise RuntimeError("网络抖动")
            lo = (page - 1) * page_size
            anns = [{"announcementTitle": f"t{i}",
                     "announcementTime": 1700000000000 + i,
                     "adjunctUrl": f"u{i}"} for i in range(lo, min(lo + page_size, total))]
            body = {"announcements": anns}
            if not omit_total:
                body["totalAnnouncement"] = total
            return _Resp(body)
        return post

    def _run(**kw):
        p = object.__new__(CninfoAnnouncementProvider)
        p._session = type("S", (), {"post": staticmethod(_poster(**kw))})()
        p._timeout = 5
        p._page_size = 30
        p._max_pages = 10
        p._request_interval = 0
        p._throttle = lambda: None
        return p._query_pages(stock="x", se_date="a~b", column="c", category="d")

    for label, kw in (("第 2 页失败", dict(total=100, fail_page=2)),
                      ("缺 totalAnnouncement", dict(total=100, omit_total=True)),
                      ("max_pages 用尽", dict(total=600))):
        with pytest.raises(IncompleteAnnouncementList) as exc:
            _run(**kw)
        assert exc.value.rows, f"{label}: 已取到的部分被丢了"

    # 健康路径不受影响
    assert len(_run(total=60)) == 60
    # 第一页就失败仍是彻底失败, 不是"部分"
    with pytest.raises(RuntimeError) as exc:
        _run(total=100, fail_page=1)
    assert not isinstance(exc.value, IncompleteAnnouncementList)


def test_partial_fetch_does_not_advance_the_sync_watermark():
    """部分取到的债不许进 ``synced_codes``。

    水位一旦推过去, 没看见的公告之后**永远不会再被拉取**, 而这是静默的。
    但已取到的部分照常解析 —— 它们是真的公告。
    """
    import inspect

    from convertible_bond import cb_event_sync

    src = inspect.getsource(cb_event_sync.sync_cb_events)
    assert "IncompleteAnnouncementList" in src, "同步侧没有区分部分取到"
    assert "incomplete_codes" in src
    assert "code not in incomplete_codes" in src, "部分取到的债仍在推水位"
    # 部分与彻底失败要分开报, 否则日志把两种状况混在一起
    assert '"partial": partial' in src
