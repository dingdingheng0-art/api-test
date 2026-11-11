"""pytest 测试入口：这里把数据驱动的用例真正跑起来。

每个用例（YAML 或 Excel）对应这里的一个测试项（由 pytest_plugin 参数化注入）。

.. note::
   函数名**不能带 "yaml"** —— 报告里显示的就是这个函数名。
   Excel 用例也走这个函数，名字里带 yaml 会让人误以为跑的都是 YAML 用例。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from framework.allure_util import allure, attach_error, attach_json, attach_text
from framework.case_param import format_params
from testcases.case_hierarchy import CaseHierarchy
from testcases.case_runner import CaseRunner
from utils import paths
from utils.logs.log_manager import log


@pytest.fixture
def case_lifecycle(
    request: pytest.FixtureRequest,
    case_runner: CaseRunner,
) -> Any:
    """用例生命周期：登录 + 前置 → （用例体）→ 后置清理。

    **为什么前置/后置要放进 fixture**

    Allure 的 **Set up / Tear down** 区块只认 pytest fixture 的生命周期；
    在用例体里执行的步骤一律进 **Test body**。以前前置/后置都由
    ``CaseRunner.run_case`` 在用例体里跑，报告上就只看到「前置 建数据」
    这种普通步骤，Set up / Tear down 里只有 apienv / case_runner 这些
    空 fixture。

    现在拆成 fixture + 用例体两段：**前置和清理进 Set up / Tear down，
    步骤留在 Test body**。
    """
    yaml_case = _yaml_case_of(request)
    if not isinstance(yaml_case, dict):
        # 理论上不会发生（只有 test_api_case 用这个 fixture）
        return

    # 报告元信息（标题/描述/标签）在 fixture 里就设好 ——
    # 否则前置失败时用例体不执行，报告上只剩一个光秃秃的函数名。
    _apply_allure_meta(yaml_case, request)

    project, data, param_vars, case, hierarchy = _unpack_case(yaml_case)
    module = hierarchy.get("feature", "")

    # 正常路径的清理入口：fixture 正常结束后由 pytest 调用。
    # 用具名函数而不是 lambda —— allure 报告里 Tear down 显示的
    # 就是这个函数名（``case_lifecycle::_teardown_case``），
    # lambda 会显示成 ``<lambda>``。
    def _teardown_case() -> None:
        _finish_case(request, case_runner, case, data)

    request.addfinalizer(_teardown_case)

    try:
        case_runner.prepare(data, project, param_vars=param_vars, module=module)
    except Exception as exc:  # noqa: BLE001
        # 前置失败**不能在这里抛**，交给用例体去抛。
        #
        # 在这里抛（= fixture setup 失败）会连着毁掉三样东西：
        #
        # 1. allure 的 ``pytest_fixture_setup`` 和 ``pytest_runtest_setup``
        #    都是旧式 hookwrapper，异常会被 throw 进生成器 —— ``yield``
        #    之后的收尾全部跳过：before-fixture 没关、容器没写盘、
        #    没登记成用例的 child。结果是 **Set up 区块整个消失**，
        #    前置里执行的步骤、失败时贴的请求响应全看不见。
        # 2. ``request.addfinalizer`` 注册的清理也不会执行 ——
        #    pytest 的 ``FixtureDef.finish`` 发现 fixture 没产出缓存值
        #    就直接返回，finalizer 列表原样丢弃。造了一半的数据没人清。
        # 3. 重试插件也就没机会按归因判断要不要重试。
        #
        # 记下来，让 ``test_api_case`` 在用例体里抛：Set up 完整可见、
        # Tear down 照常执行、失败归因和附件都挂在用例上。
        case_runner.prepare_error = exc

    # 不用 yield：清理已经通过 ``request.addfinalizer`` 登记，
    # 普通 fixture 同样会在用例结束后执行 finalizer。用 yield 的话
    # pytest 还会额外注册一个空的 generator 收尾，Allure 的 Tear down
    # 里就会多一个没有实际动作的 ``case_lifecycle::teardown`` 节点。


def test_api_case(
    yaml_case: dict[str, Any],
    case_runner: CaseRunner,
    case_lifecycle: Any,
    request: pytest.FixtureRequest,
) -> None:
    """执行一条用例的**步骤**（登录/前置/后置由 case_lifecycle 负责）。

    ``yaml_case`` 里包含：项目名、文件路径、用例数据、参数组变量。
    """
    _project, data, _param_vars, case, hierarchy = _unpack_case(yaml_case)
    module = hierarchy.get("feature", "")

    # 标题 / 描述要在这里**再设一次**。
    #
    # allure 的 ``pytest_runtest_setup`` 是个 hookwrapper：它在所有 fixture
    # 跑完之后（也就是我们 fixture 里设好标题之后）用
    # ``item.name`` 和测试函数的 docstring 覆盖 test_result 的
    # name / description —— 不补这一下，报告上就只剩
    # ``test_api_case[...]`` 和一句框架说明。
    #
    # 标签 / 参数 / 用户属性不重复设置（allure 的 add_label 是追加，
    # 设两遍会出现重复标签）。
    _apply_allure_meta(yaml_case, request, full=False)

    # 前置失败（见 case_lifecycle 里的说明）：steps 不执行，
    # 但失败要在这里抛 —— 报告上 Set up 才看得见。
    prepare_error = getattr(case_runner, "prepare_error", None)
    if prepare_error is not None:
        raise _handle_failure(
            case_runner, prepare_error, case, data, module, request
        ) from prepare_error

    try:
        case_runner.execute()
    except Exception as exc:
        raise _handle_failure(
            case_runner, exc, case, data, module, request
        ) from exc


def _yaml_case_of(request: pytest.FixtureRequest) -> dict[str, Any] | None:
    """从当前测试项的 callspec 里取 ``yaml_case``。

    不直接在 fixture 参数里写 ``yaml_case``：它是
    ``pytest_generate_tests`` 参数化出来的名字，fixture 直接依赖
    依赖注入顺序不够稳；callspec 是 pytest 权威来源。
    """
    callspec = getattr(request.node, "callspec", None)
    params = getattr(callspec, "params", None) or {}
    value = params.get("yaml_case")
    return value if isinstance(value, dict) else None


def _unpack_case(
    yaml_case: dict[str, Any],
) -> tuple[str, dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    """拆出 (项目, 用例数据, 参数组, testcase, 层级)。"""
    project = yaml_case["project"]
    data = yaml_case["case"]
    param_vars = yaml_case["params"]
    case = data["testcase"]
    # 层级：收集阶段已经算好了（pytest_plugin 里加 allure label 时算的）
    hierarchy = yaml_case.get("hierarchy") or CaseHierarchy.resolve(
        case, project, Path(yaml_case["file"]), paths.CASES_ROOT
    )
    return project, data, param_vars, case, hierarchy


def _apply_allure_meta(
    yaml_case: dict[str, Any], request: pytest.FixtureRequest, *, full: bool = True
) -> None:
    """设置报告的标题 / 描述 / 层级 / 参数（fixture 阶段就设好）。

    Args:
        full: True = 标题、描述、标签、参数全设（fixture setup 里用）；
            False = 只补标题和描述（用例体里用 —— allure 在 setup 结束后
            会覆盖这两项，标签则是**追加**语义，不能重复设）。
    """
    project, _data, param_vars, case, hierarchy = _unpack_case(yaml_case)
    yaml_file = Path(yaml_case["file"])
    param_name = yaml_case["param_name"]

    title = hierarchy["title"]
    if param_name:
        title = f"{title} [{param_name}]"
    allure.dynamic.title(title)

    # 用例描述。
    #
    # 不设的话，Allure 会去抓 pytest 函数的 docstring ——
    # 报告里就全是"执行一条用例（YAML / Excel 都走这里）"，
    # 看报告的人完全不知道这条用例在验证什么。
    #
    # Excel 用例的描述来自「用例标题」列里 "接口名 - 场景说明" 的后半段
    # （比如"（正常请求）"、"（查不到数据）"），YAML 用例则是用例自带的
    # description 字段。
    description = str(case.get("description") or "").strip()
    if description:
        allure.dynamic.description(description)

    if not full:
        return

    if hierarchy.get("epic"):
        allure.dynamic.epic(hierarchy["epic"])
    if hierarchy.get("feature"):
        allure.dynamic.feature(hierarchy["feature"])
    if hierarchy.get("story"):
        allure.dynamic.story(hierarchy["story"])

    request.node.user_properties.append(("yaml_file", str(yaml_file)))
    request.node.user_properties.append(("project", project))
    # 用例来源：Excel 还是 YAML —— 报告里一眼能看出这条是哪来的
    source = "Excel" if yaml_file.suffix.lower() in (".xlsx", ".xlsm") else "YAML"
    request.node.user_properties.append(("source", source))
    allure.dynamic.label("用例来源", source)
    # 模块也记一份，报告里按模块筛选用得上
    #
    # ``feature`` 是中文标签（"商品属性"），看报告的人认得；
    # ``module`` 是目录名（"admin_attribute"），写用例的人找文件用。
    # 两个都留着 —— 报告里显示中文，排查时能按目录名定位。
    module = hierarchy.get("feature", "")
    raw_module = hierarchy.get("module", "")
    if module:
        request.node.user_properties.append(("module", module))
    if raw_module:
        request.node.user_properties.append(("模块目录", raw_module))
    if param_vars:
        # 只显示参数名和值，不显示 __param_name__ 这个内部字段。
        # 用 format_params 拼成 "searchType=LAST_SEVEN" 这种文字，
        # 避免报告里出现 {'searchType': 'LAST_SEVEN'} 这种字典写法。
        shown = format_params(param_vars)
        if shown:
            request.node.user_properties.append(("params", shown))
            allure.dynamic.parameter("参数组", shown)


def _finish_case(
    request: pytest.FixtureRequest,
    case_runner: CaseRunner,
    case: dict[str, Any],
    data: dict[str, Any],
) -> None:
    """fixture finalizer：后置清理 + 依赖结果落盘。

    成功结果必须等清理跑完再记 —— 清理失败时用例算 error，
    下游依赖不能看到 "passed"。
    """
    case_runner.cleanup()

    rep = getattr(request.node, "rep_call", None)
    body_passed = rep is None or not getattr(rep, "failed", False)
    # setup 没跑成功时 body_ran 为 False，失败结果已经由 _handle_failure
    # 记过了，这里不能覆盖成 passed。
    if case_runner.body_ran and body_passed:
        _record_dependency_result(
            str(case.get("name") or ""),
            data,
            case_runner.session_vars,
            status="passed",
        )


def _handle_failure(
    case_runner: CaseRunner,
    exc: Exception,
    case: dict[str, Any],
    data: dict[str, Any],
    module: str,
    request: pytest.FixtureRequest,
) -> Exception:
    """失败归因 + 报告附件 + 依赖结果落盘，返回要抛出的异常。"""
    # 归因：告诉人这条失败该找谁（服务端 / 写用例的人 / 改脚本的人）
    diag = _diagnose_failure(case_runner, exc, module=module)
    # 带上用例编号 —— 自愈流程拿到归因后要能直接定位到这条用例。
    # 没有它就只能靠用例名猜，而同名用例（「查询地区详情」）会猜错。
    diag.case_id = str(case.get("case_id") or "").strip()

    # 失败时把请求 / 响应贴进报告，方便回看。
    # 用 current_request_log() 而不是 last_request_log —— 前置失败时
    # cleanup 还没跑，last_request_log 还是上一条用例的。
    request_log = case_runner.current_request_log()
    if request_log:
        attach_text(request_log, name="请求响应记录")

    # 归因结论单独贴一个附件 —— 报告里不用点进去就知道原因
    attach_json(diag.to_dict(), name="失败归因")

    # 校验证据（数据库/缓存/MQ 查到了什么）—— 失败时最想看的就是这个
    evidence = getattr(case_runner.assert_handler, "evidence", None)
    if evidence:
        attach_json(evidence, name="校验证据")

    # 异常详情（AI 缺陷分析会读这个）
    attach_error(exc, name="异常详情")

    # 记下失败 —— 依赖这条用例的下游会知道「上游挂了」
    _record_dependency_result(
        str(case.get("name") or ""), data, None, status="failed", error=exc
    )

    # 把归因结论放到异常信息最前面 —— 报告的失败列表直接显示这个
    from utils.infra.failure_classifier import enrich_message, register_failure

    register_failure(request.node.nodeid, module, diag)
    return enrich_message(exc, diag)


def _diagnose_failure(
    case_runner: CaseRunner,
    exc: Exception,
    module: str = "",
):
    """判断这条失败是「服务问题」「用例数据问题」还是「脚本问题」。

    **为什么要单独判**

    断言信息只说 ``[success] 期望等于 True，实际 False`` —— 光看它
    判断不出该找谁。结合**服务端实际返回了什么**才能分清：

    - 服务端返回 A00005 / 403 → 服务端的锅
    - 服务端说必填参数缺失   → 用例数据的锅
    - 框架自己抛了 KeyError  → 脚本的锅
    """
    from utils.infra.failure_classifier import (
        classify_exception,
        classify_response_failure,
    )

    step_info = getattr(case_runner, "last_step_info", None) or {}
    response = step_info.get("response")
    meta = step_info.get("meta") or {}
    method = str(step_info.get("method") or "")
    url = str(step_info.get("url") or "")

    # 有步骤信息就把它来自哪个阶段一起带出去 ——
    # setup / teardown / login 的失败和 steps 的失败，排查方向完全不同。
    stage = str(step_info.get("stage") or "step")

    # 断言类失败 → 看服务端返回了什么
    if isinstance(exc, AssertionError) and response is not None:
        return classify_response_failure(
            response,
            meta,
            module=module,
            method=method,
            url=url,
            assertion=str(exc),
        )

    # 其它异常 → 框架自己抛的
    context = ""
    if step_info.get("step_id"):
        label = {"setup": "前置", "teardown": "后置", "login": "登录"}.get(stage, "步骤")
        context = f"{label} {step_info['step_id']}"
    return classify_exception(
        exc, module=module, method=method, url=url, context=context, stage=stage
    )


def _record_dependency_result(
    case_name: str,
    case_data: dict[str, Any],
    step_vars: dict[str, Any] | None,
    status: str = "passed",
    error: BaseException | None = None,
) -> None:
    """把一条用例的执行结果存起来，供下游用例读取。

    **为什么要存到磁盘**：并行执行时每个 worker 是独立进程，
    内存不共享。下游用例在另一个进程里，只能从磁盘读。
    """
    if not case_name:
        return

    try:
        from utils.cases.case_dependency import collect_case_outputs, get_store

        payload = collect_case_outputs(case_name, case_data, step_vars or {})
        payload["status"] = status
        if error is not None:
            payload["error"] = str(error)

        get_store().save(case_name, payload)
    except Exception as exc:  # noqa: BLE001 - 记录失败不该影响用例本身的判定
        # 但必须留痕：下游用例会认为它"不在本轮计划里"并直接报错，
        # 没有这行日志就完全看不出真实原因。
        log.warning("依赖结果落盘失败（下游用例可能误判为未执行）: %s", exc)
