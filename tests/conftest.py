"""无头模式的加载点。

**为什么是 conftest 而不是 ``addopts = -p tests.headless``**: 那个写法要求
``tests`` 包可 import, 而这取决于**怎么调 pytest** —— ``python -m pytest`` 会把 cwd
放进 ``sys.path`` (于是本机全绿), 裸 ``pytest`` 不会 (而 ci.yml 用的正是裸的),
实测 CI 直接 ``ImportError: Error importing plugin "tests.headless"``, 一条用例都没跑。
conftest 由 pytest 自己按路径加载, 不经过 ``sys.path``, 两种调法都成立。

补丁在 import 时生效, 而 conftest 先于任何测试模块被 import, 所以时机是够的。
"""
from . import headless  # noqa: F401  —— import 本身就是打补丁

import pytest

from convertible_bond import wind_config


@pytest.fixture(autouse=True)
def isolated_wind_settings(monkeypatch, tmp_path):
    """每项测试独立配置，不能读写用户设置或继承上项测试的接口选择。"""
    monkeypatch.setenv("CBLENS_CONFIG_DIR", str(tmp_path / "cblens-config"))
    for name in (
        wind_config.WINDPY_SESSION_ENV,
        "CBLENS_WINDPY_PATH", "WINDPY_PATH", "WINDPY_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(wind_config, "_session_selection", None)
