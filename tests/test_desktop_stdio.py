"""无控制台桌面包必须能把第三方库的异常安全送回主线程。"""

import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "pyi_rth_stdio.py"


def _run_child(code, *args):
    # 放在子进程：旧实现会将 tqdm 显示锁留在 worker，主线程析构时永久等待。
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code), str(HOOK), *args],
        capture_output=True, text=True, encoding="utf-8", timeout=15,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"}, check=True,
    )


@pytest.mark.parametrize("missing", ["both", "stdout", "stderr", "neither"])
def test_windowed_hook_preserves_existing_console_and_cli_pipes(missing):
    result = _run_child("""
        import runpy, sys
        original = {name: getattr(sys, name) for name in ('stdout', 'stderr')}
        for name in original:
            if sys.argv[2] in ('both', name):
                setattr(sys, name, None)
        sys.frozen = True
        runpy.run_path(sys.argv[1])
        for name, stream in original.items():
            current = getattr(sys, name)
            if sys.argv[2] in ('both', name):
                assert current is not None and current.writable()
                current.write('中文进度条 🆕\\n')
                current.flush()
                assert current.encoding.lower() == 'utf-8'
            else:
                assert current is stream, name + ' 管道被覆盖'
        # 重复执行不会关闭/换掉已有流。
        current = (sys.stdout, sys.stderr)
        runpy.run_path(sys.argv[1])
        assert current == (sys.stdout, sys.stderr)
        print('原有管道可写', file=original['stdout'])
    """, missing)
    assert result.stdout == "原有管道可写\n"


def test_windowed_pagination_error_callback_can_be_destroyed_on_main_thread():
    """用真实 akshare/tqdm 分页，假 HTTP 抛错；主线程清理 traceback 后还能继续。"""
    result = _run_child("""
        import gc, importlib, runpy, sys, threading
        original_stdout = sys.stdout
        sys.stdout = sys.stderr = None
        sys.frozen = True
        runpy.run_path(sys.argv[1])
        del sys.frozen
        from requests.exceptions import ReadTimeout
        from tqdm import tqdm
        from tqdm.std import TqdmDefaultWriteLock
        mod = importlib.import_module('akshare.bond.bond_zh_cov')
        # 模拟 Windows 锁路径，避免在 POSIX 子进程启动 resource_tracker。
        TqdmDefaultWriteLock.mp_lock = None
        tqdm.monitor_interval = 0
        calls = []
        class Response:
            def json(self):
                return {'result': {'pages': 1}}
        def fake_get(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                return Response()
            raise ReadTimeout('合成分页超时')
        mod.requests.get = fake_get
        callbacks = []
        def worker():
            try:
                mod.bond_zh_cov()
            except Exception as exc:
                callbacks.append(lambda exc=exc: (type(exc).__name__, str(exc)))
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        thread.join(5)
        assert not thread.is_alive()
        callback = callbacks.pop()
        assert callback() == ('ReadTimeout', '合成分页超时')
        assert len(calls) == 2
        # 旧代码在这一行释放失败 tqdm 实例时卡在 _decr_instances 的显示锁。
        del callback
        gc.collect()
        assert TqdmDefaultWriteLock.th_lock.acquire(timeout=.2)
        TqdmDefaultWriteLock.th_lock.release()
        finished = []
        def next_worker():
            finished.extend(tqdm(range(2), leave=False))
        thread = threading.Thread(target=next_worker, daemon=True)
        thread.start()
        thread.join(5)
        assert not thread.is_alive() and finished == [0, 1]
        print('分页失败回调与下一轮均完成', file=original_stdout)
    """)
    assert result.stdout == "分页失败回调与下一轮均完成\n"


def test_stdio_runtime_hook_runs_before_other_custom_hooks(monkeypatch):
    """检查生成 spec 实际传给 PyInstaller 的 hook 列表及顺序。"""
    import ast
    from scripts import build_desktop

    monkeypatch.setattr(build_desktop, "_detect_windpy", lambda: (False, None))
    tree = ast.parse(build_desktop._generate_spec(ROOT))
    start = next(i for i, node in enumerate(tree.body)
                 if isinstance(node, ast.Assign)
                 and any(isinstance(target, ast.Name) and target.id == "_runtime_hooks"
                         for target in node.targets))
    end = next(i for i, node in enumerate(tree.body)
               if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
               and isinstance(node.value.func, ast.Name) and node.value.func.id == "Analysis")
    namespace = {"os": os}
    exec(compile(ast.Module(body=tree.body[start:end], type_ignores=[]), "generated.spec", "exec"), namespace)
    assert namespace["_runtime_hooks"][0] == str(HOOK)
    assert str(ROOT / "pyi_rth_network.py") in namespace["_runtime_hooks"]
    analysis = tree.body[end].value
    value = next(keyword.value for keyword in analysis.keywords if keyword.arg == "runtime_hooks")
    assert isinstance(value, ast.Name) and value.id == "_runtime_hooks"
