"""用例运行器：把 YAML 用例真正跑起来。

执行流程：

1. 读 YAML，校验结构
2. 按需登录（把 token 放进全局变量）
3. 逐步执行 steps：
   - 解析变量（``{{ }}``）
   - 发请求
   - 做断言
   - 把响应存进"步骤变量"，供后续步骤引用
4. 执行 teardowns 清理

和参考框架的差别：参考框架是"先由 YAML 生成一个 .py 文件再跑"，
这里直接数据驱动，少了一层生成物，改 YAML 立刻生效（不用删了重新生成）。
"""
from __future__ import annotations

import re
from typing import Any

from framework.allure_util import allure, attach_error, attach_json, attach_text
from framework.case_param import format_params
from utils.assertions.assert_handler import AssertHandler
from utils.cases.case_schema import validate_setups, validate_teardowns, validate_test_data
from utils.infra.globals import Globals
from utils.logs.log_manager import RequestLogger, log
from utils.infra.mock_server import MockConnectionError, get_registry
from utils.config.project_profile import ProjectRegistry
from testcases.project_login_handler import ProjectLoginHandler
from utils.http.request_handler import RequestHandler
from utils.infra.safety_guard import DangerousRequestBlocked
from utils.cases.variable_resolver import VariableResolver


class CleanupTargetMissing(AssertionError):
    """后置清理的目标（通常是 id）没取到。

    典型来源：``{{tools.max_id(...)}}`` 在查询结果为空时返回 ``None``。
    以前会把它拼进路径，发出 ``DELETE /admin/xxx/None`` —— 接口回
    A00005，报告上多一条和真实失败无关的噪音。

    现在不发这个请求，改成：
    * 用例本来就失败了 → 记日志跳过（数据多半压根没造出来）
    * 用例是通过的     → 抛这个异常，明确说"没清干净"，而不是 A00005

    继承 AssertionError：和 ``TeardownError`` 一样，这是"环境没清干净"
    的用例失败，不是框架自己坏了（不该记成 error）。
    """


class TeardownError(AssertionError):
    """后置清理（teardown）失败。

    继承 AssertionError —— 因为它代表"用例没过"（用例本身写得没问题，
    是环境没清干净），而不是框架自己坏了。pytest 会把它记成 failed
    而不是 error，和 setup 失败的处理保持一致。
    """


def _step_deletes_setup_data(
    case: dict[str, Any], step: dict[str, Any]
) -> bool:
    """这个 DELETE 步骤删的是不是本用例自己造/查出来的测试数据？

    安全闸（``utils/safety_guard.py``）的职责是**防止误删真实数据**，
    而不是阻止测试清理自己刚造的数据。所以只要删除的 id 来自：

    * 本用例的 setup（``{{查数据.data.records[0].id}}``）
    * 本用例前面某个写操作步骤（``{{新建.data.id}}``）

    就说明这个 id 是本用例运行期产生的，删除它是测试清理的一部分，
    自动放行，不需要在用例里手工写 ``allow_dangerous: true``。

    显式的 ``allow_dangerous`` 仍然保留 —— 用于删「环境里本来就有的」
    测试数据（比如固定 id 的脏数据）。这里只做「引用了本地造数」的
    自动识别，不放宽其它情况。
    """
    if str(step.get("method") or "").upper() != "DELETE":
        return False

    # 路径 / 查询参数 / 请求体里任何一处引用都算
    raw = " ".join(
        str(step.get(key) or "") for key in ("path", "params", "data")
    )
    if "{{" not in raw:
        return False

    sources = {
        str(item.get("id"))
        for item in (case.get("setups") or [])
        if item.get("id")
    }
    for prev in case.get("steps") or []:
        if prev is step:
            break
        # 只有「写」步骤的输出才是它造的数据；GET 查到的可能是
        # 环境里本来就有的真实数据，不能自动放行。
        if (
            str(prev.get("method") or "").upper() in ("POST", "PUT", "PATCH")
            and prev.get("id")
        ):
            sources.add(str(prev["id"]))

    for source in sources:
        # 匹配 {{source.xxx}} / {{tools.max_id(source.data, ...)}}
        # 之类以 ``source.`` 开头的引用；前面不能紧跟标识符字符，
        # 避免 source=a 时误匹配到 {{abc.a}}。
        if re.search(
            r"(?<![\w一-鿿])" + re.escape(source) + r"\.",
            raw,
        ):
            return True
    return False


def _teardown_deletes_case_data(
    case: dict[str, Any], teardown: dict[str, Any]
) -> bool:
    """这个后置清理删的，是不是本用例刚造的数据？

    和 ``_step_deletes_setup_data`` 的区别：teardown 里的 id 常常是
    **查询后再取最大 id**（``{{tools.max_id(查数据.data, "areaId")}}``），
    引用的是 GET 的结果，按"引用来源是不是写操作"判不出来。

    所以这里放宽一层，但**不是无条件放行**：

    1. 用例/清理显式写了 ``allow_dangerous`` → 放行
    2. 引用的是本用例 setup / 写步骤造的数据 → 放行
    3. 本用例（含 setup）**确实写过数据** → 放行

    第 3 条是关键：一条纯查询用例的 teardown 里出现 DELETE，
    那是"用例写错了"或者"路径配错了"，不该悄悄删掉环境里的真实数据 ——
    这种仍然要过安全闸（命中受保护路径就会被拦下并明确失败）。

    **但"用例写过数据"不足以放行**：``DELETE /admin/brand/100`` 这种
    硬编码目标的清理，删的是环境里的第 100 条品牌，不是本用例造的那条。
    以前只要用例有任写步骤就无条件放行，等于给这类 teardown 开了绿灯。
    现在必须先看到 ``{{...}}`` 引用（造数产出 / 查到的 id / tools.*），
    否则一律不放行，由调用方拒绝执行。

    以前这里写死 ``allow_dangerous=True``，等于对后置清理完全关掉了安全闸。
    """
    if bool(teardown.get("allow_dangerous") or case.get("allow_dangerous")):
        return True

    if _step_deletes_setup_data(case, teardown):
        return True

    # 目标不可追溯（没有引用任何变量）→ 删的是固定数据，不放行。
    if not _teardown_references_case_data(teardown):
        return False

    writes = {"POST", "PUT", "PATCH", "DELETE"}
    for item in list(case.get("setups") or []) + list(case.get("steps") or []):
        if str(item.get("method") or "").upper() in writes:
            return True
    return False


def _teardown_references_case_data(teardown: dict[str, Any]) -> bool:
    """清理目标里有没有 ``{{...}}`` 变量引用（路径/参数/请求体）。

    只看这三个字段：``Authorization: {{token}}`` 是鉴权头，不能拿它证明
    "删的是自己造的数据"。
    """
    raw = " ".join(
        str(teardown.get(key) or "") for key in ("path", "params", "data")
    )
    return "{{" in raw


#: 不带 WHERE 就不许执行的 SQL（写操作）
_DB_WRITE_NO_WHERE = ("DELETE", "UPDATE")
#: 无论有没有 WHERE 都不许执行的 SQL（结构变更 / 清表）
_DB_FORBIDDEN = ("DROP", "TRUNCATE", "ALTER", "GRANT", "REVOKE")

#: SQL 注释 —— 判断前先去掉，否则 ``DELETE FROM t -- WHERE id=1`` 会被当成"有 WHERE"
_SQL_COMMENT_BLOCK = re.compile(r"/\*.*?\*/", re.S)
_SQL_COMMENT_LINE = re.compile(r"--[^\n]*")


def _strip_sql_comments(query: str) -> str:
    return _SQL_COMMENT_LINE.sub(" ", _SQL_COMMENT_BLOCK.sub(" ", query))


def _leading_verb(normalized: str) -> str:
    """取出这条 SQL 真正的动作词。

    普通的 ``DELETE ...`` 就是第一个词。但 ``WITH x AS (...) DELETE FROM t``
    这种 CTE 写法第一个词是 ``WITH`` —— 不往下找的话整条就会被放行。
    """
    tokens = normalized.split()
    if not tokens:
        return ""
    if tokens[0] == "WITH":
        for token in tokens:
            if token in _DB_WRITE_NO_WHERE or token in _DB_FORBIDDEN:
                return token
        return "WITH"
    return tokens[0]


def _check_db_write(
    query: str,
    item: dict[str, Any],
    case: dict[str, Any] | None,
    kind: str,
    op_id: str,
) -> None:
    """拦下"会把整个表清掉"的 SQL。

    HTTP 那边有 ``safety_guard`` 防误删，db 操作是直连数据库的，
    绕过了那层。这里补一道 —— 只拦明显危险的写法，正常的
    ``DELETE ... WHERE id = %s`` 照常放行。

    Raises:
        RuntimeError: SQL 命中危险规则且没有显式 ``allow_dangerous``
    """
    from utils.infra.safety_guard import allow_dangerous_enabled

    normalized = " ".join(_strip_sql_comments(query).split()).upper()
    verb = _leading_verb(normalized)

    if verb in _DB_FORBIDDEN:
        reason = f"{verb} 属于结构变更/清表操作"
    elif verb in _DB_WRITE_NO_WHERE and " WHERE " not in f" {normalized} ":
        reason = f"{verb} 没有 WHERE 条件，会作用到整张表"
    else:
        return

    allowed = bool(item.get("allow_dangerous") or (case or {}).get("allow_dangerous"))
    if allowed or allow_dangerous_enabled():
        log.warning("%s %s 执行危险 SQL（已显式放行）: %s", kind, op_id, query)
        return

    raise RuntimeError(
        f"{kind} {op_id} 的 SQL 被安全闸拦下：{reason}。"
        f"确实需要请在这一项里写 allow_dangerous: true（或用 "
        f"APITEST_ALLOW_DANGEROUS_DELETE=1 临时放行）"
    )


class CaseRunner:
    """执行一条 YAML 用例。"""

    def __init__(self, env: str | None = None):
        self.env = env
        self.login_handler = ProjectLoginHandler()
        self.assert_handler = AssertHandler()
        # 校验证据落地到报告 —— 断言层不直接依赖 allure（utils/ 不能反向
        # import framework/），由执行层把 sink 注进去
        self.assert_handler.evidence_sink = lambda evidence: attach_json(
            evidence, name="校验证据"
        )
        # 每条断言包成 allure 子步骤 —— 否则报告里只有一个空的「校验结果」，
        # 看不出到底校验了什么、期望什么。
        self.assert_handler.step_sink = allure.step
        # 断言失败时把断言定义和错误贴成附件（步骤标题放不下完整 SQL）
        self.assert_handler.attach_sink = lambda content, name: attach_json(
            content, name=name
        )
        # 最近一条用例的请求日志（失败时贴进报告）
        self.last_request_log: str = ""
        self.last_request_records: list[dict[str, Any]] = []
        # 最近一个发出请求的步骤的信息（失败时用来归因）
        self.last_step_info: dict[str, Any] = {}
        # setup 失败原因（run_case 里会重置；放这里是为了兼容直接调
        # _run_setups 的单测，避免 except 分支 AttributeError）
        self._setup_errors: list[str] = []
        self._setup_failure_info: dict[str, Any] | None = None
        #: ``prepare()`` 抛出的异常。
        #:
        #: pytest 下前置失败**不在 fixture 里抛**（那会让 allure 丢掉整个
        #: Set up 区块，还会跳过 fixture 的 finalizer），而是记在这里，
        #: 由用例体 ``test_api_case`` 抛出 —— 详见 testcases/test_cases.py
        #: 的 ``case_lifecycle``。
        self.prepare_error: BaseException | None = None
        # 当前正在跑的用例（name / module / params）—— 归因时用来确认
        # ``last_step_info`` 确实是这条用例的，而不是上一条留下的
        self._current_case: dict[str, Any] = {}
        # 三段式执行的上下文（prepare 时写入，cleanup 时清掉）
        self._active: dict[str, Any] | None = None
        # 用例体有没有真的跑过（依赖结果落盘要用：setup 失败时不能记成通过）
        self._body_ran = False
        self._last_session_vars: dict[str, Any] = {}
        #: 这条用例是否已经失败（setup / steps 抛过异常）。
        #:
        #: 后置清理用它区分「清理目标没取到」要不要报错：
        #: - 用例本来就没过 → 目标没取到通常意味着数据压根没造出来，
        #:   跳过即可，不能再往失败信息里加噪音
        #: - 用例是通过的 → 目标没取到说明该清的没清，要明确报出来
        self._case_failed = False

    # ---------------- 主入口 ----------------

    def run_case(
        self,
        case_data: dict[str, Any],
        project_name: str,
        param_vars: dict[str, Any] | None = None,
        module: str = "",
    ) -> dict[str, Any]:
        """执行一条完整用例（前置 + 步骤 + 后置）。

        保留这个入口是为了兼容直接调用运行器的场景（单测、维护工具）。
        pytest 里走的是 fixture 驱动的 ``prepare / execute / cleanup``
        三段式 —— Allure 只会把 fixture 里的前置/后置放进
        Set up / Tear down 区块，用例体里的步骤只会进 Test body。

        Returns:
            这条用例的步骤变量（``{步骤ID: 响应}``）——
            依赖它的下游用例要用它。
        """
        original_error: BaseException | None = None
        try:
            self.prepare(
                case_data, project_name, param_vars=param_vars, module=module
            )
            self.execute()
        except BaseException as exc:
            original_error = exc
            raise
        finally:
            # teardown 会把 ``last_step_info`` 改成清理请求。用例本来已经失败时，
            # 归因必须看**原始失败那一步**（setup / steps），而不是清理请求 ——
            # 否则报告会拿 DELETE 的响应去解释一个断言失败。
            info_before_teardown = dict(self.last_step_info)
            try:
                self.cleanup()
            except Exception as cleanup_exc:  # noqa: BLE001
                # 清理失败不能盖住原始失败原因。
                #
                # 两种情况分开处理：
                # - 用例本来就没过 → 保留原始失败，把清理失败作为附加信息
                # - 用例本来是过的   → 清理失败就是本次唯一的失败原因
                if original_error is not None:
                    self._attach_cleanup_error(original_error, cleanup_exc)
                    log.error(
                        "teardown 清理失败（已附在原始失败原因上）: %s", cleanup_exc
                    )
                else:
                    raise TeardownError(
                        f"用例 {self._current_case.get('name', '')} "
                        f"执行完成，但后置清理失败: {cleanup_exc}"
                    ) from cleanup_exc
            # 用例已有原始失败 → 归因用失败那一步，而不是刚才的清理请求。
            if original_error is not None:
                self.last_step_info = info_before_teardown

        log.info("用例 %s 执行完成", self._current_case.get("name", ""))

        # 返回步骤变量 —— 依赖这条用例的下游用例要用
        return self._last_session_vars

    # ---------------- 三段式（pytest fixture 用） ----------------

    def prepare(
        self,
        case_data: dict[str, Any],
        project_name: str,
        param_vars: dict[str, Any] | None = None,
        module: str = "",
    ) -> None:
        """执行准备，并记下"这条用例已经失败了"。

        失败标记有两个用处：

        * 后置清理目标没取到时，决定是静默跳过还是报失败
          （见 ``_run_teardowns``）
        * 包装放在这里而不是调用方，是为了让 pytest 三段式和
          ``run_case`` 两条路径都覆盖到
        """
        try:
            self._prepare(case_data, project_name, param_vars=param_vars, module=module)
        except BaseException:
            self._case_failed = True
            raise

    def _prepare(
        self,
        case_data: dict[str, Any],
        project_name: str,
        param_vars: dict[str, Any] | None = None,
        module: str = "",
    ) -> None:
        """校验用例、加载依赖、登录、执行 setups。

        失败时抛异常（setup 断言失败是 ``AssertionError``）。

        pytest 下 ``case_lifecycle`` 会接住这个异常记到
        ``prepare_error``，由用例体抛出 —— 放在 fixture 里抛的话，
        allure 会把整个 Set up 区块丢掉（见 test_cases.py 的说明）。

        失败后 ``cleanup()`` 仍然要跑（setup 可能已经造了一半的数据），
        所以 ``_active`` 在跑 setups **之前**就记下了。
        """
        # 上一轮的前置异常绝不能留到这一轮 —— 用例校验（validate_test_data）
        # 本身就可能是抛异常的地方，放在它后面清就已经晚了。
        self.prepare_error = None
        validate_test_data(case_data)
        case = case_data["testcase"]
        param_vars = param_vars or {}

        # 归因用的"最近请求"必须从**这条用例**开始算。
        #
        # 运行器是整个 session 共用的，不清的话：这条用例在发第一个请求前
        # 就失败了（变量解析出错、依赖缺失），``last_step_info`` 还是上一条
        # 用例的 —— 归因会拿别人的响应去分析，得出"服务端返回 A00005"这种
        # 完全不沾边的结论。
        self.last_step_info = {}
        # setup 失败的原始原因和当时那一步的信息。
        # 不记的话，`_run_setups` 只返回 False，最终抛出的
        # 「setup 执行失败」看不到到底是哪条断言、报了什么错。
        self._setup_errors = []
        self._setup_failure_info = None
        self._body_ran = False
        self._last_session_vars = {}
        self._active = None
        self._case_failed = False
        self._current_case = {
            "name": str(case.get("name") or ""),
            "module": module,
            "params": dict(param_vars),
        }

        log.info("=" * 20)
        log.info("开始执行用例: %s (%s)", case["name"], case.get("description", ""))
        if param_vars:
            # 打成 "searchType=LAST_SEVEN" 这种文字，别打一坨字典
            log.info("参数组: %s", format_params(param_vars))
        log.info("=" * 20)

        # 清掉上一条用例留下的固定值。
        #
        # ``tools.fixed()`` 用来让「造数据」和「查数据」用同一个动态名字
        # （时间戳每次都变，不固定的话前后对不上）。
        # 但固定值不能跨用例 —— 否则第二条用例会拿到第一条的名字，
        # 查出来的数据不是自己造的，清理也会删错。
        from utils.tools import reset_fixed

        reset_fixed()

        # 本次用例单独的请求日志记录器（失败时贴进报告）
        request_logger = RequestLogger()
        RequestHandler.set_request_logger(request_logger)

        session_vars: dict[str, Any] = {}
        resolver = VariableResolver(
            session_vars=session_vars,
            global_vars=Globals.get_data(),
            param_vars=param_vars,
        )

        # ---- 依赖用例：等它跑完，把它的输出拿过来 ----
        #
        # 串行执行时，依赖用例一定已经跑完了（收集阶段排好了顺序），
        # 这里直接读结果。
        # 并行执行时，下游用例可能先启动 —— 那就等着，
        # 最多等 N 秒（默认 10 分钟），超时报错而不是永远卡住。
        dependencies = case.get("depends_on") or []
        if isinstance(dependencies, str):
            dependencies = [dependencies]
        dependencies = [str(d).strip() for d in dependencies if str(d).strip()]

        if dependencies:
            self._load_dependency_outputs(
                dependencies, resolver, str(case.get("name") or "")
            )

        # 需要登录的项目先登录一次
        self._login_projects(case, project_name)
        # 登录会往全局变量里写 token，刷新一次，让用例能引用到。
        #
        # ⚠️ 这里必须**合并**而不是整体替换 —— 依赖用例的输出挂在
        #    resolver.global_vars 上，整体替换会把它们冲掉，
        #    下游用例就取不到「{{上游用例.步骤.字段}}」了。
        resolver.global_vars.update(Globals.get_data())

        # 先登记上下文再跑 setups —— setup 失败时 cleanup() 还要靠它
        # 找到 teardowns / resolver / 请求日志。
        self._active = {
            "case": case,
            "project": project_name,
            "resolver": resolver,
            "session_vars": session_vars,
            "module": module,
            "logger": request_logger,
        }

        if validate_setups(case.get("setups")) and not self._run_setups(
            case, project_name, resolver, session_vars, module=module
        ):
            # setup 失败 = 用例失败（不是 error —— 用例本身写得没问题，
            # 是前置数据没造出来）。把第一个失败原因带上 ——
            # 否则报告里只有一句"setup 执行失败"，看不出是哪条断言挂了。
            if self._setup_failure_info:
                self.last_step_info = dict(self._setup_failure_info)
            detail = "；".join(self._setup_errors)
            suffix = f"（{detail}）" if detail else ""
            raise AssertionError(
                f"用例 {case['name']} 的 setup 执行失败{suffix}，steps 未执行"
            )

    def execute(self) -> dict[str, Any]:
        """执行用例步骤（pytest 用例体）。"""
        active = self._active
        if active is None:
            raise RuntimeError("execute() 之前必须先调用 prepare()")

        self._body_ran = True
        try:
            self._run_steps(
                active["case"],
                active["project"],
                active["resolver"],
                active["session_vars"],
                module=active["module"],
            )
        except BaseException:
            self._case_failed = True
            raise
        self._last_session_vars = active["session_vars"]
        return active["session_vars"]

    def cleanup(self) -> None:
        """执行后置清理，并结算请求日志。

        可以重复调用（第一次之后就是空操作）—— pytest fixture 的
        finalizer 在 setup 失败时也会被调用。
        """
        active = self._active
        if active is None:
            return

        case = active["case"]
        try:
            if validate_teardowns(case.get("teardowns")):
                self._run_teardowns(
                    case,
                    active["project"],
                    active["resolver"],
                    active["session_vars"],
                    module=active["module"],
                )
        finally:
            # 恢复默认（避免串到下一条用例）
            RequestHandler.set_request_logger(None)
            # 最后一条用例的请求日志留一份，失败时报告要用
            logger = active["logger"]
            self.last_request_log = logger.as_text()
            self.last_request_records = logger.as_list()
            self._active = None

    # ---------------- 三段式状态（给 fixture / 报告层读） ----------------

    @property
    def body_ran(self) -> bool:
        """用例体（steps）有没有真的跑过。

        setup 失败时用例体不会执行，依赖结果不能记成 "passed"。
        """
        return self._body_ran

    @property
    def session_vars(self) -> dict[str, Any]:
        """用例体执行后得到的步骤变量（依赖结果落盘用）。"""
        return self._last_session_vars

    def current_request_log(self) -> str:
        """当前这条用例的请求日志。

        前置失败时 ``cleanup()`` 还没跑，``last_request_log`` 还是上一条
        用例的 —— 直接读它会把这轮的失败归到上一条用例的请求上。
        """
        active = self._active
        if active is not None:
            return active["logger"].as_text()
        return self.last_request_log

    @staticmethod
    def _attach_cleanup_error(
        original_error: BaseException,
        cleanup_error: BaseException,
    ) -> None:
        """把清理失败的详情挂到原始失败原因上。

        原始异常的信息（哪条断言挂了）保持**排在最前面** —— 报告里第一眼
        看到的仍然是真正的问题。清理失败的详情追加在后面，避免"清理失败"
        这四个字把根因盖住。

        用 ``args`` 而不是 ``add_note``：note 只出现在完整堆栈里，
        报告的摘要行（``str(exc)``）看不到 —— 而那里才是别人第一时间看的。
        """
        detail = (
            f"；另外，后置清理也失败了（可能留下了脏数据）—— "
            f"{type(cleanup_error).__name__}: {cleanup_error}"
        )
        if original_error.args:
            original_error.args = (
                f"{original_error.args[0]}{detail}",
                *original_error.args[1:],
            )
        else:
            original_error.args = (detail,)

    # ---------------- 依赖编排 ----------------

    def _load_dependency_outputs(
        self,
        dependencies: list[str],
        resolver: VariableResolver,
        current_case_name: str = "",
    ) -> None:
        """加载依赖用例的输出，供本用例引用。

        下游用例里可以这样写::

            params:
              orderNo: '{{创建订单.create.result.orderNo}}'

        **并行时的等待**：如果依赖用例还没跑完（并行执行下很常见），
        就在这儿等它跑完。**所有依赖共用一个截止时间**，超时就报错 ——
        以前是每个依赖各等 10 分钟，依赖一多能把 CI 挂住半小时。

        **不拿旧结果凑合**：如果依赖用例根本不在本轮的执行计划里
        （状态被过滤、模块没选），立刻报错。读到上一轮的结果更危险 ——
        里面存的是上一轮的数据 id，拿去删/改的目标早就不是那条数据了。
        """
        import time

        from utils.cases.case_dependency import (
            get_store,
            get_wait_timeout,
            is_active_case,
            session_started_at,
        )

        store = get_store()
        started = session_started_at()
        timeout = get_wait_timeout()
        deadline = time.monotonic() + timeout

        for dep_name in dependencies:
            payload = store.load(dep_name)

            # 本轮之前的旧结果一律不认 —— 数据 id 已经失效
            if payload is not None and started:
                saved_at = payload.get("saved_at") or 0
                try:
                    saved_at = float(saved_at)
                except (TypeError, ValueError):
                    saved_at = 0.0
                if saved_at and saved_at < started:
                    log.info(
                        "依赖用例「%s」的结果是上一轮留下的，忽略", dep_name
                    )
                    payload = None

            if payload is None:
                if not is_active_case(dep_name):
                    # 本轮压根不会跑它 —— 等下去也是白等
                    raise AssertionError(
                        f"依赖用例「{dep_name}」不在本轮执行计划里"
                        f"（可能被 --mark / --case-status / --case-module 过滤掉了）。"
                        f"「{current_case_name}」需要它先跑，请把它一起选上。"
                    )

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    payload = None
                else:
                    log.info(
                        "等待依赖用例「%s」执行完成（最多再等 %.0f 秒）",
                        dep_name,
                        remaining,
                    )
                    # 只认本轮开始之后写入的结果 —— 磁盘上留下的旧结果
                    # 第一次轮询就会命中，等于"立刻等到"上一轮的数据
                    payload = store.wait_for(
                        dep_name, timeout=remaining, min_saved_at=started
                    )

            if payload is None:
                raise AssertionError(
                    f"等了 {timeout:.0f} 秒，依赖用例「{dep_name}」还是没有执行结果。"
                    f"它可能没被选中执行（比如状态是 draft），或者执行时崩了。"
                    f"可用 APITEST_DEPENDENCY_TIMEOUT 调整等待时长。"
                )

            if payload.get("status") == "failed":
                raise AssertionError(
                    f"依赖用例「{dep_name}」执行失败，不能继续跑「{current_case_name}」"
                )

            outputs = payload.get("outputs")
            if isinstance(outputs, dict) and outputs:
                # 挂到全局变量 —— 下游用 {{依赖用例名.步骤名.字段}} 引用
                resolver.global_vars[dep_name] = outputs
                log.info(
                    "已加载依赖用例「%s」的输出（%s 个步骤）",
                    dep_name,
                    len(outputs),
                )

    # ---------------- 步骤 ----------------

    def _run_steps(
        self,
        case: dict[str, Any],
        project_name: str,
        resolver: VariableResolver,
        session_vars: dict[str, Any],
        module: str = "",
    ) -> None:
        for step in case["steps"]:
            step_id = step["id"]
            step_project = step.get("project") or project_name
            project_config = self._project_config(step_project)

            log.info("--- step %s: %s", step_id, step.get("description", ""))

            step_title = f"{step_id} {step.get('description', '')}".strip()

            with allure.step(step_title):
                # 特殊步骤类型（不走普通 HTTP 请求）
                special = self._run_special_step(step, step_project, resolver)
                if special is not None:
                    session_vars[step_id] = special
                    continue

                url = project_config["host"].rstrip("/") + resolver.process_data(step["path"])
                # 默认头（配置里的）+ 用例自己的头（覆盖默认值）
                #
                # 标了 no_auth 的用例**故意**不带鉴权头（测未登录/越权）。
                # 否则框架自动补 token，C1 维度永远测不出来。
                headers = {
                    **self._default_headers(
                        step_project, resolver, skip_auth=self._skips_auth(case)
                    ),
                    **(resolver.process_data(step.get("headers")) or {}),
                }
                data = resolver.process_data(step.get("data"))
                params = resolver.process_data(step.get("params"))
                files = resolver.process_data(step.get("files"))

                # ---- 1. 组装请求 ----
                #
                # 请求信息、请求体**分开**贴，不挤在一个 JSON 里 ——
                # Allure 会给 JSON 附件做格式化高亮，分开才看得清。
                with allure.step("组装请求"):
                    attach_json(
                        {
                            "method": step["method"],
                            "url": url,
                            "headers": headers,
                            "params": params,
                        },
                        name=f"{step_id}-请求",
                    )
                    if data is not None:
                        attach_json(data, name=f"{step_id}-请求体")

                # 限流按项目分组（不同项目可以有不同 QPS）
                RequestHandler.set_rate_limit_key(step_project)

                # ---- Mock 拦截 ----
                #
                # 发请求之前先查拦截表。命中了就用预设响应替代真实请求，
                # 没命中就照常发 —— 这样「正常路径」和「异常路径」能共用
                # 同一份用例，只靠 mocks/ 下的规则文件区分。
                mocked = get_registry().try_intercept(
                    method=step["method"],
                    path=resolver.process_data(step["path"]),
                    params=params,
                    body=data,
                    project=step_project,
                )

                # ---- 2. 发送请求 ----
                #
                # 请求还没拿到响应就先把步骤上下文记上。
                #
                # ``last_step_info`` 之前只在拿到响应之后才写，所以「发送阶段
                # 抛异常」（安全闸拦截、连接超时、DNS 失败）时会停在登录时写的
                # ``{"stage": "login"}`` —— 归因把「DELETE 被安全闸拦下」误报成
                # 「登录失败：DangerousRequestBlocked」，排查方向完全跑偏。
                # response / meta 先留空，成功后再覆盖成真实值。
                self.last_step_info = {
                    "stage": "step",
                    "step_id": step_id,
                    "method": step["method"],
                    "url": url,
                    "path": resolver.process_data(step["path"]),
                    "module": module,
                    "response": None,
                    "meta": None,
                }
                with allure.step("发送请求"):
                    if mocked is not None:
                        response, meta = mocked
                    else:
                        response, meta = self._send_with_relogin(
                            step_project=step_project,
                            method=step["method"],
                            url=url,
                            headers=headers,
                            data=data,
                            params=params,
                            files=files,
                            # 放行条件（满足其一）：
                            # 1) 用例/步骤显式声明 allow_dangerous
                            # 2) DELETE 的 id 引用了本用例 setup 或前置
                            #    写步骤造出来的数据 —— 那是测试数据，
                            #    安全闸不该拦（详见 _step_deletes_setup_data）
                            allow_dangerous=bool(
                                step.get("allow_dangerous") or case.get("allow_dangerous")
                            )
                            or _step_deletes_setup_data(case, step),
                            # 下载类接口（导出 Excel）返回二进制，别按 JSON 解析
                            response_type=str(step.get("response_type") or "json"),
                            # 匿名用例被拒绝是「预期结果」，不能重登录后重试
                            skip_auth=self._skips_auth(case),
                        )

                    # 响应体单独贴一个附件
                    # （二进制下载的 content 很大，贴报告时去掉，只留元信息）
                    if isinstance(response, dict) and response.get("__binary__"):
                        attach_json(
                            {k: v for k, v in response.items() if k != "content"},
                            name=f"{step_id}-响应体",
                        )
                    else:
                        attach_json(response, name=f"{step_id}-响应体")

                    # 记下这个步骤的信息 —— 断言失败时用来归因
                    # （是服务端返回的错，还是用例自己写错了）
                    self.last_step_info = {
                        "stage": "step",
                        "step_id": step_id,
                        "method": step["method"],
                        "url": url,
                        "path": resolver.process_data(step["path"]),
                        "module": module,
                        "response": response,
                        "meta": meta,
                    }

                # 存进步骤变量，后续步骤可引用 {{step_id.xxx}}
                session_vars[step_id] = response

                # ---- 3. 校验结果 ----
                if step.get("assert"):
                    with allure.step("校验结果"):
                        self.assert_handler.handle_assertion(
                            asserts=resolver.process_data(step["assert"]),
                            response=response,
                            db_config=project_config.get("mysql"),
                            meta=meta,
                        )
                else:
                    # 没有断言也要留个痕迹 —— 否则报告里看不出这条步骤
                    # 到底校验了没有
                    with allure.step("校验结果"):
                        attach_text(
                            f"HTTP {meta.get('status_code')}，耗时 {meta.get('elapsed_ms')}ms；"
                            "这条用例没有配断言",
                            name=f"{step_id}-校验说明",
                        )

    def _run_special_step(
        self,
        step: dict[str, Any],
        project_name: str,
        resolver: VariableResolver,
    ) -> Any | None:
        """处理"特殊步骤"——不走普通 HTTP 请求的步骤。

        目前支持：

        - ``operation_type: login``
          走完整的"抓 uuid → 滑块 → 登录"链路，返回登录接口的响应。
          用来写登录接口本身的用例（普通调登录接口会因为缺 uuid 头而失败）。

        - ``operation_type: login_no_slider``
          抓个新 uuid 就直接调登录接口，**跳过滑块**。
          用来验证"服务端会拒绝没过滑块的登录请求"。

        - ``operation_type: noop``
          什么也不做，返回配置里给的值。用来做纯断言的用例。

        返回 None 表示"这是普通步骤，交给主流程处理"。
        """
        op = step.get("operation_type")

        if op == "login":
            from utils import tools as framework_tools

            # 注意：空字符串是合法输入（"空密码应该失败"），
            # 所以只有「键压根不存在」才当 None（让框架去取配置里的默认值）
            raw_user = step.get("username")
            raw_pass = step.get("password")
            username = (
                str(resolver.process_data(raw_user)) if raw_user is not None else None
            )
            password = (
                str(resolver.process_data(raw_pass)) if raw_pass is not None else None
            )

            log.info("执行登录步骤（完整滑块链路）")
            response = framework_tools.login_response(
                username, password, project=project_name
            )

            attach_json({"step": step["id"], "response": response},
                        name=f"{step['id']}-响应")

            if step.get("assert"):
                project_config = self._project_config(project_name)
                self.assert_handler.handle_assertion(
                    asserts=resolver.process_data(step["assert"]),
                    response=response,
                    db_config=project_config.get("mysql"),
                )
            return response

        if op == "login_no_slider":
            from utils import tools as framework_tools

            raw_user = step.get("username")
            raw_pass = step.get("password")
            username = (
                str(resolver.process_data(raw_user)) if raw_user is not None else None
            )
            password = (
                str(resolver.process_data(raw_pass)) if raw_pass is not None else None
            )

            log.info("执行登录步骤（跳过滑块，验证服务端会拒绝）")
            response = framework_tools.login_without_slider(
                username, password, project=project_name
            )

            attach_json({"step": step["id"], "response": response},
                        name=f"{step['id']}-响应")

            if step.get("assert"):
                project_config = self._project_config(project_name)
                self.assert_handler.handle_assertion(
                    asserts=resolver.process_data(step["assert"]),
                    response=response,
                    db_config=project_config.get("mysql"),
                )
            return response

        if op == "noop":
            # value 里可以写变量表达式（业务流用例靠它做跨接口比对）
            value = resolver.process_data(step.get("value"))
            # 包一层 {"value": ...}，让断言写法和普通步骤一致：
            #   引用：{{check.value}}      断言：field: value.matched
            result = {"value": value}
            if step.get("assert"):
                project_config = self._project_config(project_name)
                self.assert_handler.handle_assertion(
                    asserts=resolver.process_data(step["assert"]),
                    response=result,
                    db_config=project_config.get("mysql"),
                )
            return result

        if op == "mock":
            # 不发真实请求，直接返回 mock 段里配好的响应。
            #
            # 用途：下游接口还没好、或者要稳定复现某个异常场景时，
            # 让用例照样能跑通、断言照样能校验。
            mock_cfg = step.get("mock") or {}
            if not isinstance(mock_cfg, dict):
                raise ValueError(f"步骤 {step.get('id')!r} 的 mock 必须是字典")

            if mock_cfg.get("enabled") is False:
                # 显式关掉 —— 退回真实请求
                return None

            # 响应体里可以引用变量（比如把上一步的 id 塞进去）
            response = resolver.process_data(mock_cfg.get("response"))

            # 模拟网络错误
            error = mock_cfg.get("error")
            if error:
                raise MockConnectionError(str(resolver.process_data(error)))

            # 模拟慢接口
            delay = float(mock_cfg.get("delay") or 0)
            if delay > 0:
                import time

                log.info("Mock 步骤 %s 延迟 %s 秒", step.get("id"), delay)
                time.sleep(delay)

            status = int(mock_cfg.get("status", 200) or 200)
            mock_headers = resolver.process_data(mock_cfg.get("headers")) or {}
            meta = {
                "status_code": status,
                "headers": {str(k).lower(): v for k, v in mock_headers.items()},
                "elapsed_ms": 0,
                "mocked": True,
            }

            attach_json(
                {"step": step["id"], "mocked": True, "response": response},
                name=f"{step['id']}-Mock响应",
            )

            if step.get("assert"):
                project_config = self._project_config(project_name)
                self.assert_handler.handle_assertion(
                    asserts=resolver.process_data(step["assert"]),
                    response=response,
                    db_config=project_config.get("mysql"),
                    meta=meta,
                )
            return response

        return None

    # ---------------- 前置准备 ----------------

    def _run_setups(
        self,
        case: dict[str, Any],
        project_name: str,
        resolver: VariableResolver,
        session_vars: dict[str, Any],
        module: str = "",
    ) -> bool:
        """执行前置准备步骤。

        和 teardown 一样支持两种操作：

        - ``api`` —— 调接口造数据 / 改状态
        - ``db``  —— 直接操作数据库

        结果写进 ``session_vars``，后面的 steps 可以用
        ``{{setup_id.result.xxx}}`` 引用。

        Returns:
            True 表示全部 setup 成功；False 表示有 setup 失败
            （调用方会跳过 steps，但 teardown 照跑）
        """
        # 有 setup 失败就记下来（不中断后面的 setup）
        failed = False

        for setup in case.get("setups") or []:
            su_id = setup["id"]
            op = setup["operation_type"]
            su_project = setup.get("project") or project_name
            project_config = self._project_config(su_project)

            log.info("--- setup %s (%s)", su_id, op)

            with allure.step(f"前置 {su_id} ({op})"):
                try:
                    if op == "api":
                        url = project_config["host"].rstrip("/") + resolver.process_data(setup["path"])
                        RequestHandler.set_rate_limit_key(su_project)
                        # 走重登录通道 —— 和普通步骤一样。
                        #
                        # 为什么要这样：套件里有一条 ``/logOut`` 用例，它会
                        # **注销服务端的会话**。后面的用例 token 就失效了，
                        # 所有请求都返回 A00004。普通步骤有「重新登录再试一次」，
                        # setup 原先没有 —— 表现就是 setup 莫名其妙地全失败。
                        #
                        # 头也和普通步骤一致：默认头（含鉴权头）+ 用例自己写的。
                        # 以前只发用例里显式写的头，没写 Authorization 时
                        # setup 就是匿名请求 —— 表现是 setup 莫名其妙全失败。
                        headers = {
                            **self._default_headers(su_project, resolver),
                            **(resolver.process_data(setup.get("headers")) or {}),
                        }
                        response, meta = self._send_with_relogin(
                            step_project=su_project,
                            method=setup["method"],
                            url=url,
                            headers=headers,
                            data=resolver.process_data(setup.get("data")),
                            params=resolver.process_data(setup.get("params")),
                            files=None,
                            # setup 里也可能有 DELETE（比如先清理上次残留），
                            # 规则和普通步骤一致：引用本用例造的数据就放行。
                            allow_dangerous=bool(
                                setup.get("allow_dangerous") or case.get("allow_dangerous")
                            )
                            or _step_deletes_setup_data(case, setup),
                        )
                        session_vars[su_id] = response
                        attach_json(response, name=f"{su_id}-响应体")
                        # setup 的请求也算「最近一个发出的请求」——
                        # 否则 setup 的断言失败时，归因会拿上一条用例的
                        # 响应去分析，结论完全是错的。
                        self.last_step_info = {
                            "step_id": su_id,
                            "method": setup["method"],
                            "url": url,
                            "path": resolver.process_data(setup["path"]),
                            "module": module,
                            "response": response,
                            "meta": meta,
                            "stage": "setup",
                        }
                        if setup.get("assert"):
                            self.assert_handler.handle_assertion(
                                asserts=resolver.process_data(setup["assert"]),
                                response=response,
                                db_config=project_config.get("mysql"),
                                meta=meta,
                            )
                    elif op == "db":
                        self._run_db_op(
                            setup, project_config, kind="setup", case=case
                        )
                    elif op == "factory":
                        self._run_factory(setup, session_vars, kind="setup")
                    elif op == "login":
                        # 强制真实登录一次（不走 token 缓存），给本用例一份独立的
                        # accessToken / refreshToken。用完即弃，不影响共享登录态。
                        from testcases.auth import Auth

                        result = Auth().fresh_login(
                            su_project,
                            (project_config or {}).get("login") or {},
                            self.env or "",
                        )
                        if not result or not result.get("token"):
                            raise RuntimeError(f"前置 {su_id} 登录失败")
                        session_vars[su_id] = result
                        # 别把真 token 贴进报告
                        attach_json(
                            {
                                "token": "******",
                                "refresh_token": "******" if result.get("refresh_token") else "",
                            },
                            name=f"{su_id}-登录凭据",
                        )
                except Exception as exc:  # noqa: BLE001
                    # setup 失败不往上抛 —— 让用例标记为失败，
                    # 但 teardown 还能跑（setup 可能造了一半数据）
                    #
                    # 这里**不中断**后面的 setup：多个 setup 各自造一部分数据，
                    # 第一个失败就停 = 后面的没造，teardown 清理时会对不上号
                    # （它清的是"本来该存在"的东西，实际压根没建）。
                    # 全部跑完再统一返回 False，让 teardown 有完整的机会清理。
                    log.error("setup %s 失败: %s", su_id, exc)
                    attach_error(exc, name=f"{su_id}-失败详情")
                    # 记第一个失败原因 —— 最终抛出的 "setup 执行失败" 会带上它。
                    # 后面的 setup 仍会继续跑（清理机会），但原因不再被覆盖。
                    self._setup_errors.append(
                        f"{su_id}: {type(exc).__name__}: {exc}"
                    )
                    if self._setup_failure_info is None:
                        self._setup_failure_info = (
                            dict(self.last_step_info)
                            if self.last_step_info
                            else {"stage": "setup", "step_id": su_id}
                        )
                    failed = True

        return not failed

    # ---------------- 后置清理 ----------------

    def _run_teardowns(
        self,
        case: dict[str, Any],
        project_name: str,
        resolver: VariableResolver,
        session_vars: dict[str, Any],
        module: str = "",
    ) -> None:
        # 收集每个失败的 teardown，最后统一抛（不中断后面的清理）
        failures: list[tuple[str, Exception]] = []

        for teardown in case.get("teardowns") or []:
            td_id = teardown["id"]
            op = teardown["operation_type"]
            td_project = teardown.get("project") or project_name
            project_config = self._project_config(td_project)

            log.info("--- teardown %s (%s)", td_id, op)

            # 单个 teardown 失败**不中断**后面的清理。
            #
            # 为什么要这样：teardown 常常有好几个（删订单、删商品、删优惠券）。
            # 第一个失败就把后面的都跳掉，等于只清了一半 —— 脏数据全留在
            # 环境里，下一轮跑还会踩。这里把每个失败都记下来，
            # 最后一起抛出去（由 run_case 决定怎么和原始失败合并）。
            with allure.step(f"后置 {td_id} ({op})"):
                try:
                    if op == "api":
                        allow_dangerous = _teardown_deletes_case_data(case, teardown)
                        # 安全闸只拦 DELETE（且要命中受保护路径）。``PUT
                        # /admin/message/cancel/1`` 这类"改状态"清理杀的是
                        # 真实记录，闸门管不到 —— 在这里补一道：
                        # 目标不可追溯的写后置，一律拒绝执行。
                        if (
                            not allow_dangerous
                            and str(teardown.get("method") or "").upper()
                            in {"POST", "PUT", "PATCH", "DELETE"}
                        ):
                            raise DangerousRequestBlocked(
                                "后置清理拒绝执行："
                                f"{teardown.get('method')} {teardown.get('path')} "
                                "没有引用本用例造数产出的 id，删/改的是环境里的固定数据。"
                                "确需保留请显式加 allow_dangerous。"
                            )
                        # 目标 id 没取到就别发请求。
                        #
                        # ``{{tools.max_id(...)}}`` 在查询结果为空时返回 None，
                        # 拼进路径就是 ``DELETE /admin/xxx/None`` —— 接口回
                        # A00005，报告上凭空多一条和真实失败无关的噪音。
                        # 数据本来就没造出来，不发这个请求也不会漏删。
                        #
                        # 只在**整段表达式求值为 None**时拦；拼接场景里的
                        # None 会被转成字符串（``/x/None``），那种多半是用例
                        # 自己写错了，保持原样让它失败，别悄悄吞掉。
                        missing: list[str] = []
                        raw_path = teardown.get("path")
                        td_path = resolver.process_data(raw_path, missing)
                        if missing or td_path is None:
                            detail = "、".join(missing) or repr(raw_path)
                            self._skip_teardown_with_missing_target(td_id, detail)
                            continue

                        url = project_config["host"].rstrip("/") + td_path
                        RequestHandler.set_rate_limit_key(td_project)
                        # 默认头（含鉴权头）+ 用例自己写的头，和普通步骤一致。
                        #
                        # 以前 teardown 只发用例里显式写的头：没写
                        # ``Authorization`` 时请求就是匿名的 —— 后置清理
                        # 返回"未授权"，脏数据全留在环境里，而报告只说
                        # "清理失败"，看不出是没带 token。
                        headers = {
                            **self._default_headers(td_project, resolver),
                            **(resolver.process_data(teardown.get("headers")) or {}),
                        }
                        # 查询参数 / 请求体同理：目标 id 一般也在里面，
                        # 求值为 None 一样会拼成 "xxx=None"。
                        td_params = resolver.process_data(teardown.get("params"), missing)
                        td_data = resolver.process_data(teardown.get("data"), missing)
                        if missing:
                            self._skip_teardown_with_missing_target(
                                td_id, "、".join(missing)
                            )
                            continue
                        # 走重登录通道 —— 和 steps / setup 一致。
                        #
                        # 清理常常排在套件最后，这时候 token 可能已经被
                        # 某条 ``/logOut`` 用例注销了。不重登录的话，
                        # 清理必然失败，脏数据留在环境里。
                        response, meta = self._send_with_relogin(
                            step_project=td_project,
                            method=teardown["method"],
                            url=url,
                            headers=headers,
                            data=td_data,
                            params=td_params,
                            files=None,
                            # 安全闸：删的是本用例自己造的数据才放行。
                            # 上面已经拦掉了目标不可追溯的写后置。
                            allow_dangerous=allow_dangerous,
                        )
                        session_vars[td_id] = response
                        self.last_step_info = {
                            "step_id": td_id,
                            "method": teardown["method"],
                            "url": url,
                            "path": td_path,
                            "module": module,
                            "response": response,
                            "meta": meta,
                            "stage": "teardown",
                        }
                        attach_json(response, name=f"{td_id}-响应体")
                        if teardown.get("assert"):
                            self.assert_handler.handle_assertion(
                                asserts=resolver.process_data(teardown["assert"]),
                                response=response,
                                db_config=project_config.get("mysql"),
                                meta=meta,
                            )
                    elif op == "db":
                        self._run_db_op(
                            teardown, project_config, kind="teardown", case=case
                        )
                    elif op == "factory_cleanup":
                        self._run_factory_cleanup(teardown)
                    elif op == "factory":
                        # teardown 里也可以用 factory（等价于 factory_cleanup）
                        self._run_factory_cleanup(teardown)
                except Exception as exc:  # noqa: BLE001
                    log.error("teardown %s 失败: %s", td_id, exc)
                    attach_error(exc, name=f"{td_id}-失败详情")
                    failures.append((td_id, exc))

        if failures:
            # 多个失败合并成一条报错，信息里都带上
            details = "；".join(
                f"{td_id}: {type(exc).__name__}: {exc}" for td_id, exc in failures
            )
            raise TeardownError(
                f"{len(failures)} 个后置清理失败 —— {details}"
            ) from failures[0][1]

    # ---------------- 数据工厂 ----------------

    def _run_factory(
        self,
        item: dict[str, Any],
        session_vars: dict[str, Any],
        kind: str = "setup",
    ) -> None:
        """用数据工厂造数据。

        YAML 写法::

            setups:
              - id: order
                operation_type: factory
                factory: my_shop.order
                params: {count: 1}

        造出来的结果写进 ``session_vars``，后面的步骤可以引用
        （``{{order.0.orderNo}}``）。
        """
        from factory import get_factory, track_factory

        factory_name = item["factory"]
        factory = get_factory(factory_name)

        params = dict(item.get("params") or {})
        count = int(params.pop("count", 1))

        log.info("--- 工厂 %s 造数据（count=%s）", factory_name, count)

        result = factory.create(count=count, **params)

        # 登记到清理栈 —— 用例中途失败也能兜底清掉
        track_factory(factory)

        # 造的东西写进步骤变量
        session_vars_id = item["id"]
        if isinstance(result, list):
            # 列表：额外包一层方便引用（{{order.items.0.xxx}} / {{order.count}}）
            session_vars[session_vars_id] = {
                "items": result,
                "count": len(result),
                **({"first": result[0]} if result else {}),
            }
        else:
            session_vars[session_vars_id] = result

    def _skip_teardown_with_missing_target(self, td_id: str, detail: str) -> None:
        """清理目标没取到：用例已失败则跳过，否则抛 ``CleanupTargetMissing``。

        - 用例已经失败：数据多半没造出来，再报一条只会盖住根因
        - 用例是通过的：该清的没清，必须明确报出来（而不是发一个
          ``DELETE /xxx/None`` 换回一个看不懂的 A00005）
        """
        if self._case_failed:
            log.info("teardown %s 跳过：目标未取到（%s）", td_id, detail)
            attach_text(
                f"清理目标未取到，跳过：{detail}",
                name=f"{td_id}-跳过说明",
            )
            return
        raise CleanupTargetMissing(
            f"清理目标未取到：{detail} 求值为 None，未发出清理请求 —— "
            "该清的没清，可能留下了脏数据。"
        )

    def _run_factory_cleanup(self, item: dict[str, Any]) -> None:
        """清理工厂造的数据。

        YAML 写法::

            teardowns:
              - id: clean
                operation_type: factory_cleanup
                factory: my_shop.order
        """
        from factory import get_factory

        factory_name = item["factory"]
        factory = get_factory(factory_name)

        keys = factory.tracked
        if not keys:
            log.info("工厂 %s 没有待清理的数据", factory_name)
            factory.clear_tracked()
            return

        log.info("--- 清理工厂 %s 的 %s 条数据", factory_name, len(keys))
        cleaned = factory.cleanup(keys)
        factory.clear_tracked()
        log.info("工厂 %s 清理完成（%s 条）", factory_name, cleaned)

    def _run_db_op(
        self,
        item: dict[str, Any],
        project_config: dict[str, Any],
        kind: str,
        case: dict[str, Any] | None = None,
    ) -> None:
        """执行数据库操作（setup 造数据 / teardown 删数据）。

        Args:
            item: setup 或 teardown 那一项（要有 ``query``）
            project_config: 项目配置（从中取 mysql 连接信息）
            kind: ``"setup"`` 或 ``"teardown"``，只影响报错文案
            case: 所属用例（用来判断有没有显式放行危险 SQL）

        **安全闸**：SQL 是直连数据库执行的，绕过 HTTP 那套 ``safety_guard``。
        一条写错的 ``DELETE FROM prod`` 能把测试库清空，所以这里单独拦一道：

        - ``DELETE`` / ``UPDATE`` **没有 WHERE** → 拒绝（必须是显式 ``allow_dangerous``）
        - ``DROP`` / ``TRUNCATE`` / ``ALTER`` → 拒绝（同上）

        **参数化**：``query`` 里用 ``%s`` 占位，值放 ``params`` ——
        别自己拼字符串（值里有引号就会拼坏，甚至被注入）。
        """
        db_config = project_config.get("mysql")
        if not db_config:
            raise ValueError(f"{kind} {item['id']} 需要数据库操作，但配置里没有 mysql")

        query = str(item.get("query") or "").strip()
        if not query:
            raise ValueError(f"{kind} {item['id']} 的 db 操作没写 query")

        _check_db_write(query, item, case, kind=kind, op_id=str(item.get("id") or ""))

        params = item.get("params")

        import mysql.connector

        connection = mysql.connector.connect(**db_config)
        try:
            cursor = connection.cursor()
            if params:
                cursor.execute(query, params)
            else:
                cursor.execute(query)
            connection.commit()
            log.info("SQL %s 影响行数 %s", query, cursor.rowcount)
            cursor.close()
        finally:
            connection.close()

    # ---------------- 登录与配置 ----------------

    def _send_with_relogin(
        self,
        step_project: str,
        method: str,
        url: str,
        headers: dict[str, Any],
        data: Any,
        params: Any,
        files: Any,
        allow_dangerous: bool = False,
        response_type: str = "json",
        skip_auth: bool = False,
    ) -> tuple[Any, dict[str, Any]]:
        """发请求；遇到"登录态失效"就重新登录再重试一次。

        **为什么要这么做**

        有些系统的登出接口（``/logOut``）会**直接注销服务端的会话**。
        一条用例调了它，同一批跑的其它用例手里的 token 就全废了 ——
        后面每条都返回"未授权"，几百条用例集体挂掉，排查起来极其折磨。

        与其让整个套件崩掉，不如在这里**重新登录一次然后重试**：
        重新登录几秒钟，比几百条用例凭空全红划算得多。

        只重试一次 —— 反复重试说明是账号本身的问题（密码错了、被禁用了），
        那种情况应该直接报错，而不是把登录接口打爆。

        ``skip_auth=True`` 时**绝不重登录**：这条请求是故意匿名的
        （未登录 / 越权用例），"未授权"正是要验证的结果。以前这里会把
        A00004 当成登录态失效，自动补 token 再发一次，导致 C1 用例实际
        带着 token 跑，服务端返回成功，反例被误判成失败。
        """
        response, meta = RequestHandler.send_with_retry_full(
            method=method,
            url=url,
            headers=headers,
            data=data,
            params=params,
            files=files,
            allow_dangerous=allow_dangerous,
            response_type=response_type,
        )

        if skip_auth or not RequestHandler.is_unauthorized(response):
            return response, meta

        log.warning(
            "接口返回未授权（%s %s），重新登录后重试一次",
            method, url,
        )

        new_token = self._force_relogin(step_project)
        if not new_token:
            return response, meta

        # 换上新 token 再发一次
        new_headers = dict(headers)
        profile = ProjectRegistry.get(step_project, self.env or "")
        if profile is not None:
            project_data = Globals.get_data().get(step_project, {}) or {}
            session = project_data.get(profile.session_var)
            for key, value in profile.auth_headers(new_token, session).items():
                new_headers[key] = value

        response, meta = RequestHandler.send_with_retry_full(
            method=method,
            url=url,
            headers=new_headers,
            data=data,
            params=params,
            files=files,
            allow_dangerous=allow_dangerous,
            response_type=response_type,
        )
        return response, meta

    def _force_relogin(self, project_name: str) -> str | None:
        """强制重新登录某个项目，返回新的 token。

        流程：清掉缓存里那个失效的 token → 走真实登录链路 → 拿到新 token。
        """
        from utils.infra.token_cache import TokenCache

        profile = ProjectRegistry.get(project_name, self.env or "")
        token_var = profile.token_var if profile else "token"

        # 清缓存，否则登录时会直接复用那个已经失效的 token
        try:
            TokenCache.clear(project_name)
        except Exception as exc:  # noqa: BLE001
            log.warning("清理 %s 的 token 缓存失败: %s", project_name, exc)

        # 清全局变量里的 token，逼框架重新走登录流程
        project_data = Globals.get(project_name)
        if isinstance(project_data, dict):
            project_data.pop(token_var, None)

        # 走真实登录
        try:
            self.login_handler.login_if_needed(
                project_name, self._project_config(project_name), self.env or ""
            )
        except Exception as exc:  # noqa: BLE001
            log.error("%s 重新登录失败: %s", project_name, exc)
            return None

        data = Globals.get_data().get(project_name, {}) or {}
        return data.get(token_var)

    def _login_projects(self, case: dict[str, Any], project_name: str) -> None:
        """登录用例涉及的每个项目（支持一个用例跨多个项目调接口）。

        每个项目的登录都包一层 ``allure.step`` —— 登录发生在用例体里，
        不是 fixture，不包的话报告上看不到它。登录挂了（比如服务端报
        A00005）只能看到用例体里一句 ``RuntimeError``，看不出"卡在登录"。
        """
        projects = {project_name}
        for step in case["steps"]:
            if step.get("project"):
                projects.add(step["project"])

        for name in projects:
            # 登录失败时归因要能说出"卡在登录"。
            #
            # 登录发生在第一个请求之前，``last_step_info`` 这时候还是空字典；
            # 不写这一句，``_diagnose_failure`` 拼不出上下文，报告里只剩
            # 一句光秃秃的"框架异常：RuntimeError" —— 看的人第一反应会是
            # "脚本写错了"，而实际可能是服务端登录接口挂了。
            self.last_step_info = {"stage": "login", "step_id": name}
            with allure.step(f"登录 {name}"):
                self.login_handler.check_and_login_project(name, self.env or "")

    def _project_config(self, project_name: str) -> dict[str, Any]:
        """取项目配置；没有就自动从 config.yaml 载入。"""
        config = Globals.get(project_name)
        if config:
            return config

        from utils.config.config_manager import ConfigManager

        loaded = ConfigManager.get_project_env_config(project_name, self.env or "online")
        if not loaded:
            raise ValueError(f"项目 {project_name!r} 在环境 {self.env!r} 下没有配置")
        return loaded[project_name]

    @staticmethod
    def _skips_auth(case: Any) -> bool:
        """用例是否**故意**不带鉴权头（测未登录 / 越权）。

        只在主步骤上生效：前置/后置属于测试脚手架，仍然要正常鉴权。
        """
        if not isinstance(case, dict):
            return False
        if case.get("no_auth"):
            return True
        marks = case.get("marks") or []
        if isinstance(marks, str):
            marks = marks.replace(";", ",").split(",")
        return bool(
            {str(m).strip().lower() for m in marks}
            & {"no_auth", "noauth", "unauthenticated"}
        )

    def _default_headers(
        self,
        project_name: str,
        resolver: VariableResolver,
        skip_auth: bool = False,
    ) -> dict[str, Any]:
        """算这个项目的默认请求头。

        配置里可以写 ``headers:``（支持变量），比如：

        .. code-block:: yaml

            projects:
              my_shop:
                online:
                  headers:
                    uuid: "{{my_shop.uuid}}"
                    Origin: "https://admin.my-shop.com"

        用例自己的 headers 会覆盖这里的同名项。

        鉴权头（token / 会话标识）由**项目档案**决定（``utils/project_profile.py``），
        所以换个系统不用改这里 —— 头叫什么在配置里写就行。
        """
        project_config = self._project_config(project_name)
        raw = project_config.get("headers") or {}

        # 登录类接口要带会话标识，但此时可能还没登录，拿不到全局变量
        # 这种情况下用配置里的静态值
        headers: dict[str, Any] = {}
        for key, value in raw.items():
            try:
                headers[key] = resolver.process_data(value)
            except Exception:  # noqa: BLE001 - 变量取不到就用原值
                headers[key] = value

        profile = ProjectRegistry.get(project_name, self.env or "")

        # 补上框架已知的通用头
        headers.setdefault("Accept", "application/json, text/plain, */*")

        # 登录后自动带上鉴权头 ——
        # 用例里没写鉴权头时，框架补上（写了就以用例的为准）
        if profile is not None and not skip_auth:
            project_data = Globals.get_data().get(project_name, {}) or {}
            token = project_data.get(profile.token_var)
            session = project_data.get(profile.session_var)
            for key, value in profile.auth_headers(token, session).items():
                headers.setdefault(key, value)
        elif profile is not None and skip_auth:
            # 故意匿名：配置里若写了鉴权头，也要摘掉
            auth_names = {
                str(name).lower()
                for name in (profile.token_header, profile.session_header)
                if name
            }
            for key in [k for k in headers if str(k).lower() in auth_names]:
                headers.pop(key, None)

        return headers
