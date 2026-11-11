"""测试测点规划器。

**职责**：按 `apitest-design` 的维度矩阵，从接口元数据/知识图谱/项目配置里
确定性地枚举「这个接口该测哪些维度」。生成器只负责把每个测点填成具体请求和断言。

**为什么不让模型决定测什么**：模型天然偏向正例。把「测什么」交给模型，
结果就是一套通过率很高、但只覆盖 happy path 的用例。测点必须由规则枚举，
模型只在给定测点内发挥。

命令行自查::

    python -m ai.pipeline.intents --file .cache/apidoc/openapi.json --only-path /admin/area
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tools.importer.models import Endpoint

#: 不需要登录的接口关键字（登录/验证码/刷新令牌）
PUBLIC_PATH_HINTS = ("login", "token", "captcha", "logout")

#: 分页参数常见命名
PAGINATION_NAMES = {
    "page", "current", "pageNum", "pageNo", "pageIndex",
    "size", "limit", "pageSize", "page_size",
}

#: 状态字段常见命名
STATE_NAMES = {"status", "enabled", "state", "auditStatus", "isDelete", "is_deleted"}

#: 创建/修改删除类方法
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

#: 维度 ID 全集（与 apitest-design / case_rules.yaml 对齐）
DIMENSION_IDS = {
    "P0",
    "A1", "A2", "A3", "A4", "A5", "A6", "A7",
    "B1", "B2", "B3", "B4",
    "C1", "C2", "C3",
    "D1", "D2", "D3", "D4",
    "E1", "E2", "E3",
    "F1", "F2", "F3", "F4",
}

#: 维度标记前缀。
#:
#: **为什么不能直接用裸 ID**：用例标签里已经有优先级 ``p0``/``p1``，
#: 和维度 ``P0`` 撞名。大写化之后分不清「这条是正例维度」还是
#: 「这条优先级是 P0 」。前缀化之后两者各归各的。
DIMENSION_MARK_PREFIX = "dim:"


def dimension_ids_of(marks: Any) -> set[str]:
    """从用例标签里取维度 ID。

    - ``dim:A1`` → ``A1``（新写法）
    - 全大写裸 ID（如 ``A1``）→ 兼容老产物
    - ``p0`` / ``P1`` 这类优先级标签不算维度
    """
    # 用例来源可能是 Excel / YAML / 模型产物：``marks`` 写成
    # ``"dim:C1,p0"`` 或对象时不能逐字符展开，否则维度会全部漏掉。
    if isinstance(marks, str):
        marks = marks.replace(";", ",").split(",")
    elif isinstance(marks, dict):
        marks = list(marks.keys())
    elif not isinstance(marks, (list, tuple, set, frozenset)):
        return set()

    found: set[str] = set()
    for raw in marks:
        text = str(raw).strip()
        if text.lower().startswith(DIMENSION_MARK_PREFIX):
            dim = text[len(DIMENSION_MARK_PREFIX):].strip().upper()
            if dim in DIMENSION_IDS:
                found.add(dim)
        elif text == text.upper() and text in DIMENSION_IDS:
            found.add(text)
    return found


@dataclass
class TestIntent:
    """一个测试测点（不是用例，是"要测什么"）。"""

    dimension: str                  # A1 / B1 / C1 ...
    label: str                      # 必填缺失
    field: str = ""                 # 作用在哪个字段
    evidence: str = "metadata"      # metadata / knowledge / existing_case / project_config / assumption
    priority: str = "P1"
    expected: str = "rejected"      # success / rejected / boundary / unauthorized / db_check
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = {
            "维度": self.dimension,
            "测点": self.label,
            "期望": self.expected,
            "依据": self.evidence,
            "优先级": self.priority,
        }
        if self.field:
            data["字段"] = self.field
        if self.note:
            data["说明"] = self.note
        return data


def _all_params(ep: Endpoint) -> list[Any]:
    return list(ep.path_params) + list(ep.query) + list(ep.body_params)


def _required_params(ep: Endpoint) -> list[Any]:
    return [p for p in ep.query + ep.body_params if p.required]


def _requires_auth(ep: Endpoint) -> bool:
    path = str(ep.path or "").lower()
    return not any(hint in path for hint in PUBLIC_PATH_HINTS)


def _confidence_at_least(mapping: dict[str, Any], threshold: float) -> bool:
    """映射置信度是否达标。低置信映射不许用来写落库断言。"""
    try:
        return float(mapping.get("confidence")) >= threshold
    except (TypeError, ValueError):
        return False


def plan_endpoint_intents(
    ep: Endpoint,
    max_intents: int = 8,
    mappings: list[dict[str, Any]] | None = None,
) -> list[TestIntent]:
    """为一个接口枚举测点。

    Args:
        ep: 接口
        max_intents: 最多返回多少个测点（含正例）
        mappings: 知识图谱字段映射，用于 D 类测点

    Returns:
        按优先级排序的测点列表
    """
    intents: list[TestIntent] = [
        TestIntent(
            dimension="P0",
            label="正例",
            evidence="existing_case",
            priority="P0",
            expected="success",
        )
    ]

    # ---------- A 参数契约 ----------
    required = _required_params(ep)
    for param in required[:2]:
        intents.append(
            TestIntent("A1", "必填缺失", param.name, "metadata", "P1", "rejected")
        )

    # 路径参数优先：``/info/{id}`` 传非数字是最有意义的类型测点，
    # 也最容易实测（其它参数经常被服务端忽略）。
    typed = [
        p for p in ep.path_params if p.type in {"integer", "number"}
    ] + [
        p
        for p in _all_params(ep)
        if p.type in {"integer", "number", "boolean", "array", "object"}
        and p not in ep.path_params
    ]
    if typed:
        intents.append(
            TestIntent("A2", "类型错误", typed[0].name, "metadata", "P1", "rejected")
        )

    enum_fields = [p for p in _all_params(ep) if getattr(p, "enum", None)]
    if enum_fields:
        intents.append(
            TestIntent(
                "A3", "枚举非法", enum_fields[0].name, "metadata", "P1", "rejected"
            )
        )

    string_required = [
        p for p in required if p.type == "string" and p.location in {"body", "query"}
    ]
    if string_required:
        intents.append(
            TestIntent(
                "A6", "空值", string_required[0].name, "metadata", "P2", "rejected"
            )
        )

    # ---------- B 业务语义 ----------
    pagination = [p for p in ep.query if p.name in PAGINATION_NAMES]
    if pagination:
        target = pagination[0]
        intents.append(
            TestIntent(
                "B1",
                "分页边界",
                target.name,
                "metadata",
                "P1",
                "boundary",
                note=f"把 {target.name} 设为 0 / 超上限，确认服务端行为",
            )
        )

    state_fields = [p for p in ep.body_params if p.name in STATE_NAMES]
    if state_fields and ep.method.upper() in {"PUT", "PATCH"}:
        intents.append(
            TestIntent(
                "B4",
                "状态迁移",
                state_fields[0].name,
                "knowledge",
                "P2",
                "boundary",
                note="合法/非法状态值各一条",
            )
        )

    # ---------- C 权限 ----------
    if _requires_auth(ep):
        intents.append(
            TestIntent(
                "C1", "未登录", evidence="project_config", priority="P0",
                expected="unauthorized",
            )
        )

    # ---------- D 数据一致性 ----------
    trusted = [m for m in (mappings or []) if _confidence_at_least(m, 0.9)]
    if trusted:
        if ep.method.upper() == "GET":
            intents.append(
                TestIntent(
                    "D1", "响应字段 vs 库", trusted[0].get("field", ""),
                    "knowledge", "P1", "db_check",
                )
            )
        elif ep.method.upper() in {"POST", "PUT", "PATCH"}:
            intents.append(
                TestIntent(
                    "D2", "写后能查到", trusted[0].get("field", ""),
                    "knowledge", "P1", "db_check",
                )
            )
        elif ep.method.upper() == "DELETE":
            intents.append(
                TestIntent(
                    "D3", "删后查不到", trusted[0].get("field", ""),
                    "knowledge", "P1", "db_check",
                )
            )

    # ---------- E 接口关联 ----------
    if ep.method.upper() in WRITE_METHODS:
        intents.append(
            TestIntent(
                "E3", "清理隔离", evidence="metadata", priority="P0",
                expected="cleanup_only_own_data",
            )
        )

    # ---------- F 异常健壮性 ----------
    id_params = [
        p for p in ep.path_params
        if p.name.lower() in {"id", "areaid", "categoryid", "prodid"} or p.type == "integer"
    ]
    if id_params:
        intents.append(
            TestIntent(
                "F1", "不存在的 id", id_params[0].name,
                "metadata", "P2", "business_failure_or_empty",
            )
        )

    # 去重 + 按优先级排序 + 截断
    seen: set[tuple[str, str]] = set()
    unique: list[TestIntent] = []
    for intent in intents:
        key = (intent.dimension, intent.field)
        if key in seen:
            continue
        seen.add(key)
        unique.append(intent)

    order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    unique.sort(key=lambda i: order.get(i.priority, 9))
    return unique[:max_intents]


def plan_all(
    endpoints: list[Endpoint],
    max_intents: int = 8,
    mappings_by_key: dict[tuple[str, str], list[dict[str, Any]]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """给一批接口出测点清单（key = ``METHOD path``）。"""
    result: dict[str, list[dict[str, Any]]] = {}
    for ep in endpoints:
        key = (ep.method.upper(), ep.path)
        intents = plan_endpoint_intents(
            ep, max_intents=max_intents, mappings=(mappings_by_key or {}).get(key)
        )
        result[f"{ep.method.upper()} {ep.path}"] = [i.to_dict() for i in intents]
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按维度矩阵列出接口测点")
    parser.add_argument("--file", required=True, help="接口文档文件")
    parser.add_argument("--only-path", default="", help="只看路径包含该子串的接口")
    parser.add_argument("--max-intents", type=int, default=8)
    args = parser.parse_args(argv)

    from tools.importer.dispatcher import parse_file

    endpoints = parse_file(args.file)
    if args.only_path:
        endpoints = [ep for ep in endpoints if args.only_path in ep.path]

    result = plan_all(endpoints, max_intents=args.max_intents)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
