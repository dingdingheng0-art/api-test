"""最小智能体循环：让模型自己决定调哪个工具。

**为什么框架里要有这个**

前面几层（工具注册表 / MCP 服务端 / skill / agent）都是给**外部客户端**
用的：Claude Code、Cursor 这些客户端自带「模型 → 调工具 → 回灌结果」
的循环，我们只负责把工具暴露出去。

但 P3 要做评测集，就必须有一个**不依赖任何客户端**的循环：
给定一个任务，跑一遍，看它调了哪些工具、结论对不对、花了多少 token。
没有这个循环，评测就只能人工点，每次结论都不一样。

**它刻意做得很小**

- 不实现流式、不实现并行工具调用（一次一个，简单可复现）
- 不实现对话记忆（每次任务从零开始，评测才有可比性）
- 不实现重试（失败了就是失败，评测要看到真实成功率）

**安全边界没变**

循环只是"自动地调工具"，工具本身的三道闸（服务问题拒绝改断言、
断言只能变强、两轮停止）全在 ``case_io`` / ``heal`` 里。
所以模型在循环里再怎么想改，也绕不过去 —— 这正是把闸做成代码的价值。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from utils.logs.log_manager import log

#: 一轮任务最多让模型想几步（防止死循环烧钱）
DEFAULT_MAX_STEPS = 8

#: 给模型看的系统提示。刻意短 —— 长提示里的规则模型记不住，
#: 真正的约束应该在工具的返回值里（``next_step`` / ``hint``）。
SYSTEM_PROMPT = (
    "你是接口测试智能体。用提供的工具完成任务，不要编造数据。\n"
    "规则：\n"
    "1. 先看清楚再动手：list_modules / list_cases / get_case 是你的眼睛。\n"
    "2. 失败先归因：diagnose_failures 或 plan_heal，按它给的 action 走。\n"
    "3. 只要用户提到要改某条用例的断言，**第一步就是 plan_heal(case_id)** ——\n"
    "   它告诉你这条用例到底能不能改。can_edit_case=false 就不要调 apply_case_change，\n"
    "   工具会拒绝，而且拒绝本身就是结论（该重跑 / 该提缺陷 / 该人工看）。\n"
    "   不要跳过 plan_heal 直接读断言、更不要直接改。\n"
    "4. 工具返回 ok=false 时看 error.hint，换个思路，不要原样重试。\n"
    "5. 用户要求跑「全部/所有」用例时，不要直接开跑：先用 list_modules 数清总量，\n"
    "   把条数和代价（耗时、会造数据）告诉用户，请对方指定模块或确认。\n"
    "   逐个模块拆开跑同样算跑全量，不是缩小范围。\n"
    "6. 完成后用一句话给结论，不要复述工具输出。\n"
)


@dataclass
class Step:
    """循环里的一步（模型的一次工具调用）。

    ``result`` 保留工具的原始返回值 —— 评测要看"它拿到的数据对不对"，
    只看"调没调这个工具"是测不出幻觉的（模型完全可能调了工具、
    然后把数字编错）。序列化时会把大字段裁掉。

    ``skipped``：工具调用次数超过 ``max_tool_calls`` 之后，**不再真的执行**，
    只把"被拒绝"回灌给模型。见 :func:`run_task` 里那段说明。
    """

    index: int
    tool: str
    arguments: dict[str, Any]
    ok: bool = True
    error: str = ""
    duration_ms: float = 0.0
    result: dict[str, Any] = field(default_factory=dict)
    skipped: bool = False

    def to_dict(self, keep_result: bool = True) -> dict[str, Any]:
        data = {
            "index": self.index,
            "tool": self.tool,
            "arguments": self.arguments,
            "ok": self.ok,
            "error": self.error,
            "duration_ms": self.duration_ms,
        }
        if self.skipped:
            data["skipped"] = True
        if keep_result:
            data["result"] = _trim(self.result)
        return data


def _trim(value: Any, limit: int = 400) -> Any:
    """裁掉超长的字符串/列表，只留够判断的结构。"""
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "…"
    if isinstance(value, list):
        return [_trim(item, limit) for item in value[:20]]
    if isinstance(value, dict):
        return {str(k): _trim(v, limit) for k, v in value.items()}
    return value


@dataclass
class LoopResult:
    """一次任务的结果。"""

    answer: str = ""
    steps: list[Step] = field(default_factory=list)
    tool_calls: int = 0
    stopped: str = ""            # finished / max_steps / llm_error / not_configured
    error: str = ""
    tokens: dict[str, int] = field(default_factory=dict)
    elapsed_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "answer": self.answer,
            "steps": [s.to_dict() for s in self.steps],
            "tool_calls": self.tool_calls,
            "tools_used": [s.tool for s in self.steps],
            "stopped": self.stopped,
            "error": self.error,
            "tokens": self.tokens,
            "elapsed_seconds": self.elapsed_seconds,
        }


def _tool_definitions(registry: Any, allow_tools: list[str] | None) -> list[dict[str, Any]]:
    """把 ToolSpec 转成 OpenAI 兼容的工具定义。

    ``allow_tools`` 是**评测用的白名单**：评测一条任务时只给它该用的工具，
    否则模型可能用别的方式绕过去，测出来的是"它会不会用工具"，
    而不是"它会不会用对工具"。
    """
    wanted = set(allow_tools or [])
    defs: list[dict[str, Any]] = []
    for spec in registry.list_tools():
        if wanted and spec.name not in wanted:
            continue
        defs.append(
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.params_schema,
                },
            }
        )
    return defs


def _parse_arguments(raw: Any) -> dict[str, Any]:
    """模型给的工具参数（可能是 JSON 字符串）。"""
    if isinstance(raw, dict):
        return raw
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _tool_result_text(result: dict[str, Any], limit: int = 6000) -> str:
    """工具结果转成回灌给模型的文本。

    截断是必要的：``run_cases`` 一次能返回几十 KB，
    原样回灌会把上下文撑爆，而且模型也读不完。
    """
    try:
        text = json.dumps(result, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(result)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（已截断，共 {len(text)} 字符）"


def run_task(
    task: str,
    *,
    max_steps: int = DEFAULT_MAX_STEPS,
    allow_tools: list[str] | None = None,
    max_tool_calls: int = 0,
    registry: Any = None,
    client: Any = None,
) -> LoopResult:
    """跑一个任务，返回模型最后的结论和完整调用链。

    Args:
        task: 自然语言任务，比如"看下 mall4j/admin_area 有几条用例"
        max_steps: 最多几轮（每轮 = 模型回一次话，可能带多个工具调用）
        allow_tools: 只允许用这些工具（``None`` = 全部）
        max_tool_calls: 工具调用总数上限（``0`` = 不限制）。超了不再执行，
            **只把拒绝原因回灌** —— 见下面那段说明。
        registry: 工具注册表（测试注入用）
        client: LLM 客户端（测试注入用）

    Returns:
        ``LoopResult`` —— **不抛异常**，失败信息放在 ``stopped`` / ``error`` 里。
        评测需要的是"这一条没通过"，不是"整批跑崩了"。

    **为什么工具调用数要单独限**

    ``max_steps`` 限的是"模型回几次话"，但**一轮里可以带多个工具调用**，
    而且模型会「被拒 → 换参数 → 再试」。实测过一个任务：单轮返回了 15 个
    ``run_cases``，绕过 ``run_cases`` 的边界校验把 23 个模块逐个跑了一遍 ——
    总量和跑全量一样，只是分成了 15 次。限轮数根本拦不住。

    所以超限之后**不再真的执行**，只把拒绝原因作为工具结果回灌。
    模型看到"这个任务不允许再调工具了"，才有可能收敛给结论。
    """
    started = time.perf_counter()
    result = LoopResult()

    if registry is None:
        from ai.agent.tools import get_registry

        registry = get_registry()

    if client is None:
        from ai.capabilities.client import get_client

        client = get_client()

    if not getattr(client, "available", False):
        result.stopped = "not_configured"
        result.error = "LLM 没配置（AI_BASE_URL / AI_API_KEY / AI_MODEL）"
        return result

    tools = _tool_definitions(registry, allow_tools)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": task},
    ]
    tokens = {"prompt": 0, "completion": 0, "total": 0}
    call_budget = max(0, int(max_tool_calls or 0))

    for index in range(1, max(1, int(max_steps)) + 1):
        try:
            reply = client.chat_with_tools(messages, tools)
        except Exception as exc:  # noqa: BLE001 - 网络/模型错误都算这一条没跑完
            result.stopped = "llm_error"
            result.error = f"{type(exc).__name__}: {exc}"
            log.warning("评测循环第 %s 步模型调用失败: %s", index, exc)
            break

        used = reply.get("usage") or {}
        for key, field_name in (("prompt", "prompt_tokens"), ("completion", "completion_tokens")):
            tokens[key] += int(used.get(field_name) or 0)
        tokens["total"] = tokens["prompt"] + tokens["completion"]

        message = reply.get("message") or {}
        tool_calls = message.get("tool_calls") or []

        # 没有工具调用 = 模型认为任务完成了
        if not tool_calls:
            result.answer = str(message.get("content") or "").strip()
            result.stopped = "finished"
            break

        # assistant 消息要原样塞回历史，否则模型下一轮看不到自己调过什么
        messages.append(
            {
                "role": "assistant",
                "content": message.get("content") or "",
                "tool_calls": tool_calls,
            }
        )

        for call in tool_calls:
            function = call.get("function") or {}
            name = str(function.get("name") or "")
            arguments = _parse_arguments(function.get("arguments"))
            call_id = str(call.get("id") or f"call_{index}")

            # 超预算：不执行，只回灌拒绝原因。
            # 注意**也要记一步** —— 否则评测看不见"它想调但被拦了"，
            # 只能看见调用次数变少，会误判成模型自己收敛了。
            if call_budget and result.tool_calls >= call_budget:
                refusal = {
                    "ok": False,
                    "tool": name,
                    "error": {
                        "type": "ToolError",
                        "message": (
                            f"本次任务最多允许调用 {call_budget} 次工具，已经用完，"
                            "这次调用没有执行"
                        ),
                        "hint": (
                            "不要再调工具了。用已有的信息直接给结论；"
                            "信息不够就说明缺什么、建议用户怎么做。"
                        ),
                    },
                }
                result.steps.append(
                    Step(
                        index=index,
                        tool=name,
                        arguments=arguments,
                        ok=False,
                        error=refusal["error"]["message"],
                        skipped=True,
                    )
                )
                result.tool_calls += 1
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": _tool_result_text(refusal),
                    }
                )
                continue

            call_started = time.perf_counter()
            tool_result = registry.call(name, arguments)
            elapsed = round((time.perf_counter() - call_started) * 1000, 1)

            error = ""
            if not tool_result.get("ok"):
                err = tool_result.get("error") or {}
                error = str(err.get("message") or "") if isinstance(err, dict) else str(err)

            result.steps.append(
                Step(
                    index=index,
                    tool=name,
                    arguments=arguments,
                    ok=bool(tool_result.get("ok")),
                    error=error,
                    duration_ms=elapsed,
                    result=tool_result,
                )
            )
            result.tool_calls += 1

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": _tool_result_text(tool_result),
                }
            )
    else:
        # for 正常跑完 = 用完步数还没给结论
        result.stopped = "max_steps"

    if not result.stopped:
        result.stopped = "max_steps"

    result.tokens = tokens
    result.elapsed_seconds = round(time.perf_counter() - started, 2)
    return result
