"""数据工厂插件 —— 加载工厂配置 + 兜底清理。

**两件事**：

1. session 开始：从 ``configs/factories.yaml`` 加载所有工厂
2. session 结束：**兜底清理** —— 把清理栈里剩下的数据全清掉

第 2 件事是关键。用例里正常写了 teardown，数据当场就清了；
但如果用例中途挂了、teardown 没跑到，工厂造的数据就留在库里了。
清理栈兜底 —— 保证"造出来的数据一定会被清掉"。

清理结果会贴进 allure 报告，方便确认环境是干净的。
"""

from __future__ import annotations

import pytest

from utils.logs.log_manager import log


def pytest_configure(config: pytest.Config) -> None:
    """session 开始：加载工厂。"""
    from factory.base import load_factories

    try:
        count = load_factories()
    except Exception as exc:  # noqa: BLE001 - 工厂加载失败不影响其它用例
        log.error("加载测试数据工厂失败: %s", exc)
        return

    if count:
        log.info("已加载 %s 个测试数据工厂", count)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """session 结束：兜底清理 + 贴报告。"""
    from factory.cleanup import stack

    if stack.size == 0:
        return

    log.info("清理栈里还有 %s 项待清理数据，开始兜底清理", stack.size)
    result = stack.run_all()

    # 清理结果贴进 allure 报告
    try:
        from framework.allure_util import attach_json

        attach_json(result, name="测试数据清理结果")
    except Exception:  # noqa: BLE001
        pass


@pytest.fixture(scope="session")
def cleanup_stack() -> object:
    """拿到清理栈（要自己登记清理任务时用）。"""
    from factory.cleanup import stack

    return stack
