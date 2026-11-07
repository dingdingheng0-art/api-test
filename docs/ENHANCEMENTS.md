# 增强能力手册

> 本文讲框架新加的七项能力：数据校验（数据库/缓存/MQ）、测试数据工厂、
> 用例级重试、令牌桶限流、AI 用例生成、AI 缺陷分析、CI/CD。
>
> 用例写法都还是 YAML/Excel，**不用写 Python**。

---

## 一分钟速览

| 能力 | 用起来是什么样 | 需要额外装什么 |
|---|---|---|
| 数据库校验 | `assert: [{type: db_query, query: "SELECT ...", expected: 2}]` | `mysql-connector-python` |
| 缓存校验 | `assert: [{type: redis_get, key: "order:1", expected: {...}}]` | `redis` |
| 消息队列校验 | `assert: [{type: mq_received, queue: "order.created", ...}]` | `pika`（或 `kafka-python`） |
| 测试数据工厂 | `setups: [{id: o, operation_type: factory, factory: mall4j.order}]` | 无 |
| 用例级重试 | 默认开着，只重试环境抖动 | 无 |
| 令牌桶限流 | 默认开着，按项目分别限 QPS | 无 |
| AI 用例生成 | `python -m ai.capabilities.case_gen --file 接口文档.json` | 无（不配 key 也能用） |
| AI 缺陷分析 | `python -m ai.capabilities.diagnosis` | 无（不配 key 也能用） |
| CI/CD | `python ci/run_ci.py --env pre` | 无 |

**所有新能力都是可选的** —— 不装对应的包、不配对应的地址，
框架照跑不误，只是那些断言会用一句人话告诉你"没配"，
而不是甩一个 ImportError 栈。

---

## 二、数据库校验

### 怎么写

```yaml
steps:
  - id: create_order
    path: /order/create
    method: POST
    data: {orderNo: "{{tools.gen_order_no()}}"}

  - id: check_db
    operation_type: noop
    assert:
      - type: db_query
        query: "SELECT status FROM orders WHERE order_no = 'SO123'"
        expected: 2
```

### 支持的断言类型

| 类型 | 干什么 | 额外参数 |
|---|---|---|
| `db_query` | 第一列（或 `column` 指定的列）等于期望值 | `column` |
| `db_query_exists` | 有结果就算过 | |
| `db_query_true` | 第一列是真值 | |
| `db_query_count` | 行数等于期望值 | |
| `db_query_gte` / `db_query_lte` | 数值比较 | |
| `db_query_in` | 第一列在给定集合里 | `container` |

### 配置

```yaml
# configs/config.yaml
db:
  default:
    type: mysql
    host: 127.0.0.1
    port: 3306
    user: root
    password: "${DB_PASSWORD}"
    database: mall4j
```

`type` 可以填 `mysql` 或 `sqlite`。sqlite 不用装任何东西，本地自测很方便：

```yaml
db:
  default:
    type: sqlite
    database: data/test.db
```

### 多个数据库

配置里可以按别名写好几套，用例里用 `alias` 指定：

```yaml
db:
  main:    {type: mysql, host: ..., database: shop}
  replica: {type: mysql, host: ..., database: shop_replica}
```

```yaml
assert:
  - type: db_query
    alias: replica
    query: "SELECT COUNT(*) FROM orders"
    expected: 0
```

### ⚠️ 关于 SQL 注入

`query` 里的 SQL 是**原样发给数据库**的，不做参数化。

用例里的 SQL 是自己写的，没问题；但**别把用户输入直接拼进去**。
需要传值时用 `params`：

```yaml
assert:
  - type: db_query
    query: "SELECT status FROM orders WHERE order_no = %s"
    params: ["{{order_no}}"]
    expected: 2
```

---

## 三、缓存校验（Redis）

### 怎么写

```yaml
assert:
  # 缓存里的值对不对（写 JSON 字符串还是对象都行，会自动比对）
  - type: redis_get
    key: "order:info:{{order_no}}"
    expected: {status: 2}

  # 缓存有没有写进去
  - type: redis_exists
    key: "order:info:{{order_no}}"
    expected: true

  # 缓存过期时间（还有多久就失效）
  - type: redis_ttl
    key: "order:info:{{order_no}}"
    expected: {lte: 3600}

  # 缓存有没有被删掉（"更新后删缓存"这类场景）
  - type: redis_deleted
    key: "order:info:{{order_no}}"
    expected: true
```

### 配置

```yaml
redis:
  default:
    host: 127.0.0.1
    port: 6379
    db: 0
    password: "${REDIS_PASSWORD}"
    decode_responses: true
```

多个 Redis 用别名，方式跟数据库一样。

### 小提示

- `redis_get` 的期望值写成 JSON 字符串还是对象都行 ——
  框架会尝试把缓存里的值解析成 JSON 再比。
- `redis_ttl` 的返回值里，`-1` 表示"存在但不过期"，`-2` 表示"key 不存在"。
- 失败时报告里会贴出"实际读到的值"，不用再去连 Redis 查。

---

## 四、消息队列校验

### 后端可插拔

不确定被测系统用哪种 MQ？做成适配层了，配什么就用什么：

- `rabbitmq`（用 `pika`）
- `kafka`（用 `kafka-python`）
- `memory`（纯内存模拟，不连任何东西）

### 怎么写

```yaml
steps:
  # 1) 先调业务接口
  - id: create_order
    path: /order/create
    method: POST
    data: {orderNo: "{{tools.gen_order_no()}}"}

  # 2) 再校验消息有没有发出来
  - id: check_mq
    operation_type: noop
    assert:
      - type: mq_received
        queue: order.created
        routing_key: order.created
        payload_contains: {orderNo: "{{create_order.result.orderNo}}"}
        timeout: 10
        expected: true
```

### 支持的断言类型

| 类型 | 干什么 |
|---|---|
| `mq_received` | 有没有收到匹配的消息 |
| `mq_count` | 收到几条 |
| `mq_payload` | 消息体里某个字段对不对（用 `field` 指定） |

### 配置

```yaml
mq:
  default:
    type: rabbitmq
    host: 127.0.0.1
    port: 5672
    user: guest
    password: "${MQ_PASSWORD}"
    vhost: "/"
```

Kafka 的写法：

```yaml
mq:
  default:
    type: kafka
    bootstrap_servers: "127.0.0.1:9092"
```

### 怎么保证不漏消息

框架在**发请求之前**就先把队列订阅好，然后才调接口 ——
所以不会"消息发得太快，订阅还没架好就漏了"。

---

## 五、测试数据工厂

### 为什么需要

造测试数据这事儿，每个用例各写各的，最后就是灾难：
数据忘了删、字段名写错、用例之间互相污染。

工厂解决三件事：

1. **造得规范** —— 一个工厂只知道怎么造"一条订单"
2. **自动清理** —— 用例挂了也会被清掉
3. **可复用** —— 同一个工厂在多条用例里用

### 怎么写用例

```yaml
setups:
  # 造数据
  - id: order
    operation_type: factory
    factory: mall4j.order
    params: {count: 1}

steps:
  # 造出来的东西可以引用
  - id: check
    operation_type: noop
    value: '{{order.first.orderNo}}'
    assert:
      - type: not_empty
        field: value

teardowns:
  # 清数据
  - id: clean
    operation_type: factory_cleanup
    factory: mall4j.order
```

引用方式：

| 写法 | 是什么 |
|---|---|
| `{{order.count}}` | 造了几条 |
| `{{order.items}}` | 全部（列表） |
| `{{order.items.0.orderNo}}` | 第 0 条的某个字段 |
| `{{order.first.orderNo}}` | 第一条的某个字段 |

### ⚠️ 自动清理（重要）

工厂造数据时会自动登记到**清理栈**。
session 结束（不管用例成功还是失败）会兜底清一遍。

这解决的是："用例中途挂了、teardown 没跑到，数据留在库里"这个经典问题。

### 怎么加自己的工厂

**第 1 步**：写一个类，继承 `DataFactory`

```python
# factory/mall4j.py
from factory.base import DataFactory

class CouponFactory(DataFactory):
    name = "coupon"

    def create(self, count=1, **params):
        # 造数据（调接口或写库都行）
        rows = []
        for _ in range(count):
            resp = self.api.post("/promotion/coupon/create", json_body={...})
            rows.append(resp["result"])

        # 登记主键 —— 不登记就兜底清不掉！
        self.track(row["id"] for row in rows)
        return rows

    def cleanup(self, keys):
        cleaned = 0
        for coupon_id in keys:
            self.api.post("/promotion/coupon/delete", json_body={"id": coupon_id})
            cleaned += 1
        return cleaned
```

**第 2 步**：在 `configs/factories.yaml` 里注册

```yaml
mall4j:
  coupon:
    module: factory.mall4j
    class: CouponFactory
```

**第 3 步**：用例里就能用了

```yaml
setups:
  - id: coupon
    operation_type: factory
    factory: mall4j.coupon
```

---

## 六、失败重试

### 两层重试

| 层 | 什么时候重试 | 说明 |
|---|---|---|
| **网络层** | 连接超时、连接断开 | 框架一直都有，重试 3 次 |
| **用例层** | 见下表 | 新加的，整条用例重跑 |

### 用例层只重试"环境问题"

| 失败原因 | 重试吗 | 为什么 |
|---|---|---|
| 连接超时 / 连接被重置 | ✅ | 网络抖动，重试就好 |
| 服务端 5xx | ✅ | 服务端临时问题 |
| 限流（code=1003） | ✅ | 等一下就好 |
| **断言不匹配** | ❌ | 这是真 bug，重试只是掩盖 |
| **业务返回失败** | ❌ | 用例预期的失败 |
| **前置数据没造出来** | ❌ | 配置或用例问题 |

**核心原则**：断言失败**永远不重试**。否则真 bug 会被重试掩盖，
报告全绿，人就放心了 —— 这是最危险的情况。

### 配置

```yaml
retry:
  enabled: true
  max_attempts: 3        # 总共试几次（含第一次）
  wait_seconds: 2        # 每次重试前等多久
  backoff: 1.5           # 等待时间的递增倍数
```

### 强制重试

某个接口确实不稳定（第三方服务抖），给它打 `flaky` 标签，
不管什么原因失败都重试：

```yaml
marks: [api, flaky]
```

### 关掉

```powershell
$env:APITEST_NO_RETRY = "1"
python scripts/run.py --no-retry
```

---

## 七、并发控制 + 令牌桶限流

### 为什么需要

接口测试常常一口气并发几十个请求，服务端直接返回限流
（mall4j 是 `code=1003` "访问过于频繁"），或者把测试环境的连接池打满。

表现出来就是"用例时好时坏" —— 最让人怀疑测试本身。

### 令牌桶怎么工作

- 桶里放令牌，桶有上限（`burst`）
- 每放一个令牌进来，就算"允许发一个请求"
- 请求来了就拿一个令牌；桶空了就在旁边等一会儿
- 按固定速率往桶里放令牌（`qps`）

和"固定间隔排队"比，令牌桶允许**短时间突发**（桶里攒着令牌），
但长期平均速率不会超过 `qps`。

### 配置

```yaml
rate_limit:
  enabled: true
  default:
    qps: 10          # 每秒最多几个请求
    burst: 20        # 突发上限（桶容量）
    wait_timeout: 30 # 拿不到令牌最多等多久（秒）
  projects:
    mall4j:
      qps: 5         # mall4j 单独限严一点
      burst: 10
```

**按项目分别限速** —— 统计接口限制严，就单独给它配低的 QPS，
别把整个项目的接口都卡住。

### 关掉

```powershell
$env:APITEST_NO_RATE_LIMIT = "1"
```

### 并行跑（可选）

装了 `pytest-xdist` 之后可以并行：

```powershell
pip install pytest-xdist
python -m pytest . -n 4      # 4 个进程并行
```

⚠️ **注意**：并行跑的时候，每个进程各有一套令牌桶，
所以实际 QPS 是"进程数 × 配置的 qps"。要真正限住，得把 qps 除以进程数。

---

## 八、Allure 报告增强

原有的层级、附件、分类都在，这轮补的是：

- **校验证据附件** —— 失败时能直接看到"数据库查到了什么""缓存里是什么值"
- **异常详情附件** —— 完整调用栈
- **新的失败分类** —— 报告里能一眼看出是"数据校验挂的"还是"接口挂的"：
  - 数据校验失败（数据库/缓存/MQ）
  - 前置/后置处理失败
  - 接口断言失败
  - 执行报错
  - 跳过

生成报告：

```powershell
python scripts/run.py --report allure
```

---

## 九、AI 用例生成

### 怎么用

```powershell
# 从接口文档生成
python -m ai.capabilities.case_gen --file 接口文档.json --project mall4j

# 强制只用本地模板（不联网）
python -m ai.capabilities.case_gen --file 接口文档.json --mode local

# 每个接口最多几条
python -m ai.capabilities.case_gen --file 接口文档.json --max-cases 5
```

支持的文档格式：OpenAPI/Swagger、HAR（抓包导出）、cURL 命令。

### 两种模式

| 模式 | 做什么 | 需要联网 |
|---|---|---|
| `ai` | 喂给大模型，生成正例 + 反例 + 边界值 | 是（没配 key 自动退回 local） |
| `local` | 按参数类型用模板规则造用例 | 否 |

**本地模式也能出用例** —— 规则是：

- 正例：用文档里的示例参数
- 必填缺失：去掉一个必填参数，期望被拒绝
- 空值：必填参数传空串，期望被拒绝
- 类型错误：参数换成明显不对的类型

### 配 AI 服务

OpenAI 兼容协议，DeepSeek / 通义 / 智谱 / Ollama 本地模型都能接。

```yaml
# configs/config.yaml
ai:
  base_url: "https://api.deepseek.com/v1"
  api_key: "${AI_API_KEY}"
  model: "deepseek-chat"
  timeout: 60
  temperature: 0.3
```

或者用环境变量（推荐，不落文件）：

```powershell
$env:AI_BASE_URL = "https://api.deepseek.com/v1"
$env:AI_API_KEY = "sk-xxxx"
$env:AI_MODEL = "deepseek-chat"
```

### ⚠️ AI 生成的用例要人工过一遍

模型可能理解错业务。生成的用例都带 `ai_generated` 标签，方便筛出来看：

```powershell
python scripts/run.py -m ai_generated --list
```

---

## 十、AI 缺陷分析

### 怎么用

```powershell
python -m ai.capabilities.diagnosis                # 规则 + AI
python -m ai.capabilities.diagnosis --mode rules   # 只用规则（快、不花钱）
```

产出：`reports/analysis/根因分析_<时间戳>.md`（还有一份 JSON 方便 CI 采集）

### 先规则，后 AI

**规则先判**：明显的环境问题（网络抖动、限流、5xx、登录态失效），
规则命中就直接给结论 —— 快、准、不花钱。

**AI 后判**：规则没命中的，把异常详情 + 断言 + 请求响应日志喂给模型，
让它分析根因。

### 分类表

| 分类 | 性质 | 说明 |
|---|---|---|
| 网络连接异常 | 环境问题 | 服务不通或连接被断 |
| 请求超时 | 环境问题 | 服务端太慢 |
| 服务端临时不可用 | 环境问题 | 5xx，重启或过载 |
| 服务端限流 | 环境问题 | 降并发或调大 qps |
| 登录态失效 | 环境问题 | uuid 过期，重抓 |
| 前置/后置处理失败 | 需排查 | 造数据那步就挂了 |
| 校验环境未就绪 | 需排查 | 数据库/缓存/MQ 没配好 |
| 用例数据问题 | 需排查 | YAML 语法错、字段名错 |
| **断言不匹配** | **需排查** | **真 bug，或预期写错了** |
| 依赖缺失 | 需排查 | 装包 |
| 未分类 | 需排查 | 人工看 |

---

## 十一、CI/CD

### 统一入口

```powershell
python ci/run_ci.py --env pre                    # 跑全部
python ci/run_ci.py --env pre --mark smoke        # 只跑冒烟
python ci/run_ci.py --env pre --dry-run           # 只看会跑什么，不真跑
```

做的事：检查依赖 → 跑用例 → 出报告 → 出根因分析 → 写汇总。

**退出码**：

- `0` —— 全过
- `1` —— 有用例失败
- `2` —— 脚本自己出了问题（配置错、依赖缺失）

CI 靠退出码判断成败，所以这个约定很重要。

### 产物

| 文件 | 内容 |
|---|---|
| `reports/result.json` | 用例统计（pytest 写的） |
| `reports/ci_summary.json` | CI 汇总（含报告路径、构建链接） |
| `reports/allure-results/` | allure 中间结果 |
| `reports/report.html` | HTML 报告 |
| `reports/analysis/` | 根因分析报告 |

### Jenkins

`ci/Jenkinsfile` 是声明式流水线，阶段：

1. 检出代码
2. 检查环境（装依赖）
3. **冒烟测试**（挂了直接停 —— 后面的全量回归没意义）
4. 全量回归
5. 归档报告

在 Jenkins 里建一个 Pipeline 项目，脚本指向 `ci/Jenkinsfile` 就行。

需要配的凭据：`mall4j-username`、`mall4j-password`。

### GitLab CI

`ci/.gitlab-ci.yml`，阶段：check → smoke → regression → report。

把它放到项目根目录（或者在 `.gitlab-ci.yml` 里 `include` 进来）。

需要配的变量（Settings → CI/CD → Variables）：

- `MALL4J_USERNAME`
- `MALL4J_PASSWORD`
- `AI_API_KEY`（可选）

---

## 十二、常见问题

| 现象 | 原因 / 怎么办 |
|---|---|
| `用例里用了缓存校验，但 config.yaml 里没有 redis 配置` | 加一段 `redis:` 配置 |
| `缓存校验需要 redis 包` | `pip install redis` |
| `RabbitMQ 校验需要 pika 包` | `pip install pika` |
| `没找到工厂 'xxx'` | 在 `configs/factories.yaml` 里注册 |
| 造的数据没被清掉 | 工厂的 `create()` 里忘了调 `self.track(...)` |
| 用例重试了但断言还是失败 | 断言失败**本来就不重试**，这是设计 |
| AI 用例生成报"没配置" | 配 `ai.base_url` / `api_key` / `model`，或者用 `--mode local` |
| 限流太严，跑得很慢 | 调大 `rate_limit` 里的 `qps` |
| 并行跑时还是被限流 | 每个进程各有一套令牌桶，qps 要除以进程数 |
| CI 里密码没生效 | 密码走环境变量 `MALL4J_PASSWORD`，别写在文件里 |
