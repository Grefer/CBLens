"""回测取数的**跨运行磁盘缓存** (backtest_disk_cache).

历史回测每个调仓日都要逐债向数据源 (Wind) 取 point-in-time 条款和历史行情, 单期
准入即可耗时数百秒, 多周期复跑动辄数小时。关键事实: **过去某 (债, 日期) 的条款与
某 (债, 区间) 的历史收盘价都是不可变的**, 因此把它们落盘后, 后续复跑可秒级复用。

本模块提供一个**装饰器型 Provider** :class:`DiskCacheProvider`, 包在真实数据源外、
``_BacktestCacheProvider`` (单次运行内存缓存) 内层::

    Wind高保真 → DiskCacheProvider(cache_dir=...) → _BacktestCacheProvider → 回测

安全设计:
  - **只缓存严格过去的数据** (``valuation_date < today`` / 历史区间 ``end < today``),
    避免把"当日/未来"会变动的数据固化, 杜绝陈旧污染。
  - 条款用 ``cache._terms_to_json_dict`` / ``_json_dict_to_terms`` 序列化 (与
    TermsBundle 完全一致的口径), 历史价存 ``[[iso, value], ...]``。
  - 命中/未命中均**透传真实数据源结果**, 行为与不加缓存时一致; 缓存仅影响速度。
  - 缓存键带 provider 命名空间, 不同数据源/口径不会串味。

默认**不接入任何现有流程**, 由 CLI/调用方显式启用 (``cb-strategy-backtest --cache-dir``)。
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

from .cache import _json_dict_to_terms, _terms_to_json_dict
from .data_providers import DataProvider, to_date

logger = logging.getLogger(__name__)


def _sanitize(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in str(name))[:40]


def _provider_identity(provider: Any) -> str:
    """递归汇总 provider 身份: 名称 + 内层 + 条款来源文件 (bundle/patch/event/history)
    的路径与 mtime。patch/events/bundle 一更新, 身份即变, 缓存随之失效。
    与 ``strategy_backtest._provider_cache_identity`` 同口径 (此处本地实现避免对重量级
    模块的导入耦合)。"""
    parts = [str(getattr(provider, "name", type(provider).__name__))]
    inner = getattr(provider, "inner", None)
    if inner is not None and inner is not provider:
        parts.append(_provider_identity(inner))
    for attr in ("history_store", "patch_store", "event_store", "path", "bundle"):
        obj = getattr(provider, attr, None)
        if obj is None:
            continue
        path = getattr(obj, "root", None) or getattr(obj, "path", None) or (
            obj if isinstance(obj, (str, Path)) else None)
        if path is None:
            continue
        p = Path(path)
        try:
            parts.append(f"{attr}:{p}:{p.stat().st_mtime_ns}")
        except OSError:
            parts.append(f"{attr}:{p}:missing")
    return "|".join(parts)


class DiskCacheProvider(DataProvider):
    """把 ``get_bond_terms`` / 历史行情落盘, 供跨运行复用的 Provider 装饰器。"""

    def __init__(
        self,
        inner: DataProvider,
        cache_dir: str | Path,
        *,
        today: date | None = None,
        namespace: str | None = None,
    ):
        self.inner = inner
        self.name = f"{getattr(inner, 'name', 'provider')}+disk"
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._today = today or date.today()
        # 缓存身份: 显式 namespace 优先, 否则按 provider+条款来源文件 mtime 自动派生。
        # 身份变 (patch/events/bundle 更新) → 旧缓存视为失效。
        self._identity = namespace or _provider_identity(inner)
        self.stats: Counter = Counter()

        self._terms_path = self.cache_dir / "terms.json"
        self._bond_hist_path = self.cache_dir / "bond_history.json"
        self._stock_hist_path = self.cache_dir / "stock_history.json"
        self._meta_path = self.cache_dir / "_meta.json"
        stored = _load_json(self._meta_path).get("identity")
        if stored == self._identity:
            self._terms: dict[str, dict] = _load_json(self._terms_path)
            self._bond_hist: dict[str, list] = _load_json(self._bond_hist_path)
            self._stock_hist: dict[str, list] = _load_json(self._stock_hist_path)
        else:                                    # 身份不符/首次 → 弃用旧缓存, 防陈旧命中
            if stored is not None:
                logger.info("磁盘缓存身份变更, 弃用旧缓存: %s", self.cache_dir)
            self._terms, self._bond_hist, self._stock_hist = {}, {}, {}
        self._dirty: set[str] = set()

    def __getattr__(self, name):  # 透传未显式实现的属性/方法
        return getattr(self.inner, name)

    def cache_identity(self) -> str:
        ident = getattr(self.inner, "cache_identity", None)
        return ident() if callable(ident) else self.name

    # ---------------- keys (整目录已按身份隔离, 故键无需再带命名空间) ----------------
    def _terms_key(self, code: str, d: date) -> str:
        return f"{code}|{d.isoformat()}"

    def _hist_key(self, code: str, start: date, end: date) -> str:
        return f"{code}|{start.isoformat()}|{end.isoformat()}"

    # ---------------- terms ----------------
    def get_bond_terms(self, bond_code: str, valuation_date: date):
        if valuation_date >= self._today:        # 当日/未来不缓存
            return self.inner.get_bond_terms(bond_code, valuation_date)
        key = self._terms_key(bond_code, valuation_date)
        cached = self._terms.get(key)
        if cached is not None:
            self.stats["terms_hits"] += 1
            return _json_dict_to_terms(cached)
        self.stats["terms_misses"] += 1
        terms = self.inner.get_bond_terms(bond_code, valuation_date)
        try:
            self._terms[key] = _terms_to_json_dict(terms)
            self._dirty.add("terms")
        except Exception:                        # 序列化失败不致命, 退回不缓存
            logger.debug("terms 序列化失败, 跳过缓存: %s", bond_code, exc_info=True)
        return terms

    # ---------------- histories ----------------
    def _cached_history(self, store: dict, key: str):
        raw = store.get(key)
        if raw is None:
            return None
        return [(to_date(d), (float(v) if v is not None else None)) for d, v in raw]

    def get_bond_history(self, bond_code: str, start: date, end: date):
        if end >= self._today:
            return self.inner.get_bond_history(bond_code, start, end)
        key = self._hist_key(bond_code, start, end)
        cached = self._cached_history(self._bond_hist, key)
        if cached is not None:
            self.stats["bond_history_hits"] += 1
            return cached
        self.stats["bond_history_misses"] += 1
        history = self.inner.get_bond_history(bond_code, start, end)
        self._bond_hist[key] = _history_to_json(history)
        self._dirty.add("bond_hist")
        return history

    def get_stock_history(self, stock_code: str, start: date, end: date):
        if end >= self._today:
            return self.inner.get_stock_history(stock_code, start, end)
        key = self._hist_key(stock_code, start, end)
        cached = self._cached_history(self._stock_hist, key)
        if cached is not None:
            self.stats["stock_history_hits"] += 1
            return cached
        self.stats["stock_history_misses"] += 1
        history = self.inner.get_stock_history(stock_code, start, end)
        self._stock_hist[key] = _history_to_json(history)
        self._dirty.add("stock_hist")
        return history

    # ---------------- 透传 (不缓存, 但补齐 ABC 契约) ----------------
    def get_stock_close(self, stock_code: str, on_date: date) -> float:
        return self.inner.get_stock_close(stock_code, on_date)

    def get_stock_dividend_yield(self, stock_code, on_date):
        return self.inner.get_stock_dividend_yield(stock_code, on_date)

    def get_cashflow(self, bond_code):
        return self.inner.get_cashflow(bond_code)

    def get_risk_free_rate(self, on_date):
        return self.inner.get_risk_free_rate(on_date)

    def get_admission_status(self, bond_code, valuation_date, base_terms=None):
        return self.inner.get_admission_status(bond_code, valuation_date, base_terms)

    def list_bond_announcements(self, bond_code, start, end):
        return self.inner.list_bond_announcements(bond_code, start, end)

    def list_tradable_cbs(self, on_date: date | None = None):
        return self.inner.list_tradable_cbs(on_date)

    def get_terms_source_diagnostics(self, bond_code: str, valuation_date: date):
        # 诊断走本地 store, 成本低, 不缓存; 仅在 inner 支持时透传。
        describe = getattr(self.inner, "get_terms_source_diagnostics", None)
        if callable(describe):
            return describe(bond_code, valuation_date)
        raise AttributeError("inner provider 无 get_terms_source_diagnostics")

    # ---------------- 落盘 ----------------
    def flush(self) -> None:
        """把脏缓存原子写盘 (先 .tmp 再 rename), 并落地身份元数据。"""
        if not self._dirty:
            return
        if "terms" in self._dirty:
            _atomic_write(self._terms_path, self._terms)
        if "bond_hist" in self._dirty:
            _atomic_write(self._bond_hist_path, self._bond_hist)
        if "stock_hist" in self._dirty:
            _atomic_write(self._stock_hist_path, self._stock_hist)
        _atomic_write(self._meta_path, {"identity": self._identity})
        self._dirty.clear()

    def close(self) -> None:
        self.flush()

    def __enter__(self) -> "DiskCacheProvider":
        return self

    def __exit__(self, *exc) -> None:
        self.flush()


def _history_to_json(history) -> list:
    out = []
    for d, v in history or []:
        iso = d.isoformat() if isinstance(d, date) else (str(d) if d is not None else None)
        out.append([iso, (float(v) if v is not None else None)])
    return out


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        logger.warning("磁盘缓存损坏, 忽略: %s", path)
        return {}


def _atomic_write(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    tmp.replace(path)
