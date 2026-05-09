<div align="center">
  <img src="../assets/cblens-icon.png" alt="CBLens" width="80" />
  <h1>CBLens 使用文档</h1>
  <p>安装 · 数据源 · GUI · CLI · Python API · 排障</p>
</div>

---

本文面向第一次运行和日常维护 CBLens 的使用者。更底层的数据字段说明见 [`data/README.md`](../data/README.md)，维护约定见 [`AGENTS.md`](../AGENTS.md)。

## 目录

- [1. 安装](#1-安装)
- [2. 数据源分工](#2-数据源分工)
- [3. 首次使用流程](#3-首次使用流程)
- [4. GUI 使用](#4-gui-使用)
  - [4.1 批量页](#41-批量页)
  - [4.2 定价页](#42-定价页)
  - [4.3 回测页](#43-回测页)
  - [4.4 敏感性页](#44-敏感性页)
- [5. CLI 命令](#5-cli-命令)
- [6. Python API](#6-python-api)
- [7. 数据文件](#7-数据文件)
- [8. 常见问题](#8-常见问题)
- [9. 测试](#9-测试)

---

## 1. 安装

### 基础环境

```bash
git clone https://github.com/your/ConvertibleBond.git
cd ConvertibleBond

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
python -m pip install -U pip
pip install -e ".[dev]"
```

### 依赖清单

| 依赖 | 用途 | 备注 |
| --- | --- | --- |
| Python 3.10+ | 运行环境 | 使用 `X \| None` 等 3.10 类型语法 |
| `numpy`, `scipy` | PDE 定价数值计算 | |
| `customtkinter`, `matplotlib` | GUI 界面 | |
| `pillow` | 图像处理 | |
| `akshare` | 免费动态行情源 | |
| WindPy | 全字段条款同步 + 实时行情 | ⚠️ 需从 Wind 终端安装，不能通过 pip |

### WindPy 安装

WindPy 不通过 pip 发布。如需使用：

1. 打开 Wind 金融终端
2. 进入 **插件管理 → Python 接口**
3. 选择当前虚拟环境的 Python 路径进行安装
4. 验证：`python -c "from WindPy import w; print(w)"`

> [!TIP]
> 不连接 Wind 也能正常使用！离线 PDE 模型、已有 `data/cb_data.json`、akshare 动态行情都不依赖 Wind。

---

## 2. 数据源分工

CBLens 把 **静态条款** 和 **动态行情** 分开处理：

| 数据类型 | 默认来源 | 落盘位置 | 说明 |
| :--- | :--- | :--- | :--- |
| 转债条款、转股价、票息、评级、余额 | Wind | `data/cb_data.json` | 半静态信息，建议定期同步 |
| 公告事件 | cninfo | `data/cb_events.json` | 默认不需要 Wind |
| 停牌、强赎、摘牌、ST、成交额 | Wind | `data/cb_data.json` | 每日增量刷新 |
| 正股/转债行情、历史波动率、利率 | Wind / akshare / CSV | 不固定落盘 | 按行情源选择实时获取 |
| 关注池 | 本地维护 | `data/watchlist.json` | GUI 批量页管理 |

> [!IMPORTANT]
> 字段缺失时，主池准入筛选遵循**保守原则**：只有明确命中风险条件才剔除，`None` 不会直接剔除。

---

## 3. 首次使用流程

### 路径 A：有 Wind 环境

建议先建立完整本地条款库，再进入 GUI：

```bash
cb-sync-tradable             # 同步全市场基础条款
cb-sync-admission-status     # 刷新准入状态
cb-sync-events --apply       # 同步公告事件
cb-screen-pool               # 查看主池报告
cb-gui                       # 启动 GUI
```

### 路径 B：无 Wind 环境

```bash
# 验证 PDE 引擎
python CB.py

# 手工输入条款做单只定价
cb-gui

# 若仓库已有 data/cb_data.json，用 akshare 行情
python CB.py 128009.SZ --source akshare
```

---

## 4. GUI 使用

### 启动方式

```bash
cb-gui
# 或
python -m convertible_bond.gui.app
# 或
python gui.py
```

### 顶部栏功能

| 元素 | 功能 |
| :--- | :--- |
| **深色/浅色模式** | 切换 Catppuccin Latte/Mocha 主题 |
| **Tab 切换** | 📦 批量 · ⚡ 定价 · 📈 回测 · 🔥 敏感性 |
| **行情源** | 选择 Wind 或 akshare |
| **🌐 同步池** | 全市场基础信息、准入状态、公告事件同步入口 |
| **代码输入** | 输入 `128009.SZ` 或六位代码，命中条款库时自动补全 |
| **📥 同步** | 读取本地条款库 + 拉取正股行情与历史波动率 |
| **🔄 刷新** | 强制用 Wind 刷新当前债条款（下修/评级变更后使用） |
| **💾 / 📂** | 保存/加载参数预设 (Ctrl+S / Ctrl+O) |

### 快捷键

| 快捷键 | 功能 |
| :---: | :--- |
| `Ctrl + Enter` | 运行定价 |
| `Ctrl + S` | 保存预设 |
| `Ctrl + O` | 加载预设 |
| `Ctrl + D` | 收敛诊断（开发者调试） |

---

### 4.1 批量页

批量页是**默认首页**，适合从全市场池里找复核候选。

**操作步骤**：

1. 选择行情源
2. 选择视图：综合机会 / 低估候选 / 转股折价 / 需复核
3. 设置最低机会分
4. 点击 **刷新重算**
5. 主表查看理论价、市价、偏离、转股溢价、机会分、风险标签
6. 选中标的 → **加入关注池**
7. **关注池重算** 快速更新已关注标的
8. **导出 CSV** 留档

**关键指标解读**：

| 字段 | 含义 |
| :--- | :--- |
| `deviation` | `(市价 - 理论价) / 理论价`，负值越大 → 模型认为越低估 |
| `undervaluation_rate` | `-deviation`，正值表示低估程度 |
| `opportunity_score` | 综合低估、转股折价、HV、余额、评级、久期的排序辅助字段 |
| `risk_tags` | ⚠️ 优先查看：高 HV、极小余额、临近强赎/摘牌、数据缺口 |
| `review_notes` | 模型或数据异常的复核建议 |

> [!WARNING]
> `opportunity_score` 不是交易信号。它仅辅助筛选值得人工复核的标的，不能替代投资判断。

---

### 4.2 定价页

定价页适合**单债深度分析**。

**操作步骤**：

1. 输入转债代码 → 点击 **📥 同步**
2. 确认自动填充的条款参数（正股价、转股价、票息、到期日等）
3. 调整模型参数：`sigma`、`r`、`base_spread`、`distress_k`、`p_down`
4. 点击 **开始计算**（或 `Ctrl + Enter`）
5. 查看结果：
   - 理论价与市价偏离
   - 纯债价值 / 转股价值 / 期权溢价
   - 希腊值：Δ, Γ, ν, Θ
6. 输入市价 → 点击 **解 IV** 反解隐含波动率
7. 点击 **现金流** 查看付息和到期兑付计划

**参数来源标签**：

每个输入字段旁标注数据来源（手工 / 条款库 / Wind / akshare / 模型 / 预设），方便追溯。

---

### 4.3 回测页

回测页用于**复盘模型表现**。

**操作步骤**：

1. 在定价页先同步或手工确认当前条款
2. 切到回测页
3. 选择开始/结束日期和频率（日/周/月）
4. 可选：开启 **价值分解** 和 **反解 IV**
5. 点击 **运行回测**
6. 分析指标：
   - 理论价曲线 vs 市价曲线
   - IV / HV spread
   - 统计偏差指标

> [!NOTE]
> 历史回测默认使用当前条款。发生过下修的债可能出现历史转股价跳点偏差。

---

### 4.4 敏感性页

敏感性页生成 **σ–S 二维热力图**。

**操作步骤**：

1. 先在定价页准备好条款和基础参数
2. 设置 `S (%K)` 和 `sigma (%)` 的扫描范围
3. 设置网格密度
4. 点击 **运行分析**
5. 查看热力图：颜色深浅对应理论价变化
6. 点击 **PNG** 导出报告图

---

## 5. CLI 命令

### 单只定价

```bash
python CB.py 128009.SZ                              # 自动选源
python CB.py 128009.SZ 2026-04-20 --source auto     # 指定估值日
python CB.py 128009.SZ --source akshare             # 指定 akshare
python CB.py                                        # 离线示例
```

`--source` 只选择动态行情源。静态条款优先读取 `data/cb_data.json`。

### 同步全市场条款

```bash
cb-sync-tradable                                   # 全量同步
cb-sync-tradable --info                            # 仅查看状态
cb-sync-tradable --limit 50                        # 限量同步
cb-sync-tradable --codes 113050.SH 128009.SZ       # 指定代码
```

> [!TIP]
> 典型节奏：月初全量同步；新债上市、下修、评级变更、退市集中发生后补同步。

### 刷新准入状态

```bash
cb-sync-admission-status                           # 全量刷新
cb-sync-admission-status --limit 50                # 限量刷新
cb-sync-admission-status --codes 113050.SH         # 指定代码
```

刷新字段：停牌、强赎状态、最后交易日、摘牌日、正股 ST、转债成交额、评级、剩余余额等。

### 同步公告事件

```bash
cb-sync-events                                     # 扫描新事件
cb-sync-events --limit 50                          # 限量扫描
cb-sync-events --codes 118006.SH --apply           # 指定代码 + 应用
cb-sync-events --source cninfo --no-pdf            # 跳过 PDF 解析
```

`--apply` 会把事件表应用回 `cb_data.json`（例如强赎公告写入 `call_status`，不下修写入 `down_reset_block_until`）。

### 查看主池筛选

```bash
cb-screen-pool                                     # 默认参数
cb-screen-pool --min-rating AA- --min-balance 1    # 严格筛选
cb-screen-pool --min-turnover 10000000 --show-excluded 50
```

---

## 6. Python API

### 离线模型

```python
from datetime import date
from convertible_bond.pricer import UniversalCBPricer

pricer = UniversalCBPricer(
    S0=55.0,
    K=52.77,
    current_date=date(2026, 4, 20),
    maturity_date=date(2026, 7, 30),
    issue_date=date(2020, 7, 30),
    conversion_start_date=date(2021, 2, 6),
    coupon_rates=(0.003, 0.004, 0.008, 0.015, 0.018, 0.02),
    redemption_price=107.0,
)

price = pricer.price(sigma=0.28, r=0.022, base_spread=0.03)
```

### 自动取数定价

```python
from convertible_bond.pricing_api import price_from_auto

row = price_from_auto("128009.SZ", prefer="akshare")
print(row["theoretical_price"], row["market_price"], row["sigma"])
```

### 批量定价

```python
from convertible_bond.batch_pricing import build_batch_provider, list_batch_codes_from_cache
from convertible_bond.cache import TermsBundle, project_bundle_path
from convertible_bond.pricing_api import batch_price_from_provider_threaded

bundle = TermsBundle(project_bundle_path())
codes = list_batch_codes_from_cache(bundle)[:50]
provider = build_batch_provider("akshare", terms_cache=bundle)

rows = batch_price_from_provider_threaded(provider, codes, max_workers=4)
rows = [row for row in rows if row.get("status") == "ok"]
```

---

## 7. 数据文件

| 文件 | 用途 | 手工编辑 |
| :--- | :--- | :---: |
| `data/cb_data.json` | 全市场条款与准入状态 | ❌ 一般不要 |
| `data/cb_events.json` | 结构化公告事件 | ❌ 由同步维护 |
| `data/down_reset_overrides.json` | 人工下修覆盖 | ✅ 可以手工维护 |
| `data/watchlist.json` | 关注池 | ✅ 可由 GUI 管理 |
| `data/batch_pricing_cache.json` | 批量定价缓存 | ❌ 自动生成 |

> [!NOTE]
> JSON 写入采用 `.tmp` 后 `rename` 的原子写模式，避免半截文件。

---

## 8. 常见问题

### ❓ WindPy import 失败

确认 Wind 终端已安装 Python 接口到当前 venv：

```bash
which python
python -c "from WindPy import w; print(w)"
```

如果路径不匹配，需要在 Wind 终端中重新选择正确的 Python 路径。

### ❓ akshare 能取行情但不能同步条款

**这是预期行为**。akshare 动态行情可用，但完整转债条款（强赎/回售触发比例、回售观察期、完整付息计划）仍以 Wind 写入的 `cb_data.json` 为准。

### ❓ GUI 输入代码后字段为空

先确认条款库里有这只债：

```bash
cb-sync-tradable --info
cb-sync-tradable --codes 128009.SZ
```

### ❓ 批量结果出现大量失败

先缩小范围定位问题：

```bash
cb-screen-pool --show-excluded 50
cb-sync-admission-status --limit 50
cb-sync-events --limit 50 --apply
```

如果是网络或数据源问题，换行情源或降低并发后再跑。

### ❓ 理论价和市价偏差很大

优先检查以下几项：

- [ ] 转股价是否刚下修但本地条款未刷新
- [ ] 是否已公告强赎、摘牌或停牌
- [ ] 正股价、转债市价和估值日是否同日
- [ ] HV 是否异常高，导致期权价值被放大
- [ ] 余额、评级、转股溢价是否触发风险标签

---

## 9. 测试

```bash
# 全量
pytest

# 快速失败
pytest -x -q

# 按模块
pytest tests/test_pricer.py -x -q
pytest tests/test_pricing_api.py -x -q
pytest tests/test_batch_pricing.py -x -q
```

---

<div align="center">
  <sub>📘 CBLens 使用文档 · 更多信息见 <a href="../README.md">README</a></sub>
</div>
