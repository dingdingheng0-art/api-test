"""自愈闭环：失败 → 归因 → 分派 → 验证。

**为什么要有这个工具**

前面几个工具已经能「诊断」「预览改动」「落盘」「跑一遍」，
但**分派规则只写在 skill 文档里**。模型每一步都要自己读一遍文档、
自己判断"这条该不该改"，判断错了没人拦 —— 而判断错一次就可能
把真实缺陷改绿。

所以把分派做成确定性代码：给一个用例编号，工具自己查出归因，
自己决定允许哪些动作，并返回**带上下文的下一步**。

==================  ================================================
归因                工具允许的动作
==================  ================================================
``环境问题``        只能重跑（``run_cases``）。**不许改用例**
``服务问题``        什么都不许。出证据、提缺陷
``脚本问题``        只允许改用例文件的结构性内容（人审后手工改）
``用例数据问题``    允许走自愈：预览 → 落盘 → 重跑，全程人审
==================  ================================================

**三道闸都是代码拦的，不是提示词**

1. ``apply_case_change`` 拒绝服务问题的用例（``case_io`` 里）
2. ``apply_case_change`` 拒绝削弱断言（``find_weakenings``）
3. 同一用例改两轮还没跑通 → 停止（``heal_state``）

这个工具是入口，负责把这三条串起来：``plan_heal`` 给出决策，
``verify_heal`` 负责「改完跑一遍」，跑不通自动回滚到改之前。
"""

from __future__ import annotations

from typing import Any

from ai.agent.tools.registry import ToolError, ToolSpec, ok

#: 归因 → 允许的动作。**这是唯一的口径**，skill / 提示词都引用它
DECISION_BY_ATTRIBUTION: dict[str, dict[str, Any]] = {
    "环境问题": {
        "can_edit_case": False,
        "can_rerun": True,
        "action": "rerun",
        "why": "没拿到响应或服务暂时不可用，和用例内容无关。重跑确认即可。",
    },
    "服务问题": {
        "can_edit_case": False,
        "can_rerun": False,
        "action": "file_defect",
        "why": "服务端行为异常。改用例断言会让真实缺陷蒙混过关，必须提缺陷给后端。",
    },
    "脚本问题": {
        "can_edit_case": False,
        "can_rerun": True,
        "action": "manual_fix",
        "why": (
            "框架或用例文件本身有问题（变量取不到、结构写错、路径不对）。"
            "这不是断言预期的问题，改断言解决不了；看报错定位到具体文件后人工修。"
        ),
    },
    "用例数据问题": {
        "can_edit_case": True,
        "can_rerun": True,
        "action": "heal",
        "why": "服务端行为和用例预期对不上，可以用例走自愈流程（人审 diff 后落盘）。",
    },
}

#: 归因没归出来时按最保守的处理 —— 什么都不允许，先让人看
UNKNOWN_DECISION = {
    "can_edit_case": False,
    "can_rerun": True,
    "action": "manual_review",
    "why": "没能归因。证据不足时不要猜，更不要改用例。",
}


def plan_heal(case_id: str, project: str = "") -> dict[str, Any]:
    """查这条用例上一轮为什么失败，返回允许的动作和下一步。

    纯只读：不跑用例、不改文件。决定要不要动手之前先调它。
    """
    case_no = str(case_id or "").strip()
    if not case_no:
        raise ToolError("必须给用例编号", hint="形如 API-168；用 list_cases 可以查到")

    from ai.agent.tools import heal_state
    from ai.agent.tools._common import find_case_by_number

    # 用例得真的存在 —— 编号写错时在这里就拦住，
    # 否则后面每一步都会拿一个不存在的编号去查，报的错越来越偏
    if find_case_by_number(case_no) is None:
        raise ToolError(
            f"找不到用例 {case_no}",
            hint="用 list_cases 确认编号（Excel 用例都有 API-xxx 编号）",
        )

    diagnosis = _latest_diagnosis(case_no)
    attribution = str(diagnosis.get("attribution") or "")
    decision = dict(DECISION_BY_ATTRIBUTION.get(attribution, UNKNOWN_DECISION))

    rounds = heal_state.failed_rounds(case_no)
    exhausted = rounds >= heal_state.MAX_FAILED_ROUNDS
    if exhausted:
        # 轮次用尽时，连"可以改"的归因也一并停掉 —— 这就是那条规则
        decision["can_edit_case"] = False
        decision["action"] = "stop"

    return ok(
        case_id=case_no,
        found_failure=bool(diagnosis),
        attribution=attribution or "未归因",
        category=str(diagnosis.get("category") or ""),
        evidence=str(diagnosis.get("message") or ""),
        attribution_source=str(diagnosis.get("attribution_source") or ""),
        **decision,
        failed_rounds=rounds,
        max_failed_rounds=heal_state.MAX_FAILED_ROUNDS,
        rounds_exhausted=exhausted,
        next_step=_next_step(decision, case_no, exhausted, rounds),
        hint=(
            "先用 get_case / read_case_assertions 看清现状。"
            "can_edit_case=false 时不要调 apply_case_change —— 工具会拒绝，"
            "而且拒绝本身就是结论：这条不该靠改用例来解决。"
        ),
    )


def _latest_diagnosis(case_no: str) -> dict[str, Any]:
    """上一次运行里这条用例的归因（没有就返回空 dict）。"""
    from ai.agent.tools.diagnostics import diagnose_failures

    try:
        result = diagnose_failures(use_ai=False, limit=100)
    except ToolError:
        return {}
    if not result.get("ok"):
        return {}

    for item in result.get("diagnoses") or []:
        if str(item.get("case_id") or "").strip() == case_no:
            return item
    return {}


def _next_step(
    decision: dict[str, Any], case_no: str, exhausted: bool, rounds: int
) -> str:
    action = decision.get("action")

    if exhausted:
        return (
            f"已经改了 {rounds} 轮还没跑通。停下来，把每轮的改动和结果整理成结论"
            "交给用户，说明卡在哪 —— 不要继续改。"
        )
    if action == "rerun":
        return f"run_cases(case_id='{case_no}') 重跑确认。不要动用例文件。"
    if action == "file_defect":
        return (
            "整理请求/响应证据提缺陷给后端。确认是已知缺陷且短期修不了，"
            f"用 set_case_status(case_id='{case_no}', status='broken', note='...') "
            "标成已知问题 —— 用例内容保持原样。"
        )
    if action == "manual_fix":
        return (
            "按报错定位到具体文件（用例结构 / 变量引用 / 路径），人工修。"
            "改断言解决不了这类问题。"
        )
    if action == "heal":
        return (
            f"取证（get_endpoint / find_field_mapping / query_db）→ "
            f"read_case_assertions('{case_no}') 看现状 → "
            "propose_case_change 预览 diff → 人审 → apply_case_change 落盘 → "
            f"verify_heal('{case_no}') 重跑验证"
        )
    return "证据不足，需要人工看。不要猜，也不要改用例。"


def verify_heal(case_id: str, backup_dir: str = "", timeout: int = 600) -> dict[str, Any]:
    """改完跑一遍；还不过就按备份回滚。

    **为什么回滚是自动的**

    "改了但没修好"是最常见的中间状态。留着这个改动，下一轮会基于一个
    已经偏掉的版本继续改 —— 两三轮下来没人说得清原始断言长什么样。
    回滚到改之前，至少每一轮都是从一个已知正确的基线出发。

    Args:
        backup_dir: ``apply_case_change`` 返回的 ``backup_dir``。给了才可能回滚。
    """
    from ai.agent.tools import heal_state
    from ai.agent.tools.execution import run_cases

    case_no = str(case_id or "").strip()
    if not case_no:
        raise ToolError("必须给用例编号", hint="形如 API-168")

    run = run_cases(case_id=case_no, timeout=timeout)
    failed = [
        f for f in (run.get("failures") or []) if str(f.get("case") or "").strip()
    ]
    passed = int((run.get("result") or {}).get("passed") or 0)
    exit_code = int(run.get("exit_code") or 0)
    success = exit_code == 0 and not failed

    if success:
        heal_state.reset(case_no)
        return ok(
            case_id=case_no,
            verified=True,
            failed_rounds=0,
            run=run,
            next_step=(
                "跑通了。用例本来就是 draft 的话，可以 set_case_status 改成 debugged。"
                "别的状态不用动。"
            ),
        )

    rounds = heal_state.record_failed_round(
        case_no, detail=_first_error(failed) or f"exit_code={exit_code}"
    )
    rolled_back = False
    rollback_error = ""

    if backup_dir:
        try:
            from ai.agent.tools.case_io import restore_from_backup

            restore_from_backup(backup_dir)
            rolled_back = True
        except ToolError as exc:
            rollback_error = str(exc)

    exhausted = rounds >= heal_state.MAX_FAILED_ROUNDS
    return ok(
        case_id=case_no,
        verified=False,
        passed=passed,
        failed_rounds=rounds,
        max_failed_rounds=heal_state.MAX_FAILED_ROUNDS,
        rounds_exhausted=exhausted,
        rolled_back=rolled_back,
        rollback_error=rollback_error,
        failures=failed,
        run=run,
        next_step=(
            "改完还是没通过，已回滚到改动前的版本。"
            + (
                f"这是第 {rounds} 轮失败 —— 停下来把结论交给用户，别再改。"
                if exhausted
                else "重新取证：上一轮的判断大概率错了，别在同一个方向上继续调。"
            )
            if rolled_back
            else "改完还是没通过。没给 backup_dir，所以没法自动回滚 —— "
            "确认当前文件内容要不要手工恢复。"
        ),
        hint="归因变了再改。连续失败说明前面的归因就是错的。",
    )


def _first_error(failures: list[dict[str, Any]]) -> str:
    if not failures:
        return ""
    item = failures[0]
    return f"{item.get('error_type') or ''} {item.get('message') or ''}".strip()


TOOLS = [
    ToolSpec(
        name="plan_heal",
        description=(
            "自愈第一步：查这条用例上一轮为什么失败，返回归因和**允许的动作**。"
            "环境问题→只能重跑；服务问题→只能提缺陷（工具会拒绝改用例）；"
            "用例数据问题→可以走改断言流程。纯只读，决定动手前先调它。"
        ),
        params_schema={
            "type": "object",
            "properties": {
                "case_id": {"type": "string", "description": "用例编号，如 API-168"},
                "project": {"type": "string", "description": "项目名，可省略", "default": ""},
            },
            "required": ["case_id"],
            "additionalProperties": False,
        },
        handler=plan_heal,
        side_effect="read",
        tags=("heal", "diagnosis"),
    ),
    ToolSpec(
        name="verify_heal",
        description=(
            "自愈最后一步：只重跑这一条用例验证改动。**没通过会自动回滚**到改之前"
            "（需要传 apply_case_change 返回的 backup_dir）。"
            "同一条用例改两轮还没跑通，apply_case_change 会拒绝继续改。"
        ),
        params_schema={
            "type": "object",
            "properties": {
                "case_id": {"type": "string", "description": "用例编号，如 API-168"},
                "backup_dir": {
                    "type": "string",
                    "description": "apply_case_change 返回的 backup_dir；给了才能在失败时回滚",
                    "default": "",
                },
                "timeout": {
                    "type": "integer",
                    "description": "超时秒数（默认 600，最小 30）",
                    "default": 600,
                },
            },
            "required": ["case_id"],
            "additionalProperties": False,
        },
        handler=verify_heal,
        side_effect="execute",
        safety="发真实请求；失败时会把用例文件回滚到 backup_dir 里的版本",
        cost="network",
        tags=("heal", "execution"),
    ),
]
