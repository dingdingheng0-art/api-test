"""框架的 pytest 插件层。

每个插件负责一件事：

- ``verify_plugin``    —— 校验层（数据库 / 缓存 / MQ）的连接管理和收尾
- ``ratelimit_plugin`` —— 令牌桶限流
- ``retry_plugin``     —— 用例级重试（只重试"疑似环境抖动"的失败）
- ``factory_plugin``   —— 测试数据工厂的加载和兜底清理

它们都是**可选的**：某个能力没配置 / 没装依赖，对应的插件自己会安静地跳过。
"""

from __future__ import annotations

__all__ = [
    "verify_plugin",
    "ratelimit_plugin",
    "retry_plugin",
    "factory_plugin",
]
