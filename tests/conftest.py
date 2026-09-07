"""无头模式的加载点。

**为什么是 conftest 而不是 ``addopts = -p tests.headless``**: 那个写法要求
``tests`` 包可 import, 而这取决于**怎么调 pytest** —— ``python -m pytest`` 会把 cwd
放进 ``sys.path`` (于是本机全绿), 裸 ``pytest`` 不会 (而 ci.yml 用的正是裸的),
实测 CI 直接 ``ImportError: Error importing plugin "tests.headless"``, 一条用例都没跑。
conftest 由 pytest 自己按路径加载, 不经过 ``sys.path``, 两种调法都成立。

补丁在 import 时生效, 而 conftest 先于任何测试模块被 import, 所以时机是够的。
"""
from . import headless  # noqa: F401  —— import 本身就是打补丁
