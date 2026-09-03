"""原子写盘的单一实现.

AGENTS 的约定是「JSON 先写 ``.tmp`` 再 ``rename``，防半截文件」, 而它此前在 10 个
模块里各写一遍, 每一份都用**固定**的临时名 (``x.json.tmp``)。固定名让"原子"只对
崩溃成立, 对**并发**不成立: 两个进程同时写同一个文件时, 它们写的是同一个 tmp ——
A 写一半、B 写一半, 两份内容交错进同一个盘上对象, 然后各自 ``replace`` 一次, 发布
出去的就是一份坏 JSON。而这正是这个仓库的常态用法: GUI 的批量 worker 在跑, 用户
同时在终端里跑 ``cb-sync-events --apply``。``TermsBundle._merge_foreign_writes``
处理的是**内容**层面的并发 (别人新增的条目要并回来), 它管不到临时文件撞名。

顺带补 ``fsync``: 没有它, ``replace`` 之后系统崩溃仍可能留下一个长度正确、内容是零
的文件 —— 那比半截文件更难查, 因为它看上去是一份完整的空数据。
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_text(path: Path | str, text: str, *, encoding: str = "utf-8") -> Path:
    """把 *text* 原子地写到 *path*.

    临时文件建在**同一个目录**里 (``rename`` 只在同一文件系统内是原子的), 名字由
    ``mkstemp`` 保证唯一 —— 这是与旧写法唯一的实质区别, 也是并发安全的全部来源。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(path)
    except BaseException:
        # 失败时不留垃圾。``missing_ok`` 是因为 replace 可能已经成功而后续才炸。
        tmp.unlink(missing_ok=True)
        raise
    return path


def atomic_write_json(path: Path | str, payload: Any, **dump_kwargs: Any) -> Path:
    """``json.dump`` 的原子版。默认与仓库既有写法一致 (中文不转义 / 缩进 2 / 键排序)。"""
    dump_kwargs.setdefault("ensure_ascii", False)
    dump_kwargs.setdefault("indent", 2)
    dump_kwargs.setdefault("sort_keys", True)
    return atomic_write_text(path, json.dumps(payload, **dump_kwargs))
