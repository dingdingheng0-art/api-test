"""拼图滑块缺口检测。

服务端返回的字段（参考抓包）::

    backImage      300 x 150 背景图（含 1 个缺口）
    slidingImage   60 x 60 滑块拼图（缺口的"拼图块"），**带 alpha 通道**
    randomY        缺口纵坐标（服务端已给，前端只需算 X）
    originalWidth  300
    originalHeight 150
    sliderWidth    60  # 拼图块宽
    sliderHeight   60  # 拼图块高

**为什么不能用模板匹配**（这是踩过的坑）：
``slidingImage`` 是**从背景图上挖下来的那一块**，它已经不含背景内容了。
拿它当模板去 ``matchTemplate`` 背景图，匹配的是"被挖走后的空洞纹理"，
天然对不上 —— 实测匹配分只有 0.2~0.5，位置也是随机的。

**正确的做法**：``slidingImage`` 的 alpha 通道就是拼图块的精确形状，
把这个形状贴到背景图上，找"内部最暗 + 轮廓最吻合"的位置即可。

端到端实测（真实浏览器，服务端判定）一次命中率 7/8。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np

from utils.slider.image_util import (
    new_debug_filename,
    save_debug,
)
from utils.logs.logger_util import get_logger

log = get_logger(__name__)


@dataclass
class SliderInitResult:
    """滑块初始化响应（含图 + 元信息）。"""
    key: str
    back_image_b64: str
    sliding_image_b64: str
    original_width: int
    original_height: int
    slider_width: int
    slider_height: int
    random_y: int
    effective_time: int
    raw: dict[str, Any]


def parse_init_response(result: dict[str, Any]) -> SliderInitResult:
    """把 ``/slider/LOGIN?_t=...`` 返回的 ``result`` dict 解析成强类型。"""
    required = (
        "key", "backImage", "slidingImage",
        "originalWidth", "originalHeight",
        "sliderWidth", "sliderHeight",
    )
    missing = [k for k in required if k not in result]
    if missing:
        raise ValueError(f"滑块初始化响应缺字段: {missing}")

    return SliderInitResult(
        key=result["key"],
        back_image_b64=result["backImage"],
        sliding_image_b64=result["slidingImage"],
        original_width=int(result["originalWidth"]),
        original_height=int(result["originalHeight"]),
        slider_width=int(result["sliderWidth"]),
        slider_height=int(result["sliderHeight"]),
        random_y=int(result.get("randomY", 0)),
        effective_time=int(result.get("effectiveTime", 300)),
        raw=result,
    )


# ----------------------------------------------------------------------
# 主入口
# ----------------------------------------------------------------------

def solve_slider_x(
    init: SliderInitResult,
    debug_dir: str | None = None,
    save_debug_flag: bool = True,
) -> int:
    """在 backImage 里找缺口的 xPos（拼图块整张图片的 left）。

    主力走**形状掩码匹配**（见模块说明为什么模板匹配行不通），
    形状法置信度低时会用**暗度法**交叉校验。

    Args:
        init: 滑块初始化解析后的结果
        debug_dir: 调试图目录；非 None 时落图
        save_debug_flag: 是否写调试图

    Returns:
        xPos（0 ~ originalWidth - 43，与前端一致）
    """
    t0 = time.perf_counter()

    back = _decode_png(init.back_image_b64)
    piece = _decode_png(init.sliding_image_b64)
    if back.ndim == 3 and back.shape[2] == 4:
        back = back[:, :, :3]

    from utils.slider.gap_shape import detect_gap_x_by_shape
    from utils.slider.gap_shape_v2 import detect_gap_x_v2

    # 主力：v2（归一化暗度 + 显著性置信度）
    x, conf = detect_gap_x_v2(
        back, piece, init.random_y, init.slider_width,
    )

    # 交叉校验：形状法 v1 + 暗度法
    #
    # 阈值 0.9：置信度不够高就说明 v2 自己也拿不准，必须让另外两路来投票。
    # （实测 0.6 那次就错了 —— 阈值放得太松，等于没校验）
    if conf < 0.9:
        from utils.slider.gap_detect import detect_gap_x_multi_votes

        try:
            x_v1, conf_v1 = detect_gap_x_by_shape(
                back, piece, init.random_y, init.slider_width
            )
            dark_x, dark_votes = detect_gap_x_multi_votes(
                back, init.random_y, slider_width=init.slider_width
            )
        except Exception as exc:
            log.warning("交叉校验失败：%s", exc)
        else:
            log.info("三路结果：v2=%s(%.2f) v1=%s(%.2f) dark=%s (投票=%s)",
                     x, conf, x_v1, conf_v1, dark_x, dark_votes)

            # 决策原则：**多数票优先**，置信度只在票数相同时才用来决胜。
            #
            # 三路各说各话时（两两相差都超过 8 像素），光看置信度会选错 ——
            # 实战里出现过 v2=157(0.57) / v1=156(1.00) / dark=98 的情况，
            # v1 置信度最高但它错了，而 dark 的多带投票里有 4/5 票都在 98，
            # 少数服从多数才是对的。
            candidates = {"v2": x, "v1": x_v1, "dark": dark_x}

            # 找"互相接近"的簇（差值 <= 8 视为同一簇）
            clusters: list[list[str]] = []
            for name, value in candidates.items():
                placed = False
                for cluster in clusters:
                    if any(abs(value - candidates[other]) <= 8 for other in cluster):
                        cluster.append(name)
                        placed = True
                        break
                if not placed:
                    clusters.append([name])

            # 票数最多的簇获胜
            best = max(clusters, key=len)
            if len(best) > 1:
                log.info("多数票：%s 一致，取 %s", best, candidates[best[0]])
                x = candidates[best[0]]
            elif dark_votes:
                # 三路完全分歧，但暗度法的横带投票能给出真正的多数
                # （比如 5 票里有 4 票指向 98，就该信 98）
                from collections import Counter

                counter = Counter(dark_votes)
                top_count = counter.most_common(1)[0][1]
                if top_count >= 3:
                    top_x = counter.most_common(1)[0][0]
                    log.info("三路分歧，但暗度法横带投票 %s 里 %s 票指向 %s，取之",
                             dark_votes, top_count, top_x)
                    x = top_x
                else:
                    log.warning("三路完全分歧，按置信度取舍：v2=%.2f v1=%.2f", conf, conf_v1)
                    x = x if conf >= conf_v1 else x_v1
            else:
                # 三路各说各话：取置信度更高的那个（v2 显著性 vs v1 匹配分）
                log.warning("三路分歧，按置信度取舍：v2=%.2f v1=%.2f", conf, conf_v1)
                x = x if conf >= conf_v1 else x_v1

    max_x = init.original_width - 43
    x = int(np.clip(x, 0, max_x))

    log.info("缺口 xPos=%s (置信 %.2f) 耗时=%.1fms", x, conf,
             (time.perf_counter() - t0) * 1000)

    if debug_dir and save_debug_flag:
        _save_debug_images(back, piece, x=x, debug_dir=debug_dir)

    return x


def _decode_png(data_url: str) -> np.ndarray:
    """data URL / base64 -> numpy 图（保留 alpha 通道）。"""
    import base64

    if data_url.startswith("data:"):
        data_url = data_url.split(",", 1)[1]
    raw = base64.b64decode(data_url)
    return cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_UNCHANGED)


def _save_debug_images(
    bg: np.ndarray,
    slider: np.ndarray,
    x: int,
    debug_dir: str,
) -> None:
    """落调试图：原始背景、拼图块、背景上标注缺口位置。"""
    try:
        p_bg = new_debug_filename("slider", debug_dir)
        save_debug(bg, p_bg)

        p_sl = new_debug_filename("slider_piece", debug_dir)
        save_debug(slider, p_sl)

        h, w = bg.shape[:2]
        marker = bg.copy()
        cv2.rectangle(
            marker,
            (int(x), 0),
            (int(x) + 60, int(slider.shape[0])),
            color=(0, 255, 0),
            thickness=2,
        )
        p_marked = new_debug_filename("slider_marked", debug_dir)
        save_debug(marker, p_marked)

        log.info("调试图：\n  %s\n  %s\n  %s", p_bg, p_sl, p_marked)
    except Exception as exc:  # 落盘失败不影响主流程
        log.warning("保存调试图失败：exc=%s", exc)
