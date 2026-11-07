---
name: apitest-heal
description: 诊断失败用例并修复。先按归因分派——环境问题只重跑、服务问题只提缺陷、用例数据问题才改用例（预览 → 人审 → 落盘 → 跑一遍，不过自动回滚）。当用户说"这条为什么挂了""帮我修一下这条用例""自愈"时使用。
---

# 失败诊断与修复

**先归因，再动手。归因为服务问题的失败，一律不改用例。**

三步闸都是**代码拦的**，不是靠你自觉：

| 闸 | 在哪 | 拦住什么 |
|---|---|---|
| 服务问题不许改 | `apply_case_change` | 上一轮归因是「服务问题」时直接拒绝 |
| 断言只能变强 | `apply_case_change` | 删断言、把 `==` 改松（`exists` / `not_null`）会被拒 |
| 两轮还不通就停 | `apply_case_change` | 同一用例改两轮没跑通，第三次直接拒绝 |

被拒绝了**不要**想办法绕（比如手工编辑 Excel、换个编号）。拒绝本身就是结论。

## 严格按顺序执行

1. `get_last_result()` —— 确认它确实失败了，拿到错误信息。
2. `list_cases(keyword="<用例名>")` → `get_case(case_id)` —— 记下 `case_id`（`API-168` 这种）。
   写类工具和自愈工具只认它；同名用例（两条「查询地区详情」）只有编号能区分。
3. `plan_heal(case_id)` —— **这一步决定后面能做什么**。返回：
   - `attribution`：框架当场判的归因（拿着真实响应判的，比事后看文本准）
   - `can_edit_case` / `action` / `next_step`：允许的动作

## 按 action 分派

| action | 归因 | 动作 |
|---|---|---|
| `rerun` | 环境问题 | `run_cases(case_id=...)` 重跑确认。**不要动用例文件**。 |
| `file_defect` | 服务问题 | **停止**。整理请求/响应证据说明这是缺陷。确实短期修不了，`set_case_status(status="broken", note="...")` 标记 —— 用例内容保持原样。 |
| `manual_fix` | 脚本问题 | 按报错定位到具体文件（结构 / 变量引用 / 路径）。改断言解决不了这类问题。 |
| `heal` | 用例数据问题 | 继续取证（见下） |
| `stop` | 已经改两轮 | **停止**。把每轮改了什么、跑出来什么整理成结论交给用户。 |
| `manual_review` | 没归因 | **停止**。证据不足就明说，不要猜。 |

`can_edit_case=false` 时不要调 `apply_case_change` —— 工具会拒绝。

## 取证（只对 action=`heal`）

- `get_endpoint(path, method)` —— 接口定义变了吗？（字段改名 / 枚举值变了 / 路径加了前缀）
- `find_field_mapping(field, endpoint)` —— 落库断言的列名还对不对？
  - 映射变成"查不到"或置信度掉档 → 列可能改名了，`get_table_schema` 复核
  - 映射命中但 `confidence` 只有 0.6 → 这条断言本来就是猜的，要重点怀疑
- `explain_endpoint(path, method)` —— 这个接口的字段映射和覆盖用例有没有变化
- `read_case_assertions(case_id)` —— 现有断言的完整内容 + 扁平格式对照
- `query_db` 查实际数据长什么样（只读，安全）

**判断"断言是否过时"的口径**：先看 `get_endpoint` 有没有改字段，再看映射还在不在，
最后才看实际数据。**三项里只要有一项说明是服务端行为变了，就归到服务问题，不改用例。**

## 改的时候（三步，不能跳）

1. `propose_case_change(case_id, assertions=[...])`
   —— 只算 diff，不落盘（它没有 `note` 参数，`note` 只在落盘时记）。
   先自己看一眼 `diff.removed`：如果你不打算删的断言出现在里面，说明新断言列表写漏了。
2. `apply_case_change(case_id, assertions=[...], note="<为什么改>")`
   —— 落盘。`note` 会写进备份目录的 manifest，事后可追溯。
   有 `diff.removed` 或 `weakenings` 会被拒绝（设计如此）。先回头检查是不是断言写漏了；
   确认是接口契约变了才加 `allow_weakening=true`，并在 `note` 里写清楚依据。
3. `verify_heal(case_id, backup_dir="<上一步返回的 backup_dir>")`
   —— 只重跑这一条。**没通过会自动回滚**到改之前，并记一轮失败。
   没跑过就说"修好了"是不允许的；不传 `backup_dir` 就没法自动回滚。

如果这条用例本来就是 `draft` / `broken`，`verify_heal` 通过后记得
`set_case_status(case_id, "debugged", note="...")`。

## 输出格式

```
## 归因
<四类之一> —— <一句话>（来源：框架归因 / 规则 / AI）

## 证据
- <来源工具>：<关键发现>

## 改动
<case_id> 的断言：<N 条 → M 条>
```diff
-  {"type": "equal", "field": "status", "expected": 0}
+  {"type": "equal", "field": "status", "expected": 1}
```
理由：<引用证据>
备份：`.cache/backup/agent_<时间戳>/`

## 验证
verify_heal 结果：<通过 / 仍然失败：已回滚，原因>

## 风险
<断言强度是否持平/增强；有没有可能掩盖真实缺陷>
```

## 硬性约束

- **断言只能变强，不能变弱。** 把 `equal` 降级成 `not_null`、减少断言数量，一律禁止。
  工具会替你拦 —— **不要为了绕开它顺手传 `allow_weakening`**。
- **归因为服务问题的失败，不改断言、不改期望值。** 改成服务端实际返回的那个值，
  强度没变，但缺陷被藏起来了 —— 断言强度闸拦不住这种，所以归因闸先拦。
- 拿不出证据的改动不要提。
- 如果证据不足以下结论，就明说"证据不足，需要人工看"，不要猜，更不要改。
- 改了之后必须能说清楚"为什么这样改不会漏掉真实缺陷"。
- 同一条用例改了两轮还没跑通 → 停下来汇报，别再试第三次（工具也会拒）。
