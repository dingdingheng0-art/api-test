"""读 Excel：打开文件、定位 Sheet、读成字典列表。

只管"怎么把表读出来"，不管单元格里的内容怎么解释
（那是 :mod:`utils.excel.cells` 的事）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from utils.cases.case_schema import TestCaseError

# Sheet 名（允许少量别名，避免用户改个名就跑不了）
SHEET_MODULE = "模块信息"
SHEET_API = "接口定义"
SHEET_CASE = "测试用例"
SHEET_DATA = "测试数据"

SHEET_ALIASES = {
    SHEET_MODULE: ("模块信息", "模块", "module"),
    SHEET_API: ("接口定义", "接口", "api", "apis"),
    SHEET_CASE: ("测试用例", "用例", "case", "cases"),
    SHEET_DATA: ("测试数据", "数据", "data"),
}


class ExcelCaseError(TestCaseError):
    """Excel 用例数据不合法。"""


def read_workbook(path: Path) -> Any:
    """读 Excel 文件（延迟导入，没装 openpyxl 也不影响其他格式）。"""
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover
        raise ExcelCaseError(
            "要读 Excel 用例需要安装 openpyxl：uv pip install openpyxl"
        ) from exc

    if not path.exists():
        raise ExcelCaseError(f"Excel 文件不存在: {path}")

    try:
        # data_only=True：读公式算出来的值，不是公式本身
        return load_workbook(path, data_only=True)
    except Exception as exc:  # noqa: BLE001
        raise ExcelCaseError(f"Excel 读取失败 {path}: {exc}") from exc


def _sheet_by_name(workbook: Any, canonical: str) -> Any:
    """按名字找 Sheet（支持别名）。"""
    names = SHEET_ALIASES.get(canonical, (canonical,))
    for alias in names:
        if alias in workbook.sheetnames:
            return workbook[alias]
    return None


def _rows(sheet: Any) -> list[dict[str, Any]]:
    """把一张表读成「字典列表」（第一行当表头）。

    - 空行自动跳过
    - ``None`` 值转成 ``""``
    - **说明行自动跳过**（以 ``←`` 或 ``#`` 开头）
    """
    if sheet is None:
        return []

    rows: list[dict[str, Any]] = []
    headers: list[str] = []

    for row_idx, row in enumerate(sheet.iter_rows(values_only=True), start=1):
        if row_idx == 1:
            headers = [str(c).strip() if c is not None else "" for c in row]
            continue

        # 整行空就跳过
        if not any(c is not None and str(c).strip() != "" for c in row):
            continue

        # 说明行（模板里的黄色提示行）跳过
        first = str(row[0]).strip() if row and row[0] is not None else ""
        if first.startswith("←") or first.startswith("#"):
            continue

        # 整行都是说明（每个格子都以 ← 或 # 开头）也跳过
        text_cells = [str(c).strip() for c in row if c is not None and str(c).strip()]
        if text_cells and all(
            c.startswith("←") or c.startswith("#") for c in text_cells
        ):
            continue

        item: dict[str, Any] = {}
        for col_idx, header in enumerate(headers):
            if not header:
                continue
            value = row[col_idx] if col_idx < len(row) else None
            item[header] = "" if value is None else value
        item["__row__"] = row_idx
        rows.append(item)

    return rows


def _get(row: dict[str, Any], *names: str, default: Any = "") -> Any:
    """按列名取值（支持多个候选名，取第一个有的）。"""
    for name in names:
        if name in row:
            value = row[name]
            if value not in (None, ""):
                return value
    return default
