"""模块清单：模块归属、标签、执行顺序。

**为什么在 utils/ 而不是 testcases/**

它只读两个配置文件（``configs/modules.yaml`` 和模块目录里的 ``module.yaml``），
不知道用例文件本身怎么解析 —— 报告、Excel 解析、脚本都能用，
放在能力层才不会逼着上层去 import 它。

**模块信息有两个来源，后者优先**：

1. ``configs/modules.yaml`` —— 全局清单（适合集中管理）
2. ``cases/<项目>/<模块>/module.yaml`` —— 模块目录里的自描述文件（就近原则）

用法::

    modules:
      my_shop:
        auth:
          order: 10
          marks: [auth]
          description: 登录 / 认证

模块目录里的 ``module.yaml`` 长这样：

.. code-block:: yaml

    name: auth
    description: 登录 / 认证 / token 相关
    order: 10
    marks: [auth]
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from utils.logs.log_manager import log

from utils import paths

# 没在 modules.yaml 里登记的模块，默认排在这个位置
DEFAULT_ORDER = 999


class ModuleManager:
    """管理模块清单。"""

    _modules: dict[str, dict[str, dict[str, Any]]] | None = None
    # 模块目录里的 module.yaml 缓存： {模块目录绝对路径: 配置}
    _local: dict[str, dict[str, Any]] | None = None
    # 缓存对应的根目录（切换用例来源后缓存要失效）
    _local_key: tuple[str, ...] | None = None

    # ---------------- 全局清单 ----------------

    @classmethod
    def load(cls) -> dict[str, dict[str, dict[str, Any]]]:
        """读 modules.yaml。"""
        if cls._modules is not None:
            return cls._modules

        if not paths.MODULES_FILE.exists():
            log.info("没有 %s，模块将按名字排序", paths.MODULES_FILE.name)
            cls._modules = {}
            return cls._modules

        try:
            with paths.MODULES_FILE.open("r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except (OSError, yaml.YAMLError) as exc:
            log.error("modules.yaml 解析失败：%s", exc)
            cls._modules = {}
            return cls._modules

        modules = data.get("modules") or {}
        cls._modules = modules if isinstance(modules, dict) else {}
        return cls._modules

    # ---------------- 用例来源相关的目录 ----------------

    @classmethod
    def _roots(cls) -> list[Path]:
        """当前生效的用例根目录。

        用例可能在 ``cases/``（YAML），也可能在 ``cases_excel/``（Excel）——
        哪个生效由"用例来源"开关决定（见 :mod:`utils.cases.case_source`）。
        """
        try:
            from utils.cases.case_source import get_source_roots

            return get_source_roots()
        except Exception as exc:  # noqa: BLE001 - 读不到就用默认目录
            log.warning("读取用例来源失败（用默认目录）：%s", exc)
            return [paths.CASES_ROOT]

    # ---------------- 模块目录里的 module.yaml ----------------

    @classmethod
    def _load_local(cls) -> dict[str, dict[str, Any]]:
        """扫描用例目录里所有模块的 module.yaml。"""
        # 缓存按"当前根目录"分 —— 切换用例来源后缓存自动失效
        roots_key = tuple(str(r) for r in cls._roots())
        if cls._local is not None and cls._local_key == roots_key:
            return cls._local

        found: dict[str, dict[str, Any]] = {}
        for root in cls._roots():
            if not root.exists():
                continue
            for module_file in root.rglob("module.yaml"):
                try:
                    with module_file.open("r", encoding="utf-8") as f:
                        data = yaml.safe_load(f) or {}
                except (OSError, yaml.YAMLError) as exc:
                    log.error("module.yaml 解析失败 %s：%s", module_file, exc)
                    continue
                if isinstance(data, dict):
                    found[str(module_file.parent.resolve())] = data

        cls._local = found
        cls._local_key = roots_key
        return found

    @classmethod
    def _local_config(cls, module_dir: Path) -> dict[str, Any]:
        """取某个模块目录的 module.yaml 配置。"""
        cls._load_local()
        assert cls._local is not None
        return cls._local.get(str(module_dir.resolve()), {}) or {}

    @classmethod
    def _module_dir(cls, project: str, module: str) -> Path:
        """某个模块所在的目录。

        按"用例来源"决定去哪个根目录下找；都找不到就退回默认路径
        （返回一个可能不存在的路径，调用方会用 ``exists()`` 判断）。
        """
        for root in cls._roots():
            candidate = root / project / module
            if candidate.exists():
                return candidate
        return paths.CASES_ROOT / project / module

    # ---------------- 对外接口 ----------------

    @classmethod
    def get_module_config(cls, project: str, module: str) -> dict[str, Any]:
        """取模块配置。

        优先级：模块目录的 ``module.yaml`` > 全局 ``modules.yaml``。
        """
        global_cfg = dict(cls.load().get(project, {}).get(module, {}) or {})

        local_cfg = cls._local_config(cls._module_dir(project, module))
        if local_cfg:
            # 本地配置覆盖全局的同名项
            merged = {**global_cfg, **local_cfg}
            return merged
        return global_cfg

    @classmethod
    def get_order(cls, project: str, module: str) -> int:
        """模块的执行顺序。"""
        config = cls.get_module_config(project, module)
        try:
            return int(config.get("order", DEFAULT_ORDER))
        except (TypeError, ValueError):
            return DEFAULT_ORDER

    @classmethod
    def get_marks(cls, project: str, module: str) -> list[str]:
        """模块级标签。"""
        config = cls.get_module_config(project, module)
        marks = config.get("marks") or []
        if isinstance(marks, str):
            return [marks]
        return [str(m) for m in marks]

    @classmethod
    def get_description(cls, project: str, module: str) -> str:
        """模块说明。"""
        return str(cls.get_module_config(project, module).get("description", ""))

    @classmethod
    def get_label(cls, project: str, module: str) -> str:
        """模块的显示名（报告里显示的模块名）。

        优先级：

        1. 配置里的 ``label``（``modules.yaml`` 或模块目录的 ``module.yaml``）
        2. 内置的常见模块名映射（``admin_attribute`` → ``商品属性``）
        3. 英文按驼峰拆开（``admin_indexImg`` → ``admin indexImg``）

        **为什么要第 2 条**：报告的树是 ``epic > suite(模块) > subSuite(场景)``。
        模块名显示成英文 ``admin_attribute`` 时，看报告的人不知道是哪个功能；
        而中文名 ``商品属性`` 藏在下一层，两边对不上。统一成中文才看得懂。
        """
        config = cls.get_module_config(project, module)
        label = config.get("label")
        if label:
            return str(label)

        from utils.cases.module_labels import builtin_label, split_english

        builtin = builtin_label(module)
        if builtin:
            return builtin

        return split_english(module)

    @classmethod
    def list_modules(cls, project: str) -> list[dict[str, Any]]:
        """列出某个项目下所有模块（按 order 排）。"""
        modules = dict(cls.load().get(project, {}) or {})

        # 把模块目录里的 module.yaml 也算进来
        for root in cls._roots():
            module_dir = root / project
            if not module_dir.exists():
                continue
            for child in module_dir.iterdir():
                if not child.is_dir():
                    continue
                local = cls._local_config(child)
                if local:
                    modules.setdefault(child.name, {})
                    modules[child.name] = {**(modules[child.name] or {}), **local}

        result = []
        for name, config in modules.items():
            config = config or {}
            result.append(
                {
                    "name": name,
                    "order": config.get("order", DEFAULT_ORDER),
                    "marks": config.get("marks", []),
                    "description": config.get("description", ""),
                }
            )
        result.sort(key=lambda m: (m["order"], m["name"]))
        return result

    @classmethod
    def list_projects(cls) -> list[str]:
        """列出所有项目名。

        两个来源：
        - ``modules.yaml`` 里登记的
        - 用例目录（``cases/`` 或 ``cases_excel/``）下有目录的
          （哪怕没在 modules.yaml 里登记）
        """
        projects = set(cls.load().keys())

        for root in cls._roots():
            if not root.exists():
                continue
            for child in root.iterdir():
                if child.is_dir():
                    projects.add(child.name)

        return sorted(projects)

    @classmethod
    def reset(cls) -> None:
        """清缓存。

        切换"用例来源"后要调 —— 不然模块清单还是按旧的根目录算的。
        """
        cls._modules = None
        cls._local = None
        cls._local_key = None

    @classmethod
    def sort_cases(cls, cases: list[tuple[str, Path]]) -> list[tuple[str, Path]]:
        """给 ``[(项目名, 文件路径), ...]`` 排执行顺序。

        规则：模块 order -> 用例 order -> 模块名 -> 文件名
        """
        def sort_key(item: tuple[str, Path]) -> tuple[Any, ...]:
            project, path = item
            module = cls._module_of(project, path)
            module_order = cls.get_order(project, module)
            return (module_order, module, path.name)

        return sorted(cases, key=sort_key)

    @staticmethod
    def _module_of(project: str, path: Path) -> str:
        """从文件路径推断模块名。"""
        parts = path.parts
        if project in parts:
            idx = parts.index(project)
            if len(parts) > idx + 1:
                return parts[idx + 1]
        return ""
