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

mark 的注册在 ``framework/pytest_plugin.py`` 的 ``pytest_configure`` 里
（``config.addinivalue_line("markers", ...)``），本模块只负责解析。
"""
from __future__ import annotations

import re
from typing import Any

#: 字符串写法 ``"smoke and p0"`` 里的分隔符。
#:
#: ⚠️ 必须按「整词」匹配。以前是 ``value.replace("and", " ")`` ——
#: 标签里只要含 and/or 就被切碎：``order`` 变成 ``der``、``brand``
#: 变成 ``br``，``-m order`` 一条都筛不到。
_MARK_SEPARATOR = re.compile(r"\b(?:and|or)\b|[,，]")

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
            # "smoke and p0" -> ["smoke", "p0"]；"order" 保持 "order"
            parts = _MARK_SEPARATOR.split(value)
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
