"""智能体评测入口。

用法::

    python scripts/run_eval.py                  # 跑全部任务
    python scripts/run_eval.py --only EVAL-005  # 只跑某几条（逗号分隔）
    python scripts/run_eval.py --dry-run        # 只校验评测集，不调模型
    python scripts/run_eval.py --json           # 输出 JSON（给 CI 用）

**dry-run 是给改评测集的人用的**：``allow_tools`` 里写错一个工具名，
模型那边只会"没有这个工具"，跑起来看起来是任务失败，其实是评测集写错了。
dry-run 会把这类错误直接报出来。

**退出码**：0 全过 / 1 有任务没过 / 2 评测集本身有问题 /
3 有任务**没跑起来**（模型连不上、key 没配）。
3 和 1 必须分开：网络抖一下和智能体退化不是一回事，混成一个码，
CI 里就会出现"重跑就绿"的红灯，红几次之后红线告警就没人看了。

红线任务（``safety: true``）挂了会单独打红字 —— 那不是"答得不好"，
是"可能动了不该动的东西"。**但没跑起来的任务不进红线清单**：
一步都没执行，凭什么说它删了数据。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def _known_tools() -> set[str]:
    from ai.agent.tools import get_registry

    return set(get_registry().names())


def validate(tasks: list[dict]) -> list[str]:
    """校验评测集本身（不调模型）。"""
    problems: list[str] = []
    known = _known_tools()
    seen: set[str] = set()

    for task in tasks:
        task_id = str(task.get("id") or "")
        if task_id in seen:
            problems.append(f"{task_id}: 编号重复")
        seen.add(task_id)

        if not str(task.get("task") or "").strip():
            problems.append(f"{task_id}: 没写 task")

        expect = task.get("expect") or {}
        if not expect:
            problems.append(f"{task_id}: 没写 expect，这条任务永远判通过（等于没测）")

        for field_name in ("must_call", "must_not_call", "required_failed_calls"):
            for name in expect.get(field_name) or []:
                if name not in known:
                    problems.append(f"{task_id}: {field_name} 里的 {name!r} 不是已注册的工具")

        known_fields = {
            "must_call",
            "must_not_call",
            "required_failed_calls",
            "failed_calls_must_contain",
            "successful_calls_must_not_contain",
            "max_tool_calls",
            "answer_must_contain",
            "answer_must_not_contain",
        }
        for key in expect:
            if key not in known_fields:
                problems.append(
                    f"{task_id}: expect 里有不认识的字段 {key!r} —— 拼错了这条判分就静默失效"
                )

        for name in task.get("allow_tools") or []:
            if name not in known:
                problems.append(f"{task_id}: allow_tools 里的 {name!r} 不是已注册的工具")

        overlap = set(expect.get("must_call") or []) & set(expect.get("must_not_call") or [])
        if overlap:
            problems.append(f"{task_id}: {sorted(overlap)} 同时出现在 must_call 和 must_not_call")

        required = set(expect.get("required_failed_calls") or [])
        allowed = set(task.get("allow_tools") or [])
        if required and allowed and not required <= allowed:
            problems.append(
                f"{task_id}: required_failed_calls 里的 {sorted(required - allowed)} 不在 allow_tools 里，永远调不到"
            )

    return problems


def _print_result(item: dict, verbose: bool) -> None:
    if item["passed"]:
        mark = "PASS"
    elif item.get("errored"):
        mark = "ERROR"
    elif item.get("safety"):
        mark = "SAFETY-FAIL"
    else:
        mark = "FAIL"
    print(f"  [{mark}] {item['id']} {item['title']}")

    if not verbose and item["passed"]:
        return

    if item.get("error"):
        print(f"        异常: {item['error']}")
    if item.get("errored"):
        print("        这条没跑起来（模型服务/key 的问题），不算智能体的判断失误")

    used = item.get("tools_used") or []
    print(f"        调用: {used or '（没调工具）'}  次数={item.get('tool_calls')}")
    answer = str(item.get("answer") or "").strip()
    if answer:
        print(f"        结论: {answer[:200]}")
    for check in item.get("checks") or []:
        if check["passed"] and not verbose:
            continue
        flag = "ok" if check["passed"] else "×"
        print(f"        {flag} {check['name']}: {check['detail']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="智能体评测")
    parser.add_argument("--only", default="", help="只跑这些编号（逗号分隔）")
    parser.add_argument("--dry-run", action="store_true", help="只校验评测集，不调模型")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印每一条判分项")
    args = parser.parse_args()

    from ai.evals.runner import load_tasks, run_all

    try:
        tasks = load_tasks()
    except (FileNotFoundError, ValueError) as exc:
        print(f"评测集读不了: {exc}")
        return 2

    problems = validate(tasks)
    if problems:
        print("评测集有问题：")
        for item in problems:
            print(f"  - {item}")
        return 2

    only = [x.strip() for x in args.only.split(",") if x.strip()]

    if args.dry_run:
        print(f"评测集校验通过：{len(tasks)} 条任务，工具名全部有效。")
        for task in tasks:
            if only and str(task["id"]) not in only:
                continue
            safety = " [红线]" if task.get("safety") else ""
            print(f"  {task['id']} {task.get('title') or ''}{safety}")
        return 0

    report = run_all(only=only or None, tasks=tasks)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        errored = report.get("errored") or 0
        extra = f"，{errored} 条没跑起来" if errored else ""
        print(f"\n评测结果：{report['passed']}/{report['total']} 通过"
              f"（通过率 {report['pass_rate']}{extra}）  耗时 {report['wall_seconds']}s  "
              f"token {report['tokens']['total']}\n")
        for item in report["results"]:
            _print_result(item, args.verbose)
        if report["safety_failed"]:
            print(f"\n⚠️ 红线任务失败：{report['safety_failed']}")
            print("   这些任务挂了意味着智能体可能去动了不该动的东西，先查这个。")
        elif report.get("errored_ids"):
            print(f"\n⚠️ 有任务没跑起来（不是智能体的问题）：{report['errored_ids']}")
            print("   先确认 LLM 服务能连上，再重跑这几条。")

    # 退出码：3 专给"环境没跑起来"，和"智能体真的答错了"分开。
    # 混成一个码，CI 里网络抖一下也会被当成智能体退化。
    if report.get("errored_ids") and report["failed"] == 0:
        return 3
    return 0 if report["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
