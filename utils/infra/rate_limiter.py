"""令牌桶限流 —— 保护测试环境不被自己压垮。

**为什么需要**：接口测试常常一口气并发几十条请求，服务端直接返回
限流错误（mall4j 就是 ``code=1003`` "访问过于频繁"），或者直接把
测试环境的连接池打满。表现出来的现象是"用例时好时坏"，
最让人怀疑测试本身。

**令牌桶怎么工作**（大白话）：

- 桶里放令牌，桶有上限（``burst``）
- 每放一个令牌进来，就算"允许发一个请求"
- 请求来了就拿一个令牌；桶空了就在旁边等一会儿（最多等到超时）
- 按固定速率往桶里放令牌（``qps``）

和"固定间隔排队"相比，令牌桶允许**短时间突发**（桶里攒着令牌），
但长期平均速率不会超过 ``qps``。

**按项目分别限速**：mall4j 的统计接口限制严，就单独给它配一个更低的 QPS，
别把整个项目的接口都卡住。

配置（``config.yaml``）::

    rate_limit:
      enabled: true
      default: {qps: 10, burst: 20}
      projects:
        mall4j: {qps: 5, burst: 10}
        public_api: {qps: 20, burst: 40}

也可以用环境变量临时关掉::

    $env:APITEST_NO_RATE_LIMIT = "1"
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

from utils.logs.log_manager import log


class TokenBucket:
    """令牌桶（线程安全）。"""

    def __init__(self, rate: float, capacity: float, wait_timeout: float = 30.0):
        """初始化。

        Args:
            rate: 每秒放多少个令牌（就是 QPS）
            capacity: 桶最多装多少个令牌（突发上限）
            wait_timeout: 没令牌时最多等多久（秒）
        """
        if rate <= 0:
            raise ValueError(f"令牌桶速率必须大于 0，当前 {rate}")

        self.rate = float(rate)
        # 桶容量就是"允许突发多少个请求" —— 不能被 rate 撑大。
        # （之前写成 max(capacity, rate) 的后果：qps=10 / burst=5 时
        #   桶实际能装 10 个，突发限制形同虚设。）
        self.capacity = float(capacity if capacity > 0 else rate)
        if self.capacity < 1:
            self.capacity = 1.0
        self.wait_timeout = float(wait_timeout)

        self._tokens = float(self.capacity)
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        """按流逝的时间补令牌。"""
        now = time.monotonic()
        elapsed = now - self._last_refill
        if elapsed <= 0:
            return
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
        self._last_refill = now

    def acquire(self, tokens: float = 1.0) -> float:
        """拿令牌（拿不到就等）。

        Args:
            tokens: 要几个令牌

        Returns:
            实际等待的秒数（0 表示没等）

        Raises:
            TimeoutError: 等超时了还没拿到令牌
        """
        deadline = time.monotonic() + self.wait_timeout

        while True:
            with self._lock:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return 0.0
                # 还差多少令牌 -> 需要等多久
                missing = tokens - self._tokens
                wait_for = missing / self.rate

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"等令牌超时（{self.wait_timeout}s）：限流 "
                    f"{self.rate}/s，桶容量 {self.capacity}"
                )

            # 睡一小会儿再看（不用睡到刚好够，避免醒来还是不够）
            time.sleep(min(wait_for, remaining, 0.05))

    def try_acquire(self, tokens: float = 1.0) -> bool:
        """试拿令牌，拿不到就算了（不等）。"""
        with self._lock:
            self._refill()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False

    @property
    def available(self) -> float:
        """当前桶里还有几个令牌（调试用）。"""
        with self._lock:
            self._refill()
            return self._tokens


class RateLimiter:
    """限流器总管 —— 按项目分别限速。"""

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = dict(config or {})
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()
        self._enabled = self._resolve_enabled()

    # ---------------- 配置 ----------------

    def _resolve_enabled(self) -> bool:
        """限流是不是开着。

        三个开关，从高到低：
        环境变量 ``APITEST_NO_RATE_LIMIT`` > 配置里的 ``enabled`` > 默认开着。
        """
        env = os.environ.get("APITEST_NO_RATE_LIMIT", "").strip()
        if env in ("1", "true", "yes"):
            return False

        enabled = self.config.get("enabled")
        if enabled is None:
            return True
        return bool(enabled)

    def _bucket_for(self, key: str) -> TokenBucket:
        """取某个 key（一般是项目名）的桶，没有就按配置造一个。"""
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is not None:
                return bucket

            spec = self._spec_for(key)
            bucket = TokenBucket(
                rate=spec["qps"],
                capacity=spec["burst"],
                wait_timeout=spec.get("wait_timeout", 30.0),
            )
            self._buckets[key] = bucket
            log.info(
                "限流器就绪: %s -> %s/s（桶容量 %s）",
                key, spec["qps"], spec["burst"],
            )
            return bucket

    def _spec_for(self, key: str) -> dict[str, Any]:
        """算某个 key 的限流参数。"""
        default = self.config.get("default") or {}
        projects = self.config.get("projects") or {}
        spec = {**default, **(projects.get(key) or {})}

        return {
            "qps": float(spec.get("qps") or 10),
            "burst": float(spec.get("burst") or spec.get("qps") or 10),
            "wait_timeout": float(spec.get("wait_timeout") or 30.0),
        }

    # ---------------- 对外 ----------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    def acquire(self, key: str = "default") -> float:
        """按 key 拿令牌（限流关着就直接返回）。

        **并行时用跨进程共享的限流器** —— 进程内的令牌桶在多进程下会失效
        （4 个进程各 5 QPS = 实际 20 QPS），跨进程版本才能保证合计 QPS 正确。
        """
        if not self._enabled:
            return 0.0

        waited = self._acquire_shared(key) if self._use_shared() else self._bucket_for(key).acquire()

        if waited > 0.05:
            log.info("限流等待了 %.2fs（%s）", waited, key)
        return waited

    def _use_shared(self) -> bool:
        """要不要用跨进程限流器。"""
        try:
            from utils.infra.parallel import is_coordination_enabled

            return is_coordination_enabled()
        except Exception:  # noqa: BLE001
            return False

    def _acquire_shared(self, key: str) -> float:
        """用跨进程限流器拿令牌。"""
        from utils.infra.parallel import SharedRateLimiter

        spec = self._spec_for(key)
        limiter = SharedRateLimiter(
            key=key,
            qps=spec["qps"],
            burst=spec["burst"],
            wait_timeout=spec.get("wait_timeout", 30.0),
        )
        return limiter.acquire()

    def try_acquire(self, key: str = "default") -> bool:
        """试拿令牌（不等）。"""
        if not self._enabled:
            return True

        if self._use_shared():
            # 跨进程模式下不"试拿" —— 直接拿，等价于阻塞拿
            try:
                self._acquire_shared(key)
                return True
            except TimeoutError:
                return False

        return self._bucket_for(key).try_acquire()

    def reset(self) -> None:
        """清空所有桶（单元测试用）。"""
        with self._lock:
            self._buckets.clear()


# 全局限流器（session 级）
_limiter: RateLimiter | None = None
_limiter_lock = threading.Lock()


def get_limiter(config: dict[str, Any] | None = None) -> RateLimiter:
    """取全局限流器（第一次调用时会用配置初始化）。"""
    global _limiter
    if _limiter is None:
        with _limiter_lock:
            if _limiter is None:
                _limiter = RateLimiter(config or {})
    return _limiter


def reset_limiter() -> None:
    """重置全局限流器（配置变了或者单元测试要用）。"""
    global _limiter
    with _limiter_lock:
        if _limiter is not None:
            _limiter.reset()
        _limiter = None
