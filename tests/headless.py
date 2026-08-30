"""pytest 插件: 在本机复现 GitHub runner 的无头环境。

用法::

    pytest -p tests.headless -q

macOS 的 Tk 不需要 X11, 所以 ``ttk.Style()`` / ``tkinter.Tk()`` 在本机悄悄成功、
在 CI 上抛 ``TclError: no display name and no $DISPLAY`` —— 这是"本机全绿、CI 全红"
最常见的来源 (实测连红三次推送都是同一条测试)。把 ``_tkinter.create`` 换成照样抛
那个错的函数, 就能在本机把 CI 的失败逐条复现出来, 不用等一轮 CI。

不是测试模块 (文件名不以 ``test_`` 开头), 不会被收集。
"""
import _tkinter
import tkinter


def _no_display(*_args, **_kwargs):
    raise tkinter.TclError("no display name and no $DISPLAY environment variable")


_tkinter.create = _no_display
