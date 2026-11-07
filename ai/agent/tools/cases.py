"""用例查询工具（只读）。

智能体第一件要做的事永远是「先看清楚有什么」：

- 这个项目有哪些模块、多少条用例
- 某条用例具体测什么、断言了什么
- 哪些用例是 draft（不跑）、哪些是 broken（已知有问题）

这三个工具就是它的眼睛。
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from ai.agent.tools._common import (
    case_detail,
    find_case,
    find_case_by_number,
    iter_cases,
)
from ai.agent.tools.registry import ToolError, ToolSpec, ok

MAX_CASES = 500


def list_cases(
    project: str = "",
    module: str = "",
    mark: str = "",
    keyword: str = "",
    status: str = "",
    limit: int = 50,
) -> dict[str, Any]:
    """列出用例（摘要，不含完整步骤）。"""
    limit = max(1, min(int(limit or 50), MAX_CASES))

    pairs = iter_cases(
        project=project, module=module, mark=mark, keyword=keyword, status=status
    )
    if not pairs:
        return ok(
            total=0,
            returned=0,
            cases=[],
            hint="没有匹配的用例。检查 project / module 名字，或先调 list_modules 看有哪些模块。",
        )

    refs = [ref for ref, _ in pairs]
    shown = refs[:limit]

    # 按项目/模块分组统计 —— 智能体不用自己数
    by_module = Counter(f"{r.project}/{r.module}" for r in refs)
    by_status = Counter(r.status for r in refs)

    return ok(
        total=len(refs),
        returned=len(shown),
        truncated=len(refs) > limit,
        by_module=dict(by_module.most_common()),
        by_status=dict(by_status),
        cases=[r.to_dict() for r in shown],
    )


def get_case(case_id: str) -> dict[str, Any]:
    """按 ID 拿一条用例的完整结构。

    ``case_id`` 既接受工具内部的 ``case_xxx`` 哈希，也接受 ``API-168``
    这种用例编号 —— 模型从 ``list_cases`` 里抄哪种都能用，少一个踩坑点。
    """
    if not case_id:
        raise ToolError("缺少 case_id", hint="先调 list_cases 拿到 ID")

    found = find_case(case_id)
    if found is None:
        # 可能是人读编号，按 case_id 再找一遍
        found = find_case_by_number(case_id)
    if found is None:
        raise ToolError(
            f"找不到用例 {case_id!r}",
            hint="ID 可能过期（用例被改名/删除了）。重新调 list_cases。",
        )

    ref, case = found
    return ok(case=case_detail(ref, case))


def list_modules(project: str = "") -> dict[str, Any]:
    """列出项目/模块及其用例数量。"""
    from utils import paths

    pairs = iter_cases(project=project)
    counter: Counter[str] = Counter()
    for ref, _ in pairs:
        counter[f"{ref.project}/{ref.module}"] += 1

    projects: dict[str, list[dict[str, Any]]] = {}
    for key, count in sorted(counter.items()):
        proj, mod = key.split("/", 1)
        projects.setdefault(proj, []).append({"module": mod, "cases": count})

    if not projects:
        return ok(
            projects=[],
            hint=f"没有扫到用例。检查用例来源配置（当前目录 {paths.CASES_ROOT.name}）和 --case-source 开关。",
        )

    return ok(
        projects=[
            {"project": proj, "modules": mods, "total": sum(m["cases"] for m in mods)}
            for proj, mods in projects.items()
        ],
        total_cases=len(pairs),
    )


TOOLS = [
    ToolSpec(
        name="list_cases",
        description=(
            "列出接口测试用例（摘要）。支持按项目、模块、标签(mark)、关键字、状态筛选。"
            "返回每条用例的 id / 名称 / 状态 / 步骤数 / 断言数，以及按模块和状态的统计。"
            "要拿完整步骤内容请用 get_case。"
        ),
        params_schema={
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "项目名，如 mall4j。空=全部"},
                "module": {"type": "string", "description": "模块名，如 prod_prod。空=全部"},
                "mark": {
                    "type": "string",
                    "description": "标签筛选，多个用逗号分隔（AND 语义），如 smoke,p0",
                },
                "keyword": {"type": "string", "description": "关键字，匹配用例名/描述/路径"},
                "status": {
                    "type": "string",
                    "description": "用例状态：draft / debugged / disabled / broken",
                },
                "limit": {
                    "type": "integer",
                    "description": "最多返回多少条（默认 50，上限 500）",
                    "default": 50,
                },
            },
            "additionalProperties": False,
        },
        handler=list_cases,
        side_effect="read",
        tags=("case",),
    ),
    ToolSpec(
        name="get_case",
        description=(
            "按用例 ID 获取完整结构：所有步骤的 method/path/断言类型、setups/teardowns 数量、"
            "依赖关系、参数化情况。修改或诊断某条用例前先用它看清楚。"
        ),
        params_schema={
            "type": "object",
            "properties": {
                "case_id": {
                    "type": "string",
                    "description": "list_cases 返回的 id（case_xxx）；也接受 API-168 这种用例编号",
                },
            },
            "required": ["case_id"],
            "additionalProperties": False,
        },
        handler=get_case,
        side_effect="read",
        tags=("case",),
    ),
    ToolSpec(
        name="list_modules",
        description="列出所有项目和模块，以及每个模块的用例数量。用于了解项目全貌、发现没测的模块。",
        params_schema={
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "只看某个项目，空=全部"},
            },
            "additionalProperties": False,
        },
        handler=list_modules,
        side_effect="read",
        tags=("case",),
    ),
]
