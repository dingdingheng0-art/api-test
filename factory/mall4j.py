"""mall4j 后台的测试数据工厂。

**这个文件是新系统接入的模板** —— 接入你自己的系统时照着抄一份，
改掉里面的逻辑。

**怎么接自己的系统**：

1. 复制这个文件成 ``factory/<你的项目名>.py``
2. 继承 ``BaseProjectFactory``（或者直接继承 ``DataFactory``）
3. 实现 ``create()``（造数据）和可选的 ``cleanup()``（清数据）
4. 在 ``configs/factories.yaml`` 里注册::

       你的项目名:
         你的工厂名:
           module: factory.你的项目名
           class: 你的工厂类

5. 用例里就能用了::

       setups:
         - id: order
           operation_type: factory
           factory: 你的项目名.你的工厂名
           params: {count: 1}

**造出来的数据一定要登记**（调一次 ``self.track(...)``）——
框架会在 session 结束时统一清掉。
忘了登记 = 测试数据留在库里，越跑越多。

⚠️ 这里的接口路径和表名是**按 mall4j 写的**，
   接自己的系统时记得改成对应的（找不到就照着浏览器抓包的路径填）。
"""

from __future__ import annotations

import random
import string
import time
from typing import Any

from factory.base import DataFactory, FactoryError
from utils.logs.log_manager import log


class BaseProjectFactory(DataFactory):
    """项目工厂的公共基类。

    子类继承它，就自带"业务接口客户端"和"造随机数据"这两样东西。
    """

    def __init__(self, **options: Any) -> None:
        super().__init__(**options)
        self._api: Any = None

    @property
    def api(self) -> Any:
        """业务接口客户端（自动带上登录态）。

        没登录过就现登一次。多个工厂共用一个客户端 —— 登录很贵。
        """
        if self._api is None:
            from utils.http.api_client import ApiClient
            from utils.infra.globals import Globals
            from utils.config.project_profile import ProjectRegistry

            project = self.project or ProjectRegistry.default_project()
            profile = ProjectRegistry.get(project)

            config = Globals.get(project) or {}
            token = config.get(profile.token_var if profile else "token")
            session = config.get(profile.session_var if profile else "uuid")

            if token and session:
                self._api = ApiClient(
                    access_token=token, uuid=session, profile=profile
                )
            else:
                # 没登录态就先登录一次
                self._api = ApiClient.from_config(fresh=False, profile=profile)
        return self._api

    # ---------------- 造随机数据 ----------------

    @staticmethod
    def gen_no(prefix: str = "T") -> str:
        """生成一个唯一的单号。"""
        rand = "".join(random.choices(string.digits, k=8))
        return f"{prefix}{int(time.time() * 1000)}{rand}"

    @staticmethod
    def gen_mobile() -> str:
        """生成一个手机号。"""
        return "1" + random.choice("35789") + "".join(random.choices(string.digits, k=9))


class ProductFactory(BaseProjectFactory):
    """商品工厂 —— 造一条商品数据。

    **怎么改**：把 ``cleanup_sql`` 换成你自己系统的表名，
    ``create()`` 里换成你自己的入库逻辑（直连数据库或调业务接口）。
    """

    name = "product"

    #: 清理用的 SQL（``%s`` 是 id 的占位符）
    cleanup_sql = [
        "DELETE FROM pms_product WHERE id = %s",
    ]

    def create(self, count: int = 1, **params: Any) -> list[dict[str, Any]]:
        """造 ``count`` 条商品数据。

        Returns:
            每条数据的字典（带 ``id``，清理时要用）
        """
        from verify.db import DatabaseClient

        db = DatabaseClient(self.options.get("db_config"))
        created: list[dict[str, Any]] = []

        for _ in range(count):
            row = {
                "name": params.get("name") or f"测试商品_{self.gen_no('P')}",
                "code": params.get("code") or self.gen_no("C"),
                "price": params.get("price", 10),
                "status": params.get("status", 1),
            }
            columns = ", ".join(row.keys())
            placeholders = ", ".join(["%s"] * len(row))
            db.execute(
                f"INSERT INTO pms_product ({columns}) VALUES ({placeholders})",
                tuple(row.values()),
            )
            created.append(row)

        # ⚠️ 登记 —— 不登记的话 session 结束不会清，测试数据会留在库里
        self.track(row.get("id") for row in created)

        log.info("商品工厂造了 %s 条数据", len(created))
        return created


class OrderFactory(BaseProjectFactory):
    """订单工厂 —— 造一条业务订单。

    **怎么改**：把 ``create()`` 里的接口路径换成你自己的下单接口，
    把 ``cleanup()`` 换成你自己的删单接口。
    """

    name = "order"

    def create(self, count: int = 1, **params: Any) -> list[dict[str, Any]]:
        """造 ``count`` 条订单（走真实的下单接口）。"""
        created: list[dict[str, Any]] = []

        for _ in range(count):
            payload = {
                "orderNo": params.get("orderNo") or self.gen_no("SO"),
                "mobile": params.get("mobile") or self.gen_mobile(),
                "amount": params.get("amount", 100),
                "remark": params.get("remark", "自动化测试数据"),
            }
            # ⚠️ 换成你自己的下单接口（mall4j 的路径）
            response = self.api.post("/order/create", json_body=payload)
            result = (response or {}).get("result") or {}
            row = {**payload, **(result if isinstance(result, dict) else {})}
            created.append(row)

        # 登记 —— 清理时要用单号
        self.track(row.get("orderNo") for row in created)

        log.info("订单工厂造了 %s 条数据", len(created))
        return created

    def cleanup(self, keys: Any) -> int:
        """把造的订单删掉（调你的删单接口）。"""
        cleaned = 0
        for order_no in keys or []:
            try:
                # ⚠️ 换成你自己的删单接口（mall4j 的路径）
                self.api.post("/order/delete", json_body={"orderNo": order_no})
                cleaned += 1
            except Exception as exc:  # noqa: BLE001 - 清不干净不该影响用例判定
                log.warning("删订单 %s 失败: %s", order_no, exc)
        return cleaned


class RedisKeyFactory(BaseProjectFactory):
    """Redis 凭证工厂 —— 造缓存数据。

    用途：测"缓存什么时候失效"这类场景。
    """

    name = "redis_key"

    def create(self, count: int = 1, **params: Any) -> list[dict[str, Any]]:
        """往 Redis 写 ``count`` 个 key。"""
        from verify.manager import VerifyManager

        client = VerifyManager.get().redis(str(params.get("alias") or "default"))
        created: list[dict[str, Any]] = []

        for _ in range(count):
            key = params.get("key") or f"test:key:{self.gen_no('K')}"
            value = params.get("value", '{"mock":true}')
            ttl = params.get("ttl")
            client.set(key, value, ex=int(ttl) if ttl else None)
            created.append({"key": key, "value": value})

        self.track(row["key"] for row in created)
        log.info("Redis 工厂写了 %s 个 key", len(created))
        return created

    def cleanup(self, keys: Any) -> int:
        """把写的 key 删掉。"""
        from verify.manager import VerifyManager

        alias = str(self.options.get("alias") or "default")
        try:
            client = VerifyManager.get().redis(alias)
        except Exception as exc:  # noqa: BLE001
            log.warning("拿 Redis 失败，跳过清理: %s", exc)
            return 0

        cleaned = 0
        for key in keys or []:
            try:
                cleaned += client.delete(key)
            except Exception as exc:  # noqa: BLE001
                log.warning("删 Redis key %s 失败: %s", key, exc)
        return cleaned


__all__ = [
    "BaseProjectFactory",
    "ProductFactory",
    "OrderFactory",
    "RedisKeyFactory",
    "FactoryError",
]
