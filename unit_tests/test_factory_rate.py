"""测试数据工厂 + 清理栈 + 限流器的单元测试。"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from factory.base import DataFactory, FactoryError, FactoryRegistry, load_factories
from factory.cleanup import CleanupStack, track_factory
from utils.infra.rate_limiter import RateLimiter, TokenBucket


# ---------------------------------------------------------------------------
# 工厂基类
# ---------------------------------------------------------------------------


class DummyFactory(DataFactory):
    """测试用工厂 —— 记录造了什么、清了什么。"""

    name = "dummy"

    def __init__(self, **options: object) -> None:
        super().__init__(**options)
        self.created: list[dict] = []
        self.cleaned: list = []

    def create(self, count: int = 1, **params: object) -> list[dict]:
        rows = []
        for index in range(count):
            row = {"id": f"{self.name}-{index}", "params": params}
            rows.append(row)
        self.created.extend(rows)
        self.track(row["id"] for row in rows)
        return rows

    def cleanup(self, keys: object) -> int:
        keys = list(keys or [])  # type: ignore[arg-type]
        self.cleaned.extend(keys)
        return len(keys)


class TestDataFactory:
    def test_track_registers_keys(self) -> None:
        factory = DummyFactory()
        factory.create(count=3)
        assert len(factory.tracked) == 3

    def test_track_deduplicates(self) -> None:
        factory = DummyFactory()
        factory.track(["a", "a", "b"])
        assert factory.tracked == ["a", "b"]

    def test_clear_tracked(self) -> None:
        factory = DummyFactory()
        factory.track(["a"])
        factory.clear_tracked()
        assert factory.tracked == []

    def test_full_name(self) -> None:
        factory = DummyFactory()
        assert factory.full_name.endswith(".dummy")

    def test_base_create_raises(self) -> None:
        class Broken(DataFactory):
            name = "broken"

        with pytest.raises(NotImplementedError):
            Broken().create()


class TestFactoryRegistry:
    def test_register_and_get(self) -> None:
        registry = FactoryRegistry()
        factory = DummyFactory()
        registry.register(factory, "test.dummy")
        assert registry.get("test.dummy") is factory
        assert registry.has("test.dummy")

    def test_get_missing_raises_clear_error(self) -> None:
        registry = FactoryRegistry()
        with pytest.raises(FactoryError) as excinfo:
            registry.get("nope")
        assert "nope" in str(excinfo.value)

    def test_names_sorted(self) -> None:
        registry = FactoryRegistry()
        registry.register(DummyFactory(), "b.dummy")
        registry.register(DummyFactory(), "a.dummy")
        assert registry.names() == ["a.dummy", "b.dummy"]


# ---------------------------------------------------------------------------
# 清理栈
# ---------------------------------------------------------------------------


class TestCleanupStack:
    def test_runs_all_tasks(self) -> None:
        stack = CleanupStack()
        called: list[str] = []
        stack.push("任务A", lambda: called.append("A"))
        stack.push("任务B", lambda: called.append("B"))

        result = stack.run_all()

        assert result["total"] == 2
        assert result["succeeded"] == 2
        assert result["failed"] == 0
        assert called == ["B", "A"]      # 倒序清理

    def test_failure_does_not_stop_others(self) -> None:
        """一条清理失败不能挡住其它清理。"""
        stack = CleanupStack()
        called: list[str] = []

        def boom() -> None:
            raise RuntimeError("删不掉")

        stack.push("好的", lambda: called.append("ok"))
        stack.push("坏的", boom)

        result = stack.run_all()

        assert result["total"] == 2
        assert result["succeeded"] == 1
        assert result["failed"] == 1
        assert called == ["ok"]

    def test_stack_is_cleared_after_run(self) -> None:
        stack = CleanupStack()
        stack.push("x", lambda: None)
        stack.run_all()
        assert stack.size == 0

    def test_track_factory_registers_cleanup(self) -> None:
        stack = CleanupStack()
        factory = DummyFactory()
        factory.create(count=2)

        track_factory(factory, stack)

        assert stack.size == 1

        stack.run_all()
        assert len(factory.cleaned) == 2
        assert factory.tracked == []       # 清完要清空登记


# ---------------------------------------------------------------------------
# 令牌桶
# ---------------------------------------------------------------------------


class TestTokenBucket:
    def test_initial_tokens_available(self) -> None:
        bucket = TokenBucket(rate=10, capacity=5)
        assert bucket.available == 5

    def test_acquire_consumes_tokens(self) -> None:
        bucket = TokenBucket(rate=1000, capacity=5)
        for _ in range(5):
            bucket.acquire()
        assert bucket.available < 1

    def test_try_acquire_returns_false_when_empty(self) -> None:
        bucket = TokenBucket(rate=1, capacity=1)
        assert bucket.try_acquire() is True
        assert bucket.try_acquire() is False

    def test_acquire_waits_for_token(self) -> None:
        """桶空了要等 —— 等完能拿到令牌。"""
        bucket = TokenBucket(rate=50, capacity=1, wait_timeout=2)  # 20ms 一个令牌
        bucket.acquire()          # 用掉唯一的令牌
        started = time.perf_counter()
        bucket.acquire()          # 要等约 20ms
        elapsed = time.perf_counter() - started
        assert elapsed > 0.005

    def test_acquire_timeout_raises(self) -> None:
        bucket = TokenBucket(rate=0.1, capacity=1, wait_timeout=0.1)
        bucket.acquire()
        with pytest.raises(TimeoutError):
            bucket.acquire()

    def test_capacity_never_exceeds_burst(self) -> None:
        bucket = TokenBucket(rate=1000, capacity=3)
        time.sleep(0.05)          # 让它多攒一会儿
        assert bucket.available <= 3

    def test_invalid_rate_raises(self) -> None:
        with pytest.raises(ValueError):
            TokenBucket(rate=0, capacity=1)


class TestRateLimiter:
    def test_disabled_never_blocks(self) -> None:
        limiter = RateLimiter({"enabled": False})
        assert limiter.enabled is False
        assert limiter.acquire("mall4j") == 0.0

    def test_project_specific_config(self) -> None:
        limiter = RateLimiter(
            {
                "enabled": True,
                "default": {"qps": 10, "burst": 10},
                "projects": {"mall4j": {"qps": 2, "burst": 2}},
            }
        )
        # mall4j 的桶应该比 default 小
        limiter.acquire("mall4j")
        limiter.acquire("mall4j")
        assert limiter._bucket_for("mall4j").available < 1
        assert limiter._bucket_for("other").available > 5

    def test_env_var_disables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APITEST_NO_RATE_LIMIT", "1")
        limiter = RateLimiter({"enabled": True})
        assert limiter.enabled is False
