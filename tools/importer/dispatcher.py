"""格式识别 + 分发：把不同格式的文件都交给对应的解析器。"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from tools.importer.models import Endpoint

# 支持的格式
FORMAT_OPENAPI = "openapi"
FORMAT_HAR = "har"
FORMAT_CURL = "curl"

SUPPORTED = {
    FORMAT_OPENAPI: ("Swagger / OpenAPI", (".json", ".yaml", ".yml")),
    FORMAT_HAR: ("HAR（抓包导出）", (".har",)),
    FORMAT_CURL: ("cURL 命令", (".txt", ".sh", ".curl", ".http")),
}


class DocumentParseError(ValueError):
    """文档解析失败。"""


def detect_format(filename: str, content: str = "") -> str:
    """猜文件是什么格式。

    **内容优先于后缀** —— 因为：
    - HAR 也是 JSON，``.json`` 后缀可能是 HAR
    - ``.txt`` 里既可能是 cURL 也可能是别的东西

    看内容判断不出来时，才退回后缀。
    """
    text = (content or "").strip()
    suffix = Path(filename).suffix.lower()

    # ---- 先看内容 ----
    if text:
        # cURL 特征最明显，先判它
        if re.search(r"(^|\s|;|&&)curl\s", text[:2000]) and not text.startswith("{"):
            return FORMAT_CURL

        if text.startswith("{") or text.startswith("["):
            try:
                doc = json.loads(text)
            except json.JSONDecodeError:
                doc = None
            if isinstance(doc, dict):
                if "log" in doc:
                    return FORMAT_HAR
                if "openapi" in doc or "swagger" in doc or "paths" in doc:
                    return FORMAT_OPENAPI

    # ---- 再看后缀 ----
    if suffix == ".har":
        return FORMAT_HAR
    if suffix in (".json", ".yaml", ".yml"):
        return FORMAT_OPENAPI
    if suffix in (".txt", ".sh", ".curl", ".http"):
        return FORMAT_CURL

    return FORMAT_OPENAPI


def parse_document(
    filename: str,
    content: str,
    fmt: str | None = None,
) -> list[Endpoint]:
    """解析一份文档，返回接口列表。

    Args:
        filename: 文件名（用来判断格式）
        content: 文件文本内容
        fmt: 强制指定格式；不给就自动判断
    """
    fmt = fmt or detect_format(filename, content)

    if not (content or "").strip():
        raise DocumentParseError("文件是空的，没有内容可解析")

    if fmt == FORMAT_HAR:
        from tools.importer.har_parser import parse_har

        try:
            doc = json.loads(content)
        except json.JSONDecodeError as exc:
            raise DocumentParseError(f"HAR 文件不是合法 JSON：{exc}") from exc
        return parse_har(doc, source=filename)

    if fmt == FORMAT_CURL:
        from tools.importer.curl_parser import parse_curl

        return parse_curl(content, source=filename)

    if fmt == FORMAT_OPENAPI:
        from tools.importer.openapi_parser import parse_openapi_text

        try:
            return parse_openapi_text(content, source=filename)
        except Exception as exc:  # noqa: BLE001
            raise DocumentParseError(f"OpenAPI/Swagger 解析失败：{exc}") from exc

    raise DocumentParseError(f"不支持的格式：{fmt}")


def parse_file(path: str | Path) -> list[Endpoint]:
    """解析磁盘上的文件。"""
    file_path = Path(path)
    if not file_path.exists():
        raise DocumentParseError(f"文件不存在：{file_path}")

    content = file_path.read_text(encoding="utf-8-sig")
    return parse_document(file_path.name, content)


def summary_of(endpoints: list[Endpoint]) -> dict[str, Any]:
    """解析结果的概览（给网页预览用）。"""
    modules: dict[str, int] = {}
    methods: dict[str, int] = {}
    for ep in endpoints:
        modules[ep.module] = modules.get(ep.module, 0) + 1
        methods[ep.method] = methods.get(ep.method, 0) + 1

    return {
        "total": len(endpoints),
        "modules": modules,
        "methods": methods,
        "with_response": sum(1 for ep in endpoints if ep.response_example is not None),
        "with_body": sum(1 for ep in endpoints if ep.body is not None),
    }
