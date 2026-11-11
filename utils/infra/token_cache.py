"""登录凭据缓存：避免每次跑测试都重新滑块 + 登录。

**为什么需要缓存**

mall4j 的登录链路是"抓 uuid → 识别滑块缺口 → 校验 → 登录"，
每次大概 6 秒。token 有效期很长（JWT 里能读到，一般是几十天），
但每次跑测试都重登一遍太慢 —— 尤其登录类用例本身就要走滑块。

**缓存什么**

- ``token`` —— 业务接口的 accesstoken
- ``uuid`` —— 会话标识（业务接口也要带这个头）
- ``expire_at`` —— token 的过期时间（从 JWT 的 exp 字段读，读不到就按保守值算）

**什么时候不用缓存**

- 登录类用例（``operation_type: login``）**永远走真实链路** ——
  它们就是为了验证登录本身，缓存了就失去意义。
- token 过期了会自动清掉缓存重新登录。

**缓存在哪**

``apitest/.cache/credentials.json``（在 .gitignore 里，不会误提交）。

**怎么关掉**

```powershell
$env:APITEST_NO_TOKEN_CACHE = "1"
```
"""
from __future__ import annotations

import base64
import json
import os
import time
from typing import Any

from utils.logs.log_manager import log

from utils.paths import CACHE_DIR
CACHE_FILE = CACHE_DIR / "credentials.json"

# 读不到 exp 时，按这个时间算（秒）—— 保守一点，别缓存太久
DEFAULT_TTL = 3600

# 缓存结构版本。老缓存里没有 refresh_token，升级后强制重登一次再写回。
CACHE_VERSION = 2


class TokenCache:
    """登录凭据缓存。"""

    @staticmethod
    def enabled() -> bool:
        """缓存是否启用。"""
        return os.environ.get("APITEST_NO_TOKEN_CACHE", "").strip() not in ("1", "true", "yes")

    @staticmethod
    def _read_all() -> dict[str, Any]:
        if not CACHE_FILE.exists():
            return {}
        try:
            data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _write_all(data: dict[str, Any]) -> None:
        """把整份缓存写回磁盘。

        **为什么要原子写**

        以前是直接 ``write_text``。并行跑的时候两个 worker 同时写，
        或者进程写到一半被杀，文件就只剩半截 JSON —— 下次读取解析失败，
        所有 worker 都以为"没有缓存"，集体重新登录（滑块一次 6 秒）。

        先写临时文件再 ``os.replace``（同一分区上是原子操作），
        读的一方要么看到旧内容，要么看到新内容，不会看到半截。

        **明文问题**：文件里确实是明文 token。它靠两点兜底：
        1. ``.cache/`` 在 .gitignore 里，不会误提交
        2. 写完后把文件权限收成「仅当前用户可读写」（POSIX 上有效；
           Windows 的 ACL 由父目录继承，这里只是尽力而为）

        真要更强隔离，用 ``APITEST_NO_TOKEN_CACHE=1`` 关掉缓存。
        """
        tmp = CACHE_FILE.with_name(CACHE_FILE.name + ".tmp")
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, CACHE_FILE)
        except OSError as exc:
            log.warning("写 token 缓存失败：%s", exc)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _locked_update(mutate: Any) -> None:
        """加锁做一次「读 → 改 → 写」。

        并行时多个 worker 会同时登录、同时写缓存。不锁的话：
        worker A 读到 ``{mall4j: ...}``，worker B 也读到同一份，
        A 写回 ``{mall4j: 新值}``，B 写回 ``{mall4j: 旧值, demo: ...}`` ——
        A 刚登录的凭据就被 B 覆盖了。

        拿不到锁（超时）也照样写：缓存写坏了大不了重登一次，
        不值得让流程卡住。
        """
        try:
            from utils.infra.parallel import file_lock

            with file_lock("token_cache.lock", timeout=10.0):
                data = TokenCache._read_all()
                mutate(data)
                TokenCache._write_all(data)
        except Exception as exc:  # noqa: BLE001 - 锁出问题就退化成不加锁
            log.debug("token 缓存加锁失败，按不加锁处理: %s", exc)
            data = TokenCache._read_all()
            mutate(data)
            TokenCache._write_all(data)

    @staticmethod
    def get(project: str) -> dict[str, Any] | None:
        """取缓存的凭据；没有或过期返回 None。"""
        if not TokenCache.enabled():
            return None

        data = TokenCache._read_all()
        item = data.get(project)
        if not isinstance(item, dict):
            return None

        # 旧版缓存没有 refresh_token，直接复用会导致「刷新令牌」用例拿不到参数。
        # 清掉重登一次，写回新版结构后就不再重登了。
        if int(item.get("version") or 1) < CACHE_VERSION:
            log.info("%s 的 token 缓存是旧格式，重新登录一次", project)
            TokenCache.clear(project)
            return None

        token = item.get("token")
        if not token:
            return None

        expire_at = item.get("expire_at", 0)
        now = time.time()

        if expire_at and now >= expire_at:
            log.info("%s 的 token 缓存已过期，重新登录", project)
            TokenCache.clear(project)
            return None

        # 留 60 秒余量，别在用的时候刚好过期
        if expire_at and now >= expire_at - 60:
            log.info("%s 的 token 缓存快过期了，重新登录", project)
            TokenCache.clear(project)
            return None

        remaining = ""
        if expire_at:
            remaining = f"，还有 {int((expire_at - now) / 60)} 分钟"
        log.info("复用 %s 的 token 缓存（%s）%s", project, token[:16], remaining)
        return item

    @staticmethod
    def save(
        project: str,
        token: str,
        uuid: str = "",
        headers: dict[str, Any] | None = None,
        refresh_token: str = "",
    ) -> None:
        """保存凭据。"""
        if not TokenCache.enabled():
            return

        expire_at = TokenCache._extract_expire(token)
        entry = {
            "version": CACHE_VERSION,
            "token": token,
            "refresh_token": refresh_token,
            "uuid": uuid,
            "headers": headers or {},
            "expire_at": expire_at,
            "saved_at": time.time(),
        }
        # 读-改-写要在锁里做，否则并行时别的 worker 刚存的凭据会被覆盖
        TokenCache._locked_update(lambda data: data.__setitem__(project, entry))
        log.info("已缓存 %s 的 token（有效期至 %s）", project,
                 time.strftime("%Y-%m-%d %H:%M", time.localtime(expire_at)) if expire_at else "未知")

    @staticmethod
    def clear(project: str | None = None) -> None:
        """清缓存（不传 project 就全清）。"""
        if not CACHE_FILE.exists():
            return

        if project is None:
            try:
                CACHE_FILE.unlink()
                log.info("已清空 token 缓存")
            except OSError:
                pass
            return

        removed = False

        def _drop(data: dict[str, Any]) -> None:
            nonlocal removed
            # 用 or：极端情况下回调可能被调用两次（见 _locked_update 的兜底）
            removed = (data.pop(project, None) is not None) or removed

        TokenCache._locked_update(_drop)
        if removed:
            log.info("已清除 %s 的 token 缓存", project)

    @staticmethod
    def _extract_expire(token: str) -> float:
        """从 JWT 的 payload 里读 exp。

        读不到就给个保守的短有效期（``DEFAULT_TTL``），
        免得缓存了一个不知道什么时候过期的 token。
        """
        try:
            parts = token.split(".")
            if len(parts) >= 2:
                payload = parts[1]
                payload += "=" * (4 - len(payload) % 4)
                data = json.loads(base64.urlsafe_b64decode(payload))
                exp = data.get("exp")
                if isinstance(exp, (int, float)):
                    return float(exp)
        except Exception:  # noqa: BLE001
            pass
        # 读不到 exp 就给个保守的短有效期
        return time.time() + DEFAULT_TTL

    @staticmethod
    def show() -> None:
        """打印当前缓存状态（调试用）。"""
        if not CACHE_FILE.exists():
            log.info("没有 token 缓存")
            return

        data = TokenCache._read_all()
        now = time.time()
        for project, item in data.items():
            expire_at = item.get("expire_at", 0)
            state = "已过期" if expire_at and now >= expire_at else "有效"
            remaining = f"，还有 {int((expire_at - now) / 60)} 分钟" if expire_at else ""
            log.info(
                "%s: %s（%s%s）",
                project, item.get("token", "")[:16], state, remaining,
            )
