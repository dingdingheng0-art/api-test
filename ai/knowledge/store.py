"""图谱存储：SQLite 三张表 + FTS5 全文索引。

**表结构**

.. code-block:: sql

    node(id TEXT PRIMARY KEY, kind TEXT, name TEXT, project TEXT,
         props TEXT, updated_at REAL)
    edge(src TEXT, dst TEXT, kind TEXT, confidence REAL, evidence TEXT,
         PRIMARY KEY (src, dst, kind))
    node_fts(name, body, id UNINDEXED, kind UNINDEXED)   -- FTS5

**为什么节点 ID 是字符串**

图遍历时经常要"从任意节点出发"，用 ``kind:name`` 拼出来的 ID
天然全局唯一，也不用维护自增映射：

    端点      endpoint:PUT:/admin/area
    参数      param:Area.areaName
    表        table:tz_area
    列        column:tz_area.area_name
    用例      case:mall4j/admin_area/修改地区
    断言      assertion:<用例id>#<步骤id>#<序号>

**为什么不删旧节点**

全量重建时先清空再写。图谱是**派生数据**，
源文件变了就重建，不做增量合并 —— 增量合并的逻辑
（哪些节点该删、边怎么跟着失效）比重建贵得多，还容易留下脏数据。
重建一次几十毫秒，没必要省。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from utils.logs.log_manager import log

# 节点类型
KIND_PROJECT = "project"
KIND_ENDPOINT = "endpoint"
KIND_PARAM = "param"
KIND_TABLE = "table"
KIND_COLUMN = "column"
KIND_CASE = "case"
KIND_STEP = "step"
KIND_ASSERTION = "assertion"

# 边类型
EDGE_HAS_PARAM = "has_param"      # 端点 -> 参数
EDGE_HAS_COLUMN = "has_column"    # 表 -> 列（图扩展靠它从表走到列）
EDGE_MAPS_TO = "maps_to"          # 参数/响应字段 -> 列（核心）
EDGE_COVERED_BY = "covered_by"    # 端点 -> 用例
EDGE_CALLS = "calls"              # 步骤 -> 端点
EDGE_ASSERTS_ON = "asserts_on"    # 断言 -> 列（来自 db_query）
EDGE_BELONGS_TO = "belongs_to"    # 任意节点 -> 项目

_SCHEMA = """
CREATE TABLE IF NOT EXISTS node (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    name        TEXT NOT NULL,
    project     TEXT NOT NULL DEFAULT '',
    props       TEXT NOT NULL DEFAULT '{}',
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_node_kind ON node(kind);
CREATE INDEX IF NOT EXISTS idx_node_project ON node(project);

CREATE TABLE IF NOT EXISTS edge (
    src         TEXT NOT NULL,
    dst         TEXT NOT NULL,
    kind        TEXT NOT NULL,
    confidence  REAL NOT NULL DEFAULT 1.0,
    evidence    TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (src, dst, kind)
);
CREATE INDEX IF NOT EXISTS idx_edge_dst ON edge(dst, kind);
CREATE INDEX IF NOT EXISTS idx_edge_src ON edge(src, kind);

CREATE TABLE IF NOT EXISTS meta (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);
"""

_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS node_fts USING fts5(
    name,
    body,
    id UNINDEXED,
    kind UNINDEXED,
    tokenize='unicode61'
);
"""


class KnowledgeStore:
    """知识图谱的读写接口。"""

    def __init__(self, path: Path | str = ":memory:") -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._fts = False

    # ---------------- 连接 ----------------

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn

        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA)

        try:
            conn.executescript(_FTS_SCHEMA)
            self._fts = True
        except sqlite3.OperationalError as exc:
            # 少数 Python 发行版没编 FTS5 —— 退回 LIKE 搜索，功能不丢
            log.warning("SQLite 没有 FTS5，全文搜索降级为 LIKE: %s", exc)
            self._fts = False

        self._conn = conn
        return conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # ---------------- 写入 ----------------

    def clear(self) -> None:
        """清空整张图（重建前调）。"""
        with self._lock:
            conn = self.conn
            conn.execute("DELETE FROM node")
            conn.execute("DELETE FROM edge")
            if self._fts:
                conn.execute("DELETE FROM node_fts")
            # 元信息描述的是"图里有什么"，图清空了它必须一起失效。
            # 否则重建中途失败时 built_at 还留着上一轮的值，
            # 工具会以为图谱是完整且最新的。
            conn.execute("DELETE FROM meta WHERE key IN ('built_at', 'stats')")
            conn.commit()

    def add_node(
        self,
        node_id: str,
        kind: str,
        name: str,
        project: str = "",
        props: dict[str, Any] | None = None,
        body: str = "",
    ) -> None:
        """写一个节点（重复写会覆盖）。"""
        with self._lock:
            conn = self.conn
            conn.execute(
                """INSERT INTO node(id, kind, name, project, props, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     kind=excluded.kind, name=excluded.name,
                     project=excluded.project, props=excluded.props,
                     updated_at=excluded.updated_at""",
                (
                    node_id,
                    kind,
                    name,
                    project,
                    json.dumps(props or {}, ensure_ascii=False),
                    time.time(),
                ),
            )
            if self._fts:
                conn.execute("DELETE FROM node_fts WHERE id = ?", (node_id,))
                conn.execute(
                    "INSERT INTO node_fts(name, body, id, kind) VALUES (?, ?, ?, ?)",
                    (name, body or self._fts_body(props), node_id, kind),
                )

    @staticmethod
    def _fts_body(props: dict[str, Any] | None) -> str:
        """用 props 里比较有信息量的字段拼检索正文。"""
        props = props or {}
        parts = [
            str(props.get(key) or "")
            for key in ("summary", "description", "comment", "path", "type", "module")
        ]
        return " ".join(p for p in parts if p)

    def add_edge(
        self,
        src: str,
        dst: str,
        kind: str,
        confidence: float = 1.0,
        evidence: str = "",
    ) -> None:
        """写一条边（同一对节点同一类型只保留一条）。"""
        with self._lock:
            conn = self.conn
            conn.execute(
                """INSERT INTO edge(src, dst, kind, confidence, evidence)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(src, dst, kind) DO UPDATE SET
                     confidence=excluded.confidence,
                     evidence=excluded.evidence""",
                (src, dst, kind, float(confidence), evidence),
            )

    def bulk_add(self, nodes: Iterable[dict[str, Any]], edges: Iterable[dict[str, Any]]) -> None:
        """批量写入（一次提交，快很多）。"""
        with self._lock:
            conn = self.conn
            for node in nodes:
                conn.execute(
                    """INSERT INTO node(id, kind, name, project, props, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(id) DO UPDATE SET
                         kind=excluded.kind, name=excluded.name,
                         project=excluded.project, props=excluded.props,
                         updated_at=excluded.updated_at""",
                    (
                        node["id"],
                        node["kind"],
                        node["name"],
                        node.get("project", ""),
                        json.dumps(node.get("props") or {}, ensure_ascii=False),
                        time.time(),
                    ),
                )
                if self._fts:
                    conn.execute(
                        "INSERT INTO node_fts(name, body, id, kind) VALUES (?, ?, ?, ?)",
                        (
                            node["name"],
                            node.get("body") or self._fts_body(node.get("props")),
                            node["id"],
                            node["kind"],
                        ),
                    )
            conn.executemany(
                """INSERT INTO edge(src, dst, kind, confidence, evidence)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(src, dst, kind) DO UPDATE SET
                     confidence=excluded.confidence,
                     evidence=excluded.evidence""",
                [
                    (
                        e["src"],
                        e["dst"],
                        e["kind"],
                        float(e.get("confidence", 1.0)),
                        e.get("evidence", ""),
                    )
                    for e in edges
                ],
            )
            conn.commit()

    def set_meta(self, key: str, value: Any) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, ensure_ascii=False)),
            )
            self.conn.commit()

    def get_meta(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except ValueError:
            return default

    # ---------------- 查询 ----------------

    def stats(self) -> dict[str, Any]:
        """图谱规模统计（自检和工具输出用）。"""
        conn = self.conn
        by_kind = {
            row["kind"]: row["n"]
            for row in conn.execute("SELECT kind, COUNT(*) AS n FROM node GROUP BY kind")
        }
        by_edge = {
            row["kind"]: row["n"]
            for row in conn.execute("SELECT kind, COUNT(*) AS n FROM edge GROUP BY kind")
        }
        return {
            "nodes": sum(by_kind.values()),
            "edges": sum(by_edge.values()),
            "by_kind": by_kind,
            "by_edge": by_edge,
            "has_fts": self._fts,
            "built_at": self.get_meta("built_at"),
        }

    def get_node(self, node_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM node WHERE id = ?", (node_id,)).fetchone()
        return self._row_to_node(row) if row else None

    @staticmethod
    def _row_to_node(row: sqlite3.Row) -> dict[str, Any]:
        try:
            props = json.loads(row["props"])
        except (ValueError, TypeError):
            props = {}
        return {
            "id": row["id"],
            "kind": row["kind"],
            "name": row["name"],
            "project": row["project"],
            "props": props,
        }

    def find_nodes(self, kind: str = "", project: str = "", limit: int = 100) -> list[dict[str, Any]]:
        sql = "SELECT * FROM node WHERE 1=1"
        params: list[Any] = []
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        if project:
            sql += " AND project = ?"
            params.append(project)
        sql += " ORDER BY name LIMIT ?"
        params.append(int(limit))
        return [self._row_to_node(r) for r in self.conn.execute(sql, params)]

    def neighbors(
        self,
        node_id: str,
        kind: str = "",
        direction: str = "both",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """取邻居节点（图扩展用）。"""
        clauses = []
        params: list[Any] = []
        if direction in ("out", "both"):
            clauses.append("e.src = ?")
            params.append(node_id)
        if direction in ("in", "both"):
            clauses.append("e.dst = ?")
            params.append(node_id)
        where = "(" + " OR ".join(clauses) + ")"

        kind_sql = ""
        if kind:
            kind_sql = " AND e.kind = ?"
            params.append(kind)

        rows = self.conn.execute(
            f"""SELECT n.*, e.kind AS edge_kind, e.confidence, e.evidence
                FROM edge e
                JOIN node n ON (
                    CASE WHEN e.src = ? THEN n.id = e.dst ELSE n.id = e.src END
                )
                WHERE {where}{kind_sql}
                ORDER BY e.confidence DESC
                LIMIT ?""",
            [node_id, *params, int(limit)],
        ).fetchall()

        out = []
        for row in rows:
            item = self._row_to_node(row)
            item["edge_kind"] = row["edge_kind"]
            item["confidence"] = row["confidence"]
            item["evidence"] = row["evidence"]
            out.append(item)
        return out

    def edges_of(self, node_id: str, kind: str = "") -> list[dict[str, Any]]:
        sql = "SELECT * FROM edge WHERE (src = ? OR dst = ?)"
        params: list[Any] = [node_id, node_id]
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        return [dict(r) for r in self.conn.execute(sql, params)]

    # ---------------- 全文搜索 ----------------

    def search(self, query: str, kind: str = "", limit: int = 20) -> list[dict[str, Any]]:
        """全文搜索节点。

        用 FTS5 的 BM25 排序（越相关越靠前）；没有 FTS5 就退回 LIKE。
        查询词里的特殊字符会被转义，避免用户输入 ``area-name`` 时
        被当成 FTS 语法报错。
        """
        query = str(query or "").strip()
        if not query:
            return []

        limit = max(1, min(int(limit or 20), 100))

        if self._fts:
            match = self._fts_query(query)
            sql = """SELECT n.*, bm25(node_fts) AS score
                     FROM node_fts JOIN node n ON n.id = node_fts.id
                     WHERE node_fts MATCH ?"""
            params: list[Any] = [match]
            if kind:
                sql += " AND n.kind = ?"
                params.append(kind)
            sql += " ORDER BY score LIMIT ?"
            params.append(limit)
            try:
                rows = self.conn.execute(sql, params).fetchall()
            except sqlite3.OperationalError:
                rows = []
            if rows:
                out = []
                for row in rows:
                    item = self._row_to_node(row)
                    item["score"] = round(float(row["score"]), 4)
                    out.append(item)
                return out

        # 兜底：LIKE（也用于 FTS 没命中时）
        like = f"%{query}%"
        sql = "SELECT * FROM node WHERE (name LIKE ? OR props LIKE ?)"
        params = [like, like]
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " LIMIT ?"
        params.append(limit)
        return [self._row_to_node(r) for r in self.conn.execute(sql, params)]

    @staticmethod
    def _fts_query(query: str) -> str:
        """把用户输入转成安全的 FTS5 查询。

        按空白切词，每段加引号（引号内的特殊字符当字面量），
        段与段之间用 AND —— 和"都要包含"的直觉一致。
        """
        tokens = [t for t in query.replace('"', " ").split() if t]
        if not tokens:
            return '""'
        return " AND ".join(f'"{t}"' for t in tokens)


# ---------------- 单例 ----------------

_store: KnowledgeStore | None = None
_store_lock = threading.Lock()


def default_path() -> Path:
    from utils import paths

    return paths.CACHE_DIR / "knowledge" / "graph.db"


def get_store(path: Path | str | None = None) -> KnowledgeStore:
    """拿全局图谱（默认落 ``.cache/knowledge/graph.db``）。"""
    global _store
    with _store_lock:
        if _store is None:
            _store = KnowledgeStore(path or default_path())
        return _store


def reset_store(path: Path | str | None = None) -> KnowledgeStore:
    """换成新的存储（测试用）。"""
    global _store
    with _store_lock:
        if _store is not None:
            _store.close()
        _store = KnowledgeStore(path or ":memory:")
        return _store
