"""工具入参的类型归一。

``ToolRegistry.call`` 会先按 ``params_schema`` 校验一遍（见 ``registry``
里的 ``_normalize_arguments``）：类型完全对不上的直接报参数错。但
handler 也会被其他代码直接调用（单测、智能体内部流程），所以这一层
仍然兜底：分页 / 过滤参数写错，不该让一个只读工具整个报
``ValueError`` / ``TypeError`` —— 那等于把「查询」变成「工具不可用」。

语义与 ``audit_log._as_int`` 保持一致：``None`` / ``""`` / ``0`` / 坏值
都算"没给"，退回 ``default``；需要范围时再夹到 ``[low, high]``。
"""

from __future__ import annotations

from typing import Any


def safe_int(value: Any, default: int = 0) -> int:
    """把客户端传来的值转成 int；转不了就用 ``default``。"""
    if value is None or value == "" or value == 0:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def clamp_int(value: Any, default: int, low: int, high: int) -> int:
    """``max(low, min(int(value or default), high))`` 的容错版。"""
    return max(low, min(safe_int(value, default), high))
