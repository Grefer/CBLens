# _*_ coding: utf-8 _*_
"""
PyInstaller runtime hook: 让 WindPy 在冻结构建里定位原生库 (DLL / dylib).

背景
----
WindPy 在不同平台的 bootstrap 逻辑不同:

**Windows** — 通过 ``WindPy.pth`` 定位:
  WindPy.py 的 ``class w`` 在 import 阶段遍历 ``sys.path`` 找 ``site-packages``,
  读取其中的 ``WindPy.pth`` 获取 Wind 安装目录, 再 ``cdll.LoadLibrary`` 加载 DLL.
  冻结构建里 ``sys.path`` 没有 ``site-packages``, 回退到 ``"."``, open 必失败.

**macOS** — 硬编码绝对路径:
  WindPy.py 直接写死了::

      sitepath = "/Applications/Wind API.app/Contents/Frameworks/libWind.QuantData.dylib"
      quantpath = "/Applications/Wind API.app/Contents/Frameworks/libWind.Cosmos.QuantData.dylib"

  不读取 ``WindPy.pth``.  冻结构建里这两个路径不存在, import 阶段即失败.

对策
----
本 hook 在 WindPy 被 import *之前* 运行, 仅在 ``sys.frozen=True`` 时生效:

  macOS:
    1. 找到 ``_MEIPASS/WindPy.py``
    2. 把硬编码路径替换为 bundle 内的对应 dylib 路径
    3. 写回修改后的文件, 后续 ``import WindPy`` 载入修改版

  Windows:
    1. 在 ``_MEIPASS/site-packages/`` 下写 ``WindPy.pth``, 内容为 ``_MEIPASS`` 绝对路径
    2. 把 ``_MEIPASS/site-packages/`` 插进 ``sys.path`` 开头
       (spec 文件已把 WindPy.dll 等原生库打到 ``_MEIPASS`` 根目录)
"""

import os
import sys

if not getattr(sys, "frozen", False):
    # 普通解释器: no-op, 让 Wind 终端安装的 WindPy 正常工作
    pass

else:
    _mei = getattr(sys, "_MEIPASS", None)
    if not _mei:
        pass

    elif sys.platform == "darwin":
        # ── macOS: 替换 WindPy.py 中的 dylib 路径 (幂等, 每次启动都会更新) ──
        def _bundled_lib(_name):
            _candidates = [
                os.path.join(_mei, _name),
                os.path.join(os.path.dirname(_mei), "Frameworks", _name),
                os.path.join(os.path.dirname(_mei), "Resources", _name),
            ]
            _exe = os.path.abspath(sys.executable)
            _cur = os.path.dirname(_exe)
            for _ in range(6):
                _candidates.extend([
                    os.path.join(_cur, "_internal", _name),
                    os.path.join(_cur, "Frameworks", _name),
                    os.path.join(_cur, "Resources", _name),
                ])
                _next = os.path.dirname(_cur)
                if _next == _cur:
                    break
                _cur = _next
            for _candidate in _candidates:
                if os.path.isfile(_candidate):
                    return _candidate
            return None

        _sitepath = _bundled_lib("libWind.QuantData.dylib")
        _quantpath = _bundled_lib("libWind.Cosmos.QuantData.dylib")
        if _sitepath and _quantpath:
            _windpy_path = os.path.join(_mei, "WindPy.py")
            if os.path.isfile(_windpy_path):
                try:
                    import re
                    with open(_windpy_path, "r", encoding="utf-8") as _f:
                        _src = _f.read()

                    _patched = re.sub(
                        r'^(\s{8}sitepath\s*=\s*).*(libWind\.QuantData\.dylib).*$',
                        f'\\1{_sitepath!r}  '
                        '# patched by pyi_rth_windpy',
                        _src, flags=re.MULTILINE,
                    )
                    _patched = re.sub(
                        r'^(\s{8}quantpath\s*=\s*).*(libWind\.Cosmos\.QuantData\.dylib).*$',
                        f'\\1{_quantpath!r}  '
                        '# patched by pyi_rth_windpy',
                        _patched, flags=re.MULTILINE,
                    )

                    if _patched != _src:
                        with open(_windpy_path, "w", encoding="utf-8") as _f:
                            _f.write(_patched)
                except Exception as _e:
                    sys.stderr.write(f"[pyi_rth_windpy] macOS patch failed: {_e!r}\n")

    else:
        # ── Windows / Linux: 写入 WindPy.pth ─────────────────────
        _sp = os.path.join(_mei, "site-packages")
        try:
            os.makedirs(_sp, exist_ok=True)
            with open(os.path.join(_sp, "WindPy.pth"), "w") as _f:
                _f.write(_mei)
            if _sp not in sys.path:
                sys.path.insert(0, _sp)
        except Exception as _e:
            sys.stderr.write(f"[pyi_rth_windpy] setup failed: {_e!r}\n")
