"""MD5 摘要（用于登录密码传输：与服务端一致的小写 32 位 hex）。"""
from __future__ import annotations

import hashlib


def md5(text: str) -> str:
    """对 utf-8 文本做 MD5，返回小写 hex。

    例：``md5("admin123")`` -> ``"e10adc3949ba59abbe56e057f20f883e"``
    """
    if text is None:
        raise ValueError("text is None")
    return hashlib.md5(text.encode("utf-8")).hexdigest()
