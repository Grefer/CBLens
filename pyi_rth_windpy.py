# _*_ coding: utf-8 _*_
"""
PyInstaller runtime hook: 让 Wind 终端自带的 WindPy.py 在冻结构建里定位 DLL.

背景
----
WindPy.py 里 ``class w`` 在 *import 阶段* 做这样的 bootstrap::

    sitepath = "."
    for x in sys.path:
        ix = x.find('site-packages')
        if ix >= 0 and x[ix:] == 'site-packages':
            sitepath = x
            break
    sitepath = sitepath + "\\WindPy.pth"
    pathfile = open(sitepath)
    dllpath  = pathfile.readlines()
    c_windlib = cdll.LoadLibrary(dllpath[0].strip() + "\\WindPy.dll")

系统 Python 里 Wind 安装器会在 ``Lib/site-packages/`` 放一个 ``WindPy.pth``,
第一行写 Wind 安装目录 (形如 ``C:\\Software\\Wind\\x64``).  冻结构建里
``sys.path`` 没有 ``site-packages``, ``sitepath`` 回退到 ".", open 必失败.

对策
----
本 hook 在 WindPy 被 import *之前* 就:
  1. 在 ``_MEIPASS/site-packages/`` 下写一份 ``WindPy.pth``, 内容是 ``_MEIPASS``
     绝对路径 (spec 文件已经把 ``WindPy.dll`` 等原生库都打到了 ``_MEIPASS`` 根)
  2. 把 ``_MEIPASS/site-packages/`` 插进 ``sys.path`` 开头

hook 只在 ``sys.frozen=True`` 时生效; 普通解释器下是 no-op, 让 Wind 终端装的
``WindPy.pth`` 继续工作.
"""

import os
import sys

if getattr(sys, "frozen", False):
    _mei = getattr(sys, "_MEIPASS", None)
    if _mei:
        _sp = os.path.join(_mei, "site-packages")
        try:
            os.makedirs(_sp, exist_ok=True)
            with open(os.path.join(_sp, "WindPy.pth"), "w") as _f:
                _f.write(_mei)
            if _sp not in sys.path:
                sys.path.insert(0, _sp)
        except Exception as _e:
            sys.stderr.write(f"[pyi_rth_windpy] setup failed: {_e!r}\n")
