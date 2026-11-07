"""登录后的业务接口客户端。

用来在**不打开浏览器**的前提下，用拿到的 token 调任意被测系统的
业务接口（商品、订单、统计……）。

三个关键点（都踩过）：

**① 业务接口的鉴权头不一定是 `Authorization: Bearer`**
    有的系统登录之后改用 ``accesstoken: <accessToken>``，别的系统可能叫
    ``token``。具体叫什么，读项目档案（``utils/project_profile.py``）。

**② 会话标识头要带上**
    有的系统业务接口除了 token 还要 ``uuid`` 头（前端会一直带着）。

**③ 接口根地址按项目取**
    不是全局那个 ``base_url``，是各项目自己配的 ``host``。

用法::

    from utils.http.api_client import ApiClient

    with ApiClient.from_config() as api:
        info = api.get("/admin/user/info")
        print(info["result"]["username"])
"""
from __future__ import annotations

from typing import Any

import requests

from utils.config.config_util import get_config
from utils.logs.logger_util import get_logger
from utils.config.project_profile import ProjectProfile, ProjectRegistry
from utils.http.request_util import (
    ensure_login_payload,
    make_session,
    slider_init,
    slider_verify_success,
    user_login,
)
from utils.slider.slider_solver import parse_init_response, solve_slider_x

log = get_logger(__name__)


class ApiClient:
    """带 token 的业务接口客户端。

    用法一（自己登录）::

        with ApiClient.from_config() as api:
            api.get("/admin/user/info")

    用法二（拿现成 token）::

        api = ApiClient(access_token="...", uuid="...")
    """

    def __init__(
        self,
        access_token: str,
        uuid: str,
        session: requests.Session | None = None,
        profile: ProjectProfile | None = None,
        refresh_token: str = "",
    ):
        self.access_token = access_token
        #: 续期用；登录响应里没有这个字段时为空串
        self.refresh_token = refresh_token
        self.uuid = uuid
        self.session = session or make_session(uuid, profile=profile)
        self.profile = profile

    # ---------------- 构造 ----------------

    @classmethod
    def login(
        cls,
        username: str | None = None,
        password: str | None = None,
        uuid: str | None = None,
        fresh: bool = True,
        profile: ProjectProfile | None = None,
        skip_slider: bool = False,
        timeout: float | None = None,
    ) -> "ApiClient":
        """走登录链路，返回可用客户端。

        Args:
            username: 缺省取项目配置里的账号
            password: 缺省取项目配置里的密码（明文）
            uuid: 指定 uuid；``fresh=False`` 时才用这个（**必须是一个没用过的**）
            fresh: 默认 True —— 自动开浏览器抓一个全新 uuid。
                   uuid 是一次性凭证，用旧的下游会被服务端直接放行。
            profile: 项目档案；缺省按当前环境取默认项目
            skip_slider: 跳过滑块（没有滑块的系统直接账号密码登录）
            timeout: 抓 uuid 的超时上限（秒）。给了就按它算，
                     免得网络抖动时一条用例卡几十分钟。
        """
        profile = profile or ProjectRegistry.get(ProjectRegistry.default_project())
        cfg = get_config()

        if username is None:
            username = profile.username if profile else cfg["login"]["username"]
        if password is None:
            password = profile.password if profile else cfg["login"]["password"]

        if not skip_slider and (fresh or not uuid):
            from utils.slider.uuid_util import fetch_uuid

            uuid = fetch_uuid(
                timeout_ms=int(timeout * 1000) if timeout else 60_000,
                profile=profile,
            )
            if not uuid:
                raise RuntimeError(
                    "没抓到新 uuid（服务端限流？等 30 秒再试）。"
                    "也可以自己设环境变量 <项目名>_UUID 后用 fresh=False。"
                )

        session = make_session(uuid, profile=profile)

        # 1) 滑块（没有滑块的系统跳过）
        if not skip_slider:
            init = parse_init_response(slider_init(session, profile=profile))
            x = solve_slider_x(
                init,
                debug_dir=(cfg.get("slider") or {}).get("debug_dir"),
                save_debug_flag=bool((cfg.get("slider") or {}).get("save_debug", True)),
            )
            if not slider_verify_success(session, x, profile=profile):
                raise RuntimeError(f"滑块校验失败：xPos={x}")

        # 2) 登录
        resp = user_login(
            session,
            username=username,
            password=password,
            profile=profile,
        )
        tokens = ensure_login_payload(resp, profile=profile)

        log.info("登录成功，token 前缀=%s...", tokens["access_token"][:20])
        return cls(
            tokens["access_token"],
            uuid or "",
            session=session,
            profile=profile,
            refresh_token=tokens.get("refresh_token", ""),
        )

    @classmethod
    def from_config(cls, fresh: bool = True, profile: ProjectProfile | None = None) -> "ApiClient":
        """用配置里的账号密码登录。"""
        return cls.login(fresh=fresh, profile=profile)

    # ---------------- 上下文管理 ----------------

    def __enter__(self) -> "ApiClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass

    # ---------------- 请求 ----------------

    @property
    def session_id(self) -> str:
        """会话标识（uuid 那种）。"""
        return self.uuid

    def _base_url(self) -> str:
        """接口根地址：项目档案的 host > 全局 base_url。"""
        if self.profile and self.profile.host:
            return self.profile.host.rstrip("/")
        return get_config()["base_url"].rstrip("/")

    def _headers(self) -> dict[str, str]:
        """业务接口专用的请求头。

        鉴权头名按项目档案来（各自项目在 configs/config.yaml 里配）。
        """
        h = dict(self.session.headers)
        h.pop("Authorization", None)  # 登录后的业务接口一般不用它
        if self.profile:
            h.update(self.profile.auth_headers(self.access_token, self.uuid))
        else:
            h["accesstoken"] = self.access_token
            h["uuid"] = self.uuid
        return h

    def _request(self, method: str, path: str, **kw) -> dict[str, Any]:
        url = path if path.startswith("http") else self._base_url() + path
        resp = self.session.request(
            method, url, headers=self._headers(), timeout=kw.pop("timeout", 30), **kw
        )
        try:
            return resp.json()
        except ValueError:
            raise RuntimeError(
                f"{method} {path} 返回的不是 JSON：HTTP {resp.status_code} {resp.text[:300]!r}"
            )

    def get(self, path: str, **kw) -> dict[str, Any]:
        return self._request("GET", path, **kw)

    def post(self, path: str, json_body: dict[str, Any] | None = None, **kw) -> dict[str, Any]:
        return self._request("POST", path, json=json_body, **kw)

    # ---------------- 常用业务接口 ----------------

    def current_user(self) -> dict[str, Any]:
        """当前登录用户信息（用来验证 token 是否真能用）。"""
        return self.get("/admin/user/info")

    def member_menu(self) -> dict[str, Any]:
        """当前用户可见的菜单树。"""
        return self.get("/admin/permission/menu/memberMenu")

    def statistics_index(self) -> dict[str, Any]:
        """首页统计概览。"""
        return self.get("/admin/statistics/index")
