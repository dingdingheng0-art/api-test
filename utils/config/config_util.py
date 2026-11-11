"""config.yaml 加载工具。

用法::

    from utils.config.config_util import get_config
    cfg = get_config()
    BASE = cfg["base_url"]
    UUID = cfg["uuid"]

**环境变量占位符**

配置里可以写 ``${环境变量名}``，加载时会被替换成环境变量的值::

    login:
      username: "admin"
      password: "${MALL4J_PASSWORD}"   # 从环境变量读，不落文件

也可以给默认值 ``${变量名:默认值}`` —— 环境变量没设就用默认值。

这样敏感信息（密码、token）就不用进 git 了。
"""
from __future__ import annotations

import os
import re
from typing import Any

import yaml

from utils.paths import CONFIG_FILE

# ${VAR} 或 ${VAR:默认值}
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::([^}]*))?\}")


def expand_env(value: Any) -> Any:
    """递归展开配置里的 ``${环境变量}`` 占位符。

    - 整个字符串就是一个占位符时，替换成环境变量的值（**保持字符串**）
    - 字符串里混着占位符时，做字符串替换
    - 环境变量没设且没有默认值 → 保持原样（方便一眼看出没配）

    .. note::

       不做类型还原（数字 / 布尔）—— 密码这类字段可能是纯数字，
       转成 int 会导致后续 MD5 / 字符串拼接出错。
       需要数字的地方在代码里转就行。
    """
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    if not isinstance(value, str):
        return value

    # 整串就是一个占位符 -> 直接替换（保持字符串类型）
    whole = _ENV_PATTERN.fullmatch(value)
    if whole:
        name = whole.group(1)
        default = whole.group(2)
        raw = os.environ.get(name, default)
        if raw is None:
            return value
        return raw

    def _replace(match: re.Match) -> str:
        name = match.group(1)
        default = match.group(2)
        return os.environ.get(name, default if default is not None else match.group(0))

    return _ENV_PATTERN.sub(_replace, value)


def load_raw_config(force: bool = False) -> dict[str, Any]:
    """加载 ``configs/config.yaml``，但**不展开** ``${环境变量}`` 占位符。

    给敏感信息扫描用 —— 扫描器要看的正是"文件里写了什么"，
    而不是"运行时变成了什么"。如果扫展开后的配置，``.env`` 里的真实
    密码会被误报成"硬编码"。
    """
    if not CONFIG_FILE.exists():
        raise FileNotFoundError(f"配置文件不存在: {CONFIG_FILE}")

    with CONFIG_FILE.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    if not isinstance(data, dict):
        raise ValueError(f"config.yaml 顶层必须是 mapping: {CONFIG_FILE}")

    return data


def load_config(force: bool = False) -> dict[str, Any]:
    """加载 configs/config.yaml 为 dict（进程内缓存；force=True 强制重读）。

    这里**只做转发**，真正的加载和缓存都在 ``ConfigManager`` 里 ——
    以前两个类各存一份缓存，``force=True`` 只刷新其中一个，
    于是同一个进程里会出现"两套配置"（改了 .env 重启后仍读到旧值）。
    对外保留原来的契约：文件不存在时抛 ``FileNotFoundError``
    （``ConfigManager`` 那边是记日志返回空字典，工具脚本需要明确报错）。

    加载顺序：

    1. 先把 ``.env`` 文件里的值灌进环境变量（不覆盖已有的环境变量）
    2. 再读 config.yaml，把 ``${环境变量}`` 展开
    """
    if not CONFIG_FILE.exists():
        raise FileNotFoundError(f"配置文件不存在: {CONFIG_FILE}")

    # 函数内导入：config_manager 依赖本模块的 expand_env，
    # 模块级导入会形成循环。
    from utils.config.config_manager import ConfigManager

    return ConfigManager.load_config(force=force)


def get_config() -> dict[str, Any]:
    """返回完整配置（浅拷贝，防外部误改）。"""
    return dict(load_config())


def get_value(key_path: str, default: Any = None) -> Any:
    """按 '.' 路径取值：get_value('login.username') -> 'admin'。"""
    node: Any = load_config()
    for part in key_path.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return default
    return node
