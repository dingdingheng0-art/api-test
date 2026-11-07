"""结果通知的单元测试。

**不真的发消息** —— 用假响应喂给发送函数，测的是
「消息体对不对」「策略判断准不准」「失败会不会崩」这几件事。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.infra.notifier import (
    ALWAYS,
    DING_DING,
    FEISHU,
    NOT_SEND,
    ON_FAIL,
    WE_CHAT,
    NotifyResult,
    SummaryData,
    build_ding_ding_message,
    build_feishu_message,
    build_we_chat_message,
    normalize_send_policy,
    normalize_webhook_type,
    send_webhook,
    should_notify,
    sign_ding_ding,
    sign_feishu,
    sign_we_chat,
)


# ---------------------------------------------------------------------------
# 渠道归一化
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ding_ding", DING_DING),
        ("dingtalk", DING_DING),
        ("钉钉", DING_DING),
        ("we_chat", WE_CHAT),
        ("wecom", WE_CHAT),
        ("企业微信", WE_CHAT),
        ("feishu", FEISHU),
        ("lark", FEISHU),
        ("飞书", FEISHU),
    ],
)
def test_normalize_webhook_type(raw: str, expected: str) -> None:
    """渠道名各种写法都能认出来。"""
    assert normalize_webhook_type(raw) == expected


def test_normalize_webhook_type_unknown() -> None:
    """不认识的渠道原样返回（后面会报「不支持」）。"""
    assert normalize_webhook_type("slack") == "slack"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("not_send", NOT_SEND),
        ("on_fail", ON_FAIL),
        ("always", ALWAYS),
        ("never", NOT_SEND),
        ("failed", ON_FAIL),
        ("all", ALWAYS),
        ("失败时", ON_FAIL),
    ],
)
def test_normalize_send_policy(raw: str, expected: str) -> None:
    """发送策略的各种写法都能认出来。"""
    assert normalize_send_policy(raw) == expected


# ---------------------------------------------------------------------------
# 发送策略
# ---------------------------------------------------------------------------


def test_should_notify_never() -> None:
    """不发送策略下永远不发。"""
    assert should_notify(NOT_SEND, is_passed=False) is False
    assert should_notify(NOT_SEND, is_passed=True) is False


def test_should_notify_on_fail_only_when_failed() -> None:
    """只在失败时发：过了不发，挂了才发。"""
    assert should_notify(ON_FAIL, is_passed=True) is False
    assert should_notify(ON_FAIL, is_passed=False) is True


def test_should_notify_always() -> None:
    """每次都发。"""
    assert should_notify(ALWAYS, is_passed=True) is True
    assert should_notify(ALWAYS, is_passed=False) is True


# ---------------------------------------------------------------------------
# 签名
# ---------------------------------------------------------------------------


def test_sign_ding_ding_is_deterministic() -> None:
    """同一个密钥和时间戳，签名必须一样。"""
    ts1, sign1 = sign_ding_ding("secret", timestamp=1700000000000)
    ts2, sign2 = sign_ding_ding("secret", timestamp=1700000000000)

    assert ts1 == ts2 == 1700000000000
    assert sign1 == sign2
    assert sign1  # 不该是空串


def test_sign_ding_ding_differs_by_timestamp() -> None:
    """时间戳变了签名要变。"""
    _, sign1 = sign_ding_ding("secret", timestamp=1700000000000)
    _, sign2 = sign_ding_ding("secret", timestamp=1700000001000)

    assert sign1 != sign2


def test_sign_we_chat_is_url_encoded() -> None:
    """企业微信的签名要 URL 编码（可能有 + / = 这些字符）。"""
    _, sign = sign_we_chat("secret")

    assert " " not in sign
    assert "+" not in sign or "%2B" in sign


def test_sign_feishu_uses_empty_key() -> None:
    """飞书用空串做 HMAC key（官方文档的写法）。"""
    ts, sign = sign_feishu("secret", timestamp=1700000000)

    assert ts == 1700000000
    assert sign


# ---------------------------------------------------------------------------
# 消息体
# ---------------------------------------------------------------------------


def _summary(**kwargs: Any) -> SummaryData:
    return SummaryData(
        passed=kwargs.get("passed", 8),
        failed=kwargs.get("failed", 2),
        error=kwargs.get("error", 0),
        skipped=kwargs.get("skipped", 1),
        total=kwargs.get("total", 11),
        env=kwargs.get("env", "test"),
        project=kwargs.get("project", "mall4j"),
        duration=kwargs.get("duration", 12.5),
        report_url=kwargs.get("report_url", "http://ci/report"),
        failures=kwargs.get(
            "failures", [{"name": "查询订单", "reason": "断言失败：code 不等于 200"}]
        ),
    )


def test_ding_ding_message_structure() -> None:
    """钉钉消息体格式对。"""
    payload = build_ding_ding_message(_summary())

    assert payload["msgtype"] == "markdown"
    assert "title" in payload["markdown"]
    text = payload["markdown"]["text"]
    assert "mall4j" in text
    assert "通过 8" in text
    assert "失败 2" in text


def test_ding_ding_at_all_on_failure() -> None:
    """有失败时 @ 所有人。"""
    payload = build_ding_ding_message(_summary(failed=2))
    assert payload["at"]["isAtAll"] is True

    payload = build_ding_ding_message(_summary(failed=0, total=8))
    assert payload["at"]["isAtAll"] is False


def test_we_chat_message_structure() -> None:
    """企业微信消息体格式对。"""
    payload = build_we_chat_message(_summary())

    assert payload["msgtype"] == "markdown"
    content = payload["markdown"]["content"]
    assert "mall4j" in content
    assert "失败用例" in content


def test_feishu_message_structure() -> None:
    """飞书消息体是卡片格式。"""
    payload = build_feishu_message(_summary())

    assert payload["msg_type"] == "interactive"
    card = payload["card"]
    assert card["header"]["template"] == "red"
    assert "mall4j" in card["header"]["title"]["content"]


def test_feishu_card_is_green_when_passed() -> None:
    """全过时卡片是绿色的。"""
    payload = build_feishu_message(_summary(failed=0, error=0, total=8))

    assert payload["card"]["header"]["template"] == "green"


def test_message_lists_failures() -> None:
    """失败用例明细要列出来。"""
    summary = _summary(
        failures=[
            {"name": "用例A", "reason": "超时"},
            {"name": "用例B", "reason": "返回码错误"},
        ]
    )

    text = build_ding_ding_message(summary)["markdown"]["text"]
    assert "用例A" in text
    assert "超时" in text
    assert "用例B" in text


def test_failures_truncated_to_ten() -> None:
    """失败太多只列前 10 条（不然消息太长）。"""
    summary = _summary(
        failures=[{"name": f"用例{i}", "reason": "x"} for i in range(30)]
    )

    text = build_ding_ding_message(summary)["markdown"]["text"]
    assert "用例0" in text
    assert "用例29" not in text


# ---------------------------------------------------------------------------
# 摘要
# ---------------------------------------------------------------------------


def test_summary_from_results() -> None:
    """能从 pytest 的结果字典构造摘要。"""
    summary = SummaryData.from_results(
        {
            "passed": 5,
            "failed": 1,
            "error": 0,
            "skipped": 2,
            "total": 8,
            "env": "pre",
            "duration": 3.5,
        }
    )

    assert summary.passed == 5
    assert summary.failed == 1
    assert summary.total == 8
    assert summary.env == "pre"
    assert summary.is_passed is False


def test_summary_is_passed() -> None:
    """全过才算通过。"""
    assert SummaryData(passed=3, total=3).is_passed is True
    assert SummaryData(passed=2, failed=1, total=3).is_passed is False
    assert SummaryData(passed=2, error=1, total=3).is_passed is False


def test_summary_one_line() -> None:
    """一句话摘要。"""
    assert SummaryData(passed=3, total=3).one_line() == "通过 3（共 3 条）"
    assert (
        SummaryData(passed=1, failed=2, total=3).one_line()
        == "通过 1 / 失败 2（共 3 条）"
    )


# ---------------------------------------------------------------------------
# 发送
# ---------------------------------------------------------------------------


def test_send_webhook_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """平台返回成功标识时算发成功。"""
    captured: dict[str, Any] = {}

    def _fake_post(url: str, payload: dict[str, Any], timeout: float = 10.0) -> Any:
        captured["url"] = url
        captured["payload"] = payload
        return {"errcode": 0, "errmsg": "ok"}

    monkeypatch.setattr("utils.infra.notifier._post_json", _fake_post)

    result = send_webhook(DING_DING, "https://example.invalid/robot", summary=_summary())

    assert result.ok is True
    assert result.skipped is False
    assert captured["payload"]["msgtype"] == "markdown"


def test_send_webhook_appends_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    """配了密钥就要加签。"""
    captured: dict[str, Any] = {}

    def _fake_post(url: str, payload: dict[str, Any], timeout: float = 10.0) -> Any:
        captured["url"] = url
        return {"errcode": 0}

    monkeypatch.setattr("utils.infra.notifier._post_json", _fake_post)

    send_webhook(
        DING_DING, "https://example.invalid/robot", secret="s3cret", summary=_summary()
    )

    assert "timestamp=" in captured["url"]
    assert "sign=" in captured["url"]


def test_send_webhook_no_secret_no_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    """没配密钥就不加签。"""
    captured: dict[str, Any] = {}

    def _fake_post(url: str, payload: dict[str, Any], timeout: float = 10.0) -> Any:
        captured["url"] = url
        return {"errcode": 0}

    monkeypatch.setattr("utils.infra.notifier._post_json", _fake_post)

    send_webhook(DING_DING, "https://example.invalid/robot", summary=_summary())

    assert "sign=" not in captured["url"]


def test_send_webhook_failure_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    """平台返回错误码时要报出来。"""
    monkeypatch.setattr(
        "utils.infra.notifier._post_json",
        lambda *a, **kw: {"errcode": 310000, "errmsg": "关键词不匹配"},
    )

    result = send_webhook(DING_DING, "https://example.invalid/robot", summary=_summary())

    assert result.ok is False
    assert "关键词不匹配" in result.message


def test_send_webhook_empty_address_skipped() -> None:
    """地址为空就跳过，不报错。"""
    result = send_webhook(DING_DING, "", summary=_summary())

    assert result.ok is False
    assert result.skipped is True


def test_send_webhook_unsupported_channel() -> None:
    """不支持的渠道要报出来。"""
    result = send_webhook("slack", "https://example.invalid/hook", summary=_summary())

    assert result.ok is False
    assert "不支持" in result.message


def test_send_webhook_network_error_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """网络炸了不能把测试流程带崩。"""

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise ConnectionError("连不上")

    monkeypatch.setattr("utils.infra.notifier._post_json", _boom)

    result = send_webhook(DING_DING, "https://example.invalid/robot", summary=_summary())

    assert result.ok is False
    assert "连不上" in result.message


def test_feishu_success_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    """飞书的新旧两种返回格式都能识别。"""
    monkeypatch.setattr("utils.infra.notifier._post_json", lambda *a, **kw: {"code": 0, "msg": "success"})
    assert send_webhook(FEISHU, "https://example.invalid/hook", summary=_summary()).ok is True

    monkeypatch.setattr("utils.infra.notifier._post_json", lambda *a, **kw: {"StatusCode": 0, "StatusMessage": "success"})
    assert send_webhook(FEISHU, "https://example.invalid/hook", summary=_summary()).ok is True

    monkeypatch.setattr("utils.infra.notifier._post_json", lambda *a, **kw: {"code": 19001, "msg": "签名错误"})
    assert send_webhook(FEISHU, "https://example.invalid/hook", summary=_summary()).ok is False


def test_we_chat_success_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    """企业微信的成功标识。"""
    monkeypatch.setattr("utils.infra.notifier._post_json", lambda *a, **kw: {"errcode": 0, "errmsg": "ok"})
    assert send_webhook(WE_CHAT, "https://example.invalid/hook", summary=_summary()).ok is True
