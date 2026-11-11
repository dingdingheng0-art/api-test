"""用例改写工具：读 → 预览 → 落盘，三步分开。

**为什么要拆成「预览」和「落盘」两个工具**

``propose_case_change`` 只算差异、不写文件；``apply_case_change`` 才落盘。
多这一道不是为了好看：

- 模型经常「理解对了但写错了」（比如把 ``equal`` 写成 ``equals`` 之外的怪东西），
  先看 diff 能拦住大半
- Claude Code 的权限里，写类工具走 ``ask``，用户会在弹窗里看到 diff
- 自愈场景下，模型可以先 propose 一遍给自己复核，再 apply

**为什么写的是 ``case_id`` 而不是文件路径**

文件路径模型容易编。``API-168`` 是从 ``list_cases`` / ``get_case`` 里直接
抄来的，抄错的概率低得多，而且抄错了会明确报「找不到这个编号」，
而不是默默改到别的文件上。

**改完不算完**

所有写工具返回里都带 ``next_step``：改了断言要去跑，跑通了才能改状态。
没跑过就标 ``debugged`` 等于骗 CI —— 这条规则写在工具返回里，
比写在 skill 文档里更管用（模型每次都会看到）。
"""

from __future__ import annotations

from typing import Any

from ai.agent.tools import case_io
from ai.agent.tools.registry import ToolError, ToolSpec, ok

#: 写类工具的公共说明
_WRITE_NOTE = "会改用例文件（写前备份、写后校验，失败自动回滚）"


def read_case_assertions(case_id: str, project: str = "") -> dict[str, Any]:
    """读一条用例当前的断言（内部格式 + Excel 扁平格式对照）。"""
    return ok(**case_io.read_assertions(case_id, project=project))


def propose_case_change(
    case_id: str,
    assertions: list[dict[str, Any]],
    step: str = "",
    project: str = "",
) -> dict[str, Any]:
    """预览断言改动（**不写文件**）。"""
    plan = case_io.plan_assertion_change(
        case_id, assertions, step=step, project=project
    )
    hint = (
        "这是预览，文件没动。确认 diff 没问题后调 apply_case_change 落盘。"
        "diff.removed 里如果有你不想删的断言，说明新断言写漏了。"
    )
    if plan.get("weakenings"):
        # 预览阶段就说清楚：这个改动落盘会被拒
        hint += (
            f" ⚠️ 这次改动会削弱 {len(plan['weakenings'])} 处断言，"
            "apply_case_change 默认会拒绝；确实该放宽才传 allow_weakening=true。"
        )

    return ok(**plan, written=False, hint=hint)


def apply_case_change(
    case_id: str,
    assertions: list[dict[str, Any]],
    step: str = "",
    project: str = "",
    note: str = "",
    allow_weakening: bool = False,
) -> dict[str, Any]:
    """把断言写回用例文件（**会改文件**）。"""
    result = case_io.apply_assertion_change(
        case_id,
        assertions,
        step=step,
        project=project,
        note=note,
        allow_weakening=allow_weakening,
    )
    # 提醒带上 backup_dir —— 没有它，verify_heal 失败时就没法自动回滚
    result["next_step"] = (
        f"断言改了，但**还没跑过**。调 verify_heal(case_id='{result['case_id']}', "
        f"backup_dir='{result['backup_dir']}') 重跑确认 —— 没通过会自动回滚到改之前。"
    )
    return ok(**result)


def set_case_status(
    case_id: str,
    status: str,
    project: str = "",
    note: str = "",
    force: bool = False,
) -> dict[str, Any]:
    """改用例状态（draft / debugged / disabled / broken）。"""
    result = case_io.apply_status_change(
        case_id, status, project=project, note=note, force=force
    )
    return ok(**result)


def create_case(
    project: str,
    module: str,
    path: str,
    method: str,
    title: str,
    assertions: list[dict[str, Any]],
    api_name: str = "",
    case_name: str = "",
    request_data: Any = None,
    request_headers: Any = None,
    query_params: Any = None,
    priority: str = "P1",
    marks: str = "",
    setups: Any = None,
    teardowns: Any = None,
    steps: Any = None,
) -> dict[str, Any]:
    """新建一条 Excel 用例（默认 draft）。"""
    result = case_io.create_case(
        project=project,
        module=module,
        path=path,
        method=method,
        title=title,
        assertions=assertions,
        api_name=api_name,
        case_name=case_name,
        request_data=request_data,
        request_headers=request_headers,
        query_params=query_params,
        priority=priority,
        marks=marks,
        setups=setups,
        teardowns=teardowns,
        steps=steps,
    )
    return ok(**result)


_JSON = {"type": ["object", "array", "string", "number", "boolean", "null"]}

_ASSERTION_ITEMS = {
    "type": "object",
    "description": (
        "框架内部格式的断言，和 get_case 返回的一致。"
        "例：{\"type\": \"equal\", \"field\": \"success\", \"expected\": true}、"
        "{\"type\": \"db_query\", \"query\": \"SELECT area_name FROM tz_area WHERE area_id = %s\", "
        "\"params\": [1], \"expected\": \"xxx\"}。"
        "字段路径不带 $. 前缀（写 data.records，不写 $.data.records）。"
    ),
}


TOOLS = [
    ToolSpec(
        name="read_case_assertions",
        description=(
            "读一条用例当前的断言。Excel 用例会同时给出扁平格式（写进单元格的样子），"
            "方便你确认改写后的形态。改断言前先用它看现状。"
        ),
        params_schema={
            "type": "object",
            "properties": {
                "case_id": {"type": "string", "description": "用例编号，如 API-168"},
                "project": {"type": "string", "description": "项目名，可省略", "default": ""},
            },
            "required": ["case_id"],
            "additionalProperties": False,
        },
        handler=read_case_assertions,
        side_effect="read",
        tags=("case", "write"),
    ),
    ToolSpec(
        name="propose_case_change",
        description=(
            "**预览**断言改动，不写文件。返回 diff（added / removed / before_count / after_count）"
            "和 before / after 两版扁平断言。"
            "改用例断言的标准流程：read_case_assertions → propose_case_change → "
            "（人看 diff）→ apply_case_change。"
            "diff.removed 里出现你不想删的断言，就是新断言写漏了，别急着落盘。"
        ),
        params_schema={
            "type": "object",
            "properties": {
                "case_id": {"type": "string", "description": "用例编号，如 API-168"},
                "assertions": {
                    "type": "array",
                    "description": "**完整的新断言列表**（整体替换，不是追加）。不能为空。",
                    "items": _ASSERTION_ITEMS,
                },
                "step": {"type": "string", "description": "步骤 id；扁平 Excel 用例只有一步，可省略", "default": ""},
                "project": {"type": "string", "description": "项目名，可省略", "default": ""},
            },
            "required": ["case_id", "assertions"],
            "additionalProperties": False,
        },
        handler=propose_case_change,
        side_effect="read",
        tags=("case", "write"),
    ),
    ToolSpec(
        name="apply_case_change",
        description=(
            "把断言写回用例文件。写前备份到 .cache/backup/agent_<时间戳>/，"
            "写后重新解析并校验，不通过自动回滚并报错。"
            "改完**必须**用 verify_heal 跑一次确认通过（不过会自动回滚）。"
            "⚠️ 归因为服务问题的用例会被**直接拒绝**（这道闸没有开关）；"
            "断言变弱、或同一条用例改两轮还没跑通，也会被拒绝。"
        ),
        params_schema={
            "type": "object",
            "properties": {
                "case_id": {"type": "string", "description": "用例编号，如 API-168"},
                "assertions": {
                    "type": "array",
                    "description": "**完整的新断言列表**（整体替换，不是追加）。不能为空。",
                    "items": _ASSERTION_ITEMS,
                },
                "step": {"type": "string", "description": "步骤 id；扁平 Excel 用例只有一步，可省略", "default": ""},
                "project": {"type": "string", "description": "项目名，可省略", "default": ""},
                "note": {
                    "type": "string",
                    "description": "为什么改。会写进备份目录的 manifest.json，事后可追溯",
                    "default": "",
                },
                "allow_weakening": {
                    "type": "boolean",
                    "description": (
                        "是否允许放宽断言（默认 false）。删掉断言、把 == 换成 exists/not_null "
                        "这类改动会被拒绝。只有接口契约确实变了才打开，且在 note 里写依据"
                    ),
                    "default": False,
                },
            },
            "required": ["case_id", "assertions"],
            "additionalProperties": False,
        },
        handler=apply_case_change,
        side_effect="write_case",
        safety=_WRITE_NOTE,
        tags=("case", "write"),
    ),
    ToolSpec(
        name="set_case_status",
        description=(
            "改用例状态：draft（未调试，CI 不跑）/ debugged（调试通过，CI 跑）/ "
            "disabled（临时下线）/ broken（已知有问题）。"
            "改成 debugged 会核对「跑通过」凭据：必须先用 run_cases 跑（整体通过后"
            "自动记账），没记录或记录之后又改过用例，都会被拒绝。"
        ),
        params_schema={
            "type": "object",
            "properties": {
                "case_id": {"type": "string", "description": "用例编号，如 API-168"},
                "status": {
                    "type": "string",
                    "enum": ["draft", "debugged", "disabled", "broken"],
                    "description": "目标状态",
                },
                "project": {"type": "string", "description": "项目名，可省略", "default": ""},
                "note": {"type": "string", "description": "为什么改状态", "default": ""},
                "force": {
                    "type": "boolean",
                    "description": (
                        "跳过「跑通过」凭据核对（默认 false）。"
                        "只在用例确实无法执行（环境故障）时用，且必须写 note 说明原因。"
                        "正常流程不要用 —— 没跑过就标 debugged 等于骗 CI。"
                    ),
                    "default": False,
                },
            },
            "required": ["case_id", "status"],
            "additionalProperties": False,
        },
        handler=set_case_status,
        side_effect="write_case",
        safety=_WRITE_NOTE,
        tags=("case", "write"),
    ),
    ToolSpec(
        name="create_case",
        description=(
            "新建一条 Excel 用例，追加到 testcases/cases_excel/<项目>/<模块>/<模块>.xlsx。"
            "默认状态 draft（CI 不跑）。新建后必须 run_cases 跑通再 set_case_status 改成 debugged。"
            "写断言前先用 find_field_mapping 确认落库列名、用 explain_endpoint 看该断言什么。"
            "**写操作（POST/PUT/PATCH/DELETE）正例会自动补「造数 → 查真实 id → 清理」"
            "链路**：POST 补 teardowns，PUT/PATCH 补 setups+teardowns，DELETE 改成删"
            "本用例造的种子；挂上后返回 data_chain 说明。反例不造数。"
            "模型自己传了 setups/teardowns 就按传的落盘，不再重复规划。"
        ),
        params_schema={
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "项目名，如 mall4j"},
                "module": {"type": "string", "description": "模块名（也是目录名），如 admin_area"},
                "path": {"type": "string", "description": "接口路径，以 / 开头，如 /admin/area"},
                "method": {"type": "string", "description": "GET / POST / PUT / DELETE …"},
                "title": {
                    "type": "string",
                    "description": "用例标题/场景说明，如「（正常请求）」；不要重复写业务名（那是 api_name）",
                },
                "assertions": {
                    "type": "array",
                    "description": "断言列表（框架内部格式），不能为空",
                    "items": _ASSERTION_ITEMS,
                },
                "api_name": {
                    "type": "string",
                    "description": (
                        "接口的中文业务名，如「修改地区」。不要写「PUT /admin/area」这种"
                        "方法+路径；传了也会按接口去现役用例里换成中文名"
                    ),
                    "default": "",
                },
                "case_name": {
                    "type": "string",
                    "description": (
                        "可选的完整用例名称，如「地区管理 - 修改地区（正常请求）」"
                        "（模块中文名 - 业务功能（场景））。不传或写成方法+路径时，"
                        "按「模块中文名 - api_name（场景）」自动拼"
                    ),
                    "default": "",
                },
                "request_data": _JSON | {"description": "请求体 / 查询参数（GET 时放查询参数）"},
                "request_headers": _JSON
                | {"description": "请求头，如 {\"Authorization\": \"{{mall4j.token}}\"}；不传就用项目档案里的鉴权头"},
                "query_params": _JSON | {"description": "显式放到 URL 上的参数（PUT 接口常用）"},
                "priority": {"type": "string", "description": "P0 / P1 / P2 / P3", "default": "P1"},
                "marks": {"type": "string", "description": "标签，逗号分隔，如 admin_area,smoke", "default": ""},
                "setups": _JSON | {"description": "前置步骤（造数据），格式同 YAML 用例的 setups"},
                "teardowns": _JSON | {"description": "后置步骤（清数据）"},
                "steps": _JSON
                | {
                    "description": (
                        "多步场景链（创建→查询→修改→删除）。每一项："
                        "{\"id\": \"step1\", \"method\": \"POST\", \"path\": \"/admin/area\", "
                        "\"data\": {...}, \"assert\": [...]}。传了就以它为准，写进 Excel 的"
                        "「步骤」列；path/method/assertions 只用于命名和第一步兜底"
                    )
                },
            },
            "required": ["project", "module", "path", "method", "title", "assertions"],
            "additionalProperties": False,
        },
        handler=create_case,
        side_effect="write_case",
        safety=_WRITE_NOTE + "；新建用例默认 draft，不会进 CI",
        tags=("case", "write"),
    ),
]
