"""按质量闸门盘点**存量**用例（只读）。

**为什么需要**

``ai/rules/validator.py`` 的闸门只拦三个写入口（新建 / 改断言 / 转 debugged）。
闸门上线前就已经落盘、甚至已经 debugged 的用例不会被回溯检查 ——
薄断言、写操作无清理、路径里留着 ``{id}`` 的用例照样在 CI 里跑。

这个脚本把现存用例逐条跑一遍 ``check_case``，输出：

- 每个规则命中多少条（按严重度排）
- 具体是哪些用例（``case_id`` + 模块 + 规则）
- ``--json`` 给 CI / 其它工具消费

**只读**：不改任何文件。要修哪条，用 ``apply_assertion_change`` /
``set_case_status`` 走正常写入口（会自动再过一次闸）。

用法::

    python tools/maint/audit_quality.py
    python tools/maint/audit_quality.py --project mall4j --status debugged
    python tools/maint/audit_quality.py --weak-only --json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def audit(
    rows: list[tuple[Any, dict[str, Any]]],
) -> list[dict[str, Any]]:
    """对 ``[(CaseRef, 用例), ...]`` 逐条跑闸，返回有问题的用例。"""
    from ai.rules.validator import check_case

    findings: list[dict[str, Any]] = []
    for ref, case in rows:
        gate = check_case(
            case_name=str(case.get("name") or ref.name or ""),
            api_name=str(case.get("api_name") or ""),
            steps=case.get("steps"),
            setups=case.get("setups"),
            teardowns=case.get("teardowns"),
            marks=case.get("marks"),
        )
        if gate.violations:
            findings.append(
                {
                    "case_id": str(getattr(ref, "case_id", "") or ""),
                    "name": str(case.get("name") or getattr(ref, "name", "") or ""),
                    "module": str(getattr(ref, "module", "") or ""),
                    "file": str(getattr(ref, "file", "") or ""),
                    "status": str(getattr(ref, "status", "") or ""),
                    "rules": [item.rule for item in gate.violations],
                    "messages": gate.messages(),
                }
            )
    return findings


def summarize(findings: list[dict[str, Any]]) -> dict[str, int]:
    """按规则汇总（一条用例命中同一规则只算一次）。"""
    counter: Counter[str] = Counter()
    for item in findings:
        for rule in dict.fromkeys(item["rules"]):
            counter[rule] += 1
    return dict(counter.most_common())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按质量闸门盘点存量用例（只读）")
    parser.add_argument("--project", default="", help="只查某个项目，如 mall4j")
    parser.add_argument("--module", default="", help="只查某个模块，如 admin_area")
    parser.add_argument(
        "--status",
        default="",
        help="只查某个状态（debugged / draft / broken / disabled）",
    )
    parser.add_argument(
        "--weak-only", action="store_true", help="只列出有问题的用例，不打印汇总"
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON（给 CI 用）")
    parser.add_argument(
        "--fail", action="store_true", help="有违规时返回非 0（CI 用）"
    )
    args = parser.parse_args(argv)

    from ai.agent.tools._common import iter_cases

    rows = iter_cases(
        project=args.project,
        module=args.module,
        status=args.status,
        strict=True,
    )
    findings = audit(rows)
    summary = summarize(findings)

    if args.json:
        print(
            json.dumps(
                {
                    "total_cases": len(rows),
                    "violating_cases": len(findings),
                    "by_rule": summary,
                    "findings": findings,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        if not args.weak_only:
            print(f"扫描用例：{len(rows)} 条；不达标：{len(findings)} 条")
            if summary:
                print("\n按规则统计：")
                for rule, count in summary.items():
                    print(f"  {rule:<28} {count} 条")
            print()
        for item in findings:
            label = item["case_id"] or item["name"]
            print(f"- {label} [{item['module']}] {' / '.join(item['rules'])}")
            if not args.weak_only:
                for message in item["messages"]:
                    print(f"    · {message}")
        if not findings:
            print("没有发现不达标的用例。")

    if args.fail and findings:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
