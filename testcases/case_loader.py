"""用例数据加载：扫目录、读 YAML、按项目归类。

**两种目录格式都支持**

新格式（推荐，三层分离）—— 一个模块一个文件夹::

    cases/mall4j/statistics/
    ├── module.yaml          # 模块说明 / 标签 / 执行顺序
    ├── order.yaml           # 接口定义（路径、参数、断言）
    └── data/
        └── order.yaml       # 这个接口的测试数据（参数化组合）

旧格式（还在用，兼容）—— 一个 YAML 一个完整用例::

    cases/mall4j/statistics/test_statistics_order.yaml

两种格式可以混用，老文件不用改。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from utils.cases.case_schema import TestCaseError, validate_test_data
from utils.cases.case_validator import CaseValidator
from utils.cases.interface_registry import expand_case
from utils.logs.log_manager import log

from utils import paths

# 这些文件名是"结构文件"，不是用例
STRUCTURE_FILES = ("module.yaml", "interfaces.yaml")

# 校验器（进程内复用，不重复读 Schema 文件）
_validator = CaseValidator()


def load_yaml_file(path: Path) -> dict[str, Any]:
    """读一个 YAML 文件。"""
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise TestCaseError(f"{path} 顶层必须是字典")
    return data


def load_case_file(path: Path) -> dict[str, Any]:
    """读并校验一个用例文件（旧格式）。"""
    data = load_yaml_file(path)
    validate_test_data(data)
    _validate_format(data, path)
    return data


def _validate_format(case_data: dict[str, Any], path: Path) -> None:
    """格式校验（JSON Schema + 断言检查）。

    校验不通过直接抛 ``TestCaseError`` —— 用例文件写错了就该在收集阶段炸，
    而不是跑到一半才发现（那时数据已经造进环境了）。
    """
    errors = _validator.validate(case_data)
    if not errors:
        return

    detail = "\n".join(f"  - {err}" for err in errors)
    raise TestCaseError(f"用例格式有 {len(errors)} 处问题（{path.name}）：\n{detail}")


# --------------------------------------------------------------------------
# 新格式：接口定义 + 测试数据分离
# --------------------------------------------------------------------------


def _load_data_file(data_file: Path) -> dict[str, Any] | None:
    """读测试数据文件。

    支持两种写法：

    **维度字典**（框架自动做笛卡尔积）::

        searchType: [LAST_SEVEN, LAST_THIRTY]
        pageSize: [10, 20]

    **场景列表**（每个场景一组参数）::

        - name: 近七天
          data:
            searchType: LAST_SEVEN
    """
    try:
        with data_file.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as exc:
        raise TestCaseError(f"数据文件读取失败: {exc}") from exc

    if data is None:
        return None
    if not isinstance(data, (dict, list)):
        raise TestCaseError(f"{data_file} 必须是字典或列表")
    return data


def load_split_case(
    api_file: Path,
    data_file: Path | None = None,
    module_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把"接口定义 + 测试数据"合成成一份完整用例。

    Args:
        api_file: 接口定义文件（``order.yaml`` 这种）
        data_file: 测试数据文件（``data/order.yaml`` 这种），可空
        module_meta: 模块的 ``module.yaml`` 内容

    Returns:
        标准用例字典（跟旧格式一样，结构是 ``{"testcase": {...}}``）
    """
    api = load_yaml_file(api_file)

    # 接口定义可以直接写 testcase: 开头，也可以直接写平铺字段
    if "testcase" in api:
        case = dict(api["testcase"])
    else:
        case = dict(api)

    # 补上文件名当用例名（用户没写 name 时）
    if not case.get("name"):
        case["name"] = api_file.stem

    # 测试数据
    if data_file and data_file.exists():
        params = _load_data_file(data_file)
        if params is not None:
            case["parameters"] = params

    # 模块信息补进来（用例没写 marks 时用模块的）
    if module_meta:
        if not case.get("description") and module_meta.get("description"):
            case["description"] = module_meta["description"]
        if not case.get("marks") and module_meta.get("marks"):
            case["marks"] = module_meta["marks"]

    result = {"testcase": case}
    validate_test_data(result)
    _validate_format(result, api_file)
    return result


# --------------------------------------------------------------------------
# 扫描
# --------------------------------------------------------------------------


def _is_data_file(path: Path) -> bool:
    """是不是测试数据文件（在 data/ 目录下）。"""
    return "data" in path.parts


def _scan_root(root: Path, suffixes: tuple[str, ...] | None = None) -> list[tuple[str, Path]]:
    """扫描一个根目录下的用例文件。

    Args:
        root: 用例根目录
        suffixes: 只扫这些后缀的文件。给了就按后缀过滤
            （配合"用例来源"开关，确保 Excel 模式下不碰 YAML 文件）。
    """
    if not root.exists():
        return []

    found: list[tuple[str, Path]] = []

    candidates: list[Path] = []
    if suffixes is None:
        candidates.extend(sorted(root.rglob("*.yaml")))
        candidates.extend(sorted(root.rglob("*.xlsx")))
        candidates.extend(sorted(root.rglob("*.xlsm")))
    else:
        for suffix in suffixes:
            candidates.extend(sorted(root.rglob(f"*{suffix}")))

    for case_file in candidates:
        rel = case_file.relative_to(root)

        if len(rel.parts) < 2:
            log.warning("跳过 %s：用例必须放在 %s/<项目名>/ 目录下", case_file, root.name)
            continue

        # 结构文件不是用例
        if case_file.name in STRUCTURE_FILES:
            continue

        # data/ 下的文件是测试数据，由用例文件引用
        if _is_data_file(case_file):
            continue

        # Excel 的临时文件（~开头的）
        if case_file.name.startswith("~$"):
            continue

        found.append((rel.parts[0], case_file))

    return found


def discover_cases(
    root: Path | None = None,
    source: Any = None,
) -> list[tuple[str, Path]]:
    """扫描出所有用例文件。

    只返回**用例文件**（``data/`` 下的和 ``module.yaml`` 不算用例）。

    支持两种格式：

    - ``*.yaml`` —— 一个文件一条用例
    - ``*.xlsx`` —— 一个文件多条用例（Excel）

    **扫哪些目录由"用例来源"开关决定**（见 :mod:`utils.cases.case_source`）：
    选 ``excel`` 只扫 Excel 目录，选 ``yaml`` 只扫 YAML 目录，选 ``both`` 两个都扫。

    Args:
        root: 显式指定的根目录。给了就只扫这一个（不管来源开关），
            方便测试和临时排查。
        source: 显式指定来源（``CaseSource`` 枚举）。
            不传就按当前生效的来源开关。

    Returns:
        ``[(项目名, 文件路径), ...]``
    """
    if root is not None:
        return _scan_root(root)

    from utils.cases.case_source import get_allowed_suffixes, get_source_roots

    suffixes = get_allowed_suffixes(source)
    found: list[tuple[str, Path]] = []
    for case_root in get_source_roots(source):
        found.extend(_scan_root(case_root, suffixes))
    return found


def find_data_file(api_file: Path) -> Path | None:
    """给接口定义文件找对应的测试数据文件。

    规则：同目录下的 ``data/<同名>.yaml``。
    """
    candidate = api_file.parent / "data" / f"{api_file.stem}.yaml"
    if candidate.exists():
        return candidate

    # 兼容 data/<同名>_data.yaml 这种命名
    alt = api_file.parent / "data" / f"{api_file.stem}_data.yaml"
    if alt.exists():
        return alt

    return None


def find_module_meta(module_dir: Path) -> dict[str, Any]:
    """找模块目录里的 module.yaml。"""
    module_file = module_dir / "module.yaml"
    if not module_file.exists():
        return {}
    try:
        return load_yaml_file(module_file)
    except (TestCaseError, yaml.YAMLError) as exc:
        log.error("module.yaml 解析失败 %s：%s", module_file, exc)
        return {}


def load_any_case(path: Path, root: Path | None = None) -> dict[str, Any]:
    """加载一个用例文件 —— 自动判断是新格式还是旧格式。

    新格式（接口定义 + data/ 测试数据）会被合并；
    旧格式（``testcase:`` 开头）原样返回。

    Excel 文件请用 :func:`load_cases_from_file`（一个文件有多条用例）。
    """
    root = root or _detect_root(path)
    data = load_yaml_file(path)

    # 旧格式：顶层就有 testcase
    if "testcase" in data:
        validate_test_data(data)
        return data

    # 新格式：接口定义文件
    module_dir = path.parent
    module_meta = find_module_meta(module_dir)
    data_file = find_data_file(path)
    return load_split_case(path, data_file, module_meta)


def load_cases_from_file(
    path: Path,
    root: Path | None = None,
    project: str = "",
) -> list[dict[str, Any]]:
    """加载一个文件里的**所有**用例。

    - Excel（``.xlsx``）→ 一个文件里的所有用例
    - YAML → 一个用例

    Args:
        path: 用例文件路径
        root: 用例根目录。不传就按路径自动判断（YAML 和 Excel 在不同根目录）
        project: 项目名（从目录结构能推出来；显式传也行）

    Returns:
        ``[{"testcase": {...}}, ...]``
    """
    root = root or _detect_root(path)

    # 项目名：优先用传进来的，否则从路径里推（cases/<项目名>/...）
    if not project:
        try:
            rel = path.resolve().relative_to(root.resolve())
            project = rel.parts[0] if rel.parts else ""
        except (ValueError, OSError):
            project = ""

    if path.suffix.lower() in (".xlsx", ".xlsm"):
        from utils.excel.loader import load_excel_cases

        cases, excel_module = load_excel_cases(path, project=project)

        # 模块信息：module.yaml 打底，Excel 的「模块信息」表覆盖
        module_meta = find_module_meta(path.parent)
        merged_meta = {**module_meta, **{k: v for k, v in excel_module.items() if v}}
        if merged_meta:
            for case_data in cases:
                case = case_data["testcase"]
                if not case.get("description") and merged_meta.get("description"):
                    case["description"] = merged_meta["description"]
                if not case.get("marks") and merged_meta.get("marks"):
                    case["marks"] = merged_meta["marks"]
                # Excel 里没写层级时，用模块的
                allure = case.setdefault("allure", {})
                if not allure.get("feature") and merged_meta.get("label"):
                    allure["feature"] = merged_meta["label"]

        return cases

    case = load_any_case(path, root)

    # ---- 接口定义复用展开 ----
    #
    # 用例里写了 ``api: xxx`` 的步骤，在这一步展开成完整的 path/method/headers。
    # 展开后和手写的一样，执行引擎完全不用改。
    module = _module_of_path(path, root)
    expanded = expand_case(case, project=project, module=module)

    return [expanded]


def _detect_root(path: Path) -> Path:
    """猜一个用例文件属于哪个根目录。

    用例文件可能在 ``cases/``（YAML），也可能在 ``cases_excel/``（Excel），
    两个目录结构一样（``<根>/<项目>/<模块>/...``），靠路径判断。

    猜不到就退回 ``paths.CASES_ROOT`` —— 至少项目名不会算错太多。
    """
    from utils.cases.case_source import get_source_roots

    try:
        resolved = path.resolve()
    except OSError:
        return paths.CASES_ROOT

    for candidate in get_source_roots():
        try:
            resolved.relative_to(candidate.resolve())
            return candidate
        except (ValueError, OSError):
            continue

    return paths.CASES_ROOT


def _module_of_path(path: Path, root: Path | None = None) -> str:
    """从文件路径推模块名（``cases/<项目>/<模块>/xxx.yaml`` → ``<模块>``）。"""
    if root is None:
        root = _detect_root(path)

    try:
        rel = path.resolve().relative_to(root.resolve())
        if len(rel.parts) >= 2:
            return rel.parts[1]
    except (ValueError, OSError):
        pass

    return path.parent.name


def relative_case_path(path: Path) -> Path:
    """用例文件相对于**它自己那个根目录**的路径。

    用例可能在 ``cases/``（YAML），也可能在 ``cases_excel/``（Excel），
    相对路径要按各自��根目录算，不能一律减 ``cases/``。
    """
    root = _detect_root(path)
    try:
        return path.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return path.name


def load_all_cases(root: Path | None = None) -> dict[str, list[dict[str, Any]]]:
    """加载全部用例，按项目名分组。

    Returns:
        ``{项目名: [{"path": Path, "data": dict}, ...]}``
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for project, path in discover_cases(root):
        try:
            cases = load_cases_from_file(path, root)
        except (TestCaseError, yaml.YAMLError) as exc:
            log.error("用例文件 %s 解析失败：%s", path, exc)
            continue
        for data in cases:
            grouped.setdefault(project, []).append({"path": path, "data": data})
    return grouped
