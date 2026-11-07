"""MCP prompts：把「怎么用这套工具」固化下来。

**为什么要有 prompts**

工具描述告诉模型"能做什么"，但**不告诉它先做什么后做什么**。
同一条需求，不同模型走出来的路径差别很大：

- 有的上来就 ``run_cases`` 跑全量
- 有的不看失败就猜原因
- 有的直接把"服务 500"改成"断言放宽"

prompts 就是**预设的标准作业流程（SOP）**，客户端可以一键调用，
也可以在 ``/`` 命令里看到。这比在系统提示词里堆几十行规则可靠。

参数用 MCP 的 ``arguments`` 传，模板里用 ``{{名字}}`` 占位。
"""

from __future__ import annotations

from typing import Any

from ai.mcp_server.protocol import INVALID_PARAMS, RPCError

# name -> (描述, 参数定义, 模板)
PROMPTS: dict[str, dict[str, Any]] = {
    "run_and_report": {
        "description": "跑指定模块的用例，汇总结果并给出失败归因",
        "arguments": [
            {"name": "project", "description": "项目名，如 mall4j", "required": True},
            {"name": "module", "description": "模块名，如 prod_prod（留空则先列模块）", "required": False},
        ],
        "template": """你是接口测试执行助手。请完成下面的任务，过程中只用工具拿事实，不要臆测。

目标：跑「{{project}}」{{module}} 的接口用例并汇报结果。

步骤：
1. 如果模块没指定，先调 list_modules(project="{{project}}") 列出模块，挑一个最相关的并说明理由。
2. 调 run_cases(project="{{project}}", module="<选定的模块>") 执行。
   模块必须给 —— 只给 project 等于跑整个项目，工具会拒绝。
3. 如果 result 里有失败，调 diagnose_failures() 做归因。
4. 按下面的结构汇报：

## 执行结果
- 总数 / 通过 / 失败 / 跳过，耗时

## 失败清单
每条一行：用例名 | 归因 | 一句话原因 | 下一步动作

## 结论
- 环境问题 X 条 → 建议重跑
- 服务问题 Y 条 → 需要提缺陷
- 用例数据问题 Z 条 → 可走自愈流程

**硬性要求**
- 归因为「服务问题」的用例，绝对不要建议修改用例断言来让它变绿。
- 不要无筛选跑全量用例。
- 失败原因必须来自 diagnose_failures 的输出，不要自己编。""",
    },
    "write_case": {
        "description": "给一个接口写一条符合本框架规范的用例",
        "arguments": [
            {"name": "project", "description": "项目名", "required": True},
            {"name": "endpoint", "description": "接口路径或关键字", "required": True},
            {"name": "module", "description": "要放进哪个模块目录", "required": True},
        ],
        "template": """你是接口测试用例编写助手。请为接口「{{endpoint}}」写一条用例，放进「{{project}}/{{module}}」模块。

**先调研，再动笔**（顺序不能乱）：
1. search_endpoints(query="{{endpoint}}") 找到接口，再用 get_endpoint 拿完整定义。
2. list_api_definitions(project="{{project}}", module="{{module}}") —— 有现成接口定义就写 `api: <名字>` 引用，别重复写 path。
3. list_cases(project="{{project}}", module="{{module}}", limit=5) 看同模块已有用例的风格，照着写。
4. 如果接口有写操作（POST/PUT/DELETE）或需要验落库：find_field_mapping(field, endpoint)
   拿字段对应的表列，**不要自己按驼峰转下划线猜**。置信度 0.6 的候选不要用来写断言。

**落盘用 create_case，不要手写文件**
- `assertions` 用框架内部格式：{"type": "equal", "field": "code", "expected": "00000"}。
  字段路径**不带** `$.` 前缀。工具会自动转成 Excel 单元格里的扁平格式。
- 比较类断言必须写 `expected`；落库断言用 {"type": "db_query", "query": ..., "alias": ..., "params": [...], "expected": ...}。
- 新建的用例是 draft（CI 不跑），必须跑通再改状态。

**用例要求**
- 每条步骤必须有 assert，至少断言 `success` 和业务字段；只断言 status_code 不算。
- 写操作用例要有 setups（造数）和 teardowns（清理），teardown 只能删本用例自己造的数据。
- 用例名用中文短语，一眼能看懂测什么。
- 加上 `marks`，写操作用例打上模块标签。

**流程**：create_case → run_cases(case_file=...) → 通过后 set_case_status(case_id, "debugged")。
没跑通就如实说没跑通，不要标 debugged。最后附一行「覆盖了哪些断言维度」。""",
    },
    "heal_failure": {
        "description": "诊断失败用例并修复（归因分派 → 取证 → 预览 → 落盘 → 重跑验证，不过自动回滚）",
        "arguments": [
            {"name": "case_name", "description": "失败用例的名字或关键字", "required": True},
        ],
        "template": """你是接口测试自愈助手。目标用例：「{{case_name}}」。

**严格按顺序执行，不允许跳步**：

1. get_last_result() —— 确认它确实失败了，拿到错误信息。
2. list_cases(keyword="{{case_name}}") → get_case(case_id) —— 记下 case_id（`API-168` 这种）。
   后面的写类工具和自愈工具只认编号；同名用例只有编号能区分。
3. plan_heal(case_id) —— **这一步决定后面能做什么**。它返回归因和允许的动作。

**按 plan_heal 的 action 分派（不要自己另判）**：

- `rerun`（环境问题）→ run_cases(case_id=...) 重跑确认。**不要动任何文件**。
- `file_defect`（服务问题）→ **停止**。整理证据（请求/响应/期望 vs 实际）说明这是缺陷。
  确实短期修不了，set_case_status(status="broken", note="...") 标记 —— 用例内容保持原样。
  **绝对不允许**改断言或改期望值让它变绿。
- `manual_fix`（脚本问题）→ 按报错定位到具体文件（结构 / 变量引用 / 路径）。改断言没用。
- `stop`（已改两轮）/ `manual_review`（没归因）→ **停止**，把结论交给用户。

只有 `action="heal"` 才继续往下走。

4. 取证：
   - get_endpoint(path, method) 看接口定义有没有变（字段改名、枚举值变了、路径加了前缀）
   - 如果涉及落库：find_field_mapping / get_table_schema 确认列名
   - query_db 查一下实际数据长什么样
   - read_case_assertions(case_id) 看现有断言

5. 改的时候走三步，不能跳：
   - propose_case_change(case_id, assertions=[...]) —— 只算 diff 不落盘。
     先看 diff.removed 和 weakenings：不想删的断言出现在里面，说明新列表写漏了。
   - apply_case_change(case_id, assertions=[...], note="<为什么改>") —— 落盘，记下返回的 backup_dir。
     weakenings 非空时会被拒绝（这是设计如此），不是让你加 allow_weakening 绕过去。
   - verify_heal(case_id, backup_dir=...) —— **必须跑**，没通过会自动回滚。
     没跑过就说"修好了"是不允许的。

**硬性约束**
- 归因为服务问题的失败**一律不改断言、不改期望值**。改成服务端实际返回的那个值，
  强度没变但缺陷被藏起来了 —— 断言强度闸拦不住这种，所以归因闸先拦。
- 断言只能变强，不能变弱 —— 把 `equal` 降级成 `not_null`、减少断言数量，都是禁止的。
- 拿不出证据的改动不要提。
- 同一条用例改两轮还没跑通 → 停下来汇报，别再试第三次（工具也会拒）。""",
    },
    "sync_docs": {
        "description": "检查接口文档与用例的差距，列出该补的用例",
        "arguments": [
            {"name": "project", "description": "项目名", "required": True},
        ],
        "template": """你是接口覆盖分析助手。分析「{{project}}」的接口覆盖情况。

1. get_coverage(project="{{project}}") —— 拿到覆盖率和缺口。
2. 对 untested 里的前 5 个接口，用 search_endpoints / get_endpoint 看它们干什么。
3. list_modules(project="{{project}}") —— 看现有模块划分。

**输出**
## 覆盖率
接口覆盖率 / 断言覆盖率 / 用例总数

## 最该补的接口（按业务重要性排序，不超过 5 个）
每个接口一行：method path | 干什么 | 建议放哪个模块 | 该测什么

## 只调了没断言的接口
列出 no_assert 的接口，说明该补什么断言（业务码？字段存在？落库？）

不要生成用例，只做分析。""",
    },
    "review_agent_run": {
        "description": "回看智能体这一轮做了什么、花了多少，判断值不值",
        "arguments": [
            {"name": "run", "description": "评测任务 id（留空则看最近一轮）", "required": False},
        ],
        "template": """你是智能体行为审计员。请用审计日志还原最近的工作过程，只讲事实。

1. get_agent_cost(limit=500) —— 看总览：调了哪些工具、失败几次、耗时、token。
2. get_agent_audit(limit=100, run="{{run}}") —— 逐条看调用链。
   （`run` 留空就是看全部；填评测任务 id 只看那一条。）
3. 如果看到 apply_case_change / set_case_status / create_case 被调用过，
   用 get_agent_audit(tool="apply_case_change") 单独把它们列出来。

**重点看四件事**
- **有没有反复调同一个工具**（说明它在试错而不是在推进）
- **有没有改过用例文件**（write_calls > 0 就要说清楚改了哪条、为什么）
- **失败的调用是什么原因**（参数写错 → 提示词该补；被工具拒绝 → 它在试图越界）
- **token 花在哪**（llm 档的次数 × 单次规模）

**输出**
## 总览
调用 N 次 / 失败 M 次 / 耗时 X 秒 / token Y

## 调用链
按时间顺序，每条一行：工具 | 参数摘要 | 结果

## 值得注意的地方
只列真正异常的：重复调用、越界尝试、改了文件、失败率高

不要评价"智能不智能"，只描述行为和代价。""",
    },
}


def list_prompts() -> list[dict[str, Any]]:
    """MCP ``prompts/list`` 的返回。"""
    return [
        {
            "name": name,
            "description": spec["description"],
            "arguments": spec["arguments"],
        }
        for name, spec in PROMPTS.items()
    ]


def get_prompt(name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """MCP ``prompts/get``：把参数填进模板。"""
    spec = PROMPTS.get(name)
    if spec is None:
        raise RPCError(INVALID_PARAMS, f"没有提示词模板: {name}", {"available": sorted(PROMPTS)})

    args = {k: str(v) for k, v in (arguments or {}).items() if v is not None}
    text = spec["template"]
    # 可选参数没给时也要替换掉 —— 否则模板里会残留 ``{{module}}`` 这种占位符，
    # 模型看到会当成变量名照抄，甚至反问你「module 是什么」。
    for arg in spec["arguments"]:
        text = text.replace("{{" + arg["name"] + "}}", args.get(arg["name"], ""))
    for key, value in args.items():
        text = text.replace("{{" + key + "}}", value)

    # 必填参数没给就别硬填
    missing = [
        a["name"]
        for a in spec["arguments"]
        if a.get("required") and not args.get(a["name"])
    ]
    if missing:
        raise RPCError(
            INVALID_PARAMS,
            f"缺少必填参数: {', '.join(missing)}",
            {"missing": missing},
        )

    return {
        "description": spec["description"],
        "messages": [
            {"role": "user", "content": {"type": "text", "text": text}},
        ],
    }
