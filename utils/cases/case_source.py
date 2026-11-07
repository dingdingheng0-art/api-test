"""用例来源开关：用 Excel 维护，还是用 YAML 维护。

**为什么要这个开关**

同一个平台可能有人用 Excel 写用例（业务同学更顺手），有人用 YAML 写
（开发更顺手）。两种都支持，但不能同时跑 —— 否则同一个接口会被跑两遍，
报告也重复。

所以这里定义一个"当前用哪种方式维护"的开关，框架只按开关决定
收集哪边的用例。

**三种取值**

.. code-block:: yaml

    case_source:
      mode: excel          # excel / yaml / both
      excel_dir: "testcases/cases_excel"
      yaml_dir: "testcases/cases"

- ``excel`` —— 只跑 Excel 用例，YAML 一概不碰
- ``yaml`` —— 只跑 YAML 用例，Excel 一概不碰
- ``both`` —— 两边都跑（迁移期间的过渡状态）

**优先级**

1. 命令行 ``--case-source excel``
2. 环境变量 ``APITEST_CASE_SOURCE``
3. ``configs/config.yaml`` 里的 ``case_source.mode``
4. 默认 ``excel``

**为什么用目录隔离而不是按扩展名过滤**

如果两套用例混在同一个目录里，光看扩展名过滤不干净 —— 一个模块目录下
可能同时躺着 ``statistics.xlsx`` 和 ``order.yaml``，"选 Excel 就完全不碰 YAML"
这个承诺就落空了。所以两边分开放，各自一个根目录。

目录可以改：``excel_dir`` / ``yaml_dir`` 支持相对项目根的路径。
"""
from __future__ import annotations

import os
from enum import Enum
from pathlib import Path
from typing import Any

from utils.logs.log_manager import log
from utils import paths


class CaseSource(str, Enum):
    """用例来源。"""

    #: 只用 Excel 维护
    EXCEL = "excel"
    #: 只用 YAML 维护
    YAML = "yaml"
    #: 两边都跑（迁移过渡期）
    BOTH = "both"


#: 默认值 —— 平台主推 Excel 维护
DEFAULT_SOURCE = CaseSource.EXCEL

#: 配置文件里的默认值
DEFAULT_EXCEL_DIR = "testcases/cases_excel"
DEFAULT_YAML_DIR = "testcases/cases"

#: Excel 用例的文件后缀
EXCEL_SUFFIXES = (".xlsx", ".xlsm")

#: YAML 用例的文件后缀
YAML_SUFFIXES = (".yaml",)


def _resolve_dir(raw: Any, fallback: Path) -> Path:
    """把配置里的目录值解析成绝对路径。

    相对路径按**项目根**算（不是按当前工作目录）——
    否则从别的目录启动脚本就会找不到用例。
    """
    text = str(raw or "").strip()
    if not text:
        return fallback

    candidate = Path(text)
    if candidate.is_absolute():
        return candidate

    return paths.PROJECT_ROOT / candidate


def _parse(raw: Any) -> CaseSource | None:
    """把一个字符串解析成来源枚举；认不出来返回 None。"""
    text = str(raw or "").strip().lower()
    if not text:
        return None

    aliases = {
        "excel": CaseSource.EXCEL,
        "xlsx": CaseSource.EXCEL,
        "xls": CaseSource.EXCEL,
        "yaml": CaseSource.YAML,
        "yml": CaseSource.YAML,
        "both": CaseSource.BOTH,
        "all": CaseSource.BOTH,
        "mixed": CaseSource.BOTH,
    }
    return aliases.get(text)


def get_case_source(override: str | None = None) -> CaseSource:
    """当前生效的用例来源。

    Args:
        override: 命令行传进来的值（``--case-source excel``）；
            为空就按"环境变量 > 配置文件 > 默认"的顺序找。

    Returns:
        :class:`CaseSource` 枚举。认不出来的值会退���默认值并记一条警告。
    """
    # 1. 命令行最高优先级
    parsed = _parse(override)
    if parsed is not None:
        return parsed

    # 2. 环境变量
    parsed = _parse(os.environ.get("APITEST_CASE_SOURCE", ""))
    if parsed is not None:
        return parsed

    # 3. 配置文件
    try:
        from utils.config.config_manager import ConfigManager

        cfg = ConfigManager.load_config().get("case_source") or {}
        parsed = _parse(cfg.get("mode"))
        if parsed is not None:
            return parsed
    except Exception as exc:  # noqa: BLE001 - 读不到就用默认值
        log.warning("读取 case_source 配置失败（用默认值 %s）：%s", DEFAULT_SOURCE.value, exc)

    return DEFAULT_SOURCE


def get_source_roots(
    source: CaseSource | None = None,
    override: str | None = None,
) -> list[Path]:
    """当前应该扫描哪些用例根目录。

    Returns:
        按顺序返回要扫描的目录。``both`` 返回两个，``excel`` / ``yaml`` 返回一个。
    """
    active = source or get_case_source(override)

    try:
        from utils.config.config_manager import ConfigManager

        cfg = ConfigManager.load_config().get("case_source") or {}
    except Exception:  # noqa: BLE001
        cfg = {}

    excel_dir = _resolve_dir(cfg.get("excel_dir"), paths.CASES_EXCEL_ROOT)
    yaml_dir = _resolve_dir(cfg.get("yaml_dir"), paths.CASES_ROOT)

    if active is CaseSource.EXCEL:
        return [excel_dir]
    if active is CaseSource.YAML:
        return [yaml_dir]
    return [excel_dir, yaml_dir]


def get_active_roots(override: str | None = None) -> list[Path]:
    """当前生效的用例根目录（存在的那些）。"""
    return [root for root in get_source_roots(override=override) if root.exists()]


def current_excel_dir(override: str | None = None) -> Path:
    """Excel 用例该往哪个目录写。

    生成模板、导出用例这类"要产出 Excel"的操作都用它 ——
    免得产出到没被扫描的目录里（用例写了却跑不起来）。
    """
    from utils.config.config_manager import ConfigManager

    try:
        cfg = ConfigManager.load_config().get("case_source") or {}
    except Exception:  # noqa: BLE001
        cfg = {}

    return _resolve_dir(cfg.get("excel_dir"), paths.CASES_EXCEL_ROOT)


def get_allowed_suffixes(
    source: CaseSource | None = None,
    override: str | None = None,
) -> tuple[str, ...]:
    """当前来源允许扫描的文件后缀。

    目录隔离之外再加一层后缀过滤 —— 双保险。

    为什么不光靠目录隔离：``both`` 模式下两个目录都会扫，而迁移期间
    可能有人在 ``cases_excel/`` 里顺手放了个 ``notes.yaml``（不是用例）。
    加上后缀过滤，这种文件就不会被当成用例。
    """
    active = source or get_case_source(override)

    if active is CaseSource.EXCEL:
        return EXCEL_SUFFIXES
    if active is CaseSource.YAML:
        return YAML_SUFFIXES
    return EXCEL_SUFFIXES + YAML_SUFFIXES


def describe(override: str | None = None) -> str:
    """一句话描述当前来源 —— 给日志和命令行输出用。"""
    source = get_case_source(override)
    roots = get_source_roots(source)
    names = "、".join(root.relative_to(paths.PROJECT_ROOT).as_posix() for root in roots)
    label = {
        CaseSource.EXCEL: "Excel",
        CaseSource.YAML: "YAML",
        CaseSource.BOTH: "Excel + YAML",
    }[source]
    return f"{label}（目录：{names}）"
