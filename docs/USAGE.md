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
  - [4.1 ⭐ 关注池主页](#41--关注池主页默认落地页)
  - [4.2 📦 批量页](#42--批量页)
  - [4.3 策略页](#43-策略页)
  - [4.4 定价页](#44-定价页)
  - [4.5 回测页](#45-回测页)
  - [4.6 敏感性页](#46-敏感性页)
- [5. CLI 命令](#5-cli-命令)
- [6. Python API](#6-python-api)
- [7. 数据文件](#7-数据文件)
- [8. 常见问题](#8-常见问题)
- [9. 测试](#9-测试)

---

## 1. 安装

### 基础环境

```bash
git clone https://github.com/Grefer/CBLens.git
cd CBLens

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
python -m pip install -U pip
pip install -e ".[dev]"
```

> [!IMPORTANT]
> **`-e` 不是可选的**：本项目只支持两种装法——**源码 checkout（editable）** 与
> **桌面包**。wheel 里**不含数据**：`data/` 与 `assets/` 都在包目录之外，而打包只按
> `packages.find` 收 `convertible_bond*`，实测 `pip wheel --no-deps .` 产出的 92 个条目里
> `data/` 与 `assets/` **各 0 个文件**（86 个 `.py` + 6 个 dist-info 元数据）。所以 `pip install .`（非 `-e`）装出来的环境没有任何条款/事件数据，
> `cb-screen-pool` 会报「总数: 0」。
>
> 这种装法下数据目录**不会**落在 site-packages（那里写进去的东西会随下一次 pip
> 升级/卸载消失），而是回落到桌面包用的用户级目录，并打印一条警告：
>
> | 平台 | 数据目录 |
> | --- | --- |
> | macOS | `~/Library/Application Support/CBLens/data` |
> | Windows | `%APPDATA%\CBLens\data` |
> | Linux | `${XDG_DATA_HOME:-~/.local/share}/CBLens/data` |
>
> 任何形态都可以用 `CBLENS_DATA_DIR` 显式指定数据目录，例如指向一份已有的仓库 `data/`。

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

### 桌面 APP

正式 Release 会附带可直接运行的桌面包：

- macOS: `CBLens-macOS.zip`，解压后双击 `CBLens.app`
- Windows: `CBLens-Windows.zip`，解压后双击 `CBLens.exe`

> 桌面包暂未使用 Apple Developer ID 或 Windows Authenticode 证书签名，首次运行需要手动放行：
>
> - **macOS**：Gatekeeper 可能提示"无法验证开发者"或"已损坏，无法打开"。在终端执行 `xattr -dr com.apple.quarantine /path/to/CBLens.app` 去掉隔离属性，或在 Finder 中右键 `CBLens.app` → 打开，再在弹窗中点"打开"。
> - **Windows**：SmartScreen 会显示"Windows 已保护你的电脑"。点击"更多信息" → "仍要运行"。
>
> 如希望避免上述提示，可参考下方步骤在本机自行编译。

源码构建桌面包：

```bash
python -m pip install -e ".[desktop]"
python scripts/build_desktop.py
```

构建产物位于 `dist/`。打包后的 APP 会把运行态数据写入用户目录，而不是写入应用安装目录：

- macOS: `~/Library/Application Support/CBLens/data`
- Windows: `%APPDATA%\CBLens\data`
- 可用 `CBLENS_DATA_DIR` 环境变量覆盖数据目录

若打包后怀疑内置数据或数据源依赖没有被带上，可在终端运行：

```bash
dist/CBLens/CBLens --diagnose
# 或 macOS .app 形态:
dist/CBLens.app/Contents/MacOS/CBLens --diagnose
```

诊断输出会列出 APP 内置种子数据、用户数据目录中的 `cb_data.json` 债券数量，以及 WindPy / akshare / certifi / requests 是否能被定位；WindPy 会实际 import 一次但不会启动连接。
GitHub Actions 自动构建环境不带 WindPy；在装有 Wind API 的本机打包时，APP 会像 DeltaLab 一样优先使用包内 WindPy。若发布包未内置 WindPy，运行时会自动探测本机 Wind 终端；非默认位置可把 `CBLENS_WINDPY_PATH` 指向 `WindPy.py` 或其所在目录。

发布桌面包时，Windows 与 macOS 分开处理，避免 CI 中无 WindPy 的 macOS 包覆盖本机 WindPy 版：

- Windows: 触发 GitHub Actions 的 `build-desktop.yml`，它只会覆盖 Release 里的 `CBLens-Windows.zip`
- macOS: 在装有 Wind API 的本机运行 `python scripts/release_macos_desktop.py --tag v1.0.0`，脚本会先诊断 WindPy 与缓存数据，再覆盖上传 `CBLens-macOS.zip`

---

## 2. 数据源分工

CBLens 把 **静态条款** 和 **动态行情** 分开处理：

| 数据类型 | 默认来源 | 落盘位置 | 说明 |
| :--- | :--- | :--- | :--- |
| 转债条款、转股价、票息、评级、余额 | Wind | `data/cb_data.json` | 半静态信息，建议定期同步 |
| 公告事件 | cninfo | `data/cb_events.json` | 默认不需要 Wind |
| 停牌、强赎、摘牌、ST、成交额 | Wind | `data/cb_data.json` | 每日增量刷新 |
| 正股/转债行情、历史波动率、股息率、利率 | Wind / akshare / CSV | 不固定落盘 | 按行情源选择实时获取 |
| 关注池 | 本地维护 | `data/watchlist.json` | GUI 批量页管理，运行态文件默认不提交 |

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
| **Tab 切换** | ⭐ 关注 · 📦 批量 · 🎯 策略 · ⚡ 定价 · 📈 回测 · 🔥 敏感性（启动默认落在 **⭐ 关注**） |
| **行情源** | 选择 Wind 或 akshare —— **全局唯一，各页共用**：关注池主页、批量定价、单债定价、策略回测走的都是这一个（各页不再各摆一个下拉）。默认值按本机实际可用性挑，没装 WindPy 时自动落到 akshare |
| **🌐 同步池** | 全市场基础信息、准入状态、公告事件同步入口 |
| **代码输入** | 输入 `128009.SZ` 或六位代码，命中条款库时自动补全 |
| **📥 同步** | 读取本地条款库 + 拉取正股行情、历史波动率与股息率 |
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

### 4.1 ⭐ 关注池主页（默认落地页）

每天打开 CBLens 先看这一页：**表里是上次落盘的价**，不依赖你这次开机有没有跑过全市场。

**取价是三级兜底**（优先级由低到高）：

1. 磁盘热缓存 `data/watchlist_pricing_cache.json` —— 开页即有数的地基
2. 本次会话里「⚡ 关注池重算」/「🆕 扫新债」算出来的结果
3. 本次会话里跑过的全市场批量结果（取**全池**，不是主表当前视图的子集）

**操作**：

| 控件 | 作用 |
| :--- | :--- |
| **⚡ 关注池重算** | 只给关注池这几只取数定价，跳过全市场，秒级返回。结果落盘，下次开页直接就有 |
| **🆕 扫新债** | 同步新债上市日 → 扫描 → 加入关注池 → 立刻定价 |
| 右键 / `Delete` | 从关注池移除 |
| 双击 | 载入 ⚡ 定价页做单债钻取 |
| 表头单击 | 按该列排序 |

**表格列**（16 列，列序 = 读者的提问次序：这是哪只债 → 多少钱 → 便宜吗 → 现在有什么事 → 基础条款）：

`代码 / 名称 / 正股 / 上市日 / 市价 / 理论价 / 偏差(%) / 相对偏差(pp) / 双低 / 事件 / 正股/下修线 / 标签 / 剩余(年) / 评级 / 数据状态 / 加入日`

- **上市日** 在关注池里刻意排在左块（批量页把它放在后面）：未上市新债右半边整片是「—」，「还有几天挂牌」恰是它们仅有的可操作信息。
- **偏差(%) 与 相对偏差(pp) 的参照物不同**：前者比模型价，后者比全市场当期中位。一只债可以同时"比模型价贵"和"比全市场便宜"。
- **数据状态** 列把几种长得一样的「—」分开：

  主文案是**市价 as-of 的日期**，答不出来才用词说为什么答不出来：

  | 显示 | 含义 |
  | :--- | :--- |
  | `✓ 08-28` | 价是本页最新的那天的 |
  | `市价旧 08-26` | 价比本页别的行旧（停牌、节假日）——市场没给，你无能为力 |
  | `日期不明` | 走条款库 `close` 兜底，**没有 as-of**，可能任意旧 |
  | `未重算 08-26` | 整行是隔夜算的——点「⚡ 关注池重算」就能修 |
  | `无市价` | 算出了理论价，市价那条腿缺（未上市新债的天然状态） |
  | `未定价 · 已发行未上市` | 从没算过，并附上主池剔除原因 |
  | `失败 · <原因>` | 上一轮取价失败 |

  「市价旧」与「未重算」刻意用两个词：前者是市场没给新价，后者是你没重算。

**🆕 扫新债**

一键完成「同步新债上市日 → 扫描 → 加入关注池 → 立刻定价」：

- 走 akshare **窄同步**（一次调用，秒级，**不需要 Wind 终端**），不再事先问「要不要跑一次增量同步」——那道闸的判据读的是条款库写盘时间，而任何一次写盘都会把它推到今天，于是提示永远不会弹出。只有窄同步**失败**时才会问要不要回落到 `cb-sync-tradable --incremental`。
- 两类都算新债：**已定上市日**（未来 30 天内挂牌）与**已发行未上市**（数据源还没给上市日，表里显示「待定」）。
- 扫完自动对关注池里还没有理论价的标的定价。

**事件区**

关注池的「近 7 天已发生」+「未来 30 天」，末尾附全池计数，单击展开全部明细。

> [!NOTE]
> 这一区**空是常态**。实测某日关注池两条都是 0 件，而全池同口径有 52 / 20 件。所以它在没有事件时会显式写「已扫 N 只 · 近 7 天与未来 30 天均无日程事件」，而不是把自己藏起来——一个消失的控件和一个坏掉的控件长得一模一样。

> [!IMPORTANT]
> 关注池的数据分两层，都在 `data/` 下且不入版本库：`watchlist.json` 是**意图层**（我关注哪几只 + 加入瞬间的快照），`watchlist_pricing_cache.json` 与 `watchlist_daily/` 是**行情层**（这些债今天/那天多少钱）。详见 [data/README.md](../data/README.md)。

---

### 4.2 📦 批量页

从全市场池里找复核候选。

**操作步骤**：

1. 选择视图：全池 / 低估候选 / 双低 / 转股折价 / 需复核（菜单里带实时条数；**默认落「全池」**——它是分母不是筛子，永远不会空）
2. 选择列预设：简洁 / 完整（20 列）。「简洁」的列数**随视图变**：基准 13 列，视图会补上自己的判据量——全池 14 列（补「上市日」，它的排序量）、转股折价 14 列（补「转股溢价(%)」）、需复核 15 列（补「可信度」「定价状态」）
3. 点击 **🔄 刷新重算**
4. 主表默认按**当期横截面相对便宜度**排序，查看理论价、市价、相对偏差、双低、事件、风险标签
5. 选中标的 → **⭐ 加入关注池**（这个按钮留在批量页：它读的是主表的选中行）
6. **📝 导出 CSV** 留档

> [!NOTE]
> 「低估候选」的判据**不是**绝对阈值。旧口径 `机会分 ≥ 8` 架在一个水平时变的量上——模型对全市场的中位偏差 4 年间在 +0.4% ↔ +21.6% 摆动，于是 2026-08 的主池里只剩 1 只候选、页面默认打开是空表（实测当前池更已塌到 **0 只**，机会分最大值 6.48）。
> 现在是**两道闸串联**：比全市场当期中位便宜 ≥ 5pp（`MIN_RELATIVE_CHEAPNESS`）**且**排进当期最便宜的 15%（`DEFAULT_UNDERVALUED_PERCENTILE`，下限 10 行）。缺任一道都会退化——纯分位永远凑得满 15%，纯下限在市场谷底会泛滥出几百只。

> [!NOTE]
> 已发行未上市的新债**不进主批量池**（主池报告里记为「已发行未上市」）：它们还买不到，也没有市价可比，放进主池只会让偏差列一片空白。它们的去处是 ⭐ 关注池主页。

**关键指标解读**：

| 字段 | 含义 |
| :--- | :--- |
| `deviation` | `(市价 - 理论价) / 理论价`，负值越大 → 模型认为越低估 |
| `undervaluation_rate` | `-deviation`，正值表示低估程度 |
| `risk_tags` | ⚠️ 优先查看；标签按**五个维度**归类，见下 |
| `review_bucket` | 互斥分桶：需复核 / 模型存疑 / 低估候选 / 转股折价 / 全池 |
| `days_to_last_trading` | 距已公告最后交易日的天数（存续券为空；≤30 天打 `临近摘牌`）|
| `review_notes` | 模型或数据异常的复核建议 |

**标签的五个维度**

标签曾挤在一个扁平集合里同时驱动展示、置信度、视图过滤和策略选债，调一个阈值会穿透四层。
现在按性质归类，每个消费者只取自己该看的维度：

| 维度 | 含义 | 例 | 会不会拦路 |
| :--- | :--- | :--- | :--- |
| 数据质量 | 这个数算不出来/输入缺失 | 数据缺口、无市价、理论价异常 | ✅ 进「需复核」 |
| 可交易性 | 买不到/拿不住 | 转债停牌、临近摘牌、余额清零 | ✅ 进「需复核」 |
| 模型适用性 | 数算得出来，但超出模型能力边界 | 高 HV、模型溢价高、模型高估离群 | ⚠️ 单列「模型存疑」 |
| 标的风险 | 数是对的，债本身有风险 | 低评级、短久期、触及摘牌线 | ❌ 纯展示 |
| 机会信号 | 不是风险，是提示 | 转股折价、模型低估、深度低估待核 | ❌ 纯展示 |

「需复核」只留"这一行现在不能用，得先去做点什么"的两类——模型适用性是**永久属性**，
查完还是那样，塞进需复核只会让它变成 79% 的垃圾桶。

**偏差离群按方向拆开**

模型对全市场有系统性偏移（干净数据下中位 +18.8%），所以判据锚在**本期市场中位**而非 0：

- `模型高估离群`（市价高出中位 ≥20pp）：模型解释不了这个价 → 归模型适用性，扣置信度
- `深度低估待核`（市价低于中位 ≥20pp）：**这是待检验的假设，不是要剔除的噪声** →
  只挂复核建议，不扣分、不进任何排除集

对称处理会把唯一的假设来源删掉——当时实测唯一一只机会分 ≥8 的债正是被"异常"标签踢出候选的
（那之后候选口径已改成横截面两道闸；机会分本身已于 2026-08-29 整体删除，
见 [4.2 批量页](#42--批量页)）。

**余额相关的标签怎么读**

余额**不再**作为硬条件剔除（全库回填摘牌元数据后实测独立贡献为 0），改由按
**3,000 万法定停止交易线**分档的标签表达：

| 未转股余额 | 标签 | 含义 |
| :--- | :--- | :--- |
| `= 0` | `余额清零` | 已转股完毕/已赎回，是退市信号 |
| `< 0.3 亿` | `触及摘牌线` | 低于 3,000 万法定线，交易所将安排停止交易 |
| `0.3 ~ 0.5 亿` | `临近摘牌线` | 贴近法定线 |
| `0.5 ~ 1.0 亿` | `小余额` | 流动性提示 |

另有独立的 `临近摘牌`：已公告最后交易日且落在 30 天内。它只认 `last_trading_date`，
不认 `delisting_date`——存续券的摘牌日多数等于到期日（**预定**摘牌，不是事件）。
带这个标签的债会从「低估候选」移到「需复核」，但**不进** `HARD_REVIEW_TAGS`，
即不改变策略页的默认选债行为。

需要恢复余额硬过滤时，在批量页「最小余额」格里填个数值即可。

> [!WARNING]
> 批量页的排序与标签是**复核辅助**，不是交易信号。它们只帮你挑出值得人工细看的标的，不能替代投资判断。
>
> 另注：`opportunity_score`（机会分）已于 2026-08-29 **整体删除**——实测它的低估项在 95% 的债上恒为 0，分数实际由评级/余额加分决定，度量的是信用质量而非错定价（`Spearman(机会分, 质量分) = +0.517`，而与偏差只有 −0.640）。`quality_score` 保留。

---

### 4.3 策略页

策略页围绕 CBLens 自有定价模型保留一类策略：

- **估值偏差**：只保留市价低于理论价的标的，按 `(market - theoretical) / theoretical` 从低到高排序。

> [!NOTE]
> 「下修机会」策略已于 2026-08-29 删除。它的排序信号建在"反解让模型价等于市价的下修强度 λ"之上，而 `price(λ)` 单调增、`λ ≥ 0` 意味着下修只能把理论价往上推 —— 可解带宽仅 `[price(0), price(3.0)]`（实测全量程中位 12.2 元），而要解释的缺口中位 24.5 元。结果是两个 regime 从相反两端同时失效：高位市价超出上界（270/284 无解），谷底市价掉在 `price(0)` 下方（0/470 可解，回测四期 100% 现金）。详见 README 的模型边界一节。

策略按 Top N 等权持有；候选不足或缺成交价时**保留现金**，不再提供“等权全池/缺口摊回”选项。旧机会分、下修优势、双低和旧模板只保留在历史快照/Python API 兼容层。

**参数分层** —— 首屏只保留运行回测所需的五项，研究参数收在「参数设置」

| 区域 | 参数 | 普通用户建议 |
| --- | --- | --- |
| 首屏 | 策略 / 开始·结束 / 调仓 / 持仓数 / 数据模式 | `快速验证`对应标准口径，`Wind 历史`对应高保真口径 |
| 参数设置 · 标的范围 | 转债范围 | 本地全市场使用点时动态池；也可用批量页结果或自选代码 |
| 参数设置 · 选债限制 | 价格 / 溢价 / 估值偏差 / HV | 这些是准入范围，不参与排序 |
| 参数设置 · 模型与交易 | `p_down` / HV窗口与扰动 / 利差与扰动 / 困境斜率 / 成本 / 现金 / 事件退出 | 只显示当前策略会用到的参数，并随快照保存 |

基准固定自动计算，候选不足固定保留现金；点击运行时系统会先自动预检。CSV 导出和清空快照位于结果区右上角。

**数据模式怎么选**

- **Wind 历史**（GUI 默认/推荐）: 按调仓日从 Wind `tradeDate` 查询历史条款，清除当前状态并用当时已知公告重建；大池可能耗时数小时。
- **快速验证**: 本地条款 + 补丁 + 事件回放，用于快速诊断；正式结论应用 Wind 历史口径复跑。

**结果怎么读 (四条铁律)**

1. 一切对照基准: 「概览」同时给「等权基准」(全可投池) 与「中证转债指数」两条线,
   跑不赢 = 这套规则没有超额, 哪怕绝对收益为正;
2. **再看「诊断」**: 块自助给 Sharpe 置信区间与跑赢基准概率 —— **CI 含 0 = 差异
   可能只是运气**, 别把点估计 (如 Sharpe 0.6 vs 0.4) 当真; 期数越少 CI 越宽;
3. 模型偏差只是错定价的**候选线索**，不代表样本外收益已显著;
4. 「诊断」下半部分核对条款来源、模型参数、公告修正与缺价跳过数；该页应与快照中的设置一致。

**进阶旋钮 (高级研究)**

- **事件退出**: 下修提议/通过/拒绝公告会让 thesis 落地或证伪；勾选后在公告后下一可得收盘退出，余下时间持有现金。**默认关闭** —— 它此前只在下修优势排序信号下才被激活，那个信号删除后改为显式开关，以免默认选债行为悄悄改变。
- **仓位口径**: 「估值缩放」按当期全市场估值偏差中位数缩放总仓位；默认「恒定满仓」。
- **现金收益**: 留现金/缺价/降仓留出的现金按此年化计息 (默认 2.2%≈货基); 设 0 复现旧口径。
  注意 Sharpe 课征无风险门槛, 现金 0 计息会系统性低估持现金策略。

CLI 等价命令:

```bash
cb-strategy-backtest --source wind --history-mode wind-high-fidelity \
  --start 2025-01-01 --end 2026-01-01 --freq M \
  --rank-signal deviation --cash-yield 0.022 \
  --M 120 --N 400 --cache-dir .cache/bt
```

### 4.4 定价页

定价页适合**单债深度分析**。

**操作步骤**：

1. 输入转债代码 → 点击 **📥 同步**
2. 确认自动填充的条款参数（正股价、转股价、票息、到期日等）
3. 调整模型参数：`sigma`、`r`、`q`、`base_spread`、`distress_k`、`p_down`
4. 点击 **开始计算**（或 `Ctrl + Enter`）
5. 查看结果：
   - 理论价与市价偏离
   - 纯债价值 / 转股价值 / 期权溢价
   - 希腊值：Δ, Γ, ν, Θ
6. 输入市价 → 点击 **解 IV** 反解隐含波动率
7. 点击 **现金流** 查看付息和到期兑付计划
8. 点击 **⭐ 加入关注池** 把这只债连同当前理论价快照存进关注池（批量页的关注池表会同步刷新）

按钮本身就是状态显示：未关注时是橙色 **⭐ 加入关注池**，点击后闪一下 **✓ 已加入关注池**，随后固定为绿色 **★ 已关注**；切换代码时按钮会跟着变，一眼就能看出这只债在不在池子里。

> [!TIP]
> 先计算再加入，关注池会留下加入瞬间的理论价 / 市价 / 偏差快照，之后能复盘「我当时为什么觉得它便宜」。直接加入也可以，只是没有快照。

**参数来源标签**：

每个输入字段旁标注数据来源（手工 / 条款库 / Wind / akshare / 模型 / 预设），方便追溯。

**核心模型参数**：

| 参数 | 含义 | 单位与默认 |
| :--- | :--- | :--- |
| `sigma` | 正股年化历史波动率 | 百分数，默认按窗口历史价估算 |
| `r` | 无风险利率 | 百分数，可用 Shibor 参考 |
| `q` | 正股连续股息率 | 百分数，默认从数据源读取，缺失时为 0 |
| `base_spread` | 信用利差 | 百分数，可按评级填入 |
| `p_down` | 下修事件年化强度 | 百分数/年 |
| `distress_k` | 正股下跌时信用利差扩张参数 | 百分数 |

PDE 中股价风险中性漂移使用 `r - q`，折现仍使用 `r + credit_spread(S)`。因此提高 `q` 通常会压低转股权相关价值。

---

### 4.5 回测页

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

回测会沿用定价页中的 `r`、`q`、信用利差、下修强度和强赎宽限天数。历史正股价与滚动 `sigma` 从数据源取数，`q` 当前作为固定输入参与整段回测。

> [!NOTE]
> 历史回测默认使用当前条款。发生过下修的债可能出现历史转股价跳点偏差。

---

### 4.6 敏感性页

敏感性页生成 **σ–S 二维热力图**。

**操作步骤**：

1. 先在定价页准备好条款和基础参数
2. 设置 `S (%K)` 和 `sigma (%)` 的扫描范围
3. 设置网格密度
4. 点击 **运行分析**
5. 查看热力图：颜色深浅对应理论价变化
6. 点击 **PNG** 导出报告图

敏感性热力图只扫描 `S` 与 `sigma`，`r`、`q`、信用利差和下修参数保持定价页当前值不变。

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
cb-sync-admission-status --limit 50                # 限量刷新 (仅调试; 摘牌日等字段需全库跑)
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

### 修数据：清洗被门槛条款污染的余额 patch

赎回/回售公告会成段引用「未转股余额少于 3,000 万元时公司有权赎回」这类**门槛条款**。
解析器早期把它当成真实余额，写出一批 `outstanding_balance = 0.3` 的错误 patch，
让真实余额几十亿的大盘券被准入过滤当成「余额过小」剔除（主池 217 → 修复后 283）。

```bash
cb-repair-balance-patches                  # 先看报告 (dry-run, 默认)
cb-repair-balance-patches --apply          # 确认后回洗 (自动备份 .bak-<时间戳>)
```

解析侧已按**措辞**而非数值修复，真实披露的「未转股余额为 3,000 万元」仍会正常解析；
本命令只清洗历史存量，日常同步不需要重复跑。

### 补历史：两个一次性回填工具

它们都写进**生产存储**（`cb_data.json` / `cb_terms_patches.json`），跑完日常流程不需要
再碰；列在这里是因为一个敲得出来却没写过怎么用的命令，和一个坏掉的命令区分不开。

```bash
cb-backfill-delisted-cbs --dry-run          # 先看会补哪些债
cb-backfill-delisted-cbs                    # 回填已退市/已强赎的债 (需 Wind)

cb-backfill-down-reset-patches --dry-run    # 先看会生成哪些 patch
cb-backfill-down-reset-patches --fetch-pdf  # 下载公告正文重解析, 覆盖率最高但最慢
```

- `cb-backfill-delisted-cbs`：`cb-sync-tradable` 只拉**今日存续**的转债，于是
  cb_data 长期带幸存者偏差——已强赎/已到期的债退出样本，回测早年窗口会偷偷剔掉那些
  「涨到强赎」的好券。本命令按季度末扫 Wind 历史成分取并集，显式 `drop_terminal=False`
  把差集补进同一个 `cb_data.json`。
- `cb-backfill-down-reset-patches`：早期入库的下修事件解析不全，`cb_terms_patches.json`
  里下修后的转股价覆盖偏稀。本命令按 `event_price` → 标题重解析 → PDF 正文的优先级
  回填 `conversion_price` patch（同 key 不重复写）。

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

price = pricer.price(sigma=0.28, r=0.022, q=0.015, base_spread=0.03)
```

`q` 为连续股息率，小数形式。例如 `0.015` 表示 1.5%/年。

### 自动取数定价

```python
from convertible_bond.pricing_api import price_from_auto

row = price_from_auto("128009.SZ", prefer="akshare")
print(row["theoretical_price"], row["market_price"], row["sigma"], row["q"])
```

自动取数定价会调用行情源的 `get_stock_dividend_yield()`。该接口返回百分数，例如 `2.5` 表示 2.5%/年；`price_from_auto()` 返回结果里的 `q` 已经转换为模型小数，例如 `0.025`。如果行情源没有返回有效股息率，`q` 会回退为 `0.0`。

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
| `data/watchlist.json` | 关注池 | ✅ 可由 GUI 管理，默认忽略 |
| `data/batch_pricing_cache.json` | 批量定价缓存 | ❌ 自动生成，默认忽略 |

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
- [ ] 股息率 `q` 是否缺失或口径异常，尤其是高股息正股
- [ ] 余额、评级、转股溢价是否触发风险标签

### ❓ 股息率 q 为什么是 0

Wind 或 akshare 未返回有效股息率时，系统会保守回退到 `0`，避免数据缺口直接阻断定价。可以在定价页手工输入 `q (%)` 后重新计算；保存参数预设时，`q` 也会一并保存。

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
