"""日志保留策略单测（``utils.log_manager._purge_old_logs``）。

不联网、不碰真实 ``logs/`` —— 全部在 ``tmp_path`` 里造文件。
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from utils.logs import log_manager


def _make(log_dir, name: str) -> None:
    (log_dir / name).write_text("x", encoding="utf-8")


@pytest.fixture()
def log_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(log_manager, "LOG_DIR", tmp_path)
    return tmp_path


def test_purge_removes_files_older_than_keep_days(log_dir):
    old = f"{(date.today() - timedelta(days=10)):%Y-%m-%d}"
    fresh = f"{(date.today() - timedelta(days=1)):%Y-%m-%d}"
    _make(log_dir, f"{old}.log")
    _make(log_dir, f"{old}.error.log")
    _make(log_dir, f"{fresh}.log")
    _make(log_dir, f"{fresh}.error.log")

    log_manager._purge_old_logs(7)

    assert not (log_dir / f"{old}.log").exists()
    assert not (log_dir / f"{old}.error.log").exists()
    assert (log_dir / f"{fresh}.log").exists()
    assert (log_dir / f"{fresh}.error.log").exists()


def test_purge_keeps_file_exactly_on_boundary(log_dir):
    """正好 keep_days 天前的文件保留（cutoff 是严格小于）。"""
    boundary = f"{(date.today() - timedelta(days=7)):%Y-%m-%d}"
    _make(log_dir, f"{boundary}.log")

    log_manager._purge_old_logs(7)

    assert (log_dir / f"{boundary}.log").exists()


def test_purge_ignores_non_dated_files(log_dir):
    _make(log_dir, "test.log")
    _make(log_dir, "keep_me.txt")
    _make(log_dir, "2026-13-45.log")  # 日期非法，按普通文件跳过

    log_manager._purge_old_logs(7)

    assert (log_dir / "test.log").exists()
    assert (log_dir / "keep_me.txt").exists()
    assert (log_dir / "2026-13-45.log").exists()


def test_purge_disabled_when_keep_days_not_positive(log_dir):
    old = f"{(date.today() - timedelta(days=30)):%Y-%m-%d}"
    _make(log_dir, f"{old}.log")

    log_manager._purge_old_logs(0)
    log_manager._purge_old_logs(-1)

    assert (log_dir / f"{old}.log").exists()


def test_get_does_not_crash_on_bad_env(log_dir, monkeypatch):
    """APITEST_LOG_KEEP_DAYS 写错时退回默认值，不能把日志初始化搞崩。"""
    monkeypatch.setenv("APITEST_LOG_KEEP_DAYS", "not-a-number")
    log_manager._LogManager.reset()
    try:
        logger = log_manager._LogManager.get()
        assert logger is not None
    finally:
        log_manager._LogManager.reset()
