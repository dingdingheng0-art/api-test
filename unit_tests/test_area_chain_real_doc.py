"""用真实的 mall4j OpenAPI 验证造数链路能落地。

前面 ``test_data_orchestrator.py`` 用的是手写的假接口；这里换成
``.cache/apidoc/openapi.json`` 里真实解析出来的 ``Endpoint``，验证：

1. 文档里确实有造数/查数/清理三兄弟（POST、GET /list、DELETE /{id}）
2. 规划器能从真实 schema 合成出的响应示例里推断出 ``areaName`` / ``areaId``
3. POST / PUT / DELETE 三条主步骤都能拼出完整链路（发不出链路就该返回 None）

**纯静态**：只解析文档、只规划，不发 HTTP、不写任何文件。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai.capabilities.case_gen import ProjectContext
from ai.pipeline.data_orchestrator import plan_data_chain
from tools.importer.dispatcher import parse_file

DOC = PROJECT_ROOT / ".cache" / "apidoc" / "openapi.json"


@pytest.fixture(scope="module")
def area_endpoints():
    assert DOC.exists(), f"缺少接口文档 {DOC}"
    endpoints = parse_file(str(DOC))
    return [ep for ep in endpoints if ep.path.startswith("/admin/area")]


@pytest.fixture(scope="module")
def ctx() -> ProjectContext:
    # module_override 模拟 --sandbox 时的模块名，固定值 key 会带 area_chain
    return ProjectContext(
        project="mall4j",
        module_override="area_chain",
        contract={"success_code": "00000", "success_field": "success", "http_status": 200},
    )


def _find(endpoints, method: str, path: str):
    for ep in endpoints:
        if ep.method.upper() == method and ep.path == path:
            return ep
    raise AssertionError(f"文档里找不到 {method} {path}")


def test_doc_has_the_three_siblings(area_endpoints):
    """造数/查数/清理三个接口必须都在，否则链路根本无从谈起。"""
    found = {(ep.method.upper(), ep.path) for ep in area_endpoints}
    assert ("POST", "/admin/area") in found
    assert ("GET", "/admin/area/list") in found
    assert ("DELETE", "/admin/area/{id}") in found


def test_response_example_carries_area_id_and_name(area_endpoints):
    """真实 schema 合成出的示例必须带 areaId/areaName，推断才有依据。"""
    lookup = _find(area_endpoints, "GET", "/admin/area/list")
    records = (lookup.response_example or {}).get("data")
    assert isinstance(records, list) and records, "list 接口的 data 应是数组"
    sample = records[0]
    assert "areaName" in sample
    assert "areaId" in sample


def test_post_chain_on_real_doc(area_endpoints, ctx):
    producer = _find(area_endpoints, "POST", "/admin/area")
    chain = plan_data_chain(
        {"method": "POST", "path": "/admin/area", "data": dict(producer.body or {})},
        area_endpoints,
        ctx,
    )

    assert chain is not None, "POST 正例在真实文档上应能拼出链路"
    assert chain.setups == []
    assert [s["id"] for s in chain.teardowns] == ["查数据", "清理数据"]
    # 主步骤名称必须换成固定种子名，不能留着文档里的 "string"
    assert chain.main_data["areaName"].startswith("{{tools.fixed(")
    lookup, cleanup = chain.teardowns
    assert lookup["path"] == "/admin/area/list"
    assert cleanup["method"] == "DELETE"
    assert cleanup["path"].startswith("/admin/area/{{tools.find_id_by_fixed(")
    assert '"areaName"' in cleanup["path"]
    assert '"areaId"' in cleanup["path"]


def test_put_chain_on_real_doc(area_endpoints, ctx):
    put = _find(area_endpoints, "PUT", "/admin/area")
    chain = plan_data_chain(
        {"method": "PUT", "path": "/admin/area", "data": dict(put.body or {"areaId": 0})},
        area_endpoints,
        ctx,
    )

    assert chain is not None, "PUT 正例在真实文档上应能拼出链路"
    assert [s["id"] for s in chain.setups] == ["造数据", "查数据"]
    # 文档示例的 areaId=0 实测报 A00001，必须被换成查到的真实 id
    assert "find_id_by_fixed" in str(chain.main_data.get("areaId"))
    assert chain.main_data["areaName"].startswith("{{tools.fixed(")
    assert [s["id"] for s in chain.teardowns] == ["清理数据"]


def test_delete_chain_on_real_doc(area_endpoints, ctx):
    chain = plan_data_chain(
        {"method": "DELETE", "path": "/admin/area/110000000000"},
        area_endpoints,
        ctx,
    )

    assert chain is not None, "DELETE 正例在真实文档上应能拼出链路"
    assert [s["id"] for s in chain.setups] == ["造数据", "查数据"]
    assert chain.teardowns == []
    assert chain.self_cleaning is True
    assert chain.main_path.startswith("/admin/area/{{tools.find_id_by_fixed(")
