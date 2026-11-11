"""把 AI 沙箱里「方法 + 路径」式的用例名称改成现役中文命名。

**为什么要单独改一遍**

``ai/agent/tools/case_io.py`` 现在会在写盘前把命名对齐成
「模块中文名 - 业务动作（场景）」，但这管不了**已经落盘的历史行**。
早期生成的用例把接口文档里的「接口名」直接抄成了业务名，报告里显示::

    [ai admin area] PUT /admin/area - PUT /admin/area（正常）

人根本看不出这条用例测的是什么。重新跑生成器也不行 —— 指纹（方法+路径+
业务名+场景）变了，只会**再追加一批重复用例**，所以得原地改名。

**只动 ``ai_`` 沙箱**

现役模块的用例名是人工维护的，本工具一行都不碰。目录名不以 ``ai_`` 开头
直接跳过。

**先看后改**

默认只打印「旧名 → 新名」，加 ``--apply`` 才落盘。落盘前把原文件备份到
``.cache/backup/rename_ai_<时间戳>/``，写完重新读一遍校验，没改成就恢复。
顺带给沙箱模块补 ``module.yaml``（报告左侧树的中文名）。

用法::

    python tools/maint/rename_ai_cases.py             # 只看
    python tools/maint/rename_ai_cases.py --apply     # 真改
    python tools/maint/rename_ai_cases.py --apply --project mall4j
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path
from typing import Any

sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai.agent.tools import case_io  # noqa: E402
from utils.logs.log_manager import log  # noqa: E402

SHEET_FLAT = case_io.SHEET_FLAT
BACKUP_ROOT = PROJECT_ROOT / ".cache" / "backup"

#: 这几列决定用例在报告里的名字
NAMED_COLUMNS = ("接口名称", "用例标题", "用例名称")


def _project_dirs(project: str) -> list[Path]:
    from utils.cases.case_source import current_excel_dir

    root = current_excel_dir()
    if not root.exists():
        return []
    if project:
        return [root / project] if (root / project).is_dir() else []
    return [p for p in root.iterdir() if p.is_dir()]


def _sandbox_files(project: str) -> list[tuple[str, Path, Path]]:
    """``ai_`` 沙箱模块的 Excel 文件：``[(项目, 模块目录, 文件)]``。"""
    out: list[tuple[str, Path, Path]] = []
    for proj_dir in _project_dirs(project):
        for module_dir in sorted(proj_dir.iterdir()):
            if not module_dir.is_dir() or not module_dir.name.startswith("ai_"):
                continue
            for file_path in sorted(module_dir.glob("*.xlsx")):
                out.append((proj_dir.name, module_dir, file_path))
    return out


def _header_index(sheet: Any) -> dict[str, int]:
    return {
        str(cell.value).strip(): idx
        for idx, cell in enumerate(sheet[1], start=1)
        if cell.value is not None
    }


def _cell_value(row: tuple[Any, ...], headers: dict[str, int], name: str) -> str:
    idx = headers.get(name)
    if not idx or idx > len(row):
        return ""
    return str(row[idx - 1].value or "").strip()


def _needs_rename(full_name: str, old_api: str, new_api: str) -> bool:
    """``用例名称`` 是不是还带着坏的旧名字。"""
    if not full_name:
        return True
    if case_io._looks_like_method_path(full_name):
        return True
    if old_api and old_api != new_api and old_api in full_name:
        return True
    # 业务名已经是中文了，全名里却一个汉字都没有 —— 也是旧格式
    if case_io._CJK_RE.search(new_api) and not case_io._CJK_RE.search(full_name):
        return True
    return False


def _module_label(project: str, module: str, path: str) -> str:
    """沙箱模块**将来**的中文名（只算不写）。

    用例名称里的模块前缀要按它拼 —— 直接查 ``ModuleManager`` 只能拿到
    ``ai admin area``，因为 ``module.yaml`` 还没写下去。
    """
    try:
        from ai.capabilities.case_gen import _sandbox_module_label

        return str(_sandbox_module_label(project, module, {"path": path}, write=False))
    except Exception as exc:  # noqa: BLE001 - 算不出来就用模块名
        log.debug("算沙箱模块中文名失败: %s", exc)
        return module


def _first_path(sheet: Any, headers: dict[str, int]) -> str:
    for row in sheet.iter_rows(min_row=2):
        path = _cell_value(row, headers, "接口")
        if path:
            return path
    return ""


def plan_file(project: str, module_dir: Path, file_path: Path) -> list[dict[str, Any]]:
    """算出一个文件里每行该怎么改（不改文件）。"""
    import openpyxl

    module = module_dir.name
    workbook = openpyxl.load_workbook(file_path)
    try:
        if SHEET_FLAT not in workbook.sheetnames:
            log.warning("%s 不是扁平格式，跳过", file_path)
            return []
        sheet = workbook[SHEET_FLAT]
        headers = _header_index(sheet)
        if not all(name in headers for name in NAMED_COLUMNS):
            log.warning("%s 缺少命名列，跳过", file_path)
            return []

        label = _module_label(project, module, _first_path(sheet, headers))

        changes: list[dict[str, Any]] = []
        for row_number, row in enumerate(sheet.iter_rows(min_row=2), start=2):
            case_id = _cell_value(row, headers, "case_id")
            path = _cell_value(row, headers, "接口")
            method = _cell_value(row, headers, "请求方法") or "GET"
            if not path:
                continue
            old_api = _cell_value(row, headers, "接口名称")
            old_title = _cell_value(row, headers, "用例标题")
            old_name = _cell_value(row, headers, "用例名称")

            aligned = case_io.align_case_naming(
                project, module, path, method, old_api, old_title, old_name
            )
            new_api = aligned["api_name"]
            new_title = aligned["title"]
            new_name = aligned["case_name"]
            if not new_name or _needs_rename(old_name, old_api, new_api):
                new_name = case_io._default_case_name(
                    project, module, new_api, new_title, label=label
                )

            if (new_api, new_title, new_name) == (old_api, old_title, old_name):
                continue
            changes.append(
                {
                    "row": row_number,
                    "case_id": case_id,
                    "method": method,
                    "path": path,
                    "old": {
                        "接口名称": old_api,
                        "用例标题": old_title,
                        "用例名称": old_name,
                    },
                    "new": {
                        "接口名称": new_api,
                        "用例标题": new_title,
                        "用例名称": new_name,
                    },
                    "aligned": bool(new_api and case_io._CJK_RE.search(new_api)),
                }
            )
        return changes
    finally:
        workbook.close()


def apply_file(
    project: str,
    module_dir: Path,
    file_path: Path,
    changes: list[dict[str, Any]],
    backup_dir: Path,
) -> None:
    """备份 → 写 → 复验，失败恢复。"""
    import openpyxl

    target = backup_dir / project / module_dir.name
    target.mkdir(parents=True, exist_ok=True)
    backup = target / file_path.name
    shutil.copy2(file_path, backup)

    module = module_dir.name
    workbook = openpyxl.load_workbook(file_path)
    failed = False
    try:
        sheet = workbook[SHEET_FLAT]
        headers = _header_index(sheet)
        for change in changes:
            row = change["row"]
            for name in NAMED_COLUMNS:
                idx = headers.get(name)
                if idx:
                    sheet.cell(row, idx).value = change["new"][name]
        workbook.save(file_path)
    except Exception:
        failed = True
        raise
    finally:
        workbook.close()
        # 先关掉工作簿再恢复：Windows 上句柄没释放时 copy 会失败
        if failed:
            shutil.copy2(backup, file_path)

    _verify(file_path, changes, backup)

    # 沙箱模块补中文 label：报告左侧树按 module.yaml 显示
    first_path = changes[0]["path"] if changes else ""
    case_io._ensure_sandbox_label(project, module, first_path)


def _verify(
    file_path: Path,
    changes: list[dict[str, Any]],
    backup: Path,
) -> None:
    """写完重新读一遍，有一格没落成就不算成功。"""
    import openpyxl

    workbook = openpyxl.load_workbook(file_path)
    try:
        sheet = workbook[SHEET_FLAT]
        headers = _header_index(sheet)
        rows = {
            row_number: row
            for row_number, row in enumerate(
                sheet.iter_rows(min_row=2), start=2
            )
        }
        for change in changes:
            row = rows.get(change["row"])
            for name in NAMED_COLUMNS:
                expected = change["new"][name]
                actual = (
                    _cell_value(row, headers, name)
                    if row is not None and name in headers
                    else ""
                )
                if actual != expected:
                    shutil.copy2(backup, file_path)
                    raise SystemExit(
                        f"{file_path.name} 第 {change['row']} 行「{name}」没改成"
                        f"（期望 {expected!r}，实际 {actual!r}），已恢复原文件"
                    )
    finally:
        workbook.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="把 AI 沙箱用例名称改成现役中文命名")
    parser.add_argument("--project", default="", help="只看某个项目，默认全部")
    parser.add_argument("--apply", action="store_true", help="真的落盘（默认只看）")
    args = parser.parse_args()

    project_filter = str(args.project or "").strip()
    files = _sandbox_files(project_filter)
    if not files:
        print("没有找到 ai_ 沙箱模块的 Excel 用例。")
        return 0

    backup_dir = BACKUP_ROOT / f"rename_ai_{time.strftime('%Y%m%d_%H%M%S')}"
    total = 0
    unresolved = 0
    for project, module_dir, file_path in files:
        changes = plan_file(project, module_dir, file_path)
        if not changes:
            continue
        print(f"\n== {project}/{module_dir.name}/{file_path.name}（{len(changes)} 行）")
        for change in changes:
            if not change["aligned"]:
                unresolved += 1
            print(
                f"  {change['case_id'] or '?':>8}  "
                f"{change['method']:<6} {change['path']}\n"
                f"      用例名称: {change['old']['用例名称']!r}\n"
                f"            -> {change['new']['用例名称']!r}\n"
                f"      接口名称: {change['old']['接口名称']!r} -> "
                f"{change['new']['接口名称']!r}\n"
                f"      用例标题: {change['old']['用例标题']!r} -> "
                f"{change['new']['用例标题']!r}"
            )
        total += len(changes)
        if args.apply:
            apply_file(project, module_dir, file_path, changes, backup_dir)
            print(f"  已落盘（备份: {backup_dir}）")

    print(f"\n共 {total} 行需要调整；其中 {unresolved} 行没找到现役中文名（保持方法+路径）。")
    if unresolved:
        print(
            "没对齐的行说明现役用例里没有「同方法 + 同路径」的中文业务名 ——"
            "要么该方法确实没人写过，要么路径模板对不上，人工看一下再补。"
        )
    if not args.apply:
        print("这是预览。确认无误后加 --apply 真改。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
