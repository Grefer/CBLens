# ConvertibleBond — 可转债理论定价引擎

## 项目概述

A 股可转债理论定价系统，完整链路：数据同步 → 准入筛选 → Crank-Nicolson PDE 批量定价 → 低估/风险打分 → GUI/CLI 展示。

核心技术栈：Python 3.10+, NumPy, SciPy, CustomTkinter, akshare, WindPy (可选)。

## 架构速查

参考 @README.md 了解完整特性与使用方法。

### 目录结构 (9200+ 行)

```
ConvertibleBond/
├── convertible_bond/           # 主包
│   ├── pricer.py               # PDE 定价引擎 (UniversalCBPricer)
│   ├── pricing_api.py          # price_from_provider / batch_price 高级 API
│   ├── data_providers.py       # DataProvider ABC + Wind/Akshare/CSV 实现
│   ├── cninfo_provider.py      # 巨潮资讯网公告 Provider
│   ├── cache.py                # TermsBundle/TermsCache + CachedBondDataProvider
│   ├── batch_pricing.py        # 准入筛选 + 研究打分 + 结果缓存
│   ├── backtest.py             # 历史回测
│   ├── cb_events.py            # 事件模型与解析
│   ├── cb_event_sync.py        # 公告 → 事件同步
│   ├── cb_data_sync.py         # Wind → cb_data.json 同步
│   ├── admission_status.py     # 停牌/强赎/ST 状态刷新
│   ├── down_reset_overrides.py # 下修覆盖
│   ├── watchlist.py            # 关注池管理
│   ├── gui/                    # CustomTkinter GUI (app.py, theme.py, widgets.py, tabs/)
│   └── cli/                    # CLI 工具 (screen_pool, sync_*)
├── data/                       # 持久化数据 (cb_data.json, cb_events.json, ...)
├── tests/                      # pytest 测试
├── CB.py                       # CLI 兼容入口
├── gui.py                      # GUI 兼容入口
└── pyproject.toml              # 包定义
```

### 五层架构

1. **基础信息层**: WindPy → `data/cb_data.json` (TermsBundle)
2. **事件状态层**: `cb_events.json` + admission_status 刷新
3. **动态行情层**: Wind/akshare 提供正股价/波动率/利率
4. **模型定价层**: `UniversalCBPricer` (Crank-Nicolson PDE)
5. **筛选打分层**: opportunity_score / risk_tags / confidence / review_bucket

### 核心 API

```python
# 定价引擎 (不依赖数据源)
from convertible_bond.pricer import UniversalCBPricer
pricer = UniversalCBPricer(S0, K, current_date, maturity_date, ...)
theo = pricer.price(sigma, r, base_spread, return_greeks=True)

# Provider 驱动定价
from convertible_bond.pricing_api import price_from_provider, batch_price_from_provider_threaded

# 数据源
from convertible_bond.data_providers import DataProvider, WindDataProvider, AkshareDataProvider

# 缓存
from convertible_bond.cache import TermsBundle, CachedBondDataProvider, project_bundle_path
```

## 编码规范

### 语言与风格

- **语言**: 代码、注释、docstring 使用中文或中英混合 (项目惯例)
- **类型标注**: 使用 Python 3.10+ 语法 (`X | None` 而非 `Optional[X]`)
- **导入**: 从子模块直接导入，不通过 `__init__.py` 中转
- **文档**: 每个模块开头有中文 docstring 说明职责

### 关键设计模式

- **Provider 装饰器链**: `Wind/Akshare → CachingDataProvider → CachedBondDataProvider → _BatchStockCache`
- **保守过滤**: 准入筛选"字段明确才剔除"，避免因数据源缺字段误杀
- **半开区间票息**: `(start, end]` 避免边界双计
- **年化强度**: p_down 解释为年化事件强度，每步 `1-exp(-p·dt)`
- **原子写**: JSON 先写 `.tmp` 再 `rename`，防半截文件
- **鸭子类型缓存**: TermsBundle/TermsCache 共用接口 `has/get/set/list_bonds/fetched_at/is_stale/delete`

### BondTerms 字段约定

`BondTerms` dataclass 有 30+ 字段。新增字段需要同步更新:
1. `data_providers.py` 中的 `BondTerms` dataclass
2. `cache.py` 中的 `_json_dict_to_terms()` 反序列化函数
3. 对应 Provider 的 `get_bond_terms()` 实现

### PDE 引擎要点

- 漂移用无风险利率 r，折现用 r + credit_spread(S)
- 信用利差 distress 扩张: `s(S) = base_spread + distress_k · max(0, 1 - S/K)`
- 强赎宽限期: `cap = max(call_price, parity · (1 + σ√t_grace))`
- 默认网格: M=500, N=2000 (单只); M=300, N=1000 (批量)

## 测试

```bash
pytest                    # 全部测试
pytest -v                 # 详细
pytest -x -q              # 快速失败
pytest -k "down_reset"    # 按关键词
```

修改 pricer.py / pricing_api.py / batch_pricing.py 后必须运行 `pytest -x` 确认无回归。

## 数据文件

- `data/cb_data.json` — 全部转债静态条款 (~257KB)，不要手动编辑
- `data/cb_events.json` — 结构化事件表
- `data/down_reset_overrides.json` — 人工下修覆盖 (可手动编辑)
- `data/batch_pricing_cache.json` — 批量定价缓存 (~440KB)
- `data/watchlist.json` — 关注池

## CLI 入口

```bash
cb-gui                                      # GUI
cb-screen-pool --min-rating AA-             # 准入筛选报告
cb-sync-admission-status --limit 50         # 刷新状态
python -m convertible_bond.cli.sync_events  # 同步事件
python -m convertible_bond.cli.sync_tradable # 同步可交易列表
python CB.py 128009.SZ                      # 单只定价
```
