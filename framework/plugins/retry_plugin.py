"""用例级重试插件 —— 只重试"疑似环境抖动"的失败。

**为什么需要**：网络抖一下、服务器偶尔返回 503、网关重启 ——
这些跟用例本身没关系，却会让用例挂掉，报告里一片红，
真正的问题反而被淹没。

**关键原则：只重试"环境问题"，不重试"用例问题"**。

| 失败原因 | 重试吗 | 为什么 |
|---|---|---|
| 连接超时 / 连接被重置 | ✅ | 网络抖动，重试就好 |
| 服务端 5xx | ✅ | 服务端临时问题 |
| 限流（code=1003） | ✅ | 等一下就好 |
| 断言不匹配 | ❌ | 这是真 bug，重试只是掩盖 |
| 业务返回失败（code != 200） | ❌ | 用例预期的失败 |
| 前置数据没造出来 | ❌ | 配置或用例问题 |

**实现方式**：自己实现 ``pytest_runtest_call``，
在里面循环调用 ``item.runtest()``。失败了就判断该不该重试。

配置（``config.yaml``）::

    retry:
      enabled: true
      max_attempts: 3          # 总共试几次（含第一次）
      wait_seconds: 2          # 每次重试前等多久
      backoff: 1.5             # 等待时间的递增倍数

也可以用命令行关掉：``python scripts/run.py --no-retry``
或者用环境变量：``$env:APITEST_NO_RETRY = "1"``

**标记强制重试**：用例上打 ``@pytest.mark.flaky``，
不管什么原因失败都重试（适合那些确实不稳定的接口）。
"""

from __future__ import annotations

import os
import time
from typing import Any

import pytest

from framework.session_scope import targets_interface_tests
from utils.logs.log_manager import log

#: 关掉重试的环境变量（文档里一直写着，但以前**根本没实现**）
NO_RETRY_ENV = "APITEST_NO_RETRY"

# 本次运行的配置（pytest_configure 里填）
#
# **默认关闭**：``pytest_runtest_call`` 是全局包装器，根 conftest 又会
# 把本插件带进 ``pytest unit_tests``。默认开着的话，单测里任何一次
# 网络异常 / 上一条用例留下的 5xx 都会触发"环境抖动"重试，
# 睡几秒再跑一遍 —— 单元测试必须是确定性的，不能重试。
# 只有确认是接口 session（``pytest_configure``）才会打开。
_RETRY_CONFIG: dict[str, Any] = {
    "enabled": False,
    "max_attempts": 3,
    "wait_seconds": 2.0,
    "backoff": 1.5,
}


def pytest_addoption(parser: pytest.Parser) -> None:
    """加一个 ``--no-retry`` 开关。"""
    group = parser.getgroup("apitest", "接口自动化测试")
    group.addoption(
        "--no-retry",
        action="store_true",
        default=False,
        help="不要重试疑似环境抖动的失败用例",
    )
    group.addoption(
        "--retry-attempts",
        type=int,
        default=None,
        help="每条用例最多试几次（默认读 config.yaml 的 retry.max_attempts）",
    )


def pytest_configure(config: pytest.Config) -> None:
    """读配置，确定重试策略。"""
    global _RETRY_CONFIG

    if not targets_interface_tests(config):
        _RETRY_CONFIG = {**_RETRY_CONFIG, "enabled": False}
        return

    # 两个开关都认：命令行 ``--no-retry`` 和环境变量 ``APITEST_NO_RETRY=1``
    #
    # 环境变量这条以前只写在文档里，代码里没读 —— 照着文档设置的人会发现
    # 重试照样开着。
    if config.getoption("--no-retry", default=False) or _env_disabled():
        _RETRY_CONFIG = {**_RETRY_CONFIG, "enabled": False}
        log.info(
            "用例重试已关闭（%s）",
            "--no-retry" if config.getoption("--no-retry", default=False) else NO_RETRY_ENV,
        )
        return

    from utils.config.config_manager import ConfigManager

    try:
        full_config = ConfigManager.get_all()
    except Exception:  # noqa: BLE001
        full_config = {}

    raw = full_config.get("retry") or {}
    _RETRY_CONFIG = {
        "enabled": bool(raw.get("enabled", True)),
        "max_attempts": int(raw.get("max_attempts", 3)),
        "wait_seconds": float(raw.get("wait_seconds", 2.0)),
        "backoff": float(raw.get("backoff", 1.5)),
    }

    attempts = config.getoption("--retry-attempts", default=None)
    if attempts:
        _RETRY_CONFIG["max_attempts"] = max(1, int(attempts))

    if _RETRY_CONFIG["enabled"]:
        log.info(
            "用例重试已启用：最多试 %s 次，间隔 %ss",
            _RETRY_CONFIG["max_attempts"], _RETRY_CONFIG["wait_seconds"],
        )


def _env_disabled() -> bool:
    """``APITEST_NO_RETRY=1`` 关了没有。"""
    return os.environ.get(NO_RETRY_ENV, "").strip().lower() in ("1", "true", "yes", "on")


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item) -> None:
    """执行用例，必要时重试。

    **必须写成 wrapper（包装器）而不是普通实现**。

    原因：``pytest_runtest_call`` 这个钩子 pytest 自己有一个默认实现，
    默认实现里就调了 ``item.runtest()``（也就是真正跑一遍用例）。
    钩子不是 "firstresult" 的 —— 所有实现都会被依次调用。

    如果这里再写一个普通实现并调 ``item.runtest()``，
    等于跑了两遍：**每条用例都会被执行两次**。
    （接口测试里就是每个接口被调两次，报告数字也会翻倍。）

    写成 wrapper 之后，pytest 的默认实现被包在我们里面，
    我们只在它失败的时候再补跑，不会重复。

    用法::

        try:
            return (yield)          # pytest 默认实现在这里面跑用例
        except BaseException as exc:
            ...                     # 失败了，判断要不要重试
    """
    # 快照当前 allure 结果里已有的步骤/附件/标签。
    #
    # 重试是直接再调一次 ``item.runtest()``，allure 的用例容器这期间
    # 一直开着 —— 第一次失败时贴的"请求响应/失败归因/异常详情"和
    # 各个 step 都会留在最终结果里，报告上就成了"通过"的用例挂着
    # 红色的失败证据，步骤还重复两遍。
    allure_result = _current_allure_result()
    baseline = _snapshot_allure(allure_result)

    try:
        return (yield)
    except BaseException as exc:  # noqa: BLE001 - 要判断该不该重试
        if not _RETRY_CONFIG["enabled"]:
            raise

        max_attempts = max(1, int(_RETRY_CONFIG["max_attempts"]))
        if max_attempts == 1:
            raise

        if not _should_retry(item, exc):
            log.info(
                "用例 %s 失败（原因不是环境抖动，不重试）: %s",
                item.name, exc,
            )
            raise

        wait = float(_RETRY_CONFIG["wait_seconds"])
        backoff = float(_RETRY_CONFIG["backoff"])

        for attempt in range(2, max_attempts + 1):
            log.warning(
                "用例 %s 第 %s/%s 次失败（疑似环境抖动，%.1fs 后重试）: %s",
                item.name, attempt - 1, max_attempts, wait, exc,
            )
            time.sleep(wait)
            wait *= backoff

            # 重试前把上一次失败尝试的痕迹从 allure 结果里截掉 ——
            # 否则最终"通过"的用例会带着失败时的步骤和附件
            dropped = _drop_failed_attempt(allure_result, baseline)
            if dropped:
                log.info(
                    "用例 %s 重试前清掉上一次失败留下的 %s 项报告内容",
                    item.name, dropped,
                )

            # 每次重试前清掉"上一次的状态"，好判断这次是不是环境问题
            from utils.http.request_handler import RequestHandler

            RequestHandler.reset_last_status()

            try:
                item.runtest()
            except BaseException as retry_exc:  # noqa: BLE001
                exc = retry_exc
                continue

            # 重试成功 —— 吞掉原来的异常，让用例算通过
            #
            # 但第一次失败时已经登记过归因了（终端汇总按模块列失败用）。
            # 不撤掉的话，汇总里会把这条**已经通过**的用例算成失败。
            try:
                from utils.infra.failure_classifier import unregister_failure

                unregister_failure(item.nodeid)
            except Exception:  # noqa: BLE001 - 撤销登记失败不该影响用例结论
                pass

            log.info("用例 %s 第 %s 次重试通过（已撤销失败归因）", item.name, attempt)
            return None

        # 重试完了还是失败
        raise exc


#: 一次失败尝试会往 allure 结果里追加的东西。
#: 重试前要把这些列表截回「尝试开始时」的长度。
_ATTEMPT_ARTIFACTS = ("steps", "attachments", "labels", "links")


def _current_allure_result() -> Any:
    """当前用例正在写的 allure ``TestResult``（没装 allure 时返回 None）。

    通过 ``allure_commons`` 的插件管理器找 listener，而不是直接 import
    ``allure_pytest.listener`` —— 后者没装 allure 时导入就炸。
    """
    try:
        from allure_commons._core import plugin_manager as allure_plugin_manager
    except ImportError:  # pragma: no cover - 没装 allure
        return None

    for plugin in allure_plugin_manager.get_plugins():
        reporter = getattr(plugin, "allure_logger", None)
        if reporter is None:
            continue
        try:
            return reporter.get_test(None)
        except Exception:  # noqa: BLE001 - 拿不到就不清理，不能影响用例
            return None
    return None


def _snapshot_allure(result: Any) -> dict[str, int] | None:
    """记下各列表当前长度（重试后截回这里）。"""
    if result is None:
        return None
    return {
        name: len(getattr(result, name, []) or [])
        for name in _ATTEMPT_ARTIFACTS
    }


def _drop_failed_attempt(result: Any, baseline: dict[str, int] | None) -> int:
    """把失败尝试产生的步骤/附件/标签截掉。

    Returns:
        清掉了几项（0 = 没装 allure 或本来就没有可清的）。
    """
    if result is None or baseline is None:
        return 0

    dropped = 0
    for name, keep in baseline.items():
        items = getattr(result, name, None)
        if not isinstance(items, list):
            continue
        extra = len(items) - keep
        if extra > 0:
            del items[keep:]
            dropped += extra
    return dropped


def _should_retry(item: pytest.Item, exc: BaseException) -> bool:
    """判断这个失败该不该重试。"""
    # 1) 打了 flaky 标记的用例 —— 无条件重试
    if item.get_closest_marker("flaky"):
        return True

    # 2) 网络类异常 —— 典型的环境抖动
    if _is_network_error(exc):
        return True

    # 3) 服务端 5xx
    from utils.http.request_handler import RequestHandler

    status = RequestHandler.get_last_status()
    if status is not None and status >= 500:
        return True

    # 4) 其它 —— 断言失败之类，不重试
    return False


def _is_network_error(exc: BaseException) -> bool:
    """是不是网络类异常。"""
    try:
        import requests
    except ImportError:  # pragma: no cover
        return False

    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return True

    # requests 把它包在别的异常里（urllib3 那些）
    cause = exc.__cause__ or exc.__context__
    if cause is not None and cause is not exc:
        return _is_network_error(cause)

    return False
