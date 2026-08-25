# 项目数据目录

## `cb_data.json`

全市场存续可转债的**静态基础信息快照** (semi-static fields)，由 `TermsBundle` 维护。
runtime 会优先从此文件读转债基础信息，避免每次启动都打 Wind 接口。

> **只增不删**：同步只是不再*写入*终止态的债（`is_terminal_terms` 会拦下已到期/违约的），
> 但从不删除已有条目；而 Wind 的"沪深可转债"成分表不再返回退市债，所以旧条目永远不会被回访。
> 因此本文件里近一半是已到期/已退市的债（1058 只中约 449 只），这是**档案库而不是存续清单**。
> 任何遍历全库的功能（主池准入、代码联想候选、批量定价）都必须自己过滤终止态，
> 不能假设"在 cb_data 里"= "还能交易"。判定终止要同时看简称里的 `(退市)` 后缀和日期字段：
> 强赎转股提前摘牌的债 `delisting_date` 常为空（全库仅 17 只有值），只有简称带后缀；
> 而 `maturity_date` 只兜得住自然到期。

### 文件结构

```json
{
  "_bundle_meta": {
    "updated_at": "2026-04-27T15:30:00",
    "source": "Wind",
    "n_bonds": 532
  },
  "128009.SZ": {
    "sec_name": "...",
    "underlying_code": "002...",
    "issue_date": "2020-07-30",
    "listing_date": "2020-08-17",
    "tradable_date": "2020-08-17",
    "is_tradable": true,
    "trading_status": "tradable",
    "maturity_date": "2026-07-30",
    "conversion_price": 52.77,
    "redemption_price": 107.0,
    "coupon_rates": [0.003, 0.004, ...],
    ...
    "_meta": {
      "fetched_at": "...",
      "source": "Wind:admission_status",
      "fetched_at_by_source": {"Wind": "...", "Wind:admission_status": "..."}
    }
  },
  ...
}
```

> `_meta.fetched_at` 是**这条记录上次被任何人碰过的时间**，不是条款抓取日。写它的有四条
> 路径，只有全量 `sync_tradable`（`source="Wind"`）真的抓条款；`Wind:admission_status`
> （每日状态）、`akshare:ratings`（每月评级）、`cb_events`（每日事件回写）都只刷各自那几个
> 字段，却一样把它推到今天。要问"条款有多旧"必须查 `fetched_at_by_source["Wind"]`
> （`bundle.fetched_at(code, source="Wind")` / `bundle.is_stale(code, n, source="Wind")`）——
> 用全局值会让 `sync_tradable --incremental` 永久空转、并让条款 patch 在定价路径上整段失效。
> 老库里没有这个字段时按"陈旧"处理，跑一次全量同步即自愈。

### 何时刷新

| 场景 | 命令 |
| --- | --- |
| 月初定期 (新债/退市/下修) | `python -m convertible_bond.cli.sync_tradable` |
| 每日状态字段 (停牌/强赎/ST/成交额等) | `python -m convertible_bond.cli.sync_admission_status` |
| 公告事件 (下修/强赎/回售等) | `python -m convertible_bond.cli.sync_events --apply` |
| 查看公开交易主池报告 | `python -m convertible_bond.cli.screen_pool` |
| 单只债的事件后 | GUI 顶部 🔄 按钮 |
| 仅查看当前状态 | `python -m convertible_bond.cli.sync_tradable --info` |

### 数据来源对比

- **转债基础信息**: 固定由 WindPy 获取并写入 `cb_data.json`，覆盖下修/强赎/回售触发比例、回售观察期、完整付息计划等 akshare 缺失字段。
- **动态行情/股息率/利率**: GUI 和批量定价中可选择 Wind 或 akshare。正股股息率会按行情源实时获取，取不到时模型参数 `q` 回退为 0；akshare 无法返回无风险利率时，程序放弃接口获取并保留界面/参数中的手工值。

### 交易状态字段

- `issue_date`: 发行日/起息日（Wind `carrydate`），票息期与应计利息的锚点；到期日恒为它的 N 周年
- `listing_date`: 上市/挂牌日期（Wind `ipo_date`，即首个交易日），通常比 `issue_date` 晚 2~4 周；
  已发行未上市的新债该字段为空
- `tradable_date`: 进入可交易或关注窗口的日期；定向/非标准代码段若无明确字段，默认用上市/发行后 6 个月估算
- `is_tradable`: 同步日视角是否已进入可交易日期
- `trading_status`: `tradable` / `pending` / `private_pending` / `private_tradable` / `private_unknown`

> 公募转债的 `tradable_date` / `is_tradable` / `trading_status` **数据源并不提供**，
> 它们由 `infer_cb_trading_metadata` 从 `issue_date` / `listing_date` 推断后写回本文件。
> 因此判断"是不是新债"只认 `issue_date` / `listing_date`：已过起息日但 `listing_date`
> 仍为空（且起息在 180 天内）= **已发行未上市**，强制 `trading_status="pending"`、
> `tradable_date=None`、`is_tradable=False`。缓存里的旧推断值不参与判断，否则一次误判
> 会被自己确认下来再也翻不回来。这类新债不进主批量池（剔除原因「已发行未上市」），
> 由批量页「🆕 扫新债」收进关注池。
- `suspension_status`: 停牌/暂停交易等补充状态
- `call_status`, `call_announce_date`, `call_redemption_date`: 强赎公告和执行状态
- `down_reset_trigger_pct`, `call_trigger_pct`, `put_trigger_pct`: 下修 / 强赎 / 回售触发比例，单位为 `%K`。下修触发缺失时, 定价层显式使用 `85%K` 作为模型默认。
- `last_trading_date`, `delisting_date`: 最后交易日 / 摘牌日；已过最后交易日或已摘牌时从主池剔除
- `underlying_name`, `underlying_status`: 正股名称与风险状态，用于识别 ST / 退市风险
- `bond_turnover_amount`: 转债成交额，口径由数据源决定；用于风险标签和复核，不作为默认硬剔除

这些字段由 `convertible_bond.admission_status` 做增量刷新。刷新时只会写入数据源明确返回的非空值；
如果 Wind 某个候选字段不可用，不会清空本地已有值或人工维护值。

### 主池公开交易筛选

**硬剔除**（`batch_pricing_exclusion_reason`，按顺序短路）：

- 非沪深普通公募代码段，或名称明确为定向 / 非公开交易转债
- 已摘牌、已过最后交易日、已到期
- 停牌 / 暂停交易；不可交易或尚未进入可交易窗口；已发行未上市
- 正股 ST / 退市风险、正股停牌
- 评级低于 `min_credit_rating`（默认 A+）
- 成交额低于 `min_turnover_amount`（默认关闭）

**不硬剔除，进风险标签**：强赎、临近摘牌、余额、高 HV、转股折价等。

> [!NOTE]
> **余额已从硬过滤降级为标签**（`DEFAULT_MIN_OUTSTANDING_BALANCE = None`）。
> 全库回填 `delisting_date` / `last_trading_date` 后实测：关掉余额门槛主池 270 → 270，
> 独立贡献为 0 —— 它此前 99% 的作用是替缺失的摘牌元数据兜底（被它剔除的 225 只里
> 223 只是余额恰为 0 的已退市券），而那个职责现在由日期判据接管，剔除理由也从
> 「余额过小」变成诚实的「已退市」。余额本身按 3,000 万法定停止交易线分档打标签。
> 需要恢复硬过滤给 `min_outstanding_balance` 填数值即可。

### 人工事件覆盖字段

这些字段不会由 Wind 自动同步，适合记录“不下修”等公告事件：

- `down_reset_block_until`: 该日期前不计下修博弈；无 `cb_events` / `down_reset_overrides.json` 时作为 fallback
- `down_reset_p_scale`: 单债下修强度事件乘数，作用于基础 `p_down`；`0` 表示完全不计下修博弈，`0.25` 表示按基础强度的 25%
- `down_reset_note`: 覆盖原因或公告摘要

### 注意

- 此文件是 git 跟踪的，提交前可 `git diff` 检查变化是否合理 (例如下修后只该影响一只债)
- 下修事件之后，**建议手动 🔄 刷新对应债** 而不是等月度全量同步，避免短期定价偏差
- 读取 `cb_data` 命中时不会请求 Wind；正股价格、历史波动率、股息率、Shibor 等动态字段仍会按选择的行情源请求

## `cb_events.json`

结构化公告事件表。它和 `cb_data.json` 解耦，用于记录有时间属性的公告：

- `down_reset_proposed`: 提议下修
- `down_reset_approved`: 下修通过 / 转股价格调整
- `down_reset_rejected`: 不下修
- `conversion_price_adjusted`: 权益分派等导致的转股价格调整
- `call_redemption`: 公告强赎
- `call_no_redemption`: 公告不强赎
- `putback`: 回售。**注意这个类型混着三种公告**：申报窗口的「提示性公告」(843 条)、
  律所/券商出的「法律意见书 / 核查意见」等配套文件 (177 条)、以及「回售申报情况 /
  结果公告」(3 条)。只有第一种带申报窗口，后两种解析不出 `effective_start` /
  `effective_end` 是正常的，不是解析失败。
- `rating_change`: 评级调整
- `delisting`: 摘牌 / 最后交易日
- `suspension`: 停牌

文件结构：

```json
{
  "_meta": {"updated_at": "2026-04-28T18:00:00"},
  "events": [
    {
      "bond_code": "118006.SH",
      "event_date": "2026-04-15",
      "event_type": "call_redemption",
      "raw_title": "关于实施赎回暨摘牌的公告",
      "effective_start": "2026-04-27",
      "effective_end": "2026-05-06",
      "parsed_status": "已公告强赎",
      "source": "Wind"
    }
  ]
}
```

同步命令：

```bash
python -m convertible_bond.cli.sync_events --limit 50
python -m convertible_bond.cli.sync_events --codes 118006.SH --apply
```

`--apply` 会把事件表应用回 `cb_data.json` 的状态字段，例如强赎公告会写入
`call_status / call_announce_date / call_redemption_date`，不强赎公告会写入
`call_no_redemption_until`，不下修公告会写入 `down_reset_block_until / down_reset_note`。
定价时以 `down_reset_overrides.json` 和 `cb_events.json` 中的最新公告为准，避免旧
`cb_data` 字段挡住后续事件。

会改变模型输入的公告还会生成 `cb_terms_patches.json`。例如“转股价格调整”
公告会解析调整前/调整后转股价和生效日，写成 `conversion_price` patch；
明确披露债项信用等级的评级公告会写成 `credit_rating` patch。
单只和批量定价会先读取 `cb_data.json`，再按估值日应用这些 patch 和事件状态。

## 历史策略回测的条款视角

策略回测会通过 `HistoricalBondDataProvider` 尽量按估值日重建当时可见信息：

1. 先从 `cb_data_history/YYYY-MM-DD.json` 选择不晚于估值日的最近一份完整条款快照。
2. 再应用 `cb_terms_patches.json` 中 `effective_date <= 估值日` 的条款变更。
3. 最后应用 `cb_events.json` 中 `event_date <= 估值日` 的公告事件。

`cb_terms_patches.json` 用于记录会直接改变模型参数的字段，尤其是下修后的
`conversion_price`、评级、余额等。

> [!WARNING]
> **余额 patch 的门槛条款陷阱**：赎回/回售/停止交易条款会成段引用"未转股余额少于
> 3,000 万元时公司有权赎回"。早期解析把这句门槛条款当成真实余额，写出 528 条
> `outstanding_balance = 0.3` 的错误 patch，覆盖 103 只债（其中 96 只真实余额
> ≥0.5 亿），使它们被准入过滤当成"余额过小"整批剔除。解析已按措辞而非数值修复；
> 存量用 `cb-repair-balance-patches --dry-run` 查看、`--apply` 回洗（自动备份）。

> [!WARNING]
> **回售窗口"从公告日开始、永不结束"的假象**：解析不到申报期时，`effective_start`
> 会回落成**公告日本身**，于是每一条配套文件都变成一个假窗口。实测主池 28 只债的
> `putback_start_date` 就是这么来的（美锦转债真实窗口 2025-12-01~12-05，却按第三次
> 提示性公告的日期存成 2025-12-11 且无截止日）。解析侧已改为"解析不到就是 None"；
> 存量用 `cb-repair-putback-windows --download` 查看、加 `--apply` 回洗（自动备份，
> 正文按 URL 落盘缓存可续跑）。这与上面余额 patch 是同一类错误：把**解析残缺**
> 当成**当期状态**。

示例：

```json
{
  "patches": [
    {
      "bond_code": "113001.SH",
      "effective_date": "2025-02-10",
      "field": "conversion_price",
      "value": 8.0,
      "source": "announcement",
      "note": "转股价格调整"
    },
    {
      "bond_code": "113002.SH",
      "effective_date": "2025-03-01",
      "fields": {
        "credit_rating": "AA",
        "outstanding_balance": 6.5
      }
    }
  ]
}
```

如果没有历史快照，回测会退回当前 `cb_data` 的静态字段，并清掉强赎、摘牌、
停牌、ST、不下修、成交额等日级/事件状态，再用事件表按日期重建；但当前转股价
等半静态字段仍可能带有未来信息。因此严肃回测应尽量补齐历史快照或条款 patch。
