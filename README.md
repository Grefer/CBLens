<div align="center">

<img src="assets/cblens-banner.svg" alt="CBLens Banner" width="100%">

<p align="center">
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.10+-blue.svg" alt="Python"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green.svg" alt="License"></a>
  <a href="#测试"><img src="https://img.shields.io/badge/tests-pytest-blue.svg" alt="Tests"></a>
  <a href="docs/USAGE.md"><img src="https://img.shields.io/badge/docs-使用文档-orange.svg" alt="Docs"></a>
  <a href="https://github.com/Grefer/CBLens/commits/master"><img src="https://img.shields.io/github/last-commit/Grefer/CBLens" alt="Last Commit"></a>
  <a href="https://github.com/Grefer/CBLens/issues"><img src="https://img.shields.io/github/issues/Grefer/CBLens" alt="Issues"></a>
</p>

**基于 Crank-Nicolson PDE 引擎的 A 股可转债定价与机会筛选工作台**<br/>
支持多数据源接入、公告事件解析、公开交易主池筛选以及完整的 GUI / CLI 研究工作流。<br/>
把转债条款、公告事件、正股行情、信用利差和数值定价模型串成一条可重复的研究管线。

</div>


---

## ✨ 项目定位

CBLens 面向 **A 股可转债研究与复盘**。它不是交易下单系统，也不是投资建议——它的目标是帮助你更快发现 *"值得人工复核"* 的低估、转股折价、事件风险和异常标的。

当前版本是 **2.0.0 正式版**（tag：`v2.0.0`）。这一版把关注池主页、批量筛选、单债钻取、敏感性分析与桌面交付连成完整工作流：应用内可配置 Wind 接口，桌面包启动不再每次重建字体缓存，取数与准入扫描整体提速。

从 v1 升级涉及 API、CLI、CSV 和模型口径变化，请先读 [版本说明](CHANGELOG.md) 与 [v1 → v2 升级指南](docs/UPGRADING_V2.md)。

---
![alt text](assets/cblens-screenshot.png)
## 🧩 核心能力

<table>
<tr>
<td width="50%">

### 🔬 PDE 定价引擎
使用 **Crank-Nicolson 有限差分法**，支持：
- 强赎 / 回售 / 下修博弈
- 阶梯票息与应计利息
- 强赎宽限期与信用利差折现
- 连续股息率 `q`，股价漂移采用 `r - q`
- 希腊值 (Δ, Γ, ν, Θ) 与价值分解

</td>
<td width="50%">

### 📦 批量筛选与打分
从全市场条款库出发，自动完成：
- 主池公开交易筛选（剔除不可公开交易标的，风险进入复核标签）
- 批量 PDE 定价与多线程加速
- 相对偏差 / 质量分 / 置信度 / 风险标签
- 转股溢价率 / 低估率排序

</td>
</tr>
<tr>
<td>

### 📰 事件驱动分析
解析巨潮 (cninfo) 或 Wind 公告，维护结构化事件：
- 下修提议 / 通过 / 否决
- 强赎 / 不强赎公告
- 回售 / 评级变更 / 停牌 / 摘牌
- 事件自动应用回条款库

</td>
<td>

### 🖥️ 研究界面
CustomTkinter GUI 覆盖完整研究流：
- **⭐ 关注池主页**：默认落地页，开页即有上次落盘的价（带估值日）
- **批量页**：全市场候选筛选（按当期横截面相对便宜度排序）
- **定价页**：单债钻取 + 隐含波动率反解
- **回测页**：历史模型偏差复盘
- **敏感性页**：σ-S 热力图 + 报告导出

</td>
</tr>
<tr>
<td>

### 🔌 多数据源架构
灵活切换数据来源：
- **Wind**：全字段条款同步 + 实时行情
- **akshare**：免费动态行情替代
- **CSV**：自定义数据导入
- 静态条款与动态行情解耦设计

</td>
<td>

### ✅ 可测试模型
核心模块均有 pytest 覆盖：
- PDE 引擎精度与收敛性
- 数据缓存 / 事件解析 / 公开交易筛选
- 批量定价 / API 调用链
- Wind mock 测试（无需真实连接）

</td>
</tr>
</table>

---

## 🚀 快速开始

### 安装

```bash
git clone https://github.com/Grefer/CBLens.git
cd CBLens

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
python -m pip install -U pip
pip install -e ".[dev]"
```

> [!IMPORTANT]
> **`-e` 不是可选的**：只支持 **源码 checkout（editable）** 与 **桌面包** 两种装法。`data/` 与 `assets/` 都在包目录之外，wheel 不会带上它们（实测 `pip wheel --no-deps .` 产出的 92 个条目里 `data/` 与 `assets/` 各 0 个文件，86 个 `.py` 加 6 个 dist-info 元数据），所以 `pip install .` 装出来的环境没有任何条款数据，`cb-screen-pool` 会报「总数: 0」。这种装法下数据目录不会写进 site-packages，而是回落到桌面包用的用户级目录（macOS `~/Library/Application Support/CBLens/data`）并打印警告；也可以用 `CBLENS_DATA_DIR` 显式指向一份已有的 `data/`。详见 [使用文档 · 安装](docs/USAGE.md#1-安装)。

> [!NOTE]
> **WindPy** 不通过 pip 发布。如需同步全市场条款或使用 Wind 行情，需在 Wind 终端的插件管理中把 Python 接口安装到当前虚拟环境。仅使用离线 PDE 模型、已有 `data/cb_data.json` 或 akshare 动态行情时，无需连接 Wind。

桌面端可在 **同步池 → Wind 接口设置** 中自动检测或选择本机 `WindPy.py`，检测成功后保存并重启，无需配置系统环境变量。安装与路径优先级见[Wind 接口配置](docs/USAGE.md#windpy-安装与桌面配置)。

### 直接使用桌面 APP

在 [Releases](https://github.com/Grefer/CBLens/releases) 下载：

- `CBLens-macOS.zip`：解压后双击 `CBLens.app`
- `CBLens-Windows.zip`：解压后双击 `CBLens.exe`

> [!IMPORTANT]
> 桌面包暂未使用 Apple Developer ID 或 Windows Authenticode 证书签名，下载后系统会拦截：
>
> - **macOS**：双击会提示"无法验证开发者"或"已损坏"。先 `xattr -dr com.apple.quarantine /Applications/CBLens.app`（路径替换为你的实际位置），或在 Finder 里右键 → 打开 → 再次"打开"。
> - **Windows**：SmartScreen 会弹出"已保护你的电脑"。点"更多信息" → "仍要运行"。
>
> 如不放心可参考下方"源码构建桌面包"自行编译。

源码构建桌面包：

```bash
python -m pip install -e ".[desktop]"
python scripts/build_desktop.py --ref HEAD
```

构建要求干净的源码 checkout，产物记录构建 commit。Windows Release 包由 GitHub Actions 按 tag 构建；macOS Release 包由装有 Wind API 的本机构建。候选包准备与正式上传的区别见 [使用文档 · 桌面 APP](docs/USAGE.md#桌面-app)。

### 启动 GUI

```bash
cb-gui
# 或
python -m convertible_bond.gui.app
```

### 命令行定价

```bash
# 指定转债代码
python CB.py 128009.SZ

# 指定估值日和行情源
python CB.py 128009.SZ 2026-04-20 --source akshare

# 离线模型示例（无需数据源）
python CB.py
```

---

## 📅 每日研究流

一个典型的日常使用流程：

```bash
# ① 查看本地条款库状态
cb-sync-tradable --info

# ② 月初或新债/退市/下修集中变化后，全量同步基础条款
cb-sync-tradable

# ③ 每日刷新停牌、强赎、摘牌、正股 ST、成交额、余额、评级等状态字段
cb-sync-admission-status

# ③.5 每日刷新新债上市日 (窄同步, 秒级, 不需要 Wind)
#     上市日只有全量同步会写, 而新债每天都在挂牌 —— 不刷这一步, 昨天上市的新债今天进不了主池
cb-sync-new-issues --apply

# ④ 同步公告事件，并把事件状态应用回 cb_data
cb-sync-events --apply

# ⑤ 批量定价前查看公开交易主池报告
cb-screen-pool

# ⑥ 打开 GUI 做批量复核、单债钻取和敏感性分析
#    (批量重算成功后会自动把当期估值快照记入历史基线)
cb-gui

# ⑦ 查看转债大类估值/择时信号 (全市场中位偏差 + 历史分位; --record 手动入基线)
cb-valuation

# ⑧ 错定价策略回测 (按估值偏差排序; --cache-dir 复跑提速, --q 跳过逐只股息率取数)
cb-strategy-backtest --start 2025-01-01 --end 2026-01-01 --freq M \
  --rank-signal deviation --cash-yield 0.022
```

---

## 🏗️ 架构

```mermaid
flowchart LR
    A["🌐 Wind / cninfo / akshare / CSV"] --> B["💾 data/*.json<br/>条款与事件缓存"]
    B --> C["🔍 公开交易筛选<br/>admission_status + batch_pricing"]
    C --> D["⚙️ PDE 定价<br/>UniversalCBPricer"]
    D --> E["📊 相对偏差 / 风险标签<br/>复核视图"]
    E --> F["🖥️ GUI / CLI / Python API"]
```

**五层职责**：

| 层级 | 职责 | 核心模块 |
| :---: | --- | --- |
| **① 基础信息** | 发行条款、转股价、票息、强赎/回售规则、评级、余额 | `data_providers`, `cache` |
| **② 事件状态** | 公告事件、停牌、强赎、ST、成交额等状态字段 | `cb_events`, `admission_status` |
| **③ 动态行情** | 正股/转债价格、历史波动率、股息率、无风险利率 | `data_providers` |
| **④ 模型定价** | 理论价、希腊值、纯债底、转股价值、期权溢价 | `pricer` |
| **⑤ 筛选打分** | 相对偏差、转股溢价、质量分、风险标签、置信度 | `batch_pricing` |

---

## 🐍 Python API

### 离线定价（无需数据源）

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
    call_notice_days=30,
)

result = pricer.price(
    sigma=0.28,
    r=0.022,
    q=0.015,
    base_spread=0.03,
    distress_k=0.05,
    p_down=0.0,
    return_greeks=True,
)
print(result["price"], result["delta"], result["bond_floor"])
```

### Provider 驱动定价

```python
from convertible_bond.pricing_api import price_from_auto

row = price_from_auto("128009.SZ", prefer="akshare")
print(row["bond_name"], row["theoretical_price"], row["market_price"], row["q"])
```

---

## 📁 项目结构

```text
CBLens/
├── assets/                     # CBLens 图标与品牌资产
├── docs/                       # 使用文档与品牌说明
├── convertible_bond/           # 主包
│   ├── pricer.py               # PDE 定价引擎
│   ├── pricing_api.py          # provider 驱动的单只/批量定价 helper
│   ├── data_providers/         # Wind / akshare / CSV 数据源 (base / _helpers / wind / akshare / csv_provider / auto)
│   ├── cache.py                # TermsBundle / TermsCache / CachedBondDataProvider
│   ├── batch_pricing.py        # 公开交易筛选、相对偏差、风险标签、批量结果缓存
│   ├── admission_status.py     # 停牌、强赎、摘牌、ST、成交额等状态刷新
│   ├── cb_events.py            # 公告事件模型与解析
│   ├── cb_event_sync.py        # 公告同步和事件应用
│   ├── cninfo_provider.py      # 巨潮公告 provider
│   ├── backtest.py             # 历史回测
│   ├── cli/                    # 同步、筛选等 CLI
│   └── gui/                    # CustomTkinter GUI
├── data/                       # 条款、事件、关注池、批量缓存
├── tests/                      # pytest 测试
├── CB.py                       # 兼容 CLI 入口
├── gui.py                      # 兼容 GUI 入口
└── pyproject.toml
```

---

## 📖 文档

| 文档 | 说明 |
| --- | --- |
| 📘 [使用文档](docs/USAGE.md) | 安装、数据源、GUI 五大页面、CLI 命令、Python API、常见问题排障 |
| 🆕 [版本说明](CHANGELOG.md) | 2.0 正式版亮点、兼容性变化与已知边界 |
| ⬆️ [v1 → v2 升级指南](docs/UPGRADING_V2.md) | 数据备份、环境与脚本迁移、旧结果处理和回退 |
| 🎨 [品牌说明](docs/BRAND.md) | 项目名称由来、图标含义、调色板与使用建议 |
| 📦 [数据说明](data/README.md) | `cb_data.json`、`cb_events.json` 字段定义与刷新节奏 |
| 🔧 [维护约定](AGENTS.md) | 给 agent 和维护者看的项目级上下文与编码规范 |
| 🔬 [研究笔记](docs/research/2026-06-score-ic-and-valuation-timing.md) | 机会分 IC 检验、时变估值溢价、择时信号与组合层对比的完整复盘 (模型边界的依据) |

---

## 🧪 测试

```bash
# 全量测试
pytest

# 快速失败模式
pytest -x -q

# 按模块
pytest tests/test_pricer.py -x -q
pytest tests/test_pricing_api.py -x -q
pytest tests/test_batch_pricing.py -x -q
```

---

## ⚠️ 模型边界

> [!WARNING]
> CBLens 是研究工具，不是交易系统。以下模型局限需要在使用时注意：

- **路径依赖近似**：强赎和下修触发仍用单点状态近似，未完整建模滚动观察窗口；回售期内统一给底也比真实的连续低价与年度次数条件更宽。
- **下修建模**：无公告时以触发线下方的年化强度 `p_down` 描述下修概率，已提议时改用一次性节点。公告未解析出新转股价时，模型会回落到监管价格下限近似，暂未纳入每股净资产下限。
- **利率与股息率**：利率使用标量而非完整期限结构；`q` 按连续股息率处理，缺失时默认为 0。akshare 的实时股息率兜底不能代表历史估值日，固定 `q` 也只是情景假设。
- **Gamma 边界**：下修价下限按初始股价冻结，折点附近的 Γ 可能为负或随网格加密漂移，不宜直接用于对冲或精细风险限额。
- **历史回测**：`标准` 口径适合快速诊断；正式结论应使用 `Wind高保真` 口径，并核对历史条款、公告和数据来源。
- **候选筛选**：「低估候选」同时要求比当期市场中位便宜至少 5 个百分点，并进入最便宜的 15%；它是横截面复核口径，不是绝对估值结论。
- **排序与收益**：批量排序和模型偏差只提供复核线索，不能替代流动性、公告、成交约束、组合风险及样本外检验。
- **市场溢价时变**：模型与市场价格的整体偏差会随市场阶段变化；全市场中位偏差可作大类估值参考，但不能直接解释为个券买入机会。

---

## 📄 许可

本项目基于 [MIT 许可证](LICENSE) 开源。

---

<div align="center">
  <sub>Built with 🔬 by quantitative bond researchers</sub>
  <br />
  <sub><b>CBLens</b> — 看清转债的每一面</sub>
</div>
