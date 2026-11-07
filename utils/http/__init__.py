"""HTTP 层：请求执行、会话/客户端封装。

- :mod:`~utils.http.request_handler` —— 单次请求（限流 / 重试 / 安全闸 / 日志）
- :mod:`~utils.http.request_util`    —— 底层 HTTP 封装、登录链路请求
- :mod:`~utils.http.api_client`      —— 业务接口客户端（自动带鉴权头）
"""
