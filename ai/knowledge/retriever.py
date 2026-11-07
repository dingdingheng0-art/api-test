"""召回：从一个模糊的问题找到图里最相关的几个节点。

智能体问的基本都是这几类问题：

==================================================  ======================
问题                                                从哪入手
==================================================  ======================
「``/admin/area`` 的 ``areaName`` 落到哪个列」      端点 → 参数 → 列
「``tz_area.area_name`` 是哪个接口在写」             列 → 参数 → 端点
「这个接口有没有用例覆盖」                            端点 → 用例
「那个失败用例之前是不是改过表」                      用例 → 断言 → 列
==================================================  ======================

**为什么不是纯全文搜索**

FTS 只能回答"哪个节点名字里有这个词"。但用户问的是**关系**：
「``areaName`` 对应哪张表」光搜到 ``areaName`` 这个参数节点没用，
要顺着 ``maps_to`` 边走到列节点才能回答。

反过来，纯图遍历也不行 —— 得先知道从哪个节点出发。

所以是 **混合召回**：

1. **精确名匹配**（权重最高）—— 节点名和查询词完全一致，比如查 ``areaName``
2. **全文搜索**（BM25）—— 兜住"只记得大概叫什么"的情况
3. **图扩展**（1~2 跳）—— 从上面命中的节点往外走，带回它关联的东西

三路结果按 ``权重 × 衰减`` 合并排序，同一跳里越远的得分越低。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ai.knowledge.store import EDGE_MAPS_TO, KnowledgeStore, get_store
from utils.logs.log_manager import log

#: 各路召回的基础权重 —— 精确匹配比全文搜索可信
WEIGHT_EXACT = 1.0
WEIGHT_FTS = 0.6

#: 每往外跳一层的得分衰减
HOP_DECAY = (1.0, 0.5, 0.25)


def normalize_endpoint(text: str) -> str:
    """把 ``PUT /admin/area`` / ``PUT:/admin/area`` / ``put  /admin/area``
    统一成 ``PUT /admin/area``。

    端点 ID 里用的是冒号（``endpoint:PUT:/admin/area``），人写的是空格。
    两处各按自己的写法比字符串，永远对不上 —— 统一在这里做。
    """
    raw = str(text or "").strip()
    if not raw:
        return ""
    method, sep, path = raw.partition(" ")
    if not sep:
        method, sep, path = raw.partition(":")
    if not sep:
        return raw.replace(" ", "")
    return f"{method.upper()} {path.strip()}"


@dataclass
class Hit:
    """一条召回结果。"""

    node: dict[str, Any]
    score: float
    via: str                     # exact / fts / graph
    path: list[str] = field(default_factory=list)   # 从哪个节点走过来的

    @property
    def id(self) -> str:
        return str(self.node["id"])

    def to_dict(self) -> dict[str, Any]:
        data = {
            "id": self.id,
            "kind": self.node.get("kind"),
            "name": self.node.get("name"),
            "score": round(self.score, 3),
            "via": self.via,
        }
        if self.node.get("project"):
            data["project"] = self.node["project"]
        props = self.node.get("props") or {}
        if props:
            # 只给最有辨识度的几项，别把整坨 props 塞给模型
            data["props"] = {
                k: props[k]
                for k in ("summary", "path", "method", "table", "type", "comment", "file", "module")
                if k in props
            }
        if self.path:
            data["path"] = self.path
        return data


class Retriever:
    """在图上做混合召回。"""

    def __init__(self, store: KnowledgeStore | None = None) -> None:
        self.store = store or get_store()

    # ---------------- 主入口 ----------------

    def recall(
        self,
        query: str,
        kinds: tuple[str, ...] = (),
        expand: int = 1,
        limit: int = 10,
    ) -> list[Hit]:
        """召回和 ``query`` 相关的节点。

        Args:
            query: 查询词（字段名 / 接口路径 / 表名 / 中文描述都行）
            kinds: 只返回这些类型的节点（空 = 不限）
            expand: 图扩展跳数（0 = 只做名匹配和全文搜索）
            limit: 返回条数上限
        """
        query = str(query or "").strip()
        if not query:
            return []

        hits: dict[str, Hit] = {}

        self._add_exact(query, kinds, hits)
        self._add_fts(query, kinds, hits)

        if expand > 0:
            self._expand(list(hits.values()), expand, kinds, hits)

        ranked = sorted(hits.values(), key=lambda h: (-h.score, h.id))
        out = ranked[: max(1, limit)]
        log.debug("召回 %r → %s 条", query, len(out))
        return out

    # ---------------- 三种召回 ----------------

    def _add_exact(
        self,
        query: str,
        kinds: tuple[str, ...],
        hits: dict[str, Hit],
    ) -> None:
        """节点名（或 ID 尾段）和查询词完全一致。

        查 ``areaName`` 要能直接命中参数节点；
        查 ``PUT /admin/area`` 要能命中端点（它的 name 就是 ``PUT /admin/area``）；
        查 ``tz_area.area_name`` 要能命中列节点（ID 尾段）。
        """
        lowered = query.lower()
        # 「方法 + 路径」这种写法直接拼端点 ID
        for node in self._all_nodes(kinds):
            name = str(node.get("name") or "")
            node_id = str(node.get("id") or "")

            matched = (
                name.lower() == lowered
                or node_id.lower() == lowered
                # 列节点：column:tz_area.area_name 的尾段
                or node_id.lower().rsplit(":", 1)[-1] == lowered
                # 参数节点：param:endpoint:PUT:/admin/area.areaName 的尾段
                or node_id.rsplit(".", 1)[-1].lower() == lowered
            )
            if not matched:
                continue
            self._bump(hits, node, WEIGHT_EXACT, "exact", [])

    def _add_fts(
        self,
        query: str,
        kinds: tuple[str, ...],
        hits: dict[str, Hit],
    ) -> None:
        """全文搜索兜底（BM25 排序，拿不到分数就按名次折损）。"""
        try:
            found = self.store.search(query, limit=20)
        except Exception as exc:  # noqa: BLE001 - 搜索失败不该让召回整体挂掉
            log.warning("全文搜索失败 %r: %s", query, exc)
            return

        for rank, node in enumerate(found):
            if kinds and node.get("kind") not in kinds:
                continue
            # 名次越靠后权重越低；BM25 分数是负的（越小越相关），
            # 不同 SQLite 版本范围不一样，不直接拿它当权重
            weight = WEIGHT_FTS * (1.0 / (1.0 + rank * 0.3))
            self._bump(hits, node, weight, "fts", [])

    def _expand(
        self,
        seeds: list[Hit],
        hops: int,
        kinds: tuple[str, ...],
        hits: dict[str, Hit],
    ) -> None:
        """从命中节点往外走，把关联节点也带回来。"""
        frontier = seeds
        for depth in range(1, hops + 1):
            decay = HOP_DECAY[min(depth, len(HOP_DECAY) - 1)]
            next_frontier: list[Hit] = []

            for seed in frontier:
                for neighbor in self.store.neighbors(seed.id, limit=30):
                    if kinds and neighbor.get("kind") not in kinds:
                        continue

                    score = seed.score * decay * float(neighbor.get("confidence", 1.0))
                    if score < 0.05:
                        continue

                    path = [*seed.path[-2:], seed.id]
                    hit = self._bump(hits, neighbor, score, "graph", path)
                    if hit and hit.score == score:
                        next_frontier.append(hit)

            frontier = next_frontier
            if not frontier:
                return

    # ---------------- 辅助 ----------------

    def _all_nodes(self, kinds: tuple[str, ...]) -> list[dict[str, Any]]:
        """精确匹配要扫全表名 —— 图不大（几千节点），扫一遍几毫秒。

        没走 FTS 是因为 FTS 的分词会把 ``areaName`` 拆成 ``are`` ``aname``
        之类的子串（unicode61 对驼峰不友好），精确名匹配必须自己算。
        """
        if kinds:
            out: list[dict[str, Any]] = []
            for kind in kinds:
                out.extend(self.store.find_nodes(kind=kind, limit=100000))
            return out
        return self.store.find_nodes(limit=100000)

    @staticmethod
    def _bump(
        hits: dict[str, Hit],
        node: dict[str, Any],
        score: float,
        via: str,
        path: list[str],
    ) -> Hit | None:
        """把一条结果并进结果集（同一节点取最高分，via 保留最强的那个）。"""
        node_id = str(node.get("id") or "")
        if not node_id:
            return None

        existing = hits.get(node_id)
        if existing is None or score > existing.score:
            hits[node_id] = Hit(node=node, score=score, via=via, path=list(path))
            return hits[node_id]
        return None


# ---------------- 面向具体问题的封装 ----------------


def find_field_mapping(
    field: str,
    endpoint: str = "",
    store: KnowledgeStore | None = None,
) -> dict[str, Any]:
    """回答「这个字段落到哪个表的哪个列」。

    这是知识层最常被问的问题 —— 写落库断言、判断自愈方案都靠它。

    Args:
        field: 字段名（``areaName`` / ``area_name`` 都行）
        endpoint: 限定在某个接口上（``PUT /admin/area``）。
            不传就从所有声明了这个字段的端点里找，**但会标出每个证据的来源接口**
        store: 图谱存储
    """
    store = store or get_store()
    mapping_edges = store.conn.execute(
        "SELECT * FROM edge WHERE kind = ?", (EDGE_MAPS_TO,)
    ).fetchall()

    wanted = str(field or "").strip()
    all_results: list[dict[str, Any]] = []

    for edge in mapping_edges:
        src, dst = edge["src"], edge["dst"]
        # param:<端点>.<字段>
        if not src.startswith("param:"):
            continue
        owner, _, field_name = src.partition(".")
        if field_name.lower() != wanted.lower():
            continue

        eid = owner[len("param:"):]
        table, _, column = dst[len("column:"):].partition(".")
        all_results.append(
            {
                "endpoint": normalize_endpoint(eid[len("endpoint:"):]),
                "table": table,
                "column": column,
                "confidence": edge["confidence"],
                "evidence": edge["evidence"],
            }
        )

    # 同一个字段在多个接口上可能映射到**同一个**列，但证据强度不同。
    # 把它们按证据分开报 —— 把 POST 的 0.9 升成 PUT 的 1.0 是在说谎：
    # 「已验证」那个标签只属于真正跑过的接口。
    out = sorted(
        all_results, key=lambda m: (-m["confidence"], m["endpoint"])
    )

    if endpoint:
        wanted_ep = normalize_endpoint(endpoint)
        own = [m for m in out if m["endpoint"] == wanted_ep]
        note = _mapping_note(own, endpoint, elsewhere=out)
        out = own
    else:
        note = _mapping_note(out)

    return {
        "field": wanted,
        "found": bool(out),
        "mappings": out,
        "note": note,
    }


def _mapping_note(
    mappings: list[dict[str, Any]],
    endpoint: str = "",
    elsewhere: list[dict[str, Any]] | None = None,
) -> str:
    """给模型一句话说明这些映射的可靠程度。

    ``endpoint`` 非空时要按**那个接口**的证据说话 —— 光说"有已验证的映射"
    会让模型以为当前这个接口也验过。证据是分接口的。
    ``elsewhere`` 是同一个字段在别的接口上的映射，用来在"当前接口没证据"时
    提示一句"别处有、但不是这里的证据"。
    """
    if not mappings:
        if elsewhere:
            top = elsewhere[0]
            return (
                f"这个字段在 {endpoint} 上没有映射记录。"
                f"别处（{top['endpoint']}）有映射到 {top['table']}.{top['column']}，"
                f"但那是别的接口的证据 —— 用之前先确认这个接口是不是同一张表。"
            )
        return (
            "图谱里没有这个字段的映射。可能是新接口、字段名不带业务含义"
            "（如 list/detail），或表结构快照没更新。"
        )

    top = mappings[0]

    if top["confidence"] >= 1.0:
        return (
            f"{top['endpoint']} 上的映射已验证（来自已跑通的 db_query 断言），"
            f"可以直接用来写落库断言。"
        )
    if top["confidence"] >= 0.9:
        return (
            "映射由命名规则推导、表名来自请求体 DTO 且表存在，"
            "可以用于生成断言，但跑之前建议核对一眼列名。"
        )
    return (
        "映射是候选（表名靠路径/模块猜的），**不要直接拿来写断言**，"
        "先 get_table_schema 确认。"
    )


def explain_endpoint(
    path: str,
    method: str = "",
    store: KnowledgeStore | None = None,
) -> dict[str, Any]:
    """回答「这个接口和库/用例的关系」。

    返回：请求体字段落到哪些列、哪些用例覆盖了它、它写的数据被哪些断言验过。
    """
    from ai.knowledge.builder import endpoint_id, param_id

    store = store or get_store()
    method = str(method or "").strip().upper()

    # 没给 method 就把所有方法都找出来
    candidates: list[str] = []
    if method:
        candidates = [endpoint_id(method, path)]
    else:
        candidates = [
            str(row["id"])
            for row in store.conn.execute(
                "SELECT id FROM node WHERE kind = 'endpoint' AND id LIKE ?",
                (f"%:{path}",),
            )
        ]

    endpoints = [store.get_node(cid) for cid in candidates]
    endpoints = [e for e in endpoints if e]
    if not endpoints:
        return {
            "path": path,
            "found": False,
            "note": "图谱里没有这个接口。确认路径写在 OpenAPI 里、且快照没过期。",
        }

    out = []
    for ep in endpoints:
        params = store.neighbors(ep["id"], kind="has_param", direction="out")
        mappings = []
        for param in params:
            for col in store.neighbors(param["id"], kind="maps_to", direction="out"):
                table, _, column = col["id"][len("column:"):].partition(".")
                mappings.append(
                    {
                        "field": param["name"],
                        "table": table,
                        "column": column,
                        "confidence": col["confidence"],
                    }
                )

        cases = store.neighbors(ep["id"], kind="covered_by", direction="out")
        out.append(
            {
                "endpoint": ep["id"][len("endpoint:"):],
                "summary": (ep.get("props") or {}).get("summary") or "",
                "params": [p["name"] for p in params],
                "mappings": mappings,
                "covered_by": [c["name"] for c in cases],
            }
        )

    return {
        "path": path,
        "found": True,
        "endpoints": out,
    }
