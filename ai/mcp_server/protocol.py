"""MCP 协议层：JSON-RPC 2.0 over stdio。

**协议要点**

MCP 的 stdio 传输是**按行分隔的 JSON-RPC 2.0**：
每个请求/响应是一个单行 JSON，以 ``\\n`` 结尾。

客户端（Claude Code / Cursor）会依次发：

1. ``initialize`` —— 握手，声明协议版本和能力
2. ``notifications/initialized`` —— 通知（没有 id，不回复）
3. ``tools/list`` / ``tools/call`` / ``resources/read`` / ``prompts/get``

**关键约束**

- 通知（没有 ``id``）**不能回响应**，回了客户端会解析错乱
- 任何异常都要转成 JSON-RPC error，不能让进程崩掉 ——
  stdio 服务一崩，客户端就断开，用户得手动重连
- ``stdout`` 只能写协议消息；日志必须走 ``stderr``
  （``utils.logs.log_manager`` 已经写到 stdout 了，所以服务启动时要把
  它的 console handler 挪到 stderr，见 ``server.py``）
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Any

# 支持的协议版本（客户端传哪个就回哪个，不认识的回最新）
PROTOCOL_VERSION = "2024-11-05"
SUPPORTED_VERSIONS = ("2024-11-05", "2024-10-07", "2025-03-26", "2025-06-18")

# JSON-RPC 标准错误码
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


@dataclass
class RPCError(Exception):
    """可以转成 JSON-RPC error 响应的异常。"""

    code: int
    message: str
    data: Any = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            payload["data"] = self.data
        return payload


def read_message(stream: Any) -> dict[str, Any] | None:
    """从流里读一条消息（一行一个 JSON）。读到 EOF 返回 None。

    **只收 JSON 对象**。合法 JSON 不等于合法消息：客户端发来
    ``[1, 2]``（JSON-RPC 批量）、``"hi"``、``5`` 时 ``json.loads`` 都成功，
    但主循环紧接着要取 ``message.get("id")`` —— 对 list 取 ``.get`` 直接
    ``AttributeError``，而那句在 try 之外，**整个 stdio 服务会退出**，
    用户得手动重连。所以在入口就把非对象挡成 JSON-RPC 错误。
    """
    line = stream.readline()
    if not line:
        return None
    line = line.strip()
    if not line:
        return None
    try:
        data = json.loads(line)
    except ValueError as exc:
        raise RPCError(PARSE_ERROR, f"消息不是合法 JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RPCError(
            INVALID_REQUEST,
            f"消息必须是 JSON 对象，收到 {type(data).__name__}",
        )
    return data


def write_message(stream: Any, payload: dict[str, Any]) -> None:
    """写一条消息（单行 JSON + 换行，立即 flush）。"""
    stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
    stream.flush()


def make_result(msg_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def make_error(msg_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": msg_id, "error": error}


def log_stderr(message: str) -> None:
    """写 stderr（stdout 被协议占了，日志只能走这里）。"""
    try:
        sys.stderr.write(f"[apitest-mcp] {message}\n")
        sys.stderr.flush()
    except Exception:  # noqa: BLE001 - 日志失败绝不能影响服务
        pass
