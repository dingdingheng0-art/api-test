"""智能体调用审计：把每一次工具调用落盘。

**为什么不能只放内存**

``ToolRegistry`` 原来把调用轨迹存在一个最多 200 条的列表里。那个列表
只在 MCP 服务进程活着的时候存在 —— 进程一重启就没了，而"昨天那次
自愈到底调了什么、花了多久、失败了几次"恰恰是最该查的。

落盘之后能回答三类问题：

1. **成本** —— 哪些工具被调得最多、哪些是 ``llm``/``network`` 档
2. **审计** —— 谁在什么时候调了写类工具（``write_case``）
3. **排障** —— 某个工具一直失败，是参数写错还是环境挂了

**为什么是 JSONL 不是 SQLite**

图谱库（``ai/knowledge/store.py``）用 SQLite 是因为要全文检索和关系查询；
这里只追加、只按时间倒序读，JSONL 更简单，出问题能直接打开看，
而且写失败不会损坏整个库。真要做聚合查询，``summarize`` 在内存里算就够了。

**参数里的密钥必须抹掉**

工具参数会带 token / 密码。审计日志是给人看的，落到磁盘上就该按
"永久保留"的敏感级别对待 —— 所以按 key 名做黑名单替换，宁可多抹。

**为什么写失败只告警不抛异常**

审计是旁路。磁盘满、文件被占用都不该让一次正常的工具调用失败 ——
"因为记不了账所以不办事"是本末倒置。但会写 WARNING 日志，不至于静默丢。
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

from utils.logs.log_manager import log

#: 单个文件上限，超过就滚动
MAX_BYTES = 5 * 1024 * 1024
#: 保留几个历史文件（``.1`` / ``.2`` / ``.3``）
KEEP_FILES = 3

#: 单个参数值最多记多少字符
MAX_ARG_CHARS = 200
#: 全部参数加起来最多记多少字符（``create_case`` 的断言列表可能很长）
MAX_ARGS_TOTAL = 1200

#: 参数名里出现这些词就把值抹掉
_SECRET_KEYS = (
    "token",
    "password",
    "passwd",
    "secret",
    "authorization",
    "api_key",
    "apikey",
    "cookie",
    "session",
)

_LOCK = threading.Lock()

#: 模型调用在审计日志里的工具名（不是真的工具，聚合时单独看）
LLM_TOOL = "__llm__"

#: 谁在调 —— ``agent``（MCP 服务）/ ``eval``（评测脚本）/ ``test``
_ORIGIN = "agent"


def set_origin(origin: str) -> str:
    """设置当前进程的调用来源，返回旧值（方便用完还原）。"""
    global _ORIGIN
    previous = _ORIGIN
    _ORIGIN = str(origin or "agent")
    return previous


def get_origin() -> str:
    return _ORIGIN


def run_id() -> str:
    """当前这次「任务」的标识（评测脚本会设）。

    MCP 服务是长驻进程，环境变量改不了，所以它留空；评测脚本
    每跑一个任务前 ``set_run_id`` 一下，就能把调用链切成一段一段的。
    没设时返回空串 —— 那就按时间窗口筛。
    """
    return os.environ.get("APITEST_AUDIT_RUN", "")


def set_run_id(value: str) -> None:
    """设置当前任务标识（空串 = 清除）。"""
    if value:
        os.environ["APITEST_AUDIT_RUN"] = str(value)
    else:
        os.environ.pop("APITEST_AUDIT_RUN", None)


def enabled() -> bool:
    """审计开关。单测里关掉，避免往真实审计日志里塞假记录。"""
    return os.environ.get("APITEST_AUDIT", "1").strip().lower() not in ("0", "false", "off", "no")


def log_path() -> Path:
    """审计文件位置。

    **每次现取** ``paths.CACHE_DIR`` —— 单测会把它指到 tmp_path，
    在模块导入时把路径存成常量的话，测试就写进真实审计日志了。
    """
    from utils import paths

    return paths.CACHE_DIR / "agent_audit.jsonl"


# --------------------------------------------------------------------------
# 写
# --------------------------------------------------------------------------


def record(
    tool: str,
    *,
    ok: bool,
    duration_ms: float,
    side_effect: str = "",
    cost: str = "free",
    arguments: dict[str, Any] | None = None,
    error: str = "",
) -> None:
    """追加一条调用记录。**永不抛异常**（审计是旁路）。"""
    if not enabled():
        return

    entry: dict[str, Any] = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "epoch": round(time.time(), 3),
        "origin": _ORIGIN,
        "run": run_id(),
        "tool": str(tool),
        "side_effect": str(side_effect or ""),
        "cost": str(cost or "free"),
        "ok": bool(ok),
        "duration_ms": round(float(duration_ms or 0), 1),
        "args": _sanitize(arguments or {}),
    }
    if error:
        entry["error"] = str(error)[:300]

    try:
        _append(entry)
    except OSError as exc:
        log.warning("审计日志写入失败（不影响本次调用）: %s", exc)


def record_llm(model: str, prompt_tokens: int, completion_tokens: int) -> None:
    """记一次模型调用的 token 用量。

    **为什么和工具调用记在同一个文件**

    成本要连着看才有意义：「这一轮跑了 3 条用例、调了 5 次工具、
    模型烧了 8k token」。分两个文件就得按时间戳对齐，反而难查。
    用 ``tool="__llm__"`` 标记，聚合时单独拎出来。
    """
    if not enabled():
        return

    entry: dict[str, Any] = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "epoch": round(time.time(), 3),
        "origin": _ORIGIN,
        "run": run_id(),
        "tool": LLM_TOOL,
        "side_effect": "read",
        "cost": "llm",
        "ok": True,
        "duration_ms": 0.0,
        "args": {"model": model},
        "tokens": {
            "prompt": int(prompt_tokens or 0),
            "completion": int(completion_tokens or 0),
            "total": int(prompt_tokens or 0) + int(completion_tokens or 0),
        },
    }
    try:
        _append(entry)
    except OSError as exc:
        log.warning("审计日志写入失败（不影响本次调用）: %s", exc)


def _append(entry: dict[str, Any]) -> None:
    path = log_path()
    line = json.dumps(entry, ensure_ascii=False) + "\n"

    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed(path)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)


def _rotate_if_needed(path: Path) -> None:
    """文件超过上限就滚动。调用方已持锁。"""
    try:
        if path.stat().st_size < MAX_BYTES:
            return
    except OSError:
        return

    # .2 → .3 → 删除；.1 → .2；当前 → .1
    oldest = path.with_name(f"{path.name}.{KEEP_FILES}")
    if oldest.exists():
        oldest.unlink(missing_ok=True)
    for index in range(KEEP_FILES - 1, 0, -1):
        src = path.with_name(f"{path.name}.{index}")
        if src.exists():
            src.replace(path.with_name(f"{path.name}.{index + 1}"))
    path.replace(path.with_name(f"{path.name}.1"))
    log.info("审计日志已滚动: %s", path.name)


def _sanitize(arguments: dict[str, Any]) -> dict[str, Any]:
    """参数脱敏 + 截断。"""
    out: dict[str, Any] = {}
    budget = MAX_ARGS_TOTAL

    for key, value in arguments.items():
        name = str(key)
        if any(secret in name.lower() for secret in _SECRET_KEYS):
            out[name] = "***"
            continue

        text = value if isinstance(value, (int, float, bool)) or value is None else str(value)
        if not isinstance(text, str):
            out[name] = text
            budget -= len(str(text))
            continue

        if len(text) > MAX_ARG_CHARS:
            text = text[:MAX_ARG_CHARS] + "…"
        if len(text) > budget:
            text = text[: max(0, budget)] + "…"
        out[name] = text
        budget -= len(text)
        if budget <= 0:
            out["_truncated"] = True
            break

    return out


# --------------------------------------------------------------------------
# 读
# --------------------------------------------------------------------------


def recent(
    limit: int = 100,
    tool: str = "",
    only_failed: bool = False,
    origin: str = "",
    since_epoch: float = 0.0,
    run: str = "",
) -> list[dict[str, Any]]:
    """最近 N 条记录（时间正序返回，方便直接看调用链）。"""
    limit = max(1, min(int(limit or 100), 2000))
    path = log_path()
    if not path.exists():
        return []

    wanted_tool = str(tool or "").strip()
    wanted_origin = str(origin or "").strip()
    wanted_run = str(run or "").strip()
    tail: deque[dict[str, Any]] = deque(maxlen=limit)

    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue  # 写了一半的行 / 手工改坏了，跳过就行
                if wanted_tool and entry.get("tool") != wanted_tool:
                    continue
                if wanted_origin and entry.get("origin") != wanted_origin:
                    continue
                if wanted_run and entry.get("run") != wanted_run:
                    continue
                if only_failed and entry.get("ok"):
                    continue
                if since_epoch and float(entry.get("epoch") or 0) < since_epoch:
                    continue
                tail.append(entry)
    except OSError as exc:
        log.warning("审计日志读不了: %s", exc)
        return []

    return list(tail)


def summarize(
    limit: int = 500,
    origin: str = "",
    since_minutes: int = 0,
    run: str = "",
) -> dict[str, Any]:
    """按工具聚合最近 N 条记录：调用次数、失败数、耗时、成本档。"""
    since = time.time() - since_minutes * 60 if since_minutes and since_minutes > 0 else 0.0
    entries = recent(limit=limit, origin=origin, since_epoch=since, run=run)

    by_tool: dict[str, dict[str, Any]] = {}
    by_cost: dict[str, int] = {}
    failed_total = 0
    duration_total = 0.0
    tokens = {"prompt": 0, "completion": 0, "total": 0}

    for entry in entries:
        name = str(entry.get("tool") or "")
        bucket = by_tool.setdefault(
            name,
            {
                "tool": name,
                "calls": 0,
                "failed": 0,
                "duration_ms": 0.0,
                "side_effect": entry.get("side_effect") or "",
                "cost": entry.get("cost") or "free",
            },
        )
        bucket["calls"] += 1
        bucket["duration_ms"] = round(bucket["duration_ms"] + float(entry.get("duration_ms") or 0), 1)
        if not entry.get("ok"):
            bucket["failed"] += 1
            failed_total += 1
        cost = str(entry.get("cost") or "free")
        by_cost[cost] = by_cost.get(cost, 0) + 1
        duration_total += float(entry.get("duration_ms") or 0)

        used = entry.get("tokens") or {}
        if used:
            for key in ("prompt", "completion", "total"):
                tokens[key] += int(used.get(key) or 0)

    tools = sorted(by_tool.values(), key=lambda item: (-item["calls"], item["tool"]))
    total = len(entries)

    return {
        "window": total,
        "limit": limit,
        "since_minutes": since_minutes,
        "origin": origin,
        "run": run,
        "failed": failed_total,
        "success_rate": round((total - failed_total) / total, 3) if total else None,
        "total_duration_ms": round(duration_total, 1),
        "by_cost": by_cost,
        "tokens": tokens,
        "by_tool": tools,
        "slowest": sorted(tools, key=lambda item: -item["duration_ms"])[:5],
        "write_calls": sum(
            item["calls"] for item in tools if item["side_effect"] in ("write_case", "write_real")
        ),
        "execute_calls": sum(
            item["calls"] for item in tools if item["side_effect"] == "execute"
        ),
    }


def clear() -> None:
    """清空审计日志（测试用）。"""
    path = log_path()
    with _LOCK:
        path.unlink(missing_ok=True)
        for index in range(1, KEEP_FILES + 1):
            path.with_name(f"{path.name}.{index}").unlink(missing_ok=True)
