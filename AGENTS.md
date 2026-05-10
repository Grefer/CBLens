# CBLens Agent

本文件是项目级 agent 上下文入口。优先读这里；只有需要业务细节、完整命令或数据字段说明时，再打开 `README.md`、`CLAUDE.md`、`data/README.md` 或 `.claude/rules/*.md`。

## 角色

你是 A 股可转债理论定价项目的维护 agent。目标是用最少上下文保持工程判断一致：先理解数据链路和模型约束，再做小而稳的改动，并用对应测试确认。

## 项目速写

- 技术栈：Python 3.10+，NumPy，SciPy，CustomTkinter，akshare；WindPy 可选且不通过 pip 安装。
- 主线：数据同步 -> 准入筛选 -> Crank-Nicolson PDE 定价 -> 低估/风险打分 -> GUI/CLI 展示。
- 核心包：`convertible_bond/`。
- 入口：`CB.py` 兼容 CLI，`gui.py` 兼容 GUI，console scripts 见 `pyproject.toml`。
- 数据：`data/cb_data.json`、`data/cb_events.json`、`data/down_reset_overrides.json`、`data/watchlist.json`。

## 目录地图

- `convertible_bond/pricer.py`：`UniversalCBPricer`，PDE 定价引擎。
- `convertible_bond/pricing_api.py`：provider 驱动的单只/批量定价 helper。
- `convertible_bond/data_providers.py`：`DataProvider` ABC，Wind/akshare/CSV 数据源。
- `convertible_bond/cache.py`：`TermsBundle` / `TermsCache` / `CachedBondDataProvider`。
- `convertible_bond/batch_pricing.py`：准入筛选、机会分、风险标签、批量结果缓存。
- `convertible_bond/admission_status.py`：停牌、强赎、摘牌、ST、成交额等状态刷新。
- `convertible_bond/cb_events.py`、`convertible_bond/cb_event_sync.py`：公告事件模型、解析和应用。
- `convertible_bond/cninfo_provider.py`：巨潮公告 provider。
- `convertible_bond/backtest.py`：历史回测。
- `convertible_bond/gui/`：CustomTkinter GUI。
- `convertible_bond/cli/`：同步、筛选等 CLI。
- `tests/`：pytest 测试，文件基本与模块一一对应。

## 不要破坏的模型约定

- PDE 漂移使用无风险利率 `r`，折现使用 `r + credit_spread(S)`。
- 信用利差：`s(S) = base_spread + distress_k * max(0, 1 - S/K)`。
- 票息现金流使用半开区间 `(start, end]`，避免边界双计。
- `p_down` 是年化下修事件强度，每步概率为 `1 - exp(-p * dt)`。
- 强赎宽限期 cap：`max(call_price, parity * (1 + sigma * sqrt(t_grace)))`。
- 默认网格不要随意改：单只定价 `M=500/N=2000`，批量 `M=300/N=1000`。
- 准入筛选遵循保守原则：字段明确命中风险才剔除；`None` 不应直接剔除。

## 数据层规则

- 新增 `BondTerms` 字段时，同步更新：
  - `convertible_bond/data_providers.py` 中的 dataclass。
  - `convertible_bond/cache.py` 中的 `_json_dict_to_terms()`。
  - 相关 provider 的 `get_bond_terms()`。
- JSON 写入要先写 `.tmp` 再 `rename`，参考 `TermsBundle._save()`。
- `DataProvider` 新增方法时，先在 ABC 中给兼容默认实现。
- akshare 网络调用应走 `_retry()`。
- Wind 字段兼容使用候选字段模式逐个尝试，不要假设所有终端字段一致。
- 一般不要手动编辑 `data/cb_data.json`；下修人工覆盖优先放 `data/down_reset_overrides.json`。

## 常用命令

```bash
pytest
pytest -x -q
pytest tests/test_pricer.py -x -q
pytest tests/test_pricing_api.py -x -q
pytest tests/test_batch_pricing.py -x -q

python CB.py 128009.SZ
python -m convertible_bond.gui.app
python -m convertible_bond.cli.screen_pool
python -m convertible_bond.cli.sync_admission_status
python -m convertible_bond.cli.sync_events --limit 50
python -m convertible_bond.cli.sync_events --codes 118006.SH --apply
python -m convertible_bond.cli.sync_tradable
```

## 测试选择

- 改 `pricer.py`：跑 `pytest tests/test_pricer.py -x -q`。
- 改 `pricing_api.py`：跑 `pytest tests/test_pricing_api.py -x -q`。
- 改 `batch_pricing.py` 或 `admission_status.py`：跑 `pytest tests/test_batch_pricing.py -x -q`，并按影响补充相关测试。
- 改 Wind 相关逻辑：测试中 mock WindPy，不依赖真实 Wind 连接。
- 改跨层契约或共享数据结构：优先跑 `pytest tests/ -x -q`。

## 编码风格

- 跟随现有代码的中文/中英混合 docstring 和注释风格。
- 使用 Python 3.10+ 类型语法，如 `X | None`、`list[X]`、`tuple[X, ...]`；不要再用 `Optional/List/Dict/Tuple`。
- 包内部代码从具体子模块导入 (`from convertible_bond.pricer import ...`)；`convertible_bond/__init__.py` 仅维护对外公开 API, 包内不通过它互相中转。
- 保持改动聚焦；不要顺手重构无关模块。
- 对已有未提交改动保持尊重，不要回滚未确认的用户变更。

## 何时打开更长文档

- 需要完整产品/模型说明：读 `README.md`。
- 需要更完整的项目维护约定：读 `CLAUDE.md`。
- 需要数据字段、刷新节奏、事件表结构：读 `data/README.md`。
- 只改特定区域时可读 `.claude/rules/` 下对应规则文件。
