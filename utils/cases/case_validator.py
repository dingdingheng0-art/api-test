"""用例格式校验：用 JSON Schema 提前拦住写错的用例。

**为什么需要它**

以前校验只看「必填字段有没有」，问题在于：

- 断言的 ``type`` 拼错了（比如写成 ``equl``），要跑到那一步才炸
- ``expected`` 写成了字符串而实际是数字，要等比对失败才知道
- 步骤的 ``method`` 写成了小写 ``post``，框架不认
- 步骤 id 重复，前面的结果被后面的覆盖

这些问题如果发生在用例跑到一半，前面造进环境的数据已经产生副作用了。
现在改成**收集阶段**就拦下来，报错直接告诉你是哪个文件、哪条用例、哪个字段。

**怎么用**

.. code-block:: python

    from utils.cases.case_validator import CaseValidator

    errors = CaseValidator.validate(case_data)
    if errors:
        raise TestCaseError("\\n".join(errors))

校验规则写在 ``schemas/testcase.schema.json``，改规则不用改代码。
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from utils.logs.log_manager import log
from utils.assertions import assert_types

# Schema 文件位置：<项目根>/schemas/testcase.schema.json
SCHEMA_FILE = Path(__file__).resolve().parents[2] / "schemas" / "testcase.schema.json"


@lru_cache(maxsize=1)
def _load_schema() -> dict[str, Any]:
    """加载并缓存 JSON Schema。"""
    if not SCHEMA_FILE.exists():
        log.warning("用例 Schema 不存在，跳过格式校验: %s", SCHEMA_FILE)
        return {}
    try:
        return json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("读取用例 Schema 失败，跳过格式校验: %s", exc)
        return {}


def _jsonschema_available() -> bool:
    try:
        import jsonschema  # noqa: F401
    except ImportError:
        return False
    return True


# 字段路径 → 人话解释
_FIELD_LABELS = {
    "testcase": "用例",
    "steps": "步骤列表",
    "step": "步骤",
    "assert": "断言",
    "setups": "前置准备",
    "teardowns": "后置清理",
    "headers": "请求头",
    "params": "URL参数",
    "data": "请求体",
    "files": "文件",
    "operation_type": "操作类型",
    "method": "请求方法",
    "path": "请求路径",
    "id": "步骤ID",
    "name": "名称",
    "expected": "期望值",
    "type": "类型",
    "field": "字段",
    "status": "状态",
    "marks": "标签",
    "parameters": "参数组",
    "depends_on": "依赖用例",
    "mock": "Mock配置",
}


def _humanize_path(path: list[Any]) -> str:
    """把 ``['testcase', 'steps', 0, 'assert', 1, 'type']`` 变成中文路径。"""
    parts: list[str] = []
    for item in path:
        if isinstance(item, int):
            parts.append(f"第{item + 1}项")
        else:
            parts.append(_FIELD_LABELS.get(str(item), str(item)))
    return " → ".join(parts) if parts else "根节点"


def _describe_error(error: Any) -> str:
    """把一条 jsonschema 错误变成人话。"""
    path = _humanize_path(list(error.absolute_path))
    validator = error.validator
    expected = error.validator_value

    if validator == "required":
        missing = str(error.message).split("'")[1] if "'" in error.message else ""
        label = _FIELD_LABELS.get(missing, missing)
        return f"{path} 缺少必填项「{label}」"

    if validator == "enum":
        allowed = "、".join(str(v) for v in expected)
        return f"{path} 的值不在允许范围内（只能是：{allowed}）"

    if validator == "type":
        return f"{path} 的类型不对（应该是 {expected}）"

    if validator == "minLength":
        return f"{path} 不能为空"

    if validator == "minItems":
        return f"{path} 至少要 {expected} 项"

    if validator == "additionalProperties":
        # 只提示第一个多余的字段
        extra = error.message.split("'")[1] if "'" in error.message else ""
        label = _FIELD_LABELS.get(extra, extra)
        return f"{path} 出现了不认识的字段「{label}」"

    return f"{path} 不合法（{error.message}）"


class CaseValidator:
    """用例格式校验器。"""

    def __init__(self, schema: dict[str, Any] | None = None):
        self.schema = schema if schema is not None else _load_schema()

    @property
    def available(self) -> bool:
        """校验器能不能用（缺 Schema 或缺 jsonschema 包时不可用）。"""
        return bool(self.schema) and _jsonschema_available()

    def validate(self, case_data: Any) -> list[str]:
        """校验一份用例数据，返回错误清单（空列表 = 通过）。

        Args:
            case_data: 完整用例数据（形如 ``{"testcase": {...}}``）

        Returns:
            错误描述列表，每条都是能直接给人看的句子
        """
        errors: list[str] = []
        errors.extend(self._validate_schema(case_data))
        errors.extend(self._validate_assertions(case_data))
        return errors

    def _validate_schema(self, case_data: Any) -> list[str]:
        """走 JSON Schema 校验（结构层面的问题）。"""
        if not self.available:
            return []

        try:
            import jsonschema
        except ImportError:
            return []

        validator_cls = jsonschema.validators.validator_for(self.schema)
        try:
            validator = validator_cls(self.schema)
        except jsonschema.exceptions.SchemaError as exc:
            log.error("用例 Schema 本身有问题，已跳过校验: %s", exc)
            return []

        errors: list[str] = []
        for error in sorted(validator.iter_errors(case_data), key=lambda e: list(e.absolute_path)):
            errors.append(_describe_error(error))

        return errors

    def _validate_assertions(self, case_data: Any) -> list[str]:
        """断言层面的校验：类型拼错、必填字段缺失。

        JSON Schema 管不了这个 —— 断言类型是一大堆别名（``equal``/``equals``/``==``），
        写错一个字母不会触发结构错误，只会在跑到那一步时抛"未知的断言类型"，
        而那时数据已经造进环境了。
        """
        if not isinstance(case_data, dict):
            return []

        case = case_data.get("testcase")
        if not isinstance(case, dict):
            return []

        errors: list[str] = []

        # 用例级断言
        errors.extend(self._check_assert_list(case.get("assert"), "用例"))

        # 步骤级断言
        for index, step in enumerate(case.get("steps") or []):
            if not isinstance(step, dict):
                continue
            step_id = step.get("id") or f"第{index + 1}步"
            errors.extend(self._check_assert_list(step.get("assert"), f"步骤「{step_id}」"))

        # 前置 / 后置
        for group, label in (("setups", "前置准备"), ("teardowns", "后置清理")):
            for index, item in enumerate(case.get(group) or []):
                if not isinstance(item, dict):
                    continue
                item_id = item.get("id") or f"第{index + 1}项"
                errors.extend(
                    self._check_assert_list(item.get("assert"), f"{label}「{item_id}」")
                )

        return errors

    def _check_assert_list(
        self, asserts: Any, where: str
    ) -> list[str]:
        """检查一组断言。"""
        if asserts is None:
            return []
        if not isinstance(asserts, list):
            return [f"{where}的 assert 必须写成列表"]

        errors: list[str] = []
        for index, assertion in enumerate(asserts):
            if not isinstance(assertion, dict):
                errors.append(f"{where}的第{index + 1}条断言必须是字典")
                continue

            assert_type = assertion.get("type")
            if not assert_type:
                errors.append(f"{where}的第{index + 1}条断言缺少 type")
                continue

            # 类型不认识 —— 多半是拼错了
            if not assert_types.is_known(assert_type):
                suggestion = assert_types.suggest(assert_type)
                if suggestion:
                    errors.append(
                        f"{where}的第{index + 1}条断言类型「{assert_type}」不认识，"
                        f"是不是想写「{suggestion}」？"
                    )
                else:
                    errors.append(
                        f"{where}的第{index + 1}条断言类型「{assert_type}」不认识"
                    )
                continue

            # 必填字段缺失
            for required in assert_types.required_fields(assert_type):
                if assertion.get(required) is None:
                    errors.append(
                        f"{where}的第{index + 1}条断言（{assert_type}）缺少 {required}"
                    )

            # 需要 field 的断言类型
            if assert_types.needs_field(assert_type) and not assertion.get("field"):
                errors.append(
                    f"{where}的第{index + 1}条断言（{assert_type}）缺少 field"
                )

        return errors

    def is_valid(self, case_data: Any) -> bool:
        """只问一句合不合法。"""
        return not self.validate(case_data)
