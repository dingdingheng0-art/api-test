"""AI 层（用例生成 / 缺陷分析）的单元测试。

**不联网** —— 用假的 LLM 客户端验证提示词构造和结果整理逻辑。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai.capabilities.case_gen import _normalize_ai_case, generate_cases
from ai.capabilities.client import LLMClient, LLMConfig, parse_json_response
from ai.capabilities.diagnosis import (
    Diagnosis,
    FailureInfo,
    classify_by_rules,
    diagnose,
    render_markdown,
)
from tools.importer.models import Endpoint, Param


# ---------------------------------------------------------------------------
# JSON 解析
# ---------------------------------------------------------------------------


class TestParseJsonResponse:
    def test_plain_json(self) -> None:
        assert parse_json_response('{"a": 1}') == {"a": 1}

    def test_json_in_code_block(self) -> None:
        text = '```json\n{"a": 1}\n```'
        assert parse_json_response(text) == {"a": 1}

    def test_json_with_surrounding_text(self) -> None:
        text = '好的，这是结果：\n{"a": 1}\n希望有帮助。'
        assert parse_json_response(text) == {"a": 1}

    def test_json_array(self) -> None:
        assert parse_json_response("[1, 2, 3]") == [1, 2, 3]

    def test_nested_braces(self) -> None:
        text = '结果 {"outer": {"inner": "}"}} 完毕'
        assert parse_json_response(text) == {"outer": {"inner": "}"}}

    def test_empty_raises(self) -> None:
        with pytest.raises(Exception):
            parse_json_response("")

    def test_garbage_raises(self) -> None:
        with pytest.raises(Exception):
            parse_json_response("完全不是 JSON")


# ---------------------------------------------------------------------------
# 客户端配置
# ---------------------------------------------------------------------------


#: ``LLMConfig.from_config`` 除了读传入的 dict，还会去读这些环境变量。
#: 本机 ``.env`` 里配了 AI 的话，"没配置"的场景就不成立了 ——
#: 测"未配置"时必须先把它们清掉，否则单测结果取决于开发机的环境。
_AI_ENV_VARS = (
    "AI_BASE_URL",
    "AI_API_KEY",
    "AI_MODEL",
    "OPENAI_BASE_URL",
    "OPENAI_API_KEY",
    "OPENAI_MODEL",
    "DASHSCOPE_API_KEY",
)


@pytest.fixture()
def no_ai_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """把 AI 相关的环境变量全清掉。"""
    for name in _AI_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


class TestLLMConfig:
    def test_not_configured_without_settings(self, no_ai_env: None) -> None:
        config = LLMConfig.from_config({})
        assert config.configured is False

    def test_configured_with_all_fields(self) -> None:
        config = LLMConfig.from_config(
            {"base_url": "https://x/v1", "api_key": "k", "model": "m"}
        )
        assert config.configured is True

    def test_env_vars_are_used(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AI_BASE_URL", "https://env/v1")
        monkeypatch.setenv("AI_API_KEY", "env-key")
        monkeypatch.setenv("AI_MODEL", "env-model")
        config = LLMConfig.from_config({})
        assert config.configured is True
        assert config.base_url == "https://env/v1"

    def test_client_reports_availability(self, no_ai_env: None) -> None:
        client = LLMClient(LLMConfig.from_config({}))
        assert client.available is False


# ---------------------------------------------------------------------------
# 用例生成
# ---------------------------------------------------------------------------


def _make_endpoint() -> Endpoint:
    """造一个接口定义（带必填参数）。"""
    return Endpoint(
        id="create_order",
        method="POST",
        path="/order/create",
        name="创建订单",
        summary="创建订单",
        tags=["order"],
        body_params=[
            Param(name="orderNo", location="body", required=True, type="string",
                  example="SO001"),
            Param(name="mobile", location="body", required=True, type="string",
                  example="13800000000"),
            Param(name="amount", location="body", required=False, type="number",
                  example=100),
        ],
        body={"orderNo": "SO001", "mobile": "13800000000", "amount": 100},
        response_example={"code": 200, "success": True, "result": {"id": 1}},
    )


class TestCaseGenerationLocal:
    """本地模板生成（不联网）。"""

    def test_generates_positive_case(self) -> None:
        cases = generate_cases([_make_endpoint()], use_ai=False)
        assert cases
        case = cases[0]["testcase"]
        assert case["steps"]
        assert case["steps"][0]["path"] == "/order/create"

    def test_marks_include_ai_generated(self) -> None:
        cases = generate_cases([_make_endpoint()], use_ai=False)
        assert "ai_generated" in cases[0]["testcase"]["marks"]

    def test_generates_negative_case_for_required_param(self) -> None:
        cases = generate_cases([_make_endpoint()], use_ai=False, max_cases_per_endpoint=2)
        assert len(cases) >= 2
        negative = cases[1]["testcase"]
        assert "negative" in negative["marks"]
        # 必填参数应该被去掉了
        data = negative["steps"][0].get("data") or {}
        assert "orderNo" not in data or "mobile" not in data

    def test_max_cases_respected(self) -> None:
        cases = generate_cases([_make_endpoint()], use_ai=False, max_cases_per_endpoint=1)
        assert len(cases) == 1


class TestNormalizeAICase:
    def test_normalizes_valid_case(self) -> None:
        raw = {
            "name": "订单号为空",
            "description": "验证订单号不能为空",
            "steps": [
                {
                    "id": "s1",
                    "path": "/order/create",
                    "method": "post",
                    "data": {"orderNo": ""},
                    "assert": [{"type": "equal", "field": "code", "expected": 400}],
                }
            ],
        }
        case = _normalize_ai_case(raw)
        assert case is not None
        assert case["testcase"]["steps"][0]["method"] == "POST"
        assert "ai_generated" in case["testcase"]["marks"]

    def test_rejects_case_without_name(self) -> None:
        assert _normalize_ai_case({"steps": [{"path": "/x"}]}) is None

    def test_rejects_case_without_steps(self) -> None:
        assert _normalize_ai_case({"name": "x", "steps": []}) is None

    def test_rejects_non_dict(self) -> None:
        assert _normalize_ai_case("不是字典") is None


class TestGenerateWithFakeAI:
    """用假客户端验证 AI 分支。"""

    def test_uses_llm_when_available(self) -> None:
        class FakeClient:
            available = True

            def chat_json(self, prompt: str, system: str = "", **kw: object) -> dict:
                return {
                    "cases": [
                        {
                            "name": "正常创建",
                            "steps": [
                                {
                                    "path": "/order/create",
                                    "method": "POST",
                                    "assert": [{"type": "equal", "field": "code",
                                                "expected": 200}],
                                }
                            ],
                        }
                    ]
                }

        cases = generate_cases(
            [_make_endpoint()], use_ai=True, ai_client=FakeClient()
        )
        assert len(cases) == 1
        assert cases[0]["testcase"]["name"]

    def test_falls_back_to_local_when_llm_fails(self) -> None:
        class BrokenClient:
            available = True

            def chat_json(self, *args: object, **kw: object) -> dict:
                raise RuntimeError("服务挂了")

        cases = generate_cases(
            [_make_endpoint()], use_ai=True, ai_client=BrokenClient()
        )
        # 退回本地模式，至少能出正例
        assert cases
        assert "ai_generated" in cases[0]["testcase"]["marks"]


# ---------------------------------------------------------------------------
# 缺陷分析
# ---------------------------------------------------------------------------


class TestRuleClassification:
    def test_connection_error_is_environment(self) -> None:
        failure = FailureInfo(
            case="用例A", error_type="ConnectionError", message="连接被拒绝"
        )
        diagnosis = classify_by_rules(failure)
        assert diagnosis is not None
        assert diagnosis.is_environment is True
        assert "网络" in diagnosis.category

    def test_timeout_is_environment(self) -> None:
        failure = FailureInfo(case="用例B", error_type="ReadTimeout", message="超时")
        diagnosis = classify_by_rules(failure)
        assert diagnosis is not None
        assert diagnosis.is_environment is True

    def test_server_5xx_is_environment(self) -> None:
        failure = FailureInfo(case="用例C", message="HTTP 503 Service Unavailable")
        diagnosis = classify_by_rules(failure)
        assert diagnosis is not None
        assert diagnosis.is_environment is True

    def test_rate_limit_is_environment(self) -> None:
        failure = FailureInfo(case="用例D", message="访问过于频繁")
        diagnosis = classify_by_rules(failure)
        assert diagnosis is not None
        assert diagnosis.is_environment is True

    def test_assertion_failure_is_not_environment(self) -> None:
        failure = FailureInfo(
            case="用例E", error_type="AssertionError", message="[code] 期望等于 200，实际 500"
        )
        diagnosis = classify_by_rules(failure)
        assert diagnosis is not None
        assert diagnosis.is_environment is False

    def test_unmatched_returns_none(self) -> None:
        failure = FailureInfo(case="用例F", message="某个没见过的问题")
        assert classify_by_rules(failure) is None


class TestDiagnose:
    def test_rules_only(self) -> None:
        failures = [
            FailureInfo(case="A", error_type="ConnectionError", message="连接失败"),
            FailureInfo(case="B", error_type="AssertionError", message="断言失败"),
        ]
        results = diagnose(failures, use_ai=False)
        assert len(results) == 2
        assert results[0].is_environment is True
        assert results[1].is_environment is False

    def test_unclassified_gets_fallback(self) -> None:
        failures = [FailureInfo(case="X", message="完全没见过的问题")]
        results = diagnose(failures, use_ai=False)
        assert len(results) == 1
        assert results[0].category == "未分类"

    def test_ai_used_when_rules_miss(self) -> None:
        class FakeClient:
            available = True

            def chat_json(self, *args: object, **kw: object) -> dict:
                return {
                    "category": "服务端 bug",
                    "reason": "接口返回了未预期的数据",
                    "suggestion": "找开发确认",
                    "is_environment": False,
                }

        failures = [FailureInfo(case="X", message="完全没见过的问题")]
        results = diagnose(failures, use_ai=True, ai_client=FakeClient())
        assert results[0].category == "服务端 bug"
        assert results[0].source == "AI"


class TestReportRendering:
    def test_renders_summary_table(self) -> None:
        diagnoses = [
            Diagnosis(case="A", category="网络连接异常", is_environment=True),
            Diagnosis(case="B", category="断言不匹配", is_environment=False),
        ]
        text = render_markdown(diagnoses, total=10, passed=8)
        assert "# 测试失败根因分析" in text
        assert "网络连接异常" in text
        assert "疑似环境问题: 1" in text
        assert "疑似真问题: 1" in text

    def test_empty_report(self) -> None:
        text = render_markdown([], total=5, passed=5)
        assert "全绿" in text
