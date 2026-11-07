"""生成 Excel 用例模板。

用法::

    python scripts/new_case.py --module statistics                    # 给默认项目建
    python scripts/new_case.py --module order --project my_shop       # 给别的系统建

生成的模板带**填好的示例**、**下拉选项**和**单元格批注（鼠标悬停可见）**，
照着改就行。

**多系统兼容**：模板里的鉴权头、变量名都按项目档案生成 ——
换系统不用改代码，也不用手动改示例里的 ``{{项目名.token}}``。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

# 本文件在 scripts/ 下，先让项目根进 sys.path 才能 import utils/framework
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from utils.paths import PROJECT_ROOT  # noqa: E402


def current_excel_dir():
    """Excel 用例目录（跟"用例来源"开关走）。"""
    from utils.cases.case_source import current_excel_dir as _dir

    return _dir()


# --------------------------------------------------------------------------
# 各张表的表头 + 示例 + 批注
# --------------------------------------------------------------------------

MODULE_HEADERS = ["模块名", "模块标签", "模块说明", "执行顺序", "标签(多选)"]
MODULE_EXAMPLE = ["statistics", "首页统计", "首页统计、运营报表", 50, "statistics"]
MODULE_NOTES = {
    "模块名": "模块名，要和所在目录名一致",
    "模块标签": "报告里显示的模块名（简短）",
    "模块说明": "这个模块是干什么的（只给自己看）",
    "执行顺序": "数字小的先跑（登录模块建议 10）",
    "标签(多选)": "用逗号分隔。会变成 pytest 标记，比如 api, p1",
}

API_HEADERS = [
    "接口名", "接口路径", "请求方法", "接口描述",
    "请求头", "Query参数", "请求体", "断言",
]
API_EXAMPLE = [
    "order_statistics",
    "/api/statistics/order",
    "GET",
    "查订单统计",
    # 这一格生成模板时会被按项目档案替换（见 project_auth_headers）
    "Authorization: {{项目名.token}}",
    "searchType: {{searchType}}",
    "",
    "equal | code | 200\nequal | success | true\nis not None | result",
]
API_NOTES = {
    "接口名": "接口的唯一名字，测试用例表里用它关联",
    "接口路径": "只写路径，不带域名",
    "请求方法": "从下拉里选",
    "接口描述": "这个接口是干什么的",
    "请求头": "一行一个，格式「键: 值」",
    "Query参数": "一行一个，格式「键: 值」",
    "请求体": "填 JSON，例如 {\"name\": \"abc\"}",
    "断言": "一行一条，格式「类型 | 字段 | 期望值」。类型见「说明」表",
}

CASE_HEADERS = [
    "用例名", "关联接口", "用例说明",
    "模块", "场景", "用例标签", "数据组",
    "请求头", "Query参数", "请求体", "断言",
    "用例状态",
]
CASE_EXAMPLE = [
    "订单统计-按周期",
    "order_statistics",
    "订单统计（不同统计周期）",
    "首页统计",
    "订单统计",
    "api, p1",
    "统计周期",
    "", "", "", "",
    "debugged",
]
CASE_NOTES = {
    "用例名": "用例的唯一名字，会显示在报告里",
    "关联接口": "填「接口定义」表里的接口名",
    "用例说明": "这条用例在验证什么",
    "模块": "报告第二层",
    "场景": "报告第三层",
    "用例标签": "用逗号分隔，会变成 pytest 标记",
    "数据组": "填「测试数据」表里的数据组名，用来参数化",
    "请求头": "留空 = 用接口定义里的；填了 = 覆盖",
    "Query参数": "留空 = 用接口定义里的；填了 = 覆盖",
    "请求体": "留空 = 用接口定义里的；填了 = 覆盖",
    "断言": "留空 = 用接口定义里的；填了 = 覆盖",
    "用例状态": (
        "debugged（调试通过，默认 CI 只跑这些）/ "
        "draft（还没调试）/ disabled（临时下线）/ broken（已知有问题）。"
        "调试好了记得改成 debugged，否则 CI 不会跑它"
    ),
}

DATA_HEADERS = ["数据组", "场景名", "searchType", "pageSize"]
DATA_EXAMPLE = [
    ["统计周期", "近七天", "LAST_SEVEN", "10"],
    ["统计周期", "近三十天", "LAST_THIRTY", "20"],
]
DATA_NOTES = {
    "数据组": "用例表里「数据组」列填的名字",
    "场景名": "显示在报告里的场景名，可以不填",
    "searchType": "这一列就是一个参数名，要和接口里的 {{searchType}} 对上",
    "pageSize": "多几列就多几个参数",
}

# 请求方法下拉选项
METHOD_OPTIONS = ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS", "login"]

# 断言类型下拉选项
ASSERT_OPTIONS = [
    "equal", "not equal", "is None", "is not None",
    "exists", "not exists",
    "contains", "not contains", "in", "not in",
    "gt", "gte", "lt", "lte", "length", "type",
    "db_query", "db_query_count", "db_query_exists", "db_query_not_exists",
]


# --------------------------------------------------------------------------
# 按项目生成示例内容
# --------------------------------------------------------------------------

def project_auth_headers(project: str) -> str:
    """按项目档案生成示例里的鉴权头（多行文本）。"""
    from utils.config.project_profile import ProjectRegistry

    profile = ProjectRegistry.get(project)
    if profile is None:
        return f"Authorization: {{{{{project}.token}}}}"

    lines: list[str] = []
    if profile.token_header:
        lines.append(f"{profile.token_header}: {{{{{project}.{profile.token_var}}}}}")
    if profile.session_header:
        lines.append(f"{profile.session_header}: {{{{{project}.{profile.session_var}}}}}")
    return "\n".join(lines)


def project_variable_rows(project: str) -> list[tuple[str, str, str]]:
    """生成"变量写法"说明表里的全局变量那一段。"""
    from utils.config.project_profile import ProjectRegistry

    profile = ProjectRegistry.get(project)
    if profile is None:
        return [
            ("全局变量", f"{{{{{project}.token}}}}", "登录后的 token（框架自动填）"),
        ]

    rows: list[tuple[str, str, str]] = []
    if profile.token_header:
        rows.append(
            (
                "全局变量",
                f"{{{{{project}.{profile.token_var}}}}}",
                f"登录后的 token（框架自动填，放在 {profile.token_header} 头里）",
            )
        )
    if profile.session_header:
        rows.append(
            (
                "全局变量",
                f"{{{{{project}.{profile.session_var}}}}}",
                f"会话标识（放在 {profile.session_header} 头里）",
            )
        )
    return rows


def project_label(project: str) -> str:
    """项目在报告里显示的名字。"""
    from utils.config.project_profile import ProjectRegistry

    profile = ProjectRegistry.get(project)
    return (profile.label if profile and profile.label else project)


# --------------------------------------------------------------------------
# 写 Excel
# --------------------------------------------------------------------------


def _style_header(sheet: Any, headers: list[str]) -> None:
    """表头样式 + 批注 + 下拉。"""
    from openpyxl.comments import Comment
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.datavalidation import DataValidation

    header_fill = PatternFill("solid", fgColor="4472C4")
    for col_idx, header in enumerate(headers, start=1):
        cell = sheet.cell(row=1, column=col_idx)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = header_fill
        cell.alignment = Alignment(vertical="center", horizontal="center")

        letter = get_column_letter(col_idx)

        # 批注（鼠标悬停可见）
        note = NOTES_BY_SHEET.get(sheet.title, {}).get(header)
        if note:
            cell.comment = Comment(note, "接口自动化框架")

        # 下拉选项
        if header == "请求方法":
            dv = DataValidation(
                type="list",
                formula1='"' + ",".join(METHOD_OPTIONS) + '"',
                allow_blank=True,
                showErrorMessage=False,
            )
            sheet.add_data_validation(dv)
            dv.add(f"{letter}2:{letter}200")


def _write_sheet(
    sheet: Any,
    headers: list[str],
    example: list[Any],
    widths: dict[str, int] | None = None,
) -> None:
    """写一张表：表头 + 示例行。"""
    from openpyxl.styles import Alignment
    from openpyxl.utils import get_column_letter

    sheet.append(headers)
    _style_header(sheet, headers)

    # 示例行（第 2 行）
    sheet.append(example)
    for col_idx in range(1, len(headers) + 1):
        sheet.cell(row=2, column=col_idx).alignment = Alignment(
            vertical="top", wrap_text=True
        )

    # 列宽
    for idx, header in enumerate(headers, start=1):
        width = (widths or {}).get(header, 18)
        sheet.column_dimensions[get_column_letter(idx)].width = width

    # 冻结首行
    sheet.freeze_panes = "A2"

    # 自动筛选
    sheet.auto_filter.ref = f"A1:{get_column_letter(len(headers))}2"


# 批注文案表（按 Sheet 名索引）
NOTES_BY_SHEET: dict[str, dict[str, str]] = {
    "模块信息": MODULE_NOTES,
    "接口定义": API_NOTES,
    "测试用例": CASE_NOTES,
    "测试数据": DATA_NOTES,
}


def build_template(module: str, project: str = "", force: bool = False) -> Path:
    """生成模板文件，返回路径。

    Args:
        module: 模块名（会在用例目录的 ``<项目>/<模块>/`` 下建目录）
        project: 项目名（决定鉴权头示例、变量名、报告里显示的名字）。
                 不传就用配置里的默认项目（``default_project``）。
        force: 文件已存在时是否覆盖（默认**不覆盖**，避免误删已有用例）

    Raises:
        FileExistsError: 文件已存在且没传 ``force``
    """
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    if not project:
        from utils.config.project_profile import ProjectRegistry

        project = ProjectRegistry.default_project()

    # 模板写到"当前生效的用例目录"——
    # 现在默认是 Excel 维护，所以落在 cases_excel/ 下。
    module_dir = current_excel_dir() / project / module
    module_dir.mkdir(parents=True, exist_ok=True)

    target = module_dir / f"{module}.xlsx"

    # ⚠️ 已存在的用例文件**不能**直接覆盖 ——
    # 那里面可能有几十条用例，一覆盖就没了（而且这个目录不是 git 仓库，救不回来）。
    # 要重新生成请显式加 --force。
    if target.exists() and not force:
        raise FileExistsError(
            f"文件已存在：{target}\n"
            "（里面可能已经有写好的用例，不覆盖它。）\n"
            "确实要重新生成模板，加 --force。"
        )

    # 示例内容按项目走 —— 换系统不用手动改示例
    api_example = list(API_EXAMPLE)
    api_example[4] = project_auth_headers(project)   # 请求头

    case_example = list(CASE_EXAMPLE)

    wb = Workbook()

    # ---- 模块信息 ----
    ws = wb.active
    ws.title = "模块信息"
    _write_sheet(
        ws, MODULE_HEADERS, MODULE_EXAMPLE,
        widths={"模块名": 16, "模块标签": 14, "模块说明": 30, "执行顺序": 12, "标签(多选)": 18},
    )

    # ---- 接口定义 ----
    ws = wb.create_sheet("接口定义")
    _write_sheet(
        ws, API_HEADERS, api_example,
        widths={
            "接口名": 20, "接口路径": 32, "请求方法": 12, "接口描述": 24,
            "请求头": 38, "Query参数": 26, "请求体": 28, "断言": 42,
        },
    )

    # ---- 测试用例 ----
    ws = wb.create_sheet("测试用例")
    _write_sheet(
        ws, CASE_HEADERS, case_example,
        widths={
            "用例名": 20, "关联接口": 20, "用例说明": 26,
            "模块": 12, "场景": 16,
            "用例标签": 14, "数据组": 14,
            "请求头": 24, "Query参数": 20, "请求体": 24, "断言": 30,
        },
    )

    # ---- 测试数据 ----
    ws = wb.create_sheet("测试数据")
    _write_sheet(
        ws, DATA_HEADERS, DATA_EXAMPLE[0],
        widths={"数据组": 16, "场景名": 16, "searchType": 16, "pageSize": 12},
    )
    for row in DATA_EXAMPLE[1:]:
        ws.append(row)

    # ---- 说明 ----
    ws = wb.create_sheet("说明")
    ws.column_dimensions["A"].width = 22
    ws.column_dimensions["B"].width = 34
    ws.column_dimensions["C"].width = 44

    ws["A1"] = "断言类型"
    ws["B1"] = "写法"
    ws["C1"] = "说明"
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="4472C4")

    for row in [
        ("equal", "equal | code | 200", "等于"),
        ("not equal", "not equal | code | 500", "不等于"),
        ("is None", "is None | result.accessToken", "是 None"),
        ("is not None", "is not None | result.accessToken", "不是 None"),
        ("exists", "exists | data.records[0].id", "字段存在（哪怕是 null）"),
        ("not exists", "not exists | data.deletedAt", "字段不存在"),
        ("contains", "contains | message | 成功", "包含（字符串）"),
        ("not contains", "not contains | message | 失败", "不包含"),
        ("in", "in | result.status | [1, 2, 3]", "在列表里（第 4 段填列表）"),
        ("not in", "not in | result.status | [9]", "不在列表里"),
        ("gt", "gt | result.count | 10", "大于"),
        ("gte", "gte | result.count | 10", "大于等于"),
        ("lt", "lt | result.count | 100", "小于"),
        ("lte", "lte | result.count | 100", "小于等于"),
        ("length", "length | result.items | 3", "长度等于"),
        ("type", "type | result | dict", "类型是"),
        ("db_query", "db_query | SELECT status FROM t WHERE id = %s | 1",
         "查库第一列等于（需要配置 db.default）"),
        ("db_query_count", "db_query_count | SELECT id FROM t WHERE name = %s | 1",
         "查库行数等于"),
        ("db_query_not_exists", "db_query_not_exists | SELECT id FROM t WHERE id = %s",
         "查库没有结果（删数据后确认）"),
    ]:
        ws.append(list(row))

    ws.append([])
    ws.append(["变量写法", "例子", "说明"])
    for c in ws[ws.max_row]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="70AD47")

    for row in [
        *project_variable_rows(project),
        ("数据参数", "{{searchType}}", "「测试数据」表里的参数（当前组的值）"),
        ("数据参数", "{{params.searchType}}", "同上，带前缀的写法"),
        ("步骤引用", "{{member_menu.result.0.id}}", "上一步接口的响应字段"),
        ("工具函数", f'{{tools.login_credential("{project}","username")}}', "配置里的账号密码"),
    ]:
        ws.append(list(row))

    ws.append([])
    ws.append(["特殊步骤", "", ""])
    for c in ws[ws.max_row]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="70AD47")
    ws.append(["", '"请求方法" 填 login', "走完整的「抓 uuid → 滑块 → 登录」链路"])
    ws.append(["", '"请求方法" 填 noop', "什么也不做，只跑断言"])

    wb.save(target)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 Excel 用例模板")
    parser.add_argument(
        "--module", required=True, help="模块名（会在 Excel 用例目录下建目录）"
    )
    parser.add_argument(
        "--project",
        default="",
        help="项目名（默认用 configs/config.yaml 里的 default_project）",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="文件已存在时强制覆盖（会丢掉里面已有的用例，慎用）",
    )
    args = parser.parse_args()

    try:
        target = build_template(args.module, project=args.project, force=args.force)
    except ImportError:
        print("需要安装 openpyxl：uv pip install openpyxl")
        return 1
    except FileExistsError as exc:
        print(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"生成失败: {exc}")
        return 1

    print(f"已生成模板: {target}")
    print()
    print("怎么用：")
    print("  1. 打开这个 Excel，第 2 行是示例，照着改")
    print("  2. 每列的说明在表头的批注里（鼠标悬停表头就能看到）")
    print("  3. ⚠️ 「用例状态」列填 debugged，否则 CI 不会跑它")
    print("  4. 填完保存，直接跑：python scripts/run.py --project " + args.project)
    print()
    print("想看填了哪些用例：python scripts/run.py --list")
    return 0


if __name__ == "__main__":
    sys.exit(main())
