"""Runtime paths for source checkouts and frozen desktop apps."""
from __future__ import annotations

import json
import logging
import os
import shutil
import sys
from pathlib import Path

from ._version import __version__

logger = logging.getLogger(__name__)


APP_NAME = "CBLens"
MPLCONFIGDIR_ENV = "MPLCONFIGDIR"
MPL_CACHE_DIR_ENV = "CBLENS_MPLCONFIGDIR"
_SEEDED_DATA_FILES = {
    "cb_data.json", "cb_events.json", "cb_terms_patches.json",
    "down_reset_overrides.json", "batch_pricing_cache.json", "cb_valuation_history.json",
}
_BUNDLED_DATA_ALIASES = {
    # 运行态批量缓存仍写入/读取 batch_pricing_cache.json；Release 构建则可携带
    # 一个只读种子文件，避免 CI 没有本机运行态缓存时桌面包首启空表。
    "batch_pricing_cache.json": ("batch_pricing_cache.json", "desktop_batch_pricing_cache.json"),
}


def is_frozen_app() -> bool:
    """True when running from a PyInstaller bundle."""
    return bool(getattr(sys, "frozen", False)) and hasattr(sys, "_MEIPASS")


def project_root() -> Path:
    """Repository root when running from source; PyInstaller temp root when frozen."""
    if is_frozen_app():
        return Path(getattr(sys, "_MEIPASS"))
    return Path(__file__).resolve().parent.parent


def is_source_checkout() -> bool:
    """True 表示 ``project_root()`` 真的是仓库根 (源码 checkout 或 editable 安装)。

    判据是根目录里有没有 ``pyproject.toml``。这一条存在的唯一理由是: **wheel 里不含
    数据**。实测 2026-09-03 ``pip wheel --no-deps .`` 产出的 92 个条目里 86 个是 ``.py``,
    其余 6 个全是 ``dist-info`` 元数据 —— ``data/`` 与 ``assets/`` **各 0 个文件**
    (两者都在包目录之外, 而 ``[tool.setuptools.packages.find]`` 只 include
    ``convertible_bond*``)。那种装法下 ``project_root()`` 就是 site-packages, 于是
    ``data_path()`` 会在 ``site-packages/data/`` 底下**建目录并往里写** —— 实测
    ``cb-screen-pool`` 报「总数: 0」却不说为什么, 而写进去的数据会随下一次 pip
    升级/卸载一起消失, 系统级安装还可能根本没有写权限。
    editable 安装实测 ``__file__`` 仍指向源码树 (pyproject 在), 所以归为源码 checkout。
    """
    return (project_root() / "pyproject.toml").is_file()


def _user_data_dir() -> Path:
    """平台级的用户可写数据目录 (桌面包与 wheel 安装共用)。"""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME / "data"
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Roaming"
        return root / APP_NAME / "data"
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / APP_NAME / "data"


def _user_cache_dir() -> Path:
    """平台级的用户缓存目录 —— 与 ``data/`` 分开: 丢了只是慢一次, 不丢任何数据。

    Windows 走 ``LOCALAPPDATA`` 而不是 ``data/`` 用的 ``APPDATA``: 字体缓存里存的是
    **本机**字体的绝对路径, 跟着漫游配置同步到另一台机器上只会是一堆坏路径。
    """
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / APP_NAME
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
        return root / APP_NAME / "Cache"
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base) if base else Path.home() / ".cache"
    return root / APP_NAME


def matplotlib_cache_dir() -> Path:
    """桌面包复用的 Matplotlib 缓存目录 (``MPLCONFIGDIR``)。

    **按版本分目录**: 缓存里那份 ``fontlist-vNNN.json`` 对随包分发的字体只存**相对**
    ``mpl.get_data_path()`` 的路径, 换 matplotlib 版本时文件名里的 ``vNNN`` 自己会变,
    但"这一版包里带了哪些字体"没有任何东西盯着 —— 拿 CBLens 版本当键, 升级只多付一次
    冷启动, 不会拿旧名单去找已经不在包里的字体文件。
    """
    override = os.environ.get(MPL_CACHE_DIR_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return _user_cache_dir() / f"matplotlib-{__version__}"


def _prune_stale_matplotlib_caches(keep: Path) -> None:
    """删掉旧版本留下的字体缓存目录 —— 只碰 ``_user_cache_dir()`` 下自己建的那几个。"""
    root = keep.parent
    if root != _user_cache_dir():
        return  # CBLENS_MPLCONFIGDIR 指到别处时不清理: 那不是我们建的目录
    try:
        siblings = list(root.iterdir())
    except OSError:
        return
    for entry in siblings:
        if entry == keep or not entry.name.startswith("matplotlib-"):
            continue
        if not entry.is_dir():
            continue
        shutil.rmtree(entry, ignore_errors=True)


def use_persistent_matplotlib_cache(*, force: bool = False) -> Path | None:
    """把 ``MPLCONFIGDIR`` 指到跨启动存活的目录; 返回真正生效的路径 (没改则 None)。

    **这是桌面包启动慢的主因**。PyInstaller 的标准钩子 ``pyi_rth_mplconfig`` 每次启动
    ``secure_mkdtemp()`` 一个全新临时目录当 ``MPLCONFIGDIR``, 退出时删掉 —— 于是
    ``fontlist-vNNN.json`` **每次都要重建**。实测本机 (Python 3.13 / matplotlib 3.10.8)
    冷缓存 ``font_manager`` 要 **8.23s**, 热缓存 **0.005s**; 而 GUI 在建 Tk 窗口**之前**
    就经 ``controllers.backtest → pyplot`` 走到这一步, 那 8 秒整个落在首窗等待上。

    钩子当年的理由 (注释里写的 ``fontList.cache`` 指向上一个已删除的 ``_MEIxxxxx``)
    在现代 matplotlib 上**已经不成立**: ``_JSONEncoder`` 把随包字体存成相对
    ``mpl.get_data_path()`` 的路径, ``_json_decode`` 读回来再拼上**当前**的数据路径。
    实测连开三次、每次换一个 ``_MEIPASS``: 38 个随包字体全部落到当次的路径上,
    ``missing_files=0``, 载入 0.0009s。系统字体那部分是绝对路径, 但同一台机器上不变。

    只在**冻结包**里改 (``force=True`` 供测试): 源码 checkout 本来就用
    ``~/.matplotlib`` / XDG 缓存, 已经是持久的, 再插一手只会多出第二份缓存。

    必须在 **``import matplotlib`` 之前**调用 —— ``matplotlib/__init__`` 在导入时就把
    ``get_configdir()`` / ``get_cachedir()`` memo 住了。而且必须在**入口脚本**里调,
    不能写成自定义 runtime hook: PyInstaller 的自定义钩子跑在内建钩子**之前**
    (``analysis.py`` 里 custom hooks 排在 ``priority_scripts`` 头部), 设了也会被
    ``pyi_rth_mplconfig`` 无条件覆盖掉。
    """
    if not force and not is_frozen_app():
        return None
    if "matplotlib" in sys.modules:
        # 设了也没用 (配置目录已 memo)。静默失败等于"改完还是慢 8 秒且查不出原因"。
        logger.warning(
            "matplotlib 已导入, %s 不再生效 —— 持久字体缓存必须在导入前设置。",
            MPLCONFIGDIR_ENV,
        )
        return None
    target = matplotlib_cache_dir()
    try:
        target.mkdir(parents=True, exist_ok=True)
        # 探针名带 pid: 冻结包里 GUI 与 `--run-cli` 子进程可能同时起, 同名探针会互相
        # 把对方的文件 unlink 掉, 表现成"目录不可写"而白白退回临时目录。
        probe = target / f".write-probe-{os.getpid()}"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        # 目录不可写就留着钩子那份临时目录: 慢, 但能用。
        logger.warning("Matplotlib 持久字体缓存不可用 (%s): %s", target, exc)
        return None
    os.environ[MPLCONFIGDIR_ENV] = str(target)
    _prune_stale_matplotlib_caches(target)
    return target


_warned_installed_layout = False


def _warn_installed_layout_once(target: Path) -> None:
    """非源码安装只提示一次 —— 空数据目录不能和「程序坏了」长得一样。"""
    global _warned_installed_layout
    if _warned_installed_layout:
        return
    _warned_installed_layout = True
    logger.warning(
        "检测到非源码安装 (wheel 不携带 data/): 数据目录回落到 %s, 首次使用多半是空的。"
        "要用仓库里的数据请用 pip install -e 的源码 checkout, 或把 CBLENS_DATA_DIR 指向它。",
        target,
    )


def _frozen_resource_roots() -> list[Path]:
    """Candidate roots that may contain bundled resources in PyInstaller builds."""
    roots: list[Path] = []
    if is_frozen_app():
        mei = Path(getattr(sys, "_MEIPASS"))
        roots.append(mei)
        roots.append(mei.parent / "Resources")
        roots.append(mei.parent / "_internal")

        exe_parent = Path(sys.executable).resolve().parent
        roots.append(exe_parent / "_internal")
        for parent in exe_parent.parents:
            if parent.name == "Contents":
                roots.extend([
                    parent / "Resources",
                    parent / "Frameworks",
                    parent / "MacOS" / "_internal",
                ])
                break
    else:
        roots.append(project_root())

    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root)
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return unique


def bundled_data_path(filename: str) -> Path | None:
    """Return the bundled seed data path when present."""
    candidate_names = _BUNDLED_DATA_ALIASES.get(filename, (filename,))
    for root in _frozen_resource_roots():
        for candidate_name in candidate_names:
            candidate = root / "data" / candidate_name
            if candidate.exists():
                return candidate
    return None


def app_data_dir() -> Path:
    """Writable data directory used by packaged desktop apps.

    源码 checkout (含 editable 安装) 保持历史行为 ``<repo>/data``, 除非设了
    ``CBLENS_DATA_DIR``。frozen 桌面包与 **pip 装的 wheel** 都用平台级用户目录 ——
    后者此前落回 ``project_root() / "data"``, 而那时的 ``project_root()`` 是
    site-packages (见 ``is_source_checkout`` 的实测)。
    """
    override = os.environ.get("CBLENS_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if not is_frozen_app():
        if is_source_checkout():
            return project_root() / "data"
        target = _user_data_dir()
        _warn_installed_layout_once(target)
        return target
    return _user_data_dir()


def _needs_seed(target: Path, filename: str | None = None) -> bool:
    """True when the target file is missing or looks corrupt/empty."""
    if not target.exists():
        return True
    if filename == "cb_terms_patches.json":
        # 升级只补缺失的 patch 库。已有文件可能含用户回洗/同步结果，即使为空或
        # 损坏也不擅自用发行种子覆盖；严格诊断会报告损坏，修复由显式迁移处理。
        return False
    try:
        if target.stat().st_size < 10:
            return True
    except OSError:
        return True
    if filename and filename.endswith(".json"):
        try:
            with open(target, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except (json.JSONDecodeError, OSError):
            return True
        if filename == "cb_data.json":
            return not (
                isinstance(payload, dict)
                and any(not str(k).startswith("_") for k in payload)
            )
        if filename == "batch_pricing_cache.json":
            results = payload.get("results") if isinstance(payload, dict) else None
            return not (
                isinstance(payload, dict)
                and isinstance(results, list)
                and any(isinstance(row, dict) and row.get("status") == "ok" for row in results)
            )
    return False


def data_path(filename: str, *, seed: bool = False) -> Path:
    """Return a writable data file path, optionally seeding it from bundled data."""
    root = app_data_dir()
    root.mkdir(parents=True, exist_ok=True)
    target = root / filename
    if seed and filename in _SEEDED_DATA_FILES and _needs_seed(target, filename):
        bundled = bundled_data_path(filename)
        if bundled is not None and bundled.resolve() != target.resolve():
            try:
                shutil.copy2(bundled, target)
                logger.info("seeded %s from bundle → %s", filename, target)
            except OSError as exc:
                logger.warning("seed %s 失败: %s", filename, exc)
        elif bundled is None:
            logger.warning(
                "seed %s 跳过: bundled 源文件不存在, 请确认构建时 data/ 已包含此文件; candidates=%s",
                filename, [str(p / "data" / filename) for p in _frozen_resource_roots()],
            )
    return target


def seed_data_files() -> list[Path]:
    """Ensure all bundled seed data files are copied to the writable data dir.

    Safe to call multiple times; only missing/corrupt files are re-seeded.
    Returns the list of target paths.
    """
    targets: list[Path] = []
    for filename in sorted(_SEEDED_DATA_FILES):
        targets.append(data_path(filename, seed=True))
    return targets


def data_dir(*parts: str) -> Path:
    """Return a writable data directory path."""
    path = app_data_dir().joinpath(*parts)
    path.mkdir(parents=True, exist_ok=True)
    return path


def asset_path(filename: str) -> Path:
    """Return an asset path from source or a PyInstaller bundle."""
    return project_root() / "assets" / filename
