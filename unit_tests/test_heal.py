"""自愈闭环（P2）的单元测试。

**测的是「不许做什么」，不是「能做什么」**

自愈最危险的不是修不好，是**改绿**：把断言削到刚好能通过，用例绿了，
缺陷还在。所以这里的重点全在闸门上：

- 归因是服务问题 → 不允许改用例（代码拦，不是提示词）
- 断言变弱 → 拒绝落盘
- 同一用例改两轮还没跑通 → 停止
- 验证没通过 → 自动回滚

这些规则只要有一条失效，整套自愈就是在帮着掩盖缺陷。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    """``.cache`` 指到临时目录。

    自愈状态和备份目录都是**运行时产物**，测试往真实目录里写，
    等于往审计记录里塞假条目。
    """
    from utils import paths

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr(paths, "CACHE_DIR", cache_dir)

    from utils.infra import failure_classifier as fc

    monkeypatch.setattr(fc, "_FAILURES", {})
    monkeypatch.setattr(fc, "_failures_file", lambda: cache_dir / "attribution.jsonl")
    return cache_dir


def _record_failure(case_id: str, attribution: str, category: str = "服务端内部异常") -> None:
    """往归因登记里塞一条（模拟"上一轮跑挂了"）。"""
    from utils.infra import failure_classifier as fc

    diag = fc.FailureDiagnosis(
        attribution=attribution,
        category=category,
        case_id=case_id,
        module="mod",
        method="GET",
        url="/admin/x",
        server_code="A00005",
        server_msg="服务端异常",
    )
    fc.register_failure(f"testcases/test_cases.py::test_api_case[{case_id}]", "mod", diag)


# --------------------------------------------------------------------------
# 分派表：归因 → 允许的动作
# --------------------------------------------------------------------------


def test_service_problem_never_allows_case_edits():
    """服务问题不能改用例 —— 这是整个自愈的安全底线。"""
    from ai.agent.tools.heal import DECISION_BY_ATTRIBUTION

    decision = DECISION_BY_ATTRIBUTION["服务问题"]
    assert decision["can_edit_case"] is False
    assert decision["action"] == "file_defect"


def test_environment_problem_allows_rerun_only():
    from ai.agent.tools.heal import DECISION_BY_ATTRIBUTION

    decision = DECISION_BY_ATTRIBUTION["环境问题"]
    assert decision["can_edit_case"] is False
    assert decision["can_rerun"] is True


def test_script_problem_does_not_allow_assertion_edits():
    """脚本问题（变量取不到 / 结构写错）改断言解决不了。"""
    from ai.agent.tools.heal import DECISION_BY_ATTRIBUTION

    assert DECISION_BY_ATTRIBUTION["脚本问题"]["can_edit_case"] is False


def test_case_data_problem_is_the_only_healable_attribution():
    from ai.agent.tools.heal import DECISION_BY_ATTRIBUTION

    healable = {k for k, v in DECISION_BY_ATTRIBUTION.items() if v["can_edit_case"]}
    assert healable == {"用例数据问题"}


# --------------------------------------------------------------------------
# plan_heal
# --------------------------------------------------------------------------


def _stub_diagnosis(monkeypatch, attribution: str, category: str = "x") -> None:
    """把「查上一轮归因」换成一个固定结论（不依赖 reports/ 的真实内容）。"""
    from ai.agent.tools import heal

    monkeypatch.setattr(
        heal,
        "_latest_diagnosis",
        lambda case_no: {
            "case_id": case_no,
            "attribution": attribution,
            "category": category,
            "attribution_source": "框架归因",
            "message": "",
        },
    )


def test_plan_heal_blocks_service_problems(monkeypatch):
    from ai.agent.tools import heal

    _stub_diagnosis(monkeypatch, "服务问题", "服务端内部异常（A00005）")
    result = heal.plan_heal("API-168")

    assert result["ok"] is True
    assert result["can_edit_case"] is False
    assert result["action"] == "file_defect"
    assert "提缺陷" in result["next_step"]


def test_plan_heal_routes_environment_to_rerun(monkeypatch):
    from ai.agent.tools import heal

    _stub_diagnosis(monkeypatch, "环境问题")
    result = heal.plan_heal("API-168")
    assert result["can_edit_case"] is False
    assert "run_cases" in result["next_step"]


def test_plan_heal_allows_case_data_healing(monkeypatch):
    from ai.agent.tools import heal

    _stub_diagnosis(monkeypatch, "用例数据问题")
    result = heal.plan_heal("API-168")
    assert result["can_edit_case"] is True
    assert result["action"] == "heal"


def test_plan_heal_is_conservative_when_nothing_failed(monkeypatch):
    """没有失败记录时不能默认"可以改" —— 那是把所有用例都当成待修。"""
    from ai.agent.tools import heal

    monkeypatch.setattr(heal, "_latest_diagnosis", lambda case_no: {})
    result = heal.plan_heal("API-168")

    assert result["found_failure"] is False
    assert result["can_edit_case"] is False
    assert result["action"] == "manual_review"


def test_plan_heal_rejects_unknown_case():
    from ai.agent.tools.registry import ToolError
    from ai.agent.tools import heal

    with pytest.raises(ToolError) as exc:
        heal.plan_heal("API-99999")
    assert "API-99999" in str(exc.value)


def test_plan_heal_stops_after_two_failed_rounds(monkeypatch):
    """改了也不通过两轮之后，连"可以改"的归因也不再放行。"""
    from ai.agent.tools import heal, heal_state

    _stub_diagnosis(monkeypatch, "用例数据问题")
    heal_state.record_failed_round("API-168", "还是失败")
    heal_state.record_failed_round("API-168", "还是失败")

    result = heal.plan_heal("API-168")
    assert result["rounds_exhausted"] is True
    assert result["can_edit_case"] is False
    assert result["action"] == "stop"


# --------------------------------------------------------------------------
# 服务问题的用例：写入入口必须拒绝
# --------------------------------------------------------------------------


def test_apply_refuses_when_attribution_is_service_problem():
    from ai.agent.tools import case_io
    from ai.agent.tools.registry import ToolError

    _record_failure("API-168", "服务问题")

    with pytest.raises(ToolError) as exc:
        case_io.apply_assertion_change(
            "API-168",
            [{"type": "status_code", "expected": 200}],
        )
    assert "服务问题" in str(exc.value)
    assert "提缺陷" in exc.value.hint


def test_apply_allows_case_data_problem(monkeypatch):
    """归因是用例数据问题时不能被这道闸误伤 —— 那正是该改的情况。"""
    from ai.agent.tools import case_io
    from ai.agent.tools.registry import ToolError

    _record_failure("API-168", "用例数据问题")

    # 走不到真正落盘（用例不存在），但**不能**是因为"服务问题"被拒的
    with pytest.raises(ToolError) as exc:
        case_io.apply_assertion_change(
            "API-168", [{"type": "status_code", "expected": 200}]
        )
    assert "服务问题" not in str(exc.value)


def test_attribution_for_ignores_other_cases():
    from utils.infra.failure_classifier import attribution_for

    _record_failure("API-168", "服务问题")
    assert attribution_for("API-168") is not None
    assert attribution_for("API-170") is None
    assert attribution_for("") is None


def test_attribution_for_ignores_non_service_attribution():
    from utils.infra.failure_classifier import attribution_for

    _record_failure("API-168", "用例数据问题")
    assert attribution_for("API-168") is None


# --------------------------------------------------------------------------
# 轮次计数
# --------------------------------------------------------------------------


def test_heal_rounds_accumulate_and_reset():
    from ai.agent.tools import heal_state

    assert heal_state.failed_rounds("API-168") == 0
    assert heal_state.record_failed_round("API-168", "x") == 1
    assert heal_state.record_failed_round("API-168", "y") == 2
    assert heal_state.failed_rounds("API-168") == 2

    heal_state.reset("API-168")
    assert heal_state.failed_rounds("API-168") == 0


def test_heal_rounds_are_per_case():
    from ai.agent.tools import heal_state

    heal_state.record_failed_round("API-168")
    assert heal_state.failed_rounds("API-169") == 0


def test_apply_refuses_after_rounds_exhausted():
    from ai.agent.tools import case_io, heal_state
    from ai.agent.tools.registry import ToolError

    heal_state.record_failed_round("API-168", "1")
    heal_state.record_failed_round("API-168", "2")

    with pytest.raises(ToolError) as exc:
        case_io.apply_assertion_change("API-168", [{"type": "status_code", "expected": 200}])
    assert "轮" in str(exc.value)


def test_broken_state_file_does_not_break_counting(isolated_cache):
    from ai.agent.tools import heal_state

    (isolated_cache / "heal_state.json").write_text("{不是 json", encoding="utf-8")
    assert heal_state.failed_rounds("API-168") == 0
    assert heal_state.record_failed_round("API-168") == 1


# --------------------------------------------------------------------------
# 回滚
# --------------------------------------------------------------------------


def _make_backup(tmp_path: Path, content: str = "原始内容") -> tuple[Path, Path]:
    """造一个和 ``_backup`` 结构一致的备份目录。"""
    target = tmp_path / "case.xlsx"
    target.write_text(content, encoding="utf-8")

    backup_dir = tmp_path / "backup" / "agent_20260101_000000"
    backup_dir.mkdir(parents=True)
    (backup_dir / "case.xlsx").write_text("备份内容", encoding="utf-8")
    (backup_dir / "manifest.json").write_text(
        json.dumps({"tool": "apply_assertion_change", "file": str(target)}, ensure_ascii=False),
        encoding="utf-8",
    )
    return backup_dir, target


def test_restore_from_backup_puts_the_original_back(tmp_path):
    from ai.agent.tools.case_io import restore_from_backup

    backup_dir, target = _make_backup(tmp_path)
    target.write_text("改坏了的内容", encoding="utf-8")

    restore_from_backup(backup_dir)
    assert target.read_text(encoding="utf-8") == "备份内容"


def test_restore_from_backup_rejects_missing_dir(tmp_path):
    from ai.agent.tools.case_io import restore_from_backup
    from ai.agent.tools.registry import ToolError

    with pytest.raises(ToolError):
        restore_from_backup(tmp_path / "nope")


def test_restore_from_backup_refuses_ambiguous_dir(tmp_path):
    """一个备份目录只该有一个用例文件 —— 多于一个说明不是我们生成的。"""
    from ai.agent.tools.case_io import restore_from_backup
    from ai.agent.tools.registry import ToolError

    directory = tmp_path / "agent_x"
    directory.mkdir()
    (directory / "a.xlsx").write_text("a", encoding="utf-8")
    (directory / "b.xlsx").write_text("b", encoding="utf-8")
    (directory / "manifest.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ToolError) as exc:
        restore_from_backup(directory)
    assert "不知道该恢复哪个" in str(exc.value)


# --------------------------------------------------------------------------
# 归因收集：附件在两层，别只看 steps
# --------------------------------------------------------------------------


def test_case_id_is_parsed_from_allure_parameter():
    from ai.capabilities.diagnosis import _case_id_of

    assert (
        _case_id_of({"parameters": [{"name": "yaml_case", "value": "分页查询操作日志#API-183"}]})
        == "API-183"
    )
    assert _case_id_of({"parameters": [{"name": "yaml_case", "value": "没有编号的用例"}]}) == ""
    assert _case_id_of({}) == ""


def test_collect_failures_reads_top_level_attachments(tmp_path):
    """「请求响应记录」「失败归因」挂在用例上，不在 step 里。

    只遍历 steps 会漏掉它们 —— 那样归因就退化成"看异常文本猜"，
    服务端返回 A00005 的失败会被猜成"断言不匹配"。
    """
    from ai.capabilities.diagnosis import collect_failures

    (tmp_path / "req.txt").write_text("GET /admin/x 200", encoding="utf-8")
    (tmp_path / "attr.json").write_text(
        json.dumps({"归因": "服务问题", "具体原因": "服务端内部异常（A00005）"}, ensure_ascii=False),
        encoding="utf-8",
    )

    result = {
        "name": "查数据",
        "status": "failed",
        "parameters": [{"name": "yaml_case", "value": "查数据#API-168"}],
        "attachments": [
            {"name": "请求响应记录", "source": "req.txt", "type": "text/plain"},
            {"name": "失败归因", "source": "attr.json", "type": "application/json"},
        ],
        "steps": [],
        "statusDetails": {"message": "AssertionError: [success] 期望等于 True，实际 False"},
    }
    (tmp_path / "x-result.json").write_text(
        json.dumps(result, ensure_ascii=False), encoding="utf-8"
    )

    failures = collect_failures(allure_results=tmp_path)
    assert len(failures) == 1
    failure = failures[0]
    assert failure.case_id == "API-168"
    assert failure.attribution == "服务问题"
    assert failure.attribution_reason == "服务端内部异常（A00005）"
    assert "GET /admin/x" in failure.request_log


def test_framework_attribution_beats_text_rules(tmp_path):
    """框架当场判的归因优先于事后看文本的规则。

    断言信息里只有 ``[success] 期望 True 实际 False``，规则会判"断言不匹配"
    （→ 用例数据问题）；但框架拿着真实响应知道服务端返回了 A00005。
    信规则就会去改断言 —— 那正是要防的。
    """
    from ai.capabilities.diagnosis import FailureInfo, diagnose

    failures = [
        FailureInfo(
            case="查数据",
            case_id="API-168",
            error_type="AssertionError",
            message="[success] 期望等于 True，实际 False",
            attribution="服务问题",
            attribution_reason="服务端内部异常（A00005）",
        )
    ]
    result = diagnose(failures, use_ai=False)
    assert result[0].attribution == "服务问题"
    assert result[0].source == "框架归因"


def test_diagnose_failures_maps_framework_attribution(monkeypatch, tmp_path):
    """``diagnose_failures`` 要把框架归因原样透出去，不再二次猜。"""
    from ai.agent.tools import diagnostics
    from utils import paths

    # 不依赖仓库里真有 reports/ —— 干净检出时也要能跑
    allure_dir = tmp_path / "allure"
    allure_dir.mkdir()
    result_file = tmp_path / "result.json"
    result_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(paths, "ALLURE_RESULTS", allure_dir)
    monkeypatch.setattr(paths, "REPORTS_ROOT", tmp_path)

    class _Diag:
        case = "查数据"
        case_id = "API-168"
        error_type = "AssertionError"
        message = "x"
        category = "服务端内部异常"
        reason = "x"
        suggestion = "x"
        is_environment = False
        source = "框架归因"
        attribution = "服务问题"
        ai_analysis = ""

        def to_dict(self):
            return {
                "case": self.case,
                "case_id": self.case_id,
                "error_type": self.error_type,
                "message": self.message,
                "category": self.category,
                "reason": self.reason,
                "suggestion": self.suggestion,
                "is_environment": self.is_environment,
                "source": self.source,
                "attribution": self.attribution,
                "ai_analysis": self.ai_analysis,
            }

    monkeypatch.setattr(diagnostics, "_fresh_cases", lambda *a, **k: None)
    monkeypatch.setattr(
        "ai.capabilities.diagnosis.collect_failures",
        lambda **kwargs: [object()],
    )
    monkeypatch.setattr("ai.capabilities.diagnosis.diagnose", lambda failures, use_ai: [_Diag()])

    result = diagnostics.diagnose_failures()
    assert result["ok"] is True
    assert result["by_attribution"] == {"服务问题": 1}
    assert result["diagnoses"][0]["attribution"] == "服务问题"
    assert result["diagnoses"][0]["attribution_source"] == "框架归因"
