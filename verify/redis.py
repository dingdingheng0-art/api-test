"""Redis（缓存）校验。

支持校验四件事：

- **值对不对** —— 缓存里的内容是不是预期的
- **有没有写进去** —— key 存不存在
- **过期时间对不对** —— TTL 是不是在预期范围内
- **有没有失效** —— key 有没有被删掉（"更新后删缓存"这类场景）

**依赖是可选的**：没装 ``redis`` 包就用不了 redis 断言，
但框架其它部分照跑（报错是句人话，不是 ImportError 栈）。

配置（``config.yaml``）::

    redis:
      host: 127.0.0.1
      port: 6379
      db: 0
      password: "${REDIS_PASSWORD}"
      decode_responses: true

多个 Redis 可以给别名::

    redis:
      default: {host: 127.0.0.1, port: 6379, db: 0}
      cache2: {host: 127.0.0.1, port: 6380, db: 1}
"""

from __future__ import annotations

import json
import time
from typing import Any

from utils.logs.log_manager import log
from verify.errors import DependencyMissing, VerifyConfigError, VerifyError


class RedisClient:
    """Redis 客户端（懒连接，连接复用）。

    Args:
        config: 连接配置。``host`` / ``port`` / ``db`` / ``password`` / ``decode_responses``
    """

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = dict(config or {})
        self._client: Any = None

    # ---------------- 连接管理 ----------------

    def _connect(self) -> Any:
        """建立连接（连一次，之后复用）。"""
        if self._client is not None:
            return self._client

        try:
            import redis
        except ImportError as exc:
            raise DependencyMissing(
                "缓存校验需要 redis 包。装一下：uv pip install redis"
            ) from exc

        params: dict[str, Any] = {
            "host": self.config.get("host") or "127.0.0.1",
            "port": int(self.config.get("port") or 6379),
            "db": int(self.config.get("db") or 0),
        }
        password = self.config.get("password")
        if password:
            params["password"] = password

        username = self.config.get("username")
        if username:
            params["username"] = username

        # decode_responses 默认 True —— 拿到的直接是 str，不用自己 decode
        decode = self.config.get("decode_responses")
        params["decode_responses"] = True if decode is None else bool(decode)

        socket_timeout = self.config.get("socket_timeout")
        if socket_timeout:
            params["socket_timeout"] = float(socket_timeout)

        try:
            self._client = redis.Redis(**params)
            # 连一下确认通（不然第一次断言才发现连不上，报错不直观）
            self._client.ping()
        except Exception as exc:
            raise VerifyConfigError(
                f"连 Redis 失败（{params.get('host')}:{params.get('port')}）: {exc}"
            ) from exc

        log.info("Redis 已连接: %s:%s db=%s",
                 params.get("host"), params.get("port"), params.get("db"))
        return self._client

    def close(self) -> None:
        """断开连接。"""
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    def __enter__(self) -> "RedisClient":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    # ---------------- 读取 ----------------

    def get(self, key: str) -> Any:
        """取一个 key 的值（不存在返回 None）。"""
        client = self._connect()
        started = time.perf_counter()
        try:
            value = client.get(key)
        except Exception as exc:
            raise VerifyError(f"读 Redis 失败: {key} -> {exc}") from exc
        elapsed = int((time.perf_counter() - started) * 1000)
        log.info("Redis GET %s -> %r / %sms", key, _short(value), elapsed)
        return value

    def exists(self, key: str) -> bool:
        """key 是否存在。"""
        client = self._connect()
        try:
            return bool(client.exists(key))
        except Exception as exc:
            raise VerifyError(f"查 Redis 失败: {key} -> {exc}") from exc

    def ttl(self, key: str) -> int:
        """key 的剩余存活秒数。

        - ``-1`` —— key 在，但没有过期时间
        - ``-2`` —— key 不存在
        """
        client = self._connect()
        try:
            return int(client.ttl(key))
        except Exception as exc:
            raise VerifyError(f"查 Redis TTL 失败: {key} -> {exc}") from exc

    def keys(self, pattern: str = "*", limit: int = 100) -> list[str]:
        """按模式查 key（调试用；生产库别用 ``*``，会扫全库）。"""
        client = self._connect()
        try:
            found = client.keys(pattern)
        except Exception as exc:
            raise VerifyError(f"查 Redis keys 失败: {pattern} -> {exc}") from exc
        return [str(k) for k in found[:limit]]

    # ---------------- 写入 ----------------

    def set(self, key: str, value: Any, ex: int | None = None) -> None:
        """写一个 key（测试数据准备 / 清理用）。"""
        client = self._connect()
        try:
            client.set(key, value, ex=ex)
        except Exception as exc:
            raise VerifyError(f"写 Redis 失败: {key} -> {exc}") from exc

    def delete(self, key: str) -> int:
        """删一个 key，返回删掉的数量。"""
        client = self._connect()
        try:
            return int(client.delete(key))
        except Exception as exc:
            raise VerifyError(f"删 Redis 失败: {key} -> {exc}") from exc

    # ---------------- 值比较辅助 ----------------

    @staticmethod
    def normalize(value: Any) -> Any:
        """把 Redis 里取到的值统一成可比较的东西。

        缓存里经常存 JSON 字符串，但断言里写的是字典。
        这里做一层转换：能解析成 JSON 就转，转不了就原样返回。
        """
        if isinstance(value, bytes):
            try:
                value = value.decode("utf-8")
            except UnicodeDecodeError:
                return value

        if isinstance(value, str):
            text = value.strip()
            if text and text[0] in "{[":
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return value
        return value


def _short(value: Any, limit: int = 200) -> str:
    """日志里别把整个 value 打出来（可能很大）。"""
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + "..."
