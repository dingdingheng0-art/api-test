"""把详情用例里写死的、库里不存在的 id 换成真实存在的 id。

**为什么**：``/xxx/info/<id>`` 这类用例的正例把 id 写死在路径里，
id 一换环境就可能不存在 —— 接口照样返回 ``code=00000``，但 ``data`` 是
``null``，后面所有 ``data.xxx`` 断言全挂。断言本身没错，是**用例数据错了**。

这里按"改前先查库确认"的顺序做：每条 id 都必须先查库确认存在，才写进用例。
查不到就跳过并打印原因，绝不生成必然失败的断言。

用法::

    python tools/maint/fix_detail_ids.py --dry-run
    python tools/maint/fix_detail_ids.py
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

CASE_ROOT = PROJECT_ROOT / "testcases" / "cases_excel"
BACKUP_ROOT = PROJECT_ROOT / ".cache" / "backup"


#: ``(工作簿相对路径, 用例号, 老路径, 新路径, 表名, 主键列, 新 id)``
FIXES: list[tuple[str, str, str, str, str, str, Any]] = [
    (
        "mall4j/admin_area/admin_area.xlsx", "API-173",
        "/admin/area/info/1", "/admin/area/info/110000000000",
        "tz_area", "area_id", 110000000000,
    ),
    (
        "mall4j/sys_config/sys_config.xlsx", "API-040",
        "/sys/config/info/1", "/sys/config/info/79",
        "tz_sys_config", "id", 79,
    ),
]


def row_id_exists(client: Any, table: str, pk: str, value: Any) -> bool:
    """确认这行真的在库里 —— 不在就别改，改了就是造一条必然失败的用例。"""
    count = client.scalar(f"SELECT COUNT(*) FROM {table} WHERE {pk} = %s", [value])
    return bool(count)


def patch_path(ws: Any, case_id: str, old: str, new: str) -> bool:
    """把某条用例的接口路径从 old 换成 new。"""
    header = [c.value for c in ws[1]]
    id_col = header.index("case_id")
    path_col = header.index("接口")
    for row in ws.iter_rows(min_row=2):
        if row[id_col].value != case_id:
            continue
        cell = row[path_col]
        if str(cell.value).strip() != old:
            return False
        cell.value = new
        return True
    return False


def patch_asserts(ws: Any, case_id: str, transform: Any) -> bool:
    """改写某条用例的断言列表（JSON 字符串 → 改后 → 写回）。"""
    import json

    header = [c.value for c in ws[1]]
    id_col = header.index("case_id")
    asserts_col = header.index("断言")
    for row in ws.iter_rows(min_row=2):
        if row[id_col].value != case_id:
            continue
        raw = row[asserts_col].value
        if not raw:
            return False
        asserts = json.loads(raw)
        changed = transform(asserts)
        if changed:
            row[asserts_col].value = json.dumps(changed, ensure_ascii=False)
        return True
    return False


def fix_user_detail_pk_assert(asserts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """API-129：``data.userId`` 服务端不返回（null），改成断言用户名。

    实测 ``/sys/user/info/1`` 返回 ``data.userId: null``（库里 user_id=1 是
    存在的，是服务端没回填这个字段）。断言一个永远 null 的字段没有意义，
    换成同一行里确实有值的 ``data.username``。
    """
    for item in asserts:
        if item.get("jsonpath") == "$.data.userId" and item.get("comparator") == "==":
            item["jsonpath"] = "$.data.username"
            item["expect"] = "admin"
            break
    return asserts


def main() -> int:
    parser = argparse.ArgumentParser(description="修正详情用例里不存在的 id")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不写文件")
    args = parser.parse_args()

    from tools.maint.dump_schema import resolve_db_config
    from verify.db import DatabaseClient

    client = DatabaseClient(resolve_db_config("mall4j"))
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = BACKUP_ROOT / f"detail_ids_{stamp}"

    import openpyxl

    touched: set[Path] = set()
    for rel, case_id, old, new, table, pk, value in FIXES:
        path = CASE_ROOT / rel
        if not row_id_exists(client, table, pk, value):
            print(f"跳过 {case_id}: 库里没有 {table}.{pk}={value}")
            continue
        wb = openpyxl.load_workbook(path)
        ws = wb["接口测试用例"]
        if not patch_path(ws, case_id, old, new):
            print(f"跳过 {case_id}: 路径不是预期的 {old!r}")
            continue
        if args.dry_run:
            print(f"[预演] {case_id}: {old} -> {new}")
            continue
        backup_dir.mkdir(parents=True, exist_ok=True)
        if path not in touched:
            shutil.copy2(path, backup_dir / path.name)
            touched.add(path)
        wb.save(path)
        print(f"{case_id}: {old} -> {new}")

    # API-129 的响应字段断言单独处理
    path = CASE_ROOT / "mall4j/sys_user/sys_user.xlsx"
    wb = openpyxl.load_workbook(path)
    ws = wb["接口测试用例"]
    if patch_asserts(ws, "API-129", fix_user_detail_pk_assert):
        if args.dry_run:
            print("[预演] API-129: data.userId == 1 -> data.username == 'admin'")
        else:
            backup_dir.mkdir(parents=True, exist_ok=True)
            if path not in touched:
                shutil.copy2(path, backup_dir / path.name)
            wb.save(path)
            print("API-129: data.userId == 1 -> data.username == 'admin'")

    if not args.dry_run and touched:
        print(f"\n备份: {backup_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
