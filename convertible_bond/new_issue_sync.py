"""新债窄同步: 只把"新债的上市日"这一件事做完, 不重建条款.

为什么要单开一条路径, 而不是复用 ``cb-sync-tradable``:

``listing_date`` 全库只有 ``get_bond_terms`` 一条写入通道 (Wind ``ipo_date``); 每日的
``cb-sync-admission-status``、每日 ``cb-sync-events``、每月 ``cb-sync-ratings`` 都不带这个
字段。于是"新债挂牌了"只能靠**全量条款同步**发现 —— 而 GUI 的「扫新债」按钮原本是
"读 ``bundle_meta()['updated_at']`` 判新鲜度 → 超 1 天就提示跑 ``--incremental``", 三处同时失效:

1. ``updated_at`` 被**任何**写盘刷新 (见 :meth:`TermsBundle._save`), 每日状态刷新就把它推到
   今天 —— 提示永不弹出。这是 AGENTS.md 里 ``_meta.fetched_at`` 那个陷阱的第三个消费者。
2. 就算弹出并点"是", ``--incremental`` 按 ``is_stale(code, 7, source="Wind")`` 判, 而新债恰恰是
   刚被全量同步抓过的那批 → **整批跳过**; 同时它会去取另外几百只无关的债 (实测 2026-08-25
   的库: 跳过 4 只新债、真取 741 只), 十几分钟。花最贵的代价, 办不成唯一那件事。
3. 没装 WindPy 连提示都不给 —— 桌面包用户的「扫新债」永远只是重扫本地快照。

而这件事的真实规模是**每天几只**: 实测 2026-08-25 全库 1058 只里, 已发行未上市 3 只 +
已定上市日未挂牌 1 只 = 4 只; akshare ``bond_zh_cov`` 全表 1050 行里 ``上市时间`` 为空的
总共也只有 4 行。一次 ``ak.bond_zh_cov()`` (~2s, 不需要 Wind) 同时覆盖发现与上市日两件事。

**口径实测与 Wind 完全一致** (2026-08-25 全库交叉): ``申购日期`` == cb_data ``issue_date``
974/974, ``上市时间`` == ``listing_date`` 968/968 (另 6 只有一侧为空, 无法比较)。所以这条窄
路径既可以拿 ``上市时间`` 直接更新上市日, 也可以拿 ``申购日期`` 给全新代码建档当起息日。

三条不能违反的约定:

- **写盘的 source 桶固定** ``akshare:new_issues``, **绝不能写进** ``Wind`` **那格**。后者会同时
  毒化 ``cb-sync-tradable --incremental`` (以为条款刚抓过而跳过) 与 ``terms_as_of``
  (把条款 patch 整段裁掉) —— 两个坑 AGENTS.md 都记过。
- **解析不到就不写**: ``上市时间`` 为空时保持 ``listing_date=None``, 不拿申购日期兜底 ——
  一个假的上市日会让 :func:`is_issued_pending_listing` 直接判成"已上市", 新债于是带着空市价
  混进主池。反向同理: akshare 无值时**不清空**本地已有的上市日 (取不到证据 ≠ 证据为否)。
- **目标集只看 issue_date / listing_date**, 不看 ``trading_status`` / ``is_tradable`` —— 那三个是
  :func:`infer_cb_trading_metadata` 自己的派生产物, 拿它们选目标就是自我确认。

用法::

    cb-sync-new-issues            # 预览
    cb-sync-new-issues --apply    # 写盘 (每天几秒, 不需要 Wind)
"""
from __future__ import annotations

import logging
from dataclasses import replace
from datetime import date

from .cache import TermsBundle, project_bundle_path
from .data_providers.base import (
    BondTerms,
    is_issued_pending_listing,
    is_standard_public_cb_code,
    looks_private_cb_name,
    safe_date,
)
from .market_time import market_today

logger = logging.getLogger(__name__)

# 写盘用的来源标记. 独立成桶, 不与 ``Wind`` (全量条款同步) 混用 —— 见模块 docstring。
NEW_ISSUE_SOURCE = "akshare:new_issues"

# 发现全新代码的申购日期窗口. **不能**用"整表代码 - 本地代码"的集合差:
# 实测 akshare 表含 110002 这类早已退市的老债, 有 76 个不在 cb_data 里, 集合差会把它们整批拉回来。
DISCOVERY_WINDOW_DAYS = 90

# 刚挂牌的债继续跟几天: 上市首日的状态字段 (临停/成交额) 还在翻, 上市日本身也偶有更正。
JUST_LISTED_DAYS = 5

_COLUMNS = {
    "sec_name": ("债券简称", "债券名称", "证券简称"),
    "issue_date": ("申购日期",),
    "listing_date": ("上市时间", "上市日期"),
    "underlying_code": ("正股代码",),
    "underlying_name": ("正股简称", "正股名称"),
    "conversion_price": ("转股价",),
    "credit_rating": ("信用评级",),
}


def _safe_text(value) -> str | None:
    if value is None:
        return None
    try:
        if value != value:
            return None
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    return text or None


def _safe_float(value) -> float | None:
    text = _safe_text(value)
    if text is None:
        return None
    try:
        result = float(text.replace(",", ""))
    except ValueError:
        return None
    return result if result == result else None


def _pick(row, keys) -> object:
    for key in keys:
        try:
            if key in row.index:
                return row[key]
        except AttributeError:
            if isinstance(row, dict) and key in row:
                return row[key]
    return None


def to_wind_code(plain_code: str) -> str:
    """6 位代码 → Wind 代码. 与 ``AkshareDataProvider.list_tradable_cbs`` 同一约定 (11xxxx = 沪)."""
    code = str(plain_code or "").strip().zfill(6)
    return f"{code}.SH" if code.startswith("11") else f"{code}.SZ"


def fetch_new_issue_listings() -> dict[str, dict]:
    """``ak.bond_zh_cov()`` → ``{6 位代码: {字段}}``.

    一次拿全市场 (实测 1050 行 / ~2s)。字段名按候选列表容错, 上游改名时报错而不是静默返回空。
    """
    import akshare as ak

    frame = ak.bond_zh_cov()
    if frame is None or len(frame) == 0:
        raise RuntimeError("akshare bond_zh_cov 返回空表, 拒绝据此改库")
    columns = set(frame.columns)
    for field, candidates in (("sec_name", _COLUMNS["sec_name"]),
                              ("issue_date", _COLUMNS["issue_date"]),
                              ("listing_date", _COLUMNS["listing_date"])):
        if not columns.intersection(candidates):
            raise RuntimeError(f"akshare bond_zh_cov 没有 {field} 对应列 {candidates}, 上游字段可能改名了")

    out: dict[str, dict] = {}
    for _, row in frame.iterrows():
        code = _safe_text(_pick(row, ("债券代码", "代码")))
        if not code:
            continue
        out[code.zfill(6)] = {
            "sec_name": _safe_text(_pick(row, _COLUMNS["sec_name"])),
            "issue_date": safe_date(_pick(row, _COLUMNS["issue_date"])),
            "listing_date": safe_date(_pick(row, _COLUMNS["listing_date"])),
            "underlying_code": _safe_text(_pick(row, _COLUMNS["underlying_code"])),
            "underlying_name": _safe_text(_pick(row, _COLUMNS["underlying_name"])),
            "conversion_price": _safe_float(_pick(row, _COLUMNS["conversion_price"])),
            "credit_rating": _safe_text(_pick(row, _COLUMNS["credit_rating"])),
        }
    if not out:
        raise RuntimeError("akshare bond_zh_cov 一行都没解析出来, 拒绝据此改库")
    return out


def _is_standard_public(code: str, terms) -> bool:
    name = getattr(terms, "sec_name", None) if terms is not None else None
    return is_standard_public_cb_code(code) and not looks_private_cb_name(name)


def select_tracking_codes(store, *, on_date: date | None = None) -> list[str]:
    """需要每天盯上市日的那一小撮债.

    三类, 判据全部只看 ``issue_date`` / ``listing_date``:

    - 已发行未上市 (:func:`is_issued_pending_listing`)
    - 已定上市日但还没到 (交易所公告通常提前 1-2 周)
    - 刚挂牌 ``JUST_LISTED_DAYS`` 天内

    刻意不看 ``trading_status`` / ``is_tradable``: 它们是 :func:`infer_cb_trading_metadata`
    自己上一次的输出, 拿来选目标就是自我确认 —— 一次判错就永远选不回来。
    """
    check_date = on_date or market_today()
    out: list[str] = []
    for code in store.list_bonds():
        try:
            terms = store.get(code)
        except Exception:
            continue
        if terms is None or not _is_standard_public(code, terms):
            continue
        listing = getattr(terms, "listing_date", None)
        if listing is None:
            if is_issued_pending_listing(code, terms, check_date):
                out.append(code)
            continue
        if not isinstance(listing, date):
            continue
        if listing > check_date or (check_date - listing).days <= JUST_LISTED_DAYS:
            out.append(code)
    return out


def discover_new_codes(
    store,
    listings: dict[str, dict],
    *,
    on_date: date | None = None,
    window_days: int = DISCOVERY_WINDOW_DAYS,
) -> list[str]:
    """akshare 表里申购日期在窗口内、本地却没有的公募代码.

    用申购日期窗口而不是集合差 —— 见 :data:`DISCOVERY_WINDOW_DAYS`。
    """
    check_date = on_date or market_today()
    known = set(store.list_bonds())
    out: list[str] = []
    for plain, row in listings.items():
        issue_date = row.get("issue_date")
        if not isinstance(issue_date, date):
            continue
        if (check_date - issue_date).days > window_days or issue_date > check_date:
            continue
        code = to_wind_code(plain)
        if code in known:
            continue
        if not is_standard_public_cb_code(code) or looks_private_cb_name(row.get("sec_name")):
            continue
        out.append(code)
    return sorted(out)


def to_wind_stock_code(plain_code) -> str | None:
    """正股 6 位代码 → Wind 代码. 与 ``AkshareDataProvider.get_bond_terms`` 同一约定."""
    text = _safe_text(plain_code)
    if text is None:
        return None
    plain = text.zfill(6)
    if plain.startswith(("6", "9")):
        return f"{plain}.SH"
    if plain.startswith(("0", "3", "2")):
        return f"{plain}.SZ"
    return plain


def _stub_terms_for_new_bond(row: dict) -> BondTerms:
    """全新代码的**占位档**: 只填够"看得见、判得出还没上市"的字段.

    刻意不填余额/票息/触发条款 —— 那些由随后的 ``cb-sync-tradable`` 权威建档。
    占位档没有 ``Wind`` 那格时间戳, 所以 ``--incremental`` 一定会去取它, 自愈无需迁移。

    ``credit_rating`` 原样落库 (可能带 ``sti`` 后缀): 与"首次建档由 Wind 兜底覆盖率"同一
    口径, 下一次 ``cb-sync-ratings`` 会把写法洗成标准档位。
    """
    return BondTerms(
        sec_name=row.get("sec_name"),
        underlying_code=to_wind_stock_code(row.get("underlying_code")),
        underlying_name=row.get("underlying_name"),
        issue_date=row.get("issue_date"),
        listing_date=row.get("listing_date"),
        face_value=100.0,
        conversion_price=row.get("conversion_price"),
        credit_rating=row.get("credit_rating"),
    )


# 占位档缺失时可以从 akshare 顺手补齐的元信息字段. 只在本地为空时填 —— Wind 是这些字段的
# 权威源, 窄同步不该把它的值改掉 (``listing_date`` 是唯一例外, 见 sync_new_issues)。
_FILL_IF_EMPTY_FIELDS = ("sec_name", "underlying_code", "underlying_name", "issue_date")


def sync_new_issues(
    bundle_path=None,
    *,
    dry_run: bool = True,
    on_date: date | None = None,
    store=None,
    listings: dict[str, dict] | None = None,
) -> dict:
    """刷新新债的上市日, 并给全新代码建占位档.

    返回 ``{on_date, n_listings, n_tracked, changes, applied}``;
    ``changes`` 每条形如 ``{bond_code, bond_name, kind, field, before, after}``,
    ``kind`` ∈ {``listing_date``, ``fill``, ``new_bond``}。
    """
    store = store if store is not None else TermsBundle(bundle_path or project_bundle_path())
    check_date = on_date or market_today()
    listings = listings if listings is not None else fetch_new_issue_listings()

    tracked = select_tracking_codes(store, on_date=check_date)
    discovered = discover_new_codes(store, listings, on_date=check_date)

    changes: list[dict] = []
    updates: dict[str, BondTerms] = {}

    for code in tracked:
        row = listings.get(code.split(".")[0])
        if row is None:
            continue
        terms = store.get(code)
        if terms is None:
            continue
        patch: dict = {}
        remote_listing = row.get("listing_date")
        current_listing = getattr(terms, "listing_date", None)
        # 上市日是这条路径唯一"外部值可以覆盖本地值"的字段: 目标集里的债按定义就是 Wind
        # 那份快照必然落后的那几只。反向不成立 —— 远端为空时保持本地值不动。
        if isinstance(remote_listing, date) and remote_listing != current_listing:
            patch["listing_date"] = remote_listing
            changes.append({
                "bond_code": code, "bond_name": getattr(terms, "sec_name", None),
                "kind": "listing_date", "field": "listing_date",
                "before": current_listing, "after": remote_listing,
            })
        for field in _FILL_IF_EMPTY_FIELDS:
            value = row.get(field)
            if field == "underlying_code":
                value = to_wind_stock_code(value)
            if value is None or getattr(terms, field, None) is not None:
                continue
            patch[field] = value
            changes.append({
                "bond_code": code, "bond_name": getattr(terms, "sec_name", None),
                "kind": "fill", "field": field, "before": None, "after": value,
            })
        if patch:
            updates[code] = replace(terms, **patch)

    for code in discovered:
        row = listings.get(code.split(".")[0])
        if row is None:
            continue
        updates[code] = _stub_terms_for_new_bond(row)
        changes.append({
            "bond_code": code, "bond_name": row.get("sec_name"),
            "kind": "new_bond", "field": "listing_date",
            "before": None, "after": row.get("listing_date"),
        })

    if not dry_run and updates:
        store.set_many(list(updates.items()), source=NEW_ISSUE_SOURCE)

    return {
        "on_date": check_date,
        "n_listings": len(listings),
        "n_tracked": len(tracked),
        "tracked": tracked,
        "discovered": discovered,
        "changes": changes,
        "applied": bool(updates) and not dry_run,
    }
