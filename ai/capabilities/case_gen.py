"""AI 用例生成 —— 接口文档 → 测试用例。

**为什么不能只把 OpenAPI 丢给模型**

直接从 Swagger 生成用例最容易踩的坑是「模型不知道这个项目的契约」：

- 响应成功码是 ``00000`` 还是 ``200``？
- 业务失败是 HTTP 非 200，还是 HTTP 200 + ``success=false``？
- 写接口的数据从哪来、用完谁来清？
- 路径参数 ``{id}`` 该换成哪个真实 id？

这些信息 OpenAPI 里都没有（快照里的示例值通常是 ``"string"`` / ``0``）。
所以本模块在调模型之前会先做 **接地（grounding）**：

1. 扫现役用例，统计出项目的响应契约（成功 code / status_code / success）
2. 从知识图谱拿「接口 → 字段 → 表.列」映射
3. 从同模块现役用例拿风格样例和推荐路径参数值

生成后再做一层 **确定性收敛**：补 ``status_code``、纠正 ``code==200``
这类错误期望、校验路径参数、给写操作标 ``needs_manual_setup``。

**两条路**：

1. **接了 LLM**（配了 ``ai.base_url`` / ``api_key`` / ``model``）：
   按模块分批，让模型生成正例 + 反例 + 边界值。
2. **没接 LLM**（本地模式）：
   按参数类型用模板规则造用例 —— 必填缺失、类型错误、空值、超长、
   非法枚举。**不联网也能出用例**。

**落盘**：

- 默认 ``--format excel``：走 ``create_case`` 写进现役 Excel 用例目录，
  自动拿 ``API-xxx`` 编号、默认 ``draft``（CI 不跑）、带项目鉴权头。
- ``--format yaml``：写 YAML，保留给不太依赖 Excel 的项目。

用法::

    python -m ai.capabilities.case_gen --file 接口文档.json --project mall4j
    python -m ai.capabilities.case_gen --file 接口文档.json --mode local   # 强制本地模式
    python -m ai.capabilities.case_gen --file 接口文档.json --only-path /admin/area
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ai.pipeline.data_orchestrator import (
    attach_chain,
    unsafe_teardowns,
    write_is_self_cleaning,
)
from ai.pipeline.intents import DIMENSION_MARK_PREFIX, dimension_ids_of, plan_endpoint_intents
from tools.importer.case_builder import build_testcase
from tools.importer.models import Endpoint, sanitize_name
from utils.logs.log_manager import log

# 生成用例时加的标签，方便筛出来人工审阅
AI_MARK = "ai_generated"
#: 模型失败、退回本地模板时额外打的标 —— 事后能分清哪些不是真 AI 产物
FALLBACK_MARK = "ai_fallback"
DEFAULT_MODULE = "ai_generated"

#: 一次 prompt 里最多放几个接口。
#: 太大 → 模型输出会被 max_tokens 截断（JSON 不完整），反而要兜底。
MAX_ENDPOINTS_PER_CALL = 2

#: 写操作 —— 这些方法的用例必须有清理，否则会污染环境
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

#: 主步骤里不算「正例」的标签
NEGATIVE_MARKS = {"negative", "boundary", "invalid"}

_PLACEHOLDER_RE = re.compile(r"\{\{[^}]+\}\}|\{[^}]+\}")
_NUMERIC_SEGMENT_RE = re.compile(r"/\d+(?=/|$)")
#: 只匹配**单**花括号占位符（路径参数 ``{id}``），不碰框架变量 ``{{...}}``
_SINGLE_BRACE_RE = re.compile(r"(?<!\{)\{[A-Za-z_][A-Za-z0-9_]*\}(?!\})")

#: 现役用例里不可信的路径值：负例/边界用例常用这些「查不到」的 id，
#: 拿来当正例的路径参数会让所有生成用例查一个不存在的记录。
_UNTRUSTED_PATH_VALUES = {"", "0", "-1", "999999", "999999999", "null", "none", "undefined"}
#: 名字里带这些词的现役用例是反例
_NEGATIVE_NAME_HINTS = (
    "不存在", "查不到", "未登录", "缺少", "必填", "非法", "空值", "边界", "错误",
)


# ============================================================================
# 项目上下文（接地）
# ============================================================================


@dataclass
class ProjectContext:
    """生成用例前先摸清「这个项目是怎么写用例的」。"""

    project: str = "mall4j"

    #: (METHOD, 规范化路径) → 现役模块名
    module_by_endpoint: dict[tuple[str, str], str] = field(default_factory=dict)
    #: (METHOD, 规范化路径) → 现役用例样例
    examples_by_endpoint: dict[tuple[str, str], list[dict[str, Any]]] = field(
        default_factory=dict
    )
    #: 模块 → 现役用例摘要（给模型看风格）
    cases_by_module: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    #: 项目响应契约：success_code / http_status / success_field
    contract: dict[str, Any] = field(default_factory=dict)
    #: 显式指定的模块（覆盖推断）
    module_override: str = ""
    #: (METHOD, path) → 字段落库映射（懒加载）
    _db_mappings: dict[tuple[str, str], list[dict[str, Any]]] = field(
        default_factory=dict
    )

    def module_for(self, ep: Endpoint) -> str:
        if self.module_override:
            return self.module_override
        key = (ep.method.upper(), _norm_path(ep.path))
        return self.module_by_endpoint.get(key) or infer_case_module(ep.path, ep.tags)

    def examples_for(self, ep: Endpoint, limit: int = 2) -> list[dict[str, Any]]:
        key = (ep.method.upper(), _norm_path(ep.path))
        return self.examples_by_endpoint.get(key, [])[:limit]

    def module_examples(self, module: str, limit: int = 3) -> list[dict[str, Any]]:
        return self.cases_by_module.get(module, [])[:limit]

    def path_values_for(self, ep: Endpoint) -> dict[str, str]:
        """从现役用例里抠出这个接口路径参数用过的真实值。

        **只能取正例的值**：现役 Excel 里正例（``/info/110000000000``）后面
        往往紧跟一条「查不到数据」（``/info/999999999``）。如果按行序全量
        ``update``，后者会覆盖前者，探针和生成用例全部去查一个不存在的 id，
        正例变成假绿、F1 和 P0 也变成同一个请求。
        """
        values: dict[str, str] = {}
        for example in self.examples_for(ep, limit=8):
            if not _example_is_trustworthy(example):
                continue
            concrete = str(example.get("path") or "")
            for name, value in _extract_path_values(ep.path, concrete).items():
                if str(value).strip().lower() in _UNTRUSTED_PATH_VALUES:
                    continue
                values.setdefault(name, value)
        if not values:
            for param in ep.path_params:
                example = "" if param.example is None else str(param.example).strip()
                if example.lower() not in _UNTRUSTED_PATH_VALUES:
                    values[param.name] = example
        return values

    def db_mappings(self, ep: Endpoint) -> list[dict[str, Any]]:
        # 读接口也要映射：D1「响应字段 vs 库」就靠它。
        key = (ep.method.upper(), ep.path)
        if key in self._db_mappings:
            return self._db_mappings[key]
        mappings: list[dict[str, Any]] = []
        try:
            from ai.knowledge.retriever import explain_endpoint

            data = explain_endpoint(ep.path, ep.method)
            for item in data.get("endpoints") or []:
                for mapping in item.get("mappings") or []:
                    mappings.append(
                        {
                            "field": mapping.get("field"),
                            "table": mapping.get("table"),
                            "column": mapping.get("column"),
                            "confidence": mapping.get("confidence"),
                        }
                    )
        except Exception as exc:  # noqa: BLE001 - 图谱没建/没装就跳过
            log.debug("取字段映射失败（不影响生成）: %s", exc)
        self._db_mappings[key] = mappings
        return mappings


def _norm_path(path: Any) -> str:
    """把 ``/admin/area/{id}``、``/admin/area/110000000000`` 统一成 ``/admin/area/{}``。

    这样现役用例里的具体 id 才能和 OpenAPI 里的路径模板对上。
    """
    text = _PLACEHOLDER_RE.sub("{}", str(path or "").strip())
    return _NUMERIC_SEGMENT_RE.sub("/{}", text)


def _example_is_trustworthy(example: dict[str, Any]) -> bool:
    """这条现役用例能不能作为路径参数取值的来源。

    反例/边界用例的路径值是「故意不存在的 id」，拿它当正例种子会把
    整个模块的生成用例带偏。
    """
    if not isinstance(example, dict):
        return False
    marks = _as_marks(example.get("marks"))
    lowered = {m.lower() for m in marks}
    if lowered & (NEGATIVE_MARKS | {"permission", "no_auth", "needs_manual_setup"}):
        return False
    if dimension_ids_of(marks) & (_NEGATIVE_DIMS | {"B1", "B4"}):
        return False
    name = str(example.get("case") or "")
    return not any(hint in name for hint in _NEGATIVE_NAME_HINTS)


def _extract_path_values(template: Any, concrete: Any) -> dict[str, str]:
    """``/info/{id}`` + ``/info/110000000000`` → ``{"id": "110000000000"}``。"""
    t_parts = str(template or "").strip("/").split("/")
    c_parts = str(concrete or "").strip("/").split("/")
    if not t_parts or len(t_parts) != len(c_parts):
        return {}
    values: dict[str, str] = {}
    for t_part, c_part in zip(t_parts, c_parts):
        if t_part.startswith("{") and t_part.endswith("}") and c_part:
            values[t_part.strip("{}")] = c_part
    return values


def infer_case_module(path: Any, tags: list[str] | None = None) -> str:
    """从路径推断现役用例的模块名。

    现役模块的命名规则是 ``<一级路径>_<二级路径>``：

    - ``/admin/area/page``   → ``admin_area``
    - ``/prod/prodTag/list`` → ``prod_prodTag``
    - ``/sys/menu/list``     → ``sys_menu``

    对不上时退回 OpenAPI 的 tag（``area-controller`` → ``area``）。
    """
    segments = [
        s for s in str(path or "").split("/")
        if s and not s.startswith("{")
    ]
    if len(segments) >= 2 and segments[0] in {
        "admin", "sys", "prod", "shop", "user", "order", "auth", "pay",
    }:
        return sanitize_name(f"{segments[0]}_{segments[1]}", fallback=DEFAULT_MODULE)
    if tags:
        tag = str(tags[0]).replace("-controller", "").strip()
        if tag:
            return sanitize_name(tag, fallback=DEFAULT_MODULE)
    if segments:
        return sanitize_name(segments[0], fallback=DEFAULT_MODULE)
    return DEFAULT_MODULE


def _as_list(value: Any) -> list[Any]:
    """把「应该是一组」的值收敛成 list。

    模型 / 外部 json 常把单条写成字典、把数组写成字符串；直接切片或迭代
    会抛 TypeError（dict 不可切片）或逐字符拆（str）。统一在这里兜住。
    """
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, dict):
        return [value]
    return []


def _compact_asserts(asserts: Any, limit: int = 6) -> list[dict[str, Any]]:
    """只保留断言里最有用的字段，别把整坨数据塞给模型。"""
    out: list[dict[str, Any]] = []
    for item in _as_list(asserts)[:limit]:
        if not isinstance(item, dict):
            continue
        compact = {
            key: item[key]
            for key in ("type", "field", "expected", "container", "query", "alias")
            if key in item
        }
        if compact.get("type"):
            out.append(compact)
    return out


def _compact_case(ref: Any, case: Any) -> dict[str, Any]:
    """一条现役用例的摘要（给模型当风格样例）。"""
    case = case if isinstance(case, dict) else {}
    steps = []
    for step in _as_list(case.get("steps"))[:2]:
        if not isinstance(step, dict):
            continue
        steps.append(
            {
                "method": step.get("method"),
                "path": step.get("path"),
                "data": step.get("data"),
                "params": step.get("params"),
                "assert": _compact_asserts(step.get("assert")),
            }
        )
    # ``ref`` 不一定带 marks（外部调用方可能直接传用例本体）；
    # 缺了就从用例自己身上取，别把「风格样例」的标签悄悄丢光。
    return {
        "name": case.get("name"),
        "case_id": getattr(ref, "case_id", ""),
        "marks": _as_marks(getattr(ref, "marks", None)) or _as_marks(case.get("marks")),
        "has_setups": bool(case.get("setups")),
        "has_teardowns": bool(case.get("teardowns")),
        "steps": steps,
    }


def _collect_contract_values(
    case: dict[str, Any],
    codes: Counter[str],
    statuses: Counter[str],
    successes: Counter[str],
) -> None:
    """把现役用例里的成功码 / 状态码 / success 统计出来。"""
    for step in case.get("steps") or []:
        if not isinstance(step, dict):
            continue
        for item in step.get("assert") or []:
            if not isinstance(item, dict):
                continue
            atype = str(item.get("type") or "").lower().replace(" ", "_")
            field = str(item.get("field") or "")
            expected = item.get("expected")
            if atype in ("equal", "equals", "==") and field == "code":
                codes[str(expected)] += 1
            elif atype == "status_code":
                statuses[str(expected)] += 1
            elif atype in ("equal", "equals", "==") and field == "success":
                successes[str(expected)] += 1


def _dominant(counter: Counter[str], min_count: int = 2, ratio: float = 0.6) -> str:
    """取众数；样本太少或不够集中就返回空（不猜）。"""
    if not counter:
        return ""
    value, count = counter.most_common(1)[0]
    total = sum(counter.values())
    if count < min_count or (total and count / total < ratio):
        return ""
    return value


def _contract_from_config(project: str) -> dict[str, Any]:
    """配置覆盖（可选）：``ai.case_gen.*``。"""
    try:
        from utils.config.config_manager import ConfigManager

        raw = (ConfigManager.get_all().get("ai") or {}).get("case_gen") or {}
    except Exception:  # noqa: BLE001 - 配置读不到就用默认
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    return raw


def build_context(project: str = "mall4j", endpoints: list[Endpoint] | None = None) -> ProjectContext:
    """扫现役用例 + 图谱，构造生成用例需要的上下文。"""
    ctx = ProjectContext(project=project)
    codes: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    successes: Counter[str] = Counter()

    try:
        from ai.agent.tools._common import iter_cases

        pairs = iter_cases(project=project)
    except Exception as exc:  # noqa: BLE001 - 用例还没建/来源关了
        log.warning("读取现役用例失败，生成时不带项目上下文: %s", exc)
        pairs = []

    for ref, case in pairs:
        module = str(getattr(ref, "module", "") or "")
        if not module:
            continue
        if not isinstance(case, dict):
            log.warning("现役用例不是对象（%s），跳过: %r", type(case).__name__, ref)
            continue
        compact = _compact_case(ref, case)
        ctx.cases_by_module.setdefault(module, []).append(compact)

        for step in _as_list(case.get("steps")):
            if not isinstance(step, dict):
                continue
            method = str(step.get("method") or "").upper()
            path = _norm_path(step.get("path"))
            if not method or not path:
                continue
            ctx.module_by_endpoint.setdefault((method, path), module)
            ctx.examples_by_endpoint.setdefault((method, path), []).append(
                {
                    "case": case.get("name"),
                    "case_id": getattr(ref, "case_id", ""),
                    "method": method,
                    "path": step.get("path"),
                    "data": step.get("data"),
                    "params": step.get("params"),
                    "assert": _compact_asserts(step.get("assert")),
                    "marks": _as_marks(case.get("marks")),
                    "status": str(getattr(ref, "status", "") or ""),
                    "has_teardowns": bool(case.get("teardowns")),
                }
            )

        if str(getattr(ref, "status", "")) == "debugged":
            _collect_contract_values(case, codes, statuses, successes)

    # 契约优先级：配置覆盖 > 现役用例统计 > 默认值
    override = _contract_from_config(project)
    success_code = str(
        override.get("success_code")
        or _dominant(codes)
        or ""
    ).strip()
    http_status = override.get("http_status") or _dominant(statuses) or 200
    try:
        http_status = int(http_status)
    except (TypeError, ValueError):
        http_status = 200

    ctx.contract = {
        "success_code": success_code,
        # 有的项目用 success=true/false，有的用 fail；这里只做提示
        "success_field": str(override.get("success_field") or "success"),
        "http_status": http_status,
    }

    # 只为本次要生成的接口预取图谱映射，避免把 145 个接口全查一遍
    for ep in endpoints or []:
        ctx.db_mappings(ep)

    return ctx


# ============================================================================
# 入口
# ============================================================================


def generate_cases(
    endpoints: list[Endpoint],
    project: str = "mall4j",
    module: str = "",
    use_ai: bool = True,
    ai_client: Any = None,
    max_cases_per_endpoint: int = 3,
    probe_map: Any = None,
) -> list[dict[str, Any]]:
    """给一批接口生成用例。

    Args:
        endpoints: 接口清单
        project: 项目名
        module: 模块名（留空就按现役用例/路径自动推断）
        use_ai: 是否用 LLM（False 就是纯本地模板）
        ai_client: LLM 客户端（不给就自动造一个）
        max_cases_per_endpoint: 每个接口最多生成几条用例
        probe_map: 探针实测结果（``probe.probe_endpoints`` 的返回值）。
            给了就用实测行为改写断言，没给就退回原来的「假设 + 人工确认」。

    Returns:
        用例列表（标准格式 ``{"testcase": {...}}``）
    """
    if not endpoints:
        return []

    ctx = build_context(project, endpoints)
    if module:
        ctx.module_override = sanitize_name(module, fallback=DEFAULT_MODULE)
    probe_index = _probe_index(probe_map)

    if use_ai:
        ai_cases, covered = _generate_with_ai(
            endpoints, project, ai_client, max_cases_per_endpoint, ctx, probe_index
        )
        if ai_cases:
            # 模型漏掉的接口用本地模板补上 —— 但标 ai_fallback，
            # 不冒充 AI 产物。
            fallback = [
                ep for ep in endpoints
                if (ep.method.upper(), ep.path) not in covered
            ]
            if fallback:
                log.warning("有 %d 个接口没生成出用例，退回本地模板", len(fallback))
                ai_cases.extend(
                    _generate_locally(
                        fallback,
                        project,
                        max_cases_per_endpoint,
                        ctx,
                        fallback=True,
                        probe_index=probe_index,
                        all_endpoints=endpoints,
                    )
                )
            return ai_cases
        log.warning("AI 没有生成出任何用例，整体退回本地模板")

    return _generate_locally(
        endpoints,
        project,
        max_cases_per_endpoint,
        ctx,
        fallback=use_ai,
        probe_index=probe_index,
        all_endpoints=endpoints,
    )


def _generate_with_ai(
    endpoints: list[Endpoint],
    project: str,
    ai_client: Any = None,
    max_cases_per_endpoint: int = 3,
    ctx: ProjectContext | None = None,
    probe_index: dict[tuple[str, str], dict[str, dict[str, Any]]] | None = None,
) -> tuple[list[dict[str, Any]], set[tuple[str, str]]]:
    """用 LLM 生成用例。

    Returns:
        (用例列表, 已覆盖到的 (METHOD, path) 集合)
    """
    from ai.capabilities.client import get_client

    client = ai_client or get_client()
    if not client.available:
        log.info("AI 没配置，用本地模板生成")
        return [], set()

    ctx = ctx or build_context(project, endpoints)

    # 按模块分组 —— 同一模块的接口一次发给模型，模型能看到兄弟接口，
    # 也能复用同一份风格样例。
    groups: dict[str, list[Endpoint]] = defaultdict(list)
    for ep in endpoints:
        groups[ctx.module_for(ep)].append(ep)

    cases: list[dict[str, Any]] = []
    covered: set[tuple[str, str]] = set()

    for module, module_endpoints in groups.items():
        for chunk in _chunks(module_endpoints, MAX_ENDPOINTS_PER_CALL):
            prompt = _build_module_prompt(
                module, chunk, ctx, max_cases_per_endpoint, probe_index
            )
            try:
                # LLM 偶发返回非 JSON，重试一次通常就好了。
                # 重试仍失败就跳过这个模块，最后由生成入口统一决定是否本地兜底。
                data = _call_ai_json(client, prompt, _system_prompt(ctx.contract))
            except Exception as exc:  # noqa: BLE001 - 一个模块失败不影响其他模块
                log.error("AI 生成模块 %s 失败（已重试）: %s", module, exc)
                continue

            raw_cases = _cases_from_payload(data)
            if raw_cases is None:
                log.error("AI 返回的格式不对（模块 %s 没有 cases 列表）", module)
                continue

            chunk_keys = {(ep.method.upper(), ep.path) for ep in chunk}
            for item in raw_cases:
                # 一条模型产物结构离谱（断言写成标量、路径参数类型错等）
                # 不能带走整个模块的生成结果 —— 记录下来继续处理下一条。
                try:
                    case = _normalize_ai_case(item, default_module=module)
                    if not case:
                        continue
                    case = _finalize_ai_case(
                        case, ctx, chunk, probe_index, all_endpoints=endpoints
                    )
                except Exception as exc:  # noqa: BLE001 - 单条坏用例不拖垮整批
                    log.warning("AI 返回的用例结构无法处理，已跳过: %s", exc)
                    continue
                if not case:
                    continue
                cases.append(case)
                # 只有路径真落到本批接口上，才算这个接口被覆盖了
                for step in case["testcase"].get("steps") or []:
                    method = str(step.get("method") or "").upper()
                    path = _norm_path(step.get("path"))
                    for ep in chunk:
                        if method == ep.method.upper() and path == _norm_path(ep.path):
                            covered.add((ep.method.upper(), ep.path))
            if not (covered & chunk_keys):
                log.debug("模块 %s 的这一批没有产出可用用例", module)

    return cases, covered


def _call_ai_json(
    client: Any,
    prompt: str,
    system: str,
    attempts: int = 3,
) -> Any:
    """调 LLM 要 JSON，偶发失败重试一次，并抢救被截断的回复。

    不重试的话，一次网络抖动就会让整个模块静默退回本地模板，
    生成结果看起来「有用例」，其实全是兜底。

    模型一次生成多条用例时很容易把 JSON 截断（max_tokens 或网络读断），
    所以这里不直接调 ``chat_json`` —— 先拿原始文本，解析失败时把
    已经完整的用例对象抠出来，能救几条是几条。
    """
    from ai.capabilities.client import LLMError, parse_json_response

    last: Exception | None = None
    for index in range(attempts):
        try:
            # 真实客户端同时有 chat 和 chat_json；只实现了 chat_json 的
            # 测试替身直接走 chat_json，不做截断抢救。
            if not hasattr(client, "chat"):
                return client.chat_json(prompt, system=system)

            text = client.chat(prompt, system=system, json_output=True)
            try:
                data = parse_json_response(text)
            except Exception as exc:  # noqa: BLE001 - 截断时尝试抢救
                salvaged = _salvage_truncated_cases(text)
                if salvaged:
                    log.warning(
                        "模型回复被截断，已抢救出 %d 条完整用例",
                        len(salvaged.get("cases") or []),
                    )
                    return salvaged
                raise exc

            if _cases_from_payload(data) is not None:
                return data

            # 解析成功但形状不对：模型可能先输出推理，真正的 JSON 在后面。
            # 从后往前找最后一个带 cases 列表的完整 JSON 片段。
            for candidate in reversed(_json_candidates(text)):
                if _cases_from_payload(candidate) is not None:
                    log.warning("模型回复夹带推理文本，已定位到后面含 cases 的 JSON")
                    return candidate

            # 没有任何可用形状 —— 抛出去让调用方重试/兜底，不要静默返回。
            raise LLMError("模型回复里没有可用的 cases 列表")
        except Exception as exc:  # noqa: BLE001 - 统一在调用处决定是否兜底
            last = exc
            log.warning("AI 调用失败（第 %d/%d 次）: %s", index + 1, attempts, exc)
            if index + 1 < attempts:
                time.sleep(1.5)  # SSL 抖动时给服务端一点恢复时间
    raise last if last else RuntimeError("AI 调用失败")


def _json_candidates(text: Any) -> list[Any]:
    """按出现顺序扫描文本里所有完整的顶层 JSON 对象/数组。

    模型偶尔一边推理一边输出（``json_output`` 对部分 OpenAI 兼容网关
    不生效），回复里会夹带多个 JSON 片段；只取第一个往往拿到的是推理
    里的示例对象，而不是最后的 ``{"cases": [...]}``。
    """
    text = text if isinstance(text, str) else ("" if text is None else str(text))
    candidates: list[Any] = []
    index = 0
    length = len(text)
    while index < length:
        starts = [pos for pos in (text.find("{", index), text.find("[", index)) if pos != -1]
        if not starts:
            break
        start = min(starts)
        opener = text[start]
        closer = "}" if opener == "{" else "]"
        depth = 0
        in_string = False
        escaped = False
        closed_at: int | None = None
        for pos in range(start, length):
            char = text[pos]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == opener:
                depth += 1
            elif char == closer:
                depth -= 1
                if depth == 0:
                    closed_at = pos
                    break
        if closed_at is None:
            break
        try:
            candidates.append(json.loads(text[start : closed_at + 1]))
        except json.JSONDecodeError:
            pass
        index = closed_at + 1
    return candidates


#: 模型可能用来包用例列表的字段名（不只有 ``cases``）。
_CASE_LIST_KEYS = ("cases", "testcases", "test_cases", "items")


def _cases_from_payload(data: Any) -> list[Any] | None:
    """把模型返回的各种形状收敛成用例列表；认不出来返回 ``None``。

    兼容四种实际出现过的形状：

    - ``{"cases": [...]}``（提示词要求的格式）
    - 裸数组 ``[{...}, {...}]``
    - ``[{"cases": [...]}]``（外面又套了一层数组）
    - 单条用例对象（模型只回了一条）
    """
    if isinstance(data, list):
        if len(data) == 1 and isinstance(data[0], dict):
            keys = {str(key) for key in data[0]}
            if keys & set(_CASE_LIST_KEYS):
                nested = _cases_from_payload(data[0])
                if nested is not None:
                    return nested
        if all(isinstance(item, dict) for item in data):
            return data
        return None
    if isinstance(data, dict):
        for key in _CASE_LIST_KEYS:
            value = data.get(key)
            if isinstance(value, list):
                return value
        # 单条用例：有 steps，再加 name / testcase 任意一个就认。
        if "steps" in data and ("name" in data or "testcase" in data):
            return [data]
        if isinstance(data.get("testcase"), dict):
            return [data]
    return None


def _salvage_truncated_cases(text: Any) -> dict[str, Any] | None:
    """从被截断的 ``{"cases": [{...}, {...`` 里抠出完整用例对象。"""
    # 调用方可能拿到的不是 str（替身客户端 / 网关返回了对象）；
    # 这里只做抢救，类型不对就当没内容，别让 AttributeError 盖住真实原因。
    text = text if isinstance(text, str) else ("" if text is None else str(text))
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if "\n" in text:
            text = text.split("\n", 1)[1]

    start = text.find("[")
    if start == -1:
        return None

    items: list[Any] = []
    depth = 0
    in_string = False
    escaped = False
    obj_start: int | None = None

    for index in range(start + 1, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                obj_start = index
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0 and obj_start is not None:
                try:
                    items.append(json.loads(text[obj_start : index + 1]))
                except json.JSONDecodeError:
                    pass
                obj_start = None

    return {"cases": items} if items else None


def _chunks(items: list[Endpoint], size: int) -> list[list[Endpoint]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _generate_locally(
    endpoints: list[Endpoint],
    project: str,
    max_cases_per_endpoint: int = 3,
    ctx: ProjectContext | None = None,
    fallback: bool = False,
    probe_index: dict[tuple[str, str], dict[str, dict[str, Any]]] | None = None,
    all_endpoints: list[Endpoint] | None = None,
) -> list[dict[str, Any]]:
    """本地模板生成（不联网）。

    规则很简单但有用：

    - **正例**：用文档里的示例参数，断言成功
    - **必填缺失**：去掉一个必填参数，期望被拒绝
    - **类型错误**：把一个参数换成明显不对的类型
    - **空值**：必填参数给空字符串
    """
    ctx = ctx or ProjectContext(project=project)
    cases: list[dict[str, Any]] = []

    for ep in endpoints:
        module = ctx.module_for(ep)
        # 用例名必须用中文业务名：mall4j 的 OpenAPI 大多没有 summary，
        # display_name 会退化成「PUT /admin/area」。
        biz = _business_name(ep, ctx)
        base = build_testcase(ep, project=project)
        base["testcase"]["steps"][0]["description"] = biz
        base["testcase"]["allure"] = {
            "epic": (base["testcase"].get("allure") or {}).get("epic") or "接口文档导入",
            "feature": module,
            "story": biz,
        }
        base["testcase"]["status"] = "draft"

        marks = ["api", module, AI_MARK]
        if fallback:
            marks.append(FALLBACK_MARK)
        base["testcase"]["marks"] = list(dict.fromkeys(marks))

        # 本地模板同样不能把 {id} 原样发出去
        if not _resolve_local_step(base["testcase"]["steps"][0], ep, ctx):
            base["testcase"]["marks"] = list(
                dict.fromkeys(base["testcase"]["marks"] + ["needs_manual_setup"])
            )
        # 写操作的正例下面会尝试补 API 造数链路；补不上才标人工。
        # 反例本身在后面单独标 —— 反例可能根本没写进数据，不能无条件清。

        # ---- 正例 ----
        positive = _deep_copy(base)
        positive["testcase"]["name"] = f"{biz}（正常请求）"
        positive["testcase"]["api_name"] = biz
        positive["testcase"]["scenario"] = "（正常请求）"
        positive["testcase"]["description"] = f"{biz} —— 正常参数应成功"
        positive["testcase"]["marks"] = _marks_with(positive["testcase"]["marks"], "positive", "p1")
        positive["testcase"]["priority"] = "P1"
        _normalize_asserts(
            positive["testcase"]["steps"][0],
            positive["testcase"]["marks"],
            ctx.contract,
        )
        # 本地兜底同样要用实测事实：模板只知道「文档说会成功」，
        # 实测成功码/状态码/数据形态才是能落地的断言。
        probes = _probes_for_steps(
            probe_index or {}, positive["testcase"]["steps"]
        )
        if probes:
            _apply_probe_evidence(positive["testcase"], probes, ctx)
        # 文档响应示例里没有业务字段（或只有 code/success）时，正例就是薄用例。
        # 补不了就交给质量闸标人工，不在这里凭空编断言。
        _ensure_business_asserts(positive["testcase"], [ep])

        # 正例写操作：补「造数 → 查 id → 主步骤 → 清理」。
        # 造数/查数接口可能不在本次待生成清单里（AI 已经生成过），
        # 所以要用完整接口清单。
        if ep.method.upper() in WRITE_METHODS:
            chain_result = attach_chain(
                positive["testcase"],
                all_endpoints or endpoints,
                ctx,
                dimensions=dimension_ids_of(positive["testcase"]["marks"]),
            )
            if chain_result.startswith("ok:"):
                # 链路会重写主步骤路径（DELETE 的 {id} 换成查数表达式），
                # 之前因「路径参数取不到真实值」打的 needs_manual_setup 已经
                # 不成立了 —— 不撤掉的话这条用例会被 -m 过滤，永远跑不到。
                if not _SINGLE_BRACE_RE.search(
                    str(positive["testcase"]["steps"][0].get("path") or "")
                ):
                    positive["testcase"]["marks"] = [
                        m
                        for m in positive["testcase"]["marks"]
                        if m != "needs_manual_setup"
                    ]
                positive["testcase"]["marks"] = list(
                    dict.fromkeys(
                        list(positive["testcase"]["marks"]) + ["api_data_chain"]
                    )
                )
                _append_description(
                    positive["testcase"], f"数据构造：{chain_result[3:]}"
                )
            elif not chain_result.startswith("skipped:has_chain"):
                positive["testcase"]["marks"] = list(
                    dict.fromkeys(
                        list(positive["testcase"]["marks"])
                        + ["needs_manual_setup"]
                    )
                )
                log.warning(
                    "本地模板生成的写用例 %s 没有安全造数链路（%s），已标人工",
                    positive["testcase"].get("name"),
                    chain_result,
                )
        cases.append(positive)

        if max_cases_per_endpoint <= 1:
            continue

        required = [p for p in (ep.body_params + ep.query) if p.required]

        # ---- 必填缺失 ----
        if required:
            missing = _deep_copy(base)
            target = required[0]
            step = missing["testcase"]["steps"][0]
            location = "params" if target in ep.query else "data"
            # 文档里的 body 可能是数组（OpenAPI 允许），这时没有「字段」可删；
            # list.pop(name, None) 会抛 TypeError，兜成空对象。
            container = step.get(location)
            if not isinstance(container, dict):
                container = {}
            container.pop(target.name, None)
            if container:
                step[location] = container
            step["description"] = f"{biz} —— 缺少必填参数 {target.name} 应被拒绝"
            step["assert"] = [
                {"type": "not_equal", "field": "code", "expected": "200"},
            ]
            missing["testcase"]["name"] = f"{biz}（缺少必填参数 {target.name}）"
            missing["testcase"]["api_name"] = biz
            missing["testcase"]["scenario"] = f"（缺少必填参数 {target.name}）"
            missing["testcase"]["description"] = f"缺少必填参数 {target.name} 应被拒绝"
            missing["testcase"]["marks"] = _marks_with(
                missing["testcase"]["marks"], "negative", "p1"
            )
            # 本地模板不知道服务端是否真的校验这个参数，反例是「假设」，
            # 必须人工确认后才能进 CI —— 否则会像 area 一样整批误报。
            missing["testcase"]["marks"] = list(
                dict.fromkeys(missing["testcase"]["marks"] + ["needs_manual_setup"])
            )
            missing["testcase"]["priority"] = "P1"
            _normalize_asserts(step, missing["testcase"]["marks"], ctx.contract)
            cases.append(missing)

        if max_cases_per_endpoint <= 2:
            continue

        # ---- 空值 ----
        if required:
            empty = _deep_copy(base)
            target = required[0]
            step = empty["testcase"]["steps"][0]
            location = "params" if target in ep.query else "data"
            container = step.get(location)
            if not isinstance(container, dict):
                container = {}
            if target.name in container:
                container[target.name] = ""
                step[location] = container
                step["description"] = f"{biz} —— 必填参数 {target.name} 传空值应被拒绝"
                step["assert"] = [
                    {"type": "not_equal", "field": "code", "expected": "200"},
                ]
                empty["testcase"]["name"] = f"{biz}（{target.name} 传空值）"
                empty["testcase"]["api_name"] = biz
                empty["testcase"]["scenario"] = f"（{target.name} 传空值）"
                empty["testcase"]["description"] = f"必填参数 {target.name} 传空值应被拒绝"
                empty["testcase"]["marks"] = _marks_with(
                    empty["testcase"]["marks"], "negative", "p2"
                )
                empty["testcase"]["marks"] = list(
                    dict.fromkeys(empty["testcase"]["marks"] + ["needs_manual_setup"])
                )
                empty["testcase"]["priority"] = "P2"
                _normalize_asserts(step, empty["testcase"]["marks"], ctx.contract)
                cases.append(empty)

    return cases


def _append_description(testcase: dict[str, Any], note: str) -> None:
    """在用例说明后面追加一行（原说明为空时不要留下开头的句号）。"""
    base = str(testcase.get("description") or "").strip().rstrip("。")
    testcase["description"] = f"{base}。{note}" if base else note


_CJK_RE = re.compile(r"[一-鿿]")
#: 「业务名（场景）」——现役用例和报告标题都用这个形状
_SCENARIO_RE = re.compile(r"^(.*?)[（(]([^）)]*)[）)]\s*$")


def _clean_business_name(text: str) -> str:
    """收敛现役用例名：``地区管理 - 修改地区（正常请求）`` → ``修改地区``。"""
    value = str(text or "").strip()
    if " - " in value:
        value = value.rsplit(" - ", 1)[1].strip()
    match = _SCENARIO_RE.match(value)
    if match:
        value = match.group(1).strip()
    return value


def _business_name(ep: Endpoint, ctx: ProjectContext) -> str:
    """接口的中文业务名。

    优先级：现役用例的「接口名称」> 接口自身的 name/summary（中文）>
    ``display_name``。mall4j 的 OpenAPI 大多没有 summary，直接拼
    ``display_name`` 会得到「PUT /admin/area」这种名字。
    """
    for example in ctx.examples_for(ep, limit=8):
        candidate = _clean_business_name(example.get("case") or "")
        if candidate and _CJK_RE.search(candidate):
            return candidate
    for candidate in (getattr(ep, "name", ""), getattr(ep, "summary", "")):
        text = str(candidate or "").strip()
        if text and _CJK_RE.search(text):
            return _clean_business_name(text)
    return str(getattr(ep, "display_name", "") or f"{ep.method} {ep.path}").strip()


def _name_parts(testcase: dict[str, Any], step: dict[str, Any]) -> tuple[str, str]:
    """拆出 Excel 需要的「接口名称」和「用例标题（场景）」。

    现役 Excel 的约定是：``用例名称 = 模块中文名 - 业务功能（场景）``，
    报告标题 = ``[模块] 接口名称 - 用例标题``。AI 用例可能只给了一个
    ``业务名（场景）``，这里负责拆开；拆不出来就整段当标题。
    """
    api_name = str(testcase.get("api_name") or "").strip()
    scenario = str(testcase.get("scenario") or "").strip()
    name = str(testcase.get("name") or "").strip()
    match = _SCENARIO_RE.match(name)
    if not api_name and match and _CJK_RE.search(match.group(1)):
        api_name = match.group(1).strip()
    if not api_name:
        candidate = str(step.get("description") or "").strip()
        if candidate and _CJK_RE.search(candidate):
            api_name = _clean_business_name(candidate)
    if not scenario:
        if match:
            scenario = f"（{match.group(2)}）"
        elif name and name != api_name:
            scenario = name
    return api_name, scenario


def _display_name(text: str) -> str:
    """用例展示名：只收敛空白和控制字符，保留中文括号与空格。"""
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    return value[:80] or "ai_case"


def _module_display_name(project: str, module: str, step: dict[str, Any]) -> str:
    """模块的中文显示名，用来拼「模块中文名 - 业务功能（场景）」。

    沙箱模块（``ai_area_chain``）本身没有中文 label，先去掉 ``ai_`` 找
    底层现役模块，再按接口路径推断；都找不到才退回模块名本身。
    """
    from utils.cases.module_manager import ModuleManager

    candidates: list[str] = []
    if module.startswith("ai_"):
        candidates.append(module[3:])
    candidates.append(module)
    try:
        candidates.append(infer_case_module(step.get("path"), step.get("tags")))
    except Exception:  # noqa: BLE001 - 推断失败不影响落盘
        pass

    for name in candidates:
        if not name:
            continue
        label = str(ModuleManager.get_label(project, name) or "").strip()
        if label and _CJK_RE.search(label):
            return label

    return str(ModuleManager.get_label(project, module) or module).strip()


def _sandbox_module_label(
    project: str, module: str, step: dict[str, Any], write: bool = True
) -> str:
    """沙箱模块的展示名；没有中文 label 时补一个 module.yaml。

    报告左侧树按 ``module.yaml`` 的 label 显示模块名。沙箱模块是新建目录，
    没写 label 就会显示成 ``ai area chain`` —— 补上「地区管理（AI 沙箱）」
    之后，用例名称和报告标题都能对上现役的「模块中文名 - 业务功能（场景）」。

    ``write=False`` 只算名字不落盘，给「预览会改成什么」的工具用。
    """
    from utils.cases.module_manager import ModuleManager

    if not module.startswith("ai_"):
        return _module_display_name(project, module, step)

    label = str(ModuleManager.get_module_config(project, module).get("label") or "").strip()
    if label and _CJK_RE.search(label):
        return label

    base = _module_display_name(project, module, step)
    label = f"{base}（AI 沙箱）"
    if not write:
        return label

    from utils.cases.case_source import current_excel_dir

    module_dir = current_excel_dir() / project / module
    module_dir.mkdir(parents=True, exist_ok=True)
    module_yaml = module_dir / "module.yaml"
    if not module_yaml.exists():
        module_yaml.write_text(
            "# AI 生成的沙箱模块：用例会真的改环境数据，不要和现役模块混跑。\n"
            f"name: {module}\n"
            f"label: {label}\n"
            f"marks: [{module}, {AI_MARK}]\n",
            encoding="utf-8",
        )
        # 刚写进去的 module.yaml 还在缓存外，清一次让 get_label 立刻生效
        ModuleManager.reset()
    return label


def _full_case_name(
    project: str,
    module: str,
    step: dict[str, Any],
    api_name: str,
    scenario: str,
) -> str:
    """拼完整的「用例名称」：``地区管理 - 修改地区（正常请求）``。

    这是现役手工用例的主流格式（132 行里 129 行如此），也是 ``-k`` 搜索
    和人工审阅时最有用的名字。报告标题另由「接口名称 / 用例标题」两列
    拼成，不受这里影响。
    """
    label = _sandbox_module_label(project, module, step)
    if api_name and scenario:
        if scenario.startswith(("（", "(")):
            # 场景本来就是括号后缀：地区管理 - 修改地区（正常请求）
            core = f"{api_name}{scenario}"
        else:
            # 模型只给了整句描述，别硬拼成「删除地区删除区域成功」
            core = f"{api_name} - {scenario}"
    else:
        core = api_name or scenario
    if not core:
        return ""
    return f"{label} - {core}" if label else core


def _marks_with(marks: list[str], mark: str, priority: str) -> list[str]:
    out = [m for m in marks if m not in {"p0", "p1", "p2", "p3"}]
    out.extend([mark, priority])
    return list(dict.fromkeys(out))


# ============================================================================
# 提示词
# ============================================================================


def _system_prompt(contract: dict[str, Any]) -> str:
    """系统提示词：把项目契约讲清楚，别让模型猜。"""
    code = str(contract.get("success_code") or "").strip()
    status = contract.get("http_status") or 200
    success_field = str(contract.get("success_field") or "success")

    if code:
        success_rule = (
            f"- 成功响应：HTTP {status}，body 里 code={code!r}、{success_field}=true。\n"
            f"- **code 是字符串 {code!r}，不是 HTTP 状态码 200**；不要写 code==200。\n"
            "- 业务失败通常也是 HTTP 200，判断失败看 code/success，不要用 HTTP 非 200 判断。\n"
        )
        positive_assert = (
            f"- 正例必须断言：status_code={status}、code=={code!r}、"
            f"{success_field}==true，再从 data 里选 2~4 个关键字段做具体断言。\n"
        )
    else:
        success_rule = (
            f"- 成功响应：HTTP {status}；业务码/成功字段以接口文档和现有用例为准。\n"
        )
        positive_assert = (
            f"- 正例必须断言：status_code={status}，再断言响应里的关键业务字段。\n"
        )

    return (
        "你是一名资深接口测试工程师，为被测系统生成**可直接执行**的测试用例。\n\n"
        "## 项目契约（从现役已通过用例中提取，必须遵守）\n"
        f"{success_rule}"
        f"{positive_assert}"
        "- 反例：只断言你能从文档/现有用例确认的失败语义。没有依据时，"
        "不要凭空写 success==false —— 很多系统参数非法也返回 HTTP 200 + success=true。\n"
        "- 不要发明响应里不存在的字段。\n\n"
        "## 测试设计（按维度矩阵，不许只出正例）\n"
        "- 调用方会给每个接口列出「应测维度」。逐条生成，每个维度 1 条。\n"
        "- A 参数契约：必填缺失、类型错误、枚举非法、空值；有依据才写具体错误码。\n"
        "- B 业务语义：分页边界、状态迁移、重复提交。\n"
        "- C 权限：需要登录的接口至少一条未登录用例。\n"
        "- D 数据一致性：有高置信字段映射时，加 db_query 断言。\n"
        "- E 接口关联：写操作先造父资源，再调子接口；后置只删自有数据。\n"
        "- F 异常：不存在 id、非法 path 参数。\n"
        "- 没有依据的反例标 needs_manual_setup，不要凭空断言 success==false。\n\n"
        "## 数据生命周期（写操作必须遵守）\n"
        "- POST / PUT / PATCH / DELETE 必须提供 setups 造数、teardowns 清理。\n"
        "- teardowns 只能删本用例自己造的数据（引用 setups 产出的 id），"
        "禁止删除环境里固定 id 的数据。\n"
        "- 路径参数必须替换成具体值，禁止在 path 里保留 {id} 这种占位符。\n"
        "- 同一个接口的多条用例不要重名。\n\n"
        "## 输出格式（严格遵守）\n"
        "只输出 JSON，不要解释文字；JSON 尽量紧凑（不要缩进换行），必须完整闭合。\n"
        "维度优先：先覆盖清单里的维度；输出被截断会导致整批作废。\n"
        "```json\n"
        "{\n"
        '  "cases": [\n'
        "    {\n"
        '      "name": "用例名称：业务动作（场景），中文且唯一，例如「修改地区（正常请求）」「修改地区（缺少必填参数 areaName）」；不要写 HTTP 方法或路径",\n'
        '      "description": "一句话说明这条用例测什么",\n'
        '      "module": "模块名，例如 admin_area",\n'
        '      "dimension": "对应的测点维度 ID，例如 A1 / B1 / C1",\n'
        '      "priority": "P0/P1/P2",\n'
        '      "marks": ["positive"],\n'
        '      "steps": [\n'
        "        {\n"
        '          "id": "step1",\n'
        '          "description": "步骤说明",\n'
        '          "path": "/接口路径（路径参数已替换）",\n'
        '          "method": "GET",\n'
        '          "headers": {"键": "值"},\n'
        '          "params": {"键": "值"},\n'
        '          "data": {"键": "值"},\n'
        '          "assert": [{"type": "equal", "field": "code", "expected": "00000"}]\n'
        "        }\n"
        "      ],\n"
        '      "setups": [{"id": "建数据", "operation_type": "api", "path": "...", '
        '"method": "POST", "data": {}, "assert": []}],\n'
        '      "teardowns": [{"id": "清理", "operation_type": "api", "path": "...", '
        '"method": "DELETE", "assert": []}]\n'
        "    }\n"
        "  ]\n"
        "}\n"
        "```\n\n"
        "断言类型：equal、not_equal、is not None、is None、contains、"
        "in/not_in（集合放 container）、gt、gte、lt、lte、length、"
        "matches、approx_equal、status_code、db_query、db_query_exists、"
        "db_query_not_exists。\n"
        "落库断言里用 %s 占位时**必须同时给 params 数组**，"
        '例如 {"type": "db_query_exists", "query": "SELECT id FROM t WHERE id = %s", '
        '"params": [110000000000], "alias": "mall4j"}。\n'
    )


def _build_module_prompt(
    module: str,
    endpoints: list[Endpoint],
    ctx: ProjectContext,
    max_cases: int,
    probe_index: dict[tuple[str, str], dict[str, dict[str, Any]]] | None = None,
) -> str:
    """按模块拼提示词：契约 + 风格样例 + 接口 + 落库映射 + 探针实测。"""
    blocks: list[str] = []

    examples = ctx.module_examples(module, limit=3)
    if examples:
        blocks.append(
            "## 本模块现役用例（只作风格和契约参考，不要重复生成同名用例）\n"
            + json.dumps(examples, ensure_ascii=False, indent=2)
        )

    for index, ep in enumerate(endpoints, start=1):
        info: dict[str, Any] = {
            "序号": index,
            "接口名": ep.display_name,
            "路径": ep.path,
            "方法": ep.method,
            "说明": ep.summary or ep.description or "",
        }
        # 给模型一个现役用例里的中文业务名。不给的话它只能抄「接口名」，
        # 而 OpenAPI 里的接口名常常就是 ``PUT /admin/area`` —— 生成的
        # 用例名称于是变成「PUT /admin/area - PUT /admin/area（正常）」。
        business = _business_name(ep, ctx)
        if business and _CJK_RE.search(business):
            info["现役业务名"] = business
        if ep.path_params:
            info["路径参数"] = [
                {"名字": p.name, "类型": p.type, "必填": p.required}
                for p in ep.path_params
            ]
            values = ctx.path_values_for(ep)
            if values:
                info["推荐路径参数值"] = values
        if ep.query:
            info["Query参数"] = [
                {"名字": p.name, "类型": p.type, "必填": p.required, "说明": p.description}
                for p in ep.query
            ]
        if ep.body_params:
            info["请求体字段"] = [
                {"名字": p.name, "类型": p.type, "必填": p.required, "说明": p.description}
                for p in ep.body_params
            ]
        if ep.body:
            info["请求体示例"] = ep.body
        if ep.response_example:
            info["响应示例"] = ep.response_example
        mappings = ctx.db_mappings(ep)
        if mappings:
            info["字段落库映射"] = mappings[:10]
        intents = plan_endpoint_intents(ep, max_intents=max_cases, mappings=mappings)
        probes = (probe_index or {}).get(
            (ep.method.upper(), _norm_path(ep.path)), {}
        )
        intent_dicts: list[dict[str, Any]] = []
        for intent in intents:
            item = intent.to_dict()
            record = probes.get(intent.dimension)
            if _probe_is_conclusive(record):
                item["期望"] = (
                    "被拒绝" if record["rejected"] else "服务端接受（实测）"
                )
                item["实测"] = record["behavior"]
            intent_dicts.append(item)
        info["应测维度"] = intent_dicts
        confirmed = {
            dim: {
                "行为": record["behavior"],
                "状态码": record["status_code"],
                "code": record["code"],
                "有数据": record["data_present"],
                "data为null": record.get("data_is_none", False),
            }
            for dim, record in probes.items()
            if _probe_is_conclusive(record)
        }
        if confirmed:
            info["探针实测"] = confirmed
        ambiguous = [dim for dim, record in probes.items() if record.get("ambiguous")]
        if ambiguous:
            # 同一维度多个字段结论不一致，提示词里必须说清楚，否则模型会把
            # 「size 缺失被接受」当成「pid 缺失也会被接受」。
            info["探针不确定"] = (
                f"{', '.join(ambiguous)} 有多个测点结论不一致，不要据此断言成功/失败；"
                "按文档契约写期望，拿不准就标 needs_manual_setup。"
            )
        ep_examples = ctx.examples_for(ep, limit=2)
        if ep_examples:
            info["现役用例样例"] = ep_examples

        blocks.append(json.dumps(info, ensure_ascii=False, indent=2))

    return (
        f"模块：{module}\n"
        f"下面的每个接口都给出了「应测维度」清单。请逐条覆盖清单里的维度，"
        f"每个维度生成 1 条用例（每个接口总数不超过 {max_cases} 条）。\n"
        "只生成正例视为不合格；P0/P1 维度必须覆盖。"
        "没有依据的维度在 description 里注明「假设」，并在 marks 里加 needs_manual_setup。\n"
        "接口若带「探针实测」，那是刚刚对真实环境发请求测到的行为："
        "断言必须和实测一致（实测拒绝就断言错误码，实测接受就不要写「期望失败」）。\n\n"
        + "\n\n".join(blocks)
    )


# ============================================================================
# 整理 AI 返回的用例
# ============================================================================


def _normalize_ai_case(
    item: Any,
    default_module: str = "",
) -> dict[str, Any] | None:
    """把模型返回的一条用例整理成框架的标准格式。"""
    if not isinstance(item, dict):
        return None

    name = str(item.get("name") or "").strip()
    steps = _normalize_steps(item.get("steps"), prefix="step")
    if not name or not steps:
        return None

    module = str(item.get("module") or default_module or DEFAULT_MODULE).strip()
    module = sanitize_name(module, fallback=DEFAULT_MODULE)

    # marks 先归一再用：``"negative,p0"`` 这种写法以前会让优先级读成 P1。
    marks = _as_marks(item.get("marks")) or ["api"]
    priority = _priority_of(item, marks)
    dimension = str(item.get("dimension") or "").strip().upper()
    marks.extend([module, AI_MARK, priority.lower()])
    if dimension:
        # 维度 ID 进 marks，后面算维度覆盖率和筛选都用它。
        # 必须带前缀：裸 ``P0`` 会和优先级标签 ``p0`` 撞名。
        marks.append(f"{DIMENSION_MARK_PREFIX}{dimension}")
    marks = list(dict.fromkeys(marks))

    case: dict[str, Any] = {
        # 用例名是要在 Allure 里给人看的：中文括号/空格不能像模块名那样
        # 被清洗成下划线（「查询地区详情（正常请求）」→「查询地区详情_正常请求」）。
        "name": _display_name(name),
        # description 缺失时留空，避免「接口名称」和「用例标题」重复同一段文字
        "description": str(item.get("description") or ""),
        "status": "draft",
        "priority": priority,
        "marks": marks,
        "allure": {"feature": module},
        "steps": steps,
    }

    setups = _normalize_steps(item.get("setups"), prefix="setup")
    if setups:
        case["setups"] = setups
    teardowns = _normalize_steps(item.get("teardowns"), prefix="cleanup")
    if teardowns:
        case["teardowns"] = teardowns

    return {"testcase": case}


def _normalize_steps(items: Any, prefix: str) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []

    out: list[dict[str, Any]] = []
    for index, step in enumerate(items):
        if not isinstance(step, dict):
            continue
        path = str(step.get("path") or "").strip()
        if not path:
            continue

        normalized: dict[str, Any] = {
            "id": str(step.get("id") or f"{prefix}{index + 1}"),
            "description": str(step.get("description") or ""),
            "path": path,
            "method": str(step.get("method") or "GET").upper(),
        }
        if prefix != "step":
            normalized["operation_type"] = str(step.get("operation_type") or "api")
        for key in ("headers", "params", "data", "assert"):
            value = step.get(key)
            if value is None:
                continue
            if key == "assert" and not value:
                continue
            if key == "assert" and isinstance(value, dict):
                # 模型有时只写一条断言（字典），框架按 list 迭代会逐字符解析。
                value = [value]
            normalized[key] = value
        out.append(normalized)
    return out


def _as_marks(value: Any) -> list[str]:
    """把 marks 归一成字符串列表。

    模型经常把 marks 写成 ``"negative,p0"``，直接按 list 迭代会逐字符拆；
    写成对象时按 key 取。空结果返回 ``[]``，由调用方决定默认值。
    """
    if isinstance(value, str):
        parts: list[Any] = value.replace(";", ",").split(",")
    elif isinstance(value, dict):
        parts = list(value.keys())
    elif isinstance(value, (list, tuple, set, frozenset)):
        parts = list(value)
    else:
        return []
    return [text for text in (str(part).strip() for part in parts) if text]


def _priority_of(item: dict[str, Any], marks: list[str] | None = None) -> str:
    if not isinstance(item, dict):
        item = {}
    raw = str(item.get("priority") or "").strip().upper()
    if raw in {"P0", "P1", "P2", "P3"}:
        return raw
    # 调用方已经归一过 marks 时直接用，避免 ``"p0,p1"`` 被逐字符拆开漏判。
    source = _as_marks(item.get("marks")) if marks is None else marks
    for mark in source:
        mark = str(mark).strip().lower()
        if mark in {"p0", "p1", "p2", "p3"}:
            return mark.upper()
    return "P1"


def _normalize_auth_headers(step: dict[str, Any], project: str) -> None:
    """把鉴权头的值收敛成项目变量。

    模型会写 ``{{token}}`` / ``{{auth_token}}``，但框架里只有
    ``{{mall4j.token}}`` 这种项目变量。不改的话用例一跑就是变量解析失败，
    而且报错位置在请求层，看起来像环境问题。
    """
    headers = step.get("headers")
    if not isinstance(headers, dict) or not project:
        return
    for key in list(headers):
        name = str(key).lower()
        if name == "authorization" or "token" in name:
            headers[key] = f"{{{{{project}.token}}}}"


#: 文档契约类维度。探针实测「服务端接受」时，这些维度的期望不是「被拒绝」，
#: 而是「服务端没校验」—— 事实要写进断言，不能假装反例通过。
_CONTRACT_DIMS = {"A1", "A2", "A3", "A4", "A5", "A6", "A7"}

#: 期望「被拒绝」的维度。没有对应测点的实测结果时，不能拿正例实测去改写。
_NEGATIVE_DIMS = _CONTRACT_DIMS | {"C1", "C2", "C3", "F1", "F2", "F3", "F4"}


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _probe_record(outcome: Any) -> dict[str, Any] | None:
    """把探针结果（dataclass 或 JSON 里的中文键）收敛成统一字典。"""
    if outcome is None:
        return None
    if isinstance(outcome, dict):
        dimension = str(outcome.get("维度") or outcome.get("dimension") or "").upper()
        status_code = _safe_int(outcome.get("状态码", outcome.get("status_code")))
        code = str(outcome.get("code") or "")
        success = outcome.get("success")
        data_present = outcome.get("有数据", outcome.get("data_present"))
        data_is_none = outcome.get("data为null", outcome.get("data_is_none"))
        has_success = outcome.get("有success字段", outcome.get("has_success"))
        field_name = str(outcome.get("字段") or outcome.get("field") or "")
        error = str(outcome.get("error") or "")
        message = str(outcome.get("message") or "")
        behavior = str(outcome.get("行为") or outcome.get("behavior") or "")
    else:
        dimension = str(getattr(outcome, "dimension", "") or "").upper()
        status_code = _safe_int(getattr(outcome, "status_code", 0))
        code = str(getattr(outcome, "code", "") or "")
        success = getattr(outcome, "success", None)
        data_present = getattr(outcome, "data_present", False)
        data_is_none = getattr(outcome, "data_is_none", False)
        has_success = getattr(outcome, "has_success", False)
        field_name = str(getattr(outcome, "field", "") or "")
        error = str(getattr(outcome, "error", "") or "")
        message = str(getattr(outcome, "message", "") or "")
        try:
            behavior = str(outcome.behavior)
        except Exception:  # noqa: BLE001 - 纯 dict 结果没有 behavior 属性
            behavior = ""
    if not dimension:
        return None
    rejected = bool(
        not error
        and (
            status_code in (401, 403)
            or (isinstance(success, bool) and success is False)
        )
    )
    known_success = success if isinstance(success, bool) else None
    if not behavior:
        behavior = (
            "rejected"
            if rejected
            else ("success" if known_success is True else "unknown")
        )
    return {
        "dimension": dimension,
        "field": field_name,
        "status_code": status_code,
        "code": code,
        "success": known_success,
        "has_success": bool(has_success),
        "rejected": rejected,
        "data_present": bool(data_present),
        "data_is_none": bool(data_is_none),
        "error": error,
        "message": message,
        "behavior": behavior,
    }


def _probe_rejected(outcome: Any) -> bool:
    """探针结果是否「被拒绝」（兼容 dataclass 和导出的 dict）。"""
    record = _probe_record(outcome)
    return bool(record and record.get("rejected"))


def _probe_outcome_dict(outcome: Any) -> dict[str, Any]:
    """把探针结果导出成可 json 序列化的对象。

    ``--probe-output`` 以前直接调 ``outcome.to_dict()``；如果探针换成返回
    dict（或手工改过的 json 回灌），这里会 AttributeError 把 CLI 带崩。
    """
    if isinstance(outcome, dict):
        return outcome
    to_dict = getattr(outcome, "to_dict", None)
    if callable(to_dict):
        try:
            data = to_dict()
        except Exception:  # noqa: BLE001 - 导出失败不能拖垮 CLI
            data = None
        if isinstance(data, dict):
            return data
    return {"repr": repr(outcome)}


def _probe_is_conclusive(record: dict[str, Any] | None) -> bool:
    """探针结论是否可以作为断言依据。

    只有「明确被拒」或「明确成功」才算结论：``error``（网络/超时）、
    ``unknown``（5xx/HTML/没有 success 语义）、``ambiguous``（同维度多字段
    结论不一致）都不能拿来改写断言 —— 那不是事实，是没测出结论。
    """
    if not record or record.get("error") or record.get("ambiguous"):
        return False
    behavior = str(record.get("behavior") or "")
    if behavior.startswith("unknown") or behavior in ("", "ambiguous"):
        return False
    return bool(record.get("rejected")) or record.get("success") is True


def _probe_index(probe_map: Any) -> dict[tuple[str, str], dict[str, dict[str, Any]]]:
    """把探针结果整理成 ``{(METHOD, 规范化路径): {维度: 实测}}``。"""
    index: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
    if not probe_map:
        return index
    if not isinstance(probe_map, dict):
        # ``--probe-output`` 里的 json 被手工改坏、或调用方直接传了列表时，
        # 不能让它把整轮生成带崩 —— 退回「没有实测证据」即可。
        log.warning(
            "探针结果是 %s 不是对象，本次不使用实测证据",
            type(probe_map).__name__,
        )
        return index
    for key, outcomes in probe_map.items():
        if isinstance(key, tuple) and len(key) == 2:
            method, path = key
        else:
            method, _, path = str(key).strip().partition(" ")
        bucket = index.setdefault((str(method).upper(), _norm_path(path)), {})
        for outcome in _as_list(outcomes):
            record = _probe_record(outcome)
            if not record:
                continue
            dim = record["dimension"]
            existing = bucket.get(dim)
            if existing is None:
                bucket[dim] = record
            elif existing.get("behavior") != record["behavior"]:
                # 同一维度多个字段结论不同（如 pid 缺失被拒、size 缺失被接受）。
                # 索引里只存一条会张冠李戴，统一标成 ambiguous，不做证据。
                bucket[dim] = {
                    **record,
                    "ambiguous": True,
                    "behavior": "ambiguous",
                }
    return index


def _probes_for_steps(
    index: dict[tuple[str, str], dict[str, dict[str, Any]]],
    steps: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """取用例对应的探针记录。

    **只对单主步骤用例生效**：证据会让整个用例免掉 ``needs_manual_setup``，
    多步用例里真正被测的往往是第二、三步；用第一步的记录去担保整条用例
    等于给没验证的步骤开绿灯。
    """
    if not index or len(steps) != 1:
        return {}
    step = steps[0]
    key = (
        str(step.get("method") or "GET").upper(),
        _norm_path(step.get("path")),
    )
    return index.get(key, {})


def _append_assert(step: dict[str, Any], assertion: dict[str, Any]) -> None:
    """加断言，已存在同类型同字段同期望的就跳过。"""
    asserts = step.get("assert")
    if not isinstance(asserts, list):
        # 模型可能把断言写成单条对象；dict 没有 append，直接赋值会崩。
        asserts = []
        step["assert"] = asserts
    for item in asserts:
        if not isinstance(item, dict):
            continue
        if (
            _assert_type(item) == _assert_type(assertion)
            and str(item.get("field") or "") == str(assertion.get("field") or "")
            and str(item.get("expected")) == str(assertion.get("expected"))
        ):
            return
    asserts.append(assertion)


_STATUS_TYPES = ("status_code", "status", "http_status")


def _is_status_assert(item: Any) -> bool:
    """是不是状态码断言。框架同时支持 ``type=status_code`` 和 ``field=$status``。"""
    if not isinstance(item, dict):
        return False
    return _assert_type(item) in _STATUS_TYPES or str(item.get("field") or "") == "$status"


def _drop_code_asserts(step: dict[str, Any]) -> None:
    """删掉所有对 ``code`` 的等值/范围断言。

    现场实测已经拿到精确 code，模型写的（无论猜对猜错）都该由实测取代。
    留着两条 ``equal code`` 必然一条失败。
    """
    step["assert"] = [
        item
        for item in step.get("assert") or []
        if not (
            isinstance(item, dict)
            and str(item.get("field") or "") == "code"
            and _assert_type(item)
            in ("equal", "equals", "==", "not_equal", "!=", "in", "not_in")
        )
    ]


def _bool_expected(value: Any) -> bool | None:
    """把 ``true`` / ``"true"`` / ``True`` 这类写法统一成布尔；认不出返回 None。

    框架支持 ``is_true`` / ``is_false``，Excel 里也可能写成 ``equal`` + 字符串
    ``"true"``。只按 Python 布尔判断会漏删，留下和实测相反的断言。
    """
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("true", "1", "yes"):
        return True
    if text in ("false", "0", "no"):
        return False
    return None


def _drop_success_asserts(step: dict[str, Any], success_code: str = "") -> None:
    """删掉「期望成功」的断言 —— 实测被拒绝时留着必然误判。"""
    success_values = {"00000", "200", "0"}
    if success_code:
        success_values.add(success_code)
    kept: list[Any] = []
    for item in step.get("assert") or []:
        if not isinstance(item, dict):
            continue
        atype = _assert_type(item)
        field = str(item.get("field") or "")
        if field == "success" and (
            atype == "is_true" or _bool_expected(item.get("expected")) is True
        ):
            continue
        if (
            field == "code"
            and atype in ("equal", "equals", "==")
            and str(item.get("expected")) in success_values
        ):
            continue
        kept.append(item)
    step["assert"] = kept


def _drop_rejection_asserts(step: dict[str, Any]) -> None:
    """删掉「期望被拒绝」的断言 —— 实测成功时留着必然误判。"""
    kept: list[Any] = []
    for item in step.get("assert") or []:
        if not isinstance(item, dict):
            continue
        atype = _assert_type(item)
        field = str(item.get("field") or "")
        is_rejection = (
            (_is_status_assert(item) and str(item.get("expected")) in {"401", "403", "400"})
            or (
                field == "success"
                and (atype == "is_false" or _bool_expected(item.get("expected")) is False)
            )
            or (field == "code" and atype in ("not_equal", "!="))
            or (field == "code" and atype in ("in", "not_in"))
        )
        if not is_rejection:
            kept.append(item)
    step["assert"] = kept


def _set_status_assert(step: dict[str, Any], status_code: int) -> None:
    """把状态码断言改成实测值（框架会默认补契约状态码，可能和实测冲突）。"""
    if not status_code:
        return
    step["assert"] = [
        item
        for item in step.get("assert") or []
        if not _is_status_assert(item)
    ]
    _append_assert(step, {"type": "status_code", "expected": status_code})


def _apply_probe_evidence(
    tc: dict[str, Any],
    probes: dict[str, dict[str, Any]],
    ctx: ProjectContext,
) -> dict[str, Any] | None:
    """用探针实测结果改写断言。

    **为什么必须改写而不是提示模型**：模型对「未登录到底返回 401 还是
    HTTP 200 + 错误码」只能猜。猜错就是一条永远红或永远绿的用例。
    实测拿到什么，断言就写什么。
    """
    steps = tc.get("steps") or []
    if not probes or not steps:
        return None
    step = steps[0]
    marks = list(tc.get("marks") or [])
    dimension_marks = dimension_ids_of(marks)
    record = None
    matched_dim = ""
    for dim in ("C1", "C2", "C3", "F1", "A1", "A2", "A3", "A6", "B1", "B4", "P0"):
        if dim in dimension_marks and dim in probes:
            record, matched_dim = probes[dim], dim
            break
    if record is None:
        # 标了维度却没探到这个维度（max_intents 截断、字段不在基准请求里等）
        # 不能拿 P0 的正例实测顶替：那是另一组请求，断言会张冠李戴。
        if dimension_marks:
            return None
        # 没标维度、但名字一看就是反例的，也不能拿正例实测去改写
        looks_negative = any(
            key in str(tc.get("name") or "") for key in _NEGATIVE_NAME_HINTS
        )
        if looks_negative or ({str(m).lower() for m in marks} & NEGATIVE_MARKS):
            return None
        record, matched_dim = probes.get("P0"), "P0"
    # 只有明确结论能做证据：网络错误 / 5xx / 同维度结论冲突都不能改写断言，
    # 否则会把反例改成假绿用例。
    if not _probe_is_conclusive(record):
        return None

    success_code = str(ctx.contract.get("success_code") or "")

    if record["rejected"]:
        _drop_success_asserts(step, success_code)
        # 模型可能已经猜了一个错误码（对错都有），实测码必须独占，否则两条
        # ``equal code`` 必有一条失败。
        _drop_code_asserts(step)
        _set_status_assert(step, record["status_code"])
        # 精确断言：能拿到错误码就断言错误码；否则断言 success=false（仅当响应真有该字段）
        if record["code"] and record["code"] != success_code:
            _append_assert(step, {"type": "equal", "field": "code", "expected": record["code"]})
        elif record.get("has_success"):
            _append_assert(step, {"type": "is_false", "field": "success"})
    elif record["success"] is True:
        _drop_rejection_asserts(step)
        _drop_code_asserts(step)
        _set_status_assert(step, record["status_code"])
        # 文档说该拒、实测接受了 —— 这不是反例，是契约没被校验。记录下来。
        if matched_dim in _CONTRACT_DIMS or matched_dim == "C1":
            marks.append("defect_candidate")
            note = f"探针实测：{matched_dim} 场景服务端未拒绝（{record['behavior']}），疑似契约未校验"
            if note not in str(tc.get("description") or ""):
                tc["description"] = f"{tc.get('description') or ''}\n{note}".strip()
        if record["code"]:
            _append_assert(
                step, {"type": "equal", "field": "code", "expected": record["code"]}
            )
        if record.get("has_success"):
            _append_assert(step, {"type": "is_true", "field": "success"})
        # data 到底是 null 还是 {}，只有探针知道。只有实测 null 才断言 is_none，
        # 否则「空对象」会被误判成失败。
        if matched_dim == "F1" and record["data_is_none"]:
            _append_assert(step, {"type": "is_none", "field": "data"})

    note = f"探针实测：{matched_dim} → {record['behavior']}"
    if note not in str(tc.get("description") or ""):
        tc["description"] = f"{tc.get('description') or ''}\n{note}".strip()
    marks.extend(["probe_verified", f"probe:{matched_dim}"])
    tc["marks"] = list(dict.fromkeys(marks))
    return record


def _has_rejection_assert(step: dict[str, Any]) -> bool:
    """用例是否断言了「请求会被拒绝」。"""
    if not isinstance(step, dict):
        return False
    for item in _as_list(step.get("assert")):
        if not isinstance(item, dict):
            continue
        atype = _assert_type(item)
        field = str(item.get("field") or "")
        expected = item.get("expected")
        # status_code 断言没有 field，字段名就是 type；也支持 field=$status
        if _is_status_assert(item) and str(expected) in {"401", "403"}:
            return True
        if field == "success" and (
            atype == "is_false" or _bool_expected(expected) is False
        ):
            return True
        if field == "code" and atype in ("not_equal", "!="):
            return True
        # 探针实测拿到的精确错误码也是「期望失败」
        if (
            field == "code"
            and atype in ("equal", "equals", "==")
            and str(expected) not in {"", "00000", "200", "0"}
        ):
            return True
        if field == "code" and atype in ("in", "not_in"):
            container = item.get("container") or item.get("expected")
            # 模型可能把集合写成单个值（expected: 200）—— 直接迭代 int 会抛
            # TypeError。非序列值按「只有一个元素」处理。
            if not isinstance(container, (list, tuple, set, frozenset)):
                container = [] if container is None else [container]
            if "00000" not in [str(v) for v in container]:
                return True
    return False


def _match_endpoint(step: dict[str, Any], endpoints: list[Endpoint]) -> Endpoint | None:
    """按「方法 + 归一化路径」找回用例对应的接口。

    ``_norm_path`` 会把 ``/admin/area/110000000000`` 和
    ``/admin/area/{id}`` 都归一成 ``/admin/area/{}``，所以路径参数已替换的
    用例也能对上 OpenAPI 模板。
    """
    method = str(step.get("method") or "GET").upper()
    path = _norm_path(step.get("path"))
    for ep in endpoints:
        if str(ep.method or "").upper() == method and _norm_path(ep.path) == path:
            return ep
    return None


def _ensure_business_asserts(
    tc: dict[str, Any],
    endpoints: list[Endpoint],
    min_asserts: int = 2,
) -> None:
    """正例业务断言不足时，从文档响应示例补「字段存在」类断言。

    **为什么只补文档里有的字段**：模型常见输出是只写 ``code == "00000"``，
    这条用例失败时只能说明「接口挂了」，说明不了「返回的数据对不对」。
    文档响应示例属于 metadata 级证据，字段存在性有依据，不猜具体值。

    多步用例不动 —— 断言该证明哪一步的语义，只有写用例的人知道。
    """
    from ai.rules.validator import business_asserts

    steps = [s for s in tc.get("steps") or [] if isinstance(s, dict)]
    if len(steps) != 1:
        return
    step = steps[0]

    asserts = [a for a in _as_list(step.get("assert")) if isinstance(a, dict)]
    if business_asserts(asserts) >= min_asserts:
        return

    ep = _match_endpoint(step, endpoints)
    if ep is None:
        return

    from tools.importer.case_builder import build_asserts

    existing = {(_assert_type(a), str(a.get("field") or "")) for a in asserts}
    for item in build_asserts(ep):
        field = str(item.get("field") or "")
        if field in {"", "code", "success"}:
            continue
        key = (_assert_type(item), field)
        if key in existing:
            continue
        asserts.append(item)
        existing.add(key)
        if business_asserts(asserts) >= min_asserts:
            break
    step["assert"] = asserts


def _finalize_ai_case(
    case: dict[str, Any],
    ctx: ProjectContext,
    endpoints: list[Endpoint],
    probe_index: dict[tuple[str, str], dict[str, dict[str, Any]]] | None = None,
    all_endpoints: list[Endpoint] | None = None,
) -> dict[str, Any] | None:
    """确定性收敛：路径参数、断言契约、写操作清理标记、探针实测。"""
    tc = case["testcase"]
    marks = list(tc.get("marks") or [])
    needs_manual = False

    # 造数链路必须**先于**路径解析接入：写主步骤的 ``{id}`` 可能已经被
    # 模型写成真实值，规划器需要接口模板才能找回兄弟接口（造数/查数/清理）。
    # 同时要能看到整批接口，不能只看当前 chunk —— PUT 的造数接口是 POST。
    chain_result = attach_chain(
        tc,
        all_endpoints or endpoints,
        ctx,
        dimensions=dimension_ids_of(marks),
    )
    if chain_result.startswith("ok:"):
        # 链路已经解决了数据构造/路径参数问题，模型或本地模板之前打的
        # needs_manual_setup 不再成立；后续检查如果仍有问题会重新打上。
        if not any(
            _SINGLE_BRACE_RE.search(str(step.get("path") or ""))
            for step in tc.get("steps") or []
        ):
            tc["marks"] = [
                m for m in (tc.get("marks") or []) if m != "needs_manual_setup"
            ]
        tc["marks"] = list(dict.fromkeys(list(tc.get("marks") or []) + ["api_data_chain"]))
        _append_description(tc, f"数据构造：{chain_result[3:]}")
        marks = list(tc.get("marks") or [])
        log.info("用例 %s 已挂造数链路：%s", tc.get("name"), chain_result[3:])
    elif chain_result.startswith("failed:"):
        needs_manual = True
        log.warning(
            "用例 %s 是写操作但给不出安全造数链路（%s），已标 needs_manual_setup",
            tc.get("name"),
            chain_result,
        )

    for step in tc.get("steps") or []:
        resolved = _resolve_path(step.get("path"), endpoints, ctx)
        if not resolved:
            # 不丢用例 —— 留着让人工补真实 id，但绝不进 CI
            needs_manual = True
            log.warning(
                "用例 %s 的路径参数无法解析，已标 needs_manual_setup: %s",
                tc.get("name"),
                step.get("path"),
            )
        else:
            step["path"] = resolved
        if _normalize_asserts(step, marks, ctx.contract):
            needs_manual = True

    for group in ("setups", "teardowns"):
        for item in tc.get(group) or []:
            _normalize_asserts(item, marks, ctx.contract, is_prepost=True)
            _normalize_auth_headers(item, ctx.project)
            # 前置/后置里的**单**花括号路径参数（OpenAPI 的 {id}）不能原样发：
            # 运行时会被 URL 编码成 %7Bid%7D → 404（清理静默失败留脏数据）。
            # 这里刻意不自动填真实 id —— 后置 DELETE 填错 id 会删掉环境数据，
            # 交给人工确认更安全。``{{步骤.id}}`` 这种框架变量不在此列。
            if _SINGLE_BRACE_RE.search(str(item.get("path") or "")):
                needs_manual = True
                log.warning(
                    "用例 %s 的 %s 路径参数未解析，已标 needs_manual_setup: %s",
                    tc.get("name"),
                    group,
                    item.get("path"),
                )

    # 模型自己写的 teardown 不能盲信。``attach_chain`` 见到已有 setups/teardowns
    # 会直接跳过（``skipped:has_chain``），于是 ``DELETE /admin/brand/100``
    # 这种硬编码 id 就原样落盘了 —— 它删的是环境里的真实记录，不是本用例
    # 造出来的那条。这里只要后置的路径/参数/请求体里没有任何 ``{{...}}``
    # 引用，就视为不可追溯，直接剥掉转人工。宁可留脏数据，也不能误删。
    unsafe = unsafe_teardowns(tc)
    if unsafe:
        unsafe_refs = {id(item) for item in unsafe}
        tc["teardowns"] = [
            item
            for item in (tc.get("teardowns") or [])
            if id(item) not in unsafe_refs
        ]
        needs_manual = True
        log.warning(
            "用例 %s 的 teardown 删除/修改的是固定数据（%s），已移除并标 needs_manual_setup",
            tc.get("name"),
            "；".join(
                f"{item.get('method')} {item.get('path')}" for item in unsafe
            ),
        )

    main_steps = tc.get("steps") or []
    dimension_marks = dimension_ids_of(marks)

    # 请求头里模型常写 ``{{token}}`` / ``{{auth_token}}`` 这类不存在的变量，
    # 落盘前统一收敛成项目变量，否则跑起来必然解析失败。
    for step in main_steps:
        _normalize_auth_headers(step, ctx.project)

    # C1 未登录：必须是**匿名请求**。
    # 模型会正确地把请求头留空，但 create_case 默认补鉴权头 ——
    # 不显式打 no_auth，跑起来就不是"未登录"，覆盖度也会判未证实。
    if "C1" in dimension_marks and main_steps:
        main_steps[0]["headers"] = {}
        marks.append("no_auth")
        # 模型经常只写个 status_code 就算"未登录"测完了 —— 那不是验证，
        # 只是发了个请求。补一条「不等于成功码」：鉴权必需来自项目配置，
        # 是有依据的期望；真允许匿名访问时这条会失败，那是权限缺陷。
        if not _has_rejection_assert(main_steps[0]):
            success_code = str(ctx.contract.get("success_code") or "")
            if success_code:
                main_steps[0].setdefault("assert", []).append(
                    {"type": "not_equal", "field": "code", "expected": success_code}
                )

    # 写操作没清理 → 保留给人工审阅，但打标 + draft，绝不进 CI。
    # 必须看**所有**主步骤：多步用例首步是 GET，第二步才是写操作。
    write_steps = [
        s for s in main_steps if str(s.get("method") or "").upper() in WRITE_METHODS
    ]
    if (
        write_steps
        and not tc.get("teardowns")
        and not write_is_self_cleaning(tc)
    ):
        needs_manual = True
        log.warning("用例 %s 是写操作但没有 teardowns，已标 needs_manual_setup", tc.get("name"))

    # 探针实测优先于一切猜测：断言直接改成实测到的错误码/状态码/空数据。
    evidence = None
    probes = _probes_for_steps(probe_index or {}, main_steps)
    if probes:
        # 先把已累积的标记落回用例，否则探针改写时会把 no_auth 等丢掉
        tc["marks"] = list(dict.fromkeys(marks))
        evidence = _apply_probe_evidence(tc, probes, ctx)
        marks = list(tc.get("marks") or [])
        dimension_marks = dimension_ids_of(marks)

    # 正例断言密度：模型经常只写 code/success，失败时说明不了数据对不对。
    # 从文档响应示例补字段存在性断言（metadata 级证据），补不够的交给质量闸。
    if {"positive", "dim:p0"} & {str(m).lower() for m in marks}:
        _ensure_business_asserts(tc, all_endpoints or endpoints)

    # 反例/边界/权限：如果没给出具体业务错误码，本质是"猜失败"。
    # 服务端未必校验（area 传空值仍返回成功就是这么误报的），
    # 标 needs_manual_setup，不能拿假设进 CI。
    # 有探针实测结论时不算猜 —— 期望来自真实响应。
    categories = {str(m).lower() for m in marks}
    negative_dimensions = dimension_marks & {
        "A1", "A2", "A3", "A4", "A5", "A6", "A7",
        "F1", "F2", "F3", "F4",
    }
    if (
        categories & (NEGATIVE_MARKS | {"permission"}) or negative_dimensions
    ) and not evidence:
        success_code = str(ctx.contract.get("success_code") or "")
        has_specific_error = any(
            _assert_type(item) in ("equal", "equals", "==")
            and str(item.get("field") or "") == "code"
            and str(item.get("expected")) not in {"", success_code}
            and not _is_placeholder_value(item.get("expected"))
            for step in main_steps
            for item in (step.get("assert") or [])
        )
        # 权限类有「鉴权是必需的」这个项目配置依据：
        # 401/403 或 not_equal 成功码都算明确期望，不算"猜失败"。
        if dimension_marks & {"C1", "C2", "C3"}:
            has_specific_error = has_specific_error or any(
                (
                    _assert_type(item) in ("status_code", "http_status")
                    and str(item.get("expected")) in {"401", "403"}
                )
                or (
                    _assert_type(item) in ("not_equal", "!=")
                    and str(item.get("field") or "") == "code"
                )
                for step in main_steps
                for item in (step.get("assert") or [])
            )
        if not has_specific_error:
            needs_manual = True

    if needs_manual:
        marks.append("needs_manual_setup")
    tc["marks"] = list(dict.fromkeys(marks))
    tc["status"] = "draft"
    return case


def _resolve_path(
    path: Any,
    endpoints: list[Endpoint],
    ctx: ProjectContext,
) -> str:
    """把路径里的 ``{id}`` 换成现役用例用过的真实值。"""
    text = str(path or "").strip()
    if not text:
        return ""
    # 造数链路写进来的是框架变量（``{{tools.find_id_by_fixed(...)}}``），
    # 它不是 OpenAPI 的路径参数，原样交给运行时解析。
    if "{{" in text:
        return text
    if "{" not in text:
        return text

    for ep in endpoints:
        if _norm_path(ep.path) != _norm_path(text):
            continue
        values = ctx.path_values_for(ep)
        resolved = text
        for name, value in values.items():
            resolved = resolved.replace(f"{{{name}}}", str(value))
        if "{" not in resolved:
            return resolved

    # 路径模板对不上，或者拿不到真实值 —— 交给调用方标 needs_manual_setup
    return ""


def _resolve_local_step(step: dict[str, Any], ep: Endpoint, ctx: ProjectContext) -> bool:
    """本地模板也要解析路径参数；解析不了就返回 False（由调用方标人工）。"""
    resolved = _resolve_path(step.get("path"), [ep], ctx)
    if not resolved:
        return False
    step["path"] = resolved
    return True


def _normalize_asserts(
    step: dict[str, Any],
    marks: list[str],
    contract: dict[str, Any],
    is_prepost: bool = False,
) -> bool:
    """按项目契约收敛断言。返回是否需要人工补数据。

    - 补 ``status_code``
    - ``code == 200`` → ``code == "00000"``（项目成功码）
    - 正例补 ``code`` / ``success``
    - ``in`` 的集合放到 ``container``（框架内部格式）
    """
    asserts = step.get("assert")
    if not isinstance(asserts, list):
        asserts = []
    asserts = [a for a in asserts if isinstance(a, dict) and a.get("type")]
    needs_manual = False

    # in / not_in 在框架里用 container，不是 expected
    for item in asserts:
        atype = str(item.get("type") or "").lower().replace(" ", "_")
        if atype in ("in", "not_in") and "container" not in item and "expected" in item:
            item["container"] = item.pop("expected")

    if not is_prepost:
        code = str(contract.get("success_code") or "").strip()
        dimensions = dimension_ids_of(marks)
        is_negative = any(str(m).lower() in NEGATIVE_MARKS for m in marks) or bool(
            dimensions
            & {
                "A1", "A2", "A3", "A4", "A5", "A6", "A7",
                # 权限也是「期望被拒绝」语义，别把模型写对的错误码改成成功码
                "C1", "C2", "C3",
                "F1", "F2", "F3", "F4",
            }
        )

        if code:
            kept: list[dict[str, Any]] = []
            for item in asserts:
                field = str(item.get("field") or "")
                atype = _assert_type(item)
                if _is_status_assert(item) and atype in ("in", "not_in"):
                    # 一个请求只会有一个状态码，"200 或 401" 这种区间期望没有信息量
                    continue
                if field == "code" and atype in (
                    "is_not_none",
                    "is_none",
                    "exists",
                    "not_exists",
                    "is_not_null",
                ):
                    # code 字段永远存在，"非空"不等于任何业务语义
                    continue
                if field == "code":
                    if atype in ("equal", "equals", "=="):
                        if is_negative:
                            # 反例里：占位期望（"string"/200）没有依据，删掉；
                            # 具体业务错误码（如 A00005）是模型的证据，保留。
                            if _is_placeholder_value(item.get("expected")):
                                continue
                        else:
                            # 正例一律以项目契约成功码为准
                            item["expected"] = code
                    elif atype in ("not_equal", "!="):
                        if is_negative:
                            item["expected"] = code
                        else:
                            # 正例里不该出现"不等于成功码"
                            continue
                    elif atype == "in" and isinstance(item.get("container"), list):
                        container = [
                            code if _is_placeholder_value(x) else x
                            for x in item["container"]
                        ]
                        if container == [code]:
                            item["type"] = "equal"
                            item["expected"] = code
                            item.pop("container", None)
                        else:
                            item["container"] = container
                elif _is_placeholder_assert(item):
                    # message contains "string" 这种文档占位断言，删掉
                    continue
                kept.append(item)
            asserts = kept

        if not is_negative and code:
            if not any(
                _assert_type(item) in ("equal", "equals", "==")
                and str(item.get("field") or "") == "code"
                for item in asserts
            ):
                asserts.append({"type": "equal", "field": "code", "expected": code})
            if not any(str(item.get("field") or "") == "success" for item in asserts):
                asserts.append({"type": "equal", "field": "success", "expected": True})

        if not any(_is_status_assert(item) for item in asserts):
            asserts.insert(
                0,
                {"type": "status_code", "expected": contract.get("http_status", 200)},
            )

    # 落库断言里的 %s / ? 必须有 params，否则执行层会把它当字面量拼进 SQL。
    # 能从路径/请求参数推断就补上；推不出就删掉这条断言并标人工。
    kept: list[dict[str, Any]] = []
    for item in asserts:
        if not _assert_type(item).startswith("db_query"):
            kept.append(item)
            continue
        query = str(item.get("query") or "")
        placeholders = query.count("%s") + query.count("?")
        if placeholders == 0 or item.get("params"):
            kept.append(item)
            continue

        candidates = _candidate_values(step)
        if len(candidates) == placeholders:
            item["query"] = query.replace("?", "%s")
            item["params"] = [
                int(value) if str(value).isdigit() else value
                for value in candidates
            ]
            kept.append(item)
            continue

        needs_manual = True
        log.warning(
            "落库断言的占位符无法推断参数，已删除并标人工: %s",
            query,
        )

    step["assert"] = kept
    return needs_manual


def _candidate_values(step: dict[str, Any]) -> list[str]:
    """给 ``%s`` 占位符找候选值：路径里的数字 + 请求参数/请求体里的数字。"""
    values: list[str] = []
    for match in re.findall(r"\d+", str(step.get("path") or "")):
        values.append(match)

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for value in node.values():
                _walk(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                _walk(value)
        elif isinstance(node, (int, float)) or (
            isinstance(node, str) and node.strip().isdigit()
        ):
            values.append(str(node).strip())

    _walk(step.get("params"))
    _walk(step.get("data"))
    return values


def _assert_type(item: dict[str, Any]) -> str:
    return str(item.get("type") or "").lower().replace(" ", "_")


def _is_placeholder_value(value: Any) -> bool:
    """OpenAPI 示例里的占位值，不能当期望值。"""
    text = str(value).strip().lower()
    return text in {"string", "str", "", "none", "null", "0", "0.0", "200", "200.0"}


def _is_placeholder_assert(item: dict[str, Any]) -> bool:
    """``contains/matches`` 里拿文档占位串当期望的断言。"""
    if _assert_type(item) not in ("contains", "matches"):
        return False
    return _is_placeholder_value(item.get("expected"))


def _deep_copy(data: Any) -> Any:
    """深拷贝（生成变体用例时不改原数据）。"""
    return json.loads(json.dumps(data, ensure_ascii=False, default=str))


# ============================================================================
# 落盘
# ============================================================================


def write_cases(
    cases: list[dict[str, Any]],
    cases_root: Path,
    project: str = "mall4j",
    overwrite: bool = False,
) -> list[Path]:
    """把用例写成 YAML 文件。

    文件名用用例名（中文会被清洗成安全的名字）。同名时追加序号，
    **不允许静默跳过** —— 那会让模型生成的变体用例凭空消失。
    """
    written: list[Path] = []
    project_dir = Path(cases_root) / project

    for data in cases:
        case = data.get("testcase") if isinstance(data, dict) else None
        if not isinstance(case, dict):
            # 外部调用方可能传进半成品；没有 testcase 就没法落盘。
            log.warning("跳过没有 testcase 的用例（%s）", type(data).__name__)
            continue
        allure = case.get("allure")
        feature = allure.get("feature") if isinstance(allure, dict) else None
        module = feature or case.get("module") or DEFAULT_MODULE
        module_dir = project_dir / sanitize_name(module, fallback=DEFAULT_MODULE)
        module_dir.mkdir(parents=True, exist_ok=True)

        case.setdefault("status", "draft")
        base = sanitize_name(str(case.get("name") or ""), fallback="case")
        case_file = module_dir / f"{base}.yaml"
        if case_file.exists() and not overwrite:
            index = 2
            while (module_dir / f"{base}_{index}.yaml").exists():
                index += 1
            case_file = module_dir / f"{base}_{index}.yaml"

        with case_file.open("w", encoding="utf-8") as f:
            yaml.safe_dump(
                case,
                f,
                allow_unicode=True,
                sort_keys=False,
                default_flow_style=False,
                width=100,
            )
        written.append(case_file)
        log.info("已生成: %s", case_file)

    return written


def _existing_signatures(project: str, module: str) -> set[tuple[str, str, str, str]]:
    """已落盘用例的「方法 + 路径 + 接口名称 + 用例标题」指纹。

    重复跑生成器以前会**原样再追加一遍**（用例编号不同、内容一样），
    沙箱模块里很快就出现成对的同义用例。写之前先按指纹去重。

    读不出来就返回空集合 —— 去重是优化，不能因为它让生成整体失败。
    """
    from utils.cases.case_source import current_excel_dir

    path = current_excel_dir() / project / module / f"{module}.xlsx"
    if not path.exists():
        return set()

    workbook = None
    try:
        import openpyxl

        from utils.excel.flat_parser import SHEET_FLAT

        workbook = openpyxl.load_workbook(path, read_only=True)
        sheet = workbook[SHEET_FLAT]
        header_row = next(sheet.iter_rows(min_row=1, max_row=1))
        headers = {
            str(cell.value).strip(): idx
            for idx, cell in enumerate(header_row)
            if cell.value is not None
        }
        needed = ("请求方法", "接口", "接口名称", "用例标题")
        if any(name not in headers for name in needed):
            return set()

        out: set[tuple[str, str, str, str]] = set()
        for row in sheet.iter_rows(min_row=2):
            values = [
                str(row[headers[name]].value or "").strip() for name in needed
            ]
            if any(values):
                out.add((values[0].upper(), values[1], values[2], values[3]))
        return out
    except Exception as exc:  # noqa: BLE001 - 去重失败不阻断生成
        log.warning("读取已落盘用例失败（跳过去重）: %s", exc)
        return set()
    finally:
        if workbook is not None:
            # 必须显式关闭：Windows 上只读句柄没释放时，紧接着的
            # workbook.save() 可能因为文件被占用而失败。
            workbook.close()


def write_cases_excel(
    cases: list[dict[str, Any]],
    project: str = "mall4j",
    module: str = "",
    sandbox: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """把用例写进现役 Excel 用例目录（走 ``create_case`` 的安全写入）。

    Args:
        sandbox: 为 True 时写到 ``ai_<模块>``，不碰现役模块。

    Returns:
        (创建成功的用例, 跳过的用例及原因)
    """
    from ai.agent.tools.case_io import align_case_naming, create_case

    created: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    existing: dict[str, set[tuple[str, str, str, str]]] = {}

    for data in cases:
        tc = data.get("testcase") if isinstance(data, dict) else None
        if not isinstance(tc, dict):
            skipped.append(
                {"name": None, "reason": "用例结构不对（缺少 testcase）"}
            )
            continue
        # ``steps`` 可能是对象而不是数组：len() 对单键 dict 返回 1，
        # 接着 steps[0] 会 KeyError，所以类型也要一起校验。
        steps = [item for item in _as_list(tc.get("steps")) if isinstance(item, dict)]
        if not steps:
            skipped.append(
                {"name": tc.get("name"), "reason": "用例没有可执行的主步骤"}
            )
            continue

        step = steps[0]
        allure = tc.get("allure")
        feature = allure.get("feature") if isinstance(allure, dict) else None
        target_module = (
            module
            or feature
            or tc.get("module")
            or DEFAULT_MODULE
        )
        target_module = sanitize_name(target_module, fallback=DEFAULT_MODULE)
        # 带造数链路的用例会真的改环境数据，必须落在沙箱模块里；
        # 即使调用方忘了 --sandbox，也不能写进现役模块。
        case_marks = _as_marks(tc.get("marks"))
        chain_bearing = "api_data_chain" in case_marks
        if (sandbox or chain_bearing) and not target_module.startswith("ai_"):
            target_module = f"ai_{target_module}"
        # Excel 约定：用例名称 = 模块中文名 - 业务功能（场景），
        # 报告标题 = [模块] 接口名称 - 用例标题。
        api_name, scenario = _name_parts(tc, step)
        api_name = api_name or str(step.get("description") or "")
        title_text = scenario or str(tc.get("name") or "")

        # 模型有时把接口文档的「接口名」直接抄成业务名（``PUT /admin/area``），
        # 写盘前先对齐成现役的中文业务名 —— 指纹和报告标题都得用对齐后的，
        # 否则同一接口重跑生成器时会因为名字不同而重复追加。
        aligned = align_case_naming(
            project,
            target_module,
            str(step.get("path") or ""),
            str(step.get("method") or "GET"),
            api_name,
            title_text,
        )
        api_name = aligned["api_name"]
        title_text = aligned["title"]

        # 同名同路径同场景 = 重复。重跑生成器时不能原样再追加一遍。
        signature = (
            str(step.get("method") or "GET").upper(),
            str(step.get("path") or ""),
            api_name,
            title_text,
        )
        known = existing.get(target_module)
        if known is None:
            known = _existing_signatures(project, target_module)
            existing[target_module] = known
        if signature in known:
            skipped.append(
                {
                    "name": tc.get("name"),
                    "reason": "已存在相同用例（方法+路径+业务名+场景），跳过重复",
                }
            )
            continue

        try:
            result = create_case(
                project=project,
                module=target_module,
                path=str(step.get("path") or ""),
                method=str(step.get("method") or "GET"),
                title=title_text,
                assertions=step.get("assert") or [],
                api_name=api_name,
                case_name=_full_case_name(
                    project, target_module, step, api_name, title_text
                ),
                request_data=step.get("data"),
                request_headers=step.get("headers"),
                query_params=step.get("params"),
                priority=str(tc.get("priority") or _priority_of(tc)),
                marks=",".join(case_marks),
                status="draft",
                setups=tc.get("setups"),
                teardowns=tc.get("teardowns"),
                # 多步场景链（创建→查询→修改→删除）整体落盘；
                # 单步用例也走同一条路径，行为不变。
                steps=steps,
            )
            created.append(result)
            known.add(signature)
        except Exception as exc:  # noqa: BLE001 - 一条写不进去不影响其他条
            skipped.append({"name": tc.get("name"), "reason": str(exc)})

    return created, skipped


# ============================================================================
# 命令行
# ============================================================================


def _run_probe(
    endpoints: list[Endpoint],
    project: str,
    env: str,
    max_cases: int,
    output: str = "",
) -> dict[str, Any] | None:
    """生成前先探针。探针失败不阻断生成，只是退回「假设」模式。"""
    from ai.pipeline.probe import probe_endpoints

    print(f"探针: 对 {len(endpoints)} 个接口发真实请求（环境 {env}）...")
    try:
        probe_map = probe_endpoints(
            endpoints,
            project=project,
            env=env,
            # 探针要覆盖生成器可能用到的全部维度，多要一点
            max_intents=max(max_cases, 8),
        )
    except Exception as exc:  # noqa: BLE001 - 探针是增强项，不能拖垮生成
        print(f"探针失败（{exc}），本次按假设生成", file=sys.stderr)
        return None

    if not isinstance(probe_map, dict):
        print(
            f"探针返回了 {type(probe_map).__name__} 而不是对象，本次按假设生成",
            file=sys.stderr,
        )
        return None

    buckets = {
        str(key): _as_list(items)
        for key, items in probe_map.items()
    }
    total = sum(len(items) for items in buckets.values())
    rejected = sum(
        1
        for items in buckets.values()
        for outcome in items
        if _probe_rejected(outcome)
    )
    print(f"探针完成: {total} 次请求，被拒绝 {rejected} 次")

    if output:
        target = Path(output)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            key: [_probe_outcome_dict(outcome) for outcome in items]
            for key, items in buckets.items()
        }
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"探针原始结果写入 {target}")
    return probe_map


def main(argv: list[str] | None = None) -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="AI 用例生成")
    parser.add_argument("--file", required=True, help="接口文档文件（OpenAPI / HAR / cURL）")
    parser.add_argument("--project", default="mall4j", help="项目名")
    parser.add_argument("--module", default="", help="模块名（不填按现役用例/路径自动推断）")
    parser.add_argument(
        "--mode",
        choices=["ai", "local"],
        default="ai",
        help="ai=用大模型（没配置就自动退回 local）；local=纯本地模板",
    )
    parser.add_argument(
        "--format",
        choices=["excel", "yaml"],
        default="excel",
        help="落盘格式：excel=写现役 Excel（默认）；yaml=写 YAML",
    )
    parser.add_argument(
        "--max-cases",
        type=int,
        default=6,
        help="每个接口最多几条用例（同时决定最多覆盖几个测点维度）",
    )
    parser.add_argument("--only-path", default="", help="只生成路径包含该子串的接口")
    parser.add_argument("--only-method", default="", help="只生成指定方法，如 GET")
    parser.add_argument("--limit", type=int, default=0, help="最多处理几个接口（0=全部）")
    parser.add_argument(
        "--sandbox",
        action="store_true",
        help="Excel 模式写到 ai_<模块>，不碰现役用例",
    )
    parser.add_argument("--overwrite", action="store_true", help="YAML 模式覆盖已存在文件")
    parser.add_argument("--dry-run", action="store_true", help="只生成不落盘")
    parser.add_argument(
        "--probe",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="生成前先对真实环境探针（默认开）；--no-probe 关闭",
    )
    parser.add_argument("--probe-env", default="test", help="探针使用的环境")
    parser.add_argument(
        "--probe-output",
        default="",
        help="把探针原始结果另存为 JSON（留档）",
    )
    args = parser.parse_args(argv)

    from tools.importer.dispatcher import parse_file
    from utils.paths import CASES_ROOT

    print(f"解析文档: {args.file}")
    try:
        endpoints = parse_file(args.file)
    except Exception as exc:  # noqa: BLE001
        print(f"解析失败: {exc}", file=sys.stderr)
        return 1

    if args.only_path:
        endpoints = [ep for ep in endpoints if args.only_path in ep.path]
    if args.only_method:
        endpoints = [
            ep for ep in endpoints
            if ep.method.upper() == args.only_method.upper()
        ]
    if args.limit > 0:
        endpoints = endpoints[: args.limit]

    if not endpoints:
        print("没有匹配到接口", file=sys.stderr)
        return 1

    print(f"解析到 {len(endpoints)} 个接口，开始生成用例（模式: {args.mode}）")

    probe_map = None
    if args.probe and args.mode == "ai":
        probe_map = _run_probe(
            endpoints, args.project, args.probe_env, args.max_cases, args.probe_output
        )

    cases = generate_cases(
        endpoints,
        project=args.project,
        module=args.module,
        use_ai=(args.mode == "ai"),
        max_cases_per_endpoint=args.max_cases,
        probe_map=probe_map,
    )

    if not cases:
        print("没生成出用例", file=sys.stderr)
        return 1

    fallback = sum(
        1 for c in cases if FALLBACK_MARK in (c.get("testcase") or {}).get("marks", [])
    )
    manual = sum(
        1 for c in cases
        if "needs_manual_setup" in (c.get("testcase") or {}).get("marks", [])
    )
    print(f"\n生成 {len(cases)} 条用例（本地兜底 {fallback} 条，需人工补造数/清理 {manual} 条）")

    if args.dry_run:
        for data in cases:
            tc = data["testcase"]
            step = (tc.get("steps") or [{}])[0]
            print(f"  - [{tc.get('priority')}] {tc.get('name')}  {step.get('method')} {step.get('path')}")
        return 0

    if args.format == "excel":
        created, skipped = write_cases_excel(
            cases, project=args.project, module=args.module, sandbox=args.sandbox
        )
        print(f"\n写入 Excel: {len(created)} 条，跳过 {len(skipped)} 条")
        for item in created:
            print(f"  + {item.get('case_id')}  {item.get('file')}")
        for item in skipped:
            print(f"  ! {item.get('name')}  —— {item.get('reason')}")
    else:
        written = write_cases(cases, CASES_ROOT, args.project, overwrite=args.overwrite)
        print(f"\n生成了 {len(written)} 条用例文件：")
        for path in written:
            print(f"  {path}")

    print(f"\n提示：AI 生成的用例是 draft 状态，建议人工审阅并跑通后再改 debugged。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
