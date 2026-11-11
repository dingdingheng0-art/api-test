"""多系统兼容：项目档案层的单元测试。

**不连任何真实系统** —— 直接构造配置片段，测"读档案"这套逻辑本身：

- 配置怎么变成档案（字段映射、默认值兜底）
- 档案怎么拼鉴权头（不同系统头名不一样）
- 登录方式怎么分发（滑块 / 账号密码 / 自定义）
- 配置写错时会不会早点报错
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.config.project_profile import (
    LOGIN_TYPE_CUSTOM,
    LOGIN_TYPE_PASSWORD,
    LOGIN_TYPE_SLIDER,
    ProjectProfile,
    ProjectRegistry,
    build_profile,
)


def _configured(project: str, env: str) -> dict:
    """直接读 config.yaml 里某个项目/环境的原始配置。

    这几条用例检查的是「档案字段来自配置」，所以期望值不能反过来问
    ``ProjectRegistry`` 要（那是循环论证）。直接解析配置文件、独立展开
    ``${环境变量}`` 占位符，被测代码怎么改都绕不过去。
    """
    from utils import paths
    from utils.config.config_util import expand_env

    raw = yaml.safe_load(paths.CONFIG_FILE.read_text(encoding="utf-8"))
    return expand_env(raw)["projects"][project][env]


def _raw(key: str) -> object:
    """读 config.yaml 顶层字段（origin / referer 这类全局值）。"""
    from utils import paths
    from utils.config.config_util import expand_env

    return expand_env(yaml.safe_load(paths.CONFIG_FILE.read_text(encoding="utf-8")))[key]


# ---------------------------------------------------------------------------
# 配置 → 档案
# ---------------------------------------------------------------------------


class TestBuildProfile:
    def test_minimal_config_uses_defaults(self) -> None:
        """只写 host 也能出档案（老配置兼容）。"""
        profile = build_profile("demo", "online", {"host": "https://api.demo.com"})

        assert profile.name == "demo"
        assert profile.env == "online"
        assert profile.host == "https://api.demo.com"
        # 没配登录方式 → 默认滑块（和以前一样）
        assert profile.login_type == LOGIN_TYPE_SLIDER
        assert profile.needs_slider is True
        # 默认鉴权头是 mall4j 那套（老项目不配也能跑）
        assert profile.token_header == "accesstoken"
        assert profile.session_header == "uuid"

    def test_trailing_slash_stripped_from_host(self) -> None:
        profile = build_profile("demo", "online", {"host": "https://api.demo.com/"})
        assert profile.host == "https://api.demo.com"

    def test_explicit_login_type_wins(self) -> None:
        profile = build_profile(
            "demo",
            "online",
            {"host": "h", "login": {"type": "password"}},
        )
        assert profile.login_type == LOGIN_TYPE_PASSWORD
        assert profile.needs_slider is False

    def test_custom_method_implies_custom_login(self) -> None:
        """没写 type 但写了 custom_method → 当成自定义登录。"""
        profile = build_profile(
            "demo",
            "online",
            {"host": "h", "login": {"custom_method": "adapters.demo.login"}},
        )
        assert profile.login_type == LOGIN_TYPE_CUSTOM
        assert profile.custom_method == "adapters.demo.login"

    def test_auth_headers_from_config(self) -> None:
        """鉴权头名按配置来 —— 这是多系统的关键差异点。"""
        profile = build_profile(
            "demo",
            "online",
            {
                "host": "h",
                "auth": {
                    "token_header": "token",
                    "session_header": "sessionId",
                    "token_var": "tk",
                    "session_var": "sid",
                },
            },
        )
        assert profile.token_header == "token"
        assert profile.session_header == "sessionId"
        assert profile.token_var == "tk"
        assert profile.session_var == "sid"

    def test_session_header_can_be_disabled(self) -> None:
        """显式写 null → 不带会话标识头。"""
        profile = build_profile(
            "demo",
            "online",
            {"host": "h", "auth": {"session_header": None}},
        )
        assert profile.session_header is None

    def test_login_field_names_from_config(self) -> None:
        """表单字段名按配置来（不同系统叫法不一样）。"""
        profile = build_profile(
            "demo",
            "online",
            {
                "host": "h",
                "login": {
                    "type": "password",
                    "path": "/api/auth/login",
                    "method": "post",
                    "username_field": "account",
                    "password_field": "pwd",
                    "password_md5": False,
                },
            },
        )
        assert profile.login_path == "/api/auth/login"
        assert profile.login_method == "POST"
        assert profile.username_field == "account"
        assert profile.password_field == "pwd"
        assert profile.password_md5 is False

    def test_credentials_from_login_data(self) -> None:
        profile = build_profile(
            "demo",
            "online",
            {
                "host": "h",
                "login": {
                    "type": "password",
                    "data": {"username": "u1", "password": "p1"},
                },
            },
        )
        assert profile.username == "u1"
        assert profile.password == "p1"

    def test_slider_paths_from_config(self) -> None:
        profile = build_profile(
            "demo",
            "online",
            {
                "host": "h",
                "login": {"type": "slider"},
                "slider": {
                    "init_path": "/api/captcha",
                    "verify_path": "/api/captcha/check",
                    "verify_query": {"type": "LOGIN"},
                },
            },
        )
        assert profile.slider_init_path == "/api/captcha"
        assert profile.slider_verify_path == "/api/captcha/check"
        assert profile.slider_verify_query == {"type": "LOGIN"}

    def test_extra_headers_captured(self) -> None:
        profile = build_profile(
            "demo",
            "online",
            {"host": "h", "headers": {"X-Tenant": "shop1"}},
        )
        assert profile.headers == {"X-Tenant": "shop1"}

    def test_label_captured(self) -> None:
        profile = build_profile(
            "demo", "online", {"host": "h", "label": "演示系统"}
        )
        assert profile.label == "演示系统"


# ---------------------------------------------------------------------------
# 档案行为
# ---------------------------------------------------------------------------


class TestProfileBehavior:
    def test_auth_headers_uses_project_convention(self) -> None:
        """不同系统的鉴权头不一样，这里要跟着配置走。"""
        profile = ProjectProfile(
            name="demo",
            env="online",
            host="https://api.demo.com",
            token_header="token",
            session_header="sessionId",
        )
        headers = profile.auth_headers("TK", "SID")
        assert headers == {"token": "TK", "sessionId": "SID"}

    def test_auth_headers_skips_empty_values(self) -> None:
        profile = ProjectProfile(
            name="demo", env="online", host="h", token_header="token"
        )
        # 没登录 → 没 token → 不该塞空的 token 头
        assert profile.auth_headers("", "") == {}

    def test_auth_headers_omits_disabled_session(self) -> None:
        profile = ProjectProfile(
            name="demo",
            env="online",
            host="h",
            token_header="token",
            session_header=None,
        )
        headers = profile.auth_headers("TK", "SID")
        assert headers == {"token": "TK"}

    def test_effective_web_url_from_login_page(self) -> None:
        profile = ProjectProfile(
            name="demo", env="online", host="h", login_page="https://x.com/login"
        )
        assert profile.effective_web_url == "https://x.com"

    def test_effective_web_url_empty_when_nothing_configured(self) -> None:
        profile = ProjectProfile(name="demo", env="online", host="h")
        assert profile.effective_web_url == ""

    def test_needs_slider_only_for_slider_type(self) -> None:
        base = {"name": "demo", "env": "online", "host": "h"}
        assert ProjectProfile(**base, login_type=LOGIN_TYPE_SLIDER).needs_slider is True
        assert ProjectProfile(**base, login_type=LOGIN_TYPE_PASSWORD).needs_slider is False
        assert ProjectProfile(**base, login_type=LOGIN_TYPE_CUSTOM).needs_slider is False


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------


class TestProjectRegistry:
    def test_unknown_project_returns_none(self) -> None:
        assert ProjectRegistry.get("no_such_project_xyz", "online") is None

    def test_require_raises_for_unknown_project(self) -> None:
        """配置写错要早点炸，别等跑到一半才发现。"""
        with pytest.raises(ValueError) as exc:
            ProjectRegistry.require("no_such_project_xyz", "online")
        msg = str(exc.value)
        assert "no_such_project_xyz" in msg
        # 报错要说清楚当前能选哪些项目
        assert "mall4j" in msg

    def test_list_projects_includes_configured_ones(self) -> None:
        projects = ProjectRegistry.list_projects()
        assert "mall4j" in projects
        assert "demo_shop" in projects

    def test_get_returns_profile_for_real_project(self) -> None:
        """真配置里的 mall4j —— 确认字段读对了。"""
        profile = ProjectRegistry.get("mall4j", "online")
        assert profile is not None
        assert profile.host == _configured("mall4j", "online")["host"]
        assert profile.login_type == LOGIN_TYPE_CUSTOM
        assert profile.token_header == "Authorization"
        assert profile.session_header is None

    def test_cached_profile_is_reused(self) -> None:
        """连续取两次拿到同一个对象（说明走了缓存）。"""
        first = ProjectRegistry.get("mall4j", "online")
        second = ProjectRegistry.get("mall4j", "online")
        assert first is second

    def test_different_projects_have_different_headers(self) -> None:
        """这是"多系统兼容"的核心断言。"""
        mall4j = ProjectRegistry.get("mall4j", "online")
        demo = ProjectRegistry.get("demo_shop", "online")
        assert mall4j is not None and demo is not None
        assert mall4j.token_header != demo.token_header
        assert mall4j.session_header != demo.session_header

    def test_clear_cache(self) -> None:
        profile = ProjectRegistry.get("mall4j", "online")
        assert profile is not None
        ProjectRegistry.clear_cache()
        again = ProjectRegistry.get("mall4j", "online")
        assert again is not None
        assert again.host == profile.host

    def test_missing_env_falls_back_to_configured_one(self) -> None:
        """项目没配当前环境 → 自动退回它配了的环境。

        新增项目时不用把所有环境都配一遍，
        也不会因为少配一个环境就报"项目不存在"。
        """
        profile = ProjectRegistry.get("demo_shop", "no_such_env_xyz")
        assert profile is not None
        assert profile.env == "online"
        assert profile.host == "https://api.example.invalid"

    def test_explicit_env_wins_over_fallback(self) -> None:
        """明确指定的环境优先，不走兜底。"""
        profile = ProjectRegistry.get("mall4j", "pre")
        assert profile is not None
        assert profile.env == "pre"
        assert profile.host == _configured("mall4j", "pre")["host"]


# ---------------------------------------------------------------------------
# 登录方式分发
# ---------------------------------------------------------------------------


class TestLoginDispatch:
    """登录方式怎么分发 —— 直接看走的是哪条分支。

    这里把 token 缓存"关掉"，一是保证真的走到分发逻辑
    （不然命中缓存就直接返回了，测不到分发），
    二是别往真实缓存文件里写测试用的假 token。
    """

    @pytest.fixture(autouse=True)
    def _no_token_cache(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from utils.infra.token_cache import TokenCache

        monkeypatch.setattr(TokenCache, "get", staticmethod(lambda project: None))
        monkeypatch.setattr(TokenCache, "save", staticmethod(lambda *a, **kw: None))

    def test_password_login_used_for_password_type(self) -> None:
        """password 类型的项目走"直接账号密码"这条分支。"""
        from testcases.auth import Auth

        calls: list[str] = []

        class FakeAuth(Auth):
            def _password_login(self, project_name, login_config, env):  # type: ignore[override]
                calls.append("password")
                return {"token": "T", "uuid": "", "headers": {}}

            def slider_login(self, project_name, login_config, env):  # type: ignore[override]
                calls.append("slider")
                return {"token": "T", "uuid": "", "headers": {}}

        from utils.config.project_profile import ProjectRegistry as _Reg

        profile = _Reg.get("demo_shop", "online")
        assert profile is not None

        auth = FakeAuth()
        result = auth.login("demo_shop", profile.login, "online")
        assert calls == ["password"]
        assert result is not None
        assert result["token"] == "T"

    def test_custom_login_used_for_custom_type(self) -> None:
        """custom 类型的项目走"自己的函数"这条分支。"""
        from testcases.auth import Auth

        calls: list[str] = []

        class FakeAuth(Auth):
            def _call_custom(self, dotted, project_name, login_config, env):  # type: ignore[override]
                calls.append(dotted)
                return {"token": "T", "uuid": "", "headers": {}}

        from utils.config.project_profile import ProjectRegistry as _Reg

        profile = _Reg.get("demo_shop", "ticket")
        assert profile is not None
        assert profile.login_type == LOGIN_TYPE_CUSTOM

        auth = FakeAuth()
        result = auth.login("demo_shop", profile.login, "ticket")
        assert calls == ["adapters.demo.login"]
        assert result is not None

    def test_slider_login_used_for_slider_type(self) -> None:
        """slider 类型的项目走滑块那条分支。"""
        from testcases.auth import Auth

        calls: list[str] = []

        class FakeAuth(Auth):
            def slider_login(self, project_name, login_config, env):  # type: ignore[override]
                calls.append("slider")
                return {"token": "T", "uuid": "", "headers": {}}

        from utils.config.project_profile import ProjectRegistry as _Reg

        profile = _Reg.get("demo_shop", "web")
        assert profile is not None

        auth = FakeAuth()
        auth.login("demo_shop", profile.login, "web")
        assert calls == ["slider"]

    def test_login_and_store_uses_project_variable_names(self) -> None:
        """token 写进全局变量时，用的是项目配置的键名（不是写死 token/uuid）。"""
        from testcases.auth import Auth
        from utils.infra.globals import Globals
        from utils.config.project_profile import ProjectRegistry as _Reg

        profile = _Reg.get("demo_shop", "online")
        assert profile is not None

        class FakeAuth(Auth):
            def _password_login(self, project_name, login_config, env):  # type: ignore[override]
                return {"token": "TK", "uuid": "SID", "headers": {"token": "TK"}}

        auth = FakeAuth()
        token = auth.login_and_store("demo_shop", profile.login, "online")

        assert token == "TK"
        data = Globals.get_data().get("demo_shop", {}) or {}
        # demo_shop 配的就是 token / sessionId 这两个键名
        assert data.get(profile.token_var) == "TK"
        assert data.get(profile.session_var) == "SID"


# ---------------------------------------------------------------------------
# 请求头：不能把上一个系统的信息带给下一个系统
# ---------------------------------------------------------------------------


class TestRequestHeaders:
    """不同系统的请求头不一样 —— 这是多系统兼容最容易踩坑的地方。

    以前这些头是写死的 mall4j 那套，换系统时会带着
    ``admin-b2b2c.mall4j.cn`` 的域名和别人的 uuid 过去。
    """

    def test_browser_system_keeps_browser_headers(self) -> None:
        """有前端的系统 → 浏览器那套头一个都不能少。"""
        from utils.http.request_util import build_default_headers

        profile = ProjectRegistry.get("demo_shop", "web")
        assert profile is not None

        headers = build_default_headers(None, uuid="UUID-1", profile=profile)

        assert headers["Origin"] == "https://admin.example.invalid"
        assert headers["Referer"] == "https://admin.example.invalid/"
        assert headers["uuid"] == "UUID-1"
        assert headers["Authorization"] == "Bearer UUID-1"
        assert headers["Sec-Fetch-Mode"] == "cors"

    def test_token_only_system_has_no_uuid_header(self) -> None:
        """只要 token 的系统（session_header 写 null）→ 不该塞 uuid / Bearer。"""
        from utils.http.request_util import build_default_headers

        profile = ProjectRegistry.get("mall4j", "online")
        assert profile is not None
        assert profile.session_header is None

        headers = build_default_headers(None, uuid="UUID-1", profile=profile)

        # 前端地址还是要有（Origin / Referer）
        assert headers["Origin"] == _raw("origin")
        assert headers["Referer"] == _raw("referer")
        # 但会话标识那套不塞 —— token 本身就是全部凭证
        assert "uuid" not in headers
        assert "Authorization" not in headers

    def test_api_only_system_has_no_browser_headers(self) -> None:
        """纯 API 系统（没配前端）→ 不该收到 uuid / Authorization / Origin。"""
        from utils.http.request_util import build_default_headers

        profile = ProjectRegistry.get("demo_shop", "online")
        assert profile is not None
        assert profile.effective_web_url == ""

        headers = build_default_headers(None, uuid="UUID-1", profile=profile)

        # 不能带上一个系统的域名
        assert "Origin" not in headers
        assert "Referer" not in headers
        assert "mall4j" not in str(headers)
        # 会话标识也不该有 —— 那是滑块系统的东西
        assert "uuid" not in headers
        assert "Authorization" not in headers
        # 但通用的头还在
        assert headers["Accept"] == "application/json, text/plain, */*"

    def test_profile_headers_override_defaults(self) -> None:
        """项目档案里配的 headers 会补上（但不能覆盖已有的）。"""
        from utils.http.request_util import build_default_headers

        profile = build_profile(
            "demo",
            "online",
            {"host": "h", "headers": {"X-Tenant": "shop1", "Accept": "custom"}},
        )
        headers = build_default_headers(None, uuid="U", profile=profile)

        assert headers["X-Tenant"] == "shop1"
        # 已有的头不被覆盖
        assert headers["Accept"] == "application/json, text/plain, */*"

    def test_web_project_gets_its_own_origin(self) -> None:
        """有前端的第二个系统 → Origin 是它自己的域名，不是 mall4j 的。"""
        from utils.http.request_util import build_default_headers

        profile = ProjectRegistry.get("demo_shop", "web")
        assert profile is not None

        headers = build_default_headers(None, uuid="U", profile=profile)

        assert headers["Origin"] == "https://admin.example.invalid"
        assert headers["Referer"] == "https://admin.example.invalid/"
        assert "mall4j" not in str(headers)

    def test_web_project_slider_paths_from_config(self) -> None:
        """第二个系统的滑块路径也按配置走。"""
        profile = ProjectRegistry.get("demo_shop", "web")
        assert profile is not None
        assert profile.login_type == LOGIN_TYPE_SLIDER
        assert profile.slider_init_path == "/api/slider/init"
        assert profile.slider_verify_path == "/api/slider/verify"
        assert profile.slider_verify_query == {"type": "LOGIN"}


# ---------------------------------------------------------------------------
# 模块清单：新增项目不用改 modules.yaml
# ---------------------------------------------------------------------------


class TestModuleManager:
    """模块清单：新增项目不用改 modules.yaml。

    注意：``demo_shop`` 在 YAML 用例目录里，所以这些测试要显式切到
    YAML 模式 —— 默认是 Excel 模式，看不到 YAML 目录下的项目。
    """

    @pytest.fixture(autouse=True)
    def _yaml_mode(self, monkeypatch):
        """这个类测的都是 YAML 目录下的东西，先切到 YAML 模式。"""
        monkeypatch.setenv("APITEST_CASE_SOURCE", "yaml")
        from utils.cases.module_manager import ModuleManager

        # 清掉模块缓存 —— 否则会拿到上一个模式的结果
        ModuleManager.reset()

    def test_list_projects_includes_dirs_on_disk(self) -> None:
        """cases/ 下有目录的项目就应该被认出来（不用在 modules.yaml 登记）。"""
        from utils.cases.module_manager import ModuleManager

        projects = ModuleManager.list_projects()
        assert "mall4j" in projects
        assert "demo_shop" in projects

    def test_list_modules_includes_local_module_yaml(self) -> None:
        """模块目录里的 module.yaml 也要算进去（标签 / 顺序）。"""
        from utils.cases.module_manager import ModuleManager

        modules = ModuleManager.list_modules("demo_shop")
        names = [m["name"] for m in modules]
        assert "auth" in names

        auth = next(m for m in modules if m["name"] == "auth")
        assert "demo" in auth["marks"]

    def test_get_label_prefers_configured_label(self) -> None:
        from utils.cases.module_manager import ModuleManager

        assert ModuleManager.get_label("demo_shop", "auth") == "认证登录"


# ---------------------------------------------------------------------------
# Excel 模板生成：给新系统建模板时不能带 mall4j 的东西
# ---------------------------------------------------------------------------


class TestNewCaseTemplate:
    """``new_case.py`` 生成的模板要按项目走。

    这是用户给新系统建用例的第一入口 —— 模板里留着 mall4j 的
    ``accesstoken`` / ``{{mall4j.token}}`` 的话，填出来的用例
    到别的系统上直接跑不通。
    """

    def test_auth_headers_follow_project(self) -> None:
        from scripts import new_case

        headers = new_case.project_auth_headers("demo_shop")
        assert "token: {{demo_shop.token}}" in headers
        assert "sessionId: {{demo_shop.sessionId}}" in headers
        assert "accesstoken" not in headers
        assert "mall4j" not in headers

    def test_auth_headers_for_mall4j_unchanged(self) -> None:
        from scripts import new_case

        headers = new_case.project_auth_headers("mall4j")
        assert "Authorization: {{mall4j.token}}" in headers
        # mall4j 不带会话标识头 → 不该出现 uuid
        assert "uuid" not in headers

    def test_variable_rows_follow_project(self) -> None:
        from scripts import new_case

        rows = new_case.project_variable_rows("demo_shop")
        examples = [r[1] for r in rows]
        assert "{{demo_shop.token}}" in examples
        assert "{{demo_shop.sessionId}}" in examples
        assert not any("mall4j" in e for e in examples)

    def test_project_label_uses_config(self) -> None:
        from scripts import new_case

        assert new_case.project_label("demo_shop") == "示例商城（演示多系统兼容）"

    def test_existing_file_is_not_overwritten(self, tmp_path, monkeypatch) -> None:
        """已有用例文件不能被静默覆盖（这目录不是 git 仓库，救不回来）。"""
        from scripts import new_case

        # 在临时目录里先造一个已存在的用例文件 —— 不要碰真实目录
        # （真实目录里的 statistics.xlsx 是 11 条真用例，碰了就出事）
        monkeypatch.setattr(new_case, "current_excel_dir", lambda: tmp_path)
        new_case.build_template("statistics", project="mall4j")

        with pytest.raises(FileExistsError):
            new_case.build_template("statistics", project="mall4j")

    def test_force_allows_overwrite(self, tmp_path, monkeypatch) -> None:
        """显式 force=True 才允许覆盖。

        ⚠️ 必须把输出目录指到临时目录 —— 否则会真的覆盖掉
        正式的 statistics.xlsx（里面是 11 条真用例）。
        """
        from scripts import new_case

        monkeypatch.setattr(new_case, "current_excel_dir", lambda: tmp_path)

        path = new_case.build_template("mymodule", project="mall4j")
        assert path.exists()
        assert path.parent == tmp_path / "mall4j" / "mymodule"

        # 第二次默认会被挡住
        with pytest.raises(FileExistsError):
            new_case.build_template("mymodule", project="mall4j")

        # 显式 force 才放行
        again = new_case.build_template("mymodule", project="mall4j", force=True)
        assert again == path

    def test_generated_template_has_project_headers(self, tmp_path, monkeypatch) -> None:
        """生成出来的模板里，鉴权头是那个项目自己的。"""
        from scripts import new_case

        monkeypatch.setattr(new_case, "current_excel_dir", lambda: tmp_path)
        path = new_case.build_template("mymodule", project="demo_shop")

        from openpyxl import load_workbook

        wb = load_workbook(path)
        headers = wb["接口定义"].cell(row=2, column=5).value
        assert "token: {{demo_shop.token}}" in headers
        assert "mall4j" not in headers

    def test_generated_template_has_no_project_column(self, tmp_path, monkeypatch) -> None:
        """模板里不该再有「项目」列。

        项目层（epic）统一由框架按项目档案解析 —— Excel 里手填的是死名字，
        跟框架解析出来的对不上，报告里同一个项目会裂成两棵树。
        """
        from scripts import new_case

        monkeypatch.setattr(new_case, "current_excel_dir", lambda: tmp_path)
        path = new_case.build_template("mymodule", project="mall4j")

        from openpyxl import load_workbook

        wb = load_workbook(path)
        headers = [c.value for c in wb["测试用例"][1]]
        assert "项目" not in headers
        assert "模块" in headers
        assert "场景" in headers


# ---------------------------------------------------------------------------
# 环境安全：非正式环境不能偷偷指向正式地址
# ---------------------------------------------------------------------------


class TestProductionMasquerading:
    """环境名是"标签"，地址才是"事实"。

    配置里写了 ``test`` 环境但 host 还是线上地址时，
    跑测试就是在打线上 —— 而报告上写的是"测试环境"，看报告的人分辨不出来。
    所以要**大声提醒**。
    """

    def _profile(self, env: str, host: str) -> ProjectProfile:
        return build_profile("mall4j", env, {"host": host, "is_need_login": False})

    def test_test_env_pointing_at_production_warns(self, caplog) -> None:
        from utils.config.project_profile import _warn_if_production_masquerading

        profile = self._profile("test", "https://admin-api.mall4j.cn")
        with caplog.at_level("WARNING"):
            _warn_if_production_masquerading(profile)
        assert "正式地址" in caplog.text

    def test_production_env_does_not_warn(self, caplog) -> None:
        """正式环境打正式地址是正常的，不该喊。"""
        from utils.config.project_profile import _warn_if_production_masquerading

        profile = self._profile("online", "https://admin-api.mall4j.cn")
        with caplog.at_level("WARNING"):
            _warn_if_production_masquerading(profile)
        assert "正式地址" not in caplog.text

    def test_test_env_with_real_test_host_does_not_warn(self, caplog) -> None:
        """测试环境配了自己的地址，不该喊。"""
        from utils.config.project_profile import _warn_if_production_masquerading

        profile = self._profile("test", "https://test-api.mall4j.cn")
        with caplog.at_level("WARNING"):
            _warn_if_production_masquerading(profile)
        assert "正式地址" not in caplog.text

    def test_pre_env_pointing_at_production_warns(self, caplog) -> None:
        """预发环境指向正式地址，同样要提醒。"""
        from utils.config.project_profile import _warn_if_production_masquerading

        profile = self._profile("pre", "https://admin-api.mall4j.cn")
        with caplog.at_level("WARNING"):
            _warn_if_production_masquerading(profile)
        assert "正式地址" in caplog.text


# ---------------------------------------------------------------------------
# 报告层级：项目层统一由框架解析
# ---------------------------------------------------------------------------


class TestCaseHierarchyEpic:
    """epic（项目层）必须**只有一个**来源。

    手填的（Excel 的「项目」列、YAML 的 allure.epic）跟框架解析的
    不一致时，报告里同一个项目会裂成两棵树 —— 看报告的人只觉得莫名其妙。
    """

    def test_framework_label_wins_over_manual_epic(self) -> None:
        from testcases.case_hierarchy import CaseHierarchy

        case = {"testcase": {"name": "x", "allure": {"epic": "手填的项目名"}}}
        hierarchy = CaseHierarchy.resolve(case, "mall4j")
        # 框架解析出来的应该带环境后缀，而不是手填的那个
        assert hierarchy["epic"] != "手填的项目名"
        assert "mall4j" in hierarchy["epic"]

    def test_excel_project_column_is_ignored(self) -> None:
        """Excel 里手填的「项目」列不再进 allure 层级。"""
        from utils.excel.parser import parse_case_rows

        rows = [
            {
                "用例名": "示例用例",
                "关联接口": "demo",
                "项目": "手填的项目名",
                "模块": "首页统计",
                "场景": "订单统计",
            }
        ]
        apis = {"demo": {"id": "demo", "path": "/x", "method": "GET"}}
        cases = parse_case_rows(rows, apis, {})
        allure_meta = cases[0]["testcase"].get("allure", {})
        assert "epic" not in allure_meta
        assert allure_meta.get("feature") == "首页统计"
        assert allure_meta.get("story") == "订单统计"
