"""用例参数化：把一条 YAML 展开成多组数据。

两种写法：

**① 场景列表**（最常用）—— 每项一个独立场景：

.. code-block:: yaml

    parameters:
      - name: 正常管理员
        data:
          username: admin
          password: "123456"
      - name: 禁用账号
        data:
          username: disabled
          password: "123456"

**② 维度字典** —— 每个 key 是一个维度，框架自动做笛卡尔积：

.. code-block:: yaml

    parameters:
      role: [admin, operator]
      pageSize: [10, 20]

展开后每组数据都能在用例里直接引用：

.. code-block:: yaml

    data:
      username: '{{username}}'      # 当前参数组的值
      role: '{{params.role}}'       # 带前缀写法（更明确）

另外还支持：

**③ 外部数据文件** —— ``parameters: "@data/users.csv"`` 或 ``"@data/users.json"``，
按行 / 按记录读取，每个字段直接成为参数。

维度过多时会做保护：默认最多展开 200 组，超过就报错提示拆分用例。
"""
from __future__ import annotations

import csv
import itertools
import json
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from utils.logs.log_manager import log

MAX_COMBINATIONS = 200


@dataclass
class ExpandedCase:
    """展开后的一条用例。

    Attributes:
        case: 渲染好的用例数据（``{"testcase": {...}}``）
        params: 当前参数组的变量（可直接用 ``{{key}}`` 引用）
        param_name: 参数组的名字（显示在用例 ID 里）
    """

    case: dict[str, Any]
    params: dict[str, Any] = field(default_factory=dict)
    param_name: str = ""


def has_parameters(case: dict[str, Any]) -> bool:
    """判断用例有没有参数化。

    ``parameters`` 放在哪都行：
    - 顶层（和 ``testcase`` 平级）
    - ``testcase`` 里面
    """
    return bool(case.get("parameters")) or bool((case.get("testcase") or {}).get("parameters"))


def _extract_params_spec(case_data: dict[str, Any]) -> Any:
    """取出 parameters 的内容，并从原数据里摘掉（避免被当成普通变量）。"""
    if "parameters" in case_data:
        return case_data.pop("parameters")
    testcase = case_data.get("testcase")
    if isinstance(testcase, dict) and "parameters" in testcase:
        return testcase.pop("parameters")
    return None


def expand_case(case_data: dict[str, Any], yaml_file: Path | None = None) -> list[ExpandedCase]:
    """把一条带参数的用例展开成多条。

    Args:
        case_data: 原始 YAML 数据
        yaml_file: 用例文件路径（外部数据文件要靠它定位相对路径）

    Returns:
        展开后的用例列表。没有参数时返回一条（参数为空）。
    """
    # 深拷贝，避免把传入的数据改坏（pop 会影响原对象）
    case_data = deepcopy(case_data)
    params_spec = _extract_params_spec(case_data)

    if params_spec is None:
        return [ExpandedCase(case=deepcopy(case_data), params={}, param_name="")]

    if isinstance(params_spec, str):
        groups = _load_from_file(params_spec, yaml_file)
    elif isinstance(params_spec, list):
        groups = _normalize_list(params_spec)
    elif isinstance(params_spec, dict):
        groups = _expand_dimensions(params_spec)
    else:
        raise ValueError(
            f"parameters 写法不支持: {type(params_spec).__name__}"
            "（应该是列表、维度字典，或 '@文件路径'）"
        )

    if not groups:
        raise ValueError("parameters 展开后没有任何参数组")

    results: list[ExpandedCase] = []
    for index, (name, values) in enumerate(groups):
        new_case = deepcopy(case_data)
        # 参数组本身也可以引用上一层的公共变量
        new_case["parameters"] = values
        # 把参数组名字也放进变量里，用例里可以 {{__param_name__}} 引用
        values = {**values, "__param_name__": name}
        results.append(
            ExpandedCase(
                case=new_case,
                params=values,
                param_name=name or f"#{index}",
            )
        )

    log.info("用例 %s 展开为 %s 组参数", case_data.get("testcase", {}).get("name"), len(results))
    return results


# ---------------- 各种写法的处理 ----------------


def _normalize_list(params_spec: list[Any]) -> list[tuple[str, dict[str, Any]]]:
    """场景列表 → [(名字, 变量字典)]。"""
    groups: list[tuple[str, dict[str, Any]]] = []

    for index, item in enumerate(params_spec):
        if not isinstance(item, dict):
            raise ValueError(f"parameters 第 {index + 1} 项必须是字典")

        # 允许两种写法：
        #   - name: 场景名
        #     data: {...}
        #   - 直接写变量：{username: admin}
        if "data" in item and isinstance(item["data"], dict):
            name = str(item.get("name") or f"#{index}")
            values = dict(item["data"])
            # name 之外的其他顶层 key 也算变量
            for key, value in item.items():
                if key not in ("name", "data"):
                    values.setdefault(key, value)
        else:
            name = str(item.get("name") or f"#{index}")
            values = {k: v for k, v in item.items() if k != "name"}

        if not values:
            raise ValueError(f"parameters 第 {index + 1} 项没有任何变量")
        groups.append((name, values))

    return groups


def _expand_dimensions(params_spec: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """维度字典 → 笛卡尔积。"""
    if not params_spec:
        raise ValueError("parameters 维度字典不能为空")

    keys = list(params_spec.keys())
    value_lists: list[list[Any]] = []
    for key in keys:
        value = params_spec[key]
        if not isinstance(value, list):
            value = [value]
        if not value:
            raise ValueError(f"parameters.{key} 的候选值不能为空")
        value_lists.append(value)

    total = 1
    for values in value_lists:
        total *= len(values)

    if total > MAX_COMBINATIONS:
        raise ValueError(
            f"parameters 笛卡尔积会展开 {total} 组，超过上限 {MAX_COMBINATIONS}。"
            "请拆成多个用例，或改用场景列表写法。"
        )

    groups: list[tuple[str, dict[str, Any]]] = []
    for combo in itertools.product(*value_lists):
        values = dict(zip(keys, combo))
        name = "-".join(f"{k}={v}" for k, v in values.items())
        groups.append((name, values))

    return groups


def _load_from_file(spec: str, yaml_file: Path | None) -> list[tuple[str, dict[str, Any]]]:
    """从 CSV / JSON 文件读参数。"""
    raw = spec.lstrip("@").strip()
    if not raw:
        raise ValueError("parameters 文件路径为空")

    path = Path(raw)
    if not path.is_absolute() and yaml_file is not None:
        path = (yaml_file.parent / path).resolve()
    if not path.exists():
        raise ValueError(f"parameters 文件不存在: {path}")

    groups: list[tuple[str, dict[str, Any]]] = []

    if path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                values = {k: v for k, v in row.items() if k}
                name = str(values.pop("name", "") or f"#{len(groups)}")
                groups.append((name, values))

    elif path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            raise ValueError(f"parameters JSON 文件 {path} 顶层应该是数组或对象")
        for item in data:
            if not isinstance(item, dict):
                raise ValueError(f"parameters JSON 文件 {path} 里每条记录必须是对象")
            values = dict(item)
            name = str(values.pop("name", "") or f"#{len(groups)}")
            groups.append((name, values))

    else:
        raise ValueError(f"parameters 文件只支持 .csv / .json，当前: {path.suffix}")

    if not groups:
        raise ValueError(f"parameters 文件 {path} 里没有读到数据")
    return groups
