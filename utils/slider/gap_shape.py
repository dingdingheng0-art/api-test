"""基于**缺口形状掩码**的定位算法。

**关键发现**：服务端下发的 ``slidingImage`` 带 alpha 通道，不透明区域就是
拼图块的精确形状（jigsaw 拼图块轮廓）。所以缺口在背景图上的位置，
可以通过"把这个形状贴上去，看哪里最吻合"来定位。

**打分思路**：缺口是「背景被挖走 + 压暗」形成的，所以在形状内部应该看到
**比周围更暗**的内容；同时形状轮廓处应有边缘响应。两个信号一起用：

    score = 内部暗度 + 边缘吻合度

形状左边缘与 ``xPos`` 的关系：服务端 ``xPos`` 是**拼图块整张 60x60 图片
的 left**，形状在图内有偏移 ``mask_x0``，所以
``形状左边缘 = xPos + mask_x0``，反之 ``xPos = 形状左边缘 - mask_x0``。

实测（端到端，一次命中率）：
- 形状法：4/5
- 暗度融合法：1/4
- ddddocr：0/4
"""
from __future__ import annotations

import cv2
import numpy as np

from utils.slider.gap_common import alpha_mask as _alpha_mask, bbox as _bbox
from utils.logs.logger_util import get_logger

log = get_logger(__name__)


def detect_gap_x_by_shape(
    back_image: np.ndarray,
    piece_image: np.ndarray,
    random_y: int,
    slider_width: int = 60,
    canvas_width: int = 300,
    dark_weight: float = 1.0,
    edge_weight: float = 4.0,
) -> tuple[int, float]:
    """用形状掩码定位缺口，返回 ``(xPos, confidence)``。

    ``xPos`` 是拼图块整张图片的 left（与服务端一致）。
    """
    mask = _alpha_mask(piece_image)
    if mask is None:
        raise ValueError("拼图块图里抠不出形状掩码")

    mx0, my0, mx1, my1 = _bbox(mask)
    shape = mask[my0:my1, mx0:mx1]
    sh, sw = shape.shape
    if sh == 0 or sw == 0:
        raise ValueError("形状掩码为空")

    # 形状在背景图上的纵向位置：randomY 对应拼图块图片里形状的顶边
    y_top = int(random_y) + my0
    h, w = back_image.shape[:2]
    if y_top < 0 or y_top + sh > h:
        log.warning("形状纵向超出背景图：y_top=%s shape_h=%s h=%s", y_top, sh, h)
        y_top = max(0, min(y_top, h - sh))

    inner = shape > 0
    # 形状外一圈（用作"周围背景"参照）
    ring = cv2.dilate(shape, np.ones((11, 11), np.uint8), iterations=1)
    outer = (ring > 0) & (~inner)

    # 形状轮廓（边缘吻合用）
    contours, _ = cv2.findContours(shape, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    shape_edge = np.zeros((sh, sw), np.uint8)
    cv2.drawContours(shape_edge, contours, -1, 255, 1)
    shape_edge_f = shape_edge.astype(np.float64) / 255.0
    edge_denom = max(1.0, shape_edge_f.sum())

    gray = cv2.cvtColor(back_image, cv2.COLOR_BGR2GRAY).astype(np.float64)

    max_start = min(w - sw, canvas_width - sw)
    scores: list[tuple[int, float]] = []

    for x in range(0, max_start + 1):
        sx = x + mx0
        if sx < 0 or sx + sw > w:
            continue
        patch = gray[y_top: y_top + sh, sx: sx + sw]
        if patch.shape[:2] != (sh, sw):
            continue

        # ① 内部暗度：形状内应比形状外更暗
        inner_vals = patch[inner]
        outer_vals = patch[outer]
        if inner_vals.size == 0 or outer_vals.size == 0:
            continue
        darkness = float(outer_vals.mean() - inner_vals.mean())

        # ② 边缘吻合：背景边缘图在形状轮廓处应有响应
        bg_edge = cv2.Canny(
            back_image[y_top: y_top + sh, sx: sx + sw].astype(np.uint8), 50, 150
        ).astype(np.float64) / 255.0
        edge_fit = float((bg_edge * shape_edge_f).sum() / edge_denom)

        score = darkness * dark_weight + edge_fit * edge_weight
        scores.append((x, score))

    if not scores:
        raise RuntimeError("没找到候选位置")

    scores.sort(key=lambda t: -t[1])
    best_x, best_score = scores[0]
    # 次优（排除 ±12 邻域，避免同一个缺口被重复计数）
    others = [sc for sx, sc in scores if abs(sx - best_x) > 12]
    second = max(others) if others else best_score
    confidence = float(min(1.0, max(0.0, (best_score - second) / 3.0)))

    log.info(
        "形状匹配：xPos=%s score=%.3f 次优=%.3f 置信=%.2f（形状 %sx%s，mask_x0=%s）",
        best_x, best_score, second, confidence, sw, sh, mx0,
    )
    return best_x, confidence
