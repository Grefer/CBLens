---
paths:
  - "convertible_bond/data_providers.py"
  - "convertible_bond/cache.py"
  - "convertible_bond/cb_data_sync.py"
---

# 数据层规则

1. **BondTerms 字段同步**: 新增 BondTerms 字段时, 必须同步更新:
   - `data_providers.py` 中的 `BondTerms` dataclass 定义 (含正确的类型注解)
   - 相关 Provider 的 `get_bond_terms()` 实现
   注: `cache.py` 的 `_json_dict_to_terms()` 由 `dataclasses.fields + get_type_hints` 自动驱动, 不需要再手动登记字段。
2. **原子写**: JSON 文件写入必须先写 `.tmp` 再 `rename`, 参考 `TermsBundle._save()` 模式
3. **DataProvider 接口**: 新增方法时先在 `DataProvider` ABC 中加默认实现 (返回 None 或空列表), 保持向后兼容
4. **akshare 重试**: 所有 akshare 网络调用必须通过 `_retry()` 包装, 处理瞬态网络错误
5. **Wind 字段差异**: Wind 的字段名在不同终端/权限下可能有差异, 使用 `_wss_candidates` 模式逐个候选尝试
