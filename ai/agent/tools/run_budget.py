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
import threading
import time
from typing import Any

from utils.infra.json_store import atomic_write_json
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


# 读-改-写要串行：同一进程里并发调 run_cases 时，两个线程可能读到
# 同一份旧值，各加各的，最后只留下一次累加。
_LOCK = threading.Lock()

# 磁盘写失败时的内存兜底：至少本进程内还拦得住，不会因为
# ".cache 目录不可写"直接失去预算限制。
_MEMORY: dict[str, dict[str, Any]] = {}


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


def _bucket_of(container: dict[str, Any], key: str) -> dict[str, Any]:
    """取一个会话桶，**保证是 dict**。

    ``run_budget.json`` 是运行态文件：手改坏、或旧版本写成
    ``"session:1": 5`` 都不稀奇。直接 ``.get`` 会抛 AttributeError，
    而这几处调用都在 try 之外 —— ``run_cases`` 会整个挂掉，
    预算闸从"限制用量"变成了"工具不可用"。
    """
    if not isinstance(container, dict):
        return {}
    bucket = container.get(key)
    return bucket if isinstance(bucket, dict) else {}


def _safe_int(value: Any) -> int:
    """``cases`` / ``runs`` 被写成 ``"abc"`` 时按 0 处理。"""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _safe_float(value: Any) -> float:
    """``updated_at`` 被写成非数字时按 0 处理（只影响清理顺序）。"""
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def spent() -> int:
    """当前桶里已经跑掉多少条。

    磁盘值和内存兜底取较大者 —— 写盘失败时不能因为"读回来是 0"
    就把已经跑掉的量忘掉。
    """
    key = _key()
    disk = _safe_int(_bucket_of(_load(), key).get("cases"))
    memory = _safe_int(_bucket_of(_MEMORY, key).get("cases"))
    return max(disk, memory)


def add(cases: int) -> int:
    """记一笔，返回累计值。

    写盘失败**不挡本次执行**，但会记到内存里继续生效 ——
    以前只告警不记账，等于文件一坏预算就归零，闸门形同虚设。
    """
    if cases <= 0:
        return spent()

    key = _key()
    with _LOCK:
        data = _load()
        # 旧桶被写坏（不是 dict）时直接换掉：它的计数已经取不回来，
        # 以内存兜底为准；不能让它把整个 run_cases 带崩。
        bucket = _bucket_of(data, key)
        data[key] = bucket
        # 以磁盘和内存中的较大值为基准累加（两者可能不一致）
        bucket["cases"] = max(
            _safe_int(bucket.get("cases")),
            _safe_int(_bucket_of(_MEMORY, key).get("cases")),
        ) + int(cases)
        bucket["runs"] = _safe_int(bucket.get("runs")) + 1
        bucket["updated_at"] = round(time.time(), 3)
        _prune(data)

        try:
            atomic_write_json(_path(), data)
            _MEMORY.pop(key, None)
        except OSError as exc:
            log.error(
                "跑用例预算写入失败（已在内存中继续记账，进程重启后会丢）: %s",
                exc,
            )
            _MEMORY[key] = dict(bucket)

        return int(bucket["cases"])


def _prune(data: dict[str, Any]) -> None:
    """只留最近 ``MAX_BUCKETS`` 个桶。调用方原地改 ``data``。

    桶不是 dict、或 ``updated_at`` 不是数字时按最旧处理 —— 清理排序
    失败不该让 ``add()`` 抛异常（那会让预算记账整个失效）。
    """
    if len(data) <= MAX_BUCKETS:
        return
    ordered = sorted(
        data.items(),
        key=lambda kv: _safe_float(_bucket_of(data, kv[0]).get("updated_at")),
        reverse=True,
    )
    keep = {key for key, _ in ordered[:MAX_BUCKETS]}
    for key in [k for k in data if k not in keep]:
        del data[key]


def check(next_cases: int) -> None:
    """跑之前问一句：加上这次会超吗？超了抛 ``ToolError``。"""
    from ai.agent.tools.registry import ToolError

    used = spent()
    total = used + max(0, _safe_int(next_cases))
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
    key = _key()
    with _LOCK:
        _MEMORY.pop(key, None)
        try:
            path = _path()
            if not path.exists():
                return
            data = _load()
            if data.pop(key, None) is None:
                return
            if data:
                atomic_write_json(path, data)
            else:
                path.unlink(missing_ok=True)
        except OSError:
            pass
