"""断言处理的测试。

不联网、不调接口 —— 直接喂响应数据，验证每种断言的对错。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.assertions.assert_handler import AssertHandler  # noqa: E402

# 一份典型的接口响应
RESPONSE = {
    "code": 200,
    "success": True,
    "message": "操作成功",
    "result": {
        "orderNo": "SO20240101000123",
        "amount": 10.005,
        "status": 1,
        "isPaid": True,
        "remark": "",
        "tags": [],
        "user": {"id": 88, "name": "张三"},
        "items": [{"sku": "A1", "qty": 2}],
        "nullable": None,
    },
}

META = {
    "status_code": 200,
    "headers": {"content-type": "application/json; charset=utf-8", "x-request-id": "abc123"},
    "elapsed_ms": 128,
}


@pytest.fixture
def handler():
    return AssertHandler()


def run(handler, asserts, response=None, meta=None):
    handler.handle_assertion(asserts, response if response is not None else RESPONSE, meta=meta)


# --------------------------------------------------------------------------
# 老断言（回归）
# --------------------------------------------------------------------------


def test_equal(handler):
    run(handler, [{"type": "equal", "field": "code", "expected": 200}])


def test_equal_fails(handler):
    with pytest.raises(AssertionError, match="期望等于"):
        run(handler, [{"type": "equal", "field": "code", "expected": 500}])


def test_is_not_none(handler):
    run(handler, [{"type": "is not None", "field": "result.orderNo"}])


def test_contains(handler):
    run(handler, [{"type": "contains", "field": "message", "expected": "成功"}])


def test_in(handler):
    run(handler, [{"type": "in", "field": "result.status", "container": [1, 2, 3]}])


def test_nested_field(handler):
    run(handler, [{"type": "equal", "field": "result.user.name", "expected": "张三"}])


def test_array_index_field(handler):
    run(handler, [{"type": "equal", "field": "result.items[0].sku", "expected": "A1"}])


# --------------------------------------------------------------------------
# 新增：响应元信息
# --------------------------------------------------------------------------


def test_status_code(handler):
    run(handler, [{"type": "status_code", "expected": 200}], meta=META)


def test_status_code_fails(handler):
    with pytest.raises(AssertionError):
        run(handler, [{"type": "status_code", "expected": 500}], meta=META)


def test_status_code_with_operator(handler):
    """期望值写成字典 —— 支持 gt / lte 这类比较。"""
    run(handler, [{"type": "status_code", "expected": {"lte": 299}}], meta=META)
    with pytest.raises(AssertionError):
        run(handler, [{"type": "status_code", "expected": {"gte": 400}}], meta=META)


def test_status_code_without_meta_raises(handler):
    with pytest.raises(ValueError, match="状态码"):
        run(handler, [{"type": "status_code", "expected": 200}])


def test_response_header(handler):
    run(handler, [{"type": "response_header", "field": "Content-Type",
                   "expected": "application/json; charset=utf-8"}], meta=META)


def test_response_header_missing(handler):
    with pytest.raises(AssertionError, match="不存在"):
        run(handler, [{"type": "response_header", "field": "X-Nope", "expected": "x"}], meta=META)


def test_elapsed_time(handler):
    run(handler, [{"type": "elapsed_time", "expected": {"lte": 1000}}], meta=META)


def test_elapsed_time_exact(handler):
    run(handler, [{"type": "elapsed_time", "expected": 128}], meta=META)


def test_dollar_status_shorthand(handler):
    """``$status`` 这种短写法。"""
    run(handler, [{"type": "equal", "field": "$status", "expected": 200}], meta=META)


def test_dollar_header_shorthand(handler):
    run(handler, [{"type": "contains", "field": "$header.x-request-id", "expected": "abc"}],
        meta=META)


def test_dollar_elapsed_shorthand(handler):
    run(handler, [{"type": "lt", "field": "$elapsed", "expected": 1000}], meta=META)


# --------------------------------------------------------------------------
# 新增：正则 / 空值 / 布尔 / 大小写 / 结尾
# --------------------------------------------------------------------------


def test_matches(handler):
    run(handler, [{"type": "matches", "field": "result.orderNo", "expected": r"^SO\d{14}$"}])


def test_matches_fails(handler):
    with pytest.raises(AssertionError, match="期望匹配"):
        run(handler, [{"type": "matches", "field": "result.orderNo", "expected": r"^XX"}])


def test_not_matches(handler):
    run(handler, [{"type": "not_matches", "field": "result.orderNo", "expected": r"^XX"}])


def test_matches_bad_pattern(handler):
    with pytest.raises(ValueError, match="正则"):
        run(handler, [{"type": "matches", "field": "result.orderNo", "expected": "["}])


def test_empty(handler):
    run(handler, [{"type": "empty", "field": "result.remark"}])
    run(handler, [{"type": "empty", "field": "result.tags"}])


def test_not_empty(handler):
    with pytest.raises(AssertionError):
        run(handler, [{"type": "not_empty", "field": "result.remark"}])
    run(handler, [{"type": "not_empty", "field": "result.orderNo"}])


def test_empty_distinguishes_null_and_missing(handler):
    """空值断言：字段不存在也算空；但 exists 断言能区分两者。"""
    run(handler, [{"type": "empty", "field": "result.notExist"}])
    with pytest.raises(AssertionError, match="不存在"):
        run(handler, [{"type": "exists", "field": "result.notExist"}])
    run(handler, [{"type": "exists", "field": "result.nullable"}])


def test_is_true_false(handler):
    run(handler, [{"type": "is_true", "field": "result.isPaid"}])
    run(handler, [{"type": "is_true", "field": "success"}])
    with pytest.raises(AssertionError):
        run(handler, [{"type": "is_false", "field": "result.isPaid"}])


def test_equal_ignore_case(handler):
    run(handler, [{"type": "iequal", "field": "message", "expected": "操作成功"}])
    with pytest.raises(AssertionError):
        run(handler, [{"type": "iequal", "field": "message", "expected": "不对"}])


def test_ends_with(handler):
    run(handler, [{"type": "ends_with", "field": "result.orderNo", "expected": "0123"}])


# --------------------------------------------------------------------------
# 新增：近似相等 / 部分匹配
# --------------------------------------------------------------------------


def test_approx_equal(handler):
    run(handler, [{"type": "approx_equal", "field": "result.amount", "expected": 10.0}])


def test_approx_equal_with_tolerance(handler):
    run(handler, [{"type": "approx_equal", "field": "result.amount",
                   "expected": 10.0, "tolerance": 0.01}])


def test_approx_equal_fails_when_out_of_range(handler):
    with pytest.raises(AssertionError, match="约等于"):
        run(handler, [{"type": "approx_equal", "field": "result.amount",
                       "expected": 20.0, "tolerance": 0.01}])


def test_subset(handler):
    """只比关心的字段，服务端多返回的忽略。"""
    run(handler, [{"type": "subset", "field": "result",
                   "expected": {"status": 1, "user": {"name": "张三"}}}])


def test_subset_fails_on_mismatch(handler):
    with pytest.raises(AssertionError, match="期望"):
        run(handler, [{"type": "subset", "field": "result",
                       "expected": {"status": 999}}])


def test_subset_ignores_extra_fields(handler):
    """响应里有额外的字段不该导致失败。"""
    run(handler, [{"type": "subset", "field": "result",
                   "expected": {"orderNo": "SO20240101000123"}}])


# --------------------------------------------------------------------------
# 数据库断言（用假连接，不连真库）
# --------------------------------------------------------------------------


class FakeCursor:
    def __init__(self, rows, description):
        self._rows = rows
        self._description = description
        self.executed = None

    def execute(self, sql):
        self.executed = sql

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows

    @property
    def description(self):
        return self._description

    @property
    def rowcount(self):
        return len(self._rows)

    def close(self):
        pass


class FakeConnection:
    def __init__(self, rows, description=None):
        self._rows = rows
        self._description = description or [("col0",)]
        self.queries = []

    def cursor(self):
        cur = FakeCursor(self._rows, self._description)
        self.queries.append(cur)
        return cur

    def close(self):
        pass


def test_mysql_query(handler):
    conn = FakeConnection([(2,)])
    handler._handle_mysql(
        {"type": "mysql_query", "query": "SELECT status FROM orders", "expected": 2},
        conn,
    )


def test_mysql_query_count(handler):
    conn = FakeConnection([(1,), (2,), (3,)])
    handler._handle_mysql(
        {"type": "mysql_query_count", "query": "SELECT id FROM orders", "expected": 3},
        conn,
    )


def test_mysql_query_count_fails(handler):
    conn = FakeConnection([(1,)])
    with pytest.raises(AssertionError, match="行"):
        handler._handle_mysql(
            {"type": "mysql_query_count", "query": "SELECT id FROM orders", "expected": 3},
            conn,
        )


def test_mysql_query_gte(handler):
    conn = FakeConnection([(5,)])
    handler._handle_mysql(
        {"type": "mysql_query_gte", "query": "SELECT n FROM t", "expected": 3},
        conn,
    )


def test_mysql_query_in(handler):
    conn = FakeConnection([(1,)])
    handler._handle_mysql(
        {"type": "mysql_query_in", "query": "SELECT s FROM t", "container": [1, 2]},
        conn,
    )


def test_mysql_query_by_column_name(handler):
    """指定列名 —— 不再只能读第一列。"""
    conn = FakeConnection([(88, 1)], description=[("id",), ("status",)])
    handler._handle_mysql(
        {"type": "mysql_query", "query": "SELECT id, status FROM users",
         "column": "status", "expected": 1},
        conn,
    )


def test_mysql_query_by_column_index(handler):
    conn = FakeConnection([(88, 1)], description=[("id",), ("status",)])
    handler._handle_mysql(
        {"type": "mysql_query", "query": "SELECT id, status FROM users",
         "column": 1, "expected": 1},
        conn,
    )


def test_mysql_unknown_column_raises(handler):
    conn = FakeConnection([(88, 1)], description=[("id",), ("status",)])
    with pytest.raises(ValueError, match="没有"):
        handler._handle_mysql(
            {"type": "mysql_query", "query": "SELECT id, status FROM users",
             "column": "nope", "expected": 1},
            conn,
        )


# --------------------------------------------------------------------------
# 错误处理
# --------------------------------------------------------------------------


def test_unknown_assert_type_raises(handler):
    with pytest.raises(ValueError, match="未知的断言类型"):
        run(handler, [{"type": "not_a_real_type", "field": "code", "expected": 200}])


def test_missing_field_raises(handler):
    with pytest.raises(ValueError, match="缺少 field"):
        run(handler, [{"type": "equal", "expected": 200}])


def test_in_without_container_raises(handler):
    with pytest.raises(ValueError, match="container"):
        run(handler, [{"type": "in", "field": "result.status"}])


def test_empty_asserts_is_noop(handler):
    handler.handle_assertion([], RESPONSE)
    handler.handle_assertion(None, RESPONSE)


# --------------------------------------------------------------------------
# 字段是否存在
# --------------------------------------------------------------------------


def test_exists_passes_for_null_value(handler):
    """``{"nullable": null}`` 里字段是存在的 —— 和 is not None 不等价。"""
    run(handler, [{"type": "exists", "field": "result.nullable"}])


def test_exists_fails_for_missing_field(handler):
    with pytest.raises(AssertionError, match="字段不存在"):
        run(handler, [{"type": "exists", "field": "result.deletedAt"}])


def test_exists_error_lists_available_fields(handler):
    """报错要说清是哪一层断的、那层有哪些字段。"""
    with pytest.raises(AssertionError) as exc:
        run(handler, [{"type": "exists", "field": "result.user.mobile"}])

    message = str(exc.value)
    assert "result.user" in message
    assert "id" in message and "name" in message


def test_not_exists_passes_for_missing_field(handler):
    run(handler, [{"type": "not exists", "field": "result.deletedAt"}])


def test_not_exists_fails_for_null_value(handler):
    """值为 null 但字段存在 —— not exists 必须失败。"""
    with pytest.raises(AssertionError, match="实际存在"):
        run(handler, [{"type": "not exists", "field": "result.nullable"}])


def test_exists_supports_meta_field(handler):
    run(handler, [{"type": "exists", "field": "$header.X-Request-Id"}], meta=META)
    with pytest.raises(AssertionError, match="字段不存在"):
        run(handler, [{"type": "exists", "field": "$header.X-Missing"}], meta=META)


def test_exists_supports_array_index(handler):
    run(handler, [{"type": "exists", "field": "result.items[0].sku"}])
    with pytest.raises(AssertionError, match="只有 1 个元素"):
        run(handler, [{"type": "exists", "field": "result.items[1].sku"}])


# --------------------------------------------------------------------------
# 数据库断言（新版）
# --------------------------------------------------------------------------


class _FakeDbClient:
    def __init__(self, row):
        self.row = row
        self.queries = []

    def query_one(self, sql, params=None):
        self.queries.append((sql, params))
        return self.row

    def query(self, sql, params=None):
        self.queries.append((sql, params))
        return [self.row] if self.row else []


class _FakeManager:
    def __init__(self, row):
        self.client = _FakeDbClient(row)

    def db(self, alias="default"):
        assert alias == "default"
        return self.client


def test_db_query_passes_params(handler):
    manager = _FakeManager({"status": 1})
    handler._handle_db_assert(
        manager,
        {"query": "SELECT status FROM t WHERE id = %s", "params": [7]},
        "db_query",
        1,
    )
    assert manager.client.queries == [("SELECT status FROM t WHERE id = %s", [7])]


def test_db_query_not_exists_passes(handler):
    handler._handle_db_assert(
        _FakeManager(None),
        {"query": "SELECT id FROM t WHERE id = %s", "params": [7]},
        "db_query_not_exists",
        None,
    )


def test_db_query_not_exists_fails_when_row_found(handler):
    with pytest.raises(AssertionError, match="期望没有结果"):
        handler._handle_db_assert(
            _FakeManager({"id": 7}),
            {"query": "SELECT id FROM t WHERE id = %s", "params": [7]},
            "db_query_not_exists",
            None,
        )


def test_db_query_gt_and_lt(handler):
    handler._handle_db_assert(
        _FakeManager({"n": 5}), {"query": "SELECT n FROM t"}, "db_query_gt", 4
    )
    handler._handle_db_assert(
        _FakeManager({"n": 5}), {"query": "SELECT n FROM t"}, "db_query_lt", 6
    )
    with pytest.raises(AssertionError, match="期望大于"):
        handler._handle_db_assert(
            _FakeManager({"n": 5}), {"query": "SELECT n FROM t"}, "db_query_gt", 5
        )


def test_mysql_query_not_exists(handler):
    conn = FakeConnection([])  # fetchone 返回 None
    handler._handle_mysql(
        {"type": "mysql_query_not_exists", "query": "SELECT id FROM t WHERE id = 1"},
        conn,
    )
