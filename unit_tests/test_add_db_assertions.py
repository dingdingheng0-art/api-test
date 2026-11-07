"""``tools/maint/add_db_assertions.py`` 的单元测试。

落库断言写错了比没有更糟 —— 会把"用例写错"报成"服务端没落库"，
排查方向直接跑偏。这里钉住三件事：

- 三种方法（POST / PUT / DELETE）生成的 SQL 和参数对得上
- 软删表（``tz_prod_tag``）按 ``delete_time`` 判断，不要求行真的消失
- ``timestamp()`` 命名会被换成 ``fixed()``，否则断言里查不到造出来的值
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_module():
    path = ROOT / "tools" / "maint" / "add_db_assertions.py"
    spec = importlib.util.spec_from_file_location("add_db_assertions", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def tool():
    return _load_module()


def test_post_asserts_count_and_exists(tool):
    module = tool.MODULES["sys_menu"]
    items = tool.build_post_asserts(module, "{{tools.fixed('m', '自动化菜单')}}")
    assert [item["comparator"] for item in items] == ["==", "exists"]
    assert items[0]["sql"] == "SELECT COUNT(*) FROM tz_sys_menu WHERE name = %s"
    assert items[0]["expect"] == 1
    assert items[0]["params"] == ["{{tools.fixed('m', '自动化菜单')}}"]
    assert items[1]["sql"] == "SELECT menu_id FROM tz_sys_menu WHERE name = %s"


def test_post_asserts_respect_soft_delete(tool):
    module = tool.MODULES["prod_prodTag"]
    items = tool.build_post_asserts(module, "{{tools.fixed('m', '自动化标签')}}")
    assert "delete_time IS NULL" in items[0]["sql"]
    assert "delete_time IS NULL" in items[1]["sql"]


def test_put_asserts_check_new_value(tool):
    module = tool.MODULES["shop_notice"]
    items = tool.build_put_asserts(
        module, "{{tools.fixed('m', '自动化公告改')}}", "{{tools.max_id(查数据.data.records, 'id')}}"
    )
    assert len(items) == 1
    assert items[0]["sql"] == "SELECT title FROM tz_notice WHERE id = %s"
    assert items[0]["expect"] == "{{tools.fixed('m', '自动化公告改')}}"


def test_delete_asserts_hard_and_soft(tool):
    hard = tool.build_delete_asserts(tool.MODULES["sys_menu"], "{{id}}")
    assert hard[0]["comparator"] == "not_exists"
    assert hard[0]["sql"] == "SELECT menu_id FROM tz_sys_menu WHERE menu_id = %s"

    soft = tool.build_delete_asserts(tool.MODULES["prod_prodTag"], "{{id}}")
    assert soft[0]["comparator"] == "=="
    assert soft[0]["expect"] == 0
    assert "delete_time IS NULL" in soft[0]["sql"]


def test_timestamp_names_become_fixed(tool):
    body = {"name": "自动化菜单{{tools.timestamp()}}", "parentId": 1}
    changed = tool.convert_timestamp_names(body, "sys_menu", is_update=False)
    assert changed
    assert body["name"] == "{{tools.fixed('mall4j_sys_menu_name', '自动化菜单')}}"
    assert body["parentId"] == 1

    update_body = {"name": "自动化菜单改{{tools.timestamp()}}"}
    tool.convert_timestamp_names(update_body, "sys_menu", is_update=True)
    assert update_body["name"] == "{{tools.fixed('mall4j_sys_menu_name_update', '自动化菜单改')}}"


def test_fixed_names_are_left_alone(tool):
    body = {"title": "{{tools.fixed('k', '自动化标签')}}"}
    assert tool.convert_timestamp_names(body, "prod_prodTag", False) == []
    assert body["title"] == "{{tools.fixed('k', '自动化标签')}}"


def test_compound_template_is_recognised(tool):
    """``{{tools.fixed(...)}}改`` 这种拼接写法也要能当名字表达式用。"""
    assert tool.find_expr("{{tools.fixed('k', '自动化运费')}}改") == (
        "{{tools.fixed('k', '自动化运费')}}改"
    )
    assert tool.find_expr("plain text") is None
    assert tool.find_expr(123) is None


def test_id_expressions(tool):
    body = {"title": "x", "transportId": "{{tools.find_id_by_fixed(a, b, c, d, e)}}"}
    assert tool.id_expr_from_body(body) == "{{tools.find_id_by_fixed(a, b, c, d, e)}}"
    assert tool.id_expr_from_path('/sys/menu/{{tools.max_id(查数据.data, "menuId")}}') == (
        '{{tools.max_id(查数据.data, "menuId")}}'
    )
    assert tool.id_expr_from_path("/sys/menu/1") is None


def test_generated_db_asserts_are_parseable_by_framework(tool):
    from utils.excel.flat_parser import _translate_assertion

    module = tool.MODULES["shop_notice"]
    info_db, info_body = tool.build_info_asserts(module, 1, missing=False)
    info_missing, _ = tool.build_info_asserts(module, 999999999, missing=True)
    db_items = (
        tool.build_post_asserts(module, "{{name}}")
        + tool.build_put_asserts(module, "{{name}}", "{{id}}")
        + tool.build_delete_asserts(module, "{{id}}")
        + info_db
        + info_missing
    )
    for item in db_items:
        translated = _translate_assertion(item)
        assert translated is None or translated["type"].startswith("db_query")

    # 详情正例的"主键等于路径 id"是普通响应断言，不该被当成查库断言
    for item in info_body:
        translated = _translate_assertion(item)
        assert translated is not None
        assert not translated["type"].startswith("db_query")
        assert translated["field"] == "data.id"


def test_info_id_from_path_only_accepts_literal_numeric_tail(tool):
    assert tool.info_id_from_path("/admin/area/info/1") == 1
    assert tool.info_id_from_path("/prod/prod/info/18") == 18
    # 查当前登录用户没有 id，不能生成"主键等于 X"的断言
    assert tool.info_id_from_path("/sys/user/info") is None
    # 路径里是变量表达式，运行时才知道 id，这里也跳过
    assert tool.info_id_from_path("/prod/prod/info/{{tools.find_id_by_fixed(a)}}") is None


def test_info_expr_from_path_picks_up_template_pk(tool):
    """详情 id 常常是前置步骤查出来的，只认字面量会整条跳过。"""
    assert tool.info_expr_from_path("/sys/user/info/{{查数据.data.records[0].userId}}") == (
        "{{查数据.data.records[0].userId}}"
    )
    assert tool.info_expr_from_path("/admin/area/info/1") is None
    assert tool.info_expr_from_path("/sys/user/info") is None


def test_camel_case_for_response_pk_field(tool):
    assert tool.camel_case("area_id") == "areaId"
    assert tool.camel_case("hot_search_id") == "hotSearchId"
    assert tool.camel_case("id") == "id"


def test_info_asserts_positive_also_pins_pk(tool):
    db_items, resp_items = tool.build_info_asserts(tool.MODULES["admin_area"], 1, False)
    assert db_items[0]["comparator"] == "exists"
    assert db_items[0]["sql"] == "SELECT area_id FROM tz_area WHERE area_id = %s"
    assert db_items[0]["params"] == [1]
    assert resp_items == [
        {
            "check": "body",
            "jsonpath": "$.data.areaId",
            "comparator": "==",
            "expect": 1,
        }
    ]


def test_info_asserts_missing_only_checks_absence(tool):
    db_items, resp_items = tool.build_info_asserts(tool.MODULES["admin_area"], 999999999, True)
    assert [item["comparator"] for item in db_items] == ["not_exists"]
    assert db_items[0]["params"] == [999999999]
    assert resp_items == [], "data 是 null 时不能再断言 data.xxx"


def test_info_asserts_echo_pk_can_be_turned_off(tool):
    """接口响应里没有主键字段时，不能硬塞"回显主键"断言。

    回归：``/sys/user/info/1`` 的 ``data.userId`` 恒为 null ——
    工具按"字面量 id 就断言回显"生成了一条必然失败的断言。
    """
    _, resp_items = tool.build_info_asserts(
        tool.MODULES["sys_user"], 1, missing=False, echo_pk=False
    )
    assert resp_items == []


def test_sys_user_disables_echo_pk(tool):
    """配置里关掉，工具就不用再靠调用方记得传 echo_pk。"""
    assert tool.MODULES["sys_user"].get("echo_pk") is False


def test_endpoint_override_points_current_user_at_sys_user_table(tool):
    """``GET /sys/user/info`` 查的是后台管理员，不是会员表 ``tz_user``。"""
    module = tool.module_for_endpoint("admin_user", "/sys/user/info")
    assert module["table"] == "tz_sys_user"
    assert module["current_user"] is True

    # 同模块其他接口仍然走模块级配置
    other = tool.module_for_endpoint("admin_user", "/admin/user/info/1")
    assert other["table"] == "tz_user"
    assert not other.get("current_user")


def test_current_user_asserts_use_login_credential(tool):
    module = tool.module_for_endpoint("admin_user", "/sys/user/info")
    db_items, resp_items = tool.build_current_user_asserts(module)

    assert db_items[0]["sql"] == "SELECT user_id FROM tz_sys_user WHERE username = %s"
    assert db_items[0]["comparator"] == "exists"
    credential = "{{tools.login_credential('mall4j', 'username')}}"
    assert db_items[0]["params"] == [credential]
    # 响应里返回的必须就是当前登录的那个人
    assert resp_items == [
        {
            "check": "body",
            "jsonpath": "$.data.username",
            "comparator": "==",
            "expect": credential,
        }
    ]


def test_current_user_asserts_are_parseable_by_framework(tool):
    from utils.excel.flat_parser import _translate_assertion

    module = tool.module_for_endpoint("admin_user", "/sys/user/info")
    db_items, resp_items = tool.build_current_user_asserts(module)
    for item in db_items:
        assert _translate_assertion(item)["type"].startswith("db_query")
    for item in resp_items:
        assert not _translate_assertion(item)["type"].startswith("db_query")


def test_is_error_case_reads_flat_excel_format(tool):
    """Excel 里存的是 jsonpath/comparator/expect，错误路径必须认出来。"""
    assert tool.is_error_case(
        [{"check": "body", "jsonpath": "$.code", "comparator": "not_equal", "expect": "00000"}]
    )
    assert tool.is_error_case(
        [{"check": "body", "jsonpath": "$.success", "comparator": "==", "expect": False}]
    )
    assert not tool.is_error_case(
        [
            {"check": "body", "jsonpath": "$.code", "comparator": "==", "expect": "00000"},
            {"check": "body", "jsonpath": "$.success", "comparator": "==", "expect": True},
        ]
    )
    # 老写法（规范化后的键）也要认
    assert tool.is_error_case([{"type": "equal", "field": "code", "expected": "A00005"}])


def test_pk_row_exists_queries_the_module_table(tool):
    class StubClient:
        def __init__(self, count):
            self.count = count
            self.sql = ""
            self.params = None

        def scalar(self, sql, params=None):
            self.sql, self.params = sql, params
            return self.count

    module = tool.MODULES["admin_area"]
    hit = StubClient(1)
    assert tool.pk_row_exists(hit, module, 110000000000) is True
    assert hit.sql == "SELECT COUNT(*) FROM tz_area WHERE area_id = %s"
    assert hit.params == [110000000000]

    miss = StubClient(0)
    assert tool.pk_row_exists(miss, module, 1) is False
    assert tool.pk_row_exists(StubClient(None), module, 1) is False


def test_module_tables_exist_in_schema(tool):
    """映射里的表名/列名必须和真实库对得上（有 schema 快照时才检查）。"""
    schema_file = ROOT / ".cache" / "schema_mall4j.json"
    if not schema_file.exists():
        pytest.skip("没有库结构快照 .cache/schema_mall4j.json")

    schema = json.loads(schema_file.read_text(encoding="utf-8"))
    tables = {table["name"]: table for table in schema["tables"]}
    for name, module in tool.MODULES.items():
        table = tables.get(module["table"])
        assert table is not None, f"{name}: 表 {module['table']} 不存在"
        columns = {column["name"] for column in table["columns"]}
        for key in ("pk", "name_col"):
            assert module[key] in columns, (
                f"{name}: {module['table']} 里没有列 {module[key]}"
            )
        if module.get("soft_delete"):
            assert module["soft_delete"] in columns
