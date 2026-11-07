"""调试图落盘工具。

服务端返回的图是 ``data:image/<type>;base64,<payload>`` 这种 data URL，
调试时需要把它存成 png 看。
"""
from __future__ import annotations

import time
from pathlib import Path

import cv2
import numpy as np


def new_debug_filename(prefix: str, debug_dir: str | Path) -> Path:
    """生成带时间戳的调试图路径，如 data/debug/slider_20261029_132001_bg.png。"""
    d = Path(debug_dir)
    d.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S") + f"_{int(time.time() * 1000) % 1000:03d}"
    return d / f"{prefix}_{stamp}.png"


def save_debug(img: np.ndarray, path: Path | str) -> Path:
    """用 cv2.imwrite 保存 BGR 图；返回写盘路径。"""
    p = Path(path)
    ok = cv2.imwrite(str(p), img)
    if not ok:
        raise IOError(f"cv2.imwrite 失败: {p}")
    return p
