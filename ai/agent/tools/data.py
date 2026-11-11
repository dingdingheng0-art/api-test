"""数据层工具：库表结构、只读查库。

**这两个工具是「断言加强」和「自愈」的地基。**

没有表结构，智能体写的落库断言只能靠猜列名；
不能查库，它没法验证"接口说成功了，数据到底进没进"。

**安全边界（重要）**

- ``get_table_schema`` 读的是本地快照 ``.cache/schema/*.json``，不连库
- ``query_db`` 只允许 **SELECT / SHOW / DESCRIBE**，其余一律拒绝
- 连接用的是 ``.env`` 里的**只读账号**（只授权了 SELECT），
  两道闸门叠加 —— 即使 SQL 检查被绕过，数据库也不让写

这个限制写死在代码里，不通过配置开关放开。自愈智能体需要造数时，
走的是用例里的 setups/teardowns（那是被测业务自己的接口），
而不是直接写库。
"""

from __future__ import annotations

import json
import re
from typing import Any

from ai.agent.tools._params import clamp_int
from ai.agent.tools.registry import ToolError, ToolSpec, ok
from utils import paths
from utils.logs.log_manager import log

# 只读语句白名单（按第一个关键字判断）
_READONLY_VERBS = ("select", "show", "describe", "desc", "explain")

# 单次查询最多返回多少行（防止一条 SELECT * 把上下文撑爆）
MAX_ROWS = 200

# 表结构快照缓存：{路径: (mtime, 内容)}
#
# 带 mtime 的原因同 ``endpoints._OPENAPI_CACHE`` —— 长驻进程里重新
# dump 过表结构之后，不能还拿旧快照回答"这个字段是什么类型"。
_SCHEMA_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def _as_list(value: Any) -> list[Any]:
    """把「应该是数组」的字段收敛成 list（快照是外部工具写的）。"""
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, dict):
        return [value]
    return []


def _schema_files() -> list[Any]:
    cache_dir = paths.CACHE_DIR / "schema"
    if not cache_dir.exists():
        return []
    return sorted(cache_dir.glob("*.json"))


def _load_schema_file(path: Any) -> dict[str, Any]:
    key = str(path)
    try:
        mtime = path.stat().st_mtime
    except OSError as exc:
        log.warning("表结构快照读不了 %s: %s", path, exc)
        return {}

    cached = _SCHEMA_CACHE.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1]

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("表结构快照读不了 %s: %s", path, exc)
        data = {}
    if not isinstance(data, dict):
        # 被写坏成 ``[]`` / ``"..."`` 的快照当没有；否则查表结构的工具会到处 ``.get`` 崩。
        log.warning("表结构快照结构不对（%s）: %s", type(data).__name__, path)
        data = {}
    _SCHEMA_CACHE[key] = (mtime, data)
    return data


def _first_statement(sql: str) -> str:
    """去掉注释和空行，取第一条语句。"""
    cleaned = re.sub(r"--[^\n]*", " ", sql)
    cleaned = re.sub(r"/\*.*?\*/", " ", cleaned, flags=re.DOTALL)
    return cleaned.strip()


def _leading_verb(sql: str) -> str:
    """取 SQL 的第一个关键字（小写）。"""
    cleaned = _first_statement(sql)
    if not cleaned:
        return ""
    return cleaned.split()[0].lower().strip("(")


def query_db(
    sql: str,
    alias: str = "mall4j",
    limit: int = 50,
    params: Any = None,
) -> dict[str, Any]:
    """执行只读 SQL。"""
    if not sql or not sql.strip():
        raise ToolError("SQL 为空", hint="传一条 SELECT 语句")

    verb = _leading_verb(sql)
    if verb not in _READONLY_VERBS:
        raise ToolError(
            f"拒绝执行：只允许只读查询（SELECT/SHOW/DESCRIBE/EXPLAIN），收到的是 {verb.upper() or '空'}",
            hint=(
                "这个工具不会改库。要造数据请用被测业务接口（写进用例的 setups），"
                "不要直接 INSERT/UPDATE/DELETE。"
            ),
        )

    # ``SELECT`` 也能往磁盘写文件：``SELECT ... INTO OUTFILE '/path'``。
    # 第一个关键字是 SELECT，白名单拦不住 —— 单独按子句挡。
    squashed = re.sub(r"\s+", " ", _first_statement(sql).lower())
    for clause in ("into outfile", "into dumpfile"):
        if clause in squashed:
            raise ToolError(
                f"拒绝执行：{clause.upper()} 会把查询结果写到服务器本地文件",
                hint="这个工具只做只读查询，不负责导出文件。",
            )

    # 多语句注入：分号后面还有内容就拒绝
    statements = [s for s in _first_statement(sql).split(";") if s.strip()]
    if len(statements) > 1:
        raise ToolError("拒绝执行：一次只能跑一条语句")

    limit = clamp_int(limit, 50, 1, MAX_ROWS)
    bind = _bind_params(params)

    from utils.config.config_manager import ConfigManager
    from verify.db import DatabaseClient

    config = ConfigManager.get_db_config(alias)
    if not config:
        raise ToolError(
            f"没有配置数据库别名 {alias!r}",
            hint="在 configs/config.yaml 的 db: 下配置，连接信息走 .env。",
        )

    client = DatabaseClient(config)
    try:
        rows = client.query(sql, bind)
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"查询失败: {exc}", hint="先用 get_table_schema 确认表名/列名") from exc
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass

    truncated = len(rows) > limit
    return ok(
        alias=alias,
        row_count=len(rows),
        returned=min(len(rows), limit),
        truncated=truncated,
        rows=rows[:limit],
    )


def _bind_params(params: Any) -> list[Any] | None:
    """把工具入参归一成位置参数列表。

    以前 ``query_db`` 只收 SQL、不收参数，但工具描述写着"用 ``%s`` 占位" ——
    模型照着写 ``WHERE area_id = %s`` 就必然报错（占位符没有值）。
    现在参数真的能传下去了，方括号里按位置绑定。
    """
    if params is None:
        return None
    if isinstance(params, (list, tuple)):
        return list(params) or None
    raise ToolError(
        "params 必须是数组（按 %s 的顺序）",
        hint="例如 sql='SELECT ... WHERE area_id = %s', params=[110000]",
    )


def get_table_schema(table: str = "", keyword: str = "", alias: str = "mall4j") -> dict[str, Any]:
    """查表结构（读本地快照，不连库）。"""
    files = _schema_files()
    if not files:
        raise ToolError(
            "没有表结构快照",
            hint="先导出：python tools/maint/dump_schema.py --alias mall4j --format json",
        )

    # 优先挑文件名里带 alias 的
    picked = next((f for f in files if alias in f.stem), files[0])
    data = _load_schema_file(picked)
    tables = data.get("tables") or []
    if not isinstance(tables, list):
        raise ToolError(
            f"表结构快照的 tables 不是列表: {picked}",
            hint="重新导出快照：python tools/maint/dump_schema.py --alias <alias> --format json",
        )
    tables = [item for item in tables if isinstance(item, dict)]
    if not tables:
        raise ToolError(f"快照里没有表: {picked}")

    if table:
        for item in tables:
            if item.get("name") == table:
                columns = [c for c in _as_list(item.get("columns")) if isinstance(c, dict)]
                return ok(
                    source=picked.name,
                    table=item.get("name"),
                    comment=item.get("comment") or "",
                    approx_rows=item.get("approx_rows"),
                    columns=[
                        {
                            "name": c.get("name"),
                            "type": c.get("raw_type") or c.get("type"),
                            "nullable": c.get("nullable"),
                            "primary_key": c.get("primary_key"),
                            "auto_increment": c.get("auto_increment"),
                            "comment": c.get("comment") or "",
                        }
                        for c in columns
                    ],
                )
        raise ToolError(
            f"没有表 {table!r}",
            hint=f"先调 get_table_schema(keyword=...) 搜一下，共 {len(tables)} 张表",
        )

    kw = str(keyword or "").strip().lower()
    matches = [
        {
            "name": t.get("name"),
            "comment": t.get("comment") or "",
            "columns": len(_as_list(t.get("columns"))),
            "approx_rows": t.get("approx_rows"),
        }
        for t in tables
        if not kw or kw in str(t.get("name", "")).lower() or kw in str(t.get("comment", "")).lower()
    ]

    return ok(
        source=picked.name,
        database=data.get("database") or "",
        total_tables=len(tables),
        matched=len(matches),
        tables=matches[:100],
    )


TOOLS = [
    ToolSpec(
        name="query_db",
        description=(
            "对被测系统数据库执行**只读** SQL（仅 SELECT/SHOW/DESCRIBE/EXPLAIN）。"
            "用于验证接口写入是否落库、查测试数据、确认字段值。"
            "写操作一律被拒绝。"
        ),
        params_schema={
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "一条只读 SQL，用 %s 占位参数"},
                "alias": {
                    "type": "string",
                    "description": "数据库别名，默认 mall4j",
                    "default": "mall4j",
                },
                "limit": {
                    "type": "integer",
                    "description": "最多返回行数（默认 50，上限 200）",
                    "default": 50,
                },
                "params": {
                    "type": "array",
                    "items": {},
                    "description": "SQL 里 %s 占位符的位置参数，按顺序绑定（强烈建议用它，别拼字符串）",
                    "default": [],
                },
            },
            "required": ["sql"],
            "additionalProperties": False,
        },
        handler=query_db,
        side_effect="read",
        safety="只读白名单 + 只读数据库账号双保险",
        cost="network",
        tags=("data",),
    ),
    ToolSpec(
        name="get_table_schema",
        description=(
            "查询数据库表结构（读本地快照，不连库）。不传 table 就按 keyword 搜表名/注释；"
            "传 table 返回全部列（类型/是否可空/主键/自增/注释）。"
            "写落库断言前必须先用它确认列名。"
        ),
        params_schema={
            "type": "object",
            "properties": {
                "table": {"type": "string", "description": "表名，如 tz_area"},
                "keyword": {"type": "string", "description": "按表名/注释模糊搜"},
                "alias": {
                    "type": "string",
                    "description": "快照别名，默认 mall4j",
                    "default": "mall4j",
                },
            },
            "additionalProperties": False,
        },
        handler=get_table_schema,
        side_effect="read",
        tags=("data",),
    ),
]
