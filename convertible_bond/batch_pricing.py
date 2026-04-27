"""
批量定价的应用层辅助函数.

这里放 GUI / CLI 都能复用的薄业务逻辑:
  - 解析用户输入的转债代码列表
  - 从 cb_data 静态信息缓存获取默认批量转债池
  - 构造带条款缓存的 DataProvider
  - 汇总与导出 batch_price_from_provider 的结果
"""
from __future__ import annotations

import csv
import json
import math
import re
from pathlib import Path
from datetime import date, datetime, timedelta
from typing import Any, Iterable, List, Sequence

from .cache import CachedBondDataProvider
from .data_providers import (
    AkshareDataProvider,
    CSVDataProvider,
    DataProvider,
    WindDataProvider,
    infer_cb_trading_metadata,
    is_standard_public_cb_code,
    looks_private_cb_name,
    to_date,
)


BATCH_RESULT_COLUMNS = [
    "bond_code",
    "bond_name",
    "stock_code",
    "S0",
    "K",
    "sigma",
    "theoretical_price",
    "market_price",
    "deviation",
    "credit_rating",
    "status",
]

_CODE_SPLIT_RE = re.compile(r"[\s,;，；]+")
_HEADER_TOKENS = {"code", "bond_code", "证券代码", "转债代码", "代码"}
BATCH_RESULT_META_KEY = "_meta"


def project_batch_cache_path() -> Path:
    """项目级批量定价结果缓存路径."""
    return Path(__file__).resolve().parent.parent / "data" / "batch_pricing_cache.json"


def parse_bond_codes(raw: str | Iterable[str]) -> List[str]:
    """解析用户输入 / CSV 单元格中的转债代码, 去重并保持原始顺序."""
    if isinstance(raw, str):
        text = "\n".join(line for line in raw.splitlines() if not line.strip().startswith("#"))
        chunks = _CODE_SPLIT_RE.split(text)
    else:
        chunks = []
        for item in raw:
            text = "\n".join(
                line for line in str(item).splitlines()
                if not line.strip().startswith("#")
            )
            chunks.extend(_CODE_SPLIT_RE.split(text))

    codes: List[str] = []
    seen = set()
    for chunk in chunks:
        code = chunk.strip().strip('"').strip("'")
        if not code or code.startswith("#"):
            continue
        if code.lower() in _HEADER_TOKENS:
            continue
        code = code.upper()
        if code not in seen:
            seen.add(code)
            codes.append(code)
    return codes


def list_batch_codes_from_cache(terms_cache, *, include_nonstandard: bool = False) -> List[str]:
    """返回 cb_data 静态信息缓存中的批量定价代码池.

    默认只返回当前 A 股普通公募可转债常见代码段:
    - SH: 110/111/113/118
    - SZ: 123/127/128

    Wind 的"沪深可转债"成分有时会混入 124xxx/1108xx 等定向转债、NQ/BJ
    债券或退市债。这些标的即使有条款和参考价格，也不适合参与主批量排序。
    """
    if terms_cache is None or not hasattr(terms_cache, "list_bonds"):
        return []
    codes = list(terms_cache.list_bonds())
    if include_nonstandard:
        return codes
    return [
        code for code in codes
        if batch_pricing_exclusion_reason(code, _cached_terms(terms_cache, code)) is None
    ]


def split_batch_codes_from_cache(terms_cache) -> tuple[List[str], List[tuple[str, str]]]:
    """把缓存代码池拆成 (可批量定价代码, 被过滤代码及原因)."""
    if terms_cache is None or not hasattr(terms_cache, "list_bonds"):
        return [], []
    kept: List[str] = []
    excluded: List[tuple[str, str]] = []
    for code in terms_cache.list_bonds():
        reason = batch_pricing_exclusion_reason(code, _cached_terms(terms_cache, code))
        if reason is None:
            kept.append(code)
        else:
            excluded.append((code, reason))
    return kept, excluded


def batch_pricing_exclusion_reason(
    code: str,
    terms: Any = None,
    *,
    on_date: date | None = None,
) -> str | None:
    """返回批量主池过滤原因; None 表示可以进入主批量定价.

    这里采用保守的白名单策略。定向转债在可交易前可能值得关注，但进入
    deviation 排序会制造虚假的"低估"信号，因此默认不进主池。
    """
    check_date = on_date or date.today()
    terms = _with_inferred_trading_metadata(code, terms, check_date)
    tradable_date = _terms_date(terms, "tradable_date")
    is_tradable = _terms_value(terms, "is_tradable")

    raw_code = str(code or "").upper().strip()
    if "." not in raw_code:
        return "代码缺少交易所后缀"
    plain, exch = raw_code.split(".", 1)
    if exch not in {"SH", "SZ"}:
        return "非沪深主板/深市可转债"

    name = _terms_value(terms, "sec_name") or _terms_value(terms, "bond_name")
    standard_public = is_standard_public_cb_code(raw_code) and not looks_private_cb_name(name)
    if standard_public:
        if tradable_date and tradable_date > check_date:
            return f"{(tradable_date - check_date).days} 日后可交易"
        return None

    if is_tradable is True or (tradable_date and tradable_date <= check_date):
        return None
    if tradable_date:
        return f"{(tradable_date - check_date).days} 日后可交易"
    if not is_standard_public_cb_code(raw_code):
        return "非普通公募转债代码段"
    if looks_private_cb_name(name):
        return "定向转债/暂不可自由交易"
    return None


def list_upcoming_tradable_from_cache(
    terms_cache,
    *,
    on_date: date | None = None,
    window_days: int = 7,
) -> List[dict]:
    """列出未来 window_days 天内进入可交易/关注窗口的非主池转债."""
    if terms_cache is None or not hasattr(terms_cache, "list_bonds"):
        return []
    check_date = on_date or date.today()
    end_date = check_date + timedelta(days=max(0, int(window_days)))
    rows: List[dict] = []
    for code in terms_cache.list_bonds():
        terms = _with_inferred_trading_metadata(code, _cached_terms(terms_cache, code), check_date)
        if terms is None:
            continue
        tradable_date = _terms_date(terms, "tradable_date")
        if tradable_date is None or tradable_date < check_date or tradable_date > end_date:
            continue
        name = _terms_value(terms, "sec_name")
        if is_standard_public_cb_code(code) and not looks_private_cb_name(name):
            continue
        rows.append({
            "bond_code": code,
            "bond_name": name,
            "stock_code": _terms_value(terms, "underlying_code"),
            "tradable_date": tradable_date,
            "days_to_trade": (tradable_date - check_date).days,
            "K": _terms_value(terms, "conversion_price"),
            "market_price": _terms_value(terms, "close"),
            "trading_status": _terms_value(terms, "trading_status"),
        })
    rows.sort(key=lambda row: (row["tradable_date"], row["bond_code"]))
    return rows


def merge_upcoming_pricing_results(
    upcoming_rows: Sequence[dict],
    pricing_results: Sequence[dict],
) -> List[dict]:
    """把关注池元数据与批量定价结果按代码合并."""
    priced_by_code = {row.get("bond_code"): row for row in pricing_results}
    merged: List[dict] = []
    for row in upcoming_rows:
        out = dict(row)
        priced = priced_by_code.get(row.get("bond_code"))
        if priced:
            for key in (
                "S0", "sigma", "theoretical_price", "market_price", "deviation",
                "credit_rating", "status", "data_source",
            ):
                if key in priced:
                    out[key] = priced[key]
            out["bond_name"] = priced.get("bond_name") or out.get("bond_name")
            out["stock_code"] = priced.get("stock_code") or out.get("stock_code")
            out["K"] = priced.get("K", out.get("K"))
        else:
            out.setdefault("status", "待定价")
        merged.append(out)
    return merged


def build_batch_provider(
    source: str,
    *,
    terms_cache=None,
    csv_root: str | Path | None = None,
    max_age_days: int = 30,
) -> DataProvider:
    """按名称构造批量定价用 provider.

    转债基础信息固定从 cb_data 读取/由 Wind 刷新; source 只决定正股价格、
    历史波动率、转债历史和无风险利率等动态数据来源。
    """
    source_key = (source or "").strip().lower()
    if source_key == "wind":
        inner: DataProvider = WindDataProvider()
    elif source_key == "akshare":
        inner = AkshareDataProvider()
    elif source_key == "csv":
        if not csv_root:
            raise RuntimeError("请先选择 CSV 数据根目录")
        inner = CSVDataProvider(csv_root)
    else:
        raise RuntimeError(f"未知数据源: {source}")

    if terms_cache is None:
        return inner
    static_source = inner if isinstance(inner, WindDataProvider) else None
    return CachedBondDataProvider(
        inner,
        terms_cache,
        static_source=static_source,
        max_age_days=max_age_days,
    )


def _cached_terms(terms_cache, code: str):
    if terms_cache is None or not hasattr(terms_cache, "get"):
        return None
    try:
        return terms_cache.get(code)
    except Exception:
        return None


def _with_inferred_trading_metadata(code: str, terms: Any, on_date: date):
    if terms is None or isinstance(terms, dict):
        return terms
    try:
        return infer_cb_trading_metadata(code, terms, on_date)
    except Exception:
        return terms


def _terms_value(terms: Any, key: str):
    if terms is None:
        return None
    if isinstance(terms, dict):
        return terms.get(key)
    return getattr(terms, key, None)


def _terms_date(terms: Any, key: str) -> date | None:
    value = _terms_value(terms, key)
    try:
        return to_date(value)
    except Exception:
        return None


def summarize_batch_results(results: Sequence[dict]) -> dict:
    """返回批量结果的轻量汇总, 供 UI / CLI 展示."""
    ok_count = sum(1 for row in results if row.get("status") == "ok")
    return {
        "total": len(results),
        "success": ok_count,
        "failed": len(results) - ok_count,
    }


def save_batch_results_cache(
    results: Sequence[dict],
    *,
    path: str | Path | None = None,
    source: str | None = None,
    params: dict | None = None,
    upcoming_results: Sequence[dict] | None = None,
) -> Path:
    """保存批量定价结果快照, 供 GUI 下次直接加载."""
    upcoming = list(upcoming_results or [])
    cache_path = Path(path) if path else project_batch_cache_path()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        BATCH_RESULT_META_KEY: {
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "source": source,
            "params": _json_safe(params or {}),
            "n_results": len(results),
            "n_upcoming_results": len(upcoming),
            "summary": summarize_batch_results(results),
        },
        "results": [_json_safe(row) for row in results],
        "upcoming_results": [_json_safe(row) for row in upcoming],
    }
    tmp = cache_path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
    tmp.replace(cache_path)
    return cache_path


def load_batch_results_cache(path: str | Path | None = None) -> dict:
    """读取批量定价结果快照, 返回 {meta, results}."""
    cache_path = Path(path) if path else project_batch_cache_path()
    if not cache_path.exists():
        raise FileNotFoundError(f"批量定价缓存不存在: {cache_path}")
    with open(cache_path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    results = [_restore_result_row(row) for row in payload.get("results", [])]
    upcoming_results = [_restore_result_row(row) for row in payload.get("upcoming_results", [])]
    return {
        "meta": payload.get(BATCH_RESULT_META_KEY, {}),
        "results": results,
        "upcoming_results": upcoming_results,
        "path": cache_path,
    }


def write_batch_results_csv(path: str | Path, results: Sequence[dict]) -> None:
    """按统一列定义导出批量定价结果."""
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(BATCH_RESULT_COLUMNS)
        for row in results:
            writer.writerow([_csv_value(row, column) for column in BATCH_RESULT_COLUMNS])


def _csv_value(row: dict, column: str):
    if row.get("status") != "ok" and column in {
        "S0", "K", "sigma", "theoretical_price",
    }:
        return ""
    value = row.get(column, "")
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    if column == "deviation":
        return f"{float(value):.6f}" if value != "" else ""
    return value


def _json_safe(value: Any):
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def _restore_result_row(row: dict) -> dict:
    restored = dict(row)
    for key in ("deviation", "theoretical_price", "S0", "K", "sigma"):
        if key in restored and restored[key] is None:
            restored[key] = float("nan")
    return restored
