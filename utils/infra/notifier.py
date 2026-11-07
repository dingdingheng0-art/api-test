"""结果通知：把测试结果推送到钉钉 / 企业微信 / 飞书。

**要解决什么问题**

CI 跑完了，如果人不主动去看流水线，就不知道测试挂没挂。
企业里通常的做法是「测试结果发到群里」，让相关的人第一时间知道。

**三种机器人怎么发**

三个平台的群机器人接口长得不一样，但思路一样：**POST 一个 JSON 到
webhook 地址**。区别在于消息格式：

===========  ==================================  =========================
平台          地址长什么样                          消息格式
===========  ==================================  =========================
钉钉          ``https://oapi.dingtalk.com/robot/send?access_token=xxx``   ``{"msgtype": "markdown", "markdown": {...}}``
企业微信        ``https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx``  ``{"msgtype": "markdown", "markdown": {...}}``
飞书          ``https://open.feishu.cn/open-apis/bot/v2/hook/xxx``       ``{"msg_type": "interactive", "card": {...}}``
===========  ==================================  =========================

**加签名的情况**

三个平台都支持「加签」防止别人伪造请求：
请求里带时间戳和签名，平台会验。验不过就拒收。

钉钉用 ``secret``，算法是 ``HMAC-SHA256(时间戳 + "\n" + secret, secret)``
再 Base64。企业微信和飞书算法类似但细节不同。

**什么时候发**

不是每次都发 —— 全绿的时候没人关心。用 ``is_send`` 控制：

- ``not_send`` —— 从不发
- ``on_fail`` —— **只在失败时发**（最常用）
- ``always`` —— 每次都发

**怎么用**

.. code-block:: python

    from utils.infra.notifier import notify_result

    notify_result(
        webhook_type="ding_ding",
        addr="https://oapi.dingtalk.com/robot/send?access_token=xxx",
        secret="",
        results={"passed": 10, "failed": 2, "total": 12},
        env="test",
    )
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

from utils.logs.log_manager import log

# ---- 支持的通知渠道 ----

#: 钉钉
DING_DING = "ding_ding"
#: 企业微信
WE_CHAT = "we_chat"
#: 飞书
FEISHU = "feishu"

#: 所有支持的渠道
WEBHOOK_TYPES = (DING_DING, WE_CHAT, FEISHU)

# 渠道别名（用户可能写这些）
_WEBHOOK_ALIASES = {
    "dingtalk": DING_DING,
    "ding": DING_DING,
    "钉钉": DING_DING,
    "wecom": WE_CHAT,
    "wechat": WE_CHAT,
    "weixin": WE_CHAT,
    "qywx": WE_CHAT,
    "企业微信": WE_CHAT,
    "lark": FEISHU,
    "feishu": FEISHU,
    "飞书": FEISHU,
}

# ---- 什么时候发 ----

#: 从不发
NOT_SEND = "not_send"
#: 只在失败时发
ON_FAIL = "on_fail"
#: 每次都发
ALWAYS = "always"

SEND_POLICIES = (NOT_SEND, ON_FAIL, ALWAYS)


def normalize_webhook_type(webhook_type: str) -> str:
    """把渠道名归一化。"""
    text = str(webhook_type or "").strip().lower()
    if text in WEBHOOK_TYPES:
        return text
    return _WEBHOOK_ALIASES.get(text, text)


def normalize_send_policy(policy: str) -> str:
    """把发送策略归一化。"""
    text = str(policy or "").strip().lower()
    if text in SEND_POLICIES:
        return text
    aliases = {
        "never": NOT_SEND,
        "none": NOT_SEND,
        "不发送": NOT_SEND,
        "fail": ON_FAIL,
        "failed": ON_FAIL,
        "on_failure": ON_FAIL,
        "失败时": ON_FAIL,
        "all": ALWAYS,
        "always": ALWAYS,
        "每次": ALWAYS,
    }
    return aliases.get(text, ON_FAIL)


class NotifyError(RuntimeError):
    """通知发送失败。"""


@dataclass
class NotifyResult:
    """一次发送的结果。"""

    ok: bool
    channel: str
    message: str = ""
    response: Any = None
    skipped: bool = False

    def __bool__(self) -> bool:
        return self.ok


@dataclass
class SummaryData:
    """要通知的测试结果摘要。"""

    passed: int = 0
    failed: int = 0
    error: int = 0
    skipped: int = 0
    total: int = 0
    env: str = ""
    conclusion: str = ""
    duration: float = 0.0
    project: str = ""
    title: str = "接口自动化测试"
    report_url: str = ""
    #: 失败用例的明细（用例名 + 原因）
    failures: list[dict[str, Any]] = field(default_factory=list)

    @property
    def is_passed(self) -> bool:
        """是不是全过了。"""
        return self.failed == 0 and self.error == 0

    @classmethod
    def from_results(
        cls,
        results: dict[str, Any],
        failures: list[dict[str, Any]] | None = None,
    ) -> "SummaryData":
        """从 pytest 的结果字典构造。"""
        return cls(
            passed=int(results.get("passed") or 0),
            failed=int(results.get("failed") or 0),
            error=int(results.get("error") or 0),
            skipped=int(results.get("skipped") or 0),
            total=int(results.get("total") or 0),
            env=str(results.get("env") or ""),
            conclusion=str(results.get("conclusion") or ""),
            duration=float(results.get("duration") or 0),
            failures=failures or [],
        )

    def one_line(self) -> str:
        """一句话摘要。"""
        parts = [f"通过 {self.passed}"]
        if self.failed:
            parts.append(f"失败 {self.failed}")
        if self.error:
            parts.append(f"错误 {self.error}")
        if self.skipped:
            parts.append(f"跳过 {self.skipped}")
        return f"{' / '.join(parts)}（共 {self.total} 条）"


# ---------------------------------------------------------------------------
# 签名
# ---------------------------------------------------------------------------


def sign_ding_ding(secret: str, timestamp: int | None = None) -> tuple[int, str]:
    """钉钉加签。

    Returns:
        ``(时间戳, 签名)``
    """
    ts = timestamp if timestamp is not None else int(time.time() * 1000)
    text = f"{ts}\n{secret}"
    digest = hmac.new(
        secret.encode("utf-8"), text.encode("utf-8"), hashlib.sha256
    ).digest()
    sign = base64.b64encode(digest).decode("utf-8")
    return ts, sign


def sign_we_chat(secret: str) -> tuple[int, str]:
    """企业微信加签。

    跟钉钉算法一样，但签名要 URL 编码。
    """
    ts, sign = sign_ding_ding(secret)
    return ts, urllib.parse.quote_plus(sign)


def sign_feishu(secret: str, timestamp: int | None = None) -> tuple[int, str]:
    """飞书加签。

    飞书用空串做密钥（HMAC key 为空），这是官方文档的写法。
    """
    ts = timestamp if timestamp is not None else int(time.time())
    text = f"{ts}\n{secret}"
    digest = hmac.new(
        b"", text.encode("utf-8"), hashlib.sha256
    ).digest()
    sign = base64.b64encode(digest).decode("utf-8")
    return ts, sign


# ---------------------------------------------------------------------------
# 消息体
# ---------------------------------------------------------------------------


def build_ding_ding_message(summary: SummaryData) -> dict[str, Any]:
    """钉钉的 markdown 消息。"""
    title = _title(summary)
    lines = [f"### {title}", "", summary.one_line()]

    if summary.env:
        lines.append(f"- **环境**：{summary.env}")
    if summary.project:
        lines.append(f"- **项目**：{summary.project}")
    if summary.duration:
        lines.append(f"- **耗时**：{summary.duration:.1f} 秒")
    if summary.report_url:
        lines.append(f"- **报告**：{summary.report_url}")

    if summary.failures:
        lines.append("")
        lines.append(f"**失败用例（{len(summary.failures)} 条）**")
        for item in summary.failures[:10]:
            name = str(item.get("name") or item.get("case") or "未知用例")
            reason = str(item.get("reason") or item.get("error") or "")
            lines.append(f"- {name}" + (f"：{reason}" if reason else ""))

    return {
        "msgtype": "markdown",
        "markdown": {"title": title, "text": "\n".join(lines)},
        "at": {"isAtAll": summary.is_passed is False and summary.failed + summary.error > 0},
    }


def build_we_chat_message(summary: SummaryData) -> dict[str, Any]:
    """企业微信的 markdown 消息。"""
    title = _title(summary)
    lines = [f"**{title}**", "", summary.one_line()]

    if summary.env:
        lines.append(f"> 环境：{summary.env}")
    if summary.project:
        lines.append(f"> 项目：{summary.project}")
    if summary.duration:
        lines.append(f"> 耗时：{summary.duration:.1f} 秒")
    if summary.report_url:
        lines.append(f"> 报告：{summary.report_url}")

    if summary.failures:
        lines.append("")
        lines.append(f"**失败用例（{len(summary.failures)} 条）**")
        for item in summary.failures[:10]:
            name = str(item.get("name") or item.get("case") or "未知用例")
            reason = str(item.get("reason") or item.get("error") or "")
            lines.append(f"- {name}" + (f"：{reason}" if reason else ""))

    return {
        "msgtype": "markdown",
        "markdown": {"content": "\n".join(lines)},
    }


def build_feishu_message(summary: SummaryData) -> dict[str, Any]:
    """飞书的卡片消息。"""
    title = _title(summary)
    color = "green" if summary.is_passed else "red"

    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "text": {"tag": "lark_md", "content": summary.one_line()},
        }
    ]

    fields: list[str] = []
    if summary.env:
        fields.append(f"**环境**\n{summary.env}")
    if summary.project:
        fields.append(f"**项目**\n{summary.project}")
    if summary.duration:
        fields.append(f"**耗时**\n{summary.duration:.1f} 秒")
    if summary.report_url:
        fields.append(f"**报告**\n{summary.report_url}")

    if fields:
        elements.append(
            {"tag": "div", "fields": [{"tag": "lark_md", "content": f} for f in fields]}
        )

    if summary.failures:
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**失败用例（{len(summary.failures)} 条）**",
                },
            }
        )
        for item in summary.failures[:10]:
            name = str(item.get("name") or item.get("case") or "未知用例")
            reason = str(item.get("reason") or item.get("error") or "")
            elements.append(
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"- {name}" + (f"：{reason}" if reason else ""),
                    },
                }
            )

    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": color,
            },
            "elements": elements,
        },
    }


def _title(summary: SummaryData) -> str:
    """通知标题。"""
    state = "通过" if summary.is_passed else "失败"
    if summary.project:
        return f"{summary.project} 接口自动化测试 {state}"
    return f"{summary.title} {state}"


# ---------------------------------------------------------------------------
# 发送
# ---------------------------------------------------------------------------


def _post_json(url: str, payload: dict[str, Any], timeout: float = 10.0) -> Any:
    """POST 一个 JSON，返回解析后的响应。"""
    import requests

    response = requests.post(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        timeout=timeout,
    )
    try:
        return response.json()
    except ValueError:
        return {"raw": response.text, "status_code": response.status_code}


def send_webhook(
    webhook_type: str,
    addr: str,
    secret: str = "",
    summary: SummaryData | None = None,
    timeout: float = 10.0,
) -> NotifyResult:
    """往一个 webhook 地址发消息。

    Args:
        webhook_type: 渠道（ding_ding / we_chat / feishu）
        addr: webhook 地址
        secret: 加签密钥（没配就空）
        summary: 测试结果摘要
        timeout: 超时秒数

    Returns:
        :class:`NotifyResult` —— ``ok`` 表示发成功
    """
    channel = normalize_webhook_type(webhook_type)

    if not addr:
        return NotifyResult(
            ok=False, channel=channel, message="webhook 地址为空", skipped=True
        )

    data = summary or SummaryData()

    try:
        if channel == DING_DING:
            payload = build_ding_ding_message(data)
            url = addr
            if secret:
                ts, sign = sign_ding_ding(secret)
                separator = "&" if "?" in addr else "?"
                url = f"{addr}{separator}timestamp={ts}&sign={urllib.parse.quote_plus(sign)}"

        elif channel == WE_CHAT:
            payload = build_we_chat_message(data)
            url = addr
            if secret:
                ts, sign = sign_we_chat(secret)
                separator = "&" if "?" in addr else "?"
                url = f"{addr}{separator}timestamp={ts}&sign={sign}"

        elif channel == FEISHU:
            payload = build_feishu_message(data)
            url = addr
            if secret:
                ts, sign = sign_feishu(secret)
                separator = "&" if "?" in addr else "?"
                url = f"{addr}{separator}timestamp={ts}&sign={sign}"

        else:
            return NotifyResult(
                ok=False,
                channel=channel,
                message=f"不支持的通知渠道: {webhook_type}（可用：{', '.join(WEBHOOK_TYPES)}）",
                skipped=True,
            )

        response = _post_json(url, payload, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - 通知失败不该让测试流程崩掉
        log.warning("发送%s通知失败: %s", channel, exc)
        return NotifyResult(ok=False, channel=channel, message=str(exc))

    # 三个平台的成功标识不一样
    ok = _is_success(channel, response)

    if not ok:
        log.warning("%s 返回异常: %s", channel, response)
    else:
        log.info("已发送%s通知", channel)

    return NotifyResult(
        ok=ok,
        channel=channel,
        message="" if ok else _extract_error(response),
        response=response,
    )


def _is_success(channel: str, response: Any) -> bool:
    """判断平台返回的是不是成功。"""
    if not isinstance(response, dict):
        return False

    if channel == DING_DING:
        # {"errcode": 0, "errmsg": "ok"}
        return response.get("errcode") in (0, "0")

    if channel == WE_CHAT:
        # {"errcode": 0, "errmsg": "ok"}
        return response.get("errcode") in (0, "0")

    if channel == FEISHU:
        # 飞书新版返回 {"code": 0, "msg": "success"}，旧版返回 {"StatusCode": 0}
        if "code" in response:
            return response.get("code") in (0, "0")
        if "StatusCode" in response:
            return response.get("StatusCode") in (0, "0")
        # 有的版本成功时只返回 {"msg": "success"}
        return str(response.get("msg", "")).lower() == "success"

    return False


def _extract_error(response: Any) -> str:
    """从平台响应里抽出错误信息。"""
    if not isinstance(response, dict):
        return str(response)

    for key in ("errmsg", "msg", "message", "error"):
        value = response.get(key)
        if value:
            return f"{key}: {value}"

    return str(response)


def should_notify(policy: str, is_passed: bool) -> bool:
    """按策略判断这次要不要发。

    Args:
        policy: ``not_send`` / ``on_fail`` / ``always``
        is_passed: 这次测试是不是全过了
    """
    normalized = normalize_send_policy(policy)
    if normalized == NOT_SEND:
        return False
    if normalized == ALWAYS:
        return True
    return not is_passed


def notify_result(
    webhook_type: str,
    addr: str,
    secret: str = "",
    results: dict[str, Any] | None = None,
    failures: list[dict[str, Any]] | None = None,
    env: str = "",
    project: str = "",
    policy: str = ON_FAIL,
    report_url: str = "",
    title: str = "接口自动化测试",
    timeout: float = 10.0,
) -> NotifyResult:
    """发一条测试结果通知。

    这是给外部调用的主入口 —— CI、调度器、定时任务都用它。
    """
    summary = SummaryData.from_results(results or {}, failures)
    if env:
        summary.env = env
    if project:
        summary.project = project
    summary.report_url = report_url
    summary.title = title

    if not should_notify(policy, summary.is_passed):
        log.info(
            "按策略「%s」不发送通知（本次结果：%s）",
            normalize_send_policy(policy),
            "通过" if summary.is_passed else "失败",
        )
        return NotifyResult(
            ok=True, channel=normalize_webhook_type(webhook_type), skipped=True
        )

    return send_webhook(
        webhook_type=webhook_type,
        addr=addr,
        secret=secret,
        summary=summary,
        timeout=timeout,
    )


__all__ = [
    "ALWAYS",
    "DING_DING",
    "FEISHU",
    "NOT_SEND",
    "ON_FAIL",
    "NotifyError",
    "NotifyResult",
    "SEND_POLICIES",
    "SummaryData",
    "WEBHOOK_TYPES",
    "WE_CHAT",
    "build_ding_ding_message",
    "build_feishu_message",
    "build_we_chat_message",
    "normalize_send_policy",
    "normalize_webhook_type",
    "notify_result",
    "send_webhook",
    "should_notify",
    "sign_ding_ding",
    "sign_feishu",
    "sign_we_chat",
]
