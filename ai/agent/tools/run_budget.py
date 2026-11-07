"""一次会话里跑用例的累计预算。

**为什么单次上限不够**

``run_cases`` 里已经有单次条数闸（``MAX_CASES_PER_RUN``），拦的是
"一次跑太多"。但评测里模型被拦之后立刻换了个姿势：

    run_cases(module="admin_area") → run_cases(module="admin_delivery") → …

每次都在上限之内，合起来把 23 个模块全跑了一遍。总量和跑全量一模一样，
只是拆成了 23 次。**按单次设闸，永远挡不住"化整为零"。**

所以要按**累计**记：这一次会话（或这一次评测任务）已经跑掉多少条，
加上这一次超了就不许再跑。

**为什么落到磁盘而不是内存**

MCP 服务是长驻进程，但 ``run_cases`` 是起子进程跑的 —— 父进程自己
记账就够了，不需要跨进程。落盘的真正理由是**评测**：评测脚本和
被评测的循环在同一个进程，但如果将来改成跑独立进程，内存记账会归零。
一个几百字节的 JSON 换来行为一致，值。
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from utils.logs.log_manager import log

#: 本进程启动时刻（用于区分"同一个 pid 被系统复用"的情况）
_PROCESS_STARTED = f"{time.time():.0f}"

#: 磁盘上最多留几个会话桶（跑完的老桶要清掉，否则文件无限涨）
MAX_BUCKETS = 10

#: 一次会话累计最多跑多少条。
#:
#: 定这个数的依据：mall4j 全量 132 条，单模块最多 11 条。
#: 取 50 = 单次上限的同一档 —— 正常迭代"跑一两个模块看结果"绰绰有余
#: （4 个模块），但"逐个模块扫一遍"会在第 5 个模块上被拦下。
#:
#: 定 100 试过：模型能一路跑到 5 个模块还不触发，等到触发时
#: 已经跑掉一半用例了 —— 拦得住但已经太晚。
MAX_CASES_PER_SESSION = 50


def _path():
    from utils import paths

    return paths.CACHE_DIR / "run_budget.json"


def _pid() -> int:
    """本进程 id。

    单独包一层是为了测试能替换 —— 直接 ``monkeypatch.setattr(os, "getpid")``
    改的是 **stdlib 的 os 模块本身**，测试期间所有代码的 ``os.getpid()``
    都会跟着变，副作用大得离谱。
    """
    return os.getpid()


def _key() -> str:
    """预算按什么分桶：有评测任务 id 就按任务分，否则整个进程算一次会话。

    **非评测场景必须带上进程标识**

    落盘的预算如果只按 ``"session"`` 记，MCP 服务重启后读到的还是上一轮
    的数字 —— 那就不叫「会话预算」了，是**永久预算**：跑够 50 条之后
    重启服务也解不开，而报错还写着"本次会话"。

    所以普通会话按 ``pid`` + 进程启动时间分桶：进程一换就是新桶，
    磁盘上只留最近几个（见 :func:`_prune`）。
    """
    from ai.agent import audit_log

    run = audit_log.run_id()
    if run:
        return f"run:{run}"
    return f"session:{_pid()}:{_PROCESS_STARTED}"


def _load() -> dict[str, Any]:
    path = _path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("跑用例预算文件读不了，按 0 处理: %s", exc)
        return {}
    return data if isinstance(data, dict) else {}


def spent() -> int:
    """当前桶里已经跑掉多少条。"""
    return int(_load().get(_key(), {}).get("cases") or 0)


def add(cases: int) -> int:
    """记一笔，返回累计值。写失败只告警（预算记不上不该挡住正常执行）。"""
    if cases <= 0:
        return spent()

    data = _load()
    bucket = data.setdefault(_key(), {"cases": 0, "runs": 0})
    bucket["cases"] = int(bucket.get("cases") or 0) + int(cases)
    bucket["runs"] = int(bucket.get("runs") or 0) + 1
    bucket["updated_at"] = round(time.time(), 3)
    _prune(data)

    try:
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        log.warning("跑用例预算写入失败: %s", exc)

    return int(bucket["cases"])


def _prune(data: dict[str, Any]) -> None:
    """只留最近 ``MAX_BUCKETS`` 个桶。调用方原地改 ``data``。"""
    if len(data) <= MAX_BUCKETS:
        return
    ordered = sorted(
        data.items(),
        key=lambda kv: float((kv[1] or {}).get("updated_at") or 0),
        reverse=True,
    )
    keep = {key for key, _ in ordered[:MAX_BUCKETS]}
    for key in [k for k in data if k not in keep]:
        del data[key]


def check(next_cases: int) -> None:
    """跑之前问一句：加上这次会超吗？超了抛 ``ToolError``。"""
    from ai.agent.tools.registry import ToolError

    used = spent()
    total = used + max(0, int(next_cases))
    if total <= MAX_CASES_PER_SESSION:
        return

    raise ToolError(
        f"本次会话已经跑了 {used} 条，再跑 {next_cases} 条会超过累计上限 "
        f"{MAX_CASES_PER_SESSION} 条",
        hint=(
            "逐个模块跑也不允许 —— 累计是算总量的。"
            "要么用 case_id 只跑要验证的那几条，要么让用户用命令行 "
            "`python scripts/run.py ...` 跑全量。"
            "如果确实需要更多，说明前面几轮的范围选错了，先确认再跑。"
        ),
    )


def reset() -> None:
    """清空**当前桶**（测试用 / 新一轮评测）。

    只删自己那个桶，不删整个文件 —— 评测和 MCP 服务可能同时开着，
    删整个文件会把服务端正在用的预算也清掉（等于给服务端开了后门）。
    """
    try:
        path = _path()
        if not path.exists():
            return
        data = _load()
        if data.pop(_key(), None) is None:
            return
        if data:
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        else:
            path.unlink(missing_ok=True)
    except OSError:
        pass
