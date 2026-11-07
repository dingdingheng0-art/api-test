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
import time
from typing import Any

from utils.logs.log_manager import log

#: 改几轮还没跑通就停下。2 轮已经够证明"这不是一眼能看出的问题"
MAX_FAILED_ROUNDS = 2


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


def _save(data: dict[str, Any]) -> None:
    path = _path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:  # pragma: no cover - 磁盘满之类
        log.warning("自愈状态写不进去: %s", exc)


def failed_rounds(case_id: str) -> int:
    """这条用例已经「改完还是没跑通」几轮了。"""
    record = _load().get(str(case_id or "").strip()) or {}
    try:
        return int(record.get("failed_rounds") or 0)
    except (TypeError, ValueError):
        return 0


def record_failed_round(case_id: str, detail: str = "") -> int:
    """记一轮失败，返回累计轮数。"""
    key = str(case_id or "").strip()
    if not key:
        return 0

    data = _load()
    record = data.setdefault(key, {})
    record["failed_rounds"] = failed_rounds(key) + 1
    record["last_failed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    if detail:
        record["last_detail"] = str(detail)[:500]
    _save(data)
    return record["failed_rounds"]


def reset(case_id: str) -> None:
    """清零（验证通过 / 人工确认已解决）。"""
    key = str(case_id or "").strip()
    data = _load()
    if key in data:
        data.pop(key)
        _save(data)


def snapshot() -> dict[str, Any]:
    """当前所有计数（排查"为什么它不让我改了"时用）。"""
    return _load()


__all__ = ["MAX_FAILED_ROUNDS", "failed_rounds", "record_failed_round", "reset", "snapshot"]
