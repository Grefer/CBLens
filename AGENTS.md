# CBLens — 可转债理论定价引擎

## 项目概述

A 股可转债理论定价系统，完整链路：数据同步 → 准入筛选 → Crank-Nicolson PDE 批量定价 → 低估/风险打分 → GUI/CLI 展示。

核心技术栈：Python 3.10+, NumPy, SciPy, CustomTkinter, akshare, WindPy (可选)。

## 角色与协作约定

维护 agent 的默认工作方式：先理解数据链路和模型约束，再做小而稳的改动，并用对应测试确认。

- 保持改动聚焦，不要顺手重构无关模块。
- 尊重工作区里已有的未提交改动，不要回滚未经确认的用户变更。
- 需要完整产品/模型说明读 `README.md`；数据字段与刷新节奏读 `data/README.md`。

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
- **标签按维度归类, 消费者各取所需**: `RISK_TAG_DIMENSION` 把标签分成 数据质量 /
  模型适用性 / 标的风险 / 可交易性 / 机会信号 五维。新增标签**必须同时登记维度**
  (有守护测试扫 `risk_tags.append` 字面量比对)。批量页视图只拿"数据质量+可交易性"
  当拦截集 —— 模型适用性是永久属性, 塞进需复核会让它变成 79% 的垃圾桶。
  视图归属的单一事实源是 `view_exclusion_reason`, 策略页的落选解释也走它;
  两边曾各自实现一份并在重构后悄悄分叉。
- **`LEGACY_STRATEGY_EXCLUDE_TAGS` 是冻结集, 不要跟着标签体系演化**: 它是
  `ScoreStrategyConfig.exclude_risk_tags` 的默认值。曾写成 `tuple(sorted(HARD_REVIEW_TAGS))`,
  于是任何为改展示而增删标签的动作都自动变成**默认选债行为变更**。实测该集合极敏感:
  改成只排"数据质量+可交易性"候选池 59 → 262, 单去掉「偏差异常」→ 125。要改它单独立项。
- **保守过滤**: 准入筛选"字段明确才剔除"，避免因数据源缺字段误杀。**连续量不做硬阈值**:
  余额已从硬过滤降级为风险标签 (`DEFAULT_MIN_OUTSTANDING_BALANCE=None`) —— 硬阈值把
  "值得警惕"错误表达成"不存在", 一个字段解析错就让券无声消失; 而它此前 99% 的实际作用
  是替缺失的 `delisting_date` 兜底, 全库回填后独立贡献实测为 0。摘牌该由摘牌判据管
- **横截面口径 vs 绝对阈值**: 凡是判据架在"模型偏差"这类**水平时变**的量上, 一律锚当期
  横截面 (相对全市场中位), 不要用绝对阈值。实测 `cb_valuation_history` 20 期季度基线:
  中位偏差水平摆幅 21.2pp (+0.4%↔+21.6%), 而便宜尾形状 (p25−中位) 摆幅只有 4.2pp ——
  水平不可跨期比较, 形状可以。批量页「低估候选」曾用 `opportunity_score >= 8.0`,
  2026-08 主池 280 只只剩 1 只候选、页面默认打开是空表。现改为两道闸串联:
  `MIN_RELATIVE_CHEAPNESS` (比中位便宜 ≥5pp, 负责表达"今天真没便宜货") +
  `DEFAULT_UNDERVALUED_PERCENTILE` (名单长度上限, 负责挡住谷底时的几百只)。
  缺任一道都会退化 —— 纯分位永远凑得满 15%, 纯下限在谷底会泛滥。
- **机会分不是机会**: `opportunity_score` 的低估项是 `max(0, -deviation)*100`, 而全市场
  中位 deviation 长期为正, 于是**92% 的行这一项恒为 0**, 分数完全由评级/余额加分与风险
  惩罚决定 —— 它在九成的债上度量的是信用质量 (秩相关 score vs deviation 仅 −0.63,
  纯错定价排序应为 −1.0)。批量页展示已改按相对偏差排序, `quality_score` 把这部分单列。
  **但 `opportunity_score` 的数值本身冻结不动**: 它是 `rank_signal="score"` 与旧策略
  快照可复现的前提, 改它就是默认选债行为变更。展示排序另走 `sort_batch_results_for_view`。
- **半开区间票息**: `(start, end]` 避免边界双计
- **年化强度**: p_down 解释为年化事件强度，每步 `1-exp(-p·dt)`
- **原子写**: JSON 先写 `.tmp` 再 `rename`，防半截文件
- **鸭子类型缓存**: TermsBundle/TermsCache 共用接口 `has/get/set/list_bonds/fetched_at/is_stale/delete`
- **市场口径的"今天"**: 一律用 `market_time.market_today()` (Asia/Shanghai), 不要用
  `date.today()` — 后者跟着运行机器时区走, 非东八区 (如美西) 会让估值日、公告同步窗口、
  准入判断整体错开一天, 且静默不报错。落盘元信息 (`saved_at`/`fetched_at` 这类本机挂钟
  时间戳) 才继续用 `datetime.now()`。`tests/test_market_time.py` 有守护测试扫描裸 `date.today()`；
  数据源返回的时间戳同理按 `EXCHANGE_TZ` 换算 (例: 巨潮 `announcementTime` 是北京时间毫秒戳)。

### BondTerms 字段约定

`BondTerms` dataclass 有 30+ 字段。新增字段需要同步更新:
1. `data_providers.py` 中的 `BondTerms` dataclass
2. 对应 Provider 的 `get_bond_terms()` 实现

序列化无需手动登记: `cache.py` 中的 `_json_dict_to_terms()` 通过 `dataclasses.fields(BondTerms) + get_type_hints` 自动识别 `date` 与 `tuple` 字段; 只要字段类型注解写对就会被正确反序列化。

**`issue_date` = 起息日 (发行首日), 不是上市日**。Wind 侧取 `carrydate` (次选
`issue_firstissue`); `ipo_date` 是**上市首日**, 只喂 `listing_date`。判据: 到期日恒为
起息日的整周年 (全库 1046/1047), 对 `ipo_date` 则 0/400; 两者中位差 25 天, 且已发行
未上市的新债 `ipo_date` 为空 (用它会让新债缺发行日直接无法定价)。
因此: 票息期/应计利息锚 `issue_date`; "能不能买到" (回测防前视、准入) 锚 `listing_date`。

### 数据层规则

- `DataProvider` 新增方法时，先在 `data_providers/base.py` 的 ABC 里给兼容默认实现，避免打断
  provider 装饰器链。
- akshare 网络调用一律走 `_retry()`，不要裸调。
- **Wind 的能力边界是实测过的, 不要凭印象扩大**:

  | 字段 | Wind wsd 日序列 | 能否重建历史 |
  | --- | --- | --- |
  | 转股价 `clause_conversion2_swapshareprice` | 真 as-of, 有变化点 | ✅ |
  | 未转股余额 `outstandingbalance` | 真 as-of | ✅ |
  | 债项评级 `creditrating` | **恒定 = 当前值** (实测 881 天一个值) | ❌ 无源 |
  | 摘牌日 `delist_date` | 恒定 | ❌ |
  | 强赎状态 / 最后交易日 | wsd 取不到 | ❌ |
  | 公告事件 | `list_bond_announcements` 实测返回 **0 条** (cninfo 同期 28 条) | ❌ |

  所以转股价与余额已由 `cb-rebuild-terms-patches` 从 Wind 重建 (链自洽 100%), 解析结果
  被 `_drop_shadowed_patches` 逐字段屏蔽、只作无 Wind 口径的兜底; 而**评级历史、承诺期
  (不下修/不强赎)、下修提议、强赎公告只能靠 cninfo + 正文解析** —— 那里才是数据体检该盯的地方。
- **数值字段重建要区分"事件"与"漂移"**: 转股价每次变化都是一次公告 (全留);
  未转股余额随转股进度逐日微动 (实测广核转债 274 个交易日变 105 次、全程只动 0.016%),
  照单全收会生成十万条无决策含义的 patch。按决策边界 (0/0.3/0.5/1.0/10 亿) + 1% 相对变动
  过滤, 压缩 92% 而真实缩量一步不落。比较基准取**上次落库值**而非前一日, 否则连续微动
  会被逐段吞掉、累积成大偏移。
- **公告解析里"条款文字"与"当期状态"必须区分**。赎回/回售/停止交易条款会成段引用
  "未转股余额少于 3,000 万元时公司有权赎回", 早期宽松正则把它当成真实余额, 让 546 条
  余额 patch 里 528 条值恰为 0.3 亿、覆盖 103 只债 (96 只真实余额 ≥0.5 亿), 这些大盘券
  随后被准入的 `min_outstanding_balance` 整批当成"余额过小"踢出主池 (主池 217 → 修复后
  283)。判据必须是**措辞**而不是数值 —— 真实披露的"未转股余额为 3,000 万元"仍要正常解析。
  见 `parse_outstanding_balance_change` 的两道语义闸 (gap 比较词 / 金额尾缀), 存量数据用
  `cb-repair-balance-patches` 回洗。新增任何"从公告正文抽数值"的解析器时先想清楚:
  这句话说的是**已经发生的状态**, 还是**触发条件**?
- **公告关键词是多义词, 分类前先确认主语是本转债**。「摘牌」「提前赎回」在 A 股公告里同时
  指: 优先股赎回摘牌、产权交易所公开摘牌 (竞拍取得股权)、普通公司债兑付摘牌、可交换债换股
  摘牌、理财产品提前赎回。早期分类器只看关键词, 把这些统统判成本债强赎/摘牌, 经
  `apply_events_to_bundle` 写进 `last_trading_date`/`delisting_date`, 让 12 只在市转债
  (含兴业、上银两只银行转债) 被准入整体判死。`classify_announcement_title` 现对
  delisting/call_redemption/call_no_redemption 三类要求标题出现转债标识
  (`转债|可转换公司债券|转[0-9]{1,2}`; **`公司债券` 不算** —— 「可转换公司债券」含它但反之不成立,
  而 `转2/转02` 简称不含「债」字必须单列)。
- **归属判据有三层, 缺一层就有一类债漏网**:
  1. `_title_names_other_bond` — 标题点名了兄弟债 (胜蓝转债 ≠ 胜蓝转02)。守卫必须在
     **建事件之前**, 只挡 patch 不够: 事件本身会回写 BondTerms 状态。
  2. `_event_postdates_listing` — 上市之前不可能发生本债的摘牌/强赎/回售/转股价调整。
     对**同名先后两只债**是唯一判据 (110099.SH 福能转债 2025-10-30 上市, 却挂着上一只
     同名债 2024-11 的到期摘牌公告)。例外: 评级 (初始评级本就早于上市) 与正股类事件。
  3. 强赎事件只有**真解析到**停止交易日才写 `last_trading_date` —— `effective_start`
     无日期时回落成公告日, 而公告到停牌之间隔着法定提示期, "最后交易日 = 公告当天"
     恒为解析失败的信号。
- **评级历史无权威源, 只能靠与 cb_data 当前值比对**。末条 patch 必须等于当前值 (中间历史值
  本就该不同)。`_parse_bond_credit_rating` 的 `rating_re` 带 `(?<![A-C])` 左界 —— 少了它,
  `.{0,10}` 回溯会让评级"尽量晚开始", 从 AA- 抠出 A-、AA+ 抠出 A+, 低评级让债在回测准入
  里被整批误杀。`cb-repair-events` 只删这类**可证残缺**的末条 patch, 不猜一个对的填回去。
- Wind 字段用"候选字段逐个尝试"的兼容模式，不要假设所有终端字段一致
  (例: `carrydate` → `issue_firstissue`)。

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

### 按改动选测试

| 改动模块 | 先跑 |
| --- | --- |
| `pricer.py` | `pytest tests/test_pricer.py -x -q` |
| `pricing_api.py` | `pytest tests/test_pricing_api.py -x -q` |
| `batch_pricing.py` / `admission_status.py` | `pytest tests/test_batch_pricing.py -x -q` |
| `strategy_backtest.py` / GUI 策略页 | `pytest tests/test_strategy_backtest.py -x -q` |
| Wind 相关逻辑 | mock WindPy，不依赖真实连接 |
| 跨层契约或共享数据结构 | `pytest -x -q` 全量 |

### 数据体检 (`cb-data-doctor`)

每条检查都对应一次**靠运气才发现**的静默事故 —— 不抛异常、测试全绿, 只表现为"池子里
怎么全是边角料"。所以判据一律是可量化的比率与不变量。分五组: 新鲜度 / 覆盖率 /
patch 自洽 / 交叉校验 / 不变量, 外加 `--online` 的**外部对照**。

外部对照是最强的一条: 库内自洽性检查全绿时, 它靠"被准入判死、但今日 akshare 有成交"
一次抓出 19 只被误杀的活券 (精测转2 431 元、胜蓝转02 326 元), 占主池 6.8%。判据是
**今日成交量 > 0** 而不是"有没有报价" —— akshare 现货表里退市券仍留着零成交的陈旧行。

改解析器或分类器之后必跑 `cb-data-doctor`: "代码修好了"和"存量数据是对的"是两回事,
存量事件不会被任何流程重新审视。

## 数据文件

- `data/cb_data.json` — 全部转债静态条款，不要手动编辑
- `data/cb_data_history/` — 按日期归档的条款快照 (历史回测取数)
- `data/cb_events.json` — 结构化事件表
- `data/cb_terms_patches.json` — 历史条款 patch (回测防未来信息)
- `data/cb_valuation_history.json` — 大类估值历史基线 (批量重算自动追加, 入版本库;
  每条带 `caliber` 口径标记, 缺失视为 `v1`, 见 `market_valuation.CALIBER_CHANGES`)
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
cb-sync-admission-status                    # 刷新状态 (全库 1058 只约 30min; --limit 仅调试用)
cb-sync-events --apply                      # 同步公告事件并应用回 cb_data
cb-valuation                                # 大类估值/择时信号 (--record 入基线)
cb-strategy-backtest --start 2025-01-01 --end 2026-01-01 --freq M  # 策略回测 (--cache-dir 复跑提速)
cb-calibrate-down-reset                     # 从 cb_events 校准下修博弈常量
cb-data-doctor                              # 数据体检 (每天跑批**前**先跑; --online 加外部对照)
cb-repair-events --apply                    # 存量迁移: 用当前解析器重放事件表与 patch 库
cb-repair-balance-patches --apply           # 一次性存量迁移: 清洗被赎回门槛条款污染的余额 patch
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
