"""改进版形状定位：加入"显著性"判据 + 暗度加权。

**要解决的问题**（实测）：
单纯取"分数最高的位置"会在背景纹理上误判 —— 最优分 89.8、次优 72.8，
两者都很高，说明背景里还有一处很像，但两处都不是真缺口。

**改法**：
1. 打分改成归一化的"暗度优势"：形状内部比形状外暗多少（除以局部对比度），
   这样不同亮度背景下可比。
2. 判"真缺口"要求**同时**满足：
   - 暗度优势为正（形状内确实更暗）
   - 边缘吻合度高（轮廓对得上）
3. 用"最优分相对全场中位数的显著程度"算置信度，而不是只看最优 vs 次优。
"""
from __future__ import annotations

import cv2
import numpy as np

from utils.slider.gap_common import alpha_mask as _alpha_mask, bbox as _bbox
from utils.logs.logger_util import get_logger

log = get_logger(__name__)


def detect_gap_x_v2(
    back_image: np.ndarray,
    piece_image: np.ndarray,
    random_y: int,
    slider_width: int = 60,
    canvas_width: int = 300,
    dark_weight: float = 1.0,
    edge_weight: float = 3.0,
) -> tuple[int, float]:
    """改进版形状定位，返回 ``(xPos, confidence)``。

    打分 = 暗度优势 * dark_weight + 边缘吻合 * edge_weight

    暗度优势 = (形状外均值 - 形状内均值) / 局部对比度
    局部对比度 = 形状外圈的标准差（衡量这块背景本身的明暗起伏）
    归一化之后，不同亮度/纹理的背景可以横向比较。
    """
    mask = _alpha_mask(piece_image)
    if mask is None:
        raise ValueError("拼图块图里抠不出形状掩码")

    mx0, my0, mx1, my1 = _bbox(mask)
    shape = mask[my0:my1, mx0:mx1]
    sh, sw = shape.shape
    if sh == 0 or sw == 0:
        raise ValueError("形状掩码为空")

    y_top = int(random_y) + my0
    h, w = back_image.shape[:2]
    if y_top < 0 or y_top + sh > h:
        y_top = max(0, min(y_top, h - sh))

    inner = shape > 0
    ring = cv2.dilate(shape, np.ones((13, 13), np.uint8), iterations=1)
    outer = (ring > 0) & (~inner)

    contours, _ = cv2.findContours(shape, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    shape_edge = np.zeros((sh, sw), np.uint8)
    cv2.drawContours(shape_edge, contours, -1, 255, 1)
    shape_edge_f = shape_edge.astype(np.float64) / 255.0
    edge_denom = max(1.0, shape_edge_f.sum())

    gray = cv2.cvtColor(back_image, cv2.COLOR_BGR2GRAY).astype(np.float64)

    max_start = min(w - sw, canvas_width - sw)
    rows: list[tuple[int, float, float, float]] = []  # x, score, darkness, edge

    for x in range(0, max_start + 1):
        sx = x + mx0
        if sx < 0 or sx + sw > w:
            continue
        patch = gray[y_top: y_top + sh, sx: sx + sw]
        if patch.shape[:2] != (sh, sw):
            continue

        inner_vals = patch[inner]
        outer_vals = patch[outer]
        if inner_vals.size == 0 or outer_vals.size == 0:
            continue

        raw_dark = float(outer_vals.mean() - inner_vals.mean())
        # 局部对比度：外面一圈的标准差。背景平坦时对比度小，优势更显著
        contrast = float(outer_vals.std()) + 1.0
        darkness = raw_dark / contrast

        bg_edge = cv2.Canny(
            back_image[y_top: y_top + sh, sx: sx + sw].astype(np.uint8), 50, 150
        ).astype(np.float64) / 255.0
        edge_fit = float((bg_edge * shape_edge_f).sum() / edge_denom)

        score = darkness * dark_weight + edge_fit * edge_weight
        rows.append((x, score, darkness, edge_fit))

    if not rows:
        raise RuntimeError("没找到候选位置")

    scores = np.array([r[1] for r in rows])
    order = np.argsort(-scores)
    best_idx = int(order[0])
    best_x = rows[best_idx][0]
    best_score = float(scores[best_idx])

    # 显著性：最优分相对全场中位数的突出程度
    median = float(np.median(scores))
    spread = float(scores.std()) + 1e-6
    significance = (best_score - median) / spread

    # 置信度：显著性 + 暗度优势都为正才算可信
    best_dark = rows[best_idx][2]
    confidence = float(min(1.0, max(0.0, significance / 4.0)))
    if best_dark <= 0:
        confidence *= 0.3

    log.info(
        "形状定位v2：xPos=%s score=%.3f 中位=%.3f 显著=%.2f 暗度=%.2f 置信=%.2f"
        "（形状 %sx%s，mask_x0=%s）",
        best_x, best_score, median, significance, best_dark, confidence,
        sw, sh, mx0,
    )
    return best_x, confidence
