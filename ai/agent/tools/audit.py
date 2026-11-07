"""审计与成本工具（只读）。

**这两个工具回答的问题不一样**

``get_agent_audit`` —— 「刚才那轮到底调了什么？」逐条调用链，排障用。
``get_agent_cost``  —— 「这些调用花在哪了？」按工具聚合，看趋势用。

**为什么成本要分档而不是算钱**

各家模型、各条链路的单价天天变，把价格写死在代码里，第二天就是错的。
这里只按 ``ToolSpec.cost`` 的三档（``free`` / ``network`` / ``llm``）
统计**调用次数**和**耗时** —— 这两项是框架自己能准确知道的。
真要折算成钱，拿次数乘单价，在外部做。

**写类调用单独计数**

``write_calls`` 不是"成本"指标，是**审计**指标：改了多少次用例文件。
这个数字不该悄无声息地涨。
"""

from __future__ import annotations

from typing import Any

from ai.agent.tools.registry import ToolError, ToolSpec, ok

#: 逐条返回时单条参数最多多长（再长就是整份断言列表了）
_ARG_PREVIEW = 160


def get_agent_audit(
    limit: int = 50,
    tool: str = "",
    only_failed: bool = False,
    origin: str = "",
    run: str = "",
) -> dict[str, Any]:
    """读最近的工具调用记录（时间正序）。"""
    from ai.agent import audit_log

    limit = max(1, min(int(limit or 50), 500))
    entries = audit_log.recent(
        limit=limit, tool=tool, only_failed=only_failed, origin=origin, run=run
    )

    items = [
        {
            "ts": entry.get("ts"),
            "origin": entry.get("origin"),
            "run": entry.get("run") or "",
            "tool": entry.get("tool"),
            "side_effect": entry.get("side_effect") or "",
            "cost": entry.get("cost") or "free",
            "ok": bool(entry.get("ok")),
            "duration_ms": entry.get("duration_ms"),
            "args": _preview(entry.get("args") or {}),
            **({"error": entry.get("error")} if entry.get("error") else {}),
        }
        for entry in entries
    ]

    if not items:
        return ok(
            total=0,
            calls=[],
            log_file=str(audit_log.log_path()),
            hint=(
                "审计日志还没有记录。MCP 服务每调一次工具就会写一条；"
                "如果刚重启过服务，之前的调用在旧进程的内存里，没落盘。"
            ),
        )

    return ok(
        total=len(items),
        calls=items,
        log_file=str(audit_log.log_path()),
        hint="时间正序。看某一轮的完整链路时先按 run 或 origin 筛。",
    )


def get_agent_cost(
    limit: int = 500,
    since_minutes: int = 0,
    origin: str = "",
    run: str = "",
) -> dict[str, Any]:
    """按工具聚合调用次数 / 失败数 / 耗时 / 成本档。"""
    from ai.agent import audit_log

    limit = max(1, min(int(limit or 500), 2000))
    since_minutes = max(0, int(since_minutes or 0))

    summary = audit_log.summarize(
        limit=limit, origin=origin, since_minutes=since_minutes, run=run
    )

    if not summary["window"]:
        return ok(
            **summary,
            log_file=str(audit_log.log_path()),
            hint="这个窗口里没有调用记录。放宽 since_minutes 或去掉 origin/run 筛选。",
        )

    return ok(
        **summary,
        log_file=str(audit_log.log_path()),
        hint=(
            "by_cost 的三档：free=纯本地计算，network=发请求或跑用例，"
            "llm=会调模型。按次数乘单价就能折算成本；"
            "write_calls 是审计指标 —— 改了多少次用例文件，不该悄悄涨。"
        ),
    )


def _preview(args: dict[str, Any]) -> dict[str, Any]:
    """参数摘要（审计日志里已经脱敏并截断了，这里只再压一下）。"""
    out: dict[str, Any] = {}
    for key, value in args.items():
        text = value if isinstance(value, (int, float, bool)) or value is None else str(value)
        if isinstance(text, str) and len(text) > _ARG_PREVIEW:
            text = text[:_ARG_PREVIEW] + "…"
        out[key] = text
    return out


TOOLS = [
    ToolSpec(
        name="get_agent_audit",
        description=(
            "读智能体最近的工具调用记录（时间正序）：调了什么、带什么参数、"
            "成功没、花了多久。参数里的 token/密码已抹成 ***。"
            "排障时用：想确认「刚才是不是真的跑了」「这条用例改过几次」。"
        ),
        params_schema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "最多返回多少条（默认 50）", "default": 50},
                "tool": {"type": "string", "description": "只看某个工具，如 run_cases", "default": ""},
                "only_failed": {"type": "boolean", "description": "只看失败的调用", "default": False},
                "origin": {
                    "type": "string",
                    "description": "调用来源：agent（MCP 服务）/ eval（评测）/ test",
                    "default": "",
                },
                "run": {"type": "string", "description": "只看某次评测任务的调用", "default": ""},
            },
            "additionalProperties": False,
        },
        handler=get_agent_audit,
        side_effect="read",
        tags=("audit",),
    ),
    ToolSpec(
        name="get_agent_cost",
        description=(
            "按工具聚合最近的调用：次数、失败数、总耗时、成本档（free/network/llm），"
            "以及写类调用（write_calls）和执行类调用（execute_calls）的次数。"
            "用于看「智能体把时间花在哪」「有没有在反复调同一个工具」。"
        ),
        params_schema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "统计最近多少条（默认 500）", "default": 500},
                "since_minutes": {
                    "type": "integer",
                    "description": "只看最近多少分钟（0 = 不限）",
                    "default": 0,
                },
                "origin": {"type": "string", "description": "调用来源：agent / eval / test", "default": ""},
                "run": {"type": "string", "description": "只看某次评测任务", "default": ""},
            },
            "additionalProperties": False,
        },
        handler=get_agent_cost,
        side_effect="read",
        tags=("audit",),
    ),
]
