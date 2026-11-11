"""MCP 服务端主循环（stdio 传输）。

启动方式（一般由客户端拉起，不用手动跑）::

    python -m mcp_server

调试时可以手动敲 JSON 进去::

    echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' | python -m mcp_server

**日志必须走 stderr**

``utils.logs.log_manager`` 默认把 console handler 挂在 stdout 上 —— 那是给人
看终端用的。MCP 的 stdout 被协议独占，一行普通日志插进去就会让客户端
解析失败。所以启动时把 console handler 换到 stderr，协议消息才干净。

**崩溃隔离**

任何请求处理里的异常都转成 JSON-RPC error，进程继续跑。
stdio 服务一崩客户端就断开，用户得手动重连 —— 这比"某个工具报错"
严重得多。
"""

from __future__ import annotations

import sys
import traceback
from typing import Any

from ai.mcp_server import prompts, resources
from ai.mcp_server.protocol import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    PROTOCOL_VERSION,
    RPCError,
    log_stderr,
    make_error,
    make_result,
    read_message,
    write_message,
)

SERVER_NAME = "apitest-mcp"
SERVER_VERSION = "0.1.0"

# 握手时告诉客户端我们支持什么
CAPABILITIES = {
    "tools": {"listChanged": False},
    "resources": {"subscribe": False, "listChanged": False},
    "prompts": {"listChanged": False},
}


def _route_console_logs_to_stderr() -> None:
    """把框架日志的 **console** handler 从 stdout 挪到 stderr。

    ⚠️ 只挪 stream 就是 ``sys.stdout`` 的那个 —— ``FileHandler`` 是
    ``StreamHandler`` 的子类，按类型判断会把写文件的 handler 也挪过来，
    结果是每行日志在 stderr 里出现两次、日志文件里反而没了。
    """
    try:
        import logging

        from utils.logs.log_manager import _LogManager

        logger = _LogManager.get()
        for handler in logger.handlers:
            if isinstance(handler, logging.FileHandler):
                continue
            if getattr(handler, "stream", None) is sys.stdout:
                handler.setStream(sys.stderr)
    except Exception as exc:  # noqa: BLE001 - 挪不动也不能让服务起不来
        log_stderr(f"日志重定向失败（可能污染协议）: {exc}")


def handle_request(method: str, params: dict[str, Any]) -> Any:
    """处理一个请求，返回 result。失败抛 ``RPCError``。"""
    if method == "initialize":
        requested = str(params.get("protocolVersion") or PROTOCOL_VERSION)
        return {
            "protocolVersion": requested,
            "capabilities": CAPABILITIES,
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": (
                "这是接口自动化测试框架的工具集。"
                "典型用法：list_modules → run_cases → diagnose_failures；"
                "写用例前先 search_endpoints / get_endpoint / get_table_schema。"
                "自愈失败用例先调 plan_heal(case_id) —— 它按归因决定允许的动作"
                "（环境问题只重跑 / 服务问题只提缺陷 / 用例数据问题才能改），"
                "然后 read_case_assertions → propose_case_change（只看 diff）"
                "→ apply_case_change（落盘，写前备份、写后校验、失败回滚）"
                "→ verify_heal（重跑验证，不过自动回滚）。"
                "写类工具只认 case_id（API-168 这种编号），不认文件路径和用例名。"
                "归因为服务问题的失败不要通过改用例断言来修复；改完必须验证。"
                "断言只能变强：删断言或把比较符改松会被 apply_case_change 拒绝，"
                "不要用 allow_weakening 绕过它。"
                "run_cases 有边界：只给 project 会被拒绝，一次最多跑 50 条、"
                "一次会话累计最多 50 条 —— 逐个模块拆开跑同样算跑全量，"
                "用户要跑全量时先报条数和代价、请他确认。"
                "新生成/改完的用例是 draft，验证时必须带 run_draft=true —— "
                "不带的话一条都不会执行，total=0 不代表通过。"
                "set_case_status 转 debugged 会核对「跑通过」凭据（run_cases "
                "整体通过后自动记账）；没跑过、或记账之后又改过用例，都会被拒 —— "
                "不要用 force 绕过，正常做法是重新跑一遍。"
                "想回看自己刚才调了什么、花了多少 token，用 get_agent_audit / get_agent_cost。"
            ),
        }

    if method == "ping":
        return {}

    if method == "tools/list":
        from ai.agent.tools import get_registry

        return {"tools": get_registry().to_mcp_tools()}

    if method == "tools/call":
        from ai.agent.tools import get_registry

        name = str(params.get("name") or "")
        if not name:
            raise RPCError(INVALID_PARAMS, "缺少工具名 name")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise RPCError(INVALID_PARAMS, "arguments 必须是对象")

        result = get_registry().call(name, arguments)
        return {
            "content": [{"type": "text", "text": _to_text(result)}],
            "isError": not result.get("ok", True),
            "structuredContent": result,
        }

    if method == "resources/list":
        return {"resources": resources.list_resources()}

    if method == "resources/read":
        uri = str(params.get("uri") or "")
        if not uri:
            raise RPCError(INVALID_PARAMS, "缺少 uri")
        return resources.read_resource(uri)

    if method == "prompts/list":
        return {"prompts": prompts.list_prompts()}

    if method == "prompts/get":
        name = str(params.get("name") or "")
        if not name:
            raise RPCError(INVALID_PARAMS, "缺少提示词名 name")
        return prompts.get_prompt(name, params.get("arguments") or {})

    raise RPCError(METHOD_NOT_FOUND, f"不支持的方法: {method}")


def _to_text(result: dict[str, Any]) -> str:
    """工具结果转成给模型看的文本。

    用 JSON 而不是自然语言：模型解析结构化数据比解析散文准确，
    而且 ``ok: false`` 里的 ``hint`` 会原样带上。
    """
    import json

    try:
        return json.dumps(result, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError):
        return str(result)


def serve(stdin: Any = None, stdout: Any = None) -> int:
    """主循环：读一条、处理一条、回一条。"""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout

    log_stderr(f"{SERVER_NAME} {SERVER_VERSION} 启动（stdio）")

    while True:
        try:
            message = read_message(stdin)
        except RPCError as exc:
            write_message(stdout, make_error(None, exc.code, exc.message))
            continue

        if message is None:
            log_stderr("客户端断开，退出")
            return 0

        msg_id = message.get("id")
        method = str(message.get("method") or "")

        # 通知（没有 id）不回复
        if msg_id is None:
            if method == "notifications/initialized":
                log_stderr("客户端握手完成")
            continue

        params = message.get("params") or {}
        if not isinstance(params, dict):
            write_message(stdout, make_error(msg_id, INVALID_PARAMS, "params 必须是对象"))
            continue

        try:
            result = handle_request(method, params)
        except RPCError as exc:
            write_message(stdout, make_error(msg_id, exc.code, exc.message, exc.data))
            continue
        except Exception as exc:  # noqa: BLE001 - 任何异常都不能让服务挂掉
            log_stderr(f"处理 {method} 出错: {exc}\n{traceback.format_exc()}")
            write_message(
                stdout,
                make_error(msg_id, INTERNAL_ERROR, f"{type(exc).__name__}: {exc}"),
            )
            continue

        write_message(stdout, make_result(msg_id, result))


def main() -> int:
    _route_console_logs_to_stderr()
    try:
        return serve()
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
