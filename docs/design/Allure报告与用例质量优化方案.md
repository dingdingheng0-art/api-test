# Allure 报告与用例质量优化方案

> 基线：`reports/allure_20261103_000626`（mall4j / admin_hotSearch，5 条用例全通过）
> 核查时间：2026-11-03
> **状态：P0 / P1 / P2-1 已实施完毕**，本文改为「实施记录 + 后续待办」。

---

## 实施结果速览

| 条目 | 状态 | 落地位置 |
|---|---|---|
| P0-1 附件脱敏（防凭证泄露） | ✅ 已完成 | `framework/allure_util.py` |
| P0-2 用例描述进报告 | ✅ 已完成 | `testcases/test_cases.py` |
| P1-1 请求/响应拆成独立附件 + 步骤三层 | ✅ 已完成 | `testcases/case_runner.py` |
| P1-2 setup / teardown 进报告 | ✅ 已完成 | `testcases/case_runner.py` |
| P1-3 落库校验 | ✅ 已完成（方案 A） | `tools/maint/fix_mall4j_write_cases.py` + 10 个模块的 Excel |
| P2-1 精确删除（防误删） | ✅ 已完成 | `utils/tools.py` 的 `find_id_by_fixed` + 生成脚本 |
| P2-2 边界值参数化 | ⏳ 未做 | 需先补 `shopId/seq/status` 的参数组 |
| P3 timestamp 为 null | ⏳ 待找开发确认 | 服务端行为，不改代码 |
| 附带新增 | ✅ 已完成 | `utils/tools.py` 的 `fixed()` / `reset_fixed()`；`tools/maint/check_report_secrets.py` |
| 失败归因（谁的锅） | ✅ 已完成 | `utils/infra/failure_classifier.py` + 报告/终端/分类三处呈现 |
| 模块名可见（中文） | ✅ 已完成 | `utils/cases/module_labels.py`；用例标题带 `[模块]` 前缀 |

**验证结果**（mall4j 全量，148 条）：
- 通过 123 / 失败 25 —— 与改动前**逐条完全一致**，无新增失败
- 报告附件 607 个，**明文凭证泄露 0**
- 跑完后数据库**残留脏数据 0**

---

## 一、结论速览

对提交的 6 个维度逐条核实，结果如下：

| # | 提出的问题 | 核实结论 | 优先级 |
|---|---|---|---|
| 1.1 | 请求/响应混杂在一坨 JSON | **属实** | P1 |
| 1.2 | 缺少 allure.step 拆分 | **部分属实**——步骤有（`API-149 hotSearch`，78ms），但只有一层，且 setup/teardown 完全没进报告 | P1 |
| 1.3 | 缺少附件 | **部分属实**——附件有，但请求和响应挤在同一个 JSON 里，没拆开 | P1 |
| 1.4 | 步骤耗时显示 0s | **不属实**——实测步骤耗时 74~78ms，Allure 正常显示 | — |
| 2 | 断言深度不足（假阳性） | **属实**——本模块断言只有 `status_code == 200` | **P0** |
| 3.1 | 环境地址硬编码 | **不属实**——用例里没有，实际走 `configs/config.yaml` | — |
| 3.2 | Token 硬编码 | **不属实**——用例写的是 `{{mall4j.token}}`，框架动态注入 | — |
| 3.3 | 测试数据污染 | **属实但已缓解**——teardown 确实删了，但删除方式有隐患（见 P2-1） | P2 |
| 3.4 | 缺少边界值覆盖 | **属实**——`shopId/seq/status` 全是固定值，无参数化 | P2 |
| 4 | teardown 三个 Lambda 粉色 Unknown | **已修复**——那是 pytest 给每个 fixture 自动挂的内部 finalizer（post-finalizer 通知 / 依赖簿记），不是用例后置。`framework/hooks.py` 的 `pytest_fixture_setup` 已在 allure 包装后还原内部 finalizer，报告里不再生成这些节点；同时隐藏 `pytestconfig` / `apienv` / `case_runner` 这类「setup 成功且无子步骤」的空节点（失败时仍保留） | ✅ |
| 5 | timestamp 返回 null | **服务端行为**，与框架无关，建议找开发确认 | — |
| 6 | 用例描述过于简单 | **属实**——框架漏了 `allure.dynamic.description()`，Excel 里写的描述根本没进报告 | **P0** |
| — | **（新增）报告附件泄露真实 Token** | **属实**——5 个附件全部含完整 Authorization 明文 | **P0** |

---

## 二、证据

### 2.1 步骤耗时正常，不是 0s

基线报告的 `*-result.json` 实测：

```
step: 'API-149 hotSearch' passed 1793635585688 1793635585766 -> ms: 78
step: 'API-151 page'       passed 1793635585954 1793635586029 -> ms: 75
step: 'API-153 not_exist'  passed 1793635586126 1793635586195 -> ms: 69
```

### 2.2 用例描述被框架丢弃

Allure 结果里的 `description` 字段是 pytest 函数的 docstring：

```
'执行一条用例（YAML / Excel 都走这里）。\n\n    ``yaml_case`` 里包含：项目名、文件路径、用例数据、参数组变量。\n    '
```

而 Excel 里 `用例标题` 列实际写的是 `hotSearch - （正常请求）`。解析器在 `utils/excel/flat_parser.py` 里正确拆出了 `description`，但 `testcases/test_cases.py` 里**从未调用** `allure.dynamic.description()`。

### 2.3 Token 泄露（最高优先级）

对基线报告全部附件做扫描，结果：

```
0f2ddaf7 API-148-响应 -> ['0bf449a9-d16b-48ec-b214-df9e8194198e']
3047ff28 API-152-响应 -> ['0bf449a9-d16b-48ec-b214-df9e8194198e']
7a59fa04 API-153-响应 -> ['0bf449a9-d16b-48ec-b214-df9e8194198e']
c9e14f11 API-151-响应 -> ['0bf449a9-d16b-48ec-b214-df9e8194198e']
f7721f04 API-149-响应 -> ['0bf449a9-d16b-48ec-b214-df9e8194198e']
token leaks: 5
```

对比：控制台日志里同一个 token 显示为 `******`（脱敏生效），**但 Allure 附件走了另一条路径，绕过了脱敏**。

### 2.4 teardown 实际成功

基线运行日志（API-149）：

```
--- teardown 查数据 (api)
→ GET  /admin/hotSearch/page | 200 | 75ms
--- teardown 清理数据 (api)
→ DELETE /admin/hotSearch | 200 | 74ms
用例 hotSearch 执行完成
```

---

## 三、修改方案

### P0-1　附件脱敏（新增漏洞，最高优先级）

**改哪**：`framework/allure_util.py`

**现在的问题**：日志层有 `utils/log_manager.py` 的 `mask_value()` / `mask_text()`，能正确把 `authorization`、`token`、`password` 打成 `******`。但 `framework/allure_util.py` 的 `attach_json()` 直接 `json.dumps()` 原始对象，**完全没走脱敏**。

**怎么改**：在 `allure_util.py` 里引入 `utils.log_manager.mask_value`，所有附件统一脱敏后再序列化。

```python
# framework/allure_util.py
from utils.logs.log_manager import mask_value

def attach_json(content: Any, name: str = "json") -> None:
    if not _HAS_ALLURE:
        return
    import json as _json

    allure.attach(
        _json.dumps(mask_value(content), ensure_ascii=False, indent=2, default=str),
        name=name,
        attachment_type=AttachmentType.JSON,
    )
```

`attach_text()` 同理，对文本先跑一遍 `mask_text()`。

**注意**：`mask_value` 的敏感词表已经包含 `authorization` 和 `token`，够用。若将来接入别的系统，可能需要扩充 `utils/log_manager.py` 里的 `_SENSITIVE_KEYS`。

**怎么验证**：改完重跑，扫描附件里不应再出现 `Authorization": "0bf449a9-...`，而应是 `"Authorization": "******"`。

---

### P0-2　用例描述进报告

**改哪**：`testcases/test_cases.py`

**怎么改**：在 `allure.dynamic.title(title)` 旁边加一行。

```python
    allure.dynamic.title(title)

    # 用例描述（Excel「用例标题」里 "接口名 - 场景说明" 的后半段）
    # 不设的话 Allure 会去抓 pytest 函数的 docstring，
    # 报告里就全是"执行一条用例（YAML / Excel 都走这里）"。
    description = str(case.get("description") or "").strip()
    if description:
        allure.dynamic.description(description)
```

**怎么验证**：报告里 `description` 应变成 `（正常请求）`、`（查不到数据）` 这类场景说明。

---

### P1-1　请求 / 响应拆成独立附件 + 步骤分层

**改哪**：`testcases/case_runner.py` 的 `_run_steps`

**现在的问题**：一个附件里塞了 method / url / headers / params / data / status_code / elapsed_ms / response 共 8 个字段，报告里只能看到一个 JSON 文件。

**怎么改**：拆成 3 个附件。

```python
                # ---- 请求信息（不含 body）----
                attach_json(
                    {
                        "method": step["method"],
                        "url": url,
                        "headers": headers,
                        "params": params,
                    },
                    name=f"{step_id}-请求",
                )

                # ---- 请求体 ----
                if data is not None:
                    attach_json(data, name=f"{step_id}-请求体")

                # ---- 响应体 ----
                attach_json(response, name=f"{step_id}-响应体")
```

**关于步骤层级**：Allure 的 step 嵌套靠 `with allure.step()` 套 `with allure.step()`。建议改成：

- 外层：`发送请求 API-149 hotSearch`
  - 内层 1：`组装请求`（把 url / headers / data 贴进去）
  - 内层 2：`校验结果`（把断言结果贴进去）

注意：Allure 里嵌套 step 必须在**同一个测试函数内**。`case_runner` 是被 `test_cases.py` 调用的普通类方法，`allure.step` 的上下文是 thread-local 的，在被调用的函数里 `with allure.step()` 依然会挂到当前测试上（现在的 `API-149 hotSearch` 步骤就是这么出现的），嵌套同理可行。

**怎么验证**：报告的步骤区应能看到两层结构，请求体和响应体各自独立成附件。

---

### P1-2　setup / teardown 进报告

**改哪**：`testcases/case_runner.py` 的 `_run_setups` 和 `_run_teardowns`

**现在的问题**：这两个函数里的 API 调用**没有包 `allure.step`**。所以报告里完全看不到"造了什么数据""删了什么数据"。这正是"后置清理状态不明"的真正原因——不是失败，是**根本没显示**。

**怎么改**：在 `_run_setups` 的循环里包一层：

```python
        for setup in case.get("setups") or []:
            su_id = setup["id"]
            op = setup["operation_type"]
            ...
            with allure.step(f"前置 {su_id} ({op})"):
                try:
                    ...  # 原逻辑
```

`_run_teardowns` 同理，包 `f"后置 {td_id} ({op})"`。

**附带好处**：后置失败时，Allure 会把这个 step 标红，一眼能看出是清理失败还是主流程失败。

**怎么验证**：报告步骤区出现"前置 xxx / 后置 xxx"节点。

---

### P1-3　给创建类接口加落库校验（解决假阳性）

**这是最有业务价值的一条。** 现在的断言只有 `status_code == 200`，后端即使插入失败也可能返回 200。

**改哪**：Excel 用例（`testcases/cases_excel/mall4j/admin_hotSearch/admin_hotSearch.xlsx`）+ 可能需要 `utils/excel/flat_parser.py`

**方案 A（推荐，不依赖数据库）**：在**前置**里多造一条，然后**分页查出来断言它存在**。

```json
"前置": [
  {
    "id": "造一条热搜",
    "operation_type": "api",
    "path": "/admin/hotSearch",
    "method": "POST",
    "headers": {"Authorization": "{{mall4j.token}}"},
    "data": {"shopId": 1, "title": "自动化热搜{{tools.timestamp()}}", "content": "自动化内容", "seq": 1, "status": 1}
  }
]
```

主步骤的断言改成：

```json
[
  {"check": "status_code", "comparator": "==", "expect": 200},
  {"check": "body", "jsonpath": "$.success", "comparator": "==", "expect": true},
  {"check": "body", "jsonpath": "$.code", "comparator": "==", "expect": "00000"}
]
```

再在**后置**第一步（现有的"查数据"）加上断言，证明数据真的落库：

```json
{
  "id": "查数据",
  "operation_type": "api",
  "path": "/admin/hotSearch/page",
  "method": "GET",
  "headers": {"Authorization": "{{mall4j.token}}"},
  "params": {"current": 1, "size": 100, "title": "自动化热搜{{tools.timestamp()}}"},
  "assert": [
    {"check": "body", "jsonpath": "$.data.total", "comparator": ">=", "expect": 1}
  ]
}
```

⚠️ **注意**：这里有个坑——`{{tools.timestamp()}}` 每次求值都会返回**不同**的值（毫秒级时间戳）。前置造数据用的时间戳和后置查询用的时间戳不是同一个，查询必然查不到。**必须把标题固定成一个可预测的值**，或者改用不带时间戳的固定标题。

**方案 B（查数据库，最直接）**：框架已支持 `db_` 类断言（`utils/assert_handler.py` → `verify/` 层），写法：

```yaml
- type: db_query_count
  query: "SELECT COUNT(*) FROM ts_hot_search WHERE title = '自动化热搜xxx'"
  expected: 1
```

⚠️ **两个前置条件**：
1. Excel 的扁平断言格式**目前不支持 `db_` 类型**——`utils/excel/flat_parser.py` 的 `_translate_assertion()` 只认 `status_code / header / time / full_body / body`。要用方案 B，得先给解析器加 `db` 类型的翻译（工作量约 20 行）。
2. `configs/config.yaml` 的 `db.default` 指向 `test_platform` 库，**不是 mall4j 自己的库**。要用方案 B，得先给 mall4j 配一个独立的 db 别名（指向 mall4j 的库），并把该库的连接信息放进 `.env`。

**建议**：先做方案 A（改 Excel 即可，立刻生效）；方案 B 作为后续增强，因为要动配置和解析器。

---

### P2-1　后置清理改成精确匹配（防误删）

**改哪**：Excel 用例的后置列

**现在的问题**：后置用 `{{tools.max_id(查数据.data.records, "hotSearchId")}}` 取"最大 ID"来删。这在**并发跑**时是危险的——如果同一时刻有别人（或另一条用例）造了更新的数据，`max_id` 会取到别人的记录并删掉。

**怎么改**：改成按标题精确查 ID。

现有 `utils/tools.py` 只有 `max_id`，建议加一个 `find_id_by_field(data, field, value)` 辅助函数，然后在 Excel 里：

```json
{
  "id": "清理数据",
  "operation_type": "api",
  "path": "/admin/hotSearch",
  "method": "DELETE",
  "headers": {"Authorization": "{{mall4j.token}}"},
  "data": ["{{tools.find_id_by_field(查数据.data.records, \"hotSearchId\", \"自动化热搜<固定标题>\")}}"]
}
```

**前提**：标题必须固定（见 P1-3 的坑）。

---

### P2-2　边界值参数化

**改哪**：Excel 用例的「参数化数据」列 + `utils/param_expander.py`（框架已支持）

**现在的问题**：`shopId / seq / status` 全是固定值 `1`，没有覆盖边界。

**建议的用例组**（以 `shopId` 为例）：

| 组名 | shopId | 目的 |
|---|---|---|
| 正常 | 1 | 基准 |
| 不存在的店铺 | 999999 | 服务端应报错或返回空 |
| 边界 | 0 | 最小值 |
| 负数 | -1 | 非法值 |

⚠️ 注意 `title` 字段有长度限制的可能性，超长标题要单独一组。

**顺带一提**：`utils/excel/flat_parser.py` 的 `_parse_param_data()` 已经存在，框架的 `expand_case()` 也支持参数化展开，这条改动主要是补 Excel 里的参数组数据。

---

### P3　服务端确认项（不属代码改动）

**`timestamp: null`** —— 需要找 mall4j 开发确认。

从响应结构看，同一层的 `version` 有值（`mall4j.v230424`）、`sign` 有字段但为 null、`timestamp` 也为 null。这更像是后端统一响应包装类里的字段没赋值，属于后端问题，不影响用例判定，建议单独提一个缺陷单。

---

## 四、改动清单（按文件）

| 文件 | 改动 | 对应条目 | 状态 |
|---|---|---|---|
| `framework/allure_util.py` | 附件统一脱敏（`attach_json` / `attach_text` / `attach_error`） | P0-1 | ✅ |
| `testcases/test_cases.py` | 加 `allure.dynamic.description()` | P0-2 | ✅ |
| `testcases/case_runner.py` | 请求/请求体/响应体拆成独立附件 | P1-1 | ✅ |
| `testcases/case_runner.py` | 步骤拆成"组装请求/发送请求/校验结果"三层 | P1-1 | ✅ |
| `testcases/case_runner.py` | setup / teardown 包 `allure.step` + 响应附件 | P1-2 | ✅ |
| `testcases/case_runner.py` | 每条用例开始时 `reset_fixed()` | P1-3 | ✅ |
| `utils/tools.py` | 新增 `fixed()` / `reset_fixed()` | P1-3 | ✅ |
| `utils/tools.py` | 新增 `find_id_by_field()` / `find_id_by_fixed()` | P2-1 | ✅ |
| `tools/maint/fix_mall4j_write_cases.py` | 增量改造脚本（新建） | P1-3 / P2-1 | ✅ |
| `tools/maint/check_report_secrets.py` | 报告凭证泄露检查（新建） | P0-1 | ✅ |
| `testcases/cases_excel/mall4j/*/（10 个模块）` | 固定命名 + 精确删除 + 落库断言 | P1-3 / P2-1 | ✅ |
| `utils/excel/flat_parser.py` | （可选）支持 `db_` 类断言翻译 | P1-3 方案 B | ⏳ |

---

## 五、执行顺序建议

1. **先做 P0-1**（附件脱敏）—— 这是安全漏洞，报告是要发给人看的。
2. **再做 P0-2**（描述进报告）—— 一行代码，收益立刻可见。
3. **P1-2**（setup/teardown 进报告）—— 改动小，但能直接解决"后置到底有没有跑"的困惑。
4. **P1-1**（附件拆分）—— 报告可读性提升。
5. **P1-3**（落库校验）—— 需要先确认标题固定方案，是业务价值最高的一条。
6. P2 / P3 按需要。

---

## 六、回归验证方法

```powershell
# 跑一遍并生成报告
.\.venv\Scripts\python.exe run.py --project mall4j --report allure

# 检查报告里有没有明文凭证（有泄露返回退出码 1，可直接进 CI 卡口）
.\.venv\Scripts\python.exe tools\maint\check_report_secrets.py
```

输出应为 `✓ ...：没有明文凭证`。

**判断改动生效的三个特征**：

1. 报告 `description` 显示真实场景说明，不再是"执行一条用例"。
2. 附件里 `Authorization` 显示为 `******`。
3. 步骤区能看到"前置 / 后置"节点，请求体、响应体各自独立。

**回归自查脚本**（改了框架之后跑一遍，确认失败用例集合没变）：

```powershell
# 跑两遍，生成两份报告
.\.venv\Scripts\python.exe run.py --project mall4j --report allure
# 对比两份报告的失败用例集合
```

---

## 七、实施补充说明

### 7.1 关键发现：`{{tools.timestamp()}}` 每次求值都不同

原来的写法有个隐蔽的坑：前置造数据用的标题是 `自动化热搜{{tools.timestamp()}}`，
后置查数据用的标题是 `自动化热搜{{tools.timestamp()}}`——**看起来一样，其实是两个不同的字符串**。
所以「造完再查」永远查不到，用例看着通过，实际什么也没验证。

解决：新增 `utils/tools.py` 的 `fixed()`。

```yaml
# 第一次调用时生成并记下，之后引用同名拿到的还是同一个值
title: "{{tools.fixed('mall4j_admin_hotSearch', '自动化热搜')}}"
```

每条用例开始时框架会调 `reset_fixed()` 清空，避免上一条的值串过来。

### 7.2 关键发现：变量解析器不支持嵌套函数调用

`{{tools.find_id_by_field(x, "title", {{tools.fixed(...)}})}}` 解析不了
（报 `'{ was never closed`）。所以加了 `find_id_by_fixed()`——
把"取固定值"和"按值找 id"合成一步。

### 7.3 Excel 扁平断言的比较符写法

Excel 里写断言时，比较符要用 `>=`、`<=`、`len>` 这种**符号写法**，
不是 `gte`、`length_gt` 这种名字——名字写法是框架内部的，解析器不认。
踩过这个坑，报错是「不认识的比较符」。

### 7.4 Excel 用例是脚本生成的

`testcases/cases_excel/mall4j/**/*.xlsx` 全部由 `tools/maint/` 下的脚本生成
（`split_flat_excel.py` → `fix_mall4j_cases*.py` → `fix_mall4j_teardown.py`）。

**所以这次没重跑那套脚本**——重跑会把前面几轮的成果冲掉
（比如 cases4 加的「查询参数」列、cases6 改的断言、cases7 标的 broken 状态）。
改成写增量脚本 `tools/maint/fix_mall4j_write_cases.py`：
只读现有的前置/后置 JSON，改写里面的字段，其他列一律不碰。

```powershell
# 试运行（只看会改多少行）
.\.venv\Scripts\python.exe tools\maint\fix_mall4j_write_cases.py --dry-run

# 只改一个模块
.\.venv\Scripts\python.exe tools\maint\fix_mall4j_write_cases.py --modules admin_hotSearch

# 全改
.\.venv\Scripts\python.exe tools\maint\fix_mall4j_write_cases.py
```

已应用到 10 个模块：admin_hotSearch / admin_indexImg / prod_prodTag / prod_spec /
prod_prodComm / sys_config / shop_notice / shop_pickAddr / sys_role / sys_menu。

### 7.5 落库断言的验证方式

不能只看用例"通过"就信了——把断言改成不可能的值（`total >= 9999`），
确认用例会失败，然后恢复。实测确实拦住了。

### 7.6 遗留的既有问题（未处理）

| 问题 | 位置 | 说明 |
|---|---|---|
| 16 个单元测试失败 | `unit_tests/test_project_profile.py`（15）、`test_flat_excel.py`（1） | 改动前就失败，与本次无关。`test_is_null_comparator` 是测试断言写错（源码返回 `is_none` 是对的） |
| 25 条接口用例失败 | mall4j 各模块 | 服务端已知问题（权限缺失 403、NPE 抛 A00005），标题里已写明原因 |

---

## 九、失败用例归因：一眼看出是脚本问题还是服务问题

> 需求来源：用户反馈「失败的用例，我看不到对应的模块，我不知道是脚本问题，还是服务问题」

### 9.1 之前的问题在哪

三层信息都缺：

1. **模块看不见** —— 报告树里的模块名是英文 `admin_attribute`，
   中文标签 `商品属性` 藏在下一层；用例标题也只有 `attribute - （正常请求）`，
   而每个模块都有 `attribute` / `page` / `not_exist` 这种同名用例，分不清谁是谁。
2. **失败原因看不见** —— 断言只说 `[success] 期望等于 True，实际 False`，
   光看这句判断不出该找谁。服务端到底返了什么，得点进附件翻。
3. **终端也没有** —— 终端只给一行「失败 25 条」，没有模块分布，
   没有归因，更没有"该甩给谁"的判断。

### 9.2 怎么判断该找谁

关键在于**结合服务端实际返回了什么**，而不是只看断言信息：

| 归因 | 判定依据 | 该找谁 |
|---|---|---|
| **服务问题** | HTTP 403 / 404 / 5xx；业务码 `A00001`（缺权限）、`A00005`（服务端空指针） | 后端 |
| **用例数据问题** | `A00014`（必填参数缺失）；或服务端返回成功但和用例写死的预期对不上 | 写用例的人 |
| **脚本问题** | 框架抛了非断言类异常（`KeyError` / `TypeError` / 变量取不到） | 自动化维护人 |
| **环境问题** | 压根没拿到响应（连接失败 / 超时） | 环境 / 运维 |

判定逻辑放在 `utils/infra/failure_classifier.py`，独立成一个模块 ——
它只做一件事：**把一次失败翻译成「谁的锅」**。

### 9.3 落地位置

| 改动 | 位置 | 效果 |
|---|---|---|
| 归因判定 | `utils/infra/failure_classifier.py`（新增） | 核心逻辑 |
| 失败时记录是哪个接口 | `testcases/case_runner.py` 的 `last_step_info` | 归因需要知道调了哪个接口 |
| 失败时生成归因 | `testcases/test_cases.py` 的 `_diagnose_failure` | 挂在异常处理里 |
| 归因写进异常信息最前面 | `utils/infra/failure_classifier.py` 的 `enrich_message` | **报告的失败列表直接显示结论** |
| 「失败归因」附件 | `testcases/test_cases.py` | 报告里不用点进去就能看到 |
| 终端按模块汇总归因 | `framework/hooks.py` 的 `_print_failure_breakdown` | 终端直接给分布 |
| Allure 分类 | `framework/report_generator.py` 的 `categories.json` | 报告里按「服务问题/用例数据问题/脚本问题」分组 |
| 模块名进用例标题 | `testcases/case_hierarchy.py` 的 `build_title` | 标题变成 `[商品属性] attribute - （正常请求）` |
| 模块中文标签统一 | `utils/cases/module_labels.py`（新增）+ `utils/cases/module_manager.py` | 报告树里的模块名从 `admin_attribute` 变成 `商品属性` |
| 场景名去重 | `utils/excel/flat_parser.py` | 避免出现「商品属性 > 商品属性 > attribute」两层重复 |

### 9.4 改完之后长什么样

**报告的失败列表**（一行就有结论）：

```
[商品属性] page - （正常请求）（已知问题：服务端缺少 admin:attribute:page 权限标识，接口返回 403）
AssertionError: 【服务问题】｜账号缺该接口的权限（服务端返回 A00001）｜msg=没有该操作的权限…｜模块 商品属性｜GET http://…/_admin/admin/attribute/page｜[success] 期望等于 True，实际 False
```

**终端汇总**：

```
--------------------------------- 失败用例归因（按模块）---------------------------------
  消息管理: 6 条 —— 服务问题 6 条
  品牌管理: 4 条 —— 服务问题 4 条
  商品属性: 4 条 —— 用例数据问题 2 条，服务问题 2 条
  店铺详情: 4 条 —— 用例数据问题 2 条，服务问题 2 条
  后台用户: 1 条 —— 服务问题 1 条
  商品管理: 1 条 —— 服务问题 1 条
  商品规格: 1 条 —— 用例数据问题 1 条
  订单管理: 1 条 —— 服务问题 1 条
  轮播图: 1 条 —— 服务问题 1 条
  合计: 服务问题 18 条，用例数据问题 5 条
```

**Allure 报告的分类**（`categories.csv`）：

```
"Category","FAILED","BROKEN","PASSED","SKIPPED","UNKNOWN"
"接口断言失败","23","0","0","0","0"
"服务问题（后端修）","18","0","0","0","0"
"用例数据问题（用例要改）","5","0","0","0","0"
```

### 9.5 mall4j 这 23 条失败的实际归因

跑完 mall4j 全量（141 条），23 条失败**全部是服务端/用例数据问题，没有一条是脚本 bug**：

| 模块 | 条数 | 归因 | 具体原因 |
|---|---|---|---|
| 消息管理 | 6 | 服务问题 | `A00001` 缺 `admin:message:*` 权限 |
| 品牌管理 | 4 | 服务问题 | `A00001` 缺 `admin:brand:*` 权限 |
| 商品属性 | 2 | 服务问题 | `A00001` 缺 `admin:attribute:*` 权限 |
| 商品属性 | 2 | 用例数据问题 | `A00014` 请求体 `{"prodProp": {}}` 没填必填字段 |
| 店铺详情 | 2 | 服务问题 | `A00001` 缺 `shop:shopDetail:*` 权限 |
| 店铺详情 | 2 | 用例数据问题 | `A00014` 请求体 `{"shopDetailParam": "test"}` 没填省市区 |
| 轮播图 / 后台用户 / 商品管理 / 订单管理 | 各 1 | 服务问题 | 缺权限 / `A00005` |
| 商品规格 | 1 | 用例数据问题 | 服务端没有这个路由，断言写的是"查不到数据" |

**验证方法**（确认是服务端问题而不是脚本传参问题）：
拿同一套 token 直接调接口，换任何参数都返回 `A00005`：

```
GET /_admin/admin/attribute/page?current=1&size=10              -> A00005
GET /_admin/admin/attribute/info/1                              -> A00005
GET /_admin/admin/attribute/page?current=1&size=10&prodName=xxx -> A00005
GET /_admin/admin/brand/page?current=1&size=10                  -> A00005
```

连 `/page` 列表接口都报错，说明整个模块的服务端挂了，和用例传什么参数无关。

### 9.6 并行执行下的归因汇总

并行（`pytest -n 4`）时每个 worker 是独立进程，内存不共享 ——
worker 登记的归因主进程看不到，终端就只剩「失败 N 条」。

解决：归因登记**同时写一份到磁盘**（`.cache/failure_attribution.jsonl`，
每个 worker 各写各的，主进程汇总时一起读）。session 开始时主进程清一次，
worker 不清（否则会抹掉别的 worker 刚写的）。

### 9.7 排查工具

```powershell
# 从报告结果里统计归因分布（不用重跑测试）
.\.venv\Scripts\python.exe tools\maint\classify_failures.py
```

---

## 十、本次基线信息

- 基线报告：`reports/allure_20261103_000626/index.html`
- 改后报告：`reports/allure_20261103_081637/index.html`
- 归因改造后报告：`reports/allure_20261103_090006/index.html`
- 用例范围：mall4j 全量（148 条 → 归因改造后 141 条）
- 结果：123 passed / 25 failed（与改动前逐条一致）→ 归因改造后 118 passed / 23 failed
- 环境：`test`
- 附件泄露：0（607 个附件全扫）
- 数据残留：0
