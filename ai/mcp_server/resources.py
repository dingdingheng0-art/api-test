"""MCP resources：让智能体**按需**读大块内容。

**为什么要有 resources 而不是都做成 tools**

工具返回的内容会直接进上下文。用例文件、OpenAPI 快照、表结构动辄
几万字，做成工具就是"读一次烧一次钱"，而且很容易把上下文撑爆。

MCP 的 resources 是「客户端自己去取」的语义：

- 客户端先拿到 URI 列表（只有标题和描述，很便宜）
- 需要时才 ``resources/read`` 某一个
- 读到的内容由客户端决定怎么截断

URI 设计（``scheme://路径``，路径里不放中文和空格）::

    apitest://cases/{project}/{module}/{文件名}
    apitest://openapi
    apitest://schema/{别名}
    apitest://report/latest
    apitest://config/modules
    apitest://audit/recent
"""

from __future__ import annotations

import json
from typing import Any

from ai.mcp_server.protocol import RPCError, INVALID_PARAMS
from utils import paths
from utils.logs.log_manager import log

URI_PREFIX = "apitest://"

# MIME 类型
MIME_MARKDOWN = "text/markdown"
MIME_JSON = "application/json"
MIME_YAML = "text/yaml"
MIME_TEXT = "text/plain"


def _resource(uri: str, name: str, description: str, mime: str) -> dict[str, Any]:
    return {"uri": uri, "name": name, "description": description, "mimeType": mime}


def list_resources() -> list[dict[str, Any]]:
    """列出所有可用资源（静态 + 用例文件 + 快照）。"""
    items: list[dict[str, Any]] = [
        _resource(
            f"{URI_PREFIX}openapi",
            "OpenAPI 快照",
            "从 Swagger/Apifox 同步下来的接口文档（全部接口定义）",
            MIME_JSON,
        ),
        _resource(
            f"{URI_PREFIX}report/latest",
            "最近一次运行结果",
            "reports/result.json：通过/失败统计",
            MIME_JSON,
        ),
        _resource(
            f"{URI_PREFIX}config/modules",
            "模块清单",
            "configs/modules.yaml：模块顺序、标签、描述",
            MIME_YAML,
        ),
        _resource(
            f"{URI_PREFIX}audit/recent",
            "智能体调用审计（最近 200 条）",
            ".cache/agent_audit.jsonl：每次工具调用的参数/结果/耗时/token",
            MIME_JSON,
        ),
    ]

    # 表结构快照
    schema_dir = paths.CACHE_DIR / "schema"
    if schema_dir.exists():
        for path in sorted(schema_dir.glob("*.json")):
            alias = path.stem.split("_", 1)[-1]
            items.append(
                _resource(
                    f"{URI_PREFIX}schema/{alias}",
                    f"表结构快照 {alias}",
                    f"{path.name}：数据库表/列定义",
                    MIME_JSON,
                )
            )

    # 用例文件（只列文件，不列内容）
    from testcases.case_loader import discover_cases, relative_case_path

    for project, path in discover_cases():
        rel = relative_case_path(path).as_posix()
        items.append(
            _resource(
                f"{URI_PREFIX}cases/{rel}",
                f"用例 {project}/{path.stem}",
                f"用例文件 {rel}",
                MIME_YAML if path.suffix == ".yaml" else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        )

    return items


def read_resource(uri: str) -> dict[str, Any]:
    """读一个资源，返回 MCP ``resources/read`` 的 contents 结构。"""
    if not uri.startswith(URI_PREFIX):
        raise RPCError(INVALID_PARAMS, f"不认识的 URI: {uri}", {"prefix": URI_PREFIX})

    rest = uri[len(URI_PREFIX):]
    text, mime = _read(rest)
    return {"contents": [{"uri": uri, "mimeType": mime, "text": text}]}


def _read(rest: str) -> tuple[str, str]:
    """按 URI 路径读内容。"""
    if rest == "openapi":
        path = paths.CACHE_DIR / "apidoc" / "openapi.json"
        return _read_text(path, MIME_JSON), MIME_JSON

    if rest == "report/latest":
        return _read_text(paths.REPORTS_ROOT / "result.json", MIME_JSON), MIME_JSON

    if rest == "config/modules":
        return _read_text(paths.MODULES_FILE, MIME_YAML), MIME_YAML

    if rest == "audit/recent":
        # 只给最近 200 条：审计日志会一直涨，整份塞给客户端没有意义，
        # 而且要按需聚合的话用 get_agent_cost 工具更合适。
        from ai.agent import audit_log

        entries = audit_log.recent(limit=200)
        return json.dumps(entries, ensure_ascii=False, indent=2), MIME_JSON

    if rest.startswith("schema/"):
        alias = rest.split("/", 1)[1]
        schema_dir = paths.CACHE_DIR / "schema"
        for path in schema_dir.glob(f"*{alias}.json"):
            return _read_text(path, MIME_JSON), MIME_JSON
        raise RPCError(INVALID_PARAMS, f"没有表结构快照: {alias}")

    if rest.startswith("cases/"):
        rel = rest.split("/", 1)[1]
        for root in _case_roots():
            candidate = root / rel
            if candidate.exists() and candidate.is_file():
                if candidate.suffix.lower() in (".xlsx", ".xlsm"):
                    # Excel 是二进制，转成解析后的 JSON 给客户端
                    from testcases.case_loader import load_cases_from_file

                    data = load_cases_from_file(candidate)
                    return json.dumps(data, ensure_ascii=False, indent=2), MIME_JSON
                return _read_text(candidate, MIME_YAML), MIME_YAML
        raise RPCError(INVALID_PARAMS, f"没有用例文件: {rel}")

    raise RPCError(INVALID_PARAMS, f"不认识的资源路径: {rest}")


def _case_roots() -> list[Any]:
    from utils.cases.case_source import get_source_roots

    try:
        return get_source_roots()
    except Exception:  # noqa: BLE001
        return [paths.CASES_ROOT, paths.CASES_EXCEL_ROOT]


def _read_text(path: Any, mime: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise RPCError(INVALID_PARAMS, f"资源不存在: {path.name}") from None
    except OSError as exc:
        log.warning("资源读取失败 %s: %s", path, exc)
        raise RPCError(INVALID_PARAMS, f"资源读取失败: {exc}") from exc
