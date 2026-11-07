"""示例：第二个系统的登录适配器。

**这个文件是模板** —— 接入你自己的系统时照着抄一份，改掉里面的逻辑。

框架本身已经能处理两种常见情况，不用写这个文件：

1. **直接账号密码登录**（最常见）—— 在 ``configs/config.yaml`` 里配：

   .. code-block:: yaml

       projects:
         你的项目:
           online:
             host: "https://api.你的系统.com"
             is_need_login: true
             auth:
               token_header: "token"          # 你的鉴权头叫什么
             login:
               type: password                  # 不用滑块
               path: "/api/login"
               method: "POST"
               username_field: "account"       # 你的表单字段名
               password_field: "pwd"
               password_md5: false              # 密码要不要先 MD5
               data:
                 username: "${YOUR_USERNAME:test}"
                 password: "${YOUR_PASSWORD:123456}"

2. **走滑块** —— 拼图滑块那套，只要配 ``login.type: slider`` 和滑块路径。

**什么时候才需要写这个文件**

登录链路比较特别的时候，比如：

- 要先调一个接口拿临时票据，再带着票据去登录
- 登录要带签名（时间戳 + 密钥算出来的）
- 登录后 token 在响应的嵌套位置里（这种其实用配置里的
  ``login.token_path`` 就够）
- 要走 OAuth / SSO 跳转

**函数约定**

签名固定是 ``函数名(project_name, login_config, env)``，
返回的字典里必须要有 ``token`` 字段（框架会把它写进全局变量）。

.. code-block:: python

    def login(project_name, login_config, env):
        ...
        return {"token": "...", "uuid": "...", "headers": {...}}

在配置里指过去：

.. code-block:: yaml

    login:
      type: custom
      custom_method: "adapters.demo.login"
"""

from __future__ import annotations

from typing import Any

from utils.logs.log_manager import log
from utils.config.project_profile import ProjectRegistry


def login(project_name: str, login_config: dict[str, Any], env: str) -> dict[str, Any]:
    """示例登录实现。

    这里演示的是"先拿临时票据 → 再登录"这种两段式流程 ——
    换成你的系统时，把 ``_fetch_ticket`` 里的请求换成你真实的接口即可。

    Args:
        project_name: 项目名
        login_config: 配置里的 ``login`` 段
        env: 环境名

    Returns:
        ``{"token": ..., "uuid": ..., "headers": {...}}``
    """
    profile = ProjectRegistry.require(project_name, env)
    data = dict((login_config or {}).get("data") or {})

    username = data.get("username") or profile.username
    password = data.get("password") or profile.password

    log.info("[%s] 开始自定义登录（用户名=%s）", project_name, username)

    # 第一步：拿临时票据
    ticket = _fetch_ticket(profile, username)

    # 第二步：带票据登录
    token = _do_login(profile, username, password, ticket)

    session_id = ticket
    return {
        "token": token,
        "uuid": session_id,
        "headers": profile.auth_headers(token, session_id),
    }


def _fetch_ticket(profile: Any, username: str) -> str:
    """示例：先调一个接口拿临时票据。

    换成你系统真实的接口即可。这里不真的发请求，直接返回一个占位值。
    """
    # 真实写法大概长这样：
    #
    # from utils.http.request_util import api_post, make_session
    # session = make_session(profile=profile)
    # resp = api_post(
    #     session,
    #     "/api/prepareLogin",
    #     json_body={"username": username},
    #     profile=profile,
    # )
    # return str((resp.get("result") or {}).get("ticket", ""))

    log.info("（示例）获取临时票据，username=%s", username)
    return f"demo-ticket-{username}"


def _do_login(profile: Any, username: str, password: str, ticket: str) -> str:
    """示例：用票据换 token。

    真实写法大概长这样：
    #
    # from utils.http.request_util import api_post, make_session
    # session = make_session(profile=profile)
    # resp = api_post(
    #     session,
    #     profile.login_path or "/api/login",
    #     json_body={
    #         profile.username_field: username,
    #         profile.password_field: password,
    #         "ticket": ticket,
    #     },
    #     profile=profile,
    # )
    # result = resp.get("result") or {}
    # return str(result.get("token", ""))

    """

    log.info("（示例）用票据换 token")
    return f"demo-token-{username}"
