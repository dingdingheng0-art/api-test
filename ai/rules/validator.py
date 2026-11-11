"""用例质量闸门：写入口共用的硬约束。

**为什么要有这一层**

``utils.cases.case_validator`` 只管「存得下、跑得动」—— 断言类型认不认识、
必填字段缺没缺。它放行一条只有 ``status_code == 200`` 的用例，也放行一条
造了数据没人清的写用例。质量口径（维度、断言密度、清理）以前只写在
``.claude/skills/apitest-design/SKILL.md`` 里，靠模型自觉。

2026-11-10 的全量生成实测就是证据：498 条用例里 141 条正例业务断言 < 2，
25 条写用例造了数没人清，而它们全部落盘成功。

**这里只做「不能自动补」的硬约束**

能自动修的（补 ``code``/``success``、把 ``{id}`` 换成真实值、挂造数链路）
在生成器里做；本模块只拦那些**必须由人/模型补信息**的情况：

- 正例业务断言不足；多步用例的验证步没有业务断言
- 反例没有任何「期望失败」的断言（含零断言）
- 正例写操作既没有 teardowns，也不是 DELETE 自清理（多步链逐个写步骤检查）
- 路径参数还是 ``{id}`` 就发请求（``{{...}}`` 是运行时变量，不算），
  步骤 / 前置 / 后置都查
- 名字是「方法 + 路径」
- 维度标签声明了但用例内容没证实
- 转 debugged 时值断言不足（``exists`` / ``is_not_none`` 只证明字段存在）

**按模块分两档**

现役模块（``admin_area`` 这类）是人的资产：违规直接拒绝，不留半成品。
沙箱模块（``ai_*``）是生成器的草稿区：允许落盘，但**强制标
``needs_manual_setup``** 并把原因写进返回 —— 报告里能看见，CI 里不会跑。

规则来源是 ``ai/rules/case_rules.yaml`` 的 ``quality_gates``，阈值改了
这里自动跟着走，不在代码里再抄一份。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from utils.logs.log_manager import log

RULES_FILE = Path(__file__).with_name("case_rules.yaml")

#: 不算业务断言的类型 / 字段（和 dimension_coverage 保持同一口径）
_WEAK_TYPES = {"status_code", "status", "http_status"}
_WEAK_FIELDS = {"", "code", "success"}

#: 只证明「字段存在」的断言 —— 不是值断言
_EXISTENCE_TYPES = {
    "exists", "not_exists", "is_none", "is_not_none", "is_null", "is_not_null",
}

#: 「期望失败」的断言 —— 和 dimension_coverage._asserts_rejection 同源
_REJECTION_STATUS = {"401", "403"}
_SUCCESS_CODES = {"", "00000", "200", "0"}

#: 写方法
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

#: 名字是「方法 + 路径」
_METHOD_NAME_RE = re.compile(r"^(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+/")
#: 单花括号路径参数（``{id}``），不碰框架变量 ``{{...}}``
_SINGLE_BRACE_RE = re.compile(r"(?<!\{)\{[A-Za-z_][A-Za-z0-9_]*\}(?!\})")

#: 沙箱模块前缀 —— 违规只警告、不拒绝
SANDBOX_PREFIX = "ai_"


@dataclass
class Violation:
    """一条违规。``rule`` 是稳定标识，报告和测试都按它断言。"""

    rule: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"rule": self.rule, "message": self.message}


@dataclass
class GateResult:
    """闸门结论。"""

    violations: list[Violation] = field(default_factory=list)
    #: 沙箱模块里被「降级」的违规 —— 用例照落盘，但标 needs_manual_setup
    downgraded: bool = False

    @property
    def ok(self) -> bool:
        return not self.violations

    def messages(self) -> list[str]:
        return [item.message for item in self.violations]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "downgraded": self.downgraded,
            "violations": [item.to_dict() for item in self.violations],
        }


def _load_gates() -> dict[str, Any]:
    """读 ``case_rules.yaml`` 的 ``quality_gates``。

    读不到就退回代码里的默认值 —— 规则文件被删不该让整个写入口瘫痪，
    但会打一条 warning，让人知道闸门在用默认阈值。
    """
    defaults = {
        "min_business_asserts": 2,
        "min_value_asserts": 0,
        "require_negative_rejection": True,
        "require_write_cleanup": True,
        "reject_method_path_naming": True,
        "multi_step_assert_on_verification": True,
    }
    try:
        data = yaml.safe_load(RULES_FILE.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:  # noqa: BLE001 - 规则文件坏了不能拖垮写入
        log.warning("读取 %s 失败（%s），质量闸门用默认阈值", RULES_FILE.name, exc)
        return defaults
    gates = data.get("quality_gates") if isinstance(data, dict) else None
    if not isinstance(gates, dict):
        return defaults
    merged = dict(defaults)
    for key in defaults:
        if key in gates:
            merged[key] = gates[key]
    return merged


def load_gates() -> dict[str, Any]:
    """公开入口：当前生效的质量阈值（写入口要在此基础上叠加临时要求）。"""
    return _load_gates()


def _as_list(value: Any) -> list[Any]:
    """把可能写成标量的字段收敛成列表（脏数据不抛异常）。"""
    if isinstance(value, (list, tuple, set, frozenset)):
        return list(value)
    if value is None:
        return []
    return [value]


def _asserts_of(step: Any) -> list[dict[str, Any]]:
    if not isinstance(step, dict):
        return []
    asserts = step.get("assert")
    if isinstance(asserts, dict):
        asserts = [asserts]
    return [item for item in _as_list(asserts) if isinstance(item, dict)]


def _assert_type(item: dict[str, Any]) -> str:
    return str(item.get("type") or "").strip().lower().replace(" ", "_")


def business_asserts(asserts: list[dict[str, Any]]) -> int:
    """数业务断言：``status_code`` / ``code`` / ``success`` 之外都算。

    落库断言也算业务断言 —— 它验证的是数据真的落了。
    """
    count = 0
    for item in asserts:
        atype = _assert_type(item)
        if atype.startswith(("db_query", "mysql_")):
            count += 1
            continue
        if atype in _WEAK_TYPES:
            continue
        if str(item.get("field") or "") in _WEAK_FIELDS:
            continue
        count += 1
    return count


def value_asserts(asserts: list[dict[str, Any]]) -> int:
    """数值断言：能证明「值对」的断言，而不是「字段在」。

    ``exists`` / ``is_not_none`` 这类结构断言只说明字段存在，响应塞个
    ``null`` 以外的垃圾值也能过。真正有检验力的是带 ``expected`` /
    ``container`` 的比较断言，以及落库断言。

    这个口径比 :func:`business_asserts` 更严：业务断言 = 结构断言 + 值断言，
    值断言是其中的「有信息量」子集。
    """
    count = 0
    for item in asserts:
        atype = _assert_type(item)
        if atype.startswith(("db_query", "mysql_")):
            count += 1
            continue
        if atype in _WEAK_TYPES or atype in _EXISTENCE_TYPES:
            continue
        if str(item.get("field") or "") in _WEAK_FIELDS:
            continue
        if "expected" not in item and "container" not in item:
            continue
        count += 1
    return count


def asserts_rejection(asserts: list[dict[str, Any]]) -> bool:
    """这组断言里有没有「期望失败」。

    和 ``dimension_coverage._asserts_rejection`` 同一套判定，
    不在这里另立一套口径。
    """
    for item in asserts:
        atype = _assert_type(item)
        field = str(item.get("field") or "")
        expected = item.get("expected")
        if (atype in _WEAK_TYPES or field == "$status") and str(expected) in _REJECTION_STATUS:
            return True
        if field == "success" and (
            atype in ("is_false", "false") or expected is False
        ):
            return True
        # ``success not_equal true`` 也是「期望失败」：不要因为只认
        # ``is_false`` / ``expected: false`` 就把它当成没有拒绝断言。
        if (
            field == "success"
            and atype in ("not_equal", "!=")
            and (expected is True or str(expected).strip().lower() == "true")
        ):
            return True
        if field == "code" and atype in ("not_equal", "!="):
            return True
        if (
            field == "code"
            and atype in ("equal", "equals", "==")
            and str(expected) not in _SUCCESS_CODES
        ):
            return True
        if field == "code" and atype == "in":
            container = item.get("container") or item.get("expected")
            container = container if isinstance(container, (list, tuple, set)) else [container]
            if "00000" not in {str(v) for v in container}:
                return True
    return False


def _marks_of(marks: Any) -> set[str]:
    if isinstance(marks, str):
        parts = marks.replace(";", ",").split(",")
    elif isinstance(marks, (list, tuple, set, frozenset)):
        parts = list(marks)
    else:
        return set()
    return {str(item).strip().lower() for item in parts if str(item).strip()}


_NEGATIVE_MARKS = {"negative", "boundary", "invalid", "permission"}
_NEGATIVE_DIMS = {
    "A1", "A2", "A3", "A4", "A5", "A6", "A7",
    "C1", "C2", "C3", "F1", "F2", "F3", "F4",
}


def _expects_success(asserts: list[dict[str, Any]]) -> bool:
    """这组断言有没有明确「期望成功」。

    ``boundary`` 标签本身不代表反例：边界用例可能是「超大 size 被截断」
    「超大 pid 返回空列表」，这些期望的是成功。只有没写成功期望时，
    边界才按反例要求「必须断言被拒绝」。
    """
    for item in asserts:
        atype = _assert_type(item)
        field = str(item.get("field") or "")
        expected = item.get("expected")
        if field == "success" and atype in ("is_true", "true", "equal", "equals", "=="):
            if expected is True or str(expected).strip().lower() == "true":
                return True
        if field == "code" and atype in ("equal", "equals", "=="):
            if str(expected) in _SUCCESS_CODES:
                return True
    return False


def _is_negative(
    marks: set[str],
    dimensions: set[str],
    asserts: list[dict[str, Any]] | None = None,
) -> bool:
    """显式的反例：有反例标记/维度才算。

    **不能把所有「非正例」都当反例** —— ``data is_none`` 这种「查不到」
    用例既不是正例也不是反例，硬套「必须断言被拒绝」会误伤。

    ``boundary`` 要单独看断言：期望成功的边界用例（空列表、上限截断）
    不是反例；只有没写成功期望的边界才按反例要求拒绝断言。
    显式 ``positive`` 可以压过 ``boundary``，但压不过 ``negative`` /
    负向维度 —— 那是更明确的意图。
    """
    if dimensions & _NEGATIVE_DIMS:
        return True
    if marks & (_NEGATIVE_MARKS - {"boundary"}):
        return True
    if "positive" in marks:
        return False
    if "boundary" in marks:
        return not _expects_success(asserts or [])
    return False


def _is_positive(marks: set[str], dimensions: set[str], asserts: list[dict[str, Any]]) -> bool:
    """正例判定：显式 positive / dim:P0，或既没有反例标记又有成功断言。"""
    if "positive" in marks or "p0" in dimensions:
        return True
    if _is_negative(marks, dimensions, asserts):
        return False
    return any(
        str(item.get("field") or "") == "success"
        and (_assert_type(item) in ("is_true", "true") or item.get("expected") is True)
        for item in asserts
    )


def _dimension_ids(marks: set[str]) -> set[str]:
    """从 ``dim:X`` 标签里取维度 ID（裸 ``p0`` 是优先级，不算维度）。"""
    return {
        str(mark).split(":", 1)[1].upper()
        for mark in marks
        if str(mark).lower().startswith("dim:") and ":" in str(mark)
    }


def _write_is_self_cleaning(steps: list[dict[str, Any]], setups: list[dict[str, Any]]) -> bool:
    """主步骤是不是 DELETE 自己造的数据（可以没有 teardowns）。

    和 ``data_orchestrator.write_is_self_cleaning`` 同一判据：
    全是 DELETE、且路径/参数里引用了某个 setup 的输出。
    """
    writes = [s for s in steps if str(s.get("method") or "").upper() in WRITE_METHODS]
    if not writes:
        return False
    if any(str(s.get("method") or "").upper() != "DELETE" for s in writes):
        return False
    setup_ids = [
        str(item.get("id")) for item in setups if isinstance(item, dict) and item.get("id")
    ]
    if not setup_ids:
        return False
    for step in writes:
        text = " ".join(
            str(x)
            for x in (step.get("path"), step.get("params"), step.get("data"))
            if x is not None
        )
        if not any(
            re.search(rf"(?<![\w一-鿿]){re.escape(sid)}\.", text) for sid in setup_ids
        ):
            return False
    return True


def write_is_self_cleaning(steps: Any, setups: Any) -> bool:
    """公开入口：主步骤是不是 DELETE 自己造的数据。

    报告脚本和 ``check_case`` 必须用同一套判据，否则「闸门说合格、
    报告说没清理」两边打架。
    """
    return _write_is_self_cleaning(
        [item for item in _as_list(steps) if isinstance(item, dict)],
        [item for item in _as_list(setups) if isinstance(item, dict)],
    )


def _all_writes_self_cleaning(
    write_steps: list[dict[str, Any]], setups: list[dict[str, Any]]
) -> bool:
    """每个写步骤都能自己清理掉自己造的数据。

    多步链里只要有一个写步骤没去处，整条用例就算没有清理。
    """
    return bool(write_steps) and all(
        _write_is_self_cleaning([step], setups) for step in write_steps
    )


def _verification_step(steps: list[dict[str, Any]]) -> dict[str, Any]:
    """多步链的「验证步」：最后一个非写步骤；整条链都是写操作时取最后一步。"""
    for step in reversed(steps):
        if str(step.get("method") or "").upper() not in WRITE_METHODS:
            return step
    return steps[-1] if steps else {}


def check_case(
    *,
    case_name: str = "",
    api_name: str = "",
    steps: Any = None,
    setups: Any = None,
    teardowns: Any = None,
    marks: Any = None,
    gates: dict[str, Any] | None = None,
) -> GateResult:
    """对一条用例做质量检查，返回违规清单。

    入参是**用例本体**（``steps`` / ``setups`` / ``teardowns`` / ``marks``），
    不依赖文件格式 —— ``create_case``（Excel）、``apply_case_change``（改断言后
    的整条用例）、生成器都能直接用。
    """
    settings = gates or _load_gates()
    step_list = [item for item in _as_list(steps) if isinstance(item, dict)]
    setup_list = [item for item in _as_list(setups) if isinstance(item, dict)]
    teardown_list = [item for item in _as_list(teardowns) if isinstance(item, dict)]
    mark_set = _marks_of(marks)
    dimensions = _dimension_ids(mark_set)

    result = GateResult()
    # 多步用例的断言要按**整条链**看：链的检验力在「验证步」，
    # 只数第一步（通常是造数/写操作）会把合格的场景链误判成薄用例。
    asserts = [item for step in step_list for item in _asserts_of(step)]
    positive = _is_positive(mark_set, dimensions, asserts)

    # ---- 1. 名字不能是「方法 + 路径」 ----
    # 只看最终用例名，不看 api_name：接口没有现役中文名时，create_case 的
    # 兜底业务名就是 ``POST /admin/x``，再拦就没法建这条用例了。最终展示给
    # 人的是 case_name，它才是"报告里长什么样"的那一层。
    if settings.get("reject_method_path_naming", True):
        text = str(case_name or "").strip()
        if text and _METHOD_NAME_RE.match(text):
            result.violations.append(
                Violation(
                    "method_path_naming",
                    f"用例名称是「方法 + 路径」（{text}），要写成中文业务动作",
                )
            )

    # ---- 2. 路径参数不能原样发出去 ----
    # 只认单花括号 ``{id}``。``{{tools.find_id_by_fixed(...)}}`` 这类
    # 框架变量是运行时会展开的合法写法（造数链路/鉴权头都靠它）。
    # 前置/后置也要查：带 ``{id}`` 的 DELETE 会被 URL 编码成 ``%7Bid%7D``，
    # 清理静默失败，脏数据留在环境里。
    for label, group in (
        ("步骤", step_list),
        ("前置", setup_list),
        ("后置", teardown_list),
    ):
        for item in group:
            path = str(item.get("path") or "")
            if _SINGLE_BRACE_RE.search(path):
                result.violations.append(
                    Violation(
                        "unresolved_path_param",
                        f"{label}的路径参数没解析就发请求：{path}",
                    )
                )
                break
        else:
            continue
        break

    # ---- 3. 正例：业务断言密度 ----
    min_asserts = int(settings.get("min_business_asserts", 2) or 0)
    if positive and min_asserts > 0:
        count = business_asserts(asserts)
        if count < min_asserts:
            result.violations.append(
                Violation(
                    "thin_positive",
                    f"正例只有 {count} 个业务断言（要求 ≥ {min_asserts}）；"
                    "status_code / code / success 不算",
                )
            )
        # 多步场景链：断言必须落在「验证步」（链里最后一个读步骤）上。
        # 只验写接口的回执，证明不了数据真的可查/可删。
        if len(step_list) > 1 and settings.get("multi_step_assert_on_verification", True):
            check_step = _verification_step(step_list)
            if business_asserts(_asserts_of(check_step)) < 1:
                result.violations.append(
                    Violation(
                        "thin_verification_step",
                        "多步用例的验证步没有任何业务断言 —— 只验了请求回执，"
                        f"没验数据（验证步：{check_step.get('method', '')} "
                        f"{check_step.get('path', '')}）",
                    )
                )

    # ---- 3b. 正例：值断言（转正时才要求） ----
    # 结构断言（exists / is_not_none）只能证明字段存在。转 debugged 时
    # 已经跑通过，运行结果里有真实响应，完全可以补一条带 expected 的值断言。
    min_values = int(settings.get("min_value_asserts", 0) or 0)
    if positive and min_values > 0:
        value_count = value_asserts(asserts)
        if value_count < min_values:
            result.violations.append(
                Violation(
                    "value_assert_missing",
                    f"正例只有 {value_count} 个值断言（转 debugged 要求 ≥ {min_values}）—— "
                    "exists / is_not_none 只证明字段存在",
                )
            )

    # ---- 4. 反例：必须断言「被拒绝」 ----
    # ``asserts`` 为空时也要报：一条只有请求、没有断言的「反例」
    # 既证明不了拒绝，也证明不了任何东西。
    if (
        _is_negative(mark_set, dimensions, asserts)
        and settings.get("require_negative_rejection", True)
        and not asserts_rejection(asserts)
    ):
        result.violations.append(
            Violation(
                "negative_without_rejection",
                "反例没有任何「期望失败」的断言 —— 发请求不等于验证拒绝",
            )
        )

    # ---- 5. 正例写操作：必须能清理 ----
    # 反例本来就被拒绝，不会留下数据；只有「预期成功」的写才要清理。
    # 多步链里**每个**写步骤都要有去处：要么有 teardowns，要么它本身是
    # 引用 setup 输出的 DELETE。只清理第一个写步骤，链尾的写照样留脏数据。
    write_steps = [
        s for s in step_list if str(s.get("method") or "").upper() in WRITE_METHODS
    ]
    if settings.get("require_write_cleanup", True) and positive and write_steps:
        if not teardown_list and not _all_writes_self_cleaning(write_steps, setup_list):
            methods = "/".join(
                sorted({str(s.get("method") or "").upper() for s in write_steps})
            )
            result.violations.append(
                Violation(
                    "write_without_cleanup",
                    f"{methods} 用例没有 teardowns，也不是 DELETE 自清理 —— "
                    "跑一次就在环境里留一条脏数据",
                )
            )

    # ---- 6. 维度标签必须被内容证实 ----
    if "D1" in dimensions or "D2" in dimensions or "D3" in dimensions:
        has_db = any(
            _assert_type(item).startswith(("db_query", "mysql_"))
            for step in step_list
            for item in _asserts_of(step)
        )
        if not has_db:
            result.violations.append(
                Violation(
                    "unverified_dimension",
                    "标了落库维度（D1/D2/D3），但主步骤没有 db_query 断言",
                )
            )
    if (
        "E3" in dimensions
        and not teardown_list
        and not _all_writes_self_cleaning(write_steps, setup_list)
    ):
        result.violations.append(
            Violation(
                "unverified_dimension",
                "标了清理隔离（E3），但没有 teardowns",
            )
        )

    return result


def enforce(
    module: str,
    result: GateResult,
) -> GateResult:
    """按模块决定「拒绝」还是「降级」。

    - 现役模块：违规 → 抛 ``ToolError``（调用方负责转成工具错误）
    - 沙箱 ``ai_*``：违规 → 保留，标 ``downgraded``，调用方补 ``needs_manual_setup``

    这里不直接 import ``ToolError``（``ai.rules`` 被生成器引用，生成器不该
    依赖 agent 工具包），由调用方判断 ``result.downgraded`` 后处理。
    """
    result.downgraded = str(module or "").startswith(SANDBOX_PREFIX) and bool(
        result.violations
    )
    return result


def is_sandbox(module: Any) -> bool:
    """是不是 AI 沙箱模块。"""
    return str(module or "").startswith(SANDBOX_PREFIX)


def format_hint(result: GateResult) -> str:
    """把违规清单转成给人看的提示（给 ToolError 的 hint 用）。"""
    lines = ["", *[f"  · {item.message}" for item in result.violations], "", "怎么修："]
    rules = {item.rule for item in result.violations}
    if "thin_positive" in rules:
        lines.append(
            "  · 正例补业务字段断言：从 response_example 里挑 data / data.records / 主键等字段，"
            "例如 {\"type\": \"is_not_none\", \"field\": \"data.records\"} 或 "
            "{\"type\": \"length_gt\", \"field\": \"data.records\", \"expected\": 0}"
        )
    if "thin_verification_step" in rules:
        lines.append(
            "  · 多步用例把断言补在验证步（最后一个读步骤）上，验证写入的数据真的查得到"
        )
    if "value_assert_missing" in rules:
        lines.append(
            "  · 用 get_last_result 取上一次跑通的真实响应，从中挑一个字段（或数据库查询）"
            "补带 expected 的值断言；exists / is_not_none 不算值断言"
        )
    if "negative_without_rejection" in rules:
        lines.append(
            "  · 反例断言「被拒绝」：code not_equal 成功码、success is_false、或 401/403"
        )
    if "write_without_cleanup" in rules:
        lines.append(
            "  · 写用例挂 setups（造数）+ teardowns（只删本用例造的），"
            "或改成 DELETE 主步骤引用 setup 的输出自清理"
        )
    if "unresolved_path_param" in rules:
        lines.append("  · 路径参数用真实 id，或用 {{步骤.id}} 引用 setup 的输出")
    if "method_path_naming" in rules:
        lines.append("  · 业务名写中文动作（如「修改地区」），场景放 title")
    if "unverified_dimension" in rules:
        lines.append("  · 删掉没证实的维度标签，或补上对应的断言/清理")
    return "\n".join(lines)
