"""pytest 钩子（hook）层。

负责框架和 pytest 之间的衔接：

- ``pytest_configure``      —— 初始化环境、准备报告目录
- ``pytest_collection_finish``—— 统计收集到的用例数
- ``pytest_runtest_setup``  —— 清掉上一条用例留下的 HTTP 状态
- ``pytest_fixture_setup``  —— 从 Allure 的 Tear down 里去掉 pytest 内部收尾
- ``pytest_terminal_summary``—— 打印汇总、写结果文件
- ``pytest_sessionfinish``  —— 收尾（生成报告）

这些钩子让"框架跑起来"这件事对用户是无感的 —— 用户只管写 YAML。
"""
from __future__ import annotations

import functools
import time
from typing import Any

import pytest

from framework.session_scope import targets_interface_tests
from utils.infra.globals import Globals
from utils.logs.log_manager import get_logger, log

logger = get_logger(__name__)

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
    _SESSION_STATE["cases"] = []
    _SESSION_STATE["collected"] = 0
    _SESSION_STATE["interface_cases"] = 0
    _SESSION_STATE["targets_interface"] = targets_interface_tests(config)

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

    # 跑单元测试时别打这句 —— 那会让 ``pytest unit_tests`` 的输出
    # 看起来像在跑接口用例。
    if _SESSION_STATE["targets_interface"]:
        log.info("接口自动化测试启动 | 环境: %s | 报告目录: %s", env, REPORTS_ROOT)


def pytest_collection_finish(session: pytest.Session) -> None:
    """收集完成：记一下一共收了多少条用例。"""
    # 一条用例都没匹配到：必须按 pytest 的「没有收集到用例」退出（5）。
    #
    # 默认行为是 pytest 把空参数化当成一条 ``test_api_case[NOTSET]``
    # 并标成 skipped —— 退出码 0、result.json 写"全部跳过"，自愈流程
    # （verify_heal）会据此认定"修好了"，而实际上一条用例都没跑。
    #
    # 放在这里抛（而不是 pytest_generate_tests 里）：收集钩子里的异常
    # 会被包装成 collection error（退出码 2 + 堆栈），这里抛
    # ``pytest.exit`` 才是干净的退出。
    from framework.pytest_plugin import NO_CASES_REASON

    no_cases = session.config.stash.get(NO_CASES_REASON, "")
    if no_cases:
        # 别把 [NOTSET] 占位项算进 total —— 这一轮就是 0 条
        _SESSION_STATE["cases"] = []
        _SESSION_STATE["collected"] = 0
        # 0 条也仍算接口 session：result.json 要写出"未执行任何用例"
        _SESSION_STATE["interface_cases"] = 1
        # ``empty_parameter_set_mark=fail_at_collect`` 会把空参数化记成一条
        # collection error。这和「0 条用例」是同一件事，不清掉的话
        # result.json 会写成 "error 1"，自愈流程还会把它当成一次失败轮次。
        reporter = session.config.pluginmanager.getplugin("terminalreporter")
        if reporter is not None:
            reporter.stats.pop("error", None)
        log.error(no_cases)
        raise pytest.exit(
            no_cases, returncode=int(pytest.ExitCode.NO_TESTS_COLLECTED)
        )

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
    """用例开始前：清掉上一条用例留下的 HTTP 状态码。"""
    # 清掉上一条用例留下的 HTTP 状态码。
    #
    # 重试插件靠它判断"失败是不是服务端 5xx"：上一条用例恰好返回过 500，
    # 这条用例的断言失败就会被误判成环境抖动、白白重试两次。
    try:
        from utils.http.request_handler import RequestHandler

        RequestHandler.reset_last_status()
    except Exception:  # noqa: BLE001 - 清状态失败不该挡住用例
        pass


# ---------------- Allure：去掉 fixture 收尾里的 pytest 内部节点 ----------------
#
# pytest 会给**每一个** fixture 自动挂两类内部 finalizer：
#
#   1. 一个 ``<lambda>`` —— 只负责通知 ``pytest_fixture_post_finalizer``；
#   2. 一个 ``functools.partial(FixtureDef.finish)`` —— 依赖关系簿记。
#
# allure-pytest 在 ``pytest_fixture_setup`` 里把 ``_finalizers`` 的每一项
# 都包成 Tear down 节点（名字是 ``fixture::finalizer名``）。于是报告上会
# 多出一排 ``case_lifecycle::<lambda>`` / ``case_runner::<lambda>`` 的粉色
# Unknown —— 它们的 start 事件写进去了，但 lambda 执行时 fixture 容器
# 已经被 post-finalizer 关掉，stop 事件写不进去，状态永远是 Unknown。
#
# 这些节点不是用例的后置清理，失败也说明不了任何问题，纯噪音。这里在
# allure 包完之后把包装还原成原始 finalizer：
#   - 内部 lambda 直接还原（仍然会执行，只是不再生成报告节点）；
#   - yield fixture 的真实收尾（yield 之后的代码）保留，但把默认的数字
#     下标改成可读的 ``fixture::teardown``。


def _wrap_target(item: Any) -> Any:
    """取出 allure 包装器里的原始 finalizer；不是包装器就返回 None。"""
    return getattr(item, "_fixture_function", None)


# Allure 的 Set up 里不展示的框架内部 fixture。
#
# 它们只是依赖注入 / 对象构造（返回配置、环境名、执行器），内部没有
# ``allure.step``，所以在报告里永远是"绿色 0s、无步骤"的空节点 ——
# 看起来像"有步骤没执行"，实际什么信息都没有。
#
# 只隐藏 **setup 成功且没有子步骤** 的节点：
#   - setup 失败时异常会跳过钩子的后半段，节点照常保留（不会藏故障）；
#   - 万一以后有人在这些 fixture 里加了 allure.step，也会保留。
_ALLURE_HIDE_FIXTURES = frozenset({"pytestconfig", "apienv", "case_runner"})


def _drop_before_fixture(fixturedef: Any, request: pytest.FixtureRequest) -> None:
    """把刚写进 Allure 容器的空 before-fixture 节点摘掉。"""
    if fixturedef.argname not in _ALLURE_HIDE_FIXTURES:
        return

    listener = request.config.pluginmanager.getplugin("allure_listener")
    reporter = getattr(listener, "allure_logger", None)
    cache = getattr(listener, "_cache", None)
    if reporter is None or cache is None:
        return

    try:
        container_uuid = cache.get(fixturedef)
        container = reporter.get_item(container_uuid) if container_uuid else None
        befores = getattr(container, "befores", None)
        if not befores:
            return

        name = getattr(fixturedef.func, "__allure_display_name__", fixturedef.argname)
        for index in range(len(befores) - 1, -1, -1):
            if getattr(befores[index], "name", None) != name:
                continue
            # 有子步骤说明确实做了可展示的事，保留
            if getattr(befores[index], "steps", None):
                return
            del befores[index]
            return
    except Exception:  # noqa: BLE001 - 报告美化失败不能影响用例
        pass


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_fixture_setup(fixturedef: Any, request: pytest.FixtureRequest):
    """fixture setup 结束后，清理 Allure 生成的内部节点。

    ``tryfirst=True`` 保证本包装器最先进入、最后退出 —— 退出时
    allure 的包装和 before-fixture 写入都已完成（它也是 hookwrapper）。

    注意：fixture setup 失败时 pytest 会把异常 throw 进这里的 ``yield``，
    后半段不会执行 —— 这正是我们要的：失败节点保留，故障不被藏掉。
    """
    yield

    _drop_before_fixture(fixturedef, request)

    finalizers = getattr(fixturedef, "_finalizers", None)
    if not finalizers:
        return

    for index, item in enumerate(finalizers):
        inner = _wrap_target(item)
        if inner is None:
            continue

        # pytest 的 post-finalizer 通知，不是用户写的后置清理
        if getattr(inner, "__name__", None) == "<lambda>":
            finalizers[index] = inner
            continue

        # yield fixture 的真实收尾：保留，但给个能看懂的名字
        if isinstance(inner, functools.partial) and getattr(
            inner.func, "__name__", ""
        ) == "_teardown_yield_fixture":
            item._name = f"{fixturedef.argname}::teardown"


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    """把每个阶段的 report 挂到 item 上（``rep_setup`` / ``rep_call`` / ``rep_teardown``）。

    fixture 的 finalizer 需要知道用例体到底过没过：
    "用例通过、清理失败" 和 "用例本来就失败" 要区别对待
    （前者记 error，后者保留原始失败原因）。
    """
    report = yield
    setattr(item, f"rep_{report.when}", report)
    return report


#: 同一个测试项可能产出多条 report（setup / call / teardown 各一条）。
#: 汇总时按最严重的状态算一条；顺序决定优先级。
_STATUS_PRIORITY = {"passed": 0, "skipped": 1, "error": 2, "failed": 3}


def _final_counts(stats: dict[str, list[Any]]) -> tuple[int, int, int, int]:
    """按测试项去重后的 passed / failed / error / skipped 数。

    **为什么要去重**

    pytest 的一个用例会产生多条 report：setup、call、teardown 各一条。
    以前这里用 ``when != "teardown"`` 过滤 teardown，直接丢掉了
    teardown 阶段的异常 —— 清理失败（数据没删掉、连接没关）时
    ``result.json`` 仍然写"执行通过"，而 pytest 退出码是 1。

    现在按 nodeid 聚合成一条，取最严重的状态：
    call 失败 + teardown 报错只算一条 failed，不会重复计数；
    只有 teardown 报错则算 error。
    """
    final: dict[str, str] = {}

    for status in ("passed", "failed", "error", "skipped"):
        for report in stats.get(status, []):
            nodeid = getattr(report, "nodeid", "") or getattr(report, "id", "")
            if not nodeid:
                continue
            current = final.get(nodeid)
            if current is None or _STATUS_PRIORITY[status] > _STATUS_PRIORITY[current]:
                final[nodeid] = status

    counts = {"passed": 0, "failed": 0, "error": 0, "skipped": 0}
    for status in final.values():
        counts[status] += 1
    return counts["passed"], counts["failed"], counts["error"], counts["skipped"]


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
    passed, failed, error, skipped = _final_counts(stats)

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
        from utils.infra.parallel import is_parallel
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
