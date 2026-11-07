"""pytest 插件：把 YAML 用例变成 pytest 测试项。

用法（在 conftest.py 里引入）::

    pytest_plugins = ["apitest.framework.pytest_plugin"]

工作原理：
- ``pytest_generate_tests`` 阶段扫描 ``cases/`` 下的 YAML，为每条用例生成一个测试项
- 用例 ID 形如 ``cases/mall4j/user/test_user_info.yaml#user_info``
- 带 ``parameters`` 的用例会展开成多个测试项，ID 里带参数组名字
- YAML 里写的 ``marks`` 会变成 pytest 的 mark，可以用 ``-m`` 筛选

命令行参数（自动可用）：
- ``--apienv <env>``        指定环境（默认 online，可用环境变量 APITEST_ENV）
- ``--case-file <path>``    只跑指定的 YAML 用例文件（可重复）
- ``--case-project <name>`` 只跑指定项目的用例
- ``--case-module <name>``  只跑指定模块（cases/<项目>/<模块>/）
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from framework.allure_util import allure, has_allure
from framework.case_param import CasePayload, format_case_label
from testcases.case_loader import (
    discover_cases,
    load_cases_from_file,
    relative_case_path,
)
from utils import paths
from utils.logs.log_manager import log
from utils.cases.marks import extract_marks
from utils.cases.module_manager import ModuleManager
from utils.cases.param_expander import expand_case
from utils.cases.case_dependency import (
    DependencyError,
    build_graph_from_cases,
    get_store,
)


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("apitest", "接口自动化测试")
    group.addoption(
        "--apienv",
        action="store",
        default=None,
        help="测试环境名（online / pre / test），默认读 config.yaml 的 default_env",
    )
    group.addoption(
        "--case-file",
        action="append",
        default=[],
        help="只跑指定的 YAML 用例文件路径（可重复传）",
    )
    group.addoption(
        "--case-project",
        action="append",
        default=[],
        help="只跑指定项目的用例（可重复传）",
    )
    group.addoption(
        "--case-module",
        action="append",
        default=[],
        help="只跑指定模块的用例（可重复传）",
    )
    group.addoption(
        "--run-draft",
        action="store_true",
        default=False,
        help="连「未调试」的用例也一起跑（本地调试用，CI 别加）",
    )
    group.addoption(
        "--case-status",
        action="append",
        default=[],
        help="只跑指定状态的用例：draft/debugged/disabled/broken（可重复传）",
    )
    group.addoption(
        "--case-id",
        action="append",
        default=[],
        help="只跑指定编号的用例，如 API-168（可重复传）",
    )
    group.addoption(
        "--case-source",
        action="store",
        default=None,
        help="用例来源：excel / yaml / both（默认读 config.yaml 的 case_source.mode）",
    )


def pytest_configure(config: pytest.Config) -> None:
    """session 开始：注册框架认识的 mark。

    环境的确定、报告目录准备在 ``hooks.py`` 的 ``pytest_configure`` 里做
    （这里只管 mark 相关的注册，避免两处重复）。
    """
    # 命令行指定了用例来源 → 写进环境变量，让所有收集点统一按它来。
    #
    # 为什么不直接往下传参：收集用例的地方有好几个（pytest 插件、
    # run.py 的 --list、覆盖率统计……），走环境变量最省事，
    # 不用给每个函数都加一个参数。
    source_override = config.getoption("--case-source", default=None)
    if source_override:
        import os

        os.environ["APITEST_CASE_SOURCE"] = source_override
        # 模块清单有缓存，换了来源要让它重新算
        from utils.cases.module_manager import ModuleManager

        ModuleManager.reset()

    config.addinivalue_line(
        "markers", "smoke: 冒烟用例"
    )
    config.addinivalue_line("markers", "p0: 优先级 P0")
    config.addinivalue_line("markers", "p1: 优先级 P1")
    config.addinivalue_line("markers", "p2: 优先级 P2")
    config.addinivalue_line("markers", "api: 接口用例")
    config.addinivalue_line("markers", "ui: UI 用例")
    config.addinivalue_line("markers", "auth: 认证 / 登录相关用例")
    config.addinivalue_line("markers", "wip: 开发中，CI 默认跳过")
    config.addinivalue_line("markers", "slow: 慢用例")
    config.addinivalue_line("markers", "flaky: 不稳定用例")
    config.addinivalue_line("markers", "regression: 回归用例")

    _REGISTERED_MARKS.update(
        {
            "smoke", "p0", "p1", "p2", "api", "ui", "auth",
            "wip", "slow", "flaky", "regression",
        }
    )

    # 动态注册 modules.yaml 里定义的模块标签
    # （不注册的话 pytest 会报 "Unknown pytest.mark.xxx" 警告）
    from utils.cases.module_manager import ModuleManager

    for project in ModuleManager.list_projects():
        for module in ModuleManager.list_modules(project):
            for mark in module.get("marks") or []:
                config.addinivalue_line(
                    "markers", f"{mark}: 模块 {project}/{module['name']} 的标签"
                )

    # 动态注册**用例里自己写的**标签
    # （比如 login_negative.yaml 写的 negative —— 第一次用就会报
    #  "Unknown pytest.mark.negative" 警告，看着像报错，其实是提醒）
    for mark in _collect_case_marks():
        if mark not in _REGISTERED_MARKS:
            config.addinivalue_line("markers", f"{mark}: 用例自定义标签")
            _REGISTERED_MARKS.add(mark)


# 已经注册过的标签，避免重复注册
_REGISTERED_MARKS: set[str] = set()


def _collect_case_marks() -> list[str]:
    """扫描所有用例文件，把里面用到的标签收起来。

    扫描失败就跳过 —— 注册标签只是消除警告，不该影响执行。
    """
    from testcases.case_loader import discover_cases, load_cases_from_file
    from utils.cases.param_expander import expand_case

    marks: list[str] = []
    seen: set[str] = set()

    try:
        cases = discover_cases()
    except Exception:  # noqa: BLE001
        return marks

    for _project, yaml_file in cases:
        try:
            all_cases = load_cases_from_file(yaml_file)
        except Exception:  # noqa: BLE001
            continue

        for data in all_cases:
            try:
                case = data["testcase"]
                expand_case(data, yaml_file)
            except Exception:  # noqa: BLE001
                continue

            for mark in extract_marks(case):
                if mark not in seen:
                    seen.add(mark)
                    marks.append(mark)

    return marks


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """为每个 YAML 用例生成一个（或多个）参数化测试项。

    带 ``parameters`` 的用例会展开成多个测试项，ID 里带上参数组名字。
    """
    if "yaml_case" not in metafunc.fixturenames:
        return

    config = metafunc.config
    only_files = [str(Path(f).resolve()) for f in config.getoption("--case-file")]
    only_projects = set(config.getoption("--case-project"))
    only_modules = set(config.getoption("--case-module"))

    # 用例根目录由"用例来源"开关决定（Excel 目录 / YAML 目录 / 两个都扫）
    candidates = discover_cases()
    if only_projects:
        candidates = [(p, f) for p, f in candidates if p in only_projects]
    if only_modules:
        candidates = [
            (p, f)
            for p, f in candidates
            if relative_case_path(f).parts[1:2]
            and relative_case_path(f).parts[1] in only_modules
        ]
    if only_files:
        candidates = [(p, f) for p, f in candidates if str(f.resolve()) in only_files]

    # 按模块顺序排（登录模块先跑，后面的用例才有 token）
    candidates = ModuleManager.sort_cases(candidates)

    params: list[Any] = []
    ids: list[str] = []
    # 已经用过的唯一标识 —— 用来兜住「连编号都一样」的极端情况
    used_suffixes: set[str] = set()

    # ---- 调试态 / 执行态过滤 ----
    #
    # 默认只跑「调试通过」的用例。没调试过的用例（draft）、
    # 临时下线的（disabled）、已知有问题的（broken）都不进 CI。
    # 本地想全跑加 ``--run-draft``。
    run_draft = bool(config.getoption("--run-draft", default=False))
    status_filter = {
        s.strip().lower() for s in config.getoption("--case-status", default=[]) if s.strip()
    }
    if status_filter:
        from utils.cases.case_status import ALL_STATUSES

        unknown = status_filter - set(ALL_STATUSES)
        if unknown:
            log.warning(
                "未知的用例状态 %s（可用：%s）",
                "、".join(sorted(unknown)),
                "、".join(ALL_STATUSES),
            )
            status_filter = status_filter & set(ALL_STATUSES)

    skipped_by_status: dict[str, int] = {}

    # ---- 按编号精确挑用例 ----
    #
    # **为什么不能复用 ``-k``**：``-k`` 匹配的是节点 ID，而节点 ID 里只有
    # 用例名。同名用例（「查询地区详情」有正常和查不到两条）一 ``-k``
    # 就全中。自愈闭环要「改完只重跑这一条」，按名字挑会顺带跑隔壁那条，
    # 结论就不可信了。
    only_case_ids = {
        str(c).strip() for c in config.getoption("--case-id", default=[]) if str(c).strip()
    }

    # ---- 先把所有用例读出来 ----
    #
    # 为什么要先全读一遍：要用例间的依赖关系重排执行顺序，
    # 只看当前文件是看不出谁依赖谁的。
    loaded: list[tuple[str, Path, dict[str, Any]]] = []

    for project, yaml_file in candidates:
        try:
            # 一个文件可能有多条用例（Excel），YAML 是一条
            all_cases = load_cases_from_file(yaml_file)
        except Exception as exc:  # noqa: BLE001 - 用例坏了要报出来，但不能挡住其它用例
            log.error("跳过无效用例文件 %s: %s", yaml_file, exc)
            continue

        for data in all_cases:
            case = data["testcase"]

            # 编号过滤放在状态过滤**之前**：点名单条用例时状态不设门槛。
            # 自愈流程里「新建的用例（draft）跑通再改状态」正好需要这个行为 ——
            # 否则新建的用例永远跑不了，也就永远改不成 debugged。
            if only_case_ids:
                case_no = str(case.get("case_id") or "").strip()
                if case_no not in only_case_ids:
                    continue
            elif not _status_allows(case, run_draft, status_filter):
                from utils.cases.case_status import resolve_status

                key = resolve_status(case)
                skipped_by_status[key] = skipped_by_status.get(key, 0) + 1
                continue

            loaded.append((project, yaml_file, data))

    # ---- 按依赖关系重排顺序 ----
    loaded = _sort_by_dependencies(loaded)

    for project, yaml_file, data in loaded:
        case = data["testcase"]

        try:
            expanded = expand_case(data, yaml_file)
        except Exception as exc:  # noqa: BLE001
            log.error("用例 %s 参数展开失败: %s", yaml_file, exc)
            continue

        for item in expanded:
            # 层级：收集阶段就算好，报告和运行阶段共用
            from testcases.case_hierarchy import CaseHierarchy

            hierarchy = CaseHierarchy.resolve(case, project, yaml_file)

            # 唯一标识：不同模块里可能有同名用例（每个模块都有 not_exist），
            # 而 allure 靠「用例名 + 参数值」算指纹，同名的会被合并成一条。
            # 这里给每条用例补一个唯一后缀，避免报告少算用例。
            unique_suffix = _unique_suffix(case, project, yaml_file, used_suffixes)

            # 只把「简短的标识」放进 pytest 参数 —— 报告里显示的是参数值，
            # 完整用例数据太长了，塞进去报告会很难看。
            #
            # 用 CasePayload 包一层：数据照常完整（框架照读不误），
            # 但 repr() 只是一行文字，报告里就看不到 JSON 了。
            payload = CasePayload(
                {
                    "project": project,
                    "file": str(yaml_file),
                    "case": item.case,
                    "params": item.params,
                    "param_name": item.param_name,
                    "hierarchy": hierarchy,
                    "depends_on": _extract_dependencies(case),
                },
                label=format_case_label(
                    case.get("name", ""),
                    item.param_name,
                    case.get("description", ""),
                    unique_suffix=unique_suffix,
                ),
            )
            params.append(pytest.param(payload))
            # 用例 ID 里不能带 "::"（那是 pytest 的节点分隔符），用 "#" 连接
            case_id = f"{relative_case_path(yaml_file).as_posix()}#{case['name']}"
            if item.param_name:
                case_id = f"{case_id}[{_safe_id(item.param_name)}]"
            ids.append(case_id)
    metafunc.parametrize("yaml_case", params, ids=ids)

    # 告诉用户有多少用例因为状态被跳过了 —— 静默跳过会让人以为用例丢了
    if skipped_by_status:
        from utils.cases.case_status import STATUS_LABELS

        parts = [
            f"{STATUS_LABELS.get(status, status)} {count} 条"
            for status, count in sorted(skipped_by_status.items())
        ]
        log.info("按调试状态跳过 %d 条用例（%s）", sum(skipped_by_status.values()), "，".join(parts))
        if not run_draft and not status_filter:
            log.info(
                "提示：这些用例没写 status: debugged，所以 CI 不会跑。"
                "本地想全跑可以加 --run-draft"
            )


def _unique_suffix(
    case: dict[str, Any],
    project: str,
    yaml_file: Path,
    used_suffixes: set[str],
) -> str:
    """给一条用例生成唯一标识（拼在 allure 参数值后面）。

    **为什么需要**

    allure 靠「用例名 + 参数值」算出一个指纹（historyId）来识别用例。
    同名的用例会算出同一个指纹，报告就把它们**合并成一条**：

    - 每个模块都有 ``not_exist``、``page`` 这类用例（19 个不同的名字）
    - 实际跑了 148 条，报告上只剩 19 条

    指纹只影响报告的统计和去重，不影响用例本身的执行。

    **怎么生成**

    优先用用例编号（``case_id``，比如 ``API-168``）—— 它天然唯一。
    没有编号就退回「模块名 + 接口路径」，也够用。
    万一还是撞了（``used_suffixes`` 里有），加序号直到不重复。
    """
    # 1) 用例编号 —— Excel 用例里带的，最可靠
    suffix = str(case.get("case_id") or "").strip()

    # 2) 退回：模块名 + 第一个接口路径
    if not suffix:
        module = ""
        try:
            rel = relative_case_path(yaml_file)
            if len(rel.parts) >= 2:
                module = rel.parts[1]
        except (ValueError, TypeError):
            module = ""

        steps = case.get("steps") or []
        path = ""
        for step in steps:
            if step.get("path"):
                path = str(step["path"])
                break

        suffix = f"{module}:{path}" if module and path else (module or path)

    # 3) 还是重复 —— 加序号
    if not suffix or suffix in used_suffixes:
        base = suffix or "case"
        counter = 2
        while f"{base}#{counter}" in used_suffixes:
            counter += 1
        suffix = f"{base}#{counter}"

    used_suffixes.add(suffix)
    return suffix


def _status_allows(
    case: dict[str, Any],
    run_draft: bool,
    status_filter: set[str],
) -> bool:
    """这条用例按当前状态要不要跑。

    Args:
        case: 用例数据
        run_draft: 是否连未调试的也跑
        status_filter: 指定状态白名单（给了就只认这些）
    """
    from utils.cases.case_status import resolve_status

    status = resolve_status(case)

    if status_filter:
        return status in status_filter

    if run_draft:
        return True

    from utils.cases.case_status import RUNNABLE_STATUSES

    return status in RUNNABLE_STATUSES


def _extract_dependencies(case: dict[str, Any]) -> list[str]:
    """取一条用例声明的依赖（``depends_on``）。"""
    from utils.cases.case_dependency import extract_dependencies

    return extract_dependencies(case)


def _sort_by_dependencies(
    loaded: list[tuple[str, Path, dict[str, Any]]],
) -> list[tuple[str, Path, dict[str, Any]]]:
    """按依赖关系重排用例顺序。

    默认顺序是「模块 order → 文件名」。但如果用例 A 声明了
    ``depends_on: [B]``，那 B 必须排在 A 前面 —— 否则 A 跑的时候
    B 造的数据还不存在。

    有循环依赖时**不静默跳过**，而是直接报错 —— 循环依赖是配置错误，
    硬着头皮跑只会得到一堆莫名其妙的失败。
    """
    if not loaded:
        return loaded

    try:
        graph = build_graph_from_cases(loaded)
    except Exception as exc:  # noqa: BLE001
        log.warning("构建依赖图失败，按原始顺序执行: %s", exc)
        return loaded

    problems = graph.validate()
    if problems:
        raise DependencyError(
            "用例依赖关系有问题：\n"
            + "\n".join(f"  - {p}" for p in problems)
        )

    # 按拓扑顺序重排；同一项目内的依赖才有效，跨项目的保持原顺序
    order_index = {
        name: index
        for index, name in enumerate(graph.topological_order())
    }

    # 没有依赖的用例保持原位置；有依赖的按拓扑序插入
    # 简单做法：整体按拓扑序排，没被拓扑序覆盖的（跨项目）留在原位
    original = {name: index for index, (_p, _f, d) in enumerate(loaded) for name in [str((d or {}).get("testcase", {}).get("name") or "")]}

    def sort_key(item: tuple[str, Path, dict[str, Any]]) -> tuple[int, int]:
        project, _path, data = item
        name = str((data or {}).get("testcase", {}).get("name") or "")
        if name in order_index:
            return (0, order_index[name])
        return (1, original.get(name, 0))

    result = sorted(loaded, key=sort_key)

    summary = graph.summary()
    if "没有用例依赖关系" not in summary:
        log.info("用例依赖：%s", summary)

    return result


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """收集完成后：把标签打到测试项上。

    标签有两个来源，会合并：
    - **模块级**：``modules.yaml`` 里模块配的 ``marks``（比如整个 auth 模块都算 auth）
    - **用例级**：YAML 里用例自己写的 ``marks``

    这样 ``pytest -m smoke`` 就能筛了。

    ⚠️ **必须是 tryfirst**。pytest 自带的 ``-m`` 过滤也是挂在
    ``pytest_collection_modifyitems`` 上的（``_pytest.mark`` 插件），
    而且它的实现里直接调 ``deselect_by_mark``。插件按注册顺序执行，
    核心插件先注册 —— 只要本函数晚于它跑，标记就是在"筛完之后"才补上的，
    结果 ``-m smoke`` 永远一个都选不中（106 deselected / 退出码 5）。
    """
    for item in items:
        case_param = getattr(item, "callspec", None)
        if case_param is None:
            continue

        # 从参数里把用例数据取出来
        payload = None
        for value in case_param.params.values():
            if isinstance(value, dict) and "case" in value:
                payload = value
                break
        if payload is None:
            continue

        case = payload["case"].get("testcase") or {}
        project = payload.get("project", "")
        yaml_file = Path(payload.get("file", ""))

        # 1) 模块级标签
        module = ""
        try:
            rel = relative_case_path(yaml_file)
            if len(rel.parts) >= 2:
                module = rel.parts[1]
        except (ValueError, TypeError):
            pass

        all_marks: list[str] = []
        if module:
            all_marks.extend(ModuleManager.get_marks(project, module))

        # 2) 用例级标签
        all_marks.extend(extract_marks(case))

        # 去重但保持顺序
        seen: set[str] = set()
        for mark_name in all_marks:
            if mark_name in seen:
                continue
            seen.add(mark_name)
            item.add_marker(mark_name)

        # 3) 把用例名、参数组名塞进 pytest 的额外关键字
        #
        # 为什么要这一步：pytest 的 ``-k`` 默认只认测试项 ID 里的名字，
        # 而中文在 ID 里会被转义成 \uXXXX，所以 ``-k "近七天"`` 一个都选不中。
        # 塞进 extra_keyword_matches 之后，``-k`` 可以直接用中文筛：
        #   python scripts/run.py -k "近七天"
        #   python scripts/run.py -k "订单统计"
        for keyword in _search_keywords(case, payload):
            item.extra_keyword_matches.add(keyword)

        # 4) 跳过标记
        #
        # 用例里写了 ``skip: "原因"`` 就跳过 —— 报告里能看到跳过理由。
        # （比如"这个功能还没上线"、"被测环境暂时不可用"）
        skip_reason = case.get("skip")
        if skip_reason:
            item.add_marker(pytest.mark.skip(reason=str(skip_reason)))

        # 5) allure 层级
        #
        # ⚠️ 必须在**收集阶段**用 allure.label 标记，而不是运行时
        # allure.dynamic —— 因为 pytest 插件会自动加 suite="test_cases"，
        # 运行时再追加就会变成两个 suite，报告的树就散了。
        # 收集阶段加的会取代 pytest 默认值。
        if has_allure():
            hierarchy = payload.get("hierarchy") or {}
            if hierarchy.get("epic"):
                item.add_marker(allure.label("parentSuite", hierarchy["epic"]))
            if hierarchy.get("feature"):
                item.add_marker(allure.label("suite", hierarchy["feature"]))
            if hierarchy.get("story"):
                item.add_marker(allure.label("subSuite", hierarchy["story"]))
            # 严重级别（扁平格式里的「优先级」列）
            severity = case.get("severity")
            if severity:
                item.add_marker(allure.label("severity", str(severity)))

        # 6) 把层级挂在测试项上，给 HTML 报告的表格用
        #
        # 两种传递方式，各有用途：
        # - ``apitest_hierarchy`` 属性：给能拿到 item 的场景用
        # - ``user_properties``：给 pytest-html 用
        #   （HTML 报告的 hook 只拿得到 TestReport，拿不到 item）
        hierarchy = payload.get("hierarchy") or {}
        setattr(item, "apitest_hierarchy", hierarchy)

        # user_properties 里的值 pytest 会转成字符串，这里统一存字符串
        if hierarchy.get("feature"):
            item.user_properties.append(("apitest_module", str(hierarchy["feature"])))
        if hierarchy.get("story"):
            item.user_properties.append(("apitest_story", str(hierarchy["story"])))
        if hierarchy.get("title"):
            item.user_properties.append(("apitest_title", str(hierarchy["title"])))

        # 模块信息也记到报告里，方便按模块看
        if module:
            item.user_properties.append(("module", module))
            item.user_properties.append(("module_order", str(ModuleManager.get_order(project, module))))


def _search_keywords(case: dict[str, Any], payload: dict[str, Any]) -> list[str]:
    """收集这条用例「能被 -k 搜到」的关键字。

    包括：用例名、参数组名、标签、模块名。
    全是中文 —— 因为中文在 pytest 的测试项 ID 里会被转义成 \\uXXXX，
    不额外塞进关键字的话 ``-k "近七天"`` 一个都匹配不到。
    """
    keywords: list[str] = []

    def add(value: Any) -> None:
        text = str(value).strip()
        if text and text not in keywords:
            keywords.append(text)

    add(case.get("name"))
    add(case.get("alias"))
    add(case.get("description"))
    add(payload.get("param_name"))

    # 标签本身也当关键字，`-k "flow"` 等价于 `-m flow`
    for mark in extract_marks(case):
        add(mark)

    return keywords


def _safe_id(text: str) -> str:
    """把参数组名字收拾成能安全放进 pytest ID 的形式。

    pytest 会把非 ASCII 字符转义成 \\uXXXX，显示出来是一堆乱码。
    这里把中文原样保留（pytest 支持），只过滤掉会破坏 ID 的字符。
    """
    # 这些字符会破坏 pytest 的 ID 解析（: 和 # 是分隔符）
    cleaned = text.replace(":", "-").replace("#", "-")
    # 去掉控制字符和换行
    cleaned = "".join(ch for ch in cleaned if ch.isprintable() or ch == " ")
    return cleaned.strip() or "param"
