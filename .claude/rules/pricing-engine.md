---
paths:
  - "convertible_bond/pricer.py"
  - "convertible_bond/pricing_api.py"
---

# PDE 定价引擎规则

修改这两个文件时必须遵守以下规则:

1. **pricer.py 修改后必须跑测试**: 运行 `pytest tests/test_pricer.py -x -q` 确认无回归
2. **pricing_api.py 修改后必须跑测试**: 运行 `pytest tests/test_pricing_api.py -x -q`
3. **不要修改 PDE 漂移/折现约定**: 漂移用 r, 折现用 r + spread(S); 这是核心设计决策
4. **半开区间票息**: `discrete_coupon_amount` 使用 `(start, end]` 半开区间, 不要改为闭区间
5. **年化强度**: p_down 是年化事件强度, 每步 `1 - exp(-p * dt)`, 不要直接用 p_down 作为概率
6. **网格默认值**: 单只定价 M=500/N=2000, 批量 M=300/N=1000, 不要随意修改默认值
7. **类型标注**: 使用 `X | None` 而非 `Optional[X]` (Python 3.10+)
