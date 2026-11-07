"""Excel 断言单元格解析的测试。

Excel 里一个断言写成「类型 | 字段 | 期望值」三格，
这里验证各种新断言类型在 Excel 里也能正确解析。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.excel.cells import parse_assert_cell  # noqa: E402


def test_simple_equal():
    asserts = parse_assert_cell("equal | code | 200")
    assert asserts == [{"type": "equal", "field": "code", "expected": 200}]


def test_bool_expected():
    asserts = parse_assert_cell("equal | success | true")
    assert asserts[0]["expected"] is True


def test_number_string_becomes_number():
    """数字字符串要转成数字，否则 "10.005" 和 10.005 比不相等。"""
    asserts = parse_assert_cell("approx_equal | result.amount | 10.005 | 0.01")
    item = asserts[0]
    assert item["expected"] == 10.005
    assert item["tolerance"] == 0.01


def test_subset_with_json_object():
    asserts = parse_assert_cell('subset | result.user | {"name": "张三", "id": 88}')
    item = asserts[0]
    assert item["type"] == "subset"
    assert item["expected"] == {"name": "张三", "id": 88}


def test_elapsed_time_with_operator_dict():
    asserts = parse_assert_cell('elapsed_time |  | {"lte": 3000}')
    item = asserts[0]
    assert item["type"] == "elapsed_time"
    assert item["expected"] == {"lte": 3000}


def test_status_code():
    asserts = parse_assert_cell("status_code |  | 200")
    assert asserts[0]["expected"] == 200


def test_regex_pattern_stays_string():
    asserts = parse_assert_cell("matches | result.orderNo | ^SO\\d{14}$")
    assert asserts[0]["expected"] == r"^SO\d{14}$"


def test_container_still_works():
    asserts = parse_assert_cell("in | result.status |  | [1, 2, 3]")
    assert asserts[0]["container"] == [1, 2, 3]


def test_multiple_lines():
    cell = "equal | code | 200\nis not None | result.token"
    asserts = parse_assert_cell(cell)
    assert len(asserts) == 2
    assert asserts[1]["field"] == "result.token"


def test_json_array_form():
    cell = '[{"type": "equal", "field": "code", "expected": 200}]'
    asserts = parse_assert_cell(cell)
    assert asserts[0]["field"] == "code"


def test_empty_cell():
    assert parse_assert_cell("") == []
    assert parse_assert_cell(None) == []
