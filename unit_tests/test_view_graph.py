"""``tools/maint/view_graph.py`` 的行为测试。

图谱是派生数据，坏了不报错 —— 只是"看起来少了一块"。这里钉住三个
真踩过的坑：

1. **全图导出先截断节点再筛边** → 边几乎全被筛掉，图看起来像坏了
2. **``props`` 在库里是 JSON 字符串**，直接塞进 HTML 会让前端显示成碎片
3. **焦点导出的 BFS 只走 ``depth`` 跳**，不能漏掉种子节点本身
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai.knowledge.store import KnowledgeStore


def _load_module() -> Any:
    """按路径加载 ``tools/maint/view_graph.py``。

    直接 ``import tools.maint.view_graph`` 会执行模块顶层的
    ``sys.stdout.reconfigure`` —— 在 pytest 捕获输出时那会把
    stdout 换掉，后面的测试全都写不出来。按 spec 加载也一样，
    所以这里只取函数，不依赖模块副作用。
    """
    path = PROJECT_ROOT / "tools" / "maint" / "view_graph.py"
    spec = importlib.util.spec_from_file_location("_view_graph_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def vg() -> Any:
    return _load_module()


@pytest.fixture()
def store() -> Any:
    """内存图谱，不碰真实的 .cache/knowledge/graph.db。"""
    s = KnowledgeStore(":memory:")
    yield s
    s.close()


# ---------------------------------------------------------------------------
# props 解析
# ---------------------------------------------------------------------------


class TestAsProps:
    def test_json_string_becomes_dict(self, vg: Any) -> None:
        assert vg._as_props('{"a": 1}') == {"a": 1}

    def test_dict_passes_through(self, vg: Any) -> None:
        assert vg._as_props({"a": 1}) == {"a": 1}

    def test_none_becomes_empty(self, vg: Any) -> None:
        assert vg._as_props(None) == {}

    def test_empty_string_becomes_empty(self, vg: Any) -> None:
        assert vg._as_props("") == {}

    def test_garbage_does_not_raise(self, vg: Any) -> None:
        """库里万一存了坏 JSON，导出不该整体挂掉。"""
        assert vg._as_props("{not json") == {}

    def test_json_array_is_not_a_dict(self, vg: Any) -> None:
        """数组解析出来也不能当 props —— 前端按对象取键会拿到下标。"""
        assert vg._as_props("[1, 2]") == {}


# ---------------------------------------------------------------------------
# 全图导出：不能把边筛没
# ---------------------------------------------------------------------------


def _seed(store: Any) -> None:
    """两张表各 3 列，外加一条映射边。"""
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    for table in ("tz_a", "tz_b"):
        nodes.append(
            {"id": f"table:{table}", "kind": "table", "name": table, "props": {}}
        )
        for col in ("id", "name", "seq"):
            nodes.append(
                {
                    "id": f"column:{table}.{col}",
                    "kind": "column",
                    "name": col,
                    "props": {"table": table},
                }
            )
            edges.append(
                {
                    "src": f"table:{table}",
                    "dst": f"column:{table}.{col}",
                    "kind": "has_column",
                    "confidence": 1.0,
                }
            )
    store.bulk_add(nodes, edges)


def test_full_export_keeps_all_edges(vg: Any, store: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """全图导出必须带上全部边。

    回归：曾经先按 limit 截断节点、再筛"两端都在"的边 —— 截断后几乎
    没有成对的端点，2468 条边只剩 78 条，导出的图看着像坏了一半。
    """
    _seed(store)
    monkeypatch.setattr(vg, "get_store", lambda: store)

    nodes, edges = vg._collect("", 2, 0)

    assert len(nodes) == 8
    assert len(edges) == 6


def test_full_export_limit_still_keeps_consistent_edges(
    vg: Any, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """显式给 limit 时，边必须都落在留下的节点之间（不能有悬空边）。"""
    _seed(store)
    monkeypatch.setattr(vg, "get_store", lambda: store)

    nodes, edges = vg._collect("", 2, 4)
    kept = {n["id"] for n in nodes}

    assert len(nodes) == 4
    for edge in edges:
        assert edge["src"] in kept and edge["dst"] in kept


# ---------------------------------------------------------------------------
# 焦点导出
# ---------------------------------------------------------------------------


def test_focus_export_includes_seed_and_neighbors(
    vg: Any, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(store)
    monkeypatch.setattr(vg, "get_store", lambda: store)

    nodes, edges = vg._collect("table:tz_a", 1, 0)
    ids = {n["id"] for n in nodes}

    assert "table:tz_a" in ids
    assert {"column:tz_a.id", "column:tz_a.name", "column:tz_a.seq"} <= ids
    assert len(edges) == 3
    # 一跳不该跨到另一张表
    assert "table:tz_b" not in ids


def test_focus_export_accepts_bare_name(
    vg: Any, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """用户只会写 ``tz_a``，不会写 ``table:tz_a``。"""
    _seed(store)
    monkeypatch.setattr(vg, "get_store", lambda: store)

    nodes, _ = vg._collect("tz_a", 1, 0)
    assert "table:tz_a" in {n["id"] for n in nodes}


def test_focus_export_unknown_raises(vg: Any, store: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(store)
    monkeypatch.setattr(vg, "get_store", lambda: store)

    with pytest.raises(SystemExit):
        vg._collect("完全不存在的节点", 1, 0)


# ---------------------------------------------------------------------------
# HTML 落盘
# ---------------------------------------------------------------------------


def test_export_writes_self_contained_html(
    vg: Any, store: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """导出的 HTML 要自带数据、不留占位符、不引外部资源。"""
    _seed(store)
    monkeypatch.setattr(vg, "get_store", lambda: store)

    out = tmp_path / "graph.html"
    code = vg.cmd_export(
        type(
            "Args",
            (),
            {"focus": "", "depth": 2, "limit": 0, "output": str(out)},
        )()
    )

    assert code == 0
    page = out.read_text(encoding="utf-8")

    for placeholder in ("__DATA__", "__COLORS__", "__LABELS__", "__TITLE__"):
        assert placeholder not in page

    # 单文件：不能有外链脚本/样式
    assert "<script src=" not in page
    assert "http://" not in page and "https://" not in page

    payload = json.loads(
        page.split("const DATA = ", 1)[1].split(";\n", 1)[0]
    )
    assert len(payload["nodes"]) == 8
    assert len(payload["edges"]) == 6
    # props 必须是对象，前端才能 Object.keys
    assert all(isinstance(n["props"], dict) for n in payload["nodes"])


def test_export_escapes_title(
    vg: Any, store: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """节点名里带 ``<`` 时不能把 HTML 弄坏。"""
    store.bulk_add(
        [
            {
                "id": "table:x",
                "kind": "table",
                "name": "<script>alert(1)</script>",
                "props": {},
            }
        ],
        [],
    )
    monkeypatch.setattr(vg, "get_store", lambda: store)

    out = tmp_path / "g.html"
    vg.cmd_export(
        type("Args", (), {"focus": "", "depth": 1, "limit": 0, "output": str(out)})()
    )
    page = out.read_text(encoding="utf-8")

    # 出现在 <title> 里时必须是转义过的
    assert "<title>知识图谱 · 全图</title>" in page
