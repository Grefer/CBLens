"""原子写: 并发安全来自**唯一**的临时名, 不只是"先写 tmp 再 rename"."""
import json
import multiprocessing
import os
from pathlib import Path

import pytest

from convertible_bond.atomic_io import atomic_write_json, atomic_write_text


def test_temp_name_is_unique_per_write(tmp_path, monkeypatch):
    """两次并发写同一个文件不许用同一个临时名。

    旧写法一律是固定名 (``x.json.tmp``), 于是"原子"只对**崩溃**成立、对**并发**
    不成立: 两个进程写的是同一个盘上对象, A 写一半、B 写一半, 内容交错, 然后各自
    rename 一次 —— 发布出去的是一份坏 JSON。而这正是本仓库的常态用法: GUI 批量
    worker 在跑, 用户同时在终端里跑 ``cb-sync-events --apply``。
    """
    target = tmp_path / "cb_data.json"
    seen: list[str] = []
    real_replace = Path.replace

    def spy(self, other):
        seen.append(self.name)
        return real_replace(self, other)

    monkeypatch.setattr(Path, "replace", spy)
    for i in range(5):
        atomic_write_json(target, {"i": i})
    assert len(set(seen)) == 5, f"临时名重复了: {seen}"
    assert all(name != target.name for name in seen)
    # 临时名仍要落在**同一个目录**里 —— rename 只在同一文件系统内是原子的
    assert list(tmp_path.iterdir()) == [target]
    assert json.loads(target.read_text(encoding="utf-8")) == {"i": 4}


def test_failed_write_leaves_no_debris_and_no_target(tmp_path, monkeypatch):
    """写盘中途炸掉: 目标文件不许被动过, 临时文件不许留下。"""
    target = tmp_path / "a.json"
    atomic_write_json(target, {"good": 1})

    def boom(self, other):
        raise KeyboardInterrupt

    monkeypatch.setattr(Path, "replace", boom)
    with pytest.raises(KeyboardInterrupt):
        atomic_write_json(target, {"bad": 2})

    assert json.loads(target.read_text(encoding="utf-8")) == {"good": 1}
    assert list(tmp_path.iterdir()) == [target], "留下了临时文件残骸"


def test_serialisation_matches_the_repo_convention(tmp_path):
    """默认参数必须与仓库既有写法逐字一致 —— 否则一次改写就让版本库文件整体重排。

    ``cb_data.json`` / ``cb_events.json`` / ``cb_valuation_history.json`` 都进版本库,
    缩进或键序变一下就是几万行的假 diff。
    """
    payload = {"b": "中文", "a": [1, 2]}
    out = tmp_path / "x.json"
    atomic_write_json(out, payload)
    assert out.read_text(encoding="utf-8") == json.dumps(
        payload, ensure_ascii=False, indent=2, sort_keys=True)

    atomic_write_json(out, payload, sort_keys=False)
    assert out.read_text(encoding="utf-8") == json.dumps(
        payload, ensure_ascii=False, indent=2)

    atomic_write_json(out, payload, indent=None, sort_keys=False)
    assert out.read_text(encoding="utf-8") == json.dumps(payload, ensure_ascii=False)


def test_text_writer_creates_missing_parents(tmp_path):
    target = tmp_path / "deep" / "er" / "note.txt"
    atomic_write_text(target, "正文")
    assert target.read_text(encoding="utf-8") == "正文"


def _writer(args):
    path, marker = args
    atomic_write_json(path, {"who": marker, "pad": ["x" * 200] * 500})
    return marker


def test_concurrent_writers_never_publish_a_corrupt_file(tmp_path):
    """真的并发写一遍: 结果必须是**某一个**写入方的完整内容, 不是两份的拼接。

    payload 刻意做大 (~100KB), 单次 write 落不进一个原子的内核写, 固定临时名下
    交错是可复现的。
    """
    target = tmp_path / "shared.json"
    with multiprocessing.get_context("spawn").Pool(4) as pool:
        markers = pool.map(_writer, [(target, f"w{i}") for i in range(8)])

    payload = json.loads(target.read_text(encoding="utf-8"))   # 坏 JSON 会在这里抛
    assert payload["who"] in markers
    assert payload["pad"] == ["x" * 200] * 500, "内容被另一个写入方截断/交错了"
    assert [p.name for p in tmp_path.iterdir()] == [target.name]
    assert os.path.getsize(target) > 50_000
