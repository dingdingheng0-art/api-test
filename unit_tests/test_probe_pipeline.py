"""探针 → 生成 的单元测试（不联网）。

探针的价值在于「实测优先于猜测」。这里验证三件事：

1. 测点 → 探针请求的变形是对的（路径参数要真改、字段不存在不许硬发）
2. 探针结果能正确归一化（dataclass / JSON 中文键都认）
3. 实测结果能改写断言，并且能免掉「没证据的 needs_manual_setup」
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai.capabilities.case_gen import (
    ProjectContext,
    _apply_probe_evidence,
    _finalize_ai_case,
    _normalize_ai_case,
    _probe_index,
    _probe_record,
)
from ai.pipeline.intents import TestIntent as Intent
from ai.pipeline.probe import (
    NONEXISTENT_ID,
    ProbeOutcome,
    _base_step,
    _contains_template,
    _mutate_for_intent,
)
from tools.importer.models import Endpoint, Param


class FakeContext:
    """只提供探针需要的路径取值。"""

    def path_values_for(self, ep: Endpoint) -> dict[str, str]:
        return {"id": "110000000000"}


def _area_endpoint() -> Endpoint:
    return Endpoint(
        id="area",
        method="GET",
        path="/admin/area/listByPid",
        name="按父级查区域",
        tags=["area"],
        query=[
            Param(name="pid", location="query", required=True, type="integer", example=1)
        ],
    )


def _info_endpoint() -> Endpoint:
    return Endpoint(
        id="area_info",
        method="GET",
        path="/admin/area/info/{id}",
        name="区域详情",
        tags=["area"],
        path_params=[
            Param(name="id", location="path", required=True, type="integer", example=1)
        ],
    )


def _outcome(
    dimension: str,
    *,
    status_code: int = 200,
    code: str = "00000",
    success: bool | None = True,
    has_success: bool = True,
    data_present: bool = True,
    data_is_none: bool = False,
    field: str = "",
    error: str = "",
) -> ProbeOutcome:
    return ProbeOutcome(
        dimension=dimension,
        label=dimension,
        method="GET",
        path="/admin/area/listByPid",
        field=field,
        status_code=status_code,
        code=code,
        success=success,
        has_success=has_success,
        data_present=data_present,
        data_is_none=data_is_none,
        error=error,
    )


def _ctx() -> ProjectContext:
    return ProjectContext(
        project="mall4j",
        contract={"success_code": "00000", "http_status": 200},
    )


def _case(
    raw_marks: list[str],
    path: str,
    asserts: list[dict],
    dimension: str = "A1",
    method: str = "GET",
) -> dict:
    raw = {
        "name": "测试用例",
        "description": "",
        "dimension": dimension,
        "marks": raw_marks,
        "steps": [
            {
                "id": "step1",
                "path": path,
                "method": method,
                "params": {},
                "assert": asserts,
            }
        ],
    }
    return _normalize_ai_case(raw)


# ---------------------------------------------------------------------------
# 请求变形
# ---------------------------------------------------------------------------


class TestMutateForIntent:
    def test_a1_removes_required_query_param(self) -> None:
        ep = _area_endpoint()
        request = _mutate_for_intent(
            ep, Intent("A1", "必填缺失", "pid"), _base_step(ep), FakeContext()
        )
        assert request is not None
        assert "pid" not in (request["params"] or {})

    def test_a2_on_path_param_really_changes_path(self) -> None:
        ep = _info_endpoint()
        request = _mutate_for_intent(
            ep, Intent("A2", "类型错误", "id"), _base_step(ep), FakeContext()
        )
        assert request is not None, "路径参数的类型测点必须能探，不能直接放弃"
        assert request["path"] == "/admin/area/info/not-a-number"

    def test_f1_uses_nonexistent_id(self) -> None:
        ep = _info_endpoint()
        request = _mutate_for_intent(
            ep, Intent("F1", "不存在的 id", "id"), _base_step(ep), FakeContext()
        )
        assert request is not None
        assert request["path"].endswith(f"/{NONEXISTENT_ID}")

    def test_c1_drops_auth(self) -> None:
        ep = _area_endpoint()
        request = _mutate_for_intent(
            ep, Intent("C1", "未登录"), _base_step(ep), FakeContext()
        )
        assert request is not None
        assert request["auth"] is False

    def test_unresolved_template_is_detected(self) -> None:
        """现役用例里的 ``{{...}}`` 没展开时，探针不能发出去。"""
        assert _contains_template({"pid": "{{tools.fixed('x')}}"}) is True
        assert _contains_template({"nested": [{"a": "${x}"}]}) is True
        assert _contains_template({"pid": 1, "name": "area"}) is False

    def test_a2_on_missing_field_is_skipped(self) -> None:
        """字段不在基准请求里，改了也等于没改 —— 不能发出假探针。"""
        ep = _area_endpoint()
        request = _mutate_for_intent(
            ep,
            Intent("A2", "类型错误", "notInRequest"),
            _base_step(ep),
            FakeContext(),
        )
        assert request is None


# ---------------------------------------------------------------------------
# 结果归一化
# ---------------------------------------------------------------------------


class TestProbeRecord:
    def test_dataclass_rejection(self) -> None:
        record = _probe_record(_outcome("A1", code="A00005", success=False))
        assert record is not None
        assert record["rejected"] is True
        assert record["code"] == "A00005"

    def test_json_chinese_keys(self) -> None:
        record = _probe_record(
            {
                "维度": "C1",
                "行为": "rejected:A00004",
                "状态码": 200,
                "code": "A00004",
                "success": False,
                "有数据": False,
            }
        )
        assert record is not None
        assert record["dimension"] == "C1"
        assert record["rejected"] is True
        assert record["data_present"] is False

    def test_network_error_is_not_rejection(self) -> None:
        record = _probe_record(_outcome("P0", error="timeout", success=None))
        assert record is not None
        assert record["rejected"] is False
        assert record["error"] == "timeout"

    def test_index_marks_conflicting_records_ambiguous(self) -> None:
        """同维度多字段结论不同（pid 被拒 / size 接受）时不能只留一条。"""
        index = _probe_index(
            {
                "GET /admin/area/listByPid": [
                    _outcome("A1", field="pid", code="A00005", success=False),
                    _outcome("A1", field="size", code="00000", success=True),
                ]
            }
        )
        assert index[("GET", "/admin/area/listByPid")]["A1"]["ambiguous"] is True

    def test_index_normalizes_numeric_paths(self) -> None:
        index = _probe_index(
            {
                "GET /admin/area/info/110000000000": [
                    _outcome("P0"),
                ]
            }
        )
        assert ("GET", "/admin/area/info/{}") in index
        assert "P0" in index[("GET", "/admin/area/info/{}")]


# ---------------------------------------------------------------------------
# 路径参数取值：只能取正例
# ---------------------------------------------------------------------------


class TestPathValues:
    def test_negative_example_does_not_override_positive(self) -> None:
        """现役 Excel 里「查不到数据」那条不能覆盖正例的真实 id。"""
        ep = _info_endpoint()
        ctx = ProjectContext(project="mall4j")
        ctx.examples_by_endpoint[("GET", "/admin/area/info/{}")] = [
            {
                "path": "/admin/area/info/110000000000",
                "case": "查询地区详情（正常请求）",
                "marks": ["positive", "p0"],
            },
            {
                "path": "/admin/area/info/999999999",
                "case": "查询地区详情（查不到数据）",
                "marks": ["negative", "dim:F1"],
            },
        ]
        assert ctx.path_values_for(ep) == {"id": "110000000000"}

    def test_nonexistent_value_is_skipped_even_on_positive_case(self) -> None:
        ep = _info_endpoint()
        ctx = ProjectContext(project="mall4j")
        ctx.examples_by_endpoint[("GET", "/admin/area/info/{}")] = [
            {"path": "/admin/area/info/999999999", "case": "查询详情", "marks": ["positive"]},
            {"path": "/admin/area/info/123", "case": "查询详情", "marks": ["positive"]},
        ]
        assert ctx.path_values_for(ep) == {"id": "123"}


# ---------------------------------------------------------------------------
# 断言改写
# ---------------------------------------------------------------------------


def _probes(dimension: str, **kw) -> dict[str, dict]:
    record = _probe_record(_outcome(dimension, **kw))
    assert record is not None
    return {dimension: record}


class TestApplyProbeEvidence:
    def test_rejection_uses_exact_code(self) -> None:
        case = _case(
            ["negative", "dim:A1"],
            "/admin/area/listByPid",
            [
                {"type": "status_code", "expected": 200},
                {"type": "not_equal", "field": "code", "expected": "00000"},
            ],
        )
        record = _apply_probe_evidence(
            case["testcase"], _probes("A1", code="A00005", success=False), _ctx()
        )
        assert record is not None
        asserts = case["testcase"]["steps"][0]["assert"]
        assert {"type": "equal", "field": "code", "expected": "A00005"} in asserts
        # 模糊断言被精确断言取代
        assert not any(
            a.get("field") == "code" and a.get("type") == "not_equal" for a in asserts
        )

    def test_rejection_status_beats_default_contract_status(self) -> None:
        """框架默认补 200，实测 401 时必须覆盖，否则断言自相矛盾。"""
        case = _case(
            ["permission", "dim:C1"],
            "/admin/area/listByPid",
            [{"type": "status_code", "expected": 200}],
            dimension="C1",
        )
        _apply_probe_evidence(
            case["testcase"],
            _probes("C1", status_code=401, code="", success=False),
            _ctx(),
        )
        status = [
            a
            for a in case["testcase"]["steps"][0]["assert"]
            if a.get("type") == "status_code"
        ]
        assert status == [{"type": "status_code", "expected": 401}]

    def test_contract_mismatch_marks_defect_candidate(self) -> None:
        """文档说该拒、实测接受 —— 用例断言事实，并标缺陷候选。"""
        case = _case(
            ["negative", "dim:A1"],
            "/admin/area/listByPid",
            [
                {"type": "status_code", "expected": 200},
                {"type": "not_equal", "field": "code", "expected": "00000"},
            ],
        )
        _apply_probe_evidence(
            case["testcase"], _probes("A1", code="00000", success=True), _ctx()
        )
        tc = case["testcase"]
        asserts = tc["steps"][0]["assert"]
        assert {"type": "equal", "field": "code", "expected": "00000"} in asserts
        assert {"type": "is_true", "field": "success"} in asserts
        assert "defect_candidate" in tc["marks"]
        assert not any(a.get("type") == "not_equal" for a in asserts)

    def test_f1_null_data_asserted(self) -> None:
        case = _case([], "/admin/area/info/110000000000", [], dimension="F1")
        _apply_probe_evidence(
            case["testcase"],
            _probes(
                "F1", code="00000", success=True, data_present=False, data_is_none=True
            ),
            _ctx(),
        )
        asserts = case["testcase"]["steps"][0]["assert"]
        assert {"type": "is_none", "field": "data"} in asserts

    def test_f1_empty_object_is_not_asserted_as_null(self) -> None:
        """实测 data 是 {} 而不是 null 时，断言 is_none 会误报。"""
        case = _case([], "/admin/area/info/110000000000", [], dimension="F1")
        _apply_probe_evidence(
            case["testcase"],
            _probes("F1", code="00000", success=True, data_present=False),
            _ctx(),
        )
        asserts = case["testcase"]["steps"][0]["assert"]
        assert {"type": "is_none", "field": "data"} not in asserts

    def test_error_outcome_changes_nothing(self) -> None:
        case = _case(["negative", "dim:A1"], "/admin/area/listByPid", [])
        record = _apply_probe_evidence(
            case["testcase"], _probes("A1", error="timeout", success=None), _ctx()
        )
        assert record is None

    def test_unknown_behavior_changes_nothing(self) -> None:
        """5xx/HTML 没有 success 语义 —— 不是「接受」，不能当证据。"""
        case = _case(
            ["negative", "dim:A1"],
            "/admin/area/listByPid",
            [{"type": "not_equal", "field": "code", "expected": "00000"}],
        )
        record = _apply_probe_evidence(
            case["testcase"],
            _probes("A1", status_code=502, code="", success=None, has_success=False),
            _ctx(),
        )
        assert record is None
        # 原断言保持原样
        asserts = case["testcase"]["steps"][0]["assert"]
        assert {"type": "not_equal", "field": "code", "expected": "00000"} in asserts

    def test_ambiguous_record_changes_nothing(self) -> None:
        case = _case(["negative", "dim:A1"], "/admin/area/listByPid", [])
        record = _probes("A1", code="A00005", success=False)
        record["A1"]["ambiguous"] = True
        assert _apply_probe_evidence(case["testcase"], record, _ctx()) is None

    def test_no_success_field_means_no_success_assertion(self) -> None:
        """响应里没有 success 字段就不能断言它 —— 否则必红。"""
        case = _case(["negative", "dim:A1"], "/admin/area/listByPid", [])
        _apply_probe_evidence(
            case["testcase"],
            _probes("A1", code="A00005", success=False, has_success=False),
            _ctx(),
        )
        asserts = case["testcase"]["steps"][0]["assert"]
        assert {"type": "equal", "field": "code", "expected": "A00005"} in asserts
        assert not any(a.get("field") == "success" for a in asserts)

    def test_string_boolean_assertions_are_dropped_on_rejection(self) -> None:
        """``equal success "true"`` 这类写法也要被实测拒绝断言取代。"""
        case = _case(
            ["negative", "dim:A1"],
            "/admin/area/listByPid",
            [
                {"type": "equal", "field": "success", "expected": "true"},
                {"type": "is_false", "field": "code"},
            ],
        )
        _apply_probe_evidence(
            case["testcase"], _probes("A1", code="A00005", success=False), _ctx()
        )
        asserts = case["testcase"]["steps"][0]["assert"]
        assert not any(a.get("field") == "success" for a in asserts)

    def test_guessed_code_is_replaced_not_duplicated(self) -> None:
        """模型猜的错误码（哪怕看起来合理）必须被实测码取代，不能并存。"""
        case = _case(
            ["negative", "dim:A1"],
            "/admin/area/listByPid",
            [{"type": "equal", "field": "code", "expected": "A00003"}],
        )
        _apply_probe_evidence(
            case["testcase"], _probes("A1", code="A00005", success=False), _ctx()
        )
        codes = [
            a
            for a in case["testcase"]["steps"][0]["assert"]
            if a.get("field") == "code"
        ]
        assert codes == [{"type": "equal", "field": "code", "expected": "A00005"}]

    def test_success_branch_replaces_guessed_code(self) -> None:
        case = _case(
            ["negative", "dim:A1"],
            "/admin/area/listByPid",
            [{"type": "equal", "field": "code", "expected": "A00003"}],
        )
        _apply_probe_evidence(
            case["testcase"], _probes("A1", code="00000", success=True), _ctx()
        )
        codes = [
            a
            for a in case["testcase"]["steps"][0]["assert"]
            if a.get("field") == "code"
        ]
        assert codes == [{"type": "equal", "field": "code", "expected": "00000"}]

    def test_status_dollar_field_is_replaced(self) -> None:
        """框架支持 ``field=$status``，等价于 status_code，不能被漏删。"""
        case = _case(
            ["permission", "dim:C1"],
            "/admin/area/listByPid",
            [{"type": "equal", "field": "$status", "expected": 200}],
            dimension="C1",
        )
        _apply_probe_evidence(
            case["testcase"],
            _probes("C1", status_code=401, code="", success=False),
            _ctx(),
        )
        asserts = case["testcase"]["steps"][0]["assert"]
        assert {"type": "status_code", "expected": 401} in asserts
        assert not any(a.get("field") == "$status" for a in asserts)

    def test_dimension_without_probe_does_not_use_p0(self) -> None:
        """标了维度但该维度没探到 —— 不能借 P0 的正例实测改写反例。"""
        case = _case(
            ["negative", "dim:F1"], "/admin/area/info/110000000000", []
        )
        record = _apply_probe_evidence(
            case["testcase"], _probes("P0", code="00000", success=True), _ctx()
        )
        assert record is None

    def test_negative_case_without_dimension_does_not_borrow_p0(self) -> None:
        """没标维度的反例不能被正例实测改写。"""
        case = _case(["negative"], "/admin/area/listByPid", [])
        record = _apply_probe_evidence(
            case["testcase"], _probes("P0", code="00000", success=True), _ctx()
        )
        assert record is None


# ---------------------------------------------------------------------------
# 收敛：探针证据免掉 needs_manual_setup
# ---------------------------------------------------------------------------


class TestFinalizeWithProbe:
    def test_probe_evidence_clears_manual_flag(self) -> None:
        case = _case(
            ["negative", "dim:A1"],
            "/admin/area/listByPid",
            [{"type": "status_code", "expected": 200}],
        )
        probe_index = _probe_index(
            {"GET /admin/area/listByPid": [_outcome("A1", code="A00005", success=False)]}
        )
        result = _finalize_ai_case(case, _ctx(), [_area_endpoint()], probe_index)
        marks = result["testcase"]["marks"]
        assert "needs_manual_setup" not in marks
        assert "probe_verified" in marks
        assert "probe:A1" in marks

    def test_without_probe_still_needs_manual(self) -> None:
        case = _case(
            ["negative", "dim:A1"],
            "/admin/area/listByPid",
            [{"type": "status_code", "expected": 200}],
        )
        result = _finalize_ai_case(case, _ctx(), [_area_endpoint()], None)
        assert "needs_manual_setup" in result["testcase"]["marks"]

    def test_setup_single_brace_path_marks_manual(self) -> None:
        """前置里的 ``{id}`` 不解析会 404；后置里更危险（清理静默失败）。"""
        case = _case(["positive", "dim:P0"], "/admin/area/listByPid", [], dimension="P0")
        case["testcase"]["setups"] = [
            {"id": "setup1", "method": "GET", "path": "/admin/area/info/{id}", "params": {}}
        ]
        result = _finalize_ai_case(
            case, _ctx(), [_area_endpoint(), _info_endpoint()], None
        )
        assert "needs_manual_setup" in result["testcase"]["marks"]

    def test_setup_framework_variable_is_left_alone(self) -> None:
        """``{{步骤.id}}`` 是执行期变量，不能被当成未解析的路径参数。"""
        case = _case(["positive", "dim:P0"], "/admin/area/listByPid", [], dimension="P0")
        case["testcase"]["setups"] = [
            {
                "id": "setup1",
                "method": "POST",
                "path": "/admin/area/{{steps.step1.id}}",
                "headers": {"Authorization": "{{token}}"},
                "params": {},
            }
        ]
        case["testcase"]["teardowns"] = [
            {"id": "cleanup1", "method": "DELETE", "path": "/admin/area/{{steps.step1.id}}"}
        ]
        result = _finalize_ai_case(
            case, _ctx(), [_area_endpoint(), _info_endpoint()], None
        )
        assert "needs_manual_setup" not in result["testcase"]["marks"]
        headers = result["testcase"]["setups"][0]["headers"]
        assert headers["Authorization"] == "{{mall4j.token}}"

    def test_probe_evidence_does_not_rescue_missing_teardown(self) -> None:
        """写操作没清理属于安全问题，探针不能替它开绿灯。"""
        ep = Endpoint(
            id="create",
            method="POST",
            path="/admin/area/save",
            name="保存区域",
            tags=["area"],
            body_params=[Param(name="areaName", location="body", required=True)],
            body={"areaName": "x"},
        )
        case = _case(
            ["positive", "dim:P0"],
            "/admin/area/save",
            [],
            dimension="P0",
            method="POST",
        )
        probe_index = _probe_index(
            {"POST /admin/area/save": [_outcome("P0", code="00000", success=True)]}
        )
        result = _finalize_ai_case(case, _ctx(), [ep], probe_index)
        assert "needs_manual_setup" in result["testcase"]["marks"]
