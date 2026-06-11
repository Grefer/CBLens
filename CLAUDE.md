# CBLens — 可转债理论定价引擎

## 项目概述

A 股可转债理论定价系统，完整链路：数据同步 → 准入筛选 → Crank-Nicolson PDE 批量定价 → 低估/风险打分 → GUI/CLI 展示。

核心技术栈：Python 3.10+, NumPy, SciPy, CustomTkinter, akshare, WindPy (可选)。

## 架构速查

参考 @README.md 了解完整特性与使用方法。

### 目录结构 (26000+ 行)

```
CBLens/
├── convertible_bond/           # 主包
│   ├── pricer.py               # PDE 定价引擎 (UniversalCBPricer)
│   ├── pricing_api.py          # price_from_provider / batch_price 高级 API + _BatchStockCache
│   ├── data_providers/         # DataProvider 包 (base ABC, wind, akshare, csv_provider, auto)
│   ├── cninfo_provider.py      # 巨潮资讯网公告 Provider
│   ├── cache.py                # TermsBundle/TermsCache + CachedBondDataProvider
│   ├── batch_pricing.py        # 准入筛选 + 研究打分 + 结果缓存
│   ├── backtest.py             # 单债历史回测 (模型 vs 市场偏差)
│   ├── strategy_backtest.py    # 选债策略回测核心 (调仓/持仓/基准/归因, 2600 行)
│   ├── backtest_disk_cache.py  # DiskCacheProvider: 回测取数跨运行磁盘缓存
│   ├── historical_terms.py     # 历史条款投影 (TermsPatchStore + 事件重建, 防未来信息)
│   ├── market_valuation.py     # 转债大类估值/择时信号 (中位偏差 + 历史分位)
│   ├── signal_eval.py          # 信号检验 (Rank-IC / 分位收益 / 截面 zscore)
│   ├── cb_events.py            # 事件模型与解析
│   ├── cb_event_sync.py        # 公告 → 事件同步
│   ├── cb_data_sync.py         # Wind → cb_data.json 同步
│   ├── admission_status.py     # 停牌/强赎/ST 状态刷新
│   ├── down_reset_overrides.py # 下修覆盖 + 三 regime 强度解析
│   ├── watchlist.py            # 关注池管理
│   ├── gui/                    # CustomTkinter GUI
│   │   ├── app.py              # CBPricerApp: 多 mixin 组装
│   │   ├── controllers/        # 业务域 mixin; 策略回测已按职责拆为
│   │   │                       #   strategy_{setup,run,snapshots,render,
│   │   │                       #   render_analysis,compare,common} 7 模块,
│   │   │                       #   strategy_backtest.py 仅为聚合入口
│   │   └── tabs/               # 各页 UI 构建 (batch/pricing/backtest/strategy/...)
│   └── cli/                    # CLI 工具 (screen_pool, sync_*, valuation, strategy_backtest)
├── data/                       # 持久化数据 (cb_data.json, cb_events.json, ...)
├── tests/                      # pytest 测试 (380+)
├── CB.py                       # CLI 兼容入口
├── gui.py                      # GUI 兼容入口
└── pyproject.toml              # 包定义 + ruff 配置 (E9+F, CI 阻塞)
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
- **类型标注**: 使用 Python 3.10+ 语法 (`X | None` / `list[X]` / `tuple[X, ...]` / `dict[X, Y]`); 不要再用 `Optional/List/Dict/Tuple`
- **导入**: 包内部代码从子模块直接导入; `convertible_bond/__init__.py` 仅作为对外公开 API 的聚合入口
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
2. 对应 Provider 的 `get_bond_terms()` 实现

序列化无需手动登记: `cache.py` 中的 `_json_dict_to_terms()` 通过 `dataclasses.fields(BondTerms) + get_type_hints` 自动识别 `date` 与 `tuple` 字段; 只要字段类型注解写对就会被正确反序列化。

### PDE 引擎要点

- 漂移用无风险利率 r，折现用 r + credit_spread(S)
- 信用利差 distress 扩张: `s(S) = base_spread + distress_k · max(0, 1 - S/K)`
- 强赎宽限期: `cap = max(call_price, parity · (1 + σ√t_grace))`
- 默认网格: M=500, N=2000 (单只); M=300, N=1000 (批量)

## 测试与静态检查

```bash
pytest                    # 全部测试 (380+, ~5s)
pytest -x -q              # 快速失败
pytest -k "down_reset"    # 按关键词
ruff check convertible_bond tests CB.py gui.py scripts  # lint (E9+F, CI 阻塞)
```

修改 pricer.py / pricing_api.py / batch_pricing.py 后必须运行 `pytest -x` 确认无回归。
ruff 只启用正确性规则 (语法错误/未定义名/未用导入); F821 是 GUI 代码的静态防线 —
CustomTkinter 在测试环境跑不起来, 运行期 NameError 靠它兜底, 不要绕过。

GUI controller 大改后跑 `pytest -k composition` (组成性守护: mixin 无命名冲突 +
UI 入口齐全), 并提醒用户人工启动 cb-gui 冒烟 — 自动测试覆盖不到真实渲染路径。

## 数据文件

- `data/cb_data.json` — 全部转债静态条款，不要手动编辑
- `data/cb_data_history/` — 按日期归档的条款快照 (历史回测取数)
- `data/cb_events.json` — 结构化事件表
- `data/cb_terms_patches.json` — 历史条款 patch (回测防未来信息)
- `data/cb_valuation_history.json` — 大类估值历史基线 (批量重算自动追加, 入版本库)
- `data/down_reset_overrides.json` — 人工下修覆盖 (可手动编辑)
- `data/batch_pricing_cache.json` — 批量定价缓存 (运行态, gitignored)
- `data/watchlist.json` — 关注池 (运行态, gitignored)
- `data/strategy_backtest_snapshots/`, `data/strategy_backtest_cache/` — 策略回测
  快照与跨运行磁盘缓存 (运行态, gitignored)

## CLI 入口

```bash
cb-gui                                      # GUI
cb-screen-pool --min-rating AA-             # 准入筛选报告
cb-sync-tradable                            # 全量同步基础条款
cb-sync-admission-status --limit 50         # 刷新状态
cb-sync-events --apply                      # 同步公告事件并应用回 cb_data
cb-valuation                                # 大类估值/择时信号 (--record 入基线)
cb-strategy-backtest --start 2025-01-01 --end 2026-01-01 --freq M  # 策略回测 (--cache-dir 复跑提速)
cb-calibrate-down-reset                     # 从 cb_events 校准下修博弈常量
python CB.py 128009.SZ                      # 单只定价
```

### 下修博弈建模 (三 regime, 按价格影响符号分)

`resolve_down_reset_intensity` 把观测合成成 pricer 入参, 三态互斥:

- **背景** (无确定性公告): "纯触发后"模型 — 触发线下方 (S < K·trigger_ratio) 一律按 `p_down` 年化概率下修 (每步 `1-exp(-p·dt)`, 网格无关), 触发线之上为 0。`p_down` = "触发后公司跟进下修"的年化概率; 不用"越跌越可能"的 S 渐变。
- **已公告** (确定性正贡献): 输出 `scheduled_reset_date/prob/kind/target_k` 一次性下修节点, pricer 在预期生效日近确定施加, 不再放大背景强度。两个子态:
  - `kind="proposed"` 待股东会: 生效日 = 提议日+`PROPOSED_EFFECTIVE_LAG_DAYS`, 概率 `PROPOSED_PASS_PROB`。
  - `kind="approved"` 已通过待生效: 生效日 = 公告生效日 (缺失按 `APPROVED_EFFECTIVE_LAG_DAYS` 兜底), 概率 `APPROVED_PASS_PROB`≈1; **仅当生效日 > 估值日才建节点 (防与条款刷新双计)**。
  - `target_k` = 公告解析到的下修后新 K (`parse_down_reset_new_price` 填 `CBEvent.event_price`); 缺失时 pricer 回落 premium/floor 估算。`target_k==现 K` 时节点自动成 no-op, 天然防双计。
- **冻结** (强制为 0): `down_reset_block_until` 屏蔽下修价值至冷静期满。
- 常量经 `cb-calibrate-down-reset` 从历史事件校准; 改这些值或下修结构前先重跑校准。

> **关于 `event_price` 历史回填**: 不需要。`cb_data.json` 的 `conversion_price` 是 Wind
> `clause_conversion2_swapshareprice` 即**当前 K**, 已内含所有"已生效"的历史下修; 这些历史
> 事件也不会触发 regime-② 节点 (terminal/过期, 或 approved 生效日已过被守卫跳过)。`event_price`
> 仅服务**在途公告** (已提议未通过 / 已通过未生效) — 此时 cb_data K 仍是旧值, 节点需公告新 K。
> 存量事件 `event_price` 多为空 (解析代码后加), 但无害; 新公告同步时自动填充。
