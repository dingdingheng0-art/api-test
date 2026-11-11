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

import json as _json
import os
import uuid
from pathlib import Path
from typing import Any, Callable

from utils.logs.log_manager import log, mask_text, mask_value

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
        def label(_label_type: str, _value: Any) -> None:
            """加一个 allure 标签。

            和真 allure 一致：没装 allure-pytest 时 ``allure.label``
            也返回 ``None``（没有 listener 处理这个标签）。调用点
            （``framework/pytest_plugin.py``）本来就在 ``has_allure()``
            之下，这里只是保证 API 不炸。

            ⚠️ 别再额外定义一个「装饰器版」的 ``label`` —— 同名方法
            后者会悄悄覆盖前者，而且 ``item.add_marker(allure.label(...))``
            拿到一个函数也没法用。
            """
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


def attach_json_to_latest_result(
    content: Any,
    name: str = "json",
    results_dir: str | Path | None = None,
) -> bool:
    """把 JSON 附件挂到**最后一条已落盘**的 allure 结果上。

    **为什么需要这个函数**

    ``allure.attach()`` 只能在用例/步骤容器里调用。``pytest_sessionfinish``
    时所有容器都已关闭，``AllureReporter`` 取不到当前 executable，
    直接 attach 会抛 ``KeyError: None`` —— 测试数据清理结果以前就是这样
    被 ``except: pass`` 静默吞掉，从来没进过报告。

    这里不碰 reporter 的内部状态，而是按 Allure 2 的文件格式，
    把附件写进最后一条 ``*-result.json`` 的 ``attachments`` 里。
    附件内容同样先脱敏，与 ``attach_json`` 保持一致。

    Args:
        results_dir: pytest 的 ``--alluredir`` 目录。``None`` 或不存在时
            什么都不做（没开 allure 报告时本来就没有结果文件）。

    Returns:
        True = 已写入；False = 没有可挂载的结果（比如一条用例都没跑）。
    """
    if not results_dir:
        return False
    directory = Path(results_dir)
    if not directory.is_dir():
        return False

    # 最后一条结果 = ``stop`` 最大的一条（allure 用毫秒时间戳）。
    # 文件头写着 stop 丢了的话退回文件 mtime。
    latest: tuple[float, Path, dict[str, Any]] | None = None
    for path in directory.glob("*-result.json"):
        try:
            data = _json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        try:
            order = float(data.get("stop"))
        except (TypeError, ValueError):
            try:
                order = path.stat().st_mtime
            except OSError:
                continue
        if latest is None or order >= latest[0]:
            latest = (order, path, data)

    if latest is None:
        return False

    _, result_path, result = latest
    attachment_name = f"{uuid.uuid4().hex}-attachment.json"
    payload = _json.dumps(
        mask_value(content), ensure_ascii=False, indent=2, default=str
    )
    try:
        (directory / attachment_name).write_text(payload, encoding="utf-8")
        attachments = result.get("attachments")
        if not isinstance(attachments, list):
            attachments = []
            result["attachments"] = attachments
        attachments.append(
            {
                "name": name,
                "source": attachment_name,
                "type": "application/json",
            }
        )
        # 先写临时文件再替换，避免中途失败留下半截 result.json
        tmp = result_path.with_suffix(".tmp")
        tmp.write_text(
            _json.dumps(result, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        os.replace(tmp, result_path)
    except OSError as exc:
        log.warning("写 allure 附件失败 %s: %s", name, exc)
        return False

    return True


__all__ = [
    "allure",
    "has_allure",
    "attach_text",
    "attach_json",
    "attach_error",
    "attach_json_to_latest_result",
    "AttachmentType",
]
