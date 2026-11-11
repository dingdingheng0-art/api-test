"""用例读取的公共逻辑（工具之间复用）。

**为什么要抽出来**

``list_cases`` / ``get_case`` / ``run_cases`` 都要做同一件事：
按「项目 / 模块 / 标签 / 关键字」筛出用例文件，再读出里面的用例。

Excel 一个文件能装几十条用例，``load_excel_cases`` 要开 openpyxl 解析，
几百个文件扫一遍是秒级开销 —— 智能体一次对话里可能问十次，
必须缓存。这里按「文件 + 修改时间」缓存：改了文件自动失效，
没改就一直用内存里那份。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from utils.logs.log_manager import log

# 文件缓存：{绝对路径: (mtime, 用例列表)}
_CASE_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}

# 读失败的文件：{绝对路径: (mtime, 异常)}。
#
# 普通列表场景读不了就跳过（一个坏文件不该让整个列表挂掉）；但**数条数**
# 的场景跳不得 —— 跳过就是少数几条甚至数成 0，条数闸随之失效。
# 所以把失败也缓存下来，``strict=True`` 的调用方可以直接抛出去。
_FAILED_CACHE: dict[str, tuple[float, Exception]] = {}

# 这些字段太长，列表里不返回，只在 get_case 详情里给
HEAVY_KEYS = ("setups", "teardowns", "parameters", "extract")


def _as_list(value: Any) -> list[Any]:
    """把「应该是数组」的字段收敛成 list。

    用例文件是给人手改的：``assert`` / ``setups`` 写成单个对象或标量时，
    ``len()`` / 迭代会抛 ``TypeError``，把整条 get_case / 计数带崩。
    """
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, dict):
        return [value]
    return []


@dataclass
class CaseRef:
    """一条用例的定位信息（够智能体再调 ``get_case`` 拿到全文）。

    **两个 ID 的分工**

    ``id``      工具内部用的稳定哈希（``case_184707cf29``），给 ``get_case`` 用。
    ``case_id`` 人读的用例编号（``API-168``），给**写类工具**用。

    写类工具只认 ``case_id``：哈希是派生值，一旦文件挪位置就变了，
    而编号是人写在 Excel 里的，稳定、可核对、出错时也说得清是哪一条。
    """

    id: str                      # 稳定 ID：文件相对路径 + 用例序号/名字
    project: str
    module: str
    file: str                    # 相对项目根的路径
    name: str
    description: str = ""
    status: str = "draft"
    marks: list[str] = field(default_factory=list)
    steps: int = 0
    asserts: int = 0
    case_id: str = ""            # 人读编号（API-168），写类工具的入参

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "case_id": self.case_id,
            "project": self.project,
            "module": self.module,
            "file": self.file,
            "name": self.name,
            "description": self.description,
            "status": self.status,
            "marks": self.marks,
            "steps": self.steps,
            "asserts": self.asserts,
        }


def _rel(path: Path) -> str:
    from utils import paths

    try:
        return path.resolve().relative_to(paths.PROJECT_ROOT.resolve()).as_posix()
    except (ValueError, OSError):
        return path.as_posix()


def _module_of(path: Path, project: str) -> str:
    """从路径推模块名：``<根>/<项目>/<模块>/...``。"""
    parts = path.parts
    if project in parts:
        idx = parts.index(project)
        if len(parts) > idx + 2:
            return parts[idx + 1]
    return path.parent.name


def _load_file(path: Path, project: str, strict: bool = False) -> list[dict[str, Any]]:
    """读一个用例文件里的所有用例（带 mtime 缓存）。

    Args:
        strict: 读不了就抛异常，而不是返回空列表。**数条数的调用方必须开** ——
            静默返回 ``[]`` 会让「这次会跑几条」被低估，条数闸就形同虚设。
    """
    from testcases.case_loader import load_cases_from_file

    key = str(path.resolve())
    try:
        mtime = path.stat().st_mtime
    except OSError:
        if strict:
            raise
        return []

    cached = _CASE_CACHE.get(key)
    if cached and cached[0] == mtime:
        failed = _FAILED_CACHE.get(key)
        if strict and failed and failed[0] == mtime:
            raise failed[1]
        return cached[1]

    try:
        cases = load_cases_from_file(path, project=project)
    except Exception as exc:  # noqa: BLE001 - 一个坏文件不该让整个列表挂掉
        log.warning("用例文件读不了 %s: %s", path, exc)
        _CASE_CACHE[key] = (mtime, [])
        _FAILED_CACHE[key] = (mtime, exc)
        if strict:
            raise
        return []

    _FAILED_CACHE.pop(key, None)
    _CASE_CACHE[key] = (mtime, cases)
    return cases


def _case_of(data: dict[str, Any]) -> dict[str, Any]:
    """取用例本体（兼容 ``{"testcase": {...}}`` 和直接平铺两种）。"""
    if isinstance(data.get("testcase"), dict):
        return data["testcase"]
    return data


def count_asserts(case: dict[str, Any]) -> int:
    """一条用例里所有步骤的断言总数。"""
    total = 0
    for step in case.get("steps") or []:
        if isinstance(step, dict):
            total += len(_as_list(step.get("assert")))
    return total


def case_identities(cases: list[dict[str, Any]]) -> list[str]:
    """给**同一个文件**里的用例算身份串（ID 的输入，不含哈希）。

    优先级：``case_id`` → 用例名 → 用例名 + 描述 → 用例名 + 序号。

    单独抽出来是因为**图谱也要用同一套规则**（见
    :mod:`ai.knowledge.builder`）—— 否则工具说「API-168 是修改地区」、
    图谱说「API-168 是别的」，智能体就被自己的两份数据骗了。
    """
    names = [str(c.get("name") or "") for c in cases]
    counts: dict[str, int] = {}
    for name in names:
        counts[name] = counts.get(name, 0) + 1

    out: list[str] = []
    for idx, case in enumerate(cases):
        name = names[idx]

        case_no = str(case.get("case_id") or "").strip()
        if case_no:
            out.append(case_no)
            continue

        # 同文件内不重名：只用名字。插入新用例不会让别人的 ID 挪位
        if counts[name] == 1:
            out.append(name)
            continue

        # 重名（「查询地区详情」正常 / 查不到数据）：用描述区分
        desc = str(case.get("description") or "").strip()
        out.append(f"{name}::{desc}" if desc else f"{name}::{idx}")

    return out


def make_id(rel_path: str, identity: str) -> str:
    """生成稳定用例 ID：对「文件路径 + 身份串」取哈希。

    Args:
        rel_path: 用例文件相对项目根的路径
        identity: 身份串，见 :func:`case_identities`
    """
    digest = hashlib.sha1(f"{rel_path}::{identity}".encode("utf-8")).hexdigest()[:10]
    return f"case_{digest}"


def assign_ids(rel_path: str, cases: list[dict[str, Any]]) -> list[str]:
    """给**同一个文件**里的所有用例分配 ID。

    **身份为什么不能只用名字**

    一个模块里「查询地区详情（正常请求）」和「查询地区详情（查不到数据）」
    是**两条不同的用例、名字完全相同**。只按名字算哈希会把它们合并成一条 ——
    智能体拿到的详情只会有其中一条的内容。

    **为什么不一律带序号**

    序号会随插入挪位：在文件开头插一条，后面所有用例的 ID 全变，
    智能体手里的旧 ID 全失效。所以只在真正重名时才带。

    Returns:
        与 ``cases`` 等长的 ID 列表
    """
    return [make_id(rel_path, identity) for identity in case_identities(cases)]


def iter_cases(
    project: str = "",
    module: str = "",
    mark: str = "",
    keyword: str = "",
    status: str = "",
    strict: bool = False,
) -> list[tuple[CaseRef, dict[str, Any]]]:
    """按条件筛出用例。

    Args:
        project: 项目名（空 = 全部）
        module: 模块名（空 = 全部）
        mark: 标签，多个用逗号分隔（AND 语义：都要有）
        keyword: 关键字，匹配用例名 / 描述 / 路径
        status: 用例状态（draft / debugged / disabled / broken）
        strict: 有文件读不了就抛异常（默认跳过）。数条数时必须开，
            否则坏文件会被当成「0 条」，条数闸漏过去。

    Returns:
        ``[(CaseRef, 用例本体), ...]``
    """
    from testcases.case_loader import discover_cases
    from utils.cases.case_status import resolve_status
    from utils.cases.marks import extract_marks

    wanted_marks = [m.strip() for m in str(mark or "").split(",") if m.strip()]
    kw = str(keyword or "").strip().lower()
    wanted_status = str(status or "").strip().lower()

    results: list[tuple[CaseRef, dict[str, Any]]] = []

    for proj, path in discover_cases():
        if project and proj != project:
            continue
        mod = _module_of(path, proj)
        if module and mod != module:
            continue

        rel = _rel(path)
        loaded = _load_file(path, proj, strict=strict)
        cases = [_case_of(data) for data in loaded]
        ids = assign_ids(rel, cases)

        for case, cid in zip(cases, ids):
            name = str(case.get("name") or path.stem)
            marks = extract_marks(case)
            case_status = resolve_status(case)

            if wanted_marks and not all(m in marks for m in wanted_marks):
                continue
            if wanted_status and case_status != wanted_status:
                continue
            if kw:
                # 编号也要能搜 —— 用户常直接说「看下 API-168」
                haystack = (
                    f"{name} {case.get('description', '')} "
                    f"{case.get('case_id', '')} {rel}"
                ).lower()
                if kw not in haystack:
                    continue

            ref = CaseRef(
                id=cid,
                case_id=str(case.get("case_id") or ""),
                project=proj,
                module=mod,
                file=rel,
                name=name,
                description=str(case.get("description") or ""),
                status=case_status,
                marks=marks,
                steps=len(_as_list(case.get("steps"))),
                asserts=count_asserts(case),
            )
            results.append((ref, case))

    return results


def find_case(case_id: str) -> tuple[CaseRef, dict[str, Any]] | None:
    """按工具内部 ID 找一条用例。"""
    for ref, case in iter_cases():
        if ref.id == case_id:
            return ref, case
    return None


def find_case_by_number(case_no: str) -> tuple[CaseRef, dict[str, Any]] | None:
    """按人读编号（``API-168``）找一条用例。"""
    wanted = str(case_no or "").strip()
    if not wanted:
        return None
    for ref, case in iter_cases():
        if ref.case_id == wanted:
            return ref, case
    return None


def case_detail(ref: CaseRef, case: dict[str, Any]) -> dict[str, Any]:
    """把用例转成智能体看的详情结构（去掉重字段，保留关键信息）。"""
    detail = ref.to_dict()
    detail.update(
        {
            "allure": case.get("allure") or {},
            "depends_on": case.get("depends_on") or [],
            "step_list": [
                _step_summary(step)
                for step in (case.get("steps") or [])
                if isinstance(step, dict)
            ],
            "setup_count": len(_as_list(case.get("setups"))),
            "teardown_count": len(_as_list(case.get("teardowns"))),
            "has_parameters": bool(case.get("parameters")),
        }
    )
    return detail


def _step_summary(step: dict[str, Any]) -> dict[str, Any]:
    """一步的摘要：只给智能体看结构，不塞整段请求体。"""
    asserts = _as_list(step.get("assert"))
    return {
        "id": step.get("id") or "",
        "description": step.get("description") or "",
        "method": str(step.get("method") or "").upper(),
        "path": step.get("path") or step.get("api") or "",
        "assert_count": len(asserts),
        "assert_types": sorted(
            {str(a.get("type")) for a in asserts if isinstance(a, dict) and a.get("type")}
        ),
    }


def load_file_cases(path: Path, project: str = "") -> list[dict[str, Any]]:
    """读一个用例文件里的所有用例（带缓存）。

    写类工具（``case_io``）复验时也要读文件 —— 走这里，别自己再调一次
    ``load_cases_from_file``。两个缓存各读各的，写完文件后一份失效、
    另一份还是旧的，就会出现"刚写进去的用例复验说找不到"。
    """
    return _load_file(path, project)


def clear_cache() -> None:
    """清掉用例缓存（改用例后调）。"""
    _CASE_CACHE.clear()
    _FAILED_CACHE.clear()
    # 底层加载器（testcases.case_loader）也有自己的 mtime 缓存 ——
    # 一起清掉，否则"清完缓存在这次进程里还是读到旧的"。
    try:
        from testcases.case_loader import clear_load_cache

        clear_load_cache()
    except Exception as exc:  # noqa: BLE001 - 清缓存失败不该让工具挂掉
        log.debug("清理用例文件缓存失败: %s", exc)
