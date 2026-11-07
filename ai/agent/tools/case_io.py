"""用例文件的读写底座 —— 智能体改写用例的唯一通道。

**为什么不能直接改文件**

用例有**两套格式**，断言长得完全不一样：

========================  ==========================================
格式                       断言长什么样
========================  ==========================================
Excel（扁平格式，主用）     ``{"check": "body", "jsonpath": "$.code",
                            "comparator": "==", "expect": "00000"}``
YAML（框架内部格式）        ``{"type": "equal", "field": "code",
                            "expected": "00000"}``
========================  ==========================================

而智能体看到的**一直是框架内部格式**（``get_case`` 返回的那种）。
它照着内部格式往 Excel 里写，文件就废了 —— 而且要到下次跑用例才发现。

所以读写必须**成对**：

- 读：``utils.excel.flat_parser`` 把两套格式都转成内部格式
- 写：本模块把内部格式转回扁平格式，再落盘

**为什么要按单元格写**

一行有 19 列，``flat_parser`` 只认其中一部分。``响应数据(预期参考)``
这种列它读都不读 —— 整行重写会把这列**清空**。所以只动要改的格子。

**安全底线（三道闸）**

1. **只能按 ``case_id`` 定位** —— 用例名会重名（「查询地区详情」有正常和
   查不到数据两条），行号会因插行而变，只有 ``API-168`` 这种编号稳定。
2. **写前备份** —— 原文件复制到 ``.cache/backup/agent_<时间戳>/``，
   连同本次改了什么一起写进 ``manifest.json``。
3. **写后复验，失败回滚** —— 改完重新解析 + 跑 ``CaseValidator``，
   有错就恢复原文件并报错。宁可拒绝，也不留一个跑不起来的用例。
"""

from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai.agent.tools.registry import ToolError
from utils.logs.log_manager import log

#: 扁平格式的主表名（和 ``utils.excel.flat_parser.SHEET_FLAT`` 一致）
SHEET_FLAT = "接口测试用例"

#: 新建 Excel 用例文件时的表头（和现有文件保持一致）
FLAT_HEADERS = [
    "case_id",
    "模块",
    "标签",
    "用例名称",
    "优先级",
    "接口名称",
    "用例标题",
    "接口",
    "请求方法",
    "请求头",
    "请求数据",
    "响应数据(预期参考)",
    "断言",
    "参数化数据",
    "场景",
    "前置",
    "后置",
    "用例状态",
    "查询参数",
]

#: 框架断言类型 → 扁平比较符。只列**能无损表示**的那些。
_TYPE_TO_COMPARATOR = {
    "equal": "==",
    "equals": "==",
    "==": "==",
    "not_equal": "!=",
    "notequal": "!=",
    "!=": "!=",
    "gt": ">",
    "greater_than": ">",
    "gte": ">=",
    "greater_than_or_equal": ">=",
    "lt": "<",
    "less_than": "<",
    "lte": "<=",
    "less_than_or_equal": "<=",
    "contains": "contains",
    "not_contains": "not_contains",
    "is_none": "is_none",
    "none": "is_none",
    "is_not_none": "is_not_none",
    "not_none": "is_not_none",
    "exists": "exists",
    "field_exists": "exists",
    "not_exists": "not_exists",
    "field_not_exists": "not_exists",
    "matches": "regex",
    "regex": "regex",
    "match": "regex",
    "length": "len==",
    "len": "len==",
    "length_ne": "len!=",
    "length_gt": "len>",
    "length_gte": "len>=",
    "length_lt": "len<",
    "length_lte": "len<=",
}

#: 落库断言类型 → 扁平比较符
_DB_TYPE_TO_COMPARATOR = {
    "db_query": "==",
    "db_query_gt": ">",
    "db_query_gte": ">=",
    "db_query_lt": "<",
    "db_query_lte": "<=",
    "db_query_exists": "exists",
    "db_query_not_exists": "not_exists",
}

#: 这些类型就算 expected 是 None 也要写出 expect（否则解析回来含义变了）
_ALWAYS_EXPECT = frozenset({
    "equal", "equals", "==", "not_equal", "notequal", "!=",
    "gt", "gte", "lt", "lte",
    "full_body", "body_equal",
    "length", "len", "length_ne", "length_gt", "length_gte", "length_lt", "length_lte",
})


# ==========================================================================
# 内部格式 → 扁平格式
# ==========================================================================


def field_to_jsonpath(field: str) -> str:
    """``data.list[0].id`` → ``$.data.list[0].id``。"""
    text = str(field or "").strip()
    if not text:
        return "$"
    if text.startswith("$"):
        return text
    return "$." + text.lstrip(".")


def _with_expect(
    item: dict[str, Any], assertion: dict[str, Any], atype: str
) -> dict[str, Any]:
    """按需带上 ``expect``。

    ``exists`` 这类断言本来就不比期望值，硬塞一个 ``"expect": null``
    会让单元格和现有写法不一致，diff 里全是噪声。

    但 ``equal`` / ``gt`` 这些**必须**有期望值 —— 少一个就等于换了语义，
    所以直接报错，让模型把断言写完整。
    """
    if "expected" not in assertion:
        if atype in _ALWAYS_EXPECT:
            raise ToolError(
                f"断言 {assertion.get('type')!r} 缺少 expected（期望值）",
                hint="比较类断言必须写期望值，否则没法判断通过还是失败",
            )
        return item

    value = assertion["expected"]
    if value is not None or atype in _ALWAYS_EXPECT:
        item["expect"] = value
    return item


def _require_field(assertion: dict[str, Any], atype: str) -> str:
    """取字段路径；没有就报错。

    **为什么这里必须报错**

    扁平格式里 ``jsonpath: "$"`` 表示「断言整个响应体」——
    如果 ``{"type": "equal", "expected": "x"}`` 漏了 field 还照写，
    它会被静默转成 ``full_body``，语义完全变了，而且**校验能过**
    （``full_body`` 本来就不要 field）。这种"改完看不出错"的写法
    比直接报错危险得多。
    """
    field = str(assertion.get("field") or "").strip()
    if not field:
        raise ToolError(
            f"断言 {assertion.get('type')!r} 缺少 field（要断言的字段路径）",
            hint="字段路径不带 $. 前缀，比如 data.records、code、data.list[0].id",
        )
    return field


def assertion_to_flat(assertion: dict[str, Any]) -> dict[str, Any]:
    """把一条框架内部断言翻译成扁平格式。

    翻不出来的（``subset`` / ``jwt_claim`` / ``redis_*`` 等）**直接报错**，
    不静默丢弃 —— 悄悄少一条断言等于削弱了用例，比报错危险得多。
    """
    if not isinstance(assertion, dict):
        raise ToolError(f"断言必须是对象，拿到的是 {type(assertion).__name__}")

    atype = str(assertion.get("type") or "").strip()
    lowered = atype.lower().replace(" ", "_")

    if not lowered:
        raise ToolError("断言缺少 type", hint='形如 {"type": "equal", "field": "code", "expected": "00000"}')

    # ---- 状态码 ----
    if lowered in ("status_code", "status", "http_status"):
        return {
            "check": "status_code",
            "comparator": "==",
            "expect": assertion.get("expected"),
        }

    # ---- 响应头 ----
    if lowered in ("response_header", "header"):
        key = assertion.get("field") or assertion.get("key")
        if not key:
            raise ToolError("response_header 断言缺少 field（响应头名字）")
        return {
            "check": "header",
            "key": str(key),
            "comparator": "==",
            "expect": assertion.get("expected"),
        }

    # ---- 耗时（框架用毫秒，扁平格式用秒）----
    if lowered in ("elapsed_time", "elapsed", "response_time"):
        value = assertion.get("expected")
        seconds = round(value / 1000, 3) if isinstance(value, (int, float)) else value
        return {"check": "time", "comparator": "==", "expect": seconds}

    # ---- 整个响应体 ----
    if lowered in ("full_body", "body_equal"):
        item: dict[str, Any] = {
            "check": "full_body",
            "comparator": str(assertion.get("comparator") or "=="),
        }
        return _with_expect(item, assertion, lowered)

    # ---- 落库断言 ----
    #
    # 键的顺序刻意和现有用例保持一致（check → sql → comparator → alias →
    # expect → params）：同样的断言再写一遍，单元格内容**逐字节相同**，
    # 幂等写入不会在 diff 里制造假改动。
    if lowered in _DB_TYPE_TO_COMPARATOR:
        sql = assertion.get("query") or assertion.get("sql")
        if not sql:
            raise ToolError("落库断言缺少 query（SQL 语句）")
        item = {
            "check": "db",
            "sql": str(sql).strip(),
            "comparator": _DB_TYPE_TO_COMPARATOR[lowered],
        }
        if assertion.get("alias"):
            item["alias"] = assertion["alias"]
        if assertion.get("column"):
            item["column"] = assertion["column"]
        item = _with_expect(item, assertion, lowered)
        if assertion.get("params"):
            item["params"] = assertion["params"]
        return item

    if lowered == "db_query_in":
        sql = assertion.get("query") or assertion.get("sql")
        if not sql:
            raise ToolError("落库断言缺少 query（SQL 语句）")
        item = {
            "check": "db",
            "sql": str(sql).strip(),
            "comparator": "in",
            "alias": assertion.get("alias") or "",
            "expect": assertion.get("container"),
        }
        if not item["alias"]:
            item.pop("alias")
        if assertion.get("params"):
            item["params"] = assertion["params"]
        return item

    # ---- 布尔值（扁平格式没有 is_true，用 == true 完全等价）----
    if lowered in ("is_true", "true", "is_false", "false"):
        return {
            "check": "body",
            "jsonpath": field_to_jsonpath(_require_field(assertion, lowered)),
            "comparator": "==",
            "expect": lowered in ("is_true", "true"),
        }

    # ---- 集合 ----
    if lowered in ("in", "not_in"):
        return {
            "check": "body",
            "jsonpath": field_to_jsonpath(_require_field(assertion, lowered)),
            "comparator": lowered,
            "expect": assertion.get("container"),
        }

    # ---- 普通取值 ----
    comparator = _TYPE_TO_COMPARATOR.get(lowered)
    if comparator is not None:
        item = {
            "check": "body",
            "jsonpath": field_to_jsonpath(_require_field(assertion, lowered)),
            "comparator": comparator,
        }
        return _with_expect(item, assertion, lowered)

    raise ToolError(
        f"断言类型 {atype!r} 没法写回 Excel 的扁平格式",
        hint=(
            "扁平格式能表示：status_code / body 取值类 / db 落库类 / header / time / full_body。"
            "subset、jwt_claim、redis_*、mq_* 这些只能写在 YAML 用例里。"
            "如果是想删掉这条断言，请显式说明理由 —— 别用「写不进去」当作绕过的借口。"
        ),
    )


def assertions_to_flat(assertions: Any) -> list[dict[str, Any]]:
    """一组断言 → 扁平格式。"""
    if not isinstance(assertions, list):
        raise ToolError(
            "断言必须是数组",
            hint='形如 [{"type": "equal", "field": "code", "expected": "00000"}]',
        )
    return [assertion_to_flat(a) for a in assertions]


# ==========================================================================
# 定位：case_id → 文件 + 行
# ==========================================================================


@dataclass
class CaseLocation:
    """一条用例落在哪个文件。"""

    case_id: str
    file: Path
    fmt: str  # "excel" | "yaml"


def _load_all(project: str = "") -> list[tuple[Path, dict[str, Any]]]:
    """扫一遍所有用例，返回 ``[(文件, 用例本体), ...]``。"""
    from testcases.case_loader import discover_cases, load_cases_from_file

    out: list[tuple[Path, dict[str, Any]]] = []
    for proj, path in discover_cases():
        if project and proj != project:
            continue
        try:
            loaded = load_cases_from_file(path)
        except Exception as exc:  # noqa: BLE001 - 坏文件跳过，不该挡住别的文件
            log.debug("跳过读不了的用例文件 %s: %s", path, exc)
            continue
        for data in loaded:
            case = data.get("testcase") if isinstance(data.get("testcase"), dict) else data
            if isinstance(case, dict):
                out.append((path, case))
    return out


def case_key(case: dict[str, Any]) -> str:
    """一条用例的稳定标识：优先 ``case_id``，YAML 用例退回用例名。"""
    return str(case.get("case_id") or case.get("name") or "").strip()


def _case_of(case_no: str, project: str = "") -> tuple[Path, dict[str, Any]]:
    """按 ``case_id`` 找用例；找不到再按 YAML 用例名找。"""
    wanted = str(case_no or "").strip()
    if not wanted:
        raise ToolError("必须给用例编号", hint="形如 API-168；用 list_cases 可以看到")

    loaded = _load_all(project)

    # 先按 case_id 找 —— Excel 用例都有，是最稳的身份
    for path, case in loaded:
        if str(case.get("case_id") or "").strip() == wanted:
            return path, case

    # 再按名字找，但**只认 YAML** —— Excel 里重名的用例太多，
    # 按名字改可能改到隔壁那条，宁可报错让人指定编号。
    matches = [
        (path, case)
        for path, case in loaded
        if path.suffix.lower() == ".yaml" and str(case.get("name") or "").strip() == wanted
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ToolError(
            f"有 {len(matches)} 条 YAML 用例都叫 {wanted!r}，没法确定改哪一条",
            hint="给这条用例加个 case_id，或者直接编辑文件",
        )

    raise ToolError(
        f"找不到用例 {wanted!r}",
        hint="用 list_cases 确认编号（形如 API-168）。Excel 用例只认 case_id，"
        "不按名字定位 —— 同模块里重名的情况很常见。",
    )


def locate(case_no: str, project: str = "") -> CaseLocation:
    """按 ``case_id`` 找出用例在哪个文件。"""
    path, _case = _case_of(case_no, project)
    fmt = "excel" if path.suffix.lower() in (".xlsx", ".xlsm") else "yaml"
    return CaseLocation(case_id=str(case_no).strip(), file=path, fmt=fmt)


def _step_of(case: dict[str, Any], step: str = "") -> dict[str, Any]:
    """取要改的那个步骤。扁平格式一行只有一步，所以默认就取它。"""
    steps = [s for s in (case.get("steps") or []) if isinstance(s, dict)]
    if not steps:
        raise ToolError(f"用例 {case.get('case_id') or case.get('name')} 没有步骤")

    if step:
        target = next((s for s in steps if str(s.get("id")) == step), None)
        if target is None:
            raise ToolError(
                f"用例里没有步骤 {step!r}",
                hint=f"可选步骤: {[s.get('id') for s in steps]}",
            )
        return target

    if len(steps) > 1:
        raise ToolError(
            f"这条用例有 {len(steps)} 个步骤，必须指定改哪一个",
            hint=f"可选步骤: {[s.get('id') for s in steps]}",
        )
    return steps[0]


# ==========================================================================
# 读
# ==========================================================================


def read_assertions(case_no: str, project: str = "") -> dict[str, Any]:
    """读一条用例当前的全部断言（内部格式 + 扁平格式对照）。"""
    path, case = _case_of(case_no, project)
    fmt = "excel" if path.suffix.lower() in (".xlsx", ".xlsm") else "yaml"

    steps = []
    for step in case.get("steps") or []:
        if not isinstance(step, dict):
            continue
        asserts = step.get("assert") or []
        entry: dict[str, Any] = {
            "step": str(step.get("id") or ""),
            "method": str(step.get("method") or "").upper(),
            "path": step.get("path") or "",
            "assertions": asserts,
        }
        # Excel 用例额外给出扁平写法 —— 让模型看清「写进去长什么样」
        if fmt == "excel":
            try:
                entry["flat"] = assertions_to_flat(asserts)
            except ToolError as exc:
                entry["flat_error"] = str(exc)
        steps.append(entry)

    return {
        "case_id": str(case_no).strip(),
        "file": str(path),
        "format": fmt,
        "name": case.get("name") or "",
        "status": case.get("status") or "",
        "steps": steps,
    }


# ==========================================================================
# 写：备份 → 改 → 复验 → 失败回滚
# ==========================================================================


def _stamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S") + f"_{int(time.time() * 1000) % 1000:03d}"


def _backup(path: Path, manifest: dict[str, Any]) -> Path:
    """把原文件备份到 ``.cache/backup/agent_<时间戳>/``。

    manifest 一起写进去 —— 事后想回滚、或者查「谁改的、为什么改」全靠它。
    """
    from utils import paths

    target_dir = paths.CACHE_DIR / "backup" / f"agent_{_stamp()}"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / path.name
    shutil.copy2(path, target)

    (target_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log.info("已备份 %s → %s", path.name, target)
    return target


def _restore(backup: Path, path: Path) -> None:
    shutil.copy2(backup, path)
    log.warning("已回滚 %s（从 %s）", path.name, backup)


def restore_from_backup(backup_dir: str | Path) -> str:
    """把一次改动整个撤回去（自愈流程验证失败时用）。

    ``_backup`` 每个目录只放一个用例文件 + 一份 manifest，
    所以不用调用方指定「恢复哪个文件」—— 指定错了就改到别的文件上了。

    Returns:
        被恢复的文件路径。
    """
    directory = Path(backup_dir)
    if not directory.is_dir():
        raise ToolError(
            f"备份目录不存在：{directory}",
            hint="只能撤销本次会话里刚做过的改动；更早的备份请手工恢复。",
        )

    candidates = [
        p for p in directory.iterdir() if p.is_file() and p.name != "manifest.json"
    ]
    if len(candidates) != 1:
        raise ToolError(
            f"备份目录里有 {len(candidates)} 个文件，不知道该恢复哪个：{directory}",
            hint="这个目录不是 apply_* 工具生成的，请手工恢复。",
        )

    source = candidates[0]
    manifest_path = directory / "manifest.json"
    if not manifest_path.exists():
        raise ToolError(f"备份目录缺少 manifest.json：{directory}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    target = Path(str(manifest.get("file") or ""))
    if not target.is_absolute():
        raise ToolError(f"manifest 里的文件路径不合法：{target}")

    shutil.copy2(source, target)

    from ai.agent.tools._common import clear_cache

    clear_cache()
    log.warning("已按备份恢复 %s（来自 %s）", target, directory)
    return str(target)


def _verify(path: Path, case_no: str) -> list[str]:
    """重新解析 + 校验目标用例，返回错误清单（空 = 通过）。"""
    from testcases.case_loader import load_cases_from_file
    from utils.cases.case_validator import CaseValidator

    try:
        loaded = load_cases_from_file(path)
    except Exception as exc:  # noqa: BLE001
        return [f"文件解析失败：{exc}"]

    target = None
    for data in loaded:
        case = data.get("testcase") if isinstance(data.get("testcase"), dict) else data
        if case_key(case) == str(case_no).strip():
            target = data
            break

    if target is None:
        return [f"写回后在文件里找不到用例 {case_no}"]

    return CaseValidator().validate(target)


def _write_with_guard(
    path: Path,
    case_no: str,
    manifest: dict[str, Any],
    write: Any,
) -> Path:
    """统一的写入流程：备份 → 写 → 复验 → 失败回滚。"""
    backup = _backup(path, manifest)
    try:
        write()
        errors = _verify(path, case_no)
    except Exception as exc:  # noqa: BLE001 - 写挂了也要还原
        _restore(backup, path)
        raise ToolError(
            f"写入失败，已回滚：{exc}",
            hint=f"原文件已恢复。备份目录：{backup.parent}",
        ) from exc

    if errors:
        _restore(backup, path)
        raise ToolError(
            "写回后的用例没通过校验，已回滚：" + "；".join(errors[:5]),
            hint=f"原文件已恢复。备份目录：{backup.parent}",
        )

    # 写完立刻清缓存 —— 否则 list_cases / get_case 还会返回改之前那份
    from ai.agent.tools._common import clear_cache

    clear_cache()
    return backup


def _header_index(sheet: Any) -> dict[str, int]:
    return {
        str(cell.value or "").strip(): idx
        for idx, cell in enumerate(sheet[1], start=1)
        if str(cell.value or "").strip()
    }


def _open_sheet(path: Path) -> tuple[Any, Any]:
    """打开工作簿并返回 ``(workbook, 扁平表)``。"""
    try:
        import openpyxl
    except ImportError as exc:  # pragma: no cover - 环境问题
        raise ToolError("没装 openpyxl，改不了 Excel 用例", hint="uv pip install openpyxl") from exc

    workbook = openpyxl.load_workbook(path)
    if SHEET_FLAT not in workbook.sheetnames:
        raise ToolError(
            f"{path.name} 不是扁平格式（没有「{SHEET_FLAT}」表），暂不支持智能体改写",
            hint="老的四表格式请手工改，或者先迁移成扁平格式",
        )
    return workbook, workbook[SHEET_FLAT]


def _locate_row(sheet: Any, headers: dict[str, int], case_no: str) -> int:
    """按 ``case_id`` 找行。

    不用行号也不用用例名：名字会重名，行号会因插行而变。
    """
    col = headers.get("case_id") or headers.get("用例编号")
    if not col:
        raise ToolError("这张表没有 case_id 列，没法精确定位到某一条用例")

    wanted = str(case_no or "").strip()
    for row in range(2, sheet.max_row + 1):
        if str(sheet.cell(row, col).value or "").strip() == wanted:
            return row

    raise ToolError(f"在表里找不到 case_id = {wanted} 的行")


def _diff_flat(before: list[Any], after: list[Any]) -> dict[str, Any]:
    """两份扁平断言的差异（给人审用）。"""

    def key_of(item: Any) -> str:
        return json.dumps(item, ensure_ascii=False, sort_keys=True)

    before_keys = [key_of(a) for a in before]
    after_keys = [key_of(a) for a in after]

    return {
        "added": [a for a, k in zip(after, after_keys) if k not in before_keys],
        "removed": [a for a, k in zip(before, before_keys) if k not in after_keys],
        "before_count": len(before_keys),
        "after_count": len(after_keys),
    }


# --------------------------------------------------------------------------
# 「断言只能变强」
# --------------------------------------------------------------------------
#
# 这条规则原本只写在 skill 和提示词里 —— 模型照着写才对，不照着写也没人拦。
# 但它是整个自愈流程的安全底线：把 ``code == "00000"`` 换成 ``code != null``
# 能让任何失败变绿，而工具会老老实实报"已落盘"。
#
# 所以由代码来判，不靠自觉。

#: 比较符的强度。``None`` 表示不认识 —— 不认识的不参与比较，宁可漏判也不误伤
_COMPARATOR_STRENGTH: dict[str, int] = {
    "is_not_none": 1,
    "exists": 2,
    "!=": 3,
    "not_equal": 3,
    "not_contains": 3,
    "contains": 3,
    ">": 4,
    ">=": 4,
    "<": 4,
    "<=": 4,
    "len>=": 4,
    "len>": 4,
    "len<": 4,
    "len<=": 4,
    "len==": 4,
    "regex": 4,
    "in": 5,
    "not_in": 4,
    "==": 5,
}


def _target_of(item: dict[str, Any]) -> tuple[str, ...]:
    """一条断言「验的是什么」。

    强度只在**同一个目标**上可比：``$.code == "00000"`` 换成
    ``$.data.id exists`` 不是削弱，是换了个地方验。
    """
    check = str(item.get("check") or "").strip().lower()
    if check == "body":
        return ("body", str(item.get("jsonpath") or ""))
    if check == "header":
        return ("header", str(item.get("key") or ""))
    if check == "db":
        # params 参与目标 —— 去掉参数会查出一堆行，和原来验的不是一回事
        return (
            "db",
            str(item.get("sql") or ""),
            json.dumps(item.get("params") or [], ensure_ascii=False, sort_keys=True),
        )
    return (check or "unknown",)


def _strength_of(item: dict[str, Any]) -> int | None:
    comparator = str(item.get("comparator") or "==").strip().lower()
    return _COMPARATOR_STRENGTH.get(comparator)


def find_weakenings(
    before: list[dict[str, Any]], after: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """找出「原来验得更严、改完验得松了」的地方。

    Returns:
        每项 ``{why, before, after}``，空列表 = 没有削弱。
    """
    after_by_target: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for item in after:
        after_by_target.setdefault(_target_of(item), []).append(item)

    weakenings: list[dict[str, Any]] = []
    for old in before:
        target = _target_of(old)
        candidates = after_by_target.get(target) or []
        if not candidates:
            weakenings.append(
                {"why": "这条断言被删掉了", "before": old, "after": None}
            )
            continue

        old_strength = _strength_of(old)
        if old_strength is None:
            continue  # 不认识的比较符，不判
        new_strength = max((_strength_of(c) or 0) for c in candidates)
        if new_strength < old_strength:
            weakenings.append(
                {
                    "why": "同一个目标上比较变松了",
                    "before": old,
                    "after": candidates[0],
                }
            )
    return weakenings


def _format_weakening(item: dict[str, Any]) -> str:
    old = json.dumps(item["before"], ensure_ascii=False)
    new = json.dumps(item["after"], ensure_ascii=False) if item["after"] else "（删掉了）"
    return f"  - {item['why']}\n      原: {old}\n      改: {new}"


def plan_assertion_change(
    case_no: str,
    assertions: list[dict[str, Any]],
    step: str = "",
    project: str = "",
) -> dict[str, Any]:
    """算出「要把断言改成什么样」，**不落盘**。

    Returns:
        ``{case_id, file, format, step, before, after, diff}``
    """
    if not isinstance(assertions, list) or not assertions:
        raise ToolError(
            "断言不能为空",
            hint="每条用例至少要断言 success。如果确实要删断言，请说明理由 —— "
            "不要用「清空」来绕过失败。",
        )

    path, case = _case_of(case_no, project)
    fmt = "excel" if path.suffix.lower() in (".xlsx", ".xlsm") else "yaml"
    target = _step_of(case, step)

    before = list(target.get("assert") or [])
    after_flat = assertions_to_flat(assertions) if fmt == "excel" else list(assertions)
    before_flat = assertions_to_flat(before) if fmt == "excel" else before

    # 两边都是扁平格式，可以直接比强度
    weakenings = find_weakenings(before_flat, after_flat)

    return {
        "case_id": str(case_no).strip(),
        "file": str(path),
        "format": fmt,
        "step": str(target.get("id") or ""),
        "before": before_flat,
        "after": after_flat,
        "diff": _diff_flat(before_flat, after_flat),
        "weakenings": weakenings,
    }


def _service_attribution_blocker(case_no: str) -> dict[str, Any] | None:
    """这条用例上一轮是不是被归因为「服务问题」。

    断言强度闸（``find_weakenings``）只能拦"改松"：把 ``code == "00000"``
    换成 ``code exists``。它拦不住"把期望值改成服务端实际返回的那个错码" ——
    两条都是 ``==``，强度相同，用例却从红变绿，服务端的 bug 还在。

    这类失败唯一正确的处理是提缺陷。所以写入入口要问一句"它为什么挂的"。
    """
    try:
        from utils.infra.failure_classifier import attribution_for
    except Exception:  # noqa: BLE001 - 查不到归因不该挡住正常改写
        return None
    return attribution_for(case_no)


def _guard_heal_rounds(case_no: str) -> None:
    """改了两轮还没跑通就停下。

    一直改下去最坏的结果不是"没修好"，是**改到它绿了**：每轮削一点断言，
    总会碰到一条能通过的版本。那时候用例已经测不出任何东西了。
    """
    from ai.agent.tools import heal_state

    rounds = heal_state.failed_rounds(case_no)
    if rounds < heal_state.MAX_FAILED_ROUNDS:
        return

    raise ToolError(
        f"用例 {case_no} 已经改了 {rounds} 轮还没跑通，停下来人工看",
        hint=(
            "连续多轮改不通，说明前面的归因大概率就是错的 —— 继续改下去"
            "只会把断言削到刚好能通过，掩盖真正的问题。\n"
            "建议：把每轮改了什么、跑出来什么整理成一段结论交给用户，"
            "说清楚卡在哪。确实需要再试，先让人确认。"
        ),
    )


def _format_service_block(record: dict[str, Any]) -> str:
    """一句话说清楚它为什么被归为服务问题（给人看，不重复信息）。"""
    conclusion = str(record.get("结论") or record.get("具体原因") or "服务问题")
    parts = [conclusion]
    # 结论里通常已经带了 code/msg（见 FailureDiagnosis.verdict），别重复贴
    for key, label in (("服务端code", "code"), ("服务端msg", "msg")):
        value = str(record.get(key) or "").strip()
        if value and value not in conclusion:
            parts.append(f"{label}={value}")
    if record.get("接口"):
        parts.append(str(record["接口"]))
    return "｜".join(parts)


def apply_assertion_change(
    case_no: str,
    assertions: list[dict[str, Any]],
    step: str = "",
    project: str = "",
    note: str = "",
    allow_weakening: bool = False,
) -> dict[str, Any]:
    """把断言写回用例文件（**会改文件**）。

    Args:
        note: 改这条用例的理由。写进备份目录的 manifest，方便事后追溯。
        allow_weakening: 确实要放宽断言时显式打开。默认**拒绝** ——
            宁可让人多写一个参数，也不要一次误操作把断言悄悄改松。

    Raises:
        ToolError: 断言会变弱、或这条用例上一轮被归因为服务问题。
    """
    # 服务问题 → 提缺陷，不改用例。这道闸**没有开关**：
    # 改期望值让它变绿和放宽断言一样，都是在掩盖真实缺陷。
    blocked = _service_attribution_blocker(str(case_no).strip())
    if blocked is not None:
        raise ToolError(
            f"用例 {case_no} 上一轮被归因为「服务问题」，拒绝改断言",
            hint=(
                f"{_format_service_block(blocked)}\n"
                "服务端行为异常时改用例断言，等于把缺陷藏起来。正确做法：\n"
                "  1. 把请求/响应证据整理出来提缺陷给后端\n"
                "  2. 确属已知缺陷且短期修不了，用 set_case_status 标 broken（用例保留原样）\n"
                "如果服务端已经修好、或这条用例的失败其实另有原因，"
                "先 run_cases 重跑一遍 —— 归因记录每轮运行都会刷新，"
                "重跑后不再是服务问题就能改。"
            ),
        )

    _guard_heal_rounds(str(case_no).strip())

    plan = plan_assertion_change(case_no, assertions, step=step, project=project)
    path = Path(plan["file"])
    case_no = plan["case_id"]

    weakenings = plan.get("weakenings") or []
    if weakenings and not allow_weakening:
        detail = "\n".join(_format_weakening(w) for w in weakenings)
        raise ToolError(
            f"这次改动会削弱断言（{len(weakenings)} 处），已拒绝，文件没动",
            hint=(
                "断言变弱会让真实缺陷蒙混过关，所以默认拦住。\n"
                f"{detail}\n"
                "如果确实是接口契约变了、该放宽，在调用的参数里带 allow_weakening=true "
                "并在 note 里写清楚依据。"
            ),
        )

    manifest = {
        "tool": "apply_assertion_change",
        "case_id": case_no,
        "file": str(path),
        "step": plan["step"],
        "note": str(note or ""),
        "changed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "diff": plan["diff"],
    }
    if weakenings:
        # 显式放宽过的改动要留痕，不能和普通改动混在一起看
        manifest["weakenings"] = weakenings
        manifest["allow_weakening"] = True

    if plan["format"] == "yaml":
        backup = _write_yaml_assertions(path, plan, assertions, manifest)
    else:
        backup = _write_excel_assertions(path, plan, assertions, manifest)

    return {
        "case_id": case_no,
        "file": str(path),
        "format": plan["format"],
        "step": plan["step"],
        "diff": plan["diff"],
        "weakenings": weakenings,
        "written": True,
        "note": manifest["note"],
        "backup_dir": str(backup.parent),
        "next_step": (
            "断言改了，但**还没跑过**。用 verify_heal 跑这条用例确认它真的通过"
            "（没通过会自动回滚），再决定是否 set_case_status 改成 debugged。"
        ),
    }


def _write_excel_assertions(
    path: Path,
    plan: dict[str, Any],
    assertions: list[dict[str, Any]],
    manifest: dict[str, Any],
) -> Path:
    workbook, sheet = _open_sheet(path)
    headers = _header_index(sheet)
    row = _locate_row(sheet, headers, plan["case_id"])

    col = headers.get("断言")
    if not col:
        raise ToolError(f"{path.name} 缺少「断言」列")

    # 只动「断言」这一格 —— 别的列（响应数据参考、前置、后置…）原样不动
    sheet.cell(row, col).value = json.dumps(
        assertions_to_flat(assertions), ensure_ascii=False
    )

    return _write_with_guard(path, plan["case_id"], manifest, lambda: workbook.save(path))


def _write_yaml_assertions(
    path: Path,
    plan: dict[str, Any],
    assertions: list[dict[str, Any]],
    manifest: dict[str, Any],
) -> Path:
    """改 YAML 用例的断言。

    **不用 PyYAML 整体重写** —— 这些文件里注释占一多半，序列化一遍全丢。
    走文本替换：只把目标步骤下的 ``assert:`` 整段换掉，其余原样保留。
    """
    import yaml

    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    start, end, key_indent = _find_assert_block(lines, plan["step"])

    dumped = yaml.safe_dump(
        assertions,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=200,
    )
    body = "".join(
        f"{' ' * (key_indent + 2)}{line}\n" for line in dumped.rstrip("\n").splitlines()
    )
    new_block = f"{' ' * key_indent}assert:\n{body}"

    new_text = "".join(lines[:start]) + new_block + "".join(lines[end:])

    return _write_with_guard(
        path,
        plan["case_id"],
        manifest,
        lambda: path.write_text(new_text, encoding="utf-8"),
    )


def _find_assert_block(lines: list[str], step_id: str) -> tuple[int, int, int]:
    """定位某个步骤下 ``assert:`` 块的起止行号。

    Returns:
        ``(起始行, 结束行, assert 的缩进)``；``lines[起始:结束]`` 就是整段
        ``assert:`` 块（含 ``assert:`` 那一行）。
    """
    if not step_id:
        raise ToolError("YAML 用例必须指定步骤 id")

    step_pattern = re.compile(
        r"^(\s*)-\s+id:\s*['\"]?" + re.escape(step_id) + r"['\"]?\s*$"
    )
    step_idx = None
    key_indent = 0
    for idx, line in enumerate(lines):
        match = step_pattern.match(line.rstrip("\r\n"))
        if match:
            step_idx = idx
            key_indent = len(match.group(1)) + 2  # ``- `` 之后的键缩进
            break

    if step_idx is None:
        raise ToolError(f"在文件里找不到步骤 {step_id!r}")

    # 步骤块结束：缩进回到比键更浅的非空行
    step_end = len(lines)
    for idx in range(step_idx + 1, len(lines)):
        if not lines[idx].strip():
            continue
        indent = len(lines[idx]) - len(lines[idx].lstrip())
        if indent < key_indent:
            step_end = idx
            break

    assert_idx = None
    for idx in range(step_idx + 1, step_end):
        if re.match(r"^\s*assert\s*:", lines[idx]):
            if len(lines[idx]) - len(lines[idx].lstrip()) == key_indent:
                assert_idx = idx
                break

    if assert_idx is None:
        raise ToolError(
            f"步骤 {step_id!r} 里没有 assert: 块，没法就地改写",
            hint="请手工加上 assert: 再让智能体改，或者用 create_case 新建一条用例。",
        )

    # assert 块结束：第一个缩进 <= key_indent 的非空行
    block_end = step_end
    for idx in range(assert_idx + 1, step_end):
        if not lines[idx].strip():
            continue
        if len(lines[idx]) - len(lines[idx].lstrip()) <= key_indent:
            block_end = idx
            break

    return assert_idx, block_end, key_indent


# ==========================================================================
# 改状态
# ==========================================================================


#: 认得的写法（``case_status.normalize`` 会把这些映射到四个状态之一）。
#: 和那边的别名表保持一致 —— 那边改了这里也要改，否则写入口会拒掉合法写法。
_KNOWN_STATUS_SPELLINGS = frozenset({
    "draft", "debugged", "disabled", "broken",
    "todo", "new", "wip", "passed", "pass", "ok", "active",
    "enable", "enabled", "off", "disable", "skip", "skipped",
    "fail", "failed", "known_issue",
    "未调试", "待调试", "草稿", "调试通过", "已调试", "通过",
    "已禁用", "禁用", "下线", "调试不通过", "未通过", "有问题",
})


def _is_known_status_spelling(status: Any) -> bool:
    """这个写法认识吗。

    ``case_status.normalize`` 对不认识的值一律返回 ``draft`` —— 读的时候
    这是好兜底（不跑总比跑挂了好），但写的时候会让拼错的状态静默生效。
    """
    raw = str(status if status is not None else "").strip()
    if not raw:
        return False
    if raw.lower() in _KNOWN_STATUS_SPELLINGS:
        return True
    # 数字状态（和数据库字段一致：0=draft 1=debugged 2=disabled 3=broken）
    return raw.isdigit() and int(raw) in (0, 1, 2, 3)


def apply_status_change(
    case_no: str,
    status: str,
    project: str = "",
    note: str = "",
) -> dict[str, Any]:
    """改用例状态（Excel 改「用例状态」列，YAML 改顶层 ``status:``）。"""
    from utils.cases.case_status import ALL_STATUSES, normalize

    # ⚠️ 不能只看 ``normalize`` 的结果：它对认不出来的值**静默退回 draft**。
    #    读的时候要这个兜底，写的时候绝对不行 —— ``status="随便写的"``
    #    会被悄悄写成 draft，用例就此无声无息地不进 CI。
    #    所以先确认「这个写法本身是认识的」，再看它归一化成什么。
    if not _is_known_status_spelling(status):
        raise ToolError(
            f"不认识的状态 {status!r}",
            hint=f"只能是：{'、'.join(ALL_STATUSES)}"
            "（也认中文：未调试 / 调试通过 / 已禁用 / 调试不通过）",
        )

    wanted = normalize(status)

    path, case = _case_of(case_no, project)
    case_no = str(case_no).strip()
    before = str(case.get("status") or "")
    fmt = "excel" if path.suffix.lower() in (".xlsx", ".xlsm") else "yaml"

    manifest = {
        "tool": "set_case_status",
        "case_id": case_no,
        "file": str(path),
        "note": str(note or ""),
        "changed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "before": before,
        "after": wanted,
    }

    if fmt == "excel":
        workbook, sheet = _open_sheet(path)
        headers = _header_index(sheet)
        row = _locate_row(sheet, headers, case_no)
        col = headers.get("用例状态") or headers.get("状态")
        if not col:
            raise ToolError(f"{path.name} 缺少「用例状态」列")

        sheet.cell(row, col).value = wanted
        backup = _write_with_guard(
            path, case_no, manifest, lambda: workbook.save(path)
        )
    else:
        text = path.read_text(encoding="utf-8")
        new_text = _replace_yaml_scalar(text, "status", wanted)
        backup = _write_with_guard(
            path,
            case_no,
            manifest,
            lambda: path.write_text(new_text, encoding="utf-8"),
        )

    return {
        "case_id": case_no,
        "file": str(path),
        "format": fmt,
        "before": before,
        "after": wanted,
        "written": True,
        "backup_dir": str(backup.parent),
    }


def _replace_yaml_scalar(text: str, key: str, value: str) -> str:
    """替换 ``key: value``（取第一个，任意缩进），保留原缩进。

    只动这一行 —— 文件里的注释和别的字段原样保留。用例的根键可能是
    ``testcase:`` 包一层（此时 ``status`` 缩进 2 格），也可能直接平铺，
    所以不能假定顶格。
    """
    pattern = re.compile(rf"^([ \t]*){re.escape(key)}\s*:.*$", re.MULTILINE)
    match = pattern.search(text)
    if match:
        indent = match.group(1)
        return text[: match.start()] + f"{indent}{key}: {value}" + text[match.end():]
    return f"{key}: {value}\n" + text


# ==========================================================================
# 新建用例
# ==========================================================================


def next_case_no() -> str:
    """分配下一个 ``API-<n>`` 编号（现有最大号 + 1）。

    位数跟着现有编号走（现有的是 ``API-018`` 三位补零，
    新编号也补成三位），不然同一张表里 ``API-9`` 和 ``API-018``
    混着排，按字符串排序会乱。
    """
    numbers: list[int] = []
    widths: list[int] = []
    prefix = "API"

    for _path, case in _load_all():
        raw = str(case.get("case_id") or "").strip()
        head, sep, tail = raw.rpartition("-")
        if not sep or not tail.isdigit():
            continue
        prefix = head or prefix
        numbers.append(int(tail))
        widths.append(len(tail))

    next_no = max(numbers, default=0) + 1
    width = max(widths, default=3)
    return f"{prefix}-{next_no:0{width}d}"


def _check_module(module: str) -> str:
    """模块名要能当目录名 —— 挡住 ``..`` 和路径分隔符。"""
    name = str(module or "").strip()
    if not name:
        raise ToolError("必须给模块名", hint="比如 admin_area；用 list_modules 可以看到现有的")
    if any(ch in name for ch in ("/", "\\", ":", "*", "?", '"', "<", ">", "|")) or name in (".", ".."):
        raise ToolError(f"模块名 {name!r} 不能当目录名")
    return name


def create_case(
    project: str,
    module: str,
    path: str,
    method: str,
    title: str,
    assertions: list[dict[str, Any]],
    api_name: str = "",
    request_data: Any = None,
    request_headers: Any = None,
    query_params: Any = None,
    priority: str = "P1",
    marks: str = "",
    status: str = "draft",
    setups: Any = None,
    teardowns: Any = None,
) -> dict[str, Any]:
    """往模块的 Excel 里追加一条新用例。

    默认状态是 ``draft``（CI 不跑）—— 新写的用例没跑过就进 CI，
    挂一次就红了主干。跑通之后再用 ``set_case_status`` 改成 ``debugged``。
    """
    from utils.cases.case_source import current_excel_dir

    project = str(project or "").strip()
    if not project:
        raise ToolError("必须给项目名", hint="比如 mall4j")

    module = _check_module(module)
    path = str(path or "").strip()
    if not path.startswith("/"):
        raise ToolError(f"接口路径要以 / 开头，拿到 {path!r}")

    method = str(method or "").strip().upper()
    if method not in ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"):
        raise ToolError(f"不认识的请求方法 {method!r}")

    if not isinstance(assertions, list) or not assertions:
        raise ToolError(
            "断言不能为空",
            hint="至少要有 success 断言，例如 "
            '[{"type": "equal", "field": "success", "expected": true}]',
        )

    flat_asserts = assertions_to_flat(assertions)
    title = str(title or "").strip() or f"{api_name or path} 用例"
    case_no = next_case_no()

    target_dir = current_excel_dir() / project / module
    target_dir.mkdir(parents=True, exist_ok=True)
    file_path = target_dir / f"{module}.xlsx"

    import openpyxl

    if file_path.exists():
        workbook = openpyxl.load_workbook(file_path)
    else:
        workbook = openpyxl.Workbook()
        workbook.active.title = SHEET_FLAT
        sheet = workbook[SHEET_FLAT]
        for idx, name in enumerate(FLAT_HEADERS, start=1):
            sheet.cell(1, idx).value = name

    if SHEET_FLAT not in workbook.sheetnames:
        raise ToolError(
            f"{file_path.name} 不是扁平格式，不能往里追加用例",
            hint="换一个模块目录，或者先把这个文件迁移成扁平格式",
        )

    sheet = workbook[SHEET_FLAT]
    headers = _header_index(sheet)
    row = sheet.max_row + 1

    # 没写请求头就用项目档案里的鉴权头 —— 否则新用例一跑就是 401
    if request_headers in (None, "", {}, []):
        request_headers = _default_headers(project)

    values = {
        "case_id": case_no,
        "模块": module,
        "标签": str(marks or module),
        "用例名称": f"{api_name or module} - {title}",
        "优先级": str(priority or "P1").upper(),
        "接口名称": str(api_name or ""),
        "用例标题": title,
        "接口": path,
        "请求方法": method,
        "请求头": _dump_cell(request_headers),
        "请求数据": _dump_cell(request_data),
        "断言": json.dumps(flat_asserts, ensure_ascii=False),
        "参数化数据": "",
        "场景": "",
        "前置": _dump_cell(setups),
        "后置": _dump_cell(teardowns),
        "用例状态": str(status or "draft").lower(),
        "查询参数": _dump_cell(query_params),
    }

    for name, value in values.items():
        col = headers.get(name)
        if col:
            sheet.cell(row, col).value = value

    manifest = {
        "tool": "create_case",
        "case_id": case_no,
        "file": str(file_path),
        "changed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "row": row,
        "title": title,
    }

    backup = _write_with_guard(
        file_path, case_no, manifest, lambda: workbook.save(file_path)
    )

    return {
        "case_id": case_no,
        "file": str(file_path),
        "row": row,
        "status": str(status or "draft").lower(),
        "assertions": flat_asserts,
        "backup_dir": str(backup.parent),
        "next_step": (
            f"用例是 draft 状态，CI 不会跑。先 run_cases case_file={_rel(file_path)} "
            "确认通过，再 set_case_status 改成 debugged。"
        ),
    }


def _default_headers(project: str) -> dict[str, str]:
    """新用例默认带的鉴权头：``{"Authorization": "{{mall4j.token}}"}``。

    头名从项目档案里取（不同系统的鉴权头叫法不一样，见
    ``utils/config/project_profile.py``）。取不到就退回 ``Authorization`` ——
    和现有 Excel 用例的写法一致。
    """
    header_name = "Authorization"
    try:
        from utils.config.project_profile import ProjectRegistry

        profile = ProjectRegistry.get(project)
        if profile and profile.token_header:
            header_name = profile.token_header
    except Exception:  # noqa: BLE001 - 拿不到档案就用默认头名
        pass

    return {header_name: f"{{{{{project}.token}}}}"}


def _dump_cell(value: Any) -> str:
    """把字典/列表塞进单元格；空值给空串。"""
    if value in (None, "", {}, []):
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _rel(path: Path) -> str:
    from utils import paths

    try:
        return path.resolve().relative_to(paths.PROJECT_ROOT.resolve()).as_posix()
    except (ValueError, OSError):
        return path.as_posix()


__all__ = [
    "SHEET_FLAT",
    "FLAT_HEADERS",
    "CaseLocation",
    "assertion_to_flat",
    "assertions_to_flat",
    "field_to_jsonpath",
    "locate",
    "read_assertions",
    "plan_assertion_change",
    "apply_assertion_change",
    "apply_status_change",
    "create_case",
    "next_case_no",
    "find_weakenings",
    "restore_from_backup",
]
