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
from typing import Any

from ai.agent.tools._params import safe_int
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
    run_draft: bool = False,
) -> dict[str, Any]:
    """跑用例，返回结构化结果摘要。

    Args:
        run_draft: 连 draft / disabled / broken 也一起跑。

            **必须开着**的场景：智能体刚生成的用例一律是 ``draft``
            （``create_case`` 的默认状态），默认过滤下一条都不会执行，
            ``result.total`` 会是 0 —— 看起来像"跑过了"，实际上什么都没测。
            自愈验证（``verify_heal``）必须走这条路径。
    """
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
        # 多个编号用逗号分隔 —— 自愈验证时「改完只重跑这一条」靠它。
        #
        # ``--case-id`` 是 ``action="append"``：一次只能带一个编号，
        # 逗号串整体传过去一条都匹配不上（收集阶段直接 0 条）。
        # 这里按逗号拆开，逐个拼成一个 ``--case-id``。
        for one in str(case_id).split(","):
            one = one.strip()
            if one:
                cmd += ["--case-id", one]
    if env:
        cmd += ["--env", env]
    if run_draft:
        # 不加这个，draft 用例在收集阶段就被状态过滤掉了：
        # pytest 退出码 0、total=0，调用方很容易把"一条没跑"当成"全部通过"。
        cmd += ["--run-draft"]

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

    # timeout 由客户端传，schema 层不校验类型；坏值退回默认值，
    # 不能让 int("abc") 的 ValueError 变成一次"跑用例失败"。
    timeout = max(30, safe_int(timeout, DEFAULT_TIMEOUT))

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
            timeout=timeout,
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

    # report=none / html 时 pytest 根本不写 allure 结果，上面一定是空列表。
    # 这时改读 junit.xml —— run.py 每次都会产出它，里面有每条失败的名字和
    # 错误信息。否则智能体只能看到 "failed=3" 却一条明细都拿不到，
    # 接着就会去 diagnose_failures 翻到上一轮的旧失败。
    if fresh and not failures:
        failures = _read_junit_failures(since=started_wall)

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
    elif safe_int(result.get("total"), 0) == 0:
        payload["hint"] = (
            "本次没有执行任何用例（total=0）—— 这不是「通过」。"
            "检查筛选条件或用例编号是否写错；"
            "如果目标用例还是 draft（刚生成的默认状态），"
            "加 run_draft=true 再跑一次。"
        )

    # 真正整体通过之后才记「跑通过」凭据 —— set_case_status 转 debugged 要核对它。
    # 有失败/跳过就不记：说不清是哪几条过了，宁可让调用方再跑一次。
    # result.json 是运行态文件，计数被写坏时不能把「跑完读结果」这一整段弄挂
    total_count = safe_int(result.get("total"), 0)
    failed_count = safe_int(result.get("failed"), 0)
    error_count = safe_int(result.get("error"), 0)
    skipped_count = safe_int(result.get("skipped"), 0)
    if (
        fresh
        and proc.returncode == 0
        and total_count > 0
        and not failed_count
        and not error_count
        and not skipped_count
    ):
        recorded = _record_verified(
            total_count,
            run_draft=run_draft,
            project=project,
            module=module,
            mark=mark,
            keyword=keyword,
            case_file=case_file,
            case_id=case_id,
        )
        payload["verified_recorded"] = recorded
        if not recorded:
            # 不解释一句的话，随后 set_case_status(debugged) 会被拒，
            # 而拒绝理由是「账本里没有它的通过记录」—— 明明刚跑过。
            payload["verify_hint"] = (
                "这次跑通了，但没能记下「跑通过」凭据（候选条数和实际执行条数对不上，"
                "或账本写不进去）。随后 set_case_status(debugged) 会被拒 —— "
                "用 case_id 精确指定要转正的那几条再跑一次即可。"
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

    ⚠️ 统计失败时**不能返回 0**。返回 0 等于单次闸和会话闸同时消失：
    只要用例目录里有一个读不了的文件，``matched > 50`` 和
    ``run_budget.check(0)`` 都会放过，模型就能以「统计不了」为由跑全量。
    所以这里 fail-closed —— ``case_id`` 还能按编号直接数（不依赖读取），
    其余筛选直接报错，让调用方精确指定要跑哪几条。
    """
    wanted = {c.strip() for c in str(case_id).split(",") if c.strip()}
    try:
        return len(
            _matching_cases(
                project=project,
                module=module,
                mark=mark,
                keyword=keyword,
                case_file=case_file,
                case_id=case_id,
            )
        )
    except Exception as exc:  # noqa: BLE001 - 但要 fail-closed，不能静默放行
        log.warning("统计用例条数失败: %s", exc)
        if wanted:
            # 编号是调用方自己给的，数得出来 —— 按上限记账，闸仍然有效
            return len(wanted)
        raise ToolError(
            f"无法统计这次筛选会跑多少条用例，边界校验不生效：{exc}",
            hint=(
                "改用 case_id 只跑要验证的那几条（如 case_id='API-168'），"
                "或者先调 list_cases 确认用例目录能不能正常读取。"
            ),
        ) from exc


def _matching_cases(
    project: str,
    module: str,
    mark: str,
    keyword: str,
    case_file: str,
    case_id: str,
) -> list[tuple[Any, dict[str, Any]]]:
    """这组筛选条件会跑到哪些用例（**不含状态过滤**）。

    统计条数和记录「跑通过」凭据共用这一份筛选逻辑 —— 各写一套迟早对不上，
    记账时就会把没跑的用例记成通过。

    Returns:
        ``[(CaseRef, 用例本体), ...]``，和 ``iter_cases`` 的返回一致。
    """
    from ai.agent.tools._common import iter_cases

    wanted = {c.strip() for c in str(case_id).split(",") if c.strip()}
    pairs = iter_cases(
        project=project, module=module, mark=mark, keyword=keyword, strict=True
    )
    if case_id:
        pairs = [(ref, case) for ref, case in pairs if ref.case_id in wanted]
    if case_file:
        target = str(case_file).replace("\\", "/").strip()
        pairs = [
            (ref, case)
            for ref, case in pairs
            if ref.file == target or ref.file.endswith("/" + target)
        ]
    return pairs


def _record_verified(matched: int, run_draft: bool, **filters: Any) -> int:
    """把这次跑通过的用例记进账本，供 ``set_case_status`` 核对。

    Args:
        matched: 实际执行并通过的条数（``result.total``）
        run_draft: 本次是否连 draft / disabled / broken 一起跑了

    Returns:
        写入条数。任何异常都吞掉 —— 记账失败不该把一次成功执行变成报错，
        只是这次转正会因为没有凭据而被拒（调用方再跑一次即可）。
    """
    from ai.agent.tools.verify_ledger import record
    from utils.cases.case_status import RUNNABLE_STATUSES, resolve_status

    try:
        cases = _matching_cases(**filters)
        # 按 pytest 的收集语义过滤，否则「候选条数」永远对不上「实际执行条数」：
        #
        # - 给了 case_id：pytest 按编号挑，**不看状态**（见 pytest_plugin 的
        #   only_case_ids 分支）—— 自愈验证改完的 draft 用例正靠这条路径
        # - run_draft=True：全跑，不过滤
        # - 其余情况：只跑 debugged
        if not filters.get("case_id") and not run_draft:
            cases = [
                (ref, case)
                for ref, case in cases
                if resolve_status(case) in RUNNABLE_STATUSES
            ]

        expected = _expanded_count(cases)
        if expected != matched:
            # 筛选语义和执行结果对不上，说不清哪几条真跑了 —— 宁可不记
            log.warning(
                "候选 %d 条（参数化展开后）、实际通过 %d 条，跳过记录跑通过凭据",
                expected,
                matched,
            )
            return 0
        return record(cases)
    except Exception as exc:  # noqa: BLE001
        log.warning("记录跑通过凭据失败（不影响本次结果）: %s", exc)
        return 0


def _expanded_count(cases: list[tuple[Any, dict[str, Any]]]) -> int:
    """这些用例在 pytest 里会被展开成几条。

    带「参数化数据」的用例一条会变成好几条（每个参数组一条），
    ``result.total`` 数的是展开后的条数。不展开就对不上，正常模块永远记不上账。

    展开失败时按 1 条算：真展开失败的话这次执行本身就会挂，
    根本走不到记账这一步。
    """
    from utils.cases.param_expander import expand_case

    total = 0
    for _ref, case in cases:
        try:
            total += len(expand_case(case))
        except Exception as exc:  # noqa: BLE001 - 展开不了按一条算
            log.debug("参数化展开失败，按 1 条计: %s", exc)
            total += 1
    return total


def _tail(text: str, lines: int) -> str:
    rows = [line for line in text.splitlines() if line.strip()]
    return "\n".join(rows[-lines:])


def _read_result_json() -> dict[str, Any]:
    """读 ``reports/result.json``（框架每次跑完都会写）。"""
    path = paths.REPORTS_ROOT / "result.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("result.json 读不了: %s", exc)
        return {}
    # 合法 JSON 不等于合法结果：被写成 ``[]`` / ``124`` 时下面到处 ``.get``，
    # 一抛 TypeError 整个 run_cases 就挂了 —— 宁可当成「没有结果」。
    return data if isinstance(data, dict) else {}


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
        if not isinstance(data, dict):
            continue
        name = data.get("name")
        if name:
            names.add(str(name))
    return names


def _read_junit_failures(since: float | None = None) -> list[dict[str, Any]]:
    """从 ``reports/junit.xml`` 提取失败用例（report=none 时的兜底）。

    Args:
        since: 只认这个时刻之后写的 junit.xml（防止读到上一轮的）。

    Returns:
        ``[{"case": 用例名, "error_type": ..., "message": ...}, ...]``
    """
    import xml.etree.ElementTree as ET

    path = paths.REPORTS_ROOT / "junit.xml"
    if not path.exists():
        return []

    if since is not None:
        try:
            if path.stat().st_mtime < since:
                return []
        except OSError:
            return []

    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        log.warning("junit.xml 解析失败: %s", exc)
        return []

    failures: list[dict[str, Any]] = []
    for case in root.iter("testcase"):
        problem = case.find("failure")
        if problem is None:
            problem = case.find("error")
        if problem is None:
            continue

        name = case.get("name") or ""
        # pytest 的 junit 里用例名形如 "test_api_case[用例名-...#ID]"
        if name.startswith("test_api_case[") and name.endswith("]"):
            name = name[len("test_api_case["):-1]
        error_type = problem.get("type") or problem.tag
        message = problem.get("message") or (problem.text or "").strip()
        failures.append(
            {
                "case": name,
                "error_type": error_type,
                "message": truncate_text(message, 300),
            }
        )

    return failures[:50]


def get_last_result() -> dict[str, Any]:
    """读上一次运行的结果（不重新跑）。"""
    result = _read_result_json()
    if not result:
        raise ToolError(
            "还没有运行结果",
            hint="先调 run_cases 跑一次，或者确认 reports/result.json 存在。",
        )

    junit = paths.REPORTS_ROOT / "junit.xml"
    # 结果文件可能在两次读之间被清掉（并发跑 / 报告清理插件）——
    # ``stat()`` 抛 FileNotFoundError 会让「看一眼上次结果」这种只读操作报错。
    try:
        age = round(time.time() - (paths.REPORTS_ROOT / "result.json").stat().st_mtime)
    except OSError:
        age = None
    return ok(
        result=result,
        failures=_read_failures(),
        allure_dir=str(paths.ALLURE_RESULTS) if paths.ALLURE_RESULTS.exists() else "",
        junit_exists=junit.exists(),
        result_file_age_seconds=age,
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
                "run_draft": {
                    "type": "boolean",
                    "description": (
                        "连 draft / disabled / broken 也一起跑（默认 false）。"
                        "验证**刚生成**或**改完还没转正**的用例时必须开 —— "
                        "否则一条都不会执行，total=0 不代表通过"
                    ),
                    "default": False,
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
