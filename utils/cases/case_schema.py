"""测试用例定义：用例数据长什么样，以及怎么校验。

一条 YAML 用例的结构：

.. code-block:: yaml

    testcase:
      name: user_info              # 必填，用例标识（生成函数名/用例 ID）
      description: 查询当前用户信息
      allure:
        epic: mall4j 管理后台
        feature: 用户管理
        story: 用户信息
      setups:                      # 可选，用例开始前的准备（造数据 / 前置状态）
        - id: init_order
          operation_type: api
          path: /xxx/create
          method: POST
          data: {}
      steps:                       # 必填，按顺序执行的接口步骤
        - id: user_info            # 必填，步骤 ID，后续步骤可用 {{user_info.result.id}} 引用
          description: 查当前用户
          path: /admin/user/info
          method: GET
          headers:
            accesstoken: '{{mall4j.token}}'
            uuid: '{{mall4j.uuid}}'
          assert:
            - type: equal
              field: code
              expected: 200
      teardowns:                   # 可选，用例结束后的清理
        - id: clean_data
          operation_type: api
          path: /xxx/clean
          method: POST
          data: {}

**跳过一条用例**

用例还没写好、或者被测环境暂时不可用时，可以直接跳过：

.. code-block:: yaml

    testcase:
      name: 暂时没做完的用例
      skip: "这个功能还没上线"        # 有值就跳过，理由会显示在报告里

**删除测试数据**

危险删安全闸（``utils/safety_guard.py``）只防「误删真实数据」，不应该
挡住测试清理自己造的数据。因此下面三种情况都会自动放行 DELETE：

* 后置清理（``teardowns``）—— 默认放行；
* 步骤的 URL/参数/请求体引用了**本用例 setup** 产出的数据，
  如 ``/sys/menu/{{查数据.data.records[0].menuId}}``；
* 步骤引用了**本用例前面某个 POST/PUT/PATCH 步骤**产出的数据，
  如 ``/sys/menu/{{新建.data.id}}``。

引用 GET 步骤查到的数据不自动放行 —— 那可能是环境里真实的存量数据。

如果删的是「环境里本来就有的」测试数据（固定 id，没有 setup 引用），
需要显式声明：

.. code-block:: yaml

    testcase:
      allow_dangerous: true          # 也支持写在单个 step 上
      steps:
        - id: delete_test_data
          path: /sys/menu/374
          method: DELETE

Excel 扁平格式对应列：``允许危险删除`` 填 ``true``。
"""
from __future__ import annotations

from typing import Any

REQUIRED_CASE_FIELDS = ("name", "steps")
REQUIRED_STEP_FIELDS = ("id", "path", "method")
# login —— 用例开始前强制真实登录一次，拿一份独立的 token/refreshToken
#          （不走缓存，也不会把这次登录写进缓存）
VALID_SETUP_OPS = ("api", "db", "factory", "login")
VALID_TEARDOWN_OPS = ("api", "db", "factory", "factory_cleanup")

# 特殊步骤：不用发普通 HTTP 请求，自带一套校验规则
# login          —— 走完整的"抓 uuid → 滑块 → 登录"链路
# login_no_slider —— 抓个新 uuid 就直接登录（跳过滑块，验证服务端会拒绝）
# noop           —— 什么也不做，只跑断言
# mock           —— 不发真实请求，直接返回预设响应
SPECIAL_STEP_OPS = ("login", "login_no_slider", "noop", "mock")


class TestCaseError(ValueError):
    """用例数据不合法。"""


def validate_test_data(test_data: Any) -> bool:
    """校验整份用例文件（含 testcase 根键），不合法直接抛 ``TestCaseError``。"""
    if not test_data:
        raise TestCaseError("用例文件内容为空")

    if "testcase" not in test_data:
        raise TestCaseError("用例文件必须以 testcase: 开头")

    case = test_data["testcase"]
    if not isinstance(case, dict):
        raise TestCaseError("testcase 必须是字典")

    for field in REQUIRED_CASE_FIELDS:
        if not case.get(field):
            raise TestCaseError(f"testcase 缺少必需字段 {field!r}")

    steps = case["steps"]
    if not isinstance(steps, list) or not steps:
        raise TestCaseError("testcase.steps 必须是非空列表")

    seen_ids: set[str] = set()
    for step in steps:
        if not isinstance(step, dict):
            raise TestCaseError("每个 step 必须是字典")
        step_id = str(step.get("id") or "")
        # 特殊步骤有自己的一套字段要求
        op = step.get("operation_type")
        if op in SPECIAL_STEP_OPS:
            if not step_id:
                raise TestCaseError(f"step 缺少必需字段 'id': {op}")
            if op == "login" and not step.get("username"):
                raise TestCaseError(
                    f"step {step_id!r} 是 login 类型，必须提供 username"
                )
            if op == "mock" and not step.get("mock"):
                raise TestCaseError(
                    f"step {step_id!r} 是 mock 类型，必须提供 mock 配置"
                    f"（里面写要返回什么）"
                )
        else:
            # 普通步骤：必须有 id + path + method
            #
            # ⚠️ 但如果写了 ``api``（引用接口定义），path/method 会在
            #    收集阶段被接口定义填上 —— 所以这里放行。
            if step.get("api"):
                if not step_id:
                    raise TestCaseError("step 缺少必需字段 'id'")
            else:
                for field in REQUIRED_STEP_FIELDS:
                    if not step.get(field):
                        raise TestCaseError(
                            f"step 缺少必需字段 {field!r}: {step.get('id')}"
                            f"（也可以写 api: 接口名 来引用接口定义）"
                        )
        if step_id in seen_ids:
            raise TestCaseError(f"step id 重复: {step_id!r}（同一条用例里 id 必须唯一）")
        seen_ids.add(step_id)

    return True


def validate_setups(setups: Any) -> bool:
    """校验前置准备步骤；返回 True 表示有合法的 setups。

    setups 和 teardowns 的结构完全一样 —— 校验逻辑共用一套。
    """
    return _validate_pre_post("setup", setups, VALID_SETUP_OPS)


def validate_teardowns(teardowns: Any) -> bool:
    """校验后置清理步骤；返回 True 表示有合法的 teardowns。"""
    return _validate_pre_post("teardown", teardowns, VALID_TEARDOWN_OPS)


def _validate_pre_post(kind: str, items: Any, valid_ops: tuple[str, ...]) -> bool:
    """校验前置/后置步骤（两者结构一样，共用逻辑）。

    Args:
        kind: ``"setup"`` 或 ``"teardown"``，只影响报错文案
        items: 待校验的列表
        valid_ops: 允许的 operation_type
    """
    if not items:
        return False
    if not isinstance(items, list):
        raise TestCaseError(f"{kind}s 必须是列表")

    for item in items:
        if not isinstance(item, dict):
            raise TestCaseError(f"每个 {kind} 必须是字典")
        if not item.get("id"):
            raise TestCaseError(f"{kind} 缺少 id")
        op = item.get("operation_type")
        if op not in valid_ops:
            raise TestCaseError(
                f"{kind} {item['id']} 的 operation_type 必须是 {valid_ops} 之一"
            )
        if op == "api":
            for field in ("path", "method"):
                if not item.get(field):
                    raise TestCaseError(f"api 类型的 {kind} 缺少 {field!r}: {item['id']}")
        if op == "db" and not item.get("query"):
            raise TestCaseError(f"db 类型的 {kind} 缺少 query: {item['id']}")
        if op in ("factory", "factory_cleanup") and not item.get("factory"):
            raise TestCaseError(
                f"{op} 类型的 {kind} 缺少 factory（要写工厂名，比如 mall4j.order）: {item['id']}"
            )

    return True
