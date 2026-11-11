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

from pathlib import Path

import pytest

from framework.session_scope import targets_interface_tests
from utils.logs.log_manager import log


def _is_interface_session(config: pytest.Config) -> bool:
    """跑单元测试时不要加载工厂、不要兜底清理。

    **为什么**：根 conftest 无条件加载了本插件，``pytest unit_tests``
    也会走到这里 —— 单测不需要工厂配置，更不该在 session 结束时
    去清理"清理栈"、往 allure 目录里贴附件。
    """
    return targets_interface_tests(config)


def pytest_configure(config: pytest.Config) -> None:
    """session 开始：加载工厂。"""
    if not _is_interface_session(config):
        return

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
    if not _is_interface_session(session.config):
        return

    from factory.cleanup import stack

    if stack.size == 0:
        return

    log.info("清理栈里还有 %s 项待清理数据，开始兜底清理", stack.size)
    result = stack.run_all()

    # 清理结果贴进 allure 报告。
    #
    # ⚠️ 这里**不能**用 ``allure.attach``：sessionfinish 时所有用例容器都已
    # 关闭，reporter 取不到当前 executable，会抛 ``KeyError: None``（以前
    # 被 ``except: pass`` 吞掉，清理结果从来没进过报告）。改成按 Allure 2
    # 的文件格式，挂到最后一条已经落盘的结果上。
    attached = False
    try:
        from framework.allure_util import attach_json_to_latest_result

        attached = attach_json_to_latest_result(
            result,
            name="测试数据清理结果",
            results_dir=_allure_results_dir(session),
        )
    except Exception as exc:  # noqa: BLE001 - 贴附件失败不能影响退出码
        log.warning("写测试数据清理附件失败: %s", exc)

    if not attached:
        # 没开 allure 报告，或者一条用例都没跑（没有结果可挂）——
        # 至少把内容记到日志里，别让清理结果凭空消失。
        log.info("测试数据清理结果: %s", result)

    # 关掉工厂复用的数据库连接（不关的话进程退出前一直挂着）
    try:
        from factory.base import registry

        registry.clear()
    except Exception as exc:  # noqa: BLE001 - 关连接失败不影响退出码
        log.warning("清理工厂注册表失败: %s", exc)


def _allure_results_dir(session: pytest.Session) -> Path | None:
    """取本轮 pytest 的 ``--alluredir`` 目录（没传就是 None）。"""
    option = getattr(session.config.option, "allure_report_dir", None)
    if not option:
        return None
    path = Path(str(option))
    if not path.is_absolute():
        path = Path(str(session.config.rootpath)) / path
    return path


@pytest.fixture(scope="session")
def cleanup_stack() -> object:
    """拿到清理栈（要自己登记清理任务时用）。"""
    from factory.cleanup import stack

    return stack
