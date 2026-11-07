"""各张表 → 用例数据。

把 Excel 的 4 张表（模块信息 / 接口定义 / 测试用例 / 测试数据）
翻译成框架能跑的 ``{"testcase": {...}}`` 结构。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from utils.excel.cells import (
    _coerce_scalar,
    parse_assert_cell,
    parse_json_cell,
    parse_kv_cell,
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
from utils.logs.log_manager import log


def parse_module_sheet(sheet: Any) -> dict[str, Any]:
    """解析「模块信息」表。"""
    rows = _rows(sheet)
    if not rows:
        return {}

    row = rows[0]
    config: dict[str, Any] = {}

    name = _get(row, "模块名", "模块", "name")
    if name:
        config["name"] = str(name).strip()

    label = _get(row, "模块标签", "标签", "label")
    if label:
        config["label"] = str(label).strip()

    desc = _get(row, "模块说明", "说明", "description")
    if desc:
        config["description"] = str(desc).strip()

    order = _get(row, "执行顺序", "顺序", "order")
    if order not in (None, ""):
        try:
            config["order"] = int(order)
        except (TypeError, ValueError):
            pass

    marks = _get(row, "模块标签(多选)", "标签(多选)", "marks")
    if marks:
        if isinstance(marks, str):
            config["marks"] = [m.strip() for m in re.split(r"[,，\s]+", marks) if m.strip()]
        else:
            config["marks"] = list(marks)

    return config


def parse_api_rows(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """解析「接口定义」表，返回 ``{接口名: 接口定义}``。"""
    apis: dict[str, dict[str, Any]] = {}

    for row in rows:
        api_name = str(_get(row, "接口名", "接口", "name", default="")).strip()
        if not api_name:
            continue

        path = str(_get(row, "接口路径", "路径", "path", default="")).strip()
        method = str(_get(row, "请求方法", "方法", "method", default="GET")).strip().upper()

        api: dict[str, Any] = {
            "id": api_name,
            "description": str(_get(row, "接口描述", "描述", "description", default="")).strip(),
            "path": path,
            "method": method,
        }

        headers = parse_kv_cell(_get(row, "请求头", "headers", default=""), "请求头")
        if headers:
            api["headers"] = headers

        params = parse_kv_cell(_get(row, "Query参数", "查询参数", "params", default=""), "Query参数")
        if params:
            api["params"] = params

        body = parse_json_cell(_get(row, "请求体", "Body", "body", "data", default=""), "请求体")
        if body not in ("", {}, None):
            api["data"] = body

        asserts = parse_assert_cell(_get(row, "断言", "assert", default=""), "断言")
        if asserts:
            api["assert"] = asserts

        apis[api_name] = api

    return apis


def parse_case_rows(
    rows: list[dict[str, Any]],
    apis: dict[str, dict[str, Any]],
    data_groups: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """解析「测试用例」表 + 「测试数据」表，返回用例列表。

    每个用例是标准格式：``{"testcase": {...}}``
    """
    cases: list[dict[str, Any]] = []

    for row in rows:
        case_name = str(_get(row, "用例名", "用例", "name", default="")).strip()
        if not case_name:
            continue

        api_name = str(_get(row, "关联接口", "接口名", "接口", default="")).strip()
        api = apis.get(api_name)
        if api is None:
            if not api_name:
                # 没关联接口 —— 允许直接写步骤（一步到位）
                api = _parse_inline_step(row)
            else:
                raise ExcelCaseError(
                    f"第 {row.get('__row__')} 行的用例「{case_name}」关联的接口 "
                    f"「{api_name}」在「接口定义」表里找不到"
                )

        case: dict[str, Any] = {
            "name": case_name,
            "description": str(_get(row, "用例说明", "说明", "description", default="")).strip(),
        }

        # 用例状态（调试态 / 执行态）
        #
        # Excel 是**人工维护**的源 —— 写进去的用例默认就是"要跑的"。
        # 没写状态列时按 debugged 处理，不按 draft —— 不然整个 Excel
        # 用例集会因为没标状态而全部被跳过（静默跳过最坑人）。
        #
        # 想临时不跑某条，在「用例状态」列填 disabled 或 broken。
        status = _get(row, "用例状态", "状态", "status", default="")
        if status not in (None, ""):
            case["status"] = str(status).strip()
        else:
            case["status"] = "debugged"

        # 报告层级
        #
        # ⚠️ **「项目」列不读** —— epic（项目层）统一由框架按项目档案解析。
        #
        # 原因：Excel 里手填的项目名是死的（"mall4j 管理后台"），
        # 而框架解析出来的会跟着环境走（"mall4j 管理后台（测试环境）"）。
        # 两边不一致时，报告里同一个项目会被劈成两棵树，很迷惑。
        #
        # 所以 Excel 只管「模块」和「场景」两层，上面那层交给框架。
        allure: dict[str, str] = {}
        for key, column in (
            ("feature", "模块"),
            ("story", "场景"),
        ):
            value = str(_get(row, column, default="")).strip()
            if value:
                allure[key] = value
        if allure:
            case["allure"] = allure

        # 标签
        marks = _get(row, "用例标签", "标签", "marks", default="")
        if marks:
            if isinstance(marks, str):
                case["marks"] = [m.strip() for m in re.split(r"[,，\s]+", marks) if m.strip()]
            else:
                case["marks"] = list(marks)

        # 步骤（默认一个；也可以在「测试用例」表里写多步）
        steps = _parse_case_steps(row, api)
        case["steps"] = steps

        # 参数化：关联「测试数据」表里的数据组
        group_name = str(_get(row, "数据组", "参数组", "数据组名", default="")).strip()
        if group_name:
            groups = data_groups.get(group_name)
            if groups is None:
                raise ExcelCaseError(
                    f"第 {row.get('__row__')} 行的用例「{case_name}」引用的数据组 "
                    f"「{group_name}」在「测试数据」表里找不到"
                )
            case["parameters"] = groups

        cases.append({"testcase": case})

    return cases


def _parse_inline_step(row: dict[str, Any]) -> dict[str, Any]:
    """从「测试用例」表直接读一个步骤（不用「接口定义」表）。"""
    path = str(_get(row, "接口路径", "路径", "path", default="")).strip()
    method = str(_get(row, "请求方法", "方法", "method", default="GET")).strip().upper()

    step: dict[str, Any] = {
        "id": str(_get(row, "步骤ID", "步骤名", default="step")).strip() or "step",
        "description": str(_get(row, "步骤描述", default="")).strip(),
        "path": path,
        "method": method,
    }

    headers = parse_kv_cell(_get(row, "请求头", default=""), "请求头")
    if headers:
        step["headers"] = headers

    params = parse_kv_cell(_get(row, "Query参数", default=""), "Query参数")
    if params:
        step["params"] = params

    body = parse_json_cell(_get(row, "请求体", "body", default=""), "请求体")
    if body not in ("", {}, None):
        step["data"] = body

    asserts = parse_assert_cell(_get(row, "断言", default=""), "断言")
    if asserts:
        step["assert"] = asserts

    return step


def _parse_case_steps(row: dict[str, Any], api: dict[str, Any]) -> list[dict[str, Any]]:
    """把关联的接口定义变成步骤列表。

    支持一个用例多个步骤：在「测试用例」表里用「步骤序号」列区分，
    多行同名用例表示同一个用例的多个步骤。
    """
    step = dict(api)

    # 用例行里的字段覆盖接口定义（方便临时改）
    headers = parse_kv_cell(_get(row, "请求头", default=""), "请求头")
    if headers:
        step["headers"] = headers

    params = parse_kv_cell(_get(row, "Query参数", default=""), "Query参数")
    if params:
        step["params"] = params

    body = parse_json_cell(_get(row, "请求体", "body", default=""), "请求体")
    if body not in ("", {}, None):
        step["data"] = body

    asserts = parse_assert_cell(_get(row, "断言", default=""), "断言")
    if asserts:
        step["assert"] = asserts

    return [step]


def parse_data_rows(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """解析「测试数据」表。

    表结构：

    +----------+--------+--------------+----------+
    |    | 数据组   | 场景名 | searchType   | pageSize |
    +==========+========+==============+==========+
    | 近七天   |        | LAST_SEVEN   | 10       |
    | 近三十天 |        | LAST_THIRTY  | 20       |
    +----------+--------+--------------+----------+

    - 「数据组」列是组名，同名的多行 = 多个场景
    - 后面的列名就是参数名（要和接口里的 ``{{xxx}}`` 对上）
    - 「场景名」列可以填，填了就在报告里显示这个名字；不填就用参数值拼

    也支持**多个数据组**（不同组的行混在一张表里）。
    """
    groups: dict[str, list[dict[str, Any]]] = {}

    for row in rows:
        group_name = str(_get(row, "数据组", "参数组", "数据组名", default="")).strip()
        if not group_name:
            continue

        values: dict[str, Any] = {}
        for key, value in row.items():
            if key in ("__row__", "数据组", "参数组", "数据组名", "场景名", "备注"):
                continue
            if value is None or value == "":
                continue
            values[key] = _coerce_scalar(str(value))

        if not values:
            continue

        scene_name = str(_get(row, "场景名", default="")).strip()
        if scene_name:
            groups.setdefault(group_name, []).append({"name": scene_name, "data": values})
        else:
            groups.setdefault(group_name, []).append(values)

    return groups


def load_excel_cases(
    path: Path,
    project: str = "",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """读一个 Excel 文件，返回 ``(用例列表, 模块信息)``。

    用例列表的元素是标准格式 ``{"testcase": {...}}``。

    支持两种 Excel 格式（自动判断）：

    - **标准格式**：模块信息 / 接口定义 / 测试用例 / 测试数据 四张表
    - **扁平格式**：一张「接口测试用例」大表，一行一条用例（JSON 单元格）

    Args:
        path: Excel 文件路径
        project: 项目名（用于查模块标签，报告里显示中文模块名）
    """
    workbook = read_workbook(path)

    # 扁平格式：一张大表搞定，先判断
    from utils.excel.flat_parser import is_flat_workbook, parse_flat_cases

    if is_flat_workbook(workbook):
        cases = parse_flat_cases(workbook, project=project)
        log.info("Excel %s: 扁平格式，%s 条用例", path.name, len(cases))
        return cases, {}

    module_config = parse_module_sheet(_sheet_by_name(workbook, SHEET_MODULE))
    api_rows = _rows(_sheet_by_name(workbook, SHEET_API))
    case_rows = _rows(_sheet_by_name(workbook, SHEET_CASE))

    # 测试数据可能在多张表里（每张表一个数据组，参数名可以不一样）
    data_groups: dict[str, list[dict[str, Any]]] = {}
    data_sheet = _sheet_by_name(workbook, SHEET_DATA)
    if data_sheet is not None:
        data_groups.update(parse_data_rows(_rows(data_sheet)))

    # 除了标准的那张，其他表（名字叫「数据组名」或「数据1」等）也都是数据表
    standard_sheets = {SHEET_MODULE, SHEET_API, SHEET_CASE}
    standard_sheets.update(SHEET_ALIASES.get(SHEET_MODULE, ()))
    standard_sheets.update(SHEET_ALIASES.get(SHEET_API, ()))
    standard_sheets.update(SHEET_ALIASES.get(SHEET_CASE, ()))

    for sheet_name in workbook.sheetnames:
        if sheet_name in standard_sheets:
            continue
        # 「说明」表不是数据表
        if sheet_name in ("说明", "readme", "README"):
            continue
        rows = _rows(workbook[sheet_name])
        if rows:
            data_groups.update(parse_data_rows(rows))

    if not api_rows and not case_rows:
        raise ExcelCaseError(
            f"{path.name} 里没有「接口定义」和「测试用例」两张表，用例读不出来"
        )

    apis = parse_api_rows(api_rows)
    cases = parse_case_rows(case_rows, apis, data_groups)

    log.info(
        "Excel %s: %s 个接口 / %s 条用例 / %s 个数据组",
        path.name, len(apis), len(cases), len(data_groups),
    )

    return cases, module_config
