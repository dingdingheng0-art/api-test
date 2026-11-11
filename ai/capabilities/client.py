"""LLM 客户端 —— 走 OpenAI 兼容协议。

**为什么用 OpenAI 兼容协议**：现在几乎所有模型服务都支持这套格式
（OpenAI、DeepSeek、通义千问、智谱、Ollama 本地模型……）。
只要给一个 ``base_url`` + ``api_key`` 就能接上，不用为每家写适配。

**用标准库实现，不装第三方包** —— 就一个 POST 请求而已，
少一个依赖少一份麻烦。

配置（``config.yaml``）::

    ai:
      base_url: "https://api.deepseek.com/v1"
      api_key: "${AI_API_KEY}"
      model: "deepseek-chat"
      timeout: 60
      temperature: 0.3

或者用环境变量（不写进配置文件更安全）::

    $env:AI_API_KEY = "sk-xxxx"
    $env:AI_BASE_URL = "https://api.deepseek.com/v1"
    $env:AI_MODEL = "deepseek-chat"

**没配置的时候**：调用方会收到 ``LLMNotConfigured``，
由 ``case_gen`` / ``diagnosis`` 决定要不要退回本地模式。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from utils.logs.log_manager import log


class LLMError(RuntimeError):
    """调用 LLM 失败。"""


class LLMNotConfigured(LLMError):
    """没配 API key / base_url —— 调用方应该退回本地模式。"""


def _as_text(value: Any) -> str:
    """配置值转字符串。

    yaml 会把 ``base_url: 8080`` 解析成 int、把 ``api_key: 12345`` 解析成数字；
    直接 ``.rstrip()`` / ``.strip()`` 会 AttributeError，把「配置没填好」
    变成启动即崩。
    """
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_headers(value: Any) -> dict[str, str]:
    """把 headers 配置归一成 ``{str: str}``；认不出就当没配。"""
    if isinstance(value, dict):
        items: Any = value.items()
    elif isinstance(value, (list, tuple)):
        items = value
    else:
        return {}
    try:
        return {str(key): str(item) for key, item in items}
    except (TypeError, ValueError):
        return {}


@dataclass
class LLMConfig:
    """LLM 配置。"""

    base_url: str = ""
    api_key: str = ""
    model: str = ""
    timeout: int = 60
    temperature: float = 0.3
    max_tokens: int = 4000
    extra_headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_config(cls, raw: dict[str, Any] | None = None) -> "LLMConfig":
        """从配置里读，配置里没有就找环境变量。

        配置是**人写的 yaml**，类型不可假定（``timeout: "60s"``、
        ``headers`` 写成列表、整段写成字符串都见过）。这里认不出的值退回
        默认值，绝不因为一段配置写错让整个程序起不来。
        """
        raw = raw if isinstance(raw, dict) else {}

        base_url = _as_text(
            raw.get("base_url")
            or os.environ.get("AI_BASE_URL")
            or os.environ.get("OPENAI_BASE_URL")
        ).rstrip("/")

        api_key = _as_text(
            raw.get("api_key")
            or os.environ.get("AI_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or os.environ.get("DASHSCOPE_API_KEY")
        )

        model = _as_text(
            raw.get("model")
            or os.environ.get("AI_MODEL")
            or os.environ.get("OPENAI_MODEL")
        )

        return cls(
            base_url=base_url,
            api_key=api_key,
            model=model,
            # 0 和「没配」保留原来的区分：0 视为没填，回落默认值。
            timeout=_as_int(raw.get("timeout"), 60) or 60,
            temperature=_as_float(raw.get("temperature"), 0.3),
            max_tokens=_as_int(raw.get("max_tokens"), 4000) or 4000,
            extra_headers=_as_headers(raw.get("headers")),
        )

    @property
    def configured(self) -> bool:
        """配置齐了没（base_url + api_key + model）。"""
        return bool(self.base_url and self.api_key and self.model)


class LLMClient:
    """LLM 客户端。"""

    def __init__(self, config: LLMConfig | None = None):
        self.config = config or LLMConfig.from_config()

    # ---------------- 对外 ----------------

    @property
    def available(self) -> bool:
        """能不能用（配置齐了没）。"""
        return self.config.configured

    def chat(
        self,
        prompt: str,
        system: str = "",
        json_output: bool = False,
        temperature: float | None = None,
    ) -> str:
        """发一轮对话，返回模型的文字回复。

        Args:
            prompt: 用户提示
            system: 系统提示（给 AI 定角色和输出要求）
            json_output: 要不要要求模型返回 JSON
            temperature: 覆盖配置里的温度

        Raises:
            LLMNotConfigured: 没配置
            LLMError: 调用失败（网络错误、返回格式不对）
        """
        if not self.available:
            raise LLMNotConfigured(
                "AI 没配置。请在 config.yaml 的 ai 段里配 base_url / api_key / model，"
                "或者设环境变量 AI_BASE_URL / AI_API_KEY / AI_MODEL。"
            )

        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature if temperature is None else temperature,
            "max_tokens": self.config.max_tokens,
        }
        if json_output:
            payload["response_format"] = {"type": "json_object"}

        raw = self._post("/chat/completions", payload)
        return self._extract_text(raw)

    def chat_json(
        self,
        prompt: str,
        system: str = "",
        temperature: float | None = None,
    ) -> Any:
        """要模型返回 JSON，直接解析成 Python 对象。

        模型经常在 JSON 外面裹一层 markdown 代码块或者前后说点废话，
        这里都处理掉。
        """
        text = self.chat(prompt, system=system, json_output=True, temperature=temperature)
        return parse_json_response(text)

    def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        temperature: float | None = None,
    ) -> dict[str, Any]:
        """带工具定义发一轮，返回 assistant 消息 + 用量。

        **为什么单独开一个方法而不是改 ``chat``**

        ``chat`` 的调用方（``case_gen`` / ``diagnosis``）要的是纯文本，
        返回值一旦变成 dict，所有调用方都得跟着改。这里只做加法。

        Args:
            messages: 完整对话历史（含之前轮次的 assistant / tool 消息）
            tools: OpenAI 兼容的工具定义（``{"type":"function","function":{...}}``）

        Returns:
            ``{"message": {...含 tool_calls...}, "usage": {...}, "finish_reason": "..."}``
        """
        if not self.available:
            raise LLMNotConfigured(
                "AI 没配置。请在 config.yaml 的 ai 段里配 base_url / api_key / model，"
                "或者设环境变量 AI_BASE_URL / AI_API_KEY / AI_MODEL。"
            )

        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature if temperature is None else temperature,
            "max_tokens": self.config.max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        raw = self._post("/chat/completions", payload)
        choices = raw.get("choices")
        if not isinstance(choices, (list, tuple)) or not choices:
            raise LLMError(f"LLM 响应里没有 choices: {str(raw)[:300]}")
        choice = choices[0]
        if not isinstance(choice, dict):
            # 合法 json 但结构不对（有的网关 error 时也回 200）
            raise LLMError(f"LLM 响应 choices[0] 不是对象: {str(raw)[:300]}")

        message = choice.get("message")
        if isinstance(message, str):
            message = {"content": message}
        elif not isinstance(message, dict):
            message = {}
        usage = raw.get("usage")
        if not isinstance(usage, dict):
            usage = {}
        return {
            "message": message,
            "usage": usage,
            "finish_reason": str(choice.get("finish_reason") or ""),
        }

    # ---------------- 内部 ----------------

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """发 POST 请求，返回解析后的 JSON。"""
        url = f"{self.config.base_url}{path}"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        headers.update(self.config.extra_headers)

        request = urllib.request.Request(url, data=body, headers=headers, method="POST")

        log.info("调用 LLM: %s (%s)", self.config.model, url)
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:500]
            except Exception:  # noqa: BLE001
                pass
            raise LLMError(f"LLM 调用失败（HTTP {exc.code}）: {detail}") from exc
        except urllib.error.URLError as exc:
            raise LLMError(f"连不上 LLM 服务: {exc}") from exc
        except TimeoutError as exc:
            raise LLMError(f"LLM 调用超时（{self.config.timeout}s）") from exc

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LLMError(f"LLM 返回的不是 JSON: {raw[:300]!r}") from exc
        if not isinstance(data, dict):
            # 合法 JSON 但不是对象：后面 `data["choices"]` 会抛 TypeError 而不是 LLMError。
            raise LLMError(f"LLM 返回的 JSON 不是对象: {raw[:300]!r}")

        _record_usage(data, self.config.model)
        return data

    @staticmethod
    def _extract_text(raw: dict[str, Any]) -> str:
        """从响应里取出文字内容。"""
        choices = raw.get("choices")
        if not isinstance(choices, (list, tuple)) or not choices:
            raise LLMError(f"LLM 响应里没有 choices: {str(raw)[:300]}")
        choice = choices[0]
        if not isinstance(choice, dict):
            raise LLMError(f"LLM 响应 choices[0] 不是对象: {str(raw)[:300]}")

        message = choice.get("message")
        if isinstance(message, str):
            message = {"content": message}
        elif not isinstance(message, dict):
            message = {}
        content = message.get("content")

        # 有些服务返回的是数组形式的内容块
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict):
                    parts.append(str(block.get("text") or ""))
            return "".join(parts)

        return str(content or "")


def _record_usage(raw: dict[str, Any], model: str) -> None:
    """把 token 用量记进审计日志（记不了就算了，绝不抛）。"""
    usage = raw.get("usage") or {}
    if not usage:
        return
    try:
        from ai.agent import audit_log

        audit_log.record_llm(
            model=model,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
        )
    except Exception:  # noqa: BLE001 - 审计是旁路
        pass


def parse_json_response(text: str) -> Any:
    """从模型回复里抠出 JSON。

    处理三种常见情况：

    1. 直接就是 JSON
    2. 包在 ```json ... ``` 代码块里
    3. 前后有废话，中间夹着 JSON
    """
    # 调用方可能把非字符串传进来（替身客户端 / 网关返回了对象）。
    # 这里只是解析入口，类型不对按「空内容」处理，别抛 AttributeError。
    text = text if isinstance(text, str) else ("" if text is None else str(text))
    text = text.strip()

    if not text:
        raise LLMError("模型返回了空内容")

    # 情况 2：代码块
    if text.startswith("```"):
        stripped = text.strip("`")
        # 去掉 ```json / ```python 这种语言标记
        if stripped.lower().startswith("json"):
            stripped = stripped[4:]
        elif "\n" in stripped:
            first_line, rest = stripped.split("\n", 1)
            if first_line.strip().lower() in ("json", "python", "yaml", ""):
                stripped = rest
        text = stripped.strip()

    # 情况 1
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 情况 3：找第一段完整的 {...} 或 [...]
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        if start == -1:
            continue
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == opener:
                depth += 1
            elif char == closer:
                depth -= 1
                if depth == 0:
                    candidate = text[start : index + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        break

    raise LLMError(f"模型回复里找不到合法 JSON: {text[:300]!r}")


def get_client(config: dict[str, Any] | None = None) -> LLMClient:
    """造一个客户端（配置优先读 ``config.yaml``）。"""
    if config is None:
        try:
            from utils.config.config_manager import ConfigManager

            config = ConfigManager.get_all().get("ai") or {}
        except Exception:  # noqa: BLE001
            config = {}

    return LLMClient(LLMConfig.from_config(config))
