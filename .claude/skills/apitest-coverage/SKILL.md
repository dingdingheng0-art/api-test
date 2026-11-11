---
name: apitest-coverage
description: 分析接口覆盖情况，列出最该补用例的接口。当用户说"覆盖了多少""还缺哪些接口没测"时使用。
---

# 覆盖分析

## 四个维度，别只看接口数

覆盖率不是"调过没有"。按 `apitest-design` 分四维：

| 维度 | 判断方式 |
|---|---|
| 接口覆盖 | 有没有用例 |
| 断言覆盖 | 每步有没有业务字段断言（不是只有 `status_code` / `success`） |
| 落库覆盖 | 写接口有没有 confidence ≥ 0.9 的 DB 断言 |
| 关联覆盖 | CRUD 场景链 / FK 依赖是否完整，未解析的参数来源有没有列出来 |

**只报「接口覆盖率 xx%」是误导** —— 至少同时给断言覆盖和落库覆盖。

## 应测维度覆盖（有工具，别估）

`ai/pipeline/dimension_coverage.py` 按 `apitest-design` 的维度矩阵算：

```bash
python -m ai.pipeline.dimension_coverage \
  --project mall4j --module <模块> \
  --file .cache/apidoc/openapi.json --only-path /admin/<资源>
```

**它不认标签声明，只认用例内容**：标了 `C1` 却带鉴权头、标了 `D3` 却没有
`db_query`、标了 `E3` 却没有 teardown，都算未覆盖，并单独列进「标称未证实」。
报告里同时给薄用例（业务断言 < 2）和 `needs_manual_setup` 清单。

## 步骤

1. `get_coverage(project="<项目>")` —— 拿覆盖率、未覆盖清单、只调没断言的清单
2. 对 `untested` 里前 5 个接口，用 `search_endpoints` / `get_endpoint` 看它们干什么
3. 对其中**写操作**接口用 `explain_endpoint` 看它的字段落到哪些表 ——
   这决定了"要补的不只是调用，还有落库断言"
4. `list_modules(project=...)` —— 看现有模块划分，决定新用例放哪

## 输出

```
## 覆盖率（四维）
- 接口覆盖：x/y (z%)
- 断言覆盖：有业务字段断言的接口 a/b (z%)
- 落库覆盖：写接口有 DB 断言的 c/d (z%)
- 关联覆盖：完整 CRUD 链 e/f (z%)
- 用例总数：n

## 最该补的接口（≤5 个，按业务重要性排序）
| method path | 干什么 | 建议模块 | 该测什么维度 |
|---|---|---|---|

## 只调没断言 / 假覆盖
| 接口 | 缺什么维度 |
|---|---|
```

## 注意

- `untested` 的接口数可能是几十上百个，**不要全列**，只挑最重要的 5 个并说明挑选理由。
- 覆盖率的分母来自 Swagger/Apifox 同步（`.cache/apidoc/openapi.json`）。
  如果 `warnings` 里提示"接口清单不全"，先说明这个数字不可信，再给结论。
- 只做分析，不要顺手生成用例 —— 用户没要求就不要写文件。
  （确实要补的时候用 `create_case`，但那是用户明确要求之后的事，而且新建的用例是 draft，
  跑通之后才 `set_case_status` 改成 debugged。）
- 写操作接口的"覆盖"，光看有没有用例是不够的 —— 要看它写的字段有没有落库断言。
  `explain_endpoint` 返回的 `mappings` 里 `confidence >= 0.9` 的字段，没被断言验过的
  就是**假覆盖**，值得单独列一节。
- 判断"这条用例到底断言了什么"，用 `read_case_assertions(case_id)` 看原文，
  别只看 `get_case` 的 `assert_types` 摘要 —— `equal` 和 `is_not_none` 在摘要里
  都是一行，强度差得远。
