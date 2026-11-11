"""用例质量闸门（``ai/rules/validator.py``）。

**为什么要测**

``case_rules.yaml`` 以前只是文档：规则写得清清楚楚，写入口一条也没执行。
2026-11-10 全量生成时 498 条用例落盘，141 条正例只有 code/success 两条弱断言，
25 条写用例造了数没人清 —— 生成器、写入工具、报告三边都「看起来正常」。

这里锁住闸门的判定口径，防止以后调阈值/改维度时悄悄把某条规则放空。
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai.rules.validator import (  # noqa: E402
    GateResult,
    Violation,
    business_asserts,
    check_case,
    enforce,
    format_hint,
    is_sandbox,
    value_asserts,
)

WEAK = [
    {"type": "status_code", "expected": 200},
    {"type": "equal", "field": "success", "expected": True},
    {"type": "equal", "field": "code", "expected": "00000"},
]
STRONG = [
    {"type": "exists", "field": "data"},
    {"type": "length_gt", "field": "data.records", "expected": 0},
]


def _rules(result: GateResult) -> set[str]:
    return {item.rule for item in result.violations}


# --------------------------------------------------------------------------
# 断言口径
# --------------------------------------------------------------------------


def test_rules_yaml_is_loadable_and_has_gates():
    """规则文件必须是合法 YAML —— 以前它从没被代码读过，
    连 ``[positive, A?, D1?]`` 这种非法流式序列都能一直躺着。"""
    import yaml

    from ai.rules.validator import RULES_FILE

    data = yaml.safe_load(RULES_FILE.read_text(encoding="utf-8"))
    assert data["quality_gates"]["min_business_asserts"] == 2
    assert data["min_delivery"]["read"]["required"] == ["positive", "A?", "D1?"]


def test_business_asserts_ignores_status_code_and_wrapper_fields():
    assert business_asserts(WEAK) == 0
    assert business_asserts(STRONG) == 2


def test_business_asserts_counts_db_query():
    """落库断言验证的是数据真的落了，算业务断言。"""
    assert business_asserts([{"type": "db_query", "query": "SELECT 1"}]) == 1


# --------------------------------------------------------------------------
# 正例 / 反例
# --------------------------------------------------------------------------


def test_thin_positive_is_rejected():
    result = check_case(
        case_name="查数据",
        steps=[{"method": "GET", "path": "/admin/x", "assert": WEAK}],
        marks=["positive"],
    )
    assert _rules(result) == {"thin_positive"}


def test_positive_with_two_business_asserts_passes():
    result = check_case(
        case_name="查数据",
        steps=[{"method": "GET", "path": "/admin/x", "assert": WEAK + STRONG}],
        marks=["positive"],
    )
    assert result.ok


def test_negative_without_rejection_is_rejected():
    """反例只发请求、不断言被拒绝，等于没测。"""
    result = check_case(
        case_name="查数据",
        steps=[
            {
                "method": "GET",
                "path": "/admin/x",
                "assert": [{"type": "status_code", "expected": 200}],
            }
        ],
        marks=["negative"],
    )
    assert "negative_without_rejection" in _rules(result)


def test_negative_with_code_not_equal_is_accepted():
    result = check_case(
        case_name="缺少必填参数",
        steps=[
            {
                "method": "POST",
                "path": "/admin/x",
                "assert": [{"type": "not_equal", "field": "code", "expected": "00000"}],
            }
        ],
        marks=["negative"],
        teardowns=[{"id": "clean", "method": "DELETE", "path": "/admin/x"}],
    )
    assert result.ok


def test_negative_with_success_not_equal_is_accepted():
    """``success not_equal true`` 也是拒绝断言，别只认 is_false。"""
    result = check_case(
        case_name="区域ID为0被拒绝",
        steps=[
            {
                "method": "GET",
                "path": "/admin/area",
                "assert": [
                    {"type": "not_equal", "field": "success", "expected": True},
                    {"type": "is_none", "field": "data"},
                ],
            }
        ],
        marks=["boundary"],
    )
    assert result.ok


def test_boundary_case_expecting_success_is_not_negative():
    """边界不等于反例：超大 pid 期望空列表，不该被要求断言拒绝。"""
    result = check_case(
        case_name="超大pid返回空列表",
        steps=[
            {
                "method": "GET",
                "path": "/admin/area/listByPid",
                "assert": [
                    {"type": "status_code", "expected": 200},
                    {"type": "equal", "field": "code", "expected": "00000"},
                    {"type": "length", "field": "data", "expected": 0},
                ],
            }
        ],
        marks=["boundary"],
    )
    assert "negative_without_rejection" not in _rules(result)
    assert result.ok


def test_explicit_positive_overrides_boundary_mark():
    """``boundary + positive``：按正例看业务断言，不要求拒绝断言。"""
    result = check_case(
        case_name="超大分页size截断",
        steps=[
            {
                "method": "GET",
                "path": "/admin/area/page",
                "assert": [
                    {"type": "equal", "field": "code", "expected": "00000"},
                    {"type": "equal", "field": "success", "expected": True},
                    {"type": "length_lte", "field": "data.records", "expected": 1000},
                    {"type": "gte", "field": "data.total", "expected": 0},
                ],
            }
        ],
        marks=["boundary", "positive"],
    )
    assert "negative_without_rejection" not in _rules(result)
    assert result.ok


# --------------------------------------------------------------------------
# 写操作清理
# --------------------------------------------------------------------------


def test_write_without_teardown_is_rejected():
    result = check_case(
        case_name="新增地区",
        steps=[{"method": "POST", "path": "/admin/area", "assert": WEAK + STRONG}],
        marks=["positive"],
    )
    assert "write_without_cleanup" in _rules(result)


def test_write_with_teardown_passes():
    result = check_case(
        case_name="新增地区",
        steps=[{"method": "POST", "path": "/admin/area", "assert": WEAK + STRONG}],
        marks=["positive"],
        teardowns=[{"id": "clean", "method": "DELETE", "path": "/admin/area"}],
    )
    assert result.ok


def test_delete_self_cleaning_via_params_passes():
    """DELETE 引用 setup 产出的 id 就是自清理，不需要额外 teardown。

    引用可能写在 ``params`` 里（不是 path）—— 只扫 path 会误判成脏数据。
    """
    result = check_case(
        case_name="删除地区",
        steps=[
            {
                "method": "DELETE",
                "path": "/admin/area",
                "params": {"areaId": "{{seed.areaId}}"},
                "assert": WEAK + STRONG,
            }
        ],
        setups=[{"id": "seed", "method": "POST", "path": "/admin/area"}],
        marks=["positive"],
    )
    assert result.ok


# --------------------------------------------------------------------------
# 命名 / 路径参数
# --------------------------------------------------------------------------


def test_method_path_naming_is_rejected():
    result = check_case(
        case_name="GET /admin/area",
        steps=[{"method": "GET", "path": "/admin/area", "assert": WEAK + STRONG}],
        marks=["positive"],
    )
    assert "method_path_naming" in _rules(result)


def test_unresolved_single_brace_path_is_rejected():
    result = check_case(
        case_name="修改地区",
        steps=[{"method": "PUT", "path": "/admin/area/{id}", "assert": WEAK + STRONG}],
        marks=["positive"],
        teardowns=[{"id": "clean", "method": "DELETE", "path": "/admin/area"}],
    )
    assert "unresolved_path_param" in _rules(result)


def test_framework_variable_in_path_is_not_flagged():
    """``{{...}}`` 是运行时展开的框架变量，不是没解析的路径参数。"""
    result = check_case(
        case_name="删除地区",
        steps=[
            {
                "method": "DELETE",
                "path": "/admin/area/{{tools.find_id_by_fixed(data, 'areaName', 'x', 'areaId', 'y')}}",
                "assert": WEAK + STRONG,
            }
        ],
        setups=[{"id": "seed", "method": "POST", "path": "/admin/area"}],
        marks=["positive"],
    )
    assert "unresolved_path_param" not in _rules(result)


# --------------------------------------------------------------------------
# 维度
# --------------------------------------------------------------------------


def test_d1_without_db_assert_is_rejected():
    result = check_case(
        case_name="查数据",
        steps=[{"method": "GET", "path": "/admin/x", "assert": WEAK + STRONG}],
        marks=["positive", "dim:D1"],
    )
    assert "unverified_dimension" in _rules(result)


def test_d1_with_db_assert_passes():
    result = check_case(
        case_name="查数据",
        steps=[
            {
                "method": "GET",
                "path": "/admin/x",
                "assert": WEAK
                + STRONG
                + [{"type": "db_query", "query": "SELECT 1", "expected": 1}],
            }
        ],
        marks=["positive", "dim:D1"],
    )
    assert result.ok


def test_e3_without_cleanup_is_rejected():
    result = check_case(
        case_name="新增地区",
        steps=[{"method": "POST", "path": "/admin/area", "assert": WEAK + STRONG}],
        marks=["positive", "dim:E3"],
    )
    assert "unverified_dimension" in _rules(result)


# --------------------------------------------------------------------------
# 模块分档
# --------------------------------------------------------------------------


def test_sandbox_violations_are_downgraded():
    gate = check_case(
        case_name="查数据",
        steps=[{"method": "GET", "path": "/admin/x", "assert": WEAK}],
        marks=["positive"],
    )
    enforce("ai_admin_area", gate)
    assert gate.downgraded is True
    assert not gate.ok


def test_production_violations_are_not_downgraded():
    gate = check_case(
        case_name="查数据",
        steps=[{"method": "GET", "path": "/admin/x", "assert": WEAK}],
        marks=["positive"],
    )
    enforce("admin_area", gate)
    assert gate.downgraded is False


def test_is_sandbox():
    assert is_sandbox("ai_admin_area")
    assert not is_sandbox("admin_area")


# --------------------------------------------------------------------------
# 提示
# --------------------------------------------------------------------------


def test_format_hint_explains_each_rule():
    gate = check_case(
        case_name="查数据",
        steps=[{"method": "POST", "path": "/admin/x/{id}", "assert": WEAK}],
        marks=["positive"],
    )
    hint = format_hint(gate)
    assert "业务字段断言" in hint
    assert "teardowns" in hint
    assert "路径参数" in hint


def test_gate_result_serializes():
    gate = GateResult(violations=[Violation("thin_positive", "太薄")])
    assert gate.to_dict() == {
        "ok": False,
        "downgraded": False,
        "violations": [{"rule": "thin_positive", "message": "太薄"}],
    }


# --------------------------------------------------------------------------
# 零断言的反例
# --------------------------------------------------------------------------


def test_negative_with_no_asserts_is_rejected():
    """发请求但不写断言 —— 连「被拒绝」都没验证，不能因为断言为空就放行。"""
    result = check_case(
        case_name="缺少必填参数",
        steps=[{"method": "POST", "path": "/admin/x", "assert": []}],
        marks=["negative"],
    )
    assert "negative_without_rejection" in _rules(result)


# --------------------------------------------------------------------------
# 值断言
# --------------------------------------------------------------------------


def test_value_asserts_ignores_existence_only():
    """``exists`` / ``is_not_none`` 只证明字段存在，不是值断言。"""
    assert value_asserts([{"type": "exists", "field": "data"}]) == 0
    assert value_asserts([{"type": "is_not_none", "field": "data.records"}]) == 0
    assert value_asserts(
        [{"type": "length_gt", "field": "data.records", "expected": 0}]
    ) == 1
    assert value_asserts([{"type": "db_query", "query": "SELECT 1"}]) == 1


def test_value_assert_required_only_when_gate_raises_it():
    """落盘时不要求值断言；转 debugged 时（``min_value_asserts=1``）才拦。"""
    case = dict(
        case_name="查数据",
        steps=[
            {
                "method": "POST",
                "path": "/admin/area",
                "assert": WEAK
                + [
                    {"type": "exists", "field": "data"},
                    {"type": "exists", "field": "data.records"},
                ],
            }
        ],
        marks=["positive"],
        teardowns=[{"id": "clean", "method": "DELETE", "path": "/admin/area"}],
    )
    assert check_case(**case).ok
    gate = check_case(**case, gates={"min_value_asserts": 1})
    assert "value_assert_missing" in _rules(gate)


# --------------------------------------------------------------------------
# 多步场景链
# --------------------------------------------------------------------------


def _chain_steps(*, verify_asserts=None, write_both=False):
    steps = [
        {
            "method": "POST",
            "path": "/admin/area",
            "assert": WEAK + [{"type": "exists", "field": "data"}],
        }
    ]
    if write_both:
        steps.append(
            {
                "method": "PUT",
                "path": "/admin/area/{{step1.id}}",
                "assert": WEAK + [{"type": "exists", "field": "data"}],
            }
        )
    steps.append(
        {
            "method": "GET",
            "path": "/admin/area/{{step1.id}}",
            "assert": verify_asserts
            if verify_asserts is not None
            else [{"type": "exists", "field": "data.records"}],
        }
    )
    return steps


def test_multi_step_asserts_are_aggregated_across_chain():
    """断言分散在写步骤和验证步上，合起来够 2 条就该放行。"""
    result = check_case(
        case_name="新增地区",
        steps=_chain_steps(),
        marks=["positive"],
        teardowns=[{"id": "clean", "method": "DELETE", "path": "/admin/area"}],
    )
    assert result.ok


def test_multi_step_needs_assert_on_verification_step():
    result = check_case(
        case_name="新增地区",
        steps=_chain_steps(verify_asserts=[]),
        marks=["positive"],
        teardowns=[{"id": "clean", "method": "DELETE", "path": "/admin/area"}],
    )
    assert "thin_verification_step" in _rules(result)


def test_multi_step_checks_every_write_step_cleanup():
    """链尾的写步骤没去处，整条用例就算没清理。"""
    result = check_case(
        case_name="修改地区",
        steps=_chain_steps(write_both=True),
        setups=[{"id": "seed", "method": "POST", "path": "/admin/area"}],
        marks=["positive"],
    )
    assert "write_without_cleanup" in _rules(result)


def test_multi_step_path_params_checked_in_setups():
    result = check_case(
        case_name="修改地区",
        steps=_chain_steps(),
        setups=[{"id": "seed", "method": "GET", "path": "/admin/area/{id}"}],
        marks=["positive"],
        teardowns=[{"id": "clean", "method": "DELETE", "path": "/admin/area"}],
    )
    assert "unresolved_path_param" in _rules(result)
