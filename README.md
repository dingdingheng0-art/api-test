# 接口自动化测试平台（api-test）

面向 mall4j 管理后台的**数据驱动接口自动化测试平台**：用例写在 Excel / YAML 里，
pytest 负责执行，登录态、参数化、前后置清理、查库断言、报告、失败归因由框架统一完成。
平台同时提供 **AI 能力层**（用例生成、失败诊断、知识图谱、自愈闭环）和 **MCP 服务**，
可以接 Claude Code / Cursor 等智能体用自然语言驱动。

> 详细文档见 [docs/](docs/)：项目介绍 [PROJECT_INTRO.md](docs/PROJECT_INTRO.md)、
> 使用手册 [USER_GUIDE.md](docs/USER_GUIDE.md)、框架全量手册 [FRAMEWORK.md](docs/FRAMEWORK.md)。

---

## 一句话上手

```powershell
# 1. 装依赖（Python 3.12+）
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m playwright install chromium

# 2. 配账号密码（复制样例后填真实值，别写进 config.yaml）
copy configs\.env.example .env

# 3. 自检 → 跑用例
.venv\Scripts\python.exe scripts\run_unit.py     # 框架单测，不联网，约 20 秒
.venv\Scripts\python.exe scripts\run.py --list   # 看有哪些用例
.venv\Scripts\python.exe scripts\run.py          # 跑全部
```

跑完报告在 `reports/`（HTML 双击可看；allure 需用 `scripts/serve_report.py` 起服务）。

## 能做什么

| 能力 | 说明 |
|---|---|
| **零代码写用例** | Excel 四张表填完即用；也支持 YAML、Swagger/OpenAPI/HAR/cURL 导入 |
| **自动登录** | mall4j 走「拼图滑块识别 → 校验 → 加密登录」，拿到真实 JWT，用例里只写 `{{mall4j.token}}` |
| **前后置清理** | 用例自带 `setups / teardowns`，正例造数据→查 id→执行→按 id 精确删除，不误删真实数据 |
| **查库断言** | 接口返回 200 不等于真落库，`db_query_*` 断言直连业务库验证 |
| **失败归因** | 自动区分「环境 / 用例数据 / 服务缺陷 / 脚本」四类，避免把环境问题当成用例写错 |
| **多层报告** | allure 三层业务树（项目 → 模块 → 场景）、HTML 报告、CI 汇总 JSON |
| **质量闸门** | 断言加强、命名规范、清理安全性、用例状态机，写入口自动校验 |
| **AI + 智能体** | 27 个 MCP 工具：跑用例、诊断失败、查表结构、自愈闭环；不配 LLM key 也能用规则模式 |

当前仓库：**24 个模块 / 110 条可执行用例**，另有 22 条处于 disabled / broken 状态。

## 目录结构

```
api-test/
├── scripts/       入口脚本（run.py 跑接口用例、run_unit.py 跑单测、new_case.py 建模板）
├── testcases/     用例层：cases/（YAML）、cases_excel/（Excel）、加载与执行逻辑
├── framework/     pytest 插件层：收集、钩子、fixture、报告
├── utils/         能力层：http / assertions / cases / excel / config / logs / slider / infra
├── verify/        校验层：数据库 / Redis / MQ 断言
├── factory/       测试数据工厂与兜底清理
├── adapters/      被测系统适配（mall4j 登录：AES + 滑块）
├── ai/            AI 层：用例生成、缺陷诊断、知识图谱、智能体工具、MCP 服务、评测集
├── tools/         维护工具：文档导入（importer）、用例盘点/加固（maint）
├── unit_tests/    框架自身单测（不联网，1260 条）
├── ci/            CI 入口（run_ci.py + Jenkinsfile）
├── configs/       config.yaml / modules.yaml / factories.yaml
├── schemas/       用例 JSON Schema
├── docs/          全部文档
├── logs/ reports/ 运行产物（已 gitignore，自动按天/按份数清理）
└── conftest.py    pytest 入口，加载 framework 各层
```

## 常用命令

```powershell
# 列用例 / 看标签
python scripts/run.py --list
python scripts/run.py --marks

# 按条件跑
python scripts/run.py --module admin_area
python scripts/run.py -m smoke
python scripts/run.py -k "新增地区"
python scripts/run.py --case-id API-168

# 报告
python scripts/run.py --report html             # reports/report.html
python scripts/run.py --report allure           # reports/allure_<时间戳>/，自动起服务
python scripts/serve_report.py                  # 再看最近一份 allure

# 维护 / CI
python scripts/run_unit.py                      # 框架单测
python scripts/new_case.py --module order_order # 生成 Excel 模板
python ci/run_ci.py --env pre --mark smoke      # CI 统一入口
```

## 环境要求

- **Python 3.12+**（必须用项目内 `.venv/Scripts/python.exe`；系统 PATH 上的 `python` 可能是商店占位符，会静默退出）
- Chromium 浏览器内核（`playwright install chromium`，滑块识别用）
- 可选：`mysql-connector-python`（查库断言）、`redis`（缓存断言）、`pika`（MQ 断言）、
  allure 命令行（漂亮的 allure 报告；没有也能出 HTML 报告）

## 文档导航

| 文档 | 内容 |
|---|---|
| [docs/PROJECT_INTRO.md](docs/PROJECT_INTRO.md) | 项目介绍：定位、架构、核心设计、AI 能力、质量体系 |
| [docs/USER_GUIDE.md](docs/USER_GUIDE.md) | 用户使用手册：从安装到写用例、跑测试、看报告、排障 |
| [docs/FRAMEWORK.md](docs/FRAMEWORK.md) | 框架全量参考手册（用例写法、断言、参数化、登录、报告） |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 分层结构与一次运行的调用链路 |
| [docs/ASSERT.md](docs/ASSERT.md) | 断言类型速查 |
| [docs/design/](docs/design/) | 设计记录：AI 流水线、Allure 质量方案、滑块登录实现 |
| [docs/reference/](docs/reference/) | 平台数据库表结构参考 |

## 安全约定

- 密码 / token 只放 `.env` 或环境变量，`configs/config.yaml` 里全部是 `${VAR:}` 占位符
- 日志与报告附件自动脱敏（token / 密码 / 手机号 / 身份证）
- 写操作走安全闸：DELETE / PUT / POST 命中保护路径时，目标 id 必须落在安全区间（默认 `900000000000-999999999999`）
- 报告 / 日志 / `.cache` 运行产物不进版本库
