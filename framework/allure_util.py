"""Allure 集成。

装了 ``allure-pytest`` 才有完整效果；没装的话所有函数退化成空操作，
用例照跑，只是没有 HTML 报告。

用法（在测试函数里）::

    from framework.allure_util import allure

    allure.dynamic.title("xxx")
    allure.step("第一步")
    allure.attach("响应内容", name="response", attachment_type=allure.attachment_type.TEXT)
"""
from __future__ import annotations

from typing import Any, Callable

from utils.logs.log_manager import mask_text, mask_value

try:  # pragma: no cover - 取决于环境有没有装 allure
    import allure
    from allure_commons.types import AttachmentType

    _HAS_ALLURE = True
except ImportError:  # pragma: no cover
    _HAS_ALLURE = False

    class _FakeAllure:
        """没装 allure 时的空实现（API 保持一致）。"""

        class dynamic:  # noqa: N801 - 对齐 allure 的 API
            """动态标签。"""

            @staticmethod
            def title(_value: str) -> None:
                return None

            @staticmethod
            def description(_value: str) -> None:
                return None

            @staticmethod
            def epic(_value: str) -> None:
                return None

            @staticmethod
            def feature(_value: str) -> None:
                return None

            @staticmethod
            def story(_value: str) -> None:
                return None

            @staticmethod
            def parent_suite(_value: str) -> None:
                return None

            @staticmethod
            def suite(_value: str) -> None:
                return None

            @staticmethod
            def sub_suite(_value: str) -> None:
                return None

            @staticmethod
            def severity(_value: str) -> None:
                return None

            @staticmethod
            def tag(_value: str) -> None:
                return None

        @staticmethod
        def parameter(_name: str, _value: Any) -> None:
            return None

        @staticmethod
        def label(_name: str, _value: Any) -> None:
            return None

        class attachment_type:  # noqa: N801
            """附件类型常量。"""

            TEXT = "text/plain"
            JSON = "application/json"
            HTML = "text/html"

        @staticmethod
        def epic(_title: str) -> Callable:
            return lambda func: func

        @staticmethod
        def feature(_title: str) -> Callable:
            return lambda func: func

        @staticmethod
        def story(_title: str) -> Callable:
            return lambda func: func

        @staticmethod
        def severity(_title: str) -> Callable:
            return lambda func: func

        @staticmethod
        def title(_title: str) -> Callable:
            return lambda func: func

        @staticmethod
        def step(_title: str) -> Any:
            """没装 allure 时 ``step`` 要**既能当上下文管理器，也能当装饰器**。

            框架里两种写法都在用：

            - ``with allure.step("发送请求"):`` —— 上下文管理器
            - ``@allure.step("xxx")``          —— 装饰器

            以前这里只返回装饰器，结果没装 allure 的环境里
            ``with allure.step(...)`` 直接 TypeError，**每条用例都挂**。
            返回一个同时实现 ``__enter__``/``__exit__`` 和 ``__call__`` 的对象，
            两种用法都能用。
            """

            class _NoopStep:
                def __enter__(self) -> "_NoopStep":
                    return self

                def __exit__(self, *_exc: Any) -> bool:
                    return False  # 不吞异常

                def __call__(self, func: Callable) -> Callable:
                    return func

            return _NoopStep()

        @staticmethod
        def attach(*_args: Any, **_kwargs: Any) -> None:
            return None

        @staticmethod
        def label(label_type: str, value: Any) -> Any:
            """加一个 allure 标签（收集阶段用）。"""
            def deco(func: Callable) -> Callable:
                return func

            return deco

    allure = _FakeAllure()  # type: ignore[assignment]
    AttachmentType = None  # type: ignore[assignment]


def has_allure() -> bool:
    """是否装了真的 allure。"""
    return _HAS_ALLURE


def attach_text(content: str, name: str = "detail") -> None:
    """把一段文本贴到报告里。

    ⚠️ **内容会被脱敏** —— token / 密码 / 手机号这些敏感字段
    会被打成 ``******``。

    为什么要脱敏：Allure 报告是要发给别人看的（开发、测试主管、
    甚至 CI 归档），而附件里的 Authorization 明文一旦泄露，
    等于把后台账号的凭证公开了。

    日志层早就做了脱敏（``utils/log_manager.py``），但附件走的是
    另一条路 —— 以前没脱敏，导致报告里能直接看到完整 token。
    """
    if not _HAS_ALLURE:
        return
    allure.attach(mask_text(content), name=name, attachment_type=AttachmentType.TEXT)


def attach_json(content: Any, name: str = "json") -> None:
    """把 JSON 贴到报告里。

    ⚠️ **内容会被脱敏** —— 同 ``attach_text``。
    """
    if not _HAS_ALLURE:
        return
    import json as _json

    allure.attach(
        _json.dumps(mask_value(content), ensure_ascii=False, indent=2, default=str),
        name=name,
        attachment_type=AttachmentType.JSON,
    )


def attach_error(exc: BaseException, name: str = "异常") -> None:
    """把异常信息贴到报告里（AI 缺陷分析也会读这个）。

    堆栈本身也过一遍脱敏 —— 断言失败信息里可能夹带响应体，
    响应体里可能有敏感字段。
    """
    if not _HAS_ALLURE:
        return
    import traceback

    text = "".join(
        traceback.format_exception(type(exc), exc, exc.__traceback__)
    )
    allure.attach(mask_text(text), name=name, attachment_type=AttachmentType.TEXT)


__all__ = [
    "allure",
    "has_allure",
    "attach_text",
    "attach_json",
    "attach_error",
    "AttachmentType",
]
