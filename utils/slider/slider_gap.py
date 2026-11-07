"""缺口定位策略层。

**结论（端到端实测，一次命中率）**：
- 形状法 v2（拿拼图块 alpha 掩码去背景图上找）：8/8 ✅ 主力
- 形状法 v1（早期版本）：4/5 —— 保留作第二路交叉校验
- 暗度法（在 randomY 横带找最暗段）：单独用不稳 —— 交叉校验用
- ddddocr（模板匹配路线）：0/4 ❌ 已淘汰 —— 拼图块是从背景挖下来的，
  本身不含背景纹理，模板匹配匹配到的是"空洞纹理"，位置基本随机

所以对外只暴露一个 ``ShapeGapFinder``：走 v2，置信度低时用暗度法兜底。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class SliderChallenge:
    """浏览器里从服务端拿到的滑块题目。"""

    back_image_b64: str
    sliding_image_b64: str
    random_y: int
    original_width: int
    original_height: int
    slider_width: int
    slider_height: int

    @property
    def max_x(self) -> int:
        """前端允许的最大 xPos（originalWidth - 43，与前端代码一致）。"""
        return self.original_width - 43


class GapFinder(Protocol):
    """缺口定位策略接口。"""

    name: str

    def locate(self, challenge: SliderChallenge) -> int:
        """返回缺口对应的 xPos（拼图块整张图片的 left）。"""
        ...


class ShapeGapFinder:
    """主力策略：定位拼图缺口。

    直接复用 ``utils.slider_solver.solve_slider_x``（三路投票 + 置信度取舍），
    保证浏览器路径和接口路径的识别结果完全一致。

    算法细节见 ``utils/gap_shape_v2.py``（归一化暗度优势 + 轮廓边缘吻合）。
    """

    name = "shape"

    def __init__(self, min_confidence: float = 0.6) -> None:
        self.min_confidence = min_confidence

    def locate(self, challenge: SliderChallenge) -> int:
        from utils.slider.slider_solver import SliderInitResult, solve_slider_x

        init = SliderInitResult(
            key="",
            back_image_b64=challenge.back_image_b64,
            sliding_image_b64=challenge.sliding_image_b64,
            original_width=challenge.original_width,
            original_height=challenge.original_height,
            slider_width=challenge.slider_width,
            slider_height=challenge.slider_height,
            random_y=challenge.random_y,
            effective_time=0,
            raw={},
        )
        return solve_slider_x(init, debug_dir=None, save_debug_flag=False)


def get_gap_finder(name: str = "shape") -> GapFinder:
    """按名字取定位策略。

    目前只有 ``shape`` 一种 —— 其他路线实测都不行，已淘汰。
    """
    if name != "shape":
        raise ValueError(f"未知策略 {name!r}，目前只有 'shape'")
    return ShapeGapFinder()
