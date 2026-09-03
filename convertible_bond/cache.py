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
from collections.abc import Iterable
from typing import get_args, get_origin, get_type_hints

# 注: 类型标注统一使用 X | None / list[X] (PEP 604, Python 3.10+).

from .atomic_io import atomic_write_json
from .data_providers import (
    BondTerms, CashflowSchedule, DataProvider, WindDataProvider,
    infer_cb_trading_metadata, to_date,
)
from .paths import data_path

logger = logging.getLogger(__name__)


def default_cache_root() -> Path:
    """用户级缓存根目录: ~/.cb_pricer_cache/."""
    return Path(os.path.expanduser("~/.cb_pricer_cache"))


def project_bundle_path() -> Path:
    """项目级转债静态信息 bundle 默认路径 (repo_root/data/cb_data.json).

    repo_root 推断方式: 沿 convertible_bond 包向上找两级.
    本文件路径: <repo>/convertible_bond/cache.py → repo = parent.parent.
    """
    return data_path("cb_data.json", seed=True)


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
            self._disk_stamp = None
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self._data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("bundle 文件 %s 解析失败: %s; 视为空", self.path, e)
            self._data = {}
        self._disk_stamp = self._stat_stamp()

    def _stat_stamp(self) -> tuple | None:
        """盘上这份文件的身份戳 —— 用来判断"我读过之后有没有别人写过"。"""
        try:
            st = self.path.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def _merge_foreign_writes(self) -> int:
        """把**别人写进盘、而我内存里没有**的条目补回来, 返回补了几条。

        ``_save`` 是整份重写 (``json.dump(self._data)``), 所以一个长命实例只要快照
        比盘上旧, 下一次写就会静默删掉别人新增的债。实测: 实例 a 与 b 都读到 {A},
        a 写入 B, b 再写入 C —— 盘上只剩 {A, C}, B 无声消失, 而 ``_bundle_meta.n_bonds``
        跟着一起被改小, 连"少了"都看不出来。

        这不是假想的并发: GUI 的「🌐 同步池」菜单**就是**在 GUI 持有 bundle 的同时
        起子进程去写同一个文件 (``gui/controllers/wind_sync.py``)。``reload()`` 是给这个
        场景准备的, 但它要人显式调, 而两次写之间的任何一次 ``set()`` 都来不及。

        本 bundle 是**只增不删**的 (见类 docstring), 所以合并规则很简单: 盘上有而我
        没有的补进来; 两边都有的**以我为准** —— 我是这一次的写入方, 我的值更新。
        """
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                on_disk = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("bundle %s 合并前重读失败, 按整份重写处理: %s", self.path, e)
            return 0
        added = 0
        for key, value in on_disk.items():
            if key == self.BUNDLE_META_KEY:
                continue
            if key not in self._data:
                self._data[key] = value
                added += 1
        return added

    def _save(self):
        # 我读过之后别人动过这个文件 → 先把他们新增的条目并回来, 再整份重写。
        # 戳没变时一个字节都不读, 所以独占写 (全量同步的常态) 零开销。
        if getattr(self, "_disk_stamp", None) != self._stat_stamp():
            added = self._merge_foreign_writes()
            if added:
                logger.info("bundle %s 合并了外部写入的 %d 条记录", self.path, added)
        # 元信息
        n = sum(1 for k in self._data if not k.startswith("_"))
        meta = self._data.get(self.BUNDLE_META_KEY, {})
        meta["updated_at"] = datetime.now().isoformat(timespec="seconds")
        meta["n_bonds"] = n
        self._data[self.BUNDLE_META_KEY] = meta
        # 原子写
        atomic_write_json(self.path, self._data)
        self._disk_stamp = self._stat_stamp()

    def reload(self):
        """重新读取磁盘上的 bundle.

        条款同步是独立子进程写文件的 (见 GUI 的池同步), 长驻的 GUI 实例必须显式
        重读才能看到新债; 否则"扫新债"永远只看得到进程启动那一刻的快照。
        """
        self._load()
        return self

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

    def _meta_for_write(self, bond_code: str, source: str, now: str) -> dict:
        """写入用的 ``_meta``: 全局 fetched_at + **按来源分桶**的时间戳.

        为什么要分桶: ``fetched_at`` 的原意是"这份条款快照的截止日", 但写它的有四条路径,
        只有 ``cb-sync-tradable`` (source=provider.name) 真的抓条款 —— 每日的
        ``Wind:admission_status``、每月的 ``akshare:ratings``、每日的 ``cb_events``
        都只刷各自那几个状态字段, 却一样把 fetched_at 推到今天。于是这个字段退化成
        "上次被任何人碰过的时间", 而两个消费者仍按原意读它:

          - ``cb_data_sync`` 的 ``--incremental`` → 实测 1052/1058 只被判成"7 天内已更新"
            而跳过, 且打印"已在 7 天内更新", 看起来完全正常;
          - ``CachedBondDataProvider.terms_as_of()`` → 当作 ``after=`` 传给条款 patch 投影,
            于是 live 定价路径上**一条 patch 都不生效**, 两次全量同步之间的条款变更
            (晶瑞转2 K 差 19.5%、强力转债 16.5%) 完全没有兜底。

        ``fetched_at_by_source`` 缺失时上层按"陈旧"处理 —— 保守, 且跑一次全量同步就自愈,
        不需要迁移脚本。
        """
        prev = (self._data.get(bond_code) or {}).get("_meta") or {}
        by_source = dict(prev.get("fetched_at_by_source") or {})
        by_source[source] = now
        return {"fetched_at": now, "source": source, "fetched_at_by_source": by_source}

    def set(self, bond_code: str, terms: BondTerms, source: str = "?") -> Path:
        d = _terms_to_json_dict(terms)
        d["_meta"] = self._meta_for_write(
            bond_code, source, datetime.now().isoformat(timespec="seconds"))
        self._data[bond_code] = d
        self._save()
        return self.path

    def set_many(self, items: Iterable, source: str = "?"):
        """批量写入 [(code, terms), ...], 期间只刷盘一次.
        比逐条 set() 显著更快 (大批量同步用)."""
        now = datetime.now().isoformat(timespec="seconds")
        for code, terms in items:
            d = _terms_to_json_dict(terms)
            d["_meta"] = self._meta_for_write(code, source, now)
            self._data[code] = d
        self._save()

    def fetched_at(self, bond_code: str, *, source: str | None = None) -> datetime | None:
        """记录的抓取时间。``source`` 给定时只认该来源写入的那次 (见 _meta_for_write)。"""
        d = self._data.get(bond_code)
        if d is None:
            return None
        meta = d.get("_meta", {})
        if source is None:
            ts = meta.get("fetched_at")
        else:
            ts = (meta.get("fetched_at_by_source") or {}).get(source)
        if not ts:
            return None
        try:
            return datetime.fromisoformat(ts)
        except ValueError:
            return None

    def is_stale(self, bond_code: str, max_age_days: int, *,
                 source: str | None = None) -> bool:
        ts = self.fetched_at(bond_code, source=source)
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
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("缓存文件 %s 解析失败: %s", p, e)
            return None
        return _json_dict_to_terms(d)

    def set(self, bond_code: str, terms: BondTerms, source: str = "?") -> Path:
        d = _terms_to_json_dict(terms)
        now = datetime.now().isoformat(timespec="seconds")
        prev = {}
        existing = self.path(bond_code)
        if existing.exists():
            try:
                with open(existing, "r", encoding="utf-8") as f:
                    prev = (json.load(f).get("_meta") or {}).get("fetched_at_by_source") or {}
            except (json.JSONDecodeError, OSError):
                prev = {}
        d["_meta"] = {"fetched_at": now, "source": source,
                      "fetched_at_by_source": {**prev, source: now}}
        p = self.path(bond_code)
        # 原子写: 先写 .tmp 再 rename, 防止中途崩溃留下半截 JSON
        atomic_write_json(p, d, sort_keys=False)
        return p

    def fetched_at(self, bond_code: str, *, source: str | None = None) -> datetime | None:
        """与 :meth:`TermsBundle.fetched_at` 同签名 (鸭子类型缓存共用接口)。"""
        p = self.path(bond_code)
        if not p.exists():
            return None
        try:
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
        except (json.JSONDecodeError, OSError):
            return None
        meta = d.get("_meta", {})
        if source is not None:
            ts = (meta.get("fetched_at_by_source") or {}).get(source)
            if not ts:
                return None
        else:
            ts = meta.get("fetched_at")
            if not ts:
                return datetime.fromtimestamp(p.stat().st_mtime)
        try:
            return datetime.fromisoformat(ts)
        except ValueError:
            return None

    def is_stale(self, bond_code: str, max_age_days: int, *,
                 source: str | None = None) -> bool:
        ts = self.fetched_at(bond_code, source=source)
        if ts is None:
            return True
        return datetime.now() - ts > timedelta(days=max_age_days)

    def delete(self, bond_code: str) -> bool:
        p = self.path(bond_code)
        if p.exists():
            p.unlink()
            return True
        return False


#: 全量条款同步 (``cb-sync-tradable``) 落盘时用的来源桶名 = 那个 provider 的 ``name``。
#: 实测盘上 5 个桶: Wind:admission_status / Wind / cb_events / akshare:ratings /
#: akshare:new_issues —— 只有 ``Wind`` 这一桶真抓条款, 另外四个各刷几个状态字段却一样
#: 会把**全局** fetched_at 推到今天。取条款锚必须点名这一桶。
TERMS_SYNC_SOURCE = "Wind"


def terms_fetched_at(cache, bond_code: str, *, source: str | None) -> date | None:
    """条款缓存里这只债**条款那一次**的抓取日 —— 快照已含该日之前生效的全部条款变更.

    ``source`` 是**全量条款同步**那个来源桶 (provider.name, 实测盘上是 ``"Wind"``)。
    必须按来源取: 全局 ``fetched_at`` 会被每日 ``Wind:admission_status`` / 每月
    ``akshare:ratings`` / 每日 ``cb_events`` 一起推到今天, 而这三者一个条款字段都不抓。
    用全局值当锚等于宣称"今天之前的条款变更都已含在快照里", 于是条款 patch 被
    ``after=`` 整段裁掉 —— 实测 live 定价路径上一条 patch 都不生效。

    缺桶时**回落到全局 fetched_at**, 不是 None —— None 在投影层表示"不裁剪", 会把整条
    patch 链从发行日回放上来, 拿陈旧/解析错的值盖掉正确的 cb_data (实测海顺转债
    K 11.63 会被盖成 17.74)。宁可暂时保守裁掉, 也不能反向写坏。

    这个函数是**单一事实源**: 两个装饰器的 ``terms_as_of`` 与 ``batch_pricing`` 的
    ``_terms_cache_as_of`` 曾各写一份, 而只有前两份改成了按来源取 —— 第三份留在全局戳上,
    静默给主池的条款投影用错锚 (实测 3 只债的 patch 被多裁 5 天)。逐字重复的代码不会
    一起被修, 这是已经发生过的事。
    """
    if cache is None:
        return None
    getter = getattr(cache, "fetched_at", None)
    if getter is None:
        return None
    ts = None
    if source:
        try:
            ts = getter(bond_code, source=source)
        except TypeError:      # 旧式 cache 没有 source 形参
            ts = None
        except Exception:
            return None
    if ts is None:
        try:
            ts = getter(bond_code)
        except Exception:
            return None
    return ts.date() if ts is not None else None


class CachingDataProvider(DataProvider):
    """装饰器: 把 inner provider 的 get_bond_terms / get_cashflow 包一层本地缓存.

    - 条款 (get_bond_terms): 命中且未过期 → 返回缓存; 否则透传 inner 并写回缓存
    - 动态数据 (价格/历史/股息率/Shibor): 全部透传 inner, 不缓存
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

    def terms_as_of(self, bond_code: str, valuation_date: date) -> date | None:
        """条款来自本装饰器的缓存, 锚是**条款那一次**的抓取日 (见 ``terms_fetched_at``)."""
        return terms_fetched_at(self.cache, bond_code, source=self.inner.name)

    def get_bond_terms(self, bond_code, valuation_date):
        cached = self.cache.get(bond_code)
        stale = self.cache.is_stale(bond_code, self.max_age_days) if cached else True

        # 缓存命中且未过期 → 直接返回, 不打网络。``auto_refresh=False`` 时**过期也不回源**
        # (与 CachedBondDataProvider 同名参数同语义) —— 此前这个参数存了不用, 传 False
        # 被静默忽略, 装饰器照样打网络。
        if cached is not None and (not stale or not self.auto_refresh):
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


    # ── ABC 上带默认实现的四个方法必须显式透传 ────────────────────
    # 不透传不会报错, 只会**静默降级成 ABC 的默认值**, 而三个默认值各自都是一句谎话:
    #   authoritative_terms_fields → None  = "全部字段归我", 全量同步整条替换,
    #       把评级同步/状态刷新/事件回写的成果一起清掉 (AGENTS 里评级被盖回去那个坑)
    #   get_admission_status       → 退回 self.get_bond_terms, 而本类的 get_bond_terms
    #       读的是**缓存** —— "刷新状态"当场变成"读旧值", 实测返回的是缓存里的旧条款
    #   list_bond_announcements    → []    , 事件同步报"0 条公告"并安全跳过
    # (list_tradable_cbs 默认抛 NotImplementedError, 是响的那一档, 危害小得多。)
    # backtest_disk_cache / strategy_backtest / historical_terms 三个装饰器都老实透传了,
    # 只有这里漏了; 今天的生产链路走裸 provider 所以没踩到, 是埋着的雷。
    def authoritative_terms_fields(self):
        return self.inner.authoritative_terms_fields()

    def get_admission_status(self, bond_code, valuation_date, base_terms=None):
        return self.inner.get_admission_status(bond_code, valuation_date, base_terms)

    def list_bond_announcements(self, bond_code, start, end):
        return self.inner.list_bond_announcements(bond_code, start, end)

    def list_tradable_cbs(self, on_date=None):
        return self.inner.list_tradable_cbs(on_date)

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

    def get_stock_dividend_yield(self, stock_code, on_date):
        return self.inner.get_stock_dividend_yield(stock_code, on_date)

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
    - get_stock_close / get_stock_history / get_stock_dividend_yield / get_bond_history: 透传到 market provider
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

    def terms_as_of(self, bond_code: str, valuation_date: date) -> date | None:
        """cb_data 里这只债**条款**的抓取日 —— 取 ``static_source`` (全量条款同步) 那一次.

        见 ``terms_fetched_at``; 用全局 ``fetched_at`` 会把条款 patch 整段裁掉。
        """
        return terms_fetched_at(self.cache, bond_code, source=self.static_source.name)

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

    @staticmethod
    def _rederive_trading_metadata(bond_code, terms: BondTerms, valuation_date: date) -> BondTerms:
        """按 valuation_date 重算交易状态字段.

        ``tradable_date`` / ``is_tradable`` / ``trading_status`` 是估值日的函数,
        而 cb_data 里存的是**写入那天**的判断。直接返回缓存值会让"已发行未上市"
        的新债一路顶着旧的 ``tradable`` 标签走完定价链, 关注池因此既看不出它还没
        挂牌, 也丢掉新债高亮。
        """
        try:
            return infer_cb_trading_metadata(bond_code, terms, valuation_date)
        except Exception:
            return terms

    def get_bond_terms(self, bond_code, valuation_date):
        cached = self.cache.get(bond_code)
        stale = self.cache.is_stale(bond_code, self.max_age_days) if cached else True
        if cached is not None and (not stale or not self.auto_refresh):
            return self._rederive_trading_metadata(bond_code, cached, valuation_date)
        try:
            return self._refresh_static_terms(bond_code, valuation_date)
        except Exception as e:
            if cached is not None:
                logger.warning("Wind 刷新 cb_data(%s) 失败 (%s), 沿用缓存", bond_code, e)
                return cached
            raise


    # ── ABC 上带默认实现的四个方法必须显式透传 ────────────────────
    # 不透传不会报错, 只会**静默降级成 ABC 的默认值**, 而三个默认值各自都是一句谎话:
    #   authoritative_terms_fields → None  = "全部字段归我", 全量同步整条替换,
    #       把评级同步/状态刷新/事件回写的成果一起清掉 (AGENTS 里评级被盖回去那个坑)
    #   get_admission_status       → 退回 self.get_bond_terms, 而本类的 get_bond_terms
    #       读的是**缓存** —— "刷新状态"当场变成"读旧值", 实测返回的是缓存里的旧条款
    #   list_bond_announcements    → []    , 事件同步报"0 条公告"并安全跳过
    # (list_tradable_cbs 默认抛 NotImplementedError, 是响的那一档, 危害小得多。)
    # backtest_disk_cache / strategy_backtest / historical_terms 三个装饰器都老实透传了,
    # 只有这里漏了; 今天的生产链路走裸 provider 所以没踩到, 是埋着的雷。
    def authoritative_terms_fields(self):
        return self.static_source.authoritative_terms_fields()

    def get_admission_status(self, bond_code, valuation_date, base_terms=None):
        return self.static_source.get_admission_status(bond_code, valuation_date, base_terms)

    def list_bond_announcements(self, bond_code, start, end):
        return self.static_source.list_bond_announcements(bond_code, start, end)

    def list_tradable_cbs(self, on_date=None):
        return self.static_source.list_tradable_cbs(on_date)

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

    def get_stock_dividend_yield(self, stock_code, on_date):
        return self.market.get_stock_dividend_yield(stock_code, on_date)

    def get_bond_history(self, bond_code, start, end):
        return self.market.get_bond_history(bond_code, start, end)

    def get_risk_free_rate(self, on_date):
        # 不再静默吞错: GUI Shibor 按钮 / 单点定价需要看到底层 Wind/akshare 的诊断,
        # 否则只能看到 "未返回有效无风险利率" 的笼统提示. 已知 batch/wind_sync 调用方
        # 自行做了 try/except, 不会因抛出而崩溃.
        if on_date not in self._risk_free_cache:
            self._risk_free_cache[on_date] = self.market.get_risk_free_rate(on_date)
        return self._risk_free_cache[on_date]

    def hist_vol(self, stock_code, end_date, window_days):
        return self.market.hist_vol(stock_code, end_date, window_days)
