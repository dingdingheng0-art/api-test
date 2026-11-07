"""框架的 fixture 层。

这些 fixture 给用例（和用户自己写的 pytest 测试）提供现成的能力：

- ``apienv``      —— 当前环境名
- ``case_runner`` —— 用例执行器
- ``api_client``  —— 带 token 的业务接口客户端（直接调接口用）
- ``api_session`` —— 已登录的 requests.Session
- ``global_vars`` —— 全局变量（实时视图）
- ``fresh_client``—— 全新 uuid 的会话（需要独立会话时用）

**登录只有一条链路**：需要登录态的 fixture 都走
``ProjectLoginHandler.check_and_login_project()`` —— 和 YAML/Excel 用例
在 ``CaseRunner.run_case()`` 里调的是同一个方法。按项目档案的
``login.type`` 分发（滑块 / 密码 / 自定义），token 缓存共用。

不要另起一条"通用登录"路线（比如直接 ``ApiClient.from_config()``）：
它不认 ``login.type``，换个系统就登不上。

用法（在用例里直接声明即可）::

    def test_something(api_client):
        info = api_client.current_user()
        assert info["code"] == 200
"""
from __future__ import annotations

from typing import Any

import pytest
import requests

from utils.infra.globals import Globals
from utils.logs.log_manager import get_logger

log = get_logger(__name__)


@pytest.fixture(scope="session")
def apienv(pytestconfig: pytest.Config) -> str:
    """当前测试环境名。"""
    return getattr(pytestconfig, "_apienv", "online")


@pytest.fixture(scope="session")
def case_runner(apienv: str):
    """用例执行器（整个 session 共用，登录态可复用）。"""
    from testcases.case_runner import CaseRunner

    return CaseRunner(env=apienv)


@pytest.fixture(scope="session")
def global_vars() -> Any:
    """全局变量（配置 + token 等）—— **实时视图**，不是快照。

    fixture 是 session 级的，只在整轮开始时建一次。如果返回
    ``Globals.get_data()`` 的浅拷贝，之后**新加载的项目**（顶层 key）
    用例里看不见。所以返回 ``live_view()``，每次读都回读 Globals。
    """
    return Globals.live_view()


@pytest.fixture(scope="session")
def case_project(pytestconfig: pytest.Config) -> str:
    """当前测试针对的项目名。

    命令行 ``--case-project`` 传了多个时，取第一个 ——
    fixture 是"单个项目"的登录态，多项目混跑时各自走 ``case_runner``。
    """
    projects = pytestconfig.getoption("--case-project", default=[]) or []
    if projects:
        return list(projects)[0]

    from utils.config.project_profile import ProjectRegistry

    return ProjectRegistry.default_project()


@pytest.fixture(scope="session")
def api_session(apienv: str, case_project: str) -> requests.Session:
    """已登录的会话。

    **登录走的是用例用的同一条链路**（``ProjectLoginHandler`` →
    ``Auth.login``）：按项目档案的 ``login.type`` 分发，滑块 / 密码 /
    自定义登录都走得通，token 缓存也能命中。

    不能直接调 ``ApiClient.from_config()`` —— 它只会"抓 uuid + 滑块 +
    内置账号密码登录"这一条通用路线，不认 ``login.type: custom``。
    mall4j 配的就是 custom（字段名 ``userName``/``passWord``、密码 AES
    加密），用通用路线会拿错字段名和加密方式登录，必然失败。
    """
    from testcases.project_login_handler import ProjectLoginHandler
    from utils.config.config_manager import ConfigManager
    from utils.config.project_profile import ProjectRegistry
    from utils.http.request_util import make_session

    profile = ProjectRegistry.get(case_project, apienv)

    # 项目配置要先落进 Globals —— 登录结果是通过 Globals.update() 写回去的，
    # 项目还没进 Globals 时 update 会抛 KeyError。
    ConfigManager.get_project_env_config(case_project, apienv)

    ProjectLoginHandler().check_and_login_project(case_project, apienv)

    info = Globals.get(case_project) or {}
    token_var = profile.token_var if profile else "token"
    session_var = profile.session_var if profile else "uuid"
    token = str(info.get(token_var) or "")
    session_id = str(info.get(session_var) or "")

    session = make_session(session_id or None, profile=profile)
    if profile:
        session.headers.update(profile.auth_headers(token, session_id))
    else:
        session.headers.update({"accesstoken": token, "uuid": session_id})

    log.info("api_session 就绪（token 前缀=%s...）", token[:16])
    yield session
    session.close()


@pytest.fixture(scope="session")
def api_client(api_session: requests.Session, case_project: str, apienv: str) -> Any:
    """带 token 的业务接口客户端。

    用法::

        def test_x(api_client):
            api_client.get("/admin/user/info")
    """
    from utils.config.project_profile import ProjectRegistry
    from utils.http.api_client import ApiClient

    # 环境要一起传 —— 不传的话会拿默认环境（可能是 online）的档案，
    # token 头名、host 都跟当前这轮跑的对不上。
    profile = ProjectRegistry.get(case_project, apienv)

    # token / 会话标识从 Globals 取（和 api_session 同一个来源），
    # 别去 session.headers 里反查 —— 头名各项目不同，反查容易取空。
    info = Globals.get(case_project) or {}
    token = str(info.get(profile.token_var if profile else "token") or "")
    session_id = str(info.get(profile.session_var if profile else "uuid") or "")

    return ApiClient(
        access_token=token, uuid=session_id, session=api_session, profile=profile
    )


@pytest.fixture
def fresh_client(apienv: str, case_project: str) -> requests.Session:
    """一个带**全新 uuid** 的会话。

    uuid 是一次性凭证，被消费过之后服务端对同一 uuid 的后续请求直接放行，
    会让"错误输入应该失败"这类断言假通过。需要独立会话时用这个。
    """
    from utils.config.project_profile import ProjectRegistry
    from utils.http.request_util import make_session
    from utils.slider.uuid_util import fetch_uuid

    # 不给 profile 会退到"默认项目"的登录页去抓 uuid，
    # 跑别的项目时抓到的是别的系统的凭证。
    profile = ProjectRegistry.get(case_project, apienv)
    uuid = fetch_uuid(profile=profile)
    if not uuid:
        pytest.fail("没抓到新 uuid（服务端限流？等 30 秒再试）")

    session = make_session(uuid, profile=profile)
    yield session
    session.close()
