"""session 收尾的 allure 附件。

``allure.attach`` 只能在用例/步骤容器里调用。``pytest_sessionfinish``
时所有容器都关闭了，直接 attach 会抛 ``KeyError: None``（以前被
``except: pass`` 吞掉，测试数据清理结果从来没进过报告）。

这里测的是替代实现：按 Allure 2 的文件格式，把附件挂到最后一条
已经落盘的 ``*-result.json`` 上。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from framework.allure_util import attach_json_to_latest_result  # noqa: E402


def _write_result(directory: Path, stop: int, name: str) -> Path:
    path = directory / f"{name}-result.json"
    path.write_text(
        json.dumps({"name": name, "stop": stop, "attachments": []}),
        encoding="utf-8",
    )
    return path


def test_attaches_to_latest_result(tmp_path):
    older = _write_result(tmp_path, 1_000, "a")
    newer = _write_result(tmp_path, 2_000, "b")

    assert attach_json_to_latest_result(
        {"cleaned": 2}, name="测试数据清理结果", results_dir=tmp_path
    )

    latest = json.loads(newer.read_text(encoding="utf-8"))
    assert latest["attachments"] == [
        {
            "name": "测试数据清理结果",
            "source": latest["attachments"][0]["source"],
            "type": "application/json",
        }
    ]
    attachment = tmp_path / latest["attachments"][0]["source"]
    assert json.loads(attachment.read_text(encoding="utf-8")) == {"cleaned": 2}

    # 更早的结果不能被改动
    assert json.loads(older.read_text(encoding="utf-8"))["attachments"] == []


def test_attachment_is_masked(tmp_path):
    """附件和 ``attach_json`` 一样必须先脱敏。"""
    _write_result(tmp_path, 1_000, "a")

    attach_json_to_latest_result(
        {"accessToken": "super-secret-token", "ok": True},
        name="清理结果",
        results_dir=tmp_path,
    )

    (result_file,) = tmp_path.glob("*-result.json")
    data = json.loads(result_file.read_text(encoding="utf-8"))
    attachment = tmp_path / data["attachments"][0]["source"]
    payload = attachment.read_text(encoding="utf-8")

    assert "super-secret-token" not in payload
    assert "******" in payload


def test_no_result_file_returns_false(tmp_path):
    assert attach_json_to_latest_result({"x": 1}, results_dir=tmp_path) is False
    assert attach_json_to_latest_result({"x": 1}, results_dir=None) is False
    assert attach_json_to_latest_result({"x": 1}, results_dir=tmp_path / "nope") is False
