"""MCP 链路自检：不联网、不跑用例，验证"智能体能不能用上这套工具"。

**查什么**

1. 工具都注册上了（数量不少于预期下限），schema 合法
2. 起一个真实子进程，按 MCP 协议握手、列工具、调几个只读工具
   （外加一个预览类工具 —— 它算 diff 但不落盘，所以放进自检是安全的）
3. stdout 只有协议消息（日志不能污染）
4. 通知消息不产生响应
5. resources / prompts 能正常读

**为什么用子进程而不是直接调函数**

直接调函数只能证明"代码没错"，证明不了"客户端连得上"。
之前踩过的坑（日志写到 stdout、模块找不到、编码问题）只有真起进程才暴露。

用法::

    python ai/mcp_server/selfcheck.py
    python ai/mcp_server/selfcheck.py -v      # 打印每个工具的返回摘要
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Windows 控制台默认 GBK，中文会花掉（和 run.py 同样的处理）
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# 自检时调用的工具：**都只读**，不会碰环境。
# 写类工具（apply_case_change / create_case …）绝不能放进来 ——
# 自检应该随时能跑，跑一次不该改掉一条用例。
PROBE_CALLS = [
    ("list_modules", {}),
    ("list_cases", {"limit": 2}),
    ("search_endpoints", {"limit": 2}),
    ("get_table_schema", {"keyword": "area", "alias": "mall4j"}),
    ("find_field_mapping", {"field": "areaName"}),
    # 审计是只读的，而且**自己就是审计数据的消费者** ——
    # 探它等于同时验证了"调用会落盘"这条链路
    ("get_agent_cost", {"limit": 50}),
]

#: 写类工具名单：自检要确认它们**存在**，但不会去调
WRITE_TOOL_NAMES = frozenset({
    "apply_case_change",
    "set_case_status",
    "create_case",
})

#: 至少要有这么多工具。低于这个数说明有模块没注册上
MIN_TOOLS = 27


def _quiet_framework_logs() -> None:
    """把框架日志压到 WARNING。

    自检的输出来自 ``print``；框架解析 Excel 时会打一堆 INFO，
    混在一起就看不清哪几行是结论。子进程的日志本来就重定向到 stderr 了，
    这里把父进程这一路也按住。
    """
    import logging

    logging.getLogger("autotest.framework").setLevel(logging.WARNING)


def _sample_case_id() -> str:
    """随便挑一条真实用例的编号，给 ``propose_case_change`` 用。

    写死 ``API-168`` 的话，那条用例一被删或改编号，自检就红了 —— 那是假故障，
    而且会让人开始不信任自检结果。这里现查一条（只读）。
    """
    _quiet_framework_logs()
    try:
        from ai.agent.tools import get_registry

        result = get_registry().call("list_cases", {"limit": 1})
        for case in result.get("cases") or []:
            case_no = str(case.get("case_id") or "").strip()
            if case_no:
                return case_no
    except Exception:  # noqa: BLE001 - 查不到就退回空，下面会跳过这个探针
        pass
    return ""


def _msgs() -> list[dict]:
    out: list[dict] = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "clientInfo": {"name": "selfcheck"}},
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]
    for i, (name, args) in enumerate(PROBE_CALLS, start=10):
        out.append(
            {
                "jsonrpc": "2.0",
                "id": i,
                "method": "tools/call",
                "params": {"name": name, "arguments": args},
            }
        )

    # 预览类工具单独探一个 —— 它证明「读 → 算 diff」这条链路通，
    # 但绝不落盘，所以放进自检是安全的。
    case_no = _sample_case_id()
    if case_no:
        out.append(
            {
                "jsonrpc": "2.0",
                "id": 20,
                "method": "tools/call",
                "params": {
                    "name": "propose_case_change",
                    "arguments": {
                        "case_id": case_no,
                        "assertions": [{"type": "status_code", "expected": 200}],
                    },
                },
            }
        )
        # 自愈分派也是只读的（只查归因，不跑用例不改文件）——
        # 探一下能证明「失败 → 归因 → 允许的动作」这条链路通
        out.append(
            {
                "jsonrpc": "2.0",
                "id": 21,
                "method": "tools/call",
                "params": {"name": "plan_heal", "arguments": {"case_id": case_no}},
            }
        )

    out += [
        {"jsonrpc": "2.0", "id": 90, "method": "resources/list", "params": {}},
        {"jsonrpc": "2.0", "id": 91, "method": "resources/read", "params": {"uri": "apitest://openapi"}},
        {"jsonrpc": "2.0", "id": 92, "method": "prompts/list", "params": {}},
    ]
    return out


def run(verbose: bool = False) -> int:
    requests = _msgs()
    payload = "\n".join(json.dumps(m, ensure_ascii=False) for m in requests) + "\n"

    proc = subprocess.run(
        [sys.executable, "-m", "ai.mcp_server"],
        cwd=str(_ROOT),
        input=payload,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )

    failures: list[str] = []

    # 1. 进程正常退出
    if proc.returncode != 0:
        failures.append(f"进程退出码 {proc.returncode}")

    # 2. stdout 只能有协议消息
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    bad = [line for line in lines if not line.lstrip().startswith("{")]
    if bad:
        failures.append(f"stdout 混入了非协议内容 {len(bad)} 行：{bad[0][:80]}")

    responses: dict[int, dict] = {}
    for line in lines:
        try:
            msg = json.loads(line)
        except ValueError:
            failures.append(f"响应不是合法 JSON：{line[:80]}")
            continue
        responses[msg.get("id")] = msg

    # 3. 通知不回复 —— 响应数量必须等于请求数量（有 id 的）
    expected_ids = {m["id"] for m in requests if "id" in m}
    if set(responses) != expected_ids:
        missing = expected_ids - set(responses)
        extra = set(responses) - expected_ids
        failures.append(f"响应 id 不匹配，缺 {sorted(missing)}，多 {sorted(extra)}")

    # 4. 握手
    init = responses.get(1, {}).get("result") or {}
    if init.get("serverInfo", {}).get("name") != "apitest-mcp":
        failures.append("initialize 没返回正确的 serverInfo")

    # 5. 工具列表
    tools = (responses.get(2, {}).get("result") or {}).get("tools") or []
    names = {t.get("name") for t in tools}
    if len(tools) < MIN_TOOLS:
        failures.append(f"工具数量偏少：{len(tools)}（至少应有 {MIN_TOOLS} 个）")

    missing_write = WRITE_TOOL_NAMES - names
    if missing_write:
        failures.append(f"写类工具没注册上：{sorted(missing_write)}")

    # 写类工具必须**如实**标注 readOnlyHint=false —— 客户端靠这个决定要不要弹确认
    for tool in tools:
        if tool.get("name") in WRITE_TOOL_NAMES:
            if (tool.get("annotations") or {}).get("readOnlyHint") is not False:
                failures.append(f"{tool['name']} 的 readOnlyHint 应该为 false")

    # 6. 工具调用
    print(f"  工具数: {len(tools)}")
    if verbose:
        print("  " + "、".join(sorted(n for n in names if n)))

    for i, (name, _args) in enumerate(PROBE_CALLS, start=10):
        resp = responses.get(i, {})
        if "error" in resp:
            failures.append(f"{name} 返回 JSON-RPC 错误：{resp['error']}")
            continue
        result = resp.get("result") or {}
        structured = result.get("structuredContent") or {}
        ok_flag = structured.get("ok")
        print(f"  {name:<20} ok={ok_flag}  {structured.get('duration_ms', '?')}ms")
        if verbose:
            preview = {k: v for k, v in structured.items() if k not in ("tool", "duration_ms")}
            print("      " + json.dumps(preview, ensure_ascii=False)[:300])
        if not ok_flag:
            failures.append(f"{name} 调用失败：{structured.get('error')}")

    # 6b. 预览类工具：必须返回 ok，且**必须**告诉我们文件没动
    if 20 in responses:
        resp = responses[20]
        if "error" in resp:
            failures.append(f"propose_case_change 返回 JSON-RPC 错误：{resp['error']}")
        else:
            structured = (resp.get("result") or {}).get("structuredContent") or {}
            print(f"  {'propose_case_change':<20} ok={structured.get('ok')}  "
                  f"case={structured.get('case_id', '?')}")
            if not structured.get("ok"):
                failures.append(f"propose_case_change 调用失败：{structured.get('error')}")
            elif structured.get("written") is not False:
                # 预览工具一旦落盘，自检就变成了"跑一次改一条用例" —— 这是红线
                failures.append("propose_case_change 的 written 应为 false（预览不该落盘）")

    # 6c. 自愈分派：必须是只读的，而且必须给出「能不能改用例」的明确结论
    if 21 in responses:
        resp = responses[21]
        if "error" in resp:
            failures.append(f"plan_heal 返回 JSON-RPC 错误：{resp['error']}")
        else:
            structured = (resp.get("result") or {}).get("structuredContent") or {}
            print(f"  {'plan_heal':<20} ok={structured.get('ok')}  "
                  f"action={structured.get('action', '?')}")
            if not structured.get("ok"):
                failures.append(f"plan_heal 调用失败：{structured.get('error')}")
            elif "can_edit_case" not in structured:
                failures.append("plan_heal 没返回 can_edit_case —— 模型没法判断能不能动手")

    # 7. resources
    res_list = (responses.get(90, {}).get("result") or {}).get("resources") or []
    print(f"  资源数: {len(res_list)}")
    if not res_list:
        failures.append("resources/list 为空")

    openapi = (responses.get(91, {}).get("result") or {}).get("contents") or []
    if not openapi or "paths" not in openapi[0].get("text", ""):
        failures.append("resources/read(apitest://openapi) 读不到接口文档")

    # 8. prompts
    prompts = (responses.get(92, {}).get("result") or {}).get("prompts") or []
    print(f"  提示词模板: {len(prompts)}")

    print()
    if failures:
        print("自检失败：")
        for item in failures:
            print(f"  - {item}")
        return 1

    print("自检通过：MCP 链路可用（stdio 协议、工具调用、资源、提示词全部正常）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="MCP 链路自检")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印详细内容")
    args = parser.parse_args()

    print("检查 MCP 服务端 ...")
    return run(args.verbose)


if __name__ == "__main__":
    raise SystemExit(main())
