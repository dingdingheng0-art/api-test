"""AI 缺陷分析 —— 失败用例的根因分析。

**做什么**：跑完测试，把失败的用例归类、给出根因和修复建议。

**两条路**：

1. **先规则，后 AI**：
   用固定规则判断明显的环境问题（网络抖动、限流、服务端 5xx、前置数据没造出来）。
   规则命中就直接给结论 —— 快、准、不花钱。
2. **规则没命中的，交给 LLM**：
   把异常详情 + 断言 + 请求响应日志喂给模型，让它分析根因。

**没配 LLM 也完全能用** —— 规则分类照样出报告，只是不会给"AI 建议"那部分。

产出：``reports/analysis/根因分析_<时间戳>.md``

用法::

    python -m ai.capabilities.diagnosis              # 分析最近一次运行的结果
    python -m ai.capabilities.diagnosis --result reports/result.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from utils.logs.log_manager import log

# ============================================================================
# 根因分类规则
# ============================================================================

# 每条规则：命中关键词 -> (分类, 说明, 该不该算"环境问题")
# 环境问题的用例建议重跑；真 bug 的要改代码
RULES: list[tuple[str, str, str, bool]] = [
    (
        r"ConnectionError|Connection refused|Connection reset|RemoteDisconnected",
        "网络连接异常",
        "服务地址不通或连接被中断。检查：被测服务是否启动、host/port 配置对不对、代理/防火墙。",
        True,
    ),
    (
        r"ReadTimeout|ConnectTimeout|Timeout|timed out",
        "请求超时",
        "服务端在规定时间内没响应。检查：接口是否变慢、有没有慢查询、测试环境负载。",
        True,
    ),
    (
        r"502|503|504|Bad Gateway|Service Unavailable",
        "服务端临时不可用",
        "网关返回 5xx，通常是服务正在重启或过载。稍后重跑可确认。",
        True,
    ),
    (
        r"访问过于频繁|1003|too many requests|429",
        "服务端限流",
        "请求太密集被服务端拦了。降低并发，或调大限流配置的 qps。",
        True,
    ),
    (
        r"验证码已失效|滑块识别失败|uuid",
        "登录态失效",
        "滑块会话过期（uuid 是一次性凭证）。重新抓 uuid 或清 token 缓存后重跑。",
        True,
    ),
    (
        r"没有拿到 uuid|没抓到新 uuid",
        "环境限流（抓 uuid 失败）",
        "服务端限制了 uuid 获取频率。等 30 秒左右再跑。",
        True,
    ),
    (
        r"setup .*失败|teardown .*失败|造数据失败",
        "前置/后置处理失败",
        "造测试数据那一步就挂了，后面的步骤根本没执行。检查工厂配置和数据库连接。",
        False,
    ),
    (
        r"数据库断言失败|SQL .*期望|没有名为.*的配置|未配置",
        "校验环境未就绪",
        "用例要用的数据库/缓存/MQ 没配置或连不上。先把校验层配置补齐。",
        False,
    ),
    (
        r"KeyError|JSONDecodeError|yaml.YAMLError|用例文件",
        "用例数据问题",
        "用例文件本身有问题（YAML 语法错、字段名写错）。改用例。",
        False,
    ),
    (
        r"AssertionError|断言",
        "断言不匹配",
        "接口返回的值和预期不一致 —— 这是真问题，要人工看是 bug 还是预期写错了。",
        False,
    ),
    (
        r"ImportError|ModuleNotFoundError|No module named",
        "依赖缺失",
        "缺少第三方包或环境没装全。装上对应的包。",
        False,
    ),
]


@dataclass
class Diagnosis:
    """一条失败用例的诊断结果。"""

    case: str = ""
    case_id: str = ""             # API-168 —— 自愈流程要靠它精确定位用例
    error_type: str = ""
    message: str = ""
    category: str = "未分类"
    reason: str = ""
    suggestion: str = ""
    is_environment: bool = False
    source: str = "规则"          # 框架归因 / 规则 / AI
    ai_analysis: str = ""
    #: 框架当场判的四类归因（服务问题 / 用例数据问题 / 脚本问题 / 环境问题）。
    #: 只有 ``source="框架归因"`` 时才有值 —— 其它来源是事后按文本归的类，
    #: 由 ``ai.agent.tools.diagnostics._attribution_of`` 再映射。
    attribution: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "case": self.case,
            "case_id": self.case_id,
            "error_type": self.error_type,
            "message": self.message,
            "category": self.category,
            "reason": self.reason,
            "suggestion": self.suggestion,
            "is_environment": self.is_environment,
            "source": self.source,
            "attribution": self.attribution,
            "ai_analysis": self.ai_analysis,
        }


@dataclass
class FailureInfo:
    """一条失败用例的原始信息。"""

    case: str
    case_id: str = ""             # API-168
    error_type: str = ""
    message: str = ""
    traceback: str = ""
    request_log: str = ""
    evidence: Any = None
    attribution: str = ""         # 框架跑用例时当场判的归因（比事后猜准）
    attribution_reason: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FailureInfo":
        return cls(
            case=str(data.get("case") or data.get("name") or "(未知用例)"),
            case_id=str(data.get("case_id") or ""),
            error_type=str(data.get("error_type") or data.get("type") or ""),
            message=str(data.get("message") or ""),
            traceback=str(data.get("traceback") or ""),
            request_log=str(data.get("request_log") or ""),
            evidence=data.get("evidence"),
            attribution=str(data.get("attribution") or ""),
            attribution_reason=str(data.get("attribution_reason") or ""),
        )


# ============================================================================
# 规则分类
# ============================================================================


def _from_framework_attribution(failure: FailureInfo) -> Diagnosis | None:
    """用框架跑用例时当场判的归因。

    **为什么它排在规则前面**

    ``utils/infra/failure_classifier.py`` 判归因时手里有**真实响应体**
    （HTTP 状态码、服务端 code、success 字段），而这里事后只能看异常文本。
    服务端返回 ``A00005`` 但断言信息里只有 ``[success] 期望 True 实际 False``
    的时候，规则只能猜"断言不匹配"，框架那边是确凿的"服务端内部异常"。

    自愈流程拿这个结论决定能不能动用例，猜错一次就可能把真实缺陷改绿 ——
    所以有确凿结论时以它为准。
    """
    attribution = str(failure.attribution or "").strip()
    from utils.infra.failure_classifier import CASE_DATA, ENVIRONMENT, SCRIPT, SERVICE

    if attribution not in (SERVICE, CASE_DATA, SCRIPT, ENVIRONMENT):
        return None

    return Diagnosis(
        case=failure.case,
        case_id=failure.case_id,
        error_type=failure.error_type,
        message=failure.message,
        category=failure.attribution_reason or attribution,
        reason=failure.attribution_reason or f"框架执行时归因为{attribution}",
        attribution=attribution,
        suggestion={
            SERVICE: "提缺陷给后端，附上请求/响应证据。**不要改用例断言把红变绿**。",
            CASE_DATA: "服务端行为和用例预期对不上。核对接口契约后改用例，必须人审 diff。",
            SCRIPT: "框架或用例文件本身有问题（变量取不到、结构写错）。改脚本/用例文件。",
            ENVIRONMENT: "没拿到响应或服务暂时不可用。重跑确认，不要改用例。",
        }.get(attribution, "人工判断"),
        is_environment=(attribution == ENVIRONMENT),
        source="框架归因",
    )


def classify_by_rules(failure: FailureInfo) -> Diagnosis | None:
    """用规则判断根因。命中就返回结果，没命中返回 None。"""
    text = f"{failure.error_type} {failure.message} {failure.traceback}"

    for pattern, category, reason, is_env in RULES:
        if re.search(pattern, text, re.IGNORECASE):
            return Diagnosis(
                case=failure.case,
                case_id=failure.case_id,
                error_type=failure.error_type,
                message=failure.message,
                category=category,
                reason=reason,
                suggestion=(
                    "这条大概率是环境问题，建议**重跑一次**确认。"
                    if is_env
                    else "这条需要人工介入（改用例或改代码）。"
                ),
                is_environment=is_env,
                source="规则",
            )

    return None


# ============================================================================
# AI 分析
# ============================================================================

SYSTEM_PROMPT = """你是一名资深测试工程师，擅长分析接口自动化测试失败的根因。

你会收到一条失败用例的详细信息。请分析它为什么失败，并给出：
1. **根因分类**（网络抖动 / 限流 / 服务端 bug / 用例数据错 / 断言预期错 / 环境未就绪 之一）
2. **根因说明**（两三句话说清楚为什么会这样）
3. **处理建议**（具体到能做什么：改哪里、重跑、还是找开发）
4. **是否属于环境问题**（是的话建议重跑；不是的话是真问题）

**只输出 JSON**，格式：
```json
{
  "category": "根因分类",
  "reason": "根因说明",
  "suggestion": "处理建议",
  "is_environment": true
}
```
"""


def analyze_with_ai(failure: FailureInfo, client: Any = None) -> Diagnosis | None:
    """用 LLM 分析一条失败用例。失败返回 None。"""
    from ai.capabilities.client import get_client

    client = client or get_client()
    if not client.available:
        return None

    prompt = _build_diagnosis_prompt(failure)

    try:
        data = client.chat_json(prompt, system=SYSTEM_PROMPT)
    except Exception as exc:  # noqa: BLE001
        log.error("AI 分析失败 %s: %s", failure.case, exc)
        return None

    if not isinstance(data, dict):
        return None

    return Diagnosis(
        case=failure.case,
        case_id=failure.case_id,
        error_type=failure.error_type,
        message=failure.message,
        category=str(data.get("category") or "未分类"),
        reason=str(data.get("reason") or ""),
        suggestion=str(data.get("suggestion") or ""),
        is_environment=bool(data.get("is_environment")),
        source="AI",
    )


def _build_diagnosis_prompt(failure: FailureInfo) -> str:
    """把失败信息拼成提示词。"""
    parts = [
        f"用例名: {failure.case}",
        f"异常类型: {failure.error_type}",
        f"异常信息: {failure.message}",
    ]
    if failure.traceback:
        parts.append(f"调用栈:\n{failure.traceback[:3000]}")
    if failure.request_log:
        parts.append(f"请求响应记录:\n{failure.request_log[:3000]}")
    if failure.evidence:
        parts.append(
            "校验证据:\n"
            + json.dumps(failure.evidence, ensure_ascii=False, indent=2, default=str)[:2000]
        )

    return "\n\n".join(parts)


# ============================================================================
# 总入口
# ============================================================================


def diagnose(
    failures: list[FailureInfo],
    use_ai: bool = True,
    ai_client: Any = None,
) -> list[Diagnosis]:
    """分析一批失败用例。

    流程：先规则分类，规则没命中的再交给 AI。
    """
    results: list[Diagnosis] = []

    for failure in failures:
        # 框架当场判的归因 > 事后看文本猜的规则 > LLM
        diagnosis = _from_framework_attribution(failure)

        if diagnosis is None:
            diagnosis = classify_by_rules(failure)

        if diagnosis is None and use_ai:
            diagnosis = analyze_with_ai(failure, ai_client)

        if diagnosis is None:
            diagnosis = Diagnosis(
                case=failure.case,
                case_id=failure.case_id,
                error_type=failure.error_type,
                message=failure.message,
                category="未分类",
                reason="规则和 AI 都没能定位原因，需要人工看。",
                suggestion="看 allure 报告里这条用例的请求响应记录和异常详情。",
                is_environment=False,
                source="规则",
            )

        results.append(diagnosis)

    return results


# ============================================================================
# 报告输出
# ============================================================================


def render_markdown(
    diagnoses: list[Diagnosis],
    total: int = 0,
    passed: int = 0,
) -> str:
    """生成 Markdown 报告。"""
    env_count = sum(1 for d in diagnoses if d.is_environment)
    bug_count = len(diagnoses) - env_count

    lines: list[str] = [
        "# 测试失败根因分析",
        "",
        f"- 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 用例总数: {total}",
        f"- 通过: {passed}",
        f"- 失败: {len(diagnoses)}",
        f"- 疑似环境问题: {env_count}",
        f"- 疑似真问题: {bug_count}",
        "",
    ]

    if not diagnoses:
        lines.append("没有失败的用例 —— 全绿。")
        return "\n".join(lines)

    # ---- 分类汇总 ----
    lines.append("## 分类汇总")
    lines.append("")
    lines.append("| 分类 | 数量 | 性质 |")
    lines.append("|---|---|---|")
    by_category: dict[str, list[Diagnosis]] = {}
    for d in diagnoses:
        by_category.setdefault(d.category, []).append(d)
    for category, items in sorted(by_category.items(), key=lambda kv: -len(kv[1])):
        nature = "环境问题" if all(i.is_environment for i in items) else "需排查"
        lines.append(f"| {category} | {len(items)} | {nature} |")
    lines.append("")

    # ---- 逐条详情 ----
    lines.append("## 逐条详情")
    lines.append("")
    for index, d in enumerate(diagnoses, 1):
        tag = "环境问题" if d.is_environment else "需排查"
        lines.append(f"### {index}. {d.case}")
        lines.append("")
        lines.append(f"- **分类**: {d.category}（{tag}，来源: {d.source}）")
        lines.append(f"- **异常类型**: `{d.error_type}`")
        lines.append(f"- **异常信息**: {d.message}")
        lines.append(f"- **根因**: {d.reason}")
        lines.append(f"- **建议**: {d.suggestion}")
        if d.ai_analysis:
            lines.append(f"- **AI 补充**: {d.ai_analysis}")
        lines.append("")

    return "\n".join(lines)


def save_report(
    diagnoses: list[Diagnosis],
    output_dir: Path | None = None,
    total: int = 0,
    passed: int = 0,
) -> Path:
    """把报告写到磁盘（``reports/analysis/`` 下）。"""
    if output_dir is None:
        from utils.paths import REPORTS_ROOT

        output_dir = REPORTS_ROOT / "analysis"

    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"根因分析_{stamp}.md"
    path.write_text(render_markdown(diagnoses, total, passed), encoding="utf-8")

    # 同时存一份 JSON（CI 好采集）
    json_path = output_dir / f"根因分析_{stamp}.json"
    json_path.write_text(
        json.dumps(
            [d.to_dict() for d in diagnoses], ensure_ascii=False, indent=2
        ),
        encoding="utf-8",
    )

    log.info("根因分析报告: %s", path)
    return path


# ============================================================================
# 从测试结果里收集失败用例
# ============================================================================


def collect_failures(
    result_path: Path | None = None,
    allure_results: Path | None = None,
) -> list[FailureInfo]:
    """收集失败用例的信息。

    优先读 allure 的结果文件（信息最全 —— 有异常栈、请求日志、附件），
    读不到再退回 ``result.json``。
    """
    failures: list[FailureInfo] = []

    if allure_results and allure_results.exists():
        failures.extend(_collect_from_allure(allure_results))

    if not failures and result_path and result_path.exists():
        failures.extend(_collect_from_result_json(result_path))

    return failures


def _collect_from_allure(results_dir: Path) -> list[FailureInfo]:
    """从 allure 结果文件里提取失败用例。"""
    failures: list[FailureInfo] = []
    for path in results_dir.glob("*-result.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue

        status = data.get("status")
        if status not in ("failed", "broken"):
            continue

        name = data.get("name") or data.get("fullName") or "(未知用例)"

        request_log = ""
        evidence: Any = None
        attribution = ""
        attribution_reason = ""

        # 附件**两层都要看**：框架在用例失败时贴的「请求响应记录」「失败归因」
        # 挂在用例上（top-level attachments），而造数/查数步骤的响应体挂在
        # 各自的 step 上。只看 steps 会漏掉最关键的那几个。
        for attachment in list(data.get("attachments") or []) + [
            a
            for step in data.get("steps") or []
            for a in (step.get("attachments") or [])
        ]:
            name_hint = str(attachment.get("name") or "")
            if "失败归因" in name_hint:
                payload = _read_attachment(results_dir, attachment)
                if isinstance(payload, dict):
                    attribution = str(payload.get("归因") or "")
                    attribution_reason = str(payload.get("具体原因") or "")
            elif "请求响应" in name_hint:
                request_log = str(_read_attachment(results_dir, attachment) or "")
            elif "校验证据" in name_hint:
                evidence = _read_attachment(results_dir, attachment)

        status_details = data.get("statusDetails") or {}
        message = str(status_details.get("message") or "")
        traceback = str(status_details.get("trace") or "")
        error_type = _extract_error_type(traceback or message)

        failures.append(
            FailureInfo(
                case=str(name),
                case_id=_case_id_of(data),
                error_type=error_type,
                message=message,
                traceback=traceback,
                request_log=request_log,
                evidence=evidence,
                attribution=attribution,
                attribution_reason=attribution_reason,
            )
        )

    return failures


def _case_id_of(data: dict[str, Any]) -> str:
    """从 allure 参数里抠出用例编号。

    框架把参数值写成 ``分页查询操作日志#API-183``（见
    ``framework/case_param.format_case_label``）—— ``#`` 后面就是编号。
    """
    for param in data.get("parameters") or []:
        if str(param.get("name") or "") != "yaml_case":
            continue
        value = str(param.get("value") or "")
        _, sep, tail = value.rpartition("#")
        if sep and tail.strip():
            return tail.strip()
    return ""


def _read_attachment(results_dir: Path, attachment: dict[str, Any]) -> Any:
    """读 allure 附件内容。"""
    source = attachment.get("source")
    if not source:
        return None
    file_path = results_dir / str(source)
    if not file_path.exists():
        return None
    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError:
        return None

    if str(attachment.get("type") or "").endswith("json"):
        try:
            return json.loads(text)
        except ValueError:
            return text
    return text


def _extract_error_type(text: str) -> str:
    """从调用栈里提取异常类型名。"""
    if not text:
        return ""
    match = re.findall(
        r"^([A-Za-z_][\w.]*(?:Error|Exception|Timeout|Failure))", text, re.MULTILINE
    )
    if match:
        return match[-1].split(".")[-1]
    return text.strip().split("\n")[0][:120]


def _collect_from_result_json(result_path: Path) -> list[FailureInfo]:
    """从 ``result.json`` 里提取（信息少，但至少有失败列表）。"""
    try:
        data = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []

    failures: list[FailureInfo] = []
    for item in data.get("failed_cases") or []:
        if isinstance(item, str):
            failures.append(FailureInfo(case=item))
        elif isinstance(item, dict):
            failures.append(FailureInfo.from_dict(item))
    return failures


# ============================================================================
# 命令行
# ============================================================================


def main(argv: list[str] | None = None) -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="AI 缺陷分析")
    parser.add_argument("--result", default=None, help="结果 JSON（默认 reports/result.json）")
    parser.add_argument("--allure", default=None, help="allure 结果目录（默认 reports/allure-results）")
    parser.add_argument(
        "--mode",
        choices=["ai", "rules"],
        default="ai",
        help="ai=规则+AI（没配 AI 就只用规则）；rules=只用规则",
    )
    parser.add_argument("--output", default=None, help="报告输出目录")
    args = parser.parse_args(argv)

    from utils.paths import ALLURE_RESULTS, REPORTS_ROOT

    result_path = Path(args.result) if args.result else REPORTS_ROOT / "result.json"
    allure_dir = Path(args.allure) if args.allure else ALLURE_RESULTS

    failures = collect_failures(result_path, allure_dir)
    if not failures:
        print("没找到失败的用例（可能上次运行全绿，或者没找到结果文件）")
        return 0

    print(f"收集到 {len(failures)} 条失败用例，开始分析...")

    diagnoses = diagnose(failures, use_ai=(args.mode == "ai"))

    output_dir = Path(args.output) if args.output else None
    path = save_report(diagnoses, output_dir)

    # 终端也打印一份摘要
    print()
    by_category: dict[str, int] = {}
    for d in diagnoses:
        by_category[d.category] = by_category.get(d.category, 0) + 1
    print("分类汇总：")
    for category, count in sorted(by_category.items(), key=lambda kv: -kv[1]):
        print(f"  {category}: {count} 条")

    print(f"\n完整报告: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
