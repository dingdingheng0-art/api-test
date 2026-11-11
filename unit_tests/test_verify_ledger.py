"""「跑通过」凭据（``ai/agent/tools/verify_ledger.py``）与转正闸。

**为什么要测**

``set_case_status(status="debugged")`` 会把用例放进 CI。以前这一步只看
文件写没写成功 —— 智能体一次都没跑也能标绿，CI 里那条用例从没执行过。
现在转正要求账本里有这条用例的通过记录，且用例内容没再改过。

这里锁三件事：

1. 指纹只认用例自身内容（改 status 不算改，改断言算改）
2. 记账 / 查账能对上；换文件、改内容都对不上
3. ``apply_status_change`` 转 debugged 时真的拦得住；``force`` 必须写理由
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai.agent.tools import case_io, verify_ledger  # noqa: E402
from ai.agent.tools.registry import ToolError  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    """账本写到临时目录 —— 不能污染真实的 ``.cache``。"""
    from utils import paths

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setattr(paths, "CACHE_DIR", cache_dir)
    return cache_dir


@pytest.fixture()
def case_file(tmp_path, monkeypatch):
    """一个只有一条 draft 用例的最小扁平 Excel。"""
    import openpyxl

    from utils.cases import case_source

    module_dir = tmp_path / "cases_excel" / "proj" / "mod"
    module_dir.mkdir(parents=True)
    path = module_dir / "mod.xlsx"

    wb = openpyxl.Workbook()
    wb.active.title = case_io.SHEET_FLAT
    sheet = wb[case_io.SHEET_FLAT]
    for idx, name in enumerate(case_io.FLAT_HEADERS, start=1):
        sheet.cell(1, idx).value = name

    headers = {name: idx for idx, name in enumerate(case_io.FLAT_HEADERS, start=1)}
    row = {
        "case_id": "API-010",
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
        # 质量闸要求正例 ≥ 2 个业务断言 + 转正时 ≥ 1 个值断言；
        # 这些只是让转正闸的测试能走到「凭据」那一步，不会真的发请求。
        "断言": json.dumps(
            [
                {"check": "body", "jsonpath": "$.success", "comparator": "==", "expect": True},
                {"check": "body", "jsonpath": "$.data", "comparator": "exists", "expect": None},
                {"check": "body", "jsonpath": "$.data.total", "comparator": ">=", "expect": 0},
            ],
            ensure_ascii=False,
        ),
        "用例状态": "draft",
    }
    for name, value in row.items():
        sheet.cell(2, headers[name]).value = value
    wb.save(path)

    monkeypatch.setattr(case_source, "get_source_roots", lambda *a, **k: [tmp_path / "cases_excel"])
    monkeypatch.setattr(
        case_source, "current_excel_dir", lambda *a, **k: tmp_path / "cases_excel"
    )
    from ai.agent.tools._common import clear_cache

    clear_cache()
    yield path
    clear_cache()


def _case_of_file(path: Path) -> dict:
    loaded = case_io._load_all("proj")
    for file_path, case in loaded:
        if Path(file_path).resolve() == path.resolve():
            return case
    raise AssertionError("临时用例没被读到")


# --------------------------------------------------------------------------
# 指纹
# --------------------------------------------------------------------------


def test_fingerprint_ignores_status():
    """转正本身就是改 status —— 带着它算，改完就查不到自己的记录。"""
    case = {"case_id": "API-010", "name": "查数据", "status": "draft"}
    other = dict(case, status="debugged")
    assert verify_ledger.fingerprint(case) == verify_ledger.fingerprint(other)


def test_fingerprint_changes_with_content():
    case = {"case_id": "API-010", "name": "查数据", "steps": [{"path": "/a"}]}
    changed = {"case_id": "API-010", "name": "查数据", "steps": [{"path": "/b"}]}
    assert verify_ledger.fingerprint(case) != verify_ledger.fingerprint(changed)


# --------------------------------------------------------------------------
# 记账 / 查账
# --------------------------------------------------------------------------


def _ref(path: Path):
    # 用绝对路径，和 verified_state 传进来的文件路径同一套 _rel 计算
    return SimpleNamespace(file=str(path))


def test_record_then_verified(case_file):
    case = _case_of_file(case_file)
    assert verify_ledger.verified_state(case_file, case) == (False, "账本里没有它的通过记录")

    written = verify_ledger.record([(_ref(case_file), case)])
    assert written == 1
    assert verify_ledger.verified_state(case_file, case) == (True, "")


def test_verified_rejects_changed_case(case_file):
    case = _case_of_file(case_file)
    verify_ledger.record([(_ref(case_file), case)])

    changed = dict(case, name="查数据（改过了）")
    trusted, reason = verify_ledger.verified_state(case_file, changed)
    assert not trusted
    assert "改过" in reason


def test_verified_rejects_other_file(case_file):
    case = _case_of_file(case_file)
    verify_ledger.record([(_ref(case_file), case)])

    trusted, reason = verify_ledger.verified_state(case_file.parent / "other.xlsx", case)
    assert not trusted
    assert "另一个" in reason


def test_record_skips_blank_key():
    """YAML 用例没名字没编号时不能记 —— 空键会把所有这类用例串成一条。"""
    assert verify_ledger.record([(_ref(Path("x.yaml")), {"name": ""})]) == 0


# --------------------------------------------------------------------------
# 转正闸
# --------------------------------------------------------------------------


def test_debugged_requires_passing_record(case_file):
    with pytest.raises(ToolError) as exc:
        case_io.apply_status_change("API-010", "debugged")
    assert "跑通过" in str(exc.value)


def test_debugged_allowed_after_record(case_file):
    case = _case_of_file(case_file)
    verify_ledger.record([(_ref(case_file), case)])

    result = case_io.apply_status_change("API-010", "debugged", note="跑通了")
    assert result["after"] == "debugged"


def test_debugged_rejects_record_from_before_the_edit(case_file):
    case = _case_of_file(case_file)
    verify_ledger.record([(_ref(case_file), case)])

    # 模拟：记账之后用例又被改过（这里直接改内存里的 case 来代表新内容）
    import ai.agent.tools.case_io as cio

    original = cio._case_of

    def _drifted(case_no, project=""):
        path, current = original(case_no, project)
        return path, dict(current, name="查数据（后来改过）")

    cio._case_of = _drifted
    try:
        with pytest.raises(ToolError) as exc:
            case_io.apply_status_change("API-010", "debugged")
    finally:
        cio._case_of = original
    assert "改过" in str(exc.value)


def test_force_needs_a_reason(case_file):
    with pytest.raises(ToolError) as exc:
        case_io.apply_status_change("API-010", "debugged", force=True)
    assert "note" in exc.value.hint


def test_force_with_reason_is_allowed(case_file):
    result = case_io.apply_status_change(
        "API-010", "debugged", force=True, note="环境故障，无法执行"
    )
    assert result["after"] == "debugged"


def test_already_debugged_is_not_gated_again(case_file):
    """已经是 debugged 再设一次是空操作 —— 不因为账本过期就拦（比如只改 note）。"""
    case_io.apply_status_change("API-010", "debugged", force=True, note="初始")
    result = case_io.apply_status_change("API-010", "debugged", note="再确认一次")
    assert result["before"] == "debugged"
    assert result["after"] == "debugged"


def test_other_statuses_are_not_gated(case_file):
    """broken / disabled 不改变「能不能进 CI」的语义，不该被凭据拦住。"""
    assert case_io.apply_status_change("API-010", "broken")["after"] == "broken"
    assert case_io.apply_status_change("API-010", "disabled")["after"] == "disabled"


# --------------------------------------------------------------------------
# 记账条件
# --------------------------------------------------------------------------


def test_record_verified_skips_when_counts_disagree(monkeypatch):
    """候选条数和实际执行条数对不上 → 说不清哪几条过了，宁可不记。"""
    from ai.agent.tools import execution

    monkeypatch.setattr(
        execution,
        "_matching_cases",
        lambda **kwargs: [("ref", {"case_id": "API-001"})],
    )
    calls = []
    import ai.agent.tools.verify_ledger as ledger

    monkeypatch.setattr(ledger, "record", lambda pairs: calls.append(pairs) or len(pairs))

    assert execution._record_verified(2, run_draft=True, project="p", module="m",
                                      mark="", keyword="", case_file="", case_id="") == 0
    assert calls == []


def test_record_verified_counts_parameterized_groups(monkeypatch):
    """带参数化的用例一条会展开成多条 —— 候选数也要按展开后算，否则永远对不上。"""
    from ai.agent.tools import execution

    pairs = [
        (
            SimpleNamespace(file="a.xlsx"),
            {
                "case_id": "API-003",
                "status": "debugged",
                "parameters": [{"x": 1}, {"x": 2}],
            },
        )
    ]
    monkeypatch.setattr(execution, "_matching_cases", lambda **kwargs: pairs)

    seen = []
    import ai.agent.tools.verify_ledger as ledger

    monkeypatch.setattr(ledger, "record", lambda ps: seen.extend(ps) or len(ps))

    # 展开成 2 条，result.total 也是 2 → 记账成功
    assert execution._record_verified(
        2, run_draft=False, project="p", module="m", mark="",
        keyword="", case_file="", case_id="",
    ) == 1
    assert len(seen) == 1


def test_record_verified_respects_status_filter(monkeypatch):
    """不开 run_draft 时 pytest 只跑 debugged —— 候选数也要按这个口径算。"""
    from ai.agent.tools import execution

    pairs = [
        (SimpleNamespace(file="a.xlsx"), {"case_id": "API-001", "status": "debugged"}),
        (SimpleNamespace(file="a.xlsx"), {"case_id": "API-002", "status": "draft"}),
    ]
    monkeypatch.setattr(execution, "_matching_cases", lambda **kwargs: pairs)

    seen = []
    import ai.agent.tools.verify_ledger as ledger

    monkeypatch.setattr(ledger, "record", lambda ps: seen.extend(ps) or len(ps))

    assert execution._record_verified(
        1, run_draft=False, project="p", module="m", mark="",
        keyword="", case_file="", case_id="",
    ) == 1
    assert [case["case_id"] for _, case in seen] == ["API-001"]


def test_record_verified_keeps_drafts_when_case_id_given(monkeypatch):
    """按编号挑用例时 pytest 不看状态 —— draft 也要记账，否则自愈验证记不上。"""
    from ai.agent.tools import execution

    pairs = [(SimpleNamespace(file="a.xlsx"), {"case_id": "API-009", "status": "draft"})]
    monkeypatch.setattr(execution, "_matching_cases", lambda **kwargs: pairs)

    seen = []
    import ai.agent.tools.verify_ledger as ledger

    monkeypatch.setattr(ledger, "record", lambda ps: seen.extend(ps) or len(ps))

    assert execution._record_verified(
        1, run_draft=False, project="", module="", mark="",
        keyword="", case_file="", case_id="API-009",
    ) == 1
    assert seen[0][1]["case_id"] == "API-009"


# --------------------------------------------------------------------------
# 账本被手改坏：坏的那一条按「没有记录」处理，不能拖垮整份账本
# --------------------------------------------------------------------------


def _write_ledger(cache_dir: Path, cases: dict) -> None:
    (cache_dir / verify_ledger.LEDGER_NAME).write_text(
        json.dumps({"version": verify_ledger.VERSION, "cases": cases}),
        encoding="utf-8",
    )


def test_corrupt_record_is_treated_as_missing(isolated_cache, case_file):
    """某条记录被写成字符串时 ``entry.get`` 抛的是 AttributeError ——
    ``set_case_status`` 会把内部异常漏给调用方而不是拒绝转正。"""
    case = _case_of_file(case_file)
    _write_ledger(isolated_cache, {"API-010": "garbage"})

    assert verify_ledger.verified_state(case_file, case) == (
        False,
        "账本里没有它的通过记录",
    )


def test_corrupt_record_does_not_block_new_recording(isolated_cache, case_file):
    """坏记录不能被 ``_prune`` 带崩 —— 否则从此再也记不进去，转正永远被拒。"""
    case = _case_of_file(case_file)
    _write_ledger(isolated_cache, {"API-999": 123})

    assert verify_ledger.record([(_ref(case_file), case)]) == 1
    assert verify_ledger.verified_state(case_file, case) == (True, "")
