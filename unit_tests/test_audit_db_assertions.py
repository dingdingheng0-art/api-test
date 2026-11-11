"""``tools/maint/audit_db_assertions.py`` 的分类逻辑。

审计脚本的价值全在"该报的报、不该报的不报"：

- 漏报 → 该验库的用例没人管（这正是它存在的理由）
- 误报 → 每次都列一堆查不了库的接口（``/captcha/get``、``/token/refresh``），
  久而久之没人再看它的输出

这里钉住这两个方向。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_module():
    path = ROOT / "tools" / "maint" / "audit_db_assertions.py"
    spec = importlib.util.spec_from_file_location("audit_db_assertions", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def audit():
    return _load_module()


class TestClassify:
    def test_detail_needs_db(self, audit) -> None:
        assert audit.classify("GET", "/admin/area/info/1", "") == "detail"

    def test_page_list_is_not_applicable(self, audit) -> None:
        """分页验的是"接口能返回"，查库意义不大。"""
        assert audit.classify("GET", "/admin/area/page", "") == ""
        assert audit.classify("GET", "/prod/prod/list", "") == ""

    def test_write_methods(self, audit) -> None:
        assert audit.classify("POST", "/admin/area", "") == "create"
        assert audit.classify("PUT", "/admin/area", "") == "update"
        assert audit.classify("DELETE", "/admin/area/1", "") == "delete"

    @pytest.mark.parametrize("path", ["/captcha/get", "/token/refresh"])
    def test_non_persisting_endpoints_are_skipped(self, audit, path: str) -> None:
        """认证 / 验证码类 POST 不写业务表，不能按"新增"要求落库断言。"""
        assert audit.classify("POST", path, "") == ""

    def test_current_user_info_still_needs_db(self, audit) -> None:
        """``/sys/user/info`` 路径里没主键，但它查的确实是库里的人。

        审计按路径判成 ``detail`` 是对的 —— 逼着它必须有一条落库断言；
        具体怎么补（用登录账号当主键）由 add_db_assertions 的
        ``ENDPOINT_OVERRIDES`` 负责。
        """
        assert audit.classify("GET", "/sys/user/info", "") == "detail"

    def test_case_insensitive_path(self, audit) -> None:
        assert audit.classify("POST", "/Token/Refresh", "") == ""
        assert audit.classify("GET", "/ADMIN/AREA/PAGE", "") == ""


class TestCollectAssertTypes:
    def test_flat_excel_db_check_is_recognised(self, audit) -> None:
        """回归：只认内部格式的 ``type`` 时，每条用例都显示"没有断言"。"""
        out: set[str] = set()
        audit.collect_assert_types(
            [{"check": "db", "sql": "SELECT 1", "comparator": "exists"}], out
        )
        assert "db" in out

    def test_flat_body_check_is_not_db(self, audit) -> None:
        out: set[str] = set()
        audit.collect_assert_types(
            [{"check": "body", "jsonpath": "$.success", "comparator": "=="}], out
        )
        assert "db" not in out
        assert "body" in out

    def test_nested_setup_and_teardown_are_walked(self, audit) -> None:
        out: set[str] = set()
        audit.collect_assert_types(
            {
                "setup": [{"assert": [{"check": "db", "sql": "SELECT 1"}]}],
                "teardown": [{"assert": [{"type": "db_query_not_exists"}]}],
            },
            out,
        )
        assert "db" in out
        assert "db_query_not_exists" in out

    def test_internal_format_type_is_recognised(self, audit) -> None:
        out: set[str] = set()
        audit.collect_assert_types([{"type": "db_query"}], out)
        assert "db_query" in out


def test_teardown_db_assert_is_reported_as_misplaced(
    audit, tmp_path, monkeypatch, capsys
) -> None:
    """后置里的落库断言不能抵覆盖率，要单列成「放错位置」。"""
    import json

    import openpyxl

    module_dir = tmp_path / "admin_area"
    module_dir.mkdir()
    path = module_dir / "admin_area.xlsx"

    headers = ["用例ID", "接口", "请求方法", "断言", "前置", "后置", "用例状态", "用例名称"]
    values = {
        "用例ID": "API-001",
        "接口": "/admin/area",
        "请求方法": "POST",
        "断言": json.dumps(
            [{"check": "body", "jsonpath": "$.success", "comparator": "==", "expect": True}]
        ),
        "前置": "",
        "后置": json.dumps(
            [
                {
                    "id": "查数据",
                    "operation_type": "api",
                    "path": "/admin/area/page",
                    "method": "GET",
                    "assert": [
                        {"check": "db", "sql": "SELECT 1", "comparator": "exists"}
                    ],
                }
            ],
            ensure_ascii=False,
        ),
        "用例状态": "debugged",
        "用例名称": "新增地区",
    }

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = audit.SHEET
    for idx, header in enumerate(headers, start=1):
        ws.cell(1, idx).value = header
        ws.cell(2, idx).value = values[header]
    wb.save(path)

    monkeypatch.setattr(audit, "CASE_ROOT", tmp_path)
    code = audit.audit(False)
    out = capsys.readouterr().out

    assert code == 1
    assert "放在后置" in out
    assert "API-001" in out


def test_cleanup_delete_db_assert_is_allowed(
    audit, tmp_path, monkeypatch, capsys
) -> None:
    """后置 DELETE 上的「删后查不到」是清理验证，不算放错位置。"""
    import json

    import openpyxl

    module_dir = tmp_path / "admin_area"
    module_dir.mkdir()
    path = module_dir / "admin_area.xlsx"

    headers = ["用例ID", "接口", "请求方法", "断言", "前置", "后置", "用例状态", "用例名称"]
    values = {
        "用例ID": "API-002",
        "接口": "/admin/area",
        "请求方法": "POST",
        "断言": json.dumps(
            [
                {"check": "body", "jsonpath": "$.success", "comparator": "==", "expect": True},
                {"check": "db", "sql": "SELECT area_id FROM tz_area WHERE area_id = %s",
                 "comparator": "exists"},
            ]
        ),
        "前置": "",
        "后置": json.dumps(
            [
                {
                    "id": "清理数据",
                    "operation_type": "api",
                    "path": "/admin/area/1",
                    "method": "DELETE",
                    "assert": [
                        {
                            "check": "db",
                            "sql": "SELECT area_id FROM tz_area WHERE area_id = %s",
                            "comparator": "not_exists",
                        }
                    ],
                }
            ],
            ensure_ascii=False,
        ),
        "用例状态": "debugged",
        "用例名称": "新增地区",
    }

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = audit.SHEET
    for idx, header in enumerate(headers, start=1):
        ws.cell(1, idx).value = header
        ws.cell(2, idx).value = values[header]
    wb.save(path)

    monkeypatch.setattr(audit, "CASE_ROOT", tmp_path)
    code = audit.audit(False)
    out = capsys.readouterr().out

    assert code == 0
    assert "放在后置" not in out
