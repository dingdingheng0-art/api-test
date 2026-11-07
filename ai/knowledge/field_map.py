"""字段映射：把 API 字段名映射到数据库列名。

**这是整个知识层最值钱的一步。**

``areaId`` ↔ ``tz_area.area_id``、``isTop`` ↔ ``tz_notice.is_top``
—— 有了这个映射，智能体写落库断言时才不用猜列名，
自愈时才能判断"到底是接口改了还是表改了"。

**证据分三档，按可信度从高到低**：

1. **已验证**（``confidence=1.0``）
   用例里已经写好并跑通的 ``db_query`` 断言里，SQL 明确写了
   ``SELECT area_name FROM tz_area``，用例又往 ``/admin/area`` 的请求体里
   发了 ``areaName`` —— 这是**被验证过**的映射，最可靠。

2. **命名规则 + 表能对上**（``0.9``）
   ``areaName`` → 驼峰转下划线 → ``area_name``；
   表名由 schema 名推出（``Area`` → ``tz_area``）并且在快照里真实存在。

3. **只有命名规则**（``0.6``）
   列名转出来了，但表名是靠"接口路径/模块名"猜的，快照里不一定有。
   这类**只做候选**，不自动用于生成断言。

**为什么不做模糊匹配**

``id`` 和 ``userId`` 都能映射到很多列，
用编辑距离硬凑出来的映射看着像对、实际是错的 ——
错的落库断言比没有断言更糟（它会让人误以为验过了）。
所以这里**只做确定性推导**，宁可少给。

**LLM 辅助的定位**

留给"候选生成"：规则推不出来的字段，把参数表 + 候选列喂给模型，
让它给出映射和理由。但输出仍然标 ``confidence=0.6`` 进待确认队列，
**不直接进图的高置信度区**。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------
# 命名规则
# --------------------------------------------------------------------------

# 常见缩写 / 同义词，先归一化再比对
# key 是小写去下划线的形式，value 是标准词
_SYNONYMS = {
    "isdelete": "deleted",
    "del": "delete",
    "img": "image",
    "pic": "image",
    "photo": "image",
    "photoes": "image",
    "pics": "image",
    "tel": "phone",
    "mobile": "phone",
    "mobileno": "phone",
    "telephone": "phone",
    "name": "name",
    "title": "title",
    "content": "content",
    "desc": "description",
    "remark": "remark",
    "memo": "remark",
    "qty": "quantity",
    "num": "num",
    "no": "no",
    "amt": "amount",
    "price": "price",
    "money": "price",
    "time": "time",
    "date": "date",
    "createtime": "create_time",
    "updatetime": "update_time",
    "createdat": "create_time",
    "updatedat": "update_time",
}

# 这些列是"技术字段"，任何接口的字段都可能映射到它们，
# 但自动推导很容易误判 —— 只允许从已验证的 SQL 里学。
_TECH_COLUMNS = {
    "id",
    "create_time",
    "update_time",
    "deleted",
    "version",
    "shop_id",
}


def snake_case(name: str) -> str:
    """驼峰 / 帕斯卡 / 全大写转下划线小写。

    ``areaName`` → ``area_name``
    ``AreaID``   → ``area_id``
    ``userID``   → ``user_id``
    ``is_top``   → ``is_top``
    """
    if not name:
        return ""
    # 已经带下划线的，先按段处理
    text = name.strip()
    # HTML / 用户输入里的连字符也当分隔符
    text = re.sub(r"[-\s]+", "_", text)
    # aB -> a_B；ABc -> A_Bc；A1 -> A_1 要小心（areaId2 -> area_id2）
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)
    text = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", text)
    return text.lower()


def squash(name: str) -> str:
    """归一化成"无分隔小写"，用于同义词比对。"""
    return re.sub(r"[_\-\s]", "", snake_case(name)).lower()


def canonical(name: str) -> str:
    """归一化到标准词（应用同义词表）。"""
    key = squash(name)
    return _SYNONYMS.get(key, key)


def names_match(api_name: str, column_name: str) -> tuple[bool, str]:
    """判断一个 API 字段名和一个列名是不是"同一个东西"。

    Returns:
        ``(是否匹配, 匹配理由)``
    """
    if not api_name or not column_name:
        return False, ""

    # 1. 直接相等（大小写不敏感）
    if api_name.lower() == column_name.lower():
        return True, "同名"

    # 2. 驼峰 → 下划线后相等
    if snake_case(api_name) == column_name.lower():
        return True, "驼峰转下划线同名"

    # 3. 去分隔符后相等（处理 is_top vs isTop vs isTop_）
    if squash(api_name) == squash(column_name):
        return True, "去分隔符同名"

    # 4. 同义词归一后相等
    if canonical(api_name) == canonical(column_name):
        return True, f"同义词归一（{canonical(api_name)}）"

    return False, ""


def stem(word: str) -> str:
    """粗略取词根（英文复数 / ing），只用于比较，不追求语言学正确。"""
    w = word.lower()
    for suffix in ("ies", "es", "s", "ing", "ed"):
        if len(w) > len(suffix) + 2 and w.endswith(suffix):
            return w[: -len(suffix)]
    return w


# --------------------------------------------------------------------------
# 表名推导
# --------------------------------------------------------------------------


def is_tech_column(column_name: str) -> bool:
    """是不是"技术字段"（任何表都有，自动推导必错）。

    ``id`` / ``create_time`` / ``deleted`` 这类列，靠命名规则去猜
    等于掷骰子 —— 所以只允许从**已验证的 SQL** 里学。
    """
    return snake_case(column_name) in _TECH_COLUMNS


def table_candidates_detail(
    schema_name: str,
    endpoint_path: str = "",
    module: str = "",
) -> list[tuple[str, str]]:
    """同 :func:`table_candidates`，但每条候选带**来源标签**。

    来源决定了映射的可信度：

    ==========  ==========================================  ======
    来源         怎么来的                                    置信度
    ==========  ==========================================  ======
    ``schema``  请求体 DTO 名（``Area`` → ``tz_area``）       高
    ``path``    接口路径段（``/admin/hotSearch``）            中
    ``module``  用例模块名（``admin_area``）                  中
    ==========  ==========================================  ======

    Returns:
        ``[(表名, 来源), ...]``，按可信度从高到低，已去重
    """
    candidates: list[tuple[str, str]] = []
    if schema_name:
        base = snake_case(schema_name)
        for prefix in ("tz_", ""):
            for suffix in ("", "s"):
                candidates.append((f"{prefix}{base}{suffix}", "schema"))

    # 从接口路径推：/admin/hotSearch → hot_search
    segs = [s for s in re.split(r"[/\-{}]+", endpoint_path or "") if s]
    segs = [s for s in segs if s.lower() not in ("admin", "api", "v1", "v2", "info", "page")]
    if segs:
        base = snake_case(segs[0])
        for prefix in ("tz_", ""):
            candidates.append((f"{prefix}{base}", "path"))

    if module:
        base = snake_case(module)
        for prefix in ("tz_", ""):
            candidates.append((f"{prefix}{base}", "module"))

    # 去重保序：同一个表名保留**最先出现**（即可信度最高）的来源
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for name, source in candidates:
        if name and name not in seen:
            seen.add(name)
            out.append((name, source))
    return out


def table_candidates(schema_name: str, endpoint_path: str = "", module: str = "") -> list[str]:
    """给一个 schema 名（如 ``HotSearch``）推可能的表名。

    商城类系统的命名习惯：``tz_`` 前缀 + 下划线。

    Returns:
        候选表名，按可信度从高到低
    """
    return [name for name, _source in table_candidates_detail(schema_name, endpoint_path, module)]


# --------------------------------------------------------------------------
# 映射结果
# --------------------------------------------------------------------------


@dataclass
class FieldMapping:
    """一条字段映射。"""

    api_field: str
    column: str
    table: str
    confidence: float
    evidence: str
    source: str = "rule"     # verified | rule | heuristic

    #: 这条映射是在哪个接口上验证的（``endpoint:PUT:/admin/area``）。
    #: 只有 ``source="verified"`` 才有意义 —— 它决定了这条证据
    #: **能不能推广到别的接口**，见 :meth:`applies_to`。
    verified_at: str = ""

    @property
    def qualified_column(self) -> str:
        return f"{self.table}.{self.column}" if self.table else self.column

    def applies_to(self, endpoint: str) -> bool:
        """这条映射能不能算作 ``endpoint`` 的已验证证据。

        **为什么不能随便推广**

        「修改地区」用例调的是 ``PUT /admin/area``，它证明了
        ``areaName`` → ``tz_area.area_name``。``POST /admin/area``（新增）
        也声明了 ``areaName``，但这两条用例没跑过新增 —— 把 PUT 的证据
        直接记到 POST 头上，等于凭空给了一个"已验证"标签。

        规则：只认**同一个接口**的证据。别的接口上的同名同义字段，
        由命名规则给 0.9，仍然可用，只是不冒充"验证过"。
        """
        if self.source != "verified" or not self.verified_at:
            return True   # 非验证类映射不受此限
        return self.verified_at == endpoint

    def to_dict(self) -> dict[str, Any]:
        data = {
            "api_field": self.api_field,
            "table": self.table,
            "column": self.column,
            "confidence": round(self.confidence, 2),
            "evidence": self.evidence,
            "source": self.source,
        }
        if self.verified_at:
            data["verified_at"] = self.verified_at
        return data


@dataclass
class MappingResult:
    """一个接口的字段映射结果。"""

    mappings: list[FieldMapping] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)

    def by_field(self) -> dict[str, FieldMapping]:
        return {m.api_field: m for m in self.mappings}

    def confident(self, threshold: float = 0.9) -> list[FieldMapping]:
        return [m for m in self.mappings if m.confidence >= threshold]
