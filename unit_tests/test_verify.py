"""校验层（数据库 / 缓存 / 消息队列）的单元测试。

**不连真实环境** —— 数据库用 sqlite（Python 自带），
Redis / MQ 用假的（内存模拟）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.assertions.assert_handler import AssertHandler
from verify.db import DatabaseClient
from verify.manager import VerifyManager
from verify.mq import MemoryBackend, matches


# ---------------------------------------------------------------------------
# 数据库
# ---------------------------------------------------------------------------


@pytest.fixture()
def db() -> DatabaseClient:
    """一个内存 sqlite 库，带一张测试表。"""
    client = DatabaseClient({"type": "sqlite", "database": ":memory:"})
    client.execute(
        "CREATE TABLE orders ("
        "  id INTEGER PRIMARY KEY,"
        "  order_no TEXT,"
        "  status INTEGER,"
        "  amount REAL"
        ")"
    )
    client.execute(
        "INSERT INTO orders (id, order_no, status, amount) VALUES (1, 'SO001', 2, 99.5)"
    )
    client.execute(
        "INSERT INTO orders (id, order_no, status, amount) VALUES (2, 'SO002', 1, 10.0)"
    )
    yield client
    client.close()


class TestDatabaseClient:
    def test_query_returns_dicts(self, db: DatabaseClient) -> None:
        rows = db.query("SELECT * FROM orders ORDER BY id")
        assert len(rows) == 2
        assert rows[0]["order_no"] == "SO001"
        assert rows[0]["status"] == 2

    def test_query_one(self, db: DatabaseClient) -> None:
        row = db.query_one("SELECT * FROM orders WHERE id = ?", (1,))
        assert row is not None
        assert row["order_no"] == "SO001"

    def test_query_one_missing_returns_none(self, db: DatabaseClient) -> None:
        assert db.query_one("SELECT * FROM orders WHERE id = ?", (999,)) is None

    def test_scalar(self, db: DatabaseClient) -> None:
        assert db.scalar("SELECT COUNT(*) FROM orders") == 2

    def test_scalar_by_column_name(self, db: DatabaseClient) -> None:
        value = db.scalar(
            "SELECT order_no, status FROM orders WHERE id = ?", (1,), column="status"
        )
        assert value == 2

    def test_execute_returns_affected_rows(self, db: DatabaseClient) -> None:
        affected = db.execute("UPDATE orders SET status = 9 WHERE id = ?", (1,))
        assert affected == 1

    def test_cleanup_ignores_errors(self, db: DatabaseClient) -> None:
        """清理失败不该抛异常（清理是收尾动作）。"""
        assert db.cleanup("DELETE FROM 不存在的表 WHERE id = 1") == 0

    # ---- 占位符：用例统一写 %s，sqlite 要自动换成 ? ----

    def test_mysql_placeholder_works_on_sqlite(self, db: DatabaseClient) -> None:
        row = db.query_one("SELECT * FROM orders WHERE id = %s", (1,))
        assert row is not None
        assert row["order_no"] == "SO001"

    def test_mysql_placeholder_in_execute(self, db: DatabaseClient) -> None:
        assert db.execute("UPDATE orders SET status = %s WHERE id = %s", (7, 2)) == 1
        assert db.scalar("SELECT status FROM orders WHERE id = %s", (2,)) == 7

    def test_placeholder_inside_quotes_is_left_alone(self) -> None:
        """``LIKE '%s%'`` 里的 %s 是数据，不能被换成 ?。"""
        from verify.db import _sqlite_placeholders

        converted = _sqlite_placeholders(
            "SELECT * FROM t WHERE a LIKE '%s%' AND b = %s"
        )
        assert converted == "SELECT * FROM t WHERE a LIKE '%s%' AND b = ?"

    def test_escaped_percent_survives(self, db: DatabaseClient) -> None:
        assert db.scalar("SELECT COUNT(*) FROM orders WHERE order_no LIKE '%%'") == 2

    def test_double_quoted_identifier_not_touched(self, db: DatabaseClient) -> None:
        assert db.scalar('SELECT COUNT(*) FROM "orders" WHERE id = %s', (1,)) == 1

    def test_mysql_port_from_env_is_coerced(self, monkeypatch) -> None:
        """配置里 ``${MALL4J_DB_PORT:3306}`` 展开是字符串，驱动要 int。"""
        import mysql.connector

        captured: dict = {}

        def fake_connect(**kwargs):
            captured.update(kwargs)
            return object()

        monkeypatch.setattr(mysql.connector, "connect", fake_connect)
        client = DatabaseClient(
            {
                "type": "mysql",
                "host": "h",
                "user": "u",
                "password": "p",
                "database": "d",
                "port": "3306",
            }
        )
        client._connect()
        assert captured["port"] == 3306

    def test_mysql_missing_connection_info_raises_clear_error(self) -> None:
        """没填 .env 时，报错要指向缺哪个字段，而不是连 127.0.0.1 超时。"""
        from verify.errors import VerifyConfigError

        client = DatabaseClient(
            {"type": "mysql", "host": "", "user": "", "password": "", "database": ""}
        )
        with pytest.raises(VerifyConfigError) as excinfo:
            client._connect()
        message = str(excinfo.value)
        assert "host" in message and "MALL4J_DB_HOST" in message

    def test_mysql_bad_port_raises_config_error(self, monkeypatch) -> None:
        from verify.errors import VerifyConfigError

        import mysql.connector

        monkeypatch.setattr(mysql.connector, "connect", lambda **_: object())
        client = DatabaseClient(
            {"type": "mysql", "host": "h", "user": "u", "database": "d", "port": "abc"}
        )
        with pytest.raises(VerifyConfigError):
            client._connect()

    def test_mysql_connection_enables_autocommit(self, monkeypatch) -> None:
        """必须开 autocommit —— 否则会读到过期快照。

        mysql-connector 默认 ``autocommit=False``，加上 MySQL 默认的
        REPEATABLE-READ，连接不提交就一直停在同一个事务里，读视图在第一次
        SELECT 时钉死。被测系统之后新写的数据（用例刚造的）这条连接看不见，
        查库断言会假失败。

        实测：A 连接读到 112，B 连接插入并提交，A 再读还是 112，
        新开连接读到 113。
        """
        import mysql.connector

        captured: dict = {}

        def fake_connect(**kwargs):
            captured.update(kwargs)
            return object()

        monkeypatch.setattr(mysql.connector, "connect", fake_connect)
        client = DatabaseClient(
            {"type": "mysql", "host": "h", "user": "u", "database": "d"}
        )
        client._connect()

        assert captured.get("autocommit") is True

    def test_mysql_autocommit_can_be_overridden(self, monkeypatch) -> None:
        """个别场景要事务语义时，配置里显式写 autocommit: false 要能生效。"""
        import mysql.connector

        captured: dict = {}

        def fake_connect(**kwargs):
            captured.update(kwargs)
            return object()

        monkeypatch.setattr(mysql.connector, "connect", fake_connect)
        client = DatabaseClient(
            {
                "type": "mysql",
                "host": "h",
                "user": "u",
                "database": "d",
                "autocommit": False,
            }
        )
        client._connect()

        assert captured.get("autocommit") is False


class TestDbAssert:
    """用 sqlite 配好校验层，跑 db_* 断言。"""

    @pytest.fixture(autouse=True)
    def _setup(self) -> None:
        manager = VerifyManager.get()
        manager.reset()
        manager.setup(
            {
                "db": {
                    "default": {
                        "type": "sqlite",
                        "database": ":memory:",
                    }
                }
            }
        )
        # 建表 + 塞数据（同一个连接）
        client = manager.db()
        client.execute(
            "CREATE TABLE IF NOT EXISTS orders ("
            "  id INTEGER PRIMARY KEY, status INTEGER, order_no TEXT)"
        )
        client.execute("DELETE FROM orders")
        client.execute(
            "INSERT INTO orders (id, status, order_no) VALUES (1, 2, 'SO001')"
        )
        yield
        manager.reset()

    def test_db_query_equal(self) -> None:
        handler = AssertHandler()
        handler.handle_assertion(
            [{"type": "db_query", "query": "SELECT status FROM orders WHERE id = 1",
              "expected": 2}],
            response={},
        )

    def test_db_query_mismatch_raises(self) -> None:
        handler = AssertHandler()
        with pytest.raises(AssertionError):
            handler.handle_assertion(
                [{"type": "db_query", "query": "SELECT status FROM orders WHERE id = 1",
                  "expected": 999}],
                response={},
            )

    def test_db_query_exists(self) -> None:
        handler = AssertHandler()
        handler.handle_assertion(
            [{"type": "db_query_exists",
              "query": "SELECT id FROM orders WHERE id = 1"}],
            response={},
        )

    def test_db_query_count(self) -> None:
        handler = AssertHandler()
        handler.handle_assertion(
            [{"type": "db_query_count", "query": "SELECT id FROM orders", "expected": 1}],
            response={},
        )

    def test_evidence_is_collected(self) -> None:
        """校验证据要被收集起来（贴进 allure 报告用）。"""
        handler = AssertHandler()
        handler.handle_assertion(
            [{"type": "db_query", "query": "SELECT status FROM orders WHERE id = 1",
              "expected": 2}],
            response={},
        )
        assert handler.evidence
        assert handler.evidence[0]["kind"] == "db"
        assert handler.evidence[0]["actual"] == 2


# ---------------------------------------------------------------------------
# 缓存
# ---------------------------------------------------------------------------


class FakeRedis:
    """假装是 redis 客户端（够用来测断言逻辑）。"""

    def __init__(self, data: dict[str, str] | None = None, ttls: dict[str, int] | None = None):
        self.data = data or {}
        self.ttls = ttls or {}

    def get(self, key: str) -> str | None:
        return self.data.get(key)

    def exists(self, key: str) -> bool:
        return key in self.data

    def ttl(self, key: str) -> int:
        return self.ttls.get(key, -1)

    def delete(self, key: str) -> int:
        return 1 if self.data.pop(key, None) is not None else 0


class FakeRedisClient:
    """包装 FakeRedis，模拟 verify.redis.RedisClient 的接口。"""

    def __init__(self, fake: FakeRedis) -> None:
        self.fake = fake

    def get(self, key: str) -> str | None:
        return self.fake.get(key)

    def exists(self, key: str) -> bool:
        return self.fake.exists(key)

    def ttl(self, key: str) -> int:
        return self.fake.ttl(key)

    def delete(self, key: str) -> int:
        return self.fake.delete(key)

    @staticmethod
    def normalize(value: object) -> object:
        from verify.redis import RedisClient

        return RedisClient.normalize(value)


class TestRedisAssert:
    @pytest.fixture()
    def fake(self) -> FakeRedis:
        return FakeRedis(
            data={"order:1": '{"status":2}', "order:2": "plain"},
            ttls={"order:1": 1800},
        )

    @pytest.fixture(autouse=True)
    def _setup(self, fake: FakeRedis) -> None:
        manager = VerifyManager.get()
        manager.reset()
        manager._redis_clients["default"] = FakeRedisClient(fake)
        yield
        manager.reset()

    def test_redis_get_json_string(self) -> None:
        handler = AssertHandler()
        handler.handle_assertion(
            [{"type": "redis_get", "key": "order:1", "expected": {"status": 2}}],
            response={},
        )

    def test_redis_get_plain_string(self) -> None:
        handler = AssertHandler()
        handler.handle_assertion(
            [{"type": "redis_get", "key": "order:2", "expected": "plain"}],
            response={},
        )

    def test_redis_get_mismatch_raises(self) -> None:
        handler = AssertHandler()
        with pytest.raises(AssertionError):
            handler.handle_assertion(
                [{"type": "redis_get", "key": "order:2", "expected": "other"}],
                response={},
            )

    def test_redis_exists(self) -> None:
        handler = AssertHandler()
        handler.handle_assertion(
            [{"type": "redis_exists", "key": "order:1", "expected": True}],
            response={},
        )

    def test_redis_not_exists(self) -> None:
        handler = AssertHandler()
        handler.handle_assertion(
            [{"type": "redis_exists", "key": "nonexistent", "expected": False}],
            response={},
        )

    def test_redis_deleted(self) -> None:
        handler = AssertHandler()
        handler.handle_assertion(
            [{"type": "redis_deleted", "key": "nonexistent", "expected": True}],
            response={},
        )

    def test_redis_ttl_lte(self) -> None:
        handler = AssertHandler()
        handler.handle_assertion(
            [{"type": "redis_ttl", "key": "order:1", "expected": {"lte": 3600}}],
            response={},
        )

    def test_redis_ttl_gte_fails(self) -> None:
        handler = AssertHandler()
        with pytest.raises(AssertionError):
            handler.handle_assertion(
                [{"type": "redis_ttl", "key": "order:1", "expected": {"gte": 3600}}],
                response={},
            )


# ---------------------------------------------------------------------------
# 消息队列
# ---------------------------------------------------------------------------


class TestMQ:
    def test_memory_backend_inject_and_poll(self) -> None:
        backend = MemoryBackend()
        backend.subscribe("order.created")
        backend.inject({"orderNo": "SO001"}, routing_key="order.created")
        messages = backend.poll(0.2)
        assert len(messages) == 1
        assert messages[0].payload["orderNo"] == "SO001"

    def test_matches_by_routing_key(self) -> None:
        from verify.mq import Message

        message = Message(payload={"a": 1}, routing_key="order.created")
        assert matches(message, routing_key="order.created")
        assert not matches(message, routing_key="other.key")

    def test_matches_by_payload(self) -> None:
        from verify.mq import Message

        message = Message(payload={"orderNo": "SO001", "status": 2})
        assert matches(message, payload_contains={"orderNo": "SO001"})
        assert not matches(message, payload_contains={"orderNo": "SO999"})
        assert not matches(message, payload_contains={"missing": 1})

    def test_matches_number_as_string(self) -> None:
        """数字比较时做类型无关比较（"123" vs 123）。"""
        from verify.mq import Message

        message = Message(payload={"amount": 123})
        assert matches(message, payload_contains={"amount": "123"})

    def test_mq_assert_received(self) -> None:
        manager = VerifyManager.get()
        manager.reset()
        backend = MemoryBackend()
        manager._mq_backends["default"] = backend

        backend.inject({"orderNo": "SO001"}, routing_key="order.created")

        handler = AssertHandler()
        handler.handle_assertion(
            [{
                "type": "mq_received",
                "queue": "order.created",
                "routing_key": "order.created",
                "payload_contains": {"orderNo": "SO001"},
                "timeout": 0.2,
                "expected": True,
            }],
            response={},
        )
        manager.reset()

    def test_mq_assert_not_received_raises(self) -> None:
        manager = VerifyManager.get()
        manager.reset()
        manager._mq_backends["default"] = MemoryBackend()

        handler = AssertHandler()
        with pytest.raises(AssertionError):
            handler.handle_assertion(
                [{
                    "type": "mq_received",
                    "queue": "order.created",
                    "timeout": 0.1,
                    "expected": True,
                }],
                response={},
            )
        manager.reset()


# ---------------------------------------------------------------------------
# 配置错误处理
# ---------------------------------------------------------------------------


class TestConfigErrors:
    def test_redis_not_configured_gives_clear_message(self) -> None:
        manager = VerifyManager.get()
        manager.reset()
        manager.setup({})

        handler = AssertHandler()
        with pytest.raises(Exception) as excinfo:
            handler.handle_assertion(
                [{"type": "redis_get", "key": "x", "expected": "1"}],
                response={},
            )
        message = str(excinfo.value)
        assert "redis" in message.lower()
        assert "配置" in message
        manager.reset()

    def test_db_not_configured_gives_clear_message(self) -> None:
        manager = VerifyManager.get()
        manager.reset()
        manager.setup({})

        handler = AssertHandler()
        with pytest.raises(Exception) as excinfo:
            handler.handle_assertion(
                [{"type": "db_query", "query": "SELECT 1", "expected": 1}],
                response={},
            )
        assert "db" in str(excinfo.value).lower()
        manager.reset()
