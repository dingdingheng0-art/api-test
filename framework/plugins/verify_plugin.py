"""校验层插件 —— 管数据库 / 缓存 / MQ 连接的建立和关闭。

**做什么**：

1. session 开始：读配置，把数据库 / Redis / MQ 客户端准备好
2. session 结束：关掉连接

**为什么不在用例里现连现关**：连接很贵。一条用例断言 5 次，
现连现关就是 5 次 TCP + 认证。几百条用例跑下来，慢到无法接受。
"""

from __future__ import annotations

from typing import Any

import pytest

from utils.logs.log_manager import log


def pytest_configure(config: pytest.Config) -> None:
    """session 开始：准备校验层的连接。"""
    from utils.config.config_manager import ConfigManager

    try:
        full_config = ConfigManager.get_all()
    except Exception as exc:  # noqa: BLE001 - 配置读不到就跳过校验层
        log.warning("读取配置失败，校验层不启用: %s", exc)
        return

    verify_config = {
        "db": full_config.get("db"),
        "redis": full_config.get("redis"),
        "mq": full_config.get("mq"),
    }

    if not any(verify_config.values()):
        log.info("没配置数据库 / 缓存 / MQ，校验层不启用")
        return

    from verify.manager import VerifyManager

    manager = VerifyManager.get()
    try:
        manager.setup(verify_config)
    except Exception as exc:  # noqa: BLE001 - 配置有问题也不该让整个 run 挂掉
        log.error("校验层初始化失败（相关断言会报错）: %s", exc)


def pytest_unconfigure(config: pytest.Config) -> None:
    """session 结束：关掉所有连接。"""
    from verify.manager import VerifyManager

    try:
        VerifyManager.get().teardown()
    except Exception as exc:  # noqa: BLE001
        log.warning("关闭校验层连接时出错: %s", exc)


@pytest.fixture(scope="session")
def verify_manager() -> Any:
    """拿到校验层总管（用例里要直接查数据库时用）。"""
    from verify.manager import VerifyManager

    return VerifyManager.get()
