# 从 v1 升级到 CBLens 2.0

适用于 `v1.0.0` 及其后的开发版。当前版本为 `2.0.0rc3`，对应 tag 为 `v2.0.0-rc.3`；从 RC1 / RC2 升级也应保留个人数据。这里的迁移步骤适用于候选版试用；发布亮点见 [版本说明](../CHANGELOG.md)。

## 先备份实际数据

关闭 CBLens GUI、同步命令和正在运行的回测，再把**整个数据目录复制到应用安装目录之外**，保留原副本。这包含关注池、人工覆盖、历史条款和研究结果，不只是行情缓存。

| 使用方式 | 默认数据位置 |
| --- | --- |
| v1 / v2 源码 checkout，含 editable 安装 | `<仓库>/data/` |
| v2 macOS 桌面包 | `~/Library/Application Support/CBLens/data/` |
| v2 Windows 桌面包 | `%APPDATA%\CBLens\data\` |
| v2 Linux 用户安装 | `${XDG_DATA_HOME:-~/.local/share}/CBLens/data/` |
| 设置过 `CBLENS_DATA_DIR` 的 v2 环境 | 该变量指定的目录 |

若曾使用 v1 的 `cb-sync-terms`，另备份 `~/.cb_pricer_cache/`，以及当时通过 `--cache-dir` 指定的目录。该命令写的逐债 `TermsCache` 与 GUI 使用的 `cb_data.json` 是两套存储。

已有桌面包可以运行 `CBLens --diagnose` 查看实际数据位置，具体可执行文件路径见 [安装说明](USAGE.md#桌面-app)。早期开发版的策略快照可能写在应用内部 `data/`，不随用户目录一起备份；替换旧 APP 前还应检查并保存 `strategy_backtest_snapshot.json` 与 `strategy_backtest_snapshots/`。新版本不会自动搜索和搬走旧应用内的文件。

整目录备份至少应保留：`watchlist.json`、`watchlist_pricing_cache.json`、`watchlist_daily/`、`down_reset_overrides.json`、`cb_data.json`、`cb_events.json`、`cb_terms_patches.json`、`cb_data_history/`、`cb_valuation_history.json`，以及策略快照和回测缓存。不存在的文件不必创建；不要因为文件被 Git 忽略就漏备份。参数预设和自行导出的 CSV / PNG 若存放在别处，也应单独保存。

## 安装和包名

Python 分发包名从 `convertible-bond-pricer` 改为 `cblens`，import 名仍是 `convertible_bond`。推荐为 v2 使用独立 checkout 和虚拟环境，保留 v1 环境用于核对与回退。候选版正式发布后，可检出对应 tag；不要把尚在变化的 `main` 当作固定的候选版。

在 v2 checkout 中安装：

```bash
python -m venv .venv
source .venv/bin/activate
# Windows PowerShell 使用：.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -e ".[dev]"
```

若必须复用旧虚拟环境，先完成数据备份，再用该环境的 `python -m pip uninstall convertible-bond-pricer` 移除旧分发包，随后安装 v2。不要让两个分发包共同管理 `convertible_bond`。WindPy 需要在 Wind 终端中为所用 Python 环境安装接口。

只支持源码 editable 安装和桌面包；普通 wheel 不携带条款和图像数据。新源码 checkout 使用的 `data/` 与旧 checkout 独立：把个人关注池和研究记录按下一节迁移，旧库有自定义数据时另外保留并复核。不要用一份旧 `cb_data.json` 无条件盖掉新版修复过的种子库。

## 从源码迁到桌面

桌面包使用上表的用户数据目录，**不会自动读取旧仓库里的关注池**。

1. 备份源目录与目标目录，确认两个版本都已关闭。
2. 若目标目录尚不存在，按上表建立它。把旧目录中的 `watchlist.json` 复制进去；若有 `watchlist_pricing_cache.json` 和 `watchlist_daily/`，一起复制以保留历史记录。
3. 需要保留开发版策略结果时，复制 `strategy_backtest_snapshot.json` 与 `strategy_backtest_snapshots/`。人工 `down_reset_overrides.json` 也需保留。目标已有同名个人文件时先保留两份副本，应用没有自动合并两个关注池或两套人工覆盖的工具。
4. 启动桌面包，核对关注代码和加入日；在可用行情源下刷新关注池与批量结果。旧理论价只代表其保存日期和旧模型口径。

也可让 v2 通过 `CBLENS_DATA_DIR` 指向一份**复制出来的**旧数据目录，但这会沿用其中的旧条款、事件和 patch，需继续执行下节检查。环境变量必须对启动 APP 的进程生效；仅在终端设置它后再从 Finder 双击，不保证 Finder 启动的进程能继承该变量。

种子数据只为缺失或符合损坏判据的文件提供初始内容，不会把有效的旧库自动升级成最新库。因此，“装了新版 APP”不等于“旧公告错误已经修完”。

## 修复旧公告数据

转股价公告可能同时引用多组前后价格。旧解析结果若选错对象，已写入的历史 patch 不会因更新代码自动消失。使用 v2 的 `cb-repair-conversion-prices` 先预览：

```bash
cb-repair-conversion-prices
# 需要补取公告正文时：
cb-repair-conversion-prices --download
```

先核对报告中的转股价、生效日与公告正文，再加 `--apply` 应用；可用 `--codes` 限定债券范围。该工具只重放 cninfo 的转股价原始 patch，保留 Wind 记录、评级和其他字段，写入前会备份。已有当前条款状态仍需按 [日常同步流程](../README.md#-每日研究流) 复核，不能用“重复追加一次公告”替代清理旧错值。

其他余额、回售或评级问题按 `cb-data-doctor` 报告和对应修复命令处理；不要无差别运行所有带 `--apply` 的命令。修复工具自己的文件备份也不能替代升级前的整目录备份。

体检的“末条 patch == 当前值”只比较截至体检日已经生效的最后一条转股价记录。已公告但未来才生效的新 K 不应提前写成当前 K；到了生效日仍有分歧，主池检查仍会报错。

批量缓存的 K 则与该行估值日的历史投影比较，避免把价格调整前的正确旧快照判错。旧缓存仍接受检查，真实错误的历史 K 不会因为缓存陈旧而被忽略。

## Python API 和 CLI

这次大版本保留 `convertible_bond` 的旧公开导入名称，但不承诺旧位置实参、删除的参数或默认行为保持不变。

| v1 用法 | v2 处理 |
| --- | --- |
| `UniversalCBPricer(...)` 用长串位置实参传条款 | 改用关键字，尤其是 `down_reset_premium`、`down_reset_block_until` 和 `call_notice_days`；新增回售参数插入了旧参数顺序。`BondTerms` 也应按字段名构造。 |
| `AdmissionFilterConfig(delist_window_days=...)` | 删除该参数。临近最后交易日的信息通过 `days_to_last_trading` 与风险标签表达；若策略需要距离阈值，在策略层显式设置，不再是准入配置的这个旋钮。 |
| `batch_pricing_exclusion_reason(..., delist_window_days=...)` | 删除该参数；核对新主池与旧名单的差异。 |
| `filter_batch_results_by_view(..., undervalued_score_threshold=...)` | 删除该参数。低估候选改用当期相对便宜程度和分位范围；没有同义的机会分阈值可替换。 |
| `cb-screen-pool --delist-window ...` | 删除该开关。`--min-balance`、`--min-rating`、`--min-turnover` 仍接受显式值，但已从简洁帮助中隐藏。 |
| `cb-sync-terms CODE ...` / `python -m convertible_bond.cli.sync_terms` | 改为 `cb-sync-tradable --codes CODE ...`，写入应用使用的 bundle。旧 `--file`、`--list`、`--cache-dir` 没有逐一改名的等价开关；先提取代码，再用 `--codes`，用 `cb-sync-tradable --info` 检查 bundle。 |

构造示例：

```python
from datetime import date
from convertible_bond.pricer import UniversalCBPricer

pricer = UniversalCBPricer(
    S0=50.0, K=50.0,
    current_date=date(2026, 9, 1), maturity_date=date(2028, 9, 1),
    down_reset_premium=1.02,
    down_reset_block_until=None,
    call_notice_days=30,
)
```

期间开发版的 `--pde-*-band`、`--no-down-reset-event-exit` 和下修优势参数已删除。需要事件退出时显式传 `--down-reset-event-exit`，默认关闭。Python 旧信号 `score`、`down_reset_edge`、`down_reset_robust_edge` 会映射到 `deviation`；CLI 接受后两种兼容别名，但不再接受 `--rank-signal score`，请显式改用 `deviation` 并重新检验策略。

## 旧快照和导出文件

批量 CSV 从 v1 的 21 列变为 35 列，删除 `opportunity_score`，新增 `relative_deviation`、`quality_score`、事件、横截面秩和下修价值等字段。它们不是机会分的数值替代品。CSV 消费程序应按表头名读取并允许额外列，避免按列号解析；当前完整表头见 `convertible_bond.batch_pricing.BATCH_RESULT_COLUMNS`。

开发版的策略快照 schema 1/2 可以继续加载，v2 保存使用 schema 3。加载时会在内存补齐缺失的策略类别、历史口径和回撤日期，原文件不会因此重写；保存的收益曲线也不会自动重算。退休信号可能通过兼容类别显示，核对时以备份中的原始 `config`、`run_settings` 与收益结果为准，不把显示类别当作同策略复现的证据。

快照应放在当前数据目录的 `strategy_backtest_snapshots/`，旧单文件入口是 `strategy_backtest_snapshot.json`。APP 会自动读取这些位置，没有任意路径的快照导入向导。正常保存只保留最近 8 份目录快照；长期研究记录请放在应用管理目录之外，避免被后续保存清理。CSV 只是导出结果，不能作为策略快照直接导回。

升级后保留旧 CSV / 快照作为原始记录，另开新运行，按版本区分输出文件。不要把新旧理论价、排名或收益序列拼成一条连续的同口径历史。

## 模型口径与旧研究结果

- Provider 驱动定价的背景下修强度默认由 0.15/年改为 0.25/年，与 GUI / 策略默认一致；这是触发线下方的年化强度，不能直接读作一年内无条件下修概率。
- 单债回测默认 `point_in_time=True`，叠加历史条款与公告投影。回售、已公告强赎、评级利差下限和下修价下限也已统一进入定价链；只固定旧 `p_down` 不能完整恢复 v1 的结果。缺少历史证据仍可能用到当前静态字段，应检查条款来源诊断。
- 机会分退役，候选改按当期横截面口径筛选；旧策略的选债集合、排名和收益都需要重新检验。
- 下修价下限按初始股价冻结，折点附近的 Gamma 可能为负或随网格步长明显漂移。当前不保证 Gamma 收敛，不应直接把它用于对冲或精细风险限额。
- akshare 的股息率兜底可能取实时快照，未必代表历史估值日；股息率没有跨运行磁盘缓存。需要可重复的情景对比，可在 CLI 显式传固定 `--q`（小数制），例如 `--q 0`，并记录假设。这能跳过股息率取数，但不等于历史股息率已知或真实为 0。单债 GUI 回测沿用定价页固定的 `q`。

模型输出用于研究与复核。即使数值计算成功、回测有超额，也仍需检查数据来源、参数稳定性与样本外表现。

## 需要回退时

关闭 v2 并备份本次新增的个人记录。切回保留的 v1 checkout / 虚拟环境，或重新安装此前使用的桌面包，再将升级前备份恢复到它原来使用的数据位置。恢复时使用**完整的对应备份**，不要让旧代码继续覆盖 v2 刚写的混合数据；同时恢复此前的环境变量设置。

旧版不能完整解释 v2 新增的事件字段和研究口径，当前没有 v2 → v1 的自动 schema 降级命令。若升级后加入了新关注，可根据另存的记录在回退环境中手工补回；保留 v2 的独立备份，避免覆盖新研究成果。
