"""工具集合：把框架能力注册成智能体可调用的工具。

**三类工具，按 ``side_effect`` 分**

- ``read`` —— 只读，随便调（查用例、查接口、查表、查图谱）
- ``execute`` —— 会跑用例，可能造/清测试数据（``run_cases`` / ``verify_heal``）
- ``write_case`` —— 会改用例文件（``apply_case_change`` / ``set_case_status`` /
  ``create_case``），写前备份、写后校验、失败回滚

写类工具在 Claude Code 的 ``.claude/settings.json`` 里走 ``ask``，
每次落盘都要人点确认 —— 智能体拿到的是「提交改动」的能力，
不是「随便改文件」的能力。

``heal`` 模块是自愈闭环的入口：``plan_heal``（只读）按归因决定允许的动作，
``verify_heal``（执行）改完跑一遍、不过就回滚。分派规则写成代码而不是提示词，
因为判断错一次就可能把真实缺陷改绿。

新增一个工具只需要三步：

1. 写一个普通 Python 函数，返回 dict，失败抛 ``ToolError``
2. 声明一个 ``ToolSpec``（参数用 JSON Schema）
3. 加进对应模块的 ``TOOLS`` 列表

MCP 服务端会自动发现，不用改别处。
"""

from __future__ import annotations

from ai.agent.tools import (
    audit,
    case_edit,
    cases,
    data,
    diagnostics,
    endpoints,
    execution,
    heal,
    knowledge,
)
from ai.agent.tools.registry import ToolRegistry

_MODULES = (
    cases,
    execution,
    endpoints,
    data,
    diagnostics,
    knowledge,
    case_edit,
    heal,
    audit,
)


def build_registry() -> ToolRegistry:
    """注册所有工具，返回注册表。"""
    registry = ToolRegistry.get()
    for module in _MODULES:
        for spec in module.TOOLS:
            if spec.name in registry.names():
                continue  # 重复构建（单测里常见）时跳过
            registry.register(spec)
    return registry


def get_registry() -> ToolRegistry:
    """拿全局注册表（首次调用时自动注册）。"""
    return build_registry()


def reset_registry() -> ToolRegistry:
    """清空并重建（测试用）。"""
    ToolRegistry._instance = None  # noqa: SLF001 - 测试需要重置单例
    return build_registry()


__all__ = ["build_registry", "get_registry", "reset_registry"]
