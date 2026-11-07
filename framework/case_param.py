"""用例参数的展示格式。

**为什么有这个文件**

报告里显示参数时，框架把整条用例数据（项目、文件、步骤、断言……）
当成了 pytest 的参数传下去。结果 allure 报告的「参数」区就变成一大坨 JSON：

.. code-block:: text

    yaml_case: {'project': 'mall4j', 'file': 'C:\\...\\login.yaml',
                'case': {'testcase': {'name': '正向-正确账密', 'steps': [...]}}}

难看，而且没信息量 —— 真正想看的只是「这条用例叫什么、带哪组参数」。

**怎么解决**

这里提供两个小工具：

- :class:`CasePayload` —— 包的字典。数据还是完整的（框架照常读它），
  但 ``repr()`` 只返回一行简短文字，报告里显示的就是文字而不是 JSON。
- :func:`format_params` —— 把参数组字典拼成 ``searchType=LAST_SEVEN`` 这种文字。
"""

from __future__ import annotations

from typing import Any


class CasePayload(dict):
    """用例数据容器：内容完整，但报告里只显示一行文字。

    参数化时用它替代普通 dict，报告里的「参数」区就不会再冒出整坨 JSON。

    用法::

        payload = CasePayload({"project": "mall4j", ...}, label="订单统计-按周期[近七天]")
    """

    def __init__(self, data: dict[str, Any], label: str = "") -> None:
        super().__init__(data)
        self._label = label or ""

    def __repr__(self) -> str:
        """报告里显示的就是这个 —— 一行文字，不是 JSON。"""
        return self._label or super().__repr__()

    def __str__(self) -> str:
        return self.__repr__()


def format_params(params: dict[str, Any] | None, separator: str = ", ") -> str:
    """把参数组字典拼成简短文字。

    ``{"searchType": "LAST_SEVEN", "pageSize": 10}``
    → ``searchType=LAST_SEVEN, pageSize=10``

    空字典返回空串（调用方可以直接跳过显示）。
    """
    if not params:
        return ""

    parts: list[str] = []
    for key, value in params.items():
        # __param_name__ 是框架内部字段（参数组名字），不展示
        if key.startswith("__"):
            continue
        parts.append(f"{key}={value}")
    return separator.join(parts)


def format_case_label(
    name: str,
    param_name: str = "",
    description: str = "",
    unique_suffix: str = "",
) -> str:
    """拼一条用例在报告里显示的短标签。

    优先用「用例名 + 参数组名」；没有名字就用说明。

    **为什么要加 ``unique_suffix``**

    allure 靠「用例名 + 参数值」算指纹来识别一条用例。同名的用例
    （比如每个模块都有 ``not_exist``）会算出同一个指纹，报告就把它们
    合并成一条 —— 明明跑了 148 条，报告上只剩 19 条。

    所以这里把用例编号（唯一）拼进标签，让指纹互不相同。
    编号只影响 allure 的指纹计算，报告里显示的标题不受影响。
    """
    label = name or description
    if param_name:
        label = f"{label}[{param_name}]" if label else f"[{param_name}]"
    if unique_suffix:
        label = f"{label}#{unique_suffix}" if label else f"#{unique_suffix}"
    return label


__all__ = ["CasePayload", "format_params", "format_case_label"]
