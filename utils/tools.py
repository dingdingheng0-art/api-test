"""自定义工具函数：用例里可以用 ``{{tools.函数名()}}`` 调。

想加自己的函数，直接在这个文件里写就行，用例里直接写
``{{tools.my_func(a=1)}}``。

**业务流用例常用的一组"数据处理"函数**（在文件末尾）：

- :func:`sum_field`     —— 把列表里某个字段加起来（算 UV / PV / 金额）
- :func:`count_field`   —— 数一���列表有多长
- :func:`find_in_tree`  —— 在多层树里按字段值找到那一项（菜单查找）
- :func:`pick`          —— 从列表里挑出满足条件的项
- :func:`pluck`         —— 从列表里把某个字段抽成一维数组
- :func:`distinct`      —— 列表去重
- :func:`compare`       —— 比较两个值，给出可读的差异说明

这些函数让**跨接口的数据一致性**可以写成断言，而不用写 Python 脚本。
"""
from __future__ import annotations

import random
import string
import time
from typing import Any


def gen_order_no(prefix: str = "SO", length: int = 12) -> str:
    """生成一个订单号：前缀 + 时间戳 + 随机串。"""
    rand = "".join(random.choices(string.digits, k=length))
    return f"{prefix}{int(time.time())}{rand}"


def gen_mobile() -> str:
    """生成一个手机号（测试用假号）。"""
    return "1" + random.choice("35789") + "".join(random.choices(string.digits, k=9))


def gen_id() -> str:
    """生成一个 32 位 UUID（去掉横线）。"""
    import uuid as _uuid

    return _uuid.uuid4().hex


def sleep(seconds: float = 1.0) -> None:
    """等待若干秒（用例里偶尔需要让服务端异步任务完成）。"""
    time.sleep(seconds)


def today() -> str:
    """今天的日期，格式 YYYY-MM-DD。"""
    from datetime import date

    return date.today().isoformat()


def timestamp() -> str:
    """当前时间戳（13 位）—— 用来造**唯一**的测试数据。

    很多线上接口要求名字全局唯一（比如品牌名重复会直接报错），
    所以建数据时在名字后面拼一个时间戳：

    .. code-block:: yaml

        data:
          name: "自动化品牌{{tools.timestamp()}}"
    """
    return str(int(time.time() * 1000))


# --------------------------------------------------------------------------
# 固定值：一个用例里多次引用同一个动态值
# --------------------------------------------------------------------------
#
# 为什么要这个：
#
# ``{{tools.timestamp()}}`` **每次求值都返回不同的值**。
# 前置造数据用的标题和后置查询用的标题不是同一个字符串，
# 于是「造完再查」必然查不到 —— 这类用例永远跑不通。
#
# 固定值机制解决这个问题：第一次调用 ``fixed()`` 时生成一个值并记下来，
# 后面再引用同一个名字，拿到的还是那个值。
#
# 用法（先取名，再用名）：
#
# .. code-block:: yaml
#
#     setups:
#       - id: 建数据
#         data:
#           title: "{{tools.fixed('热搜标题', '自动化热搜')}}"
#     teardowns:
#       - id: 查数据
#         params:
#           title: "{{tools.fixed('热搜标题')}}"
#
# 名字是关键字 —— 同一个用例里用同一个名字，就是同一个值。
# 不同用例之间互不影响（每条用例用完就清）。
_FIXED_VALUES: dict[str, Any] = {}


def fixed(name: str, prefix: str = "", suffix: str = "") -> Any:
    """取一个**固定**的动态值 —— 同一个名字永远返回同一个值。

    第一次调用时按 ``prefix + 时间戳 + suffix`` 生成并记下来；
    之后用同一个名字调用，直接返回之前记下的那个值。

    Args:
        name: 名字。同一个用例里用同一个名字 = 同一个值。
        prefix: 值的前缀（第一次生成时才用）
        suffix: 值的后缀（第一次生成时才用）

    Returns:
        第一次生成的值；之后返回同一个值。

    用途：造数据用动态名字（避免重名），但查询/删除时要用**同一个**名字
    —— ``timestamp()`` 做不到这一点，因为它每次都返回新值。
    """
    if name not in _FIXED_VALUES:
        _FIXED_VALUES[name] = f"{prefix}{timestamp()}{suffix}"
    return _FIXED_VALUES[name]


def reset_fixed() -> None:
    """清空固定值（每条用例开始前调用，避免上一条的值串过来）。"""
    _FIXED_VALUES.clear()


def safe_id(name: str = "default") -> int:
    """生成一个稳定、唯一、落在安全白名单区间内的测试数据 id。

    和 :func:`fixed` 一样，同一个 ``name`` 在同一个用例里多次调用返回同一个值，
    所以造数据、删数据可以写成同一个 ``{{tools.safe_id('xxx')}}``。

    返回值落在 ``[900000000000, 999999999999]`` —— 这正是 config.yaml 里
    ``safety.dangerous_write.safe_id_ranges`` 放行的「自己造的测试数据」区间。
    于是「真删」用例删的是自己刚造的那条，安全闸会放行，又不会误删生产数据。
    """
    key = f"__safe_id__:{name}"
    if key not in _FIXED_VALUES:
        _FIXED_VALUES[key] = 900000000000 + (int(time.time() * 1000) % 100000000000)
    return _FIXED_VALUES[key]


def short_stamp() -> str:
    """4 位短数字 —— 给**有长度限制**的字段用。

    比如计量单位的名称最长 5 个字符，拼个 13 位时间戳就超长了。
    """
    return str(int(time.time()) % 10000)


def env_value(name: str, default: Any = None) -> Any:
    """读环境变量。"""
    import os

    return os.environ.get(name, default)


def md5(text: str) -> str:
    """算 MD5（小写 32 位 hex）。

    登录接口要传 MD5 后的密码，用例里写 ``{{tools.md5(password)}}``。
    """
    import hashlib

    return hashlib.md5(text.encode("utf-8")).hexdigest()


def mall4j_password(password: str) -> str:
    """把明文密码加密成 mall4j 后台接口要的格式。

    新增 / 修改管理员账号时，``password`` 字段服务端会先做 AES 解密 ——
    直接传明文会返回 ``A00001 AES解密错误``。用例里这样写：

    .. code-block:: yaml

        password: '{{tools.mall4j_password("123456")}}'

    加密逻辑和登录适配器共用（``timestamp + 明文`` 用固定密钥 AES-ECB），
    所以这里直接复用 :func:`adapters.mall4j.encrypt_password`。
    """
    from adapters.mall4j import encrypt_password

    return encrypt_password(password)


def config_value(path: str, default: Any = None) -> Any:
    """按 ``a.b.c`` 的路径读配置值。

    用例里写 ``{{tools.config_value('login.username')}}`` 拿配置里的账号密码。
    """
    from utils.config.config_manager import ConfigManager

    value: Any = ConfigManager.get_all()
    for key in path.split("."):
        if isinstance(value, dict) and key in value:
            value = value[key]
        else:
            return default
    return value


def login_credential(project: str, field: str = "username") -> Any:
    """取某个项目在当前环境下的登录账号或密码。

    用例里写 ``{{tools.login_credential('mall4j', 'username')}}``。
    登录用例自己要用，但框架的自动登录还没跑，所以不能从 Globals 拿，
    这里直接从 config.yaml 里读。
    """
    from utils.config.config_manager import ConfigManager

    env = ConfigManager.get_env()
    projects = ConfigManager.get_all().get("projects") or {}
    env_config = (projects.get(project) or {}).get(env) or {}
    login = env_config.get("login") or {}
    data = login.get("data") or {}
    return data.get(field)


def login_field(project: str, field: str = "username", override_field: str = "", override: str = "") -> Any:
    """取登录字段，但允许用参数覆盖掉其中一个。

    用来写"反向登录"用例 —— 同一套步骤，只把某个字段换掉：

    .. code-block:: yaml

        parameters:
          - name: 密码错误
            data:
              override_field: password
              override: 'wrong_password_12345'

        data:
          username: '{{tools.login_field("mall4j", "username", override_field, override)}}'
          password: '{{tools.md5(tools.login_field("mall4j", "password", override_field, override))}}'

    Args:
        project: 项目名
        field: 要取哪个字段（username / password）
        override_field: 要被覆盖的字段名，空表示不覆盖
        override: 覆盖成什么值
    """
    if override_field and override_field == field:
        return override
    return login_credential(project, field)


def fresh_uuid(project: str = "") -> str:
    """抓一个全新的 uuid（滑块会话标识）。

    uuid 是一次性凭证，用过就失效。登录用例要自己走一遍滑块时，
    必须用全新的 uuid —— 用例里写 ``{{tools.fresh_uuid()}}``。

    不传 project 时用配置里的默认项目（``default_project``）。
    """
    from utils.config.project_profile import ProjectRegistry
    from utils.slider.uuid_util import fetch_uuid

    project = project or ProjectRegistry.default_project()
    uuid = fetch_uuid(profile=ProjectRegistry.get(project))
    if not uuid:
        raise RuntimeError("没抓到新 uuid（服务端限流？等 30 秒再试）")
    return uuid


def slider_xpos(uuid: str, project: str = "") -> int:
    """用给定的 uuid 跑一遍滑块，返回缺口位置。

    登录用例想完整走一遍"滑块 → 登录"链路时用这个：

    .. code-block:: yaml

        data:
          uuid: '{{tools.fresh_uuid()}}'
          xpos: '{{tools.slider_xpos(uuid)}}'
    """
    from utils.config.project_profile import ProjectRegistry
    from utils.http.request_util import make_session, slider_init, slider_verify
    from utils.slider.slider_solver import parse_init_response, solve_slider_x

    project = project or ProjectRegistry.default_project()
    profile = ProjectRegistry.get(project)

    session = make_session(uuid, profile=profile)
    try:
        init_raw = slider_init(session, profile=profile)
        init = parse_init_response(init_raw)
        x = solve_slider_x(init)
        # 校验一下（服务端会消费这个 uuid）
        resp = slider_verify(session, x, profile=profile)
        if not (resp.get("success") is True and resp.get("result")):
            raise RuntimeError(f"滑块校验失败: {resp}")
        return x
    finally:
        session.close()


def login_without_slider(
    username: str | None = None,
    password: str | None = None,
    project: str = "",
) -> dict[str, Any]:
    """**跳过滑块**直接调登录接口，返回登录接口的原始响应。

    用途：验证"服务端会拒绝没过滑块的登录请求"。

    实现：抓一个全新 uuid（从未做过滑块），直接拿它调登录接口。

    .. code-block:: yaml

        steps:
          - id: login
            operation_type: login_no_slider
            username: '{{tools.login_credential("<项目名>", "username")}}'
            password: '{{tools.login_credential("<项目名>", "password")}}'

    断言应该是"拿不到 token"：

    .. code-block:: yaml

        assert:
          - type: is None
            field: result.accessToken
          - type: not equal
            field: success
            expected: true
    """
    from utils.config.project_profile import ProjectRegistry
    from utils.http.request_util import make_session, user_login
    from utils.slider.uuid_util import fetch_uuid

    project = project or ProjectRegistry.default_project()

    # 注意：只认 None，不认空字符串 ——
    # 空密码是一个**合法的测试场景**（"空密码应该登录失败"），
    # 如果用 `if not password` 判断，空串会被当成"没传"然后回退到配置里的真密码。
    if username is None:
        username = str(login_credential(project, "username") or "")
    if password is None:
        password = str(login_credential(project, "password") or "")

    profile = ProjectRegistry.get(project)

    uuid = fetch_uuid(profile=profile)
    if not uuid:
        raise RuntimeError("没抓到新 uuid（服务端限流？等 30 秒再试）")

    session = make_session(uuid, profile=profile)
    try:
        # 注意：这里**不做** slider_init / slider_verify
        return user_login(
            session, username=username, password=password, profile=profile
        )
    finally:
        session.close()


def jwt_payload(token: str) -> dict[str, Any]:
    """解 JWT 的 payload 段（不解签名，只看内容）。

    用途：断言 token 里的用户信息对不对，比如 ``sub == username``。

    .. code-block:: yaml

        assert:
          - type: equal
            field: result.accessToken
            expected: 'eyJ...'      # 不能直接比整串
    """
    import base64
    import json

    if not token or "." not in token:
        raise ValueError(f"不是 JWT（没有 '.' 分段）：{token!r}")

    payload_part = token.split(".")[1]
    # base64 url-safe 的长度要补齐到 4 的倍数
    payload_part += "=" * (4 - len(payload_part) % 4)
    return json.loads(base64.urlsafe_b64decode(payload_part))


def login_response(
    username: str | None = None,
    password: str | None = None,
    retries: int = 3,
    project: str = "",
) -> dict[str, Any]:
    """走完整的"抓 uuid → 滑块 → 登录"链路，返回登录接口的原始响应。

    登录用例想直接验证登录接口时用这个：

    .. code-block:: yaml

        steps:
          - id: login
            operation_type: login
            username: '{{...}}'
            password: '{{...}}'

    **滑块识别有噪声**（图像复杂时会偏），所以这里内置重试：
    每失败一次就换一个新 uuid 重新识别，最多试 ``retries`` 次。

    Args:
        username: 用户名，空则取配置里的
        password: 明文密码，空则取配置里的（内部会做 MD5）
        retries: 滑块失败时的重试次数
        project: 项目名（决定用哪套地址 / 路径）
    """
    import time as _time

    from utils.config.project_profile import ProjectRegistry
    from utils.http.request_util import make_session, slider_init, slider_verify, user_login
    from utils.slider.slider_solver import parse_init_response, solve_slider_x
    from utils.slider.uuid_util import fetch_uuid

    project = project or ProjectRegistry.default_project()
    profile = ProjectRegistry.get(project)

    # 注意：只认 None，不认空字符串 ——
    # 空密码是一个**合法的测试场景**（"空密码应该登录失败"），
    # 如果用 `if not password` 判断，空串会被当成"没传"然后回退到配置里的真密码。
    if username is None:
        username = str(login_credential(project, "username") or "")
    if password is None:
        password = str(login_credential(project, "password") or "")

    last_error = ""

    for attempt in range(1, retries + 1):
        session = None
        try:
            uuid = fetch_uuid(profile=profile)
            if not uuid:
                raise RuntimeError("没抓到新 uuid（服务端限流？等 30 秒再试）")

            session = make_session(uuid, profile=profile)
            init_raw = slider_init(session, profile=profile)
            init = parse_init_response(init_raw)
            x = solve_slider_x(init)

            resp = slider_verify(session, x, profile=profile)
            if not (resp.get("success") is True and resp.get("result")):
                raise RuntimeError(f"滑块校验失败: {resp}")

            # 登录（密码走 MD5，user_login 内部已经处理）
            return user_login(
                session, username=username, password=password, profile=profile
            )

        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            if attempt < retries:
                # 滑块识别偏了是常见情况，换个新 uuid 再试
                import logging

                logging.getLogger(__name__).warning(
                    "滑块识别失败（第 %s/%s 次）：%s，换新 uuid 重试",
                    attempt, retries, last_error,
                )
                _time.sleep(2.0)
        finally:
            if session is not None:
                session.close()

    raise RuntimeError(f"滑块识别连续 {retries} 次失败：{last_error}")


# ============================================================================
# 数据处理工具 —— 给"业务流用例"用
#
# 场景：概览接口说 lastSevenUV=261，访问量明细接口返回 7 条记录，
# 怎么在 YAML 里断言"这两边对得上"？
#
#   steps:
#     - id: index
#       path: /admin/statistics/index
#       ...
#     - id: view_list
#       path: /admin/statistics/view/list
#       params: {searchType: LAST_SEVEN}
#       ...
#     - id: check
#       operation_type: noop
#       assert:
#         - type: equal
#           field: value
#           expected: '{{tools.sum_field(view_list.result, "uvNum")}}'
# ============================================================================


def _to_list(data: Any) -> list[Any]:
    """把各种形态的数据统一成列表。

    - 列表 → 原样
    - 字典 → 当成单元素列表
    - None → 空列表
    """
    if data is None:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return [data]
    return [data]


def _get_field(item: Any, field: str) -> Any:
    """取一项里的某个字段；字段可以是 ``a.b`` 这样的多层路径。"""
    if not isinstance(item, dict):
        return None
    value: Any = item
    for key in field.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(key)
        if value is None:
            return None
    return value


def sum_field(data: Any, field: str, default: Any = 0) -> Any:
    """把列表里某个字段的值全部加起来。

    用例里写 ``{{tools.sum_field(view_list.result, "uvNum")}}``。

    字段缺失或不是数字的按 0 算（这样"某天没数据"不会让断言炸掉）。
    """
    total: Any = default
    for item in _to_list(data):
        value = _get_field(item, field)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        total = total + value
    return total


def count_field(data: Any, field: str | None = None, default: Any = 0) -> Any:
    """数列表有多长；给了 ``field`` 就只数该字段有值的项。"""
    items = _to_list(data)
    if field is None:
        return len(items)
    return sum(1 for item in items if _get_field(item, field) is not None)


def max_id(data: Any, field: str = "id", default: Any = None) -> Any:
    """取列表里 ``field`` 字段**最大**的那一项的 id。

    建数之后想找到"刚建的那条记录"，不能直接拿 ``records[0]`` ——
    服务端分页默认按创建顺序排，第一条往往是**种子数据**，
    拿它去改，等于把系统自带的数据改坏了。

    新建的记录 id 一定是最大的（自增），所以取最大值就是刚建的那条：

    .. code-block:: yaml

        data:
          prodId: '{{tools.max_id(查数据.data.records, "prodId")}}'

    Args:
        data: 列表（也可以是带 ``records`` 的分页对象）
        field: id 字段名，默认 ``id``
        default: 一条都没有时返回什么
    """
    # 传进来的是分页对象的话，取里面的 records
    if isinstance(data, dict) and isinstance(data.get("records"), list):
        data = data["records"]

    best: Any = default
    for item in _to_list(data):
        value = _get_field(item, field)
        if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if best is default or value > best:
            best = value
    return best


def find_id_by_field(
    data: Any,
    field: str,
    value: Any,
    id_field: str = "id",
    default: Any = None,
) -> Any:
    """按**业务字段的值**找到对应记录的 id（而不是取最大的 id）。

    为什么不用 ``max_id``：``max_id`` 取的是列表里 id 最大的那条。
    并发跑的时候，别人（或另一条用例）可能刚造了更新的数据，
    ``max_id`` 就会取到**别人的记录** —— 拿它去改、去删，
    等于误伤别人的数据。

    按业务字段（比如 title）精确匹配就没这个问题：

    .. code-block:: yaml

        data:
          - '{{tools.find_id_by_field(查数据.data.records, "hotSearchId", "自动化热搜固定标题")}}'

    Args:
        data: 列表（也可以是带 ``records`` 的分页对象）
        field: 用来匹配的字段名（比如 ``title``）
        value: 要匹配的值
        id_field: 找到后返回哪个字段（默认 ``id``）
        default: 没找到时的返回值
    """
    target = str(value)
    for item in _to_list(data):
        current = _get_field(item, field)
        if current is not None and str(current) == target:
            return item.get(id_field, default)
    return default


def find_id_by_fixed(
    data: Any,
    field: str,
    fixed_name: str,
    id_field: str = "id",
    prefix: str = "",
    default: Any = None,
) -> Any:
    """按**固定值**匹配到的记录，取出它的 id。

    和 :func:`find_id_by_field` 的区别：匹配用的值不是直接写出来，
    而是给一个「固定值的名字」—— 名字对应的值由 :func:`fixed` 统一生成，
    造数据和查数据用的是同一个值。

    为什么要这么绕：变量解析器**不支持嵌套函数调用**
    （``{{tools.a({{tools.b()}})}}`` 解析不了），
    所以没法把 ``fixed()`` 嵌在 ``find_id_by_field()`` 的参数里。
    这个函数把两步合成一步。

    .. code-block:: yaml

        data:
          - '{{tools.find_id_by_fixed(查数据.data.records, "title", "mall4j_admin_hotSearch", "hotSearchId", "自动化热搜")}}'

    Args:
        data: 列表（也可以是带 ``records`` 的分页对象）
        field: 用来匹配的字段名（比如 ``title``）
        fixed_name: 固定值的名字（和造数据时用的同一个）
        id_field: 找到后返回哪个字段（默认 ``id``）
        prefix: 固定值的前缀（要和造数据时一致）
        default: 没找到时的返回值
    """
    target = str(fixed(fixed_name, prefix))
    for item in _to_list(data):
        current = _get_field(item, field)
        if current is not None and str(current) == target:
            return item.get(id_field, default)
    return default


def find_in_tree(
    data: Any,
    field: str,
    value: Any,
    return_field: str = "id",
    default: Any = None,
) -> Any:
    """在多层树（菜单那种）里找到 ``field`` 等于 ``value`` 的那一项，返回它的 ``return_field``。

    菜单接口返回的是三层嵌套的树，YAML 里没法直接定位到"标题叫 XX 的那个菜单"。
    这个函数把整棵树拍平搜索：

    .. code-block:: yaml

        menu_id: '{{tools.find_in_tree(member_menu.result, "title", "会员列表", "id")}}'

    Args:
        data: 树（列表或字典）
        field: 用来匹配的字段名（``title`` / ``name`` / ``id`` …）
        value: 要匹配的值
        return_field: 找到后返回哪个字段，默认返回 ``id``
        default: 没找到时的返回值（默认 None）
    """
    target = str(value)

    def search(nodes: Any) -> dict[str, Any] | None:
        for node in _to_list(nodes):
            if not isinstance(node, dict):
                continue
            current = _get_field(node, field)
            if current is not None and str(current) == target:
                return node
            found = search(node.get("children"))
            if found is not None:
                return found
        return None

    node = search(data)
    if node is None:
        return default
    return node.get(return_field, default)


def pick(data: Any, field: str, value: Any, default: Any = None) -> Any:
    """从列表里挑出 ``field`` 等于 ``value`` 的那一项（返回整个字典）。

    没找到返回 ``default``。
    """
    target = str(value)
    for item in _to_list(data):
        current = _get_field(item, field)
        if current is not None and str(current) == target:
            return item
    return default


def pluck(data: Any, field: str) -> list[Any]:
    """把列表里每个项的某个字段抽成一维数组（滤掉取不到值的项）。"""
    values: list[Any] = []
    for item in _to_list(data):
        value = _get_field(item, field)
        if value is not None:
            values.append(value)
    return values


def distinct(data: Any) -> list[Any]:
    """列表去重，保持原有顺序。"""
    seen: list[Any] = []
    for item in _to_list(data):
        if item not in seen:
            seen.append(item)
    return seen


def compare(left: Any, right: Any) -> dict[str, Any]:
    """比较两个值，返回一份可读的结果字典。

    用例里这样用（跨接口一致性断言）：

    .. code-block:: yaml

        - id: check
          operation_type: noop
          value: '{{tools.compare(index.result.lastSevenUV, tools.sum_field(view_list.result, "uvNum"))}}'
          assert:
            - type: equal
              field: value.matched
              expected: true

    ``matched`` 是 True 表示两边相等。
    """
    matched = left == right
    return {
        "left": left,
        "right": right,
        "matched": matched,
        "diff": (right - left) if isinstance(left, (int, float))
        and isinstance(right, (int, float)) and not isinstance(left, bool) else None,
    }
