"""根 conftest：把框架各层接起来。

**pytest 工程根就在项目根**，加载的内容：

- ``framework.pytest_plugin``       —— 收集 YAML/Excel 用例、命令行参数、mark 筛选
- ``framework.hooks``               —— pytest 生命周期钩子（结果统计、报告准备）
- ``framework.fixtures``            —— 给用例提供的 fixture（api_client 等）
- ``framework.html_report_plugin``  —— HTML 报告表格加「模块/场景/用例」三列

**目录结构**

.. code-block:: text

    scripts/       入口脚本（run.py / run_unit.py …）
    testcases/     用例数据 + 用例执行逻辑
    configs/       配置文件
    logs/          运行日志
    reports/       测试报告
    utils/         通用封装（按职责分子包）
    framework/     pytest 插件（就是本文件加载的）
    verify/        数据库 / 缓存 / MQ 校验
    factory/       测试数据工厂
    adapters/      被测系统适配（登录等）
    ai/            AI 能力层（用例生成 / 缺陷分析 / 智能体 / MCP）
    tools/         维护与导入工具
"""
from __future__ import annotations

import sys
from pathlib import Path

# 项目根（能 import 到 utils.* / testcases.* / framework.*）
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 各层都通过 pytest_plugins 声明，pytest 才会真正加载
# （直接 import 的话，fixture 和 hook 都不会被注册）
#
# 顺序很重要：plugin 先加载（它注册 --apienv 命令行参数），
# hooks 后加载（它要用到这个参数）。
pytest_plugins = [
    "framework.pytest_plugin",       # 收集 / 参数 / mark / 命令行参数
    "framework.hooks",               # 生命周期钩子
    "framework.fixtures",            # fixture
    "framework.html_report_plugin",  # HTML 报告表格加「模块/场景/用例」三列
    # ---- 以下是可选增强插件（没配置/没装依赖时会自己安静跳过）----
    "framework.plugins.verify_plugin",     # 数据库 / 缓存 / MQ 校验的连接管理
    "framework.plugins.ratelimit_plugin",  # 令牌桶限流
    "framework.plugins.retry_plugin",      # 用例级重试（只重试环境抖动）
    "framework.plugins.factory_plugin",    # 测试数据工厂加载 + 兜底清理
]
