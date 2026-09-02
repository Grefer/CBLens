"""CLI 子命令包。

这里**只放常量**, 不 import 任何子模块 —— ``gui.py`` 的冻结入口要读白名单, 而它必须
保持轻: 一旦这里 import 了子模块, 每次启动 GUI 都要把 akshare/WindPy 那条链拉起来。
"""

from __future__ import annotations

#: GUI「🌐 同步池」菜单允许以子进程方式启动的模块。
#:
#: **必须是白名单**: 冻结包里这些模块由 ``gui.py --run-cli <模块>`` 分派 (见那里的
#: 说明), 而模块名来自 argv —— 不限死就等于给了"用命令行参数 import 任意模块"的口子。
#: 名单本身与 ``gui/controllers/wind_sync._POOL_SYNC_TARGETS`` 同步, 有守护测试比对。
POOL_SYNC_MODULES: frozenset[str] = frozenset({
    "convertible_bond.cli.sync_tradable",
    "convertible_bond.cli.sync_admission_status",
    "convertible_bond.cli.sync_events",
    "convertible_bond.cli.sync_new_issues",
})

#: 冻结包里用来分派 CLI 的 argv 开关 (见 ``gui.py``)。
RUN_CLI_FLAG = "--run-cli"
