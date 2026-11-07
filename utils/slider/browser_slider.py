"""用 Playwright 驱动真实浏览器完成「拖动滑块解锁 → 登录」。

核心难点是滑块：前端要求鼠标**按下 → 移动 → 松开**三步走，位置差就是
``xPos``，同时也是拼图块左边缘的偏移量。Playwright 的
``mouse.down/move/up`` 正好能模拟这套动作，拖完服务端会返回校验结果，
页面文案会变成「解锁成功」。

另外这台机器上装了 VPN，系统代理指向 127.0.0.1:12001，浏览器默认会走代理，
导致连接被掐断。所以启动时统一加 ``--proxy-server=direct://`` 强制直连。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from playwright.sync_api import Page, Response, sync_playwright

from utils.logs.logger_util import get_logger
from utils.slider.slider_gap import SliderChallenge

log = get_logger(__name__)

# 让浏览器绕过系统代理直连（否则会被 VPN 代理掐断）
DIRECT_PROXY_ARGS = ["--proxy-server=direct://"]

LOGIN_URL = ""
SLIDER_API_MARKER = ""


@dataclass
class SliderResult:
    """拖动滑块的结果。"""

    success: bool
    x_pos: int
    message: str


class SliderSolveError(RuntimeError):
    """滑块校验没通过。"""


class BrowserSliderSolver:
    """在真实浏览器里解开拼图滑块。

    用法::

        with BrowserSliderSolver(headless=True) as solver:
            page = solver.open_login_page()
            solver.fill_credentials(page, "admin", "123456")
            solver.open_slider(page)
            challenge = solver.read_challenge(page)
            x = get_gap_finder("shape").locate(challenge)
            result = solver.drag_to(page, x)
    """

    def __init__(
        self,
        *,
        headless: bool = True,
        viewport: dict[str, int] | None = None,
        slow_mo: int = 0,
        user_agent: str | None = None,
        login_url: str = "",
        slider_marker: str = "",
    ) -> None:
        self.headless = headless
        self.viewport = viewport or {"width": 1440, "height": 900}
        self.slow_mo = slow_mo
        self.user_agent = user_agent
        # 登录页地址 + 滑块接口路径特征（不传就用项目档案里的）
        self.login_url = login_url or LOGIN_URL
        self.slider_marker = slider_marker or SLIDER_API_MARKER
        self._pw = None
        self._browser = None
        self._ctx = None
        # 滑块初始化接口返回的原始题目信息
        self._api: dict[str, Any] = {}
        # 最近一次校验接口的响应体
        self._verify_resp: dict[str, Any] | None = None

    # ---------------- 生命周期 ----------------

    def __enter__(self) -> "BrowserSliderSolver":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def start(self) -> "BrowserSliderSolver":
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=self.headless,
            args=DIRECT_PROXY_ARGS,
            slow_mo=self.slow_mo,
        )
        self._ctx = self._browser.new_context(
            viewport=self.viewport,
            user_agent=self.user_agent,
        )
        log.info("浏览器已启动（headless=%s）", self.headless)
        return self

    def close(self) -> None:
        for obj in (self._ctx, self._browser):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass
        self._pw = self._browser = self._ctx = None

    def new_page(self) -> Page:
        if self._ctx is None:
            raise RuntimeError("浏览器还没启动，先调 start()")
        page = self._ctx.new_page()
        # 监听滑块接口，把服务端下发的原始尺寸抓下来
        page.on("response", self._on_response)
        return page

    def _on_response(self, response: Response) -> None:
        if self.slider_marker not in response.url:
            return
        # 注意：校验接口返回 HTTP 400，response.json() 可能因内容类型/编码抛异常，
        # 所以先取原始 text 再自己解析，失败也不要吞掉（否则会误判成"没响应"）。
        try:
            raw = response.text()
        except Exception as exc:
            log.warning("读取滑块响应失败：%s", exc)
            return
        try:
            body = json.loads(raw)
        except Exception:
            log.warning("滑块响应不是 JSON：%r", raw[:200])
            return

        result = body.get("result")
        if isinstance(result, dict) and "backImage" in result:
            # init 响应：只更新题目，**不要**动 _verify_resp
            # （校验失败后前端会重新 init，若在这里清空会把失败误判成"没响应"）
            self._api = result
        elif "verificationEnums" in response.url or "xPos" in response.url:
            self._verify_resp = body
            log.info("滑块校验响应：%s", body)

    # ---------------- 页面操作 ----------------

    def open_login_page(self) -> Page:
        """打开登录页。

        ⚠️ 不用 ``wait_until="networkidle"`` ——
        登录页有轮询/心跳请求，networkidle 永远等不到，
        结果就是每次都卡满 60 秒超时（失败时再重试 3 次 = 卡几小时）。

        改成等 DOM 加载完，再等滑块相关元素出现 —— 更快也更稳。
        """
        page = self.new_page()
        page.goto(self.login_url, wait_until="domcontentloaded", timeout=30_000)

        # 等登录面板渲染出来（最多 15 秒），拿不到就早点报错，
        # 别让后面每个选择器都各超时一次
        try:
            page.locator(".login-panel input").first.wait_for(
                state="visible", timeout=15_000
            )
        except Exception:  # noqa: BLE001 - 页面结构变了也别把整个套件卡住
            log.warning("登录页没等到登录面板（可能页面结构变了），继续尝试")

        log.info("登录页已打开：%s", page.url)
        return page

    @staticmethod
    def fill_credentials(page: Page, username: str, password: str) -> None:
        """填账号密码。"""
        inputs = page.locator(".login-panel input")
        inputs.nth(0).fill(username)
        inputs.nth(1).fill(password)

    @staticmethod
    def open_slider(page: Page, retries: int = 3) -> None:
        """点「登录」按钮，弹出滑块。

        服务端有频率限制，偶尔会因为太频繁导致滑块弹不出来，
        这时等一下再点即可。
        """
        last_exc: Exception | None = None
        for attempt in range(retries):
            try:
                page.locator("button.login-btn").click()
                page.locator(".verify-content .imgBox").wait_for(
                    state="visible", timeout=10_000
                )
                return
            except Exception as exc:
                last_exc = exc
                log.warning("滑块未弹出（第 %s 次尝试）：%s", attempt + 1, exc)
                time.sleep(3 + attempt * 3)
        raise SliderSolveError(f"多次尝试后滑块仍未弹出：{last_exc}")

    def read_challenge(self, page: Page) -> SliderChallenge:
        """读出背景图、拼图块和尺寸信息（尺寸以服务端返回为准）。"""
        dom = page.evaluate(
            """() => {
                const imgBox = document.querySelector('.verify-content .imgBox');
                const back = imgBox.querySelector('img');
                const piece = imgBox.querySelector('img.slider');
                return {
                    backImage: back.src,
                    slidingImage: piece.src,
                    pieceLeft: parseFloat(piece.style.left || '0'),
                    pieceTop: parseFloat(piece.style.top || '0'),
                    imgBoxWidth: imgBox.clientWidth,
                    imgBoxHeight: imgBox.clientHeight,
                    pieceWidth: piece.clientWidth,
                    pieceHeight: piece.clientHeight
                };
            }"""
        )
        api = self._api or {}
        challenge = SliderChallenge(
            back_image_b64=dom["backImage"],
            sliding_image_b64=dom["slidingImage"],
            random_y=int(api.get("randomY", dom["pieceTop"])),
            original_width=int(api.get("originalWidth", dom["imgBoxWidth"])),
            original_height=int(api.get("originalHeight", dom["imgBoxHeight"])),
            slider_width=int(api.get("sliderWidth", dom["pieceWidth"])),
            slider_height=int(api.get("sliderHeight", dom["pieceHeight"])),
        )
        log.info(
            "滑块题目：原图 %sx%s，块 %sx%s，randomY=%s",
            challenge.original_width, challenge.original_height,
            challenge.slider_width, challenge.slider_height,
            challenge.random_y,
        )
        return challenge

    def drag_to(self, page: Page, x_pos: int) -> SliderResult:
        """把滑块拖到 x_pos（缺口左边缘像素，相对背景图左上角）。

        前端记录的是「按下点」与「松开点」的水平距离，并夹在
        ``[0, originalWidth - 43]`` 之间；这个距离同时就是拼图块左边缘的偏移。
        """
        img_box = page.locator(".verify-content .imgBox")
        swiper = page.locator(".verify-content .swiper")
        box = img_box.bounding_box()
        sbox = swiper.bounding_box()
        if not box or not sbox:
            raise SliderSolveError("找不到滑块元素（imgBox / swiper）")

        origin_w = self._api.get("originalWidth") or box["width"]
        origin_w = int(origin_w)
        max_x = origin_w - 43  # 前端硬编码的上限
        x_pos = max(0, min(int(x_pos), max_x))

        # 背景图在页面上可能被 CSS 缩放，页面位移要按比例换算
        scale = box["width"] / origin_w

        start_x = sbox["x"] + sbox["width"] / 2
        start_y = sbox["y"] + sbox["height"] / 2
        delta = x_pos * scale
        end_x = start_x + delta

        log.info(
            "拖动滑块：xPos=%s（页面位移 %.1f px），起点=(%.1f, %.1f)",
            x_pos, delta, start_x, start_y,
        )

        # 重置上一轮的判定（必须在拖动之前，响应随时可能回来）
        self._verify_resp = None

        page.mouse.move(start_x, start_y)
        page.mouse.down()
        # 分多步移动，模拟真人轨迹（前端按 mousemove 累加位移）
        steps = 20
        for i in range(1, steps + 1):
            page.mouse.move(start_x + delta * i / steps, start_y)
            time.sleep(0.01)
        page.mouse.up()

        # 等校验接口返回。注意：服务端有限流，校验响应可能延迟十几秒才到。
        # 双重条件：接口响应回来了 **或** 页面文案变了（前端拿到响应必然改文案）。
        deadline = time.time() + 30.0
        while time.time() < deadline:
            if self._verify_resp is not None:
                break
            text = self._handle_text(page)
            if "成功" in text or "失败" in text:
                break
            time.sleep(0.2)

        result = self._read_result(page, x_pos)
        if self._verify_resp is not None:
            resp = self._verify_resp
            # 服务端契约（对照前端 JS `e.success ? e.result : ...`）：
            # - 通过：success=true，result 是**下一题的初始化数据**（dict）
            # - 失败：success=true，result=null
            # 所以判据是 success 为真 且 result 为真值（不为 None/False）。
            res = resp.get("result")
            ok = resp.get("success") is True and bool(res)
            result = SliderResult(
                success=ok,
                x_pos=x_pos,
                message=result.message or str(resp.get("message") or ""),
            )
            log.info("服务端判定：%s", resp)
        return result

    def wait_login_success(self, page: Page, timeout: float = 15.0) -> bool:
        """等登录成功（URL 离开 /login，或页面出现用户信息）。

        注意前端行为：**滑块校验失败后会自动换一道新题**，所以调用方
        要在失败时重新读题、重新定位再拖一次。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if "/login" not in page.url:
                log.info("登录成功，当前页：%s", page.url)
                return True
            time.sleep(0.3)
        log.warning("等待登录成功超时，url=%s", page.url)
        return False

    # ---------------- 内部 ----------------

    @staticmethod
    def _handle_text(page: Page) -> str:
        try:
            return page.locator(".verify-content .handle .text").inner_text(timeout=2000)
        except Exception:
            return ""

    def _read_result(self, page: Page, x_pos: int) -> SliderResult:
        text = self._handle_text(page)
        success = "成功" in text and "失败" not in text
        return SliderResult(success=success, x_pos=x_pos, message=text)
