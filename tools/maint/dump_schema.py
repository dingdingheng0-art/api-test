"""连数据库读表结构，导出成 Markdown / JSON。

**用途**：写校验 SQL 的时候，最大的时间黑洞是"我到底要查哪张表、哪个字段"。
这个脚本连上库把结构读出来，直接给你一份能看的清单。

导出两种格式：

- **markdown** —— 给人看，也能直接粘给 AI 当上下文
- **json** —— 给程序用（比如喂给生成校验 SQL 的脚本）

读结构本身有缓存（默认 1 小时），连一次库要几百毫秒，不用每回都重连。

用法::

    # 用 configs/config.yaml 里的 db.default 连接，读全部表
    python tools/maint/dump_schema.py

    # 指定某个别名
    python tools/maint/dump_schema.py --alias test_db

    # 只看名字里带 order 的表
    python tools/maint/dump_schema.py --filter order

    # 导出文件
    python tools/maint/dump_schema.py --format markdown -o docs/schema.md
    python tools/maint/dump_schema.py --format json -o docs/schema.json

    # 强制重读（忽略缓存）
    python tools/maint/dump_schema.py --refresh

**没配数据库怎么办**：加 ``--sqlite <文件路径>`` 用本地 sqlite 库读，
不连任何外部服务，方便本地验证：

    python tools/maint/dump_schema.py --sqlite data/demo.db
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.config.config_util import load_config  # noqa: E402
from verify.schema import DatabaseSchema, SchemaInspector  # noqa: E402


# --------------------------------------------------------------------------
# 配置解析
# --------------------------------------------------------------------------


def resolve_db_config(alias: str | None) -> dict[str, Any]:
    """从 config.yaml 里拿数据库连接配置。

    支持两种写法（和框架其他地方一致）::

        db:
          default: {type: mysql, ...}
        # 或者
        db: {type: mysql, ...}
    """
    config = load_config()
    raw = config.get("db")
    if not raw:
        raise SystemExit(
            "config.yaml 里没有 db 配置。"
            "加一段：db: {default: {type: mysql, host: ..., database: ...}}"
        )

    if not isinstance(raw, dict):
        raise SystemExit("config.yaml 里的 db 配置格式不对（应该是个字典）")

    # 扁平的写法 —— 直接就是连接参数
    type_keys = {"type", "host", "port", "user", "password", "database", "params"}
    if any(key in raw for key in type_keys):
        return dict(raw)

    # 嵌套的写法 —— 按别名取
    wanted = alias or "default"
    if wanted not in raw:
        available = ", ".join(str(k) for k in raw)
        raise SystemExit(f"没有名为 {wanted!r} 的数据库配置（可用的：{available}）")

    return dict(raw[wanted])


# --------------------------------------------------------------------------
# 导出
# --------------------------------------------------------------------------


def to_markdown(schema: DatabaseSchema, keyword: str = "") -> str:
    """导出成 Markdown。"""
    tables = schema.find_tables(keyword) if keyword else schema.tables

    lines: list[str] = []
    lines.append(f"# 数据库表结构：{schema.database or '(未命名)'}")
    lines.append("")
    lines.append(f"共 {len(tables)} 张表。")
    lines.append("")

    for table in tables:
        title = f"## {table.name}"
        if table.comment:
            title += f" —— {table.comment}"
        lines.append(title)
        lines.append("")

        # ---- 字段 ----
        if table.columns:
            lines.append("| 字段 | 类型 | 必填 | 主键 | 默认值 | 说明 |")
            lines.append("| --- | --- | --- | --- | --- | --- |")
            for column in table.columns:
                flags = []
                if column.primary_key:
                    flags.append("主键")
                if column.unique:
                    flags.append("唯一")
                if column.auto_increment:
                    flags.append("自增")
                lines.append(
                    "| `{column}` | {ctype} | {required} | {flags} | {default} | {comment} |".format(
                        column=column.name,
                        ctype=column.raw_type or column.type,
                        required="是" if column.is_required else "否",
                        flags="、".join(flags) if flags else "",
                        default=("`" + str(column.default) + "`") if column.default is not None else "",
                        comment=column.comment or "",
                    )
                )
            lines.append("")

        # ---- 主键 ----
        if table.primary_key:
            lines.append(f"主键：{', '.join(f'`{c}`' for c in table.primary_key)}")
            lines.append("")

        # ---- 索引 ----
        if table.indexes:
            lines.append("| 索引 | 字段 | 唯一 |")
            lines.append("| --- | --- | --- |")
            for index in table.indexes:
                lines.append(
                    f"| `{index.name}` | {', '.join(f'`{c}`' for c in index.columns)} | "
                    f"{'是' if index.unique else '否'} |"
                )
            lines.append("")

        # ---- 外键 ----
        if table.foreign_keys:
            lines.append("| 外键 | 本表字段 | 指向 |")
            lines.append("| --- | --- | --- |")
            for fk in table.foreign_keys:
                lines.append(
                    f"| `{fk.name}` | `{fk.column}` | "
                    f"`{fk.referenced_table}.{fk.referenced_column}` |"
                )
            lines.append("")

    return "\n".join(lines)


def to_json(schema: DatabaseSchema) -> str:
    """导出成 JSON。"""
    return json.dumps(schema.to_dict(), ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def _friendly_error(exc: Exception) -> str:
    """把底层报错翻译成"该怎么办"。

    数据库报错的原文对人没用 —— 关键是告诉人下一步做什么。
    """
    text = str(exc)
    code = getattr(exc, "errno", None)

    # Access denied —— 密码不对 / 没权限
    if code == 1045 or "Access denied" in text:
        return (
            f"{text}\n"
            "  → 密码不对或没有权限。密码配在环境变量里：\n"
            "      $env:DB_PASSWORD = \"你的密码\"\n"
            "    （也可以直接改 configs/config.yaml 的 db 段）"
        )

    # Unknown database —— 库名不存在
    if code == 1049 or "Unknown database" in text:
        name = ""
        for part in text.split("'"):
            if part not in ("", " ") and "Unknown database" not in part:
                name = part
                break
        return (
            f"{text}\n"
            "  → 库名不存在。"
            + (f"配置里写的是 {name!r}，" if name else "")
            + "跑一下看看有哪些库：\n"
            "      python -c \"import mysql.connector; c=mysql.connector.connect("
            "host='127.0.0.1',user='root',password='你的密码');"
            "c.cursor().execute('SHOW DATABASES');print(*c.fetchall(),sep='\\n')\""
        )

    # 连不上 —— 服务没起
    if code in (2003, 2002, 2005) or "Can't connect" in text:
        return (
            f"{text}\n"
            "  → 连不上数据库。检查：MySQL 服务起没起、host/port 对不对、"
            "防火墙有没有挡。"
        )

    # 驱动没装
    if "No module named" in text and "mysql" in text:
        return (
            f"{text}\n"
            "  → 没装 MySQL 驱动。装一下：\n"
            "      uv pip install mysql-connector-python"
        )

    return text


def main() -> int:
    parser = argparse.ArgumentParser(description="连数据库读表结构并导出")
    parser.add_argument("--alias", default=None, help="用 config.yaml 里哪个数据库别名（默认 default）")
    parser.add_argument("--sqlite", default=None, help="用本地 sqlite 库读（不连外部服务）")
    parser.add_argument("--format", choices=["markdown", "json"], default="markdown", help="导出格式")
    parser.add_argument("-o", "--output", default=None, help="输出文件（不给就打印到屏幕）")
    parser.add_argument("--filter", default="", help="只看名字里含这个关键词的表")
    parser.add_argument("--refresh", action="store_true", help="强制重读（忽略缓存）")
    parser.add_argument("--no-cache", action="store_true", help="完全不用缓存")
    parser.add_argument("--list", action="store_true", help="只列表名和行数，不导明细")
    args = parser.parse_args()

    # ---- 连接配置 ----
    if args.sqlite:
        db_config: dict[str, Any] = {"type": "sqlite", "database": args.sqlite}
    else:
        db_config = resolve_db_config(args.alias)

    inspector = SchemaInspector(
        db_config,
        cache_ttl=0 if args.no_cache else 3600,
    )

    try:
        schema = inspector.inspect(refresh=args.refresh, include_columns=not args.list)
    except Exception as exc:  # noqa: BLE001 - 顶层脚本，打印人话错误
        print(f"读表结构失败: {_friendly_error(exc)}")
        return 1
    finally:
        inspector.close()

    # ---- 只列表名 ----
    if args.list:
        print(f"库 {schema.database or '(未命名)'}：共 {len(schema.tables)} 张表\n")
        for table in schema.tables:
            comment = f"  —— {table.comment}" if table.comment else ""
            rows = f"  (~{table.approx_rows} 行)" if table.approx_rows else ""
            print(f"  {table.name}{comment}{rows}")
        return 0

    # ---- 导出 ----
    content = (
        to_json(schema)
        if args.format == "json"
        else to_markdown(schema, args.filter)
    )

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(content, encoding="utf-8")
        print(f"已导出到 {out_path}（{len(content)} 字符）")
    else:
        print(content)

    return 0


if __name__ == "__main__":
    sys.exit(main())
