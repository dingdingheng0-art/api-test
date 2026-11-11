"""写探针的数据足迹（``ai/pipeline/probe.py``）。

``--include-write`` 造的 POST/PUT/PATCH 数据**不会自动清理**（DELETE 被禁）。
环境闸只保证「不会在准生产上写」，保证不了「测试环境里造的那批谁负责清」。
现在每次写探针成功都留一条足迹：测点、完整请求体、响应里认出的 id、
按文档匹配到的删除接口，命令行跑完摊平成 ``probe_footprints.json``。

这里锁三件事：

1. id 识别只看一层，且不把 0 / 空串 / 布尔当 id
2. 删除接口只认 ``<base>/<单个路径参数>``，嵌套路径和同前缀的其它接口不认
3. 只有**成功**的写探针才留足迹；失败/只读探针一条都不记
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai.pipeline import probe  # noqa: E402
from tools.importer.models import Endpoint, Param  # noqa: E402


# ---------------- id 识别 ----------------


@pytest.mark.parametrize(
    "key",
    ["id", "areaId", "areaID", "area_id", "ID", "parentId"],
)
def test_id_like_keys(key):
    assert probe._looks_like_id_key(key)


@pytest.mark.parametrize("key", ["name", "address", "idCard", "code", "ids"])
def test_non_id_like_keys(key):
    assert not probe._looks_like_id_key(key)


def test_extract_ids_from_flat_dict():
    assert probe._extract_created_ids({"areaId": 17}) == {"areaId": 17}


def test_extract_ids_does_not_recurse():
    """深一层的 id 多半是关联数据 —— 认进来清单就不值得信了。"""
    data = {"areaId": 17, "parent": {"id": 3}, "children": [{"id": 9}]}
    assert probe._extract_created_ids(data) == {"areaId": 17}


@pytest.mark.parametrize("value", [0, "0", "", "  ", True, False, None, [], {}])
def test_zero_blank_and_bool_are_not_ids(value):
    assert probe._extract_created_ids({"id": value}) == {}


def test_scalar_data_is_treated_as_new_id():
    assert probe._extract_created_ids(42) == {"id": 42}
    assert probe._extract_created_ids("42") == {"id": "42"}
    assert probe._extract_created_ids(0) == {}
    assert probe._extract_created_ids(True) == {}


# ---------------- 删除接口匹配 ----------------


def _ep(method: str, path: str, **kwargs) -> Endpoint:
    return Endpoint(id=f"{method}:{path}", method=method, path=path, **kwargs)


def test_suggests_single_segment_delete():
    endpoints = [
        _ep("POST", "/admin/area"),
        _ep("DELETE", "/admin/area/{id}", path_params=[Param(name="id", location="path")]),
    ]
    assert (
        probe.suggest_delete_endpoint(endpoints[0], endpoints)
        == "DELETE /admin/area/{id}"
    )


def test_does_not_suggest_nested_delete():
    """``/area/{id}/status`` 删的是状态不是记录，给错建议比不给更危险。"""
    endpoints = [
        _ep("POST", "/admin/area"),
        _ep("DELETE", "/admin/area/{id}/status"),
    ]
    assert probe.suggest_delete_endpoint(endpoints[0], endpoints) == ""


def test_does_not_suggest_sibling_path():
    """``/area/export`` 是同前缀的另一条路径，不是「删掉 area」的接口。"""
    endpoints = [
        _ep("POST", "/admin/area"),
        _ep("DELETE", "/admin/area/export"),
    ]
    assert probe.suggest_delete_endpoint(endpoints[0], endpoints) == ""


def test_no_delete_endpoint_returns_blank():
    assert probe.suggest_delete_endpoint(_ep("POST", "/admin/area"), []) == ""


# ---------------- 只有成功的写探针才留足迹 ----------------


class _Ctx:
    contract = {"success_code": "00000"}

    def db_mappings(self, ep):
        return {}

    def examples_for(self, ep, limit=1):
        return []

    def path_values_for(self, ep):
        return {}


class _Client:
    env = "test"

    #: 匿名请求的真实行为 —— 未登录写接口会被拒。不模拟的话 C1（未登录）
    #: 测点也会「成功」，足迹里多出一条根本没真造出来的数据。
    UNAUTHORIZED = {"success": False, "code": "A00004", "message": "token 失效"}

    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self.body = body if body is not None else {"success": True, "data": {"id": 7}}
        self.calls = 0

    def send(self, method, path, params=None, data=None, auth=True):
        self.calls += 1
        if not auth:
            return {"status_code": 200, "body": dict(self.UNAUTHORIZED), "error": ""}
        return {"status_code": self.status_code, "body": self.body, "error": ""}


def test_successful_write_probe_leaves_one_footprint():
    ep = _ep("POST", "/admin/area", body={"areaName": "x"})
    outcomes = probe.probe_endpoint(
        ep, _Client(), _Ctx(), max_intents=1, include_write=True
    )

    created = [c for o in outcomes for c in o.created]
    assert len(created) == 1
    assert created[0]["ids"] == {"id": 7}
    assert created[0]["请求"]["data"] == {"areaName": "x"}
    assert outcomes[0].to_dict()["造的数据"]


def test_rejected_write_probe_leaves_no_footprint():
    ep = _ep("POST", "/admin/area", body={"areaName": "x"})
    client = _Client(body={"success": False, "code": "A00001", "message": "参数错"})
    outcomes = probe.probe_endpoint(
        ep, client, _Ctx(), max_intents=1, include_write=True
    )

    assert all(not o.created for o in outcomes)
    assert "造的数据" not in outcomes[0].to_dict()


def test_put_probe_records_modification_not_creation():
    """PUT/PATCH 改的是已有记录 —— 记成「造的数据」再 DELETE 会删掉原有数据。"""
    ep = _ep("PUT", "/admin/area", body={"areaId": 1, "areaName": "x"})
    outcomes = probe.probe_endpoint(
        ep, _Client(), _Ctx(), max_intents=1, include_write=True
    )

    assert all(not o.created for o in outcomes)
    modified = [m for o in outcomes for m in o.modified]
    assert len(modified) == 1
    assert modified[0]["请求"]["data"]["areaId"] == 1

    data = outcomes[0].to_dict()
    assert "改动的数据" in data
    assert "造的数据" not in data, "更新探针不能留下会被 DELETE 的「新造」记录"


def test_post_probe_does_not_record_modification():
    ep = _ep("POST", "/admin/area", body={"areaName": "x"})
    outcomes = probe.probe_endpoint(
        ep, _Client(), _Ctx(), max_intents=1, include_write=True
    )

    assert all(not o.modified for o in outcomes)


def test_read_probe_never_leaves_footprint():
    """只读探针不造数据 —— 响应里有 id 也不能记成「这次新建的」。"""
    ep = _ep("GET", "/admin/area")
    outcomes = probe.probe_endpoint(ep, _Client(), _Ctx())

    assert outcomes
    assert all(not o.created for o in outcomes)


def test_footprint_without_ids_still_keeps_request():
    """认不出 id 也要留请求体 —— 否则这条脏数据彻底没线索了。"""
    ep = _ep("POST", "/admin/area", body={"areaName": "x"})
    outcomes = probe.probe_endpoint(
        ep,
        _Client(body={"success": True, "data": None}),
        _Ctx(),
        max_intents=1,
        include_write=True,
    )

    created = [c for o in outcomes for c in o.created]
    assert len(created) == 1
    assert created[0]["ids"] == {}
    assert created[0]["请求"]["data"] == {"areaName": "x"}


# ---------------- 摊平成清单 ----------------


def test_collect_footprints_flattens_and_attaches_cleanup():
    made = probe.ProbeOutcome(
        dimension="P0",
        label="正例",
        method="POST",
        path="/admin/area",
        created=[{"测点": "正例", "维度": "P0", "请求": {}, "ids": {"id": 1}}],
        delete_endpoint="DELETE /admin/area/{id}",
    )
    untouched = probe.ProbeOutcome(dimension="P0", label="正例", method="GET", path="/x")

    items = probe.collect_footprints({"POST /admin/area": [made], "GET /x": [untouched]})

    assert len(items) == 1, "没造数据的探针不该进清单"
    assert items[0]["接口"] == "POST /admin/area"
    assert items[0]["清理接口"] == "DELETE /admin/area/{id}"


def test_collect_modifications_is_not_cleanable():
    """改动的数据只进人工还原清单，不进 DELETE 计划。"""
    made = probe.ProbeOutcome(
        dimension="P0",
        label="正例",
        method="PUT",
        path="/admin/area/1",
        modified=[{"测点": "正例", "维度": "P0", "请求": {}, "ids": {"id": 1}}],
        delete_endpoint="DELETE /admin/area/{id}",
    )
    result = {"PUT /admin/area/1": [made]}

    assert probe.collect_footprints(result) == [], "改动的数据不能被当成可删数据"
    items = probe.collect_modifications(result)
    assert len(items) == 1
    assert items[0]["接口"] == "PUT /admin/area/1"
    assert "清理接口" not in items[0]


def test_collect_footprints_omits_missing_cleanup_hint():
    """找不到删除接口时不要写一个空值 —— 读的人会以为是「不用删」。"""
    made = probe.ProbeOutcome(
        dimension="P0",
        label="正例",
        method="POST",
        path="/admin/area",
        created=[{"测点": "正例", "维度": "P0", "请求": {}, "ids": {}}],
    )

    items = probe.collect_footprints({"POST /admin/area": [made]})

    assert items and "清理接口" not in items[0]


# ---------------- 落盘（_report_footprints） ----------------
#
# 上面几条只测「摊平成清单」这个纯函数。真正决定脏数据有没有线索的是
# 落盘那一层：文件名对不对、只有改动时写不写、空的时候会不会平白建个文件、
# 写不进去时会不会把警告吞掉。这几条以前一条测试都没有。


def _created_outcome(ids: dict, delete_at: str = "") -> probe.ProbeOutcome:
    return probe.ProbeOutcome(
        dimension="P0",
        label="正例",
        method="POST",
        path="/admin/area",
        created=[{"测点": "正例", "维度": "P0", "请求": {"data": {"a": 1}}, "ids": ids}],
        delete_endpoint=delete_at,
    )


def test_report_writes_next_to_output_with_footprints_suffix(tmp_path, capsys):
    """清单要跟着输出文件名走，否则第二次探针会把它覆盖掉。"""
    out = tmp_path / "probe_area.json"
    probe._report_footprints(
        {"POST /admin/area": [_created_outcome({"id": 3}, "DELETE /admin/area/{id}")]},
        env="test",
        output=str(out),
    )

    target = tmp_path / "probe_area_footprints.json"
    assert target.exists(), "清单路径应基于输出文件名"
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["env"] == "test"
    assert payload["条数"] == 1
    assert payload["items"][0]["清理接口"] == "DELETE /admin/area/{id}"
    assert "不会自动删除" in payload["说明"]

    err = capsys.readouterr().err
    assert "不会自动删除" in err, "造了数据就必须在 stderr 上大声说"


def test_report_writes_when_only_modifications(tmp_path, capsys):
    """只有 PUT/PATCH 改动时也要落盘 —— 否则这批改动彻底没记录。"""
    modified = probe.ProbeOutcome(
        dimension="P0",
        label="正例",
        method="PUT",
        path="/admin/area",
        modified=[{"测点": "正例", "维度": "P0", "请求": {"data": {"areaId": 1}}, "ids": {}}],
    )
    out = tmp_path / "probe_area.json"
    probe._report_footprints({"PUT /admin/area": [modified]}, env="test", output=str(out))

    target = tmp_path / "probe_area_footprints.json"
    assert target.exists()
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["条数"] == 0
    assert payload["items"] == []
    assert len(payload["改动的数据"]) == 1
    assert "不能 DELETE" in payload["说明"]

    err = capsys.readouterr().err
    assert "DELETE 清不了" in err


def test_report_creates_no_file_when_nothing_changed(tmp_path, capsys):
    """没造数据、没改数据 → 不留文件（别让空清单看起来像「已清干净」）。"""
    read_only = probe.ProbeOutcome(dimension="P0", label="正例", method="GET", path="/x")
    out = tmp_path / "probe_area.json"
    probe._report_footprints({"GET /x": [read_only]}, env="test", output=str(out))

    assert not (tmp_path / "probe_area_footprints.json").exists()
    assert "没有新建出数据" in capsys.readouterr().err


def test_report_uses_cache_dir_when_output_is_blank(tmp_path, monkeypatch):
    """不给 --output 时要落到 .cache 下（而不是当前工作目录）。"""
    from utils import paths

    monkeypatch.setattr(paths, "CACHE_DIR", tmp_path)
    probe._report_footprints({"POST /admin/area": [_created_outcome({"id": 5})]}, env="test")

    target = tmp_path / "ai_compare" / "probe_footprints.json"
    assert target.exists()
    assert json.loads(target.read_text(encoding="utf-8"))["条数"] == 1


def test_report_does_not_swallow_write_failure(tmp_path, capsys):
    """清单写不进去时不能一声不吭 —— 至少要逐条打到 stderr。"""
    # 目标路径被一个同名目录占住 → write_text 抛 IsADirectoryError（OSError 子类）
    (tmp_path / "probe_area_footprints.json").mkdir()
    out = tmp_path / "probe_area.json"

    probe._report_footprints(
        {"POST /admin/area": [_created_outcome({"id": 9})]}, env="test", output=str(out)
    )

    err = capsys.readouterr().err
    assert "写入失败" in err
    assert '"id": 9' in err, "写盘失败时必须把数据本身打到 stderr，不然线索就丢了"
