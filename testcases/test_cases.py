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


def test_api_case(
    yaml_case: dict[str, Any],
    case_runner: CaseRunner,
    request: pytest.FixtureRequest,
) -> None:
    """执行一条用例（YAML / Excel 都走这里）。

    ``yaml_case`` 里包含：项目名、文件路径、用例数据、参数组变量。
    """
    project = yaml_case["project"]
    yaml_file = Path(yaml_case["file"])
    data = yaml_case["case"]
    param_vars = yaml_case["params"]
    param_name = yaml_case["param_name"]

    case = data["testcase"]

    # 层级：收集阶段已经算好了（pytest_plugin 里加 allure label 时算的）
    hierarchy = yaml_case.get("hierarchy") or CaseHierarchy.resolve(
        case, project, yaml_file, paths.CASES_ROOT
    )

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

    case_name = str(case.get("name") or "")

    try:
        step_vars = case_runner.run_case(
            data, project, param_vars=param_vars, module=module
        )
    except Exception as exc:
        # 归因：告诉人这条失败该找谁（服务端 / 写用例的人 / 改脚本的人）
        diag = _diagnose_failure(case_runner, exc, module=module)
        # 带上用例编号 —— 自愈流程拿到归因后要能直接定位到这条用例。
        # 没有它就只能靠用例名猜，而同名用例（「查询地区详情」）会猜错。
        diag.case_id = str(case.get("case_id") or "").strip()

        # 失败时把请求 / 响应贴进报告，方便回看
        request_log = case_runner.last_request_log
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
        _record_dependency_result(case_name, data, None, status="failed", error=exc)

        # 把归因结论放到异常信息最前面 —— 报告的失败列表直接显示这个
        from utils.infra.failure_classifier import enrich_message, register_failure

        register_failure(request.node.nodeid, module, diag)
        raise enrich_message(exc, diag) from exc

    # 成功：把步骤变量存下来，供依赖它的下游用例引用
    _record_dependency_result(case_name, data, step_vars, status="passed")


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
    except Exception:  # noqa: BLE001 - 记录失败不该影响用例本身的判定
        pass
