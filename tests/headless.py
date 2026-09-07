"""pytest 插件: 在本机复现 GitHub runner 的无头环境。

由 ``pyproject.toml`` 的 ``addopts`` **默认加载** —— 靠文档里一句"记得加 -p"防守
是防不住的。要用本机真实的 Tk (那 1 条量字体的用例在无头下会 skip)::

    CBLENS_REAL_TK=1 pytest -q

**逃生口只能是环境变量, 不能是 ``-p no:tests.headless``**: 这个模块在 **import 时**
就把 ``_tkinter.create`` 换掉了, 而 ``addopts`` 里的 ``-p`` 先于命令行的 ``no:`` 生效,
等 pytest 去"屏蔽"它的时候补丁早就打上了 —— 实测两种跑法的 skip 数完全相同,
是一个看上去有效、其实什么也没关掉的开关。

macOS 的 Tk 不需要 X11, 所以 ``ttk.Style()`` / ``tkinter.Tk()`` 在本机悄悄成功、
在 CI 上抛 ``TclError: no display name and no $DISPLAY`` —— 这是"本机全绿、CI 全红"
最常见的来源 (实测连红三次推送都是同一条测试)。把 ``_tkinter.create`` 换成照样抛
那个错的函数, 就能在本机把 CI 的失败逐条复现出来, 不用等一轮 CI。

不是测试模块 (文件名不以 ``test_`` 开头), 不会被收集。
"""
import os

import _tkinter
import tkinter


def _no_display(*_args, **_kwargs):
    raise tkinter.TclError("no display name and no $DISPLAY environment variable")


if not os.environ.get("CBLENS_REAL_TK"):
    _tkinter.create = _no_display
