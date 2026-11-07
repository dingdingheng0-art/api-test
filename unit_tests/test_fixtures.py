"""``framework/fixtures.py`` 的行为测试。

这些 fixture 原来一个用例都没用，所以坏了也没人发现。这里钉住三件事：

- ``global_vars`` 是**实时视图**，不是快照
- ``api_session`` / ``api_client`` 走的是**用例同一条登录链路**
  （``ProjectLoginHandler``），不是 ``ApiClient.from_config()`` 那条通用路线
- ``fresh_client`` 抓 uuid 时带着当前项目的档案

前两条是真踩过的坑：``ApiClient.from_config()`` 不认 ``login.type: custom``，
mall4j 配的正是 custom（字段名 ``userName``/``passWord``、密码 AES 加密），
用它登录必然失败；而 ``Globals.update()`` 在项目没加载时抛 ``KeyError``。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.infra.globals import Globals, GlobalsView


@pytest.fixture(autouse=True)
def _clean_globals() -> Any:
    """每个测试用一份干净的 Globals，跑完还原。"""
    original = Globals.get_data()
    Globals.clear()
    yield
    Globals.clear()
    for key, value in original.items():
        Globals.set(key, value)


def _code_of(fixture: Any) -> str:
    """fixture 函数去掉 docstring 后的源码。

    只查代码不查注释/docstring —— 那里会写着"不要用 ApiClient.from_config"，
    按纯文本匹配会把它自己当成违规。
    """
    import ast
    import inspect
    import textwrap

    func = getattr(fixture, "__wrapped__", fixture)
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    body = tree.body[0].body
    if body and isinstance(body[0], ast.Expr) and isinstance(
        getattr(body[0], "value", None), ast.Constant
    ):
        body = body[1:]  # 去掉 docstring
    return "\n".join(ast.unparse(node) for node in body)


# ---------------------------------------------------------------------------
# global_vars：实时视图
# ---------------------------------------------------------------------------


def test_live_view_sees_projects_loaded_later() -> None:
    """session 级 fixture 建好之后才加载的项目，视图里也要看得到。"""
    view = Globals.live_view()

    Globals.set("loaded_later", {"host": "https://x.invalid"})

    assert "loaded_later" in view
    assert view["loaded_later"]["host"] == "https://x.invalid"


def test_get_data_snapshot_misses_later_projects() -> None:
    """对照组：快照看不到后来才出现的顶层 key（这正是不能用它的原因）。"""
    snapshot = Globals.get_data()

    Globals.set("loaded_later", {"host": "x"})

    assert "loaded_later" not in snapshot


def test_live_view_writes_through_to_globals() -> None:
    """往视图里写，就是往 Globals 里写。"""
    view = Globals.live_view()

    view["mall4j"] = {"host": "h"}
    Globals.update("mall4j", "token", "T1")

    assert Globals.get("mall4j")["token"] == "T1"
    assert view["mall4j"]["token"] == "T1"

    del view["mall4j"]
    assert Globals.get("mall4j") is None


def test_live_view_behaves_like_a_mapping() -> None:
    view = Globals.live_view()
    Globals.set("a", 1)
    Globals.set("b", 2)

    assert sorted(view) == ["a", "b"]
    assert len(view) == 2
    assert dict(view) == {"a": 1, "b": 2}
    assert isinstance(view, GlobalsView)


# ---------------------------------------------------------------------------
# api_session / api_client：必须和用例走同一条登录链路
# ---------------------------------------------------------------------------


def test_api_session_uses_project_login_handler() -> None:
    """登录必须经由 ProjectLoginHandler —— 它会按 login.type 分发。

    直接调 ``ApiClient.from_config()`` 的话，``login.type: custom`` 的项目
    （mall4j）会走错路线，字段名和加密方式都对不上。
    """
    import framework.fixtures as fixtures

    code = _code_of(fixtures.api_session)

    assert "ProjectLoginHandler" in code
    assert "check_and_login_project" in code
    assert "ApiClient.from_config" not in code


def test_api_session_loads_project_config_before_login() -> None:
    """登录前要先把项目配置落进 Globals，否则 Globals.update() 抛 KeyError。"""
    import framework.fixtures as fixtures

    assert "ConfigManager.get_project_env_config" in _code_of(fixtures.api_session)


def test_api_session_reports_correct_env() -> None:
    """档案要按当前环境取 —— 不能退到默认环境（可能是 online）。"""
    import framework.fixtures as fixtures

    assert "ProjectRegistry.get(case_project, apienv)" in _code_of(fixtures.api_session)


def test_api_client_reads_token_from_globals() -> None:
    """token 从头名各不相同的 headers 里反查容易取空，统一从 Globals 取。"""
    import framework.fixtures as fixtures

    code = _code_of(fixtures.api_client)

    assert "Globals.get(case_project)" in code
    assert "ProjectRegistry.get(case_project, apienv)" in code


def test_fresh_client_passes_profile_to_uuid_fetch() -> None:
    """抓 uuid 要带当前项目的档案，否则抓到的是别的系统的凭证。"""
    import framework.fixtures as fixtures

    code = _code_of(fixtures.fresh_client)

    assert "fetch_uuid(profile=" in code
    assert "make_session(uuid, profile=" in code


# ---------------------------------------------------------------------------
# case_runner：只定义一次
# ---------------------------------------------------------------------------


def test_case_runner_fixture_not_shadowed() -> None:
    """``testcases/test_cases.py`` 不该再定义一份 case_runner。

    两处定义会让"改了一处没改另一处"，而且测试文件里的那份会盖掉
    ``framework/fixtures.py`` 的，别处引用时行为不一致。
    """
    import inspect

    import testcases.test_cases as test_module

    source = inspect.getsource(test_module)

    assert "def case_runner" not in source
    assert "CaseRunner" in source, "类型注解还要用它"
