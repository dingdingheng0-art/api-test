# tools/ — 维护与导入工具

这些脚本**不是测试用例**，是维护用例、导入接口文档时用的工具。
跑测试用例请用 `python scripts/run.py`（接口）或 `python scripts/run_unit.py`（框架单测）。

> 所有脚本都从**项目根目录**运行，路径以根目录为基准。

---

## importer/ — 接口文档 → 用例

把 Swagger / OpenAPI / HAR / cURL 解析成框架的 YAML 用例或 Excel 用例。

| 脚本 | 干什么 | 怎么跑 |
|---|---|---|
| `cli.py` | 命令行入口：预览 / 导出 | `python -m tools.importer.cli preview api.json` |
| `dispatcher.py` | 自动识别文档格式并分派给对应解析器 | 被 cli 调用 |
| `openapi_parser.py` | OpenAPI 3 / Swagger 2 解析 | 被 dispatcher 调用 |
| `har_parser.py` | 浏览器 HAR 包解析 | 被 dispatcher 调用 |
| `curl_parser.py` | cURL 命令解析 | 被 dispatcher 调用 |
| `case_builder.py` | 接口列表 → 用例结构 / 写 YAML / 导出 Excel | 被 cli 调用 |
| `web.py` | 本地网页上传页（拖文件 → 预览 → 一键生成） | `python -m tools.importer.cli web` |
| `test_importer.py` | 导入器的单测 | `python -m pytest tools/importer/test_importer.py` |

示例文档在 `data/importer_demo/`。

---

## maint/ — 用例与数据维护

| 脚本 | 干什么 | 怎么跑 |
|---|---|---|
| `strengthen_assertions.py` | 按 OpenAPI 快照给用例补业务码 + 字段存在断言 | `python tools/maint/strengthen_assertions.py --dry-run` |
| `add_db_assertions.py` | 给新增/修改/删除/详情用例补落库断言 | `python tools/maint/add_db_assertions.py --dry-run` |
| `audit_db_assertions.py` | 盘点"该查库却没查库"的用例（写接口/详情接口） | `python tools/maint/audit_db_assertions.py --detail` |
| `survey_assertions.py` | 盘点还有多少用例只断言了 status_code + success | `python tools/maint/survey_assertions.py --weak-only` |
| `replay_assertions.py` | 实网登录挂掉时，离线验证断言能不能抓到缺陷 | `python tools/maint/replay_assertions.py` |
| `clean_dirty_rows.py` | 清掉库里含未渲染模板（`{{...}}`）的脏行，默认只列出 | `python tools/maint/clean_dirty_rows.py` |
| `dump_schema.py` | 连库导出表结构（写落库断言前先看真实表名/列名） | `python tools/maint/dump_schema.py --alias mall4j --format json` |
| `split_flat_excel.py` | 把「接口测试用例_全量.xlsx」拆成按模块的扁平格式 | `python tools/maint/split_flat_excel.py` |
| `audit_endpoints.py` | 核对用例里的接口路径和 Swagger 是否一致 | `python tools/maint/audit_endpoints.py` |
| `case_status.py` | 用例状态迁移（draft / debugged / broken / disabled） | `python tools/maint/case_status.py --help` |
| `scan_secrets.py` | 扫描源码里硬编码的密码 / token / 凭证 | `python tools/maint/scan_secrets.py` |
| `fix_detail_ids.py` | 详情用例里写死但库里不存在的 id，换成真实存在的（改前先查库） | `python tools/maint/fix_detail_ids.py --dry-run` |
| `view_graph.py` | 看知识图谱：命令行查接口/表/字段关系，或导出可交互 HTML | `python tools/maint/view_graph.py stats` |

---

## 什么时候用哪个

| 你想干的事 | 用哪个 |
|---|---|
| 跑接口用例 | `python scripts/run.py` |
| 跑框架单元测试 | `python scripts/run_unit.py` |
| 新模块要建用例 | `python scripts/new_case.py --module xxx` |
| 有接口文档，想批量生成用例 | `python -m tools.importer.cli export api.json --project mall4j` |
| 用例断言太弱 | `strengthen_assertions.py` + `add_db_assertions.py` |
| 想知道哪些用例没验库 | `audit_db_assertions.py` |
| 想知道还差哪些接口没测 | `/apitest-coverage` 或 `survey_assertions.py` |
| 想确认表名/列名 | `dump_schema.py --alias mall4j` |
