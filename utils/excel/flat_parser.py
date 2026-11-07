"""扁平格式的 Excel 用例（一张大表 + JSON 单元格）。

框架原有格式是「模块信息 / 接口定义 / 测试用例 / 测试数据」四张表；
这份解析器处理的是**另一种**格式：一张叫「接口测试用例」的大表，
一行一条用例，请求头 / 请求数据 / 断言都是 JSON 字符串。

两种格式并存：文件里有没有「接口测试用例」这张表，决定走哪条解析路径
（见 :func:`utils.excel.parser.load_excel_cases`）。

字段对应关系：

==========================  ==========================================
Excel 列                     框架内部字段
==========================  ==========================================
case_id                      用例名的一部分（保证唯一）
用例名称                      description 后缀（能用来 -k 筛选）
接口名称                      用例名 / 报告标题里的业务名（中文）
用例标题                      description
优先级                        allure severity + pytest 标记（P0~P3）
标签                          pytest 标记
接口 / 请求方法                steps[].path / steps[].method
请求头                        steps[].headers（空值丢掉，用例级优先）
请求数据                      GET/DELETE → params，其余 → body
断言                          steps[].assert
参数化数据                    parameters
==========================  ==========================================
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from utils.excel.cells import parse_json_cell
from utils.excel.reader import ExcelCaseError, _rows, _sheet_by_name, read_workbook

# 扁平格式的主表
SHEET_FLAT = "接口测试用例"
# 扁平格式的可选配置表
SHEET_CONFIG = "配置"

# 优先级 → allure 严重级别
SEVERITY = {
    "P0": "critical",
    "P1": "normal",
    "P2": "minor",
    "P3": "trivial",
}

# 这些方法把「请求数据」当查询参数发
QUERY_METHODS = ("GET", "DELETE", "HEAD", "OPTIONS")

# 「标签」列里表示"执行方式"而不是"业务分组"的标签。
# 判断场景名时要跳过它们，否则报告里会出现「商品管理 > smoke」。
_EXECUTION_MARKS = frozenset({"smoke", "p0", "p1", "p2", "p3", "wip", "slow", "regression"})

# 配置表里认识的键（其它键忽略）
_CONFIG_KEYS = ("base_url", "timeout", "global_headers", "verify_ssl")

# 请求头名字统一成框架用的写法
_HEADER_ALIASES = {
    "accesstoken": "accesstoken",
    "access-token": "accesstoken",
    "x-access-token": "accesstoken",
}


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------


def is_flat_workbook(workbook: Any) -> bool:
    """这个工作簿是不是扁平格式。"""
    return SHEET_FLAT in workbook.sheetnames


def load_flat_cases(path: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """读一个扁平格式的 Excel，返回 ``(用例列表, 模块信息)``。

    扁平格式没有「模块信息」表，所以模块信息返回空 dict ——
    框架会退回用同目录的 ``module.yaml``。

    同时会读同目录的 ``interfaces.yaml``（接口路径 → 中文名），
    用来给没填「接口名称」列的用例兜底。
    """
    workbook = read_workbook(path)
    return parse_flat_cases(workbook, interfaces=load_interface_names(path)), {}


def load_interface_names(path: Any) -> dict[str, str]:
    """读 Excel 同目录的 ``interfaces.yaml``。

    文件格式（接口路径 → 中文名）::

        /admin/statistics/order: 订单统计

    没有这个文件就返回空字典。
    """
    try:
        yaml_path = Path(path).resolve().parent / "interfaces.yaml"
    except (OSError, TypeError, ValueError):
        return {}

    if not yaml_path.exists():
        return {}

    try:
        import yaml

        data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 - 配错了不该让用例跑不起来
        return {}

    if not isinstance(data, dict):
        return {}

    # 支持两种写法：直接映射，或者套一层 interfaces:
    raw = data.get("interfaces", data)
    if not isinstance(raw, dict):
        return {}

    return {str(k): str(v) for k, v in raw.items() if v}


def parse_flat_cases(
    workbook: Any,
    interfaces: dict[str, str] | None = None,
    project: str = "",
) -> list[dict[str, Any]]:
    """把「接口测试用例」表解析成框架能跑的用例列表。

    Args:
        workbook: 已打开的工作簿
        interfaces: 接口路径 → 中文名（用于兜底命名）
        project: 项目名（用于查模块标签；不传就按默认项目兜底）
    """
    sheet = _sheet_by_name(workbook, SHEET_FLAT)
    if sheet is None:
        raise ExcelCaseError(f"Excel 里没有「{SHEET_FLAT}」这张表")

    rows = _rows(sheet)
    if not rows:
        return []

    global_headers = _parse_config(_sheet_by_name(workbook, SHEET_CONFIG))

    cases: list[dict[str, Any]] = []
    for row in rows:
        case = _parse_row(row, global_headers, interfaces, project=project)
        if case is not None:
            cases.append({"testcase": case})

    return cases


# --------------------------------------------------------------------------
# 配置表
# --------------------------------------------------------------------------


def _parse_config(sheet: Any) -> dict[str, Any]:
    """读「配置」表。

    只有 ``global_headers`` 会被用到（合进每条用例的请求头）；
    ``base_url`` / ``timeout`` / ``verify_ssl`` 由框架自己的配置管，
    这里读了也不生效，所以直接忽略。

    表结构：第 1 行标题、第 2 行空行、第 3 行表头（配置项/值/说明），
    第 4 行起是数据。
    """
    if sheet is None:
        return {}

    for row in sheet.iter_rows(min_row=4, values_only=True):
        if not row:
            continue
        key = str(row[0]).strip() if row[0] is not None else ""
        if key != "global_headers":
            continue
        value = row[1] if len(row) > 1 else None
        parsed = parse_json_cell(value, "global_headers")
        if isinstance(parsed, dict):
            return parsed

    return {}


# --------------------------------------------------------------------------
# 单行 → 一条用例
# --------------------------------------------------------------------------


def _parse_row(
    row: dict[str, Any],
    global_headers: dict[str, Any],
    interfaces: dict[str, str] | None = None,
    project: str = "",
) -> dict[str, Any] | None:
    """把「接口测试用例」表里的一行变成一条用例。"""
    case_id = str(_get(row, "case_id", "用例编号", default="")).strip()
    case_name = str(_get(row, "用例名称", default="")).strip()
    title = str(_get(row, "用例标题", default="")).strip()
    path = str(_get(row, "接口", "接口路径", default="")).strip()
    # 「接口名称」是作者自己写的业务名（中文），报告里显示它
    api_name = str(_get(row, "接口名称", default="")).strip()

    # 一行什么都没填 → 跳过
    if not (case_id or case_name or title or path):
        return None
    if not path:
        raise ExcelCaseError(
            f"第 {row.get('__row__')} 行（{case_id or case_name}）没有填「接口」"
        )

    method = str(_get(row, "请求方法", default="GET")).strip().upper()

    # 报告标题 = 「接口名称 - 场景说明」
    #   接口名称优先用作者填的「接口名称」列；
    #   没填就用同目录 interfaces.yaml 里的映射；
    #   都没有就从「用例标题」里拆，最后退回用例编号。
    if not api_name and interfaces:
        api_name = interfaces.get(path, "")
    if api_name:
        name = api_name
        description = _split_desc(title)
    else:
        name, description = _split_title(title, case_id, case_name)

    case: dict[str, Any] = {
        "name": name,
        "description": description,
    }
    # pytest 风格的用例名单独留着，方便 -k 搜索
    if case_name and case_name != name:
        case["alias"] = case_name
    # 用例编号也留着，报告里能对上 Excel 那一行
    if case_id:
        case["case_id"] = case_id

    # ---- 报告层级 / 严重级别 / 标记 ----
    priority = str(_get(row, "优先级", default="")).strip().upper()
    if priority:
        severity = SEVERITY.get(priority)
        if severity:
            case["severity"] = severity
        case["priority"] = priority

    marks: list[str] = []
    for raw in (str(_get(row, "标签", default="")), priority.lower()):
        for mark in re.split(r"[,，\s]+", raw):
            mark = mark.strip()
            if mark and mark not in marks:
                marks.append(mark)
    if marks:
        case["marks"] = marks

    # ---- 调试状态 ----
    #
    # 「用例状态」列：draft（没调试过）/ debugged（调试通过）/
    # disabled（临时下线）/ broken（已知有问题）。
    #
    # Excel 是**人工维护**的源 —— 写进去的用例默认就是"要跑的"。
    # 没写状态列时按 debugged 处理，不按 draft —— 不然整个 Excel
    # 用例集会因为没标状态而全部被跳过（静默跳过最坑人）。
    #
    # 想临时不跑某条，在「用例状态」列填 disabled 或 broken。
    status = str(_get(row, "用例状态", "状态", default="")).strip().lower()
    if status:
        case["status"] = status
    else:
        case["status"] = "debugged"

    # ---- 危险删除开关 ----
    #
    # 「允许危险删除」列填 true/1/是 时，这条用例的步骤可以 DELETE
    # 受保护路径上的数据。只给「真删」类用例用 —— 删的是本用例
    # setup 刚造出来的测试数据，但服务端分配的 id 不在 safe_id 区间时，
    # 安全闸会误拦。
    allow_dangerous = str(
        _get(row, "允许危险删除", "危险删除", default="")
    ).strip().lower()
    if allow_dangerous in ("1", "true", "yes", "y", "on", "是", "允许"):
        case["allow_dangerous"] = True

    # ---- 报告层级 ----
    #
    # 层级约定（和框架另一套 Excel 格式保持一致）：
    #   feature（模块层）—— 由同目录的 module.yaml 决定，中文名
    #   story  （场景层）—— 用例的「场景」列
    #
    # 场景列没填就退回「标签」列，再退回「模块」列。
    # 标签/模块列填的是英文模块名（比如 manager_statistics），
    # 直接当场景名在报告里没法看，所以先查中文映射表。
    #
    # ⚠️ 如果查出来和模块名一样（常见 —— 场景列没填的情况），
    #    就拿「接口名称」当场景名 —— 否则报告会显示成
    #    「商品属性 > 商品属性 > attribute」，两层重复，看着别扭。
    module_name = str(_get(row, "模块", default="")).strip()
    scene_column = str(_get(row, "场景", default="")).strip()
    tag_name = str(_get(row, "标签", default="")).strip()

    # 「标签」列现在会混放业务分组（``prod_prod, smoke, p1``），
    # 整串拿去当场景名会显示成「商品管理 > prod prod, smoke」。
    # 取第一个既不是模块名、也不是常见执行标签（smoke / p0~p3 / wip）的，
    # 才算业务标签；都没有就退回模块名。
    tag_first = next(
        (
            t
            for t in (x.strip() for x in tag_name.replace("，", ",").split(","))
            if t and t != module_name and t.lower() not in _EXECUTION_MARKS
        ),
        module_name,
    )

    story = scene_column or _readable_name(tag_first or module_name, project=project)

    # 场景名和模块名重复 → 换成接口名，至少能区分 page / info / add
    if story and module_name and story == _readable_name(module_name, project=project):
        story = name or story

    if story:
        case.setdefault("allure", {})["story"] = story

    # ---- 步骤 ----
    # 步骤名用接口中文名，别把用例标题再拼一遍（报告里步骤区会很啰嗦）
    step: dict[str, Any] = {
        "id": case_id or name,
        "description": name,
        "path": path,
        "method": method,
    }

    headers = _parse_headers(_get(row, "请求头", default=""), global_headers)
    if headers:
        step["headers"] = headers

    request_data = _parse_request_data(
        _get(row, "请求数据", default=""), method
    )
    params, body = request_data
    if params:
        step["params"] = params
    if body not in ("", {}, None):
        step["data"] = body

    # 「查询参数」列 —— 显式指定放 URL 上的参数
    #
    # 有些 PUT 接口的参数是走 **query** 的（比如 ``/prod/prod/prodStatus``
    # 要 ``?prodId=18&prodStatus=1``），不是 body。
    # 但框架默认把「请求数据」当 body 发 —— 那样服务端收不到参数，
    # 直接报「Required request parameter 'prodId' is not present」。
    # 所以给这两类接口单独留一列。
    query_params = _parse_query_params(_get(row, "查询参数", default=""))
    if query_params:
        merged = dict(step.get("params") or {})
        merged.update(query_params)
        step["params"] = merged
        # 参数已经挪到 query 了，body 就别再重复发一份
        if step.get("data") == query_params:
            step.pop("data", None)

    # 「响应类型」列 —— 下载类接口（导出 Excel）返回的是文件，不是 JSON
    #
    # 不写这一列时按 JSON 解析；写了 binary/file 就返回二进制描述字典，
    # 断言里用 __binary__.size / __binary__.content_type 校验。
    response_type = str(_get(row, "响应类型", default="")).strip().lower()
    if response_type in ("binary", "file", "download"):
        step["response_type"] = "binary"

    asserts = _parse_asserts(_get(row, "断言", default=""))
    if asserts:
        step["assert"] = asserts

    # ---- 前置 / 后置 ----
    #
    # 「前置」和「后置」列都是 JSON 数组，格式和 YAML 用例里的一致：
    #
    #   前置: [{"id": "建数据", "operation_type": "api", "path": "/api/...",
    #           "method": "POST", "data": {...}, "assert": [...]}]
    #
    # 写操作（POST / PUT / DELETE）通常需要「先造数据再查」，
    # 所以这两列不是可有可无的 —— 生成器会把它们填好。
    setups = _parse_pre_post(_get(row, "前置", "前置准备", default=""), "前置")
    if setups:
        case["setups"] = setups

    teardowns = _parse_pre_post(_get(row, "后置", "后置清理", default=""), "后置")
    if teardowns:
        case["teardowns"] = teardowns

    case["steps"] = [step]

    # ---- 参数化 ----
    params_spec = _parse_param_data(_get(row, "参数化数据", default=""))
    if params_spec:
        case["parameters"] = params_spec

    return case


# --------------------------------------------------------------------------
# 前置 / 后置
# --------------------------------------------------------------------------


def _parse_pre_post(value: Any, label: str) -> list[dict[str, Any]]:
    """解析「前置」/「后置」列。

    格式是 JSON 数组，每项是一个操作：

    .. code-block:: json

        [{"id": "造一条品牌", "operation_type": "api",
          "path": "/prod/prod/brand", "method": "POST",
          "headers": {"Content-Type": "application/x-www-form-urlencoded"},
          "data": {"name": "自动化品牌", "logo": "https://x/y.png"},
          "assert": [{"type": "equal", "field": "code", "expected": 200}]}]

    ``operation_type`` 支持 ``api`` / ``db`` / ``factory``（后置还支持
    ``factory_cleanup``），和 YAML 用例的写法完全一样。
    """
    raw = parse_json_cell(value, label)
    if raw in ("", {}, None):
        return []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        raise ExcelCaseError(f"「{label}」必须是 JSON 数组")

    items: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ExcelCaseError(f"「{label}」里每一项都必须是对象: {item!r}")

        op = str(item.get("operation_type") or "api").strip()
        if not item.get("id"):
            raise ExcelCaseError(f"「{label}」里的每一项都要有 id")
        if op == "api" and not (item.get("path") and item.get("method")):
            raise ExcelCaseError(f"「{label}」的 {item['id']} 是 api 类型，缺少 path 或 method")
        if op == "db" and not item.get("query"):
            raise ExcelCaseError(f"「{label}」的 {item['id']} 是 db 类型，缺少 query")
        if op in ("factory", "factory_cleanup") and not item.get("factory"):
            raise ExcelCaseError(f"「{label}」的 {item['id']} 缺少 factory")

        parsed = dict(item)
        parsed["operation_type"] = op
        parsed["method"] = str(parsed.get("method") or "GET").upper()
        if parsed.get("assert"):
            parsed["assert"] = _parse_asserts(parsed["assert"])
        items.append(parsed)

    return items


# --------------------------------------------------------------------------
# 请求头 / 请求数据
# --------------------------------------------------------------------------


def _parse_headers(value: Any, global_headers: dict[str, Any]) -> dict[str, Any]:
    """解析「请求头」列。

    - 空字符串值的头直接丢掉（不然会把框架自动填的 token/uuid 覆盖成空）
    - ``accessToken`` 这种写法统一成框架用的 ``accesstoken``
    """
    raw = parse_json_cell(value, "请求头")
    if not isinstance(raw, dict):
        if raw in ("", {}, None):
            return {}
        raise ExcelCaseError("「请求头」必须是 JSON 对象")

    headers: dict[str, Any] = {}
    for key, val in raw.items():
        if val in (None, ""):
            continue
        name = str(key).strip()
        lowered = name.lower()
        name = _HEADER_ALIASES.get(lowered, name)
        headers[name] = val

    # 全局请求头优先级最低
    for key, val in global_headers.items():
        headers.setdefault(key, val)

    return headers


def _parse_query_params(value: Any) -> dict[str, Any]:
    """解析「查询参数」列（必须是 JSON 对象）。"""
    raw = parse_json_cell(value, "查询参数")
    if raw in ("", {}, None):
        return {}
    if not isinstance(raw, dict):
        raise ExcelCaseError("「查询参数」必须是 JSON 对象")
    return _convert_placeholders(_strip_bullets(raw))


def _parse_request_data(value: Any, method: str) -> tuple[dict[str, Any], Any]:
    """解析「请求数据」列。

    Returns:
        ``(查询参数, 请求体)`` —— GET/DELETE 放查询参数，其余放请求体。
    """
    raw = parse_json_cell(value, "请求数据")
    if raw in ("", {}, None):
        return {}, None
    if not isinstance(raw, (dict, list)):
        raw = {"body": raw}

    raw = _strip_bullets(raw)
    raw = _convert_placeholders(raw)

    if method in QUERY_METHODS:
        return raw, None
    return {}, raw


def _strip_bullets(value: Any) -> Any:
    """去掉文档表格里那种 ``»`` 前缀。

    接口文档的表格用 ``»`` 表示层级，比如 ``» app_id``；
    真发请求时键名就是 ``app_id``。
    """
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, val in value.items():
            name = re.sub(r"^[»\s]+", "", str(key))
            result[name] = _strip_bullets(val)
        return result
    if isinstance(value, list):
        return [_strip_bullets(item) for item in value]
    return value


# --------------------------------------------------------------------------
# 断言
# --------------------------------------------------------------------------


def _parse_asserts(value: Any) -> list[dict[str, Any]]:
    """把扁平格式的断言数组翻译成框架的断言。

    扁平格式::

        [{"check": "body", "jsonpath": "$.success", "comparator": "==", "expect": true}]

    框架格式::

        {"type": "equal", "field": "success", "expected": True}
    """
    raw = parse_json_cell(value, "断言")
    if raw in ("", {}, None):
        return []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        raise ExcelCaseError("「断言」必须是 JSON 数组")

    asserts: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ExcelCaseError(f"「断言」里每一项都必须是对象: {item!r}")
        translated = _translate_assertion(item)
        if translated is not None:
            asserts.append(translated)

    return asserts


def _translate_assertion(item: dict[str, Any]) -> dict[str, Any] | None:
    """翻译一条断言。"""
    check = str(item.get("check") or "").strip().lower()
    comparator = str(item.get("comparator") or "==").strip()
    expect = _convert_placeholders(item.get("expect"))

    if check == "status_code":
        return {"type": "status_code", "expected": expect}

    if check == "header":
        key = item.get("key")
        if not key:
            raise ExcelCaseError("header 断言缺少 key")
        assertion: dict[str, Any] = {
            "type": "response_header",
            "field": str(key),
            "expected": expect,
        }
        return assertion

    if check == "time":
        # 扁平格式用秒，框架的耗时断言用毫秒
        ms = expect * 1000 if isinstance(expect, (int, float)) else expect
        return {"type": "elapsed_time", "expected": ms}

    if check == "full_body":
        return {"type": "full_body", "expected": expect, "comparator": comparator}

    if check == "body":
        field = _jsonpath_to_field(str(item.get("jsonpath") or ""))
        if field == "":
            # 校验整个响应体
            return {"type": "full_body", "expected": expect, "comparator": comparator}
        return _value_assertion(field, comparator, expect)

    if check == "db":
        return _db_assertion(item, comparator, expect)

    raise ExcelCaseError(f"不认识的断言类型 check={check!r}")


def _db_assertion(
    item: dict[str, Any], comparator: str, expect: Any
) -> dict[str, Any]:
    """``check: db`` —— 直接查库校验（落库断言）。

    扁平格式::

        {"check": "db", "sql": "SELECT status FROM t WHERE id = %s",
         "params": [123], "comparator": "==", "expect": 1}

    ``sql`` / ``query`` / ``jsonpath`` 三个键都认（``jsonpath`` 是为了让
    从 Postman 导出的表直接改 ``check`` 就能用）。

    比较符按"第一列的值"翻译；``exists`` / ``not_exists`` 走行数判断。
    """
    sql = item.get("sql") or item.get("query") or item.get("jsonpath")
    if not sql:
        raise ExcelCaseError("db 断言缺少 sql（也可以写 query）")

    assertion: dict[str, Any] = {"query": str(sql).strip()}
    if item.get("column"):
        assertion["column"] = item["column"]
    if item.get("alias"):
        assertion["alias"] = item["alias"]
    if item.get("params"):
        assertion["params"] = item["params"]

    comp = str(comparator).strip().lower()

    if comp in ("exists", "not_exists"):
        assertion["type"] = f"db_query_{comp}"
        return assertion

    if comp in ("in", "not_in"):
        # 查库结果"在/不在集合里"用同一个类型，集合放 container
        assertion["type"] = "db_query_in"
        assertion["container"] = expect
        return assertion

    # 直接映射，不用再绕 _value_assertion —— 它的 "gt" 在这里没有对应类型
    db_types = {
        "==": "db_query",
        ">": "db_query_gt",
        ">=": "db_query_gte",
        "<": "db_query_lt",
        "<=": "db_query_lte",
    }
    db_type = db_types.get(comp)
    if db_type is None:
        raise ExcelCaseError(
            f"db 断言不支持比较符 {comparator!r}"
            "（可用：== / > / >= / < / <= / in / not_in / exists / not_exists）"
        )
    assertion["type"] = db_type
    assertion["expected"] = expect
    return assertion


def _value_assertion(field: str, comparator: str, expect: Any) -> dict[str, Any]:
    """按比较符翻译成框架的取值类断言。"""
    simple = {
        "==": "equal",
        "!=": "not_equal",
        ">": "gt",
        ">=": "gte",
        "<": "lt",
        "<=": "lte",
        "contains": "contains",
        "not_contains": "not_contains",
        "is_none": "is_none",
        "is_not_none": "is_not_none",
        "is_null": "is_none",
        "is_not_null": "is_not_none",
        "null": "is_none",
        "not_null": "is_not_none",
        "regex": "matches",
        "exists": "exists",
        "not_exists": "not_exists",
    }

    comp = str(comparator).strip().lower()

    if comp in simple:
        return {"type": simple[comp], "field": field, "expected": expect}

    if comp in ("in", "not_in"):
        return {
            "type": "in" if comp == "in" else "not in",
            "field": field,
            "container": expect,
        }

    if comp.startswith("len"):
        # len> / len>= / len< / len<= / len== / len!=
        suffix = comp[3:] or "=="
        length_type = {
            "==": "length",
            "!=": "length_ne",
            ">": "length_gt",
            ">=": "length_gte",
            "<": "length_lt",
            "<=": "length_lte",
        }.get(suffix)
        if length_type is None:
            raise ExcelCaseError(f"不认识的比较符: {comparator!r}")
        return {"type": length_type, "field": field, "expected": expect}

    raise ExcelCaseError(f"不认识的比较符: {comparator!r}")


def _jsonpath_to_field(path: str) -> str:
    """jsonpath → 框架的字段路径。

    ``$.data.list[0].id`` → ``data.list[0].id``
    ``$.result['name']``   → ``result.name``
    """
    if not path or path == "$":
        return ""

    text = path.strip()
    if text.startswith("$"):
        text = text[1:]

    # 只支持根节点开始的路径
    text = re.sub(r"^\.+", "", text)

    # ['xxx'] → .xxx
    text = re.sub(r"\[\s*'([^']+)'\s*\]", r".\1", text)
    text = re.sub(r'\[\s*"([^"]+)"\s*\]', r".\1", text)

    # $.a.b → a.b
    text = text.lstrip(".")

    return text


# --------------------------------------------------------------------------
# 参数化
# --------------------------------------------------------------------------


def _parse_param_data(value: Any) -> list[dict[str, Any]]:
    """解析「参数化数据」列。

    扁平格式用 ``${key}`` 占位，框架用 ``{{key}}``，这里顺便换过来。
    """
    raw = parse_json_cell(value, "参数化数据")
    if raw in ("", {}, None):
        return []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        raise ExcelCaseError("「参数化数据」必须是 JSON 数组")

    groups: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ExcelCaseError(f"「参数化数据」里每一项都必须是对象: {item!r}")
        # 场景名用原始值（``${key}`` 原样显示），变量才做占位符转换
        name = _group_name(item)
        values = {k: _convert_placeholders(v) for k, v in item.items()}
        # 带个场景名，报告里显示得更清楚（框架认 name/data 的写法）
        groups.append({"name": name, "data": values})

    return groups


def _group_name(values: dict[str, Any]) -> str:
    """给参数组起个名字（报告里显示）。"""
    if not values:
        return ""
    return "-".join(f"{k}={v}" for k, v in values.items())


def _convert_placeholders(value: Any) -> Any:
    """``${key}`` → ``{{key}}``（框架的变量写法）。"""
    if isinstance(value, str):
        return re.sub(r"\$\{(\w+)\}", r"{{\1}}", value)
    if isinstance(value, dict):
        return {k: _convert_placeholders(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_convert_placeholders(item) for item in value]
    return value


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------


def _split_desc(title: str) -> str:
    """从「用例标题」里取出说明部分。

    ``店铺统计（正常请求）`` → ``店铺统计（正常请求）``
    ``店铺销售统计 - 店铺统计（正常请求）`` → ``店铺统计（正常请求）``

    有「接口名称」列时，说明就是标题里 ``-`` 后面的部分；
    没有分隔符就整段当说明。
    """
    text = (title or "").strip()
    if not text:
        return ""
    if " - " in text:
        _, _, desc = text.partition(" - ")
        return desc.strip()
    return text


def _split_title(title: str, case_id: str, case_name: str) -> tuple[str, str]:
    """把「用例标题」拆成（用例名, 用例说明）。

    标题形如 ``店铺销售统计 - 店铺统计（正常请求）``，
    拆开正好对上框架的两个字段：name = 前半，description = 后半。
    报告里拼出来就是 ``店铺销售统计 - 店铺统计（正常请求）``，不重复。

    没有分隔符就整体当用例名，说明留空。
    """
    text = (title or "").strip()
    if not text:
        return (case_id or case_name or ""), ""

    if " - " in text:
        name, _, desc = text.partition(" - ")
        name = name.strip()
        desc = desc.strip()
        if name and desc:
            return name, desc
        return (name or desc), ""

    return text, ""


def _readable_name(name: str, project: str = "") -> str:
    """把英文模块名变成报告里能看的中文。

    优先用 ``configs/modules.yaml`` 里配好的模块标签；
    没配的走一份内置的常见词映射；再不行就把英文按驼峰拆开。

    Args:
        name: 英文模块名
        project: 项目名（查模块标签时用；不传就用配置里的默认项目）
    """
    if not name:
        return ""

    # 已经是中文就直接用
    if any("\u4e00" <= ch <= "\u9fff" for ch in name):
        return name

    # 模块目录里配过 label 的，优先用 label
    try:
        from utils.cases.module_manager import ModuleManager
        from utils.config.project_profile import ProjectRegistry

        project = project or ProjectRegistry.default_project()
        label = ModuleManager.get_label(project, name)
        if label and any("\u4e00" <= ch <= "\u9fff" for ch in label):
            return label
    except Exception:  # noqa: BLE001 - 取不到就用内置映射
        pass

    from utils.cases.module_labels import builtin_label, split_english

    return builtin_label(name) or split_english(name)


def _split_english(name: str) -> str:
    """``admin_indexImg`` → ``admin indexImg``（拆不出中文时至少好读一点）。"""
    from utils.cases.module_labels import split_english

    return split_english(name)


def _get(row: dict[str, Any], *names: str, default: Any = "") -> Any:
    """按列名取值（支持多个候选名，取第一个有的）。"""
    for name in names:
        if name in row:
            value = row[name]
            if value not in (None, ""):
                return value
    return default


__all__ = [
    "SHEET_FLAT",
    "SHEET_CONFIG",
    "is_flat_workbook",
    "load_flat_cases",
    "parse_flat_cases",
]
