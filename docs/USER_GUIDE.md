# 用户使用手册

> 读者：要日常使用本平台写用例、跑测试、看报告、排查问题的人。
> 想先了解项目定位和架构，读 [PROJECT_INTRO.md](PROJECT_INTRO.md)；查语法细节，读 [FRAMEWORK.md](FRAMEWORK.md)。

---

## 目录

1. [安装与环境准备](#一安装与环境准备)
2. [配置（一次性）](#二配置一次性)
3. [第一次跑起来](#三第一次跑起来)
4. [看懂用例从哪来](#四看懂用例从哪来)
5. [写一条用例（Excel）](#五写一条用例excel)
6. [前后置：造数、查 id、清理](#六前后置造数查-id清理)
7. [断言怎么写](#七断言怎么写)
8. [查库断言](#八查库断言)
9. [参数化](#九参数化)
10. [标签与状态](#十标签与状态)
11. [跑测试的常用姿势](#十一跑测试的常用姿势)
12. [看报告](#十二看报告)
13. [常见错误与排查](#十三常见错误与排查)
14. [维护工具速查](#十四维护工具速查)
15. [用 AI / 智能体驱动](#十五用-ai--智能体驱动)
16. [日常清理与目录约定](#十六日常清理与目录约定)

---

## 一、安装与环境准备

### 1.1 Python

要求 **Python 3.12+**。项目自带虚拟环境 `.venv`，**请始终使用它**：

```powershell
# ✅ 正确
.venv\Scripts\python.exe scripts\run.py

# ❌ 不要用裸 python —— Windows 商店占位符会静默退出（退出码 49）
python scripts\run.py
```

### 1.2 装依赖

```powershell
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m playwright install chromium
```

第二个命令装 Chromium 内核 —— 滑块登录要开一次无头浏览器。不装的话登录会失败。

### 1.3 可选依赖（用到再装）

| 能力 | 装什么 |
|---|---|
| 查库断言 `db_query_*` | `mysql-connector-python` |
| 缓存断言 `redis_*` | `redis` |
| MQ 断言 `mq_*` | `pika`（RabbitMQ）或 `kafka-python` |
| 并行跑用例 | `pytest-xdist` |
| allure 精美报告 | 独立安装 allure 命令行 2.20+（不装则用 `--report html`） |

---

## 二、配置（一次性）

### 2.1 复制环境变量样例

```powershell
copy configs\.env.example .env
```

然后编辑项目根目录的 `.env`，填上真实值：

```ini
# mall4j 登录账号
MALL4J_USERNAME=admin
MALL4J_PASSWORD=你的密码

# mall4j 业务库（查库断言用，只读账号）
MALL4J_DB_HOST=...
MALL4J_DB_PORT=3306
MALL4J_DB_USER=...
MALL4J_DB_PASSWORD=...
MALL4J_DB_NAME=yami_shops

# AI 功能（可选，不填就退回本地规则模式）
AI_BASE_URL=
AI_API_KEY=
AI_MODEL=
```

> **`.env` 已在 `.gitignore` 里，不会被提交。**
> `configs/config.yaml` 里全部是 `${变量名:默认值}` 占位符，**不要把密码写进 config.yaml** —— 那个文件要进版本库。

### 2.2 确认被测环境

`configs/config.yaml` 里：

```yaml
default_env: test          # 默认环境；要测别的环境用 --env

projects:
  mall4j:
    pre:
      host: "${MALL4J_PRE_HOST:http://.../_admin}"
```

环境优先级：命令行 `--env` > 环境变量 `APITEST_ENV` > `default_env`。

> ⚠️ **别把测试环境指向生产地址**。框架会检测并大声警告，但最终要靠你确认。

### 2.3 自检

```powershell
.venv\Scripts\python.exe scripts\run_unit.py      # 框架单测，不联网，约 20 秒
.venv\Scripts\python.exe scripts\run.py --list    # 用例能否正常收集
```

单测全绿 + 能列出用例 = 环境 OK。

---

## 三、第一次跑起来

```powershell
# 看有哪些用例（不执行）
.venv\Scripts\python.exe scripts\run.py --list

# 看有哪些标签
.venv\Scripts\python.exe scripts\run.py --marks

# 先跑冒烟（最快确认登录 + 业务链路通不通）
.venv\Scripts\python.exe scripts\run.py -m smoke

# 跑某个模块
.venv\Scripts\python.exe scripts\run.py --module admin_area

# 跑全部
.venv\Scripts\python.exe scripts\run.py
```

报告默认出 allure；想双击就看用：

```powershell
.venv\Scripts\python.exe scripts\run.py --report html
# 打开 reports\report.html
```

终端最后会打印通过 / 失败 / 跳过数量和失败归因摘要。

---

## 四、看懂用例从哪来

用例来源由 `configs/config.yaml` 控制，**一次只启用一种**：

```yaml
case_source:
  mode: excel          # excel / yaml / both
  excel_dir: "testcases/cases_excel"
  yaml_dir: "testcases/cases"
```

当前默认 **Excel 模式**，只跑 `testcases/cases_excel/`；`testcases/cases/` 里只有 1 个 demo 文件，不会被跑。

临时切换（不改配置文件）：

```powershell
python scripts\run.py --case-source yaml --list
python scripts\run.py --case-source both
```

目录约定：

```
testcases/cases_excel/<项目>/<模块>/
├── module.yaml            # 模块说明、标签、执行顺序
└── <模块>.xlsx            # 用例本体
```

模块顺序由 `configs/modules.yaml` 的 `order` 决定（数字小的先跑，登录相关排最前）。
**不要在 `module.yaml` 里写 `order`** —— 它的优先级高于全局配置，会把整体顺序盖掉。

---

## 五、写一条用例（Excel）

### 5.1 生成模板

```powershell
# 给默认项目（mall4j）建模块
.venv\Scripts\python.exe scripts\new_case.py --module order_order

# 给别的项目建
.venv\Scripts\python.exe scripts\new_case.py --module order_order --project demo_shop
```

会在 `testcases/cases_excel/<项目>/<模块>/<模块>.xlsx` 生成模板，含四张表：

| 表 | 填什么 |
|---|---|
| 模块信息 | 模块名、报告显示名、执行顺序 |
| 接口定义 | 一个接口一行：路径、方法、请求头、参数、断言 |
| 测试用例 | 一条用例一行：关联接口、用例名、场景、参数组、状态 |
| 说明 | 断言类型和变量写法速查 |

> 表头有**批注**，鼠标悬停就能看到每列怎么填；「请求方法」列有下拉。
> 已存在的文件不会被覆盖，要重建加 `--force`。

### 5.2 关键约定

**① 接口定义和测试用例分开**：一个接口写一次，多条用例复用它。

**② 请求头 / Query 参数：一行一个「键: 值」**

```
Authorization: {{mall4j.token}}
Accept: application/json
```

**③ 请求体：填 JSON**

```json
{"areaName": "自动化测试省", "parentId": 110000000000, "level": 2}
```

**④ 断言：一行一条「类型 | 字段 | 期望值」**

```
equal | code | 00000
equal | success | true
exists | result
```

**⑤ 用例行的参数 / 断言会覆盖接口定义里的** —— 同一个接口测不同入参不用改接口定义。

**⑥ 用例状态**

| 值 | 含义 | CI 跑吗 |
|---|---|---|
| `debugged` | 调试通过 | ✅ |
| `draft` | 未调试 | ❌ |
| `disabled` | 临时下线 | ❌ |
| `broken` | 已知有问题 | ❌ |

> Excel 用例**留空状态列 = 按 `debugged` 处理**（人工维护的源就是要跑的）。
> YAML 用例相反：不写 `status` 默认 `draft`，本地调试要加 `--run-draft`。

**⑦ 不要填「项目」列** —— 项目层由框架按环境解析，手填的会被忽略。

### 5.3 用例命名

统一格式：`业务名 - 场景`，例如：

```
地区管理 - 修改地区（正常请求）
热搜词 - 删除热搜词（关联数据不存在时失败）
```

反例必须在名称或标签里明确标出「反向 / 异常 / 非法」，质量闸门会检查。

---

## 六、前后置：造数、查 id、清理

这是本项目最重要的一条约定：**正例自己造数据、自己清理，且只能删自己造的那条。**

### 6.1 标准链路（以「修改地区」为例）

```jsonc
setups: [
  { "id": "建数据", "operation_type": "api", "path": "/admin/area", "method": "POST",
    "data": { "areaName": "{{tools.fixed('mall4j_admin_area_update_seed', '自动化测试省改前')}}", ... } },
  { "id": "查数据", "operation_type": "api", "path": "/admin/area/list", "method": "GET" }
],
steps: [
  { "id": "API-168", "path": "/admin/area", "method": "PUT",
    "data": { "areaId": "{{tools.find_id_by_fixed(查数据.data, 'areaName', 'mall4j_admin_area_update_seed', 'areaId', '自动化测试省改前')}}", ... } }
],
teardowns: [
  { "id": "清理数据", "path": "/admin/area/{{tools.find_id_by_fixed(...)}}", "method": "DELETE" }
]
```

三个工具函数是核心：

| 函数 | 作用 |
|---|---|
| `tools.fixed(键, 值)` | 生成一个固定值（同一轮内可复现），用来造数和回查 |
| `tools.find_id_by_fixed(查数响应, 名字字段, 键, id字段, 值)` | 从列表响应里按名称找到自己造的那条记录的 id |
| `tools.fresh_uuid()` | 抓一个全新滑块 uuid（登录用例用） |

> **为什么不能写死 id**：`DELETE /admin/area/1` 删的是真实基础数据，后台会瘫。
> 安全闸会拦下来，用例直接失败。

### 6.2 执行顺序

```
登录（按需）
  → setups（按顺序）
  → steps（主步骤）
  → 断言
  → teardowns（按顺序，无论主步骤成败都执行）
```

前置失败时主步骤不执行，但后置仍会跑（清理已造的数据）。
**前置 / 后置的断言放在 fixture 流程里，不要写在 teardown 里做"验证"** ——
teardown 只负责清理，验证逻辑放主步骤。

---

## 七、断言怎么写

完整类型表见 [ASSERT.md](ASSERT.md)。日常最常用的：

| 目的 | 写法（Excel「类型 \| 字段 \| 期望」） |
|---|---|---|
| 业务码 | `equal \| code \| 00000` |
| 成功标记 | `is_true \| success` |
| 字段存在（可为 null） | `exists \| result.accessToken` |
| 字段非空 | `is_not_none \| result.username` |
| 数值范围 | `lte \| result.total \| 1000` |
| 部分匹配 | `subset \| result \| {"status": 1}` |
| 正则 | `matches \| result.orderNo \| ^SO\d{14}$` |
| HTTP 状态 | `status_code \| \| 200` |
| 耗时 | `elapsed_time \| \| {"lte": 3000}` |

**几条硬规则**：

- ❌ 只断言 `status_code + success` 的用例会被质量闸门标记为「薄用例」；
  至少要有一条业务字段断言。
- ❌ **不许为了让用例变绿而放宽断言**。断言只能变强：删断言或把比较符改松会被写用例工具直接拒绝。
- ✅ 浮点数用 `approx_equal`，不要用 `equal`。
- ✅ 区分 `exists`（字段在不在）和 `is_not_none`（值是不是 null）。

---

## 八、查库断言

接口返回 `code=00000` 只说明服务端没抛异常，**不代表数据真落库了**。

在断言行里写：

```
db_query_exists | SELECT area_id FROM tz_area WHERE area_name = %s | | ["{{...}}"]
db_query_count  | SELECT id FROM tz_area WHERE area_name = %s | 1 | ["..."]
db_query_not_exists | SELECT area_id FROM tz_area WHERE area_id = %s | | [123]
```

- SQL 用 `%s` 占位，参数放第四格的 JSON 数组（**不要自己拼字符串**）；
- `alias` 指向 `config.yaml` 里 `db.<别名>`，mall4j 用 `alias: mall4j`；
- 表名带 `tz_` 前缀（mall4j 的库是 `yami_shops`）。

**写落库断言前先看表结构**：

```powershell
.venv\Scripts\python.exe tools\maint\dump_schema.py --alias mall4j --list
.venv\Scripts\python.exe tools\maint\dump_schema.py --alias mall4j --filter area
```

> 查库断言必须开 autocommit 读最新数据，框架已处理；如果你自己写脚本查库，
> 记得也别用一个长事务的快照 —— 否则会出现"接口成功了，脚本却说没落库"。

---

## 九、参数化

一条用例跑多组数据。Excel 里在「测试数据」表加一个数据组：

```
数据组   | 场景名   | searchType
统计周期 | 近七天   | LAST_SEVEN
统计周期 | 近三十天 | LAST_THIRTY
```

「测试用例」表的**数据组**列填 `统计周期`，这一条就会展开成两条，报告里分别显示场景名。

请求里引用参数：`{{searchType}}` 或 `{{params.searchType}}`。

YAML 里两种写法：

```yaml
# 维度字典：自动笛卡尔积
searchType: [LAST_SEVEN, LAST_THIRTY]
pageSize: [10, 20]

# 场景列表：每组起中文名
- name: 近七天
  data: {searchType: LAST_SEVEN}
```

只想跑其中一组：

```powershell
python scripts\run.py --list -k "近七天"
python scripts\run.py -k "近七天"
```

---

## 十、标签与状态

### 10.1 标签（mark）

标签来自两处，会自动合并：

| 位置 | 写法 | 范围 |
|---|---|---|
| `<模块>/module.yaml` | `marks: [admin_area, admin]` | 整个模块 |
| 单条用例 | 「用例标签」列 / YAML `marks` | 单条 |

当前可用标签用 `--marks` 查看。常用：

```
smoke       冒烟（15 条）
p0 / p1 / p2 / p3   优先级
admin / prod / shop / sys / order   业务域
<模块名>    按模块
```

跑法支持 pytest 表达式：

```powershell
python scripts\run.py -m smoke
python scripts\run.py -m "smoke or p0"
python scripts\run.py -m "admin and not p2"
```

### 10.2 状态迁移

改状态用维护工具，不要手改 Excel 的状态列（会漏校验）：

```powershell
.venv\Scripts\python.exe tools\maint\case_status.py --help
```

> 通过智能体把用例转成 `debugged` 时，框架会核对「这条用例确实跑通过」的凭据；
> 没跑过、或跑通后又改过用例，都会被拒绝。**正常做法是重新跑一遍。**

---

## 十一、跑测试的常用姿势

```powershell
# 基础筛选
python scripts\run.py --list                         # 只列不跑
python scripts\run.py --project mall4j               # 按项目
python scripts\run.py --module admin_area            # 按模块（可重复）
python scripts\run.py --case-file testcases\...\x.xlsx
python scripts\run.py --case-id API-168              # 精确到一条
python scripts\run.py -k "新增地区"                  # 关键字（支持中文）
python scripts\run.py -m smoke                       # 标签

# 状态
python scripts\run.py --run-draft                    # 连 draft / broken 一起跑（本地调试）
python scripts\run.py --case-status debugged         # 只跑某状态（CI 入口支持）

# 环境与报告
python scripts\run.py --env pre
python scripts\run.py --report none                  # 不出报告，最快
python scripts\run.py --report html
python scripts\run.py --report allure

# 登录缓存
python scripts\run.py --show-cache                   # 看当前 token 缓存
python scripts\run.py --clear-cache                  # 清掉
python scripts\run.py --no-cache                     # 这次不用缓存

# 日志
python scripts\run.py --log-cli-level=DEBUG          # 终端看请求明细
```

**CI 统一入口**：

```powershell
python ci\run_ci.py --env pre --mark smoke --report both
python ci\run_ci.py --env pre --dry-run              # 只看会跑什么
python ci\run_ci.py --env pre --workers 4            # 并行（需 pytest-xdist）
```

退出码：`0` 全过 / `1` 有用例失败 / `2` 脚本自身出错。

---

## 十二、看报告

### 12.1 allure（信息最全）

```powershell
python scripts\run.py --report allure
```

跑完会自动起本地 HTTP 服务并打开浏览器，报告在 `reports/allure_<时间戳>/index.html`。

> ⚠️ **不要直接双击 index.html**，`file://` 下会空白（前端要 fetch 本地 json）。
> 之后再看某一份：

```powershell
python scripts\serve_report.py
python scripts\serve_report.py reports\allure_20261111_093000
python scripts\serve_report.py --port 8888
```

报告里能看到的层级：

```
mall4j 管理后台          ← 项目
└── 地区管理              ← 模块
    └── 修改地区          ← 场景
        └── 地区管理 - 修改地区（正常请求）
            ├── Set up：建数据 / 查数据
            ├── 请求 / 响应（分附件，已脱敏）
            ├── 断言明细
            ├── Tear down：清理数据
            └── 失败归因（若失败）
```

### 12.2 HTML（最省事）

```powershell
python scripts\run.py --report html
```

`reports/report.html` 双击可看，表格带「模块 / 场景 / 用例」三列。

### 12.3 机器可读

- `reports/result.json` —— 本次运行汇总（CI / 调度器读它）
- `reports/ci_summary.json` —— CI 汇总（`ci/run_ci.py` 写）

---

## 十三、常见错误与排查

| 现象 | 原因 / 处理 |
|---|---|
| `python` 没反应、退出码 49 | 用了系统商店的 python 占位符。改用 `.venv\Scripts\python.exe` |
| `fixture 'apienv' not found` | conftest 没有用 `pytest_plugins` 加载框架模块；确认从项目根跑 |
| 登录失败 / 抓不到 uuid | 八成是 `MALL4J_PASSWORD` 没配 —— 前端没密码不弹滑块 |
| 滑块识别偏了 | 看 `data/debug/` 下三张标注图；复杂纹理可能重试才能过 |
| `80204 验证码已失效` | uuid 被消费，换新的（框架会自动处理） |
| `1003 访问过于频繁` | 服务端限流，等一会儿；框架已内置退避重试 |
| 业务接口 403 未登录 | 鉴权头写错：业务接口用 `Authorization: {{mall4j.token}}`，别用 `Bearer` |
| 一条用例都没跑 | 「用例状态」不是 `debugged`（YAML 默认 draft）；加 `--run-draft` |
| 筛选后 0 条收集 | `--mark` / `--module` 写错。框架会在收集阶段直接报错（不会假装全过） |
| 一条挂了后面全没跑 | 检查是否给 pytest 加了 `--maxfail=1`，删掉 |
| 查库断言说没数据但接口成功 | 数据库连接是长事务快照，没读到最新提交；确认 autocommit |
| allure 报告空白 | 别用 `file://` 打开；用 `serve_report.py`。或 allure 命令行版本太老（<2.20） |
| `A00005` / 环境性报错 | 先确认是环境问题还是用例问题 —— 看失败归因，环境问题不要改用例 |

**排障顺序建议**：先看失败归因 → 再看 allure 里的请求/响应附件 → 再看 `logs/YYYY-MM-DD.error.log`。

---

## 十四、维护工具速查

```powershell
# 表结构
python tools\maint\dump_schema.py --alias mall4j --list
python tools\maint\dump_schema.py --alias mall4j -o docs\schema.md

# 盘点用例质量
python tools\maint\survey_assertions.py          # 断言强度
python tools\maint\audit_db_assertions.py        # 写操作缺落库断言
python tools\maint\audit_quality.py              # 按质量闸门全量体检
python tools\maint\audit_endpoints.py            # 用例路径 vs 服务端 swagger

# 加强断言
python tools\maint\strengthen_assertions.py
python tools\maint\add_db_assertions.py

# 数据维护
python tools\maint\fix_detail_ids.py             # 详情用例里写死的失效 id
python tools\maint\clean_dirty_rows.py           # 清理库里未渲染的模板脏行

# 导入接口文档成用例
python -m tools.importer.cli preview data\importer_demo\openapi_demo.json
python -m tools.importer.cli export data\importer_demo\openapi_demo.json --project mall4j

# YAML ↔ Excel
python scripts\to_excel.py --module admin_area
python scripts\to_excel.py --all
```

> 维护脚本大多支持 `--dry-run`，先看它要改什么再落盘。

---

## 十五、用 AI / 智能体驱动

### 15.1 在 Claude Code 里

项目已带 `.mcp.json` 和 `.claude/settings.json`，重启会话后自动连上 MCP 服务。然后直接说人话：

```
跑一下 mall4j 的 admin_hotSearch 模块，失败了告诉我为什么
```

或用 skill：

```
/apitest-run        跑用例 + 归因
/apitest-author     写用例
/apitest-heal       自愈失败用例
/apitest-coverage   覆盖率分析
/apitest-design     测试设计
```

### 15.2 智能体能做什么、不能做什么

| 能做 | 约束 |
|---|---|
| 查模块 / 用例 / 接口 / 表结构 | 只读，随便查 |
| 跑用例 | 必须带筛选条件；单次 ≤50 条、单会话累计 ≤50 条 |
| 诊断失败、给修复计划 | 只读，按归因给允许的动作 |
| 改用例（提案 → 落盘 → 重跑验证） | 只能改用例数据问题；断言只能变强；写前备份、失败回滚 |
| 改真实业务数据 | **没有这个能力**，也不打算有 |

### 15.3 自检与评测

```powershell
.venv\Scripts\python.exe -m ai.mcp_server.selfcheck          # 起真实子进程走一遍协议
.venv\Scripts\python.exe scripts\run_eval.py --dry-run       # 校验评测集格式
.venv\Scripts\python.exe scripts\run_eval.py                 # 跑评测
```

### 15.4 不配 LLM key 也能用

用例生成退回本地模板，诊断退回规则分类。配了 key（`.env` 的 `AI_*`）才走模型。

---

## 十六、日常清理与目录约定

### 16.1 哪些是运行产物（可随时删）

| 目录 | 内容 | 自动清理 |
|---|---|---|
| `logs/` | 按天日志 | 保留 7 天（`APITEST_LOG_KEEP_DAYS`） |
| `reports/` | allure 目录 | 保留最近 10 份（`APITEST_REPORT_KEEP`） |
| `.cache/` | token 缓存、依赖结果、知识图谱、备份、审计日志 | 部分自动，可手工清 |
| `data/debug/` | 滑块调试图 | 手工清 |
| `__pycache__/`、`.pytest_cache/` | Python 缓存 | 可随时删 |

手工全清（不影响源码和用例）：

```powershell
Remove-Item logs\*, reports\* -Recurse -Force
Remove-Item .cache\.cache -Recurse -Force
Get-ChildItem -Recurse -Directory -Filter __pycache__ | Remove-Item -Recurse -Force
```

### 16.2 `.cache/` 里哪些删了会丢东西

| 路径 | 删了会怎样 |
|---|---|
| `.cache/apidoc/openapi.json` | 接口搜索 / 知识图谱不可用，重新同步文档即可 |
| `.cache/schema*/` | 表结构快照失效，`dump_schema.py` 重新导出即可 |
| `.cache/knowledge/graph.db` | 知识图谱重建即可（`rebuild_knowledge`） |
| `.cache/backup/` | 历史改动备份丢失（只影响回溯） |
| `.cache/verify_ledger.json` | 「跑通过」凭据丢失，用例转 debugged 需要重跑 |
| `.cache/agent_audit.jsonl` | 智能体调用审计记录丢失 |

### 16.3 不要把什么提交进版本库

`.gitignore` 已覆盖：`logs/`、`reports/`、`.cache/`、`.env`、`__pycache__`、`.pytest_cache/`、
`.idea/`、`.vscode/`、`*.bak`。提交前确认 `git status` 里没有这些。

### 16.4 新增一个被测系统

1. `configs/config.yaml` 的 `projects` 下加项目档案（host / 登录方式 / 鉴权头）；
2. 需要自定义登录就写一个 `adapters/<项目>.py`；
3. `testcases/cases_excel/<项目>/<模块>/` 下建用例目录；
4. `.env` 里加该项目的账号密码变量；
5. `python scripts\run.py --project <项目> --list` 验证收集。
