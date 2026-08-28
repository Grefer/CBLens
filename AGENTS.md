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
│   ├── new_issue_sync.py       # 新债窄同步 (上市日; akshare, 不依赖 Wind)
│   ├── admission_status.py     # 停牌/强赎/ST 状态刷新
│   ├── down_reset_overrides.py # 下修覆盖 + 三 regime 强度解析
│   ├── watchlist.py            # 关注池**意图层** (我关注什么 + 加入时快照)
│   ├── watchlist_cache.py      # 关注池**行情层** (热缓存 + 按日窄快照, 纯数据无 GUI 依赖)
│   ├── gui/                    # CustomTkinter GUI
│   │   ├── app.py              # CBPricerApp: 多 mixin 组装
│   │   ├── controllers/        # 业务域 mixin; 策略回测已按职责拆为
│   │   │                       #   strategy_{setup,run,snapshots,render,
│   │   │                       #   render_analysis,compare,common} 7 模块,
│   │   │                       #   strategy_backtest.py 仅为聚合入口
│   │   └── tabs/               # 各页 UI 构建
│   │       #   home.py     ⭐ 关注池主页 (默认落地页; 只建控件, 逻辑在 batch_watchlist)
│   │       #   batch.py    📦 批量页; batch_watchlist.py 关注池数据/渲染/动作
│   │       #   batch_common.py 两页共用 helper (Treeview 样式/列宽/染色/表格区)
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
- **`_batch_results` 是视图子集, `_batch_all_results` 才是全池**。前者在
  `_render_batch_views` 里被赋成 `filter_batch_results_by_view(...)` 的结果, 只服务主表渲染;
  任何**跨表**取值 (关注池取理论价、关注池重算回填) 都必须读后者。关注池曾读前者, 于是
  主表切到「低估候选」(实测 40/284 只) 时, 关注的债只要不在那 40 只里就整行显示「—」,
  理论价随视图开关忽有忽无; 更隐蔽的是 `_watchlist_pricing_worker` **回填的也是**
  `_batch_all_results`, 所以「⚡ 关注池重算」对这些行永远无效 —— 状态栏照常报
  "主表 3 / 关注 3", 而表里只有走 `_batch_upcoming_results` 的那 3 只出得来价。
- **关注池的取价是三级兜底, 且必须能自愈**。`_priced_rows_by_code` 的优先级由低到高:
  ① 磁盘热缓存 `watchlist_pricing_cache.json` → ② `_batch_upcoming_results` →
  ③ `_batch_all_results` (**全池**, 不是视图子集 `_batch_results`)。第 ① 层是"开页即有数"
  的地基 —— 没有它, 关注池的理论价完全寄生在"这次开机跑没跑过全市场"上 (实测缓存
  `n_upcoming_results=0` 时三只在途新债连着几天没有理论价; 新债不进主池, 剔除原因
  「已发行未上市」)。**读盘只许发生在 `load_price_cache_into`**, 由启动路径显式调 ——
  展示层一旦隐式碰真实磁盘, 用例就变成"过不过取决于你上次开 GUI 点没点刷新"
  (`sync_cb_events` 那批用例踩过这个坑, 一次纯数据提交就让套件转红)。
  自愈判据从"是不是没价的新债"放宽成 `_price_state != "ok"` (`stale_watchlist_codes`),
  带 15 分钟防抖; 非用户发起的那一轮传 `quiet=True`, 且遇到源连不上会被直接挡掉。
- **`_price_state` 把三种长得一样的「—」分开**: `unpriced` (从没算过) / `failed`
  (算了但失败) / `no_market` (算了但数据源没给市价) / `stale` (隔夜的价) / `ok`。
  实测同一份关注池里三种同时存在。`no_market` 那一档尤其要留神: 118076.SH 先锋转债
  `status=="ok"`、估值日就是今天、唯独市价是 None —— 只看"是不是今天算的"会让它当天
  永远不再重试。反过来**还没上市的新债**的 `no_market` 是天然状态, 不该每轮陪跑。
- **GUI 启动路径上不许建立新的数据源连接**。`w.start()` 的 WindPy 默认签名是
  `start(options=None, waitTime=120)` —— **终端没开时它等满两分钟**, 而"装了 WindPy
  但终端没登录"恰恰是最常见的一档 (实测本机: 可导入、`isconnected()` 为 False)。
  启动 80ms 后那一轮自愈 (`_load_result_cache` → `price_unpriced_new_bonds` →
  `_start_watchlist_pricing(quiet=True)` → worker → `build_batch_provider("Wind")` →
  `get_risk_free_rate`) 于是把"打开 GUI"变成"打开后转两分钟圈"。三道闸缺一不可:
  ① `wind_is_ready()` 只问 `isconnected()`、**绝不 start**, 非用户发起的取数按它决定起不起
  (`_source_ready_without_connecting`: Wind 要已连接 / akshare 纯 HTTP 放行 /
  CSV 要弹模态挡住)。注意它与 `detect_available_providers()` 不是一回事 —— 后者只答
  "装没装", 而"装了但没连"正是会卡住的那一档。
  ② `WIND_START_WAIT_SEC` 给 `w.start()` 一个上限 (默认 20s, `CBLENS_WIND_START_WAIT_SEC`
  可覆盖; 设 0 沿用 WindPy 默认)。
  ③ 连接失败进负缓存 (`WIND_CONNECT_COOLDOWN_SEC`, 默认 60s) —— 失败时 `self._w` 仍是
  None, 没有这道闸每次取数都重等一遍, 全池 284 只按 10 线程折算就是约 570s 的假死。
  行情源默认值也不再硬编码 `"Wind"`, 改走 `gui.constants.default_market_source()`
  (有守护测试扫 `StringVar(value="Wind")`)。**代价要认**: Wind 没连时启动那一轮不再给
  新债补价, 状态栏改为提示点「⚡ 关注池重算」—— 但它此前的结局本来也是失败, 只是先卡两分钟。
- **关注池是独立主页, 但「⭐ 加入关注池」搬不走**。`tabs/home.py` 是默认落地页, 拥有
  关注池表 / 摘要条 / 事件横幅 / 「⚡ 今日刷新」/「🆕 扫新债」; 而「⭐ 加入关注池」必须
  留在批量页 —— 它读主表控件 `app._batch_main_tree` 的 selection, 且 iid 是
  `_batch_results` 的**整数下标**。三条接线约定 (都有守护测试):
  ① `home_tab.build` 必须排在 `batch_tab.build` **之前** —— `_render_watchlist_table`
  拿不到 `batch_watchlist_table_frame` 时是 `return` 而不是报错, 顺序反了只表现为
  "默认落地页首屏是空的"。
  ② 两页共用的 `v_batch_source` / `v_batch_status` / `_batch_watchlist` /
  `_watchlist_price_cache` 提到 `app._build_vars`, 谁都不该假设自己是创建方
  (定价页的 ⭐ 按钮也读 `_batch_watchlist`)。同一个 `v_batch_status` 挂两个 Label,
  于是"⚡ 已刷新 N 只"这类消息在哪页都看得见。
  ③ `_render_batch_views(refresh_home_table=False)` **只给纯展示操作用**
  (切视图 / 切列预设): 那时 `_batch_all_results` 一个字节没变, 而重画会把主页那棵
  17 列的树整个 destroy 重建, 排序/选中/滚动全丢。凡是数据变了的路径都要保持 True。
- **关注池表的列定义是单一事实源 `_WATCHLIST_COLUMNS`**。表头文本 / 列宽 / 拉伸权重
  三者必须同步, 而权重表是按表头**文本**索引的: 删列留死条目、加列查不到会走
  `batch_common` 的默认 1.0 (与「名称」同级), 窗口一拉宽就把富余宽度均摊给窄数字列。
  不报错、不红测试, 只是越拉越难看。「涨跌」列的表头带**动态日期**
  (`change_column_label`), 查权重时要归一化回 `CHANGE_COLUMN_KEY`。
  写死「日涨跌」是错的: 基准由盘上有没有那天的窄快照决定 —— 周一/长假后它其实是
  3 天涨跌, 而你没开过 GUI 的那些天更是直接跳过去。
- **「涨跌」「偏差Δ」缺基准时返回 None 显示「—」, 不是 0.0**。"没有基准"和"确实没变"
  必须分得开, 否则用户会把"我昨天没开过 GUI"读成"今天没动"。
- **整行颜色是稀缺通道, 只留给"否决", 不许表达贵/便宜**。`_resolve_row_tag` 三档:
  `new` (未进入市场, 价格类判据一律不适用, 优先级最高) → `blocked`
  (`TRADABILITY_RISK_TAGS`, 红 + **bold**, 实测 3/284) → `nodata`
  (`DATA_QUALITY_RISK_TAGS` ∪ `status != ok`, `TEXT_DIM` + *italic*, 实测 1/284)
  → 无色。两条独立理由: ① 便宜度已经被**行位置**编码完了 —— `sort_batch_results_for_view`
  对「综合机会/低估候选/转股折价」一律按 `relative_deviation` 升序, 而「低估候选」的准入
  判据本身就是 `rel < −5pp`, 任何架在便宜度上的行色在默认落地页上都是整表同色 (实测 40/40);
  ② 旧的绝对绿线 `dev < −3%` 换算到相对轴是 `rel < −(3% + 中位)`, 而橙线 `|rel| ≥ 20pp`
  优先级更高 —— **`绿 ⊂ 橙 ⟺ 当期中位 ≥ 17%`** (20 期估值基线里 5 期如此)。实测中位
  +20.86% 时 `underpriced` 渲染 **0/284** (独立判据其实命中侨银 −5.03% / 万讯 −3.93% /
  宝莱 −3.24% 三只, 全被橙吃掉), `overpriced` 占 75.4% —— 颜色通道近乎常量; 更糟的是
  「低估候选」40 行里有 **2 只被染红** (长汽 `rel=−15.46pp`、长海 `−15.73pp`), 页面说
  "这是最便宜的 40 只", 颜色说其中 2 只贵。**红绿轴因此整体退出这两张表**: `theme.GREEN`
  在本项目已有 4 种含义 (策略页收益为正 / 数据源可信 / 这里的"便宜" / A 股行情软件的"跌"),
  而关注池「涨跌」列是全 app 最像行情软件的一格 —— 一旦上色, 同一行会同时出现"红=涨(好)"
  与"红=贵(差)"。**「涨跌」「偏差Δ」两列不上色**: 方向已由 `+/−` 与数值承载, 而这两列
  可排序、进得了 CSV, 颜色两样都做不到。**「无色」是有含义的一档 (没有否决理由), 所以
  `row_colour_legend()` 是硬要求** —— 默认视图里 blocked 恒为 0 是设计意图, 没有那句话
  它和"配色坏了"长得一模一样 (与事件横幅空态那条同源)。
  `no_market` / `unpriced` 明确不进行色: 未上市新债没有市价是天然状态, 那两档由「数据」
  列的五档文案分开。
- **两个拦截维度不共用一个警报色 —— 是"警报 vs 静音"而不是两个红**。`blocked`
  (可交易性) 是关于**这只债**的事实、需要动作 (临近摘牌 = 30 天内必须卖掉);
  `nodata` (数据质量 ∪ 定价失败) 是关于**数据管线**的事实, 在选债页上无事可做,
  该去跑 `cb-data-doctor`。分开的理由不是频次 (实测 3 : 1), 是**降级场景**: 数据源
  抖一下「无市价」能一次命中几百行 —— 共用红色会让一屏红被读成"市场出事了", 而真相是
  "取数挂了"。优先级 **可交易性压过数据质量**: 一只「临近摘牌」且当天恰好取不到价的债,
  该看见的是"30 天内必须卖掉"。`nodata` 的静音必须配 *italic*: `TEXT_DIM` 与 `TEXT`
  在浅色下只差 7.06:1 → 4.37:1, 光靠"淡一点"分不出来。
  (`failed` 归灰是**恢复**老设计 —— 它原本就是 `TEXT_DIM`, 中途被并进过红色一次。)
- **方向相反的标签不许共享同一个视觉输出**。`深度低估待核` (机会信号维, 相对偏差中位
  **−21.95pp**) 与 `模型高估离群` (模型适用性维, **+27.76pp**) 曾被一个字面量集合合成
  同一个橙色行, 而 `batch_watchlist` 的摘要条又抄了第二份同样的字面量 —— 与「暂停转股
  与恢复转股同色」是同一次分叉的两半。方向常量收在 `batch_pricing`
  (`MODEL_OVERVALUED_TAGS` / `DEEP_UNDERVALUED_TAGS` / `LEGACY_DEVIATION_OUTLIER_TAGS`,
  最后那个是拆分前的对称旧名, **不带方向所以归不进任何一族**, 要报就单独报)。
- **ttk 的颜色分发链会静默失效, 加通道前先修链**。① `_render_table` 空结果**照样建表**,
  不许早返回: 早返回留下的是已 `destroy` 却还挂在 `_TREE_ATTRS` 上的悬垂 Treeview, 而
  `getattr(app, attr, None) is not None` 拦不住它 (它还是个对象) —— 真机 Tk **8.6.15**
  实测 `tag_configure` 抛 `TclError: invalid command name`。触发链是现成的: 默认落地
  「低估候选」(40 行) → 切「下修优势」或「转股折价」(**实测都是 0 行**) → 切主题。
  ② `refresh_theme` 因此**逐树 try/except 并把死树从注册表摘掉**: 异常从
  `for attr in _TREE_ATTRS` 抛出会中断整轮循环, 而那是个 `set`、遍历顺序随
  `PYTHONHASHSEED` 随机 —— 用户看到的不是崩溃, 是"切了下主题, 有些表变色有些没变,
  每次开机还不是同一批"。③ 通道预算是硬的: 每行只有 `foreground` / `background` /
  `font` 三个 tag 属性, 两张表都是 `show="headings"` 所以 `#0` 列不渲染、**图标那条路
  是关的**; 单元格级只能靠 getter 的字符串前缀, 且前缀必须是 **ASCII** (`_WIN_EMOJI_FALLBACK`
  之外的 emoji 在 Windows 上会降级成黑白线框, 而 macOS 上开发看不见这个失效)。
  tag 上写 `font=` 会盖住 `_apply_responsive_tree_font` 改的全局 style font, 所以
  `_apply_tag_colors` 要读 `tree._responsive_font_size` 而不是写死字号。
- **底色由数据决定的控件不许写死前景色**。`EVENT_TYPE_COLOR` 的 9 个色值全是 Latte
  (浅色) 档, 配写死的白字实测 **13/18 低于 WCAG AA 的 4.5:1、6 个连 3:1 都不到**,
  最差的 `down_reset_proposed` (#e6a700) 只有 **2.12:1** —— 而"下修提议"是在途事件,
  恰恰最该被读到。走 `theme.badge_text_color()` 在深浅两端里挑对比度更高的那个,
  判据跟着色值走而不是跟着一份会过期的白名单走。
- **事件横幅的空是常态, 不许 `grid_remove`**。实测关注池近 7 天与未来 30 天**都是
  0 件** (全池同口径 52 / 20)。藏起控件会重演「低估候选默认打开是空表、用户以为坏了」
  那次 —— 一个消失的控件和一个坏掉的控件长得一模一样, 所以空态要显式写
  「已扫 N 只 · 近 7 天与未来 30 天均无日程事件」。扫描集拆成两个:
  `_watchlist_scan_codes` 是主集 (铺明细), `_pool_scan_codes` 只报计数 ——
  原来那条理由 (「横幅真正的用处是告诉你**还不知道的那些**」) 依然成立, 不能因为
  换了页面就把全池那 50 多件事整个丢掉。
- **事件展示表只许有一份**: `EVENT_TYPE_SHORT_LABEL` / `EVENT_ACTIONABILITY` /
  `EVENT_END_LABEL` / **`EVENT_TYPE_COLOR`** 全在 `cb_events.py`。GUI 曾自带一份私有
  配色表只覆盖 14/18, 剩下 4 类 (balance_change / conversion_suspension /
  conversion_resume / unknown) 全渲染成同一个中性灰 —— 而暂停转股与恢复转股是**相反**
  的意思。这与"GUI 自带短标签表、badge 渲染出 bala/conv/unkn"是同一次分叉的两半。
  守护测试比对 `EVENT_TYPES` 全覆盖 + 相反事件不同色。
- **市价的 as-of 与估值日不是一回事**。`market_price_as_of` / `market_price_source`
  由 `pricing_api._latest_bond_close_with_provenance` 产出, 三档: `history` (行情序列,
  日期真实, **可能早于估值日** —— 停牌/节假日) / `terms_close` (条款库兜底, **没有
  as-of**, 可以任意旧: 日升转债库里的 `close=99.994` 是 2021 年撤销发行前的值) / `None`。
  陈旧**只标注不拒绝** —— 拒绝旧值会让那一行回到整行「—」, 而"空表"和"真的没数据"
  长得一模一样。展示层由「数据」列承载 (`_row_data_label` 五档)。
- **行情源全局只有一个下拉 (顶栏)**。`v_batch_source` 就是 `v_data_source` **本身**
  (同一个 StringVar 对象, 在 `_build_vars` 里赋), 不是两个 var 互相同步 —— 同步总会漏掉
  某条路径, 而漏掉的表现是"两页显示的源不一样", 用户无从判断哪个说了算。此前有三个下拉
  (顶栏 / 批量页 / 关注池主页) 控三条链路, 「我明明选了 akshare 怎么还在连 Wind」是找不出
  原因的那类问题。守护测试扫全 GUI 目录的 `CTkOptionMenu(variable=...)`。
- **NaN 不是 None, 判空一律用 `_is_finite`**。落盘时 NaN 写成 `null`, 读回来还原成 NaN
  (`watchlist_cache._NAN_FIELDS`, 与内存路径保持一致) —— 而 `NaN is not None` 为**真**,
  于是 `entry.get(k) is not None` 这种判据会放行 NaN 并把"今天没有市价"渲染成字面的
  `"nan"`。实测三只未上市新债的市价列全中。同一个坑在 `safe_date` / `pandas.NaT`
  那条约定里已经踩过一次 (NaT 是 datetime 子类且 `bool(NaT)` 为真)。
- **用户可见的按钮文案要有单一事实源**。`WATCH_REFRESH_LABEL` 定义在 `batch_watchlist`,
  按钮与状态栏那句"点「…」再试"都引它。实测事故: 按钮改名后消息里还写着旧名字, 用户在
  页面上找不到那个按钮。守护测试直接钉住"那句消息必须**插值**常量" —— 扫字面量抓不到
  真实故障形态 (留着一个**过期**的名字, 而不是重复写了当前名字)。
- **`tabs/home.py` 刻意不用 `from ..theme import *`**。pyproject 给 `tabs/batch.py` 与
  `tabs/batch_watchlist.py` 豁免了 `F403/F405`, 而 star import 会把本该报 **F821
  (未定义名)** 的错降级成 F405 被豁免吃掉 —— 那两个文件 `ruff check --isolated` 实测
  84 处告警全被吸收, 于是**任何拼错/漏删的名字 ruff 都看不见**, 只在真实渲染那一行抛
  NameError。这不是假想: 搬页时删掉一个 import 却留着两处调用, ruff 与 pytest 双双全绿。
  守护测试 `test_star_import_exemption_only_shields_real_theme_names` 把豁免收窄成
  "只放行 theme 里真实导出的名字"。新页不要把这道防线一起关掉。
- **`LEGACY_STRATEGY_EXCLUDE_TAGS` 是冻结集, 不要跟着标签体系演化**: 它是
  `ScoreStrategyConfig.exclude_risk_tags` 的默认值。曾写成 `tuple(sorted(HARD_REVIEW_TAGS))`,
  于是任何为改展示而增删标签的动作都自动变成**默认选债行为变更**。实测该集合极敏感:
  改成只排"数据质量+可交易性"候选池 59 → 262, 单去掉「偏差异常」→ 125。要改它单独立项。
- **保守过滤**: 准入筛选"字段明确才剔除"，避免因数据源缺字段误杀。**连续量不做硬阈值**:
  余额已从硬过滤降级为风险标签 (`DEFAULT_MIN_OUTSTANDING_BALANCE=None`) —— 硬阈值把
  "值得警惕"错误表达成"不存在", 一个字段解析错就让券无声消失; 而它此前 99% 的实际作用
  是替缺失的 `delisting_date` 兜底, 全库回填后独立贡献实测为 0。摘牌该由摘牌判据管
- **`is_tradable` / `trading_status` 是派生字段, 不能拿缓存值当独立证据**。公募转债这三个
  字段数据源根本不提供 (Wind `get_admission_status` 对它们显式返回 None), 缓存里读到的只可能是
  `infer_cb_trading_metadata` 自己上一次的输出。`is_issued_pending_listing` 的文档早就点名了
  这个自我确认陷阱, 但当时只堵了**判定侧**、没堵回填侧: 上市日到了之后, "已发行未上市"那一档
  留下的 `pending`/`False` 会覆盖新推断, 永远翻不回来 —— 实测派克转债 / 中仑转债两只上市首日
  分别成交 2.57 亿 / 12.95 亿的新债被准入判成"不可交易"。库内判据: **成交额 > 0 却
  `is_tradable=False`** 是自相矛盾, 全库正好只命中这两只。
- **「盘中停牌」不是停牌**。新债上市首日触发涨跌幅熔断时 Wind 的 `trade_status` 就返回
  "盘中停牌", 那是几分钟到半小时的机制性临停, 收盘照样有巨额成交。子串匹配会先命中"停牌",
  所以这类词必须在通用关键词**之前**识别 (`_INTRADAY_HALT_KEYWORDS`)。
- **撤销发行的债会以"条款齐备"的样子留在库里**。`123095.SZ` 日升转债 2021-01 发行申购,
  2021-02 东方日升业绩预告大幅亏损后**撤销发行**、申购资金退回, 从未上市交易; 但 Wind 里
  仍留着代码、到期日 2027-01-22 和一个 99.994 的陈旧价, 于是它带着 AA 评级被定出 −14%
  低估在主池里躺了三年 —— 库内每一项自洽性检查都正常, 因为它的条款确实一应俱全, 只是
  那个市场从未存在过。判据是 `_never_entered_market`: **有到期日**(说明这是一只被设计
  出来的债) 而**起息日与上市日同时缺失**(没有任何进入市场的痕迹)。三个条件缺一不可 ——
  实测全库"有上市日却没起息日"的 **0 只**, "有起息日却没上市日"的 35 只 (那是已发行未
  上市/老债数据缺口, 各有各的判据), 两个都缺的 10 只全部确认从未交易; 而少了"有到期日"
  这一条, 判据会退化成对任何信息不全的记录都开火, 与「字段明确才剔除」直接冲突。
  `infer_cb_trading_metadata` 兜不住这一档: 两个日期都没有时 `tradable_date` 为 None, 而
  `inferred_is_tradable = tradable_date is None or ...` 把"没有日期"读成"随时可交易" ——
  那个默认对定向债是对的, 对撤销发行的公募债恰好反了。
- **小批量标注要传锚, 而且传锚修不了秩**: `annotate_batch_results` 有两个独立开关,
  给关注池/新债这类**主池外的一小撮**标注时两个都要动 (GUI 侧统一走
  `tabs/batch._annotate_off_pool`, 有守护测试扫 GUI 目录里的裸调用)。
  ① `market_median_deviation=` 传主池锚 (从 `cross_section_anchor_from` 取, 它读的是
  **行内**的 `market_median_deviation`, **不是 `_meta`** —— 实测 `batch_pricing_cache.json`
  的 `_meta` 里根本没有这个键, 走那条路永远静默取不到)。不传时 `median_deviation_of`
  样本 <30 返回 None 退回绝对阈值; 真自算更糟 —— 6 行子集的中位就是它们自己, 每只的
  `relative_deviation` 恰好偏移一个中位 (实测 +20.86pp), 而那是个看上去完全正常的数字。
  ② `rank_scope=False` 把 `cheapness_rank/percentile/total` 等 9 个秩字段显式写成 None。
  秩是 `_assign_cross_sectional_ranks` 在**传进来的这一批内部**排的, 与锚无关: 实测
  123281.SZ 全池 `cheapness_percentile=0.8794`, 单独拿子集算变成 **0.0** —— 一个"全市场
  最便宜的 0%"标签。这一档尤其危险因为**没有自愈路径**: `_batch_all_results` 每轮都过
  `sort_batch_results_for_review` 在全池上重标注, 错了下一轮修回来; 而
  `_batch_upcoming_results` 标注一次之后再没人碰。
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
- **事件旗标与风险标签是两族, 不要合并**: `risk_tags` 驱动策略排除集
  (`LEGACY_STRATEGY_EXCLUDE_TAGS`)、置信度扣分与视图拦截 —— 加一个就是默认选债行为变更;
  `event_flags` (`batch_pricing.event_flags`) 只进展示与 CSV, 回答"这只债现在有没有正在
  发生的事"。旗标按**可操作性**排序而非字母序, 因为列窄只放得下前几条。刻意不收在
  近半数债上都亮的状态 (「已触发下修线」45% / 「下修冻结中」66%) —— 那描述的是市场
  不是这只债, 改由「距下修线」数值列与「下修优势」承载。
- **事件类型的展示词表与可操作性次序集中在 `cb_events.py`**
  (`EVENT_TYPE_SHORT_LABEL` / `EVENT_ACTIONABILITY` / `EVENT_END_LABEL`), 与写进
  `BondTerms` 状态字段的 `_event_status` **分开** (后者是数据, 前者是展示)。GUI 曾自带
  一份短标签表并漏掉 4 个类型, badge 渲染出 `bala`/`conv`/`unkn`, 且暂停转股与恢复转股
  两个相反的意思同显 `conv`。有守护测试比对 `EVENT_TYPES` 全覆盖 + 标签互不重复。
- **区间事件的 `effective_end` 要逐类型验过才能用**: 实测全库 7794 条,
  `call_no_redemption` 94% / `down_reset_rejected` 66% / `suspension` 86% / `putback` 60% /
  `call_redemption` 38% 有 end 且语义清楚; 而 `conversion_suspension`/`conversion_resume`
  虽然 70%/98% 有 end, 那个 end 却被公告正文里的**回售期区间**污染 (宝莱转债
  "关于回售期间…暂停转股" 解析出 `start=2021-03-11 end=2026-09-03`), 用它做未来事件提示
  会渲染出"恢复转股到期"这种胡话。**入窗判定与显示日期必须是同一个日期** —— 曾经判定看
  三个日期里任意一个、显示固定取 `effective_start`, 于是"未来 30 天"里冒出几个月前的日子。
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
- **解析不到就写 None, 不要回落成公告日**。`effective_start` 的通用回落值是公告日本身,
  而"最后交易日/申报期从公告当天开始"几乎恒为解析失败的信号。`call_redemption` 早就有
  这道守卫 (见 `apply_events_to_terms` 里的注释), `putback` 却漏了 —— 于是 177 条
  **法律意见书/核查意见** (它们本来就没有申报窗口) 每条都变成"从公告日开始、永不结束"
  的假窗口, 实测污染主池 28 只债的 `putback_start_date`。存量回洗走
  `cb-repair-putback-windows`。判断"这个字段是真解析出来的还是兜底填的"要有显式判据
  (`putback_window_is_complete` / `putback_start_is_degraded`), 解析侧与回洗侧共用。
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
- **信用评级的当前值走第三方 (akshare), 不走 Wind**。Wind 的 `creditrating` 是**发行时值**:
  `cb_data.json` 跨 17 个版本 (2026-04~08)、约 4000 次逐债 Wind 重取, 该字段变化 **0 次**,
  而同批刷新中 `conversion_price` 变了 287 次, 区间还完整覆盖 6 月法定年度跟踪评级季 ——
  库里因此出现过 搜特退债 / 鸿达退债 / 正邦转债 这类**已违约券仍标 AA**。
  评级经 `pricing_api._rating_spread_floor` 直接变成 pricer 的信用利差下限
  (AA 2.50% ↔ C 80.00%), 陈旧的高评级会系统性高估困境债的理论价。
  因此 `WindDataProvider.get_admission_status` **显式返回 `credit_rating=None`**
  (否则每日状态刷新会把第三方新值盖回冻结值), 当前值由 `cb-sync-ratings` 从
  `ak.bond_zh_cov()` 批量刷新 (一次调用拿全市场, 评级按年变动, 每月跑一次即可)。
  首次建档仍由 Wind `get_bond_terms` 兜底覆盖率。**实测落地: 136 只评级变化 (下调 120 /
  上调 16), 主池 298 → 284。**
  **全量 `cb-sync-tradable` 这条路也要堵**: `get_bond_terms` 照常返回冻结值, 一次全量重建
  就把第三方刷来的值整批盖回去 —— 实测 15 只已下调到 A-/A/BBB 的债被还原, 主池 285 → 301,
  那 15 只全是刚被"评级过低"正确剔除的 (中装转2 CC→AA、宏图转债 CC→A)。
  `cb_data_sync._LOCALLY_AUTHORITATIVE_FIELDS` 让本地非空值胜出, 空值仍由 Wind 兜底。
  **另一个副作用是良性的**: `get_bond_terms` 不取 `delisting_date` (只有
  `get_admission_status` 取), 所以全量同步会把它清成 None —— 但存续券的 `delist_date`
  恒等于 `maturity_date` (零信息量, 且是未来日期不触发剔除), 跑一次每日状态刷新即恢复。
  实测丢失的 314 只里**过去日期 0 只**, 死券混不进池。
- **判断评级对错不能用库内自洽**。评级没有任何库内裁判: Wind 冻结、公告解析无自校验。
  曾按"末条 patch 必须等于 cb_data"判 patch 脏, 据此删了 18 条又剥掉 330 条 —— 方向是反的
  (灵康转债第三方 `A-`、cb_data `AA-`、patch `A-`, patch 才对)。实测对第三方的精确命中率:
  cb_data(同步前) 79% / 平均差 0.55 档, 公告末条 84% / 0.42 档。
  **`cb-repair-events` 因此不碰评级**: 解析 bug 的错误方向 (后缀残缺 → 评级偏低) 与真实下调
  的方向完全重合, 任何架在数值上的启发式都分不开二者。
  `_parse_bond_credit_rating` 的 `rating_re` 带 `(?<![A-C])` 左界 —— 少了它 `.{0,10}` 回溯会让
  评级"尽量晚开始", 从 AA- 抠出 A-。这个正则 bug 是真的, 但**不能反推哪条存量 patch 是它造成的**。
  同理, 体检里拿 cb_data 跟 akshare 比是**自证**的 (前者就是从后者同步来的), 那条只量同步水位。
  **但"分歧 = cb_data 落后"这个方向在 `cb-sync-ratings` 落地后已经反过来了**: cb_data 现在
  是第三方当前值, 拿第三方逐条当裁判, 体检标记的 17 条分歧里 **15 条是公告 patch 错、
  cb_data 对**, 只有 1 条 (科蓝转债) 是真落后。所以那条检查改名「公告评级 vs cb_data 分歧」,
  只报分歧率、不断言谁错 —— 判谁对要靠与两边都独立的第三方 (`--online` 的「评级同步水位」)。
- **存量评级 patch 可以重放, 但不能靠数值猜**。"不能用启发式反推哪条是 bug 造成的"依然成立
  (偏低的方向与真实下调重合); 但**把公告正文重新取回来、用当前解析器重新推导**是换了独立
  证据源, 不是在旧数值上做判断。实测当前解析器在样本上从不产生错值: 要么解析对 (直接纠正
  存量错值), 要么返回 None (安全失败)。走 `cb-repair-rating-patches`, 三档处置不对称 ——
  解析出新值→改写 / 解析不出→删该字段 (无源之水) / **正文取不到→原样保留** (取不到证据
  ≠ 证据为否; 扫描件公告正文 0 字, 按"删"处理会销毁正确数据)。
- **评级报告末尾的「评级符号设置及含义」附录是词表, 不是状态**。跟踪评级报告会成段解释
  "列入评级观察是对于…评级观察分为'列入正面观察名单'…", 早期正则全文搜关键词就命中了它 ——
  实测主池 51 只带观察状态的债里 **47 只**的值来自那段附录 (皓元/国检/花园/鸿路四份 2026
  跟踪评级报告原句一字不差), 真正来自专项公告的只有 4 只。与 `parse_outstanding_balance_change`
  踩过的"赎回门槛条款被当成当期余额"是同一类陷阱。两道闸: `_strip_rating_legend` 按附录标题
  锚点截掉正文尾部 (鸿路那份 21342 字, 附录起于第 20662 字), 加 `_RE_WATCH_DEFINITION`
  排除系词/枚举句式 (有些机构把释义混排进正文)。
- **`credit_rating_outlook` / `credit_watch_status` 只有公告一个来源**: Wind 侧有候选字段
  (`ratingoutlook`/`creditratingoutlook`) 但实测取不到, cb_data 里 0/1058, `CBEvent` 里也不带
  这两个字段 —— 所以 `apply_events_to_terms` 那条路走不通, **唯一通道是 patch 投影**。
  它们因此登记在 `historical_terms._SNAPSHOT_UNCOVERED_FIELDS` 里: `terms_as_of` 的裁剪
  **逐字段**判, 快照覆盖不到的字段不裁 (对它们"快照已含更早的变更"根本不成立, 快照里是空的)。
  `credit_rating` **不在**这个集合里也不能加 —— 快照覆盖得到的字段就该由快照说了算。
- **`_meta.fetched_at` 不等于"条款抓取日"**。写它的有四条路径, 只有全量 `cb-sync-tradable`
  (source=`Wind`) 真抓条款; 每日 `Wind:admission_status`、每月 `akshare:ratings`、每日
  `cb_events` 都只刷各自那几个状态字段, 却一样把它推到今天 (实测 875/128/49 vs 6)。
  两个消费者按原意读它, 于是双双静默失效: `cb-sync-tradable --incremental` 把 **1052/1058**
  判成"7 天内已更新"而跳过 (还照常打印"已在 N 天内更新"); `terms_as_of` 把整段条款 patch
  按 `after=今天` 裁掉, live 定价路径上**一条 patch 都不生效** —— 两次全量同步之间的条款变更
  完全没有兜底 (实测晶瑞转2 K 差 19.5%、强力转债 16.5% 就掉在这个窗口里)。
  修法是 `_meta.fetched_at_by_source` 按来源分桶, `is_stale(..., source=)` /
  `fetched_at(..., source=)` 查对应那一格。**两个方向的兜底不对称**: 同步侧缺戳按"陈旧"处理
  (顶多多刷一次), 而 `terms_as_of` 缺戳必须回落到全局值 —— 返回 None 在投影层表示"不裁剪",
  会把整条 patch 链从发行日回放上来、拿陈旧值盖掉正确的 cb_data。
- **信用评级的 `sti` 后缀**: akshare 对部分券 (科创板 118xxx 段居多) 返回 `AA+sti` / `A-sti`,
  档位标准、后缀只是上游口径标记。早期按"值必须精确落在 `_RANK` 里"过滤, 于是这一整类券
  **每次同步都被静默跳过** —— 实测 26 只 (主池 21 只) 从未刷新过, 其中科蓝转债本地 `AA-` /
  第三方 `A-`, 差 3 档 (信用利差下限 3.50% vs 8.00%)。`sync_ratings.normalize_rating` 只剥
  已知后缀, 剥完必须精确命中 `_RANK`, 否则宁可返回 None 也不猜。
- **新债的上市日走窄同步 `cb-sync-new-issues`, 不要靠全量条款同步**。`listing_date` 全库只有
  `get_bond_terms` 一条写入通道 (Wind `ipo_date`), 而挂牌是**每天**发生的事 —— 两次全量同步
  之间新债一直空着上市日, 于是昨天挂牌的债今天仍被判「已发行未上市」而进不了主池。GUI 的
  「扫新债」原本靠"条款库超 1 天就提示跑 `--incremental`"兜底, 三处同时失效: ① 判龄读的是
  `bundle_meta()['updated_at']`, 而**任何**一次写盘都把它推到今天 (每日状态刷新即可),
  提示永不弹出 —— 这是 `_meta.fetched_at` 那个陷阱的第三个消费者; ② 就算弹出,
  `--incremental` 按 `is_stale(code, 7, source="Wind")` 判, **恰好跳过**刚被全量同步抓过的
  那批新债, 同时去取几百只无关的债 (实测 2026-08-25: 跳过 4 只、真取 741 只); ③ 没装 WindPy
  连提示都不给。真实规模是**每天几只** (实测全库 1058 只里在途新债 4 只), 所以判据不该是
  "库有多旧"而是"这几只债的上市日变了没" —— 直接同步, 不做闸门。
  取数走 akshare `bond_zh_cov` (一次调用 ~2s, 不需要 Wind), 口径实测与 Wind 完全一致:
  `上市时间` == `listing_date` **968/968**, `申购日期` == `issue_date` **974/974**。三条约定:
  写盘 source 桶固定 `akshare:new_issues` (进 `Wind` 那格会同时毒化 `--incremental` 与
  `terms_as_of`); 远端为空时**既不兜底也不清空** (`listing_date` 非空正是"已挂牌"的判据,
  伪造一个就让新债带着空市价混进主池); 目标集只看 `issue_date`/`listing_date`, 不看
  `trading_status`/`is_tradable` (自我确认)。
- **DataFrame 单元格一律走 `safe_date`, 不要用 `to_date`**。`pandas.NaT` 是 `datetime` 的
  **子类**且 `bool(NaT) is True` —— `to_date` 会原样放行, `x or fallback` 也不回落。实测
  akshare provider 的 `listing_date=listing_dt or issue_dt` 因此对**还没挂牌的新债**产出
  `NaT` 或起息日: 两种结果都让 `is_issued_pending_listing` 判成"已上市", 新债于是带着空市价
  进主池、同时从「扫新债」里消失。
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

**体检自己的判据也会被别处的改动悄悄弄坏, 而且同样是静默的**:

- `_looks_delisted` 曾写成 `delisting_date is not None`。当时全库只有 17 只有摘牌日, 没问题;
  2026-08-22 全库回填 (17 → 1041) 之后, 几乎每只在市债都带着一个**未来的**到期摘牌日, 于是
  「末条 patch == 当前值」跳过 **952/958 (99%)** 条链、只真检查 6 只, 藏着 30 只不符。判据
  必须是**日期已过**, 不是"有没有这个字段"。
- 「判死但今日有成交」的 `dead` 集曾只列 `{已退市, 已过最后交易日}`, 于是上市首日被判成
  "不可交易"/"停牌"的新债从底下整只漏过去 (派克转债当天成交 2.57 亿、中仑转债 12.95 亿,
  检查还报 0)。判据要按**语义**收全所有"断言这只债不能交易"的剔除原因; 反过来评级过低 /
  正股 ST / 成交额过低这些是**策略口径**, 收进来会天天误报几十只。
- **外部对照要成对**: 「判死但今日有成交」查**误杀**, 「主池却查无行情」查**误留** ——
  后者才抓得住日升转债那种"条款齐备但市场从未存在"的幽灵行。两条都要挡交易时段边界的
  假阳性: akshare 现货表的 `ticktime` **只有时分秒没有日期**, 收盘后仍留着上一交易日的
  行情, 而 `market_today()` 按 Asia/Shanghai 走 —— 在美西运行时本机上午已是上海次日凌晨。
  于是"最后交易日恰好是上一交易日"的债会被误报成仍在交易 (春23转债), "今天才挂牌"的债
  会被误报成查无行情 (先锋转债)。两边都用 `_RECENT_STOP_GRACE_DAYS` 留出这个窗口。

## 数据文件

- `data/cb_data.json` — 全部转债静态条款，不要手动编辑
- `data/cb_data_history/` — 按日期归档的条款快照 (历史回测取数)
- `data/cb_events.json` — 结构化事件表
- `data/cb_terms_patches.json` — 历史条款 patch (回测防未来信息)
- `data/cb_valuation_history.json` — 大类估值历史基线 (批量重算自动追加, 入版本库;
  每条带 `caliber` 口径标记, 缺失视为 `v1`, 见 `market_valuation.CALIBER_CHANGES`)
- `data/down_reset_overrides.json` — 人工下修覆盖 (可手动编辑)
- `data/batch_pricing_cache.json` — 批量定价缓存 (运行态, gitignored)
- `data/watchlist.json` — 关注池**意图层**: 我关注什么 + 加入瞬间的 `snapshot_*` (运行态, gitignored)
- `data/watchlist_pricing_cache.json` — 关注池**行情层**热缓存, 最新一期完整行, 逐只 upsert
  (运行态, gitignored)。与 `watchlist.json` 分开是因为两者的保留期与语义不同: 前者是
  "我为什么关注它"(永久), 后者是"它今天多少钱"(每次刷新重写)
- `data/watchlist_daily/YYYY-MM-DD.json` — 关注池按日窄快照, 每交易日一份、只追加,
  支撑"涨跌 vs 上一交易日"(运行态, gitignored)
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
cb-sync-ratings --apply                     # 从 akshare 第三方刷新信用评级 (每月; Wind 的是发行时冻结值)
cb-sync-new-issues --apply                  # 新债上市日窄同步 (每天; 秒级, 不需要 Wind)
cb-data-doctor                              # 数据体检 (每天跑批**前**先跑; --online 加外部对照)
cb-repair-events --apply                    # 存量迁移: 用当前解析器重放事件表与 patch 库
cb-repair-balance-patches --apply           # 一次性存量迁移: 清洗被赎回门槛条款污染的余额 patch
cb-repair-putback-windows --download --apply # 一次性存量迁移: 重取正文补回售申报窗口, 清掉退化的公告日起始日
cb-repair-rating-patches --download --apply # 一次性存量迁移: 重取正文重放评级/展望/观察状态 patch
python CB.py 128009.SZ                      # 单只定价
```

### 下修博弈建模 (三 regime, 按价格影响符号分)

`resolve_down_reset_intensity` 把观测合成成 pricer 入参, 三态互斥:

- **背景** (无确定性公告): "纯触发后"模型 — 触发线下方 (S < K·trigger_ratio) 一律按 `p_down` 年化概率下修 (每步 `1-exp(-p·dt)`, 网格无关), 触发线之上为 0。`p_down` = "触发后公司跟进下修"的年化概率; 不用"越跌越可能"的 S 渐变。
- **已公告** (确定性正贡献): 输出 `scheduled_reset_date/prob/kind/target_k` 一次性下修节点, pricer 在预期生效日近确定施加, 不再放大背景强度。两个子态:
  - `kind="proposed"` 待股东会: 生效日 = 提议日+`PROPOSED_EFFECTIVE_LAG_DAYS`, 概率 `PROPOSED_PASS_PROB`。
  - `kind="approved"` 已通过待生效: 生效日 = 公告生效日 (缺失按 `APPROVED_EFFECTIVE_LAG_DAYS` 兜底), 概率 `APPROVED_PASS_PROB`≈1; **仅当生效日 > 估值日才建节点 (防与条款刷新双计)**。
  - `target_k` = 公告解析到的下修后新 K (`parse_down_reset_new_price` 填 `CBEvent.event_price`); 缺失时 pricer 回落 premium/floor 估算。`target_k==现 K` 时节点自动成 no-op, 天然防双计。
  - **`target_k` 严格高于现 K 一定是解析错了, 必须丢掉**。下修公告正文开头会成段引用"历次
    转股价格调整情况", 而 `parse_down_reset_new_price` 取的是**第一个**"由 A 元/股 修正为
    B 元/股" —— 抓到的是几年前那次调整的 B。实测全库 147 条带 `event_price` 的下修事件里
    **106 条 (72%)** 方向不可能 (强力转债 2026-08-07 提议公告正文依次出现
    18.98/18.98/18.94/18.90/12.70, 解析结果 18.94 = 2021 年的值, 而当时 K 已是 12.70)。
    这个错值**不会算出错价** —— 节点是 `max(V, reset_value)`, 偏高的 target_k 只让 reset_value
    低于 V, 节点静默变 no-op —— 代价是下修价值被整只抹平: 实测晶能转债 uplift 0.024% →
    丢掉错值改用 premium/floor 估算后恢复到 **+11.28% (12.99 元)**。闸在
    `resolve_down_reset_intensity(current_k=)` 与 pricer `__init__` 各一道, **是 `>` 不是 `>=`**
    (等于现 K 是"下修已落地"的正常状态, 拦掉反而会让它被再算一遍)。
- **冻结** (强制为 0): `down_reset_block_until` 屏蔽下修价值至冷静期满。
- 常量经 `cb-calibrate-down-reset` 从历史事件校准; 改这些值或下修结构前先重跑校准。

> **关于 `event_price` 历史回填**: 不需要。`cb_data.json` 的 `conversion_price` 是 Wind
> `clause_conversion2_swapshareprice` 即**当前 K**, 已内含所有"已生效"的历史下修; 这些历史
> 事件也不会触发 regime-② 节点 (terminal/过期, 或 approved 生效日已过被守卫跳过)。`event_price`
> 仅服务**在途公告** (已提议未通过 / 已通过未生效) — 此时 cb_data K 仍是旧值, 节点需公告新 K。
> 存量事件 `event_price` 多为空 (解析代码后加), 但无害; 新公告同步时自动填充。
