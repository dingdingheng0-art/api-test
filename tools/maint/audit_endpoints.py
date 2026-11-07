"""审计用例里的接口路径：跟服务端 swagger 对一遍，分清「用例写错」和「服务端坏了」。

**为什么要有这个**

自动生成的用例里，接口路径是照着文档抄的。文档和线上对不上是常态 ——
但对不上的表现有三种，后果完全不同：

1. **路径根本不存在** —— 用例写错了，或者文档过时了。改用例就能修好。
2. **路径存在，但服务端自己抛异常**（``A00005 服务器出了点小差``）——
   这是**被测系统的 bug**，改用例没用，标成已知问题就行。
3. **路径存在、方法不对** —— 比如服务只支持 POST，用例写的是 PUT。

不把这三件事分开，就会把"我们系统的 bug"当成"用例没调通"，
或者反过来 —— 明明是服务端坏了，还在用例上反复改。

**产出**

控制台一份审计报告，外加 ``tools/maint/mall4j_endpoints.yaml``（接口清单）。

**用法**

.. code-block:: text

    python tools/maint/audit_endpoints.py                    # 审计 + 打印报告
    python tools/maint/audit_endpoints.py --write-manifest   # 顺便写接口清单
    python tools/maint/audit_endpoints.py --fix-paths        # 自动把路径改成服务端的写法
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import yaml  # noqa: E402

MANIFEST = Path(__file__).resolve().parent / "mall4j_endpoints.yaml"

#: 服务端表示"自己抛异常了"的错误码
SERVER_ERROR_CODES = ("A00005",)

#: 服务端表示"没登录/登录态失效"的错误码
UNAUTHORIZED_CODES = ("A00004",)

#: 占位符里的参数名（swagger 里是 ``{id}`` 这种）
_PLACEHOLDER = re.compile(r"\{[^}]+\}")


def fetch_spec(base: str, token: str) -> dict[str, Any]:
    """拉服务端的 swagger 规格。"""
    import requests

    resp = requests.get(
        f"{base.rstrip('/')}/v3/api-docs",
        headers={"Authorization": token, "Accept": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def match_template(path: str, templates: list[str]) -> str | None:
    """把实际路径匹配到 swagger 里的模板。

    ``/admin/area/info/1`` → ``/admin/area/info/{id}``
    """
    if path in templates:
        return path

    parts = path.split("/")
    for tmpl in templates:
        tparts = tmpl.split("/")
        if len(tparts) != len(parts):
            continue
        ok = True
        for tp, p in zip(tparts, parts):
            if _PLACEHOLDER.fullmatch(tp):
                continue
            if tp != p:
                ok = False
                break
        if ok:
            return tmpl
    return None


def load_excel_cases(root: Path) -> list[dict[str, Any]]:
    """读所有 Excel 用例，返回 ``[{模块, 文件, 路径, 方法, 标题}, ...]``。"""
    import openpyxl

    cases: list[dict[str, Any]] = []
    if not root.exists():
        return cases

    for xlsx in sorted(root.rglob("*.xlsx")):
        try:
            wb = openpyxl.load_workbook(xlsx, read_only=True)
        except Exception:  # noqa: BLE001
            continue
        if "接口测试用例" not in wb.sheetnames:
            wb.close()
            continue
        ws = wb["接口测试用例"]
        rows = list(ws.iter_rows(values_only=True))
        wb.close()
        if not rows:
            continue

        header = [str(h or "") for h in rows[0]]
        try:
            i_path = header.index("接口")
            i_method = header.index("请求方法")
        except ValueError:
            continue
        i_title = header.index("用例标题") if "用例标题" in header else None

        for row in rows[1:]:
            path = row[i_path]
            if not path:
                continue
            cases.append({
                "module": xlsx.parent.name,
                "file": str(xlsx),
                "path": str(path),
                "method": str(row[i_method] or "").upper(),
                "title": str(row[i_title] or "") if i_title is not None else "",
            })
    return cases


def probe(server: str, token: str, method: str, path: str, timeout: int = 15) -> str:
    """探一下这个接口现在返回什么（只做一次，不重试）。"""
    import requests

    url = server.rstrip("/") + path
    try:
        resp = requests.request(
            method,
            url,
            headers={"Authorization": token, "Accept": "application/json"},
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001
        return f"EXC:{str(exc)[:40]}"

    try:
        body = resp.json()
    except ValueError:
        return f"HTTP:{resp.status_code}"

    if isinstance(body, dict):
        if body.get("success") is True:
            return "OK"
        code = str(body.get("code") or "?")
        msg = str(body.get("msg") or "")
        return f"{code} {msg}".strip()
    return "OK"


def main() -> int:
    parser = argparse.ArgumentParser(description="审计用例里的接口路径")
    parser.add_argument("--project", default="mall4j", help="项目名（默认 mall4j）")
    parser.add_argument("--env", default="test", help="环境（默认 test）")
    parser.add_argument("--write-manifest", action="store_true", help="写接口清单文件")
    parser.add_argument("--probe", action="store_true", help="逐条探测服务端实际返回")
    args = parser.parse_args()

    from utils.config.config_manager import ConfigManager
    from utils.config.project_profile import ProjectRegistry

    profile = ProjectRegistry.require(args.project, args.env)
    server = profile.host.rstrip("/")
    if not server.endswith("/_admin"):
        server += "/_admin"

    cfg = ConfigManager.get_project_env_config(args.project, args.env)
    from testcases.auth import Auth

    token = Auth().login_and_store(args.project, cfg[args.project], args.env)
    if not token:
        print("登录失败，退出")
        return 1

    spec = fetch_spec(server, token)
    paths: dict[str, Any] = spec.get("paths") or {}
    templates = list(paths)

    cases = load_excel_cases(Path("testcases/cases_excel") / args.project)
    print(f"服务端接口 {len(paths)} 条，用例 {len(cases)} 条\n")

    # ---- 分类 ----
    unknown: list[dict[str, Any]] = []
    bad_method: list[dict[str, Any]] = []
    ok_cases: list[dict[str, Any]] = []
    server_broken: list[dict[str, Any]] = []

    for case in cases:
        tmpl = match_template(case["path"], templates)
        if tmpl is None:
            unknown.append(case)
            continue
        methods = {
            m.upper() for m in paths[tmpl] if m.lower() in ("get", "post", "put", "delete")
        }
        if case["method"] not in methods:
            case["swagger_methods"] = sorted(methods)
            bad_method.append(case)
            continue
        ok_cases.append(case)

    if args.probe:
        print("逐条探测服务端返回（慢一点，但能分清是谁的问题）...\n")
        for case in ok_cases:
            result = probe(server, token, case["method"], case["path"])
            if result.startswith(SERVER_ERROR_CODES):
                case["probe"] = result
                server_broken.append(case)
            elif result.startswith(UNAUTHORIZED_CODES):
                case["probe"] = result
            else:
                case["probe"] = result

    def dump(title: str, items: list[dict[str, Any]]) -> None:
        print(f"=== {title}（{len(items)} 条）===")
        for c in items:
            extra = ""
            if "swagger_methods" in c:
                extra = f"   服务端支持: {c['swagger_methods']}"
            if "probe" in c:
                extra += f"   实际返回: {c['probe']}"
            print(f"  {c['method']:6} {c['path']:38} [{c['module']}]{extra}")
        print()

    dump("路径在服务端不存在（用例写错了，或文档过时）", unknown)
    dump("路径存在但方法不对", bad_method)
    if args.probe:
        dump("服务端自己抛异常（被测系统的 bug，用例无过错）", server_broken)

    print(f"路径与方法都对：{len(ok_cases)} 条")

    if args.write_manifest:
        manifest = {
            tmpl: {
                "methods": sorted(
                    m.upper() for m in ops if m.lower() in ("get", "post", "put", "delete")
                ),
                "parameters": [
                    {
                        "name": p.get("name"),
                        "in": p.get("in"),
                        "required": p.get("required"),
                    }
                    for m in ops
                    for p in (ops[m].get("parameters") or [])
                ],
            }
            for tmpl, ops in paths.items()
        }
        MANIFEST.write_text(
            yaml.safe_dump(manifest, allow_unicode=True, sort_keys=True),
            encoding="utf-8",
        )
        print(f"\n接口清单已写入 {MANIFEST}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
