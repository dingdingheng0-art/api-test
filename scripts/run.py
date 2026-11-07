"""接口自动化测试入口。

用法::

    python scripts/run.py                          # 跑全部用例（默认生成 allure 报告）
    python scripts/run.py --env pre                # 指定环境
    python scripts/run.py --project mall4j
    python scripts/run.py --module statistics
    python scripts/run.py --case-file testcases/cases/mall4j/user/user_info.yaml
    python scripts/run.py --list                   # 只列出用例，不执行
    python scripts/run.py --report allure          # 生成 allure 报告（默认）
    python scripts/run.py --report html            # 生成 HTML 报告
    python scripts/run.py --report none            # 不生成报告

它本质上就是调 pytest，只是把环境、筛选条件、报告这些都包好了。

**目录结构**

.. code-block:: text

    testcases/     用例数据（YAML / Excel）+ 用例执行逻辑
    configs/       配置文件（config.yaml / modules.yaml）
    logs/          运行日志
    reports/       测试报告
    utils/         通用封装（yaml/excel/log/requests/参数化/断言…）
    framework/     pytest 插件、报告生成、钩子
    run.py         本文件 —— 启动器
"""
from __future__ import annotations

from typing import Any

import argparse
import sys
from pathlib import Path

# Windows 控制台默认是 GBK，中文输出会花掉。
# 强制用 UTF-8 写 stdout，终端里就能正常显示中文。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover - 老环境兜底
        pass

# 本文件在 scripts/ 下，项目根是它的上一级
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from framework.case_param import format_params
from framework.report_generator import ALLURE_RESULTS, REPORTS_ROOT  # noqa: E402
from testcases.case_loader import discover_cases, load_cases_from_file  # noqa: E402
from utils.logs.log_manager import log  # noqa: E402
from utils.cases.marks import extract_marks  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="接口自动化测试入口")
    parser.add_argument("--env", default=None, help="环境名：online / pre / test")
    parser.add_argument("--project", action="append", default=[], help="只跑指定项目（可重复）")
    parser.add_argument("--module", action="append", default=[], help="只跑指定模块（可重复）")
    parser.add_argument("--case-file", action="append", default=[], help="只跑指定用例文件（可重复）")
    parser.add_argument(
        "--case-id",
        action="append",
        default=[],
        help="只跑指定编号的用例，如 API-168（可重复）。同名用例只有它能精确挑出来",
    )
    parser.add_argument(
        "--case-source",
        default=None,
        help="用例来源：excel / yaml / both（默认读 config.yaml 的 case_source.mode）",
    )
    parser.add_argument("-m", dest="mark", default=None, help="按 mark 筛选用例，如 smoke")
    parser.add_argument("-k", dest="keyword", default=None, help="按关键字筛选用例")
    parser.add_argument(
        "--report",
        choices=["none", "allure", "html"],
        default="allure",
        help="报告类型：allure（默认）/ html / none",
    )
    parser.add_argument("--list", action="store_true", help="只列出用例")
    parser.add_argument("--marks", action="store_true", help="列出所有可用标签及其用例数")
    parser.add_argument(
        "--run-draft",
        action="store_true",
        help="连状态不是 debugged 的用例（draft / disabled / broken）也一起跑",
    )
    parser.add_argument("--clear-cache", action="store_true", help="清掉登录 token 缓存")
    parser.add_argument("--show-cache", action="store_true", help="查看登录 token 缓存状态")
    parser.add_argument("--no-cache", action="store_true", help="本次运行不用 token 缓存")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args()


def list_cases(
    mark_filter: str | None = None,
    keyword_filter: str | None = None,
    only_projects: list[str] | None = None,
    only_modules: list[str] | None = None,
    run_draft: bool = False,
) -> None:
    """列出所有用例。

    按「项目 / 模块」分组，显示每个接口的路径、方法、标签，
    以及测试数据在哪个文件、展开成几组参数。

    支持和执行时一样的筛选（``-m`` 标签、``-k`` 关键字），
    方便先看清楚「这个标签下有哪些用例」再决定跑哪几条。
    """
    from testcases.case_loader import find_data_file, relative_case_path
    from utils import paths
    from utils.cases.module_manager import ModuleManager
    from utils.cases.param_expander import expand_case

    cases = discover_cases()
    if not cases:
        print("没有找到任何用例")
        return

    if only_projects:
        cases = [(p, f) for p, f in cases if p in only_projects]
    if only_modules:
        cases = [
            (p, f)
            for p, f in cases
            if relative_case_path(f).parts[1:2]
            and relative_case_path(f).parts[1] in only_modules
        ]

    # 按执行顺序排
    cases = ModuleManager.sort_cases(cases)

    # 先把所有命中的用例收起来，最后按模块分组打印。
    # （如果边筛边打，会出现「这个模块一条都没匹配上、却还是打了一遍标题」的情况）
    matched: list[dict[str, Any]] = []
    invalid: list[str] = []

    for project, path in cases:
        rel = relative_case_path(path)
        module = rel.parts[1] if len(rel.parts) >= 2 else ""

        try:
            all_cases = load_cases_from_file(path)
        except Exception as exc:  # noqa: BLE001
            invalid.append(f"  [无效] {rel} -> {exc}")
            continue

        for data in all_cases:
            case = data["testcase"]
            try:
                expanded = expand_case(data, path)
            except Exception as exc:  # noqa: BLE001
                invalid.append(f"  [无效] {rel} / {case.get('name')} -> {exc}")
                continue

            desc = case.get("description", "")
            case_marks = extract_marks(case)

            # 状态：不是 debugged 的用例不会执行（除非 --run-draft）。
            # 以前这里不看状态，把 disabled / broken 也算进"会执行"的条数里，
            # 报 127 条、实际只跑 106 条，看清单的人会以为用例丢了。
            from utils.cases.case_status import resolve_status, RUNNABLE_STATUSES

            status = resolve_status(case)
            runnable = status in RUNNABLE_STATUSES or bool(run_draft)

            # ---- 筛选：标签 ----
            # 用例能带上的标签 = 模块标签 + 用例标签（和执行时一致）
            module_marks = ModuleManager.get_marks(project, module)
            all_marks = list(dict.fromkeys(module_marks + case_marks))
            if mark_filter and not _match_mark(mark_filter, all_marks):
                continue

            # ---- 筛选：关键字（用例名 / 参数组名 / 标签）----
            # 关键字如果命中的是某个参数组名，就只保留那一组参数，
            # 这样「列出来的条数」和「实际会跑的条数」是一致的。
            filtered_expanded = expanded
            if keyword_filter:
                filtered_expanded = [
                    exp for exp in expanded
                    if _keyword_hits(keyword_filter, case, exp, all_marks)
                ]
                if not filtered_expanded:
                    continue

            matched.append(
                {
                    "project": project,
                    "module": module,
                    "rel": rel,
                    "case": case,
                    "desc": desc,
                    "case_marks": case_marks,
                    "all_marks": all_marks,
                    "expanded": filtered_expanded,
                    "data_file": find_data_file(path),
                    "status": status,
                    "runnable": runnable,
                }
            )

    for line in invalid:
        print(line)
    if invalid:
        print()

    total = 0          # 本次真正会执行的条数
    skipped = 0        # 状态不是 debugged、会被跳过的条数
    printed_header = ""
    first_module = True

    for item in matched:
        project = item["project"]
        module = item["module"]
        rel = item["rel"]
        case = item["case"]

        # 模块标题：只在第一次出现这个模块时打
        header_key = f"{project}/{module}"
        if header_key != printed_header:
            printed_header = header_key
            if not first_module:
                print()
            first_module = False
            order = ModuleManager.get_order(project, module)
            module_marks = ModuleManager.get_marks(project, module)
            desc = ModuleManager.get_description(project, module)

            header = f"【{project} / {module}】"
            if module_marks:
                header += f"  标签: {', '.join(module_marks)}"
            print(header + (f"  — {desc}" if desc else ""))
            if module:
                print(f"  执行顺序: {order}")
            print()

        print(f"  ● {case['name']}")
        if item["desc"]:
            print(f"      说明: {item['desc']}")

        # 会被跳过的用例标出来（不然看到清单以为会跑，跑起来却是 skip）
        skip_reason = case.get("skip")
        if skip_reason:
            print(f"      ⚠ 已跳过: {skip_reason}")
        if not item["runnable"]:
            print(f"      ⚠ 不会执行: 用例状态 = {item['status']}（加 --run-draft 才会跑）")

        # 接口路径 / 方法
        for step in case.get("steps", []):
            if step.get("path"):
                method = step.get("method", "?")
                print(f"      接口: {method} {step['path']}")
            elif step.get("operation_type"):
                print(f"      特殊步骤: {step['operation_type']}")

        if item["case_marks"]:
            print(f"      用例标签: {', '.join(item['case_marks'])}")

        # 测试数据来源
        data_file = item["data_file"]
        if data_file:
            from testcases.case_loader import relative_case_path

            data_rel = relative_case_path(data_file).as_posix()
            print(f"      数据文件: {data_rel}")

        print(f"      定义文件: {rel.as_posix()}")

        expanded = item["expanded"]
        if item["runnable"]:
            total += len(expanded)
        else:
            skipped += len(expanded)
        if len(expanded) > 1:
            print(f"      参数化 → 展开 {len(expanded)} 条:")
            for exp in expanded:
                print(f"        - {exp.param_name}: {format_params(exp.params)}")
        print()

    if not matched:
        print("没有匹配到用例")
        print(f"  标签筛选: {mark_filter or '(无)'}")
        print(f"  关键字筛选: {keyword_filter or '(无)'}")
        return

    print(f"实际会执行 {total} 条用例")
    if skipped:
        print(f"另有 {skipped} 条状态不是 debugged（disabled / broken / draft），不会执行")

    # ---- 提示另一种来源里还藏着多少用例 ----
    #
    # 选了 Excel 模式但 YAML 目录里还有一堆用例时，用户常以为用例丢了。
    # 这里直接告诉他"那边还有多少、怎么切过去看"。
    _print_other_source_hint()


def _print_other_source_hint() -> None:
    """当前模式没被扫到的另一种来源里，还有多少用例。"""
    from testcases.case_loader import discover_cases
    from utils.cases.case_source import CaseSource, get_case_source

    active = get_case_source()
    if active is CaseSource.BOTH:
        return

    other = CaseSource.YAML if active is CaseSource.EXCEL else CaseSource.EXCEL

    try:
        other_cases = discover_cases(source=other)
    except Exception:  # noqa: BLE001 - 提示信息而已，读不到就算了
        return

    if not other_cases:
        return

    label = "Excel" if active is CaseSource.EXCEL else "YAML"
    other_label = "YAML" if other is CaseSource.YAML else "Excel"
    print()
    print(f"提示: 当前是 {label} 模式，{other_label} 目录里还有 "
          f"{len(other_cases)} 个用例文件不会执行。")
    print(f"      想改成执行它们，用 --case-source {other.value} 切换。")


def _match_mark(mark_filter: str, all_marks: list[str]) -> bool:
    """判断用例是否命中标签筛选。

    支持 pytest 那套写法：``smoke``、``"smoke and p0"``、``"not slow"``、``"smoke or p0"``。
    """
    import re

    expr = mark_filter.strip()
    if not expr:
        return True

    # 先按 " and " 拆成若干段，每段再按 " or " 拆
    for and_part in re.split(r"\s+and\s+", expr, flags=re.IGNORECASE):
        and_part = and_part.strip()
        if not and_part:
            continue

        negate = False
        if and_part.lower().startswith("not "):
            negate = True
            and_part = and_part[4:].strip()

        # 一段里的 " or "：任意一个命中就行
        hit = False
        for or_part in re.split(r"\s+or\s+", and_part, flags=re.IGNORECASE):
            name = or_part.strip().strip("'\"")
            if not name:
                continue
            if any(m == name or m.lower() == name.lower() for m in all_marks):
                hit = True
                break

        if negate:
            hit = not hit

        if not hit:
            return False

    return True


def _keyword_hits(
    keyword_filter: str,
    case: dict[str, Any],
    expanded_item: Any,
    all_marks: list[str],
) -> bool:
    """判断「一条具体的参数组」是否命中关键字。

    比对范围：用例名、参数组名、标签。

    ⚠️ 刻意**不**比对「说明」字段 —— pytest 那边匹配不到说明，
    这里要是也匹配，列出来的条数和实际跑的条数就对不上了。
    """
    needle = keyword_filter.strip().lower()
    if not needle:
        return True

    haystacks = [
        str(case.get("name", "")),
        str(expanded_item.param_name),
        *all_marks,
    ]
    return any(needle in text.lower() for text in haystacks)


def _quiet_logs() -> None:
    """只列出用例时，把框架自己的 INFO 日志压掉。

    不然控制台会先刷一堆「用例 X 展开为 N 组参数」，
    把人能看的清单挤到下面去。
    """
    import logging

    from utils.logs.log_manager import log as framework_log

    framework_log.setLevel(logging.WARNING)
    framework_log.parent.setLevel(logging.WARNING)


def list_marks() -> None:
    """列出所有可用标签，以及每个标签下有多少条用例。

    跑之前先 ``python scripts/run.py --marks`` 看一眼，就知道有哪些标签、怎么筛。
    """
    from testcases.case_loader import load_cases_from_file, relative_case_path
    from utils.cases.module_manager import ModuleManager
    from utils.cases.param_expander import expand_case

    cases = discover_cases()
    if not cases:
        print("没有找到任何用例")
        return

    # 标签 -> 用例条数
    counter: dict[str, int] = {}
    # 标签 -> 用例名（最多列 3 个当例子）
    samples: dict[str, list[str]] = {}

    for project, path in cases:
        rel = relative_case_path(path)
        module = rel.parts[1] if len(rel.parts) >= 2 else ""

        try:
            all_cases = load_cases_from_file(path)
        except Exception:  # noqa: BLE001
            continue

        module_marks = ModuleManager.get_marks(project, module)

        for data in all_cases:
            case = data["testcase"]
            try:
                expanded = expand_case(data, path)
            except Exception:  # noqa: BLE001
                continue

            marks = list(dict.fromkeys(module_marks + extract_marks(case)))
            for mark in marks:
                counter[mark] = counter.get(mark, 0) + len(expanded)
                bucket = samples.setdefault(mark, [])
                if len(bucket) < 3:
                    bucket.append(str(case.get("name", "")))

    print("可用标签（按用例数排序）\n")
    for mark in sorted(counter, key=lambda m: (-counter[m], m)):
        print(f"  {mark:<12} {counter[mark]:>4} 条   示例: {'、'.join(samples.get(mark, []))}")

    print("\n用法:  python scripts/run.py -m <标签>          只跑这个标签")
    print("       python scripts/run.py --list -m <标签>   先看看这个标签下有哪些用例")


def build_pytest_args(args: argparse.Namespace) -> list[str]:
    """把命令行参数翻译成 pytest 参数。"""
    # 指向项目根 —— 根 conftest.py 里用 pytest_plugins 声明了框架各层
    # （指向 testcases/ 的话根 conftest 不会被加载，插件就失效了）
    cmd = [str(_PROJECT_ROOT), "-v" if args.verbose else "-q"]

    # 只收集用例层，工具脚本（tools/）不混进接口测试报告
    cmd += ["--ignore", str(_PROJECT_ROOT / "tools")]
    # 单元测试（框架自己的单测）单独跑，不混进接口测试报告
    cmd += ["--ignore", str(_PROJECT_ROOT / "unit_tests")]
    # 临时脚本目录（调试用），里面可能有 test_*.py，别被误收集
    cmd += ["--ignore", str(_PROJECT_ROOT / "data")]
    if args.env:
        cmd += ["--apienv", args.env]
    for project in args.project:
        cmd += ["--case-project", project]
    for module in args.module:
        cmd += ["--case-module", module]
    for case_file in args.case_file:
        cmd += ["--case-file", case_file]
    for case_id in args.case_id:
        cmd += ["--case-id", case_id]
    if args.case_source:
        cmd += ["--case-source", args.case_source]
    if args.mark:
        cmd += ["-m", args.mark]
    if args.keyword:
        cmd += ["-k", args.keyword]
    if args.run_draft:
        cmd += ["--run-draft"]

    # JUnit XML —— CI（Jenkins / GitLab）的 junit 步骤只认这个格式。
    # 和 allure / html 无关，只要跑用例就产出，免得 CI 端拿不到结构化结果。
    cmd += ["--junitxml", str(REPORTS_ROOT / "junit.xml")]

    if args.report == "html":
        cmd += ["--html", str(REPORTS_ROOT / "report.html"), "--self-contained-html"]
    elif args.report == "allure":
        # 关键：跑的时候就要指定 allure 结果目录，
        # 否则 pytest 结束后目录是空的，生成不出 HTML
        cmd += ["--alluredir", str(ALLURE_RESULTS)]
    return cmd


def main() -> int:
    args = parse_args()

    # 用例来源要先定下来 —— --list / --marks 这些是 run.py 自己执行的，
    # 不走 pytest，所以命令行参数得先落到环境变量上，
    # 让 run.py 内部的收集逻辑和 pytest 那边看到的是同一个来源。
    if args.case_source:
        import os

        os.environ["APITEST_CASE_SOURCE"] = args.case_source

    # 缓存相关的开关要在跑之前处理
    if args.no_cache:
        import os

        os.environ["APITEST_NO_TOKEN_CACHE"] = "1"
        print("警告:   本次运行不使用 token 缓存（会重新走滑块登录）\n")

    if args.clear_cache:
        from utils.infra.token_cache import TokenCache

        TokenCache.clear()
        print(" token 缓存已清空\n")
        return 0

    if args.show_cache:
        from utils.infra.token_cache import TokenCache

        print("token 缓存状态：")
        TokenCache.show()
        return 0

    if args.marks:
        _quiet_logs()
        list_marks()
        return 0

    if args.list:
        _quiet_logs()
        list_cases(
            mark_filter=args.mark,
            keyword_filter=args.keyword,
            only_projects=args.project,
            only_modules=args.module,
            run_draft=args.run_draft,
        )
        return 0

    cmd = build_pytest_args(args)
    log.info("执行: %s", " ".join(cmd))

    import pytest

    # 上一轮的 result.json / junit.xml 先删掉 —— 这一轮如果一条用例都没跑，
    # 留着旧文件会让 CI 读到上一次的结果（"绿"得莫名其妙）。
    for stale in ("result.json", "junit.xml"):
        try:
            (REPORTS_ROOT / stale).unlink()
        except FileNotFoundError:
            pass

    # 清 allure 结果目录要放在 pytest 跑之前 —— 跑完再清就把数据删了
    if args.report == "allure":
        from framework.report_generator import clear_allure_results

        clear_allure_results()

    exit_code = pytest.main(cmd)

    if args.report == "html":
        html_path = REPORTS_ROOT / "report.html"
        print(f"\n HTML 报告: {html_path}")

    if args.report == "allure":
        from framework.report_generator import generate_allure_report
        from utils.config.config_manager import ConfigManager

        env = args.env or ConfigManager.get_env()
        index = generate_allure_report(env)
        if index:
            print(f"\n allure 报告: {index}")
        else:
            print("\n警告:   allure 报告生成失败（结果目录可能是空的）")

    print("\n" + "=" * 60)
    print(f"测试结束，退出码: {exit_code}")
    print("=" * 60)
    return int(exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
