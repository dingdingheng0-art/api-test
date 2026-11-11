"""Session 判定的单测：``pytest unit_tests`` 不能被当成接口 session。

根 conftest 无条件加载框架插件，所以这四个判断决定了单测会不会
连带连数据库、加载工厂、重试用例、覆盖 ``reports/result.json``。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from framework.session_scope import targets_interface_tests

ROOT = Path(__file__).resolve().parent.parent


def _config(args: list[str] | None = None, testpaths: list[str] | None = None) -> Any:
    """造一个够用的 config（只实现 helper 会碰到的属性）。"""
    invocation = None if args is None else SimpleNamespace(args=list(args))
    return SimpleNamespace(
        rootpath=ROOT,
        invocation_params=invocation,
        getini=lambda name: list(testpaths or []) if name == "testpaths" else [],
    )


def test_no_args_uses_testpaths_unit_tests() -> None:
    """裸跑 ``pytest``：testpaths 指向 unit_tests → 不是接口 session。"""
    assert targets_interface_tests(_config(args=[], testpaths=["unit_tests"])) is False


def test_args_unit_tests_dir() -> None:
    assert targets_interface_tests(_config(args=["unit_tests", "-q"])) is False


def test_args_single_unit_test_file() -> None:
    assert targets_interface_tests(_config(args=["unit_tests/test_schema.py"])) is False


def test_args_case_file_nodeid() -> None:
    """``testcases/test_cases.py::test_api_case[...]`` 也是接口用例。"""
    cfg = _config(args=["testcases/test_cases.py::test_api_case[x]"])
    assert targets_interface_tests(cfg) is True


def test_args_testcases_subdir() -> None:
    """以前只认 ``testcases`` 目录本身，子目录会被漏判。"""
    assert targets_interface_tests(_config(args=["testcases/cases_excel"])) is True


def test_args_project_root() -> None:
    """scripts/run.py 传的是项目根 —— 接口用例在里面。"""
    assert targets_interface_tests(_config(args=[str(ROOT)])) is True


def test_no_args_without_testpaths_is_interface() -> None:
    """没配 testpaths 时 pytest 收 rootdir（含 testcases/）。"""
    assert targets_interface_tests(_config(args=[], testpaths=[])) is True


def test_stub_config_defaults_to_interface() -> None:
    """拿不到判断依据（单测里的 stub config）→ 宁可多初始化。"""

    class _Config:
        def getoption(self, _name: str, default: Any = None) -> Any:
            return default

    assert targets_interface_tests(_Config()) is True
