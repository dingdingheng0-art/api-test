"""运行时状态文件（小 JSON）的原子写入。

**为什么不能直接 ``write_text``**

``.cache/run_budget.json``、``.cache/heal_state.json`` 这类文件是
「跑了多少条 / 改了几轮」的唯一记录。直接 ``write_text`` 有两个坑：

1. **写到一半被打断**（进程被杀、磁盘满）→ 文件只剩半截 JSON，
   下次读取解析失败、按空处理 —— 计数清零等于闸门失效
2. **两个写入者同时写**→ 后写的覆盖先写的，中间那份内容直接消失

做法：先写同目录下的临时文件，``fsync`` 之后再 ``os.replace``。
同一分区上 ``os.replace`` 是原子操作，读到的要么是旧内容、要么是新内容，
不会是半截。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def atomic_write_json(path: Path | str, data: Any, *, indent: int | None = None) -> None:
    """把 ``data`` 原子地写成 JSON 文件。

    Args:
        path: 目标文件
        data: 可 JSON 序列化的对象
        indent: 缩进（``None`` = 单行，省空间）

    Raises:
        OSError: 写不进去（磁盘满、权限不足等）—— 调用方自己决定要不要吞。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    # 临时文件必须和目标同目录，跨分区 os.replace 不是原子操作
    tmp = target.with_name(f".{target.name}.tmp.{os.getpid()}")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=indent)
            f.flush()
            os.fsync(f.fileno())  # 保证内容真的落盘，再替换
        os.replace(tmp, target)
    except BaseException:
        # 失败时别把半截临时文件留在目录里
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
