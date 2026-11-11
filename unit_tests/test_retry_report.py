"""重试后 allure 报告要"干净"。

重试是通过再调一次 ``item.runtest()`` 实现的，allure 的用例容器在这期间
一直开着。第一次失败时贴的请求/响应、失败归因、异常详情，以及已经
关掉的 step，都会留在同一个 TestResult 里 —— 最终"通过"的用例会挂着
红色的失败证据，步骤还重复两遍。

这里测的是重试前的截断逻辑：只删「本次尝试新增」的部分。
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from framework.plugins.retry_plugin import (  # noqa: E402
    _drop_failed_attempt,
    _snapshot_allure,
)


class _FakeResult:
    """够用的 TestResult 替身（只关心那四个列表）。"""

    def __init__(self) -> None:
        self.steps: list[object] = []
        self.attachments: list[object] = []
        self.labels: list[object] = []
        self.links: list[object] = []


def test_drop_only_removes_artifacts_added_by_failed_attempt():
    result = _FakeResult()
    result.steps.append("已有的前置步骤")
    result.attachments.append("已有的附件")

    baseline = _snapshot_allure(result)

    # 一次失败尝试新增的内容
    result.steps.extend(["发送请求", "校验结果"])
    result.attachments.append("异常详情")
    result.labels.append("用例来源")

    dropped = _drop_failed_attempt(result, baseline)

    assert dropped == 4
    assert result.steps == ["已有的前置步骤"]
    assert result.attachments == ["已有的附件"]
    assert result.labels == []
    assert result.links == []


def test_drop_is_noop_without_allure():
    assert _snapshot_allure(None) is None
    assert _drop_failed_attempt(None, None) == 0

    result = _FakeResult()
    assert _drop_failed_attempt(result, None) == 0
    assert result.steps == []


def test_drop_does_not_touch_non_list_attributes():
    result = _FakeResult()
    baseline = _snapshot_allure(result)
    result.steps = "not-a-list"  # type: ignore[assignment]

    # 结构不对时跳过，不能抛异常把重试流程打断
    assert _drop_failed_attempt(result, baseline) == 0
