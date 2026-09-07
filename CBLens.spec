# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 参考入口；与正式构建共用数据、隐藏导入和版本定义。

本地可运行 ``pyinstaller --noconfirm CBLens.spec``，要求干净 checkout。
发布请用 ``python scripts/build_desktop.py --ref <tag>`` 生成 zip 和身份清单。
"""
import json
import sys
from pathlib import Path

ROOT = Path(SPECPATH).resolve()
sys.path.insert(0, str(ROOT))
from scripts.build_desktop import _generate_spec, build_identity

identity = build_identity(ROOT, "HEAD")
identity_path = ROOT / "build" / "desktop_build.json"
identity_path.parent.mkdir(parents=True, exist_ok=True)
identity_path.write_text(json.dumps(identity, indent=2) + "\n", encoding="utf-8")
exec(compile(_generate_spec(ROOT, identity_path=identity_path), str(ROOT / "CBLens.spec"), "exec"))
