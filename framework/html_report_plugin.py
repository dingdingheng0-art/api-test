"""pytest-html 报告增强：给表格加「模块 / 场景 / 用例」三列。

**为什么要加**

pytest-html 4.x 的表格默认只有 "Result / Test / Duration / Links"，
所有用例平铺在一起，用例 ID 又很长（带完整路径和参数组），
几十条用例就没法看了。

这里把用例的业务信息拆成三列：

.. code-block:: text

    | Result | 模块     | 场景         | 用例名                   | Duration | Links |
    | ------ | -------- | ------------ | ------------------------ | -------- | ----- |
    | Passed | 认证登录 | 账号密码登录 | 正向-正确账密             | 210 ms   |       |
    | Passed | 认证登录 | 账号密码登录 | 反向-错误账密[密码错误]    | 8 s      |       |

**信息从哪来**

用例 ID（nodeid）本身就带全了：

.. code-block:: text

    mall4j/auth/login.yaml#正向-正确账密[密码错误]
    └──┬──┘ └─┬─┘        └──┬───┘ └───┬───┘
      项目  模块    文件          用例名    参数组

所以直接从 nodeid 解析，不用额外传数据。
"""
from __future__ import annotations

import html as html_mod
import re
from functools import lru_cache
from typing import Any

import pytest

# 从 nodeid 里抠出「项目/模块/文件#用例名[参数组]」
#
# nodeid 实际长这样：
#   testcases/test_cases.py::test_api_case[mall4j/auth/login.yaml#正向-正确账密]
#
# 注意两点：
#   1) 前面有 pytest 的文件路径和函数名，要跳过
#   2) 中文被 pytest 转义成 \uXXXX，要先还原
_CASE_SEGMENT_RE = re.compile(
    r"(?P<project>[^/\[\]#]+)/(?P<module>[^/\[\]#]+)/(?P<file>[^\]#]+?)\.(?:yaml|yml|xlsx)"
    r"#(?P<case>.+?)(?:\[(?P<param>[^\]]*)\])?\]?$"
)

_UNICODE_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})|\\u\{([0-9a-fA-F]+)\}")


def _unescape_unicode(text: str) -> str:
    """把 ``\\uXXXX`` 还原成真正的字符。"""

    def repl(match: re.Match[str]) -> str:
        hex_str = match.group(1) or match.group(2)
        try:
            return chr(int(hex_str, 16))
        except ValueError:
            return match.group(0)

    return _UNICODE_ESCAPE.sub(repl, text)


# ``optionalhook=True`` 不能省：
#
# 这两个钩子是 pytest-html 定义的，而本插件在根 conftest 里**无条件**加载。
# 没装 pytest-html（比如只装了核心依赖）时，pytest 会报
# ``PluginValidationError: unknown hook 'pytest_html_results_table_header'``，
# 整个会话直接起不来 —— 明明只是少一个报告插件。
@pytest.hookimpl(optionalhook=True)
def pytest_html_results_table_header(cells: list[Any]) -> None:
    """给表头加三列。"""
    # 默认表头: Result / Test / Duration / Links（都是 <th>）
    # 在 Test 前面插三列（模块 / 场景 / 用例）
    new_cells = [
        '<th class="sortable" data-column-type="apitest-module">模块</th>',
        '<th class="sortable" data-column-type="apitest-story">场景</th>',
        '<th class="sortable" data-column-type="apitest-case">用例</th>',
    ]
    cells[1:1] = new_cells


@pytest.hookimpl(optionalhook=True)
def pytest_html_results_table_row(report: Any, cells: list[Any]) -> None:
    """给每行填三列的内容。

    顺便把 pytest 默认那列超长的用例 ID 换成简短好读的一行
    （默认那列是 ``testcases/test_cases.py::test_api_case[...]``，
    路径长、中文还被转义成 ``\\uXXXX``，很难看）。
    """
    info = resolve_row_info(report)

    new_cells = [
        f'<td class="col-apitest-module">{_esc(info["module"])}</td>',
        f'<td class="col-apitest-story">{_esc(info["story"])}</td>',
        f'<td class="col-apitest-case">{_esc(info["case"])}</td>',
    ]
    cells[1:1] = new_cells

    # 把默认的 testId 单元格换成简短 ID
    short_id = info.get("short_id") or ""
    if short_id:
        for index, cell in enumerate(cells):
            if 'class="col-testId"' in cell:
                cells[index] = f'<td class="col-testId">{_esc(short_id)}</td>'
                break


def resolve_row_info(report: Any) -> dict[str, str]:
    """拿一行报告的「模块 / 场景 / 用例名」。

    **优先用收集阶段算好的层级**（存在 ``user_properties`` 里）——
    那是权威来源，YAML 和 Excel 都有，Excel 用例也能正确显示模块和场景。

    拿不到才回退到从 nodeid 解析（老数据 / 手工构造的报告）。
    """
    nodeid = getattr(report, "nodeid", "") or ""
    props = _user_properties(report)
    module = props.get("apitest_module", "")
    story = props.get("apitest_story", "")

    # 收集阶段已经把场景写进 user_properties 了 —— 这时**不要**再去
    # 扫用例文件找 story，否则每行报告都要把一个模块的 Excel 重新解析
    # 一遍（132 行实测 1.87s，全是白费）。
    info = parse_nodeid(nodeid, need_story=not story)

    if module:
        info["module"] = module
    if story:
        info["story"] = story

    return info


def _user_properties(report: Any) -> dict[str, str]:
    """把 report.user_properties 转成字典。"""
    result: dict[str, str] = {}
    try:
        for key, value in report.user_properties:
            result[str(key)] = str(value)
    except Exception:  # noqa: BLE001
        pass
    return result


def _case_name_from_nodeid(nodeid: str) -> str:
    """从 nodeid 里取用例名（不含参数组）。"""
    nodeid = _unescape_unicode(nodeid)
    match = _CASE_SEGMENT_RE.search(nodeid)
    if match:
        return match.group("case") or ""
    return ""

def parse_nodeid(nodeid: str, need_story: bool = True) -> dict[str, str]:
    """从 nodeid 解析出模块 / 场景 / 用例名。

    .. code-block:: text

        testcases/test_cases.py::test_api_case[mall4j/auth/login.yaml#正向-正确账密]
        -> module = 认证登录
           story  = 账号密码登录
           case   = 正向-正确账密
    """
    # 先把中文还原
    nodeid = _unescape_unicode(nodeid)

    match = _CASE_SEGMENT_RE.search(nodeid)
    if not match:
        # 认不出来时：用例名显示 nodeid 的最后一段（别把整条路径糊在表格里）
        tail = nodeid.rsplit("::", 1)[-1]
        return {
            "module": "",
            "story": "",
            "case": tail,
            "short_id": tail,
        }

    project = match.group("project")
    module = match.group("module")
    file_name = match.group("file")
    case = match.group("case") or ""
    param = match.group("param")

    # 用例名带上参数组
    full_case = f"{case}[{param}]" if param else case

    # 模块显示名（module.yaml 里的 label）
    from utils.cases.module_manager import ModuleManager

    module_label = ModuleManager.get_label(project, module) or module

    # 场景：用例的 allure.story（拿不到就用用例名）。
    # ``need_story=False`` 时调用方已经有权威的 story，跳过整目录扫描。
    story = (_story_of(project, module, case) if need_story else "") or case

    # 简短 ID：项目/模块/文件#用例名（不带 pytest 那长串路径）
    short_id = f"{project}/{module}/{file_name}#{full_case}"

    return {
        "module": module_label,
        "story": story,
        "case": full_case,
        "short_id": short_id,
    }


def _story_of(project: str, module: str, case_name: str) -> str:
    """找用例定义的 allure.story（按模块缓存，见 ``_module_stories``）。"""
    return _module_stories(project, module).get(case_name, "")


@lru_cache(maxsize=256)
def _module_stories(project: str, module: str) -> dict[str, str]:
    """一个模块里「用例名 → allure.story」的映射。

    YAML 和 Excel 都要扫 —— Excel 里的用例同样带 ``allure.story``，
    只扫 YAML 会让 Excel 用例的场景列空着。

    **必须缓存**：这个函数以前每条报告行都调一次，等于每行都重新打开
    模块目录下的每本 Excel（openpyxl 全量解析）。行数 × 文件数是平方级
    增长 —— 132 行就要 1.87s，上千条用例会拖到十几秒。
    一个模块的文件只需要解析一次。
    """
    from testcases.case_loader import load_cases_from_file
    from utils.cases.module_manager import ModuleManager

    stories: dict[str, str] = {}

    # 模块目录按"用例来源"决定在哪儿（cases/ 还是 cases_excel/）
    module_dir = ModuleManager._module_dir(project, module)
    if not module_dir.exists():
        return stories

    files = sorted(module_dir.glob("*.yaml")) + sorted(module_dir.glob("*.xlsx"))

    for case_file in files:
        if case_file.name == "module.yaml":
            continue
        try:
            cases = load_cases_from_file(case_file)
        except Exception:  # noqa: BLE001
            continue

        for item in cases:
            if not isinstance(item, dict):
                continue
            tc = item.get("testcase") or {}
            story = (tc.get("allure") or {}).get("story")
            name = tc.get("name")
            if name and story:
                stories[str(name)] = str(story)
    return stories


def _esc(text: str) -> str:
    """HTML 转义。"""
    return html_mod.escape(str(text))


__all__ = ["pytest_html_results_table_header", "pytest_html_results_table_row", "parse_nodeid"]
