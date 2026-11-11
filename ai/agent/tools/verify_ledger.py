"""用例「跑通过」的凭据 —— ``set_case_status`` 转正前要核对。

**为什么需要这份账本**

``set_case_status(status="debugged")`` 等于对外声明「这条用例跑通过、可以进
CI」。在此之前它只是一次文件写入 —— 智能体完全可以在没跑过的情况下标绿，
CI 拿到的是一条从没执行过的用例，等它挂了才发现用例本身就有问题。

所以 ``run_cases`` 每次**确认整体通过**之后，把这次跑过的用例连同内容指纹
记到 ``.cache/verify_ledger.json``；``apply_status_change`` 转 ``debugged``
时要求指纹对得上。没记录、或记录之后用例内容又改过，都不给转正。

**指纹为什么按用例算，不按文件**

同一张 Excel 里改一条用例，整个文件都变样。按文件哈希算，改 A 就把 B 的
通过记录废掉 —— 一个模块十几条用例互相踩，根本没法用。这里对「去掉
``status`` 的用例 dict」取哈希，只认这条用例自身的内容。``status`` 要去掉：
转正本身就是改 status，带着它算，改完之前的记录立刻失效。

**记录条件为什么这么严**

只有「本次真的执行了」且「total == 候选条数」且 failed / error / skipped
全为 0 时才记账。差一条就说明筛选语义和执行结果对不上（比如 ``-k`` 的
匹配范围和 ``iter_cases`` 不同），这时「到底哪几条跑了」说不清 ——
少记只是让智能体再跑一次，误记会让没跑过的用例被标成 debugged。
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from utils import paths
from utils.logs.log_manager import log

#: 账本文件名。放 .cache 下 —— 它是运行态数据，不该进版本库
LEDGER_NAME = "verify_ledger.json"

#: 记录保留天数。过期记录没有意义（用例早该重新验证过）
KEEP_DAYS = 30

#: 账本格式版本，将来结构变了可以据此丢弃旧数据
VERSION = 1


def _ledger_path() -> Any:
    """账本路径。

    **不能在导入时算好**：测试会把 ``paths.CACHE_DIR`` 指到临时目录，
    导入时算死就会往真实的 ``.cache`` 里写测试数据。
    """
    return paths.CACHE_DIR / LEDGER_NAME


def case_key(case: dict[str, Any]) -> str:
    """用例在账本里的键：优先 ``case_id``，YAML 用例退回用例名。

    和 :func:`ai.agent.tools.case_io.case_key` 保持同一套规则 ——
    写记录和查记录必须是同一个键，否则永远查不到。
    """
    return str(case.get("case_id") or case.get("name") or "").strip()


def fingerprint(case: dict[str, Any]) -> str:
    """用例内容指纹（不含 ``status``）。

    Args:
        case: 用例本体（``testcase`` 那一层）

    Returns:
        40 位 sha1。用例内容改一个字符就会变。
    """
    body = {k: v for k, v in case.items() if k != "status"}
    text = json.dumps(body, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _rel(path: Any) -> str:
    """文件相对项目根的路径（和 CaseRef.file 同一套写法）。

    已经是相对路径的直接返回 —— ``Path.resolve()`` 会拿**当前工作目录**
    去拼，MCP 服务的 cwd 不一定是项目根，拼出来就错位了。
    """
    from pathlib import Path

    p = Path(str(path))
    if not p.is_absolute():
        return p.as_posix()
    try:
        resolved = p.resolve()
    except OSError:
        return p.as_posix()
    try:
        return resolved.relative_to(paths.PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        # 项目外的文件（测试的临时目录）：用规范化后的绝对路径，
        # 保证「记账」和「查账」两边算出来的字符串一致
        return resolved.as_posix()


def _load() -> dict[str, dict[str, Any]]:
    """读账本；文件坏了就当成空账本（不能因此让工具全挂）。"""
    path = _ledger_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("跑通过账本读不了，按空账本处理: %s", exc)
        return {}
    if not isinstance(data, dict) or data.get("version") != VERSION:
        return {}
    cases = data.get("cases")
    return cases if isinstance(cases, dict) else {}


def _save(cases: dict[str, dict[str, Any]]) -> None:
    payload = {"version": VERSION, "cases": cases}
    path = _ledger_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # 先写临时文件再替换：中途失败不会留下半截 JSON（半截账本 = 全查不到）
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(path)
    except OSError as exc:
        log.warning("跑通过账本写不了（不影响本次执行）: %s", exc)


def _prune(cases: dict[str, dict[str, Any]], now: float | None = None) -> dict[str, dict[str, Any]]:
    """丢掉过期记录。"""
    now = time.time() if now is None else now
    cutoff = now - KEEP_DAYS * 86400
    kept: dict[str, dict[str, Any]] = {}
    for key, entry in cases.items():
        if not isinstance(entry, dict):
            # 账本被手改坏（某条记录写成字符串/数字）：丢掉那一条，
            # 不能让整份账本从此记不进去。
            log.warning("跑通过账本里有格式不对的记录，已丢弃: %r", key)
            continue
        try:
            at = float(entry.get("ts") or 0)
        except (TypeError, ValueError):
            at = 0
        if at >= cutoff:
            kept[key] = entry
    return kept


def record(pairs: list[tuple[Any, dict[str, Any]]]) -> int:
    """把「这次跑通过」的用例记进账本。

    Args:
        pairs: ``[(CaseRef, 用例本体), ...]`` —— 和 ``iter_cases`` 的返回一致

    Returns:
        实际写入的记录条数。
    """
    if not pairs:
        return 0

    cases = _prune(_load())
    now = time.time()
    stamped = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
    written = 0
    for ref, case in pairs:
        key = case_key(case)
        if not key:
            continue
        cases[key] = {
            "fp": fingerprint(case),
            "file": _rel(getattr(ref, "file", "")),
            "at": stamped,
            "ts": now,
        }
        written += 1
    if written:
        _save(cases)
    return written


def verified_state(file_path: Any, case: dict[str, Any]) -> tuple[bool, str]:
    """这条用例现在能不能证明「跑通过」。

    Returns:
        ``(是否可信, 不可信的原因)``。可信时原因为空串。
    """
    key = case_key(case)
    if not key:
        return False, "这条用例既没有编号也没有名字，无法核对"

    entry = _load().get(key)
    if not isinstance(entry, dict) or not entry:
        # 记录被写成字符串/数字时按「没有记录」处理 —— 否则 ``entry.get``
        # 抛的是 AttributeError，``set_case_status`` 会把内部异常漏给调用方。
        return False, "账本里没有它的通过记录"

    expected_file = _rel(file_path)
    if str(entry.get("file") or "") != expected_file:
        # 同名 YAML 用例可能来自另一个模块 —— 不能拿别人的记录顶数
        return False, "通过记录来自另一个用例文件"

    if str(entry.get("fp") or "") != fingerprint(case):
        return False, "用例内容在跑通过之后又改过"

    return True, ""


def reset() -> None:
    """清空账本（测试用）。"""
    try:
        _ledger_path().unlink()
    except OSError:
        pass


__all__ = ["record", "verified_state", "fingerprint", "case_key", "reset"]
