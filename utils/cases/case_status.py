"""用例状态：区分「调试态」和「执行态」。

**为什么需要这个**

以前用例只有"有"和"没有"两种状态 —— 写好就进 CI 跑。
问题很实际：

1. 新写的用例还没调通，第一次跑就挂，主干 CI 红了，阻塞别人的提交
2. 某个用例暂时挂掉（比如下游服务在维护），要么阻塞整条流水线，
   要么手动改文件跳过 —— 改文件会留下提交记录，改起来麻烦
3. 用例越攒越多，没人说得清"哪些是可信的"

现在引入四个状态（和平台数据库里的设计一致）：

===============  ==========  ==========  ==============================
状态              含义        CI 是否跑    典型场景
===============  ==========  ==========  ==============================
``draft``        未调试       不跑         刚写好，还没调通
``debugged``     调试通过     跑          可信用例
``disabled``     调试通过     不跑         临时下线（比如在重构）
``broken``       调试不通过   不跑         已知有问题，标记出来
===============  ==========  ==========  ==============================

**怎么用**

YAML 里写 ``status:``：

.. code-block:: yaml

    testcase:
      name: 查询订单
      status: debugged    # 调试通过了，CI 会跑
      steps: ...

**没写 status 算什么**

默认算 ``draft``（不跑）。这是**刻意保守**的选择：

- 老用例没写 status，突然全部不跑了 —— 会造成困惑，所以框架会给一个
  明确的提示（见 ``resolve_status``）
- 但如果默认是"跑"，新写的用例就会直接进 CI，笔误照样能挂主干

折中办法：用 ``--run-draft`` 参数可以临时把 draft 也跑上，
本地调试时用着方便，CI 里不用加这个参数。

**怎么把用例标成"调试通过"**

跑一遍确认没问题后，把 ``status`` 改成 ``debugged`` 提交。
或者用 ``python scripts/run.py --mark-passed <用例名>`` 自动改（见 ``tools/maint``）。
"""
from __future__ import annotations

from typing import Any

from utils.logs.log_manager import log

# ---- 状态定义 ----

# 未调试 —— 默认状态，CI 不跑
DRAFT = "draft"
# 调试通过 —— CI 跑
DEBUGGED = "debugged"
# 调试通过但临时下线 —— CI 不跑
DISABLED = "disabled"
# 调试不通过 —— CI 不跑（已知问题）
BROKEN = "broken"

# 所有合法状态
ALL_STATUSES = (DRAFT, DEBUGGED, DISABLED, BROKEN)

# 中文说明（报错和报告里用）
STATUS_LABELS = {
    DRAFT: "未调试（默认不执行）",
    DEBUGGED: "调试通过（执行）",
    DISABLED: "已禁用（调试通过但暂时不执行）",
    BROKEN: "调试不通过（已知有问题，不执行）",
}

# CI 会执行的状态
RUNNABLE_STATUSES = frozenset({DEBUGGED})

# 兼容旧写法：数字状态（和数据库里的 api_test_case.status 一致）
_NUMERIC_STATUS = {
    0: DRAFT,
    1: DEBUGGED,
    2: DISABLED,
    3: BROKEN,
}


def normalize(status: Any) -> str:
    """把各种写法的状态归一化。

    支持：

    - 字符串 ``debugged``
    - 中文 ``调试通过`` / ``未调试`` / ``已禁用`` / ``调试不通过``
    - 数字 ``0`` / ``1`` / ``2`` / ``3``（和数据库字段一致）
    - 常见别名（``passed`` → ``debugged``、``todo`` → ``draft``）

    认不出来的一律当 ``draft``（保守：不跑总比跑挂了好）。
    """
    if status is None:
        return DRAFT

    if isinstance(status, bool):
        return DEBUGGED if status else DRAFT

    if isinstance(status, int):
        return _NUMERIC_STATUS.get(status, DRAFT)

    text = str(status).strip().lower()
    if not text:
        return DRAFT

    if text in ALL_STATUSES:
        return text

    # 数字字符串
    if text.isdigit():
        return _NUMERIC_STATUS.get(int(text), DRAFT)

    # 中文写法
    chinese = {
        "未调试": DRAFT,
        "待调试": DRAFT,
        "草稿": DRAFT,
        "调试通过": DEBUGGED,
        "已调试": DEBUGGED,
        "通过": DEBUGGED,
        "已禁用": DISABLED,
        "禁用": DISABLED,
        "下线": DISABLED,
        "调试不通过": BROKEN,
        "未通过": BROKEN,
        "有问题": BROKEN,
    }
    if text in chinese:
        return chinese[text]

    # 英文别名
    aliases = {
        "todo": DRAFT,
        "new": DRAFT,
        "wip": DRAFT,
        "passed": DEBUGGED,
        "pass": DEBUGGED,
        "ok": DEBUGGED,
        "active": DEBUGGED,
        "enable": DEBUGGED,
        "enabled": DEBUGGED,
        "off": DISABLED,
        "disable": DISABLED,
        "skip": DISABLED,
        "skipped": DISABLED,
        "fail": BROKEN,
        "failed": BROKEN,
        "known_issue": BROKEN,
    }
    return aliases.get(text, DRAFT)


def resolve_status(case: dict[str, Any]) -> str:
    """取一条用例的状态。

    除了用例自己的 ``status``，还认两个地方：

    - ``allure.metadata.status``（Excel 用例里可能写在这）
    - 标签 ``wip`` —— 打了 ``wip`` 标签的用例当成 ``draft``
    """
    if not isinstance(case, dict):
        return DRAFT

    raw = case.get("status")
    if raw is None:
        meta = case.get("allure") or {}
        if isinstance(meta, dict):
            raw = meta.get("status")

    if raw is not None:
        return normalize(raw)

    # 打了 wip 标签的用例，视为未完成
    marks = case.get("marks") or case.get("mark") or case.get("tags") or []
    if isinstance(marks, str):
        marks = [marks]
    for mark in marks:
        if str(mark).strip().lower() == "wip":
            return DRAFT

    return DRAFT


def should_run(case: dict[str, Any]) -> bool:
    """这条用例现在该不该跑。"""
    return resolve_status(case) in RUNNABLE_STATUSES


def explain(case: dict[str, Any]) -> str:
    """生成一句人话说明，报告和日志里用。"""
    status = resolve_status(case)
    label = STATUS_LABELS.get(status, status)
    name = str(case.get("name") or "未命名用例")
    return f"用例「{name}」当前状态：{label}"


def describe_missing_status(case: dict[str, Any]) -> str | None:
    """没写 ``status`` 时给个提示。

    返回 ``None`` 表示不用提示（比如显式写了 draft）。
    """
    if not isinstance(case, dict):
        return None

    raw = case.get("status")
    if raw is None:
        meta = case.get("allure") or {}
        if isinstance(meta, dict):
            raw = meta.get("status")

    if raw is not None:
        return None

    return (
        "没写 status，默认按「未调试」处理（CI 不会执行）。"
        "确认没问题后改成 status: debugged。"
    )


def status_filter(
    cases: list[tuple[str, dict[str, Any]]],
    include_draft: bool = False,
    only_statuses: set[str] | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    """按状态筛选用例。

    Args:
        cases: ``[(项目名, 用例数据), ...]``
        include_draft: 是否把 ``draft`` 也算上（本地调试用）
        only_statuses: 只保留这些状态（优先于 ``include_draft``）
    """
    if only_statuses:
        return [
            (project, case)
            for project, case in cases
            if resolve_status(case) in only_statuses
        ]

    if include_draft:
        return list(cases)

    return [
        (project, case)
        for project, case in cases
        if resolve_status(case) in RUNNABLE_STATUSES
    ]


def log_status_summary(cases: list[tuple[str, dict[str, Any]]]) -> None:
    """打印一份状态统计（收集阶段用）。"""
    counts: dict[str, int] = {}
    for _project, case in cases:
        status = resolve_status(case)
        counts[status] = counts.get(status, 0) + 1

    if not counts:
        return

    parts = [
        f"{STATUS_LABELS.get(status, status)} {count} 条"
        for status, count in sorted(counts.items())
    ]
    log.info("用例状态分布：%s", "，".join(parts))
