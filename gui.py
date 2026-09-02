#!/usr/bin/env python3
"""
根目录 GUI 兼容入口.

旧版完整实现已拆分到 gui/ 包中.
保留本文件仅为兼容 `python gui.py` 这类历史启动方式.
"""

import sys


def _run_cli_from_argv(argv: list[str]) -> int:
    """``gui.py --run-cli <模块> [参数...]`` → 跑那个 CLI 的 ``main()`` 并退出。

    冻结的桌面包里 ``sys.executable`` **是 app 本身**, 不是 Python 解释器 ——
    ``[sys.executable, "-u", "-m", 模块]`` 那种写法会被 PyInstaller 的 bootloader
    连同 ``-m`` 一起吃掉, 结果是**又开了一个 GUI**。而「🌐 同步池」菜单正是这么起
    子进程的。这里给冻结包一个真正的入口: 参数由 app 自己解释。

    模块名来自 argv, 所以必须落在 ``POOL_SYNC_MODULES`` 白名单里 —— 否则就是
    "用命令行参数 import 任意模块"。

    四个 CLI 的 ``main()`` 签名不一致 (三个不收 argv, 自己读 ``sys.argv``), 所以这里
    统一改写 ``sys.argv`` 再调用, 对四个都成立。
    """
    import importlib

    from convertible_bond.cli import POOL_SYNC_MODULES

    module = argv[0] if argv else ""
    if module not in POOL_SYNC_MODULES:
        print(f"不允许的模块: {module!r}", file=sys.stderr)
        return 2
    sys.argv = [module, *argv[1:]]
    rc = importlib.import_module(module).main()
    return int(rc or 0)


if __name__ == "__main__" and "--run-cli" in sys.argv[1:]:
    _i = sys.argv.index("--run-cli")
    raise SystemExit(_run_cli_from_argv(sys.argv[_i + 1:]))
elif __name__ == "__main__" and any(arg in {"--diagnose", "--diagnostics"} for arg in sys.argv[1:]):
    from convertible_bond.desktop_diagnostics import main as _diagnostics_main

    raise SystemExit(_diagnostics_main(sys.argv[1:]))
else:
    from convertible_bond.gui.app import CBPricerApp, main

    __all__ = ["CBPricerApp", "main"]

    if __name__ == "__main__":
        main()
