"""项目路径常量：全项目统一从这里取路径，不要各自 ``Path(__file__)``。

目录结构::

    <项目根>/
    ├── scripts/       入口脚本（run.py / run_unit.py / new_case.py …）
    ├── testcases/     用例数据 + 用例执行逻辑
    ├── configs/       配置文件
    ├── logs/          运行日志
    ├── reports/       测试报告
    ├── utils/         通用封装（按职责分子包）
    ├── framework/     pytest 插件 / 报告生成
    ├── ai/            AI 能力层（用例生成 / 缺陷分析 / 智能体 / MCP）
    └── tools/         维护与导入工具
"""
from __future__ import annotations

from pathlib import Path

# 项目根（utils/paths.py 的上一级）
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ---- 各层目录 ----
TESTCASES_DIR = PROJECT_ROOT / "testcases"          # 用例层
CASES_ROOT = TESTCASES_DIR / "cases"                # YAML 用例数据
CASES_EXCEL_ROOT = TESTCASES_DIR / "cases_excel"    # Excel 用例数据
CONFIGS_DIR = PROJECT_ROOT / "configs"              # 配置层
LOGS_DIR = PROJECT_ROOT / "logs"                    # 日志层
REPORTS_DIR = PROJECT_ROOT / "reports"              # 报告层
UTILS_DIR = PROJECT_ROOT / "utils"                  # 封装层
FRAMEWORK_DIR = PROJECT_ROOT / "framework"          # pytest 插件层
SCRIPTS_DIR = PROJECT_ROOT / "scripts"              # 入口脚本
AI_DIR = PROJECT_ROOT / "ai"                        # AI 能力层
TOOLS_DIR = PROJECT_ROOT / "tools"                  # 维护 / 导入工具
DATA_DIR = PROJECT_ROOT / "data"                    # 调试数据

# ---- 配置文件 ----
CONFIG_FILE = CONFIGS_DIR / "config.yaml"
MODULES_FILE = CONFIGS_DIR / "modules.yaml"

# ---- 报告产物 ----
REPORTS_ROOT = REPORTS_DIR
ALLURE_RESULTS = REPORTS_DIR / "allure-results"
HTML_REPORT = REPORTS_DIR / "report.html"

# ---- 日志产物 ----
LOG_DIR = LOGS_DIR
LOG_FILE = LOGS_DIR / "framework.log"

# ---- 缓存 ----
CACHE_DIR = PROJECT_ROOT / ".cache"


def ensure_dirs() -> None:
    """确保各层目录都存在（框架启动时调一次）。"""
    for directory in (TESTCASES_DIR, CONFIGS_DIR, LOGS_DIR, REPORTS_DIR, DATA_DIR):
        directory.mkdir(parents=True, exist_ok=True)
