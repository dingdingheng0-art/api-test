"""CI 统一入口。

**为什么要有这个脚本**：CI 流水线里写一长串命令很烦，而且每家 CI 的
写法都不一样（Jenkins 用 shell、GitLab 用 script 块）。
把这个脚本做成一个"命令行黑盒"，CI 里只要调它就行。

做的事：

1. 检查依赖（缺什么会明确告诉你）
2. 跑用例（可指定环境、标签、模块）
3. 生成报告（allure / html / json）
4. 生成失败根因分析报告
5. 按结果返回退出码（CI 靠这个判断成败）

用法::

    python ci/run_ci.py --env pre --mark smoke
    python ci/run_ci.py --env pre --all --report allure
    python ci/run_ci.py --env pre --dry-run     # 只看会跑什么

**退出码**：

- ``0`` —— 全过
- ``1`` —— 有用例失败
- ``2`` —— 脚本自己出了问题（配置错、依赖缺失）
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _log(message: str) -> None:
    """打印（CI 日志里加时间戳，方便定位）。"""
    stamp = time.strftime("%H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def check_dependencies() -> list[str]:
    """检查必需的依赖，返回缺失清单。"""
    missing: list[str] = []

    try:
        import pytest  # noqa: F401
    except ImportError:
        missing.append("pytest")

    try:
        import yaml  # noqa: F401
    except ImportError:
        missing.append("PyYAML")

    return missing


def build_pytest_args(args: argparse.Namespace) -> list[str]:
    """把命令行参数翻译成 pytest 参数。"""
    from framework.report_generator import ALLURE_RESULTS

    cmd = [str(PROJECT_ROOT), "-v"]

    # 只收用例层，别把工具脚本和框架自测混进来
    for folder in ("tools", "unit_tests", "data"):
        cmd += ["--ignore", str(PROJECT_ROOT / folder)]

    if args.env:
        cmd += ["--apienv", args.env]
    if args.mark:
        cmd += ["-m", args.mark]
    if args.keyword:
        cmd += ["-k", args.keyword]
    for project in args.project:
        cmd += ["--case-project", project]
    for module in args.module:
        cmd += ["--case-module", module]

    # ---- 调试态过滤 ----
    #
    # 默认只跑「调试通过」的用例。想全跑加 --run-draft。
    if args.run_draft:
        cmd += ["--run-draft"]
    for status in args.case_status:
        cmd += ["--case-status", status]

    # ---- 并行 ----
    if args.workers and args.workers > 1:
        cmd += ["-n", str(args.workers)]
        # 并行时每个 worker 的 allure 结果分开写，跑完再合并
        # （不然多个进程同时写一个目录，结果会互相覆盖）
        cmd += ["--alluredir", str(_worker_allure_dir())]
        # xdist 的调度方式：按用例负载均衡，比按文件平均分更均衡
        cmd += ["--dist", args.dist]
    else:
        cmd += ["--alluredir", str(ALLURE_RESULTS)]

    if not args.no_retry:
        # 默认开着重试（环境抖动不判失败）
        pass
    else:
        cmd += ["--no-retry"]

    # ---- JUnit XML ----
    #
    # Jenkins / GitLab 的 ``junit`` 步骤只认 XML。以前 CI 配的是
    # ``reports/result.json``（自定义 JSON），平台解析不了 —— 等于没有测试结果。
    from framework.report_generator import REPORTS_ROOT

    cmd += ["--junitxml", str(REPORTS_ROOT / "junit.xml")]

    if args.report in ("html", "both"):
        cmd += ["--html", str(REPORTS_ROOT / "report.html"), "--self-contained-html"]

    return cmd


def _worker_allure_dir() -> Path:
    """并行时 allure 结果的临时目录（跑完会合并到正式目录）。"""
    from framework.report_generator import ALLURE_RESULTS

    return ALLURE_RESULTS / "_workers"


def run_tests(args: argparse.Namespace) -> int:
    """跑用例，返回 pytest 的退出码。"""
    import pytest

    from framework.report_generator import REPORTS_ROOT, clear_allure_results

    # allure 结果目录要在跑之前清（跑完再清就等于删数据）
    if args.report in ("allure", "both"):
        clear_allure_results()

    # 上一轮的结果文件先删 —— 本轮一条都没跑时，不能让 CI 读到旧结果
    try:
        (REPORTS_ROOT / "result.json").unlink()
    except FileNotFoundError:
        pass

    cmd = build_pytest_args(args)
    _log("执行: pytest " + " ".join(cmd))

    exit_code = int(pytest.main(cmd))

    # 并行跑的话，各 worker 的结果要合并回来
    if args.workers and args.workers > 1:
        _merge_parallel_results()

    _log(f"测试结束，退出码 {exit_code}")
    return exit_code


def _merge_parallel_results() -> None:
    """把各 worker 的 allure 结果合并到主目录。"""
    from framework.report_generator import ALLURE_RESULTS, merge_worker_results

    # worker 结果先在 _workers 子目录里，合并完搬到主目录
    workers_dir = ALLURE_RESULTS / "_workers"
    if workers_dir.exists():
        for item in list(workers_dir.iterdir()):
            target = ALLURE_RESULTS / item.name
            try:
                if target.exists():
                    if target.is_dir():
                        shutil.rmtree(target, ignore_errors=True)
                    else:
                        target.unlink()
                shutil.move(str(item), str(target))
            except OSError as exc:
                _log(f"合并 worker 结果失败 {item.name}: {exc}")

        try:
            shutil.rmtree(workers_dir, ignore_errors=True)
        except OSError:
            pass

    merged = merge_worker_results()
    if merged:
        _log(f"已合并 {merged} 个并行结果文件")


def generate_reports(args: argparse.Namespace) -> None:
    """生成 allure 报告和根因分析报告。"""
    from framework.report_generator import generate_allure_report
    from utils.config.config_manager import ConfigManager

    env = args.env or ConfigManager.get_env()

    if args.report in ("allure", "both"):
        index = generate_allure_report(env, open_browser=False)
        if index:
            _log(f"allure 报告: {index}")
        else:
            _log("allure 报告生成失败（可能是没装 allure 命令行）")

    # 根因分析（有失败用例才做）
    if not args.no_diagnosis:
        try:
            from ai.capabilities.diagnosis import collect_failures, diagnose, save_report
            from utils.paths import ALLURE_RESULTS, REPORTS_ROOT

            failures = collect_failures(REPORTS_ROOT / "result.json", ALLURE_RESULTS)
            if failures:
                _log(f"分析 {len(failures)} 条失败用例...")
                diagnoses = diagnose(failures, use_ai=not args.rules_only)
                path = save_report(diagnoses)
                _log(f"根因分析报告: {path}")
            else:
                _log("没有失败用例，跳过根因分析")
        except Exception as exc:  # noqa: BLE001 - 分析失败不该影响主流程
            _log(f"根因分析失败（已忽略）: {exc}")


def write_ci_summary() -> Path:
    """写一份 CI 好采集的汇总。

    产出 ``reports/ci_summary.json``，包含结论、统计、报告路径。
    """
    from utils.paths import REPORTS_ROOT

    REPORTS_ROOT.mkdir(parents=True, exist_ok=True)
    result_path = REPORTS_ROOT / "result.json"

    data: dict[str, Any] = {}
    if result_path.exists():
        try:
            data = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}

    summary = {
        "conclusion": data.get("conclusion", "未知"),
        "total": data.get("total", 0),
        "passed": data.get("passed", 0),
        "failed": data.get("failed", 0),
        "error": data.get("error", 0),
        "skipped": data.get("skipped", 0),
        "duration_seconds": round(data.get("duration", 0), 1),
        "env": data.get("env"),
        "build_url": os.environ.get("BUILD_URL") or os.environ.get("CI_JOB_URL") or "",
        "report_files": {
            "result_json": str(result_path),
            "allure_results": str(REPORTS_ROOT / "allure-results"),
            "html_report": str(REPORTS_ROOT / "report.html"),
        },
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }

    out = REPORTS_ROOT / "ci_summary.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def send_notifications(args: argparse.Namespace) -> None:
    """按配置把结果发到群里。

    **什么时候发**：看 ``--notify`` 和 ``--notify-policy``。

    - ``--notify ding_ding`` —— 指定渠道（可重复传，发多个群）
    - ``--notify-policy on_fail`` —— 只在失败时发（默认）
    - ``--notify-policy always`` —— 每次都发
    - ``--notify-policy not_send`` —— 不发
    - 不加 ``--notify`` 就不发（Webhook 地址由 CI 变量传入）

    **不会让 CI 挂掉**：通知发不出去只记日志，不影响测试结论。
    """
    from utils.paths import REPORTS_ROOT

    if args.notify_policy == "not_send":
        _log("通知策略：不发送")
        return

    # 收集失败用例明细
    failures: list[dict[str, Any]] = []
    try:
        from ai.capabilities.diagnosis import collect_failures
        from utils.paths import ALLURE_RESULTS

        failures = collect_failures(REPORTS_ROOT / "result.json", ALLURE_RESULTS)
    except Exception as exc:  # noqa: BLE001 - 收集失败明细失败不影响通知
        _log(f"收集失败明细失败（已忽略）: {exc}")

    # 组装 webhook 列表
    webhooks: list[dict[str, Any]] = []
    for channel in args.notify:
        webhooks.append(
            {
                "type": channel,
                "addr": args.notify_addr or "",
                "secret": args.notify_secret or "",
                "name": channel,
            }
        )

    if not webhooks:
        _log("没有指定 --notify，跳过通知")
        return

    # 读结果
    results: dict[str, Any] = {}
    result_path = REPORTS_ROOT / "result.json"
    if result_path.exists():
        try:
            results = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            results = {}

    # 报告链接（CI 里一般能拿到构建地址）
    report_url = (
        os.environ.get("BUILD_URL")
        or os.environ.get("CI_JOB_URL")
        or os.environ.get("REPORT_URL")
        or ""
    )

    try:
        from utils.infra.notifier import notify_result

        env = args.env or results.get("env") or ""
        project = args.project[0] if args.project else ""
        for hook in webhooks:
            outcome = notify_result(
                webhook_type=hook["type"],
                addr=hook["addr"],
                secret=hook["secret"],
                results=results,
                failures=failures,
                env=env,
                project=project,
                policy=args.notify_policy,
                report_url=report_url,
            )
            if outcome.skipped:
                continue
            state = "成功" if outcome.ok else f"失败（{outcome.message}）"
            _log(f"通知 {outcome.channel}: {state}")
    except Exception as exc:  # noqa: BLE001 - 通知失败不该让 CI 挂掉
        _log(f"发送通知失败（已忽略）: {exc}")


def main(argv: list[str] | None = None) -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="CI 统一入口")
    parser.add_argument("--env", default=None, help="测试环境（online / pre / test）")
    parser.add_argument("--mark", default=None, help="按标签筛选（如 smoke）")
    parser.add_argument("--keyword", default=None, help="按关键字筛选")
    parser.add_argument("--project", action="append", default=[], help="只跑指定项目")
    parser.add_argument("--module", action="append", default=[], help="只跑指定模块")
    parser.add_argument(
        "--report",
        choices=["none", "allure", "html", "both"],
        default="allure",
        help="生成什么报告",
    )
    parser.add_argument("--no-retry", action="store_true", help="关掉用例级重试")
    parser.add_argument("--no-diagnosis", action="store_true", help="不生成根因分析报告")
    parser.add_argument("--rules-only", action="store_true", help="根因分析只用规则，不用 AI")
    parser.add_argument("--dry-run", action="store_true", help="只打印要跑什么，不真跑")
    parser.add_argument(
        "--workers", type=int, default=1,
        help="并行执行的进程数（默认 1 = 串行）。例：--workers 4",
    )
    parser.add_argument(
        "--dist", default="load",
        choices=["load", "loadscope", "loadfile", "each", "worksteal"],
        help="xdist 调度方式（默认 load = 按负载均衡）",
    )
    parser.add_argument(
        "--run-draft", action="store_true",
        help="连「未调试」的用例也一起跑（本地调试用）",
    )
    parser.add_argument(
        "--case-status", action="append", default=[],
        help="只跑指定状态的用例：draft/debugged/disabled/broken（可重复传）",
    )
    parser.add_argument(
        "--notify", action="append", default=[],
        help="结果通知渠道：ding_ding / we_chat / feishu（可重复传）。"
             "不传就从数据库 config_webhook 表读配置",
    )
    parser.add_argument(
        "--notify-addr", default="",
        help="Webhook 地址（配合 --notify 用；不传就只读数据库配置）",
    )
    parser.add_argument(
        "--notify-secret", default="",
        help="Webhook 加签密钥（配合 --notify 用）",
    )
    parser.add_argument(
        "--notify-policy", default="on_fail",
        choices=["not_send", "on_fail", "always"],
        help="什么时候发通知：只在失败时发（默认）/ 每次都发 / 不发",
    )
    args = parser.parse_args(argv)

    # 注意：``both``（allure + html）**不要**降级成 allure ——
    # 以前这里悄悄改成 allure，导致 --report both 从来没生成过 HTML。

    _log("=" * 60)
    _log(f"CI 运行开始 | 环境: {args.env or '(配置默认)'} | 报告: {args.report}")
    _log("=" * 60)

    missing = check_dependencies()
    if missing:
        _log(f"缺少依赖: {', '.join(missing)}")
        _log("装一下: uv pip install -r requirements.txt")
        return 2

    if args.dry_run:
        cmd = build_pytest_args(args)
        _log("将要执行: pytest " + " ".join(cmd))
        return 0

    try:
        exit_code = run_tests(args)
    except Exception as exc:  # noqa: BLE001
        _log(f"运行出错: {exc}")
        return 2

    if exit_code == 5:
        # pytest 的 5 = 一条都没收集到。最常见的原因是 --mark / --keyword
        # 写错（比如标签根本没有打在用例上），得说人话，别让人去翻 pytest 文档。
        _log(
            "没有收集到任何用例 —— 检查 --mark / --keyword / --case-status "
            "是否有对应的用例（当前筛选条件可能就是空的）"
        )

    try:
        generate_reports(args)
    except Exception as exc:  # noqa: BLE001
        _log(f"生成报告出错（已忽略）: {exc}")

    summary_path = write_ci_summary()
    _log(f"CI 汇总: {summary_path}")

    # 通知（失败不影响 CI 结论）
    try:
        send_notifications(args)
    except Exception as exc:  # noqa: BLE001
        _log(f"发送通知出错（已忽略）: {exc}")

    # 退出码：区分"用例失败"和"脚本坏了"
    if exit_code in (0, 1):
        return exit_code
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
