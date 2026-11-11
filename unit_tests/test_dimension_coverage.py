"""维度覆盖率的口径测试。

覆盖度是「验收指标」，不是「展示指标」。这里锁住几条不能松的口径：

- 标了 C1 但请求带鉴权头 → 不算覆盖
- 标了 D 类但没有 db_query 断言 → 不算覆盖
- 标了 E3 但没有 teardown → 不算覆盖
- 正例 + 分页边界是叠加关系，不能只算一个
"""

from __future__ import annotations

from ai.pipeline import dimension_coverage as dc


def _case(marks=None, steps=None, teardowns=None):
    return {"marks": marks or [], "steps": steps or [], "teardowns": teardowns or []}


def _step(asserts=None, headers=None, params=None, method="GET"):
    return {
        "method": method,
        "path": "/admin/area/page",
        "headers": headers if headers is not None else {},
        "params": params or {},
        "assert": asserts or [],
    }


_SUCCESS = [
    {"type": "equal", "field": "success", "expected": True},
    {"type": "equal", "field": "code", "expected": "00000"},
]

_REJECT = [{"type": "not_equal", "field": "code", "expected": "00000"}]


def test_claimed_c1_but_has_auth_header_is_not_covered():
    step = _step(asserts=_SUCCESS, headers={"Authorization": "Bearer x"})
    ok, reason = dc._verify_dimension("C1", _case(["C1"]), [step], step)
    assert ok is False
    assert "鉴权头" in reason


def test_c1_needs_no_auth_mark_because_framework_injects_token():
    """请求头为空但没标 no_auth → 框架仍会补 token，不算匿名。"""
    step = _step(asserts=[{"type": "not_equal", "field": "code", "expected": "00000"}])
    ok, reason = dc._verify_dimension("C1", _case(["C1"]), [step], step)
    assert ok is False
    assert "no_auth" in reason


def test_c1_with_no_auth_mark_and_rejection_is_covered():
    step = _step(asserts=[{"type": "status_code", "expected": 401}])
    ok, _ = dc._verify_dimension("C1", _case(["C1", "no_auth"]), [step], step)
    assert ok is True


def test_c1_with_only_status_200_is_not_covered():
    """发了个请求不等于验证了未登录。"""
    step = _step(asserts=[{"type": "status_code", "expected": 200}])
    ok, reason = dc._verify_dimension("C1", _case(["C1", "no_auth"]), [step], step)
    assert ok is False
    assert "被拒绝" in reason


def test_f1_accepts_success_with_empty_data():
    step = _step(
        asserts=_SUCCESS + [{"type": "is_none", "field": "data"}],
        method="GET",
    )
    ok, _ = dc._verify_dimension("F1", _case(["dim:F1"]), [step], step)
    assert ok is True


def test_f1_without_any_empty_assertion_is_not_covered():
    step = _step(asserts=_SUCCESS)
    ok, _ = dc._verify_dimension("F1", _case(["dim:F1"]), [step], step)
    assert ok is False


def test_claimed_db_dimension_needs_a_db_assert():
    step = _step(asserts=_SUCCESS)
    ok, reason = dc._verify_dimension("D2", _case(["D2"]), [step], step)
    assert ok is False
    assert "db_query" in reason

    step_with_db = _step(
        asserts=_SUCCESS + [{"type": "db_query_exists", "query": "SELECT 1"}]
    )
    ok, _ = dc._verify_dimension("D2", _case(["D2"]), [step_with_db], step_with_db)
    assert ok is True


def test_claimed_cleanup_needs_teardown():
    step = _step(asserts=_SUCCESS, method="DELETE")
    ok, reason = dc._verify_dimension("E3", _case(["E3"]), [step], step)
    assert ok is False
    assert "teardown" in reason

    case = _case(["E3"], teardowns=[{"method": "DELETE", "path": "/x"}])
    ok, _ = dc._verify_dimension("E3", case, [step], step)
    assert ok is True


def test_negative_dimension_needs_rejection_assert():
    step = _step(asserts=_SUCCESS)
    ok, reason = dc._verify_dimension("A1", _case(["A1"]), [step], step)
    assert ok is False
    assert "期望失败" in reason

    step = _step(asserts=_REJECT)
    ok, _ = dc._verify_dimension("A1", _case(["A1"]), [step], step)
    assert ok is True

    # 探针实测后写的是精确错误码，同样算「期望失败」
    step = _step(asserts=[{"type": "equal", "field": "code", "expected": "A00005"}])
    ok, _ = dc._verify_dimension("A1", _case(["A1"]), [step], step)
    assert ok is True


def test_positive_and_pagination_boundary_stack():
    step = _step(asserts=_SUCCESS, params={"current": 1, "size": 999})
    dims = dc._infer_dimensions(_case(), step)
    assert "P0" in dims
    assert "B1" in dims


def test_normal_page_size_is_not_boundary():
    step = _step(asserts=_SUCCESS, params={"current": 1, "size": 10})
    dims = dc._infer_dimensions(_case(), step)
    assert "P0" in dims
    assert "B1" not in dims


def test_positive_case_must_assert_success():
    step = _step(asserts=[{"type": "status_code", "expected": 200}])
    ok, reason = dc._verify_dimension("P0", _case(), [step], step)
    assert ok is False
    assert "成功语义" in reason


def test_path_normalization_matches_concrete_ids():
    assert dc._endpoint_key("GET", "/admin/area/info/123") == dc._endpoint_key(
        "get", "/admin/area/info/{id}"
    )


def test_probed_contract_mismatch_is_not_negative_coverage():
    """探针实测「服务端接受」后断言成功 —— 这是缺陷线索，不算 A1 覆盖。"""
    step = _step(asserts=_SUCCESS)
    ok, reason = dc._verify_dimension("A1", _case(["dim:A1", "defect_candidate"]), [step], step)
    assert ok is False
    assert "期望失败" in reason


def test_probe_note_is_extracted_from_description():
    case = {
        "description": "反例\n探针实测：A1 场景服务端未拒绝（success），疑似契约未校验",
    }
    assert dc._probe_note(case).startswith("探针实测")


def test_priority_mark_is_not_a_dimension():
    """``p0`` 是优先级，不是维度 P0。撞名会让覆盖率虚高。"""
    from ai.pipeline.intents import dimension_ids_of

    assert dimension_ids_of(["p0", "p1", "smoke"]) == set()
    assert dimension_ids_of(["dim:A1", "p0"]) == {"A1"}
    # 老产物里的裸大写 ID 仍然认
    assert dimension_ids_of(["A1", "p0"]) == {"A1"}
