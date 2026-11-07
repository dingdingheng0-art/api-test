"""清理栈 —— 保证"造出来的数据一定会被清掉"。

**问题**：用例里造了数据，用例中途失败了，teardown 没跑到，数据就留在库里了。
下次跑可能撞到脏数据，排查起来很痛苦。

**做法**：工厂造数据时把"钥匙"（主键）登记进清理栈。
不管用例成功还是失败，session 结束前都会统一清一遍。

清理栈是 **session 级** 的（一次运行共用一个），按登记顺序的**倒序**清理 ——
后造的先清，避免外键约束挡住。
"""

from __future__ import annotations

import threading
from typing import Any, Callable

from utils.logs.log_manager import log


class CleanupStack:
    """待清理任务的栈。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # 每项是 (描述, 清理函数)
        self._tasks: list[tuple[str, Callable[[], Any]]] = []

    def push(self, description: str, cleanup: Callable[[], Any]) -> None:
        """登记一个清理任务。

        Args:
            description: 描述（出问题时日志里能看到是啥没清掉）
            cleanup: 清理函数（无参数）
        """
        with self._lock:
            self._tasks.append((description, cleanup))

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._tasks)

    def run_all(self) -> dict[str, Any]:
        """把所有清理任务跑一遍（倒序）。

        **单个任务失败不影响其它任务** —— 清理失败只记日志，
        不能因为一条数据删不掉就把整个 session 判成失败。

        Returns:
            ``{"total": 总数, "succeeded": 成功数, "failed": 失败数, "errors": [...]}``
        """
        with self._lock:
            tasks = list(reversed(self._tasks))
            self._tasks.clear()

        succeeded = 0
        errors: list[dict[str, str]] = []

        for description, cleanup in tasks:
            try:
                cleanup()
                succeeded += 1
                log.info("已清理: %s", description)
            except Exception as exc:  # noqa: BLE001 - 清理失败不往上抛
                errors.append({"item": description, "error": str(exc)})
                log.warning("清理失败（已忽略）: %s -> %s", description, exc)

        result = {
            "total": len(tasks),
            "succeeded": succeeded,
            "failed": len(errors),
            "errors": errors,
        }
        if tasks:
            log.info(
                "清理完成: 共 %s 项，成功 %s，失败 %s",
                result["total"], result["succeeded"], result["failed"],
            )
        return result

    def clear(self) -> None:
        """丢弃所有待清理任务（不执行）。"""
        with self._lock:
            self._tasks.clear()


# 全局清理栈（session 级）
stack = CleanupStack()


def track_factory(factory: Any, target: CleanupStack | None = None) -> None:
    """把工厂登记到清理栈 —— 用例失败也能兜底清掉。

    工厂造完数据后调一下这个函数即可。

    Args:
        factory: 工厂实例
        target: 登记到哪个清理栈（默认全局那个）
    """
    the_stack = target if target is not None else stack

    def _cleanup() -> None:
        keys = factory.tracked
        if not keys:
            return
        try:
            factory.cleanup(keys)
        finally:
            factory.clear_tracked()

    the_stack.push(f"工厂 {factory.full_name} 造的数据", _cleanup)

