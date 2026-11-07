# ai/agent/ — 接口测试智能体运行时

把现有框架的能力包成**智能体可调用的工具**，不重写任何业务逻辑。

## 分层

```
ai/mcp_server/      MCP 服务端（stdio），把工具暴露给 Claude Code / Cursor / 平台
ai/agent/tools/     工具契约 + 各工具的适配器   ← 你新增工具只改这里
.claude/skills/     给人用的自然语言入口（/apitest-run 等）
.claude/agents/     专职 subagent（执行员、分析员）
```

## 已注册的工具（27 个）

| 工具 | 干什么 | 副作用 |
|---|---|---|
| `list_modules` | 列出项目/模块及用例数 | 只读 |
| `list_cases` | 按项目/模块/标签/关键字/状态筛用例 | 只读 |
| `get_case` | 拿一条用例的完整结构 | 只读 |
| `list_marks` | 列出所有标签及用例数 | 只读 |
| `run_cases` | **跑用例**（必须带筛选条件） | 执行 |
| `get_last_result` | 读上次运行结果 | 只读 |
| `search_endpoints` | 在 OpenAPI 快照里搜接口 | 只读 |
| `get_endpoint` | 拿接口完整定义（参数/请求体/响应） | 只读 |
| `list_api_definitions` | 列出可复用的接口定义 | 只读 |
| `get_table_schema` | 查库表结构（读本地快照） | 只读 |
| `query_db` | 只读 SQL（白名单 + 只读账号双保险） | 只读 |
| `diagnose_failures` | 失败归因（环境/用例数据/服务/脚本） | 只读 |
| `get_coverage` | 覆盖率 + 未覆盖接口清单 | 只读 |
| `plan_heal` | 查归因，返回允许的动作 | 只读 |
| `get_agent_audit` | 逐条调用链（排障） | 只读 |
| `get_agent_cost` | 按工具聚合次数/耗时/token | 只读 |

（完整清单见 `python -m ai.mcp_server.selfcheck -v`。另有 3 个写用例工具、
1 个执行验证工具，全部登记在 `test_non_read_tools_are_exactly_the_expected_set` 里。）

**会改变环境的只有 `run_cases` 和 `verify_heal`**，其余要么只读、要么只改用例文件。

## 怎么用

### 在 Claude Code 里

项目根已有 `.mcp.json` 和 `.claude/settings.json`，重启会话后自动连上。
然后用自然语言：

```
跑一下 mall4j 的 admin_hotSearch 模块，失败了告诉我为什么
```

或者用 skill：`/apitest-run`、`/apitest-author`、`/apitest-heal`、`/apitest-coverage`

### 自检（不联网、不跑用例）

```bash
python mcp_server/selfcheck.py        # 起真实子进程走一遍 MCP 协议
python -m pytest unit_tests/test_agent_tools.py -q
```

### 手动调试协议

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' | python -m mcp_server
```

## 加一个新工具

1. 在 `ai/agent/tools/<模块>.py` 写个普通函数：返回 dict，失败抛 `ToolError(msg, hint=...)`
2. 声明 `ToolSpec`（参数用 JSON Schema），加进该文件的 `TOOLS` 列表
3. 跑 `python -m pytest unit_tests/test_agent_tools.py -q`

MCP 服务端、`tools/list` 会自动发现，不用改别处。

## 设计约束（别破坏）

- **`ToolSpec.side_effect` 必须诚实**：`read` 的工具绝不能改任何东西。
  智能体的权限策略是按这个字段配的。
- **错误要带 `hint`**：只说"失败了"模型会重试同一个错；说清楚"接下来试什么"它才会换路。
- **返回值控制在上下文能承受的量**：列表类工具默认 limit 50，超过要显式 `truncated: true`。
- **写类工具必须能撤销**：写前备份、写后校验、失败回滚，三道都不能省。
- **`run_cases` 有三道边界闸**（条件 / 单次条数 / 会话累计）——
  单看任何一道都挡不住"化整为零"，见 `docs/FRAMEWORK.md` 第二十一节。
- **审计是旁路**：写失败只告警，绝不能让记不了账导致不办事。
