"""自动读取数据库表结构。

**为什么要做这个**：手工写校验 SQL 的时候，最费时间的不是写 SELECT，
而是"我到底要查哪张表、哪个字段叫什么"。这些信息数据库里本来就有，
连上库读一下就有。

读完能干什么：

- **写校验 SQL** —— 看到真实的表名、字段名、注释，不用猜
- **反推校验点** —— 知道哪张表有哪个字段，就能自动生成"查这条记录在不在"
  这类断言
- **喂给 AI** —— 把表结构喂给模型，它生成的 SQL 才是对的

**缓存**：读完会存一份到 ``.cache/schema_<库名>.json``。
连一次库读结构要几百毫秒到几秒，每次跑测试都读一遍太慢。
缓存默认 1 小时过期，也可以用 ``--refresh`` 强制刷新。

**支持的数据库**：

- **mysql** —— 读 ``information_schema``
- **sqlite** —— 读 ``PRAGMA``（不用装任何东西，本地自测用）

用法::

    from verify.schema import SchemaInspector

    inspector = SchemaInspector({"type": "mysql", "host": ..., "database": "shop"})
    for table in inspector.list_tables():
        print(table.name, table.comment)
        for column in table.columns:
            print("   ", column.name, column.type, column.comment)
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from utils.logs.log_manager import log
from verify.db import DatabaseClient
from verify.errors import VerifyConfigError

# 缓存有效期（秒）—— 一小时
DEFAULT_CACHE_TTL = 3600

CACHE_DIR_NAME = "schema"


# ============================================================================
# 数据模型
# ============================================================================


@dataclass
class ColumnInfo:
    """一个字段。"""

    name: str = ""
    type: str = ""              # 归一化后的类型（int / varchar / decimal ...）
    raw_type: str = ""          # 原始类型（varchar(64)）
    nullable: bool = True
    default: Any = None
    primary_key: bool = False
    unique: bool = False
    auto_increment: bool = False
    comment: str = ""
    ordinal: int = 0            # 第几列（从 1 开始）

    @property
    def is_required(self) -> bool:
        """是不是必填字段（非空且没有默认值）。"""
        return not self.nullable and self.default is None

    @property
    def is_numeric(self) -> bool:
        """是不是数字类型（做数值范围校验用）。"""
        return self.type in (
            "int", "integer", "bigint", "smallint", "tinyint",
            "decimal", "numeric", "float", "double", "money",
        )

    @property
    def is_text(self) -> bool:
        return self.type in ("varchar", "char", "text", "longtext", "mediumtext", "tinytext")

    @property
    def is_datetime(self) -> bool:
        return self.type in ("datetime", "timestamp", "date", "time", "year")

    @property
    def max_length(self) -> int | None:
        """字符串的最大长度（从 ``varchar(64)`` 里抠出来）。"""
        if "(" not in self.raw_type:
            return None
        inner = self.raw_type[self.raw_type.index("(") + 1 : self.raw_type.rindex(")")]
        first = inner.split(",")[0].strip()
        return int(first) if first.isdigit() else None


@dataclass
class IndexInfo:
    """一个索引。"""

    name: str = ""
    columns: list[str] = field(default_factory=list)
    unique: bool = False
    primary: bool = False


@dataclass
class ForeignKeyInfo:
    """一个外键。"""

    name: str = ""
    column: str = ""
    referenced_table: str = ""
    referenced_column: str = ""


@dataclass
class TableInfo:
    """一张表。"""

    name: str = ""
    comment: str = ""
    engine: str = ""
    approx_rows: int = 0
    columns: list[ColumnInfo] = field(default_factory=list)
    indexes: list[IndexInfo] = field(default_factory=list)
    foreign_keys: list[ForeignKeyInfo] = field(default_factory=list)

    # ---------------- 便捷属性 ----------------

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]

    @property
    def primary_key(self) -> list[str]:
        """主键字段（按顺序）。"""
        for index in self.indexes:
            if index.primary:
                return list(index.columns)
        return [c.name for c in self.columns if c.primary_key]

    @property
    def required_columns(self) -> list[ColumnInfo]:
        """必填字段。"""
        return [c for c in self.columns if c.is_required]

    def get_column(self, name: str) -> ColumnInfo | None:
        """按名字找字段（大小写不敏感）。"""
        target = name.lower()
        for column in self.columns:
            if column.name.lower() == target:
                return column
        return None

    def find_columns(self, keyword: str) -> list[ColumnInfo]:
        """按关键字找字段 —— 名字里含这个关键词的。

        用来做"哪个字段能装订单号"这类推断。
        """
        target = keyword.lower()
        return [
            c for c in self.columns
            if target in c.name.lower() or target in (c.comment or "").lower()
        ]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DatabaseSchema:
    """整个库的表结构。"""

    database: str = ""
    tables: list[TableInfo] = field(default_factory=list)
    fetched_at: float = 0.0

    def get_table(self, name: str) -> TableInfo | None:
        target = name.lower()
        for table in self.tables:
            if table.name.lower() == target:
                return table
        return None

    def find_tables(self, keyword: str) -> list[TableInfo]:
        """按关键字找表（表名或注释里含这个关键词）。"""
        target = keyword.lower()
        return [
            t for t in self.tables
            if target in t.name.lower() or target in (t.comment or "").lower()
        ]

    def find_columns(self, keyword: str) -> list[tuple[TableInfo, ColumnInfo]]:
        """在整个库里找字段 —— 返回 ``[(表, 字段), ...]``。

        这个是写校验 SQL 最常用的入口：
        想知道"订单号存在哪个字段"，搜一下就有了。
        """
        results: list[tuple[TableInfo, ColumnInfo]] = []
        for table in self.tables:
            for column in table.find_columns(keyword):
                results.append((table, column))
        return results

    def to_dict(self) -> dict[str, Any]:
        return {
            "database": self.database,
            "fetched_at": self.fetched_at,
            "tables": [t.to_dict() for t in self.tables],
        }


# ============================================================================
# 读取器
# ============================================================================


class SchemaInspector:
    """自动读取表结构。

    Args:
        config: 数据库连接配置（和 ``verify/db.py`` 一样）
        cache_ttl: 缓存有效期（秒），0 = 不缓存
        cache_dir: 缓存目录（默认 ``.cache/schema/``）
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        cache_ttl: int = DEFAULT_CACHE_TTL,
        cache_dir: Path | None = None,
    ):
        self.config = dict(config or {})
        self.cache_ttl = int(cache_ttl)
        self.cache_dir = cache_dir
        self._client: DatabaseClient | None = None

    # ---------------- 连接 ----------------

    @property
    def client(self) -> DatabaseClient:
        if self._client is None:
            self._client = DatabaseClient(self.config)
        return self._client

    @property
    def driver(self) -> str:
        return str(self.config.get("type") or "mysql").lower()

    @property
    def database(self) -> str:
        return str(self.config.get("database") or "")

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    # ---------------- 缓存 ----------------

    def _cache_file(self) -> Path | None:
        """缓存文件路径。"""
        if self.cache_ttl <= 0:
            return None
        if self.cache_dir is not None:
            directory = self.cache_dir
        else:
            from utils.paths import CACHE_DIR

            directory = CACHE_DIR / CACHE_DIR_NAME

        name = f"{self.driver}_{_safe_name(self.database or 'default')}.json"
        return directory / name

    def _read_cache(self, include_columns: bool = True) -> DatabaseSchema | None:
        """读缓存（过期了返回 None）。

        ``include_columns=False`` 时，只要缓存里有字段明细就直接用 ——
        免得为了看一眼表名就把库重连一遍。
        """
        path = self._cache_file()
        if path is None or not path.exists():
            return None

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

        fetched_at = float(data.get("fetched_at") or 0)
        if self.cache_ttl and (time.time() - fetched_at) > self.cache_ttl:
            log.info("表结构缓存已过期，重新读取")
            return None

        schema = _schema_from_dict(data)

        # 只读表名的时候，缓存里有明细就用明细版的
        if not include_columns and schema.tables:
            has_columns = any(t.columns for t in schema.tables)
            if has_columns:
                return schema

        # 要字段明细但缓存里没有 —— 不能用这份缓存
        if include_columns and schema.tables and not any(
            t.columns for t in schema.tables
        ):
            log.info("表结构缓存里没有字段明细，重新读取")
            return None

        log.info("用缓存的表结构（%s 张表）", len(schema.tables))
        return schema

    def _write_cache(self, schema: DatabaseSchema, include_columns: bool = True) -> None:
        """写缓存。

        **只读表名的时候不要覆盖缓存** —— 否则会把一份"有表名、没字段"的残缺
        结构存进去，下次要字段明细时就只能重连库了。
        """
        if not include_columns:
            # 只补表名，别把已有的字段明细冲掉
            existing = self._read_cache(include_columns=True)
            if existing is not None and existing.tables:
                merged = DatabaseSchema(
                    database=schema.database or existing.database,
                    tables=schema.tables,
                    fetched_at=existing.fetched_at,
                )
                # 保留原有的字段明细
                for table in merged.tables:
                    old = existing.get_table(table.name)
                    if old is not None:
                        table.columns = old.columns
                        table.indexes = old.indexes
                        table.foreign_keys = old.foreign_keys
                schema = merged

        path = self._cache_file()
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(schema.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            log.warning("写表结构缓存失败: %s", exc)

    # ---------------- 主入口 ----------------

    def inspect(
        self,
        refresh: bool = False,
        include_columns: bool = True,
    ) -> DatabaseSchema:
        """读取表结构。

        Args:
            refresh: 强制重新读（忽略缓存）
            include_columns: 是否读字段明细（只读表名会快很多）
        """
        if not refresh:
            cached = self._read_cache(include_columns)
            if cached is not None:
                return cached

        if self.driver == "sqlite":
            schema = self._inspect_sqlite(include_columns)
        elif self.driver in ("mysql", "mariadb"):
            schema = self._inspect_mysql(include_columns)
        else:
            raise VerifyConfigError(
                f"不支持的数据库类型 {self.driver!r}（支持 mysql / sqlite）"
            )

        self._write_cache(schema, include_columns)
        log.info(
            "已读取表结构: %s 张表（库 %s）",
            len(schema.tables), schema.database or "(sqlite)",
        )
        return schema

    # ---------------- MySQL ----------------

    def _inspect_mysql(self, include_columns: bool) -> DatabaseSchema:
        """读 MySQL 的 information_schema。"""
        db_name = self.database
        if not db_name:
            raise VerifyConfigError(
                "读表结构需要知道库名 —— config.yaml 里 db 段要配 database"
            )

        schema = DatabaseSchema(database=db_name, fetched_at=time.time())

        # ---- 表 ----
        table_rows = self.client.query(
            "SELECT TABLE_NAME, TABLE_COMMENT, ENGINE, TABLE_ROWS "
            "FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE' "
            "ORDER BY TABLE_NAME",
            (db_name,),
        )
        for row in table_rows:
            schema.tables.append(
                TableInfo(
                    name=str(row.get("TABLE_NAME") or ""),
                    comment=str(row.get("TABLE_COMMENT") or ""),
                    engine=str(row.get("ENGINE") or ""),
                    approx_rows=int(row.get("TABLE_ROWS") or 0),
                )
            )

        if not include_columns:
            return schema

        # ---- 字段 ----
        column_rows = self.client.query(
            "SELECT TABLE_NAME, COLUMN_NAME, ORDINAL_POSITION, COLUMN_DEFAULT, "
            "       IS_NULLABLE, DATA_TYPE, COLUMN_TYPE, COLUMN_KEY, "
            "       EXTRA, COLUMN_COMMENT "
            "FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = %s "
            "ORDER BY TABLE_NAME, ORDINAL_POSITION",
            (db_name,),
        )
        for row in column_rows:
            table = schema.get_table(str(row.get("TABLE_NAME") or ""))
            if table is None:
                continue
            table.columns.append(_column_from_mysql_row(row))

        # ---- 索引 ----
        index_rows = self.client.query(
            "SELECT TABLE_NAME, INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, COLUMN_NAME "
            "FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA = %s "
            "ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX",
            (db_name,),
        )
        _fill_indexes(schema, index_rows)

        # ---- 外键 ----
        try:
            fk_rows = self.client.query(
                "SELECT TABLE_NAME, CONSTRAINT_NAME, COLUMN_NAME, "
                "       REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME "
                "FROM information_schema.KEY_COLUMN_USAGE "
                "WHERE TABLE_SCHEMA = %s AND REFERENCED_TABLE_NAME IS NOT NULL",
                (db_name,),
            )
        except Exception as exc:  # noqa: BLE001 - 没权限读外键也无所谓
            log.warning("读外键信息失败（忽略）: %s", exc)
            fk_rows = []

        for row in fk_rows:
            table = schema.get_table(str(row.get("TABLE_NAME") or ""))
            if table is None:
                continue
            table.foreign_keys.append(
                ForeignKeyInfo(
                    name=str(row.get("CONSTRAINT_NAME") or ""),
                    column=str(row.get("COLUMN_NAME") or ""),
                    referenced_table=str(row.get("REFERENCED_TABLE_NAME") or ""),
                    referenced_column=str(row.get("REFERENCED_COLUMN_NAME") or ""),
                )
            )

        return schema

    # ---------------- SQLite ----------------

    def _inspect_sqlite(self, include_columns: bool) -> DatabaseSchema:
        """读 SQLite 的结构（用 PRAGMA）。"""
        schema = DatabaseSchema(database=self.database or "main", fetched_at=time.time())

        table_rows = self.client.query(
            "SELECT name, type FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        )
        for row in table_rows:
            schema.tables.append(
                TableInfo(
                    name=str(row.get("name") or ""),
                    comment="",
                    engine="sqlite",
                )
            )

        if not include_columns:
            return schema

        for table in schema.tables:
            # ---- 字段 ----
            for row in self.client.query(f"PRAGMA table_info('{table.name}')"):
                table.columns.append(
                    ColumnInfo(
                        name=str(row.get("name") or ""),
                        type=str(row.get("type") or "").lower().split("(")[0].strip(),
                        raw_type=str(row.get("type") or ""),
                        nullable=not bool(row.get("notnull")),
                        default=row.get("dflt_value"),
                        primary_key=bool(row.get("pk")),
                        ordinal=int(row.get("cid") or 0) + 1,
                    )
                )

            # ---- 索引 ----
            index_rows = self.client.query(f"PRAGMA index_list('{table.name}')")
            for index_row in index_rows:
                index_name = str(index_row.get("name") or "")
                columns = [
                    str(c.get("name") or "")
                    for c in self.client.query(
                        f"PRAGMA index_info('{index_name}')"
                    )
                ]
                table.indexes.append(
                    IndexInfo(
                        name=index_name,
                        columns=columns,
                        unique=bool(index_row.get("unique")),
                        primary=index_name.startswith("sqlite_autoindex"),
                    )
                )

            # ---- 外键 ----
            for fk_row in self.client.query(f"PRAGMA foreign_key_list('{table.name}')"):
                table.foreign_keys.append(
                    ForeignKeyInfo(
                        name=str(fk_row.get("id") or ""),
                        column=str(fk_row.get("from") or ""),
                        referenced_table=str(fk_row.get("table") or ""),
                        referenced_column=str(fk_row.get("to") or ""),
                    )
                )

        return schema


# ============================================================================
# 内部辅助
# ============================================================================


def _safe_name(name: str) -> str:
    """把名字变成能当文件名的形式。

    sqlite 的 database 是一段文件路径，里面有 ``:`` ``\\`` 这些不能进文件名的字符。
    """
    safe = "".join(ch if (ch.isalnum() or ch in "-_") else "_" for ch in name)
    # 太长的话截断 + 打个哈希，避免重名
    if len(safe) > 60:
        import hashlib

        digest = hashlib.md5(name.encode("utf-8")).hexdigest()[:8]
        safe = f"{safe[:50]}_{digest}"
    return safe or "default"


def _column_from_mysql_row(row: dict[str, Any]) -> ColumnInfo:
    """把 information_schema 的一行变成 ColumnInfo。"""
    raw_type = str(row.get("COLUMN_TYPE") or "")
    data_type = str(row.get("DATA_TYPE") or "").lower()

    extra = str(row.get("EXTRA") or "").lower()
    column_key = str(row.get("COLUMN_KEY") or "").upper()

    default = row.get("COLUMN_DEFAULT")
    # MySQL 的默认值是字符串 "NULL" 时要当 NULL
    if isinstance(default, str) and default.upper() == "NULL":
        default = None

    return ColumnInfo(
        name=str(row.get("COLUMN_NAME") or ""),
        type=data_type,
        raw_type=raw_type,
        nullable=str(row.get("IS_NULLABLE") or "YES").upper() == "YES",
        default=default,
        primary_key=(column_key == "PRI"),
        unique=(column_key == "UNI"),
        auto_increment="auto_increment" in extra,
        comment=str(row.get("COLUMN_COMMENT") or ""),
        ordinal=int(row.get("ORDINAL_POSITION") or 0),
    )


def _fill_indexes(schema: DatabaseSchema, index_rows: list[dict[str, Any]]) -> None:
    """把索引行填进各张表（同名索引的多行要合并）。"""
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in index_rows:
        table_name = str(row.get("TABLE_NAME") or "")
        index_name = str(row.get("INDEX_NAME") or "")
        key = (table_name, index_name)

        entry = grouped.get(key)
        if entry is None:
            entry = {
                "name": index_name,
                "columns": [],
                "unique": not bool(row.get("NON_UNIQUE")),
                "primary": index_name == "PRIMARY",
            }
            grouped[key] = entry

        column_name = str(row.get("COLUMN_NAME") or "")
        if column_name:
            entry["columns"].append(column_name)

    for (table_name, _), entry in grouped.items():
        table = schema.get_table(table_name)
        if table is None:
            continue
        table.indexes.append(
            IndexInfo(
                name=entry["name"],
                columns=entry["columns"],
                unique=entry["unique"],
                primary=entry["primary"],
            )
        )


def _schema_from_dict(data: dict[str, Any]) -> DatabaseSchema:
    """从缓存的 JSON 还原成 DatabaseSchema。"""
    schema = DatabaseSchema(
        database=str(data.get("database") or ""),
        fetched_at=float(data.get("fetched_at") or 0),
    )

    for table_data in data.get("tables") or []:
        table = TableInfo(
            name=str(table_data.get("name") or ""),
            comment=str(table_data.get("comment") or ""),
            engine=str(table_data.get("engine") or ""),
            approx_rows=int(table_data.get("approx_rows") or 0),
        )
        for column_data in table_data.get("columns") or []:
            table.columns.append(ColumnInfo(**column_data))
        for index_data in table_data.get("indexes") or []:
            table.indexes.append(IndexInfo(**index_data))
        for fk_data in table_data.get("foreign_keys") or []:
            table.foreign_keys.append(ForeignKeyInfo(**fk_data))
        schema.tables.append(table)

    return schema
