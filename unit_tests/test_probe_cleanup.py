"""写探针足迹 → 清理计划（``ai/pipeline/probe_cleanup.py``）。

删除不可逆，所以这一步刻意拆成两半：足迹（``probe`` 写）和计划
（本模块算）。计划生成必须是**纯函数、不发请求**，而且宁可跳过也不能
给一个「可能删错」的 DELETE。

这里锁三件事：

1. 路径参数能被响应 id 正确填上（含 ``{id}`` ↔ ``areaId`` 这种改名）
2. 填不上的情况一律跳过，并给出原因 —— 不猜
3. ``plan_cleanup`` 只做规划，不导入任何 HTTP 客户端
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai.pipeline import probe_cleanup as pc  # noqa: E402


# ---------------- 路径拼装 ----------------


def test_resolves_by_placeholder_name():
    path, reason = pc.resolve_delete_path("DELETE /admin/area/{areaId}", {"areaId": 12})
    assert path == "/admin/area/12"
    assert reason == ""


def test_resolves_id_placeholder_from_renamed_response_field():
    """删除路径写 ``{id}``、响应字段叫 ``areaId`` —— 单 id 时就是它。"""
    path, reason = pc.resolve_delete_path("DELETE /admin/area/{id}", {"areaId": 12})
    assert path == "/admin/area/12"
    assert reason == ""


def test_skips_when_multiple_ids_do_not_match_placeholder():
    path, reason = pc.resolve_delete_path(
        "DELETE /admin/area/{id}", {"areaId": 12, "parentId": 3}
    )
    assert path == ""
    assert "多个 id" in reason


def test_resolves_generic_id_by_entity_name():
    """``{id}`` + 多个 id 时，按资源名挑 ``areaId``，不碰 ``parentId``。"""
    path, reason = pc.resolve_delete_path(
        "DELETE /admin/area/{id}", {"areaId": 12, "parentId": 3}, entity="area"
    )
    assert path == "/admin/area/12"
    assert reason == ""


def test_entity_mismatch_still_skips():
    """资源名对不上宁可不删 —— 挑错了就是删别人的数据。"""
    path, reason = pc.resolve_delete_path(
        "DELETE /admin/area/{id}", {"goodsId": 12, "parentId": 3}, entity="area"
    )
    assert path == ""
    assert "多个 id" in reason


@pytest.mark.parametrize(
    "api,expected",
    [
        ("POST /admin/area", "area"),
        ("PUT /admin/area/{id}", "area"),
        ("POST /sys/config/save", "save"),
        ("", ""),
    ],
)
def test_entity_of_takes_last_path_segment(api, expected):
    assert pc._entity_of(api) == expected


def test_skips_when_no_id_in_response():
    path, reason = pc.resolve_delete_path("DELETE /admin/area/{id}", {})
    assert path == ""
    assert "没有 id" in reason


def test_skips_when_no_delete_template():
    path, reason = pc.resolve_delete_path("", {"id": 1})
    assert path == ""
    assert "不是 DELETE" in reason


def test_skips_when_template_has_no_path_param():
    """没有路径参数的 DELETE 会删一批，谁也不知道删掉什么 —— 不给。"""
    path, reason = pc.resolve_delete_path("DELETE /admin/area", {"id": 1})
    assert path == ""
    assert "没有路径参数" in reason


@pytest.mark.parametrize("bad", ["1/2", "..", "1?x=2", "1#y", "a b", ""])
def test_skips_unsafe_id_values(bad):
    path, reason = pc.resolve_delete_path("DELETE /admin/area/{id}", {"id": bad})
    assert path == ""
    assert "不像合法主键" in reason


@pytest.mark.parametrize("bad", [None, True, False])
def test_skips_non_scalar_id_types(bad):
    """``str(None)`` 是 "None"、``str(True)`` 是 "True"，都能过字符白名单 ——
    不按类型挡掉就会拼出 ``/admin/area/None`` 这种 DELETE。"""
    path, reason = pc.resolve_delete_path("DELETE /admin/area/{id}", {"id": bad})
    assert path == ""
    assert "不是有效主键" in reason


def test_skips_non_delete_method():
    path, reason = pc.resolve_delete_path("POST /admin/area/{id}", {"id": 1})
    assert path == ""
    assert "不是 DELETE" in reason


# ---------------- 计划汇总 ----------------


def _footprint(cleanup: str, ids: dict, api: str = "POST /admin/area", label: str = "正例"):
    return {"接口": api, "测点": label, "清理接口": cleanup, "ids": ids, "请求": {}}


def test_plan_splits_planned_and_skipped():
    plan = pc.plan_cleanup(
        [
            _footprint("DELETE /admin/area/{id}", {"id": 1}),
            _footprint("DELETE /admin/area/{id}", {}),
            _footprint("", {"id": 3}),
        ]
    )

    assert plan["total"] == 3
    assert len(plan["planned"]) == 1
    assert plan["planned"][0]["path"] == "/admin/area/1"
    assert len(plan["skipped"]) == 2
    assert all(item["跳过原因"] for item in plan["skipped"])


def test_plan_uses_entity_from_create_endpoint():
    """计划层要把「接口」里的资源名传给路径拼装，否则多 id 场景一律跳过。"""
    plan = pc.plan_cleanup(
        [_footprint("DELETE /admin/area/{id}", {"areaId": 12, "parentId": 3})]
    )

    assert len(plan["planned"]) == 1
    assert plan["planned"][0]["path"] == "/admin/area/12"


def test_plan_keeps_context_for_review():
    """计划里要能看到这条 DELETE 对应哪次探针 —— 不然人复核不了。"""
    plan = pc.plan_cleanup(
        [_footprint("DELETE /admin/area/{id}", {"id": 9}, api="POST /admin/area", label="A1 必填缺失")]
    )

    item = plan["planned"][0]
    assert item["method"] == "DELETE"
    assert item["接口"] == "POST /admin/area"
    assert item["测点"] == "A1 必填缺失"


def test_plan_is_pure_and_does_not_import_http():
    """这条测试是护栏：本模块不该有任何发请求的能力。"""
    source = Path(pc.__file__).read_text(encoding="utf-8")
    for forbidden in ("requests", "ProbeClient", "session.request", "make_session"):
        assert forbidden not in source, f"清理计划模块里出现了 {forbidden}"


# ---------------- 读文件 ----------------


def test_load_footprints_accepts_wrapper_and_bare_list(tmp_path):
    wrapped = tmp_path / "wrapped.json"
    wrapped.write_text('{"items": [{"ids": {"id": 1}}]}', encoding="utf-8")
    bare = tmp_path / "bare.json"
    bare.write_text('[{"ids": {"id": 2}}]', encoding="utf-8")

    assert pc.load_footprints(wrapped) == [{"ids": {"id": 1}}]
    assert pc.load_footprints(bare) == [{"ids": {"id": 2}}]


def test_load_footprints_rejects_bad_shape(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"items": "nope"}', encoding="utf-8")

    with pytest.raises(ValueError):
        pc.load_footprints(bad)
