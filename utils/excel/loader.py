"""Excel 用例解析：把 .xlsx 读成框架能跑的用例数据。

**为什么要这个**

YAML 写用例太啰嗦：缩进、变量写法、层级都得记。
Excel 里填表格更直观，产品经理/测试同学也能上手。

**文件约定**

一个 Excel 文件 = 一个模块：

.. code-block:: text

    testcases/cases/mall4j/statistics/statistics.xlsx

里面有 4 张表（Sheet）：

============ ==================================================
Sheet         内容
============ ==================================================
模块信息      模块名 / 标签 / 说明 / 执行顺序（只有一行）
接口定义      每个接口一行
测试用例      每个用例一行（关联到接口）
测试数据      参数化的数据组（可选）
============ ==================================================

**和 YAML 的关系**

解析出来的结构和 YAML 完全一样（``{"testcase": {...}}``），
所以框架的执行引擎、报告、断言逻辑一行都不用改。
YAML 继续兼容，两种格式可以混用。

**本包分三个模块**

============ ====================================================
模块           职责
============ ====================================================
``reader``     读 Excel 文件、定位 Sheet、读成字典列表
``cells``      单元格值的解析（JSON / 键值对 / 断言）
``parser``     各张表 → 用例数据
============ ====================================================

对外统一从 ``utils.excel.loader`` 进（保持原有 import 路径不变）。
"""
from __future__ import annotations

from utils.excel.cells import (
    _coerce_scalar,
    parse_assert_cell,
    parse_json_cell,
    parse_kv_cell,
)
from utils.excel.parser import (
    parse_api_rows,
    parse_case_rows,
    parse_data_rows,
    parse_module_sheet,
)
from utils.excel.reader import (
    SHEET_ALIASES,
    SHEET_API,
    SHEET_CASE,
    SHEET_DATA,
    SHEET_MODULE,
    ExcelCaseError,
    _get,
    _rows,
    _sheet_by_name,
    read_workbook,
)
from utils.excel.parser import _parse_case_steps, _parse_inline_step

__all__ = [
    # 错误
    "ExcelCaseError",
    # Sheet 名
    "SHEET_MODULE",
    "SHEET_API",
    "SHEET_CASE",
    "SHEET_DATA",
    "SHEET_ALIASES",
    # 读表
    "read_workbook",
    "_sheet_by_name",
    "_rows",
    "_get",
    # 单元格
    "parse_json_cell",
    "parse_kv_cell",
    "_coerce_scalar",
    "parse_assert_cell",
    # 解析
    "parse_module_sheet",
    "parse_api_rows",
    "parse_case_rows",
    "_parse_inline_step",
    "_parse_case_steps",
    "parse_data_rows",
    "load_excel_cases",
]


def load_excel_cases(path, project: str = "") -> tuple[list[dict], dict]:
    """读一个 Excel 文件，返回 ``(用例列表, 模块信息)``。

    用例列表的元素是标准格式 ``{"testcase": {...}}``。

    Args:
        path: Excel 文件路径
        project: 项目名（用于查模块标签）
    """
    from utils.excel.parser import load_excel_cases as _impl

    return _impl(path, project=project)
