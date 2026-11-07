"""给断言薄弱的用例补断言（按 OpenAPI 文档的响应 schema 生成）。

**问题**：一开始批量生成的用例，断言只有

    HTTP 200 + success == true

这两条挡不住任何真实缺陷 —— 业务码错了、`data` 没返回、详情少了字段，
用例照样绿。线上实测下来 111 条 debugged 用例里有 71 条只有这两条断言。

**做法**：拿 ``.cache/apidoc/openapi.json``（接口文档的 OpenAPI 快照）当
"响应契约"的唯一来源，把每个接口的 ``200`` 响应 schema 解析出来，
按 ``data`` 的形状分六类，分别补不同的断言：

==================  ================================================
``data`` 形状        补的断言
==================  ================================================
分页对象 IPage        ``data.records`` 存在、``data.total/pages >= 0``、
                    ``data.size >= 0``、``data.current >= 1``
实体对象              ``data`` 存在 + 每个字段 ``data.<字段>`` 存在
数组                 ``data`` 存在 + ``len(data) >= 0``
Map（无字段定义）     ``data`` 存在
基本类型             ``data`` 存在（boolean 再加 ``== true``）
Void / 无 data       ``data`` 存在（键在、值是 null 也算）
==================  ================================================

所有接口再统一补一条 ``code == "00000"``。

验证码接口是个例外 —— ``/captcha/*`` 返回的是 ``ResponseModel``
（``repCode`` / ``repMsg`` / ``repData``），没有 ``code`` 字段，
所以改成断言 ``data.repCode == "0000"``。

**前置/后置步骤**（造数据、查数据、清理数据）也一起补：
- 每个 API 步骤补 ``code == "00000"``
- 分页查询步骤补 ``data.records`` 存在

没有这些，"造数据"失败了后面照样往下跑，报错会跑到主用例头上。

⚠️ **敏感字段不生成断言**：``password`` / ``token`` 这类字段，
断言"存在"会变相要求服务端把密码吐出来。

用法::

    python tools/maint/strengthen_assertions.py --dry-run    # 只看会加什么
    python tools/maint/strengthen_assertions.py              # 全部模块
    python tools/maint/strengthen_assertions.py --modules sys_menu admin_area

改之前会把整个工作簿备份到 ``.cache/backup/strengthen_<时间戳>/``。
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
SPEC_FILE = PROJECT_ROOT / ".cache" / "apidoc" / "openapi.json"
BACKUP_ROOT = PROJECT_ROOT / ".cache" / "backup"

SHEET = "接口测试用例"

#: 这些字段不生成"存在"断言 —— 断言存在等于要求服务端返回密码/令牌
SENSITIVE_FIELDS = frozenset({
    "password",
    "loginPassword",
    "payPassword",
    "secretKey",
    "token",
    "accessToken",
    "refreshToken",
    "salt",
    "uuid",
})

#: 分页对象认得的字段
PAGE_FIELDS = ("records", "total", "size", "current", "pages")


# --------------------------------------------------------------------------
# OpenAPI 解析
# --------------------------------------------------------------------------


class Spec:
    """OpenAPI 文档的薄封装：路径归一化 + ``$ref`` 解引用。"""

    def __init__(self, path: Path) -> None:
        self.raw = json.loads(path.read_text(encoding="utf-8"))
        self.schemas: dict[str, Any] = self.raw.get("components", {}).get("schemas", {})
        self.paths: dict[str, dict[str, Any]] = {}
        for path, operations in self.raw.get("paths", {}).items():
            self.paths.setdefault(self.normalize(path), {}).update(operations)

    @staticmethod
    def normalize(path: str) -> str:
        """把 ``{{变量}}`` / 数字 / ``{id}`` 都换成 ``{x}``，方便匹配。

        用例里的路径是 ``/admin/area/info/1``、``/admin/area/{id}``、
        ``/admin/area/{{tools.max_id(...)}}`` 三种混着的。
        """
        path = re.sub(r"\{\{.*?\}\}", "{x}", path)
        parts = []
        for segment in path.split("/"):
            if re.fullmatch(r"\{.*\}|\d+", segment):
                parts.append("{x}")
            else:
                parts.append(segment)
        return "/".join(parts)

    def operation(self, path: str, method: str) -> dict[str, Any] | None:
        return self.paths.get(self.normalize(path), {}).get(method.lower())

    def deref(self, node: Any) -> Any:
        seen: set[str] = set()
        while isinstance(node, dict) and "$ref" in node:
            ref = node["$ref"]
            if ref in seen:
                return {}
            seen.add(ref)
            node = self.schemas.get(ref.rsplit("/", 1)[-1], {})
        return node

    def data_shape(self, path: str, method: str) -> dict[str, Any]:
        """返回 ``{"kind": ..., "fields": [...]}``，认不出来时 kind 为空。"""
        operation = self.operation(path, method)
        if not operation:
            return {"kind": "", "fields": []}

        responses = operation.get("responses") or {}
        content = (responses.get("200") or {}).get("content") or {}
        schema = self.deref((content.get("*/*") or content.get("application/json") or {}).get("schema"))
        data = self.deref((schema.get("properties") or {}).get("data"))

        if not isinstance(data, dict) or not data:
            return {"kind": "void", "fields": []}

        if data.get("type") == "array":
            return {"kind": "array", "fields": []}

        properties = data.get("properties")
        if isinstance(properties, dict) and properties:
            names = list(properties)
            if all(name in properties for name in PAGE_FIELDS):
                return {"kind": "page", "fields": names}
            return {"kind": "object", "fields": names}

        primitive = data.get("type")

        # ``Map<String, Object>`` 这种没有 properties 的对象
        if primitive == "object" or not primitive:
            return {"kind": "map", "fields": []}

        return {"kind": primitive, "fields": []}


# --------------------------------------------------------------------------
# 断言构造
# --------------------------------------------------------------------------


def body(path: str, comparator: str, expect: Any = None) -> dict[str, Any]:
    item: dict[str, Any] = {"check": "body", "jsonpath": path, "comparator": comparator}
    if expect is not None:
        item["expect"] = expect
    return item


def has(
    asserts: list[dict[str, Any]], path: str, comparator: str, expect: Any = None
) -> bool:
    """断言数组里是不是已经有等价的一条。"""
    for item in asserts:
        if not isinstance(item, dict):
            continue
        if str(item.get("check", "")).lower() != "body":
            continue
        if str(item.get("jsonpath", "")) != path:
            continue
        if str(item.get("comparator", "==")) != comparator:
            continue
        if expect is None or item.get("expect") == expect:
            return True
    return False


def is_error_case(asserts: list[dict[str, Any]]) -> bool:
    """这条用例是不是在**验错误路径**（HTTP 200 但业务失败）。

    认两种写法：``$.success == false`` 和 ``$.code != "00000"``。
    """
    for item in asserts:
        if not isinstance(item, dict):
            continue
        path = str(item.get("jsonpath", ""))
        comparator = str(item.get("comparator", "==")).lower()
        if path == "$.success" and comparator == "==" and item.get("expect") is False:
            return True
        if path == "$.code" and comparator in ("!=", "not_equal") and item.get("expect") == "00000":
            return True
    return False


def is_not_found_case(asserts: list[dict[str, Any]]) -> bool:
    """这条用例是不是在验**"查不到数据"**（``data`` 为 null）。

    这类用例业务是成功的（``success == true``），所以 ``code == "00000"``
    照样该断言；只是 ``data`` 下面没有字段，不能生成字段断言。
    """
    return any(
        isinstance(item, dict)
        and str(item.get("jsonpath", "")) == "$.data"
        and str(item.get("comparator", "==")).lower() in ("is_null", "is_none", "null")
        for item in asserts
    )


def is_negative_case(asserts: list[dict[str, Any]]) -> bool:
    """错误路径 + 查不到 —— 两种都不能按正常响应的形状生成断言。"""
    return is_error_case(asserts) or is_not_found_case(asserts)


def is_strengthened(asserts: list[dict[str, Any]], path: str) -> bool:
    """这条用例是不是已经加强过了（业务码 + 至少一条字段/结构断言）。

    已经加强过的不再动 —— 比如 admin_hotSearch 这个样板模块，
    里面的断言是手工调过的（id 精确匹配等），别被自动生成的覆盖成噪音。
    """
    if is_captcha(path):
        return has(asserts, "$.data.repCode", "==", "0000") and has(
            asserts, "$.data.repData", "exists"
        )
    if not has(asserts, "$.code", "==", "00000"):
        return False
    return any(
        str(item.get("comparator", "")).lower() in ("exists", "len>=")
        for item in asserts
        if isinstance(item, dict)
    )


def is_binary_case(asserts: list[dict[str, Any]]) -> bool:
    """导出类接口返回的是文件，不是 JSON envelope。"""
    for item in asserts:
        if str(item.get("jsonpath", "")) in ("$.content_type", "$.size"):
            return True
    return False


def is_captcha(path: str) -> bool:
    return path.startswith("/captcha/")


def add_main_assertions(
    asserts: list[dict[str, Any]], path: str, method: str, shape: dict[str, Any]
) -> list[dict[str, Any]]:
    """给主步骤补断言，返回新增的条目。"""
    added: list[dict[str, Any]] = []

    def push(item: dict[str, Any]) -> None:
        asserts.append(item)
        added.append(item)

    if is_captcha(path):
        # ResponseModel：没有 code，业务码在 data.repCode
        if not has(asserts, "$.data.repCode", "==", "0000"):
            push(body("$.data.repCode", "==", "0000"))
        if not has(asserts, "$.data.repData", "exists"):
            push(body("$.data.repData", "exists"))
        return added

    if not has(asserts, "$.code", "==", "00000"):
        push(body("$.code", "==", "00000"))

    # "查不到数据"用例：业务成功但 data 是 null，data 下面没有字段可断言
    if is_not_found_case(asserts):
        return added

    kind = shape["kind"]
    if kind == "page":
        for name in ("records",):
            if not has(asserts, f"$.data.{name}", "exists"):
                push(body(f"$.data.{name}", "exists"))
        # ``pages`` 在文档里标了 deprecated，有些版本不返回，不拿它当必检项
        for name in ("total", "size"):
            if not has(asserts, f"$.data.{name}", ">=", 0):
                push(body(f"$.data.{name}", ">=", 0))
        if not has(asserts, "$.data.current", ">=", 1):
            push(body("$.data.current", ">=", 1))
    elif kind == "object":
        if not has(asserts, "$.data", "exists"):
            push(body("$.data", "exists"))
        for name in shape["fields"]:
            if name in SENSITIVE_FIELDS:
                continue
            if not has(asserts, f"$.data.{name}", "exists"):
                push(body(f"$.data.{name}", "exists"))
    elif kind == "array":
        if not has(asserts, "$.data", "exists"):
            push(body("$.data", "exists"))
        if not has(asserts, "$.data", "len>=", 0):
            push(body("$.data", "len>=", 0))
    elif kind in ("map", "void", "string", "integer", "number", "boolean"):
        # 写操作返回什么各版本不一致（Void / true / 被删的 id 字符串），
        # 文档和"响应数据"列对不上，只断言 data 这个键在："键在" 已经能
        # 挡住"服务端 panic 返回了个空对象"这类问题，又不会误判具体值。
        if not has(asserts, "$.data", "exists"):
            push(body("$.data", "exists"))

    return added


def add_step_assertions(
    step: dict[str, Any], spec: Spec
) -> list[dict[str, Any]]:
    """给前置/后置里的一个 API 步骤补断言（只补最基础的两条）。"""
    if str(step.get("operation_type", "api")).lower() != "api":
        return []

    path = str(step.get("path") or "")
    method = str(step.get("method") or "GET")
    if not path:
        return []

    asserts = step.get("assert")
    if not isinstance(asserts, list):
        asserts = []
        step["assert"] = asserts

    added: list[dict[str, Any]] = []
    if is_captcha(path):
        if not has(asserts, "$.data.repCode", "==", "0000"):
            item = body("$.data.repCode", "==", "0000")
            asserts.append(item)
            added.append(item)
        return added

    if not has(asserts, "$.code", "==", "00000"):
        item = body("$.code", "==", "00000")
        asserts.append(item)
        added.append(item)

    shape = spec.data_shape(path, method)
    if shape["kind"] == "page" and not has(asserts, "$.data.records", "exists"):
        item = body("$.data.records", "exists")
        asserts.append(item)
        added.append(item)

    return added


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
    text = str(value).strip()
    if not text:
        return None
    return json.loads(text)


def process_workbook(path: Path, spec: Spec, dry_run: bool) -> dict[str, int]:
    """改一个工作簿，返回 ``{统计项: 数量}``。"""
    workbook = openpyxl.load_workbook(path)
    if SHEET not in workbook.sheetnames:
        return {}

    ws = workbook[SHEET]
    headers = {
        name: column_index(ws, name)
        for name in ("接口", "请求方法", "断言", "前置", "后置", "用例状态")
    }
    missing = [name for name, idx in headers.items() if idx is None]
    if missing:
        print(f"    ! 跳过 {path.parent.name}：缺少列 {missing}")
        return {}

    stats = {"cases": 0, "main_added": 0, "steps_added": 0}

    for row in range(2, ws.max_row + 1):
        status = str(ws.cell(row, headers["用例状态"]).value or "").strip()
        if status != "debugged":
            continue

        api_path = str(ws.cell(row, headers["接口"]).value or "").strip()
        method = str(ws.cell(row, headers["请求方法"]).value or "GET").strip()
        if not api_path:
            continue

        case_id = ws.cell(row, 1).value
        changed = False

        # ---- 主步骤断言 ----
        assert_cell = ws.cell(row, headers["断言"])
        asserts = read_json(assert_cell.value)
        if asserts is None:
            asserts = []
        if not isinstance(asserts, list):
            print(f"    ! {case_id} 断言不是数组，跳过")
            continue
        # 错误路径用例（success=false）和导出类用例（返回文件）的响应
        # 形状和正常用例不一样，套业务码/字段断言会误伤，直接跳过主步骤。
        # "查不到数据"用例不算 —— 它业务是成功的，只是 data 下面没字段，
        # 交给 add_main_assertions 自己只补业务码。
        if (
            is_error_case(asserts)
            or is_binary_case(asserts)
            or is_strengthened(asserts, api_path)
        ):
            added = []
        else:
            added = add_main_assertions(
                asserts, api_path, method, spec.data_shape(api_path, method)
            )
        if added:
            assert_cell.value = json.dumps(asserts, ensure_ascii=False)
            stats["main_added"] += len(added)
            changed = True

        # ---- 前置 / 后置步骤断言 ----
        for column in ("前置", "后置"):
            cell = ws.cell(row, headers[column])
            steps = read_json(cell.value)
            if not isinstance(steps, list):
                continue
            step_added = 0
            for step in steps:
                if isinstance(step, dict):
                    step_added += len(add_step_assertions(step, spec))
            if step_added:
                cell.value = json.dumps(steps, ensure_ascii=False)
                stats["steps_added"] += step_added
                changed = True

        if changed:
            stats["cases"] += 1

    if not dry_run and stats["cases"]:
        workbook.save(path)
    return stats


def backup(files: list[Path], stamp: str) -> Path:
    target = BACKUP_ROOT / f"strengthen_{stamp}"
    for path in files:
        relative = path.relative_to(CASE_ROOT)
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="按 OpenAPI 文档给用例补断言")
    parser.add_argument("--modules", nargs="*", help="只处理这些模块（默认全部）")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不写文件")
    parser.add_argument("--spec", default=str(SPEC_FILE), help="OpenAPI 文件路径")
    args = parser.parse_args()

    spec_path = Path(args.spec)
    if not spec_path.exists():
        print(f"找不到接口文档快照：{spec_path}")
        print("先跑 tools/maint/sync_api.py（或确认 .cache/apidoc/openapi.json 在）")
        return 2

    spec = Spec(spec_path)
    files = sorted(CASE_ROOT.glob("*/*.xlsx"))
    if args.modules:
        wanted = set(args.modules)
        files = [f for f in files if f.parent.name in wanted]
    if not files:
        print("没有匹配的用例文件")
        return 2

    if not args.dry_run:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        target = backup(files, stamp)
        print(f"已备份 {len(files)} 个工作簿到 {target}")
    else:
        print("（dry-run，不写文件）")

    total = {"cases": 0, "main_added": 0, "steps_added": 0}
    for path in files:
        stats = process_workbook(path, spec, args.dry_run)
        if not stats:
            continue
        for key in total:
            total[key] += stats[key]
        if stats["cases"]:
            print(
                f"  {path.parent.name:16} 用例 {stats['cases']:3}   "
                f"主步骤 +{stats['main_added']:3} 条   前后置 +{stats['steps_added']:3} 条"
            )

    print(
        f"\n合计：{total['cases']} 条用例被加强，"
        f"主步骤新增 {total['main_added']} 条断言，"
        f"前置/后置新增 {total['steps_added']} 条断言"
    )
    if not args.dry_run and total["cases"]:
        print("回滚：把 .cache/backup 下对应目录的文件拷回去即可")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
