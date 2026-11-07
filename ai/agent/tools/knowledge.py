"""知识层工具：把接口、表、用例连起来的查询入口。

**这几个工具回答的是"关系"问题**

原来的工具是"查一类东西"：``search_endpoints`` 查接口、
``get_table_schema`` 查表、``list_cases`` 查用例。但真实工作里问的是：

- 这个字段落到哪个表哪个列？（写落库断言）
- 这个接口写的数据有没有被验过？（判断覆盖是不是假的）
- 这个接口历史上挂过吗？（自愈时参考）

这些跨了三个数据源，靠模型自己去拼，它只会瞎猜列名 ——
猜错的落库断言比没有断言更糟，因为看起来验过了。

**图谱过期了怎么办**

所有工具都先看图谱的 ``built_at`` 和源文件（OpenAPI 快照、表结构快照、用例目录）
的最新修改时间比一下，过期就提示重建，但不自动重建 ——
重建要读几十个 Excel，模型一个没想清楚的问题不该触发几秒的 IO。
"""

from __future__ import annotations

from typing import Any

from ai.agent.tools.registry import ToolError, ToolSpec, ok
from utils.logs.log_manager import log


def _stale_reason() -> str:
    """图谱是不是比源文件旧。返回空串表示不过期。"""
    from ai.knowledge.store import get_store
    from utils import paths

    store = get_store()
    built_at = store.get_meta("built_at")
    if not built_at:
        return "图谱还没建过"

    newest = 0.0
    sources = [
        paths.CACHE_DIR / "apidoc" / "openapi.json",
        *(paths.CACHE_DIR / "schema").glob("*.json"),
    ]

    from testcases.case_loader import discover_cases

    for _proj, path in discover_cases():
        sources.append(path)

    for path in sources:
        try:
            newest = max(newest, path.stat().st_mtime)
        except OSError:
            continue

    if newest > float(built_at):
        return "源文件比图谱新"
    return ""


def _ensure_graph() -> Any:
    """拿图谱；没建过就建一次（只在完全没数据时建，过期只提示）。"""
    from ai.knowledge.store import get_store

    store = get_store()
    if not store.get_meta("built_at"):
        from ai.knowledge.builder import build

        build(store=store)
    return store


def find_field_mapping(field: str, endpoint: str = "") -> dict[str, Any]:
    """查一个字段映射到哪张表的哪个列。"""
    from ai.knowledge.retriever import find_field_mapping as _find

    field = str(field or "").strip()
    if not field:
        raise ToolError("必须给字段名", hint="比如 areaName / area_name")

    store = _ensure_graph()
    result = _find(field, endpoint=endpoint, store=store)
    result["stale_hint"] = _stale_reason()
    return ok(**result)


def explain_endpoint(path: str, method: str = "") -> dict[str, Any]:
    """一个接口的上下文：字段映射 + 覆盖它的用例。"""
    from ai.knowledge.retriever import explain_endpoint as _explain

    path = str(path or "").strip()
    if not path:
        raise ToolError("必须给接口路径", hint="比如 /admin/area")

    store = _ensure_graph()
    result = _explain(path, method=method, store=store)
    result["stale_hint"] = _stale_reason()
    return ok(**result)


def search_knowledge(query: str, kind: str = "", limit: int = 10) -> dict[str, Any]:
    """在整张图谱上做混合召回（名匹配 + 全文 + 图扩展）。"""
    from ai.knowledge.retriever import Retriever

    query = str(query or "").strip()
    if not query:
        raise ToolError("必须给查询词", hint="字段名 / 接口路径 / 表名 / 用例名都行")

    kinds = tuple(k.strip() for k in kind.split(",") if k.strip()) if kind else ()
    store = _ensure_graph()
    hits = Retriever(store).recall(query, kinds=kinds, limit=limit)

    return ok(
        query=query,
        total=len(hits),
        hits=[h.to_dict() for h in hits],
        stale_hint=_stale_reason(),
    )


def rebuild_knowledge(include_cases: bool = True) -> dict[str, Any]:
    """重建图谱（源文件变了之后调）。"""
    from ai.knowledge.builder import build
    from ai.knowledge.store import get_store

    store = get_store()
    stats = build(store=store, include_cases=include_cases)
    return ok(stats=stats.to_dict(), graph=store.stats())


def knowledge_status() -> dict[str, Any]:
    """图谱规模 + 是否过期。"""
    from ai.knowledge.store import default_path, get_store

    store = get_store()
    if not store.get_meta("built_at"):
        return ok(built=False, path=str(default_path()), note="还没建过图谱，调 rebuild_knowledge 或任一知识工具会触发构建")

    return ok(
        built=True,
        path=str(default_path()),
        graph=store.stats(),
        stats=store.get_meta("stats") or {},
        stale_hint=_stale_reason(),
    )


TOOLS = [
    ToolSpec(
        name="find_field_mapping",
        description=(
            "查接口字段映射到数据库的哪张表哪个列（如 areaName → tz_area.area_name）。"
            "写落库断言（db_query）之前**必须**先用它确认列名，别自己猜。"
            "返回的 confidence：1.0 = 已被跑通的断言验证，0.9 = 命名规则+表确认，0.6 = 候选，不要直接用于断言。"
            "注意证据是**分接口**的：某接口的 1.0 不代表同名字段在别的接口也验证过。"
        ),
        params_schema={
            "type": "object",
            "properties": {
                "field": {"type": "string", "description": "字段名，areaName / area_name 都可以"},
                "endpoint": {
                    "type": "string",
                    "description": "限定接口，如 'PUT /admin/area'。不传则返回所有接口上的映射",
                    "default": "",
                },
            },
            "required": ["field"],
            "additionalProperties": False,
        },
        handler=find_field_mapping,
        side_effect="read",
        tags=("knowledge",),
    ),
    ToolSpec(
        name="explain_endpoint",
        description=(
            "一个接口的全景：请求体字段各落到哪张表的哪个列、哪些用例覆盖了它。"
            "写新用例前用它看清「这个接口动的是哪些数据」，避免漏断言。"
        ),
        params_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "接口路径，如 /admin/area"},
                "method": {"type": "string", "description": "HTTP 方法；不传返回该路径下所有方法", "default": ""},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        handler=explain_endpoint,
        side_effect="read",
        tags=("knowledge",),
    ),
    ToolSpec(
        name="search_knowledge",
        description=(
            "在知识图谱上做混合召回（精确名匹配 + 全文搜索 + 图关系扩展）。"
            "当你不确定某个概念在哪个接口/表/用例里时用它，比 search_endpoints 更宽。"
        ),
        params_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "查询词：字段名、路径、表名、用例名都行"},
                "kind": {
                    "type": "string",
                    "description": "只返回这些类型，逗号分隔：endpoint / param / table / column / case",
                    "default": "",
                },
                "limit": {"type": "integer", "description": "返回条数（默认 10）", "default": 10},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        handler=search_knowledge,
        side_effect="read",
        tags=("knowledge",),
    ),
    ToolSpec(
        name="knowledge_status",
        description="看知识图谱的规模（节点/边/映射数）和是否已过期（源文件比图谱新）。",
        params_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=knowledge_status,
        side_effect="read",
        tags=("knowledge",),
    ),
    ToolSpec(
        name="rebuild_knowledge",
        description=(
            "重建知识图谱。改了用例文件、更新了 OpenAPI / 表结构快照之后调它。"
            "全量重建，耗时约 1-2 秒。"
        ),
        params_schema={
            "type": "object",
            "properties": {
                "include_cases": {
                    "type": "boolean",
                    "description": "是否把用例也建进图（默认 true；false 快很多）",
                    "default": True,
                }
            },
            "additionalProperties": False,
        },
        handler=rebuild_knowledge,
        side_effect="read",
        safety="只写 .cache/knowledge/graph.db（派生数据），不碰用例和源码",
        tags=("knowledge",),
    ),
]
