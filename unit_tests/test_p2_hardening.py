"""P2 加固项的单元测试。

覆盖四件事：

1. ``_check_db_write`` —— 数据库 setup/teardown 的危险 SQL 安全闸
2. ``_run_db_op`` —— 参数化 SQL 的传参，以及安全闸在连库之前生效
3. 重试插件 —— ``APITEST_NO_RETRY`` 环境变量、重试成功后撤销失败归因
4. token 缓存 —— 原子写、读-改-写不互相覆盖
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from testcases.case_runner import CaseRunner, _check_db_write


# ---------------------------------------------------------------------------
# 1. 危险 SQL 安全闸
# ---------------------------------------------------------------------------


def _check(query: str, item: dict[str, Any] | None = None,
           case: dict[str, Any] | None = None) -> None:
    _check_db_write(query, item or {}, case, kind="setup", op_id="sql")


def test_select_passes() -> None:
    _check("SELECT id FROM admin_area WHERE id = %s")


def test_delete_with_where_passes() -> None:
    _check("DELETE FROM admin_area WHERE id = %s")


def test_delete_without_where_is_rejected() -> None:
    with pytest.raises(RuntimeError, match="没有 WHERE"):
        _check("DELETE FROM admin_area")


def test_update_without_where_is_rejected() -> None:
    with pytest.raises(RuntimeError, match="没有 WHERE"):
        _check("UPDATE admin_area SET name = 'x'")


@pytest.mark.parametrize("query", [
    "DROP TABLE admin_area",
    "TRUNCATE TABLE admin_area",
    "ALTER TABLE admin_area ADD COLUMN x INT",
    "GRANT ALL ON *.* TO 'x'@'%'",
    "  revoke select on db.* from u  ",
])
def test_structure_changes_are_rejected(query: str) -> None:
    with pytest.raises(RuntimeError, match="结构变更/清表"):
        _check(query)


def test_multiline_and_extra_spaces_are_normalized() -> None:
    """换行/多空格拼出来的 DELETE 也要能认出来。"""
    with pytest.raises(RuntimeError, match="没有 WHERE"):
        _check("DELETE\n   FROM   admin_area\n")


def test_lowercase_is_rejected_too() -> None:
    with pytest.raises(RuntimeError, match="没有 WHERE"):
        _check("delete from admin_area")


def test_comments_cannot_fake_a_where_clause() -> None:
    """``-- WHERE ...`` 是注释，不是条件，不能被当成"有 WHERE"。"""
    with pytest.raises(RuntimeError, match="没有 WHERE"):
        _check("DELETE FROM admin_area -- WHERE id = 1")

    with pytest.raises(RuntimeError, match="没有 WHERE"):
        _check("UPDATE admin_area SET x = 1 /* WHERE id = 1 */")


def test_cte_delete_without_where_is_rejected() -> None:
    """``WITH ... DELETE`` 的第一个词是 WITH，动作词在后头。"""
    with pytest.raises(RuntimeError, match="没有 WHERE"):
        _check("WITH x AS (SELECT id FROM admin_area) DELETE FROM admin_area")


def test_cte_delete_with_where_passes() -> None:
    _check(
        "WITH x AS (SELECT id FROM admin_area) "
        "DELETE FROM admin_area WHERE id IN (SELECT id FROM x)"
    )


def test_item_allow_dangerous_passes() -> None:
    _check("DELETE FROM admin_area", item={"allow_dangerous": True})


def test_case_allow_dangerous_passes() -> None:
    _check("DROP TABLE admin_area", case={"allow_dangerous": True})


def test_env_allow_dangerous_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APITEST_ALLOW_DANGEROUS_DELETE", "1")
    _check("TRUNCATE TABLE admin_area")


def test_false_allow_dangerous_does_not_pass() -> None:
    """写了 ``allow_dangerous: false`` 不能当成放行。"""
    with pytest.raises(RuntimeError):
        _check("DELETE FROM admin_area", item={"allow_dangerous": False})


# ---------------------------------------------------------------------------
# 2. _run_db_op：安全闸在连库之前、参数化传参
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.rowcount = 1
        self.closed = False

    def execute(self, query: str, params: Any = None) -> None:
        self.calls.append((query, params))

    def close(self) -> None:
        self.closed = True


class _FakeConnection:
    def __init__(self) -> None:
        self.cursor_obj = _FakeCursor()
        self.committed = False
        self.closed = False

    def cursor(self) -> _FakeCursor:
        return self.cursor_obj

    def commit(self) -> None:
        self.committed = True

    def close(self) -> None:
        self.closed = True


def _install_fake_mysql(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """把 ``mysql.connector`` 换成假实现，记录 connect 参数。"""
    captured: dict[str, Any] = {}
    connection = _FakeConnection()

    def _connect(**kwargs: Any) -> _FakeConnection:
        captured["connect_kwargs"] = kwargs
        return connection

    connector = types.ModuleType("mysql.connector")
    connector.connect = _connect  # type: ignore[attr-defined]
    mysql_mod = types.ModuleType("mysql")
    mysql_mod.connector = connector  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "mysql", mysql_mod)
    monkeypatch.setitem(sys.modules, "mysql.connector", connector)
    captured["connection"] = connection
    return captured


def _runner() -> CaseRunner:
    return CaseRunner(env="test")


_DB_CONFIG = {"mysql": {"host": "127.0.0.1", "database": "shop"}}


def test_run_db_op_passes_params(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _install_fake_mysql(monkeypatch)
    runner = _runner()

    runner._run_db_op(
        {"id": "create", "query": "DELETE FROM admin_area WHERE id = %s",
         "params": [7]},
        _DB_CONFIG,
        kind="teardown",
    )

    connection = captured["connection"]
    assert connection.cursor_obj.calls == [
        ("DELETE FROM admin_area WHERE id = %s", [7])
    ]
    assert connection.committed is True
    assert connection.closed is True


def test_run_db_op_rejects_dangerous_sql_before_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """危险 SQL 必须在 ``connect`` 之前就被拦下 —— 连上再拦已经晚了。"""
    captured = _install_fake_mysql(monkeypatch)

    with pytest.raises(RuntimeError, match="安全闸"):
        _runner()._run_db_op(
            {"id": "wipe", "query": "DELETE FROM admin_area"},
            _DB_CONFIG,
            kind="teardown",
        )

    assert "connect_kwargs" not in captured


def test_run_db_op_requires_query() -> None:
    with pytest.raises(ValueError, match="没写 query"):
        _runner()._run_db_op({"id": "empty"}, _DB_CONFIG, kind="setup")


def test_run_db_op_requires_mysql_config() -> None:
    with pytest.raises(ValueError, match="没有 mysql"):
        _runner()._run_db_op(
            {"id": "q", "query": "SELECT 1"}, {"mysql": None}, kind="setup"
        )


# ---------------------------------------------------------------------------
# 3. 重试插件
# ---------------------------------------------------------------------------


def test_no_retry_env_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    from framework.plugins import retry_plugin

    for value in ("1", "true", "YES", "on"):
        monkeypatch.setenv(retry_plugin.NO_RETRY_ENV, value)
        assert retry_plugin._env_disabled() is True, value

    for value in ("", "0", "false", "no"):
        monkeypatch.setenv(retry_plugin.NO_RETRY_ENV, value)
        assert retry_plugin._env_disabled() is False, value


def test_pytest_configure_honors_no_retry_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from framework.plugins import retry_plugin

    original = dict(retry_plugin._RETRY_CONFIG)
    monkeypatch.setattr(retry_plugin, "_RETRY_CONFIG", dict(original))
    monkeypatch.setenv(retry_plugin.NO_RETRY_ENV, "1")

    class _Config:
        def getoption(self, _name: str, default: Any = None) -> Any:
            return default

    retry_plugin.pytest_configure(_Config())  # type: ignore[arg-type]

    assert retry_plugin._RETRY_CONFIG["enabled"] is False
    # 恢复，别串到别的测试
    retry_plugin._RETRY_CONFIG = original  # type: ignore[assignment]


def test_runtest_setup_resets_last_status() -> None:
    """上一条用例留下的 5xx 不能影响这一条的重试判断。"""
    from framework import hooks
    from utils.http.request_handler import RequestHandler

    RequestHandler._last_status = 503

    class _Item:
        nodeid = "testcases/test_cases.py::test_api_case[case]"
        name = "test_api_case"

    hooks.pytest_runtest_setup(_Item())  # type: ignore[arg-type]
    assert RequestHandler.get_last_status() is None


# ---------------------------------------------------------------------------
# 4. 失败归因的撤销
# ---------------------------------------------------------------------------


def test_unregister_failure_removes_memory_and_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from utils.infra import failure_classifier as fc

    monkeypatch.setattr(fc, "_failures_file", lambda: tmp_path / "attr.jsonl")
    monkeypatch.setattr(fc, "_FAILURES", {})

    diag = fc.FailureDiagnosis(
        attribution=fc.SERVICE, category="服务端 5xx", module="area"
    )
    fc.register_failure("case-a", "area", diag)
    fc.register_failure("case-b", "area", diag)
    assert set(fc._FAILURES) == {"case-a", "case-b"}

    fc.unregister_failure("case-a")

    assert set(fc._FAILURES) == {"case-b"}
    lines = [
        json.loads(line)
        for line in (tmp_path / "attr.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [line["nodeid"] for line in lines] == ["case-b"]


def test_unregister_unknown_nodeid_is_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from utils.infra import failure_classifier as fc

    monkeypatch.setattr(fc, "_failures_file", lambda: tmp_path / "attr.jsonl")
    monkeypatch.setattr(fc, "_FAILURES", {})

    # 文件都不存在时也不能抛异常
    fc.unregister_failure("not-there")
    assert fc._FAILURES == {}


def test_unregister_failure_without_file_is_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from utils.infra import failure_classifier as fc

    monkeypatch.setattr(fc, "_failures_file", lambda: tmp_path / "missing.jsonl")
    fc.unregister_failure("x")


# ---------------------------------------------------------------------------
# 5. token 缓存
# ---------------------------------------------------------------------------


@pytest.fixture()
def cache_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from utils.infra import token_cache

    target = tmp_path / ".cache" / "credentials.json"
    monkeypatch.setattr(token_cache, "CACHE_DIR", target.parent)
    monkeypatch.setattr(token_cache, "CACHE_FILE", target)
    monkeypatch.delenv("APITEST_NO_TOKEN_CACHE", raising=False)
    return target


def test_save_writes_cache_atomically(cache_file: Path) -> None:
    from utils.infra.token_cache import TokenCache

    TokenCache.save("demo", "token-a")

    assert cache_file.exists()
    # 临时文件不能留在磁盘上
    assert not cache_file.with_name(cache_file.name + ".tmp").exists()
    data = json.loads(cache_file.read_text(encoding="utf-8"))
    assert data["demo"]["token"] == "token-a"
    assert data["demo"]["version"] >= 2


def test_save_is_read_modify_write(cache_file: Path) -> None:
    """存第二个项目不能把第一个项目的凭据冲掉。"""
    from utils.infra.token_cache import TokenCache

    TokenCache.save("a", "token-a")
    TokenCache.save("b", "token-b")

    data = json.loads(cache_file.read_text(encoding="utf-8"))
    assert set(data) == {"a", "b"}


def test_clear_only_removes_target_project(cache_file: Path) -> None:
    from utils.infra.token_cache import TokenCache

    TokenCache.save("a", "token-a")
    TokenCache.save("b", "token-b")

    TokenCache.clear("a")

    data = json.loads(cache_file.read_text(encoding="utf-8"))
    assert "a" not in data
    assert "b" in data


def test_cache_file_is_not_world_readable(cache_file: Path) -> None:
    """写完后收权限（POSIX 上有效；Windows 上只要能写成功即可）。"""
    import os

    from utils.infra.token_cache import TokenCache

    TokenCache.save("demo", "token-a")

    if os.name == "posix":
        assert (cache_file.stat().st_mode & 0o777) == 0o600
