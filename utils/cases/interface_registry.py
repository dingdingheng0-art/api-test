"""接口定义复用：一个接口定义，多条用例引用。

**问题**

以前用例里写死了完整路径::

    steps:
      - id: query_order
        path: /admin/statistics/order
        method: GET

同一个接口被 8 条用例用，就要写 8 遍。接口路径一改（比如加了版本前缀
``/api/v2``），8 个地方都要改 —— 漏一个就是一条挂掉的用例。

**解法**

把接口定义抽出来，放到 ``testcases/apis/<项目>/<模块>.yaml``::

    interfaces:
      query_order:
        method: GET
        path: /admin/statistics/order
        headers:
          accesstoken: '{{mall4j.token}}'
        timeout: 30

用例里引用它::

    steps:
      - id: query_order
        api: query_order          # 引用接口定义
        params:
          searchType: '{{searchType}}'
        assert:
          - type: equal
            field: code
            expected: 200

改接口路径只改接口定义一处，所有引用它的用例自动跟着变。

**优先级**

一步里可以同时写 ``api`` 和 ``path`` / ``method``。此时：

- 接口定义里有的 → 用接口定义的
- 用例里明确写了 → 用例里写的覆盖（"这个用例就是特殊一点"）

**怎么用**

在 YAML 里写 ``api: <接口名>``，框架在**收集阶段**就会把它展开成完整的
``path`` / ``method`` / ``headers``。展开后的结果和原来手写的一样，
执行引擎一行都不用改。

不用改任何配置文件 —— 框架会自动扫 ``testcases/apis/`` 目录。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from utils.logs.log_manager import log

# 接口定义目录（相对 testcases/）
API_DIR_NAME = "apis"

# 接口定义文件里的顶层键
INTERFACES_KEY = "interfaces"


class InterfaceRegistry:
    """接口定义注册表。

    从 ``testcases/apis/<项目>/<模块>.yaml`` 读接口定义，
    按 ``项目 + 模块 + 接口名`` 索引。
    """

    _instance: "InterfaceRegistry | None" = None

    def __init__(self, api_root: Path | None = None):
        self.api_root = api_root
        self._data: dict[str, dict[str, Any]] = {}
        self._loaded = False

    @classmethod
    def get(cls, api_root: Path | None = None) -> "InterfaceRegistry":
        if cls._instance is None:
            cls._instance = cls(api_root)
        return cls._instance

    # ---------------- 加载 ----------------

    @property
    def root(self) -> Path:
        """接口定义根目录。"""
        if self.api_root is not None:
            return self.api_root
        from utils import paths

        return paths.TESTCASES_DIR / API_DIR_NAME

    def load(self, force: bool = False) -> dict[str, dict[str, Any]]:
        """加载所有接口定义。

        Returns:
            ``{"项目/模块/接口名": 接口定义, ...}``
        """
        if self._loaded and not force:
            return self._data

        self._data = {}
        self._loaded = True

        root = self.root
        if not root.exists():
            log.debug("没有接口定义目录: %s", root)
            return self._data

        import yaml

        for path in sorted(root.rglob("*.y*ml")):
            try:
                with path.open("r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
            except (OSError, yaml.YAMLError) as exc:
                log.warning("接口定义文件读不了 %s: %s", path.name, exc)
                continue

            if not isinstance(data, dict):
                continue

            interfaces = data.get(INTERFACES_KEY)
            if not isinstance(interfaces, dict):
                # 也支持直接写接口名（不套 interfaces 层）
                interfaces = {
                    k: v for k, v in data.items()
                    if isinstance(v, dict)
                }

            # 从路径推项目和模块：apis/<项目>/<模块>.yaml
            rel = path.relative_to(root)
            project = rel.parts[0] if len(rel.parts) >= 2 else ""
            module = path.stem

            for name, spec in interfaces.items():
                if not isinstance(spec, dict):
                    continue
                key = self._key(project, module, str(name))
                self._data[key] = spec

        if self._data:
            log.info("已加载 %s 个接口定义（来自 %s）", len(self._data), root.name)
        return self._data

    @staticmethod
    def _key(project: str, module: str, name: str) -> str:
        return f"{project}/{module}/{name}"

    # ---------------- 查询 ----------------

    def find(
        self,
        name: str,
        project: str = "",
        module: str = "",
    ) -> dict[str, Any] | None:
        """按名字找接口定义。

        查找顺序（越精确越优先）：

        1. ``项目/模块/接口名``
        2. ``项目/接口名``
        3. 任意同名（只有一个时用它）
        """
        data = self.load()
        if not data:
            return None

        if project and module:
            exact = self._key(project, module, name)
            if exact in data:
                return data[exact]

        if project:
            scoped = self._key(project, "", name)
            if scoped in data:
                return data[scoped]

        # 全局搜同名的
        matches = [
            (key, spec)
            for key, spec in data.items()
            if key.rsplit("/", 1)[-1] == name
        ]
        if len(matches) == 1:
            return matches[0][1]
        if len(matches) > 1:
            log.warning(
                "接口名 %r 在多个模块里都有，用例里建议写全（项目/模块/接口名）：%s",
                name,
                "、".join(key for key, _ in matches),
            )
        return None

    def list_all(self, project: str = "") -> list[tuple[str, dict[str, Any]]]:
        """列出所有接口定义。"""
        data = self.load()
        items = [
            (key, spec)
            for key, spec in data.items()
            if not project or key.startswith(f"{project}/")
        ]
        return sorted(items, key=lambda x: x[0])

    def clear(self) -> None:
        """清缓存（改了接口定义文件后调）。"""
        self._loaded = False
        self._data = {}


def expand_step_with_api(
    step: dict[str, Any],
    project: str = "",
    module: str = "",
) -> dict[str, Any]:
    """把步骤里的 ``api: xxx`` 展开成完整的步骤。

    **不会修改原字典** —— 返回一个新字典。

    Args:
        step: 原始步骤
        project: 项目名
        module: 模块名

    Returns:
        展开后的步骤（``path`` / ``method`` / ``headers`` 都填好了）
    """
    api_name = step.get("api")
    if not api_name:
        return step

    registry = InterfaceRegistry.get()
    spec = registry.find(str(api_name), project=project, module=module)

    if spec is None:
        # 找不到就原样返回 —— 后面跑的时候会因为缺 path 报错，报错信息更直白
        log.warning("步骤 %s 引用的接口 %r 找不到", step.get("id"), api_name)
        return step

    result = dict(step)

    # 接口定义里的字段作为基础
    for key, value in spec.items():
        if key in ("name", "description"):
            result.setdefault(key, value)
            continue
        result.setdefault(key, value)

    # 用例里明确写的覆盖接口定义的
    for key in ("path", "method", "headers", "params", "data", "timeout"):
        if step.get(key) is not None:
            result[key] = step[key]

    # headers 要**合并**而不是覆盖 ——
    # 接口定义里带了 token 头，用例又加了个自定义头，两个都要
    base_headers = spec.get("headers") or {}
    own_headers = step.get("headers") or {}
    if isinstance(base_headers, dict) and isinstance(own_headers, dict):
        result["headers"] = {**base_headers, **own_headers}

    result.pop("api", None)
    result["_api_ref"] = str(api_name)

    return result


def expand_case(
    case: dict[str, Any],
    project: str = "",
    module: str = "",
) -> dict[str, Any]:
    """把一条用例里的所有步骤都展开。

    Args:
        case: 用例数据（``{"testcase": {...}}`` 或纯 testcase）
        project: 项目名
        module: 模块名

    Returns:
        展开后的用例（**新对象**，不改原数据）
    """
    if not isinstance(case, dict):
        return case

    inner = case.get("testcase") if isinstance(case.get("testcase"), dict) else case
    steps = inner.get("steps")

    if not isinstance(steps, list):
        return case

    # 没有一个步骤引用接口定义就不用动
    if not any(isinstance(s, dict) and s.get("api") for s in steps):
        return case

    result = dict(case)
    inner_copy = dict(inner)
    inner_copy["steps"] = [
        expand_step_with_api(s, project=project, module=module)
        if isinstance(s, dict)
        else s
        for s in steps
    ]
    result["testcase"] = inner_copy
    return result
