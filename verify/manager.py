"""校验层的"总管"：管住整个 session 的数据库 / 缓存 / MQ 连接。

**为什么要总管**：连接很贵（建一次 TCP + 认证）。如果每条断言都新建连接，
跑几百条用例会慢到无法接受。所以整个 session 只建一次连接，用例共享。

生命周期：

- session 开始 → :meth:`VerifyManager.setup`（读配置、建连接）
- 用例跑的时候 → 断言处理器来这里拿客户端
- session 结束 → :meth:`VerifyManager.teardown`（关连接）

用法（框架内部调用，不用管）::

    from verify.manager import VerifyManager

    manager = VerifyManager.get()
    manager.setup({"db": {...}, "redis": {...}, "mq": {...}})
"""

from __future__ import annotations

import threading
from typing import Any

from utils.logs.log_manager import log
from verify.db import DatabaseClient
from verify.errors import VerifyConfigError
from verify.mq import BaseBackend, build_backend
from verify.redis import RedisClient


class VerifyManager:
    """校验层总管（单例）。"""

    _instance: "VerifyManager | None" = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._db_clients: dict[str, DatabaseClient] = {}
        self._redis_clients: dict[str, RedisClient] = {}
        self._mq_backends: dict[str, BaseBackend] = {}
        self._mq_subscribed: set[tuple[str, str]] = set()
        self._configured = False

    # ---------------- 单例 ----------------

    @classmethod
    def get(cls) -> "VerifyManager":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    # ---------------- 配置 ----------------

    def setup(self, config: dict[str, Any] | None = None) -> None:
        """按配置准备好各类客户端。

        配置结构（在 ``config.yaml`` 里）::

            db:
              default:
                type: mysql
                host: 127.0.0.1
                user: root
                password: "${DB_PASSWORD}"
                database: shop
            redis:
              default:
                host: 127.0.0.1
                port: 6379
                db: 0
            mq:
              default:
                type: rabbitmq
                host: 127.0.0.1
                port: 5672
                user: guest
                password: "${MQ_PASSWORD}"

        也支持扁平的写法（不嵌套别名）::

            db: {type: mysql, host: ...}
            redis: {host: ...}
            mq: {type: rabbitmq, ...}
        """
        cfg = config or {}
        self._configured = True

        self._setup_db(cfg.get("db"))
        self._setup_redis(cfg.get("redis"))
        self._setup_mq(cfg.get("mq"))

    def _setup_db(self, raw: Any) -> None:
        if not raw:
            return
        for alias, cfg in _normalize_aliases(raw, "db").items():
            self._db_clients[alias] = DatabaseClient(cfg)
            log.info("数据库客户端就绪: %s", alias)

    def _setup_redis(self, raw: Any) -> None:
        if not raw:
            return
        for alias, cfg in _normalize_aliases(raw, "redis").items():
            self._redis_clients[alias] = RedisClient(cfg)
            log.info("Redis 客户端就绪: %s", alias)

    def _setup_mq(self, raw: Any) -> None:
        if not raw:
            return
        for alias, cfg in _normalize_aliases(raw, "mq").items():
            self._mq_backends[alias] = build_backend(cfg)
            log.info("MQ 后端就绪: %s (%s)", alias, cfg.get("type", "自动"))

    # ---------------- 取客户端 ----------------

    def db(self, alias: str = "default") -> DatabaseClient:
        """取数据库客户端。

        没配数据库配置 → 抛"未配置"错误（而不是 ImportError）。
        """
        client = self._db_clients.get(alias)
        if client is None:
            if not self._db_clients:
                raise VerifyConfigError(
                    "用例里用了数据库校验，但 config.yaml 里没有 db 配置。"
                    "加一段：db: {default: {type: mysql, host: ..., database: ...}}"
                )
            raise VerifyConfigError(
                f"没有名为 {alias!r} 的数据库配置"
                f"（可用的：{', '.join(self._db_clients)}）"
            )
        return client

    def redis(self, alias: str = "default") -> RedisClient:
        """取 Redis 客户端。"""
        client = self._redis_clients.get(alias)
        if client is None:
            if not self._redis_clients:
                raise VerifyConfigError(
                    "用例里用了缓存校验，但 config.yaml 里没有 redis 配置。"
                    "加一段：redis: {default: {host: 127.0.0.1, port: 6379, db: 0}}"
                )
            raise VerifyConfigError(
                f"没有名为 {alias!r} 的 Redis 配置"
                f"（可用的：{', '.join(self._redis_clients)}）"
            )
        return client

    def mq(self, alias: str = "default") -> BaseBackend:
        """取 MQ 后端。"""
        backend = self._mq_backends.get(alias)
        if backend is None:
            if not self._mq_backends:
                raise VerifyConfigError(
                    "用例里用了 MQ 校验，但 config.yaml 里没有 mq 配置。"
                    "加一段：mq: {default: {type: rabbitmq, host: ..., port: 5672}}"
                )
            raise VerifyConfigError(
                f"没有名为 {alias!r} 的 MQ 配置"
                f"（可用的：{', '.join(self._mq_backends)}）"
            )
        return backend

    def has_db(self) -> bool:
        return bool(self._db_clients)

    def has_redis(self) -> bool:
        return bool(self._redis_clients)

    def has_mq(self) -> bool:
        return bool(self._mq_backends)

    # ---------------- MQ 订阅管理 ----------------

    def ensure_subscribed(self, queue: str, alias: str = "default") -> None:
        """确保某个队列已经被订阅（重复调用不会重复订阅）。"""
        key = (alias, queue)
        if key in self._mq_subscribed:
            return
        self.mq(alias).subscribe(queue)
        self._mq_subscribed.add(key)

    # ---------------- 收尾 ----------------

    def teardown(self) -> None:
        """关掉所有连接。"""
        for client in self._db_clients.values():
            _safe_close(client)
        self._db_clients.clear()

        for client in self._redis_clients.values():
            _safe_close(client)
        self._redis_clients.clear()

        for backend in self._mq_backends.values():
            _safe_close(backend)
        self._mq_backends.clear()
        self._mq_subscribed.clear()
        self._configured = False

    def reset(self) -> None:
        """清空所有状态（单元测试之间隔离用）。"""
        self.teardown()
        self._configured = False


def _safe_close(target: Any) -> None:
    """关连接 —— 对方没 close 方法或者关了会报错，都不影响别人。"""
    close = getattr(target, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception:  # noqa: BLE001 - 关连接失败不用管
        pass


def _normalize_aliases(raw: Any, kind: str) -> dict[str, dict[str, Any]]:
    """把配置整理成 ``{别名: 配置}``。

    支持两种写法：

    - 扁平的：``db: {type: mysql, host: ...}`` → ``{"default": {...}}``
    - 嵌套的：``db: {default: {...}, other: {...}}`` → 原样
    """
    if not isinstance(raw, dict):
        return {}

    # 有别名键（值是字典）就是嵌套写法
    if all(isinstance(v, dict) for v in raw.values()) and raw:
        return {str(k): dict(v) for k, v in raw.items()}

    # 扁平写法 —— 但要排除 "type" 这种连接参数键
    type_keys = {"type", "host", "port", "db", "user", "password", "params", "database"}
    if any(k in type_keys for k in raw):
        return {"default": dict(raw)}

    # 全是别名（每个值都是字典）
    return {str(k): dict(v) for k, v in raw.items() if isinstance(v, dict)}
