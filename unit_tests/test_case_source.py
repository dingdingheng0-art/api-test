"""用例来源开关：Excel 维护 / YAML 维护 / 两个都跑。

要保证的行为：

1. 选 ``excel`` 时**只**发现 Excel 目录下的用例，YAML 一概不碰
2. 选 ``yaml`` 时反过来
3. 选 ``both`` 时两边都扫
4. 目录隔离真的起作用（不是只看扩展名）
"""
from __future__ import annotations

from pathlib import Path

import pytest

from utils.cases.case_source import (
    CaseSource,
    _parse,
    current_excel_dir,
    get_case_source,
    get_source_roots,
)


# ---------------------------------------------------------------------------
# 解析各种写法
# ---------------------------------------------------------------------------


class TestSourceParsing:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("excel", CaseSource.EXCEL),
            ("EXCEL", CaseSource.EXCEL),
            ("  excel  ", CaseSource.EXCEL),
            ("xlsx", CaseSource.EXCEL),
            ("yaml", CaseSource.YAML),
            ("yml", CaseSource.YAML),
            ("both", CaseSource.BOTH),
            ("all", CaseSource.BOTH),
        ],
    )
    def test_parse_known_values(self, raw: str, expected: CaseSource) -> None:
        assert _parse(raw) is expected

    @pytest.mark.parametrize("raw", ["", None, "csv", "unknown", "表"])
    def test_parse_unknown_returns_none(self, raw) -> None:
        """认不出来的值返回 None —— 让上层退回默认值，而不是乱猜。"""
        assert _parse(raw) is None


# ---------------------------------------------------------------------------
# 优先级：命令行 > 环境变量 > 配置文件
# ---------------------------------------------------------------------------


class TestSourcePriority:
    def test_command_line_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APITEST_CASE_SOURCE", "yaml")
        assert get_case_source("excel") is CaseSource.EXCEL

    def test_env_wins_over_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APITEST_CASE_SOURCE", "yaml")
        assert get_case_source() is CaseSource.YAML

    def test_unknown_env_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """环境变量写了看不懂的值 —— 退回默认值，别整个跑挂。"""
        monkeypatch.setenv("APITEST_CASE_SOURCE", "不存在的格式")
        assert get_case_source() is CaseSource.EXCEL

    def test_default_is_excel(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """平台主推 Excel 维护 —— 默认就该是 excel。"""
        monkeypatch.delenv("APITEST_CASE_SOURCE", raising=False)
        assert get_case_source() is CaseSource.EXCEL


# ---------------------------------------------------------------------------
# 扫哪些目录
# ---------------------------------------------------------------------------


class TestSourceRoots:
    def test_excel_mode_scans_excel_dir_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APITEST_CASE_SOURCE", "excel")
        roots = get_source_roots()

        assert len(roots) == 1
        assert roots[0].name == "cases_excel"

    def test_yaml_mode_scans_yaml_dir_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APITEST_CASE_SOURCE", "yaml")
        roots = get_source_roots()

        assert len(roots) == 1
        assert roots[0].name == "cases"

    def test_both_mode_scans_both_dirs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APITEST_CASE_SOURCE", "both")
        roots = get_source_roots()

        assert len(roots) == 2
        names = {r.name for r in roots}
        assert names == {"cases_excel", "cases"}

    def test_current_excel_dir_is_excel_root(self) -> None:
        """产出 Excel 的地方必须是 Excel 目录 —— 否则用例写了却跑不起来。"""
        assert current_excel_dir().name == "cases_excel"


# ---------------------------------------------------------------------------
# 发现用例：真的隔离开了
# ---------------------------------------------------------------------------


def _write_yaml_case(root: Path, project: str = "demo") -> None:
    """造一个最小的 YAML 用例文件。"""
    module_dir = root / project / "some_module"
    module_dir.mkdir(parents=True, exist_ok=True)
    (module_dir / "case.yaml").write_text(
        "testcase:\n"
        "  name: demo 用例\n"
        "  description: 测试用\n"
        "  steps:\n"
        "    - id: s1\n"
        "      path: /demo\n"
        "      method: GET\n",
        encoding="utf-8",
    )


def _write_excel_case(root: Path, project: str = "demo") -> None:
    """造一个最小的 Excel 用例文件。"""
    from openpyxl import Workbook

    module_dir = root / project / "some_module"
    module_dir.mkdir(parents=True, exist_ok=True)

    wb = Workbook()
    ws = wb.active
    ws.title = "接口测试用例"
    ws.append(["case_id", "模块", "标签", "用例名称", "接口", "请求方法"])
    ws.append(["E-001", "some_module", "demo", "Excel 用例", "/demo", "GET"])
    wb.save(module_dir / "case.xlsx")


def _strip_config_dirs(monkeypatch: pytest.MonkeyPatch) -> None:
    """把 config.yaml 里的 ``case_source.*_dir`` 去掉。

    配了目录项的话，它比 ``utils.paths`` 的默认值优先 ——
    单测里想指向临时目录，就得先把这项摘掉。
    """
    from utils.config.config_manager import ConfigManager

    original = ConfigManager.load_config

    def _without_dirs(force: bool = False) -> dict:
        cfg = dict(original(force=force))
        source = dict(cfg.get("case_source") or {})
        source.pop("excel_dir", None)
        source.pop("yaml_dir", None)
        cfg["case_source"] = source
        return cfg

    monkeypatch.setattr(ConfigManager, "load_config", staticmethod(_without_dirs))


def _patch_roots(
    monkeypatch: pytest.MonkeyPatch,
    excel_root: Path,
    yaml_root: Path,
) -> None:
    """把用例根目录指到临时目录。

    要改两处：
    - ``utils.paths`` 里的默认目录
    - ``config.yaml`` 里的 ``case_source.excel_dir`` / ``yaml_dir``
      （配了的话优先级更高，不然改 paths 不起作用）
    """
    monkeypatch.setattr("utils.cases.case_source.paths.CASES_EXCEL_ROOT", excel_root)
    monkeypatch.setattr("utils.cases.case_source.paths.CASES_ROOT", yaml_root)
    _strip_config_dirs(monkeypatch)


class TestDiscoverCases:
    def test_excel_mode_finds_only_excel(self, tmp_path: Path, monkeypatch) -> None:
        """选 Excel 就只认 Excel —— YAML 目录里的用例一条都不该出现。"""
        from testcases.case_loader import discover_cases

        excel_root = tmp_path / "cases_excel"
        yaml_root = tmp_path / "cases"
        _write_excel_case(excel_root)
        _write_yaml_case(yaml_root)

        _patch_roots(monkeypatch, excel_root, yaml_root)
        monkeypatch.setenv("APITEST_CASE_SOURCE", "excel")

        found = discover_cases()
        assert len(found) == 1
        assert found[0][1].suffix == ".xlsx"

    def test_yaml_mode_finds_only_yaml(self, tmp_path: Path, monkeypatch) -> None:
        from testcases.case_loader import discover_cases

        excel_root = tmp_path / "cases_excel"
        yaml_root = tmp_path / "cases"
        _write_excel_case(excel_root)
        _write_yaml_case(yaml_root)

        _patch_roots(monkeypatch, excel_root, yaml_root)
        monkeypatch.setenv("APITEST_CASE_SOURCE", "yaml")

        found = discover_cases()
        assert len(found) == 1
        assert found[0][1].suffix == ".yaml"

    def test_both_mode_finds_both(self, tmp_path: Path, monkeypatch) -> None:
        from testcases.case_loader import discover_cases

        excel_root = tmp_path / "cases_excel"
        yaml_root = tmp_path / "cases"
        _write_excel_case(excel_root)
        _write_yaml_case(yaml_root)

        _patch_roots(monkeypatch, excel_root, yaml_root)
        monkeypatch.setenv("APITEST_CASE_SOURCE", "both")

        found = discover_cases()
        assert len(found) == 2
        suffixes = {f.suffix for _p, f in found}
        assert suffixes == {".xlsx", ".yaml"}

    def test_explicit_root_overrides_source(self, tmp_path: Path, monkeypatch) -> None:
        """显式传 root 就只扫那一个 —— 排查问题时用。"""
        from testcases.case_loader import discover_cases

        excel_root = tmp_path / "cases_excel"
        yaml_root = tmp_path / "cases"
        _write_excel_case(excel_root)
        _write_yaml_case(yaml_root)

        _patch_roots(monkeypatch, excel_root, yaml_root)
        monkeypatch.setenv("APITEST_CASE_SOURCE", "excel")

        # 明明是 Excel 模式，但显式指定了 YAML 目录 —— 显式的优先
        found = discover_cases(yaml_root)
        assert len(found) == 1
        assert found[0][1].suffix == ".yaml"


# ---------------------------------------------------------------------------
# 模块清单也跟着来源走
# ---------------------------------------------------------------------------


class TestModuleManagerFollowsSource:
    def test_project_list_depends_on_source(self, tmp_path: Path, monkeypatch) -> None:
        """Excel 模式下，YAML 目录里的项目不算数。"""
        from utils.cases.module_manager import ModuleManager

        excel_root = tmp_path / "cases_excel"
        yaml_root = tmp_path / "cases"

        # Excel 目录里放一个项目
        (excel_root / "excel_proj" / "mod").mkdir(parents=True)
        (excel_root / "excel_proj" / "mod" / "module.yaml").write_text(
            "name: mod\nlabel: Excel 模块\n", encoding="utf-8"
        )
        # YAML 目录里放另一个项目
        (yaml_root / "yaml_proj" / "mod").mkdir(parents=True)
        (yaml_root / "yaml_proj" / "mod" / "module.yaml").write_text(
            "name: mod\nlabel: YAML 模块\n", encoding="utf-8"
        )

        monkeypatch.setattr(
            "utils.cases.case_source.paths.CASES_EXCEL_ROOT", excel_root
        )
        monkeypatch.setattr("utils.cases.case_source.paths.CASES_ROOT", yaml_root)
        _strip_config_dirs(monkeypatch)

        monkeypatch.setenv("APITEST_CASE_SOURCE", "excel")
        ModuleManager.reset()
        projects = ModuleManager.list_projects()
        assert "excel_proj" in projects
        assert "yaml_proj" not in projects

        monkeypatch.setenv("APITEST_CASE_SOURCE", "yaml")
        ModuleManager.reset()
        projects = ModuleManager.list_projects()
        assert "yaml_proj" in projects
        assert "excel_proj" not in projects

        monkeypatch.setenv("APITEST_CASE_SOURCE", "both")
        ModuleManager.reset()
        projects = ModuleManager.list_projects()
        assert "excel_proj" in projects
        assert "yaml_proj" in projects

        ModuleManager.reset()

    def test_reset_clears_cache(self) -> None:
        """切了来源要能清缓存 —— 不然模块清单还是按旧目录算的。"""
        from utils.cases.module_manager import ModuleManager

        ModuleManager.reset()
        assert ModuleManager._modules is None
        assert ModuleManager._local is None
        assert ModuleManager._local_key is None
