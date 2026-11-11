# 文档索引

| 文档 | 读者 | 内容 |
|---|---|---|
| [PROJECT_INTRO.md](PROJECT_INTRO.md) | 所有人 | **项目介绍**：定位、解决的问题、架构、核心设计、AI 能力、质量体系、适用边界 |
| [USER_GUIDE.md](USER_GUIDE.md) | 使用者 | **使用手册**：安装配置、写用例、前后置、断言、查库、参数化、跑测试、看报告、排障 |
| [FRAMEWORK.md](FRAMEWORK.md) | 写用例 / 改框架 | 框架全量参考：用例语法、变量、断言、登录步驟、参数化、配置、报告、日志、MCP |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 改框架 | 分层结构、一次运行的完整调用链路、依赖方向约束 |
| [ASSERT.md](ASSERT.md) | 写用例 | 断言类型速查与易错点 |

## 设计记录（docs/design/）

| 文档 | 内容 |
|---|---|
| [AI用例生成流水线设计.md](design/AI用例生成流水线设计.md) | AI 生成流水线的十个环节、闸门、数据构造与断言生成约定 |
| [Allure报告与用例质量优化方案.md](design/Allure报告与用例质量优化方案.md) | allure 报告改造与用例质量优化的实施记录 |
| [ENHANCEMENTS.md](design/ENHANCEMENTS.md) | 增强能力手册：查库/缓存/MQ 断言、数据工厂、重试、限流、AI 生成、CI |
| [滑块登录实现说明.md](design/滑块登录实现说明.md) | mall4j 登录链路、滑块识别的三个坑、uuid 语义、鉴权头 |

## 参考（docs/reference/）

| 文件 | 内容 |
|---|---|
| [TEST_PLATFORM_SCHEMA.md](reference/TEST_PLATFORM_SCHEMA.md) | 平台数据库（`test_platform`）表结构说明 |
| [TEST_PLATFORM_SCHEMA.json](reference/TEST_PLATFORM_SCHEMA.json) | 同上，机器可读版本 |

## 代码内的说明文档

| 位置 | 内容 |
|---|---|
| [../ai/agent/README.md](../ai/agent/README.md) | 智能体工具契约、已注册工具、加新工具的步骤与设计约束 |
| [../tools/README.md](../tools/README.md) | 维护与导入工具总览 |
| [../tools/importer/README.md](../tools/importer/README.md) | Swagger / OpenAPI / HAR / cURL 导入用法 |

> 运行产物在 `logs/`、`reports/`、`.cache/`，都已在 `.gitignore` 里，可按需清理（见 USER_GUIDE 第十六节）。
