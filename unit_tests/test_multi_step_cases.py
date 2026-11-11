"""多步场景链（Excel「步骤」列）的读写测试。

**为什么要测**

Excel 扁平格式原来是「一行 = 一步」。多步链（创建→查询→修改→删除）
只能靠一行存一个 JSON 数组表达 —— 一旦解析或写回错位，用例会「存得下、
跑不了」，或者更糟：断言改在了行级单元格上，运行时不生效。

这里覆盖四件事：

1. 解析：``步骤`` 列 → ``case["steps"]``，不填时保持老的单步行为
2. 写盘：``create_case(steps=[...])`` 写进「步骤」列并能原样读回
3. 改断言：``apply_assertion_change`` 只改目标步骤，不动其它步骤
4. 审计：``tools/maint/audit_quality.audit`` 能抓出薄用例
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai.agent.tools import case_io  # noqa: E402
from utils.excel.flat_parser import (  # noqa: E402
    ExcelCaseError,
    load_flat_cases,
    parse_flat_cases,
)

SHEET = case_io.SHEET_FLAT


def _flat_workbook(values: dict, headers: list[str] | None = None):
    """用给定单元格内容造一个最小的扁平格式工作簿。"""
    import openpyxl

    wb = openpyxl.Workbook()
    wb.active.title = SHEET
    sheet = wb[SHEET]
    names = headers or case_io.FLAT_HEADERS
    for idx, name in enumerate(names, start=1):
        sheet.cell(1, idx).value = name
    for name, value in values.items():
        sheet.cell(2, names.index(name) + 1).value = value
    return wb


def _parse_one(values: dict) -> dict:
    return parse_flat_cases(_flat_workbook(values))[0]["testcase"]


def _step(step_id: str, method: str, path: str, asserts: list[dict]) -> dict:
    return {
        "id": step_id,
        "description": f"{method} {path}",
        "method": method,
        "path": path,
        "assert": asserts,
    }


SUCCESS = {"check": "body", "jsonpath": "$.success", "comparator": "==", "expect": True}


def _without_none(asserts: list[dict]) -> list[dict]:
    """结构断言从 Excel 读回来会多一个 ``expected: None`` —— 比较时忽略。"""
    return [
        {key: val for key, val in item.items() if not (key == "expected" and val is None)}
        for item in asserts
    ]


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------


def test_steps_column_is_parsed_into_case_steps():
    case = _parse_one(
        {
            "case_id": "API-010",
            "用例名称": "地区场景链",
            "接口": "/admin/area",
            "请求方法": "POST",
            "断言": json.dumps([SUCCESS]),
            "步骤": json.dumps(
                [
                    {
                        "id": "create",
                        "method": "POST",
                        "path": "/admin/area",
                        "data": {"areaName": "自动化区域"},
                        "assert": [SUCCESS],
                    },
                    {
                        "id": "verify",
                        "method": "GET",
                        "path": "/admin/area/{{create.data.areaId}}",
                        "assert": [
                            {
                                "check": "body",
                                "jsonpath": "$.data.records",
                                "comparator": "exists",
                            }
                        ],
                    },
                ],
                ensure_ascii=False,
            ),
        }
    )

    assert [step["id"] for step in case["steps"]] == ["create", "verify"]
    assert case["steps"][1]["path"] == "/admin/area/{{create.data.areaId}}"
    # 结构断言解析回来带 ``expected: None``（扁平格式里本来就没有 expect）
    assert case["steps"][1]["assert"] == [
        {"type": "exists", "field": "data.records", "expected": None}
    ]
    assert case["steps"][0]["data"] == {"areaName": "自动化区域"}


def test_no_steps_column_keeps_single_step_behaviour():
    case = _parse_one(
        {
            "case_id": "API-011",
            "用例名称": "查数据",
            "接口": "/admin/x",
            "请求方法": "GET",
            "断言": json.dumps([SUCCESS]),
        }
    )
    assert len(case["steps"]) == 1
    assert case["steps"][0]["path"] == "/admin/x"


def test_invalid_steps_json_raises_with_clear_error():
    with pytest.raises(ExcelCaseError):
        _parse_one(
            {
                "case_id": "API-012",
                "用例名称": "坏步骤",
                "接口": "/admin/x",
                "步骤": "[{not json",
            }
        )


def test_step_without_path_is_rejected():
    with pytest.raises(ExcelCaseError):
        _parse_one(
            {
                "case_id": "API-013",
                "用例名称": "缺路径",
                "接口": "/admin/x",
                "步骤": json.dumps([{"id": "s1", "method": "GET"}]),
            }
        )


# --------------------------------------------------------------------------
# 写盘 / 读回
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    """写盘会备份到 ``.cache/backup`` —— 单测不能污染真实目录。"""
    from utils import paths

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr(paths, "CACHE_DIR", cache_dir)
    return cache_dir


@pytest.fixture
def case_root(tmp_path, monkeypatch):
    """把用例根指到临时目录，避免污染真实模块。"""
    from utils.cases import case_source

    root = tmp_path / "cases_excel"
    monkeypatch.setattr(case_source, "get_source_roots", lambda *a, **k: [root])
    monkeypatch.setattr(case_source, "current_excel_dir", lambda *a, **k: root)

    from ai.agent.tools._common import clear_cache

    clear_cache()
    yield root
    clear_cache()


CHAIN_STEPS = [
    {
        "id": "create",
        "method": "POST",
        "path": "/admin/area",
        "data": {"areaName": "自动化区域"},
        "assert": [
            {"type": "equal", "field": "success", "expected": True},
            {"type": "exists", "field": "data"},
        ],
    },
    {
        "id": "verify",
        "method": "GET",
        "path": "/admin/area/{{create.data.areaId}}",
        "assert": [
            {"type": "exists", "field": "data.records"},
            {"type": "length_gt", "field": "data.records", "expected": 0},
        ],
    },
]

CHAIN_TEARDOWN = [
    {
        "id": "clean",
        "operation_type": "api",
        "method": "DELETE",
        "path": "/admin/area/{{verify.data.records[0].areaId}}",
    }
]


def test_create_case_writes_multiple_steps_and_reads_back(case_root):
    result = case_io.create_case(
        project="proj",
        module="mod",
        path="/admin/area",
        method="POST",
        title="（场景链）",
        api_name="地区场景链",
        steps=CHAIN_STEPS,
        teardowns=CHAIN_TEARDOWN,
    )

    file_path = Path(result["file"])
    cases, _meta = load_flat_cases(file_path)
    case = cases[0]["testcase"]
    assert [step["id"] for step in case["steps"]] == ["create", "verify"]
    assert case["steps"][1]["path"] == "/admin/area/{{create.data.areaId}}"
    assert case["steps"][1]["assert"] == [
        {"type": "exists", "field": "data.records", "expected": None},
        {"type": "length_gt", "field": "data.records", "expected": 0},
    ]
    # 行级字段展示第一步，方便 -k / 人工浏览
    assert case["steps"][0]["method"] == "POST"


def test_apply_assertion_change_updates_the_target_step_only(case_root):
    result = case_io.create_case(
        project="proj",
        module="mod",
        path="/admin/area",
        method="POST",
        title="（场景链）",
        api_name="地区场景链",
        steps=CHAIN_STEPS,
        teardowns=CHAIN_TEARDOWN,
    )
    case_io.apply_assertion_change(
        result["case_id"],
        [
            {"type": "exists", "field": "data.records"},
            {"type": "length_gt", "field": "data.records", "expected": 1},
        ],
        step="verify",
        note="补一条值断言",
    )

    cases, _meta = load_flat_cases(Path(result["file"]))
    steps = cases[0]["testcase"]["steps"]
    assert _without_none(steps[0]["assert"]) == _without_none(
        CHAIN_STEPS[0]["assert"]
    ), "不该动第一步"
    assert steps[1]["assert"][-1] == {
        "type": "length_gt",
        "field": "data.records",
        "expected": 1,
    }


# --------------------------------------------------------------------------
# 存量审计
# --------------------------------------------------------------------------


def _ref(module: str = "mod", case_id: str = "API-001"):
    return SimpleNamespace(
        case_id=case_id, name="用例", module=module, file="proj/mod/mod.xlsx", status="debugged"
    )


def test_audit_flags_thin_positive_and_missing_cleanup():
    from tools.maint.audit_quality import audit

    thin = {
        "name": "新增地区",
        "marks": ["positive"],
        "steps": [{"method": "POST", "path": "/admin/area",
                   "assert": [{"type": "equal", "field": "success", "expected": True}]}],
    }
    findings = audit([(_ref(), thin)])
    assert findings
    assert set(findings[0]["rules"]) >= {"thin_positive", "write_without_cleanup"}


def test_audit_passes_a_good_case():
    from tools.maint.audit_quality import audit

    good = {
        "name": "新增地区",
        "marks": ["positive"],
        "steps": [
            {
                "method": "POST",
                "path": "/admin/area",
                "assert": [
                    {"type": "equal", "field": "success", "expected": True},
                    {"type": "exists", "field": "data"},
                    {"type": "exists", "field": "data.records"},
                ],
            }
        ],
        "teardowns": [{"id": "clean", "method": "DELETE", "path": "/admin/area"}],
    }
    assert audit([(_ref(), good)]) == []
