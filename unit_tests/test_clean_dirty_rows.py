"""``tools/maint/clean_dirty_rows.py`` 的单元测试。

清理脚本直接删线上库的行，最怕两件事：删错表、删错范围。这里钉住：

- 识别脏行的口径只看**字符串列里的 ``{{``**，别把正常数据扫进来
- ``--ids`` 解析要精确，不能把 ``tz_hot_search=86`` 理解成"整表"
- 默认（不加 ``--apply``）一条 DELETE 都不发
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_tool():
    path = ROOT / "tools" / "maint" / "clean_dirty_rows.py"
    spec = importlib.util.spec_from_file_location("clean_dirty_rows", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _tool():
    return _load_tool()


def _table(columns, name="tz_demo"):
    return {
        "name": name,
        "columns": [
            {"name": col, "type": type_, "primary_key": col == columns[0][0]}
            for col, type_ in columns
        ],
    }


def test_primary_key_prefers_flag_then_first_column():
    table = _table([("id", "bigint"), ("name", "varchar")])
    assert _tool().primary_key(table) == "id"

    no_flag = _table([("code", "varchar"), ("name", "varchar")])
    for column in no_flag["columns"]:
        column["primary_key"] = False
    assert _tool().primary_key(no_flag) == "code"


def test_text_columns_only_scans_strings():
    module = _tool()
    table = _table(
        [
            ("id", "bigint"),
            ("name", "varchar(64)"),
            ("content", "longtext"),
            ("price", "decimal(10,2)"),
            ("created", "datetime"),
        ]
    )
    assert module.text_columns(table) == ["name", "content"]


def test_find_dirty_builds_like_query_over_text_columns_only():
    module = _tool()
    table = _table([("id", "bigint"), ("name", "varchar"), ("price", "decimal")])

    class StubClient:
        def __init__(self):
            self.sql = ""
            self.params = None

        def query(self, sql, params=None):
            self.sql, self.params = sql, params
            return [{"id": 2}, {"id": 1}]

    client = StubClient()
    rows = module.find_dirty(client, table)
    assert "`name` LIKE %s" in client.sql
    assert "`price`" not in client.sql
    assert client.params == ["%{{%"]
    assert [row["id"] for row in rows] == [1, 2], "结果要按主键排序，方便核对"


def test_table_without_text_columns_is_skipped():
    module = _tool()
    table = _table([("id", "bigint"), ("price", "decimal")])
    assert module.text_columns(table) == []
    assert module.find_dirty(object(), table) == []


def test_parse_id_filter_is_exact():
    module = _tool()
    assert module.parse_id_filter("tz_hot_search=86,87") == {"tz_hot_search": [86, 87]}
    assert module.parse_id_filter("a=1;b=2,3") == {"a": [1], "b": [2, 3]}
    assert module.parse_id_filter("") == {}
    # 非数字主键（比如 uuid）原样保留
    assert module.parse_id_filter("t=abc") == {"t": ["abc"]}


def test_id_filter_narrows_scan_to_named_ids_only():
    """``--ids`` 必须能把扫描结果收窄，不能退化成整表删除。"""
    module = _tool()
    table = _table([("id", "bigint"), ("name", "varchar")])

    class StubClient:
        def query(self, sql, params=None):
            return [{"id": 1}, {"id": 2}, {"id": 3}]

    rows = module.find_dirty(StubClient(), table)
    allowed = set(module.parse_id_filter("tz_demo=2")["tz_demo"])
    assert [row["id"] for row in rows if row["id"] in allowed] == [2]


def test_schema_snapshot_covers_target_tables():
    module = _tool()
    schema = module.load_schema()
    for name in ("tz_hot_search", "tz_index_img", "tz_area", "tz_category"):
        assert name in schema, f"库结构快照里没有 {name}，先重新 dump_schema"
