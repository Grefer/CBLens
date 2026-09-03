"""包级公开 API 的守护 (``convertible_bond/__init__.py``).

``__init__.py`` 从饥饿导入改成 PEP 562 惰性导入 (2026-09-03) 之后, 有两件事原先是
**免费**在做的, 现在必须靠这里补回来:

① 饥饿导入会在 ``import`` 那一刻校验每个名字都真的解析得出来。惰性之后, 一个搬了家
   或拼错的名字要等到第一次访问才炸 —— 而"第一次访问"可能发生在用户手里。
② ``__all__`` 是对外承诺。仓库外的调用方看不见, 静默少一个名字就是无声的破坏性变更。

所以下面的名单是**字面量**, 不从 ``convertible_bond.__all__`` 或 ``_EXPORTS`` 推导 ——
从被测常量推导出来的期望值恒真, 那是一条绿着的空守护。
"""
from __future__ import annotations

import subprocess
import sys

import convertible_bond

# 2026-09-03 惰性化当天冻结的 95 个公开名。少一个 = 破坏性变更, 要在这里显式改。
PUBLIC_NAMES = [
    "__version__",
    "UniversalCBPricer",
    "DEFAULT_COUPON_RATES",
    "DEFAULT_FACE_VALUE",
    "DEFAULT_REDEMPTION_PRICE",
    "price_from_provider",
    "price_from_wind",
    "price_from_auto",
    "batch_price_from_provider",
    "batch_price_from_provider_threaded",
    "AdmissionFilterConfig",
    "AdmissionFilterResult",
    "BATCH_RESULT_COLUMNS",
    "build_batch_provider",
    "list_upcoming_tradable_from_cache",
    "list_batch_codes_from_cache",
    "load_batch_results_cache",
    "merge_upcoming_pricing_results",
    "parse_bond_codes",
    "project_batch_cache_path",
    "save_batch_results_cache",
    "screen_batch_pool_from_cache",
    "summarize_batch_results",
    "summarize_exclusions",
    "write_batch_results_csv",
    "backtest_theoretical_price",
    "PDEStrategyConfig",
    "ScoreStrategyConfig",
    "backtest_pde_strategy",
    "backtest_score_strategy",
    "build_rebalance_schedule",
    "write_strategy_backtest_csv",
    "HistoricalBondDataProvider",
    "TermsHistoryStore",
    "TermsPatch",
    "TermsPatchStore",
    "default_terms_patch_store",
    "project_terms",
    "project_terms_history_dir",
    "project_terms_patches_path",
    "reload_default_terms_patch_store",
    "strip_current_status_fields",
    "DataProvider",
    "BondTerms",
    "CashflowSchedule",
    "WindDataProvider",
    "AkshareDataProvider",
    "CSVDataProvider",
    "CninfoAnnouncementProvider",
    "extract_text_from_pdf_bytes",
    "auto_data_provider",
    "detect_available_providers",
    "TermsCache",
    "TermsBundle",
    "CachedBondDataProvider",
    "CachingDataProvider",
    "filter_listed_codes",
    "is_terminal_terms",
    "refresh_one",
    "sync_cb_data",
    "sync_cb_terms",
    "ADMISSION_STATUS_FIELDS",
    "changed_admission_fields",
    "merge_admission_status",
    "refresh_admission_status",
    "refresh_admission_status_from_store",
    "CBEvent",
    "CBEventStore",
    "apply_events_to_terms",
    "classify_announcement_title",
    "default_event_store",
    "parse_event_from_announcement",
    "parse_call_redemption_dates",
    "parse_conversion_suspension_terms",
    "parse_down_reset_new_price",
    "parse_putback_terms",
    "project_events_path",
    "apply_events_to_bundle",
    "parse_credit_rating_change",
    "parse_credit_rating_terms",
    "parse_conversion_price_adjustment",
    "parse_outstanding_balance_change",
    "parse_terms_patch_from_announcement",
    "sync_cb_events",
    "project_bundle_path",
    "seed_data_files",
    "data_path",
    "data_dir",
    "app_data_dir",
    "asset_path",
    "project_root",
    "is_frozen_app",
    "APP_NAME",
    "to_date",
    "parse_coupon_string",]


def test_public_name_set_did_not_silently_shrink() -> None:
    """``__all__`` 是对外承诺, 少一个名字就是破坏性变更。"""
    assert len(PUBLIC_NAMES) == 95
    exported = set(convertible_bond.__all__)
    missing = sorted(set(PUBLIC_NAMES) - exported)
    assert not missing, f"公开 API 少了这些名字, 是破坏性变更: {missing}"


def test_every_public_name_actually_resolves() -> None:
    """惰性导入把"名字解析得出来吗"推迟到了访问时, 这里把那次校验补回来。

    饥饿导入时代它是白送的; 现在没有这条, 一个搬了家的符号会一路绿灯到用户手里。
    """
    broken = []
    for name in PUBLIC_NAMES:
        try:
            getattr(convertible_bond, name)
        except Exception as exc:  # noqa: BLE001 — 要把失败原因原样报出来
            broken.append(f"{name}: {type(exc).__name__}: {exc}")
    assert not broken, "这些公开名解析不出来: " + "; ".join(broken)


def test_unknown_attribute_still_raises_attribute_error() -> None:
    """``__getattr__`` 兜底不能把拼错的名字变成别的异常 (``hasattr`` 会被打乱)。"""
    try:
        convertible_bond.definitely_not_an_export
    except AttributeError:
        pass
    else:
        raise AssertionError("未知属性没有抛 AttributeError")
    assert not hasattr(convertible_bond, "definitely_not_an_export")


def test_dir_lists_the_public_names_before_they_are_touched() -> None:
    """没有 ``__dir__``, 补全结果会随"这个进程碰过什么"变化 —— 所以另起进程验。"""
    code = (
        "import convertible_bond as cb;"
        "names = set(dir(cb));"
        "print(int({'UniversalCBPricer', 'sync_cb_events', 'TermsBundle'} <= names))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert out == "1", f"dir() 没有列出未被访问过的公开名: {out}"


def test_importing_the_package_does_not_drag_in_scipy() -> None:
    """惰性导入的**行为**判据: 只 import 包本身, 重依赖不许被拉起来。

    2026-09-03 实测: 饥饿导入时 ``import convertible_bond`` 要 0.39s (裸解释器 0.02s),
    而 ``cb-sync-new-issues`` —— AGENTS 说它"秒级, 不需要 Wind" —— 冷启动 0.35s 里
    scipy/requests 全是白拉的, 它一个都用不上。惰性之后 0.06s。

    这条断言的是模块图不是耗时: 耗时会随机器抖, 而"scipy 在不在 sys.modules 里"
    是确定的, 且饥饿导入一回来它当场变红。
    """
    code = (
        "import sys, convertible_bond;"
        "heavy = sorted(m for m in ('scipy', 'requests') if m in sys.modules);"
        "print(','.join(heavy))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert out == "", f"import convertible_bond 饥饿拉起了重依赖: {out}"


def test_submodule_import_still_works_without_being_re_exported() -> None:
    """全仓 37 处包级导入都是 ``from convertible_bond import <子模块>`` 这个形式。

    它由导入机制解决, 不经过 ``_EXPORTS`` —— 但删掉饥饿导入很容易让人以为它坏了,
    所以显式钉一条; 顺带钉住"子模块不必登记在 ``__all__`` 里"。
    """
    from convertible_bond import market_time, watchlist

    assert market_time.__name__ == "convertible_bond.market_time"
    assert watchlist.__name__ == "convertible_bond.watchlist"
    assert "market_time" not in convertible_bond.__all__


def test_desktop_bundle_still_reaches_the_modules_the_eager_import_used_to_carry() -> None:
    """饥饿导入曾是桌面包的一张**白捡的**安全网, 惰性化把它撤掉了。

    PyInstaller 走静态分析, 看不见 ``_EXPORTS`` 里的字符串。实测 (2026-09-03) 以
    ``gui.py`` 为唯一种子时, 顶层子模块的静态可达集从 **29 掉到 27** —— 掉的正是
    ``admission_status`` 与 ``cb_data_sync``: 此前它们**只**经由 ``__init__.py`` 的
    ``from .admission_status import ...`` 才够得着。

    它们今天仍在包里, 靠的是 spec 把 4 个 ``cli.*`` 列进了 hiddenimports (那半条链由
    ``test_build_desktop.py`` 钉着), 加上这两个 CLI 各自 import 了它们 —— 而**这半条**
    此前没有任何东西钉。断了它, 表现是冻结包运行到「🌐 同步池」才 ModuleNotFoundError,
    源码跑和整套测试都是绿的。
    """
    import ast
    from pathlib import Path

    from convertible_bond.cli import POOL_SYNC_MODULES

    # (曾经只靠饥饿导入才可达的模块, 现在负责把它带进包的那个 CLI)
    carried_by = {
        "admission_status": "convertible_bond.cli.sync_admission_status",
        "cb_data_sync": "convertible_bond.cli.sync_tradable",
    }
    root = Path(__file__).resolve().parent.parent
    for module, cli_module in carried_by.items():
        assert cli_module in POOL_SYNC_MODULES, (
            f"{cli_module} 不在 POOL_SYNC_MODULES 里, 它不会进桌面包的 hiddenimports"
        )
        src = (root / cli_module.replace(".", "/")).with_suffix(".py").read_text()
        imported = {
            node.module
            for node in ast.walk(ast.parse(src))
            if isinstance(node, ast.ImportFrom)
        }
        assert module in imported, (
            f"{cli_module} 不再 import {module} 了; 惰性化之后没有别的静态路径"
            f"把 convertible_bond.{module} 带进桌面包"
        )


def test_submodule_attribute_works_on_a_bare_import() -> None:
    """``import convertible_bond`` 之后 ``convertible_bond.pricer`` 必须直接取得到。

    饥饿导入时代这是白送的 —— 那 14 条 ``from .X import ...`` 顺带把 ``X`` 挂在了包上。
    惰性化后它一度变成"取决于这个进程碰过什么" (2026-09-03 实测: 裸 import 之后
    ``cb.pricer`` 抛 AttributeError, 而先碰一下 ``cb.UniversalCBPricer`` 它又出现了),
    与这个文件里 ``__dir__`` 那条注释说的是同一种病。

    **必须另起进程**: 同一个 pytest 进程里别的用例早就把这些子模块 import 过了,
    在这里 ``getattr`` 一定成功 —— 那是一条测不到东西的绿守护。
    """
    code = (
        "import convertible_bond as cb;"
        # pricer 走 _EXPORTS (有导出名), market_time 完全不在 _EXPORTS 里, 两条路都要覆盖
        "print(type(cb.pricer).__name__, type(cb.market_time).__name__)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert proc.returncode == 0, f"裸 import 后取子模块失败:\n{proc.stderr}"
    assert proc.stdout.strip() == "module module", proc.stdout


def test_submodule_fallback_does_not_defeat_the_lazy_import() -> None:
    """子模块回落只在**取到那个名字**时才 import, 不许把包变回饥饿。

    上一条 (``..._works_on_a_bare_import``) 只证明回落**够用**; 最省事的"够用"写法是
    在 ``__getattr__`` 里把整个包扫一遍或干脆预 import, 那会把刚省下的 0.37s 原样赔回去。
    所以这里另钉一条: 探一个**不存在**的名字 (``hasattr`` 就是这个形状) 之后, 包仍旧
    干净 —— 既没把它判成存在, 也没拉起 scipy/requests。
    """
    code = (
        "import sys, convertible_bond as cb;"
        "probe = hasattr(cb, 'definitely_not_a_submodule');"
        "heavy = sorted(m for m in ('scipy', 'requests') if m in sys.modules);"
        "print(int(probe), '|'.join(heavy) or '-')"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert out == "0 -", f"探测未知属性时拉起了重依赖或把它判成了存在: {out!r}"
