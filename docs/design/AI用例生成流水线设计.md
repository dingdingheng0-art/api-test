# AI 用例生成流水线设计

> 2026-11-08。本文是 ai 模块的架构约定，后续改动以本文为准。

## 1. 背景

现有能力是**散件**：文档导入、知识图谱、用例生成、执行、修复、覆盖率各自可用，
但没有串成一条流水线，且 AI 生成器不读框架规则，产物不规范。

目标是把 10 个环节收拢成一条**有闸门、可重跑、可回退**的流水线。

## 2. 端到端流水线

```
① 文档摄入 → ② 环境探针 → ③ 知识图谱 → ④ 依赖规划 → ⑤ 用例生成
   → ⑥ 数据构造 → ⑦ 断言生成 → ⑧ 执行报告 → ⑨ 失败修复 → ⑩ 覆盖率
```

| # | 阶段 | 输入 | 输出 | 复用 / 新增 |
|---|---|---|---|---|
| ① | 文档摄入 | 文件 / 上传 | `Endpoint[]` | 复用 `tools.importer.dispatcher.parse_file` |
| ② | 环境探针 | `config.yaml` + `.env` | `EnvProfile`（服务可用、DB 只读/可写、登录态） | 新增 `ai/pipeline/probe.py` |
| ③ | 知识图谱 | endpoints + 表结构 + 现役用例 | `graph.db` | 复用 `ai/knowledge/builder.build` |
| ④ | 依赖规划 | endpoints + 图谱 | `CasePlan`（调用顺序、参数来源） | 新增 `ai/pipeline/dependency.py` |
| ⑤ | 用例生成 | CasePlan + 规则包 | `draft` 用例 | 复用/改造 `ai/capabilities/case_gen.py` |
| ⑥ | 数据构造 | 用例 + 依赖图 | setups / teardowns | 新增 `ai/pipeline/data_orchestrator.py` |
| ⑦ | 断言生成 | 用例 + 图谱映射 | 字段断言 + DB 断言 | 新增 `ai/pipeline/assert_builder.py` |
| ⑧ | 执行报告 | 用例 | pytest / allure / 汇总 | 复用 `run_cases`、`report_generator` |
| ⑨ | 失败修复 | 失败结果 | 归因 + 修复 + 验证 | 复用 `diagnose`、`plan_heal`、`apply/verify_heal` |
| ⑩ | 覆盖率 | 用例 + endpoints + 图谱 | 四维覆盖报告 | 扩展现有 `get_coverage` |

编排入口：`python -m ai.pipeline <stage|all>`，阶段产物落 `.cache/pipeline/<run_id>/`。

## 3. 关键设计

### 3.0 测试设计优先（本轮最重要的修正）

**旧范式**：一个接口一段 prompt，让模型决定测什么 → 模型只出正例，
通过率很高但覆盖极浅。7/7 全绿就是证据。

**新范式**：确定性枚举测点，模型只负责填数据和断言。

```
接口元数据 + 知识图谱 + 项目契约
        ↓
ai/pipeline/intents.py：按维度矩阵枚举 TestIntent
        ↓
case_gen：模型逐条把 intent 填成请求 + 断言
        ↓
dimension_coverage：检查每个接口的应测维度是否覆盖
```

维度定义见 `.claude/skills/apitest-design/SKILL.md`，机器可读版见
`ai/rules/case_rules.yaml`。覆盖 A 参数契约 / B 业务语义 / C 权限 /
D 数据一致性 / E 接口关联 / F 异常健壮性六类。

验收口径随之改变：**不再以通过率为唯一指标**，改为
「应测维度覆盖率 + 有效断言密度 + 场景链完整度」，通过率只代表稳定性。

### 3.1 规则注入（解决"没有 skill 规则限制"）

- `ai/rules/case_rules.yaml`：把 `apitest-author` 的散文规则变成机器规则。
- `ai/rules/validator.py`（`check_case` / `enforce` / `format_hint`）：
  写入口逐条校验；现役模块违规直接拒绝，沙箱 `ai_*` 放行但强制标
  `needs_manual_setup` 并返回违规清单。同一道闸接在 `create_case` /
  `apply_assertion_change` / `set_case_status(debugged)` 上。
- 硬规则（不可绕过）：
  - 每步必须有 `assert`；正例至少 2 个业务断言（`status_code` / `code` /
    `success` 不算）
  - 反例必须断言「被拒绝」，只发请求不算验证
  - 显式声明 D1/D2/D3 必须有 `db_query` 断言，E3 必须有清理
  - **正例**写操作必须有 setups + teardowns，或 DELETE 自清理；
    teardown 只删自有数据
  - 路径参数不能留着 `{id}` 发请求（`{{...}}` 框架变量除外）
  - DB 断言只在主步骤；映射置信度 < 0.9 不自动生成 DB 断言
  - 路径参数必须替换为真实值
- `case_gen` 生成时把规则包注入 system prompt，`_finalize_ai_case` 再做确定性
  收敛（补业务断言、挂造数链路、剥不可追溯的 teardown）；落盘时由
  `create_case` 里的 `validator.enforce` 逐条兜底 —— 现役模块不达标直接
  拒绝落盘，沙箱 `ai_*` 降级为 `needs_manual_setup`。

### 3.2 接口关联与数据依赖

- 图谱已有 `param → table.column`；补 `table.column → producer endpoint` 边。
- `dependency.py` 输出有向图：
  `POST /prod/category → id → POST /prod/prod.categoryId → id → GET /prod/prod/info/{id}`。
- 解析不出依赖时显式标记，**禁止编造 id**。
- 同一模块内可复用现役用例里的真实路径参数值作为种子。

### 3.3 数据构造（已落地：只走 API）

- 写操作统一用被测 API 造数，产出 id 通过 `{{步骤.id}}` 引用。
- teardown 只删本用例造的数据；固定 id 数据只读。
- 造数失败 → 用例标 `needs_manual_setup`，不进 CI。
- 不做 DB 直插（mall4j 账号只读）。

实现：`ai/pipeline/data_orchestrator.py`（纯规划器，不发请求、不写文件）。
它从**完整接口清单**里找同一资源集合路径上的兄弟接口：

| 角色 | 选择规则 |
|---|---|
| 生产者 | `POST <集合路径>` |
| 查数 | `GET <集合>/list`（`data` 数组）优先，其次 `GET <集合>/page`（`data.records`） |
| 清理 | `DELETE <集合>/{id}`（路径式）优先，其次 `DELETE <集合>`（body 数组式） |

字段从查数接口的 `response_example` 推断：主键 `*Id`/`id`，名称 `*Name`/`title`。
造数请求体优先取**现役正例**（文档示例里的 `parentId: 0` / `level: 0` 实测会被
服务端拒绝），名称改成 `{{tools.fixed('<项目>_<模块>_<字段>_seed', 'AI造数')}}`，
再用 `{{tools.find_id_by_fixed(查数据.<list_path>, "<名称字段>", "<key>", "<id字段>", "AI造数")}}`
把服务端生成的 id 查回来。三种主步骤的链路：

- **POST 正例**：主步骤即造数 → teardown 查数 + 删除
- **PUT/PATCH 正例**：setup 造数 + 查数 → 主步骤用查到的 id 改 → teardown 删除
- **DELETE 正例**：setup 造数 + 查数 → 主步骤路径换成查到的 id（主步骤自己删掉种子，不需要 teardown）

安全边界：

- **只给正例造数**。反例/边界/权限用例可能根本没写进数据，无条件清理会把
  正确的拒绝变成 teardown 失败，所以它们继续标 `needs_manual_setup`。
- 清理只删本用例造的种子：DELETE 路径里必须引用查数步骤的输出。
- 给不出生产者/查数/清理，或请求体形状不明 → 返回 `None`，调用方继续标人工，
  绝不发「半条」链路。
- 带 `api_data_chain` 标记的用例会被强制写进 `ai_<模块>` 沙箱。
- 前置/后置断言在落盘时统一转成 Excel 扁平格式（`create_case` 里的
  `_prepost_to_flat`），否则 `flat_parser` 读回来会报「不认识的断言类型」。


### 3.4 断言生成

- 字段断言：响应示例/schema + 现役用例契约（成功码、success、关键字段）。
- DB 断言：仅使用置信度 ≥ 0.9 的映射；放主步骤，不放 teardown。
- 写操作追加"造数后能查到 / 清理后查不到"的落库断言。

### 3.5 执行与报告

- 执行仍由 pytest + 框架完成，AI 负责编排、归因、汇总。
- 遵守 `run_cases` 边界：单次 ≤ 50 条、单会话累计 ≤ 100 条；全量前先报代价。

### 3.6 失败修复

- 复用既有闭环：`diagnose → plan_heal → propose/apply → verify`。
- 归因闸门：环境问题只重跑；服务问题只提缺陷；用例数据问题才允许改。
- 断言只能变强，不允许删断言或放宽比较符。

### 3.7 覆盖率

从「接口级」扩到四维：

1. 接口覆盖：有没有用例
2. 断言覆盖：有没有字段断言（不是只有 status_code）
3. 落库覆盖：写接口有没有 DB 断言
4. 关联覆盖：依赖链是否完整、是否有未解析的参数来源

**已落地：维度覆盖闸门** `ai/pipeline/dimension_coverage.py`

```bash
python -m ai.pipeline.dimension_coverage \
  --project mall4j --module ai_admin_area \
  --file .cache/apidoc/openapi.json --only-path /admin/area
```

口径（这几点决定了报告能不能信）：

- **应测维度**由 `intents.plan_endpoint_intents` 确定性枚举，不看用例
- **实际覆盖**必须被用例内容证实，**不认标签声明**：
  - 标了 `C1` 但请求带鉴权头 → 不算（除非显式标 `no_auth`）
  - 标了 `C1`/`no_auth` 但没断言「被拒绝」→ 不算（发请求 ≠ 验证权限）
  - 标了 `D1/D2/D3` 但没有 `db_query` 断言 → 不算
  - 标了 `E3` 但没有 `teardown` → 不算
  - 标了 `A*` / `F2..F4` 但没有任何「期望失败」断言 → 不算
  - `F1` 没有断言「查不到/为空」→ 不算（成功 + data 为空也算验证）
  - 正例没有成功语义断言 → 不算
- 未证实的声明单独列「标称未证实」，**不计入分子**
- 业务断言 < 2 的列为「薄用例」
- 维度标签统一写 `dim:A1`；裸 `p0` 是优先级，不是维度

**配合的框架改造**：`no_auth` 标签。

以前框架在用例没写鉴权头时自动补 token，导致「未登录」用例实际带着 token 跑。
现在 `CaseRunner._default_headers(skip_auth=True)` 与 `create_case`
两处都认 `no_auth`，主步骤真正匿名（前置/后置仍正常鉴权）。

### 3.8 探针驱动（实测优先于猜测）

维度矩阵保证「该测什么不遗漏」，但保证不了「接口实际怎么表现」。
没有实测时，生成器只能猜：未登录是 401 还是 HTTP 200 + 业务错误码？
少传必填参数真的会被拒吗？不存在的 id 是报错还是返回空？

猜的代价就是大批 `needs_manual_setup` —— 智能体没解决问题，只是把问题贴了个标签。

**做法**：生成前先对真实环境按测点逐个发请求（`ai/pipeline/probe.py`），
把实测结果喂回生成器（`case_gen.generate_cases(..., probe_map=...)`）：

```
测点 → 构造探针请求 → 真实响应 → 断言改写 → 生成用例
```

- 测点变形：A1 去字段、A2/A3/A6/B1 改值、C1 去鉴权、F1 换不存在 id、P0/D* 原样
- 路径参数的 A2 必须**在替换真实 id 之前**改成非法值，否则测出来的是「接受」
- 字段不在基准请求里时，A2/A3/A6/B1 直接跳过，不发出必然「接受」的假探针
- 实测被拒 → 断言精确错误码/状态码，删掉「期望成功」的断言
- 实测接受 + 文档契约维度（A*）→ 断言事实并标 `defect_candidate`，
  这是「服务端没校验」，不是反例通过
- F1 实测成功且无数据 → 断言 `data` 为空
- 有实测证据的反例不再标 `needs_manual_setup`；写操作缺 teardown 仍照旧拦截
  （安全问题不能被探针豁免）

安全边界：默认只探 GET；写接口需显式放开；DELETE 永远不探；探针失败不影响生成。

**证据可信度**（第三轮补强，防止「拿错事实改断言」）：

- 路径参数只从**正例**现役用例取值。同一个接口的「正常请求」和「查不到数据」
  往往在 Excel 里相邻，全量 `update` 会让 999999999 覆盖真实 id，正例直接变假绿。
- 只有明确「被拒绝」或明确「成功」的实测才算证据；超时、5xx、HTML、
  响应里没有 success 字段、同维度多字段结论冲突 → 一律不改写断言，
  反例照旧标 `needs_manual_setup`。
- 实测码独占：改写前删掉模型已经写下的所有 `code` 等值断言，
  避免两条互斥的 `equal code`。
- 标了维度但该维度没探到（被 `max_intents` 截断等）→ 不用 P0 实测顶替。
- 只对单主步骤用例套证据；多步用例不拿第一步的记录担保整条用例。
- 前置/后置里的单花括号路径参数（`{id}`）不自动填真实 id，
  直接标 `needs_manual_setup` —— 后置 DELETE 填错 id 会删掉环境数据。
- 请求里还有 `{{...}}` / `${...}` 未展开时跳过该探针：
  实测的必须是用例真正会发的请求，不能把模板串当参数发出去。

```bash
# 生成时自动探针（AI 模式默认开）
python -m ai.capabilities.case_gen --file .cache/apidoc/openapi.json \
  --project mall4j --only-path /admin/area --sandbox --probe-output .cache/ai_compare/probe_area.json

# 只探不打（留档/排查用）
python -m ai.pipeline.probe --file .cache/apidoc/openapi.json \
  --project mall4j --only-path /admin/area --output .cache/ai_compare/probe_area.json
```

## 4. 已确认决策

| 决策 | 选择 |
|---|---|
| 造数方式 | 只走 API 造数，DB 只读用于校验 |
| 产物落点 | 沙箱模块 `ai_generated`，不碰现役 24 个模块 |
| 入口形态 | CLI 统一编排，后续再包 MCP |
| 首期范围 | area-controller 端到端，再扩全量 |

## 5. 分阶段计划

- **P0 设计层（先立标准）**：`apitest-design` skill + `ai/rules/case_rules.yaml` + 测点规划器 `ai/pipeline/intents.py`
- **P1 生成层**：按 intent 生成，本地模板和 AI 都只填测点，不自由发挥
- **P2 维度闸门**：`dimension_coverage.py`，按最小交付标准检查每个接口的应测维度
- **P3 关联层**：依赖图 + FK 解析 + 多步场景链（创建→查询→修改→删除）。
  写入/执行链路已支持（Excel「步骤」列，见 §7.6）；生成器尚未自动产出 E1 链
- **P4 造数层（已完成基础版）**：`data_orchestrator.py` 规划 POST/PUT/DELETE 的
  造数→查 id→清理链路；单步用例自动挂链路，多步链由调用方/生成器整条给出
- **P5 断言层**：字段断言 + 高置信 DB 断言
- **P6 编排**：`ai/pipeline` 串阶段、中间产物、单阶段重跑
- **P7 修复+覆盖**：复用闭环，覆盖率扩四维
- **P8 验收**：area 端到端，按维度覆盖率验收，再谈全量

## 6. 验收标准（按维度，不按通过率）

以 `admin_area` 为基准：

- 现役基线：8/8 通过（不得回归）
- 每个接口的**应测维度覆盖率 ≥ 80%**（按 `case_rules.yaml` 的 `min_delivery` 算）
- GET 接口至少覆盖：正例 + A1/A2（有元数据时）+ B1（分页）+ C1 + D1（有映射时）+ F1
- 写接口至少覆盖：正例 + A 类 + D2/D3 + E2/E3，且 100% 带 setups/teardowns
- 每条用例至少 2 个业务字段断言；只断言 `status_code`/`success` 的不计数
- 没有依据的测点标 `needs_manual_setup`，**不计入覆盖率分子**，单独列清单
- 通过率只作为稳定性指标，不作为"覆盖完成"的依据

## 7. 验证记录（2026-11-08）

| 版本 | 范围 | 结果 |
|---|---|---|
| 现役 `admin_area` | 8 条 | **8/8 通过**（无回归） |
| 优化前 AI | area GET 12 条 | 2/12 通过 |
| 优化后 AI（沙箱） | area GET 7 条 | **7/7 通过，0 条兜底** |

产物：`testcases/cases_excel/mall4j/ai_admin_area/ai_admin_area.xlsx`（API-189~195，全部 `draft`）

本次修复：

1. 契约接地：`code` 一律收敛到现役用例统计出的成功码 `00000`
2. 路径参数：从现役用例抠真实 id，替换 `{id}`
3. 字段断言：`data.current/size/records/areaId/areaName/level` 等
4. 落库断言：`tz_area` + `parent_id/area_id`，`%s` 占位符自动补 `params`
5. 写操作无 teardown → `needs_manual_setup`
6. LLM 截断抢救 + 退避重试 + 小批次（2 个接口/批）
7. 本地兜底反例标 `needs_manual_setup`，不再冒充已验证用例

已知未做：写操作依赖链、自动造数、规则包独立成文件、覆盖率四维。

### 7.1 第二轮：按维度生成（同一日）

产物：`ai_admin_area` 24 条 + 1 条多步用例被拒（当时 `write_cases_excel`
只支持单主步骤；后来补了「步骤」列，见 §7.6）。

维度覆盖（修复标签撞名前测得）：

| 模块 | 维度覆盖 | 薄用例 |
|---|---|---|
| 现役 `admin_area`（9 条） | 10/38（26%） | 1 |
| 旧 AI（7 条全正例） | 5/38（13%） | 1 |
| 新 AI（24 条） | 覆盖维度明显增加，但 17 条业务断言 < 2 | 17 |

这一轮暴露的问题，已修：

1. **优先级标签 `p0` 和维度 `P0` 撞名** → 维度统一 `dim:` 前缀
2. **鉴权头变量名不合法**（模型写 `{{token}}`）→ 落盘前收敛为 `{{<project>.token}}`
3. **反例断言永远为真**（`is_not_none code`）→ 删除；`status_code in [200,401]` 这类区间期望也删除
4. **框架自动补 token 导致 C1 测不出来** → 新增 `no_auth` 标签
5. **标了维度≠覆盖** → 覆盖率改为按用例内容证实

### 7.2 第三轮：探针驱动（已验证）

第三轮的核心问题是「假设太多」：未登录返回什么、必填缺失会不会被拒，
生成器全靠猜，猜不中就一律 `needs_manual_setup`。本轮把猜测换成实测：

- 新增 `ai/pipeline/probe.py`：按测点对真实环境发请求（默认只 GET，DELETE 永不探）
- `generate_cases(..., probe_map=...)`：实测结果参与提示词和断言改写
- 实测拒绝 → 精确错误码/状态码断言；实测接受 → 断言事实 + `defect_candidate`
- 有实测证据的反例不再标 `needs_manual_setup`；写操作缺 teardown 仍拦截
- `dimension_coverage` 新增「缺陷候选」清单，区分「没测」和「测了但服务端没校验」

实测记录（2026-11-08，真实环境）：

```bash
# 1) 探针：7 个 area 接口，共 18 次请求
.venv/Scripts/python.exe -m ai.pipeline.probe --file .cache/apidoc/openapi.json \
  --project mall4j --only-path admin/area --max-intents 6 \
  --output .cache/ai_compare/probe_area_round3.json

# 2) 生成：25 条 draft（沙箱模块 ai_area_probe，不碰现役模块）
.venv/Scripts/python.exe -m ai.capabilities.case_gen --file .cache/apidoc/openapi.json \
  --project mall4j --module area_probe --only-path admin/area --sandbox

# 3) 执行 + 覆盖率
.venv/Scripts/python.exe scripts/run.py --project mall4j --module ai_area_probe \
  --run-draft --report none --no-retry -m "not needs_manual_setup"
.venv/Scripts/python.exe -m ai.pipeline.dimension_coverage \
  --project mall4j --module ai_area_probe --only-path admin/area
```

结果：

| 指标 | 数值 |
|---|---|
| 生成 | 25 条（GET 18 / 写接口 7 条本地兜底） |
| 执行（排除 needs_manual_setup） | **16/16 通过** |
| 维度覆盖 | **20/38（53%）** |
| 假设用例 | 9 条（7 条写接口缺造数链路 + 2 条 page 的 A1/A2 基准请求里没有可探字段） |
| 缺陷候选 | 2 条（`/admin/area/list` 的 A1/A2 实测被接受） |
| 标称未证实 | 4 条（A1/A2 实测被接受后改断言为成功，不再算反例覆盖） |
| 现役 `admin_area` 回归 | 8/8 通过 |
| 框架单测 | 930 passed |

探针实测的关键事实：`C1` 未登录 → HTTP 200 + `A00004`；
`listByPid` 缺/错 `pid` → `A00005`；`info/999999999` → 成功且 `data=null`；
`list` 缺 `area` / `area` 类型错误 → **服务端照常返回数据**（缺陷候选）。

本轮同时修掉一个框架缺陷：`_send_with_relogin` 把未登录的 `A00004`
当成「登录态失效」，自动重新登录并补上 token 重发，导致 C1 用例实际
带着 token 跑、反例变成假失败。现在 `skip_auth=True`（`no_auth` 用例）
时不做重登录重试。

仍未解决（下一轮）：

- 反例拿不到具体业务错误码时只能标 `needs_manual_setup`，覆盖率分子不含它
- 写接口默认不探针，因此 A1/A6 等维度拿不到实测证据
- 多步场景链（E1 创建→查询→修改→删除）当时被单步骤写入限制挡住
  （写入层已补齐，见 §7.6；生成器仍未自动产出这条链）
- 生成器仍会为写接口退回本地模板（`ai_fallback`），这类用例只覆盖正例

### 7.3 第四轮：API 造数链路（已实现，规划器已实测）

`ai/pipeline/data_orchestrator.py` 补上了写接口的造数/清理：

- 正例 POST/PUT/PATCH/DELETE 自动挂 setups/teardowns；反例继续标人工
- 只删本用例造的种子：DELETE 路径引用查数步骤的输出
- 给不出安全链路时返回 `None`，不发半条链路
- `create_case` 落盘时把前置/后置断言转成扁平格式，修掉「存得下、读不回」的格式错配
- 带 `api_data_chain` 的用例强制进 `ai_<模块>` 沙箱

**实测结果（2026-11-08）：**

| 验证 | 结果 |
|---|---|
| `unit_tests/test_data_orchestrator.py` | 11 passed |
| 全量 `unit_tests` | 954 passed |
| `test_area_chain_real_doc.py`（真实 `openapi.json`，纯静态） | 5 passed |
| `ai_area_chain` 沙箱产物结构检查 | 8 passed |
| `admin_area` 现役回归（真实环境） | 8/8 通过 |
| `ai_area_chain` 真实执行（修复前） | 6 passed / 0 failed / 11 deselected |
| `ai_area_chain` 真实执行（修复后） | **7 passed / 0 failed / 10 deselected** |

真实文档验证确认：`/admin/area`（POST/PUT）、`/admin/area/list`、
`/admin/area/{id}`（DELETE）三兄弟齐全；`ServerResponseEntityListArea`
的 `data` 是 `Area` 数组，规划器据此推断出 `areaName → areaId`；
PUT 的 `areaId=0` 被替换成查数表达式，DELETE 主路径换成种子 id。

真实执行日志确认闭环成立：PUT 用例造种子（`areaId=999000000166`）→ 主步骤
修改自己造的记录 → teardown 用 `find_id_by_fixed` 查回后删除；POST 用例
主步骤造数 → teardown 查数据 + 清理；均无残留。

**本轮发现并修复的缺陷：** DELETE 正例在路径解析阶段因 `{id}` 取不到
可信值被标 `needs_manual_setup`，随后造数链路成功重写了路径，但标记没撤，
导致真实执行时被 `-m "not needs_manual_setup"` 过滤掉。现在链路挂上且主
步骤无残留单花括号参数时会撤掉该标记（`case_gen.py` 本地/AI 两条路径），
并加了回归单测与「挂了链路就不该标人工」的产物级不变量。

**修复后复跑（2026-11-08）：** 可执行用例从 6 条增至 7 条。DELETE 正例日志：
setup `造数据` 用固定名称 POST（服务端生成 id）→ setup `查数据` 按名称查回
→ 主步骤 `DELETE /admin/area/999000000170` 返回 `00000`，断言全过，无 teardown
（主步骤自清理）。10 条 deselected 均为反例/边界（数据构造不适用），符合设计。

### 7.4 第五轮：用例命名对齐现役规范（已实现，待复跑）

**问题**：AI 生成的报告里出现 `PUT /admin/area`、`GET /admin/area/page`、
`DELETE /admin/area/{id}（正常）` 这类名字。根因是 mall4j 的 OpenAPI
没有 `summary`，`Endpoint.display_name` 退化成 `f"{method} {path}"`
（`tools/importer/models.py:66`），生成器又直接拿它当业务名。

**现役规范**（扫 24 个模块 132 行手工用例得出，129/132 = 97.7% 一致）：

| 列 | 格式 | 示例 |
|---|---|---|
| 用例名称 | `模块中文名 - 业务功能（场景）` | `地区管理 - 修改地区（正常请求）` |
| 接口名称 | 中文业务动作，不带方法/路径 | `修改地区` |
| 用例标题 | 只写场景 | `（正常请求）` / `（查不到数据）` / `（真删）` |
| 报告标题（自动拼） | `[模块] 接口名称 - 用例标题` | `[地区管理] 修改地区 - （正常请求）` |

**修正点（`ai/capabilities/case_gen.py`）：**

- `_business_name`：业务名优先级 = 现役用例的「接口名称」> 中文
  name/summary > `display_name`，不再直接落到「方法 + 路径」
- `_name_parts`：把 `业务名（场景）` 拆成「接口名称」+「用例标题」
- `_full_case_name`：拼完整的「模块中文名 - 业务功能（场景）」；
  沙箱模块没有中文 label 时自动补 `module.yaml`（`地区管理（AI 沙箱）`）
- `create_case` 新增可选 `case_name` 参数；不传时由 `_default_case_name`
  按「模块中文名 - api_name + title」拼（见 §7.5），不再落到旧的
  `api_name - title`
- AI 提示词要求 `name` 写成「业务动作（场景）」，禁止方法/路径

**回归**：新增 `unit_tests/test_case_gen_naming.py`（5 条，锁住上述规则）；
`test_area_chain_artifact.py` 加了产物级不变量「接口名称不能是方法+路径」。

**实测结果（2026-11-08 19:44–19:59）：**

| 验证 | 结果 |
|---|---|
| `test_case_gen_naming.py` + `test_data_orchestrator.py` | 16 passed |
| `test_area_chain_artifact.py` + `test_case_gen_naming.py` | 14 passed |
| 重新生成 `ai_area_chain`（`--mode local --sandbox`） | 17 条，API-237~253 |
| 真实执行 `-m "not needs_manual_setup"` | **7 passed / 0 failed / 10 deselected** |
| Allure 报告 | `reports/allure_20261108_195840/index.html` |

报告里实际渲染的标题（取自 `data/behaviors.json`）：

```
[地区管理（AI 沙箱）] 修改地区 - （正常请求）
[地区管理（AI 沙箱）] 新增地区 - （正常请求）
[地区管理（AI 沙箱）] 分页查询地区 - （正常请求）
[地区管理（AI 沙箱）] 查询地区列表 - （正常请求）
[地区管理（AI 沙箱）] 按父级查询下级地区 - （正常请求）
[地区管理（AI 沙箱）] 查询地区详情 - （正常请求）
[地区管理（AI 沙箱）] 删除地区 - （正常请求）
```

`ai_area_chain/module.yaml` 由生成器自动补上（`label: 地区管理（AI 沙箱）`），
feature 层不再是「ai area chain」。

**踩到的坑**：`--only-path /admin/area` 在 Git Bash 下会被当成 Windows 绝对路径
转换，传到 Python 时已经变形，表现为「没有匹配到接口」。写 `--only-path "admin/area"`
（不带前导斜杠）即可。

#### 7.4.1 写入层兜底 + 历史行改名（2026-11-09）

上面那套修正只在「生成器 → `case_gen.write_cases_excel`」这条链路上生效。
只要有**别的**入口把接口文档里的「接口名」原样传进来（模型直调
`create_case`、评测脚本、手工补用例），落盘出来的仍然是
`[ai admin area] PUT /admin/area - PUT /admin/area（正常）`。

- `ai/agent/tools/case_io.py` 新增 `align_case_naming()`，在 `create_case`
  写盘前兜底：`api_name` 为空 / 是「方法 + 路径」/ 不是中文 → 按
  **同方法 + 同路径**去现役用例里找中文业务名（路径模板和具体 id 都归一成
  `{}`）；`title` 里重复了业务名就只留场景；`case_name` 带着被替换的旧名字
  就作废，交给 `_default_case_name` 重拼。方法必须一致 —— `POST /admin/area`
  不能借用 `GET` 用例的「查询地区」。
- `case_gen.write_cases_excel` 在算去重指纹**之前**先对齐，否则同一接口换个
  名字重跑会被当成新用例再追加一遍。提示词里也补了 `现役业务名` 字段。
- `create_case` 写 `ai_` 沙箱模块时自动补 `module.yaml`（`_ensure_sandbox_label`），
  报告左侧树显示「地区管理（AI 沙箱）」而不是「ai admin area」。
- `tools/maint/rename_ai_cases.py`：原地改**历史** AI 用例（只扫 `ai_` 目录，
  默认只看，`--apply` 才落盘；写前备份到 `.cache/backup/rename_ai_<时间戳>/`，
  写后逐格复验，不一致就恢复）。重新生成不是替代方案 —— 指纹变了只会多一批重复。

新增单测：`test_case_io.py` 5 条（写入层兜底）、`test_case_gen_naming.py`
1 条（提示词带现役业务名）、`test_rename_ai_cases.py` 5 条（改名工具）。
**待复跑**：`.venv\Scripts\python.exe -m pytest unit_tests -q`。

### 7.5 第六轮：自愈闭环与产物体检（2026-11-08）

这一轮针对的是「看起来跑过了，其实没跑」和「跑多了，没人收」两类问题。

**1. `run_cases` 跑不了 draft —— 自愈闭环断在这里**

`create_case` 默认写 `draft`，而 `run_cases` 起子进程时没有 `--run-draft`：
pytest 在收集阶段就把它们过滤掉，退出码 0、`total=0`。`verify_heal` 同样走
`run_cases`，于是「改完验证」永远得到「没有执行任何用例」。

- `run_cases` 新增 `run_draft`（默认 `false`，不传时行为不变）
- `verify_heal` 固定 `run_draft=True`
- `total=0` 的提示里直接给出「加 `run_draft=true` 再跑」
- `create_case` / `plan_heal` 的 `next_step`、MCP prompt 同步改口径

**2. `create_case` 默认命名缺模块前缀**

MCP 入口以前拼 `api_name - title`，和 `case_gen` 的完整命名不一致。
新增 `_default_case_name`：模块中文名取 `ModuleManager.get_label`，
取不到退回模块名；`title` 以括号开头时直接拼（`修改地区（正常请求）`），
否则用 ` - ` 分隔（避免「删除地区删除区域成功」）。

**3. 备份目录无限增长**

每次写用例新建一个 `.cache/backup/agent_<时间戳>/`，生成 17 条就是 17 个目录，
当天已累积 200+。新增 `BACKUP_KEEP = 50`，`_backup` 写完顺手清理更旧的目录
（只动 `agent_*`，别的目录不碰）。

**4. `_count_matching` 异常时返回 0 —— 两道闸同时失效**

统计用例条数失败（目录里有一个读不了的文件就够）时原先 `return 0`：
`matched > MAX_CASES_PER_RUN` 不成立、`run_budget.check(0)` 也放行，
模型可以以「统计不了」为由跑全量。现在 fail-closed：

- 给了 `case_id` → 按编号直接数（不依赖读文件），闸仍有效
- 其余筛选 → 抛 `ToolError`，让调用方精确指定要跑哪几条

配套改了 `_common`：`_load_file` / `iter_cases` 新增 `strict`。默认行为不变
（列表场景一个坏文件不该拖垮整个列表），但**数条数时传 `strict=True`**，
并且把「读失败」连同 mtime 一起缓存 —— 否则第一次非严格调用缓存了 `[]`，
后面的严格调用会命中缓存、继续低估。

**5. 重跑生成器会追加重复用例**

`write_cases_excel` 写前按「方法 + 路径 + 接口名称 + 用例标题」查重，
命中就进 `skipped` 并给出原因，不再原样追加。读文件用只读模式并显式
`close()`（Windows 上句柄不释放会让紧接着的 `save()` 失败）。

**6. `set_case_status` 转 debugged 不核对「真的跑通过」**

`debugged` 的语义是「跑通过、可以进 CI」，但这一步原先只写文件 —— 智能体
一次都没跑就能把 draft 标绿，CI 里那条用例从没执行过。现在：

- `run_cases` 在**整体通过**（退出码 0、`total > 0`、failed / error / skipped
  全 0、且 `total` 等于候选条数）后，把这次跑过的用例写进
  `.cache/verify_ledger.json`，记录内容指纹、文件路径和时间
- 指纹按**单条用例**算（`json` 序列化后 sha1），并且**去掉 `status`** ——
  按文件哈希算会让同表其它用例的改动互相作废，带着 `status` 算则转正后
  自己就失效
- `apply_status_change(debugged)` 核对账本：没记录 / 记录来自别的文件 /
  记录之后用例又改过，一律拒绝；逃生口 `force=true` 必须写 `note`
- 记录条件严格是故意的：候选条数和实际执行条数对不上，说明筛选语义和
  执行结果不一致（比如 `-k` 的匹配范围），宁可不记也不误记。候选条数按
  pytest 的收集口径算：带「参数化数据」的用例按展开后的组数计，不给
  `run_draft` 时只算 `debugged`，按 `case_id` 挑时不看状态

**7. 写探针的环境只靠帮助文本约束**

`--include-write` 会把 POST/PUT/PATCH 真的发出去，而它造的数据**没有清理**
（DELETE 被禁）。唯一的保护是 `--help` 里那句「只在测试环境用」—— 加个
`--env online` 就绕过去了，脏数据留在准生产上。

现在 `ensure_write_allowed` 是硬闸：环境不在 `WRITE_PROBE_ENVS`
（`test` / `dev`）就直接 `RuntimeError`，而且**在发第一个请求之前**拦
（`probe_endpoints` 入口先查一次）—— 放到逐接口里查只会得到一堆
「探针失败」的 outcome，看起来像接口坏了。只读探针不受影响。

环境闸只解决「别在准生产上写」，解决不了「测试环境里造的数据谁清」。
所以写探针**每次成功后都留足迹**：`ProbeOutcome.created` 记下测点、
完整请求体、从响应 `data` 里认出的一层主键（`id` / `areaId` /
`area_id`），`probe_endpoints` 再补上按文档匹配到的删除接口
（只认 `<base>/<一个路径参数>`，嵌套路径和同前缀的其它接口都不认）。
命令行跑完把这些摊平成 `<输出名>_footprints.json`（跟 `--output` 同目录，
没给 `--output` 就写 `.cache/ai_compare/probe_footprints.json`），
并在 stderr 上标注条数。

**只留线索、不自动删**：自动删要能证明「删的就是这次造的那条」，
这条链路没在真实测试环境验证过之前，宁可让人按清单手工清。

**POST 和 PUT/PATCH 必须分开记**：只有 POST 是新建、能按 DELETE 清；
PUT/PATCH 改的是本来就存在的那条，按 DELETE 清会删掉原有数据。
所以更新探针只进 `ProbeOutcome.modified`（「改动的数据」），
`collect_footprints` 只认 `created` —— 它直接决定清理计划里有什么。

清单到 DELETE 之间再加一层：`ai/pipeline/probe_cleanup.py` 把足迹翻成
清理计划（`plan_cleanup` 是纯函数，模块里没有任何 HTTP 客户端）。填充
路径参数时先按名字对（`{areaId}` ← `areaId`）；通用 `{id}` 再按资源名
对一次（`/admin/area` → `areaId` / `area_id`）；还不行且只有一个 id 时
才用它；多个 id 又对不上就跳过。没有路径参数的删除接口、id 里带
`/` `?` `#` 的一律跳过并写明原因 —— **不猜**。
命令行 `python -m ai.pipeline.probe_cleanup --footprints <清单>` 只打印/
落盘计划，不发送请求；真正执行删除留到链路在测试环境验证通过之后。

**8. 沙箱模块 `ai_*` 没有生命周期管理**

AI 生成的用例落在 `ai_<模块>`，默认 draft、不进 CI，也**不会自动退役**。
没人管就一直堆着。现在 `list_modules` 给每个 `ai_*` 模块额外算
`sandbox / draft / debugged / idle_days / advice`（按文件 mtime 算闲置天数），
并在顶层给出 `sandbox_modules` 清单和提示。判定分四档：

| 状态 | 建议 |
|---|---|
| 全 draft + 闲置 ≥ 30 天 | 确认无用后整个目录删掉 |
| 全 draft + 近期动过 | 跑通转正，或确认无用后清理 |
| 有 debugged + 闲置 ≥ 30 天 | 重跑确认 |
| 有 debugged + 近期动过 | 正常维护中 |

**只提示，不自动删** —— 用例是人的资产，框架不该替人决定删什么。

**回归**：新增 `unit_tests/test_create_case_naming.py`（7 条：默认命名、
不重复业务名、旧写法业务名去重、模块名兜底、`run_draft` 参数存在、
`verify_heal` 传了 `run_draft`、备份只留最近 50 个）、
`unit_tests/test_run_cases_gate.py`（6 条：坏文件严格抛错、统计失败不返回 0、
case_id 兜底、失败时挡下 run_cases、`--run-draft` 透传、默认不传）、
`unit_tests/test_verify_ledger.py`（17 条：指纹忽略 status / 随内容变化、
记账后可查、改内容 / 换文件后不可查、空键不记、转正被拦 / 记账后放行 /
记录过期于改动、`force` 必须写理由、已是 debugged 不重复拦、其它状态不受限、
条数对不上不记、参数化按展开后计数、不开 `run_draft` 时按 debugged 过滤、
按编号挑时保留 draft）、`unit_tests/test_probe_write_gate.py`
（11 条：测试环境放行 / 准生产拒绝、批量探针在建客户端前拦下、只读探针不受限）、
`unit_tests/test_probe_footprints.py`（36 条 / 19 组：主键名识别、只看一层、
标量 data、0 / 空串 / 布尔不算 id、删除接口只认单段路径参数、成功写探针
才留足迹、失败与只读探针不留、`data` 为 null 也留请求体、PUT/PATCH 只记
「改动的数据」且不进清理计划、清单摊平与「清理接口」缺失）、
`unit_tests/test_probe_cleanup.py`（27 条：路径参数按
名字填充 / 通用 `{id}` 按资源名挑 / 单 id 兜底、资源名对不上就跳过、
缺删除接口 / 无路径参数 / 非法 id 值跳过（含 `None` / 布尔 ——
`str(None)` 是 `"None"`，光靠字符白名单拦不住）、计划拆分与上下文保留、
模块里没有 HTTP 客户端、足迹文件两种格式）
和 `unit_tests/test_sandbox_lifecycle.py`（5 条：四档建议的判定、
取不到 mtime 按闲置处理、`list_modules` 只给 `ai_*` 加字段）。

**全量回归**：`unit_tests` **1071 passed**（实测）。上一次 1061 passed / 7 failed
全在 `_entity_of`：`split_target` 返回 `(method, path)` 两个值，这里按三个
解包。已改成 `_, path = split_target(api)`；同轮又补了 3 条「`None` / 布尔
不能当 id」的用例（`str(None)` 是 `"None"`，能过字符白名单）。

**本轮又补的边界（26 条用例）**：

| 位置 | 缺陷 | 用例 |
| --- | --- | --- |
| `probe._report_footprints` | 落盘那一层以前零测试：文件名推导、只有改动时写不写、空时不建文件、写盘失败兜底全是裸的 | 5 |
| `audit_log._sanitize` | 只扫一层 key，`create_case(request_headers={"Authorization": ...})` 的 token 原样落进长期保留的审计日志；`X-Api-Key` 这种连字符写法也匹配不上黑名单 | 1 |
| `mcp_server.resources._read` | `apitest://cases/../../configs/config.yaml` 能读到用例目录外的任意文件；非 `.yaml/.xlsx` 后缀也照读 | 9 |
| `agent.tools.data.query_db` | 工具描述写着"用 `%s` 占位"却不收参数，模型照写必报错；`SELECT ... INTO OUTFILE` 第一个关键字是 SELECT，白名单拦不住 | 8 |
| `case_io.create_case` | `status` 不在工具 schema 里，但注册表按参数名直接调 handler —— 多塞一个 `status="debugged"` 就能绕开 `set_case_status` 的「跑通过凭据」闸 | 3 |

**同一轮又补的四处（24 条用例）**：

| 位置 | 缺陷 | 用例 |
| --- | --- | --- |
| `mcp_server.protocol.read_message` | 合法 JSON 不等于合法消息：`[1,2]` / `"hi"` / `5` 都能过 `json.loads`，但主循环对它取 `.get` 会 `AttributeError`，那句不在 `try` 里 —— 一条坏消息就能让 stdio 服务退出 | 6 |
| `mcp_server.resources` 的 `schema/{alias}` | alias 被直接拼进 glob：`apitest://schema/*` 能匹配快照目录里的任意文件；改成白名单校验 + 按文件名逐个比对 | 10 |
| `heal_state` | 手改坏的计数文件（某条记录写成字符串/数字）`.get` 抛 `AttributeError`，整条自愈链路挂掉 | 3 |
| `execution._read_result_json` | `result.json` 是合法 JSON 但不是对象（`[]`/`124`）时，`run_cases` 里到处 `.get` 抛 `TypeError` | 5 |

**又一轮（6 条用例）**：

| 位置 | 缺陷 | 用例 |
| --- | --- | --- |
| `case_io._replace_yaml_scalar` + `_verify` | YAML 的 `status` 是文本替换：取「第一个匹配」时，步骤请求体里的 `data.status` 会被改成 `debugged`（用例本体仍是 draft、接口参数被改坏）；没有 `status` 键时又插到 `testcase:` 外面，`resolve_status` 永远读不到。写完还不核对状态是否真的落到了用例本体上 | 6 |

**全量回归**：`unit_tests` **1121 passed**（实测，16.18s）。
加这 6 条后实测 **1153 passed / 2 failed**（1155 收集）—— 失败的 2 条是插入
`status` 时漏了行尾换行，已修（详见下一节末尾）。1071 → 1121 正好是上面两张表合计的 50 条。

**又一轮：合法 JSON 但结构不对（29 条用例）**

前面修的是「文件读不了」。这一类更隐蔽：**JSON 解析成功了，但根节点不是对象**。
此时 `json.loads` 不抛异常，异常在下一行 `.get(...)` 才炸，而且往往是整片工具报错
（`search_endpoints` / `query_db` / `diagnose_failures` 全挂）或整轮归因中断，
排查时看不出跟坏文件的关系。

| 位置 | 缺陷 | 用例 |
| --- | --- | --- |
| `endpoints.load_openapi` / `_iter_operations` | 快照根是 `[]`/`"x"` 时 `doc.get("paths")` 抛 `AttributeError`；`paths` 是列表时 `.items()` 同样崩 | 6 |
| `diagnostics._failed_count` | `result.json` 是非对象时 `data.get` 抛 `AttributeError`（只 catch 了 `TypeError/ValueError`） | 4 |
| `diagnostics._fresh_cases` / `execution._fresh_result_names` | allure 结果文件被写坏成列表/标量时 `data.get("name")` 抛 `AttributeError`，整轮归因中断 | 2 |
| `data._load_schema_file` / `get_table_schema` | 表结构快照根不是对象时缓存进非 dict，查表工具到处 `.get` 崩；`tables` 是对象时遍历拿到字符串键、`tables`/`columns` 里混进非 dict 元素同样崩 | 5 |
| `case_io.restore_from_backup` | `manifest.json` 是 `[]`/`5` 时 `manifest.get` 抛 `AttributeError`；JSON 坏了也直接抛 `JSONDecodeError` | 3 |
| `knowledge.builder._load_openapi` / `_load_tables` | 同上的非对象根节点，建图谱时崩 | 4 |
| `audit_log.recent` | 审计日志里混进一行合法 JSON 非对象（`[]`/`5`）时 `.get` 抛 `AttributeError` | 1 |
| `verify_ledger._prune` / `verified_state` | 账本某条记录被手改成字符串/数字：`_prune` 崩掉导致**从此记不进新凭据**；`verified_state` 把内部 `AttributeError` 漏给 `set_case_status`，而不是给出「没有记录」的拒绝理由 | 2 |
| `evals.runner.load_tasks` | 评测集 YAML 根节点是列表时 `data.get("tasks")` 抛 `AttributeError`，报错看不出是评测集写错了 | 1 |
| `mcp_server.selfcheck` | 子进程回一行 `5`，自检自己在 `msg.get` 上崩 | 0（与 stdio 同一类，合并校验） |

**实测**：`unit_tests` 收集 **1155**，第一次跑 **1153 passed / 2 failed** —— 失败的两条
正是上一轮新加的 YAML 状态用例，暴露了一个真 bug：往 `testcase:` 下插 `status`
时漏了行尾换行，拼成 `status: broken  name: 上架商品`，整份 YAML 解析失败；
复验闸门识别出「找不到用例」并回滚（**没有**把坏文件留在磁盘上）。

同一处还有第二个坑：扁平用例往文件开头插 `status` 时，如果文件以注释 + `---`
开头，插在 `---` 之前会变成两份文档，用例本体落到第二份、`safe_load` 只读第一份。
已改成跳过文件头的注释 / `%YAML` 指令 / `---` 标记后再插。

修复后复跑：`unit_tests/test_case_io.py` **68 passed**，全量 **1156 passed**
（15.71s，实测 2026-11-09 20:15）。新增的
`test_flat_yaml_status_inserted_after_document_header` 已覆盖 `---` 文档头场景。

**再一轮：运行态文件里的坏字段（6 条用例）**

上一轮收的是「整个根节点不是对象」。这一类更细：文件**结构是对的**，
只是某个字段被手改成了字符串/数字。崩的地方都恰好在 try 之外，后果不是
"读不到数据"，而是**工具直接不可用**：

| 位置 | 缺陷 | 用例 |
| --- | --- | --- |
| `run_budget.spent` / `add` | 会话桶被写成 `"session:x": "garbage"`、或 `cases` 写成 `"abc"` 时 `.get` 抛 `AttributeError` / `int` 抛 `ValueError` —— `run_cases` 整个挂掉，累计预算闸从"限制用量"变成"工具坏了" | 2 |
| `run_budget._prune` | 桶不是 dict 时 `kv[1].get("updated_at")` 抛 `AttributeError`；`updated_at` 不是数字时 `float` 抛 `ValueError`（都在 `add()` 的 try 外） | 并入上一条 |
| `audit_log.recent` | 某行 `epoch` 写成 `"昨天"` 时 `float()` 抛 `ValueError`，`get_agent_audit` 完全不可用 | 1 |
| `audit_log.summarize` | `duration_ms` / `tokens` 里的值不是数字、`tokens` 不是对象时同样抛异常，成本统计工具崩掉 | 1 |
| `heal_state.reset` | 验证通过要清零。写盘失败时内存里只把计数记成 0，而 `failed_rounds` 取 `max(磁盘, 内存)` —— 磁盘上的旧计数把刚跑通的用例顶回「两轮已用尽」，此后 `plan_heal` 永远返回 `stop` | 1 |
| `execution.get_last_result` | 读完成结果到取 `stat()` 之间文件被清掉（并发跑 / 报告清理）→ `FileNotFoundError`，只想看一眼上次结果也报错 | 1 |

修法统一成「运行态字段先收类型」：`_bucket_of` / `_safe_int` / `_safe_float`
（`run_budget`）和 `_as_int` / `_as_float`（`audit_log`），坏值按 0 / 默认值处理。
**计数类坏值按 0 是安全的**：预算与轮次本身还有内存兜底取 `max()`，
磁盘上的坏值只会让闸更宽松一次，不会永久失效。

用例加在 `unit_tests/test_hardening_round2.py`。复跑全量 **1162 passed**
（19.25s，实测 2026-11-09 20:33）。

**再一轮：入口参数与生成产物的类型容错（8 条用例）**

运行态文件收完了，还剩两个入口：

1. **工具参数**：MCP 的 `params_schema` 只用于展示，服务端不做类型校验；
   客户端传 `{"limit": "abc"}` 会原样进 handler。`int(limit or 50)` 抛
   `ValueError`，排障 / 查询工具整个不可用。
2. **生成产物**：`data_orchestrator` 假设用例是 `steps: [...]` /
   `marks: [...]` 的正规结构。模型偶尔漏字段或给错类型，`list(5)` /
   迭代整数会抛 `TypeError`，把整批用例生成打断。

| 位置 | 缺陷 | 用例 |
| --- | --- | --- |
| `tools/audit.get_agent_audit` / `get_agent_cost` | `limit="abc"` / `since_minutes="很久"` → `ValueError` | 1 |
| `tools/cases.list_cases` | 同上 | 1 |
| `tools/endpoints.search_endpoints` | 同上 | 1 |
| `tools/knowledge.search_knowledge` | `limit="abc"` 在 `max(1, "abc")` 抛 `TypeError`；`kind=5` 在 `.split` 抛 `AttributeError` | 1 |
| `tools/data.query_db` / `tools/diagnostics.diagnose_failures` / `tools/execution.run_cases` | `limit` / `timeout` 非数字 → `ValueError` | 0（复用 `_params` 的用例） |
| `tools/heal.verify_heal` / `execution.run_cases` | `result.json` 的 `total` / `passed` 被写成字符串 → 读结果时 `ValueError` | 0（并入运行态字段） |
| `pipeline/data_orchestrator._find` / `_match_endpoint` | 接口清单里混进非对象元素时 `ep.method` 抛 `AttributeError` | 1 |
| `data_orchestrator._trusted_example_body` / `_lookup_params` | 现役示例条目不是对象时 `.get` 崩 | 1 |
| `data_orchestrator._success_asserts` | `ctx.contract` 不是对象时 `.get` 崩 | 1 |
| `data_orchestrator.attach_chain` / `write_is_self_cleaning` | `steps` / `marks` / `setups` 不是列表时迭代抛 `TypeError`（在 try 之外） | 1 |

新增 `ai/agent/tools/_params.py`：`safe_int` / `clamp_int`，语义与
`audit_log._as_int` 一致（`None` / `""` / `0` / 坏值都退回默认值）。

用例：`test_hardening_round2.py` 5 条 + `test_data_orchestrator.py` 3 条。
复跑全量 **1170 passed**（15.94s，实测 2026-11-09 21:07）。

**再一轮：`params_schema` 真正参与校验（3 条用例）**

前一轮的容错是「坏值静默退回默认值」：调 `list_cases(limit="abc")` 会
拿到 50 条，模型却以为按它说的条数查了；参数名写错、必填漏传，也只是
在 handler 里碰巧被 `**kwargs` 之外的东西拦下。`params_schema` 之前只
给客户端看，服务端不按它校验。

这一轮在 `ToolRegistry.call` 进入 handler **之前**按 schema 校验：

- 能无损转的直接转：`"50"` → 50、`"true"` → True、标量 → 字符串，
  数组的 tuple 转 list —— JSON 客户端常见写法不该被当成错
- 转不了就抛 `ToolError`，由 `call` 收成 `ok: false` + hint，
  例如「参数 limit 需要整数，收到 str: 'abc'」
- `None` 一律按「没给」处理：可选参数落到 handler 默认值，必填参数报缺失，
  不再把 `None` 原样塞进 handler
- `additionalProperties: false` 的工具拒绝不认识的参数（名字写错时
  直接告诉模型该工具的参数清单，而不是等 `TypeError`）
- `enum` 与数组元素类型（`items.type`）同样校验
- 校验器本身对坏 schema 容错：`properties` 不是对象、`required`
  写成字符串、`type` 写成对象，都不会让校验器崩

| 位置 | 缺陷 | 用例 |
| --- | --- | --- |
| `tools/registry._normalize_arguments` | 未知参数 / 缺必填 / 类型错 / enum 外取值全部放行，错误被 handler 的 `int()` 兜底吃掉 | 3 |

用例加在 `unit_tests/test_hardening_round2.py`：JSON 友好类型归一、
错误类型与数组元素拒绝、缺失/未知/枚举拒绝。复跑全量 **1173 passed**
（16.13s，实测 2026-11-09 21:29）。

**再一轮：智能体循环扛住脏回复（7 条用例）**

`ai/agent/loop.py` 是"模型 → 调工具 → 回灌"的最小循环，契约写的是
「不抛异常，失败信息放 `stopped` / `error`」。但它对 LLM 客户端返回的
结构是全信任的 —— 评测注入的客户端、第三方 OpenAI 兼容网关都可能
给出不合规的 `usage` / `message` / `tool_calls`，一个坏字段就让**整批
评测**崩掉（一条任务失败应该只影响这一条）。

| 位置 | 缺陷 | 用例 |
| --- | --- | --- |
| `loop.run_task` token 汇总 | `usage.prompt_tokens` 是 `"abc"` / `None` → `int()` 抛 `ValueError` | 1 |
| `loop.run_task` 预算参数 | `max_steps` / `max_tool_calls` 传坏值 → 进循环前就抛 | 1 |
| `loop.run_task` 回复结构 | `reply` 不是对象、`message` 是字符串、`tool_calls` 不是列表 → `.get` / 迭代 `AttributeError` | 2 |
| `loop.run_task` 工具调用 | `call` / `function` 不是对象、工具名为空 → `AttributeError` 或拿空名字调工具 | 并入上条 |
| `loop._tool_definitions` | `allow_tools` 传字符串被 `set()` 拆成单个字符，白名单形同虚设；空列表反而**放开全部** | 1 |
| `loop.run_task` 调用工具 | 注入的 registry 抛异常 / 返回非 dict → 整个任务崩 | 1 |
| `loop.Step.to_dict` | `arguments` 不截断，模型塞超大 `request_data` 会把评测结果 json 撑到几十 MB | 1 |
| `evals.runner` | `task.expect` 不是对象时 `.get` 崩（`run_task` 与 `grade` 两处）；token 汇总 `int()` 无容错 | 1 |

顺手改掉的两个语义问题：

- `allow_tools` 按**收紧**方向解释：字符串按逗号/空格拆；空列表 =
  一个工具都不给；无法识别的结构也按「没有可用工具」处理并告警 ——
  以前写错白名单等于全放开，这是评测里最危险的失败方式
- 同一轮多个 tool_call 都没有 `id` 时，回填用的 `call_<step>_<序号>`
  不再重复（重复的 `tool_call_id` 会被 OpenAI 兼容接口拒掉）

用例加在 `unit_tests/test_hardening_round2.py`。复跑全量 **1180 passed**
（17.88s，实测 2026-11-09 21:44）。

**又一轮：生成产物与 LLM 客户端的脏结构（16 条用例）**

`case_gen` 处理的是**模型产物**和**人写的配置**，两者的类型都不能假定。
这一轮把「一个坏字段 / 一个坏形状就带走整批生成」的路径全部堵住：

| 位置 | 缺陷 | 用例 |
| --- | --- | --- |
| `case_gen._compact_asserts` / `_compact_case` | 断言/步骤写成单个对象时 `[:limit]` 抛 `TypeError`（dict 不可切片）；`marks` 是字符串时被拆成单个字符塞给模型 | 1 |
| `case_gen._priority_of` / `_normalize_ai_case` | `marks: "negative,p0"` 逐字符遍历，优先级漏判成 P1 | 1 |
| `case_gen.build_context` | 现役用例不是对象时 `case.get` 抛 `AttributeError`；`marks` 字符串同样逐字符展开 | 并入上面两条 |
| `case_gen._probe_index` | 探针结果整体不是对象（手工改过的 json）时 `.items()` 崩；单个维度写成对象时不认 | 1 |
| `case_gen._append_assert` | `step["assert"]` 是对象（不是数组）时 `dict.append` 抛 `AttributeError` | 1 |
| `case_gen._has_rejection_assert` | `container` 写成标量（`expected: 200`）时迭代 int 抛 `TypeError`；断言数组里混进非对象同样崩 | 1 |
| `case_gen._generate_locally` | 文档 body 是数组时 `container.pop(name, None)` / `container[name] = ""` 抛 `TypeError`，整个本地生成中断 | 并入上面的空值/缺参路径 |
| `case_gen._generate_with_ai` | 单条模型产物结构离谱时 `_finalize_ai_case` 抛异常，**整批**（含其他正常用例）丢掉 | 1 |
| `case_gen.write_cases` / `write_cases_excel` | `data["testcase"]` 直接 `KeyError`；`steps` 写成对象时 `len()==1` 蒙混过关、`steps[0]` 抛 `KeyError`；`allure` 不是对象时 `.get` 崩；`marks` 是字符串时 `",".join` 拆成字符 | 2 |
| `case_gen._run_probe` / `_salvage_truncated_cases` | `--probe-output` 直接调 `outcome.to_dict()`（dict 结果没有该方法）、tuple key 进 `json.dumps` 也会报错；截断抢救收到非字符串抛 `AttributeError` | 并入上面的探针用例 |
| `client.LLMConfig.from_config` | yaml 把 `base_url`/`api_key` 解析成数字、`timeout: "60s"`、`temperature: hot`、`headers` 不是键值对 —— 任一处都会让程序在启动时就 `ValueError`/`AttributeError` | 1 |
| `client.chat` / `chat_with_tools` / `_extract_text` | 网关回 `200` 但 `choices` 是字符串 / 元素不是对象 → `.get` 抛 `AttributeError`，而不是可归因的 `LLMError` | 1 |
| `client.parse_json_response` | 收到非字符串（替身客户端）时 `.strip()` 抛 `AttributeError` | 1 |
| `evals.runner.grade` | `expect` 里的列表写成字符串时逐字符判分（`must_call` 永远不过）；`max_tool_calls: "5次"` / `tool_calls: "abc"` 抛 `ValueError`；`steps` 非数组或混进非对象时判分自己崩 | 1 |
| `pipeline.dimension_coverage` | `assert` 写成对象/混进字符串时 `.get` 抛 `AttributeError`，整个覆盖率报告挂掉；`marks` 字符串逐字符展开 | 1 |
| `pipeline.intents.dimension_ids_of` | `marks: "dim:A1,p0"` 逐字符遍历，维度全部漏判 | 1 |
| `agent.tools._common` | `assert` / `setups` / `steps` 非数组时 `len()` 抛 `TypeError`，`get_case` 详情与用例计数崩 | 1 |
| `knowledge.builder._load_tables` | 表结构快照的 `columns` 是对象/字符串、元素混进字符串时，建图在 `col.get` 处崩 | 1 |
| `capabilities.diagnosis._collect_from_allure` | allure 结果的 `attachments` / `steps` / `statusDetails` / `parameters` 类型坏了，整轮失败归集中断 | 并入上面的结构容错 |

用例加在 `unit_tests/test_hardening_round2.py`：新增 16 条，复跑全量
**1196 passed**（18.17s，实测 2026-11-09 22:54）。


**仍未做 / 已知限制**：

- 清理计划**不执行** DELETE。要能证明「删的就是本次造的那条」，得先在
  测试环境把「造 → 认 id → 删除」整条链路跑通再落地
- mall4j 的新增接口返回 `ServerResponseEntityVoid`（`data` 为 null），
  响应里没有 id —— 足迹只留得下请求体（mall4j 的创建请求自带白名单 id），
  清理计划对这些条目会按「响应里没有 id」跳过，需人工按请求体核对

### 7.6 多步场景链落盘 + 质量闸门口径（2026-11-10）

**问题**：扁平 Excel 原来是「一行 = 一步」，一条「创建 → 查询 → 修改 → 删除」
的链只能存成四个用例，步骤之间互相不可见；生成器碰到多步用例只能跳过，
`write_cases_excel` 的 `skipped` 里写着「只支持单主步骤」。

**格式**：主表「接口测试用例」新增**可选**列「步骤」——JSON 数组，
一行仍然是一条用例::

    [{"id": "create", "description": "创建地区", "method": "POST",
      "path": "/admin/area", "data": {"areaName": "自动化区域"},
      "assert": [{"check": "body", "jsonpath": "$.success",
                  "comparator": "==", "expect": true}]},
     {"id": "verify", "method": "GET",
      "path": "/admin/area/{{create.data.areaId}}",
      "assert": [{"check": "body", "jsonpath": "$.data.records",
                  "comparator": "exists"}]}]

- 不填「步骤」→ 完全沿用老行为（一行一步），现役用例不受影响
- 填了就**以「步骤」为准**；行级「接口 / 请求方法 / 断言」只是第一步的
  展示副本，方便 `-k` 筛选和人工浏览
- 断言两种写法都认：扁平 `check/jsonpath/comparator/expect`，以及框架内部
  `type/field/expected`（手写或从 YAML 迁移时常用）
- 坏 JSON / 步骤缺 `path` → `ExcelCaseError` 直接报，不静默丢步骤

**写入链路**：

| 入口 | 行为 |
|---|---|
| `create_case(steps=[...])` | 写「步骤」列（老文件自动补这一列）；`path/method/assertions` 只用于命名和单步兜底 |
| `apply_assertion_change(..., step="verify")` | 只改「步骤」JSON 里目标步骤的 `assert`；目标是第一步时同步行级「断言」 |
| `case_gen.write_cases_excel` | 多步用例不再跳过，整条链交给 `create_case` |
| `flat_parser` | 「步骤」→ `case["steps"]`，报告/执行/覆盖率都按多步链走 |

**质量闸门（`ai/rules/validator.py`）按整条链判定**：

- 业务断言**跨步骤汇总**（链的检验力分散在写步骤和验证步上）
- 验证步（最后一个非写步骤）必须至少有 1 个业务断言
  （`thin_verification_step`）—— 只验写接口的回执证明不了数据可查
- **每个**写步骤都要有去处：有 teardowns，或它本身是引用 setup 输出的
  DELETE（`write_without_cleanup`）；只清第一步，链尾的写照样留脏数据
- 路径参数扫描覆盖 步骤 / 前置 / 后置；`{id}` 拦，`{{...}}` 放行

**值断言（转正门槛）**：`quality_gates.min_value_asserts` 落盘时是 0，
`set_case_status(debugged)` 临时叠加为 1。`exists` / `is_not_none` 只证明
字段存在，不算值断言；转正时已经跑通过，用 `get_last_result` 取真实响应补
一条带 `expected` 的断言（或 `db_query`）即可。`force` 只跳过「跑通过」
凭据，跳不过这道闸。

**顺带修掉的两个闸门漏洞**：

- 反例写零断言时直接放行 —— 条件里多了 `and asserts`，一条只有请求没有
  断言的「反例」不触发 `negative_without_rejection`
- `plan_assertion_change` 把**扁平格式**断言喂给 `check_case`：它按
  `field/type` 判定，扁平格式里这两个都是空的，于是「有没有正例」永远
  判不出来，改断言的闸门实际空转。现在传框架内部格式

**反例判定口径修正（2026-11-11）**：

- `boundary` 不再无条件算反例：显式 `positive`，或断言里有成功期望
  （`success == true` / `code == '00000'`）时，按正例/中性看；只有没写
  成功期望的边界才要求「断言被拒绝」。否则「超大 pid 返回空列表」
  「超大 size 截断」这类**期望成功**的边界用例会被误判成反例。
- `success not_equal true` 也认作拒绝断言（原来只认 `is_false` /
  `expected: false`）；`dimension_coverage._asserts_rejection` 同步，
  两边口径不能分叉。
- 显式 `negative` / `invalid` / `permission` 标记和负向维度（A/C/F）
  仍然优先于 `boundary` —— 那是更明确的「期望失败」意图。

**存量盘点**：`tools/maint/audit_quality.py`（只读）按同一套闸门扫现存
用例，输出按规则汇总 + 具体编号；`--json` 给 CI，`--fail` 有违规时非 0。
修哪条走正常写入口（会自动再过一次闸）。

**单测**：`unit_tests/test_multi_step_cases.py`（解析 / 写盘读回 / 定向改
断言 / 审计），`test_case_quality_rules.py` 补了多步链路、值断言、零断言
反例，`test_verify_ledger.py` 的夹具补了一条值断言以适配转正门槛。
