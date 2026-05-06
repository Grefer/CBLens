"""
转债静态基础信息本地缓存层.

设计原则:
  - 转债基础信息是 *半静态* 数据 (发行后几乎不变, 仅下修/评级调整时变), 适合本地持久化
  - Wind 才能稳定覆盖强赎/回售等完整字段, 因此 cb_data 默认由 WindPy 同步
  - 动态数据 (正股价格/历史 σ/Shibor) 不缓存, 始终走用户选择的 market provider
  - 缓存丢失或过期 → 可透传到 Wind 拉取并写回; 拉取失败 → 仍可用过期缓存兜底

两种存储后端 (实现相同接口, 任选其一传给 CachedBondDataProvider / CachingDataProvider):

  TermsBundle  — 单 JSON 文件, 适合作为项目 snapshot 提交到 git
                  (例: data/cb_data.json), 跨设备一致
  TermsCache   — 一债一文件, 默认在 ~/.cb_pricer_cache/terms/, 方便用户级临时扩展
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, fields, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, get_args, get_origin, get_type_hints

# 注: 类型标注统一使用 X | None / list[X] (PEP 604, Python 3.10+).

from .data_providers import (
    BondTerms, CashflowSchedule, DataProvider, WindDataProvider, to_date,
)

logger = logging.getLogger(__name__)


def default_cache_root() -> Path:
    """用户级缓存根目录: ~/.cb_pricer_cache/."""
    return Path(os.path.expanduser("~/.cb_pricer_cache"))


def project_bundle_path() -> Path:
    """项目级转债静态信息 bundle 默认路径 (repo_root/data/cb_data.json).

    repo_root 推断方式: 沿 convertible_bond 包向上找两级.
    本文件路径: <repo>/convertible_bond/cache.py → repo = parent.parent.
    """
    return Path(__file__).resolve().parent.parent / "data" / "cb_data.json"


def _unwrap_type_args(tp) -> tuple:
    """返回类型注解里出现的具体类型 (剥掉 Optional/X|None 等 Union 包装)."""
    origin = get_origin(tp)
    if origin is None:
        return (tp,)
    return get_args(tp) or (tp,)


# 通过 get_type_hints 把 PEP 563 字符串注解还原成真正类型, 用于驱动序列化
_BOND_TERM_FIELDS = tuple(fields(BondTerms))
_BOND_TERM_HINTS = get_type_hints(BondTerms)
_DATE_FIELD_NAMES = frozenset(
    f.name for f in _BOND_TERM_FIELDS
    if any(t is date for t in _unwrap_type_args(_BOND_TERM_HINTS.get(f.name, f.type)))
)
_TUPLE_FIELD_NAMES = frozenset(
    f.name for f in _BOND_TERM_FIELDS
    if any(get_origin(t) is tuple for t in _unwrap_type_args(_BOND_TERM_HINTS.get(f.name, f.type)))
)


def _terms_to_json_dict(terms: BondTerms) -> dict:
    """BondTerms → JSON 可序列化 dict (date 转 ISO string)."""
    d = asdict(terms)
    for k, v in list(d.items()):
        if isinstance(v, date):
            d[k] = v.isoformat()
        elif isinstance(v, tuple):
            d[k] = list(v)
    return d


def _json_dict_to_terms(d: dict) -> BondTerms:
    """JSON dict → BondTerms.

    字段类型由 ``BondTerms`` dataclass 反射驱动: 声明里出现 ``date`` 的字段走
    ``to_date`` 反序列化; 声明里出现 ``tuple`` 的字段把 list 还原为 tuple。
    新增字段时无需修改本函数, 只要在 ``BondTerms`` 上加字段即可。
    """
    kwargs: dict = {}
    for f in _BOND_TERM_FIELDS:
        if f.name not in d:
            continue
        value = d[f.name]
        if value is None:
            kwargs[f.name] = None
            continue
        if f.name in _DATE_FIELD_NAMES:
            kwargs[f.name] = to_date(value)
        elif f.name in _TUPLE_FIELD_NAMES and isinstance(value, list):
            kwargs[f.name] = tuple(float(x) for x in value)
        else:
            kwargs[f.name] = value
    return BondTerms(**kwargs)


class TermsBundle:
    """单 JSON 文件存储, 适合作为 repo 内的 cb_data snapshot 提交到 git.

    文件结构:
        {
          "_bundle_meta": {"updated_at": "...", "source": "wind", "n_bonds": 532},
          "128009.SZ": {"sec_name": "...", "conversion_price": 52.77, ..., "_meta": {...}},
          "113029.SH": {...},
          ...
        }

    与 TermsCache 接口对齐 (has/get/set/list_bonds/fetched_at/is_stale/delete),
    可以直接传给 CachedBondDataProvider / CachingDataProvider.
    """

    BUNDLE_META_KEY = "_bundle_meta"

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else project_bundle_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict = {}
        self._load()

    def _load(self):
        if not self.path.exists():
            self._data = {}
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self._data = json.load(f)
        except Exception as e:
            logger.warning("bundle 文件 %s 解析失败: %s; 视为空", self.path, e)
            self._data = {}

    def _save(self):
        # 元信息
        n = sum(1 for k in self._data if not k.startswith("_"))
        meta = self._data.get(self.BUNDLE_META_KEY, {})
        meta["updated_at"] = datetime.now().isoformat(timespec="seconds")
        meta["n_bonds"] = n
        self._data[self.BUNDLE_META_KEY] = meta
        # 原子写
        tmp = self.path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2, sort_keys=True)
        tmp.replace(self.path)

    # ── 查询 ─────────────────────────────────────────────
    def has(self, bond_code: str) -> bool:
        return bond_code in self._data

    def list_bonds(self) -> list[str]:
        return sorted(k for k in self._data if not k.startswith("_"))

    def bundle_meta(self) -> dict:
        return dict(self._data.get(self.BUNDLE_META_KEY, {}))

    # ── 读写 ─────────────────────────────────────────────
    def get(self, bond_code: str) -> BondTerms | None:
        d = self._data.get(bond_code)
        if d is None:
            return None
        return _json_dict_to_terms(d)

    def set(self, bond_code: str, terms: BondTerms, source: str = "?") -> Path:
        d = _terms_to_json_dict(terms)
        d["_meta"] = {
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
            "source": source,
        }
        self._data[bond_code] = d
        self._save()
        return self.path

    def set_many(self, items: Iterable, source: str = "?"):
        """批量写入 [(code, terms), ...], 期间只刷盘一次.
        比逐条 set() 显著更快 (大批量同步用)."""
        now = datetime.now().isoformat(timespec="seconds")
        for code, terms in items:
            d = _terms_to_json_dict(terms)
            d["_meta"] = {"fetched_at": now, "source": source}
            self._data[code] = d
        self._save()

    def fetched_at(self, bond_code: str) -> datetime | None:
        d = self._data.get(bond_code)
        if d is None:
            return None
        ts = d.get("_meta", {}).get("fetched_at")
        if not ts:
            return None
        try:
            return datetime.fromisoformat(ts)
        except ValueError:
            return None

    def is_stale(self, bond_code: str, max_age_days: int) -> bool:
        ts = self.fetched_at(bond_code)
        if ts is None:
            return True
        return datetime.now() - ts > timedelta(days=max_age_days)

    def delete(self, bond_code: str) -> bool:
        if bond_code in self._data:
            del self._data[bond_code]
            self._save()
            return True
        return False


class TermsCache:
    """转债条款 JSON 文件缓存 (一债一文件, 跨进程安全)."""

    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else default_cache_root()
        self.terms_dir = self.root / "terms"
        self.terms_dir.mkdir(parents=True, exist_ok=True)

    # ── 路径与查询 ───────────────────────────────────────
    def path(self, bond_code: str) -> Path:
        return self.terms_dir / f"{bond_code}.json"

    def has(self, bond_code: str) -> bool:
        return self.path(bond_code).exists()

    def list_bonds(self) -> list[str]:
        """缓存中所有债代码 (按文件名)."""
        return sorted(p.stem for p in self.terms_dir.glob("*.json"))

    # ── 读写 ─────────────────────────────────────────────
    def get(self, bond_code: str) -> BondTerms | None:
        p = self.path(bond_code)
        if not p.exists():
            return None
        try:
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
        except Exception as e:
            logger.warning("缓存文件 %s 解析失败: %s", p, e)
            return None
        return _json_dict_to_terms(d)

    def set(self, bond_code: str, terms: BondTerms, source: str = "?") -> Path:
        d = _terms_to_json_dict(terms)
        d["_meta"] = {
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
            "source": source,
        }
        p = self.path(bond_code)
        # 原子写: 先写 .tmp 再 rename, 防止中途崩溃留下半截 JSON
        tmp = p.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        tmp.replace(p)
        return p

    def fetched_at(self, bond_code: str) -> datetime | None:
        p = self.path(bond_code)
        if not p.exists():
            return None
        try:
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            return None
        meta = d.get("_meta", {})
        ts = meta.get("fetched_at")
        if not ts:
            return datetime.fromtimestamp(p.stat().st_mtime)
        try:
            return datetime.fromisoformat(ts)
        except ValueError:
            return None

    def is_stale(self, bond_code: str, max_age_days: int) -> bool:
        ts = self.fetched_at(bond_code)
        if ts is None:
            return True
        return datetime.now() - ts > timedelta(days=max_age_days)

    def delete(self, bond_code: str) -> bool:
        p = self.path(bond_code)
        if p.exists():
            p.unlink()
            return True
        return False


class CachingDataProvider(DataProvider):
    """装饰器: 把 inner provider 的 get_bond_terms / get_cashflow 包一层本地缓存.

    - 条款 (get_bond_terms): 命中且未过期 → 返回缓存; 否则透传 inner 并写回缓存
    - 动态数据 (价格/历史/Shibor): 全部透传 inner, 不缓存
    - inner 调用失败时, 若缓存里有旧数据, 仍返回旧数据 + 记 warning

    构造:
        cache = TermsCache()
        provider = CachingDataProvider(WindDataProvider(), cache, max_age_days=30)
    """

    def __init__(self, inner: DataProvider, cache,
                 max_age_days: int = 30, auto_refresh: bool = True):
        """`cache` 可以是 TermsBundle 或 TermsCache (鸭子类型: 需 has/get/set/fetched_at/is_stale)."""
        self.inner = inner
        self.cache = cache
        self.max_age_days = max_age_days
        self.auto_refresh = auto_refresh
        self.name = f"{inner.name}+cache"
        self._write_lock = threading.Lock()

    def get_bond_terms(self, bond_code, valuation_date):
        cached = self.cache.get(bond_code)
        stale = self.cache.is_stale(bond_code, self.max_age_days) if cached else True

        # 缓存命中且未过期 → 直接返回, 不打网络
        if cached is not None and not stale:
            return cached

        # 否则尝试从 inner 拉取
        try:
            fresh = self.inner.get_bond_terms(bond_code, valuation_date)
            # 至少要有 K 才认为有效, 否则不覆盖已有缓存
            if fresh.conversion_price is not None:
                with self._write_lock:
                    self.cache.set(bond_code, fresh, source=self.inner.name)
                return fresh
            elif cached is not None:
                logger.warning("inner 返回的 %s 条款不完整 (无 K), 沿用缓存", bond_code)
                return cached
            return fresh
        except Exception as e:
            if cached is not None:
                logger.warning("inner.get_bond_terms(%s) 失败 (%s), 沿用缓存", bond_code, e)
                return cached
            raise

    def force_refresh(self, bond_code: str, valuation_date: date) -> BondTerms:
        """强制从 inner 拉取最新条款, 覆盖本地缓存. 失败会抛出."""
        fresh = self.inner.get_bond_terms(bond_code, valuation_date)
        with self._write_lock:
            self.cache.set(bond_code, fresh, source=self.inner.name)
        return fresh

    # ── 以下接口全部透传 ───────────────────────────────────
    def get_stock_close(self, stock_code, on_date):
        return self.inner.get_stock_close(stock_code, on_date)

    def get_stock_history(self, stock_code, start, end):
        return self.inner.get_stock_history(stock_code, start, end)

    def get_bond_history(self, bond_code, start, end):
        return self.inner.get_bond_history(bond_code, start, end)

    def get_cashflow(self, bond_code) -> CashflowSchedule | None:
        return self.inner.get_cashflow(bond_code)

    def get_risk_free_rate(self, on_date):
        return self.inner.get_risk_free_rate(on_date)

    def hist_vol(self, stock_code, end_date, window_days):
        return self.inner.hist_vol(stock_code, end_date, window_days)


class CachedBondDataProvider(DataProvider):
    """组合 provider: Wind 静态 cb_data + 可选动态行情源.

    - get_bond_terms / get_cashflow: 优先从 cb_data 读取; 缓存缺失或强制刷新时只用 Wind
    - get_stock_close / get_stock_history / get_bond_history: 透传到 market provider
    - get_risk_free_rate: 透传到 market provider, 并按日期缓存一次结果

    这让 akshare 只负责它擅长的动态行情, 不再参与转债条款补全。
    """

    def __init__(
        self,
        market: DataProvider,
        cache,
        *,
        static_source: DataProvider | None = None,
        max_age_days: int = 365,
        auto_refresh: bool = False,
        with_cashflow: bool = True,
    ):
        self.market = market
        self.cache = cache
        self.static_source = static_source or WindDataProvider()
        self.max_age_days = max_age_days
        self.auto_refresh = auto_refresh
        self.with_cashflow = with_cashflow
        self.name = f"cb_data+{market.name}"
        self._write_lock = threading.Lock()
        self._risk_free_cache: dict[date, float | None] = {}

    def _merge_cashflow(self, bond_code: str, terms: BondTerms) -> BondTerms:
        if not self.with_cashflow:
            return terms
        try:
            cf = self.static_source.get_cashflow(bond_code)
        except Exception as e:
            logger.debug("Wind get_cashflow(%s) 失败, 沿用条款字段: %s", bond_code, e)
            return terms
        if not cf:
            return terms
        patch = {}
        if cf.coupon_rates:
            patch["coupon_rates"] = cf.coupon_rates
        if cf.maturity_date and not terms.maturity_date:
            patch["maturity_date"] = cf.maturity_date
        if cf.redemption_price is not None:
            patch["redemption_price"] = float(cf.redemption_price)
        return replace(terms, **patch) if patch else terms

    def _refresh_static_terms(self, bond_code: str, valuation_date: date) -> BondTerms:
        fresh = self.static_source.get_bond_terms(bond_code, valuation_date)
        fresh = self._merge_cashflow(bond_code, fresh)
        if fresh.conversion_price is None:
            raise RuntimeError(f"Wind 返回的 {bond_code} 静态信息不完整: 无转股价 K")
        with self._write_lock:
            self.cache.set(bond_code, fresh, source=self.static_source.name)
        return fresh

    def get_bond_terms(self, bond_code, valuation_date):
        cached = self.cache.get(bond_code)
        stale = self.cache.is_stale(bond_code, self.max_age_days) if cached else True
        if cached is not None and (not stale or not self.auto_refresh):
            return cached
        try:
            return self._refresh_static_terms(bond_code, valuation_date)
        except Exception as e:
            if cached is not None:
                logger.warning("Wind 刷新 cb_data(%s) 失败 (%s), 沿用缓存", bond_code, e)
                return cached
            raise

    def force_refresh(self, bond_code: str, valuation_date: date) -> BondTerms:
        """强制从 Wind 拉取静态字段并覆盖 cb_data."""
        return self._refresh_static_terms(bond_code, valuation_date)

    def get_cashflow(self, bond_code) -> CashflowSchedule | None:
        terms = self.cache.get(bond_code)
        if terms is None:
            return None
        if not terms.coupon_rates and terms.redemption_price is None and terms.maturity_date is None:
            return None
        return CashflowSchedule(
            coupon_rates=terms.coupon_rates,
            redemption_price=terms.redemption_price,
            maturity_date=terms.maturity_date,
            cashflows=[],
        )

    def get_stock_close(self, stock_code, on_date):
        return self.market.get_stock_close(stock_code, on_date)

    def get_stock_history(self, stock_code, start, end):
        return self.market.get_stock_history(stock_code, start, end)

    def get_bond_history(self, bond_code, start, end):
        return self.market.get_bond_history(bond_code, start, end)

    def get_risk_free_rate(self, on_date):
        if on_date not in self._risk_free_cache:
            try:
                self._risk_free_cache[on_date] = self.market.get_risk_free_rate(on_date)
            except Exception as e:
                logger.warning("%s 无风险利率获取失败: %s", self.market.name, e)
                self._risk_free_cache[on_date] = None
        return self._risk_free_cache[on_date]

    def hist_vol(self, stock_code, end_date, window_days):
        return self.market.hist_vol(stock_code, end_date, window_days)
