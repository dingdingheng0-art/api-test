"""配置管理：读项目根目录的 ``configs/config.yaml``。

配置文件长这样（项目 / 环境 / 登录信息）::

    projects:
      my_shop:
        online:
          host: https://api.my-shop.com
          is_need_login: True
          login:
            data:
              username: admin
              password: "123456"

同一个文件里还有被测系统的基础信息（``base_url`` / ``origin`` / ``uuid`` /
``slider`` 等）—— 那是 ``utils/`` 下的老代码在用，合并到一处避免出现两个
config.yaml 让人不知道该改哪个。

取配置时会把结果塞进 ``Globals``，这样 YAML 用例里就能写
``{{my_shop.token}}`` 直接引用登录后的 token。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from utils.config.config_util import expand_env
from utils.infra.globals import Globals
from utils.logs.log_manager import log
from utils import paths

_config: dict[str, Any] | None = None


class ConfigManager:
    """读取并缓存 config.yaml。"""

    @classmethod
    def load_config(cls, force: bool = False) -> dict[str, Any]:
        global _config
        if _config is not None and not force:
            return _config

        merged: dict[str, Any] = {}

        # 唯一配置文件：configs/config.yaml
        if paths.CONFIG_FILE.exists():
            try:
                with paths.CONFIG_FILE.open("r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                if isinstance(data, dict):
                    # .env 文件 → 环境变量（环境变量本身优先）
                    try:
                        from utils.config.secrets import apply_env_file

                        apply_env_file()
                    except Exception as exc:  # noqa: BLE001
                        log.warning("加载 .env 失败（已忽略）: %s", exc)
                    # 展开 ${环境变量} 占位符（密码等敏感信息不进 git）
                    merged.update(expand_env(data))
            except (OSError, yaml.YAMLError) as exc:
                log.error("配置读取失败 %s：%s", paths.CONFIG_FILE, exc)
        else:
            log.warning("配置文件不存在：%s", paths.CONFIG_FILE)

        _config = merged
        log.info("读取框架配置成功: %s", paths.CONFIG_FILE)
        return _config

    @classmethod
    def get_project_env_config(
        cls, project_name: str, env: str
    ) -> dict[str, Any] | None:
        """取某个项目在某个环境下的配置。

        返回的是 ``{project_name: 环境配置}`` 这样的字典（没有就返回 None）。
        取到的配置同时写入 ``Globals[project_name]``。
        """
        config = cls.load_config()
        projects = config.get("projects") or {}
        project_config = projects.get(project_name)
        if not project_config:
            log.warning("config.yaml 里没有项目 %r", project_name)
            return None

        env_config = project_config.get(env)
        if not env_config:
            log.warning("项目 %r 里没有环境 %r", project_name, env)
            return None

        cls.validate_config(env_config, project_name, env)
        Globals.set(project_name, env_config)
        return {project_name: env_config}

    @staticmethod
    def validate_config(env_config: dict[str, Any], project_name: str, env: str) -> None:
        """检查配置里必需的字段，缺了就直接报错（配置错误要早暴露，别等跑到一半才炸）。"""
        for key in ("host", "is_need_login"):
            if key not in env_config:
                raise ValueError(f"项目 {project_name}/{env} 的配置缺少必需项 {key!r}")

        if env_config["is_need_login"]:
            login = env_config.get("login") or {}
            # 登录方式有两种：
            #   ① 配置了 custom_method —— 调自己的登录函数，此时 url/method 由那个函数自己负责
            #   ② 没配 —— 走框架内置的登录（比如滑块登录），不需要 url/method
            if login.get("custom_method"):
                return
            if login.get("url") and not login.get("method"):
                raise ValueError(
                    f"项目 {project_name}/{env} 的 login 配了 url 但没配 method"
                )
            data = login.get("data") or {}
            # 配了 username 却没配 password —— 配置错误，要早暴露。
            #
            # ⚠️ 但要区分两种情况：
            #   - password 是**空字符串** → 合法！有些系统的密码就是空的，
            #     而且"空密码应该登录失败"本身就是一条要测的用例
            #   - password **根本没这个键** → 配置漏了，报错
            if data.get("username") and "password" not in data:
                raise ValueError(
                    f"项目 {project_name}/{env} 的 login 配了 username 但没配 password。"
                    f"密码别写在配置文件里 —— 复制 .env.example 成 .env，"
                    f"填上 MALL4J_PASSWORD 即可"
                )

    @classmethod
    def get_all(cls) -> dict[str, Any]:
        """整个配置（原始 dict）。"""
        return cls.load_config()

    @classmethod
    def get_db_config(cls, alias: str = "default") -> dict[str, Any] | None:
        """取某个别名的数据库连接配置。

        配置写法（和校验层 ``verify/`` 一致）::

            db:
              default:
                type: mysql
                host: 127.0.0.1
                user: root
                password: "${DB_PASSWORD}"
                database: shop

        也支持扁平的写法（不嵌套别名）::

            db: {type: mysql, host: ..., database: ...}

        Returns:
            连接配置字典；没配返回 None
        """
        db_section = cls.load_config().get("db")
        if not db_section:
            return None

        if not isinstance(db_section, dict):
            return None

        # 扁平的写法 —— 直接就是连接配置
        if "type" in db_section or "host" in db_section or "database" in db_section:
            return dict(db_section)

        # 带别名的写法
        cfg = db_section.get(alias)
        if cfg is None and alias == "default":
            # 只有一个别名就用它
            values = [v for v in db_section.values() if isinstance(v, dict)]
            if len(values) == 1:
                return dict(values[0])
            return None

        return dict(cfg) if isinstance(cfg, dict) else None

    @classmethod
    def get_env(cls) -> str:
        """当前环境名：命令行 ``--env`` > 环境变量 ``APITEST_ENV`` > config.yaml 的 default_env > online。"""
        for candidate in (
            os.environ.get("APITEST_ENV"),
            (cls.load_config().get("default_env")),
            "online",
        ):
            if candidate:
                return str(candidate)
        return "online"
