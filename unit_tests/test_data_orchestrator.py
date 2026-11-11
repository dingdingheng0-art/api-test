"""API 造数编排的单元测试（不联网）。

验证的是「写用例能不能自己造数并清理」：

1. POST / PUT / DELETE 三种主步骤各自拼出的 setups/teardowns 形状
2. 主步骤必须改的是**自己造的那一条**（id 来自查数，不是文档占位值）
3. 反例不造数、给不出链路就返回 None（调用方继续标人工）
4. 前置/后置断言落盘前会转成 Excel 扁平格式（否则读回来就报错）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai.agent.tools.case_io import _prepost_to_flat
from ai.capabilities.case_gen import ProjectContext, _finalize_ai_case
from ai.pipeline.data_orchestrator import (
    _infer_id_field,
    _infer_name_field,
    attach_chain,
    plan_data_chain,
    unsafe_teardowns,
    write_is_self_cleaning,
)
from tools.importer.models import Endpoint, Param

AREA_LIST = {
    "code": "00000",
    "success": True,
    "data": [
        {
            "areaId": 110000000000,
            "areaName": "北京",
            "parentId": 0,
            "level": 1,
        }
    ],
}

POST_BODY = {"areaName": "string", "parentId": 0, "level": 0}
TRUSTED_POST_BODY = {"areaName": "x", "parentId": 110000000000, "level": 2}


def _endpoints() -> list[Endpoint]:
    return [
        Endpoint(
            id="area_post",
            method="POST",
            path="/admin/area",
            name="新增地区",
            tags=["area"],
            body=dict(POST_BODY),
            body_params=[
                Param(name="areaName", location="body", required=True),
                Param(name="parentId", location="body", required=True),
                Param(name="level", location="body", required=True),
            ],
            response_example={"code": "00000", "success": True, "data": None},
        ),
        Endpoint(
            id="area_put",
            method="PUT",
            path="/admin/area",
            name="修改地区",
            tags=["area"],
            body={"areaId": 0, "areaName": "string", "parentId": 0, "level": 0},
            body_params=[
                Param(name="areaId", location="body", required=True),
                Param(name="areaName", location="body", required=True),
            ],
        ),
        Endpoint(
            id="area_list",
            method="GET",
            path="/admin/area/list",
            name="查询地区列表",
            tags=["area"],
            response_example=AREA_LIST,
        ),
        Endpoint(
            id="area_page",
            method="GET",
            path="/admin/area/page",
            name="分页查询地区",
            tags=["area"],
            query=[
                Param(name="current", location="query"),
                Param(name="size", location="query"),
            ],
            response_example={
                "code": "00000",
                "success": True,
                "data": {"records": AREA_LIST["data"], "total": 1},
            },
        ),
        Endpoint(
            id="area_delete",
            method="DELETE",
            path="/admin/area/{id}",
            name="删除地区",
            tags=["area"],
            path_params=[Param(name="id", location="path")],
        ),
    ]


def _context() -> ProjectContext:
    ctx = ProjectContext(
        project="mall4j",
        contract={"success_code": "00000", "success_field": "success", "http_status": 200},
    )
    ctx.examples_by_endpoint = {
        ("POST", "/admin/area"): [
            {"data": dict(TRUSTED_POST_BODY), "marks": ["positive"]}
        ],
        ("PUT", "/admin/area"): [
            {
                "data": {
                    "areaId": 110000000000,
                    "areaName": "北京",
                    "parentId": 110000000000,
                    "level": 2,
                },
                "marks": ["positive"],
            }
        ],
    }
    return ctx


# ---------------------------------------------------------------------------
# 三种主步骤
# ---------------------------------------------------------------------------


def test_post_main_creates_then_cleans_up_by_fixed_name():
    endpoints = _endpoints()
    chain = plan_data_chain(
        {"method": "POST", "path": "/admin/area", "data": dict(POST_BODY)},
        endpoints,
        _context(),
    )

    assert chain is not None
    assert chain.setups == []
    assert [s["id"] for s in chain.teardowns] == ["查数据", "清理数据"]

    lookup, cleanup = chain.teardowns
    assert lookup["path"] == "/admin/area/list"
    assert cleanup["method"] == "DELETE"
    assert "tools.find_id_by_fixed(查数据.data" in cleanup["path"]
    assert '"areaName"' in cleanup["path"]
    assert '"areaId"' in cleanup["path"]

    # 主步骤的请求体用造数名称，不用文档里的 "string"
    assert chain.main_data["areaName"].startswith("{{tools.fixed(")
    assert chain.main_data["parentId"] == 110000000000


def test_put_main_seeds_and_updates_own_id():
    chain = plan_data_chain(
        {
            "method": "PUT",
            "path": "/admin/area",
            # 文档示例里的 areaId=0 实测会报 A00001，必须被链路覆盖
            "data": {"areaId": 0, "areaName": "string"},
        },
        _endpoints(),
        _context(),
    )

    assert chain is not None
    assert [s["id"] for s in chain.setups] == ["造数据", "查数据"]
    assert [s["id"] for s in chain.teardowns] == ["清理数据"]

    seed, lookup = chain.setups
    assert "tools.fixed(" in seed["data"]["areaName"]
    assert seed["data"]["parentId"] == 110000000000
    assert lookup["path"] == "/admin/area/list"

    assert chain.main_data["areaId"] == (
        '{{tools.find_id_by_fixed(查数据.data, "areaName", '
        '"mall4j_admin_area_areaName_seed", "areaId", "AI造数")}}'
    )
    assert chain.main_data["areaName"].startswith("{{tools.fixed(")
    assert "改" in chain.main_data["areaName"]

    assert "tools.find_id_by_fixed(查数据.data" in chain.teardowns[0]["path"]


def test_delete_main_deletes_seed_and_needs_no_teardown():
    chain = plan_data_chain(
        {"method": "DELETE", "path": "/admin/area/110000000000"},
        _endpoints(),
        _context(),
    )

    assert chain is not None
    assert [s["id"] for s in chain.setups] == ["造数据", "查数据"]
    assert chain.teardowns == []
    assert chain.main_path is not None
    assert chain.main_path.startswith("/admin/area/{{tools.find_id_by_fixed(")
    assert chain.self_cleaning is True


def _user_endpoints() -> list[Endpoint]:
    """``/sys/user`` 这类接口：主键叫 userId，和「创建人 userId」同名。"""
    return [
        Endpoint(
            id="user_post",
            method="POST",
            path="/sys/user",
            name="新增用户",
            body={"username": "string", "password": "string"},
            body_params=[
                Param(name="username", location="body", required=True),
                Param(name="password", location="body", required=True),
            ],
        ),
        Endpoint(
            id="user_page",
            method="GET",
            path="/sys/user/page",
            name="分页查询用户",
            query=[
                Param(name="current", location="query"),
                Param(name="size", location="query"),
            ],
            response_example={
                "code": "00000",
                "success": True,
                "data": {
                    "records": [{"userId": 1, "username": "admin", "shopId": 1}],
                    "total": 1,
                },
            },
        ),
        Endpoint(
            id="user_delete",
            method="DELETE",
            path="/sys/user",
            name="批量删除用户",
            body=[0],
        ),
    ]


def _addr_endpoints() -> list[Endpoint]:
    """``/user/addr`` 这类接口：没有 name/title 字段，只有收货人、手机号、地址。"""
    addr_body = {
        "addrId": 0,
        "userId": "string",
        "receiver": "string",
        "mobile": "string",
        "addr": "string",
        "postCode": "string",
        "status": 0,
    }
    return [
        Endpoint(
            id="addr_post",
            method="POST",
            path="/user/addr",
            name="新增收货地址",
            body=dict(addr_body),
            body_params=[
                Param(name=name, location="body") for name in addr_body
            ],
        ),
        Endpoint(
            id="addr_put",
            method="PUT",
            path="/user/addr",
            name="修改收货地址",
            body=dict(addr_body),
            body_params=[
                Param(name=name, location="body") for name in addr_body
            ],
        ),
        Endpoint(
            id="addr_page",
            method="GET",
            path="/user/addr/page",
            name="分页查询收货地址",
            query=[
                Param(name="current", location="query"),
                Param(name="size", location="query"),
            ],
            response_example={
                "code": "00000",
                "success": True,
                "data": {
                    "records": [
                        {
                            "addrId": 1,
                            "userId": "u1",
                            "receiver": "张三",
                            "mobile": "13800138000",
                            "addr": "某路1号",
                            "postCode": "000000",
                            "status": 0,
                        }
                    ],
                    "total": 1,
                },
            },
        ),
        Endpoint(
            id="addr_delete",
            method="DELETE",
            path="/user/addr/{addrId}",
            name="删除收货地址",
            path_params=[Param(name="addrId", location="path")],
        ),
    ]


def test_name_field_falls_back_to_body_string_field():
    """没有 name/title 的接口也要能造数：退到请求体里的字符串字段（receiver）。"""
    chain = plan_data_chain(
        {
            "method": "POST",
            "path": "/user/addr",
            "data": {
                "receiver": "张三",
                "mobile": "13800138000",
                "addr": "某路1号",
            },
        },
        _addr_endpoints(),
        ProjectContext(project="mall4j", contract={"success_code": "00000"}),
        prefer_main_body=True,
    )

    assert chain is not None
    assert chain.setups == []
    assert "tools.fixed" in chain.main_data["receiver"]
    # 手机号是被服务端校验格式的字段，不能被换成种子字符串
    assert chain.main_data["mobile"] == "13800138000"
    assert chain.main_data["addr"] == "某路1号"

    lookup, cleanup = chain.teardowns
    assert lookup["path"] == "/user/addr/page"
    assert lookup["params"]["size"] >= 100, "现役用例写的 size=10 会把造的那条漏掉"
    assert '"receiver"' in cleanup["path"]
    assert '"addrId"' in cleanup["path"]


def test_name_field_skips_id_and_formatted_fields():
    """兜底挑字段时跳过 id / 手机号 / 邮编 / 状态这类不能当标签的字段。"""
    endpoint = Endpoint(
        id="addr_post",
        method="POST",
        path="/user/addr",
        body_params=[
            Param(name="mobile", location="body"),
            Param(name="postCode", location="body"),
            Param(name="addrId", location="body"),
            Param(name="userId", location="body"),
            Param(name="status", location="body"),
            Param(name="receiver", location="body"),
        ],
    )
    sample = {
        "addrId": 1,
        "userId": "u1",
        "receiver": "张三",
        "mobile": "13800138000",
        "postCode": "000000",
        "status": 0,
    }

    assert _infer_name_field(sample, endpoint) == "receiver"


def test_put_seed_body_prefers_callers_request_over_doc_placeholders():
    """PUT 造数用调用方给的报文，不用文档里的 ``userId: "string"`` 占位值。"""
    chain = plan_data_chain(
        {
            "method": "PUT",
            "path": "/user/addr",
            "data": {
                "addrId": 0,
                "receiver": "张三",
                "mobile": "13800138000",
                "addr": "某路1号",
            },
        },
        _addr_endpoints(),
        ProjectContext(project="mall4j", contract={"success_code": "00000"}),
        prefer_main_body=True,
    )

    assert chain is not None
    seed = chain.setups[0]["data"]
    assert "userId" not in seed, "文档占位值不能进造数报文"
    assert "addrId" not in seed, "造数要新建一条，不能带主键"
    assert "tools.fixed" in seed["receiver"]

    assert "tools.fixed" in chain.main_data["receiver"]
    assert "tools.find_id_by_fixed" in chain.main_data["addrId"]
    assert [step["id"] for step in chain.setups] == ["造数据", "查数据"]
    assert [step["id"] for step in chain.teardowns] == ["清理数据"]


def test_entity_derived_id_accepts_blacklisted_user_id():
    """``userId`` 在黑名单里，但作为 ``/sys/user`` 的主键必须能用。

    只有集合路径的实体名和字段名对得上（``/sys/user`` → ``user`` →
    ``userId``）时才认它。否则别的接口记录里带的创建人 ``userId`` 会被
    误当主键 —— 清理就可能删掉环境里原有的一条记录。
    """
    chain = plan_data_chain(
        {
            "method": "POST",
            "path": "/sys/user",
            "data": {"username": "autotest_seed", "password": "123456"},
        },
        _user_endpoints(),
        ProjectContext(project="mall4j", contract={"success_code": "00000"}),
        prefer_main_body=True,
    )

    assert chain is not None
    lookup, cleanup = chain.teardowns
    assert lookup["id"] == "查数据"
    assert cleanup["method"] == "DELETE"
    assert '"userId"' in cleanup["data"][0]
    assert "tools.fixed" in chain.main_data["username"]

    # 集合路径实体名对不上时，记录里的 userId 仍按外键处理，不能当主键
    assert _infer_id_field({"userId": 1, "orderNo": "x"}, "orderNo", None, entity="order") == ""


def test_delete_by_body_wraps_id_in_array():
    endpoints = _endpoints()
    endpoints.append(
        Endpoint(
            id="area_delete_body",
            method="DELETE",
            path="/admin/area",
            name="批量删除地区",
            body=[0],
        )
    )
    chain = plan_data_chain(
        {"method": "DELETE", "path": "/admin/area"},
        endpoints,
        _context(),
    )

    assert chain is not None
    assert chain.main_data == [
        '{{tools.find_id_by_fixed(查数据.data, "areaName", '
        '"mall4j_admin_area_areaName_seed", "areaId", "AI造数")}}'
    ]


# ---------------------------------------------------------------------------
# 拒绝出链路的场景
# ---------------------------------------------------------------------------


def test_negative_case_gets_no_chain():
    assert (
        plan_data_chain(
            {"method": "POST", "path": "/admin/area", "data": {}},
            _endpoints(),
            _context(),
            positive=False,
        )
        is None
    )


def test_missing_lookup_gets_no_chain():
    endpoints = [ep for ep in _endpoints() if "list" not in ep.path and "page" not in ep.path]
    assert (
        plan_data_chain(
            {"method": "POST", "path": "/admin/area", "data": {}},
            endpoints,
            _context(),
        )
        is None
    )


def test_multi_step_case_is_left_manual():
    case = {
        "testcase": {
            "name": "先查再删",
            "marks": ["positive"],
            "steps": [
                {"id": "step1", "method": "GET", "path": "/admin/area/list"},
                {"id": "step2", "method": "DELETE", "path": "/admin/area/{id}"},
            ],
        }
    }
    assert attach_chain(case["testcase"], _endpoints(), _context()).startswith("failed:")


# ---------------------------------------------------------------------------
# 模型/手写产物结构不对时不能把生成打断
# ---------------------------------------------------------------------------


def test_attach_chain_tolerates_non_list_steps_and_marks():
    # steps 不是列表：按「没有写步骤」处理，而不是 TypeError
    assert (
        attach_chain({"steps": 5, "marks": ["positive"]}, _endpoints(), _context())
        == "skipped:no_write"
    )
    assert (
        attach_chain({"steps": "nope", "marks": "positive"}, _endpoints(), _context())
        == "skipped:no_write"
    )

    # marks 不是列表：按「没有反例标签」处理，照常挂链路
    case = {
        "name": "新增地区（正常）",
        "marks": 5,
        "steps": [
            {
                "id": "s1",
                "method": "POST",
                "path": "/admin/area",
                "data": dict(POST_BODY),
            }
        ],
    }
    assert attach_chain(case, _endpoints(), _context()).startswith("ok:")


def test_plan_skips_non_endpoint_entries():
    endpoints = ["garbage", 5, None, *_endpoints()]
    chain = plan_data_chain(
        {"method": "POST", "path": "/admin/area", "data": dict(POST_BODY)},
        endpoints,
        _context(),
    )
    assert chain is not None


def test_plan_tolerates_non_dict_examples_and_contract():
    ctx = _context()
    ctx.examples_by_endpoint = {
        ("POST", "/admin/area"): [
            None,
            "坏了",
            {"data": dict(TRUSTED_POST_BODY), "marks": ["positive"]},
        ]
    }
    ctx.contract = "broken"  # 契约解析异常时不能 AttributeError

    chain = plan_data_chain(
        {"method": "POST", "path": "/admin/area", "data": dict(POST_BODY)},
        _endpoints(),
        ctx,
    )

    assert chain is not None
    assert chain.main_data["parentId"] == TRUSTED_POST_BODY["parentId"]
    # 拿不到成功码就不写空断言
    assert all("assert" not in step for step in chain.teardowns)


def test_post_prefers_main_body_when_caller_asks():
    """``create_case`` 的 request_data 优先，不被现役样例覆盖。

    智能体按接口文档准备的报文是这次要发的真实请求；如果造数还用现役
    样例里的另一份 body，主步骤请求体和用例断言就对不上了。
    """
    main_body = {"areaName": "测试区域", "parentId": 110000000000, "level": 2}
    chain = plan_data_chain(
        {"method": "POST", "path": "/admin/area", "data": dict(main_body)},
        _endpoints(),
        _context(),
        prefer_main_body=True,
    )

    assert chain is not None
    dumped = json.dumps(chain.main_data, ensure_ascii=False)
    assert chain.main_data["parentId"] == 110000000000
    assert "测试区域" not in dumped
    assert "tools.fixed" in dumped


# ---------------------------------------------------------------------------
# 接入 case_gen
# ---------------------------------------------------------------------------


def test_finalize_attaches_chain_and_drops_manual_mark():
    case = {
        "testcase": {
            "name": "删除地区（正常）",
            "description": "正常删除",
            "marks": ["api", "positive", "p1"],
            "steps": [
                {
                    "id": "step1",
                    "description": "删除地区",
                    "method": "DELETE",
                    # 已经解析成真实 id，规划器仍要能换回模板
                    "path": "/admin/area/110000000000",
                    "assert": [{"type": "status_code", "expected": 200}],
                }
            ],
        }
    }

    result = _finalize_ai_case(case, _context(), _endpoints())

    assert result is not None
    tc = result["testcase"]
    assert "needs_manual_setup" not in tc["marks"]
    assert "api_data_chain" in tc["marks"]
    assert tc["setups"]
    assert tc["steps"][0]["path"].startswith("/admin/area/{{tools.find_id_by_fixed(")
    assert write_is_self_cleaning(tc) is True
    assert "数据构造" in tc["description"]


def test_finalize_drops_stale_manual_mark_when_chain_attaches():
    """DELETE 的 {id} 解析失败先标了人工，挂上链路后必须撤掉。"""
    case = {
        "testcase": {
            "name": "删除地区（正常）",
            "marks": ["api", "positive", "p1", "needs_manual_setup"],
            "steps": [
                {
                    "id": "step1",
                    "description": "删除地区",
                    "method": "DELETE",
                    "path": "/admin/area/110000000000",
                    "assert": [{"type": "status_code", "expected": 200}],
                }
            ],
        }
    }

    result = _finalize_ai_case(case, _context(), _endpoints())

    marks = result["testcase"]["marks"]
    assert "api_data_chain" in marks
    assert "needs_manual_setup" not in marks


def test_finalize_keeps_manual_when_no_chain_exists():
    endpoints = [
        ep for ep in _endpoints() if "list" not in ep.path and "page" not in ep.path
    ]
    case = {
        "testcase": {
            "name": "新增地区（正常）",
            "marks": ["api", "positive", "p1"],
            "steps": [
                {
                    "id": "step1",
                    "description": "新增地区",
                    "method": "POST",
                    "path": "/admin/area",
                    "data": dict(POST_BODY),
                    "assert": [{"type": "status_code", "expected": 200}],
                }
            ],
        }
    }

    result = _finalize_ai_case(case, _context(), endpoints)

    assert result is not None
    assert "needs_manual_setup" in result["testcase"]["marks"]


def test_unsafe_teardowns_flags_fixed_targets():
    """硬编码 id 的清理删的是环境原有记录，必须识别出来。"""
    case = {
        "teardowns": [
            {"id": "清理", "method": "DELETE", "path": "/admin/brand/100"},
            {"id": "改状态", "method": "PUT", "path": "/admin/message/cancel/1"},
        ]
    }

    assert len(unsafe_teardowns(case)) == 2


def test_unsafe_teardowns_accepts_any_variable_reference():
    """路径/查询参数/请求体任意一处引用了变量，就算可追溯。"""
    case = {
        "teardowns": [
            {"id": "路径", "method": "DELETE", "path": "/admin/brand/{{造品牌.data.id}}"},
            {
                "id": "请求体",
                "method": "DELETE",
                "path": "/admin/hotSearch",
                "params": {"body": "{{tools.safe_id('hot_search')}}"},
            },
            {
                "id": "请求数据",
                "method": "PUT",
                "path": "/admin/message/cancel",
                "data": {"ids": "{{创建消息.data.ids}}"},
            },
        ]
    }

    assert unsafe_teardowns(case) == []


def test_unsafe_teardowns_ignores_non_write_teardowns():
    case = {
        "teardowns": [
            {"id": "删库", "method": "db", "path": ""},
            {"id": "工厂", "operation_type": "factory"},
        ]
    }

    assert unsafe_teardowns(case) == []


def test_finalize_removes_fixed_id_teardown_and_marks_manual():
    """模型自带 setups/teardowns 时 attach_chain 不接管 —— 由护栏兜底。"""
    case = {
        "testcase": {
            "name": "修改地区（正常）",
            "marks": ["api", "positive", "p1"],
            "setups": [
                {
                    "id": "造数据",
                    "operation_type": "api",
                    "method": "POST",
                    "path": "/admin/area",
                    "data": dict(POST_BODY),
                }
            ],
            "teardowns": [
                {
                    "id": "清理",
                    "operation_type": "api",
                    "method": "DELETE",
                    "path": "/admin/area/110000000000",
                }
            ],
            "steps": [
                {
                    "id": "step1",
                    "description": "修改地区",
                    "method": "PUT",
                    "path": "/admin/area",
                    "data": {"areaId": 110000000000, "areaName": "x"},
                    "assert": [{"type": "status_code", "expected": 200}],
                }
            ],
        }
    }

    result = _finalize_ai_case(case, _context(), _endpoints())

    tc = result["testcase"]
    assert not tc.get("teardowns")
    assert "needs_manual_setup" in tc["marks"]


def test_finalize_keeps_traceable_teardown():
    case = {
        "testcase": {
            "name": "修改地区（正常）",
            "marks": ["api", "positive", "p1"],
            "setups": [
                {
                    "id": "造数据",
                    "operation_type": "api",
                    "method": "POST",
                    "path": "/admin/area",
                    "data": dict(POST_BODY),
                }
            ],
            "teardowns": [
                {
                    "id": "清理",
                    "operation_type": "api",
                    "method": "DELETE",
                    "path": "/admin/area/{{查数据.areaId}}",
                }
            ],
            "steps": [
                {
                    "id": "step1",
                    "description": "修改地区",
                    "method": "PUT",
                    "path": "/admin/area",
                    "data": {"areaId": "{{查数据.areaId}}", "areaName": "x"},
                    "assert": [{"type": "status_code", "expected": 200}],
                }
            ],
        }
    }

    result = _finalize_ai_case(case, _context(), _endpoints())

    assert len(result["testcase"]["teardowns"]) == 1


# ---------------------------------------------------------------------------
# Excel 落盘格式
# ---------------------------------------------------------------------------


def test_prepost_asserts_are_flattened_for_excel():
    steps = [
        {
            "id": "查数据",
            "operation_type": "api",
            "path": "/admin/area/list",
            "method": "GET",
            "assert": [{"type": "equal", "field": "code", "expected": "00000"}],
        }
    ]

    flat = _prepost_to_flat(steps)

    assert flat[0]["assert"] == [
        {"check": "body", "jsonpath": "$.code", "comparator": "==", "expect": "00000"}
    ]
    # 幂等：已经是扁平格式的不能再翻一遍
    assert _prepost_to_flat(flat) == flat
