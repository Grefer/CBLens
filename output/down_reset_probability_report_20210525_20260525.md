# 近 5 年可转债下修触发后公司下修概率

- 样本窗口: 2021-05-25 至 2026-05-25
- 数据源: 巨潮资讯网公告搜索接口；按月切分关键词抓取后去重。
- 主口径: 将同一发行主体/转债名称 120 天内的“提议下修→通过下修”合并为一轮 episode；分母只使用已有终态的 `approved + rejected`。

## 公告事件计数

- down_reset_approved: 640
- down_reset_proposed: 439
- down_reset_rejected: 2269
- down_reset_trigger_notice: 1588

## Episode 统计

- 已终态 episode: 2909
- 下修通过/实施: 640
- 不下修: 2269
- 仍仅看到提议、未见终态: 43
- 触发/决策后实际下修率: 640/2909 = 22.00%

## 提议后通过滞后

- 有提议且后续通过的 episode: 373
- 中位滞后天数: 17
- 平均滞后天数: 19.8

## 显式触发提示公告的 90 日跟踪

- 触发/预计触发提示公告: 1588
- approved: 245
- no_decision_in_90d: 126
- proposed_pending: 17
- rejected: 1200
- 90 日内已有终态的显式触发样本下修率: 245/1445 = 16.96%

## 输出文件

- 公告明细: `output/down_reset_cninfo_announcements_20210525_20260525.csv`
- episode 明细: `output/down_reset_episodes_20210525_20260525.csv`
- 显式触发跟踪: `output/down_reset_trigger_followups_20210525_20260525.csv`
- 机器摘要: `output/down_reset_probability_summary_20210525_20260525.json`

## 口径限制

- 这是公告口径，不是逐日行情精确回放口径；没有公告的潜在触发不会进入主分母。
- 巨潮关键词搜索可能受标题措辞影响；脚本保留公告 URL 便于抽样复核。
- `approved` 按公告终态计数；`proposed_pending` 未纳入主分母。