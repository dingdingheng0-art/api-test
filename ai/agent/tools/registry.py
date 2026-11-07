"""工具注册表 —— 智能体能调用的所有能力的统一契约。

**为什么需要契约**

LLM 要调工具，必须先知道三件事：

1. 工具叫什么、干什么（``name`` / ``description``）
2. 参数长什么样（``params_schema``，标准 JSON Schema，MCP 直接用）
3. 调了会发生什么（``side_effect``）

第 3 点最容易被忽略，但它决定了**能不能让智能体自动调用**：

============  ============================================
side_effect   含义
============  ============================================
``read``      只读。随便调，不会改变任何东西
``execute``   会跑用例。可能造/清测试数据，但不动用例文件
``write_case``会改用例文件（必须人审 + 备份 + 回滚）
``write_real``会改真实业务数据（默认禁止）
============  ============================================

``write_case`` 已经开放，但拆成了三步：**预览**（``propose_case_change``，
标 ``read``，只算 diff 不落盘）、**落盘**（``apply_case_change``，写前备份、
写后校验、失败回滚）、**改状态**（``set_case_status``）。智能体拿到的
不是"直接改文件"的能力，而是"提交 patch 并让框架去写"的能力 ——
格式转换、校验、回滚都在 ``case_io`` 里，绕不过去。

``write_real`` 迄今没有工具，也不打算有：动真实业务数据应该由用例
自己的 setup/teardown 去做，那是可审计、可重放的。

**统一错误处理**

工具抛异常不该让 MCP 连接断掉，也不该让智能体拿到一堆调用栈。
``ToolRegistry.call`` 把异常统一收成::

    {"ok": false, "error": {"type": "ToolError", "message": "...", "hint": "..."}}

智能体看到 ``ok: false`` 就知道要换个思路，而不是重试同一个错误调用。
"""

from __future__ import annotations

import json
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

from utils.logs.log_manager import log


class ToolError(RuntimeError):
    """工具执行失败（可预期的业务错误，不是代码 bug）。

    ``hint`` 是给智能体看的下一步建议 —— 光说"失败了"没用，
    要说"接下来可以试什么"。
    """

    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.hint = hint


@dataclass(frozen=True)
class ToolSpec:
    """一个工具的完整定义。"""

    name: str
    description: str
    params_schema: dict[str, Any]
    handler: Callable[..., Any]
    #: read | execute | write_case | write_real
    side_effect: str = "read"
    #: 需要的额外保护说明（比如"会过 safety_guard"）
    safety: str = ""
    #: free | network | llm
    cost: str = "free"
    tags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def read_only(self) -> bool:
        return self.side_effect == "read"

    def to_mcp_tool(self) -> dict[str, Any]:
        """转成 MCP ``tools/list`` 里的条目。"""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.params_schema,
            "annotations": {
                "readOnlyHint": self.read_only,
                "destructiveHint": self.side_effect in ("write_case", "write_real"),
                "idempotentHint": self.read_only,
                "openWorldHint": self.cost != "free",
            },
        }


class ToolRegistry:
    """工具注册表（进程内单例）。"""

    _instance: "ToolRegistry | None" = None

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._calls: list[dict[str, Any]] = []

    @classmethod
    def get(cls) -> "ToolRegistry":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ---------------- 注册 ----------------

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"工具重复注册: {spec.name}")
        self._tools[spec.name] = spec

    def register_many(self, specs: list[ToolSpec]) -> None:
        for spec in specs:
            self.register(spec)

    # ---------------- 查询 ----------------

    def get_tool(self, name: str) -> ToolSpec:
        spec = self._tools.get(name)
        if spec is None:
            raise ToolError(
                f"没有名为 {name!r} 的工具",
                hint=f"可用工具: {', '.join(sorted(self._tools))}",
            )
        return spec

    def names(self) -> list[str]:
        return sorted(self._tools)

    def list_tools(self) -> list[ToolSpec]:
        return [self._tools[n] for n in sorted(self._tools)]

    def to_mcp_tools(self) -> list[dict[str, Any]]:
        return [spec.to_mcp_tool() for spec in self.list_tools()]

    # ---------------- 调用 ----------------

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """调用一个工具，永远返回 dict，永远不抛异常。

        返回结构::

            成功: {"ok": true, "tool": "...", "duration_ms": 12, ...业务字段}
            失败: {"ok": false, "tool": "...", "error": {"type", "message", "hint"}}
        """
        arguments = dict(arguments or {})
        started = time.perf_counter()

        try:
            spec = self.get_tool(name)
        except ToolError as exc:
            return self._fail(name, exc, started, arguments)

        try:
            result = spec.handler(**arguments)
        except ToolError as exc:
            return self._fail(name, exc, started, arguments)
        except TypeError as exc:
            # 参数名写错 / 少传 —— 这是智能体最容易犯的错，要说清楚
            return self._fail(
                name,
                ToolError(
                    f"参数不匹配: {exc}",
                    hint=f"该工具的参数: {self._describe_params(spec)}",
                ),
                started,
                arguments,
            )
        except Exception as exc:  # noqa: BLE001 - 工具内部任何异常都要收住
            log.error("工具 %s 执行异常: %s", name, exc)
            log.debug(traceback.format_exc())
            return self._fail(
                name,
                ToolError(f"{type(exc).__name__}: {exc}", hint="查看日志 logs/ 里的堆栈"),
                started,
                arguments,
            )

        elapsed = round((time.perf_counter() - started) * 1000, 1)

        if not isinstance(result, dict):
            result = {"result": result}
        result.setdefault("ok", True)
        result.setdefault("tool", name)
        result["duration_ms"] = elapsed

        self._record(name, arguments, result, elapsed)
        return result

    def _fail(
        self,
        name: str,
        exc: ToolError,
        started: float,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        elapsed = round((time.perf_counter() - started) * 1000, 1)
        payload = {
            "ok": False,
            "tool": name,
            "duration_ms": elapsed,
            "error": {"type": type(exc).__name__, "message": str(exc)},
        }
        if exc.hint:
            payload["error"]["hint"] = exc.hint
        log.warning("工具 %s 失败: %s", name, exc)
        self._record(name, dict(arguments or {}), payload, elapsed)
        return payload

    @staticmethod
    def _describe_params(spec: ToolSpec) -> str:
        props = (spec.params_schema.get("properties") or {})
        if not props:
            return "（无参数）"
        return "、".join(
            f"{k}({v.get('type', 'any')}{'，必填' if k in (spec.params_schema.get('required') or []) else ''})"
            for k, v in props.items()
        )

    def _record(
        self,
        name: str,
        arguments: dict[str, Any],
        result: dict[str, Any],
        elapsed: float,
    ) -> None:
        """留一份调用轨迹：内存留最近 200 条，同时追加到审计日志。

        **内存那份为什么还留着**：``recent_calls()`` 是给同一进程内的
        调用链看上下文用的（比如自愈流程要回看刚改了什么），读内存最快。
        落盘那份解决的是**跨进程**的问题 —— 服务重启后还能查。

        ``_fail`` 里调进来时 ``arguments`` 是空的：失败可能是参数没传对，
        这时候原始参数反而更有诊断价值。所以失败路径单独把参数带上。
        """
        entry = {
            "tool": name,
            "arguments": arguments,
            "ok": bool(result.get("ok")),
            "duration_ms": elapsed,
        }
        self._calls.append(entry)
        # 只留最近 200 条，长时间跑的 MCP 服务不能无限涨内存
        if len(self._calls) > 200:
            del self._calls[:-200]

        self._audit(name, arguments, result, elapsed)

    def _audit(
        self,
        name: str,
        arguments: dict[str, Any],
        result: dict[str, Any],
        elapsed: float,
    ) -> None:
        """写审计日志。拿不到工具定义也照记 —— 调用一个不存在的工具
        本身就是值得审计的事件。"""
        from ai.agent import audit_log

        spec = self._tools.get(name)
        error = ""
        if not result.get("ok"):
            err = result.get("error") or {}
            error = str(err.get("message") or "") if isinstance(err, dict) else str(err)

        audit_log.record(
            name,
            ok=bool(result.get("ok")),
            duration_ms=elapsed,
            side_effect=spec.side_effect if spec else "",
            cost=spec.cost if spec else "",
            arguments=arguments,
            error=error,
        )

    def recent_calls(self) -> list[dict[str, Any]]:
        return list(self._calls)


def ok(**fields: Any) -> dict[str, Any]:
    """构造一个成功结果（工具内部用）。"""
    return {"ok": True, **fields}


def truncate_text(text: str, limit: int = 4000) -> str:
    """截断长文本，并明确标出截了多少 —— 让智能体知道信息不全。"""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...（已截断，共 {len(text)} 字符）"


def json_size(value: Any) -> int:
    """估算一个结构的 JSON 长度（判断要不要截断用）。"""
    try:
        return len(json.dumps(value, ensure_ascii=False))
    except (TypeError, ValueError):
        return 0
