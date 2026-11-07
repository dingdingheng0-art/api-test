"""表结构读取的单元测试。

**不连真实数据库** —— 用 sqlite（Python 自带）造库，
测的是"读结构"这套逻辑本身，不是某个具体库。

MySQL 那条路径靠 ``information_schema``，这里只测它拼出来的 SQL 对不对 ——
用假客户端把查询结果喂回去，不真连库。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from verify.schema import (
    ColumnInfo,
    DatabaseSchema,
    SchemaInspector,
    TableInfo,
)


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture()
def sqlite_file(tmp_path: Path) -> Path:
    """造一个带两张表的 sqlite 库文件。"""
    import sqlite3

    db_path = tmp_path / "demo.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE orders ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  order_no VARCHAR(64) NOT NULL,"
        "  status TINYINT DEFAULT 0,"
        "  amount DECIMAL(10,2),"
        "  created_at DATETIME"
        ")"
    )
    conn.execute(
        "CREATE TABLE users ("
        "  id INTEGER PRIMARY KEY,"
        "  name TEXT NOT NULL,"
        "  email VARCHAR(128) UNIQUE"
        ")"
    )
    conn.execute("CREATE INDEX idx_orders_no ON orders(order_no)")
    conn.execute("CREATE TABLE order_items ("
        "  id INTEGER PRIMARY KEY,"
        "  order_id INTEGER REFERENCES orders(id),"
        "  sku VARCHAR(32)"
        ")")
    conn.commit()
    conn.close()
    return db_path


class CountingInspector(SchemaInspector):
    """记录自己发过多少条查询的检查器。

    用来证明"第二次读走了缓存、没再查库"——
    比删库文件再试更稳（Windows 上 sqlite 文件还开着的时候删不掉）。
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.query_count = 0

    @property
    def client(self) -> Any:
        client = super().client
        original_query = client.query

        if getattr(client, "_counting_wrapped", False):
            return client

        def counting_query(sql: str, params: Any = None) -> list[dict[str, Any]]:
            self.query_count += 1
            return original_query(sql, params)

        client.query = counting_query  # type: ignore[method-assign]
        client._counting_wrapped = True  # type: ignore[attr-defined]
        return client


@pytest.fixture()
def inspector(sqlite_file: Path) -> SchemaInspector:
    """一个不缓存的 sqlite 检查器。"""
    insp = SchemaInspector(
        {"type": "sqlite", "database": str(sqlite_file)},
        cache_ttl=0,
    )
    yield insp
    insp.close()


# ---------------------------------------------------------------------------
# 读结构
# ---------------------------------------------------------------------------


class TestInspectSQLite:
    def test_lists_all_tables(self, inspector: SchemaInspector) -> None:
        schema = inspector.inspect()
        names = [t.name for t in schema.tables]
        assert "orders" in names
        assert "users" in names
        assert "order_items" in names

    def test_reads_columns(self, inspector: SchemaInspector) -> None:
        schema = inspector.inspect()
        orders = schema.get_table("orders")
        assert orders is not None
        assert orders.column_names == [
            "id", "order_no", "status", "amount", "created_at"
        ]

    def test_column_types_normalized(self, inspector: SchemaInspector) -> None:
        schema = inspector.inspect()
        orders = schema.get_table("orders")
        assert orders is not None
        order_no = orders.get_column("order_no")
        assert order_no is not None
        assert order_no.type == "varchar"
        assert order_no.raw_type == "VARCHAR(64)"

    def test_primary_key_detected(self, inspector: SchemaInspector) -> None:
        schema = inspector.inspect()
        orders = schema.get_table("orders")
        assert orders is not None
        assert orders.primary_key == ["id"]

    def test_required_columns(self, inspector: SchemaInspector) -> None:
        schema = inspector.inspect()
        orders = schema.get_table("orders")
        users = schema.get_table("users")
        assert orders is not None and users is not None
        assert [c.name for c in orders.required_columns] == ["order_no"]
        assert [c.name for c in users.required_columns] == ["name"]

    def test_reads_indexes(self, inspector: SchemaInspector) -> None:
        schema = inspector.inspect()
        orders = schema.get_table("orders")
        assert orders is not None
        idx_names = [i.name for i in orders.indexes]
        assert "idx_orders_no" in idx_names

        idx = next(i for i in orders.indexes if i.name == "idx_orders_no")
        assert idx.columns == ["order_no"]
        assert idx.unique is False

    def test_reads_foreign_keys(self, inspector: SchemaInspector) -> None:
        schema = inspector.inspect()
        items = schema.get_table("order_items")
        assert items is not None
        assert len(items.foreign_keys) == 1
        assert items.foreign_keys[0].referenced_table == "orders"

    def test_include_columns_false_skips_details(
        self, inspector: SchemaInspector
    ) -> None:
        schema = inspector.inspect(include_columns=False)
        assert len(schema.tables) == 3
        assert all(not t.columns for t in schema.tables)


# ---------------------------------------------------------------------------
# 搜索
# ---------------------------------------------------------------------------


class TestSearch:
    def test_find_table_by_name(self, inspector: SchemaInspector) -> None:
        schema = inspector.inspect()
        found = schema.find_tables("order")
        names = [t.name for t in found]
        assert "orders" in names
        assert "order_items" in names
        assert "users" not in names

    def test_find_columns_across_schema(self, inspector: SchemaInspector) -> None:
        schema = inspector.inspect()
        results = schema.find_columns("order_no")
        assert len(results) == 1
        table, column = results[0]
        assert table.name == "orders"
        assert column.name == "order_no"

    def test_get_column_case_insensitive(self, inspector: SchemaInspector) -> None:
        schema = inspector.inspect()
        orders = schema.get_table("ORDERS")
        assert orders is not None
        assert orders.get_column("ORDER_NO") is not None

    def test_get_table_missing_returns_none(self, inspector: SchemaInspector) -> None:
        schema = inspector.inspect()
        assert schema.get_table("no_such_table") is None


# ---------------------------------------------------------------------------
# 列属性
# ---------------------------------------------------------------------------


class TestColumnInfo:
    def test_max_length_from_varchar(self) -> None:
        column = ColumnInfo(name="x", type="varchar", raw_type="varchar(64)")
        assert column.max_length == 64

    def test_max_length_none_without_parens(self) -> None:
        column = ColumnInfo(name="x", type="text", raw_type="text")
        assert column.max_length is None

    def test_is_numeric(self) -> None:
        assert ColumnInfo(type="int").is_numeric
        assert ColumnInfo(type="decimal").is_numeric
        assert not ColumnInfo(type="varchar").is_numeric

    def test_is_required(self) -> None:
        assert ColumnInfo(nullable=False, default=None).is_required
        assert not ColumnInfo(nullable=False, default=0).is_required
        assert not ColumnInfo(nullable=True, default=None).is_required

    def test_is_datetime(self) -> None:
        assert ColumnInfo(type="datetime").is_datetime
        assert not ColumnInfo(type="int").is_datetime


# ---------------------------------------------------------------------------
# 缓存
# ---------------------------------------------------------------------------


class TestCache:
    def test_second_read_uses_cache(self, sqlite_file: Path, tmp_path: Path) -> None:
        """第二次读不应该再查库。"""
        cache_dir = tmp_path / "cache"
        insp = CountingInspector(
            {"type": "sqlite", "database": str(sqlite_file)},
            cache_ttl=3600,
            cache_dir=cache_dir,
        )
        try:
            first = insp.inspect()
            assert len(first.tables) == 3
            queries_after_first = insp.query_count
            assert queries_after_first > 0

            second = insp.inspect()
            assert len(second.tables) == 3
            assert sum(len(t.columns) for t in second.tables) == 11
            # 第二次一条查询都没发 —— 走的是缓存
            assert insp.query_count == queries_after_first
        finally:
            insp.close()

    def test_refresh_ignores_cache(self, sqlite_file: Path, tmp_path: Path) -> None:
        """refresh=True 时要重新查库。"""
        cache_dir = tmp_path / "cache"
        insp = CountingInspector(
            {"type": "sqlite", "database": str(sqlite_file)},
            cache_ttl=3600,
            cache_dir=cache_dir,
        )
        try:
            insp.inspect()
            queries_after_first = insp.query_count

            insp.inspect(refresh=True)
            assert insp.query_count > queries_after_first
        finally:
            insp.close()

    def test_expired_cache_triggers_reread(
        self, sqlite_file: Path, tmp_path: Path
    ) -> None:
        """缓存过期了要重新查库。"""
        cache_dir = tmp_path / "cache"
        insp = CountingInspector(
            {"type": "sqlite", "database": str(sqlite_file)},
            cache_ttl=3600,
            cache_dir=cache_dir,
        )
        try:
            insp.inspect()
            queries_after_first = insp.query_count

            # 把缓存文件的时间戳改老 —— 模拟过期
            cache_file = next(cache_dir.glob("*.json"))
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            data["fetched_at"] = time.time() - 7200
            cache_file.write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8"
            )

            insp.inspect()
            assert insp.query_count > queries_after_first
        finally:
            insp.close()

    def test_cache_roundtrip_preserves_data(
        self, sqlite_file: Path, tmp_path: Path
    ) -> None:
        cache_dir = tmp_path / "cache"
        insp = SchemaInspector(
            {"type": "sqlite", "database": str(sqlite_file)},
            cache_ttl=3600,
            cache_dir=cache_dir,
        )
        try:
            original = insp.inspect()
        finally:
            insp.close()

        cache_file = next(cache_dir.glob("*.json"))
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        assert data["database"]
        assert len(data["tables"]) == 3

        restored = SchemaInspector(
            {"type": "sqlite", "database": str(sqlite_file)},
            cache_ttl=3600,
            cache_dir=cache_dir,
        ).inspect()

        assert [t.name for t in restored.tables] == [
            t.name for t in original.tables
        ]
        orders = restored.get_table("orders")
        assert orders is not None
        assert orders.primary_key == ["id"]
        assert len(orders.indexes) == 1

    def test_cache_ttl_zero_disables_cache(
        self, sqlite_file: Path, tmp_path: Path
    ) -> None:
        """ttl=0 时完全不用缓存 —— 连缓存目录都不碰。"""
        cache_dir = tmp_path / "no_cache_dir"
        insp = SchemaInspector(
            {"type": "sqlite", "database": str(sqlite_file)},
            cache_ttl=0,
            cache_dir=cache_dir,
        )
        try:
            insp.inspect()
            # 缓存目录压根没被创建
            assert not cache_dir.exists()
        finally:
            insp.close()

    def test_partial_read_does_not_poison_cache(
        self, sqlite_file: Path, tmp_path: Path
    ) -> None:
        """只读表名之后，缓存里还得有字段明细。

        回归测试：以前 ``--list``（只读表名）会把完整缓存覆盖成残缺版，
        导致紧接着的完整读取拿到一堆空表。
        """
        cache_dir = tmp_path / "cache"
        insp = SchemaInspector(
            {"type": "sqlite", "database": str(sqlite_file)},
            cache_ttl=3600,
            cache_dir=cache_dir,
        )
        try:
            # 先读一次完整的（带字段明细）
            insp.inspect()

            # 再来一次只读表名的
            insp.inspect(include_columns=False)

            # 最后读完整的 —— 字段明细必须还在
            final = insp.inspect()
            orders = final.get_table("orders")
            assert orders is not None
            assert orders.column_names == [
                "id", "order_no", "status", "amount", "created_at"
            ]
        finally:
            insp.close()

    def test_partial_read_keeps_full_cache_on_disk(
        self, sqlite_file: Path, tmp_path: Path
    ) -> None:
        """只读表名不该把磁盘上的缓存换成残缺版。"""
        cache_dir = tmp_path / "cache"
        insp = SchemaInspector(
            {"type": "sqlite", "database": str(sqlite_file)},
            cache_ttl=3600,
            cache_dir=cache_dir,
        )
        try:
            insp.inspect()
            insp.inspect(include_columns=False)
        finally:
            insp.close()

        cache_file = next(cache_dir.glob("*.json"))
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        orders = next(t for t in data["tables"] if t["name"] == "orders")
        assert len(orders["columns"]) == 5


# ---------------------------------------------------------------------------
# MySQL 路径（用假客户端，不真连库）
# ---------------------------------------------------------------------------


class FakeClient:
    """假数据库客户端 —— 按 SQL 里的关键字返回预设结果。"""

    def __init__(self, tables: list[dict[str, Any]], columns: list[dict[str, Any]],
                 indexes: list[dict[str, Any]], fks: list[dict[str, Any]]):
        self._tables = tables
        self._columns = columns
        self._indexes = indexes
        self._fks = fks
        self.queries: list[str] = []
        self.params: list[Any] = []

    def query(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        self.queries.append(sql)
        self.params.append(params)
        if "information_schema.TABLES" in sql:
            return self._tables
        if "information_schema.COLUMNS" in sql:
            return self._columns
        if "information_schema.STATISTICS" in sql:
            return self._indexes
        if "information_schema.KEY_COLUMN_USAGE" in sql:
            return self._fks
        return []

    def close(self) -> None:
        pass


class TestMySQLPath:
    """MySQL 走 information_schema，这里只验证查询拼装和数据映射。"""

    def _inspector(self, fake: FakeClient) -> SchemaInspector:
        insp = SchemaInspector(
            {"type": "mysql", "host": "x", "database": "shop"},
            cache_ttl=0,
        )
        insp._client = fake  # 塞假客户端
        return insp

    def test_reads_schema_from_information_schema(self) -> None:
        fake = FakeClient(
            tables=[
                {"TABLE_NAME": "orders", "TABLE_COMMENT": "订单表",
                 "ENGINE": "InnoDB", "TABLE_ROWS": 128},
            ],
            columns=[
                {"TABLE_NAME": "orders", "COLUMN_NAME": "id",
                 "ORDINAL_POSITION": 1, "COLUMN_DEFAULT": None,
                 "IS_NULLABLE": "NO", "DATA_TYPE": "bigint",
                 "COLUMN_TYPE": "bigint(20)", "COLUMN_KEY": "PRI",
                 "EXTRA": "auto_increment", "COLUMN_COMMENT": "主键"},
                {"TABLE_NAME": "orders", "COLUMN_NAME": "order_no",
                 "ORDINAL_POSITION": 2, "COLUMN_DEFAULT": None,
                 "IS_NULLABLE": "NO", "DATA_TYPE": "varchar",
                 "COLUMN_TYPE": "varchar(64)", "COLUMN_KEY": "UNI",
                 "EXTRA": "", "COLUMN_COMMENT": "订单号"},
                {"TABLE_NAME": "orders", "COLUMN_NAME": "status",
                 "ORDINAL_POSITION": 3, "COLUMN_DEFAULT": "0",
                 "IS_NULLABLE": "YES", "DATA_TYPE": "tinyint",
                 "COLUMN_TYPE": "tinyint(4)", "COLUMN_KEY": "",
                 "EXTRA": "", "COLUMN_COMMENT": ""},
            ],
            indexes=[
                {"TABLE_NAME": "orders", "INDEX_NAME": "PRIMARY",
                 "NON_UNIQUE": 0, "SEQ_IN_INDEX": 1, "COLUMN_NAME": "id"},
            ],
            fks=[],
        )

        schema = self._inspector(fake).inspect()

        assert len(schema.tables) == 1
        table = schema.get_table("orders")
        assert table is not None
        assert table.comment == "订单表"
        assert table.engine == "InnoDB"
        assert table.approx_rows == 128

        assert table.column_names == ["id", "order_no", "status"]

        id_col = table.get_column("id")
        assert id_col is not None
        assert id_col.primary_key is True
        assert id_col.auto_increment is True
        assert id_col.is_required is True
        assert id_col.comment == "主键"

        no_col = table.get_column("order_no")
        assert no_col is not None
        assert no_col.unique is True
        assert no_col.max_length == 64

        status_col = table.get_column("status")
        assert status_col is not None
        assert status_col.default == "0"
        assert status_col.is_required is False

        assert table.primary_key == ["id"]

    def test_queries_use_parameter_binding(self) -> None:
        """库名要用占位符传，不能拼进 SQL 字符串。"""
        fake = FakeClient([], [], [], [])
        self._inspector(fake).inspect()

        assert fake.queries, "应该至少发一条查询"
        for sql in fake.queries:
            assert "information_schema" in sql
            # 库名不能出现在 SQL 里（那是拼字符串，注入风险）
            assert "shop" not in sql
            # 库名是当参数传进去的
            assert "%s" in sql

        # 每条查询都带了库名这个参数
        for params in fake.params:
            assert params == ("shop",)

    def test_missing_database_name_raises(self) -> None:
        insp = SchemaInspector({"type": "mysql", "host": "x"}, cache_ttl=0)
        with pytest.raises(Exception) as exc:
            insp.inspect()
        assert "database" in str(exc.value).lower()

    def test_unsupported_driver_raises(self) -> None:
        insp = SchemaInspector({"type": "oracle", "database": "x"}, cache_ttl=0)
        with pytest.raises(Exception) as exc:
            insp.inspect()
        assert "oracle" in str(exc.value)


# ---------------------------------------------------------------------------
# 手工构造的模型
# ---------------------------------------------------------------------------


class TestModels:
    def test_schema_find_columns_returns_pairs(self) -> None:
        schema = DatabaseSchema(
            database="shop",
            tables=[
                TableInfo(
                    name="orders",
                    columns=[
                        ColumnInfo(name="order_no", type="varchar"),
                        ColumnInfo(name="amount", type="decimal"),
                    ],
                ),
                TableInfo(
                    name="items",
                    columns=[ColumnInfo(name="order_no", type="varchar")],
                ),
            ],
        )
        results = schema.find_columns("order_no")
        assert len(results) == 2
        assert {t.name for t, _ in results} == {"orders", "items"}

    def test_table_to_dict_is_json_serializable(self) -> None:
        table = TableInfo(
            name="orders",
            columns=[ColumnInfo(name="id", type="int", primary_key=True)],
        )
        data = table.to_dict()
        json.dumps(data)  # 不报错就行
        assert data["name"] == "orders"
        assert data["columns"][0]["primary_key"] is True
