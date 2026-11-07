"""工厂基类 + 注册表。

**工厂怎么写**：继承 ``DataFactory``，实现 ``create()`` 和 ``cleanup()``。

.. code-block:: python

    class OrderFactory(DataFactory):
        name = "order"

        def create(self, count=1, **kwargs):
            # 造数据（调接口 / 写库都行）
            rows = []
            for _ in range(count):
                resp = self.api.post("/order/create", json={...})
                rows.append(resp["result"])
            # 登记到清理栈 —— 用例挂了也能自动清掉
            self.track(row["orderNo"] for row in rows)
            return rows

        def cleanup(self, ids):
            for order_no in ids:
                self.api.post("/order/delete", json={"orderNo": order_no})

工厂名字用 ``模块.名字`` 的形式（``my_shop.order``），
这样不同模块的同名工厂不会撞车。

**注册方式**（两种）：

- 在 ``configs/factories.yaml`` 里写（推荐，测试同学能改）::

      my_shop:
        order:
          module: factory.my_shop
          class: OrderFactory

- 在代码里装饰器注册::

      @register
      class OrderFactory(DataFactory):
          ...
"""

from __future__ import annotations

from typing import Any, Callable, Iterable

from utils.logs.log_manager import log


class FactoryError(RuntimeError):
    """工厂相关的问题（名字找不到、参数不对、造数据失败）。"""


class DataFactory:
    """测试数据工厂基类。

    子类要覆盖的：

    - ``create()``   —— 造数据，返回造出来的东西（列表或字典）
    - ``cleanup()``  —— 清数据（可以只留默认实现，用 ``cleanup_sql``）
    """

    # 工厂名（不含模块前缀）；子类一般会覆盖
    name: str = ""

    # 清数据用的 SQL 列表（子类可以填，默认走 cleanup()）
    cleanup_sql: list[str] = []

    def __init__(self, **options: Any) -> None:
        self.options = dict(options)
        # 本次造出来、等着被清的"钥匙"（主键 / 业务号）
        self._tracked: list[Any] = []

    # ---------------- 子类实现 ----------------

    def create(self, count: int = 1, **params: Any) -> Any:
        """造数据。子类必须实现。"""
        raise NotImplementedError(f"工厂 {self.full_name} 没实现 create()")

    def cleanup(self, keys: Iterable[Any]) -> int:
        """清理数据。返回清理掉的数量。

        默认实现：按 ``cleanup_sql`` 逐条执行。
        """
        from verify.db import DatabaseClient

        sql_list = self.cleanup_sql
        if not sql_list:
            return 0

        db = DatabaseClient(self.options.get("db_config"))
        cleaned = 0
        for sql in sql_list:
            cleaned += db.cleanup(sql, tuple(keys) if keys else None)
        return cleaned

    # ---------------- 公共能力 ----------------

    @property
    def project(self) -> str:
        """这个工厂属于哪个项目。

        默认取工厂注册时用的模块名（``factories.yaml`` 里那一层的 key），
        所以 ``my_shop.order`` 属于 my_shop，``shop.order`` 属于 shop。

        也可以在 ``factories.yaml`` 的 ``options.project`` 里显式指定。
        """
        explicit = self.options.get("project")
        if explicit:
            return str(explicit)
        return str(self.options.get("module") or "")

    def track(self, keys: Iterable[Any]) -> None:
        """登记要清理的"钥匙"。

        造的每条数据都要调一下 —— session 结束时统一清。
        """
        for key in keys:
            if key is not None and key not in self._tracked:
                self._tracked.append(key)

    @property
    def tracked(self) -> list[Any]:
        """当前登记的、等着被清的钥匙。"""
        return list(self._tracked)

    def clear_tracked(self) -> None:
        """清空登记（已经清过了）。"""
        self._tracked.clear()

    @property
    def full_name(self) -> str:
        """工厂全名 —— ``模块.名字``。"""
        module = self.options.get("module") or type(self).__module__
        short = self.name or type(self).__name__
        return f"{module}.{short}"

    def __repr__(self) -> str:
        return f"<工厂 {self.full_name}>"


class FactoryRegistry:
    """工厂注册表 —— 名字 → 工厂实例。"""

    def __init__(self) -> None:
        self._factories: dict[str, DataFactory] = {}

    def register(self, factory: DataFactory, name: str = "") -> None:
        """注册一个工厂。"""
        key = name or factory.full_name
        self._factories[key] = factory
        log.info("已注册工厂: %s", key)

    def get(self, name: str) -> DataFactory:
        """按名字取工厂。"""
        factory = self._factories.get(name)
        if factory is None:
            raise FactoryError(
                f"没找到工厂 {name!r}。"
                f"已注册的：{', '.join(sorted(self._factories)) or '(空)'}"
            )
        return factory

    def has(self, name: str) -> bool:
        return name in self._factories

    def names(self) -> list[str]:
        return sorted(self._factories)

    def clear(self) -> None:
        self._factories.clear()


# 全局注册表
registry = FactoryRegistry()


def register(cls: type[DataFactory]) -> type[DataFactory]:
    """类装饰器：把工厂注册进全局表。

    .. code-block:: python

        @register
        class OrderFactory(DataFactory):
            name = "order"
    """
    factory = cls()
    registry.register(factory, factory.full_name)
    return cls


def load_factories(config_path: Any = None) -> int:
    """从 YAML 配置文件加载工厂。

    配置结构（``configs/factories.yaml``）::

        my_shop:
          order:
            module: factory.my_shop
            class: OrderFactory
            options:
              db_alias: default

    返回加载成功的工厂数量。
    """
    import importlib
    from pathlib import Path

    import yaml

    from utils.paths import CONFIGS_DIR

    path = Path(config_path) if config_path else CONFIGS_DIR / "factories.yaml"
    if not path.exists():
        log.info("工厂配置文件不存在，跳过加载: %s", path)
        return 0

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        log.error("工厂配置读取失败 %s: %s", path, exc)
        return 0

    if not isinstance(data, dict):
        return 0

    loaded = 0
    for module_name, factories in data.items():
        if not isinstance(factories, dict):
            continue
        for short_name, spec in factories.items():
            spec = spec or {}
            full_name = f"{module_name}.{short_name}"
            try:
                factory = _build_factory(module_name, short_name, spec)
            except Exception as exc:  # noqa: BLE001 - 一个工厂坏了不影响别的
                log.error("工厂 %s 加载失败: %s", full_name, exc)
                continue

            registry.register(factory, full_name)
            loaded += 1

    log.info("已加载 %s 个工厂", loaded)
    return loaded


def _build_factory(
    module_name: str,
    short_name: str,
    spec: dict[str, Any],
) -> DataFactory:
    """按配置造一个工厂实例。"""
    import importlib

    module_path = spec.get("module") or f"factory.{module_name}"
    class_name = spec.get("class")

    module = importlib.import_module(module_path)
    if not class_name:
        # 没写类名就按约定找：OrderFactory / orderFactory
        candidates = [
            f"{short_name.title().replace('_', '')}Factory",
            f"{short_name.title().replace('_', '')}",
            "Factory",
        ]
        for candidate in candidates:
            cls = getattr(module, candidate, None)
            if cls is not None:
                break
        else:
            raise FactoryError(
                f"模块 {module_path} 里找不到工厂类（试过 {candidates}）"
            )
    else:
        cls = getattr(module, class_name, None)
        if cls is None:
            raise FactoryError(f"模块 {module_path} 里没有类 {class_name}")

    options = dict(spec.get("options") or {})
    options.setdefault("module", module_name)

    factory = cls(**options)
    if not factory.name:
        factory.name = short_name
    return factory
