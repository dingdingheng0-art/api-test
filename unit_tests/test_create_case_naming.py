"""MCP ``create_case`` 的默认命名 + ``run_cases`` 的 draft 开关。

两处以前不一致，都会让「生成的用例看起来跑过了」：

1. ``case_gen`` 那条链路自己拼了「模块中文名 - 业务（场景）」，
   MCP 的 ``create_case`` 却只拼 ``api_name - title``，少一截模块前缀。
2. ``run_cases`` 不传 ``--run-draft``，而 ``create_case`` 的默认状态就是
   draft —— 验证时一条都不会执行，pytest 还返回 0，``total=0`` 很容易被
   当成"全部通过"。自愈验证 ``verify_heal`` 必须能跑 draft。
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai.agent.tools.case_io import _default_case_name


def test_default_case_name_uses_module_label():
    assert (
        _default_case_name("mall4j", "admin_area", "修改地区", "（正常请求）")
        == "地区管理 - 修改地区（正常请求）"
    )


def test_default_case_name_does_not_duplicate_business_name():
    """title 已经是整句时，不能拼成「删除地区 - 删除地区成功」。"""
    assert (
        _default_case_name("mall4j", "admin_area", "删除地区", "删除已存在的区域成功")
        == "地区管理 - 删除地区 - 删除已存在的区域成功"
    )


def test_default_case_name_strips_repeated_business_name():
    """旧调用风格把业务名写进 title 时，不能拼出两遍。"""
    assert (
        _default_case_name("mall4j", "admin_area", "修改地区", "修改地区（正常请求）")
        == "地区管理 - 修改地区（正常请求）"
    )
    assert (
        _default_case_name("mall4j", "admin_area", "修改地区", "修改地区")
        == "地区管理 - 修改地区"
    )


def test_default_case_name_falls_back_to_module():
    """新模块没配中文名时，用框架的英文显示名兜底（和报告里一致），不能崩。"""
    name = _default_case_name("mall4j", "not_a_real_module", "查询地区", "（正常请求）")
    assert name == "not a real module - 查询地区（正常请求）"


def test_run_cases_accepts_run_draft():
    """run_draft 必须是 run_cases 的显式参数（默认关，避免误跑 disabled）。"""
    import inspect

    from ai.agent.tools.execution import run_cases

    params = inspect.signature(run_cases).parameters
    assert "run_draft" in params
    assert params["run_draft"].default is False


def test_verify_heal_runs_draft_cases():
    """verify_heal 走 run_cases 时必须带 run_draft=True，否则永远验证不了 draft。"""
    import inspect

    from ai.agent.tools import heal

    source = inspect.getsource(heal.verify_heal)
    assert "run_draft=True" in source


def test_backup_prune_keeps_only_latest(tmp_path):
    """备份目录不能无限长 —— 只留最近 50 个，最旧的被删。"""
    from ai.agent.tools.case_io import BACKUP_KEEP, _prune_backups

    for idx in range(BACKUP_KEEP + 10):
        (tmp_path / f"agent_20260101_0000{idx:02d}_000").mkdir()
    (tmp_path / "not_a_backup").mkdir()

    _prune_backups(tmp_path)

    kept = sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("agent_"))
    assert len(kept) == BACKUP_KEEP
    assert kept[0].endswith("000010_000")  # 最旧的 10 个被清掉
    assert (tmp_path / "not_a_backup").exists()  # 不碰别的目录

