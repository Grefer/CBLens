---
paths:
  - "tests/**/*.py"
---

# 测试规则

1. **不要修改已通过的测试用例**: 除非业务逻辑确实发生了变化
2. **测试文件命名**: `test_<module_name>.py`, 与被测模块一一对应
3. **测试类命名**: `Test<FeatureName>`, 按功能分组
4. **Mock WindPy**: Wind 相关测试必须 mock `WindPy`, 不要依赖实际 Wind 连接
5. **运行测试**: `pytest tests/ -x -q` (快速失败模式)
