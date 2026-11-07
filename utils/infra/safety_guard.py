"""危险写操作防护 —— 防止测试用例误删真实数据。

**为什么需要这个**

测试用例里写着 ``DELETE /admin/area/1`` 这种"占位"路径时，
它真的会去删 id=1 的那条记录。行政区划、菜单、角色这些是**基础配置数据**，
删掉之后整个后台就瘫了（我就在 area 上踩过这个坑，删掉了 49 条行政区）。

**防护规则**

1. 只拦 ``DELETE``（和带 body 的 ``PUT`` / ``POST``）—— 读操作不碰数据。
2. 只拦命中「受保护路径」的请求 —— 别的模块（自己造的数据）照常删。
3. 命中受保护路径时，URL 里的 id 必须落在「安全 id」白名单里：
   - 白名单可以配具体 id（自己造的测试数据）
   - 也可以配 id 的**范围**（比如只允许删 id > 1000000 的）
4. 拦下来时**不报错跳过**，而是**明确失败** —— 静默跳过最坑人，
   会让人以为用例跑过了。

**怎么放行**

真要删（比如专门写一条"删除行政区"的用例），在配置里加白名单，
或者设环境变量 ``APITEST_ALLOW_DANGEROUS_DELETE=1`` 临时全放行。
"""

from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import urlparse

#: 放行开关的环境变量名
ALLOW_ENV = "APITEST_ALLOW_DANGEROUS_DELETE"

#: URL 里的 id 提取正则（路径最后一段是数字就当 id）
_ID_IN_PATH = re.compile(r"/(\d+)(?:[/?#]|$)")


class DangerousRequestBlocked(RuntimeError):
    """危险请求被防护拦下。"""


def _config() -> dict[str, Any]:
    from utils.config.config_manager import ConfigManager

    try:
        return ConfigManager.load_config() or {}
    except Exception:  # noqa: BLE001 - 配置读不到就当没配防护
        return {}


def _rules() -> dict[str, Any]:
    return (_config().get("safety") or {}).get("dangerous_write") or {}


def _is_allowed_globally() -> bool:
    return str(os.environ.get(ALLOW_ENV, "")).strip().lower() in ("1", "true", "yes", "on")


def allow_dangerous_enabled() -> bool:
    """全局放行开关开了没有（``APITEST_ALLOW_DANGEROUS_DELETE=1``）。

    给数据库操作那条安全闸复用 —— 同一个开关管所有危险写操作，
    不用记两个环境变量。
    """
    return _is_allowed_globally()


def _protected_prefixes() -> list[str]:
    return [str(p) for p in (_rules().get("protected_paths") or [])]


def _safe_ids() -> list[int]:
    out: list[int] = []
    for raw in _rules().get("safe_ids") or []:
        try:
            out.append(int(raw))
        except (TypeError, ValueError):
            continue
    return out


def _safe_id_ranges() -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for raw in _rules().get("safe_id_ranges") or []:
        try:
            low, high = str(raw).split("-", 1)
            out.append((int(low), int(high)))
        except (TypeError, ValueError):
            continue
    return out


def _matches_prefix(path: str, prefixes: list[str]) -> str | None:
    """路径命中哪个受保护前缀。

    注意：接口根地址里往往带了前缀（mall4j 是 ``/_admin``），
    所以要拿**去掉根前缀之后**的路径来比，否则 ``/_admin/admin/area/1``
    匹配不上 ``/admin/area``。
    """
    for prefix in prefixes:
        p = prefix.rstrip("/")
        if _strip_base_prefix(path) == p or _strip_base_prefix(path).startswith(p + "/"):
            return p
    return None


def _strip_base_prefix(path: str) -> str:
    """去掉接口根前缀（``/_admin`` / ``/_api`` / ``/prod`` 之类）。"""
    for base in ("/_admin", "/_api"):
        if path == base:
            return "/"
        if path.startswith(base + "/"):
            return path[len(base):]
    return path


def extract_id(path: str) -> int | None:
    """从路径里抠出 id（最后一段是数字就当 id）。"""
    match = _ID_IN_PATH.search(path)
    return int(match.group(1)) if match else None


def check_request(method: str, url: str, data: Any = None, allow: bool = False) -> None:
    """检查这个请求要不要拦。

    只拦 ``DELETE`` —— 删数据是不可逆的破坏操作。``POST`` / ``PUT``
    只是「多一条 / 改一条」，而且那正是测试本身要验证的行为，
    拦掉反而让用例没法跑。

    Args:
        allow: 显式放行。用例的**后置清理**会传 True ——
            删的是这条用例自己刚造出来的数据，删它是应该的。

    Raises:
        DangerousRequestBlocked: 请求会破坏真实数据。
    """
    if allow or _is_allowed_globally():
        return

    method = (method or "").upper()
    if method != "DELETE":
        return

    parsed = urlparse(url)
    path = parsed.path or ""
    prefix = _matches_prefix(path, _protected_prefixes())
    if prefix is None:
        return

    # id 在白名单里 → 放行（自己造的数据）
    target_id = extract_id(path)
    if target_id is not None:
        if target_id in _safe_ids():
            return
        for low, high in _safe_id_ranges():
            if low <= target_id <= high:
                return

    # body 里带 id 的（DELETE /admin/user 这种 body 传 id 的）也查一下
    if isinstance(data, dict):
        for key in ("id", "areaId", "userId", "roleId", "menuId", "prodId"):
            raw = data.get(key)
            if raw is None:
                continue
            try:
                body_id = int(raw)
            except (TypeError, ValueError):
                continue
            if body_id in _safe_ids():
                return
            for low, high in _safe_id_ranges():
                if low <= body_id <= high:
                    return

    raise DangerousRequestBlocked(
        f"危险请求被拦截：{method} {url}\n"
        f"  路径命中受保护前缀 {prefix!r}，但目标 id 不在安全白名单里。\n"
        f"  确认要删这条数据的话，把 id 加进 config.yaml 的 "
        f"safety.dangerous_write.safe_ids，或设环境变量 {ALLOW_ENV}=1 临时放行。"
    )
