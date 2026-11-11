"""给写操作（新增 / 修改 / 删除）用例补**落库断言**。

**为什么光验接口不够**：HTTP 200 + ``success == true`` 只说明"服务端没抛异常"。
插入语句因为唯一键冲突被吞、更新影响了 0 行、删除只改了缓存 ——
这些都可能是成功响应。只有回头查一次库才能证明数据真的动了。

**怎么加**（按接口方法分三种，和 admin_hotSearch 样板一致）：

======  ============================================================
方法     落库断言（加在哪）
======  ============================================================
POST     ``SELECT COUNT(*) FROM t WHERE name = %s`` == 1
        （加在主步骤的「断言」列；旧版加在「后置」的「查数据」步骤上，
        现在会被自动挪回主步骤）
PUT      ``SELECT name FROM t WHERE pk = %s`` == 改后的名字
        （加在主步骤上 —— 响应返回时事务已经提交）
DELETE   ``SELECT pk FROM t WHERE pk = %s`` 查不到
        （加在主步骤上；软删表改成 ``COUNT(*) WHERE pk=... AND
        delete_time IS NULL`` == 0）
======  ============================================================

**落库断言只放主步骤，不放后置的验证步骤**：后置只做清理。断言放在后置里，
失败会被报成 teardown 错误（而不是用例断言失败），主步骤先失败时还会多出
一条误导性的失败；报告里也看不到"是哪个接口写坏了"。后置 DELETE 清理步骤上
的「删后查不到」是清理验证，可以保留。

表名 / 主键 / 名字列集中写在 :data:`MODULES` 里。mall4j 的表名带 ``tz_``
前缀、列名是下划线风格（接口里的 ``hotSearchId`` → ``hot_search_id``），
照接口名写 SQL 会报表不存在。

**参数不拼字符串**：值都用 ``%s`` 占位放 ``params`` 里；id / 名字引用
用例里已有的变量表达式（``{{tools.fixed(...)}}`` / ``{{tools.max_id(...)}}``），
运行时求值，和请求里用的是同一个值。

用法::

    python tools/maint/add_db_assertions.py --dry-run
    python tools/maint/add_db_assertions.py
    python tools/maint/add_db_assertions.py --modules sys_menu prod_spec
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

sys.stdout.reconfigure(encoding="utf-8")

import openpyxl

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

CASE_ROOT = PROJECT_ROOT / "testcases" / "cases_excel" / "mall4j"
BACKUP_ROOT = PROJECT_ROOT / ".cache" / "backup"
SHEET = "接口测试用例"
DB_ALIAS = "mall4j"

#: 每个模块的库表信息
#:
#: ``table`` / ``pk`` / ``name_col`` 来自 ``.cache/schema_mall4j.json``
#: （``python tools/maint/dump_schema.py --alias mall4j --format json``）。
#: ``name_field`` 是请求体里存名字的字段。
#: ``soft_delete`` 是软删标记列 —— 有值的表 DELETE 只写标记、不删行。
MODULES: dict[str, dict[str, Any]] = {
    "admin_area": {
        "table": "tz_area",
        "pk": "area_id",
        "name_col": "area_name",
        "name_field": "areaName",
    },
    "admin_indexImg": {
        "table": "tz_index_img",
        "pk": "img_id",
        "name_col": "title",
        "name_field": "title",
    },
    "prod_category": {
        "table": "tz_category",
        "pk": "category_id",
        "name_col": "category_name",
        "name_field": "categoryName",
    },
    "prod_prodComm": {
        "table": "tz_prod_comm",
        "pk": "prod_comm_id",
        "name_col": "content",
        "name_field": "content",
    },
    "prod_prodTag": {
        "table": "tz_prod_tag",
        "pk": "id",
        "name_col": "title",
        "name_field": "title",
        "soft_delete": "delete_time",
    },
    "prod_spec": {
        "table": "tz_prod_prop",
        "pk": "prop_id",
        "name_col": "prop_name",
        "name_field": "propName",
    },
    "shop_notice": {
        "table": "tz_notice",
        "pk": "id",
        "name_col": "title",
        "name_field": "title",
    },
    "shop_pickAddr": {
        "table": "tz_pick_addr",
        "pk": "addr_id",
        "name_col": "addr_name",
        "name_field": "addrName",
    },
    "shop_transport": {
        "table": "tz_transport",
        "pk": "transport_id",
        "name_col": "trans_name",
        "name_field": "transName",
    },
    "sys_config": {
        "table": "tz_sys_config",
        "pk": "id",
        "name_col": "param_key",
        "name_field": "paramKey",
    },
    "sys_menu": {
        "table": "tz_sys_menu",
        "pk": "menu_id",
        "name_col": "name",
        "name_field": "name",
    },
    "sys_role": {
        "table": "tz_sys_role",
        "pk": "role_id",
        "name_col": "role_name",
        "name_field": "roleName",
    },
    "sys_user": {
        "table": "tz_sys_user",
        "pk": "user_id",
        "name_col": "username",
        "name_field": "username",
        # /sys/user/info 的响应里**没有** userId 字段（只有 username / status
        # 这些），断言"响应回显主键"会凭空造出一条必然失败的用例。
        "echo_pk": False,
    },
    # 下面三个模块只补"详情查得到"的断言：写接口本身有问题（见
    # [[mall4j-env-defects]]），补写操作的落库断言只会让失败原因更绕
    "admin_hotSearch": {
        "table": "tz_hot_search",
        "pk": "hot_search_id",
        "name_col": "title",
        "name_field": "title",
        "write": False,
    },
    "prod_prod": {
        "table": "tz_prod",
        "pk": "prod_id",
        "name_col": "prod_name",
        "name_field": "prodName",
        "write": False,
    },
    "admin_user": {
        "table": "tz_user",
        "pk": "user_id",
        "name_col": "user_mail",
        "name_field": "userMail",
        "write": False,
    },
}

#: 按接口单独覆盖表信息 —— ``MODULES`` 是"一个模块一张表"，但一个模块里
#: 常常混着两个业务域的接口。
#:
#: ``admin_user`` 模块（会员管理）大部分接口打 ``tz_user``，可
#: ``GET /sys/user/info`` 查的是**后台管理员**，落在 ``tz_sys_user``。
#: 不覆盖的话，要么查错表，要么（路径里没有 id）根本补不上断言。
#:
#: ``current_user`` 标记"查的是当前登录人"，路径里没有主键 —— 用登录账号
#: 当主键去查库，顺带把"响应里的 username 就是登录账号"钉死。
ENDPOINT_OVERRIDES: dict[tuple[str, str], dict[str, Any]] = {
    ("admin_user", "/sys/user/info"): {
        "table": "tz_sys_user",
        "pk": "user_id",
        "name_col": "username",
        "name_field": "username",
        "echo_pk": False,
        "current_user": True,
    },
}


def module_for_endpoint(module_name: str, path: str) -> dict[str, Any]:
    """取这条用例该用的表信息（接口级覆盖优先）。"""
    override = ENDPOINT_OVERRIDES.get((module_name, path.strip()))
    if override is None:
        return MODULES[module_name]
    return {**MODULES[module_name], **override}


# --------------------------------------------------------------------------
# 断言构造
# --------------------------------------------------------------------------


def db_assert(
    sql: str,
    comparator: str,
    expect: Any = None,
    params: list[Any] | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "check": "db",
        "sql": sql,
        "comparator": comparator,
        "alias": DB_ALIAS,
    }
    if expect is not None:
        item["expect"] = expect
    if params:
        item["params"] = params
    return item


def has_sql(asserts: list[dict[str, Any]], sql: str) -> bool:
    return any(
        isinstance(item, dict)
        and str(item.get("check", "")).lower() == "db"
        and str(item.get("sql", "")).strip() == sql
        for item in asserts
    )


def find_expr(value: Any) -> str | None:
    """字段值里带 ``{{ ... }}`` 就返回整串（可以是 ``{{...}}改`` 这种拼接）。

    断言参数会走变量解析，拼接的写法求值后和请求里发出去的值一致。
    """
    if not isinstance(value, str):
        return None
    if re.search(r"\{\{.*?\}\}", value):
        return value
    return None


def id_expr_from_body(body: dict[str, Any]) -> str | None:
    """从请求体里找主键表达式（``xxxId`` / ``id`` 字段）。"""
    for field, value in body.items():
        if str(field).lower().endswith("id"):
            expr = find_expr(value)
            if expr:
                return expr
    return None


def id_expr_from_path(path: str) -> str | None:
    """从路径里找主键表达式：``/sys/menu/{{tools.max_id(...)}}``。"""
    match = re.search(r"\{\{.*?\}\}", path)
    return match.group(0) if match else None


def info_id_from_path(path: str) -> int | None:
    """详情接口路径里的字面量主键：``/admin/area/info/1`` → ``1``。

    只认纯数字、且恰好是路径末段 —— ``/sys/user/info``（查当前登录用户）
    没有 id，返回 None，不生成断言。
    """
    match = re.fullmatch(r".*/info/(\d+)", path.strip())
    return int(match.group(1)) if match else None


def info_expr_from_path(path: str) -> str | None:
    """详情接口路径里的**表达式**主键：``/sys/user/info/{{...}}`` → ``{{...}}``。

    很多详情用例的 id 是前置步骤查出来的（``{{tools.find_id_by_fixed(...)}}``），
    写死数字的只是少数。只认字面量的 ``info_id_from_path`` 会把它们全跳过，
    于是"前置造了数据、主接口却只验响应体"—— 数据到底在不在库里没人验。
    """
    match = re.fullmatch(r".*/info/(\{\{.*\}\})", path.strip())
    return match.group(1) if match else None


def camel_case(snake: str) -> str:
    """``area_id`` → ``areaId``；``id`` → ``id``。"""
    head, *rest = snake.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in rest)


def pk_row_exists(client: Any, module: dict[str, Any], pk_value: int) -> bool:
    """详情用例写死的那个 id，库里到底有没有。

    生成"该主键存在"的断言前必须先确认 —— 用例里写死 ``/info/1``、库里却
    没有 id=1 的行时，这条断言必然失败，而且失败原因是用例写错了，不是
    服务端的问题。
    """
    return (
        client.scalar(
            f"SELECT COUNT(*) FROM {module['table']} WHERE {module['pk']} = %s",
            [pk_value],
        )
        or 0
    ) > 0


def _field_of(item: dict[str, Any]) -> str:
    """断言的目标字段 —— 扁平格式写 ``jsonpath``，规范化后写 ``field``。"""
    path = str(item.get("field") or item.get("jsonpath") or "")
    return path.lstrip("$.")


def _operator_of(item: dict[str, Any]) -> str:
    return str(item.get("type") or item.get("comparator") or "").strip()


def _expect_of(item: dict[str, Any]) -> Any:
    return item["expect"] if "expect" in item else item.get("expected")


def is_error_case(asserts: list[dict[str, Any]]) -> bool:
    """错误路径用例（``success=false`` / 业务码非 ``00000``）。

    这些用例本来就期望接口报错，套"查得到"的成功模板必然误报。
    两种写法都要认：Excel 里存的是 ``jsonpath``/``comparator``/``expect``。
    """
    for item in asserts:
        if not isinstance(item, dict):
            continue
        field, operator, expected = _field_of(item), _operator_of(item), _expect_of(item)
        if field == "success" and expected is False:
            return True
        if field == "code":
            if operator in ("not_equal", "!=", "notequal"):
                return True
            if operator in ("equal", "==", "equals") and expected != "00000":
                return True
    return False


def build_info_asserts(
    module: dict[str, Any],
    pk_value: int | str,
    missing: bool,
    echo_pk: bool = True,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """详情接口断言。

    返回 ``(落库断言, 响应字段断言)``：

    - 查得到：库里该主键存在，且响应里的主键字段就是路径里要的那个 id
      （只断言 ``exists`` 的话，接口返回别的记录也一样能过）
    - 查不到：库里确实没有这个主键 —— 否则 ``data is null`` 可能是在掩盖
      "数据还在、接口查崩了"

    Args:
        pk_value: 字面量数字，或 ``{{...}}`` 表达式（前置步骤查出来的 id）。
            表达式会原样写进 ``params`` / ``expect``，运行时求值。
        echo_pk: 要不要顺手断言"响应里的主键等于路径里那个 id"。
            **只有字面量 id 才该断言**：id 是运行时才知道的表达式时，
            ``data`` 里回不回显主键是另一回事 —— 实测 mall4j 的
            ``/sys/user/info/{{...}}`` 就不返回 ``data.userId``，
            硬塞这条会让原本能过的用例变红。

    ``pk_value`` 可以是字面量数字，也可以是 ``{{...}}`` 表达式
    （前置步骤查出来的 id）。表达式会原样写进 ``params`` / ``expect``，
    运行时求值，和请求里用的是同一个值。
    """
    table, pk = module["table"], module["pk"]
    sql = f"SELECT {pk} FROM {table} WHERE {pk} = %s"
    if missing:
        return [db_assert(sql, "not_exists", None, [pk_value])], []

    if not echo_pk:
        return [db_assert(sql, "exists", None, [pk_value])], []

    response_asserts = [
        {
            "check": "body",
            "jsonpath": f"$.data.{camel_case(pk)}",
            "comparator": "==",
            "expect": pk_value,
        }
    ]
    return [db_assert(sql, "exists", None, [pk_value])], response_asserts


def build_current_user_asserts(
    module: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``GET /sys/user/info`` 这类"查当前登录人"的接口。

    路径里没有主键，但登录账号是知道的（``tools.login_credential``），
    用它当主键查库就行：

    - 落库：``tz_sys_user`` 里真有这个账号
    - 响应：``data.username`` 就是这个账号

    这两条合起来才说明"接口返回的确实是当前登录的那个人"。
    只断言 ``data`` 存在的话，返回谁的数据都能过。
    """
    table, name_col = module["table"], module["name_col"]
    sql = f"SELECT {module['pk']} FROM {table} WHERE {name_col} = %s"
    credential = "{{tools.login_credential('mall4j', 'username')}}"
    response_asserts = [
        {
            "check": "body",
            "jsonpath": f"$.data.{camel_case(name_col)}",
            "comparator": "==",
            "expect": credential,
        }
    ]
    return [db_assert(sql, "exists", None, [credential])], response_asserts


def build_post_asserts(module: dict[str, Any], name_expr: str) -> list[dict[str, Any]]:
    table, name_col = module["table"], module["name_col"]
    condition = f"{name_col} = %s"
    if module.get("soft_delete"):
        condition += f" AND {module['soft_delete']} IS NULL"
    sql_count = f"SELECT COUNT(*) FROM {table} WHERE {condition}"
    sql_exists = f"SELECT {module['pk']} FROM {table} WHERE {condition}"
    return [
        db_assert(sql_count, "==", 1, [name_expr]),
        db_assert(sql_exists, "exists", None, [name_expr]),
    ]


def build_put_asserts(
    module: dict[str, Any], name_expr: str, pk_expr: str
) -> list[dict[str, Any]]:
    sql = (
        f"SELECT {module['name_col']} FROM {module['table']} "
        f"WHERE {module['pk']} = %s"
    )
    return [db_assert(sql, "==", name_expr, [pk_expr])]


def build_delete_asserts(
    module: dict[str, Any], pk_expr: str
) -> list[dict[str, Any]]:
    table, pk = module["table"], module["pk"]
    soft = module.get("soft_delete")
    if soft:
        # 软删：行还在，但标记列要有值才算删掉
        sql = f"SELECT COUNT(*) FROM {table} WHERE {pk} = %s AND {soft} IS NULL"
        return [db_assert(sql, "==", 0, [pk_expr])]
    sql = f"SELECT {pk} FROM {table} WHERE {pk} = %s"
    return [db_assert(sql, "not_exists", None, [pk_expr])]


# --------------------------------------------------------------------------
# 把 timestamp 命名改成 fixed 命名
# --------------------------------------------------------------------------


def to_fixed(value: str, key: str) -> str:
    """``前缀{{tools.timestamp()}}`` → ``{{tools.fixed('key','前缀')}}``。"""
    match = re.fullmatch(r"(.*?)\{\{\s*tools\.timestamp\(\)\s*\}\}", value)
    if not match:
        return value
    prefix = match.group(1)
    return "{{tools.fixed('%s', '%s')}}" % (key, prefix)


def convert_timestamp_names(
    body: dict[str, Any], module_name: str, is_update: bool
) -> list[str]:
    """把请求体里用 ``timestamp()`` 造的名字换成 ``fixed()``。

    ``timestamp()`` 每次求值都变 —— 请求里造的名字和断言里查的名字对不上，
    落库断言就没法写。``fixed()`` 同一个名字永远同一个值。
    """
    changed: list[str] = []
    for field, value in body.items():
        if not isinstance(value, str) or "tools.timestamp()" not in value:
            continue
        key = f"mall4j_{module_name}_{field}" + ("_update" if is_update else "")
        new_value = to_fixed(value, key)
        if new_value != value:
            body[field] = new_value
            changed.append(f"{field}: {value} -> {new_value}")
    return changed


# --------------------------------------------------------------------------
# 工作簿读写
# --------------------------------------------------------------------------


def column_index(ws: Any, name: str) -> int | None:
    for idx, cell in enumerate(ws[1], start=1):
        if str(cell.value or "").strip() == name:
            return idx
    return None


def read_json(value: Any) -> Any:
    if value in (None, ""):
        return None
    if isinstance(value, (list, dict)):
        return value
    return json.loads(str(value).strip())


def _is_db_assert(item: Any) -> bool:
    """是不是一条落库断言（扁平 ``check=db`` 或内部 ``db_query*``）。"""
    if not isinstance(item, dict):
        return False
    if str(item.get("check") or "").lower() == "db":
        return True
    kind = str(item.get("type") or "").lower()
    return kind.startswith(("db_query", "mysql_query"))


def _sql_of(item: dict[str, Any]) -> str:
    return str(item.get("sql") or item.get("query") or "").strip()


def _is_cleanup_step(item: Any) -> bool:
    """后置里的纯清理动作（DELETE / factory_cleanup / db）。"""
    if not isinstance(item, dict):
        return False
    op = str(item.get("operation_type") or "").lower()
    if op in ("factory", "factory_cleanup", "db"):
        return True
    return str(item.get("method") or "").upper() == "DELETE"


def _references_step(assertion: dict[str, Any], step_id: str) -> bool:
    """断言是否引用了后置步骤自己的产出（``{{查数据.xxx}}``）。

    这种断言不能挪到主步骤 —— 主步骤跑的时候那个变量还不存在。
    """
    if not step_id:
        return False
    needle = f"{{{{{step_id}."
    texts = [
        str(assertion.get("sql") or ""),
        str(assertion.get("query") or ""),
        json.dumps(assertion.get("params"), ensure_ascii=False),
    ]
    return any(needle in text for text in texts)


def move_verification_db_asserts_out_of_teardowns(
    teardowns: list[Any], main_asserts: list[dict[str, Any]]
) -> tuple[int, int]:
    """把后置**验证步骤**里的落库断言挪到主步骤，返回 ``(挪动数, 跳过的数)``。

    后置 DELETE / factory_cleanup / db 这类清理步骤不动 —— 它们上面的
    「删后查不到」是在验证清理结果。验证步骤（比如「查数据」GET）上的落库
    断言才是放错位置。

    引用了后置步骤自身结果的断言挪不了（主步骤跑的时候变量还不存在），
    原样留下并计入 skipped，由审计继续提示。
    """
    moved = skipped = 0
    for item in teardowns:
        if not isinstance(item, dict) or _is_cleanup_step(item):
            continue
        step_id = str(item.get("id") or "")
        asserts = item.get("assert")
        if not isinstance(asserts, list):
            continue
        kept: list[Any] = []
        for assertion in asserts:
            if not _is_db_assert(assertion):
                kept.append(assertion)
                continue
            if _references_step(assertion, step_id):
                kept.append(assertion)
                skipped += 1
                continue
            if not has_sql(main_asserts, _sql_of(assertion)):
                main_asserts.append(assertion)
            moved += 1
        if len(kept) != len(asserts):
            if kept:
                item["assert"] = kept
            else:
                item.pop("assert", None)
    return moved, skipped


def move_db_asserts_out_of_teardowns(
    teardowns: list[Any], target_sqls: set[str]
) -> int:
    """把后置步骤里指定 SQL 的落库断言摘掉，返回摘掉的数量。

    旧版工具把 POST 的落库断言加在「后置」的「查数据」步骤上。后置只该做
    清理：断言放在那里，失败会显示成 teardown 错误，主步骤失败时还会多出
    一条误导性的失败。重跑本工具时顺手把这些断言挪回主步骤。

    只摘 ``target_sqls`` 里的（本工具自己生成的那几条），手工写在别的后置
    步骤上的断言（比如"删除后查不到"）不动 —— 那不是这里能安全搬的。
    """
    moved = 0
    for item in teardowns:
        if not isinstance(item, dict) or _is_cleanup_step(item):
            continue
        asserts = item.get("assert")
        if not isinstance(asserts, list):
            continue
        kept = [
            assertion
            for assertion in asserts
            if not (_is_db_assert(assertion) and _sql_of(assertion) in target_sqls)
        ]
        if len(kept) == len(asserts):
            continue
        moved += len(asserts) - len(kept)
        if kept:
            item["assert"] = kept
        else:
            item.pop("assert", None)
    return moved


def process_workbook(
    path: Path, module_name: str, dry_run: bool, client: Any = None
) -> dict[str, int]:
    workbook = openpyxl.load_workbook(path)
    if SHEET not in workbook.sheetnames:
        return {}

    ws = workbook[SHEET]
    headers = {
        name: column_index(ws, name)
        for name in ("请求方法", "请求数据", "断言", "前置", "后置", "用例状态")
    }
    if any(idx is None for idx in headers.values()):
        print(f"    ! 跳过 {module_name}：缺少列")
        return {}
    # 「接口」列不是所有工作簿都有；没有就退回模块级配置
    headers["接口"] = column_index(ws, "接口")

    stats = {"cases": 0, "db_added": 0, "renamed": 0, "moved": 0}

    for row in range(2, ws.max_row + 1):
        if str(ws.cell(row, headers["用例状态"]).value or "").strip() != "debugged":
            continue

        method = str(ws.cell(row, headers["请求方法"]).value or "").upper()
        if method not in ("POST", "PUT", "DELETE", "GET"):
            continue

        case_id = ws.cell(row, 1).value
        body = read_json(ws.cell(row, headers["请求数据"]).value) or {}
        if not isinstance(body, dict):
            body = {}
        api_col = headers.get("接口")
        api_path = str(ws.cell(row, api_col).value or "") if api_col else ""
        # 一个模块里可能混着两个业务域的接口（会员管理里夹了个查后台管理员的），
        # 所以表信息要按接口取，不能整个模块一把梭
        module = module_for_endpoint(module_name, api_path)
        changed = False

        # ---- 后置验证步骤上的落库断言，先挪回主步骤 ----
        #
        # 后置 DELETE 清理步骤上的「删后查不到」不动（那是清理验证）；
        # 「查数据」这类验证步骤上的断言全部收到主步骤。
        teardowns = read_json(ws.cell(row, headers["后置"]).value) or []
        asserts = read_json(ws.cell(row, headers["断言"]).value) or []
        moved, skipped = move_verification_db_asserts_out_of_teardowns(
            teardowns, asserts
        )
        if moved:
            ws.cell(row, headers["后置"]).value = json.dumps(
                teardowns, ensure_ascii=False
            )
            ws.cell(row, headers["断言"]).value = json.dumps(asserts, ensure_ascii=False)
            stats["moved"] += moved
            changed = True
        if skipped:
            print(
                f"    ! {case_id} 有 {skipped} 条落库断言引用了后置步骤自身的结果，"
                f"无法挪动"
            )

        # ---- 先把 timestamp 命名换成 fixed ----（只对写操作的请求体）
        renaming = convert_timestamp_names(body, module_name, is_update=method == "PUT")
        if renaming:
            ws.cell(row, headers["请求数据"]).value = json.dumps(body, ensure_ascii=False)
            stats["renamed"] += len(renaming)
            changed = True

        # ---- 再补落库断言 ----
        added: list[dict[str, Any]] = []
        if method != "GET" and module.get("write") is False:
            # 写接口本身在环境上就报错（A00005），补落库断言只会让失败原因更绕
            continue
        if method == "GET":
            literal_pk = info_id_from_path(api_path)
            expr_pk = info_expr_from_path(api_path)
            pk_value: int | str | None = (
                literal_pk if literal_pk is not None else expr_pk
            )
            if pk_value is None:
                # 路径里没主键，只剩一种情况能验库：查"当前登录人"的接口。
                # 它的主键就是登录账号，用账号去查 —— 不查的话这条用例
                # 只证明了"接口返回了点什么"，连查的是不是这个人都不确定。
                #
                # ⚠️ 这里**不能** ``continue``：底部还负责统计和存盘，
                #    跳过它的话断言写进了内存里的单元格、文件却没保存，
                #    脚本还一声不响地报"0 条用例被加强"。
                if module.get("current_user"):
                    asserts = read_json(ws.cell(row, headers["断言"]).value) or []
                    db_items, response_items = build_current_user_asserts(module)
                    for item in response_items:
                        if not any(
                            isinstance(old, dict)
                            and _field_of(old) == _field_of(item)
                            and _operator_of(old) in ("equal", "==", "equals")
                            for old in asserts
                        ):
                            asserts.append(item)
                            added.append(item)
                    for item in db_items:
                        if not has_sql(asserts, item["sql"]):
                            asserts.append(item)
                            added.append(item)
                    if added:
                        ws.cell(row, headers["断言"]).value = json.dumps(
                            asserts, ensure_ascii=False
                        )
                if not added:
                    continue
            else:
                asserts = read_json(ws.cell(row, headers["断言"]).value) or []

            if pk_value is not None:
                # 「查不到」用例的响应里 data 就是 null，判据看原有用例里有没有这条
                missing = any(
                    isinstance(item, dict)
                    and _operator_of(item) in ("is_none", "is_null", "none")
                    and _field_of(item) == "data"
                    for item in asserts
                )
                # 负例（success=false / 业务码非 00000）也可能是"查不到"，
                # 判据是路径里那个 id 在库里确实不存在。
                #
                # ⚠️ 以前这里遇到 is_error_case 直接 continue —— 结果负例永远
                #    补不上"库里确实没有这条"。而这条恰恰是最该有的：
                #    data 返回 null 可能只是接口崩了/没查，库里有没有数据是
                #    唯一能把两者分开的证据。写死的大 id（999999999）不用连库
                #    就能判，只有字面量 id 才需要先查一下。
                if is_error_case(asserts) and not missing:
                    if isinstance(pk_value, int):
                        if client is not None and pk_row_exists(
                            client, module, pk_value
                        ):
                            print(
                                f"    ! {case_id} 期望查不到，但库里 {module['table']}."
                                f"{module['pk']}={pk_value} 存在，跳过"
                            )
                            continue
                        missing = True
                    else:
                        # 表达式 id 的负例：判断不了它到底查的是哪一行，
                        # 不硬塞"不存在"断言（可能反而写反）
                        print(
                            f"    ! {case_id} 负例 + 表达式主键，无法确定目标行，跳过"
                        )
                        continue

                # 正例必须先确认库里真有这条记录 —— 否则"主键存在"断言必然失败
                if not missing and isinstance(pk_value, int):
                    if client is None:
                        print(
                            f"    ! {case_id} 没连库，无法确认 id={pk_value} 是否存在，跳过"
                        )
                        continue
                    if not pk_row_exists(client, module, pk_value):
                        print(
                            f"    ! {case_id} 库里没有 {module['table']}.{module['pk']}"
                            f"={pk_value}，跳过（这条用例的 id 得先改成真实存在的）"
                        )
                        continue
                # 只有字面量 id 才顺手断言"响应回显的主键"—— 表达式 id 是运行时
                # 才求出来的，回不回显主键是接口自己的事（mall4j 的
                # /sys/user/info/{{...}} 就不返回 data.userId）
                db_items, response_items = build_info_asserts(
                    module,
                    pk_value,
                    missing,
                    echo_pk=isinstance(pk_value, int) and module.get("echo_pk", True),
                )
                for item in response_items:
                    if not any(
                        isinstance(old, dict)
                        and _field_of(old) == _field_of(item)
                        and _operator_of(old) in ("equal", "==", "equals")
                        for old in asserts
                    ):
                        asserts.append(item)
                        added.append(item)
                for item in db_items:
                    if not has_sql(asserts, item["sql"]):
                        asserts.append(item)
                        added.append(item)
                if added:
                    ws.cell(row, headers["断言"]).value = json.dumps(
                        asserts, ensure_ascii=False
                    )
        elif method == "POST":
            name_expr = find_expr(body.get(module["name_field"]))
            if not name_expr:
                print(f"    ! {case_id} 请求体里找不到固定名字，跳过落库断言")
                continue
            db_items = build_post_asserts(module, name_expr)

            # 旧版把这两条加在「后置」的「查数据」步骤上。先摘干净，
            # 保证落库断言只出现在主步骤 —— 后置只做清理。
            teardowns = read_json(ws.cell(row, headers["后置"]).value) or []
            target_sqls = {item["sql"] for item in db_items}
            moved = move_db_asserts_out_of_teardowns(teardowns, target_sqls)
            if moved:
                ws.cell(row, headers["后置"]).value = json.dumps(
                    teardowns, ensure_ascii=False
                )
                stats["moved"] += moved
                changed = True

            asserts = read_json(ws.cell(row, headers["断言"]).value) or []
            for item in db_items:
                if not has_sql(asserts, item["sql"]):
                    asserts.append(item)
                    added.append(item)
            if added:
                ws.cell(row, headers["断言"]).value = json.dumps(
                    asserts, ensure_ascii=False
                )
        elif method == "PUT":
            name_expr = find_expr(body.get(module["name_field"]))
            pk_expr = id_expr_from_body(body)
            if not name_expr or not pk_expr:
                print(f"    ! {case_id} 请求体里找不到名字/主键表达式，跳过落库断言")
                continue
            asserts = read_json(ws.cell(row, headers["断言"]).value) or []
            for item in build_put_asserts(module, name_expr, pk_expr):
                if not has_sql(asserts, item["sql"]):
                    asserts.append(item)
                    added.append(item)
            if added:
                ws.cell(row, headers["断言"]).value = json.dumps(asserts, ensure_ascii=False)
        else:  # DELETE
            api_col = column_index(ws, "接口")
            api_path = str(ws.cell(row, api_col).value or "") if api_col else ""
            pk_expr = id_expr_from_path(api_path)
            if not pk_expr:
                print(f"    ! {case_id} 路径里没有主键表达式，跳过落库断言")
                continue
            asserts = read_json(ws.cell(row, headers["断言"]).value) or []
            for item in build_delete_asserts(module, pk_expr):
                if not has_sql(asserts, item["sql"]):
                    asserts.append(item)
                    added.append(item)
            if added:
                ws.cell(row, headers["断言"]).value = json.dumps(asserts, ensure_ascii=False)

        if added:
            stats["db_added"] += len(added)
            changed = True
        if changed:
            stats["cases"] += 1

    if not dry_run and stats["cases"]:
        workbook.save(path)
    return stats


def open_db_client() -> Any:
    """连库只为了核对"详情用例写死的 id 在不在"，连不上就不做这项核对。"""
    try:
        from tools.maint.dump_schema import resolve_db_config
        from verify.db import DatabaseClient

        client = DatabaseClient(resolve_db_config(DB_ALIAS))
        client.scalar("SELECT 1")  # 懒连接，这里先探一下
        return client
    except Exception as exc:  # noqa: BLE001 - 没配库不该挡住补断言
        print(f"! 连不上数据库（{exc}），详情用例不做 id 存在性核对")
        return None


def backup(files: list[Path], stamp: str) -> Path:
    target = BACKUP_ROOT / f"db_assert_{stamp}"
    for path in files:
        destination = target / path.relative_to(CASE_ROOT)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="给写操作用例补落库断言")
    parser.add_argument("--modules", nargs="*", help="只处理这些模块（默认全部）")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不写文件")
    args = parser.parse_args()

    modules = args.modules or list(MODULES)
    unknown = [name for name in modules if name not in MODULES]
    if unknown:
        print(f"不认识的模块：{unknown}")
        return 2

    files = [CASE_ROOT / name / f"{name}.xlsx" for name in modules]
    files = [path for path in files if path.exists()]
    if not files:
        print("没有匹配的用例文件")
        return 2

    if not args.dry_run:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        print(f"已备份 {len(files)} 个工作簿到 {backup(files, stamp)}")
    else:
        print("（dry-run，不写文件）")

    client = open_db_client()
    total = {"cases": 0, "db_added": 0, "renamed": 0, "moved": 0}
    for path in files:
        stats = process_workbook(path, path.parent.name, args.dry_run, client)
        if not stats or not stats["cases"]:
            continue
        for key in total:
            total[key] += stats[key]
        print(
            f"  {path.parent.name:16} 用例 {stats['cases']:3}   "
            f"落库断言 +{stats['db_added']:3} 条   "
            f"后置挪回主步骤 {stats['moved']:2} 条   命名固定化 {stats['renamed']:2} 处"
        )

    print(
        f"\n合计：{total['cases']} 条用例被加强，"
        f"落库断言 +{total['db_added']} 条，"
        f"从后置挪回主步骤 {total['moved']} 条，"
        f"命名固定化 {total['renamed']} 处"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
