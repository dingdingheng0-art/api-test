"""``tools/maint/rename_ai_cases.py`` 的行为测试。

历史 AI 用例把「接口名」抄成了 ``PUT /admin/area`` 这种「方法 + 路径」，
报告里看不出测什么。改名工具要满足三条：

1. **只碰 ``ai_`` 沙箱** —— 现役模块的用例名是人工维护的
2. 能按「方法 + 路径」从现役用例里找到中文业务名
3. 写盘后复核，任何一格没落成就恢复原文件
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _load_module() -> Any:
    """按路径加载，避开模块顶层 ``sys.stdout.reconfigure`` 对 pytest 的副作用。"""
    path = PROJECT_ROOT / "tools" / "maint" / "rename_ai_cases.py"
    spec = importlib.util.spec_from_file_location("_rename_ai_cases_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def rac() -> Any:
    return _load_module()


@pytest.fixture()
def excel_tree(tmp_path, monkeypatch):
    """现役模块一条中文用例 + 沙箱模块一条「方法+路径」用例。"""
    import openpyxl

    from utils.cases import case_source

    from ai.agent.tools import case_io

    root = tmp_path / "cases_excel"
    live_dir = root / "proj" / "mod"
    live_dir.mkdir(parents=True)
    sandbox_dir = root / "proj" / "ai_mod"
    sandbox_dir.mkdir(parents=True)

    headers = {name: idx for idx, name in enumerate(case_io.FLAT_HEADERS, start=1)}

    def write(path: Path, rows: list[dict[str, Any]]) -> None:
        wb = openpyxl.Workbook()
        wb.active.title = case_io.SHEET_FLAT
        sheet = wb[case_io.SHEET_FLAT]
        for idx, name in enumerate(case_io.FLAT_HEADERS, start=1):
            sheet.cell(1, idx).value = name
        for row_idx, values in enumerate(rows, start=2):
            for name, value in values.items():
                sheet.cell(row_idx, headers[name]).value = value
        wb.save(path)

    base = {
        "模块": "mod",
        "标签": "mod",
        "优先级": "P1",
        "请求方法": "GET",
        "请求头": "{}",
        "请求数据": "{}",
        "断言": json.dumps(
            [{"check": "body", "jsonpath": "$.success", "comparator": "==", "expect": True}],
            ensure_ascii=False,
        ),
        "用例状态": "draft",
    }
    write(
        live_dir / "mod.xlsx",
        [
            {
                **base,
                "case_id": "API-001",
                "用例名称": "数据管理 - 查数据（正常请求）",
                "接口名称": "查数据",
                "用例标题": "（正常请求）",
                "接口": "/admin/x",
            }
        ],
    )
    write(
        sandbox_dir / "ai_mod.xlsx",
        [
            {
                **base,
                "case_id": "API-900",
                "用例名称": "ai mod - GET /admin/x - GET /admin/x（正常）",
                "接口名称": "GET /admin/x",
                "用例标题": "GET /admin/x（正常）",
                "接口": "/admin/x",
            }
        ],
    )

    monkeypatch.setattr(case_source, "get_source_roots", lambda *a, **k: [root])
    monkeypatch.setattr(case_source, "current_excel_dir", lambda *a, **k: root)
    from ai.agent.tools._common import clear_cache

    clear_cache()
    yield root
    clear_cache()


def _cell(file_path: Path, row: int, column: str) -> str:
    import openpyxl

    from ai.agent.tools import case_io

    sheet = openpyxl.load_workbook(file_path)[case_io.SHEET_FLAT]
    headers = {c.value: idx for idx, c in enumerate(sheet[1], start=1) if c.value}
    return str(sheet.cell(row, headers[column]).value or "")


def test_only_ai_modules_are_scanned(rac, excel_tree):
    files = rac._sandbox_files("proj")
    assert [module_dir.name for _proj, module_dir, _file in files] == ["ai_mod"]


def test_plan_aligns_method_path_to_live_chinese_name(rac, excel_tree):
    _proj, module_dir, file_path = rac._sandbox_files("proj")[0]
    changes = rac.plan_file("proj", module_dir, file_path)
    assert len(changes) == 1
    change = changes[0]
    assert change["old"]["接口名称"] == "GET /admin/x"
    assert change["new"]["接口名称"] == "查数据"
    assert change["new"]["用例标题"] == "（正常）"
    assert change["new"]["用例名称"].endswith(" - 查数据（正常）")
    # 预览不能动文件
    assert _cell(file_path, 2, "接口名称") == "GET /admin/x"


def test_apply_writes_and_backs_up(rac, excel_tree, tmp_path, monkeypatch):
    _proj, module_dir, file_path = rac._sandbox_files("proj")[0]
    changes = rac.plan_file("proj", module_dir, file_path)
    backup_dir = tmp_path / "backup"
    rac.apply_file("proj", module_dir, file_path, changes, backup_dir)

    assert _cell(file_path, 2, "接口名称") == "查数据"
    assert _cell(file_path, 2, "用例标题") == "（正常）"
    assert _cell(file_path, 2, "用例名称").endswith(" - 查数据（正常）")
    assert (backup_dir / "proj" / "ai_mod" / "ai_mod.xlsx").exists()
    # 沙箱模块补了中文 label，报告左侧树才不是 "ai mod"
    assert (module_dir / "module.yaml").exists()


def test_apply_does_not_clobber_other_columns(rac, excel_tree, tmp_path):
    """只改命名三列，断言/预期响应这些列原样保留。"""
    _proj, module_dir, file_path = rac._sandbox_files("proj")[0]
    before = _cell(file_path, 2, "断言")
    changes = rac.plan_file("proj", module_dir, file_path)
    rac.apply_file("proj", module_dir, file_path, changes, tmp_path / "backup")
    assert _cell(file_path, 2, "断言") == before
    assert _cell(file_path, 2, "用例状态") == "draft"


def test_needs_rename_flags_old_names_only(rac):
    assert rac._needs_rename("", "GET /x", "查数据") is True
    assert rac._needs_rename("GET /x - GET /x", "GET /x", "查数据") is True
    assert rac._needs_rename("ai mod - GET /x（正常）", "GET /x", "查数据") is True
    assert rac._needs_rename("地区管理 - 查数据（正常）", "查数据", "查数据") is False
