"""登录阶段的可见性与归因。

登录发生在用例体里（``CaseRunner.run_case`` → ``_login_projects``），
既不是 fixture 也不是普通 step。于是有两个后果，这里各钉一条：

1. **报告上看不见** —— 得包一层 ``allure.step``，否则登录挂了，
   Allure 里只有用例体的一行 ``RuntimeError``，看不出"卡在登录"。
2. **归因说不出上下文** —— ``_diagnose_failure`` 靠 ``last_step_info``
   拼"哪一步出的错"。登录时 ``last_step_info`` 还是空字典，
   结论会退化成光秃秃的"框架异常：RuntimeError"，指向"脚本写错了"，
   而实际可能是服务端登录接口挂了（A00005）。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from testcases.case_runner import CaseRunner


def _case(*projects: str) -> dict[str, Any]:
    """一条带 steps 的用例（``_login_projects`` 收的是内层 case，不含 testcase 壳）。"""
    return {
        "name": "跨项目用例",
        "steps": [
            {"id": f"s{i}", "operation_type": "noop", "project": p}
            for i, p in enumerate(projects)
        ],
    }


class _RecordingAllure:
    """替身：只记下 `step()` 用了什么标题。"""

    def __init__(self) -> None:
        self.titles: list[str] = []

    def step(self, title: str) -> Any:
        import contextlib

        self.titles.append(title)
        return contextlib.nullcontext()


@pytest.fixture()
def runner(monkeypatch: pytest.MonkeyPatch) -> CaseRunner:
    """不真登录的运行器。"""
    r = CaseRunner(env="test")
    r.login_handler.check_and_login_project = lambda *_a, **_kw: None  # type: ignore[method-assign]
    return r


# ---------------------------------------------------------------------------
# 1. 登录要出现在报告里
# ---------------------------------------------------------------------------


def test_login_is_wrapped_in_allure_step(runner: CaseRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    """每个项目的登录都要有一个同名步骤，方便在报告里一眼看到。"""
    fake = _RecordingAllure()
    monkeypatch.setattr("testcases.case_runner.allure", fake)

    runner._login_projects(_case(), "mall4j")

    assert fake.titles == ["登录 mall4j"]


def test_every_involved_project_gets_its_own_step(
    runner: CaseRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一个用例跨多个项目调接口时，每个项目各自登录一次、各自一个步骤。"""
    fake = _RecordingAllure()
    monkeypatch.setattr("testcases.case_runner.allure", fake)

    logged: list[str] = []
    runner.login_handler.check_and_login_project = (  # type: ignore[method-assign]
        lambda name, _env: logged.append(name)
    )

    runner._login_projects(_case("other"), "mall4j")

    # set() 顺序不定，比集合
    assert set(fake.titles) == {"登录 mall4j", "登录 other"}
    assert set(logged) == {"mall4j", "other"}


# ---------------------------------------------------------------------------
# 2. 登录失败要能归因到"登录"
# ---------------------------------------------------------------------------


def test_login_records_stage_before_calling_handler(runner: CaseRunner) -> None:
    """登录**之前**就要写好 last_step_info —— 登录自己抛异常时来不及补。"""
    seen: dict[str, Any] = {}

    def _explode(name: str, _env: str) -> None:
        seen.update(runner.last_step_info)
        raise RuntimeError("登录接口返回 A00005")

    runner.login_handler.check_and_login_project = _explode  # type: ignore[method-assign]

    with pytest.raises(RuntimeError):
        runner._login_projects(_case(), "mall4j")

    assert seen.get("stage") == "login"
    assert seen.get("step_id") == "mall4j"


def test_login_failure_is_attributed_to_login(runner: CaseRunner) -> None:
    """登录抛异常时，归因结论里要写着"登录"。"""
    from testcases.test_cases import _diagnose_failure

    def _explode(_name: str, _env: str) -> None:
        raise RuntimeError("登录接口返回 A00005")

    runner.login_handler.check_and_login_project = _explode  # type: ignore[method-assign]

    exc = RuntimeError("登录接口返回 A00005")
    try:
        runner._login_projects(_case(), "mall4j")
    except RuntimeError as raised:
        exc = raised

    diag = _diagnose_failure(runner, exc)

    assert "登录" in diag.category
    assert "RuntimeError" in diag.category


def test_login_a00005_is_blamed_on_the_service(runner: CaseRunner) -> None:
    """登录报 A00005 是服务端的锅，不能说成"脚本问题"。

    这是最容易带偏排查方向的一条：登录适配器拿到 ``success=false``
    抛的是 ``RuntimeError``，异常类型上和脚本 bug 长得一模一样。
    归成"改脚本"，人就会去翻自动化代码，而真正要修的是后端。
    """
    from testcases.test_cases import _diagnose_failure

    runner.login_handler.check_and_login_project = (  # type: ignore[method-assign]
        lambda _n, _e: (_ for _ in ()).throw(
            RuntimeError("登录失败：code=A00005 msg=服务器内部错误")
        )
    )

    exc: BaseException = RuntimeError("")
    try:
        runner._login_projects(_case(), "mall4j")
    except RuntimeError as raised:
        exc = raised

    diag = _diagnose_failure(runner, exc)

    assert diag.attribution == "服务问题"
    assert "登录" in diag.category
    assert diag.server_code == "A00005"


def test_login_wrong_password_is_case_data(runner: CaseRunner) -> None:
    """账号密码不对是**用例配置**问题，不是服务端也不是脚本。"""
    from testcases.test_cases import _diagnose_failure

    runner.login_handler.check_and_login_project = (  # type: ignore[method-assign]
        lambda _n, _e: (_ for _ in ()).throw(
            RuntimeError("登录失败：code=A00014 msg=密码不正确")
        )
    )

    exc: BaseException = RuntimeError("")
    try:
        runner._login_projects(_case(), "mall4j")
    except RuntimeError as raised:
        exc = raised

    diag = _diagnose_failure(runner, exc)

    assert diag.attribution == "用例数据问题"
    assert diag.server_code == "A00014"


def test_login_connection_error_is_environment(runner: CaseRunner) -> None:
    """连不上服务是环境问题 —— 提示里要说"服务起没起"，不是"改脚本"。"""
    from testcases.test_cases import _diagnose_failure

    runner.login_handler.check_and_login_project = (  # type: ignore[method-assign]
        lambda _n, _e: (_ for _ in ()).throw(
            ConnectionError("Max retries exceeded with url: /_admin/adminLogin")
        )
    )

    exc: BaseException = ConnectionError("")
    try:
        runner._login_projects(_case(), "mall4j")
    except ConnectionError as raised:
        exc = raised

    diag = _diagnose_failure(runner, exc)

    assert diag.attribution == "环境问题"
    assert "连不上" in diag.category


def test_non_login_stage_still_blames_the_script(runner: CaseRunner) -> None:
    """非登录阶段的框架异常仍然归"脚本问题" —— 别把上面的规则扩大化。"""
    from testcases.test_cases import _diagnose_failure

    runner.last_step_info = {"stage": "step", "step_id": "s1"}
    diag = _diagnose_failure(runner, KeyError("data"))

    assert diag.attribution == "脚本问题"


def test_login_clears_step_info_for_the_next_case(runner: CaseRunner) -> None:
    """登录写下的 stage=login 不能留到下一条用例的步骤失败上。

    ``run_case`` 开头会把 ``last_step_info`` 清空，所以只要确认
    "清空"发生在登录之前——否则上一条的 `login` 会污染下一条。
    """
    from testcases.test_cases import _diagnose_failure

    runner._login_projects(_case(), "mall4j")
    assert runner.last_step_info.get("stage") == "login"

    runner.last_step_info = {}
    diag = _diagnose_failure(runner, RuntimeError("boom"))
    assert "登录" not in diag.category
