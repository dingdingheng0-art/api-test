"""沙箱模块的生命周期提示（``ai/agent/tools/cases.py``）。

``ai_*`` 是 AI 生成用例的落脚点：默认全 draft、不进 CI，也**不会自动退役**。
以前 ``list_modules`` 只报条数，看不出「这堆是不是已经没人管了」——
沙箱目录就这么一直堆着。现在每个 ``ai_*`` 模块额外带
``sandbox / draft / debugged / idle_days / advice``。

这里锁两件事：

1. 判定逻辑本身（全 draft + 长期闲置 → 建议删）
2. ``list_modules`` 只给沙箱模块加这组字段，现役模块的输出一字不变
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai.agent.tools import cases  # noqa: E402


def _item(last_modified: float, **by_status) -> dict:
    from collections import Counter

    return {"by_status": Counter(by_status), "last_modified": last_modified}


def test_all_draft_and_stale_is_flagged_for_removal():
    now = time.time()
    state = cases._sandbox_state(_item(now - 60 * 86400, draft=7), now)
    assert state["sandbox"] is True
    assert state["draft"] == 7
    assert state["debugged"] == 0
    assert state["idle_days"] == 60
    assert "删掉" in state["advice"]


def test_all_draft_but_recent_asks_to_promote_or_clean():
    now = time.time()
    state = cases._sandbox_state(_item(now - 2 * 86400, draft=3), now)
    assert state["idle_days"] == 2
    assert "draft" in state["advice"]
    assert "删掉" not in state["advice"]


def test_promoted_but_stale_asks_to_rerun():
    now = time.time()
    state = cases._sandbox_state(_item(now - 45 * 86400, draft=1, debugged=2), now)
    assert state["debugged"] == 2
    assert "重跑" in state["advice"]


def test_no_mtime_is_treated_as_stale():
    """文件时间取不到时按「很久没动」处理 —— 宁可多提醒一次，也别漏掉。"""
    state = cases._sandbox_state(_item(0.0, draft=1), time.time())
    assert state["idle_days"] is None
    assert "删掉" in state["advice"]


def test_list_modules_marks_sandbox_modules_only(monkeypatch):
    """现役模块的输出不能变 —— 只有 ai_* 才带沙箱字段。"""

    class _Ref:
        def __init__(self, project, module, file, status="draft"):
            self.project = project
            self.module = module
            self.file = file
            self.status = status

    pairs = [
        (_Ref("mall4j", "admin_area", "testcases/cases_excel/mall4j/admin_area/admin_area.xlsx",
              "debugged"), {}),
        (_Ref("mall4j", "ai_area_chain", "testcases/cases_excel/mall4j/ai_area_chain/ai_area_chain.xlsx"),
         {}),
    ]
    monkeypatch.setattr(cases, "iter_cases", lambda **kwargs: pairs)

    result = cases.list_modules("mall4j")
    mods = {m["module"]: m for m in result["projects"][0]["modules"]}

    assert "sandbox" not in mods["admin_area"], "现役模块不该带沙箱字段"
    assert mods["ai_area_chain"]["sandbox"] is True
    assert mods["ai_area_chain"]["draft"] == 1
    assert result["sandbox_modules"] == ["mall4j/ai_area_chain"]
    assert "ai_*" in result["hint"]
