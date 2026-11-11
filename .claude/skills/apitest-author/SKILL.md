---
name: apitest-author
description: 给指定接口写一条符合本框架规范的测试用例。当用户说"给 xx 接口写个用例""补一下 xx 模块的用例"时使用。
---

# 写一条接口测试用例

本项目默认用例来源是 **Excel 扁平格式**（`configs/config.yaml` 的
`case_source.mode`，默认 `excel`，文件在 `testcases/cases_excel/<项目>/<模块>/`）。
先跑一次 `list_modules` 确认当前在维护哪套用例，再决定写 Excel 还是 YAML。

## 先调研，再动笔（顺序不能乱）

1. `search_endpoints(query=...)` 找到接口
2. `get_endpoint(path, method)` 拿完整定义（参数类型、必填、枚举、响应字段）
3. `list_api_definitions(project, module)` —— 有现成接口定义就写 `api: <名字>` 引用，
   **不要重复写 path**（路径改了要改很多处）
4. `list_cases(project, module, limit=5)` 看同模块已有用例的风格，照着写
5. `explain_endpoint(path, method)` 看这个接口动的是哪些数据、现有用例覆盖到哪
6. 涉及写操作或要验落库时：`find_field_mapping(field, endpoint)` 拿字段对应的表列。
   **别自己按驼峰转下划线猜列名** —— 猜错的落库断言比没有断言更糟，因为看起来验过了。
   - `confidence: 1.0` 已验证，直接用
   - `0.9` 命名规则 + 表确认过，可以用，跑之前核对一眼
   - `0.6` 候选，**不要用来写断言**，先 `get_table_schema` 确认
   - 没有结果时再退回 `get_table_schema(keyword=...)` 手工确认

**关于置信度的口径**：已验证的证据是**分接口**的。`PUT /admin/area` 上验过的
映射不代表 `POST /admin/area` 也验证过 —— 别把别的接口的 1.0 套过来。

## 先设计测点，再写用例

一个接口不是写一条正例就完事。先按 `apitest-design` 的维度矩阵列测点，再挑有依据的写：

- **A 参数契约**：必填缺失 / 类型 / 枚举 / 长度 / 格式 / 空值
- **B 业务语义**：分页边界 / 排序过滤 / 幂等 / 状态迁移
- **C 权限**：未登录 / 无权限角色 / token 失效
- **D 数据一致性**：响应 vs DB / 写后能查 / 删后查不到
- **E 接口关联**：创建→查询→修改→删除 / FK 依赖
- **F 异常**：不存在 id / 已删除 id / 非法 path 参数

每个测点写清依据（元数据 / 图谱 / 现役用例 / 项目配置）。**没有依据的测点标
`needs_manual_setup`，不要编。**

## 怎么落盘

**Excel 用例（默认）**：用 `create_case`，不要手写文件。

```
create_case(
  project="mall4j", module="admin_area",
  path="/admin/area", method="PUT",
  title="修改地区（正常请求）", api_name="修改地区",
  assertions=[...],          # 框架内部格式，和 get_case 返回的一样
  request_data={...},
  priority="P1", marks="admin_area,smoke",
)
```

写操作（POST/PUT/PATCH/DELETE）正例的 `setups` / `teardowns` **不用自己写**：
`create_case` 会按接口文档自动补「造数 → 查真实 id → 清理」链路
（POST 补 teardowns；PUT/PATCH 补 setups+teardowns；DELETE 改成删本用例造的种子）。
返回里 `data_chain` 非空就是挂上了；为空说明规划不出安全链路（接口没有名称字段、
没有列表查询接口等），看 `quality_warnings` 处理。自己传了 `setups` / `teardowns`
就以你传的为准，工具不再重复规划。

它返回的 `case_id`（`API-189` 这种）就是后续所有写操作要用的句柄。
新建的用例是 `draft`，CI 不跑 —— 跑通之后才改状态：

1. `run_cases(case_file="testcases/cases_excel/mall4j/admin_area/admin_area.xlsx")`
2. 通过后 `set_case_status(case_id="API-189", status="debugged", note="新用例跑通")`

**YAML 用例**：直接给完整 YAML 内容（`testcase:` 开头），由用户保存。

## 断言怎么写

`assertions` 用**框架内部格式**（`{"type": ..., "field": ..., "expected": ...}`），
写工具会自动转成 Excel 单元格里的扁平格式，不用你操心。

- 每一步都必须有 `assert`，至少断言 `success` 和关键业务字段。
  只断言 HTTP 状态码等于没测。
- 字段路径**不带** `$.` 前缀：写 `data.records`，不写 `$.data.records`。
- 比较类断言（`equal` / `gte` / `length_gte` …）必须写 `expected`。
- 落库断言用 `{"type": "db_query", "query": "SELECT ...", "alias": "mall4j", "params": [...], "expected": ...}`。
  **只放主步骤的断言里，不要放到 teardowns 的验证步骤上** —— 后置只做清理。
  断言放在后置，失败会被报成 teardown 错误，主步骤先失败时还会多一条误导性的失败。
  例外：后置 DELETE 清理步骤上的「删后查不到」是清理验证，可以保留。
- 写操作（POST/PUT/DELETE）必须有造数 + 清理，但**交给 `create_case` 自动补**，
  自己手写只在这两种情况下考虑：
  - `data_chain` 为空（规划不出安全链路），需要人给 `setups` / `teardowns`
  - 有特殊前置（比如要先建父级分类），自动链路覆盖不到
  手写时 teardown **只能删本用例自己造的数据**（引用 `{{本用例步骤.id}}` 产出的 id，
  安全闸会自动放行）；硬编码 id 的清理会被运行时拒绝。

## 不要做的事

- 不要只写正例就宣布这个接口测完了 —— 按 `apitest-design` 的最小交付标准检查维度。
- 不要写没有依据的负例（服务端没校验的参数，断言"被拒绝"必然误报）。
- 不要写只断言 `status_code == 200` 的用例。
- 不要在 teardown 里删环境里本来就有的数据（比如固定 id）。
- 不要把落库断言放到 teardown 的验证步骤上（DELETE 清理验证除外）。
- 不要凭想象写字段名 —— 必须来自 `get_endpoint` / `get_table_schema`。
- 不要用 `apply_case_change` 去"修"一条因为服务端问题而失败的用例 ——
  改断言把失败变绿，等于把 bug 藏起来。

## 输出

落盘后给一句话结论：`case_id`、落在哪个文件、断言覆盖了哪些维度、下一步该跑什么。
如果发现接口定义和实际响应有出入，单独指出来，不要默默按自己的理解写。
