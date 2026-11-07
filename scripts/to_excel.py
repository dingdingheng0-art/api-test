"""把 YAML 用例导出成 Excel。

方便把现有的 YAML 用例迁到 Excel 维护。

用法::

    python scripts/to_excel.py --module statistics
    python scripts/to_excel.py --module statistics --output 别的名字.xlsx
    python scripts/to_excel.py --all        # 所有模块都导出
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

from utils.paths import CASES_ROOT, PROJECT_ROOT  # noqa: E402
MODULE_HEADERS = ["模块名", "模块标签", "模块说明", "执行顺序", "标签(多选)"]
API_HEADERS = [
    "接口名", "接口路径", "请求方法", "接口描述",
    "请求头", "Query参数", "请求体", "断言",
]
CASE_HEADERS = [
    "用例名", "关联接口", "用例说明",
    "项目", "模块", "场景", "用例标签", "数据组",
    "请求头", "Query参数", "请求体", "断言",
]
DATA_HEADERS = ["数据组", "场景名"]


def _kv_to_text(data: Any) -> str:
    """字典 → 「键: 值」多行文本。"""
    if not isinstance(data, dict) or not data:
        return ""
    return "\n".join(f"{k}: {v}" for k, v in data.items())


def _assert_to_text(asserts: Any) -> str:
    """断言列表 → 「类型 | 字段 | 期望值」多行文本。"""
    if not isinstance(asserts, list) or not asserts:
        return ""
    lines = []
    for item in asserts:
        if not isinstance(item, dict):
            continue
        parts = [str(item.get("type", "")), str(item.get("field", ""))]
        if "expected" in item:
            parts.append(str(item["expected"]))
        if "container" in item:
            parts.append(str(item["container"]))
        lines.append(" | ".join(parts))
    return "\n".join(lines)


def export_module(module_dir: Path, output: Path) -> int:
    """导出一个模块目录下的 YAML 用例。"""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    from testcases.case_loader import load_cases_from_file
    from utils.cases.module_manager import ModuleManager

    project = module_dir.parent.name
    module = module_dir.name

    # 模块信息
    meta = ModuleManager.get_module_config(project, module)

    wb = Workbook()

    # ---- 模块信息 ----
    ws = wb.active
    ws.title = "模块信息"
    ws.append(MODULE_HEADERS)
    ws.append([
        meta.get("name", module),
        meta.get("label", module),
        meta.get("description", ""),
        meta.get("order", ""),
        ", ".join(meta.get("marks") or []),
    ])

    # ---- 接口定义 / 测试用例 ----
    api_rows: list[list[Any]] = []
    case_rows: list[list[Any]] = []
    data_blocks: list[dict[str, Any]] = []

    for yaml_file in sorted(module_dir.glob("*.yaml")):
        if yaml_file.name == "module.yaml":
            continue
        try:
            cases = load_cases_from_file(yaml_file, module_dir.parent.parent)
        except Exception:  # noqa: BLE001
            continue

        for case_data in cases:
            case = case_data["testcase"]
            allure = case.get("allure") or {}

            for step in case.get("steps", []):
                api_name = step.get("id") or yaml_file.stem
                api_rows.append([
                    api_name,
                    step.get("path", ""),
                    step.get("method", "GET"),
                    step.get("description", ""),
                    _kv_to_text(step.get("headers")),
                    _kv_to_text(step.get("params")),
                    step.get("data") if isinstance(step.get("data"), str)
                    else (str(step.get("data")) if step.get("data") else ""),
                    _assert_to_text(step.get("assert")),
                ])

                case_rows.append([
                    case.get("name", ""),
                    api_name,
                    case.get("description", ""),
                    allure.get("epic", ""),
                    allure.get("feature", ""),
                    allure.get("story", ""),
                    ", ".join(case.get("marks") or []),
                    "",  # 数据组
                    "", "", "", "",
                ])

            # 参数化 → 测试数据表
            #
            # 用 expand_case 把所有参数化写法（场景列表 / 维度字典 / 外部文件）
            # 统一展开成场景列表 —— 维度字典是笛卡尔积，展开后才对应 Excel 的行
            try:
                from utils.cases.param_expander import expand_case

                expanded = expand_case(case_data, yaml_file)
            except Exception:  # noqa: BLE001
                expanded = []

            if len(expanded) > 1:
                group = f"{case.get('name', yaml_file.stem)}-数据"
                keys: list[str] = []
                scenes: list[tuple[str, dict[str, Any]]] = []
                for item in expanded:
                    values = {
                        k: v for k, v in item.params.items() if k != "__param_name__"
                    }
                    scenes.append((item.param_name, values))
                    for key in values:
                        if key not in keys:
                            keys.append(key)
                data_blocks.append({"keys": keys, "scenes": scenes, "group": group})
                case_rows[-1][7] = group

    # ---- 写接口定义 ----
    ws = wb.create_sheet("接口定义")
    ws.append(API_HEADERS)
    for row in api_rows:
        ws.append(row)

    # ---- 写测试用例 ----
    ws = wb.create_sheet("测试用例")
    ws.append(CASE_HEADERS)
    for row in case_rows:
        ws.append(row)

    # ---- 写测试数据 ----
    #
    # 每个数据组的参数名可能不一样，所以每个组单独一张 Sheet。
    # Sheet 名 = 数据组名，用例表里的「数据组」列填这个名。
    for index, block in enumerate(data_blocks):
        keys = block["keys"]
        group = block["group"]
        ws = wb.create_sheet(group[:31] if group else f"数据{index + 1}")
        ws.append([DATA_HEADERS[0], DATA_HEADERS[1], *keys])
        for name, values in block["scenes"]:
            row = [group, name]
            for key in keys:
                row.append(values.get(key, ""))
            ws.append(row)

    if not data_blocks:
        ws = wb.create_sheet("测试数据")
        ws.append(DATA_HEADERS)

    # 样式
    for sheet in wb.worksheets:
        for cell in sheet[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="4472C4")
            cell.alignment = Alignment(vertical="center", horizontal="center")
        for idx in range(1, sheet.max_column + 1):
            sheet.column_dimensions[get_column_letter(idx)].width = 20
        for row in sheet.iter_rows(min_row=2):
            for cell in row:
                cell.alignment = Alignment(vertical="top", wrap_text=True)
        sheet.freeze_panes = "A2"

    output.parent.mkdir(parents=True, exist_ok=True)
    wb.save(output)
    return len(case_rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="YAML 用例导出成 Excel")
    parser.add_argument("--module", help="要导出的模块名（在 cases/<项目>/ 下）")
    parser.add_argument("--all", action="store_true", help="导出所有模块")
    parser.add_argument("--output", help="输出文件名（默认 <模块>.xlsx）")
    parser.add_argument(
        "--project",
        default="",
        help="项目名（默认用 configs/config.yaml 里的 default_project）",
    )
    args = parser.parse_args()

    if not args.module and not args.all:
        print("要指定 --module 或 --all")
        return 1

    # 源：YAML 用例目录（不管当前是不是 YAML 模式，都从这儿读）
    # 目标：Excel 用例目录（产出到会被扫描到的地方）
    from utils.cases.case_source import current_excel_dir

    source_root = CASES_ROOT
    target_root = current_excel_dir()

    from utils.config.project_profile import ProjectRegistry

    project = args.project or ProjectRegistry.default_project()

    modules: list[Path] = []
    if args.all:
        modules = sorted(
            d for d in source_root.rglob("*")
            if d.is_dir() and list(d.glob("*.yaml"))
        )
    else:
        target = source_root / project / args.module
        if not target.exists():
            print(f"模块目录不存在: {target}")
            return 1
        modules = [target]

    total = 0
    for module_dir in modules:
        # 输出到 Excel 目录下的对应位置
        rel = module_dir.relative_to(source_root)
        out_dir = target_root / rel
        out_dir.mkdir(parents=True, exist_ok=True)
        output = out_dir / f"{args.output or module_dir.name}.xlsx"
        try:
            count = export_module(module_dir, output)
        except Exception as exc:  # noqa: BLE001
            print(f"{module_dir.name} 导出失败: {exc}")
            continue
        total += count
        print(f"{module_dir.name}: {count} 条用例 -> {output}")

    print(f"\n共导出 {total} 条用例")
    return 0


if __name__ == "__main__":
    sys.exit(main())
