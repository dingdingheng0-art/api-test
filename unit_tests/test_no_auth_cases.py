"""``no_auth`` 用例的口径。

测未登录/越权必须真的**不带 token**。两处都要认这个标签：

- ``create_case`` 写用例时不能补默认鉴权头
- ``CaseRunner`` 执行时不能补默认鉴权头

只改一处的话，用例名叫"未登录"，请求却带着 token —— 测试是假的。
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai.agent.tools.case_io import _has_no_auth_mark  # noqa: E402
from testcases.case_runner import CaseRunner  # noqa: E402


def test_mark_detection_accepts_common_spellings():
    for value in ("no_auth", "C1,no_auth", "noauth", "unauthenticated", ["no_auth"]):
        assert _has_no_auth_mark(value) is True


def test_mark_detection_ignores_normal_marks():
    for value in ("", None, "smoke,p1", "ai_generated", ["C1"]):
        assert _has_no_auth_mark(value) is False


def test_runner_skips_auth_for_no_auth_case():
    assert CaseRunner._skips_auth({"marks": ["no_auth"]}) is True
    assert CaseRunner._skips_auth({"marks": "C1,no_auth"}) is True
    assert CaseRunner._skips_auth({"marks": ["smoke"], "no_auth": True}) is True


def test_runner_keeps_auth_for_normal_case():
    assert CaseRunner._skips_auth({"marks": ["smoke", "p1"]}) is False
    assert CaseRunner._skips_auth({}) is False
    assert CaseRunner._skips_auth(None) is False
