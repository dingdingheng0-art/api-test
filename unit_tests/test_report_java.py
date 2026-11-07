"""allure 生成时 Java 自动探测的测试。

allure 命令行本身是 Java 程序。之前 ``allure generate`` 失败（Windows
上退出码 9009）就是因为 java 不在 PATH、而 JAVA_HOME 只配在用户环境
变量里、进程没继承到。``_find_java_home`` / ``_allure_env`` 负责补上。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from framework.report_generator import _allure_env, _find_java_home  # noqa: E402

_JAVA_NAME = "java.exe" if sys.platform == "win32" else "java"


def _fake_java_home(root: Path) -> Path:
    home = root / "jdk-test"
    (home / "bin").mkdir(parents=True)
    (home / "bin" / _JAVA_NAME).write_text("", encoding="utf-8")
    return home


def test_find_java_home_uses_env_var(tmp_path, monkeypatch):
    home = _fake_java_home(tmp_path)
    monkeypatch.setenv("JAVA_HOME", str(home))

    assert _find_java_home() == str(home)


def test_find_java_home_ignores_broken_env_var(tmp_path, monkeypatch):
    """JAVA_HOME 指向不存在的目录 —— 不能直接信，要继续往下找。"""
    broken = tmp_path / "not-a-jdk"
    broken.mkdir()
    monkeypatch.setenv("JAVA_HOME", str(broken))

    found = _find_java_home()

    assert found != str(broken)
    if found is not None:
        assert (Path(found) / "bin" / _JAVA_NAME).exists()


def test_allure_env_injects_java_home_and_path(tmp_path, monkeypatch):
    home = _fake_java_home(tmp_path)
    monkeypatch.setenv("JAVA_HOME", str(home))
    monkeypatch.setenv("PATH", r"C:\somewhere-else")

    env = _allure_env()

    assert env["JAVA_HOME"] == str(home)
    assert env["PATH"].split(os.pathsep)[0] == str(home / "bin")
