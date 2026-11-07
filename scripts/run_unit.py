"""单元测试入口：跑框架自己的单测。

用法::

    python scripts/run_unit.py              # 跑全部单元测试
    python scripts/run_unit.py -v           # 显示每个用例
    python scripts/run_unit.py -k excel     # 按关键字筛

**为什么单独一个入口**

单元测试（``unit_tests/``）测的是框架自己的代码 ——
比如 Excel 解析、断言处理。

它们和接口用例（``testcases/``）是两回事：

- 单元测试不需要网络、不需要登录，跑得飞快
- 接口用例要真的调服务端

混在一起会让接口测试报告里塞满框架自己的单测，
报告就没法看了 —— 所以 ``run.py`` 会排除 ``unit_tests/``。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

# 本文件在 scripts/ 下，项目根是它的上一级
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
UNIT_TESTS_DIR = _PROJECT_ROOT / "unit_tests"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="框架单元测试入口")
    parser.add_argument("-v", "--verbose", action="store_true", help="显示每个用例")
    parser.add_argument("-k", "--keyword", default=None, help="按关键字筛选")
    parser.add_argument("-x", "--exitfirst", action="store_true", help="第一个失败就停")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if not UNIT_TESTS_DIR.exists():
        print(f"单元测试目录不存在: {UNIT_TESTS_DIR}")
        return 1

    cmd = [
        sys.executable,
        "-m",
        "pytest",
        str(UNIT_TESTS_DIR),
        "-q",
        "-p",
        "no:cacheprovider",
        "--no-header",
    ]
    if args.verbose:
        cmd.append("-v")
    if args.exitfirst:
        cmd.append("-x")
    if args.keyword:
        cmd += ["-k", args.keyword]

    print("单元测试：")
    print("  " + " ".join(cmd[2:]))
    print()

    return subprocess.call(cmd, cwd=str(_PROJECT_ROOT))


if __name__ == "__main__":
    raise SystemExit(main())
