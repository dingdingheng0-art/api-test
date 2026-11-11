"""HTTP 封装：统一的请求头构造 + 常用接口封装。

服务端鉴权规则（来自抓包）：
- ``Authorization: <token>``      —— 业务接口的鉴权头（头名按项目档案走）
- ``uuid: <uuid>``                —— 会话标识头（有的系统要，有的不要）
- Origin / Referer 必须指向管理后台（前端域名），否则可能触发限流
- ``_t`` 时间戳：每次 POST 用 ``int(time.time())`` 现取（10 位秒，与抓包一致）

**多系统兼容**：路径、请求头、接口根地址都按**项目档案**取
（``utils/project_profile.py``），所以同一套代码能测多个系统。
不传 ``profile`` 时按老规矩取默认值，行为跟以前一样。

uuid 来源（优先级）：
1. 环境变量 ``<项目名大写>_UUID``（推荐 —— 浏览器里刷新一次登录页，从
   Network 看滑块请求的 ``uuid:`` header，设好后再跑 pytest）
2. ``configs/config.yaml`` 的 ``uuid`` 字段（仅作演示 fallback；该值会在
   浏览器侧被服务端消费后报 ``code:80204 验证码已失效``）
"""
from __future__ import annotations

import os
import time
from typing import Any

import requests

from utils.config.config_util import get_config
from utils.logs.log_manager import mask_text
from utils.logs.logger_util import get_logger
from utils.md5_util import md5
from utils.config.project_profile import ProjectProfile

log = get_logger(__name__)


class SliderTokenError(RuntimeError):
    """uuid 已被服务端消费 / 滑块 token 失效（典型 code=80204）。

    由 ``slider_init`` / ``slider_verify`` 抛出；fixture 层直接
    ``pytest.fail(str(exc))`` 即可，让用户看到刷新 uuid 的指引。
    """


def _resolve_uuid(profile: ProjectProfile | None = None) -> str:
    """uuid 解析：环境变量 ``<项目名>_UUID`` 优先，回退 config.yaml。"""
    env_name = _uuid_env_name(profile)
    env_val = os.environ.get(env_name, "").strip()
    if env_val:
        return env_val
    cfg_val = get_config().get("uuid", "").strip()
    if not cfg_val:
        raise RuntimeError(
            "缺少 uuid。两种方式任选其一：\n"
            f"  1) 浏览器刷新登录页 → Network 看滑块请求的 'uuid' header → "
            f"环境里设 ${env_name}='<uuid>' 后跑 pytest\n"
            "  2) 把该 uuid 写进 configs/config.yaml 的 uuid 字段"
        )
    return cfg_val


def _uuid_env_name(profile: ProjectProfile | None = None) -> str:
    """会话标识用的环境变量名：``<项目名大写>_UUID``。"""
    name = (profile.name if profile else "") or "DEFAULT"
    return f"{name.upper()}_UUID"


def _refresh_uuid_hint(profile: ProjectProfile | None = None) -> str:
    """生成 80204 等"uuid 已被消费"错误的用户友好提示。"""
    uuid = _resolve_uuid(profile)
    login_page = (
        profile.effective_login_page if profile else "（未配置登录页）"
    )
    env_name = _uuid_env_name(profile)
    return (
        f"当前 uuid={uuid} 已被服务端消费（code=80204 验证码已失效）。\n"
        "修复方法（任选其一）：\n"
        f"  1) 刷新 {login_page} → F12 → Network 找 "
        "滑块校验请求的 'uuid' header → 复制 → "
        f"  ${env_name} = '<uuid>'   （PowerShell）\n"
        "  2) 或直接把新 uuid 写入 configs/config.yaml 的 uuid 字段\n"
        "然后再重跑 pytest。"
    )


def build_default_headers(
    session: requests.Session,
    uuid: str | None = None,
    profile: ProjectProfile | None = None,
) -> dict[str, str]:
    """构造固定请求头（与浏览器抓包一致）。

    Args:
        session: 目标 session（保留兼容）
        uuid: 指定 uuid；缺省从环境变量 / 配置取
        profile: 项目档案；决定 Origin / Referer 等前端相关头

    **多系统兼容**：不是每个系统都有前端管理后台。
    只有"浏览器抓包系统"（配了 ``web_url`` / ``login_page``）才需要
    Origin / Referer / uuid / Authorization 这些浏览器头；
    纯 API 系统（没配前端地址）不该收到它们 —— 否则会把上一个系统的
    域名和会话标识带过去，反而被服务端拒掉。
    """
    cfg = get_config()

    headers: dict[str, str] = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
        "Connection": "keep-alive",
        "User-Agent": cfg["user_agent"],
    }

    # 浏览器那一套头（Origin / Referer / Sec-Fetch-* / uuid / Authorization）
    #
    # 只有"浏览器抓包系统"才需要 —— 判据是项目配了前端地址
    # （web_url / login_page）。纯 API 系统（没配前端）不该收到这些头，
    # 否则会把上一个系统的域名和会话标识带过去，反而被服务端拒掉。
    #
    # profile 是 None 时按老行为走（顶层配置里的 origin / uuid）。
    has_web = profile is None or bool(profile.effective_web_url)
    if has_web:
        origin = profile.effective_web_url if profile else cfg["origin"]
        referer = origin + "/" if profile else cfg["referer"]
        headers.update(
            {
                "Origin": origin,
                "Referer": referer,
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-site",
                "sec-ch-ua": cfg["sec_ch_ua"],
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
            }
        )

        # 会话标识头 + Authorization —— 只有"靠 uuid 维持会话"的系统才需要。
        #
        # 项目档案里 ``session_header: null`` 表示这个系统不带会话标识
        # （比如 token 本身就是全部凭证），那就别硬塞 uuid 和
        # ``Authorization: Bearer <uuid>``，免得把服务端拒掉。
        if profile is None or profile.session_header:
            session_uuid = uuid or _resolve_uuid(profile)
            headers["uuid"] = session_uuid
            headers["Authorization"] = f"Bearer {session_uuid}"
    # else: 纯 API 系统 —— 前端地址都不知道，就别瞎填，省得污染请求

    # 项目档案里配了额外头就补上（不覆盖上面已有的）
    if profile:
        for key, value in (profile.headers or {}).items():
            headers.setdefault(key, value)

    return headers


def make_session(
    uuid: str | None = None,
    profile: ProjectProfile | None = None,
) -> requests.Session:
    """创建带默认请求头 + 默认 timeout=30s 的 Session。

    Args:
        uuid: 指定 uuid（uuid 是一次性凭证，换一个就能拿到全新题目）
        profile: 项目档案（决定请求头里的前端地址等）
    """
    s = requests.Session()
    # 不走系统代理（VPN / 抓包工具装的本机代理会拦请求，返回 502）
    s.trust_env = False
    s.headers.update(build_default_headers(s, uuid, profile=profile))
    return s


def _base_url_of(profile: ProjectProfile | None = None) -> str:
    """接口根地址：项目档案的 host > 顶层 base_url。"""
    if profile and profile.host:
        return profile.host.rstrip("/")
    return get_config()["base_url"].rstrip("/")


def _build_url(base_url: str, path: str, query: dict[str, Any] | None = None) -> str:
    """拼接 base_url + path + query。"""
    url = base_url.rstrip("/") + path if not path.startswith("http") else path
    if query:
        from urllib.parse import urlencode

        qs = urlencode({k: v for k, v in query.items() if v is not None})
        url = f"{url}?{qs}" if qs else url
    return url


def api_post(
    session: requests.Session,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    files: dict[str, Any] | None = None,
    query: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 30,
    path_base: str | None = None,
    raise_for_status: bool = True,
    profile: ProjectProfile | None = None,
) -> dict[str, Any]:
    """POST 通用入口。

    - ``json_body`` / ``data`` / ``files`` 三选一（form-data 用 data/files）
    - ``query``：拼接到 URL 上的 query 参数（如 ``?xPos=216``）
    - ``path_base``：覆盖接口根地址（一般不需要）
    - ``profile``：项目档案，决定接口根地址
    - ``raise_for_status``：默认 True（HTTP>=400 直接 raise）；
      需要自己解析 body 的场景（如 80204 验证码已失效）传 False

    返回响应 JSON。
    """
    base = path_base or _base_url_of(profile)
    url = _build_url(base, path, query)

    req_headers: dict[str, str] = {}
    if headers:
        req_headers.update(headers)

    # multipart/form-data 时不要带显式 Content-Type，requests 会自己生成 boundary
    kwargs: dict[str, Any] = {"headers": req_headers or None, "timeout": timeout}
    if json_body is not None:
        kwargs["json"] = json_body
    elif data is not None:
        kwargs["data"] = data
    if files is not None:
        kwargs["files"] = files

    log.info("→ POST %s  query=%s", url, mask_text(str(query or {})))
    resp = session.post(url, **kwargs)
    # 响应必须脱敏：登录接口的 accessToken / refreshToken 就在 body 里，
    # 原样落盘等于把凭证写进 logs/test.log（这个 logger 没有其它脱敏层）。
    log.info("← HTTP %s  body[%s]=", resp.status_code, mask_text(resp.text[:300]))
    if raise_for_status:
        resp.raise_for_status()
    return _parse_json(resp)


def api_get(
    session: requests.Session,
    path: str,
    *,
    query: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 30,
    path_base: str | None = None,
    raise_for_status: bool = True,
    profile: ProjectProfile | None = None,
) -> dict[str, Any]:
    """GET 通用入口。

    - ``profile``：项目档案，决定接口根地址
    - ``raise_for_status``：默认 True（HTTP>=400 直接 raise）；
      需要自己解析 body 的场景（如 80204 验证码已失效）传 False
    """
    base = path_base or _base_url_of(profile)
    url = _build_url(base, path, query)
    log.info("→ GET %s", url)
    resp = session.get(url, headers=headers or None, timeout=timeout)
    if raise_for_status:
        resp.raise_for_status()
    return _parse_json(resp)


def _parse_json(resp: requests.Response) -> dict[str, Any]:
    """把响应体解析成 dict（空响应返回 {}）。"""
    if not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError as exc:
        raise ValueError(
            f"响应不是 JSON（HTTP {resp.status_code}）：{resp.text[:200]}"
        ) from exc


# ----------------------------------------------------------------------
# 业务封装：把"滑块 + 登录"三个常用动作做成函数，给 fixture 调用更清晰
# ----------------------------------------------------------------------

def slider_init(
    session: requests.Session,
    max_retries: int = 3,
    retry_delay: float = 8.0,
    profile: ProjectProfile | None = None,
) -> dict[str, Any]:
    """GET 滑块初始化接口，返回响应 JSON 里的 ``result``。

    路径从项目档案取（``slider.init_path``），默认
    ``/captcha/get``。

    注意：init 是 GET（浏览器抓包为 ``GET /captcha/get?_t=...``），
    若误用 POST 服务端会按"未形成 verify 会话"返回 ``code:80204 验证码已失效``。

    失败路径：
    - ``code=1003`` 访问过于频繁（服务端 QPS 限制）→ 自动等待重试
    - HTTP 400 + code=80204（uuid 已被消费/滑块失效） → ``SliderTokenError``（带刷新 uuid 指引）
    - 其他 code != 200 或 success is not True       → ``AssertionError``（带 code/message）
    """
    init_path = profile.slider_init_path if profile else "/captcha/get"

    resp: dict[str, Any] = {}
    for attempt in range(max_retries + 1):
        resp = api_get(
            session,
            init_path,
            query={"_t": int(time.time())},
            raise_for_status=False,
            profile=profile,
        )
        # 限流：等一会儿重试
        if resp.get("code") == 1003 and attempt < max_retries:
            log.warning("滑块接口限流（code=1003），%.0fs 后重试（第 %s 次）",
                        retry_delay, attempt + 1)
            time.sleep(retry_delay * (attempt + 1))
            continue
        break

    # 80204 优先命中（uuid 已失效）—— 独立异常类 + 刷新指引
    if resp.get("code") == 80204:
        raise SliderTokenError(_refresh_uuid_hint(profile))
    # 其余 code != 200 或 success != True：通用断言失败，把完整 body 打出来
    code = resp.get("code")
    if code != 200 or resp.get("success") is not True:
        message = resp.get("message")
        extra = ""
        # 额外检测：有些 80204 信息可能藏在 message 里（防服务端没给 code）
        if "失效" in str(message or "") or "过期" in str(message or ""):
            extra = "\n" + _refresh_uuid_hint(profile)
        raise AssertionError(
            "slider_init 失败: code=%s message=%s resp=%s%s"
            % (code, message, resp, extra)
        )
    return resp["result"]


def slider_verify(
    session: requests.Session,
    x_pos: int,
    max_retries: int = 3,
    retry_delay: float = 8.0,
    profile: ProjectProfile | None = None,
) -> dict[str, Any]:
    """POST 滑块校验接口，返回响应 JSON。

    路径和附加参数从项目档案取（``slider.verify_path`` / ``slider.verify_query``）。

    **必须是 POST**（浏览器抓包实测）。用 GET 的话服务端会把它当成"取下一题"
    的请求直接放行 —— 传任何 xPos 都返回 ``code=200 / success=true``，
    根本不会校验 xPos，会导致测试假通过。

    服务端契约（对照前端 JS ``e.success ? e.result : ...``）：
    - 通过：``success=true``，``result`` 是**下一题的初始化数据**（dict）
    - 失败：``success=false``，``result=null``，``code=80202``
    - 限流：``code=1003``「访问过于频繁」→ 自动等待重试
    """
    verify_path = profile.slider_verify_path if profile else "/captcha/get"
    verify_query = (
        dict(profile.slider_verify_query) if profile else {"verificationEnums": "LOGIN"}
    )
    query = {**verify_query, "xPos": int(x_pos)}

    resp: dict[str, Any] = {}
    for attempt in range(max_retries + 1):
        resp = api_post(
            session,
            verify_path,
            query=query,
            raise_for_status=False,
            profile=profile,
        )
        if resp.get("code") == 1003 and attempt < max_retries:
            log.warning("滑块校验限流（code=1003），%.0fs 后重试（第 %s 次）",
                        retry_delay, attempt + 1)
            time.sleep(retry_delay * (attempt + 1))
            continue
        break

    if resp.get("code") == 80204:
        # 走到这里说明 init→verify 之间的 300s 内 uuid 被消费/失效
        raise SliderTokenError(_refresh_uuid_hint(profile))
    return resp


def slider_verify_success(
    session: requests.Session,
    x_pos: int,
    profile: ProjectProfile | None = None,
) -> bool:
    """验证是否通过。

    服务端契约（对照前端 JS ``e.success ? e.result : ...``）：
    - 通过：``success=true``，``result`` 是**下一题的初始化数据**（dict）
    - 失败：``success=true``，``result=null``
    所以判据是 success 为真且 result 为真值。
    """
    resp = slider_verify(session, x_pos, profile=profile)
    return bool(resp.get("success") is True) and bool(resp.get("result"))


def user_login(
    session: requests.Session,
    username: str | None = None,
    password: str | None = None,
    raise_for_status: bool = False,
    profile: ProjectProfile | None = None,
) -> dict[str, Any]:
    """调登录接口。

    路径、方法、字段名都从项目档案取：

    - ``login.path``（默认 ``/adminLogin``）
    - ``login.method``（默认 ``POST``）
    - ``login.username_field`` / ``login.password_field``（默认 ``username`` / ``password``）
    - ``login.password_md5``（默认 True —— 密码先 MD5 再传，和前端一致）

    账号密码缺省时取项目配置里的。

    .. note::
       默认**不**因为 HTTP 400 抛异常 —— 账密错误时服务端返回的就是
       HTTP 400 + JSON body，调用方要靠 body 里的 code/message 判定。
    """
    cfg = get_config()
    if username is None:
        username = (
            profile.username if profile and profile.username else cfg["login"]["username"]
        )
    if password is None:
        password = (
            profile.password if profile and profile.password else cfg["login"]["password"]
        )

    login_path = (
        profile.login_path if profile and profile.login_path else "/adminLogin"
    )
    username_field = profile.username_field if profile else "username"
    password_field = profile.password_field if profile else "password"
    use_md5 = profile.password_md5 if profile else True

    form = {
        # 字段名与浏览器抓包一致：name="username" / name="password"
        # 注意：password 是 MD5 后的小写 32 位 hex
        username_field: username,
        password_field: md5(password) if use_md5 else password,
    }
    resp = api_post(
        session, login_path, data=form,
        raise_for_status=raise_for_status,
        profile=profile,
    )
    return resp


# ----------------------------------------------------------------------
# 工具：统一断言包装
# ----------------------------------------------------------------------

def _ensure_success(resp: dict[str, Any], path: str = "") -> None:
    """通用成功断言：code==200 且 success==True。"""
    if resp.get("code") != 200 or resp.get("success") is not True:
        detail = {
            "path": path,
            "code": resp.get("code"),
            "message": resp.get("message"),
            "result_type": type(resp.get("result")).__name__,
        }
        raise AssertionError(f"接口调用失败: {detail}")


def ensure_login_payload(
    resp: dict[str, Any],
    profile: ProjectProfile | None = None,
) -> dict[str, str]:
    """登录成功时把响应里的 token 抽出来。

    默认按 ``result.accessToken`` / ``result.refreshToken`` 取。
    别的系统可以在配置里写取值路径（点号分隔）::

        login:
          token_path: "data.token"
          refresh_path: "data.refresh_token"

    不配 refresh 就只返回 token（refresh_token 为空串）。
    """
    if resp.get("code") != 200 or resp.get("success") is not True:
        raise AssertionError(f"登录失败响应: code={resp.get('code')} msg={resp.get('message')}")
    result = resp.get("result") or {}

    def _pick(path: str) -> Any:
        """按点号路径取值（``data.token``）。"""
        node: Any = resp
        for key in path.split("."):
            if isinstance(node, dict) and key in node:
                node = node[key]
            else:
                return None
        return node

    login_cfg = (profile.login if profile else {}) or {}
    token_path = str(login_cfg.get("token_path") or "result.accessToken")
    refresh_path = str(login_cfg.get("refresh_path") or "result.refreshToken")

    access = _pick(token_path)
    refresh = _pick(refresh_path)

    if not access:
        raise AssertionError(
            f"登录响应里找不到 token（路径 {token_path}）：keys={list(result.keys())}"
        )
    return {"access_token": str(access), "refresh_token": str(refresh or "")}
