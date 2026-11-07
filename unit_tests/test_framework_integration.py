"""框架各层能否正确加载（防止改了某个文件把整个框架搞挂）。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class TestImports:
    def test_verify_layer_imports(self) -> None:
        from verify.db import DatabaseClient
        from verify.manager import VerifyManager
        from verify.mq import MemoryBackend, build_backend
        from verify.redis import RedisClient

        assert DatabaseClient is not None
        assert RedisClient is not None
        assert MemoryBackend is not None
        assert VerifyManager is not None
        assert build_backend is not None

    def test_factory_layer_imports(self) -> None:
        from factory.base import DataFactory, registry
        from factory.cleanup import stack
        from factory.demo_shop import OrderFactory, ProductFactory

        assert DataFactory is not None
        assert OrderFactory is not None
        assert ProductFactory is not None
        assert registry is not None
        assert stack is not None

    def test_tools_ai_imports(self) -> None:
        from ai.capabilities.case_gen import generate_cases
        from ai.capabilities.client import LLMClient, get_client
        from ai.capabilities.diagnosis import diagnose

        assert generate_cases is not None
        assert diagnose is not None
        assert LLMClient is not None
        assert get_client is not None

    def test_ci_imports(self) -> None:
        from ci.run_ci import build_pytest_args, check_dependencies

        assert callable(build_pytest_args)
        assert callable(check_dependencies)

    def test_rate_limiter_imports(self) -> None:
        from utils.infra.rate_limiter import RateLimiter, TokenBucket, get_limiter

        assert TokenBucket is not None
        assert RateLimiter is not None
        assert get_limiter is not None


class TestConfig:
    def test_config_yaml_parses(self) -> None:
        import yaml

        from utils.paths import CONFIG_FILE

        data = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8"))
        assert isinstance(data, dict)
        assert "projects" in data

    def test_verify_sections_exist(self) -> None:
        """校验层的三块配置都要在（值可以为空，但键要在）。"""
        import yaml

        from utils.paths import CONFIG_FILE

        data = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8"))
        for key in ("db", "redis", "mq", "rate_limit", "retry", "ai"):
            assert key in data, f"config.yaml 缺少 {key} 配置"

    def test_factories_yaml_parses(self) -> None:
        import yaml

        from utils.paths import CONFIGS_DIR

        path = CONFIGS_DIR / "factories.yaml"
        assert path.exists(), "缺少 configs/factories.yaml"

        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict)
        assert "demo_shop" in data

    def test_factories_load(self) -> None:
        """工厂配置里的工厂都能加载出来。"""
        from factory.base import load_factories, registry

        registry.clear()
        count = load_factories()
        assert count >= 3, "至少要能加载出 3 个工厂"
        assert "demo_shop.order" in registry.names()


class TestPlugins:
    def test_plugins_are_importable(self) -> None:
        """四个插件都要能 import（注册在根 conftest.py 里）。"""
        import framework.plugins.factory_plugin  # noqa: F401
        import framework.plugins.ratelimit_plugin  # noqa: F401
        import framework.plugins.retry_plugin  # noqa: F401
        import framework.plugins.verify_plugin  # noqa: F401

    def test_retry_plugin_has_hooks(self) -> None:
        from framework.plugins import retry_plugin

        assert hasattr(retry_plugin, "pytest_runtest_call")
        assert hasattr(retry_plugin, "pytest_configure")

    def test_verify_plugin_has_hooks(self) -> None:
        from framework.plugins import verify_plugin

        assert hasattr(verify_plugin, "pytest_configure")
        assert hasattr(verify_plugin, "pytest_unconfigure")


class TestCaseSchema:
    def test_factory_op_is_allowed(self) -> None:
        """工厂操作类型要能通过用例校验。"""
        from utils.cases.case_schema import validate_setups, validate_teardowns

        assert validate_setups(
            [{"id": "order", "operation_type": "factory", "factory": "mall4j.order"}]
        )
        assert validate_teardowns(
            [{"id": "clean", "operation_type": "factory_cleanup", "factory": "mall4j.order"}]
        )

    def test_factory_without_name_is_rejected(self) -> None:
        from utils.cases.case_schema import TestCaseError, validate_setups

        with pytest.raises(TestCaseError):
            validate_setups([{"id": "order", "operation_type": "factory"}])

    def test_unknown_op_is_rejected(self) -> None:
        from utils.cases.case_schema import TestCaseError, validate_setups

        with pytest.raises(TestCaseError):
            validate_setups([{"id": "x", "operation_type": "unknown_op"}])


class TestLayering:
    """分层约束：utils/ 不能反向依赖上层（见 docs/ARCHITECTURE.md 第五节）。

    分层塌了不会立刻报错，但会慢慢变成「什么都 import 什么」的一团，
    所以这里用静态扫描守住这条线。
    """

    #: utils/ 里允许 import 的顶层包 —— 只能是能力层自己/stdlib/第三方
    FORBIDDEN_PREFIXES = (
        "testcases.",
        "framework.",
        "scripts.",
        "ci.",
        "unit_tests.",
    )

    def _utils_py_files(self) -> list[Path]:
        return [
            p
            for p in (PROJECT_ROOT / "utils").rglob("*.py")
            if "__pycache__" not in p.parts
        ]

    def test_utils_does_not_import_upper_layers(self) -> None:
        import ast

        violations: list[str] = []
        for path in self._utils_py_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                targets: list[str] = []
                if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    targets.append(node.module)
                elif isinstance(node, ast.Import):
                    targets = [a.name for a in node.names]
                for target in targets:
                    if target.startswith(self.FORBIDDEN_PREFIXES):
                        rel = path.relative_to(PROJECT_ROOT).as_posix()
                        violations.append(f"{rel}:{node.lineno} → {target}")

        assert not violations, "utils/ 反向依赖了上层：\n" + "\n".join(violations)

    def test_all_utils_subpackages_importable(self) -> None:
        """各子包能独立 import（防止拆包时漏写 __init__ 或循环依赖）。"""
        import importlib

        for pkg in (
            "http",
            "assertions",
            "cases",
            "config",
            "logs",
            "slider",
            "infra",
            "excel",
        ):
            assert importlib.import_module(f"utils.{pkg}") is not None
