"""清理库里"未渲染模板"的脏行。

**为什么会有这种行**：用例里的变量写在 Excel 里是 ``{{tools.fixed(...)}}`` 这种模板，
框架在发请求前会把它渲染成真值。一旦某条用例没走到渲染（早退、前置失败、或被跳过的
步骤），原样字符串就被 POST 进了库 —— 库里留下一串字面量 ``{{tools.fixed('...')}}``。

这类行有两个特征，本工具就按它们识别：

1. 字符串列里有 ``{{`` —— 正常业务数据不会长这样
2. 因此它**永远匹配不上** ``find_id_by_fixed`` 的按名字查找，只会污染分页和统计

**先备份再删**：删之前把整行 dump 成 JSON 落到
``.cache/backup/dirty_rows_<时间戳>/<表名>.json``，出问题能照着插回去。

用法::

    python tools/maint/clean_dirty_rows.py                # 只看，不删（默认）
    python tools/maint/clean_dirty_rows.py --apply        # 真删
    python tools/maint/clean_dirty_rows.py --apply --tables tz_hot_search,tz_area
    python tools/maint/clean_dirty_rows.py --apply --ids tz_hot_search=86,87,88,89
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.maint.dump_schema import resolve_db_config  # noqa: E402
from verify.db import DatabaseClient  # noqa: E402

SCHEMA_FILE = PROJECT_ROOT / ".cache" / "schema_mall4j.json"
BACKUP_ROOT = PROJECT_ROOT / ".cache" / "backup"

# 只有这些类型的列才可能"长得像模板"
TEXT_TYPES = ("varchar", "char", "text", "longtext", "mediumtext", "tinytext")


def load_schema() -> dict[str, dict[str, Any]]:
    if not SCHEMA_FILE.exists():
        raise SystemExit(f"缺少库结构快照 {SCHEMA_FILE}，先跑 tools/maint/dump_schema.py")
    schema = json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))
    return {table["name"]: table for table in schema["tables"]}


def primary_key(table: dict[str, Any]) -> str:
    for column in table["columns"]:
        if column.get("primary_key"):
            return column["name"]
    return table["columns"][0]["name"]


def text_columns(table: dict[str, Any]) -> list[str]:
    return [
        column["name"]
        for column in table["columns"]
        if column["type"].lower().startswith(TEXT_TYPES)
    ]


def find_dirty(client: DatabaseClient, table: dict[str, Any]) -> list[dict[str, Any]]:
    """返回含 ``{{`` 的整行，按主键排序。"""
    columns = text_columns(table)
    if not columns:
        return []
    where = " OR ".join(f"`{name}` LIKE %s" for name in columns)
    sql = f"SELECT * FROM `{table['name']}` WHERE {where}"
    rows = client.query(sql, ["%{{%"] * len(columns))
    pk = primary_key(table)
    return sorted(rows, key=lambda row: row.get(pk) or 0)


def dump_backup(tables: dict[str, list[dict[str, Any]]]) -> Path:
    folder = BACKUP_ROOT / f"dirty_rows_{datetime.now():%Y%m%d_%H%M%S}"
    folder.mkdir(parents=True, exist_ok=True)
    for name, rows in tables.items():
        (folder / f"{name}.json").write_text(
            json.dumps(rows, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
    return folder


def delete_rows(client: DatabaseClient, table: dict[str, Any], rows: list[dict[str, Any]]) -> int:
    pk = primary_key(table)
    ids = [row[pk] for row in rows]
    placeholders = ",".join(["%s"] * len(ids))
    return client.execute(f"DELETE FROM `{table['name']}` WHERE `{pk}` IN ({placeholders})", ids)


def parse_id_filter(raw: str) -> dict[str, list[Any]]:
    """``--ids tz_hot_search=86,87`` → ``{"tz_hot_search": [86, 87]}``。"""
    result: dict[str, list[Any]] = {}
    for chunk in raw.split(";"):
        if not chunk.strip():
            continue
        name, _, values = chunk.partition("=")
        ids: list[Any] = []
        for value in values.split(","):
            value = value.strip()
            if not value:
                continue
            ids.append(int(value) if value.isdigit() else value)
        result[name.strip()] = ids
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="清理含未渲染模板的脏行")
    parser.add_argument("--apply", action="store_true", help="真删（默认只列出）")
    parser.add_argument("--tables", default="", help="只处理这些表，逗号分隔")
    parser.add_argument(
        "--ids", default="", help="只删指定主键，格式 表=id,id;表=id（需配合 --apply）"
    )
    args = parser.parse_args()

    schema = load_schema()
    only_tables = {name.strip() for name in args.tables.split(",") if name.strip()}
    id_filter = parse_id_filter(args.ids)

    client = DatabaseClient(resolve_db_config("mall4j"))
    found: dict[str, list[dict[str, Any]]] = {}
    for name, table in schema.items():
        if only_tables and name not in only_tables:
            continue
        if id_filter and name not in id_filter:
            continue
        rows = find_dirty(client, table)
        if id_filter:
            allowed = set(id_filter[name])
            rows = [row for row in rows if row[primary_key(table)] in allowed]
        if rows:
            found[name] = rows

    if not found:
        print("没有发现脏行。")
        return 0

    print(f"{'表':20} {'主键':16} {'条数':>4}  样例")
    total = 0
    for name, rows in found.items():
        pk = primary_key(schema[name])
        sample = rows[0]
        preview = next(
            (
                str(sample[column])[:60]
                for column in text_columns(schema[name])
                if "{{" in str(sample.get(column) or "")
            ),
            "",
        )
        print(f"{name:20} {pk:16} {len(rows):>4}  {preview}")
        total += len(rows)
    print(f"\n合计 {total} 行")

    if not args.apply:
        # 这份 --ids 字符串可以原样粘给用户确认"就删这些"，避免误删整表
        exact = ";".join(
            f"{name}=" + ",".join(str(row[primary_key(schema[name])]) for row in rows)
            for name, rows in found.items()
        )
        print("\n对应的精确主键清单（确认后可用这条命令删）:")
        print(f"  python tools/maint/clean_dirty_rows.py --apply --ids \"{exact}\"")
        print("\n这是预演（没删）。")
        return 0

    folder = dump_backup(found)
    print(f"\n已备份到 {folder.relative_to(PROJECT_ROOT)}")
    for name, rows in found.items():
        affected = delete_rows(client, schema[name], rows)
        print(f"  删除 {name}: {affected} 行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
