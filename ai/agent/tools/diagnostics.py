"""诊断工具：失败归因 + 覆盖率。

**归因不是"给个说法"，是决定下一步动作**

``diagnose_failures`` 输出每条失败的三类归属，智能体据此决定：

================  ==========================================
归因              智能体该做什么
================  ==========================================
``环境问题``      重跑 / 重新登录 / 等一会。**不要改用例**
``用例数据问题``  可能是断言过时，走自愈流程（人审后改）
``服务问题``      提缺陷单。**绝对不要改用例把红变绿**
================  ==========================================

这个区分是自愈闭环的安全底线：把"服务问题"误判成"用例问题"，
就等于用改断言掩盖真实 bug —— 比不修更糟。

现有的 ``utils/failure_classifier.py`` 和 ``ai/capabilities/diagnosis.py``
已经实现了规则 + AI 两级归因，这里只做适配和结果归一。
"""

from __future__ import annotations

from typing import Any

from ai.agent.tools.registry import ToolError, ToolSpec, ok, truncate_text
from utils import paths
from utils.logs.log_manager import log

# 归因 -> 建议动作（给智能体的行动指令）
ACTION_BY_ATTRIBUTION = {
    "环境问题": "重跑该用例确认。不要修改用例内容。",
    "用例数据问题": "检查断言预期是否过时（对比 OpenAPI 快照和实际响应）。可以走自愈流程，但必须人审 diff。",
    "服务问题": "提缺陷单给后端，附上请求/响应证据。绝对不要改用例把失败改绿。",
    "脚本问题": "框架或用例文件本身有问题（YAML 结构、变量引用、路径）。改用例文件。",
}


def diagnose_failures(
    use_ai: bool = False, limit: int = 20, since_last_run: bool = True
) -> dict[str, Any]:
    """对上一次运行的失败用例做归因。"""
    from ai.capabilities.diagnosis import collect_failures, diagnose

    allure_dir = paths.ALLURE_RESULTS
    result_file = paths.REPORTS_ROOT / "result.json"

    if not allure_dir.exists() and not result_file.exists():
        raise ToolError(
            "没有可分析的结果",
            hint="先调 run_cases 跑一次。",
        )

    failures = collect_failures(
        result_path=result_file,
        allure_results=allure_dir if allure_dir.exists() else None,
    )

    # ⚠️ allure-results 是跨轮次累积的（只有 --report allure 时才清）。
    # 默认 report=none 时，上一轮、上上轮的失败文件都还躺在里面 ——
    # 不按时间过滤的话，自愈流程会把「早就修好的失败」又归因一遍，
    # 然后去改一条本来就没问题的用例。这比漏报危险得多。
    #
    # 基准取 result.json 的写盘时间：框架每跑完一轮都会重写它。
    stale_dropped = 0
    if since_last_run and result_file.exists() and allure_dir.exists():
        cutoff = result_file.stat().st_mtime
        fresh = _fresh_cases(allure_dir, cutoff)
        if fresh:
            kept = [f for f in failures if str(f.case) in fresh]
            stale_dropped = len(failures) - len(kept)
            failures = kept

    if not failures:
        return ok(
            total=0,
            diagnoses=[],
            stale_dropped=stale_dropped,
            hint="上次运行没有失败用例。"
            + (f"（已忽略 {stale_dropped} 条更早轮次的旧失败）" if stale_dropped else ""),
        )

    limit = max(1, min(int(limit or 20), 100))
    selected = failures[:limit]

    # AI 归因要花钱、要时间 —— 默认关闭，由调用方显式开启
    diagnoses = diagnose(selected, use_ai=use_ai)

    # 再叠一层「服务端返回了什么」的归因（信息更具体）
    items: list[dict[str, Any]] = []
    for diag in diagnoses:
        item = diag.to_dict()
        attribution = _attribution_of(diag)
        item["attribution"] = attribution
        item["attribution_source"] = item.get("source") or ""
        item["next_action"] = ACTION_BY_ATTRIBUTION.get(attribution, "人工判断")
        # 没有编号的失败（YAML 用例 / 老报告）自愈流程定位不到 —— 明说，
        # 免得模型拿用例名去猜，同名用例一猜就错
        if not str(item.get("case_id") or "").strip():
            item["case_id"] = ""
            item["next_action"] += "（这条没有用例编号，自愈工具定位不到，只能人工处理）"
        items.append(item)

    summary: dict[str, int] = {}
    for item in items:
        key = item["attribution"]
        summary[key] = summary.get(key, 0) + 1

    return ok(
        total=len(failures),
        analyzed=len(items),
        truncated=len(failures) > limit,
        stale_dropped=stale_dropped,
        by_attribution=summary,
        diagnoses=items,
        hint=(
            "按 attribution 分派：环境问题→重跑；服务问题→提缺陷（不要改用例）；"
            "用例数据问题→可自愈（人审 diff）。"
            + (f" 已忽略 {stale_dropped} 条更早轮次的旧失败。" if stale_dropped else "")
        ),
    )


def _fresh_cases(allure_dir: Any, cutoff: float) -> set[str]:
    """本轮（``result.json`` 写盘之后）产生的 allure 结果里有哪些用例名。"""
    import json

    names: set[str] = set()
    for path in allure_dir.glob("*-result.json"):
        try:
            if path.stat().st_mtime < cutoff:
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        name = data.get("name") or data.get("fullName")
        if name:
            names.add(str(name))
    return names


def _attribution_of(diag: Any) -> str:
    """把 ``Diagnosis`` 的结论映射到四类归因。

    ``ai/capabilities/diagnosis.py`` 的 ``is_environment`` 只分了两类，
    这里按 category 关键字再细分 —— 不重写规则，只做归类。

    框架当场判过的话（``source="框架归因"``）直接用它：那个结论是拿着
    真实响应体判的，比事后按文本猜准。自愈流程靠这个决定能不能动断言。
    """
    confirmed = str(getattr(diag, "attribution", "") or "").strip()
    if confirmed in ACTION_BY_ATTRIBUTION:
        return confirmed

    if getattr(diag, "is_environment", False):
        return "环境问题"

    category = str(getattr(diag, "category", "") or "")
    text = f"{category} {getattr(diag, 'reason', '')}"

    if any(k in text for k in ("断言", "预期", "用例数据", "参数", "用例文件")):
        return "用例数据问题"
    if any(k in text for k in ("脚本", "框架", "KeyError", "YAML", "解析")):
        return "脚本问题"
    return "服务问题"


def get_coverage(project: str = "mall4j") -> dict[str, Any]:
    """接口覆盖率 + 缺口清单。"""
    from testcases.coverage import calculate_coverage

    report = calculate_coverage(project)
    data = report.to_dict()

    # 只把「最该补的」挑出来 —— 全量列表几百条，智能体看不完
    untested = [a.to_dict() for a in report.untested_apis()][:30]
    no_assert = [a.to_dict() for a in report.no_assert_apis()][:30]

    return ok(
        project=project,
        summary=data["summary"],
        warnings=data.get("warnings") or [],
        untested_endpoints=untested,
        untested_total=len(report.untested_apis()),
        no_assert_endpoints=no_assert,
        no_assert_total=len(report.no_assert_apis()),
        hint=(
            "untested = 一条用例都没有的接口（最该补）；"
            "no_assert = 调了但没断言（等于没测）。"
        ),
    )


TOOLS = [
    ToolSpec(
        name="diagnose_failures",
        description=(
            "对上一次运行的失败用例做根因归因。返回每条失败的归因"
            "（环境问题/用例数据问题/服务问题/脚本问题）和下一步动作建议。"
            "默认只用规则（快、免费）；use_ai=true 时规则没命中的交给 LLM（慢、花钱）。"
        ),
        params_schema={
            "type": "object",
            "properties": {
                "use_ai": {
                    "type": "boolean",
                    "description": "是否用 LLM 分析规则没命中的用例（默认 false）",
                    "default": False,
                },
                "limit": {"type": "integer", "description": "最多分析多少条（默认 20）", "default": 20},
                "since_last_run": {
                    "type": "boolean",
                    "description": (
                        "只分析最近一轮跑出来的失败（默认 true）。"
                        "allure-results 跨轮次累积，关掉会连上几轮的旧失败一起归因"
                    ),
                    "default": True,
                },
            },
            "additionalProperties": False,
        },
        handler=diagnose_failures,
        side_effect="read",
        cost="llm",
        tags=("diagnosis",),
    ),
    ToolSpec(
        name="get_coverage",
        description=(
            "计算项目接口覆盖率，返回未覆盖接口清单（untested）和调了没断言的接口（no_assert）。"
            "用于决定下一步补哪些用例。"
        ),
        params_schema={
            "type": "object",
            "properties": {"project": {"type": "string", "description": "项目名，默认 mall4j"}},
            "additionalProperties": False,
        },
        handler=get_coverage,
        side_effect="read",
        tags=("diagnosis",),
    ),
]
