"""自动获取一个**全新的 uuid**（会话标识）。

uuid 是很多管理后台的**一次性会话凭证**：滑块校验通过后就被服务端
消费掉，同一个 uuid 再用会返回 ``code=80204 验证码已失效``；更麻烦的是
消费之后服务端对该 uuid 的后续请求**直接放行**，连 xPos 都不校验。

所以每次独立验证都必须先拿一个新的 uuid。

怎么拿：
    前端点登录后弹出滑块组件，组件挂载时调滑块初始化接口，
    uuid 就在这个请求头里。这里开一次无头浏览器把这个头抓出来。

**多系统兼容**：登录页地址、滑块接口路径都从项目档案取
（``utils/project_profile.py``），所以换一个系统不用改代码。
"""
from __future__ import annotations

import sys

from utils.logs.logger_util import get_logger

log = get_logger(__name__)

#: 找不到项目档案时的兜底登录页
DEFAULT_LOGIN_PAGE = ""

#: 滑块初始化接口的路径特征（用来在抓包时认出哪个请求带 uuid）
DEFAULT_SLIDER_MARKER = ""


def _login_page_and_marker(profile) -> tuple[str, str]:
    """取（登录页地址，滑块接口路径特征）。"""
    login_page = profile.effective_login_page if profile else ""
    marker = profile.slider_init_path if profile else ""
    return login_page or DEFAULT_LOGIN_PAGE, marker or DEFAULT_SLIDER_MARKER


def fetch_uuid(timeout_ms: int = 60000, profile=None) -> str:
    """开一次无头浏览器，抓一个全新的 uuid。

    Args:
        timeout_ms: 打开页面的超时时间
        profile: 项目档案（决定去哪个登录页抓）

    Returns:
        uuid 字符串；抓不到就返回空串。
    """
    from utils.slider.browser_slider import BrowserSliderSolver
    from utils.config.config_util import get_config
    from utils.config.project_profile import ProjectRegistry

    if profile is None:
        profile = ProjectRegistry.get(ProjectRegistry.default_project())

    cfg = get_config()
    captured: dict[str, str] = {}
    login_page, marker = _login_page_and_marker(profile)

    username = profile.username if profile and profile.username else cfg["login"]["username"]
    password = profile.password if profile and profile.password else cfg["login"]["password"]

    with BrowserSliderSolver(
        headless=True,
        login_url=login_page,
        slider_marker=marker,
    ) as solver:
        page = solver.new_page()

        def on_req(req):
            if marker in req.url and "uuid" not in captured:
                captured["uuid"] = req.headers.get("uuid", "")

        page.on("request", on_req)
        page.goto(login_page, wait_until="networkidle", timeout=timeout_ms)
        solver.fill_credentials(page, username, password)
        try:
            solver.open_slider(page)
        except Exception:
            # 滑块弹不出来（限流）也没关系，只要 uuid 抓到了就行
            pass
        for _ in range(30):
            if captured.get("uuid"):
                break
            page.wait_for_timeout(200)

    uuid = captured.get("uuid", "")
    if uuid:
        log.info("抓到新 uuid：%s", uuid)
    else:
        log.warning("没抓到 uuid")
    return uuid


def fetch_uuid_or_die(profile=None) -> str:
    """同 :func:`fetch_uuid`，但抓不到就直接报错退出。"""
    uuid = fetch_uuid(profile=profile)
    if not uuid:
        print("没抓到 uuid（可能是服务端限流，等 30 秒再试）", file=sys.stderr)
        raise SystemExit(1)
    return uuid
