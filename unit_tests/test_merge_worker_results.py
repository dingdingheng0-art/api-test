"""并行跑完以后，worker 的 allure 结果合并。

以前只搬 ``*.json``：``result.json`` 里按文件名引用的附件
（请求响应、异常堆栈……）全留在 worker 目录里，合并完目录一删，
报告里所有附件都变成"附件缺失"。这里锁住两个行为：

1. 附件也要一起搬过去；
2. 重名的结果文件换名后，必须仍然以 ``-result.json`` 结尾 ——
   allure2 是按文件名后缀识别结果文件的，拼错位置等于丢掉这条用例。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import framework.report_generator as report_generator  # noqa: E402


def _write(path: Path, content: str = "{}") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_merge_copies_attachments_too(tmp_path, monkeypatch):
    monkeypatch.setattr(report_generator, "ALLURE_RESULTS", tmp_path)

    worker = tmp_path / "worker-gw0"
    result = _write(
        worker / "r1-result.json",
        json.dumps(
            {
                "name": "case",
                "attachments": [{"name": "响应", "source": "a1-attachment.json"}],
            }
        ),
    )
    _write(worker / "a1-attachment.json", '{"ok": true}')

    merged = report_generator.merge_worker_results()

    assert merged == 2
    assert (tmp_path / result.name).exists()
    # 关键：附件必须跟着搬，否则 result.json 的 source 悬空
    assert (tmp_path / "a1-attachment.json").read_text(encoding="utf-8") == '{"ok": true}'
    assert not worker.exists()


def test_collision_rename_keeps_allure_suffix(tmp_path, monkeypatch):
    monkeypatch.setattr(report_generator, "ALLURE_RESULTS", tmp_path)

    _write(tmp_path / "same-result.json", "{}")
    _write(tmp_path / "worker-gw0" / "same-result.json", "{}")

    assert report_generator.merge_worker_results() == 1

    renamed = tmp_path / "same-worker-gw0-result.json"
    assert renamed.exists()
    assert renamed.name.endswith("-result.json")


def test_collision_on_attachment_is_skipped_not_renamed(tmp_path, monkeypatch):
    """附件改名会让 result.json 的 source 指空，只能保留主目录那份。"""
    monkeypatch.setattr(report_generator, "ALLURE_RESULTS", tmp_path)

    _write(tmp_path / "a1-attachment.txt", "主目录")
    _write(tmp_path / "worker-gw0" / "a1-attachment.txt", "worker")

    assert report_generator.merge_worker_results() == 0
    assert (tmp_path / "a1-attachment.txt").read_text(encoding="utf-8") == "主目录"
    assert not (tmp_path / "a1-worker-gw0-attachment.txt").exists()
