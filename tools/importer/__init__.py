"""接口文档导入解析：把 Swagger / OpenAPI / HAR / cURL 变成能跑的测试用例。

模块划分：

- ``models`` —— 统一的数据模型
- ``openapi_parser`` / ``har_parser`` / ``curl_parser`` —— 各格式解析器
- ``dispatcher`` —— 格式识别 + 分发
- ``case_builder`` —— 生成 YAML / Excel 用例
- ``cli`` / ``web`` —— 命令行入口 / 本地网页上传页
"""
from __future__ import annotations

__all__ = ["models", "dispatcher", "case_builder", "cli", "web"]
