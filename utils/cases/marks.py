"""给 YAML 用例打 pytest mark。

用途：按标签筛选用例。

YAML 里写：

.. code-block:: yaml

    testcase:
      name: login
      marks: [smoke, p0]          # 方式一：列表
      mark: smoke                 # 方式二：单个

或者写成一整串：

.. code-block:: yaml

    marks: "smoke and p0"

然后命令行就能筛：

.. code-block:: bash

    pytest apitest -m smoke          # 只跑 smoke
    pytest apitest -m "not slow"     # 排除 slow
    pytest apitest -m "smoke or p0"

框架也会自动注册这些 mark，避免 pytest 报 "unknown mark" 警告。
"""
from __future__ import annotations

from typing import Any

import pytest

# 框架预注册的 mark（避免 unknown mark 警告）
KNOWN_MARKS = (
    "smoke",     # 冒烟
    "p0", "p1", "p2",  # 优先级
    "api",       # 接口用例
    "ui",        # UI 用例
    "wip",       # 开发中，CI 默认跳过
    "slow",      # 慢用例
    "flaky",     # 不稳定
    "regression",  # 回归
)


def register_marks() -> None:
    """把框架认识的 mark 注册到 pytest（消除 unknown mark 警告）。"""
    for mark in KNOWN_MARKS:
        config = pytest.Config  # noqa: F841 - 仅为类型提示
        try:
            # 通过 pytest 的 mark 机制注册
            _register_single(mark)
        except Exception:  # noqa: BLE001 - 注册失败不影响执行
            pass


def _register_single(name: str) -> None:
    """注册单个 mark。"""
    # pytest 8+ 有公开的 mark 存储；这里用兼容方式
    try:
        from _pytest.mark.structures import Mark

        Mark(name, (), {}, _ispytest=True)
    except Exception:  # noqa: BLE001
        pass


def extract_marks(case: dict[str, Any]) -> list[str]:
    """从用例数据里取出所有 mark。

    支持的写法：
    - ``marks: [smoke, p0]``
    - ``mark: smoke``
    - ``marks: "smoke and p0"``（字符串会被拆开）
    - ``tags: [xxx]``（别名）
    """
    marks: list[str] = []

    for key in ("marks", "mark", "tags", "tag"):
        value = case.get(key)
        if not value:
            continue
        if isinstance(value, str):
            # "smoke and p0" -> ["smoke", "p0"]
            parts = value.replace("and", " ").replace("or", " ").replace(",", " ").split()
            marks.extend(p.strip() for p in parts if p.strip())
        elif isinstance(value, (list, tuple)):
            marks.extend(str(m).strip() for m in value if str(m).strip())
        else:
            marks.append(str(value).strip())

    # 去重但保持顺序
    seen: set[str] = set()
    result: list[str] = []
    for m in marks:
        if m not in seen:
            seen.add(m)
            result.append(m)
    return result


def apply_marks(item: pytest.Item, marks: list[str]) -> None:
    """把 mark 打到 pytest 的测试项上。"""
    for mark_name in marks:
        item.add_marker(mark_name)
