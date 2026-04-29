---
paths:
  - "convertible_bond/batch_pricing.py"
  - "convertible_bond/admission_status.py"
---

# 批量定价 & 准入筛选规则

1. **保守过滤原则**: 准入筛选遵循"字段明确才剔除"规则 — 字段为 None 时不剔除, 只有数据明确触发风险条件时才排除
2. **评级分数**: 使用 `_RATING_SCORES` 字典映射, 新增评级时在这里添加
3. **HARD_REVIEW_TAGS**: 修改硬复核标签集合时需确认 `filter_batch_results_by_view` 各视图的过滤逻辑仍然正确
4. **opportunity_score 可为 NaN**: 无市价或理论价异常时 score 为 NaN, 上层排序逻辑已处理
5. **修改后必须跑测试**: `pytest tests/test_batch_pricing.py -x -q`
