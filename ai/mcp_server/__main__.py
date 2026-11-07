"""``python -m mcp_server`` 入口。

把项目根加进 ``sys.path`` —— MCP 客户端拉起的进程工作目录不一定
是项目根（Claude Code 一般是，但不保证），依赖 cwd 会偶发
``ModuleNotFoundError``。
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from ai.mcp_server.server import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
