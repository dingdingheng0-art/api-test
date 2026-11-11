"""写探针足迹 → 清理计划（**只生成计划，不发任何请求**）。

写探针 ``--include-write`` 造的数据不会自动删除；探针跑完会留下
``*_footprints.json``（见 :mod:`ai.pipeline.probe`）。这个模块把足迹
翻译成「要发哪些 DELETE、每条对应哪次探针」，但它**不执行**：

- 删除不可逆，执行前必须有人看过计划
- 「删的就是本次造的那条」这件事，得先在测试环境把「造 → 认 id → 删除」
  整条链路跑通才敢自动化

用法::

    # 只看计划
    python -m ai.pipeline.probe_cleanup \\
      --footprints .cache/ai_compare/probe_area_footprints.json

    # 计划落盘给别的脚本/人复核
    python -m ai.pipeline.probe_cleanup --footprints ... --output plan.json --json
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

#: 删除路径里的路径参数，如 ``/admin/area/{id}`` 里的 ``id``
PLACEHOLDER = re.compile(r"\{([^{}]+)\}")

#: id 值只允许这些字符 —— 拒绝 ``/`` ``?`` ``#`` 之类，
#: 免得一个被污染的 id 把删除请求拼到别的路径上去
SAFE_ID = re.compile(r"[A-Za-z0-9_\-]+")

_MISSING = object()


def split_target(text: str) -> tuple[str, str]:
    """把 ``"DELETE /admin/area/{id}"`` 拆成 ``("DELETE", "/admin/area/{id}")``。"""
    method, _, path = str(text or "").strip().partition(" ")
    return method.upper(), path.strip()


def _entity_of(api: str) -> str:
    """从 ``"POST /admin/area"`` 取资源名 ``"area"``。

    用于 ``{id}`` 这种通用占位符：响应里同时有 ``areaId`` 和 ``parentId``
    时，得知道这次操作的是哪个资源才敢挑。取路径最后一段（跳过 ``{id}``
    这类参数段）；取不到就返回空串，调用方按「对不上」跳过。
    """
    _, path = split_target(api)
    segments = [
        seg for seg in path.strip("/").split("/") if seg and not PLACEHOLDER.search(seg)
    ]
    return segments[-1] if segments else ""


def _pick_id(ids: dict[str, Any], name: str, entity: str = "") -> Any:
    """给一个路径参数名找对应的 id。

    顺序：

    1. 名字完全对上（忽略大小写）：``{areaId}`` ← ``areaId``
    2. 通用 ``{id}`` 且知道资源名：``/admin/area`` ← ``areaId`` / ``area_id``
    3. 只有一个 id：就是它
    4. 其它情况**不猜**，让调用方跳过
    """
    for key, value in ids.items():
        if str(key).lower() == str(name).lower():
            return value
    if entity and str(name).lower() == "id":
        candidates = {entity.lower() + "id", entity.lower() + "_id"}
        for key, value in ids.items():
            if str(key).lower() in candidates:
                return value
    if len(ids) == 1:
        return next(iter(ids.values()))
    return _MISSING


def resolve_delete_path(
    template: str, ids: dict[str, Any], entity: str = ""
) -> tuple[str, str]:
    """把删除接口模板和响应 id 拼成具体路径。

    Args:
        entity: 资源名（``area``），只在通用 ``{id}`` 上用来挑对应字段。

    Returns:
        ``(路径, 跳过原因)``。能删时原因为空串；不能删时路径为空串。
    """
    method, path = split_target(template)
    if method != "DELETE":
        return "", f"清理接口不是 DELETE（{template or '空'}）"
    if not path:
        return "", "缺少删除接口"
    if not isinstance(ids, dict) or not ids:
        return "", "响应里没有 id，定位不到具体那条数据"

    names = PLACEHOLDER.findall(path)
    if not names:
        # 没有路径参数 = 删的不止一条，谁也不知道会删掉什么
        return "", "删除接口没有路径参数，删的是哪条说不清"

    resolved = path
    for name in names:
        value = _pick_id(ids, name, entity)
        if value is _MISSING:
            return "", f"响应里有多个 id（{', '.join(map(str, ids))}），对不上 {{{name}}}"
        # str(None) 是 "None"、str(True) 是 "True"，都能通过字符白名单 ——
        # 光靠正则会把它们拼进删除路径，先按类型挡掉
        if value is None or isinstance(value, bool):
            return "", f"id 值不是有效主键（{value!r}），不拼删除路径"
        text = str(value).strip()
        if not SAFE_ID.fullmatch(text):
            return "", f"id 值不像合法主键（{text!r}），不拼删除路径"
        resolved = resolved.replace("{" + name + "}", text)
    return resolved, ""


def plan_cleanup(footprints: list[dict[str, Any]]) -> dict[str, Any]:
    """把足迹清单翻译成清理计划（不含任何网络动作）。"""
    planned: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for item in footprints:
        ids = item.get("ids")
        path, reason = resolve_delete_path(
            str(item.get("清理接口") or ""),
            ids if isinstance(ids, dict) else {},
            entity=_entity_of(str(item.get("接口") or "")),
        )
        if reason:
            skipped.append({**item, "跳过原因": reason})
            continue
        planned.append(
            {
                "method": "DELETE",
                "path": path,
                "模板": str(item.get("清理接口") or ""),
                "接口": str(item.get("接口") or ""),
                "测点": str(item.get("测点") or ""),
                "请求": item.get("请求") or {},
            }
        )
    return {"total": len(footprints), "planned": planned, "skipped": skipped}


def load_footprints(path: str | Path) -> list[dict[str, Any]]:
    """读 ``*_footprints.json``。格式不对就抛 ``ValueError``。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ValueError("足迹文件格式不对：需要 items 列表")
    return [item for item in items if isinstance(item, dict)]


def _describe(plan: dict[str, Any]) -> str:
    lines = [
        f"清理计划：共 {plan['total']} 条足迹，"
        f"可生成 {len(plan['planned'])} 个 DELETE，跳过 {len(plan['skipped'])} 条",
        "⚠️ 本工具只列计划，不会发送任何请求。",
    ]
    if plan["planned"]:
        lines.append("")
        lines.append("可删除：")
        for idx, item in enumerate(plan["planned"], start=1):
            lines.append(
                f"  [{idx}] DELETE {item['path']}"
                f"  ← {item['接口']}（{item['测点'] or '未标注'}）"
            )
    if plan["skipped"]:
        lines.append("")
        lines.append("跳过（需要人工核对）：")
        for idx, item in enumerate(plan["skipped"], start=1):
            lines.append(
                f"  [{idx}] {item.get('接口') or '未知接口'}"
                f"（{item.get('测点') or '未标注'}）：{item['跳过原因']}"
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="把写探针的足迹翻成 DELETE 清理计划（只列计划，不发请求）"
    )
    parser.add_argument("--footprints", required=True, help="probe 生成的 *_footprints.json")
    parser.add_argument("--output", default="", help="计划写文件（UTF-8 JSON）")
    parser.add_argument("--json", action="store_true", help="stdout 只输出 JSON")
    args = parser.parse_args(argv)

    try:
        footprints = load_footprints(args.footprints)
    except (OSError, ValueError) as exc:
        print(f"读不了足迹文件：{exc}")
        return 2

    plan = plan_cleanup(footprints)
    text = json.dumps(plan, ensure_ascii=False, indent=2)

    if args.output:
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        print(f"清理计划写入 {target}")

    print(text if args.json else _describe(plan))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
