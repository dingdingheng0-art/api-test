"""项目档案：把"某个被测系统长什么样"集中在一处。

**为什么要这个文件**

框架要能测多个系统，但每个系统的"长相"不一样：

- 接口根地址不一样（``http://103.236.97.137:8190/_admin`` vs 别的）
- 前端页面地址不一样（滑块抓 uuid 要开前端登录页）
- 鉴权头名字不一样（有的叫 ``Authorization``，有的叫 ``accesstoken``）
- 登录方式不一样（有的走拼图滑块，有的就账号密码一把梭）

以前这些散落在代码里，写死成某个项目名。现在统一收到**配置**里，
代码只读配置 —— 加新系统 = 改配置 + 写登录函数，不用动框架。

**配置长这样**（写在 ``configs/config.yaml`` 的 ``projects.<项目名>.<环境>`` 下）：

.. code-block:: yaml

    projects:
      my_shop:
        online:
          host: "https://api.my-shop.com"
          is_need_login: true
          # 前端页面地址（滑块登录要靠它抓 uuid）
          web_url: "https://admin.my-shop.com"
          login_page: "https://admin.my-shop.com/login"
          # 登录方式：slider（拼图滑块）/ custom（自己的函数）/ password（直接账号密码）
          login.type: slider
          # 业务接口的鉴权头（框架自动帮你带上）
          auth:
            token_header: "Authorization"
            session_header: null        # 没有会话标识头就写 null
            token_var: "token"
            session_var: "uuid"
          # 滑块接口路径（只有 login.type=slider 才需要）
          slider:
            init_path: "/api/slider/init"
            verify_path: "/api/slider/verify"
            verify_query:
              verificationEnums: "LOGIN"
          login:
            path: "/api/auth/login"
            method: "POST"
            data:
              username: "${TARGET_USERNAME:admin}"
              password: "${TARGET_PASSWORD:}"

**没配的字段怎么办**

每个字段都有合理默认值，**只配 host 也能跑**（老配置直接兼容）。
拿不准就看 ``ProjectProfile`` 里每个属性的默认值。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from utils.logs.log_manager import log

# ============================================================================
# 默认值 —— 缺省时的兜底，保证老配置不报错
# ============================================================================

#: 鉴权头名（业务接口带 token 的那个头）
DEFAULT_TOKEN_HEADER = "accesstoken"

#: 会话标识头（uuid 那种）；没有就填 None
DEFAULT_SESSION_HEADER = "uuid"

#: 登录方式：滑块 / 自定义函数 / 直接账号密码
LOGIN_TYPE_SLIDER = "slider"
LOGIN_TYPE_CUSTOM = "custom"
LOGIN_TYPE_PASSWORD = "password"

#: 滑块默认路径（没配 slider 段时的兜底）
#:
#: ⚠️ 这只是**占位值** —— 滑块接口每个系统都不一样，
#:    必须在项目档案的 ``slider.init_path`` / ``slider.verify_path`` 里配实际的。
#:    这里的默认值只保证"没配也不至于崩"，别指望它能用。
DEFAULT_SLIDER_INIT_PATH = "/captcha/get"
DEFAULT_SLIDER_VERIFY_PATH = "/captcha/check"


@dataclass
class ProjectProfile:
    """一个项目在某个环境下的"档案"。

    属性名和 ``config.yaml`` 里的字段一一对应。
    """

    # ---------------- 基础 ----------------

    name: str
    env: str
    host: str

    #: 要不要登录
    is_need_login: bool = True

    #: 前端管理后台地址（滑块登录要靠它开页面抓 uuid）
    web_url: str = ""

    #: 登录页地址（抓 uuid 用）
    login_page: str = ""

    #: 报告里显示的项目中文名
    label: str = ""

    #: 项目级默认请求头（会被用例自己的头覆盖）
    headers: dict[str, Any] = field(default_factory=dict)

    #: 该项目用到的数据库配置（setup/teardown 里的 db 操作、断言查库）
    mysql: dict[str, Any] | None = None

    # ---------------- 鉴权 ----------------

    #: 业务接口的鉴权头名（``accesstoken`` / ``token`` / ...）
    token_header: str = DEFAULT_TOKEN_HEADER

    #: 会话标识头名（uuid 那种），没有就 None
    session_header: str | None = DEFAULT_SESSION_HEADER

    #: token 在全局变量里的键名
    token_var: str = "token"

    #: 会话标识在全局变量里的键名
    session_var: str = "uuid"

    #: refreshToken 在全局变量里的键名（没有 refreshToken 的系统用不到）
    refresh_token_var: str = "refreshToken"

    # ---------------- 登录 ----------------

    #: 登录方式：slider / custom / password
    login_type: str = LOGIN_TYPE_SLIDER

    #: 登录配置（原始 dict，包含 data / url / method / custom_method 等）
    login: dict[str, Any] = field(default_factory=dict)

    #: 自定义登录函数（``模块名.函数名``），login_type=custom 时用
    custom_method: str = ""

    #: 登录接口路径（login_type=password 时用）
    login_path: str = ""
    login_method: str = "POST"

    #: 登录账号密码
    username: str = ""
    password: str = ""

    #: 登录请求体字段名（不同系统的表单字段名可能不一样）
    username_field: str = "username"
    password_field: str = "password"

    #: 密码是否要 MD5（前端一般会先 MD5 再传）
    password_md5: bool = True

    #: 滑块重试次数
    slider_retries: int = 3

    # ---------------- 滑块 ----------------

    slider_init_path: str = DEFAULT_SLIDER_INIT_PATH
    slider_verify_path: str = DEFAULT_SLIDER_VERIFY_PATH
    slider_verify_query: dict[str, Any] = field(default_factory=dict)

    # ---------------- 工具方法 ----------------

    @property
    def effective_web_url(self) -> str:
        """前端地址：优先 ``web_url``，否则从 ``login_page`` 推。"""
        if self.web_url:
            return self.web_url.rstrip("/")
        if self.login_page:
            return self.login_page.rstrip("/").rsplit("/", 1)[0]
        return ""

    @property
    def effective_login_page(self) -> str:
        """登录页地址：优先 ``login_page``，否则 ``web_url`` + ``/login``。"""
        if self.login_page:
            return self.login_page
        if self.web_url:
            return self.web_url.rstrip("/") + "/login"
        return ""

    @property
    def needs_slider(self) -> bool:
        """要不要走滑块。"""
        return self.login_type == LOGIN_TYPE_SLIDER

    def auth_headers(self, token: str = "", session: str = "") -> dict[str, Any]:
        """按项目约定拼出鉴权头。"""
        headers: dict[str, Any] = {}
        if token and self.token_header:
            headers[self.token_header] = token
        if session and self.session_header:
            headers[self.session_header] = session
        return headers

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<ProjectProfile {self.name}/{self.env} host={self.host}>"


def _resolve_session_header(auth_cfg: dict[str, Any]) -> str | None:
    """解析会话标识头名。

    - 配置里写了名字 → 用它
    - 配置里显式写 ``null`` → 返回 None（这个系统不需要会话标识头）
    - 配置里**没写这个键** → 用默认值（老配置兼容）
    """
    if "session_header" not in auth_cfg:
        return DEFAULT_SESSION_HEADER
    value = auth_cfg.get("session_header")
    if value is None:
        return None
    text = str(value).strip()
    return text or None


# 正式环境的标志 —— 名字里带这些字眼的一律当"线上"
_PRODUCTION_ENV_NAMES = ("online", "prod", "production", "official", "live")

# 非正式环境的标志 —— 名字里带这些字眼的一律当"测试环境"
_NON_PRODUCTION_ENV_NAMES = (
    "test", "pre", "pretest", "dev", "develop", "staging", "uat", "sandbox",
)


def _configured_production_hosts() -> tuple[str, ...]:
    """配置里指定的"正式地址"。

    在 ``configs/config.yaml`` 里写::

        production_hosts:
          - "api.my-shop.com"
          - "admin-api.my-shop.com"

    没配就返回空 —— 那就靠下面的兜底启发式判断。
    """
    from utils.config.config_manager import ConfigManager

    raw = ConfigManager.get_all().get("production_hosts") or []
    if isinstance(raw, str):
        raw = [raw]
    return tuple(str(item).strip().lower() for item in raw if str(item).strip())



def _production_env_host_of(profile: ProjectProfile) -> str:
    """这个项目"正式环境"配的地址（拿不到就返回空串）。

    用来判断"当前环境是不是在偷偷用正式地址" ——
    只有当正式环境确实存在、且地址跟当前环境**一样**时，才需要喊。
    """
    from utils.config.config_manager import ConfigManager

    projects = ConfigManager.get_all().get("projects") or {}
    envs = projects.get(profile.name) or {}
    for env_name in _PRODUCTION_ENV_NAMES:
        raw = envs.get(env_name)
        if not isinstance(raw, dict):
            continue
        host = str(raw.get("host") or "").strip().rstrip("/").lower()
        if host:
            return host
    return ""


def _warn_if_production_masquerading(profile: ProjectProfile) -> None:
    """非正式环境却指向正式地址时，喊一嗓子。

    **为什么需要这个** —— 环境名是"标签"，地址才是"事实"。
    配置里写了 ``test`` 环境，但 host 还是线上地址的时候，
    跑测试就是在打线上 —— 而报告上写的是"测试环境"，看报告的人完全看不出来。

    所以这里不改行为（可能有人就是想用 test 环境打线上），
    只是**大声提醒**，别让人稀里糊涂把线上打挂了。

    判断依据（满足其一就提醒）：

    1. host 命中配置里 ``production_hosts`` 列的正式地址
    2. 兜底启发式 —— 环境名是"测试"，但地址里一个"测试"字样都没有，
       且这个项目**确实另有**一个正式地址（两边地址不一样，
       说明当前环境是在拿正式地址冒充测试环境）

    .. note::
       第 2 条里"确实另有正式地址"这个前提很重要 —— 有的项目就一套地址，
       环境名只是个标签。这种情况不该每次跑都喊。
    """
    env = (profile.env or "").lower()
    if env in _PRODUCTION_ENV_NAMES:
        return  # 正式环境本来就该打正式地址

    host = (profile.host or "").rstrip("/").lower()
    if not host:
        return

    # 1) 命中配置里列的正式地址
    markers = _configured_production_hosts()
    is_production_host = any(marker in host for marker in markers) if markers else False

    # 2) 兜底启发式：环境名是"测试"，但地址里一个"测试"字样都没有
    if not is_production_host and env in _NON_PRODUCTION_ENV_NAMES:
        if not any(marker in host for marker in _NON_PRODUCTION_ENV_NAMES):
            # 只有"这个项目确实另有正式地址"时才提醒 ——
            # 一套地址的项目（环境名只是标签）不该每次都喊。
            production_host = _production_env_host_of(profile)
            is_production_host = bool(production_host) and production_host != host

    if not is_production_host:
        return

    log.warning(
        "⚠️  环境 %r 指向的地址是正式地址（%s）—— "
        "跑测试就是在打线上，但报告上会显示成 %r，看报告的人分辨不出来。"
        "请把 %r 环境的 host 改成你们自己的测试地址"
        "（在 configs/config.yaml 里改，或设 <项目名>_TEST_HOST 环境变量）。",
        profile.env, profile.host, profile.env, profile.env,
    )


def build_profile(project: str, env: str, raw: dict[str, Any]) -> ProjectProfile:
    """把配置里的一段原始 dict 变成 :class:`ProjectProfile`。

    Args:
        project: 项目名
        env: 环境名
        raw: ``config.yaml`` 里 ``projects.<project>.<env>`` 的内容
    """
    raw = raw or {}

    login_cfg = raw.get("login") or {}
    auth_cfg = raw.get("auth") or {}
    slider_cfg = raw.get("slider") or {}
    login_data = login_cfg.get("data") or {}

    # 登录方式：显式配了就用，没配就按"有没有 custom_method"推断
    login_type = str(login_cfg.get("type") or "").strip()
    custom_method = str(login_cfg.get("custom_method") or "").strip()
    if not login_type:
        login_type = LOGIN_TYPE_CUSTOM if custom_method else LOGIN_TYPE_SLIDER

    return ProjectProfile(
        name=project,
        env=env,
        host=str(raw.get("host") or "").rstrip("/"),
        is_need_login=bool(raw.get("is_need_login", True)),
        web_url=str(raw.get("web_url") or "").rstrip("/"),
        login_page=str(raw.get("login_page") or ""),
        label=str(raw.get("label") or ""),
        headers=dict(raw.get("headers") or {}),
        mysql=raw.get("mysql"),
        token_header=str(auth_cfg.get("token_header") or DEFAULT_TOKEN_HEADER),
        session_header=_resolve_session_header(auth_cfg),
        token_var=str(auth_cfg.get("token_var") or "token"),
        session_var=str(auth_cfg.get("session_var") or "uuid"),
        refresh_token_var=str(auth_cfg.get("refresh_token_var") or "refreshToken"),
        login_type=login_type,
        login=dict(login_cfg),
        custom_method=custom_method,
        login_path=str(login_cfg.get("path") or login_cfg.get("url") or ""),
        login_method=str(login_cfg.get("method") or "POST").upper(),
        username=str(login_data.get("username") or raw.get("username") or ""),
        password=str(login_data.get("password") or raw.get("password") or ""),
        username_field=str(login_cfg.get("username_field") or "username"),
        password_field=str(login_cfg.get("password_field") or "password"),
        password_md5=bool(login_cfg.get("password_md5", True)),
        slider_retries=int(login_cfg.get("slider_retries", 3)),
        slider_init_path=str(slider_cfg.get("init_path") or DEFAULT_SLIDER_INIT_PATH),
        slider_verify_path=str(slider_cfg.get("verify_path") or DEFAULT_SLIDER_VERIFY_PATH),
        slider_verify_query=dict(slider_cfg.get("verify_query") or {}),
    )


class ProjectRegistry:
    """项目档案的读取入口（带缓存）。

    用法::

        profile = ProjectRegistry.get("my_shop", "online")
        profile.host        # https://api.my-shop.com
    """

    _cache: dict[tuple[str, str], ProjectProfile] = {}

    @classmethod
    def get(cls, project: str, env: str = "") -> ProjectProfile | None:
        """取某个项目在某个环境下的档案；没有就返回 None。

        **环境兜底**：某个项目没配当前环境（比如只配了 ``online``，
        但当前跑的是 ``test``）→ 自动退回 ``online``。
        这样新增项目时不用把所有环境都配一遍，
        也不会因为少配一个环境就报"项目不存在"。
        """
        from utils.config.config_manager import ConfigManager

        if not env:
            env = ConfigManager.get_env()

        key = (project, env)
        if key in cls._cache:
            return cls._cache[key]

        raw = ConfigManager.get_project_env_config(project, env)
        if raw is None and env != "online":
            # 当前环境没配 → 退回 online（配置里第一个配了的环境也行）
            fallback = cls._first_configured_env(project)
            if fallback:
                log.info(
                    "项目 %s 没配环境 %s，改用 %s",
                    project, env, fallback,
                )
                raw = ConfigManager.get_project_env_config(project, fallback)
                if raw:
                    env = fallback
                    key = (project, env)

        if not raw:
            return None

        profile = build_profile(project, env, raw[project])
        _warn_if_production_masquerading(profile)
        cls._cache[key] = profile
        return profile

    @classmethod
    def _first_configured_env(cls, project: str) -> str:
        """这个项目配了哪些环境（按配置里的顺序，取第一个）。

        优先 ``online`` —— 它是"正式环境"的通用叫法，
        别的项目基本都配了它。
        """
        from utils.config.config_manager import ConfigManager

        projects = ConfigManager.get_all().get("projects") or {}
        envs = projects.get(project) or {}
        configured = [str(k) for k, v in envs.items() if isinstance(v, dict)]
        if "online" in configured:
            return "online"
        return configured[0] if configured else ""

    @classmethod
    def require(cls, project: str, env: str = "") -> ProjectProfile:
        """取档案；取不到直接报错（配置错了要早暴露）。"""
        profile = cls.get(project, env)
        if profile is None:
            from utils.config.config_manager import ConfigManager

            raise ValueError(
                f"项目 {project!r} 在环境 {env!r} 下没有配置。"
                f"请检查 configs/config.yaml 的 projects 段。"
                f"（当前可用项目：{', '.join(cls.list_projects()) or '无'}）"
            )
        return profile

    @classmethod
    def list_projects(cls) -> list[str]:
        """配置里有哪些项目。"""
        from utils.config.config_manager import ConfigManager

        projects = ConfigManager.get_all().get("projects") or {}
        return sorted(projects.keys())

    @classmethod
    def default_project(cls) -> str:
        """默认项目名 —— 跑测试默认打哪个系统。

        **优先级**：环境变量 ``APITEST_PROJECT`` > ``config.yaml`` 的
        ``default_project`` > 配置里第一个项目。

        框架里所有"默认打哪个系统"的地方都走这里 ——
        不要在代码里写死项目名，换系统时只改配置就行。
        """
        from utils.config.config_manager import ConfigManager

        env_value = os.environ.get("APITEST_PROJECT", "").strip()
        if env_value:
            return env_value

        config_value = str(ConfigManager.get_all().get("default_project") or "").strip()
        if config_value:
            return config_value

        projects = cls.list_projects()
        return projects[0] if projects else ""

    @classmethod
    def clear_cache(cls) -> None:
        """清缓存（配置改了之后调一下）。"""
        cls._cache.clear()


def resolve_project_of(profile: ProjectProfile) -> str:
    """档案对应的全局变量键名。

    就是项目名本身 —— 全局变量里 ``Globals["<项目名>"]["token"]`` 存着它的 token。
    """
    return profile.name


def label_of(profile: ProjectProfile) -> str:
    """报告里显示的项目名：配置里的 ``label`` > 项目名。"""
    return profile.label or profile.name


def log_profile(profile: ProjectProfile) -> None:
    """把档案的关键信息打进日志（排查时有用）。"""
    log.info(
        "项目档案 %s/%s：host=%s 登录方式=%s 鉴权头=%s",
        profile.name,
        profile.env,
        profile.host,
        profile.login_type,
        profile.token_header,
    )
