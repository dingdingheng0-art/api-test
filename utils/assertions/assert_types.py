"""断言类型注册表：框架认识哪些断言类型。

**为什么要单独维护一份**

断言类型散落在 ``utils/assert_handler.py`` 的一堆 ``if`` 分支里。
校验器要判断"这个 ``type`` 写对了吗"，如果在校验器里再抄一份，
以后加了新断言却忘了同步，两边就会打架。

所以这里把类型清单集中放一处，断言处理器和校验器都从这里读。

**加新断言类型时**：在 ``utils/assert_handler.py`` 里实现分支，
然后在这里加进对应的集合，校验器就自动认识了。
"""
from __future__ import annotations

# ---- 响应体断言（比字段值）----
BODY_ASSERT_TYPES = frozenset({
    "equal", "equals", "==",
    "not_equal", "notequal", "!=",
    "iequal", "equal_ignore_case", "equals_ignore_case",
    "inequal", "not_equal_ignore_case",
    "is_none", "none",
    "is_not_none", "not_none",
    "is_true", "true",
    "is_false", "false",
    "empty", "is_empty",
    "not_empty", "is_not_empty",
    "contains",
    "not_contains",
    "matches", "regex", "match",
    "not_matches", "not_regex",
    "starts_with", "startswith",
    "ends_with", "endswith",
    "in",
    "not_in",
    "gt", "greater_than",
    "gte", "greater_than_or_equal",
    "lt", "less_than",
    "lte", "less_than_or_equal",
    "length", "len",
    "length_ne", "length_not_equal",
    "length_gt", "length_greater_than",
    "length_gte", "length_greater_than_or_equal",
    "length_lt", "length_less_than",
    "length_lte", "length_less_than_or_equal",
    "full_body", "body_equal",
    "type", "is_type",
    "approx_equal", "approximately_equal", "almost_equal",
    "jwt_claim", "jwt",
    "exists", "field_exists",
    "not_exists", "field_not_exists",
    "subset",
})

# ---- 响应元信息断言（状态码 / 响应头 / 耗时）----
META_ASSERT_TYPES = frozenset({
    "status_code", "status", "http_status",
    "response_header", "header",
    "elapsed_time", "elapsed", "response_time",
})

# ---- 数据库断言 ----
DB_ASSERT_TYPES = frozenset({
    "db_query",
    "db_query_exists",
    "db_query_not_exists",
    "db_query_true",
    "db_query_count",
    "db_query_gt",
    "db_query_gte",
    "db_query_lt",
    "db_query_lte",
    "db_query_in",
})

# ---- 老版数据库断言（mysql_ 前缀，与 db_* 并存）----
LEGACY_DB_ASSERT_TYPES = frozenset({
    "mysql_query",
    "mysql_query_exists",
    "mysql_query_not_exists",
    "mysql_query_true",
    "mysql_query_count",
    "mysql_query_gte",
    "mysql_query_lte",
    "mysql_query_in",
})

# ---- 缓存断言 ----
REDIS_ASSERT_TYPES = frozenset({
    "redis_get",
    "redis_exists",
    "redis_ttl",
    "redis_deleted",
})

# ---- 消息队列断言 ----
MQ_ASSERT_TYPES = frozenset({
    "mq_received",
    "mq_count",
    "mq_payload",
})

# 全部认识的断言类型
ALL_ASSERT_TYPES = (
    BODY_ASSERT_TYPES
    | META_ASSERT_TYPES
    | DB_ASSERT_TYPES
    | LEGACY_DB_ASSERT_TYPES
    | REDIS_ASSERT_TYPES
    | MQ_ASSERT_TYPES
)

# 各类型需要的必填字段（用于校验"字段对不上"）
REQUIRED_FIELDS_BY_TYPE: dict[str, tuple[str, ...]] = {
    "db_query": ("query",),
    "db_query_exists": ("query",),
    "db_query_not_exists": ("query",),
    "db_query_true": ("query",),
    "db_query_count": ("query",),
    "db_query_gt": ("query",),
    "db_query_gte": ("query",),
    "db_query_lt": ("query",),
    "db_query_lte": ("query",),
    "db_query_in": ("query", "container"),
    "mysql_query": ("query",),
    "mysql_query_exists": ("query",),
    "mysql_query_not_exists": ("query",),
    "mysql_query_true": ("query",),
    "mysql_query_count": ("query",),
    "mysql_query_gte": ("query",),
    "mysql_query_lte": ("query",),
    "mysql_query_in": ("query", "container"),
    "redis_get": ("key",),
    "redis_exists": ("key",),
    "redis_ttl": ("key",),
    "redis_deleted": ("key",),
    "mq_received": ("queue",),
    "mq_count": ("queue",),
    "mq_payload": ("queue",),
}

# 需要 field 的断言类型（比响应体字段的）
_NEEDS_FIELD = BODY_ASSERT_TYPES - {"full_body", "body_equal"}


def normalize(assert_type: str) -> str:
    """归一化断言类型：转小写、空格换成下划线。

    和 ``utils/assert_handler.py`` 里的处理保持一致 ——
    ``is not None`` 和 ``is_not_none`` 是同一个意思。
    """
    return str(assert_type or "").strip().lower().replace(" ", "_")


def is_known(assert_type: str) -> bool:
    """这个断言类型框架认不认识。"""
    return normalize(assert_type) in ALL_ASSERT_TYPES


def suggest(assert_type: str) -> str:
    """给拼错的断言类型推荐一个正确的。

    用简单编辑距离找最接近的已知类型。找不到就返回空串。
    """
    import difflib

    target = normalize(assert_type)
    if not target or target in ALL_ASSERT_TYPES:
        return ""

    # 先按前缀猜（db_xxx / redis_xxx / mq_xxx）
    for prefix, known_set in (
        ("db_", DB_ASSERT_TYPES),
        ("mysql_", LEGACY_DB_ASSERT_TYPES),
        ("redis_", REDIS_ASSERT_TYPES),
        ("mq_", MQ_ASSERT_TYPES),
    ):
        if target.startswith(prefix):
            suffix = target[len(prefix):]
            pool = {t[len(prefix):] for t in known_set}
            match = difflib.get_close_matches(suffix, pool, n=1, cutoff=0.6)
            if match:
                return prefix + match[0]

    match = difflib.get_close_matches(target, ALL_ASSERT_TYPES, n=1, cutoff=0.6)
    return match[0] if match else ""


def required_fields(assert_type: str) -> tuple[str, ...]:
    """这个断言类型需要哪些必填字段。"""
    return REQUIRED_FIELDS_BY_TYPE.get(normalize(assert_type), ())


def needs_field(assert_type: str) -> bool:
    """这个断言类型要不要 ``field``。"""
    return normalize(assert_type) in _NEEDS_FIELD
