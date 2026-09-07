#!/usr/bin/env python3
"""在**一棵全新检出的树**上跑 ci.yml 的每一步, 用来拦住"本机全绿、CI 全红"。

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

WORKFLOW = Path(".github/workflows/ci.yml")

#: 不在本机跑的步骤。**只有这一条**, 而且理由是硬的: 它会把 editable 安装重新指向
#: 临时树, 把你的开发环境搞坏 —— 而它在 CI 上装的东西, 本机早就装好了。
#: 新增步骤不会被静默漏掉: ``test_ci_parity`` 断言 ci.yml 里每个 ``run:`` 步骤要么
#: 在这里被跑, 要么显式登记在这张表里。
_SKIP_STEPS = {"Install dependencies"}


def _ci_steps(workflow: Path) -> list[tuple[str, str]]:
    """从 ci.yml 里读出 ``(步骤名, shell 命令)``。

    **命令不在这里重写一份** —— 上一版手抄成 ``python -m pytest``, 而 ci.yml 是裸
    ``pytest``; 两者只差"cwd 在不在 ``sys.path``", 而那恰好就是当时要抓的 bug,
    于是这个号称复现 CI 的脚本在唯一要紧的一维上和 CI 不同, 照样放行。抄一份就会
    分叉, 所以直接读。

    手写的小解析器 (不引 PyYAML): ci.yml 的结构固定, 而多一个依赖就多一种
    "本机装了 CI 没装"的失败 —— 正是这个脚本要防的那类。解析结果由
    ``test_ci_parity`` 兜底 (步骤数与内容都断言过)。
    """
    steps: list[tuple[str, str]] = []
    name: str | None = None
    block: list[str] | None = None
    block_indent = 0
    for raw in workflow.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        indent = len(raw) - len(raw.lstrip())
        if block is not None:
            if stripped and indent >= block_indent:
                block.append(stripped)
                continue
            steps.append((name or "?", "\n".join(block)))
            block = None
        if stripped.startswith("- name:"):
            name = stripped[len("- name:"):].strip()
        elif stripped == "run: |":
            block, block_indent = [], indent + 1
        elif stripped.startswith("run:"):
            steps.append((name or "?", stripped[len("run:"):].strip()))
    if block is not None:
        steps.append((name or "?", "\n".join(block)))
    return steps


def _repo_root() -> Path:
    out = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                         capture_output=True, text=True, check=True)
    return Path(out.stdout.strip())


def _export(root: Path, rev: str, dest: Path) -> None:
    """把 *rev* 那一版检出到 *dest* —— 与 ``actions/checkout`` 同形。

    用 ``clone`` 而不是 ``git archive``: 后者只吐工作树、**没有
    ``.git``**, 于是任何 shell 出去问 git 的用例 (``test_ci_parity`` 那条"读的文件
    必须被跟踪"就是) 在这里炸 ``CalledProcessError``, 而它在 CI 上明明是好的 ——
    一个比 CI 更严的检查会制造假红, 和假绿一样糟。只写 *dest*, 不碰仓库本体。
    """
    # ``clone --shared`` 借用仓库本体的对象库 (不复制), 所以任意 commit 都取得到 ——
    # ``fetch --depth 1 <sha>`` 不行, 取任意 SHA 要服务端显式放行
    # (``uploadpack.allowReachableSHA1InWant``), 实测对 --rev <sha> 直接 exit 128。
    subprocess.run(["git", "clone", "-q", "--shared", "--no-checkout", str(root), str(dest)],
                   check=True)
    subprocess.run(["git", "checkout", "-q", "--detach", rev], cwd=dest, check=True)


def _clean_env(shim_dir: Path) -> dict[str, str]:
    """去掉会把仓库本体拽回 import 路径的环境变量, 并补上 runner 有而本机未必有的
    ``python``。

    ci.yml 写的是 ``python -m compileall`` / ``python -m pip`` (runner 上
    ``setup-python`` 会提供 ``python``), 而 macOS 上常常只有 ``python3`` ——
    实测 ``/bin/sh: python: command not found``, 一个与仓库内容无关的**假红**。
    假红和假绿一样糟: 它会训练你忽略这个检查。所以造一个指向当前解释器的 shim,
    只在子进程的 PATH 里生效, 不动系统。
    """
    shim_dir.mkdir(parents=True, exist_ok=True)
    shim = shim_dir / "python"
    if not shim.exists():
        shim.symlink_to(sys.executable)
    env = dict(os.environ)
    for name in ("PYTHONPATH", "PYTEST_ADDOPTS", "CBLENS_DATA_DIR"):
        env.pop(name, None)
    env["PATH"] = f"{shim_dir}{os.pathsep}{env.get('PATH', '')}"
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
    tree = workspace / "tree"   # git clone 自己建
    failed: list[str] = []
    try:
        _export(root, args.rev, tree)
        sha = subprocess.run(["git", "rev-parse", "--short", args.rev], cwd=root,
                             capture_output=True, text=True, check=True).stdout.strip()
        print(f"[like-ci] {sha} → {tree}")
        env = _clean_env(workspace / "bin")
        for name, cmd in _ci_steps(tree / WORKFLOW):
            if name in _SKIP_STEPS:
                print(f"\n── {name}: 跳过 (会把 editable 安装指向临时树)")
                continue
            print(f"\n── {name} " + "─" * max(0, 56 - len(name)))
            # shell=True: 跑的就是 ci.yml 里那行 shell 命令原文, 不做二次解释。
            if subprocess.run(cmd, cwd=tree, env=env, shell=True).returncode:
                failed.append(name)
        print()
        if failed:
            print(f"[like-ci] 失败: {', '.join(failed)}")
            print(f"[like-ci] 导出树留在 {tree} 供排查")
            return 1
        print("[like-ci] 全部步骤通过 —— 这一版推上去 CI 应当是绿的")
        return 0
    finally:
        # 失败时留着现场; 成功 (或导出本身就炸了) 就不留垃圾。
        if not failed and not args.keep:
            shutil.rmtree(workspace, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
