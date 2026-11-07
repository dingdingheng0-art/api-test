"""失败归因：判断这条用例是「脚本问题」「服务问题」还是「用例数据问题」。

**为什么要有这个**

用例挂掉时，最耗时间的不是修，是**判断该找谁**：

- 服务端返回了 ``A00005``（服务端空指针）→ 找后端
- 服务端说"参数不能为空" → 找写用例的人（请求体没填全）
- 框架自己抛了 ``KeyError`` → 找自动化维护人

光看断言信息（``[success] 期望等于 True，实际 False``）判断不出来 ——
必须结合**服务端到底返回了什么**。

**归因规则**

======== ==========================================================
归因     依据
======== ==========================================================
服务问题 服务端返回 403 / 404 / 5xx / A00005 / success=false
用例数据 服务端返回参数校验错误（A00014）；或服务端的实际行为
问题     和用例写死的预期不一致
脚本问题 框架抛了非断言类异常（KeyError / TypeError / ValueError）
环境问题 拿不到响应（连接失败、超时）
======== ==========================================================

用法::

    from utils.infra.failure_classifier import classify_response_failure

    diag = classify_response_failure(response, meta, step=step, url=url)
    print(diag.verdict)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ---- 归因分类名（报告和终端里都显示这几个词）----

SERVICE = "服务问题"        # 服务端的锅
CASE_DATA = "用例数据问题"  # 用例本身写错了（参数没填全、预期过时）
SCRIPT = "脚本问题"         # 自动化脚本的锅（框架异常）
ENVIRONMENT = "环境问题"    # 网络不通、服务没起

# 服务端常见的错误码
_CODE_NPE = "A00005"          # 服务端空指针 / 内部异常
_CODE_PARAM = "A00014"        # 参数校验不通过
_CODE_NO_PERMISSION = "A00001"  # 没有该操作的权限


@dataclass
class FailureDiagnosis:
    """一条失败的归因结论。"""

    attribution: str          # 服务问题 / 用例数据问题 / 脚本问题 / 环境问题
    category: str             # 具体原因，比如"服务端内部异常"
    case_id: str = ""         # 用例编号（API-168）—— 自愈流程靠它定位到具体一条
    module: str = ""          # 所属模块
    method: str = ""
    url: str = ""
    http_status: int | None = None
    server_code: str = ""
    server_msg: str = ""
    assertion: str = ""       # 断言本身怎么说
    hint: str = ""            # 下一步该干什么
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def verdict(self) -> str:
        """一句话结论 —— 报告里直接显示这一句。

        刻意保持短：报告的失败列表一行放不下太长的文字，
        太长反而没人看。详细原因放「失败归因」附件里。
        """
        parts = [f"【{self.attribution}】", self.category]
        if self.server_code and self.server_code not in self.category:
            parts.append(f"code={self.server_code}")
        if self.server_msg and self.server_msg not in self.category:
            short = self.server_msg if len(self.server_msg) <= 24 else self.server_msg[:24] + "…"
            parts.append(f"msg={short}")
        return "｜".join(parts)

    def to_dict(self) -> dict[str, Any]:
        """给报告附件用的字典。"""
        return {
            "归因": self.attribution,
            "结论": self.verdict,
            "具体原因": self.category,
            "用例编号": self.case_id,
            "模块": self.module,
            "接口": f"{self.method} {self.url}".strip(),
            "HTTP状态码": self.http_status,
            "服务端code": self.server_code,
            "服务端msg": self.server_msg,
            "断言信息": self.assertion,
            "处理建议": self.hint,
            **({"补充": self.extra} if self.extra else {}),
        }


# --------------------------------------------------------------------------
# 归因
# --------------------------------------------------------------------------


def classify_response_failure(
    response: Any,
    meta: dict[str, Any] | None = None,
    *,
    module: str = "",
    method: str = "",
    url: str = "",
    assertion: str = "",
) -> FailureDiagnosis:
    """根据**服务端返回了什么**来归因。

    Args:
        response: 解析后的响应体（dict / list / None）
        meta: 响应元信息（``status_code`` / ``headers`` / ``elapsed_ms``）
        module: 所属模块
        method / url: 接口信息
        assertion: 断言失败信息（原样带过去，方便对照）
    """
    meta = meta or {}
    status = meta.get("status_code")
    body = response if isinstance(response, dict) else {}
    code = _as_text(body.get("code"))
    msg = _as_text(body.get("msg"))
    success = body.get("success")

    diag = FailureDiagnosis(
        attribution=SERVICE,
        category="",
        module=module,
        method=method,
        url=url,
        http_status=status if isinstance(status, int) else None,
        server_code=code,
        server_msg=msg,
        assertion=assertion,
    )

    # ---- 1. HTTP 层面的问题 ----
    if isinstance(status, int):
        if status == 403:
            diag.category = "服务端拒绝访问（HTTP 403，缺权限标识）"
            diag.hint = (
                "当前登录账号没有这个接口的权限标识。"
                "需要在后台「菜单/角色权限」里把对应权限配上，或换一个高权限账号跑。"
            )
            return diag
        if status == 404:
            diag.category = "接口路由不存在（HTTP 404）"
            diag.hint = "服务端没有这个路由 —— 接口路径可能变了，或该功能还没上线。找后端确认。"
            return diag
        if status >= 500:
            diag.category = f"服务端异常（HTTP {status}）"
            diag.hint = "服务端 5xx —— 找后端。"
            return diag

    # ---- 2. 业务层的错误码 ----
    if code == _CODE_NPE:
        diag.category = "服务端内部异常（A00005，常见于数据不存在导致空指针）"
        diag.hint = (
            "服务端抛了 A00005。用同一套 token 直接调一次这个接口就能确认："
            "如果换个参数还是 A00005，就是整个模块的服务有问题，找后端；"
            "如果只是这个 id 报错，说明用例里写死了不存在的 id，要改成动态取值。"
        )
        return diag

    if code == _CODE_NO_PERMISSION or "权限" in msg:
        diag.category = "账号缺该接口的权限（服务端返回 A00001）"
        diag.hint = (
            "当前登录账号没有这个接口的权限标识。"
            "在后台「菜单/角色权限」里给这个账号配上对应权限，再重新登录跑；"
            "或者换一个高权限账号跑这批用例。"
        )
        return diag

    if code == _CODE_PARAM or "参数" in msg:
        diag.category = "服务端参数校验不通过（请求体/参数没填全）"
        diag.hint = (
            "服务端说必填参数缺失 —— 这是**用例数据问题**。"
            "把 Excel 里这条用例的请求体字段补全（或改成先查再改的动态数据）再跑。"
        )
        diag.attribution = CASE_DATA
        return diag

    if success is False:
        diag.category = f"服务端返回失败（{code or '无错误码'}）"
        diag.hint = "服务端明确返回了失败。拿这个 code 去查后端的错误码表。"
        return diag

    if success is True:
        # 服务端说成功，但断言挂了 —— 用例的预期和服务端实际行为对不上
        diag.attribution = CASE_DATA
        diag.category = "服务端返回成功，但和用例写死的预期不一致"
        diag.hint = (
            "服务端行为和用例预期不同。可能是接口语义变了（正常返回空列表也算成功），"
            "也可能是这条用例本来就是一条反向用例。确认后改用例的断言。"
        )
        return diag

    # ---- 3. 压根没拿到响应 ----
    if response is None and not code:
        diag.attribution = ENVIRONMENT
        diag.category = "没有拿到响应（连接失败或超时）"
        diag.hint = "请求没发出去或者没收到回包 —— 先确认服务地址通不通、网络有没有断。"
        return diag

    diag.category = "响应内容不符合断言"
    diag.hint = "看断言信息和响应体，判断是服务端行为变了还是用例预期过时。"
    return diag


def classify_exception(
    exc: BaseException,
    *,
    module: str = "",
    method: str = "",
    url: str = "",
    context: str = "",
    stage: str = "step",
) -> FailureDiagnosis:
    """框架自己抛异常时的归因（拿不到响应，只能按异常类型分）。

    Args:
        stage: 异常发生在哪个阶段 —— ``step`` / ``setup`` / ``teardown`` / ``login``。
            登录要单独判：登录失败抛的也是 ``RuntimeError``，但它跟
            "脚本写错了"完全不是一回事。
    """
    diag = FailureDiagnosis(
        attribution=SCRIPT,
        category=f"{context}抛出框架异常：{type(exc).__name__}" if context
        else f"框架异常：{type(exc).__name__}",
        module=module,
        method=method,
        url=url,
        assertion=str(exc),
    )
    diag.hint = (
        "这不是服务端的业务报错，是自动化脚本自己抛的异常"
        "（常见：变量取不到、请求体结构写错、断言类型不认识）。改脚本。"
    )

    if stage == "login":
        return _classify_login_failure(exc, diag)

    return diag


def _classify_login_failure(exc: BaseException, diag: FailureDiagnosis) -> FailureDiagnosis:
    """登录阶段的失败。

    **为什么不能沿用"脚本问题"**

    登录适配器拿到服务端回包 ``success=false`` 时抛的是 ``RuntimeError``，
    异常类型上看和 ``KeyError`` 一样"像脚本 bug"，但归因正好相反：

    - ``code=A00005`` —— 服务端空指针，**找后端**
    - ``code=A00014`` / 提示密码错误 —— 账号或密码配错了，**改用例配置**
    - 连不上 / 超时 —— 环境挂了，**看服务起没起**

    归成"改脚本"会把排查方向带偏：明明服务端登录接口炸了，
    却去翻自动化代码，白花时间。
    """
    text = str(exc)

    # 连接层面没通 —— 环境问题
    if isinstance(exc, (ConnectionError, TimeoutError)) or any(
        kw in text for kw in ("Connection", "Max retries", "timed out", "连接")
    ):
        diag.attribution = ENVIRONMENT
        diag.category = f"登录时连不上服务：{type(exc).__name__}"
        diag.hint = (
            "请求根本没发到服务端 —— 先确认服务地址通不通、服务起没起、"
            "代理有没有把请求拦掉（框架默认 trust_env=False，不受系统代理影响）。"
        )
        return diag

    code = _extract_code(text)
    if code:
        diag.server_code = code

    if code == _CODE_NPE:
        diag.attribution = SERVICE
        diag.category = "登录接口返回服务端内部异常（A00005）"
        diag.hint = (
            "服务端的登录接口自己抛了空指针 —— 不是账号密码的问题"
            "（密码错了会返回明确的提示）。找后端看登录接口的日志。"
        )
        return diag

    if code == _CODE_PARAM:
        diag.attribution = CASE_DATA
        diag.category = "登录接口参数校验不通过（A00014）"
        diag.hint = (
            "登录请求的字段没拼对（用户名 / 加密后的密码 / 验证码票据）。"
            "检查 config.yaml 里该项目的 login 配置，以及适配器的字段名。"
        )
        return diag

    if code:
        diag.attribution = SERVICE
        diag.category = f"登录接口返回失败（{code}）"
        diag.hint = "服务端明确拒绝了登录。拿这个 code 查后端错误码表，确认是账号问题还是接口问题。"
        return diag

    # 没有错误码：可能是账号密码不对，也可能是验证码没过
    if "验证码" in text or "captcha" in text.lower():
        diag.attribution = ENVIRONMENT
        diag.category = "登录时验证码没通过"
        diag.hint = (
            "滑块识别不是 100% 准（实测 20 次过 19 次）。先重跑一次；"
            "连续失败说明服务端的验证码接口变了，或者识别逻辑要调。"
        )
        return diag

    diag.attribution = ENVIRONMENT
    diag.category = f"登录失败：{type(exc).__name__}"
    diag.hint = (
        "登录没成功，但服务端没给错误码 —— 先手动用同一套账号登录一次，"
        "确认账号密码和验证码接口是否正常。"
    )
    return diag


def _extract_code(text: str) -> str:
    """从异常信息里抠服务端错误码（形如 ``code=A00005``）。"""
    import re

    match = re.search(r"code[=:]\s*([A-Za-z]\d{4,})", text)
    return match.group(1) if match else ""


def _as_text(value: Any) -> str:
    """把服务端返回值转成短文本。"""
    if value is None:
        return ""
    text = str(value).strip()
    return text


# --------------------------------------------------------------------------
# 归因落盘（并行执行时跨进程传递）
# --------------------------------------------------------------------------


def _failures_file():
    """归因记录的文件路径。"""
    from utils.paths import CACHE_DIR

    return CACHE_DIR / "failure_attribution.jsonl"


def _append_to_disk(record: dict[str, Any]) -> None:
    """把一条归因追加到文件（并行时 worker 各写各的）。

    **为什么追加也要加锁**：``unregister_failure``（重试成功后撤销）
    是"读全文 → 删掉一条 → 整份重写"。如果这时别的 worker 正在追加，
    追加的那一行会随着重写一起被抹掉 —— 终端汇总就少报一条失败。

    锁拿不到也照样写：丢一条归因比卡住用例强。
    """
    try:
        import json

        path = _failures_file()
        path.parent.mkdir(parents=True, exist_ok=True)

        def _write() -> None:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

        try:
            from utils.infra.parallel import file_lock
        except Exception:  # noqa: BLE001 - 拿不到锁就直接写
            _write()
            return

        with file_lock("failure_attribution.lock", timeout=10.0):
            _write()
    except OSError:
        pass


def _read_from_disk() -> list[dict[str, Any]]:
    """读磁盘上所有 worker 登记的归因。"""
    try:
        import json

        path = _failures_file()
        if not path.exists():
            return []

        records: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                records.append(record)
        return records
    except OSError:
        return []


# --------------------------------------------------------------------------
# 失败登记（给终端汇总用）
# --------------------------------------------------------------------------

# nodeid -> 归因记录（进程内）
_FAILURES: dict[str, dict[str, Any]] = {}


def register_failure(nodeid: str, module: str, diag: FailureDiagnosis) -> None:
    """登记一条失败（终端汇总按模块归类时用）。

    **为什么要写文件**

    并行执行（``pytest -n 4``）时每个 worker 是独立进程，内存不共享。
    worker 登记的归因主进程看不到 —— 终端汇总就只剩"失败 23 条"，
    没有模块和归因分布。所以这里同时写一份到磁盘，
    主进程汇总时把所有 worker 的记录一起读进来。
    """
    record = {"module": module, **diag.to_dict()}
    _FAILURES[nodeid] = record
    # nodeid 一起落盘 —— 重试成功时要能把这条撤掉（见 unregister_failure）
    _append_to_disk({"nodeid": nodeid, **record})


def unregister_failure(nodeid: str) -> None:
    """撤销一条失败登记（用例重试后通过了）。

    没有这一步的话，终端汇总的「失败用例归因」里会留着一条**已经通过**
    的用例 —— 报告说"失败 0 条"，归因却列了 3 条，看的人会以为漏了。

    磁盘记录也要删（并行时主进程读的是各 worker 写的文件）。
    """
    _FAILURES.pop(nodeid, None)

    import json

    path = _failures_file()
    if not path.exists():
        return

    try:
        from utils.infra.parallel import file_lock

        with file_lock("failure_attribution.lock", timeout=10.0):
            lines = path.read_text(encoding="utf-8").splitlines()
            kept: list[str] = []
            for line in lines:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue  # 坏行直接丢掉
                if isinstance(record, dict) and record.get("nodeid") == nodeid:
                    continue
                kept.append(line)
            if len(kept) != len(lines):
                path.write_text(
                    "\n".join(kept) + ("\n" if kept else ""),
                    encoding="utf-8",
                )
    except Exception:  # noqa: BLE001 - 撤销登记失败不该影响用例结论
        pass


def get_failures() -> list[dict[str, Any]]:
    """取所有登记过的失败（含各 worker 的）。"""
    records = list(_FAILURES.values())
    seen = {id(r) for r in records}
    for record in _read_from_disk():
        # 磁盘上的记录按 (模块, 接口, 归因) 去重
        key = (record.get("模块"), record.get("接口"), record.get("归因"))
        if any(
            (r.get("模块"), r.get("接口"), r.get("归因")) == key
            for r in records
        ):
            continue
        records.append(record)
    return records


def attribution_for(case_id: str) -> dict[str, Any] | None:
    """上一次运行里，这条用例被归因为「服务问题」吗。

    **给写类工具用的闸门**

    断言强度闸只能拦住"改松"，拦不住"把期望值从 ``00000`` 改成 ``A00005``" ——
    两边都是 ``==``，强度一模一样，用例却从红变绿了，而服务端的 bug 还在。
    这类失败唯一的正确处理是提缺陷，不是改用例。

    所以写用例的入口要问一句：这条用例上一轮为什么挂的？
    答"服务问题"就拒绝写入。

    Returns:
        命中服务问题时返回那条归因记录（含具体原因、服务端 code/msg），
        否则 ``None``（没失败过 / 归因为别的 / 没有编号）。
    """
    wanted = str(case_id or "").strip()
    if not wanted:
        return None

    for record in get_failures():
        if str(record.get("用例编号") or "").strip() != wanted:
            continue
        if str(record.get("归因") or "") == SERVICE:
            return record
    return None


def clear_failures() -> None:
    """清空登记（新 session 开始时调）。"""
    _FAILURES.clear()
    try:
        _failures_file().unlink(missing_ok=True)
    except OSError:
        pass


def summarize_by_module() -> list[tuple[str, int, dict[str, int]]]:
    """按模块汇总失败数 + 各归因的数量。

    Returns:
        ``[(模块, 失败条数, {归因: 条数}), ...]``，按失败数从多到少排。
    """
    buckets: dict[str, dict[str, int]] = {}
    for record in get_failures():
        module = str(record.get("模块") or "未分类")
        attribution = str(record.get("归因") or "未分类")
        counts = buckets.setdefault(module, {})
        counts[attribution] = counts.get(attribution, 0) + 1
    return sorted(
        ((m, sum(c.values()), c) for m, c in buckets.items()),
        key=lambda x: (-x[1], x[0]),
    )


def enrich_message(exc: BaseException, diag: FailureDiagnosis) -> BaseException:
    """把归因结论拼到异常信息最前面。

    报告里失败列表显示的就是 ``str(exc)`` —— 把结论放最前面，
    一眼就知道该找谁，不用点进附件翻。
    """
    prefix = f"{diag.verdict}｜"
    location = ""
    if diag.module:
        location = f"模块 {diag.module}"
    if diag.method and diag.url:
        location = (location + " " if location else "") + f"{diag.method} {diag.url}"
    elif diag.module:
        location = ""
    header = f"{prefix}{location}｜" if location else prefix

    message = str(exc)
    if message.startswith(prefix):
        return exc
    if diag.assertion and diag.assertion not in message:
        message = f"{message}（断言：{diag.assertion}）"
    exc.args = (f"{header}{message}", *exc.args[1:])
    return exc
