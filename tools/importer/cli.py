"""接口文档导入 → 测试用例生成。

用法::

    # 解析并预览（不落盘）
    python -m tools.importer.cli preview api.json

    # 导出 YAML 用例（落到 testcases/cases/<项目>/<模块>/）
    python -m tools.importer.cli export api.json --project mall4j

    # 导出 Excel 用例
    python -m tools.importer.cli export api.har --format excel

    # 起一个本地网页，拖文件上传
    python -m tools.importer.cli web
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from tools.importer.case_builder import export_excel, write_cases  # noqa: E402
from tools.importer.dispatcher import (  # noqa: E402
    SUPPORTED,
    DocumentParseError,
    parse_file,
    summary_of,
)
from utils import paths  # noqa: E402


def _default_project() -> str:
    """配置里的默认项目名。"""
    from utils.config.project_profile import ProjectRegistry

    return ProjectRegistry.default_project()


def default_auth_headers(project: str = "") -> dict[str, str]:
    """按项目档案生成默认的鉴权头。

    鉴权头叫什么、token 从哪个全局变量取，都在项目档案里，
    所以换一个系统不用改这里的代码。
    """
    from utils.config.project_profile import ProjectRegistry

    project = project or ProjectRegistry.default_project()
    profile = ProjectRegistry.get(project)
    if profile is None:
        return {
            "accesstoken": "{{{{project}}.token}}".format(project=project),
            "uuid": "{{{{project}}.uuid}}".format(project=project),
        }

    headers: dict[str, str] = {}
    if profile.token_header:
        headers[profile.token_header] = f"{{{{{project}.{profile.token_var}}}}}"
    if profile.session_header:
        headers[profile.session_header] = f"{{{{{project}.{profile.session_var}}}}}"
    return headers


def _print_preview(endpoints: list[Any], filename: str) -> None:
    summary = summary_of(endpoints)
    print(f"\n文件: {filename}")
    print(f"识别到 {summary['total']} 个接口")
    print(f"  带请求体: {summary['with_body']}   带响应示例: {summary['with_response']}")
    print("  模块分布: " + ", ".join(f"{k}({v})" for k, v in summary["modules"].items()))
    print("  方法分布: " + ", ".join(f"{k}({v})" for k, v in summary["methods"].items()))
    print()

    current = ""
    for ep in endpoints:
        if ep.module != current:
            current = ep.module
            print(f"【{current}】")
        params = len(ep.query) + len(ep.path_params)
        body_mark = "有" if ep.body else "-"
        resp_mark = "有" if ep.response_example else "-"
        print(f"  {ep.method:<7} {ep.path}")
        print(f"          {ep.display_name}  | 参数 {params} | 请求体 {body_mark} | 响应 {resp_mark}")


def cmd_preview(args: argparse.Namespace) -> int:
    try:
        endpoints = parse_file(args.file)
    except DocumentParseError as exc:
        print(f"解析失败: {exc}")
        return 1

    _print_preview(endpoints, args.file)

    if args.json:
        print()
        print(json.dumps(
            [ep.to_dict() for ep in endpoints], ensure_ascii=False, indent=2
        ))
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    try:
        endpoints = parse_file(args.file)
    except DocumentParseError as exc:
        print(f"解析失败: {exc}")
        return 1

    if not endpoints:
        print("文档里没解析出任何接口")
        return 1

    auth = dict(default_auth_headers(args.project))
    if args.no_auth:
        auth = {}

    project = args.project or _default_project()
    if args.format == "excel":
        # Excel 用例要落到"会被扫描到"的目录 ——
        # 否则导进去了却跑不起来（最坑的一种失败）。
        from utils.cases.case_source import current_excel_dir

        excel_root = current_excel_dir()
        output = Path(args.output) if args.output else (
            excel_root / project / f"{project}_imported.xlsx"
        )
        count = export_excel(endpoints, output, project, args.epic, auth)
        print(f"导出 {count} 条用例 → {output}")
        return 0

    # YAML 用例：显式指定 --format yaml 就是想产出 YAML，
    # 所以固定落 YAML 目录（不管当前是不是 Excel 模式）。
    yaml_root = paths.CASES_ROOT
    written = write_cases(
        endpoints, yaml_root, project, args.epic, auth, overwrite=args.overwrite
    )
    modules = sorted({ep.module for ep in endpoints})
    print(f"导出 {len(written)} 条用例 → {yaml_root / project}")
    print(f"  模块: {', '.join(modules)}")
    for path in written:
        print(f"  - {path.relative_to(paths.PROJECT_ROOT)}")
    print()
    print(f"跑起来: python scripts/run.py --project {project}")
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    from tools.importer.web import serve

    return serve(args.host, args.port, not args.no_browser)


def cmd_formats(_: argparse.Namespace) -> int:
    print("支持的文档格式：\n")
    for key, (label, exts) in SUPPORTED.items():
        print(f"  {label:<24} {' '.join(exts)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tools.importer.cli",
        description="接口文档导入解析（Swagger / HAR / cURL）→ 测试用例生成",
    )
    sub = parser.add_subparsers(dest="command")

    # preview
    p_preview = sub.add_parser("preview", help="解析并打印接口清单（不落盘）")
    p_preview.add_argument("file", help="文档文件（.json / .yaml / .har / .txt）")
    p_preview.add_argument("--json", action="store_true", help="额外打印 JSON 结构")
    p_preview.set_defaults(func=cmd_preview)

    # export
    p_export = sub.add_parser("export", help="导出测试用例")
    p_export.add_argument("file", help="文档文件")
    p_export.add_argument(
        "--project",
        default="",
        help="项目名（默认用 configs/config.yaml 里的 default_project）",
    )
    p_export.add_argument("--epic", default="接口文档导入", help="报告分组名")
    p_export.add_argument("--format", choices=["yaml", "excel"], default="yaml")
    p_export.add_argument("--output", help="Excel 输出路径（--format excel 时有效）")
    p_export.add_argument("--overwrite", action="store_true", help="覆盖已存在的用例文件")
    p_export.add_argument("--no-auth", action="store_true", help="不自动加鉴权请求头")
    p_export.set_defaults(func=cmd_export)

    # web
    p_web = sub.add_parser("web", help="启动本地网页上传页")
    p_web.add_argument("--host", default="127.0.0.1")
    p_web.add_argument("--port", type=int, default=8770)
    p_web.add_argument("--no-browser", action="store_true")
    p_web.set_defaults(func=cmd_web)

    # formats
    p_formats = sub.add_parser("formats", help="列出支持的格式")
    p_formats.set_defaults(func=cmd_formats)

    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0

    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
