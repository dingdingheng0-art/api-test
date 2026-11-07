"""统一的数据模型：各种接口文档格式解析后，都变成同一种「接口」结构。

为什么要统一：后面生成用例、导出 Excel、生成网页预览都只认这一种结构，
新增一种文档格式（比如 Postman）只要写一个解析器就行，下游一行都不用改。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Param:
    """一个接口参数（query / path / header / body 字段）。"""

    name: str
    location: str = "query"          # query | path | header | cookie | body
    required: bool = False
    type: str = "string"             # string | integer | number | boolean | array | object
    example: Any = None
    description: str = ""
    enum: list[Any] = field(default_factory=list)
    fake: bool = False               # example 是按类型造出来的假值（文档里没给）

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "location": self.location,
            "required": self.required,
            "type": self.type,
            "example": self.example,
            "description": self.description,
            "enum": list(self.enum),
            "fake": self.fake,
        }


@dataclass
class Endpoint:
    """一个接口（一条可执行的 HTTP 请求）。"""

    id: str                          # 步骤 ID（用例里用它引用响应）
    method: str = "GET"
    path: str = "/"
    name: str = ""                   # 接口名（报告里显示）
    summary: str = ""                # 一句话说明
    description: str = ""
    tags: list[str] = field(default_factory=list)     # 分组 → 用例模块
    base_url: str = ""               # 服务地址
    headers: list[Param] = field(default_factory=list)
    query: list[Param] = field(default_factory=list)
    path_params: list[Param] = field(default_factory=list)
    cookies: list[Param] = field(default_factory=list)
    body_params: list[Param] = field(default_factory=list)   # 请求体里的字段
    body: Any = None                 # 请求体示例（dict / list / str）
    body_type: str = ""              # json | form | none
    response_example: Any = None     # 响应示例
    response_schema: dict[str, Any] | None = None
    source: str = ""                 # 来源文件名
    warnings: list[str] = field(default_factory=list)

    # ---------------- 便捷属性 ----------------

    @property
    def display_name(self) -> str:
        return self.name or self.summary or f"{self.method} {self.path}"

    @property
    def module(self) -> str:
        """所属模块：优先用第一个 tag，没有就用路径推断。"""
        if self.tags:
            return sanitize_name(self.tags[0])
        return infer_module(self.path)

    @property
    def full_url(self) -> str:
        if not self.base_url:
            return self.path
        return self.base_url.rstrip("/") + self.path

    def header_map(self) -> dict[str, str]:
        return {p.name: str(p.example) for p in self.headers if p.example is not None}

    def query_map(self) -> dict[str, Any]:
        return {p.name: p.example for p in self.query if p.example is not None}

    def path_map(self) -> dict[str, Any]:
        return {p.name: p.example for p in self.path_params if p.example is not None}

    def to_dict(self) -> dict[str, Any]:
        """转成纯 dict（给网页预览 / JSON 接口用）。"""
        return {
            "id": self.id,
            "method": self.method,
            "path": self.path,
            "name": self.display_name,
            "summary": self.summary,
            "description": self.description,
            "tags": list(self.tags),
            "module": self.module,
            "base_url": self.base_url,
            "headers": [p.to_dict() for p in self.headers],
            "query": [p.to_dict() for p in self.query],
            "path_params": [p.to_dict() for p in self.path_params],
            "body_params": [p.to_dict() for p in self.body_params],
            "body": self.body,
            "body_type": self.body_type,
            "response_example": self.response_example,
            "source": self.source,
            "warnings": list(self.warnings),
        }


_INVALID_CHARS = re.compile(r"[^0-9A-Za-z_\u4e00-\u9fff]+")

# 通用前缀 —— 剥掉它们，剩下的第一段才是有意义的模块名
GENERIC_PREFIXES = {
    "manager", "api", "apis", "rest", "service", "services", "gateway",
    "v1", "v2", "v3", "index", "open", "public", "private", "web", "app",
}


def infer_module(path: str, tags: list[str] | None = None) -> str:
    """从路径推断模块名。

    ``/admin/statistics/order`` → ``statistics``
    ``/api/v1/order/list``       → ``order``
    """
    segments = [s for s in (path or "").split("/") if s and not s.startswith("{")]
    while segments and segments[0].lower() in GENERIC_PREFIXES:
        segments.pop(0)
    if not segments:
        return "default"
    return sanitize_name(segments[0])


def sanitize_name(text: str, fallback: str = "default") -> str:
    """把任意文字变成能当目录名 / 文件名 / 变量名的字符串。"""
    if not text:
        return fallback
    cleaned = _INVALID_CHARS.sub("_", str(text)).strip("_")
    return cleaned or fallback


def snake_case(text: str, fallback: str = "api") -> str:
    """接口 ID：英文保留原样，中文转拼音做不到就用下划线连起来的短名。"""
    return sanitize_name(text, fallback).lower()
