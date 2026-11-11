"""用例改写底座（``ai/agent/tools/case_io.py``）的单元测试。

**测的是"翻译对不对"和"写坏了会不会回滚"**

这两件事都是**一旦出错就很难发现**的：

- 翻译错了 → 单元格里是能读的 JSON，但框架解析出来是另一套断言，
  要到下次跑用例才炸，而且报错位置离原因很远
- 写入没回滚 → 文件被改坏了但工具说成功，下次跑直接收集失败

所以这里用**临时 Excel 文件**做真实的读-改-写，不 mock openpyxl。

**为什么反复测往返（round-trip）**

``assertions_to_flat`` 和 ``flat_parser._parse_asserts`` 是一对反向函数。
只测一边没意义 —— 真正要保证的是「写进去再读出来，和原来一模一样」。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai.agent.tools import case_io  # noqa: E402
from ai.agent.tools.registry import ToolError  # noqa: E402
from utils.excel.flat_parser import _parse_asserts  # noqa: E402


# --------------------------------------------------------------------------
# 内部格式 → 扁平格式
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    """把 ``.cache`` 指到临时目录。

    ``.cache/backup/agent_*`` 是**真实改动的审计记录**。测试往里写备份，
    等于往审计日志里塞假条目 —— 事后想查"这条用例是谁改的"会被测试数据淹没。
    """
    from utils import paths

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr(paths, "CACHE_DIR", cache_dir)
    return cache_dir


@pytest.mark.parametrize(
    "assertion, expected",
    [
        (
            {"type": "equal", "field": "code", "expected": "00000"},
            {"check": "body", "jsonpath": "$.code", "comparator": "==", "expect": "00000"},
        ),
        (
            {"type": "not_equal", "field": "code", "expected": 200},
            {"check": "body", "jsonpath": "$.code", "comparator": "!=", "expect": 200},
        ),
        (
            {"type": "gte", "field": "data.total", "expected": 0},
            {"check": "body", "jsonpath": "$.data.total", "comparator": ">=", "expect": 0},
        ),
        (
            {"type": "exists", "field": "data.records"},
            {"check": "body", "jsonpath": "$.data.records", "comparator": "exists"},
        ),
        (
            {"type": "is_none", "field": "data"},
            {"check": "body", "jsonpath": "$.data", "comparator": "is_none"},
        ),
        (
            {"type": "length_gte", "field": "data.records", "expected": 1},
            {"check": "body", "jsonpath": "$.data.records", "comparator": "len>=", "expect": 1},
        ),
        (
            {"type": "status_code", "expected": 200},
            {"check": "status_code", "comparator": "==", "expect": 200},
        ),
    ],
)
def test_assertion_to_flat(assertion, expected):
    assert case_io.assertion_to_flat(assertion) == expected


def test_is_true_becomes_equal_true():
    """扁平格式没有 is_true，用 ``== true`` 表示，语义完全一样。"""
    flat = case_io.assertion_to_flat({"type": "is_true", "field": "success"})
    assert flat == {"check": "body", "jsonpath": "$.success", "comparator": "==", "expect": True}


def test_db_assertion_keeps_sql_alias_and_params():
    flat = case_io.assertion_to_flat(
        {
            "type": "db_query",
            "query": "SELECT area_name FROM tz_area WHERE area_id = %s",
            "alias": "mall4j",
            "params": [1],
            "expected": "x",
        }
    )
    assert flat["check"] == "db"
    assert flat["sql"].startswith("SELECT area_name")
    assert flat["alias"] == "mall4j"
    assert flat["params"] == [1]
    assert flat["expect"] == "x"


def test_db_assertion_key_order_matches_existing_files():
    """键顺序和现有用例一致 —— 同样的断言再写一遍是**逐字节相同**的。

    顺序不一样不会改变含义，但会让幂等写入在 diff 里制造一堆假改动，
    人审的时候根本看不出真正改了什么。
    """
    flat = case_io.assertion_to_flat(
        {
            "type": "db_query",
            "query": "SELECT 1",
            "alias": "mall4j",
            "expected": "x",
            "params": [1],
        }
    )
    assert list(flat) == ["check", "sql", "comparator", "alias", "expect", "params"]


def test_elapsed_time_converts_ms_to_seconds():
    flat = case_io.assertion_to_flat({"type": "elapsed_time", "expected": 3000})
    assert flat == {"check": "time", "comparator": "==", "expect": 3.0}


def test_response_header_uses_key():
    flat = case_io.assertion_to_flat(
        {"type": "response_header", "field": "Content-Type", "expected": "application/json"}
    )
    assert flat["check"] == "header"
    assert flat["key"] == "Content-Type"


def test_unknown_assertion_type_raises_instead_of_dropping():
    """翻不出来必须报错 —— 静默丢掉一条断言等于悄悄削弱了用例。"""
    with pytest.raises(ToolError) as exc:
        case_io.assertion_to_flat({"type": "subset", "field": "data", "expected": {}})
    assert "subset" in str(exc.value)
    assert "YAML" in exc.value.hint


def test_missing_field_raises_instead_of_becoming_full_body():
    """``equal`` 少了 field 会被解析成「断言整个响应体」，语义完全变了。

    这种改法**能过校验**（full_body 本来就不要 field），所以必须在
    翻译这一层就拦住。
    """
    with pytest.raises(ToolError) as exc:
        case_io.assertion_to_flat({"type": "equal", "expected": "x"})
    assert "field" in str(exc.value)


def test_missing_expected_raises_for_comparison_types():
    with pytest.raises(ToolError) as exc:
        case_io.assertion_to_flat({"type": "equal", "field": "code"})
    assert "expected" in str(exc.value)


def test_assertions_to_flat_rejects_non_list():
    with pytest.raises(ToolError):
        case_io.assertions_to_flat({"type": "equal"})


# --------------------------------------------------------------------------
# 往返：写进去再读出来必须一样
# --------------------------------------------------------------------------


#: 注意：``exists`` / ``is_none`` 这些解析回来会带 ``expected: None``
#: （``flat_parser`` 统一给每个断言塞一个 expected）。这里按**解析后的形态**
#: 写，往返才有意义 —— 要验的是「写进去再读出来不变」，不是「原样没动过」。
ROUNDTRIP_CASES = [
    [{"type": "status_code", "expected": 200}],
    [{"type": "equal", "field": "success", "expected": True}],
    [{"type": "equal", "field": "code", "expected": "00000"}],
    [{"type": "exists", "field": "data", "expected": None}],
    [{"type": "not_exists", "field": "data.list", "expected": None}],
    [{"type": "is_none", "field": "data", "expected": None}],
    [{"type": "is_not_none", "field": "data.areaName", "expected": None}],
    [{"type": "gte", "field": "data.total", "expected": 0}],
    [{"type": "gt", "field": "data.total", "expected": 0}],
    [{"type": "length_gte", "field": "data.records", "expected": 1}],
    [{"type": "length", "field": "data.records", "expected": 3}],
    [{"type": "contains", "field": "msg", "expected": "成功"}],
    [
        {
            "type": "db_query",
            "query": "SELECT area_name FROM tz_area WHERE area_id = %s",
            "alias": "mall4j",
            "params": ["{{x}}"],
            "expected": "y",
        }
    ],
    [
        {
            "type": "db_query_not_exists",
            "query": "SELECT 1 FROM tz_area WHERE area_id = %s",
            "alias": "mall4j",
            "params": [1],
        }
    ],
    [
        {
            "type": "db_query_exists",
            "query": "SELECT 1 FROM tz_area",
            "alias": "mall4j",
        }
    ],
]


@pytest.mark.parametrize("assertions", ROUNDTRIP_CASES)
def test_roundtrip_through_flat_format(assertions):
    """内部格式 → 扁平 → 内部，必须完全一致。"""
    flat = case_io.assertions_to_flat(assertions)
    assert _parse_asserts(flat) == assertions


def test_roundtrip_preserves_order_and_count():
    assertions = ROUNDTRIP_CASES[0] + ROUNDTRIP_CASES[1] + ROUNDTRIP_CASES[3]
    flat = case_io.assertions_to_flat(assertions)
    assert len(flat) == 3
    assert _parse_asserts(flat) == assertions


# --------------------------------------------------------------------------
# 临时 Excel 文件：真实的读-改-写
# --------------------------------------------------------------------------


@pytest.fixture()
def excel_file(tmp_path, monkeypatch):
    """造一个最小可用的扁平格式 Excel，并把用例根指向它。"""
    import openpyxl

    from utils.cases import case_source

    module_dir = tmp_path / "cases_excel" / "proj" / "mod"
    module_dir.mkdir(parents=True)
    file_path = module_dir / "mod.xlsx"

    wb = openpyxl.Workbook()
    wb.active.title = case_io.SHEET_FLAT
    sheet = wb[case_io.SHEET_FLAT]
    for idx, name in enumerate(case_io.FLAT_HEADERS, start=1):
        sheet.cell(1, idx).value = name

    headers = {name: idx for idx, name in enumerate(case_io.FLAT_HEADERS, start=1)}
    rows = [
        {
            "case_id": "API-001",
            "模块": "mod",
            "标签": "mod",
            "用例名称": "查数据 - 正常请求",
            "优先级": "P1",
            "接口名称": "查数据",
            "用例标题": "（正常请求）",
            "接口": "/admin/x",
            "请求方法": "GET",
            "请求头": "{}",
            "请求数据": "{}",
            "响应数据(预期参考)": '{"code": "00000"}',
            "断言": json.dumps(
                [
                    {"check": "status_code", "comparator": "==", "expect": 200},
                    {"check": "body", "jsonpath": "$.success", "comparator": "==", "expect": True},
                ],
                ensure_ascii=False,
            ),
            "用例状态": "debugged",
        },
        {
            "case_id": "API-002",
            "模块": "mod",
            "标签": "mod",
            "用例名称": "查数据 - 查不到",
            "优先级": "P1",
            "接口名称": "查数据",
            "用例标题": "（查不到数据）",
            "接口": "/admin/x",
            "请求方法": "GET",
            "请求头": "{}",
            "请求数据": "{}",
            "响应数据(预期参考)": '{"code": "00000"}',
            "断言": json.dumps(
                [{"check": "body", "jsonpath": "$.data", "comparator": "is_none"}],
                ensure_ascii=False,
            ),
            "用例状态": "debugged",
        },
    ]

    for row_idx, values in enumerate(rows, start=2):
        for name, value in values.items():
            sheet.cell(row_idx, headers[name]).value = value

    wb.save(file_path)

    monkeypatch.setattr(case_source, "get_source_roots", lambda *a, **k: [tmp_path / "cases_excel"])
    monkeypatch.setattr(
        case_source, "current_excel_dir", lambda *a, **k: tmp_path / "cases_excel"
    )
    from ai.agent.tools._common import clear_cache

    clear_cache()
    yield file_path
    clear_cache()


def _cell(file_path: Path, row: int, column: str) -> str:
    import openpyxl

    sheet = openpyxl.load_workbook(file_path)[case_io.SHEET_FLAT]
    headers = {c.value: idx for idx, c in enumerate(sheet[1], start=1) if c.value}
    return sheet.cell(row, headers[column]).value


def test_read_assertions_gives_both_formats(excel_file):
    result = case_io.read_assertions("API-001")
    assert result["format"] == "excel"
    step = result["steps"][0]
    assert step["assertions"][0] == {"type": "status_code", "expected": 200}
    assert step["flat"][0]["check"] == "status_code"


def test_plan_does_not_touch_the_file(excel_file):
    before = excel_file.read_bytes()
    plan = case_io.plan_assertion_change(
        "API-001", [{"type": "status_code", "expected": 200}]
    )
    assert excel_file.read_bytes() == before
    assert plan["diff"]["removed"][0]["check"] == "body"


def test_apply_writes_only_the_assert_cell(excel_file):
    """改断言不能把「响应数据(预期参考)」这类列清掉。"""
    case_io.apply_assertion_change(
        "API-001",
        [
            {"type": "status_code", "expected": 200},
            {"type": "equal", "field": "success", "expected": True},
            {"type": "equal", "field": "code", "expected": "00000"},
            {"type": "exists", "field": "data"},
            {"type": "exists", "field": "data.records"},
        ],
        note="测试",
    )
    assert _cell(excel_file, 2, "响应数据(预期参考)") == '{"code": "00000"}'
    assert _cell(excel_file, 2, "用例名称") == "查数据 - 正常请求"
    assert json.loads(_cell(excel_file, 2, "断言")) == [
        {"check": "status_code", "comparator": "==", "expect": 200},
        {"check": "body", "jsonpath": "$.success", "comparator": "==", "expect": True},
        {"check": "body", "jsonpath": "$.code", "comparator": "==", "expect": "00000"},
        {"check": "body", "jsonpath": "$.data", "comparator": "exists"},
        {"check": "body", "jsonpath": "$.data.records", "comparator": "exists"},
    ]


def test_apply_is_idempotent_at_cell_level(excel_file):
    """同样的断言写两遍，单元格内容**逐字节相同**。"""
    assertions = [
        {"type": "status_code", "expected": 200},
        {"type": "equal", "field": "success", "expected": True},
        {"type": "exists", "field": "data"},
        {"type": "exists", "field": "data.records"},
    ]
    case_io.apply_assertion_change("API-001", assertions)
    first = _cell(excel_file, 2, "断言")
    case_io.apply_assertion_change("API-001", assertions)
    assert _cell(excel_file, 2, "断言") == first


def test_apply_creates_a_backup_with_manifest(excel_file, isolated_cache):
    case_io.apply_assertion_change(
        "API-001",
        [
            {"type": "status_code", "expected": 200},
            {"type": "equal", "field": "success", "expected": True},
            {"type": "equal", "field": "code", "expected": "00000"},
            {"type": "exists", "field": "data"},
            {"type": "exists", "field": "data.records"},
        ],
        note="补断言",
    )
    dirs = sorted((isolated_cache / "backup").glob("agent_*"))
    assert dirs, "没有生成备份目录"
    latest = dirs[-1]
    assert (latest / excel_file.name).exists()
    manifest = json.loads((latest / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["case_id"] == "API-001"
    assert manifest["note"] == "补断言"


def test_apply_rejects_empty_assertions(excel_file):
    with pytest.raises(ToolError) as exc:
        case_io.apply_assertion_change("API-001", [])
    assert "不能为空" in str(exc.value)


def test_apply_rejects_unknown_case_id(excel_file):
    with pytest.raises(ToolError) as exc:
        case_io.apply_assertion_change("API-999", [{"type": "status_code", "expected": 200}])
    assert "API-999" in str(exc.value)


def test_excel_cases_are_not_located_by_name(excel_file):
    """Excel 里按名字定位会改错行（同名用例很常见），所以只认编号。"""
    with pytest.raises(ToolError):
        case_io.apply_assertion_change("查数据", [{"type": "status_code", "expected": 200}])


# --------------------------------------------------------------------------
# 「断言只能变强」的闸门
# --------------------------------------------------------------------------
#
# 这条规则原本只写在 skill 里。模型不照着写的话，把 ``code == "00000"``
# 换成 ``code exists`` 就能让任何失败变绿 —— 而且工具会报"已落盘"。


def test_apply_rejects_deleting_assertions(excel_file):
    """删断言 = 削弱，默认拒绝，而且文件不能被动过。"""
    before = excel_file.read_bytes()
    with pytest.raises(ToolError) as exc:
        case_io.apply_assertion_change(
            "API-001", [{"type": "status_code", "expected": 200}]
        )
    assert "削弱断言" in str(exc.value)
    assert excel_file.read_bytes() == before, "被拒绝了却还是写了文件"


def test_apply_rejects_downgrading_comparator(excel_file):
    """``code == "00000"`` 改成 ``code exists`` —— 同一个字段，验得松了。"""
    # 先把 API-001 改成有三条断言的形态
    case_io.apply_assertion_change(
        "API-001",
        [
            {"type": "status_code", "expected": 200},
            {"type": "equal", "field": "success", "expected": True},
            {"type": "equal", "field": "code", "expected": "00000"},
            {"type": "exists", "field": "data"},
            {"type": "exists", "field": "data.records"},
        ],
    )
    with pytest.raises(ToolError) as exc:
        case_io.apply_assertion_change(
            "API-001",
            [
                {"type": "status_code", "expected": 200},
                {"type": "equal", "field": "success", "expected": True},
                {"type": "exists", "field": "code"},
            ],
        )
    assert "削弱断言" in str(exc.value)


def test_apply_allows_weakening_when_explicitly_asked(excel_file, isolated_cache):
    """接口契约真的变了，要放宽时给显式开关 —— 而且 manifest 里必须留痕。"""
    case_io.apply_assertion_change(
        "API-001",
        [
            {"type": "status_code", "expected": 200},
            {"type": "equal", "field": "success", "expected": True},
            {"type": "exists", "field": "data"},
            {"type": "exists", "field": "data.records"},
        ],
    )
    result = case_io.apply_assertion_change(
        "API-001",
        [{"type": "status_code", "expected": 200}],
        note="接口去掉了 success 字段",
        allow_weakening=True,
    )
    assert result["written"] is True
    assert result["weakenings"]

    latest = sorted((isolated_cache / "backup").glob("agent_*"))[-1]
    manifest = json.loads((latest / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["allow_weakening"] is True
    assert manifest["weakenings"]


def test_strengthening_is_not_blocked(excel_file):
    """加断言、把 exists 换成 == 都是变强，不能误伤。"""
    result = case_io.apply_assertion_change(
        "API-001",
        [
            {"type": "status_code", "expected": 200},
            {"type": "equal", "field": "success", "expected": True},
            {"type": "equal", "field": "code", "expected": "00000"},
            {"type": "exists", "field": "data"},
            {"type": "exists", "field": "data.records"},
        ],
    )
    assert result["written"] is True
    assert result["weakenings"] == []


def test_db_assertion_param_change_counts_as_a_different_target(excel_file):
    """落库断言的 ``params`` 算目标的一部分。

    换成"换一条已经满足期望值的记录来查"就能让失败的落库断言变绿 ——
    那正是这条闸要拦的。所以改参数会被当成「旧断言没了」，需要人放行。
    无改动重写不受影响（扁平文本逐字节相同，目标也相同）。
    """
    from ai.agent.tools import case_io as cio

    before = [
        {"check": "db", "sql": "SELECT x FROM t WHERE id = %s", "params": [1], "comparator": "==", "expect": 1}
    ]
    after = [
        {"check": "db", "sql": "SELECT x FROM t WHERE id = %s", "params": [2], "comparator": "==", "expect": 1}
    ]
    weakenings = cio.find_weakenings(before, after)
    assert len(weakenings) == 1
    assert weakenings[0]["why"] == "这条断言被删掉了"


def test_same_target_with_looser_comparator_is_flagged():
    """同一个字段/同一条 SQL，比较符变松 —— 这是最典型的"改绿"。"""
    from ai.agent.tools import case_io as cio

    before = [{"check": "body", "jsonpath": "$.code", "comparator": "==", "expect": "00000"}]
    after = [{"check": "body", "jsonpath": "$.code", "comparator": "is_not_none"}]
    weakenings = cio.find_weakenings(before, after)
    assert weakenings and weakenings[0]["why"] == "同一个目标上比较变松了"


def test_unknown_comparator_is_not_flagged(excel_file):
    """不认识的比较符不判 —— 宁可漏判，也不要误伤正常改动。"""
    from ai.agent.tools import case_io as cio

    before = [{"check": "body", "jsonpath": "$.x", "comparator": "某个新玩法", "expect": 1}]
    after = [{"check": "body", "jsonpath": "$.x", "comparator": "exists"}]
    assert cio.find_weakenings(before, after) == []


def test_status_change_writes_the_status_column(excel_file):
    result = case_io.apply_status_change("API-002", "broken", note="已知问题")
    assert result["before"] == "debugged"
    assert result["after"] == "broken"
    assert _cell(excel_file, 3, "用例状态") == "broken"
    assert _cell(excel_file, 2, "用例状态") == "debugged", "不该动别的行"


def test_status_change_rejects_unknown_status(excel_file):
    with pytest.raises(ToolError):
        case_io.apply_status_change("API-001", "随便写的")


#: 质量闸要求正例 ≥ 2 个业务断言。这些用例只验证落盘结构，不会真的执行。
QUALITY_ASSERTS = [
    {"type": "equal", "field": "success", "expected": True},
    {"type": "exists", "field": "data"},
    {"type": "exists", "field": "data.records"},
]

#: 写用例还要有清理，否则质量闸直接拒绝落盘。
QUALITY_TEARDOWN = [
    {"id": "清理", "operation_type": "api", "method": "DELETE", "path": "/admin/x"},
]


def test_create_case_appends_a_draft_row(excel_file):
    result = case_io.create_case(
        project="proj",
        module="mod",
        path="/admin/x",
        method="POST",
        title="新增数据（智能体）",
        assertions=QUALITY_ASSERTS,
        request_data={"name": "x"},
        teardowns=QUALITY_TEARDOWN,
    )
    assert result["case_id"] == "API-003", "编号要接着现有的往下排"
    assert result["status"] == "draft", "新建的用例默认不进 CI"

    row = result["row"]
    assert _cell(excel_file, row, "请求方法") == "POST"
    assert _cell(excel_file, row, "用例状态") == "draft"
    assert json.loads(_cell(excel_file, row, "断言"))[0]["check"] == "body"


def test_create_case_names_case_by_convention(excel_file):
    """用例名称 = 模块显示名 - 业务动作（场景），不是 api_name - title。

    和现役 132 条手工用例保持一致：``title`` 只写场景，业务名放 ``api_name``。
    """
    result = case_io.create_case(
        project="proj",
        module="mod",
        path="/admin/x",
        method="POST",
        title="（正常请求）",
        api_name="新增数据",
        assertions=QUALITY_ASSERTS,
        teardowns=QUALITY_TEARDOWN,
    )
    assert _cell(excel_file, result["row"], "用例名称") == "mod - 新增数据（正常请求）"
    assert _cell(excel_file, result["row"], "接口名称") == "新增数据"
    assert _cell(excel_file, result["row"], "用例标题") == "（正常请求）"


def test_create_case_replaces_method_path_with_live_business_name(excel_file):
    """模型把「接口名」抄成 ``GET /admin/x`` 时，用现役中文名替换。

    报告里 ``GET /admin/x - GET /admin/x（正常请求）`` 完全看不出测什么；
    现役用例里同一个接口叫「查数据」，就该用它。
    """
    result = case_io.create_case(
        project="proj",
        module="mod",
        path="/admin/x",
        method="GET",
        title="GET /admin/x（正常请求）",
        api_name="GET /admin/x",
        assertions=QUALITY_ASSERTS,
    )
    assert _cell(excel_file, result["row"], "接口名称") == "查数据"
    assert _cell(excel_file, result["row"], "用例标题") == "（正常请求）"
    assert _cell(excel_file, result["row"], "用例名称") == "mod - 查数据（正常请求）"


def test_create_case_rebuilds_case_name_that_carries_method_path(excel_file):
    """调用方传了方法+路径拼的 case_name —— 作废重拼，不能原样落盘。"""
    result = case_io.create_case(
        project="proj",
        module="mod",
        path="/admin/x",
        method="GET",
        title="GET /admin/x（正常请求）",
        api_name="GET /admin/x",
        case_name="proj - GET /admin/x - GET /admin/x（正常请求）",
        assertions=QUALITY_ASSERTS,
    )
    assert _cell(excel_file, result["row"], "用例名称") == "mod - 查数据（正常请求）"


def test_create_case_does_not_borrow_name_across_methods(excel_file):
    """同一路径不同方法不能借名字：``POST /admin/x`` 不是「查数据」。"""
    result = case_io.create_case(
        project="proj",
        module="mod",
        path="/admin/x",
        method="POST",
        title="（正常请求）",
        api_name="POST /admin/x",
        assertions=QUALITY_ASSERTS,
        teardowns=QUALITY_TEARDOWN,
    )
    assert _cell(excel_file, result["row"], "接口名称") != "查数据"
    assert _cell(excel_file, result["row"], "用例名称") == "mod - POST /admin/x（正常请求）"


def test_norm_endpoint_matches_templates_and_concrete_ids():
    """``/admin/area/{id}`` 和 ``/admin/area/1101`` 归一成同一个键。"""
    assert case_io._norm_endpoint("/admin/area/{id}") == "/admin/area/{}"
    assert case_io._norm_endpoint("/admin/area/1101") == "/admin/area/{}"
    assert case_io._norm_endpoint("/admin/area/1101?x=1") == "/admin/area/{}"
    assert case_io._norm_endpoint("/admin/area/") == "/admin/area"


def test_align_case_naming_leaves_good_names_alone():
    result = case_io.align_case_naming(
        "proj", "mod", "/admin/x", "POST", "新增数据", "（正常请求）", "mod - 新增数据（正常请求）"
    )
    assert result == {
        "api_name": "新增数据",
        "title": "（正常请求）",
        "case_name": "mod - 新增数据（正常请求）",
    }


def test_create_case_defaults_auth_header(excel_file):
    result = case_io.create_case(
        project="proj",
        module="mod",
        path="/admin/x",
        method="GET",
        title="t",
        assertions=[{"type": "status_code", "expected": 200}],
    )
    headers = json.loads(_cell(excel_file, result["row"], "请求头"))
    assert list(headers)[0].lower() in ("authorization", "accesstoken")
    assert "{{proj.token}}" in list(headers.values())[0]


@pytest.mark.parametrize("status", ["debugged", "broken", "disabled"])
def test_create_case_rejects_non_draft_status(excel_file, status):
    """不能靠 new 的时候写 status 绕开「跑通过凭据」闸。"""
    with pytest.raises(ToolError):
        case_io.create_case(
            project="proj",
            module="mod",
            path="/admin/x",
            method="GET",
            title="t",
            assertions=[{"type": "status_code", "expected": 200}],
            status=status,
        )


def test_create_case_rejects_bad_module_name(excel_file):
    with pytest.raises(ToolError):
        case_io.create_case(
            project="proj",
            module="../escape",
            path="/x",
            method="GET",
            title="t",
            assertions=[{"type": "status_code", "expected": 200}],
        )


def test_create_case_requires_leading_slash(excel_file):
    with pytest.raises(ToolError):
        case_io.create_case(
            project="proj",
            module="mod",
            path="admin/x",
            method="GET",
            title="t",
            assertions=[{"type": "status_code", "expected": 200}],
        )


# --------------------------------------------------------------------------
# YAML 用例
# --------------------------------------------------------------------------


YAML_CASE = """\
# 这个文件里有注释，改写时不能丢
testcase:
  status: debugged
  name: 正向登录
  description: 登录

  steps:
    - id: login
      description: 登录
      method: POST
      path: /api/auth/login
      data:
        account: a
      assert:
        - type: equal
          field: code
          expected: 200
        # 这行注释在断言块里
        - type: is not None
          field: data.token

    - id: profile
      method: GET
      path: /api/user
      assert:
        - type: status_code
          expected: 200
"""


@pytest.fixture()
def yaml_file(tmp_path, monkeypatch):
    from utils.cases import case_source

    module_dir = tmp_path / "cases" / "proj" / "mod"
    module_dir.mkdir(parents=True)
    file_path = module_dir / "login.yaml"
    file_path.write_text(YAML_CASE, encoding="utf-8")

    # 来源开关默认是 excel，只扫 .xlsx —— 要一起放开后缀过滤
    monkeypatch.setattr(case_source, "get_source_roots", lambda *a, **k: [tmp_path / "cases"])
    monkeypatch.setattr(
        case_source, "get_allowed_suffixes", lambda *a, **k: (".yaml",)
    )
    from ai.agent.tools._common import clear_cache

    clear_cache()
    yield file_path
    clear_cache()


def test_yaml_find_assert_block(yaml_file):
    lines = yaml_file.read_text(encoding="utf-8").splitlines(keepends=True)
    start, end, indent = case_io._find_assert_block(lines, "login")
    block = "".join(lines[start:end])
    assert block.startswith(" " * indent + "assert:")
    assert "data.token" in block
    assert "status_code" not in block, "不该把下一个步骤的断言也圈进来"


def test_yaml_apply_keeps_comments_and_other_steps(yaml_file):
    case_io.apply_assertion_change(
        "正向登录",
        [{"type": "equal", "field": "code", "expected": 200}],
        step="login",
        note="简化断言",
    )
    text = yaml_file.read_text(encoding="utf-8")

    assert "# 这个文件里有注释，改写时不能丢" in text, "文件头注释不能丢"
    assert "id: profile" in text, "别的步骤不能被删"
    assert "status_code" in text, "第二个步骤的断言还在"
    assert "data.token" not in text, "被替换掉的老断言应该没了"

    import yaml

    data = yaml.safe_load(text)
    assert data["testcase"]["steps"][0]["assert"] == [
        {"type": "equal", "field": "code", "expected": 200}
    ]


def test_yaml_apply_rejects_unknown_step(yaml_file):
    with pytest.raises(ToolError):
        case_io.apply_assertion_change(
            "正向登录",
            [{"type": "equal", "field": "code", "expected": 200}],
            step="不存在",
        )


def test_yaml_status_change_keeps_comments(yaml_file):
    case_io.apply_status_change("正向登录", "broken", note="挂了")
    text = yaml_file.read_text(encoding="utf-8")
    assert text.startswith("# 这个文件里有注释")
    assert "\n  status: broken" in text, "缩进要保留（status 在 testcase 下面）"


def _rewrite_yaml(yaml_file, text: str) -> None:
    """换掉用例文件内容并清缓存（mtime 可能同秒，只靠 mtime 不保险）。"""
    yaml_file.write_text(text, encoding="utf-8")
    from ai.agent.tools._common import clear_cache

    clear_cache()


NESTED_STATUS_YAML = """\
testcase:
  name: 上架商品
  steps:
    - id: s1
      method: POST
      path: /admin/prod/status
      data:
        status: 1
      assert:
        - type: equal
          field: success
          expected: true
"""

FLAT_NESTED_STATUS_YAML = """\
name: 上架商品
steps:
  - id: s1
    method: POST
    path: /admin/prod/status
    data:
      status: 1
    assert:
      - type: equal
        field: success
        expected: true
"""


def test_yaml_status_change_does_not_touch_nested_status(yaml_file):
    """步骤请求体里的 ``status: 1`` 不是用例状态。

    以前取「第一个匹配」，请求体排在前面时会把 ``data.status`` 改成
    ``broken`` —— 文件还能解析、用例还在，返回「写成功」，用例本身仍是
    draft，接口参数反被改坏。
    """
    _rewrite_yaml(yaml_file, NESTED_STATUS_YAML)

    case_io.apply_status_change("上架商品", "broken")

    import yaml

    data = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
    assert data["testcase"]["status"] == "broken", "状态要写到用例本体上"
    assert data["testcase"]["steps"][0]["data"]["status"] == 1, "请求体不能被改"


def test_yaml_status_inserted_inside_testcase_wrapper(yaml_file):
    """没有 status 键时要插在 ``testcase:`` 下面，不是文件开头。"""
    _rewrite_yaml(yaml_file, NESTED_STATUS_YAML)

    case_io.apply_status_change("上架商品", "disabled")

    import yaml

    data = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
    assert data["testcase"]["status"] == "disabled"
    assert "status" not in {k for k in data if k != "testcase"}, "不能写成 testcase 的兄弟键"


def test_flat_yaml_status_inserted_at_top_level(yaml_file):
    """扁平用例（没有 ``testcase:`` 包装）要插在顶格。"""
    _rewrite_yaml(yaml_file, FLAT_NESTED_STATUS_YAML)

    case_io.apply_status_change("上架商品", "broken")

    import yaml

    data = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
    assert data["status"] == "broken"
    assert data["steps"][0]["data"]["status"] == 1, "请求体不能被改"


FLAT_YAML_WITH_HEADER = """\
# 扁平用例的注释头，改写时不能丢
---
name: 上架商品
steps:
  - id: s1
    method: POST
    path: /admin/prod/status
"""


def test_flat_yaml_status_inserted_after_document_header(yaml_file):
    """``status`` 不能插到 ``---`` 前面 —— 那样会变成两份文档，
    用例本体落到第二份，``safe_load`` 只读第一份（里面只有 status）。"""
    _rewrite_yaml(yaml_file, FLAT_YAML_WITH_HEADER)

    case_io.apply_status_change("上架商品", "broken")

    import yaml

    text = yaml_file.read_text(encoding="utf-8")
    assert text.startswith("# 扁平用例的注释头"), "文件头注释不能丢"
    data = yaml.safe_load(text)
    assert data["status"] == "broken"
    assert data["name"] == "上架商品", "用例本体要和 status 在同一份文档里"


def test_replace_yaml_scalar_prefers_outermost_key():
    """同一个键在多个缩进层级出现时，认最外层那个。"""
    text = (
        "testcase:\n"
        "  steps:\n"
        "    - data:\n"
        "        status: 1\n"
        "  status: draft\n"
    )

    out = case_io._replace_yaml_scalar(text, "status", "broken")

    assert "        status: 1" in out, "内层不能被改"
    assert "\n  status: broken\n" in out


def test_verify_reports_status_that_did_not_land(yaml_file):
    """复验要核对「文件里读出来的状态确实是它」—— 否则改到别处也会报成功。"""
    errors = case_io._verify(yaml_file, "正向登录", expect_status="broken")

    assert any("状态没写进去" in e for e in errors)


def test_status_change_rolls_back_when_status_did_not_land(yaml_file, monkeypatch):
    """写是写完了、但状态没落到用例本体上 → 必须回滚，不能报成功。"""
    before = yaml_file.read_text(encoding="utf-8")
    monkeypatch.setattr(case_io, "_replace_yaml_scalar", lambda text, key, value: text)

    with pytest.raises(ToolError) as exc:
        case_io.apply_status_change("正向登录", "broken")

    assert "回滚" in str(exc.value)
    assert yaml_file.read_text(encoding="utf-8") == before
