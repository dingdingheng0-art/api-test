"""测试数据工厂层。

**为什么需要**：造测试数据这事儿，每个用例各写各的，最后变成灾难 ——
数据忘了删、字段名写错、用例之间互相污染。

工厂层解决三件事：

1. **造得规范** —— 一个工厂只知道怎么造"一条订单"，不用关心别的
2. **自动清理** —— 工厂造出来的数据，登记到清理栈；
   session 结束（不管用例成功还是失败）统一兜底删掉
3. **可复用** —— 同一个工厂在多条用例里用，造法一致

用法（YAML 里）::

    setups:
      - id: order
        operation_type: factory
        factory: my_shop.order             # 工厂名（在 factories.yaml 里注册）
        params: {count: 1}

    teardowns:
      - id: clean
        operation_type: factory_cleanup
        factory: my_shop.order

代码里也可以直接用::

    from factory import get_factory

    order = get_factory("my_shop.order").create(count=1)
"""

from __future__ import annotations

from factory.base import DataFactory, FactoryError, FactoryRegistry
from factory.cleanup import CleanupStack, track_factory

__all__ = [
    "DataFactory",
    "FactoryError",
    "FactoryRegistry",
    "CleanupStack",
    "get_factory",
    "register_factory",
    "track_factory",
]


def get_factory(name: str) -> DataFactory:
    """按名字取一个工厂。

    名字是 ``模块.工厂名`` 的形式（``my_shop.order``）。
    """
    from factory.base import registry

    return registry.get(name)


def register_factory(factory: DataFactory, name: str = "") -> None:
    """注册一个工厂（代码里自定义工厂时用）。"""
    from factory.base import registry

    registry.register(factory, name)
