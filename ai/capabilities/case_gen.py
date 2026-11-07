"""AI 用例生成 —— 需求文档 → 测试用例。

**两条路**：

1. **接了 LLM**（配了 ``ai.base_url`` / ``api_key`` / ``model``）：
   把接口信息喂给模型，让它生成**正例 + 反例 + 边界值**三类用例。
2. **没接 LLM**（本地模式）：
   按参数类型用模板规则造用例 —— 必填缺失、类型错误、空值、超长、
   非法枚举。**不联网也能出用例**。

**产出的东西**：标准格式的 YAML 用例，落在
``testcases/cases/<项目>/<模块>/`` 下，和手写的完全一样，
可以直接跑、可以人工改。

**AI 生成的用例要人工过一遍** —— 模型可能理解错业务。
工具会尽量标上来源（``marks: [ai_generated]``），方便筛出来看。

用法::

    python -m ai.capabilities.case_gen --file 接口文档.json --project mall4j
    python -m ai.capabilities.case_gen --file 接口文档.json --mode local   # 强制本地模式
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

from tools.importer.case_builder import build_asserts, build_step, build_testcase
from tools.importer.models import Endpoint, Param, sanitize_name
from utils.logs.log_manager import log

# 生成用例时加的标签，方便筛出来人工审阅
AI_MARK = "ai_generated"

SYSTEM_PROMPT = """你是一名资深测试工程师，擅长根据接口定义设计测试用例。

你的任务：根据给定的接口信息，生成高质量的测试用例。

要求：
1. 覆盖三类场景：
   - 正例（正常参数，校验成功返回）
   - 反例（参数缺失、类型错误、空值、非法值，校验应该被拒绝）
   - 边界值（超大/超小/临界值）
2. 断言要具体，能定位问题。优先断言 `code`、`success`、关键业务字段。
3. 用例名用中文短语，能一眼看懂测什么。
4. **只输出 JSON**，不要任何解释文字。

输出格式（严格遵守）：
```json
{
  "cases": [
    {
      "name": "用例名称（中文）",
      "description": "一句话说明这条用例测什么",
      "marks": ["api", "p1"],
      "steps": [
        {
          "id": "步骤ID",
          "description": "步骤说明",
          "path": "/接口路径",
          "method": "GET",
          "headers": {"键": "值"},
          "params": {"键": "值"},
          "data": {"键": "值"},
          "assert": [
            {"type": "equal", "field": "code", "expected": 200}
          ]
        }
      ]
    }
  ]
}
```

`assert` 支持的类型：equal、not_equal、is not None、is None、contains、
in、gt、gte、lt、lte、length、matches、approx_equal、subset、status_code。
"""


# ============================================================================
# 入口
# ============================================================================


def generate_cases(
    endpoints: list[Endpoint],
    project: str = "mall4j",
    module: str = "",
    use_ai: bool = True,
    ai_client: Any = None,
    max_cases_per_endpoint: int = 3,
) -> list[dict[str, Any]]:
    """给一批接口生成用例。

    Args:
        endpoints: 接口清单
        project: 项目名
        module: 模块名（留空就按接口自己的模块分）
        use_ai: 是否用 LLM（False 就是纯本地模板）
        ai_client: LLM 客户端（不给就自动造一个）
        max_cases_per_endpoint: 每个接口最多生成几条用例

    Returns:
        用例列表（标准格式 ``{"testcase": {...}}``）
    """
    results: list[dict[str, Any]] = []

    for ep in endpoints:
        if use_ai:
            ai_cases = _generate_with_ai([ep], project, ai_client, max_cases_per_endpoint)
            if ai_cases:
                results.extend(ai_cases)
                continue
            # LLM 失败 -> 退回本地模式
            log.warning("接口 %s 的 AI 生成失败，改用本地模板", ep.id)

        results.extend(_generate_locally([ep], project, max_cases_per_endpoint))

    return results


def _generate_with_ai(
    endpoints: list[Endpoint],
    project: str,
    ai_client: Any = None,
    max_cases_per_endpoint: int = 3,
) -> list[dict[str, Any]]:
    """用 LLM 生成用例。失败返回空列表。"""
    from ai.capabilities.client import get_client

    client = ai_client or get_client()
    if not client.available:
        log.info("AI 没配置，用本地模板生成")
        return []

    prompt = _build_prompt(endpoints, max_cases_per_endpoint)

    try:
        data = client.chat_json(prompt, system=SYSTEM_PROMPT)
    except Exception as exc:  # noqa: BLE001 - LLM 出问题就退回本地
        log.error("AI 生成用例失败: %s", exc)
        return []

    raw_cases = data.get("cases") if isinstance(data, dict) else None
    if not isinstance(raw_cases, list):
        log.error("AI 返回的格式不对（没有 cases 列表）")
        return []

    cases: list[dict[str, Any]] = []
    for item in raw_cases:
        case = _normalize_ai_case(item)
        if case:
            cases.append(case)

    return cases


def _generate_locally(
    endpoints: list[Endpoint],
    project: str,
    max_cases_per_endpoint: int = 3,
) -> list[dict[str, Any]]:
    """本地模板生成（不联网）。

    规则很简单但有用：

    - **正例**：用文档里的示例参数，断言成功
    - **必填缺失**：去掉一个必填参数，期望被拒绝
    - **类型错误**：把一个参数换成明显不对的类型
    - **空值**：必填参数给空字符串
    """
    cases: list[dict[str, Any]] = []

    for ep in endpoints:
        base = build_testcase(ep, project=project)
        base["testcase"]["marks"] = ["api", AI_MARK]

        # ---- 正例 ----
        positive = _deep_copy(base)
        positive["testcase"]["description"] = (
            f"{ep.summary or ep.display_name} —— 正常参数应成功"
        )
        cases.append(positive)

        if max_cases_per_endpoint <= 1:
            continue

        required = [p for p in (ep.body_params + ep.query) if p.required]

        # ---- 必填缺失 ----
        if required:
            missing = _deep_copy(base)
            target = required[0]
            step = missing["testcase"]["steps"][0]
            location = "params" if target in ep.query else "data"
            container = step.get(location) or {}
            container.pop(target.name, None)
            if container:
                step[location] = container
            step["description"] = f"缺少必填参数 {target.name} 应被拒绝"
            step["assert"] = [
                {"type": "not_equal", "field": "code", "expected": 200},
                {"type": "is_false", "field": "success"},
            ]
            missing["testcase"]["description"] = f"缺少必填参数 {target.name} 应被拒绝"
            missing["testcase"]["marks"] = ["api", AI_MARK, "negative"]
            cases.append(missing)

        if max_cases_per_endpoint <= 2:
            continue

        # ---- 空值 ----
        if required:
            empty = _deep_copy(base)
            target = required[0]
            step = empty["testcase"]["steps"][0]
            location = "params" if target in ep.query else "data"
            container = step.get(location) or {}
            if target.name in container:
                container[target.name] = ""
                step[location] = container
                step["description"] = f"必填参数 {target.name} 传空值应被拒绝"
                step["assert"] = [
                    {"type": "not_equal", "field": "code", "expected": 200},
                    {"type": "is_false", "field": "success"},
                ]
                empty["testcase"]["description"] = f"必填参数 {target.name} 传空值应被拒绝"
                empty["testcase"]["marks"] = ["api", AI_MARK, "negative"]
                cases.append(empty)

    return cases


# ============================================================================
# 提示词
# ============================================================================


def _build_prompt(endpoints: list[Endpoint], max_cases: int) -> str:
    """把接口信息拼成提示词。"""
    blocks: list[str] = []
    for ep in endpoints:
        info: dict[str, Any] = {
            "接口名": ep.display_name,
            "路径": ep.path,
            "方法": ep.method,
            "说明": ep.summary or ep.description,
        }
        if ep.query:
            info["Query参数"] = [
                {"名字": p.name, "类型": p.type, "必填": p.required, "说明": p.description}
                for p in ep.query
            ]
        if ep.body_params:
            info["请求体字段"] = [
                {"名字": p.name, "类型": p.type, "必填": p.required, "说明": p.description}
                for p in ep.body_params
            ]
        if ep.body:
            info["请求体示例"] = ep.body
        if ep.response_example:
            info["响应示例"] = ep.response_example

        blocks.append(json.dumps(info, ensure_ascii=False, indent=2))

    return (
        f"请为下面 {len(endpoints)} 个接口生成测试用例，"
        f"每个接口最多 {max_cases} 条。\n\n"
        + "\n\n".join(blocks)
    )


# ============================================================================
# 整理 AI 返回的用例
# ============================================================================


def _normalize_ai_case(item: Any) -> dict[str, Any] | None:
    """把模型返回的一条用例整理成框架的标准格式。"""
    if not isinstance(item, dict):
        return None

    name = str(item.get("name") or "").strip()
    steps = item.get("steps")
    if not name or not isinstance(steps, list) or not steps:
        return None

    normalized_steps: list[dict[str, Any]] = []
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        if not step.get("path"):
            continue

        normalized_steps.append(
            {
                "id": str(step.get("id") or f"step{index + 1}"),
                "description": str(step.get("description") or ""),
                "path": str(step["path"]),
                "method": str(step.get("method") or "GET").upper(),
                **({"headers": step["headers"]} if step.get("headers") else {}),
                **({"params": step["params"]} if step.get("params") else {}),
                **({"data": step["data"]} if step.get("data") is not None else {}),
                **({"assert": step["assert"]} if step.get("assert") else {}),
            }
        )

    if not normalized_steps:
        return None

    marks = item.get("marks")
    if not isinstance(marks, list):
        marks = ["api"]
    marks = [str(m) for m in marks] + [AI_MARK]

    return {
        "testcase": {
            "name": sanitize_name(name, fallback="ai_case"),
            "description": str(item.get("description") or name),
            "marks": list(dict.fromkeys(marks)),
            "steps": normalized_steps,
        }
    }


def _deep_copy(data: Any) -> Any:
    """深拷贝（生成变体用例时不改原数据）。"""
    return json.loads(json.dumps(data, ensure_ascii=False, default=str))


# ============================================================================
# 落盘
# ============================================================================


def write_cases(
    cases: list[dict[str, Any]],
    cases_root: Path,
    project: str = "mall4j",
    overwrite: bool = False,
) -> list[Path]:
    """把用例写成 YAML 文件。

    文件名用用例名（中文会被清洗成安全的名字）。
    """
    written: list[Path] = []
    project_dir = cases_root / project

    for data in cases:
        case = data["testcase"]
        module = (case.get("allure") or {}).get("feature") or "ai_generated"
        module_dir = project_dir / sanitize_name(module)
        module_dir.mkdir(parents=True, exist_ok=True)

        file_name = sanitize_name(case["name"], fallback="case")
        case_file = module_dir / f"{file_name}.yaml"
        if case_file.exists() and not overwrite:
            log.info("已存在，跳过: %s", case_file)
            continue

        with case_file.open("w", encoding="utf-8") as f:
            yaml.safe_dump(
                case,
                f,
                allow_unicode=True,
                sort_keys=False,
                default_flow_style=False,
                width=100,
            )
        written.append(case_file)
        log.info("已生成: %s", case_file)

    return written


# ============================================================================
# 命令行
# ============================================================================


def main(argv: list[str] | None = None) -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="AI 用例生成")
    parser.add_argument("--file", required=True, help="接口文档文件（OpenAPI / HAR / cURL）")
    parser.add_argument("--project", default="mall4j", help="项目名")
    parser.add_argument("--module", default="", help="模块名（不填按接口自己的模块分）")
    parser.add_argument(
        "--mode",
        choices=["ai", "local"],
        default="ai",
        help="ai=用大模型（没配置就自动退回 local）；local=纯本地模板",
    )
    parser.add_argument("--max-cases", type=int, default=3, help="每个接口最多几条用例")
    parser.add_argument("--overwrite", action="store_true", help="覆盖已存在的用例文件")
    args = parser.parse_args(argv)

    from tools.importer.dispatcher import parse_file
    from utils.paths import CASES_ROOT

    print(f"解析文档: {args.file}")
    try:
        endpoints = parse_file(args.file)
    except Exception as exc:  # noqa: BLE001
        print(f"解析失败: {exc}", file=sys.stderr)
        return 1

    print(f"解析到 {len(endpoints)} 个接口，开始生成用例（模式: {args.mode}）")

    cases = generate_cases(
        endpoints,
        project=args.project,
        module=args.module,
        use_ai=(args.mode == "ai"),
        max_cases_per_endpoint=args.max_cases,
    )

    if not cases:
        print("没生成出用例", file=sys.stderr)
        return 1

    written = write_cases(cases, CASES_ROOT, args.project, overwrite=args.overwrite)

    print(f"\n生成了 {len(written)} 条用例文件：")
    for path in written:
        print(f"  {path}")
    print(f"\n提示：AI 生成的用例建议人工审阅一遍（筛选用例标签 {AI_MARK}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
