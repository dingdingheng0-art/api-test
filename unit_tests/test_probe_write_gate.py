"""探针的写操作环境闸（``ai/pipeline/probe.py``）。

写探针会把请求真的发出去，且**没有配套清理**（DELETE 被禁）。
以前唯一的保护是 ``--include-write`` 的帮助文本里写了句"只在测试环境用" ——
模型或者人随手加个 ``--env online`` 就绕过去了，脏数据留在准生产上。

现在改成硬闸：非测试环境直接报错，连第一个请求都不发。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai.pipeline import probe  # noqa: E402


@pytest.mark.parametrize("env", ["test", "dev", "TEST", " dev "])
def test_write_probe_allowed_in_test_envs(env):
    probe.ensure_write_allowed(env)


@pytest.mark.parametrize("env", ["online", "pre", "prod", "", None])
def test_write_probe_blocked_outside_test_envs(env):
    with pytest.raises(RuntimeError) as exc:
        probe.ensure_write_allowed(env)
    assert "写探针只允许" in str(exc.value)


def test_batch_probe_blocks_before_sending_anything(monkeypatch):
    """批量探针必须在建客户端之前就拦下 —— 不能一条条报「探针失败」。"""
    from tools.importer.models import Endpoint

    called = {"client": False}

    class _Client:
        def __init__(self, *args, **kwargs):
            called["client"] = True

    monkeypatch.setattr(probe, "ProbeClient", _Client)

    ep = Endpoint(id="create", method="POST", path="/admin/x")
    with pytest.raises(RuntimeError):
        probe.probe_endpoints([ep], project="mall4j", env="online", include_write=True)
    assert not called["client"], "环境闸没拦在发请求之前"


def test_read_only_probe_is_not_gated(monkeypatch):
    """只读探针在任何环境都该能跑 —— 闸只管写。"""
    from tools.importer.models import Endpoint

    class _Client:
        env = "online"

        def send(self, *args, **kwargs):
            return {"status_code": 200, "body": {"success": True}, "error": ""}

    class _Ctx:
        contract = {"success_code": "00000"}

        def db_mappings(self, ep):
            return {}

        def examples_for(self, ep, limit=1):
            return []

        def path_values_for(self, ep):
            return {}

    outcomes = probe.probe_endpoint(
        Endpoint(id="list", method="GET", path="/admin/x"), _Client(), _Ctx()
    )
    assert outcomes, "只读探针不该被环境闸挡住"
