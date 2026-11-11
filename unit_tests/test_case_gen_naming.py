"""AI 生成用例的命名规范（不联网）。

现役 132 条手工 Excel 用例里，129 条用例名称是
「模块中文名 - 业务功能（场景）」，例如::

    地区管理 - 修改地区（正常请求）

报告标题则由「接口名称」「用例标题」两列拼成::

    [地区管理] 修改地区 - （正常请求）

AI 生成器以前直接用 ``Endpoint.display_name``，mall4j 的 OpenAPI 又没有
summary，于是落成了 ``PUT /admin/area`` 这种「方法 + 路径」的名字。
这里锁住修正后的三条规则：

1. 业务名优先取现役用例的「接口名称」，其次才是接口元数据
2. ``业务名（场景）`` 要拆成「接口名称 = 业务名」「用例标题 = （场景）」
3. 完整用例名称必须带上模块中文名
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai.capabilities.case_gen import (
    ProjectContext,
    _build_module_prompt,
    _business_name,
    _full_case_name,
    _name_parts,
)
from tools.importer.models import Endpoint

PUT_AREA = Endpoint(
    id="area_put",
    method="PUT",
    path="/admin/area",
    # mall4j 的 OpenAPI 没有 summary，display_name 会退化成 "PUT /admin/area"
    tags=["area-controller"],
    body={"areaId": 0, "areaName": "string"},
)


def test_business_name_prefers_live_case_name():
    ctx = ProjectContext(project="mall4j")
    ctx.examples_by_endpoint = {
        ("PUT", "/admin/area"): [{"case": "地区管理 - 修改地区（正常请求）"}]
    }
    assert _business_name(PUT_AREA, ctx) == "修改地区"


def test_business_name_uses_chinese_metadata_before_display_name():
    """没有现役用例可参考时，中文接口元数据优先于 display_name。"""
    ctx = ProjectContext(project="mall4j")
    ctx.examples_by_endpoint = {}
    ep = Endpoint(id="x", method="PUT", path="/admin/area", name="修改地区")
    assert _business_name(ep, ctx) == "修改地区"


def test_name_parts_split_business_and_scenario():
    api_name, scenario = _name_parts(
        {"name": "修改地区（正常请求）", "api_name": "修改地区", "scenario": "（正常请求）"},
        {"description": "修改地区"},
    )
    assert api_name == "修改地区"
    assert scenario == "（正常请求）"


def test_full_case_name_matches_live_convention():
    assert (
        _full_case_name("mall4j", "admin_area", {"path": "/admin/area"}, "修改地区", "（正常请求）")
        == "地区管理 - 修改地区（正常请求）"
    )


def test_module_prompt_carries_the_live_chinese_name():
    """提示词里带现役中文名，模型才不会拿 ``PUT /admin/area`` 当业务名。"""
    ctx = ProjectContext(project="mall4j")
    ctx.examples_by_endpoint = {
        ("PUT", "/admin/area"): [
            {"case": "修改地区", "method": "PUT", "path": "/admin/area"}
        ]
    }
    prompt = _build_module_prompt("admin_area", [PUT_AREA], ctx, max_cases=1)
    assert "现役业务名" in prompt
    assert "修改地区" in prompt


def test_full_case_name_keeps_sentence_scenario_separate():
    """模型只给整句描述时，不要拼成「删除地区删除区域成功」。"""
    assert (
        _full_case_name(
            "mall4j", "admin_area", {"path": "/admin/area"}, "删除地区", "删除已存在的区域成功"
        )
        == "地区管理 - 删除地区 - 删除已存在的区域成功"
    )
