"""公告 PDF 本地缓存与系统阅读器打开.

事件层 (cb_events.json) 的每条公告事件都带有 ``url`` (巨潮静态 PDF 地址).
本模块负责: 按 (bond_code, event_date, url) 计算稳定的本地缓存路径,
缺失时下载, 然后调用系统默认 PDF 阅读器打开.

GUI 中事件时间线的"预览公告"按钮直接调用 :func:`fetch_and_open`.
"""
from __future__ import annotations

import hashlib
import logging
import os
import platform
import re
import subprocess
from datetime import date
from pathlib import Path

from .paths import data_dir

logger = logging.getLogger(__name__)


def project_pdf_cache_dir() -> Path:
    """``data/announcement_pdfs/`` — 本地 PDF 缓存根目录."""
    return data_dir("announcement_pdfs")


def _safe_filename_part(text: str) -> str:
    cleaned = re.sub(r"[^\w一-鿿\-]+", "_", str(text or "").strip())
    return cleaned[:60] or "untitled"


def announcement_pdf_path(
    bond_code: str,
    event_date: date,
    url: str,
    *,
    cache_dir: Path | None = None,
) -> Path:
    """根据 (bond_code, event_date, url) 计算稳定的本地缓存路径.

    URL 取 sha1 前 10 位作为去重指纹, 同一债同一天可能有多份公告也不会撞名.
    """
    base = cache_dir or project_pdf_cache_dir()
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
    folder = base / _safe_filename_part(bond_code)
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{event_date.isoformat()}_{digest}.pdf"


def fetch_announcement_pdf(
    url: str,
    out_path: Path,
    *,
    timeout: int = 20,
    force: bool = False,
) -> Path:
    """下载 PDF 到 ``out_path``; 若文件已存在且 ``force=False`` 则直接返回.

    使用原子写: 先写 ``.tmp`` 再 rename, 防止半截文件污染缓存.
    """
    out_path = Path(out_path)
    if not force and out_path.exists() and out_path.stat().st_size > 500:
        return out_path

    import requests

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    }
    resp = requests.get(url, timeout=timeout, headers=headers)
    if resp.status_code != 200:
        raise RuntimeError(f"PDF 下载失败 HTTP {resp.status_code}: {url}")
    if len(resp.content) < 500:
        raise RuntimeError(f"PDF 内容异常 (size={len(resp.content)}): {url}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".pdf.tmp")
    tmp.write_bytes(resp.content)
    tmp.replace(out_path)
    return out_path


def open_with_system_viewer(path: Path) -> None:
    """用系统默认 PDF 阅读器打开本地文件."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(str(path))
    system = platform.system()
    if system == "Darwin":
        subprocess.Popen(["open", str(path)])
    elif system == "Windows":
        os.startfile(str(path))  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", str(path)])


def fetch_and_open(
    bond_code: str,
    event_date: date,
    url: str,
    *,
    cache_dir: Path | None = None,
) -> Path:
    """端到端入口: 算路径 → (按需) 下载 → 系统阅读器打开 → 返回本地路径."""
    if not url:
        raise ValueError("公告 URL 为空, 无法预览")
    path = announcement_pdf_path(bond_code, event_date, url, cache_dir=cache_dir)
    fetch_announcement_pdf(url, path)
    open_with_system_viewer(path)
    return path


def fetch_only(
    bond_code: str,
    event_date: date,
    url: str,
    *,
    cache_dir: Path | None = None,
) -> Path:
    """下载 (按需) 但不打开外部阅读器, 返回本地路径. 供 APP 内嵌预览使用."""
    if not url:
        raise ValueError("公告 URL 为空, 无法预览")
    path = announcement_pdf_path(bond_code, event_date, url, cache_dir=cache_dir)
    fetch_announcement_pdf(url, path)
    return path


def render_pdf_pages(path: Path, *, dpi: int = 110, max_pages: int = 50):
    """把 PDF 渲染成 PIL.Image 列表 (按页顺序).

    用 ``pdfplumber.Page.to_image(...).original`` 获取 PIL 对象, 失败 / 缺依赖时抛
    ``RuntimeError``, 上层应该 fallback 到系统阅读器。``max_pages`` 防止误开几百页
    的招股书把内存打爆。
    """
    try:
        import pdfplumber
    except ImportError as exc:
        raise RuntimeError("pdfplumber 不可用, 无法在 APP 内预览 PDF") from exc

    images = []
    with pdfplumber.open(str(path)) as doc:
        for idx, page in enumerate(doc.pages):
            if idx >= max_pages:
                break
            page_img = page.to_image(resolution=dpi)
            images.append(page_img.original)
    if not images:
        raise RuntimeError(f"PDF 无可渲染页面: {path}")
    return images
