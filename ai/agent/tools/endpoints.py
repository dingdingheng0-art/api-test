"""接口文档查询工具（只读）。

**数据从哪来**

1. ``testcases/apis/<项目>/<模块>.yaml`` —— 手写的接口定义（可复用）
2. ``.cache/apidoc/openapi.json`` —— 从 Swagger / Apifox 同步下来的快照

两者都要能查。智能体写用例时先查接口定义（能直接 ``api: xxx`` 引用），
查不到再退回 OpenAPI 快照（知道有哪些字段、什么类型）。

**为什么要单独提供**：OpenAPI 快照有 100+ 接口、125 个 schema，
一次性塞给模型会撑爆上下文。工具负责「先搜再取」。
"""

from __future__ import annotations

import json
from typing import Any

from ai.agent.tools.registry import ToolError, ToolSpec, ok
from utils import paths
from utils.logs.log_manager import log

# OpenAPI 快照缓存（文件不大，读一次就够了）
_OPENAPI_CACHE: dict[str, Any] | None = None


def _openapi_path() -> Any:
    return paths.CACHE_DIR / "apidoc" / "openapi.json"


def load_openapi() -> dict[str, Any]:
    """读 OpenAPI 快照（缓存）。"""
    global _OPENAPI_CACHE
    if _OPENAPI_CACHE is not None:
        return _OPENAPI_CACHE

    path = _openapi_path()
    if not path.exists():
        raise ToolError(
            f"没有 OpenAPI 快照：{path}",
            hint="先同步接口文档：python tools/maint/sync_api.py --project <项目> --url <v3/api-docs>",
        )

    try:
        _OPENAPI_CACHE = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ToolError(f"OpenAPI 快照解析失败: {exc}") from exc
    return _OPENAPI_CACHE


def _resolve_ref(doc: dict[str, Any], schema: Any, depth: int = 0) -> Any:
    """展开 ``$ref``（只展开一层，避免递归爆栈）。"""
    if not isinstance(schema, dict) or depth > 3:
        return schema
    ref = schema.get("$ref")
    if not ref or not str(ref).startswith("#/"):
        return schema

    node: Any = doc
    for part in str(ref)[2:].split("/"):
        if not isinstance(node, dict):
            return schema
        node = node.get(part)
    if isinstance(node, dict) and "$ref" not in node:
        return node
    return schema


def _schema_fields(doc: dict[str, Any], schema: Any, limit: int = 40) -> list[dict[str, Any]]:
    """把 schema 摊成字段列表（名字 / 类型 / 是否必填 / 说明）。"""
    schema = _resolve_ref(doc, schema)
    if not isinstance(schema, dict):
        return []

    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    fields: list[dict[str, Any]] = []

    for name, spec in list(props.items())[:limit]:
        spec = _resolve_ref(doc, spec)
        item: dict[str, Any] = {
            "name": name,
            "type": spec.get("type") or ("object" if "properties" in spec else "any"),
            "required": name in required,
        }
        if spec.get("format"):
            item["format"] = spec["format"]
        if spec.get("description"):
            item["description"] = str(spec["description"])[:120]
        if spec.get("enum"):
            item["enum"] = list(spec["enum"])[:10]
        if spec.get("example") is not None:
            item["example"] = spec["example"]
        if item["type"] == "array":
            inner = _resolve_ref(doc, spec.get("items") or {})
            item["items_type"] = inner.get("type") or "any"
        fields.append(item)

    return fields


def _iter_operations(doc: dict[str, Any]):
    """遍历所有接口：``(path, method, operation)``。"""
    for path, methods in (doc.get("paths") or {}).items():
        if not isinstance(methods, dict):
            continue
        for method, op in methods.items():
            if method.lower() in ("get", "post", "put", "delete", "patch") and isinstance(op, dict):
                yield path, method.upper(), op


def search_endpoints(query: str = "", tag: str = "", method: str = "", limit: int = 30) -> dict[str, Any]:
    """按关键字 / 标签搜接口。"""
    doc = load_openapi()
    q = str(query or "").strip().lower()
    tag_q = str(tag or "").strip().lower()
    method_q = str(method or "").strip().upper()
    limit = max(1, min(int(limit or 30), 100))

    matches: list[dict[str, Any]] = []
    for path, verb, op in _iter_operations(doc):
        if method_q and verb != method_q:
            continue
        tags = [str(t) for t in (op.get("tags") or [])]
        if tag_q and not any(tag_q in t.lower() for t in tags):
            continue
        if q:
            haystack = " ".join(
                [
                    path,
                    str(op.get("summary") or ""),
                    str(op.get("operationId") or ""),
                    " ".join(tags),
                    str(op.get("description") or ""),
                ]
            ).lower()
            if q not in haystack:
                continue

        matches.append(
            {
                "method": verb,
                "path": path,
                "summary": op.get("summary") or "",
                "operation_id": op.get("operationId") or "",
                "tags": tags,
                "deprecated": bool(op.get("deprecated")),
            }
        )
        if len(matches) >= limit:
            break

    if not matches:
        return ok(
            total=0,
            endpoints=[],
            hint="没搜到。OpenAPI 里的 tag 形如 'product-controller'，可以先用空 query 看看有哪些 tag。",
        )

    return ok(total=len(matches), endpoints=matches)


def get_endpoint(path: str, method: str = "GET") -> dict[str, Any]:
    """取一个接口的完整定义（参数 / 请求体 / 响应）。"""
    if not path:
        raise ToolError("缺少 path", hint="先调 search_endpoints 找到接口路径")

    doc = load_openapi()
    methods = (doc.get("paths") or {}).get(path)
    if not isinstance(methods, dict):
        raise ToolError(
            f"OpenAPI 里没有路径 {path}",
            hint="路径要完全一致（含前缀）。先调 search_endpoints 搜。",
        )

    op = methods.get(method.lower())
    if not isinstance(op, dict):
        raise ToolError(
            f"{path} 不支持 {method}",
            hint=f"该路径支持: {', '.join(m.upper() for m in methods)}",
        )

    params = []
    for raw in op.get("parameters") or []:
        if not isinstance(raw, dict):
            continue
        schema = _resolve_ref(doc, raw.get("schema") or {})
        params.append(
            {
                "name": raw.get("name"),
                "in": raw.get("in"),
                "required": bool(raw.get("required")),
                "type": schema.get("type") or "any",
                "description": str(raw.get("description") or "")[:120],
            }
        )

    body_fields: list[dict[str, Any]] = []
    body_required = False
    content = ((op.get("requestBody") or {}).get("content") or {})
    if content:
        body_required = bool((op.get("requestBody") or {}).get("required"))
        first = next(iter(content.values()), {})
        body_fields = _schema_fields(doc, first.get("schema") or {})

    response_fields: list[dict[str, Any]] = []
    for code, resp in (op.get("responses") or {}).items():
        if not str(code).startswith("2"):
            continue
        resp_content = (resp or {}).get("content") or {}
        first = next(iter(resp_content.values()), {})
        if first:
            response_fields = _schema_fields(doc, first.get("schema") or {})
        break

    return ok(
        endpoint={
            "method": method.upper(),
            "path": path,
            "summary": op.get("summary") or "",
            "description": str(op.get("description") or "")[:1000],
            "operation_id": op.get("operationId") or "",
            "tags": op.get("tags") or [],
            "deprecated": bool(op.get("deprecated")),
            "params": params,
            "body_required": body_required,
            "body_fields": body_fields,
            "response_fields": response_fields,
        }
    )


def list_api_definitions(project: str = "", module: str = "") -> dict[str, Any]:
    """列出可复用的接口定义（``testcases/apis/`` 下的）。"""
    from utils.cases.interface_registry import InterfaceRegistry

    registry = InterfaceRegistry.get()
    items = registry.list_all(project)

    out: list[dict[str, Any]] = []
    for key, spec in items:
        parts = key.split("/")
        mod = parts[1] if len(parts) > 1 else ""
        if module and mod != module:
            continue
        out.append(
            {
                "key": key,
                "name": parts[-1],
                "project": parts[0],
                "module": mod,
                "method": str(spec.get("method") or "GET").upper(),
                "path": spec.get("path") or "",
                "has_headers": bool(spec.get("headers")),
            }
        )

    if not out:
        return ok(
            total=0,
            interfaces=[],
            hint=(
                "没有接口定义文件。接口定义放在 testcases/apis/<项目>/<模块>.yaml，"
                "用例里用 api: <名字> 引用；也可以直接用 OpenAPI 里的 path。"
            ),
        )

    return ok(total=len(out), interfaces=out)


TOOLS = [
    ToolSpec(
        name="search_endpoints",
        description=(
            "在 OpenAPI 快照里搜索接口（按路径/摘要/operationId/tag 关键字）。"
            "返回 method + path + 摘要 + tag。写用例前先用它找接口。"
        ),
        params_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "关键字，匹配路径/摘要/标签"},
                "tag": {"type": "string", "description": "按 tag 过滤，如 product-controller"},
                "method": {"type": "string", "description": "HTTP 方法过滤"},
                "limit": {"type": "integer", "description": "最多返回多少条（默认 30）", "default": 30},
            },
            "additionalProperties": False,
        },
        handler=search_endpoints,
        side_effect="read",
        tags=("endpoint",),
    ),
    ToolSpec(
        name="get_endpoint",
        description=(
            "获取一个接口的完整定义：query/path/header 参数、请求体字段（含类型/必填/枚举）、"
            "响应字段。写用例和加强断言时用。"
        ),
        params_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "接口路径，必须完全一致"},
                "method": {"type": "string", "description": "HTTP 方法，默认 GET", "default": "GET"},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        handler=get_endpoint,
        side_effect="read",
        tags=("endpoint",),
    ),
    ToolSpec(
        name="list_api_definitions",
        description=(
            "列出 testcases/apis/ 下可复用的接口定义。用例里写 api: <名字> 就能引用，"
            "改路径只改一处。"
        ),
        params_schema={
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "项目名"},
                "module": {"type": "string", "description": "模块名"},
            },
            "additionalProperties": False,
        },
        handler=list_api_definitions,
        side_effect="read",
        tags=("endpoint",),
    ),
]
