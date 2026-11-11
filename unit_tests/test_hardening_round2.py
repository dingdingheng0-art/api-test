"""状态文件 / 缓存 / 失败明细的加固测试。

覆盖这一轮修掉的问题：

- 预算和自愈计数在写盘失败时不能清零（否则闸门失效）
- 预算文件 / 审计日志里坏掉的字段不能让工具整个挂掉（闸门变成"工具不可用"）
- 图谱清空后 ``built_at`` 必须一起失效
- 用例文件缓存要能感知文件修改，且不能把调用方的改动带进下一次读取
- ``report=none`` 时从 junit.xml 提取失败明细
- ``diagnose_failures`` 在"本轮没有 allure 结果"时不能返回历史失败
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# 状态文件：写盘失败时仍在内存里生效
# ---------------------------------------------------------------------------


def test_run_budget_keeps_counting_when_disk_write_fails(monkeypatch, tmp_path):
    from ai.agent.tools import run_budget

    monkeypatch.setattr(run_budget, "_path", lambda: tmp_path / "run_budget.json")
    monkeypatch.setattr(run_budget, "_key", lambda: "session:test")
    monkeypatch.setattr(run_budget, "_MEMORY", {})

    def _boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(run_budget, "atomic_write_json", _boom)

    run_budget.add(10)
    run_budget.add(5)

    # 没写进磁盘，但本进程内必须仍然累计
    assert run_budget.spent() == 15


def test_heal_state_keeps_counting_when_disk_write_fails(monkeypatch, tmp_path):
    from ai.agent.tools import heal_state

    monkeypatch.setattr(heal_state, "_path", lambda: tmp_path / "heal_state.json")
    monkeypatch.setattr(heal_state, "_MEMORY", {})

    def _boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(heal_state, "atomic_write_json", _boom)

    assert heal_state.record_failed_round("API-1") == 1
    assert heal_state.record_failed_round("API-1") == 2
    assert heal_state.failed_rounds("API-1") == 2
    # 快照里也要看得到，否则排查时以为一次都没记
    assert "API-1" in heal_state.snapshot()


def test_heal_state_reset_survives_disk_write_failure(monkeypatch, tmp_path):
    """验证通过必须清零。写盘失败时只把内存记成 0 不够 ——
    ``failed_rounds`` 取 ``max(磁盘, 内存)``，磁盘上的旧计数会把刚跑通的
    用例顶回「两轮已用尽」，智能体此后一直被拒。"""
    from ai.agent.tools import heal_state

    state = tmp_path / "heal_state.json"
    state.write_text(json.dumps({"API-1": {"failed_rounds": 2}}), encoding="utf-8")
    monkeypatch.setattr(heal_state, "_path", lambda: state)
    monkeypatch.setattr(heal_state, "_MEMORY", {})

    def _boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(heal_state, "atomic_write_json", _boom)

    assert heal_state.failed_rounds("API-1") == 2
    heal_state.reset("API-1")
    assert heal_state.failed_rounds("API-1") == 0
    # 快照里不能再露出磁盘上的旧计数
    assert "API-1" not in heal_state.snapshot()
    # 清零之后的第一次失败重新从 1 开始计，而不是 3
    assert heal_state.record_failed_round("API-1") == 1


def test_run_budget_survives_corrupt_bucket(monkeypatch, tmp_path):
    """``run_budget.json`` 是运行态文件，桶可能被手改坏。

    ``spent()`` / ``add()`` 里的 ``.get`` 在 try 之外 —— 桶写成字符串时
    抛的是 AttributeError，``run_cases`` 直接不可用。预算闸不该把工具弄挂。
    """
    from ai.agent.tools import run_budget

    state = tmp_path / "run_budget.json"
    state.write_text(
        json.dumps(
            {
                "session:test": "garbage",  # 桶整个写成字符串
                "old": {"cases": "abc"},  # 计数不是数字
                "worse": {"updated_at": "昨天"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(run_budget, "_path", lambda: state)
    monkeypatch.setattr(run_budget, "_key", lambda: "session:test")
    monkeypatch.setattr(run_budget, "_MEMORY", {})

    assert run_budget.spent() == 0
    assert run_budget.add(3) == 3
    assert run_budget.spent() == 3


def test_run_budget_prune_tolerates_corrupt_buckets(monkeypatch, tmp_path):
    """清理老桶时不能因为某个桶不是 dict / ``updated_at`` 不是数字就抛异常。"""
    from ai.agent.tools import run_budget

    state = tmp_path / "run_budget.json"
    buckets: dict = {
        f"old{i}": {"cases": 1, "updated_at": i} for i in range(11)
    }
    buckets["broken"] = ["not", "a", "dict"]
    buckets["garbled"] = {"updated_at": "abc"}
    state.write_text(json.dumps(buckets), encoding="utf-8")

    monkeypatch.setattr(run_budget, "_path", lambda: state)
    monkeypatch.setattr(run_budget, "_key", lambda: "session:test")
    monkeypatch.setattr(run_budget, "_MEMORY", {})

    assert run_budget.add(1) == 1

    saved = json.loads(state.read_text(encoding="utf-8"))
    assert len(saved) == run_budget.MAX_BUCKETS
    # 当前会话桶必须留下，坏桶按"最旧"清掉
    assert "session:test" in saved
    assert "broken" not in saved and "garbled" not in saved


def test_audit_recent_ignores_bad_epoch(monkeypatch, tmp_path):
    from ai.agent import audit_log
    from utils import paths

    monkeypatch.setattr(paths, "CACHE_DIR", tmp_path)
    (tmp_path / "agent_audit.jsonl").write_text(
        json.dumps({"tool": "run_cases", "epoch": "昨天", "ok": True}) + "\n"
        + json.dumps({"tool": "run_cases", "epoch": 10, "ok": True}) + "\n",
        encoding="utf-8",
    )

    # 坏 epoch 按 0 处理：不抛异常，只是被时间窗过滤掉
    rows = audit_log.recent(since_epoch=1)
    assert [row["tool"] for row in rows] == ["run_cases"]
    # limit 传成非数字时退回默认值，而不是 ValueError
    assert len(audit_log.recent(limit="abc")) == 2


def test_audit_summarize_survives_bad_numbers_and_tokens(monkeypatch, tmp_path):
    from ai.agent import audit_log
    from utils import paths

    monkeypatch.setattr(paths, "CACHE_DIR", tmp_path)
    (tmp_path / "agent_audit.jsonl").write_text(
        json.dumps(
            {
                "tool": "run_cases",
                "ok": True,
                "duration_ms": "很久",
                "tokens": ["不是", "对象"],
            }
        )
        + "\n"
        + json.dumps(
            {
                "tool": "run_cases",
                "ok": True,
                "duration_ms": 2.0,
                "tokens": {"prompt": "abc", "completion": 3, "total": 5},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    summary = audit_log.summarize(limit=10)

    assert summary["window"] == 2
    assert summary["total_duration_ms"] == 2.0
    assert summary["tokens"]["completion"] == 3
    assert summary["tokens"]["prompt"] == 0  # 坏值不当成功处理


def test_agent_audit_tools_tolerate_bad_paging_args(monkeypatch, tmp_path):
    """``limit`` / ``since_minutes`` 由客户端传，schema 层不校验类型。

    传 ``"abc"`` 时原来的 ``int(limit or 50)`` 直接抛 ``ValueError``，
    排障工具反而在最需要它的时候不可用。坏值应退回默认值。
    """
    from ai.agent.tools import audit
    from utils import paths

    monkeypatch.setattr(paths, "CACHE_DIR", tmp_path)
    (tmp_path / "agent_audit.jsonl").write_text(
        json.dumps({"tool": "run_cases", "ok": True, "epoch": 10}) + "\n"
        + json.dumps({"tool": "get_case", "ok": True, "epoch": 11}) + "\n",
        encoding="utf-8",
    )

    payload = audit.get_agent_audit(limit="abc")
    assert payload["ok"] is True
    assert payload["total"] == 2

    cost = audit.get_agent_cost(limit="abc", since_minutes="很久")
    assert cost["ok"] is True
    assert cost["window"] == 2


# ---------------------------------------------------------------------------
# 工具入参：schema 层不做类型校验，坏值不能变成工具失败
# ---------------------------------------------------------------------------


def test_params_safe_int_and_clamp():
    from ai.agent.tools._params import clamp_int, safe_int

    assert safe_int("abc", 50) == 50
    assert safe_int(None, 50) == 50
    assert safe_int("", 50) == 50
    assert safe_int(0, 50) == 50
    assert safe_int("12", 50) == 12

    assert clamp_int("abc", 50, 1, 500) == 50
    assert clamp_int(999, 50, 1, 500) == 500
    assert clamp_int(-3, 50, 1, 500) == 1


def test_list_cases_tolerates_bad_limit():
    from ai.agent.tools import cases

    payload = cases.list_cases(limit="abc")
    assert payload["ok"] is True


def test_search_endpoints_tolerates_bad_limit():
    from ai.agent.tools import endpoints

    payload = endpoints.search_endpoints(limit="abc")
    assert payload["ok"] is True


def test_search_knowledge_tolerates_bad_limit_and_kind(monkeypatch):
    from ai.agent.tools import knowledge

    captured: dict = {}

    class _Retriever:
        def __init__(self, _store):
            pass

        def recall(self, query, kinds=(), limit=10):
            captured.update(limit=limit, kinds=kinds)
            return []

    monkeypatch.setattr(knowledge, "_ensure_graph", lambda: object())
    monkeypatch.setattr(knowledge, "_stale_reason", lambda: "")
    monkeypatch.setattr("ai.knowledge.retriever.Retriever", _Retriever)

    payload = knowledge.search_knowledge("area", kind=5, limit="abc")

    assert payload["ok"] is True
    assert captured == {"limit": 10, "kinds": ("5",)}


def _dummy_tool_registry():
    """一个不碰文件 / 网络的小工具，专门验证 registry 的参数校验。"""
    from ai.agent.tools.registry import ToolRegistry, ToolSpec

    captured: dict = {}

    def handler(limit=10, flag=False, name="", items=None, mode="a"):
        captured.update(
            limit=limit, flag=flag, name=name, items=items, mode=mode
        )
        return {"ok": True, "captured": dict(captured)}

    spec = ToolSpec(
        name="dummy",
        description="测试用",
        params_schema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 10},
                "flag": {"type": "boolean", "default": False},
                "name": {"type": "string", "default": ""},
                "items": {"type": "array", "items": {"type": "object"}},
                "mode": {"type": "string", "enum": ["a", "b"], "default": "a"},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        handler=handler,
    )
    registry = ToolRegistry()
    registry.register(spec)
    return registry, captured


def test_registry_coerces_json_friendly_arguments():
    registry, captured = _dummy_tool_registry()

    result = registry.call(
        "dummy", {"name": 123, "limit": "5", "flag": "true", "items": [{"a": 1}]}
    )

    assert result["ok"] is True
    assert captured == {
        "limit": 5,
        "flag": True,
        "name": "123",
        "items": [{"a": 1}],
        "mode": "a",
    }


def test_registry_rejects_wrong_argument_types():
    registry, _ = _dummy_tool_registry()

    bad_int = registry.call("dummy", {"name": "x", "limit": "abc"})
    assert bad_int["ok"] is False
    assert "limit" in bad_int["error"]["message"]
    assert "整数" in bad_int["error"]["message"]

    bad_items = registry.call("dummy", {"name": "x", "items": ["不是对象"]})
    assert bad_items["ok"] is False
    assert "items[0]" in bad_items["error"]["message"]


def test_registry_rejects_missing_unknown_and_bad_enum():
    registry, _ = _dummy_tool_registry()

    missing = registry.call("dummy", {})
    assert missing["ok"] is False
    assert "必填" in missing["error"]["message"]

    unknown = registry.call("dummy", {"name": "x", "nope": 1})
    assert unknown["ok"] is False
    assert "nope" in unknown["error"]["message"]

    bad_enum = registry.call("dummy", {"name": "x", "mode": "c"})
    assert bad_enum["ok"] is False
    assert "只能是" in bad_enum["error"]["message"]


def test_atomic_write_json_replaces_file(monkeypatch, tmp_path):
    from utils.infra.json_store import atomic_write_json

    target = tmp_path / "state.json"
    atomic_write_json(target, {"a": 1})
    atomic_write_json(target, {"a": 2})

    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 2}
    # 临时文件不能留在目录里
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


# ---------------------------------------------------------------------------
# 报告保留：旧 allure 报告目录不能无限堆积
# ---------------------------------------------------------------------------


def test_prune_old_reports_keeps_latest(monkeypatch, tmp_path):
    from framework import report_generator

    monkeypatch.setattr(report_generator, "REPORTS_ROOT", tmp_path)
    for stamp in ("20260101_000000", "20260102_000000", "20260103_000000"):
        (tmp_path / f"allure_{stamp}").mkdir()
    (tmp_path / "allure-results").mkdir()
    (tmp_path / "allure_latest.html").write_text("x", encoding="utf-8")

    removed = report_generator.prune_old_reports(keep=2)

    remaining = sorted(p.name for p in tmp_path.iterdir() if p.is_dir())
    assert removed == 1
    assert remaining == ["allure-results", "allure_20260102_000000", "allure_20260103_000000"]


# ---------------------------------------------------------------------------
# 图谱：清空后元信息一起失效
# ---------------------------------------------------------------------------


def test_knowledge_store_clear_drops_built_at():
    from ai.knowledge.store import KnowledgeStore

    store = KnowledgeStore(":memory:")
    store.add_node("case:1", "case", "查数据")
    store.set_meta("built_at", 123.0)
    store.set_meta("stats", {"nodes": 1})

    store.clear()

    assert store.get_meta("built_at") is None
    assert store.get_meta("stats") is None


# ---------------------------------------------------------------------------
# 用例文件缓存
# ---------------------------------------------------------------------------


def _write_case(path: Path, name: str, status: str = "draft") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "name: %s\nstatus: %s\nsteps:\n  - id: s1\n    method: GET\n    path: /ping\n"
        % (name, status),
        encoding="utf-8",
    )


def test_load_cache_invalidates_when_file_changes(tmp_path):
    from testcases import case_loader

    case_loader.clear_load_cache()

    root = tmp_path / "cases"
    path = root / "demo" / "mod" / "ping.yaml"
    _write_case(path, "第一版")

    first = case_loader.load_cases_from_file(path, root=root)
    assert first[0]["testcase"]["name"] == "第一版"

    # 改写内容并显式推一下 mtime（有些文件系统时间精度不够）
    _write_case(path, "第二版")
    import os

    stat = path.stat()
    os.utime(path, (stat.st_atime, stat.st_mtime + 5))

    second = case_loader.load_cases_from_file(path, root=root)
    assert second[0]["testcase"]["name"] == "第二版"


def test_load_cache_returns_isolated_copies(tmp_path):
    from testcases import case_loader

    case_loader.clear_load_cache()

    root = tmp_path / "cases"
    path = root / "demo" / "mod" / "ping.yaml"
    _write_case(path, "原名")

    first = case_loader.load_cases_from_file(path, root=root)
    first[0]["testcase"]["name"] = "被调用方改掉了"

    second = case_loader.load_cases_from_file(path, root=root)
    assert second[0]["testcase"]["name"] == "原名"


# ---------------------------------------------------------------------------
# 失败明细：report=none 走 junit.xml
# ---------------------------------------------------------------------------


def test_junit_failures_are_extracted(monkeypatch, tmp_path):
    from ai.agent.tools import execution
    from utils import paths

    monkeypatch.setattr(paths, "REPORTS_ROOT", tmp_path)
    (tmp_path / "junit.xml").write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite>
  <testcase classname="testcases.test_cases" name="test_api_case[查询地区-API-168]">
    <failure message="assert False" type="AssertionError">traceback...</failure>
  </testcase>
  <testcase classname="testcases.test_cases" name="test_api_case[通过-API-169]" />
</testsuite></testsuites>""",
        encoding="utf-8",
    )

    failures = execution._read_junit_failures()

    assert len(failures) == 1
    assert failures[0]["case"] == "查询地区-API-168"
    assert failures[0]["error_type"] == "AssertionError"
    assert "assert False" in failures[0]["message"]


@pytest.mark.parametrize("payload", ["[]", "124", '"text"', "null"])
def test_read_result_json_ignores_non_dict(monkeypatch, tmp_path, payload):
    """合法 JSON 不等于合法结果：``[]`` / ``124`` 读进来会让 run_cases 里
    到处 ``.get`` 抛 TypeError —— 当成「没有结果」处理。"""
    from ai.agent.tools import execution
    from utils import paths

    monkeypatch.setattr(paths, "REPORTS_ROOT", tmp_path)
    (tmp_path / "result.json").write_text(payload, encoding="utf-8")

    assert execution._read_result_json() == {}


def test_get_last_result_tolerates_missing_result_file(monkeypatch, tmp_path):
    """读结果和取文件时间是两次独立操作：中间被清掉时
    ``stat()`` 会抛 FileNotFoundError，只读工具不该因此报错。"""
    from ai.agent.tools import execution
    from utils import paths

    monkeypatch.setattr(paths, "REPORTS_ROOT", tmp_path)  # 目录里没有 result.json
    monkeypatch.setattr(paths, "ALLURE_RESULTS", tmp_path / "no_allure")
    monkeypatch.setattr(execution, "_read_result_json", lambda: {"total": 1, "passed": 1})
    monkeypatch.setattr(execution, "_read_failures", lambda: [])

    payload = execution.get_last_result()

    assert payload["result"]["total"] == 1
    assert payload["result_file_age_seconds"] is None


def test_read_result_json_returns_dict(monkeypatch, tmp_path):
    from ai.agent.tools import execution
    from utils import paths

    monkeypatch.setattr(paths, "REPORTS_ROOT", tmp_path)
    (tmp_path / "result.json").write_text(
        json.dumps({"total": 3, "passed": 3}), encoding="utf-8"
    )

    assert execution._read_result_json()["total"] == 3


def test_diagnose_drops_history_when_no_fresh_allure(monkeypatch, tmp_path):
    from ai.agent.tools import diagnostics
    from utils import paths

    allure_dir = tmp_path / "allure"
    allure_dir.mkdir()
    (tmp_path / "result.json").write_text(
        json.dumps({"failed": 1}), encoding="utf-8"
    )
    monkeypatch.setattr(paths, "ALLURE_RESULTS", allure_dir)
    monkeypatch.setattr(paths, "REPORTS_ROOT", tmp_path)

    class _Failure:
        case = "上一轮的失败"
        error_type = "AssertionError"
        message = "old"

    monkeypatch.setattr(
        "ai.capabilities.diagnosis.collect_failures",
        lambda **_kwargs: [_Failure()],
    )
    # 本轮没有任何 allure 结果 —— 不能把历史失败当成这一轮的
    monkeypatch.setattr(diagnostics, "_fresh_cases", lambda *_a, **_k: set())

    result = diagnostics.diagnose_failures()

    assert result["total"] == 0
    assert result["diagnoses"] == []
    # 要告诉调用方"有失败但拿不到明细"，而不是说"没有失败"
    assert "1 条失败" in result["hint"]


# ---------------------------------------------------------------------------
# 合法 JSON 但结构不对：不能让整片工具 / 整轮归因崩掉
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("payload", ["[]", '"text"', "124", "null", "true"])
def test_load_openapi_rejects_non_object_root(monkeypatch, tmp_path, payload):
    """OpenAPI 快照被同步脚本写坏时，要给清楚的 ToolError，而不是让后面
    每个工具在 ``doc.get("paths")`` 上抛 AttributeError。"""
    from ai.agent.tools import endpoints

    monkeypatch.setattr(endpoints.paths, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(endpoints, "_OPENAPI_CACHE", None)
    apidoc = tmp_path / "apidoc"
    apidoc.mkdir()
    (apidoc / "openapi.json").write_text(payload, encoding="utf-8")

    with pytest.raises(endpoints.ToolError) as excinfo:
        endpoints.load_openapi()
    assert "结构不对" in str(excinfo.value)


def test_iter_operations_tolerates_non_dict_paths():
    from ai.agent.tools import endpoints

    assert list(endpoints._iter_operations({"paths": [1, 2, 3]})) == []
    assert list(endpoints._iter_operations({"paths": "nope"})) == []


@pytest.mark.parametrize("payload", ["[]", "124", '"text"', "null"])
def test_failed_count_ignores_non_dict(tmp_path, payload):
    from ai.agent.tools import diagnostics

    result_file = tmp_path / "result.json"
    result_file.write_text(payload, encoding="utf-8")

    assert diagnostics._failed_count(result_file) == 0


def test_fresh_cases_skips_non_dict_result(monkeypatch, tmp_path):
    from ai.agent.tools import diagnostics

    allure_dir = tmp_path / "allure"
    allure_dir.mkdir()
    (allure_dir / "a-result.json").write_text("[]", encoding="utf-8")
    (allure_dir / "b-result.json").write_text(
        json.dumps({"name": "正常用例"}), encoding="utf-8"
    )

    assert diagnostics._fresh_cases(allure_dir, cutoff=0) == {"正常用例"}


def test_fresh_result_names_skips_non_dict_result(tmp_path):
    from ai.agent.tools import execution

    (tmp_path / "a-result.json").write_text('"text"', encoding="utf-8")
    (tmp_path / "b-result.json").write_text(
        json.dumps({"name": "正常用例"}), encoding="utf-8"
    )

    assert execution._fresh_result_names(tmp_path, since=0) == {"正常用例"}


@pytest.mark.parametrize("payload", ["[]", "5", '"text"'])
def test_schema_snapshot_non_dict_is_empty(tmp_path, monkeypatch, payload):
    from ai.agent.tools import data

    snapshot = tmp_path / "mysql_demo.json"
    snapshot.write_text(payload, encoding="utf-8")
    monkeypatch.setattr(data, "_SCHEMA_CACHE", {})

    assert data._load_schema_file(snapshot) == {}


def test_table_schema_rejects_non_list_tables(monkeypatch, tmp_path):
    """``tables`` 被写成对象时，遍历拿到的是字符串键 —— ``item.get`` 会崩。"""
    from ai.agent.tools import data

    monkeypatch.setattr(data, "_schema_files", lambda: [tmp_path / "mysql_demo.json"])
    monkeypatch.setattr(data, "_load_schema_file", lambda _p: {"tables": {"a": 1}})

    with pytest.raises(data.ToolError) as excinfo:
        data.get_table_schema()
    assert "不是列表" in str(excinfo.value)


def test_table_schema_skips_non_dict_table_entries(monkeypatch, tmp_path):
    from ai.agent.tools import data

    monkeypatch.setattr(data, "_schema_files", lambda: [tmp_path / "mysql_demo.json"])
    monkeypatch.setattr(
        data,
        "_load_schema_file",
        lambda _p: {"tables": ["坏表", {"name": "tz_area", "columns": ["坏列"]}]},
    )

    result = data.get_table_schema(table="tz_area")
    assert result["table"] == "tz_area"
    assert result["columns"] == []


@pytest.mark.parametrize("payload", ["[]", "5", '"text"'])
def test_restore_backup_rejects_non_dict_manifest(monkeypatch, tmp_path, payload):
    from ai.agent.tools import case_io

    backup = tmp_path / "backup"
    (backup / "agent_1").mkdir(parents=True)
    (backup / "agent_1" / "case.yaml").write_text("name: x\n", encoding="utf-8")
    (backup / "agent_1" / "manifest.json").write_text(payload, encoding="utf-8")

    with pytest.raises(case_io.ToolError) as excinfo:
        case_io.restore_from_backup(backup / "agent_1")
    assert "manifest.json" in str(excinfo.value)


@pytest.mark.parametrize("payload", ["[]", "1", '"text"'])
def test_knowledge_builder_skips_non_dict_openapi(monkeypatch, tmp_path, payload):
    from ai.knowledge import builder
    from utils import paths

    (tmp_path / "apidoc").mkdir()
    (tmp_path / "apidoc" / "openapi.json").write_text(payload, encoding="utf-8")
    monkeypatch.setattr(paths, "CACHE_DIR", tmp_path)

    assert builder._load_openapi() == {}


def test_knowledge_builder_skips_non_dict_schema(monkeypatch, tmp_path):
    from ai.knowledge import builder
    from utils import paths

    schema_dir = tmp_path / "schema"
    schema_dir.mkdir()
    (schema_dir / "mysql_demo.json").write_text("[]", encoding="utf-8")
    (schema_dir / "mysql_ok.json").write_text(
        json.dumps({"tables": [{"name": "tz_area"}]}), encoding="utf-8"
    )
    monkeypatch.setattr(paths, "CACHE_DIR", tmp_path)

    assert set(builder._load_tables()) == {"tz_area"}


def test_audit_recent_skips_non_dict_lines(monkeypatch, tmp_path):
    from ai.agent import audit_log
    from utils import paths

    monkeypatch.setattr(paths, "CACHE_DIR", tmp_path)
    (tmp_path / "agent_audit.jsonl").write_text(
        "[]\n5\n" + json.dumps({"tool": "run_cases", "ok": True}) + "\n",
        encoding="utf-8",
    )

    rows = audit_log.recent()

    assert [row["tool"] for row in rows] == ["run_cases"]


# --------------------------------------------------------------------------
# 智能体循环：模型返回的结构不能假定（round 4）
# --------------------------------------------------------------------------


class _ReplyClient:
    """按顺序返回预设回复的假 LLM 客户端。"""

    available = True

    def __init__(self, replies):
        self._replies = list(replies)

    def chat_with_tools(self, _messages, _tools, temperature=None):
        if self._replies:
            return self._replies.pop(0)
        return {"message": {"content": "", "tool_calls": []}}


def test_loop_tolerates_malformed_reply_fields():
    from ai.agent.loop import run_task
    from ai.agent.tools import reset_registry

    client = _ReplyClient(
        [
            {
                "usage": "not-a-dict",
                "message": {
                    "content": "",
                    "tool_calls": [
                        "junk",  # 不是对象
                        {"function": "not-a-dict"},  # function 不是对象
                        {
                            "function": {
                                "name": "list_modules",
                                "arguments": "not json",  # 参数不是合法 JSON
                            }
                        },
                    ],
                },
            },
            {"message": "完成了", "usage": {"prompt_tokens": "abc", "completion_tokens": None}},
        ]
    )

    result = run_task("列模块", registry=reset_registry(), client=client)

    assert result.stopped == "finished"
    assert result.answer == "完成了"
    assert [s.tool for s in result.steps] == ["list_modules"]
    assert result.steps[0].ok is True
    assert result.steps[0].arguments == {}
    assert result.tokens["total"] == 0


def test_loop_non_dict_reply_is_llm_error_not_silent_finish():
    from ai.agent.loop import run_task
    from ai.agent.tools import reset_registry

    result = run_task("x", registry=reset_registry(), client=_ReplyClient(["not a dict"]))

    assert result.stopped == "llm_error"
    assert "str" in result.error
    assert result.steps == []


def test_loop_tolerates_bad_budget_and_step_args():
    from ai.agent.loop import run_task
    from ai.agent.tools import reset_registry

    call = {"id": "c1", "function": {"name": "list_modules", "arguments": "{}"}}
    client = _ReplyClient(
        [
            {"message": {"content": "", "tool_calls": [call]}},
            {"message": {"content": "好了", "tool_calls": []}},
        ]
    )

    result = run_task(
        "x", max_steps="abc", max_tool_calls="abc", registry=reset_registry(), client=client
    )

    assert result.stopped == "finished"
    assert result.tool_calls == 1


def test_loop_survives_registry_that_raises():
    from ai.agent.loop import run_task

    class _BoomRegistry:
        def list_tools(self):
            return []

        def call(self, name, arguments):
            raise RuntimeError("registry 炸了")

    call = {"id": "c1", "function": {"name": "list_modules", "arguments": "{}"}}
    client = _ReplyClient(
        [
            {"message": {"content": "", "tool_calls": [call]}},
            {"message": {"content": "好了", "tool_calls": []}},
        ]
    )

    result = run_task("x", registry=_BoomRegistry(), client=client)

    assert result.stopped == "finished"
    assert result.steps[0].ok is False
    assert "registry 炸了" in result.steps[0].error


def test_tool_whitelist_fails_closed_and_normalizes():
    from ai.agent.loop import _tool_definitions, _tool_whitelist
    from ai.agent.tools import reset_registry

    assert _tool_whitelist(None) is None
    assert _tool_whitelist("list_cases, get_case") == {"list_cases", "get_case"}
    assert _tool_whitelist(["list_cases", "", None]) == {"list_cases"}
    assert _tool_whitelist([]) == set()
    assert _tool_whitelist(5) == set()

    registry = reset_registry()
    assert _tool_definitions(registry, []) == []


def test_step_to_dict_trims_huge_arguments():
    from ai.agent.loop import Step

    step = Step(
        index=1,
        tool="create_case",
        arguments={"blob": "x" * 5000, "items": list(range(100))},
    )
    data = step.to_dict(keep_result=False)

    assert len(data["arguments"]["blob"]) <= 401
    assert len(data["arguments"]["items"]) == 20


def test_evals_runner_tolerates_non_dict_expect():
    from ai.agent import audit_log
    from ai.evals import runner

    client = _ReplyClient([{"message": {"content": "看完了", "tool_calls": []}}])
    try:
        result = runner.run_task(
            {"id": "T-1", "task": "x", "expect": ["not-a-dict"]},
            client=client,
        )
    finally:
        # run_task 会把自己的 origin 设成 eval，别让它漏到后面的用例
        audit_log.set_origin("agent")

    assert result.error_kind != "runner_error"
    assert result.loop.get("stopped") == "finished"


# --------------------------------------------------------------------------
# 生成产物 / LLM 配置与响应：结构不能假定（round 5）
# --------------------------------------------------------------------------

#: 读配置前要清掉的环境变量，否则测试结果取决于开发机的 .env
_AI_ENV_VARS = (
    "AI_BASE_URL",
    "AI_API_KEY",
    "AI_MODEL",
    "OPENAI_BASE_URL",
    "OPENAI_API_KEY",
    "OPENAI_MODEL",
    "DASHSCOPE_API_KEY",
)


def test_case_gen_compact_helpers_tolerate_bad_shapes():
    from ai.capabilities.case_gen import _compact_asserts, _compact_case

    single = {"type": "equal", "field": "code", "expected": "00000"}
    # 单条断言写成对象时不能按 dict 切片（TypeError）
    assert _compact_asserts(single) == [single]
    assert _compact_asserts("not-a-list") == []

    case = {
        "name": "示例",
        "steps": {"method": "GET", "path": "/x", "assert": single},
        "marks": "negative,p0",
    }
    compact = _compact_case(object(), case)

    assert compact["name"] == "示例"
    assert compact["marks"] == ["negative", "p0"]  # 不再拆成单个字符
    assert compact["steps"][0]["path"] == "/x"
    assert compact["steps"][0]["assert"] == [single]


def test_case_gen_marks_string_drives_priority():
    from ai.capabilities.case_gen import _normalize_ai_case, _priority_of

    assert _priority_of({"marks": "p2,negative"}) == "P2"

    case = _normalize_ai_case(
        {
            "name": "查询区域（缺少必填参数）",
            "marks": "negative,p0",
            "steps": [{"method": "GET", "path": "/admin/area/listByPid"}],
        },
        default_module="ai_area",
    )

    assert case is not None
    tc = case["testcase"]
    assert tc["priority"] == "P0"
    assert "negative" in tc["marks"]
    assert "p0" in tc["marks"]


def test_case_gen_probe_index_tolerates_bad_shapes():
    from ai.capabilities.case_gen import _probe_index

    # 整个探针结果类型不对：退回「没有实测证据」，不能让生成崩
    assert _probe_index(["nope"]) == {}

    # 单个维度的结果写成对象而不是数组，也要能索引
    index = _probe_index(
        {
            ("GET", "/admin/area/listByPid"): {
                "维度": "C1",
                "状态码": 401,
                "有success字段": True,
            }
        }
    )

    record = index[("GET", "/admin/area/listByPid")]["C1"]
    assert record["status_code"] == 401
    assert record["rejected"] is True


def test_case_gen_append_assert_replaces_non_list():
    from ai.capabilities.case_gen import _append_assert

    step = {"assert": {"type": "equal", "field": "code", "expected": "00000"}}
    _append_assert(step, {"type": "is_true", "field": "success"})
    _append_assert(step, {"type": "is_true", "field": "success"})

    assert step["assert"] == [{"type": "is_true", "field": "success"}]


def test_case_gen_rejection_assert_tolerates_scalar_container():
    from ai.capabilities.case_gen import _has_rejection_assert

    # container 写成标量（int）时不能直接迭代（TypeError）
    assert _has_rejection_assert(
        {"assert": [{"type": "in", "field": "code", "container": 500}]}
    ) is True
    assert _has_rejection_assert(
        {"assert": [{"type": "in", "field": "code", "container": ["00000"]}]}
    ) is False
    assert _has_rejection_assert({"assert": ["junk"]}) is False


def test_case_gen_write_excel_skips_bad_case_structures():
    from ai.capabilities.case_gen import write_cases_excel

    # 全部在进入 create_case 之前被拦下，不会落任何盘
    created, skipped = write_cases_excel(
        [
            {"testcase": {"name": "无步骤", "steps": []}},
            {"testcase": {"name": "步骤不是对象", "steps": [1]}},
            {"name": "没有 testcase"},
            "not-a-dict",
        ],
        project="mall4j",
        module="ai_hardening_probe",
    )

    assert created == []
    assert len(skipped) == 4
    assert all(item["reason"] for item in skipped)


def test_case_gen_write_yaml_skips_bad_case_structures(tmp_path):
    from ai.capabilities.case_gen import write_cases

    written = write_cases(["junk", {"name": "没有 testcase"}], tmp_path, project="mall4j")

    assert written == []
    assert list(tmp_path.rglob("*.yaml")) == []


def test_case_gen_ai_batch_skips_unusable_case():
    from ai.capabilities.case_gen import ProjectContext, _generate_with_ai
    from tools.importer.models import Endpoint

    class _ChatClient:
        available = True

        def __init__(self, payload):
            self._text = json.dumps(payload, ensure_ascii=False)

        def chat(self, *_args, **_kwargs):
            return self._text

    endpoint = Endpoint(
        id="area_list",
        method="GET",
        path="/admin/area/listByPid",
        name="按父级查区域",
        tags=["area"],
    )
    payload = {
        "cases": [
            {
                "name": "查询区域（正常请求）",
                "steps": [{"method": "GET", "path": "/admin/area/listByPid"}],
            },
            123,  # 不是对象
            {"name": "结构坏", "steps": "not-a-list"},  # 步骤不是数组
        ]
    }

    cases, covered = _generate_with_ai(
        [endpoint],
        "mall4j",
        _ChatClient(payload),
        3,
        ProjectContext(project="mall4j"),
    )

    assert len(cases) == 1
    assert covered == {("GET", "/admin/area/listByPid")}


def test_llm_config_tolerates_bad_types(monkeypatch):
    from ai.capabilities.client import LLMConfig

    for name in _AI_ENV_VARS:
        monkeypatch.delenv(name, raising=False)

    config = LLMConfig.from_config(
        {
            "base_url": 8080,          # yaml 里没加引号的端口
            "api_key": 12345,          # 数字 key
            "model": "m",
            "timeout": "60s",          # 带单位的时长
            "temperature": "hot",      # 认不出的温度
            "max_tokens": None,
            "headers": ["X-Bad"],      # 不是键值对
        }
    )

    assert config.base_url == "8080"
    assert config.api_key == "12345"
    assert config.timeout == 60
    assert config.temperature == 0.3
    assert config.max_tokens == 4000
    assert config.extra_headers == {}
    assert config.configured is True

    # 整段配置不是对象 → 按未配置处理，不能 AttributeError
    assert LLMConfig.from_config("oops").configured is False


def test_llm_client_tolerates_bad_choice_shapes():
    from ai.capabilities.client import LLMClient, LLMConfig, LLMError

    class _Client(LLMClient):
        def __init__(self, payload):
            super().__init__(LLMConfig(base_url="http://x", api_key="k", model="m"))
            self._payload = payload

        def _post(self, path, payload):
            return self._payload

    with pytest.raises(LLMError):
        _Client({"choices": "nope"}).chat("hi")
    with pytest.raises(LLMError):
        _Client({"choices": ["junk"]}).chat("hi")
    with pytest.raises(LLMError):
        _Client({"choices": "nope"}).chat_with_tools([], [])

    client = _Client({"choices": [{"message": "纯文本"}]})
    assert client.chat("hi") == "纯文本"
    assert client.chat_with_tools([], [])["message"] == {"content": "纯文本"}


def test_parse_json_response_tolerates_non_str_input():
    from ai.capabilities.client import LLMError, parse_json_response

    with pytest.raises(LLMError):
        parse_json_response(None)
    with pytest.raises(LLMError):
        parse_json_response({"a": 1})


def test_evals_grade_tolerates_string_expect_and_bad_steps():
    from ai.evals.runner import grade

    checks = grade(
        {
            "id": "T-1",
            "expect": {
                # yaml 里可能写成逗号简写 / 人类可读的数字
                "must_call": "list_cases, get_case",
                "max_tool_calls": "5次",
                "answer_must_contain": "9",
            },
        },
        {
            "steps": [{"tool": "list_cases", "ok": True}, "junk"],
            "tool_calls": "abc",
            "answer": "有 9 条用例",
            "stopped": "finished",
        },
    )

    by_name = {check.name: check for check in checks}
    assert by_name["must_call"].passed is True
    assert by_name["max_tool_calls"].passed is True  # 坏计数按 0 收紧
    assert by_name["answer_must_contain"].passed is True
    assert "loop_finished" not in by_name  # finished 时不加这条判分


def test_dimension_coverage_tolerates_assert_shapes():
    from ai.pipeline.dimension_coverage import (
        _asserts_success,
        _iter_asserts,
        _marks_set,
    )

    single = {"type": "is_true", "field": "success"}
    step = {"assert": single}

    assert _iter_asserts(step) == [single]
    assert _asserts_success(step) is True
    assert _iter_asserts({"assert": ["junk"]}) == []
    assert _marks_set({"marks": "dim:C1,p0"}) == {"dim:c1", "p0"}
    assert _marks_set({"marks": {"needs_manual_setup": True}}) == {"needs_manual_setup"}


def test_dimension_ids_of_accepts_string_marks():
    from ai.pipeline.intents import dimension_ids_of

    assert dimension_ids_of("dim:A1,p0") == {"A1"}
    assert dimension_ids_of({"dim:C1": 1}) == {"C1"}


def test_common_count_asserts_tolerates_non_list_asserts():
    from ai.agent.tools._common import count_asserts

    # 断言写成单个对象 = 一条（以前会按 dict 的 key 数算）
    assert count_asserts(
        {"steps": [{"assert": {"type": "equal", "field": "code"}}]}
    ) == 1
    assert count_asserts({"steps": [{"assert": "junk"}]}) == 0
    assert count_asserts({"steps": ["junk"]}) == 0


def test_knowledge_builder_normalizes_schema_columns(monkeypatch, tmp_path):
    from ai.knowledge import builder
    from utils import paths

    schema_dir = tmp_path / "schema"
    schema_dir.mkdir()
    (schema_dir / "mysql_demo.json").write_text(
        json.dumps(
            {
                "tables": [
                    {"name": "tz_bad", "columns": "junk"},
                    {"name": "tz_mixed", "columns": [{"name": "id"}, "junk"]},
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(paths, "CACHE_DIR", tmp_path)

    tables = builder._load_tables()

    assert tables["tz_bad"]["columns"] == []
    assert tables["tz_mixed"]["columns"] == [{"name": "id"}]
