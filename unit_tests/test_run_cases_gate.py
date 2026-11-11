"""``run_cases`` 的条数闸在异常路径上必须 fail-closed。

以前 ``_count_matching`` 吞掉所有异常并返回 0：
``matched > MAX_CASES_PER_RUN`` 不成立、``run_budget.check(0)`` 也放行 ——
只要用例目录里有一个读不了的文件，单次闸和会话闸就同时消失，
模型可以以「统计不了」为由跑全量。这里锁住修正后的行为。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai.agent.tools import _common, execution, run_budget
from ai.agent.tools.registry import ToolError


def _boom(*_args, **_kwargs):
    raise RuntimeError("xlsx 读不了")


def test_load_file_strict_raises_on_bad_file(tmp_path, monkeypatch):
    """坏文件在数条数时不能悄悄当 0 —— 非严格跳过，严格必抛（含缓存命中）。"""
    import testcases.case_loader as loader

    bad = tmp_path / "bad.xlsx"
    bad.write_bytes(b"not an xlsx")

    def _raise(*_args, **_kwargs):
        raise RuntimeError("解析失败")

    monkeypatch.setattr(loader, "load_cases_from_file", _raise)

    assert _common._load_file(bad, "mall4j") == []
    with pytest.raises(RuntimeError):
        _common._load_file(bad, "mall4j", strict=True)


def test_count_matching_raises_instead_of_returning_zero(monkeypatch):
    monkeypatch.setattr(_common, "iter_cases", _boom)
    with pytest.raises(ToolError) as exc:
        execution._count_matching("mall4j", "admin_area", "", "", "", "")
    assert "无法统计" in str(exc.value)


def test_count_matching_still_counts_explicit_case_ids(monkeypatch):
    """给了 case_id 就不用读目录，按编号数 —— 闸仍然有效。"""
    monkeypatch.setattr(_common, "iter_cases", _boom)
    assert execution._count_matching("", "", "", "", "", "API-1, API-2") == 2


def test_failed_count_blocks_run_cases(monkeypatch):
    """统计失败时 run_cases 直接拒绝，不能悄悄跑。"""
    monkeypatch.setattr(_common, "iter_cases", _boom)
    with pytest.raises(ToolError):
        execution.run_cases(module="admin_area")


class _Proc:
    returncode = 0
    stdout = ""
    stderr = ""


def _drive(monkeypatch, **kwargs) -> list[str]:
    """跑一遍 run_cases，返回真正传给 run.py 的命令行。"""
    captured: dict[str, list[str]] = {}

    def fake_run(cmd, **_kwargs):
        captured["cmd"] = list(cmd)
        return _Proc()

    monkeypatch.setattr(execution.subprocess, "run", fake_run)
    monkeypatch.setattr(execution, "_count_matching", lambda *a, **k: 1)
    monkeypatch.setattr(run_budget, "check", lambda n: None)
    monkeypatch.setattr(run_budget, "add", lambda n: 0)

    execution.run_cases(**kwargs)
    return captured["cmd"]


def test_run_draft_is_forwarded_to_run_py(monkeypatch):
    """run_draft=True 必须变成子进程的 --run-draft。"""
    cmd = _drive(monkeypatch, case_id="API-1", run_draft=True)
    assert "--run-draft" in cmd


def test_run_draft_is_off_by_default(monkeypatch):
    cmd = _drive(monkeypatch, case_id="API-1")
    assert "--run-draft" not in cmd
