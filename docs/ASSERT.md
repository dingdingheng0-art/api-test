# 断言速查

全部断言写在 YAML 的 `assert:` 下面，每条是「类型 + 字段 + 期望值」。

```yaml
assert:
  - type: equal
    field: code
    expected: 200
```

---

## 一、值比较

| 类型 | 别名 | 说明 |
|---|---|---|
| `equal` | `equals` `==` | 相等 |
| `not_equal` | `!=` | 不等 |
| `iequal` | `equal_ignore_case` | 相等（忽略大小写） |
| `inequal` | `not_equal_ignore_case` | 不等（忽略大小写） |
| `is_true` | `true` | 是 True |
| `is_false` | `false` | 是 False |
| `is_none` | `none` | 是 None |
| `is_not_none` | `not_none` | 不是 None |
| `empty` | `is_empty` | 空值（None / 空串 / 空列表 / 空字典） |
| `not_empty` | `is_not_empty` | 非空值 |
| `exists` | `field_exists` | 字段存在（哪怕值是 null） |
| `gt` `gte` `lt` `lte` | | 大小比较 |
| `length` | `len` | 长度等于 |
| `type` | `is_type` | 类型是 |
| `contains` / `not_contains` | | 包含 / 不包含 |
| `in` / `not_in` | | 在集合里 / 不在集合里 |
| `matches` | `regex` `match` | 正则匹配 |
| `not_matches` | `not_regex` | 正则不匹配 |
| `starts_with` | `startswith` | 以…开头 |
| `ends_with` | `endswith` | 以…结尾 |
| `approx_equal` | `approximately_equal` | 浮点近似相等 |
| `subset` | | 部分 JSON 匹配 |
| `jwt_claim` | `jwt` | JWT 里的 claim |

---

## 二、响应元信息

这三类断言不看响应体，看的是 HTTP 层面的东西。

### 状态码

```yaml
assert:
  - type: status_code
    expected: 200
```

期望值也可以写成比较条件：

```yaml
  - type: status_code
    expected: {lte: 299}
  - type: status_code
    expected: {gte: 200}
```

### 响应头

```yaml
assert:
  - type: response_header
    field: Content-Type
    expected: application/json
```

响应头名字大小写不敏感。

### 耗时（毫秒）

```yaml
assert:
  - type: elapsed_time
    expected: {lte: 3000}
  - type: elapsed_time
    expected: 128
```

### 短写法

`$` 前缀可以直接在 `field` 里取元信息，配合任意值比较类型用：

```yaml
assert:
  - type: equal
    field: $status          # 等价于 status_code
    expected: 200
  - type: contains
    field: $header.Set-Cookie
    expected: session
  - type: lt
    field: $elapsed
    expected: 1000
```

---

## 三、数据库

需要 `configs/config.yaml` 里配了 `mysql` 连接信息。

| 类型 | 说明 |
|---|---|
| `mysql_query` | 查询结果等于期望值 |
| `mysql_query_exists` | 有结果 |
| `mysql_query_true` | 结果是真值 |
| `mysql_query_count` | 行数等于期望值 |
| `mysql_query_gte` / `mysql_query_lte` | 数值比较 |
| `mysql_query_in` | 结果在给定集合里 |

```yaml
assert:
  - type: mysql_query
    query: SELECT status FROM orders WHERE id = 123
    expected: 2
```

### 指定列

默认取第一列。要取别的列加 `column`（写列名或从 0 开始的序号都行）：

```yaml
assert:
  - type: mysql_query
    query: SELECT id, status FROM orders WHERE id = 123
    column: status
    expected: 2
```

### 不知道表名字段名？先读表结构

写校验 SQL 最费时间的不是写 SELECT，而是"我到底要查哪张表、哪个字段"。
框架能自己连库把结构读出来。

```powershell
# 看有哪些表
python tools/maint/dump_schema.py --list

# 导出完整结构（Markdown 给人看，也能直接粘给 AI 当上下文）
python tools/maint/dump_schema.py -o docs/schema.md

# 只看名字里带 order 的表
python tools/maint/dump_schema.py --filter order
```

会连 `configs/config.yaml` 里 `db` 段配的库。读到的结构有缓存（默认 1 小时），
加 `--refresh` 强制重读。

也可以在代码里直接用：

```python
from verify.schema import SchemaInspector

inspector = SchemaInspector({"type": "mysql", "host": "...", "database": "shop"})
schema = inspector.inspect()

# 找表
table = schema.get_table("oms_order")
# 在整个库里找字段（返回 [(表, 字段), ...]）
hits = schema.find_columns("order_no")
```

**想知道真实表结构** —— 连库导出（写落库断言前先看一眼）：

```powershell
python tools/maint/dump_schema.py --alias mall4j --format json
```

---

## 几个容易踩的坑

**`is not None` 不等于"字段存在"** ——
`{"a": null}` 里 `a` 存在但值是 null。`is not None` 会失败，
要区分就用 `exists`。

**浮点数别用 `equal`** ——
`0.1 + 0.2 != 0.3`。金额类的用 `approx_equal`，默认容差 0.01，可以自己给：

```yaml
  - type: approx_equal
    field: result.amount
    expected: 10.0
    tolerance: 0.001
```

**`subset` 只比关心的字段** ——
服务端多返回的字段不会导致失败，嵌套对象也支持：

```yaml
  - type: subset
    field: result
    expected:
      status: 1
      user: {name: 张三}
```

**`field` 支持数组下标** —— 两种写法都行：
`result.items[0].sku` 或 `result.items.0.sku`

---

## Excel 里怎么写

跟 YAML 一栏一栏对应，用 `|` 分隔：

```
equal | code | 200
matches | result.orderNo | ^SO\d{14}$
subset | result.user | {"name": "张三"}
approx_equal | result.amount | 10.005 | 0.01
elapsed_time |  | {"lte": 3000}
status_code |  | 200
```

- 第三格留空 = 不要期望值（比如 `is not None | result.token`）
- 第四格在 `approx_equal` 里是**容差**，其他类型是 **container**
- 期望值写 JSON 对象 = 比较条件（`{"lte": 3000}`）

---

## 跑测试

```powershell
python -m pytest utils/test_assert_handler.py utils/excel/test_cells.py -v
```
