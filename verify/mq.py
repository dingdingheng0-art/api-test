"""消息队列校验。

**后端可插拔** —— 被测系统用哪种 MQ 不知道，所以做成适配层：

- ``rabbitmq`` —— 用 ``pika``
- ``kafka``  —— 用 ``kafka-python``
- ``memory`` —— 纯内存模拟（不连任何东西，跑单测和自检用）

校验流程（框架已经帮你排好顺序）：

1. **发请求之前**：订阅 / 开始消费（先把监听架起来，别漏消息）
2. **调被测接口**
3. **发请求之后**：轮询等消息（带超时）
4. **拿到消息后**：按 routing_key + 内容做断言

所以用例里写起来是这样::

    steps:
      - id: create_order
        path: /order/create
        method: POST
        data: {...}

      - id: check_mq
        operation_type: mq_check
        queue: order.created
        payload_contains: {orderNo: "{{create_order.result.orderNo}}"}
        timeout: 10
        assert:
          - type: mq_received
            expected: true

或者更简单 —— 直接在步骤里写 ``mq_received`` 断言，
框架会自动先订阅再断言。

配置（``config.yaml``）::

    mq:
      type: rabbitmq
      host: 127.0.0.1
      port: 5672
      user: guest
      password: "${MQ_PASSWORD}"
      vhost: "/"
      queue: order.created        # 默认消费的队列
"""

from __future__ import annotations

import json
import queue as _queue
import threading
import time
from typing import Any, Protocol

from utils.logs.log_manager import log
from verify.errors import DependencyMissing, VerifyConfigError, VerifyError


class Message(dict):
    """一条消息。

    就是个 dict，但多了几个便捷属性，读起来清楚点::

        msg["routing_key"]
        msg["payload"]
        msg["received_at"]
    """

    @property
    def routing_key(self) -> str:
        return str(self.get("routing_key") or "")

    @property
    def payload(self) -> Any:
        return self.get("payload")

    @property
    def body(self) -> str:
        """消息体的原始字符串。"""
        raw = self.get("raw")
        if raw is None:
            raw = self.get("payload")
        if isinstance(raw, (dict, list)):
            return json.dumps(raw, ensure_ascii=False)
        return str(raw or "")


class MQBackend(Protocol):
    """MQ 后端要实现的接口。"""

    def connect(self) -> None:
        """建立连接。"""

    def subscribe(self, queue: str) -> None:
        """开始监听某个队列（之后收到的消息进本地队列）。"""

    def poll(self, timeout: float) -> list[Message]:
        """等一小段时间，返回这段时间里收到的消息。"""

    def close(self) -> None:
        """关掉连接。"""


class BaseBackend:
    """后端基类：公共逻辑（连接状态、本地消息队列、超时控制）。"""

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = dict(config or {})
        self._messages: _queue.Queue[Message] = _queue.Queue()
        self._connected = False

    # ---- 子类实现 ----

    def _do_connect(self) -> None:
        raise NotImplementedError

    def _do_subscribe(self, queue: str) -> None:
        raise NotImplementedError

    def _do_close(self) -> None:
        pass

    # ---- 公共逻辑 ----

    def connect(self) -> None:
        if self._connected:
            return
        self._do_connect()
        self._connected = True

    def subscribe(self, queue: str) -> None:
        self.connect()
        self._do_subscribe(queue)

    def poll(self, timeout: float) -> list[Message]:
        """等 ``timeout`` 秒，把这段时间内收到的消息全拿出来。"""
        collected: list[Message] = []
        deadline = time.perf_counter() + max(0.0, timeout)
        while True:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                break
            try:
                message = self._messages.get(timeout=min(remaining, 0.2))
            except _queue.Empty:
                continue
            collected.append(message)
        return collected

    def close(self) -> None:
        if not self._connected:
            return
        try:
            self._do_close()
        finally:
            self._connected = False

    def _put(self, message: Message) -> None:
        """子类收到消息时调用。"""
        self._messages.put(message)


class MemoryBackend(BaseBackend):
    """内存模拟后端 —— 不连任何 MQ。

    用来做单测、框架自检，或者在没有 MQ 的环境里验证流程通不通。
    外部可以通过 :meth:`inject` 往里塞消息。
    """

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(config)
        self.subscribed: list[str] = []

    def _do_connect(self) -> None:
        return None

    def _do_subscribe(self, queue: str) -> None:
        self.subscribed.append(queue)

    def inject(self, payload: Any, routing_key: str = "") -> None:
        """塞一条消息进去（模拟 MQ 投递）。"""
        self._put(
            Message(
                payload=payload,
                raw=payload,
                routing_key=routing_key,
                received_at=time.time(),
                backend="memory",
            )
        )


class RabbitMQBackend(BaseBackend):
    """RabbitMQ 后端（依赖 ``pika``）。"""

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(config)
        self._connection: Any = None
        self._channel: Any = None
        self._consumer_tag: str | None = None
        self._stop = threading.Event()

    def _do_connect(self) -> None:
        try:
            import pika
        except ImportError as exc:
            raise DependencyMissing(
                "RabbitMQ 校验需要 pika 包。装一下：uv pip install pika"
            ) from exc

        credentials = pika.PlainCredentials(
            self.config.get("user") or "guest",
            self.config.get("password") or "guest",
        )
        params: dict[str, Any] = {
            "credentials": credentials,
            "virtual_host": self.config.get("vhost") or "/",
            "connection_attempts": 3,
            "retry_delay": 2,
        }
        host = self.config.get("host")
        if host:
            params["host"] = host
            params["port"] = int(self.config.get("port") or 5672)

        try:
            self._connection = pika.BlockingConnection(pika.ConnectionParameters(**params))
            self._channel = self._connection.channel()
            self._channel.basic_qos(prefetch_count=10)
        except Exception as exc:
            raise VerifyConfigError(f"连 RabbitMQ 失败: {exc}") from exc

        log.info("RabbitMQ 已连接: %s", self.config.get("host"))

    def _do_subscribe(self, queue: str) -> None:
        import pika

        self._stop.clear()

        def on_message(channel: Any, method: Any, properties: Any, body: bytes) -> None:
            raw = body.decode("utf-8", "replace")
            payload: Any = raw
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                pass

            self._put(
                Message(
                    payload=payload,
                    raw=raw,
                    routing_key=getattr(method, "routing_key", "") or "",
                    received_at=time.time(),
                    backend="rabbitmq",
                )
            )
            try:
                channel.basic_ack(delivery_tag=method.delivery_tag)
            except Exception:  # noqa: BLE001
                pass

        try:
            self._consumer_tag = self._channel.basic_consume(
                queue=queue, on_message_callback=on_message, auto_ack=False
            )
            # 起个后台线程跑消费循环
            thread = threading.Thread(target=self._consume_loop, daemon=True)
            thread.start()
        except Exception as exc:
            raise VerifyConfigError(f"订阅 RabbitMQ 队列 {queue!r} 失败: {exc}") from exc

    def _consume_loop(self) -> None:
        try:
            self._channel.start_consuming()
        except Exception:  # noqa: BLE001 - 消费线程退出不影响主流程
            pass

    def _do_close(self) -> None:
        self._stop.set()
        try:
            if self._channel is not None:
                self._channel.stop_consuming()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._connection is not None:
                self._connection.close()
        except Exception:  # noqa: BLE001
            pass


class KafkaBackend(BaseBackend):
    """Kafka 后端（依赖 ``kafka-python``）。"""

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(config)
        self._consumer: Any = None
        self._topic: str = ""

    def _do_connect(self) -> None:
        try:
            from kafka import KafkaConsumer
        except ImportError as exc:
            raise DependencyMissing(
                "Kafka 校验需要 kafka-python 包。装一下：uv pip install kafka-python"
            ) from exc

        bootstrap = self.config.get("bootstrap_servers") or self.config.get("host")
        if not bootstrap:
            raise VerifyConfigError("Kafka 配置里没有 bootstrap_servers")

        if isinstance(bootstrap, str):
            bootstrap = [s.strip() for s in bootstrap.split(",") if s.strip()]

        try:
            self._consumer = KafkaConsumer(
                bootstrap_servers=bootstrap,
                value_deserializer=lambda b: b.decode("utf-8", "replace"),
                auto_offset_reset="latest",
                consumer_timeout_ms=1000,
                **({"sasl_plain_username": self.config["user"],
                    "sasl_plain_password": self.config["password"]}
                   if self.config.get("user") and self.config.get("password") else {}),
            )
        except Exception as exc:
            raise VerifyConfigError(f"连 Kafka 失败: {exc}") from exc

        log.info("Kafka 已连接: %s", bootstrap)

    def _do_subscribe(self, queue: str) -> None:
        self._topic = queue
        try:
            self._consumer.subscribe([queue])
        except Exception as exc:
            raise VerifyConfigError(f"订阅 Kafka topic {queue!r} 失败: {exc}") from exc

    def poll(self, timeout: float) -> list[Message]:
        """Kafka 用的是阻塞拉取，单独实现。"""
        if self._consumer is None:
            return []

        collected: list[Message] = []
        deadline = time.perf_counter() + max(0.0, timeout)
        while time.perf_counter() < deadline:
            try:
                batch = self._consumer.poll(timeout_ms=500, max_records=50)
            except Exception as exc:  # noqa: BLE001
                log.warning("读 Kafka 失败: %s", exc)
                break

            if not batch:
                continue

            for _tp, records in batch.items():
                for record in records:
                    raw = record.value
                    payload: Any = raw
                    try:
                        payload = json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        pass
                    collected.append(
                        Message(
                            payload=payload,
                            raw=raw,
                            routing_key=getattr(record, "topic", self._topic) or "",
                            received_at=time.time(),
                            backend="kafka",
                        )
                    )
        return collected

    def _do_close(self) -> None:
        try:
            if self._consumer is not None:
                self._consumer.close()
        except Exception:  # noqa: BLE001
            pass


# 后端注册表 —— 加新 MQ 类型时在这里登记
_BACKENDS: dict[str, type[BaseBackend]] = {
    "memory": MemoryBackend,
    "rabbitmq": RabbitMQBackend,
    "kafka": KafkaBackend,
}


def build_backend(config: dict[str, Any] | None = None) -> BaseBackend:
    """按配置造一个后端。

    配置里 ``type`` 决定用哪种；没写就按有没有 ``bootstrap_servers`` 猜 Kafka，
    再猜 RabbitMQ，最后退回内存模拟。
    """
    cfg = dict(config or {})
    backend_type = str(cfg.get("type") or "").lower()

    if not backend_type:
        if cfg.get("bootstrap_servers"):
            backend_type = "kafka"
        elif cfg.get("host"):
            backend_type = "rabbitmq"
        else:
            backend_type = "memory"

    backend_cls = _BACKENDS.get(backend_type)
    if backend_cls is None:
        raise VerifyConfigError(
            f"不支持的 MQ 类型 {backend_type!r}（支持 {', '.join(_BACKENDS)}）"
        )

    return backend_cls(cfg)


def matches(
    message: Message,
    routing_key: str = "",
    payload_contains: dict[str, Any] | None = None,
) -> bool:
    """判断一条消息是不是我们要的那条。

    Args:
        message: 收到的消息
        routing_key: 期望的 routing key（空则不校验这一项）
        payload_contains: 期望消息体里包含的字段（空则不校验）
    """
    if routing_key:
        if message.routing_key != routing_key:
            return False

    if payload_contains:
        payload = message.payload
        if not isinstance(payload, dict):
            return False
        for key, expected in payload_contains.items():
            if key not in payload:
                return False
            actual = payload[key]
            # 数字比较时做类型无关比较（"123" vs 123 算匹配）
            if actual != expected:
                try:
                    if str(actual) != str(expected):
                        return False
                except Exception:  # noqa: BLE001
                    return False

    return True
