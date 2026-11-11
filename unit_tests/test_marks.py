"""标签解析的单测。

字符串写法 ``"smoke and p0"`` 会按整词拆分；标签里本身含 ``and`` / ``or``
的（``order``、``brand``）不能被切碎 —— 切碎了 ``-m order`` 就一条都筛不到。
"""
from __future__ import annotations

from utils.cases.marks import extract_marks


def test_list_form() -> None:
    assert extract_marks({"marks": ["smoke", "p0"]}) == ["smoke", "p0"]


def test_string_form_splits_whole_words() -> None:
    assert extract_marks({"marks": "smoke and p0"}) == ["smoke", "p0"]
    assert extract_marks({"marks": "smoke or p0, regression"}) == ["smoke", "p0", "regression"]


def test_words_containing_and_or_survive() -> None:
    assert extract_marks({"marks": "order"}) == ["order"]
    assert extract_marks({"marks": "brand"}) == ["brand"]
    assert extract_marks({"marks": "android"}) == ["android"]


def test_aliases_and_dedupe() -> None:
    assert extract_marks({"mark": "smoke", "tags": ["smoke", "api"]}) == ["smoke", "api"]
