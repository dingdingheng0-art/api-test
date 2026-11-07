"""Mock 服务的单元测试。

测的是三件事：

1. 规则怎么匹配（路径、方法、参数、请求体）
2. 拦截表能不能在发请求前把真实请求替换掉
3. ``operation_type: mock`` 步骤能不能直接返回预设响应
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.infra.mock_server import MockConnectionError, MockRegistry, MockRule


# ---------------------------------------------------------------------------
# 规则匹配
# ---------------------------------------------------------------------------


def test_exact_path_matches() -> None:
    """精确路径只匹配自己。"""
    rule = MockRule({"match": {"path": "/api/user"}, "response": {"ok": True}})

    assert rule.matches("GET", "/api/user") is True
    assert rule.matches("GET", "/api/users") is False
    assert rule.matches("GET", "/api/user/detail") is False


def test_trailing_slash_tolerated() -> None:
    """末尾斜杠写不写都算同一个路径。"""
    rule = MockRule({"match": {"path": "/api/user"}, "response": {}})

    assert rule.matches("GET", "/api/user/") is True


def test_wildcard_path_matches_multiple() -> None:
    """通配路径能匹配一整段。"""
    rule = MockRule({"match": {"path": "/api/user/*"}, "response": {}})

    assert rule.matches("GET", "/api/user/1") is True
    assert rule.matches("GET", "/api/user/detail") is True
    # * 只匹配一段，不跨 /
    assert rule.matches("GET", "/api/user/1/detail") is False


def test_double_wildcard_crosses_segments() -> None:
    """双通配能跨多段。"""
    rule = MockRule({"match": {"path": "/api/**"}, "response": {}})

    assert rule.matches("GET", "/api/a") is True
    assert rule.matches("GET", "/api/a/b/c") is True


def test_method_must_match() -> None:
    """方法不一样就不拦。"""
    rule = MockRule({"match": {"method": "POST", "path": "/api/user"}, "response": {}})

    assert rule.matches("POST", "/api/user") is True
    assert rule.matches("GET", "/api/user") is False


def test_method_is_case_insensitive() -> None:
    """方法大小写不敏感。"""
    rule = MockRule({"match": {"method": "post", "path": "/api/user"}, "response": {}})

    assert rule.matches("POST", "/api/user") is True


def test_params_must_all_match() -> None:
    """规则里写了几个参数，就得都对得上。"""
    rule = MockRule(
        {
            "match": {"path": "/api/list", "params": {"page": "1", "size": "10"}},
            "response": {},
        }
    )

    assert rule.matches("GET", "/api/list", params={"page": "1", "size": "10"}) is True
    # 一个对不上就不拦
    assert rule.matches("GET", "/api/list", params={"page": "2", "size": "10"}) is False
    # 少一个也不对
    assert rule.matches("GET", "/api/list", params={"page": "1"}) is False


def test_unspecified_params_are_not_restricted() -> None:
    """规则没管的参数不限制。"""
    rule = MockRule({"match": {"path": "/api/list", "params": {"page": "1"}}, "response": {}})

    assert rule.matches("GET", "/api/list", params={"page": "1", "extra": "x"}) is True


def test_body_match_is_partial() -> None:
    """请求体只比对规则里写了的字段。"""
    rule = MockRule(
        {
            "match": {"method": "POST", "path": "/api/order", "body": {"type": "vip"}},
            "response": {},
        }
    )

    assert rule.matches("POST", "/api/order", body={"type": "vip", "extra": 1}) is True
    assert rule.matches("POST", "/api/order", body={"type": "normal"}) is False


def test_project_isolation() -> None:
    """A 项目的规则不拦 B 项目的请求。"""
    rule = MockRule({"project": "mall4j", "match": {"path": "/api/user"}, "response": {}})

    assert rule.matches("GET", "/api/user", project="mall4j") is True
    assert rule.matches("GET", "/api/user", project="other") is False


def test_disabled_rule_never_matches() -> None:
    """enabled: false 的规则直接不生效。"""
    rule = MockRule({"enabled": False, "match": {"path": "/api/user"}, "response": {}})

    assert rule.matches("GET", "/api/user") is False


def test_empty_match_matches_everything() -> None:
    """没写 match 的规则拦所有请求（慎用，但合法）。"""
    rule = MockRule({"response": {}})

    assert rule.matches("GET", "/anything") is True


# ---------------------------------------------------------------------------
# 响应构造
# ---------------------------------------------------------------------------


def test_build_response_default_status() -> None:
    """默认 200。"""
    rule = MockRule({"match": {"path": "/x"}, "response": {"code": 200}})

    body, meta = rule.build_response()

    assert body == {"code": 200}
    assert meta["status_code"] == 200
    assert meta["mocked"] is True


def test_build_response_custom_status_and_headers() -> None:
    """状态码和响应头都能配。"""
    rule = MockRule(
        {
            "match": {"path": "/x"},
            "status": 504,
            "headers": {"Content-Type": "application/json"},
            "response": {"code": 504},
        }
    )

    body, meta = rule.build_response()

    assert body == {"code": 504}
    assert meta["status_code"] == 504
    # 响应头键统一小写（和真实请求保持一致）
    assert meta["headers"]["content-type"] == "application/json"


def test_build_response_raises_on_error() -> None:
    """配了 error 就抛网络错误。"""
    rule = MockRule({"match": {"path": "/x"}, "error": "连接被拒绝"})

    with pytest.raises(MockConnectionError):
        rule.build_response()


# ---------------------------------------------------------------------------
# 拦截表
# ---------------------------------------------------------------------------


@pytest.fixture()
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MockRegistry:
    """造一个只认临时目录的规则表。"""
    reg = MockRegistry()
    reg.clear()

    mocks_dir = tmp_path / "mocks"
    mocks_dir.mkdir()
    (mocks_dir / "demo.yaml").write_text(
        """
project: demo
mocks:
  - name: 库存查询超时
    match:
      method: GET
      path: /prod/prod/stock
      params:
        skuId: "123"
    status: 504
    response:
      code: 504
      message: 服务超时

  - name: 通用兜底
    match:
      path: /prod/prod/**
    response:
      code: 200
""",
        encoding="utf-8",
    )

    monkeypatch.setattr(type(reg), "root", property(lambda _self: mocks_dir))
    reg.load(force=True)
    yield reg
    reg.clear()


def test_registry_loads_rules(registry: MockRegistry) -> None:
    """规则能被读出来。"""
    assert len(registry.load()) == 2


def test_registry_finds_by_params(registry: MockRegistry) -> None:
    """参数对得上才命中精确规则。"""
    rule = registry.find_rule(
        "GET", "/prod/prod/stock", params={"skuId": "123"}, project="demo"
    )

    assert rule is not None
    assert rule.name == "库存查询超时"


def test_registry_falls_back_to_wildcard(registry: MockRegistry) -> None:
    """精确规则不匹配时，通配规则兜底。"""
    rule = registry.find_rule(
        "GET", "/prod/prod/other", params={"skuId": "999"}, project="demo"
    )

    assert rule is not None
    assert rule.name == "通用兜底"


def test_registry_other_project_not_matched(registry: MockRegistry) -> None:
    """别的项目不受影响。"""
    rule = registry.find_rule("GET", "/prod/prod/stock", project="other")

    assert rule is None


def test_try_intercept_returns_none_when_no_match(registry: MockRegistry) -> None:
    """没命中就返回 None，让调用方走真实请求。"""
    result = registry.try_intercept("GET", "/unrelated/path", project="demo")

    assert result is None


def test_try_intercept_returns_response(registry: MockRegistry) -> None:
    """命中就返回预设响应和元信息。"""
    result = registry.try_intercept(
        "GET", "/prod/prod/stock", params={"skuId": "123"}, project="demo"
    )

    assert result is not None
    body, meta = result
    assert body["code"] == 504
    assert meta["status_code"] == 504


# ---------------------------------------------------------------------------
# 作为步骤类型
# ---------------------------------------------------------------------------


def _run_mock_step(case: dict[str, Any]) -> Any:
    """跑一条只含 mock 步骤的用例，返回步骤变量。"""
    from testcases.case_runner import CaseRunner

    runner = CaseRunner(env="test")
    runner._login_projects = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    runner._project_config = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
        "host": "http://mock.invalid",
        "mysql": None,
    }

    captured: dict[str, Any] = {}

    class _Recorder:
        def handle_assertion(self, **_kwargs: Any) -> None:
            captured["asserted"] = True

    runner.assert_handler = _Recorder()  # type: ignore[assignment]
    runner.run_case(case, "demo")
    return captured


def test_mock_step_returns_configured_response() -> None:
    """mock 步骤直接返回预设响应。"""
    captured = _run_mock_step(
        {
            "testcase": {
                "name": "mock 用例",
                "steps": [
                    {
                        "id": "fake",
                        "operation_type": "mock",
                        "mock": {"response": {"code": 5001, "message": "库存不足"}},
                        "assert": [{"type": "equal", "field": "code", "expected": 5001}],
                    }
                ],
            }
        }
    )

    assert captured.get("asserted") is True


def test_mock_step_can_raise_network_error() -> None:
    """配了 error 就当网络错误抛出去。"""
    from testcases.case_runner import CaseRunner

    runner = CaseRunner(env="test")
    runner._login_projects = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    runner._project_config = lambda *_args, **_kwargs: {}  # type: ignore[method-assign]

    with pytest.raises(MockConnectionError):
        runner.run_case(
            {
                "testcase": {
                    "name": "mock 错误",
                    "steps": [
                        {
                            "id": "boom",
                            "operation_type": "mock",
                            "mock": {"error": "服务不可用"},
                        }
                    ],
                }
            },
            "demo",
        )
