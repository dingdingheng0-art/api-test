"""HTTP 请求处理。

对外只暴露一个 ``send_request``，内部按方法区分参数位置：

- GET   → data 放 query
- POST  → 默认放 JSON body；``Content-Type: application/x-www-form-urlencoded`` 时放表单
- PUT / DELETE → 放 JSON body

返回值统一是响应体的 JSON（dict/list）；不是 JSON 就抛异常并带上原文片段。

每次请求会：
- 记一条脱敏日志
- 在 allure 报告里生成一个 step（装了 allure 才有）
- 把请求 / 响应挂到上下文，供报告使用
"""
from __future__ import annotations

import time
from typing import Any

import requests

from utils.logs.log_manager import RequestLogger, dumps, log
from utils.infra.safety_guard import check_request as check_safety


class RequestHandler:
    """发起 HTTP 请求并返回解析后的 JSON。"""

    DEFAULT_TIMEOUT = 30

    # 网络类错误（连接超时、服务端限流）重试几次
    DEFAULT_RETRIES = 3

    # 当前用例的请求日志记录器（由 fixture / runner 设置）
    _request_logger: RequestLogger | None = None

    # 复用的 Session（不走系统代理）
    #
    # 为什么不用 ``requests.request()``：它每次新建 Session，``trust_env``
    # 默认是 True，会去读环境变量 / Windows 注册表里的系统代理。
    # 机器上装了 VPN 或抓包工具时（本机代理 127.0.0.1:xxxx），请求会被
    # 代理拦下并返回 502 —— 服务端明明正常，客户端却全失败。
    # 测试框架要直连目标地址，所以固定一个 ``trust_env=False`` 的 Session。
    _http_session: requests.Session | None = None

    @classmethod
    def _session(cls) -> requests.Session:
        """拿到共用的 HTTP Session（绕过系统代理）。"""
        if cls._http_session is None:
            session = requests.Session()
            session.trust_env = False
            cls._http_session = session
        return cls._http_session

    # 最近一次响应的状态码（重试插件用来判断"是不是服务端 5xx"）
    _last_status: int | None = None

    @classmethod
    def get_last_status(cls) -> int | None:
        """最近一次响应的 HTTP 状态码（没发过请求就是 None）。"""
        return cls._last_status

    @classmethod
    def reset_last_status(cls) -> None:
        """清掉状态码记录（每次重试前调一下）。"""
        cls._last_status = None

    # 全局默认请求头（每个项目可以有自己的）
    # 比如登录接口要带 uuid 头、业务接口要带 accesstoken 头
    _default_headers: dict[str, Any] = {}

    @classmethod
    def set_request_logger(cls, request_logger: RequestLogger | None) -> None:
        """设置当前上下文的请求日志记录器。"""
        cls._request_logger = request_logger

    @classmethod
    def get_request_logger(cls) -> RequestLogger | None:
        return cls._request_logger

    @classmethod
    def set_default_headers(cls, headers: dict[str, Any] | None) -> None:
        """设置全局默认请求头（会被用例自己的 headers 覆盖）。"""
        cls._default_headers = dict(headers or {})

    @classmethod
    def get_default_headers(cls) -> dict[str, Any]:
        return dict(cls._default_headers)

    # 当前请求所属的限流分组（默认 "default"）
    _rate_limit_key: str = "default"

    @classmethod
    def set_rate_limit_key(cls, key: str | None) -> None:
        """设置当前请求按哪个分组限流（一般是项目名）。"""
        cls._rate_limit_key = key or "default"

    @staticmethod
    def _acquire_rate_limit(url: str) -> None:
        """发包前拿限流令牌。

        限流器没初始化（比如纯单元测试环境）就跳过。
        等令牌超时**不会**让请求失败 —— 宁可慢一点，也别丢请求。
        """
        from utils.infra.rate_limiter import get_limiter

        try:
            limiter = get_limiter()
        except Exception:  # noqa: BLE001 - 限流器初始化失败不影响发请求
            return

        if not limiter.enabled:
            return

        key = RequestHandler._rate_limit_key or "default"
        try:
            limiter.acquire(key)
        except TimeoutError as exc:
            log.warning("限流等待超时，继续发请求: %s (%s)", url, exc)

    @staticmethod
    def send_request(
        method: str,
        url: str,
        headers: dict[str, Any] | None = None,
        data: Any = None,
        params: dict[str, Any] | None = None,
        files: Any = None,
        timeout: int | None = None,
    ) -> Any:
        """发请求，只返回响应体（老接口，保持兼容）。"""
        body, _ = RequestHandler.send_request_full(
            method=method,
            url=url,
            headers=headers,
            data=data,
            params=params,
            files=files,
            timeout=timeout,
        )
        return body

    @staticmethod
    def send_request_full(
        method: str,
        url: str,
        headers: dict[str, Any] | None = None,
        data: Any = None,
        params: dict[str, Any] | None = None,
        files: Any = None,
        timeout: int | None = None,
        allow_dangerous: bool = False,
        response_type: str = "json",
    ) -> tuple[Any, dict[str, Any]]:
        """发请求，返回 ``(响应体, 响应元信息)``。

        响应元信息里有三样东西，是给断言用的：

        - ``status_code`` —— HTTP 状态码
        - ``headers``     —— 响应头（小写键）
        - ``elapsed_ms``  —— 耗时（毫秒）

        ``response_type`` 决定怎么解析响应体：

        - ``json``（默认）—— 解析成 dict/list，解析不了就报错
        - ``binary`` / ``file`` —— 下载类接口（导出 Excel 等）返回二进制，
          不解析成 JSON，而是给一个描述字典::

              {"__binary__": True, "content_type": "...", "size": 12345,
               "content": b"PK\\x03\\x04..."}

          断言里用 ``field: size`` / ``field: content_type`` 就能校验，
          不会因为「响应不是 JSON」直接判失败。

        .. code-block:: yaml

            assert:
              - type: status_code
                expected: 200
              - type: response_header
                field: Content-Type
                expected: application/json
              - type: elapsed_time
                expected: {lte: 3000}
        """
        method = (method or "GET").upper()
        # 默认头在前，用例自己的头在后（用例可以覆盖默认值）
        merged_headers = {**RequestHandler.get_default_headers(), **(headers or {})}
        headers = merged_headers
        timeout = timeout or RequestHandler.DEFAULT_TIMEOUT

        # 危险写操作防护（DELETE 基础配置数据这类）
        # 放在发包之前 —— 数据删了再拦就晚了。
        check_safety(method, url, data, allow=allow_dangerous)

        # 限流：发包之前先拿令牌（防止把测试环境压垮）
        RequestHandler._acquire_rate_limit(url)

        request_logger = RequestHandler.get_request_logger()
        payload = {
            "headers": headers,
            "params": params if params is not None else (data if method == "GET" else None),
            "data": data if method != "GET" else None,
        }
        if request_logger:
            request_logger.log_request(method, url, **payload)

        kwargs: dict[str, Any] = {"headers": headers, "timeout": timeout}
        if params is not None:
            kwargs["params"] = params
        if files is not None:
            kwargs["files"] = files

        if method == "GET":
            kwargs["params"] = params if params is not None else data
        elif method == "POST":
            content_type = str(headers.get("Content-Type", ""))
            if files is not None:
                pass  # 文件上传，data 不再传
            elif "x-www-form-urlencoded" in content_type:
                kwargs["data"] = data
            else:
                kwargs["json"] = data
        elif method in ("PUT", "PATCH", "DELETE"):
            # 线上大部分写接口收表单（application/x-www-form-urlencoded），
            # 只有少数接口收 JSON —— 按 Content-Type 决定怎么发，
            # 不然服务端会收到一个空 body，报「XX名称不能为空」。
            content_type = str(headers.get("Content-Type", ""))
            if "x-www-form-urlencoded" in content_type:
                kwargs["data"] = data
            else:
                kwargs["json"] = data
        else:
            raise ValueError(f"不支持的 HTTP 方法: {method}")

        started = time.perf_counter()
        response = None
        last_exc: requests.RequestException | None = None

        # 网络类错误自动重试（服务端偶发限流/连接超时很常见）
        retries = RequestHandler.DEFAULT_RETRIES
        for attempt in range(1, retries + 1):
            try:
                response = RequestHandler._session().request(method, url, **kwargs)
                break
            except requests.RequestException as exc:
                last_exc = exc
                # 4xx 是业务问题，重试也没用（连接类错误才重试）
                if attempt >= retries:
                    break
                wait = attempt * 2
                log.warning(
                    "请求失败（第 %s/%s 次），%s 秒后重试: %s %s -> %s",
                    attempt, retries, wait, method, url, exc,
                )
                time.sleep(wait)

        if response is None:
            message = f"请求失败: {method} {url} -> {last_exc}"
            if request_logger:
                request_logger.log_error(message)
            assert last_exc is not None
            raise last_exc

        elapsed_ms = int((time.perf_counter() - started) * 1000)

        # 记一下状态码（重试插件判断 5xx 用）
        RequestHandler._last_status = response.status_code

        if response_type in ("binary", "file"):
            # 下载类接口：响应体是文件（xlsx/zip/图片），别按 JSON 解析
            content = response.content or b""
            body = {
                "__binary__": True,
                "content_type": str(response.headers.get("Content-Type", "")),
                "size": len(content),
                "content": content,
            }
            # 日志里只记元信息 —— 二进制塞进日志会把文件撑爆
            logged = {
                "__binary__": True,
                "content_type": body["content_type"],
                "size": body["size"],
            }
            if request_logger:
                request_logger.log_response(response.status_code, logged, elapsed_ms)
            else:
                log.info(
                    "← HTTP %s | 二进制响应 %s（%s 字节）| %sms",
                    response.status_code,
                    body["content_type"] or "unknown",
                    body["size"],
                    elapsed_ms,
                )
        else:
            try:
                body = response.json()
            except ValueError:
                message = (
                    f"响应不是 JSON：HTTP {response.status_code} | {response.text[:500]!r}"
                )
                if request_logger:
                    request_logger.log_error(message)
                log.error("%s", message)
                response.raise_for_status()
                raise RuntimeError(message)

            if request_logger:
                request_logger.log_response(response.status_code, body, elapsed_ms)
            else:
                log.info("← HTTP %s | %s | %sms", response.status_code, dumps(body, limit=800), elapsed_ms)

        meta = {
            "status_code": response.status_code,
            "headers": {k.lower(): v for k, v in response.headers.items()},
            "elapsed_ms": elapsed_ms,
        }
        return body, meta

    # ------------------------------------------------------------------
    # 业务级重试：服务端限流
    # ------------------------------------------------------------------

    # 服务端返回这些 code 表示"请求太频繁，等会儿再试"
    THROTTLE_CODES = (1003,)

    @staticmethod
    def is_throttled(body: Any) -> bool:
        """响应体是不是"被限流了"。"""
        if not isinstance(body, dict):
            return False
        return body.get("code") in RequestHandler.THROTTLE_CODES

    # ------------------------------------------------------------------
    # 业务级重试：登录态失效
    # ------------------------------------------------------------------

    # 服务端返回这些 code 表示"你的登录态没了"（token 过期 / 被登出踢掉）
    UNAUTHORIZED_CODES = ("A00004", "401", 401)

    # 消息里出现这些关键词也算登录态失效（不同版本 code 可能不一样）
    UNAUTHORIZED_MSG_KEYWORDS = ("Unauthorized", "未登录", "登录已过期", "token已过期")

    @staticmethod
    def is_unauthorized(body: Any) -> bool:
        """响应体是不是"登录态失效了"。

        为什么需要单独识别这个：

        有些系统的登出接口（``/logOut``）会**直接注销服务端的会话**，
        一旦有用例调了它，同一批跑的其它用例手里的 token 就全废了 ——
        后面每条都返回"未授权"。

        与其让几百条用例凭空全挂，不如让框架在这里**自动重新登录一次再重试**，
        一次登录几秒钟，比报错让人排查半天划算得多。
        """
        if not isinstance(body, dict):
            return False

        code = body.get("code")
        if code is not None and str(code) in RequestHandler.UNAUTHORIZED_CODES:
            return True

        msg = str(body.get("msg") or "")
        return any(k in msg for k in RequestHandler.UNAUTHORIZED_MSG_KEYWORDS)

    @classmethod
    def send_with_retry(
        cls,
        method: str,
        url: str,
        headers: dict[str, Any] | None = None,
        data: Any = None,
        params: dict[str, Any] | None = None,
        files: Any = None,
        timeout: int | None = None,
        max_attempts: int = 3,
        wait_seconds: float = 5.0,
    ) -> Any:
        """发请求，遇到**业务层限流**自动退避重试（只返回响应体）。"""
        body, _ = cls.send_with_retry_full(
            method=method,
            url=url,
            headers=headers,
            data=data,
            params=params,
            files=files,
            timeout=timeout,
            max_attempts=max_attempts,
            wait_seconds=wait_seconds,
        )
        return body

    @classmethod
    def send_with_retry_full(
        cls,
        method: str,
        url: str,
        headers: dict[str, Any] | None = None,
        data: Any = None,
        params: dict[str, Any] | None = None,
        files: Any = None,
        timeout: int | None = None,
        max_attempts: int = 3,
        wait_seconds: float = 5.0,
        allow_dangerous: bool = False,
        response_type: str = "json",
    ) -> tuple[Any, dict[str, Any]]:
        """发请求，遇到**业务层限流**自动退避重试。

        返回 ``(响应体, 响应元信息)``。

        和 :meth:`send_request` 的区别：

        - ``send_request`` 只在**网络异常**（超时、连接断开）时重试
        - 这个方法额外处理 **HTTP 200 但 body 里 code=1003「访问过于频繁」**

        业务流用例会连着调好几个统计接口，很容易撞上服务端 QPS 限制。
        不处理的话，用例会随机失败（时好时坏），最让人怀疑测试本身。

        Args:
            max_attempts: 总共试几次
            wait_seconds: 每次重试前等多久（会随次数递增）
        """
        attempt = 0
        while True:
            attempt += 1
            body, meta = cls.send_request_full(
                method=method,
                url=url,
                headers=headers,
                data=data,
                params=params,
                files=files,
                timeout=timeout,
                allow_dangerous=allow_dangerous,
                response_type=response_type,
            )
            if not cls.is_throttled(body) or attempt >= max_attempts:
                return body, meta

            wait = wait_seconds * attempt
            log.warning(
                "服务端限流（code=%s），%.0f 秒后重试（第 %s/%s 次）: %s %s",
                body.get("code"), wait, attempt, max_attempts, method, url,
            )
            time.sleep(wait)
