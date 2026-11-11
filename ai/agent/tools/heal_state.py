"""自愈轮次计数 —— 「同一条用例改两轮还没跑通就停下」由代码管。

**为什么要落盘计数**

这条规则原本只写在 skill 里。但模型没有「第几轮」的概念：一次对话里
它可以改一遍、跑一遍、再改一遍、再跑一遍，来回十次也不会觉得自己越界 ——
每次都以为"这次肯定对了"。

而自愈最危险的不是修不好，是**反复改**：改到某一轮，断言已经被削得
面目全非，用例却绿了。所以轮次必须是外部状态，模型改不动它。

**只统计"验证没通过"的轮次**

``apply`` 本身不计数 —— 人工正常改用例也可能连着改两遍。
只有走完 ``verify_heal`` 且**仍然失败**才 +1，验证通过就清零。

存储位置 ``.cache/heal_state.json``（和备份目录一样，属于运行时产物）。
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

from utils.infra.json_store import atomic_write_json
from utils.logs.log_manager import log

#: 改几轮还没跑通就停下。2 轮已经够证明"这不是一眼能看出的问题"
MAX_FAILED_ROUNDS = 2

# 读-改-写串行 + 写盘失败的内存兜底。
#
# 轮次计数是"防反复改断言"的最后一道闸：写盘失败却悄悄清零，
# 智能体就能一直改下去 —— 所以必须至少在进程内继续生效。
_LOCK = threading.Lock()
_MEMORY: dict[str, dict[str, Any]] = {}

#: 写盘失败时，"已清零"的标记写进 ``_MEMORY`` 的这一项。
#:
#: 光把内存里的计数改成 0 不够：``failed_rounds`` 取 ``max(磁盘, 内存)``，
#: 磁盘上的旧计数会把刚验证通过的用例顶回「两轮已用尽」，智能体从此被
#: 永久卡住 —— 明明跑通了，工具还说"别再改了"。标记放在 ``_MEMORY`` 里
#: （而不是另一个全局）是为了让单测替换 ``_MEMORY`` 时能一起隔离掉。
_RESET_FLAG = "_reset"


def _path():
    from utils import paths

    return paths.CACHE_DIR / "heal_state.json"


def _load() -> dict[str, Any]:
    path = _path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        # 文件坏了就当成空的 —— 计数丢了只是少拦一次，不该让工具整个挂掉
        log.warning("自愈状态文件读不了，按空的处理: %s", exc)
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict[str, Any], key: str = "") -> bool:
    """写盘；成功返回 True。失败时把该条记录留在内存兜底里。"""
    try:
        atomic_write_json(_path(), data, indent=2)
        if key:
            _MEMORY.pop(key, None)
        return True
    except OSError as exc:  # pragma: no cover - 磁盘满之类
        log.error("自愈状态写不进去（已在内存中继续计数，进程重启后会丢）: %s", exc)
        if key:
            record = _record_of(data, key)
            if record:
                _MEMORY[key] = dict(record)
            else:
                # 记录已经被删除（reset 走到这里）：内存里也别留空的占位
                _MEMORY.pop(key, None)
        return False


def _record_of(container: dict[str, Any], key: str) -> dict[str, Any]:
    """取一条记录，**保证是 dict**。

    文件是给人留的运行时产物：手改坏、或者被别的版本写成
    ``"API-1": "garbage"`` 都不稀奇。这里不把类型收住的话，
    下一次 ``record.get(...)`` 会抛 ``AttributeError`` ——
    「计数文件坏了」不该让整条自愈链路挂掉。
    """
    if not isinstance(container, dict):
        return {}
    record = container.get(key)
    return record if isinstance(record, dict) else {}


def _rounds_of(record: dict[str, Any]) -> int:
    try:
        return int(record.get("failed_rounds") or 0)
    except (TypeError, ValueError):
        return 0


def failed_rounds(case_id: str) -> int:
    """这条用例已经「改完还是没跑通」几轮了。"""
    key = str(case_id or "").strip()
    memory_record = _record_of(_MEMORY, key)
    if memory_record.get(_RESET_FLAG):
        # 清零没写进磁盘：以内存里的"已清零"为准
        return 0
    disk = _rounds_of(_record_of(_load(), key))
    return max(disk, _rounds_of(memory_record))


def record_failed_round(case_id: str, detail: str = "") -> int:
    """记一轮失败，返回累计轮数。"""
    key = str(case_id or "").strip()
    if not key:
        return 0

    with _LOCK:
        # 上一轮已经验证通过（只是没清进磁盘）—— 磁盘上的旧计数作废，
        # 这一次失败算第 1 轮。
        memory_record = _record_of(_MEMORY, key)
        stale_reset = bool(memory_record.get(_RESET_FLAG))
        _MEMORY.pop(key, None)
        data = _load()
        record = _record_of(data, key)
        data[key] = record
        base = (
            0
            if stale_reset
            else max(_rounds_of(record), _rounds_of(memory_record))
        )
        record["failed_rounds"] = base + 1
        record["last_failed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        if detail:
            record["last_detail"] = str(detail)[:500]
        _save(data, key)
        return int(record["failed_rounds"])


def reset(case_id: str) -> None:
    """清零（验证通过 / 人工确认已解决）。"""
    key = str(case_id or "").strip()
    with _LOCK:
        _MEMORY.pop(key, None)
        data = _load()
        if key in data:
            data.pop(key)
            if not _save(data, key):
                # 磁盘上还留着旧计数 —— 本进程内必须让它失效
                _MEMORY[key] = {"failed_rounds": 0, _RESET_FLAG: True}


def snapshot() -> dict[str, Any]:
    """当前所有计数（排查"为什么它不让我改了"时用）。"""
    data = _load()
    # 内存里的是"写盘失败但仍在生效"的记录，优先级高于磁盘
    for key in list(_MEMORY):
        record = _record_of(_MEMORY, key)
        if record.get(_RESET_FLAG):
            # 已清零（只是没能写进磁盘）：磁盘上的旧计数不该再露出来
            data.pop(key, None)
        else:
            data[key] = record
    return data


__all__ = ["MAX_FAILED_ROUNDS", "failed_rounds", "record_failed_round", "reset", "snapshot"]
