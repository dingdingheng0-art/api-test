"""登录调度：判断某个项目要不要登录、要不要重新登录。

规则（跟参考框架一致）：
- 项目配置 ``is_need_login: False`` → 不登录
- 全局里已经有 token → 不重复登录（省时间）
- 后置清理阶段若需要登录态，会强制重新登录拿一个干净的 token
"""
from __future__ import annotations

from typing import Any

from testcases.auth import Auth
from utils.config.config_manager import ConfigManager
from utils.infra.globals import Globals
from utils.logs.log_manager import log


class ProjectLoginHandler:
    """负责按需登录。"""

    def __init__(self) -> None:
        self.auth = Auth()

    def login_if_needed(
        self, project_name: str, project_config: dict[str, Any], env: str
    ) -> str | None:
        """如果配置要求登录，就登录一次并返回 token。"""
        if not project_config or not project_config.get("is_need_login"):
            return None

        login_config = project_config.get("login") or {}
        log.info("开始登录 %s，登录配置: %s", project_name, login_config)
        try:
            token = self.auth.login_and_store(project_name, login_config, env)
        except Exception as exc:
            log.error("%s 登录失败: %s", project_name, exc)
            raise

        if token:
            log.info("%s 登录成功", project_name)
        return token

    def check_and_login_project(self, project_name: str, env: str) -> None:
        """确保某个项目处于已登录状态（已有 token 就跳过）。"""
        project_info = Globals.get(project_name)
        if project_info and project_info.get("token"):
            log.info("%s 已有 token，跳过登录", project_name)
            return

        project_env_config = ConfigManager.get_project_env_config(project_name, env)
        if not project_env_config:
            log.warning("未找到项目 %s 的配置，跳过登录", project_name)
            return

        self.login_if_needed(project_name, project_env_config.get(project_name), env)
