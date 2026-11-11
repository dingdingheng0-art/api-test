"""核对沙箱生成产物 ``ai_area_chain.xlsx`` 是否真的带上了造数链路。

这是对**落盘结果**的验证（不是规划器的单测）：走真实 Excel 加载器，
所以能同时发现：

- 前置/后置断言格式不对（框架格式写进去，flat_parser 读回来会报错）
- 造数链路没写进 Excel，或者主步骤还是文档里的占位值（areaId=0 / "string"）
- DELETE 主步骤路径还是写死的 id，而不是引用查数步骤

文件不存在时跳过 —— 它是一次性的沙箱产物，不随仓库长期保留。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from testcases.case_loader import load_cases_from_file

ARTIFACT = (
    PROJECT_ROOT
    / "testcases"
    / "cases_excel"
    / "mall4j"
    / "ai_area_chain"
    / "ai_area_chain.xlsx"
)

pytestmark = pytest.mark.skipif(
    not ARTIFACT.exists(), reason="ai_area_chain 沙箱产物不存在（先生成再跑）"
)


@pytest.fixture(scope="module")
def cases() -> list[dict]:
    return [item["testcase"] for item in load_cases_from_file(ARTIFACT, project="mall4j")]


def _by_method(cases: list[dict], method: str) -> list[dict]:
    out = []
    for case in cases:
        steps = case.get("steps") or []
        if steps and str(steps[0].get("method") or "").upper() == method:
            out.append(case)
    return out


def _chain_cases(cases: list[dict], method: str) -> list[dict]:
    return [
        c
        for c in _by_method(cases, method)
        if "api_data_chain" in (c.get("marks") or [])
    ]


def test_artifact_loads_with_all_cases(cases):
    """能完整加载：前置/后置断言格式必须能被 flat_parser 读回来。"""
    assert len(cases) == 17


def test_positive_writes_carry_data_chain(cases):
    """三种写主步骤都至少有一条挂了造数链路。"""
    for method in ("POST", "PUT", "DELETE"):
        assert _chain_cases(cases, method), f"{method} 正例没有挂造数链路"


def test_post_positive_uses_fixed_name_and_cleanup(cases):
    case = _chain_cases(cases, "POST")[0]
    step = case["steps"][0]
    assert "{{tools.fixed(" in str((step.get("data") or {}).get("areaName"))
    assert [t.get("id") for t in case.get("teardowns") or []] == ["查数据", "清理数据"]


def test_put_positive_updates_own_seed_id(cases):
    case = _chain_cases(cases, "PUT")[0]
    step = case["steps"][0]
    data = step.get("data") or {}
    # 文档示例的 areaId=0 实测报 A00001，必须换成查数步骤查到的 id
    assert "find_id_by_fixed" in str(data.get("areaId"))
    assert [s.get("id") for s in case.get("setups") or []] == ["造数据", "查数据"]


def test_delete_positive_deletes_own_seed(cases):
    case = _chain_cases(cases, "DELETE")[0]
    step = case["steps"][0]
    assert "find_id_by_fixed" in str(step.get("path"))
    assert [s.get("id") for s in case.get("setups") or []] == ["造数据", "查数据"]
    assert not case.get("teardowns")


def test_prepost_asserts_are_flat_in_raw_excel():
    """前置/后置断言在**原始单元格**里必须是扁平格式。

    注意：``load_cases_from_file`` 会把扁平断言翻译成框架断言，所以不能
    用加载后的对象判断 —— 必须直接读 xlsx 的原始 JSON 文本。写进去的是
    框架格式的话，读回来会报「不认识的断言类型 check=''」。
    """
    import json

    import openpyxl

    from utils.excel.flat_parser import SHEET_FLAT

    workbook = openpyxl.load_workbook(ARTIFACT)
    sheet = workbook[SHEET_FLAT]
    headers = {
        str(cell.value).strip(): idx
        for idx, cell in enumerate(sheet[1], start=1)
        if cell.value is not None
    }
    prepost_columns = [headers[name] for name in ("前置", "后置") if name in headers]
    assert prepost_columns, "Excel 里找不到前置/后置列"

    checked = 0
    for row in sheet.iter_rows(min_row=2):
        for column in prepost_columns:
            raw = row[column - 1].value
            if not raw:
                continue
            steps = json.loads(raw) if isinstance(raw, str) else raw
            for step in steps:
                for assertion in step.get("assert") or []:
                    assert "check" in assertion, (step.get("id"), assertion)
                    assert "jsonpath" in assertion
                    assert "comparator" in assertion
                    assert "expect" in assertion
                    checked += 1
    assert checked > 0, "没有任何前置/后置断言可校验"


def test_case_names_follow_business_convention(cases):
    """落盘的名字必须是中文业务名，不能是「PUT /admin/area」。

    - 报告标题取「接口名称」→ 必须是中文业务动作
    - 用例名称（alias）→「模块中文名 - 业务功能（场景）」
    """
    import re

    method_path = re.compile(r"^(GET|POST|PUT|PATCH|DELETE)\s+\S+$")
    for case in cases:
        name = str(case.get("name") or "")
        assert name and not method_path.match(name), f"接口名称还是方法+路径: {name!r}"
        assert re.search(r"[一-鿿]", name), f"接口名称没有中文: {name!r}"

        alias = str(case.get("alias") or "")
        assert alias, f"用例名称丢了: {case.get('case_id')}"
        assert " - " in alias, f"用例名称缺「模块 - 业务」结构: {alias!r}"
        assert not method_path.search(alias.split(" - ", 1)[1] if " - " in alias else alias), (
            f"用例名称里还有方法+路径: {alias!r}"
        )


def test_chain_cases_are_runnable(cases):
    """挂了造数链路就不该再标 needs_manual_setup —— 否则真实执行会被过滤掉。"""
    offenders = [
        (c.get("case_id"), c.get("name"), c.get("marks"))
        for c in cases
        if "api_data_chain" in (c.get("marks") or [])
        and "needs_manual_setup" in (c.get("marks") or [])
    ]
    assert not offenders, f"挂了链路却仍标人工: {offenders}"


def test_chain_prepost_has_no_unresolved_single_brace(cases):
    """链路里不能残留单花括号路径参数（运行时会被编码成 %7Bid%7D）。"""
    import re

    single = re.compile(r"(?<!\{)\{[A-Za-z_][A-Za-z0-9_]*\}(?!\})")
    for case in cases:
        for group in ("setups", "teardowns"):
            for item in case.get(group) or []:
                assert not single.search(str(item.get("path") or "")), (
                    case.get("name"),
                    item.get("id"),
                    item.get("path"),
                )
