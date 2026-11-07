"""执行工具：跑用例、读上次运行结果。

**为什么单独一个文件**

``run_cases`` 是唯一一个 ``side_effect=execute`` 的工具 —— 它会真的
发请求、造数据、清数据。``write_case`` 那三个只改用例文件、不发请求；
其余工具都是只读的。

所以这里要额外做三件事：

1. **明确告知副作用** —— 工具描述里写清楚"会发真实请求"
2. **默认最小范围** —— 不传筛选条件就报错，不允许"什么都不传跑全量"
   （智能体很容易图省事，一条全量跑下去几十分钟还污染环境）
3. **输出结构化摘要** —— 智能体要的是"哪条挂了、为什么"，
   不是 pytest 那几十行终端输出

执行方式：**起子进程跑 ``run.py``**，不在 MCP 服务进程里调 pytest。
原因：pytest 有大量进程级全局状态（插件、Globals、连接池），
在长驻服务里反复跑会互相污染，而且一次崩了会拖死整个 MCP 服务。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from ai.agent.tools.registry import ToolError, ToolSpec, ok, truncate_text
from utils import paths
from utils.logs.log_manager import log

# 单次执行的硬上限（秒）—— 防止智能体把 MCP 服务卡死
DEFAULT_TIMEOUT = 600

#: 一次最多允许跑多少条用例。超了要显式确认。
#:
#: **为什么按条数而不是按筛选条件**
#:
#: 按条件拦不住「分多次调」：``run_cases(module="a")`` + ``(module="b")`` + …
#: 逐个模块跑一遍，总量和跑全量一模一样，每次都"有边界"。
#: 评测里模型就是这么干的 —— 先被 ``project`` 那条拦下，转头写了 15 次
#: ``module=...``，把 23 个模块全跑了一遍。
#:
#: 条数是**结果侧**的度量，拆多少次都绕不过去。
#: 单模块最多 11 条（当前 mall4j），所以 50 条足够放开正常用法。
MAX_CASES_PER_RUN = 50


def run_cases(
    project: str = "",
    module: str = "",
    mark: str = "",
    keyword: str = "",
    case_file: str = "",
    case_id: str = "",
    env: str = "",
    timeout: int = DEFAULT_TIMEOUT,
    report: str = "none",
) -> dict[str, Any]:
    """跑用例，返回结构化结果摘要。"""
    if not any([project, module, mark, keyword, case_file, case_id]):
        raise ToolError(
            "必须至少指定一个筛选条件（project / module / mark / keyword / case_file / case_id）",
            hint="不允许无筛选跑全量：几百条用例要跑很久，还会往环境里造数据。先 list_modules 挑一个模块。",
        )

    # ⚠️ 两道闸，按「条件」和「条数」各拦一次
    #
    # 第一道（条件侧）：只给 project 等于跑全量。原来只拦了"一个条件都不给"，
    # 但 ``project="mall4j"`` 是**给了一个条件**却仍然覆盖 132 条用例。
    #
    # 第二道（结果侧）：算出这次到底会跑多少条。因为第一道拦不住
    # "分多次调"—— ``module=a`` + ``module=b`` + … 每次都有边界，
    # 合起来还是全量。评测里模型被拦之后就是这么绕的。
    if project and not any([module, mark, keyword, case_file, case_id]):
        raise ToolError(
            f"只给 project={project!r} 会跑掉这个项目下的全部用例",
            hint=(
                "再加一个边界：module（如 admin_area）/ mark（如 smoke）/ "
                "case_file / case_id。真要跑整个项目请用命令行 "
                f"`python scripts/run.py --project {project}` 并说明理由。"
            ),
        )

    matched = _count_matching(project, module, mark, keyword, case_file, case_id)
    if matched > MAX_CASES_PER_RUN:
        raise ToolError(
            f"这次筛选会跑 {matched} 条用例，超过单次上限 {MAX_CASES_PER_RUN} 条",
            hint=(
                "缩小范围：指定 module，或用 mark 挑一批（list_marks 看有哪些），"
                "或者直接用 case_id 只跑要验证的那几条。"
                "分多次逐个模块跑也不允许 —— 那和跑全量没区别。"
                "确实要跑这么多请用户用命令行 `python scripts/run.py ...` 跑。"
            ),
        )

    # 第三道（累计侧）：单次闸挡不住「逐个模块跑」，这里按会话累计。
    from ai.agent.tools import run_budget

    run_budget.check(matched)

    cmd = [sys.executable, str(paths.SCRIPTS_DIR / "run.py"), "--report", report]
    if project:
        cmd += ["--project", project]
    if module:
        cmd += ["--module", module]
    if mark:
        cmd += ["-m", mark]
    if keyword:
        cmd += ["-k", keyword]
    if case_file:
        cmd += ["--case-file", case_file]
    if case_id:
        # 多个编号用逗号分隔 —— 自愈验证时「改完只重跑这一条」靠它
        cmd += ["--case-id", str(case_id)]
    if env:
        cmd += ["--env", env]

    log.info("工具 run_cases 执行: %s", " ".join(cmd))

    env_vars = dict(os.environ)
    env_vars["APITEST_MCP_RUN"] = "1"  # 让被调用的 run.py 知道自己是被智能体调起的

    # 记账放在**真正执行之前**：子进程崩了也算跑过，
    # 否则"跑了但崩了"可以无限重试，累计闸就形同虚设。
    run_budget.add(matched)

    # 先记下旧结果的时间戳 —— 子进程如果崩在收集阶段，result.json 根本不会
    # 被重写，这时读到的就是**上一次**的结果。不比对的话，智能体会把上一轮
    # 的"全部通过"当成本次结论报出去，这比报错更危险。
    result_path = paths.REPORTS_ROOT / "result.json"
    before_mtime = result_path.stat().st_mtime if result_path.exists() else None

    started_wall = time.time()
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(paths.PROJECT_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(30, int(timeout)),
            env=env_vars,
        )
    except subprocess.TimeoutExpired:
        raise ToolError(
            f"执行超过 {timeout} 秒被中止",
            hint="缩小范围（指定 module 或 case_file）再试，或者调大 timeout。",
        ) from None

    elapsed = round(time.perf_counter() - started, 1)

    after_mtime = result_path.stat().st_mtime if result_path.exists() else None
    fresh = after_mtime is not None and after_mtime != before_mtime
    result = _read_result_json() if fresh else {}

    # ⚠️ 失败清单只从**本次**的 allure 结果里取。
    #
    # allure-results 目录是跨轮次累积的（run.py 只在 --report allure 时才清），
    # 默认 report=none 时上一轮的失败文件还躺在里面。直接读会把
    # "上一次全量跑的 20 条失败"挂到"本次 1 条通过"的结果上 ——
    # 自相矛盾的数据比没有数据更糟，模型会照着旧失败编结论。
    failures = _read_failures(since=started_wall) if fresh else []

    payload = ok(
        exit_code=proc.returncode,
        elapsed_seconds=elapsed,
        command=" ".join(cmd[2:]),  # 去掉解释器路径，省点上下文
        result=result,
        failures=failures,
        console_tail=truncate_text(_tail(proc.stdout + proc.stderr, 60), 3000),
    )

    if not fresh:
        payload["hint"] = (
            "没有产出新的 result.json —— pytest 很可能在收集阶段就退出了"
            "（用例格式错 / 导入失败 / 参数不对）。看 console_tail 找具体报错。"
            "不要把上一次的结果当成本次结论。"
        )

    return payload


def _count_matching(
    project: str,
    module: str,
    mark: str,
    keyword: str,
    case_file: str,
    case_id: str,
) -> int:
    """算出这组筛选条件会跑多少条用例（跑之前先算，不要先跑了再说）。

    条件之间是 **AND**：和 ``run.py`` 的筛选语义一致。
    算不出来（用例目录读不了）时返回 0 —— 宁可放过，也不要因为
    统计失败把正常调用挡掉。
    """
    from ai.agent.tools._common import iter_cases

    try:
        if case_id:
            wanted = {c.strip() for c in str(case_id).split(",") if c.strip()}
            return sum(1 for ref, _ in iter_cases() if ref.case_id in wanted)

        pairs = iter_cases(
            project=project, module=module, mark=mark, keyword=keyword
        )
        if case_file:
            target = str(case_file).replace("\\", "/").strip()
            pairs = [p for p in pairs if p[0].file == target or p[0].file.endswith("/" + target)]
        return len(pairs)
    except Exception as exc:  # noqa: BLE001 - 统计失败不该挡住正常执行
        log.warning("统计用例条数失败（跳过条数闸）: %s", exc)
        return 0


def _tail(text: str, lines: int) -> str:
    rows = [line for line in text.splitlines() if line.strip()]
    return "\n".join(rows[-lines:])


def _read_result_json() -> dict[str, Any]:
    """读 ``reports/result.json``（框架每次跑完都会写）。"""
    path = paths.REPORTS_ROOT / "result.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("result.json 读不了: %s", exc)
        return {}


def _read_failures(since: float | None = None) -> list[dict[str, Any]]:
    """从 allure 结果里提取失败用例（比 result.json 信息全）。

    Args:
        since: 只认修改时间晚于这个时刻（``time.perf_counter`` 基准换算来的
            墙钟时间）的结果文件。``None`` = 不限（读全部）。

    只取最关键几项 —— 详细归因交给 ``diagnose_failures`` 工具，
    这里塞太多会白白占上下文。
    """
    from ai.capabilities.diagnosis import collect_failures

    results_dir = paths.ALLURE_RESULTS
    if not results_dir.exists():
        return []

    try:
        failures = collect_failures(
            result_path=paths.REPORTS_ROOT / "result.json",
            allure_results=results_dir,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("收集失败用例出错: %s", exc)
        return []

    if since is not None:
        # 用文件修改时间过滤：allure 的 *-result.json 是每条用例跑完就写的，
        # 本轮开始之前就存在的那些属于上一轮。
        fresh_names = _fresh_result_names(results_dir, since)
        failures = [f for f in failures if _is_fresh(f, fresh_names)]

    return [
        {
            "case": f.case,
            "error_type": f.error_type,
            "message": truncate_text(f.message, 300),
        }
        for f in failures[:50]
    ]


def _is_fresh(failure: Any, fresh_names: set[str]) -> bool:
    """这条失败是不是本轮跑出来的（按用例名比对）。"""
    target = str(getattr(failure, "case", "") or "")
    return bool(target) and target in fresh_names


def _fresh_result_names(results_dir: Any, since: float) -> set[str]:
    """本轮跑出来的 allure 结果文件里都有哪些用例名。

    只看 mtime，不解析内容 —— 文件多的时候解析全部 JSON 太慢，
    而 ``name`` 字段在 ``collect_failures`` 里已经拿到了。
    """
    names: set[str] = set()
    for path in results_dir.glob("*-result.json"):
        try:
            if path.stat().st_mtime < since:
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        name = data.get("name")
        if name:
            names.add(str(name))
    return names


def get_last_result() -> dict[str, Any]:
    """读上一次运行的结果（不重新跑）。"""
    result = _read_result_json()
    if not result:
        raise ToolError(
            "还没有运行结果",
            hint="先调 run_cases 跑一次，或者确认 reports/result.json 存在。",
        )

    junit = paths.REPORTS_ROOT / "junit.xml"
    return ok(
        result=result,
        failures=_read_failures(),
        allure_dir=str(paths.ALLURE_RESULTS) if paths.ALLURE_RESULTS.exists() else "",
        junit_exists=junit.exists(),
        result_file_age_seconds=round(time.time() - (paths.REPORTS_ROOT / "result.json").stat().st_mtime),
    )


def list_marks(project: str = "") -> dict[str, Any]:
    """列出所有标签及用例数（决定用哪个 -m 跑）。"""
    from ai.agent.tools._common import iter_cases

    counter: dict[str, int] = {}
    samples: dict[str, list[str]] = {}
    for ref, _ in iter_cases(project=project):
        for mark in ref.marks:
            counter[mark] = counter.get(mark, 0) + 1
            samples.setdefault(mark, [])
            if len(samples[mark]) < 3:
                samples[mark].append(ref.name)

    ordered = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    return ok(
        marks=[{"mark": m, "cases": c, "samples": samples[m]} for m, c in ordered],
        total=len(ordered),
    )


TOOLS = [
    ToolSpec(
        name="run_cases",
        description=(
            "执行接口测试用例（会发真实 HTTP 请求，可能造/清测试数据）。"
            "必须给**有边界的**筛选条件：module / mark / case_file / case_id 之一；"
            "只给 project 会被拒绝（那等于跑整个项目）。"
            "返回通过/失败统计、失败用例摘要、终端输出尾部。"
            "跑完想深入分析失败原因，接着调 diagnose_failures；"
            "只想验证单条改动用 verify_heal（失败会自动回滚）。"
        ),
        params_schema={
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "项目名，如 mall4j"},
                "module": {"type": "string", "description": "模块名，如 prod_prod"},
                "mark": {"type": "string", "description": "标签，如 smoke"},
                "keyword": {"type": "string", "description": "用例名关键字"},
                "case_file": {"type": "string", "description": "指定用例文件（相对项目根）"},
                "case_id": {
                    "type": "string",
                    "description": (
                        "指定用例编号（如 API-168，多个用逗号分隔）。"
                        "同名用例（如两条「查询地区详情」）只有它能精确挑出单条"
                    ),
                },
                "env": {"type": "string", "description": "环境名：online / pre / test"},
                "timeout": {
                    "type": "integer",
                    "description": "超时秒数（默认 600，最小 30）",
                    "default": 600,
                },
                "report": {
                    "type": "string",
                    "description": "报告类型：none（默认，最快）/ html / allure",
                    "default": "none",
                },
            },
            "additionalProperties": False,
        },
        handler=run_cases,
        side_effect="execute",
        safety="发真实请求；用例里的 setups/teardowns 会造数/清数，过 utils/safety_guard.py",
        cost="network",
        tags=("execution",),
    ),
    ToolSpec(
        name="get_last_result",
        description="读取上一次运行的统计结果和失败用例摘要（不重新执行）。",
        params_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=get_last_result,
        side_effect="read",
        tags=("execution",),
    ),
    ToolSpec(
        name="list_marks",
        description="列出所有可用标签及各自的用例数量。用它决定 run_cases 该用哪个 mark。",
        params_schema={
            "type": "object",
            "properties": {"project": {"type": "string", "description": "只看某个项目"}},
            "additionalProperties": False,
        },
        handler=list_marks,
        side_effect="read",
        tags=("case",),
    ),
]
