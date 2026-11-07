"""项目登录：拿到 token 后塞进全局变量。

框架只认一个约定：登录方法返回的字典里必须有 ``token`` 字段，
它会被写进 ``Globals[project_name]["token"]``，
这样 YAML 用例里就能写 ``{{mall4j.token}}``。

**登录方式由配置决定**（见 ``utils/project_profile.py``）：

- ``login.type: slider``   —— 走滑块登录（抓 uuid → 识别缺口 → 校验 → 登录）
- ``login.type: custom``   —— 调你在 ``login.custom_method`` 里指定的函数
- ``login.type: password`` —— 直接用账号密码调登录接口（没有滑块的系统）

默认是 ``slider``，所以老配置不用改。
"""
from __future__ import annotations

import time
from typing import Any

from utils.infra.globals import Globals
from utils.logs.log_manager import log
from utils.config.project_profile import (
    LOGIN_TYPE_CUSTOM,
    LOGIN_TYPE_PASSWORD,
    LOGIN_TYPE_SLIDER,
    ProjectProfile,
    ProjectRegistry,
)
from utils.infra.token_cache import TokenCache


class Auth:
    """登录方法集合。"""

    def login(self, project_name: str, login_config: dict[str, Any], env: str) -> dict[str, Any] | None:
        """按配置选登录方式，返回 ``{"token": ..., "uuid": ..., "headers": {...}}``。

        **会先看缓存**：token 还有效就直接复用，省掉一次滑块 + 登录（约 6 秒）。

        **并行时还会协调**：多个 worker 同时要登录时，只让一个真的去登录，
        其它 worker 等它登录完直接复用结果 —— 见 :func:`utils.parallel.coordinated_login`。
        """
        # 1) 先试缓存
        cached = TokenCache.get(project_name)
        if cached:
            return {
                "token": cached["token"],
                "refresh_token": cached.get("refresh_token", ""),
                "uuid": cached.get("uuid", ""),
                "headers": cached.get("headers", {}),
                "from_cache": True,
            }

        # 2) 没缓存，走真实登录（并行时加锁协调）
        from utils.infra.parallel import coordinated_login

        return coordinated_login(project_name, lambda: self._do_login(project_name, login_config, env))

    def fresh_login(
        self, project_name: str, login_config: dict[str, Any], env: str
    ) -> dict[str, Any] | None:
        """强制真实登录一次，**不读也不写** token 缓存。

        用于「刷新令牌」这类会消耗登录态的用例：它需要一份自己的、
        未被使用过的 refreshToken，用完即弃，不能影响共享缓存。
        """
        return self._do_login(project_name, login_config, env, save_cache=False)

    def _do_login(
        self,
        project_name: str,
        login_config: dict[str, Any],
        env: str,
        save_cache: bool = True,
    ) -> dict[str, Any] | None:
        """真正执行登录（挑方式、存缓存）。"""
        profile = ProjectRegistry.get(project_name, env)
        login_type = profile.login_type if profile else LOGIN_TYPE_SLIDER

        if login_type == LOGIN_TYPE_CUSTOM:
            result = self._call_custom(
                profile.custom_method if profile else "",
                project_name,
                login_config,
                env,
            )
        elif login_type == LOGIN_TYPE_PASSWORD:
            result = self._password_login(project_name, login_config, env)
        else:
            result = self.slider_login(project_name, login_config, env)

        # 3) 存缓存
        if save_cache and result and result.get("token"):
            TokenCache.save(
                project_name,
                result["token"],
                result.get("uuid", ""),
                result.get("headers"),
                result.get("refresh_token", ""),
            )

        return result

    # ---------------- 滑块登录 ----------------

    def slider_login(
        self,
        project_name: str,
        login_config: dict[str, Any],
        env: str,
        max_timeout: float = 300.0,
    ) -> dict[str, Any] | None:
        """走完整链路登录：抓新 uuid → 滑块 → 登录 → 返回 token。

        登录成功后，把 token 和会话标识一起放进全局变量，
        因为业务接口的鉴权头名每个系统不一样（有的用 ``Authorization``，
        有的用 ``accesstoken`` + ``uuid``）。

        滑块识别偶尔会偏（图像纹理复杂时），这里做有限次重试：
        每次重试都重新抓一个全新的 uuid（uuid 是一次性凭证，重用会被服务端放行）。

        Args:
            max_timeout: 整个登录流程的总时间上限（秒）。
                超了就放弃，不让一条登录用例把整个套件拖死。
        """
        from utils.http.api_client import ApiClient

        profile = ProjectRegistry.get(project_name, env)
        data = (login_config or {}).get("data") or {}
        username = data.get("username")
        password = data.get("password")
        retries = int((login_config or {}).get("slider_retries", 3))

        last_error: Exception | None = None

        # 整个登录流程的总时间上限（秒）。
        #
        # ⚠️ 为什么需要这个：滑块登录内部有多层重试
        #    （框架重试 3 次 × Playwright 各处 10~30 秒超时），
        #    网络一抖就能拖到几小时 —— 之前就因为一条登录用例卡了 7 小时。
        #
        # 超了就直接放弃这条用例，让后面的用例继续跑。
        deadline = time.time() + max_timeout

        for attempt in range(1, retries + 1):
            # 已经超时了就别再试了
            remaining = deadline - time.time()
            if remaining <= 0:
                log.warning(
                    "%s 登录总超时（%s 秒），不再重试", project_name, max_timeout
                )
                break

            try:
                log.info(
                    "正在登录 %s（第 %s/%s 次，自动抓新 uuid + 滑块识别）",
                    project_name,
                    attempt,
                    retries,
                )
                with ApiClient.login(
                    username=username,
                    password=password,
                    fresh=True,
                    profile=profile,
                    timeout=remaining,
                ) as api:
                    token = api.access_token
                    refresh_token = api.refresh_token
                    session = api.session_id
            except Exception as exc:
                last_error = exc
                log.warning("%s 第 %s 次登录失败: %s", project_name, attempt, exc)
                if attempt < retries:
                    time.sleep(3)  # 等一下，避免撞服务端限流
                continue

            log.info("%s 登录成功，token 前缀=%s...", project_name, token[:16])
            return {
                "token": token,
                "refresh_token": refresh_token,
                "uuid": session,
                "headers": self._auth_headers(profile, token, session),
            }

        raise RuntimeError(f"{project_name} 登录失败（重试 {retries} 次）: {last_error}")

    # ---------------- 直接账号密码登录 ----------------

    def _password_login(
        self, project_name: str, login_config: dict[str, Any], env: str
    ) -> dict[str, Any] | None:
        """没有滑块的系统：直接拿账号密码调登录接口。

        登录接口的路径、方法、字段名都在配置里（``login.path`` / ``login.method`` /
        ``login.username_field`` / ``login.password_field``）。

        响应里 token 放哪儿也有讲究，所以支持自定义取值路径::

            login:
              token_path: "result.accessToken"    # 默认就是它
        """
        from utils.http.api_client import ApiClient

        profile = ProjectRegistry.require(project_name, env)
        data = dict((login_config or {}).get("data") or {})

        username = data.get("username", profile.username)
        password = data.get("password", profile.password)

        with ApiClient.login(
            username=username,
            password=password,
            fresh=False,
            profile=profile,
            skip_slider=True,
        ) as api:
            token = api.access_token
            refresh_token = api.refresh_token
            session = api.session_id

        return {
            "token": token,
            "refresh_token": refresh_token,
            "uuid": session,
            "headers": self._auth_headers(profile, token, session),
        }

    # ---------------- 自定义登录 ----------------

    def _call_custom(
        self, dotted: str, project_name: str, login_config: dict[str, Any], env: str
    ) -> dict[str, Any] | None:
        """调用配置里指定的 ``模块名.函数名``。"""
        import importlib

        if not dotted:
            raise ValueError(
                f"项目 {project_name!r} 配了 custom 登录方式，但没写 login.custom_method"
            )

        module_name, func_name = dotted.rsplit(".", 1)
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            raise ImportError(f"找不到自定义登录模块 {module_name}: {exc}") from exc

        func = getattr(module, func_name, None)
        if not callable(func):
            raise AttributeError(f"{module_name} 里没有可调用的 {func_name}")

        result = func(project_name, login_config, env)
        if not isinstance(result, dict) or "token" not in result:
            raise ValueError(f"自定义登录 {dotted} 必须返回含 'token' 的字典")
        return result

    # ---------------- 对外入口 ----------------

    def login_and_store(self, project_name: str, login_config: dict[str, Any], env: str) -> str | None:
        """登录并把 token / 会话标识写进 Globals，返回 token。"""
        result = self.login(project_name, login_config, env)
        if not result:
            return None

        profile = ProjectRegistry.get(project_name, env)
        token_var = profile.token_var if profile else "token"
        session_var = profile.session_var if profile else "uuid"
        refresh_var = profile.refresh_token_var if profile else "refreshToken"

        Globals.update(project_name, token_var, result.get("token"))
        if result.get("refresh_token"):
            # 刷新令牌用例把它当请求参数：{{mall4j.refreshToken}}
            Globals.update(project_name, refresh_var, result["refresh_token"])
        if result.get("uuid"):
            Globals.update(project_name, session_var, result["uuid"])
        if result.get("headers"):
            Globals.update(project_name, "login_headers", result["headers"])
        return result.get("token")

    # ---------------- 辅助 ----------------

    @staticmethod
    def _auth_headers(
        profile: ProjectProfile | None, token: str, session: str
    ) -> dict[str, Any]:
        """按项目约定拼鉴权头。"""
        if profile is None:
            headers: dict[str, Any] = {}
            if token:
                headers["accesstoken"] = token
            if session:
                headers["uuid"] = session
            return headers
        return profile.auth_headers(token, session)
