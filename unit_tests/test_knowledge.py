"""知识层单元测试：存储、字段映射、建图、召回。

**不联网、不连库、不跑用例**。测试用的数据是手工造的小样本，
真实建图另有一组标记为 ``integration`` 的用例（依赖 ``.cache/`` 快照）。

测的重点是**判断逻辑**，不是数据量：

- 置信度三档的边界对不对
- 已验证的证据会不会被规则推导冲掉、会不会错误地推广到别的接口
- 同名的两条用例会不会被合并成一条
- FTS 查询里的特殊字符会不会炸
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# --------------------------------------------------------------------------
# 存储
# --------------------------------------------------------------------------


@pytest.fixture()
def store():
    from ai.knowledge.store import KnowledgeStore

    s = KnowledgeStore(":memory:")
    yield s
    s.close()


def test_store_writes_and_reads_nodes(store):
    store.add_node("table:tz_area", "table", "tz_area", props={"comment": "地区"})
    node = store.get_node("table:tz_area")
    assert node is not None
    assert node["kind"] == "table"
    assert node["props"]["comment"] == "地区"


def test_store_bulk_add_is_idempotent(store):
    nodes = [{"id": "t:1", "kind": "table", "name": "t1"}]
    edges = [{"src": "t:1", "dst": "t:1", "kind": "self"}]

    store.bulk_add(nodes, edges)
    store.bulk_add(nodes, edges)

    assert store.stats()["nodes"] == 1
    assert store.stats()["edges"] == 1


def test_store_clear_empties_graph(store):
    store.bulk_add([{"id": "t:1", "kind": "table", "name": "t1"}], [])
    store.clear()
    assert store.stats()["nodes"] == 0


def test_store_neighbors_both_directions(store):
    store.bulk_add(
        [
            {"id": "a", "kind": "param", "name": "a"},
            {"id": "b", "kind": "column", "name": "b"},
        ],
        [{"src": "a", "dst": "b", "kind": "maps_to", "confidence": 0.9}],
    )
    assert [n["id"] for n in store.neighbors("a")] == ["b"]
    assert [n["id"] for n in store.neighbors("b")] == ["a"]


def test_store_search_finds_node(store):
    store.add_node("table:tz_area", "table", "tz_area", body="地区 地区表")
    hits = store.search("tz_area")
    assert any(h["id"] == "table:tz_area" for h in hits)


def test_store_search_tolerates_fts_special_chars(store):
    """``-`` ``*`` ``(`` 在 FTS5 里是语法字符，不能让整次搜索报错。"""
    store.add_node("table:tz_area", "table", "tz_area", body="地区")
    for query in ("area-name", "tz_*", "(area)", 'quo"te', "a AND b"):
        store.search(query)   # 不抛异常即可


def test_store_empty_search_returns_empty(store):
    store.add_node("t:1", "table", "t1")
    assert store.search("") == []
    assert store.search("   ") == []


def test_store_meta_roundtrip(store):
    store.set_meta("built_at", 123.5)
    assert store.get_meta("built_at") == 123.5
    assert store.get_meta("missing", default="x") == "x"


# --------------------------------------------------------------------------
# 字段映射命名规则
# --------------------------------------------------------------------------


def test_snake_case_handles_common_forms():
    from ai.knowledge.field_map import snake_case

    assert snake_case("areaName") == "area_name"
    assert snake_case("AreaID") == "area_id"
    assert snake_case("userID") == "user_id"
    assert snake_case("is_top") == "is_top"
    assert snake_case("shopId") == "shop_id"


def test_names_match_tiers():
    from ai.knowledge.field_map import names_match

    assert names_match("areaName", "area_name") == (True, "驼峰转下划线同名")
    assert names_match("is_top", "is_top")[0] is True
    assert names_match("isTop", "is_top")[0] is True
    # 同义词
    assert names_match("pic", "image")[0] is True
    # 无关字段不能硬凑
    assert names_match("prodName", "area_name")[0] is False


def test_is_tech_column():
    from ai.knowledge.field_map import is_tech_column

    assert is_tech_column("id") is True
    assert is_tech_column("createTime") is True
    assert is_tech_column("areaName") is False


def test_table_candidates_prefers_schema_origin():
    from ai.knowledge.field_map import table_candidates_detail

    detail = table_candidates_detail("Area")
    sources = [origin for _name, origin in detail]
    assert sources[0] == "schema"
    assert any(name == "tz_area" for name, _ in detail)


# --------------------------------------------------------------------------
# SQL 解析
# --------------------------------------------------------------------------


def test_parse_sql_simple_select():
    from ai.knowledge.builder import parse_sql

    table, select_cols, where_cols = parse_sql(
        "SELECT area_name FROM tz_area WHERE area_id = %s"
    )
    assert table == "tz_area"
    assert select_cols == ["area_name"]
    assert where_cols == ["area_id"]


def test_parse_sql_rejects_multi_table():
    from ai.knowledge.builder import parse_sql

    # JOIN 的映射是模糊的，宁可放弃
    assert parse_sql("SELECT a.x FROM tz_a a JOIN tz_b b ON a.id = b.id") == ("", [], [])
    assert parse_sql("SELECT x FROM tz_a, tz_b WHERE tz_a.id = tz_b.id") == ("", [], [])


def test_parse_sql_skips_functions():
    from ai.knowledge.builder import parse_sql

    table, select_cols, _where = parse_sql("SELECT COUNT(*), area_name FROM tz_area")
    assert table == "tz_area"
    assert select_cols == ["area_name"]


def test_parse_sql_garbage_returns_empty():
    from ai.knowledge.builder import parse_sql

    assert parse_sql("") == ("", [], [])
    assert parse_sql("not a sql") == ("", [], [])


# --------------------------------------------------------------------------
# 用例身份（回归：同名用例被合并成一条）
# --------------------------------------------------------------------------


def test_case_identities_uses_case_no():
    from ai.agent.tools._common import case_identities

    cases = [{"name": "修改地区", "case_id": "API-168"}]
    assert case_identities(cases) == ["API-168"]


def test_case_identities_distinguishes_same_name_cases():
    """「查询地区详情」正常/查不到数据是**两条**用例，名字完全一样。

    只按名字算 ID 会把它们合并成一条 —— 智能体拿详情只会看到其中一条。
    """
    from ai.agent.tools._common import case_identities

    cases = [
        {"name": "查询地区详情", "description": "（正常请求）"},
        {"name": "查询地区详情", "description": "（查不到数据）"},
    ]
    identities = case_identities(cases)
    assert len(set(identities)) == 2


def test_case_identities_ignores_index_when_no_collision():
    """不重名时只用名字 —— 插入新用例不会让别人的 ID 挪位。"""
    from ai.agent.tools._common import case_identities

    before = case_identities([{"name": "A"}, {"name": "B"}])
    after = case_identities([{"name": "B"}])
    assert after[0] == before[1]


def test_assign_ids_are_unique_within_file():
    from ai.agent.tools._common import assign_ids

    cases = [
        {"name": "查询地区详情", "description": "（正常请求）"},
        {"name": "查询地区详情", "description": "（查不到数据）"},
        {"name": "修改地区", "case_id": "API-168"},
    ]
    ids = assign_ids("testcases/x/admin_area.xlsx", cases)
    assert len(set(ids)) == 3
    assert all(i.startswith("case_") for i in ids)


# --------------------------------------------------------------------------
# 建图（用小样本，不依赖 .cache）
# --------------------------------------------------------------------------


@pytest.fixture()
def mini_graph(tmp_path, monkeypatch):
    """用最小样本建一张图：一张表 + 一个端点 + 一条带 db_query 的用例。"""
    import json

    cache = tmp_path / ".cache"
    (cache / "schema").mkdir(parents=True)
    (cache / "apidoc").mkdir(parents=True)

    (cache / "schema" / "mini.json").write_text(
        json.dumps(
            {
                "tables": [
                    {
                        "name": "tz_area",
                        "comment": "地区",
                        "approx_rows": 10,
                        "columns": [
                            {"name": "area_id", "raw_type": "bigint", "primary_key": True},
                            {"name": "area_name", "raw_type": "varchar(50)"},
                        ],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    (cache / "apidoc" / "openapi.json").write_text(
        json.dumps(
            {
                "paths": {
                    "/admin/area": {
                        "put": {
                            "summary": "修改地区",
                            "tags": ["area"],
                            "requestBody": {
                                "content": {
                                    "application/json": {
                                        "schema": {"$ref": "#/components/schemas/Area"}
                                    }
                                }
                            },
                        }
                    }
                },
                "components": {
                    "schemas": {
                        "Area": {
                            "type": "object",
                            "properties": {
                                "areaId": {"type": "integer"},
                                "areaName": {"type": "string"},
                            },
                        }
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    from utils import paths

    monkeypatch.setattr(paths, "CACHE_DIR", cache)

    from ai.knowledge.builder import build
    from ai.knowledge.store import KnowledgeStore

    store = KnowledgeStore(":memory:")
    stats = build(store=store, include_cases=False)
    return store, stats


def test_build_creates_tables_and_columns(mini_graph):
    store, stats = mini_graph
    assert stats.tables == 1
    assert stats.columns == 2
    assert store.get_node("column:tz_area.area_name") is not None


def test_build_creates_endpoint_and_params(mini_graph):
    store, _stats = mini_graph
    assert store.get_node("endpoint:PUT:/admin/area") is not None
    assert store.get_node("param:endpoint:PUT:/admin/area.areaName") is not None


def test_build_derives_rule_mapping(mini_graph):
    """``areaName`` → ``tz_area.area_name``，表名来自请求体 DTO，置信度 0.9。"""
    store, _stats = mini_graph
    pid = "param:endpoint:PUT:/admin/area.areaName"
    mapped = [n for n in store.neighbors(pid, kind="maps_to")]
    assert any(n["id"] == "column:tz_area.area_name" for n in mapped)
    edge = next(n for n in mapped if n["id"] == "column:tz_area.area_name")
    assert edge["confidence"] == pytest.approx(0.9)


def test_derive_mapping_skips_tech_columns(mini_graph):
    """``id`` / ``createTime`` 这类技术字段每张表都有，规则推导不许猜。

    （``areaId`` → ``tz_area.area_id`` 是合法的业务映射，不在这个限制里 ——
    它是有明确业务含义的主键，不是"任何表都可能叫这个名"的技术列。）
    """
    from ai.knowledge.builder import _derive_mapping

    columns = {"tz_area": ["area_id", "area_name", "id", "create_time"]}
    for tech_field in ("id", "createTime"):
        assert _derive_mapping(tech_field, [("tz_area", "schema")], columns) is None


def test_derive_mapping_allows_business_pk(mini_graph):
    from ai.knowledge.builder import _derive_mapping

    columns = {"tz_area": ["area_id", "area_name"]}
    mapping = _derive_mapping("areaId", [("tz_area", "schema")], columns)
    assert mapping is not None
    assert mapping.column == "area_id"


def test_build_is_idempotent(mini_graph):
    from ai.knowledge.builder import build

    store, _stats = mini_graph
    before = store.stats()
    build(store=store, include_cases=False)
    after = store.stats()
    assert after["nodes"] == before["nodes"]
    assert after["edges"] == before["edges"]


# --------------------------------------------------------------------------
# 已验证证据的传播边界
# --------------------------------------------------------------------------


def test_verified_mapping_only_applies_to_its_own_endpoint():
    from ai.knowledge.field_map import FieldMapping

    mapping = FieldMapping(
        api_field="areaName",
        table="tz_area",
        column="area_name",
        confidence=1.0,
        evidence="验证于 PUT",
        source="verified",
        verified_at="endpoint:PUT:/admin/area",
    )
    assert mapping.applies_to("endpoint:PUT:/admin/area") is True
    # 新增接口没跑过，不能借 PUT 的证据
    assert mapping.applies_to("endpoint:POST:/admin/area") is False


def test_rule_mapping_has_no_endpoint_restriction():
    from ai.knowledge.field_map import FieldMapping

    mapping = FieldMapping(
        api_field="areaName",
        table="tz_area",
        column="area_name",
        confidence=0.9,
        evidence="命名规则",
        source="rule",
    )
    assert mapping.applies_to("endpoint:PUT:/admin/area") is True


# --------------------------------------------------------------------------
# 召回
# --------------------------------------------------------------------------


@pytest.fixture()
def recall_graph():
    from ai.knowledge.store import KnowledgeStore

    store = KnowledgeStore(":memory:")
    store.bulk_add(
        [
            {"id": "endpoint:PUT:/admin/area", "kind": "endpoint", "name": "PUT /admin/area"},
            {"id": "param:endpoint:PUT:/admin/area.areaName", "kind": "param", "name": "areaName"},
            {"id": "table:tz_area", "kind": "table", "name": "tz_area"},
            {"id": "column:tz_area.area_name", "kind": "column", "name": "area_name"},
        ],
        [
            {"src": "endpoint:PUT:/admin/area", "dst": "param:endpoint:PUT:/admin/area.areaName",
             "kind": "has_param"},
            {"src": "param:endpoint:PUT:/admin/area.areaName", "dst": "column:tz_area.area_name",
             "kind": "maps_to", "confidence": 1.0, "evidence": "验证过"},
            {"src": "table:tz_area", "dst": "column:tz_area.area_name", "kind": "has_column"},
        ],
    )
    return store


def test_recall_exact_name_wins(recall_graph):
    from ai.knowledge.retriever import Retriever

    hits = Retriever(recall_graph).recall("areaName")
    assert hits[0].id == "param:endpoint:PUT:/admin/area.areaName"
    assert hits[0].via == "exact"


def test_recall_expands_through_graph(recall_graph):
    """搜列名要能带出映射到它的参数 —— 这才是"关系"查询的意义。"""
    from ai.knowledge.retriever import Retriever

    hits = Retriever(recall_graph).recall("area_name")
    ids = {h.id for h in hits}
    assert "param:endpoint:PUT:/admin/area.areaName" in ids
    assert any(h.via == "graph" for h in hits)


def test_recall_respects_kind_filter(recall_graph):
    from ai.knowledge.retriever import Retriever

    hits = Retriever(recall_graph).recall("area_name", kinds=("column",))
    assert all(h.node["kind"] == "column" for h in hits)


def test_recall_empty_query(recall_graph):
    from ai.knowledge.retriever import Retriever

    assert Retriever(recall_graph).recall("") == []


def test_find_field_mapping_reports_per_endpoint_evidence(recall_graph):
    """同一列在两个接口上证据强度不同时，必须分开报，不能都标成 1.0。"""
    from ai.knowledge.retriever import find_field_mapping

    recall_graph.bulk_add(
        [{"id": "param:endpoint:POST:/admin/area.areaName", "kind": "param", "name": "areaName"}],
        [{"src": "param:endpoint:POST:/admin/area.areaName", "dst": "column:tz_area.area_name",
          "kind": "maps_to", "confidence": 0.9, "evidence": "命名规则"}],
    )
    result = find_field_mapping("areaName", store=recall_graph)

    by_conf = {m["confidence"]: m["endpoint"] for m in result["mappings"]}
    assert by_conf[1.0] == "PUT /admin/area"
    assert by_conf[0.9] == "POST /admin/area"


def test_find_field_mapping_note_uses_own_endpoint_evidence(recall_graph):
    """问 POST 时按 POST 的证据说话（0.9），不能借 PUT 的 1.0 说"已验证"。"""
    from ai.knowledge.retriever import find_field_mapping

    recall_graph.bulk_add(
        [{"id": "param:endpoint:POST:/admin/area.areaName", "kind": "param", "name": "areaName"}],
        [{"src": "param:endpoint:POST:/admin/area.areaName", "dst": "column:tz_area.area_name",
          "kind": "maps_to", "confidence": 0.9, "evidence": "命名规则"}],
    )
    result = find_field_mapping("areaName", endpoint="POST /admin/area", store=recall_graph)
    assert "已验证" not in result["note"]
    assert "命名规则" in result["note"]


def test_find_field_mapping_note_warns_when_endpoint_has_no_evidence(recall_graph):
    """问一个完全没有映射的接口时，要提醒"别处有、但不是这个接口的证据"。"""
    from ai.knowledge.retriever import find_field_mapping

    result = find_field_mapping("areaName", endpoint="DELETE /admin/area", store=recall_graph)
    assert "没有映射记录" in result["note"]
    assert "别的接口的证据" in result["note"]


def test_find_field_mapping_unknown_field(recall_graph):
    from ai.knowledge.retriever import find_field_mapping

    result = find_field_mapping("nopeField", store=recall_graph)
    assert result["found"] is False
    assert result["mappings"] == []


# --------------------------------------------------------------------------
# 真实数据建图（依赖 .cache 快照，跑不到就跳过）
# --------------------------------------------------------------------------


@pytest.mark.skipif(
    not (PROJECT_ROOT / ".cache" / "apidoc" / "openapi.json").exists(),
    reason="没有 OpenAPI 快照",
)
def test_real_graph_builds_without_collisions():
    """真实数据建图：用例节点数必须等于实际用例数（同名用例不能合并）。"""
    from ai.knowledge.builder import build
    from ai.knowledge.store import KnowledgeStore

    store = KnowledgeStore(":memory:")
    stats = build(store=store)

    counts = store.stats()["by_kind"]
    assert counts.get("case", 0) == stats.cases, "有同名用例被合并了"
    assert counts.get("endpoint", 0) > 0
    assert counts.get("column", 0) > 0

    # areaName 的映射确实学到了
    pid = "param:endpoint:PUT:/admin/area.areaName"
    mapped = [n for n in store.neighbors(pid, kind="maps_to")]
    assert any(n["id"] == "column:tz_area.area_name" for n in mapped)
