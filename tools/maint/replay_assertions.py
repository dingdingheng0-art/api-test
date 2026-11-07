"""离线回放：把真实用例里的断言套在"正确 / 有缺陷"的响应上，看会不会变红。

**为什么需要**：实网登录挂了（A00005）时用例一条都跑不了，但"断言有没有用"
是另一件事，不该跟着一起停摆。这里用库里的真实数据造一份正确响应，再人为
制造三种缺陷，断言该过的过、该红的红：

- 少一个字段 —— 字段存在性断言要抓到
- 业务码改错 —— 业务码断言要抓到
- `data` 变成 null —— 详情正例的断言要抓到

库里的表结构映射复用 ``tools/maint/add_db_assertions.py`` 的 ``MODULES``。

用法::

    python tools/maint/replay_assertions.py
    python tools/maint/replay_assertions.py --case API-152
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.maint.add_db_assertions import MODULES, camel_case  # noqa: E402
from tools.maint.dump_schema import resolve_db_config  # noqa: E402
from utils.assertions.assert_handler import AssertHandler  # noqa: E402
from utils.excel.flat_parser import load_flat_cases  # noqa: E402
from verify.db import DatabaseClient  # noqa: E402

CASE_ROOT = PROJECT_ROOT / "testcases" / "cases_excel"

#: 每条是 (用例号, 模块, 库里主键值) —— 主键值都是脚本查库确认过的
SAMPLES = [
    ("API-152", "admin_hotSearch", 1),
    ("API-129", "sys_user", 1),
    ("API-032", "sys_menu", 1),
    ("API-023", "sys_role", 1),
    ("API-069", "shop_notice", 1),
    ("API-109", "prod_category", 1),
    ("API-087", "prod_prod", 18),
    ("API-174", "admin_area", 999999999),  # 查不到：库里确实没有这行
]


def load_case(case_id: str) -> dict[str, Any] | None:
    for path in sorted(CASE_ROOT.rglob("*.xlsx")):
        cases, _ = load_flat_cases(path)
        for raw in cases:
            case = raw["testcase"]
            if case.get("case_id") == case_id:
                case["_module"] = path.parent.name
                return case
    return None


def _plain(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, Decimal):
        return float(value)
    return value


def fetch_row(client: DatabaseClient, module: dict[str, Any], pk_value: Any) -> dict[str, Any] | None:
    rows = client.query(
        f"SELECT * FROM {module['table']} WHERE {module['pk']} = %s", [pk_value]
    )
    return rows[0] if rows else None


def to_response(row: dict[str, Any]) -> dict[str, Any]:
    """库里的行 → 接口响应（列名转驼峰，外面套 mall4j 的 envelope）。"""
    data = {camel_case(column): _plain(value) for column, value in row.items()}
    return {
        "code": "00000",
        "data": data,
        "fail": False,
        "msg": None,
        "success": True,
        "version": "mall4j.v230424",
    }


def split_asserts(case: dict[str, Any]) -> list[dict[str, Any]]:
    """只留响应断言 —— 查库断言要连库，回放里单独跳过。

    ``load_flat_cases`` 已经把扁平格式翻译过了，所以判据是 ``type``
    （``db_query`` / ``db_query_exists`` …），不是原来的 ``check: db``。
    """
    asserts = case["steps"][-1].get("assert") or []
    return [item for item in asserts if not str(item.get("type", "")).startswith("db_")]


def expected_data_fields(asserts: list[dict[str, Any]]) -> set[str]:
    """用例自己点名要存在的 ``data`` 子字段。"""
    fields = set()
    for item in asserts:
        field = str(item.get("field") or "")
        if field.startswith("data.") and "." not in field[len("data."):]:
            fields.add(field[len("data."):])
    return fields


def to_response(row: dict[str, Any], wanted: set[str]) -> dict[str, Any]:
    """库里的行 → 接口响应（列名转驼峰，外面套 mall4j 的 envelope）。

    库里有值的字段用真值；接口联表算出来的字段（``parentName`` /
    ``menuIdList`` / ``skuList`` 这类）库里没有，用占位值补上 ——
    这一步只验证"断言逻辑对不对"，不是验证接口真实返回形状。
    """
    data = {camel_case(column): _plain(value) for column, value in row.items()}
    for field in wanted:
        data.setdefault(field, f"<{field}>")
    return {
        "code": "00000",
        "data": data,
        "fail": False,
        "msg": None,
        "success": True,
        "version": "mall4j.v230424",
    }


def check(asserts: list[dict[str, Any]], response: dict[str, Any]) -> str:
    try:
        AssertHandler().handle_assertion(
            asserts, response, None, {"status_code": 200, "headers": {}}
        )
    except AssertionError as exc:
        return f"FAIL → {exc}"
    except Exception as exc:  # noqa: BLE001
        return f"ERROR → {type(exc).__name__}: {exc}"
    return "PASS"


def main() -> int:
    parser = argparse.ArgumentParser(description="离线回放用例断言")
    parser.add_argument("--case", help="只看某条用例")
    args = parser.parse_args()

    # 查库断言走 AssertHandler → VerifyManager，得先把 mall4j 别名注册进去
    from verify.manager import VerifyManager

    VerifyManager.get().setup({"db": {"mall4j": resolve_db_config("mall4j")}})
    client = DatabaseClient(resolve_db_config("mall4j"))
    samples = [s for s in SAMPLES if not args.case or s[0] == args.case]
    if not samples:
        print(f"没有样例用例 {args.case!r}（可选：{', '.join(s[0] for s in SAMPLES)}）")
        return 2

    for case_id, module_name, pk_value in samples:
        case = load_case(case_id)
        if case is None:
            print(f"{case_id}: 用例不存在")
            continue
        module = MODULES[module_name]
        row = fetch_row(client, module, pk_value)
        asserts = split_asserts(case)
        expects_missing = any(
            str(item.get("type")) in ("is_none", "is_null")
            and str(item.get("field")) == "data"
            for item in asserts
        )
        if row is None and not expects_missing:
            print(f"{case_id}: 库里没有 {module['table']}.{module['pk']}={pk_value}，跳过")
            continue
        if expects_missing:
            # "查不到"用例：正确响应就是 data 为 null
            good = {"code": "00000", "data": None, "fail": False, "msg": None,
                    "success": True, "version": "mall4j.v230424"}
        else:
            good = to_response(row, expected_data_fields(asserts))
        print(f"\n{'=' * 72}")
        print(f"{case_id} {case['name']}（{case['_module']}，{len(asserts)} 条非查库断言）")
        print("=" * 72)
        print(f"  正确响应            : {check(asserts, good)}")

        if expects_missing:
            # "查不到"用例：反过来，数据出现了才是缺陷
            bogus = {"code": "00000", "data": {"areaId": pk_value}, "fail": False,
                     "msg": None, "success": True, "version": "mall4j.v230424"}
            print(f"  接口突然查到数据      : {check(asserts, bogus)}")
            db_asserts = [
                item
                for item in (case["steps"][-1].get("assert") or [])
                if str(item.get("type", "")).startswith("db_")
            ]
            for item in db_asserts:
                sql = str(item.get("query") or item.get("sql") or "")
                print(f"  查库 {sql[:52]:<52}: {check([item], good)}")
            continue

        # 缺陷 1：少一个字段（挑断言里点名的第一个 data 子字段）
        target = next(
            (
                str(item.get("field"))
                for item in asserts
                if str(item.get("type")) == "exists"
                and str(item.get("field", "")).startswith("data.")
            ),
            None,
        )
        if target:
            broken = {**good, "data": dict(good["data"])}
            broken["data"].pop(target.split(".")[-1], None)
            print(f"  少字段 {target:<22}: {check(asserts, broken)}")

        # 缺陷 2：业务码错
        print(f"  业务码改成 A00005    : {check(asserts, {**good, 'code': 'A00005'})}")

        # 缺陷 3：data 变 null（详情查不到）
        print(f"  data 变 null         : {check(asserts, {**good, 'data': None})}")

        # 查库断言：真连库跑（库是通的，接口登录挂了不影响这一步）
        db_asserts = [
            item
            for item in (case["steps"][-1].get("assert") or [])
            if str(item.get("type", "")).startswith("db_")
        ]
        for item in db_asserts:
            sql = str(item.get("query") or item.get("sql") or "")
            print(f"  查库 {sql[:52]:<52}: {check([item], good)}")

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
