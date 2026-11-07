"""把「接口测试用例_全量.xlsx」拆成框架能直接跑的**分模块**扁平格式用例。

背景
----
``接口测试用例_全量.xlsx`` 是从接口文档自动生成的单张大表（191 条用例、35 个模块）。
框架跑用例是按 ``cases/<项目>/<模块>/`` 目录组织的，一个模块一个文件夹，
所以需要按「模块」列把大表拆开。

**生成出来的用例要满足这几条**（这是硬要求，不是锦上添花）：

1. **用例名不出现英文** —— 报告里显示的是中文业务名。
2. **写操作也要覆盖** —— POST / PUT / DELETE 不能只生成 GET。
3. **前置后置要填** —— 写操作先造数据、后置清理，别把脏数据留在线上。
4. **断言要有业务含义** —— 光校验 ``code == 200`` 不算数，
   还要校验业务字段（比如"查出来的总数 >= 0"、"创建的 id 不为空"）。

顺便修掉自动生成时的几个毛病：

- 请求头里的空值（``""``）会被丢掉 —— 留着会把框架自动填的 token 覆盖成空
- ``/manager/`` 开头的接口自动带上鉴权头（头名按项目档案走）
- ``searchType`` 这种占位值换成真实的统计周期，并做成参数化
- ``»`` 前缀（文档表格的层级标记）去掉

用法::

    python tools/maint/split_flat_excel.py                # 只生成样板模块
    python tools/maint/split_flat_excel.py --all          # 全部模块都拆
    python tools/maint/split_flat_excel.py --modules manager_statistics user_addr

输出：``testcases/cases_excel/<项目>/<模块>/<模块>.xlsx`` + 同目录的 ``module.yaml``
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.paths import CASES_ROOT  # noqa: E402
from utils.cases.case_source import current_excel_dir  # noqa: E402

# Excel 用例输出目录（跟"用例来源"开关走，默认 cases_excel/）
EXCEL_ROOT = current_excel_dir()

from tools.maint.write_case_builder import (  # noqa: E402
    build_write_cases,
    write_endpoint_modules,
)

# 默认的输入文件
DEFAULT_SOURCE = Path(r"C:\Users\17358\Desktop\接口测试用例生成器\接口测试用例_全量.xlsx")

# 项目名（用例放在哪个项目下）
#
# 留空 = 用 configs/config.yaml 里的 default_project。
# 换系统不用改这个文件。
PROJECT = ""


def project_name() -> str:
    """实际用的项目名。"""
    if PROJECT:
        return PROJECT
    from utils.config.project_profile import ProjectRegistry

    return ProjectRegistry.default_project()

# 17 列的表头
#
# 在源表的 13 列基础上补了 4 列：
#   接口名称 —— 中文业务名，报告里的主标题用它
#   场景     —— allure 报告里的分组名（中文）
#   前置     —— 造数据（JSON 数组）
#   后置     —— 清数据（JSON 数组）
HEADERS = [
    "case_id", "模块", "标签", "用例名称", "优先级", "接口名称", "用例标题",
    "接口", "请求方法", "请求头", "请求数据", "响应数据(预期参考)",
    "断言", "参数化数据", "场景", "前置", "后置", "用例状态",
]

# 各模块在报告里显示的名字、业务分组（marks 里的第二个标签）
#
# **不写 order** —— 顺序统一由 configs/modules.yaml 决定，
# 这里写会把它盖掉（module.yaml 优先级更高）。
MODULE_META: dict[str, dict[str, Any]] = {
    "captcha_get": {"label": "获取验证码", "group": "auth", "description": "获取图形验证码"},
    "captcha_check": {"label": "校验验证码", "group": "auth", "description": "校验图形验证码"},
    "logOut": {"label": "退出登录", "group": "auth", "description": "注销登录会话"},
    "token_refresh": {"label": "令牌刷新", "group": "auth", "description": "刷新访问令牌"},
    "prod_prod": {"label": "商品管理", "group": "prod", "description": "商品列表 / 详情"},
    "prod_prodComm": {"label": "商品评论", "group": "prod", "description": "商品评论管理"},
    "prod_prodTag": {"label": "商品标签", "group": "prod", "description": "商品标签管理"},
    "prod_category": {"label": "商品分类", "group": "prod", "description": "商品分类管理"},
    "prod_spec": {"label": "商品规格", "group": "prod", "description": "商品规格管理"},
    "order_order": {"label": "订单管理", "group": "order", "description": "订单查询 / 详情"},
    "shop_transport": {"label": "运费模板", "group": "shop", "description": "运费模板"},
    "shop_notice": {"label": "店铺公告", "group": "shop", "description": "店铺公告"},
    "shop_pickAddr": {"label": "自提点", "group": "shop", "description": "自提点管理"},
    "admin_user": {"label": "会员管理", "group": "admin", "description": "会员列表 / 详情"},
    "sys_user": {"label": "系统用户", "group": "sys", "description": "系统用户管理"},
    "sys_role": {"label": "角色管理", "group": "sys", "description": "角色与权限"},
    "sys_menu": {"label": "菜单管理", "group": "sys", "description": "菜单树"},
    "sys_config": {"label": "参数配置", "group": "sys", "description": "系统参数"},
    "sys_log": {"label": "操作日志", "group": "sys", "description": "操作日志"},
    "admin_area": {"label": "地区管理", "group": "admin", "description": "地区数据"},
    "admin_delivery": {"label": "配送配置", "group": "admin", "description": "配送设置"},
    "admin_file": {"label": "文件上传", "group": "admin", "description": "文件上传"},
    "admin_hotSearch": {"label": "热门搜索", "group": "admin", "description": "热搜词管理"},
    "admin_indexImg": {"label": "轮播图", "group": "admin", "description": "首页轮播图"},
}

# 统计周期：占位值换成真实值，并做成参数化
SEARCH_TYPE_PARAMS = [
    {"searchType": "LAST_SEVEN"},
    {"searchType": "LAST_THIRTY"},
]

# 不需要带鉴权头的路径前缀（公开接口）
PUBLIC_PATH_PREFIXES = ("/webid", "/list", "/captcha/")


# --------------------------------------------------------------------------
# 读源文件
# --------------------------------------------------------------------------


def read_source(path: Path) -> list[dict[str, Any]]:
    """读源表，返回「每行一个 dict」的列表。"""
    from openpyxl import load_workbook

    wb = load_workbook(path, data_only=True)
    ws = wb["接口测试用例"]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        raise SystemExit(f"{path} 里没有「接口测试用例」表")

    header = [str(h).strip() for h in rows[0]]
    result: list[dict[str, Any]] = []
    for row in rows[1:]:
        if not row or not row[0]:
            continue
        item = {h: (row[i] if i < len(row) else None) for i, h in enumerate(header)}
        result.append(item)

    return result


# --------------------------------------------------------------------------
# 逐行修整
# --------------------------------------------------------------------------


def clean_headers(raw: Any, api_path: str) -> dict[str, Any]:
    """整理请求头：去空值、补鉴权头。"""
    headers: dict[str, Any] = {}
    if isinstance(raw, str) and raw.strip():
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            if value in (None, ""):
                continue
            headers[str(key)] = value

    # 公开接口不用带鉴权
    if not api_path.startswith(PUBLIC_PATH_PREFIXES):
        # 鉴权头叫什么按项目档案走（不同系统头名不一样）
        from utils.config.project_profile import ProjectRegistry

        project = project_name()
        profile = ProjectRegistry.get(project)
        if profile is not None:
            if profile.token_header:
                headers.setdefault(
                    profile.token_header, f"{{{{{project}.{profile.token_var}}}}}"
                )
            if profile.session_header:
                headers.setdefault(
                    profile.session_header, f"{{{{{project}.{profile.session_var}}}}}"
                )

    return headers


def clean_request_data(raw: Any, api_path: str) -> Any:
    """整理请求数据：去 ``»`` 前缀、清掉缓存打戳参数。"""
    if isinstance(raw, str) and raw.strip():
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None
    if not isinstance(raw, (dict, list)):
        return None

    data = _strip_bullets(raw)

    # ``_t`` 是前端加的缓存打戳，服务端不认，去掉
    if isinstance(data, dict):
        data.pop("_t", None)

    return data


def _strip_bullets(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(k).lstrip("» ").strip(): _strip_bullets(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_strip_bullets(v) for v in value]
    return value


# 哪些接口真的收 searchType（统计周期）
SEARCH_TYPE_PATHS = (
    "/manager/statistics/order",
    "/manager/statistics/index/storeStatistics",
    "/manager/statistics/index/goodsStatistics",
    "/manager/statistics/view/list",
    "/manager/statistics/view/online/history",
)


def fix_search_type(data: Any, api_path: str) -> Any:
    """``searchType`` 的占位值换成真实统计周期。"""
    if not isinstance(data, dict):
        return data
    if data.get("searchType") in (None, "", "test") and api_path in SEARCH_TYPE_PATHS:
        data["searchType"] = "${searchType}"
    return data


def build_param_data(module: str, api_path: str, raw: Any) -> Any:
    """生成「参数化数据」列。"""
    if isinstance(raw, str) and raw.strip():
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = None
    if raw:
        return json.dumps(raw, ensure_ascii=False)

    if api_path in SEARCH_TYPE_PATHS:
        return json.dumps(SEARCH_TYPE_PARAMS, ensure_ascii=False)

    return ""


def build_title(row: dict[str, Any], api_path: str, method: str) -> str:
    """生成「用例标题」列的内容。

    这里只写**场景说明**（比如「店铺统计（正常请求）」），
    接口业务名放在「接口名称」列 —— 两列分开，报告里拼起来才是完整标题，
    Excel 里也不会把同一句话写两遍。
    """
    original = str(row.get("用例标题") or "").strip()

    scene = scene_name(api_path)

    # 反向用例（查不到数据）要和正向的区分开
    if "查不到" in original:
        return f"{scene}（查不到数据）"
    return f"{scene}（正常请求）"


def chinese_case_name(api_label: str, title: str) -> str:
    """生成**纯中文**的用例名。

    报告里显示的就是这个名字，所以不能出现英文 ——
    源表里的 ``test_manager_statistics_get_1`` 这种名字要转掉。

    组成：``接口名 - 标题``。比如「店铺销售统计 - 店铺统计（正常请求）」。
    """
    left = (api_label or "").strip()
    right = (title or "").strip()

    if left and right and right not in left:
        return f"{left} - {right}"
    return left or right or "未命名用例"


def write_interfaces_yaml(module: str, spec: dict[str, Any], out_dir: Path) -> None:
    """写模块的 interfaces.yaml（接口路径 → 中文名）。"""
    path = spec.get("path")
    name = spec.get("name")
    if not path or not name:
        return

    lines = [
        "# ============================================================================",
        f"# {module} 模块的接口中文名",
        "#",
        "# 报告里显示的接口名从这里取。左边是接口路径，右边是想显示的名字。",
        "# ============================================================================",
        "",
        f"{path}: {name}",
        "",
    ]
    read = spec.get("read")
    if read and read.get("path"):
        lines.insert(-1, f"{read['path']}: {name}列表")

    (out_dir / "interfaces.yaml").write_text("\n".join(lines), encoding="utf-8")


# 接口路径 → 好读的中文接口名
#
# **这个表只是兜底** —— 优先顺序是：
#   1. Excel 的「接口名称」列（用例作者自己填，最准）
#   2. 模块目录下的 interfaces.yaml（模块负责人维护，一处维护整个模块）
#   3. 这个内置表（临时用）
#
# 接口多了以后建议改用第 2 种方式。
_INTERFACE_NAMES = {
    "/manager/statistics/index/storeStatistics": "店铺销售统计",
    "/manager/statistics/index/goodsStatistics": "商品销售统计",
    "/manager/statistics/index/notice": "首页待办统计",
    "/manager/statistics/index": "首页统计概览",
    "/manager/statistics/order": "订单统计",
    "/manager/statistics/view/list": "店铺访问量统计",
    "/manager/statistics/view/online/history": "实时在线人数",
    "/manager/permission/menu/memberMenu": "当前用户可见菜单",
}


def load_interface_names(module: str) -> dict[str, str]:
    """读模块目录下的 ``interfaces.yaml``。

    文件格式（按接口路径映射到中文名）::

        /manager/statistics/order: 订单统计
        /manager/statistics/view/list: 店铺访问量统计

    没这个文件就返回空字典（走内置表兜底）。
    """
    path = EXCEL_ROOT / project_name() / module / "interfaces.yaml"
    if not path.exists():
        return {}

    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        return {}

    # 支持两种写法：直接映射，或者套一层 interfaces:
    raw = data.get("interfaces", data)
    if not isinstance(raw, dict):
        return {}

    return {str(k): str(v) for k, v in raw.items() if v}


def interface_name(api_path: str, method: str, module: str = "") -> str:
    """接口路径 → 中文接口名。"""
    if module:
        names = load_interface_names(module)
        if api_path in names:
            return names[api_path]

    if api_path in _INTERFACE_NAMES:
        return _INTERFACE_NAMES[api_path]

    # 没收录的：取路径最后一段，驼峰拆开
    tail = [p for p in api_path.split("/") if p and not p.startswith("{")]
    if not tail:
        return f"{method} {api_path}"
    return tail[-1]


# 场景层（报告里的分组）—— 跟标准格式一致：按接口分，不按模块分
_SCENES = {
    "/manager/statistics/index/storeStatistics": "店铺统计",
    "/manager/statistics/index/goodsStatistics": "商品统计",
    "/manager/statistics/index/notice": "待办统计",
    "/manager/statistics/index": "概览统计",
    "/manager/statistics/order": "订单统计",
    "/manager/statistics/view/list": "访问量统计",
    "/manager/statistics/view/online/history": "在线人数",
    "/manager/permission/menu/memberMenu": "菜单查询",
}


def scene_name(api_path: str) -> str:
    """接口路径 → 报告里的场景名（分组用）。"""
    if api_path in _SCENES:
        return _SCENES[api_path]
    return ""


def build_asserts(raw: Any, api_path: str) -> str:
    """整理断言列。

    自动生成的断言只校验了状态码和 ``success``，这里补上业务码 ``code == 200``，
    让失败时能一眼看出是业务报错还是 HTTP 报错。
    """
    if isinstance(raw, str) and raw.strip():
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = []
    if not isinstance(raw, list):
        raw = []

    asserts: list[dict[str, Any]] = []
    has_code_check = False
    for item in raw:
        if not isinstance(item, dict):
            continue
        if item.get("check") == "body" and item.get("jsonpath") in ("$.code", "$.data.code"):
            has_code_check = True
        asserts.append(item)

    if not has_code_check and api_path.startswith("/manager/"):
        asserts.insert(
            1,
            {"check": "body", "jsonpath": "$.code", "comparator": "==", "expect": 200},
        )

    return json.dumps(asserts, ensure_ascii=False)


# --------------------------------------------------------------------------
# 写文件
# --------------------------------------------------------------------------


def build_workbook(cases: list[dict[str, Any]], out_path: Path) -> None:
    """把整理好的用例写成扁平格式的工作簿。"""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()

    # ---- 配置表 ----
    ws_cfg = wb.active
    ws_cfg.title = "配置"
    ws_cfg.append(["全局配置（运行前按需修改）"])
    ws_cfg["A1"].font = Font(bold=True, size=14, color="1F4E78")
    ws_cfg.append([])
    ws_cfg.append(["配置项", "值", "说明"])
    for cell in ws_cfg[3]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F4E78")
    for row in [
        ("base_url", "http://103.236.97.137:8190/_admin", "接口服务根地址（框架默认读 configs/config.yaml，这里只是备注）"),
        ("timeout", 10, "单条请求超时时间（秒）"),
        ("global_headers", '{"Accept": "application/json"}', "全局请求头，会与用例级 headers 合并（用例级优先）"),
        ("verify_ssl", False, "是否校验 HTTPS 证书"),
    ]:
        ws_cfg.append(list(row))
    for col, width in zip("ABC", (18, 42, 50)):
        ws_cfg.column_dimensions[col].width = width
    ws_cfg.freeze_panes = "A4"

    # ---- 主表 ----
    ws = wb.create_sheet("接口测试用例")
    ws.append(HEADERS)
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F4E78")
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    for row in cases:
        ws.append(row)
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    for idx, width in enumerate(
        (14, 20, 14, 30, 8, 24, 34, 40, 9, 30, 26, 26, 44, 30, 16), start=1
    ):
        ws.column_dimensions[get_column_letter(idx)].width = width
    ws.freeze_panes = "A2"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)


def write_module_yaml(module: str, out_dir: Path) -> None:
    """写模块的 module.yaml（报告里显示的模块名、标签）。

    **不写 order。** ``module.yaml`` 的优先级高于 ``configs/modules.yaml``，
    这里兜底写个 ``order: 80`` 会把全局配置里的模块顺序整个盖掉 ——
    历史上就是这么变成「23 个模块全排 80、按字母序跑」的。
    想调顺序就去改 ``configs/modules.yaml``，一处生效。
    """
    meta = MODULE_META.get(module, {})
    lines = [
        "# ============================================================================",
        f"# {module} 模块",
        "#",
        "# 这个模块的用例由 tools/maint/split_flat_excel.py 从接口文档自动生成。",
        "#",
        "# order 不在这里写：本文件优先级高于 configs/modules.yaml，",
        "# 写死会把全局配置的模块顺序盖掉。要调顺序去改 configs/modules.yaml。",
        "# ============================================================================",
        "",
        f"name: {module}",
    ]
    if meta.get("label"):
        lines.append(f"label: {meta['label']}")
    if meta.get("description"):
        lines.append(f"description: {meta['description']}")
    marks = [module]
    if meta.get("group"):
        marks.append(meta["group"])
    lines.append(f"marks: [{', '.join(marks)}]")
    lines.append("")

    (out_dir / "module.yaml").write_text("\n".join(lines), encoding="utf-8")


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="拆分扁平格式的全量用例表")
    parser.add_argument("--source", default=str(DEFAULT_SOURCE), help="源 Excel 路径")
    parser.add_argument("--all", action="store_true", help="所有模块都拆")
    parser.add_argument(
        "--modules", nargs="*", default=None, help="只拆指定的模块（默认拆样板模块）"
    )
    parser.add_argument("--dry-run", action="store_true", help="只看会生成什么，不写文件")
    args = parser.parse_args()

    source = Path(args.source)
    if not source.exists():
        print(f"源文件不存在: {source}")
        return 1

    rows = read_source(source)

    if args.modules:
        wanted = set(args.modules)
    elif args.all:
        wanted = None
    else:
        wanted = {"manager_statistics", "manager_permission"}

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        module = str(row.get("模块") or "").strip()
        if not module:
            continue
        if wanted is not None and module not in wanted:
            continue
        grouped.setdefault(module, []).append(row)

    if not grouped:
        print("没有匹配的模块")
        return 1

    for module, module_rows in sorted(grouped.items()):
        out_dir = EXCEL_ROOT / project_name() / module
        out_file = out_dir / f"{module}.xlsx"

        cases: list[list[Any]] = []
        for row in module_rows:
            api_path = str(row.get("接口") or "").strip()
            method = str(row.get("请求方法") or "GET").strip().upper()

            request_data = clean_request_data(row.get("请求数据"), api_path)
            request_data = fix_search_type(request_data, api_path)

            api_label = (
                str(row.get("接口名称") or "").strip()
                or interface_name(api_path, method, module)
            )
            title = build_title(row, api_path, method)

            cases.append([
                row.get("case_id"),
                module,
                row.get("标签") or module,
                # 用例名 —— 全中文（报告里显示的是它）
                chinese_case_name(api_label, title),
                row.get("优先级") or "P1",
                api_label,
                title,
                api_path,
                method,
                json.dumps(clean_headers(row.get("请求头"), api_path), ensure_ascii=False),
                json.dumps(request_data, ensure_ascii=False) if request_data else "",
                (row.get("响应数据(预期参考)") or "")[:500],
                build_asserts(row.get("断言"), api_path),
                build_param_data(module, api_path, row.get("参数化数据")),
                scene_name(api_path),
                str(row.get("前置") or ""),
                str(row.get("后置") or ""),
                # 用例状态 —— Excel 是手工维护的源，默认就是"要跑的"
                str(row.get("用例状态") or "debugged"),
            ])

        if args.dry_run:
            print(f"[试运行] {module}: {len(cases)} 条 → {out_file}")
            continue

        build_workbook(cases, out_file)
        write_module_yaml(module, out_dir)
        print(f"已生成 {module}: {len(cases)} 条用例 → {out_file}")

    # ---- 写操作的模块（从实测清单来，不从文档来）----
    write_specs = write_endpoint_modules()
    if write_specs:
        if args.modules:
            wanted_write = {m for m in write_specs if m in args.modules}
        elif args.all:
            wanted_write = set(write_specs)
        else:
            wanted_write = set()

        for module, spec in sorted(write_specs.items()):
            if module not in wanted_write:
                continue

            out_dir = EXCEL_ROOT / project_name() / module
            out_file = out_dir / f"{module}.xlsx"

            write_cases = build_write_cases(spec)
            rows_out = [
                [c.get(h, "") for h in HEADERS]
                for c in write_cases
            ]

            if args.dry_run:
                print(f"[试运行] {module}: {len(rows_out)} 条 → {out_file}")
                continue

            build_workbook(rows_out, out_file)
            write_module_yaml(module, out_dir)
            write_interfaces_yaml(module, spec, out_dir)
            print(f"已生成 {module}: {len(rows_out)} 条写操作用例 → {out_file}")

    if not args.dry_run:
        print()
        print("怎么用：")
        print(f"  python scripts/run.py --module {sorted(grouped)[0]}")
        print("  python scripts/run.py --list")

    return 0


if __name__ == "__main__":
    sys.exit(main())
