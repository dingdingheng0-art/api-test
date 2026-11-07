"""OpenAPI / Swagger 解析器。

支持两种版本：

- **Swagger 2.0**（``swagger: "2.0"``）—— 老格式，参数都在 ``paths -> 参数 -> parameters``
- **OpenAPI 3.x**（``openapi: 3.x.x``）—— 参数按位置分成
  ``parameters`` / ``requestBody`` / ``responses``

两种都能读，输出统一的 :class:`Endpoint` 列表。
"""
from __future__ import annotations

import json
from typing import Any

from tools.importer.models import Endpoint, Param

HTTP_METHODS = ("get", "put", "post", "delete", "patch", "head", "options", "trace")


def _resolve_ref(ref: str, root: dict[str, Any]) -> dict[str, Any]:
    """解析 ``$ref: '#/components/schemas/Xxx'`` 这种内部引用。"""
    if not ref.startswith("#/"):
        return {}
    node: Any = root
    for part in ref.lstrip("#/").split("/"):
        if not isinstance(node, dict) or part not in node:
            return {}
        node = node[part]
    return node if isinstance(node, dict) else {}


def _deref(schema: Any, root: dict[str, Any]) -> dict[str, Any]:
    """拿到 schema 真正的定义（解开 $ref）。"""
    if not isinstance(schema, dict):
        return {}
    if "$ref" in schema:
        return _resolve_ref(str(schema["$ref"]), root)
    return schema


def _example_and_fake(schema: dict[str, Any], root: dict[str, Any],
                      depth: int = 0) -> tuple[Any, bool]:
    """取示例值，并标记它是不是"造出来的假值"。

    文档里真给了 example / default / enum → 不是假值；
    按类型造出来的 → 是假值（生成用例时用它占位，但要标出来）。
    """
    if not isinstance(schema, dict) or depth > 6:
        return None, False

    if "example" in schema:
        return schema["example"], False

    schema = _deref(schema, root)

    if "example" in schema:
        return schema["example"], False
    if "default" in schema:
        return schema["default"], False

    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return enum[0], False

    schema_type = schema.get("type")
    if schema_type == "array":
        item_schema = schema.get("items") or {}
        item, _ = _example_and_fake(item_schema, root, depth + 1)
        return [item], True
    if schema_type == "object":
        props = schema.get("properties") or {}
        result: dict[str, Any] = {}
        for key, sub in list(props.items())[:20]:
            value, _ = _example_and_fake(sub, root, depth + 1)
            result[key] = value
        return result, True

    return _fake_value(schema_type, schema.get("format")), True


def _example_of(schema: dict[str, Any], root: dict[str, Any], depth: int = 0) -> Any:
    """只要示例值，不关心真假。"""
    value, _ = _example_and_fake(schema, root, depth)
    return value


def _fake_value(schema_type: Any, fmt: Any = None) -> Any:
    """按类型造一个像样的假值（比返回 None 更有用，生成的用例能直接跑）。"""
    if schema_type == "integer":
        return 0
    if schema_type == "number":
        return 0
    if schema_type == "boolean":
        return True
    if schema_type == "array":
        return []
    if schema_type == "object":
        return {}
    if fmt == "date-time":
        return "2024-01-01 00:00:00"
    if fmt == "date":
        return "2024-01-01"
    if fmt in ("int64", "int32"):
        return 0
    if fmt == "double":
        return 0
    return "string"


def _param_from_schema(name: str, schema: dict[str, Any], root: dict[str, Any],
                       location: str, required: bool, description: str = "") -> Param:
    schema = _deref(schema, root)
    example, fake = _example_and_fake(schema, root)
    return Param(
        name=name,
        location=location,
        required=required,
        type=str(schema.get("type") or "string"),
        example=example,
        description=description or str(schema.get("description") or ""),
        enum=list(schema.get("enum") or []),
        fake=fake,
    )


def _params_from_content(content: dict[str, Any], root: dict[str, Any]) -> tuple[list[Param], Any, str]:
    """从 OpenAPI 3 的 requestBody.content 里抽参数 + 请求体示例。

    Returns:
        ``(请求体字段列表, 请求体示例, 报文类型)``
    """
    params: list[Param] = []
    body_example: Any = None
    body_type = ""

    for media_type, media in content.items():
        media = media or {}
        schema = media.get("schema") or {}
        if media_type == "application/json" or media_type.endswith("+json"):
            body_type = "json"
            body_example = _example_of(schema, root)
        elif media_type in ("application/x-www-form-urlencoded", "multipart/form-data"):
            body_type = "form"
            props = _deref(schema, root).get("properties") or {}
            form_data: dict[str, Any] = {}
            for key, sub in props.items():
                sub = _deref(sub, root)
                params.append(
                    _param_from_schema(key, sub, root, "body", bool(sub.get("required")), "")
                )
                form_data[key] = _example_of(sub, root)
            body_example = form_data
        else:
            body_type = body_type or "json"
            body_example = _example_of(schema, root)

        # schema 是对象时，把每个字段也列成请求体字段（预览用）
        if body_type == "json":
            props = _deref(schema, root).get("properties") or {}
            required_set = set(_deref(schema, root).get("required") or [])
            for key, sub in props.items():
                desc = ""
                if isinstance(sub, dict):
                    desc = str(sub.get("description") or "")
                params.append(
                    _param_from_schema(key, sub, root, "body", key in required_set, desc)
                )

        break  # 只取第一个 content 类型

    return params, body_example, body_type


def parse_openapi(doc: dict[str, Any], source: str = "") -> list[Endpoint]:
    """把 OpenAPI / Swagger 文档解析成接口列表。"""
    if not isinstance(doc, dict):
        return []

    paths = doc.get("paths") or {}
    if not isinstance(paths, dict):
        return []

    # 服务地址
    base_url = _extract_base_url(doc)
    # Swagger 2.0 的 basePath 是路径前缀，要拼到每个 path 前面
    path_prefix = ""
    if doc.get("swagger") and str(doc.get("swagger")).startswith("2"):
        path_prefix = str(doc.get("basePath") or "").rstrip("/")

    endpoints: list[Endpoint] = []
    used_ids: set[str] = set()

    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        full_path = path_prefix + path

        # 路径级公共参数
        shared_params = path_item.get("parameters") or []
        path_tags = [str(t) for t in (path_item.get("tags") or [])]

        for method in HTTP_METHODS:
            operation = path_item.get(method)
            if not isinstance(operation, dict):
                continue

            tags = [str(t) for t in (operation.get("tags") or path_tags)]
            summary = str(operation.get("summary") or "").strip()
            description = str(operation.get("description") or "").strip()
            name = summary or description.split("\n")[0] or f"{method.upper()} {path}"

            headers: list[Param] = []
            query: list[Param] = []
            path_params: list[Param] = []
            cookies: list[Param] = []
            body_params: list[Param] = []
            body: Any = None
            body_type = ""

            # ---- 参数（Swagger 2.0 和 OpenAPI 3 都在 parameters 里）----
            raw_params = list(shared_params) + list(operation.get("parameters") or [])
            for raw in raw_params:
                if not isinstance(raw, dict):
                    continue
                raw = _deref(raw, doc)
                pname = str(raw.get("name") or "").strip()
                if not pname:
                    continue
                location = str(raw.get("in") or "query").lower()
                required = bool(raw.get("required"))
                desc = str(raw.get("description") or "")

                if location == "header":
                    schema = raw.get("schema") or {"type": raw.get("type", "string")}
                    headers.append(_param_from_schema(pname, schema, doc, "header", required, desc))
                elif location == "path":
                    schema = raw.get("schema") or {"type": raw.get("type", "string")}
                    path_params.append(_param_from_schema(pname, schema, doc, "path", True, desc))
                elif location == "query":
                    schema = raw.get("schema") or {"type": raw.get("type", "string")}
                    query.append(_param_from_schema(pname, schema, doc, "query", required, desc))
                elif location == "cookie":
                    schema = raw.get("schema") or {"type": raw.get("type", "string")}
                    cookies.append(_param_from_schema(pname, schema, doc, "cookie", required, desc))
                elif location == "body":
                    # Swagger 2.0 的 body 参数
                    schema = raw.get("schema") or {}
                    body = _example_of(schema, doc)
                    body_type = "json"
                elif location == "formData":
                    schema = raw.get("schema") or {"type": raw.get("type", "string")}
                    body = body if isinstance(body, dict) else {}
                    body[pname] = _example_of(schema, doc)
                    body_type = "form"

            # ---- Swagger 2.0 body 参数的字段展开 ----
            for raw in raw_params:
                if not isinstance(raw, dict):
                    continue
                raw = _deref(raw, doc)
                if str(raw.get("in") or "").lower() != "body":
                    continue
                schema = _deref(raw.get("schema") or {}, doc)
                props = schema.get("properties") or {}
                required_set = set(schema.get("required") or [])
                for key, sub in props.items():
                    sub = _deref(sub, doc)
                    body_params.append(
                        _param_from_schema(key, sub, doc, "body", key in required_set,
                                           str(sub.get("description") or ""))
                    )

            # ---- OpenAPI 3 的 requestBody ----
            request_body = operation.get("requestBody")
            if isinstance(request_body, dict):
                request_body = _deref(request_body, doc)
                content = request_body.get("content") or {}
                if isinstance(content, dict):
                    new_body_params, body_example, body_type2 = _params_from_content(content, doc)
                    if body_example is not None:
                        body = body_example
                    if body_type2:
                        body_type = body_type2
                    known = {p.name for p in body_params}
                    for p in new_body_params:
                        if p.name not in known:
                            body_params.append(p)

            # ---- 响应示例 ----
            response_example = None
            response_schema = None
            responses = operation.get("responses") or {}
            if isinstance(responses, dict):
                for code, resp in responses.items():
                    if not str(code).startswith("2"):
                        continue
                    if not isinstance(resp, dict):
                        continue
                    resp = _deref(resp, doc)
                    content = resp.get("content") or {}
                    if isinstance(content, dict):
                        for media_type, media in content.items():
                            schema = (media or {}).get("schema") or {}
                            response_example = _example_of(schema, doc)
                            response_schema = _deref(schema, doc) or None
                            break
                    if response_example is None and "schema" in resp:
                        # Swagger 2.0
                        schema = resp.get("schema") or {}
                        response_example = _example_of(schema, doc)
                        response_schema = _deref(schema, doc) or None
                    break

            # ---- 路径里的 {xxx} 补进 path 参数 ----
            import re as _re

            for token in _re.findall(r"\{([^}]+)\}", full_path):
                if not any(p.name == token for p in path_params):
                    path_params.append(Param(name=token, location="path", required=True,
                                            type="string", example=token))

            ep = Endpoint(
                id=_unique_id(full_path, method, used_ids),
                method=method.upper(),
                path=full_path,
                name=name,
                summary=summary,
                description=description,
                tags=tags,
                base_url=base_url,
                headers=headers,
                query=query,
                path_params=path_params,
                cookies=cookies,
                body_params=body_params,
                body=body,
                body_type=body_type,
                response_example=response_example,
                response_schema=response_schema,
                source=source,
            )
            endpoints.append(ep)

    return endpoints


def _unique_id(path: str, method: str, used: set[str]) -> str:
    """生成不重复的步骤 ID。"""
    import re as _re

    parts = [p for p in _re.split(r"[/\-_{}\.\s]+", path) if p]
    base = "_".join(parts[-2:]) if parts else method
    base = "".join(ch for ch in base if ch.isalnum() or ch == "_").lower() or method.lower()
    candidate = f"{method.lower()}_{base}"
    counter = 2
    while candidate in used:
        candidate = f"{method.lower()}_{base}_{counter}"
        counter += 1
    used.add(candidate)
    return candidate


def _extract_base_url(doc: dict[str, Any]) -> str:
    """取服务地址（OpenAPI 3 的 servers / Swagger 2.0 的 host+basePath）。"""
    servers = doc.get("servers")
    if isinstance(servers, list) and servers:
        first = servers[0]
        if isinstance(first, dict) and first.get("url"):
            return str(first["url"]).rstrip("/")

    host = str(doc.get("host") or "").strip()
    if host:
        schemes = doc.get("schemes") or ["https"]
        scheme = str(schemes[0]) if schemes else "https"
        base_path = str(doc.get("basePath") or "").rstrip("/")
        return f"{scheme}://{host}{base_path}"

    return ""


def load_openapi_file(path: str) -> list[Endpoint]:
    """从文件读取并解析（支持 .json / .yaml / .yml）。"""
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    return parse_openapi_text(text, source=path)


def parse_openapi_text(text: str, source: str = "") -> list[Endpoint]:
    """从文本解析（自动判断 JSON 还是 YAML）。"""
    text = text.strip()
    if text.startswith("{"):
        doc = json.loads(text)
    else:
        import yaml

        doc = yaml.safe_load(text)
    return parse_openapi(doc or {}, source=source)
