"""单元格值解析：JSON / 键值对 / 断言。

Excel 里一个格子能塞很多东西，这三个函数负责把它们翻译成 Python 对象。
"""
from __future__ import annotations

import json
import re
from typing import Any

from utils.excel.reader import ExcelCaseError


def parse_json_cell(value: Any, field: str = "") -> Any:
    """把单元格解析成 Python 对象。

    支持：

    - 直接是 JSON：``{"a": 1}`` / ``[1, 2]``
    - 空 → ``{}`` 或 ``[]``（看字段名猜）
    - 简单值（数字 / 布尔 / 字符串）→ 直接转
    """
    if value is None or value == "":
        return {} if field.startswith(("header", "param", "body", "data", "assert")) else ""

    if isinstance(value, (dict, list, int, float, bool)):
        return value

    text = str(value).strip()

    # 试着当 JSON 解析
    if text.startswith(("{", "[")):
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ExcelCaseError(
                f"第 {value} 行的 {field or '值'} 不是合法 JSON：{exc}"
            ) from exc

    # 布尔 / 数字
    low = text.lower()
    if low in ("true", "是"):
        return True
    if low in ("false", "否"):
        return False
    if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
        return int(text)

    return text


def parse_kv_cell(value: Any, field: str = "") -> dict[str, Any]:
    """把单元格解析成「键值字典」。

    支持两种写法：

    **每行一个键值**（推荐，能填下拉）::

        accesstoken: {{mall4j.token}}
        uuid: {{mall4j.uuid}}

    **一行 JSON**::

        {"accesstoken": "{{mall4j.token}}"}
    """
    if value is None or value == "":
        return {}

    if isinstance(value, dict):
        return value

    text = str(value).strip()

    if text.startswith("{"):
        parsed = parse_json_cell(text, field)
        if not isinstance(parsed, dict):
            raise ExcelCaseError(f"{field} 应该是 JSON 对象")
        return parsed

    result: dict[str, Any] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # 支持 a=b 和 a: b
        if "=" in line and ":" not in line.split("=")[0]:
            key, _, val = line.partition("=")
        elif ":" in line:
            key, _, val = line.partition(":")
        else:
            continue
        key = key.strip()
        val = val.strip()
        if not key:
            continue
        result[key] = _coerce_scalar(val)

    return result


def _coerce_scalar(text: str) -> Any:
    """把字符串转成数字 / 布尔 / 字符串。"""
    low = text.lower()
    if low in ("true", "是"):
        return True
    if low in ("false", "否"):
        return False
    if text.isdigit():
        return int(text)
    if text.startswith("-") and text[1:].isdigit():
        return int(text)
    try:
        if re.fullmatch(r"-?\d+\.\d+", text):
            return float(text)
    except ValueError:
        pass
    return text


def parse_assert_cell(value: Any, field: str = "断言") -> list[dict[str, Any]]:
    """解析断言。

    支持两种写法：

    **每行一条断言**（推荐）::

        equal | code | 200
        equal | success | true
        is not None | result.accessToken

    **JSON 数组**::

        [{"type": "equal", "field": "code", "expected": 200}]
    """
    if value is None or value == "":
        return []

    if isinstance(value, list):
        return value

    text = str(value).strip()

    if text.startswith("["):
        parsed = parse_json_cell(text, field)
        if not isinstance(parsed, list):
            raise ExcelCaseError(f"{field} 应该是 JSON 数组")
        return parsed

    asserts: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2:
            raise ExcelCaseError(
                f"{field} 格式不对（应该是「类型 | 字段 | 期望值」）：{line}"
            )

        assert_type = parts[0]
        field_name = parts[1]
        expected = parts[2] if len(parts) > 2 else None

        # 查库断言：第二列是 SQL，不是字段路径
        is_db = assert_type.strip().lower().startswith(("db_", "mysql_"))
        item: dict[str, Any] = (
            {"type": assert_type, "query": field_name}
            if is_db
            else {"type": assert_type, "field": field_name}
        )

        # 期望值可能是 JSON 对象（比较条件），也可能是普通值
        if expected not in (None, ""):
            item["expected"] = parse_json_cell(expected, field)
            # 数字字符串要转成数字 —— 否则 "10.005" 和 10.005 比不相等
            if isinstance(item["expected"], str):
                item["expected"] = _coerce_scalar(item["expected"])

        # 第四格：容差（approx_equal）、query 参数（db）或 container
        if len(parts) > 3 and parts[3]:
            extra = parts[3]
            assert_type_lower = assert_type.strip().lower()
            if assert_type_lower in ("approx_equal", "approximately_equal", "almost_equal"):
                item["tolerance"] = _coerce_scalar(extra)
            elif is_db and not assert_type_lower.endswith("_in"):
                # db_query | SELECT ... WHERE id = %s | 1 | [123]
                item["params"] = parse_json_cell(extra, field)
            else:
                item["container"] = parse_json_cell(extra, field)

        asserts.append(item)

    return asserts
