"""HAR 解析器。

HAR 是浏览器/抓包工具导出的「完整请求记录」格式，结构固定：

.. code-block:: json

    {
      "log": {
        "entries": [
          {
            "request": {
              "method": "POST",
              "url": "https://api.xxx.com/order/create",
              "headers": [{"name": "accesstoken", "value": "xxx"}],
              "queryString": [{"name": "a", "value": "1"}],
              "postData": {"mimeType": "application/json", "text": "{...}"}
            },
            "response": {"status": 200, "content": {"text": "..."}}
          }
        ]
      }
    }

比 OpenAPI 更真实 —— 里面有真实的参数值，所以生成的用例能直接跑。
"""
from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qsl, urlparse

from tools.importer.models import Endpoint, Param, infer_module


def parse_har(doc: dict[str, Any], source: str = "") -> list[Endpoint]:
    """把 HAR 解析成接口列表。"""
    if not isinstance(doc, dict):
        return []

    log = doc.get("log")
    entries: list[Any] = []
    if isinstance(log, dict):
        entries = log.get("entries") or []
    elif isinstance(doc.get("entries"), list):
        entries = doc["entries"]

    endpoints: list[Endpoint] = []
    used_ids: set[str] = set()

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        request = entry.get("request") or {}
        if not isinstance(request, dict):
            continue

        url = str(request.get("url") or "").strip()
        if not url:
            continue

        method = str(request.get("method") or "GET").upper()
        parsed = urlparse(url)
        base_url = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme else ""
        path = parsed.path or "/"

        # ---- 请求头 ----
        headers: list[Param] = []
        for item in request.get("headers") or []:
            if not isinstance(item, dict):
                continue
            hname = str(item.get("name") or "").strip()
            if not hname:
                continue
            headers.append(
                Param(
                    name=hname,
                    location="header",
                    required=False,
                    type="string",
                    example=item.get("value"),
                )
            )

        # ---- Query 参数 ----
        query: list[Param] = []
        for item in request.get("queryString") or []:
            if not isinstance(item, dict):
                continue
            qname = str(item.get("name") or "").strip()
            if not qname:
                continue
            query.append(
                Param(name=qname, location="query", required=False,
                      type="string", example=item.get("value"))
            )
        # 有些工具不写 queryString，从 URL 里补
        if not query and parsed.query:
            for qname, qvalue in parse_qsl(parsed.query, keep_blank_values=True):
                query.append(Param(name=qname, location="query", example=qvalue))

        # ---- 请求体 ----
        body: Any = None
        body_type = ""
        post_data = request.get("postData")
        if isinstance(post_data, dict):
            mime = str(post_data.get("mimeType") or "")
            text = post_data.get("text")
            if text:
                body_type = "form" if "form-urlencoded" in mime else "json"
                try:
                    body = json.loads(text)
                except (json.JSONDecodeError, TypeError):
                    body = text
            elif post_data.get("params"):
                body_type = "form"
                body = {
                    str(p.get("name")): p.get("value")
                    for p in post_data["params"]
                    if isinstance(p, dict) and p.get("name")
                }

        # ---- 响应示例 ----
        response_example = None
        response_obj = entry.get("response") or {}
        if isinstance(response_obj, dict):
            content = response_obj.get("content") or {}
            if isinstance(content, dict):
                text = content.get("text")
                if text:
                    try:
                        response_example = json.loads(text)
                    except (json.JSONDecodeError, TypeError):
                        response_example = text

        # ---- 归类：按路径推断模块 ----
        tags = [infer_module(path)]

        name = _guess_name(request, method, path)

        endpoints.append(
            Endpoint(
                id=_unique_id(path, method, used_ids),
                method=method,
                path=path,
                name=name,
                summary=name,
                tags=tags,
                base_url=base_url,
                headers=headers,
                query=query,
                body=body,
                body_type=body_type,
                response_example=response_example,
                source=source,
            )
        )

    return endpoints


def _guess_name(request: dict[str, Any], method: str, path: str) -> str:
    """给接口起个中文/英文名字：优先抓包工具里的注释。"""
    for key in ("comment", "name"):
        value = request.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return f"{method} {path}"


def _unique_id(path: str, method: str, used: set[str]) -> str:
    import re

    parts = [p for p in re.split(r"[/\-_{}\.\s]+", path) if p]
    base = "_".join(parts[-2:]) if parts else method
    base = "".join(ch for ch in base if ch.isalnum() or ch == "_").lower() or method.lower()
    candidate = f"{method.lower()}_{base}"
    counter = 2
    while candidate in used:
        candidate = f"{method.lower()}_{base}_{counter}"
        counter += 1
    used.add(candidate)
    return candidate


def load_har_file(path: str) -> list[Endpoint]:
    with open(path, "r", encoding="utf-8-sig") as f:
        doc = json.load(f)
    return parse_har(doc, source=path)
