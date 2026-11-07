"""用例层级：给 allure 报告组织出清晰的结构。

**为什么要分层**

allure 报告的左侧树是按 ``epic > feature > story`` 分层的。
如果不用它，所有用例都会平铺在一层，几十条用例根本没法看。

框架自动按目录结构推断层级：

.. code-block:: text

    cases/mall4j/auth/login_api.yaml
           │      │   └── story    （文件名）
           │      └── feature   （模块名）
           └── epic        （项目名）

也可以在用例里显式指定（会覆盖自动推断）：

.. code-block:: yaml

    allure:
      epic: 我的系统管理后台
      feature: 认证登录
      story: 账号密码登录
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from utils.cases.module_manager import ModuleManager


class CaseHierarchy:
    """算出一条用例的 allure 层级和结构化标题。"""

    @staticmethod
    def resolve(
        case: dict[str, Any],
        project: str,
        yaml_file: Path | None = None,
        cases_root: Path | None = None,
    ) -> dict[str, str]:
        """算出层级。

        Returns:
            ``{"epic": ..., "feature": ..., "story": ..., "title": ...}``
        """
        meta = case.get("allure") or {}

        module = CaseHierarchy._module_of(project, yaml_file, cases_root)
        module_label = ModuleManager.get_label(project, module) if module else ""

        # epic：项目
        #
        # **框架解析优先** —— 项目档案里的 label 会跟着环境走
        # （"我的系统管理后台（测试环境）"），比用例里手填的死名字准。
        # 手填的只当兜底（框架没档案时用）。
        #
        # 不这么做的话，同一个项目会分裂成两棵树：
        # 手填的 "我的系统管理后台" vs 框架解析的 "我的系统管理后台（测试环境）"。
        epic = CaseHierarchy._project_label(project) or meta.get("epic", "")

        # feature：模块（用模块的短标签，没配就用模块名）
        feature = meta.get("feature") or module_label or CaseHierarchy._humanize(module)

        # story：场景（优先用 allure.story，其次用用例名）
        story = (
            meta.get("story")
            or CaseHierarchy._humanize(case.get("name", ""))
            or (yaml_file.stem if yaml_file else "")
        )

        return {
            "epic": epic,
            "feature": feature,
            "story": story,
            "module": module,
            "title": CaseHierarchy.build_title(case, feature, story),
        }

    @staticmethod
    def build_title(case: dict[str, Any], feature: str, story: str) -> str:
        """拼一个结构化的标题。

        格式：``[模块] 用例名 - 说明``

        比如 ``[商品属性] attribute - （正常请求）``

        **为什么标题里要带模块名**

        报告的失败列表只显示标题，不显示所在目录。以前标题是
        ``attribute - （正常请求）`` —— 光看这条根本不知道是哪个模块，
        而且每个模块都有 ``attribute`` / ``page`` / ``not_exist``
        这种同名用例，分不清谁是谁。

        带上模块名之后，失败列表一眼就能扫出"哪些模块有问题"，
        不用点进附件去找。

        注意：模块名放在最前面（用方括号包住）—— 报告按模块排序时
        同一个模块的用例会挨在一起。
        """
        name = str(case.get("name") or "")
        desc = str(case.get("description") or "")

        core = f"{name} - {desc}" if name and desc else (name or desc or story)

        module = str(feature or "").strip()
        if not module:
            return core
        return f"[{module}] {core}"

    @staticmethod
    def _module_of(
        project: str,
        yaml_file: Path | None,
        cases_root: Path | None,
    ) -> str:
        """从文件路径推模块名。"""
        if yaml_file is None:
            return ""
        if cases_root:
            try:
                rel = yaml_file.relative_to(cases_root)
                if len(rel.parts) >= 2:
                    return rel.parts[1]
            except ValueError:
                pass
        # 没给根目录（或路径不在根目录下）——
        # 按"项目名后面那一段"推（Excel 和 YAML 目录结构一样，都能算对）
        parts = yaml_file.parts
        if project in parts:
            idx = parts.index(project)
            if len(parts) > idx + 1:
                return parts[idx + 1]
        return ""

    @staticmethod
    def _project_label(project: str) -> str:
        """项目名 → 好读的中文标签。

        优先用项目档案里的 ``label``（配置里写的那个），
        没配就退回内置映射，再不行就把英文按驼峰拆开。
        """
        from utils.config.project_profile import ProjectRegistry

        profile = ProjectRegistry.get(project)
        if profile and profile.label:
            return profile.label

        labels = {
            "public_api": "公开接口",
        }
        return labels.get(project, CaseHierarchy._humanize(project))

    @staticmethod
    def _humanize(name: str) -> str:
        """``login_api`` → ``登录 API``；已经是中文就原样返回。"""
        if not name:
            return ""
        if any("\u4e00" <= ch <= "\u9fff" for ch in name):
            return name

        # 拆下划线
        words = [w for w in name.replace("-", "_").split("_") if w]
        # 常见缩写
        abbreviations = {
            "api": "API",
            "id": "ID",
            "url": "URL",
            "info": "信息",
            "list": "列表",
            "order": "订单",
            "statistics": "统计",
            "login": "登录",
            "negative": "反向",
            "menu": "菜单",
            "user": "用户",
            "auth": "认证",
            "goods": "商品",
            "detail": "详情",
        }
        return " ".join(abbreviations.get(w.lower(), w) for w in words)
