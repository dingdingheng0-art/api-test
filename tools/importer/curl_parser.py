"""cURL 命令解析器。

支持浏览器「复制为 cURL」和 Postman 导出的各种写法：

.. code-block:: bash

    curl 'https://api.xxx.com/order/list' \\
      -H 'accesstoken: xxx' \\
      -H 'Content-Type: application/json' \\
      --data-raw '{"pageSize":10}'

也支持一行写完、``-X POST``、``--data`` / ``--data-raw`` / ``--data-binary``、
``-d``、``-F``、``-b``、``-H``、``--url``、``--compressed`` 等。
"""
from __future__ import annotations

import json
import re
import shlex
from typing import Any
from urllib.parse import parse_qsl, urlparse

from tools.importer.models import Endpoint, Param, infer_module

# 会把下一个参数吃掉的选项
_VALUE_OPTIONS = {
    "-X", "--request",
    "-H", "--header",
    "-d", "--data", "--data-raw", "--data-binary", "--data-ascii", "--data-urlencode",
    "-b", "--cookie",
    "-F", "--form", "--form-string",
    "-u", "--user",
    "-A", "--user-ai.agent",
    "-e", "--referer",
    "-o", "--output",
    "--url",
    "-x", "--proxy",
    "-m", "--max-time",
    "--connect-timeout",
    "-w", "--write-out",
}
# 无值选项
_FLAG_OPTIONS = {
    "-G", "--get", "-I", "--head", "-k", "--insecure", "-s", "--silent",
    "-v", "--verbose", "--compressed", "-L", "--location", "-i", "--include",
    "-f", "--fail", "-N", "--no-buffer", "-g", "--globoff",
}


def parse_curl(text: str, source: str = "") -> list[Endpoint]:
    """解析一段 cURL 命令（可以包含多条，用换行或 ``&&`` 分隔）。"""
    endpoints: list[Endpoint] = []
    used_ids: set[str] = set()

    for chunk in _split_commands(text):
        endpoint = _parse_one(chunk, source, used_ids)
        if endpoint:
            endpoints.append(endpoint)

    return endpoints


def _split_commands(text: str) -> list[str]:
    """把多段 curl 命令拆开（按 ``&&`` 或独立的一行一条）。"""
    text = text.replace("\\\r\n", " ").replace("\\\n", " ")
    chunks: list[str] = []

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        # 一行里可能有 && 分隔的多条
        parts = [p.strip() for p in re.split(r"&&", line) if p.strip()]
        for part in parts:
            if part.lower().startswith("curl"):
                chunks.append(part)

    # 整段被拼成一行（用 && 分隔）的情况上面已处理；
    # 如果只有一行但里面有多个 curl，也再拆一次
    expanded: list[str] = []
    for chunk in chunks:
        matches = re.findall(r"curl\s+.*?(?=\s+curl\s+|$)", chunk)
        expanded.extend(matches if matches else [chunk])

    return expanded


def _parse_one(command: str, source: str, used_ids: set[str]) -> Endpoint | None:
    """解析一条 curl 命令。"""
    # 去掉开头的 curl
    body = re.sub(r"^\s*curl\s+", "", command).strip()

    try:
        tokens = shlex.split(body, posix=True)
    except ValueError:
        # 引号不配对时退回简单切分
        tokens = body.split()

    url = ""
    method = ""
    headers: list[Param] = []
    cookies = ""
    data_parts: list[str] = []
    data_is_form = False
    form_fields: dict[str, str] = {}

    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1

        if token in ("--url",):
            if index < len(tokens):
                url = tokens[index]
                index += 1
            continue

        if token in ("-X", "--request"):
            if index < len(tokens):
                method = tokens[index].upper()
                index += 1
            continue

        if token in ("-H", "--header"):
            if index < len(tokens):
                _add_header(headers, tokens[index])
                index += 1
            continue

        if token in ("-d", "--data", "--data-raw", "--data-binary", "--data-ascii", "--data-urlencode"):
            if index < len(tokens):
                value = tokens[index]
                if token == "--data-urlencode" and "=" in value:
                    key, _, val = value.partition("=")
                    form_fields[key] = val
                    data_is_form = True
                else:
                    data_parts.append(value)
                index += 1
            continue

        if token in ("-F", "--form", "--form-string"):
            if index < len(tokens):
                value = tokens[index]
                key, _, val = value.partition("=")
                form_fields[key] = val
                data_is_form = True
                index += 1
            continue

        if token in ("-b", "--cookie"):
            if index < len(tokens):
                cookies = tokens[index]
                index += 1
            continue

        if token in ("-A", "--user-ai.agent"):
            if index < len(tokens):
                headers.append(Param(name="User-Agent", location="header",
                                     example=tokens[index]))
                index += 1
            continue

        if token in ("-e", "--referer"):
            if index < len(tokens):
                headers.append(Param(name="Referer", location="header",
                                     example=tokens[index]))
                index += 1
            continue

        if token in _FLAG_OPTIONS:
            continue

        if token in _VALUE_OPTIONS:
            index += 1  # 跳过它的值
            continue

        if token.startswith("-"):
            continue

        if not url:
            url = token

    if not url:
        return None

    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme else ""
    path = parsed.path or "/"

    # 方法：没写 -X 就按有没有 body 猜
    if not method:
        method = "POST" if (data_parts or form_fields) else "GET"

    # Query 参数
    query: list[Param] = []
    for qname, qvalue in parse_qsl(parsed.query, keep_blank_values=True):
        query.append(Param(name=qname, location="query", example=qvalue))

    # 请求体
    body: Any = None
    body_type = ""
    if form_fields:
        body = form_fields
        body_type = "form"
    elif data_parts:
        raw = "&".join(data_parts)
        try:
            body = json.loads(raw)
            body_type = "json"
        except (json.JSONDecodeError, TypeError):
            body = raw
            body_type = "form"
            # form 形式：a=1&b=2
            parsed_form = dict(parse_qsl(raw, keep_blank_values=True))
            if parsed_form:
                body = parsed_form

    # Cookie
    cookie_params: list[Param] = []
    if cookies:
        for cname, cvalue in parse_qsl(cookies, keep_blank_values=True):
            cookie_params.append(Param(name=cname, location="cookie", example=cvalue))

    tags = [infer_module(path)]

    return Endpoint(
        id=_unique_id(path, method, used_ids),
        method=method,
        path=path,
        name=f"{method} {path}",
        summary=f"{method} {path}",
        tags=tags,
        base_url=base_url,
        headers=headers,
        query=query,
        cookies=cookie_params,
        body=body,
        body_type=body_type,
        source=source,
    )


def _add_header(headers: list[Param], raw: str) -> None:
    """解析一条 ``-H '名: 值'``。"""
    if ":" not in raw:
        return
    name, _, value = raw.partition(":")
    name = name.strip()
    if not name:
        return
    value = value.strip()
    # curl 里的占位符（如 <TOKEN>）原样保留
    headers.append(Param(name=name, location="header", example=value))


def _unique_id(path: str, method: str, used: set[str]) -> str:
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


def load_curl_file(path: str) -> list[Endpoint]:
    with open(path, "r", encoding="utf-8-sig") as f:
        return parse_curl(f.read(), source=path)
