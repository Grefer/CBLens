#!/usr/bin/env python3
"""在**一棵全新导出的树**上跑 CI 的三步, 用来拦住"本机全绿、CI 全红"。

这一类失败的共同形状是**本机比 CI 宽松**: 测试在你机器上看得见的东西, CI 的全新
checkout 上没有。已经踩过两种 ——

- 有显示器: ``ttk.Style()`` / ``tkinter.Tk()`` 在 macOS 上悄悄成功, runner 上抛
  ``TclError`` (已由 ``tests/headless.py`` 解决, 现在是 pytest 的默认加载项);
- **有文件但没进版本库**: ``CBLens.spec`` 命中 ``.gitignore`` 的 ``*.spec``,
  而 ``tests/test_build_desktop.py`` 读它 —— 本机全绿, CI 报 ``FileNotFoundError``,
  两次推送连红三天没人发现。

第二种不可能靠一份名单挡住 (下一次换成哪个文件事先不知道), 所以这里不做名单:
新检出的树里**只有版本库里真有的东西**, 凡是"本机有、库里没有"的一次全抓,
包括还没发生的那些。

一个必要条件已经实测过: 检出树里的 ``convertible_bond`` 会压过 ``pip install -e``
装的那份 (import 解析到临时目录而不是仓库), 否则这个检查等于什么都没查。

用法::

    python scripts/check_like_ci.py            # 检查 HEAD (= push 上去的东西)
    python scripts/check_like_ci.py --rev abc1234

检查的是**某个 commit**, 不是工作区 —— 工作区里没提交的改动本来也推不上去。
工作区脏时会提醒一句, 但不改变检查对象。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

#: 与 ``.github/workflows/ci.yml`` 的三步逐字对应。改 CI 时这里要跟着改 ——
#: 两边分叉的表现是"本机这个脚本绿、CI 还是红", 那比没有这个脚本更误导人。
_TARGETS = ["convertible_bond", "CB.py", "gui.py", "scripts"]
_STEPS: list[tuple[str, list[str]]] = [
    ("Compile check", ["-m", "compileall", "-q", *_TARGETS]),
    ("Lint (ruff E9+F)", ["-m", "ruff", "check", "convertible_bond", "tests", *_TARGETS[1:]]),
    ("Test", ["-m", "pytest", "-q"]),
]


def _repo_root() -> Path:
    out = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                         capture_output=True, text=True, check=True)
    return Path(out.stdout.strip())


def _export(root: Path, rev: str, dest: Path) -> None:
    """把 *rev* 那一版检出到 *dest* —— 与 ``actions/checkout`` 同形。

    用 ``init`` + 浅 ``fetch`` 而不是 ``git archive``: 后者只吐工作树、**没有
    ``.git``**, 于是任何 shell 出去问 git 的用例 (``test_ci_parity`` 那条"读的文件
    必须被跟踪"就是) 在这里炸 ``CalledProcessError``, 而它在 CI 上明明是好的 ——
    一个比 CI 更严的检查会制造假红, 和假绿一样糟。只写 *dest*, 不碰仓库本体。
    """
    subprocess.run(["git", "init", "-q", str(dest)], check=True)
    subprocess.run(["git", "fetch", "-q", "--depth", "1", str(root), rev],
                   cwd=dest, check=True)
    subprocess.run(["git", "checkout", "-q", "FETCH_HEAD"], cwd=dest, check=True)


def _clean_env() -> dict[str, str]:
    """去掉会把仓库本体重新拽回 import 路径的环境变量。"""
    env = dict(os.environ)
    for name in ("PYTHONPATH", "PYTEST_ADDOPTS", "CBLENS_DATA_DIR"):
        env.pop(name, None)
    return env


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rev", default="HEAD", help="要检查的 commit (默认 HEAD)")
    parser.add_argument("--keep", action="store_true", help="成功后也保留导出树")
    args = parser.parse_args(argv)

    root = _repo_root()
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=root,
                           capture_output=True, text=True, check=True).stdout.strip()
    if dirty:
        print(f"[note] 工作区有未提交改动, 但检查的是 {args.rev} —— 那才是会被推上去的东西\n")

    workspace = Path(tempfile.mkdtemp(prefix="cblens-like-ci-"))
    tree = workspace / "tree"
    tree.mkdir()
    failed: list[str] = []
    try:
        _export(root, args.rev, tree)
        sha = subprocess.run(["git", "rev-parse", "--short", args.rev], cwd=root,
                             capture_output=True, text=True, check=True).stdout.strip()
        print(f"[like-ci] {sha} → {tree}")
        env = _clean_env()
        for name, cmd in _STEPS:
            print(f"\n── {name} " + "─" * max(0, 56 - len(name)))
            if subprocess.run([sys.executable, *cmd], cwd=tree, env=env).returncode:
                failed.append(name)
        print()
        if failed:
            print(f"[like-ci] 失败: {', '.join(failed)}")
            print(f"[like-ci] 导出树留在 {tree} 供排查")
            return 1
        print("[like-ci] 三步全过 —— 这一版推上去 CI 应当是绿的")
        return 0
    finally:
        # 失败时留着现场; 成功 (或导出本身就炸了) 就不留垃圾。
        if not failed and not args.keep:
            shutil.rmtree(workspace, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
