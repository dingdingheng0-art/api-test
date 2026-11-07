"""密钥治理：敏感信息的加载、检测、兜底。

**要解决什么问题**

配置文件（``configs/config.yaml``）是要进 git 的，但里面需要密码、token 这类
敏感信息。以前的做法是写环境变量占位符：

.. code-block:: yaml

    password: "${MALL4J_PASSWORD:123456}"

问题有三个：

1. **默认值是能用的** —— ``123456`` 是真实可用的密码，谁 clone 下来都能连生产
2. **占位符必须记名字** —— 加一个系统要配十几个环境变量，容易漏
3. **没人检查** —— 哪天有人图省事直接写了明文，代码评审不一定看得出来

现在改成三层：

.. code-block:: text

    1. 环境变量        —— 最高优先级，CI 里用这个
    2. .env 文件       —— 本地开发用，不进 git（.gitignore 已含）
    3. 配置里的占位符  —— 兜底，可以没有

**新增敏感信息怎么加**

1. 在 ``configs/.env.example`` 里加一行（这个文件进 git，只有键名没有值）
2. 在 ``config.yaml`` 里用 ``${变量名}`` 引用，**不要写默认值**
3. 跑 ``python tools/maint/scan_secrets.py`` 确认没漏

**生产环境兜底**

``default_env: online`` 直接指向生产 —— 这本身就是个风险。
本模块提供 :func:`check_production_default` 做提醒，CI 可以据此拒绝启动。
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from utils.logs.log_manager import log

# 项目根
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 本地 .env 文件（不进 git）
ENV_FILE = PROJECT_ROOT / ".env"

# 示例文件（进 git，只有键名）
ENV_EXAMPLE = PROJECT_ROOT / "configs" / ".env.example"

# ---- 敏感关键词（用来扫描配置里有没有硬编码密钥）----

# 这些键名下面的值被认为是敏感信息
#
# ⚠️ 不能只看有没有 "token" 字样 —— ``token_header``（请求头叫什么）、
# ``token_path``（响应里 token 在哪个字段）都是**配置说明**，不是密钥。
# 所以这里用的是「精确匹配」而不是「包含」。
SENSITIVE_KEY_NAMES = frozenset({
    "password",
    "passwd",
    "pwd",
    "pass",
    "secret",
    "api_key",
    "apikey",
    "access_key",
    "accesskey",
    "secret_key",
    "private_key",
    "privatekey",
    "token",
    "access_token",
    "accesstoken",
    "refresh_token",
    "id_token",
    "credential",
    "credentials",
    "uuid",
    "authorization",
    "auth_key",
    "salt",
    "signature",
})

# 这些是**包含**敏感词但通常不是密钥的键（要排除）
SENSITIVE_KEY_EXCLUDE = frozenset({
    "token_header",
    "token_var",
    "token_path",
    "session_header",
    "session_var",
    "auth_header",
    "header",
    "headers",
    "cookie",
    "secret_header",
})

# 这些值长得像密钥（硬编码在配置里时告警）
SENSITIVE_VALUE_PATTERNS = (
    re.compile(r"^[0-9a-f]{32,64}$", re.IGNORECASE),      # md5 / sha
    re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE),  # uuid
    re.compile(r"^ey[A-Za-z0-9_-]{20,}$"),                # JWT
)

# 已知是"演示用"的值，不算真密钥（这些本来就是假的系统）
KNOWN_FAKE_VALUES = frozenset({
    "demo", "demo123", "guest", "test", "example",
    "password", "changeme", "your_password", "xxx", "",
})

# 值的最小长度 —— 太短的不算密钥（避免把 "0"、"1" 之类误报）
MIN_SECRET_LENGTH = 6


def load_env_file(path: Path | None = None, override: bool = False) -> dict[str, str]:
    """读 ``.env`` 文件。

    格式就是 KEY=VALUE，一行一个，``#`` 开头是注释::

        MALL4J_PASSWORD=xxx
        DB_PASSWORD=yyy

    Args:
        path: 文件路径（默认项目根的 ``.env``）
        override: 已有同名环境变量时是否覆盖（默认 False —— 环境变量优先）
    """
    target = path or ENV_FILE

    if not target.exists():
        return {}

    values: dict[str, str] = {}
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("读 .env 文件失败: %s", exc)
        return {}

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue

        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()

        # 去掉引号
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]

        if not key:
            continue

        if not override and key in os.environ:
            continue

        values[key] = value

    return values


def apply_env_file(path: Path | None = None) -> int:
    """把 ``.env`` 里的值灌进环境变量。

    返回灌进去的条数。已经存在的环境变量不动（环境变量优先）。
    """
    values = load_env_file(path)
    count = 0
    for key, value in values.items():
        if key not in os.environ:
            os.environ[key] = value
            count += 1
    return count


def is_sensitive_key(key: str) -> bool:
    """这个键名是不是敏感信息。

    判定方式：键名去下划线后**完全等于**某个敏感词才算。
    这样 ``token_header``（头名）、``token_path``（字段路径）不会被误判，
    但 ``db_password`` 里的 ``password`` 也不会漏 —— 所以还要看子串。
    """
    lowered = str(key or "").lower().strip()

    if lowered in SENSITIVE_KEY_EXCLUDE:
        return False

    if lowered in SENSITIVE_KEY_NAMES:
        return True

    # 带前缀/后缀的（db_password、mysql_password、user_secret）
    # 拆开看每一段
    parts = re.split(r"[_\-\.]+", lowered)
    for part in parts:
        if part in SENSITIVE_KEY_NAMES:
            return True
        # 常见组合词
        if part in ("password", "passwd", "pwd", "secret", "apikey"):
            return True

    return False


def looks_like_secret(value: Any) -> bool:
    """这个值长得像硬编码的密钥。"""
    if not isinstance(value, str):
        return False

    text = value.strip()
    if not text or text in KNOWN_FAKE_VALUES:
        return False
    if len(text) < MIN_SECRET_LENGTH:
        return False

    # 占位符不算（那正是推荐写法）
    if "${" in text:
        return False

    return any(pattern.match(text) for pattern in SENSITIVE_VALUE_PATTERNS)


def scan_config(config: dict[str, Any], path_prefix: str = "") -> list[dict[str, Any]]:
    """扫描配置，找出硬编码的敏感信息。

    Returns:
        ``[{"路径": "login.password", "值": "123456", "原因": "..."}, ...]``
    """
    findings: list[dict[str, Any]] = []

    if not isinstance(config, dict):
        return findings

    for key, value in config.items():
        current = f"{path_prefix}.{key}" if path_prefix else str(key)

        if isinstance(value, dict):
            findings.extend(scan_config(value, current))
            continue

        if is_sensitive_key(key) and isinstance(value, str):
            text = value.strip()
            if not text:
                continue
            if "${" in text:
                continue  # 占位符，推荐写法
            if text in KNOWN_FAKE_VALUES:
                continue
            if len(text) < MIN_SECRET_LENGTH:
                continue
            findings.append(
                {
                    "路径": current,
                    "值": _mask(text),
                    "原因": "敏感字段直接写了明文",
                }
            )
            continue

        if looks_like_secret(value):
            findings.append(
                {
                    "路径": current,
                    "值": _mask(str(value).strip()),
                    "原因": "值长得像密钥/会话凭证",
                }
            )

    return findings


def _mask(value: str) -> str:
    """打码 —— 日志和报告里不该出现完整密钥。"""
    if len(value) <= 4:
        return "***"
    return f"{value[:2]}***{value[-2:]}"


def check_production_default(config: dict[str, Any]) -> str | None:
    """检查默认环境是不是指向生产。

    Returns:
        有风险时返回一句提示，正常返回 None
    """
    default_env = str(config.get("default_env") or "").strip().lower()

    if default_env in ("online", "prod", "production", "formal", "pre_prod"):
        return (
            f"default_env 是「{default_env}」，本地跑用例会直接打到生产环境。"
            f"建议改成 test/pre，生产环境用 --apienv {default_env} 显式指定。"
        )
    return None


def ensure_env_file_created() -> bool:
    """``.env`` 不存在时，从示例模板复制一份。

    返回 True 表示创建了。
    """
    if ENV_FILE.exists():
        return False

    if not ENV_EXAMPLE.exists():
        return False

    try:
        ENV_FILE.write_text(
            ENV_EXAMPLE.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    except OSError as exc:
        log.warning("创建 .env 失败: %s", exc)
        return False

    log.info("已根据模板创建 %s，请填入本地用的密码等敏感信息", ENV_FILE.name)
    return True
