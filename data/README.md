# 项目数据目录

## `cb_data.json`

全市场存续可转债的**静态基础信息快照** (semi-static fields)，由 `TermsBundle` 维护。
runtime 会优先从此文件读转债基础信息，避免每次启动都打 Wind 接口。

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
    "_meta": {"fetched_at": "...", "source": "wind"}
  },
  ...
}
```

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

- `listing_date`: 数据源返回的上市/挂牌日期；没有显式字段时可能与 `issue_date` 相同
- `tradable_date`: 进入可交易或关注窗口的日期；定向/非标准代码段若无明确字段，默认用上市/发行后 6 个月估算
- `is_tradable`: 同步日视角是否已进入可交易日期
- `trading_status`: `tradable` / `pending` / `private_pending` / `private_tradable` / `private_unknown`
- `suspension_status`: 停牌/暂停交易等补充状态
- `call_status`, `call_announce_date`, `call_redemption_date`: 强赎公告和执行状态
- `down_reset_trigger_pct`, `call_trigger_pct`, `put_trigger_pct`: 下修 / 强赎 / 回售触发比例，单位为 `%K`。下修触发缺失时, 定价层显式使用 `85%K` 作为模型默认。
- `last_trading_date`, `delisting_date`: 最后交易日 / 摘牌日；已过最后交易日或已摘牌时从主池剔除
- `underlying_name`, `underlying_status`: 正股名称与风险状态，用于识别 ST / 退市风险
- `bond_turnover_amount`: 转债成交额，口径由数据源决定；用于风险标签和复核，不作为默认硬剔除

这些字段由 `convertible_bond.admission_status` 做增量刷新。刷新时只会写入数据源明确返回的非空值；
如果 Wind 某个候选字段不可用，不会清空本地已有值或人工维护值。

### 主池公开交易筛选

批量定价主池只硬剔除转债本身不能公开交易的标的：

- 不可交易或尚未进入可交易窗口
- 停牌 / 暂停交易
- 已过最后交易日、已摘牌或已到期
- 非沪深普通公募代码段，或名称明确为定向 / 非公开交易转债

强赎、临近摘牌、正股 ST / 停牌、低成交额、小余额、低评级等不再硬剔除；
这些信息进入风险标签、复核视图或单债提示，避免把仍可公开交易的债误杀。

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
- `putback`: 回售
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
`conversion_price`、评级、余额等。示例：

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
