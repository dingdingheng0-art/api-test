"""扁平格式 Excel（一张大表 + JSON 单元格）的解析测试。

对应文件：``utils/excel/flat_parser.py``
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.excel.flat_parser import (  # noqa: E402
    _jsonpath_to_field,
    _parse_asserts,
    _parse_headers,
    _parse_param_data,
    _parse_request_data,
)
from utils.excel.reader import ExcelCaseError  # noqa: E402


# --------------------------------------------------------------------------
# 请求头
# --------------------------------------------------------------------------


def test_empty_header_values_are_dropped():
    """空值的头要丢掉 —— 不然会把框架自动填的 token 覆盖成空。"""
    headers = _parse_headers('{"Accept": "", "accesstoken": "abc"}', {})
    assert headers == {"accesstoken": "abc"}


def test_access_token_header_alias():
    headers = _parse_headers('{"accessToken": "abc"}', {})
    assert headers == {"accesstoken": "abc"}


def test_global_headers_are_lowest_priority():
    headers = _parse_headers('{"Accept": "text/html"}', {"Accept": "application/json"})
    assert headers["Accept"] == "text/html"


def test_global_headers_fill_missing_ones():
    headers = _parse_headers('{"X-A": "1"}', {"Accept": "application/json"})
    assert headers == {"X-A": "1", "Accept": "application/json"}


# --------------------------------------------------------------------------
# 请求数据
# --------------------------------------------------------------------------


def test_get_puts_data_into_query_params():
    params, body = _parse_request_data('{"searchType": "LAST_SEVEN"}', "GET")
    assert params == {"searchType": "LAST_SEVEN"}
    assert body is None


def test_post_puts_data_into_body():
    params, body = _parse_request_data('{"name": "abc"}', "POST")
    assert params == {}
    assert body == {"name": "abc"}


def test_bullet_prefix_is_stripped():
    """文档表格里的 ``»`` 是层级标记，不是真的键名。"""
    _, body = _parse_request_data('{"» app_id": 1}', "POST")
    assert body == {"app_id": 1}


def test_placeholder_is_converted_to_framework_style():
    """``${key}`` 要换成框架认识的 ``{{key}}``。"""
    params, _ = _parse_request_data('{"searchType": "${searchType}"}', "GET")
    assert params == {"searchType": "{{searchType}}"}


# --------------------------------------------------------------------------
# 断言
# --------------------------------------------------------------------------


def test_status_code_assertion():
    asserts = _parse_asserts('[{"check": "status_code", "comparator": "==", "expect": 200}]')
    assert asserts == [{"type": "status_code", "expected": 200}]


def test_body_assertion_with_jsonpath():
    asserts = _parse_asserts(
        '[{"check": "body", "jsonpath": "$.success", "comparator": "==", "expect": true}]'
    )
    assert asserts == [{"type": "equal", "field": "success", "expected": True}]


def test_header_assertion():
    asserts = _parse_asserts(
        '[{"check": "header", "key": "Content-Type", "comparator": "contains", "expect": "json"}]'
    )
    assert asserts[0]["type"] == "response_header"
    assert asserts[0]["field"] == "Content-Type"


def test_time_assertion_seconds_becomes_milliseconds():
    asserts = _parse_asserts('[{"check": "time", "comparator": "<", "expect": 1.0}]')
    assert asserts == [{"type": "elapsed_time", "expected": 1000.0}]


def test_in_assertion_uses_container():
    asserts = _parse_asserts(
        '[{"check": "body", "jsonpath": "$.status", "comparator": "in", "expect": [1, 2]}]'
    )
    assert asserts[0]["type"] == "in"
    assert asserts[0]["container"] == [1, 2]


def test_is_null_comparator():
    asserts = _parse_asserts(
        '[{"check": "body", "jsonpath": "$.data", "comparator": "is_null"}]'
    )
    assert asserts[0]["type"] == "is_none"


def test_len_comparator():
    asserts = _parse_asserts(
        '[{"check": "body", "jsonpath": "$.items", "comparator": "len>", "expect": 2}]'
    )
    assert asserts[0]["type"] == "length_gt"


def test_unknown_check_raises():
    with pytest.raises(ExcelCaseError):
        _parse_asserts('[{"check": "magic", "comparator": "==", "expect": 1}]')


def test_unknown_comparator_raises():
    with pytest.raises(ExcelCaseError):
        _parse_asserts(
            '[{"check": "body", "jsonpath": "$.a", "comparator": "~~", "expect": 1}]'
        )


# --------------------------------------------------------------------------
# jsonpath 转换
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("$.success", "success"),
        ("$.code", "code"),
        ("$.data.list[0].id", "data.list[0].id"),
        ("$['result']['name']", "result.name"),
        ("$", ""),
    ],
)
def test_jsonpath_to_field(path, expected):
    assert _jsonpath_to_field(path) == expected


# --------------------------------------------------------------------------
# 用例状态：Excel 是手工维护的源，默认就该是"要跑的"
# --------------------------------------------------------------------------


def _build_flat_workbook(tmp_path: Path, status_value: str | None):
    """造一个最小的扁平格式 Excel。"""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "接口测试用例"
    ws.append(
        [
            "case_id", "模块", "标签", "用例名称", "接口", "请求方法",
            "用例状态",
        ]
    )
    row = ["E-001", "demo", "demo", "测试用例", "/demo", "GET"]
    if status_value is not None:
        row.append(status_value)
    ws.append(row)

    path = tmp_path / "case.xlsx"
    wb.save(path)
    return path


def test_excel_case_defaults_to_debugged(tmp_path: Path):
    """没写「用例状态」列 —— 默认按 debugged 处理（要跑）。

    不按 draft 的话，整个 Excel 用例集会因为没标状态被静默跳过。
    """
    from utils.excel.flat_parser import parse_flat_cases
    from utils.excel.reader import read_workbook

    path = _build_flat_workbook(tmp_path, None)
    cases = parse_flat_cases(read_workbook(path))

    assert len(cases) == 1
    assert cases[0]["testcase"]["status"] == "debugged"


def test_excel_case_keeps_explicit_status(tmp_path: Path):
    """写了状态就按写的来 —— disabled / broken 的用例要能跳过。"""
    from utils.excel.flat_parser import parse_flat_cases
    from utils.excel.reader import read_workbook

    path = _build_flat_workbook(tmp_path, "disabled")
    cases = parse_flat_cases(read_workbook(path))

    assert cases[0]["testcase"]["status"] == "disabled"


def test_allow_dangerous_column_is_parsed(tmp_path: Path):
    """「允许危险删除」列填 true —— 真删用例要能通过安全闸。"""
    from openpyxl import Workbook

    from utils.excel.flat_parser import parse_flat_cases
    from utils.excel.reader import read_workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "接口测试用例"
    ws.append(
        [
            "case_id", "用例名称", "接口", "请求方法",
            "允许危险删除",
        ]
    )
    ws.append(["E-001", "删测试数据", "/sys/menu/{{id}}", "DELETE", "true"])
    ws.append(["E-002", "普通用例", "/sys/menu/info/1", "GET", ""])
    path = tmp_path / "case.xlsx"
    wb.save(path)

    cases = parse_flat_cases(read_workbook(path))

    assert cases[0]["testcase"]["allow_dangerous"] is True
    assert "allow_dangerous" not in cases[1]["testcase"]


def test_four_table_excel_case_defaults_to_debugged(tmp_path: Path):
    """四表格式也一样 —— 没标状态默认要跑。"""
    from openpyxl import Workbook

    from utils.excel.parser import parse_case_rows
    from utils.excel.reader import _rows, read_workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "测试用例"
    ws.append(["用例名", "关联接口", "用例说明"])
    ws.append(["测试用例", "api1", "说明"])
    path = tmp_path / "case.xlsx"
    wb.save(path)

    rows = _rows(read_workbook(path)["测试用例"])
    apis = {"api1": {"id": "api1", "path": "/demo", "method": "GET"}}
    cases = parse_case_rows(rows, apis=apis, data_groups={})

    assert cases[0]["testcase"]["status"] == "debugged"


# --------------------------------------------------------------------------
# 参数化
# --------------------------------------------------------------------------


def test_param_data_placeholders_converted():
    groups = _parse_param_data('[{"searchType": "${searchType}", "page": 10}]')
    assert groups == [
        {"name": "searchType=${searchType}-page=10", "data": {"searchType": "{{searchType}}", "page": 10}}
    ]


def test_empty_param_data():
    assert _parse_param_data("") == []
    assert _parse_param_data(None) == []


# --------------------------------------------------------------------------
# 字段存在 / 查库断言
# --------------------------------------------------------------------------


def test_exists_assertion():
    asserts = _parse_asserts(
        '[{"check": "body", "jsonpath": "$.data.id", "comparator": "exists"}]'
    )
    assert asserts == [{"type": "exists", "field": "data.id", "expected": None}]


def test_not_exists_assertion():
    asserts = _parse_asserts(
        '[{"check": "body", "jsonpath": "$.data.deletedAt", "comparator": "not_exists"}]'
    )
    assert asserts == [{"type": "not_exists", "field": "data.deletedAt", "expected": None}]


def test_db_assertion_equal():
    asserts = _parse_asserts(
        '[{"check": "db", "sql": "SELECT status FROM t WHERE id = %s",'
        ' "params": [7], "comparator": "==", "expect": 1}]'
    )
    assert asserts == [
        {"type": "db_query", "query": "SELECT status FROM t WHERE id = %s",
         "params": [7], "expected": 1}
    ]


def test_db_assertion_not_exists():
    asserts = _parse_asserts(
        '[{"check": "db", "query": "SELECT id FROM t WHERE id = 7",'
        ' "comparator": "not_exists"}]'
    )
    assert asserts == [{"type": "db_query_not_exists", "query": "SELECT id FROM t WHERE id = 7"}]


def test_db_assertion_in_uses_container():
    asserts = _parse_asserts(
        '[{"check": "db", "sql": "SELECT status FROM t",'
        ' "comparator": "in", "expect": [1, 2]}]'
    )
    assert asserts[0]["type"] == "db_query_in"
    assert asserts[0]["container"] == [1, 2]


def test_db_assertion_comparator_order():
    expected = {
        ">": "db_query_gt",
        ">=": "db_query_gte",
        "<": "db_query_lt",
        "<=": "db_query_lte",
    }
    for comparator, want in expected.items():
        asserts = _parse_asserts(
            f'[{{"check": "db", "sql": "SELECT n FROM t",'
            f' "comparator": "{comparator}", "expect": 1}}]'
        )
        assert asserts[0]["type"] == want, comparator


def test_db_assertion_requires_sql():
    with pytest.raises(ExcelCaseError, match="缺少 sql"):
        _parse_asserts('[{"check": "db", "comparator": "==", "expect": 1}]')


def test_db_assertion_rejects_unsupported_comparator():
    with pytest.raises(ExcelCaseError, match="不支持比较符"):
        _parse_asserts(
            '[{"check": "db", "sql": "SELECT n FROM t",'
            ' "comparator": "contains", "expect": 1}]'
        )


def test_pipe_assert_cell_supports_exists_and_db():
    from utils.excel.cells import parse_assert_cell

    asserts = parse_assert_cell(
        "exists | data.id\n"
        "not exists | data.deletedAt\n"
        "db_query | SELECT status FROM t WHERE id = %s | 1 | [7]\n"
        "db_query_not_exists | SELECT id FROM t WHERE id = %s | | [7]\n"
        "db_query_in | SELECT status FROM t | | [1, 2]"
    )

    assert asserts[0] == {"type": "exists", "field": "data.id"}
    assert asserts[1] == {"type": "not exists", "field": "data.deletedAt"}
    assert asserts[2]["query"] == "SELECT status FROM t WHERE id = %s"
    assert asserts[2]["params"] == [7]
    assert asserts[2]["expected"] == 1
    assert asserts[3]["type"] == "db_query_not_exists"
    assert asserts[3]["params"] == [7]
    assert asserts[4]["type"] == "db_query_in"
    assert asserts[4]["container"] == [1, 2]
