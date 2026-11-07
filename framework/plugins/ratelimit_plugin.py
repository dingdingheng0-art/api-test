"""限流插件 —— 按项目分别限制请求速率。

**做什么**：session 开始时把 ``config.yaml`` 里的限流配置交给限流器，
session 结束时清空。

限流器本身是在 ``utils/rate_limiter.py`` 里的，
真正"发包前拿令牌"的动作在 ``RequestHandler`` 里做 ——
这样不管请求是从用例发起的，还是从框架内部（登录、工厂）发起的，
都会被限流管住。
"""

from __future__ import annotations

import pytest

from utils.logs.log_manager import log


def pytest_configure(config: pytest.Config) -> None:
    """session 开始：初始化限流器。"""
    from utils.config.config_manager import ConfigManager
    from utils.infra.rate_limiter import get_limiter, reset_limiter

    # 先重置（可能上一轮跑过）
    reset_limiter()

    try:
        full_config = ConfigManager.get_all()
    except Exception as exc:  # noqa: BLE001
        log.warning("读取配置失败，限流不启用: %s", exc)
        return

    rate_config = full_config.get("rate_limit")
    if not rate_config:
        log.info("没配 rate_limit，限流不启用")
        return

    limiter = get_limiter(rate_config)
    if limiter.enabled:
        log.info("限流已启用: %s", rate_config)
    else:
        log.info("限流已关闭")


def pytest_unconfigure(config: pytest.Config) -> None:
    """session 结束：清空限流器。"""
    from utils.infra.rate_limiter import reset_limiter

    reset_limiter()
