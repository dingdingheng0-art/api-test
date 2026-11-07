"""建图：把 OpenAPI 快照 + 表结构 + 用例文件合成一张知识图谱。

**输入**（缺哪个就跳过哪部分，不报错）

========================================  ==================================
来源                                      提供什么
========================================  ==================================
``.cache/apidoc/openapi.json``            端点、参数、请求体字段
``.cache/schema/*.json``                  表、列
用例文件（Excel / YAML）                   用例、步骤、断言、**已验证的映射**
========================================  ==================================

**建图顺序很关键**

1. 先建表和列节点 —— 后面的映射要靠它们校验候选列是否真实存在
2. 再建端点和参数节点，并记下每个端点的请求体字段
3. **从用例的 ``db_query`` 断言里学已验证的映射**（``confidence=1.0``）
4. 最后用命名规则补剩下的（0.9 / 0.6）

第 3 步学到的映射会**覆盖**第 4 步 —— 所以规则推导前先把已验证的
``(字段, 表, 列)`` 记下来，跳过重复的。

**幂等**

每次全量重建（先清空再写）。图谱是**派生数据**，源文件变了就重建 ——
几十毫秒的事，比增量合并（哪些节点该删、边怎么跟着失效）可靠得多。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from ai.knowledge.field_map import (
    FieldMapping,
    is_tech_column,
    names_match,
    table_candidates_detail,
)
from ai.knowledge.store import (
    EDGE_ASSERTS_ON,
    EDGE_BELONGS_TO,
    EDGE_CALLS,
    EDGE_COVERED_BY,
    EDGE_HAS_COLUMN,
    EDGE_HAS_PARAM,
    EDGE_MAPS_TO,
    KIND_ASSERTION,
    KIND_CASE,
    KIND_COLUMN,
    KIND_ENDPOINT,
    KIND_PARAM,
    KIND_PROJECT,
    KIND_STEP,
    KIND_TABLE,
    KnowledgeStore,
    get_store,
)
from utils.logs.log_manager import log

#: 规则推导出的映射，置信度按"表名从哪来"分两档
CONFIDENCE_SCHEMA_TABLE = 0.9   # 表名来自请求体 DTO 名 / 已验证映射偏好，且快照里真有这张表
CONFIDENCE_GUESS = 0.6          # 表名靠路径/模块猜，或只是全库唯一同名列

_HTTP_METHODS = ("get", "post", "put", "delete", "patch")


# --------------------------------------------------------------------------
# 统计
# --------------------------------------------------------------------------


@dataclass
class BuildStats:
    """建图结果统计 —— 既给人看，也存进 ``meta`` 供自检比对。"""

    endpoints: int = 0
    params: int = 0
    tables: int = 0
    columns: int = 0
    cases: int = 0
    steps: int = 0
    assertions: int = 0
    mappings_verified: int = 0
    mappings_rule: int = 0
    mappings_guess: int = 0
    unmatched_fields: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    elapsed_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "endpoints": self.endpoints,
            "params": self.params,
            "tables": self.tables,
            "columns": self.columns,
            "cases": self.cases,
            "steps": self.steps,
            "assertions": self.assertions,
            "mappings": {
                "verified": self.mappings_verified,
                "rule": self.mappings_rule,
                "guess": self.mappings_guess,
            },
            "unmatched_fields": sorted(set(self.unmatched_fields))[:30],
            "warnings": self.warnings[:10],
            "elapsed_ms": self.elapsed_ms,
        }


# --------------------------------------------------------------------------
# 节点 / 边 ID 规范
# --------------------------------------------------------------------------


def endpoint_id(method: str, path: str) -> str:
    return f"endpoint:{method.upper()}:{path}"


def param_id(owner: str, name: str) -> str:
    return f"param:{owner}.{name}"


def table_id(table: str) -> str:
    return f"table:{table}"


def column_id(table: str, column: str) -> str:
    return f"column:{table}.{column}"


def case_id(project: str, module: str, file_stem: str, identity: str) -> str:
    """图谱里的用例节点 ID。

    没人拿这个 ID 去 ``get_case``（那是 :mod:`ai.agent.tools._common` 的
    ``case_<hash>``），所以这里用**可读**的拼法，方便人看图和调试。
    ``identity`` 的取值规则和那边一致（``case_id`` 优先，见
    :func:`ai.agent.tools._common.assign_ids`）。
    """
    return f"case:{project}/{module}/{file_stem}#{identity}"


def step_id(case: str, step_name: str) -> str:
    return f"step:{case}@{step_name}"


def assertion_id(case: str, step_name: str, index: int) -> str:
    return f"assertion:{case}@{step_name}${index}"


def project_id(project: str) -> str:
    return f"project:{project}"


# --------------------------------------------------------------------------
# 读源数据
# --------------------------------------------------------------------------


def _load_openapi() -> dict[str, Any]:
    from utils import paths

    path = paths.CACHE_DIR / "apidoc" / "openapi.json"
    if not path.exists():
        log.warning("没有 OpenAPI 快照，接口部分跳过：%s", path)
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("OpenAPI 快照读不了: %s", exc)
        return {}


def _load_tables() -> dict[str, dict[str, Any]]:
    """读所有表结构快照，合并成 ``{表名: 表定义}``。"""
    from utils import paths

    out: dict[str, dict[str, Any]] = {}
    cache_dir = paths.CACHE_DIR / "schema"
    if not cache_dir.exists():
        log.warning("没有表结构快照，表/列部分跳过：%s", cache_dir)
        return out

    for path in sorted(cache_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("表结构快照读不了 %s: %s", path.name, exc)
            continue
        for table in data.get("tables") or []:
            name = str(table.get("name") or "")
            if name:
                out[name] = table
    return out


def _resolve_schema(doc: dict[str, Any], schema: Any, depth: int = 0) -> dict[str, Any]:
    """展开 ``$ref``，最多三层（防循环引用）。"""
    if not isinstance(schema, dict) or depth > 3:
        return {}
    ref = schema.get("$ref")
    if not ref or not str(ref).startswith("#/"):
        return schema
    node: Any = doc
    for part in str(ref)[2:].split("/"):
        if not isinstance(node, dict):
            return schema
        node = node.get(part)
    return node if isinstance(node, dict) else {}


def _schema_name_of(schema: Any) -> str:
    """从 ``{"$ref": "#/components/schemas/Area"}`` 里取出 ``Area``。"""
    ref = schema.get("$ref") if isinstance(schema, dict) else None
    return str(ref).rsplit("/", 1)[-1] if ref else ""


# --------------------------------------------------------------------------
# SQL 解析（学已验证的映射用）
# --------------------------------------------------------------------------

_SQL_FROM = re.compile(r"\bFROM\s+([A-Za-z_][\w.]*)", re.IGNORECASE)
#: FROM 后面的整段表引用（到 WHERE / GROUP / ORDER / LIMIT 为止），
#: 用来判断是不是多表 —— 逗号必须在这段里找，不能在整个 SQL 里找，
#: 否则 ``SELECT COUNT(*), area_name FROM tz_area`` 会被误判成多表。
_SQL_FROM_CLAUSE = re.compile(
    r"\bFROM\s+(.*?)(?:\bWHERE\b|\bGROUP\b|\bORDER\b|\bLIMIT\b|$)",
    re.IGNORECASE | re.DOTALL,
)
_SQL_SELECT = re.compile(r"\bSELECT\s+(.*?)\s+FROM\b", re.IGNORECASE | re.DOTALL)
_SQL_WHERE = re.compile(
    r"\bWHERE\s+(.*?)(?:\bGROUP\b|\bORDER\b|\bLIMIT\b|$)",
    re.IGNORECASE | re.DOTALL,
)
_SQL_CMP_COLUMN = re.compile(
    r"([A-Za-z_]\w*)\s*(?:=|<>|!=|>=|<=|>|<|\bLIKE\b|\bIN\b)", re.IGNORECASE
)


def _clean_identifier(raw: str) -> str:
    return raw.strip().split(".")[-1].strip("`\"[] ")


def parse_sql(sql: str) -> tuple[str, list[str], list[str]]:
    """从 SQL 里抽 ``(表名, SELECT 列, WHERE 列)``。

    **只处理单表简单 SELECT** —— 复杂 SQL（JOIN / 子查询）返回空，
    交给人工。解析错了会学出**错误的映射**，比没有映射更糟：
    错的落库断言看起来是验过了，实际在骗人。
    """
    text = str(sql or "").strip()
    if not text:
        return "", [], []

    m_from = _SQL_FROM.search(text)
    if not m_from:
        return "", [], []

    # 子查询：这个 FROM 落在没闭合的括号里，说明它是子查询的 FROM，不是主表。
    # 数括号深度而不是"有没有出现过 ( `` —— ``SELECT COUNT(*), x FROM t``
    # 的括号在 FROM 前就已经闭合了，深度是 0，不是子查询。
    head = text[: m_from.start()]
    if head.count("(") > head.count(")"):
        return "", [], []

    m_clause = _SQL_FROM_CLAUSE.search(text)
    from_clause = m_clause.group(1) if m_clause else ""
    if " JOIN " in text.upper() or "," in from_clause:
        return "", [], []   # 多表，放弃

    table = _clean_identifier(m_from.group(1))

    select_columns: list[str] = []
    m_sel = _SQL_SELECT.search(text)
    if m_sel:
        for raw in m_sel.group(1).split(","):
            part = raw.strip()
            if not part or "(" in part:
                continue   # 函数调用（COUNT(*) 之类）不是列
            name = _clean_identifier(part.split()[0])
            if name and name != "*":
                select_columns.append(name)

    where_columns: list[str] = []
    m_where = _SQL_WHERE.search(text)
    if m_where:
        for name in _SQL_CMP_COLUMN.findall(m_where.group(1)):
            cleaned = _clean_identifier(name)
            if cleaned and cleaned.upper() not in ("AND", "OR", "NOT", "SELECT"):
                where_columns.append(cleaned)

    return table, select_columns, where_columns


# --------------------------------------------------------------------------
# 端点上下文（映射推导的输入）
# --------------------------------------------------------------------------


@dataclass
class EndpointCtx:
    """一个端点的请求体字段。"""

    eid: str
    path: str
    schema_name: str
    body_fields: list[str]
    param_names: list[str]


# --------------------------------------------------------------------------
# 建图主流程
# --------------------------------------------------------------------------


def build(
    store: KnowledgeStore | None = None,
    project: str = "",
    source: Any = None,
    include_cases: bool = True,
) -> BuildStats:
    """全量重建图谱。

    Args:
        store: 目标存储。不传就用全局单例（落 ``.cache/knowledge/graph.db``）
        project: 只建某个项目的用例部分。OpenAPI / 表结构是全局的，不受影响
        source: 用例来源（``excel`` / ``yaml`` / ``both``）。不传按当前开关
        include_cases: 是否把用例建进图。只想要接口图时关掉，快很多
    """
    store = store or get_store()
    started = time.perf_counter()
    stats = BuildStats()

    store.clear()

    tables = _load_tables()
    doc = _load_openapi()
    all_columns = {
        name: [str(c.get("name")) for c in (info.get("columns") or []) if c.get("name")]
        for name, info in tables.items()
    }

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    _emit_table_nodes(tables, nodes, edges, stats)
    contexts = _emit_endpoint_nodes(doc, project, nodes, edges, stats)

    # 先落节点：用例建图时要判断"这个端点存不存在"
    store.bulk_add(nodes, edges)
    known_endpoints = {ctx.eid for ctx in contexts}

    verified: list[FieldMapping] = []
    if include_cases:
        verified = _emit_case_nodes(
            store, project, source, all_columns, known_endpoints, stats
        )

    _emit_mapping_edges(store, tables, all_columns, contexts, verified, stats)

    stats.elapsed_ms = int((time.perf_counter() - started) * 1000)
    store.set_meta("built_at", time.time())
    store.set_meta("stats", stats.to_dict())

    summary = store.stats()
    log.info(
        "知识图谱建好：%s 节点 / %s 边 / 映射 %s 条（验证 %s + 规则 %s + 候选 %s），耗时 %sms",
        summary["nodes"],
        summary["edges"],
        stats.mappings_verified + stats.mappings_rule + stats.mappings_guess,
        stats.mappings_verified,
        stats.mappings_rule,
        stats.mappings_guess,
        stats.elapsed_ms,
    )
    return stats


# ---------------- 1. 表 / 列 ----------------


def _emit_table_nodes(
    tables: dict[str, dict[str, Any]],
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    stats: BuildStats,
) -> None:
    for table_name, table in sorted(tables.items()):
        nodes.append(
            {
                "id": table_id(table_name),
                "kind": KIND_TABLE,
                "name": table_name,
                "props": {
                    "comment": table.get("comment") or "",
                    "approx_rows": table.get("approx_rows"),
                    "column_count": len(table.get("columns") or []),
                },
                "body": f"{table_name} {table.get('comment') or ''}",
            }
        )
        stats.tables += 1

        for col in table.get("columns") or []:
            col_name = str(col.get("name") or "")
            if not col_name:
                continue
            nodes.append(
                {
                    "id": column_id(table_name, col_name),
                    "kind": KIND_COLUMN,
                    "name": col_name,
                    "props": {
                        "table": table_name,
                        "type": col.get("raw_type") or col.get("type"),
                        "nullable": col.get("nullable"),
                        "primary_key": bool(col.get("primary_key")),
                        "comment": col.get("comment") or "",
                    },
                    "body": f"{col_name} {col.get('raw_type') or ''} {col.get('comment') or ''}",
                }
            )
            stats.columns += 1

            # 表 -> 列。图扩展靠这条边：搜到 tz_area 要能带回它的列，
            # 否则 recall("tz_area") 只能返回一个孤零零的表节点。
            edges.append(
                {
                    "src": table_id(table_name),
                    "dst": column_id(table_name, col_name),
                    "kind": EDGE_HAS_COLUMN,
                }
            )


# ---------------- 2. 端点 / 参数 ----------------


def _emit_endpoint_nodes(
    doc: dict[str, Any],
    project: str,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    stats: BuildStats,
) -> list[EndpointCtx]:
    contexts: list[EndpointCtx] = []

    for path, methods in sorted((doc.get("paths") or {}).items()):
        if not isinstance(methods, dict):
            continue

        for method, op in methods.items():
            if method.lower() not in _HTTP_METHODS or not isinstance(op, dict):
                continue

            eid = endpoint_id(method, path)
            stats.endpoints += 1
            tags = [str(t) for t in (op.get("tags") or [])]
            summary = str(op.get("summary") or "")

            nodes.append(
                {
                    "id": eid,
                    "kind": KIND_ENDPOINT,
                    "name": f"{method.upper()} {path}",
                    "project": project,
                    "props": {
                        "method": method.upper(),
                        "path": path,
                        "summary": summary,
                        "operation_id": op.get("operationId") or "",
                        "tags": tags,
                        "deprecated": bool(op.get("deprecated")),
                    },
                    "body": " ".join(
                        [summary, str(op.get("description") or "")[:300], " ".join(tags), path]
                    ),
                }
            )

            param_names: list[str] = []

            # query / path / header 参数
            for raw in op.get("parameters") or []:
                if not isinstance(raw, dict):
                    continue
                pname = str(raw.get("name") or "")
                if not pname:
                    continue
                pid = param_id(eid, pname)
                param_names.append(pname)
                stats.params += 1
                nodes.append(
                    {
                        "id": pid,
                        "kind": KIND_PARAM,
                        "name": pname,
                        "project": project,
                        "props": {
                            "location": raw.get("in") or "query",
                            "required": bool(raw.get("required")),
                            "description": str(raw.get("description") or "")[:200],
                            "owner": eid,
                        },
                        "body": f"{pname} {raw.get('description') or ''}",
                    }
                )
                edges.append({"src": eid, "dst": pid, "kind": EDGE_HAS_PARAM})

            # 请求体字段
            content = (op.get("requestBody") or {}).get("content") or {}
            body_schema_raw = next(iter(content.values()), {}).get("schema") or {}
            body_schema = _resolve_schema(doc, body_schema_raw)
            schema_name = _schema_name_of(body_schema_raw)
            body_fields = [str(k) for k in (body_schema.get("properties") or {})]
            required = [str(x) for x in (body_schema.get("required") or [])]

            for fname in body_fields:
                pid = param_id(eid, fname)
                stats.params += 1
                nodes.append(
                    {
                        "id": pid,
                        "kind": KIND_PARAM,
                        "name": fname,
                        "project": project,
                        "props": {
                            "location": "body",
                            "required": fname in required,
                            "owner": eid,
                            "schema": schema_name,
                        },
                        "body": f"{fname} {schema_name}",
                    }
                )
                edges.append({"src": eid, "dst": pid, "kind": EDGE_HAS_PARAM})

            if body_fields or param_names:
                contexts.append(
                    EndpointCtx(
                        eid=eid,
                        path=path,
                        schema_name=schema_name,
                        body_fields=body_fields,
                        param_names=param_names,
                    )
                )

    return contexts


# ---------------- 3. 用例 / 步骤 / 断言 ----------------


def _emit_case_nodes(
    store: KnowledgeStore,
    project_filter: str,
    source: Any,
    all_columns: dict[str, list[str]],
    known_endpoints: set[str],
    stats: BuildStats,
) -> list[FieldMapping]:
    """把用例建进图，并从中学习**已验证**的字段映射。"""
    from ai.agent.tools._common import case_identities
    from testcases.case_loader import (
        discover_cases,
        load_cases_from_file,
        relative_case_path,
    )

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    verified: list[FieldMapping] = []
    projects_seen: set[str] = set()

    for proj, path in discover_cases(source=source):
        if project_filter and proj != project_filter:
            continue

        rel = relative_case_path(path)
        module = rel.parts[1] if len(rel.parts) > 1 else path.parent.name

        try:
            cases = load_cases_from_file(path, project=proj)
        except Exception as exc:  # noqa: BLE001 - 一个坏文件不该让整张图建不出来
            stats.warnings.append(f"用例文件读不了 {rel.as_posix()}: {exc}")
            continue

        if proj not in projects_seen:
            projects_seen.add(proj)
            nodes.append(
                {
                    "id": project_id(proj),
                    "kind": KIND_PROJECT,
                    "name": proj,
                    "project": proj,
                    "props": {},
                    "body": proj,
                }
            )

        bodies = [
            data.get("testcase") if isinstance(data.get("testcase"), dict) else data
            for data in cases
        ]
        # 用例身份规则和 ai.agent.tools 保持同一套，别各算各的
        identities = case_identities(bodies)

        for case, identity in zip(bodies, identities):
            cid = _emit_one_case(
                case, proj, module, path.stem, identity, known_endpoints,
                all_columns, nodes, edges, verified, stats,
            )
            edges.append({"src": cid, "dst": project_id(proj), "kind": EDGE_BELONGS_TO})

    store.bulk_add(nodes, edges)
    stats.mappings_verified = len(verified)
    return verified


def _emit_one_case(
    case: dict[str, Any],
    proj: str,
    module: str,
    file_stem: str,
    identity: str,
    known_endpoints: set[str],
    all_columns: dict[str, list[str]],
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    verified: list[FieldMapping],
    stats: BuildStats,
) -> str:
    """建一条用例的节点和边，返回用例 ID。"""
    name = str(case.get("name") or file_stem)
    cid = case_id(proj, module, file_stem, identity)
    stats.cases += 1

    nodes.append(
        {
            "id": cid,
            "kind": KIND_CASE,
            "name": name,
            "project": proj,
            "props": {
                "module": module,
                "file": file_stem,
                "case_id": case.get("case_id") or "",
                "status": case.get("status") or "",
                "marks": case.get("marks") or [],
                "description": str(case.get("description") or "")[:300],
            },
            "body": " ".join(
                [
                    name,
                    str(case.get("description") or ""),
                    module,
                    " ".join(str(m) for m in (case.get("marks") or [])),
                ]
            ),
        }
    )

    # setups 里发的字段也算"这个用例发过的"（落库断言的 WHERE 值常常来自 setups）
    setup_fields = _fields_of(case.get("setups"))

    for s_idx, step in enumerate(case.get("steps") or []):
        if not isinstance(step, dict):
            continue

        step_name = str(step.get("id") or f"step{s_idx}")
        sid = step_id(cid, step_name)
        method = str(step.get("method") or "").upper()
        path_str = str(step.get("path") or "")
        stats.steps += 1

        nodes.append(
            {
                "id": sid,
                "kind": KIND_STEP,
                "name": str(step.get("description") or step_name),
                "project": proj,
                "props": {"method": method, "path": path_str, "case": cid},
                "body": f"{method} {path_str}",
            }
        )

        # 步骤 -> 端点（路径里带 {{}} 的跳过：那是拼出来的，匹配不上）
        step_endpoint = ""
        if path_str and "{{" not in path_str:
            eid = endpoint_id(method, path_str)
            if eid in known_endpoints:
                step_endpoint = eid
                edges.append({"src": sid, "dst": eid, "kind": EDGE_CALLS})
                edges.append({"src": eid, "dst": cid, "kind": EDGE_COVERED_BY})

        # 这一步涉及的字段 = 自己的 + setups 带过来的
        own_fields = _fields_of([step])
        step_fields = own_fields + [f for f in setup_fields if f not in own_fields]

        for a_idx, assertion in enumerate(step.get("assert") or []):
            if not isinstance(assertion, dict):
                continue
            stats.assertions += 1
            aid = assertion_id(cid, step_name, a_idx)
            atype = str(assertion.get("type") or "")

            nodes.append(
                {
                    "id": aid,
                    "kind": KIND_ASSERTION,
                    "name": atype,
                    "project": proj,
                    "props": {
                        "type": atype,
                        "field": assertion.get("field"),
                        "expected": _shorten(assertion.get("expected")),
                        "step": sid,
                    },
                    "body": f"{atype} {assertion.get('field') or ''}",
                }
            )
            edges.append({"src": sid, "dst": aid, "kind": EDGE_ASSERTS_ON})

            if atype.startswith("db_query"):
                for mapping in _learn_from_db_assertion(
                    assertion,
                    str(case.get("name") or file_stem),
                    step_fields,
                    all_columns,
                    step_endpoint,
                ):
                    verified.append(mapping)
                    edges.append(
                        {
                            "src": aid,
                            "dst": column_id(mapping.table, mapping.column),
                            "kind": EDGE_ASSERTS_ON,
                            "confidence": mapping.confidence,
                            "evidence": mapping.evidence,
                        }
                    )

    return cid


def _fields_of(containers: Any) -> list[str]:
    """收集一批步骤 / setups 里出现过的请求字段名（保序去重）。"""
    if not isinstance(containers, list):
        return []

    out: list[str] = []

    def walk(container: Any) -> None:
        if isinstance(container, dict):
            out.extend(str(k) for k in container)
        elif isinstance(container, list):
            for item in container:
                walk(item)

    for step in containers:
        if not isinstance(step, dict):
            continue
        walk(step.get("data"))
        walk(step.get("params"))
    return list(dict.fromkeys(out))


def _learn_from_db_assertion(
    assertion: dict[str, Any],
    case_name: str,
    step_fields: list[str],
    all_columns: dict[str, list[str]],
    step_endpoint: str = "",
) -> list[FieldMapping]:
    """从一个**已经写好并跑通**的 ``db_query`` 断言里学映射。

    SQL 长这样::

        SELECT area_name FROM tz_area WHERE area_id = %s

    用例这一步又发了 ``areaName`` —— 于是 ``areaName`` →
    ``tz_area.area_name`` 这条映射就是**被验证过**的，置信度 1.0。

    WHERE 里的列也算：``params`` 里通常写着
    ``{{tools.max_id(查数据.data, "areaId")}}``，说明这个值来自用例自己发的
    ``areaId``，同样是硬证据。

    匹配用的是确定性的命名规则（驼峰 / 同义词），**没有"猜"的成分** ——
    猜出来的映射会让后面的落库断言写在错误的列上。

    ``step_endpoint`` 记进 ``verified_at``：这条证据**只在被调的那个接口上算数**，
    别推广到同名字段的其它接口（详见 :meth:`FieldMapping.applies_to`）。
    """
    table, select_columns, where_columns = parse_sql(str(assertion.get("query") or ""))
    if not table or table not in all_columns:
        return []

    valid_columns = set(all_columns[table])
    out: list[FieldMapping] = []

    for column in [*select_columns, *where_columns]:
        if column not in valid_columns:
            continue
        for field_name in step_fields:
            matched, reason = names_match(field_name, column)
            if not matched:
                continue
            out.append(
                FieldMapping(
                    api_field=field_name,
                    table=table,
                    column=column,
                    confidence=1.0,
                    evidence=(
                        f"用例「{case_name}」在 {step_endpoint or '未知接口'} 上"
                        f"的 {assertion.get('type')} 断言验证（{reason}）"
                    ),
                    source="verified",
                    verified_at=step_endpoint,
                )
            )
            break

    return out


# ---------------- 4. 映射边 ----------------


def _emit_mapping_edges(
    store: KnowledgeStore,
    tables: dict[str, dict[str, Any]],
    all_columns: dict[str, list[str]],
    contexts: list[EndpointCtx],
    verified: list[FieldMapping],
    stats: BuildStats,
) -> None:
    """写 ``param --maps_to--> column`` 边。

    先铺已验证的（1.0），再用命名规则补（0.9 / 0.6）——
    已验证的 ``(字段, 表, 列)`` 不重复推。

    **已验证的证据只贴在它自己的接口上**

    「修改地区」跑的是 ``PUT /admin/area``，它只证明了 PUT 的
    ``areaName`` → ``tz_area.area_name``。``POST /admin/area`` 也声明了
    ``areaName``，但新增用例没跑过 —— 给它贴 1.0 等于凭空造了个"验证过"。
    所以 ``applies_to`` 不做匹配的，交给下面的命名规则给 0.9。
    """
    edges: list[dict[str, Any]] = []

    # --- 已验证的：只关联到**真正验证过**的那个端点 ---
    # 记下已经写过 1.0 的边（src -> dst）。后面规则推导要跳过这些 ——
    # 边表按 (src, dst, kind) 去重，后用 0.9 覆盖会把 1.0 冲掉。
    verified_edges: dict[str, set[str]] = {}
    for mapping in verified:
        dst = column_id(mapping.table, mapping.column)
        for ctx in contexts:
            if mapping.api_field not in ctx.body_fields and mapping.api_field not in ctx.param_names:
                continue
            if not mapping.applies_to(ctx.eid):
                continue
            src = param_id(ctx.eid, mapping.api_field)
            edges.append(
                {
                    "src": src,
                    "dst": dst,
                    "kind": EDGE_MAPS_TO,
                    "confidence": 1.0,
                    "evidence": mapping.evidence,
                }
            )
            verified_edges.setdefault(src, set()).add(dst)

    # --- 规则推导的：只对**请求体字段**做 ---
    # query 参数里混着 page / limit / sort 这类分页参数，硬推就是垃圾映射；
    # 它们要是有真实映射，会从上面的 db_query 断言里学到。
    for ctx in contexts:
        candidates = [
            (name, origin)
            for name, origin in table_candidates_detail(ctx.schema_name, ctx.path)
            if name in all_columns
        ]
        preferred = {m.table for m in verified if m.api_field in ctx.body_fields}
        ordered = [c for c in candidates if c[0] in preferred] or candidates

        unmatched: list[str] = []

        for field_name in ctx.body_fields:
            best = _derive_mapping(field_name, ordered, all_columns)

            if best is None:
                unmatched.append(field_name)
                continue

            src = param_id(ctx.eid, field_name)
            dst = column_id(best.table, best.column)

            if dst in verified_edges.get(src, set()):
                continue   # 这个端点上的这条映射已验证过，别用低置信度冲掉

            edges.append(
                {
                    "src": src,
                    "dst": dst,
                    "kind": EDGE_MAPS_TO,
                    "confidence": best.confidence,
                    "evidence": best.evidence,
                }
            )
            if best.confidence >= CONFIDENCE_SCHEMA_TABLE:
                stats.mappings_rule += 1
            else:
                stats.mappings_guess += 1

        if unmatched:
            stats.unmatched_fields.append(f"{ctx.path}: {','.join(unmatched[:5])}")

    store.bulk_add([], edges)


def _derive_mapping(
    field_name: str,
    candidates: list[tuple[str, str]],
    all_columns: dict[str, list[str]],
) -> FieldMapping | None:
    """给一个请求体字段推一条映射，推不出来返回 ``None``。

    两档：

    - 表名来自**请求体 DTO 名** → ``0.9``
    - 表名靠路径/模块猜，或只是全库唯一同名 → ``0.6``（只当候选）
    """
    # 技术字段（id / create_time / deleted ...）任何表都有，靠规则推必错
    if is_tech_column(field_name):
        return None

    for table, origin in candidates:
        for column in all_columns.get(table, []):
            matched, reason = names_match(field_name, column)
            if not matched:
                continue
            if origin == "schema":
                return FieldMapping(
                    api_field=field_name,
                    table=table,
                    column=column,
                    confidence=CONFIDENCE_SCHEMA_TABLE,
                    evidence=f"命名规则（{reason}）+ 表 {table} 由请求体 {origin} 推出",
                    source="rule",
                )
            return FieldMapping(
                api_field=field_name,
                table=table,
                column=column,
                confidence=CONFIDENCE_GUESS,
                evidence=(
                    f"命名规则（{reason}）+ 表 {table} 由"
                    f"{'接口路径' if origin == 'path' else '模块名'}推出，未经确认"
                ),
                source="heuristic",
            )

    # 退一步：全库找唯一同名列（表名对不上，但只有一处命中）
    hits = [
        (t, c)
        for t, cols in all_columns.items()
        for c in cols
        if not is_tech_column(c) and names_match(field_name, c)[0]
    ]
    if len(hits) == 1:
        t, c = hits[0]
        return FieldMapping(
            api_field=field_name,
            table=t,
            column=c,
            confidence=CONFIDENCE_GUESS,
            evidence=f"全库唯一同名列 {t}.{c}，表名未经确认",
            source="heuristic",
        )

    return None


def _shorten(value: Any, limit: int = 60) -> Any:
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"
