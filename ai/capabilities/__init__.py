"""AI 能力层。

三块：

- ``client``     —— LLM 客户端（OpenAI 兼容协议）
- ``case_gen``   —— 需求文档 → 测试用例
- ``diagnosis``  —— 失败用例 → 根因分析

**重要**：这一层是**可选**的。没配 API key 也能用 ——
``case_gen`` 会退回本地模板生成，``diagnosis`` 会退回本地规则分类。
"""

from __future__ import annotations

__all__ = ["client", "case_gen", "diagnosis"]
