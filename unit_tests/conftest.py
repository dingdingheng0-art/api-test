"""单元测试的公共准备。

**为什么要有这个文件**

``test_project_profile.py`` 里有一大批用例在验证"多系统兼容"——
它们拿 ``demo_shop``（示例项目）和 ``mall4j`` 做对比，确认
鉴权头、登录方式、请求头都是按项目配置走的，而不是写死 mall4j 那一套。

但 ``demo_shop`` 在 ``configs/config.yaml`` 里是**注释掉**的：
它的地址是假域名（``example.invalid``），留在生效配置里会让
``ProjectRegistry.list_projects()`` 之类的调用多出一个跑不通的项目。

结果是这批单测全部红着 —— 不是代码坏了，是测试需要的数据不在。

这里在**单元测试范围内**把 ``demo_shop`` 补回配置里，不影响真实运行：

- 只在 pytest 收集 ``unit_tests/`` 时生效
- 只改内存里的那份配置缓存，不写文件
- 地址仍然是假域名，单测全程不发请求
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# 和 configs/config.yaml 里被注释掉的那段保持一致 ——
# 以后真要把示例项目放回去，两边一起改。
_DEMO_PROJECTS: dict[str, Any] = {
    "online": {
        "host": "https://api.example.invalid",
        "is_need_login": True,
        "label": "示例商城（演示多系统兼容）",
        "auth": {
            "token_header": "token",
            "session_header": "sessionId",
            "token_var": "token",
            "session_var": "sessionId",
        },
        "login": {
            "type": "password",
            "path": "/api/auth/login",
            "method": "POST",
            "username_field": "account",
            "password_field": "pwd",
            "password_md5": False,
            "data": {"username": "demo", "password": "demo123"},
        },
    },
    "ticket": {
        "host": "https://api.example.invalid",
        "is_need_login": True,
        "label": "示例商城（自定义登录）",
        "auth": {
            "token_header": "token",
            "session_header": "sessionId",
            "token_var": "token",
            "session_var": "sessionId",
        },
        "login": {
            "type": "custom",
            "custom_method": "adapters.demo.login",
            "data": {"username": "demo", "password": "demo123"},
        },
    },
    "web": {
        "host": "https://api.example.invalid",
        "is_need_login": True,
        "label": "示例商城（有前端）",
        "web_url": "https://admin.example.invalid",
        "login_page": "https://admin.example.invalid/login",
        "auth": {
            "token_header": "token",
            "session_header": "sessionId",
            "token_var": "token",
            "session_var": "sessionId",
        },
        "slider": {
            "init_path": "/api/slider/init",
            "verify_path": "/api/slider/verify",
            "verify_query": {"type": "LOGIN"},
        },
        "login": {"type": "slider"},
    },
}


@pytest.fixture(autouse=True)
def _no_audit_pollution(monkeypatch: pytest.MonkeyPatch) -> None:
    """单测默认**不写审计日志**。

    审计日志（``.cache/agent_audit.jsonl``）是真实调用的长期记录，
    单测每跑一次就往里塞几十条假调用，会把"这个工具最近被调了多少次"
    这类统计彻底搅乱。默认关掉，需要测审计逻辑的用例自己打开。
    """
    monkeypatch.setenv("APITEST_AUDIT", "0")
    monkeypatch.delenv("APITEST_AUDIT_RUN", raising=False)


@pytest.fixture(autouse=True)
def _inject_demo_project(monkeypatch: pytest.MonkeyPatch) -> None:
    """把 demo_shop 注进配置缓存（仅本次测试）。"""
    from utils.config.config_manager import ConfigManager
    from utils.config.project_profile import ProjectRegistry

    original = ConfigManager.load_config()
    patched = dict(original)
    projects = dict(patched.get("projects") or {})
    if "demo_shop" not in projects:
        projects["demo_shop"] = _DEMO_PROJECTS
    patched["projects"] = projects

    monkeypatch.setattr(ConfigManager, "load_config", classmethod(lambda cls, force=False: patched))
    # 档案是带缓存的，配置换了要把旧缓存清掉
    ProjectRegistry.clear_cache()
    yield
    ProjectRegistry.clear_cache()
