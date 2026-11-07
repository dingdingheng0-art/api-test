"""接口测试智能体的运行时。

**这一层是什么**

现有的 ``run.py`` / ``verify/`` / ``utils/`` 是一套**给人用**的框架：
命令行敲参数、看终端输出、读报告。

智能体（LLM）要用的不是命令行，而是**带 schema 的工具**：

- 它得知道有哪些能力（``tools/list``）
- 每个能力要什么参数、返回什么结构（JSON Schema）
- 调用的副作用有多大（只读？会跑用例？会写文件？）

所以这里只做一件事：**把现有能力包成工具**，不重写任何业务逻辑。

.. code-block:: text

    ai.agent/tools/registry.py   工具契约（ToolSpec）+ 注册表 + 统一错误处理
    ai.agent/tools/cases.py      用例查询
    ai.agent/tools/execution.py  执行用例、读上次结果
    ai.agent/tools/endpoints.py  接口文档查询
    ai.agent/tools/data.py       库表结构、只读查库
    ai.agent/tools/diagnostics.py 失败归因
    ai.agent/tools/knowledge.py  知识图谱查询
    ai.agent/tools/case_io.py    用例文件读写（格式转换、备份、回滚）
    ai.agent/tools/case_edit.py  读断言 / 预览 / 落盘 / 改状态 / 新建用例

MCP 服务端（``mcp_server/``）只是把这些工具翻译成 MCP 协议，
换成 Claude Code / IDE / 平台都能接。
"""

from ai.agent.tools import get_registry, reset_registry

__all__ = ["get_registry", "reset_registry"]
