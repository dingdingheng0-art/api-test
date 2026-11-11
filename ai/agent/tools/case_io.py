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

import copy
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
    # 多步场景链：JSON 数组。不填就是「一行一步」的老格式。
    "步骤",
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

#: 中文汉字 —— 业务名必须是中文（现役用例的约定）
_CJK_RE = re.compile(r"[一-鿿]")

#: 「方法 + 路径」形态的名字，比如 ``PUT /admin/area``。
#:
#: 模型很爱把接口文档里的「接口名」直接抄成这种形式，报告里就显示成
#: ``[ai admin area] PUT /admin/area - PUT /admin/area（正常）``——
#: 既不是业务动作，也和现役用例对不上。
_METHOD_PATH_RE = re.compile(
    r"^\s*(?:GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\s+(/\S*)\s*$", re.IGNORECASE
)

#: 路径模板占位符：``{id}`` / ``{{id}}`` / ``<id>`` / ``:id``
_PATH_PLACEHOLDER_RE = re.compile(r"\{\{[^}]+\}\}|\{[^}]+\}|<[^>]+>|(?<=/):\w+")

#: 路径里的具体数字段（``/admin/area/110000000000`` → ``/admin/area/{}``）
_NUMERIC_SEGMENT_RE = re.compile(r"/\d+(?=/|$)")

#: 名字末尾的场景括号：``（正常请求）``
_SCENARIO_TAIL_RE = re.compile(r"[（(]([^）)]*)[）)]\s*$")


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
    from testcases.case_loader import discover_cases

    from ai.agent.tools._common import load_file_cases

    out: list[tuple[Path, dict[str, Any]]] = []
    for proj, path in discover_cases():
        if project and proj != project:
            continue
        try:
            loaded = load_file_cases(path, proj)
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


def _case_quality(
    case: dict[str, Any],
    module: str,
    *,
    step_id: str = "",
    asserts: Any = None,
    gates: dict[str, Any] | None = None,
):
    """对一条用例跑质量闸（可替换某一步的断言后再检查）。

    写入口（``create_case`` / ``apply_assertion_change`` /
    ``apply_status_change``）共用同一道闸 —— 否则「生成时拦住、改一改又漏进去」，
    闸门就只是个摆设。

    Args:
        step_id: 只替换这个步骤的断言；空表示不替换
        asserts: 替换后的断言（``None`` 表示用文件里的原值）
        gates: 临时叠加的阈值（比如转 debugged 时要求值断言）
    """
    from ai.rules.validator import check_case, enforce

    steps: list[dict[str, Any]] = []
    for raw in case.get("steps") or []:
        if isinstance(raw, dict):
            steps.append(dict(raw))

    # 只替换目标步骤。以前写成 `not step_id or id == step_id`，多步用例
    # 没传 step_id 时会把**每一步**都换成同一组断言 —— 闸门看到的用例
    # 和真正要落的盘不是一回事。
    if asserts is not None:
        replaced = list(asserts) if isinstance(asserts, (list, tuple)) else asserts
        if step_id:
            for step in steps:
                if str(step.get("id") or "") == step_id:
                    step["assert"] = replaced
                    break
        elif len(steps) == 1:
            steps[0]["assert"] = replaced

    return enforce(
        module,
        check_case(
            case_name=str(case.get("name") or ""),
            api_name=str(case.get("api_name") or ""),
            steps=steps,
            setups=case.get("setups"),
            teardowns=case.get("teardowns"),
            marks=case.get("marks"),
            gates=gates,
        ),
    )


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


#: 磁盘上最多留多少份备份目录。
#:
#: 每次写用例都会新建一个 ``agent_<时间戳>`` 目录（写 17 条就是 17 个），
#: 没有回收的话一天就能攒出几百个 —— 备份是给人回滚用的，留最近 50 次
#: 足够查「刚才那次改了什么」，再往前的旧快照只会撑大缓存目录。
BACKUP_KEEP = 50


def _prune_backups(root: Path, keep: int = BACKUP_KEEP) -> None:
    """只留最近 ``keep`` 个备份目录（按目录名里的时间戳排序）。"""
    try:
        dirs = sorted(
            (p for p in root.iterdir() if p.is_dir() and p.name.startswith("agent_")),
            key=lambda p: p.name,
        )
    except OSError as exc:  # 目录不存在 / 没权限都不该影响写入
        log.warning("备份目录清理跳过: %s", exc)
        return
    for old in dirs[:-keep]:
        shutil.rmtree(old, ignore_errors=True)


def _backup(path: Path, manifest: dict[str, Any]) -> Path:
    """把原文件备份到 ``.cache/backup/agent_<时间戳>/``。

    manifest 一起写进去 —— 事后想回滚、或者查「谁改的、为什么改」全靠它。
    """
    from utils import paths

    root = paths.CACHE_DIR / "backup"
    target_dir = root / f"agent_{_stamp()}"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / path.name
    shutil.copy2(path, target)

    (target_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _prune_backups(root)
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

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ToolError(
            f"备份目录的 manifest.json 读不了：{exc}",
            hint="这个目录不是 apply_* 工具生成的，请手工恢复。",
        ) from exc
    if not isinstance(manifest, dict):
        raise ToolError(
            f"备份目录的 manifest.json 结构不对（根节点是 {type(manifest).__name__}）",
            hint="这个目录不是 apply_* 工具生成的，请手工恢复。",
        )
    target = Path(str(manifest.get("file") or ""))
    if not target.is_absolute():
        raise ToolError(f"manifest 里的文件路径不合法：{target}")

    shutil.copy2(source, target)

    from ai.agent.tools._common import clear_cache

    clear_cache()
    log.warning("已按备份恢复 %s（来自 %s）", target, directory)
    return str(target)


def _verify(path: Path, case_no: str, expect_status: str = "") -> list[str]:
    """重新解析 + 校验目标用例，返回错误清单（空 = 通过）。

    Args:
        expect_status: 非空时额外核对「文件里读出来的状态确实是它」。
            YAML 的 ``status`` 是文本替换出来的 —— 替换到步骤请求体里的
            ``status: 1``、或者插到 ``testcase:`` 外面，文件都还能解析、
            用例也还存在，``CaseValidator`` 更不会报错，但状态根本没变。
            不核对这一步就会「报成功、实际没转正」。
    """
    from ai.agent.tools._common import load_file_cases
    from utils.cases.case_status import resolve_status
    from utils.cases.case_validator import CaseValidator

    try:
        loaded = load_file_cases(path)
    except Exception as exc:  # noqa: BLE001
        return [f"文件解析失败：{exc}"]

    target = None
    case = None
    for data in loaded:
        candidate = data.get("testcase") if isinstance(data.get("testcase"), dict) else data
        if case_key(candidate) == str(case_no).strip():
            target = data
            case = candidate
            break

    if target is None:
        return [f"写回后在文件里找不到用例 {case_no}"]

    errors = CaseValidator().validate(target)
    if expect_status and isinstance(case, dict):
        actual = resolve_status(case)
        if actual != expect_status:
            errors.append(
                f"状态没写进去：期望 {expect_status}，文件里读出来是 {actual}"
            )
    return errors


def _write_with_guard(
    path: Path,
    case_no: str,
    manifest: dict[str, Any],
    write: Any,
    expect_status: str = "",
) -> Path | None:
    """统一的写入流程：备份 → 写 → 复验 → 失败回滚。

    Returns:
        备份文件路径；**新建文件**时没有可备份的原文，返回 ``None``。
    """
    # 文件不存在（新建模块/新文件）时不能备份 —— 以前直接 copy2 一个
    # 还没写出来的文件，create_case 必抛 FileNotFoundError。
    backup = _backup(path, manifest) if path.exists() else None
    try:
        write()
        # 复验前先清缓存 —— 文件刚写完，缓存里还是写之前的旧内容
        _clear_case_caches()
        errors = _verify(path, case_no, expect_status=expect_status)
    except Exception as exc:  # noqa: BLE001 - 写挂了也要还原
        _rollback(backup, path)
        _clear_case_caches()
        raise ToolError(
            f"写入失败，已回滚：{exc}",
            hint=_rollback_hint(backup),
        ) from exc

    if errors:
        _rollback(backup, path)
        _clear_case_caches()
        raise ToolError(
            "写回后的用例没通过校验，已回滚：" + "；".join(errors[:5]),
            hint=_rollback_hint(backup),
        )

    # 写完立刻清缓存 —— 否则 list_cases / get_case 还会返回改之前那份
    from ai.agent.tools._common import clear_cache

    clear_cache()
    return backup


def _clear_case_caches() -> None:
    """清掉用例读取缓存（写文件前后都要清，见 ``_common.clear_cache``）。"""
    from ai.agent.tools._common import clear_cache

    clear_cache()


def _rollback(backup: Path | None, path: Path) -> None:
    """写入失败时的还原动作。

    有备份 → 恢复原文件；本来就没有原文件（新建）→ 删掉刚写的半成品，
    别留下一个校验不通过的用例文件。
    """
    if backup is not None:
        _restore(backup, path)
        return
    try:
        path.unlink()
        log.warning("已删除写入失败的新文件 %s", path)
    except FileNotFoundError:
        pass


def _rollback_hint(backup: Path | None) -> str:
    if backup is not None:
        return f"原文件已恢复。备份目录：{backup.parent}"
    return "原文件本来就不存在，已删除刚新建的文件。"


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

    # 改完还要过质量闸：断言变强了，但这组断言可能仍然证明不了任何东西
    # （比如正例只剩 ``success == true``）。预览阶段就把话说在前面。
    #
    # ⚠️ 传**框架内部格式**（``type/field/expected``），不是 ``after_flat``：
    # ``check_case`` 按 ``field`` / ``type`` 判定正例和业务断言，扁平格式
    # （``check/jsonpath``）在它眼里字段名是空的 —— 一条只有 success 的用例
    # 也会被当成「没有正例」放过去，闸门形同虚设。
    quality = _case_quality(
        case,
        path.parent.name,
        step_id=str(target.get("id") or ""),
        asserts=list(assertions),
    )

    return {
        "case_id": str(case_no).strip(),
        "file": str(path),
        "format": fmt,
        "step": str(target.get("id") or ""),
        "before": before_flat,
        "after": after_flat,
        "diff": _diff_flat(before_flat, after_flat),
        "weakenings": weakenings,
        "quality_findings": [item.to_dict() for item in quality.violations],
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

    # 断言强度只回答「有没有变松」，不回答「这组断言能不能证明什么」。
    # 一条只有 success 的用例改完还是薄用例，必须一起拦。
    from ai.rules.validator import GateResult, Violation, format_hint, is_sandbox

    gate = GateResult(
        violations=[
            Violation(str(item.get("rule") or ""), str(item.get("message") or ""))
            for item in plan.get("quality_findings") or []
        ]
    )
    quality_warnings: list[str] = []
    if gate.violations:
        if is_sandbox(path.parent.name):
            # 沙箱是草稿区：允许改，但把问题带回去，别让调用方以为已经合格
            quality_warnings = gate.messages()
        else:
            raise ToolError(
                f"改完的断言过不了质量闸，文件没动（{len(gate.violations)} 处）",
                hint=format_hint(gate),
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
        "quality_warnings": quality_warnings,
        "written": True,
        "note": manifest["note"],
        "backup_dir": str(backup.parent) if backup is not None else "",
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

    flat = assertions_to_flat(assertions)

    # 多步用例：真身是「步骤」列，行级「断言」只是第一步的展示副本。
    # 不改「步骤」列的话，改完的断言在运行时根本不会被用上。
    steps_col = headers.get("步骤")
    if steps_col:
        raw = sheet.cell(row, steps_col).value
        parsed = raw if isinstance(raw, list) else None
        if parsed is None and isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = None
        if isinstance(parsed, list) and parsed:
            target = str(plan.get("step") or "")
            if not target:
                if len(parsed) > 1:
                    raise ToolError(
                        f"这条用例有 {len(parsed)} 个步骤，必须指定改哪一个",
                        hint=f"可选步骤: {[s.get('id') for s in parsed if isinstance(s, dict)]}",
                    )
                target = str(parsed[0].get("id") or "step1")

            hit = False
            for index, item in enumerate(parsed, start=1):
                if not isinstance(item, dict):
                    continue
                if str(item.get("id") or f"step{index}") == target:
                    item["assert"] = flat
                    hit = True
                    break
            if not hit:
                raise ToolError(f"「步骤」列里找不到步骤 {target!r}")

            sheet.cell(row, steps_col).value = json.dumps(parsed, ensure_ascii=False)
            # 行级「断言」= 第一步的展示副本，改的是第一步就同步
            if str(parsed[0].get("id") or "step1") == target:
                sheet.cell(row, col).value = json.dumps(flat, ensure_ascii=False)
            return _write_with_guard(
                path, plan["case_id"], manifest, lambda: workbook.save(path)
            )

    # 只动「断言」这一格 —— 别的列（响应数据参考、前置、后置…）原样不动
    sheet.cell(row, col).value = json.dumps(flat, ensure_ascii=False)

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


def _require_verified(
    path: Path,
    case: dict[str, Any],
    note: str = "",
    force: bool = False,
) -> None:
    """转 ``debugged`` 之前先核对「跑通过」凭据。

    ``debugged`` 的意思就是「跑通过、可以进 CI」。不核对的话，智能体
    完全可以一次都没跑就把 draft 标绿 —— CI 里那条用例从没执行过，
    真挂了也没人知道它是新加的。

    凭据由 ``run_cases`` 在整体通过后写进 ``.cache/verify_ledger.json``
    （见 :mod:`ai.agent.tools.verify_ledger`）。

    Args:
        force: 确实无法执行（环境故障）时的逃生口，**必须**写 ``note`` 说明。
    """
    from ai.agent.tools.verify_ledger import verified_state

    trusted, reason = verified_state(path, case)
    if trusted:
        return

    if force:
        if not str(note or "").strip():
            raise ToolError(
                f"这条用例没有跑通过的记录（{reason}），force 又没有写理由",
                hint="force=true 时必须写 note，说明为什么无法执行验证。",
            )
        return

    raise ToolError(
        f"这条用例没有可信的「跑通过」记录（{reason}），不能标成 debugged",
        hint=(
            "先 run_cases(case_id=..., run_draft=true) 跑通 —— "
            "整体通过后会自动记下凭据；改过用例内容要重新跑。"
            "确实跑不了（环境故障）时才用 force=true，并在 note 里写明原因。"
        ),
    )


def apply_status_change(
    case_no: str,
    status: str,
    project: str = "",
    note: str = "",
    force: bool = False,
) -> dict[str, Any]:
    """改用例状态（Excel 改「用例状态」列，YAML 改顶层 ``status:``）。

    Args:
        force: 转 ``debugged`` 时跳过「跑通过」凭据核对。只在确实无法执行
            （环境故障）时用，且必须写 ``note``。
    """
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

    # 转正必须拿得出「跑通过」的凭据 —— 见 _require_verified。
    # 已经是 debugged 再设一次是空操作，不拦（否则改个 note 也要重跑）。
    if wanted == "debugged" and normalize(before) != "debugged":
        _require_verified(path, case, note=note, force=force)

        # 「跑通过」只证明它执行成功，不证明它有检验能力。
        # 薄用例 / 写用例无清理 / 反例没有拒绝断言，跑绿了也不该进 CI。
        # 注意：force 只跳过凭据核对，**不能**跳过质量闸。
        #
        # 转正比落盘更严一档：必须有**值断言**。落盘时只有 metadata 级证据，
        # 逼人写 expected 会变成编期望值；但转正时已经跑通过，运行结果里
        # 就有真实响应，从 get_last_result 取一个字段补上即可。
        from ai.rules.validator import format_hint, load_gates

        gates = {**load_gates(), "min_value_asserts": 1}
        gate = _case_quality(case, path.parent.name, gates=gates)
        if gate.violations:
            raise ToolError(
                f"用例 {case_no} 质量不达标，不能转 debugged（{len(gate.violations)} 处）",
                hint=format_hint(gate) + "\n\n（force 只能跳过「跑通过」凭据，跳不过质量闸）",
            )

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
            path,
            case_no,
            manifest,
            lambda: workbook.save(path),
            expect_status=wanted,
        )
    else:
        text = path.read_text(encoding="utf-8")
        new_text = _replace_yaml_scalar(text, "status", wanted)
        backup = _write_with_guard(
            path,
            case_no,
            manifest,
            lambda: path.write_text(new_text, encoding="utf-8"),
            expect_status=wanted,
        )

    return {
        "case_id": case_no,
        "file": str(path),
        "format": fmt,
        "before": before,
        "after": wanted,
        "written": True,
        "backup_dir": str(backup.parent) if backup is not None else "",
    }


def _replace_yaml_scalar(text: str, key: str, value: str) -> str:
    """替换用例本体上的 ``key: value``，保留原缩进。

    只动这一行 —— 文件里的注释和别的字段原样保留。

    **必须认「最外层」那个 key**。用例本体可能是扁平的（``status`` 顶格），
    也可能包一层 ``testcase:``（缩进 2 格）；而步骤的请求体里同样可能有
    ``status: 1``（比如调「上架/下架」接口）—— 缩进更深。以前取第一个匹配，
    只要请求体排在前面，就会把**请求体里的 status 改成 debugged**：
    文件照样能解析、用例照样存在、``CaseValidator`` 也不报错，返回却是
    「写成功」，而用例本身还是 draft（反而把接口参数改坏了）。
    所以按缩进取最浅的一个；同缩进取最靠前的。

    **没有这个 key 时插对位置**：有 ``testcase:`` 包装就插到它下面，
    否则插在文件开头。直接顶格插在 ``testcase:`` 前面等于写在用例外面 ——
    ``resolve_status`` 读的是用例本体，永远读不到。
    """
    pattern = re.compile(rf"^([ \t]*){re.escape(key)}\s*:.*$", re.MULTILINE)
    matches = list(pattern.finditer(text))

    # 先解析出来「用例本体」到底是哪一层 —— 只看缩进不够聪明：
    # 如果 status 只出现在请求体里（用例本身没写 status），
    # 按「最浅的那个」也会改错。解析失败就退回按缩进猜。
    wrapped = False
    has_key = None
    try:
        import yaml

        data = yaml.safe_load(text)
        if isinstance(data, dict):
            inner = data.get("testcase")
            wrapped = isinstance(inner, dict)
            body = inner if wrapped else data
            has_key = key in body
    except Exception:  # noqa: BLE001 - 解析不了就按文本猜，反正后面还有复验
        has_key = None

    child_indent = _testcase_child_indent(text) if wrapped else ""

    if has_key and matches:
        # 认「用例本体那一层」的缩进：扁平是顶格，包装的是 testcase 的子键缩进
        exact = [m for m in matches if m.group(1) == child_indent]
        match = exact[0] if exact else min(
            matches, key=lambda m: (len(m.group(1).expandtabs(4)), m.start())
        )
        indent = match.group(1)
        return text[: match.start()] + f"{indent}{key}: {value}" + text[match.end():]

    if has_key is None and matches:
        # 解析失败：保守地取最浅的那个
        match = min(matches, key=lambda m: (len(m.group(1).expandtabs(4)), m.start()))
        indent = match.group(1)
        return text[: match.start()] + f"{indent}{key}: {value}" + text[match.end():]

    # 用例本体里没有这个键 —— 插到用例本体那一层（不是插到文件开头）
    if wrapped:
        root = re.search(r"^testcase[ \t]*:[ \t]*(?:#.*)?$", text, re.MULTILINE)
        if root:
            insert_at = text.find("\n", root.end())
            if insert_at == -1:
                return text + f"\n{child_indent}{key}: {value}\n"
            insert_at += 1
            # ⚠️ 结尾必须带换行：``text[insert_at:]`` 从下一行第一个字符开始，
            # 少了 ``\n`` 会拼成 ``status: broken  name: xxx``，整份 YAML 解析失败
            # （然后被复验闸门回滚，报的是"找不到用例"，看不出是换行丢了）。
            return (
                text[:insert_at]
                + f"{child_indent}{key}: {value}\n"
                + text[insert_at:]
            )

    # 扁平用例：插到第一个内容行前面。**要跳过文件头的注释 / ``%YAML`` 指令 /
    # ``---`` 文档标记** —— 插在 ``---`` 前面会凭空多出一份只含 status 的文档，
    # 用例本体落到第二份文档里，safe_load 只读第一份，状态和用例就不在同一层了。
    insert_at = 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if (
            not stripped
            or stripped.startswith("#")
            or stripped.startswith("%")
            or stripped == "---"
            or stripped.startswith("--- #")
        ):
            insert_at += len(line)
            continue
        break
    return text[:insert_at] + f"{key}: {value}\n" + text[insert_at:]


def _testcase_child_indent(text: str) -> str:
    """``testcase:`` 下第一行子键的缩进（默认 2 空格）。"""
    root = re.search(r"^testcase[ \t]*:[ \t]*(?:#.*)?$", text, re.MULTILINE)
    if not root:
        return "  "
    for line in text[root.end():].splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        return line[: len(line) - len(line.lstrip())]
    return "  "


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


def _norm_endpoint(path: Any) -> str:
    """路径归一：去 query/hash、模板占位符和具体 id 统一成 ``{}``。"""
    text = str(path or "").strip().split("?", 1)[0].split("#", 1)[0]
    text = _PATH_PLACEHOLDER_RE.sub("{}", text)
    text = _NUMERIC_SEGMENT_RE.sub("/{}", text)
    if len(text) > 1:
        text = text.rstrip("/")
    return text.lower()


def _looks_like_method_path(text: Any) -> bool:
    """``PUT /admin/area`` 这类「方法 + 路径」的名字。"""
    return bool(_METHOD_PATH_RE.match(str(text or "")))


def _case_endpoints(case: dict[str, Any]) -> list[tuple[str, str]]:
    """用例调了哪些 ``(方法, 路径)`` —— 扁平/YAML 都从 steps 里取。"""
    out: list[tuple[str, str]] = []
    steps = case.get("steps")
    if isinstance(steps, list):
        for step in steps:
            if isinstance(step, dict):
                out.append(
                    (
                        str(step.get("method") or "").strip().upper(),
                        str(step.get("path") or "").strip(),
                    )
                )
    if not out:
        out.append(
            (
                str(case.get("method") or "").strip().upper(),
                str(case.get("path") or "").strip(),
            )
        )
    return out


def _live_business_name(project: str, path: str, method: str) -> str:
    """从现役用例里找这个接口的中文业务名（报告里的「接口名称」）。

    **方法必须一致**：``GET /admin/area/page``（查询）和
    ``POST /admin/area``（新增）是两件事，只按路径匹配会把新增用例
    命名成「查询地区」—— 比不给名字更糟。
    """
    wanted_path = _norm_endpoint(path)
    wanted_method = str(method or "").strip().upper()
    if not wanted_path or not wanted_method:
        return ""

    try:
        loaded = _load_all(project)
    except Exception as exc:  # noqa: BLE001 - 取现役名字失败不该挡住写用例
        log.debug("扫现役用例取业务名失败: %s", exc)
        return ""

    for _file, case in loaded:
        matches = any(
            m == wanted_method and _norm_endpoint(p) == wanted_path
            for m, p in _case_endpoints(case)
        )
        if not matches:
            continue
        name = str(case.get("name") or "").strip()
        if not name or _looks_like_method_path(name) or not _CJK_RE.search(name):
            continue
        return name
    return ""


def align_case_naming(
    project: str,
    module: str,
    path: str,
    method: str,
    api_name: Any,
    title: Any,
    case_name: Any = "",
) -> dict[str, str]:
    """把名字收敛成现役约定「模块中文名 - 业务动作（场景）」。

    **为什么在写入这一层兜底**

    ``case_gen`` 的提示词已经要求模型写中文业务名，但它不总是照做 ——
    接口文档里那个「接口名」字段经常就是 ``PUT /admin/area``，模型抄过去
    最省事。提示词管不住的，写入前必须拦：报告里一旦出现
    ``[ai admin area] PUT /admin/area - PUT /admin/area（正常）``，
    人根本看不出这条用例测的是什么。

    规则：

    1. ``api_name`` 为空 / 是「方法 + 路径」/ 不是中文
       → 按 ``方法 + 路径`` 去现役用例里找中文业务名
    2. ``title`` 里重复了被替换掉的旧名字（``PUT /admin/area（正常）``）
       → 只留场景。**只剥旧名字**，不动调用方自己写的业务名 ——
       ``分页查询地区_正例校验返回结构`` 这种标题是现役用例的写法，
       剥完只剩 ``_正例校验返回结构``，反而更难看
    3. ``case_name`` 里带着被替换掉的旧名字 → 作废，交给
       :func:`_default_case_name` 重拼

    Returns:
        ``{"api_name": ..., "title": ..., "case_name": ...}``；
        ``case_name`` 为空串表示「请用默认拼法」。
    """
    api_name = str(api_name or "").strip()
    title = str(title or "").strip()
    case_name = str(case_name or "").strip()

    original = api_name
    if (
        not api_name
        or _looks_like_method_path(api_name)
        or not _CJK_RE.search(api_name)
    ):
        live = _live_business_name(project, path, method)
        if live:
            api_name = live

    # 「PUT /admin/area（正常请求）」→「（正常请求）」
    if original and title.startswith(original):
        rest = title[len(original):].strip(" -—:：")
        if rest:
            title = rest
        elif title == original:
            title = ""

    # 标题整段就是「方法 路径」时，只保括号里的场景，别再留路径
    if _looks_like_method_path(title):
        match = _SCENARIO_TAIL_RE.search(title)
        title = match.group(0).strip() if match else ""

    if case_name and (
        _looks_like_method_path(case_name)
        or (original and original != api_name and original in case_name)
    ):
        case_name = ""

    return {"api_name": api_name, "title": title, "case_name": case_name}


def _ensure_sandbox_label(project: str, module: str, path: str) -> None:
    """``ai_`` 沙箱模块补中文 ``module.yaml``。

    没有 label 时报告左侧树显示 ``ai admin area``；补上之后是
    「地区管理（AI 沙箱）」，用例名称里的模块前缀也才对得上现役用例。
    补不上不影响写用例 —— 只是显示名难看。
    """
    if not module.startswith("ai_"):
        return
    try:
        from ai.capabilities.case_gen import _sandbox_module_label

        _sandbox_module_label(project, module, {"path": path})
    except Exception as exc:  # noqa: BLE001 - 补 label 是锦上添花
        log.debug("补沙箱模块 label 失败（不影响写用例）: %s", exc)


def _default_case_name(
    project: str, module: str, api_name: str, title: str, label: str = ""
) -> str:
    """按现役约定拼「模块中文名 - 业务功能（场景）」。

    ``create_case`` 以前只拼 ``api_name - title``，少了模块前缀，
    和现役 132 条手工用例对不上；``case_gen`` 那条链路自己拼了全名，
    两个入口于是产出两种命名。这里统一。

    ``label`` 给了就直接用 —— 改名预览工具要在**不写 module.yaml** 的
    前提下算出「写完会是什么名字」。

    模块中文名查不到（新模块、配置损坏）就退回框架的英文显示名，
    再查不到就用模块名 —— 起名失败不能挡住写用例。
    """
    label = str(label or "").strip()
    if not label:
        try:
            from utils.cases.module_manager import ModuleManager

            label = str(ModuleManager.get_label(project, module) or "").strip()
        except Exception as exc:  # noqa: BLE001 - 起名失败不该挡住写用例
            log.warning("取模块中文名失败，退回模块名: %s", exc)

    api_name = str(api_name or "").strip()
    title = str(title or "").strip()
    # 兼容旧调用风格：title 里又写了一遍业务名（"修改地区（正常请求）"）。
    # 不剥掉的话会拼成「地区管理 - 修改地区 - 修改地区（正常请求）」。
    if api_name and title.startswith(api_name):
        rest = title[len(api_name):].strip()
        if rest:
            title = rest
        elif title == api_name:
            title = ""
    if api_name and title:
        core = f"{api_name}{title}" if title[0] in "（(" else f"{api_name} - {title}"
    else:
        core = api_name or title
    prefix = label or module
    return f"{prefix} - {core}" if core else prefix


#: 自动造数上下文缓存：``(项目|OpenAPI 快照路径) -> (快照 mtime, ctx, endpoints)``
#
# MCP 服务是长驻进程，一次会话里可能连续新建几十条用例。每建一条都扫一遍
# 全部用例 + 解析 145 个接口要 1 秒以上，必须缓存；快照换了才重建。
_CHAIN_CONTEXT_CACHE: dict[str, tuple[float, Any, list[Any]]] = {}

#: 挂了造数链路的用例统一打这个标（和生成器保持同一口径）
_DATA_CHAIN_MARK = "api_data_chain"

#: 写方法（和 data_orchestrator / validator 同一口径）
_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _chain_context(project: str) -> tuple[Any | None, list[Any]]:
    """给造数规划器准备 ``(ProjectContext, endpoints)``。

    只读本地 OpenAPI 快照和现役用例，不联网。拿不到就返回
    ``(None, [])`` —— 造数是锦上添花，不能因为它把新建用例挡住。
    """
    from ai.agent.tools.endpoints import _openapi_path

    snapshot = _openapi_path()
    try:
        mtime = snapshot.stat().st_mtime if snapshot.exists() else 0.0
    except OSError:
        mtime = 0.0

    key = f"{project}|{snapshot}"
    cached = _CHAIN_CONTEXT_CACHE.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1], cached[2]
    if not snapshot.exists():
        return None, []

    try:
        from ai.agent.tools.endpoints import load_openapi
        from ai.capabilities.case_gen import build_context
        from tools.importer.openapi_parser import parse_openapi

        endpoints = parse_openapi(load_openapi(), source=str(snapshot))
        # 不传 endpoints：``build_context`` 会为每个接口预取图谱映射，
        # 造数规划用不到，白白多花几秒。
        ctx = build_context(project, [])
    except Exception as exc:  # noqa: BLE001 - 文档没同步/用例读坏了都要能继续写用例
        log.warning("建造数上下文失败，跳过自动清理：%s", exc)
        return None, []

    _CHAIN_CONTEXT_CACHE[key] = (mtime, ctx, endpoints)
    return ctx, endpoints


def _json_value(value: Any) -> Any:
    """JSON 字符串 → Python 对象（解析不了就原样返回）。

    模型有时把 ``request_data`` 写成 ``'{"areaName": "x"}'`` 这种字符串。
    造数规划要读里面的字段，必须先还原成对象。
    """
    if not isinstance(value, str):
        return value
    text = value.strip()
    if len(text) < 2 or text[0] not in "{[":
        return value
    try:
        return json.loads(text)
    except ValueError:
        return value


def _as_step_list(value: Any) -> Any:
    """JSON 字符串形式的前置/后置 → 列表（解析不了就原样返回）。"""
    return _json_value(value)


def _marks_list(marks: Any) -> list[str]:
    if isinstance(marks, (list, tuple, set)):
        parts = list(marks)
    else:
        parts = str(marks or "").replace(";", ",").split(",")
    return [str(item).strip() for item in parts if str(item).strip()]


def _add_mark(marks: Any, mark: str) -> str:
    parts = _marks_list(marks)
    if mark not in parts:
        parts.append(mark)
    return ",".join(parts)


def _sync_echoed_asserts(
    step: dict[str, Any],
    before: Any,
    after: Any,
) -> None:
    """造数链路替换了请求体里的名称字段后，把断言里回显的旧值一起换掉。

    模型经常写 ``{"type": "equal", "field": "data.areaName", "expected": "测试区域"}``。
    主步骤的名称被换成 ``{{tools.fixed(...)}}`` 之后，这条断言必然失败 ——
    它不是接口错了，是用例里抄了一份请求值。这里按字段对应关系同步。
    """
    if not isinstance(before, dict) or not isinstance(after, dict):
        return
    echoes: dict[str, str] = {}
    for key, old in before.items():
        new = after.get(key)
        if isinstance(old, str) and isinstance(new, str) and new != old:
            echoes[old] = new
    if not echoes:
        return
    for assertion in step.get("assert") or []:
        if not isinstance(assertion, dict):
            continue
        if assertion.get("expected") in echoes:
            assertion["expected"] = echoes[assertion["expected"]]
        params = assertion.get("params")
        if isinstance(params, list):
            assertion["params"] = [
                echoes.get(item, item) if isinstance(item, str) else item
                for item in params
            ]


def _attach_write_cleanup(
    *,
    project: str,
    module: str,
    case_name: str,
    marks: Any,
    step: dict[str, Any],
    setups: Any,
    teardowns: Any,
) -> tuple[Any, Any, str, str]:
    """给写用例自动补「造数 → 查 id → 清理」链路。

    只有**正例**、且调用方没自带 setups/teardowns 时才规划；反例本来就被
    拒绝，不会留下数据，无条件清理反而会把正确的拒绝变成 teardown 失败。

    返回 ``(setups, teardowns, marks, note)``。``note`` 为空表示没挂上。
    """
    if str(step.get("method") or "").upper() not in _WRITE_METHODS:
        return setups, teardowns, marks, ""
    if setups or teardowns:
        # 模型自己写了链路就尊重它（安全闸在运行时兜底）。
        return setups, teardowns, marks, ""

    from ai.pipeline.data_orchestrator import attach_chain, is_positive_case
    from ai.pipeline.intents import dimension_ids_of

    dims = dimension_ids_of(marks)
    if not is_positive_case(_marks_list(marks), dims):
        # 反例/边界/权限用例根本没写进去，无条件清理会把正确的拒绝
        # 变成 teardown 失败。先按标签挡掉，连上下文都不必建。
        return setups, teardowns, marks, ""

    ctx, endpoints = _chain_context(project)
    if ctx is None or not endpoints:
        return setups, teardowns, marks, ""

    # fixed(...) 的 key 要带真实模块名，否则同一接口在不同模块里会串名。
    # 浅拷贝足够：只改 module_override 这一个字符串字段。
    scoped = copy.copy(ctx)
    scoped.module_override = module

    case = {
        "name": case_name,
        "marks": _marks_list(marks) or [module],
        "steps": [step],
    }
    before_data = copy.deepcopy(step.get("data"))
    try:
        result = attach_chain(
            case,
            endpoints,
            scoped,
            dimensions=dimension_ids_of(marks),
            # 模型给的 request_data 是按接口文档准备的，优先拿它造数；
            # 生成器那条链路仍用现役正例兜底（文档示例常有非法占位值）。
            prefer_main_body=True,
        )
    except Exception as exc:  # noqa: BLE001 - 规划失败不能把新建用例打断
        log.warning("造数链路规划失败（%s），用例保持原样: %s", case_name, exc)
        return setups, teardowns, marks, ""

    if not str(result).startswith("ok:"):
        if str(result).startswith("failed:"):
            log.warning(
                "写用例 %s 给不出安全造数链路（%s），未自动补清理",
                case_name,
                result,
            )
        return setups, teardowns, marks, ""

    _sync_echoed_asserts(step, before_data, step.get("data"))
    new_marks = _add_mark(marks or module, _DATA_CHAIN_MARK)
    log.info("用例 %s 已自动挂造数链路：%s", case_name, result[3:])
    return case.get("setups"), case.get("teardowns"), new_marks, result[3:]


def create_case(
    project: str,
    module: str,
    path: str,
    method: str,
    title: str,
    assertions: list[dict[str, Any]] | None = None,
    api_name: str = "",
    case_name: str = "",
    request_data: Any = None,
    request_headers: Any = None,
    query_params: Any = None,
    priority: str = "P1",
    marks: str = "",
    status: str = "draft",
    setups: Any = None,
    teardowns: Any = None,
    steps: Any = None,
) -> dict[str, Any]:
    """往模块的 Excel 里追加一条新用例。

    默认状态是 ``draft``（CI 不跑）—— 新写的用例没跑过就进 CI，
    挂一次就红了主干。跑通之后再用 ``set_case_status`` 改成 ``debugged``。

    Args:
        steps: 多步场景链（创建→查询→修改→删除）。给了就以它为准，
            写进「步骤」列；``path`` / ``method`` / ``assertions`` 只用于
            命名和单步兜底。不给就是「一行一步」的老格式。
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

    # 多步场景链：给了 steps 就以它为准（写进「步骤」列），
    # 行级的 path/method/assertions 只用于命名和单步兜底。
    step_list = _normalize_steps(steps)
    if step_list:
        path = str(step_list[0].get("path") or path)
        method = str(step_list[0].get("method") or method).upper()
        if not assertions:
            assertions = list(step_list[0].get("assert") or [])

    if not isinstance(assertions, list) or not assertions:
        raise ToolError(
            "断言不能为空",
            hint="至少要有 success 断言，例如 "
            '[{"type": "equal", "field": "success", "expected": true}]',
        )

    # 模型常把这些字段写成 JSON 字符串。先还原成对象：造数规划要读字段
    # 名，质量闸要按列表逐条判断，字符串形态在两边都看不进去。
    request_data = _json_value(request_data)
    request_headers = _json_value(request_headers)
    query_params = _json_value(query_params)
    setups = _as_step_list(setups)
    teardowns = _as_step_list(teardowns)

    teardown_db_assert = _find_teardown_db_assert(teardowns)
    if teardown_db_assert:
        raise ToolError(
            f"teardowns 里不能放落库断言（{teardown_db_assert}）",
            hint="落库断言放主步骤的 assertions；teardowns 只做清理",
        )

    # ⚠️ 新建用例**只能是 draft**。
    #
    # ``status`` 不在工具 schema 里，但注册表是按参数名直接调 handler 的 ——
    # 模型多塞一个 ``status="debugged"`` 就能绕开 ``set_case_status`` 的
    # 「跑通过凭据」闸，把一条从没执行过的用例塞进 CI。这条路必须堵死。
    if str(status or "").strip().lower() not in ("", "draft"):
        raise ToolError(
            f"新建用例只能是 draft，收到 {status!r}",
            hint=(
                "刚写出来的用例还没跑过，直接标 debugged 等于让 CI 执行一条从未跑过的用例。"
                "正确顺序：create_case（draft）→ run_cases(case_id=..., run_draft=true) "
                "跑通 → set_case_status 改成 debugged（会核对跑通过凭据）。"
            ),
        )

    # 名字对齐现役约定。调用方（模型 / case_gen / 评测脚本）经常把接口文档
    # 里的「接口名」直接当业务名传进来，那就是 ``PUT /admin/area`` ——
    # 报告里显示成「PUT /admin/area - PUT /admin/area（正常）」，
    # 既不是业务动作，也没法和现役用例对齐。写入是最后一道闸。
    naming = align_case_naming(project, module, path, method, api_name, title, case_name)
    api_name = naming["api_name"]
    title = naming["title"]
    case_name = naming["case_name"]

    if step_list:
        main_step = step_list[0]
    else:
        main_step = {
            "id": "main",
            "method": method,
            "path": path,
            "assert": assertions,
            "headers": request_headers,
            "data": request_data,
            "params": query_params,
        }
        step_list = [main_step]

    # 写用例自动补「造数 → 查 id → 清理」。以前只有批量生成器
    # （``case_gen``）会挂这条链路，智能体走 ``create_case`` 时全靠模型
    # 手写 setups/teardowns —— 实际经常漏掉，一跑就在环境里留脏数据。
    # 这里和生成器共用同一个规划器，链路挂上后主步骤的 path/data 也会被
    # 改写成引用查数步骤的输出（清理只删本用例刚造的那一条）。
    #
    # 只对**单步**用例自动补：多步链是调用方自己搭的（或生成器挂好的），
    # 再按「一个主步骤」去规划会改错步骤、和已有链路打架。
    chain_note = ""
    if len(step_list) == 1:
        setups, teardowns, marks, chain_note = _attach_write_cleanup(
            project=project,
            module=module,
            case_name=case_name,
            marks=marks,
            step=main_step,
            setups=setups,
            teardowns=teardowns,
        )
        path = str(main_step.get("path") or path)
        request_data = main_step.get("data")

    # 质量闸门：结构校验管不了「有没有信息量」。只有 status_code 的正例、
    # 造了数没人清的写用例、路径里还留着 {id} 的用例，都会在这里被拦下。
    # 现役模块直接拒绝；沙箱 ai_* 允许落盘，但强制标 needs_manual_setup，
    # 保证它们不进 CI、不计入覆盖分子。
    from ai.rules.validator import check_case, enforce, format_hint, is_sandbox

    gate = enforce(
        module,
        check_case(
            case_name=case_name,
            api_name=api_name,
            steps=step_list,
            setups=setups,
            teardowns=teardowns,
            marks=marks,
        ),
    )
    quality_warnings: list[str] = []
    if gate.violations:
        if is_sandbox(module):
            quality_warnings = gate.messages()
            parts = [p.strip() for p in str(marks or "").replace(";", ",").split(",") if p.strip()]
            if "needs_manual_setup" not in {p.lower() for p in parts}:
                parts.append("needs_manual_setup")
                marks = ",".join(dict.fromkeys(parts))
        else:
            raise ToolError(
                "用例质量不达标，拒绝落盘",
                hint=format_hint(gate),
            )

    # 多步用例：行级的「接口 / 请求方法 / 断言」展示第一步，
    # 完整步骤链放「步骤」列（读取端优先用「步骤」）。
    steps_flat: list[dict[str, Any]] = []
    if len(step_list) > 1:
        path = str(step_list[0].get("path") or path)
        method = str(step_list[0].get("method") or method).upper()
        flat_asserts = assertions_to_flat(step_list[0].get("assert") or [])
        steps_flat = _steps_to_flat(step_list)
    else:
        flat_asserts = assertions_to_flat(assertions)
    # 前置/后置也要转成扁平断言。写进 Excel 的是框架内部格式的话，
    # flat_parser 读回来会报「不认识的断言类型 check=''」—— 生成的用例
    # 存得下、跑不了，问题要到执行阶段才暴露。
    setups = _prepost_to_flat(setups)
    teardowns = _prepost_to_flat(teardowns)
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

    # 老文件没有「步骤」列 —— 多步用例要写进去，先把列补到最右边。
    # 放在写数据之前，否则多步内容会被静默丢掉（只写了个单步）。
    if len(step_list) > 1 and "步骤" not in headers:
        col = (sheet.max_column or 1) + 1
        sheet.cell(1, col).value = "步骤"
        headers["步骤"] = col

    # 沙箱模块先补中文 label —— 用例名称的模块前缀按 module.yaml 拼。
    # 放在格式检查之后：写不进去就别留下空目录和 module.yaml。
    _ensure_sandbox_label(project, module, path)

    # 没写请求头就用项目档案里的鉴权头 —— 否则新用例一跑就是 401。
    # 但标了 no_auth 的用例是**故意**匿名的（测未登录/越权），不能补。
    if request_headers in (None, "", {}, []):
        request_headers = {} if _has_no_auth_mark(marks) else _default_headers(project)

    # 多步链每一步都要能鉴权 —— 行级「请求头」列只代表第一步，
    # 其余步骤在这里补齐（已经显式写了的步骤不动）。
    if len(step_list) > 1:
        for item in step_list:
            if not item.get("headers"):
                item["headers"] = {} if _has_no_auth_mark(marks) else _default_headers(project)

    values = {
        "case_id": case_no,
        "模块": module,
        "标签": str(marks or module),
        # 用例名称是给人看的全名（也是 -k 搜索的 alias）：
        # 「模块中文名 - 业务功能（场景）」，和现役手工用例保持一致。
        # 调用方给了 case_name 就用它；没给才退回旧的两段拼法。
        "用例名称": str(case_name or "").strip()
        or _default_case_name(project, module, api_name, title),
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
        "步骤": _dump_cell(steps_flat),
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
        # 实际落盘的三列名字 —— 调用方可能传的是「方法+路径」，这里回显
        # 对齐后的结果，免得模型以为没生效又新建一条。
        "case_name": values["用例名称"],
        "api_name": api_name,
        "title": title,
        "status": str(status or "draft").lower(),
        "assertions": flat_asserts,
        "data_chain": chain_note,
        "setups": len(setups) if isinstance(setups, list) else 0,
        "teardowns": len(teardowns) if isinstance(teardowns, list) else 0,
        "quality_warnings": quality_warnings,
        "backup_dir": str(backup.parent) if backup is not None else "",
        "next_step": (
            f'用例是 draft 状态，CI 不会跑。先 run_cases(case_file="{_rel(file_path)}", '
            "run_draft=true) 确认通过（**不加 run_draft 一条都不会执行**，"
            "total=0 不代表通过），再 set_case_status 改成 debugged。"
        ),
    }


def _has_no_auth_mark(marks: Any) -> bool:
    """用例是否故意匿名（测未登录/越权）。

    约定用 ``no_auth`` 标签。``create_case`` 看到它就不补默认鉴权头，
    运行时 ``CaseRunner`` 也不补 —— 两处必须一致，否则一边补一边不补，
    用例名是"未登录"，请求却带着 token。
    """
    if isinstance(marks, str):
        parts = marks.replace(";", ",").split(",")
    elif isinstance(marks, (list, tuple, set)):
        parts = list(marks)
    else:
        parts = []
    return bool(
        {str(item).strip().lower() for item in parts}
        & {"no_auth", "noauth", "unauthenticated"}
    )


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


#: 认识的主步骤方法（和测试框架保持一致）
_STEP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})


def _normalize_steps(value: Any) -> list[dict[str, Any]]:
    """把多步场景链收敛成框架内部格式。

    入参可以是 JSON 字符串 / 单个对象 / 数组。每个步骤必须有
    ``path`` 和 ``method``；断言统一成列表（字典自动包一层）。
    结构不对直接抛 ``ToolError`` —— 宁可建不出来，也不要写进一条
    跑到一半才发现少路径的用例。
    """
    raw = _as_step_list(value)
    if raw in (None, "", {}, []):
        return []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        raise ToolError(f"steps 必须是 JSON 数组，拿到 {type(raw).__name__}")

    steps: list[dict[str, Any]] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise ToolError(f"steps 的第 {index} 项必须是对象，拿到 {item!r}")
        step = dict(item)
        step["id"] = str(step.get("id") or f"step{index}")
        path = str(step.get("path") or "").strip()
        if not path.startswith("/"):
            raise ToolError(f"steps 的第 {index} 项路径要以 / 开头，拿到 {path!r}")
        method = str(step.get("method") or "GET").strip().upper()
        if method not in _STEP_METHODS:
            raise ToolError(f"steps 的第 {index} 项方法不认识：{method!r}")
        step["path"] = path
        step["method"] = method

        asserts = step.get("assert")
        if isinstance(asserts, dict):
            asserts = [asserts]
        if asserts and not isinstance(asserts, list):
            raise ToolError(f"steps 的第 {index} 项 assert 必须是数组")
        step["assert"] = list(asserts or [])
        for key in ("headers", "data", "params"):
            if key in step:
                step[key] = _json_value(step[key])
        steps.append(step)

    return steps


def _steps_to_flat(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """多步链 → 扁平格式（断言转 ``check/comparator/expect``）。"""
    out: list[dict[str, Any]] = []
    for step in steps:
        item = {key: val for key, val in step.items() if key != "assert" and val is not None}
        item["assert"] = assertions_to_flat(step.get("assert") or [])
        out.append(item)
    return out


def _prepost_to_flat(value: Any) -> Any:
    """把前置/后置步骤里的框架断言统一转成 Excel 扁平断言。

    读取端（``utils/excel/flat_parser._parse_asserts``）只认扁平格式。
    已经是扁平格式的（带 ``check``）原样保留 —— 现役用例就是这种写法。
    """
    if not isinstance(value, list):
        return value
    out: list[Any] = []
    for item in value:
        if not isinstance(item, dict):
            out.append(item)
            continue
        step = dict(item)
        asserts = step.get("assert")
        if isinstance(asserts, dict):
            asserts = [asserts]
        if isinstance(asserts, list) and asserts:
            converted: list[Any] = []
            for assertion in asserts:
                if isinstance(assertion, dict) and "check" in assertion:
                    converted.append(assertion)
                else:
                    converted.append(assertion_to_flat(assertion))
            step["assert"] = converted
        step = {key: val for key, val in step.items() if val is not None}
        out.append(step)
    return out


def _is_cleanup_step(value: Any) -> bool:
    """后置里的纯清理动作（DELETE / factory_cleanup / db）。"""
    if not isinstance(value, dict):
        return False
    op = str(value.get("operation_type") or "").lower()
    if op in ("factory", "factory_cleanup", "db"):
        return True
    return str(value.get("method") or "").upper() == "DELETE"


def _find_teardown_db_assert(value: Any) -> str | None:
    """在 teardowns 的**验证类步骤**里找落库断言，返回 SQL / 类型。

    落库断言只该放主步骤。后置只做清理：验证步骤上的断言失败会被报成
    teardown 错误，主步骤先失败时还会多一条误导性的失败。

    清理步骤（DELETE）上的「删后查不到」不算 —— 那是在验证清理结果，
    属于后置自己的职责。
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
        return _find_teardown_db_assert(value)
    if isinstance(value, list):
        for item in value:
            if _is_cleanup_step(item):
                continue
            found = _find_teardown_db_assert(item)
            if found:
                return found
    elif isinstance(value, dict):
        if _is_cleanup_step(value):
            return None
        if str(value.get("check") or "").lower() == "db":
            return str(value.get("sql") or "db")
        kind = str(value.get("type") or "").lower()
        if kind.startswith(("db_query", "mysql_query")):
            return kind
        for key in ("assert", "asserts", "steps"):
            found = _find_teardown_db_assert(value.get(key))
            if found:
                return found
    return None


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
    "align_case_naming",
    "create_case",
    "next_case_no",
    "find_weakenings",
    "restore_from_backup",
]
