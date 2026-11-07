"""基于 ``randomY`` 的拼图缺口左边缘检测。

**为什么不用模板匹配**：服务端下发的 ``slidingImage`` 是「从背景图上挖下来的
那一块」，它已经**不包含**背景内容了。拿它当模板去 matchTemplate 背景图，
匹配的是被挖走后的空洞纹理，天然对不上（实测 score 只有 0.2~0.5）。

**正确思路**：服务端同时给了 ``randomY``（缺口的纵坐标），所以我们不需要
在整张图里盲搜，只要：

1. 在 ``randomY`` 附近取一条横带
2. 算出每列相对周围背景的**暗度**（缺口是半透明压暗的，所以比周围暗）
3. 找「最暗的连续窗口」，窗口宽度 = 拼图块宽，这个窗口的起点就是缺口左边缘

实测在服务端认可 ``xPos=180`` 的那张图上，预测值偏差为 0。
"""
from __future__ import annotations

from typing import Any

import cv2
import numpy as np

from utils.logs.logger_util import get_logger

log = get_logger(__name__)


def detect_gap_x(
    back_image: np.ndarray,
    random_y: int,
    slider_width: int = 60,
    band_ratio: float = 0.3,
    baseline_window: int = 51,
) -> tuple[int, float]:
    """找出缺口左边缘 X。

    Args:
        back_image: 背景图（BGR）
        random_y: 缺口顶边纵坐标（服务端下发）
        slider_width: 拼图块宽
        band_ratio: 取样横带的高度比例（相对块高，取块高中部）
        baseline_window: 低频基线窗口（越大越关注"局部"差异）

    Returns:
        ``(x, confidence)``，confidence 越大越可信（0~1）
    """
    if back_image is None or back_image.size == 0:
        raise ValueError("背景图为空")

    gray = cv2.cvtColor(back_image, cv2.COLOR_BGR2GRAY).astype(np.float64)
    h, w = gray.shape[:2]

    # 取缺口横带：块高中部一段，避开上下边框
    block_h = int(slider_width)  # 正方形块
    band_h = max(3, int(block_h * band_ratio))
    y0 = int(random_y) + (block_h - band_h) // 2
    y0 = max(0, min(y0, h - band_h))
    band = gray[y0: y0 + band_h, :]

    row = band.mean(axis=0)

    # 低频基线：用大窗口模糊，代表"这一带的正常亮度"
    win = max(11, int(baseline_window))
    if win % 2 == 0:
        win += 1
    baseline = cv2.blur(row.reshape(1, -1), (win, 1)).ravel()
    diff = row - baseline  # 负值 = 比周围暗

    # 在所有起点上滑动一个块宽的窗口，取平均暗度最大的（即最暗）
    win_w = max(4, int(slider_width))
    if win_w >= w:
        return 0, 0.0
    scores = np.array(
        [diff[i: i + win_w].mean() for i in range(0, w - win_w + 1)]
    )
    best = int(np.argmin(scores))
    best_score = float(scores[best])

    # 置信度：最暗窗口比次优窗口暗多少（差距越大越可信）
    order = np.argsort(scores)
    second = float(scores[order[1]]) if len(order) > 1 else 0.0
    gap = second - best_score  # 正数
    confidence = float(min(1.0, max(0.0, gap / 12.0)))

    log.info(
        "缺口检测：x=%s score=%.2f 次优=%.2f 置信=%.2f",
        best, best_score, second, confidence,
    )
    return best, confidence


def detect_gap_x_multi(
    back_image: np.ndarray,
    random_y: int,
    slider_width: int = 60,
    **kwargs: Any,
) -> int:
    """多横带投票版：对不同高度的横带各算一次，取中位数（抗单带干扰）。"""
    block_h = int(slider_width)
    offsets = [0.30, 0.40, 0.50, 0.60, 0.70]
    xs: list[int] = []
    for r in offsets:
        band_h = max(3, int(block_h * 0.16))
        y0 = int(random_y) + int(block_h * r) - band_h // 2
        h = back_image.shape[0]
        y0 = max(0, min(y0, h - band_h))
        gray = cv2.cvtColor(back_image, cv2.COLOR_BGR2GRAY).astype(np.float64)
        band = gray[y0: y0 + band_h, :]
        row = band.mean(axis=0)
        win = 51
        baseline = cv2.blur(row.reshape(1, -1), (win, 1)).ravel()
        diff = row - baseline
        w = back_image.shape[1]
        win_w = max(4, int(slider_width))
        if win_w >= w:
            continue
        scores = np.array([diff[i: i + win_w].mean() for i in range(0, w - win_w + 1)])
        xs.append(int(np.argmin(scores)))

    if not xs:
        return detect_gap_x(back_image, random_y, slider_width)[0]
    xs.sort()
    median = xs[len(xs) // 2]
    log.info("多带投票：xs=%s -> 取中位数 %s", xs, median)
    return median


def detect_gap_x_multi_votes(
    back_image: np.ndarray,
    random_y: int,
    slider_width: int = 60,
    **kwargs: Any,
) -> tuple[int, list[int]]:
    """和 :func:`detect_gap_x_multi` 一样，但同时返回每一横带的投票明细。

    明细很有用 —— 5 条横带里如果 4 条都指向同一个位置，
    那这个位置的可信度就比"中位数"更能说明问题。
    """
    block_h = int(slider_width)
    offsets = [0.30, 0.40, 0.50, 0.60, 0.70]
    xs: list[int] = []
    for r in offsets:
        band_h = max(3, int(block_h * 0.16))
        y0 = int(random_y) + int(block_h * r) - band_h // 2
        h = back_image.shape[0]
        y0 = max(0, min(y0, h - band_h))
        gray = cv2.cvtColor(back_image, cv2.COLOR_BGR2GRAY).astype(np.float64)
        band = gray[y0: y0 + band_h, :]
        row = band.mean(axis=0)
        win = 51
        baseline = cv2.blur(row.reshape(1, -1), (win, 1)).ravel()
        diff = row - baseline
        w = back_image.shape[1]
        win_w = max(4, int(slider_width))
        if win_w >= w:
            continue
        scores = np.array([diff[i: i + win_w].mean() for i in range(0, w - win_w + 1)])
        xs.append(int(np.argmin(scores)))

    if not xs:
        return detect_gap_x(back_image, random_y, slider_width)[0], []

    xs.sort()
    return xs[len(xs) // 2], xs
