"""结果统计口径的单测：teardown 的异常必须算进 result.json。

``pytest_terminal_summary`` 里的统计以前用 ``when != "teardown"`` 过滤，
清理阶段报错的用例在 ``reports/result.json`` 里仍是"执行通过"，
而 pytest 退出码是 1 —— CI 和自愈流程都会看错。
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from framework.hooks import _final_counts


def _report(nodeid: str, when: str) -> Any:
    return SimpleNamespace(nodeid=nodeid, when=when, id=nodeid)


def test_teardown_error_counts_as_error() -> None:
    stats = {"passed": [_report("a", "call")], "error": [_report("a", "teardown")]}
    assert _final_counts(stats) == (0, 0, 1, 0)


def test_call_failure_with_teardown_error_counts_once() -> None:
    """call 失败 + teardown 报错 → 一条 failed，不能算成 failed 1 + error 1。"""
    stats = {
        "failed": [_report("a", "call")],
        "error": [_report("a", "teardown")],
    }
    assert _final_counts(stats) == (0, 1, 0, 0)


def test_setup_failure_and_skipped_teardown() -> None:
    stats = {
        "error": [_report("a", "setup")],
        "skipped": [_report("a", "teardown")],
        "passed": [_report("b", "call")],
    }
    assert _final_counts(stats) == (1, 0, 1, 0)


def test_passed_teardown_does_not_double_count() -> None:
    stats = {
        "passed": [_report("a", "call"), _report("a", "teardown")],
    }
    assert _final_counts(stats) == (1, 0, 0, 0)


def test_empty_stats() -> None:
    assert _final_counts({}) == (0, 0, 0, 0)
