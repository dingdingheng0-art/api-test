"""并行执行协调：让 pytest-xdist 真正能安全地跑。

**问题在哪**

``pytest -n 4`` 会起 4 个独立**进程**。每个进程都是全新的 Python 解释器，
所有模块级状态都是各自一份。于是并行时会出现三个问题：

**① 重复登录**
每个 worker 各自触发一次滑块登录（约 6 秒），4 个 worker 就是 24 秒 ——
比串行还慢。而且滑块 uuid 是一次性凭证，多进程同时抢登录容易触发限流。

**② 限流失效**
限流器是进程内的令牌桶。4 个进程各有一个桶，实际并发 QPS 是配置值的 4 倍，
等于没限流 —— 测试环境容易被自己压垮。

**③ 报告散落**
各 worker 写自己的 allure-results 目录，合并逻辑不对会出现报告不全。

**这个模块怎么解决**

- **登录共享**：worker 之间用文件锁 + 缓存文件协调。第一个要登录的 worker
  去登录，其余 worker 等它登录完直接复用缓存（和 :class:`TokenCache` 配合）。
- **限流跨进程**：限流器改成基于文件的共享计数，多进程合计 QPS 正确。
- **报告合并**：在 session 结束时把各 worker 的 allure 结果合并。

**怎么用**

直接 ``pytest -n 4`` 就行，不需要额外配置。框架会自动检测是否在并行模式
（看 ``PYTEST_XDIST_WORKER`` 环境变量），只在并行时才启用协调逻辑。

也可以显式关掉::

    APITEST_NO_PARALLEL_COORD=1 pytest -n 4
"""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from utils.logs.log_manager import log
from utils.paths import CACHE_DIR

# xdist 给每个 worker 设的环境变量
WORKER_ENV = "PYTEST_XDIST_WORKER"
# xdist 设定 worker 总数的环境变量
WORKER_COUNT_ENV = "PYTEST_XDIST_WORKER_COUNT"

# 协调用的目录
COORD_DIR = CACHE_DIR / "parallel"

# 登录锁文件名
LOGIN_LOCK = "login.lock"
# 跨进程限流状态文件名
RATE_LIMIT_STATE = "rate_limit.json"


def is_parallel() -> bool:
    """当前是不是在并行模式（xdist worker）。"""
    return bool(os.environ.get(WORKER_ENV, "").strip())


def worker_id() -> str:
    """当前 worker 的编号（主进程返回 ``master``）。"""
    return os.environ.get(WORKER_ENV, "").strip() or "master"


def worker_count() -> int:
    """总共几个 worker（主进程返回 1）。"""
    raw = os.environ.get(WORKER_COUNT_ENV, "").strip()
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 1


def is_coordination_enabled() -> bool:
    """协调功能开不开。

    环境变量 ``APITEST_NO_PARALLEL_COORD=1`` 可以关掉（调试时用）。
    """
    if os.environ.get("APITEST_NO_PARALLEL_COORD", "").strip() in ("1", "true", "yes"):
        return False
    return is_parallel()


def _ensure_coord_dir() -> Path:
    COORD_DIR.mkdir(parents=True, exist_ok=True)
    return COORD_DIR


@contextmanager
def file_lock(name: str, timeout: float = 120.0) -> Iterator[bool]:
    """跨进程文件锁。

    Windows 和 Linux 实现不同：Windows 用 ``msvcrt.locking``，
    Linux 用 ``fcntl.flock``。

    Yields:
        True 表示拿到了锁，False 表示超时（没拿到）
    """
    _ensure_coord_dir()
    lock_path = COORD_DIR / name

    handle = None
    acquired = False

    try:
        handle = open(lock_path, "a+")
    except OSError as exc:
        log.warning("打不开锁文件 %s: %s", lock_path, exc)
        yield False
        return

    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                # 锁被别人占着，等一会儿再试
                time.sleep(0.2)

        yield acquired
    finally:
        if acquired:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        try:
            handle.close()
        except OSError:
            pass


def coordinated_login(project_name: str, do_login: Any) -> Any:
    """协调登录：多个 worker 抢同一个项目时，只让一个真的登录。

    Args:
        project_name: 项目名
        do_login: 无参可调用对象，返回登录结果（dict）

    Returns:
        登录结果字典
    """
    if not is_coordination_enabled():
        return do_login()

    with file_lock(LOGIN_LOCK) as locked:
        if not locked:
            # 拿不到锁就自己登录（总比卡死强）
            log.warning("拿不到登录锁，%s 自行登录", project_name)
            return do_login()

        # 拿到锁了 —— 先看缓存里有没有别的 worker 刚登录好的
        from utils.infra.token_cache import TokenCache

        cached = TokenCache.get(project_name)
        if cached:
            log.info("%s 复用其它 worker 刚登录的凭据", project_name)
            return {
                "token": cached["token"],
                "refresh_token": cached.get("refresh_token", ""),
                "uuid": cached.get("uuid", ""),
                "headers": cached.get("headers", {}),
                "from_cache": True,
            }

        return do_login()


class SharedRateLimiter:
    """跨进程限流器。

    令牌桶在进程内是准的，多进程就不准了 —— 4 个进程各 5 QPS，
    实际就是 20 QPS。

    这个类用文件记录"上一批令牌的时间戳和数量"，所有进程共享同一份状态，
    这样多进程合计的 QPS 才等于配置值。

    **代价**：每次取令牌要读写一次文件，比内存里的慢一点。
    但限流本来就不快，这点开销可以接受。
    """

    def __init__(self, key: str, qps: float, burst: int, wait_timeout: float = 30.0):
        self.key = key
        self.qps = max(0.1, float(qps))
        self.burst = max(1, int(burst))
        self.wait_timeout = float(wait_timeout)
        self._state_file = _ensure_coord_dir() / f"rate_{key}.json"

    def acquire(self) -> float:
        """拿一个令牌，返回等待秒数。"""
        deadline = time.monotonic() + self.wait_timeout

        while True:
            with file_lock(f"rate_{self.key}.lock", timeout=5.0):
                state = self._read_state()
                now = time.time()

                # 按时间补充令牌
                elapsed = now - state.get("updated_at", now)
                tokens = state.get("tokens", float(self.burst))
                tokens = min(self.burst, tokens + elapsed * self.qps)

                if tokens >= 1.0:
                    self._write_state(tokens - 1.0, now)
                    return 0.0

                # 不够，算要等多久
                missing = 1.0 - tokens
                wait_for = missing / self.qps
                self._write_state(tokens, now)

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"限流等待超时（{self.key}，{self.qps}/s）"
                )
            time.sleep(min(wait_for, remaining))

    def _read_state(self) -> dict[str, Any]:
        try:
            import json

            if self._state_file.exists():
                data = json.loads(self._state_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except (OSError, ValueError):
            pass
        return {"tokens": float(self.burst), "updated_at": time.time()}

    def _write_state(self, tokens: float, updated_at: float) -> None:
        try:
            import json

            self._state_file.write_text(
                json.dumps({"tokens": tokens, "updated_at": updated_at}),
                encoding="utf-8",
            )
        except OSError as exc:
            log.debug("写限流状态失败: %s", exc)


def worker_report_dir(base_dir: Path) -> Path:
    """各 worker 独立的报告目录。

    并行时每个 worker 写自己的子目录，session 结束再合并 ——
    否则多个进程同时写同一个目录，allure 结果会互相覆盖。
    """
    if not is_parallel():
        return base_dir

    target = base_dir / f"worker-{worker_id()}"
    target.mkdir(parents=True, exist_ok=True)
    return target


def describe() -> str:
    """当前并行状态的一句话描述（启动时打日志用）。"""
    if not is_parallel():
        return "串行执行"
    return f"并行执行（worker {worker_id()} / 共 {worker_count()} 个）"
