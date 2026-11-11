"""用例生成：把接口列表变成框架能跑的 YAML 用例。

产出直接落在 ``testcases/cases/<项目>/<模块>/`` 下，结构跟手写的 YAML 完全一致：

.. code-block:: yaml

    name: get_order_list
    description: 订单列表

    allure:
      epic: mall4j 管理后台
      feature: 订单管理
      story: 订单列表

    marks: [api, p1]

    steps:
      - id: get_order_list
        description: 订单列表
        path: /order/list
        method: GET
        headers:
          accesstoken: '{{mall4j.token}}'
        params:
          pageSize: 10
        assert:
          - type: equal
            field: code
            expected: 200

另外还支持导出成 Excel（跟 ``to_excel.py`` 的表结构一致）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from tools.importer.models import Endpoint, sanitize_name

# 生成用例时的默认配置
DEFAULT_PROJECT = "mall4j"
DEFAULT_EPIC = "接口文档导入"

# 需要鉴权才调得通的接口（路径里带这些关键词）
AUTH_HINTS = ("passport", "login", "user/info", "order", "statistics", "goods", "menu")

# 这些请求头由框架/网络层自己管，用例里不用写
SKIP_HEADERS = {
    "host", "content-length", "connection", "accept-encoding",
    "postman-token", "cache-control", "pragma", "upgrade-insecure-requests",
    "sec-fetch-mode", "sec-fetch-site", "sec-fetch-dest", "sec-ch-ua",
    "sec-ch-ua-mobile", "sec-ch-ua-platform", "accept-language",
}


def group_by_module(endpoints: list[Endpoint]) -> dict[str, list[Endpoint]]:
    """按模块（tag）分组。"""
    groups: dict[str, list[Endpoint]] = {}
    for ep in endpoints:
        groups.setdefault(ep.module, []).append(ep)
    return groups


def build_testcase(
    ep: Endpoint,
    project: str = DEFAULT_PROJECT,
    module: str = "",
    epic: str = DEFAULT_EPIC,
    auth_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """把一个接口变成一条标准用例（``{"testcase": {...}}``）。"""
    case: dict[str, Any] = {
        "name": ep.id,
        "description": ep.summary or ep.display_name,
        "allure": {
            "epic": epic,
            "feature": module or ep.module,
            "story": ep.summary or ep.display_name,
        },
        "marks": ["api"],
        "steps": [build_step(ep, auth_headers=auth_headers)],
    }
    return {"testcase": case}


def build_step(ep: Endpoint, auth_headers: dict[str, str] | None = None) -> dict[str, Any]:
    """把一个接口变成一个步骤。"""
    step: dict[str, Any] = {
        "id": ep.id,
        "description": ep.summary or ep.display_name,
        "path": ep.path,
        "method": ep.method,
    }

    # 请求头：鉴权头优先，文档里的头只补空缺
    headers: dict[str, str] = {}
    for param in ep.headers:
        if param.name.lower() in SKIP_HEADERS:
            continue
        # 文档里没给真实值（造出来的假值）就不写进去 —— 免得把鉴权头覆盖成 "string"
        if param.example in (None, "") or param.fake:
            continue
        headers[param.name] = str(param.example)
    if auth_headers:
        headers.update(auth_headers)   # 鉴权头优先级最高
    if headers:
        step["headers"] = headers

    # Query 参数（只放真正的 query 参数，请求体字段不算）
    params = ep.query_map()
    if params:
        step["params"] = params

    # 请求体
    if ep.body is not None and ep.body != {} and ep.body != "":
        step["data"] = ep.body

    # 断言：根据响应示例自动推断
    asserts = build_asserts(ep)
    if asserts:
        step["assert"] = asserts

    return step


def build_asserts(ep: Endpoint) -> list[dict[str, Any]]:
    """根据响应示例生成基础断言。

    规则：

    - ``{code, success, ...}`` 包装 → 断言 code / success（这两条不算业务断言）
    - ``result`` / ``data`` 里的字段 → 逐字段断言存在（``is not None``）
    - 数值字段用 ``approx_equal``（金额类的浮点数用 ``==`` 会因精度翻车）
    - 业务数据是列表时断言字段存在，**不猜具体条数** —— 空列表也可能是合法响应

    ``data`` 是 mall4j 的主响应字段。以前只认 ``result``，导致正例只剩
    code/success 两条弱断言 —— 2026-11-10 全量生成里 141 条薄用例都是这么来的。
    """
    asserts: list[dict[str, Any]] = []

    example = ep.response_example
    if not isinstance(example, dict):
        return asserts

    if "code" in example:
        asserts.append({"type": "equal", "field": "code", "expected": example["code"]})
    if "success" in example:
        asserts.append({"type": "equal", "field": "success", "expected": example["success"]})
    if "message" in example and example.get("message"):
        asserts.append({"type": "contains", "field": "message",
                        "expected": str(example["message"])[:40]})

    result = example.get("result")
    if isinstance(result, dict):
        asserts.extend(_field_asserts(result, "result"))

    if "data" in example:
        data = example.get("data")
        if isinstance(data, dict):
            if data:
                asserts.extend(_field_asserts(data, "data"))
            else:
                asserts.append({"type": "exists", "field": "data"})
        else:
            # 列表 / null / 标量：只证明字段在，不猜内容
            asserts.append({"type": "exists", "field": "data"})

    # ``msg`` 是 mall4j 的稳定字段。业务断言不足时它至少能证明响应结构没变。
    if example.get("msg") not in (None, ""):
        asserts.append({"type": "exists", "field": "msg"})

    return asserts


def _field_asserts(container: dict[str, Any], prefix: str, limit: int = 5) -> list[dict[str, Any]]:
    """把一层响应字段翻译成断言（``prefix`` 是 ``result`` / ``data``）。"""
    out: list[dict[str, Any]] = []
    for key, value in list(container.items())[:limit]:
        field = f"{prefix}.{key}"
        if isinstance(value, bool):
            out.append({"type": "is_true" if value else "is_false", "field": field})
        elif isinstance(value, float):
            # 浮点数用近似相等，避免精度问题
            out.append({"type": "approx_equal", "field": field,
                        "expected": value, "tolerance": 0.01})
        elif isinstance(value, (str, int)):
            out.append({"type": "is_not_none", "field": field})
        elif isinstance(value, list):
            # 列表断言字段存在即可；条数依赖环境数据，猜了就会假失败
            out.append({"type": "exists", "field": field})
        elif isinstance(value, dict):
            # 对象字段用部分匹配，只比文档里给的字段
            out.append({"type": "subset", "field": field, "expected": value})
    return out


def build_module_yaml(
    module: str,
    endpoints: list[Endpoint],
    project: str = DEFAULT_PROJECT,
    epic: str = DEFAULT_EPIC,
    auth_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """生成模块级的 module.yaml。"""
    return {
        "name": sanitize_name(module),
        "label": module,
        "description": f"从接口文档导入的模块：{module}",
        "order": 500,
        "marks": ["imported"],
    }


def write_cases(
    endpoints: list[Endpoint],
    cases_root: Path,
    project: str = DEFAULT_PROJECT,
    epic: str = DEFAULT_EPIC,
    auth_headers: dict[str, str] | None = None,
    overwrite: bool = False,
) -> list[Path]:
    """把接口写成 YAML 用例文件。

    Returns:
        写入的文件路径列表
    """
    written: list[Path] = []
    groups = group_by_module(endpoints)

    project_dir = cases_root / project
    project_dir.mkdir(parents=True, exist_ok=True)

    for module, eps in groups.items():
        module_dir = project_dir / sanitize_name(module)
        module_dir.mkdir(parents=True, exist_ok=True)

        # module.yaml
        module_file = module_dir / "module.yaml"
        if overwrite or not module_file.exists():
            _dump_yaml(module_file, build_module_yaml(module, eps, project, epic))

        # 每个接口一个文件（跟现有框架的目录习惯一致）
        for ep in eps:
            case_file = module_dir / f"{ep.id}.yaml"
            if case_file.exists() and not overwrite:
                continue
            case = build_testcase(ep, project, module, epic, auth_headers)
            _dump_yaml(case_file, case)
            written.append(case_file)

    return written


def _dump_yaml(path: Path, data: dict[str, Any]) -> None:
    """写 YAML（保持中文、可读、顺序稳定）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(
            data,
            f,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
            width=100,
        )


# --------------------------------------------------------------------------
# 导出成 Excel（和 to_excel.py 的表结构一致，可以直接被 excel_loader 读）
# --------------------------------------------------------------------------


def export_excel(
    endpoints: list[Endpoint],
    output: Path,
    project: str = DEFAULT_PROJECT,
    epic: str = DEFAULT_EPIC,
    auth_headers: dict[str, str] | None = None,
) -> int:
    """导出成一个 Excel 文件（表结构跟框架的 Excel 用例完全一致）。

    文件里有 4 张表：

    - **模块信息** —— 模块名 / 标签 / 说明 / 执行顺序
    - **接口定义** —— 每个接口一行（路径、方法、请求头、参数、请求体、断言）
    - **测试用例** —— 每条用例一行，关联到接口
    - **测试数据** —— 参数化数据（这里先留空表）

    这样导出的 Excel **能被框架的 Excel 读取器读回来**，
    可以直接在上面继续改用例。

    Returns:
        写出的用例条数
    """
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    groups = group_by_module(endpoints)

    wb = Workbook()

    # ---- 模块信息 ----
    ws = wb.active
    ws.title = "模块信息"
    ws.append(["模块名", "模块标签", "模块说明", "执行顺序", "标签(多选)"])
    module_names = list(groups.keys())
    for index, module in enumerate(module_names):
        ws.append([
            sanitize_name(module),
            module,
            f"从接口文档导入的模块：{module}",
            500 + index,
            "imported",
        ])

    # ---- 接口定义 ----
    ws = wb.create_sheet("接口定义")
    ws.append(["接口名", "接口路径", "请求方法", "接口描述",
               "请求头", "Query参数", "请求体", "断言"])

    # ---- 测试用例 ----
    ws_case = wb.create_sheet("测试用例")
    ws_case.append(["用例名", "关联接口", "用例说明",
                    "项目", "模块", "场景", "用例标签", "数据组",
                    "请求头", "Query参数", "请求体", "断言"])

    total = 0
    for module, eps in groups.items():
        for ep in eps:
            step = build_step(ep, auth_headers=auth_headers)
            asserts = step.get("assert") or []

            ws.append([
                ep.id,
                ep.path,
                ep.method,
                ep.summary or ep.display_name,
                _kv_to_text(step.get("headers")),
                _kv_to_text(step.get("params")),
                _json_to_text(step.get("data")),
                _assert_to_text(asserts),
            ])

            ws_case.append([
                ep.id,
                ep.id,
                ep.summary or ep.display_name,
                epic,
                module,
                ep.summary or ep.display_name,
                "api",
                "",
                _kv_to_text(step.get("headers")),
                _kv_to_text(step.get("params")),
                _json_to_text(step.get("data")),
                _assert_to_text(asserts),
            ])
            total += 1

    # ---- 测试数据（先留空表）----
    ws_data = wb.create_sheet("测试数据")
    ws_data.append(["数据组", "场景名"])

    # ---- 样式 ----
    for sheet in wb.worksheets:
        for cell in sheet[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="4472C4")
            cell.alignment = Alignment(vertical="center", horizontal="center")
        for idx in range(1, sheet.max_column + 1):
            sheet.column_dimensions[get_column_letter(idx)].width = 22
        for row in sheet.iter_rows(min_row=2):
            for cell in row:
                cell.alignment = Alignment(vertical="top", wrap_text=True)
        sheet.freeze_panes = "A2"

    output.parent.mkdir(parents=True, exist_ok=True)
    wb.save(output)
    return total


def _kv_to_text(data: Any) -> str:
    if not isinstance(data, dict) or not data:
        return ""
    return "\n".join(f"{k}: {v}" for k, v in data.items())


def _json_to_text(data: Any) -> str:
    if data is None or data == "":
        return ""
    if isinstance(data, str):
        return data
    return json.dumps(data, ensure_ascii=False, indent=2)


def _assert_to_text(asserts: Any) -> str:
    if not isinstance(asserts, list) or not asserts:
        return ""
    lines = []
    for item in asserts:
        if not isinstance(item, dict):
            continue
        parts = [str(item.get("type", "")), str(item.get("field", ""))]
        if "expected" in item:
            parts.append(str(item["expected"]))
        lines.append(" | ".join(parts))
    return "\n".join(lines)
