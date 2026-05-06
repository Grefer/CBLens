"""σ-S 敏感性网格计算 (纯函数, 不依赖 GUI).

把原本嵌在 ``CBPricerApp._sensitivity_worker`` 里的 numpy + PDE 计算抽到这里,
方便单测/复用; GUI 只负责取参数、跑线程、画图。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

import numpy as np

from .pricer import UniversalCBPricer


def compute_sensitivity_grid(
    pricer_kwargs: dict,
    model_kwargs: dict,
    *,
    s_grid: np.ndarray,
    sigma_grid: np.ndarray,
    max_workers: int = 4,
    progress_cb: Callable[[int, int], None] | None = None,
) -> np.ndarray:
    """计算 (sigma_grid × s_grid) 形状的理论价网格.

    参数:
        pricer_kwargs: 喂给 ``UniversalCBPricer`` 的 kwargs (S0 会被网格点覆盖)
        model_kwargs:  ``pricer.price`` 的 kwargs (sigma 会被网格点覆盖)
        s_grid:        正股价网格 (列方向)
        sigma_grid:    波动率网格 (行方向, 单位: 小数)
        max_workers:   并行线程数, ≤ CPU 核数为佳
        progress_cb:   每完成 1 个网格点回调 ``(done, total)``

    返回 ``grid[i, j]`` = sigma=sigma_grid[i], S=s_grid[j] 的理论价。
    """
    rows = len(sigma_grid)
    cols = len(s_grid)
    if rows == 0 or cols == 0:
        return np.empty((rows, cols))
    grid = np.zeros((rows, cols))
    total = rows * cols
    done = 0

    def compute_one(i: int, j: int) -> tuple[int, int, float]:
        local_pricer_kwargs = dict(pricer_kwargs, S0=float(s_grid[j]))
        pricer = UniversalCBPricer(**local_pricer_kwargs)
        local_model = dict(model_kwargs, sigma=float(sigma_grid[i]))
        return i, j, float(pricer.price(**local_model))

    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        futs = [pool.submit(compute_one, i, j) for i in range(rows) for j in range(cols)]
        for fut in as_completed(futs):
            i, j, v = fut.result()
            grid[i, j] = v
            done += 1
            if progress_cb is not None:
                progress_cb(done, total)
    return grid
