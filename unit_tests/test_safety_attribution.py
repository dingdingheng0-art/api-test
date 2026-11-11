"""失败归因：安全闸拦截不能被说成"登录失败"或"脚本写错了"。

历史问题
--------
``CaseRunner.last_step_info`` 原来只在**拿到响应之后**才写。安全闸
（``safety_guard``）是在发请求途中抛 ``DangerousRequestBlocked`` 的，
这时 ``last_step_info`` 还停在 ``_login_projects`` 里写的
``{"stage": "login"}`` —— 归因于是输出「登录失败：DangerousRequestBlocked」，
排查方向直接被带偏（明明登录早就成功了）。

现在两处都修了：

1. ``_run_steps`` 在发请求**之前**就写入当前步骤上下文（stage=step）；
2. ``classify_exception`` 对 ``DangerousRequestBlocked`` 单独归因。
"""

from __future__ import annotations

from utils.infra.failure_classifier import CASE_DATA, classify_exception
from utils.infra.safety_guard import DangerousRequestBlocked


def test_safety_block_is_case_data_not_script() -> None:
    """危险写请求被拦：归因为用例数据问题，并给出放行办法。"""
    diag = classify_exception(
        DangerousRequestBlocked("DELETE /prod/prod/1 会破坏真实数据"),
        context="步骤 delete_prod",
        stage="step",
    )

    assert diag.attribution == CASE_DATA
    assert "安全闸" in diag.category
    # 提示不能再说"改脚本" —— 这不是脚本 bug
    assert "白名单" in diag.hint or "allow_dangerous" in diag.hint


def test_safety_block_does_not_use_login_wording() -> None:
    """即使 stage 因为某种原因还是 login，也不能说成登录失败。

    真实原因是被安全闸拦下，跟登录没关系 —— 说成登录失败会让人去查
    账号、验证码、token，全是无用功。
    """
    diag = classify_exception(
        DangerousRequestBlocked("blocked"), stage="login"
    )

    assert "登录失败" not in diag.category
    assert "DangerousRequestBlocked" in diag.category or "安全闸" in diag.category
