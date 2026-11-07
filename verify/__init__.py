"""校验层：数据库 / 缓存 / 消息队列三套"事后验数据"的能力。

这层和 ``utils/assert_handler.py`` 的关系：

- ``utils/assert_handler.py`` 负责**看懂断言**（断言类型怎么分发、怎么比较）
- ``verify/`` 负责**真的去查**（连数据库、连 Redis、消费消息）

为什么要分开：
断言处理是"翻译官"，校验执行是"跑腿的"，混在一起会让断言文件越写越大。
拆开之后，新增一种校验手段只要在 ``verify/`` 加一个文件。

**所有第三方依赖都是可选的** —— 没装 ``redis`` 包就只是不能用 redis 断言，
框架本身照跑不误（抛的是一句人话，不是 ImportError 栈）。

用法（一般不直接调，用例里写断言就行）::

    from verify.db import DatabaseClient

    db = DatabaseClient({"host": "127.0.0.1", "database": "shop"})
    rows = db.query("SELECT status FROM orders WHERE id = %s", ("123",))

读表结构（写校验 SQL 前先看看有哪些表、哪些字段）::

    from verify.schema import SchemaInspector

    inspector = SchemaInspector({"type": "mysql", "host": ..., "database": "shop"})
    schema = inspector.inspect()
    schema.find_columns("order_no")   # [(表, 字段), ...]
"""

from __future__ import annotations

from verify.errors import (
    DependencyMissing,
    VerifyConfigError,
    VerifyError,
)

__all__ = [
    "DependencyMissing",
    "VerifyConfigError",
    "VerifyError",
]
