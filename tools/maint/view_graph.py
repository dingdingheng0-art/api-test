"""看知识图谱：命令行查 + 导出可交互的 HTML。

**为什么要有这个**

图谱落在 SQLite 里，平时只能通过 MCP 工具间接用（``find_field_mapping`` 等），
想「看一眼全局」得自己写 SQL。这个工具补上两种看法：

- 命令行：查某个接口 / 表 / 字段的关系，适合边写用例边查
- HTML：整张图导成一个文件，双击就能在浏览器里点、搜、展开邻居

HTML 是**单文件**，不依赖网络、不依赖任何 JS 库 —— 图谱是内部资产，
不该为了看它去连 CDN。

用法::

    # 接口全景：字段落到哪张表、被哪些用例覆盖
    python tools/maint/view_graph.py endpoint "PUT /admin/area"

    # 某张表的列，以及哪些接口写它
    python tools/maint/view_graph.py table tz_area

    # 某个字段在所有接口上的映射（带置信度）
    python tools/maint/view_graph.py field areaName

    # 看某个节点的邻居（图扩展）
    python tools/maint/view_graph.py neighbors "table:tz_area"

    # 全局统计
    python tools/maint/view_graph.py stats

    # 导出可交互 HTML（默认 reports/graph.html）
    python tools/maint/view_graph.py export
    python tools/maint/view_graph.py export --focus tz_area --depth 2
"""
from __future__ import annotations

import argparse
import html
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai.knowledge.store import get_store  # noqa: E402

# 节点类型 → 显示用的中文名和颜色（HTML 和终端共用一套，避免两边对不上）
KIND_LABELS: dict[str, tuple[str, str]] = {
    "project": ("项目", "#6b7280"),
    "endpoint": ("接口", "#2563eb"),
    "param": ("参数", "#7c3aed"),
    "table": ("数据表", "#059669"),
    "column": ("字段", "#10b981"),
    "case": ("用例", "#d97706"),
    "step": ("步骤", "#f59e0b"),
    "assertion": ("断言", "#dc2626"),
}


def _label(kind: str) -> str:
    return KIND_LABELS.get(kind, (kind, "#9ca3af"))[0]


def _color(kind: str) -> str:
    return KIND_LABELS.get(kind, (kind, "#9ca3af"))[1]


def _short(text: Any, limit: int = 70) -> str:
    text = str(text or "").replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"


def _as_props(value: Any) -> dict[str, Any]:
    """``props`` 在库里存的是 JSON 字符串。

    直接走 SQL 拿到的行没经过 ``store._row_to_node``，props 还是字符串。
    不解析就塞进 HTML，前端 ``Object.keys(props)`` 会拿到 "0","1","2" 这种
    下标，详情面板显示成一堆碎片。
    """
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------


def cmd_stats(_args: argparse.Namespace) -> int:
    store = get_store()
    stats = store.stats()
    graph = stats.get("graph") or stats

    print(f"图谱文件: {store.path}")
    print(f"节点 {graph.get('nodes', 0)} ｜ 边 {graph.get('edges', 0)}")
    print()

    print("节点")
    for kind, count in sorted(
        (graph.get("by_kind") or {}).items(), key=lambda kv: -kv[1]
    ):
        print(f"  {_label(kind):6} {kind:12} {count:>6}")
    print()

    print("边")
    for kind, count in sorted(
        (graph.get("by_edge") or {}).items(), key=lambda kv: -kv[1]
    ):
        print(f"  {kind:14} {count:>6}")

    mappings = stats.get("mappings") or {}
    if mappings:
        print()
        print(
            "字段映射："
            f"已验证 {mappings.get('verified', 0)} ｜ "
            f"规则推导 {mappings.get('rule', 0)} ｜ "
            f"候选 {mappings.get('guess', 0)}"
        )
    return 0


def _print_node(node: dict[str, Any], indent: str = "  ") -> None:
    print(f"{indent}[{_label(node.get('kind', ''))}] {node.get('name')}  ({node.get('id')})")
    props = node.get("props") or {}
    if props:
        shown = {k: v for k, v in props.items() if v not in ("", None, [], {})}
        if shown:
            print(f"{indent}  {_short(json.dumps(shown, ensure_ascii=False), 200)}")


def cmd_endpoint(args: argparse.Namespace) -> int:
    """接口全景：参数 → 表列，覆盖它的用例。"""
    from ai.knowledge.retriever import explain_endpoint

    method, _, path = args.query.partition(" ")
    if not path:
        path, method = method, ""

    result = explain_endpoint(path, method)
    if not result.get("found"):
        print(f"没找到接口 {args.query}")
        return 1

    for ep in result.get("endpoints") or []:
        print(f"接口 {ep.get('endpoint')}")
        if ep.get("summary"):
            print(f"  说明: {ep['summary']}")

        print(f"  参数: {', '.join(ep.get('params') or []) or '（无）'}")

        mappings = ep.get("mappings") or []
        if mappings:
            print("  字段映射:")
            for m in mappings:
                conf = m.get("confidence")
                mark = "已验证" if conf == 1 else ("规则" if conf and conf >= 0.9 else "候选")
                print(
                    f"    {m.get('field'):24} → {m.get('table')}.{m.get('column'):24}"
                    f" [{mark} {conf}]"
                )
        else:
            print("  字段映射: （无）")

        covered = ep.get("covered_by") or []
        print(f"  覆盖用例: {', '.join(covered) if covered else '（无）'}")
        print()
    return 0


def cmd_table(args: argparse.Namespace) -> int:
    """一张表：所有列 + 哪些接口写它。"""
    store = get_store()
    table_id = f"table:{args.query}"
    node = store.get_node(table_id)
    if not node:
        print(f"没找到表 {args.query}")
        return 1

    _print_node(node)
    print()

    columns = store.neighbors(table_id, kind="has_column")
    print(f"列（{len(columns)}）")
    for col in sorted(columns, key=lambda c: c.get("name") or ""):
        props = col.get("props") or {}
        print(
            f"  {col.get('name'):28} {str(props.get('type') or ''):16}"
            f" {props.get('comment') or ''}"
        )
    print()

    # 哪些接口的字段映射到这张表
    writers: dict[str, set[str]] = {}
    for col in columns:
        for param in store.neighbors(col["id"], kind="maps_to", direction="in"):
            owner = (param.get("props") or {}).get("owner") or ""
            if owner:
                writers.setdefault(owner, set()).add(param.get("name") or "")
    if writers:
        print(f"写这张表的接口（{len(writers)}）")
        for owner, fields in sorted(writers.items()):
            print(f"  {owner.replace('endpoint:', ''):34} {', '.join(sorted(fields))}")
    return 0


def cmd_field(args: argparse.Namespace) -> int:
    """一个字段在所有接口上的映射。"""
    store = get_store()
    rows = store.conn.execute(
        "SELECT id, kind, name, props FROM node WHERE kind = 'param' AND name = ?",
        (args.query,),
    ).fetchall()
    if not rows:
        print(f"没找到字段 {args.query}")
        return 1

    print(f"字段 {args.query} 出现在 {len(rows)} 个接口上")
    for row in rows:
        owner = (json.loads(row["props"] or "{}").get("owner") or "").replace(
            "endpoint:", ""
        )
        targets = store.neighbors(row["id"], kind="maps_to", direction="out")
        if not targets:
            print(f"  {owner:34} → （无映射）")
            continue
        for target in targets:
            props = target.get("props") or {}
            conf = target.get("confidence")
            mark = "已验证" if conf == 1 else ("规则" if conf and conf >= 0.9 else "候选")
            print(
                f"  {owner:34} → {props.get('table')}.{target.get('name')}"
                f"  [{mark} {conf}]"
            )
    return 0


def cmd_neighbors(args: argparse.Namespace) -> int:
    store = get_store()
    node = store.get_node(args.query)
    if not node:
        print(f"没找到节点 {args.query}")
        return 1

    _print_node(node)
    print()

    neighbors = store.neighbors(args.query, limit=args.limit)
    if not neighbors:
        print("（没有邻居）")
        return 0

    by_edge: dict[str, list[dict[str, Any]]] = {}
    for item in neighbors:
        by_edge.setdefault(str(item.get("edge_kind")), []).append(item)

    for edge_kind, items in sorted(by_edge.items()):
        print(f"{edge_kind}（{len(items)}）")
        for item in items:
            conf = item.get("confidence")
            suffix = f"  [{conf}]" if conf is not None else ""
            print(f"  [{_label(item.get('kind', ''))}] {item.get('name')}{suffix}")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    store = get_store()
    hits = store.search(args.query, limit=args.limit)
    if not hits:
        print(f"没搜到 {args.query!r}")
        print("提示: 英文标识符（tz_area / areaName / PUT /admin/area）命中率最高。")
        return 1

    for hit in hits:
        print(f"  [{_label(hit.get('kind', ''))}] {hit.get('name')}  ({hit.get('id')})")
    return 0


# ---------------------------------------------------------------------------
# 导出 HTML
# ---------------------------------------------------------------------------


def _collect(focus: str, depth: int, limit: int) -> tuple[list[dict], list[dict]]:
    """收集节点和边。``focus`` 为空就导全图（可能很大，按 limit 截断）。"""
    store = get_store()

    if not focus:
        # 全图：**先取边再筛**。反过来（先按 limit 截断节点、再筛两端都在的边）
        # 会把绝大多数边筛掉 —— 截断后的节点集里几乎没有成对的端点，
        # 导出的图看着全是孤立的点，还以为是图坏了。
        edges = [dict(row) for row in store.conn.execute("SELECT * FROM edge")]
        ids = {e["src"] for e in edges} | {e["dst"] for e in edges}
        nodes = [
            dict(row)
            for row in store.conn.execute("SELECT * FROM node ORDER BY kind, name")
            if row["id"] in ids
        ]
        # 没有边的节点（孤立项目节点等）也带上，不然它们凭空消失
        covered = {n["id"] for n in nodes}
        for row in store.conn.execute("SELECT * FROM node"):
            if row["id"] not in covered:
                nodes.append(dict(row))
        # limit=0 表示不限。**默认就是不限** —— 截断节点必然连带截断边，
        # 导出来的图会缺掉大半个结构，看着像图谱本身有问题。
        if limit and len(nodes) > limit:
            nodes = nodes[:limit]
            keep = {n["id"] for n in nodes}
            edges = [e for e in edges if e["src"] in keep and e["dst"] in keep]
        return nodes, edges

    # 以某个节点为种子做 BFS
    seed = focus
    if store.get_node(seed) is None:
        # 允许只写名字，比如 tz_area / PUT /admin/area
        candidates = [
            r["id"]
            for r in store.conn.execute(
                "SELECT id FROM node WHERE name = ? OR id = ? LIMIT 5", (focus, focus)
            )
        ]
        if not candidates:
            raise SystemExit(f"没找到节点 {focus}")
        seed = candidates[0]

    seen = {seed}
    frontier = [seed]
    edges: list[dict] = []
    for _ in range(max(0, depth)):
        next_frontier: list[str] = []
        for node_id in frontier:
            for edge in store.edges_of(node_id):
                edges.append(dict(edge))
                other = edge["dst"] if edge["src"] == node_id else edge["src"]
                if other not in seen:
                    seen.add(other)
                    next_frontier.append(other)
        frontier = next_frontier
        if not frontier or len(seen) > limit:
            break

    nodes = [store.get_node(i) for i in seen]
    return [n for n in nodes if n], edges


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>知识图谱 · __TITLE__</title>
<style>
  :root { --bg:#0f172a; --panel:#1e293b; --text:#e2e8f0; --muted:#94a3b8; }
  * { box-sizing: border-box; }
  body { margin:0; font:14px/1.5 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
         background:var(--bg); color:var(--text); display:flex; height:100vh; }
  #side { width:300px; background:var(--panel); padding:16px; overflow-y:auto;
          border-right:1px solid #334155; flex-shrink:0; }
  #side h1 { font-size:15px; margin:0 0 4px; }
  #side .sub { color:var(--muted); font-size:12px; margin-bottom:16px; }
  #search { width:100%; padding:8px; border-radius:6px; border:1px solid #475569;
            background:#0f172a; color:var(--text); font-size:13px; }
  #legend { margin-top:16px; }
  .legend-row { display:flex; align-items:center; gap:8px; padding:3px 0;
                font-size:12px; color:var(--muted); cursor:pointer; }
  .legend-row.off { opacity:.35; }
  .dot { width:10px; height:10px; border-radius:50%; flex-shrink:0; }
  #detail { margin-top:16px; font-size:12px; color:var(--muted); }
  #detail pre { white-space:pre-wrap; word-break:break-all; background:#0f172a;
                padding:8px; border-radius:6px; font-size:11px; }
  #graph { flex:1; position:relative; }
  canvas { display:block; width:100%; height:100%; cursor:grab; }
  canvas.dragging { cursor:grabbing; }
  #hint { position:absolute; bottom:12px; left:12px; color:var(--muted);
          font-size:12px; background:rgba(15,23,42,.85); padding:6px 10px;
          border-radius:6px; }
</style>
</head>
<body>
<div id="side">
  <h1>知识图谱</h1>
  <div class="sub">__SUBTITLE__</div>
  <input id="search" placeholder="搜节点名…" autocomplete="off">
  <div id="legend"></div>
  <div id="detail">点一个节点看详情</div>
</div>
<div id="graph">
  <canvas id="cv"></canvas>
  <div id="hint">滚轮缩放 ｜ 拖拽平移 ｜ 拖动节点 ｜ 点节点看详情</div>
</div>
<script>
const DATA = __DATA__;
const COLORS = __COLORS__;
const LABELS = __LABELS__;

const cv = document.getElementById('cv');
const ctx = cv.getContext('2d');
const hidden = new Set();

// ---- 布局：简单的力导向（Fruchterman-Reingold 的轻量版） ----
let W = 0, H = 0, DPR = window.devicePixelRatio || 1;
function resize() {
  const rect = cv.getBoundingClientRect();
  W = rect.width; H = rect.height;
  cv.width = W * DPR; cv.height = H * DPR;
  ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
}
window.addEventListener('resize', () => { resize(); });

const nodes = DATA.nodes.map((n, i) => {
  const a = (i / Math.max(1, DATA.nodes.length)) * Math.PI * 2;
  return { ...n, x: Math.cos(a) * 200, y: Math.sin(a) * 200, vx: 0, vy: 0 };
});
const index = new Map(nodes.map(n => [n.id, n]));
const edges = DATA.edges
  .map(e => ({ ...e, s: index.get(e.src), t: index.get(e.dst) }))
  .filter(e => e.s && e.t);

let scale = 1, ox = 0, oy = 0;
let dragging = null, panning = false, last = null, hovered = null;

function visible(n) { return !hidden.has(n.kind); }

function tick() {
  const visibleNodes = nodes.filter(visible);
  const k = Math.sqrt((W * H) / Math.max(1, visibleNodes.length)) * 0.45;

  // 斥力
  for (let i = 0; i < visibleNodes.length; i++) {
    const a = visibleNodes[i];
    for (let j = i + 1; j < visibleNodes.length; j++) {
      const b = visibleNodes[j];
      let dx = a.x - b.x, dy = a.y - b.y;
      let d2 = dx * dx + dy * dy || 0.01;
      if (d2 > 400000) continue;
      const d = Math.sqrt(d2);
      const f = (k * k) / d2;
      const fx = (dx / d) * f, fy = (dy / d) * f;
      a.vx += fx; a.vy += fy; b.vx -= fx; b.vy -= fy;
    }
  }
  // 引力（沿边）
  for (const e of edges) {
    if (!visible(e.s) || !visible(e.t)) continue;
    const dx = e.t.x - e.s.x, dy = e.t.y - e.s.y;
    const d = Math.sqrt(dx * dx + dy * dy) || 0.01;
    const f = (d * d) / k * 0.02;
    const fx = (dx / d) * f, fy = (dy / d) * f;
    e.s.vx += fx; e.s.vy += fy; e.t.vx -= fx; e.t.vy -= fy;
  }
  // 向心 + 阻尼
  for (const n of visibleNodes) {
    n.vx -= n.x * 0.002; n.vy -= n.y * 0.002;
    n.vx *= 0.85; n.vy *= 0.85;
    const speed = Math.hypot(n.vx, n.vy);
    if (speed > 20) { n.vx = n.vx / speed * 20; n.vy = n.vy / speed * 20; }
    n.x += n.vx; n.y += n.vy;
  }
}

function draw() {
  ctx.clearRect(0, 0, W, H);
  ctx.save();
  ctx.translate(W / 2 + ox, H / 2 + oy);
  ctx.scale(scale, scale);

  // 边
  for (const e of edges) {
    if (!visible(e.s) || !visible(e.t)) continue;
    const alpha = e.confidence == null ? 0.12 : (0.08 + e.confidence * 0.22);
    ctx.strokeStyle = `rgba(148,163,184,${alpha})`;
    ctx.lineWidth = 1 / scale;
    ctx.beginPath();
    ctx.moveTo(e.s.x, e.s.y);
    ctx.lineTo(e.t.x, e.t.y);
    ctx.stroke();
  }

  // 节点
  for (const n of nodes) {
    if (!visible(n)) continue;
    const r = (n.kind === 'endpoint' || n.kind === 'table') ? 6 : 4;
    ctx.beginPath();
    ctx.arc(n.x, n.y, r, 0, Math.PI * 2);
    ctx.fillStyle = COLORS[n.kind] || '#9ca3af';
    ctx.fill();
    if (n === hovered) {
      ctx.strokeStyle = '#fff'; ctx.lineWidth = 2 / scale; ctx.stroke();
    }
  }

  // 标签：只在放大到一定程度时画，不然糊成一团
  if (scale > 1.4) {
    ctx.fillStyle = 'rgba(226,232,240,.85)';
    ctx.font = `${11 / scale}px -apple-system,"Segoe UI",sans-serif`;
    for (const n of nodes) {
      if (!visible(n)) continue;
      ctx.fillText(n.name, n.x + 7 / scale, n.y + 4 / scale);
    }
  }
  ctx.restore();
}

function loop() { tick(); draw(); requestAnimationFrame(loop); }

// ---- 交互 ----
function toWorld(clientX, clientY) {
  const rect = cv.getBoundingClientRect();
  return {
    x: (clientX - rect.left - W / 2 - ox) / scale,
    y: (clientY - rect.top - H / 2 - oy) / scale,
  };
}
function pick(clientX, clientY) {
  const p = toWorld(clientX, clientY);
  let best = null, bestD = 14 / scale;
  for (const n of nodes) {
    if (!visible(n)) continue;
    const d = Math.hypot(n.x - p.x, n.y - p.y);
    if (d < bestD) { bestD = d; best = n; }
  }
  return best;
}

cv.addEventListener('mousedown', e => {
  const hit = pick(e.clientX, e.clientY);
  if (hit) { dragging = hit; }
  else { panning = true; }
  last = { x: e.clientX, y: e.clientY };
  cv.classList.add('dragging');
});
window.addEventListener('mousemove', e => {
  if (dragging) {
    const p = toWorld(e.clientX, e.clientY);
    dragging.x = p.x; dragging.y = p.y;
    dragging.vx = 0; dragging.vy = 0;
  } else if (panning && last) {
    ox += e.clientX - last.x; oy += e.clientY - last.y;
    last = { x: e.clientX, y: e.clientY };
  } else {
    hovered = pick(e.clientX, e.clientY);
    cv.style.cursor = hovered ? 'pointer' : 'grab';
  }
});
window.addEventListener('mouseup', e => {
  if (dragging && last && Math.hypot(e.clientX - last.x, e.clientY - last.y) < 3) {
    showDetail(dragging);
  }
  dragging = null; panning = false; last = null;
  cv.classList.remove('dragging');
});
cv.addEventListener('wheel', e => {
  e.preventDefault();
  const before = toWorld(e.clientX, e.clientY);
  scale *= e.deltaY < 0 ? 1.12 : 1 / 1.12;
  scale = Math.max(0.15, Math.min(6, scale));
  const after = toWorld(e.clientX, e.clientY);
  ox += (after.x - before.x) * scale;
  oy += (after.y - before.y) * scale;
}, { passive: false });

// ---- 侧栏 ----
function showDetail(n) {
  const props = n.props && Object.keys(n.props).length
    ? JSON.stringify(n.props, null, 2) : '（无）';
  document.getElementById('detail').innerHTML =
    `<b style="color:${COLORS[n.kind]}">${LABELS[n.kind] || n.kind}</b> ` +
    `<span style="color:var(--text)">${n.name}</span><br>` +
    `<code style="font-size:11px">${n.id}</code><pre>${props}</pre>`;
}

const legend = document.getElementById('legend');
const counts = {};
for (const n of nodes) counts[n.kind] = (counts[n.kind] || 0) + 1;
for (const kind of Object.keys(LABELS)) {
  if (!counts[kind]) continue;
  const row = document.createElement('div');
  row.className = 'legend-row';
  row.innerHTML = `<span class="dot" style="background:${COLORS[kind]}"></span>` +
                  `${LABELS[kind]} <span style="margin-left:auto">${counts[kind]}</span>`;
  row.onclick = () => {
    if (hidden.has(kind)) { hidden.delete(kind); row.classList.remove('off'); }
    else { hidden.add(kind); row.classList.add('off'); }
  };
  legend.appendChild(row);
}

document.getElementById('search').addEventListener('input', e => {
  const q = e.target.value.trim().toLowerCase();
  if (!q) return;
  const hit = nodes.find(n => n.name.toLowerCase().includes(q));
  if (hit) {
    ox = -hit.x * scale; oy = -hit.y * scale;
    showDetail(hit);
  }
});

resize();
loop();
</script>
</body>
</html>
"""


def cmd_export(args: argparse.Namespace) -> int:
    nodes, edges = _collect(args.focus, args.depth, args.limit)
    if not nodes:
        print("没有可导出的节点")
        return 1

    data = {
        "nodes": [
            {
                "id": n["id"],
                "kind": n["kind"],
                "name": n["name"],
                "props": _as_props(n.get("props")),
            }
            for n in nodes
        ],
        "edges": [
            {"src": e["src"], "dst": e["dst"], "kind": e["kind"],
             "confidence": e.get("confidence")}
            for e in edges
        ],
    }

    title = args.focus or "全图"
    subtitle = f"{len(nodes)} 节点 / {len(edges)} 边"
    if args.focus:
        subtitle += f"（从 {args.focus} 展开 {args.depth} 跳）"

    page = (
        _HTML_TEMPLATE.replace("__TITLE__", html.escape(title))
        .replace("__SUBTITLE__", html.escape(subtitle))
        .replace("__DATA__", json.dumps(data, ensure_ascii=False))
        .replace("__COLORS__", json.dumps({k: v[1] for k, v in KIND_LABELS.items()}, ensure_ascii=False))
        .replace("__LABELS__", json.dumps({k: v[0] for k, v in KIND_LABELS.items()}, ensure_ascii=False))
    )

    out = Path(args.output)
    if not out.is_absolute():
        out = PROJECT_ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")

    print(f"已导出 {len(nodes)} 节点 / {len(edges)} 边 → {out}")
    print("双击用浏览器打开即可（单文件，不依赖网络）")
    return 0


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="查看知识图谱")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("stats", help="全局统计")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("endpoint", help="接口全景：字段映射 + 覆盖用例")
    p.add_argument("query", help='如 "PUT /admin/area" 或 "/admin/area"')
    p.set_defaults(func=cmd_endpoint)

    p = sub.add_parser("table", help="一张表的列和写它的接口")
    p.add_argument("query", help="表名，如 tz_area")
    p.set_defaults(func=cmd_table)

    p = sub.add_parser("field", help="一个字段在所有接口上的映射")
    p.add_argument("query", help="字段名，如 areaName")
    p.set_defaults(func=cmd_field)

    p = sub.add_parser("neighbors", help="看某个节点的邻居")
    p.add_argument("query", help="节点 ID，如 table:tz_area")
    p.add_argument("--limit", type=int, default=100)
    p.set_defaults(func=cmd_neighbors)

    p = sub.add_parser("search", help="全文搜节点")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("export", help="导出可交互 HTML")
    p.add_argument("--focus", default="", help="只导出这个节点附近（名字或 ID）")
    p.add_argument("--depth", type=int, default=2, help="展开几跳（默认 2）")
    p.add_argument(
        "--limit", type=int, default=0, help="节点数上限（默认 0 = 不限，导全图）"
    )
    p.add_argument("--output", default="reports/graph.html")
    p.set_defaults(func=cmd_export)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
