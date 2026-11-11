"""维度覆盖率分析。

**为什么不能只报接口覆盖率**：一个接口有一条正例也算"覆盖"，
但 A 参数契约 / C 权限 / D 落库 / E 关联一个没测，等于没测。

本模块按 `apitest-design` 的最小交付标准：

1. 用 ``intents.plan_endpoint_intents`` 算出每个接口的**应测维度**
2. 从用例的 marks / 名称 / 断言里识别**实际覆盖维度**
3. 给出缺口清单、薄用例（只有 status_code/success）、假设用例（needs_manual_setup）

用法::

    python -m ai.pipeline.dimension_coverage --project mall4j --module ai_admin_area \
        --file .cache/apidoc/openapi.json --only-path /admin/area
"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from ai.pipeline.intents import DIMENSION_IDS, dimension_ids_of

#: 维度 ID 全集（唯一定义在 ``intents``，避免两处漂移）
DIMENSIONS = DIMENSION_IDS

#: 判断「薄用例」时不算业务字段的断言
_WEAK_FIELDS = {"", "code", "success"}
_WEAK_TYPES = {"status_code", "status", "http_status"}

#: 鉴权头名称（大小写不敏感）。C1 未登录用例必须不带这些头。
_AUTH_HEADERS = {
    "authorization",
    "token",
    "x-token",
    "admin-token",
    "access-token",
    "x-access-token",
    "cookie",
}

_NUMERIC_SEGMENT_RE = re.compile(r"/\d+(?=/|$)")


def _norm_path(path: Any) -> str:
    text = re.sub(r"\{[^}]+\}", "{}", str(path or "").strip())
    return _NUMERIC_SEGMENT_RE.sub("/{}", text)


def _endpoint_key(method: Any, path: Any) -> str:
    return f"{str(method or '').upper()} {_norm_path(path)}"


def _has_auth(step: dict[str, Any]) -> bool:
    headers = step.get("headers") if isinstance(step, dict) else None
    if not isinstance(headers, dict):
        return False
    return any(str(key).lower() in _AUTH_HEADERS for key in headers)


def _is_no_auth_case(case: dict[str, Any]) -> bool:
    """用例是否显式声明匿名请求。

    光看「请求头里没有 Authorization」不够：框架会在请求头缺失时
    自动补 token。只有标了 ``no_auth``，请求才是真的匿名。
    """
    marks = case.get("marks") or []
    if isinstance(marks, str):
        marks = marks.replace(";", ",").split(",")
    return bool(
        {str(item).strip().lower() for item in marks}
        & {"no_auth", "noauth", "unauthenticated"}
    )


def _marks_set(case: Any) -> set[str]:
    """用例标签归一成小写集合（字符串按逗号拆，不逐字符展开）。"""
    marks = case.get("marks") if isinstance(case, dict) else None
    if isinstance(marks, str):
        marks = marks.replace(";", ",").split(",")
    elif isinstance(marks, dict):
        marks = list(marks.keys())
    elif not isinstance(marks, (list, tuple, set, frozenset)):
        return set()
    return {str(item).strip().lower() for item in marks if str(item).strip()}


def _iter_asserts(step: Any) -> list[dict[str, Any]]:
    """取出步骤里的断言对象。

    用例来自 Excel / YAML（人写的），``assert`` 可能被写成一个对象、
    一个字符串，或数组里混进字符串。直接迭代会在 ``item.get`` 抛
    ``AttributeError``，让整个覆盖率报告挂掉。
    """
    asserts = step.get("assert") if isinstance(step, dict) else None
    if isinstance(asserts, dict):
        asserts = [asserts]
    if not isinstance(asserts, (list, tuple)):
        return []
    return [item for item in asserts if isinstance(item, dict)]


def _has_db_assert(steps: list[dict[str, Any]]) -> bool:
    for step in steps:
        for item in _iter_asserts(step):
            if str(item.get("type") or "").lower().replace(" ", "_").startswith("db_query"):
                return True
    return False


def _asserts_rejection(step: dict[str, Any]) -> bool:
    """用例里有没有「期望失败」的断言。"""
    for item in _iter_asserts(step):
        atype = str(item.get("type") or "").lower().replace(" ", "_")
        field = str(item.get("field") or "")
        expected = item.get("expected")
        # status_code 断言没有 field，字段名就是 type；框架也支持 field=$status
        if (atype in ("status_code", "http_status", "status") or field == "$status") and str(
            expected
        ) in {"401", "403"}:
            return True
        if field == "success" and (
            atype == "is_false" or expected is False
        ):
            return True
        # 和 validator.asserts_rejection 保持同一口径：
        # ``success not_equal true`` 也是期望失败。
        if (
            field == "success"
            and atype in ("not_equal", "!=")
            and (expected is True or str(expected).strip().lower() == "true")
        ):
            return True
        if field == "code" and atype == "not_equal":
            return True
        # 探针实测后会给精确错误码（如 A00005），这也是「期望失败」
        if (
            field == "code"
            and atype in ("equal", "equals", "==")
            and str(expected) not in {"", "00000", "200", "0"}
        ):
            return True
        if field == "code" and atype == "in":
            container = item.get("container") or item.get("expected")
            if not isinstance(container, (list, tuple, set, frozenset)):
                container = [] if container is None else [container]
            if "00000" not in [str(v) for v in container]:
                return True
    return False


def _asserts_empty(step: dict[str, Any]) -> bool:
    """是否断言「查不到 / 空结果」。

    不存在资源（F1）的正确期望未必是报错 —— 不少系统返回成功 + data 为空。
    """
    for item in _iter_asserts(step):
        atype = str(item.get("type") or "").lower().replace(" ", "_")
        field = str(item.get("field") or "")
        if atype in ("is_none", "is_null", "not_exists") and (
            field == "data" or field.startswith("data.")
        ):
            return True
        if atype in ("length_equal", "length_eq") and str(item.get("expected")) == "0":
            return True
        if atype == "db_query_not_exists":
            return True
    return False


def _asserts_success(step: dict[str, Any]) -> bool:
    for item in _iter_asserts(step):
        atype = str(item.get("type") or "").lower().replace(" ", "_")
        field = str(item.get("field") or "")
        expected = item.get("expected")
        if field == "success" and (atype == "is_true" or expected is True):
            return True
        if field == "code" and atype in {"equal", "eq", "=="}:
            return True
    return False


def _verify_dimension(
    dim: str,
    case: dict[str, Any],
    steps: list[dict[str, Any]],
    step: dict[str, Any],
) -> tuple[bool, str]:
    """标称维度是否真的被用例内容证实。

    **不接受"标了就算"**：标记只是声明，覆盖度必须看实际请求和断言。
    """
    if dim == "C1":
        if _has_auth(step):
            return False, "标了未登录，但用例带了鉴权头"
        if not _is_no_auth_case(case):
            return False, "没有 no_auth 标记，框架会自动补 token，实际不是匿名请求"
        if not _asserts_rejection(step):
            return False, "未登录用例没有断言「被拒绝」"
        return True, ""
    if dim in {"D1", "D2", "D3", "D4"}:
        if not _has_db_assert(steps):
            return False, "标了落库断言，但没有 db_query 断言"
        return True, ""
    if dim == "E3":
        if not (case.get("teardowns") or case.get("teardown")):
            return False, "标了清理隔离，但没有 teardown"
        return True, ""
    if dim == "F1":
        # 不存在资源：报错 或 成功但为空，都算验证过
        if not (_asserts_rejection(step) or _asserts_empty(step)):
            return False, "没有断言「查不到 / 失败」"
        return True, ""
    if dim in {"A1", "A2", "A3", "A4", "A5", "A6", "A7", "F2", "F3", "F4"}:
        if not _asserts_rejection(step):
            return False, "反例没有任何「期望失败」断言"
        return True, ""
    if dim == "P0":
        if not _asserts_success(step):
            return False, "正例没有成功语义断言"
        return True, ""
    return True, ""


def _infer_dimensions(case: dict[str, Any], step: dict[str, Any]) -> set[str]:
    """从 marks / 名称 / 请求值 / 断言里识别「这条用例在测什么」。

    注意：一条用例可以同时是正例 + 分页边界，所以 P0 与其它维度是**叠加**关系，
    不能在命中 B1 之后就不算 P0。
    """
    marks = {str(m).lower() for m in (case.get("marks") or [])}
    text = f"{case.get('name', '')} {case.get('description', '')}".lower()

    # ``dim:C1`` 形式；优先级标签 p0/p1 不会被误认成维度
    dims: set[str] = dimension_ids_of(case.get("marks") or [])

    if "必填" in text or "缺少" in text:
        dims.add("A1")
    if "空值" in text or "null" in text:
        dims.add("A6")
    if "类型" in text:
        dims.add("A2")
    if "枚举" in text:
        dims.add("A3")
    if "未登录" in text or "无权限" in text or "token" in text:
        dims.add("C1")
    if "不存在" in text:
        dims.add("F1")

    # 分页边界要看真实取值，不能看到 page 就算边界
    values = {}
    for source in (step.get("params"), step.get("data"), step.get("body")):
        if isinstance(source, dict):
            values.update(source)
    for name in ("size", "limit", "pageSize", "page_size"):
        try:
            number = int(values.get(name))
        except (TypeError, ValueError):
            continue
        if number <= 0 or number > 100:
            dims.add("B1")
            break

    for item in _iter_asserts(step):
        atype = str(item.get("type") or "").lower().replace(" ", "_")
        if atype.startswith("db_query"):
            method = str(step.get("method") or "").upper()
            dims.add({"GET": "D1", "DELETE": "D3"}.get(method, "D2"))

    if _asserts_success(step) and not _asserts_rejection(step):
        dims.add("P0")

    if not dims:
        dims.add("NEG" if marks & {"negative", "boundary", "invalid"} else "P0")
    return dims


def _probe_note(case: dict[str, Any]) -> str:
    """从描述里取出探针实测结论（生成器写进去的那一行）。"""
    for line in reversed(str(case.get("description") or "").splitlines()):
        text = line.strip()
        if text.startswith("探针实测"):
            return text
    return ""


def _business_assert_count(step: dict[str, Any]) -> int:
    count = 0
    for item in _iter_asserts(step):
        atype = str(item.get("type") or "").lower().replace(" ", "_")
        if atype.startswith("db_query"):
            count += 1  # 落库断言也是业务断言
            continue
        if atype in _WEAK_TYPES:
            continue
        field = str(item.get("field") or "")
        if field in _WEAK_FIELDS:
            continue
        count += 1
    return count


def analyze(
    project: str,
    module: str = "",
    endpoints: list[Any] | None = None,
    max_intents: int = 8,
) -> dict[str, Any]:
    """算维度覆盖率。

    Args:
        project: 项目名
        module: 只分析某个模块的用例（空 = 全部）
        endpoints: 接口清单（空 = 读缓存 openapi）
        max_intents: 每个接口最多算几个应测维度
    """
    from ai.agent.tools._common import iter_cases
    from ai.capabilities.case_gen import build_context
    from ai.pipeline.intents import plan_endpoint_intents

    if endpoints is None:
        endpoints = _load_endpoints()

    ctx = build_context(project, endpoints)
    expected: dict[str, list[str]] = {}
    endpoint_by_key: dict[str, Any] = {}
    for ep in endpoints:
        key = _endpoint_key(ep.method, ep.path)
        endpoint_by_key[key] = ep
        intents = plan_endpoint_intents(
            ep, max_intents=max_intents, mappings=ctx.db_mappings(ep)
        )
        expected[key] = [intent.dimension for intent in intents]

    actual: dict[str, set[str]] = defaultdict(set)
    thin_cases: list[dict[str, Any]] = []
    assumption_cases: list[dict[str, Any]] = []
    claimed_unverified: list[dict[str, Any]] = []
    defect_candidates: list[dict[str, Any]] = []
    case_count = 0

    for ref, case in iter_cases(project=project, module=module):
        if not isinstance(case, dict):
            continue
        case_count += 1
        marks = _marks_set(case)
        if "needs_manual_setup" in marks:
            assumption_cases.append(
                {"case_id": ref.case_id, "name": case.get("name"), "reason": "标了 needs_manual_setup"}
            )
        if "defect_candidate" in marks:
            # 探针实测「服务端接受了本该拒绝的请求」—— 这不是覆盖，是缺陷线索。
            defect_candidates.append(
                {
                    "case_id": ref.case_id,
                    "name": case.get("name"),
                    "note": _probe_note(case),
                }
            )

        steps = [s for s in (case.get("steps") or []) if isinstance(s, dict)]
        if not steps:
            continue
        step = steps[0]
        key = _endpoint_key(step.get("method"), step.get("path"))
        if key not in expected:
            continue

        dims = _infer_dimensions(case, step)
        verified: set[str] = set()
        for dim in dims:
            ok, reason = _verify_dimension(dim, case, steps, step)
            if ok:
                verified.add(dim)
            else:
                claimed_unverified.append(
                    {"case_id": ref.case_id, "name": case.get("name"), "dimension": dim, "reason": reason}
                )
        actual[key].update(verified)

        # 薄用例只对**正例**有意义：正例要看字段断言够不够。
        # 反例的职责是「被拒绝」，一条精确的拒绝断言就够，不该按条数扣分。
        if "P0" in dims:
            business_asserts = sum(_business_assert_count(s) for s in steps)
            if business_asserts < 2:
                thin_cases.append(
                    {
                        "case_id": ref.case_id,
                        "name": case.get("name"),
                        "business_asserts": business_asserts,
                    }
                )

    details: list[dict[str, Any]] = []
    total_expected = 0
    total_covered = 0
    for key, want in expected.items():
        got = sorted(d for d in actual.get(key, set()) if d in DIMENSIONS)
        want_unique = list(dict.fromkeys(want))
        missing = [d for d in want_unique if d not in got]
        covered = [d for d in want_unique if d in got]
        total_expected += len(want_unique)
        total_covered += len(covered)
        details.append(
            {
                "endpoint": key,
                "expected": want_unique,
                "covered": covered,
                "missing": missing,
                "coverage": round(len(covered) / len(want_unique), 2) if want_unique else 1.0,
                "case_count": _count_cases_for(project, module, key),
            }
        )

    details.sort(key=lambda item: item["coverage"])
    ratio = round(total_covered / total_expected, 2) if total_expected else 0.0

    return {
        "summary": {
            "project": project,
            "module": module or "(全部)",
            "endpoints": len(expected),
            "cases": case_count,
            "expected_dimensions": total_expected,
            "covered_dimensions": total_covered,
            "dimension_coverage": ratio,
            "thin_cases": len(thin_cases),
            "assumption_cases": len(assumption_cases),
            "defect_candidates": len(defect_candidates),
            "claimed_unverified": len(claimed_unverified),
        },
        "endpoints": details,
        "thin_cases": thin_cases,
        "assumption_cases": assumption_cases,
        "defect_candidates": defect_candidates,
        "claimed_unverified": claimed_unverified,
    }


def _count_cases_for(project: str, module: str, key: str) -> int:
    """某个接口实际有多少条用例。"""
    from ai.agent.tools._common import iter_cases

    count = 0
    for _ref, case in iter_cases(project=project, module=module):
        steps = [s for s in (case.get("steps") or []) if isinstance(s, dict)]
        if steps and _endpoint_key(steps[0].get("method"), steps[0].get("path")) == key:
            count += 1
    return count


def _load_endpoints() -> list[Any]:
    from tools.importer.dispatcher import parse_file

    cache = Path(".cache/apidoc/openapi.json")
    if not cache.exists():
        return []
    return parse_file(str(cache))


def render_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "## 维度覆盖",
        "",
        f"- 项目：{summary['project']} ｜ 模块：{summary['module']}",
        f"- 接口数：{summary['endpoints']} ｜ 用例数：{summary['cases']}",
        f"- **维度覆盖：{summary['covered_dimensions']}/{summary['expected_dimensions']} "
        f"({summary['dimension_coverage']:.0%})**",
        f"- 薄用例（业务断言 < 2）：{summary['thin_cases']} 条",
        f"- 假设用例（needs_manual_setup）：{summary['assumption_cases']} 条",
        f"- 缺陷候选（探针实测服务端未校验）：{summary.get('defect_candidates', 0)} 条",
        f"- 标称未证实（标了维度但用例内容不支持）：{summary.get('claimed_unverified', 0)} 条",
        "",
        "## 按接口",
        "",
        "| 接口 | 覆盖 | 应测维度 | 缺失维度 | 用例数 |",
        "|---|---|---|---|---|",
    ]
    for item in report["endpoints"]:
        lines.append(
            f"| `{item['endpoint']}` | {item['coverage']:.0%} | "
            f"{', '.join(item['expected'])} | {', '.join(item['missing']) or '-'} | "
            f"{item['case_count']} |"
        )

    if report["thin_cases"]:
        lines += ["", "## 薄用例", "", "| 编号 | 用例 | 业务断言数 |", "|---|---|---|"]
        for item in report["thin_cases"][:20]:
            lines.append(
                f"| {item['case_id']} | {item['name']} | {item['business_asserts']} |"
            )

    if report.get("defect_candidates"):
        lines += [
            "",
            "## 缺陷候选（探针实测服务端未校验）",
            "",
            "> 这些用例按实测事实断言成功，因此不计入反例维度覆盖；",
            "> 需要确认是文档契约过期还是服务端漏校验。",
            "",
            "| 编号 | 用例 | 实测结论 |",
            "|---|---|---|",
        ]
        for item in report["defect_candidates"][:30]:
            lines.append(
                f"| {item['case_id']} | {item['name']} | {item['note'] or '-'} |"
            )

    if report.get("claimed_unverified"):
        lines += [
            "",
            "## 标称未证实",
            "",
            "> 用例标了某个维度，但请求/断言不支持该维度，不计入覆盖。",
            "",
            "| 编号 | 用例 | 维度 | 原因 |",
            "|---|---|---|---|",
        ]
        for item in report["claimed_unverified"][:30]:
            lines.append(
                f"| {item['case_id']} | {item['name']} | {item['dimension']} | {item['reason']} |"
            )

    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="维度覆盖率分析")
    parser.add_argument("--project", default="mall4j")
    parser.add_argument("--module", default="")
    parser.add_argument("--file", default=".cache/apidoc/openapi.json")
    parser.add_argument("--only-path", default="")
    parser.add_argument("--max-intents", type=int, default=8)
    parser.add_argument("--format", choices=["markdown", "json"], default="markdown")
    args = parser.parse_args(argv)

    from tools.importer.dispatcher import parse_file

    endpoints = parse_file(args.file)
    if args.only_path:
        endpoints = [ep for ep in endpoints if args.only_path in ep.path]

    report = analyze(
        project=args.project,
        module=args.module,
        endpoints=endpoints,
        max_intents=args.max_intents,
    )
    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
