"""用例依赖编排的单元测试。

测四件事：

1. 依赖图怎么建、怎么排序
2. 循环依赖能不能被发现（不能死等）
3. 依赖不存在的用例能不能报错
4. 执行结果能不能跨进程存取
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.cases.case_dependency import (
    CaseNode,
    DependencyError,
    DependencyGraph,
    DependencyStore,
    build_graph_from_cases,
    collect_case_outputs,
    extract_dependencies,
    merge_dependencies_into_globals,
)


# ---------------------------------------------------------------------------
# 建图
# ---------------------------------------------------------------------------


def test_build_graph_from_cases() -> None:
    """能从用例数据里读出依赖关系。"""
    cases = [
        ("mall4j", Path("a.yaml"), {"testcase": {"name": "创建订单"}}),
        (
            "mall4j",
            Path("b.yaml"),
            {"testcase": {"name": "查询订单", "depends_on": ["创建订单"]}},
        ),
    ]

    graph = build_graph_from_cases(cases)

    assert len(graph) == 2
    assert graph.get("创建订单").depends_on == []
    assert graph.get("查询订单").depends_on == ["创建订单"]


def test_extract_dependencies_accepts_string() -> None:
    """depends_on 写成字符串也认（等价于只有一个依赖）。"""
    assert extract_dependencies({"depends_on": "创建订单"}) == ["创建订单"]


def test_extract_dependencies_ignores_empty() -> None:
    """空的依赖列表返回空列表。"""
    assert extract_dependencies({"depends_on": []}) == []
    assert extract_dependencies({}) == []


# ---------------------------------------------------------------------------
# 排序
# ---------------------------------------------------------------------------


def test_topological_order_puts_dependency_first() -> None:
    """被依赖的用例排在前面。"""
    graph = DependencyGraph(
        [
            CaseNode(name="查询订单", project="p", depends_on=["创建订单"], order=0),
            CaseNode(name="创建订单", project="p", order=1),
        ]
    )

    order = graph.topological_order("p")

    assert order.index("创建订单") < order.index("查询订单")


def test_topological_order_handles_chain() -> None:
    """三段链式依赖按顺序排。"""
    graph = DependencyGraph(
        [
            CaseNode(name="C", project="p", depends_on=["B"], order=0),
            CaseNode(name="B", project="p", depends_on=["A"], order=1),
            CaseNode(name="A", project="p", order=2),
        ]
    )

    order = graph.topological_order("p")

    assert order == ["A", "B", "C"]


def test_topological_order_keeps_independent_order() -> None:
    """没有依赖的用例保持原有相对顺序。"""
    graph = DependencyGraph(
        [
            CaseNode(name="X", project="p", order=0),
            CaseNode(name="Y", project="p", order=1),
            CaseNode(name="Z", project="p", order=2),
        ]
    )

    assert graph.topological_order("p") == ["X", "Y", "Z"]


def test_topological_order_raises_on_cycle() -> None:
    """循环依赖要报错，不能死等。"""
    graph = DependencyGraph(
        [
            CaseNode(name="A", project="p", depends_on=["B"], order=0),
            CaseNode(name="B", project="p", depends_on=["A"], order=1),
        ]
    )

    with pytest.raises(DependencyError):
        graph.topological_order("p")


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------


def test_validate_detects_missing_dependency() -> None:
    """依赖一个不存在的用例要报错。"""
    graph = DependencyGraph(
        [CaseNode(name="A", project="p", depends_on=["不存在的用例"], order=0)]
    )

    problems = graph.validate("p")

    assert any("不存在" in p for p in problems)


def test_validate_detects_cycle() -> None:
    """循环依赖要被发现。"""
    graph = DependencyGraph(
        [
            CaseNode(name="A", project="p", depends_on=["B"], order=0),
            CaseNode(name="B", project="p", depends_on=["A"], order=1),
        ]
    )

    problems = graph.validate("p")

    assert any("循环" in p for p in problems)


def test_validate_rejects_cross_project_dependency() -> None:
    """不能跨项目依赖。"""
    graph = DependencyGraph(
        [
            CaseNode(name="A", project="p1", depends_on=["B"], order=0),
            CaseNode(name="B", project="p2", order=1),
        ]
    )

    problems = graph.validate("p1")

    assert any("另一个项目" in p for p in problems)


def test_validate_passes_for_valid_graph() -> None:
    """正常的依赖关系不该报错。"""
    graph = DependencyGraph(
        [
            CaseNode(name="A", project="p", order=0),
            CaseNode(name="B", project="p", depends_on=["A"], order=1),
        ]
    )

    assert graph.validate("p") == []


# ---------------------------------------------------------------------------
# 并行分组
# ---------------------------------------------------------------------------


def test_parallel_groups_separate_dependent_cases() -> None:
    """有依赖关系的用例分到不同组（组间可并行，组内串行）。"""
    graph = DependencyGraph(
        [
            CaseNode(name="创建订单", project="p", order=0),
            CaseNode(name="查询订单", project="p", depends_on=["创建订单"], order=1),
            CaseNode(name="独立用例", project="p", order=2),
        ]
    )

    groups = graph.parallel_groups("p")

    # 第一组：创建订单 + 独立用例（互相没依赖）
    assert set(groups[0]) == {"创建订单", "独立用例"}
    # 第二组：查询订单
    assert groups[1] == ["查询订单"]


def test_parallel_groups_chain_has_three_levels() -> None:
    """三段链分成三层。"""
    graph = DependencyGraph(
        [
            CaseNode(name="C", project="p", depends_on=["B"], order=0),
            CaseNode(name="B", project="p", depends_on=["A"], order=1),
            CaseNode(name="A", project="p", order=2),
        ]
    )

    groups = graph.parallel_groups("p")

    assert len(groups) == 3


# ---------------------------------------------------------------------------
# 结果存取
# ---------------------------------------------------------------------------


def test_store_save_and_load(tmp_path: Path) -> None:
    """结果能存能读。"""
    store = DependencyStore(tmp_path)

    store.save("创建订单", {"outputs": {"create": {"code": 200}}, "status": "passed"})

    result = store.load("创建订单")
    assert result is not None
    assert result["outputs"]["create"]["code"] == 200
    assert result["status"] == "passed"


def test_store_load_missing_returns_none(tmp_path: Path) -> None:
    """没跑过的用例读出来是 None。"""
    store = DependencyStore(tmp_path)

    assert store.load("没跑过的用例") is None


def test_store_has(tmp_path: Path) -> None:
    """has 判断用例跑过没有。"""
    store = DependencyStore(tmp_path)

    assert store.has("A") is False
    store.save("A", {"outputs": {}})
    assert store.has("A") is True


def test_store_handles_chinese_names(tmp_path: Path) -> None:
    """中文用例名不能把文件名搞坏。"""
    store = DependencyStore(tmp_path)

    store.save("创建订单详情", {"outputs": {"x": 1}})

    assert store.load("创建订单详情") is not None


def test_store_wait_for_times_out(tmp_path: Path) -> None:
    """等不到就返回 None（不能无限等）。"""
    store = DependencyStore(tmp_path)

    result = store.wait_for("不存在的用例", timeout=0.5, interval=0.1)

    assert result is None


def test_store_wait_for_finds_existing(tmp_path: Path) -> None:
    """结果已经在了就立刻返回。"""
    store = DependencyStore(tmp_path)
    store.save("A", {"outputs": {"x": 1}})

    result = store.wait_for("A", timeout=1.0, interval=0.1)

    assert result is not None
    assert result["outputs"]["x"] == 1


def test_store_clear(tmp_path: Path) -> None:
    """清空后结果就没了。"""
    store = DependencyStore(tmp_path)
    store.save("A", {"outputs": {}})

    store.clear()

    assert store.load("A") is None


# ---------------------------------------------------------------------------
# 输出传递
# ---------------------------------------------------------------------------


def test_collect_case_outputs() -> None:
    """收集步骤变量和用例级变量。"""
    case_data = {
        "testcase": {
            "name": "创建订单",
            "variables": {"orderNo": "123"},
        }
    }

    result = collect_case_outputs("创建订单", case_data, {"create": {"code": 200}})

    assert result["name"] == "创建订单"
    assert result["outputs"]["create"]["code"] == 200
    assert result["variables"]["orderNo"] == "123"


def test_merge_dependencies_into_globals() -> None:
    """依赖输出能挂到全局变量上。"""
    global_vars: dict[str, Any] = {}

    merge_dependencies_into_globals(
        {"创建订单": {"outputs": {"create": {"result": {"orderNo": "N1"}}}}},
        global_vars,
    )

    assert global_vars["创建订单"]["create"]["result"]["orderNo"] == "N1"


def test_merge_ignores_malformed_payload() -> None:
    """数据不对时不能炸。"""
    global_vars: dict[str, Any] = {}

    merge_dependencies_into_globals({"X": "不是字典"}, global_vars)
    merge_dependencies_into_globals({"Y": {"outputs": "不是字典"}}, global_vars)

    assert global_vars == {}


# ---------------------------------------------------------------------------
# 结果过期 / 清理 / 本轮计划
# ---------------------------------------------------------------------------


def test_store_ignores_expired_result(tmp_path: Path) -> None:
    """过期的结果当作没有 —— 里面是上一轮的数据 id。"""
    store = DependencyStore(tmp_path, max_age=0.01)
    store.save("A", {"outputs": {"id": 1}})

    time.sleep(0.05)

    assert store.load("A") is None
    assert store.has("A") is True  # 文件还在，只是内容不认


def test_store_prune_removes_only_expired(tmp_path: Path) -> None:
    """prune 只删过期的，新的留着。"""
    store = DependencyStore(tmp_path, max_age=0.01)
    store.save("old", {"outputs": {}})
    time.sleep(0.05)
    store.save("new", {"outputs": {}})

    removed = store.prune()

    assert removed == 1
    assert store.load("old") is None
    assert store.load("new") is not None


def test_store_prune_with_zero_age_keeps_everything(tmp_path: Path) -> None:
    """``result_max_age: 0`` = 不过期，一个都不能删。"""
    store = DependencyStore(tmp_path, max_age=0)
    store.save("A", {"outputs": {}})

    assert store.prune() == 0
    assert store.load("A") is not None


def test_begin_session_records_plan(tmp_path: Path, monkeypatch) -> None:
    """开始新一轮时记下本轮用例名单，并能判断某条用例在不在计划里。"""
    from utils.cases import case_dependency

    monkeypatch.setattr(
        case_dependency, "get_store", lambda: DependencyStore(tmp_path, max_age=0)
    )

    case_dependency.begin_session(["A", "B"])

    assert case_dependency.has_session_plan() is True
    assert case_dependency.is_active_case("A") is True
    assert case_dependency.is_active_case("C") is False

    # 没登记计划时无从判断，不能因为"没在名单里"就报错
    case_dependency._SESSION_CASES.clear()
    assert case_dependency.is_active_case("随便一条") is True


def test_wait_timeout_from_env(monkeypatch) -> None:
    """等待超时可以用环境变量临时调；非法值退回配置/默认值，不能抛异常。"""
    from utils.cases.case_dependency import get_wait_timeout

    monkeypatch.setenv("APITEST_DEPENDENCY_TIMEOUT", "7")
    assert get_wait_timeout() == 7.0

    monkeypatch.setenv("APITEST_DEPENDENCY_TIMEOUT", "不是数字")
    assert get_wait_timeout() > 0


# ---------------------------------------------------------------------------
# 下游用例加载依赖（case_runner._load_dependency_outputs）
# ---------------------------------------------------------------------------


def _load(runner, deps, resolver):
    return runner._load_dependency_outputs(deps, resolver, "下游用例")


def test_dependency_outside_session_plan_fails_fast(tmp_path: Path, monkeypatch) -> None:
    """依赖用例不在本轮计划里 → 立刻报错，不能拿上一轮的结果凑合。"""
    from testcases.case_runner import CaseRunner
    from utils.cases import case_dependency
    from utils.cases.variable_resolver import VariableResolver

    monkeypatch.setattr(
        case_dependency, "get_store", lambda: DependencyStore(tmp_path, max_age=0)
    )
    case_dependency.begin_session(["别的用例"])
    monkeypatch.setenv("APITEST_DEPENDENCY_TIMEOUT", "1")

    runner = CaseRunner(env="test")
    resolver = VariableResolver(session_vars={}, global_vars={})

    with pytest.raises(AssertionError) as exc_info:
        _load(runner, ["没被选中的用例"], resolver)

    assert "不在本轮执行计划里" in str(exc_info.value)


def test_dependency_ignores_result_from_previous_run(tmp_path: Path, monkeypatch) -> None:
    """上一轮留下的结果不能当成本轮的（里面的数据 id 已经失效）。"""
    import time as _time

    from testcases.case_runner import CaseRunner
    from utils.cases import case_dependency
    from utils.cases.variable_resolver import VariableResolver

    store = DependencyStore(tmp_path, max_age=0)
    store.save("上游", {"outputs": {"x": 1}, "status": "passed"})
    monkeypatch.setattr(case_dependency, "get_store", lambda: store)
    monkeypatch.setenv("APITEST_DEPENDENCY_TIMEOUT", "1")

    # 本轮开始时间晚于结果写入时间
    case_dependency.begin_session(["上游"])
    case_dependency._SESSION_STARTED_AT = _time.time() + 1

    runner = CaseRunner(env="test")
    resolver = VariableResolver(session_vars={}, global_vars={})

    with pytest.raises(AssertionError) as exc_info:
        _load(runner, ["上游"], resolver)

    assert "上游" in str(exc_info.value)


def test_dependency_outputs_are_loaded(tmp_path: Path, monkeypatch) -> None:
    """本轮上游跑完的结果能正常加载到全局变量。"""
    from testcases.case_runner import CaseRunner
    from utils.cases import case_dependency
    from utils.cases.variable_resolver import VariableResolver

    monkeypatch.setattr(
        case_dependency, "get_store", lambda: DependencyStore(tmp_path, max_age=0)
    )
    case_dependency.begin_session(["上游"])
    case_dependency.get_store().save(
        "上游", {"outputs": {"create": {"data": {"id": 9}}}, "status": "passed"}
    )

    runner = CaseRunner(env="test")
    resolver = VariableResolver(session_vars={}, global_vars={})

    _load(runner, ["上游"], resolver)

    assert resolver.global_vars["上游"]["create"]["data"]["id"] == 9


def test_dependency_failed_upstream_is_reported(tmp_path: Path, monkeypatch) -> None:
    """上游失败时下游不能继续跑。"""
    from testcases.case_runner import CaseRunner
    from utils.cases import case_dependency
    from utils.cases.variable_resolver import VariableResolver

    monkeypatch.setattr(
        case_dependency, "get_store", lambda: DependencyStore(tmp_path, max_age=0)
    )
    case_dependency.begin_session(["上游"])
    case_dependency.get_store().save("上游", {"outputs": {}, "status": "failed"})

    runner = CaseRunner(env="test")
    resolver = VariableResolver(session_vars={}, global_vars={})

    with pytest.raises(AssertionError) as exc_info:
        _load(runner, ["上游"], resolver)

    assert "执行失败" in str(exc_info.value)
