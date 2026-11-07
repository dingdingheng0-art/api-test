"""本地网页上传页：拖文件 → 解析预览 → 一键导出测试用例。

不装任何第三方库（标准库 ``http.server`` 就够），
所以不用改 requirements.txt，双击就能跑。

启动::

    python -m tools.importer.web

然后浏览器打开 http://127.0.0.1:8770

**为什么用 JSON 上传而不是 multipart**
浏览器先把文件读成文本，再 POST JSON —— 省掉手写 multipart 解析，
也不用装 ``python-multipart``。
"""
from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from tools.importer.case_builder import export_excel, write_cases  # noqa: E402
from tools.importer.dispatcher import (  # noqa: E402
    SUPPORTED,
    DocumentParseError,
    parse_document,
    summary_of,
)
from tools.importer.models import Endpoint  # noqa: E402
from utils import paths  # noqa: E402

PAGE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>接口文档导入 · 测试用例生成</title>
<style>
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 0; font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
    background: #f5f6f8; color: #24292f;
  }
  .wrap { max-width: 1180px; margin: 0 auto; padding: 24px 20px 60px; }
  h1 { font-size: 20px; margin: 0 0 4px; }
  .sub { color: #656d76; font-size: 13px; margin-bottom: 20px; }
  .card {
    background: #fff; border: 1px solid #d0d7de; border-radius: 8px;
    padding: 18px; margin-bottom: 16px;
  }
  .drop {
    border: 2px dashed #c9d1d9; border-radius: 8px; padding: 34px 20px;
    text-align: center; cursor: pointer; transition: all .15s; background: #fbfcfd;
  }
  .drop:hover, .drop.over { border-color: #1f6feb; background: #f0f6ff; }
  .drop b { color: #1f6feb; }
  .hint { font-size: 12px; color: #656d76; margin-top: 8px; }
  .row { display: flex; gap: 12px; flex-wrap: wrap; align-items: flex-end; }
  .field { display: flex; flex-direction: column; gap: 5px; }
  .field label { font-size: 12px; color: #656d76; }
  input[type=text], input[type=number], select {
    padding: 7px 10px; border: 1px solid #d0d7de; border-radius: 6px;
    font-size: 13px; min-width: 150px; background: #fff;
  }
  button {
    padding: 8px 18px; border-radius: 6px; border: 1px solid #1f6feb;
    background: #1f6feb; color: #fff; font-size: 13px; cursor: pointer;
  }
  button:hover { background: #1a63d8; }
  button.ghost { background: #fff; color: #1f6feb; }
  button:disabled { opacity: .5; cursor: not-allowed; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 7px 9px; border-bottom: 1px solid #eaeef2; }
  th { background: #f6f8fa; font-weight: 600; color: #57606a; }
  .tag {
    display: inline-block; padding: 1px 7px; border-radius: 10px;
    font-size: 11px; font-weight: 600;
  }
  .m-GET { background: #ddf4ff; color: #0969da; }
  .m-POST { background: #dafbe1; color: #1a7f37; }
  .m-PUT { background: #fff8c5; color: #9a6700; }
  .m-DELETE { background: #ffebe9; color: #cf222e; }
  .m-PATCH { background: #ddf4ff; color: #0969da; }
  .stats { display: flex; gap: 22px; flex-wrap: wrap; margin: 10px 0; font-size: 13px; }
  .stats b { font-size: 17px; }
  .msg { padding: 10px 14px; border-radius: 6px; margin: 10px 0; font-size: 13px; }
  .msg.ok { background: #dafbe1; color: #1a7f37; }
  .msg.err { background: #ffebe9; color: #cf222e; }
  .hidden { display: none; }
  code, pre { font-family: ui-monospace, Consolas, monospace; }
  pre {
    background: #f6f8fa; padding: 10px; border-radius: 6px; overflow: auto;
    font-size: 12px; max-height: 260px;
  }
  .filters { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 10px; }
  .filters button { padding: 4px 12px; font-size: 12px; }
  .filters button.on { background: #1f6feb; color: #fff; }
</style>
</head>
<body>
<div class="wrap">
  <h1>接口文档导入 → 测试用例生成</h1>
  <div class="sub">支持 Swagger / OpenAPI、HAR 抓包、cURL 命令。上传后自动解析成用例，可导出 YAML 或 Excel。</div>

  <div class="card">
    <div class="drop" id="drop">
      <div>把文件拖到这里，或 <b>点击选择</b></div>
      <div class="hint">支持 .json / .yaml / .yml（Swagger、OpenAPI）、.har（浏览器抓包）、.txt（cURL 命令）</div>
      <input type="file" id="file" style="display:none" accept=".json,.yaml,.yml,.har,.txt,.sh,.curl">
    </div>
    <div id="msg"></div>
  </div>

  <div class="card hidden" id="cfg">
    <div class="row">
      <div class="field">
        <label>项目名（用例放在 cases/&lt;项目&gt;/ 下）</label>
        <input type="text" id="project" value="__DEFAULT_PROJECT__">
      </div>
      <div class="field">
        <label>报告分组（epic）</label>
        <input type="text" id="epic" value="接口文档导入">
      </div>
      <div class="field">
        <label>鉴权请求头（JSON，可留空）</label>
        <input type="text" id="auth" value='{"Authorization": "{{mall4j.token}}"}' style="min-width:380px">
      </div>
    </div>
    <div class="row" style="margin-top:14px">
      <button id="btnYaml">导出 YAML 用例</button>
      <button id="btnExcel">导出 Excel 用例</button>
      <button class="ghost" id="btnRedo">重新上传</button>
    </div>
  </div>

  <div class="card hidden" id="preview">
    <div id="stats" class="stats"></div>
    <div class="filters" id="filters"></div>
    <table id="tbl">
      <thead><tr>
        <th style="width:70px">方法</th><th style="width:240px">路径</th><th>接口名</th>
        <th style="width:130px">模块</th><th style="width:90px">参数</th><th style="width:80px">请求体</th>
        <th style="width:80px">响应示例</th>
      </tr></thead>
      <tbody></tbody>
    </table>
  </div>
</div>

<script>
let parsed = null;      // {filename, endpoints:[...], summary:{}}
let curModule = "全部";

function show(text, cls) {
  document.getElementById("msg").innerHTML =
    '<div class="msg ' + (cls||"") + '">' + text + "</div>";
}
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"]/g, c =>
    ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
}

// ---------- 上传 & 解析 ----------
async function handleFile(file) {
  if (!file) return;
  const text = await file.text();
  try {
    const resp = await fetch("/api/parse", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({filename: file.name, content: text})
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || "解析失败");
    parsed = data;
    show("解析成功：识别到 <b>" + data.summary.total + "</b> 个接口（" + esc(data.fmtLabel) + "）", "ok");
    renderPreview();
    document.getElementById("cfg").classList.remove("hidden");
  } catch (e) {
    show("解析失败：" + esc(e.message), "err");
  }
}

document.getElementById("drop").onclick = () => document.getElementById("file").click();
document.getElementById("file").onchange = e => handleFile(e.target.files[0]);

const drop = document.getElementById("drop");
["dragenter","dragover"].forEach(ev => drop.addEventListener(ev, e => {
  e.preventDefault(); drop.classList.add("over");
}));
["dragleave","drop"].forEach(ev => drop.addEventListener(ev, e => {
  e.preventDefault(); drop.classList.remove("over");
}));
drop.addEventListener("drop", e => handleFile(e.dataTransfer.files[0]));

// ---------- 预览 ----------
function renderPreview() {
  const s = parsed.summary;
  document.getElementById("stats").innerHTML =
    "<span>接口 <b>" + s.total + "</b></span>" +
    "<span>模块 <b>" + Object.keys(s.modules).length + "</b></span>" +
    "<span>带响应示例 <b>" + s.with_response + "</b></span>" +
    "<span>带请求体 <b>" + s.with_body + "</b></span>";

  // 模块过滤按钮
  const names = ["全部"].concat(Object.keys(s.modules));
  document.getElementById("filters").innerHTML = names.map(n =>
    '<button class="' + (n === curModule ? "on" : "") + '" data-m="' + esc(n) + '">' +
    esc(n) + (n === "全部" ? "" : " (" + s.modules[n] + ")") + "</button>").join("");
  document.querySelectorAll("#filters button").forEach(b => {
    b.onclick = () => { curModule = b.dataset.m; renderPreview(); };
  });

  const rows = parsed.endpoints.filter(e => curModule === "全部" || e.module === curModule);
  document.querySelector("#tbl tbody").innerHTML = rows.map(e => `
    <tr>
      <td><span class="tag m-${esc(e.method)}">${esc(e.method)}</span></td>
      <td><code>${esc(e.path)}</code></td>
      <td>${esc(e.name)}
        ${e.description && e.description !== e.name ? "<div class='hint'>" + esc(e.description.slice(0,80)) + "</div>" : ""}
      </td>
      <td>${esc(e.module)}</td>
      <td>${e.query.length + e.path_params.length}</td>
      <td>${e.body ? "有" : "-"}</td>
      <td>${e.response_example ? "有" : "-"}</td>
    </tr>`).join("");
  document.getElementById("preview").classList.remove("hidden");
}

// ---------- 导出 ----------
async function exportCases(kind) {
  if (!parsed) return;
  const authText = document.getElementById("auth").value.trim();
  let auth = {};
  if (authText) {
    try { auth = JSON.parse(authText); }
    catch (e) { show("鉴权头不是合法 JSON：" + esc(e.message), "err"); return; }
  }
  const payload = {
    filename: parsed.filename,
    endpoints: parsed.endpoints,
    project: document.getElementById("project").value.trim() || "mall4j",
    epic: document.getElementById("epic").value.trim() || "接口文档导入",
    auth: auth,
    kind: kind
  };
  try {
    const resp = await fetch("/api/export", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload)
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || "导出失败");
    show("导出完成：" + esc(data.message), "ok");
  } catch (e) {
    show("导出失败：" + esc(e.message), "err");
  }
}
document.getElementById("btnYaml").onclick = () => exportCases("yaml");
document.getElementById("btnExcel").onclick = () => exportCases("excel");
document.getElementById("btnRedo").onclick = () => {
  parsed = null;
  document.getElementById("cfg").classList.add("hidden");
  document.getElementById("preview").classList.add("hidden");
  document.getElementById("msg").innerHTML = "";
  document.getElementById("file").value = "";
};
</script>
</body>
</html>
"""


def _endpoints_from_payload(items: list[dict[str, Any]]) -> list[Endpoint]:
    """把网页传来的 JSON 还原成 Endpoint 对象。"""
    from tools.importer.models import Param

    endpoints: list[Endpoint] = []
    for item in items:
        ep = Endpoint(
            id=item.get("id", ""),
            method=item.get("method", "GET"),
            path=item.get("path", "/"),
            name=item.get("name", ""),
            summary=item.get("summary", ""),
            description=item.get("description", ""),
            tags=item.get("tags") or [],
            base_url=item.get("base_url", ""),
            body=item.get("body"),
            body_type=item.get("body_type", ""),
            response_example=item.get("response_example"),
            source=item.get("source", ""),
        )
        for key, target in (
            ("headers", ep.headers),
            ("query", ep.query),
            ("path_params", ep.path_params),
            ("body_params", ep.body_params),
        ):
            for p in item.get(key) or []:
                target.append(
                    Param(
                        name=p.get("name", ""),
                        location=p.get("location", key),
                        required=bool(p.get("required")),
                        type=p.get("type", "string"),
                        example=p.get("example"),
                        description=p.get("description", ""),
                        enum=p.get("enum") or [],
                        fake=bool(p.get("fake")),
                    )
                )
        endpoints.append(ep)
    return endpoints


class Handler(BaseHTTPRequestHandler):
    """处理网页请求。"""

    server_version = "DocImporter/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        # 安静一点，只在出错时打印
        pass

    # ---------------- 路由 ----------------

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            self._send_html(self._render_page())
        elif self.path == "/api/formats":
            self._send_json({
                "formats": [
                    {"key": key, "label": label, "exts": list(exts)}
                    for key, (label, exts) in SUPPORTED.items()
                ]
            })
        else:
            self._send_json({"error": "not found"}, status=404)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            self._send_json({"error": f"请求不是合法 JSON：{exc}"}, status=400)
            return

        if self.path == "/api/parse":
            self._handle_parse(payload)
        elif self.path == "/api/export":
            self._handle_export(payload)
        else:
            self._send_json({"error": "not found"}, status=404)

    # ---------------- 处理 ----------------

    def _handle_parse(self, payload: dict[str, Any]) -> None:
        filename = str(payload.get("filename") or "doc.json")
        content = str(payload.get("content") or "")
        if not content.strip():
            self._send_json({"error": "文件是空的"}, status=400)
            return

        try:
            endpoints = parse_document(filename, content)
        except DocumentParseError as exc:
            self._send_json({"error": str(exc)}, status=400)
            return
        except Exception as exc:  # noqa: BLE001
            self._send_json({"error": f"解析出错：{exc}"}, status=500)
            return

        if not endpoints:
            self._send_json(
                {"error": "文档里没解析出任何接口 —— 检查一下是不是 Swagger/OpenAPI/HAR/cURL 格式"},
                status=400,
            )
            return

        fmt_key = payload.get("fmt") or ""
        label = SUPPORTED.get(fmt_key, ("接口文档",))[0]

        self._send_json({
            "filename": filename,
            "fmtLabel": label,
            "summary": summary_of(endpoints),
            "endpoints": [ep.to_dict() for ep in endpoints],
        })

    def _handle_export(self, payload: dict[str, Any]) -> None:
        from utils.config.project_profile import ProjectRegistry

        kind = str(payload.get("kind") or "yaml")
        default_project = ProjectRegistry.default_project()
        project = str(payload.get("project") or default_project).strip() or default_project
        epic = str(payload.get("epic") or "接口文档导入").strip() or "接口文档导入"
        auth = payload.get("auth") or {}
        if not isinstance(auth, dict):
            auth = {}

        endpoints = _endpoints_from_payload(payload.get("endpoints") or [])
        if not endpoints:
            self._send_json({"error": "没有可导出的接口"}, status=400)
            return

        try:
            if kind == "excel":
                # Excel 用例要落到"会被扫描到"的目录 ——
                # 否则导进去了却跑不起来（最坑的一种失败）。
                from utils.cases.case_source import current_excel_dir

                output = current_excel_dir() / project / f"{project}_imported.xlsx"
                count = export_excel(endpoints, output, project, epic, auth)
                message = f"{count} 条用例 → {output}"
            else:
                # YAML 显式落 YAML 目录（不管当前是不是 Excel 模式）
                written = write_cases(
                    endpoints, paths.CASES_ROOT, project, epic, auth, overwrite=True
                )
                modules = sorted({ep.module for ep in endpoints})
                message = (
                    f"{len(written)} 条用例 → {paths.CASES_ROOT / project} "
                    f"（模块：{', '.join(modules)}）"
                )
        except Exception as exc:  # noqa: BLE001
            self._send_json({"error": f"导出失败：{exc}"}, status=500)
            return

        self._send_json({"message": message, "count": len(endpoints)})

    # ---------------- 响应 ----------------

    def _send_json(self, data: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _render_page() -> str:
        """把页面模板里的占位符换成配置里的默认项目。

        项目名不能写死在 HTML 里 —— 换系统时只改配置就行。
        """
        from utils.config.project_profile import ProjectRegistry

        project = ProjectRegistry.default_project() or "my_project"
        html = PAGE_HTML.replace("__DEFAULT_PROJECT__", project)
        html = html.replace(
            '{{mall4j.token}}',
            f"{{{{{project}.token}}}}",
        )
        return html.replace('|| "mall4j"', f'|| "{project}"')


def serve(host: str = "127.0.0.1", port: int = 8770, open_browser: bool = True) -> int:
    """启动网页服务。"""
    server = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}"

    print("=" * 60)
    print("接口文档导入服务已启动")
    print(f"  地址: {url}")
    print("  按 Ctrl+C 停止")
    print("=" * 60)

    if open_browser:
        import webbrowser

        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="接口文档上传解析（本地网页）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()
    return serve(args.host, args.port, not args.no_browser)


if __name__ == "__main__":
    raise SystemExit(main())
