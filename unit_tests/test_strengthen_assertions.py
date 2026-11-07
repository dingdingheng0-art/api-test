"""``tools/maint/strengthen_assertions.py`` 的单元测试。

这个工具会批量改 Excel 里的断言，改错了整批用例都会红，
所以规则要单独钉住：

- 正常用例补业务码 + ``data`` 字段断言
- 错误路径用例（``success == false`` / ``code != 00000`` / ``data is_null``）不动
- 导出类用例（返回文件）不动
- 验证码用例走 ``data.repCode``，没有 ``code`` 字段
- 已经加强过的用例不重复加
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_module():
    path = ROOT / "tools" / "maint" / "strengthen_assertions.py"
    spec = importlib.util.spec_from_file_location("strengthen_assertions", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def tool():
    return _load_module()


@pytest.fixture(scope="module")
def spec(tool):
    spec_file = ROOT / ".cache" / "apidoc" / "openapi.json"
    if not spec_file.exists():
        pytest.skip("没有接口文档快照 .cache/apidoc/openapi.json")
    return tool.Spec(spec_file)


def _paths(added: list[dict]) -> list[str]:
    return [f"{item['jsonpath']}:{item['comparator']}" for item in added]


def test_page_shape_gets_paging_assertions(tool, spec):
    asserts = [{"check": "status_code", "comparator": "==", "expect": 200}]
    added = tool.add_main_assertions(
        asserts, "/admin/area/page", "GET", spec.data_shape("/admin/area/page", "GET")
    )
    paths = _paths(added)
    assert "$.code:==" in paths
    assert "$.data.records:exists" in paths
    assert "$.data.total:>=" in paths
    assert "$.data.current:>=" in paths
    # ``pages`` 标了 deprecated，不该被当成必检项
    assert "$.data.pages:exists" not in paths


def test_detail_shape_gets_field_assertions(tool, spec):
    asserts = []
    added = tool.add_main_assertions(
        asserts, "/sys/config/info/1", "GET", spec.data_shape("/sys/config/info/1", "GET")
    )
    paths = _paths(added)
    for name in ("id", "paramKey", "paramValue", "remark"):
        assert f"$.data.{name}:exists" in paths


def test_sensitive_fields_are_not_asserted(tool, spec):
    asserts = []
    added = tool.add_main_assertions(
        asserts, "/sys/user/info", "GET", spec.data_shape("/sys/user/info", "GET")
    )
    paths = _paths(added)
    assert "$.data.userId:exists" in paths
    assert "$.data.password:exists" not in paths


def test_array_shape_gets_length_assertion(tool, spec):
    asserts = []
    added = tool.add_main_assertions(
        asserts, "/sys/menu/list", "GET", spec.data_shape("/sys/menu/list", "GET")
    )
    assert "$.data:len>=" in _paths(added)


def test_captcha_uses_rep_code(tool, spec):
    asserts = []
    added = tool.add_main_assertions(
        asserts, "/captcha/get", "POST", spec.data_shape("/captcha/get", "POST")
    )
    paths = _paths(added)
    assert "$.data.repCode:==" in paths
    assert "$.data.repData:exists" in paths
    assert "$.code:==" not in paths


def test_negative_case_is_left_alone(tool):
    for asserts in (
        [{"check": "body", "jsonpath": "$.success", "comparator": "==", "expect": False}],
        [{"check": "body", "jsonpath": "$.code", "comparator": "!=", "expect": "00000"}],
        [{"check": "body", "jsonpath": "$.data", "comparator": "is_null", "expect": None}],
    ):
        assert tool.is_negative_case(asserts)
    assert not tool.is_negative_case(
        [{"check": "body", "jsonpath": "$.success", "comparator": "==", "expect": True}]
    )


def test_binary_case_is_detected(tool):
    assert tool.is_binary_case(
        [{"check": "body", "jsonpath": "$.content_type", "comparator": "contains", "expect": "spreadsheet"}]
    )
    assert not tool.is_binary_case(
        [{"check": "body", "jsonpath": "$.code", "comparator": "==", "expect": "00000"}]
    )


def test_strengthened_case_is_not_touched(tool):
    asserts = [
        {"check": "body", "jsonpath": "$.code", "comparator": "==", "expect": "00000"},
        {"check": "body", "jsonpath": "$.data", "comparator": "exists"},
    ]
    assert tool.is_strengthened(asserts, "/admin/area/info/1")
    assert not tool.is_strengthened(
        [{"check": "body", "jsonpath": "$.code", "comparator": "==", "expect": "00000"}],
        "/admin/area/info/1",
    )


def test_no_duplicate_when_run_twice(tool, spec):
    asserts = [{"check": "status_code", "comparator": "==", "expect": 200}]
    tool.add_main_assertions(
        asserts, "/admin/area/page", "GET", spec.data_shape("/admin/area/page", "GET")
    )
    first = json.dumps(asserts, sort_keys=True, ensure_ascii=False)
    added = tool.add_main_assertions(
        asserts, "/admin/area/page", "GET", spec.data_shape("/admin/area/page", "GET")
    )
    assert added == []
    assert json.dumps(asserts, sort_keys=True, ensure_ascii=False) == first


def test_setup_step_gets_code_assertion(tool, spec):
    step = {"operation_type": "api", "path": "/sys/menu", "method": "POST"}
    added = tool.add_step_assertions(step, spec)
    assert "$.code:==" in _paths(added)
    assert step["assert"][0]["jsonpath"] == "$.code"


def test_setup_page_step_gets_records_assertion(tool, spec):
    step = {"operation_type": "api", "path": "/admin/area/page", "method": "GET"}
    added = tool.add_step_assertions(step, spec)
    paths = _paths(added)
    assert "$.code:==" in paths
    assert "$.data.records:exists" in paths


def test_generated_assertions_are_parseable_by_framework(tool, spec):
    """生成的是扁平格式，得能过 flat_parser 的翻译（不然用例直接报错）。"""
    from utils.excel.flat_parser import _translate_assertion

    asserts = []
    tool.add_main_assertions(
        asserts, "/admin/area/page", "GET", spec.data_shape("/admin/area/page", "GET")
    )
    tool.add_main_assertions(
        asserts, "/sys/config/info/1", "GET", spec.data_shape("/sys/config/info/1", "GET")
    )
    for item in asserts:
        translated = _translate_assertion(item)
        assert translated is not None
        assert translated["type"]
