---
name: apitest-coverage
description: 分析接口覆盖情况，列出最该补用例的接口。当用户说"覆盖了多少""还缺哪些接口没测"时使用。
---

# 覆盖分析

## 步骤

1. `get_coverage(project="<项目>")` —— 拿覆盖率、未覆盖清单、只调没断言的清单
2. 对 `untested` 里前 5 个接口，用 `search_endpoints` / `get_endpoint` 看它们干什么
3. 对其中**写操作**接口用 `explain_endpoint` 看它的字段落到哪些表 ——
   这决定了"要补的不只是调用，还有落库断言"
4. `list_modules(project=...)` —— 看现有模块划分，决定新用例放哪

## 输出

```
## 覆盖率
接口覆盖率 x/y (z%) ｜ 断言覆盖率 z% ｜ 用例总数 n

## 最该补的接口（≤5 个，按业务重要性排序）
| method path | 干什么 | 建议模块 | 该测什么 |

## 只调没断言
| 接口 | 建议补什么断言 |
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
