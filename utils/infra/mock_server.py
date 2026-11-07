"""Mock 服务：不依赖被测系统，直接返回预设响应。

**要解决什么问题**

真实接口测试有三件绕不开的麻烦：

1. **下游没准备好** —— 用例要调的接口还没开发完，或者测试环境挂了，
   这时候没法验证「我的断言逻辑对不对」，只能干等
2. **造异常场景太难** —— 「库存不足时返回什么」「服务超时怎么提示」，
   真实环境很难稳定复现
3. **依赖链太长** —— A 接口的返回值喂给 B 接口，B 的再喂给 C，
   中间任何一环挂了整条业务流都测不了

Mock 就是让框架自己扮演被测系统：按预设规则返回响应，
用例照样能跑通、断言照样能校验。

**两种用法**

**一、作为步骤类型**（最常用）—— 不发真实请求，直接返回预设响应：

.. code-block:: yaml

    testcase:
      name: 库存不足时要提示
      steps:
        - id: query_stock
          operation_type: mock
          mock:
            status: 200
            response:
              code: 5001
              message: 库存不足
          assert:
            - type: equal
              field: code
              expected: 5001

**二、作为拦截规则**（全局替换）—— 用例没写 ``operation_type: mock``，
但框架在发请求前先查拦截表，命中了就用预设响应替代真实响应。

拦截表从 ``testcases/mocks/`` 目录读，一个项目一个 YAML 文件：

.. code-block:: yaml

    # testcases/mocks/mall4j.yaml
    project: mall4j
    mocks:
      - name: 库存查询超时
        match:
          method: GET
          path: /prod/prod/stock
          params:
            skuId: "123"
        response:
          code: 504
          message: 服务超时
        delay: 3

**为什么拦截表放在 ``testcases/mocks/``**

和 ``testcases/apis/`` 一个道理 —— 测试资产和用例放在一起，
进 git 就能版本管理，不用另建一套数据库配置。

**加新规则要做什么**

1. 在 ``testcases/mocks/<项目>.yaml`` 里写一条规则
2. 用例照常写正常路径，框架会自动拦截

不需要改代码。
"""
from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from typing import Any

from utils.logs.log_manager import log

# Mock 规则文件所在目录（和 apis/ 平级）
MOCKS_DIR_NAME = "mocks"

# 规则文件里的顶层键
MOCKS_KEY = "mocks"


class MockError(RuntimeError):
    """Mock 配置有问题（规则写错、响应不合法等）。"""


class MockRule:
    """一条 Mock 规则。

    说人话：告诉框架「什么样的请求该被替换，返回什么」。
    """

    def __init__(self, spec: dict[str, Any], source: str = ""):
        self.spec = spec or {}
        self.source = source
        self.name = str(self.spec.get("name") or "未命名规则")
        self.enabled = bool(self.spec.get("enabled", True))
        self.match = dict(self.spec.get("match") or {})
        self.response = self.spec.get("response")
        self.status = int(self.spec.get("status", 200) or 200)
        self.headers = dict(self.spec.get("headers") or {})
        self.delay = float(self.spec.get("delay", 0) or 0)
        self.error = self.spec.get("error")
        self.project = str(self.spec.get("project") or "")

        self._method = str(self.match.get("method") or "").upper()
        self._path_pattern = self.match.get("path") or ""
        self._path_regex = self._compile_path(self._path_pattern)

    @staticmethod
    def _compile_path(pattern: str) -> re.Pattern[str] | None:
        """把路径匹配串编译成正则。

        支持两种写法：

        - 精确路径：``/api/user`` —— 只匹配这个路径
        - 通配路径：``/api/user/*`` —— 匹配 ``/api/user/`` 开头的一切
          （``*`` 只匹配一段，``**`` 匹配多段）
        """
        if not pattern:
            return None

        text = str(pattern)
        if "*" not in text:
            # 精确匹配时也允许忽略末尾斜杠
            return re.compile(f"^{re.escape(text.rstrip('/'))}/?$")

        # 把 ** 和 * 翻译成正则
        regex_text = re.escape(text)
        regex_text = regex_text.replace(r"\*\*", "\x00")
        regex_text = regex_text.replace(r"\*", "[^/]*")
        regex_text = regex_text.replace("\x00", ".*")
        return re.compile(f"^{regex_text}/?$")

    # ---------------- 匹配 ----------------

    def matches(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        body: Any = None,
        project: str = "",
    ) -> bool:
        """这个请求要不要被拦截。

        Args:
            method: HTTP 方法
            path: 请求路径（不含 host）
            params: 查询参数
            body: 请求体
            project: 项目名
        """
        if not self.enabled:
            return False

        # 项目隔离 —— A 项目的规则不该拦 B 项目的请求
        if self.project and project and self.project != project:
            return False

        if self._method and self._method != str(method or "").upper():
            return False

        if self._path_regex and not self._path_regex.match(str(path or "")):
            return False

        # 参数匹配：规则里写了几个参数，就必须都对得上
        # （没写的参数不限制 —— 不想管那么多细节时）
        expected_params = self.match.get("params")
        if expected_params:
            actual = params or {}
            for key, expected in expected_params.items():
                if str(actual.get(key)) != str(expected):
                    return False

        # 请求体匹配
        expected_body = self.match.get("body")
        if expected_body is not None:
            if not _body_matches(expected_body, body):
                return False

        return True

    # ---------------- 产出响应 ----------------

    def build_response(self) -> tuple[Any, dict[str, Any]]:
        """生成这次要返回的 ``(响应体, 响应元信息)``。

        响应元信息的格式和真实请求保持一致 —— 这样断言里写
        ``status_code`` / ``response_header`` 的用法完全不用改。
        """
        if self.delay > 0:
            log.info("Mock %s 延迟 %s 秒", self.name, self.delay)
            time.sleep(self.delay)

        if self.error:
            # 模拟网络错误：调用方应该按「请求失败」处理
            raise MockConnectionError(self.error)

        meta: dict[str, Any] = {
            "status_code": self.status,
            "headers": {str(k).lower(): v for k, v in self.headers.items()},
            "elapsed_ms": 0,
            "mocked": True,
            "mock_name": self.name,
        }
        return self.response, meta


class MockConnectionError(ConnectionError):
    """Mock 出来的网络错误（模拟服务不可用）。"""


def _body_matches(expected: Any, actual: Any) -> bool:
    """请求体是否匹配。

    规则里只写了要关心的字段就行 —— 其余字段不限制。
    """
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key, want in expected.items():
            if key not in actual:
                return False
            if not _body_matches(want, actual[key]):
                return False
        return True

    return str(expected) == str(actual)


class MockRegistry:
    """Mock 规则表：全局单例。

    职责：

    1. 从 ``testcases/mocks/`` 加载规则
    2. 接到请求时找出第一条匹配的规则
    3. 执行步骤时给出 Mock 响应
    """

    _instance: "MockRegistry | None" = None
    _lock = threading.Lock()

    def __new__(cls) -> "MockRegistry":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._loaded = False
                    cls._instance._rules: list[MockRule] = []
        return cls._instance

    # ---------------- 加载 ----------------

    @property
    def root(self) -> Path:
        """Mock 规则目录。"""
        from utils import paths

        return paths.TESTCASES_DIR / MOCKS_DIR_NAME

    def load(self, force: bool = False) -> list[MockRule]:
        """加载所有规则。

        Args:
            force: 强制重读（改了规则文件后调）
        """
        if self._loaded and not force:
            return self._rules

        with self._lock:
            if self._loaded and not force:
                return self._rules

            rules: list[MockRule] = []
            root = self.root

            if not root.exists():
                self._rules = rules
                self._loaded = True
                return rules

            import yaml

            for path in sorted(root.rglob("*.y*ml")):
                try:
                    with path.open("r", encoding="utf-8") as f:
                        data = yaml.safe_load(f) or {}
                except (OSError, yaml.YAMLError) as exc:
                    log.warning("Mock 规则文件读不了 %s: %s", path.name, exc)
                    continue

                if not isinstance(data, dict):
                    continue

                # 文件顶层的 project 兜底给每条规则
                file_project = str(data.get("project") or "")

                items = data.get(MOCKS_KEY)
                if not isinstance(items, list):
                    continue

                for item in items:
                    if not isinstance(item, dict):
                        continue
                    spec = dict(item)
                    if file_project and not spec.get("project"):
                        spec["project"] = file_project
                    try:
                        rules.append(MockRule(spec, source=str(path)))
                    except (TypeError, ValueError) as exc:
                        log.warning("Mock 规则 %s 写错了（%s）: %s", path.name, exc, spec.get("name"))

            self._rules = rules
            self._loaded = True

            if rules:
                log.info("已加载 %s 条 Mock 规则", len(rules))
            return rules

    def clear(self) -> None:
        """清缓存（改了规则文件后调）。"""
        with self._lock:
            self._rules = []
            self._loaded = False

    # ---------------- 查询 ----------------

    def list_rules(self, project: str = "") -> list[MockRule]:
        """列出规则（按项目筛）。"""
        rules = self.load()
        if not project:
            return list(rules)
        return [r for r in rules if not r.project or r.project == project]

    def find_rule(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        body: Any = None,
        project: str = "",
    ) -> MockRule | None:
        """找出第一条匹配的规则。

        后写的规则**不会**覆盖先写的 —— 顺序即优先级，
        想要更精确的规则就把它写前面。
        """
        for rule in self.load():
            if rule.matches(method, path, params, body, project):
                return rule
        return None

    # ---------------- 应用 ----------------

    def try_intercept(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        body: Any = None,
        project: str = "",
    ) -> tuple[Any, dict[str, Any]] | None:
        """尝试拦截一个请求。

        Returns:
            命中时返回 ``(响应体, 响应元信息)``；没命中返回 None
            （调用方继续走真实请求）。
        """
        rule = self.find_rule(method, path, params, body, project)
        if rule is None:
            return None

        log.info("命中 Mock 规则「%s」: %s %s", rule.name, method, path)
        return rule.build_response()


def get_registry() -> MockRegistry:
    """拿全局 Mock 规则表。"""
    return MockRegistry()


__all__ = [
    "MockError",
    "MockRule",
    "MockRegistry",
    "MockConnectionError",
    "get_registry",
]
