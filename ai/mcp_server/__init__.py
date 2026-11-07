"""MCP 服务端：把接口测试框架暴露给任何支持 MCP 的客户端。

Claude Code / Cursor / 平台都能连。暴露三类东西：

- **tools** —— 直接来自 ``ai.agent/tools/`` 的注册表，零重复定义
- **resources** —— 用例文件、OpenAPI 快照、表结构、最新报告（按需读，不占上下文）
- **prompts** —— 预置的提示词模板（跑测试 / 写用例 / 诊断失败 / 同步文档）

**为什么用最小 stdio 实现而不是官方 SDK**

官方 ``mcp`` 包不在当前依赖里，而 MCP 的 stdio 协议本身就是
「一行一个 JSON-RPC 2.0 消息」，实现量很小。自己实现的好处：

- 不新增依赖（这个项目一贯风格：能不加包就不加）
- 出错时能看到真实堆栈，不用猜 SDK 里发生了什么
- 协议升级时改动点集中在一个文件

需要官方 SDK 的特性（如 HTTP 传输、采样）时再换，工具层不用改。

**注意**：这里只导出协议常量，**不 import ``server``** ——
``server`` 一 import 就会去动 logging 的 handler，单测里会有副作用。
需要主循环时用 ``python -m mcp_server``。
"""

from ai.mcp_server.protocol import PROTOCOL_VERSION, RPCError

__all__ = ["PROTOCOL_VERSION", "RPCError"]
