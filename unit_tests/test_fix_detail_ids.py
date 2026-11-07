"""``tools/maint/fix_detail_ids.py`` 的单元测试。

这个工具的职责是"把详情用例里写死但库里不存在的 id 换成真实存在的"。
写错的两个方向都会有害：

- **改错了**：换成另一个也不存在的 id，用例照样必挂
- **该跳过没跳过**：库里查不到还硬改，等于把用例改成必然失败

所以这里钉住：查库命中才改、查不到就跳过、路径不匹配不动。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_tool():
    path = ROOT / "tools" / "maint" / "fix_detail_ids.py"
    spec = importlib.util.spec_from_file_location("fix_detail_ids", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def tool():
    return _load_tool()


class _StubClient:
    """记录查了什么，按预设返回行数。"""

    def __init__(self, count: int):
        self.count = count
        self.sql = ""
        self.params = None

    def scalar(self, sql, params=None):
        self.sql, self.params = sql, params
        return self.count


# ---------------------------------------------------------------------------
# 查库确认
# ---------------------------------------------------------------------------


def test_row_id_exists_queries_right_table(tool):
    hit = _StubClient(1)
    assert tool.row_id_exists(hit, "tz_area", "area_id", 110000000000) is True
    assert hit.sql == "SELECT COUNT(*) FROM tz_area WHERE area_id = %s"
    assert hit.params == [110000000000]


def test_row_id_exists_treats_none_as_missing(tool):
    """``scalar`` 拿不到就返回 None —— 这种情况要当成"不存在"。"""
    assert tool.row_id_exists(_StubClient(None), "tz_area", "area_id", 1) is False
    assert tool.row_id_exists(_StubClient(0), "tz_area", "area_id", 1) is False


# ---------------------------------------------------------------------------
# 路径改写
# ---------------------------------------------------------------------------


def _sheet(rows):
    """造一个最小的 worksheet 替身（只用到 iter_rows）。"""
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["case_id", "接口", "断言"])
    for row in rows:
        ws.append(row)
    return ws


def test_patch_path_replaces_only_the_target_case(tool):
    ws = _sheet([
        ["API-173", "/admin/area/info/1", "[]"],
        ["API-174", "/admin/area/info/999999999", "[]"],
    ])
    assert tool.patch_path(ws, "API-173", "/admin/area/info/1", "/admin/area/info/110000000000")
    assert ws["B2"].value == "/admin/area/info/110000000000"
    # 别的用例一个字符都不能动
    assert ws["B3"].value == "/admin/area/info/999999999"


def test_patch_path_refuses_unexpected_old_value(tool):
    """路径和预期对不上就别改 —— 说明用例被改过了，盲目替换会写坏。"""
    ws = _sheet([["API-173", "/admin/area/info/7", "[]"]])
    assert not tool.patch_path(ws, "API-173", "/admin/area/info/1", "/admin/area/info/110000000000")
    assert ws["B2"].value == "/admin/area/info/7"


def test_patch_path_returns_false_for_missing_case(tool):
    ws = _sheet([["API-999", "/x", "[]"]])
    assert not tool.patch_path(ws, "API-173", "/admin/area/info/1", "/y")


# ---------------------------------------------------------------------------
# 断言改写
# ---------------------------------------------------------------------------


def test_patch_asserts_writes_back_json(tool):
    ws = _sheet([["API-129", "/sys/user/info/1", json.dumps([
        {"check": "body", "jsonpath": "$.data.userId", "comparator": "==", "expect": 1},
    ])]])

    assert tool.patch_asserts(ws, "API-129", tool.fix_user_detail_pk_assert)
    written = json.loads(ws["C2"].value)
    assert written[0]["jsonpath"] == "$.data.username"
    assert written[0]["expect"] == "admin"


def test_fix_user_detail_pk_assert_only_touches_equality_check(tool):
    """``exists`` 断言的 userId 留着（那是另一个检查），只改等值断言。"""
    asserts = [
        {"check": "body", "jsonpath": "$.data.userId", "comparator": "exists"},
        {"check": "body", "jsonpath": "$.data.userId", "comparator": "==", "expect": 1},
    ]
    tool.fix_user_detail_pk_assert(asserts)
    assert asserts[0]["comparator"] == "exists"
    assert asserts[0]["jsonpath"] == "$.data.userId"
    assert asserts[1]["jsonpath"] == "$.data.username"


def test_fix_user_detail_pk_assert_noop_when_absent(tool):
    asserts = [{"check": "body", "jsonpath": "$.code", "comparator": "==", "expect": "00000"}]
    assert tool.fix_user_detail_pk_assert(asserts) == asserts


# ---------------------------------------------------------------------------
# 配置自检
# ---------------------------------------------------------------------------


def test_fixes_target_existing_files(tool):
    for rel, *_ in tool.FIXES:
        assert (tool.CASE_ROOT / rel).exists(), f"清单里的工作簿不存在: {rel}"


def test_fixes_all_use_literal_ids(tool):
    """新 id 必须是字面量 —— 留个模板占位符在路径里是跑不通的。"""
    for _rel, case_id, _old, new, _table, _pk, value in tool.FIXES:
        assert new.rsplit("/", 1)[-1] == str(value), f"{case_id}: 路径末尾和 id 对不上"
        assert isinstance(value, int), f"{case_id}: id 应该是整数"
