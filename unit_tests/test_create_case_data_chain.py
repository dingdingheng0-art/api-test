"""``create_case`` 自动挂「造数 → 查 id → 清理」链路。

**历史问题**

批量生成器（``case_gen``）会给写用例补 setups/teardowns，但智能体直接
调 MCP ``create_case`` 时没有这一步：写接口用例落盘后既没造数也没清理，
一跑就在环境里留下一堆脏数据。这里锁住修复后的行为：

1. 正例写用例 → 自动补 setups（PUT/DELETE）或 teardowns（POST）
2. 清理只删本用例自己造的记录（路径引用 ``tools.find_id_by_fixed``）
3. 反例不造数（可能根本没写进去，无条件清理反而会把正确拒绝变成失败）
4. 模型自己写了 setups/teardowns → 尊重它，不再重复规划
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai.agent.tools import case_io  # noqa: E402
from ai.capabilities.case_gen import ProjectContext  # noqa: E402
from tools.importer.models import Endpoint, Param  # noqa: E402

AREA_RECORD = {"areaId": 110000000000, "areaName": "北京", "parentId": 0, "level": 1}


def _endpoints() -> list[Endpoint]:
    return [
        Endpoint(
            id="area_post",
            method="POST",
            path="/admin/area",
            name="新增地区",
            tags=["area"],
            body={"areaName": "string", "parentId": 0, "level": 0},
            body_params=[
                Param(name="areaName", location="body", required=True),
                Param(name="parentId", location="body", required=True),
                Param(name="level", location="body", required=True),
            ],
        ),
        Endpoint(
            id="area_list",
            method="GET",
            path="/admin/area/list",
            name="查询地区列表",
            tags=["area"],
            response_example={"code": "00000", "success": True, "data": [AREA_RECORD]},
        ),
        Endpoint(
            id="area_delete",
            method="DELETE",
            path="/admin/area/{id}",
            name="删除地区",
            tags=["area"],
            path_params=[Param(name="id", location="path")],
        ),
    ]


def _context() -> ProjectContext:
    # 空 examples：验证 POST 造数优先用 create_case 传进来的 request_data，
    # 而不是被现役样例覆盖。
    return ProjectContext(
        project="proj",
        contract={"success_code": "00000", "success_field": "success", "http_status": 200},
    )


@pytest.fixture(autouse=True)
def _fake_chain_context(tmp_path, monkeypatch):
    """把 ``.cache`` 和造数上下文都指到测试内的假数据。"""
    from utils import paths

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr(paths, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(
        case_io, "_chain_context", lambda project: (_context(), _endpoints())
    )
    return cache_dir


@pytest.fixture()
def excel_file(tmp_path, monkeypatch):
    """一个只有表头的扁平格式 Excel；用例根指向它。"""
    import openpyxl

    from utils.cases import case_source

    module_dir = tmp_path / "cases_excel" / "proj" / "mod"
    module_dir.mkdir(parents=True)
    file_path = module_dir / "mod.xlsx"

    workbook = openpyxl.Workbook()
    workbook.active.title = case_io.SHEET_FLAT
    sheet = workbook[case_io.SHEET_FLAT]
    for idx, name in enumerate(case_io.FLAT_HEADERS, start=1):
        sheet.cell(1, idx).value = name
    workbook.save(file_path)

    monkeypatch.setattr(
        case_source, "get_source_roots", lambda *a, **k: [tmp_path / "cases_excel"]
    )
    monkeypatch.setattr(
        case_source, "current_excel_dir", lambda *a, **k: tmp_path / "cases_excel"
    )

    from ai.agent.tools._common import clear_cache

    clear_cache()
    yield file_path
    clear_cache()


def _cell(file_path: Path, row: int, column: str):
    import openpyxl

    sheet = openpyxl.load_workbook(file_path)[case_io.SHEET_FLAT]
    headers = {c.value: idx for idx, c in enumerate(sheet[1], start=1) if c.value}
    return sheet.cell(row, headers[column]).value


def test_positive_post_case_gets_chain_and_cleanup(excel_file):
    result = case_io.create_case(
        project="proj",
        module="mod",
        path="/admin/area",
        method="POST",
        title="（正常请求）",
        api_name="新增地区",
        assertions=[
            {"type": "equal", "field": "success", "expected": True},
            {"type": "equal", "field": "data.areaName", "expected": "测试区域"},
            {"type": "is_not_none", "field": "data.areaId"},
        ],
        request_data={"areaName": "测试区域", "parentId": 110000000000, "level": 2},
        marks="proj_mod",
    )

    assert result["data_chain"], "正例 POST 应该自动挂上造数/清理链路"
    assert result["teardowns"] == 2, "POST 的清理 = 查数 + 删除"

    row = result["row"]
    stored_data = json.loads(_cell(excel_file, row, "请求数据"))
    assert "tools.fixed" in json.dumps(stored_data, ensure_ascii=False)
    assert stored_data["parentId"] == 110000000000, "其余字段要保留，不能被样例覆盖"

    teardowns = json.loads(_cell(excel_file, row, "后置"))
    assert [item["method"] for item in teardowns] == ["GET", "DELETE"]
    assert "find_id_by_fixed(查数据.data" in teardowns[1]["path"]
    assert '"areaId"' in teardowns[1]["path"]

    assert "api_data_chain" in _cell(excel_file, row, "标签")

    # 请求体里的名称被换成固定种子后，断言里回显的旧值要跟着换，
    # 否则这条断言必然失败。
    asserts = json.loads(_cell(excel_file, row, "断言"))
    echoed = [a for a in asserts if a.get("jsonpath") == "$.data.areaName"]
    assert echoed and "tools.fixed" in str(echoed[0].get("expect"))


def test_negative_write_case_gets_no_chain(excel_file):
    result = case_io.create_case(
        project="proj",
        module="mod",
        path="/admin/area",
        method="POST",
        title="（缺少必填参数）",
        api_name="新增地区",
        assertions=[
            {"type": "equal", "field": "success", "expected": False},
            {"type": "not_equal", "field": "code", "expected": "00000"},
        ],
        request_data={"parentId": 110000000000},
        marks="negative,proj_mod",
    )

    assert result["data_chain"] == ""
    assert result["teardowns"] == 0
    assert not _cell(excel_file, result["row"], "后置")


def test_model_supplied_teardown_is_not_replanned(excel_file):
    result = case_io.create_case(
        project="proj",
        module="mod",
        path="/admin/area/{{seed.areaId}}",
        method="DELETE",
        title="（正常请求）",
        api_name="删除地区",
        assertions=[
            {"type": "equal", "field": "success", "expected": True},
            {"type": "is_not_none", "field": "data"},
            # 质量闸要求正例至少两个业务断言（success / code 不算）
            {"type": "is_not_none", "field": "data.areaId"},
        ],
        setups=[
            {
                "id": "seed",
                "operation_type": "api",
                "method": "POST",
                "path": "/admin/area",
                "data": {"areaName": "测试区域", "parentId": 110000000000, "level": 2},
            }
        ],
        teardowns=[
            {
                "id": "清理",
                "operation_type": "api",
                "method": "DELETE",
                "path": "/admin/area/{{seed.areaId}}",
            }
        ],
        marks="proj_mod",
    )

    assert result["data_chain"] == "", "模型自带的链路不再重复规划"
    stored = json.loads(_cell(excel_file, result["row"], "后置"))
    assert stored[0]["path"] == "/admin/area/{{seed.areaId}}"


def test_request_data_as_json_string_still_gets_chain(excel_file):
    result = case_io.create_case(
        project="proj",
        module="mod",
        path="/admin/area",
        method="POST",
        title="（正常请求）",
        api_name="新增地区",
        assertions=[
            {"type": "equal", "field": "success", "expected": True},
            {"type": "is_not_none", "field": "data.areaId"},
            # 回显请求体的断言会被造数链路同步成固定种子，不能留在旧值上
            {"type": "equal", "field": "data.areaName", "expected": "测试区域"},
        ],
        request_data='{"areaName": "测试区域", "parentId": 110000000000, "level": 2}',
        marks="proj_mod",
    )

    assert result["data_chain"]
    stored_data = json.loads(_cell(excel_file, result["row"], "请求数据"))
    assert stored_data["parentId"] == 110000000000

    asserts = json.loads(_cell(excel_file, result["row"], "断言"))
    echoed = [a for a in asserts if a.get("jsonpath") == "$.data.areaName"]
    assert echoed and "tools.fixed" in str(echoed[0].get("expect"))
