"""接口文档导入解析的测试。

覆盖三种格式：OpenAPI 3、Swagger 2、HAR、cURL，
以及「解析 → 生成用例 → 框架能读回来」这条完整链路。

这些是**单元测试**，不联网、不调接口，所以跑得快。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.importer.case_builder import build_step, build_testcase, group_by_module  # noqa: E402
from tools.importer.dispatcher import detect_format, parse_document  # noqa: E402
from tools.importer.models import infer_module  # noqa: E402

DEMO_DIR = PROJECT_ROOT / "data" / "importer_demo"


# --------------------------------------------------------------------------
# 格式识别
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("api.json", "openapi"),
        ("api.yaml", "openapi"),
        ("api.yml", "openapi"),
        ("capture.har", "har"),
        ("commands.txt", "curl"),
        ("commands.sh", "curl"),
    ],
)
def test_detect_format_by_extension(filename, expected):
    assert detect_format(filename) == expected


def test_detect_format_by_content():
    """后缀不认识时看内容。"""
    assert detect_format("unknown.txt", '{"log": {"entries": []}}') == "har"
    assert detect_format("unknown.txt", "curl 'http://x/y'") == "curl"
    assert detect_format("unknown.txt", '{"openapi": "3.0.0"}') == "openapi"


# --------------------------------------------------------------------------
# OpenAPI 3
# --------------------------------------------------------------------------


@pytest.fixture
def openapi_endpoints():
    content = (DEMO_DIR / "openapi_demo.json").read_text(encoding="utf-8")
    return parse_document("openapi_demo.json", content)


def test_openapi_parses_all_endpoints(openapi_endpoints):
    assert len(openapi_endpoints) == 3


def test_openapi_extracts_query_params(openapi_endpoints):
    ep = next(e for e in openapi_endpoints if e.path.endswith("/statistics/order"))
    names = {p.name for p in ep.query}
    assert names == {"searchType", "pageSize"}
    # 枚举取第一个当示例值
    assert ep.query_map()["searchType"] == "LAST_SEVEN"


def test_openapi_extracts_request_body(openapi_endpoints):
    ep = next(e for e in openapi_endpoints if e.path.endswith("/order/list"))
    assert ep.method == "POST"
    assert ep.body == {"orderNo": "SO202401010001", "status": 1, "pageSize": 20}
    # 请求体字段不该混进 query 参数
    assert ep.query == []
    assert {p.name for p in ep.body_params} == {"orderNo", "status", "pageSize"}


def test_openapi_extracts_response_example(openapi_endpoints):
    ep = next(e for e in openapi_endpoints if e.path.endswith("/statistics/order"))
    assert ep.response_example["code"] == 200
    assert ep.response_example["success"] is True


def test_openapi_base_url(openapi_endpoints):
    assert all(e.base_url == "http://103.236.97.137:8190/_admin" for e in openapi_endpoints)


def test_openapi_path_param_extracted(openapi_endpoints):
    ep = next(e for e in openapi_endpoints if "userId" in e.path)
    assert "userId" in {p.name for p in ep.path_params}


# --------------------------------------------------------------------------
# Swagger 2.0
# --------------------------------------------------------------------------


@pytest.fixture
def swagger2_endpoints():
    content = (DEMO_DIR / "swagger2_demo.json").read_text(encoding="utf-8")
    return parse_document("swagger2_demo.json", content)


def test_swagger2_parses(swagger2_endpoints):
    assert len(swagger2_endpoints) == 2


def test_swagger2_applies_base_path(swagger2_endpoints):
    """Swagger 2.0 的 basePath 要拼到路径前面。"""
    assert all(e.path.startswith("/admin/") for e in swagger2_endpoints)


def test_swagger2_resolves_ref_in_body(swagger2_endpoints):
    """body 参数指向 definitions 里的 $ref，要能解开。"""
    ep = next(e for e in swagger2_endpoints if e.method == "POST")
    assert ep.body["goodsId"] == 1001
    assert "goodsId" in {p.name for p in ep.body_params}


def test_swagger2_resolves_ref_in_response(swagger2_endpoints):
    ep = next(e for e in swagger2_endpoints if e.method == "GET")
    assert ep.response_example["code"] == 200


# --------------------------------------------------------------------------
# HAR
# --------------------------------------------------------------------------


@pytest.fixture
def har_endpoints():
    content = (DEMO_DIR / "har_demo.har").read_text(encoding="utf-8")
    return parse_document("har_demo.har", content)


def test_har_parses_entries(har_endpoints):
    assert len(har_endpoints) == 2


def test_har_extracts_url_parts(har_endpoints):
    ep = har_endpoints[0]
    assert ep.method == "GET"
    assert ep.path == "/admin/user/info"
    assert ep.base_url == "http://103.236.97.137:8190/_admin"


def test_har_extracts_query_and_body(har_endpoints):
    ep = next(e for e in har_endpoints if e.method == "POST")
    assert ep.query_map() == {"from": "app"}
    assert ep.body == {"goodsId": 1001, "quantity": 2, "addressId": 88}


def test_har_extracts_headers(har_endpoints):
    ep = har_endpoints[0]
    assert "accesstoken" in ep.header_map()


def test_har_extracts_response(har_endpoints):
    ep = har_endpoints[0]
    assert ep.response_example["result"]["username"] == "admin"


# --------------------------------------------------------------------------
# cURL
# --------------------------------------------------------------------------


@pytest.fixture
def curl_endpoints():
    content = (DEMO_DIR / "curl_demo.txt").read_text(encoding="utf-8")
    return parse_document("curl_demo.txt", content)


def test_curl_parses_multiple_commands(curl_endpoints):
    assert len(curl_endpoints) == 3


def test_curl_extracts_method_and_path(curl_endpoints):
    ep = next(e for e in curl_endpoints if "goods" in e.path)
    assert ep.method == "POST"
    assert ep.path == "/prod/prod/detail"


def test_curl_extracts_json_body(curl_endpoints):
    ep = next(e for e in curl_endpoints if "goods" in e.path)
    assert ep.body == {"goodsId": 1001, "name": "测试商品"}


def test_curl_extracts_headers(curl_endpoints):
    ep = curl_endpoints[0]
    assert "accesstoken" in ep.header_map()
    assert ep.header_map()["uuid"] == "c4f67488-d43a-4c6a-b8e6-86a40ea8a540"


def test_curl_extracts_query_from_url(curl_endpoints):
    ep = curl_endpoints[0]
    assert ep.query_map() == {"searchType": "LAST_SEVEN"}


def test_curl_handles_single_line():
    """一行写完的 curl 也要能解析。"""
    endpoints = parse_document(
        "x.txt",
        """curl 'https://api.x.com/a/b' -H 'k: v' --data-raw '{"n":1}'""",
    )
    assert len(endpoints) == 1
    assert endpoints[0].method == "POST"
    assert endpoints[0].path == "/a/b"
    assert endpoints[0].body == {"n": 1}


# --------------------------------------------------------------------------
# 模块归类
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/admin/statistics/order", "statistics"),
        ("/order/list", "order"),
        ("/api/v1/goods/detail", "goods"),
        ("/admin/user/info", "passport"),
    ],
)
def test_infer_module(path, expected):
    assert infer_module(path) == expected


# --------------------------------------------------------------------------
# 生成用例
# --------------------------------------------------------------------------


def test_build_step_separates_query_and_body(openapi_endpoints):
    ep = next(e for e in openapi_endpoints if e.method == "POST")
    step = build_step(ep)
    assert step["method"] == "POST"
    assert step["path"] == "/order/list"
    assert "params" not in step          # body 字段不该进 params
    assert step["data"]["orderNo"] == "SO202401010001"


def test_build_step_auth_headers_win(openapi_endpoints):
    """鉴权头优先级最高，不能被文档里的头覆盖。"""
    ep = next(e for e in openapi_endpoints if e.method == "POST")
    step = build_step(ep, auth_headers={"accesstoken": "{{mall4j.token}}"})
    assert step["headers"]["accesstoken"] == "{{mall4j.token}}"


def test_build_step_skips_fake_header_values():
    """文档里没给真实值的头不写进用例（免得把鉴权头写成 "string"）。"""
    from tools.importer.models import Endpoint, Param

    ep = Endpoint(
        id="x", method="GET", path="/a",
        headers=[Param(name="accesstoken", location="header", example="string", fake=True)],
    )
    step = build_step(ep, auth_headers={"accesstoken": "{{mall4j.token}}"})
    assert step["headers"]["accesstoken"] == "{{mall4j.token}}"


def test_build_asserts_from_response(openapi_endpoints):
    ep = next(e for e in openapi_endpoints if e.path.endswith("/statistics/order"))
    step = build_step(ep)
    fields = {a["field"] for a in step["assert"]}
    assert "code" in fields
    assert "success" in fields


def test_group_by_module(openapi_endpoints):
    groups = group_by_module(openapi_endpoints)
    assert set(groups) == {"statistics", "order", "user"}


def test_build_testcase_shape(openapi_endpoints):
    ep = openapi_endpoints[0]
    case = build_testcase(ep, project="demo", module="statistics", epic="E")["testcase"]
    assert case["name"] == ep.id
    assert case["allure"]["epic"] == "E"
    assert case["allure"]["feature"] == "statistics"
    assert len(case["steps"]) == 1
    assert case["steps"][0]["id"] == ep.id


# --------------------------------------------------------------------------
# 端到端：解析 → 写 YAML → 框架能读回来
# --------------------------------------------------------------------------


def test_generated_yaml_passes_framework_validation(tmp_path, openapi_endpoints):
    """生成的用例必须能被框架的校验器通过（否则跑不起来）。"""
    from testcases.case_loader import load_cases_from_file
    from tools.importer.case_builder import write_cases

    write_cases(openapi_endpoints, tmp_path, project="demo")

    files = sorted(tmp_path.rglob("*.yaml"))
    assert files

    for file in files:
        if file.name == "module.yaml":
            continue
        cases = load_cases_from_file(file)
        assert cases
        for case in cases:
            assert case["testcase"]["steps"]


def test_generated_excel_roundtrip(tmp_path, openapi_endpoints):
    """导出的 Excel 必须能被框架的 Excel 读取器读回来。"""
    from utils.excel.loader import load_excel_cases
    from tools.importer.case_builder import export_excel

    output = tmp_path / "cases.xlsx"
    count = export_excel(openapi_endpoints, output, project="demo")
    assert count == 3

    cases, module = load_excel_cases(output)
    assert len(cases) == 3
    assert module.get("name")


def test_excel_export_keeps_body_and_params(tmp_path, openapi_endpoints):
    """Excel 往返后请求体和 Query 参数不能串味。"""
    from utils.excel.loader import load_excel_cases
    from tools.importer.case_builder import export_excel

    output = tmp_path / "c.xlsx"
    export_excel(openapi_endpoints, output, project="demo")
    cases, _ = load_excel_cases(output)

    by_name = {c["testcase"]["name"]: c["testcase"] for c in cases}

    order_case = by_name["post_order_list"]
    step = order_case["steps"][0]
    assert step["data"]["orderNo"] == "SO202401010001"
    assert not step.get("params")

    stat_case = by_name["get_statistics_order"]
    step = stat_case["steps"][0]
    assert step["params"]["searchType"] == "LAST_SEVEN"
    assert not step.get("data")


# --------------------------------------------------------------------------
# 异常输入
# --------------------------------------------------------------------------


def test_empty_content_raises():
    from tools.importer.dispatcher import DocumentParseError

    with pytest.raises(DocumentParseError):
        parse_document("x.json", "")


def test_broken_json_raises():
    from tools.importer.dispatcher import DocumentParseError

    with pytest.raises(DocumentParseError):
        parse_document("x.har", "{not json")


def test_unknown_format_raises():
    from tools.importer.dispatcher import DocumentParseError

    with pytest.raises(DocumentParseError):
        parse_document("x.json", "{}", fmt="postman")
