"""盘点：哪些用例**应该**有落库断言，实际却没有。

**为什么单独做这个**

``survey_assertions.py`` 只回答"断言弱不弱"（是不是只有 status_code + success）。
它答不了另一个问题：**这条用例调的是写接口，但断言全在响应体上，数据到底
有没有落库根本没验。** HTTP 200 + ``success=true`` 只说明服务端没抛异常 ——
插入被唯一键冲突吞掉、更新影响了 0 行、删除只清了缓存，都是这种响应。

**判定规则**

============  ================================================
用例类型      该有的落库断言
============  ================================================
POST 新增     库里有这条（按名字 COUNT == 1）
PUT  修改     按主键查，名字是改后的
DELETE 删除   按主键查不到（软删表查 delete_time IS NULL 为 0）
GET  详情     按主键查得到（证明数据真实存在，不只是响应回显）
============  ================================================

**不判的**：分页/列表查询（GET 且路径含 page/list）—— 它们验的是
"接口能返回"，落库断言意义不大；以及配置里标了 ``write: False`` 的模块
（写接口本身在环境上就报 A00005，见 [[mall4j-env-defects]]）。

**位置也算问题**：落库断言只该放主步骤（或前置）。写在「后置」的**验证类
步骤**（比如「查数据」GET）里会单独列为「放错位置」—— 后置只做清理，断言
放那里失败会被报成 teardown 错误，主步骤先失败时还会多一条误导性失败。
后置 **DELETE 清理步骤**上的「删后查不到」是清理验证，不在此列。

用法::

    python tools/maint/audit_db_assertions.py            # 只看缺哪些
    python tools/maint/audit_db_assertions.py --detail   # 连 SQL 一起打出来
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.stdout.reconfigure(encoding="utf-8")

import openpyxl

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.maint.add_db_assertions import MODULES, SHEET  # noqa: E402

CASE_ROOT = PROJECT_ROOT / "testcases" / "cases_excel" / "mall4j"

# 这些断言类型算"查了库"（扁平格式统一记成 "db"，见 collect_assert_types）
DB_ASSERT_TYPES = {
    "db",
    "db_query",
    "db_query_exists",
    "db_query_not_exists",
    "db_query_count",
    "db_query_gt",
    "db_query_gte",
    "db_query_lt",
    "db_query_lte",
    "db_query_in",
    "db_query_not_in",
}


def column_index(ws: Any, name: str) -> int | None:
    for i in range(1, ws.max_column + 1):
        if str(ws.cell(1, i).value or "").strip() == name:
            return i
    return None


def read_json(value: Any) -> Any:
    if isinstance(value, (list, dict)):
        return value
    if not value:
        return None
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return None


def _is_cleanup_step(item: Any) -> bool:
    """后置步骤里，哪些是纯清理动作。

    清理步骤（DELETE / factory_cleanup / db）上的落库断言是在验证"清理
    有没有做干净"，属于后置自己的职责，不算放错位置。只有「查数据」这类
    验证步骤上的落库断言才该挪回主步骤。
    """
    if not isinstance(item, dict):
        return False
    op = str(item.get("operation_type") or "").lower()
    if op in ("factory", "factory_cleanup", "db"):
        return True
    return str(item.get("method") or "").upper() == "DELETE"


def collect_assert_types(obj: Any, out: set[str]) -> None:
    """把断言树里所有断言种类收进来（含前置/后置/嵌套）。

    **Excel 里存的是扁平格式**，判据是 ``check`` 字段
    （``{"check": "db", "sql": ...}``），不是内部格式的 ``type``。
    只认 ``type`` 的话，每条用例都会显示"没有任何断言" —— 那是审计脚本
    自己的 bug，不是用例的问题。两种格式都收，兼容 YAML 用例。
    """
    if isinstance(obj, list):
        for item in obj:
            collect_assert_types(item, out)
    elif isinstance(obj, dict):
        # 扁平格式：check = status_code / body / header / db / redis / mq
        check = obj.get("check")
        if isinstance(check, str):
            if check == "db":
                # 扁平格式不区分 db_query / db_query_not_exists，
                # 统一记成 db，够判"有没有查库"了
                out.add("db")
            else:
                out.add(check)
        # 内部格式：type = equal / exists / db_query ...
        kind = obj.get("type")
        if isinstance(kind, str):
            out.add(kind)
        for key in ("assert", "asserts", "steps", "setup", "teardown"):
            if key in obj:
                collect_assert_types(obj[key], out)


#: POST 但**不落业务数据**的接口 —— 认证 / 验证码 / 上传这类。
#:
#: 按方法分类会把它们误判成"新增"，于是审计永远报这几条缺口。
#: 它们写的是会话、缓存、文件，没有对应的业务表可查；硬要补一条
#: ``SELECT ... FROM tz_xxx`` 反而是在造一个假的断言。
NON_PERSISTING = (
    "/token",
    "/captcha",
    "/login",
    "/logout",
    "/upload",
    "/file",
    "/sms",
    "/notify",
)


def classify(method: str, path: str, case_name: str) -> str:
    """判断这条用例该有哪种落库断言；返回空串表示不适用。"""
    path_l = path.lower()
    if any(marker in path_l for marker in NON_PERSISTING):
        return ""
    if method == "GET":
        # 详情才有意义；列表/分页不判
        if "page" in path_l or "list" in path_l:
            return ""
        if "/info/" in path_l or path_l.rstrip("/").endswith("info"):
            return "detail"
        return ""
    if method == "POST":
        return "create"
    if method == "PUT":
        return "update"
    if method == "DELETE":
        return "delete"
    return ""


REQUIREMENT = {
    "create": "新增后库里有这条（COUNT == 1）",
    "update": "按主键查，名字是改后的",
    "delete": "按主键查不到（软删表查 delete_time IS NULL）",
    "detail": "按主键查得到",
}


def audit(detail: bool) -> int:
    findings: list[dict[str, Any]] = []
    # 落库断言放错位置：写在「后置」里。后置只做清理 —— 断言放那里，
    # 失败会被报成 teardown 错误，主步骤先失败时还会多一条误导性失败。
    misplaced: list[dict[str, Any]] = []

    for wb_path in sorted(CASE_ROOT.glob("*/*.xlsx")):
        module = wb_path.parent.name
        try:
            wb = openpyxl.load_workbook(wb_path, read_only=True, data_only=True)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! 读不了 {wb_path.name}: {exc}")
            continue
        if SHEET not in wb.sheetnames:
            continue
        ws = wb[SHEET]

        cols = {
            name: column_index(ws, name)
            for name in ("接口", "请求方法", "断言", "前置", "后置", "用例状态", "用例名称")
        }
        if any(v is None for v in (cols["接口"], cols["请求方法"], cols["用例状态"])):
            continue

        for row in range(2, ws.max_row + 1):
            status = str(ws.cell(row, cols["用例状态"]).value or "").strip()
            if status != "debugged":
                continue

            method = str(ws.cell(row, cols["请求方法"]).value or "").strip().upper()
            path = str(ws.cell(row, cols["接口"]).value or "").strip()
            case_id = str(ws.cell(row, 1).value or "").strip()
            name = str(
                ws.cell(row, cols["用例名称"]).value if cols["用例名称"] else ""
            ).strip()

            need = classify(method, path, name)
            if not need:
                continue

            # 模块在配置里标了 write: False —— 写接口本身有问题，不要求
            cfg = MODULES.get(module) or {}
            if need != "detail" and cfg.get("write") is False:
                continue

            # 覆盖率只认主步骤和前置里的落库断言。后置里的单独算「放错位置」，
            # 既不能拿来抵覆盖率，也不能装作没看见。
            types: set[str] = set()
            for key in ("断言", "前置"):
                collect_assert_types(read_json(ws.cell(row, cols[key]).value), types)

            teardown_types: set[str] = set()
            teardown_items = read_json(ws.cell(row, cols["后置"]).value) or []
            if not isinstance(teardown_items, list):
                teardown_items = [teardown_items]
            for item in teardown_items:
                if _is_cleanup_step(item):
                    continue
                collect_assert_types(item, teardown_types)
            if teardown_types & DB_ASSERT_TYPES:
                misplaced.append(
                    {
                        "module": module,
                        "case_id": case_id,
                        "name": name or path,
                        "method": method,
                        "path": path,
                    }
                )
                # 有落库断言，只是放错位置 —— 该做的是挪，不是再补一条
                continue

            if types & DB_ASSERT_TYPES:
                continue

            findings.append(
                {
                    "module": module,
                    "case_id": case_id,
                    "name": name or path,
                    "method": method,
                    "path": path,
                    "need": need,
                    "table": cfg.get("table", "?"),
                    "pk": cfg.get("pk", "?"),
                    "has_db": False,
                    "types": sorted(types),
                }
            )

        wb.close()

    if not findings:
        print("所有该有落库断言的用例都补上了。")
    else:
        print(f"缺少落库断言的用例：{len(findings)} 条\n")
        by_module: dict[str, list[dict[str, Any]]] = {}
        for item in findings:
            by_module.setdefault(item["module"], []).append(item)

        for module, items in sorted(by_module.items()):
            table = items[0]["table"]
            print(f"{module}  →  {table}  （{len(items)} 条）")
            for item in items:
                print(
                    f"  {item['case_id']:8} {item['method']:6} {item['path']:36}"
                    f" {item['need']:6} {item['name'][:24]}"
                )
                if detail:
                    print(f"           要求: {REQUIREMENT[item['need']]}")
                    print(f"           现有断言类型: {', '.join(item['types']) or '（无）'}")
            print()

        # 按缺口类型汇总，便于决定先补哪类
        from collections import Counter

        print("按缺口类型：")
        for need, count in Counter(i["need"] for i in findings).most_common():
            print(f"  {need:8} {count:3}  {REQUIREMENT[need]}")

    if misplaced:
        print(f"\n落库断言放在后置（应挪回主步骤）：{len(misplaced)} 条")
        for item in misplaced:
            print(
                f"  {item['case_id']:8} {item['method']:6} {item['path']:36}"
                f" {item['name'][:24]}"
            )
        print("  改法：把后置步骤 assert 里的 check=db 断言移到主步骤「断言」列；"
              "后置只保留清理。")

    return 1 if findings or misplaced else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="盘点缺落库断言的用例")
    parser.add_argument("--detail", action="store_true", help="连现有断言类型一起打出来")
    args = parser.parse_args()
    return audit(args.detail)


if __name__ == "__main__":
    raise SystemExit(main())
