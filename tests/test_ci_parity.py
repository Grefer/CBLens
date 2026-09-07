"""本机跑测试的口径必须与 CI 一致 —— 这三条守的是"本机全绿、CI 全红"那一类。

它们钉的不是某个功能, 是**跑测试这件事本身的配置**: 配置一松, 下面上千条用例
照样全绿, 而 CI 红 —— 那正是这一类失败唯一的表现形式。
"""
import ast
import os
import subprocess
import sys
from pathlib import Path

import _tkinter

ROOT = Path(__file__).resolve().parent.parent

#: 用例读、但**故意**不进版本库的运行态文件。登记的代价要认清楚: 这些用例在 CI 上
#: 永远 skip, 只在维护者本机跑得起来 —— 那是"本机全绿 CI 全红"的反面, 同样是两边
#: 口径不一致, 只是失败方向相反 (CI 看不见回归, 而不是凭空报错)。
#:
#: 能登记的**只有会 skip 的**: 文件缺席时用例自己 `pytest.skip`。缺席就报错的那种
#: 不许进这张表 —— 那正是 CBLens.spec 那个 bug, 要修不要登记。
_RUNTIME_ONLY_INPUTS = {
    # 批量定价缓存 (gitignored, 见 AGENTS「数据文件」)。test_pricer 用它钉住
    # down_reset_floor == S0 的现存量级, 缺席时那条用例 skip。
    "data/batch_pricing_cache.json",
}


def test_headless_plugin_is_loaded_by_default_not_by_remembering_a_flag():
    """无头模式必须来自 ``addopts``, 而不是"记得加 ``-p tests.headless``"。

    此前这条约定只写在 AGENTS 里, 结果连红三次推送都是同一条测试 —— 人不会记得。
    判据取**运行时效果**而不是配置文本: 配置写对了但插件没生效 (拼错模块名、
    被别的 ``-p`` 顶掉) 一样是假绿, 而那种假绿只有量效果才看得出来。
    """
    if os.environ.get("CBLENS_REAL_TK"):
        import pytest
        pytest.skip("显式要了真实 Tk (CBLENS_REAL_TK=1), 这一条不适用")
    assert _tkinter.create.__module__ == "tests.headless", (
        "本机跑的是有显示器的宽松模式, 与 CI 不同口径。"
        " pyproject.toml 的 [tool.pytest.ini_options].addopts 里应有 -p tests.headless"
    )


def test_like_ci_runs_every_workflow_step_or_says_why_not():
    """``check_like_ci.py`` 漏掉 ci.yml 的某一步 = 比没有这个脚本更糟。

    分叉的表现是"本机这个脚本绿、CI 还是红": 你照着一个自称等价的检查放行, 而它
    查的根本不是同一批东西。**上一版就是这么漏的** —— 脚本把命令手抄成
    ``python -m pytest`` 而 ci.yml 是裸 ``pytest``, 而这条用例只比对模块名之后的
    参数, 对调用形式免疫, 于是两边一起放行、CI 一条用例都没跑。

    所以现在脚本直接读 ci.yml (不再有第二份命令), 这条改为守**覆盖面**:
    每个 ``run:`` 步骤要么真的跑, 要么显式登记在 ``_SKIP_STEPS`` 里。
    """
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import check_like_ci
    finally:
        sys.path.pop(0)

    steps = check_like_ci._ci_steps(ROOT / check_like_ci.WORKFLOW)
    names = [name for name, _ in steps]
    assert len(steps) >= 4, f"ci.yml 只解析出 {len(steps)} 个 run 步骤, 解析器坏了: {names}"
    for expected in ("Compile check", "Lint (ruff E9+F)", "Test"):
        assert expected in names, f"没解析到 {expected!r} 这一步: {names}"

    stale = check_like_ci._SKIP_STEPS - set(names)
    assert not stale, f"这些步骤 ci.yml 里已经没有了, 从 _SKIP_STEPS 删掉: {stale}"

    # 真正会被执行的那几步, 命令必须与 ci.yml 逐字一致 (读的就是同一份文件,
    # 这里再断言一次是为了钉住"解析出来的确实是命令行而不是别的什么")。
    ran = {name: cmd for name, cmd in steps if name not in check_like_ci._SKIP_STEPS}
    assert ran["Test"] == "pytest -q", f"Test 步骤解析成了 {ran['Test']!r}"
    assert ran["Lint (ruff E9+F)"].startswith("ruff check "), ran["Lint (ruff E9+F)"]


def test_pre_push_hook_sample_is_installable_and_points_at_the_real_checker():
    """钩子样板 / AGENTS 里的装法 / 被调的脚本, 三者必须对得上。

    **钩子失效是静默的**: push 照常成功, 只是没检查 —— 和"根本没装"长得一模一样。
    所以路径不许靠人肉同步: 改名 `pre-push.sample` 或 `check_like_ci.py` 而漏改另
    一头, 这条当场红。
    """
    sample = ROOT / "scripts/pre-push.sample"
    assert sample.is_file(), "scripts/pre-push.sample 不见了, AGENTS 记的装法会失败"
    assert os.access(sample, os.X_OK), "样板没有执行位, 软链过去的钩子跑不起来"

    body = sample.read_text(encoding="utf-8")
    assert "scripts/check_like_ci.py" in body, "样板没在调 check_like_ci.py"

    # AGENTS 里那条 ln 命令的目标必须真的存在 —— 文档腐烂同样是静默的。
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    install = "ln -sf ../../scripts/pre-push.sample .git/hooks/pre-push"
    assert install in agents, f"AGENTS.md 里没有这条装法: {install}"
    linked = (ROOT / ".git/hooks" / "../../scripts/pre-push.sample").resolve()
    assert linked == sample.resolve(), f"AGENTS 里的相对路径解析到了 {linked}"


def test_every_repo_file_the_tests_read_is_actually_tracked_by_git():
    """测试读的仓库文件必须进过版本库。

    ``CBLens.spec`` 命中 ``.gitignore`` 的 ``*.spec``, 而 ``test_build_desktop``
    读它 —— 本机有文件所以全绿, CI 全新 checkout 报 ``FileNotFoundError``,
    两次推送连红三天。这条在 CI 上自证, 不依赖谁的本机装了钩子。
    """
    tracked = set(subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                                 text=True, check=True).stdout.split("\n"))
    stale = sorted(p for p in _RUNTIME_ONLY_INPUTS if p in tracked)
    assert not stale, f"这些已经进版本库了, 从 _RUNTIME_ONLY_INPUTS 里删掉: {stale}"

    missing = []
    for path in sorted((ROOT / "tests").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            name = node.value
            # 判据是**本机存在这个文件、而版本库里没有** —— 只有这一种形状会本机绿
            # CI 红。"两边都没有"是普通的写错路径, 那条用例自己会报 FileNotFoundError。
            #
            # 扫的是**全部字符串常量**而不是 `ROOT / "x"` 那种拼法: 要守的那个真实
            # bug 长的是 `for spec in ("CBLens.spec", ...): (root / spec)` —— 路径
            # 在循环变量里, 按拼法扫一条都抓不到 (第一版就是这么写的, 变异验证时
            # 把 CBLens.spec 重新藏起来它照样绿)。
            # 先把明显不是路径的滤掉 —— docstring 里有句号也有斜杠, 直接拿去
            # is_file() 会 OSError (文件名过长)。
            if len(name) > 120 or not name.strip() or any(c in name for c in "\n\t "):
                continue
            if "/" not in name and "." not in name:
                continue                       # 目录名/普通词, 不当路径看
            try:
                exists = (ROOT / name).is_file()
            except OSError:
                continue
            if exists and name not in tracked and name not in _RUNTIME_ONLY_INPUTS:
                missing.append(f"{path.relative_to(ROOT)}: {name}")
    assert not missing, "测试读了没进版本库的文件 (CI 上会 FileNotFoundError): " + "; ".join(missing)
