"""接口探针：生成用例前先问接口本人。

**写探针的数据足迹**：``--include-write`` 允许探 POST/PUT/PATCH，改动不会
自动清理（DELETE 被禁）。环境闸只保证「不在准生产上写」，所以每次写探针
成功都记一条足迹（测点 / 请求体 / 响应 id / 建议的删除接口），命令行跑完
摊平成 ``*_footprints.json`` —— 留线索，不替人删。

**POST 和 PUT/PATCH 必须分开**：只有 POST 是「新建」，能按 DELETE 清；
PUT/PATCH 改的是本来就存在的记录，DELETE 会删掉原有数据，只记进
「改动的数据」供人工还原，绝不进清理计划。

**为什么必须有这一步**

维度矩阵能保证「该测什么不遗漏」，但保证不了「接口实际怎么表现」。
在没有探针之前，生成器只能猜：

- 未登录到底是 401，还是 HTTP 200 + 业务错误码？
- 少传必填参数，服务端真的会拒吗？错误码是多少？
- 查一个不存在的 id，是报错还是返回空？

猜的结果就是大批 ``needs_manual_setup`` —— 智能体没解决问题，只是把问题
贴了个标签。探针把这些「假设」变成「实测事实」：断言直接用真实错误码，
没校验的接口则标成 ``defect_candidate``，交给执行阶段去暴露。

**安全边界**

- 默认只探 GET：只读，无副作用
- 写接口需要显式 ``--include-write``；DELETE 永远不探
- 每条用例只探一次，不做批量压测
- 探针失败（超时/网络）不影响生成，只记录 error
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
import time
from dataclasses import dataclass, field as dc_field
from typing import Any

from ai.pipeline.intents import WRITE_METHODS
from tools.importer.models import Endpoint

#: 探针超时（秒）。生成阶段不该被一个慢接口拖住。
PROBE_TIMEOUT = 15

#: 永远不探的方法 —— 删除无法回滚
FORBIDDEN_METHODS = {"DELETE"}

#: 允许跑写探针的环境。
#:
#: 写探针会把请求真的发出去，**没有对应的清理步骤**（DELETE 被禁）。
#: 在 online / pre 上跑等于往准生产环境写脏数据，所以只放行测试环境。
#: 这是硬闸，不靠调用方自觉。
WRITE_PROBE_ENVS = frozenset({"test", "dev"})

#: 不存在的 id 用这个值，足够大且不会撞上真实数据
NONEXISTENT_ID = 999999999


@dataclass
class ProbeOutcome:
    """一次探针的实测结果。"""

    dimension: str
    label: str
    method: str
    path: str
    kind: str = ""
    #: 测点作用的字段名（A1/A2 等可能有多个字段，索引时要能区分）
    field: str = ""
    request: dict[str, Any] = dc_field(default_factory=dict)
    status_code: int = 0
    code: str = ""
    success: bool | None = None
    #: 响应体里到底有没有 success 字段 —— 没有就不能断言它
    has_success: bool = False
    message: str = ""
    data_present: bool = False
    #: 响应体里 data 字段是不是显式 null（区别于 {} / [] / 缺字段）
    data_is_none: bool = False
    error: str = ""
    #: 这次探针**新建出来**的数据（只有 POST 成功才有）。每项含测点、请求和
    #: 可能的 id。写探针没有自动清理，这份清单就是事后能清理的唯一线索。
    created: list[dict[str, Any]] = dc_field(default_factory=list)
    #: 这次探针**改动了已有数据**（PUT/PATCH 成功才有）。这些记录本来就存在，
    #: **不能**用 DELETE 清 —— 删掉的是别人/原有的数据。只留请求体供人工还原。
    modified: list[dict[str, Any]] = dc_field(default_factory=list)
    #: 能删掉 ``created`` 那条数据的删除接口（``DELETE /path``），找不到就是空串
    delete_endpoint: str = ""

    @property
    def rejected(self) -> bool:
        """请求是否被明确拒绝。"""
        if self.error:
            return False
        if self.status_code in (401, 403):
            return True
        if self.success is False:
            return True
        return False

    @property
    def behavior(self) -> str:
        if self.error:
            return f"error:{self.error}"
        if self.status_code in (401, 403):
            return f"http:{self.status_code}"
        if self.rejected:
            return f"rejected:{self.code or self.message or 'success=false'}"
        if self.success is True:
            return "success"
        # 没拿到 success 字段、也不是 2xx 业务成功 —— 这是「没测出结论」，
        # 不能当成「服务端接受」，否则反例会被改写成假绿用例。
        return f"unknown:http{self.status_code or 0}"

    def to_dict(self) -> dict[str, Any]:
        data = {
            "维度": self.dimension,
            "测点": self.label,
            "行为": self.behavior,
            "状态码": self.status_code,
            "code": self.code,
            "success": self.success,
            "有success字段": self.has_success,
            "有数据": self.data_present,
            "data为null": self.data_is_none,
            "请求": self.request,
        }
        if self.field:
            data["字段"] = self.field
        if self.message:
            data["message"] = self.message
        if self.error:
            data["error"] = self.error
        if self.created:
            # 写探针新建出来的数据 —— 放在结果里，跑完就能看到要清什么
            data["造的数据"] = self.created
            if self.delete_endpoint:
                data["清理接口"] = self.delete_endpoint
        if self.modified:
            # 改的是已有记录：DELETE 清不了（会删掉原有的数据），只能人工还原
            data["改动的数据"] = self.modified
        return data


class ProbeClient:
    """按项目配置登录并发真实请求。登录只做一次。"""

    def __init__(self, project: str, env: str = "test") -> None:
        self.project = project
        self.env = env
        self._config: dict[str, Any] | None = None
        self._profile: Any = None
        self._session: Any = None
        self._token: str | None = None
        self._login_error = ""
        self._failed = False

    # ---------------- 初始化 ----------------

    def _ensure_ready(self) -> None:
        if self._session is not None:
            return
        if self._failed:
            raise RuntimeError(self._login_error or "探针初始化失败")

        from testcases.project_login_handler import ProjectLoginHandler
        from utils.config.config_manager import ConfigManager
        from utils.config.project_profile import ProjectRegistry
        from utils.http.request_util import make_session

        loaded = ConfigManager.get_project_env_config(self.project, self.env)
        if not loaded:
            raise RuntimeError(
                f"项目 {self.project!r} 在环境 {self.env!r} 下没有配置"
            )
        self._config = loaded[self.project]
        self._profile = ProjectRegistry.get(self.project, self.env)
        self._session = make_session(profile=self._profile)

        try:
            self._token = ProjectLoginHandler().login_if_needed(
                self.project, self._config, self.env
            )
        except Exception as exc:  # noqa: BLE001 - 登录失败要让调用方看到 error
            self._login_error = str(exc)
            self._failed = True
            raise

    def _headers(self, auth: bool) -> dict[str, Any]:
        headers = dict(self._session.headers)
        if auth and self._profile is not None:
            from utils.infra.globals import Globals

            project_data = Globals.get_data().get(self.project, {}) or {}
            session_id = project_data.get(self._profile.session_var)
            headers.update(
                self._profile.auth_headers(self._token or "", session_id or "")
            )
        else:
            # 匿名探针：默认头里如果带了鉴权头要摘掉
            for key in list(headers):
                if str(key).lower() in {"authorization", "token", "cookie"}:
                    headers.pop(key, None)
        return headers

    # ---------------- 发请求 ----------------

    def send(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        data: Any = None,
        auth: bool = True,
    ) -> dict[str, Any]:
        """发一次探针请求，返回 (status_code, body, meta)。异常不抛出。"""
        self._ensure_ready()
        method = method.upper()
        url = str(self._config.get("host") or "").rstrip("/") + path
        headers = self._headers(auth)

        try:
            kwargs: dict[str, Any] = {"headers": headers, "timeout": PROBE_TIMEOUT}
            if method == "GET":
                kwargs["params"] = params if params is not None else data
            else:
                content_type = str(headers.get("Content-Type") or "")
                if "x-www-form-urlencoded" in content_type:
                    kwargs["data"] = data
                else:
                    kwargs["json"] = data
                if params:
                    kwargs["params"] = params
            response = self._session.request(method, url, **kwargs)
        except Exception as exc:  # noqa: BLE001 - 网络异常记进 error
            return {"status_code": 0, "body": None, "error": str(exc)}

        try:
            body = response.json()
        except ValueError:
            body = None
        return {"status_code": response.status_code, "body": body, "error": ""}


# ---------------- 构造请求 ----------------


def _base_step(ep: Endpoint, ctx: Any = None) -> dict[str, Any]:
    """探针的基准请求。

    优先用**现役用例的真实参数**，而不是文档示例：文档示例常是
    ``{"area": {...}, "size": 0, "total": 0}`` 这种从响应 schema 反推出来的
    脏数据，既不是合法请求，也测不出真实行为。
    """
    from tools.importer.case_builder import build_step

    step = copy.deepcopy(build_step(ep))
    examples = ctx.examples_for(ep, limit=1) if ctx is not None else []
    if examples:
        sample = examples[0]
        if sample.get("params"):
            step["params"] = copy.deepcopy(sample["params"])
        if sample.get("data"):
            step["data"] = copy.deepcopy(sample["data"])
    return step


def _resolve_path(ep: Endpoint, ctx: Any, override_id: Any = None) -> str | None:
    """把 ``{id}`` 换成真实值；换不掉返回 None。"""
    path = str(ep.path or "")
    if "{" not in path:
        return path

    values = dict(ctx.path_values_for(ep) or {})
    if override_id is not None:
        values = {name: override_id for name in values} or {"id": override_id}

    for name, value in values.items():
        path = path.replace(f"{{{name}}}", str(value))
    return None if "{" in path else path


def _locate(step: dict[str, Any], field_name: str) -> tuple[dict[str, Any], str]:
    """找到参数字段所在容器：query(params) 还是 body(data)。

    **必须确认字段真的在容器里**：嵌套请求体（``{"area": {"areaName": ...}}``）
    的字段不在顶层，返回整个 data 再 pop 是空操作，探针会拿着完整合法请求
    测出「A1 被接受」的假结论。
    """
    if isinstance(step.get("params"), dict) and field_name in step["params"]:
        return step["params"], "params"
    if isinstance(step.get("data"), dict) and field_name in step["data"]:
        return step["data"], "data"
    return {}, "data"


def _mutate_for_intent(
    ep: Endpoint,
    intent: Any,
    base: dict[str, Any],
    ctx: Any,
) -> dict[str, Any] | None:
    """按测点改出一次探针请求；不支持的测点返回 None。"""
    dimension = intent.dimension
    name = str(getattr(intent, "field", "") or "")

    step = copy.deepcopy(base)

    # 路径参数要在「换成真实值」之前处理：换完之后 ``{id}`` 已经没了，
    # 再 replace 是空操作，探针会拿着合法 id 测出「接受」的假结论。
    path_params = {p.name for p in (ep.path_params or [])}
    if name in path_params and dimension in {"A1", "A2", "A3", "A6", "B1"}:
        if dimension != "A2":
            # 路径参数不能省略、也没有枚举/空值/分页语义
            return None
        illegal = _resolve_path(ep, ctx, override_id="not-a-number")
        return (
            None
            if illegal is None
            else {
                "path": illegal,
                "params": copy.deepcopy(step.get("params")),
                "data": copy.deepcopy(step.get("data")),
                "auth": True,
            }
        )

    path = _resolve_path(ep, ctx)
    if path is None:
        return None

    request = {
        "path": path,
        "params": copy.deepcopy(step.get("params")),
        "data": copy.deepcopy(step.get("data")),
        "auth": True,
    }

    if dimension == "P0":
        return request

    if dimension in {"D1", "D2", "D3", "D4"}:
        # 落库一致性探针就是正常请求，DB 比对在执行阶段做
        return request

    if dimension == "C1":
        request["auth"] = False
        return request

    if dimension == "F1":
        request["path"] = _resolve_path(ep, ctx, override_id=NONEXISTENT_ID) or path
        return request

    if dimension in {"A1", "A2", "A3", "A6", "B1"} and name:
        container, _ = _locate(step, name)
        if not container:
            # 字段不在基准请求里：省略是空操作、改值也改不到，
            # 硬发出去只会测成「接受」，必须跳过。
            return None
        if container is step.get("params"):
            request["params"] = dict(container)
        else:
            request["data"] = copy.deepcopy(container)

        if dimension == "A1":
            (request["params"] or {}).pop(name, None)
            if isinstance(request["data"], dict):
                request["data"].pop(name, None)
        elif dimension == "A2":
            _set_value(request, name, "not-a-number")
        elif dimension == "A3":
            _set_value(request, name, "__invalid_enum__")
        elif dimension == "A6":
            _set_value(request, name, "")
        elif dimension == "B1":
            _set_value(request, name, 0)
        return request

    return None


def _contains_template(value: Any) -> bool:
    """请求里还有没展开的框架变量（``{{...}}`` / ``${...}``）。

    现役用例的请求体常写成 ``${"areaName": "{{tools.fixed(...)}}"}``。探针不跑
    变量解析，直接发出去会把模板串当真实值（写接口还会造出脏数据），
    实测结果也就不代表用例真正会发的请求。
    """
    if isinstance(value, str):
        return "{{" in value or "${" in value
    if isinstance(value, dict):
        return any(
            _contains_template(k) or _contains_template(v) for k, v in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_template(item) for item in value)
    return False


def _set_value(request: dict[str, Any], name: str, value: Any) -> None:
    for key in ("params", "data"):
        container = request.get(key)
        if isinstance(container, dict) and name in container:
            container[name] = value
            return


# ---------------- 写探针的数据足迹 ----------------


def _looks_like_id_key(key: Any) -> bool:
    """字段名像不像主键：``id`` / ``areaId`` / ``area_id`` / ``ID``。"""
    name = str(key)
    return (
        name == "id"
        or name.endswith("Id")
        or name.endswith("ID")
        or name.endswith("_id")
    )


def _extract_created_ids(data: Any) -> dict[str, Any]:
    """从响应 ``data`` 里挑出新建记录的主键。

    **只看一层**、只认名字像主键的字段，不做递归猜测：深一层的字段多半是
    关联数据，猜错了清单就不值得信。这里也不区分主键和外键（``parentId``
    同样会留下）—— 清单是给人按图索骥用的，每条都带着完整请求体，
    人工核对即可。认不出来返回空。
    """
    if isinstance(data, dict):
        ids: dict[str, Any] = {}
        for key, value in data.items():
            if not _looks_like_id_key(key) or isinstance(value, bool):
                continue
            if isinstance(value, (int, str)) and str(value).strip() not in ("", "0"):
                ids[str(key)] = value
        return ids
    # 有的接口直接把新 id 当 data 返回
    if isinstance(data, int) and not isinstance(data, bool) and data != 0:
        return {"id": data}
    if isinstance(data, str) and data.strip().isdigit() and data.strip() != "0":
        return {"id": data.strip()}
    return {}


def suggest_delete_endpoint(ep: Endpoint, endpoints: list[Endpoint]) -> str:
    """给一个写接口找「删掉它造的那条数据」的删除接口。

    只认 ``<base>/<一个路径参数>`` 这种最直白的删除接口。嵌套路径
    （``/area/{id}/status``）和同前缀的其它接口（``/area/export``）都不算 ——
    给错建议比不给更危险。返回 ``"DELETE /path"``，找不到返回空串。
    """
    base = str(ep.path or "").rstrip("/")
    if not base:
        return ""
    prefix = base + "/"
    for other in endpoints:
        if str(other.method or "").upper() != "DELETE":
            continue
        path = str(other.path or "").rstrip("/")
        if not path.startswith(prefix):
            continue
        tail = path[len(prefix):]
        if "/" in tail or tail.count("{") != 1 or not tail.endswith("}"):
            continue
        return f"DELETE {path}"
    return ""


def collect_footprints(
    result: dict[str, list[ProbeOutcome]],
) -> list[dict[str, Any]]:
    """把写探针造出来的数据摊平成清单。

    纯函数、不碰文件 —— 调用方拿到 ``probe_endpoints`` 的结果后都能自己
    决定怎么展示/落盘；命令行再包一层 :func:`_report_footprints`。
    """
    items: list[dict[str, Any]] = []
    for key, outcomes in result.items():
        for outcome in outcomes:
            for entry in outcome.created:
                item = {"接口": key, **entry}
                if outcome.delete_endpoint:
                    item["清理接口"] = outcome.delete_endpoint
                items.append(item)
    return items


def collect_modifications(
    result: dict[str, list[ProbeOutcome]],
) -> list[dict[str, Any]]:
    """PUT/PATCH 探针改动过的已有数据。

    这些记录**本来就存在**，DELETE 清不掉问题（删掉的是原有数据），
    所以只摊平供人工还原，绝不进清理计划。
    """
    items: list[dict[str, Any]] = []
    for key, outcomes in result.items():
        for outcome in outcomes:
            for entry in outcome.modified:
                items.append({"接口": key, **entry})
    return items


def _report_footprints(
    result: dict[str, list[ProbeOutcome]], env: str, output: str = ""
) -> None:
    """把写探针造的数据汇总成清单 —— 落盘并大声提醒，**绝不自动删除**。

    自动删需要证明「删的就是这次造的那条」。在测试环境把这条链路验证
    通过之前，先做到「留下可清理的线索」：发过什么请求、响应里的 id、
    该用哪个删除接口。没有线索的脏数据才是真正的问题。
    """
    items = collect_footprints(result)
    modified = collect_modifications(result)

    if modified:
        print(
            f"⚠️ 另有 {len(modified)} 次 PUT/PATCH 探针改动了已有数据；"
            "DELETE 清不了（会删掉原有记录），清单里只留请求体供人工还原。",
            file=sys.stderr,
        )

    if not items and not modified:
        print(
            "写探针已执行，没有新建出数据 —— 无需要清理的记录。",
            file=sys.stderr,
        )
        return

    from pathlib import Path

    from utils import paths

    target = (
        # 跟着输出文件名走，别说「probe_area.json」的清单落到
        # 「probe_footprints.json」然后被下一次探针覆盖掉
        Path(output).with_name(Path(output).stem + "_footprints.json")
        if output
        else paths.CACHE_DIR / "ai_compare" / "probe_footprints.json"
    )
    payload = {
        "env": env,
        "生成时间": time.strftime("%Y-%m-%d %H:%M:%S"),
        "条数": len(items),
        "items": items,
        "说明": (
            "写探针新建的数据不会自动删除。按「清理接口」+ ids（没有 id 就用请求体"
            "里的唯一字段）手工清理；清单只记本次探针，别拿它当历史账本。"
        ),
    }
    if modified:
        payload["说明"] += (
            "「改动的数据」是 PUT/PATCH 改过的已有记录，不能 DELETE —— "
            "按请求体里的字段人工还原。"
        )
        payload["改动的数据"] = modified
    if items:
        print(
            f"⚠️ 写探针成功造出 {len(items)} 条数据，不会自动删除。"
            f"清单见 {target}",
            file=sys.stderr,
        )
    else:
        print(f"⚠️ 没有新建数据；改动清单见 {target}", file=sys.stderr)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        # 清单写不进去也不能吞掉 —— 至少把每条的数据打到 stderr
        print(f"足迹清单写入失败（{exc}），逐条打印：", file=sys.stderr)
        print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)


# ---------------- 对外入口 ----------------


def ensure_write_allowed(env: str) -> None:
    """写探针的环境闸。

    写探针会把请求真的发出去，而且**不会清理**（DELETE 被禁）——
    在 online / pre 上跑等于往准生产写脏数据。这是硬闸，不靠调用方自觉。

    Raises:
        RuntimeError: 当前环境不在白名单里。
    """
    current = str(env or "").strip().lower()
    if current in WRITE_PROBE_ENVS:
        return
    raise RuntimeError(
        "写探针只允许在 %s 环境跑，当前 env=%r。"
        "写探针造的数据没有自动清理，别在准生产上用。"
        % ("、".join(sorted(WRITE_PROBE_ENVS)), env)
    )


def probe_endpoint(
    ep: Endpoint,
    client: ProbeClient,
    ctx: Any,
    max_intents: int = 8,
    include_write: bool = False,
) -> list[ProbeOutcome]:
    """对一个接口的所有测点依次探针。"""
    from ai.pipeline.intents import plan_endpoint_intents

    method = str(ep.method or "").upper()
    if method in FORBIDDEN_METHODS:
        return []
    if method in WRITE_METHODS and not include_write:
        return []
    if method in WRITE_METHODS:
        # 写探针没有配套清理（DELETE 被禁），只放行测试环境
        ensure_write_allowed(str(getattr(client, "env", "") or ""))

    intents = plan_endpoint_intents(
        ep, max_intents=max_intents, mappings=ctx.db_mappings(ep)
    )
    base = _base_step(ep, ctx)

    outcomes: list[ProbeOutcome] = []
    for intent in intents:
        request = _mutate_for_intent(ep, intent, base, ctx)
        if request is None:
            continue
        if _contains_template(request.get("params")) or _contains_template(
            request.get("data")
        ):
            # 参数里还有未展开的变量 —— 实测的不是用例真正会发的请求，
            # 不能拿来当断言依据（写接口还会把模板串写进业务数据）。
            continue

        result = client.send(
            method,
            request["path"],
            params=request.get("params"),
            data=request.get("data"),
            auth=bool(request.get("auth", True)),
        )
        body = result.get("body")
        if not isinstance(body, dict):
            body = {}

        code = str(body.get("code") or "")
        success = body.get("success")
        if not isinstance(success, bool) and code:
            # 有的接口只回业务码不回 success —— 用项目契约的成功码判断
            success_code = str(getattr(ctx, "contract", {}).get("success_code") or "")
            if success_code:
                success = code == success_code
        data = body.get("data")

        # 写探针成功 = 环境被改了。把「哪个测点、发了什么、响应给的 id」
        # 记下来 —— 不记的话这批改动除了跑的人没人知道。
        #
        # ⚠️ POST 才是**新建**，能按 DELETE 清；PUT/PATCH 改的是本来就在的
        # 记录，拿 DELETE 清会删掉别人的数据 —— 只能单独记、人工还原。
        created: list[dict[str, Any]] = []
        modified: list[dict[str, Any]] = []
        if method in WRITE_METHODS and isinstance(success, bool) and success:
            entry = {
                "测点": intent.label,
                "维度": intent.dimension,
                "请求": {
                    "path": request["path"],
                    "params": request.get("params"),
                    "data": request.get("data"),
                },
                "ids": _extract_created_ids(data),
            }
            if method == "POST":
                created.append(entry)
            elif method in {"PUT", "PATCH"}:
                modified.append(entry)

        outcomes.append(
            ProbeOutcome(
                dimension=intent.dimension,
                label=intent.label,
                method=method,
                path=request["path"],
                kind=str(intent.label),
                field=str(getattr(intent, "field", "") or ""),
                has_success=("success" in body),
                request={
                    "path": request["path"],
                    "params": request.get("params"),
                    "data": request.get("data"),
                    "auth": request.get("auth", True),
                },
                status_code=int(result.get("status_code") or 0),
                code=code,
                success=bool(success) if isinstance(success, bool) else None,
                message=str(body.get("message") or ""),
                data_present=data not in (None, "", [], {}),
                data_is_none=("data" in body and body.get("data") is None),
                error=str(result.get("error") or ""),
                created=created,
                modified=modified,
            )
        )

    return outcomes


def probe_endpoints(
    endpoints: list[Endpoint],
    project: str = "mall4j",
    env: str = "test",
    max_intents: int = 8,
    include_write: bool = False,
) -> dict[str, list[ProbeOutcome]]:
    """批量探针。key = ``METHOD path``。"""
    from ai.capabilities.case_gen import build_context

    # 环境闸要在发第一个请求之前拦下 —— 放到 probe_endpoint 里逐接口抛，
    # 只会得到一堆「探针失败」的 outcome，看起来像接口坏了。
    if include_write:
        ensure_write_allowed(env)

    ctx = build_context(project, endpoints)
    client = ProbeClient(project, env)
    result: dict[str, list[ProbeOutcome]] = {}
    for ep in endpoints:
        key = f"{ep.method.upper()} {ep.path}"
        try:
            items = probe_endpoint(
                ep, client, ctx, max_intents=max_intents, include_write=include_write
            )
            # 造了数据就把「怎么删」一起写上 —— 只提示，不替人删
            for outcome in items:
                if outcome.created:
                    outcome.delete_endpoint = suggest_delete_endpoint(ep, endpoints)
            result[key] = items
        except Exception as exc:  # noqa: BLE001 - 单接口失败不影响其它接口
            outcome = ProbeOutcome(
                dimension="P0",
                label="探针失败",
                method=ep.method.upper(),
                path=ep.path,
                error=str(exc),
            )
            result[key] = [outcome]
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="接口探针：实测每个测点的真实行为")
    parser.add_argument("--file", required=True, help="接口文档文件")
    parser.add_argument("--project", default="mall4j")
    parser.add_argument("--env", default="test")
    parser.add_argument("--only-path", default="")
    parser.add_argument("--only-method", default="")
    parser.add_argument("--max-intents", type=int, default=8)
    parser.add_argument(
        "--include-write",
        action="store_true",
        help=(
            "也探写接口（POST/PUT/PATCH）；DELETE 永远不探。"
            "⚠️ 写探针会把文档示例值（常是 \"string\" / 0）真的写进环境，"
            "且**不会自动清理**（DELETE 被禁）—— 只在 "
            f"{'、'.join(sorted(WRITE_PROBE_ENVS))} 环境可用（硬闸）。"
            "每次造的数据会连同请求体、响应 id、删除接口写进 "
            "<输出名>_footprints.json；用 "
            "`python -m ai.pipeline.probe_cleanup --footprints <该文件>` "
            "生成清理计划（只列计划，不发请求）"
        ),
    )
    parser.add_argument("--output", default="", help="结果写文件（UTF-8 JSON）")
    args = parser.parse_args(argv)

    from tools.importer.dispatcher import parse_file

    endpoints = parse_file(args.file)
    if args.only_path:
        endpoints = [ep for ep in endpoints if args.only_path in ep.path]
    if args.only_method:
        endpoints = [ep for ep in endpoints if ep.method.upper() == args.only_method]

    result = probe_endpoints(
        endpoints,
        project=args.project,
        env=args.env,
        max_intents=args.max_intents,
        include_write=args.include_write,
    )
    payload = {
        key: [outcome.to_dict() for outcome in outcomes]
        for key, outcomes in result.items()
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)

    if args.output:
        from pathlib import Path

        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        print(f"探针结果写入 {target}")
    else:
        print(text)

    # 跑完立刻汇总「造了哪些数据」—— 这是写探针唯一的清理线索，
    # 不能等调用方想起来才生成
    if args.include_write:
        _report_footprints(result, args.env, output=args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
