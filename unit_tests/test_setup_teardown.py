"""前置 / 后置清理的单元测试。

测的是三件事：

1. 用例本身失败时，teardown 再失败**不能盖住**原始失败原因
2. 用例通过时，teardown 失败要算作用例失败
3. 多个 teardown 里第一个失败，后面的**照样要跑**（不能只清一半）
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from testcases.case_runner import CaseRunner, TeardownError


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _make_runner() -> CaseRunner:
    """造一个不登录、不发真实请求的运行器。"""
    runner = CaseRunner(env="test")
    runner._login_projects = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    runner._project_config = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
        "host": "http://mock.invalid",
        "mysql": None,
    }
    return runner


class _Boom(Exception):
    """模拟"清理接口挂了"。"""


def _case_with_failing_step() -> dict[str, Any]:
    """一条 steps 必然失败的用例。

    用 ``noop`` 步骤 —— 不发真实请求（否则测试会打到 mock.invalid 上）。
    失败由 assert_handler 制造。
    """
    return {
        "testcase": {
            "name": "原始失败的用例",
            "steps": [
                {
                    "id": "check",
                    "operation_type": "noop",
                    "value": {"code": 500},
                    "assert": [{"type": "equal", "field": "value.code", "expected": 200}],
                }
            ],
        }
    }


def _case_with_teardown_that_raises() -> dict[str, Any]:
    """一条 steps 会失败、teardown 也会失败的用例。"""
    case = _case_with_failing_step()
    case["testcase"]["teardowns"] = [
        {
            "id": "clean",
            "operation_type": "api",
            "path": "/api/clean",
            "method": "POST",
        }
    ]
    return case


# ---------------------------------------------------------------------------
# 1. teardown 失败不能盖住原始失败
# ---------------------------------------------------------------------------


def test_cleanup_failure_does_not_hide_original_error() -> None:
    """用例本身失败时，报告里要能看到原始失败原因。"""
    runner = _make_runner()

    # 让 teardown 必挂
    def _explode(*_args: Any, **_kwargs: Any) -> Any:
        raise _Boom("清理接口 500")

    runner._run_teardowns = _explode  # type: ignore[method-assign]

    # 让断言失败
    def _fail_assertion(**_kwargs: Any) -> None:
        raise AssertionError("[code] 期望等于 200，实际 500")

    runner.assert_handler.handle_assertion = _fail_assertion  # type: ignore[method-assign]

    with pytest.raises(AssertionError) as exc_info:
        runner.run_case(_case_with_teardown_that_raises(), "demo")

    message = str(exc_info.value)
    # 原始失败原因还在
    assert "期望等于 200" in message
    # 清理失败的详情也附上了
    assert "清理接口 500" in message


def test_original_error_type_is_preserved() -> None:
    """原始异常的类型不能被清理异常替换掉。"""
    runner = _make_runner()

    def _explode(*_args: Any, **_kwargs: Any) -> Any:
        raise _Boom("清理接口 500")

    runner._run_teardowns = _explode  # type: ignore[method-assign]

    def _fail_assertion(**_kwargs: Any) -> None:
        raise ValueError("这是业务层的错")

    runner.assert_handler.handle_assertion = _fail_assertion  # type: ignore[method-assign]

    with pytest.raises(ValueError):
        runner.run_case(_case_with_teardown_that_raises(), "demo")


# ---------------------------------------------------------------------------
# 2. 用例通过、teardown 失败 -> 用例失败
# ---------------------------------------------------------------------------


def test_cleanup_failure_alone_fails_the_case() -> None:
    """用例过了但清理挂了，用例也要算失败（环境留了脏数据）。"""
    runner = _make_runner()

    def _explode(*_args: Any, **_kwargs: Any) -> Any:
        raise _Boom("清理接口 500")

    runner._run_teardowns = _explode  # type: ignore[method-assign]

    case = {
        "testcase": {
            "name": "用例本身没问题",
            "steps": [
                {"id": "noop", "operation_type": "noop", "value": {"ok": True}},
            ],
            "teardowns": [
                {
                    "id": "clean",
                    "operation_type": "api",
                    "path": "/api/clean",
                    "method": "POST",
                }
            ],
        }
    }

    with pytest.raises(TeardownError) as exc_info:
        runner.run_case(case, "demo")

    assert "清理接口 500" in str(exc_info.value)


def test_cleanup_error_is_assertion_error() -> None:
    """清理失败算 failed 而不是 error。"""
    from testcases.case_runner import TeardownError as TE

    assert issubclass(TE, AssertionError)


# ---------------------------------------------------------------------------
# 3. 多个 teardown：第一个失败，后面的照样跑
# ---------------------------------------------------------------------------


def test_later_teardowns_still_run_after_one_fails() -> None:
    """第一个清理失败不能把后面的清理跳掉。"""
    runner = _make_runner()

    called: list[str] = []

    class _FakeHandler:
        def handle_assertion(self, **_kwargs: Any) -> None:
            return None

    runner.assert_handler = _FakeHandler()  # type: ignore[assignment]

    def _fake_send(method: str, url: str, **_kwargs: Any) -> Any:
        called.append(url)
        if url.endswith("/first"):
            raise _Boom("第一个清理失败")
        return {"code": 200}, {"status_code": 200, "headers": {}, "elapsed_ms": 1}

    from utils.http import request_handler

    original = request_handler.RequestHandler.send_with_retry_full
    request_handler.RequestHandler.send_with_retry_full = staticmethod(_fake_send)  # type: ignore[assignment]
    try:
        case = {
            "testcase": {
                "name": "多个清理",
                "steps": [
                    {"id": "noop", "operation_type": "noop", "value": {"ok": True}},
                ],
                "teardowns": [
                    {
                        "id": "first",
                        "operation_type": "api",
                        "path": "/first",
                        "method": "POST",
                    },
                    {
                        "id": "second",
                        "operation_type": "api",
                        "path": "/second",
                        "method": "POST",
                    },
                ],
            }
        }

        with pytest.raises(TeardownError):
            runner.run_case(case, "demo")
    finally:
        request_handler.RequestHandler.send_with_retry_full = original  # type: ignore[assignment]

    # 第二个清理确实被执行了
    assert any(url.endswith("/second") for url in called), (
        f"第一个 teardown 失败后，后面的 teardown 应该继续执行，实际只跑了 {called}"
    )


def test_multiple_cleanup_failures_are_all_reported() -> None:
    """多个清理都失败时，报错里要能看到每一个。"""
    runner = _make_runner()

    class _FakeHandler:
        def handle_assertion(self, **_kwargs: Any) -> None:
            return None

    runner.assert_handler = _FakeHandler()  # type: ignore[assignment]

    def _fake_send(method: str, url: str, **_kwargs: Any) -> Any:
        raise _Boom(f"失败于 {url}")

    from utils.http import request_handler

    original = request_handler.RequestHandler.send_with_retry_full
    request_handler.RequestHandler.send_with_retry_full = staticmethod(_fake_send)  # type: ignore[assignment]
    try:
        case = {
            "testcase": {
                "name": "多个清理都失败",
                "steps": [
                    {"id": "noop", "operation_type": "noop", "value": {"ok": True}},
                ],
                "teardowns": [
                    {"id": "a", "operation_type": "api", "path": "/a", "method": "POST"},
                    {"id": "b", "operation_type": "api", "path": "/b", "method": "POST"},
                ],
            }
        }

        with pytest.raises(TeardownError) as exc_info:
            runner.run_case(case, "demo")
    finally:
        request_handler.RequestHandler.send_with_retry_full = original  # type: ignore[assignment]

    message = str(exc_info.value)
    assert "/a" in message
    assert "/b" in message


# ---------------------------------------------------------------------------
# 4. setup 失败：后面的 setup 照样要跑
# ---------------------------------------------------------------------------


def test_later_setups_still_run_after_one_fails() -> None:
    """setup 第一个失败，后面的 setup 也要跑（否则清理会对不上号）。"""
    runner = _make_runner()

    called: list[str] = []

    class _FakeHandler:
        def handle_assertion(self, **_kwargs: Any) -> None:
            return None

    runner.assert_handler = _FakeHandler()  # type: ignore[assignment]

    def _fake_send(method: str, url: str, **_kwargs: Any) -> Any:
        called.append(url)
        if url.endswith("/setup_first"):
            raise _Boom("第一个 setup 失败")
        return {"code": 200}, {"status_code": 200, "headers": {}, "elapsed_ms": 1}

    from utils.http import request_handler

    original = request_handler.RequestHandler.send_with_retry_full
    request_handler.RequestHandler.send_with_retry_full = staticmethod(_fake_send)  # type: ignore[assignment]
    try:
        case = {
            "testcase": {
                "name": "多个 setup",
                "steps": [
                    {"id": "noop", "operation_type": "noop", "value": {"ok": True}},
                ],
                "setups": [
                    {
                        "id": "s1",
                        "operation_type": "api",
                        "path": "/setup_first",
                        "method": "POST",
                    },
                    {
                        "id": "s2",
                        "operation_type": "api",
                        "path": "/setup_second",
                        "method": "POST",
                    },
                ],
            }
        }

        with pytest.raises(AssertionError) as exc_info:
            runner.run_case(case, "demo")
    finally:
        request_handler.RequestHandler.send_with_retry_full = original  # type: ignore[assignment]

    # setup 失败 -> steps 不执行
    assert "setup 执行失败" in str(exc_info.value)
    # 第二个 setup 确实跑了
    assert any(url.endswith("/setup_second") for url in called), (
        f"第一个 setup 失败后，后面的 setup 应该继续执行，实际只跑了 {called}"
    )


def test_teardown_runs_when_setup_fails() -> None:
    """setup 失败时 teardown 照样要跑（数据造了一半得清）。"""
    runner = _make_runner()

    cleaned: list[str] = []

    class _FakeHandler:
        def handle_assertion(self, **_kwargs: Any) -> None:
            return None

    runner.assert_handler = _FakeHandler()  # type: ignore[assignment]

    def _fake_send(method: str, url: str, **_kwargs: Any) -> Any:
        if url.endswith("/setup"):
            raise _Boom("setup 挂了")
        cleaned.append(url)
        return {"code": 200}, {"status_code": 200, "headers": {}, "elapsed_ms": 1}

    from utils.http import request_handler

    original = request_handler.RequestHandler.send_with_retry_full
    request_handler.RequestHandler.send_with_retry_full = staticmethod(_fake_send)  # type: ignore[assignment]
    try:
        case = {
            "testcase": {
                "name": "setup 失败也要清理",
                "steps": [
                    {"id": "noop", "operation_type": "noop", "value": {"ok": True}},
                ],
                "setups": [
                    {"id": "s", "operation_type": "api", "path": "/setup", "method": "POST"},
                ],
                "teardowns": [
                    {"id": "t", "operation_type": "api", "path": "/cleanup", "method": "POST"},
                ],
            }
        }

        with pytest.raises(AssertionError):
            runner.run_case(case, "demo")
    finally:
        request_handler.RequestHandler.send_with_retry_full = original  # type: ignore[assignment]

    assert any(url.endswith("/cleanup") for url in cleaned), "setup 失败后 teardown 必须执行"


# ---------------------------------------------------------------------------
# 5. setup / teardown 的头、重登录、安全闸
# ---------------------------------------------------------------------------


def _capture_sends(runner: CaseRunner) -> list[dict[str, Any]]:
    """把发请求的调用记录下来（不真发）。"""
    calls: list[dict[str, Any]] = []

    def _fake_send(method: str, url: str, **kwargs: Any) -> Any:
        calls.append({"method": method, "url": url, **kwargs})
        return {"code": 200, "data": {"id": 1}}, {
            "status_code": 200,
            "headers": {},
            "elapsed_ms": 1,
        }

    from utils.http import request_handler

    request_handler.RequestHandler.send_with_retry_full = staticmethod(_fake_send)  # type: ignore[assignment]
    runner._calls = calls  # type: ignore[attr-defined]
    return calls


def test_setup_uses_default_headers() -> None:
    """setup 没写 Authorization 时，框架要自动补上鉴权头。

    以前 setup 只发用例里显式写的头 —— 没写就是匿名请求，
    表现是"前置莫名其妙全失败"。
    """
    runner = _make_runner()
    runner._default_headers = lambda *_a, **_k: {  # type: ignore[method-assign]
        "Authorization": "tk-from-default",
        "Accept": "application/json",
    }

    class _FakeHandler:
        def handle_assertion(self, **_kwargs: Any) -> None:
            return None

    runner.assert_handler = _FakeHandler()  # type: ignore[assignment]
    calls = _capture_sends(runner)

    from utils.http import request_handler

    original = request_handler.RequestHandler.send_with_retry_full
    try:
        case = {
            "testcase": {
                "name": "setup 带头",
                "setups": [
                    {
                        "id": "造数据",
                        "operation_type": "api",
                        "path": "/sys/menu",
                        "method": "POST",
                    }
                ],
                "steps": [
                    {"id": "noop", "operation_type": "noop", "value": {"ok": True}},
                ],
            }
        }
        runner.run_case(case, "demo")
    finally:
        request_handler.RequestHandler.send_with_retry_full = original  # type: ignore[assignment]

    setup_call = next(c for c in calls if c["url"].endswith("/sys/menu"))
    assert setup_call["headers"].get("Authorization") == "tk-from-default"


def test_teardown_uses_default_headers() -> None:
    """teardown 同样要自动补鉴权头。"""
    runner = _make_runner()
    runner._default_headers = lambda *_a, **_k: {  # type: ignore[method-assign]
        "Authorization": "tk-from-default"
    }

    class _FakeHandler:
        def handle_assertion(self, **_kwargs: Any) -> None:
            return None

    runner.assert_handler = _FakeHandler()  # type: ignore[assignment]
    calls = _capture_sends(runner)

    from utils.http import request_handler

    original = request_handler.RequestHandler.send_with_retry_full
    try:
        case = {
            "testcase": {
                "name": "teardown 带头",
                "steps": [
                    {
                        "id": "新建",
                        "method": "POST",
                        "path": "/sys/menu",
                        "data": {"name": "t"},
                    },
                ],
                "teardowns": [
                    {
                        "id": "清理",
                        "operation_type": "api",
                        "path": "/sys/menu/{{新建.data.id}}",
                        "method": "DELETE",
                    }
                ],
            }
        }
        runner.run_case(case, "demo")
    finally:
        request_handler.RequestHandler.send_with_retry_full = original  # type: ignore[assignment]

    cleanup_call = next(c for c in calls if c["method"] == "DELETE")
    assert cleanup_call["headers"].get("Authorization") == "tk-from-default"


def test_teardown_does_not_blanket_allow_dangerous() -> None:
    """纯查询用例里误配的 DELETE 不能自动放行（以前写死 True）。"""
    from testcases.case_runner import _teardown_deletes_case_data

    read_only_case = {
        "setups": [],
        "steps": [{"id": "查询", "method": "GET", "path": "/sys/menu/page"}],
    }
    delete_td = {"id": "清理", "method": "DELETE", "path": "/sys/menu/1"}

    assert _teardown_deletes_case_data(read_only_case, delete_td) is False


def test_teardown_allows_delete_of_data_it_created() -> None:
    """用例自己造过数据，后置清理删它要放行。"""
    from testcases.case_runner import _teardown_deletes_case_data

    case = {
        "setups": [
            {"id": "造数据", "method": "POST", "path": "/sys/menu"},
            {"id": "查数据", "method": "GET", "path": "/sys/menu/table"},
        ],
        "steps": [{"id": "noop", "operation_type": "noop"}],
    }
    # 查询后取最大 id 再删 —— 引用的是 GET 结果，但用例确实造过数据
    delete_td = {
        "id": "清理",
        "method": "DELETE",
        "path": '/sys/menu/{{tools.max_id(查数据.data, "menuId")}}',
    }

    assert _teardown_deletes_case_data(case, delete_td) is True


def test_teardown_explicit_allow_dangerous_wins() -> None:
    from testcases.case_runner import _teardown_deletes_case_data

    case = {"setups": [], "steps": [{"id": "查询", "method": "GET", "path": "/x"}]}
    delete_td = {
        "id": "清理",
        "method": "DELETE",
        "path": "/sys/menu/1",
        "allow_dangerous": True,
    }

    assert _teardown_deletes_case_data(case, delete_td) is True
