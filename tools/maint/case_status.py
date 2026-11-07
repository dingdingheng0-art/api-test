"""用例状态迁移工具：批量给用例标注调试状态。

**为什么需要**

引入「调试态 / 执行态」之后，没写 ``status`` 的用例默认按「未调试」处理，
CI 不会跑。存量用例都是这样，需要一次性标成「调试通过」。

但**不能无脑全标** —— 新写的用例本来就该是「未调试」。

所以工具支持两种模式：

- ``--mark-all``：把所有没状态的用例标成 ``debugged``（存量迁移用）
- ``--name <用例名>``：只标指定用例（日常调试通过后用）

**怎么用**

存量迁移（先备份再执行）::

    python tools/maint/case_status.py --mark-all --dry-run   # 先看看会改哪些
    python tools/maint/case_status.py --mark-all             # 实际改

日常标注::

    python tools/maint/case_status.py --name 查询当前用户 --status debugged

**安全措施**

- 默认 ``--dry-run``，要加 ``--apply`` 才真改
- 改之前自动备份原文件（``.bak``）
- 只改 YAML；Excel 用例请在 Excel 里直接填「用例状态」列
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import yaml  # noqa: E402

from utils.cases.case_status import ALL_STATUSES, STATUS_LABELS  # noqa: E402
from utils.logs.log_manager import get_logger  # noqa: E402

log = get_logger(__name__)

# 要保留的注释（PyYAML 不支持，这里用文本方式改，尽量保留原格式）
_HEADER_COMMENT = (
    "# 用例状态：draft=未调试(默认不跑) / debugged=调试通过(执行) /\n"
    "#           disabled=调试通过但暂时不跑 / broken=调试不通过(不跑)\n"
)


def _read_yaml(path: Path) -> dict[str, Any] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        # Excel 是二进制文件，读不了 —— 直接跳过
        log.debug("跳过非 UTF-8 文件 %s: %s", path.name, exc)
        return None

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        log.warning("解析失败 %s: %s", path.name, exc)
        return None

    return data if isinstance(data, dict) else None


def _case_of(data: dict[str, Any]) -> dict[str, Any] | None:
    """取用例字典（兼容 ``testcase:`` 包裹和平铺两种写法）。"""
    if "testcase" in data and isinstance(data["testcase"], dict):
        return data["testcase"]
    return data


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    """写回 YAML（保留中文，键顺序不变）。"""
    text = yaml.safe_dump(
        data,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=100,
    )
    path.write_text(text, encoding="utf-8")


def _insert_status_yaml(path: Path, case: dict[str, Any], status: str) -> bool:
    """在 YAML 里插入 ``status`` 字段（文本插入，保住注释和格式）。

    不用 PyYAML 重新序列化 —— 那会把文件里的注释全部弄丢，
    而这些用例文件里注释比字段还多。

    返回 True 表示改了。
    """
    if case.get("status") == status:
        return False

    try:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError as exc:
        log.warning("读取失败 %s: %s", path.name, exc)
        return False

    # 找到顶层 ``name:`` 那一行，在它前面插 ``status:``
    insert_at = _find_insert_point(lines)

    if insert_at is None:
        # 找不到 name 字段，放弃（不动文件）
        log.warning("在 %s 里找不到 name 字段，跳过", path.name)
        return False

    # 备份
    backup = path.with_suffix(path.suffix + ".bak")
    shutil.copy2(path, backup)

    new_lines = list(lines)
    new_lines.insert(insert_at, f"status: {status}\n")
    path.write_text("".join(new_lines), encoding="utf-8")
    return True


def _find_insert_point(lines: list[str]) -> int | None:
    """找 ``status:`` 该插在第几行。

    规则：插在顶层 ``name:`` 之前。顶层就是**行首没有缩进**的行。
    """
    for index, line in enumerate(lines):
        if line.startswith("name:") and not line.startswith(" "):
            return index
    return None


def _find_yaml_files(root: Path) -> list[Path]:
    """找出所有用例 YAML（跳过模块配置和数据文件）。"""
    from testcases.case_loader import discover_cases

    result = []
    for _project, f in discover_cases(root):
        # Excel 用例不能在脚本里改状态 —— 请在 Excel 的「用例状态」列里填
        if f.suffix.lower() not in (".yaml", ".yml"):
            continue
        result.append(f)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="批量标注用例的调试状态")
    parser.add_argument(
        "--name", action="append", default=[],
        help="只处理指定名称的用例（可重复传）",
    )
    parser.add_argument(
        "--status", default="debugged",
        help=f"要标的状态（{'/'.join(ALL_STATUSES)}）",
    )
    parser.add_argument(
        "--mark-all", action="store_true",
        help="处理所有没写 status 的用例（存量迁移用）",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="真的改文件（不加这个只打印计划）",
    )
    parser.add_argument(
        "--root", default=None,
        help="用例根目录（默认 testcases/cases）",
    )
    args = parser.parse_args(argv)

    status = args.status.strip().lower()
    if status not in ALL_STATUSES:
        print(f"状态「{status}」不合法，可用：{'、'.join(ALL_STATUSES)}")
        return 2

    from utils import paths

    # 默认扫"当前启用的那套用例目录"——
    # 用例来源开关切到 yaml 时，Excel 目录里的文件本来就改不了（那是表格）
    if args.root:
        root = Path(args.root)
    else:
        from utils.cases.case_source import get_active_roots

        roots = get_active_roots()
        root = roots[0] if roots else paths.CASES_ROOT
        if len(roots) > 1:
            print(f"提示: 当前是 both 模式，只处理 {root.relative_to(paths.PROJECT_ROOT)}")

    if not args.name and not args.mark_all:
        print("要么指定 --name 用例名，要么用 --mark-all 处理全部")
        return 2

    wanted = {n.strip() for n in args.name if n.strip()}

    changed: list[Path] = []
    matched = 0

    for path in _find_yaml_files(root):
        data = _read_yaml(path)
        if data is None:
            continue

        case = _case_of(data)
        if case is None:
            continue

        case_name = str(case.get("name") or "")

        if wanted:
            if case_name not in wanted:
                continue
        elif not args.mark_all:
            continue
        elif case.get("status"):
            # --mark-all 模式：已经标过的不动
            continue

        matched += 1
        if args.apply:
            if _insert_status_yaml(path, case, status):
                changed.append(path)
        else:
            print(f"  将修改: {path.relative_to(root)}  (用例「{case_name}」 -> {status})")

    print()
    print(f"匹配到 {matched} 条用例")
    if args.apply:
        print(f"已修改 {len(changed)} 个文件")
        for p in changed:
            print(f"  {p.relative_to(root)}")
    else:
        print("这是预览模式，没有真的改文件。加上 --apply 执行。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
