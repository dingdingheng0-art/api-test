# 项目结构与模块关系

> 这份文档回答两个问题：**代码怎么分层的**、**一次跑用例时谁调谁**。
> 具体怎么写用例看 [FRAMEWORK.md](FRAMEWORK.md)，断言类型看 [ASSERT.md](ASSERT.md)。

---

## 一、分层总览

```
┌────────────────────────────────────────────────────────────────────────────┐
│  入口层   scripts/                                                         │
│  run.py（接口用例）  run_unit.py（框架单测）                               │
│  new_case.py / to_excel.py / serve_report.py（辅助）                       │
└──────────────┬─────────────────────────────────────────────────────────────┘
                               │
┌──────────────▼─────────────────────────────────────────────────────────────┐
│  执行层   testcases/  +  framework/                                        │
│                                                                            │
│  framework/   pytest 插件：收集用例、命令行参数、hooks、fixtures、报告     │
│  testcases/   用例加载（YAML/Excel）→ 执行用例 → 三层报告结构              │
│               覆盖率统计（口径建立在「用例怎么加载」之上，所以在这层）     │
└──────────────┬─────────────────────────────────────────────────────────────┘
                               │
┌──────────────▼─────────────────────────────────────────────────────────────┐
│  能力层   utils/  verify/  factory/  adapters/                             │
│                                                                            │
│  utils/http        发请求（限流 / 重试 / 安全闸 / 日志）                   │
│  utils/assertions  断言执行                                                │
│  utils/cases       用例校验、来源开关、依赖、参数展开、变量解析、模块清单  │
│  utils/excel       Excel 用例读取                                          │
│  utils/config      配置读取、项目档案、密钥脱敏                            │
│  utils/logs        日志（按天分文件 + 脱敏）                               │
│  utils/slider      滑块缺口识别（登录前置）                                │
│  utils/infra       限流 / 并行 / 缓存 / 安全闸 / 归因 / 通知               │
│  verify/           查库 / 查缓存 / 查 MQ 断言                              │
│  factory/          测试数据工厂 + 兜底清理                                 │
│  adapters/         被测系统登录适配                                        │
└──────────────┬─────────────────────────────────────────────────────────────┘
                               │
┌──────────────▼─────────────────────────────────────────────────────────────┐
│  数据与配置   testcases/cases_excel/  testcases/cases/  configs/           │
│              testcases/apis/（接口定义）  testcases/mocks/（Mock 表）      │
└────────────────────────────────────────────────────────────────────────────┘

旁路：
  ai/        AI 能力层（用例生成 / 缺陷分析 / 智能体工具 / MCP 服务）
  tools/     维护工具（接口文档导入、断言加强、盘点、导表结构）
  ci/        CI 统一入口
```

**依赖方向是单向的**：入口 → 执行 → 能力 → 数据。
`utils/` 里不允许反向 import `testcases/`；`ai/` 和 `tools/` 只能调用能力层，不能反过来被调用。

---

## 二、一次 `python scripts/run.py` 的执行链路

```
scripts/run.py
  │
  ├─ 1. 读配置        utils/config/config_manager.py  ← configs/config.yaml
  ├─ 2. 定用例来源    utils/cases/case_source.py      ← excel / yaml / both
  ├─ 3. 拼 pytest 参数，起 pytest（子进程内）
  │
  └─ pytest 启动 → conftest.py 加载 pytest_plugins
        │
        ├─ framework/pytest_plugin.py
        │     注册命令行参数（--apienv / --case-project / --case-module / --case-file）
        │     收集用例 → testcases/case_loader.py 扫目录
        │       ├─ Excel：utils/excel/{reader,cells,parser,flat_parser,loader}.py
        │       └─ YAML ：yaml.safe_load + 接口定义展开
        │     参数化    → utils/cases/param_expander.py
        │     模块排序  → utils/cases/module_manager.py + configs/modules.yaml
        │
        ├─ framework/hooks.py
        │     session 开始：建目录、准备 allure、清过期依赖结果
        │     session 结束：统计 → reports/result.json + 终端汇总
        │
        ├─ framework/fixtures.py
        │     按项目准备 api_client / 登录态
        │
        └─ testcases/test_cases.py  ← 每条用例落在这里
               └─ testcases/case_runner.py  ← 核心执行器
                     │
                     ├─ setups     变量解析 → utils/cases/variable_resolver.py
                     │             造数据   → factory/
                     ├─ 每个 step  构造请求 → utils/http/request_handler.py
                     │             ├─ 安全闸   utils/infra/safety_guard.py
                     │             ├─ 限流     utils/infra/rate_limiter.py
                     │             ├─ 鉴权头   utils/config/project_profile.py
                     │             └─ 失败重试 framework/plugins/retry_plugin.py
                     ├─ 断言        utils/assertions/assert_handler.py
                     │             └─ 查库/缓存/MQ → verify/manager.py
                     ├─ teardowns   factory/cleanup.py 兜底清理
                     └─ 结果        framework/allure_util.py → reports/
```

---

## 三、模块职责速查

| 目录 | 职责 | 不该放什么 |
|---|---|---|
| `scripts/` | 只有入口和参数解析，薄壳 | 业务逻辑、请求封装 |
| `testcases/` | 用例加载、执行编排、报告层级 | 具体的 HTTP 细节、断言实现 |
| `framework/` | pytest 生命周期：收集、参数、hooks、fixture、报告 | 用例数据、被测系统知识 |
| `utils/` | 可复用能力，**不含业务、不含用例格式知识** | 某个项目的硬编码路径/账号、依赖 testcases 的统计 |
| `verify/` | 只负责"断言时去查什么"（库/缓存/MQ） | 造数据 |
| `factory/` | 造数据 + 登记清理 | 断言 |
| `adapters/` | 被测系统的登录/鉴权适配（每个系统一个文件） | 通用请求逻辑 |
| `ai/` | 用例生成、失败归因、MCP 工具封装 | 改业务逻辑（只做包装） |
| `tools/` | 一次性/周期性维护工具 | 被 `run.py` 依赖 |

---

## 四、扩展点

| 你要加什么 | 改哪里 | 不用改什么 |
|---|---|---|
| 接一套新的被测系统 | `configs/config.yaml` 的 `projects:` + `adapters/<系统>.py`（登录方式特殊时） | framework、testcases |
| 加一类断言 | `utils/assertions/assert_types.py` 登记类型 + `assert_handler.py` 实现 | 用例执行器 |
| 加一个造数据工厂 | `factory/<项目>.py` + `configs/factories.yaml` 登记 | 用例本身 |
| 加一个 pytest 插件 | `framework/plugins/<名字>.py` + 根 `conftest.py` 的 `pytest_plugins` | 其它层 |
| 给智能体加一个工具 | `ai/agent/tools/<名字>.py` + `ai/agent/tools/__init__.py` 注册 | MCP 服务端（自动透出） |
| 加一份配置项 | `configs/config.yaml` + `utils/config/config_manager.py` 的默认值 | 调用方 |

---

## 五、两条硬规则

1. **路径只从 `utils/paths.py` 取**，不要各层自己 `Path(__file__)` 拼。
2. **`utils/` 不许 import `testcases/` / `framework/` / `scripts/`**，否则分层就塌了
   （`unit_tests/test_framework_integration.py` 会检查这一点）。

   想让 `utils/` 也用到上层的能力时，**不要偷偷加懒加载 import**，二选一：

   - **挪到该去的层**：只对"用例文件长什么样"有意义的（统计覆盖率、模块清单），
     就住在 `testcases/` / `utils/cases/` 这一层；`utils/cases/` 只管配置与格式，
     不碰用例扫描
   - **回调注入**：能力层只定义 `Callable` 字段，由上层在建对象时塞进去
     （例：`AssertHandler.evidence_sink`，报告怎么贴由执行层决定）

---

## 六、命令速查

```powershell
python scripts/run.py --list          # 列出所有用例
python scripts/run.py -m smoke        # 只跑冒烟
python scripts/run_unit.py            # 框架单元测试（不联网）
python -m ai.mcp_server               # 起 MCP 服务（给 Claude Code / Cursor）
python -m tools.importer.cli --help   # 接口文档导入
python tools/maint/dump_schema.py --alias mall4j --format json   # 导表结构
```
