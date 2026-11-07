"""拼图验证码的公共工具：形状掩码提取 + 包围盒计算。

这三个函数在 ``gap_shape`` / ``gap_shape_v2`` / ``gap_fused`` 里各抄了一份，
改一处忘另一处很容易出错，所以统一放这里。
"""
from __future__ import annotations

import cv2
import numpy as np


def alpha_mask(piece: np.ndarray) -> np.ndarray | None:
    """从拼图块图抠出形状掩码。

    优先用 alpha 通道（服务端给的 slidingImage 自带透明通道，形状最准）；
    没有 alpha 就退回按亮度阈值 + 取最大连通域。
    """
    if piece is None or piece.size == 0:
        return None
    if piece.ndim == 3 and piece.shape[2] == 4:
        mask = (piece[:, :, 3] > 128).astype(np.uint8)
        return mask if mask.sum() else None
    if piece.ndim == 3:
        gray = cv2.cvtColor(piece[:, :, :3], cv2.COLOR_BGR2GRAY)
    else:
        gray = piece
    mask = (gray < 245).astype(np.uint8)
    if mask.sum() == 0:
        return None
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    if n > 1:
        biggest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        mask = (labels == biggest).astype(np.uint8)
    return mask


def bbox(mask: np.ndarray) -> tuple[int, int, int, int]:
    """掩码的包围盒，返回 ``(x0, y0, x1, y1)``（右/下边界为开区间）。"""
    ys, xs = np.nonzero(mask)
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def piece_offset(piece: np.ndarray) -> int:
    """拼图块形状在整张 slidingImage 里的左偏移 ``mask_x0``。

    服务端要的 ``xPos`` 是**整张拼图块图的 left**，不是形状的左边缘，
    两者正好差这个偏移。
    """
    mask = alpha_mask(piece)
    if mask is None:
        return 0
    x0, _y0, _x1, _y1 = bbox(mask)
    return x0
