"""pytest 钩子（hook）层。

负责框架和 pytest 之间的衔接：

- ``pytest_configure``      —— 初始化环境、准备报告目录
- ``pytest_collection``     —— 统计收集到的用例数
- ``pytest_runtest_setup``  —— 标记用例开始、准备日志记录器
- ``pytest_runtest_call``   —— 记录用例结果
- ``pytest_runtest_teardown``—— 用例结束清理
- ``pytest_terminal_summary``—— 打印汇总、写结果文件
- ``pytest_sessionfinish``  —— 收尾（生成报告）

这些钩子让"框架跑起来"这件事对用户是无感的 —— 用户只管写 YAML。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from utils.infra.globals import Globals
from utils.logs.log_manager import RequestLogger, get_logger, log

logger = get_logger(__name__)

# 每条用例的运行时状态（用 nodeid 做 key）
_RUN_STATE: dict[str, dict[str, Any]] = {}
# 本次 session 的整体统计
_SESSION_STATE: dict[str, Any] = {
    "started_at": None,
    "cases": [],
    "env": None,
}
# 结果文件写过没有（terminal_summary 先写，sessionfinish 兜底）
_RESULT_WRITTEN = False


def pytest_configure(config: pytest.Config) -> None:
    """session 开始：确定环境、准备输出目录。"""
    from utils.config.config_manager import ConfigManager

    global _RESULT_WRITTEN
    _RESULT_WRITTEN = False
    # 同一进程里连续跑两次 pytest（run_ci 会）时，
    # 上一轮的状态不能串到这一轮 —— 否则第 2 次会误判成"接口 session"、
    # 沿用上一轮的收集数，result.json 就写错了。
    Globals.set("test_results", None)
    _RUN_STATE.clear()
    _SESSION_STATE["cases"] = []
    _SESSION_STATE["collected"] = 0
    _SESSION_STATE["interface_cases"] = 0
    _SESSION_STATE["targets_interface"] = _targets_interface_tests(config)

    # 清掉上一轮遗留的归因记录 —— 汇总时不该看到上次的失败
    #
    # ⚠️ 只在主进程清 —— 并行时 worker 也会跑这个钩子，
    #    worker 一清就把别的 worker 刚写的记录抹掉了。
    try:
        from utils.infra.failure_classifier import clear_failures
        from utils.infra.parallel import is_parallel

        if not is_parallel():
            clear_failures()
    except Exception:  # noqa: BLE001 - 清缓存失败不该挡住测试
        pass

    # --apienv 选项由 pytest_plugin 注册，加载顺序不保证，所以做个兜底
    try:
        env = config.getoption("--apienv", default=None)
    except ValueError:
        env = None
    env = env or ConfigManager.get_env()

    config._apienv = env  # type: ignore[attr-defined]
    _SESSION_STATE["env"] = env
    _SESSION_STATE["started_at"] = time.time()

    # 报告目录
    from framework.report_generator import REPORTS_ROOT

    REPORTS_ROOT.mkdir(parents=True, exist_ok=True)

    log.info("接口自动化测试启动 | 环境: %s | 报告目录: %s", env, REPORTS_ROOT)


def pytest_collection_finish(session: pytest.Session) -> None:
    """收集完成：记一下一共收了多少条用例。"""
    count = len(session.items)
    _SESSION_STATE["collected"] = count
    # 汇总里的 ``total`` 取的就是这个列表的长度。
    # 之前只声明了 ``cases: []`` 却从来没往里写过东西 —— total 永远是 0。
    _SESSION_STATE["cases"] = [item.nodeid for item in session.items]

    # 这一轮到底是不是「接口用例」的 session？
    #
    # 根 conftest 里加载了框架插件，所以 ``pytest unit_tests`` 也会走到这些钩子。
    # 不区分的话，跑单元测试会把 reports/result.json 覆盖成单测结果。
    interface_cases = 0
    for item in session.items:
        callspec = getattr(item, "callspec", None)
        if callspec is not None and "yaml_case" in getattr(callspec, "params", {}):
            interface_cases += 1
    _SESSION_STATE["interface_cases"] = interface_cases

    _start_dependency_session(session)

    log.info("共收集到 %s 条用例", count)


def _start_dependency_session(session: pytest.Session) -> None:
    """告诉依赖缓存"这一轮跑哪些用例"，并清掉过期结果。

    **为什么非做不可**

    依赖结果是**落盘**的，上一轮留下的文件这一轮照样读得到。用例里写的是
    ``DELETE /admin/area/{{查数据...}}`` 这种"删自己刚造的数据"，如果读到
    上一轮的结果，删掉的就是上一轮那条 —— 或者更糟，那个 id 已经被别人用掉了。

    记下本轮用例名单之后，下游用例就能分清两种情况：

    - 依赖用例**在本轮计划里**，但还没跑到 → 等它（并行时的正常情况）
    - 依赖用例**不在本轮计划里**（状态被过滤、模块没选）→ 立刻报错，
      别拿旧结果凑合

    ⚠️ 并行时只有主进程做清理 —— worker 一清就会把别的 worker
    刚写的结果抹掉（和 ``clear_failures`` 是同一个道理）。
    """
    try:
        from utils.cases.case_dependency import begin_session
        from utils.infra.parallel import is_parallel
    except Exception:  # noqa: BLE001
        return

    names: list[str] = []
    for item in session.items:
        callspec = getattr(item, "callspec", None)
        params = getattr(callspec, "params", None) or {}
        payload = params.get("yaml_case")
        if not isinstance(payload, dict):
            continue
        case = (payload.get("case") or {}).get("testcase") or {}
        name = str(case.get("name") or "").strip()
        if name:
            names.append(name)

    # worker 只登记名单不删文件（删文件只由主进程做）
    removed = begin_session(names, prune=not is_parallel())
    if removed:
        log.info("清理过期依赖结果 %s 个", removed)


def pytest_runtest_setup(item: pytest.Item) -> None:
    """用例开始前：准备独立的请求记录器。"""
    _RUN_STATE[item.nodeid] = {
        "start": time.perf_counter(),
        "logger": RequestLogger(),
        "name": item.name,
    }

    # 清掉上一条用例留下的 HTTP 状态码。
    #
    # 重试插件靠它判断"失败是不是服务端 5xx"：上一条用例恰好返回过 500，
    # 这条用例的断言失败就会被误判成环境抖动、白白重试两次。
    try:
        from utils.http.request_handler import RequestHandler

        RequestHandler.reset_last_status()
    except Exception:  # noqa: BLE001 - 清状态失败不该挡住用例
        pass


def pytest_runtest_call(item: pytest.Item) -> None:
    """用例执行中（hook 里什么都不做，主要是留个扩展点）。"""
    return None


def pytest_runtest_teardown(item: pytest.Item) -> None:
    """用例结束后：记录耗时。

    请求 / 响应日志由 CaseRunner 自己持有（每条用例一个记录器），
    失败时由 test_cases.py 贴进 allure 报告。
    """
    state = _RUN_STATE.get(item.nodeid)
    if state is None:
        return

    state["elapsed"] = (time.perf_counter() - state["start"]) * 1000


def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    """每个阶段（setup/call/teardown）结束时记录结果。"""
    report = call.when == "call" and call.excinfo is None
    state = _RUN_STATE.get(item.nodeid)
    if state is None:
        return

    entry = state.setdefault("report", {})
    if call.when == "setup" and call.excinfo is not None:
        entry["status"] = "error"
        entry["error"] = str(call.excinfo.value)
    elif call.when == "call":
        if call.excinfo is not None:
            entry["status"] = "failed"
            entry["error"] = str(call.excinfo.value)
        else:
            entry["status"] = "passed"
    elif call.when == "teardown" and call.excinfo is not None:
        entry.setdefault("status", "passed")
        entry["teardown_error"] = str(call.excinfo.value)


def pytest_terminal_summary(terminalreporter, exitstatus, config) -> None:
    """终端汇总：打印统计 + 把结果写进全局变量并落盘（给 run.py / CI 用）。

    **为什么要在这里落盘**

    pytest 内置的 terminal 插件是在 ``pytest_sessionfinish`` 的 wrapper
    ``yield`` 之后才调用 ``pytest_terminal_summary`` 的，所以
    ``pytest_sessionfinish`` 里读 ``Globals["test_results"]`` 永远是空 ——
    ``reports/result.json`` 以前一次都没被写出来过。结果是在这里算、
    在这里写，``pytest_sessionfinish`` 只留作兜底。
    """
    # 只汇总「接口用例」的 session —— 根 conftest 也会把插件带进
    # ``pytest unit_tests``，那些不该覆盖 result.json，
    # 也不该在它们的输出里打「接口自动化测试结果」。
    if not _is_interface_session(terminalreporter):
        return

    stats = terminalreporter.stats
    total = len(_SESSION_STATE.get("cases", []))

    def _count(key: str) -> int:
        return len([i for i in stats.get(key, []) if i.when != "teardown"])

    passed = _count("passed")
    failed = _count("failed")
    error = _count("error")
    skipped = _count("skipped")

    # 并行跑的时候主进程没有 session.items（收集在 worker 里做），
    # 所以 cases 为空时退回用 pytest 的统计口径求和。
    if not total:
        total = passed + failed + error + skipped

    started = _SESSION_STATE.get("started_at") or time.time()
    duration = time.time() - started

    executed = passed + failed + error + skipped

    conclusion = "执行通过"
    if executed == 0:
        # 一条都没跑（比如 --mark 写错、状态全被过滤、只做了 --collect-only）
        # 不能被说成"通过"
        conclusion = "未执行任何用例"
    elif failed and error:
        conclusion = "执行失败，包含失败用例和错误用例"
    elif failed:
        conclusion = "执行失败，包含失败用例"
    elif error:
        conclusion = "执行失败，包含错误用例"
    elif skipped and not passed:
        conclusion = "全部跳过"

    results = {
        "conclusion": conclusion,
        "total": total,
        "passed": passed,
        "failed": failed,
        "error": error,
        "skipped": skipped,
        "duration": duration,
        "env": _SESSION_STATE.get("env"),
    }
    Globals.set("test_results", results)

    terminalreporter.write_sep("=", "接口自动化测试结果")
    terminalreporter.write_line(
        f"结论: {conclusion} | 环境: {_SESSION_STATE.get('env')} | "
        f"通过 {passed} / 失败 {failed} / 错误 {error} / 跳过 {skipped} "
        f"| 耗时 {duration:.1f}s"
    )

    _print_failure_breakdown(terminalreporter)

    _write_result_file(results)


def _print_failure_breakdown(terminalreporter) -> None:
    """终端里按模块列出失败，并标注每条是「谁的锅」。

    **为什么要这段**

    失败列表最烦的是"看不出该找谁"。这里直接按模块分组，
    每组标清楚是服务端问题、用例数据问题还是脚本问题 ——
    看完这一段就知道该把哪些用例甩给后端、哪些自己改。
    """
    from utils.infra.failure_classifier import summarize_by_module

    rows = summarize_by_module()
    if not rows:
        return

    terminalreporter.write_sep("-", "失败用例归因（按模块）")

    for module, count, breakdown in rows:
        parts = "，".join(f"{key} {value} 条" for key, value in sorted(breakdown.items()))
        terminalreporter.write_line(f"  {module}: {count} 条 —— {parts}")

    # 归因总计
    totals: dict[str, int] = {}
    for _module, _count, breakdown in rows:
        for key, value in breakdown.items():
            totals[key] = totals.get(key, 0) + value
    summary = "，".join(f"{key} {value} 条" for key, value in sorted(totals.items()))
    terminalreporter.write_line(f"  合计: {summary}")


def _targets_interface_tests(config: pytest.Config) -> bool:
    """命令行指定的收集目标里，有没有接口用例层（``testcases/test_cases.py``）。

    这条判断专门用来兜住"一条都没收集到"的情况：
    ``pytest unit_tests`` 空跑时不该覆盖 ``reports/result.json``，
    而 ``pytest . -m smoke`` 空跑时必须写一份"未执行任何用例"。
    """
    args = list(getattr(config, "invocation_params", None).args or [])
    if not args:
        return True  # 没指定目标 = 用 pytest.ini 的默认值，按接口用例算

    for raw in args:
        arg = str(raw).strip()
        if arg.startswith("-"):
            continue
        try:
            path = Path(arg).resolve()
        except (OSError, ValueError):
            continue
        # 目标是项目根 / testcases / test_cases.py 时都算接口用例
        if path == Path(config.rootpath).resolve():
            return True
        if path.name == "test_cases.py" or path.name == "testcases":
            return True
    return False


def _is_interface_session(terminalreporter) -> bool:
    """这一轮是不是在跑接口用例（而不是单元测试 / 自检）。

    串行时收集阶段能直接数出来；并行时主进程没有 ``session.items``，
    退而用 pytest 汇总结果里的 nodeid 判断（都是 ``test_api_case[...]``）。

    一条用例都没收集到时**仍然算**接口 session —— 那正是
    ``-m smoke`` 写错标签的场景，要让它以"未执行任何用例"暴露出来，
    而不是静默什么都不打印。
    """
    if _SESSION_STATE.get("interface_cases"):
        return True
    if not _SESSION_STATE.get("collected"):
        return bool(_SESSION_STATE.get("targets_interface", True))

    for reports in terminalreporter.stats.values():
        for report in reports:
            nodeid = getattr(report, "nodeid", "") or ""
            if "test_api_case" in nodeid:
                return True
    return False


def _write_result_file(results: dict[str, Any]) -> None:
    """把结果写到 ``reports/result.json``（CI 和调度器都读这个文件）。"""
    global _RESULT_WRITTEN

    import json

    from framework.report_generator import REPORTS_ROOT

    REPORTS_ROOT.mkdir(parents=True, exist_ok=True)
    out = REPORTS_ROOT / "result.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    _RESULT_WRITTEN = True
    log.info("结果已写入 %s", out)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """session 结束：合并并行结果 + 结果文件兜底。

    正常情况下 ``pytest_terminal_summary`` 已经把 result.json 写好了
    （那时才算得出通过/失败数）。这里只在一种情况下补写：
    pytest 因为参数错误等原因**没走到 terminal_summary**（比如
    ``--no-summary`` 或退出码不在 terminal 插件认的范围内）。
    """
    _merge_parallel_results()

    if _RESULT_WRITTEN:
        return

    results = Globals.get("test_results")
    if not results:
        return

    _write_result_file(results)


def _merge_parallel_results() -> None:
    """并行跑完之后合并各 worker 的结果。

    只有主进程（不是 worker）需要做这件事。
    """
    try:
        from utils.infra.parallel import is_parallel, worker_id
    except Exception:  # noqa: BLE001
        return

    if is_parallel():
        # worker 进程不合并 —— 合并由主进程做
        return

    try:
        from framework.report_generator import merge_worker_results

        merge_worker_results()
    except Exception as exc:  # noqa: BLE001 - 合并失败不该影响测试结果
        log.warning("合并并行结果失败（已忽略）: %s", exc)
