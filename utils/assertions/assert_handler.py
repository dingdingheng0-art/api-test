"""断言处理。

断言分三大类：

**① 对响应体断言**（主要用这个）—— 在 YAML 里这样写：

.. code-block:: yaml

    assert:
      - type: equal
        field: code
        expected: 200
      - type: is not None
        field: result.accessToken
      - type: in
        field: result.status
        container: [1, 2, 3]
      - type: contains
        field: message
        expected: 成功

``field`` 支持多层路径和数组下标：``data[0].areaName.ids[1].id``

**② 对响应元信息断言** —— 状态码 / 响应头 / 耗时：

.. code-block:: yaml

    assert:
      - type: status_code
        expected: 200
      - type: response_header
        field: Content-Type
        expected: application/json
      - type: elapsed_time
        expected: 3000

也可以用 ``$`` 前缀的 field 写法（更短）::

    assert:
      - type: equal
        field: $status          # 等价于 status_code
        expected: 200
      - type: contains
        field: $header.Set-Cookie
        expected: session

**③ 对数据库断言** —— 需要 ``mysql`` 配置，断言类型以 ``mysql_`` 开头：

.. code-block:: yaml

    assert:
      - type: mysql_query
        query: SELECT status FROM orders WHERE id = 123
        expected: 2

**④ 对缓存 / 消息队列断言** —— 由 ``verify/`` 层执行：

.. code-block:: yaml

    assert:
      - type: redis_get              # 缓存里的值对不对
        key: "order:info:{{order_id}}"
        expected: '{"status":2}'
      - type: redis_exists           # 缓存有没有写进去
        key: "order:info:{{order_id}}"
        expected: true
      - type: redis_ttl              # 缓存过期时间
        key: "order:info:{{order_id}}"
        expected: {lte: 3600}
      - type: redis_deleted          # 缓存有没有失效
        key: "order:info:{{order_id}}"
        expected: true
      - type: mq_received            # 消息有没有正确发出来
        queue: order.created
        payload_contains: {orderNo: "{{order_no}}"}
        timeout: 10
        expected: true
"""
from __future__ import annotations

import re
from typing import Any, Callable

from utils.logs.log_manager import log

# field 路径解析：``a.b[0].c`` 和 ``a.b.0.c`` 都支持
_FIELD_RE = re.compile(r"([a-zA-Z_]\w*)\[(\d+)\]|(\d+)|([a-zA-Z_]\w*)")

# 状态码断言：允许比较大小
_STATUS_TYPES = ("status_code", "status", "http_status")
# 响应头断言
_HEADER_TYPES = ("response_header", "header")
# 耗时断言（毫秒）
_ELAPSED_TYPES = ("elapsed_time", "elapsed", "response_time")


class AssertHandler:
    """执行 YAML 里定义的断言。"""

    def __init__(self) -> None:
        # 最近一次执行收集到的"校验证据"（db/redis/mq 查到了什么）
        self.evidence: list[dict[str, Any]] = []
        #: 证据的落地方式（由调用方注入，比如贴进 allure 报告）。
        #: 为空就不落地 —— 断言逻辑本身不关心报告长什么样。
        self.evidence_sink: Callable[[list[dict[str, Any]]], None] | None = None
        #: 把单条断言包成报告步骤的工厂（注入 ``allure.step``）。
        #: 签名：``(title) -> 上下文管理器``。为空就退化成"只执行、不记录"。
        self.step_sink: Callable[[str], Any] | None = None
        #: 把断言明细贴进报告（注入 ``attach_json``）。只在失败时用。
        self.attach_sink: Callable[..., None] | None = None

    def handle_assertion(
        self,
        asserts: list[dict[str, Any]] | None,
        response: Any,
        db_config: dict[str, Any] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> None:
        """执行一组断言。

        Args:
            asserts: 断言列表
            response: 响应体（JSON 解析后的 dict / list）
            db_config: mysql 连接配置（用老的 ``mysql_*`` 断言时才需要）
            meta: 响应元信息 —— ``{"status_code": 200, "headers": {...}, "elapsed_ms": 12}``
        """
        if not asserts:
            return

        log.info("执行断言 %s", asserts)

        # 证据快照：db / redis / mq 校验会把"到底查到了什么"记下来，
        # 跑完贴进 allure 报告，失败时不用再去翻数据库
        self.evidence = []

        need_legacy_db = any(
            str(a.get("type", "")).startswith("mysql") for a in asserts
        )
        connection = None
        try:
            if need_legacy_db:
                if not db_config:
                    raise ValueError("用例里有 mysql 相关断言，但配置中缺少 mysql 连接信息")
                import mysql.connector  # 延迟导入：没装 mysql 驱动也不影响其他用例

                connection = mysql.connector.connect(**db_config)

            for assertion in asserts:
                self._handle_one(assertion, response, connection, meta)
        finally:
            if connection is not None:
                connection.close()

        # 证据贴进 allure —— 通过回调注入，不直接 import 上层框架
        # （utils/ 不能反向依赖 framework/，见 docs/ARCHITECTURE.md 第五节）
        if self.evidence and self.evidence_sink is not None:
            self.evidence_sink(self.evidence)

        log.info("断言全部通过")

    # ---------------- 单条断言 ----------------

    def _handle_one(
        self,
        assertion: dict[str, Any],
        response: Any,
        connection: Any,
        meta: dict[str, Any] | None = None,
    ) -> None:
        """执行单条断言；注入了 ``step_sink`` 时把它包成报告步骤。

        以前这里直接执行断言，报告里只有一个空的「校验结果」步骤 ——
        跑了哪些断言、期望什么，看报告的人一概不知道。现在每条断言
        都会成为「校验结果」下的一个子步骤，失败时再把断言定义和
        错误信息贴成附件。
        """
        if self.step_sink is None:
            self._evaluate(assertion, response, connection, meta)
            return

        label = self._describe(assertion)
        with self.step_sink(label):
            try:
                self._evaluate(assertion, response, connection, meta)
            except BaseException as exc:
                if self.attach_sink is not None:
                    self.attach_sink(
                        {"断言": assertion, "错误": str(exc)},
                        name=f"{label}-失败详情",
                    )
                raise

    @staticmethod
    def _describe(assertion: dict[str, Any]) -> str:
        """把一条断言写成报告里一眼能看懂的一句话。

        例：``code == '00000'``、``数据存在: SELECT id FROM tz_notice ...``。
        """
        raw_type = str(assertion.get("type") or "?").strip()
        norm_type = raw_type.lower().replace(" ", "_")
        field = assertion.get("field")
        expected = assertion.get("expected")

        # 数据库 / 缓存 / 消息队列：把目标也带上，不然看不出在查什么
        if raw_type.startswith(("mysql", "db_")):
            sql = str(assertion.get("query") or "").strip()
            suffix = "" if expected is None else f" -> {expected!r}"
            text = f"数据库断言 {raw_type}: {sql}{suffix}"
        elif norm_type.startswith("redis_"):
            text = f"缓存断言 {raw_type}: {assertion.get('key')}"
            if expected is not None:
                text += f" -> {expected!r}"
        elif norm_type.startswith("mq_"):
            text = f"消息断言 {raw_type}: {assertion.get('queue')}"
            if expected is not None:
                text += f" -> {expected!r}"
        elif norm_type in ("exists", "field_exists") and field:
            text = f"{field} 存在"
        elif norm_type in ("not_exists", "field_not_exists") and field:
            text = f"{field} 不存在"
        elif field:
            op = {
                "equal": "==", "equals": "==",
                "not_equal": "!=", "notequal": "!=",
                "status_code": "状态码 ==", "status": "状态码 ==",
                "http_status": "状态码 ==",
            }.get(norm_type, norm_type)
            text = f"{field} {op} {expected!r}"
        else:
            text = raw_type if expected is None else f"{raw_type} {expected!r}"

        # 步骤标题过长会在报告里折成好几行，截一下
        return text if len(text) <= 160 else text[:157] + "..."

    def _evaluate(
        self,
        assertion: dict[str, Any],
        response: Any,
        connection: Any,
        meta: dict[str, Any] | None = None,
    ) -> None:
        assert_type = str(assertion.get("type", "")).strip()
        field = assertion.get("field")
        expected = assertion.get("expected")
        container = assertion.get("container")
        query = assertion.get("query")

        if assert_type.startswith("mysql"):
            self._handle_mysql(assertion, connection)
            return

        norm_type = assert_type.lower().replace(" ", "_")

        # ---- 缓存 / 消息队列 / 新版数据库断言（走 verify/ 层）----
        if norm_type.startswith(("redis_", "mq_", "db_")):
            self._handle_verify(assertion, norm_type, expected)
            return

        # ---- 响应元信息类断言 ----
        if norm_type in _STATUS_TYPES:
            self._handle_status(norm_type, expected, meta)
            return

        if norm_type in _HEADER_TYPES:
            self._handle_header(assertion, expected, meta)
            return

        if norm_type in _ELAPSED_TYPES:
            self._handle_elapsed(norm_type, expected, meta)
            return

        if norm_type in ("full_body", "body_equal"):
            # 整个响应体比较 —— 不需要 field
            self._handle_full_body(assertion, response)
            return

        if not field:
            raise ValueError(f"断言缺少 field: {assertion}")

        # ``$status`` / ``$header.X`` / ``$elapsed`` —— 取元信息而不是响应体
        field = str(field)
        if field.startswith("$"):
            actual = self._get_meta_value(field, meta)
        else:
            actual = self.get_field_value(response, field)

        # ---- 需要"字段是否存在"的类型，单独处理 ----
        #
        # ``$status`` / ``$header.X`` 这类元信息字段不走响应体路径，
        # 取到值就算存在（以前会拿 "$header.X" 去响应体里找，永远找不到）。
        is_meta_field = str(field).startswith("$")
        if norm_type in ("exists", "field_exists"):
            exists = actual is not None if is_meta_field else self.field_exists(response, field)
            hint = "" if is_meta_field else f"（{self._missing_field_hint(response, field)}）"
            assert exists, f"[{field}] 字段不存在{hint}"
            return

        if norm_type in ("not_exists", "field_not_exists"):
            exists = actual is not None if is_meta_field else self.field_exists(response, field)
            assert not exists, f"[{field}] 字段应该不存在，实际存在（值 {actual!r}）"
            return

        if norm_type == "subset":
            self._handle_subset(field, expected, response)
            return

        # ---- 值比较类 ----
        if norm_type in ("equal", "equals", "=="):
            assert actual == expected, f"[{field}] 期望等于 {expected!r}，实际 {actual!r}"
        elif norm_type in ("not_equal", "notequal", "!="):
            assert actual != expected, f"[{field}] 期望不等于 {expected!r}，实际 {actual!r}"
        elif norm_type in ("iequal", "equal_ignore_case", "equals_ignore_case"):
            assert str(actual).lower() == str(expected).lower(), (
                f"[{field}] 期望等于（忽略大小写）{expected!r}，实际 {actual!r}"
            )
        elif norm_type in ("inequal", "not_equal_ignore_case"):
            assert str(actual).lower() != str(expected).lower(), (
                f"[{field}] 期望不等于（忽略大小写）{expected!r}，实际 {actual!r}"
            )
        elif norm_type in ("is_none", "none"):
            assert actual is None, f"[{field}] 期望是 None，实际 {actual!r}"
        elif norm_type in ("is_not_none", "not_none"):
            assert actual is not None, f"[{field}] 期望不是 None"
        elif norm_type in ("is_true", "true"):
            assert actual is True, f"[{field}] 期望是 True，实际 {actual!r}"
        elif norm_type in ("is_false", "false"):
            assert actual is False, f"[{field}] 期望是 False，实际 {actual!r}"
        elif norm_type in ("empty", "is_empty"):
            assert self._is_empty(actual), f"[{field}] 期望是空值，实际 {actual!r}"
        elif norm_type in ("not_empty", "is_not_empty"):
            assert not self._is_empty(actual), f"[{field}] 期望不是空值，实际 {actual!r}"
        elif norm_type in ("contains",):
            assert actual is not None and expected in actual, f"[{field}] 期望包含 {expected!r}，实际 {actual!r}"
        elif norm_type in ("not_contains",):
            assert actual is None or expected not in actual, f"[{field}] 期望不包含 {expected!r}，实际 {actual!r}"
        elif norm_type in ("matches", "regex", "match"):
            self._handle_matches(field, actual, expected)
        elif norm_type in ("not_matches", "not_regex"):
            self._handle_matches(field, actual, expected, negate=True)
        elif norm_type in ("starts_with", "startswith"):
            assert actual is not None and str(actual).startswith(str(expected)), (
                f"[{field}] 期望以 {expected!r} 开头，实际 {actual!r}"
            )
        elif norm_type in ("ends_with", "endswith"):
            assert actual is not None and str(actual).endswith(str(expected)), (
                f"[{field}] 期望以 {expected!r} 结尾，实际 {actual!r}"
            )
        elif norm_type in ("in",):
            if container is None:
                raise ValueError(f"断言 {assertion} 缺少 container")
            assert actual in container, f"[{field}] 期望在 {container!r} 中，实际 {actual!r}"
        elif norm_type in ("not_in",):
            if container is None:
                raise ValueError(f"断言 {assertion} 缺少 container")
            assert actual not in container, f"[{field}] 期望不在 {container!r} 中，实际 {actual!r}"
        elif norm_type in ("gt", "greater_than"):
            assert actual > expected, f"[{field}] 期望大于 {expected!r}，实际 {actual!r}"
        elif norm_type in ("gte", "greater_than_or_equal"):
            assert actual >= expected, f"[{field}] 期望大于等于 {expected!r}，实际 {actual!r}"
        elif norm_type in ("lt", "less_than"):
            assert actual < expected, f"[{field}] 期望小于 {expected!r}，实际 {actual!r}"
        elif norm_type in ("lte", "less_than_or_equal"):
            assert actual <= expected, f"[{field}] 期望小于等于 {expected!r}，实际 {actual!r}"
        elif norm_type in ("length", "len"):
            assert len(actual) == expected, f"[{field}] 期望长度 {expected!r}，实际 {len(actual) if actual is not None else None}"
        elif norm_type in ("length_ne", "length_not_equal"):
            assert len(actual) != expected, f"[{field}] 期望长度不等于 {expected!r}，实际 {len(actual) if actual is not None else None}"
        elif norm_type in ("length_gt",):
            assert len(actual) > expected, f"[{field}] 期望长度大于 {expected!r}，实际 {len(actual) if actual is not None else None}"
        elif norm_type in ("length_gte",):
            assert len(actual) >= expected, f"[{field}] 期望长度大于等于 {expected!r}，实际 {len(actual) if actual is not None else None}"
        elif norm_type in ("length_lt",):
            assert len(actual) < expected, f"[{field}] 期望长度小于 {expected!r}，实际 {len(actual) if actual is not None else None}"
        elif norm_type in ("length_lte",):
            assert len(actual) <= expected, f"[{field}] 期望长度小于等于 {expected!r}，实际 {len(actual) if actual is not None else None}"
        elif norm_type in ("full_body", "body_equal"):
            # 整个响应体比较（扁平格式的 full_body 断言）
            self._handle_full_body(assertion, response)
        elif norm_type in ("type", "is_type"):
            assert isinstance(actual, expected), f"[{field}] 期望类型 {expected!r}，实际 {type(actual).__name__}"
        elif norm_type in ("approx_equal", "approximately_equal", "almost_equal"):
            self._handle_approx_equal(assertion, field, actual, expected)
        elif norm_type in ("jwt_claim", "jwt"):
            # field 指向的是 token 本身（比如 result.accessToken）
            # expected 是期望的 claim 值；claim 名写在 claim 里（默认 sub）
            claim = str(assertion.get("claim") or "sub")
            payload = self._decode_jwt(actual)
            assert claim in payload, f"JWT 里没有 {claim!r} 这个字段，实际字段: {list(payload)}"
            actual_claim = payload[claim]
            assert actual_claim == expected, (
                f"JWT.{claim} 期望等于 {expected!r}，实际 {actual_claim!r}"
            )
        else:
            raise ValueError(f"未知的断言类型: {assert_type}")

    # ---------------- 响应元信息断言 ----------------

    def _handle_status(self, norm_type: str, expected: Any, meta: dict[str, Any] | None) -> None:
        """HTTP 状态码断言。"""
        if not meta or meta.get("status_code") is None:
            raise ValueError("没有拿到响应状态码 —— 这个断言只能用在 HTTP 步骤上")

        actual = meta["status_code"]
        if norm_type in ("status", "http_status") or expected is None:
            assert actual == expected, f"[状态码] 期望 {expected!r}，实际 {actual!r}"
        elif norm_type == "status_code":
            # 允许用 gt / lt 这类的期望值形式
            self._compare_numeric("状态码", actual, expected)

    def _handle_header(self, assertion: dict[str, Any], expected: Any, meta: dict[str, Any] | None) -> None:
        """响应头断言。"""
        name = assertion.get("field") or assertion.get("header")
        if not name:
            raise ValueError(f"响应头断言缺少 field（要写响应头的名字）: {assertion}")
        if not meta or not meta.get("headers"):
            raise ValueError("没有拿到响应头 —— 这个断言只能用在 HTTP 步骤上")

        headers = meta["headers"]
        key = str(name).strip().lower()
        actual = headers.get(key)
        assert actual is not None, f"[响应头 {name}] 不存在，实际有的头: {sorted(headers)}"
        assert actual == expected, f"[响应头 {name}] 期望 {expected!r}，实际 {actual!r}"

    def _handle_elapsed(self, norm_type: str, expected: Any, meta: dict[str, Any] | None) -> None:
        """响应耗时断言（毫秒）。"""
        if not meta or meta.get("elapsed_ms") is None:
            raise ValueError("没有拿到响应耗时 —— 这个断言只能用在 HTTP 步骤上")

        actual = meta["elapsed_ms"]
        self._compare_numeric("耗时(ms)", actual, expected)

    def _get_meta_value(self, field: str, meta: dict[str, Any] | None) -> Any:
        """``$status`` / ``$header.X`` / ``$elapsed`` —— 取响应元信息。"""
        meta = meta or {}
        if field == "$status":
            return meta.get("status_code")
        if field == "$elapsed":
            return meta.get("elapsed_ms")
        if field.startswith("$header."):
            headers = meta.get("headers") or {}
            return headers.get(field[len("$header."):].strip().lower())
        if field.startswith("$"):
            raise ValueError(f"不认识的元信息字段: {field}")
        return None

    @staticmethod
    def _compare_numeric(label: str, actual: Any, expected: Any) -> None:
        """数值比较 —— 期望值可以是数字，也可以是 ``{"gt": 100}`` 这种写法。"""
        if isinstance(expected, dict):
            for op, want in expected.items():
                norm_op = str(op).lower().replace(" ", "_")
                if norm_op in ("gt", "greater_than"):
                    assert actual > want, f"[{label}] 期望大于 {want!r}，实际 {actual!r}"
                elif norm_op in ("gte", "greater_than_or_equal"):
                    assert actual >= want, f"[{label}] 期望大于等于 {want!r}，实际 {actual!r}"
                elif norm_op in ("lt", "less_than"):
                    assert actual < want, f"[{label}] 期望小于 {want!r}，实际 {actual!r}"
                elif norm_op in ("lte", "less_than_or_equal"):
                    assert actual <= want, f"[{label}] 期望小于等于 {want!r}，实际 {actual!r}"
                elif norm_op in ("eq", "equal", "equals", "=="):
                    assert actual == want, f"[{label}] 期望等于 {want!r}，实际 {actual!r}"
                else:
                    raise ValueError(f"不认识的比较方式: {op!r}")
            return

        assert actual == expected, f"[{label}] 期望 {expected!r}，实际 {actual!r}"

    # ---------------- 特殊值比较 ----------------

    @staticmethod
    def _is_empty(value: Any) -> bool:
        """是不是空值 —— None / 空串 / 空白 / 空列表 / 空字典。"""
        if value is None:
            return True
        if isinstance(value, str):
            return value.strip() == ""
        if isinstance(value, (list, tuple, dict, set)):
            return len(value) == 0
        return False

    @staticmethod
    def _handle_matches(field: str, actual: Any, expected: Any, negate: bool = False) -> None:
        """正则匹配断言。"""
        if actual is None:
            raise AssertionError(f"[{field}] 实际值是 None，正则匹配不了")
        pattern = str(expected)
        try:
            matched = re.search(pattern, str(actual)) is not None
        except re.error as exc:
            raise ValueError(f"[{field}] 正则表达式不合法: {pattern!r} -> {exc}") from exc

        if negate:
            assert not matched, f"[{field}] 期望不匹配 {pattern!r}，实际 {actual!r}"
        else:
            assert matched, f"[{field}] 期望匹配 {pattern!r}，实际 {actual!r}"

    @staticmethod
    def _handle_approx_equal(assertion: dict[str, Any], field: str, actual: Any, expected: Any) -> None:
        """浮点近似相等（金额校验用）。

        两种写法：

        - ``expected: 10.0`` + ``tolerance: 0.01``
        - ``expected: {"value": 10.0, "tolerance": 0.01}``
        """
        tolerance = assertion.get("tolerance")
        target = expected

        if isinstance(expected, dict):
            target = expected.get("value")
            tolerance = expected.get("tolerance", tolerance)

        if tolerance is None:
            tolerance = 0.01
        tolerance = float(tolerance)

        assert actual is not None, f"[{field}] 实际值是 None"
        try:
            diff = abs(float(actual) - float(target))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"[{field}] 近似相等要求两边都是数字，实际 {actual!r} / {target!r}") from exc

        assert diff <= tolerance, (
            f"[{field}] 期望约等于 {target!r}（误差 ±{tolerance}），实际 {actual!r}（差 {diff}）"
        )

    def _handle_full_body(self, assertion: dict[str, Any], response: Any) -> None:
        """整个响应体比较（扁平格式的 ``full_body`` 断言）。"""
        expected = assertion.get("expected")
        comparator = str(assertion.get("comparator") or "==").strip()
        if comparator in ("!=", "not equal"):
            assert response != expected, f"[整个响应体] 期望不等于 {expected!r}，实际 {response!r}"
        else:
            assert response == expected, f"[整个响应体] 期望等于 {expected!r}，实际 {response!r}"

    def _handle_subset(self, field: str, expected: Any, response: Any) -> None:
        """部分 JSON 匹配 —— 只比关心的字段，服务端多返回的字段忽略。

        ``expected`` 是要检查的子集：

        .. code-block:: yaml

            assert:
              - type: subset
                field: result
                expected:
                  status: 1
                  user: {name: 张三}
        """
        if not isinstance(expected, dict):
            raise ValueError(f"[{field}] subset 断言的 expected 必须是字典")

        target = response
        if field:
            target = self.get_field_value(response, field)

        if not isinstance(target, dict):
            raise AssertionError(f"[{field}] 实际值不是字典，无法做部分匹配：{target!r}")

        self._assert_subset(f"[{field}]", expected, target)

    def _assert_subset(self, label: str, expected: dict[str, Any], actual: dict[str, Any]) -> None:
        """递归检查子集。"""
        for key, want in expected.items():
            if key not in actual:
                raise AssertionError(f"{label} 缺少字段 {key!r}，实际字段: {sorted(actual)}")
            got = actual[key]

            if isinstance(want, dict) and isinstance(got, dict):
                self._assert_subset(f"{label}.{key}", want, got)
            elif want != got:
                raise AssertionError(f"{label}.{key} 期望 {want!r}，实际 {got!r}")

    # ---------------- 取值 ----------------

    @staticmethod
    def get_field_value(response: Any, field_path: str) -> Any:
        """按路径取值，支持 ``a.b[0].c`` 和 ``a.b.0.c``；路径不存在返回 None。

        ``a.b.0.c`` 里的数字段会被当成数组下标 —— 写起来比 ``a.b[0].c`` 顺眼。
        """
        value = response
        for match in _FIELD_RE.finditer(field_path):
            field, index, numeric, simple = match.groups()
            if field and index is not None:
                value = value.get(field) if isinstance(value, dict) else None
                if isinstance(value, list) and len(value) > int(index):
                    value = value[int(index)]
                else:
                    value = None
            elif numeric is not None:
                # 纯数字段 -> 数组下标（``a.0.b`` 等价于 ``a[0].b``）
                if isinstance(value, list) and len(value) > int(numeric):
                    value = value[int(numeric)]
                else:
                    value = None
            elif simple:
                value = value.get(simple) if isinstance(value, dict) else None
            if value is None:
                return None
        return value

    @classmethod
    def field_exists(cls, response: Any, field_path: str) -> bool:
        """字段是否存在（和「值为 None」区分开）。

        ``{"a": null}`` 里 ``a`` 是**存在**的；``{}`` 里 ``a`` 是**不存在**的。
        """
        if not field_path:
            return response is not None

        value = response
        for match in _FIELD_RE.finditer(field_path):
            field, index, numeric, simple = match.groups()
            if field and index is not None:
                if not isinstance(value, dict) or field not in value:
                    return False
                value = value[field]
                if isinstance(value, list):
                    if len(value) <= int(index):
                        return False
                    value = value[int(index)]
                else:
                    return False
            elif numeric is not None:
                # 纯数字段 -> 数组下标
                if not isinstance(value, list) or len(value) <= int(numeric):
                    return False
                value = value[int(numeric)]
            elif simple:
                if not isinstance(value, dict) or simple not in value:
                    return False
                value = value[simple]
        return True

    @classmethod
    def _missing_field_hint(cls, response: Any, field_path: str) -> str:
        """字段不存在时，说清楚是**哪一层**断的。

        只说"字段不存在"最容易被误判成"服务端没返回"，
        实际经常是路径写错了（多了一层 ``data``、数组下标写错）。
        这里把能走到的最深一层和它当时有哪些字段列出来 —— 一眼看出
        该改路径还是该找后端。
        """
        if not field_path:
            return "响应体本身是 None"

        value = response
        parents: list[str] = []  # 已经走过的段，用来拼"在哪一层断的"

        def _level() -> str:
            return ".".join(parents) or "响应根节点"

        for match in _FIELD_RE.finditer(field_path):
            field, index, numeric, simple = match.groups()
            segment = match.group(0)

            if field and index is not None:
                if not isinstance(value, dict):
                    return f"{_level()} 是 {type(value).__name__}，取不到 {field}"
                if field not in value:
                    return f"{_level()} 上没有 {field}，实际有: {list(value)}"
                value = value[field]
                if not isinstance(value, list):
                    return f"{'.'.join(parents + [field])} 是 {type(value).__name__}，不是数组"
                if len(value) <= int(index):
                    return f"{'.'.join(parents + [field])} 只有 {len(value)} 个元素，取不到下标 {index}"
                value = value[int(index)]
            elif numeric is not None:
                if not isinstance(value, list):
                    return f"{_level()} 是 {type(value).__name__}，不是数组"
                if len(value) <= int(numeric):
                    return f"{_level()} 只有 {len(value)} 个元素，取不到下标 {numeric}"
                value = value[int(numeric)]
            elif simple:
                if not isinstance(value, dict):
                    return f"{_level()} 是 {type(value).__name__}，取不到 {simple}"
                if simple not in value:
                    return f"{_level()} 上没有 {simple}，实际有: {list(value)}"
                value = value[simple]

            parents.append(segment)

        return "字段路径为空"

    @staticmethod
    def _decode_jwt(token: Any) -> dict[str, Any]:
        """解 JWT 的 payload 段。"""
        import base64
        import json

        if not token or not isinstance(token, str) or "." not in token:
            raise ValueError(f"不是 JWT（没有 '.' 分段）：{token!r}")

        payload_part = token.split(".")[1]
        payload_part += "=" * (4 - len(payload_part) % 4)
        return json.loads(base64.urlsafe_b64decode(payload_part))

    # ---------------- 新版校验断言（db_ / redis_ / mq_）----------------

    def _handle_verify(
        self,
        assertion: dict[str, Any],
        norm_type: str,
        expected: Any,
    ) -> None:
        """执行 db / redis / mq 断言（走 ``verify/`` 层）。

        和老的 ``mysql_*`` 断言并存 —— 老的保持原样不动，
        新的走 ``verify/`` 层（支持别名、多套配置、证据快照）。
        """
        from verify.manager import VerifyManager

        manager = VerifyManager.get()

        if norm_type.startswith("db_"):
            self._handle_db_assert(manager, assertion, norm_type, expected)
            return

        if norm_type.startswith("redis_"):
            self._handle_redis_assert(manager, assertion, norm_type, expected)
            return

        if norm_type.startswith("mq_"):
            self._handle_mq_assert(manager, assertion, norm_type, expected)
            return

        raise ValueError(f"未知的断言类型: {assertion.get('type')}")

    # ---- 数据库 ----

    def _handle_db_assert(
        self,
        manager: Any,
        assertion: dict[str, Any],
        norm_type: str,
        expected: Any,
    ) -> None:
        """数据库断言（新版）。

        支持的类型：

        - ``db_query``        —— 第一列（或 ``column`` 指定的列）等于期望值
        - ``db_query_exists`` —— 有结果
        - ``db_query_not_exists`` —— 没有结果（删数据后确认真的删掉了）
        - ``db_query_true``   —— 第一列是真值
        - ``db_query_count``  —— 行数等于期望值
        - ``db_query_gt`` / ``db_query_gte`` / ``db_query_lt`` / ``db_query_lte``
          —— 数值比较
        - ``db_query_in``     —— 第一列在给定集合里
        """
        query = assertion.get("query")
        if not query:
            raise ValueError(f"数据库断言 {norm_type} 缺少 query")

        alias = str(assertion.get("alias") or "default")
        client = manager.db(alias)

        column = assertion.get("column")
        params = assertion.get("params")

        if norm_type == "db_query_count":
            rows = client.query(query, params)
            count = len(rows)
            self.evidence.append(
                {"kind": "db", "alias": alias, "sql": query, "row_count": count}
            )
            assert count == expected, (
                f"SQL {query} 期望 {expected!r} 行，实际 {count!r} 行"
            )
            return

        # ``query_one`` 本身就是 ``query(...)[0]`` ——
        # 以前 db_query_exists 为了拿 evidence 又完整查了一遍，
        # 同一条 SQL 跑两次（带 join / 聚合时代价翻倍）。
        # 这里一次查完，行和 evidence 都用同一份结果。
        rows = client.query(query, params) if norm_type == "db_query_exists" else None
        row = rows[0] if rows is not None else client.query_one(query, params)
        result = self._pick_verify_column(row, column)

        self.evidence.append(
            {
                "kind": "db",
                "alias": alias,
                "sql": query,
                "rows": rows,
                "actual": result,
            }
        )

        if norm_type == "db_query":
            assert result == expected, f"SQL {query} 期望 {expected!r}，实际 {result!r}"
        elif norm_type == "db_query_exists":
            assert row is not None, f"SQL {query} 期望有结果，实际没有"
        elif norm_type == "db_query_not_exists":
            assert row is None, f"SQL {query} 期望没有结果，实际查到 {result!r}"
        elif norm_type == "db_query_true":
            assert bool(result), f"SQL {query} 期望为真，实际 {result!r}"
        elif norm_type == "db_query_gt":
            assert result is not None and result > expected, (
                f"SQL {query} 期望大于 {expected!r}，实际 {result!r}"
            )
        elif norm_type == "db_query_gte":
            assert result is not None and result >= expected, (
                f"SQL {query} 期望大于等于 {expected!r}，实际 {result!r}"
            )
        elif norm_type == "db_query_lt":
            assert result is not None and result < expected, (
                f"SQL {query} 期望小于 {expected!r}，实际 {result!r}"
            )
        elif norm_type == "db_query_lte":
            assert result is not None and result <= expected, (
                f"SQL {query} 期望小于等于 {expected!r}，实际 {result!r}"
            )
        elif norm_type == "db_query_in":
            container = assertion.get("container")
            if container is None:
                raise ValueError(f"数据库断言 {assertion} 缺少 container")
            assert result in container, (
                f"SQL {query} 期望在 {container!r} 中，实际 {result!r}"
            )
        else:
            raise ValueError(f"未知的数据库断言类型: {norm_type}")

    # ---- 缓存 ----

    def _handle_redis_assert(
        self,
        manager: Any,
        assertion: dict[str, Any],
        norm_type: str,
        expected: Any,
    ) -> None:
        """缓存断言。

        支持的类型：

        - ``redis_get``     —— 缓存里的值等于期望值
        - ``redis_exists``  —— key 存在（``expected: true``）/ 不存在（``false``）
        - ``redis_ttl``     —— 剩余存活秒数（可用 ``{"lte": 3600}`` 这种写法）
        - ``redis_deleted`` —— key 已被删掉
        """
        key = assertion.get("key")
        if not key:
            raise ValueError(f"缓存断言 {norm_type} 缺少 key")

        alias = str(assertion.get("alias") or "default")
        client = manager.redis(alias)

        if norm_type == "redis_get":
            raw = client.get(key)
            actual = client.normalize(raw)
            self.evidence.append(
                {"kind": "redis", "alias": alias, "key": key, "actual": actual}
            )
            expected_value = client.normalize(expected)
            assert actual == expected_value, (
                f"[redis {key}] 期望 {expected_value!r}，实际 {actual!r}"
            )
            return

        if norm_type == "redis_exists":
            actual = client.exists(key)
            self.evidence.append(
                {"kind": "redis", "alias": alias, "key": key, "exists": actual}
            )
            assert actual == bool(expected), (
                f"[redis {key}] 期望存在={bool(expected)!r}，实际 {actual!r}"
            )
            return

        if norm_type == "redis_deleted":
            actual = not client.exists(key)
            self.evidence.append(
                {"kind": "redis", "alias": alias, "key": key, "deleted": actual}
            )
            assert actual == bool(expected), (
                f"[redis {key}] 期望已删除={bool(expected)!r}，实际 {actual!r}"
            )
            return

        if norm_type == "redis_ttl":
            actual = client.ttl(key)
            self.evidence.append(
                {"kind": "redis", "alias": alias, "key": key, "ttl": actual}
            )
            self._compare_numeric(f"redis TTL({key})", actual, expected)
            return

        raise ValueError(f"未知的缓存断言类型: {norm_type}")

    # ---- 消息队列 ----

    def _handle_mq_assert(
        self,
        manager: Any,
        assertion: dict[str, Any],
        norm_type: str,
        expected: Any,
    ) -> None:
        """消息队列断言。

        支持的类型：

        - ``mq_received``  —— 收到了匹配的消息
        - ``mq_count``     —— 收到的匹配消息条数等于期望值
        - ``mq_payload``   —— 消息体里某个字段等于期望值
        """
        from verify.mq import matches

        alias = str(assertion.get("alias") or "default")
        queue = assertion.get("queue")
        if not queue:
            raise ValueError(f"消息断言 {norm_type} 缺少 queue")

        routing_key = str(assertion.get("routing_key") or "")
        payload_contains = assertion.get("payload_contains")
        timeout = float(assertion.get("timeout") or 10)

        backend = manager.mq(alias)
        # 先订阅，再等消息（避免漏掉刚好在这期间发出来的）
        manager.ensure_subscribed(queue, alias)

        messages = backend.poll(timeout)
        matched = [
            m for m in messages
            if matches(m, routing_key=routing_key, payload_contains=payload_contains)
        ]

        self.evidence.append(
            {
                "kind": "mq",
                "alias": alias,
                "queue": queue,
                "routing_key": routing_key,
                "received_count": len(messages),
                "matched_count": len(matched),
                "messages": [
                    {"routing_key": m.routing_key, "payload": m.payload}
                    for m in messages[:10]
                ],
            }
        )

        if norm_type == "mq_received":
            assert bool(matched) == bool(expected), (
                f"[MQ {queue}] 期望收到消息={bool(expected)!r}，"
                f"实际收到 {len(matched)} 条（共收到 {len(messages)} 条）"
            )
            return

        if norm_type == "mq_count":
            assert len(matched) == expected, (
                f"[MQ {queue}] 期望收到 {expected!r} 条消息，实际 {len(matched)} 条"
            )
            return

        if norm_type == "mq_payload":
            if not matched:
                raise AssertionError(
                    f"[MQ {queue}] 没收到匹配的消息（期望 payload 包含 {payload_contains!r}）"
                )
            field = assertion.get("field")
            if not field:
                raise ValueError(f"mq_payload 断言缺少 field")
            actual = self.get_field_value(matched[0].payload, field)
            assert actual == expected, (
                f"[MQ {queue}] payload.{field} 期望 {expected!r}，实际 {actual!r}"
            )
            return

        raise ValueError(f"未知的消息断言类型: {norm_type}")

    @staticmethod
    def _pick_verify_column(row: Any, column: Any) -> Any:
        """从一行查询结果里取某一列（新版数据库断言用）。"""
        if row is None:
            return None
        if column is None:
            values = list(row.values())
            return values[0] if values else None
        if isinstance(column, int) or (isinstance(column, str) and column.isdigit()):
            values = list(row.values())
            index = int(column)
            return values[index] if index < len(values) else None
        return row.get(str(column))

    # ---------------- 数据库断言 ----------------

    def _handle_mysql(self, assertion: dict[str, Any], connection: Any) -> None:
        """执行数据库断言。

        支持的类型：

        - ``mysql_query`` —— 第一列（或 ``column`` 指定的列）等于期望值
        - ``mysql_query_exists`` —— 有结果
        - ``mysql_query_not_exists`` —— 没有结果
        - ``mysql_query_true`` —— 第一列是真值
        - ``mysql_query_count`` —— 行数等于期望值
        - ``mysql_query_gte`` / ``mysql_query_lte`` —— 数值比较
        - ``mysql_query_in`` —— 第一列在给定集合里
        """
        assert_type = str(assertion.get("type", "")).strip()
        query = assertion.get("query")
        expected = assertion.get("expected")
        container = assertion.get("container")
        column = assertion.get("column")

        if not query:
            raise ValueError(f"数据库断言 {assert_type} 缺少 query")
        if connection is None:
            raise ValueError(f"数据库断言 {assert_type} 需要 mysql 连接")

        cursor = connection.cursor()
        try:
            cursor.execute(query)

            # 行数类断言用不到 fetch
            if assert_type == "mysql_query_count":
                rows = cursor.fetchall()
                count = len(rows)
                log.info("SQL %s -> %s 行", query, count)
                assert count == expected, f"SQL {query} 期望 {expected!r} 行，实际 {count!r} 行"
                return

            row = cursor.fetchone()
            description = cursor.description or []
        finally:
            cursor.close()

        result = self._pick_column(row, description, column) if row else None
        log.info("SQL %s -> %r", query, result)

        if assert_type == "mysql_query":
            assert result == expected, f"SQL {query} 期望 {expected!r}，实际 {result!r}"
        elif assert_type == "mysql_query_exists":
            assert row is not None, f"SQL {query} 期望有结果，实际没有"
        elif assert_type == "mysql_query_not_exists":
            assert row is None, f"SQL {query} 期望没有结果，实际查到 {result!r}"
        elif assert_type == "mysql_query_true":
            assert bool(result), f"SQL {query} 期望为真，实际 {result!r}"
        elif assert_type == "mysql_query_gte":
            assert result is not None and result >= expected, (
                f"SQL {query} 期望大于等于 {expected!r}，实际 {result!r}"
            )
        elif assert_type == "mysql_query_lte":
            assert result is not None and result <= expected, (
                f"SQL {query} 期望小于等于 {expected!r}，实际 {result!r}"
            )
        elif assert_type == "mysql_query_in":
            if container is None:
                raise ValueError(f"数据库断言 {assertion} 缺少 container")
            assert result in container, f"SQL {query} 期望在 {container!r} 中，实际 {result!r}"
        else:
            raise ValueError(f"未知的数据库断言类型: {assert_type}")

    @staticmethod
    def _pick_column(row: Any, description: Any, column: Any) -> Any:
        """从结果行里取某一列。

        - 不指定 ``column`` → 第一列（保持老行为）
        - ``column: 1`` → 第 2 列（0 开头）
        - ``column: status`` → 按列名找
        """
        if row is None:
            return None
        if column is None:
            return row[0]

        if isinstance(column, int) or (isinstance(column, str) and column.isdigit()):
            index = int(column)
            return row[index] if len(row) > index else None

        names = [d[0] for d in description]
        name = str(column)
        if name in names:
            return row[names.index(name)]
        raise ValueError(f"SQL 结果里没有 {name!r} 这一列，实际列名: {names}")
