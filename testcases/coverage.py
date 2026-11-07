"""覆盖率统计：知道"测了什么、漏了什么"。

**要回答的问题**

- 我这个项目有多少接口？多少有被测过？
- 哪些接口**一个用例都没有**？（这是最该补的）
- 哪些接口有接口定义但没有断言？（测了但没验，等于没测）
- 参数覆盖了多少？比如 searchType 的 4 个枚举值，只测了 2 个？

**两种覆盖口径**

.. code-block:: text

    接口覆盖率   = 被至少一条用例引用过的接口数 / 接口总数
    断言覆盖率   = 有断言的步骤数 / 总步骤数
    参数覆盖率   = 被用例实际用过的参数值数 / 参数理论取值数

**数据从哪来**

- **接口总数**：来自 Swagger / Apifox 同步（没同步过就是 0）
- **被测接口**：来自用例（YAML/Excel 里的步骤，或者数据库里的步骤表）
- **断言情况**：来自用例里的 ``assert``
- **参数取值**：来自用例里实际出现的参数值

**怎么用**

.. code-block:: python

    from testcases.coverage import calculate_coverage

    report = calculate_coverage(project="mall4j")
    print(report.summary())
    print(report.untested_apis())

**注意**：接口覆盖率的前提是"接口清单是全的"。只有一个接口都没同步过时，
这个数字没意义 —— 报告里会明确提示。

**为什么放在 testcases/ 而不是 utils/**

统计口径完全建立在"用例文件怎么加载"之上（扫描目录、读 YAML/Excel、
推项目名和模块名），这些是 :mod:`testcases.case_loader` 的知识。
放到 utils/ 就会反过来 import testcases/，分层就塌了
（见 ``docs/ARCHITECTURE.md`` 第五节和 ``TestLayering``）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from utils.logs.log_manager import log


@dataclass
class ApiCoverage:
    """单个接口的覆盖情况。"""

    api_id: int
    name: str
    method: str
    addr: str
    #: 被多少条用例引用
    used_by: int = 0
    #: 有几个步骤带断言
    asserted_steps: int = 0
    #: 有几个步骤（不含断言）
    total_steps: int = 0
    #: 来源：'file'（YAML/Excel）还是 'db'
    source: str = "file"

    @property
    def covered(self) -> bool:
        """这个接口有没有被测过。"""
        return self.used_by > 0

    @property
    def asserted(self) -> bool:
        """有没有断言（测了但没验 = 白测）。"""
        return self.asserted_steps > 0

    @property
    def quality(self) -> str:
        """覆盖质量。

        - ``untested`` —— 没被测过
        - ``no_assert``  —— 测了但没断言
        - ``partial``   —— 测了，部分步骤有断言
        - ``covered``   —— 测了且都有断言
        """
        if not self.covered:
            return "untested"
        if self.asserted_steps == 0:
            return "no_assert"
        if self.asserted_steps < self.total_steps:
            return "partial"
        return "covered"

    def to_dict(self) -> dict[str, Any]:
        return {
            "api_id": self.api_id,
            "name": self.name,
            "method": self.method,
            "addr": self.addr,
            "used_by": self.used_by,
            "asserted_steps": self.asserted_steps,
            "total_steps": self.total_steps,
            "covered": self.covered,
            "asserted": self.asserted,
            "quality": self.quality,
            "source": self.source,
        }


@dataclass
class CoverageReport:
    """覆盖率报告。"""

    project: str
    apis: list[ApiCoverage] = field(default_factory=list)
    #: 同步过来的接口总数（Swagger / Apifox）
    synced_api_total: int = 0
    #: 用例总数
    case_total: int = 0
    #: 参数覆盖：``{参数名: {"used": [...], "total": [...]}}``
    parameters: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: 警告信息（比如"接口清单不全"）
    warnings: list[str] = field(default_factory=list)

    # ---------------- 统计 ----------------

    @property
    def api_total(self) -> int:
        """接口总数。"""
        return len(self.apis)

    @property
    def covered_count(self) -> int:
        """被测过的接口数。"""
        return sum(1 for a in self.apis if a.covered)

    @property
    def asserted_count(self) -> int:
        """有断言的接口数。"""
        return sum(1 for a in self.apis if a.asserted)

    @property
    def api_rate(self) -> float:
        """接口覆盖率（0~1）。"""
        if not self.apis:
            return 0.0
        return self.covered_count / len(self.apis)

    @property
    def assert_rate(self) -> float:
        """断言覆盖率（0~1）—— 有断言的步骤 / 总步骤。"""
        total_steps = sum(a.total_steps for a in self.apis)
        asserted_steps = sum(a.asserted_steps for a in self.apis)
        if not total_steps:
            return 0.0
        return asserted_steps / total_steps

    def untested_apis(self) -> list[ApiCoverage]:
        """完全没测过的接口。"""
        return [a for a in self.apis if not a.covered]

    def no_assert_apis(self) -> list[ApiCoverage]:
        """测了但没断言的接口（白测）。"""
        return [a for a in self.apis if a.covered and not a.asserted]

    def by_quality(self) -> dict[str, list[ApiCoverage]]:
        """按覆盖质量分组。"""
        groups: dict[str, list[ApiCoverage]] = {
            "untested": [],
            "no_assert": [],
            "partial": [],
            "covered": [],
        }
        for api in self.apis:
            groups.setdefault(api.quality, []).append(api)
        return groups

    # ---------------- 输出 ----------------

    def summary(self) -> str:
        """给人看的摘要。"""
        lines = [
            f"项目「{self.project}」覆盖率报告",
            "",
            f"接口覆盖率: {self.covered_count}/{self.api_total} "
            f"({self.api_rate:.1%})",
            f"断言覆盖率: {self.assert_rate:.1%}",
            f"用例总数: {self.case_total}",
        ]

        groups = self.by_quality()
        lines.append("")
        lines.append("覆盖质量分布:")
        for quality, label in (
            ("covered", "已覆盖（有断言）"),
            ("partial", "部分覆盖（部分步骤没断言）"),
            ("no_assert", "只调用没断言（等于没测）"),
            ("untested", "完全没测"),
        ):
            items = groups.get(quality, [])
            lines.append(f"  {label}: {len(items)} 个")

        if self.warnings:
            lines.append("")
            lines.append("注意:")
            for warning in self.warnings:
                lines.append(f"  - {warning}")

        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        """转成能存 JSON 的结构。"""
        return {
            "project": self.project,
            "summary": {
                "api_total": self.api_total,
                "covered_count": self.covered_count,
                "asserted_count": self.asserted_count,
                "api_rate": round(self.api_rate, 4),
                "assert_rate": round(self.assert_rate, 4),
                "case_total": self.case_total,
                "synced_api_total": self.synced_api_total,
            },
            "apis": [a.to_dict() for a in self.apis],
            "parameters": self.parameters,
            "warnings": self.warnings,
        }


# ============================================================================
# 从文件统计
# ============================================================================


def collect_from_files(project: str, root: Any = None) -> tuple[list[ApiCoverage], int, dict[str, dict[str, Any]]]:
    """从 YAML / Excel 用例里统计覆盖情况。

    Returns:
        ``(接口列表, 用例数, 参数覆盖)``
    """
    from testcases.case_loader import (
        discover_cases,
        load_cases_from_file,
        relative_case_path,
    )

    # ``(method, addr) -> ApiCoverage``
    api_map: dict[tuple[str, str], ApiCoverage] = {}
    case_total = 0
    param_values: dict[str, set[str]] = {}

    for case_project, path in discover_cases(root):
        if project and case_project != project:
            continue

        try:
            cases = load_cases_from_file(path, root)
        except Exception as exc:  # noqa: BLE001 - 一个文件坏了不影响统计
            log.warning("统计覆盖时跳过 %s: %s", path.name, exc)
            continue

        rel = relative_case_path(path)
        module = rel.parts[1] if len(rel.parts) >= 2 else "default"

        for data in cases:
            case = data.get("testcase") or {}
            case_total += 1

            for step in case.get("steps") or []:
                if not isinstance(step, dict):
                    continue

                method = str(step.get("method") or "GET").upper()
                addr = str(step.get("path") or "")
                if not addr:
                    continue

                key = (method, addr)
                coverage = api_map.get(key)
                if coverage is None:
                    coverage = ApiCoverage(
                        api_id=0,
                        name=str(step.get("description") or addr),
                        method=method,
                        addr=addr,
                        source="file",
                    )
                    api_map[key] = coverage

                coverage.total_steps += 1
                coverage.used_by += 1

                if step.get("assert"):
                    coverage.asserted_steps += 1

                # 参数取值
                # ``params`` 两种写法都有：YAML 用 dict（``{k: v}``），
                # 扁平 Excel 解析出来是 list（``[{"name": ..., "value": ...}]``）
                for param_name, param_value in _iter_params(step.get("params")):
                    param_values.setdefault(param_name, set()).add(param_value)

    apis = list(api_map.values())
    return apis, case_total, _build_parameter_coverage(param_values)


def _iter_params(params: Any) -> list[tuple[str, str]]:
    """把步骤的 ``params`` 拍平成 ``[(参数名, 参数值), ...]``。

    ``params`` 有两种形态：

    - YAML 用例：``{"searchType": 1, "prodName": "x"}``
    - 扁平 Excel 解析出来的：``[{"name": "searchType", "value": 1}, ...]``

    两种都要能统计，否则 Excel 模式下参数覆盖率永远是空的。
    """
    if isinstance(params, dict):
        return [(str(k), str(v)) for k, v in params.items()]

    if isinstance(params, list):
        pairs: list[tuple[str, str]] = []
        for item in params:
            if not isinstance(item, dict):
                continue
            name = item.get("name") or item.get("key") or ""
            if not name:
                continue
            pairs.append((str(name), str(item.get("value", ""))))
        return pairs

    return []


def _build_parameter_coverage(values: dict[str, set[str]]) -> dict[str, dict[str, Any]]:
    """整理参数覆盖信息。"""
    result: dict[str, dict[str, Any]] = {}
    for name, used in values.items():
        result[name] = {
            "used_count": len(used),
            "used": sorted(used)[:50],
        }
    return result


# ============================================================================
# 从数据库统计
# ============================================================================


def calculate_coverage(
    project: str,
    root: Any = None,
) -> CoverageReport:
    """算某个项目的覆盖率。

    口径完全基于用例文件（YAML / Excel）：
    「接口总数」= 用例里出现过的接口，不代表被测系统的真实接口总数。

    Args:
        project: 项目名
        root: 用例根目录

    Returns:
        覆盖率报告
    """
    report = CoverageReport(project=project)

    report.apis, report.case_total, param_coverage = collect_from_files(
        project, root=root
    )
    report.parameters = param_coverage

    # 提醒
    if report.synced_api_total == 0:
        report.warnings.append(
            "还没同步过接口清单（Swagger / Apifox），"
            "现在的「接口总数」只是用例里出现过的接口，不代表项目真实接口数"
        )
    if report.api_total and report.api_rate < 0.5:
        report.warnings.append(
            f"接口覆盖率只有 {report.api_rate:.0%}，建议优先补齐没测过的接口"
        )

    return report
