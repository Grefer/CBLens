"""Provider 装饰器链的组成性守护。

背景: ``DataProvider`` 新增方法时按约定要在 base ABC 给兼容默认实现, 免得打断装饰器链。
但对**返回口径信息**的方法, 这个默认值会让遗漏**静默失败**而不是报错 ——
``terms_as_of`` 的默认是 None ("未知, 不裁剪"), 一旦某个装饰器忘了透传, 整条链的
锚就悄悄消失, 表现为"修了但没生效": 实测批量页因 ``_BatchStockCache`` 漏转发,
主池 168 只债的 K 仍被历史 patch 盖掉, 而所有单元测试都是绿的。

因此凡是**包着 inner provider** 又自己实现了 ``get_bond_terms`` 的类, 必须显式处理
``terms_as_of``: 要么透传内层, 要么按自己的条款来源给出锚。
"""
import importlib
import inspect
import pkgutil

import pytest

import convertible_bond as pkg
from convertible_bond.data_providers import DataProvider

_WRAPPER_PARAMS = {"inner", "market"}


def _all_provider_classes() -> dict[str, type]:
    found: dict[str, type] = {}
    modules = [pkg]
    for info in pkgutil.walk_packages(pkg.__path__, pkg.__name__ + "."):
        if ".gui" in info.name:          # CustomTkinter 在测试环境起不来
            continue
        try:
            modules.append(importlib.import_module(info.name))
        except Exception:
            continue
    for module in modules:
        for obj in vars(module).values():
            if (inspect.isclass(obj) and issubclass(obj, DataProvider)
                    and obj is not DataProvider):
                found[f"{obj.__module__}.{obj.__qualname__}"] = obj
    return found


def _is_wrapper(cls: type) -> bool:
    """构造函数收一个 inner/market provider 的即为装饰器。"""
    try:
        params = inspect.signature(cls.__init__).parameters
    except (TypeError, ValueError):
        return False
    return bool(_WRAPPER_PARAMS & set(params))


def test_provider_classes_are_discoverable():
    """守护本文件自身: 发现不到 provider 就说明扫描逻辑坏了, 后面的断言会假通过。"""
    classes = _all_provider_classes()
    assert len(classes) >= 8
    assert any(name.endswith("_BatchStockCache") for name in classes)
    assert any(name.endswith("CachedBondDataProvider") for name in classes)


@pytest.mark.parametrize("name, cls", sorted(_all_provider_classes().items()))
def test_terms_bearing_wrappers_handle_terms_as_of(name, cls):
    """包着 inner 又自己实现 get_bond_terms 的装饰器, 必须显式处理 terms_as_of。

    漏了不会报错, 只会让条款口径锚静默消失 —— 所以靠这条测试兜。
    """
    if "get_bond_terms" not in cls.__dict__:
        return                            # 叶子 provider 或不碰条款的装饰器
    if not _is_wrapper(cls):
        return                            # 叶子数据源: 返回 None (未知) 是合法的
    assert "terms_as_of" in cls.__dict__, (
        f"{name} 包着 inner provider 且自己实现了 get_bond_terms, "
        f"但没有实现 terms_as_of —— ABC 默认返回 None 会让条款口径锚静默丢失。"
        f"透传 inner 的实现, 或按自己的条款来源给出锚。"
    )
