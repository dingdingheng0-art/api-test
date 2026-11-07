"""工具注册表 + 各工具的单元测试。

**不联网、不连库、不跑用例** —— 只验证契约和边界：

- 注册表能列出工具，schema 是合法 JSON Schema 结构
- 参数写错时返回 ``ok: false`` 而不是抛异常
- 只读工具不会真的去写东西
- ``query_db`` 拒绝写操作
- MCP 协议层能把工具翻译成正确的 ``tools/list`` 结构
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture()
def registry():
    from ai.agent.tools import reset_registry

    return reset_registry()


# --------------------------------------------------------------------------
# 注册表契约
# --------------------------------------------------------------------------


def test_registry_has_p0_tools(registry):
    names = set(registry.names())
    expected = {
        "list_cases",
        "get_case",
        "list_modules",
        "run_cases",
        "get_last_result",
        "list_marks",
        "search_endpoints",
        "get_endpoint",
        "list_api_definitions",
        "query_db",
        "get_table_schema",
        "diagnose_failures",
        "get_coverage",
        # 知识层（P1）
        "find_field_mapping",
        "explain_endpoint",
        "search_knowledge",
        "knowledge_status",
        "rebuild_knowledge",
        # 写类（P2 前置：读 → 预览 → 落盘）
        "read_case_assertions",
        "propose_case_change",
        "apply_case_change",
        "set_case_status",
        "create_case",
        # 自愈闭环（P2）
        "plan_heal",
        "verify_heal",
        # 审计 / 成本（P3）
        "get_agent_audit",
        "get_agent_cost",
    }
    assert expected <= names, f"缺少工具: {expected - names}"


def test_every_tool_has_valid_schema(registry):
    for spec in registry.list_tools():
        schema = spec.params_schema
        assert schema.get("type") == "object", f"{spec.name} 的 schema 顶层必须是 object"
        assert isinstance(schema.get("properties", {}), dict)
        for required in schema.get("required", []):
            assert required in schema["properties"], (
                f"{spec.name} 声明了必填参数 {required} 但没在 properties 里定义"
            )
        assert spec.description, f"{spec.name} 没有描述"


def test_non_read_tools_are_exactly_the_expected_set(registry):
    """会动东西的工具必须是**已知的那几个** —— 新加的非只读工具要显式登记。

    这条断言防的是「悄悄加了个能写文件的工具」：写类工具要人审、
    要备份、要能回滚，多一个没被审查过的写入口，前面那些就白做了。
    """
    by_effect: dict[str, set[str]] = {}
    for spec in registry.list_tools():
        if spec.read_only:
            continue
        by_effect.setdefault(spec.side_effect, set()).add(spec.name)

    assert by_effect.get("execute") == {"run_cases", "verify_heal"}
    assert by_effect.get("write_case") == {
        "apply_case_change",
        "set_case_status",
        "create_case",
    }
    assert "write_real" not in by_effect, "不允许注册会改真实业务数据的工具"


def test_write_tools_are_not_marked_read_only(registry):
    """MCP 的 readOnlyHint 必须如实反映副作用 —— 客户端靠它决定要不要弹确认。"""
    for name in ("apply_case_change", "set_case_status", "create_case"):
        spec = registry.get_tool(name)
        assert not spec.read_only
        assert spec.to_mcp_tool()["annotations"]["readOnlyHint"] is False
        assert spec.to_mcp_tool()["annotations"]["destructiveHint"] is True


def test_propose_does_not_write(registry):
    """``propose_case_change`` 是纯预览，绝不能落盘。"""
    spec = registry.get_tool("propose_case_change")
    assert spec.read_only
    assert spec.to_mcp_tool()["annotations"]["readOnlyHint"] is True


def test_write_tools_are_ask_not_allow(registry):
    """写类工具必须进 ``ask``，绝不能进 ``allow``。

    这条防的是「顺手把权限加到 allow 里让流程跑快一点」——
    那样一来写文件就不弹确认了，写前备份、diff 复核这些设计等于没有。
    """
    settings_path = PROJECT_ROOT / ".claude" / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    perms = settings.get("permissions") or {}
    allowed = set(perms.get("allow") or [])
    asked = set(perms.get("ask") or [])

    for spec in registry.list_tools():
        perm = f"mcp__apitest__{spec.name}"
        if spec.read_only:
            assert perm not in asked, f"只读工具 {spec.name} 不需要弹确认"
            continue
        assert perm in asked, f"写类工具 {spec.name} 必须在 ask 里"
        assert perm not in allowed, f"写类工具 {spec.name} 绝不能进 allow"


def test_apply_case_change_exposes_weakening_escape_hatch(registry):
    """``allow_weakening`` 必须是 schema 里的显式参数，默认 false。

    有它才有"接口契约真的变了"的出路；没有它，模型会去直接改 Excel 绕开工具。
    """
    schema = registry.get_tool("apply_case_change").params_schema
    prop = schema["properties"]["allow_weakening"]
    assert prop["type"] == "boolean"
    assert prop.get("default") is False


def test_mcp_tool_shape(registry):
    tools = registry.to_mcp_tools()
    assert tools
    for tool in tools:
        assert set(tool) >= {"name", "description", "inputSchema"}
        assert "readOnlyHint" in tool["annotations"]


def test_unknown_tool_returns_error_not_raise(registry):
    result = registry.call("no_such_tool", {})
    assert result["ok"] is False
    assert "no_such_tool" in result["error"]["message"]
    assert "hint" in result["error"]


def test_bad_argument_returns_hint(registry):
    """参数名写错时，错误里要告诉模型正确的参数长什么样。"""
    result = registry.call("get_case", {"wrong_param": 1})
    assert result["ok"] is False
    assert "参数" in result["error"]["message"] or "参数" in result["error"].get("hint", "")


# --------------------------------------------------------------------------
# 用例工具
# --------------------------------------------------------------------------


def test_list_modules_returns_structure(registry):
    result = registry.call("list_modules", {})
    assert result["ok"] is True
    assert isinstance(result["projects"], list)
    # 项目里至少有一个用例（本仓库自带 demo_shop）
    assert result["total_cases"] >= 1


def test_list_cases_and_get_case_roundtrip(registry):
    listed = registry.call("list_cases", {"limit": 5})
    assert listed["ok"] is True
    if not listed["cases"]:
        pytest.skip("当前用例来源下没有用例")

    case_id = listed["cases"][0]["id"]
    detail = registry.call("get_case", {"case_id": case_id})
    assert detail["ok"] is True
    assert detail["case"]["id"] == case_id
    assert "step_list" in detail["case"]


def test_get_case_missing_id(registry):
    result = registry.call("get_case", {"case_id": "case_not_exist"})
    assert result["ok"] is False
    assert "hint" in result["error"]


def test_case_ids_are_stable(registry):
    """同样的用例调两次，ID 必须一样 —— 模型会拿旧 ID 再查。"""
    first = registry.call("list_cases", {"limit": 5})
    second = registry.call("list_cases", {"limit": 5})
    assert [c["id"] for c in first["cases"]] == [c["id"] for c in second["cases"]]


# --------------------------------------------------------------------------
# 执行工具的安全闸
# --------------------------------------------------------------------------


def test_run_cases_refuses_no_filter(registry):
    """不允许无筛选跑全量。"""
    result = registry.call("run_cases", {})
    assert result["ok"] is False
    assert "筛选条件" in result["error"]["message"]


def test_failures_only_include_current_run(tmp_path):
    """上一轮的 allure 失败文件不能被挂到本轮结果上。

    这是真踩过的坑：``--report none`` 时 allure-results 不会被清，
    读全部失败会得到"本次 1 条通过 + 20 条失败"这种自相矛盾的数据。
    """
    import json as _json
    import os
    import time as _time

    from ai.agent.tools.execution import _fresh_result_names, _is_fresh

    old = tmp_path / "old-result.json"
    old.write_text(_json.dumps({"name": "上一轮的失败"}), encoding="utf-8")
    # 把 mtime 改到 10 分钟前
    past = _time.time() - 600
    os.utime(old, (past, past))

    new = tmp_path / "new-result.json"
    new.write_text(_json.dumps({"name": "本轮的失败"}), encoding="utf-8")

    since = _time.time() - 60
    names = _fresh_result_names(tmp_path, since)

    class _FakeFailure:
        def __init__(self, case):
            self.case = case

    assert _is_fresh(_FakeFailure("本轮的失败"), names) is True
    assert _is_fresh(_FakeFailure("上一轮的失败"), names) is False
    assert _is_fresh(_FakeFailure(""), names) is False


# --------------------------------------------------------------------------
# 数据工具
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM tz_area",
        "UPDATE tz_area SET area_name='x'",
        "INSERT INTO tz_area VALUES (1)",
        "DROP TABLE tz_area",
        "TRUNCATE tz_area",
    ],
)
def test_query_db_rejects_writes(registry, sql):
    result = registry.call("query_db", {"sql": sql})
    assert result["ok"] is False
    assert "只读" in result["error"]["message"]


def test_query_db_rejects_multi_statement(registry):
    result = registry.call("query_db", {"sql": "SELECT 1; DELETE FROM tz_area"})
    assert result["ok"] is False


def test_query_db_rejects_comment_prefixed_write(registry):
    """注释开头骗不过白名单。"""
    result = registry.call("query_db", {"sql": "-- 查一下\n DELETE FROM tz_area"})
    assert result["ok"] is False


def test_get_table_schema_by_keyword(registry):
    result = registry.call("get_table_schema", {"keyword": "area", "alias": "mall4j"})
    assert result["ok"] is True
    assert result["tables"]


def test_get_table_schema_unknown_table(registry):
    result = registry.call("get_table_schema", {"table": "tz_not_exists", "alias": "mall4j"})
    assert result["ok"] is False


# --------------------------------------------------------------------------
# 接口文档工具
# --------------------------------------------------------------------------


def test_search_endpoints(registry):
    result = registry.call("search_endpoints", {"limit": 5})
    assert result["ok"] is True
    assert result["endpoints"]
    for item in result["endpoints"]:
        assert item["method"] and item["path"]


def test_get_endpoint_full_definition(registry):
    found = registry.call("search_endpoints", {"query": "hot", "limit": 3})
    if not found["endpoints"]:
        pytest.skip("快照里没有匹配接口")
    target = found["endpoints"][0]
    detail = registry.call(
        "get_endpoint", {"path": target["path"], "method": target["method"]}
    )
    assert detail["ok"] is True
    assert detail["endpoint"]["path"] == target["path"]


def test_get_endpoint_wrong_method_suggests_alternatives(registry):
    found = registry.call("search_endpoints", {"limit": 1})
    target = found["endpoints"][0]
    wrong = "DELETE" if target["method"] != "DELETE" else "PATCH"
    result = registry.call("get_endpoint", {"path": target["path"], "method": wrong})
    # 有的路径支持全部方法，不强制失败；失败时必须给出可选项
    if not result["ok"]:
        assert "支持" in result["error"].get("hint", "")


# --------------------------------------------------------------------------
# MCP 协议层
# --------------------------------------------------------------------------


def test_mcp_initialize():
    from ai.mcp_server.server import handle_request

    result = handle_request("initialize", {"protocolVersion": "2024-11-05"})
    assert result["protocolVersion"] == "2024-11-05"
    assert "tools" in result["capabilities"]
    assert result["serverInfo"]["name"] == "apitest-mcp"


def test_mcp_tools_list_matches_registry():
    from ai.agent.tools import get_registry
    from ai.mcp_server.server import handle_request

    result = handle_request("tools/list", {})
    names = {t["name"] for t in result["tools"]}
    assert names == set(get_registry().names())


def test_mcp_tools_call_returns_structured_content():
    from ai.mcp_server.server import handle_request

    result = handle_request("tools/call", {"name": "list_modules", "arguments": {}})
    assert result["isError"] is False
    assert result["structuredContent"]["ok"] is True
    # 文本内容必须能解析回 JSON（模型按 JSON 解析更稳）
    assert json.loads(result["content"][0]["text"])["ok"] is True


def test_mcp_tools_call_error_sets_is_error():
    from ai.mcp_server.server import handle_request

    result = handle_request("tools/call", {"name": "get_case", "arguments": {}})
    assert result["isError"] is True


def test_mcp_unknown_method():
    from ai.mcp_server.protocol import RPCError
    from ai.mcp_server.server import handle_request

    with pytest.raises(RPCError) as exc:
        handle_request("no/such/method", {})
    assert "不支持" in str(exc.value)


def test_mcp_resources_list():
    from ai.mcp_server.server import handle_request

    result = handle_request("resources/list", {})
    uris = {r["uri"] for r in result["resources"]}
    assert "apitest://openapi" in uris
    assert "apitest://report/latest" in uris


def test_mcp_resource_read_openapi():
    from ai.mcp_server.server import handle_request

    result = handle_request("resources/read", {"uri": "apitest://openapi"})
    content = result["contents"][0]
    assert content["mimeType"] == "application/json"
    assert "paths" in json.loads(content["text"])


def test_mcp_resource_read_unknown_uri():
    from ai.mcp_server.protocol import RPCError
    from ai.mcp_server.server import handle_request

    with pytest.raises(RPCError):
        handle_request("resources/read", {"uri": "apitest://nope"})


def test_mcp_prompts_list_and_get():
    from ai.mcp_server.protocol import RPCError
    from ai.mcp_server.server import handle_request

    listed = handle_request("prompts/list", {})
    names = {p["name"] for p in listed["prompts"]}
    assert {
        "run_and_report",
        "write_case",
        "heal_failure",
        "sync_docs",
        "review_agent_run",
    } <= names

    prompt = handle_request(
        "prompts/get", {"name": "run_and_report", "arguments": {"project": "mall4j"}}
    )
    text = prompt["messages"][0]["content"]["text"]
    assert "mall4j" in text
    # 可选参数没给时也不能残留占位符 —— 模型会把它当变量名照抄
    assert "{{" not in text

    with pytest.raises(RPCError):
        handle_request("prompts/get", {"name": "run_and_report", "arguments": {}})


# --------------------------------------------------------------------------
# 端到端：真的走一遍 stdio 循环
# --------------------------------------------------------------------------


def test_stdio_loop_end_to_end():
    """喂几行 JSON 进去，验证回包顺序和格式。"""
    import io

    from ai.mcp_server.server import serve

    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "no/such/method", "params": {}},
    ]
    stdin = io.StringIO("\n".join(json.dumps(r) for r in requests) + "\n")
    stdout = io.StringIO()

    assert serve(stdin, stdout) == 0

    lines = [line for line in stdout.getvalue().splitlines() if line.strip()]
    # 通知不回包 -> 3 条响应
    assert len(lines) == 3

    responses = [json.loads(line) for line in lines]
    assert [r["id"] for r in responses] == [1, 2, 3]
    assert responses[0]["result"]["serverInfo"]["name"] == "apitest-mcp"
    assert responses[1]["result"]["tools"]
    assert responses[2]["error"]["code"] == -32601
