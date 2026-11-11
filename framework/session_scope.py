"""判断「这一轮 pytest 在跑什么」—— 接口用例还是框架自己的单测。

**为什么需要**

根 ``conftest.py`` 里无条件加载了框架插件（收集、报告、工厂、校验层、
限流、重试），所以 ``pytest unit_tests`` 也会把整套钩子走一遍。结果是：

- 单测一启动就去连真实数据库（校验层 ``pytest_configure``）
- 单测一启动就加载工厂配置、登记全局工厂
- 单测的失败会被重试插件当成"环境抖动"重试，还可能读上一条用例留下的 5xx
- session 结束时兜底清理、往 allure 目录里贴附件

这些副作用以前只能靠 ``_SESSION_STATE["interface_cases"]`` 事后识别，
而那时 configure 早就跑完了。**必须在 configure 阶段就能判断**，
但那时还没收集用例，只能看「命令行收集目标」和 ``pytest.ini`` 的
``testpaths``。

**判定规则**

1. 命令行给了收集目标（位置参数）→ 目标里有没有接口用例层
   （``testcases/`` 目录、``testcases/test_cases.py``、项目根）
2. 没给 → 看 ``pytest.ini`` 的 ``testpaths``（本项目是 ``unit_tests``）
3. 两者都判断不了（比如单测里传进来的 stub config）→ 按「是接口 session」
   处理：宁可多初始化，也不要漏掉接口用例需要的环境。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

#: 接口用例层的目录名 / 文件名
_INTERFACE_DIRS = ("testcases",)
_INTERFACE_FILES = ("test_cases.py",)


def targets_interface_tests(config: Any) -> bool:
    """这一轮 session 会不会跑接口用例。"""
    args = _positional_args(config)
    if not args:
        # 没给收集目标（或者拿到的是 stub config）→ 按 testpaths 判断
        return _targets_from_testpaths(config)

    root = _rootpath(config)
    for raw in args:
        path = _resolve(raw)
        if path is None:
            continue
        if _is_interface_path(path, root):
            return True
    return False


def _positional_args(config: Any) -> list[str]:
    """命令行里的收集目标（去掉选项）。

    拿不到 ``invocation_params`` 时返回空列表 —— 交给 ``testpaths`` 兜底，
    不再瞎猜。
    """
    params = getattr(config, "invocation_params", None)
    raw_args = getattr(params, "args", None)
    if not raw_args:
        return []

    result: list[str] = []
    for raw in raw_args:
        text = str(raw).strip()
        if not text or text.startswith("-"):
            continue
        result.append(text)
    return result


def _targets_from_testpaths(config: Any) -> bool:
    """没给收集目标时，按 ``pytest.ini`` 的 ``testpaths`` 判断。"""
    try:
        testpaths = list(config.getini("testpaths") or [])
    except (AttributeError, KeyError, TypeError, ValueError):
        # stub config / 老版本 pytest —— 判断不了，保守当成接口 session
        return True

    if not testpaths:
        # pytest 默认收集 rootdir，里面就有 testcases/
        return True

    root = _rootpath(config)
    for item in testpaths:
        path = Path(str(item))
        if not path.is_absolute() and root is not None:
            path = Path(root) / path
        if _is_interface_path(path, root):
            return True
    return False


def _rootpath(config: Any) -> Path | None:
    root = getattr(config, "rootpath", None)
    if root is None:
        return None
    try:
        return Path(root).resolve()
    except (OSError, ValueError):
        return None


def _resolve(raw: str) -> Path | None:
    """把命令行里的位置参数转成路径。

    去掉 ``::测试类::测试函数`` 的 nodeid 尾巴；不是路径的复杂写法
    （比如 ``-k`` 的值已经被上面过滤掉了）解析失败就跳过。
    """
    text = raw.split("::", 1)[0].strip()
    if not text:
        return None
    try:
        return Path(text).resolve()
    except (OSError, ValueError):
        return None


def _is_interface_path(path: Path, root: Path | None) -> bool:
    """这个收集目标里有没有接口用例层。"""
    if path.name in _INTERFACE_FILES:
        return True

    if root is not None:
        if path == root:
            return True
        try:
            parts = path.relative_to(root).parts
        except (OSError, ValueError):
            parts = ()
        if parts and parts[0] in _INTERFACE_DIRS:
            return True

    return False


__all__ = ["targets_interface_tests"]
