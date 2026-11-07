"""「删除测试数据」自动放行规则的测试。

对应逻辑：``testcases/case_runner.py::_step_deletes_setup_data``

安全闸防的是误删真实数据。用例自己 setup 造出来、或前一步刚写进去的
数据，id 是本用例运行期的产物 —— 删它属于测试清理，应当自动放行。
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from testcases.case_runner import _step_deletes_setup_data  # noqa: E402


def _case(setups=None, steps=None):
    return {"setups": setups or [], "steps": steps or []}


def _step(step_id, method, path="", data=None, params=None):
    return {
        "id": step_id,
        "method": method,
        "path": path,
        "data": data,
        "params": params,
    }


# --------------------------------------------------------------------------
# 应该放行：删除的是本用例造的数据
# --------------------------------------------------------------------------


def test_delete_referencing_setup_data_is_allowed():
    """API-034：setup 造菜单，步骤按 max_id 把它删掉。"""
    case = _case(
        setups=[
            {
                "id": "造数据",
                "operation_type": "api",
                "method": "POST",
                "path": "/sys/menu",
            }
        ]
    )
    step = _step(
        "删除",
        "DELETE",
        path='/sys/menu/{{tools.max_id(造数据.data, "menuId")}}',
    )
    assert _step_deletes_setup_data(case, step) is True


def test_delete_referencing_setup_response_is_allowed():
    case = _case(setups=[{"id": "查数据", "operation_type": "api", "method": "GET"}])
    step = _step(
        "删除",
        "DELETE",
        path="/sys/menu/{{查数据.data.records[0].menuId}}",
    )
    assert _step_deletes_setup_data(case, step) is True


def test_delete_referencing_prior_write_step_is_allowed():
    """前一步 POST 新建的数据，下一步删掉 —— id 也是本用例造的。"""
    create = _step("新建", "POST", path="/sys/menu", data={"name": "t"})
    delete = _step("删除", "DELETE", path="/sys/menu/{{新建.data.id}}")
    case = _case(steps=[create, delete])

    assert _step_deletes_setup_data(case, delete) is True


def test_setup_delete_referencing_another_setup_is_allowed():
    """setup 里先清理上次残留：id 来自前面 setup 造的数据。"""
    create = {
        "id": "造数据",
        "operation_type": "api",
        "method": "POST",
        "path": "/sys/menu",
    }
    delete = {
        "id": "清理残留",
        "operation_type": "api",
        "method": "DELETE",
        "path": "/sys/menu/{{造数据.data.id}}",
    }
    case = _case(setups=[create, delete])

    assert _step_deletes_setup_data(case, delete) is True


def test_reference_in_data_or_params_is_allowed():
    case = _case(setups=[{"id": "造数据", "operation_type": "api", "method": "POST"}])
    by_data = _step("删除", "DELETE", path="/system/x", data={"id": "{{造数据.data.id}}"})
    by_params = _step("删除", "DELETE", path="/system/x", params={"id": "{{造数据.data.id}}"})

    assert _step_deletes_setup_data(case, by_data) is True
    assert _step_deletes_setup_data(case, by_params) is True


# --------------------------------------------------------------------------
# 不应放行
# --------------------------------------------------------------------------


def test_non_delete_step_is_not_affected():
    case = _case(setups=[{"id": "造数据", "operation_type": "api", "method": "POST"}])
    step = _step("查询", "GET", path="/sys/menu/{{造数据.data.id}}")

    assert _step_deletes_setup_data(case, step) is False


def test_delete_with_hardcoded_id_is_not_auto_allowed():
    """写死的 id 可能是真实数据，仍然要走安全闸（或用显式开关）。"""
    case = _case(setups=[{"id": "造数据", "operation_type": "api", "method": "POST"}])
    step = _step("删除", "DELETE", path="/sys/menu/374")

    assert _step_deletes_setup_data(case, step) is False


def test_delete_referencing_prior_read_step_is_not_auto_allowed():
    """GET 查到的数据可能是环境里真实存在的，不能因为"是前一步"就放行。"""
    query = _step("查数据", "GET", path="/sys/menu/page")
    delete = _step("删除", "DELETE", path="/sys/menu/{{查数据.data.records[0].menuId}}")
    case = _case(steps=[query, delete])

    assert _step_deletes_setup_data(case, delete) is False


def test_reference_after_the_delete_step_does_not_count():
    """引用的是后面的步骤 —— 说明这个 id 不是删除时已知的造数。"""
    create = _step("新建", "POST", path="/sys/menu", data={"name": "t"})
    delete = _step("删除", "DELETE", path="/sys/menu/{{新建.data.id}}")
    case = _case(steps=[delete, create])

    assert _step_deletes_setup_data(case, delete) is False


def test_similar_source_name_is_not_a_false_match():
    """setup id 叫 ``a``，不能因为路径里出现 ``{{abc.a}}`` 就放行。"""
    case = _case(setups=[{"id": "a", "operation_type": "api", "method": "POST"}])
    step = _step("删除", "DELETE", path="/sys/menu/{{abc.a}}")

    assert _step_deletes_setup_data(case, step) is False


def test_unresolved_template_without_known_source_is_not_allowed():
    case = _case(setups=[{"id": "造数据", "operation_type": "api", "method": "POST"}])
    step = _step("删除", "DELETE", path="/sys/menu/{{其它.data.id}}")

    assert _step_deletes_setup_data(case, step) is False
