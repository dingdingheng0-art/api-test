"""全局变量容器（线程安全）。

框架里各模块之间共享的数据都放这里：
- 配置信息（每个 project-环境一份）
- 登录后的 token
- 用例执行过程中产生的中间变量

用法::

    from utils.infra.globals import Globals
    Globals.set("my_shop", {"host": ..., "token": ...})
    Globals.get("my_shop")
    Globals.get_data()   # 拿全量拷贝
"""
from __future__ import annotations

import threading
from collections.abc import Iterator, MutableMapping
from typing import Any


class Globals:
    """线程安全的全局变量管理器（单例语义）。"""

    _instance: "Globals | None" = None
    _lock = threading.Lock()
    _data: dict[str, Any] = {}

    def __new__(cls) -> "Globals":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    @classmethod
    def live_view(cls) -> "GlobalsView":
        """拿到一份**实时视图**（读写都落到 Globals 本身）。

        和 :meth:`get_data` 的区别：``get_data`` 是浅拷贝，拷完之后**新出现
        的顶层 key**（跑到一半才加载的项目）它看不见 —— 内层 dict 是共享的，
        所以往已有项目里写 token 反倒看得见。这种"半新半旧"最容易误导人。

        ``live_view`` 每次都回读 Globals，适合"要一直跟着全局变量走"的场景
        （比如 session 级的 pytest fixture）。返回对象是 ``MutableMapping``，
        用法跟普通 dict 一样。
        """
        return GlobalsView()

    @classmethod
    def get_data(cls) -> dict[str, Any]:
        """拿到全量数据的一份浅拷贝（快照 —— 之后的变化看不到）。"""
        with cls._lock:
            return dict(cls._data)

    @classmethod
    def set(cls, key: str, value: Any) -> None:
        with cls._lock:
            cls._data[key] = value

    @classmethod
    def get(cls, key: str, default: Any = None) -> Any:
        with cls._lock:
            return cls._data.get(key, default)

    @classmethod
    def update(cls, key: str, sub_key: str, sub_value: Any) -> None:
        """更新某个 project 下的一个字段，例如 ``Globals.update("my_shop", "token", "xxx")``。"""
        with cls._lock:
            node = cls._data.get(key)
            if isinstance(node, dict):
                node[sub_key] = sub_value
            else:
                raise KeyError(f"Globals 里没有 key={key!r}，或它不是字典，无法 update")

    @classmethod
    def clear(cls) -> None:
        with cls._lock:
            cls._data.clear()


class GlobalsView(MutableMapping[str, Any]):
    """Globals 的实时视图 —— 每次读都回读 Globals，不是拷贝。

    只做一层薄包装，不自己存数据。这样 ``view["mall4j"]["token"]`` 在
    任何时刻读到的都是当前值，不会像 ``get_data()`` 那样停在快照那一刻。
    """

    def __getitem__(self, key: str) -> Any:
        with Globals._lock:
            return Globals._data[key]

    def __setitem__(self, key: str, value: Any) -> None:
        Globals.set(key, value)

    def __delitem__(self, key: str) -> None:
        with Globals._lock:
            del Globals._data[key]

    def __iter__(self) -> Iterator[str]:
        with Globals._lock:
            return iter(list(Globals._data))

    def __len__(self) -> int:
        with Globals._lock:
            return len(Globals._data)

    def __repr__(self) -> str:
        return f"GlobalsView({Globals.get_data()!r})"
