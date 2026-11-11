"""API 造数编排：给写接口补齐「造数 → 查真实 id → 主步骤 → 清理」链路。

**为什么需要**

生成器以前对写接口一刀切：POST/PUT/PATCH/DELETE 一律标
``needs_manual_setup``，因为它们没有前置造数和后置清理。结果是写接口
永远进不了 CI —— 智能体只是把问题贴了个标签，没有解决。

造数不能走数据库：mall4j 的账号只读，而且直插会绕过业务校验。所以只能
用被测系统自己的 API 造数，再用业务字段把服务端生成的 id 查回来。

**和现役用例保持同一套写法**

现役写用例已经有一套稳定范式（见 ``admin_area`` / ``admin_hotSearch``）：

- 造数：``POST`` 集合接口，名称用 ``{{tools.fixed('key', '前缀')}}``
- 查 id：``GET .../list`` 或 ``GET .../page``，再用
  ``{{tools.find_id_by_fixed(查数据.data, "areaName", "key", "areaId", "前缀")}}``
- 清理：``DELETE 集合/{id}``，路径里引用上面查到的 id

本模块只负责**确定性地**拼出这套 setups/teardowns，不引入第二套机制。

**安全边界**

- 只给**正例**造数；反例仍标 ``needs_manual_setup``（反例可能根本不产生
  数据，无条件 DELETE 会把正确的拒绝变成 teardown 失败）
- 清理只删本用例造的种子：路径里必须引用造数/查数步骤的输出
- DELETE 永远只出现在 teardown，或作为主步骤删自己造的种子
- 找不到可靠的生产者/查询/清理接口时返回 ``None``，调用方继续标人工
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any

from ai.pipeline.intents import WRITE_METHODS

#: 路径里的 ``{id}``
_PATH_PARAM_RE = re.compile(r"\{([^}/]+)\}")

#: 响应列表里像主键的字段（areaId / id / user_id）。
#: 大小写敏感：``valid``、``paid`` 这种词也以 id 结尾，不能误判成主键。
_ID_FIELD_RE = re.compile(r"(^id$|[a-z]Id$|_id$)")

#: 响应列表里像名称的字段（areaName / title / name）
_NAME_FIELD_RE = re.compile(r"(name|title)", re.IGNORECASE)

#: 分页查询的默认参数
_PAGE_PARAMS = {"current": 1, "size": 100}

#: 造数名称前缀（中文，便于在环境里一眼看出是自动化造的）
_DEFAULT_PREFIX = "AI造数"
_UPDATE_PREFIX = "AI造数改"

#: teardown/主步骤里引用「查数据」的标志
_LOOKUP_STEP_ID = "查数据"
_CREATE_STEP_ID = "造数据"
_CLEANUP_STEP_ID = "清理数据"


@dataclass
class DataChain:
    """一条写用例的数据生命周期。"""

    setups: list[dict[str, Any]] = field(default_factory=list)
    teardowns: list[dict[str, Any]] = field(default_factory=list)
    #: 改写后的主步骤路径（None = 不改）
    main_path: str | None = None
    #: 改写后的主步骤请求体（None = 不改）
    main_data: Any = None
    #: 主步骤自己就删掉了种子数据（不需要 teardown）
    self_cleaning: bool = False
    notes: list[str] = field(default_factory=list)


# ============================================================================
# 接口识别
# ============================================================================


def _norm(path: Any) -> str:
    """``/admin/area/{id}`` 和 ``/admin/area/110000000000`` 都归一成模板。"""
    text = re.sub(r"\{[^}]+\}", "{}", str(path or "").strip())
    return re.sub(r"/\d+(?=/|$)", "/{}", text)


def _collection_path(path: Any) -> str:
    """``/admin/area/{id}`` → ``/admin/area``（也兼容已解析的真实 id）。"""
    text = str(path or "").rstrip("/")
    text = re.sub(r"/\{[^}]+\}$", "", text)
    return re.sub(r"/\d+$", "", text)


def _entity_of(collection_path: Any) -> str:
    """集合路径的实体名：``/sys/user`` → ``user``（拼 ``<实体>Id`` 用）。"""
    text = str(collection_path or "").rstrip("/")
    if not text:
        return ""
    tail = text.rsplit("/", 1)[-1]
    # ``{id}`` / 纯数字这类占位段不是实体名
    if not tail or tail.startswith("{") or tail.isdigit():
        return ""
    return tail


def _find(
    endpoints: list[Any], method: str, norm_path: str
) -> Any | None:
    for ep in endpoints:
        if (
            str(getattr(ep, "method", "") or "").upper() == method
            and _norm(getattr(ep, "path", "")) == norm_path
        ):
            return ep
    return None


def _match_endpoint(path: Any, endpoints: list[Any], method: str = "") -> Any | None:
    """按规范化路径找回 OpenAPI 里的接口定义（拿它的模板路径）。"""
    target = _norm(path)
    for ep in endpoints:
        if str(getattr(ep, "method", "") or "").upper() not in ("", method.upper()):
            continue
        if _norm(getattr(ep, "path", "")) == target:
            return ep
    return None


def _template_path(main_step: dict[str, Any], endpoints: list[Any]) -> str:
    """主步骤里可能是已经解析过的真实 id，规划时要用回 ``{id}`` 模板。"""
    method = str(main_step.get("method") or "").upper()
    ep = _match_endpoint(main_step.get("path"), endpoints, method)
    if ep is not None:
        return str(ep.path or "")
    return str(main_step.get("path") or "")


def _find_producer(base: str, endpoints: list[Any]) -> Any | None:
    """集合路径上的 POST = 生产者。"""
    return _find(endpoints, "POST", base)


def _find_lookup(base: str, endpoints: list[Any]) -> Any | None:
    """优先 ``/list``（返回平铺数组），其次 ``/page``（返回 records）。"""
    for suffix in ("/list", "/page"):
        ep = _find(endpoints, "GET", base + suffix)
        if ep is not None:
            return ep
    return None


def _find_cleanup(base: str, endpoints: list[Any]) -> tuple[Any | None, str]:
    """返回 ``(清理接口, 风格)``；风格 = path / body / ""。"""
    item = _find(endpoints, "DELETE", base + "/{}")
    if item is not None:
        return item, "path"
    item = _find(endpoints, "DELETE", base)
    if item is not None:
        return item, "body"
    return None, ""


# ============================================================================
# 字段推断
# ============================================================================


def _response_records(ep: Any) -> tuple[list[Any], str]:
    """从响应示例里找列表，返回 ``(列表, 模板里引用它的前缀)``。

    例如 ``data``（平铺数组）或 ``data.records``（分页对象）。
    """
    body = getattr(ep, "response_example", None)
    if not isinstance(body, dict):
        return [], ""
    data = body.get("data")
    if isinstance(data, list):
        return data, "data"
    if isinstance(data, dict) and isinstance(data.get("records"), list):
        return data["records"], "data.records"
    return [], ""


def _first_mapping(records: list[Any]) -> dict[str, Any] | None:
    for item in records:
        if isinstance(item, dict):
            return item
    return None


#: 看起来像外键、不能当主键用的字段
_FOREIGN_KEY_FIELDS = {"parentid", "shopid", "userid", "createuserid", "updateuserid"}


def _infer_id_field(
    sample: dict[str, Any] | None,
    name_field: str = "",
    cleanup: Any = None,
    entity: str = "",
) -> str:
    """推断「用哪个字段能把刚造的记录的唯一标识取出来」。

    优先级：DELETE 路径参数名（最强证据）> ``*Name`` 派生的 ``*Id``
    （areaName→areaId）> 集合路径实体名派生的 ``<实体>Id``
    （``/sys/user`` → userId）> 响应里第一个 ``*Id``（跳过 parentId 这类外键）。

    ``entity`` 那条是为 ``/sys/user`` 这类接口准备的：``userId`` 在
    外键黑名单里（很多接口的记录会带创建人 ``userId``），但这条记录
    自己的主键也叫 ``userId``。只有**路径实体名**对得上时才认它，
    不会把创建人 id 误当主键删掉别的记录。
    """
    if not sample:
        return ""
    for param in _PATH_PARAM_RE.findall(str(getattr(cleanup, "path", "") or "")):
        if param in sample:
            return param
    if name_field.endswith("Name"):
        derived = name_field[: -len("Name")] + "Id"
        if derived in sample:
            return derived
    if entity:
        want = entity.lower() + "id"
        for key in sample:
            if str(key).lower() == want:
                return str(key)
    for key in sample:
        text = str(key)
        if (
            _ID_FIELD_RE.search(text)
            and text.lower() not in _FOREIGN_KEY_FIELDS
        ):
            return text
    return ""


#: 能被造数改掉、但「改了就不像标签」的字段：格式化/状态/时间类。
#: 拿它们当检索键会把请求改坏（手机号变乱码直接被服务端拒），
#: 或者改了也还是查不准（状态、版本号全表都一样）。
_NOT_LABEL_FIELDS = {
    "password", "passwd", "pwd", "mobile", "phone", "tel", "email",
    "postcode", "status", "version", "level", "sort", "timestamp",
    "createtime", "updatetime", "commonaddr", "defaultaddr",
}


def _body_keys(producer: Any) -> list[str]:
    """造数请求体的字段名（保持文档里的顺序）。"""
    names: list[str] = []
    for param in getattr(producer, "body_params", None) or []:
        name = str(getattr(param, "name", "") or "")
        if name and name not in names:
            names.append(name)
    if names:
        return names
    body = getattr(producer, "body", None)
    if isinstance(body, dict):
        return [str(key) for key in body]
    return []


def _looks_unsearchable(key: str) -> bool:
    """字段能不能当「造个数、再按它查回来」的检索键。"""
    text = str(key)
    if _ID_FIELD_RE.search(text):
        return True
    return text.lower() in _NOT_LABEL_FIELDS


def _infer_name_field(
    sample: dict[str, Any] | None,
    producer: Any = None,
) -> str:
    """挑一个「造数时写进去、查数时按它找回来」的字段。

    优先 ``*name*`` / ``*title*``（areaName、username、title）。没有这类
    字段的接口（``/user/addr`` 只有 receiver / addr / mobile）退到
    **造数请求体里第一个字符串字段** —— 前提是它同时出现在列表响应里，
    且不是 id / 状态 / 时间 / 手机号这类改了会出问题的字段。
    """
    if not sample:
        return ""
    for key in sample:
        if _NAME_FIELD_RE.search(str(key)):
            return str(key)
    for key in _body_keys(producer):
        if key not in sample or not isinstance(sample.get(key), str):
            continue
        if _looks_unsearchable(key):
            continue
        return key
    return ""


def _trusted_example_body(ep: Any, ctx: Any) -> dict[str, Any] | None:
    """从现役正例里取一份能跑的请求体。

    文档示例（``parentId: 0`` / ``level: 0``）不是合法请求，拿它造数会被
    服务端拒绝。现役用例里已有通过校验的真实请求体，优先用它。
    """
    for example in ctx.examples_for(ep, limit=8):
        if not isinstance(example, dict):
            continue  # 现役用例读坏了也不能让造数规划崩掉
        body = example.get("data")
        if not isinstance(body, dict) or not body:
            continue
        marks = {str(m).lower() for m in (example.get("marks") or [])}
        if "needs_manual_setup" in marks or "negative" in marks:
            continue
        return copy.deepcopy(body)
    body = getattr(ep, "body", None)
    return copy.deepcopy(body) if isinstance(body, dict) and body else None


def _main_body_for_post(
    main_step: dict[str, Any],
    producer: Any,
    name_field: str,
    ctx: Any,
    *,
    prefer_main_body: bool = False,
) -> dict[str, Any] | None:
    """POST 造数用哪份请求体。

    默认沿用「现役正例 > 文档示例」：文档示例里的 ``parentId: 0`` /
    ``level: 0`` 实测会被服务端拒绝。但智能体通过 ``create_case`` 传进来的
    ``request_data`` 是它按接口文档准备的报文，调用方明确要求时可以优先用它
    （``prefer_main_body=True``）；它缺失、不是对象或者没有名称字段时仍退回
    现役正例 / 文档示例。
    """
    data = main_step.get("data")
    if (
        prefer_main_body
        and isinstance(data, dict)
        and data
        and name_field in data
    ):
        return copy.deepcopy(data)
    return _trusted_example_body(producer or main_step, ctx)


def _lookup_params(ep: Any, ctx: Any, name_field: str, fixed_expr: str) -> dict[str, Any]:
    """查数请求参数：现役正例的参数最可靠，分页接口再补 current/size。"""
    params: dict[str, Any] = {}
    for example in ctx.examples_for(ep, limit=8):
        if not isinstance(example, dict):
            continue
        sample = example.get("params")
        if isinstance(sample, dict) and sample:
            marks = {str(m).lower() for m in (example.get("marks") or [])}
            if "needs_manual_setup" in marks or "negative" in marks:
                continue
            params = copy.deepcopy(sample)
            break

    if str(ep.path or "").endswith("/page"):
        params.setdefault("current", _PAGE_PARAMS["current"])
        # 现役用例多半写 size=10。造的那条排在第 11 位以后就查不到 id，
        # 清理会失败并把数据留在环境里 —— 查数这步宁可多拉一点。
        try:
            params["size"] = max(int(params.get("size") or 0), _PAGE_PARAMS["size"])
        except (TypeError, ValueError):
            params["size"] = _PAGE_PARAMS["size"]

    # 文档里 ``area`` 这种对象型查询参数是生成器的产物，发出去服务端不认
    params = {
        key: value
        for key, value in params.items()
        if not isinstance(value, (dict, list))
    }

    # 有同名过滤参数就用造数名称过滤，减少全表扫描
    query_names = {
        name
        for name in (
            str(getattr(p, "name", "") or "") for p in (getattr(ep, "query", None) or [])
        )
        if name
    }
    if name_field and name_field in query_names:
        params[name_field] = fixed_expr
    return params


def _fixed_key(
    project: str, module: str, field: str, suffix: str = ""
) -> str:
    """固定值名字沿用现役约定：``<项目>_<模块>_<字段>``。

    同一个用例里 ``fixed`` 只生成一次，跨用例会重置，所以名字重复不影响隔离。
    """
    module = str(module or "ai_generated").replace(".", "_")
    key = f"{project}_{module}_{field}"
    return f"{key}_{suffix}" if suffix else key


def _module_of(ep: Any, ctx: Any) -> str:
    try:
        name = ctx.module_for(ep)
    except Exception:  # noqa: BLE001 - 没有 ctx 方法时退回通用模块名
        name = ""
    return str(name or getattr(ctx, "module_override", "") or "ai_generated")


def _chain_settings(
    project: str,
    module: str,
    name_field: str,
    id_field: str,
    list_path: str,
) -> dict[str, Any]:
    """一条链路里造数、改名、查 id 共用的三个表达式。"""
    seed_key = _fixed_key(project, module, name_field, "seed")
    update_key = _fixed_key(project, module, name_field, "update")
    return {
        "seed_key": seed_key,
        "seed_expr": f"{{{{tools.fixed('{seed_key}', '{_DEFAULT_PREFIX}')}}}}",
        "update_expr": f"{{{{tools.fixed('{update_key}', '{_UPDATE_PREFIX}')}}}}",
        "id_expr": _id_expr(
            _LOOKUP_STEP_ID,
            list_path,
            name_field,
            seed_key,
            id_field,
            _DEFAULT_PREFIX,
        ),
        "main_path": None,
    }


def _body_payload(ep: Any, id_value: str) -> Any | None:
    """按接口文档的请求体形状包一个 id；形状不明就返回 None。"""
    sample = getattr(ep, "body", None) if ep is not None else None
    if isinstance(sample, list):
        return [id_value]
    if isinstance(sample, dict):
        list_key = next(
            (str(key) for key, value in sample.items() if isinstance(value, list)),
            "",
        )
        return {list_key: [id_value]} if list_key else None
    return None


def _id_expr(
    lookup_id: str,
    list_path: str,
    name_field: str,
    fixed_key: str,
    id_field: str,
    prefix: str,
) -> str:
    return (
        f"{{{{tools.find_id_by_fixed({lookup_id}.{list_path}, "
        f'"{name_field}", "{fixed_key}", "{id_field}", "{prefix}")}}}}'
    )


def _success_asserts(ctx: Any) -> list[dict[str, Any]]:
    """造数/查数/清理步骤的断言：接口成功即可。

    拿不到项目成功码时退回 ``success == true`` —— 两条都拿不到说明项目
    契约没解析出来，此时宁可不造数（调用方会标人工），也不写空断言。
    """
    contract = getattr(ctx, "contract", {})
    if not isinstance(contract, dict):
        contract = {}
    code = str(contract.get("success_code") or "").strip()
    if code:
        return [{"type": "equal", "field": "code", "expected": code}]
    return []


def _with_success_asserts(step: dict[str, Any], ctx: Any) -> dict[str, Any]:
    asserts = _success_asserts(ctx)
    if asserts:
        step["assert"] = asserts
    return step


# ============================================================================
# 构造步骤
# ============================================================================


def _create_step(producer: Any, body: dict[str, Any], ctx: Any) -> dict[str, Any]:
    return _with_success_asserts(
        {
            "id": _CREATE_STEP_ID,
            "operation_type": "api",
            "description": f"造数：{producer.display_name}",
            "path": producer.path,
            "method": "POST",
            "data": body,
        },
        ctx,
    )


def _lookup_step(
    lookup: Any,
    list_path: str,
    params: dict[str, Any],
    ctx: Any,
) -> dict[str, Any]:
    step: dict[str, Any] = {
        "id": _LOOKUP_STEP_ID,
        "operation_type": "api",
        "description": f"查 id：{lookup.display_name}",
        "path": lookup.path,
        "method": "GET",
    }
    if params:
        step["params"] = params
    _with_success_asserts(step, ctx)
    # 查数只证明接口成功；记录列表引用路径，供调用方排查
    step["description"] = f"{step['description']}（{list_path}）"
    return step


def _delete_step(
    cleanup: Any,
    style: str,
    id_value: str,
    ctx: Any,
) -> dict[str, Any] | None:
    """构造清理步骤；风格不明确时返回 None（宁可人工，也不乱删）。"""
    step: dict[str, Any] = {
        "id": _CLEANUP_STEP_ID,
        "operation_type": "api",
        "description": f"清理：{cleanup.display_name}",
        "method": "DELETE",
    }
    if style == "path":
        params = _PATH_PARAM_RE.findall(str(cleanup.path or ""))
        if len(params) != 1:
            return None
        step["path"] = _PATH_PARAM_RE.sub(id_value, str(cleanup.path), count=1)
    elif style == "body":
        # 批量删除的请求体形状必须和接口一致：``[...]`` 还是 ``{"ids": [...]}``。
        # 猜错会把清理请求发成非法报文，teardown 失败反而盖住真正的结果。
        payload = _body_payload(cleanup, id_value)
        if payload is None:
            return None
        step["path"] = cleanup.path
        step["data"] = payload
    else:
        return None
    return _with_success_asserts(step, ctx)


# ============================================================================
# 对外入口
# ============================================================================


def plan_write_chain(
    main_step: dict[str, Any],
    endpoints: list[Any],
    ctx: Any,
    *,
    positive: bool = True,
    prefer_main_body: bool = False,
) -> DataChain | None:
    """给一条写主步骤规划数据生命周期。

    Args:
        main_step: 主步骤（method/path/data）
        endpoints: **完整**接口清单（要能找到兄弟接口）
        ctx: ``case_gen.ProjectContext``
        positive: 只给正例造数；反例返回 None，继续人工
        prefer_main_body: POST 造数优先用主步骤自己的请求体（``create_case``
            用；生成器仍用现役正例，避免文档占位值被服务端拒绝）

    Returns:
        ``DataChain``；给不出可靠链路时返回 None（调用方标 needs_manual_setup）
    """
    if not positive:
        return None

    method = str(main_step.get("method") or "").upper()
    if method not in WRITE_METHODS:
        return None

    # 主步骤的 path 可能已经被解析成真实 id（``/admin/area/110000000000``），
    # 用它的模板形式来定位兄弟接口。
    path = _template_path(main_step, endpoints)
    if not path:
        return None

    base = _collection_path(path)
    producer = _find_producer(base, endpoints)
    lookup = _find_lookup(base, endpoints)
    cleanup, cleanup_style = _find_cleanup(base, endpoints)
    if lookup is None:
        return None

    records, list_path = _response_records(lookup)
    sample = _first_mapping(records)
    if not list_path:
        return None
    name_field = _infer_name_field(sample, producer)
    id_field = _infer_id_field(sample, name_field, cleanup, entity=_entity_of(base))
    if not id_field or not name_field:
        return None

    project = str(getattr(ctx, "project", "") or "mall4j")
    module = _module_of(producer or main_step, ctx)

    # ---------------- POST：主步骤就是生产者 ----------------
    if method == "POST":
        body = _main_body_for_post(
            main_step,
            producer,
            name_field,
            ctx,
            prefer_main_body=prefer_main_body,
        )
        if body is None or name_field not in body:
            # 名称字段不在请求体里，就无法用名字把记录查回来
            return None
        settings = _chain_settings(project, module, name_field, id_field, list_path)
        body[name_field] = settings["seed_expr"]
        # 没有清理接口就不是完整链路 —— 宁可不造数，也不能留下脏数据
        if cleanup is None:
            return None
        delete = _delete_step(cleanup, cleanup_style, settings["id_expr"], ctx)
        if delete is None:
            return None
        chain = DataChain(
            main_data=body,
            teardowns=[
                _lookup_step(
                    lookup,
                    list_path,
                    _lookup_params(
                        lookup, ctx, name_field, settings["seed_expr"]
                    ),
                    ctx,
                ),
                delete,
            ],
        )
        chain.notes.append("POST 主步骤造数，teardown 用固定名称查 id 后删除")
        return chain

    # ---------------- PUT / PATCH：先造种子，再改 ----------------
    if producer is None:
        return None
    body = _trusted_example_body(producer, ctx)
    # 智能体（``create_case``）给的 PUT 报文是照着接口文档准备的真实请求，
    # 比文档示例里的占位值（``userId: "string"``）可靠。拿它当造数的种子
    # 报文，但要去掉 id —— 造数要新建一条，不能带上别的主键。
    main_base = main_step.get("data")
    if prefer_main_body and isinstance(main_base, dict) and main_base:
        seed = copy.deepcopy(main_base)
        seed.pop(id_field, None)
        if name_field in seed:
            body = seed
    if body is None or name_field not in body:
        return None
    settings = _chain_settings(project, module, name_field, id_field, list_path)
    body[name_field] = settings["seed_expr"]

    # 主步骤的请求体必须能改到「自己造的那一条」。文档示例里可能是
    # ``areaId: 0`` 这种占位值（实测 A00001），所以以造数请求体为底，
    # 只替换 id 和名称。
    main_data = copy.deepcopy(body)
    main_data[name_field] = settings["update_expr"]
    main_ep = _match_endpoint(main_step.get("path"), endpoints, method)
    put_fields = {
        str(p.name) for p in (getattr(main_ep, "body_params", None) or [])
    }
    if id_field in put_fields or (not put_fields and id_field in body):
        main_data[id_field] = settings["id_expr"]
    elif _PATH_PARAM_RE.search(path):
        settings["main_path"] = _PATH_PARAM_RE.sub(
            settings["id_expr"], path, count=1
        )
    else:
        return None

    # PUT/PATCH 造了种子，必须能清理掉，否则不挂链路
    if cleanup is None:
        return None
    delete = _delete_step(cleanup, cleanup_style, settings["id_expr"], ctx)
    if delete is None:
        return None
    chain = DataChain(
        setups=[
            _create_step(producer, body, ctx),
            _lookup_step(
                lookup,
                list_path,
                _lookup_params(lookup, ctx, name_field, settings["seed_expr"]),
                ctx,
            ),
        ],
        teardowns=[delete],
        main_path=settings["main_path"],
        main_data=main_data,
    )
    chain.notes.append("PUT/PATCH 先造种子，主步骤改的是自己造的 id")
    return chain


def plan_delete_chain(
    main_step: dict[str, Any],
    endpoints: list[Any],
    ctx: Any,
    *,
    positive: bool = True,
) -> DataChain | None:
    """DELETE 主步骤：造种子 → 主步骤删种子。"""
    if not positive:
        return None
    method = str(main_step.get("method") or "").upper()
    if method != "DELETE":
        return None

    path = _template_path(main_step, endpoints)
    if not path:
        return None
    base = _collection_path(path)
    producer = _find_producer(base, endpoints)
    lookup = _find_lookup(base, endpoints)
    if producer is None or lookup is None:
        return None

    records, list_path = _response_records(lookup)
    sample = _first_mapping(records)
    name_field = _infer_name_field(sample, producer)
    delete_ep = _match_endpoint(main_step.get("path"), endpoints, "DELETE")
    id_field = _infer_id_field(sample, name_field, delete_ep, entity=_entity_of(base))
    if not list_path or not id_field or not name_field:
        return None

    body = _trusted_example_body(producer, ctx)
    if body is None or name_field not in body:
        return None
    project = str(getattr(ctx, "project", "") or "mall4j")
    module = _module_of(producer, ctx)
    settings = _chain_settings(project, module, name_field, id_field, list_path)
    body[name_field] = settings["seed_expr"]

    chain = DataChain(
        setups=[
            _create_step(producer, body, ctx),
            _lookup_step(
                lookup,
                list_path,
                _lookup_params(lookup, ctx, name_field, settings["seed_expr"]),
                ctx,
            ),
        ],
        self_cleaning=True,
    )

    if len(_PATH_PARAM_RE.findall(path)) == 1:
        chain.main_path = _PATH_PARAM_RE.sub(
            settings["id_expr"], path, count=1
        )
        chain.notes.append("DELETE 主步骤删的是本用例造的种子，不需要额外 teardown")
    else:
        # 按请求体删除：形状必须和接口一致，猜错会发非法报文
        main_ep = _match_endpoint(main_step.get("path"), endpoints, method)
        payload = _body_payload(main_ep, settings["id_expr"])
        if payload is None:
            return None
        chain.main_data = payload
        chain.notes.append("DELETE 通过请求体删除本用例造的种子")
    return chain


def plan_data_chain(
    main_step: dict[str, Any],
    endpoints: list[Any],
    ctx: Any,
    *,
    positive: bool = True,
    prefer_main_body: bool = False,
) -> DataChain | None:
    """按主步骤方法分派到 POST/PUT/DELETE 的规划器。"""
    method = str(main_step.get("method") or "").upper()
    if method == "DELETE":
        return plan_delete_chain(
            main_step, endpoints, ctx, positive=positive
        )
    return plan_write_chain(
        main_step,
        endpoints,
        ctx,
        positive=positive,
        prefer_main_body=prefer_main_body,
    )


#: 用例名里出现这些词，基本可以断定是反例
_NEGATIVE_NAME_HINTS = (
    "缺少", "必填", "非法", "空值", "边界", "不存在", "未登录", "无权限",
    "越权", "错误", "类型错误", "重复",
)


def _looks_negative(testcase: dict[str, Any], writes: list[dict[str, Any]]) -> bool:
    """名字或断言表示「期望被拒绝」时返回 True。

    LLM 偶尔会漏打 ``negative`` 标签。如果只看标签，就会把一条反例
    当成正例来造数，甚至把反例的请求体替换成合法数据 —— 反例直接被
    改成正例，覆盖度也会虚高。这里再按名字和断言兜一层。
    """
    name = str(testcase.get("name") or "")
    if any(hint in name for hint in _NEGATIVE_NAME_HINTS):
        return True
    for step in writes:
        for item in step.get("assert") or []:
            if not isinstance(item, dict):
                continue
            atype = str(item.get("type") or "").strip().lower().replace(" ", "_")
            field = str(item.get("field") or "")
            expected = item.get("expected")
            if (
                atype in ("status_code", "status", "http_status")
                and str(expected) in {"401", "403", "404"}
            ):
                return True
            if field == "success" and (
                atype in ("is_false", "false") or expected is False
            ):
                return True
            if field == "code" and atype in ("not_equal", "!="):
                return True
            if (
                field == "code"
                and atype in ("equal", "equals", "==")
                and str(expected) not in {"", "00000", "200", "0", "None"}
            ):
                return True
    return False


def is_positive_case(marks: list[str], dimensions: set[str] | None = None) -> bool:
    """用例是否「**期望**服务端真的写成功」。

    只有这种用例才值得造数：反例、权限、边界用例可能根本没写进去，
    无条件清理反而会把正确的拒绝变成 teardown 失败。
    """
    lowered = {str(m).lower() for m in marks}
    if lowered & {"negative", "boundary", "invalid", "no_auth", "permission"}:
        return False
    dims = dimensions if dimensions is not None else set()
    if dims & {
        "A1", "A2", "A3", "A4", "A5", "A6", "A7",
        "C1", "C2", "C3",
        "F1", "F2", "F3", "F4",
    }:
        return False
    return True


def attach_chain(
    testcase: dict[str, Any],
    endpoints: list[Any],
    ctx: Any,
    *,
    dimensions: set[str] | None = None,
    prefer_main_body: bool = False,
) -> str:
    """给一条用例补造数链路，返回结果码（给日志/描述用）。

    - ``skipped:no_write`` —— 没有写主步骤，不动
    - ``skipped:negative`` —— 反例，不造数
    - ``skipped:has_chain`` —— 模型自己写了 setups/teardowns，尊重它
    - ``failed:<原因>`` —— 给不出安全链路，调用方继续标人工
    - ``ok:<说明>`` —— 已挂上
    """
    # 用例结构可能是模型/手写产物：``steps`` 不是列表、``marks`` 不是列表时
    # 直接迭代会抛 TypeError，把整批用例生成打断。
    raw_steps = testcase.get("steps")
    steps = [
        s
        for s in (raw_steps if isinstance(raw_steps, (list, tuple)) else [])
        if isinstance(s, dict)
    ]
    writes = [
        s for s in steps if str(s.get("method") or "").upper() in WRITE_METHODS
    ]
    if not writes:
        return "skipped:no_write"
    if len(writes) != 1 or len(steps) != 1:
        return "failed:multi_step_unsupported"
    raw_marks = testcase.get("marks")
    if isinstance(raw_marks, str):
        marks = [raw_marks]
    elif isinstance(raw_marks, (list, tuple, set)):
        marks = [str(m) for m in raw_marks]
    else:
        marks = []
    if not is_positive_case(marks, dimensions):
        return "skipped:negative"
    if _looks_negative(testcase, writes):
        return "skipped:negative"
    if testcase.get("setups") or testcase.get("teardowns"):
        return "skipped:has_chain"

    try:
        chain = plan_data_chain(
            writes[0],
            endpoints,
            ctx,
            positive=True,
            prefer_main_body=prefer_main_body,
        )
    except Exception as exc:  # noqa: BLE001 - 规划失败不能把生成整个打断
        return f"failed:error:{exc}"
    if chain is None:
        return "failed:no_chain"

    apply_chain(writes[0], chain)

    # 和现役用例保持一致：前置/后置显式带鉴权头。
    # 运行时会合并默认头，但写进 Excel 后别人一眼能看出这些步骤是登录态。
    token = f"{{{{{getattr(ctx, 'project', '') or 'mall4j'}.token}}}}"
    for step in list(chain.setups) + list(chain.teardowns):
        headers = step.setdefault("headers", {})
        headers.setdefault("Authorization", token)

    if chain.setups:
        testcase["setups"] = chain.setups
    if chain.teardowns:
        testcase["teardowns"] = chain.teardowns
    return "ok:" + ("；".join(chain.notes) or "已挂造数链路")


def apply_chain(main_step: dict[str, Any], chain: DataChain) -> None:
    """把链路写回主步骤（路径 / 请求体）。"""
    if chain.main_path:
        main_step["path"] = chain.main_path
    if chain.main_data is not None:
        main_step["data"] = chain.main_data


#: 框架变量占位符 ``{{...}}``（造数产出 / tools.* / 项目 token）
_CASE_VARIABLE_RE = re.compile(r"\{\{[^{}]+\}\}")


def teardown_targets_case_data(teardown: dict[str, Any]) -> bool:
    """后置清理是否指向本用例自己造的数据。

    判据只有一条：路径 / 查询参数 / 请求体里引用了 ``{{...}}`` 变量。
    硬编码的 ``DELETE /admin/brand/100`` 删的是环境里原有记录，不是
    本用例刚造出来的那条 —— 这种 teardown 必须当危险操作处理。
    """
    if not isinstance(teardown, dict):
        return False
    raw = " ".join(
        str(teardown.get(key) or "") for key in ("path", "params", "data")
    )
    return bool(_CASE_VARIABLE_RE.search(raw))


def unsafe_teardowns(case: dict[str, Any]) -> list[dict[str, Any]]:
    """列出会把固定数据当成自己数据删/改的写后置。

    ``DELETE`` 是最危险的（不可逆），``PUT`` / ``POST`` / ``PATCH`` 用于
    ``/cancel/{id}`` 这类"改状态"清理时同样会碰到真实记录，一并检查。
    """
    raw = case.get("teardowns") if isinstance(case, dict) else None
    items = raw if isinstance(raw, (list, tuple)) else []
    return [
        item
        for item in items
        if isinstance(item, dict)
        and str(item.get("method") or "").upper() in WRITE_METHODS
        and not teardown_targets_case_data(item)
    ]


def write_is_self_cleaning(case: dict[str, Any]) -> bool:
    """写用例是不是「主步骤自己删掉了造的数据」。

    这种用例可以没有 teardowns：DELETE 路径里引用了 setup 的输出，
    删的就是本用例刚造的那一条。
    """
    raw_steps = case.get("steps")
    steps = [
        s
        for s in (raw_steps if isinstance(raw_steps, (list, tuple)) else [])
        if isinstance(s, dict)
    ]
    writes = [
        s for s in steps if str(s.get("method") or "").upper() in WRITE_METHODS
    ]
    if not writes:
        return False
    if any(str(s.get("method") or "").upper() != "DELETE" for s in writes):
        return False
    raw_setups = case.get("setups")
    setup_ids = [
        str(item.get("id"))
        for item in (raw_setups if isinstance(raw_setups, (list, tuple)) else [])
        if isinstance(item, dict) and item.get("id")
    ]
    if not setup_ids:
        return False
    for step in writes:
        text = " ".join(
            str(x)
            for x in (step.get("path"), step.get("params"), step.get("data"))
            if x is not None
        )
        if not any(
            re.search(rf"(?<![\w一-鿿]){re.escape(sid)}\.", text)
            for sid in setup_ids
        ):
            return False
    return True
