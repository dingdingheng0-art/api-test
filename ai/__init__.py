"""AI 能力层。

- :mod:`~ai.capabilities` —— 用例生成 / 缺陷分析 / LLM 客户端
- :mod:`~ai.agent`        —— 把框架能力包成智能体可调用的工具
- :mod:`~ai.mcp_server`   —— MCP 服务端（stdio），暴露给 Claude Code / Cursor / 平台

**这一层是可选的** —— 不配 API key 也能用，用例生成退回本地模板，
缺陷分析退回规则分类；不起 MCP 服务也不影响 `run.py` 跑用例。
"""
