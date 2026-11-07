"""盘点用例的断言强度（改完断言跑一遍，看还有没有漏网的）。

**为什么需要**：断言加强是分批做的，做完一批要能一眼看出还剩哪些薄弱用例，
以及薄弱在哪 —— 光看"通过率"没用，全绿的用例也可能是空断言。

统计口径（只看 ``用例状态 = debugged``）：

- **只有 basic** —— 主步骤断言 <= 2 条，且没有 ``exists`` / 查库断言
- **有业务码** —— 主步骤有 ``code == "00000"``（或验证码的 ``repCode == "0000"``）
- **有字段断言** —— 主步骤有 ``exists``
- **有查库断言** —— 主步骤或前后置里有 ``db_*``

用法::

    python tools/maint/survey_assertions.py
    python tools/maint/survey_assertions.py --weak-only   # 只列薄弱用例
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.excel.flat_parser import load_flat_cases  # noqa: E402

CASE_ROOT = PROJECT_ROOT / "testcases" / "cases_excel"


def _all_asserts(case: dict[str, Any]) -> list[dict[str, Any]]:
    steps = [case["steps"][-1]]
    steps += case.get("setups") or []
    steps += case.get("teardowns") or []
    asserts: list[dict[str, Any]] = []
    for step in steps:
        asserts.extend(step.get("assert") or [])
    return asserts


def survey() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(CASE_ROOT.rglob("*.xlsx")):
        try:
            cases, _ = load_flat_cases(path)
        except Exception as exc:  # noqa: BLE001 - 个别文件读不了不该挡住盘点
            print(f"! 跳过 {path.name}: {exc}")
            continue
        for raw in cases:
            case = raw["testcase"]
            if case.get("status") != "debugged":
                continue

            main = case["steps"][-1].get("assert") or []
            every = _all_asserts(case)
            types = [str(item.get("type")) for item in every]

            rows.append(
                {
                    "module": path.parent.name,
                    "case_id": case.get("case_id"),
                    "name": case.get("name"),
                    "asserts": len(main),
                    "code": any(
                        item.get("type") == "equal"
                        and item.get("field") in ("code", "data.repCode")
                        for item in main
                    ),
                    "exists": any(
                        item.get("type") in ("exists", "field_exists") for item in main
                    ),
                    "db": any(name.startswith("db_") for name in types),
                    "only_basic": len(main) <= 2
                    and not any(
                        item.get("type") in ("exists", "field_exists") for item in main
                    ),
                }
            )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="盘点用例断言强度")
    parser.add_argument("--weak-only", action="store_true", help="只列薄弱用例")
    args = parser.parse_args()

    rows = survey()
    weak = [row for row in rows if row["only_basic"]]

    print(f"debugged 用例总数: {len(rows)}")
    print(f"只有 status_code+success 的: {len(weak)}")
    print(f"有业务码断言的: {sum(1 for row in rows if row['code'])}")
    print(f"有字段断言的: {sum(1 for row in rows if row['exists'])}")
    print(f"有查库断言的: {sum(1 for row in rows if row['db'])}")

    if weak:
        print("\n薄弱用例按模块分布:")
        for module, count in Counter(row["module"] for row in weak).most_common():
            print(f"  {count:3}  {module}")

    if args.weak_only:
        print("\n薄弱用例:")
        for row in weak:
            print(f"  {row['case_id']:8} {row['module']:16} {row['name']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
